#!/usr/bin/env python3
"""Inventory every error-handling site in a Python project.

The silent-failure-hunter is told to look at *every* error-handling site. This
script lists them so the coverage is checkable and cheap to re-run, and so the
agent spends its time judging sites rather than finding them. It does not decide
whether a site is a bug.

Explicit sites (counted in ``summary.explicit_sites``):

- each ``except`` clause of a ``try`` / ``try*``, with what it catches and what
  its body does: re-raise, raise a new exception (chained or not), exit,
  return, continue, log (with or without a traceback), or fall through;
- each ``with suppress(...)`` block, with its size (a wide
  ``suppress(Exception)`` around many statements is invisible to linters);
- each ``__exit__`` / ``__aexit__`` that can return a true value, which
  suppresses the exception.

Also listed, but not counted as sites:

- ``finally`` blocks: the calls they make outside a nested ``try`` (a raise
  there replaces the result or the exception in flight) and any
  ``return``/``break``/``continue`` (which discards the exception);
- implicit sites, counted separately by kind in ``summary.implicit_by_kind``:
  ``getattr(obj, name, default)``, ``hasattr(...)``, ``.get(key, <empty>)``
  with an empty-container default, and ``subprocess.run``/``call`` without
  ``check=``.

Per-handler leads for the "what if the handler itself fails" pass:

- ``maybe_unbound``: names the handler reads that are only assigned inside the
  ``try`` body (``UnboundLocalError`` inside the handler masks the real error);
- ``cleanup-last-in-try`` flag: the ``try`` body ends with a cleanup call
  (``rmtree``, ``unlink``, ``remove``...) and the handler does more than
  re-raise, so a cleanup failure runs the failure path after the work
  succeeded.

Usage:
    python survey_error_handling.py [path] [--max-files N] [--no-implicit]

Options:
    --max-files N   Analyze at most N files (``files_capped`` says so).
    --no-implicit   Skip the implicit-site inventory.
"""

from __future__ import annotations

import ast
import re
import sys
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scan_common import (
    build_envelope,
    collect_python_files,
    emit,
    parse_common_args,
    parse_source,
    relative_to_root,
    resolve_target,
)

BROAD_NAMES = frozenset({"Exception", "BaseException"})
LOG_METHODS = frozenset(
    {
        "debug",
        "info",
        "warning",
        "warn",
        "error",
        "exception",
        "critical",
        "fatal",
        "log",
    }
)
TRACEBACK_FUNCS = frozenset(
    {"print_exc", "format_exc", "print_exception", "format_exception"}
)
EXIT_FUNCS = frozenset({"sys.exit", "exit", "quit", "os._exit"})
TEXT_ATTRS = frozenset({"args", "message", "msg", "strerror", "detail"})
EXPOSING_KEYWORDS = frozenset({"detail", "message", "error", "content", "reason"})
CLEANUP_CALL = re.compile(
    r"^(rmtree|unlink|remove|removedirs|rmdir"
    r"|_?(cleanup|clean_up|remove|delete|purge|discard)\w*)$"
)
EMPTY_CALLS = frozenset({"dict", "list", "set", "tuple", "str", "frozenset"})
SUBPROCESS_UNCHECKED = frozenset({"subprocess.run", "subprocess.call"})
BODY_PREVIEW_STATEMENTS = 3
BODY_PREVIEW_WIDTH = 80
MAX_FINALLY_CALLS = 10

# The order in which a handler's actions decide its single `outcome`: the
# strongest thing the body does anywhere. `actions` lists all of them.
_OUTCOME_PRIORITY = (
    "raise-unchained",
    "raise-from-none",
    "raise-chained",
    "reraise",
    "exit",
    "return",
    "continue",
    "break",
)
_PROPAGATING = frozenset(
    {"raise-unchained", "raise-from-none", "raise-chained", "reraise", "exit"}
)
_REPORTING = frozenset({"log", "print", "warn", "traceback"})
_NESTED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
_LOOPS = (ast.For, ast.AsyncFor, ast.While)


