#!/usr/bin/env python3
"""Detect, run, and normalize output from external analysis tools.

Supports ruff, mypy, vulture (via subprocess) and coverage artifacts
(via file parsing).  Works when no external tools are installed — reports
availability and skips the rest.

Usage:
    python run_external_tools.py [path] [options]

Tool selection:
    --tools TOOL[,TOOL,...]     Run ONLY these tools
    --skip TOOL[,TOOL,...]      Run all available EXCEPT these
    --all                        Run all available tools (default)

See --help for full option list.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scan_common import (  # noqa: E402
    find_project_root,
)
from run_lint_rules import except_closure_annotation  # noqa: E402

_KNOWN_TOOLS = ("ruff", "mypy", "vulture", "coverage")

_TOOL_TIMEOUTS = {
    "ruff": 120,
    "mypy": 300,
    "vulture": 120,
}

# Ruff rule-prefix → category mapping.
_RUFF_DEAD_CODE_RULES = frozenset({"F401", "F811", "F841"})

_RUFF_CATEGORY_BY_PREFIX: dict[str, str] = {
    "F": "bug-risk",
    "B": "bug-risk",
    "SIM": "simplification",
    "S": "security",
    "RET": "simplification",
    "PIE": "dead-code",
    "UP": "deprecated",
    "PERF": "performance",
}


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def make_relative(filepath: str, project_root: Path) -> str:
    """Make *filepath* relative to *project_root*."""
    p = Path(filepath)
    if p.is_absolute():
        try:
            return str(p.relative_to(project_root))
        except ValueError:
            return filepath
    return filepath


def get_tool_version(cmd: str) -> str | None:
    """Get a tool's version string."""
    try:
        result = subprocess.run(
            [cmd, "--version"],
            capture_output=True, text=True, timeout=10,
        )
        return result.stdout.strip().split()[-1] if result.stdout else None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


# ---------------------------------------------------------------------------
# Project configuration detection
# ---------------------------------------------------------------------------

def _read_pyproject(project_root: Path) -> dict | None:
    """Read pyproject.toml if it exists, returning parsed dict or None."""
    path = project_root / "pyproject.toml"
    if not path.exists():
        return None
    try:
        import tomllib  # Python 3.11+
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except ImportError:
        pass
    try:
        import tomli as tomllib  # type: ignore[no-redef]
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except ImportError:
        pass
    # Fallback: simple string search for tool sections.
    return None


def _pyproject_has_section(project_root: Path, section: str) -> bool:
    """Check if pyproject.toml contains *section* (e.g. 'tool.ruff')."""
    data = _read_pyproject(project_root)
    if data is not None:
        parts = section.split(".")
        node = data
        for part in parts:
            if not isinstance(node, dict) or part not in node:
                return False
            node = node[part]
        return True
    # Fallback: raw text search.
    path = project_root / "pyproject.toml"
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    return f"[{section}]" in text


def _file_has_section(path: Path, section: str) -> bool:
    """Check if an INI-style file contains [section]."""
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    return f"[{section}]" in text


def has_project_config(tool_name: str, project_root: Path) -> bool:
    """Check whether *project_root* has configuration for *tool_name*."""
    if tool_name == "ruff":
        if _pyproject_has_section(project_root, "tool.ruff"):
            return True
        if (project_root / "ruff.toml").exists():
            return True
        if (project_root / ".ruff.toml").exists():
            return True
        return False

    if tool_name == "mypy":
        if _pyproject_has_section(project_root, "tool.mypy"):
            return True
        if (project_root / "mypy.ini").exists():
            return True
        if (project_root / ".mypy.ini").exists():
            return True
        if _pyproject_has_section(project_root, "mypy"):
            return True
        if _file_has_section(project_root / "setup.cfg", "mypy"):
            return True
        return False

    if tool_name == "vulture":
        if _pyproject_has_section(project_root, "tool.vulture"):
            return True
        if (project_root / ".vulture_whitelist.py").exists():
            return True
        return False

    if tool_name == "coverage":
        if _pyproject_has_section(project_root, "tool.coverage"):
            return True
        if (project_root / ".coveragerc").exists():
            return True
        if _file_has_section(project_root / "setup.cfg", "coverage:run"):
            return True
        return False

    return False


