#!/usr/bin/env python3
"""Find lock-discipline and blocking defects in threaded and async Python code.

Ported from ft-review-toolkit's lock-discipline, shared-state and stop-the-world
scanners (which target C extensions) to Python's ``ast``. Each check maps 1:1 to
a shape in data/python_bug_shapes.json whose ``agent`` is ``concurrency-auditor``.

Three families of checks, all project-level because locks and call chains cross
module boundaries:

1. Lock-guard inference. For every class attribute and module global that is
   written after construction, find the lock held at most of its access sites,
   then flag the sites that touch it without that lock. A private helper that is
   only ever called with the lock held inherits it (the entry lockset), so a
   ``_foo_locked`` helper is not a false positive.
2. Lock discipline. ``acquire()`` without a ``finally`` release, an ignored
   ``acquire(timeout=...)`` result, lock-order cycles (from nested ``with``
   blocks AND from calls made while holding a lock), a non-reentrant lock taken
   again through a call chain (including a read-to-write upgrade on a project's
   own reader/writer lock), ``Condition`` misuse, and a slot or semaphore taken
   before the ``try`` that releases it (or released only in ``except
   Exception``, which cancellation skips).
3. Forbidden work in a critical region, followed through the project call graph:
   blocking calls while holding a lock, ``await`` while holding a thread lock,
   ``async def`` code that calls a sync project function which blocks or takes a
   thread lock, and signal handlers that do more than set a flag.

What counts as a lock: objects built from ``threading``/``multiprocessing``/
``asyncio`` primitives; names that look like locks (``_lock``, ``mutex``,
``_cv``, ``cond``, ``sem``); and the project's own context managers whose body
holds a lock around ``yield`` (e.g. ``read_lock()``/``write_lock()`` on a
custom reader/writer lock, or ``_guard_for(key)``).

Calls are resolved through ``self.method``, module functions, ``from x import
f`` aliases, attributes and locals whose type is known from a constructor call,
an annotation or a return annotation (an interface stub resolves to its
implementations), and -- marked ``weak`` -- a method name defined by at most a
few project classes, narrowed to the class the receiver's name points at
(``index`` -> ``SearchIndex``). Every chain-based finding carries its ``chain``
so the agent can check each hop; a weak hop ends in ``[weak: name-only]`` (with
the candidate count when more than one class remains).

Confidence levels:
    high    -- the mechanism holds on every resolved hop
    medium  -- matches the shape, but a legitimate reading exists; agent must check
    low     -- weak signal (heuristic lock, weak call edge); a starting point only

Usage:
    python scan_concurrency.py [path] [--max-files N] [--check ID[,ID...]]
        [--exclude PAT[,PAT...]] [--blocking NAME[,NAME...]] [--all-blocking]

``--all-blocking`` reports every file-I/O and pipe wait under a lock as a
finding; by default they are summarized per lock in
``summary.blocking_low_signal``, because they are mostly by design.
"""

from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scan_common import (  # noqa: E402
    build_envelope,
    collect_python_files,
    emit,
    find_project_root,
    parse_common_args,
    parse_source,
    relative_to_root,
)

# --------------------------------------------------------------------------
# Reference tables
# --------------------------------------------------------------------------

# Factory (fully qualified) -> lock kind. `Lock`, `Condition(Lock())` and
# `Semaphore` deadlock when re-acquired by the holder; `RLock` and a default
# `Condition()` (which wraps an RLock) do not.
_LOCK_FACTORIES = {
    "threading.Lock": "Lock",
    "threading.RLock": "RLock",
    "threading.Condition": "Condition",
    "threading.Semaphore": "Semaphore",
    "threading.BoundedSemaphore": "Semaphore",
    "_thread.allocate_lock": "Lock",
    "_thread.LockType": "Lock",
    "multiprocessing.Lock": "Lock",
    "multiprocessing.RLock": "RLock",
    "multiprocessing.Condition": "Condition",
    "multiprocessing.Semaphore": "Semaphore",
    "asyncio.Lock": "async",
    "asyncio.Condition": "async",
    "asyncio.Semaphore": "async",
    "asyncio.BoundedSemaphore": "async",
    "anyio.Lock": "async",
    "anyio.Semaphore": "async",
    "anyio.Condition": "async",
}
_NON_REENTRANT = frozenset({"Lock", "Semaphore", "Condition(Lock)"})

# A name that is a lock by naming convention.
_LOCKISH_NAME = re.compile(
    r"(?i)(^|_)(r?lock|mutex|cv|cond|condition|sem|semaphore)$|lock$|_lock_|^lock_|locks?_guard$"
)
# A method on a lock object that returns a context manager holding it in a mode.
_MODE_METHODS = re.compile(
    r"(?i)^(try_)?(read|write|shared|exclusive|reader|writer)(_lock)?$"
)
_CONDITION_NAME = re.compile(r"(?i)(^|_)(cv|cond|condition)$")

