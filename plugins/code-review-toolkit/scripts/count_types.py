#!/usr/bin/env python3
"""Count type annotation coverage and catalog type design patterns.

Surveys: annotated vs. unannotated function signatures (module-level
functions, methods, and -- separately -- nested functions such as route
handlers defined inside a factory), Any usage (direct, and through type
aliases), type: ignore comments, data container types (dataclass, TypedDict,
NamedTuple, Protocol, Enum, ABC, pseudo-enum constant classes, dict/list/set
subclasses), class and instance attribute annotations, parameter names that
are annotated in some functions and not in others, and untyped functions
that typed code calls (the boundary where annotating one signature lets the
checker see a whole call chain).

Usage:
    python count_types.py [path] [--max-files N]
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scan_common import (  # noqa: E402
    build_envelope,
    discover_python_files,
    emit,
    find_project_root,
    parse_common_args,
)

# Bare (unparameterized) container names reported as `untyped_containers`.
_BARE_CONTAINERS = frozenset(
    {
        "list",
        "dict",
        "set",
        "frozenset",
        "tuple",
        "List",
        "Dict",
        "Set",
        "FrozenSet",
        "Tuple",
    }
)

# Subscripted names whose subscript makes an assignment a type alias:
# `Payload = dict[str, Any]` is an alias, `value = data["key"]` is not.
_GENERIC_NAMES = frozenset(
    {
        "dict",
        "list",
        "set",
        "frozenset",
        "tuple",
        "type",
        "Dict",
        "List",
        "Set",
        "FrozenSet",
        "Tuple",
        "Type",
        "Optional",
        "Union",
        "Callable",
        "Mapping",
        "MutableMapping",
        "Sequence",
        "MutableSequence",
        "Iterable",
        "Iterator",
        "Generator",
        "AsyncIterable",
        "AsyncIterator",
        "AsyncGenerator",
        "Awaitable",
        "Coroutine",
        "Collection",
        "Container",
        "AbstractSet",
        "MutableSet",
        "Literal",
        "Annotated",
        "DefaultDict",
        "OrderedDict",
        "Counter",
        "Deque",
        "deque",
        "defaultdict",
        "ChainMap",
        "TypeGuard",
        "TypeIs",
        "Required",
        "NotRequired",
        "ReadOnly",
    }
)

# Names that make `A | B` a type expression rather than a bitwise or.
_BUILTIN_TYPES = frozenset(
    {"int", "str", "float", "bool", "bytes", "bytearray", "complex", "object"}
)

_CONTAINER_BASES = {
    "dict": "dict-subclass",
    "Dict": "dict-subclass",
    "UserDict": "dict-subclass",
    "OrderedDict": "dict-subclass",
    "defaultdict": "dict-subclass",
    "list": "list-subclass",
    "List": "list-subclass",
    "UserList": "list-subclass",
    "set": "set-subclass",
    "Set": "set-subclass",
    "frozenset": "set-subclass",
}

_UPPER_CASE = re.compile(r"^[A-Z][A-Z0-9_]*$")

# Methods whose `self.x = ...` assignments define the instance's attributes.
_INIT_METHODS = ("__init__", "__post_init__")

_PUBLIC_DUNDERS = (
    "__init__",
    "__new__",
    "__call__",
    "__enter__",
    "__exit__",
    "__repr__",
    "__str__",
    "__eq__",
    "__hash__",
    "__len__",
    "__iter__",
    "__next__",
    "__getitem__",
    "__setitem__",
    "__contains__",
    "__bool__",
)

_LIST_CAP = 50


def _annotation_to_str(node: ast.AST | None) -> str | None:
    """Convert an annotation AST node to a string representation."""
    if node is None:
        return None
    return ast.unparse(node)


def _is_any(annotation: ast.AST | None) -> bool:
    """Check if an annotation is 'Any'."""
    if annotation is None:
        return False
    return (isinstance(annotation, ast.Name) and annotation.id == "Any") or (
        isinstance(annotation, ast.Attribute) and annotation.attr == "Any"
    )


def _contains_any(annotation: ast.AST | None) -> bool:
    """Check if an annotation contains Any anywhere (e.g. Dict[str, Any])."""
    if annotation is None:
        return False
    if _is_any(annotation):
        return True
    for child in ast.walk(annotation):
        if _is_any(child):
            return True
    return False


def _parse_string_annotation(node: ast.AST | None) -> ast.AST | None:
    """A quoted annotation (`"Payload"`) as an expression; others unchanged."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            return ast.parse(node.value, mode="eval").body
        except (SyntaxError, ValueError, RecursionError):
            return None
    return node


def _terminal_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


# ---------------------------------------------------------------------------
# Container classification
# ---------------------------------------------------------------------------


def _is_pseudo_enum(class_node: ast.ClassDef) -> bool:
    """A constants class acting as an enum: only UPPER_CASE literal assignments.

    `class Status: OK = "ok"; FAILED = "failed"` gets none of Enum's checking
    (any str is accepted where a Status is meant), which is the container
    question worth asking. Needs at least two constants, no methods, and no
    base other than `object`, so a config class with one constant or a
    subclass that only overrides constants is not reported. Docstrings and
    dunder settings (`__test__ = False`) are allowed alongside.
    """
    if class_node.decorator_list:
        return False
    if any(_terminal_name(b) not in ("object",) for b in class_node.bases):
        return False
    constants = 0
    for stmt in class_node.body:
        # Docstrings (class and attribute docstrings) and dunder settings
        # such as `__test__ = False` or `__slots__ = ()` do not disqualify.
        if (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str)
        ):
            continue
        if (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
            and stmt.targets[0].id.startswith("__")
            and stmt.targets[0].id.endswith("__")
        ):
            continue
        if not (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
            and _UPPER_CASE.match(stmt.targets[0].id)
            and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, (str, int))
            and not isinstance(stmt.value.value, bool)
        ):
            return False
        constants += 1
    return constants >= 2