def find_config_file(tool_name: str, project_root: Path) -> str | None:
    """Return the path to the config file for *tool_name*, or None."""
    if tool_name == "ruff":
        for name in ("ruff.toml", ".ruff.toml"):
            if (project_root / name).exists():
                return name
        if _pyproject_has_section(project_root, "tool.ruff"):
            return "pyproject.toml"
        return None

    if tool_name == "mypy":
        for name in ("mypy.ini", ".mypy.ini"):
            if (project_root / name).exists():
                return name
        if _pyproject_has_section(project_root, "tool.mypy"):
            return "pyproject.toml"
        if _file_has_section(project_root / "setup.cfg", "mypy"):
            return "setup.cfg"
        return None

    if tool_name == "vulture":
        if _pyproject_has_section(project_root, "tool.vulture"):
            return "pyproject.toml"
        if (project_root / ".vulture_whitelist.py").exists():
            return ".vulture_whitelist.py"
        return None

    if tool_name == "coverage":
        if (project_root / ".coveragerc").exists():
            return ".coveragerc"
        if _pyproject_has_section(project_root, "tool.coverage"):
            return "pyproject.toml"
        if _file_has_section(project_root / "setup.cfg", "coverage:run"):
            return "setup.cfg"
        return None

    return None


# ---------------------------------------------------------------------------
# Tool detection
# ---------------------------------------------------------------------------

def detect_tools(project_root: Path) -> dict[str, dict]:
    """Detect which tools are available and their versions."""
    tools: dict[str, dict] = {}

    for name in ("ruff", "mypy", "vulture"):
        path = shutil.which(name)
        project_config = has_project_config(name, project_root)
        config_file = find_config_file(name, project_root)
        if path:
            version = get_tool_version(name)
            tools[name] = {
                "available": True,
                "version": version,
                "project_config": project_config,
                "config_file": config_file,
            }
        else:
            tools[name] = {
                "available": False,
                "version": None,
                "project_config": project_config,
                "config_file": config_file if project_config else None,
            }

    # Coverage — look for artifacts, not a binary.
    coverage_artifacts = find_coverage_artifacts(project_root)
    coverage_importable = check_coverage_importable()
    tools["coverage"] = {
        "available": bool(coverage_artifacts),
        "artifacts": coverage_artifacts,
        "coverage_importable": coverage_importable,
    }

    return tools


def detect_configured_missing(
    project_root: Path, availability: dict[str, dict],
) -> list[dict]:
    """Find tools that have project config but aren't installed."""
    missing: list[dict] = []
    for tool_name, info in availability.items():
        if tool_name == "coverage":
            continue
        if not info["available"] and info.get("project_config"):
            missing.append({
                "tool": tool_name,
                "config_file": info.get("config_file"),
                "message": (
                    f"{tool_name} is configured in {info.get('config_file')} "
                    f"but is not installed. Install it to get additional "
                    f"analysis findings."
                ),
            })
    return missing


def resolve_tool_selection(
    args: argparse.Namespace, availability: dict[str, dict],
) -> list[str]:
    """Determine which tools to run based on CLI flags and availability."""
    if args.tools:
        requested = [t.strip() for t in args.tools.split(",")]
        return [t for t in requested if availability.get(t, {}).get("available")]
    if args.skip:
        skipped = {t.strip() for t in args.skip.split(",")}
        return [
            t for t in _KNOWN_TOOLS
            if t not in skipped and availability.get(t, {}).get("available")
        ]
    # Default: all available.
    return [
        t for t in _KNOWN_TOOLS
        if availability.get(t, {}).get("available")
    ]


# ---------------------------------------------------------------------------
# Ruff
# ---------------------------------------------------------------------------

def classify_ruff_category(rule: str) -> str:
    """Map a ruff rule code to a finding category."""
    if rule in _RUFF_DEAD_CODE_RULES:
        return "dead-code"
    prefix = rule.rstrip("0123456789")
    return _RUFF_CATEGORY_BY_PREFIX.get(prefix, "style")


def classify_ruff_severity(rule: str) -> str:
    """Map a ruff rule code to a severity level."""
    if rule.startswith(("F821", "F811", "B")):
        return "error"
    if rule.startswith("S"):
        return "warning"
    return "warning"


