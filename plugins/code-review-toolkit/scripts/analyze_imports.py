#!/usr/bin/env python3
"""Build a dependency graph from Python imports using AST parsing.

Outputs a JSON structure with:
- files: per-file import details
- internal_graph: edges between project modules
- external_deps: third-party and stdlib imports
- metrics: fan-in/fan-out per module
- cycles: detected circular dependencies
- re_exports: __init__.py re-exports and __all__ declarations

Usage:
    python analyze_imports.py [path]

    path: directory, file, or omitted for current directory
"""

import ast
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scan_common import (  # noqa: E402
    discover_python_files,
    find_project_root,
    parse_common_args,
)


# Comprehensive stdlib module list (Python 3.10+).  Used to distinguish
# stdlib imports from third-party imports when we cannot rely on
# importlib.metadata or sys.stdlib_module_names being available.
_STDLIB_TOP_LEVEL = frozenset(
    {
        "__future__",
        "_thread",
        "abc",
        "aifc",
        "argparse",
        "array",
        "ast",
        "asynchat",
        "asyncio",
        "asyncore",
        "atexit",
        "audioop",
        "base64",
        "bdb",
        "binascii",
        "binhex",
        "bisect",
        "builtins",
        "bz2",
        "calendar",
        "cgi",
        "cgitb",
        "chunk",
        "cmath",
        "cmd",
        "code",
        "codecs",
        "codeop",
        "collections",
        "colorsys",
        "compileall",
        "concurrent",
        "configparser",
        "contextlib",
        "contextvars",
        "copy",
        "copyreg",
        "cProfile",
        "crypt",
        "csv",
        "ctypes",
        "curses",
        "dataclasses",
        "datetime",
        "dbm",
        "decimal",
        "difflib",
        "dis",
        "distutils",
        "doctest",
        "email",
        "encodings",
        "enum",
        "errno",
        "faulthandler",
        "fcntl",
        "filecmp",
        "fileinput",
        "fnmatch",
        "fractions",
        "ftplib",
        "functools",
        "gc",
        "getopt",
        "getpass",
        "gettext",
        "glob",
        "graphlib",
        "grp",
        "gzip",
        "hashlib",
        "heapq",
        "hmac",
        "html",
        "http",
        "idlelib",
        "imaplib",
        "imghdr",
        "imp",
        "importlib",
        "inspect",
        "io",
        "ipaddress",
        "itertools",
        "json",
        "keyword",
        "lib2to3",
        "linecache",
        "locale",
        "logging",
        "lzma",
        "mailbox",
        "mailcap",
        "marshal",
        "math",
        "mimetypes",
        "mmap",
        "modulefinder",
        "multiprocessing",
        "netrc",
        "nis",
        "nntplib",
        "numbers",
        "operator",
        "optparse",
        "os",
        "ossaudiodev",
        "pathlib",
        "pdb",
        "pickle",
        "pickletools",
        "pipes",
        "pkgutil",
        "platform",
        "plistlib",
        "poplib",
        "posix",
        "posixpath",
        "pprint",
        "profile",
        "pstats",
        "pty",
        "pwd",
        "py_compile",
        "pyclbr",
        "pydoc",
        "queue",
        "quopri",
        "random",
        "re",
        "readline",
        "reprlib",
        "resource",
        "rlcompleter",
        "runpy",
        "sched",
        "secrets",
        "select",
        "selectors",
        "shelve",
        "shlex",
        "shutil",
        "signal",
        "site",
        "smtpd",
        "smtplib",
        "sndhdr",
        "socket",
        "socketserver",
        "spwd",
        "sqlite3",
        "sre_compile",
        "sre_constants",
        "sre_parse",
        "ssl",
        "stat",
        "statistics",
        "string",
        "stringprep",
        "struct",
        "subprocess",
        "sunau",
        "symtable",
        "sys",
        "sysconfig",
        "syslog",
        "tabnanny",
        "tarfile",
        "telnetlib",
        "tempfile",
        "termios",
        "test",
        "textwrap",
        "threading",
        "time",
        "timeit",
        "tkinter",
        "token",
        "tokenize",
        "tomllib",
        "trace",
        "traceback",
        "tracemalloc",
        "tty",
        "turtle",
        "turtledemo",
        "types",
        "typing",
        "unicodedata",
        "unittest",
        "urllib",
        "uu",
        "uuid",
        "venv",
        "warnings",
        "wave",
        "weakref",
        "webbrowser",
        "winreg",
        "winsound",
        "wsgiref",
        "xdrlib",
        "xml",
        "xmlrpc",
        "zipapp",
        "zipfile",
        "zipimport",
        "zlib",
        "zoneinfo",
        "_ast",
        "_collections_abc",
        "_compat_pickle",
        "_compression",
        "_markupbase",
        "_osx_support",
        "_pydecimal",
        "_pyio",
        "_sitebuiltins",
        "_strptime",
        "_threading_local",
        "_weakrefset",
        "antigravity",
        "this",
    }
)


def _is_stdlib(top_level_name: str) -> bool:
    """Check whether a top-level module name belongs to the stdlib."""
    # Try the canonical set first (Python 3.10+).
    if hasattr(sys, "stdlib_module_names"):
        return top_level_name in sys.stdlib_module_names
    return top_level_name in _STDLIB_TOP_LEVEL


