#!/usr/bin/env python3
"""Survey docstrings: coverage, contract accuracy, testable claims, suppressions.

Four inventories for the documentation-auditor, each one mechanical:

- ``coverage``: documented / total public modules, classes, functions, methods
  and HTTP route handlers (a route documented through ``summary=`` or
  ``description=`` on its decorator counts as documented), plus trivial
  docstrings that only restate the name.
- ``contract_issues``: a docstring's ``Returns:`` / ``Raises:`` / ``Args:``
  sections (Google, NumPy or Sphinx style) checked against the function's own
  ``return`` and ``raise`` statements, its signature and its return annotation.
- ``claims``: sentences in docstrings, comments and route/field descriptions
  that make a testable promise -- ``concurrency`` ("lock-free", "called with the
  lock held", "thread-safe", "does not block"), ``safety`` ("secure",
  "sandboxed", "escaped", "redacted") and ``invariant`` ("never", "always",
  docstrings only). Each carries ``function`` / ``function_key`` /
  ``class_key`` in scan_concurrency.py's naming, so concurrency claims can be
  joined by hand with that script's ``findings``, ``guards`` and ``slow_locks``.
- ``suppressions``: ``# nosec`` / ``# noqa`` / ``# type: ignore`` /
  ``# pyright: ignore`` / ``# pylint: disable`` / ``# pragma: no cover``
  comments, with the rule ids and the stated reason -- each one is a claim
  ("this rule does not apply here, because ...") to verify.

Test files are skipped unless ``--include-tests`` is given. Unparseable files
are listed under ``skipped``, never silently dropped.

Usage:
    python survey_docstrings.py [path] [--max-files N] [--include-tests]
"""

from __future__ import annotations

import ast
import bisect
import builtins
import io
import re
import sys
import tokenize
from dataclasses import dataclass, field
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

_FUNC_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)
_SCOPE_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)

# --------------------------------------------------------------------------
# File classification
# --------------------------------------------------------------------------


def is_test_file(rel: str) -> bool:
    """True for test modules: a ``test``/``tests`` directory, ``test_*.py``,
    ``*_test.py`` or ``conftest.py``."""
    parts = Path(rel).parts
    if any(p in ("test", "tests", "testing") for p in parts[:-1]):
        return True
    name = parts[-1] if parts else rel
    return (
        name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py"
    )


def module_name(rel: str) -> str:
    """Dotted module name for *rel*, the same rule scan_concurrency.py uses."""
    parts = list(Path(rel).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts) or rel


# --------------------------------------------------------------------------
# Docstring parsing
# --------------------------------------------------------------------------

_GOOGLE_SECTIONS = {
    "args": "args",
    "arguments": "args",
    "parameters": "args",
    "params": "args",
    "keyword args": "args",
    "keyword arguments": "args",
    "other parameters": "args",
    "returns": "returns",
    "return": "returns",
    "yields": "yields",
    "yield": "yields",
    "raises": "raises",
    "raise": "raises",
    "exceptions": "raises",
    "except": "raises",
    "attributes": "other",
    "example": "other",
    "examples": "other",
    "note": "other",
    "notes": "other",
    "see also": "other",
    "warning": "other",
    "warnings": "other",
    "todo": "other",
    "references": "other",
}
_GOOGLE_HEADER = re.compile(
    r"^(\s*)(" + "|".join(re.escape(k) for k in _GOOGLE_SECTIONS) + r"):\s*$",
    re.IGNORECASE,
)
_DASHES = re.compile(r"^\s*-{3,}\s*$")
_SPHINX_FIELD = re.compile(
    r"^\s*:(param|parameter|arg|argument|key|keyword|type|returns?|rtype|raises?|except|exception|yields?|ytype)\b"
)
_SPHINX_PARAM = re.compile(
    r"^\s*:(?:param|parameter|arg|argument|key|keyword)\s+(?:[^:]*\s)?\**(\w+)\s*:"
)
_SPHINX_RAISES = re.compile(r"^\s*:(?:raises?|except|exception)\s+([\w.]+)\s*:")
_SPHINX_RTYPE = re.compile(r"^\s*:rtype:\s*(.+?)\s*$")
_SPHINX_RETURNS = re.compile(r"^\s*:returns?:")
_SPHINX_YIELDS = re.compile(r"^\s*:(?:yields?|ytype):")
_ARG_ENTRY = re.compile(r"^\**(\w+)\s*(?:\(([^)]*)\))?\s*:")
_RET_PAREN = re.compile(r"^\w+\s*\(([^)]+)\)\s*:")
_PROSE_RAISES = re.compile(
    r"\b(?i:raises|raise|is\s+raised|are\s+raised)\s+(?:an?\s+|the\s+)?"
    r"(?::\w+:)?`{0,2}~?([A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt))\b"
)
_EXC_NAME = re.compile(r"^:?(?:exc:|class:)?`?~?([A-Za-z_][\w.]*)`?")


@dataclass
class DocContract:
    style: str = "plain"
    args: list[str] = field(default_factory=list)
    returns: bool = False
    returns_type: str | None = None
    yields: bool = False
    raises: list[str] = field(default_factory=list)
    raises_prose: list[str] = field(default_factory=list)


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _entries(body: list[str]) -> list[str]:
    """Lines of a section body at its base indentation (continuations dropped)."""
    nonblank = [ln for ln in body if ln.strip()]
    if not nonblank:
        return []
    base = min(_indent(ln) for ln in nonblank)
    return [ln.strip() for ln in nonblank if _indent(ln) == base]


def _split_sections(lines: list[str]) -> tuple[str, dict[str, list[str]]]:
    """Return (style, {canonical section: body lines}) for Google/NumPy layouts."""
    sections: dict[str, list[str]] = {}
    style = ""
    i = 0
    while i < len(lines):
        line = lines[i]
        m = _GOOGLE_HEADER.match(line)
        if m:
            name = _GOOGLE_SECTIONS[m.group(2).lower()]
            hdr = len(m.group(1))
            body: list[str] = []
            j = i + 1
            while j < len(lines):
                nxt = lines[j]
                if nxt.strip() and _indent(nxt) <= hdr:
                    break
                body.append(nxt)
                j += 1
            sections.setdefault(name, []).extend(body)
            style = style or "google"
            i = j
            continue
        if (
            i + 1 < len(lines)
            and _DASHES.match(lines[i + 1])
            and line.strip().lower() in _GOOGLE_SECTIONS
        ):
            name = _GOOGLE_SECTIONS[line.strip().lower()]
            hdr = _indent(line)
            body = []
            j = i + 2
            while j < len(lines):
                if (
                    j + 1 < len(lines)
                    and _DASHES.match(lines[j + 1])
                    and _indent(lines[j]) <= hdr
                    and lines[j].strip()
                ):
                    break
                body.append(lines[j])
                j += 1
            sections.setdefault(name, []).extend(body)
            style = "numpy"
            i = j
            continue
        i += 1
    return style, sections


