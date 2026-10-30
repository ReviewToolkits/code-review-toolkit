#!/usr/bin/env python3
"""Measure per-function complexity metrics using AST analysis.

Outputs a JSON structure with per-function metrics:
- line_count (excluding blanks and comments)
- nesting_depth (maximum indentation level of control flow)
- parameter_count
- branch_count (if/elif/else/match-case/try-except branches)
- loop_count (for/while including nested)
- local_variable_count
- return_count (number of return statements)
- cognitive_complexity (weighted complexity score)

Usage:
    python measure_complexity.py [path]
"""

import ast
import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from analyze_history import (  # noqa: E402
    DIFF_OPTIONS,
    BlobFunctionIndex,
    attribute_file_diff,
    classify_commit,
    def_start_line,
    iter_qualified_defs,
    parse_key_types,
    parse_unified_diff,
)
from scan_common import (  # noqa: E402
    discover_python_files,
    find_project_root,
)

_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _iter_own_nodes(func_node: ast.AST):
    """Walk a function's own body, NOT descending into nested defs or classes.

    `ast.walk` plus `continue` does not prune: the nested def's children are
    still queued. That is how a router factory whose body is nothing but
    nested route handlers came out with `return_count: 46` and a hotspot score.
    """
    todo = list(ast.iter_child_nodes(func_node))
    while todo:
        node = todo.pop()
        yield node
        if not isinstance(node, _SCOPE_NODES):
            todo.extend(ast.iter_child_nodes(node))


def _nested_scopes(func_node: ast.AST) -> list[ast.AST]:
    """Defs and classes directly inside this function's own scope."""
    return [n for n in _iter_own_nodes(func_node) if isinstance(n, _SCOPE_NODES)]


def _is_always_false(test: ast.expr) -> bool:
    """True for `if 0:` / `if False:` -- a disabled block, not a branch."""
    return isinstance(test, ast.Constant) and not test.value


class _NestingVisitor(ast.NodeVisitor):
    """Walk a function body tracking nesting depth of control flow.

    Measures *reader-facing* depth. Two constructs that the AST nests and a
    reader does not are deliberately flattened: an `elif` chain (which the AST
    models as an `If` inside each `orelse`) and a disabled `if False:` block.
    Counting either inflates every enclosing function and made complexity a
    worse ranking signal than it needed to be.
    """

    def __init__(self) -> None:
        self.max_depth = 0
        self._depth = 0

    def _enter(self) -> None:
        self._depth += 1
        self.max_depth = max(self.max_depth, self._depth)

    def _exit(self) -> None:
        self._depth -= 1

    # Control flow nodes that increase nesting.
    def visit_If(self, node: ast.If) -> None:
        # `if 0:` / `if False:` is a disabled debug block. It costs the reader
        # nothing and counting it overstates depth for the whole enclosing
        # function -- a common idiom in older stdlib code.
        if _is_always_false(node.test):
            return
        self._enter()
        for child in node.body:
            self.visit(child)
        # An `elif` is an `If` sitting in `orelse`. Descending into it through
        # generic_visit charges a level per arm, so a flat if/elif/elif chain
        # reads as depth 3. A reader sees one decision point.
        orelse = node.orelse
        while len(orelse) == 1 and isinstance(orelse[0], ast.If):
            elif_node = orelse[0]
            if _is_always_false(elif_node.test):
                return
            for child in elif_node.body:
                self.visit(child)
            orelse = elif_node.orelse
        for child in orelse:
            self.visit(child)
        self._exit()

    def visit_For(self, node: ast.For) -> None:
        self._enter()
        self.generic_visit(node)
        self._exit()

    def visit_While(self, node: ast.While) -> None:
        self._enter()
        self.generic_visit(node)
        self._exit()

    def visit_With(self, node: ast.With) -> None:
        self._enter()
        self.generic_visit(node)
        self._exit()

    def visit_Try(self, node: ast.Try) -> None:
        self._enter()
        self.generic_visit(node)
        self._exit()

    def visit_TryStar(self, node: ast.AST) -> None:
        self._enter()
        self.generic_visit(node)
        self._exit()

    def visit_Match(self, node: ast.AST) -> None:
        self._enter()
        self.generic_visit(node)
        self._exit()

    # Don't recurse into nested function/class definitions.
    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        pass

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        pass

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        pass