def normalize_ruff_finding(finding: dict, project_root: Path) -> dict:
    """Normalize a single ruff JSON finding."""
    rule = finding.get("code", "")
    fixable = finding.get("fix") is not None
    return {
        "rule": rule,
        "rule_name": finding.get("message", ""),
        "severity": classify_ruff_severity(rule),
        "file": make_relative(finding.get("filename", ""), project_root),
        "line": finding.get("location", {}).get("row", 0),
        "column": finding.get("location", {}).get("column", 0),
        "message": finding.get("message", ""),
        "category": classify_ruff_category(rule),
        "fixable": fixable,
        "significance": "reduced" if fixable else "normal",
    }


def _flag_known_tool_bugs(
    finding: dict, filename: str | None, closures: dict,
) -> dict:
    """Re-label a hit that matches a known ruff false-positive mode.

    The case handled is an ``except ... as exc`` binding read from a closure
    nested in that handler: ruff 0.16.0 reports the read as F821 and the
    binding as F841. Unlabelled, the F821 hits were shown as ``bug-risk``,
    and the F841 hits offered a "safe" autofix (dropping ``as exc``) that
    breaks the closure with a NameError. They get their own category so they
    leave the bug-risk and dead-code feeds, and F841 is marked
    ``autofix_unsafe`` so no agent recommends the fix.
    """
    annotation = except_closure_annotation(
        finding["rule"],
        finding["message"],
        finding["line"],
        Path(filename) if filename else None,
        closures,
    )
    if annotation:
        finding.update(annotation)
        finding["category"] = "suspected-tool-false-positive"
        finding["significance"] = "reduced"
    return finding


def _extract_rules_applied(findings: list[dict]) -> list[str]:
    """Extract the set of unique rule codes from ruff findings."""
    return sorted({f.get("code", "") for f in findings if f.get("code")})


def run_ruff(
    args: argparse.Namespace, project_root: Path, scan_root: Path,
) -> dict:
    """Run ruff and return normalized findings."""
    # --no-cache: ruff otherwise writes `.ruff_cache/` into the reviewed project.
    cmd = ["ruff", "check", "--output-format=json", "--no-cache"]

    if args.ignore_config or not has_project_config("ruff", project_root):
        # DEFECT-GRADE selection is `run_lint_rules.py`'s job, not this script's.
        # This script answers "what does the PROJECT's own tooling say?"; when
        # the project has no ruff config there is nothing to report, so fall
        # back to ruff's own default rather than inventing a second, competing
        # curated list. The previous default here was
        # `F,B,SIM,S,RET,PIE,UP,PERF`, which measured 65-92% style-grade across
        # the three benchmark corpora while missing a quarter of the tier-1
        # defects (it excluded PL, RUF, DTZ and ASYNC entirely). Shipping it
        # alongside run_lint_rules.py's tiers would put two contradictory ruff
        # configurations in one toolkit.
        if args.ruff_rules:
            cmd.extend(["--select", args.ruff_rules])
            config_source = "explicit"
        else:
            config_source = "ruff-default"
    else:
        if args.ruff_config:
            cmd.extend(["--config", args.ruff_config])
        config_source = "project"

    cmd.append(str(scan_root))

    result = subprocess.run(
        cmd, capture_output=True, text=True,
        timeout=_TOOL_TIMEOUTS["ruff"],
    )

    findings = json.loads(result.stdout) if result.stdout else []

    closures: dict = {}
    normalized = [
        _flag_known_tool_bugs(
            normalize_ruff_finding(f, project_root), f.get("filename"), closures
        )
        for f in findings[:args.max_findings]
    ]

    return {
        "config_source": config_source,
        "rules_applied": _extract_rules_applied(findings),
        "total_findings": len(findings),
        "findings_capped": len(findings) > args.max_findings,
        # Hits re-labelled `suspected-tool-false-positive`; see each one's
        # `tool_bug_note`.
        "suspected_tool_false_positives": sum(
            1 for f in normalized if f.get("suspect_tool_bug")
        ),
        "findings": normalized,
    }


# ---------------------------------------------------------------------------
# Mypy
# ---------------------------------------------------------------------------

_MYPY_TEXT_RE = re.compile(
    r"^(.+?):(\d+)(?::(\d+))?: (error|warning|note): (.+?)(?:\s+\[(.+?)\])?$"
)


