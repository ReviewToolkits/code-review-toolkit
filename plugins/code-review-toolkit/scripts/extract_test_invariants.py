#!/usr/bin/env python3
"""Extract test invariants for propagation analysis.

Parses test files to extract assertions, classify the invariants they encode,
identify the source functions they test, and find structurally similar functions
that should satisfy the same invariants but may not be tested.

Usage:
    python extract_test_invariants.py [path] [--max-files N] [--with-git]
"""

import ast
import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scan_common import (  # noqa: E402
    discover_python_files,
    find_project_root,
)
from correlate_tests import (  # noqa: E402
    build_module_index,
    find_external_tests,
    module_name,
)


# ---------------------------------------------------------------------------
# Project discovery (adapted from correlate_tests.py)
# ---------------------------------------------------------------------------

def classify_files(
    files: list[Path], project_root: Path
) -> tuple[list[Path], list[Path]]:
    """Split files into (source, test) lists."""
    source: list[Path] = []
    test: list[Path] = []
    for f in files:
        rel = f.relative_to(project_root)
        parts = rel.parts
        is_test = (
            f.name.startswith("test_")
            or f.name.endswith("_test.py")
            or "test" in parts[:-1]
            or "tests" in parts[:-1]
        )
        if is_test:
            test.append(f)
        elif f.name not in ("setup.py", "conftest.py"):
            source.append(f)
    return source, test


# ---------------------------------------------------------------------------
# Assertion extraction
# ---------------------------------------------------------------------------

# Assertion method → invariant type mapping
_ASSERT_INVARIANT_MAP: dict[str, str] = {
    "assertRaises": "error_condition",
    "assertRaisesRegex": "error_condition",
    "assertWarns": "warning_condition",
    "assertWarnsRegex": "warning_condition",
    "assertEqual": "equality",
    "assertNotEqual": "inequality",
    "assertTrue": "truthiness",
    "assertFalse": "falsiness",
    "assertIs": "identity",
    "assertIsNot": "non_identity",
    "assertIsNone": "nullability",
    "assertIsNotNone": "non_nullability",
    "assertIn": "membership",
    "assertNotIn": "non_membership",
    "assertIsInstance": "type_check",
    "assertNotIsInstance": "type_exclusion",
    "assertGreater": "ordering",
    "assertGreaterEqual": "ordering",
    "assertLess": "ordering",
    "assertLessEqual": "ordering",
    "assertAlmostEqual": "approximate_equality",
    "assertRegex": "pattern_match",
    "assertNotRegex": "pattern_exclusion",
    "assertCountEqual": "collection_equality",
    "assertSequenceEqual": "sequence_equality",
    "assertListEqual": "sequence_equality",
    "assertTupleEqual": "sequence_equality",
    "assertSetEqual": "set_equality",
    "assertDictEqual": "dict_equality",
}

# pytest patterns
_PYTEST_RAISES_NAMES = {"raises", "warns", "deprecated_call"}

# High-signal words in a test's (or its file's) name, matched as prefixes of
# underscore-separated words: `race` matches `races` but not `trace`.
_HIGH_SIGNAL_WORDS = (
    "error", "invalid", "empty", "none", "null", "negative", "overflow",
    "underflow", "boundary", "edge", "corner", "fail", "crash", "corrupt",
    "malform", "bad", "broken", "cleanup", "close", "teardown", "shutdown",
    "concurren", "thread", "async", "race", "cancel", "timeout", "retr",
    "recover", "rollback", "deadlock", "lock", "leak", "reject",
)
# The subset naming failure modes that tests rarely cover and reviews rarely
# find unaided (races, lost cancellations, partial rollbacks): within the
# error/boundary tier these are selected first.
_STRONG_SIGNAL_WORDS = (
    "concurren", "thread", "race", "cancel", "timeout", "retr", "recover",
    "rollback", "deadlock", "lock", "leak", "crash", "corrupt",
)


def _get_call_name(node: ast.expr) -> str | None:
    """Extract the function/method name from a Call node's func."""
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def extract_assertions(func_node: ast.FunctionDef) -> list[dict]:
    """Extract assertion calls and classify them from a test function AST."""
    assertions: list[dict] = []

    for node in ast.walk(func_node):
        if not isinstance(node, ast.Call):
            continue

        call_name = _get_call_name(node.func)
        if call_name is None:
            continue

        # unittest-style: self.assertXxx(...)
        if call_name in _ASSERT_INVARIANT_MAP:
            inv_type = _ASSERT_INVARIANT_MAP[call_name]
            # For assertRaises, extract the exception type
            detail = ""
            if call_name in ("assertRaises", "assertRaisesRegex") and node.args:
                exc_arg = node.args[0]
                if isinstance(exc_arg, ast.Name):
                    detail = exc_arg.id
                elif isinstance(exc_arg, ast.Attribute):
                    detail = exc_arg.attr
            assertions.append({
                "method": call_name,
                "invariant_type": inv_type,
                "line": node.lineno,
                "detail": detail,
                "is_implementation_detail": False,
            })

        # pytest.raises / pytest.warns
        elif call_name in _PYTEST_RAISES_NAMES:
            exc_detail = ""
            if node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Name):
                    exc_detail = arg.id
                elif isinstance(arg, ast.Attribute):
                    exc_detail = arg.attr
            assertions.append({
                "method": f"pytest.{call_name}",
                "invariant_type": "error_condition" if call_name == "raises" else "warning_condition",
                "line": node.lineno,
                "detail": exc_detail,
                "is_implementation_detail": False,
            })

        # Plain assert statements are captured too (via ast.Assert, not Call)
        # Mock assertions are implementation details
        elif call_name in (
            "assert_called_with", "assert_called_once_with",
            "assert_called", "assert_called_once",
            "assert_not_called", "assert_any_call",
        ):
            assertions.append({
                "method": call_name,
                "invariant_type": "mock_interaction",
                "line": node.lineno,
                "detail": "",
                "is_implementation_detail": True,
            })

    # Also capture bare assert statements
    for node in ast.walk(func_node):
        if isinstance(node, ast.Assert):
            assertions.append({
                "method": "assert",
                "invariant_type": "general",
                "line": node.lineno,
                "detail": "",
                "is_implementation_detail": False,
            })

    return assertions