def docstring_style(doc: str | None) -> str:
    """``google``, ``numpy``, ``sphinx``, ``plain`` (no sections) or ``none``."""
    if not doc or not doc.strip():
        return "none"
    return parse_docstring(doc).style


def _exc_names(entry: str) -> list[str]:
    out = []
    for part in re.split(r"\s*,\s*|\s+or\s+", entry.split(":", 1)[0].strip()):
        m = _EXC_NAME.match(part.strip())
        if m:
            name = m.group(1).rsplit(".", 1)[-1]
            if name[:1].isupper():
                out.append(name)
    return out


def parse_docstring(doc: str) -> DocContract:
    """Extract the Args/Returns/Yields/Raises contract from a cleaned docstring."""
    lines = doc.expandtabs().splitlines()
    contract = DocContract()
    sphinx = any(_SPHINX_FIELD.match(ln) for ln in lines)
    if sphinx:
        for ln in lines:
            if m := _SPHINX_PARAM.match(ln):
                contract.args.append(m.group(1))
            elif m := _SPHINX_RAISES.match(ln):
                contract.raises.append(m.group(1).rsplit(".", 1)[-1])
            elif m := _SPHINX_RTYPE.match(ln):
                contract.returns = True
                contract.returns_type = m.group(1)
            elif _SPHINX_RETURNS.match(ln):
                contract.returns = True
            elif _SPHINX_YIELDS.match(ln):
                contract.yields = True

    style, sections = _split_sections(lines)
    if sphinx:
        contract.style = "mixed" if style else "sphinx"
    else:
        contract.style = style or "plain"
    for entry in _entries(sections.get("args", [])):
        if style == "numpy":
            for name in entry.split(":", 1)[0].split(","):
                name = name.strip().lstrip("*")
                if name.isidentifier():
                    contract.args.append(name)
        elif m := _ARG_ENTRY.match(entry):
            contract.args.append(m.group(1))
    if "returns" in sections and not contract.returns_type:
        contract.returns = True
        entries = _entries(sections["returns"])
        if entries:
            contract.returns_type = _return_type_text(entries[0], style)
    if "yields" in sections:
        contract.yields = True
    for entry in _entries(sections.get("raises", [])):
        contract.raises.extend(_exc_names(entry))
    if "raises" not in sections and not contract.raises:
        for m in _PROSE_RAISES.finditer(" ".join(lines)):
            name = m.group(1).rsplit(".", 1)[-1]
            if name not in contract.raises_prose:
                contract.raises_prose.append(name)
    return contract


def _return_type_text(entry: str, style: str) -> str | None:
    """The type a Returns entry names, or None when it only describes."""
    if style == "numpy":
        return entry.split(":", 1)[1].strip() if ":" in entry else entry.strip()
    if m := _RET_PAREN.match(entry):
        return m.group(1).strip()
    if ":" not in entry:
        bare = entry.strip().rstrip(".")
        return (
            bare
            if re.fullmatch(r"[\w\[\]., |]+", bare) and len(bare.split()) <= 3
            else None
        )
    head = entry.split(":", 1)[0].strip()
    if not head or len(head) > 60 or len(head.split()) > 4:
        return None
    return head


# --------------------------------------------------------------------------
# Type kinds -- a coarse lattice that is safe to compare
# --------------------------------------------------------------------------

_KIND_NAMES = {
    "str": "str", "string": "str", "unicode": "str", "Text": "str",
    "int": "int", "integer": "int",
    "float": "float",
    "bool": "bool", "boolean": "bool",
    "dict": "dict", "Dict": "dict", "dictionary": "dict", "Mapping": "dict",
    "MutableMapping": "dict",
    "list": "list", "List": "list",
    "tuple": "tuple", "Tuple": "tuple",
    "set": "set", "Set": "set", "frozenset": "set", "FrozenSet": "set",
    "bytes": "bytes",
    "None": "none", "none": "none", "NoneType": "none",
    "Any": "any", "any": "any", "object": "any",
}  # fmt: skip
_CONTAINER_HEADS = {"list", "dict", "tuple", "set", "frozenset", "List", "Dict",
                    "Tuple", "Set", "FrozenSet", "Mapping", "MutableMapping"}  # fmt: skip