# Calls that suspend the calling thread, by category. Severity under a lock and
# confidence in async code both follow the category.
_SLEEP_CALLS = frozenset({"time.sleep"})
_NETWORK_CALLS = frozenset(
    {
        "urllib.request.urlopen",
        "socket.create_connection",
        "socket.getaddrinfo",
        "requests.get",
        "requests.post",
        "requests.put",
        "requests.delete",
        "requests.patch",
        "requests.head",
        "requests.request",
        "httpx.get",
        "httpx.post",
        "httpx.put",
        "httpx.delete",
        "httpx.request",
    }
)
_SUBPROCESS_CALLS = frozenset(
    {
        "os.system",
        "os.waitpid",
        "os.wait",
        "subprocess.run",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
    }
)
_FILE_IO_CALLS = frozenset(
    {
        "open",
        "io.open",
        "os.fsync",
        "os.fdatasync",
        "shutil.copy",
        "shutil.copy2",
        "shutil.copyfile",
        "shutil.copytree",
        "shutil.move",
        "shutil.rmtree",
        "json.dump",
        "json.load",
        "pickle.dump",
        "pickle.load",
        "numpy.save",
        "numpy.load",
        "faiss.write_index",
        "faiss.read_index",
        "sqlite3.connect",
    }
)
_FILE_IO_METHODS = frozenset(
    {"read_text", "write_text", "read_bytes", "write_bytes", "fsync", "flush_to_disk"}
)
_LOG_RECEIVERS = re.compile(r"(?i)^(logging|log|logger|_log|_logger|LOG|LOGGER)$")
_LOG_METHODS = frozenset(
    {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
)

# Category -> (severity under a lock, confidence under a lock, label).
_CATEGORY = {
    "sleep": ("FIX", "high", "sleeps"),
    "thread-wait": ("FIX", "medium", "waits on another thread"),
    "network": ("FIX", "medium", "does network I/O"),
    "subprocess": ("FIX", "medium", "waits on a subprocess"),
    "ipc": ("CONSIDER", "low", "waits on a pipe or socket"),
    "file-io": ("CONSIDER", "low", "does file I/O"),
    "custom": ("CONSIDER", "medium", "calls a project-designated slow function"),
}
# Categories only used for some checks: acquiring a lock, logging.
_BLOCKING_CATEGORIES = frozenset(_CATEGORY)

# Method names never resolved by uniqueness: they are methods of builtin types,
# so `x.update()` is far more often a dict than the one project class with an
# `update` method.
_BUILTIN_METHOD_NAMES = frozenset(
    name
    for typ in (str, bytes, dict, list, set, frozenset, tuple, int, float, object)
    for name in dir(typ)
) | frozenset(
    {
        "close",
        "read",
        "write",
        "flush",
        "open",
        "run",
        "start",
        "stop",
        "join",
        "send",
        "recv",
        "wait",
        "result",
        "submit",
        "shutdown",
        "acquire",
        "release",
        "put",
        "get",
        "cancel",
        "set",
        "is_set",
        "execute",
        "commit",
        "rollback",
        "fetchone",
        "fetchall",
        "seek",
        "tell",
        "exists",
        "mkdir",
        "unlink",
        "resolve",
        "info",
        "debug",
        "warning",
        "error",
        "exception",
        "log",
    }
)

# Callables that run their argument elsewhere: a sibling coroutine using one of
# these is the guarded twin of a coroutine that calls the same kind of thing inline.
_OFFLOAD_CALLS = re.compile(
    r"(^|\.)(run_in_executor|to_thread|run_in_threadpool|run_sync|run_sync_in_worker_thread|submit)$"
)

_INIT_METHODS = frozenset(
    {
        "__init__",
        "__new__",
        "__post_init__",
        "__del__",
        "__repr__",
        "__getstate__",
        "__setstate__",
    }
)
_MUTATING_METHODS = frozenset(
    {
        "append",
        "extend",
        "insert",
        "remove",
        "pop",
        "popitem",
        "clear",
        "update",
        "setdefault",
        "add",
        "discard",
        "sort",
        "reverse",
        "appendleft",
        "popleft",
        "extendleft",
        "rotate",
        "move_to_end",
        "difference_update",
        "intersection_update",
        "symmetric_difference_update",
    }
)

_MAX_CHAIN = 8
# A method name defined by at most this many project classes resolves to all of
# them, as weak edges (typically an interface, its implementation, a test double).
_MAX_WEAK_CANDIDATES = 3


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Held:
    """A lock held at a program point."""

    key: str  # stable identity, or "?text" for an anonymous (local) lock
    mode: str  # "", "read" or "write"
    is_async: bool  # acquired with `async with`
    kind: str  # Lock / RLock / Condition / Semaphore / custom / heuristic / async
    text: str  # source text, for messages
    instance: str = ""  # argument text for a lock family (`_lock_for(key)`)

    @property
    def anonymous(self) -> bool:
        return self.key.startswith("?")


@dataclass
class Call:
    node: ast.Call
    held: tuple[Held, ...]
    awaited: bool
    callees: list[tuple[str, bool]] = field(default_factory=list)  # (func key, strong)
    self_call: bool = False


@dataclass
class Access:
    target: str  # attribute or global key
    kind: str  # read / write / mutate / iterate
    held: tuple[Held, ...]
    node: ast.AST
    const_write: bool = False


@dataclass
class Func:
    key: str
    name: str
    qualname: str
    module: str
    rel: str
    cls: str | None  # class key ("mod:Class") or None
    node: ast.FunctionDef | ast.AsyncFunctionDef
    is_async: bool
    nested: dict[str, str] = field(default_factory=dict)  # local name -> func key
    calls: list[Call] = field(default_factory=list)
    accesses: list[Access] = field(default_factory=list)
    acquires: list[tuple[Held, tuple[Held, ...], ast.AST]] = field(default_factory=list)
    awaits_under_lock: list[tuple[ast.AST, tuple[Held, ...]]] = field(
        default_factory=list
    )
    direct_blocking: list[tuple[str, str, ast.Call, tuple[Held, ...]]] = field(
        default_factory=list
    )
    logs: list[ast.Call] = field(default_factory=list)
    bare_acquires: list[tuple[ast.Call, str, str]] = field(default_factory=list)
    cond_waits: list[tuple[ast.Call, str, bool]] = field(default_factory=list)
    cond_notifies: list[tuple[ast.Call, str, tuple[Held, ...]]] = field(
        default_factory=list
    )
    check_then_act: list[tuple[str, ast.AST, tuple[Held, ...], str]] = field(
        default_factory=list
    )
    offloads: bool = False
    value_refs: set[str] = field(default_factory=set)  # self.<m> used as a value


@dataclass
class ClassInfo:
    key: str
    name: str
    module: str
    bases: list[str]
    methods: dict[str, str] = field(default_factory=dict)  # name -> func key
    lock_attrs: dict[str, str] = field(default_factory=dict)  # attr -> kind
    attr_types: dict[str, str] = field(default_factory=dict)  # attr -> class key
    is_lock_class: bool = False
    reentrant: bool = (
        False  # tracks its owner thread (get_ident), so re-entry is allowed
    )


@dataclass
class ModuleInfo:
    name: str
    rel: str
    tree: ast.Module
    imports: dict[str, str] = field(default_factory=dict)  # alias -> dotted target
    functions: dict[str, str] = field(default_factory=dict)  # name -> func key
    classes: dict[str, str] = field(default_factory=dict)  # name -> class key
    lock_globals: dict[str, str] = field(default_factory=dict)  # name -> kind
    globals: set[str] = field(default_factory=set)  # module-level assigned names
    signal_handlers: list[tuple[ast.Call, ast.expr, str | None, str]] = field(
        default_factory=list
    )


# --------------------------------------------------------------------------
# Small AST helpers
# --------------------------------------------------------------------------


def _dotted(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def _text(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except (ValueError, TypeError, RecursionError):  # pragma: no cover
        return "<expr>"


def _module_name(rel: str) -> str:
    parts = list(Path(rel).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts) or rel


def _self_attr(node: ast.AST) -> str | None:
    """`self.x` -> "x"."""
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in ("self", "cls")
    ):
        return node.attr
    return None


def _is_contextmanager(fn: ast.AST) -> bool:
    for dec in getattr(fn, "decorator_list", []):
        name = _dotted(dec.func if isinstance(dec, ast.Call) else dec)
        if name.split(".")[-1] in ("contextmanager", "asynccontextmanager"):
            return True
    return False


def _keyword(node: ast.Call, name: str) -> ast.expr | None:
    for kw in node.keywords:
        if kw.arg == name:
            return kw.value
    return None


# --------------------------------------------------------------------------
# The project index
# --------------------------------------------------------------------------


class Project:
    """Everything the checks need, built in two passes over the corpus."""

    def __init__(self, corpus: list[tuple[str, ast.Module]], extra_blocking: set[str]):
        self.modules: dict[str, ModuleInfo] = {}
        self.classes: dict[str, ClassInfo] = {}
        self.funcs: dict[str, Func] = {}
        self.method_index: dict[str, list[str]] = {}  # method name -> func keys
        self.extra_blocking = extra_blocking
        # contextmanager func key -> ("own", None) | ("param", index) | None
        self.lock_cms: dict[str, tuple[str, int | None] | None] = {}
        # For a lock CM: the kind and mode it inherits from the locks it holds at
        # `yield`, and (when it holds another project lock CM) the one it wraps.
        self.lock_cm_kind: dict[str, str] = {}
        self.lock_cm_mode: dict[str, str] = {}
        self.lock_cm_via: dict[str, str] = {}
        self.lock_cm_alias: dict[str, str] = {}  # derived CM -> wrapped lock's key
        self._subclasses: dict[str, list[str]] | None = None
        for rel, tree in corpus:
            self._index_module(rel, tree)
        self._infer_attr_types()
        self._resolve_lock_cms()
        for func in self.funcs.values():
            _FunctionScanner(self, func).run()

    # -- pass 1: names ------------------------------------------------------

    def _index_module(self, rel: str, tree: ast.Module) -> None:
        mod = ModuleInfo(_module_name(rel), rel, tree)
        self.modules[mod.name] = mod
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        mod.imports[alias.asname] = alias.name
                    else:
                        top = alias.name.split(".")[0]
                        mod.imports[top] = top
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    pkg = mod.name.split(".")
                    if not rel.endswith("__init__.py"):
                        pkg = pkg[:-1]
                    pkg = pkg[: len(pkg) - (node.level - 1)] if node.level > 1 else pkg
                    base = ".".join(pkg + ([base] if base else []))
                for alias in node.names:
                    mod.imports[alias.asname or alias.name] = f"{base}.{alias.name}"
        for stmt in tree.body:
            self._index_toplevel(mod, stmt)

    def _index_toplevel(self, mod: ModuleInfo, stmt: ast.stmt) -> None:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            mod.functions[stmt.name] = self._add_func(mod, stmt, None, stmt.name)
        elif isinstance(stmt, ast.ClassDef):
            self._index_class(mod, stmt)
        elif isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            for tgt in targets:
                if isinstance(tgt, ast.Name):
                    mod.globals.add(tgt.id)
                    kind = self._factory_kind(mod, stmt.value)
                    if kind:
                        mod.lock_globals[tgt.id] = kind
        elif isinstance(stmt, (ast.If, ast.Try)):
            for child in ast.iter_child_nodes(stmt):
                if isinstance(child, ast.stmt):
                    self._index_toplevel(mod, child)
            for handler in getattr(stmt, "handlers", []):
                for child in handler.body:
                    self._index_toplevel(mod, child)

    def _index_class(self, mod: ModuleInfo, node: ast.ClassDef) -> None:
        key = f"{mod.name}:{node.name}"
        info = ClassInfo(key, node.name, mod.name, [_dotted(b) for b in node.bases])
        self.classes[key] = info
        mod.classes[node.name] = key
        for stmt in node.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                info.methods[stmt.name] = self._add_func(
                    mod, stmt, key, f"{node.name}.{stmt.name}"
                )
                self.method_index.setdefault(stmt.name, []).append(
                    info.methods[stmt.name]
                )
        names = set(info.methods)
        info.is_lock_class = bool(
            re.search(r"(?i)(lock|mutex|semaphore)$", node.name)
            or {"acquire", "release"} <= names
            or any(_MODE_METHODS.match(n) for n in names)
        )
        info.reentrant = info.is_lock_class and any(
            (isinstance(n, ast.Name) and n.id in ("get_ident", "current_thread"))
            or (
                isinstance(n, ast.Attribute)
                and n.attr in ("get_ident", "current_thread")
            )
            for n in ast.walk(node)
        )
        # Class-level fields: `lock: threading.Lock = field(default_factory=threading.Lock)`.
        for stmt in node.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                kind = None
                value = stmt.value
                if isinstance(value, ast.Call) and _dotted(value.func).endswith(
                    "field"
                ):
                    factory = _keyword(value, "default_factory")
                    if factory is not None:
                        kind = _LOCK_FACTORIES.get(self.qualify(mod, factory))
                if kind is None:
                    kind = _LOCK_FACTORIES.get(self.qualify(mod, stmt.annotation))
                if kind:
                    info.lock_attrs[stmt.target.id] = kind
        # Attribute kinds from assignments anywhere in the class body.
        for sub in ast.walk(node):
            if not isinstance(sub, (ast.Assign, ast.AnnAssign)) or sub.value is None:
                continue
            targets = sub.targets if isinstance(sub, ast.Assign) else [sub.target]
            for tgt in targets:
                attr = _self_attr(tgt)
                if attr is None:
                    continue
                kind = self._factory_kind(mod, sub.value)
                if kind:
                    info.lock_attrs[attr] = kind
                elif isinstance(sub.value, ast.Call):
                    cls_key = self.resolve_class(mod, sub.value.func)
                    if cls_key:
                        info.attr_types[attr] = cls_key

    def _add_func(
        self,
        mod: ModuleInfo,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        cls: str | None,
        qualname: str,
    ) -> str:
        key = f"{mod.name}:{qualname}"
        func = Func(
            key,
            node.name,
            qualname,
            mod.name,
            mod.rel,
            cls,
            node,
            isinstance(node, ast.AsyncFunctionDef),
        )
        self.funcs[key] = func
        # Nested defs are functions of their own (closures handed to executors,
        # route handlers inside a router factory); they keep the enclosing class.
        for sub in _walk_scope(node):
            if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                func.nested[sub.name] = self._add_func(
                    mod, sub, cls, f"{qualname}.<locals>.{sub.name}"
                )
        return key

    def qualify(self, mod: ModuleInfo, node: ast.AST) -> str:
        """Dotted name with its first component resolved through the imports."""
        name = _dotted(node)
        if not name:
            return ""
        head, _, rest = name.partition(".")
        target = mod.imports.get(head)
        if target:
            return f"{target}.{rest}" if rest else target
        return name

    def _factory_kind(self, mod: ModuleInfo, value: ast.AST | None) -> str | None:
        if not isinstance(value, ast.Call):
            return None
        name = self.qualify(mod, value.func)
        kind = _LOCK_FACTORIES.get(name)
        if kind is None:
            short = name.split(".")[-1]
            if short in ("Lock", "RLock", "Condition", "Semaphore", "BoundedSemaphore"):
                # A bare name imported from somewhere we don't know: threading's
                # semantics are the common case.
                kind = {"BoundedSemaphore": "Semaphore"}.get(short, short)
            else:
                cls_key = self.resolve_class(mod, value.func)
                if cls_key and self.classes[cls_key].is_lock_class:
                    info = self.classes[cls_key]
                    return "custom-reentrant" if info.reentrant else "custom"
                return None
        if kind == "Condition" and value.args:
            inner = self._factory_kind(mod, value.args[0])
            if inner == "Lock":
                return "Condition(Lock)"
        return kind

    def resolve_class(self, mod: ModuleInfo, func: ast.AST) -> str | None:
        if isinstance(func, ast.Name):
            if func.id in mod.classes:
                return mod.classes[func.id]
            target = mod.imports.get(func.id)
            if target:
                return self._class_by_dotted(target)
        elif isinstance(func, ast.Attribute):
            return self._class_by_dotted(self.qualify(mod, func))
        return None

    def _class_by_dotted(self, dotted: str) -> str | None:
        modname, _, name = dotted.rpartition(".")
        key = f"{modname}:{name}"
        return key if key in self.classes else None

    def func_by_dotted(self, dotted: str, _depth: int = 0) -> str | None:
        if _depth > 5:
            return None
        modname, _, name = dotted.rpartition(".")
        mod = self.modules.get(modname)
        if mod is None:
            return None
        if name in mod.functions:
            return mod.functions[name]
        if name in mod.classes:
            return self.classes[mod.classes[name]].methods.get("__init__")
        # A re-export: `from .impl import Store` in a package __init__.
        target = mod.imports.get(name)
        if target and target != dotted:
            return self.func_by_dotted(target, _depth + 1)
        return None

    def method_of(
        self, cls_key: str, name: str, _seen: set[str] | None = None
    ) -> str | None:
        """Look *name* up on a project class and its project bases."""
        seen = _seen or set()
        if cls_key in seen or cls_key not in self.classes:
            return None
        seen.add(cls_key)
        info = self.classes[cls_key]
        if name in info.methods:
            return info.methods[name]
        mod = self.modules[info.module]
        for base in info.bases:
            base_key = (
                self.resolve_class(mod, ast.parse(base, mode="eval").body)
                if base
                else None
            )
            if base_key:
                found = self.method_of(base_key, name, seen)
                if found:
                    return found
        return None

    def lock_attr_kind(self, cls_key: str | None, attr: str) -> str | None:
        seen: set[str] = set()
        while cls_key and cls_key in self.classes and cls_key not in seen:
            seen.add(cls_key)
            info = self.classes[cls_key]
            if attr in info.lock_attrs:
                return info.lock_attrs[attr]
            mod = self.modules[info.module]
            nxt = None
            for base in info.bases:
                if base:
                    nxt = self.resolve_class(mod, ast.parse(base, mode="eval").body)
                    if nxt:
                        break
            cls_key = nxt
        return None

    # -- pass 1a: what type an attribute or a local holds --------------------

    def annotation_class(self, mod: ModuleInfo, ann: ast.AST | None) -> str | None:
        """The project class an annotation names (`X`, `X | None`, `Optional[X]`)."""
        if ann is None:
            return None
        if isinstance(ann, ast.Constant) and isinstance(ann.value, str):
            try:
                ann = ast.parse(ann.value, mode="eval").body
            except SyntaxError:
                return None
        if isinstance(ann, ast.BinOp):  # X | None
            return self.annotation_class(mod, ann.left) or self.annotation_class(
                mod, ann.right
            )
        if isinstance(ann, ast.Subscript):
            # Only wrappers that still mean "an X": `list[X]` is not an X.
            if _dotted(ann.value).split(".")[-1] not in _TYPE_WRAPPERS:
                return None
            inner = ann.slice
            elts = inner.elts if isinstance(inner, ast.Tuple) else [inner]
            for elt in elts:
                found = self.annotation_class(mod, elt)
                if found:
                    return found
            return None
        return self.resolve_class(mod, ann)

    def return_class(self, func_key: str) -> str | None:
        func = self.funcs[func_key]
        return self.annotation_class(self.modules[func.module], func.node.returns)

    def value_class(
        self,
        mod: ModuleInfo,
        cls_key: str | None,
        value: ast.AST | None,
        env: dict[str, str],
    ) -> str | None:
        """The project class *value* evaluates to, when its provenance shows it:
        a constructor, a call with a return annotation, an annotated parameter
        (*env*), an attribute of known type, or either side of `a or b`."""
        if isinstance(value, ast.Call):
            found = self.resolve_class(mod, value.func)
            if found:
                return found
            func = value.func
            callee = None
            method = _self_attr(func)
            if method and cls_key:
                callee = self.method_of(cls_key, method)
            elif isinstance(func, ast.Name) and func.id in mod.functions:
                callee = mod.functions[func.id]
            return self.return_class(callee) if callee else None
        if isinstance(value, ast.BoolOp):
            for operand in value.values:
                found = self.value_class(mod, cls_key, operand, env)
                if found:
                    return found
            return None
        if isinstance(value, ast.IfExp):
            return self.value_class(mod, cls_key, value.body, env) or self.value_class(
                mod, cls_key, value.orelse, env
            )
        if isinstance(value, ast.Name):
            return env.get(value.id)
        attr = _self_attr(value)
        if attr and cls_key and cls_key in self.classes:
            return self.classes[cls_key].attr_types.get(attr)
        return None

    def param_env(
        self, mod: ModuleInfo, fn: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> dict[str, str]:
        args = fn.args
        env: dict[str, str] = {}
        for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
            found = self.annotation_class(mod, arg.annotation)
            if found:
                env[arg.arg] = found
        return env

    def _infer_attr_types(self) -> None:
        """`self.x = param`, `self.x = a or Factory()`, `self.x: X = ...`: the
        attribute's class, beyond the direct constructor call pass 1 sees."""
        for info in self.classes.values():
            mod = self.modules[info.module]
            for func_key in info.methods.values():
                fn = self.funcs[func_key].node
                env = self.param_env(mod, fn)
                for sub in _walk_scope(fn):
                    if isinstance(sub, ast.AnnAssign):
                        attr = _self_attr(sub.target)
                        found = self.annotation_class(mod, sub.annotation)
                    elif isinstance(sub, ast.Assign) and len(sub.targets) == 1:
                        attr = _self_attr(sub.targets[0])
                        found = (
                            self.value_class(mod, info.key, sub.value, env)
                            if attr
                            else None
                        )
                    else:
                        continue
                    if attr and found and attr not in info.lock_attrs:
                        info.attr_types.setdefault(attr, found)

    def implementations(self, cls_key: str, name: str) -> list[str]:
        """Non-stub definitions of *name* in the project subclasses of *cls_key*."""
        if self._subclasses is None:
            self._subclasses = {}
            for key, info in self.classes.items():
                mod = self.modules[info.module]
                for base in info.bases:
                    try:
                        base_node = ast.parse(base, mode="eval").body
                    except SyntaxError:
                        continue
                    base_key = self.resolve_class(mod, base_node) if base else None
                    if base_key:
                        self._subclasses.setdefault(base_key, []).append(key)
        out: list[str] = []
        seen = {cls_key}
        queue = list(self._subclasses.get(cls_key, ()))
        while queue:
            sub = queue.pop(0)
            if sub in seen:
                continue
            seen.add(sub)
            found = self.classes[sub].methods.get(name)
            if found and not _is_stub(self.funcs[found].node):
                out.append(found)
            queue.extend(self._subclasses.get(sub, ()))
        return out

    # -- pass 1b: which context managers hold a lock -------------------------

    def _resolve_lock_cms(self) -> None:
        """Decide, for each project @contextmanager, whether it holds a lock at `yield`.

        Three rounds:

        1. Direct: a `with` around `yield` on a lock (or on a parameter), or an
           `acquire()` in the body.
        2. A fixpoint over the project's own context managers: a CM whose `with`
           around `yield` enters another lock CM holds that lock too
           (`with self._locks(key): yield`), however deep the nesting.
        3. A second fixpoint for the kind: a CM inherits the reentrancy of the
           locks it holds at `yield`, so a CM over an `RLock` is reentrant and one
           over a plain `Lock` is not.
        """
        cms = {k: f for k, f in self.funcs.items() if _is_contextmanager(f.node)}
        for key, func in cms.items():
            self.lock_cms[key] = self._direct_lock_cm(func)
        scanners: dict[str, _FunctionScanner] = {}

        def scanner(key: str) -> _FunctionScanner:
            if key not in scanners:
                scanners[key] = _FunctionScanner(self, cms[key])
            return scanners[key]

        for _ in range(len(cms) + 1):
            changed = False
            for key, func in cms.items():
                if self.lock_cms[key] is not None:
                    continue
                for expr, is_async in _yield_with_items(func.node):
                    if not isinstance(expr, ast.Call):
                        continue
                    inner = scanner(key)._lock_of(expr, is_async)
                    if inner is not None:
                        self.lock_cms[key] = ("own", None)
                        self.lock_cm_via[key] = inner.text
                        if not inner.anonymous:
                            # Same lock, two spellings: `with self._guard(k)` and
                            # `with self._guards(k)` must count as one guard.
                            self.lock_cm_alias[key] = inner.key
                        changed = True
                        break
            if not changed:
                break
        for _ in range(len(cms) + 1):
            changed = False
            for key, func in cms.items():
                if self.lock_cms[key] != ("own", None):
                    continue
                held = self._cm_held(scanner(key), func)
                kind = _combine_kinds([h.kind for h in held])
                modes = {h.mode for h in held}
                mode = modes.pop() if len(modes) == 1 else ""
                if (
                    self.lock_cm_kind.get(key) != kind
                    or self.lock_cm_mode.get(key) != mode
                ):
                    self.lock_cm_kind[key] = kind
                    self.lock_cm_mode[key] = mode
                    changed = True
            if not changed:
                break

    def _direct_lock_cm(self, func: Func) -> tuple[str, int | None] | None:
        params = [a.arg for a in func.node.args.args]
        for expr, _is_async in _yield_with_items(func.node):
            base = expr.func if isinstance(expr, ast.Call) else expr
            while isinstance(base, ast.Attribute) and _MODE_METHODS.match(base.attr):
                base = base.value
            if isinstance(base, ast.Name) and base.id in params:
                return ("param", params.index(base.id))
            if self._looks_like_lock(func, expr):
                return ("own", None)
        # `lock.acquire()` ... `yield` ... `lock.release()`
        receivers = [
            n.func.value
            for n in ast.walk(func.node)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "acquire"
        ]
        if not receivers:
            return None
        # `lock.acquire()` on a parameter: the caller's argument is the lock.
        if len(receivers) == 1:
            recv = receivers[0]
            if isinstance(recv, ast.Name) and recv.id in params:
                return ("param", params.index(recv.id))
        return ("own", None)

    @staticmethod
    def _cm_held(scanner: _FunctionScanner, func: Func) -> list[Held]:
        """The locks a context manager holds at `yield`, as far as they resolve.

        A `with` item that resolves to no lock (an `ExitStack`, a helper CM) or an
        `enter_context()` call may hold a lock this cannot see, so it adds an
        unknown (`custom`) entry: the CM is then never taken to be reentrant.
        """
        held: list[Held] = []
        unknown = Held("?", "", False, "custom", "?")
        for expr, is_async in _yield_with_items(func.node):
            lock = scanner._lock_of(expr, is_async)
            held.append(lock if lock is not None else unknown)
        if any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr in ("enter_context", "enter_async_context")
            for n in ast.walk(func.node)
        ):
            held.append(unknown)
        for n in ast.walk(func.node):
            if (
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "acquire"
            ):
                lock = scanner._lock_of(n.func.value, False, explicit=True)
                if lock is not None:
                    held.append(lock)
        return held

    def _looks_like_lock(self, func: Func, expr: ast.AST) -> bool:
        base = expr.func if isinstance(expr, ast.Call) else expr
        while isinstance(base, ast.Attribute) and _MODE_METHODS.match(base.attr):
            base = base.value
        attr = _self_attr(base)
        if attr and self.lock_attr_kind(func.cls, attr):
            return True
        name = base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", "")
        return bool(name and _LOCKISH_NAME.search(name))


def _yield_with_items(fn: ast.AST) -> list[tuple[ast.expr, bool]]:
    """`with` items whose block contains the function's own `yield`."""
    out: list[tuple[ast.expr, bool]] = []
    for node in _walk_scope(fn):
        if not isinstance(node, (ast.With, ast.AsyncWith)):
            continue
        if not any(
            isinstance(n, (ast.Yield, ast.YieldFrom))
            for stmt in node.body
            for n in [stmt, *_walk_scope(stmt)]
        ):
            continue
        out.extend(
            (item.context_expr, isinstance(node, ast.AsyncWith)) for item in node.items
        )
    return out


_REENTRANT_KINDS = ("custom-reentrant", "RLock", "Condition")
_TYPE_WRAPPERS = frozenset(
    {"Optional", "Union", "Annotated", "Final", "ClassVar", "type", "Type"}
)


def _is_stub(fn: ast.AST) -> bool:
    """A body that does nothing: `...`, `pass`, a docstring, `raise NotImplementedError`."""
    body = list(getattr(fn, "body", []))
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    for stmt in body:
        if isinstance(stmt, ast.Pass):
            continue
        if (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Constant)
            and stmt.value.value is Ellipsis
        ):
            continue
        if isinstance(stmt, ast.Raise) and stmt.exc is not None:
            exc = stmt.exc.func if isinstance(stmt.exc, ast.Call) else stmt.exc
            if _dotted(exc).endswith("NotImplementedError"):
                continue
        return False
    return True


def _receiver_matches(receiver: ast.AST, func: Func) -> bool:
    """Does the receiver's name point at the class that defines *func*?

    `index.query(...)` -> `SearchIndex`, `self._cache.put(...)` -> `DiskCache`.
    """
    if not func.cls:
        return False
    if isinstance(receiver, ast.Call):
        receiver = receiver.func
    name = (
        receiver.attr
        if isinstance(receiver, ast.Attribute)
        else getattr(receiver, "id", "")
    )
    cls_name = func.cls.split(":", 1)[-1].lower()
    tokens = [t for t in name.lower().split("_") if len(t) >= 3]
    tokens = [t for t in tokens if t not in ("get", "ensure", "make", "new", "self")]
    return any(t in cls_name for t in tokens)


def _combine_kinds(kinds: list[str]) -> str:
    """The kind of a context manager that holds all of *kinds* at once.

    One non-reentrant lock makes the whole CM non-reentrant; it is reentrant only
    when every lock it holds is known to be. Anything else is unknown (`custom`).
    """
    for kind in kinds:
        if kind in _NON_REENTRANT:
            return kind
    if kinds and all(k == "async" for k in kinds):
        return "async"
    if kinds and all(k in _REENTRANT_KINDS for k in kinds):
        return next(k for k in _REENTRANT_KINDS if k in kinds)
    return "custom"


def _walk_scope(node: ast.AST):
    """Descendants of *node*, not entering nested function or class scopes."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield child  # the def itself, not its body
            continue
        if isinstance(child, (ast.Lambda, ast.ClassDef)):
            continue
        yield child
        yield from _walk_scope(child)


# --------------------------------------------------------------------------
# Pass 2: per-function facts, with the set of locks held at each point
# --------------------------------------------------------------------------


class _FunctionScanner:
    def __init__(self, project: Project, func: Func):
        self.p = project
        self.f = func
        self.mod = project.modules[func.module]
        self.globals_declared: set[str] = set()
        self.locals: set[str] = set()
        self.param_types: dict[str, str] = {}
        for stmt in ast.walk(func.node):
            if isinstance(stmt, ast.Global):
                self.globals_declared.update(stmt.names)
        args = func.node.args
        for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
            self.locals.add(arg.arg)
            cls_key = self._annotation_class(arg.annotation)
            if cls_key:
                self.param_types[arg.arg] = cls_key
        for extra in (args.vararg, args.kwarg):
            if extra:
                self.locals.add(extra.arg)
        self.local_types = self._local_types()
        for node in _walk_scope(func.node):
            if (
                isinstance(node, ast.Name)
                and isinstance(node.ctx, ast.Store)
                and node.id not in self.globals_declared
            ):
                self.locals.add(node.id)

    def _annotation_class(self, ann: ast.AST | None) -> str | None:
        return self.p.annotation_class(self.mod, ann)

    def _local_types(self) -> dict[str, str]:
        """`index = self._index`, `db = self._open_db()`: a local's class, when
        every assignment to it agrees."""
        seen: dict[str, str | None] = {}
        for node in _walk_scope(self.f.node):
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                name = node.target.id
                found = self._annotation_class(node.annotation)
            elif (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
            ):
                name = node.targets[0].id
                found = self.p.value_class(
                    self.mod, self.f.cls, node.value, self.param_types
                )
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.withitem)):
                target = (
                    node.target
                    if not isinstance(node, ast.withitem)
                    else node.optional_vars
                )
                if isinstance(target, ast.Name):
                    seen[target.id] = None
                continue
            else:
                continue
            if name in self.param_types:
                continue
            if name in seen and seen[name] != found:
                seen[name] = None
            else:
                seen.setdefault(name, found)
        return {k: v for k, v in seen.items() if v}

    def run(self) -> None:
        self._body(self.f.node.body, (), loop_depth=0)

    def _check_then_act(self, stmt: ast.If, held: tuple[Held, ...]) -> None:
        _check_then_act_collect(self, stmt, held)

    # -- statements ---------------------------------------------------------

    def _body(
        self, stmts: list[ast.stmt], held: tuple[Held, ...], loop_depth: int
    ) -> None:
        for stmt in stmts:
            held = self._stmt(stmt, held, loop_depth)

    def _stmt(
        self, stmt: ast.stmt, held: tuple[Held, ...], loop_depth: int
    ) -> tuple[Held, ...]:
        """Visit one statement; return the lockset in effect after it."""
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for dec in getattr(stmt, "decorator_list", []):
                self._expr(dec, held, loop_depth)
            return held
        if isinstance(stmt, (ast.With, ast.AsyncWith)):
            inner = held
            for item in stmt.items:
                self._expr(item.context_expr, inner, loop_depth)
                lock = self._lock_of(item.context_expr, isinstance(stmt, ast.AsyncWith))
                if lock is not None:
                    self.f.acquires.append((lock, inner, item.context_expr))
                    inner = inner + (lock,)
            self._body(stmt.body, inner, loop_depth)
            return held
        if isinstance(stmt, ast.If):
            self._check_then_act(stmt, held)
            self._expr(stmt.test, held, loop_depth)
            self._body(stmt.body, held, loop_depth)
            self._body(stmt.orelse, held, loop_depth)
            return self._after_acquire_stmt(stmt, held)
        if isinstance(stmt, ast.While):
            self._expr(stmt.test, held, loop_depth + 1)
            self._body(stmt.body, held, loop_depth + 1)
            self._body(stmt.orelse, held, loop_depth)
            return held
        if isinstance(stmt, (ast.For, ast.AsyncFor)):
            self._expr(stmt.iter, held, loop_depth)
            self._iteration(stmt.iter, held)
            self._targets([stmt.target], held, loop_depth)
            self._body(stmt.body, held, loop_depth)
            self._body(stmt.orelse, held, loop_depth)
            return held
        if isinstance(stmt, (ast.Try, getattr(ast, "TryStar", ast.Try))):
            self._body(stmt.body, held, loop_depth)
            for handler in stmt.handlers:
                self._body(handler.body, held, loop_depth)
            self._body(stmt.orelse, held, loop_depth)
            self._body(stmt.finalbody, held, loop_depth)
            # A lock released in `finally` is no longer held after the try.
            released = self._released_in(stmt.finalbody)
            return tuple(h for h in held if h.text not in released)
        if isinstance(stmt, ast.Match):
            self._expr(stmt.subject, held, loop_depth)
            for case in stmt.cases:
                self._body(case.body, held, loop_depth)
            return held
        if isinstance(stmt, ast.Assign):
            self._expr(stmt.value, held, loop_depth)
            self._targets(stmt.targets, held, loop_depth, value=stmt.value)
            return self._after_acquire_stmt(stmt, held)
        if isinstance(stmt, ast.AnnAssign):
            if stmt.value is not None:
                self._expr(stmt.value, held, loop_depth)
                self._targets([stmt.target], held, loop_depth, value=stmt.value)
            return held
        if isinstance(stmt, ast.AugAssign):
            self._expr(stmt.value, held, loop_depth)
            self._targets([stmt.target], held, loop_depth, aug=True)
            return held
        if isinstance(stmt, ast.Delete):
            self._targets(stmt.targets, held, loop_depth, delete=True)
            return held
        if isinstance(stmt, ast.Expr):
            self._expr(stmt.value, held, loop_depth)
            return self._after_acquire_stmt(stmt, held)
        for child in ast.iter_child_nodes(stmt):
            if isinstance(child, ast.expr):
                self._expr(child, held, loop_depth)
        return held

    def _after_acquire_stmt(
        self, stmt: ast.stmt, held: tuple[Held, ...]
    ) -> tuple[Held, ...]:
        """`lock.acquire()` as a statement holds the lock until `lock.release()`."""
        for node in ast.walk(stmt):
            if not isinstance(node, ast.Call) or not isinstance(
                node.func, ast.Attribute
            ):
                continue
            receiver = node.func.value
            if node.func.attr == "acquire":
                lock = self._lock_of(receiver, False, explicit=True)
                if lock is not None and lock.text not in {h.text for h in held}:
                    self.f.acquires.append((lock, held, node))
                    held = held + (lock,)
            elif node.func.attr == "release":
                text = _text(receiver)
                held = tuple(h for h in held if h.text != text)
        return held

    @staticmethod
    def _released_in(stmts: list[ast.stmt]) -> set[str]:
        out: set[str] = set()
        for stmt in stmts:
            for node in ast.walk(stmt):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "release"
                ):
                    out.add(_text(node.func.value))
        return out

    def _targets(
        self,
        targets: list[ast.expr],
        held: tuple[Held, ...],
        loop_depth: int,
        *,
        value: ast.expr | None = None,
        aug: bool = False,
        delete: bool = False,
    ) -> None:
        for tgt in targets:
            if isinstance(tgt, (ast.Tuple, ast.List)):
                self._targets(tgt.elts, held, loop_depth, aug=aug, delete=delete)
                continue
            if isinstance(tgt, ast.Starred):
                self._targets([tgt.value], held, loop_depth, aug=aug, delete=delete)
                continue
            key = self._state_key(tgt)
            if key:
                const = isinstance(value, ast.Constant) and not aug
                self.f.accesses.append(
                    Access(key, "write", held, tgt, const_write=const)
                )
                continue
            if isinstance(tgt, ast.Subscript):
                key = self._state_key(tgt.value)
                if key:
                    self.f.accesses.append(Access(key, "mutate", held, tgt))
                self._expr(tgt.value, held, loop_depth, skip_state=bool(key))
                self._expr(tgt.slice, held, loop_depth)
            elif isinstance(tgt, ast.Attribute):
                self._expr(tgt.value, held, loop_depth)

    # -- expressions ----------------------------------------------------------

    def _expr(
        self,
        node: ast.AST,
        held: tuple[Held, ...],
        loop_depth: int,
        skip_state: bool = False,
    ) -> None:
        stack: list[tuple[ast.AST, bool]] = [(node, False)]
        while stack:
            cur, awaited = stack.pop()
            if isinstance(cur, ast.Lambda):
                continue
            if isinstance(cur, ast.Await):
                if any(not h.is_async for h in held):
                    self.f.awaits_under_lock.append((cur, held))
                stack.append((cur.value, True))
                continue
            if isinstance(cur, ast.Call):
                self._call(cur, held, awaited, loop_depth)
                stack.extend((a, False) for a in cur.args)
                stack.extend((k.value, False) for k in cur.keywords)
                func = cur.func
                if isinstance(func, ast.Attribute):
                    key = self._state_key(func.value)
                    if key and not skip_state:
                        kind = "mutate" if func.attr in _MUTATING_METHODS else "read"
                        self.f.accesses.append(Access(key, kind, held, cur))
                    elif not (
                        isinstance(func.value, ast.Name)
                        and func.value.id in ("self", "cls")
                    ):
                        stack.append((func.value, False))
                elif not isinstance(func, ast.Name):
                    stack.append((func, False))
                continue
            if isinstance(cur, (ast.Attribute, ast.Name)) and not skip_state:
                key = self._state_key(cur)
                if key and isinstance(cur.ctx, ast.Load):
                    self.f.accesses.append(Access(key, "read", held, cur))
                    continue
                attr = _self_attr(cur)
                if attr and isinstance(cur.ctx, ast.Load):
                    self.f.value_refs.add(attr)
            if isinstance(
                cur, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
            ):
                for gen in cur.generators:
                    self._iteration(gen.iter, held)
            stack.extend((child, False) for child in ast.iter_child_nodes(cur))

    def _iteration(self, it: ast.expr, held: tuple[Held, ...]) -> None:
        """`for x in self._d` / `self._d.items()` iterates shared state."""
        base = it
        if (
            isinstance(base, ast.Call)
            and isinstance(base.func, ast.Attribute)
            and base.func.attr in ("items", "keys", "values")
            and not base.args
        ):
            base = base.func.value
        key = self._state_key(base)
        if key:
            self.f.accesses.append(Access(key, "iterate", held, it))

    def _state_key(self, node: ast.AST) -> str | None:
        """Key for shared mutable state: a `self.` attribute or a module global."""
        attr = _self_attr(node)
        if attr is not None and self.f.cls:
            if self.p.lock_attr_kind(self.f.cls, attr) or _LOCKISH_NAME.search(attr):
                return None
            if self.p.method_of(self.f.cls, attr):
                return None
            return f"{self.f.cls}.{attr}"
        if isinstance(node, ast.Name):
            name = node.id
            declared = name in self.globals_declared
            if (
                name in self.mod.globals
                and name not in self.mod.lock_globals
                and (declared or name not in self.locals)
            ):
                if name.isupper() and not declared:
                    return None  # a constant by convention
                return f"{self.f.module}:{name}"
        return None

    # -- locks ------------------------------------------------------------------

    def _lock_of(
        self, expr: ast.AST, is_async: bool, explicit: bool = False
    ) -> Held | None:
        """The lock a `with` item (or an `.acquire()` receiver) holds, if any."""
        if isinstance(expr, ast.Call):
            func = expr.func
            # `self._rw.read_lock()`: a mode of a lock object. Checked before
            # resolving the method, because a reader/writer lock's own `read_lock()`
            # releases its internal condition before `yield` and would look lock-free.
            if isinstance(func, ast.Attribute) and _MODE_METHODS.match(func.attr):
                base = self._lock_of(func.value, is_async)
                if base is not None:
                    return Held(
                        base.key, _mode_of(func.attr), is_async, base.kind, _text(expr)
                    )
            callee = self._resolve_callees(expr)
            for key, _strong in callee:
                cm = self.p.lock_cms.get(key)
                if key in self.p.lock_cms:
                    if cm is None:
                        return None  # a project context manager that holds no lock
                    if cm[0] == "param" and cm[1] is not None:
                        idx = cm[1] - (1 if self.p.funcs[key].cls else 0)
                        if 0 <= idx < len(expr.args):
                            return self._lock_of(expr.args[idx], is_async)
                    family = self.p.funcs[key].qualname
                    instance = ", ".join(_text(a) for a in expr.args)
                    mode = _mode_of(self.p.funcs[key].name) or self.p.lock_cm_mode.get(
                        key, ""
                    )
                    kind = self.p.lock_cm_kind.get(key, "custom")
                    return Held(
                        self.p.lock_cm_alias.get(
                            key, f"{self.p.funcs[key].module}:{family}"
                        ),
                        mode,
                        is_async or kind == "async",
                        kind,
                        _text(expr),
                        instance,
                    )
            if isinstance(func, ast.Attribute) and _MODE_METHODS.match(func.attr):
                return None
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else getattr(func, "id", "")
            )
            if name and _LOCKISH_NAME.search(name):
                owner = self.f.cls if _self_attr(func) else self.f.module
                instance = ", ".join(_text(a) for a in expr.args)
                return Held(
                    f"{owner}.{name}(*)" if owner else f"?{_text(func)}",
                    _mode_of(name),
                    is_async,
                    "heuristic",
                    _text(expr),
                    instance,
                )
            return None
        attr = _self_attr(expr)
        if attr is not None:
            kind = self.p.lock_attr_kind(self.f.cls, attr)
            if kind is None and not _LOCKISH_NAME.search(attr):
                return None
            kind = kind or "heuristic"
            return Held(
                f"{self.f.cls}.{attr}" if self.f.cls else f"?{_text(expr)}",
                "",
                is_async or kind == "async",
                kind,
                _text(expr),
            )
        if isinstance(expr, ast.Name):
            kind = self.mod.lock_globals.get(expr.id)
            if kind and expr.id not in self.locals:
                return Held(
                    f"{self.f.module}:{expr.id}",
                    "",
                    is_async or kind == "async",
                    kind,
                    expr.id,
                )
            if (
                _LOCKISH_NAME.search(expr.id)
                or explicit
                and _LOCKISH_NAME.search(expr.id)
            ):
                return Held(f"?{expr.id}", "", is_async, "heuristic", expr.id)
            return None
        if isinstance(expr, ast.Attribute) and _LOCKISH_NAME.search(expr.attr):
            # `slot.lock`, `state.search_lock`: identity known only by its text.
            return Held(
                f"{self.f.module}:{_text(expr)}", "", is_async, "heuristic", _text(expr)
            )
        return None

    # -- calls --------------------------------------------------------------------

    def _call(
        self, node: ast.Call, held: tuple[Held, ...], awaited: bool, loop_depth: int
    ) -> None:
        name = self.p.qualify(self.mod, node.func)
        func = node.func
        method = func.attr if isinstance(func, ast.Attribute) else ""
        receiver = func.value if isinstance(func, ast.Attribute) else None
        if _OFFLOAD_CALLS.search(name or method or ""):
            self.f.offloads = True
        call = Call(node, held, awaited)
        call.callees = self._resolve_callees(node)
        call.self_call = receiver is not None and _self_attr(func) is not None
        if isinstance(func, ast.Name) and func.id in self.f.nested:
            call.self_call = True
        self.f.calls.append(call)
        category = self._blocking_category(node, name, method, receiver, held)
        if category:
            self.f.direct_blocking.append((category, name or _text(func), node, held))
        logs = name == "print" or (
            receiver is not None
            and method in _LOG_METHODS
            and bool(_LOG_RECEIVERS.match(_dotted(receiver).split(".")[-1] or ""))
        )
        if logs:
            self.f.logs.append(node)
        # Explicit acquire / Condition usage.
        if method == "acquire" and receiver is not None:
            lock = self._lock_of(receiver, False, explicit=True)
            if lock is not None:
                self.f.bare_acquires.append((node, _text(receiver), lock.kind))
        if (
            method in ("wait", "wait_for", "notify", "notify_all")
            and receiver is not None
        ):
            cond = self._condition_key(receiver)
            if cond:
                if method == "wait":
                    self.f.cond_waits.append((node, cond, loop_depth > 0))
                elif method in ("notify", "notify_all"):
                    self.f.cond_notifies.append((node, cond, held))
        if name in ("signal.signal",) and len(node.args) >= 2:
            handler = node.args[1]
            keys = self._resolve_value(handler)
            self.mod.signal_handlers.append((node, handler, keys, self.f.key))

    def _condition_key(self, receiver: ast.AST) -> str | None:
        attr = _self_attr(receiver)
        if attr is not None:
            kind = self.p.lock_attr_kind(self.f.cls, attr)
            if kind in ("Condition", "Condition(Lock)") or (
                kind is None and _CONDITION_NAME.search(attr)
            ):
                return _text(receiver)
            return None
        if isinstance(receiver, ast.Name):
            kind = self.mod.lock_globals.get(receiver.id)
            if kind in ("Condition", "Condition(Lock)") or _CONDITION_NAME.search(
                receiver.id
            ):
                return receiver.id
        return None

    def _resolve_value(self, node: ast.AST) -> str | None:
        """A function referenced (not called): `handler`, `self.on_exit`."""
        if isinstance(node, ast.Name):
            if node.id in self.f.nested:
                return self.f.nested[node.id]
            if node.id in self.mod.functions:
                return self.mod.functions[node.id]
            target = self.mod.imports.get(node.id)
            return self.p.func_by_dotted(target) if target else None
        attr = _self_attr(node)
        if attr and self.f.cls:
            return self.p.method_of(self.f.cls, attr)
        return None

    def _resolve_callees(self, node: ast.Call) -> list[tuple[str, bool]]:
        func = node.func
        if isinstance(func, ast.Name):
            if func.id in self.f.nested:
                return [(self.f.nested[func.id], True)]
            if func.id in self.locals:
                return []
            if func.id in self.mod.functions:
                return [(self.mod.functions[func.id], True)]
            if func.id in self.mod.classes:
                init = self.p.classes[self.mod.classes[func.id]].methods.get("__init__")
                return [(init, True)] if init else []
            target = self.mod.imports.get(func.id)
            if target:
                found = self.p.func_by_dotted(target)
                return [(found, True)] if found else []
            return []
        if not isinstance(func, ast.Attribute):
            return []
        method = func.attr
        receiver = func.value
        # self.m(...)
        if (
            isinstance(receiver, ast.Name)
            and receiver.id in ("self", "cls")
            and self.f.cls
        ):
            found = self.p.method_of(self.f.cls, method)
            return [(found, True)] if found else []
        # super().m(...)
        if (
            isinstance(receiver, ast.Call)
            and isinstance(receiver.func, ast.Name)
            and receiver.func.id == "super"
            and self.f.cls
        ):
            info = self.p.classes[self.f.cls]
            for base in info.bases:
                base_key = self.p.resolve_class(
                    self.mod, ast.parse(base, mode="eval").body
                )
                if base_key:
                    found = self.p.method_of(base_key, method)
                    if found:
                        return [(found, True)]
            return []
        # module.f(...) / package.module.f(...)
        dotted = self.p.qualify(self.mod, func)
        if dotted:
            found = self.p.func_by_dotted(dotted)
            if found:
                return [(found, True)]
        # self.attr.m(...) with a known attribute type, or param.m(...) with an annotation.
        cls_key = None
        attr = _self_attr(receiver)
        if attr and self.f.cls:
            cls_key = self.p.classes[self.f.cls].attr_types.get(attr)
        elif isinstance(receiver, ast.Name):
            cls_key = self.param_types.get(receiver.id) or self.local_types.get(
                receiver.id
            )
        if cls_key:
            found = self.p.method_of(cls_key, method)
            if found and _is_stub(self.p.funcs[found].node):
                # An interface (Protocol / ABC): the code that runs is an
                # implementation. One subclass is as good as the class itself.
                impls = self.p.implementations(cls_key, method)
                if impls:
                    return [(k, len(impls) == 1) for k in impls]
                return self._weak_candidates(method, receiver) or [(found, True)]
            return [(found, True)] if found else []
        # Any other receiver: a method name only a few project classes define.
        return self._weak_candidates(method, receiver)

    def _weak_candidates(
        self, method: str, receiver: ast.AST
    ) -> list[tuple[str, bool]]:
        """Name-only resolution: the project classes that define *method*.

        Interface stubs are dropped when a real body exists, and when the
        receiver's name points at one class (`index` -> `SearchIndex`) the
        others are dropped too. What is left is a weak edge each.
        """
        if method in _BUILTIN_METHOD_NAMES or method.startswith("__"):
            return []
        candidates = self.p.method_index.get(method, [])
        real = [c for c in candidates if not _is_stub(self.p.funcs[c].node)]
        if real:
            candidates = real
        if len(candidates) > 1:
            named = [
                c for c in candidates if _receiver_matches(receiver, self.p.funcs[c])
            ]
            if named:
                candidates = named
        if 0 < len(candidates) <= _MAX_WEAK_CANDIDATES:
            return [(c, False) for c in candidates]
        return []

    def _blocking_category(
        self,
        node: ast.Call,
        name: str,
        method: str,
        receiver: ast.AST | None,
        held: tuple[Held, ...],
    ) -> str | None:
        if name in self.p.extra_blocking or method and method in self.p.extra_blocking:
            return "custom"
        if name in _SLEEP_CALLS:
            return "sleep"
        if name in _NETWORK_CALLS:
            return "network"
        if name in _SUBPROCESS_CALLS or method == "communicate":
            return "subprocess"
        if name in _FILE_IO_CALLS or (
            name.startswith("np.") and name.split(".")[-1] in ("save", "load")
        ):
            return "file-io"
        if method in _FILE_IO_METHODS:
            return "file-io"
        if receiver is None:
            return None
        if isinstance(receiver, ast.Constant):
            return None  # ", ".join(...)
        rname = _dotted(receiver).split(".")[-1].lower() if _dotted(receiver) else ""
        positional = len(node.args)
        timeout_kw = {k.arg for k in node.keywords} <= {"timeout", "block"}
        if method == "join" and positional <= 1 and timeout_kw:
            if positional == 1 and not isinstance(
                node.args[0], (ast.Constant, ast.Name)
            ):
                return None
            if (
                positional == 1
                and isinstance(node.args[0], ast.Name)
                and "timeout" not in (node.args[0].id)
            ):
                return None
            if (
                re.search(r"thread|worker|proc|process|^t$|_t$", rname)
                or positional == 0
            ):
                return "thread-wait"
        if method == "result" and positional <= 1 and timeout_kw:
            return "thread-wait"
        if method == "wait" and positional <= 1 and timeout_kw:
            text = _text(receiver)
            if any(h.text == text for h in held):
                return None  # Condition.wait releases its own lock
            return "thread-wait"
        if (
            method == "get"
            and timeout_kw
            and positional == 0
            and re.search(r"queue|^q$|_q$", rname)
        ):
            return "thread-wait"
        if method in ("recv", "recv_bytes", "poll") and re.search(
            r"conn|pipe|sock", rname
        ):
            return "ipc"
        return None


def _mode_of(name: str) -> str:
    low = name.lower()
    if "read" in low or "shared" in low:
        return "read"
    if "write" in low or "exclusive" in low:
        return "write"
    return ""


# --------------------------------------------------------------------------
# Pass 3: interprocedural summaries
# --------------------------------------------------------------------------


@dataclass
class Step:
    func: str
    rel: str
    line: int
    text: str
    weak: str = ""  # why this hop is uncertain ("weak: 1 of 2 candidates")

    def render(self) -> str:
        out = f"{self.rel}:{self.line} {self.func}: {self.text}"
        return f"{out} [{self.weak}]" if self.weak else out


def _call_step(func: Func, call: Call, strong: bool, text: str = "") -> Step:
    """The chain step for *call*, marked `[weak]` when resolved by method name only."""
    weak = ""
    if not strong:
        n = sum(1 for _c, st in call.callees if not st)
        weak = f"weak: name-only, 1 of {n} candidates" if n > 1 else "weak: name-only"
    return Step(
        func.qualname,
        func.rel,
        getattr(call.node, "lineno", 0),
        text or f"calls {_text(call.node.func)}(...)",
        weak,
    )


class Summaries:
    """Transitive facts over the call graph: what a call to f may block on or acquire."""

    def __init__(self, project: Project):
        self.p = project
        # func -> category -> (chain, strong)
        self.blocks: dict[str, dict[str, tuple[list[Step], bool]]] = {}
        # func -> lock key -> (Held, chain, strong, same_instance)
        self.acquires: dict[str, dict[str, tuple[Held, list[Step], bool, bool]]] = {}
        self.entry_held: dict[str, frozenset[str]] = {}
        self.slow_locks: dict[str, list[Step]] = {}
        self.slow_strong: dict[
            str, bool
        ] = {}  # every hop of the witness resolved strongly
        self._direct()
        self._propagate()
        self._entry_locksets()
        self._find_slow_locks()

    def _step(self, func: Func, node: ast.AST, text: str) -> Step:
        return Step(func.qualname, func.rel, getattr(node, "lineno", 0), text)

    def _direct(self) -> None:
        for key, func in self.p.funcs.items():
            blocks: dict[str, tuple[list[Step], bool]] = {}
            for category, name, node, _held in func.direct_blocking:
                blocks.setdefault(
                    category, ([self._step(func, node, f"{name}(...)")], True)
                )
            for lock, _held, node in func.acquires:
                if lock.is_async:
                    continue
                blocks.setdefault(
                    "lock", ([self._step(func, node, f"acquires {lock.text}")], True)
                )
            if func.logs:
                blocks.setdefault(
                    "logging",
                    ([self._step(func, func.logs[0], _text(func.logs[0].func))], True),
                )
            self.blocks[key] = blocks
            acq: dict[str, tuple[Held, list[Step], bool, bool]] = {}
            for lock, _held, node in func.acquires:
                if lock.anonymous:
                    continue
                acq.setdefault(
                    lock.key,
                    (
                        lock,
                        [self._step(func, node, f"acquires {lock.text}")],
                        True,
                        True,
                    ),
                )
            self.acquires[key] = acq

    def _edges(self):
        for key, func in self.p.funcs.items():
            for call in func.calls:
                for callee, strong in call.callees:
                    if callee is None or callee not in self.p.funcs:
                        continue
                    target = self.p.funcs[callee]
                    # Calling a coroutine function runs nothing until awaited, and an
                    # awaited one runs on the loop: its own body is reported on its own.
                    if target.is_async:
                        continue
                    yield key, func, call, callee, strong

    def _propagate(self) -> None:
        edges = list(self._edges())
        changed = True
        rounds = 0
        while changed and rounds < 50:
            changed = False
            rounds += 1
            for key, func, call, callee, strong in edges:
                step = _call_step(func, call, strong)
                for category, (chain, cstrong) in self.blocks[callee].items():
                    if len(chain) >= _MAX_CHAIN:
                        continue
                    if category not in self.blocks[key]:
                        self.blocks[key][category] = (
                            [step, *chain],
                            strong and cstrong,
                        )
                        changed = True
                for lock_key, (lock, chain, cstrong, same) in self.acquires[
                    callee
                ].items():
                    if len(chain) >= _MAX_CHAIN or lock_key in self.acquires[key]:
                        continue
                    self.acquires[key][lock_key] = (
                        lock,
                        [step, *chain],
                        strong and cstrong,
                        same and call.self_call,
                    )
                    changed = True

    def _entry_locksets(self) -> None:
        """Locks a function can assume are held on entry: the intersection over its
        callers. Public, dunder and callback functions assume nothing, because
        callers outside the project (or outside the call graph) exist."""
        callers: dict[str, list[tuple[str, frozenset[str]]]] = {}
        value_refs: set[tuple[str | None, str]] = set()
        for func in self.p.funcs.values():
            for ref in func.value_refs:
                value_refs.add((func.cls, ref))
            for call in func.calls:
                if not call.self_call:
                    continue
                for callee, strong in call.callees:
                    if strong and callee in self.p.funcs:
                        held = frozenset(h.key for h in call.held if not h.anonymous)
                        callers.setdefault(callee, []).append((func.key, held))
        inferred = {
            key
            for key, func in self.p.funcs.items()
            if func.name.startswith("_")
            and not func.name.startswith("__")
            and key in callers
            and (func.cls, func.name) not in value_refs
            and "<locals>" not in func.qualname
        }
        top = frozenset({"*"})  # not yet constrained
        entry: dict[str, frozenset[str]] = {
            key: (top if key in inferred else frozenset()) for key in self.p.funcs
        }
        for _ in range(50):
            changed = False
            for key in inferred:
                acc: frozenset[str] | None = None
                for caller, held in callers[key]:
                    if entry[caller] == top:
                        continue
                    here = held | entry[caller]
                    acc = here if acc is None else acc & here
                new = acc if acc is not None else top
                if new != entry[key]:
                    entry[key] = new
                    changed = True
            if not changed:
                break
        self.entry_held = {
            k: (frozenset() if v == top else v) for k, v in entry.items()
        }

    def _find_slow_locks(self) -> None:
        """Locks some code holds across blocking work: waiting for one is slow too.

        Holding L while waiting for a slow lock M makes L slow as well, so this
        runs to a fixpoint. Each lock keeps one witness chain, preferring one whose
        every hop resolved strongly.
        """

        def mark(held: tuple[Held, ...], chain: list[Step], strong: bool) -> bool:
            changed = False
            for h in held:
                if h.anonymous or h.is_async:
                    continue
                if h.key not in self.slow_locks or (
                    strong and not self.slow_strong[h.key]
                ):
                    self.slow_locks[h.key] = chain
                    self.slow_strong[h.key] = strong
                    changed = True
            return changed

        for func in self.p.funcs.values():
            for _category, name, node, held in func.direct_blocking:
                mark(held, [self._step(func, node, f"{name}(...)")], True)
        for _ in range(20):
            changed = False
            for func in self.p.funcs.values():
                for lock, held, node in func.acquires:
                    if held and lock.key in self.slow_locks:
                        step = self._step(func, node, f"waits for {lock.text}")
                        changed |= mark(
                            held,
                            [step, *self.slow_locks[lock.key]],
                            self.slow_strong[lock.key],
                        )
                for call in func.calls:
                    if not call.held:
                        continue
                    for callee, strong in call.callees:
                        step = _call_step(
                            func, call, strong, f"calls {_text(call.node.func)}"
                        )
                        for category, (chain, cstrong) in self.blocks.get(
                            callee, {}
                        ).items():
                            if category in _BLOCKING_CATEGORIES:
                                changed |= mark(
                                    call.held, [step, *chain], strong and cstrong
                                )
                        for key, (_lock, chain, cstrong, _same) in self.acquires.get(
                            callee, {}
                        ).items():
                            if key in self.slow_locks:
                                changed |= mark(
                                    call.held,
                                    [
                                        step,
                                        *chain,
                                        *_meanwhile(_lock.text, self.slow_locks[key]),
                                    ],
                                    strong and cstrong and self.slow_strong[key],
                                )
            if not changed:
                break


def _meanwhile(lock_text: str, witness: list[Step]) -> list[Step]:
    """Splice a slow lock's witness into a chain, marking where the hop changes thread."""
    first = witness[0]
    return [
        Step(
            first.func,
            first.rel,
            first.line,
            f"meanwhile, another thread holds {lock_text} here: {first.text}",
        ),
        *witness[1:],
    ]


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------


def _finding(
    shape: str,
    severity: str,
    confidence: str,
    func: Func,
    node: ast.AST,
    message: str,
    detail: str = "",
    chain: list[Step] | None = None,
) -> dict:
    out = {
        "shape": shape,
        "type": shape,
        "severity": severity,
        "confidence": confidence,
        "file": func.rel,
        "line": getattr(node, "lineno", 0),
        "column": getattr(node, "col_offset", 0),
        "function": func.qualname,
        "message": message,
        "detail": detail,
    }
    if chain:
        out["chain"] = [s.render() for s in chain]
    return out


def _lower(confidence: str) -> str:
    return {"high": "medium", "medium": "low"}.get(confidence, "low")


def _held_keys(func: Func, held: tuple[Held, ...], s: Summaries) -> set[str]:
    return {h.key for h in held if not h.anonymous} | set(
        s.entry_held.get(func.key, ())
    )


def _check_guards(p: Project, s: Summaries) -> tuple[list[dict], list[dict]]:
    """unguarded-access-to-lock-guarded-state, plus the inferred guard map."""
    by_target: dict[str, list[tuple[Func, Access, set[str]]]] = {}
    for func in p.funcs.values():
        if func.name in _INIT_METHODS:
            continue
        for acc in func.accesses:
            by_target.setdefault(acc.target, []).append(
                (func, acc, _held_keys(func, acc.held, s))
            )
    findings: list[dict] = []
    guards: list[dict] = []
    for target, sites in sorted(by_target.items()):
        writes = [x for x in sites if x[1].kind in ("write", "mutate")]
        if not writes or len(sites) < 3:
            continue
        counts: dict[str, int] = {}
        for _f, _a, held in sites:
            for key in held:
                counts[key] = counts.get(key, 0) + 1
        if not counts:
            continue
        lock, guarded = max(counts.items(), key=lambda kv: (kv[1], kv[0]))
        ratio = guarded / len(sites)
        if guarded < 2 or ratio < 0.5:
            continue
        guards.append(
            {
                "state": target,
                "lock": lock,
                "guarded_sites": guarded,
                "total_sites": len(sites),
            }
        )
        for func, acc, held in sites:
            if lock in held or acc.kind == "read":
                continue
            if acc.kind == "iterate":
                severity, confidence = "FIX", "medium"
                what = "iterates"
                consequence = (
                    "a concurrent writer under the lock can change its size mid-iteration "
                    "(RuntimeError) or the loop sees a half-applied update"
                )
            else:
                what = "writes" if acc.kind == "write" else "mutates"
                severity = "FIX"
                confidence = "high" if ratio >= 0.75 and guarded >= 3 else "medium"
                if acc.const_write:
                    confidence = _lower(confidence)
                consequence = (
                    "this update can interleave with, or be lost to, a guarded one"
                )
            name = target.split(":", 1)[-1]
            findings.append(
                _finding(
                    "unguarded-access-to-lock-guarded-state",
                    severity,
                    confidence,
                    func,
                    acc.node,
                    f"{func.qualname}() {what} {name} without holding {lock.split(':', 1)[-1]}, "
                    f"which guards {guarded} of its {len(sites)} access sites",
                    f"{consequence}. Check whether every caller already holds the lock "
                    f"(then document it), or take the lock here.",
                )
            )
    findings.extend(_unguarded_helper_calls(p, s, guards))
    return findings, guards


def _uses_instance(func: Func) -> bool:
    """A method that reads `self` state (a static/pure helper needs no lock)."""
    if any(
        _dotted(d) in ("staticmethod", "classmethod") for d in func.node.decorator_list
    ):
        return False
    return bool(func.accesses or any(c.self_call for c in func.calls))


def _unguarded_helper_calls(p: Project, s: Summaries, guards: list[dict]) -> list[dict]:
    """A private helper that its callers almost always invoke under one lock.

    The call-level twin of a guarded attribute: fifty methods wrap
    `self._ensure_index()` in the state lock and one calls it bare. Only
    `self.` calls to private methods count, and the bar is higher than for
    state (three-quarters of at least four call sites), because a helper may
    legitimately be safe to call unlocked.
    """
    sites: dict[str, list[tuple[Func, Call, set[str]]]] = {}
    for func in p.funcs.values():
        if func.name in _INIT_METHODS:
            continue
        for call in func.calls:
            if not call.self_call:
                continue
            for callee, strong in call.callees:
                target = p.funcs.get(callee)
                if (
                    not strong
                    or target is None
                    or not target.name.startswith("_")
                    or target.name.startswith("__")
                    or callee == func.key
                    or not _uses_instance(target)
                ):
                    continue
                sites.setdefault(callee, []).append(
                    (func, call, _held_keys(func, call.held, s))
                )
    out: list[dict] = []
    for callee, calls in sorted(sites.items()):
        if len(calls) < 4:
            continue
        counts: dict[str, int] = {}
        for _f, _c, held in calls:
            for key in held:
                counts[key] = counts.get(key, 0) + 1
        if not counts:
            continue
        lock, guarded = max(counts.items(), key=lambda kv: (kv[1], kv[0]))
        if guarded / len(calls) < 0.75:
            continue
        target = p.funcs[callee]
        guards.append(
            {
                "state": f"{target.qualname}()",
                "lock": lock,
                "guarded_sites": guarded,
                "total_sites": len(calls),
            }
        )
        for func, call, held in calls:
            if lock in held:
                continue
            out.append(
                _finding(
                    "unguarded-access-to-lock-guarded-state",
                    "FIX",
                    "medium",
                    func,
                    call.node,
                    f"{func.qualname}() calls {target.name}() without holding "
                    f"{lock.split(':', 1)[-1]}; {guarded} of its {len(calls)} call sites hold it",
                    "the other callers are the guarded twin: check what the lock protects "
                    "across this call (a concurrent replace/close of the state the helper "
                    "returns), and take it here or document why this caller is exempt",
                )
            )
    return out


def _written_under_lock(p: Project, s: Summaries) -> set[str]:
    """State written or mutated with some lock held, at least once."""
    return {
        acc.target
        for func in p.funcs.values()
        for acc in func.accesses
        if acc.kind in ("write", "mutate") and _held_keys(func, acc.held, s)
    }


def _check_check_then_act(p: Project, s: Summaries, guarded: set[str]) -> list[dict]:
    out: list[dict] = []
    for func in p.funcs.values():
        if func.name in _INIT_METHODS:
            continue
        for target, node, held, form in func.check_then_act:
            if _held_keys(func, held, s):
                continue
            owner_has_lock = bool(func.cls and p.classes[func.cls].lock_attrs) or bool(
                p.modules[func.module].lock_globals
            )
            if target in guarded:
                confidence = "medium"
            elif owner_has_lock:
                confidence = "low"
            else:
                continue
            name = target.split(":", 1)[-1]
            out.append(
                _finding(
                    "check-then-act-without-lock",
                    "FIX",
                    confidence,
                    func,
                    node,
                    f"{form} on {name} without a lock: two threads can both pass the check "
                    f"and both act",
                    "hold the lock across the check and the act (double-checked locking: "
                    "re-check inside the lock), or use an atomic operation (dict.setdefault)",
                )
            )
    return out


def _check_then_act_collect(
    scanner: _FunctionScanner, stmt: ast.If, held: tuple[Held, ...]
) -> None:
    """Record `if <state> is None: <state> = ...` and `if k not in <state>: <state>[k] = ...`."""
    test = stmt.test
    target = None
    form = ""
    if isinstance(test, ast.Compare) and len(test.ops) == 1:
        op, right = test.ops[0], test.comparators[0]
        if (
            isinstance(op, ast.Is)
            and isinstance(right, ast.Constant)
            and right.value is None
        ):
            target = scanner._state_key(test.left)
            form = "lazy initialization"
        elif isinstance(op, ast.NotIn):
            target = scanner._state_key(right)
            form = "check-then-insert"
    elif isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        target = scanner._state_key(test.operand)
        form = "lazy initialization"
    if not target:
        return
    for sub in stmt.body:
        for node in ast.walk(sub):
            if isinstance(node, (ast.With, ast.AsyncWith)):
                return  # double-checked locking: the act happens under a lock
        for node in ast.walk(sub):
            tgt = None
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    base = t.value if isinstance(t, ast.Subscript) else t
                    if scanner._state_key(base) == target:
                        tgt = t
            if tgt is not None:
                scanner.f.check_then_act.append((target, stmt, held, form))
                return


def _check_bare_acquire(p: Project) -> list[dict]:
    out: list[dict] = []
    for func in p.funcs.values():
        for node, text, kind in func.bare_acquires:
            if kind == "async":
                continue
            if (
                func.name in ("__enter__", "__aenter__", "acquire")
                or _is_contextmanager(func.node)
                and _released_in_finally(func.node, text)
            ):
                continue  # the lock protocol itself: released by __exit__ / the finally
            parent_stmt = _parents(func.node).get(id(node))
            # `if not lock.acquire(timeout=...)`, `ok = lock.acquire(False)`: result used.
            uses_result = not (
                isinstance(parent_stmt, ast.Expr) and parent_stmt.value is node
            )
            blocking_kw = _keyword(node, "blocking")
            nonblocking = bool(
                _keyword(node, "timeout") is not None
                or (blocking_kw is not None and not _is_true(blocking_kw))
                or (node.args and not _is_true(node.args[0]))
                or len(node.args) >= 2
            )
            if nonblocking and not uses_result:
                out.append(
                    _finding(
                        "lock-acquire-result-ignored",
                        "FIX",
                        "high",
                        func,
                        node,
                        f"{text}.acquire({', '.join(_text(a) for a in node.args)}"
                        f"{', ' if node.args and node.keywords else ''}"
                        f"{', '.join(_text(k) for k in node.keywords)}) can return False, "
                        f"but the result is discarded: the code runs unlocked",
                        "check the return value and handle the timeout",
                    )
                )
                continue
            if _released_in_finally(func.node, text):
                continue
            released_anywhere = any(
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "release"
                and _text(n.func.value) == text
                for n in ast.walk(func.node)
            )
            if not released_anywhere:
                confidence = (
                    "low"  # released elsewhere: a hand-off, the agent must follow it
                )
            else:
                confidence = "high"
            out.append(
                _finding(
                    "acquire-without-finally-release",
                    "FIX",
                    confidence,
                    func,
                    node,
                    f"{text}.acquire() is not paired with a release in a `finally`: an "
                    f"exception before the release leaves it held forever",
                    "use `with lock:`, or `acquire()` immediately followed by try/finally: release()",
                )
            )
    return out


def _parents(root: ast.AST) -> dict[int, ast.AST]:
    """Map id(child) -> parent for every node under *root*."""
    out: dict[int, ast.AST] = {}
    for node in ast.walk(root):
        for child in ast.iter_child_nodes(node):
            out[id(child)] = node
    return out


def _is_true(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _released_in_finally(root: ast.AST, text: str) -> bool:
    for node in ast.walk(root):
        if isinstance(node, ast.Try) and node.finalbody:
            for sub in node.finalbody:
                for n in ast.walk(sub):
                    if (
                        isinstance(n, ast.Call)
                        and isinstance(n.func, ast.Attribute)
                        and n.func.attr == "release"
                        and _text(n.func.value) == text
                    ):
                        return True
    return False


def _check_lock_order_and_reentry(p: Project, s: Summaries) -> list[dict]:
    """lock-order-inversion and non-reentrant-lock-reacquired."""
    out: list[dict] = []
    # edge (a, b) -> (func, node, chain, strong)
    edges: dict[tuple[str, str], tuple[Func, ast.AST, list[Step], bool]] = {}
    for func in p.funcs.values():
        entry = s.entry_held.get(func.key, frozenset())
        # Lexical acquisitions.
        for lock, held, node in func.acquires:
            if lock.anonymous:
                continue
            for h in held:
                if h.anonymous:
                    continue
                if h.key == lock.key:
                    out.extend(_reentry(func, node, h, lock, [], True))
                    continue
                edges.setdefault(
                    (h.key, lock.key),
                    (
                        func,
                        node,
                        [
                            Step(
                                func.qualname,
                                func.rel,
                                node.lineno,
                                f"acquires {lock.text} holding {h.text}",
                            )
                        ],
                        True,
                    ),
                )
            for key in entry:
                if key != lock.key:
                    edges.setdefault(
                        (key, lock.key),
                        (
                            func,
                            node,
                            [
                                Step(
                                    func.qualname,
                                    func.rel,
                                    node.lineno,
                                    f"acquires {lock.text} (callers hold {key.split(':', 1)[-1]})",
                                )
                            ],
                            True,
                        ),
                    )
        # Acquisitions through calls.
        for call in func.calls:
            if not call.held:
                continue
            for callee, strong in call.callees:
                if callee not in s.acquires or p.funcs[callee].is_async:
                    continue
                for lock_key, (lock, chain, cstrong, same) in s.acquires[
                    callee
                ].items():
                    step = _call_step(func, call, strong)
                    for h in call.held:
                        if h.anonymous:
                            continue
                        if h.key == lock_key:
                            if call.self_call and same:
                                out.extend(
                                    _reentry(
                                        func,
                                        call.node,
                                        h,
                                        lock,
                                        [step, *chain],
                                        strong and cstrong,
                                    )
                                )
                            continue
                        edges.setdefault(
                            (h.key, lock_key),
                            (func, call.node, [step, *chain], strong and cstrong),
                        )
    # Cycles.
    graph: dict[str, set[str]] = {}
    for a, b in edges:
        graph.setdefault(a, set()).add(b)
    reported: set[frozenset[str]] = set()
    for a, b in sorted(edges):
        path = _path(graph, b, a)
        if path is None:
            continue
        cycle = [a, *path[:-1]]  # path runs b .. a; drop the repeated a
        ident = frozenset(cycle)
        if ident in reported:
            continue
        reported.add(ident)
        witnesses: list[Step] = []
        strong = True
        for x, y in zip(cycle, cycle[1:] + [cycle[0]]):
            if (x, y) not in edges:
                continue
            _f, _n, chain, st = edges[(x, y)]
            witnesses.extend(chain)
            strong = strong and st
        func, node, _chain, _st = edges[(a, b)]
        names = " -> ".join(k.split(":", 1)[-1] for k in [*cycle, cycle[0]])
        out.append(
            _finding(
                "lock-order-inversion",
                "FIX",
                "medium" if strong else "low",
                func,
                node,
                f"lock-order cycle {names}: two threads taking these in opposite orders deadlock",
                "pick one global order and acquire in it everywhere (or merge the locks); "
                "each hop of the cycle is in `chain`",
                witnesses,
            )
        )
    return out


def _reentry(
    func: Func, node: ast.AST, held: Held, lock: Held, chain: list[Step], strong: bool
) -> list[dict]:
    upgrade = held.mode == "read" and lock.mode == "write"
    if held.kind in ("RLock", "Condition", "async") or (
        held.kind == "custom-reentrant" and not upgrade
    ):
        return []
    if held.instance and lock.instance and held.instance != lock.instance:
        return []  # two members of a lock family (different keys): an ordering question
    if upgrade:
        confidence = "high" if strong else "medium"
        msg = (
            f"{func.qualname}() holds {held.text} (read) and then needs {lock.text} (write): "
            f"a read-to-write upgrade waits for all readers, including itself"
        )
    elif held.kind in _NON_REENTRANT:
        confidence = "high" if strong else "medium"
        msg = f"{func.qualname}() re-acquires the non-reentrant {held.kind} {held.text} it already holds"
    elif held.mode == "read" and lock.mode == "read":
        confidence = "low"
        msg = (
            f"{func.qualname}() re-acquires {held.text} for reading while holding it for reading; "
            f"a writer-preferring lock deadlocks if a writer queues in between"
        )
    else:
        confidence = "low" if held.kind == "heuristic" else "medium"
        msg = f"{func.qualname}() re-acquires {held.text}, which it already holds (reentrant?)"
    return [
        _finding(
            "non-reentrant-lock-reacquired",
            "FIX",
            confidence,
            func,
            node,
            msg,
            "self-deadlock: the thread waits for itself. Split a `_locked` helper that assumes "
            "the lock, or use an RLock if reentry is intended",
            chain or None,
        )
    ]


def _path(graph: dict[str, set[str]], start: str, goal: str) -> list[str] | None:
    """Shortest path start -> goal (BFS), as a node list starting at *start*."""
    if start == goal:
        return [start]
    prev: dict[str, str] = {}
    queue = [start]
    seen = {start}
    while queue:
        cur = queue.pop(0)
        for nxt in sorted(graph.get(cur, ())):
            if nxt in seen:
                continue
            prev[nxt] = cur
            if nxt == goal:
                path = [goal]
                while path[-1] != start:
                    path.append(prev[path[-1]])
                return list(reversed(path))
            seen.add(nxt)
            queue.append(nxt)
    return None


def _check_conditions(p: Project, s: Summaries) -> list[dict]:
    out: list[dict] = []
    for func in p.funcs.values():
        for node, cond, in_loop in func.cond_waits:
            if in_loop:
                continue
            out.append(
                _finding(
                    "condition-wait-outside-predicate-loop",
                    "FIX",
                    "medium",
                    func,
                    node,
                    f"{cond}.wait() is not inside a `while <predicate>` loop: a spurious or "
                    f"stolen wakeup proceeds as if the condition held",
                    "use `while not predicate: cond.wait()` or `cond.wait_for(predicate)`",
                )
            )
        for node, cond, held in func.cond_notifies:
            held_text = {h.text for h in held}
            if cond in held_text or _held_keys(func, held, s) & {
                f"{func.cls}.{cond.split('.', 1)[-1]}",
                f"{func.module}:{cond}",
            }:
                continue
            out.append(
                _finding(
                    "condition-notify-without-lock",
                    "FIX",
                    "medium",
                    func,
                    node,
                    f"{_text(node.func)}() without holding {cond}: raises RuntimeError "
                    f"(threading) at runtime, and a waiter can miss the state change",
                    "notify inside `with cond:`, after changing the state the waiters check",
                )
            )
    return out


# Worst first: a call that reaches both a sleep and a file read is reported as the sleep.
_BLOCKING_ORDER = (
    "sleep",
    "network",
    "subprocess",
    "thread-wait",
    "custom",
    "ipc",
    "file-io",
)
# Categories whose hits under a lock are mostly by design (one writer per file, one
# request per pipe): summarized per lock unless --all-blocking asks for each one.
_LOW_SIGNAL_BLOCKING = frozenset({"file-io", "ipc"})


def _check_blocking_under_lock(
    p: Project,
    s: Summaries,
    low_signal: list[dict] | None = None,
) -> list[dict]:
    """blocking-call-under-lock.

    With *low_signal* given, file-I/O and pipe hits go there (for the per-lock
    summary) instead of becoming findings.
    """
    out: list[dict] = []

    def emit(finding: dict, category: str, lock_key: str, leaf: str) -> None:
        if low_signal is not None and category in _LOW_SIGNAL_BLOCKING:
            low_signal.append(
                {
                    "lock": lock_key.split(":", 1)[-1],
                    "category": category,
                    "site": f"{finding['file']}:{finding['line']}",
                    "leaf": leaf,
                }
            )
        else:
            out.append(finding)

    for func in p.funcs.values():
        entry = s.entry_held.get(func.key, frozenset())
        direct_nodes = {id(node) for _c, _n, node, _h in func.direct_blocking}
        for category, name, node, held in func.direct_blocking:
            thread_held = [h for h in held if not h.is_async]
            if not thread_held and not entry:
                continue
            severity, confidence, label = _CATEGORY[category]
            lock_text = (
                thread_held[0].text
                if thread_held
                else f"{min(entry).split(':', 1)[-1]} (held by every caller)"
            )
            lock_key = thread_held[0].key if thread_held else min(entry)
            emit(
                _finding(
                    "blocking-call-under-lock",
                    severity,
                    confidence,
                    func,
                    node,
                    f"{name}(...) {label} while holding {lock_text}: every thread that needs "
                    f"the lock waits for it",
                    "move the slow work out of the critical section (compute, then lock and "
                    "publish), or document why the lock must cover it",
                ),
                category,
                lock_key,
                f"{func.qualname}: {name}(...)",
            )
        for call in func.calls:
            thread_held = [h for h in call.held if not h.is_async]
            if not thread_held or id(call.node) in direct_nodes:
                continue  # a call that is itself designated slow is reported once
            for callee, strong in call.callees:
                if callee not in s.blocks or p.funcs[callee].is_async:
                    continue
                blocks = s.blocks[callee]
                category = next((c for c in _BLOCKING_ORDER if c in blocks), None)
                if category is None:
                    continue
                chain, cstrong = blocks[category]
                severity, confidence, label = _CATEGORY[category]
                confidence = _lower(
                    confidence
                )  # through a call: one more hop to verify
                if not (strong and cstrong):
                    confidence = _lower(confidence)
                step = _call_step(func, call, strong)
                emit(
                    _finding(
                        "blocking-call-under-lock",
                        severity,
                        confidence,
                        func,
                        call.node,
                        f"{_text(call.node.func)}(...) {label} (via {len(chain)} call"
                        f"{'s' if len(chain) > 1 else ''}) while holding {thread_held[0].text}",
                        "move the slow work out of the critical section, or document why the "
                        "lock must cover it",
                        [step, *chain],
                    ),
                    category,
                    thread_held[0].key,
                    f"{chain[-1].func}: {chain[-1].text}",
                )
    return out


def _summarize_low_signal(low_signal: list[dict]) -> list[dict]:
    """Group summarized blocking hits by lock, then by the leaf they end in."""
    by_lock: dict[str, list[dict]] = {}
    for hit in low_signal:
        by_lock.setdefault(hit["lock"], []).append(hit)
    out: list[dict] = []
    for lock, hits in by_lock.items():
        categories: dict[str, int] = {}
        leaves: dict[str, int] = {}
        for hit in hits:
            categories[hit["category"]] = categories.get(hit["category"], 0) + 1
            leaves[hit["leaf"]] = leaves.get(hit["leaf"], 0) + 1
        out.append(
            {
                "lock": lock,
                "sites": len(hits),
                "by_category": dict(sorted(categories.items())),
                "leaves": dict(sorted(leaves.items(), key=lambda kv: (-kv[1], kv[0]))),
                "examples": sorted({h["site"] for h in hits})[:5],
            }
        )
    out.sort(key=lambda g: (-g["sites"], g["lock"]))
    return out


def _check_await_under_lock(p: Project) -> list[dict]:
    out: list[dict] = []
    for func in p.funcs.values():
        for node, held in func.awaits_under_lock:
            lock = next(h for h in held if not h.is_async)
            out.append(
                _finding(
                    "await-while-holding-thread-lock",
                    "FIX",
                    "high" if lock.kind != "heuristic" else "medium",
                    func,
                    node,
                    f"`await` while holding the thread lock {lock.text}: the coroutine suspends "
                    f"with the lock held, and another task on the same loop that takes it "
                    f"blocks the loop thread forever",
                    "release before awaiting, or use an asyncio.Lock with `async with`",
                )
            )
    return out


def _check_transitive_blocking_in_async(p: Project, s: Summaries) -> list[dict]:
    out: list[dict] = []
    offloading_modules = {
        f.module for f in p.funcs.values() if f.is_async and f.offloads
    }
    for func in p.funcs.values():
        if not func.is_async:
            continue
        twin = func.module in offloading_modules
        # Direct: a sync `with lock:` in the coroutine itself.
        for lock, held, node in func.acquires:
            if lock.is_async:
                continue
            slow = lock.key in s.slow_locks
            confidence = "high" if slow and twin else "medium" if slow else "low"
            out.append(
                _finding(
                    "transitive-blocking-call-in-async",
                    "FIX",
                    confidence,
                    func,
                    node,
                    f"async def {func.name}() takes the thread lock {lock.text} on the event loop: "
                    f"while another thread holds it, every coroutine stops",
                    _slow_detail(lock.key, s),
                    s.slow_locks.get(lock.key),
                )
            )
        for call in func.calls:
            if call.awaited:
                continue
            best: tuple[int, dict] | None = None
            for callee, strong in call.callees:
                target = p.funcs.get(callee)
                if target is None or target.is_async or _is_contextmanager(target.node):
                    continue
                verdict = _async_call_verdict(s, callee, strong, twin)
                if verdict is None:
                    continue
                confidence, label, chain = verdict
                step = _call_step(func, call, strong)
                detail = (
                    "a coroutine in this module already offloads sync work "
                    "(run_in_executor/to_thread): this call is its unguarded twin. "
                    if twin
                    else ""
                ) + "await asyncio.to_thread(...) / loop.run_in_executor(...) instead"
                finding = _finding(
                    "transitive-blocking-call-in-async",
                    "FIX",
                    confidence,
                    func,
                    call.node,
                    f"async def {func.name}() calls sync {target.qualname}(), which {label}, "
                    f"on the event loop",
                    detail,
                    [step, *chain],
                )
                rank = _RANK[confidence]
                if best is None or rank > best[0]:
                    best = (rank, finding)
            if best is not None:
                out.append(best[1])
    return out


_RANK = {"low": 0, "medium": 1, "high": 2}
_ASYNC_ORDER = (
    "sleep",
    "network",
    "subprocess",
    "thread-wait",
    "ipc",
    "custom",
    "lock",
    "file-io",
)


def _async_call_verdict(
    s: Summaries, callee: str, strong: bool, twin: bool
) -> tuple[str, str, list[Step]] | None:
    """(confidence, what it does, chain) for a sync call made on the event loop."""
    blocks = s.blocks.get(callee, {})
    ranked = [c for c in _ASYNC_ORDER if c in blocks]
    if not ranked:
        return None
    category = ranked[0]
    chain, cstrong = blocks[category]
    if category == "lock":
        # Prefer the chain to a lock that someone holds across blocking work, and
        # show that witness: the lock is only as slow as the work done under it.
        slow = [
            (k, v) for k, v in s.acquires.get(callee, {}).items() if k in s.slow_locks
        ]
        if slow:
            slow.sort(
                key=lambda kv: (not (kv[1][2] and s.slow_strong[kv[0]]), len(kv[1][1]))
            )
            key, (lock, chain, cstrong, _same) = slow[0]
            cstrong = cstrong and s.slow_strong[key]
            chain = [*chain, *_meanwhile(lock.text, s.slow_locks[key])]
            confidence = "high" if twin else "medium"
            label = "waits for a thread lock that other code holds across blocking work"
        else:
            confidence, label = "low", "takes a thread lock"
    elif category in ("file-io", "ipc"):
        confidence, label = "low", _CATEGORY[category][2]
    else:
        confidence = "high" if twin else "medium"
        label = _CATEGORY[category][2]
    if not (strong and cstrong):
        confidence = _lower(confidence)
    return confidence, label, chain


def _slow_detail(lock_key: str, s: Summaries) -> str:
    if lock_key in s.slow_locks:
        return "other code holds this lock across blocking work (see `chain`); offload the call"
    return "offload with asyncio.to_thread, or use an asyncio.Lock"


def _check_signal_handlers(p: Project, s: Summaries) -> list[dict]:
    out: list[dict] = []
    for mod in p.modules.values():
        for node, handler, key, owner in mod.signal_handlers:
            if key is None or key not in p.funcs:
                continue
            target = p.funcs[key]
            blocks = s.blocks.get(key, {})
            reasons = [
                c
                for c in (
                    "lock",
                    "logging",
                    "file-io",
                    "sleep",
                    "thread-wait",
                    "network",
                    "subprocess",
                )
                if c in blocks
            ]
            if not reasons:
                continue
            chain, strong = blocks[reasons[0]]
            func = p.funcs[owner]
            out.append(
                _finding(
                    "signal-handler-does-real-work",
                    "CONSIDER" if reasons[0] == "logging" else "FIX",
                    "medium" if strong else "low",
                    func,
                    node,
                    f"signal handler {target.qualname}() {'logs' if reasons[0] == 'logging' else 'takes a lock' if reasons[0] == 'lock' else 'blocks'}: "
                    f"it runs between two bytecodes of the main thread, which may already hold "
                    f"that lock (including logging's)",
                    "set a flag or write to a self-pipe in the handler; do the work in the main loop",
                    chain,
                )
            )
    return out


# Calls that cannot plausibly raise or suspend between an acquire and its try.
_SAFE_CALLS = frozenset(
    {
        "len",
        "isinstance",
        "issubclass",
        "any",
        "all",
        "repr",
        "id",
        "type",
        "bool",
        "str",
        "getattr",
        "hasattr",
        "min",
        "max",
        "sorted",
        "list",
        "dict",
        "tuple",
        "set",
        "frozenset",
        "range",
        "enumerate",
        "zip",
        "time.monotonic",
        "time.time",
        "time.perf_counter",
    }
)
_SAFE_METHODS = frozenset(
    {
        "get",
        "items",
        "keys",
        "values",
        "copy",
        "format",
        "join",
        "split",
        "strip",
        "lower",
        "upper",
        "startswith",
        "endswith",
        "append",
        "add",
        "setdefault",
    }
)


def _releases(node: ast.AST, res: str) -> bool:
    """Does *node* (not a nested def) call `<res>.release(...)`?"""
    for sub in [node, *_walk_scope(node)]:
        if (
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Attribute)
            and sub.func.attr == "release"
            and _text(sub.func.value) == res
        ):
            return True
    return False


def _risky_expr(p: Project, func: Func, node: ast.AST) -> ast.AST | None:
    """The first `await` or call in *node* that can raise or suspend."""
    mod = p.modules[func.module]
    for sub in [node, *_walk_scope(node)]:
        if isinstance(sub, ast.Await):
            return sub
        if not isinstance(sub, ast.Call):
            continue
        name = p.qualify(mod, sub.func)
        method = ""
        receiver = ""
        if isinstance(sub.func, ast.Attribute):
            method = sub.func.attr
            receiver = (_dotted(sub.func.value) or "").split(".")[-1]
        if name in _SAFE_CALLS or method in _SAFE_METHODS:
            continue
        if re.search(r"(Error|Exception)$", name or method):
            continue  # building the exception a guard raises
        if method in _LOG_METHODS and _LOG_RECEIVERS.match(receiver):
            continue
        return sub
    return None


def _risky_in(
    p: Project, func: Func, stmts: list[ast.stmt], res: str
) -> ast.AST | None:
    """The first statement-level hazard in *stmts* before *res* is released."""
    for st in stmts:
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(st, ast.If):
            found = _risky_expr(p, func, st.test)
            # `if slot is None: raise Busy()`: a branch on the resource itself is
            # the not-taken path (or its own release), not a hazard.
            if found is None and res not in {
                n.id for n in ast.walk(st.test) if isinstance(n, ast.Name)
            }:
                found = _risky_in(p, func, st.body, res) or _risky_in(
                    p, func, st.orelse, res
                )
        elif isinstance(st, (ast.With, ast.AsyncWith)):
            found = next(
                (
                    n
                    for item in st.items
                    if (n := _risky_expr(p, func, item.context_expr))
                ),
                None,
            ) or _risky_in(p, func, st.body, res)
        elif isinstance(st, (ast.For, ast.AsyncFor, ast.While)):
            header = st.test if isinstance(st, ast.While) else st.iter
            found = _risky_expr(p, func, header) or _risky_in(p, func, st.body, res)
        elif isinstance(st, ast.Try):
            if _releases_in(st.finalbody, res):
                return None
            found = _risky_in(p, func, st.body, res)
        else:
            # An explicit exit is a path of its own (often the not-acquired one);
            # the hazard this looks for is an exception nobody planned for.
            if _releases(st, res) or isinstance(st, (ast.Return, ast.Raise)):
                return None
            found = _risky_expr(p, func, st)
        if found is not None:
            return found
    return None


def _releases_in(stmts: list[ast.stmt], res: str) -> bool:
    return any(_releases(st, res) for st in stmts)


def _catches_cancellation(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    types = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any(
        _dotted(t).split(".")[-1] in ("BaseException", "CancelledError") for t in types
    )


def _acquired_resources(st: ast.stmt, released: set[str]) -> list[str]:
    """`x = take_slot()`, `x, err = gate()`, `sem.acquire()`: what *st* takes,
    when the function releases it somewhere (`x.release()`)."""
    if (
        isinstance(st, ast.Expr)
        and isinstance(st.value, ast.Call)
        and isinstance(st.value.func, ast.Attribute)
        and st.value.func.attr == "acquire"
        and not st.value.args
        and not st.value.keywords
    ):
        res = _text(st.value.func.value)
        return [res] if res in released else []
    value = getattr(st, "value", None)
    if isinstance(value, ast.Await):
        value = value.value
    if not isinstance(st, (ast.Assign, ast.AnnAssign)) or not isinstance(
        value, ast.Call
    ):
        return []
    targets = st.targets if isinstance(st, ast.Assign) else [st.target]
    names: list[str] = []
    for tgt in targets:
        elts = tgt.elts if isinstance(tgt, (ast.Tuple, ast.List)) else [tgt]
        names.extend(e.id for e in elts if isinstance(e, ast.Name))
    return [n for n in names if n in released]


def _stmt_lists(fn: ast.FunctionDef | ast.AsyncFunctionDef):
    """Every statement list in *fn*'s own scope."""
    stack: list[list[ast.stmt]] = [fn.body]
    while stack:
        stmts = stack.pop()
        yield stmts
        for st in stmts:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            for name in ("body", "orelse", "finalbody"):
                sub = getattr(st, name, None)
                if isinstance(sub, list) and sub and isinstance(sub[0], ast.stmt):
                    stack.append(sub)
            for handler in getattr(st, "handlers", []):
                stack.append(handler.body)
            for case in getattr(st, "cases", []):
                stack.append(case.body)


def _check_slot_release(p: Project) -> list[dict]:
    """slot-acquired-before-the-try-that-releases-it."""
    out: list[dict] = []
    for func in p.funcs.values():
        released = {
            _text(n.func.value)
            for n in ast.walk(func.node)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "release"
        }
        if not released:
            continue
        for stmts in _stmt_lists(func.node):
            for i, st in enumerate(stmts):
                for res in _acquired_resources(st, released):
                    finding = _slot_verdict(p, func, st, stmts[i + 1 :], res)
                    if finding is not None:
                        out.append(finding)
    return out


def _slot_verdict(
    p: Project, func: Func, acq: ast.stmt, rest: list[ast.stmt], res: str
) -> dict | None:
    shape = "slot-acquired-before-the-try-that-releases-it"
    risk: ast.AST | None = None
    for st in rest:
        if isinstance(st, ast.Try):
            if _releases_in(st.finalbody, res):
                if risk is None:
                    return None
                return _finding(
                    shape,
                    "FIX",
                    "high" if isinstance(risk, ast.Await) else "medium",
                    func,
                    acq,
                    f"{res} is taken at line {acq.lineno}, but the try whose `finally` "
                    f"releases it starts at line {st.lineno}; line {risk.lineno} can raise "
                    f"or be cancelled first, and then nothing releases it",
                    "open the try immediately after the acquire (or move the acquire to just "
                    "before it); every exit between them leaks the slot until restart",
                )
            handlers = [h for h in st.handlers if _releases_in(h.body, res)]
            if handlers:
                awaited = _first_await(st.body)
                if (
                    func.is_async
                    and awaited is not None
                    and not any(_catches_cancellation(h) for h in st.handlers)
                ):
                    return _finding(
                        shape,
                        "FIX",
                        "medium",
                        func,
                        acq,
                        f"{res} is released only in `except` handlers that do not catch "
                        f"CancelledError (a BaseException): cancelling the `await` at line "
                        f"{awaited.lineno} skips them and leaks it",
                        "release in `finally` (guarded by a flag set once ownership is handed "
                        "off), or add `except BaseException: release(); raise`",
                    )
                return None
        if not isinstance(st, ast.Try) and _releases(st, res) and _is_simple(st):
            break  # released on the straight-line path
        if isinstance(st, ast.Return) and res in {
            n.id for n in ast.walk(st) if isinstance(n, ast.Name)
        }:
            break  # handed off to the caller
        if risk is None:
            risk = _risky_in(p, func, [st], res)
        if isinstance(st, (ast.Return, ast.Raise)):
            break
    if risk is None:
        return None
    return _finding(
        shape,
        "FIX",
        "high" if isinstance(risk, ast.Await) else "medium",
        func,
        acq,
        f"{res} is taken at line {acq.lineno} and released (or handed off) only on the "
        f"straight-line path: line {risk.lineno} can raise or be cancelled first, and no "
        f"try/finally covers it",
        "wrap everything after the acquire in try/finally (release unless ownership was "
        "handed off), or use a context manager",
    )


def _is_simple(st: ast.stmt) -> bool:
    return isinstance(st, (ast.Expr, ast.Assign, ast.AnnAssign, ast.AugAssign))


def _first_await(stmts: list[ast.stmt]) -> ast.AST | None:
    for st in stmts:
        for sub in [st, *_walk_scope(st)]:
            if isinstance(sub, ast.Await):
                return sub
    return None


_CONTAINER_FACTORIES = frozenset(
    {
        "dict",
        "list",
        "set",
        "collections.defaultdict",
        "collections.OrderedDict",
        "collections.deque",
        "collections.Counter",
        "weakref.WeakValueDictionary",
        "weakref.WeakKeyDictionary",
        "weakref.WeakSet",
    }
)


def _module_state(p: Project) -> list[dict]:
    """Process-global state: the starting point for a re-entrancy review.

    Module-level instances of project classes (singletons), module-level
    containers that some function mutates, and globals a function rebinds
    (`global _client; _client = Client()`). Listed whether or not any lock is
    involved: in a project with no threads, a second in-process call (a test
    runner, an embedding application, a server) is the concurrency.
    """
    out: list[dict] = []
    for mod in p.modules.values():
        defined: dict[str, tuple[str, str, int]] = {}  # name -> (kind, class, line)
        for stmt in mod.tree.body:
            if isinstance(stmt, ast.Assign):
                targets, value = stmt.targets, stmt.value
            elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
                targets, value = [stmt.target], stmt.value
            else:
                continue
            for tgt in targets:
                if not isinstance(tgt, ast.Name) or tgt.id in mod.lock_globals:
                    continue
                kind, cls = "", ""
                if isinstance(value, ast.Call):
                    cls_key = p.resolve_class(mod, value.func)
                    if cls_key and not p.classes[cls_key].is_lock_class:
                        kind, cls = "instance", cls_key.split(":", 1)[-1]
                    elif p.qualify(mod, value.func) in _CONTAINER_FACTORIES:
                        kind = "container"
                elif isinstance(
                    value,
                    (
                        ast.Dict,
                        ast.List,
                        ast.Set,
                        ast.DictComp,
                        ast.ListComp,
                        ast.SetComp,
                    ),
                ):
                    kind = "container"
                else:
                    kind = "value"
                defined[tgt.id] = (kind, cls, stmt.lineno)
        if not defined:
            continue
        importers = {mod.name: {n: n for n in defined}}
        for other in p.modules.values():
            if other is mod:
                continue
            names = {
                alias: target.rsplit(".", 1)[-1]
                for alias, target in other.imports.items()
                if target.rsplit(".", 1)[0] == mod.name
                and target.rsplit(".", 1)[-1] in defined
            }
            if names:
                importers[other.name] = names
        mutated: dict[str, set[str]] = {}
        rebound: dict[str, set[str]] = {}
        used: dict[str, set[str]] = {}
        for func in p.funcs.values():
            names = importers.get(func.module)
            if not names:
                continue
            declared = {
                n
                for node in _walk_scope(func.node)
                if isinstance(node, ast.Global)
                for n in node.names
            }
            local = {
                node.id
                for node in _walk_scope(func.node)
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
            } | {a.arg for a in ast.walk(func.node.args) if isinstance(a, ast.arg)}
            for node in _walk_scope(func.node):
                if not isinstance(node, ast.Name) or node.id not in names:
                    continue
                name = names[node.id]
                if isinstance(node.ctx, ast.Store):
                    if node.id in declared:
                        rebound.setdefault(name, set()).add(func.qualname)
                    continue
                if node.id in local and node.id not in declared:
                    continue
                used.setdefault(name, set()).add(func.qualname)
            for node in _walk_scope(func.node):
                base = None
                if isinstance(node, (ast.Subscript,)) and isinstance(
                    node.ctx, (ast.Store, ast.Del)
                ):
                    base = node.value
                elif (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in _MUTATING_METHODS
                ):
                    base = node.func.value
                if (
                    isinstance(base, ast.Name)
                    and base.id in names
                    and (base.id not in local or base.id in declared)
                ):
                    mutated.setdefault(names[base.id], set()).add(func.qualname)
        for name, (kind, cls, line) in defined.items():
            entry = None
            if name in rebound:
                entry = {"kind": "rebound", "by": sorted(rebound[name])}
            elif kind == "instance":
                entry = {
                    "kind": "instance",
                    "class": cls,
                    "by": sorted(used.get(name, ())),
                }
            elif kind == "container" and name in mutated:
                entry = {"kind": "container", "by": sorted(mutated[name])}
            if entry is None:
                continue
            by = entry.pop("by")
            out.append(
                {
                    "name": f"{mod.name}:{name}",
                    "file": mod.rel,
                    "line": line,
                    **entry,
                    "functions": by[:10],
                    "function_count": len(by),
                }
            )
    out.sort(key=lambda e: (e["file"], e["line"]))
    return out


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

_CHECKS = (
    "unguarded-access-to-lock-guarded-state",
    "check-then-act-without-lock",
    "acquire-without-finally-release",
    "lock-acquire-result-ignored",
    "lock-order-inversion",
    "non-reentrant-lock-reacquired",
    "condition-wait-outside-predicate-loop",
    "condition-notify-without-lock",
    "blocking-call-under-lock",
    "await-while-holding-thread-lock",
    "transitive-blocking-call-in-async",
    "signal-handler-does-real-work",
    "slot-acquired-before-the-try-that-releases-it",
)


def analyze(
    target: str,
    *,
    max_files: int = 0,
    checks: list[str] | None = None,
    exclude: list[str] | None = None,
    blocking: list[str] | None = None,
    all_blocking: bool = False,
) -> dict:
    """Scan *target* (a directory or a file) for concurrency defects.

    File-I/O and pipe waits under a lock are summarized per lock in
    ``summary["blocking_low_signal"]`` unless *all_blocking* reports each one.
    """
    resolved = Path(target).resolve()
    project_root = find_project_root(resolved)
    scan_root = resolved
    files, files_total = collect_python_files(scan_root, max_files)
    corpus: list[tuple[str, ast.Module]] = []
    for path in files:
        rel = relative_to_root(path, project_root)
        if exclude and any(pattern in rel for pattern in exclude):
            continue
        tree = parse_source(path)
        if tree is not None:
            corpus.append((rel, tree))

    project = Project(corpus, set(blocking or ()))
    summaries = Summaries(project)
    selected = set(checks or _CHECKS)

    findings: list[dict] = []
    low_signal: list[dict] = []
    guard_findings, guards = _check_guards(project, summaries)
    if "unguarded-access-to-lock-guarded-state" in selected:
        findings.extend(guard_findings)
    runs = {
        "check-then-act-without-lock": lambda: _check_check_then_act(
            project, summaries, _written_under_lock(project, summaries)
        ),
        "acquire-without-finally-release": lambda: _check_bare_acquire(project),
        "lock-order-inversion": lambda: _check_lock_order_and_reentry(
            project, summaries
        ),
        "condition-wait-outside-predicate-loop": lambda: _check_conditions(
            project, summaries
        ),
        "blocking-call-under-lock": lambda: _check_blocking_under_lock(
            project, summaries, None if all_blocking else low_signal
        ),
        "await-while-holding-thread-lock": lambda: _check_await_under_lock(project),
        "transitive-blocking-call-in-async": lambda: (
            _check_transitive_blocking_in_async(project, summaries)
        ),
        "signal-handler-does-real-work": lambda: _check_signal_handlers(
            project, summaries
        ),
        "slot-acquired-before-the-try-that-releases-it": lambda: _check_slot_release(
            project
        ),
    }
    # Some checks produce two shapes; run each producer once if either is selected.
    produces = {
        "acquire-without-finally-release": {
            "acquire-without-finally-release",
            "lock-acquire-result-ignored",
        },
        "lock-order-inversion": {
            "lock-order-inversion",
            "non-reentrant-lock-reacquired",
        },
        "condition-wait-outside-predicate-loop": {
            "condition-wait-outside-predicate-loop",
            "condition-notify-without-lock",
        },
    }
    for name, run in runs.items():
        shapes = produces.get(name, {name})
        if shapes & selected:
            findings.extend(f for f in run() if f["shape"] in selected)

    findings = _dedupe(findings)
    findings.sort(key=lambda f: (f["file"], f["line"], f["shape"]))

    by_shape: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    by_confidence: dict[str, int] = {}
    for f in findings:
        by_shape[f["shape"]] = by_shape.get(f["shape"], 0) + 1
        by_severity[f["severity"]] = by_severity.get(f["severity"], 0) + 1
        by_confidence[f["confidence"]] = by_confidence.get(f["confidence"], 0) + 1

    lock_kinds: dict[str, int] = {}
    for cls in project.classes.values():
        for kind in cls.lock_attrs.values():
            lock_kinds[kind] = lock_kinds.get(kind, 0) + 1
    for mod in project.modules.values():
        for kind in mod.lock_globals.values():
            lock_kinds[kind] = lock_kinds.get(kind, 0) + 1

    module_state = _module_state(project)
    result = build_envelope(project_root, scan_root, files_total, len(files))
    result["summary"] = {
        "total_findings": len(findings),
        "by_shape": dict(sorted(by_shape.items())),
        "by_severity": dict(sorted(by_severity.items())),
        "by_confidence": dict(sorted(by_confidence.items())),
        "checks_run": sorted(selected & set(_CHECKS)),
        "excluded_patterns": sorted(exclude or []),
        "functions_indexed": len(project.funcs),
        "locks_declared": dict(sorted(lock_kinds.items())),
        "lock_context_managers": sorted(
            project.funcs[k].qualname for k, v in project.lock_cms.items() if v
        ),
        # The reentrancy each lock CM inherits from the locks it holds at `yield`
        # (`custom` = unknown), and the project lock CM a nested one wraps.
        "lock_context_manager_kinds": {
            project.funcs[k].qualname: project.lock_cm_kind.get(k, "custom")
            for k, v in sorted(project.lock_cms.items())
            if v == ("own", None)
        },
        "lock_context_managers_wrapping": {
            project.funcs[k].qualname: via
            for k, via in sorted(project.lock_cm_via.items())
        },
        # Context managers with a `with` around `yield` that did not resolve to
        # a lock: check each by hand, a missed lock CM hides every site using it.
        "possible_lock_cms_missed": sorted(
            f"{project.funcs[k].qualname}: with {', '.join(_text(e) for e, _ in items)}"
            for k, v in project.lock_cms.items()
            if v is None and (items := _yield_with_items(project.funcs[k].node))
        ),
        "guards_inferred": len(guards),
        "module_state_entries": len(module_state),
        "slow_locks": sorted(k.split(":", 1)[-1] for k in summaries.slow_locks),
        # File I/O and pipe waits under a lock: mostly by design, so summarized
        # per lock instead of one finding each (--all-blocking lists them).
        "blocking_low_signal": {
            "reported_as": "findings" if all_blocking else "summary",
            "sites": None if all_blocking else len(low_signal),
            "by_lock": [] if all_blocking else _summarize_low_signal(low_signal),
        },
    }
    result["guards"] = guards
    result["module_state"] = module_state
    result["findings"] = findings
    return result


def _dedupe(findings: list[dict]) -> list[dict]:
    seen: set[tuple] = set()
    out: list[dict] = []
    for f in findings:
        key = (f["file"], f["line"], f["shape"], f["message"])
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


def _extract_options(argv: list[str]) -> tuple[list[str], dict[str, list[str] | None]]:
    rest: list[str] = []
    opts: dict[str, list[str] | None] = {
        "check": None,
        "exclude": None,
        "blocking": None,
    }
    i = 0
    while i < len(argv):
        flag = argv[i][2:] if argv[i].startswith("--") else ""
        if flag in opts and i + 1 < len(argv):
            opts[flag] = [x.strip() for x in argv[i + 1].split(",") if x.strip()]
            i += 2
        else:
            rest.append(argv[i])
            i += 1
    return rest, opts


def main() -> None:
    argv = sys.argv[1:]
    all_blocking = "--all-blocking" in argv
    argv = [a for a in argv if a != "--all-blocking"]
    argv, opts = _extract_options(argv)
    target, max_files = parse_common_args(argv)
    checks = opts["check"]
    if checks:
        unknown = [c for c in checks if c not in _CHECKS]
        if unknown:
            emit(
                {
                    "error": f"unknown check(s): {', '.join(unknown)}",
                    "available": list(_CHECKS),
                }
            )
            sys.exit(2)
    emit(
        analyze(
            target,
            max_files=max_files,
            checks=checks,
            exclude=opts["exclude"],
            blocking=opts["blocking"],
            all_blocking=all_blocking,
        )
    )


if __name__ == "__main__":
    main()