# ---------------------------------------------------------------------------
# Test → source mapping
# ---------------------------------------------------------------------------
#
# pytest suites rarely call the code under test as a bare imported function.
# They call methods on objects built from imported classes or by fixtures
# (`db = Database(...)`; `def test_x(db): db.query(...)`), and drive web
# apps through a test client (`client.post("/api/items/...")`).
# The mapper therefore resolves calls through the test file's imports, typed
# local variables, conftest fixtures and HTTP route decorators, and only
# then falls back to name matching.

_HTTP_VERBS = frozenset({"get", "post", "put", "patch", "delete", "head", "options"})
_ROUTE_DECORATORS = _HTTP_VERBS | {"route", "api_route", "websocket"}
# One-word names too generic to identify a function from a test name alone.
_GENERIC_TOKENS = frozenset({
    "get", "set", "run", "add", "new", "make", "main", "call", "init", "load",
    "save", "read", "write", "open", "close", "start", "stop", "test", "check",
    "update", "create", "delete", "list", "is", "has", "to", "from", "do",
})
_SKIP_CALLS = frozenset({
    "setUp", "tearDown", "patch", "MagicMock", "Mock", "AsyncMock",
    "skipTest", "skipIf", "skipUnless", "raises", "warns", "fixture",
    "parametrize", "approx", "subTest",
})


def _extract_imports(tree: ast.Module, package: str = "") -> dict[str, str]:
    """Map each name an import binds to the dotted path it refers to.

    `import a.b` binds `a` (to `a`); `import a.b as x` binds `x` to `a.b`;
    `from a import b as c` binds `c` to `a.b`. Relative imports are resolved
    against *package*.
    """
    imports: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    imports[alias.asname] = alias.name
                else:
                    first = alias.name.split(".")[0]
                    imports[first] = first
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".") if package else []
                if node.level > 1:
                    base = base[: len(base) - (node.level - 1)]
                module = ".".join(base + ([node.module] if node.module else []))
            else:
                module = node.module or ""
            for alias in node.names:
                if alias.name == "*":
                    continue
                name = alias.asname or alias.name
                imports[name] = f"{module}.{alias.name}" if module else alias.name
    return imports


def _dotted_chain(node: ast.expr) -> list[str] | None:
    """`a.b.c` as `["a", "b", "c"]`; None for anything but names/attributes."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return parts[::-1]


def _tokens(name: str) -> list[str]:
    return [t for t in name.lower().strip("_").split("_") if t]


def _path_segments(node: ast.expr) -> list[str | None] | None:
    """URL path segments of a str / f-string literal; None marks a wildcard."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        text = node.value
    elif isinstance(node, ast.JoinedStr):
        pieces = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                pieces.append(value.value)
            else:
                pieces.append("\0")
        text = "".join(pieces)
    else:
        return None
    if not text.startswith("/"):
        return None
    text = text.split("?", 1)[0]
    return [
        None if "\0" in seg else seg for seg in text.strip("/").split("/") if seg
    ]


def _template_segments(path: str) -> list[str | None]:
    """Segments of a route template; `{param}` / `<param>` become wildcards."""
    return [
        None if ("{" in seg or "<" in seg) else seg
        for seg in path.strip("/").split("/")
        if seg
    ]


class SourceIndex:
    """What a test may reach in the source tree, indexed for call resolution."""

    def __init__(self, source_files: list[Path], project_root: Path):
        self.project_root = project_root
        self.modules = build_module_index(source_files, project_root)
        self.functions: dict[tuple[str, str], int] = {}
        self.classes: dict[tuple[str, str], dict] = {}
        self.class_by_name: dict[str, list[tuple[str, str]]] = defaultdict(list)
        self.routes: list[dict] = []
        self.short_names: dict[str, list[tuple[str, str]]] = defaultdict(list)
        self._bindings: dict[str, dict[str, str]] = {}
        for path in source_files:
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            except (SyntaxError, ValueError):
                continue
            rel = str(path.relative_to(project_root))
            package = module_name(path, project_root)
            if path.name != "__init__.py":
                package = package.rpartition(".")[0]
            self._bindings[rel] = _extract_imports(tree, package)
            self._index_tree(tree, rel)

    def _index_tree(self, tree: ast.Module, rel: str) -> None:
        method_nodes: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                methods: dict[str, int] = {}
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        methods[child.name] = child.lineno
                        method_nodes.add(id(child))
                self.classes[(rel, node.name)] = {
                    "line": node.lineno,
                    "methods": methods,
                    "bases": [
                        chain[-1]
                        for chain in map(_dotted_chain, node.bases)
                        if chain
                    ],
                }
                self.class_by_name[node.name].append((rel, node.name))
                for name, line in methods.items():
                    self.functions[(rel, f"{node.name}.{name}")] = line
                    self.short_names[name].append((rel, f"{node.name}.{name}"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._index_route(node, rel)
                if id(node) not in method_nodes and (rel, node.name) not in self.functions:
                    self.functions[(rel, node.name)] = node.lineno
                    self.short_names[node.name].append((rel, node.name))

    def _index_route(self, node: ast.FunctionDef | ast.AsyncFunctionDef, rel: str):
        for dec in node.decorator_list:
            if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)):
                continue
            verb = dec.func.attr
            if verb not in _ROUTE_DECORATORS or not dec.args:
                continue
            path = dec.args[0]
            if not (isinstance(path, ast.Constant) and isinstance(path.value, str)):
                continue
            if not path.value.startswith("/"):
                continue
            if verb in _HTTP_VERBS:
                methods = {verb}
            else:
                methods = {"get"}
                for kw in dec.keywords:
                    if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
                        methods = {
                            e.value.lower()
                            for e in kw.value.elts
                            if isinstance(e, ast.Constant) and isinstance(e.value, str)
                        }
            self.routes.append({
                "file": rel,
                "line": node.lineno,
                "function": node.name,
                "methods": methods,
                "segments": _template_segments(path.value),
            })

    # -- resolution ---------------------------------------------------------

    def resolve_dotted(self, dotted: str, _depth: int = 0) -> tuple[str, str] | None:
        """`pkg.mod.Class.meth` → `(file, "Class.meth")`, following re-exports."""
        parts = dotted.split(".")
        for i in range(len(parts), 0, -1):
            files = self.modules.get(".".join(parts[:i]))
            if not files:
                continue
            rest = parts[i:]
            if not rest:
                return None
            rel = files[0]
            qual = ".".join(rest)
            if (rel, qual) in self.functions or (rel, rest[0]) in self.classes:
                return rel, qual
            # `from pkg import Thing` where pkg/__init__.py re-exports Thing.
            binding = self._bindings.get(rel, {}).get(rest[0])
            if binding and _depth < 3 and binding != dotted:
                return self.resolve_dotted(
                    ".".join([binding, *rest[1:]]), _depth + 1
                )
            return None
        return None

    def method(self, rel: str, cls: str, name: str, _seen=None) -> tuple[str, str] | None:
        """`(file, "Class.name")` for *name* on *cls* or the nearest base."""
        if (rel, f"{cls}.{name}") in self.functions:
            return rel, f"{cls}.{name}"
        seen = _seen or set()
        seen.add((rel, cls))
        info = self.classes.get((rel, cls))
        for base in info["bases"] if info else []:
            # Same file first, then a unique class of that name anywhere.
            owners = [(rel, base)] if (rel, base) in self.classes else (
                self.class_by_name.get(base, []) if len(self.class_by_name.get(base, [])) == 1 else []
            )
            for owner in owners:
                if owner in seen:
                    continue
                found = self.method(owner[0], owner[1], name, seen)
                if found:
                    return found
        return None

    def match_route(self, verb: str, segments: list[str | None]) -> dict | None:
        """The most specific route whose template is a suffix of the path."""
        best = None
        best_key = (0, 0)
        for route in self.routes:
            if verb not in route["methods"]:
                continue
            tmpl = route["segments"]
            if len(tmpl) > len(segments) or not tmpl:
                continue
            tail = segments[len(segments) - len(tmpl):]
            literal = 0
            for want, got in zip(tmpl, tail):
                if want is None or got is None:
                    continue
                if want != got:
                    break
                literal += 1
            else:
                key = (literal, len(tmpl))
                if literal and key > best_key:
                    best, best_key = route, key
        return best


