#!/usr/bin/env python3
"""Find the code a fix commit replaced that still exists elsewhere.

A propagation sweep: given a fix commit, take the Python statements and
conditions the fix removed or rewrote (its "removed forms"), and list every
place in the current tree that still has the same form. Those are the sites
the fix may have missed.

A form is the statement's syntax tree with variable names abstracted: called
names, attribute names, literals and operators must match, the variables
need not. `if order.paid:` rewritten to `if order.paid and not order.cancelled:`
yields the form `<v>.paid` (the condition), and every other condition that
is exactly `<v>.paid` is a candidate. For a
compound statement only its header is a form (an `if`/`while` condition, a
`for` target and iterable, the `with` items), so a fix inside a block does
not make the whole block a pattern.

A removed form is one that occurs fewer times in the file after the commit
than before, so lines that were only moved are not reported. Forms with no
call, attribute or subscript are skipped: `return <v>` matches everything. A fix that only adds code (a new guard
before an unchanged line) removes nothing; the output says so, and the
sweep for it is the git-history-analyzer's by hand.

Read-only: `git show` for the commit's two sides, the working tree for the
search.

Usage:
    python sweep_fix_pattern.py [path] --commit REV [--max-files N]

Options:
    --commit REV      The fix commit (any revision git accepts); required
    --max-files N     Search at most N Python files (default: no limit)
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scan_common import (  # noqa: E402
    build_envelope,
    collect_python_files,
    emit,
    exit_on_help,
    parse_common_args,
    parse_source,
    relative_to_root,
    resolve_target,
)

_GIT_TIMEOUT = 30
_MIN_NODES = 2
_MAX_MATCHES = 30
_MAX_FORMS = 50
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@", re.MULTILINE)
_SKIP_FIELDS = frozenset({"ctx", "type_comment", "kind"})
_SUBSTANTIVE = (ast.Call, ast.Attribute, ast.Subscript)


def _git(args: list[str], cwd: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=_GIT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def form_of(node: ast.AST) -> tuple[str, int, bool]:
    """(form, node count, substantive) for a node, names abstracted.

    A `Name` becomes `<v>` unless it is the function being called, so
    `parse(data)` and `parse(raw)` share a form and `parse(x)` and
    `load(x)` do not.
    """
    callees = {id(n.func) for n in ast.walk(node) if isinstance(n, ast.Call)}
    parts: list[str] = []
    count = 0
    substantive = False
    todo: list[object] = [node]
    while todo:
        item = todo.pop()
        if isinstance(item, str):
            parts.append(item)
            continue
        if not isinstance(item, ast.AST):
            parts.append(repr(item))
            continue
        count += 1
        substantive = substantive or isinstance(item, _SUBSTANTIVE)
        if isinstance(item, ast.Name):
            parts.append(item.id if id(item) in callees else "<v>")
            continue
        parts.append(type(item).__name__ + "(")
        pending: list[object] = []
        for field, value in ast.iter_fields(item):
            if field in _SKIP_FIELDS:
                continue
            if isinstance(value, list):
                pending.append("[")
                pending.extend(value)
                pending.append("]")
            else:
                pending.append(value)
        pending.append(")")
        todo.extend(reversed(pending))
    return " ".join(parts), count, substantive


def _units(tree: ast.AST):
    """(node, first line, last line) for every form-bearing unit in a tree.

    A simple statement is one unit. A compound statement contributes its
    header only; its body's statements are units of their own.
    """
    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.While)):
            yield node.test, node.test.lineno, node.test.end_lineno or node.lineno
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            pair = ast.Tuple(elts=[node.target, node.iter], ctx=ast.Load())
            yield pair, node.lineno, node.iter.end_lineno or node.lineno
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            items = ast.Tuple(elts=[i.context_expr for i in node.items], ctx=ast.Load())
            last = node.items[-1].context_expr
            yield items, node.lineno, last.end_lineno or node.lineno
        elif isinstance(node, ast.Match):
            yield (
                node.subject,
                node.subject.lineno,
                node.subject.end_lineno or node.lineno,
            )
        elif (
            isinstance(node, ast.stmt)
            and not isinstance(
                node,
                (
                    ast.FunctionDef,
                    ast.AsyncFunctionDef,
                    ast.ClassDef,
                    ast.Try,
                    ast.Import,
                    ast.ImportFrom,
                    ast.Pass,
                    ast.Break,
                    ast.Continue,
                    ast.Global,
                    ast.Nonlocal,
                ),
            )
            and not hasattr(node, "body")
        ):
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                continue  # docstrings and bare literals
            yield node, node.lineno, node.end_lineno or node.lineno


def _function_spans(tree: ast.AST) -> list[tuple[str, int, int]]:
    spans: list[tuple[str, int, int]] = []

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}{child.name}"
                if not isinstance(child, ast.ClassDef):
                    spans.append((name, child.lineno, child.end_lineno or child.lineno))
                walk(child, name + ".")
            else:
                walk(child, prefix)

    walk(tree, "")
    return spans


def _enclosing(spans: list[tuple[str, int, int]], line: int) -> str | None:
    best = None
    for name, start, end in spans:
        if start <= line <= end and (best is None or start >= best[1]):
            best = (name, start)
    return best[0] if best else None


def _parse(text: str | None) -> ast.Module | None:
    if text is None:
        return None
    try:
        return ast.parse(text)
    except (SyntaxError, ValueError):
        return None


def _form_counts(tree: ast.AST) -> Counter:
    return Counter(form_of(node)[0] for node, _, _ in _units(tree))


def removed_forms(before: str, after: str | None, removed: list[tuple[int, int]]):
    """The forms on `removed` line ranges of `before` that `after` has fewer of.

    Returns (forms, reason): forms is a list of {"form", "line", "code"}; the
    reason is set when the file could not be parsed.
    """
    old_tree = _parse(before)
    if old_tree is None:
        return [], "the file before the commit does not parse"
    new_tree = (
        _parse(after) if after is not None else ast.Module(body=[], type_ignores=[])
    )
    if new_tree is None:
        return [], "the file after the commit does not parse"
    before_counts = _form_counts(old_tree)
    after_counts = _form_counts(new_tree)
    lines = before.splitlines()
    out: list[dict] = []
    seen: set[str] = set()
    for node, first, last in _units(old_tree):
        if not any(first <= end and last >= start for start, end in removed):
            continue
        form, count, substantive = form_of(node)
        if count < _MIN_NODES or not substantive or form in seen:
            continue
        if after_counts[form] >= before_counts[form]:
            continue  # moved or duplicated, not removed
        seen.add(form)
        out.append(
            {
                "form": form,
                "line": first,
                "code": "\n".join(lines[first - 1 : last]).strip(),
            }
        )
    return out, None


def _removed_ranges(diff: str) -> list[tuple[int, int]]:
    ranges = []
    for start, count in _HUNK_RE.findall(diff):
        n = 1 if count == "" else int(count)
        if n:
            ranges.append((int(start), int(start) + n - 1))
    return ranges


def analyze(target: str, *, max_files: int = 0, commit: str | None = None) -> dict:
    """Sweep the tree for the forms `commit` removed; see the module docstring."""
    _resolved, project_root, scan_root = resolve_target(target)
    if not commit:
        return {"error": "--commit REV is required"}
    top = _git(["rev-parse", "--show-toplevel"], project_root)
    if top is None:
        return {"error": "not a git repository", "project_root": str(project_root)}
    git_root = Path(top.strip())
    sha = _git(["rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}"], git_root)
    if not sha:
        return {"error": f"unknown commit {commit!r}"}
    sha = sha.strip()
    subject = (_git(["log", "-1", "--format=%s", sha], git_root) or "").strip()
    parents = (_git(["log", "-1", "--format=%P", sha], git_root) or "").split()
    parent = parents[0] if parents else None

    names = _git(
        ["diff-tree", "--no-commit-id", "-r", "--name-status", "--no-renames", sha]
        if parent
        else ["diff-tree", "--root", "--no-commit-id", "-r", "--name-status", sha],
        git_root,
    )
    forms: list[dict] = []
    skipped: list[dict] = []
    for row in (names or "").splitlines():
        status, _, path = row.partition("\t")
        if not path.endswith(".py"):
            continue
        if status.startswith("A") or parent is None:
            continue  # a new file replaced nothing
        before = _git(["show", f"{parent}:{path}"], git_root)
        after = (
            None
            if status.startswith("D")
            else _git(["show", f"{sha}:{path}"], git_root)
        )
        diff = _git(
            ["diff", "-U0", "--no-color", "--no-ext-diff", parent, sha, "--", path],
            git_root,
        )
        if before is None or diff is None:
            skipped.append({"file": path, "reason": "git show failed"})
            continue
        found, reason = removed_forms(before, after, _removed_ranges(diff))
        if reason:
            skipped.append({"file": path, "reason": reason})
        for item in found:
            forms.append({"file": path, **item})
    forms_total = len(forms)
    forms = forms[:_MAX_FORMS]

    files, files_total = collect_python_files(scan_root, max_files)
    wanted = {f["form"]: f for f in forms}
    matches: dict[str, list[dict]] = {f["form"]: [] for f in forms}
    unparsed = 0
    for path in files:
        tree = parse_source(path)
        if tree is None:
            unparsed += 1
            continue
        rel = relative_to_root(path, git_root)
        spans = _function_spans(tree)
        source_lines: list[str] | None = None
        for node, first, last in _units(tree):
            if not wanted:
                break
            form = form_of(node)[0]
            if form not in wanted:
                continue
            if source_lines is None:
                source_lines = path.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines()
            matches[form].append(
                {
                    "file": rel,
                    "line": first,
                    "function": _enclosing(spans, first),
                    "code": "\n".join(source_lines[first - 1 : last]).strip(),
                    # The file the fix changed: an unfixed sibling in the
                    # same file is the likeliest miss.
                    "same_file": rel == wanted[form]["file"],
                }
            )

    result = build_envelope(project_root, scan_root, files_total, len(files))
    for form in forms:
        hits = matches[form["form"]]
        form["matches"] = hits[:_MAX_MATCHES]
        form["matches_total"] = len(hits)
    result.update(
        {
            "commit": sha,
            "subject": subject,
            "removed_forms": forms,
            "removed_forms_total": forms_total,
            "removed_forms_capped": forms_total > len(forms),
            "files_unparsed": unparsed,
            "skipped": skipped,
            "notes": [],
        }
    )
    if parent is None:
        result["notes"].append("The commit has no parent: it replaced nothing.")
    elif not forms_total:
        result["notes"].append(
            "The commit removed or rewrote no substantive Python statement or "
            "condition (it only added code, or changed trivial lines). There is "
            "no removed form to sweep for; look for siblings of what it ADDED "
            "by hand (the call sites that lack the new guard)."
        )
    return result


def main() -> None:
    argv = sys.argv[1:]
    exit_on_help(argv, __doc__)
    commit = None
    rest: list[str] = []
    i = 0
    while i < len(argv):
        if argv[i] == "--commit" and i + 1 < len(argv):
            commit = argv[i + 1]
            i += 2
        else:
            rest.append(argv[i])
            i += 1
    target, max_files = parse_common_args(rest, __doc__)
    result = analyze(target, max_files=max_files, commit=commit)
    emit(result)
    if "error" in result:
        sys.exit(1)


if __name__ == "__main__":
    main()
