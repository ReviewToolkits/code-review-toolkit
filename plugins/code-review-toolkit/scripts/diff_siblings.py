#!/usr/bin/env python3
"""Diff sibling functions: what most of them do that a few don't.

Backs the consistency-auditor's sibling comparison, which found the most
damaging consistency bugs of past reviews by hand. Two passes:

- ``sibling_outliers``: functions are grouped as siblings -- the methods of
  one class, the methods or module functions sharing a name prefix
  (``create_*``), and the functions project-wide that share a registering
  decorator (``@route``, ``@task``) -- and each one's features are collected:
  calls, decorators, decorator keywords (``permission=``), keywords passed to
  calls (``timeout=``), context managers (``with lock:``) and early-exit guards
  (``if not x: raise``). A feature most siblings have and a few lack is listed
  with the siblings that lack it.
- ``helper_bypasses``: per class, a helper most methods call to reach an
  attribute (``self._conn()`` wrapping ``self._db``), and the methods that
  touch that attribute directly instead.

Each entry is a candidate for reading, not a finding: a sibling may lack a
call because it does not need it.

Usage:
    python diff_siblings.py [path] [--max-files N] [--include-tests]
        [--min-group N] [--min-share F]
"""

from __future__ import annotations

import ast
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scan_common import (  # noqa: E402
    build_envelope,
    collect_python_files,
    emit,
    parse_common_args,
    relative_to_root,
    resolve_target,
)
from survey_docstrings import is_stub, is_test_file  # noqa: E402

_FUNC_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)
# Calls too generic to say anything about a sibling.
_TRIVIAL_CALLS = frozenset({
    "len", "str", "int", "float", "bool", "list", "dict", "set", "tuple",
    "isinstance", "issubclass", "getattr", "hasattr", "setattr", "super", "range",
    "enumerate", "zip", "sorted", "reversed", "min", "max", "sum", "any", "all",
    "iter", "next", "type", "repr", "print", "map", "filter", "abs", "round",
    "format", "id", "hash", "callable", "vars", "frozenset", "bytes", "object",
})  # fmt: skip
_INERT_DECORATORS = frozenset({
    "staticmethod", "classmethod", "property", "cached_property", "abstractmethod",
    "override", "final", "overload", "wraps", "contextmanager",
    "asynccontextmanager", "setter", "getter", "deleter", "cache", "lru_cache",
})  # fmt: skip
_MAX_OUTLIERS = 150
_MAX_FEATURES_PER_GROUP = 8


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Call):
        return _dotted(node.func)
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        inner = _dotted(node.value)
        return f"{inner}.{node.attr}" if inner else node.attr
    return ""