def _detect_container_type(class_node: ast.ClassDef) -> str | None:
    """Detect if a class is a dataclass, TypedDict, NamedTuple, Protocol, Enum, ABC,
    a pseudo-enum constants class, or a subclass of dict/list/set."""
    # Check decorators for @dataclass.
    for dec in class_node.decorator_list:
        dec_name = None
        if isinstance(dec, ast.Name):
            dec_name = dec.id
        elif isinstance(dec, ast.Attribute):
            dec_name = dec.attr
        elif isinstance(dec, ast.Call):
            if isinstance(dec.func, ast.Name):
                dec_name = dec.func.id
            elif isinstance(dec.func, ast.Attribute):
                dec_name = dec.func.attr
        if dec_name == "dataclass":
            return "dataclass"

    # Check base classes.
    container_subclass = None
    for base in class_node.bases:
        base_name = None
        if isinstance(base, ast.Name):
            base_name = base.id
        elif isinstance(base, ast.Attribute):
            base_name = base.attr
        elif isinstance(base, ast.Subscript):
            if isinstance(base.value, ast.Name):
                base_name = base.value.id
            elif isinstance(base.value, ast.Attribute):
                base_name = base.value.attr

        if base_name == "TypedDict":
            return "TypedDict"
        elif base_name == "NamedTuple":
            return "NamedTuple"
        elif base_name == "Protocol":
            return "Protocol"
        elif base_name in ("Enum", "IntEnum", "StrEnum", "Flag", "IntFlag"):
            return "Enum"
        elif base_name == "ABC":
            return "ABC"
        elif base_name in _CONTAINER_BASES and container_subclass is None:
            container_subclass = _CONTAINER_BASES[base_name]

    if container_subclass:
        return container_subclass
    if _is_pseudo_enum(class_node):
        return "pseudo-enum"
    return None


def _check_frozen_dataclass(class_node: ast.ClassDef) -> bool:
    """Check if a dataclass has frozen=True."""
    for dec in class_node.decorator_list:
        if isinstance(dec, ast.Call):
            for kw in dec.keywords:
                if kw.arg == "frozen" and isinstance(kw.value, ast.Constant):
                    return bool(kw.value.value)
    return False


# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------


def _is_type_expr(node: ast.AST, alias_names: set[str]) -> bool:
    """Whether the right-hand side of `Name = <node>` is a type expression.

    Deliberately narrow: a subscript of a known generic, `Any`, a `|` union
    that includes a builtin type, `None`, a generic or another alias, or a
    reference to another alias. `Payload = dict[str, Any]` qualifies;
    `value = data["key"]` and `FLAGS = READ | WRITE` do not.
    """
    if _is_any(node):
        return True
    if isinstance(node, ast.Name) and node.id in alias_names:
        return True
    if isinstance(node, ast.Subscript):
        return _terminal_name(node.value) in _GENERIC_NAMES or (
            isinstance(node.value, ast.Name) and node.value.id in alias_names
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):

        def side_ok(n: ast.AST) -> bool:
            return (
                (isinstance(n, ast.Constant) and n.value is None)
                or (isinstance(n, ast.Name) and n.id in _BUILTIN_TYPES)
                or _is_type_expr(n, alias_names)
            )

        def side_typeish(n: ast.AST) -> bool:
            return side_ok(n) or isinstance(n, (ast.Name, ast.Attribute))

        return (side_ok(node.left) or side_ok(node.right)) and (
            side_typeish(node.left) and side_typeish(node.right)
        )
    return False


def _module_aliases(tree: ast.Module) -> dict[str, dict]:
    """Module-level type aliases: `X = <type expr>`, `X: TypeAlias = ...`, `type X = ...`."""
    aliases: dict[str, dict] = {}
    for stmt in tree.body:
        name = value = None
        explicit = False
        if (
            isinstance(stmt, ast.AnnAssign)
            and isinstance(stmt.target, ast.Name)
            and stmt.value is not None
            and _terminal_name(stmt.annotation) == "TypeAlias"
        ):
            name, value, explicit = stmt.target.id, stmt.value, True
        elif type(stmt).__name__ == "TypeAlias":  # `type X = ...` (3.12+)
            name = stmt.name.id  # type: ignore[attr-defined]
            value = stmt.value  # type: ignore[attr-defined]
            explicit = True
        elif (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
            and _is_type_expr(stmt.value, set(aliases))
        ):
            name, value = stmt.targets[0].id, stmt.value
        if name is not None:
            aliases[name] = {"value": value, "line": stmt.lineno, "explicit": explicit}
    return aliases