def _classify(top: str | None, project_packages: set[str]) -> str:
    """Classify an import's top-level name.

    **The project's own packages win over the stdlib list.** When the reviewed
    tree IS a stdlib package -- CPython's own `Lib/idlelib`, `Lib/asyncio` --
    checking the stdlib first classifies every `from idlelib.x import y` as
    `stdlib`, drops it from the internal graph, and yields an empty graph with
    `cycles: []`. That reads as "no coupling" rather than "nothing resolved".
    """
    if not top:
        return "external"
    if top in project_packages:
        return "internal"
    return "stdlib" if _is_stdlib(top) else "external"


def _resolve_relative_import(
    source_file: Path, project_root: Path, level: int, module: str | None
) -> str | None:
    """Resolve a relative import to a dotted module path within the project.

    Returns None if resolution fails (e.g. goes above the project root).
    """
    # Start from the package containing source_file.
    try:
        rel = source_file.relative_to(project_root)
    except ValueError:
        return None

    parts = list(rel.parts[:-1])  # directory components (package path)

    # `from .X import Y` (level=1) means "X in MY package", so level=1 keeps the
    # containing package; each extra dot strips one more. Resolving level=1 to
    # the PARENT package (the previous behaviour) silently produced module paths
    # that match no file, which zeroed every fan_in metric for any project using
    # relative imports -- i.e. most of them.
    if level > len(parts) + 1:
        return None
    base_parts = parts[: len(parts) - (level - 1)] if level > 0 else parts

    dotted = ".".join(base_parts)
    if module:
        return f"{dotted}.{module}" if dotted else module
    return dotted or None


def analyze_file(
    filepath: Path, project_root: Path, project_packages: set[str]
) -> dict:
    """Parse a single Python file and extract import information."""
    result: dict = {
        "file": str(filepath.relative_to(project_root)),
        "imports": [],
        "all_declaration": None,
        "is_init": filepath.name == "__init__.py",
        "parse_error": None,
    }

    try:
        source = filepath.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(filepath))
    except SyntaxError as exc:
        result["parse_error"] = f"SyntaxError: {exc.msg} (line {exc.lineno})"
        return result

    # Detect TYPE_CHECKING guard ranges.
    type_checking_ranges: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            test = node.test
            is_tc = False
            if isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING":
                is_tc = True
            elif isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
                is_tc = True
            if is_tc:
                start = node.body[0].lineno if node.body else node.lineno
                end = max(
                    (getattr(n, "end_lineno", None) or getattr(n, "lineno", start))
                    for n in node.body
                )
                type_checking_ranges.append((start, end))

    def _in_type_checking(lineno: int) -> bool:
        return any(s <= lineno <= e for s, e in type_checking_ranges)

    # Detect try/except ImportError blocks (conditional imports).
    conditional_ranges: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            for handler in node.handlers:
                if handler.type is None:
                    continue
                names: list[str] = []
                if isinstance(handler.type, ast.Name):
                    names = [handler.type.id]
                elif isinstance(handler.type, ast.Tuple):
                    names = [e.id for e in handler.type.elts if isinstance(e, ast.Name)]
                if any(n in ("ImportError", "ModuleNotFoundError") for n in names):
                    start = node.body[0].lineno if node.body else node.lineno
                    end = node.end_lineno or node.lineno
                    conditional_ranges.append((start, end))

    def _is_conditional(lineno: int) -> bool:
        return any(s <= lineno <= e for s, e in conditional_ranges)

    # Extract __all__.
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "__all__"
        ):
            if isinstance(node.value, (ast.List, ast.Tuple)):
                result["all_declaration"] = [
                    elt.value
                    for elt in node.value.elts
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                ]

    scopes = _scope_map(tree)
    import_time_calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and scopes.get(id(node), ("module", None))[0] != "function"
    }

    def _timing(node: ast.AST) -> dict:
        """`scope` of an import and whether it is `lazy` (deferred to a call).

        An import inside a function runs when the function is called, not when
        the module is imported -- unless the module itself calls that function
        at import time (`_STATE = _init()`, where `_init` holds a "lazy"
        `from pkg.config import settings`). Such an edge can close an import
        cycle that fails a plain `import` while looking function-level.
        """
        scope, top_fn = scopes.get(id(node), ("module", None))
        lazy = scope == "function" and top_fn not in import_time_calls
        return {"scope": scope, "lazy": lazy}

    # Extract imports.
    dynamic_sites: list[dict] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            dynamic = _dynamic_import(node)
            if dynamic is None:
                continue
            call_name, arg = dynamic
            literal = (
                arg.value
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                else None
            )
            if not literal:
                dynamic_sites.append(
                    {
                        "line": node.lineno,
                        "call": call_name,
                        "expression": ast.unparse(arg) if arg is not None else None,
                        "prefix": _constant_prefix(arg),
                        **_timing(node),
                    }
                )
                continue
            level = len(literal) - len(literal.lstrip("."))
            module_str = literal.lstrip(".")
            resolved = (
                _resolve_relative_import(
                    filepath, project_root, level, module_str or None
                )
                if level
                else None
            )
            top = (
                (resolved or module_str).split(".")[0]
                if (resolved or module_str)
                else None
            )
            result["imports"].append(
                {
                    "module": module_str,
                    "names": [{"name": literal, "alias": None}],
                    "alias": None,
                    "top_level": top,
                    "category": "internal"
                    if level
                    else _classify(top, project_packages),
                    "line": node.lineno,
                    "is_relative": bool(level),
                    "relative_level": level,
                    "resolved_module": resolved,
                    "type_checking_only": _in_type_checking(node.lineno),
                    "conditional": _is_conditional(node.lineno),
                    "dynamic": True,
                    "form": "dynamic",
                    **_timing(node),
                }
            )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                category = _classify(top, project_packages)
                result["imports"].append(
                    {
                        "module": alias.name,
                        # `import a.b` binds the module itself: listing it keeps
                        # `names` a list for every entry (it used to be null).
                        "names": [{"name": alias.name, "alias": alias.asname}],
                        "alias": alias.asname,
                        "top_level": top,
                        "category": category,
                        "line": node.lineno,
                        "is_relative": False,
                        "relative_level": 0,
                        "type_checking_only": _in_type_checking(node.lineno),
                        "conditional": _is_conditional(node.lineno),
                        "dynamic": False,
                        "form": "import",
                        **_timing(node),
                    }
                )
        elif isinstance(node, ast.ImportFrom):
            names_imported = [{"name": a.name, "alias": a.asname} for a in node.names]
            level = node.level or 0
            module_str = node.module or ""
            is_relative = level > 0

            if is_relative:
                resolved = _resolve_relative_import(
                    filepath, project_root, level, node.module
                )
                top = resolved.split(".")[0] if resolved else None
                category = "internal"
            else:
                top = module_str.split(".")[0] if module_str else None
                category = _classify(top, project_packages)

            result["imports"].append(
                {
                    "module": module_str,
                    "names": names_imported,
                    "alias": None,
                    "top_level": top,
                    "category": category,
                    "line": node.lineno,
                    "is_relative": is_relative,
                    "relative_level": level,
                    "resolved_module": (
                        _resolve_relative_import(
                            filepath, project_root, level, node.module
                        )
                        if is_relative
                        else None
                    ),
                    "type_checking_only": _in_type_checking(node.lineno),
                    "conditional": _is_conditional(node.lineno),
                    "dynamic": False,
                    "form": "from",
                    **_timing(node),
                }
            )

    result["dynamic_import_sites"] = dynamic_sites
    result["import_time_side_effects"] = import_time_side_effects(tree)
    return result


