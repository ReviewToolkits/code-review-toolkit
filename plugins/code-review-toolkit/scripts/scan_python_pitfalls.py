#!/usr/bin/env python3
"""Detect the correctness pitfalls catalogued in data/python_bug_shapes.json.

Each check corresponds 1:1 to a shape `id` in that catalog, so a finding can be
traced straight back to its shape (with its guarded twin, sibling-hunt directive,
and differential). The differentials are applied here as confidence downgrades
rather than hard filters: the script's job is high recall with an honest
confidence signal, and the agent's job is the final call.

Confidence levels:
    high    -- the differential does not apply; this is very likely a real defect
    medium  -- matches the shape, but a legitimate reading exists; agent must check
    low     -- weak signal, reported only so a sibling hunt can start from it

Usage:
    python scan_python_pitfalls.py [path] [--max-files N] [--check ID[,ID...]] [--exclude PAT[,PAT...]]
"""

from __future__ import annotations

import ast
import builtins
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scan_common import (  # noqa: E402
    build_envelope,
    collect_python_files,
    emit,
    parse_common_args,
    parse_source,
    relative_to_root,
)

# --------------------------------------------------------------------------
# Reference tables
# --------------------------------------------------------------------------

# Names that make a default value mutable. `datetime.now()`-style calls are
# evaluated once at def time, which is the same defect wearing different clothes.
_MUTABLE_CALLS = frozenset(
    {
        "list",
        "dict",
        "set",
        "bytearray",
        "collections",
        "Counter",
        "defaultdict",
        "OrderedDict",
        "deque",
    }
)
_EVALUATED_ONCE_CALLS = frozenset(
    {"now", "today", "utcnow", "time", "uuid4", "monotonic"}
)

# Calls that block the thread -- fatal inside `async def`.
_BLOCKING_CALLS = frozenset(
    {
        "time.sleep",
        "os.system",
        "subprocess.run",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
        "urllib.request.urlopen",
        "requests.get",
        "requests.post",
        "requests.put",
        "requests.delete",
        "requests.patch",
        "requests.head",
        "requests.request",
        "socket.create_connection",
    }
)

# Methods that mutate the receiver in place.
_MUTATING_METHODS = frozenset(
    {
        "append",
        "extend",
        "insert",
        "remove",
        "pop",
        "clear",
        "update",
        "add",
        "discard",
        "sort",
        "setdefault",
        "popitem",
    }
)

# Calls that produce a safe snapshot of a container.
_SNAPSHOT_CALLS = frozenset(
    {
        "list",
        "tuple",
        "set",
        "dict",
        "sorted",
        "frozenset",
        "copy",
        "deepcopy",
        "reversed",
        "items",
        "keys",
        "values",
    }
)

# Decorators that make a class derive __eq__/__hash__ for you.
_DATACLASS_DECORATORS = frozenset(
    {"dataclass", "attrs", "define", "frozen", "total_ordering"}
)

# struct format codes that decode as SIGNED integers. The uppercase twin of each
# is the unsigned form, which is the fix for a header field that cannot be
# negative. 'n' is ssize_t (native-only) and is signed as well.
_SIGNED_STRUCT_CODES = frozenset("bhilqn")

# Substrings that mark a name as an extent -- something used to size, offset, or
# index into a buffer, where a negative value silently re-anchors a slice instead
# of raising.
_EXTENT_HINTS = (
    "size",
    "len",
    "count",
    "offset",
    "num",
    "width",
    "height",
    "pos",
    "index",
    "idx",
    "total",
)
_EXTENT_NAMES = frozenset({"n", "nb", "cnt"})

# Calls whose arguments are consumed as an extent.
_EXTENT_CALLS = frozenset(
    {"range", "read", "seek", "recv", "unpack_from", "iter_unpack", "truncate"}
)

# Functions that open a stream. `Path.open` is handled separately, since its
# path is the receiver rather than the first argument.
_OPEN_MODULES = frozenset({"io", "codecs", "gzip", "bz2", "lzma", "tarfile"})

# `errors=` values that silently substitute rather than fail. Pairing one of
# these on the read side with a strict write is what destroys data.
_LENIENT_ERRORS = frozenset(
    {"replace", "ignore", "backslashreplace", "xmlcharrefreplace"}
)

# Hooks that mark the end of an object's lifecycle. The commit subset carries
# "the operation completed" semantics, so calling one on an abort path records
# work that was abandoned; the rest usually mean "release resources", which is
# legitimate on both paths.
_COMMIT_HOOKS = frozenset(
    {"finish", "commit", "save", "accept", "submit", "done", "complete", "finalize"}
)

# APIs with a KNOWN return domain, so a comparison against anything else is a
# dead guard. The bool records whether the domain is closed (Unicode general
# categories are fixed by the standard) or merely well-known (new platforms
# appear), which is the difference between high and medium confidence.
_UNICODE_CATEGORIES = frozenset(
    {
        "Cc",
        "Cf",
        "Cn",
        "Co",
        "Cs",
        "Ll",
        "Lm",
        "Lo",
        "Lt",
        "Lu",
        "Mc",
        "Me",
        "Mn",
        "Nd",
        "Nl",
        "No",
        "Pc",
        "Pd",
        "Pe",
        "Pf",
        "Pi",
        "Po",
        "Ps",
        "Sc",
        "Sk",
        "Sm",
        "So",
        "Zl",
        "Zp",
        "Zs",
    }
)
_API_VALUE_DOMAINS: dict[str, tuple[frozenset[str], bool]] = {
    "unicodedata.category": (_UNICODE_CATEGORIES, True),
    "category": (_UNICODE_CATEGORIES, True),
    "unicodedata.east_asian_width": (frozenset({"F", "H", "W", "Na", "A", "N"}), True),
    "unicodedata.bidirectional": (
        frozenset(
            {
                "L",
                "R",
                "AL",
                "EN",
                "ES",
                "ET",
                "AN",
                "CS",
                "NSM",
                "BN",
                "B",
                "S",
                "WS",
                "ON",
                "LRE",
                "LRO",
                "RLE",
                "RLO",
                "PDF",
                "LRI",
                "RLI",
                "FSI",
                "PDI",
                "",
            }
        ),
        True,
    ),
    "os.name": (frozenset({"posix", "nt", "java"}), True),
    "sys.platform": (
        frozenset(
            {
                "linux",
                "darwin",
                "win32",
                "cygwin",
                "aix",
                "sunos5",
                "freebsd",
                "openbsd",
                "netbsd",
                "emscripten",
                "wasi",
                "android",
                "ios",
                "vxworks",
            }
        ),
        False,
    ),
}

# Names that denote a type without being capitalized.
_BUILTIN_TYPES = frozenset(
    {
        "int",
        "str",
        "bytes",
        "bytearray",
        "float",
        "complex",
        "bool",
        "list",
        "dict",
        "set",
        "frozenset",
        "tuple",
        "type",
        "object",
        "slice",
        "range",
        "memoryview",
        "property",
        "staticmethod",
        "classmethod",
    }
)

# Filename prefixes that mark parallel per-platform implementations of one
# interface. A sentinel that differs across such a pair is the shape.
_PARALLEL_PREFIXES = (
    "unix_",
    "windows_",
    "win32_",
    "win_",
    "posix_",
    "nt_",
    "linux_",
    "darwin_",
    "mac_",
    "macos_",
    "bsd_",
)

# Receivers whose lifecycle hook usually means "release this resource" rather
# than "the operation completed".
_RESOURCE_RECEIVERS = frozenset(
    {
        "console",
        "screen",
        "display",
        "term",
        "terminal",
        "window",
        "socket",
        "sock",
        "conn",
        "connection",
        "stream",
        "file",
        "fd",
        "proc",
        "process",
        "pool",
        "thread",
    }
)

# Scope names that mark an abort path -- the operation is being abandoned, not
# completed.
_ABORT_HINTS = (
    "cancel",
    "abort",
    "interrupt",
    "ctrl_c",
    "sigint",
    "discard",
    "rollback",
    "reject",
    "revert",
    "escape",
    "undo",
    "kill",
    "terminate",
)


def _exception_ancestors() -> dict[str, set[str]]:
    """Map each builtin exception name to the set of its ancestor names.

    Derived from the running interpreter, so it is always correct for the Python
    in use rather than a hand-maintained table that drifts.
    """
    table: dict[str, set[str]] = {}
    for name in dir(builtins):
        obj = getattr(builtins, name)
        if isinstance(obj, type) and issubclass(obj, BaseException):
            table[name] = {
                base.__name__
                for base in obj.__mro__[1:]
                if isinstance(base, type) and issubclass(base, BaseException)
            }
    return table


_EXC_ANCESTORS = _exception_ancestors()


# --------------------------------------------------------------------------
# AST helpers
# --------------------------------------------------------------------------