def parse_mypy_text_line(line: str) -> dict | None:
    """Parse a mypy text output line: file:line:col: severity: message [code]."""
    m = _MYPY_TEXT_RE.match(line)
    if not m:
        return None
    return {
        "file": m.group(1),
        "line": int(m.group(2)),
        "column": int(m.group(3)) if m.group(3) else 0,
        "severity": m.group(4),
        "message": m.group(5),
        "code": m.group(6) or "",
    }


def parse_mypy_output(
    stdout: str, max_findings: int = 0,
) -> list[dict]:
    """Parse mypy output, trying JSON first then text fallback."""
    findings: list[dict] = []
    for line in stdout.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            findings.append(json.loads(line))
        except json.JSONDecodeError:
            parsed = parse_mypy_text_line(line)
            if parsed:
                findings.append(parsed)
        if max_findings > 0 and len(findings) >= max_findings:
            break
    return findings


def normalize_mypy_finding(finding: dict, project_root: Path) -> dict:
    """Normalize a single mypy finding."""
    return {
        "code": finding.get("code", ""),
        "severity": finding.get("severity", "error"),
        "file": make_relative(finding.get("file", ""), project_root),
        "line": finding.get("line", 0),
        "column": finding.get("column", 0),
        "message": finding.get("message", ""),
        "category": "type-error",
        "fixable": False,
        "significance": "normal",
    }


def run_mypy(
    args: argparse.Namespace, project_root: Path, scan_root: Path,
) -> dict:
    """Run mypy and return normalized findings."""
    cmd = ["mypy", "--output=json"]

    if args.ignore_config or not has_project_config("mypy", project_root):
        cmd.extend(["--ignore-missing-imports", "--no-error-summary"])
        if args.mypy_strict:
            cmd.append("--strict")
        config_source = "curated"
    else:
        config_source = "project"

    # mypy's default cache is `.mypy_cache/` in the working directory, which is
    # usually the reviewed project. Give it a private cache and remove it
    # afterwards, so the review leaves nothing behind. A real directory rather
    # than `/dev/null`: the run stays an ordinary incremental run.
    cache_dir = tempfile.mkdtemp(prefix="crt-mypy-cache-")
    cmd.extend(["--cache-dir", cache_dir])
    cmd.append(str(scan_root))

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=_TOOL_TIMEOUTS["mypy"],
        )
    finally:
        shutil.rmtree(cache_dir, ignore_errors=True)

    findings = parse_mypy_output(
        result.stdout, max_findings=args.max_findings,
    )

    normalized = [
        normalize_mypy_finding(f, project_root)
        for f in findings[:args.max_findings]
    ]

    return {
        "config_source": config_source,
        "strict_mode": args.mypy_strict,
        "total_findings": len(findings),
        "findings_capped": len(findings) > args.max_findings,
        "findings": normalized,
    }


# ---------------------------------------------------------------------------
# Vulture
# ---------------------------------------------------------------------------

_VULTURE_RE = re.compile(
    r"^(.+?):(\d+): unused (\w+) ['\"](.+?)['\"] \((\d+)% confidence\)"
)


def parse_vulture_output(
    stdout: str,
    project_root: Path,
    max_findings: int = 0,
) -> list[dict]:
    """Parse vulture's text output into normalized findings."""
    findings: list[dict] = []
    for line in stdout.strip().splitlines():
        m = _VULTURE_RE.match(line)
        if m:
            findings.append({
                "type": f"unused-{m.group(3).lower()}",
                "name": m.group(4),
                "file": make_relative(m.group(1), project_root),
                "line": int(m.group(2)),
                "confidence": int(m.group(5)),
                "category": "dead-code",
                "fixable": False,
                "significance": "normal",
            })
            if max_findings > 0 and len(findings) >= max_findings:
                break
    return findings


def run_vulture(
    args: argparse.Namespace, project_root: Path, scan_root: Path,
) -> dict:
    """Run vulture and return normalized findings."""
    min_confidence = args.vulture_min_confidence

    cmd = ["vulture", str(scan_root), f"--min-confidence={min_confidence}"]

    whitelist = project_root / ".vulture_whitelist.py"
    if whitelist.exists():
        cmd.append(str(whitelist))

    result = subprocess.run(
        cmd, capture_output=True, text=True,
        timeout=_TOOL_TIMEOUTS["vulture"],
    )

    findings = parse_vulture_output(
        result.stdout, project_root,
        max_findings=args.max_findings,
    )
    normalized = findings[:args.max_findings]

    return {
        "min_confidence": min_confidence,
        "total_findings": len(findings),
        "findings_capped": len(findings) > args.max_findings,
        "findings": normalized,
    }


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------

