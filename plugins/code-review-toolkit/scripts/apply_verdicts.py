#!/usr/bin/env python3
"""Fold reproduction verdicts and reclassification decisions into findings.json.

Reads the consolidated findings (kept untouched as the input) and the slices'
verdict fragments (see merge_fragments.py, whose checks run first: nothing is
written while any of them fails), then writes a new findings file in which:

- ``status`` follows the verdict: ``reproduced`` -> reproduced,
  ``not-reproduced`` -> refuted, ``no-mre-possible`` -> confirmed. The raw
  verdict is kept as ``repro_verdict``;
- ``classification`` takes the slice's ``proposed_classification`` unless the
  decisions file rejects it, and an orchestrator override wins over both.
  Every change records why in ``classification_decision``;
- ``corrections`` to ``title``, ``mechanism``, ``failure_scenario`` and
  ``locations`` are applied; other corrected keys are appended to ``notes``;
- ``repro`` (the reproducer files), ``repro_main``, ``repro_branches``,
  ``branch_status`` and ``repro_slice`` are filled from the verdict;
- the original of every changed field is kept under ``pre_repro``;
- new findings reported by the slices are appended.

Always apply onto the same untouched input (keep the consolidation output as,
say, ``findings.pre-repro.json``): the step is then repeatable after a
decision changes. ``--out`` may not be the input file.

Decisions file (optional):

    {"reject":   {"F-031": "upgrade argued from reading only"},
     "override": {"F-044": {"classification": "CONSIDER", "reason": "..."}},
     "notes":    {"F-038": "only observable once F-037 is fixed"}}

Usage:
    python apply_verdicts.py FINDINGS.json FRAGMENT [FRAGMENT ...] --out FILE
        [--decisions FILE] [--assignments FILE] [--root DIR] [--require-all]

Options:
    FRAGMENT            A fragment file or directory (as for merge_fragments.py),
                        or the file `merge_fragments.py --out` wrote
    --out FILE          The findings file to write (not the input)
    --decisions FILE    Rejected proposals, overrides and notes, as above
    --require-all       Fail when a finding has no verdict (default: list them
                        as `unverified` and leave them unchanged)
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from leak_scan import findings_list
from merge_fragments import (
    CLASSIFICATIONS,
    VERDICT_STATUS,
    file_paths,
    load_all,
    load_assignments,
    merge,
    read_json,
    verdict_of,
)
from scan_common import emit, exit_on_help

CORRECTABLE = ("title", "mechanism", "failure_scenario", "locations")


def _append_note(finding: dict, text: str) -> None:
    """Add *text* to ``notes``, keeping the field's type (string or list)."""
    notes = finding.get("notes")
    if isinstance(notes, list):
        notes.append(text)
    elif notes:
        finding["notes"] = f"{notes}\n{text}"
    else:
        finding["notes"] = text


def _set(finding: dict, pre: dict, key: str, value: Any) -> None:
    """Set a field, remembering its first original value in *pre*."""
    if finding.get(key) != value:
        pre.setdefault(key, finding.get(key))
        finding[key] = value


def apply(
    findings: list[dict],
    verdicts: list[dict],
    new_findings: list[dict],
    decisions: dict,
) -> dict:
    """Apply *verdicts* and *decisions* to *findings* in place; return a log."""
    reject = decisions.get("reject") or {}
    override = decisions.get("override") or {}
    notes = decisions.get("notes") or {}
    for nf in new_findings:
        nf = dict(nf)
        slice_name = nf.pop("_fragment", None)
        if slice_name:
            nf.setdefault("repro_slice", slice_name)
        findings.append(nf)
    by_id = {str(f.get("id")): f for f in findings}
    log: Counter = Counter()
    reclassified, rejected = [], []
    for v in verdicts:
        f = by_id[str(v["id"])]
        pre: dict[str, Any] = {}
        raw = verdict_of(v)
        _set(f, pre, "status", VERDICT_STATUS[raw])
        f["repro_verdict"] = raw
        prop = v.get("proposed_classification")
        cur = f.get("classification")
        if prop and prop != cur:
            if f["id"] in reject:
                log["proposal-rejected"] += 1
                f["classification_decision"] = (
                    f"kept {cur} (proposed {prop}): {reject[f['id']]}"
                )
                rejected.append({"id": f["id"], "kept": cur, "proposed": prop})
            else:
                log[f"{cur}->{prop}"] += 1
                f["classification_decision"] = (
                    f"{cur} -> {prop}: {v.get('classification_reason', '')}".rstrip(
                        ": "
                    )
                )
                reclassified.append(
                    {"id": f["id"], "from": cur, "to": prop, "by": "proposal"}
                )
                _set(f, pre, "classification", prop)
        corrections = v.get("corrections") or {}
        for key in CORRECTABLE:
            if corrections.get(key):
                _set(f, pre, key, corrections[key])
                log[f"corrected:{key}"] += 1
        extra = {k: c for k, c in corrections.items() if k not in CORRECTABLE}
        if extra:
            _append_note(
                f, f"[reproduction corrections] {json.dumps(extra, ensure_ascii=False)}"
            )
        files = file_paths(v)
        if files:
            f["repro"] = files
        if v.get("main") is not None:
            f["repro_main"] = v["main"]
        if v.get("branches"):
            f["repro_branches"] = v["branches"]
        if v.get("branch_status"):
            _set(f, pre, "branch_status", v["branch_status"])
        f["repro_slice"] = v.get("fragment")
        if v.get("notes"):
            f["repro_notes"] = v["notes"]
        if pre:
            f["pre_repro"] = {**pre, **(f.get("pre_repro") or {})}
    for fid, d in override.items():
        f = by_id[fid]
        new = d.get("classification")
        if new and new != f.get("classification"):
            f.setdefault("pre_repro", {}).setdefault(
                "classification", f.get("classification")
            )
            f["classification_decision"] = (
                f"{f.get('classification')} -> {new} (override): {d.get('reason', '')}"
            )
            reclassified.append(
                {
                    "id": fid,
                    "from": f.get("classification"),
                    "to": new,
                    "by": "override",
                }
            )
            f["classification"] = new
            log["override"] += 1
    for fid, text in notes.items():
        _append_note(by_id[fid], text)
    return {"changes": dict(log), "reclassified": reclassified, "rejected": rejected}


