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

It also reports `clones`: groups of near-identical statement windows
(normalized-AST hashing), within and across functions and files.

Usage:
    python measure_complexity.py [path] [--max-files N] [--key-type SPEC]
                                 [--no-clones]

Options:
    --max-files N     Analyze at most N files
    --key-type SPEC   Tracker-key prefixes (or single keys) that mark a commit's
                      type, as in analyze_history.py (e.g. B=fix)
    --no-clones       Skip near-duplicate block detection
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


_TEST_DIRS = frozenset({"tests", "test"})


def is_test_path(rel: str) -> bool:
    """True for a file test runners collect: under tests/ or test/, or named
    test_*.py, *_test.py, tests.py or conftest.py."""
    parts = Path(rel).parts
    name = parts[-1] if parts else ""
    return (
        any(part in _TEST_DIRS for part in parts[:-1])
        or name.startswith("test_")
        or name.endswith("_test.py")
        or name in ("tests.py", "conftest.py")
    )


def _sets_test_false(cls: ast.ClassDef) -> bool:
    """`__test__ = False` in the class body: test runners must not collect it."""
    for stmt in cls.body:
        if isinstance(stmt, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__test__" for t in stmt.targets
        ):
            return isinstance(stmt.value, ast.Constant) and stmt.value.value is False
    return False


def _is_test_case_class(cls: ast.ClassDef) -> bool:
    """A unittest-style class (a base named *TestCase) that is not opted out."""
    for base in cls.bases:
        name = base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", "")
        if name.endswith("TestCase"):
            return not _sets_test_false(cls)
    return False


def _test_classes(tree: ast.AST) -> set[str]:
    """Qualified names (as iter_qualified_defs builds them) of TestCase classes."""
    found: set[str] = set()
    stack: list[tuple[ast.AST, str]] = [(tree, "")]
    while stack:
        node, prefix = stack.pop()
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                qual = prefix + child.name
                if _is_test_case_class(child):
                    found.add(qual)
                stack.append((child, qual + "."))
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                stack.append((child, prefix + child.name + "."))
            else:
                stack.append((child, prefix))
    return found