def check_coverage_importable() -> bool:
    """Check if the ``coverage`` package can be imported."""
    try:
        import coverage as _cov  # noqa: F401
        return True
    except ImportError:
        return False


def find_coverage_artifacts(project_root: Path) -> list[dict]:
    """Find existing coverage data files."""
    candidates: list[tuple[Path, str]] = [
        (project_root / ".coverage", "sqlite"),
        (project_root / "coverage.xml", "cobertura_xml"),
        (project_root / "coverage.json", "json"),
    ]
    for subdir in ("reports", "test-reports", "build", "htmlcov"):
        d = project_root / subdir
        if d.is_dir():
            candidates.extend([
                (d / "coverage.xml", "cobertura_xml"),
                (d / "coverage.json", "json"),
            ])

    found: list[dict] = []
    for path, fmt in candidates:
        if path.exists():
            mtime = path.stat().st_mtime
            found.append({
                "path": str(path.relative_to(project_root)),
                "format": fmt,
                "modified": datetime.fromtimestamp(
                    mtime, tz=timezone.utc
                ).isoformat(),
                "mtime": mtime,
            })
    return found


def assess_coverage_freshness(
    artifacts: list[dict], project_root: Path,
) -> dict:
    """Assess coverage data freshness relative to the codebase."""
    skip_dirs = {
        ".git", ".tox", ".venv", "venv", "__pycache__",
        "node_modules", ".eggs", "build", "dist",
    }

    latest_source_mtime = 0.0
    for py_file in project_root.rglob("*.py"):
        parts = set(py_file.relative_to(project_root).parts)
        if parts & skip_dirs:
            continue
        try:
            mtime = py_file.stat().st_mtime
            if mtime > latest_source_mtime:
                latest_source_mtime = mtime
        except OSError:
            continue

    if not artifacts or latest_source_mtime == 0.0:
        return {"status": "unknown", "message": "Cannot determine freshness"}

    newest_artifact = max(artifacts, key=lambda a: a["mtime"])
    artifact_mtime = newest_artifact["mtime"]

    diff_seconds = latest_source_mtime - artifact_mtime
    diff_days = diff_seconds / 86400

    if diff_seconds <= 0:
        return {
            "status": "fresh",
            "message": "Coverage data is up to date",
            "artifact": newest_artifact["path"],
        }
    if diff_days <= 3:
        return {
            "status": "slightly_stale",
            "message": (
                f"Coverage data is {diff_days:.1f} days older than the "
                f"latest source changes. Results may not reflect recent "
                f"modifications."
            ),
            "artifact": newest_artifact["path"],
            "days_behind": round(diff_days, 1),
        }
    return {
        "status": "stale",
        "message": (
            f"Coverage data is {diff_days:.1f} days older than the "
            f"latest source changes. Results are likely outdated. "
            f"Consider re-running your test suite with coverage."
        ),
        "artifact": newest_artifact["path"],
        "days_behind": round(diff_days, 1),
    }


def parse_coverage_xml(path: Path, project_root: Path) -> dict:
    """Parse Cobertura-format coverage XML."""
    tree = ET.parse(path)
    root = tree.getroot()

    total_statements = 0
    total_covered = 0
    files: list[dict] = []

    for package in root.findall(".//package"):
        for cls in package.findall("classes/class"):
            filename = cls.get("filename", "")
            lines = cls.findall("lines/line")

            statements = len(lines)
            covered = sum(1 for ln in lines if int(ln.get("hits", "0")) > 0)
            missing = [
                int(ln.get("number", "0"))
                for ln in lines
                if int(ln.get("hits", "0")) == 0
            ]

            total_statements += statements
            total_covered += covered

            if statements > 0:
                files.append({
                    "file": make_relative(filename, project_root),
                    "statements": statements,
                    "covered": covered,
                    "missing_lines": missing,
                    "coverage_percent": round(100 * covered / statements, 1),
                })

    return {
        "source": str(path.relative_to(project_root)),
        "total_statements": total_statements,
        "total_covered": total_covered,
        "coverage_percent": (
            round(100 * total_covered / total_statements, 1)
            if total_statements > 0 else 0
        ),
        "files": sorted(files, key=lambda f: f["coverage_percent"]),
    }