def _module_dotted(rel: str) -> str:
    parts = list(Path(rel).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _module_imports(tree: ast.Module, dotted: str, is_pkg: bool) -> tuple[dict, dict]:
    """`(from_imports, module_imports)` of a module, relative imports made absolute.

    `from_imports` maps a local name to `(module, original_name)`;
    `module_imports` maps a local name to a module (`import a.b as m`).
    """
    from_imports: dict[str, tuple[str, str]] = {}
    module_imports: dict[str, str] = {}
    package = dotted.split(".") if is_pkg else dotted.split(".")[:-1]
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                keep = len(package) - (node.level - 1)
                if keep < 0:
                    continue
                base = package[:keep]
                module = ".".join(base + ([node.module] if node.module else []))
            else:
                module = node.module or ""
            for alias in node.names:
                if alias.name != "*":
                    from_imports[alias.asname or alias.name] = (module, alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    module_imports[alias.asname] = alias.name
                else:
                    top = alias.name.split(".")[0]
                    module_imports.setdefault(top, top)
    return from_imports, module_imports


class _SymbolIndex:
    """Per-module aliases, imports and functions across the scanned files.

    Resolves a name used in one module to the alias or function it denotes,
    following `from x import Name` (relative or absolute, through re-exports)
    into other scanned modules. Modules outside the scan are never resolved.
    """

    def __init__(self) -> None:
        self.modules: dict[str, dict] = {}
        self._any_memo: dict[tuple[str, str], bool] = {}
        self._find_memo: dict[str, str | None] = {}

    def add(self, rel: str, tree: ast.Module) -> str:
        dotted = _module_dotted(rel)
        from_imports, module_imports = _module_imports(
            tree, dotted, Path(rel).stem == "__init__"
        )
        self.modules[dotted] = {
            "file": rel,
            "aliases": _module_aliases(tree),
            "from_imports": from_imports,
            "module_imports": module_imports,
            "functions": {},
        }
        self._find_memo.clear()
        return dotted

    def find_module(self, name: str) -> str | None:
        """The scanned module *name* refers to: exact, or a unique suffix match
        (an import written against a package root above the scan root)."""
        if name in self._find_memo:
            return self._find_memo[name]
        found: str | None = None
        if name in self.modules:
            found = name
        elif name:
            hits = [m for m in self.modules if m.endswith("." + name)]
            found = hits[0] if len(hits) == 1 else None
        self._find_memo[name] = found
        return found

    def resolve(
        self, module: str, name: str, kind: str = "aliases", _depth: int = 0
    ) -> tuple[str, str] | None:
        """`(module, name)` where *name* as used in *module* is defined as a *kind*."""
        info = self.modules.get(module)
        if info is None or _depth > 10:
            return None
        if name in info[kind]:
            return module, name
        imported = info["from_imports"].get(name)
        if imported is None:
            return None
        target = self.find_module(imported[0])
        if target is None:
            return None
        return self.resolve(target, imported[1], kind, _depth + 1)

    def module_ref(self, module: str, local: str) -> str | None:
        """The scanned module a local name is bound to, if it is one."""
        info = self.modules.get(module)
        if info is None:
            return None
        if local in info["module_imports"]:
            return self.find_module(info["module_imports"][local])
        imported = info["from_imports"].get(local)
        if imported is not None:
            return self.find_module(".".join(p for p in imported if p))
        return None

    def alias_refs(self, module: str, annotation: ast.AST | None) -> list[tuple]:
        """Aliases an annotation names directly, each as `(module, name)`."""
        annotation = _parse_string_annotation(annotation)
        if annotation is None:
            return []
        refs: list[tuple[str, str]] = []
        for node in ast.walk(annotation):
            hit = None
            if isinstance(node, ast.Name):
                hit = self.resolve(module, node.id)
            elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                target = self.module_ref(module, node.value.id)
                if target is not None:
                    hit = self.resolve(target, node.attr)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                refs.extend(self.alias_refs(module, node))
            if hit is not None and hit not in refs:
                refs.append(hit)
        return refs

    def alias_contains_any(self, module: str, name: str) -> bool:
        key = (module, name)
        if key in self._any_memo:
            return self._any_memo[key]
        self._any_memo[key] = False  # cycle guard
        value = self.modules[module]["aliases"][name]["value"]
        result = _contains_any(_parse_string_annotation(value)) or any(
            self.alias_contains_any(*ref) for ref in self.alias_refs(module, value)
        )
        self._any_memo[key] = result
        return result

    def any_aliases(self, module: str, annotation: ast.AST | None) -> list[str]:
        """Names of the Any-carrying aliases an annotation uses (empty if none)."""
        return [
            name
            for ref_module, name in self.alias_refs(module, annotation)
            if self.alias_contains_any(ref_module, name)
        ]


# ---------------------------------------------------------------------------
# Per-file analysis
# ---------------------------------------------------------------------------


def _function_any_usage(rel: str, func_info: dict) -> dict:
    """One `any_usages[]` entry for a function or method.

    Every entry, whatever its `kind`, carries `file` and `line`; `location` is
    the same pair as one `file:line` string. `details` lists the parameters
    whose annotation contains Any and is empty when Any is in the return
    annotation only (then `in_return` is true). `nested` marks a function
    defined inside another function.
    """
    entry = {
        "kind": "function",
        "file": rel,
        "line": func_info["line"],
        "location": f"{rel}:{func_info['line']}",
        "function": func_info["name"],
        "details": func_info["any_in_params"],
        "in_return": func_info["any_in_return"],
    }
    if func_info.get("nested"):
        entry["nested"] = True
    return entry


def _positional_params(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.arg]:
    return node.args.posonlyargs + node.args.args


def _is_typed_def(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether mypy checks this body by default: any annotation at all."""
    a = node.args
    every = (
        a.posonlyargs + a.args + a.kwonlyargs + [x for x in (a.vararg, a.kwarg) if x]
    )
    return node.returns is not None or any(x.annotation is not None for x in every)


def _iter_nested_defs(node: ast.AST, prefix: str):
    """Yield `(def_node, qualified_name)` for defs nested inside a function body.

    Recurses through nested defs and through classes defined in the body (their
    methods are nested too: they exist only per call of the outer function).
    """
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            name = f"{prefix}.{child.name}"
            yield child, name
            yield from _iter_nested_defs(child, name)
        elif isinstance(child, ast.ClassDef):
            yield from _iter_nested_defs(child, f"{prefix}.{child.name}")
        elif not isinstance(child, ast.Lambda):
            yield from _iter_nested_defs(child, prefix)


def _walk_own_body(node: ast.AST):
    """`ast.walk` over a function body without entering nested defs or classes."""
    stack = list(ast.iter_child_nodes(node))
    while stack:
        cur = stack.pop()
        yield cur
        if isinstance(
            cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
        ):
            continue
        stack.extend(ast.iter_child_nodes(cur))


def analyze_file(
    filepath: Path,
    project_root: Path,
    *,
    index: _SymbolIndex | None = None,
    collector: dict | None = None,
) -> dict:
    """Analyze type annotations in a file.

    *index* resolves type aliases and functions across the scanned modules;
    without one, only the file's own aliases are followed. *collector*, when
    given, receives the per-parameter and per-call data the project-level
    checks need (`param_sites`, `calls`).
    """
    rel = str(filepath.relative_to(project_root))
    result: dict = {
        "file": rel,
        "functions": [],
        "nested_functions": [],
        "classes": [],
        "any_usages": [],
        "any_via_alias_usages": [],
        "type_ignore_count": 0,
        "untyped_containers": [],
        "parse_error": None,
        # Filled in below; stays None when the file could not be parsed.
        "coverage": None,
    }

    try:
        source = filepath.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(filepath))
    except SyntaxError as exc:
        result["parse_error"] = f"SyntaxError: {exc.msg} (line {exc.lineno})"
        return result

    if index is None:
        index = _SymbolIndex()
    module = _module_dotted(rel)
    if index.modules.get(module, {}).get("file") != rel:
        module = index.add(rel, tree)
    lines = source.splitlines()

    # Count type: ignore comments.
    for line in lines:
        if re.search(r"#\s*type:\s*ignore", line):
            result["type_ignore_count"] += 1

    param_sites: list[dict] = []
    calls: list[dict] = []

    def _alias_entry(kind: str, line: int, **extra) -> dict:
        return {"kind": kind, "file": rel, "line": line, **extra}

    # Analyze functions.
    def _analyze_func(
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        class_name: str | None = None,
        qualname: str | None = None,
        nested: bool = False,
    ) -> dict:
        name = qualname or (f"{class_name}.{node.name}" if class_name else node.name)
        is_public = not node.name.startswith("_") or node.name in _PUBLIC_DUNDERS

        # Check parameter annotations.
        all_args = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
        # Exclude self/cls.
        params = [a for a in all_args if a.arg not in ("self", "cls")]
        annotated_params = [a for a in params if a.annotation is not None]
        has_return = node.returns is not None

        # Check for Any in annotations.
        any_in_params = [
            {"param": a.arg, "annotation": _annotation_to_str(a.annotation)}
            for a in params
            if _contains_any(a.annotation)
        ]
        any_in_return = _contains_any(node.returns)

        total_params = len(params)
        annotated_count = len(annotated_params)
        fully_annotated = (
            (annotated_count == total_params and has_return)
            if total_params > 0
            else has_return
        )

        info = {
            "name": name,
            "line": node.lineno,
            "is_public": is_public,
            "is_method": class_name is not None,
            "total_params": total_params,
            "annotated_params": annotated_count,
            "has_return_annotation": has_return,
            "return_annotation": _annotation_to_str(node.returns),
            "fully_annotated": fully_annotated,
            "any_in_params": any_in_params,
            "any_in_return": any_in_return,
        }
        if nested:
            info["nested"] = True

        # Any reached only through an alias (`body: Payload` where
        # `Payload = dict[str, Any]`): invisible to any_in_params.
        via_params = []
        for a in params:
            if a.annotation is not None and not _contains_any(a.annotation):
                aliases = index.any_aliases(module, a.annotation)
                if aliases:
                    via_params.append(
                        {
                            "param": a.arg,
                            "annotation": _annotation_to_str(a.annotation),
                            "aliases": aliases,
                        }
                    )
        via_return = (
            index.any_aliases(module, node.returns) if not any_in_return else []
        )
        if via_params or via_return:
            result["any_via_alias_usages"].append(
                _alias_entry(
                    "function",
                    node.lineno,
                    function=name,
                    details=via_params,
                    in_return=bool(via_return),
                    return_aliases=via_return,
                    sites=len(via_params) + bool(via_return),
                )
            )

        for a in params:
            param_sites.append(
                {
                    "param": a.arg,
                    "annotation": _annotation_to_str(a.annotation),
                    "file": rel,
                    "line": node.lineno,
                    "function": name,
                }
            )
        _collect_calls(node, name, class_name)
        return info

    def _collect_calls(
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        name: str,
        class_name: str | None,
    ) -> None:
        """Calls made from a body mypy checks by default (a typed def)."""
        if not _is_typed_def(node):
            return
        known: dict[str, str] = {}
        for a in node.args.posonlyargs + node.args.args + node.args.kwonlyargs:
            if a.annotation is not None and not _contains_any(a.annotation):
                known[a.arg] = _annotation_to_str(a.annotation)
        self_name = None
        if class_name is not None and _positional_params(node):
            self_name = _positional_params(node)[0].arg
        for sub in _walk_own_body(node):
            if (
                isinstance(sub, ast.AnnAssign)
                and isinstance(sub.target, ast.Name)
                and not _contains_any(sub.annotation)
            ):
                known[sub.target.id] = _annotation_to_str(sub.annotation)
        for sub in _walk_own_body(node):
            if not isinstance(sub, ast.Call):
                continue
            func = sub.func
            if isinstance(func, ast.Name):
                callee = ("name", func.id)
            elif (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and self_name is not None
                and func.value.id == self_name
            ):
                callee = ("method", class_name, func.attr)
            elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                callee = ("modattr", func.value.id, func.attr)
            else:
                continue
            args: list[dict] = []
            for i, arg in enumerate(sub.args):
                if isinstance(arg, ast.Starred):
                    break
                args.append({"position": i, **_arg_info(arg, known)})
            for kw in sub.keywords:
                if kw.arg is not None:
                    args.append({"keyword": kw.arg, **_arg_info(kw.value, known)})
            if args:
                calls.append(
                    {
                        "module": module,
                        "file": rel,
                        "line": sub.lineno,
                        "caller": name,
                        "callee": callee,
                        "args": args,
                    }
                )

    def _arg_info(arg: ast.AST, known: dict[str, str]) -> dict:
        text = ast.unparse(arg)
        return {
            "arg": text if len(text) <= 60 else text[:57] + "...",
            "arg_annotation": known.get(arg.id) if isinstance(arg, ast.Name) else None,
        }

    def _add_nested(node: ast.AST, prefix: str) -> None:
        for child, qualname in _iter_nested_defs(node, prefix):
            info = _analyze_func(child, qualname=qualname, nested=True)
            result["nested_functions"].append(info)
            if info["any_in_params"] or info["any_in_return"]:
                result["any_usages"].append(_function_any_usage(rel, info))

    # Analyze classes.
    def _analyze_class(node: ast.ClassDef) -> dict:
        container_type = _detect_container_type(node)
        is_public = not node.name.startswith("_")

        # Count annotated class attributes.
        annotated_attrs: list[dict] = []
        unannotated_attrs: list[str] = []
        enum_members: list[str] = []
        for child in node.body:
            if isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
                annotated_attrs.append(
                    {
                        "name": child.target.id,
                        "line": child.lineno,
                        "annotation": _annotation_to_str(child.annotation),
                        "has_any": _contains_any(child.annotation),
                    }
                )
            elif isinstance(child, ast.Assign):
                for target in child.targets:
                    if isinstance(target, ast.Name) and not target.id.startswith("_"):
                        # Enum members must NOT be annotated (typing spec), so
                        # they are members, not unannotated attributes.
                        if container_type == "Enum":
                            enum_members.append(target.id)
                        else:
                            unannotated_attrs.append(target.id)

        # Analyze methods.
        methods = []
        method_nodes = []
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                methods.append(_analyze_func(child, node.name))
                method_nodes.append(child)

        info: dict = {
            "name": node.name,
            "line": node.lineno,
            "is_public": is_public,
            "container_type": container_type,
            "annotated_attributes": annotated_attrs,
            "unannotated_attributes": unannotated_attrs,
            "instance_attributes": _instance_attributes(
                method_nodes, {a["name"]: a["annotation"] for a in annotated_attrs}
            ),
            "methods": methods,
        }
        if container_type == "Enum":
            info["enum_members"] = enum_members

        if container_type == "dataclass":
            info["frozen"] = _check_frozen_dataclass(node)

        for child in method_nodes:
            _add_nested(child, f"{node.name}.{child.name}")
        return info

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func_info = _analyze_func(node)
            result["functions"].append(func_info)
            # Track Any usage.
            if func_info["any_in_params"] or func_info["any_in_return"]:
                result["any_usages"].append(_function_any_usage(rel, func_info))
            _add_nested(node, node.name)
        elif isinstance(node, ast.ClassDef):
            cls_info = _analyze_class(node)
            result["classes"].append(cls_info)
            # Track Any in class attributes.
            for attr in cls_info["annotated_attributes"]:
                if attr["has_any"]:
                    result["any_usages"].append(
                        {
                            "kind": "class_attribute",
                            "file": rel,
                            "line": attr["line"],
                            # `location` keeps the class line it always had, so
                            # diff_findings keys built on it stay stable.
                            "location": f"{rel}:{node.lineno}",
                            "class": cls_info["name"],
                            "attribute": attr["name"],
                            "annotation": attr["annotation"],
                        }
                    )
            for attr in cls_info["instance_attributes"]:
                if attr["typed_via"] == "annotation" and attr["has_any"]:
                    result["any_usages"].append(
                        {
                            "kind": "instance_attribute",
                            "file": rel,
                            "line": attr["line"],
                            "location": f"{rel}:{attr['line']}",
                            "class": cls_info["name"],
                            "attribute": attr["name"],
                            "annotation": attr["annotation"],
                        }
                    )
            # Track methods' Any usage.
            for method in cls_info["methods"]:
                if method["any_in_params"] or method["any_in_return"]:
                    result["any_usages"].append(_function_any_usage(rel, method))
            # Any through an alias in class-body and `self.x: T` annotations
            # (a local variable annotation inside a method is not an attribute).
            attr_nodes = [c for c in node.body if isinstance(c, ast.AnnAssign)]
            for method in node.body:
                if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    attr_nodes.extend(
                        c
                        for c in _walk_own_body(method)
                        if isinstance(c, ast.AnnAssign)
                        and isinstance(c.target, ast.Attribute)
                    )
            for child in attr_nodes:
                target = child.target
                if _contains_any(child.annotation):
                    continue
                aliases = index.any_aliases(module, child.annotation)
                if aliases:
                    result["any_via_alias_usages"].append(
                        _alias_entry(
                            "attribute",
                            child.lineno,
                            **{"class": node.name},
                            attribute=ast.unparse(target),
                            annotation=_annotation_to_str(child.annotation),
                            aliases=aliases,
                            sites=1,
                        )
                    )

    result["untyped_containers"] = _untyped_containers(tree, lines)
    result["coverage"] = _file_coverage(
        _all_file_functions(result), result["nested_functions"]
    )
    if collector is not None:
        collector.setdefault("param_sites", []).extend(param_sites)
        collector.setdefault("calls", []).extend(calls)
        functions = index.modules[module]["functions"]
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions[node.name] = _signature(node, rel, node.name, False)
            elif isinstance(node, ast.ClassDef):
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        functions[f"{node.name}.{child.name}"] = _signature(
                            child, rel, f"{node.name}.{child.name}", True
                        )
    return result


def _signature(
    node: ast.FunctionDef | ast.AsyncFunctionDef, rel: str, name: str, is_method: bool
) -> dict:
    """What the untyped-boundary check needs to map call arguments to params."""
    decorators = {_terminal_name(d) for d in node.decorator_list}
    positional = _positional_params(node)
    if is_method and "staticmethod" not in decorators and positional:
        positional = positional[1:]
    return {
        "file": rel,
        "name": name,
        "line": node.lineno,
        "typed": _is_typed_def(node),
        "positional": [(a.arg, a.annotation is not None) for a in positional],
        "keyword": {
            a.arg: a.annotation is not None for a in (positional + node.args.kwonlyargs)
        },
    }


def _instance_attributes(
    methods: list[ast.FunctionDef | ast.AsyncFunctionDef],
    class_annotations: dict[str, str | None],
) -> list[dict]:
    """`self.x` attributes assigned in `__init__` / `__post_init__`.

    `typed_via` says where the attribute's type comes from: `annotation`
    (`self.x: T = ...`), `class_annotation` (`x: T` in the class body),
    `parameter` (`self.x = x` where the parameter `x` is annotated -- mypy
    infers the attribute's type from it), or null (nothing types it).
    """
    found: dict[str, dict] = {}
    for method in methods:
        if method.name not in _INIT_METHODS or not _positional_params(method):
            continue
        self_name = _positional_params(method)[0].arg
        param_ann = {
            a.arg: a.annotation
            for a in method.args.posonlyargs + method.args.args + method.args.kwonlyargs
            if a.annotation is not None
        }
        for sub in _walk_own_body(method):
            if isinstance(sub, ast.AnnAssign):
                targets, annotation, value = [sub.target], sub.annotation, sub.value
            elif isinstance(sub, ast.Assign):
                targets, annotation, value = sub.targets, None, sub.value
            else:
                continue
            for target in targets:
                if not (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == self_name
                ):
                    continue
                name = target.attr
                if annotation is not None:
                    typed_via, ann = "annotation", annotation
                elif name in class_annotations:
                    typed_via, ann = "class_annotation", None
                elif isinstance(value, ast.Name) and value.id in param_ann:
                    typed_via, ann = "parameter", param_ann[value.id]
                else:
                    typed_via, ann = None, None
                entry = {
                    "name": name,
                    "line": sub.lineno,
                    "annotation": (
                        class_annotations[name]
                        if typed_via == "class_annotation"
                        else _annotation_to_str(ann)
                    ),
                    "typed_via": typed_via,
                    "has_any": _contains_any(ann),
                }
                previous = found.get(name)
                # The first assignment wins unless a later one types it.
                if previous is None or (
                    previous["typed_via"] is None and typed_via is not None
                ):
                    found[name] = entry
    return sorted(found.values(), key=lambda e: e["line"])


def _untyped_containers(tree: ast.Module, lines: list[str]) -> list[dict]:
    """Bare `list`/`dict`/`set`/`tuple` (and typing aliases) in annotations.

    Walks the annotation nodes themselves (parameters, returns, variable
    annotations), so `-> tuple:` is found and a docstring or dict literal
    mentioning `list` is not.
    """
    found: dict[tuple[int, str], dict] = {}

    def check(annotation: ast.AST | None, where: str) -> None:
        if annotation is None:
            return
        # A quoted annotation's parsed nodes carry lines relative to the
        # string, so every hit in one is anchored on the annotation's line.
        anchor = annotation.lineno if isinstance(annotation, ast.Constant) else None
        annotation = _parse_string_annotation(annotation)
        if annotation is None:
            return
        subscripted = {
            id(n.value) for n in ast.walk(annotation) if isinstance(n, ast.Subscript)
        }
        for n in ast.walk(annotation):
            name = _terminal_name(n)
            if name in _BARE_CONTAINERS and id(n) not in subscripted:
                if isinstance(n, ast.Attribute) and not isinstance(n.value, ast.Name):
                    continue
                line = anchor or getattr(n, "lineno", None) or 0
                key = (line, where)
                if key not in found:
                    content = lines[line - 1].strip() if 0 < line <= len(lines) else ""
                    found[key] = {
                        "line": line,
                        "content": content,
                        "annotation": name,
                        "where": where,
                    }

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a = node.args
            for arg in a.posonlyargs + a.args + a.kwonlyargs:
                check(arg.annotation, f"param {arg.arg} of {node.name}")
            for arg in (a.vararg, a.kwarg):
                if arg is not None:
                    check(arg.annotation, f"param {arg.arg} of {node.name}")
            check(node.returns, f"return of {node.name}")
        elif isinstance(node, ast.AnnAssign):
            check(node.annotation, f"variable {ast.unparse(node.target)}")
    return sorted(found.values(), key=lambda e: (e["line"], e["where"]))


def _percent(part: int, whole: int) -> float | None:
    """`part/whole` as a percentage; None when there is nothing to measure."""
    return round(100 * part / whole, 1) if whole else None


def _file_coverage(funcs: list[dict], nested: list[dict] | None = None) -> dict:
    """Per-file annotation coverage over functions AND methods.

    `files[].functions` lists module-level functions only, while the summary
    counts methods too; coverage computed from `functions` alone is badly
    wrong for class-heavy modules (a module with 2 functions and 160
    methods reads as 2). A file with no functions reports null, not 0%.
    Nested functions (defined inside another function) are counted
    separately so the established totals keep their meaning.
    """
    public = [f for f in funcs if f["is_public"]]
    annotated = sum(1 for f in funcs if f["fully_annotated"])
    annotated_public = sum(1 for f in public if f["fully_annotated"])
    nested = nested or []
    nested_annotated = sum(1 for f in nested if f["fully_annotated"])
    return {
        "total_functions": len(funcs),
        "public_functions": len(public),
        "fully_annotated_functions": annotated,
        "fully_annotated_public": annotated_public,
        "annotation_coverage_all": _percent(annotated, len(funcs)),
        "annotation_coverage_public": _percent(annotated_public, len(public)),
        "nested_functions": len(nested),
        "fully_annotated_nested": nested_annotated,
        "annotation_coverage_nested": _percent(nested_annotated, len(nested)),
    }


def _all_file_functions(file_analysis: dict) -> list[dict]:
    """Module-level functions followed by class methods, in summary order."""
    funcs = list(file_analysis["functions"])
    for cls in file_analysis["classes"]:
        funcs.extend(cls["methods"])
    return funcs


# ---------------------------------------------------------------------------
# Project-level checks
# ---------------------------------------------------------------------------


def _inconsistent_parameters(param_sites: list[dict]) -> list[dict]:
    """Parameter names annotated at some definitions and bare at others.

    `session: Session` in a few functions and bare `session` in many is an
    unchecked contract at each bare one, and the annotated sites already say what
    the type is. `annotations` counts each annotation used (most common
    first, also as `dominant_annotation`); `consistent` is true when the
    annotated sites all agree, which is when copying the annotation is safe.
    Ranked by how many bare sites there are.
    """
    by_name: dict[str, list[dict]] = {}
    for site in param_sites:
        if site["param"] in ("_", "args", "kwargs"):
            continue
        by_name.setdefault(site["param"], []).append(site)
    out = []
    for name, sites in by_name.items():
        typed = [s for s in sites if s["annotation"] is not None]
        bare = [s for s in sites if s["annotation"] is None]
        if not typed or not bare:
            continue
        annotations: dict[str, int] = {}
        for s in typed:
            annotations[s["annotation"]] = annotations.get(s["annotation"], 0) + 1
        ranked = dict(sorted(annotations.items(), key=lambda kv: (-kv[1], kv[0])))
        out.append(
            {
                "param": name,
                "annotated": len(typed),
                "unannotated": len(bare),
                "dominant_annotation": next(iter(ranked)),
                "annotations": ranked,
                "consistent": len(annotations) == 1,
                "files": len({s["file"] for s in sites}),
                "unannotated_sites": [
                    {"file": s["file"], "line": s["line"], "function": s["function"]}
                    for s in bare[:10]
                ],
            }
        )
    out.sort(key=lambda e: (-e["unannotated"], e["param"]))
    return out


def _resolve_callee(index: _SymbolIndex, call: dict) -> dict | None:
    module = call["module"]
    callee = call["callee"]
    if callee[0] == "name":
        hit = index.resolve(module, callee[1], "functions")
    elif callee[0] == "method":
        hit = (module, f"{callee[1]}.{callee[2]}")
    else:
        target = index.module_ref(module, callee[1])
        hit = index.resolve(target, callee[2], "functions") if target else None
    if hit is None:
        return None
    return index.modules[hit[0]]["functions"].get(hit[1])


def _untyped_boundaries(index: _SymbolIndex, calls: list[dict]) -> list[dict]:
    """Unannotated parameters of in-scope functions that typed code passes values to.

    The caller is a typed def, so mypy checks its body and knows the argument's
    type; the callee's parameter is bare, so that type is dropped at the call
    and nothing inside the callee is checked against it. Annotating the
    parameter (with `arg_annotations`, when the caller's names say what it is)
    and re-running mypy is how a defect behind the boundary becomes provable.
    """
    groups: dict[tuple, dict] = {}
    for call in calls:
        sig = _resolve_callee(index, call)
        if sig is None:
            continue
        for arg in call["args"]:
            if "position" in arg:
                if arg["position"] >= len(sig["positional"]):
                    continue
                param, annotated = sig["positional"][arg["position"]]
            else:
                if arg["keyword"] not in sig["keyword"]:
                    continue
                param, annotated = arg["keyword"], sig["keyword"][arg["keyword"]]
            if annotated:
                continue
            key = (sig["file"], sig["name"], param)
            group = groups.setdefault(
                key,
                {
                    "callee": sig["name"],
                    "file": sig["file"],
                    "line": sig["line"],
                    "param": param,
                    "callee_typed": sig["typed"],
                    "call_sites": 0,
                    "arg_annotations": {},
                    "sample_call_sites": [],
                },
            )
            group["call_sites"] += 1
            if arg["arg_annotation"]:
                anns = group["arg_annotations"]
                anns[arg["arg_annotation"]] = anns.get(arg["arg_annotation"], 0) + 1
            if len(group["sample_call_sites"]) < 5:
                group["sample_call_sites"].append(
                    {
                        "file": call["file"],
                        "line": call["line"],
                        "caller": call["caller"],
                        "arg": arg["arg"],
                    }
                )
    out = list(groups.values())
    out.sort(key=lambda g: (not g["arg_annotations"], -g["call_sites"], g["callee"]))
    return out


def _alias_inventory(index: _SymbolIndex, uses: dict[tuple, int]) -> list[dict]:
    inventory = []
    for module, info in sorted(index.modules.items()):
        for name, alias in info["aliases"].items():
            inventory.append(
                {
                    "name": name,
                    "file": info["file"],
                    "line": alias["line"],
                    "definition": ast.unparse(alias["value"]),
                    "explicit": alias["explicit"],
                    "contains_any": index.alias_contains_any(module, name),
                    "annotation_uses": uses.get((module, name), 0),
                }
            )
    inventory.sort(key=lambda a: (-a["annotation_uses"], a["file"], a["line"]))
    return inventory


def _count_alias_uses(index: _SymbolIndex) -> dict:
    """How many annotation sites name each alias (directly)."""
    uses: dict[tuple, int] = {}
    for module, info in index.modules.items():
        path = info.get("path")
        if path is None:
            continue
        tree = info["tree"]
        for node in ast.walk(tree):
            annotations: list[ast.AST | None] = []
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                a = node.args
                annotations = [
                    x.annotation for x in a.posonlyargs + a.args + a.kwonlyargs
                ]
                annotations.append(node.returns)
            elif isinstance(node, ast.AnnAssign):
                annotations = [node.annotation]
            for ann in annotations:
                if ann is None:
                    continue
                for ref in index.alias_refs(module, ann):
                    uses[ref] = uses.get(ref, 0) + 1
    return uses


def analyze(target: str, *, max_files: int = 0) -> dict:
    target_path = Path(target).resolve()
    project_root = find_project_root(target_path)
    scan_root = target_path if target_path.is_dir() else project_root
    all_files = sorted(discover_python_files(scan_root))
    files_total = len(all_files)
    if max_files > 0 and files_total > max_files:
        all_files = all_files[:max_files]

    # Pass 1: every module's aliases and imports, so an alias defined in one
    # module resolves where another module imports it.
    index = _SymbolIndex()
    for path in all_files:
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=str(path))
        except (SyntaxError, ValueError, RecursionError):
            continue
        dotted = index.add(str(path.relative_to(project_root)), tree)
        index.modules[dotted]["path"] = path
        index.modules[dotted]["tree"] = tree

    collector: dict = {}
    file_analyses = [
        analyze_file(f, project_root, index=index, collector=collector)
        for f in all_files
    ]

    # Aggregate statistics.
    all_functions: list[dict] = []
    all_nested: list[dict] = []
    all_classes: list[dict] = []
    all_any: list[dict] = []
    all_any_alias: list[dict] = []
    total_type_ignores = 0

    for fa in file_analyses:
        all_functions.extend(fa["functions"])
        all_nested.extend(fa["nested_functions"])
        all_classes.extend(fa["classes"])
        all_any.extend(fa["any_usages"])
        all_any_alias.extend(fa["any_via_alias_usages"])
        total_type_ignores += fa["type_ignore_count"]
        # Include methods from classes.
        for cls in fa["classes"]:
            all_functions.extend(cls["methods"])

    public_funcs = [f for f in all_functions if f["is_public"]]
    fully_annotated = [f for f in all_functions if f["fully_annotated"]]
    public_fully_annotated = [f for f in public_funcs if f["fully_annotated"]]
    nested_annotated = [f for f in all_nested if f["fully_annotated"]]

    # Container type inventory.
    container_types: dict[str, list[str]] = {}
    for cls in all_classes:
        ct = cls["container_type"]
        if ct:
            container_types.setdefault(ct, []).append(cls["name"])

    # Classes whose attributes (class-level, and instance attributes set in
    # __init__) are all typed. Enum members are not attributes to annotate.
    with_attrs = 0
    attrs_typed = 0
    for cls in all_classes:
        n_attrs = (
            len(cls["annotated_attributes"])
            + len(cls["unannotated_attributes"])
            + len(cls["instance_attributes"])
        )
        if not n_attrs:
            continue
        with_attrs += 1
        if not cls["unannotated_attributes"] and all(
            a["typed_via"] is not None for a in cls["instance_attributes"]
        ):
            attrs_typed += 1

    modules_with_functions = [
        fa
        for fa in file_analyses
        if fa["coverage"] and fa["coverage"]["total_functions"]
    ]
    modules_complete = [
        fa
        for fa in modules_with_functions
        if fa["coverage"]["fully_annotated_functions"]
        == fa["coverage"]["total_functions"]
    ]

    # Unannotated public functions. `file` is needed to tell apart same-named
    # functions in different modules (one `build` per plugin module).
    unannotated_public = [
        {"file": fa["file"], "name": f["name"], "line": f["line"]}
        for fa in file_analyses
        for f in _all_file_functions(fa)
        if f["is_public"] and not f["fully_annotated"]
    ]
    untyped_containers = [
        {"file": fa["file"], **entry}
        for fa in file_analyses
        for entry in fa["untyped_containers"]
    ]

    inconsistent = _inconsistent_parameters(collector.get("param_sites", []))
    boundaries = _untyped_boundaries(index, collector.get("calls", []))
    alias_uses = _count_alias_uses(index)
    aliases = _alias_inventory(index, alias_uses)

    output = {
        **build_envelope(project_root, scan_root, files_total, len(all_files)),
        "summary": {
            "total_functions": len(all_functions),
            "public_functions": len(public_funcs),
            "fully_annotated_functions": len(fully_annotated),
            "fully_annotated_public": len(public_fully_annotated),
            "annotation_coverage_all": (
                round(100 * len(fully_annotated) / len(all_functions), 1)
                if all_functions
                else 0
            ),
            "annotation_coverage_public": (
                round(100 * len(public_fully_annotated) / len(public_funcs), 1)
                if public_funcs
                else 0
            ),
            # Functions defined inside other functions (route handlers in a
            # router factory, closures). NOT included in total_functions.
            "nested_functions": len(all_nested),
            "fully_annotated_nested": len(nested_annotated),
            "annotation_coverage_nested": _percent(
                len(nested_annotated), len(all_nested)
            ),
            "modules_with_functions": len(modules_with_functions),
            "modules_fully_annotated": len(modules_complete),
            "total_classes": len(all_classes),
            "classes_with_attributes": with_attrs,
            "classes_attributes_fully_typed": attrs_typed,
            "total_any_usages": len(all_any),
            # Annotations whose Any is reached only through a type alias
            # (`body: Payload` with `Payload = dict[str, Any]`). Not part of
            # total_any_usages: entries are per function / attribute, `sites`
            # per annotation.
            "total_any_via_alias_usages": len(all_any_alias),
            "any_via_alias_sites": sum(e["sites"] for e in all_any_alias),
            "type_aliases": len(aliases),
            "total_type_ignores": total_type_ignores,
            "untyped_containers": len(untyped_containers),
            "inconsistent_parameters": len(inconsistent),
            "untyped_boundaries": len(boundaries),
            "container_types": {k: len(v) for k, v in container_types.items()},
        },
        "container_inventory": {k: sorted(v) for k, v in container_types.items()},
        "any_usages": all_any,
        "any_via_alias_usages": all_any_alias,
        "alias_inventory": aliases,
        # Report the DENOMINATOR with the cap. A bare list of 50 reads as a
        # count of 50; on idlelib the true figure is 2079, and a reviewer who
        # quoted "50 unannotated public functions" would be off by 40x.
        "unannotated_public_functions_total": len(unannotated_public),
        "unannotated_public_functions_capped": len(unannotated_public) > _LIST_CAP,
        "unannotated_public_functions": unannotated_public[:_LIST_CAP],
        "untyped_containers_total": len(untyped_containers),
        "untyped_containers_capped": len(untyped_containers) > 2 * _LIST_CAP,
        "untyped_containers": untyped_containers[: 2 * _LIST_CAP],
        "inconsistent_parameters_total": len(inconsistent),
        "inconsistent_parameters_capped": len(inconsistent) > _LIST_CAP,
        "inconsistent_parameters": inconsistent[:_LIST_CAP],
        "untyped_boundaries_total": len(boundaries),
        "untyped_boundaries_capped": len(boundaries) > _LIST_CAP,
        "untyped_boundaries": boundaries[:_LIST_CAP],
        "files": file_analyses,
    }
    return output


def main() -> None:
    target, max_files = parse_common_args(sys.argv[1:], __doc__)
    emit(analyze(target, max_files=max_files))


if __name__ == "__main__":
    main()