def check_decisions(decisions: Any, known: set[str]) -> list[str]:
    if not isinstance(decisions, dict):
        return ["decisions must be an object"]
    errors = []
    for section in ("reject", "override", "notes"):
        value = decisions.get(section) or {}
        if not isinstance(value, dict):
            errors.append(f"decisions.{section} must be an object")
            continue
        for fid in value:
            if fid not in known:
                errors.append(f"decisions.{section}: unknown id {fid}")
    for fid, d in (decisions.get("override") or {}).items():
        if not isinstance(d, dict) or d.get("classification") not in CLASSIFICATIONS:
            errors.append(
                f"decisions.override.{fid}: needs a classification in {sorted(CLASSIFICATIONS)}"
            )
    return errors


def _parse(argv: list[str]) -> tuple[list[str], dict[str, str]]:
    positional: list[str] = []
    opts: dict[str, str] = {}
    i = 0
    while i < len(argv):
        if argv[i] == "--require-all":
            opts[argv[i]] = "1"
            i += 1
        elif argv[i].startswith("--"):
            if i + 1 >= len(argv):
                raise ValueError(f"{argv[i]} needs a value")
            opts[argv[i]] = argv[i + 1]
            i += 2
        else:
            positional.append(argv[i])
            i += 1
    return positional, opts


def run(argv: list[str]) -> tuple[dict, int]:
    """The CLI's work: ``(report, exit status)``."""
    positional, opts = _parse(argv)
    if len(positional) < 2 or "--out" not in opts:
        raise ValueError(
            "usage: apply_verdicts.py FINDINGS.json FRAGMENT [...] --out FILE"
        )
    src_path, out_path = Path(positional[0]), Path(opts["--out"])
    if out_path.exists() and out_path.resolve() == src_path.resolve():
        raise ValueError(
            "--out must not be the input: always apply onto the untouched consolidation output"
        )
    src = read_json(src_path)
    findings = findings_list(src)
    fragments = load_all(positional[1:])
    assignments = (
        load_assignments(Path(opts["--assignments"]))
        if "--assignments" in opts
        else None
    )
    root = Path(opts.get("--root") or src_path.resolve().parent)
    report = merge(findings, fragments, assignments, root)
    merged = report.pop("merged")
    decisions = read_json(Path(opts["--decisions"])) if "--decisions" in opts else {}
    known = {str(f.get("id")) for f in findings} | {
        str(n["id"]) for n in merged["new_findings"]
    }
    errors = [
        f"{p['kind']}: {p['id']} ({p['fragment']}) {p['detail']}".rstrip()
        for p in report["problems"]
    ]
    errors += check_decisions(decisions, known)
    unverified = report["coverage"]["uncovered"]
    if "--require-all" in opts and unverified:
        errors.append(f"{len(unverified)} findings have no verdict (--require-all)")
    if errors:
        return {
            "error": "not applied: fix these first",
            "problems": errors,
            "written": False,
        }, 1
    log = apply(findings, merged["verdicts"], merged["new_findings"], decisions)
    out = {**src, "findings": findings} if isinstance(src, dict) else findings
    out_path.write_text(
        json.dumps(out, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    result = {
        "findings": len(findings),
        "verdicts_applied": len(merged["verdicts"]),
        "new_findings": len(merged["new_findings"]),
        "unverified": unverified,
        **log,
        "classification": dict(Counter(f.get("classification") for f in findings)),
        "status": dict(Counter(f.get("status") for f in findings)),
        "fix_by_status": dict(
            Counter(
                f.get("status") for f in findings if f.get("classification") == "FIX"
            )
        ),
        "warnings": report["warnings"],
        "out": str(out_path),
        "written": True,
    }
    return result, 0


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    exit_on_help(argv, __doc__)
    try:
        result, code = run(argv)
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
        AttributeError,
    ) as exc:
        emit({"error": str(exc)})
        sys.exit(2)
    emit(result)
    sys.exit(code)


if __name__ == "__main__":
    main()
