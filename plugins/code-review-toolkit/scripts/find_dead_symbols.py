#!/usr/bin/env python3
"""Find dead code: unused imports, unreferenced functions, and orphan files.

Uses AST analysis to build a reference graph and identify symbols that are
defined but never used. Accounts for dynamic dispatch, __all__, magic methods,
test discovery, and entry points.

Usage:
    python find_dead_symbols.py [path]
"""

import ast
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scan_common import (  # noqa: E402
    _is_excluded,
    discover_python_files,
    find_project_root,
)


# Magic methods that are called implicitly by Python.
_MAGIC_METHODS = frozenset({
    "__init__", "__new__", "__del__", "__repr__", "__str__", "__bytes__",
    "__format__", "__lt__", "__le__", "__eq__", "__ne__", "__gt__", "__ge__",
    "__hash__", "__bool__", "__getattr__", "__getattribute__", "__setattr__",
    "__delattr__", "__dir__", "__get__", "__set__", "__delete__",
    "__set_name__", "__init_subclass__", "__class_getitem__",
    "__call__", "__len__", "__length_hint__", "__getitem__", "__setitem__",
    "__delitem__", "__missing__", "__iter__", "__next__", "__reversed__",
    "__contains__", "__add__", "__radd__", "__iadd__", "__sub__", "__rsub__",
    "__isub__", "__mul__", "__rmul__", "__imul__", "__matmul__", "__rmatmul__",
    "__imatmul__", "__truediv__", "__rtruediv__", "__itruediv__",
    "__floordiv__", "__rfloordiv__", "__ifloordiv__", "__mod__", "__rmod__",
    "__imod__", "__divmod__", "__rdivmod__", "__pow__", "__rpow__",
    "__ipow__", "__lshift__", "__rlshift__", "__ilshift__", "__rshift__",
    "__rrshift__", "__irshift__", "__and__", "__rand__", "__iand__",
    "__xor__", "__rxor__", "__ixor__", "__or__", "__ror__", "__ior__",
    "__neg__", "__pos__", "__abs__", "__invert__", "__complex__",
    "__int__", "__float__", "__index__", "__round__", "__trunc__",
    "__floor__", "__ceil__", "__enter__", "__exit__", "__aenter__",
    "__aexit__", "__aiter__", "__anext__", "__await__",
    "__fspath__", "__copy__", "__deepcopy__", "__reduce__", "__reduce_ex__",
    "__getnewargs__", "__getnewargs_ex__", "__getstate__", "__setstate__",
    "__post_init__",
})


# Decorators that wrap a function without registering it anywhere. Any OTHER
# decorator (`@app.get(...)`, `@validator`, `@pytest.fixture`, `@click.command`)
# may be how the framework finds and calls it, so a method carrying one is
# excluded rather than reported as dead.
_INERT_DECORATORS = frozenset({
    "staticmethod", "classmethod", "property", "cached_property",
    "abstractmethod", "override", "final", "overload", "contextmanager",
    "asynccontextmanager", "cache", "lru_cache", "wraps", "total_ordering",
})

# Base classes whose subclasses' ordinary methods are NOT framework hooks.
# Any other base defined outside the project (`uvicorn.Server`,
# `ast.NodeVisitor`, `threading.Thread`, `logging.Handler`, an SDK's
# `BaseClient`) may call a method the project never names: an override of a
# hook is invoked by the framework, so it is excluded, not reported.
_INERT_BASES = frozenset({
    "object", "ABC", "Protocol", "Generic", "NamedTuple", "TypedDict",
    "Enum", "IntEnum", "StrEnum", "Flag", "IntFlag",
    "Exception", "BaseException",
    "dict", "list", "set", "frozenset", "tuple", "str", "int", "float", "bytes",
})

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _dotted(node: ast.AST) -> str:
    """`a.b.c` for a Name/Attribute chain, the callee for a Call, else ''."""
    if isinstance(node, ast.Call):
        return _dotted(node.func)
    if isinstance(node, ast.Subscript):  # Generic[T], Protocol[T]
        return _dotted(node.value)
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        inner = _dotted(node.value)
        return f"{inner}.{node.attr}" if inner else node.attr
    return ""


def _decorator_kind(decorators: list[ast.expr]) -> str | None:
    """None when every decorator is inert, else why the method is excluded."""
    for dec in decorators:
        name = _dotted(dec)
        last = name.rsplit(".", 1)[-1]
        if last in ("setter", "getter", "deleter") and not isinstance(dec, ast.Call):
            return "property_accessor"
        if last not in _INERT_DECORATORS:
            return "decorated"
    return None