class _TestFileContext:
    """Per-test-file resolution state: its imports and the fixtures it sees."""

    def __init__(self, path: Path, tree: ast.Module, index: SourceIndex,
                 fixtures: dict[str, tuple[str, str]]):
        self.index = index
        package = module_name(path, index.project_root).rpartition(".")[0]
        self.bindings = _extract_imports(tree, package)
        self.fixtures = fixtures
        self.local_functions = {
            n.name: n
            for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and not n.name.startswith("test")
        }
        # Classes this file imports: an untyped `x.meth()` may be one of theirs.
        self.imported_classes: list[tuple[str, str]] = []
        for dotted in self.bindings.values():
            hit = index.resolve_dotted(dotted)
            if hit and hit in index.classes:
                self.imported_classes.append(hit)
        self.imported_files = {
            hit[0]
            for hit in (index.resolve_dotted(d) for d in self.bindings.values())
            if hit
        } | {
            files[0]
            for d in self.bindings.values()
            if (files := index.modules.get(d))
        }

    def resolve_chain(self, chain: list[str]) -> tuple[str, str] | None:
        target = self.bindings.get(chain[0])
        if target is None:
            return None
        return self.index.resolve_dotted(".".join([target, *chain[1:]]))


def _class_of_call(call: ast.expr, ctx: _TestFileContext) -> tuple[str, str] | None:
    """The source class a call constructs, if it is one."""
    if not isinstance(call, ast.Call):
        return None
    chain = _dotted_chain(call.func)
    if not chain:
        return None
    hit = ctx.resolve_chain(chain)
    if hit and hit in ctx.index.classes:
        return hit
    return None


def collect_fixture_types(
    conftests: list[tuple[Path, ast.Module]], index: SourceIndex
) -> dict[str, tuple[str, str]]:
    """Map pytest fixture names to the source class they return or yield."""
    types: dict[str, tuple[str, str]] = {}
    for path, tree in conftests:
        ctx = _TestFileContext(path, tree, index, {})
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            name = node.name
            is_fixture = False
            for dec in node.decorator_list:
                target = dec.func if isinstance(dec, ast.Call) else dec
                chain = _dotted_chain(target)
                if chain and chain[-1] == "fixture":
                    is_fixture = True
                    if isinstance(dec, ast.Call):
                        for kw in dec.keywords:
                            if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                                name = str(kw.value.value)
            if not is_fixture:
                continue
            local: dict[str, tuple[str, str]] = {}
            for sub in ast.walk(node):
                if isinstance(sub, ast.Assign) and len(sub.targets) == 1:
                    cls = _class_of_call(sub.value, ctx)
                    if cls and isinstance(sub.targets[0], ast.Name):
                        local[sub.targets[0].id] = cls
                elif isinstance(sub, (ast.Return, ast.Yield)) and sub.value is not None:
                    cls = _class_of_call(sub.value, ctx)
                    if cls is None and isinstance(sub.value, ast.Name):
                        cls = local.get(sub.value.id)
                    if cls:
                        types[name] = cls
    return types