def analyze_file(filepath: Path, project_root: Path) -> dict:
    """Analyze all functions/methods in a file.

    `is_test` is decided by the file's path (tests/, test_*.py, *_test.py,
    conftest.py), or by an enclosing unittest-style TestCase class that does
    not set `__test__ = False`. Deciding by name made every method of a
    source class whose name starts with `Test`, and every `tests` property,
    test code, so they vanished from the averages and the hotspots.
    """
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
    test_file = is_test_path(rel)
    test_classes = set() if test_file else _test_classes(tree)

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
        in_test_class = any(
            ".".join(segments[: i + 1]) in test_classes for i in range(len(segments))
        )
        return {
            "name": qualname,
            "qualified_name": f"{rel}::{qualname}",
            "file": rel,
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
            "is_test": test_file or in_test_class,
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
    if prefix is not None and not paths:
        return {
            "history_available": True,
            "since": since,
            "fix_commits_seen": None,
            "fix_commits_seen_scope": "hotspot files only",
            "note": "no hotspots, so nothing to cross; see top_fixed_functions",
        }
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
        # Fix commits touching the HOTSPOTS' files only; for the whole scan
        # root see top_fixed_functions.
        "fix_commits_seen": sum(1 for c in history if c["is_fix"]),
        "fix_commits_seen_scope": "hotspot files only",
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


# Parse budget for the scope-wide fix count (every source file, not only the
# hotspots' files).
MAX_SCOPE_PARSES = 3000
TOP_FIXED = 15


def top_fixed_functions(
    functions: list[dict],
    project_root: Path,
    scan_root: Path,
    since: str = "2 years ago",
    key_types: dict[str, str] | None = None,
    limit: int = TOP_FIXED,
) -> dict:
    """The source functions with the most fix commits, whatever their score.

    The hotspot crossing only reads the hotspots' files, so on a structurally
    simple codebase -- where the fix-dense functions all score below the
    hotspot threshold -- it reported no fix commits although the window held
    fixes to functions in other files. This counts over every source file in
    the scan root.
    """
    prefix = _git_prefix(project_root)
    try:
        scope = scan_root.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError:
        scope = "."
    history = (
        _history_commits(project_root, since, [scope or "."], key_types)
        if prefix is not None and functions
        else None
    )
    if history is None:
        return {
            "available": False,
            "reason": "no usable git history",
            "functions": [],
        }
    by_key = {(f["file"], f["name"]): f for f in functions}
    wanted = {prefix + rel: rel for rel in {f["file"] for f in functions}}
    fixes: dict[tuple[str, str], set[str]] = {}
    unattributed: set[str] = set()
    with BlobFunctionIndex(project_root, max_parses=MAX_SCOPE_PARSES) as index:
        for commit in history:
            for file_diff in commit["diffs"]:
                rel = wanted.get(file_diff["path"])
                if rel is None:
                    continue
                touched = attribute_file_diff(file_diff, index)
                if touched is None:
                    unattributed.add(commit["sha"])
                    continue
                for qual in touched:
                    if (rel, qual) in by_key:
                        fixes.setdefault((rel, qual), set()).add(commit["sha"])
        cap_reached = index.exhausted
    ranked = sorted(
        fixes.items(),
        key=lambda item: (-len(item[1]), -by_key[item[0]]["score"], item[0]),
    )
    return {
        "available": True,
        "scope": "every source function under the scan root",
        "since": since,
        "fix_commits_seen": len(history),
        "fix_commits_unattributed": len(unattributed),
        "parse_cap_reached": cap_reached,
        "functions_with_fixes": len(fixes),
        "functions": [
            {
                "qualified_name": by_key[key]["qualified_name"],
                "file": key[0],
                "name": key[1],
                "line_start": by_key[key]["line_start"],
                "fix_commits_2y": len(shas),
                "score": by_key[key]["score"],
                "is_hotspot": by_key[key]["score"] >= 5.0,
            }
            for key, shas in ranked[:limit]
        ],
    }


# --- Near-duplicate blocks -------------------------------------------------
#
# Two copies of one step that later drift apart are a strong bug signal that
# per-function metrics cannot show: on a validation project a long literal
# was AST-identical in two functions and a stop sequence was written out at
# each exit of one job runner. A window of consecutive statements is hashed on
# its normalized AST: layout, comments and docstrings ignored, and names BOUND
# inside the window (locals, parameters, loop and `as` targets) renamed by
# order of appearance. Free names (functions called, modules, globals),
# attribute names and literals are kept, so two blocks that call different
# functions or use different constants do not match.

CLONE_MIN_NODES = 40
CLONE_MIN_LINES = 6
MAX_CLONE_GROUPS = 30
_NAME_MARK = "\x01"
_SKIP_FIELDS = frozenset({"ctx", "type_comment", "kind"})


class _StmtInfo:
    __slots__ = ("bound", "end", "nodes", "start", "tokens")

    def __init__(self, stmt: ast.stmt) -> None:
        tokens: list[str] = []
        bound: set[str] = set()
        nodes = 0
        todo: list[object] = [stmt]
        while todo:
            item = todo.pop()
            if isinstance(item, str):
                tokens.append(item)
                continue
            if not isinstance(item, ast.AST):
                tokens.append(repr(item))
                continue
            nodes += 1
            tokens.append(type(item).__name__)
            if isinstance(item, ast.Name):
                if not isinstance(item.ctx, ast.Load):
                    bound.add(item.id)
                tokens.append(_NAME_MARK + item.id)
                continue
            if isinstance(item, ast.arg):
                bound.add(item.arg)
                tokens.append(_NAME_MARK + item.arg)
                continue
            if isinstance(item, ast.ExceptHandler) and item.name:
                bound.add(item.name)
            pending: list[object] = []
            for field, value in ast.iter_fields(item):
                if field in _SKIP_FIELDS:
                    continue
                if isinstance(item, ast.ExceptHandler) and field == "name" and value:
                    pending.append(_NAME_MARK + value)
                    continue
                if isinstance(value, list):
                    pending.append("[")
                    pending.extend(value)
                    pending.append("]")
                else:
                    pending.append(value)
            todo.extend(reversed(pending))
        self.tokens = tokens
        self.bound = bound
        self.nodes = nodes
        self.start = def_start_line(stmt)
        self.end = getattr(stmt, "end_lineno", None) or stmt.lineno


def _window_key(stmts: list[_StmtInfo], exact: bool = False) -> tuple:
    bound: set[str] = set()
    for info in stmts:
        bound |= info.bound
    mapping: dict[str, str] = {}
    out: list[str] = []
    for info in stmts:
        for token in info.tokens:
            if token.startswith(_NAME_MARK) and not exact:
                name = token[1:]
                if name in bound:
                    token = mapping.setdefault(name, f"{_NAME_MARK}v{len(mapping)}")
            out.append(token)
        out.append(";")
    return tuple(out)


def _is_docstring(stmt: ast.stmt) -> bool:
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and isinstance(stmt.value.value, str)
    )