class _CognitiveVisitor(ast.NodeVisitor):
    """Compute a cognitive complexity score.

    Increments for each control-flow break, with additional weight for nesting.
    Based on the SonarSource cognitive complexity specification (simplified).
    """

    def __init__(self) -> None:
        self.score = 0
        self._nesting = 0

    def _incr(self, nesting_penalty: bool = True) -> None:
        self.score += 1 + (self._nesting if nesting_penalty else 0)

    def visit_If(self, node: ast.If) -> None:
        self._incr()
        self._nesting += 1
        # Visit the body.
        for child in node.body:
            self.visit(child)
        self._nesting -= 1
        # elif / else: +1 each without nesting penalty.
        for child in node.orelse:
            if isinstance(child, ast.If):
                # elif
                self.score += 1  # no nesting penalty for elif
                self._nesting += 1
                for c in child.body:
                    self.visit(c)
                self._nesting -= 1
                # Continue with its orelse.
                for c in child.orelse:
                    if isinstance(c, ast.If):
                        self.visit_If(c)
                    else:
                        self.visit(c)
            else:
                self.score += 1  # else
                self.visit(child)

    def visit_For(self, node: ast.For) -> None:
        self._incr()
        self._nesting += 1
        self.generic_visit(node)
        self._nesting -= 1

    def visit_While(self, node: ast.While) -> None:
        self._incr()
        self._nesting += 1
        self.generic_visit(node)
        self._nesting -= 1

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        self._incr()
        self._nesting += 1
        self.generic_visit(node)
        self._nesting -= 1

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        # Each boolean operator sequence adds 1.
        self.score += 1
        self.generic_visit(node)

    def visit_Break(self, node: ast.Break) -> None:
        self.score += 1

    def visit_Continue(self, node: ast.Continue) -> None:
        self.score += 1

    # Don't recurse into nested definitions.
    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        pass

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        pass

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        pass


def _count_branches(func_node: ast.AST) -> int:
    """Count if/elif/else/match-case/except branches in a function body."""
    count = 0
    for node in _iter_own_nodes(func_node):
        if isinstance(node, ast.If):
            count += 1  # if
            if node.orelse:
                count += 1  # else or elif (each elif becomes another If)
        elif isinstance(node, ast.ExceptHandler):
            count += 1
        elif isinstance(node, ast.Match):
            count += len(getattr(node, "cases", []))

    return count


def _count_loops(func_node: ast.AST) -> int:
    """Count for/while loops (including nested)."""
    count = 0
    for node in _iter_own_nodes(func_node):
        if isinstance(node, (ast.For, ast.While, ast.AsyncFor)):
            count += 1
    return count


def _count_returns(func_node: ast.AST) -> int:
    """Count return statements."""
    count = 0
    for node in _iter_own_nodes(func_node):
        if isinstance(node, ast.Return):
            count += 1
    return count