def _name_of(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def annotation_kinds(node: ast.AST | None) -> set[str] | None:
    """Kinds an annotation (or a parsed doc type) admits; None when unknown."""
    if node is None:
        return None
    if isinstance(node, ast.Constant):
        if node.value is None:
            return {"none"}
        if isinstance(node.value, str):
            try:
                return annotation_kinds(ast.parse(node.value, mode="eval").body)
            except SyntaxError:
                return None
        return None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        left, right = annotation_kinds(node.left), annotation_kinds(node.right)
        return None if left is None or right is None else left | right
    if isinstance(node, ast.Subscript):
        head = _name_of(node.value)
        args = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
        if head == "Optional":
            inner = annotation_kinds(args[0])
            return None if inner is None else inner | {"none"}
        if head == "Union":
            out: set[str] = set()
            for arg in args:
                kinds = annotation_kinds(arg)
                if kinds is None:
                    return None
                out |= kinds
            return out
        if head in _CONTAINER_HEADS:
            return {_KIND_NAMES[head]}
        if head == "Annotated" and args:
            return annotation_kinds(args[0])
        return None
    name = _name_of(node)
    if name in _KIND_NAMES:
        return {_KIND_NAMES[name]}
    return None


def doc_type_kinds(text: str | None) -> set[str] | None:
    """Kinds a docstring type phrase names ("tuple", "str or None", "list of
    int", "Optional[dict]"); None when any part is unknown."""
    if not text:
        return None
    text = re.sub(r":\w+:`~?([^`]+)`", r"\1", text).replace("`", "").strip()
    text = re.sub(r",?\s*optional\s*$", " | None", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+or\s+", " | ", text)
    text = re.sub(r"^(\w+)\s+(?:of|containing|with)\b.*$", r"\1", text)
    try:
        return annotation_kinds(ast.parse(text, mode="eval").body)
    except SyntaxError:
        return None


def compatible(actual: str, documented: set[str]) -> bool:
    if actual in documented or "any" in documented or actual == "any":
        return True
    if actual == "bool" and "int" in documented:
        return True
    return actual == "int" and "float" in documented


_CALL_KINDS = {
    "str": "str", "repr": "str", "int": "int", "len": "int", "float": "float",
    "bool": "bool", "isinstance": "bool", "issubclass": "bool", "callable": "bool",
    "list": "list", "sorted": "list", "dict": "dict", "tuple": "tuple",
    "set": "set", "frozenset": "set", "bytes": "bytes",
}  # fmt: skip


def expr_kind(node: ast.AST | None) -> str | None:
    """The kind a return expression certainly has, or None when unknown."""
    if node is None:
        return "none"
    if isinstance(node, ast.Constant):
        value = node.value
        if value is None:
            return "none"
        for kind, typ in (("bool", bool), ("int", int), ("float", float),
                          ("str", str), ("bytes", bytes)):  # fmt: skip
            if isinstance(value, typ):
                return kind
        return None
    if isinstance(node, ast.JoinedStr):
        return "str"
    if isinstance(node, ast.Tuple):
        return "tuple"
    if isinstance(node, (ast.List, ast.ListComp)):
        return "list"
    if isinstance(node, (ast.Dict, ast.DictComp)):
        return "dict"
    if isinstance(node, (ast.Set, ast.SetComp)):
        return "set"
    if isinstance(node, ast.Compare) or (
        isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not)
    ):
        return "bool"
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        return _CALL_KINDS.get(node.func.id)
    return None


# --------------------------------------------------------------------------
# Function-body helpers
# --------------------------------------------------------------------------


def iter_own(node: ast.AST):
    """Walk *node*'s body without descending into nested scopes."""
    stack = list(ast.iter_child_nodes(node))
    while stack:
        child = stack.pop()
        yield child
        if not isinstance(child, _SCOPE_TYPES):
            stack.extend(ast.iter_child_nodes(child))


def _body_without_docstring(func: ast.AST) -> list[ast.stmt]:
    body = list(func.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return body


def is_stub(func: ast.AST) -> bool:
    """Abstract, overload or placeholder bodies: nothing to check against."""
    for dec in func.decorator_list:
        if _name_of(dec) in ("abstractmethod", "overload", "abstractproperty"):
            return True
    for stmt in _body_without_docstring(func):
        if isinstance(stmt, ast.Pass):
            continue
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            continue
        if isinstance(stmt, ast.Raise) and stmt.exc is not None:
            exc = stmt.exc.func if isinstance(stmt.exc, ast.Call) else stmt.exc
            if _name_of(exc) == "NotImplementedError":
                continue
        if (
            isinstance(stmt, ast.Return)
            and isinstance(stmt.value, ast.Name)
            and stmt.value.id == "NotImplemented"
        ):
            continue
        return False
    return True


def _param_names(args: ast.arguments) -> list[str]:
    names = [a.arg for a in args.posonlyargs + args.args + args.kwonlyargs]
    if args.vararg:
        names.append(args.vararg.arg)
    if args.kwarg:
        names.append(args.kwarg.arg)
    return names


def _param_annotations(args: ast.arguments) -> dict[str, ast.AST | None]:
    return {a.arg: a.annotation for a in args.posonlyargs + args.args + args.kwonlyargs}


def _assigned_names(func: ast.AST) -> set[str]:
    out: set[str] = set()
    for node in iter_own(func):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            out.add(node.id)
    return out


def _raise_name(exc: ast.AST | None) -> str | None:
    """Exception class name a ``raise`` names; None for a re-raise or a variable."""
    if exc is None:
        return None
    if isinstance(exc, ast.Call):
        exc = exc.func
    name = _name_of(exc)
    return name if name and name[:1].isupper() else None


def _called_names(func: ast.AST) -> set[str]:
    out = set()
    for node in iter_own(func):
        if isinstance(node, ast.Call):
            name = _name_of(node.func)
            if name:
                out.add(name)
    return out


def _direct_raises(func: ast.AST) -> tuple[set[str], bool]:
    """(exception names raised in the body, whether any raise is dynamic)."""
    names: set[str] = set()
    dynamic = False
    for node in iter_own(func):
        if isinstance(node, ast.Raise):
            name = _raise_name(node.exc)
            if name is None:
                dynamic = True
            else:
                names.add(name)
        elif isinstance(node, ast.Assert):
            names.add("AssertionError")
    return names, dynamic


def _raises_outside_try(func: ast.AST) -> list[tuple[int, str, ast.Raise]]:
    """Explicit ``raise X`` statements not inside any ``try`` body."""
    out: list[tuple[int, str, ast.Raise]] = []

    def visit(node: ast.AST, in_try: bool) -> None:
        if isinstance(node, _SCOPE_TYPES):
            return
        if isinstance(node, ast.Raise) and not in_try:
            name = _raise_name(node.exc)
            if name:
                out.append((node.lineno, name, node))
        if isinstance(node, ast.Try) or type(node).__name__ == "TryStar":
            for stmt in node.body:
                visit(stmt, True)
            for part in (node.handlers, node.orelse, node.finalbody):
                for stmt in part:
                    visit(stmt, in_try)
            return
        for child in ast.iter_child_nodes(node):
            visit(child, in_try)

    for stmt in func.body:
        visit(stmt, False)
    return out


# Builtin exceptions that library and builtin calls raise all the time: a
# documented one may well come from a call the survey cannot see into.
_LIBRARY_RAISED = {
    "ValueError", "TypeError", "KeyError", "IndexError", "LookupError",
    "AttributeError", "OSError", "IOError", "FileNotFoundError", "PermissionError",
    "IsADirectoryError", "NotADirectoryError", "FileExistsError", "UnicodeError",
    "UnicodeDecodeError", "UnicodeEncodeError", "ConnectionError", "TimeoutError",
    "RuntimeError", "StopIteration", "ZeroDivisionError", "OverflowError",
    "ArithmeticError", "MemoryError", "RecursionError", "EOFError", "ImportError",
    "ModuleNotFoundError", "KeyboardInterrupt", "Exception", "BaseException",
}  # fmt: skip


class ProjectIndex:
    """Project-wide facts the contract checks need: which exceptions each
    function name raises (directly and through callees named in the project),
    and the project's exception class hierarchy."""

    def __init__(self) -> None:
        self.raises_by_name: dict[str, set[str]] = {}
        self.calls_by_name: dict[str, set[str]] = {}
        self.bases: dict[str, set[str]] = {}
        self._closure: dict[str, set[str]] = {}

    def add_module(self, tree: ast.Module) -> None:
        for node in ast.walk(tree):
            if isinstance(node, _FUNC_TYPES):
                raised, _ = _direct_raises(node)
                self.raises_by_name.setdefault(node.name, set()).update(raised)
                self.calls_by_name.setdefault(node.name, set()).update(
                    _called_names(node)
                )
            elif isinstance(node, ast.ClassDef):
                self.bases.setdefault(node.name, set()).update(
                    n for n in (_name_of(b) for b in node.bases) if n
                )

    def defines_function(self, name: str) -> bool:
        return name in self.raises_by_name

    def transitive_raises(self, name: str) -> set[str]:
        if name in self._closure:
            return self._closure[name]
        seen: set[str] = set()
        out: set[str] = set()
        stack = [name]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            out |= self.raises_by_name.get(cur, set())
            stack.extend(self.calls_by_name.get(cur, ()))
        self._closure[name] = out
        return out

    def ancestors(self, name: str) -> set[str]:
        out: set[str] = set()
        stack = [name]
        while stack:
            cur = stack.pop()
            if cur in out:
                continue
            out.add(cur)
            stack.extend(self.bases.get(cur, ()))
            builtin = getattr(builtins, cur, None)
            if isinstance(builtin, type) and issubclass(builtin, BaseException):
                out.update(c.__name__ for c in builtin.__mro__)
        return out

    def matches(self, raised: str, documented: str) -> bool:
        return documented in self.ancestors(raised)


# --------------------------------------------------------------------------
# Contract checks
# --------------------------------------------------------------------------


def _issue(rel, func, qualname, kind, confidence, documented, detail, evidence=None):
    out = {
        "file": rel,
        "line": func.lineno,
        "function": qualname,
        "kind": kind,
        "confidence": confidence,
        "documented": documented,
        "detail": detail,
    }
    if evidence:
        out["evidence"] = evidence
    return out


def check_contract(
    rel: str, func: ast.AST, qualname: str, doc: str, index: ProjectIndex
) -> list[dict]:
    """Compare one function's docstring contract with its body and signature."""
    contract = parse_docstring(doc)
    issues: list[dict] = []
    params = _param_names(func.args)

    if contract.args and func.args.kwarg is None:
        # ALL-CAPS entries are CLI metavars in a click/typer help docstring.
        stale = [a for a in contract.args if a not in params and not a.isupper()]
        for name in stale:
            issues.append(
                _issue(
                    rel,
                    func,
                    qualname,
                    "args-documented-not-in-signature",
                    "high",
                    name,
                    f"docstring documents parameter `{name}`, which the signature "
                    f"does not have (parameters: {', '.join(params) or 'none'})",
                )
            )

    if is_stub(func):
        return issues
    own = list(iter_own(func))
    generator = any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in own)
    returns = [n for n in own if isinstance(n, ast.Return)]

    if contract.returns and not generator:
        issues.extend(_check_returns(rel, func, qualname, contract, returns))
    if contract.raises or contract.raises_prose:
        issues.extend(_check_raises(rel, func, qualname, contract, index))
    return issues


def _check_returns(rel, func, qualname, contract, returns) -> list[dict]:
    doc_kinds = doc_type_kinds(contract.returns_type)
    documented = contract.returns_type or "(described, no type)"
    if doc_kinds == {"none"}:
        return []
    valued = [r for r in returns if expr_kind(r.value) != "none"]
    if not valued:
        return [
            _issue(
                rel,
                func,
                qualname,
                "returns-documented-no-value",
                "high",
                documented,
                "docstring documents a return value, but no `return` statement in "
                "the body returns one",
            )
        ]
    if doc_kinds is None or "any" in doc_kinds:
        return []
    issues = []
    ann_kinds = annotation_kinds(func.returns)
    if (
        ann_kinds
        and "any" not in ann_kinds
        and not any(compatible(k, doc_kinds) for k in ann_kinds if k != "none")
    ):
        issues.append(
            _issue(
                rel,
                func,
                qualname,
                "returns-type-mismatch",
                "high",
                documented,
                f"docstring says the return is `{contract.returns_type}`, the "
                f"annotation says `{ast.unparse(func.returns)}`",
            )
        )
        return issues
    wrong = []
    for ret in valued:
        kind = expr_kind(ret.value)
        if kind and kind != "none" and not compatible(kind, doc_kinds):
            wrong.append(f"{ret.lineno}: return {ast.unparse(ret.value)[:80]}")
    if wrong:
        issues.append(
            _issue(
                rel,
                func,
                qualname,
                "returns-type-mismatch",
                "medium",
                documented,
                f"docstring says the return is `{contract.returns_type}`, but "
                f"{len(wrong)} of {len(valued)} return statements return another type",
                wrong,
            )
        )
    annotations = _param_annotations(func.args)
    reassigned = _assigned_names(func)
    passthrough = []
    for ret in valued:
        if (
            isinstance(ret.value, ast.Name)
            and ret.value.id in annotations
            and ret.value.id not in ("self", "cls")
            and ret.value.id not in reassigned
        ):
            ann = annotations[ret.value.id]
            kinds = annotation_kinds(ann)
            if (
                kinds
                and "any" not in kinds
                and all(compatible(k, doc_kinds) for k in kinds)
            ):
                continue
            shown = ast.unparse(ann) if ann is not None else "unannotated"
            passthrough.append(
                f"{ret.lineno}: return {ret.value.id}  (parameter, {shown})"
            )
    if passthrough:
        issues.append(
            _issue(
                rel,
                func,
                qualname,
                "returns-parameter-unchanged",
                "medium",
                documented,
                f"docstring says the return is `{contract.returns_type}`, but a path "
                "returns a parameter unchanged whose type the signature does not "
                "restrict to that",
                passthrough,
            )
        )
    return issues


def _not_raised(rel, node, qualname, documented, bodies, index, owner="function"):
    """Documented exceptions that none of *bodies* (nor their callees) raises."""
    raised: set[str] = set()
    called: set[str] = set()
    for body in bodies:
        names, dynamic = _direct_raises(body)
        if dynamic:
            return []
        raised |= names
        called |= _called_names(body)
    via_callees: set[str] = set()
    for name in called:
        via_callees |= index.transitive_raises(name)
    external_calls = any(not index.defines_function(n) for n in called)
    issues = []
    for doc_exc in dict.fromkeys(documented):
        if any(index.matches(r, doc_exc) for r in raised | via_callees):
            continue
        if doc_exc in index.bases:
            confidence = "medium"
        elif doc_exc in _LIBRARY_RAISED or not hasattr(builtins, doc_exc):
            confidence = "low" if external_calls else "medium"
        else:
            confidence = "medium"
        issues.append(
            _issue(
                rel,
                node,
                qualname,
                "raises-documented-not-raised",
                confidence,
                doc_exc,
                f"docstring says it raises `{doc_exc}`; neither this {owner} nor "
                "any project function it calls by name raises it"
                + ("; a library call might" if confidence == "low" else ""),
            )
        )
    return issues


def check_class_raises(rel, cls: ast.ClassDef, qualname, doc, index) -> list[dict]:
    """A class docstring that says the class raises X ("Raises `X` when ..."):
    some method, or a project function a method calls, must raise X."""
    contract = parse_docstring(doc)
    documented = contract.raises + contract.raises_prose
    methods = [n for n in cls.body if isinstance(n, _FUNC_TYPES) and not is_stub(n)]
    if not documented or not methods:
        return []
    return _not_raised(
        rel, cls, qualname, documented, methods, index, owner="class's methods"
    )


def _check_raises(rel, func, qualname, contract, index: ProjectIndex) -> list[dict]:
    issues = _not_raised(
        rel, func, qualname, contract.raises + contract.raises_prose, [func], index
    )
    for line, name, _node in _raises_outside_try(func) if contract.raises else ():
        if name == "NotImplementedError":
            continue
        if any(index.matches(name, d) for d in contract.raises):
            continue
        issues.append(
            _issue(
                rel,
                func,
                qualname,
                "raises-undocumented",
                "medium",
                ", ".join(contract.raises),
                f"line {line} raises `{name}`, which the Raises section does not list",
            )
        )
    return issues


# --------------------------------------------------------------------------
# Claims
# --------------------------------------------------------------------------

_LOCK_WORD = r"(?:lock|mutex|semaphore|rlock|condition)"
# A word naming a lock: "lock", "read lock", `_index_lock`, `IndexLock`,
# but not "block", "unblocking", "clock" or "deadlock".
_LOCK_REF = r"\b(?!(?:un|non)?block|clock|deadlock)\w*" + _LOCK_WORD + r"\w*"
_CLAIM_PATTERNS: list[tuple[str, re.Pattern]] = [
    (
        "concurrency",
        re.compile(
            r"\block[- ]free\b"
            r"|\bwithout\s+(?:taking|holding|acquiring|grabbing|any)\b[^.;]{0,30}?"
            + _LOCK_REF
            + r"|\b(?:called|invoked|run|runs|executed)\s+(?:only\s+)?(?:with|while|under|holding|inside)\b[^.;]{0,40}?"
            + _LOCK_REF
            + r"|"
            + _LOCK_REF
            + r"[^.;]{0,25}?\b(?:held|acquired|taken)\b"
            r"|\bcallers?\s+(?:must|should|needs?\s+to)\s+(?:hold|own|acquire|take)\b"
            r"|\b(?:holding|under)\s+(?:(?:the|a|its|our|this|that)\s+)?(?:[\w-]+\s+)?`?"
            + _LOCK_REF
            + r"|\bthread[- ]?safe(?:ty)?\b|\bnot\s+thread[- ]?safe\b"
            r"|\batomic(?:ally)?\b"
            r"|\b(?:does\s+not|doesn't|do\s+not|don't|never|won't|will\s+not|must\s+not|cannot|can't)\s+block\b"
            r"|\bnon[- ]?blocking\b"
            r"|\b(?:does\s+not|doesn't|don't|never|won't|will\s+not)\s+contend\b"
            r"|\b(?:safe|ok|okay)\s+to\s+(?:call|use|run)\s+(?:concurrently|from\s+(?:any|multiple|other|several)\s+threads?)\b"
            r"|\b(?:can|may)\s+be\s+called\s+(?:concurrently|from\s+(?:any|multiple|other|several)\s+threads?)\b"
            r"|\bre-?entrant\b|\bidempotent\b"
            r"|\b(?:keeps?|kept)\s+serving\b|\b(?:stays?|remains?)\s+(?:responsive|available)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "safety",
        re.compile(
            r"\bsecure(?:ly|d)?\b|\binsecure\b"
            r"|(?<![-\w])safe(?:ly)?\b|\bunsafe\b"
            r"|\bsandbox(?:ed|ing|es)?\b"
            r"|\b(?:escape[sd]|escaping|unescaped)\b"
            r"|\bsaniti[sz](?:e[sd]?|ing|ation)\b"
            r"|\b(?:redact(?:s|ed|ion|ing)?|obfuscat\w*)\b"
            r"|\bmask(?:s|ed|ing)?\b"
            r"|\b(?:hides?|hidden|hiding)\b"
            r"|\b(?:never|not)\s+(?:be\s+)?(?:logged|printed|exposed|leaked|echoed|persisted)\b"
            r"|\buntrusted\b|\binjection\b|\btraversal\b"
            r"|\bconstant[- ]time\b"
            r"|\bcannot\s+(?:escape|access|read|write|reach)\b",
            re.IGNORECASE,
        ),
    ),
]
_INVARIANT = re.compile(
    r"\b(?:never|always|guarantee[sd]?|only\s+ever|must\s+not|at\s+most\s+once"
    r"|exactly\s+once|in\s+lockstep)\b",
    re.IGNORECASE,
)
_LOCK_IDENT = re.compile(
    r"`([^`]*(?i:lock|mutex|semaphore|condition)[^`]*)`"
    r"|\b(\w*(?:_lock|Lock|_mutex|Mutex|_cond|_cv|_sem)\w*)\b"
    r"|\b([a-z][\w-]*)\s+(?i:lock)\b"
)
_LOCK_NOISE = {"the", "a", "an", "this", "that", "its", "our", "any", "same", "one",
               "no", "global", "without", "holding", "with", "under", "own",
               "lock", "free", "non", "thread", "dead", "to", "is", "and", "or",
               "of", "per", "each", "every", "which", "lock-free"}  # fmt: skip


def _snippet(text: str, start: int, end: int) -> str:
    """The sentence around ``text[start:end]``; a newline (paragraph break,
    section header or new entry) is a hard boundary."""
    left = max(text.rfind(". ", 0, start), text.rfind("; ", 0, start))
    left = 0 if left < 0 else left + 2
    left = max(left, text.rfind("\n", 0, start) + 1)
    right_candidates = [
        i
        for i in (text.find(". ", end), text.find("; ", end), text.find("\n", end))
        if i >= 0
    ]
    right = min(right_candidates) + 1 if right_candidates else len(text)
    out = text[left:right].strip()
    return out if len(out) <= 240 else out[:237] + "..."


_SEGMENT_START = re.compile(r"^(?:\**\w+\s*(?:\([^)]*\))?\s*:|:\w+|-{3,}$|[-*] )")


def locks_named(text: str) -> list[str]:
    out: list[str] = []
    for m in _LOCK_IDENT.finditer(text):
        if m.group(1):
            name = m.group(1).strip()
        elif m.group(2):
            name = m.group(2)
        else:
            if m.group(3).lower() in _LOCK_NOISE:
                continue
            name = f"{m.group(3)} lock"
        if name.lower() not in _LOCK_NOISE and name not in out:
            out.append(name)
    return out


def find_claims(lines: list[tuple[int, str]], *, invariants: bool) -> list[dict]:
    """Match claim patterns over *lines* (``(lineno, text)``) joined as prose."""
    starts: list[int] = []
    linenos: list[int] = []
    parts: list[str] = []
    pos = 0
    for lineno, text in lines:
        text = text.strip()
        # A blank line, a section header or a new Args-style entry starts a new
        # segment, so a snippet never runs from one entry into the next.
        sep = "\n" if not text or _SEGMENT_START.match(text) else " "
        starts.append(pos)
        linenos.append(lineno)
        parts.append(sep + text if parts else text)
        pos += len(parts[-1])
    joined = "".join(parts)
    patterns = list(_CLAIM_PATTERNS)
    if invariants:
        patterns.append(("invariant", _INVARIANT))
    out = []
    taken: list[tuple[int, int]] = []
    seen: set[tuple[str, str]] = set()
    for category, pattern in patterns:
        for m in pattern.finditer(joined):
            if any(s < m.end() and m.start() < e for s, e in taken):
                continue
            taken.append((m.start(), m.end()))
            # One entry per phrase per docstring/comment block: an Args list
            # that says "hidden" for every parameter is one claim.
            key = (category, re.sub(r"\s+", " ", m.group(0).lower()))
            if key in seen:
                continue
            seen.add(key)
            idx = bisect.bisect_right(starts, m.start() + 1) - 1
            text = _snippet(joined, m.start(), m.end())
            out.append(
                {
                    "line": linenos[max(idx, 0)],
                    "category": category,
                    "phrase": m.group(0),
                    "text": text,
                    "locks_named": locks_named(text)
                    if category == "concurrency"
                    else [],
                }
            )
    return out


# --------------------------------------------------------------------------
# Suppression comments
# --------------------------------------------------------------------------

_SUPPRESSIONS = [
    ("bandit", re.compile(r"\bnosec\b(?:\s*:?\s*(?P<rules>B\d{3}(?:[\s,]+B\d{3})*))?", re.IGNORECASE)),
    ("ruff/flake8", re.compile(r"\bnoqa\b(?:\s*:\s*(?P<rules>[A-Z]+\d+(?:[\s,]+[A-Z]+\d+)*))?", re.IGNORECASE)),
    ("mypy", re.compile(r"\btype:\s*ignore\b(?:\[(?P<rules>[^\]]*)\])?")),
    ("pyright", re.compile(r"\bpyright:\s*ignore\b(?:\[(?P<rules>[^\]]*)\])?")),
    ("pylint", re.compile(r"\bpylint:\s*disable(?:-next)?\s*=\s*(?P<rules>[\w\-]+(?:\s*,\s*[\w\-]+)*)")),
    ("coverage", re.compile(r"\bpragma:\s*no\s*(?:cover|branch)\b")),
]  # fmt: skip


def parse_suppressions(comment: str) -> list[dict]:
    found = []
    spans = []
    for tool, pattern in _SUPPRESSIONS:
        for m in pattern.finditer(comment):
            rules_text = m.groupdict().get("rules") or ""
            rules = [r for r in re.split(r"[\s,]+", rules_text) if r]
            found.append({"tool": tool, "directive": m.group(0), "rules": rules})
            spans.append((m.start(), m.end()))
    if not found:
        return []
    reason = comment
    for start, end in sorted(spans, reverse=True):
        reason = reason[:start] + " " + reason[end:]
    reason = re.sub(r"#", " ", reason)
    reason = re.sub(r"\s+", " ", reason).strip(" -:;,.–—")
    for item in found:
        item["blanket"] = not item["rules"]
        item["reason"] = reason
    return found


# --------------------------------------------------------------------------
# Per-file survey
# --------------------------------------------------------------------------

_ROUTE_METHODS = {"get", "post", "put", "patch", "delete", "head", "options",
                  "trace", "api_route", "websocket", "route"}  # fmt: skip
_PARAM_FUNCS = {"Field", "Query", "Path", "Body", "Header", "Cookie", "Form",
                "File", "Param", "Security"}  # fmt: skip
_STOP = {"the", "a", "an", "this", "that", "of", "to", "for", "and", "or", "is",
         "it", "returns", "return", "get", "gets", "sets", "set", "method",
         "function", "class", "property", "value", "object", "given", "new"}  # fmt: skip


def _route_decorator(func: ast.AST) -> ast.Call | None:
    for dec in func.decorator_list:
        if (
            isinstance(dec, ast.Call)
            and isinstance(dec.func, ast.Attribute)
            and dec.func.attr in _ROUTE_METHODS
        ):
            path = dec.args[0] if dec.args else None
            if path is None:
                path = next((k.value for k in dec.keywords if k.arg == "path"), None)
            if (
                isinstance(path, ast.Constant)
                and isinstance(path.value, str)
                and (path.value == "" or path.value.startswith("/"))
            ):
                return dec
    return None


def _is_trivial(name: str, doc: str) -> bool:
    words = re.findall(r"[a-z0-9]+", doc.lower())
    if not words or len(words) > 6:
        return False
    tokens = set(
        re.findall(r"[a-z0-9]+", re.sub(r"(?<=[a-z])(?=[A-Z])", "_", name).lower())
    )
    meaningful = [w for w in words if w not in _STOP]
    return all(w in tokens or w.rstrip("s") in tokens for w in meaningful)


def _module_all(tree: ast.Module) -> set[str] | None:
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets):
                value = node.value
                if isinstance(value, (ast.List, ast.Tuple)):
                    return {
                        e.value
                        for e in value.elts
                        if isinstance(e, ast.Constant) and isinstance(e.value, str)
                    }
    return None


@dataclass
class Scope:
    start: int
    end: int
    qualname: str
    is_function: bool
    class_name: str | None
    node: ast.AST


class FileSurvey:
    def __init__(self, rel: str, source: str, tree: ast.Module, index: ProjectIndex):
        self.rel = rel
        self.source = source
        self.tree = tree
        self.index = index
        self.module = module_name(rel)
        self.private_module = any(
            p.startswith("_") and p not in ("__init__.py", "__main__.py")
            for p in Path(rel).parts
        )
        self.exported = _module_all(tree)
        self.coverage: list[dict] = []
        self.trivial: list[dict] = []
        self.issues: list[dict] = []
        self.claims: list[dict] = []
        self.suppressions: list[dict] = []
        self.styles: dict[str, int] = {}
        self.scopes: list[Scope] = []
        self.def_starts: dict[int, Scope] = {}
        self.described_fields = 0

    # -- traversal ----------------------------------------------------------

    def run(self) -> None:
        mod_doc = ast.get_docstring(self.tree)
        empty_init = Path(self.rel).name == "__init__.py" and not self.tree.body
        if not self.private_module and not empty_init:
            self._count("module", "<module>", 1, mod_doc, None)
        self._docstring_claims(self.tree, None, None)
        self._walk(self.tree.body, prefix="", class_name=None, public=not self.private_module,
                   in_function=False)  # fmt: skip
        self._description_claims()
        self._comments()

    def _walk(self, body, prefix, class_name, public, in_function) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                qual = prefix + node.name
                self._scope(node, qual, False, node.name)
                node_public = public and self._is_public(node.name, prefix, in_function)
                doc = ast.get_docstring(node)
                if node_public:
                    self._count("class", qual, node.lineno, doc, None)
                self._docstring_claims(node, None, node.name)
                if doc:
                    self.issues.extend(
                        check_class_raises(self.rel, node, qual, doc, self.index)
                    )
                self._walk(node.body, qual + ".", node.name, node_public, in_function)
            elif isinstance(node, _FUNC_TYPES):
                qual = prefix + node.name
                self._scope(node, qual, True, class_name)
                self._function(node, qual, class_name, public, in_function, prefix)
                self._walk(node.body, qual + ".<locals>.", None, False, True)
            elif isinstance(node, (ast.If, ast.Try, ast.With, ast.AsyncWith)) or (
                type(node).__name__ == "TryStar"
            ):
                for part in ("body", "orelse", "finalbody", "handlers"):
                    for child in getattr(node, part, []) or []:
                        inner = (
                            child.body
                            if isinstance(child, ast.ExceptHandler)
                            else [child]
                        )
                        self._walk(inner, prefix, class_name, public, in_function)

    def _is_public(self, name: str, prefix: str, in_function: bool) -> bool:
        if in_function or name.startswith("_"):
            return False
        if not prefix and self.exported is not None:
            return name in self.exported
        return True

    def _scope(self, node, qual, is_function, class_name) -> None:
        start = min([node.lineno] + [d.lineno for d in node.decorator_list])
        scope = Scope(
            start, node.end_lineno or node.lineno, qual, is_function, class_name, node
        )
        self.scopes.append(scope)
        self.def_starts[start] = scope

    def _function(self, node, qual, class_name, public, in_function, prefix) -> None:
        doc = ast.get_docstring(node)
        route = _route_decorator(node)
        decorators = {_name_of(d) for d in node.decorator_list}
        is_setter = any(
            isinstance(d, ast.Attribute) and d.attr in ("setter", "deleter")
            for d in node.decorator_list
        )
        if route is not None:
            described = any(
                k.arg in ("summary", "description") and k.value is not None
                for k in route.keywords
            )
            self._count("route", qual, node.lineno, doc, "decorator" if described and not doc else None,
                        documented=bool(doc) or described)  # fmt: skip
        elif (
            public
            and self._is_public(node.name, prefix, in_function)
            and "overload" not in decorators
            and not is_setter
        ):
            kind = "method" if class_name else "function"
            self._count(kind, qual, node.lineno, doc, None)
        if doc:
            self.issues.extend(check_contract(self.rel, node, qual, doc, self.index))
        self._docstring_claims(node, qual, class_name)

    def _count(self, kind, name, line, doc, documented_by, documented=None) -> None:
        if documented is None:
            documented = bool(doc and doc.strip())
        entry = {"kind": kind, "name": name, "line": line, "documented": documented}
        if documented_by:
            entry["documented_by"] = documented_by
        self.coverage.append(entry)
        if doc and doc.strip():
            style = docstring_style(doc)
            self.styles[style] = self.styles.get(style, 0) + 1
            short = name.rsplit(".", 1)[-1]
            if kind != "module" and _is_trivial(short, doc):
                self.trivial.append(
                    {
                        "file": self.rel,
                        "line": line,
                        "kind": kind,
                        "name": name,
                        "docstring": doc.strip()[:120],
                    }
                )

    # -- claims -------------------------------------------------------------

    def _owner(self, qualname: str | None, class_name: str | None) -> dict:
        out: dict = {"function": qualname, "class": class_name}
        out["function_key"] = f"{self.module}:{qualname}" if qualname else None
        out["class_key"] = f"{self.module}:{class_name}" if class_name else None
        return out

    def _add_claims(self, found, source, qualname, class_name, node=None) -> None:
        for claim in found:
            entry = {"file": self.rel, "line": claim["line"], "source": source}
            entry.update(self._owner(qualname, class_name))
            entry.update(claim)
            if claim["category"] == "concurrency":
                # None: no function body to look at (a class or module docstring).
                entry["acquires_directly"] = (
                    _locks_acquired(node) if node is not None else None
                )
            self.claims.append(entry)

    def _docstring_claims(self, node, qualname, class_name) -> None:
        body = getattr(node, "body", None)
        if not body or not isinstance(body[0], ast.Expr):
            return
        const = body[0].value
        if not (isinstance(const, ast.Constant) and isinstance(const.value, str)):
            return
        lines = [
            (const.lineno + i, text) for i, text in enumerate(const.value.splitlines())
        ]
        func = node if isinstance(node, _FUNC_TYPES) else None
        self._add_claims(find_claims(lines, invariants=True), "docstring", qualname,
                         class_name, func)  # fmt: skip

    def _description_claims(self) -> None:
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Call):
                continue
            name = _name_of(node.func)
            for kw in node.keywords:
                if not (
                    isinstance(kw.value, ast.Constant)
                    and isinstance(kw.value.value, str)
                ):
                    continue
                if name in _PARAM_FUNCS and kw.arg == "description":
                    source = "field_description"
                    self.described_fields += 1
                elif (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr in _ROUTE_METHODS
                    and kw.arg in ("summary", "description")
                ):
                    source = "route_description"
                else:
                    continue
                lines = [
                    (kw.value.lineno + i, t)
                    for i, t in enumerate(kw.value.value.splitlines())
                ]
                scope = self._innermost(kw.value.lineno)
                qual = scope.qualname if scope and scope.is_function else None
                cls = scope.class_name if scope else None
                self._add_claims(find_claims(lines, invariants=True), source, qual, cls)

    def _innermost(self, line: int) -> Scope | None:
        best = None
        for scope in self.scopes:
            if scope.start <= line <= scope.end and (
                best is None or scope.start >= best.start
            ):
                best = scope
        return best

    def _comments(self) -> None:
        try:
            tokens = list(tokenize.generate_tokens(io.StringIO(self.source).readline))
        except (tokenize.TokenError, IndentationError, SyntaxError):
            return
        lines = self.source.splitlines()
        blocks: list[list[tuple[int, str]]] = []
        prev = -2
        for tok in tokens:
            if tok.type != tokenize.COMMENT:
                continue
            line = tok.start[0]
            text = tok.string.lstrip("#").strip()
            for item in parse_suppressions(tok.string):
                code = (
                    lines[line - 1][: tok.start[1]].strip()
                    if line <= len(lines)
                    else ""
                )
                scope = self._innermost(line)
                entry = {"file": self.rel, "line": line, **item, "code": code[:160]}
                entry["scope"] = self._suppression_scope(item, tok.string, line, code)
                entry["function"] = (
                    scope.qualname if scope and scope.is_function else None
                )
                self.suppressions.append(entry)
            if any(pattern.search(tok.string) for _t, pattern in _SUPPRESSIONS):
                prev = -2
                continue
            standalone = line <= len(lines) and lines[line - 1].lstrip().startswith("#")
            if standalone and blocks and prev == line - 1:
                blocks[-1].append((line, text))
            else:
                blocks.append([(line, text)])
            prev = line if standalone else -2
        for block in blocks:
            scope = self._comment_owner(block[-1][0], lines)
            qual = scope.qualname if scope and scope.is_function else None
            cls = scope.class_name if scope else None
            node = scope.node if scope and scope.is_function else None
            self._add_claims(
                find_claims(block, invariants=False), "comment", qual, cls, node
            )

    def _suppression_scope(self, item: dict, comment: str, line: int, code: str) -> str:
        """``line`` (applies to its own line), ``file`` (a file-level form) or
        ``standalone`` (a comment on a line of its own, which most tools ignore:
        it suppresses nothing)."""
        if code:
            return "line"
        if item["tool"] == "ruff/flake8" and re.search(
            r"\b(?:ruff|flake8)\s*:\s*noqa", comment, re.IGNORECASE
        ):
            return "file"
        first = self.tree.body[0].lineno if self.tree.body else 10**9
        if item["tool"] == "mypy" and line < first:
            return "file"
        if item["tool"] in ("pylint", "coverage"):
            return "line"  # these tools do honour a comment on its own line
        return "standalone"

    def _comment_owner(self, last_line: int, lines: list[str]) -> Scope | None:
        """The def a comment block sits in, or the def it sits directly above."""
        nxt = last_line
        while nxt < len(lines) and not lines[nxt].strip():
            nxt += 1
        if nxt + 1 in self.def_starts:
            above = self.def_starts[nxt + 1]
            inside = self._innermost(last_line)
            if inside is None or inside.start <= above.start:
                return above
        return self._innermost(last_line)