def _dotted_name(node: ast.AST) -> str:
    """Render an attribute/name chain as a dotted string ('a.b.c'), else ''."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def _call_name(node: ast.Call) -> str:
    """Best-effort dotted name of what a Call invokes."""
    return _dotted_name(node.func)


def _names_used(node: ast.AST) -> set[str]:
    """All bare Name identifiers loaded anywhere under *node*."""
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _name_list(names: list[str], limit: int = 6) -> str:
    """Render identifiers for a finding message, capped so it stays readable."""
    if not names:
        return "a value"
    if len(names) <= limit:
        return ", ".join(names)
    return ", ".join(names[:limit]) + f" and {len(names) - limit} more"


def _is_snapshot(node: ast.AST) -> bool:
    """True if *node* evaluates to a copy rather than the live container."""
    if isinstance(node, ast.Call):
        name = _call_name(node)
        return bool(name) and name.split(".")[-1] in _SNAPSHOT_CALLS
    # d[:] / lst[:] produce a copy.
    return isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice)


def _decorator_names(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
) -> set[str]:
    """Flattened decorator names, including the attribute tail."""
    found: set[str] = set()
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        name = _dotted_name(target)
        if name:
            found.add(name)
            found.add(name.split(".")[-1])
    return found


# Methods that change a container's SIZE. Only these are unsafe during
# iteration -- reassigning an existing key/index leaves the size untouched and
# is explicitly safe (CPython raises only on a size change).
_RESIZING_METHODS = frozenset(
    {
        "append",
        "extend",
        "insert",
        "remove",
        "pop",
        "clear",
        "update",
        "add",
        "discard",
        "setdefault",
        "popitem",
    }
)


def _resizes_during_iteration(
    name: str, loop_vars: set[str], body: list[ast.stmt]
) -> bool:
    """True if *name* may change SIZE inside a loop iterating over it.

    The discriminator that matters: ``d[k] = v`` where ``k`` is the loop
    variable rewrites an existing entry and is safe; ``del d[k]``, a resizing
    method call, or ``d[something_else] = v`` (which can insert a new key) is
    not. Getting this wrong flags idiomatic in-place value updates -- observed
    on CPython's own idlelib, where all four raw hits were safe rewrites.
    """
    for stmt in body:
        for node in ast.walk(stmt):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in _RESIZING_METHODS
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == name
            ):
                return True
            if isinstance(node, ast.Delete):
                for tgt in node.targets:
                    if (
                        isinstance(tgt, ast.Subscript)
                        and isinstance(tgt.value, ast.Name)
                        and tgt.value.id == name
                    ):
                        return True
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                for tgt in targets:
                    if not (
                        isinstance(tgt, ast.Subscript)
                        and isinstance(tgt.value, ast.Name)
                        and tgt.value.id == name
                    ):
                        continue
                    # Subscript by the loop variable -> existing entry -> safe.
                    key = tgt.slice
                    if isinstance(key, ast.Name) and key.id in loop_vars:
                        continue
                    return True
    return False


def _mutates(name: str, body: list[ast.stmt]) -> bool:
    """True if *name* is mutated (method call, item/attr assignment, del) in *body*."""
    for stmt in body:
        for node in ast.walk(stmt):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in _MUTATING_METHODS
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == name
            ):
                return True
            if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                for tgt in targets:
                    if isinstance(tgt, ast.Subscript) and isinstance(
                        tgt.value, ast.Name
                    ):
                        if tgt.value.id == name:
                            return True
            if isinstance(node, ast.Delete):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Subscript) and isinstance(
                        tgt.value, ast.Name
                    ):
                        if tgt.value.id == name:
                            return True
    return False


def _walk_same_scope(node: ast.AST):
    """Yield descendants of *node* without entering a nested function scope.

    ``ast.walk`` descends unconditionally, so a `return` inside a nested `def`
    would otherwise be attributed to the enclosing construct.
    """
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        yield child
        yield from _walk_same_scope(child)


def _returns(name: str, body: list[ast.stmt]) -> bool:
    """True if the OBJECT bound to *name* escapes via a return or yield.

    Deliberately narrow. Matching any occurrence of the name inside the returned
    expression treats `return self._configure(index, cnf, kw)` as returning
    `cnf`, when it passes it to a call and returns that call's result. tkinter
    has seven of exactly that shape, and every one was reported at HIGH
    confidence with "the shared object is returned to callers" -- a claim the
    code does not support.

    A call may of course return its argument, but that is a question about the
    callee, not something this scope can answer; guessing yes here is what
    produced the false positives.
    """

    def escapes(value: ast.expr) -> bool:
        if isinstance(value, ast.Name):
            return value.id == name
        # A container literal built around the name still lets it escape:
        # `return [cnf]`, `return (cnf, kw)`, `return {"c": cnf}`.
        if isinstance(value, (ast.Tuple, ast.List, ast.Set)):
            return any(escapes(e) for e in value.elts)
        if isinstance(value, ast.Dict):
            return any(v is not None and escapes(v) for v in value.values)
        # `return cnf or {}` / `return cnf if x else y`
        if isinstance(value, ast.BoolOp):
            return any(escapes(v) for v in value.values)
        if isinstance(value, ast.IfExp):
            return escapes(value.body) or escapes(value.orelse)
        return False

    for stmt in body:
        for node in ast.walk(stmt):
            if isinstance(node, (ast.Return, ast.Yield)) and node.value is not None:
                if escapes(node.value):
                    return True
    return False


def _finding(
    shape: str,
    severity: str,
    confidence: str,
    node: ast.AST,
    message: str,
    detail: str = "",
) -> dict:
    """Build one finding keyed to a bug-shape id."""
    return {
        "shape": shape,
        "type": shape,
        "severity": severity,
        "confidence": confidence,
        "line": getattr(node, "lineno", 0),
        "column": getattr(node, "col_offset", 0),
        "message": message,
        "detail": detail,
    }


# --------------------------------------------------------------------------
# Checks -- one per bug shape
# --------------------------------------------------------------------------


def _check_mutable_default(tree: ast.AST) -> list[dict]:
    out: list[dict] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        args = fn.args
        pairs = list(
            zip(
                args.args[-len(args.defaults) :] if args.defaults else [], args.defaults
            )
        )
        pairs += [
            (a, d) for a, d in zip(args.kwonlyargs, args.kw_defaults) if d is not None
        ]
        for arg, default in pairs:
            kind = ""
            if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                kind = type(default).__name__.lower()
            elif isinstance(default, ast.Call):
                name = _call_name(default)
                tail = name.split(".")[-1] if name else ""
                if tail in _MUTABLE_CALLS:
                    kind = f"{tail}()"
                elif tail in _EVALUATED_ONCE_CALLS:
                    kind = f"{name}()"
            if not kind:
                continue
            mutated = _mutates(arg.arg, fn.body)
            returned = _returns(arg.arg, fn.body)
            if mutated:
                conf, why = "high", "the parameter is mutated in the body"
            elif returned:
                conf, why = "high", "the shared object is returned to callers"
            else:
                conf, why = (
                    "medium",
                    "no mutation seen; may be read-only (see differential)",
                )
            out.append(
                _finding(
                    "mutable-default-argument",
                    "FIX",
                    conf,
                    default,
                    f"{fn.name}(): default for '{arg.arg}' is {kind}, evaluated once at "
                    f"def time and shared by every call",
                    why,
                )
            )
    return out


def _check_late_binding_closure(tree: ast.AST) -> list[dict]:
    out: list[dict] = []
    for loop in ast.walk(tree):
        if not isinstance(loop, (ast.For, ast.AsyncFor, ast.While)):
            continue
        if isinstance(loop, ast.While):
            loop_vars: set[str] = set()
        else:
            loop_vars = {n.id for n in ast.walk(loop.target) if isinstance(n, ast.Name)}
        if not loop_vars:
            continue
        for node in ast.walk(loop):
            if not isinstance(
                node, (ast.Lambda, ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                continue
            body = node.body if isinstance(node, ast.Lambda) else node
            captured = (
                _names_used(body if isinstance(body, ast.AST) else node) & loop_vars
            )
            if not captured:
                continue
            # Default-argument binding (lambda i=i: ...) is the documented fix.
            bound = (
                {a.arg for a in (node.args.args if hasattr(node, "args") else [])}
                if isinstance(node, (ast.Lambda, ast.FunctionDef, ast.AsyncFunctionDef))
                else set()
            )
            if captured <= bound:
                continue
            out.append(
                _finding(
                    "late-binding-closure-in-loop",
                    "FIX",
                    "medium",
                    node,
                    f"closure created in a loop captures {sorted(captured)} by reference; "
                    f"every instance will see the final value",
                    "safe if the callable is consumed within the same iteration -- "
                    "check whether it escapes the loop",
                )
            )
    return out


def _check_except_ordering(tree: ast.AST) -> list[dict]:
    out: list[dict] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        seen: list[tuple[str, ast.ExceptHandler]] = []
        for handler in node.handlers:
            names: list[str] = []
            if isinstance(handler.type, ast.Name):
                names = [handler.type.id]
            elif isinstance(handler.type, ast.Tuple):
                names = [e.id for e in handler.type.elts if isinstance(e, ast.Name)]
            for name in names:
                ancestors = _EXC_ANCESTORS.get(name)
                if ancestors is None:
                    continue  # user-defined; hierarchy unknown statically
                for earlier, _ in seen:
                    if earlier in ancestors:
                        out.append(
                            _finding(
                                "except-clause-ordering-unreachable",
                                "FIX",
                                "high",
                                handler,
                                f"`except {name}` is unreachable: `except {earlier}` above "
                                f"already catches it ({name} subclasses {earlier})",
                                "clauses are tested top-to-bottom; order most-specific first",
                            )
                        )
                        break
            for name in names:
                seen.append((name, handler))
    return out


def _check_return_in_finally(tree: ast.AST) -> list[dict]:
    out: list[dict] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try) or not node.finalbody:
            continue
        for stmt in node.finalbody:
            # Control flow belonging to THIS finally only -- a return inside a
            # nested def belongs to that function, not to the finally.
            candidates = [stmt, *_walk_same_scope(stmt)]
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                candidates = []
            for inner in candidates:
                kw = None
                if isinstance(inner, ast.Return):
                    kw = "return"
                elif isinstance(inner, ast.Break):
                    kw = "break"
                elif isinstance(inner, ast.Continue):
                    kw = "continue"
                if kw:
                    out.append(
                        _finding(
                            "return-or-break-in-finally",
                            "FIX",
                            "high",
                            inner,
                            f"`{kw}` inside `finally` discards any exception propagating "
                            f"through the try block",
                            "the caller sees a normal return instead of the error",
                        )
                    )
    return out


def _check_eq_without_hash(tree: ast.AST) -> list[dict]:
    out: list[dict] = []
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef):
            continue
        decorators = _decorator_names(cls)
        if decorators & _DATACLASS_DECORATORS:
            continue  # eq/hash are derived
        methods = {
            m.name
            for m in cls.body
            if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        # An explicit `__hash__ = None` is a deliberate opt-out.
        explicit = {
            t.id
            for stmt in cls.body
            if isinstance(stmt, ast.Assign)
            for t in stmt.targets
            if isinstance(t, ast.Name)
        }
        if (
            "__eq__" in methods
            and "__hash__" not in methods
            and "__hash__" not in explicit
        ):
            out.append(
                _finding(
                    "eq-without-hash",
                    "CONSIDER",
                    "medium",
                    cls,
                    f"class {cls.name} defines __eq__ but not __hash__; instances become "
                    f"unhashable (Python sets __hash__ = None)",
                    "only a problem if instances are used in a set or as dict keys -- check",
                )
            )
    return out


def _check_mutation_during_iteration(tree: ast.AST) -> list[dict]:
    out: list[dict] = []
    for loop in ast.walk(tree):
        if not isinstance(loop, (ast.For, ast.AsyncFor)):
            continue
        iterated = loop.iter
        # `for k in d.items()` etc. still iterates d -- unwrap one attribute call.
        if isinstance(iterated, ast.Call) and isinstance(iterated.func, ast.Attribute):
            if iterated.func.attr in {"items", "keys", "values"}:
                iterated = iterated.func.value
            elif _is_snapshot(iterated):
                continue
        elif _is_snapshot(iterated):
            continue
        if not isinstance(iterated, ast.Name):
            continue
        name = iterated.id
        loop_vars = {n.id for n in ast.walk(loop.target) if isinstance(n, ast.Name)}
        if _resizes_during_iteration(name, loop_vars, loop.body):
            out.append(
                _finding(
                    "mutation-during-iteration",
                    "FIX",
                    "high",
                    loop,
                    f"'{name}' is mutated while being iterated",
                    "dict/set raise RuntimeError; LISTS SILENTLY SKIP elements as indices "
                    "shift -- iterate over a snapshot such as list(...)",
                )
            )
    return out


def _check_fire_and_forget_task(tree: ast.AST) -> list[dict]:
    out: list[dict] = []
    for node in ast.walk(tree):
        # A bare expression statement -- the return value goes nowhere.
        if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
            continue
        name = _call_name(node.value)
        tail = name.split(".")[-1] if name else ""
        if tail in {"create_task", "ensure_future"} and (
            "asyncio" in name or "loop" in name or tail == "create_task"
        ):
            out.append(
                _finding(
                    "asyncio-fire-and-forget-task",
                    "FIX",
                    "high",
                    node,
                    f"{name}(...) result is discarded; the event loop keeps only a weak "
                    f"reference, so the task may be garbage-collected before it finishes",
                    "retain it in a set with add_done_callback(discard), or await it",
                )
            )
    return out


def _check_blocking_in_async(tree: ast.AST) -> list[dict]:
    out: list[dict] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        # A nested def or lambda is its own scope: a blocking call there runs wherever
        # that callable is invoked (often an executor), not on this coroutine. A nested
        # async def is scanned in its own right by the outer walk.
        for node in _walk_same_scope(fn):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            if name in _BLOCKING_CALLS:
                out.append(
                    _finding(
                        "blocking-call-in-async-function",
                        "FIX",
                        "high",
                        node,
                        f"blocking call {name}(...) inside async def {fn.name}() stalls the "
                        f"entire event loop",
                        "use the async equivalent, or await asyncio.to_thread(...)",
                    )
                )
    return out


def _check_unawaited_coroutine(tree: ast.AST) -> list[dict]:
    """Intra-file only: a coroutine defined here, called here, never awaited."""
    out: list[dict] = []
    async_names = {
        n.name for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)
    }
    if not async_names:
        return out
    awaited: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
            awaited.add(id(node.value))
        # gather/wait/create_task consume the coroutine object legitimately.
        if isinstance(node, ast.Call):
            name = _call_name(node)
            tail = name.split(".")[-1] if name else ""
            if tail in {
                "gather",
                "wait",
                "create_task",
                "ensure_future",
                "run",
                "wait_for",
            }:
                for arg in node.args:
                    if isinstance(arg, ast.Call):
                        awaited.add(id(arg))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
            continue
        call = node.value
        if id(call) in awaited:
            continue
        name = _call_name(call)
        tail = name.split(".")[-1] if name else ""
        if tail in async_names:
            out.append(
                _finding(
                    "unawaited-coroutine",
                    "FIX",
                    "high",
                    call,
                    f"coroutine {tail}() is called without await; the body never runs",
                    "an un-awaited coroutine object is truthy, so guards silently take the "
                    "wrong branch",
                )
            )
    return out


def _check_lru_cache_on_method(tree: ast.AST) -> list[dict]:
    out: list[dict] = []
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef):
            continue
        for fn in cls.body:
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            decorators = _decorator_names(fn)
            if not (decorators & {"lru_cache", "cache"}):
                continue
            first = fn.args.args[0].arg if fn.args.args else ""
            if first not in {"self", "cls"}:
                continue
            out.append(
                _finding(
                    "lru-cache-on-method",
                    "CONSIDER",
                    "high",
                    fn,
                    f"{cls.name}.{fn.name}() is cached on the CLASS with '{first}' in the key, "
                    f"so every instance ever passed is retained for the process lifetime",
                    "use cached_property or a per-instance cache; harmless for singletons",
                )
            )
    return out


def _attribute_mutated_anywhere(tree: ast.AST, attr: str) -> bool:
    """True if ``<anything>.attr`` is mutated anywhere in this module.

    A class-level mutable that is never mutated is declarative configuration
    (the `menu_specs`/`Meta`-style pattern), not shared-state corruption. This
    is a whole-module check because the mutation, if any, lives in a method far
    from the class-body assignment.
    """
    for node in ast.walk(tree):
        # obj.attr.append(...) / obj.attr.update(...)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _MUTATING_METHODS
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == attr
        ):
            return True
        # obj.attr[k] = v  /  del obj.attr[k]
        targets: list[ast.expr] = []
        if isinstance(node, (ast.Assign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        elif isinstance(node, ast.Delete):
            targets = node.targets
        for tgt in targets:
            if (
                isinstance(tgt, ast.Subscript)
                and isinstance(tgt.value, ast.Attribute)
                and tgt.value.attr == attr
            ):
                return True
    return False


def _check_class_level_mutable(tree: ast.AST) -> list[dict]:
    out: list[dict] = []
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef):
            continue
        if _decorator_names(cls) & _DATACLASS_DECORATORS:
            continue
        for stmt in cls.body:
            targets: list[ast.expr] = []
            value: ast.expr | None = None
            if isinstance(stmt, ast.Assign):
                targets, value = stmt.targets, stmt.value
            elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
                targets, value = [stmt.target], stmt.value
            if value is None or not isinstance(value, (ast.List, ast.Dict, ast.Set)):
                continue
            for tgt in targets:
                if not isinstance(tgt, ast.Name):
                    continue
                # The defect requires an actual mutation. ALL_CAPS reads as a
                # constant, and an attribute never mutated in this module is
                # declarative class config (the menu_specs/Meta pattern seen
                # throughout idlelib) -- downgrade rather than drop, since the
                # mutation could live in another module.
                mutated = _attribute_mutated_anywhere(tree, tgt.id)
                if mutated:
                    conf, why = (
                        "high",
                        "the shared object is mutated in this module -- state bleeds "
                        "between instances",
                    )
                elif tgt.id.isupper():
                    conf, why = (
                        "low",
                        "ALL_CAPS and no mutation seen: reads as a constant (prefer a "
                        "tuple/frozenset to make that explicit)",
                    )
                else:
                    conf, why = (
                        "medium",
                        "no mutation seen in this module: likely declarative class "
                        "config, possibly overridden by subclasses -- confirm nothing "
                        "elsewhere mutates it",
                    )
                out.append(
                    _finding(
                        "class-level-mutable-attribute",
                        "FIX",
                        conf,
                        stmt,
                        f"{cls.name}.{tgt.id} is a mutable class attribute shared by every "
                        f"instance",
                        why,
                    )
                )
    return out


_CONTROL_FLOW_EXCEPTIONS = frozenset(
    {"SystemExit", "KeyboardInterrupt", "GeneratorExit"}
)


def _guarded_by_earlier_reraise(
    try_node: ast.Try, handler: ast.ExceptHandler
) -> set[str]:
    """Control-flow exceptions already re-raised by an earlier clause of *try_node*.

    ``except SystemExit: raise`` followed by a bare ``except:`` discharges the
    obligation for SystemExit -- the bare clause can no longer swallow it. The
    idiom appears in CPython's own idlelib (`rpc.py`).
    """
    covered: set[str] = set()
    for clause in try_node.handlers:
        if clause is handler:
            break
        if not any(isinstance(n, ast.Raise) for n in ast.walk(clause)):
            continue
        names: list[str] = []
        if isinstance(clause.type, ast.Name):
            names = [clause.type.id]
        elif isinstance(clause.type, ast.Tuple):
            names = [e.id for e in clause.type.elts if isinstance(e, ast.Name)]
        covered |= {n for n in names if n in _CONTROL_FLOW_EXCEPTIONS}
    return covered


def _check_bare_except(tree: ast.AST) -> list[dict]:
    out: list[dict] = []
    # Map each handler to its owning Try so sibling clauses can be inspected.
    owner: dict[int, ast.Try] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            for clause in node.handlers:
                owner[id(clause)] = node
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        is_bare = node.type is None
        is_base = isinstance(node.type, ast.Name) and node.type.id == "BaseException"
        if not (is_bare or is_base):
            continue
        # A handler that re-raises is a legitimate boundary.
        if any(isinstance(n, ast.Raise) for n in ast.walk(node)):
            continue
        # Capturing the exception for the caller to re-raise (the standard
        # thread/worker-body pattern) preserves control flow -- observed in real
        # code, so downgrade rather than drop.
        captured = bool(node.name) and any(
            isinstance(n, ast.Name) and n.id == node.name
            for stmt in node.body
            for n in ast.walk(stmt)
        )
        label = "except:" if is_bare else "except BaseException:"
        try_node = owner.get(id(node))
        covered = _guarded_by_earlier_reraise(try_node, node) if try_node else set()
        remaining = sorted(_CONTROL_FLOW_EXCEPTIONS - covered)
        if not remaining:
            continue  # every control-flow exception re-raised by an earlier clause
        if covered:
            conf = "medium"
            detail = (
                f"an earlier clause re-raises {sorted(covered)}, but this one still "
                f"swallows {remaining}"
            )
        elif captured:
            conf = "medium"
            detail = (
                f"the exception is captured as '{node.name}' -- legitimate if the caller "
                f"re-raises it (thread/worker boundary); a real bug if it is only logged"
            )
        else:
            conf = "high"
            detail = "use `except Exception:` so Ctrl-C and sys.exit() still work"
        out.append(
            _finding(
                "bare-except-swallows-control-flow",
                "FIX",
                conf,
                node,
                f"`{label}` without re-raise also swallows KeyboardInterrupt and SystemExit",
                detail,
            )
        )
    return out


def _check_exception_in_del(tree: ast.AST) -> list[dict]:
    out: list[dict] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if fn.name != "__del__":
            continue
        guarded = any(isinstance(n, ast.Try) for n in fn.body)
        if guarded:
            continue
        risky = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and _call_name(n)]
        if not risky:
            continue
        out.append(
            _finding(
                "exception-in-del-or-finalizer",
                "CONSIDER",
                "medium",
                fn,
                "__del__ performs calls without a try/except; an exception here is "
                "printed and ignored, silently skipping the rest of the finalizer",
                "prefer weakref.finalize or an explicit close(); module globals may "
                "already be None at interpreter shutdown",
            )
        )
    return out


def _check_is_literal(tree: ast.AST) -> list[dict]:
    out: list[dict] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        for op, comparator in zip(node.ops, node.comparators):
            if not isinstance(op, (ast.Is, ast.IsNot)):
                continue
            for side in (node.left, comparator):
                if not isinstance(side, ast.Constant):
                    continue
                if side.value is None or side.value is True or side.value is False:
                    continue
                if side.value is Ellipsis:
                    continue
                out.append(
                    _finding(
                        "is-comparison-with-literal",
                        "CONSIDER",
                        "high",
                        node,
                        f"`is` compared against the literal {side.value!r}; identity holds "
                        f"only by interning accident",
                        "use == for value comparison; keep `is` for None/True/False/sentinels",
                    )
                )
    return out


# --------------------------------------------------------------------------
# Shapes derived from a 40-bug audit of CPython's pure-Python stdlib.
# Overly-broad `except Exception:` alone accounted for ~50% of those findings.
# --------------------------------------------------------------------------

# Releasing a resource. Not exhaustive by design -- these are the verbs whose
# omission on an error path actually leaks something.
_RELEASE_METHODS = frozenset(
    {"close", "quit", "shutdown", "release", "disconnect", "terminate", "unlink"}
)

# Logging calls too quiet to surface a failure under default configuration.
_QUIET_LOG_METHODS = frozenset({"debug", "info"})
_LOUD_LOG_METHODS = frozenset(
    {"warning", "warn", "error", "exception", "critical", "fatal"}
)


def _handler_swallows(handler: ast.ExceptHandler) -> bool:
    """True if the handler neither re-raises nor propagates a failure signal.

    `pass`, a bare default assignment, or returning a fallback all mean the
    caller cannot tell the operation failed.
    """
    if any(isinstance(n, ast.Raise) for n in ast.walk(handler)):
        return False
    for stmt in handler.body:
        if isinstance(stmt, ast.Pass):
            continue
        if isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            continue
        if isinstance(stmt, ast.Return):
            continue
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            # A logging/reporting call is still swallowing unless it is loud.
            name = _call_name(stmt.value)
            tail = name.split(".")[-1] if name else ""
            if tail in _QUIET_LOG_METHODS or tail in _LOUD_LOG_METHODS:
                continue
            return False
        return False
    return True


def _check_except_exception_too_broad(tree: ast.AST) -> list[dict]:
    out: list[dict] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        # A narrow try body is the signal: the author guarded one operation.
        narrow = len(node.body) <= 2
        for handler in node.handlers:
            if not isinstance(handler.type, ast.Name):
                continue
            if handler.type.id not in {"Exception", "BaseException"}:
                continue
            if not _handler_swallows(handler):
                continue
            # A loud log makes the failure visible -- not silent, so not FIX.
            loud = any(
                isinstance(n, ast.Call)
                and (_call_name(n).split(".")[-1] if _call_name(n) else "")
                in _LOUD_LOG_METHODS
                for n in ast.walk(handler)
            )
            if narrow and not loud:
                conf = "high"
                why = (
                    f"the try body is {len(node.body)} statement(s) -- one narrow "
                    f"operation guarded by a catch-all"
                )
            elif narrow:
                conf, why = (
                    "medium",
                    "narrow try body, but the handler logs loudly -- confirm the "
                    "broad catch is deliberate containment",
                )
            else:
                conf, why = (
                    "low",
                    "large try body: may be a genuine boundary (plugin loader, CLI "
                    "top level, call into user code) -- check for a rationale",
                )
            out.append(
                _finding(
                    "except-exception-too-broad",
                    "FIX",
                    conf,
                    handler,
                    f"`except {handler.type.id}:` swallows every failure of the "
                    f"guarded operation, not just the expected one",
                    why,
                )
            )
    return out


def _check_cleanup_only_on_success(tree: ast.AST) -> list[dict]:
    out: list[dict] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try) or not node.handlers or node.finalbody:
            continue
        if not node.body:
            continue
        last = node.body[-1]
        if not (isinstance(last, ast.Expr) and isinstance(last.value, ast.Call)):
            continue
        call = last.value
        if not isinstance(call.func, ast.Attribute):
            continue
        verb = call.func.attr
        if verb not in _RELEASE_METHODS:
            continue
        target = _dotted_name(call.func.value) or "<resource>"
        # If a handler also releases it, the error path is covered.
        released_in_handler = any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == verb
            for h in node.handlers
            for n in ast.walk(h)
        )
        if released_in_handler:
            continue
        out.append(
            _finding(
                "cleanup-only-on-success-path",
                "FIX",
                "high" if len(node.body) > 1 else "medium",
                last,
                f"`{target}.{verb}()` runs only when the try body succeeds; an "
                f"exception above it leaks the resource",
                "move it to a `finally:` block, or use a `with` statement",
            )
        )
    return out


def _check_error_reported_below_warning(tree: ast.AST) -> list[dict]:
    out: list[dict] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if any(isinstance(n, ast.Raise) for n in ast.walk(node)):
            continue
        quiet: list[str] = []
        loud = False
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            name = _call_name(inner)
            tail = name.split(".")[-1] if name else ""
            if tail in _LOUD_LOG_METHODS:
                loud = True
            elif tail in _QUIET_LOG_METHODS and name:
                quiet.append(name)
        if loud or not quiet:
            continue
        out.append(
            _finding(
                "error-reported-below-warning",
                "CONSIDER",
                "medium",
                node,
                f"the only report of this failure is {quiet[0]}(...), which default "
                f"logging configuration discards",
                "an operator running with defaults sees nothing; use warning/exception "
                "if the condition is actionable",
            )
        )
    return out


# "Nothing to do right now" signals. Catching one of these in a poll loop and
# continuing IS the design -- an event loop that drains a queue with a timeout
# is not a hang. Observed on idlelib's rpc.py/run.py event loops, where every
# raw hit was this pattern; the gist's genuine instance was `except OSError:`
# on a directory scan, where the error means real failure.
_POLL_SIGNAL_EXCEPTIONS = frozenset(
    {
        "Empty",
        "Full",
        "queue.Empty",
        "queue.Full",
        "timeout",
        "socket.timeout",
        "TimeoutError",
        "BlockingIOError",
        "InterruptedError",
        "asyncio.TimeoutError",
        "KeyboardInterrupt",
    }
)


def _is_poll_signal(handler: ast.ExceptHandler) -> bool:
    """True if the handler catches only 'nothing ready yet' conditions."""
    if handler.type is None:
        return False
    parts = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    names = {_dotted_name(p) for p in parts}
    names |= {n.split(".")[-1] for n in names if n}
    caught = {n for n in names if n}
    # An unresolvable name (not a plain Name/Attribute) means we cannot tell.
    if len(caught) < len(parts):
        return False
    return bool(caught) and caught <= _POLL_SIGNAL_EXCEPTIONS


def _check_except_in_loop_without_exit(tree: ast.AST) -> list[dict]:
    out: list[dict] = []
    for loop in ast.walk(tree):
        # Unbounded loop only: `while True:` / `while 1:`.
        if not isinstance(loop, ast.While):
            continue
        test = loop.test
        unbounded = (isinstance(test, ast.Constant) and bool(test.value)) or (
            isinstance(test, ast.Name) and test.id == "True"
        )
        if not unbounded:
            continue
        for node in ast.walk(loop):
            if not isinstance(node, ast.Try):
                continue
            for handler in node.handlers:
                # Any exit from the loop discharges the obligation.
                exits = any(
                    isinstance(n, (ast.Break, ast.Return, ast.Raise))
                    for n in ast.walk(handler)
                )
                if exits:
                    continue
                if _is_poll_signal(handler):
                    continue  # poll loop draining a queue/socket, not a hang
                # A handler that reports loudly is diagnosable, not a silent
                # hang -- this shape's whole complaint is "no diagnostic".
                if any(
                    isinstance(n, ast.Call)
                    and (_call_name(n).split(".")[-1] if _call_name(n) else "")
                    in _LOUD_LOG_METHODS
                    for n in ast.walk(handler)
                ):
                    continue
                # A `while True:` whose ENTIRE body is the guarded operation is a
                # spin. A loop that does other work each iteration -- a REPL
                # reading input, a server accepting connections -- makes progress
                # even when this operation keeps failing.
                sole = len(loop.body) == 1 and loop.body[0] is node
                if not _handler_swallows(handler):
                    continue
                label = (
                    ast.unparse(handler.type) if handler.type is not None else "bare"
                )
                out.append(
                    _finding(
                        "except-in-loop-without-exit",
                        "FIX",
                        "high" if sole else "medium",
                        handler,
                        f"`except {label}` inside `while True:` neither breaks, returns "
                        f"nor raises; a persistent failure spins forever",
                        "a transient failure recovers, but a permanent one hangs the "
                        "process with no diagnostic -- bound the retries",
                    )
                )
    return out


def _check_raise_without_from(tree: ast.AST) -> list[dict]:
    out: list[dict] = []
    for candidate in ast.walk(tree):
        if not isinstance(candidate, ast.ExceptHandler):
            continue
        handler = candidate
        for node in ast.walk(handler):
            if not isinstance(node, ast.Raise):
                continue
            if node.exc is None:  # bare `raise` re-raises; correct
                continue
            if node.cause is not None:  # explicit `from err` / `from None`
                continue
            # Re-raising the caught name itself needs no cause.
            if isinstance(node.exc, ast.Name) and node.exc.id == handler.name:
                continue
            out.append(
                _finding(
                    "raise-without-from-in-except",
                    "CONSIDER",
                    "medium",
                    node,
                    "raising a new exception inside `except` without `from` loses the "
                    "explicit cause",
                    "use `from err` to chain or `from None` to suppress deliberately",
                )
            )
    return out


def _flag_assignments(fn: ast.AST) -> dict[str, list[tuple[int, ast.AST]]]:
    """Map `self.attr` / bare-name targets to their constant assignments."""
    found: dict[str, list[tuple[int, ast.AST]]] = {}
    for node in _walk_same_scope(fn):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        if not isinstance(node.value, ast.Constant):
            continue
        target = node.targets[0]
        name = _dotted_name(target)
        # Only state that OUTLIVES the call can wedge anything -- an attribute
        # (`self.busy`) or a qualified global. A bare local dies with the frame,
        # so re-binding it is ordinary computation, not a missed reset.
        if not name or "." not in name:
            continue
        found.setdefault(name, []).append((node.lineno, node))
    return found


def _check_flag_not_reset_on_early_exit(tree: ast.AST) -> list[dict]:
    """A guard flag set at entry but reset only on the success path.

    `self.busy = True` ... early `return` ... `self.busy = False` at the end
    leaves the flag stuck for the object's lifetime, so every later call takes
    the "already busy" branch and silently does nothing. The correct form --
    `try: ... finally: self.busy = False` -- usually exists on a sibling method
    already (idlelib has five, e.g. `Debugger.run`'s `self.interacting`).
    """
    out: list[dict] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # A finally that touches the flag discharges the obligation.
        guarded_in_finally: set[str] = set()
        for node in ast.walk(fn):
            if isinstance(node, ast.Try):
                for stmt in node.finalbody:
                    for inner in ast.walk(stmt):
                        if isinstance(inner, ast.Assign):
                            for tgt in inner.targets:
                                nm = _dotted_name(tgt)
                                if nm:
                                    guarded_in_finally.add(nm)
        for name, assigns in _flag_assignments(fn).items():
            if name in guarded_in_finally or len(assigns) < 2:
                continue
            set_line, set_node = assigns[0]
            reset_line, _reset_node = assigns[-1]
            if reset_line <= set_line:
                continue
            # Different values: a set/reset pair, not two writes of the same value.
            values = {a[1].value.value for a in (assigns[0], assigns[-1])}  # type: ignore[attr-defined]
            if len(values) < 2:
                continue
            # An exit between them skips the reset.
            exits = [
                n
                for n in _walk_same_scope(fn)
                if isinstance(n, (ast.Return, ast.Raise))
                and set_line < n.lineno < reset_line
            ]
            if not exits:
                continue
            out.append(
                _finding(
                    "flag-not-reset-on-early-exit",
                    "FIX",
                    "high",
                    set_node,
                    f"`{name}` is set at line {set_line} but reset only at line "
                    f"{reset_line}; {len(exits)} earlier exit(s) skip the reset",
                    "the flag stays set for the object's lifetime, so every later "
                    "call takes the guarded branch and silently does nothing -- "
                    "use `try: ... finally:` to restore it on every path",
                )
            )
    return out


def _check_guard_rechecks_call_receiver(tree: ast.AST) -> list[dict]:
    """`m = prog.match(...)` followed by `if not prog:` -- the guard tests the
    receiver instead of the result, so the result is never checked.

    The receiver was just used successfully as a call target, so re-testing it
    is dead code; meanwhile the freshly-bound result can be None and flows on.
    """
    out: list[dict] = []
    for parent in ast.walk(tree):
        body = getattr(parent, "body", None)
        if not isinstance(body, list):
            continue
        for assign, following in zip(body, body[1:]):
            if not isinstance(assign, ast.Assign) or len(assign.targets) != 1:
                continue
            target = assign.targets[0]
            if not isinstance(target, ast.Name):
                continue
            if not (
                isinstance(assign.value, ast.Call)
                and isinstance(assign.value.func, ast.Attribute)
                and isinstance(assign.value.func.value, ast.Name)
            ):
                continue
            receiver = assign.value.func.value.id
            if not isinstance(following, ast.If):
                continue
            test = following.test
            checked = None
            if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
                if isinstance(test.operand, ast.Name):
                    checked = test.operand.id
            elif (
                isinstance(test, ast.Compare)
                and isinstance(test.left, ast.Name)
                and len(test.ops) == 1
                and isinstance(test.ops[0], ast.Is)
                and isinstance(test.comparators[0], ast.Constant)
                and test.comparators[0].value is None
            ):
                checked = test.left.id
            if checked is None or checked != receiver or checked == target.id:
                continue
            out.append(
                _finding(
                    "guard-rechecks-call-receiver",
                    "FIX",
                    "high",
                    following,
                    f"the guard tests `{receiver}` -- the call receiver -- instead of "
                    f"`{target.id}`, the result just assigned",
                    f"`{receiver}` was already used successfully as a call target, so "
                    f"this branch is dead; `{target.id}` is never checked and flows on "
                    f"possibly-None",
                )
            )
    return out


def _check_falsy_check_for_none_default(tree: ast.AST) -> list[dict]:
    """`def f(x=None)` with `if not x:` -- conflates None with 0/''/[]/False.

    The author means "argument omitted", but the test also fires for every
    legitimate falsy value the caller may pass.
    """
    out: list[dict] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        args = fn.args
        none_defaults: set[str] = set()
        pairs = list(
            zip(
                args.args[-len(args.defaults) :] if args.defaults else [], args.defaults
            )
        )
        pairs += [
            (a, d) for a, d in zip(args.kwonlyargs, args.kw_defaults) if d is not None
        ]
        for arg, default in pairs:
            if isinstance(default, ast.Constant) and default.value is None:
                none_defaults.add(arg.arg)
        if not none_defaults:
            continue
        # A parameter reassigned from its default is no longer a sentinel test.
        reassigned = {
            t.id
            for n in _walk_same_scope(fn)
            if isinstance(n, ast.Assign)
            for t in n.targets
            if isinstance(t, ast.Name)
        }
        for node in _walk_same_scope(fn):
            if not isinstance(node, ast.UnaryOp) or not isinstance(node.op, ast.Not):
                continue
            if not isinstance(node.operand, ast.Name):
                continue
            name = node.operand.id
            if name not in none_defaults or name in reassigned:
                continue
            out.append(
                _finding(
                    "falsy-check-for-none-default",
                    "CONSIDER",
                    "medium",
                    node,
                    f"`not {name}` tests falsiness, but `{name}` defaults to None -- "
                    f"the check also fires for 0, '', [] and False",
                    f"use `{name} is None` if the intent is 'argument omitted'; this "
                    f"matters whenever a falsy value is legitimate input",
                )
            )
    return out


# unittest assertion methods whose arguments decide whether a test can fail.
_ASSERT_PREFIXES = ("assert", "failUnless", "failIf")
_FIXTURE_NAMES = frozenset(
    {
        "setUp",
        "setUpClass",
        "tearDown",
        "tearDownClass",
        "setUpModule",
        "tearDownModule",
    }
)


def _is_testcase_class(cls: ast.ClassDef) -> bool:
    """True if the class looks like a unittest TestCase."""
    for base in cls.bases:
        name = _dotted_name(base)
        if name and ("TestCase" in name or name.endswith("TestCase")):
            return True
    return False


def _assertion_aliases(fn: ast.AST) -> set[str]:
    """Local names bound to an assertion method.

    `Equal = self.assertEqual` then `Equal(a, b)` is a very common unittest
    idiom (used throughout CPython's own tests). Without this, every test using
    it looks assertion-free.
    """
    aliases: set[str] = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        bound = _dotted_name(node.value)
        if bound and bound.split(".")[-1].startswith(_ASSERT_PREFIXES):
            aliases.add(target.id)
    return aliases


def _has_assertion(fn: ast.AST) -> bool:
    """True if the function body contains any assertion or explicit failure."""
    aliases = _assertion_aliases(fn)
    for node in ast.walk(fn):
        if isinstance(node, ast.Assert):
            return True
        if isinstance(node, ast.Call):
            name = _call_name(node)
            tail = name.split(".")[-1] if name else ""
            if tail.startswith(_ASSERT_PREFIXES) or tail in {"fail", "skipTest"}:
                return True
            # A call to a locally-aliased assertion, or to a project assertion
            # helper (assert_*/check_*), also counts.
            if tail in aliases or tail.startswith(("assert_", "check_", "_assert")):
                return True
        if isinstance(node, ast.withitem):
            name = _dotted_name(getattr(node.context_expr, "func", node.context_expr))
            if name and name.split(".")[-1].startswith(_ASSERT_PREFIXES):
                return True
    return False


def _called_names(tree: ast.AST) -> set[str]:
    """Every simple/attribute call target name used anywhere in the module."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _call_name(node)
            if name:
                names.add(name.split(".")[-1])
    return names


def _is_effectively_empty(body: list[ast.stmt]) -> bool:
    """True if the body is only `pass`, `...`, and/or a docstring."""
    for stmt in body:
        if isinstance(stmt, ast.Pass):
            continue
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            continue  # docstring or a bare `...`
        return False
    return True


def _check_test_cannot_fail(tree: ast.AST) -> list[dict]:
    """Tests that pass regardless of what the code under test does.

    A test that cannot fail is worse than no test: it reports coverage and
    consumes review attention while verifying nothing, and it makes every other
    invariant the suite claims less trustworthy.
    """
    out: list[dict] = []
    called = _called_names(tree)
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef) or not _is_testcase_class(cls):
            continue
        methods = [
            m
            for m in cls.body
            if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        tests = [m for m in methods if m.name.startswith("test")]
        # Methods of this class that assert directly -- a test calling one of
        # these is verified even though it has no assertion of its own.
        asserting_helpers = {
            m.name
            for m in methods
            if not m.name.startswith("test") and _has_assertion(m)
        }
        fixtures = [m for m in methods if m.name in _FIXTURE_NAMES]

        if fixtures and not tests:
            out.append(
                _finding(
                    "test-cannot-fail",
                    "CONSIDER",
                    "medium",
                    cls,
                    f"{cls.name} defines fixtures ({', '.join(m.name for m in fixtures)}) "
                    f"but no test methods -- the setup runs for nothing",
                    "either the tests were removed or their names lost the `test` prefix",
                )
            )

        for method in methods:
            is_test = method.name.startswith("test")
            if is_test and _is_effectively_empty(method.body):
                out.append(
                    _finding(
                        "test-cannot-fail",
                        "FIX",
                        "high",
                        method,
                        f"{cls.name}.{method.name} has an empty body -- it always passes",
                        "an unwritten test still reports as coverage; mark it skipped "
                        "or write it",
                    )
                )
                continue
            # A test delegating to an in-class asserting helper is verified.
            delegates = any(
                isinstance(n, ast.Call)
                and (_call_name(n).split(".")[-1] if _call_name(n) else "")
                in asserting_helpers
                for n in ast.walk(method)
            )
            if is_test and not _has_assertion(method) and not delegates:
                out.append(
                    _finding(
                        "test-cannot-fail",
                        "CONSIDER",
                        "medium",
                        method,
                        f"{cls.name}.{method.name} contains no assertion",
                        "may be a deliberate does-not-raise smoke test; if so say so, "
                        "otherwise it verifies nothing",
                    )
                )
            # An asserting helper that IS called from a test is correct DRY
            # design (idlelib has many: assert_sidebar_lines_synced, check,
            # runcase). Only an orphan -- one nothing calls -- is a lost test.
            if (
                not is_test
                and _has_assertion(method)
                and method.name not in _FIXTURE_NAMES
                and method.name not in called
            ):
                out.append(
                    _finding(
                        "test-cannot-fail",
                        "FIX",
                        "high",
                        method,
                        f"{cls.name}.{method.name} asserts but is not named `test*`, so "
                        f"unittest never runs it",
                        "rename it, or call it explicitly from a real test",
                    )
                )

    # A comprehension/loop over a literal EMPTY container: cases are built and
    # none are run. CPython's test_keymap.py:35 has `for key in []` producing 60
    # cases and running zero -- introduced by a commit titled "Increase test
    # coverage", which replaced three passing assertions with it.
    for node in ast.walk(tree):
        iters: list[ast.expr] = []
        if isinstance(
            node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)
        ):
            iters = [g.iter for g in node.generators]
        elif isinstance(node, ast.For):
            iters = [node.iter]
        for it in iters:
            empty = (
                isinstance(it, (ast.List, ast.Set, ast.Tuple)) and not it.elts
            ) or (isinstance(it, ast.Dict) and not it.keys)
            if not empty:
                continue
            out.append(
                _finding(
                    "test-cannot-fail",
                    "FIX",
                    "high",
                    node,
                    "iterates over a literal empty container -- the body never runs",
                    "any cases built for it are silently discarded; check whether an "
                    "iterable was meant here",
                )
            )

    # Vacuous assertion arguments, anywhere in the module.
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        tail = name.split(".")[-1] if name else ""
        if not tail.startswith(_ASSERT_PREFIXES):
            continue
        for arg in node.args:
            # assertTrue(all(filter(...))) -- filter yields only matching items,
            # so all() is true unless a yielded item is itself falsy.
            if (
                isinstance(arg, ast.Call)
                and _call_name(arg) in {"all", "any"}
                and arg.args
                and isinstance(arg.args[0], ast.Call)
                and _call_name(arg.args[0]) == "filter"
            ):
                out.append(
                    _finding(
                        "test-cannot-fail",
                        "FIX",
                        "high",
                        node,
                        f"{tail}({_call_name(arg)}(filter(...))) -- filter already drops "
                        f"non-matching items, so the predicate is never actually tested",
                        "use a generator expression applying the predicate: "
                        "all(pred(x) for x in xs)",
                    )
                )
        # assertTrue(True) / assertEqual(1, 1) and friends.
        consts = [a for a in node.args if isinstance(a, ast.Constant)]
        if consts and len(consts) == len(node.args) and node.args:
            values = [a.value for a in consts]
            vacuous = (
                (tail in {"assertTrue", "failUnless"} and bool(values[0]))
                or (tail in {"assertFalse", "failIf"} and not values[0])
                or (
                    tail in {"assertEqual", "assertEquals", "failUnlessEqual"}
                    and len(values) >= 2
                    and values[0] == values[1]
                )
            )
            if vacuous:
                out.append(
                    _finding(
                        "test-cannot-fail",
                        "FIX",
                        "high",
                        node,
                        f"{tail}({', '.join(repr(v) for v in values)}) compares only "
                        f"constants -- it can never fail",
                        "assert on a value produced by the code under test",
                    )
                )
    return out


def _check_self_referential_accumulate(tree: ast.AST) -> list[dict]:
    """`e.raw += e.raw` -- accumulating a value into itself.

    Almost always a copy-paste where the SOURCE was not updated: an adjacent
    line accumulates from a different object (`e.data += e2.data`) and this one
    was duplicated without changing it. For an accumulator starting empty the
    statement is a permanent no-op, so the data it should have collected is
    silently discarded.
    """
    out: list[dict] = []
    for parent in ast.walk(tree):
        body = getattr(parent, "body", None)
        if not isinstance(body, list):
            continue
        for idx, stmt in enumerate(body):
            if not isinstance(stmt, ast.AugAssign):
                continue
            target = _dotted_name(stmt.target)
            value = _dotted_name(stmt.value)
            if not target or target != value:
                continue
            # A sibling accumulate into the same object from a DIFFERENT source
            # is the copy-paste twin, and makes this near-certain.
            obj = target.rsplit(".", 1)[0] if "." in target else target
            twin = None
            for other in body:
                if other is stmt or not isinstance(other, ast.AugAssign):
                    continue
                o_target = _dotted_name(other.target)
                o_value = _dotted_name(other.value)
                if not o_target or not o_value or o_target == o_value:
                    continue
                if o_target.rsplit(".", 1)[0] == obj:
                    twin = f"{o_target} += {o_value}"
                    break
            out.append(
                _finding(
                    "self-referential-accumulate",
                    "FIX",
                    "high" if twin else "medium",
                    stmt,
                    f"`{target} += {target}` accumulates a value into itself",
                    (
                        f"the sibling `{twin}` accumulates from a different source -- "
                        f"this line was almost certainly copied and its source not "
                        f"updated"
                        if twin
                        else "for an accumulator starting empty this is a permanent "
                        "no-op; confirm the source is meant to be itself"
                    ),
                )
            )
            _ = idx
    return out


def _check_duplicated_guard(tree: ast.AST) -> list[dict]:
    """Two structurally identical guards in one block, with a value computed
    between them -- the second was copied without updating its operand.

    The failure is quiet: instead of raising, the code proceeds with a value the
    guard was supposed to reject (a short slice, an unclamped index), so the
    error re-emerges downstream as a DIFFERENT exception type that the caller's
    `except` clause was not written for.
    """
    out: list[dict] = []
    for parent in ast.walk(tree):
        body = getattr(parent, "body", None)
        if not isinstance(body, list) or len(body) < 2:
            continue
        seen: dict[str, ast.If] = {}
        for stmt in body:
            if not isinstance(stmt, ast.If):
                continue
            try:
                key = ast.dump(stmt.test)
            except (TypeError, ValueError):
                continue
            # Only guards -- a test whose body just raises or returns.
            if not any(isinstance(n, (ast.Raise, ast.Return)) for n in stmt.body):
                continue
            if key in seen:
                first = seen[key]
                # Something must be computed between them, else it is dead code
                # rather than a mis-copied guard.
                # Any binding in the SPAN between the guards, at any nesting
                # depth -- a reassignment inside an `if` still means the second
                # guard is re-testing a new value.
                between = [
                    n
                    for n in ast.walk(parent)
                    if isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign))
                    and first.lineno < n.lineno < stmt.lineno
                ]
                if not between:
                    continue
                # Every name bound between the guards, INCLUDING tuple targets
                # (`token, value = get_fws(value)`) -- missing those made the
                # discriminator blind to the commonest reassignment idiom.
                # dict.fromkeys dedupes while preserving order: a plain `x = ...`
                # target is yielded BOTH by _dotted_name and by the ast.walk
                # below, which put every simple name in the message twice.
                seen_names: dict[str, None] = {}
                for n in between:
                    targets = n.targets if isinstance(n, ast.Assign) else [n.target]
                    for t in targets:
                        dotted = _dotted_name(t)
                        if dotted:
                            seen_names[dotted] = None
                        for sub in ast.walk(t):
                            if isinstance(sub, ast.Name):
                                seen_names[sub.id] = None
                names: list[str] = list(seen_names)
                # If the guard's OWN operand was reassigned in between, the
                # repeat is a deliberate re-test of a new value -- the
                # `path = ...; if path.is_file()` loop idiom. Only a guard whose
                # operands are all unchanged is a mis-copied one.
                tested = _names_used(stmt.test) | {
                    _dotted_name(n)
                    for n in ast.walk(stmt.test)
                    if isinstance(n, ast.Attribute)
                }
                if any(a in tested or a.split(".")[0] in tested for a in names):
                    continue
                out.append(
                    _finding(
                        "duplicated-guard-wrong-operand",
                        "FIX",
                        "high",
                        stmt,
                        f"this guard repeats the test at line {first.lineno} verbatim, "
                        f"though {_name_list(names)} was computed in between",
                        "a copied guard whose operand was not updated: it re-checks the "
                        "already-validated value and never checks the new one",
                    )
                )
            else:
                seen[key] = stmt
    return out