# --- import-time side effects ------------------------------------------------
#
# A module-level statement runs when the module is imported, so a library that
# calls `logging.basicConfig()` -- or a helper that does -- at import time
# rewrites its host's logging the moment anyone imports it. These are the
# process-wide operations worth knowing about; each maps to a category.
_SIDE_EFFECT_CALLS: dict[str, str] = {
    **dict.fromkeys(
        (
            "logging.basicConfig",
            "logging.disable",
            "logging.captureWarnings",
            "logging.setLoggerClass",
            "logging.setLogRecordFactory",
            "logging.config.dictConfig",
            "logging.config.fileConfig",
        ),
        "logging",
    ),
    **dict.fromkeys(
        (
            "os.putenv",
            "os.unsetenv",
            "os.environ.update",
            "os.environ.setdefault",
            "os.environ.pop",
            "os.environ.popitem",
            "os.environ.clear",
            "os.environ.__setitem__",
            "os.environ.__delitem__",
        ),
        "environment",
    ),
    **dict.fromkeys(
        (
            "open",
            "os.makedirs",
            "os.mkdir",
            "os.remove",
            "os.unlink",
            "os.rename",
            "os.replace",
            "os.rmdir",
            "os.removedirs",
            "os.symlink",
            "os.chmod",
            "shutil.copy",
            "shutil.copy2",
            "shutil.copyfile",
            "shutil.copytree",
            "shutil.move",
            "shutil.rmtree",
            "tempfile.mkdtemp",
            "tempfile.mkstemp",
            "logging.FileHandler",
            "logging.handlers.RotatingFileHandler",
            "logging.handlers.TimedRotatingFileHandler",
        ),
        "filesystem",
    ),
    **dict.fromkeys(
        (
            "sys.path.insert",
            "sys.path.append",
            "sys.path.extend",
            "sys.setrecursionlimit",
            "sys.settrace",
            "sys.setprofile",
            "sys.setswitchinterval",
        ),
        "sys",
    ),
    **dict.fromkeys(
        (
            "os.chdir",
            "os.umask",
            "signal.signal",
            "warnings.filterwarnings",
            "warnings.simplefilter",
            "socket.setdefaulttimeout",
            "locale.setlocale",
            "faulthandler.enable",
            "asyncio.set_event_loop_policy",
            "multiprocessing.set_start_method",
        ),
        "process_global",
    ),
}
# Assigning to (or into) one of these replaces process-wide state.
_SIDE_EFFECT_TARGETS: dict[str, str] = {
    "os.environ": "environment",
    "sys.path": "sys",
    "sys.modules": "sys",
    "sys.excepthook": "sys",
    "sys.stdout": "sys",
    "sys.stderr": "sys",
    "sys.stdin": "sys",
    "threading.excepthook": "process_global",
}
# pathlib methods that write to disk.
_FS_METHODS = frozenset({"mkdir", "write_text", "write_bytes", "touch"})
# Mutations of a logger object; flagged only on the ROOT logger.
_LOGGER_MUTATORS = frozenset(
    {"addHandler", "removeHandler", "setLevel", "addFilter", "removeFilter"}
)
_LIST_MUTATORS = frozenset({"append", "extend", "insert", "remove", "clear", "pop"})


def _import_aliases(tree: ast.Module) -> dict[str, str]:
    """Local name -> dotted origin for module-level imports (`environ` ->
    `os.environ`, `lg` -> `logging`)."""
    aliases: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    aliases[alias.asname] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return aliases


def _is_main_guard(node: ast.If) -> bool:
    test = node.test
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
    )