def _names_in(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


_RAISES_CONTEXTS = frozenset({
    "raises", "warns", "assertRaises", "assertRaisesRegex", "assertWarns",
    "assertWarnsRegex",
})


def _asserted_calls(func_node: ast.AST) -> set[int]:
    """ids of the Call nodes whose outcome the test checks.

    A call is checked when it sits inside an `assert` / `self.assertX(...)`,
    inside a `with pytest.raises(...)` / `self.assertRaises(...)` block, or
    when its result is bound to a name that (directly or through further
    assignments) reaches such a check: `r = client.post(...); body = r.json();
    assert body["ok"]`. A setup call (`cfg.set(...)`, creating the collection
    the test then queries) is not.
    """
    checked: set[int] = set()
    checked_names: set[str] = set()
    assigns: list[tuple[set[str], ast.expr]] = []

    def calls_in(node: ast.AST) -> set[int]:
        return {id(c) for c in ast.walk(node) if isinstance(c, ast.Call)}

    for node in ast.walk(func_node):
        if isinstance(node, ast.Assert):
            checked |= calls_in(node)
            checked_names |= _names_in(node.test)
        elif isinstance(node, ast.Call):
            chain = _dotted_chain(node.func)
            if chain and chain[-1].startswith("assert"):
                for arg in [*node.args, *(k.value for k in node.keywords)]:
                    checked |= calls_in(arg)
                    checked_names |= _names_in(arg)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                expr = item.context_expr
                chain = _dotted_chain(expr.func) if isinstance(expr, ast.Call) else None
                if chain and chain[-1] in _RAISES_CONTEXTS:
                    for stmt in node.body:
                        checked |= calls_in(stmt)
        if isinstance(node, ast.Assign):
            assigns.append((set().union(*map(_names_in, node.targets)), node.value))
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and node.value:
            assigns.append((_names_in(node.target), node.value))
    changed = True
    while changed:
        changed = False
        for targets, value in assigns:
            if targets & checked_names:
                new = _names_in(value) - checked_names
                if new:
                    checked_names |= new
                    changed = True
    for targets, value in assigns:
        if targets & checked_names:
            checked |= calls_in(value)
    return checked


def _call_candidates(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
    ctx: _TestFileContext,
    via_helper: bool = False,
    helper_asserted: bool = False,
) -> list[dict]:
    """Source functions a test body calls, in source order."""
    index = ctx.index
    var_types: dict[str, tuple[str, str]] = {}
    for arg in func_node.args.args + func_node.args.kwonlyargs:
        if arg.arg in ctx.fixtures:
            var_types[arg.arg] = ctx.fixtures[arg.arg]
    for node in ast.walk(func_node):
        targets: list[ast.expr] = []
        value = None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            targets, value = [node.optional_vars], node.context_expr
        cls = _class_of_call(value, ctx) if value is not None else None
        if cls:
            for target in targets:
                if isinstance(target, ast.Name):
                    var_types[target.id] = cls

    calls = sorted(
        (n for n in ast.walk(func_node) if isinstance(n, ast.Call)),
        key=lambda n: (n.lineno, n.col_offset),
    )
    checked = _asserted_calls(func_node)
    found: list[dict] = []

    def add(rel: str, qual: str, kind: str, line: int | None = None) -> None:
        if line is None:
            line = index.functions.get((rel, qual)) or index.classes.get(
                (rel, qual), {}
            ).get("line", 0)
        found.append({
            "function": qual, "file": rel, "line": line,
            "match_method": kind, "via_helper": via_helper,
            # The test checks this call's outcome (see _asserted_calls).
            # Through a helper: whether the test checks the helper's result.
            "result_asserted": helper_asserted if via_helper else id(call) in checked,
        })

    for call in calls:
        func = call.func
        chain = _dotted_chain(func)
        if chain and chain[-1] in _SKIP_CALLS or (
            chain and chain[-1].startswith("assert")
        ):
            continue
        # client.post("/api/...") → the route handler.
        if isinstance(func, ast.Attribute) and func.attr in _HTTP_VERBS and call.args:
            segments = _path_segments(call.args[0])
            if segments is not None:
                route = index.match_route(func.attr, segments)
                if route:
                    add(route["file"], route["function"], "http_route", route["line"])
                continue
        if not chain:
            continue
        # var.meth() on a variable or fixture of a known source class.
        if len(chain) == 2 and chain[0] in var_types:
            rel, cls = var_types[chain[0]]
            hit = index.method(rel, cls, chain[1])
            if hit:
                add(hit[0], hit[1], "method_call")
                continue
        hit = ctx.resolve_chain(chain)
        if hit:
            rel, qual = hit
            if (rel, qual) in index.classes:
                init = index.method(rel, qual, "__init__")
                add(*(init or hit), "constructor")
            elif (rel, qual) in index.functions:
                add(rel, qual, "method_call" if "." in qual else "imported_call")
            continue
        # A helper defined in the test file: look one level inside it.
        if len(chain) == 1 and chain[0] in ctx.local_functions and not via_helper:
            found.extend(_call_candidates(
                ctx.local_functions[chain[0]], ctx,
                via_helper=True, helper_asserted=id(call) in checked,
            ))
            continue
        # x.meth() on an untyped receiver, where meth belongs to a class the
        # file imports or a fixture provides.
        if len(chain) >= 2:
            owners = list(dict.fromkeys(ctx.imported_classes + list(var_types.values())))
            hits = [index.method(r, c, chain[-1]) for r, c in owners]
            hits = list(dict.fromkeys(h for h in hits if h))
            if len(hits) == 1:
                add(hits[0][0], hits[0][1], "method_name_match")
                continue
        # Last resort: a distinctive name defined exactly once in the source
        # (`cli.run_cli(...)` on a module a fixture reloaded).
        name = chain[-1]
        homes = index.short_names.get(name, [])
        if (
            len(homes) == 1
            and not name.startswith("__")
            and ("_" in name.strip("_") or len(name) >= 8)
            and name not in ctx.local_functions
        ):
            add(homes[0][0], homes[0][1], "unique_name_match")
    return found


def resolve_tested_function(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
    ctx: _TestFileContext,
) -> dict | None:
    """Identify the source function a test exercises.

    1. Among the source functions the test body calls (through imports, typed
       variables, fixtures, HTTP routes, one level of same-file helpers), the
       one the test's name mentions (`test_get_document_*` → `get_document`).
    2. Otherwise, preferring real calls over constructors: the call whose
       outcome the test asserts, then one sharing a distinctive word with the
       test name, the most often asserted, direct over helper calls, earliest.
    3. Otherwise the test-name convention (`test_foo_bar_*` → `foo_bar`),
       restricted to modules the test imports unless the name is specific.
    4. Otherwise the first bare call to a function defined exactly once.
    """
    index = ctx.index
    test_tokens = _tokens(func_node.name)
    if test_tokens and test_tokens[0] == "test":
        test_tokens = test_tokens[1:]

    def named_in_test(short: str) -> int:
        toks = _tokens(short)
        if not toks:
            return 0
        if len(toks) == 1 and (len(toks[0]) < 4 or toks[0] in _GENERIC_TOKENS):
            return 0
        n = len(toks)
        for i in range(len(test_tokens) - n + 1):
            if test_tokens[i:i + n] == toks:
                return n
        return 0

    def overlap(short: str) -> int:
        # Distinctive words shared with the test name (`*_on_upload` and
        # `upload_post`).
        return sum(
            1 for t in _tokens(short)
            if len(t) >= 4 and t not in _GENERIC_TOKENS and t in test_tokens
        )

    candidates = _call_candidates(func_node, ctx)
    if candidates:
        # How often the test checks each function's outcome: the code under
        # test is usually the one asserted on repeatedly.
        asserted_count: dict[tuple[str, str], int] = defaultdict(int)
        for c in candidates:
            if c["result_asserted"]:
                asserted_count[(c["file"], c["function"])] += 1

        def rank(item: tuple[int, dict]) -> tuple:
            i, c = item
            short = c["function"].rsplit(".", 1)[-1]
            return (
                named_in_test(short),
                # A constructor's result is asserted on whenever any method of
                # the object is; it is the subject only when nothing else is.
                c["match_method"] != "constructor",
                # Private helpers poked from a test are usually setup.
                not short.startswith("_"),
                c["result_asserted"],
                overlap(short),
                asserted_count[(c["file"], c["function"])],
                not c["via_helper"],
                -i,
            )

        best = max(enumerate(candidates), key=rank)[1]
        if named_in_test(best["function"].rsplit(".", 1)[-1]):
            return {**best, "match_method": "call_named_in_test",
                    "call_kind": best["match_method"]}
        return best

    # 3. Name convention: the longest leading run of test-name tokens that
    #    names a source function.
    for n in range(len(test_tokens), 0, -1):
        short = "_".join(test_tokens[:n])
        homes = index.short_names.get(short, [])
        if not homes:
            continue
        if n == 1 and (len(short) < 4 or short in _GENERIC_TOKENS):
            homes = [h for h in homes if h[0] in ctx.imported_files]
        elif n == 1:
            imported = [h for h in homes if h[0] in ctx.imported_files]
            homes = imported or (homes if len(homes) == 1 else [])
        if homes:
            rel, qual = min(homes, key=lambda h: h[0] not in ctx.imported_files)
            return {"function": qual, "file": rel,
                    "line": index.functions.get((rel, qual), 0),
                    "match_method": "test_name_convention", "via_helper": False,
                    "result_asserted": False}

    # 4. A bare call to an unambiguous module-level function.
    for call in sorted(
        (n for n in ast.walk(func_node) if isinstance(n, ast.Call)),
        key=lambda n: (n.lineno, n.col_offset),
    ):
        if isinstance(call.func, ast.Name):
            name = call.func.id
            if name in ctx.local_functions or name in _SKIP_CALLS:
                continue
            homes = [h for h in index.short_names.get(name, []) if "." not in h[1]]
            if len(homes) == 1:
                rel, qual = homes[0]
                return {"function": qual, "file": rel,
                        "line": index.functions.get((rel, qual), 0),
                        "match_method": "first_call", "via_helper": False,
                        "result_asserted": False}
    return None


# ---------------------------------------------------------------------------
# Source function extraction and similarity
# ---------------------------------------------------------------------------

def extract_source_functions(source_files: list[Path], project_root: Path) -> dict[str, list[dict]]:
    """Extract all function/method definitions from source files.

    Returns {qualified_name: [{file, line, params, is_method}]}.
    """
    functions: dict[str, list[dict]] = defaultdict(list)

    for filepath in source_files:
        try:
            source = filepath.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=str(filepath))
        except SyntaxError:
            continue

        rel = str(filepath.relative_to(project_root))

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Build qualified name
                params = [a.arg for a in node.args.args if a.arg != "self"]
                is_method = any(
                    a.arg == "self" for a in node.args.args
                )

                # Get parent class name if method
                # (simplified — checks direct parent)
                parent_name = ""
                for parent in ast.walk(tree):
                    if isinstance(parent, ast.ClassDef):
                        if node in ast.iter_child_nodes(parent):
                            parent_name = parent.name
                            break

                qualified = f"{parent_name}.{node.name}" if parent_name else node.name

                functions[qualified].append({
                    "file": rel,
                    "line": node.lineno,
                    "params": params,
                    "param_count": len(params),
                    "is_method": is_method,
                    "is_async": isinstance(node, ast.AsyncFunctionDef),
                    "name": node.name,
                })

    return dict(functions)