def _iter_scopes(tree: ast.AST):
    """Yield the module and every function scope inside it."""
    yield tree
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _struct_field_codes(fmt: str) -> list[str]:
    """One type code per value a struct format produces, in order.

    Needed to tell WHICH name in a tuple unpack got the signed field: in
    `'<4sBBHH8xIIHH8shhQQx4s'` only two of fifteen values are signed, and
    flagging the other thirteen by association is pure noise. Repeat counts
    multiply ordinary codes, but `Ns`/`Np` consume N bytes for ONE value and
    `Nx` is padding that produces none.
    """
    codes: list[str] = []
    count = ""
    for char in fmt:
        if char in "@=<>!":
            continue
        if char.isdigit():
            count += char
            continue
        repeat = int(count) if count else 1
        count = ""
        if char in "sp":
            codes.append(char)
        elif char == "x":
            continue
        else:
            codes.extend(char * repeat)
    return codes


def _unpack_field_codes(node: ast.AST) -> list[str] | None:
    """Per-value type codes of a `struct.unpack*` call, else None.

    Handles `struct.unpack(fmt, buf)` and `struct.unpack(fmt, buf)[i]`, and the
    f-string form `f"<{n}h"` that builds a repeat count at runtime (whose length
    is unknowable statically, so alignment is skipped for it).
    """
    index: int | None = None
    if isinstance(node, ast.Subscript):
        if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, int):
            index = node.slice.value
        node = node.value
    if not isinstance(node, ast.Call) or not node.args:
        return None
    tail = (_call_name(node) or "").split(".")[-1]
    if tail not in {"unpack", "unpack_from", "iter_unpack"}:
        return None
    fmt = node.args[0]
    if isinstance(fmt, ast.Constant) and isinstance(fmt.value, str):
        codes = _struct_field_codes(fmt.value)
    elif isinstance(fmt, ast.JoinedStr):
        # f"<{count}h" -- only the literal segments carry type codes, and the
        # runtime repeat count means the produced length is unknown.
        literal = "".join(
            v.value
            for v in fmt.values
            if isinstance(v, ast.Constant) and isinstance(v.value, str)
        )
        return _struct_field_codes(literal) or None
    else:
        return None
    if index is not None:
        return [codes[index]] if -len(codes) <= index < len(codes) else None
    return codes