def _is_type_checking_guard(node: ast.If) -> bool:
    test = node.test
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _executed_nodes(stmts: list[ast.stmt]):
    """Every node *stmts* execute when run, without entering nested function or
    lambda bodies, `if __name__ == "__main__":` or `if TYPE_CHECKING:`."""
    stack: list[ast.AST] = list(reversed(stmts))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Decorators and defaults run at definition time; the body does not.
            children: list[ast.AST] = [*node.decorator_list, node.args]
        elif isinstance(node, ast.Lambda):
            children = [node.args]
        elif isinstance(node, ast.If) and (
            _is_main_guard(node) or _is_type_checking_guard(node)
        ):
            children = list(node.orelse)
        else:
            children = list(ast.iter_child_nodes(node))
        stack.extend(reversed(children))


def import_time_side_effects(tree: ast.Module) -> list[dict]:
    """Process-wide operations a module performs when it is imported.

    Covers module-level (and class-body) code, plus the bodies of module-level
    functions that code calls -- transitively, within the same file -- since
    `_LOG = _setup_logging()` runs `_setup_logging` on import. Each entry has
    the `line` of the operation, its `category` (logging, environment,
    filesystem, sys, process_global), the `operation`, and, when it happens
    inside a called function, `via` (the call chain) and `call_line` (the
    module-level call that triggers it).
    """
    aliases = _import_aliases(tree)
    functions = {
        n.name: n
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    def canon(node: ast.AST) -> str:
        name = _dotted_name(node)
        head, _, rest = name.partition(".")
        if head in aliases:
            return f"{aliases[head]}.{rest}" if rest else aliases[head]
        return name

    def is_root_logger(node: ast.AST, roots: set[str]) -> bool:
        if isinstance(node, ast.Name) and node.id in roots:
            return True
        if isinstance(node, ast.Attribute) and canon(node) == "logging.root":
            return True
        if isinstance(node, ast.Call) and canon(node.func) == "logging.getLogger":
            if not node.args and not node.keywords:
                return True
            arg = node.args[0] if node.args else node.keywords[0].value
            return isinstance(arg, ast.Constant) and arg.value in (None, "")
        return False

    def effect(node: ast.AST, roots: set[str]) -> tuple[str, str] | None:
        if isinstance(node, ast.Call):
            name = canon(node.func)
            if name in _SIDE_EFFECT_CALLS:
                if name == "open" and not _opens_for_writing(node):
                    return None
                return _SIDE_EFFECT_CALLS[name], name
            func = node.func
            if isinstance(func, ast.Attribute):
                if func.attr in _LOGGER_MUTATORS and is_root_logger(func.value, roots):
                    return "logging", f"root logger .{func.attr}()"
                if (
                    func.attr in _LIST_MUTATORS
                    and isinstance(func.value, ast.Attribute)
                    and func.value.attr == "handlers"
                    and is_root_logger(func.value.value, roots)
                ):
                    return "logging", f"root logger .handlers.{func.attr}()"
                if func.attr in _FS_METHODS:
                    return "filesystem", f".{func.attr}()"
            return None
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)) and node.value:
            targets = [node.target]
        elif isinstance(node, ast.Delete):
            targets = list(node.targets)
        for target in targets:
            base = target.value if isinstance(target, ast.Subscript) else target
            name = canon(base)
            if name in _SIDE_EFFECT_TARGETS:
                return _SIDE_EFFECT_TARGETS[name], f"{name} assignment"
            if (
                isinstance(base, ast.Attribute)
                and base.attr in ("handlers", "level", "propagate")
                and is_root_logger(base.value, roots)
            ):
                return "logging", f"root logger .{base.attr} assignment"
        return None

    out: list[dict] = []
    seen: set[str] = set()
    queue: list[tuple[list[ast.stmt], list[str], int | None]] = [(tree.body, [], None)]
    while queue:
        body, via, call_line = queue.pop(0)
        nodes = list(_executed_nodes(body))
        roots = {
            t.id
            for n in nodes
            if isinstance(n, ast.Assign) and is_root_logger(n.value, set())
            for t in n.targets
            if isinstance(t, ast.Name)
        }
        for node in nodes:
            found = effect(node, roots)
            if found:
                entry = {
                    "line": node.lineno,
                    "category": found[0],
                    "operation": found[1],
                }
                if via:
                    entry["via"] = via
                    entry["call_line"] = call_line
                out.append(entry)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in functions
                and node.func.id not in seen
            ):
                seen.add(node.func.id)
                queue.append(
                    (
                        functions[node.func.id].body,
                        [*via, node.func.id],
                        call_line if via else node.lineno,
                    )
                )
    out.sort(key=lambda e: (e.get("call_line") or e["line"], e["line"]))
    return out


