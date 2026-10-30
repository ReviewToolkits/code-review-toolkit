#!/usr/bin/env python3
"""Survey coding conventions and quantify how far each one diverges.

Backs the consistency-auditor's "discover conventions, then quantify divergence"
phase. For each category it counts every variant, says which one is the
majority, how many files mix variants, and lists the sites of the minority
variants (the candidates for "module B does it differently"):

- ``string_formatting``: f-string / ``str.format`` / ``%`` / ``+`` concatenation
  (logging messages are counted under ``logging_message`` instead).
- ``logging_message``: how logger calls build their message -- lazy ``%`` args,
  f-string, ``%`` operator, ``.format``, concatenation, constant, or
  ``prebuilt`` (a variable or call holding a message built earlier).
- ``logger_acquisition``: ``getLogger(__name__)`` vs a literal name vs root.
- ``generic_style``: ``typing.List[...]`` and friends vs ``list[...]``.
- ``union_style``: ``Optional[...]`` / ``Union[...]`` vs ``X | Y``.
- ``annotation_coverage``: per function, fully / partially / not annotated.
- ``docstring_style``: Google / NumPy / Sphinx / mixed, among docstrings that
  have sections at all (one-line docstrings are counted under ``extra``).
- ``exits``: ``sys.exit`` / ``exit()`` / ``raise SystemExit`` / ``os._exit`` /
  a framework's exit (``raise typer.Exit``, ``ctx.exit()``) -- every site.
- ``output``: ``print`` to stdout / ``print(file=sys.stderr)`` /
  ``sys.stdout.write`` / a rich-style ``console.print`` -- every site.

Test files are skipped unless ``--include-tests`` is given. Unparseable files
are listed under ``skipped``.

Usage:
    python survey_conventions.py [path] [--max-files N] [--include-tests]
"""

from __future__ import annotations

import ast
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
from survey_docstrings import docstring_style, is_test_file  # noqa: E402

_FUNC_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)
_LOG_METHODS = {"debug", "info", "warning", "warn", "error", "exception",
                "critical", "fatal", "log"}  # fmt: skip
_LEGACY_GENERICS = {"List", "Dict", "Tuple", "Set", "FrozenSet", "Type",
                    "DefaultDict", "Deque", "Counter", "OrderedDict", "ChainMap"}  # fmt: skip
_BUILTIN_GENERICS = {"list", "dict", "tuple", "set", "frozenset", "type"}
_SITE_CAP = 100

# Categories whose every site matters (an inventory), not just the minority's.
_INVENTORY = {"exits", "output"}


def _name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    return ""


def _is_str(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _is_logger(receiver: ast.AST) -> bool:
    name = _dotted(receiver).lower()
    last = name.rsplit(".", 1)[-1]
    return last in ("logging", "log", "_log", "logger", "_logger") or last.endswith(
        ("logger", "_log")
    )


def string_kind(node: ast.AST) -> str | None:
    """The formatting variant *node* is, or None when it is not one."""
    if isinstance(node, ast.JoinedStr):
        if any(isinstance(v, ast.FormattedValue) for v in node.values):
            return "fstring"
        return None
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "format"
        and _is_str(node.func.value)
    ):
        return "format_method"
    if (
        isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Mod)
        and _is_str(node.left)
    ):
        return "percent"
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        sides = (node.left, node.right)
        literal = any(_is_str(s) or isinstance(s, ast.JoinedStr) for s in sides)
        dynamic = any(not _is_str(s) for s in sides)
        if literal and dynamic:
            return "concat"
    return None


def _message_kind(call: ast.Call, msg: ast.AST, extra_args: int) -> str:
    kind = string_kind(msg)
    if kind == "percent":
        return "percent_operator"
    if kind:
        return kind
    if _is_str(msg):
        return "lazy_percent_args" if extra_args else "constant"
    return "prebuilt"  # a variable or call: the message was built earlier


def _annotation_nodes(tree: ast.Module):
    """Every annotation expression in the module (string annotations parsed)."""
    for node in ast.walk(tree):
        anns: list[ast.AST | None] = []
        if isinstance(node, _FUNC_TYPES):
            args = node.args
            for a in args.posonlyargs + args.args + args.kwonlyargs:
                anns.append(a.annotation)
            for a in (args.vararg, args.kwarg):
                if a is not None:
                    anns.append(a.annotation)
            anns.append(node.returns)
        elif isinstance(node, ast.AnnAssign):
            anns.append(node.annotation)
        for ann in anns:
            if ann is None:
                continue
            if _is_str(ann):
                try:
                    ann = ast.parse(ann.value, mode="eval").body
                except SyntaxError:
                    continue
            yield ann