def _bound_names(targets: list[ast.expr]) -> list[tuple[str, ast.expr]]:
    """Flatten assignment targets to (name, node) pairs, including tuple targets."""
    out: list[tuple[str, ast.expr]] = []
    for target in targets:
        for node in ast.walk(target):
            if isinstance(node, ast.Name):
                out.append((node.id, node))
    return out


def _looks_like_extent(name: str) -> bool:
    lowered = name.lower()
    return lowered in _EXTENT_NAMES or any(h in lowered for h in _EXTENT_HINTS)


def _lower_bound_checked(name: str, nodes: list[ast.AST]) -> bool:
    """True if *name* is anywhere compared against 0/1, or clamped.

    Deliberately broad: the goal is to stay silent whenever the author showed ANY
    awareness that the value could be negative, even via a comparison this check
    does not model precisely.
    """
    for node in nodes:
        if isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            has_name = any(name in _names_used(o) for o in operands)
            has_zero = any(
                isinstance(o, ast.Constant) and o.value in (0, 1) for o in operands
            )
            if has_name and has_zero:
                return True
        elif isinstance(node, ast.Call):
            tail = (_call_name(node) or "").split(".")[-1]
            if tail in {"max", "abs"} and any(
                name in _names_used(a) for a in node.args
            ):
                return True
    return False


