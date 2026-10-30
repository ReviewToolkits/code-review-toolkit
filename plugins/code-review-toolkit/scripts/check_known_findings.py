#!/usr/bin/env python3
"""Cross-reference a catalog of known findings against a fresh scan.

Answers one question: **of the findings we already know about, which are still
in this tree?** It is static and drift-tolerant -- it runs the scanner, not the
repros.

Keyed on ``(file, shape, qualname)``, never on line numbers. Python's `ast`
gives stable qualified names, so a finding whose line moved is still `present`
rather than a spurious `absent` plus a spurious new finding. This is the whole
reason the sibling C toolkit needs a `line_drifted` verdict and this one does
not.

**The caveat that must survive into every report:** an `absent` verdict is NOT
proof of a fix. Of the 90 catalogued shapes, **32 are `agent-only`** -- they are
confirmed by cross-artifact judgement, not by a scanner check, so a fresh scan
cannot see them at all and will report every one of them `absent`. Those are
emitted as `not_scannable` and are excluded from the fix count, but the same
caution applies to any shape whose instance carries no local token.

Catalog format is the findings repo's `known_findings.tsv`:

    # id	location	shape	severity	status
    CRF-COVPY-0001	sqldata.py:390	fix-not-propagated-to-sibling-path	FIX	reproduced

``location`` may be ``file.py:12``, ``file.py:12-18`` or ``file.py:12,20``.

Usage:
    python check_known_findings.py [scope] --catalog PATH [--max-files N]
"""

from __future__ import annotations

import argparse
import ast
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import scan_python_pitfalls  # noqa: E402
from scan_common import emit, find_project_root, load_data  # noqa: E402

# Verdicts, strongest regression signal first.
_ORDER = (
    "present",
    "present_elsewhere",
    # The scanner still finds the candidate, but a differential now settles it
    # (`scan_python_pitfalls` lists it under `suppressed`). The code is still
    # there, so this is never evidence of a fix.
    "suppressed",
    "absent_in_qualname",
    "absent",
    "out_of_scope",
    "file_missing",
    "not_scannable",
)


def _line_numbers(spec: str) -> list[int]:
    """`12-18` -> [12..18]; `12` -> [12]; anything else -> []."""
    spec = spec.strip()
    if "-" in spec:
        lo, _, hi = spec.partition("-")
        if lo.strip().isdigit() and hi.strip().isdigit():
            return list(range(int(lo), int(hi) + 1))
        return []
    return [int(spec)] if spec.isdigit() else []


def parse_locations(location: str) -> list[tuple[str, list[int]]]:
    """Parse a catalog `location` into (file, lines) pairs.

    Real catalog rows use four shapes, and getting any of them wrong silently
    turns a live finding into `file_missing`:

        sqldata.py:390
        sqldata.py:744-884
        autocomplete.py:117,134
        patch.py:56-57, :74-75                       <- continuation segment
        tests/a.py:256-259, tests/b.py:290           <- two different files
    """
    out: list[tuple[str, list[int]]] = []
    current: str | None = None
    for segment in location.split(","):
        segment = segment.strip()
        if not segment:
            continue
        if ":" in segment:
            path, _, spec = segment.rpartition(":")
            path = path.strip()
            if path:
                current = path
            lines = _line_numbers(spec)
        else:
            # A bare `134` continues the previous file; a bare name is a file.
            lines = _line_numbers(segment)
            if not lines:
                current = segment
        if current is None:
            continue
        for existing in out:
            if existing[0] == current:
                existing[1].extend(lines)
                break
        else:
            out.append((current, list(lines)))
    return out


def qualname_index(tree: ast.AST) -> list[tuple[int, int, str]]:
    """(start, end, qualname) for every def/class, innermost last."""
    out: list[tuple[int, int, str]] = []

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}.{child.name}" if prefix else child.name
                end = getattr(child, "end_lineno", None) or child.lineno
                out.append((child.lineno, end, name))
                walk(child, name)
            else:
                walk(child, prefix)

    walk(tree, "")
    return out


def qualname_at(index: list[tuple[int, int, str]], line: int) -> str:
    """The innermost qualname containing *line*, or "" at module level."""
    best = ""
    best_span = None
    for start, end, name in index:
        if start <= line <= end:
            span = end - start
            if best_span is None or span <= best_span:
                best, best_span = name, span
    return best