class Survey:
    def __init__(self) -> None:
        self.sites: dict[str, dict[str, list[tuple[str, int, str]]]] = {}
        self.extra: dict[str, dict[str, int]] = {}

    def add(self, category: str, variant: str, rel: str, line: int, code: str) -> None:
        self.sites.setdefault(category, {}).setdefault(variant, []).append(
            (rel, line, code)
        )

    def bump(self, category: str, key: str) -> None:
        bucket = self.extra.setdefault(category, {})
        bucket[key] = bucket.get(key, 0) + 1

    # -- per file -----------------------------------------------------------

    def scan(self, rel: str, source: str, tree: ast.Module) -> None:
        lines = source.splitlines()

        def code(node: ast.AST) -> str:
            line = getattr(node, "lineno", 0)
            return lines[line - 1].strip()[:120] if 0 < line <= len(lines) else ""

        def add(category: str, variant: str, node: ast.AST) -> None:
            self.add(category, variant, rel, getattr(node, "lineno", 0), code(node))

        skip: set[int] = set()  # nodes counted elsewhere (log messages, specs)
        for node in ast.walk(tree):
            if isinstance(node, ast.FormattedValue) and node.format_spec is not None:
                skip.add(id(node.format_spec))
            if isinstance(node, ast.Call):
                self._call(node, add, skip)
            elif isinstance(node, ast.Raise) and node.exc is not None:
                self._raise(node, add)
        for node in ast.walk(tree):
            if id(node) in skip:
                # A skipped expression's own operands are not new sites.
                for child in ast.iter_child_nodes(node):
                    skip.add(id(child))
                continue
            kind = string_kind(node)
            if kind:
                add("string_formatting", kind, node)
                for child in ast.walk(node):
                    if child is not node:
                        skip.add(id(child))
        self._annotations(tree, add)
        self._functions(tree, add)

    def _call(self, node: ast.Call, add, skip: set[int]) -> None:
        func = node.func
        dotted = _dotted(func)
        if (
            isinstance(func, ast.Attribute)
            and func.attr in _LOG_METHODS
            and _is_logger(func.value)
        ):
            args = node.args[1:] if func.attr == "log" else node.args
            if args:
                msg = args[0]
                add("logging_message", _message_kind(node, msg, len(args) - 1), node)
                skip.add(id(msg))
            return
        if _name(func) == "getLogger":
            arg = node.args[0] if node.args else None
            if arg is None:
                variant = "root"
            elif isinstance(arg, ast.Name) and arg.id == "__name__":
                variant = "dunder_name"
            elif _is_str(arg):
                variant = "literal"
            else:
                variant = "other"
            add("logger_acquisition", variant, node)
            return
        if dotted == "sys.exit":
            add("exits", "sys_exit", node)
        elif dotted in ("exit", "quit"):
            add("exits", "builtin_exit", node)
        elif dotted == "os._exit":
            add("exits", "os_exit", node)
        elif (
            isinstance(func, ast.Attribute)
            and func.attr == "exit"
            and _dotted(func.value).endswith("ctx")
        ):
            add("exits", "framework_exit", node)
        elif dotted == "print":
            to_stderr = any(
                k.arg == "file" and _dotted(k.value) == "sys.stderr"
                for k in node.keywords
            )
            add("output", "print_stderr" if to_stderr else "print_stdout", node)
        elif dotted in ("sys.stdout.write", "sys.stderr.write"):
            add("output", "stream_write", node)
        elif (
            isinstance(func, ast.Attribute)
            and func.attr == "print"
            and "console" in _dotted(func.value).lower()
        ):
            add("output", "console_print", node)

    def _raise(self, node: ast.Raise, add) -> None:
        exc = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
        dotted = _dotted(exc)
        if dotted == "SystemExit":
            add("exits", "raise_systemexit", node)
        elif dotted.endswith((".Exit", ".Abort")) or dotted in ("Exit", "Abort"):
            add("exits", "framework_exit", node)

    def _annotations(self, tree: ast.Module, add) -> None:
        inner_unions: set[int] = set()
        for ann in _annotation_nodes(tree):
            for node in ast.walk(ann):
                if isinstance(node, ast.Subscript):
                    head = _name(node.value)
                    if head in _LEGACY_GENERICS:
                        add("generic_style", "typing_alias", node)
                    elif head in _BUILTIN_GENERICS and isinstance(node.value, ast.Name):
                        add("generic_style", "builtin", node)
                    elif head == "Optional":
                        add("union_style", "optional", node)
                    elif head == "Union":
                        add("union_style", "union", node)
                elif (
                    isinstance(node, ast.BinOp)
                    and isinstance(node.op, ast.BitOr)
                    and id(node) not in inner_unions
                ):
                    add("union_style", "pipe", node)
                    # `A | B | C` is one union, not two.
                    for side in (node.left, node.right):
                        if isinstance(side, ast.BinOp) and isinstance(
                            side.op, ast.BitOr
                        ):
                            inner_unions.add(id(side))

    def _functions(self, tree: ast.Module, add) -> None:
        def visit(body, in_class: bool) -> None:
            for node in body:
                if isinstance(node, ast.ClassDef):
                    self._docstring(node, add)
                    visit(node.body, True)
                elif isinstance(node, _FUNC_TYPES):
                    self._annotation_coverage(node, in_class, add)
                    self._docstring(node, add)
                    visit(node.body, False)
                elif isinstance(node, (ast.If, ast.Try, ast.With, ast.AsyncWith)):
                    for part in ("body", "orelse", "finalbody"):
                        visit(getattr(node, part, []) or [], in_class)
                    for handler in getattr(node, "handlers", []) or []:
                        visit(handler.body, in_class)

        visit(tree.body, False)

    def _annotation_coverage(self, node, in_class: bool, add) -> None:
        args = node.args
        params = args.posonlyargs + args.args + args.kwonlyargs
        is_static = any(_name(d) == "staticmethod" for d in node.decorator_list)
        if in_class and params and not is_static:
            params = params[1:]  # self / cls
        params = params + [a for a in (args.vararg, args.kwarg) if a is not None]
        slots = [p.annotation is not None for p in params]
        if node.name != "__init__" or node.returns is not None:
            slots.append(node.returns is not None)
        if not slots:
            return
        if all(slots):
            variant = "full"
        elif any(slots):
            variant = "partial"
        else:
            variant = "none"
        add("annotation_coverage", variant, node)

    def _docstring(self, node, add) -> None:
        style = docstring_style(ast.get_docstring(node))
        if style in ("plain", "none"):
            self.bump("docstring_style", style)
        else:
            add("docstring_style", style, node)

    # -- result -------------------------------------------------------------

    def result(self) -> dict:
        out: dict[str, dict] = {}
        for category in (
            "string_formatting", "logging_message", "logger_acquisition",
            "generic_style", "union_style", "annotation_coverage",
            "docstring_style", "exits", "output",
        ):  # fmt: skip
            variants = self.sites.get(category, {})
            total = sum(len(v) for v in variants.values())
            counts = {k: len(v) for k, v in variants.items()}
            majority = max(counts, key=lambda k: (counts[k], k)) if counts else None
            per_file: dict[str, dict[str, int]] = {}
            for variant, sites in variants.items():
                for rel, _line, _code in sites:
                    bucket = per_file.setdefault(rel, {})
                    bucket[variant] = bucket.get(variant, 0) + 1
            listed = [
                {"file": rel, "line": line, "variant": variant, "code": code}
                for variant, sites in sorted(variants.items())
                if category in _INVENTORY or variant != majority
                for rel, line, code in sites
            ]
            listed.sort(key=lambda s: (s["file"], s["line"]))
            entry = {
                "total": total,
                "majority": majority,
                "majority_share": round(counts[majority] / total, 3) if total else None,
                "variants": {
                    k: {"count": counts[k], "files": len({s[0] for s in variants[k]})}
                    for k in sorted(counts)
                },
                "files_mixing_variants": sum(
                    1 for v in per_file.values() if len(v) > 1
                ),
                "per_file": dict(sorted(per_file.items())),
                "sites_listed": "all" if category in _INVENTORY else "minority",
                "sites": listed[:_SITE_CAP],
                "sites_truncated": max(0, len(listed) - _SITE_CAP),
            }
            if category in self.extra:
                entry["extra"] = dict(sorted(self.extra[category].items()))
            out[category] = entry
        return out


