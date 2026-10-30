#!/usr/bin/env python3
"""Map where exceptions and error codes are produced and where they are consumed.

Backs the producer/consumer diff of the pattern-consistency-checker: "which
exceptions are raised where, and which handlers catch them", and "which error
codes are produced, and which mappings (status maps, client error classes)
know about them". A code produced but never mapped, or mapped but never
produced, is a concrete lead.

Exceptions:

- ``classes``: the project's exception classes with their ancestors (project
  and built-in), so a handler for a base class counts as catching a subclass;
- ``raise_sites``: every ``raise`` of a named class, its error code if it
  carries one, whether it is chained, and whether it escapes its function or
  a ``try`` in the same function catches it (``caught_locally_at``);
- ``exceptions``: per class name, where it is raised and which handlers catch
  it (directly or through an ancestor). Broad handlers (``Exception``, bare)
  are counted in the summary, not listed per class;
- leads: project classes caught but never raised in scope, and project
  classes raised but never caught by a specific handler.

Error codes (string literals that look like identifiers: ``not_found``,
``E_TIMEOUT``, ``auth.expired``):

- produced by ``raise SomeError("code", ...)`` when the class takes a code
  first (its ``__init__`` names the parameter ``code``, or every literal first
  argument in scope looks like a code), by ``code=`` keywords, by a ``code =``
  class attribute of a raised class, and by dict literals ``{"code": ...}``;
- consumed by dict literals keyed by codes (status maps, error-class maps),
  by comparisons against an expression that names a code (``code == "x"``,
  ``err.code in ("a", "b")``, ``match result["code"]``), and by prefix,
  suffix and substring tests on one (``code.endswith("_not_found")``).

The code keys default to ``code``, ``error_code``, ``error_type`` and
``err_code``; add more with ``--code-key NAME``.

Limits: classes are matched by simple name, so two classes with one name in
different modules merge. Propagation is lexical within one function: a code
raised in a helper and caught by its caller still shows as escaping the
helper.

Usage:
    python map_exceptions.py [path] [--max-files N] [--code-key NAME ...]
"""

from __future__ import annotations

import ast
import builtins
import re
import sys
from collections import defaultdict
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

DEFAULT_CODE_KEYS = ("code", "error_code", "error_type", "err_code")
CODE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*(?:[_.\-][A-Za-z0-9]+)*$")
MAX_CODE_LENGTH = 64
BROAD_NAMES = frozenset({"Exception", "BaseException"})
_NESTED = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _builtin_ancestors(name: str) -> list[str] | None:
    """The MRO of a built-in exception class by name, or None if not one."""
    obj = getattr(builtins, name, None)
    if isinstance(obj, type) and issubclass(obj, BaseException):
        return [c.__name__ for c in obj.__mro__[1:] if c is not object]
    return None


def _last_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _looks_like_class(name: str | None) -> bool:
    return bool(name) and name.lstrip("_")[:1].isupper()


def _is_code(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= MAX_CODE_LENGTH
        and CODE_RE.match(value) is not None
    )