def load_catalog(path: Path) -> list[dict]:
    rows: list[dict] = []
    for row in csv.reader(
        path.read_text(encoding="utf-8").splitlines(), delimiter="\t"
    ):
        if not row or row[0].startswith("#") or len(row) < 3:
            continue
        rows.append(
            {
                "id": row[0],
                "location": row[1],
                "sites": parse_locations(row[1]),
                "shape": row[2],
                "severity": row[3] if len(row) > 3 else "",
                "status": row[4] if len(row) > 4 else "",
            }
        )
    return rows


def _scannable_shapes() -> set[str]:
    """Shapes a scanner check can actually produce."""
    try:
        catalog = load_data("python_bug_shapes.json")
    except Exception:  # noqa: BLE001 -- data file is optional at runtime
        return set(scan_python_pitfalls._CHECKS)
    return {
        s["id"]
        for s in catalog.get("shapes", [])
        if s.get("detectability") == "implemented"
    } | set(scan_python_pitfalls._CHECKS)


def analyze(target: str, *, max_files: int = 0, catalog: str | None = None) -> dict:
    """Cross-reference *catalog* against a fresh scan of *target*."""
    scan_root = Path(target).resolve()
    project_root = find_project_root(scan_root)
    if not catalog:
        return {"error": "--catalog is required"}
    catalog_path = Path(catalog).expanduser()
    if not catalog_path.is_file():
        return {"error": f"catalog not found: {catalog_path}"}

    entries = load_catalog(catalog_path)
    scannable = _scannable_shapes()

    scan = scan_python_pitfalls.analyze(str(scan_root), max_files=max_files)
    # Index the fresh findings by (basename, shape) -- the catalog records paths
    # relative to the reviewed package, the scan relative to the project root.
    fresh: dict[tuple[str, str], list[dict]] = {}
    for f in scan["findings"]:
        fresh.setdefault((Path(f["file"]).name, f["shape"]), []).append(f)
    settled: dict[tuple[str, str], list[dict]] = {}
    for f in scan.get("suppressed") or []:
        settled.setdefault((Path(f["file"]).name, f["shape"]), []).append(f)

    qual_cache: dict[Path, list[tuple[int, int, str]]] = {}

    def resolve(rel: str) -> Path | None:
        for base in (scan_root, project_root, scan_root.parent):
            path = base / rel
            if path.is_file():
                return path
        return None

    def index_for(path: Path) -> list[tuple[int, int, str]]:
        if path not in qual_cache:
            try:
                qual_cache[path] = qualname_index(
                    ast.parse(path.read_text(encoding="utf-8", errors="replace"))
                )
            except (OSError, SyntaxError, ValueError):
                qual_cache[path] = []
        return qual_cache[path]

    def verdict_for(shape: str, rel: str, lines: list[int]) -> dict:
        """One (file, lines) site of a catalog entry."""
        path = resolve(rel)
        if path is None:
            return {"verdict": "file_missing", "qualname": None, "matched_at": None}
        # The file exists but sits outside what was scanned -- usually a catalog
        # entry in `tests/` checked against a scope of the package only. Saying
        # `absent` there would claim we looked and found nothing; we did not look.
        if not path.is_relative_to(scan_root):
            return {
                "verdict": "out_of_scope",
                "qualname": None,
                "matched_at": None,
                "note": (
                    f"{rel} exists but is outside the scanned scope "
                    f"({scan_root}); widen the scope to check it"
                ),
            }
        index = index_for(path)
        qualname = qualname_at(index, lines[0]) if lines else None
        hits = fresh.get((Path(rel).name, shape), [])
        muted = [
            m
            for m in settled.get((Path(rel).name, shape), [])
            if qualname is None or qualname_at(index, m["line"]) == qualname
        ]

        def suppressed() -> dict:
            hit = muted[0]
            return {
                "verdict": "suppressed",
                "qualname": qualname,
                "matched_at": f"{hit['file']}:{hit['line']}",
                "note": (
                    f"the scanner still finds this candidate, but suppresses it "
                    f"({hit.get('reason')}); the code is unchanged, so this is "
                    "not evidence of a fix -- check whether the differential is right"
                ),
            }

        if not hits:
            if muted:
                return suppressed()
            return {"verdict": "absent", "qualname": qualname, "matched_at": None}
        # Same qualname => same finding, whatever the line did.
        hit_quals = {qualname_at(index, h["line"]): h for h in hits}
        if qualname is not None and qualname in hit_quals:
            hit = hit_quals[qualname]
            return {
                "verdict": "present",
                "qualname": qualname,
                "matched_at": f"{hit['file']}:{hit['line']}",
            }
        if muted:
            return suppressed()
        if qualname is None:
            # The catalog recorded no line, so any hit in the file is a match.
            return {
                "verdict": "present",
                "qualname": None,
                "matched_at": f"{hits[0]['file']}:{hits[0]['line']}",
            }
        return {
            "verdict": "absent_in_qualname",
            "qualname": qualname,
            "matched_at": f"{hits[0]['file']}:{hits[0]['line']}",
            "note": (f"the file still has {shape} findings, but none in {qualname!r}"),
        }

    results: list[dict] = []
    for entry in entries:
        record = {
            "id": entry["id"],
            "location": entry["location"],
            "shape": entry["shape"],
            "severity": entry["severity"],
            "catalog_status": entry["status"],
            "qualname": None,
            "verdict": "",
            "matched_at": None,
        }

        if entry["shape"] not in scannable:
            # An agent-only shape. A scan can never see it, so `absent` would be
            # a lie. This is 32 of the 90 catalogued shapes.
            record["verdict"] = "not_scannable"
            record["note"] = (
                "shape is agent-only; a fresh scan cannot see it and its absence "
                "here is not evidence of a fix"
            )
            results.append(record)
            continue

        if not entry["sites"]:
            record["verdict"] = "file_missing"
            record["note"] = "could not parse a file path out of the location"
            results.append(record)
            continue

        # A multi-site finding takes its STRONGEST verdict: one site still
        # present means the finding is still present.
        per_site = [
            verdict_for(entry["shape"], rel, lines) for rel, lines in entry["sites"]
        ]
        best = min(per_site, key=lambda s: _ORDER.index(s["verdict"]))
        record.update(best)
        if len(per_site) > 1:
            record["sites"] = [
                {"file": rel, **s} for (rel, _), s in zip(entry["sites"], per_site)
            ]
        results.append(record)

    counts = {v: sum(1 for r in results if r["verdict"] == v) for v in _ORDER}
    still_present = [
        r for r in results if r["verdict"] in ("present", "present_elsewhere")
    ]

    return {
        "project_root": str(project_root),
        "scan_root": str(scan_root),
        "catalog": str(catalog_path),
        "catalog_entries": len(entries),
        "files_analyzed": scan.get("files_analyzed"),
        "summary": {
            **counts,
            "still_present": len(still_present),
            # Deliberately NOT called "fixed". See the note below.
            # Deliberately excludes out_of_scope and not_scannable: neither was
            # looked at, so neither is evidence of anything.
            "possibly_fixed": counts["absent"] + counts["absent_in_qualname"],
            # `suppressed` (in the counts above) is in neither: still in the
            # code, but no longer reported.
            "not_checked": counts["out_of_scope"] + counts["not_scannable"],
        },
        "notes": [
            "An `absent` verdict is NOT proof of a fix. Read the file before "
            "concluding anything is fixed.",
            f"{counts['not_scannable']} catalog entries name an agent-only shape. "
            "A fresh scan cannot see those at all; they are excluded from the "
            "counts above rather than reported as absent.",
            "Keyed on (file, shape, qualname), never on line numbers, so a "
            "finding whose line moved is still `present`.",
            (
                f"{counts['suppressed']} catalog entries are `suppressed`: the "
                "scanner still finds them but a differential now settles them. "
                "They are neither still-present nor possibly fixed; check "
                "whether the suppression is right."
            ),
        ],
        "results": results,
        "findings": still_present,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", nargs="?", default=".")
    parser.add_argument("--catalog", required=False)
    parser.add_argument("--max-files", type=int, default=0)
    args = parser.parse_args()
    try:
        result = analyze(args.target, max_files=args.max_files, catalog=args.catalog)
        emit(result)
        sys.exit(1 if "error" in result else 0)
    except Exception as exc:  # noqa: BLE001 -- top-level guard
        emit({"error": str(exc), "type": type(exc).__name__})
        sys.exit(1)


if __name__ == "__main__":
    main()