def _statement_runs(tree: ast.AST) -> list[list[ast.stmt]]:
    """Runs of consecutive plain statements in every block of the file.

    A def, a class or an import ends a run (each def's body is its own
    block), and docstrings are left out.
    """
    runs: list[list[ast.stmt]] = []
    for node in ast.walk(tree):
        for _field, value in ast.iter_fields(node):
            if not (
                isinstance(value, list) and value and isinstance(value[0], ast.stmt)
            ):
                continue
            run: list[ast.stmt] = []
            for stmt in value:
                if isinstance(
                    stmt,
                    (
                        ast.FunctionDef,
                        ast.AsyncFunctionDef,
                        ast.ClassDef,
                        ast.Import,
                        ast.ImportFrom,
                    ),
                ) or _is_docstring(stmt):
                    if run:
                        runs.append(run)
                    run = []
                    continue
                run.append(stmt)
            if run:
                runs.append(run)
    return runs


def _enclosing_function(spans: list[tuple[str, int, int]], line: int) -> str | None:
    best: tuple[str, int, int] | None = None
    for qual, start, end in spans:
        if start <= line <= end and (best is None or start >= best[1]):
            best = (qual, start, end)
    return best[0] if best else None


def find_clones(
    files: list[tuple[str, ast.AST]],
    min_nodes: int = CLONE_MIN_NODES,
    min_lines: int = CLONE_MIN_LINES,
    max_groups: int = MAX_CLONE_GROUPS,
) -> dict:
    """Groups of near-identical statement windows, across files and functions.

    `files` is [(relative path, parsed module)]. Each window is the shortest
    run of consecutive statements, from each starting statement, holding at
    least `min_nodes` AST nodes; matching windows are then grown statement by
    statement while every copy still matches. A group is reported when each
    copy spans at least `min_lines` lines and it is not wholly inside a larger
    reported group.
    """
    runs: list[tuple[str, list[_StmtInfo]]] = []
    spans_by_file: dict[str, list[tuple[str, int, int]]] = {}
    for rel, tree in files:
        spans_by_file[rel] = [
            (
                qual,
                def_start_line(node),
                getattr(node, "end_lineno", None) or node.lineno,
            )
            for qual, node, _ in iter_qualified_defs(tree)
        ]
        for run in _statement_runs(tree):
            runs.append((rel, [_StmtInfo(stmt) for stmt in run]))

    # Minimal windows, keyed by their normalized AST.
    by_key: dict[tuple, list[tuple[int, int, int]]] = {}
    for run_id, (_rel, infos) in enumerate(runs):
        for i in range(len(infos)):
            total, j = 0, i
            while j < len(infos) and total < min_nodes:
                total += infos[j].nodes
                j += 1
            if total < min_nodes:
                break
            by_key.setdefault(_window_key(infos[i:j]), []).append((run_id, i, j))

    groups: list[dict] = []
    for occurrences in by_key.values():
        # Drop copies overlapping an earlier copy in the same run.
        kept: list[list[int]] = []
        for run_id, i, j in occurrences:
            if any(r == run_id and i < kj and ki < j for r, ki, kj in kept):
                continue
            kept.append([run_id, i, j])
        if len(kept) < 2:
            continue
        # Grow every copy while all of them still match.
        while True:
            grown = []
            for run_id, i, j in kept:
                infos = runs[run_id][1]
                if j >= len(infos):
                    break
                grown.append((run_id, i, j + 1))
            if len(grown) != len(kept):
                break
            overlap = any(
                a[0] == b[0] and a[1] < b[2] and b[1] < a[2]
                for n, a in enumerate(grown)
                for b in grown[n + 1 :]
            )
            keys = {_window_key(runs[r][1][i:j]) for r, i, j in grown}
            if overlap or len(keys) != 1:
                break
            kept = [list(g) for g in grown]
        locations = []
        for run_id, i, j in kept:
            rel, infos = runs[run_id]
            start, end = infos[i].start, infos[j - 1].end
            locations.append(
                {
                    "file": rel,
                    "function": _enclosing_function(spans_by_file[rel], start),
                    "line_start": start,
                    "line_end": end,
                }
            )
        if (
            min(loc["line_end"] - loc["line_start"] + 1 for loc in locations)
            < min_lines
        ):
            continue
        first_run, first_i, first_j = kept[0]
        sample = runs[first_run][1][first_i:first_j]
        exact = {_window_key(runs[r][1][i:j], exact=True) for r, i, j in kept}
        groups.append(
            {
                "kind": "exact" if len(exact) == 1 else "renamed",
                "statements": first_j - first_i,
                "nodes": sum(info.nodes for info in sample),
                "lines": max(
                    loc["line_end"] - loc["line_start"] + 1 for loc in locations
                ),
                "copies": len(locations),
                "locations": sorted(
                    locations, key=lambda loc: (loc["file"], loc["line_start"])
                ),
            }
        )

    # Largest first; a group wholly inside an accepted one is the same clone.
    groups.sort(key=lambda g: (-g["nodes"] * (g["copies"] - 1), -g["nodes"]))
    accepted: list[dict] = []
    for group in groups:
        inside = all(
            any(
                other["file"] == loc["file"]
                and other["line_start"] <= loc["line_start"]
                and loc["line_end"] <= other["line_end"]
                for kept_group in accepted
                for other in kept_group["locations"]
            )
            for loc in group["locations"]
        )
        if not inside:
            accepted.append(group)
    return {
        "method": (
            "windows of consecutive statements with at least "
            f"{min_nodes} AST nodes, compared on the normalized AST (layout, "
            "comments and docstrings ignored; names bound inside the window "
            "renamed; called names, attributes and literals kept), grown while "
            "every copy matches. `exact`: identical apart from layout; "
            "`renamed`: identical up to local names. Test files are skipped."
        ),
        "min_nodes": min_nodes,
        "min_lines": min_lines,
        "groups_total": len(accepted),
        "groups": accepted[:max_groups],
        "capped": len(accepted) > max_groups,
    }


def main() -> None:
    from scan_common import exit_on_help

    exit_on_help(sys.argv[1:], __doc__)
    max_files = 0  # 0 = no limit
    positional: list[str] = []
    argv = sys.argv[1:]
    i = 0
    key_types: dict[str, str] = {}
    no_clones = False
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
        elif argv[i] == "--no-clones":
            no_clones = True
            i += 1
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
    clone_inputs: list[tuple[str, ast.AST]] = []
    if not no_clones:
        for path, analysis in zip(all_files, file_analyses):
            if analysis["parse_error"] or is_test_path(analysis["file"]):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            except (SyntaxError, ValueError):
                continue
            clone_inputs.append((analysis["file"], tree))

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
    history_meta["top_fixed_functions"] = top_fixed_functions(
        source_funcs, project_root, scan_root, key_types=key_types
    )

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
        "clones": (
            find_clones(clone_inputs)
            if not no_clones
            else {"skipped": True, "reason": "--no-clones", "groups": []}
        ),
        "files": file_analyses,
    }

    json.dump(output, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