def _walk_local(nodes: list[ast.AST]) -> Iterator[ast.AST]:
    """Walk *nodes* without descending into nested functions, lambdas or classes.

    Code inside a nested def runs later (or never), so a ``raise`` there is not
    something the handler does. The def node itself is yielded: its name is a
    binding in the enclosing scope.
    """
    stack = list(reversed(nodes))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, _NESTED_SCOPES):
            continue
        stack.extend(reversed(list(ast.iter_child_nodes(node))))


def _dotted(node: ast.AST) -> str:
    """``a.b.c`` for a Name/Attribute chain; the unparsed text otherwise."""
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
        return ".".join(reversed(parts))
    return ast.unparse(node)


def _last_name(node: ast.AST) -> str:
    return _dotted(node).rsplit(".", 1)[-1]


def _caught_names(type_node: ast.AST | None) -> list[str]:
    if type_node is None:
        return []
    elts = type_node.elts if isinstance(type_node, ast.Tuple) else [type_node]
    return [_dotted(e) for e in elts]


def _is_broad(names: list[str]) -> bool:
    return not names or any(n.rsplit(".", 1)[-1] in BROAD_NAMES for n in names)


def _preview(body: list[ast.stmt]) -> str:
    parts = []
    for stmt in body[:BODY_PREVIEW_STATEMENTS]:
        first = ast.unparse(stmt).split("\n", 1)[0]
        parts.append(first[:BODY_PREVIEW_WIDTH])
    if len(body) > BODY_PREVIEW_STATEMENTS:
        parts.append("...")
    return "; ".join(parts)


def _is_log_call(call: ast.Call) -> bool:
    func = call.func
    if not isinstance(func, ast.Attribute) or func.attr not in LOG_METHODS:
        return False
    receiver = _dotted(func.value).lower()
    return "log" in receiver


def _log_level(call: ast.Call) -> str:
    method = call.func.attr  # type: ignore[union-attr]
    if method == "log" and call.args:
        return _last_name(call.args[0]).lower()
    return {"warn": "warning", "fatal": "critical"}.get(method, method)


def _log_has_traceback(call: ast.Call) -> bool:
    if call.func.attr == "exception":  # type: ignore[union-attr]
        return not any(
            kw.arg == "exc_info"
            and isinstance(kw.value, ast.Constant)
            and not kw.value.value
            for kw in call.keywords
        )
    for kw in call.keywords:
        if kw.arg == "exc_info":
            return not (isinstance(kw.value, ast.Constant) and not kw.value.value)
    return False


def _is_exit_call(call: ast.Call) -> bool:
    return _dotted(call.func) in EXIT_FUNCS


def _classify_raise(node: ast.Raise, bound: str | None) -> str:
    if node.exc is None:
        return "reraise"
    if bound and isinstance(node.exc, ast.Name) and node.exc.id == bound:
        return "reraise"
    target = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
    if _last_name(target) == "SystemExit":
        return "exit"
    if node.cause is None:
        return "raise-unchained"
    if isinstance(node.cause, ast.Constant) and node.cause.value is None:
        return "raise-from-none"
    return "raise-chained"


def _mentions(node: ast.AST, name: str) -> bool:
    return any(
        isinstance(n, ast.Name) and n.id == name and isinstance(n.ctx, ast.Load)
        for n in ast.walk(node)
    )


def _exposes(node: ast.AST, name: str) -> bool:
    """True when *node* carries exception *name* or its text, not a field of it.

    ``str(exc)``, ``f"{exc}"``, ``exc`` and ``exc.args`` expose the message;
    ``exc.code`` or ``exc.status_code`` is a deliberate, structured field.
    """
    field_reads: set[int] = set()
    for n in ast.walk(node):
        if (
            isinstance(n, ast.Attribute)
            and isinstance(n.value, ast.Name)
            and n.value.id == name
        ):
            if n.attr in TEXT_ATTRS:
                return True
            field_reads.add(id(n.value))
    return any(
        isinstance(n, ast.Name)
        and n.id == name
        and isinstance(n.ctx, ast.Load)
        and id(n) not in field_reads
        for n in ast.walk(node)
    )