def _str_constants(node: ast.AST) -> list[str]:
    """String constants of a constant or a tuple/list/set of them."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return [
            e.value
            for e in node.elts
            if isinstance(e, ast.Constant) and isinstance(e.value, str)
        ]
    return []


class _FileScan(ast.NodeVisitor):
    """Collect the raw facts of one file; the cross-file joins come later."""

    def __init__(self, rel: str, code_keys: frozenset[str]) -> None:
        self.rel = rel
        self.module = rel[:-3].replace("/", ".") if rel.endswith(".py") else rel
        self.code_keys = code_keys
        self.scope: list[str] = []
        # Handlers protecting the current point: one entry per enclosing try
        # body in this function, holding (handler line, caught names).
        self.protecting: list[list[tuple[int, list[str]]]] = []
        self.classes: list[dict] = []
        self.raises: list[dict] = []
        self.handlers: list[dict] = []
        self.produced: list[dict] = []
        self.consumers: list[dict] = []
        self.dicts: list[dict] = []
        self.constructed: list[dict] = []
        self._assign_names: dict[int, str] = {}
        self._raised_calls: set[int] = set()

    def _where(self, node: ast.AST) -> dict:
        return {
            "file": self.rel,
            "line": node.lineno,
            "function": ".".join(self.scope) or "<module>",
        }

    def _names_code(self, node: ast.AST) -> bool:
        """True when *node* is an expression that names an error code."""
        if isinstance(node, ast.Name):
            return node.id in self.code_keys
        if isinstance(node, ast.Attribute):
            return node.attr in self.code_keys
        if isinstance(node, ast.Subscript):
            key = node.slice
            return isinstance(key, ast.Constant) and key.value in self.code_keys
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
        ):
            key = node.args[0]
            return isinstance(key, ast.Constant) and key.value in self.code_keys
        if isinstance(node, ast.Call) and _last_name(node.func) == "str" and node.args:
            return self._names_code(node.args[0])
        return False

    # -- scopes ------------------------------------------------------------

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.scope.append(node.name)
        saved, self.protecting = self.protecting, []
        self.generic_visit(node)
        self.protecting = saved
        self.scope.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def visit_Lambda(self, node: ast.Lambda) -> None:
        saved, self.protecting = self.protecting, []
        self.generic_visit(node)
        self.protecting = saved

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        init_param = None
        code_attr = None
        for stmt in node.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                stmt.name == "__init__"
            ):
                params = [*stmt.args.posonlyargs, *stmt.args.args][1:]
                init_param = params[0].arg if params else None
            targets = []
            if isinstance(stmt, ast.Assign):
                targets = stmt.targets
            elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
                targets = [stmt.target]
            for target in targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id in self.code_keys
                    and isinstance(stmt.value, ast.Constant)
                    and _is_code(stmt.value.value)
                ):
                    code_attr = stmt.value.value
        record = self._where(node)
        record.update(
            {
                "name": node.name,
                "module": self.module,
                "bases": [b for b in (_last_name(base) for base in node.bases) if b],
                "init_first_param": init_param,
                "code": code_attr,
            }
        )
        self.classes.append(record)
        self.scope.append(node.name)
        saved, self.protecting = self.protecting, []
        self.generic_visit(node)
        self.protecting = saved
        self.scope.pop()

    # -- exceptions --------------------------------------------------------

    def _visit_try(self, node: ast.Try) -> None:
        caught_by = []
        for handler in node.handlers:
            names = self._caught(handler.type)
            caught_by.append((handler.lineno, names))
            record = self._where(handler)
            record["caught"] = names
            record["broad"] = not names or any(n in BROAD_NAMES for n in names)
            self.handlers.append(record)
        self.protecting.append(caught_by)
        for stmt in node.body:
            self.visit(stmt)
        self.protecting.pop()
        for handler in node.handlers:
            self.visit(handler)
        for stmt in (*node.orelse, *node.finalbody):
            self.visit(stmt)

    visit_Try = _visit_try
    visit_TryStar = _visit_try

    @staticmethod
    def _caught(type_node: ast.AST | None) -> list[str]:
        if type_node is None:
            return []
        elts = type_node.elts if isinstance(type_node, ast.Tuple) else [type_node]
        return [n for n in (_last_name(e) for e in elts) if n]

    def visit_Raise(self, node: ast.Raise) -> None:
        if node.exc is not None:
            call = node.exc if isinstance(node.exc, ast.Call) else None
            target = call.func if call else node.exc
            name = _last_name(target)
            if call is not None:
                self._raised_calls.add(id(call))
            if _looks_like_class(name):
                record = self._where(node)
                first = None
                if call and call.args and isinstance(call.args[0], ast.Constant):
                    first = call.args[0].value
                keyword_code = None
                if call:
                    for kw in call.keywords:
                        if (
                            kw.arg in self.code_keys
                            and isinstance(kw.value, ast.Constant)
                            and _is_code(kw.value.value)
                        ):
                            keyword_code = kw.value.value
                record.update(
                    {
                        "exception": name,
                        "first_arg": first if isinstance(first, str) else None,
                        "keyword_code": keyword_code,
                        "chained": node.cause is not None
                        and not (
                            isinstance(node.cause, ast.Constant)
                            and node.cause.value is None
                        ),
                        "protecting": [list(level) for level in self.protecting],
                    }
                )
                self.raises.append(record)
        self.generic_visit(node)

    # -- codes -------------------------------------------------------------

    def visit_Assign(self, node: ast.Assign) -> None:
        if isinstance(node.value, ast.Dict) and len(node.targets) == 1:
            self._assign_names[id(node.value)] = ast.unparse(node.targets[0])
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.value, ast.Dict):
            self._assign_names[id(node.value)] = ast.unparse(node.target)
        self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict) -> None:
        keys = [k.value for k in node.keys if isinstance(k, ast.Constant)]
        for key, value in zip(node.keys, node.values):
            if (
                isinstance(key, ast.Constant)
                and key.value in self.code_keys
                and isinstance(value, ast.Constant)
                and _is_code(value.value)
            ):
                record = self._where(node)
                record.update({"code": value.value, "via": f"dict key {key.value!r}"})
                self.produced.append(record)
        str_keys = [k for k in keys if isinstance(k, str)]
        if len(str_keys) >= 2 and len(str_keys) == len(node.keys):
            record = self._where(node)
            record.update(
                {
                    "name": self._assign_names.get(id(node)),
                    "keys": str_keys,
                    "values": sorted({type(v).__name__ for v in node.values}),
                }
            )
            self.dicts.append(record)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = _last_name(node.func)
        if id(node) not in self._raised_calls and _looks_like_class(name):
            # Built outside a raise: a factory that returns the exception, or
            # one stored and raised later. Filtered to exception classes later.
            record = self._where(node)
            record["exception"] = name
            self.constructed.append(record)
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and len(node.args) == 2
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value in self.code_keys
            and isinstance(node.args[1], ast.Constant)
            and _is_code(node.args[1].value)
        ):
            record = self._where(node)
            record.update(
                {
                    "code": node.args[1].value,
                    "via": f".get({node.args[0].value!r}) default",
                }
            )
            self.produced.append(record)
        for kw in node.keywords:
            if (
                kw.arg in self.code_keys
                and isinstance(kw.value, ast.Constant)
                and _is_code(kw.value.value)
            ):
                record = self._where(node)
                record.update({"code": kw.value.value, "via": f"keyword {kw.arg}="})
                self.produced.append(record)
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr in ("startswith", "endswith")
            and node.args
            and self._names_code(func.value)
        ):
            for literal in _str_constants(node.args[0]):
                self._consume(node, literal, func.attr)
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        operands = [node.left, *node.comparators]
        for op, left, right in zip(node.ops, operands, operands[1:]):
            if isinstance(op, (ast.Eq, ast.NotEq, ast.In, ast.NotIn)):
                if self._names_code(left):
                    for literal in _str_constants(right):
                        self._consume(node, literal, "equals")
                elif self._names_code(right):
                    match = (
                        "contains" if isinstance(op, (ast.In, ast.NotIn)) else "equals"
                    )
                    for literal in _str_constants(left):
                        self._consume(node, literal, match)
        self.generic_visit(node)

    def visit_Match(self, node: ast.Match) -> None:
        if self._names_code(node.subject):
            for case in node.cases:
                for pattern in ast.walk(case.pattern):
                    if isinstance(pattern, ast.MatchValue):
                        for literal in _str_constants(pattern.value):
                            self._consume(pattern, literal, "equals")
        self.generic_visit(node)

    def _consume(self, node: ast.AST, literal: str, match: str) -> None:
        if match == "equals" and not _is_code(literal):
            return
        record = self._where(node)
        record.update({"literal": literal, "match": match})
        self.consumers.append(record)


def _loc(record: dict) -> str:
    return f"{record['file']}:{record['line']}"


def _ancestry(classes: dict[str, dict]) -> dict[str, list[str]]:
    """Ancestors of every project exception class, project ones first."""
    memo: dict[str, list[str] | None] = {}

    def resolve(name: str, seen: frozenset[str]) -> list[str] | None:
        if name in memo:
            return memo[name]
        if name in seen:
            return None
        cls = classes.get(name)
        if cls is None:
            return None
        result: list[str] | None = None
        for base in cls["bases"]:
            builtin_mro = _builtin_ancestors(base)
            if builtin_mro is not None:
                chain = [base, *builtin_mro]
            else:
                parent = resolve(base, seen | {name})
                if parent is None:
                    continue
                chain = [base, *parent]
            result = (result or []) + [c for c in chain if c not in (result or [])]
        memo[name] = result
        return result

    out = {}
    for name in classes:
        chain = resolve(name, frozenset())
        if chain is not None:
            out[name] = chain
    return out


def _all_ancestors(name: str, project: dict[str, list[str]]) -> list[str]:
    if name in project:
        return project[name]
    return _builtin_ancestors(name) or []


def analyze(
    target: str, *, max_files: int = 0, code_keys: list[str] | None = None
) -> dict:
    """Map exceptions and error codes under *target*."""
    keys = frozenset(DEFAULT_CODE_KEYS) | frozenset(code_keys or ())
    resolved, project_root, scan_root = resolve_target(target)
    files, files_total = collect_python_files(resolved, max_files)
    scans: list[_FileScan] = []
    unparsed: list[str] = []
    for path in files:
        rel = relative_to_root(path, project_root)
        tree = parse_source(path)
        if tree is None:
            unparsed.append(rel)
            continue
        scan = _FileScan(rel, keys)
        scan.visit(tree)
        scans.append(scan)

    all_classes = {c["name"]: c for s in scans for c in s.classes}
    ancestry = _ancestry(all_classes)
    raises = [r for s in scans for r in s.raises]
    handlers = [h for s in scans for h in s.handlers]

    # A class carries a code as its first argument when its __init__ says
    # so, or when every literal first argument it is raised with in scope
    # looks like a code (two or more sites).
    first_args: dict[str, list[str]] = defaultdict(list)
    for r in raises:
        if r["first_arg"] is not None:
            first_args[r["exception"]].append(r["first_arg"])

    def init_param(name: str) -> str | None:
        for cls_name in [name, *ancestry.get(name, [])]:
            cls = all_classes.get(cls_name)
            if cls and cls["init_first_param"]:
                return cls["init_first_param"]
        return None

    def code_bearing(name: str) -> bool:
        if name not in ancestry:
            return False
        param = init_param(name)
        if param is not None:
            return param in keys
        literals = first_args.get(name, [])
        return len(literals) >= 2 and all(_is_code(v) for v in literals)

    def class_code(name: str) -> str | None:
        for cls_name in [name, *ancestry.get(name, [])]:
            cls = all_classes.get(cls_name)
            if cls and cls["code"]:
                return cls["code"]
        return None

    produced: list[dict] = [p for s in scans for p in s.produced]
    raise_sites = []
    for r in raises:
        name = r["exception"]
        lineage = {name, *_all_ancestors(name, ancestry)}
        caught_at = None
        for level in reversed(r["protecting"]):
            for line, names in level:
                if not names or set(names) & (lineage | BROAD_NAMES):
                    caught_at = line
                    break
            if caught_at is not None:
                break
        code = r["keyword_code"]
        via = "keyword"
        if code is None and r["first_arg"] is not None and code_bearing(name):
            code, via = (
                (r["first_arg"], "first argument")
                if _is_code(r["first_arg"])
                else (None, None)
            )
        if code is None and class_code(name):
            code, via = class_code(name), "class attribute"
        site = {
            "file": r["file"],
            "line": r["line"],
            "function": r["function"],
            "exception": name,
            "project_defined": name in ancestry,
            "code": code,
            "chained": r["chained"],
            "escapes_function": caught_at is None,
            "caught_locally_at": caught_at,
        }
        raise_sites.append(site)
        if code is not None:
            produced.append(
                {
                    "file": r["file"],
                    "line": r["line"],
                    "function": r["function"],
                    "code": code,
                    "via": f"raise {name} ({via})",
                }
            )

    # Exceptions: raised where, caught where.
    exceptions: dict[str, dict] = {}

    def entry(name: str) -> dict:
        return exceptions.setdefault(
            name,
            {
                "project_defined": name in ancestry,
                "ancestors": _all_ancestors(name, ancestry),
                "raised_at": [],
                "caught_at": [],
            },
        )

    for site in raise_sites:
        entry(site["exception"])["raised_at"].append(_loc(site))
    constructed_names: set[str] = set()
    for record in (c for s in scans for c in s.constructed):
        if record["exception"] in ancestry:
            constructed_names.add(record["exception"])
            entry(record["exception"]).setdefault("constructed_at", []).append(
                _loc(record)
            )
    for h in handlers:
        for caught in h["caught"]:
            if caught not in BROAD_NAMES:
                entry(caught)
    for name, info in exceptions.items():
        lineage = [name, *info["ancestors"]]
        for h in handlers:
            via = next(
                (c for c in h["caught"] if c in lineage and c not in BROAD_NAMES),
                None,
            )
            if via is not None:
                info["caught_at"].append(
                    {"at": _loc(h), "via": via} if via != name else {"at": _loc(h)}
                )

    # A class built by a factory and raised by its caller counts as raised.
    raised_names = {s["exception"] for s in raise_sites} | constructed_names

    def raised_or_subclass_raised(name: str) -> bool:
        return any(
            r == name or name in _all_ancestors(r, ancestry) for r in raised_names
        )

    caught_never_raised = sorted(
        name
        for name, info in exceptions.items()
        if info["project_defined"]
        and info["caught_at"]
        and not raised_or_subclass_raised(name)
    )
    raised_never_caught = sorted(
        name
        for name, info in exceptions.items()
        if info["project_defined"] and info["raised_at"] and not info["caught_at"]
    )

    # Codes: produced vs consumed.
    produced_codes: dict[str, list[dict]] = defaultdict(list)
    for p in produced:
        produced_codes[p["code"]].append(p)
    consumers = [c for s in scans for c in s.consumers]
    mappings = []
    for d in (d for s in scans for d in s.dicts):
        # A mapping is keyed mostly by produced codes. A result dict that
        # happens to share one key with a code ({"ok": ..., "failed": 2}) is not.
        known = [k for k in d["keys"] if k in produced_codes]
        if not known or 2 * len(known) < len(d["keys"]):
            continue
        mapping = dict(d)
        mapping["keys_not_produced"] = [k for k in d["keys"] if k not in produced_codes]
        mappings.append(mapping)
        for key in d["keys"]:
            consumers.append(
                {
                    "file": d["file"],
                    "line": d["line"],
                    "function": d["function"],
                    "literal": key,
                    "match": "mapping key",
                }
            )

    def consumed_by(code: str) -> list[dict]:
        hits = []
        for c in consumers:
            lit, match = c["literal"], c["match"]
            if (
                (match in ("equals", "mapping key") and lit == code)
                or (match == "startswith" and code.startswith(lit))
                or (match == "endswith" and code.endswith(lit))
                or (match == "contains" and lit in code)
            ):
                hits.append(c)
        return hits

    codes = {}
    for code in sorted(produced_codes):
        hits = consumed_by(code)
        codes[code] = {
            "produced_at": [
                {"at": _loc(p), "function": p["function"], "via": p["via"]}
                for p in produced_codes[code]
            ],
            "consumed_at": [
                {"at": _loc(c), "match": c["match"], "literal": c["literal"]}
                if c["literal"] != code
                else {"at": _loc(c), "match": c["match"]}
                for c in hits
            ],
        }
    exact_consumed = {
        c["literal"]
        for c in consumers
        if c["match"] in ("equals", "mapping key") and _is_code(c["literal"])
    }
    consumed_not_produced = sorted(exact_consumed - set(produced_codes))

    by_module: dict[str, dict[str, set[str]]] = defaultdict(
        lambda: {"produced": set(), "consumed": set()}
    )
    for p in produced:
        by_module[p["file"]]["produced"].add(p["code"])
    for c in consumers:
        by_module[c["file"]]["consumed"].add(c["literal"])

    output = build_envelope(project_root, scan_root, files_total, len(files))
    output["files_unparsed"] = unparsed
    output["code_keys"] = sorted(keys)
    output["summary"] = {
        "project_exception_classes": len(ancestry),
        "raise_sites": len(raise_sites),
        "raise_sites_escaping_function": sum(
            1 for s in raise_sites if s["escapes_function"]
        ),
        "handlers": len(handlers),
        "broad_handlers": sum(1 for h in handlers if h["broad"]),
        "caught_never_raised": len(caught_never_raised),
        "raised_never_caught": len(raised_never_caught),
        "codes_produced": len(produced_codes),
        "codes_produced_not_consumed": sum(
            1 for info in codes.values() if not info["consumed_at"]
        ),
        "codes_consumed_not_produced": len(consumed_not_produced),
        "mappings": len(mappings),
    }
    output["classes"] = {
        name: {
            "file": all_classes[name]["file"],
            "line": all_classes[name]["line"],
            "bases": all_classes[name]["bases"],
            "ancestors": ancestry[name],
            "code": class_code(name),
            "code_first_arg": code_bearing(name),
        }
        for name in sorted(ancestry)
    }
    output["exceptions"] = dict(sorted(exceptions.items()))
    output["raise_sites"] = raise_sites
    output["leads"] = {
        "caught_never_raised": caught_never_raised,
        "raised_never_caught": raised_never_caught,
        "codes_produced_not_consumed": [
            code for code, info in codes.items() if not info["consumed_at"]
        ],
        "codes_consumed_not_produced": consumed_not_produced,
    }
    output["codes"] = codes
    output["mappings"] = mappings
    output["by_module"] = {
        module: {k: sorted(v) for k, v in sides.items()}
        for module, sides in sorted(by_module.items())
    }
    return output


def _extract_code_keys(argv: list[str]) -> tuple[list[str], list[str]]:
    rest: list[str] = []
    keys: list[str] = []
    i = 0
    while i < len(argv):
        if argv[i] == "--code-key" and i + 1 < len(argv):
            keys.append(argv[i + 1])
            i += 2
        else:
            rest.append(argv[i])
            i += 1
    return rest, keys


def main() -> None:
    argv, code_keys = _extract_code_keys(sys.argv[1:])
    target, max_files = parse_common_args(argv)
    emit(analyze(target, max_files=max_files, code_keys=code_keys))


if __name__ == "__main__":
    main()
