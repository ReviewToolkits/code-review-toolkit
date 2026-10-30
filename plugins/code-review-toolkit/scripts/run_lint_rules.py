#!/usr/bin/env python3
"""Run a pinned, explicitly-tiered ruff selection and report defect-grade findings.

This is NOT a linter pass. It is a *defect* pass that happens to use ruff as the
engine: the tiers below select the rules that mean "this code is wrong", and
deliberately exclude the ~95% of ruff that means "this code is unfashionable".
Measured over idlelib / `_pyrepl` / coverage.py, the previous untiered selection
(`F,B,SIM,S,RET,PIE,UP,PERF`) produced 610 / 124 / 259 findings of which
65% / 73% / 92% were style-grade, while missing a quarter of the tier-1-grade
defects because it excluded `PL`, `RUF`, `DTZ` and `ASYNC` outright.

Design constraints, every one of them measured (see docs/decision-log.md D-10):

* **Pin the version.** ruff's default rule set went from 59 to 413 rules in
  0.16.0, and it is not a superset -- 18 rules were dropped. A ruff bump is a
  calibration event, so the version is recorded in every envelope and a mismatch
  against ``PINNED_RUFF_VERSION`` is reported rather than ignored.
* **Always an explicit ``--select``, never ``--extend-select``**, which composes
  with a default that just changed sevenfold.
* **JSON output only.** Concise output changed shape between versions *and*
  between preview modes, and 0.16.0's preview mode omits the rule code entirely.
* **Capture stderr.** Two staleness classes warn there and nowhere else: a
  remapped code (``'PGH001' has been remapped to 'S307'``) and a preview-gated
  selection having no effect.
* **No ``--preview``.** It mutates already-stable rules and carries no
  deprecation policy. ``--preview-pass`` runs the four preview-gated codes
  separately and labels their findings.
* **``rule_validation`` inspects ``status``, not membership.** Verified trap:
  removed rules (``RUF076``, ``UP038``, ``ANN101``) are still listed by
  ``ruff rule --all``, so a membership test passes for a deleted rule.

Two fields carry the design:

``shape_id``
    Non-null when the rule overlaps a catalogued bug shape. A finding with a
    shape_id should be MERGED with the scanner's finding at raised confidence,
    never double-reported.
``has_suppression_comment``
    Measured to be the strongest dismissal signal. Tier-1 precision is 50-70%,
    not 100%, and an existing suppression naming the rule class is usually why.

Usage:
    python run_lint_rules.py [path] [--tier 1|2|3] [--ruff-bin PATH]
                             [--preview-pass] [--max-findings N]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scan_common import emit, find_project_root, relative_to_root  # noqa: E402

# A ruff bump is a calibration event. Bump this WITH a fixture-corpus re-run.
PINNED_RUFF_VERSION = "0.16.0"

_RUFF_TIMEOUT = 300

# ---------------------------------------------------------------------------
# The tiers. These were measured once and then lost; they live here now so a
# future run reproduces rather than re-derives them.
# ---------------------------------------------------------------------------

# TIER 1 -- the code is wrong. A hit here is a candidate FIX finding.
TIER_1 = (
    # pyflakes: genuine errors
    "F402 F501 F502 F503 F504 F506 F507 F508 F509 F521 F522 F523 F524 F525 "
    "F601 F602 F621 F622 F631 F632 F633 F634 F701 F702 F811 F821 F823 F901 "
    # bugbear: the correctness half. B007 (unused loop variable) is deliberately
    # NOT here -- it is usually intentional and was 11 of 94 on idlelib.
    "B002 B003 B004 B005 B006 B008 B012 B015 B016 B017 B018 B019 B020 "
    "B023 B025 B026 B029 B030 B031 B032 B033 B035 B905 "
    # pylint errors, and the warnings that mean a defect
    "PLE0100 PLE0101 PLE0116 PLE0117 PLE0118 PLE0237 PLE0241 PLE0302 PLE0303 "
    "PLE0304 PLE0305 PLE0307 PLE0308 PLE0309 PLE0604 PLE0605 PLE0643 PLE0704 "
    "PLE1132 PLE1142 PLE1205 PLE1206 PLE1300 PLE1307 PLE1310 PLE1519 PLE1520 "
    "PLE1700 PLE2502 PLE2510 PLE2512 PLE2513 PLE2514 PLE2515 "
    "PLW0127 PLW0129 PLW0131 PLW0177 PLW0211 PLW0245 PLW0602 PLW0711 "
    "PLW1501 PLW1508 PLW1509 PLW1510 PLW1641 PLW2101 PLW3301 "
    # ruff's own correctness rules. RUF005/RUF041/RUF051 are style and live in
    # tier 2; RUF055 and PLW1514 are preview-gated and live in PREVIEW_ONLY.
    "RUF006 RUF008 RUF009 RUF016 RUF017 RUF020 RUF024 RUF026 RUF032 "
    "RUF033 RUF034 RUF036 RUF049 "
    # timezone-naive datetime: silently wrong across DST and locales
    "DTZ001 DTZ002 DTZ003 DTZ004 DTZ005 DTZ006 DTZ007 DTZ011 DTZ012 "
    # async correctness -- UNVALIDATED until an async corpus is run (Phase 5.2)
    "ASYNC100 ASYNC105 ASYNC109 ASYNC110 ASYNC115 ASYNC116 ASYNC210 "
    "ASYNC220 ASYNC221 ASYNC222 ASYNC230 ASYNC251 "
    # security rules that indicate a real vulnerability, not a policy preference
    "S301 S302 S307 S312 S317 S319 S324 S506 S508 S509 S602 S608 S609 "
    # comparison rules dropped from ruff's 0.16.0 default but still real defects.
    # E713/E714 (`not x in y`) are spelling, not semantics -- they are tier 2.
    "E711 E712 E721 E722"
).split()

# TIER 2 -- likely a defect, or a strong smell worth a look. Agent-triaged.
TIER_2 = (
    "B007 B009 B010 B011 B013 B014 B021 B022 B024 B027 B028 B034 B039 B904 "
    "BLE001 E713 E714 RUF005 RUF041 RUF051 "
    # PLC2701 / PLR0202 / PLR0203 are preview-gated and were dropped here after
    # `rule_validation` reported them having no effect.
    "PLC0105 PLC0131 PLC0132 PLC0205 PLC0208 PLC0414 PLC0415 PLC2401 "
    "PLC3002 PLR0124 PLR0133 PLR0206 PLR0402 PLR1704 PLR1711 "
    "PLR1714 PLR1722 PLR2004 PLR5501 PLW0120 PLW0603 PLW0604 PLW2901 "
    "RUF001 RUF002 RUF003 RUF010 RUF012 RUF013 RUF015 RUF018 RUF019 RUF046 "
    "S101 S102 S103 S104 S105 S106 S107 S108 S110 S112 S113 S201 S202 "
    "PGH003 PGH004 PGH005 "
    "C901 PLR0911 PLR0912 PLR0913 PLR0915 "
    "F401 F403 F405 F841"
).split()

# Preview-gated codes worth a separate, labelled pass. PLW1514 (unspecified
# encoding -- a locale-dependent decode, a real defect class) and RUF055 were
# found here by `rule_validation` on its first run: both had been tiered as
# stable and silently had no effect, which is exactly the failure D-10 predicted.
PREVIEW_ONLY = ("B909", "PLE1141", "RUF027", "RUF069", "PLW1514", "RUF055")

# Rules that overlap a catalogued bug shape. A hit here MERGES with the
# scanner's finding at raised confidence -- it must never be double-reported.
# Measured agreement, from the pre-plan survey: exact on B006/B012/PLW1641/E722;
# BLE001 has ~30% better recall than the scanner; B023 finds ZERO across all
# three corpora where the scanner finds a real one, so the scanner check must
# never be dropped in favour of ruff.
RULE_TO_SHAPE = {
    "B006": "mutable-default-argument",
    "B008": "mutable-default-argument",
    "B023": "late-binding-closure-in-loop",
    "B012": "return-or-break-in-finally",
    "PLW1641": "eq-without-hash",
    "BLE001": "except-exception-too-broad",
    "E722": "bare-except-swallows-control-flow",
    "B904": "raise-without-from-in-except",
    # Found by the first novel-shape harvest: B019 was being reported as novel
    # while `lru-cache-on-method` had been catalogued since the first wave.
    "B019": "lru-cache-on-method",
    "B017": "test-cannot-fail",
    "RUF006": "asyncio-fire-and-forget-task",
    "RUF008": "class-level-mutable-attribute",
    "RUF009": "class-level-mutable-attribute",
    "RUF012": "class-level-mutable-attribute",
    "PLW1508": "unvalidated-numeric-from-environment",
}

_SUPPRESSION = re.compile(
    r"#\s*(noqa|type:\s*ignore|pylint:\s*disable|ruff:\s*noqa|nosec|pragma)",
    re.IGNORECASE,
)


def _tier_codes(tier: int) -> list[str]:
    if tier == 1:
        return [str(c) for c in TIER_1]
    if tier == 2:
        return [str(c) for c in (*TIER_1, *TIER_2)]
    raise ValueError(f"unknown tier: {tier}")


def ruff_version(ruff_bin: str) -> str | None:
    """The version string of *ruff_bin*, or None if it cannot be run."""
    try:
        out = subprocess.run(
            [ruff_bin, "--version"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return None
    parts = out.stdout.split()
    return parts[1] if len(parts) > 1 else None


def validate_rules(ruff_bin: str, codes: list[str]) -> dict:
    """Classify every selected code against ruff's own rule metadata.

    Reads ``status``, NOT membership: a removed rule is still listed by
    ``ruff rule --all``, so a membership test passes for a deleted rule.
    """
    result: dict[str, list[str]] = {
        "ok": [],
        "unknown": [],
        "removed": [],
        "preview_gated": [],
    }
    try:
        out = subprocess.run(
            [ruff_bin, "rule", "--all", "--output-format", "json"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        known = {r["code"]: r for r in json.loads(out.stdout)}
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, KeyError):
        return {**result, "error": "could not read ruff rule metadata"}

    for code in codes:
        rule = known.get(code)
        if rule is None:
            result["unknown"].append(code)
            continue
        status = rule.get("status")
        state = next(iter(status)) if isinstance(status, dict) and status else "Unknown"
        if state == "Removed":
            result["removed"].append(code)
        elif state == "Preview":
            result["preview_gated"].append(code)
        else:
            result["ok"].append(code)
    return result


def _source_line(path: Path, lineno: int, cache: dict[Path, list[str]]) -> str:
    if path not in cache:
        try:
            cache[path] = path.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
        except OSError:
            cache[path] = []
    lines = cache[path]
    return lines[lineno - 1] if 0 < lineno <= len(lines) else ""


def normalize(
    raw: dict, project_root: Path, tier: int, cache: dict, preview: bool = False
) -> dict:
    """Normalize one ruff JSON finding.

    0.16.0's JSON notes that ``filename`` and ``location`` "may now be null",
    so both are read defensively.
    """
    code = raw.get("code") or ""
    filename = raw.get("filename") or ""
    location = raw.get("location") or {}
    line = location.get("row", 0) or 0
    path = Path(filename) if filename else None
    text = _source_line(path, line, cache) if path else ""
    return {
        "code": code,
        "rule": raw.get("name") or "",
        "file": relative_to_root(path, project_root) if path else "",
        "line": line,
        "column": location.get("column", 0) or 0,
        "message": (raw.get("message") or "").strip(),
        "tier": tier,
        "preview": preview,
        # Non-null => merge with the scanner's finding, do not double-report.
        "shape_id": RULE_TO_SHAPE.get(code),
        # The strongest measured dismissal signal.
        "has_suppression_comment": bool(_SUPPRESSION.search(text)),
    }


def detect_target_version(project_root: Path) -> str:
    """The Python version to tell ruff to assume.

    `--isolated` is on by default so a project's own ruff config cannot silently
    change the selection -- but it also discards `requires-python`, and ruff then
    assumes its oldest supported version. Measured on asyncio: that produced
    **10 of 14 tier-1 findings as F821 false positives**, every one a builtin
    added in a later release (`ExceptionGroup`, `BaseExceptionGroup`). Isolation
    should control the RULE SET, not the language level.
    """
    pyproject = project_root / "pyproject.toml"
    if pyproject.is_file():
        try:
            text = pyproject.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        match = re.search(r'requires-python\s*=\s*["\']>=\s*3\.(\d+)', text)
        if match:
            return f"py3{match.group(1)}"
    # No declaration -- assume the interpreter running the review. For stdlib
    # targets this is the only correct answer, and it is a far better default
    # than ruff's floor.
    return f"py3{sys.version_info.minor}"


def _run_ruff(
    ruff_bin: str,
    codes: list[str],
    target: Path,
    preview: bool,
    target_version: str | None = None,
) -> tuple[list[dict], str, str | None]:
    """Run ruff over *target*; return (findings, stderr, error)."""
    cmd = [
        ruff_bin,
        "check",
        "--output-format",
        "json",
        # --isolated so results are comparable across projects: a project's own
        # ruff config would otherwise silently change the selection.
        "--isolated",
        # --no-cache: ruff otherwise writes `.ruff_cache/` into the project it
        # lints. A review must leave the reviewed checkout exactly as it found it.
        "--no-cache",
        "--select",
        ",".join(codes),
    ]
    if target_version:
        cmd.extend(["--target-version", target_version])
    if preview:
        cmd.append("--preview")
    cmd.append(str(target))
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=_RUFF_TIMEOUT)
    except subprocess.TimeoutExpired:
        return [], "", f"ruff timed out after {_RUFF_TIMEOUT}s"
    except OSError as exc:
        return [], "", f"could not run ruff: {exc}"
    try:
        findings = json.loads(out.stdout) if out.stdout.strip() else []
    except json.JSONDecodeError as exc:
        return [], out.stderr, f"ruff emitted unparseable JSON: {exc}"
    return findings, out.stderr, None


def analyze(
    target: str,
    *,
    max_files: int = 0,
    tier: int = 1,
    ruff_bin: str = "ruff",
    preview_pass: bool = False,
    max_findings: int = 2000,
) -> dict:
    """Run the tiered ruff selection over *target*."""
    scan_root = Path(target).resolve()
    project_root = find_project_root(scan_root)
    version = ruff_version(ruff_bin)

    if version is None:
        return {
            "project_root": str(project_root),
            "scan_root": str(scan_root),
            "error": f"ruff not runnable as {ruff_bin!r}",
            "ruff_version": None,
            "findings": [],
        }

    codes = _tier_codes(tier)
    validation = validate_rules(ruff_bin, codes)
    # Never send ruff a code it will reject; an unknown code aborts the whole run.
    runnable = [c for c in codes if c not in validation["unknown"]]

    target_version = detect_target_version(project_root)
    raw, stderr, error = _run_ruff(
        ruff_bin, runnable, scan_root, preview=False, target_version=target_version
    )
    cache: dict[Path, list[str]] = {}
    findings = [normalize(f, project_root, tier, cache) for f in raw[:max_findings]]

    preview_findings: list[dict] = []
    preview_stderr = ""
    if preview_pass:
        praw, preview_stderr, _ = _run_ruff(
            ruff_bin,
            list(PREVIEW_ONLY),
            scan_root,
            preview=True,
            target_version=target_version,
        )
        preview_findings = [
            normalize(f, project_root, tier, cache, preview=True)
            for f in praw[:max_findings]
        ]

    by_code: dict[str, int] = {}
    for f in findings + preview_findings:
        by_code[f["code"]] = by_code.get(f["code"], 0) + 1

    merged = [f for f in findings if f["shape_id"]]
    suppressed = [f for f in findings if f["has_suppression_comment"]]

    return {
        "project_root": str(project_root),
        "scan_root": str(scan_root),
        "ruff_version": version,
        "pinned_version": PINNED_RUFF_VERSION,
        # A mismatch does not fail the run, but it makes every count in this
        # envelope non-comparable with a calibrated one. Say so.
        "version_matches_pin": version == PINNED_RUFF_VERSION,
        "tier": tier,
        "target_version": target_version,
        "rules_selected": len(runnable),
        "rule_validation": validation,
        # Remapped and preview-gated codes warn on stderr and nowhere else.
        "stderr": (stderr + preview_stderr).strip(),
        "error": error,
        "summary": {
            "total": len(findings) + len(preview_findings),
            "preview": len(preview_findings),
            "by_code": dict(sorted(by_code.items(), key=lambda kv: -kv[1])),
            # Findings that overlap a catalogued shape: merge, do not re-report.
            "overlaps_a_shape": len(merged),
            # ~30 selected rules have no shape at all. A confirmed true positive
            # from one of those is a NEW CATALOG ENTRY -- the highest-value
            # output of this script, more than the findings themselves.
            "novel_shape_candidates": sorted(
                {f["code"] for f in findings if not f["shape_id"]}
            ),
            "has_suppression_comment": len(suppressed),
        },
        "findings_capped": len(raw) > max_findings,
        "findings": findings + preview_findings,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", nargs="?", default=".")
    parser.add_argument("--tier", type=int, default=1, choices=(1, 2))
    parser.add_argument("--ruff-bin", default="ruff")
    parser.add_argument("--preview-pass", action="store_true")
    parser.add_argument("--max-findings", type=int, default=2000)
    parser.add_argument("--max-files", type=int, default=0)
    args = parser.parse_args()
    try:
        result = analyze(
            args.target,
            max_files=args.max_files,
            tier=args.tier,
            ruff_bin=args.ruff_bin,
            preview_pass=args.preview_pass,
            max_findings=args.max_findings,
        )
        emit(result)
        sys.exit(1 if result.get("error") else 0)
    except Exception as exc:  # noqa: BLE001 -- top-level guard
        emit({"error": str(exc), "type": type(exc).__name__})
        sys.exit(1)


if __name__ == "__main__":
    main()