def _dotted_name(node: ast.AST) -> str:
    """`a.b.c` for a Name/Attribute chain, else ''."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        inner = _dotted_name(node.value)
        return f"{inner}.{node.attr}" if inner else ""
    return ""


def _opens_for_writing(call: ast.Call) -> bool:
    mode = call.args[1] if len(call.args) > 1 else None
    for kw in call.keywords:
        if kw.arg == "mode":
            mode = kw.value
    return (
        isinstance(mode, ast.Constant)
        and isinstance(mode.value, str)
        and any(c in mode.value for c in "wax+")
    )


def _scope_map(tree: ast.AST) -> dict[int, tuple[str, str | None]]:
    """Map ``id(node)`` to ``(scope, enclosing module-level function)``.

    *scope* is ``module``, ``class`` (a class body runs at import time) or
    ``function``. The function name is the OUTERMOST module-level ``def``
    enclosing the node, so a module-level call to it can be recognized.
    """
    out: dict[int, tuple[str, str | None]] = {}

    def visit(node: ast.AST, scope: str, top_fn: str | None) -> None:
        for child in ast.iter_child_nodes(node):
            out[id(child)] = (scope, top_fn)
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                name = getattr(child, "name", None) if scope == "module" else top_fn
                visit(child, "function", name)
            elif isinstance(child, ast.ClassDef):
                visit(child, "function" if scope == "function" else "class", top_fn)
            else:
                visit(child, scope, top_fn)

    visit(tree, "module", None)
    return out


def _dynamic_import(node: ast.Call) -> tuple[str, ast.expr | None] | None:
    """``(call name, module argument)`` for ``importlib.import_module(...)`` or
    ``__import__(...)``, else None."""
    func = node.func
    if isinstance(func, ast.Name) and func.id in ("__import__", "import_module"):
        name = func.id
    elif isinstance(func, ast.Attribute) and func.attr == "import_module":
        name = (
            f"{func.value.id}.import_module"
            if isinstance(func.value, ast.Name)
            else "import_module"
        )
    else:
        return None
    arg = node.args[0] if node.args else None
    if arg is None:
        for kw in node.keywords:
            if kw.arg == "name":
                arg = kw.value
    return name, arg


def _constant_prefix(arg: ast.expr | None) -> str | None:
    """The literal leading part of a computed module name (`f"pkg.{x}"` -> `pkg.`)."""
    if isinstance(arg, ast.JoinedStr) and arg.values:
        first = arg.values[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
    if (
        isinstance(arg, ast.BinOp)
        and isinstance(arg.op, ast.Add)
        and isinstance(arg.left, ast.Constant)
        and isinstance(arg.left.value, str)
    ):
        return arg.left.value
    return None


def identify_project_packages(root: Path, scan_root: Path | None = None) -> set[str]:
    """Identify top-level Python packages in the project.

    *scan_root* is included as a package in its own right when it is a package
    directory below *root*. Without it, reviewing a subpackage of a large tree
    resolves nothing: on CPython's ``Lib/idlelib`` the detected packages came out
    as ``['python-config', 'python-gdb']`` from the checkout root, every
    ``from idlelib.x import y`` was classified `stdlib`, and the whole internal
    graph came back empty -- reported as ``cycles: []``, which reads as a clean
    result rather than as a total resolution failure.
    """
    packages: set[str] = set()
    if scan_root is not None and (scan_root / "__init__.py").exists():
        packages.add(scan_root.name)
    # Look for directories with __init__.py.
    for item in root.iterdir():
        if item.is_dir() and (item / "__init__.py").exists():
            if item.name not in {"test", "tests", ".git", "__pycache__"}:
                packages.add(item.name)
        # Also consider top-level .py files as modules.
        if item.is_file() and item.suffix == ".py" and item.name != "setup.py":
            packages.add(item.stem)
    # Check src/ layout.
    src = root / "src"
    if src.is_dir():
        for item in src.iterdir():
            if item.is_dir() and (item / "__init__.py").exists():
                packages.add(item.name)
    return packages


def build_internal_graph(
    file_analyses: list[dict],
) -> dict[str, list[dict]]:
    """Build a graph of internal module dependencies."""
    graph: dict[str, list[dict]] = {}
    for fa in file_analyses:
        source = fa["file"]
        edges: list[dict] = []
        for imp in fa["imports"]:
            if imp["category"] != "internal":
                continue
            target_module = imp.get("resolved_module") or imp["module"]
            edges.append(
                {
                    "target": target_module,
                    "names": imp.get("names"),
                    "line": imp["line"],
                    "type_checking_only": imp["type_checking_only"],
                    "conditional": imp["conditional"],
                    "scope": imp.get("scope", "module"),
                    "lazy": imp.get("lazy", False),
                    "dynamic": imp.get("dynamic", False),
                    "form": imp.get("form", "from"),
                }
            )
        if edges:
            graph[source] = edges
    return graph


def resolve_edge_targets(edge: dict, index: dict[str, str]) -> set[str]:
    """Files an import edge actually depends on.

    `from pkg import submodule` binds the SUBMODULE -- Python's `_handle_fromlist`
    falls back to importing it -- so the dependency is on `pkg/submodule.py`, not
    on `pkg/__init__.py`. Attributing it to the package manufactures a cycle
    through the package facade: 16 of the 20 cycles reported for coverage.py were
    this one idiom (`from coverage import env`), and none of them is real.

    `from pkg import SomeName` is different: that name is BOUND in `__init__.py`,
    so the dependency is on the package and is order-sensitive.
    """
    target = edge.get("target") or ""
    # Only `from X import name` can bind a submodule; for `import a.b` and a
    # dynamic import, `names` just repeats the module itself.
    names = (
        [n.get("name") for n in (edge.get("names") or []) if n.get("name")]
        if edge.get("form", "from") == "from"
        else []
    )
    submodules = {
        index[f"{target}.{name}"] for name in names if f"{target}.{name}" in index
    }
    bound = [name for name in names if f"{target}.{name}" not in index]
    resolved = set(submodules)
    # A bare `import pkg`, or a name genuinely bound in the package __init__.
    if (not names or bound) and target in index:
        resolved.add(index[target])
    return resolved


def module_name_for(path: str) -> str:
    """Dotted module name a project-relative file path denotes."""
    module = path.replace("/", ".").replace("\\", ".")
    if module.endswith(".py"):
        module = module[:-3]
    if module.endswith(".__init__"):
        module = module[:-9]
    return module


def compute_metrics(graph: dict[str, list[dict]], all_files: list[str]) -> dict:
    """Compute fan-in and fan-out per file.

    Fan-in resolves each target to EXACTLY one file. The previous prefix match
    (`t.startswith(f_module + ".")`) made a package's `__init__.py` match every
    module inside it, so `coverage/__init__.py` reported a fan-in of 209 in a
    44-file package where the true figure is 24. That is the same prefix
    fallback `detect_cycles` had already dropped -- the fix was never propagated
    to its sibling.
    """
    index = {module_name_for(f): f for f in all_files}
    fan_out: dict[str, int] = {}
    fan_in: dict[str, int] = {}

    for f in all_files:
        fan_out[f] = 0
        fan_in[f] = 0

    for source, edges in graph.items():
        targets = {e["target"] for e in edges}
        fan_out[source] = len(targets)
        depends_on: set[str] = set()
        for edge in edges:
            depends_on |= resolve_edge_targets(edge, index)
        for resolved in depends_on:
            fan_in[resolved] = fan_in.get(resolved, 0) + 1

    return {
        "fan_out": dict(sorted(fan_out.items(), key=lambda x: -x[1])),
        "fan_in": dict(sorted(fan_in.items(), key=lambda x: -x[1])),
    }


def implicit_package_inits(
    target: str, importer: str, module_to_file: dict[str, str]
) -> set[str]:
    """Package ``__init__.py`` files an import of *target* runs on the way.

    ``from a.b.c import X`` executes ``a/__init__.py`` and ``a/b/__init__.py``
    before ``a/b/c.py``, so a cycle can close THROUGH a parent package: ``helpers``
    imports ``pkg.graph.consts``, ``pkg/graph/__init__.py`` imports ``vertex``,
    and ``vertex`` imports ``helpers``. Recording only the edge to
    ``pkg.graph.consts``
    hid exactly that cycle, which fails as ``ImportError`` when ``utils`` is
    imported first.

    Packages that enclose the importer itself are left out: they were imported
    (at least partially) before the importer started running, so the import
    cannot execute them again. Adding them would turn every
    ``from pkg import x`` inside ``pkg`` into a phantom cycle through the
    facade -- the false positive ``resolve_edge_targets`` exists to prevent.
    """
    importer_module = module_name_for(importer)
    own = importer_module.split(".")
    own_packages = {".".join(own[:i]) for i in range(1, len(own))}
    if importer.endswith("__init__.py"):
        own_packages.add(importer_module)
    parts = target.split(".")
    out: set[str] = set()
    for i in range(1, len(parts) + 1):
        package = ".".join(parts[:i])
        path = module_to_file.get(package)
        if path and path.endswith("__init__.py") and package not in own_packages:
            out.add(path)
    return out


def file_adjacency(
    graph: dict[str, list[dict]],
    all_files: list[str] | None = None,
    *,
    include_type_checking: bool = False,
    include_lazy: bool = True,
) -> dict[str, set[str]]:
    """``file -> files it depends on``, with edges resolved to concrete files.

    Includes the implicit edge from an import of ``a.b.c`` to each parent
    package ``__init__.py`` it executes (see ``implicit_package_inits``).
    """
    adj: dict[str, list[dict]] = {}
    for source, edges in graph.items():
        adj[source] = [
            e
            for e in edges
            if (include_type_checking or not e.get("type_checking_only"))
            and (include_lazy or not e.get("lazy"))
        ]
    # Index EVERY file, not only those that have imports of their own. A leaf
    # module (coverage/env.py, fan-out 0) is absent from the graph keys, so
    # indexing from them alone leaves `coverage.env` unresolvable -- and the
    # bare-package fallback then attributes `from coverage import env` to the
    # package __init__, manufacturing a cycle through the facade.
    module_to_file = {
        module_name_for(f): f for f in (all_files if all_files is not None else adj)
    }
    out: dict[str, set[str]] = {}
    for f, edges in adj.items():
        resolved: set[str] = set()
        for edge in edges:
            # No prefix fallback: an unresolvable target is not a file-level
            # edge. A prefix match fabricated edges to the enclosing package's
            # __init__, and with them phantom cycles.
            resolved |= resolve_edge_targets(edge, module_to_file)
            resolved |= implicit_package_inits(
                edge.get("target") or "", f, module_to_file
            )
        resolved.discard(f)
        out[f] = resolved
    return out


def detect_cycles(
    graph: dict[str, list[dict]],
    include_type_checking: bool = False,
    all_files: list[str] | None = None,
    include_lazy: bool = True,
) -> list[list[str]]:
    """Detect circular dependencies using DFS.

    Normalises targets to file paths so that cycles between files can be
    detected even when the graph edges use dotted module names.

    A `TYPE_CHECKING`-guarded import does NOT run, so a cycle that exists only
    through such edges is not an import cycle -- avoiding it is precisely why
    the guard is there. Those edges are excluded by default; the flag keeps the
    type-time graph available for callers that want it.

    ``include_lazy=False`` also drops function-level imports that do not run at
    import time, leaving the cycles that can fail a plain ``import``.

    An import of ``a.b.c`` also depends on the parent package ``a/b/__init__.py``
    (when the importer lives outside ``a.b``): importing it runs that file.
    """
    file_adj = file_adjacency(
        graph,
        all_files,
        include_type_checking=include_type_checking,
        include_lazy=include_lazy,
    )
    return _cycles_in(file_adj)


def _cycles_in(file_adj: dict[str, set[str]]) -> list[list[str]]:
    """Cycles found by a DFS over a file-level adjacency map."""
    # Standard cycle detection with DFS.
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {f: WHITE for f in file_adj}
    parent: dict[str, str | None] = {f: None for f in file_adj}
    cycles: list[list[str]] = []

    def dfs(u: str) -> None:
        color[u] = GRAY
        for v in sorted(file_adj.get(u, ())):
            if v not in color:
                color[v] = WHITE
            if color[v] == GRAY:
                # Found a cycle -- reconstruct it. The walk back from `u` ends ON
                # `v`, so seeding the list with `v` as well repeated it: every
                # cycle came out one element too long, and a 2-cycle rendered as
                # three nodes ("a -> b -> b"). All 26 cycles reported for
                # coverage.py had a duplicated node.
                cycle = [u]
                node = u
                while node != v and parent.get(node) is not None:
                    node = parent[node]  # type: ignore[assignment]
                    cycle.append(node)
                cycle.reverse()
                # Normalise: start from the lexicographically smallest element.
                min_idx = cycle.index(min(cycle))
                cycle = cycle[min_idx:] + cycle[:min_idx]
                if cycle not in cycles:
                    cycles.append(cycle)
            elif color[v] == WHITE:
                parent[v] = u
                dfs(v)
        color[u] = BLACK

    for f in sorted(file_adj):
        if color.get(f, WHITE) == WHITE:
            dfs(f)

    return cycles


def _prefix_module(site: dict, project_root: Path) -> str | None:
    """The dotted prefix a computed dynamic import name starts with, resolved
    against the importing file when it is relative (`f".{name}"`)."""
    prefix = site.get("prefix")
    if not prefix:
        return None
    if not prefix.startswith("."):
        return prefix
    level = len(prefix) - len(prefix.lstrip("."))
    base = _resolve_relative_import(
        project_root / site["file"], project_root, level, None
    )
    rest = prefix.lstrip(".")
    return f"{base}.{rest}" if base else None


def expand_dynamic_sites(
    sites: list[dict], all_files: list[str], project_root: Path
) -> dict[str, list[dict]]:
    """Possible edges for computed-name dynamic imports with a literal prefix.

    ``importlib.import_module(f"pkg.plugins.{kind}")`` can load any module
    directly under ``pkg.plugins``. Those modules are recorded on the site as
    ``expanded_targets`` and returned as edges (``possible: true``), so a cycle
    that closes only through the factory -- factory -> plugin -> factory -- can
    be reported. Only direct children of the prefix are expanded; an allowlist
    in the code may narrow the real set further, which is why they are
    "possible".
    """
    modules = {module_name_for(f): f for f in all_files}
    extra: dict[str, list[dict]] = {}
    for site in sites:
        prefix = _prefix_module(site, project_root)
        targets: list[str] = []
        if prefix:
            for module, path in sorted(modules.items()):
                rest = module[len(prefix) :] if module.startswith(prefix) else ""
                if rest and "." not in rest and path != site["file"]:
                    targets.append(path)
                    extra.setdefault(site["file"], []).append(
                        {
                            "target": module,
                            "names": [{"name": module, "alias": None}],
                            "line": site["line"],
                            "type_checking_only": False,
                            "conditional": False,
                            "scope": site.get("scope", "module"),
                            "lazy": site.get("lazy", False),
                            "dynamic": True,
                            "form": "dynamic",
                            "possible": True,
                        }
                    )
        site["expanded_targets"] = targets
    return extra


def dynamic_cycles(
    graph: dict[str, list[dict]],
    extra: dict[str, list[dict]],
    all_files: list[str],
) -> list[dict]:
    """Cycles that close only through an expanded dynamic-import edge."""
    if not extra:
        return []
    merged = {k: list(v) for k, v in graph.items()}
    for source, edges in extra.items():
        merged.setdefault(source, []).extend(edges)
    static = file_adjacency(graph, all_files)
    eager = file_adjacency(merged, all_files, include_lazy=False)
    out: list[dict] = []
    for cycle in _cycles_in(file_adjacency(merged, all_files)):
        pairs = list(zip(cycle, cycle[1:] + cycle[:1]))
        via = [{"from": a, "to": b} for a, b in pairs if b not in static.get(a, set())]
        if via:
            out.append(
                {
                    "cycle": cycle,
                    "via_dynamic_import": via,
                    # Every step also runs at import time (no lazy edge).
                    "import_time": all(b in eager.get(a, set()) for a, b in pairs),
                }
            )
    return out


def analyze(target: str, *, max_files: int = 0) -> dict:
    """Import graph, metrics and cycles for *target* (see the module docstring)."""
    target_path = Path(target).resolve()

    project_root = find_project_root(target_path)
    scan_root = target_path if target_path.is_dir() else project_root
    project_packages = identify_project_packages(project_root, scan_root)

    all_files = sorted(discover_python_files(scan_root))
    files_total = len(all_files)
    if max_files > 0 and files_total > max_files:
        all_files = all_files[:max_files]

    file_analyses = [analyze_file(f, project_root, project_packages) for f in all_files]

    all_file_paths = [fa["file"] for fa in file_analyses]
    internal_graph = build_internal_graph(file_analyses)
    metrics = compute_metrics(internal_graph, all_file_paths)
    cycles = detect_cycles(internal_graph, all_files=all_file_paths)
    import_time_cycles = detect_cycles(
        internal_graph, all_files=all_file_paths, include_lazy=False
    )
    dynamic_import_sites = [
        {"file": fa["file"], **site}
        for fa in file_analyses
        for site in fa.get("dynamic_import_sites", [])
    ]
    side_effects = [
        {"file": fa["file"], **effect}
        for fa in file_analyses
        for effect in fa.get("import_time_side_effects", [])
    ]
    possible_edges = expand_dynamic_sites(
        dynamic_import_sites, all_file_paths, project_root
    )
    cycles_via_dynamic = dynamic_cycles(internal_graph, possible_edges, all_file_paths)

    # Collect external dependencies.
    external: dict[str, list[str]] = {}
    for fa in file_analyses:
        for imp in fa["imports"]:
            if imp["category"] == "external" and imp["top_level"]:
                external.setdefault(imp["top_level"], []).append(fa["file"])

    # Collect re-exports.
    re_exports: list[dict] = []
    for fa in file_analyses:
        if fa["is_init"] and fa["all_declaration"] is not None:
            re_exports.append(
                {
                    "file": fa["file"],
                    "all": fa["all_declaration"],
                }
            )
        elif fa["is_init"]:
            # __init__.py without __all__ — list what it imports.
            init_imports = [
                imp["module"] or (imp.get("resolved_module") or "")
                for imp in fa["imports"]
                if imp["category"] == "internal"
            ]
            if init_imports:
                re_exports.append(
                    {
                        "file": fa["file"],
                        "all": None,
                        "re_imported_modules": init_imports,
                    }
                )

    # Prune intermediate fields from file analyses to reduce memory.
    for fa in file_analyses:
        for imp in fa["imports"]:
            imp.pop("resolved_module", None)

    # Denominator honesty: a graph with no internal edges over a non-trivial
    # file set is a RESOLUTION FAILURE, not a project with no coupling. Reporting
    # `cycles: []` for it states a clean result the run did not establish.
    resolution = "ok"
    resolution_note = None
    # metrics is {"fan_in": {file: n}, "fan_out": {file: n}} -- not per-module
    # dicts. Reading it the other way makes every project look unresolved.
    graphed = sum(
        1
        for bucket in ("fan_in", "fan_out")
        for n in metrics.get(bucket, {}).values()
        if n
    )
    if len(file_analyses) > 3 and not internal_graph:
        resolution = "FAILED"
        resolution_note = (
            f"no internal import edges resolved across {len(file_analyses)} files. "
            f"Detected packages {sorted(project_packages)!r} — if that list does "
            "not contain the package under review, every internal import was "
            "misclassified as external and `cycles`, `metrics` and "
            "`internal_graph` below are empty for that reason, NOT because the "
            "code has no coupling."
        )
    elif internal_graph and not graphed:
        # KNOWN LIMITATION. `internal_graph` keys on dotted module names while
        # `metrics`/`cycles` key on repo-relative file paths. Those agree only
        # when the package root IS the project root. Reviewing `Lib/idlelib`
        # inside a CPython checkout, `idlelib.editor` never matches
        # `Lib/idlelib/editor.py`, so 113 resolved edges still produce zero
        # metrics and zero cycles -- which would read as "no coupling".
        resolution = "PARTIAL"
        resolution_note = (
            f"{len(internal_graph)} import edges resolved, but NONE mapped onto a "
            "scanned file, so `metrics` and `cycles` are empty and must not be "
            "read as findings. This happens when the reviewed package is not at "
            "the project root (e.g. Lib/idlelib inside a CPython checkout): the "
            "graph keys on dotted module names and the metrics key on file paths."
        )

    output = {
        "project_root": str(project_root),
        "scan_root": str(scan_root),
        "resolution": resolution,
        "resolution_note": resolution_note,
        "project_packages": sorted(project_packages),
        "files_total": files_total,
        "files_analyzed": len(all_files),
        "files_capped": max_files > 0 and files_total > max_files,
        "file_count": len(file_analyses),
        "files": file_analyses,
        "internal_graph": internal_graph,
        "external_dependencies": {
            k: sorted(set(v)) for k, v in sorted(external.items())
        },
        "metrics": metrics,
        "cycles": cycles,
        # The subset of `cycles` whose every edge runs at import time (no
        # TYPE_CHECKING-only or lazy function-level edge): these can fail a
        # plain `import`; the rest only bite when the lazy import executes.
        "import_time_cycles": import_time_cycles,
        # importlib.import_module / __import__ calls whose module name is
        # computed. Literal ones are ordinary `imports` entries with
        # `dynamic: true`; these are not edges, so modules reached only this
        # way show fan-in 0.
        "dynamic_import_sites": dynamic_import_sites,
        # Cycles that close only through a computed-name dynamic import whose
        # literal prefix was expanded to the modules under it
        # (`dynamic_import_sites[].expanded_targets`). Possible, not certain:
        # the code may only ever load some of those modules.
        "dynamic_cycles": cycles_via_dynamic,
        # Process-wide operations a module performs when it is imported
        # (root-logger setup, environment writes, files, sys.path, signal and
        # warning filters), directly or through a module-level function the
        # module calls on import (`via`, `call_line`).
        "import_time_side_effects": side_effects,
        "re_exports": re_exports,
    }
    return output


def main() -> None:
    target_arg, max_files = parse_common_args(sys.argv[1:], __doc__)
    json.dump(analyze(target_arg, max_files=max_files), sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