_LOCKISH = re.compile(
    r"lock|mutex|sem(?:aphore)?\b|cond(?:ition)?\b|_cv\b", re.IGNORECASE
)


def _locks_acquired(func: ast.AST | None) -> list[str]:
    """Lock-like objects the function's own body enters or acquires (syntactic)."""
    if func is None:
        return []
    out: list[str] = []
    for node in iter_own(func):
        exprs = []
        if isinstance(node, (ast.With, ast.AsyncWith)):
            exprs = [item.context_expr for item in node.items]
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "acquire"
        ):
            exprs = [node.func.value]
        for expr in exprs:
            text = ast.unparse(expr)
            if _LOCKISH.search(text) and text not in out:
                out.append(text)
    return out


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------


def _summarize_coverage(surveys: list[FileSurvey]) -> dict:
    totals: dict[str, dict] = {}
    per_file = []
    undocumented = []
    trivial = []
    for s in surveys:
        documented = total = 0
        mod_doc = None
        for entry in s.coverage:
            bucket = totals.setdefault(entry["kind"], {"documented": 0, "total": 0})
            bucket["total"] += 1
            if entry["documented"]:
                bucket["documented"] += 1
            if entry["kind"] == "module":
                mod_doc = entry["documented"]
                if not entry["documented"]:
                    undocumented.append(
                        {"file": s.rel, "line": 1, "kind": "module", "name": s.module}
                    )
                continue
            total += 1
            documented += entry["documented"]
            if not entry["documented"]:
                undocumented.append(
                    {
                        "file": s.rel,
                        "line": entry["line"],
                        "kind": entry["kind"],
                        "name": entry["name"],
                    }
                )
        trivial.extend(s.trivial)
        per_file.append(
            {
                "file": s.rel,
                "module_docstring": mod_doc,
                "documented": documented,
                "total": total,
            }
        )
    for bucket in totals.values():
        bucket["percent"] = (
            round(100 * bucket["documented"] / bucket["total"], 1)
            if bucket["total"]
            else None
        )
    return {
        "totals": dict(sorted(totals.items())),
        "per_file": per_file,
        "undocumented": undocumented,
        "trivial": trivial,
    }