def _extent_names(nodes: list[ast.AST]) -> set[str]:
    """Names that reach a slice, an index, or an extent-consuming call.

    Propagates backwards through assignment: once `offset` is known to index a
    buffer, the `name_size` in `offset += name_size` is an extent too.
    """
    seed: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.Subscript):
            seed |= _names_used(node.slice)
        elif isinstance(node, ast.Call):
            # Strip the private-method underscore: `self._recv(size)` consumes an
            # extent exactly as `sock.recv(size)` does.
            tail = (_call_name(node) or "").split(".")[-1].lstrip("_")
            if tail in _EXTENT_CALLS:
                for arg in node.args:
                    seed |= _names_used(arg)
    # Fixpoint over assignments, bounded -- chains this long do not occur.
    for _ in range(5):
        grown = set(seed)
        for node in nodes:
            if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                names = {n for t in targets for n, _ in _bound_names([t])}
                if names & seed and node.value is not None:
                    grown |= _names_used(node.value)
        if grown == seed:
            break
        seed = grown
    return seed


def _check_signed_length_from_header(tree: ast.AST) -> list[dict]:
    """A length/offset unpacked SIGNED from a binary header and never checked
    for negativity.

    In C this is the classic signed-overflow read. In Python it is harder to
    spot, and worse in one respect: a negative bound does not raise. Negative
    slicing re-anchors from the end of the buffer, so a crafted file parses
    cleanly and yields attacker-chosen bytes instead of an error.
    """
    out: list[dict] = []
    for scope in _iter_scopes(tree):
        nodes = list(_walk_same_scope(scope))
        extents = _extent_names(nodes)
        for stmt in nodes:
            if not isinstance(stmt, ast.Assign):
                continue
            codes = _unpack_field_codes(stmt.value)
            if not codes or not any(c in _SIGNED_STRUCT_CODES for c in codes):
                continue
            bound = _bound_names(stmt.targets)
            # Align names with fields so only the names that actually received a
            # signed field are flagged; without this, one signed field in a wide
            # header taints every name in the tuple.
            if len(bound) == len(codes):
                bound = [
                    pair
                    for pair, code in zip(bound, codes)
                    if code in _SIGNED_STRUCT_CODES
                ]
            signed = "".join(c for c in codes if c in _SIGNED_STRUCT_CODES)
            for name, node in bound:
                if not _looks_like_extent(name):
                    continue
                if _lower_bound_checked(name, nodes):
                    continue
                reaches = name in extents
                out.append(
                    _finding(
                        "signed-length-from-untrusted-header",
                        "FIX",
                        "high" if reaches else "medium",
                        node,
                        f"'{name}' is unpacked with a signed struct code "
                        f"('{signed}') and never checked for a negative value",
                        (
                            "it reaches a slice, index, or read length, where a "
                            "negative value re-anchors instead of raising"
                            if reaches
                            else "no extent use found in this scope; the value may be "
                            "validated or consumed elsewhere (see differential)"
                        ),
                    )
                )
    return out


def _open_target(node: ast.Call) -> tuple[str, ast.Call] | None:
    """Normalized identity of the path a stream-opening call operates on."""
    func = node.func
    if isinstance(func, ast.Name) and func.id == "open":
        return (ast.dump(node.args[0]), node) if node.args else None
    if isinstance(func, ast.Attribute) and func.attr == "open":
        receiver = _dotted_name(func.value)
        if receiver and receiver.split(".")[-1] in _OPEN_MODULES:
            return (ast.dump(node.args[0]), node) if node.args else None
        # A path object: `p.open("w", encoding=...)`.
        return (ast.dump(func.value), node)
    return None


def _render_codec(node: ast.AST) -> str:
    """Render a codec argument, collapsing every computed form to one token.

    Two different variable names are not evidence of a codec mismatch -- they may
    hold the same value -- so all non-literals compare equal.
    """
    if isinstance(node, ast.Constant):
        return str(node.value)
    return "<expr>"


def _keyword_value(node: ast.Call, name: str) -> str | None:
    for kw in node.keywords:
        if kw.arg == name:
            return _render_codec(kw.value)
    return None


def _open_mode(node: ast.Call) -> str:
    """The literal mode of an open call, defaulting to 'r'. '' if not literal."""
    positional = node.args[1] if len(node.args) > 1 else None
    if positional is not None:
        if isinstance(positional, ast.Constant) and isinstance(positional.value, str):
            return positional.value
        return ""
    for kw in node.keywords:
        if kw.arg == "mode":
            if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                return kw.value.value
            return ""
    return "r"


def _manual_codec(nodes: list[ast.AST], method: str) -> tuple[str | None, str | None]:
    """Codec of the first `.decode()`/`.encode()` in a scope.

    A binary open carries no codec of its own; the conversion is done by hand,
    and that is where the asymmetry usually hides -- a `errors='replace'` on the
    decode side paired with a default-strict encode on the way back out.
    """
    for node in nodes:
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != method:
            continue
        # `self.encode(text)` is a method of the enclosing class taking DATA, not
        # the str/bytes builtin taking a codec name. Reading its first argument
        # as an encoding is how this check invented a mismatch in idlelib.
        if _dotted_name(func.value) in {"self", "cls"}:
            continue
        positional: list[str | None] = [_render_codec(a) for a in node.args[:2]]
        while len(positional) < 2:
            positional.append(None)
        return (
            _keyword_value(node, "encoding") or positional[0],
            _keyword_value(node, "errors") or positional[1],
        )
    return (None, None)


def _check_asymmetric_encode_decode(tree: ast.AST) -> list[dict]:
    """The same path opened for read and for write with different text codecs.

    A lenient read (`errors='replace'`) paired with a strict write means the
    program's own round-trip destroys data: the substitution characters the read
    produced are what the write persists, so the original bytes are gone after
    one cycle.
    """
    readers: dict[str, list[tuple[ast.Call, str | None, str | None]]] = {}
    writers: dict[str, list[tuple[ast.Call, str | None, str | None]]] = {}
    for scope in _iter_scopes(tree):
        nodes = list(_walk_same_scope(scope))
        decoded: tuple[str | None, str | None] | None = None
        encoded: tuple[str | None, str | None] | None = None
        for node in nodes:
            if not isinstance(node, ast.Call):
                continue
            target = _open_target(node)
            if target is None:
                continue
            key, call = target
            mode = _open_mode(call)
            if not mode:
                continue
            writing = any(c in mode for c in "wax")
            if "b" in mode:
                # Binary: the codec lives in a hand-written encode/decode nearby.
                if writing:
                    if encoded is None:
                        encoded = _manual_codec(nodes, "encode")
                    codec = encoded
                else:
                    if decoded is None:
                        decoded = _manual_codec(nodes, "decode")
                    codec = decoded
                if codec == (None, None):
                    continue
            else:
                codec = (
                    _keyword_value(call, "encoding"),
                    _keyword_value(call, "errors"),
                )
            bucket = writers if writing else readers
            bucket.setdefault(key, []).append((call, *codec))

    out: list[dict] = []
    for key, writes in writers.items():
        reads = readers.get(key)
        if not reads:
            continue
        # One codec per side is what "the two sides disagree" presupposes. A path
        # opened under three different codecs is a module VARYING the codec on
        # purpose -- which is what codec test suites do, and they dominated the
        # raw output by two orders of magnitude.
        r_codecs = {(enc, err) for _, enc, err in reads}
        w_codecs = {(enc, err) for _, enc, err in writes}
        if len(r_codecs) > 1 or len(w_codecs) > 1:
            continue
        (r_enc, r_err), (w_enc, w_err) = r_codecs.pop(), w_codecs.pop()
        if (r_enc, r_err) == (w_enc, w_err):
            continue
        reader, writer = reads[0][0], writes[0][0]
        lenient_read = r_err in _LENIENT_ERRORS and w_err not in _LENIENT_ERRORS
        detail = (
            f"read uses errors={r_err!r}, write uses errors={w_err!r} -- what "
            f"the read substituted is what the write persists"
            if lenient_read
            else f"read uses encoding={r_enc!r}/errors={r_err!r}, write uses "
            f"encoding={w_enc!r}/errors={w_err!r}"
        )
        out.append(
            _finding(
                "asymmetric-encode-decode-pair",
                "FIX",
                "high" if lenient_read else "medium",
                writer,
                f"this write and the read at line {reader.lineno} open the same "
                f"path with different text codecs, so a round-trip is lossy",
                detail,
            )
        )
    return out


