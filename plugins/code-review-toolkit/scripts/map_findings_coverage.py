#!/usr/bin/env python3
"""Map findings' file:line ranges to measured coverage, or probe them with mutations.

Coverage mode answers "did the suite ever execute the defective lines?" for
every `file:line` (or `file:a-b`) reference other agents reported. It reads a
`coverage json` report (run with `--branch` for branch data) and, per
reference, gives the statements that ran and did not, the missing branches,
the enclosing function's coverage and a verdict:

    never_run             no statement in the range ran
    partly_run            some ran, some did not
    ran_branches_missing  every statement ran, a branch out of the range never did
    ran                   every statement and branch ran (it ran: whether a test
                          set up the failing trigger, or checks the result, is
                          the agent's question, and mutation mode's)
    null                  not measured (file not in the coverage data, or no
                          statement at that line); `reason` says why

Mutation mode separates "covered and checked" from "covered and blind". It
applies `(file, old, new)` edits one at a time to an EXPORTED copy of the
project, runs the test command after each, and restores the file. A "break"
mutation that survives is code the suite runs but never checks; a "fix"
mutation (the fix for a reported defect) that is killed means a test pins
the defective behaviour. The baseline run must pass, or nothing is judged.

Usage:
    python map_findings_coverage.py [path] --coverage coverage.json \\
        (--findings FILE)... (--ref FILE:LINE[-END])...
    python map_findings_coverage.py [path] --mutations mutations.json \\
        --export DIR --test-cmd "python -m pytest -q -x -p no:cacheprovider" \\
        [--timeout SECONDS]

`path` is the project root (default `.`); source is read from it to find
enclosing functions. `--findings` takes a JSON file (a list of objects, or an
object holding one, with `file` + `line` [+ `end_line`] or a `location`
string; `id` is kept) or any text file, from which every `path.py:N` and
`path.py:N-M` is taken. A mutations file is a JSON list (or `{"mutations":
[...]}`) of `{"id", "file", "old", "new", "kind": "break"|"fix", "finding"}`;
`old` must occur exactly once in the file. `--export` must not be a git
checkout: make one with `git -C <repo> archive HEAD | tar -x -C DIR`.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scan_common import (  # noqa: E402
    build_envelope,
    emit,
    exit_on_help,
    find_project_root,
    parse_source,
)

_REF_RE = re.compile(r"(?P<file>[\w./\\-]+\.py):(?P<start>\d+)(?:-(?P<end>\d+))?")
_LINE_KEYS = ("line", "line_start", "start_line", "lineno")
_END_KEYS = ("end_line", "line_end", "end_lineno")
_ID_KEYS = ("id", "key", "finding_id", "finding")


# ---------------------------------------------------------------------------
# Finding references
# ---------------------------------------------------------------------------


def _ref(file: str, start: int, end: int | None, ident, source: str) -> dict:
    end = end if end is not None and end >= start else start
    return {"id": ident, "file": file, "start": start, "end": end, "source": source}


def _refs_from_json(data, source: str) -> list[dict]:
    """References from any list of finding objects inside *data*."""
    refs: list[dict] = []
    stack = [data]
    while stack:
        item = stack.pop()
        if isinstance(item, list):
            stack.extend(reversed(item))
            continue
        if not isinstance(item, dict):
            continue
        ident = next((item[k] for k in _ID_KEYS if isinstance(item.get(k), str)), None)
        file = item.get("file") or item.get("path")
        line = next((item[k] for k in _LINE_KEYS if isinstance(item.get(k), int)), None)
        if isinstance(file, str) and file.endswith(".py") and line is not None:
            end = next(
                (item[k] for k in _END_KEYS if isinstance(item.get(k), int)), None
            )
            refs.append(_ref(file, line, end, ident, source))
        elif isinstance(item.get("location"), str):
            for m in _REF_RE.finditer(item["location"]):
                end = int(m.group("end")) if m.group("end") else None
                refs.append(
                    _ref(m.group("file"), int(m.group("start")), end, ident, source)
                )
        stack.extend(v for v in item.values() if isinstance(v, (list, dict)))
    return refs


def _refs_from_text(text: str, source: str) -> list[dict]:
    refs: list[dict] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for m in _REF_RE.finditer(line):
            end = int(m.group("end")) if m.group("end") else None
            refs.append(
                _ref(
                    m.group("file"),
                    int(m.group("start")),
                    end,
                    None,
                    f"{source}:{lineno}",
                )
            )
    return refs


def load_refs(findings: list[str], adhoc: list[str]) -> tuple[list[dict], list[str]]:
    """All references from *findings* files and `--ref` strings, deduplicated."""
    refs: list[dict] = []
    warnings: list[str] = []
    for name in findings:
        path = Path(name)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            warnings.append(f"cannot read findings file {name}: {exc}")
            continue
        found: list[dict] = []
        if path.suffix == ".json":
            try:
                found = _refs_from_json(json.loads(text), path.name)
            except json.JSONDecodeError:
                found = _refs_from_text(text, path.name)
        else:
            found = _refs_from_text(text, path.name)
        if not found:
            warnings.append(f"no file:line references found in {name}")
        refs.extend(found)
    for item in adhoc:
        found = _refs_from_text(item, "--ref")
        if not found:
            warnings.append(f"not a file:line reference: {item!r}")
        refs.extend(found)
    unique: dict[tuple, dict] = {}
    for ref in refs:
        key = (ref["file"], ref["start"], ref["end"])
        if key in unique:
            if ref["id"] and not unique[key]["id"]:
                unique[key]["id"] = ref["id"]
            continue
        unique[key] = ref
    return list(unique.values()), warnings


# ---------------------------------------------------------------------------
# Coverage mode
# ---------------------------------------------------------------------------


def _parts(path: str) -> tuple[str, ...]:
    return tuple(p for p in Path(path.replace("\\", "/")).parts if p not in ("/", "."))


def match_coverage_key(
    ref_file: str, keys: list[str], project_root: Path
) -> str | None:
    """The coverage-report key naming the same file as *ref_file*.

    Coverage keys are relative to wherever the suite ran (often an export) or
    absolute; a reference is project-relative or absolute. They match when
    one's path components end with the other's. None when no key matches,
    or when several match equally well.
    """
    ref = Path(ref_file)
    if ref.is_absolute():
        try:
            ref = ref.resolve().relative_to(project_root)
        except ValueError:
            pass
    want = _parts(str(ref))
    best: list[str] = []
    best_n = 0
    for key in keys:
        have = _parts(key)
        n = min(len(want), len(have))
        if not n or have[-n:] != want[-n:]:
            continue
        if n > best_n:
            best, best_n = [key], n
        elif n == best_n:
            best.append(key)
    # Two equally good keys (`a/util.py` and `b/util.py` for `util.py`): the
    # reference is ambiguous, and a guess would report the wrong file.
    return best[0] if len(best) == 1 else None


def _ranges(nums: list[int]) -> list[str]:
    """`[3, 4, 5, 9]` → `["3-5", "9"]`."""
    out: list[str] = []
    start = prev = None
    for n in sorted(nums):
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            out.append(f"{start}" if start == prev else f"{start}-{prev}")
            start = prev = n
    if start is not None:
        out.append(f"{start}" if start == prev else f"{start}-{prev}")
    return out


class _SourceFile:
    """A parsed project file: enclosing functions and statement starts."""

    def __init__(self, tree: ast.Module | None):
        self.functions: list[tuple[int, int, str]] = []
        self.statements: list[tuple[int, int]] = []
        if tree is None:
            return

        def visit(node: ast.AST, prefix: str) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    name = f"{prefix}{child.name}"
                    first = min(
                        [child.lineno] + [d.lineno for d in child.decorator_list]
                    )
                    self.functions.append(
                        (first, child.end_lineno or child.lineno, name)
                    )
                    visit(child, f"{name}.")
                elif isinstance(child, ast.ClassDef):
                    visit(child, f"{prefix}{child.name}.")
                else:
                    visit(child, prefix)

        visit(tree, "")
        # Simple statements only: a line inside an `if` block is not the
        # `if` line, but a continuation line is its call's first line.
        self.statements = [
            (n.lineno, n.end_lineno or n.lineno)
            for n in ast.walk(tree)
            if isinstance(n, ast.stmt) and not hasattr(n, "body")
        ]

    def enclosing_function(self, line: int) -> tuple[int, int, str] | None:
        hits = [f for f in self.functions if f[0] <= line <= f[1]]
        return min(hits, key=lambda f: f[1] - f[0]) if hits else None

    def statement_start(self, line: int) -> int | None:
        """First line of the innermost simple statement spanning *line*."""
        hits = [s for s in self.statements if s[0] <= line <= s[1]]
        return min(hits, key=lambda s: s[1] - s[0])[0] if hits else None


def _verdict(executed: list[int], missing: list[int], missing_branches) -> str:
    if not executed:
        return "never_run"
    if missing:
        return "partly_run"
    if missing_branches:
        return "ran_branches_missing"
    return "ran"


def map_coverage(
    project_root: Path, coverage_path: Path, refs: list[dict]
) -> tuple[list[dict], list[str], bool]:
    """Join *refs* to the coverage report; returns `(rows, warnings, branches)`."""
    data = json.loads(coverage_path.read_text(encoding="utf-8"))
    files: dict[str, dict] = data.get("files", {})
    branches = bool(data.get("meta", {}).get("branch_coverage"))
    keys = list(files)
    sources: dict[str, _SourceFile] = {}
    rows: list[dict] = []
    warnings: list[str] = []
    if not branches:
        warnings.append(
            "The coverage data has no branch information (run `coverage run "
            "--branch`): `ran` cannot tell an exception edge that never fired."
        )
    for ref in refs:
        row = dict(ref)
        key = match_coverage_key(ref["file"], keys, project_root)
        if key is None:
            row.update(verdict=None, reason="file not in the coverage data")
            rows.append(row)
            continue
        fdata = files[key]
        row["coverage_file"] = key
        if ref["file"] not in sources:
            local = Path(ref["file"])
            local = local if local.is_absolute() else project_root / local
            sources[ref["file"]] = _SourceFile(parse_source(local))
        src = sources[ref["file"]]
        start, end = ref["start"], ref["end"]
        executed = [n for n in fdata.get("executed_lines", []) if start <= n <= end]
        missing = [n for n in fdata.get("missing_lines", []) if start <= n <= end]
        if not executed and not missing:
            # A continuation line or a decorator: coverage counts the
            # statement at its first line.
            first = src.statement_start(start)
            if first is not None and first != start:
                row["expanded_to"] = first
                start = end = first
                executed = [n for n in fdata.get("executed_lines", []) if n == first]
                missing = [n for n in fdata.get("missing_lines", []) if n == first]
        if not executed and not missing:
            row.update(
                verdict=None,
                reason="no executable statement in the range (comment, blank "
                "or excluded line)",
            )
            rows.append(row)
            continue
        missing_branches = None
        if branches:
            missing_branches = [
                b for b in fdata.get("missing_branches", []) if start <= b[0] <= end
            ]
        row.update(
            statements=len(executed) + len(missing),
            executed=len(executed),
            missing_lines=_ranges(missing),
            missing_branches=missing_branches,
            verdict=_verdict(executed, missing, missing_branches),
        )
        func = src.enclosing_function(ref["start"])
        if func is not None:
            f_exec = [
                n for n in fdata.get("executed_lines", []) if func[0] <= n <= func[1]
            ]
            f_miss = [
                n for n in fdata.get("missing_lines", []) if func[0] <= n <= func[1]
            ]
            total = len(f_exec) + len(f_miss)
            row["function"] = {
                "name": func[2],
                "start": func[0],
                "end": func[1],
                "statements": total,
                "executed": len(f_exec),
                "percent": round(100 * len(f_exec) / total, 1) if total else None,
            }
        rows.append(row)
    return rows, warnings, branches


# ---------------------------------------------------------------------------
# Mutation mode
# ---------------------------------------------------------------------------

_FAILED_RE = re.compile(r"^(?:FAILED|ERROR)[: ]\s*(.+)$")


def _run(cmd: list[str], cwd: Path, env: dict, timeout: int) -> dict:
    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "returncode": None,
            "timed_out": True,
            "seconds": round(time.monotonic() - started, 1),
        }
    except OSError as exc:
        return {
            "returncode": None,
            "timed_out": False,
            "error": str(exc),
            "seconds": round(time.monotonic() - started, 1),
        }
    output = (proc.stdout or "") + "\n" + (proc.stderr or "")
    lines = [ln for ln in output.splitlines() if ln.strip()]
    failed = [m.group(1)[:200] for ln in lines if (m := _FAILED_RE.match(ln))]
    return {
        "returncode": proc.returncode,
        "timed_out": False,
        "seconds": round(time.monotonic() - started, 1),
        "summary": lines[-1][:200] if lines else "",
        "failed_tests": failed[:5],
    }


def load_mutations(path: Path) -> list[dict]:
    """The mutations in *path*; raises on a malformed file."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("mutations", [])
    if not isinstance(data, list):
        raise TypeError('a mutations file is a JSON list or {"mutations": [...]}')
    out = []
    for i, item in enumerate(data, 1):
        if not (isinstance(item, dict) and {"file", "old", "new"} <= item.keys()):
            raise ValueError(f"mutation {i} needs file, old and new")
        kind = item.get("kind", "break")
        if kind not in ("break", "fix"):
            raise ValueError(f"mutation {i}: kind must be 'break' or 'fix'")
        out.append(
            {
                "id": item.get("id") or f"M{i}",
                "file": item["file"],
                "old": item["old"],
                "new": item["new"],
                "kind": kind,
                "finding": item.get("finding"),
            }
        )
    return out


