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

from run_lint_rules import (  # noqa: E402
    detect_target_version,
    dismissal_reason,
    except_closure_annotation,
    unlinted_error,
    unlinted_paths,
)
from scan_common import (  # noqa: E402
    find_project_root,
)

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
    "BLE": "bug-risk",
    "SIM": "simplification",
    "S": "security",
    "RET": "simplification",
    "PIE": "dead-code",
    "UP": "deprecated",
    "PERF": "performance",
}

# The toolkit's selection for the per-agent category feeds, run as a second,
# labelled pass (see run_ruff_toolkit_pass). Keyed by the feed it fills; each
# code's category still comes from _RUFF_CATEGORY_BY_PREFIX.
_TOOLKIT_FEED_SELECTION: dict[str, tuple[str, ...]] = {
    "bug-risk": ("B", "BLE001"),
    "security": ("S",),
    "simplification": ("SIM", "RET"),
    "performance": ("PERF",),
    "deprecated": ("UP",),
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


# The first version-looking token: `mypy 1.19.0 (compiled: yes)` ends in
# "yes)", so the last whitespace token is not the version.
_VERSION_TOKEN = re.compile(r"\b\d+(?:\.\d+)+\S*")


def get_tool_version(cmd: str) -> str | None:
    """Get a tool's version string, or None when --version names none."""
    try:
        result = subprocess.run(
            [cmd, "--version"],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    match = _VERSION_TOKEN.search(result.stdout or "")
    return match.group(0).rstrip(",;)") if match else None


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
    finding: dict,
    filename: str | None,
    closures: dict,
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
        # configurations in one toolkit. (The agents' category feeds are filled
        # separately, by the labelled run_ruff_toolkit_pass below.)
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

    findings, stderr = _run_ruff_json(cmd)
    unlinted = unlinted_paths(stderr)

    closures: dict = {}
    normalized = [
        _flag_known_tool_bugs(
            normalize_ruff_finding(f, project_root), f.get("filename"), closures
        )
        for f in findings[:args.max_findings]
    ]
    for finding in normalized:
        finding["selection"] = config_source

    toolkit_findings, toolkit_pass = run_ruff_toolkit_pass(
        args, project_root, scan_root, normalized, closures,
    )

    output = {
        "config_source": config_source,
        "rules_applied": _extract_rules_applied(findings),
        "total_findings": len(findings),
        "findings_capped": len(findings) > args.max_findings,
        # Hits re-labelled `suspected-tool-false-positive`; see each one's
        # `tool_bug_note`.
        "suspected_tool_false_positives": sum(
            1 for f in normalized + toolkit_findings if f.get("suspect_tool_bug")
        ),
        # The second, toolkit-selected pass. Its findings are appended to
        # `findings` with `selection: "toolkit"`; `total_findings` and
        # `rules_applied` above describe the project's own pass only.
        "toolkit_pass": toolkit_pass,
        # Paths ruff could not read ("Failed to lint" on stderr, exit 0).
        "unlinted": unlinted,
        "findings": normalized + toolkit_findings,
    }
    if unlinted:
        # Never a clean result: those files were not linted at all.
        output["error"] = unlinted_error(unlinted)
    return output


def _run_ruff_json(cmd: list[str]) -> tuple[list[dict], str]:
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=_TOOL_TIMEOUTS["ruff"],
    )
    findings = json.loads(result.stdout) if result.stdout else []
    return findings, result.stderr or ""


def run_ruff_toolkit_pass(
    args: argparse.Namespace,
    project_root: Path,
    scan_root: Path,
    project_findings: list[dict],
    closures: dict,
) -> tuple[list[dict], dict]:
    """Run the toolkit's feed selection; return (new findings, pass metadata).

    The project pass answers "what does the project's own config say?". When
    that config is narrow -- say, only E/F/W/I -- every category feed
    the agents read (bug-risk, security, simplification, performance,
    deprecated) comes back empty, and each agent had to re-run ruff by hand.
    This pass fills those feeds with a fixed selection, labelled as the
    toolkit's, and drops any hit the project pass already reported.

    It is a FEED for the review agents, not a defect pass: the tiered
    defect-grade selection stays in run_lint_rules.py.
    """
    selection = {cat: list(codes) for cat, codes in _TOOLKIT_FEED_SELECTION.items()}
    meta: dict = {
        "ran": False,
        "selection": selection,
        "note": (
            "The toolkit's own selection, NOT the project's ruff config. A hit "
            "here may be a rule the project chose not to enable: treat it as "
            "POLICY unless it is a defect on its own merits."
        ),
    }
    if args.no_toolkit_ruff_pass:
        meta["skipped_reason"] = "--no-toolkit-ruff-pass"
        return [], meta
    if args.ruff_rules:
        meta["skipped_reason"] = "explicit --ruff-rules selection"
        return [], meta

    codes = sorted({c for group in selection.values() for c in group})
    cmd = [
        "ruff",
        "check",
        "--output-format=json",
        "--no-cache",
        "--select",
        ",".join(codes),
    ]
    if has_project_config("ruff", project_root) and not args.ignore_config:
        # The project's config still supplies target-version, excludes and
        # per-file-ignores; only the rule selection is the toolkit's.
        if args.ruff_config:
            cmd.extend(["--config", args.ruff_config])
    else:
        # Without a config ruff assumes its oldest target, which makes UP
        # report "deprecated" syntax the project could not have used.
        cmd.extend(["--target-version", detect_target_version(project_root)])
    cmd.append(str(scan_root))

    try:
        raw, stderr = _run_ruff_json(cmd)
    except (subprocess.TimeoutExpired, OSError, ValueError) as exc:
        meta["error"] = f"toolkit ruff pass failed: {exc}"
        return [], meta
    meta["ran"] = True
    meta["unlinted"] = unlinted_paths(stderr)
    if meta["unlinted"]:
        meta["error"] = unlinted_error(meta["unlinted"])

    seen = {(f["rule"], f["file"], f["line"], f["column"]) for f in project_findings}
    added: list[dict] = []
    duplicates = 0
    dismissed: dict[str, int] = {}
    trees: dict = {}
    lines: dict = {}
    for item in raw:
        finding = normalize_ruff_finding(item, project_root)
        if (
            finding["rule"],
            finding["file"],
            finding["line"],
            finding["column"],
        ) in seen:
            duplicates += 1
            continue
        filename = item.get("filename")
        path = Path(filename) if filename else None
        reason = dismissal_reason(
            {
                "code": finding["rule"],
                "line": finding["line"],
                "column": finding["column"],
            },
            path,
            trees,
            lines,
        )
        if reason:
            dismissed[reason] = dismissed.get(reason, 0) + 1
            continue
        finding["selection"] = "toolkit"
        added.append(_flag_known_tool_bugs(finding, filename, closures))

    by_category: dict[str, int] = {}
    for finding in added:
        by_category[finding["category"]] = by_category.get(finding["category"], 0) + 1
    meta.update(
        {
            "rules_applied": _extract_rules_applied(raw),
            "total_findings": len(added),
            "findings_capped": len(added) > args.max_findings,
            "duplicates_of_project_findings": duplicates,
            # Mechanical dismissals, by reason (see run_lint_rules.DISMISSAL_REASONS):
            # counted, not silently dropped.
            "dismissed": dismissed,
            "by_category": dict(sorted(by_category.items())),
        }
    )
    return added[: args.max_findings], meta


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

    # mypy looks for its config in the WORKING directory (walking up to the
    # repository root), not next to the files it checks. Run from an agent's
    # private temp directory, a project's `[tool.mypy]` was silently ignored
    # while the output still said `config_source: "project"`. So name the
    # config file explicitly, and disable discovery (`--config-file=`) for the
    # curated run so a stray config in the caller's cwd cannot leak in either.
    config_file = find_config_file("mypy", project_root)
    if args.ignore_config or config_file is None:
        cmd.extend(["--ignore-missing-imports", "--no-error-summary"])
        if args.mypy_strict:
            cmd.append("--strict")
        cmd.append("--config-file=")
        config_source = "curated"
        config_used = None
    else:
        config_used = str(project_root / config_file)
        cmd.extend(["--config-file", config_used])
        config_source = "project"

    # mypy's default cache is `.mypy_cache/` in the working directory, which is
    # usually the reviewed project. Give it a private cache and remove it
    # afterwards, so the review leaves nothing behind. A real directory rather
    # than `/dev/null`: the run stays an ordinary incremental run.
    cache_dir = tempfile.mkdtemp(prefix="crt-mypy-cache-")
    cmd.extend(["--cache-dir", cache_dir])
    cmd.append(str(scan_root))

    try:
        # cwd: relative paths in the config (`mypy_path`, `files`) resolve
        # against the working directory, as when the project runs mypy itself.
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=_TOOL_TIMEOUTS["mypy"], cwd=str(project_root),
        )
    finally:
        shutil.rmtree(cache_dir, ignore_errors=True)

    # Parse everything, then cap: stopping the parse at max_findings made
    # `total_findings` equal the cap and `findings_capped` never true.
    findings = parse_mypy_output(result.stdout)

    normalized = [
        normalize_mypy_finding(f, project_root)
        for f in findings[:args.max_findings]
    ]

    return {
        "config_source": config_source,
        # The config file mypy was told to read (None for the curated run).
        "config_file": config_used,
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

    # Parse everything, then cap: stopping the parse at max_findings made
    # `total_findings` equal the cap and `findings_capped` never true.
    findings = parse_vulture_output(result.stdout, project_root)
    normalized = findings[:args.max_findings]

    by_confidence: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for finding in findings:
        key = str(finding["confidence"])
        by_confidence[key] = by_confidence.get(key, 0) + 1
        by_type[finding["type"]] = by_type.get(finding["type"], 0) + 1

    return {
        "min_confidence": min_confidence,
        "total_findings": len(findings),
        "findings_capped": len(findings) > args.max_findings,
        # Each finding keeps its own `confidence`. Vulture scores unused
        # functions, methods, classes and attributes at 60% -- it cannot see
        # dynamic dispatch -- so these buckets say how much of the output is
        # that verify-first tier, and how much is 90-100% (imports,
        # unreachable code, unused arguments).
        "by_confidence": dict(
            sorted(by_confidence.items(), key=lambda kv: -int(kv[0]))
        ),
        "by_type": dict(sorted(by_type.items())),
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
        if name == "coverage" and not info.get("available"):
            # Coverage is read from artifacts, not run: "available" means an
            # artifact exists. Whether coverage.py is installed is a separate
            # fact, and calling a missing artifact "not installed" sent
            # reviews looking for a missing package.
            skipped.append({
                "tool": name,
                "reason": "no coverage artifacts",
                "coverage_importable": info.get("coverage_importable"),
                "hint": (
                    "Run the test suite under coverage (e.g. `coverage run -m "
                    "pytest && coverage json`) to produce .coverage, "
                    "coverage.xml or coverage.json."
                ),
            })
        elif not info.get("available"):
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
    parser.add_argument(
        "--no-toolkit-ruff-pass", action="store_true", default=False,
        help=(
            "Skip the second, toolkit-selected ruff pass that fills the "
            "bug-risk/security/simplification/performance/deprecated feeds"
        ),
    )

    # Mypy options.
    parser.add_argument(
        "--mypy-strict", action="store_true", default=False,
        help="Run mypy in strict mode",
    )

    # Vulture options.
    parser.add_argument(
        # 60, not 80: vulture caps unused functions, methods, classes and
        # attributes at 60%, so at 80 every one of them is dropped: a review
        # then saw only `__exit__`-style parameters, and every real dead
        # method had to come from a manual re-run at 60.
        "--vulture-min-confidence", type=int, default=60,
        help=(
            "Minimum confidence threshold (default: 60, which keeps unused "
            "functions and methods; each finding reports its confidence)"
        ),
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