def _count_local_variables(func_node: ast.AST) -> int:
    """Count unique variable names assigned in the function body."""
    names: set[str] = set()
    for node in _iter_own_nodes(func_node):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
                elif isinstance(target, ast.Tuple):
                    for elt in target.elts:
                        if isinstance(elt, ast.Name):
                            names.add(elt.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            if isinstance(node.target, ast.Name):
                names.add(node.target.id)
            elif isinstance(node.target, ast.Tuple):
                for elt in node.target.elts:
                    if isinstance(elt, ast.Name):
                        names.add(elt.id)
    return len(names)


def _effective_line_count(source_lines: list[str]) -> int:
    """Count non-blank, non-comment lines."""
    count = 0
    in_docstring = False
    docstring_quote: str | None = None
    for line in source_lines:
        stripped = line.strip()
        if not stripped:
            continue

        # Handle multiline strings (rough heuristic).
        if in_docstring:
            if docstring_quote and docstring_quote in stripped:
                in_docstring = False
            continue

        if stripped.startswith(('"""', "'''")):
            quote = stripped[:3]
            # Single-line docstring.
            if stripped.count(quote) >= 2:
                continue
            in_docstring = True
            docstring_quote = quote
            continue

        if stripped.startswith("#"):
            continue

        count += 1
    return count


def _param_count(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """Count parameters excluding 'self' and 'cls'."""
    args = func_node.args
    count = len(args.args) + len(args.posonlyargs) + len(args.kwonlyargs)
    # Subtract self/cls.
    if args.args and args.args[0].arg in ("self", "cls"):
        count -= 1
    if args.vararg:
        count += 1
    if args.kwarg:
        count += 1
    return count


def _compute_score(metrics: dict) -> float:
    """Compute an overall complexity score (1-10) from individual metrics."""
    score = 1.0

    # Line count contribution (>50 is notable, >100 is high, >200 is critical).
    lc = metrics["line_count"]
    if lc > 200:
        score += 3.0
    elif lc > 100:
        score += 2.0
    elif lc > 50:
        score += 1.0

    # Nesting depth (>3 is notable, >5 is high).
    nd = metrics["nesting_depth"]
    if nd > 5:
        score += 2.5
    elif nd > 3:
        score += 1.5
    elif nd > 2:
        score += 0.5

    # Parameter count (>5 is notable, >8 is high).
    pc = metrics["parameter_count"]
    if pc > 8:
        score += 2.0
    elif pc > 5:
        score += 1.0

    # Cognitive complexity (>15 is notable, >30 is high, >50 is critical).
    cc = metrics["cognitive_complexity"]
    if cc > 50:
        score += 3.0
    elif cc > 30:
        score += 2.0
    elif cc > 15:
        score += 1.0

    # Branch count (>10 is notable).
    if metrics["branch_count"] > 10:
        score += 1.0

    # Local variables (>10 is notable).
    if metrics["local_variable_count"] > 10:
        score += 0.5

    return min(score, 10.0)


def analyze_file(filepath: Path, project_root: Path) -> dict:
    """Analyze all functions/methods in a file."""
    rel = str(filepath.relative_to(project_root))
    result: dict = {
        "file": rel,
        "functions": [],
        "parse_error": None,
    }

    try:
        source = filepath.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(filepath))
    except SyntaxError as exc:
        result["parse_error"] = f"SyntaxError: {exc.msg} (line {exc.lineno})"
        return result

    source_lines = source.splitlines()

    def _process_func(
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        qualname: str,
        kinds: tuple[str, ...],
    ) -> dict:
        start = node.lineno
        end = node.end_lineno or node.lineno
        # A nested def or class is measured as its own entry, so its lines
        # are not this function's lines.
        nested = _nested_scopes(node)
        excluded: set[int] = set()
        for child in nested:
            excluded.update(
                range(def_start_line(child), (child.end_lineno or child.lineno) + 1)
            )
        func_lines = [
            source_lines[i - 1]
            for i in range(start, end + 1)
            if i not in excluded and i - 1 < len(source_lines)
        ]
        effective_lines = _effective_line_count(func_lines)

        nesting_v = _NestingVisitor()
        for child in ast.iter_child_nodes(node):
            nesting_v.visit(child)

        cognitive_v = _CognitiveVisitor()
        for child in ast.iter_child_nodes(node):
            cognitive_v.visit(child)

        metrics = {
            "line_count": effective_lines,
            "total_lines": end - start + 1,
            "nesting_depth": nesting_v.max_depth,
            "parameter_count": _param_count(node),
            "branch_count": _count_branches(node),
            "loop_count": _count_loops(node),
            "return_count": _count_returns(node),
            "local_variable_count": _count_local_variables(node),
            "cognitive_complexity": cognitive_v.score,
        }

        segments = qualname.split(".")[:-1]
        enclosing = list(zip(segments, kinds))
        in_test_class = any(k == "class" and n.startswith("Test") for n, k in enclosing)
        in_test_func = any(
            k == "function" and n.startswith("test") for n, k in enclosing
        )
        return {
            "name": qualname,
            "qualified_name": f"{rel}::{qualname}",
            "line_start": start,
            "line_end": end,
            "is_method": bool(kinds) and kinds[-1] == "class",
            # Defined inside another function: a closure, or a route handler
            # inside a router factory.
            "is_nested": "function" in kinds,
            "parent": ".".join(segments) or None,
            "nested_def_count": sum(
                1
                for n in nested
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            ),
            "is_async": isinstance(node, ast.AsyncFunctionDef),
            "is_test": node.name.startswith("test") or in_test_class or in_test_func,
            "has_decorators": len(node.decorator_list) > 0,
            "decorator_count": len(node.decorator_list),
            "metrics": metrics,
            "score": round(_compute_score(metrics), 1),
        }

    # Every function at any depth: module level, methods, nested classes, and
    # functions defined inside other functions (each measured on its own).
    for qualname, node, kinds in iter_qualified_defs(tree):
        result["functions"].append(_process_func(node, qualname, kinds))
    result["functions"].sort(key=lambda f: f["line_start"])

    return result


# Each historical version of a hotspot's file is parsed to find which function
# a fix touched at that commit. This caps the parses; past it the remaining
# fix commits are counted as unattributed and the affected verdicts say so.
MAX_HISTORY_PARSES = 1500
# Commits per `git show` call when fetching the diffs the crossing needs.
_SHOW_BATCH = 100
# A function at least this share of whose current lines were added within the
# last RECENT_DAYS is new code: too young for a low fix count to mean anything.
RECENT_DAYS = 90
RECENT_SHARE = 0.5


def _git_prefix(project_root: Path, timeout: int = 10) -> str | None:
    """project_root's path inside its repository ("" at the top), or None."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-prefix"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip()


def _history_commits(
    project_root: Path,
    since: str,
    paths: list[str],
    key_types: dict[str, str] | None = None,
    recent_cutoff: datetime | None = None,
    timeout: int = 120,
) -> list[dict] | None:
    """Commits since `since` that touch `paths` and matter to the crossing.

    Those are the fix commits, plus every commit at or after `recent_cutoff`
    (any type), each with its parsed diff: {"sha", "date", "is_fix",
    "recent", "diffs"}. The subjects are listed first, without patches, and
    only the commits kept are diffed.

    Returns None when history is unavailable (not a repo, git missing, a
    timeout) so the caller can say so rather than reporting zero fix commits
    everywhere -- which would mark every hotspot `settled` and invert the
    ranking exactly as the plain complexity score does.

    A commit is a fix by analyze_history's classifier (bracketed tags,
    Conventional Commits types, word-boundary keywords). The substring test
    used before counted "prefixes" as a fix.
    """
    listing = _git(
        [
            "log",
            f"--since={since}",
            "--no-merges",
            "--format=%H%x00%cI%x00%s",
            "--",
            *paths,
        ],
        project_root,
        timeout,
    )
    if listing is None:
        return None
    commits: list[dict] = []
    for line in listing.splitlines():
        sha, cdate, subject = (line.split("\x00") + ["", ""])[:3]
        if not sha:
            continue
        is_fix = classify_commit(subject, key_types) == "fix"
        recent = False
        if recent_cutoff is not None:
            try:
                recent = datetime.fromisoformat(cdate) >= recent_cutoff
            except ValueError:
                recent = False
        if is_fix or recent:
            commits.append(
                {"sha": sha, "date": cdate, "is_fix": is_fix, "recent": recent}
            )

    by_sha: dict[str, list[dict]] = {}
    for batch_start in range(0, len(commits), _SHOW_BATCH):
        batch = [c["sha"] for c in commits[batch_start : batch_start + _SHOW_BATCH]]
        out = _git(
            [
                "-c",
                "core.quotepath=off",
                "show",
                "--format=%x00%H",
                *DIFF_OPTIONS,
                *batch,
                "--",
                *paths,
            ],
            project_root,
            timeout,
        )
        if out is None:
            return None
        sha = ""
        body: list[str] = []
        for line in out.splitlines():
            if line.startswith("\x00"):
                if sha:
                    by_sha[sha] = parse_unified_diff(body)
                sha, body = line[1:].strip(), []
            else:
                body.append(line)
        if sha:
            by_sha[sha] = parse_unified_diff(body)
    for commit in commits:
        commit["diffs"] = by_sha.get(commit["sha"], [])
    return commits


def _git(args: list[str], cwd: Path, timeout: int) -> str | None:
    """stdout of a git command, or None if it could not run or failed."""
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout


def _fix_commit_diffs(
    project_root: Path,
    since: str,
    paths: list[str],
    key_types: dict[str, str] | None = None,
    timeout: int = 120,
) -> list[tuple[str, list[dict]]] | None:
    """The fix commits since `since` that touch `paths`, with their parsed diffs."""
    commits = _history_commits(project_root, since, paths, key_types, None, timeout)
    if commits is None:
        return None
    return [(c["sha"], c["diffs"]) for c in commits if c["is_fix"]]


# Two independent fix commits to the same function inside two years is a real
# signal on a codebase of any size. A median-derived threshold was tried first
# and is unstable: over a handful of hotspots the median lands on whichever
# value happens to sit in the middle, and the gate moves with it.
ACTIVE_RISK_FIXES = 2
HIGH_COMPLEXITY_SCORE = 8.0


def verdict_for(
    fix_commits: int, score: float, recent_share: float | None = None
) -> str:
    """Cross a complexity score with a fix count and recent rewriting.

    `active-risk` is gated on the FIX HISTORY ALONE, deliberately not on the
    complexity score as well. Requiring both inverts the ranking all over again:
    on coverage.py `sysmon_py_start` has the most fix commits of any hotspot (5)
    and a score of 7.5, so an `and score >= 8.0` clause labelled the single
    most-repaired function in the codebase `quiet`.

    Complexity decides `settled` vs `quiet`; history decides `active-risk`.
    `settled` is the actionable one -- it says DEPRIORITIZE, which is the
    opposite of what a complexity ranking alone would say.

    `recent-churn` comes before both: when at least RECENT_SHARE of the
    function's current lines were added in the last RECENT_DAYS, a low fix
    count only says the code is young. On a validation project a nested
    helper with the top score, almost entirely rewritten that quarter, was
    labelled `settled` (deprioritize), and it held a reproduced bug.
    `recent_share` is None when it was not measured.
    """
    if fix_commits >= ACTIVE_RISK_FIXES:
        return "active-risk"
    if recent_share is not None and recent_share >= RECENT_SHARE:
        return "recent-churn"
    if score >= HIGH_COMPLEXITY_SCORE:
        return "settled"
    return "quiet"


def cross_with_history(
    hotspots: list[dict],
    project_root: Path,
    since: str = "2 years ago",
    key_types: dict[str, str] | None = None,
    recent_since: datetime | None = None,
) -> dict:
    """Annotate hotspots with fix density and a verdict.

    This encodes the session's sharpest calibration lesson. On coverage.py,
    churn and complexity were **inverted**: `pytracer._trace` scores 10.0 with 2
    fix commits in two years, while `sysmon.py` scores 7.5 with 11. High
    complexity marked *settled* code, so complexity-driven triage pointed at
    exactly the wrong files. Crossing the two is emitted as a first-class output
    rather than left to the reader, so the crossing cannot be forgotten.

    A fix is credited to a hotspot when its hunks fell inside the function OF
    THE SAME QUALIFIED NAME in that commit's own version of the file, matched
    by full path. Intersecting old line numbers with today's spans, keyed by
    basename, reported a function with two fix commits as `quiet` with 0
    fixes on a validation project.
    """
    if recent_since is None:
        recent_since = datetime.now().astimezone() - timedelta(days=RECENT_DAYS)
    prefix = _git_prefix(project_root)
    paths = sorted({h["qualified_name"].split("::")[0] for h in hotspots})
    history = (
        _history_commits(project_root, since, paths, key_types, recent_since)
        if prefix is not None and paths
        else None
    )
    if history is None:
        for h in hotspots:
            h["fix_commits_2y"] = None
            h["fix_density"] = None
            h["recent_commits"] = None
            h["lines_added_recent"] = None
            h["lines_changed_recent"] = None
            h["recent_share"] = None
            h["verdict"] = "unknown"
        return {
            "history_available": False,
            "note": (
                "no usable git history (not a repo, git unavailable, or a "
                "shallow clone). Every verdict is `unknown`: treating a missing "
                "history as zero fix commits would mark every hotspot `settled` "
                "and reproduce exactly the inverted ranking this crossing exists "
                "to correct."
            ),
        }

    wanted = {prefix + p: p for p in paths}
    fixes: dict[tuple[str, str], set[str]] = {}
    unattributed: dict[str, set[str]] = {}
    # (file, qualname) -> [recent commits, lines added, lines removed]
    recent: dict[tuple[str, str], list] = {}
    recent_unattributed: dict[str, set[str]] = {}
    with BlobFunctionIndex(project_root, max_parses=MAX_HISTORY_PARSES) as index:
        for commit in history:
            sha = commit["sha"]
            for file_diff in commit["diffs"]:
                rel = wanted.get(file_diff["path"])
                if rel is None:
                    continue
                touched = attribute_file_diff(file_diff, index)
                if touched is None:
                    if commit["is_fix"]:
                        unattributed.setdefault(rel, set()).add(sha)
                    if commit["recent"]:
                        recent_unattributed.setdefault(rel, set()).add(sha)
                    continue
                for qual, (added, removed) in touched.items():
                    if commit["is_fix"]:
                        fixes.setdefault((rel, qual), set()).add(sha)
                    if commit["recent"]:
                        entry = recent.setdefault((rel, qual), [set(), 0, 0])
                        entry[0].add(sha)
                        entry[1] += added
                        entry[2] += removed
        parses, cap_reached = index.parses, index.exhausted

    for h in hotspots:
        rel, _, name = h["qualified_name"].partition("::")
        shas = fixes.get((rel, name), set())
        length = max(h["line_end"] - h["line_start"] + 1, 1)
        h["fix_commits_2y"] = len(shas)
        h["fix_density"] = round(len(shas) / length, 4)
        missing = len(unattributed.get(rel, ()))
        if missing:
            h["fix_commits_unattributed"] = missing
        r_shas, r_added, r_removed = recent.get((rel, name), (set(), 0, 0))
        h["recent_commits"] = len(r_shas)
        h["lines_added_recent"] = r_added
        h["lines_changed_recent"] = r_added + r_removed
        # Share of the function's current span added in the recent window.
        h["recent_share"] = round(min(r_added / length, 1.0), 2)

    ranked = sorted(hotspots, key=lambda h: -(h["fix_commits_2y"] or 0))
    for rank, h in enumerate(ranked, 1):
        h["risk_rank"] = rank

    fix_counts = sorted(h["fix_commits_2y"] for h in hotspots)
    median = fix_counts[len(fix_counts) // 2] if fix_counts else 0
    for h in hotspots:
        rel = h["qualified_name"].partition("::")[0]
        missing = h.get("fix_commits_unattributed", 0)
        verdict = verdict_for(h["fix_commits_2y"], h["score"], h["recent_share"])
        recent_missing = len(recent_unattributed.get(rel, ()))
        if h["fix_commits_2y"] < ACTIVE_RISK_FIXES <= h["fix_commits_2y"] + missing:
            # The unattributed fixes could tip it into active-risk: `quiet` or
            # `settled` here would be an unmeasured value posing as a clean one.
            h["verdict"] = "unknown"
            h["verdict_reason"] = (
                f"{missing} fix commit(s) to this file could not be attributed "
                "to a function"
            )
        elif verdict in ("settled", "quiet") and recent_missing:
            # Unattributed recent commits could make it `recent-churn`.
            h["verdict"] = "unknown"
            h["verdict_reason"] = (
                f"{recent_missing} recent commit(s) to this file could not be "
                "attributed to a function"
            )
        else:
            h["verdict"] = verdict

    all_unattributed: set[str] = set()
    for shas in unattributed.values():
        all_unattributed |= shas
    return {
        "history_available": True,
        "since": since,
        "fix_commits_seen": sum(1 for c in history if c["is_fix"]),
        "median_fix_commits_per_hotspot": median,
        "active_risk_threshold": ACTIVE_RISK_FIXES,
        "attribution": (
            "per commit: each fix's hunks are mapped onto the functions of "
            "that commit's own version of the file and matched by qualified "
            "name and full path. A function renamed, or moved to another "
            "file, within the window loses its earlier fixes."
        ),
        "file_versions_parsed": parses,
        "fix_commits_unattributed": len(all_unattributed),
        "parse_cap_reached": cap_reached,
        "recent_window": {
            "since": recent_since.isoformat(timespec="seconds"),
            "days": RECENT_DAYS,
            "recent_churn_share": RECENT_SHARE,
            "commits_seen": sum(1 for c in history if c["recent"]),
        },
        "note": (
            "`settled` means high complexity and low fix density -- deprioritize "
            "it, and say so. Complexity alone ranked coverage.py's files in "
            "almost the reverse of their fix history. `recent-churn` means at "
            "least half of the function's current lines were added inside the "
            "recent window: a low fix count there says the code is young, not "
            "that it is safe."
        ),
    }


def main() -> None:
    from scan_common import exit_on_help

    exit_on_help(sys.argv[1:], __doc__)
    max_files = 0  # 0 = no limit
    positional: list[str] = []
    argv = sys.argv[1:]
    i = 0
    key_types: dict[str, str] = {}
    while i < len(argv):
        if argv[i] == "--max-files" and i + 1 < len(argv):
            max_files = int(argv[i + 1])
            i += 2
        elif argv[i] == "--key-type" and i + 1 < len(argv):
            # Same meaning as analyze_history's flag: tracker-key prefixes
            # that mark a commit's type, e.g. B=fix.
            try:
                key_types.update(parse_key_types(argv[i + 1]))
            except ValueError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                sys.exit(2)
            i += 2
        elif argv[i].startswith("--"):
            i += 1
        else:
            positional.append(argv[i])
            i += 1
    target = Path(positional[0]) if positional else Path(".")
    target = target.resolve()

    project_root = find_project_root(target)
    scan_root = target if target.is_dir() else project_root
    all_files = sorted(discover_python_files(scan_root))
    files_total = len(all_files)
    if max_files > 0 and files_total > max_files:
        all_files = all_files[:max_files]

    file_analyses = [analyze_file(f, project_root) for f in all_files]

    # Collect all functions and rank by score.
    all_functions = []
    for fa in file_analyses:
        for func in fa["functions"]:
            all_functions.append(func)

    all_functions.sort(key=lambda f: -f["score"])

    # Summary statistics.
    source_funcs = [f for f in all_functions if not f["is_test"]]
    test_funcs = [f for f in all_functions if f["is_test"]]

    hotspots = [f for f in source_funcs if f["score"] >= 5.0]
    history_meta = cross_with_history(hotspots[:30], project_root, key_types=key_types)

    output = {
        "project_root": str(project_root),
        "scan_root": str(scan_root),
        "files_total": files_total,
        "files_analyzed": len(all_files),
        "files_capped": max_files > 0 and files_total > max_files,
        "summary": {
            "total_functions": len(all_functions),
            "source_functions": len(source_funcs),
            "test_functions": len(test_funcs),
            "hotspots_score_5_plus": len(hotspots),
            "hotspots_score_8_plus": len([f for f in hotspots if f["score"] >= 8.0]),
            "avg_score_source": (
                round(
                    sum(f["score"] for f in source_funcs) / len(source_funcs),
                    1,
                )
                if source_funcs
                else 0
            ),
            "avg_cognitive_complexity_source": (
                round(
                    sum(f["metrics"]["cognitive_complexity"] for f in source_funcs)
                    / len(source_funcs),
                    1,
                )
                if source_funcs
                else 0
            ),
        },
        "hotspots": hotspots[:30],  # Top 30 hotspots.
        # 4.6: the crossing is a first-class output, not the reader's job.
        "history_crossing": history_meta,
        "files": file_analyses,
    }

    json.dump(output, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