def _is_empty_literal(node: ast.AST) -> bool:
    if isinstance(node, (ast.Dict, ast.List, ast.Tuple, ast.Set)):
        return not (getattr(node, "keys", None) or getattr(node, "elts", None))
    if isinstance(node, ast.Constant):
        return node.value in ("", b"")
    if isinstance(node, ast.Call):
        return (
            isinstance(node.func, ast.Name)
            and node.func.id in EMPTY_CALLS
            and not node.args
            and not node.keywords
        )
    return False


def _count_statements(body: list[ast.stmt]) -> int:
    return sum(1 for n in _walk_local(body) if isinstance(n, ast.stmt))


def _cleanup_call(stmt: ast.stmt) -> str | None:
    """The callee of *stmt* when it is a bare (or awaited) cleanup call."""
    if not isinstance(stmt, ast.Expr):
        return None
    value = stmt.value.value if isinstance(stmt.value, ast.Await) else stmt.value
    if isinstance(value, ast.Call) and CLEANUP_CALL.match(_last_name(value.func)):
        return _dotted(value.func)
    return None


def _is_suppress(expr: ast.AST) -> bool:
    return isinstance(expr, ast.Call) and _last_name(expr.func) == "suppress"


def _always_bound(body: list[ast.stmt]) -> set[str]:
    """Names bound by the leading statements of *body* that cannot raise.

    ``stage = "prepare"`` as the first line of a ``try`` is bound before
    anything in the ``try`` can fail, so a handler reading it is safe.
    """
    out: set[str] = set()
    for stmt in body:
        if not (
            isinstance(stmt, (ast.Assign, ast.AnnAssign))
            and stmt.value is not None
            and (isinstance(stmt.value, ast.Constant) or _is_empty_literal(stmt.value))
        ):
            break
        targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
        if not all(isinstance(t, ast.Name) for t in targets):
            break
        out.update(t.id for t in targets)
    return out


