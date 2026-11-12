#!/usr/bin/env python3
"""Merge per-slice reproduction verdicts and check them against findings.json.

A large Phase 3 splits the findings into slices, one reproduction agent per
slice, and each agent writes one fragment file of verdicts. This script reads
the fragments back and answers: is every finding covered exactly once, does
every verdict hold together, and what do the slices propose? It changes
nothing; `apply_verdicts.py` folds the result into findings.json.

A fragment is a JSON list of verdicts, or an object
``{"slice": NAME, "ids": [...], "verdicts": [...], "new_findings": [...]}``
(``slice`` defaults to the file name without a ``slice-`` prefix; ``ids``,
the findings assigned to the slice, is optional). One verdict:

    {"id": "F-012", "verdict": "reproduced | not-reproduced | no-mre-possible",
     "main": "BUG | OK | ERROR", "files": ["artifacts/repro/F-012.py"],
     "branches": {"origin/fix-x": "OK"}, "branch_status": "...",
     "proposed_classification": "CONSIDER", "classification_reason": "...",
     "corrections": {"locations": ["pkg/mod.py:40-44"]}, "notes": "..."}

``verdict`` may also be spelled ``status``, and the ladder names ``refuted``
and ``confirmed`` are accepted for ``not-reproduced`` and
``no-mre-possible``. ``main`` is the reproducer's one-line verdict on the
default branch. ``files`` are relative to ``--root``; a path that exists
only relative to the fragment's own directory is rewritten relative to
``--root`` (a ``path-normalized`` warning).

Problems (exit 1): an id not in findings.json, an id in two fragments, an id
outside its slice's assignment or missing from it, an unknown verdict or
``main``, ``reproduced`` with no files or ``main`` other than BUG,
``not-reproduced`` with ``main`` BUG, a missing reproducer file, an unknown
proposed classification, a new finding whose id is missing or taken.

Usage:
    python merge_fragments.py FINDINGS.json FRAGMENT [FRAGMENT ...]
        [--assignments FILE] [--root DIR] [--out FILE]

Options:
    FRAGMENT            A fragment file, or a directory: its slice-*.json files
    --assignments FILE  {"S01": ["F-001", ...]} or [{"slice": ..., "ids": [...]}]:
                        which slice owns which finding; slices without a
                        fragment yet are reported as pending, not as gaps
    --root DIR          Base for verdict `files` (default: FINDINGS.json's dir)
    --out FILE          Write the merged {"verdicts": [...], "new_findings": [...]}
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from leak_scan import findings_list
from scan_common import emit, exit_on_help

# Verdict spellings -> the status ladder of the reproduction convention.
VERDICT_STATUS = {
    "reproduced": "reproduced",
    "not-reproduced": "refuted",
    "refuted": "refuted",
    "no-mre-possible": "confirmed",
    "confirmed": "confirmed",
}
MAIN_VALUES = {"BUG", "OK", "ERROR"}
CLASSIFICATIONS = {"FIX", "CONSIDER", "POLICY", "ACCEPTABLE"}


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def fragment_files(paths: list[Path]) -> list[Path]:
    """Fragment files: each file as given, each directory's ``slice-*.json``.

    Only ``slice-*`` files are taken from a directory, so the assignment and
    decision files can live next to the fragments.
    """
    out: list[Path] = []
    for p in paths:
        if p.is_dir():
            out += sorted(q for q in p.glob("slice-*.json") if q.is_file())
        else:
            out.append(p)
    return out


def load_fragment(path: Path) -> dict:
    """``{name, path, verdicts, new_findings, ids}`` for one fragment file."""
    data = read_json(path)
    name = path.stem.removeprefix("slice-")
    if isinstance(data, list):
        return {
            "name": name,
            "path": str(path),
            "verdicts": data,
            "new_findings": [],
            "ids": None,
        }
    if not isinstance(data, dict):
        raise TypeError(f"{path}: expected a list or an object")
    ids = data.get("ids")
    return {
        "name": str(data.get("slice") or name),
        "path": str(path),
        "verdicts": list(data.get("verdicts") or []),
        "new_findings": list(data.get("new_findings") or []),
        "ids": [str(i) for i in ids] if isinstance(ids, list) else None,
    }


def load_assignments(path: Path) -> dict[str, list[str]]:
    data = read_json(path)
    if isinstance(data, dict):
        return {str(k): [str(i) for i in v] for k, v in data.items()}
    out: dict[str, list[str]] = {}
    for row in data:
        name = row.get("slice") or row.get("name")
        out[str(name)] = [str(i) for i in row.get("ids") or []]
    return out


def verdict_of(entry: dict) -> str | None:
    raw = entry.get("verdict", entry.get("status"))
    return str(raw) if raw is not None else None


def file_paths(entry: dict) -> list[str]:
    files = entry.get("files") or []
    if isinstance(files, str):
        files = [files]
    return [str(f) for f in files]


def merge(
    findings: list[dict],
    fragments: list[dict],
    assignments: dict[str, list[str]] | None = None,
    root: Path | None = None,
) -> dict:
    """Check *fragments* against *findings*; return the report and the merge."""
    problems: list[dict] = []
    warnings: list[dict] = []

    def problem(kind: str, fid: Any, frag: str, detail: str = "") -> None:
        problems.append({"kind": kind, "id": fid, "fragment": frag, "detail": detail})

    known = {str(f.get("id")) for f in findings if f.get("id") is not None}
    new_findings: list[dict] = []
    for frag in fragments:
        for nf in frag["new_findings"]:
            nid = nf.get("id") if isinstance(nf, dict) else None
            if not nid:
                problem("new-finding-without-id", None, frag["name"])
            elif str(nid) in known:
                problem("new-finding-id-taken", nid, frag["name"])
            else:
                known.add(str(nid))
                new_findings.append({"_fragment": frag["name"], **nf})

    owner: dict[str, str] = {}
    if assignments is None:
        given = {f["name"]: f["ids"] for f in fragments if f["ids"] is not None}
        assignments = given or None
    if assignments:
        for name, ids in assignments.items():
            for fid in ids:
                owner.setdefault(fid, name)
        # A new finding belongs to the slice that reported it.
        for nf in new_findings:
            owner.setdefault(str(nf["id"]), nf["_fragment"])

    merged: dict[str, dict] = {}
    for frag in fragments:
        name = frag["name"]
        for entry in frag["verdicts"]:
            if not isinstance(entry, dict) or not entry.get("id"):
                problem("verdict-without-id", None, name)
                continue
            fid = str(entry["id"])
            if fid not in known:
                problem("unknown-id", fid, name)
                continue
            if fid in merged:
                problem("duplicate", fid, name, f"also in {merged[fid]['fragment']}")
                continue
            if owner and owner.get(fid) != name:
                problem("not-assigned", fid, name, f"assigned to {owner.get(fid)}")
            merged[fid] = {"fragment": name, **entry}
            _check_entry(
                merged[fid], root, problem, warnings, Path(frag["path"]).parent
            )

    done = {f["name"] for f in fragments}
    pending = sorted(set(assignments or {}) - done)
    if assignments:
        for name in sorted(done & set(assignments)):
            for fid in assignments[name]:
                if fid not in merged:
                    problem("missing-from-fragment", fid, name)

    all_ids = [str(f.get("id")) for f in findings if f.get("id") is not None]
    all_ids += [str(nf["id"]) for nf in new_findings]
    uncovered = [i for i in all_ids if i not in merged]
    by_id = {str(f.get("id")): f for f in findings}
    by_id.update({str(nf["id"]): nf for nf in new_findings})
    reclass = []
    for fid, e in merged.items():
        prop = e.get("proposed_classification")
        cur = by_id.get(fid, {}).get("classification")
        if prop and prop != cur:
            reclass.append(
                {
                    "id": fid,
                    "from": cur,
                    "to": prop,
                    "reason": e.get("classification_reason", ""),
                }
            )
    return {
        "findings_total": len(findings),
        "new_findings": len(new_findings),
        "fragments": sorted(done),
        "pending_slices": pending,
        "coverage": {
            "expected": len(all_ids),
            "covered": len(merged),
            "uncovered": uncovered,
            "uncovered_in_pending_slices": [
                i for i in uncovered if owner.get(i) in pending
            ],
            "unassigned": [i for i in all_ids if assignments and i not in owner],
        },
        "complete": not uncovered and not problems,
        "counts": {
            "verdict": dict(Counter(verdict_of(e) for e in merged.values())),
            "main": dict(Counter(str(e.get("main")) for e in merged.values())),
        },
        "reclassifications": reclass,
        "corrections": sorted(fid for fid, e in merged.items() if e.get("corrections")),
        "problems": problems,
        "warnings": warnings,
        "merged": {"verdicts": list(merged.values()), "new_findings": new_findings},
    }


def _check_entry(
    e: dict,
    root: Path | None,
    problem,
    warnings: list[dict],
    frag_dir: Path | None = None,
) -> None:
    fid, name = e["id"], e["fragment"]
    verdict = verdict_of(e)
    if verdict not in VERDICT_STATUS:
        problem("bad-verdict", fid, name, repr(verdict))
        return
    main = e.get("main")
    if main is not None and main not in MAIN_VALUES:
        problem("bad-main", fid, name, repr(main))
    files = file_paths(e)
    status = VERDICT_STATUS[verdict]
    if status == "reproduced":
        if not files:
            problem("reproduced-without-files", fid, name)
        if main is None:
            warnings.append(
                {"kind": "reproduced-without-main", "id": fid, "fragment": name}
            )
        elif main != "BUG":
            problem("verdict-main-mismatch", fid, name, f"reproduced but main={main}")
    elif status == "refuted" and main == "BUG":
        problem("verdict-main-mismatch", fid, name, "not-reproduced but main=BUG")
    if root is not None:
        fixed = []
        for f in files:
            if (root / f).is_file():
                fixed.append(f)
                continue
            # A slice that wrote a path relative to its own fragment: normalise it.
            alt = frag_dir / f if frag_dir is not None else None
            try:
                rel = (
                    alt.resolve().relative_to(root.resolve()).as_posix()
                    if alt and alt.is_file()
                    else None
                )
            except ValueError:
                rel = None
            if rel is None:
                problem("file-missing", fid, name, f)
                fixed.append(f)
            else:
                warnings.append(
                    {
                        "kind": "path-normalized",
                        "id": fid,
                        "fragment": name,
                        "detail": f"{f} -> {rel}",
                    }
                )
                fixed.append(rel)
        if files:
            e["files"] = fixed
    prop = e.get("proposed_classification")
    if prop is not None and prop not in CLASSIFICATIONS:
        problem("bad-classification", fid, name, repr(prop))
    corrections = e.get("corrections")
    if corrections is not None and not isinstance(corrections, dict):
        problem("bad-corrections", fid, name, "corrections must be an object")


def load_all(fragment_args: list[str]) -> list[dict]:
    files = fragment_files([Path(p) for p in fragment_args])
    if not files:
        raise ValueError("no fragment files found")
    return [load_fragment(p) for p in files]


def _parse(argv: list[str]) -> tuple[list[str], dict[str, str]]:
    positional: list[str] = []
    opts: dict[str, str] = {}
    i = 0
    while i < len(argv):
        if argv[i].startswith("--"):
            if i + 1 >= len(argv):
                raise ValueError(f"{argv[i]} needs a value")
            opts[argv[i]] = argv[i + 1]
            i += 2
        else:
            positional.append(argv[i])
            i += 1
    return positional, opts


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    exit_on_help(argv, __doc__)
    try:
        positional, opts = _parse(argv)
        if len(positional) < 2:
            raise ValueError(
                "usage: merge_fragments.py FINDINGS.json FRAGMENT [FRAGMENT ...]"
            )
        findings_path = Path(positional[0])
        findings = findings_list(read_json(findings_path))
        fragments = load_all(positional[1:])
        assignments = (
            load_assignments(Path(opts["--assignments"]))
            if "--assignments" in opts
            else None
        )
        root = Path(opts.get("--root") or findings_path.resolve().parent)
    except (
        OSError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
        AttributeError,
    ) as exc:
        emit({"error": str(exc)})
        sys.exit(2)
    result = merge(findings, fragments, assignments, root)
    merged = result.pop("merged")
    if "--out" in opts:
        Path(opts["--out"]).write_text(
            json.dumps(merged, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        result["out"] = opts["--out"]
    emit(result)
    sys.exit(1 if result["problems"] else 0)


if __name__ == "__main__":
    main()