def analyze(target: str, *, max_files: int = 0, include_tests: bool = False) -> dict:
    """Survey conventions under *target* (a directory or a file)."""
    _resolved, project_root, scan_root = resolve_target(target)
    files, files_total = collect_python_files(scan_root, max_files)
    survey = Survey()
    skipped_tests = 0
    unparseable: list[str] = []
    surveyed = 0
    future_annotations = 0
    for path in files:
        rel = relative_to_root(path, project_root)
        if not include_tests and is_test_file(rel):
            skipped_tests += 1
            continue
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source)
        except (OSError, SyntaxError, ValueError, RecursionError):
            unparseable.append(rel)
            continue
        surveyed += 1
        if any(
            isinstance(n, ast.ImportFrom)
            and n.module == "__future__"
            and any(a.name == "annotations" for a in n.names)
            for n in tree.body
        ):
            future_annotations += 1
        survey.scan(rel, source, tree)

    categories = survey.result()
    result = build_envelope(project_root, scan_root, files_total, len(files))
    result["summary"] = {
        "files_surveyed": surveyed,
        "files_with_future_annotations": future_annotations,
        "majority": {k: v["majority"] for k, v in categories.items()},
        "files_mixing_variants": {
            k: v["files_mixing_variants"] for k, v in categories.items()
        },
    }
    result["categories"] = categories
    result["skipped"] = {
        "test_files": skipped_tests,
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