class _FunctionNames:
    """Parameters and assignment lines of one function, for ``maybe_unbound``."""

    def __init__(self, func: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        args = func.args
        self.params = {a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
        for extra in (args.vararg, args.kwarg):
            if extra is not None:
                self.params.add(extra.arg)
        self.declared: set[str] = set()
        self.store_lines: dict[str, list[int]] = {}
        for node in _walk_local(func.body):
            if isinstance(node, (ast.Global, ast.Nonlocal)):
                self.declared.update(node.names)
            for name in _stored_names(node):
                self.store_lines.setdefault(name, []).append(node.lineno)

    def bound_before(self, name: str, line: int) -> bool:
        if name in self.params or name in self.declared:
            return True
        return any(ln < line for ln in self.store_lines.get(name, ()))


def _stored_names(node: ast.AST) -> list[str]:
    """Names a single node binds (assignment targets, imports, defs, handlers)."""
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
        return [node.id]
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [node.name]
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return [(a.asname or a.name).split(".", 1)[0] for a in node.names]
    if isinstance(node, ast.ExceptHandler) and node.name:
        return [node.name]
    if isinstance(node, ast.MatchAs) and node.name:
        return [node.name]
    return []


class _Surveyor(ast.NodeVisitor):
    def __init__(self, rel: str, implicit: bool) -> None:
        self.rel = rel
        self.want_implicit = implicit
        self.scope: list[str] = []
        self.functions: list[_FunctionNames | None] = [None]
        self.loop_depth: list[int] = [0]
        self.try_count = 0
        self.handlers: list[dict] = []
        self.suppress: list[dict] = []
        self.finally_blocks: list[dict] = []
        self.exits: list[dict] = []
        self.implicit: list[dict] = []

    # -- context -----------------------------------------------------------

    def _where(self, node: ast.AST) -> dict:
        return {
            "file": self.rel,
            "line": node.lineno,
            "function": ".".join(self.scope) or "<module>",
        }

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.scope.append(node.name)
        self.functions.append(_FunctionNames(node))
        self.loop_depth.append(0)
        if node.name in ("__exit__", "__aexit__"):
            self._record_exit(node)
        self.generic_visit(node)
        self.loop_depth.pop()
        self.functions.pop()
        self.scope.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope.append(node.name)
        self.functions.append(None)
        self.loop_depth.append(0)
        self.generic_visit(node)
        self.loop_depth.pop()
        self.functions.pop()
        self.scope.pop()

    def _visit_loop(self, node: ast.For | ast.AsyncFor | ast.While) -> None:
        self.loop_depth[-1] += 1
        self.generic_visit(node)
        self.loop_depth[-1] -= 1

    visit_For = _visit_loop
    visit_AsyncFor = _visit_loop
    visit_While = _visit_loop

    # -- try / except / finally -------------------------------------------

    def _visit_try(self, node: ast.Try) -> None:
        self.try_count += 1
        cleanup_last = self._cleanup_last(node.body)
        for handler in node.handlers:
            self.handlers.append(self._handler_record(node, handler, cleanup_last))
        if node.finalbody:
            self.finally_blocks.append(self._finally_record(node))
        self.generic_visit(node)

    visit_Try = _visit_try
    visit_TryStar = _visit_try

    @staticmethod
    def _cleanup_last(body: list[ast.stmt]) -> str | None:
        """A cleanup call on the try body's success exit, after other work.

        The call must end a block that is the try's exit: the try body itself,
        the trailing ``if``/``with``/loop block of an exit block, or any block
        that ends in ``return``. Some statement must come before it: a try
        around nothing but the cleanup is the guarded form, not the shape.
        Nested ``try`` statements are left to their own handlers.
        """

        def search(block: list[ast.stmt], preceded: bool, at_exit: bool) -> str | None:
            returns = bool(block) and isinstance(block[-1], ast.Return)
            tail = block[:-1] if returns else block
            if tail and (at_exit or returns) and (preceded or len(tail) > 1):
                call = _cleanup_call(tail[-1])
                if call:
                    return call
            for index, stmt in enumerate(block):
                before = preceded or index > 0
                exit_here = (at_exit or returns) and stmt is (
                    tail[-1] if tail else None
                )
                if isinstance(stmt, ast.If):
                    blocks = [stmt.body, stmt.orelse]
                elif isinstance(stmt, (ast.With, ast.AsyncWith, *_LOOPS)):
                    blocks = [stmt.body]
                else:
                    continue
                for sub in blocks:
                    found = search(sub, before, exit_here)
                    if found:
                        return found
            return None

        return search(body, False, True)

    def _handler_record(
        self, try_node: ast.Try, handler: ast.ExceptHandler, cleanup_last: str | None
    ) -> dict:
        caught = _caught_names(handler.type)
        bound = handler.name
        actions: set[str] = set()
        logs: list[dict] = []
        decisive: dict[str, bool] = {}  # action -> True if at top level of body
        top_level = {id(s) for s in handler.body}
        exposed = False
        for node in _walk_local(handler.body):
            action = None
            if isinstance(node, ast.Raise):
                action = _classify_raise(node, bound)
            elif isinstance(node, ast.Return):
                action = "return"
                if bound and node.value is not None and _exposes(node.value, bound):
                    exposed = True
            elif isinstance(node, ast.Continue):
                action = "continue"
            elif isinstance(node, ast.Break):
                action = "break"
            elif isinstance(node, ast.Call):
                action = self._call_action(node, logs)
                if bound and any(
                    kw.arg in EXPOSING_KEYWORDS and _exposes(kw.value, bound)
                    for kw in node.keywords
                ):
                    exposed = True
            elif (
                isinstance(node, ast.Dict)
                and bound
                and any(_exposes(v, bound) for v in node.values if v is not None)
            ):
                exposed = True
            if action:
                actions.add(action)
                at_top = id(node) in top_level or (
                    isinstance(node, ast.Call)
                    and any(
                        isinstance(s, ast.Expr) and s.value is node
                        for s in handler.body
                    )
                )
                decisive[action] = decisive.get(action, False) or at_top
        only_pass = all(
            isinstance(s, ast.Pass)
            or (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))
            for s in handler.body
        )
        if only_pass:
            actions.add("pass")
        outcome = next((a for a in _OUTCOME_PRIORITY if a in actions), None)
        if outcome is None:
            if actions & _REPORTING:
                outcome = "report-and-continue"
            elif only_pass:
                outcome = "pass"
            else:
                outcome = "fall-through"
        propagates = bool(actions & _PROPAGATING)
        record = self._where(handler)
        record.update(
            {
                "try_line": try_node.lineno,
                "caught": caught,
                "bare": handler.type is None,
                "broad": _is_broad(caught),
                "name": bound,
                "outcome": outcome,
                "outcome_conditional": outcome in decisive and not decisive[outcome],
                "actions": sorted(actions),
                "propagates": propagates,
                "logs": logs,
                "logs_traceback": "traceback" in actions
                or any(entry["traceback"] for entry in logs),
                "uses_exception": bool(bound)
                and any(_mentions(s, bound) for s in handler.body),
                "exception_exposed": exposed,
                "in_loop": self.loop_depth[-1] > 0,
                "maybe_unbound": self._maybe_unbound(try_node, handler),
                "flags": [],
                "body": _preview(handler.body),
            }
        )
        trivial_reraise = (
            len(handler.body) == 1
            and isinstance(handler.body[0], ast.Raise)
            and handler.body[0].exc is None
        )
        if cleanup_last and not trivial_reraise and outcome != "pass":
            record["flags"].append("cleanup-last-in-try")
            record["cleanup_call"] = cleanup_last
        return record

    @staticmethod
    def _call_action(call: ast.Call, logs: list[dict]) -> str | None:
        if _is_log_call(call):
            logs.append(
                {
                    "line": call.lineno,
                    "level": _log_level(call),
                    "traceback": _log_has_traceback(call),
                }
            )
            return "log"
        name = _dotted(call.func)
        if _is_exit_call(call):
            return "exit"
        if name.rsplit(".", 1)[-1] in TRACEBACK_FUNCS and "traceback" in name:
            return "traceback"
        if name == "print":
            return "print"
        if name in ("warnings.warn", "warn"):
            return "warn"
        return None

    def _maybe_unbound(
        self, try_node: ast.Try, handler: ast.ExceptHandler
    ) -> list[str]:
        func = self.functions[-1]
        if func is None:
            return []
        assigned_in_try: set[str] = set()
        for node in _walk_local(try_node.body):
            assigned_in_try.update(_stored_names(node))
        assigned_in_try -= _always_bound(try_node.body)
        if not assigned_in_try:
            return []
        handler_stores: dict[str, int] = {}
        for node in _walk_local(handler.body):
            for name in _stored_names(node):
                handler_stores.setdefault(name, node.lineno)
        out: set[str] = set()
        for node in _walk_local(handler.body):
            if not (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)):
                continue
            name = node.id
            if name not in assigned_in_try or name == handler.name:
                continue
            if handler_stores.get(name, node.lineno + 1) <= node.lineno:
                continue
            if func.bound_before(name, try_node.lineno):
                continue
            out.add(name)
        return sorted(out)

    def _finally_record(self, node: ast.Try) -> dict:
        unguarded: list[str] = []
        control_exit: set[str] = set()

        def visit(current: ast.AST, in_loop: bool) -> None:
            if isinstance(current, _NESTED_SCOPES):
                return
            if isinstance(current, ast.Try) and current.handlers:
                # Calls in the body of a nested try with handlers are guarded;
                # its handlers, else and finally are not.
                for part in (*(h.body for h in current.handlers), current.orelse):
                    for stmt in part:
                        visit(stmt, in_loop)
                for stmt in current.finalbody:
                    visit(stmt, in_loop)
                return
            if isinstance(current, (ast.With, ast.AsyncWith)) and any(
                _is_suppress(item.context_expr) for item in current.items
            ):
                return
            if isinstance(current, ast.Return):
                control_exit.add("return")
            elif isinstance(current, (ast.Break, ast.Continue)) and not in_loop:
                control_exit.add(type(current).__name__.lower())
            if isinstance(current, ast.Call):
                text = _dotted(current.func)
                if text not in unguarded:
                    unguarded.append(text)
            inner = in_loop or isinstance(current, _LOOPS)
            for child in ast.iter_child_nodes(current):
                visit(child, inner)

        for stmt in node.finalbody:
            visit(stmt, False)
        record = self._where(node.finalbody[0])
        record.update(
            {
                "try_line": node.lineno,
                "statements": _count_statements(node.finalbody),
                "unguarded_calls": unguarded[:MAX_FINALLY_CALLS],
                "unguarded_calls_total": len(unguarded),
                "control_flow_exit": sorted(control_exit),
            }
        )
        return record

    # -- with suppress(...) ------------------------------------------------

    def _visit_with(self, node: ast.With | ast.AsyncWith) -> None:
        for item in node.items:
            expr = item.context_expr
            if _is_suppress(expr):
                caught = [_dotted(a) for a in expr.args]
                record = self._where(node)
                statements = _count_statements(node.body)
                record.update(
                    {
                        "caught": caught,
                        "broad": _is_broad(caught),
                        "statements": statements,
                        "line_span": (node.end_lineno or node.lineno) - node.lineno + 1,
                        "body": _preview(node.body),
                    }
                )
                self.suppress.append(record)
        self.generic_visit(node)

    visit_With = _visit_with
    visit_AsyncWith = _visit_with

    # -- __exit__ ----------------------------------------------------------

    def _record_exit(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        returns = [n for n in _walk_local(node.body) if isinstance(n, ast.Return)]
        truthy = [
            r
            for r in returns
            if r.value is not None
            and not (isinstance(r.value, ast.Constant) and not r.value.value)
        ]
        if not truthy:
            return
        always = any(
            isinstance(s, ast.Return)
            and isinstance(s.value, ast.Constant)
            and s.value.value is True
            for s in node.body
        )
        record = self._where(node)
        record.update(
            {
                "suppresses": "always" if always else "conditional",
                "returns": [ast.unparse(r.value) for r in truthy][:5],
            }
        )
        self.exits.append(record)

    # -- implicit sites ----------------------------------------------------

    def visit_Call(self, node: ast.Call) -> None:
        if self.want_implicit:
            kind = self._implicit_kind(node)
            if kind:
                record = self._where(node)
                record.update({"kind": kind, "code": ast.unparse(node)[:120]})
                self.implicit.append(record)
        self.generic_visit(node)

    @staticmethod
    def _implicit_kind(node: ast.Call) -> str | None:
        func = node.func
        if isinstance(func, ast.Name):
            if func.id == "getattr" and len(node.args) == 3:
                return "getattr-default"
            if func.id == "hasattr":
                return "hasattr"
            return None
        if not isinstance(func, ast.Attribute):
            return None
        if (
            func.attr == "get"
            and len(node.args) == 2
            and _is_empty_literal(node.args[1])
        ):
            return "get-empty-default"
        if _dotted(func) in SUBPROCESS_UNCHECKED and not any(
            kw.arg == "check" for kw in node.keywords
        ):
            return "subprocess-unchecked"
        return None


def survey_file(
    path: Path, project_root: Path, *, implicit: bool = True
) -> dict | None:
    """Survey one file. Returns None when it cannot be parsed."""
    tree = parse_source(path)
    if tree is None:
        return None
    surveyor = _Surveyor(relative_to_root(path, project_root), implicit)
    surveyor.visit(tree)
    return {
        "try_statements": surveyor.try_count,
        "handlers": surveyor.handlers,
        "suppress": surveyor.suppress,
        "finally": surveyor.finally_blocks,
        "exits": surveyor.exits,
        "implicit": surveyor.implicit,
    }


def _summary(
    try_count: int,
    handlers: list[dict],
    suppress: list[dict],
    finally_blocks: list[dict],
    exits: list[dict],
    implicit: list[dict] | None,
) -> dict:
    def count(pred) -> int:
        return sum(1 for h in handlers if pred(h))

    return {
        "explicit_sites": len(handlers) + len(suppress) + len(exits),
        "try_statements": try_count,
        "handlers": len(handlers),
        "suppress_blocks": len(suppress),
        "suppressing_exit_methods": len(exits),
        "finally_blocks": len(finally_blocks),
        "by_outcome": dict(Counter(h["outcome"] for h in handlers).most_common()),
        "bare": count(lambda h: h["bare"]),
        "broad": count(lambda h: h["broad"]),
        "broad_not_propagating": count(lambda h: h["broad"] and not h["propagates"]),
        "neither_raise_nor_report": count(
            lambda h: not h["propagates"] and not (set(h["actions"]) & _REPORTING)
        ),
        "raise_unchained": count(lambda h: "raise-unchained" in h["actions"]),
        "logs_without_traceback": count(
            lambda h: h["logs"] and not h["logs_traceback"] and not h["propagates"]
        ),
        "exception_exposed": count(lambda h: h["exception_exposed"]),
        "maybe_unbound": count(lambda h: h["maybe_unbound"]),
        "cleanup_last_in_try": count(lambda h: "cleanup-last-in-try" in h["flags"]),
        "broad_suppress_multi_statement": sum(
            1 for s in suppress if s["broad"] and s["statements"] > 1
        ),
        "finally_with_control_flow_exit": sum(
            1 for f in finally_blocks if f["control_flow_exit"]
        ),
        "implicit_sites": None if implicit is None else len(implicit),
        "implicit_by_kind": None
        if implicit is None
        else dict(Counter(i["kind"] for i in implicit).most_common()),
    }


def analyze(target: str, *, max_files: int = 0, implicit: bool = True) -> dict:
    """Inventory the error-handling sites under *target*."""
    resolved, project_root, scan_root = resolve_target(target)
    files, files_total = collect_python_files(resolved, max_files)
    handlers: list[dict] = []
    suppress: list[dict] = []
    finally_blocks: list[dict] = []
    exits: list[dict] = []
    implicit_sites: list[dict] = []
    unparsed: list[str] = []
    try_count = 0
    for path in files:
        result = survey_file(path, project_root, implicit=implicit)
        if result is None:
            unparsed.append(relative_to_root(path, project_root))
            continue
        try_count += result["try_statements"]
        handlers.extend(result["handlers"])
        suppress.extend(result["suppress"])
        finally_blocks.extend(result["finally"])
        exits.extend(result["exits"])
        implicit_sites.extend(result["implicit"])
    output = build_envelope(project_root, scan_root, files_total, len(files))
    output["files_unparsed"] = unparsed
    output["summary"] = _summary(
        try_count,
        handlers,
        suppress,
        finally_blocks,
        exits,
        implicit_sites if implicit else None,
    )
    output["handlers"] = handlers
    output["suppress"] = suppress
    output["exit_methods"] = exits
    output["finally"] = finally_blocks
    output["implicit"] = implicit_sites if implicit else None
    if not implicit:
        output["skipped"] = ["implicit: --no-implicit"]
    return output


def main() -> None:
    argv = sys.argv[1:]
    implicit = "--no-implicit" not in argv
    target, max_files = parse_common_args(argv)
    emit(analyze(target, max_files=max_files, implicit=implicit))


if __name__ == "__main__":
    main()