def find_similar_functions(
    func_name: str, func_info: dict,
    all_functions: dict[str, list[dict]],
    max_similar: int = 5,
) -> list[dict]:
    """Find functions structurally similar to the given one.

    Similarity is based on name pattern and parameter count.
    """
    results: list[dict] = []
    base_name = func_info.get("name", func_name.rsplit(".", 1)[-1])
    base_params = func_info.get("param_count", 0)

    # Extract the verb prefix (validate_, parse_, process_, handle_, etc.)
    parts = base_name.split("_")
    prefix = parts[0] if parts else ""

    for qname, locations in all_functions.items():
        if qname == func_name:
            continue

        short = qname.rsplit(".", 1)[-1] if "." in qname else qname
        loc = locations[0]

        # Same file is less interesting — different files are higher signal
        same_file = loc["file"] == func_info.get("file", "")

        # Similarity scoring
        score = 0

        # Name prefix match (validate_input ↔ validate_config)
        other_parts = short.split("_")
        if other_parts and other_parts[0] == prefix and len(prefix) > 2:
            score += 3

        # Similar parameter count (±1)
        if abs(loc.get("param_count", 0) - base_params) <= 1:
            score += 1

        # Same async nature
        if loc.get("is_async") == func_info.get("is_async"):
            score += 1

        # Same method-ness
        if loc.get("is_method") == func_info.get("is_method"):
            score += 1

        # Different file bonus (cross-module invariant propagation is higher value)
        if not same_file:
            score += 1

        if score >= 3:
            results.append({
                "function": qname,
                "file": loc["file"],
                "line": loc["line"],
                "similarity_score": score,
                "similarity_reason": _similarity_reason(prefix, short, base_params, loc, same_file),
            })

    # Sort by score descending, take top N
    results.sort(key=lambda x: x["similarity_score"], reverse=True)
    return results[:max_similar]