def _own_nodes(func: ast.AST):
    """Nodes of *func*'s body (not its decorators or defaults), not descending
    into nested defs or classes."""
    stack = list(func.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (*_FUNC_TYPES, ast.ClassDef, ast.Lambda)):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def _const(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(
        node.value, (bool, int, str, type(None))
    ):
        text = repr(node.value)
        return text if len(text) <= 40 else None
    return None


def _exits(body: list[ast.stmt]) -> bool:
    return bool(body) and isinstance(body[-1], (ast.Raise, ast.Return, ast.Continue))


def features(func: ast.AST) -> set[str]:
    """What *func* does, as comparable feature strings."""
    out: set[str] = set()
    for dec in func.decorator_list:
        name = _dotted(dec)
        if not name or name.rsplit(".", 1)[-1] in _INERT_DECORATORS:
            continue
        out.add(f"decorator:{name}")
        if isinstance(dec, ast.Call):
            for kw in dec.keywords:
                if kw.arg:
                    out.add(f"decorator_kwarg:{name}({kw.arg}=)")
                    value = _const(kw.value)
                    if value is not None:
                        out.add(f"decorator_kwarg:{name}({kw.arg}={value})")
    for node in _own_nodes(func):
        if isinstance(node, ast.Call):
            name = _dotted(node.func)
            if not name or name in _TRIVIAL_CALLS:
                continue
            out.add(f"call:{name}")
            for kw in node.keywords:
                if kw.arg:
                    out.add(f"kwarg:{name}({kw.arg}=)")
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                name = _dotted(item.context_expr)
                if name:
                    out.add(f"with:{name}")
        elif isinstance(node, ast.If) and _exits(node.body):
            for sub in ast.walk(node.test):
                if isinstance(sub, (ast.Name, ast.Attribute)):
                    name = _dotted(sub)
                    if name and name not in ("None", "True", "False", "self", "cls"):
                        out.add(f"guard:{name}")
    return out


class _Member:
    __slots__ = ("feats", "file", "line", "name", "node")

    def __init__(self, name: str, file: str, node: ast.AST) -> None:
        self.name = name
        self.file = file
        self.line = node.lineno
        self.node = node
        self.feats = features(node)


def _prefix(name: str) -> str | None:
    head, sep, rest = name.lstrip("_").partition("_")
    return head if sep and rest and len(head) >= 3 else None


def _registering_decorators(func: ast.AST) -> list[str]:
    names = []
    for dec in func.decorator_list:
        name = _dotted(dec)
        if name and name.rsplit(".", 1)[-1] not in _INERT_DECORATORS:
            names.append(name.rsplit(".", 1)[-1])
    return names


def collect_groups(
    parsed: list[tuple[str, ast.Module]], min_group: int
) -> dict[str, list[_Member]]:
    """Sibling groups: a class's methods, functions sharing a name prefix
    (per class or per module), and functions sharing a registering decorator
    across the project."""
    groups: dict[str, list[_Member]] = {}

    def add(key: str, member: _Member) -> None:
        groups.setdefault(key, []).append(member)

    for rel, tree in parsed:
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                for child in node.body:
                    if not isinstance(child, _FUNC_TYPES) or is_stub(child):
                        continue
                    if child.name.startswith("__") and child.name.endswith("__"):
                        continue
                    member = _Member(f"{node.name}.{child.name}", rel, child)
                    add(f"class {rel}::{node.name}", member)
                    prefix = _prefix(child.name)
                    if prefix:
                        add(f"prefix {rel}::{node.name}.{prefix}_*", member)
                    for dec in _registering_decorators(child):
                        add(f"decorator @{dec}", member)
            elif isinstance(node, _FUNC_TYPES) and not is_stub(node):
                member = _Member(node.name, rel, node)
                prefix = _prefix(node.name)
                if prefix:
                    add(f"prefix {rel}::{prefix}_*", member)
                for dec in _registering_decorators(node):
                    add(f"decorator @{dec}", member)
    return {k: v for k, v in groups.items() if len(v) >= min_group}


def _parents(feat: str) -> list[str]:
    """More general features that imply *feat* is missing too."""
    kind, _, rest = feat.partition(":")
    head, _, arg = rest.partition("(")
    if kind == "kwarg":
        return [f"call:{head}"]
    if kind == "decorator_kwarg":
        name_only = f"decorator_kwarg:{head}({arg.split('=', 1)[0]}=)"
        return ([name_only] if name_only != feat else []) + [f"decorator:{head}"]
    return []


def _rank(feat: str) -> int:
    """General features first, so they win the de-duplication."""
    if feat.startswith(("kwarg:", "decorator_kwarg:")):
        return 2 if not feat.endswith("=)") else 1
    return 0


def sibling_outliers(
    groups: dict[str, list[_Member]], min_share: float
) -> tuple[list[dict], int]:
    """``(outliers, total before the cap)``: per group, the features at least
    *min_share* of the members have and the members lacking them."""
    outliers: list[dict] = []
    for key, members in sorted(groups.items()):
        size = len(members)
        need = max(3, math.ceil(min_share * size))
        counts: dict[str, int] = {}
        for m in members:
            for f in m.feats:
                counts[f] = counts.get(f, 0) + 1
        found: list[dict] = []
        seen: set[tuple] = set()
        ranked = sorted(counts.items(), key=lambda kv: (-kv[1], _rank(kv[0]), kv[0]))
        for feat, count in ranked:
            if count < need or count == size:
                continue
            missing = [m for m in members if feat not in m.feats]
            names = tuple(m.name for m in missing)
            seen.add((names, feat))
            # `call:x` and `kwarg:x(timeout=)` missing from the same siblings
            # are one observation: keep the more general feature.
            if any((names, parent) in seen for parent in _parents(feat)):
                continue
            found.append(
                {
                    "group": key,
                    "group_size": size,
                    "feature": feat,
                    "present_in": count,
                    "share": round(count / size, 3),
                    "missing_from": [
                        {"function": m.name, "file": m.file, "line": m.line}
                        for m in missing
                    ],
                }
            )
        outliers.extend(found[:_MAX_FEATURES_PER_GROUP])
    # Strongest first: the most shared feature, then the largest group.
    outliers.sort(key=lambda o: (-o["share"], -o["group_size"], o["group"]))
    return outliers[:_MAX_OUTLIERS], len(outliers)


def _self_calls(func: ast.AST) -> set[str]:
    out = set()
    for node in _own_nodes(func):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "self"
        ):
            out.add(node.func.attr)
    return out


def _self_attrs(func: ast.AST, methods: set[str]) -> set[str]:
    out = set()
    for node in _own_nodes(func):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
            and node.attr not in methods
        ):
            out.add(node.attr)
    return out


# Methods that set up or tear down a resource touch it directly by design.
_LIFECYCLE = ("open", "close", "connect", "disconnect", "start", "stop", "shutdown",
              "setup", "teardown", "reset", "init", "dispose", "cleanup", "aclose")  # fmt: skip


