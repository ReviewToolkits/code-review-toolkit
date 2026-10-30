#!/usr/bin/env python3
"""Diff two report directories -- the false-positive regression gate.

A "report directory" is what a run leaves in ``reports/<target>_v<n>/``: one
``<script>.json`` per analysis script. This script compares two of them and
answers the only question a regression gate needs: **what is new, what is gone,
and what merely moved?**

Findings are keyed WITHOUT line numbers. A finding that shifted because lines
were inserted above it is the same finding, and a gate that calls it "gone" plus
"added" reports two regressions where there were none. Line numbers are still
carried, so a keyed match with a different line is reported as ``moved``.

Every list compared is registered explicitly in ``_FINDING_LISTS`` below rather
than sniffed, because a heuristic that silently stops recognising a list would
make a regression look like an improvement. Anything unregistered, unreadable, or
present in only one of the two runs is reported in ``notes`` -- the denominator is
part of the answer.

Usage:
    python diff_findings.py OLD_REPORT_DIR NEW_REPORT_DIR [--severity FIX]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scan_common import emit, exit_on_help  # noqa: E402

# Which top-level lists in each script's report are FINDINGS, and the fields that
# identify one. `line` is deliberately never part of a key -- see the module
# docstring. A list absent from here is inventory, not findings.
_FINDING_LISTS: dict[str, dict[str, tuple[str, ...]]] = {
    # `message` is deliberately NOT part of the key. It carries the finding's
    # explanatory detail, so improving the wording of a check re-splits every
    # finding it produces into one `gone` plus one `added` -- the same spurious
    # pair the line-number exclusion exists to prevent, arriving by another
    # route. Caught on the idlelib v2 -> v3 gate: a message dedup fix reported a
    # regression and a fix at the same file, line and shape.
    "scan_python_pitfalls": {
        "findings": ("file", "shape", "type"),
        # Candidates a differential settled: still in the code, not reported.
        # A finding that moves here from `findings` is `newly_suppressed`,
        # not gone -- see _RECLASSIFIED.
        "suppressed": ("file", "shape", "reason"),
    },
    "run_lint_rules": {
        "findings": ("file", "code"),
    },
    "check_typing": {
        "findings": ("file", "code"),
        "phantom_import_findings": ("file", "code"),
        "unreachable_findings": ("file", "code"),
        "stale_ignore_findings": ("file", "code"),
    },
    "check_known_findings": {
        "findings": ("id", "shape"),
    },
    "find_dead_symbols": {
        "unused_imports": ("file", "name", "module"),
        "unreferenced_symbols": ("file", "name", "type"),
        "referenced_only_from_tests": ("file", "name", "type"),
        "orphan_files": ("file",),
        "commented_code_blocks": ("file", "preview"),
    },
    "measure_complexity": {
        # qualified_name is `path/to/file.py::Class.method` -- file and symbol in one.
        "hotspots": ("qualified_name",),
    },
    "collect_debt": {
        "items": ("file", "category", "full_line"),
    },
    "count_types": {
        "any_usages": ("location", "function"),
        # Keyed by (file, name): one `build` per plugin module is not one
        # function. Falls back to `name` for reports from before `file`
        # existed -- see _FALLBACK_KEYS.
        "unannotated_public_functions": ("file", "name"),
    },
    "extract_test_invariants": {
        "invariants": ("file", "function"),
    },
}

# Lists whose items are not dicts and need their own key function.
_SEQUENCE_LISTS = {"analyze_imports": ("cycles",)}

# Fields carried into the diff output for a human to act on, when present.
_CARRY = (
    "code",
    "file",
    "line",
    "location",
    "qualified_name",
    "shape",
    "type",
    "severity",
    "confidence",
    "name",
    "message",
    "score",
    "category",
    "reason",
)

# A weaker key for a list whose items gained a field later: used (and noted)
# when either run has an item without it, so an old report still compares.
_FALLBACK_KEYS: dict[tuple[str, str], tuple[str, ...]] = {
    ("count_types", "unannotated_public_functions"): ("name",),
}

# Lists capped by the producing script: `<list>_capped` true in either run
# means items past the cap churn in and out without changing.
_CAPPED = {("count_types", "unannotated_public_functions")}

# (script, reported list, suppressed list, shared key): a finding that leaves
# the reported list and appears in the suppressed one was reclassified by a
# differential -- the code is unchanged -- so it is neither `gone` nor a new
# suppressed entry, but `newly_suppressed`.
_RECLASSIFIED = (("scan_python_pitfalls", "findings", "suppressed", ("file", "shape")),)


def _load_report(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Load one report JSON, returning ``(data, error)``."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "missing"
    except json.JSONDecodeError as exc:
        # Seen in the wild: a ResourceWarning captured into the JSON file made
        # reports/coveragepy_v1/analyze_history.json unparseable.
        return None, f"unparseable ({exc.msg} at line {exc.lineno})"
    except OSError as exc:
        return None, f"unreadable ({type(exc).__name__})"
    if not isinstance(data, dict):
        return None, f"top-level {type(data).__name__}, expected object"
    return data, None


def _key_of(item: dict, fields: tuple[str, ...]) -> tuple:
    """Identity of a finding: the registered fields, line number excluded."""
    return tuple(str(item.get(f, "")) for f in fields)


def _index(items: list, fields: tuple[str, ...] | None) -> dict[tuple, dict]:
    """Index a finding list by key, disambiguating genuine duplicates."""
    out: dict[tuple, dict] = {}
    seen: dict[tuple, int] = {}
    for item in items:
        if fields is None:
            # A sequence finding (an import cycle): the sequence IS the key.
            key = (json.dumps(item, sort_keys=True),)
            payload = {"cycle": item}
        elif isinstance(item, dict):
            key = _key_of(item, fields)
            payload = {k: item[k] for k in _CARRY if k in item}
        else:
            continue
        n = seen.get(key, 0)
        seen[key] = n + 1
        # Two findings identical in every keyed field are distinguished by
        # occurrence order, so a run with three of them and a run with two
        # reports one `gone` rather than nothing.
        out[key + (str(n),) if n else key] = payload
    return out


def _diff_list(old: list, new: list, fields: tuple[str, ...] | None) -> dict:
    """Compare one finding list between two runs."""
    old_ix, new_ix = _index(old, fields), _index(new, fields)
    old_keys, new_keys = set(old_ix), set(new_ix)

    added = [new_ix[k] for k in sorted(new_keys - old_keys, key=str)]
    gone = [old_ix[k] for k in sorted(old_keys - new_keys, key=str)]
    moved = []
    for key in sorted(old_keys & new_keys, key=str):
        before, after = old_ix[key].get("line"), new_ix[key].get("line")
        if before != after and before is not None and after is not None:
            moved.append({**new_ix[key], "line_was": before})
    return {
        "old_count": len(old),
        "new_count": len(new),
        "added": added,
        "gone": gone,
        "moved": moved,
        "unchanged": len(old_keys & new_keys) - len(moved),
    }


def _lacks_fields(items: list, fields: tuple[str, ...]) -> bool:
    return any(isinstance(i, dict) and any(f not in i for f in fields) for i in items)


def _reclassify(
    by_source: dict, script: str, reported: str, hidden: str, key: tuple[str, ...]
) -> None:
    """Pair findings that moved between a reported list and its suppressed list.

    Reported before, suppressed now: the code did not change, a differential
    did, so it is `newly_suppressed` on the reported list instead of `gone`
    (and not an `added` suppression). Suppressed before, reported now: it stays
    `added` -- it is visible again and needs triage like any new finding --
    marked `was_suppressed`, and is no longer `gone` from the suppressed list.
    """
    shown = by_source.get(f"{script}.{reported}")
    muted = by_source.get(f"{script}.{hidden}")
    if not shown or not muted:
        return

    def pair(gone: list[dict], added: list[dict]) -> list[tuple[dict, dict]]:
        waiting: dict[tuple, list[dict]] = {}
        for entry in added:
            waiting.setdefault(_key_of(entry, key), []).append(entry)
        pairs = []
        for entry in gone:
            match = waiting.get(_key_of(entry, key))
            if match:
                pairs.append((entry, match.pop(0)))
        return pairs

    hidden_now = pair(shown["gone"], muted["added"])
    if hidden_now:
        left = {id(old) for old, _ in hidden_now}
        arrived = {id(new) for _, new in hidden_now}
        shown["gone"] = [e for e in shown["gone"] if id(e) not in left]
        muted["added"] = [e for e in muted["added"] if id(e) not in arrived]
        shown["newly_suppressed"] = [
            {**old, "reason": new.get("reason")} for old, new in hidden_now
        ]
    shown_again = pair(muted["gone"], shown["added"])
    if shown_again:
        left = {id(old) for old, _ in shown_again}
        muted["gone"] = [e for e in muted["gone"] if id(e) not in left]
        for old, new in shown_again:
            new["was_suppressed"] = old.get("reason")
        shown["unsuppressed"] = [new for _, new in shown_again]


def _severity_of(entry: dict) -> str:
    return str(entry.get("severity", "")).upper()


def analyze(argv: list[str]) -> dict:
    """Diff two report directories. Takes argv, like ``analyze_history.py``."""
    positional = [a for a in argv if not a.startswith("--")]
    severity = None
    for i, arg in enumerate(argv):
        if arg == "--severity" and i + 1 < len(argv):
            severity = argv[i + 1].upper()
    positional = [a for a in positional if a != severity and a.upper() != severity]

    if len(positional) < 2:
        return {"error": "usage: diff_findings.py OLD_REPORT_DIR NEW_REPORT_DIR"}
    old_dir, new_dir = Path(positional[0]), Path(positional[1])
    for label, path in (("old", old_dir), ("new", new_dir)):
        if not path.is_dir():
            return {"error": f"{label} report directory not found: {path}"}

    notes: list[str] = []
    by_source: dict[str, dict] = {}
    totals = {
        "added": 0,
        "gone": 0,
        "moved": 0,
        "unchanged": 0,
        # Moved from a reported list to its suppressed list (not `gone`), and
        # back (still counted in `added`).
        "newly_suppressed": 0,
        "unsuppressed": 0,
    }

    scripts = sorted(
        {p.stem for p in old_dir.glob("*.json")}
        | {p.stem for p in new_dir.glob("*.json")}
    )
    for script in scripts:
        registered = dict(_FINDING_LISTS.get(script, {}))
        for name in _SEQUENCE_LISTS.get(script, ()):
            registered[name] = None  # type: ignore[assignment]
        if not registered:
            notes.append(f"{script}: no finding lists registered — not compared")
            continue

        old_data, old_err = _load_report(old_dir / f"{script}.json")
        new_data, new_err = _load_report(new_dir / f"{script}.json")
        if old_err or new_err:
            notes.append(
                f"{script}: not compared — old is {old_err or 'ok'}, "
                f"new is {new_err or 'ok'}"
            )
            continue

        assert old_data is not None and new_data is not None
        # Two runs of the same script over DIFFERENT trees are not comparable.
        # Caught on the coveragepy v1 -> v2 gate: v1's extract_test_invariants
        # had been run against `coveragepy/tests` and v2 against
        # `coveragepy/coverage`, and the diff reported 30 findings "gone" from a
        # tree the second run never looked at.
        old_root = old_data.get("scan_root")
        new_root = new_data.get("scan_root")
        if old_root and new_root and old_root != new_root:
            notes.append(
                f"{script}: NOT COMPARED — different scan roots "
                f"({old_root} vs {new_root}). A diff across scopes is meaningless."
            )
            continue
        for list_name, fields in registered.items():
            old_items = old_data.get(list_name) or []
            new_items = new_data.get(list_name) or []
            if not isinstance(old_items, list) or not isinstance(new_items, list):
                notes.append(f"{script}.{list_name}: not a list in one run — skipped")
                continue
            if not old_items and not new_items:
                continue
            fallback = _FALLBACK_KEYS.get((script, list_name))
            if fallback and fields and _lacks_fields(old_items + new_items, fields):
                fields = fallback
                notes.append(
                    f"{script}.{list_name}: an item has no "
                    f"{'/'.join(f for f in registered[list_name] if f not in fallback)}"
                    f" field (an older report), so it is keyed by "
                    f"{'/'.join(fallback)} and identically named symbols collide"
                )
            if (script, list_name) in _CAPPED and (
                old_data.get(f"{list_name}_capped")
                or new_data.get(f"{list_name}_capped")
            ):
                notes.append(
                    f"{script}.{list_name}: capped in at least one run, so items "
                    "past the cap appear as added/gone without changing"
                )
            result = _diff_list(old_items, new_items, fields)
            if severity:
                for bucket in ("added", "gone", "moved"):
                    result[bucket] = [
                        e for e in result[bucket] if _severity_of(e) == severity
                    ]
            by_source[f"{script}.{list_name}"] = result

        for name, reported, hidden, key in _RECLASSIFIED:
            if name == script:
                _reclassify(by_source, script, reported, hidden, key)

    for result in by_source.values():
        for bucket in totals:
            if bucket == "unchanged":
                totals[bucket] += result[bucket]
            else:
                totals[bucket] += len(result.get(bucket, ()))

    # Deliberately NOT a pass/fail boolean. An added finding is either a new true
    # positive (the point of a shape wave) or a false-positive regression, and
    # nothing here can tell them apart -- only triage can. The verdict names what
    # was measured and leaves the judgement to a human.
    if totals["added"] == 0 and totals["gone"] == 0:
        verdict = "stable — no findings appeared or disappeared"
        if totals["newly_suppressed"] or totals["unsuppressed"]:
            verdict += (
                f" ({totals['newly_suppressed']} newly suppressed, "
                f"{totals['unsuppressed']} no longer suppressed)"
            )
    elif totals["added"] == 0:
        verdict = f"{totals['gone']} finding(s) disappeared; none appeared"
    else:
        verdict = (
            f"{totals['added']} finding(s) appeared — triage each as a new true "
            f"positive or a false-positive regression"
        )

    return {
        "old": str(old_dir),
        "new": str(new_dir),
        "severity_filter": severity,
        "summary": totals,
        "verdict": verdict,
        "by_source": by_source,
        "notes": notes,
    }


def main() -> None:
    exit_on_help(sys.argv[1:], __doc__)
    try:
        result = analyze(sys.argv[1:])
        emit(result)
        sys.exit(1 if "error" in result else 0)
    except Exception as exc:  # noqa: BLE001 -- top-level guard
        emit({"error": str(exc), "type": type(exc).__name__})
        sys.exit(1)


if __name__ == "__main__":
    main()