def analyze(target: str, *, max_files: int = 0, include_tests: bool = False) -> dict:
    """Survey docstrings under *target* (a directory or a file)."""
    _resolved, project_root, scan_root = resolve_target(target)
    files, files_total = collect_python_files(scan_root, max_files)
    skipped_tests: list[str] = []
    unparseable: list[str] = []
    parsed: list[tuple[str, str, ast.Module]] = []
    for path in files:
        rel = relative_to_root(path, project_root)
        if not include_tests and is_test_file(rel):
            skipped_tests.append(rel)
            continue
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source)
        except (OSError, SyntaxError, ValueError, RecursionError):
            unparseable.append(rel)
            continue
        parsed.append((rel, source, tree))

    index = ProjectIndex()
    for _rel, _source, tree in parsed:
        index.add_module(tree)
    surveys = []
    for rel, source, tree in parsed:
        survey = FileSurvey(rel, source, tree, index)
        survey.run()
        surveys.append(survey)

    issues = [i for s in surveys for i in s.issues]
    claims = [c for s in surveys for c in s.claims]
    suppressions = [x for s in surveys for x in s.suppressions]
    styles: dict[str, int] = {}
    for s in surveys:
        for style, n in s.styles.items():
            styles[style] = styles.get(style, 0) + n

    def tally(items, key):
        out: dict[str, int] = {}
        for item in items:
            out[item[key]] = out.get(item[key], 0) + 1
        return dict(sorted(out.items()))

    coverage = _summarize_coverage(surveys)
    result = build_envelope(project_root, scan_root, files_total, len(files))
    result["summary"] = {
        "files_surveyed": len(surveys),
        "coverage": {
            k: f"{v['documented']}/{v['total']}" for k, v in coverage["totals"].items()
        },
        "docstring_styles": dict(sorted(styles.items())),
        "contract_issues": tally(issues, "kind"),
        "contract_issues_by_confidence": tally(issues, "confidence"),
        "claims": tally(claims, "category"),
        "claims_by_source": tally(claims, "source"),
        "suppressions": tally(suppressions, "tool"),
        "blanket_suppressions": sum(1 for x in suppressions if x["blanket"]),
        "described_fields": sum(s.described_fields for s in surveys),
    }
    result["coverage"] = coverage
    result["contract_issues"] = issues
    result["claims"] = claims
    result["suppressions"] = suppressions
    result["skipped"] = {
        "test_files": len(skipped_tests),
        "include_tests": include_tests,
        "unparseable": unparseable,
    }
    return result


def main() -> None:
    argv = sys.argv[1:]
    target, max_files = parse_common_args(argv, __doc__)
    emit(analyze(target, max_files=max_files, include_tests="--include-tests" in argv))


if __name__ == "__main__":
    main()