def _walk_with_scope(node: ast.AST, names: tuple[str, ...] = ()):
    """Yield (node, enclosing scope names) for every descendant."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield from _walk_with_scope(child, names + (child.name,))
        else:
            yield child, names
            yield from _walk_with_scope(child, names)


def _is_abort_scope(names: tuple[str, ...]) -> bool:
    return any(h in n.lower() for n in names for h in _ABORT_HINTS)


def _is_test_name(name: str) -> bool:
    lowered = name.lower()
    return lowered.startswith("test") or lowered.endswith(("test", "tests", "testcase"))


def _call_args_key(node: ast.Call) -> str:
    """Structural signature of a call's arguments, for comparing two call sites."""
    return ast.dump(
        ast.Call(
            func=ast.Name(id="_", ctx=ast.Load()),
            args=node.args,
            keywords=node.keywords,
        )
    )


def _check_lifecycle_hook_two_meanings(tree: ast.AST) -> list[dict]:
    """A commit-semantic lifecycle hook invoked from an abort path.

    `finish`/`commit`/`save` read as "the operation completed", and the override
    is written to match -- it persists something. Calling one to tear down an
    ABANDONED operation therefore records work the user cancelled. The hook is
    doing double duty, and only one of its two meanings is implemented.

    Release-semantic hooks (`close`, `cleanup`, `flush`) are deliberately NOT
    checked: they mean "let go of the resource", which is correct on both paths,
    and including them buries the real signal.
    """
    abort_sites: list[tuple[ast.Call, tuple[str, ...], str, str]] = []
    normal_hooks: dict[str, tuple[ast.Call, tuple[str, ...]]] = {}
    for stmt, scope in _walk_with_scope(tree):
        # Statement context only. `if self.done():` is a PREDICATE being read,
        # not a hook being invoked -- asyncio's Future.done() made that the
        # single largest false-positive class in the raw pass.
        if not isinstance(stmt, ast.Expr) or not isinstance(stmt.value, ast.Call):
            continue
        node = stmt.value
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in _COMMIT_HOOKS:
            continue
        receiver = _dotted_name(func.value)
        if not receiver:
            continue
        # Test scopes name the path they exercise, so `test_cancel` and
        # `InterruptedSendTimeoutTest` read as abort paths for any hook they call.
        if any(_is_test_name(n) for n in scope):
            continue
        if _is_abort_scope(scope):
            abort_sites.append((node, scope, receiver, func.attr))
        else:
            normal_hooks.setdefault(func.attr, (node, scope))

    out: list[dict] = []
    for node, scope, receiver, hook in abort_sites:
        sibling = normal_hooks.get(hook)
        # The guarded twin: if the normal path passes DIFFERENT arguments, the
        # hook is being told which outcome it is handling and implements both
        # meanings. tkinter's dnd.py is the stdlib's model of this -- `cancel`
        # calls `self.finish(event, 0)` where `on_release` calls
        # `self.finish(event, 1)`. That is the fix, not the bug.
        if sibling is not None and _call_args_key(node) != _call_args_key(sibling[0]):
            continue
        tail = receiver.split(".")[-1].lower()
        # On a resource-like object the hook usually means "tear down", not
        # "commit" -- real ambiguity, so report it lower rather than suppress it.
        release_reading = tail in _RESOURCE_RECEIVERS and hook not in {
            "commit",
            "save",
            "submit",
            "accept",
        }
        confidence = "medium" if release_reading or sibling is None else "high"
        where = f"'{'.'.join(scope)}'" if scope else "an abort path"
        detail = (
            f"'{hook}' reads as 'the operation completed'; if the override persists "
            f"anything, the abandoned operation is recorded as done"
        )
        if sibling is not None:
            detail += f" -- the same hook is called on a normal path at line {sibling[0].lineno}"
        if release_reading:
            detail += (
                f" (on a '{tail}' the hook may just mean tear-down; check the override)"
            )
        out.append(
            _finding(
                "one-lifecycle-hook-two-meanings",
                "FIX",
                confidence,
                node,
                f"{receiver}.{hook}() is called from {where}, an abort path",
                detail,
            )
        )
    return out


# --------------------------------------------------------------------------
# Shapes banked from the _pyrepl benchmark
# --------------------------------------------------------------------------


def _check_api_value_domain(tree: ast.AST) -> list[dict]:
    """A comparison against a value the API can never return.

    The guard is DEAD -- it looks like validation, reviews like validation, and
    never fires. `unicodedata.category(k)` returns two-letter subclasses, so
    `== "C"` is false for every input, and the control characters the branch was
    written to reject fall through into the accepting branch.
    """
    out: list[dict] = []
    for scope in _iter_scopes(tree):
        nodes = list(_walk_same_scope(scope))
        # Names bound directly from one of the tabulated APIs.
        bound: dict[str, str] = {}
        for node in nodes:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                api = _call_name(node.value)
                if api in _API_VALUE_DOMAINS:
                    for name, _ in _bound_names(node.targets):
                        bound[name] = api
        for node in nodes:
            if not isinstance(node, ast.Compare):
                continue
            if not all(isinstance(op, (ast.Eq, ast.NotEq)) for op in node.ops):
                continue
            api = ""
            if isinstance(node.left, ast.Call):
                api = _call_name(node.left)
            elif isinstance(node.left, ast.Name):
                api = bound.get(node.left.id, "")
            elif isinstance(node.left, ast.Attribute):
                api = _dotted_name(node.left)
            if api not in _API_VALUE_DOMAINS:
                continue
            domain, closed = _API_VALUE_DOMAINS[api]
            for comparator in node.comparators:
                if not isinstance(comparator, ast.Constant) or not isinstance(
                    comparator.value, str
                ):
                    continue
                if comparator.value in domain:
                    continue
                prefixes = sorted(v for v in domain if v.startswith(comparator.value))
                out.append(
                    _finding(
                        "api-value-domain-mismatch",
                        "FIX",
                        "high" if closed else "medium",
                        node,
                        f"{api}() never returns {comparator.value!r}, so this "
                        f"comparison is always "
                        f"{'False' if isinstance(node.ops[0], ast.Eq) else 'True'}",
                        (
                            f"the API returns finer-grained values -- did you mean one of "
                            f"{', '.join(repr(p) for p in prefixes[:6])}?"
                            if prefixes
                            else f"documented domain: {', '.join(sorted(domain)[:8])}..."
                        ),
                    )
                )
    return out


_CONTAINER_TYPES = frozenset(
    {
        "tuple",
        "list",
        "dict",
        "set",
        "frozenset",
        "str",
        "bytes",
        "bytearray",
        "Sequence",
        "Iterable",
        "Collection",
        "Mapping",
        "MutableSequence",
        "MutableMapping",
        "Container",
        "Sized",
        "Reversible",
        "AbstractSet",
    }
)


def _check_isinstance_on_container(tree: ast.AST) -> list[dict]:
    """`isinstance(seq, T)` where `seq` is provably a sequence, not a `T`.

    The scope subscripts the same name -- `cmd[0]` -- so `cmd` holds the spec
    TUPLE while the object built from it lives in a neighbouring variable. The
    test is therefore always false and the branch it guards is dead.

    (The related transposed-argument form, `issubclass(cls, self.last_command)`,
    is NOT checked here: at stdlib scale 31 of 40 matches were legitimate
    lowercase class names, so it is undecidable statically and belongs to the
    agent rather than the scanner.)
    """
    out: list[dict] = []
    conditional = _conditional_body_ids(tree)
    for scope in _iter_scopes(tree):
        nodes = list(_walk_same_scope(scope))
        # A subscript inside a conditional BODY proves nothing -- it usually sits
        # under an `isinstance(value, str)` guard, which is exactly why the name
        # is a sequence there and not elsewhere.
        # Earliest unconditional subscript per name. It must come BEFORE the
        # isinstance: `if not isinstance(other, Counter): return NotImplemented`
        # followed by `other[elem]` is the CORRECT idiom, and matching it made
        # Counter alone supply eight findings.
        subscripted: dict[str, int] = {}
        for n in nodes:
            if not isinstance(n, ast.Subscript) or id(n) in conditional:
                continue
            name = _dotted_name(n.value)
            if name:
                subscripted[name] = min(subscripted.get(name, n.lineno), n.lineno)
        if not subscripted:
            continue
        for node in nodes:
            if not isinstance(node, ast.Call) or len(node.args) < 2:
                continue
            if _call_name(node) not in {"isinstance", "issubclass"}:
                continue
            subject = _dotted_name(node.args[0])
            if subject not in subscripted or subscripted[subject] >= node.lineno:
                continue
            tested = _dotted_name(node.args[1])
            tail = tested.split(".")[-1] if tested else ""
            # Testing a sequence AGAINST a sequence type is the normal idiom.
            if not tail or tail in _CONTAINER_TYPES:
                continue
            if isinstance(node.args[1], (ast.Tuple, ast.List)):
                continue
            out.append(
                _finding(
                    "isinstance-on-container-not-element",
                    "FIX",
                    "high",
                    node,
                    f"'{subject}' is subscripted elsewhere in this scope, so it holds a "
                    f"sequence -- testing it against {tested} is always False",
                    "the object built from the sequence is usually in a neighbouring "
                    "variable; the guard names the container, so the branch is dead",
                )
            )
    return out


def _check_mock_callable_as_spec(tree: ast.AST) -> list[dict]:
    """`MagicMock(lambda ...)` -- the first positional parameter is `spec`.

    The callable is used to derive the mock's ATTRIBUTE SET and is never called,
    so the stub silently returns a `Mock` instead of the value it appears to
    supply. Every assertion downstream then passes vacuously.
    """
    out: list[dict] = []
    lambdas_bound = {
        name
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Lambda)
        for name, _ in _bound_names(node.targets)
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        tail = (_call_name(node) or "").split(".")[-1]
        if tail not in {"Mock", "MagicMock", "AsyncMock", "NonCallableMock"}:
            continue
        first = node.args[0]
        callable_arg = isinstance(first, ast.Lambda) or (
            isinstance(first, ast.Name) and first.id in lambdas_bound
        )
        if not callable_arg:
            continue
        out.append(
            _finding(
                "mock-callable-as-spec",
                "FIX",
                "high",
                node,
                f"{tail}()'s first positional parameter is 'spec', not "
                f"'side_effect' -- this callable is never called",
                "the mock returns a fresh Mock instead of the value the callable "
                "appears to supply, so assertions on it pass vacuously; pass "
                "side_effect=... or return_value=...",
            )
        )
    return out


def _check_decode_error_as_incomplete(tree: ast.AST) -> list[dict]:
    """A decode failure handled as "need more bytes", with no invalid case.

    `except UnicodeError: return` cannot tell an INCOMPLETE multi-byte sequence
    from an INVALID one. On invalid input the buffer is never drained, so it
    grows forever and the stream goes permanently deaf -- an unrecoverable hang
    rather than an error.
    """
    out: list[dict] = []
    # One pass to map each handler to its Try. Resolving the parent by walking
    # the tree per handler is quadratic, and on stdlib-sized files with hundreds
    # of handlers that alone made a full run take minutes.
    owner: dict[int, ast.Try] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            for handler in node.handlers:
                owner[id(handler)] = node
    for candidate in ast.walk(tree):
        if not isinstance(candidate, ast.ExceptHandler):
            continue
        handler = candidate
        caught: set[str] = set()
        if handler.type is not None:
            for n in ast.walk(handler.type):
                dotted = _dotted_name(n)
                if dotted:
                    caught.add(dotted.split(".")[-1])
        if not caught & {"UnicodeError", "UnicodeDecodeError", "ValueError"}:
            continue
        # The handler must simply give up: return / pass / break / continue.
        body = [s for s in handler.body if not _is_docstring(s)]
        if len(body) != 1 or not isinstance(
            body[0], (ast.Return, ast.Pass, ast.Break, ast.Continue)
        ):
            continue
        if isinstance(body[0], ast.Return) and body[0].value is not None:
            continue
        parent_try = owner.get(id(handler))
        if parent_try is None:
            continue
        # Read the method name off the Attribute directly: `_dotted_name`
        # returns "" when a CALL sits in the receiver chain, and
        # `bytes(self.buf).decode(...)` -- the exact exemplar -- is that shape.
        decodes = any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr in {"decode", "decodebytes"}
            for stmt in parent_try.body
            for n in ast.walk(stmt)
        )
        if not decodes:
            continue
        out.append(
            _finding(
                "decode-error-treated-as-incomplete",
                "FIX",
                "high",
                handler,
                "this handler gives up on a decode error without distinguishing "
                "an INCOMPLETE sequence from an INVALID one",
                "on invalid input the buffer is never drained, so it grows without "
                "bound and the stream goes permanently deaf -- a silent hang, not "
                "an error; validate with an incremental decoder or drop the byte",
            )
        )
    return out