def parse_coverage_json(path: Path, project_root: Path) -> dict:
    """Parse coverage.py JSON report."""
    data = json.loads(path.read_text(encoding="utf-8"))

    total = data.get("totals", {})
    files_data = data.get("files", {})

    files: list[dict] = []
    for filename, info in files_data.items():
        summary = info.get("summary", {})
        files.append({
            "file": make_relative(filename, project_root),
            "statements": summary.get("num_statements", 0),
            "covered": summary.get("covered_lines", 0),
            "missing_lines": info.get("missing_lines", []),
            "coverage_percent": summary.get("percent_covered", 0),
        })

    return {
        "source": str(path.relative_to(project_root)),
        "total_statements": total.get("num_statements", 0),
        "total_covered": total.get("covered_lines", 0),
        "coverage_percent": total.get("percent_covered", 0),
        "files": sorted(files, key=lambda f: f["coverage_percent"]),
    }


def parse_coverage_sqlite(path: Path, project_root: Path) -> dict | None:
    """Parse .coverage SQLite database using the coverage package."""
    try:
        from coverage import CoverageData
    except ImportError:
        return None

    covdata = CoverageData(basename=str(path))
    covdata.read()

    files: list[dict] = []
    for filename in covdata.measured_files():
        lines = covdata.lines(filename)
        if lines is None:
            continue
        files.append({
            "file": make_relative(filename, project_root),
            "covered_lines": sorted(lines),
            "note": (
                "Statement count unavailable from .coverage; "
                "use coverage.json or coverage.xml for complete data"
            ),
        })

    return {
        "source": str(path.relative_to(project_root)),
        "note": (
            "Parsed from .coverage SQLite; for complete data generate "
            "coverage.json or coverage.xml"
        ),
        "files": files,
    }


def read_coverage_data(
    artifacts: list[dict],
    project_root: Path,
    require_fresh: bool = False,
    freshness: dict | None = None,
) -> dict | None:
    """Read coverage data from the best available artifact.

    Priority: json > cobertura_xml > sqlite (sqlite needs coverage pkg).
    """
    if require_fresh and freshness and freshness.get("status") != "fresh":
        return None

    preference = {"json": 0, "cobertura_xml": 1, "sqlite": 2}
    sorted_artifacts = sorted(
        artifacts, key=lambda a: preference.get(a["format"], 99),
    )

    for artifact in sorted_artifacts:
        full_path = project_root / artifact["path"]
        try:
            if artifact["format"] == "json":
                return parse_coverage_json(full_path, project_root)
            if artifact["format"] == "cobertura_xml":
                return parse_coverage_xml(full_path, project_root)
            if artifact["format"] == "sqlite":
                return parse_coverage_sqlite(full_path, project_root)
        except Exception:
            continue

    return None


def run_coverage(
    args: argparse.Namespace, project_root: Path, scan_root: Path,
) -> dict:
    """Read and normalize existing coverage artifacts."""
    artifacts = find_coverage_artifacts(project_root)
    if not artifacts:
        return {
            "error": "No coverage artifacts found",
            "total_findings": 0,
            "findings": [],
        }

    freshness = assess_coverage_freshness(artifacts, project_root)

    data = read_coverage_data(
        artifacts, project_root,
        require_fresh=args.coverage_require_fresh,
        freshness=freshness,
    )

    if data is None and args.coverage_require_fresh:
        return {
            "freshness": freshness,
            "skipped": True,
            "reason": "Coverage data is not fresh and --coverage-require-fresh was set",
            "total_findings": 0,
            "findings": [],
        }

    return {
        "freshness": freshness,
        "data": data,
        "total_findings": 0,
        "findings": [],
    }


# ---------------------------------------------------------------------------
# Safe runner
# ---------------------------------------------------------------------------

_TOOL_RUNNERS = {
    "ruff": run_ruff,
    "mypy": run_mypy,
    "vulture": run_vulture,
    "coverage": run_coverage,
}