# What a verdict means for each kind of mutation.
_MEANING = {
    ("break", "killed"): "checked",
    ("break", "survived"): "covered_but_unchecked",
    ("fix", "killed"): "pinned",
    ("fix", "survived"): "fix_not_detected",
}


def probe_mutations(
    export: Path, mutations: list[dict], test_cmd: list[str], timeout: int
) -> tuple[dict, list[dict]]:
    """Run the baseline, then each mutation; returns `(baseline, rows)`."""
    scratch = Path(tempfile.mkdtemp(prefix="crt-mutation-"))
    env = dict(os.environ, TMPDIR=str(scratch), PYTHONDONTWRITEBYTECODE="1")
    try:
        baseline = _run(test_cmd, export, env, timeout)
        rows: list[dict] = []
        passed = baseline["returncode"] == 0
        for mutation in mutations:
            row = {k: mutation[k] for k in ("id", "file", "kind", "finding")}
            if not passed:
                row.update(verdict=None, reason="the baseline run did not pass")
                rows.append(row)
                continue
            target = export / mutation["file"]
            try:
                original = target.read_bytes()
            except OSError as exc:
                row.update(verdict="skipped", reason=f"cannot read the file: {exc}")
                rows.append(row)
                continue
            text = original.decode("utf-8")
            count = text.count(mutation["old"])
            if count != 1:
                row.update(
                    verdict="skipped",
                    reason=f"`old` occurs {count} times in the file (needs exactly 1)",
                )
                rows.append(row)
                continue
            try:
                target.write_text(
                    text.replace(mutation["old"], mutation["new"]), encoding="utf-8"
                )
                result = _run(test_cmd, export, env, timeout)
            finally:
                target.write_bytes(original)
            if result.get("timed_out"):
                verdict = "timeout"
            elif result["returncode"] is None:
                verdict = None
                row["reason"] = result.get("error", "the test command did not run")
            else:
                verdict = "survived" if result["returncode"] == 0 else "killed"
            row.update(
                verdict=verdict,
                meaning=_MEANING.get((mutation["kind"], verdict)),
                seconds=result["seconds"],
                summary=result.get("summary", ""),
                failed_tests=result.get("failed_tests", []),
            )
            rows.append(row)
        return baseline, rows
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def analyze(
    target: str,
    *,
    max_files: int = 0,
    coverage: str | None = None,
    findings: list[str] | None = None,
    refs: list[str] | None = None,
    mutations: str | None = None,
    export: str | None = None,
    test_cmd: str | None = None,
    timeout: int = 300,
) -> dict:
    """Coverage mode (`coverage`) or mutation mode (`mutations`)."""
    project_root = find_project_root(Path(target).resolve())
    if mutations:
        return _analyze_mutations(
            project_root, Path(mutations), export, test_cmd, timeout, max_files
        )
    if not coverage:
        return {
            "status": "FAILED",
            "error": "give --coverage (coverage mode) or --mutations (mutation mode)",
        }
    all_refs, warnings = load_refs(findings or [], refs or [])
    files = list(dict.fromkeys(r["file"] for r in all_refs))
    if max_files > 0 and len(files) > max_files:
        kept = set(files[:max_files])
        all_refs = [r for r in all_refs if r["file"] in kept]
    analyzed = len({r["file"] for r in all_refs})
    envelope = build_envelope(project_root, project_root, len(files), analyzed)
    try:
        rows, cov_warnings, branches = map_coverage(
            project_root, Path(coverage), all_refs
        )
    except (OSError, ValueError) as exc:
        return {
            **envelope,
            "mode": "coverage",
            "status": "FAILED",
            "error": f"cannot read the coverage report {coverage}: {exc}",
        }
    counts: dict[str, int] = {}
    for row in rows:
        verdict = row["verdict"] or "not_measured"
        counts[verdict] = counts.get(verdict, 0) + 1
    status = "ok" if rows else "no_refs"
    if not rows:
        warnings.append("no file:line references to map: nothing was measured")
    return {
        **envelope,
        "mode": "coverage",
        "status": status,
        "coverage_report": str(coverage),
        "branch_coverage": branches,
        "warnings": warnings + cov_warnings,
        "summary": {
            "refs": len(rows),
            **{
                k: counts.get(k, 0)
                for k in (
                    "never_run",
                    "partly_run",
                    "ran_branches_missing",
                    "ran",
                    "not_measured",
                )
            },
        },
        "refs": rows,
    }