def _check_unvalidated_env_numeric(tree: ast.AST) -> list[dict]:
    """`int(os.environ[...])` used as a dimension with no range check.

    The environment is user-controlled, and the branch that reads it is usually
    the one that got LESS scrutiny -- the author debugged the syscall path,
    which is the failure they actually hit, and validated only that one.
    """
    out: list[dict] = []
    for scope in _iter_scopes(tree):
        nodes = list(_walk_same_scope(scope))
        env_calls: list[tuple[ast.Call, str]] = []
        for node in nodes:
            if not (
                isinstance(node, ast.Call)
                and _call_name(node) in {"int", "float"}
                and node.args
                and _reads_environment(node.args[0])
            ):
                continue
            # The value may be validated under the NAME it was bound to rather
            # than as the call expression itself.
            bound = ""
            for stmt in nodes:
                if isinstance(stmt, ast.Assign) and stmt.value is node:
                    names = [n for n, _ in _bound_names(stmt.targets)]
                    bound = names[0] if names else ""
            env_calls.append((node, bound))
        if not env_calls:
            continue

        compared: set[str] = set()
        clamped: set[str] = set()
        guards_non_env = False
        for node in nodes:
            if isinstance(node, ast.Compare):
                operands = [node.left, *node.comparators]
                compared |= {n for o in operands for n in _names_used(o)}
                if not any(_reads_environment(o) for o in operands):
                    guards_non_env = True
            elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
                # `if not height:` -- a validation, even if a partial one.
                compared |= _names_used(node.operand)
                if not _reads_environment(node.operand):
                    guards_non_env = True
            elif isinstance(node, ast.Call) and _call_name(node) in {"max", "min"}:
                clamped |= {n for a in node.args for n in _names_used(a)}

        for call, bound in env_calls:
            if bound and (bound in compared or bound in clamped):
                continue
            if any(
                isinstance(n, ast.Compare)
                and any(o is call for o in [n.left, *n.comparators])
                for n in nodes
            ):
                continue
            out.append(
                _finding(
                    "unvalidated-numeric-from-environment",
                    "FIX",
                    "high" if guards_non_env else "medium",
                    call,
                    "this numeric comes straight from the environment with no range "
                    "check",
                    (
                        "the same function validates the value it gets from another "
                        "source -- the untrusted branch is the unguarded one"
                        if guards_non_env
                        else "a zero or negative value propagates into arithmetic "
                        "downstream (ZeroDivisionError, IndexError, or a silently "
                        "wrong layout)"
                    ),
                )
            )
    return out


def _check_wrapper_mutates_foreign_collection(tree: ast.AST) -> list[dict]:
    """Mutating a collection reached THROUGH another object.

    The owner maintains bookkeeping alongside the collection -- a cursor, a
    parallel list, a dirty flag. Reaching past its API mutates the data and
    leaves the bookkeeping stale, so the owner's own invariants break.
    """
    out: list[dict] = []
    for node in ast.walk(tree):
        target: ast.expr | None = None
        what = ""
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr not in _RESIZING_METHODS:
                continue
            target, what = node.func.value, f".{node.func.attr}()"
        elif isinstance(node, ast.Delete):
            for t in node.targets:
                if isinstance(t, ast.Subscript):
                    target, what = t.value, " del"
                    break
        if target is None:
            continue
        # The receiver must be an ATTRIBUTE OF a call result -- `a.get_x().items`
        # -- which is reaching past another object's API into its data. A bare
        # `a.get_x().append()` is just using the returned object normally.
        if not (
            isinstance(target, ast.Attribute)
            and any(isinstance(n, ast.Call) for n in ast.walk(target.value))
        ):
            continue
        owner = _dotted_name(target) or "the wrapped object"
        out.append(
            _finding(
                "wrapper-mutates-foreign-collection",
                "CONSIDER",
                "medium",
                node,
                f"{what.strip()} mutates a collection reached through another "
                f"object rather than going through its API",
                "if the owner keeps bookkeeping beside the collection -- a cursor, a "
                f"parallel list, a dirty flag -- this leaves it stale ({owner})",
            )
        )
    return out


def _check_save_state_clobbered(tree: ast.AST) -> list[dict]:
    """A snapshot-then-modify method with no idempotence guard.

    Calling it twice overwrites the saved ORIGINAL with the already-modified
    state, so the paired restore puts back the modification instead of the
    original. Re-entry across a signal or a suspend boundary is the usual way in.
    """
    out: list[dict] = []
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef):
            continue
        methods = [
            m
            for m in cls.body
            if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        restored: set[str] = set()
        for method in methods:
            if not any(h in method.name.lower() for h in ("restore", "exit", "close")):
                continue
            for node in ast.walk(method):
                if isinstance(node, ast.Attribute) and _dotted_name(node).startswith(
                    "self."
                ):
                    restored.add(node.attr)
        for method in methods:
            if any(h in method.name.lower() for h in ("restore", "exit", "close")):
                continue
            # Initialization and context entry are SUPPOSED to snapshot; the
            # shape is about a method that can be re-entered.
            if method.name.startswith("__") and method.name.endswith("__"):
                continue
            for stmt in _walk_same_scope(method):
                if not isinstance(stmt, ast.Assign) or not isinstance(
                    stmt.value, ast.Call
                ):
                    continue
                attrs = [
                    t.attr
                    for t in stmt.targets
                    if isinstance(t, ast.Attribute)
                    and isinstance(t.value, ast.Name)
                    and t.value.id == "self"
                ]
                snapshot = [a for a in attrs if a in restored]
                if not snapshot:
                    continue
                # An idempotence guard anywhere in the method discharges it.
                guarded = any(
                    isinstance(n, ast.Compare)
                    and any(
                        isinstance(o, ast.Attribute) and o.attr in snapshot
                        for o in [n.left, *n.comparators]
                    )
                    for n in ast.walk(method)
                )
                if guarded:
                    continue
                # The modify must be the SAME API as the snapshot, differing only
                # get->set (tcgetattr/tcsetattr). Any set*-prefixed call matched
                # far too much: 60 findings, dominated by __init__ assignments.
                snapshot_call = (_call_name(stmt.value) or "").split(".")[-1]
                stem = _get_set_stem(snapshot_call)
                if stem is None:
                    continue
                modifies = any(
                    isinstance(n, ast.Call)
                    and n is not stmt.value
                    and (_call_name(n) or "").split(".")[-1] == stem
                    for n in ast.walk(method)
                )
                if not modifies:
                    continue
                out.append(
                    _finding(
                        "save-state-clobbered-by-reentry",
                        "FIX",
                        "medium",
                        stmt,
                        f"{cls.name}.{method.name}() snapshots self.{snapshot[0]} and "
                        f"then modifies that state, with no guard against running twice",
                        "a second call saves the ALREADY-MODIFIED state as the "
                        "'original', so the paired restore puts back the modification; "
                        "guard the snapshot or split save from apply",
                    )
                )
    return out


def _check_return_ignored_against_family(tree: ast.AST) -> list[dict]:
    """A status-returning call discarded where its siblings are all checked.

    The argument is the file's own convention: when every other call in the same
    family has its result tested, the one that does not is an oversight rather
    than a decision.
    """
    # The observation was about a foreign-function binding, and the convention
    # argument only holds there. Without this gate the check fired on every test
    # module that constructs CamelCase objects: 720 of 787 findings were tests.
    imports = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in getattr(node, "names", [])
    } | {
        getattr(node, "module", None) or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    if not imports & {"ctypes", "_winapi", "msvcrt", "winreg", "_ctypes"}:
        return []

    checked: dict[str, list[ast.Call]] = {}
    discarded: dict[str, list[ast.Call]] = {}
    for node, _ in _walk_with_scope(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            family = _call_family(_call_name(node.value))
            if family:
                discarded.setdefault(family, []).append(node.value)
    # "Checked" means the result is actually TESTED -- an `if`/`while` test, a
    # comparison, an assert, or a raise-if-false. Counting every non-statement
    # position also counted `f(Foo())`, inflating the sibling count.
    tested: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.While)):
            tested.append(node.test)
        elif isinstance(node, ast.Assert):
            tested.append(node.test)
        elif isinstance(node, (ast.Compare, ast.BoolOp, ast.UnaryOp)):
            tested.append(node)
    for root in tested:
        for call in (n for n in ast.walk(root) if isinstance(n, ast.Call)):
            family = _call_family(_call_name(call))
            if family:
                checked.setdefault(family, []).append(call)

    out: list[dict] = []
    for family, calls in discarded.items():
        siblings = checked.get(family, [])
        # Three checked siblings is the threshold at which "the file checks these"
        # becomes a convention rather than a coincidence.
        if len(siblings) < 3:
            continue
        for call in calls:
            out.append(
                _finding(
                    "return-ignored-against-checked-family",
                    "CONSIDER",
                    "medium",
                    call,
                    f"{_call_name(call)}() discards its result, while "
                    f"{len(siblings)} sibling '{family}' calls in this file check theirs",
                    "the file's own convention says this result is a status code; a "
                    "silent failure here leaves the following code operating on state "
                    "it never established",
                )
            )
    return out


def _call_family(name: str) -> str:
    """Group calls that share a status-returning convention.

    Restricted to CamelCase tails, which in Python means a foreign-function or
    Win32-style binding rather than an ordinary method. The first attempt keyed
    on get/set stems instead and collapsed `self.__setstate` and `self.state`
    into one family, producing 1414 findings across the stdlib -- almost all of
    them setters that correctly return None.
    """
    tail = name.split(".")[-1].lstrip("_")
    if not tail[:1].isupper() or tail.isupper() or not any(c.islower() for c in tail):
        return ""
    return name.rsplit(".", 1)[0] if "." in name else "<module>"


def _get_set_stem(name: str) -> str | None:
    """The `set` twin of a `get`-style accessor name, else None."""
    lowered = name.lower()
    for getter, setter in (("tcget", "tcset"), ("get", "set")):
        if lowered.startswith(getter):
            return setter + name[len(getter) :]
    return None


def _reads_environment(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Subscript) and _dotted_name(sub.value).endswith(
            "environ"
        ):
            return True
        if isinstance(sub, ast.Call) and _call_name(sub) in {
            "os.getenv",
            "os.environ.get",
            "environ.get",
            "getenv",
        }:
            return True
    return False


def _conditional_body_ids(tree: ast.AST) -> set[int]:
    """Ids of nodes inside a conditional BODY (not its test).

    A single DFS carrying a flag. Marking each `if` subtree with its own
    ast.walk is quadratic on nested conditionals and cost minutes on a
    stdlib-sized run.
    """
    out: set[int] = set()

    def descend(node: ast.AST, inside: bool) -> None:
        if inside:
            out.add(id(node))
        if isinstance(node, ast.If):
            descend(node.test, inside)
            for stmt in node.body + node.orelse:
                descend(stmt, True)
            return
        if isinstance(node, ast.Try):
            children: list[ast.AST] = [
                *node.body,
                *node.handlers,
                *node.orelse,
                *node.finalbody,
            ]
            for child in children:
                descend(child, True)
            return
        for child in ast.iter_child_nodes(node):
            descend(child, inside)

    descend(tree, False)
    return out


def _is_policy_flag(test: ast.expr) -> bool:
    """True if a guard reads as a POLICY switch rather than a data condition.

    `if should_auto_add_history:` is a policy the inverse must also respect;
    `if x in seen:` or `if len(t) > 2:` is algorithmic, and an unguarded inverse
    beside it is normal.
    """
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        test = test.operand
    if isinstance(test, ast.BoolOp):
        # `if ret and should_auto_add_history:` -- a policy flag ANDed with a
        # data condition is still a policy the inverse must honour.
        return any(_is_policy_flag(v) for v in test.values)
    return bool(_dotted_name(test))


def _is_docstring(stmt: ast.stmt) -> bool:
    return isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)


# --------------------------------------------------------------------------
# Project-level checks -- these compare files against each other, so they take
# the whole parsed corpus rather than one tree.
# --------------------------------------------------------------------------


def _parallel_key(rel_path: str) -> str | None:
    """Strip a platform prefix so parallel implementations share a key."""
    name = rel_path.rsplit("/", 1)[-1]
    for prefix in _PARALLEL_PREFIXES:
        if name.startswith(prefix):
            return name[len(prefix) :]
    return None


def _sentinel_kind(node: ast.AST) -> str | None:
    """Classify an empty-ish literal, which is where the divergence shows up."""
    if isinstance(node, ast.Constant):
        if node.value is None:
            return "None"
        if node.value == "" and isinstance(node.value, str):
            return "empty str"
        if node.value == b"":
            return "empty bytes"
        if node.value == 0 and isinstance(node.value, int):
            return "0"
    if isinstance(node, (ast.List, ast.Tuple)) and not node.elts:
        return "empty sequence"
    if isinstance(node, ast.Dict) and not node.keys:
        return "empty dict"
    return None


def _check_divergent_sentinel(corpus: list[tuple[str, ast.AST]]) -> list[dict]:
    """Parallel implementations constructing one type with different sentinels.

    The guarded-twin relation is INVERTED here, which is why it survives review:
    the side that emits the SAFE value also carries a defensive guard it never
    needs, while the side that emits the dangerous one has none. Looking for
    "which side has the guard" therefore points at the wrong file.
    """
    # (parallel key, constructor, arg position) -> {sentinel: (file, node)}
    seen: dict[tuple[str, str, int], dict[str, tuple[str, ast.AST]]] = {}
    for rel, tree in corpus:
        key = _parallel_key(rel)
        if key is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            ctor = _call_name(node)
            tail = ctor.split(".")[-1] if ctor else ""
            if not tail or not tail[:1].isupper():
                continue
            for index, arg in enumerate(node.args):
                kind = _sentinel_kind(arg)
                if kind is None:
                    continue
                seen.setdefault((key, tail, index), {}).setdefault(kind, (rel, node))

    out: list[dict] = []
    for (key, ctor, index), variants in sorted(seen.items()):
        if len(variants) < 2:
            continue
        ordered = sorted(variants.items())
        for kind, (rel, node) in ordered:
            others = ", ".join(
                f"{k} in {r.rsplit('/', 1)[-1]}" for k, (r, _) in ordered if k != kind
            )
            finding = _finding(
                "divergent-sentinel-across-parallel-modules",
                "FIX",
                "high",
                node,
                f"{ctor}() is constructed with {kind} at argument {index} here, but "
                f"with {others} in the parallel implementation of '{key}'",
                "consumers written against one side break on the other; the side "
                "emitting the safe value often also carries a defensive guard it "
                "does not need, so 'which side has the guard' points at the wrong file",
            )
            finding["file"] = rel
            out.append(finding)
    return out