def run_tool_safely(
    tool_name: str,
    args: argparse.Namespace,
    project_root: Path,
    scan_root: Path,
) -> dict:
    """Run a tool function, catching all errors."""
    runner = _TOOL_RUNNERS.get(tool_name)
    if runner is None:
        return {"error": f"Unknown tool: {tool_name}", "total_findings": 0, "findings": []}
    try:
        return runner(args, project_root, scan_root)
    except subprocess.TimeoutExpired:
        return {
            "error": f"{tool_name} timed out",
            "total_findings": 0,
            "findings": [],
        }
    except FileNotFoundError:
        return {
            "error": f"{tool_name} binary not found (was it uninstalled?)",
            "total_findings": 0,
            "findings": [],
        }
    except Exception as exc:
        return {
            "error": f"{tool_name} failed: {exc}",
            "total_findings": 0,
            "findings": [],
        }


# ---------------------------------------------------------------------------
# Output assembly
# ---------------------------------------------------------------------------

def build_skipped_report(
    availability: dict[str, dict], tools_run: list[str],
) -> list[dict]:
    """Build a list of tools that were not run and why."""
    skipped: list[dict] = []
    for name in _KNOWN_TOOLS:
        if name in tools_run:
            continue
        info = availability.get(name, {})
        if not info.get("available"):
            skipped.append({"tool": name, "reason": "not installed"})
        else:
            skipped.append({"tool": name, "reason": "excluded by user"})
    return skipped


def build_output(
    project_root: Path,
    scan_root: Path,
    availability: dict[str, dict],
    tools_run: list[str],
    results: dict[str, dict],
    configured_missing: list[dict],
) -> dict:
    """Assemble the final JSON output."""
    output: dict = {
        "project_root": str(project_root),
        "scan_root": str(scan_root),
        "tools_available": availability,
        "tools_run": tools_run,
        "tools_skipped": build_skipped_report(availability, tools_run),
        "configured_but_not_installed": configured_missing,
    }

    for tool in tools_run:
        output[tool] = results[tool]

    return output


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Detect, run, and normalize output from external analysis tools.",
    )
    parser.add_argument(
        "path", nargs="?", default=".",
        help="Directory or file to analyze (default: current directory)",
    )

    # Tool selection.
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--tools", default=None,
        help="Run ONLY these tools (comma-separated, e.g. 'ruff,mypy')",
    )
    group.add_argument(
        "--skip", default=None,
        help="Run all available EXCEPT these (comma-separated)",
    )
    group.add_argument(
        "--all", action="store_true", default=True, dest="run_all",
        help="Run all available tools (default)",
    )

    # Ruff options.
    parser.add_argument("--ruff-config", default=None, help="Path to ruff config file")
    parser.add_argument(
        "--ruff-rules", default=None,
        help="Override rule selection (comma-separated, e.g. 'F,B,SIM')",
    )

    # Mypy options.
    parser.add_argument(
        "--mypy-strict", action="store_true", default=False,
        help="Run mypy in strict mode",
    )

    # Vulture options.
    parser.add_argument(
        "--vulture-min-confidence", type=int, default=80,
        help="Minimum confidence threshold (default: 80)",
    )

    # Coverage options.
    parser.add_argument(
        "--coverage-file", default=None,
        help="Path to specific coverage artifact",
    )
    parser.add_argument(
        "--coverage-require-fresh", action="store_true", default=False,
        help="Only use coverage data fresher than the codebase",
    )

    # General.
    parser.add_argument(
        "--max-findings", type=int, default=200,
        help="Cap findings per tool (default: 200)",
    )
    config_group = parser.add_mutually_exclusive_group()
    config_group.add_argument(
        "--respect-config", action="store_false", dest="ignore_config",
        help="Prefer project's tool config when it exists (default)",
    )
    config_group.add_argument(
        "--ignore-config", action="store_true", dest="ignore_config",
        help="Always use curated config, ignore project config",
    )
    parser.set_defaults(ignore_config=False)

    return parser.parse_args(argv)


def analyze(argv: list[str] | None = None) -> dict:
    """Main analysis pipeline — testable entry point."""
    args = parse_args(argv)
    target = Path(args.path).resolve()
    project_root = find_project_root(target)
    scan_root = target

    availability = detect_tools(project_root)
    configured_missing = detect_configured_missing(project_root, availability)
    tools_to_run = resolve_tool_selection(args, availability)

    results: dict[str, dict] = {}
    for tool in tools_to_run:
        results[tool] = run_tool_safely(tool, args, project_root, scan_root)

    return build_output(
        project_root, scan_root, availability,
        tools_to_run, results, configured_missing,
    )


def main() -> None:
    """Entry point."""
    result = analyze()
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