def _is_self_receiver(node: ast.expr) -> bool:
    """`self`, `cls` or `super()` -- a receiver bound to the enclosing class."""
    if isinstance(node, ast.Name):
        return node.id in ("self", "cls")
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "super"
    )


def _is_inert_base(name: str) -> bool:
    last = name.rsplit(".", 1)[-1]
    return last in _INERT_BASES or last.endswith(("Error", "Exception", "Warning"))


def _alias_line(alias: ast.alias, node: ast.stmt) -> int:
    """The line of the imported NAME, which is the line ruff (F401) reports.

    For `from x import (\n    a,\n    b,\n)` the statement starts on the `from`
    line while each name sits on its own; reporting the statement line made
    findings impossible to match against ruff by `file:line`.
    """
    return getattr(alias, "lineno", None) or node.lineno


def analyze_file(filepath: Path, project_root: Path) -> dict:
    """Analyze a file for defined symbols and their usage."""
    rel = str(filepath.relative_to(project_root))
    result: dict = {
        "file": rel,
        "imports": [],
        "defined_symbols": [],
        "referenced_names": set(),
        "all_declaration": None,
        # Class name -> dotted names of its bases (and `metaclass=`), so a
        # method can be recognised as a possible override of a framework hook.
        "class_bases": {},
        # Identifier-like string constants: `getattr(obj, "pause_job")`
        # names a method no Name/Attribute node ever mentions.
        "string_refs": set(),
        # Literal prefixes of computed getattr names: `getattr(self, f"cmd_{x}")`
        # can reach any method whose name starts with `cmd_`.
        "dispatch_prefixes": set(),
        "is_init": filepath.name == "__init__.py",
        "is_test": (
            filepath.name.startswith("test_")
            or filepath.name.endswith("_test.py")
            or "test" in filepath.relative_to(project_root).parts[:-1]
            or "tests" in filepath.relative_to(project_root).parts[:-1]
        ),
        "has_main_guard": False,
        "parse_error": None,
    }

    try:
        source = filepath.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(filepath))
    except SyntaxError as exc:
        result["parse_error"] = f"SyntaxError: {exc.msg} (line {exc.lineno})"
        return result

    # Check for __all__.
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "__all__"
            and isinstance(node.value, (ast.List, ast.Tuple))
        ):
            result["all_declaration"] = [
                elt.value
                for elt in node.value.elts
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
            ]

    # Check for if __name__ == "__main__" guard.
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.If):
            test = node.test
            if (
                isinstance(test, ast.Compare)
                and isinstance(test.left, ast.Name)
                and test.left.id == "__name__"
            ):
                result["has_main_guard"] = True

    # Collect imports (what names are imported).
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local_name = alias.asname or alias.name.split(".")[0]
                result["imports"].append({
                    "local_name": local_name,
                    "module": alias.name,
                    "line": _alias_line(alias, node),
                    "statement_line": node.lineno,
                    "is_from": False,
                })
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    continue
                local_name = alias.asname or alias.name
                result["imports"].append({
                    "local_name": local_name,
                    "module": f"{node.module or ''}.{alias.name}",
                    "line": _alias_line(alias, node),
                    "statement_line": node.lineno,
                    "is_from": True,
                })

    # `# pragma: debugging` marks a maintainer-only tool invoked by hand from a
    # debugger; being unreferenced is its normal state.
    source_lines = source.splitlines()

    def debugging_pragma(node: ast.AST) -> bool:
        start = getattr(node, "lineno", 0)
        end = getattr(node, "end_lineno", start) or start
        window = source_lines[max(0, start - 1) : min(len(source_lines), end)]
        return any("pragma: debugging" in line for line in window)

    # Collect defined symbols (top-level and class-level).
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            result["defined_symbols"].append({
                "name": node.name,
                "type": "function",
                "line": node.lineno,
                "is_public": not node.name.startswith("_"),
                "is_test_method": node.name.startswith("test"),
                "is_magic": node.name in _MAGIC_METHODS,
                "is_debugging_pragma": debugging_pragma(node),
                "decorator_kind": _decorator_kind(node.decorator_list),
            })
        elif isinstance(node, ast.ClassDef):
            result["defined_symbols"].append({
                "name": node.name,
                "type": "class",
                "line": node.lineno,
                "is_public": not node.name.startswith("_"),
            })
            bases = [_dotted(b) or "<expr>" for b in node.bases]
            bases += [
                _dotted(k.value) or "<expr>"
                for k in node.keywords
                if k.arg == "metaclass"
            ]
            result["class_bases"].setdefault(node.name, []).extend(bases)
            # Class methods.
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    result["defined_symbols"].append({
                        "name": f"{node.name}.{child.name}",
                        "type": "method",
                        "line": child.lineno,
                        "is_public": not child.name.startswith("_"),
                        "is_test_method": child.name.startswith("test"),
                        "is_magic": child.name in _MAGIC_METHODS,
                        "class": node.name,
                        "decorator_kind": _decorator_kind(child.decorator_list),
                        "is_debugging_pragma": debugging_pragma(child),
                    })

    # Collect all referenced names (everything used, not just defined).
    # Attribute references are also split by receiver: `self.x` / `cls.x` /
    # `super().x` inside a class body can only reach that class's hierarchy,
    # while `obj.x` may reach any class.
    class _NameCollector(ast.NodeVisitor):
        def __init__(self) -> None:
            self.names: set[str] = set()
            self.plain: set[str] = set()
            self.other_attrs: set[str] = set()
            self.self_attrs: dict[str, set[str]] = {}
            self.classes: list[str] = []

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self.classes.append(node.name)
            self.generic_visit(node)
            self.classes.pop()

        def visit_Name(self, node: ast.Name) -> None:
            self.names.add(node.id)
            self.plain.add(node.id)
            self.generic_visit(node)

        def visit_Attribute(self, node: ast.Attribute) -> None:
            self.names.add(node.attr)
            if self.classes and _is_self_receiver(node.value):
                self.self_attrs.setdefault(self.classes[-1], set()).add(node.attr)
            else:
                self.other_attrs.add(node.attr)
            self.generic_visit(node)

    collector = _NameCollector()
    collector.visit(tree)
    result["referenced_names"] = collector.names
    result["plain_names"] = collector.plain
    result["other_attr_refs"] = collector.other_attrs
    result["self_attr_refs"] = collector.self_attrs

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _IDENTIFIER.fullmatch(node.value):
                result["string_refs"].add(node.value)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in ("getattr", "hasattr")
            and len(node.args) >= 2
        ):
            name_arg = node.args[1]
            prefix = None
            if isinstance(name_arg, ast.JoinedStr) and name_arg.values:
                first = name_arg.values[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    prefix = first.value
            elif (
                isinstance(name_arg, ast.BinOp)
                and isinstance(name_arg.op, ast.Add)
                and isinstance(name_arg.left, ast.Constant)
                and isinstance(name_arg.left.value, str)
            ):
                prefix = name_arg.left.value
            if prefix:
                result["dispatch_prefixes"].add(prefix)

    return result


def find_unused_imports(file_analysis: dict) -> list[dict]:
    """Find imports whose local names are never referenced in the file."""
    unused = []
    refs = file_analysis["referenced_names"]
    all_decl = file_analysis["all_declaration"]

    for imp in file_analysis["imports"]:
        local_name = imp["local_name"]
        # `from __future__ import annotations` is a COMPILER DIRECTIVE (PEP 563),
        # not an import: it binds no name, so a reference scanner will always call
        # it unused. Removing it flips annotation evaluation from lazy to eager and
        # breaks every unquoted forward reference. It was 42 of the 42 "unused
        # imports" reported for coverage.py -- the entire category.
        if imp["module"].split(".")[0] == "__future__":
            continue
        # Check if name is used in the file body.
        if local_name in refs:
            continue
        # Check if it's in __all__ (re-export).
        if all_decl and local_name in all_decl:
            continue
        # In __init__.py, imports may be re-exports even without __all__.
        if file_analysis["is_init"]:
            continue

        unused.append({
            "file": file_analysis["file"],
            "line": imp["line"],
            "statement_line": imp["statement_line"],
            "name": local_name,
            "module": imp["module"],
            "confidence": "high",
        })

    return unused


_SETUP_TEARDOWN = frozenset({
    "setUp", "tearDown", "setUpClass", "tearDownClass",
    "setUpModule", "tearDownModule",
})


def _framework_classes(all_files: list[dict]) -> set[str]:
    """Classes with a non-inert base defined outside the project, directly or
    through project classes. Their methods may be hooks a framework calls.

    A base resolves to a project class only when its name (or, for `a.b.C`,
    its qualifier) is not imported from outside the project: `class
    Server(uvicorn.Server)` is a subclass of uvicorn's class, not of itself.
    """
    tops: set[str] = set()
    for fa in all_files:
        parts = Path(fa["file"]).with_suffix("").parts
        tops.update(parts)
    project_classes: set[str] = set()
    for fa in all_files:
        project_classes.update(fa.get("class_bases", {}))

    def is_internal_module(module: str) -> bool:
        return module.startswith(".") or module.split(".")[0] in tops

    # class -> list of (project class it inherits from | None, base as written)
    edges: dict[str, list[tuple[str | None, str]]] = {}
    for fa in all_files:
        imported = {imp["local_name"]: imp["module"] for imp in fa["imports"]}
        for cls, names in fa.get("class_bases", {}).items():
            for base in names:
                head, _, _rest = base.partition(".")
                last = base.rsplit(".", 1)[-1]
                if "." in base:
                    internal = head in imported and is_internal_module(imported[head])
                elif base in imported:
                    internal = is_internal_module(imported[base])
                else:
                    internal = base != cls
                target = last if internal and last in project_classes else None
                edges.setdefault(cls, []).append((target, base))

    memo: dict[str, bool] = {}

    def external(cls: str, seen: frozenset[str]) -> bool:
        if cls in memo:
            return memo[cls]
        hit = False
        for target, base in edges.get(cls, ()):
            if target is None:
                hit = not _is_inert_base(base)
            elif target not in seen:
                hit = external(target, seen | {target})
            if hit:
                break
        memo[cls] = hit
        return hit

    return {cls for cls in edges if external(cls, frozenset({cls}))}


def _candidates(
    all_files: list[dict],
    excluded: dict[str, int] | None = None,
    excluded_functions: dict[str, int] | None = None,
):
    """Yield (file_analysis, symbol, bare_name) for every symbol the dead-code
    checks may report, counting each method left out in *excluded* by reason,
    and each top-level function left out by its decorator in
    *excluded_functions*.

    Methods were once skipped wholesale ("too noisy") while the summary still
    read `unreferenced_symbols: 0`. They are now checked, minus the shapes a
    name-reference scan cannot see: dunders and `_sunder_` hooks, methods in
    test files, methods behind a registering decorator, overrides in classes
    that inherit from a framework, and names a computed `getattr` can reach.
    """
    framework = _framework_classes(all_files)
    prefixes: set[str] = set()
    for fa in all_files:
        prefixes |= fa.get("dispatch_prefixes", set())

    def skip(reason: str) -> None:
        if excluded is not None:
            excluded[reason] = excluded.get(reason, 0) + 1

    for fa in all_files:
        all_decl = fa["all_declaration"]
        for sym in fa["defined_symbols"]:
            name = sym["name"]
            bare_name = name.split(".")[-1]  # For methods, just the method name.
            is_method = sym["type"] == "method"

            if is_method:
                if bare_name.startswith("__") and bare_name.endswith("__"):
                    skip("dunder")
                    continue
                if (
                    len(bare_name) > 2
                    and bare_name.startswith("_")
                    and bare_name.endswith("_")
                ):
                    skip("sunder_hook")
                    continue
                if fa["is_test"]:
                    skip("test_file")
                    continue
                if sym.get("decorator_kind"):
                    skip(sym["decorator_kind"])
                    continue
                if sym.get("class") in framework:
                    skip("framework_base")
                    continue
                if any(bare_name.startswith(p) for p in prefixes):
                    skip("dynamic_dispatch")
                    continue
                if sym.get("is_debugging_pragma"):
                    skip("debugging_pragma")
                    continue
                if all_decl and bare_name in all_decl:
                    skip("in_all")
                    continue
                yield fa, sym, bare_name
                continue

            # Skip magic methods — called implicitly.
            if sym.get("is_magic"):
                continue
            # A registering decorator is how the function is reached:
            # `@dispatcher.register(list)`, `@app.get("/")`, `@pytest.fixture`.
            # This exclusion used to apply to methods only, so a top-level
            # `singledispatch` overload was reported as high-confidence dead.
            if sym["type"] == "function" and sym.get("decorator_kind"):
                if excluded_functions is not None:
                    kind = sym["decorator_kind"]
                    excluded_functions[kind] = excluded_functions.get(kind, 0) + 1
                continue
            # Skip test methods — called by test runner.
            if sym.get("is_test_method"):
                continue
            # Skip if in __all__.
            if all_decl and bare_name in all_decl:
                continue
            # Skip setUp/tearDown/setUpClass/tearDownClass.
            if bare_name in _SETUP_TEARDOWN:
                continue
            # Skip if the file has a __main__ guard and this could be an entry point.
            if fa["has_main_guard"] and sym["type"] == "function":
                continue
            # `# pragma: debugging` marks a tool the maintainer invokes by hand
            # from a debugger. Unreferenced is its normal state, not a defect.
            if sym.get("is_debugging_pragma"):
                continue
            yield fa, sym, bare_name


def _split_refs(all_files: list[dict]) -> tuple[set[str], set[str]]:
    """(production, test) names referenced by the scanned files, including
    identifier-like string constants (`getattr(obj, "name")`)."""
    prod: set[str] = set()
    test: set[str] = set()
    for fa in all_files:
        bucket = test if fa["is_test"] else prod
        bucket |= fa["referenced_names"]
        bucket |= fa.get("string_refs", set())
        # from X import Y -- Y is referenced.
        for imp in fa["imports"]:
            if imp["is_from"]:
                parts = imp["module"].rsplit(".", 1)
                if len(parts) > 1:
                    bucket.add(parts[1])
    return prod, test


class _MethodRefs:
    """Class-aware references for methods whose name more than one class defines.

    Matching by bare name alone lets a call to `A._load()` keep an unrelated
    `B._load()` alive. For such a shared name, a `self.`/`cls.`/`super().`
    reference counts only for classes in the same hierarchy as the class whose
    body contains it; a reference through any other receiver (`obj._load()`),
    a bare name, a string or an import still counts for every class.
    """

    def __init__(self, all_files: list[dict]) -> None:
        owners: dict[str, set[str]] = {}
        for fa in all_files:
            for sym in fa["defined_symbols"]:
                if sym["type"] == "method":
                    owners.setdefault(sym["name"].split(".")[-1], set()).add(
                        sym["class"]
                    )
        self.shared = {name for name, classes in owners.items() if len(classes) > 1}
        self.plain = {"prod": set(), "test": set()}
        self.self_refs: dict[str, dict[str, set[str]]] = {"prod": {}, "test": {}}
        bases: dict[str, set[str]] = {}
        for fa in all_files:
            bucket = "test" if fa["is_test"] else "prod"
            self.plain[bucket] |= (
                fa.get("plain_names", set())
                | fa.get("other_attr_refs", set())
                | fa.get("string_refs", set())
            )
            for imp in fa["imports"]:
                if imp["is_from"]:
                    self.plain[bucket].add(imp["module"].rsplit(".", 1)[-1])
            for cls, names in fa.get("self_attr_refs", {}).items():
                self.self_refs[bucket].setdefault(cls, set()).update(names)
            for cls, names in fa.get("class_bases", {}).items():
                bases.setdefault(cls, set()).update(n.rsplit(".", 1)[-1] for n in names)
        self._bases = bases
        self._children: dict[str, set[str]] = {}
        for cls, parents in bases.items():
            for parent in parents:
                self._children.setdefault(parent, set()).add(cls)

    def _closure(self, start: str, edges: dict[str, set[str]]) -> set[str]:
        seen = {start}
        stack = [start]
        while stack:
            for nxt in edges.get(stack.pop(), ()):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return seen

    def applies(self, sym: dict) -> bool:
        return sym["type"] == "method" and sym["name"].split(".")[-1] in self.shared

    def referenced(self, sym: dict, bucket: str) -> bool:
        """Whether *sym* (a shared-name method) is referenced from *bucket*."""
        name = sym["name"].split(".")[-1]
        if name in self.plain[bucket]:
            return True
        cls = sym["class"]
        related = self._closure(cls, self._bases) | self._closure(cls, self._children)
        return any(name in self.self_refs[bucket].get(c, ()) for c in related)


def _method_confidence(sym: dict) -> str:
    # A private method with no reference anywhere has no caller outside the
    # class either; a public one may be API used by code outside the project.
    return "medium" if not sym["is_public"] else "low"


def find_unreferenced_symbols(
    all_files: list[dict],
    external_refs: set[str] | None = None,
    *,
    test_refs: set[str] | None = None,
    doc_refs: set[str] | None = None,
    excluded: dict[str, int] | None = None,
    excluded_functions: dict[str, int] | None = None,
) -> list[dict]:
    """Find functions, classes and methods defined but never referenced anywhere.

    ``external_refs`` carries names referenced OUTSIDE the scanned tree -- an
    entry point in setup.py, a public helper used only by tests, an API shown in
    the docs. Without it, reviewing a package on its own reports every such
    symbol as dead. ``test_refs`` and ``doc_refs`` are the same, split by
    origin; for this check every origin counts as a use.
    """
    prod, test = _split_refs(all_files)
    outside = set(external_refs or ()) | set(test_refs or ()) | set(doc_refs or ())
    global_refs = prod | test | outside
    methods = _MethodRefs(all_files)

    unreferenced = []
    for fa, sym, bare_name in _candidates(all_files, excluded, excluded_functions):
        if methods.applies(sym):
            if (
                bare_name in outside
                or methods.referenced(sym, "prod")
                or methods.referenced(sym, "test")
            ):
                continue
        elif bare_name in global_refs:
            continue
        if sym["type"] == "method":
            confidence = _method_confidence(sym)
        elif sym["type"] == "class" and sym["is_public"]:
            confidence = "medium"  # Classes may be instantiated dynamically.
        else:
            confidence = "high"
        unreferenced.append({
            "file": fa["file"],
            "line": sym["line"],
            "name": sym["name"],
            "type": sym["type"],
            "is_public": sym.get("is_public", False),
            "confidence": confidence,
        })
    return unreferenced


def find_test_only_symbols(
    all_files: list[dict],
    external_refs: set[str] | None = None,
    *,
    test_refs: set[str] | None = None,
    doc_refs: set[str] | None = None,
) -> list[dict]:
    """Production symbols whose only references are in tests.

    A mention in a test used to count as a use, so production code kept alive
    only by its own tests was invisible: a read helper whose last production
    caller was refactored away, kept alive by the tests written for it. These
    are not dead -- removing one means editing tests, and it may be deliberate
    API -- so they are a category of their own rather than
    `unreferenced_symbols`.
    """
    prod, test = _split_refs(all_files)
    outside_prod = set(external_refs or ())
    prod |= outside_prod
    test |= set(test_refs or ())
    docs = set(doc_refs or ())
    methods = _MethodRefs(all_files)

    test_only = []
    for fa, sym, bare_name in _candidates(all_files):
        if fa["is_test"] or bare_name not in test:
            continue
        if methods.applies(sym):
            # A same-named method on an unrelated class does not keep this one
            # alive in production.
            if bare_name in outside_prod or methods.referenced(sym, "prod"):
                continue
        elif bare_name in prod:
            continue
        if sym.get("decorator_kind"):
            continue  # registered by its decorator: that is a production use
        test_only.append({
            "file": fa["file"],
            "line": sym["line"],
            "name": sym["name"],
            "type": sym["type"],
            "is_public": sym.get("is_public", False),
            "mentioned_in_docs": bare_name in docs,
            "confidence": "medium",
        })
    return test_only


def find_orphan_files(
    all_files: list[dict], project_root: Path, external_refs: set[str] | None = None
) -> list[dict]:
    """Find Python files that are never imported by any other file.

    ``external_refs`` also covers files referenced by NAME rather than imported --
    coverage.py's `pth_file.py` is read as text by setup.py and embedded into the
    installed `.pth`, so it is a source template, not a module.
    """
    # Collect all modules that are imported from.
    imported_modules: set[str] = set()
    for fa in all_files:
        for imp in fa["imports"]:
            imported_modules.add(imp["module"])
            # Add partial paths.
            parts = imp["module"].split(".")
            for i in range(1, len(parts) + 1):
                imported_modules.add(".".join(parts[:i]))

    orphans = []
    for fa in all_files:
        rel = fa["file"]
        # Convert file path to module path.
        mod = rel.replace("/", ".").replace("\\", ".")
        if mod.endswith(".py"):
            mod = mod[:-3]
        if mod.endswith(".__init__"):
            mod = mod[:-9]

        # Skip files that are likely entry points.
        if fa["has_main_guard"]:
            continue
        if fa["is_test"]:
            continue  # Test files are run by test runners.
        if rel.endswith("__init__.py"):
            continue  # __init__.py files are imported with the package.
        if rel in ("setup.py", "conftest.py"):
            continue
        # `python -m pkg` executes __main__.py directly; being unimported is what
        # it is FOR. Flagging it is wrong by construction.
        if Path(rel).name == "__main__.py":
            continue

        # Check if any variant of this module is imported.
        is_imported = any(
            mod == m or mod.endswith("." + m) or m.endswith("." + mod)
            or m.startswith(mod + ".")
            for m in imported_modules
        )

        if not is_imported and Path(rel).stem not in (external_refs or set()):
            orphans.append({
                "file": rel,
                "module": mod,
                "confidence": "medium",  # Could be entry point or dynamic.
            })

    return orphans


def find_commented_code(filepath: Path, project_root: Path) -> list[dict]:
    """Find blocks of commented-out code (not documentation comments)."""
    try:
        lines = filepath.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []

    rel = str(filepath.relative_to(project_root))
    blocks: list[dict] = []

    # Heuristic: consecutive comment lines that look like code.
    _CODE_PATTERNS = re.compile(
        r"^#\s*(def |class |import |from |if |for |while |return |raise |"
        r"try:|except |with |yield |assert |self\.|print\(|"
        r"\w+\s*=\s*|^\s*#\s*\w+\()"
    )

    # A comment run whose FIRST line is prose is documentation that happens to
    # contain a code example -- all 6 blocks reported for coverage.py were this:
    # explanatory text illustrating an edge case the surrounding code handles.
    _PROSE = re.compile(r"^#\s*[A-Z][a-z]+\b[^=(]*$")

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if _CODE_PATTERNS.match(line):
            # Start of a potential commented-out code block.
            block_start = i + 1
            block_lines = [line]
            j = i + 1
            while j < len(lines):
                next_line = lines[j].strip()
                if next_line.startswith("#") and len(next_line) > 1:
                    block_lines.append(next_line)
                    j += 1
                else:
                    break
            # Walk back: is this run introduced by a prose sentence?
            lead = i - 1
            while lead >= 0 and lines[lead].strip().startswith("#"):
                if _PROSE.match(lines[lead].strip()):
                    block_lines = []
                    break
                lead -= 1
            if len(block_lines) >= 3:  # At least 3 consecutive lines.
                blocks.append({
                    "file": rel,
                    "line_start": block_start,
                    "line_end": block_start + len(block_lines) - 1,
                    "line_count": len(block_lines),
                    "preview": block_lines[0][:80],
                })
            i = j
        else:
            i += 1

    return blocks


# Template files read attributes and call helpers by name: `{{ order.total }}`
# in a Jinja template is a production use no Python file shows.
_TEMPLATE_SUFFIXES = frozenset({".html", ".htm", ".jinja", ".jinja2", ".j2"})
_TEMPLATE_BLOCK = re.compile(r"\{\{(.*?)\}\}|\{%(.*?)%\}", re.DOTALL)
_MAX_TEMPLATE_BYTES = 2_000_000


def template_refs(scan_root: Path) -> tuple[set[str], list[str]]:
    """Identifiers inside `{{ ... }}` and `{% ... %}` blocks of the template
    files under *scan_root*, and the files read (relative to *scan_root*)."""
    names: set[str] = set()
    files: list[str] = []
    for path in sorted(scan_root.rglob("*")):
        if path.suffix.lower() not in _TEMPLATE_SUFFIXES or not path.is_file():
            continue
        if _is_excluded(path, scan_root):
            continue
        try:
            if path.stat().st_size > _MAX_TEMPLATE_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        found = False
        for match in _TEMPLATE_BLOCK.finditer(text):
            found = True
            names |= set(_IDENTIFIER.findall(match.group(1) or match.group(2) or ""))
        if found:
            files.append(str(path.relative_to(scan_root)))
    return names, files


# Where references from outside the scanned tree come from, by kind.
_TEST_SCOPES = ("conftest.py", "tests", "test")
_DOC_SCOPES = ("doc", "Doc", "docs")
_PRODUCTION_SCOPES = ("setup.py", "scripts", "tools", "bin", "benchmarks", "examples")


def base_of(extra: Path, project_root: Path, scan_root: Path) -> Path:
    """The search base *extra* was found under (the scanned tree's parent wins:
    it is the deeper of the two)."""
    for base in (scan_root.parent, project_root):
        if extra.is_relative_to(base) and extra != base:
            return base
    return project_root


def main() -> None:
    from scan_common import exit_on_help

    exit_on_help(sys.argv[1:], __doc__)
    max_files = 0  # 0 = no limit
    positional: list[str] = []
    argv = sys.argv[1:]
    i = 0
    while i < len(argv):
        if argv[i] == "--max-files" and i + 1 < len(argv):
            max_files = int(argv[i + 1])
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

    # A package reviewed on its own does not contain every reference to its own
    # symbols: setup.py declares console_scripts, tests/ exercises public helpers,
    # doc/ shows documented APIs. Scoping references to the scanned tree alone
    # reported 9 unreferenced symbols for coverage.py of which 9 were referenced
    # elsewhere in the repo. Collect references from the wider project without
    # analysing those files as subjects -- keeping their ORIGIN, because a symbol
    # referenced only by tests is a finding of its own (`referenced_only_from_tests`).
    external_refs: set[str] = set()  # production: setup.py, scripts/, ...
    test_refs: set[str] = set()
    doc_refs: set[str] = set()
    external_scopes: list[str] = []
    scope_kinds: dict[str, str] = {}
    if scan_root != project_root:
        package = scan_root.name
        # Search the project root AND the scanned tree's own parent: CPython puts
        # Lib/test/ and Doc/ beside Lib/<package>/, not at the repository root.
        candidates: list[Path] = []
        for base in (project_root, scan_root.parent):
            for name in (*_TEST_SCOPES, *_DOC_SCOPES, *_PRODUCTION_SCOPES):
                extra = base / name
                if not extra.exists():
                    continue
                if extra.is_dir():
                    # Prefer a subdirectory dedicated to this package -- walking
                    # all of CPython's Lib/test to review one package is both slow
                    # and wrong.
                    for special in (f"test_{package}", f"test_{package.lstrip('_')}",
                                    package):
                        if (extra / special).is_dir():
                            extra = extra / special
                            break
                if extra not in candidates:
                    candidates.append(extra)
        for extra in candidates:
            top = extra.relative_to(base_of(extra, project_root, scan_root)).parts[0]
            kind = (
                "test" if top in _TEST_SCOPES
                else "docs" if top in _DOC_SCOPES
                else "production"
            )
            bucket = {"test": test_refs, "docs": doc_refs}.get(kind, external_refs)
            paths = (
                [extra] if extra.is_file() else sorted(discover_python_files(extra))
            )
            for path in paths:
                if path in all_files:
                    continue
                ext = analyze_file(path, project_root)
                bucket |= ext["referenced_names"]
                bucket |= ext["string_refs"]
            scope = str(extra.relative_to(project_root))
            external_scopes.append(scope)
            scope_kinds[scope] = kind
        # Documentation and packaging metadata reference symbols as plain text.
        for name in ("setup.py", "doc", "Doc", "docs"):
            extra = project_root / name
            if not extra.exists():
                continue
            texts = (
                [extra]
                if extra.is_file()
                else [q for q in extra.rglob("*") if q.is_file() and q.suffix in
                      (".rst", ".md", ".txt", ".cfg", ".toml")]
            )
            bucket = external_refs if name == "setup.py" else doc_refs
            for path in texts:
                try:
                    body = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                bucket |= set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", body))

    # Templates inside the scanned tree are production code too.
    from_templates, template_files = template_refs(scan_root)
    external_refs |= from_templates

    # Find issues (before converting sets to lists).
    unused_imports: list[dict] = []
    for fa in file_analyses:
        unused_imports.extend(find_unused_imports(fa))

    excluded_methods: dict[str, int] = {}
    excluded_functions: dict[str, int] = {}
    unreferenced = find_unreferenced_symbols(
        file_analyses,
        external_refs,
        test_refs=test_refs,
        doc_refs=doc_refs,
        excluded=excluded_methods,
        excluded_functions=excluded_functions,
    )
    test_only = find_test_only_symbols(
        file_analyses, external_refs, test_refs=test_refs, doc_refs=doc_refs
    )
    methods_defined = sum(
        1 for fa in file_analyses for sym in fa["defined_symbols"]
        if sym["type"] == "method"
    )

    # Drop per-file reference sets to free memory.
    for fa in file_analyses:
        for key in ("referenced_names", "plain_names", "other_attr_refs"):
            fa.pop(key, None)
        fa.pop("self_attr_refs", None)

    orphans = find_orphan_files(
        file_analyses, project_root, external_refs | test_refs | doc_refs
    )

    commented_code: list[dict] = []
    for f in all_files:
        commented_code.extend(find_commented_code(f, project_root))

    # Summary.
    high_confidence = (
        [u for u in unused_imports if u["confidence"] == "high"]
        + [u for u in unreferenced if u["confidence"] == "high"]
    )
    medium_confidence = (
        [u for u in unused_imports if u["confidence"] == "medium"]
        + [u for u in unreferenced if u["confidence"] == "medium"]
        + orphans
    )
    low_confidence = [u for u in unreferenced if u["confidence"] == "low"]

    output = {
        "project_root": str(project_root),
        "scan_root": str(scan_root),
        "files_total": files_total,
        "files_analyzed": len(all_files),
        "files_capped": max_files > 0 and files_total > max_files,
        "summary": {
            "unused_imports": len(unused_imports),
            "unreferenced_symbols": len(unreferenced),
            # Methods are checked; these say how many were, and how many were
            # left out and why, so an empty list is never mistaken for "every
            # method is used".
            "methods_checked": True,
            "methods_defined": methods_defined,
            "methods_excluded": sum(excluded_methods.values()),
            "methods_excluded_by_reason": dict(sorted(excluded_methods.items())),
            # Top-level functions left out because a decorator registers them
            # (`@dispatcher.register`, `@app.get`, `@pytest.fixture`).
            "functions_excluded_by_reason": dict(sorted(excluded_functions.items())),
            "referenced_only_from_tests": len(test_only),
            "external_reference_scopes": external_scopes,
            "external_reference_scope_kinds": scope_kinds,
            # Template files (Jinja/HTML) whose `{{ }}`/`{% %}` blocks were read
            # as production references.
            "template_files_scanned": template_files,
            "orphan_files": len(orphans),
            "commented_code_blocks": len(commented_code),
            "high_confidence_items": len(high_confidence),
            "medium_confidence_items": len(medium_confidence),
            "low_confidence_items": len(low_confidence),
        },
        "unused_imports": unused_imports,
        "unreferenced_symbols": unreferenced,
        "referenced_only_from_tests": test_only,
        "orphan_files": orphans,
        "commented_code_blocks": commented_code,
    }

    json.dump(output, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