def _check_unguarded_inverse(corpus: list[tuple[str, ast.AST]]) -> list[dict]:
    """An operation guarded at one site and its inverse unguarded at another.

    The append is conditional on a policy flag; the pop that undoes it is not.
    Turn the policy off and the inverse consumes something it never added.
    """
    guarded: dict[tuple[str, str, str], tuple[str, ast.AST, str]] = {}
    bare: list[tuple[str, str, ast.Call, str, str, str]] = []
    for rel, tree in corpus:
        directory = rel.rsplit("/", 1)[0] if "/" in rel else "."
        conditional = _conditional_body_ids(tree)
        # Only guards that read as a policy switch count. An `if` on the data
        # itself is algorithmic, and an unguarded inverse beside it is normal --
        # that distinction is the whole difference between 164 and a usable
        # number at stdlib scale.
        policy: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.If) and _is_policy_flag(node.test):
                for stmt in node.body + node.orelse:
                    policy.update(id(n) for n in ast.walk(stmt))
        for scope in _iter_scopes(tree):
            for node in _walk_same_scope(scope):
                if not isinstance(node, ast.Call) or not isinstance(
                    node.func, ast.Attribute
                ):
                    continue
                method = node.func.attr
                target = _dotted_name(node.func.value)
                if not target:
                    continue
                # The collection must be OWNED by an object (`reader.history`),
                # not a bare local. A local list is managed by one algorithm and
                # an unguarded inverse beside it is normal; generic local names
                # like `parts`/`lines` otherwise matched across unrelated files.
                if "." not in target:
                    continue
                collection = target.split(".")[-1]
                if collection in {"self", "cls"} or len(collection) < 3:
                    continue
                where = getattr(scope, "name", "<module>")
                for adder, remover in _INVERSE_OPS:
                    if method == adder and id(node) in policy:
                        guarded.setdefault(
                            (directory, collection, adder), (rel, node, where)
                        )
                    elif method == remover and id(node) not in conditional:
                        bare.append((rel, collection, node, adder, remover, where))

    out: list[dict] = []
    for rel, collection, node, adder, remover, scope in bare:
        directory = rel.rsplit("/", 1)[0] if "/" in rel else "."
        source = guarded.get((directory, collection, adder))
        if source is None:
            continue
        # Same function means one algorithm managing its own stack, not a policy
        # the inverse forgot to honour.
        if source[0] == rel and source[2] == scope:
            continue
        where = source[0].rsplit("/", 1)[-1]
        finding = _finding(
            "unguarded-inverse-of-guarded-operation",
            "FIX",
            "medium",
            node,
            f"{collection}.{remover}() is unconditional, but the "
            f"{collection}.{adder}() it undoes is guarded at "
            f"{where}:{getattr(source[1], 'lineno', 0)}",
            "when the guard says no, the inverse still runs and consumes something "
            "it never added -- it removes a neighbouring entry, or raises on empty",
        )
        finding["file"] = rel
        out.append(finding)
    return out


_INVERSE_OPS = (
    ("append", "pop"),
    ("add", "discard"),
    ("add", "remove"),
    ("push", "pop"),
    ("acquire", "release"),
    ("insert", "remove"),
)


# A `str.format` replacement field whose name is an IDENTIFIER, optionally with
# .attr / [key] access, a !conversion and a :spec. Deliberately NOT matching `{}`
# or `{0}`: a bare or numeric field is indistinguishable from a regex quantifier
# (`\d{4}`) or a literal brace in a character class, and those dominate the raw
# candidates. A named field is what an author writes when they meant an f-string.
_NAMED_FORMAT_FIELD = re.compile(
    r"(?<!\{)\{"
    r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*|\[[^\]{}]*\])*"
    r"(?:![rsa])?"
    r"(?::[^{}]*)?"
    r"\}(?!\})"
)

# Callables whose string argument is a MESSAGE -- something a human eventually
# reads. A brace literal anywhere else is usually a template on its way to a
# formatter; a brace literal here has nowhere left to be formatted.
_MESSAGE_SINKS = frozenset(
    {
        "warn",
        "warn_explicit",
        "warning",
        "info",
        "debug",
        "error",
        "critical",
        "exception",
        "log",
        "print",
        "fail",
        "skipTest",
        "abort",
    }
)


def _is_exception_constructor(func: ast.expr) -> bool:
    """True if *func* names something that looks like an exception class."""
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    return bool(name) and name.endswith(("Error", "Exception", "Warning"))


def _check_unformatted_format_string(tree: ast.AST) -> list[dict]:
    """A `{name}` literal reaching a message sink that never formats it."""
    out: list[dict] = []

    raise_calls: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
            raise_calls.add(id(node.exc))

    # Every name bound anywhere in the module. A field naming one of them is the
    # strong case: the author was reaching for an f-string and dropped the `f`.
    bound: set[str] = {"self", "cls"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.alias):
            bound.add((node.asname or node.name).split(".")[0])

    for call in ast.walk(tree):
        if not isinstance(call, ast.Call):
            continue
        is_sink = (
            _is_exception_constructor(call.func)
            or id(call) in raise_calls
            or (
                call.func.attr
                if isinstance(call.func, ast.Attribute)
                else getattr(call.func, "id", "")
            )
            in _MESSAGE_SINKS
        )
        if not is_sink:
            continue
        # ANY other argument means something may still format the string --
        # `trace(line, *k, **kw)` in _pyrepl formats only `if k or kw`. This is
        # the differential that keeps the check at zero false positives.
        if len(call.args) + len(call.keywords) != 1:
            continue
        arg = call.args[0] if call.args else None
        if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
            continue

        text = arg.value
        fields = _NAMED_FORMAT_FIELD.findall(text)
        if not fields:
            continue
        # `${name}` is string.Template / shell syntax, not str.format.
        if all(text[text.index(f) - 1 : text.index(f)] == "$" for f in fields):
            continue

        names = {
            f.lstrip("{").split("}")[0].split("!")[0].split(":")[0].split(".")[0]
            for f in fields
        }
        resolvable = sorted(n for n in names if n in bound)
        shown = ", ".join(sorted(fields)[:3])
        out.append(
            _finding(
                "unformatted-format-string-literal",
                "FIX",
                "high" if resolvable else "medium",
                arg,
                f"{shown} is a format field in a message nothing formats — the "
                f"braces reach the reader verbatim"
                + (
                    f"; {', '.join(resolvable)} is in scope, so an `f` prefix was "
                    "probably intended"
                    if resolvable
                    else ""
                ),
                text[:200],
            )
        )
    return out


_PROJECT_CHECKS = {
    "divergent-sentinel-across-parallel-modules": _check_divergent_sentinel,
    "unguarded-inverse-of-guarded-operation": _check_unguarded_inverse,
}


_CHECKS = {
    "mutable-default-argument": _check_mutable_default,
    "late-binding-closure-in-loop": _check_late_binding_closure,
    "except-clause-ordering-unreachable": _check_except_ordering,
    "return-or-break-in-finally": _check_return_in_finally,
    "eq-without-hash": _check_eq_without_hash,
    "mutation-during-iteration": _check_mutation_during_iteration,
    "asyncio-fire-and-forget-task": _check_fire_and_forget_task,
    "blocking-call-in-async-function": _check_blocking_in_async,
    "unawaited-coroutine": _check_unawaited_coroutine,
    "lru-cache-on-method": _check_lru_cache_on_method,
    "class-level-mutable-attribute": _check_class_level_mutable,
    "bare-except-swallows-control-flow": _check_bare_except,
    "exception-in-del-or-finalizer": _check_exception_in_del,
    "is-comparison-with-literal": _check_is_literal,
    "except-exception-too-broad": _check_except_exception_too_broad,
    "cleanup-only-on-success-path": _check_cleanup_only_on_success,
    "error-reported-below-warning": _check_error_reported_below_warning,
    "except-in-loop-without-exit": _check_except_in_loop_without_exit,
    "raise-without-from-in-except": _check_raise_without_from,
    "flag-not-reset-on-early-exit": _check_flag_not_reset_on_early_exit,
    "guard-rechecks-call-receiver": _check_guard_rechecks_call_receiver,
    "falsy-check-for-none-default": _check_falsy_check_for_none_default,
    "test-cannot-fail": _check_test_cannot_fail,
    "self-referential-accumulate": _check_self_referential_accumulate,
    "duplicated-guard-wrong-operand": _check_duplicated_guard,
    "signed-length-from-untrusted-header": _check_signed_length_from_header,
    "asymmetric-encode-decode-pair": _check_asymmetric_encode_decode,
    "one-lifecycle-hook-two-meanings": _check_lifecycle_hook_two_meanings,
    "api-value-domain-mismatch": _check_api_value_domain,
    "isinstance-on-container-not-element": _check_isinstance_on_container,
    "mock-callable-as-spec": _check_mock_callable_as_spec,
    "decode-error-treated-as-incomplete": _check_decode_error_as_incomplete,
    "unvalidated-numeric-from-environment": _check_unvalidated_env_numeric,
    "wrapper-mutates-foreign-collection": _check_wrapper_mutates_foreign_collection,
    "save-state-clobbered-by-reentry": _check_save_state_clobbered,
    "return-ignored-against-checked-family": _check_return_ignored_against_family,
    "unformatted-format-string-literal": _check_unformatted_format_string,
}


def analyze_file(
    path: Path,
    project_root: Path,
    checks: list[str] | None = None,
    corpus: list[tuple[str, ast.AST]] | None = None,
) -> list[dict]:
    """Run the selected checks over one file. Unparseable files yield nothing.

    ``corpus`` collects (relative path, tree) for the project-level checks, which
    compare files against each other and therefore cannot run here.
    """
    tree = parse_source(path)
    if tree is None:
        return []
    selected = checks or list(_CHECKS)
    findings: list[dict] = []
    rel = relative_to_root(path, project_root)
    if corpus is not None:
        corpus.append((rel, tree))
    for name in selected:
        check = _CHECKS.get(name)
        if check is None:
            continue
        for finding in check(tree):
            finding["file"] = rel
            findings.append(finding)
    return findings


def analyze(
    target: str,
    *,
    max_files: int = 0,
    checks: list[str] | None = None,
    exclude: list[str] | None = None,
) -> dict:
    """Scan *target* for catalogued Python pitfalls.

    ``exclude`` drops files whose project-relative path contains any of the given
    substrings. Real-world runs show generated content (report artifacts, golden
    fixtures, vendored trees) is the dominant false-positive source, and it is
    project-specific enough that it cannot be hardcoded -- hence the option, plus
    the ``by_directory`` breakdown below so such clustering is visible at a glance.
    """
    resolved = Path(target).resolve()
    from scan_common import find_project_root

    project_root = find_project_root(resolved)
    # A file target scans exactly that file. (Several sibling scripts instead
    # fall back to the project root here, which silently turns "scan this file"
    # into "scan everything" -- a trap worth not reproducing. discover_python_files
    # handles a file path directly, so no special-casing is needed.)
    scan_root = resolved
    files, files_total = collect_python_files(scan_root, max_files)

    findings: list[dict] = []
    corpus: list[tuple[str, ast.AST]] = []
    for path in files:
        rel = relative_to_root(path, project_root)
        if exclude and any(pattern in rel for pattern in exclude):
            continue
        findings.extend(analyze_file(path, project_root, checks, corpus))

    for name in checks or list(_PROJECT_CHECKS):
        project_check = _PROJECT_CHECKS.get(name)
        if project_check is not None:
            findings.extend(project_check(corpus))

    # Deterministic ordering -- see explore's --runs cross-run deduplication.
    findings.sort(key=lambda f: (f["file"], f["line"], f["shape"]))

    by_shape: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    by_confidence: dict[str, int] = {}
    by_directory: dict[str, int] = {}
    for finding in findings:
        by_shape[finding["shape"]] = by_shape.get(finding["shape"], 0) + 1
        by_severity[finding["severity"]] = by_severity.get(finding["severity"], 0) + 1
        by_confidence[finding["confidence"]] = (
            by_confidence.get(finding["confidence"], 0) + 1
        )
        top = finding["file"].split("/")[0] if "/" in finding["file"] else "."
        by_directory[top] = by_directory.get(top, 0) + 1

    result = build_envelope(project_root, scan_root, files_total, len(files))
    result["summary"] = {
        "total_findings": len(findings),
        "by_shape": dict(sorted(by_shape.items())),
        "by_severity": dict(sorted(by_severity.items())),
        "by_confidence": dict(sorted(by_confidence.items())),
        # Sorted by count: a single directory dominating usually means generated
        # content, not a real defect cluster. Triage the directory, not each hit.
        "by_directory": dict(
            sorted(by_directory.items(), key=lambda kv: (-kv[1], kv[0]))
        ),
        "checks_run": sorted(checks or (set(_CHECKS) | set(_PROJECT_CHECKS))),
        "excluded_patterns": sorted(exclude or []),
    }
    result["findings"] = findings
    return result


def _extract_options(
    argv: list[str],
) -> tuple[list[str], list[str] | None, list[str] | None]:
    """Pull ``--check`` and ``--exclude`` out of argv, returning the remainder."""
    rest: list[str] = []
    checks: list[str] | None = None
    exclude: list[str] | None = None
    i = 0
    while i < len(argv):
        if argv[i] == "--check" and i + 1 < len(argv):
            checks = [c.strip() for c in argv[i + 1].split(",") if c.strip()]
            i += 2
        elif argv[i] == "--exclude" and i + 1 < len(argv):
            exclude = [p.strip() for p in argv[i + 1].split(",") if p.strip()]
            i += 2
        else:
            rest.append(argv[i])
            i += 1
    return rest, checks, exclude


def main() -> None:
    argv, checks, exclude = _extract_options(sys.argv[1:])
    target, max_files = parse_common_args(argv)
    if checks:
        known = set(_CHECKS) | set(_PROJECT_CHECKS)
        unknown = [c for c in checks if c not in known]
        if unknown:
            emit(
                {
                    "error": f"unknown check(s): {', '.join(unknown)}",
                    "available": sorted(known),
                }
            )
            sys.exit(2)
    emit(analyze(target, max_files=max_files, checks=checks, exclude=exclude))


if __name__ == "__main__":
    main()