def _analyze_mutations(
    project_root: Path,
    mutations_path: Path,
    export: str | None,
    test_cmd: str | None,
    timeout: int,
    max_files: int,
) -> dict:
    if not export or not test_cmd:
        return {
            "mode": "mutation",
            "status": "FAILED",
            "error": "mutation mode needs --export DIR and --test-cmd",
        }
    export_root = Path(export).resolve()
    if not export_root.is_dir():
        return {
            "mode": "mutation",
            "status": "FAILED",
            "error": f"--export {export} is not a directory",
        }
    if (export_root / ".git").exists() or export_root == project_root:
        return {
            "mode": "mutation",
            "status": "FAILED",
            "error": "--export must be an exported copy, not a checkout: "
            "git -C <repo> archive HEAD | tar -x -C <dir>",
        }
    try:
        items = load_mutations(mutations_path)
    except (OSError, ValueError, TypeError) as exc:
        return {"mode": "mutation", "status": "FAILED", "error": str(exc)}
    files = list(dict.fromkeys(m["file"] for m in items))
    if max_files > 0 and len(files) > max_files:
        kept = set(files[:max_files])
        items = [m for m in items if m["file"] in kept]
    baseline, rows = probe_mutations(export_root, items, shlex.split(test_cmd), timeout)
    counts: dict[str, int] = {}
    for row in rows:
        verdict = row["verdict"] or "not_run"
        counts[verdict] = counts.get(verdict, 0) + 1
    passed = baseline["returncode"] == 0
    return {
        **build_envelope(
            export_root, export_root, len(files), len({m["file"] for m in items})
        ),
        "mode": "mutation",
        # baseline_failed: the suite fails without any mutation, so no
        # verdict means anything.
        "status": "ok" if passed else "baseline_failed",
        "test_cmd": test_cmd,
        "baseline": baseline,
        "summary": {
            "mutations": len(rows),
            **{
                k: counts.get(k, 0)
                for k in (
                    "killed",
                    "survived",
                    "timeout",
                    "skipped",
                    "not_run",
                )
            },
        },
        "mutations": rows,
    }


def main() -> None:
    argv = sys.argv[1:]
    exit_on_help(argv, __doc__)
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("path", nargs="?", default=".")
    parser.add_argument("--max-files", type=int, default=0)
    parser.add_argument("--coverage")
    parser.add_argument("--findings", action="append", default=[])
    parser.add_argument("--ref", action="append", default=[])
    parser.add_argument("--mutations")
    parser.add_argument("--export")
    parser.add_argument("--test-cmd")
    parser.add_argument("--timeout", type=int, default=300)
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        emit({"status": "FAILED", "error": "bad arguments; see --help"})
        sys.exit(2)
    result = analyze(
        args.path,
        max_files=args.max_files,
        coverage=args.coverage,
        findings=args.findings,
        refs=args.ref,
        mutations=args.mutations,
        export=args.export,
        test_cmd=args.test_cmd,
        timeout=args.timeout,
    )
    emit(result)
    if result.get("status") == "FAILED":
        sys.exit(1)


if __name__ == "__main__":
    main()