def helper_bypasses(
    parsed: list[tuple[str, ast.Module]], min_callers: int
) -> list[dict]:
    """Per class: helpers called by at least *min_callers* methods, what they
    wrap -- an attribute (`self._db`) or a method (`self._connect_db()`)
    the helper touches and its callers rarely do -- and the other methods that
    touch it directly. Lifecycle methods (`open`, `close`, `reset`, ...) and
    the helper's own sub-helpers are not bypasses."""
    out: list[dict] = []
    for rel, tree in parsed:
        for cls in (n for n in tree.body if isinstance(n, ast.ClassDef)):
            methods = {c.name: c for c in cls.body if isinstance(c, _FUNC_TYPES)}
            names = set(methods)
            calls = {n: _self_calls(f) for n, f in methods.items()}
            attrs = {n: _self_attrs(f, names) for n, f in methods.items()}
            for helper, hnode in methods.items():
                callers = sorted(
                    n for n, c in calls.items() if helper in c and n != helper
                )
                if len(callers) < min_callers:
                    continue
                touches = {n: attrs[n] | {f"{c}()" for c in calls[n]} for n in methods}
                for attr in sorted(touches[helper]):
                    if attr.removesuffix("()") == helper:
                        continue
                    direct = [c for c in callers if attr in touches[c]]
                    if len(direct) * 3 > len(callers):
                        continue  # callers use it too: not wrapped
                    bypass = [
                        n
                        for n in methods
                        if n != helper
                        and n not in callers
                        and n not in calls[helper]  # a sub-helper of the helper
                        and not (n.startswith("__") and n.endswith("__"))
                        and not n.lstrip("_").startswith(_LIFECYCLE)
                        and attr in touches[n]
                        # another helper at the same level wraps it too
                        and sum(1 for c in calls.values() if n in c) < min_callers
                    ]
                    # The helper must be THE route: bypasses are the exception.
                    if bypass and len(bypass) * 2 < len(callers):
                        out.append(
                            {
                                "file": rel,
                                "class": cls.name,
                                "helper": helper,
                                "helper_line": hnode.lineno,
                                "callers": len(callers),
                                "wrapped_attribute": attr,
                                "bypassing": [
                                    {"method": n, "line": methods[n].lineno}
                                    for n in bypass
                                ],
                            }
                        )
    out.sort(key=lambda b: (-b["callers"], b["file"], b["class"], b["helper"]))
    return out


def _option(argv: list[str], flag: str, default: float) -> float:
    for i, arg in enumerate(argv):
        value = None
        if arg == flag and i + 1 < len(argv):
            value = argv[i + 1]
        elif arg.startswith(flag + "="):
            value = arg.split("=", 1)[1]
        if value is not None:
            try:
                return float(value)
            except ValueError:
                emit({"error": f"{flag} requires a number, got '{value}'"})
                sys.exit(2)
    return default


def analyze(
    target: str,
    *,
    max_files: int = 0,
    include_tests: bool = False,
    min_group: int = 4,
    min_share: float = 0.75,
) -> dict:
    """Diff sibling functions under *target* (a directory or a file)."""
    _resolved, project_root, scan_root = resolve_target(target)
    files, files_total = collect_python_files(scan_root, max_files)
    parsed: list[tuple[str, ast.Module]] = []
    unparseable: list[str] = []
    skipped_tests = 0
    for path in files:
        rel = relative_to_root(path, project_root)
        if not include_tests and is_test_file(rel):
            skipped_tests += 1
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError, ValueError, RecursionError):
            unparseable.append(rel)
            continue
        parsed.append((rel, tree))
    groups = collect_groups(parsed, min_group)
    outliers, total = sibling_outliers(groups, min_share)
    bypasses = helper_bypasses(parsed, max(2, min_group - 1))
    result = build_envelope(project_root, scan_root, files_total, len(files))
    result["parameters"] = {"min_group": min_group, "min_share": min_share}
    result["summary"] = {
        "files_scanned": len(parsed),
        "groups_examined": len(groups),
        "groups_by_kind": {
            kind: sum(1 for k in groups if k.startswith(kind + " "))
            for kind in ("class", "prefix", "decorator")
        },
        "sibling_outliers": total,
        "sibling_outliers_listed": len(outliers),
        "helper_bypasses": len(bypasses),
    }
    result["sibling_outliers"] = outliers
    result["helper_bypasses"] = bypasses
    result["skipped"] = {
        "test_files": skipped_tests,
        "include_tests": include_tests,
        "unparseable": unparseable,
    }
    return result


def main() -> None:
    argv = sys.argv[1:]
    own = ("--min-group", "--min-share")
    rest = [
        a
        for i, a in enumerate(argv)
        if a not in own
        and a != "--include-tests"
        and not a.startswith(tuple(f"{f}=" for f in own))
        and not (i > 0 and argv[i - 1] in own)
    ]
    target, max_files = parse_common_args(rest, __doc__)
    emit(
        analyze(
            target,
            max_files=max_files,
            include_tests="--include-tests" in argv,
            min_group=int(_option(argv, "--min-group", 4)),
            min_share=_option(argv, "--min-share", 0.75),
        )
    )


if __name__ == "__main__":
    main()