def _similarity_reason(
    prefix: str, other_name: str, base_params: int, loc: dict, same_file: bool
) -> str:
    """Build a human-readable similarity explanation."""
    reasons: list[str] = []
    other_parts = other_name.split("_")
    if other_parts and other_parts[0] == prefix and len(prefix) > 2:
        reasons.append(f"same '{prefix}_' prefix")
    if abs(loc.get("param_count", 0) - base_params) <= 1:
        reasons.append("similar parameter count")
    if not same_file:
        reasons.append("different module")
    return ", ".join(reasons) if reasons else "structural similarity"


# ---------------------------------------------------------------------------
# Test selection (three-tier)
# ---------------------------------------------------------------------------

def _is_high_signal_test(name: str, file: str = "") -> bool:
    """True when the test's name, or its file's name, uses failure vocabulary.

    Matched per underscore-separated word, by prefix (`race` matches `races`
    but not `trace`), so `test_cache_race.py` marks every test in it.
    """
    return any(w.startswith(_HIGH_SIGNAL_WORDS) for w in _name_words(name, file))


def _name_words(name: str, file: str = "") -> list[str]:
    return _tokens(name) + (_tokens(Path(file).stem) if file else [])


def _is_strong_signal_test(name: str, file: str = "") -> bool:
    return any(w.startswith(_STRONG_SIGNAL_WORDS) for w in _name_words(name, file))


# Bug-fix commit subjects: whole words (`fix`, `fixes`, `[fix]`) and word
# prefixes, so `prefix` or `dispatch` do not count.
_FIX_COMMIT_RE = re.compile(
    r"\b(?:fix|bug|crash|leak|error|patch|regress|race|cancel|rollback|"
    r"concurren|lock|deadlock|timeout|thread)",
    re.IGNORECASE,
)

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)
_BUG_FIX_LIST_CAP = 15


def _git(args: list[str], cwd: Path, timeout: int = 10) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args], capture_output=True, text=True, timeout=timeout,
            cwd=str(cwd), check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    return result.stdout if result.returncode == 0 else None


def _tests_touched(source: str, added: list[tuple[int, int]]) -> list[str]:
    """Names of the test functions whose body overlaps an added line range."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    names: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test"):
            continue
        first = min([node.lineno] + [d.lineno for d in node.decorator_list])
        last = node.end_lineno or node.lineno
        if any(start <= last and end >= first for start, end in added):
            names.append(node.name)
    return names


def _get_bug_fix_tests(
    test_files: list[Path], project_root: Path
) -> tuple[list[dict], int]:
    """Identify the tests that bug-fix commits added or changed, using git.

    A commit counts as a fix when its subject uses fix vocabulary (see
    `_FIX_COMMIT_RE`). For each test file it touched, the diff's added lines
    name the test functions it changed (`test_functions`, with
    `attribution: "function"`); when no test function was touched (a fixture
    or helper changed) the entry keeps `test_functions: []` and
    `attribution: "file"`. Returns ``(entries, total)``; the caller caps the
    list it prints, never the list it selects from.
    """
    log = _git(
        ["log", "--format=%H %s", "-100", "--", "*.py"], project_root, timeout=30
    )
    if log is None:
        return [], 0

    fix_commits: list[tuple[str, str]] = []
    for line in log.strip().split("\n"):
        sha, _, msg = line.partition(" ")
        if sha and msg and _FIX_COMMIT_RE.search(msg):
            fix_commits.append((sha, msg))

    test_rel_paths = set()
    for tf in test_files:
        try:
            test_rel_paths.add(str(tf.relative_to(project_root)))
        except ValueError:
            pass

    bug_fix_tests: list[dict] = []
    for sha, msg in fix_commits[:30]:
        changed = _git(
            ["diff-tree", "--no-commit-id", "-r", "--name-only", sha], project_root
        )
        for changed_file in (changed or "").strip().split("\n"):
            if changed_file not in test_rel_paths:
                continue
            diff = _git(["show", "-U0", "--format=", sha, "--", changed_file],
                        project_root) or ""
            added = [
                (int(start), int(start) + max(int(count or 1), 1) - 1)
                for start, count in _HUNK_RE.findall(diff)
                if count != "0"
            ]
            at_commit = _git(["show", f"{sha}:{changed_file}"], project_root) or ""
            functions = _tests_touched(at_commit, added)
            bug_fix_tests.append({
                "test_file": changed_file,
                "fix_commit": sha[:8],
                "fix_message": msg[:120],
                "test_functions": functions,
                "attribution": "function" if functions else "file",
            })

    return bug_fix_tests, len(bug_fix_tests)


def _round_robin(tests: list[dict], file_rank=None) -> list[dict]:
    """Interleave tests across files, keeping each file's own order.

    Without this the alphabetically first files fill every slot of a tier.
    *file_rank* (file → sort key) orders the files; it matters whenever a
    tier spans more files than it has slots.
    """
    by_file: dict[str, list[dict]] = {}
    for t in tests:
        by_file.setdefault(t.get("file", ""), []).append(t)
    files = list(by_file)
    if file_rank is not None:
        files.sort(key=file_rank)
    queues = [by_file[f] for f in files]
    out: list[dict] = []
    depth = 0
    while len(out) < len(tests):
        for queue in queues:
            if depth < len(queue):
                out.append(queue[depth])
        depth += 1
    return out


def select_tests(
    test_info: list[dict],
    bug_fix_tests: list[dict],
    max_tests: int = 30,
) -> list[dict]:
    """Select high-value tests using three-tier prioritization.

    Each item in test_info is {file, function, line, assertions, ...}.
    Returns selected subset with selection_tier added. Within a tier, tests
    are taken round-robin across files.
    """
    tier1: list[dict] = []  # bug-fix tests
    tier2: list[dict] = []  # error/boundary tests
    tier3: list[dict] = []  # remaining tests

    # Tests a fix changed, by name; whole files only where the fix touched
    # no test function (or the entry predates function attribution).
    fix_functions = {
        (bt["test_file"], fn)
        for bt in bug_fix_tests
        for fn in bt.get("test_functions") or ()
    }
    fix_files = {
        bt["test_file"] for bt in bug_fix_tests if not bt.get("test_functions")
    }

    for t in test_info:
        # Skip tests with only implementation-detail assertions
        behavioral = [a for a in t.get("assertions", []) if not a.get("is_implementation_detail")]
        if not behavioral:
            continue

        rel_file = t.get("file", "")

        if (rel_file, t.get("function")) in fix_functions or rel_file in fix_files:
            tier1.append({**t, "selection_tier": "bug_fix"})
        elif _is_high_signal_test(t.get("function", ""), rel_file) or any(
            a["invariant_type"] == "error_condition" for a in behavioral
        ):
            tier2.append({**t, "selection_tier": "error_boundary"})
        else:
            tier3.append({**t, "selection_tier": "general"})

    # Concurrency/cancellation/rollback tests first within tier 2, then the
    # rest; each group round-robin across files.
    # Files whose own name carries the signal (`test_cache_race.py`) first.
    strong = [t for t in tier2 if _is_strong_signal_test(t["function"], t["file"])]
    weak = [t for t in tier2 if not _is_strong_signal_test(t["function"], t["file"])]
    tier2 = _round_robin(
        strong, lambda f: not _is_strong_signal_test("", f)
    ) + _round_robin(weak)
    # Bug-fix files in commit order, newest fix first.
    newest_fix: dict[str, int] = {}
    for i, bt in enumerate(bug_fix_tests):
        newest_fix.setdefault(bt["test_file"], i)
    tier1 = _round_robin(tier1, lambda f: newest_fix.get(f, len(newest_fix)))
    tier3 = _round_robin(tier3)

    # Distribute quota: 10/10/10 by default, redistribute if any tier is short
    quota1 = min(len(tier1), max_tests // 3)
    quota2 = min(len(tier2), max_tests // 3)
    quota3 = max_tests - quota1 - quota2

    # If tier3 also short, redistribute back
    quota3 = min(len(tier3), quota3)
    remaining = max_tests - quota1 - quota2 - quota3
    if remaining > 0:
        # Give remaining to tier2, then tier1
        extra2 = min(len(tier2) - quota2, remaining)
        quota2 += extra2
        remaining -= extra2
        extra1 = min(len(tier1) - quota1, remaining)
        quota1 += extra1

    selected = tier1[:quota1] + tier2[:quota2] + tier3[:quota3]
    return selected


# ---------------------------------------------------------------------------
# Main analysis
# ---------------------------------------------------------------------------

def _conftest_trees(
    test_file: Path, project_root: Path, cache: dict[Path, ast.Module | None]
) -> list[tuple[Path, ast.Module]]:
    """conftest.py files that apply to *test_file*, outermost first."""
    chain: list[Path] = []
    directory = test_file.parent
    while True:
        chain.append(directory / "conftest.py")
        if directory == project_root or directory == directory.parent:
            break
        directory = directory.parent
    found: list[tuple[Path, ast.Module]] = []
    for path in reversed(chain):
        if path not in cache:
            tree = None
            if path.is_file():
                try:
                    tree = ast.parse(
                        path.read_text(encoding="utf-8", errors="replace")
                    )
                except (SyntaxError, ValueError):
                    tree = None
            cache[path] = tree
        if cache[path] is not None:
            found.append((path, cache[path]))
    return found


def _extract_all_test_info(
    test_files: list[Path], project_root: Path, index: SourceIndex,
) -> list[dict]:
    """Extract test function info from all test files."""
    all_tests: list[dict] = []
    conftest_cache: dict[Path, ast.Module | None] = {}

    for filepath in test_files:
        try:
            source = filepath.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=str(filepath))
        except SyntaxError:
            continue

        rel = str(filepath.relative_to(project_root))
        # Fixtures from the applicable conftest.py files, then the file's own;
        # a nearer definition overrides an outer one, as in pytest.
        fixtures = collect_fixture_types(
            _conftest_trees(filepath, project_root, conftest_cache)
            + [(filepath, tree)],
            index,
        )
        ctx = _TestFileContext(filepath, tree, index, fixtures)

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("test"):
                continue

            assertions = extract_assertions(node)
            if not assertions:
                continue

            tested_func = resolve_tested_function(node, ctx)

            info: dict = {
                "file": rel,
                "function": node.name,
                "line": node.lineno,
                "assertions": assertions,
                "assertion_count": len(assertions),
                "behavioral_assertion_count": sum(
                    1 for a in assertions if not a.get("is_implementation_detail")
                ),
                # Sorted: set iteration order varies with PYTHONHASHSEED, which
                # would make output non-reproducible across runs and defeat
                # cross-run deduplication (see explore's --runs option).
                "invariant_types": sorted({a["invariant_type"] for a in assertions}),
                "tested_function": tested_func,
            }
            all_tests.append(info)

    return all_tests


def analyze(target: str, *, max_files: int = 0, with_git: bool = False) -> dict:
    """Run test invariant extraction and return structured results."""
    target_path = Path(target).resolve()
    project_root = find_project_root(target_path)
    scan_root = target_path if target_path.is_dir() else target_path.parent

    all_files = list(discover_python_files(scan_root))
    if max_files > 0:
        all_files = all_files[:max_files]

    source_files, test_files = classify_files(all_files, project_root)

    # A package is usually reviewed on its own (`pkg/`) while its tests live
    # beside it (`tests/`). Finding none under the scan root must not read as
    # "this project has no invariants": look where the project keeps its
    # tests, and say where they came from. Source functions stay limited to
    # the scan root.
    warnings: list[str] = []
    test_discovery: dict = {"method": "scan_root", "roots": []}
    if not test_files:
        found: list[Path] = []
        roots: list[str] = []
        how: str | None = None
        if scan_root != project_root:
            found, roots, how = find_external_tests(scan_root, project_root)
        if found:
            test_files = found
            test_discovery = {"method": how, "roots": roots}
            warnings.append(
                f"No test files under the scan root; tests were taken from "
                f"{', '.join(roots)} ({how}). Source functions are limited to "
                f"the scan root."
            )
        else:
            test_discovery = {"method": None, "roots": []}
            warnings.append(
                "No test files found under the scan root or in the project's "
                "test directories (pytest testpaths, tests/, test/). Nothing "
                "was extracted: this is not a clean result."
            )

    # Extract source functions for mapping
    source_functions = extract_source_functions(source_files, project_root)

    # Extract all test info
    index = SourceIndex(source_files, project_root)
    all_test_info = _extract_all_test_info(test_files, project_root, index)

    # Get bug-fix tests if git is available
    bug_fix_tests: list[dict] = []
    bug_fix_tests_total = 0
    if with_git:
        bug_fix_tests, bug_fix_tests_total = _get_bug_fix_tests(
            test_files, project_root
        )

    # Select high-value tests from ALL bug-fix tests; only the printed list
    # is capped.
    selected = select_tests(all_test_info, bug_fix_tests, max_tests=30)
    bug_fix_tests = bug_fix_tests[:_BUG_FIX_LIST_CAP]

    # For each selected test with a resolved tested function, find similar functions
    invariants: list[dict] = []
    for test in selected:
        tested = test.get("tested_function")
        if tested is None:
            invariants.append({
                **test,
                "similar_functions": [],
            })
            continue

        func_name = tested["function"]
        # Find the function info for similarity matching
        func_locations = source_functions.get(func_name, [])
        func_info = func_locations[0] if func_locations else {"param_count": 0}

        similar = find_similar_functions(
            func_name, func_info, source_functions, max_similar=5,
        )

        invariants.append({
            **test,
            "similar_functions": similar,
        })

    # Find untested similar functions (functions similar to tested ones but with no tests)
    tested_func_names = {
        t["tested_function"]["function"]
        for t in all_test_info
        if t.get("tested_function")
    }
    untested_similar: list[dict] = []
    for test in selected:
        tested = test.get("tested_function")
        if tested is None:
            continue
        func_name = tested["function"]
        func_locations = source_functions.get(func_name, [])
        func_info = func_locations[0] if func_locations else {"param_count": 0}

        for sim in find_similar_functions(func_name, func_info, source_functions):
            if sim["function"] not in tested_func_names:
                untested_similar.append({
                    "function": sim["function"],
                    "file": sim["file"],
                    "line": sim["line"],
                    "similar_to": func_name,
                    "tested_in": test["file"],
                    "invariant_types": test["invariant_types"],
                    "similarity_reason": sim["similarity_reason"],
                })

    # Deduplicate untested_similar by function name
    seen: set[str] = set()
    deduped: list[dict] = []
    for item in untested_similar:
        if item["function"] not in seen:
            seen.add(item["function"])
            deduped.append(item)
    untested_similar_total = len(deduped)
    untested_similar = deduped[:20]

    return {
        "project_root": str(project_root),
        "scan_root": str(scan_root),
        # "no_tests_found" when no test file was found anywhere: an empty
        # result then means "not measured", not "no invariants".
        "status": "ok" if test_files else "no_tests_found",
        "test_discovery": test_discovery,
        "warnings": warnings,
        "summary": {
            "source_files": len(source_files),
            "test_files": len(test_files),
            "source_functions": sum(len(v) for v in source_functions.values()),
            "total_test_functions": len(all_test_info),
            "selected_test_functions": len(selected),
            "invariants_extracted": sum(
                t["behavioral_assertion_count"] for t in selected
            ),
            "similar_functions_found": sum(
                len(t.get("similar_functions", [])) for t in invariants
            ),
            # Both lists below are CAPPED. Report the true totals beside them:
            # a reviewer who reads `untested_similar_functions: 20` as a count
            # is reading the cap, not the measurement.
            "untested_similar_functions": len(untested_similar),
            "untested_similar_functions_total": untested_similar_total,
            "untested_similar_functions_capped": untested_similar_total > 20,
            "bug_fix_tests_found": len(bug_fix_tests),
            "bug_fix_tests_total": bug_fix_tests_total,
            "bug_fix_tests_capped": bug_fix_tests_total > _BUG_FIX_LIST_CAP,
        },
        "invariants": invariants,
        "bug_fix_tests": bug_fix_tests,
        "untested_similar_functions": untested_similar,
    }


def main() -> None:
    """CLI entry point."""
    from scan_common import exit_on_help

    exit_on_help(sys.argv[1:], __doc__)
    try:
        max_files = 0
        with_git = False
        positional: list[str] = []
        argv = sys.argv[1:]
        i = 0
        while i < len(argv):
            if argv[i] == "--max-files" and i + 1 < len(argv):
                max_files = int(argv[i + 1])
                i += 2
            elif argv[i] == "--with-git":
                with_git = True
                i += 1
            elif argv[i].startswith("--"):
                i += 1
            else:
                positional.append(argv[i])
                i += 1
        target = positional[0] if positional else "."
        result = analyze(target, max_files=max_files, with_git=with_git)
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
    except Exception as e:
        json.dump({"error": str(e), "type": type(e).__name__},
                  sys.stdout, indent=2)
        sys.stdout.write("\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
