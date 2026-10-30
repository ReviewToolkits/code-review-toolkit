#!/usr/bin/env python3
"""Correlate source modules with their test files.

Maps each source file to its corresponding test file(s), counts test classes
and methods, and identifies source files with no test coverage.

Usage:
    python correlate_tests.py [path]
"""

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scan_common import (  # noqa: E402
    discover_python_files,
    find_project_root,
    parse_source,
)

_TEST_DIR_NAMES = frozenset({"test", "tests"})


def _test_named(f: Path) -> bool:
    """True for `test_*.py` / `*_test.py` file names."""
    return f.name.startswith("test_") or f.name.endswith("_test.py")


def _in_test_dir(f: Path, project_root: Path) -> bool:
    return bool(_TEST_DIR_NAMES & set(f.relative_to(project_root).parts[:-1]))


def defines_tests(filepath: Path) -> bool:
    """True when *filepath* defines a test pytest or unittest would collect.

    A module-level `test*` function, or a class with a `test*` method. An
    unparseable file counts as a test file: its name is the only evidence.
    """
    tree = parse_source(filepath)
    if tree is None:
        return True
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test"):
                return True
        elif isinstance(node, ast.ClassDef) and any(
            isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            and child.name.startswith("test")
            for child in node.body
        ):
            return True
    return False


def classify_files(
    files: list[Path],
    project_root: Path,
    test_named_sources: list[Path] | None = None,
) -> tuple[list[Path], list[Path]]:
    """Split files into source and test files.

    Everything under a `test`/`tests` directory is test code. Elsewhere a
    `test_*.py` / `*_test.py` name is not enough: a package may own a source
    module so named (an enum of test outcomes, imported by production code).
    Such a file is a test only when it defines tests; otherwise it is source,
    and it is appended to *test_named_sources* when that list is given.
    """
    source: list[Path] = []
    test: list[Path] = []
    for f in files:
        if _in_test_dir(f, project_root):
            test.append(f)
        elif _test_named(f):
            if defines_tests(f):
                test.append(f)
            else:
                source.append(f)
                if test_named_sources is not None:
                    test_named_sources.append(f)
        # Skip setup.py and conftest.py outside test directories.
        elif f.name not in ("setup.py", "conftest.py"):
            source.append(f)
    return source, test


def _is_skip_decorator(d: ast.expr) -> bool:
    """`@unittest.skip...`, `@pytest.mark.skip/skipif` and the like."""
    target = d.func if isinstance(d, ast.Call) else d
    if isinstance(target, ast.Attribute):
        return "skip" in target.attr.lower()
    return isinstance(target, ast.Name) and "skip" in target.id.lower()


def _is_skipped(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True for a test function or method decorated to be skipped."""
    return any(_is_skip_decorator(d) for d in func.decorator_list)


# pytest's own markers that select or configure tests rather than label them.
# Skips are counted as skipped tests instead.
_UNLABELLED_MARKS = frozenset({
    "parametrize", "usefixtures", "filterwarnings", "skip", "skipif",
})


def _mark_name(expr: ast.expr) -> str | None:
    """`slow` for `pytest.mark.slow` / `mark.slow(...)`; None otherwise."""
    target = expr.func if isinstance(expr, ast.Call) else expr
    if (
        isinstance(target, ast.Attribute)
        and isinstance(target.value, (ast.Attribute, ast.Name))
        and (
            getattr(target.value, "attr", None) == "mark"
            or getattr(target.value, "id", None) == "mark"
        )
    ):
        return target.attr
    return None


def _pytestmark(body: list[ast.stmt]) -> list[ast.expr]:
    """The marks a module or class assigns to `pytestmark`."""
    marks: list[ast.expr] = []
    for node in body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets)
        ):
            value = node.value
            marks.extend(value.elts if isinstance(value, (ast.List, ast.Tuple)) else [value])
    return marks


def _calls_pytest_skip(node: ast.AST, *, module_level: bool = False) -> bool:
    """A `pytest.skip(...)` call (or `importorskip`, at module level) in *node*.

    At module level only statements outside functions and classes count.
    """
    if module_level:
        stack = [n for n in node.body if not isinstance(
            n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        )]
        nodes = (sub for n in stack for sub in ast.walk(n))
    else:
        nodes = ast.walk(node)
    names = {"skip", "importorskip"} if module_level else {"skip"}
    for sub in nodes:
        if (
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Attribute)
            and sub.func.attr in names
            and isinstance(sub.func.value, ast.Name)
            and sub.func.value.id == "pytest"
        ):
            return True
    return False


def _extract_test_info(filepath: Path) -> dict:
    """Extract test classes and methods from a test file.

    Counts unittest `TestCase` methods, methods of plain pytest classes and
    pytest-style module-level `test*` functions alike. A skip decorator on
    the class, or a skip mark in a module's or class's `pytestmark`, skips
    every test under it. `markers` counts the tests carrying each pytest
    mark (decorator, class or module `pytestmark`); `runtime_skip_tests`
    names the tests whose body may call `pytest.skip()`, and `module_skip`
    says the whole file may be skipped at import (`pytest.skip(...,
    allow_module_level=True)`, `pytest.importorskip`).
    """
    try:
        source = filepath.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(filepath))
    except SyntaxError:
        return {
            "classes": [],
            "standalone_tests": [],
            "skipped_standalone_tests": [],
            "markers": {},
            "runtime_skip_tests": [],
            "module_skip": False,
            "parse_error": True,
        }

    classes: list[dict] = []
    standalone: list[str] = []
    skipped_standalone: list[str] = []
    markers: dict[str, int] = {}
    runtime_skip: list[str] = []

    module_marks = _pytestmark(tree.body)
    module_skipped = any(_is_skip_decorator(m) for m in module_marks)

    def record(func: ast.FunctionDef | ast.AsyncFunctionDef,
               inherited: list[ast.expr]) -> None:
        names = {
            name
            for d in [*inherited, *func.decorator_list]
            if (name := _mark_name(d)) and name not in _UNLABELLED_MARKS
        }
        for name in names:
            markers[name] = markers.get(name, 0) + 1
        if _calls_pytest_skip(func):
            runtime_skip.append(func.name)

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            class_marks = [*module_marks, *node.decorator_list, *_pytestmark(node.body)]
            class_skipped = any(_is_skip_decorator(m) for m in class_marks)
            methods = []
            skipped = []
            for child in ast.iter_child_nodes(node):
                if (
                    isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and child.name.startswith("test")
                ):
                    if class_skipped or _is_skipped(child):
                        skipped.append(child.name)
                    else:
                        methods.append(child.name)
                    record(child, class_marks)

            classes.append({
                "name": node.name,
                "test_methods": methods,
                "skipped_methods": skipped,
                "total_test_methods": len(methods) + len(skipped),
                "line_start": node.lineno,
            })
        elif (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test")
        ):
            if module_skipped or _is_skipped(node):
                skipped_standalone.append(node.name)
            else:
                standalone.append(node.name)
            record(node, module_marks)

    return {
        "classes": classes,
        "standalone_tests": standalone,
        "skipped_standalone_tests": skipped_standalone,
        "markers": dict(sorted(markers.items())),
        "runtime_skip_tests": runtime_skip,
        "module_skip": _calls_pytest_skip(tree, module_level=True),
        "parse_error": False,
    }


def _extract_source_info(filepath: Path) -> dict:
    """Extract public classes and functions from a source file."""
    try:
        source = filepath.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(filepath))
    except SyntaxError:
        return {"classes": [], "functions": [], "parse_error": True}

    # Check for __all__.
    all_names: list[str] | None = None
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "__all__"
            and isinstance(node.value, (ast.List, ast.Tuple))
        ):
            all_names = [
                elt.value
                for elt in node.value.elts
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
            ]

    classes: list[dict] = []
    functions: list[str] = []

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            if not node.name.startswith("_") or (
                all_names and node.name in all_names
            ):
                methods = [
                    child.name
                    for child in ast.iter_child_nodes(node)
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and not child.name.startswith("_")
                ]
                classes.append({
                    "name": node.name,
                    "public_methods": methods,
                })
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_") or (
                all_names and node.name in all_names
            ):
                functions.append(node.name)

    return {
        "classes": classes,
        "functions": functions,
        "all_declaration": all_names,
        "parse_error": False,
    }


# Directories that name a test layer rather than the code under test.
_LAYER_DIR_NAMES = frozenset({
    "test", "tests", "testing", "unit", "units", "integration", "functional",
    "e2e", "system", "acceptance", "regression", "src",
})


def _match_test_to_source(
    test_file: Path, source_files: list[Path], project_root: Path
) -> list[str]:
    """Find source files that a test file likely corresponds to.

    Heuristics:
    - test_runner.py → runner.py (any directory; several same-stem sources
      all match)
    - tests/<stem>/test_*.py → <stem>.py: a directory per source module,
      holding one test file per method or behaviour

    Only test-named files (`test_*.py`, `*_test.py`) name-match: a test
    package's `__init__.py` or a `helpers.py` corresponds to no module, and
    a package `__init__.py` is never matched by name.
    """
    if not _test_named(test_file):
        return []
    test_name = test_file.stem  # e.g. "test_runner"
    source_stem = test_name[5:] if test_name.startswith("test_") else test_name[:-5]
    parent = test_file.parent.name
    by_directory = (
        _in_test_dir(test_file, project_root)
        and parent not in _LAYER_DIR_NAMES
        and parent != "__init__"
    )

    matches: list[str] = []
    for sf in source_files:
        if sf.stem == "__init__":
            continue
        if sf.stem == source_stem or (by_directory and sf.stem == parent):
            matches.append(str(sf.relative_to(project_root)))
    return list(dict.fromkeys(matches))


# ---------------------------------------------------------------------------
# HTTP routes: a test that drives a module through a test client imports the
# app, not the module, so it correlates by URL instead.
# ---------------------------------------------------------------------------

HTTP_VERBS = frozenset({"get", "post", "put", "patch", "delete", "head", "options"})
ROUTE_DECORATORS = HTTP_VERBS | {"route", "api_route", "websocket"}


def path_segments(node: ast.expr) -> list[str | None] | None:
    """URL path segments of a str / f-string literal; None marks a wildcard.

    `{}` / `{name}` placeholders (for `.format()`) are wildcards too. Returns
    None for anything that is not a literal starting with `/`.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        text = node.value
    elif isinstance(node, ast.JoinedStr):
        pieces = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                pieces.append(value.value)
            else:
                pieces.append("\0")
        text = "".join(pieces)
    else:
        return None
    if not text.startswith("/") or any(c.isspace() for c in text):
        return None
    text = text.split("?", 1)[0]
    return [
        None if ("\0" in seg or "{" in seg) else seg
        for seg in text.strip("/").split("/")
        if seg
    ]


def template_segments(path: str) -> list[str | None]:
    """Segments of a route template; `{param}` / `<param>` become wildcards."""
    return [
        None if ("{" in seg or "<" in seg) else seg
        for seg in path.strip("/").split("/")
        if seg
    ]


def router_prefixes(tree: ast.Module) -> dict[str, str]:
    """`router = APIRouter(prefix="/x")` / `Blueprint(url_prefix=...)` → {name: prefix}."""
    prefixes: dict[str, str] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)):
            continue
        for kw in node.value.keywords:
            if (
                kw.arg in ("prefix", "url_prefix")
                and isinstance(kw.value, ast.Constant)
                and isinstance(kw.value.value, str)
            ):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        prefixes[target.id] = kw.value.value
    return prefixes


def route_templates(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
    prefixes: dict[str, str] | None = None,
) -> list[tuple[set[str], str]]:
    """`(methods, path)` for each route decorator on *func*.

    Recognizes `@x.get("/p")` (any HTTP verb), `@x.route("/p", methods=[...])`,
    `@x.api_route` and `@x.websocket`, with the path positional or as
    `path=`. A router created in the same file with a `prefix=` /
    `url_prefix=` gets that prefix prepended.
    """
    found: list[tuple[set[str], str]] = []
    for dec in func.decorator_list:
        if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)):
            continue
        verb = dec.func.attr
        if verb not in ROUTE_DECORATORS:
            continue
        path = dec.args[0] if dec.args else next(
            (kw.value for kw in dec.keywords if kw.arg in ("path", "rule")), None
        )
        if not (isinstance(path, ast.Constant) and isinstance(path.value, str)):
            continue
        if not path.value.startswith("/"):
            continue
        if verb in HTTP_VERBS:
            methods = {verb}
        else:
            methods = {"get"}
            for kw in dec.keywords:
                if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
                    methods = {
                        e.value.lower()
                        for e in kw.value.elts
                        if isinstance(e, ast.Constant) and isinstance(e.value, str)
                    }
        prefix = ""
        if prefixes and isinstance(dec.func.value, ast.Name):
            prefix = prefixes.get(dec.func.value.id, "").rstrip("/")
        found.append((methods, prefix + path.value))
    return found


def build_route_index(source_files: list[Path], project_root: Path) -> list[dict]:
    """Every route decorator in *source_files*: `{file, function, line, segments}`."""
    routes: list[dict] = []
    for sf in source_files:
        tree = parse_source(sf)
        if tree is None:
            continue
        prefixes = router_prefixes(tree)
        rel = str(sf.relative_to(project_root))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for methods, path in route_templates(node, prefixes):
                    routes.append({
                        "file": rel,
                        "function": node.name,
                        "line": node.lineno,
                        "methods": methods,
                        "segments": template_segments(path),
                    })
    return routes


def best_routes(
    segments: list[str | None], routes: list[dict], verb: str | None = None
) -> list[dict]:
    """The most specific routes whose template is a suffix of *segments*.

    A template matches when each of its literal segments equals the URL's
    segment at the same position from the end (wildcards match anything) and
    at least one literal segment matched: the app may mount a router under a
    prefix (`/v1`) the decorator never spells. Most literal segments wins,
    then the longest template; ties are all returned.
    """
    best: list[dict] = []
    best_key = (0, 0)
    for route in routes:
        if verb is not None and verb not in route["methods"]:
            continue
        tmpl = route["segments"]
        if not tmpl or len(tmpl) > len(segments):
            continue
        tail = segments[len(segments) - len(tmpl):]
        literal = 0
        for want, got in zip(tmpl, tail):
            if want is None or got is None:
                continue
            if want != got:
                break
            literal += 1
        else:
            key = (literal, len(tmpl))
            if not literal or key < best_key:
                continue
            if key > best_key:
                best, best_key = [], key
            best.append(route)
    return best


def route_matched_sources(tree: ast.AST, routes: list[dict]) -> set[str]:
    """Source files whose routes a test file's URL literals reach."""
    if not routes:
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr) or (
            isinstance(node, ast.Constant) and isinstance(node.value, str)
        ):
            segments = path_segments(node)
            if segments:
                found.update(r["file"] for r in best_routes(segments, routes))
    return found


def _read_test_imports(filepath: Path, project_root: Path) -> list[str]:
    """Extract internal modules imported by a test file."""
    try:
        source = filepath.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(filepath))
    except SyntaxError:
        return []

    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                modules.append(alias.name)
    return modules


def pytest_testpaths(project_root: Path) -> list[Path]:
    """Directories named by the project's pytest `testpaths` setting.

    Reads the first pytest config pytest itself would use: `pytest.ini`,
    `pyproject.toml` (`[tool.pytest.ini_options]`), `tox.ini`, `setup.cfg`.
    Returns only entries that exist as directories; globs are expanded.
    """
    import configparser

    raw: list[str] | None = None
    for name, section in (
        ("pytest.ini", "pytest"),
        ("pyproject.toml", None),
        ("tox.ini", "pytest"),
        ("setup.cfg", "tool:pytest"),
    ):
        path = project_root / name
        if not path.is_file():
            continue
        if section is None:
            try:
                import tomllib
            except ImportError:  # Python 3.10: no TOML parser in the stdlib.
                continue
            try:
                data = tomllib.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            opts = data.get("tool", {}).get("pytest", {}).get("ini_options")
            if opts is None:
                continue
            value = opts.get("testpaths", [])
            raw = [value] if isinstance(value, str) else list(value)
            break
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(path, encoding="utf-8")
        except (configparser.Error, OSError, UnicodeDecodeError):
            continue
        if not parser.has_section(section):
            continue
        raw = parser.get(section, "testpaths", fallback="").split()
        break

    dirs: list[Path] = []
    for entry in raw or []:
        for match in sorted(project_root.glob(str(entry))):
            if match.is_dir() and match not in dirs:
                dirs.append(match)
    return dirs


def find_external_tests(
    scan_root: Path, project_root: Path
) -> tuple[list[Path], list[str], str | None]:
    """Find the project's tests when none live under *scan_root*.

    Returns `(test_files, roots, how)`: the test files, the directories they
    came from (relative to the project root), and `"pytest_testpaths"` or
    `"conventional_dir"` (`None` when nothing was found). The pytest
    `testpaths` setting wins; otherwise `tests/`, `test/` and `testing/` are
    tried at the project root and beside the scanned tree (CPython keeps
    `Lib/test/` beside `Lib/_pyrepl/`). A subdirectory dedicated to the
    scanned package is preferred: scanning all of CPython's Lib/test to review
    one package would be both slow and wrong.
    """
    package = scan_root.name

    def narrowed(root: Path) -> Path:
        targets = [
            root / f"test_{package}",
            root / f"test_{package.lstrip('_')}",
            root / package,
        ]
        return next((c for c in targets if c.is_dir()), root)

    configured = pytest_testpaths(project_root)
    if configured:
        files: list[Path] = []
        roots: list[str] = []
        for root in configured:
            candidate = narrowed(root)
            found = sorted(discover_python_files(candidate))
            if found:
                # Everything under a configured test path is test code, even
                # where the directory is not named `test`/`tests`.
                files.extend(f for f in found if f not in files)
                roots.append(str(candidate.relative_to(project_root)))
        if files:
            return files, roots, "pytest_testpaths"

    for base in (project_root, scan_root.parent):
        for name in ("tests", "test", "testing"):
            root = base / name
            if not root.is_dir():
                continue
            candidate = narrowed(root)
            found = sorted(discover_python_files(candidate))
            _, discovered = classify_files(found, project_root)
            if discovered:
                return (
                    discovered,
                    [str(candidate.relative_to(project_root))],
                    "conventional_dir",
                )
    return [], [], None


def module_name(path: Path, project_root: Path) -> str:
    """Dotted import name of *path*, rooted at its topmost package.

    Leading directories that are not packages (`src/`, CPython's `Lib/`) are
    not part of the import name: `src/pkg/a.py` is `pkg.a`, and
    `pkg/sub/__init__.py` is `pkg.sub`.
    """
    rel = path.relative_to(project_root).with_suffix("")
    parts = list(rel.parts)
    depth = 0
    parent = path.parent
    while parent != project_root and (parent / "__init__.py").is_file():
        depth += 1
        parent = parent.parent
    parts = parts[-(depth + 1):]
    if parts[-1] == "__init__" and len(parts) > 1:
        parts = parts[:-1]
    return ".".join(parts)


def build_module_index(
    source_files: list[Path], project_root: Path
) -> dict[str, list[str]]:
    """Map dotted module names to the source files that define them.

    Each file is indexed under its package-rooted name and, when different,
    under its plain path from the project root (minus a leading `src`), so a
    namespace package without `__init__.py` still resolves.
    """
    index: dict[str, list[str]] = {}
    for sf in source_files:
        rel = str(sf.relative_to(project_root))
        names = {module_name(sf, project_root)}
        parts = list(sf.relative_to(project_root).with_suffix("").parts)
        if parts[-1] == "__init__" and len(parts) > 1:
            parts = parts[:-1]
        if parts[0] == "src" and len(parts) > 1:
            parts = parts[1:]
        names.add(".".join(parts))
        for name in names:
            index.setdefault(name, []).append(rel)
    return index


def build_reexport_index(
    source_files: list[Path], project_root: Path
) -> dict[str, dict[str, str]]:
    """Names each package `__init__.py` re-exports from its submodules.

    `{"pkg.sub": {"Thing": "pkg.sub.thing.Thing"}}` for a `pkg/sub/__init__.py`
    holding `from .thing import Thing` (or the absolute spelling), so a test's
    `from pkg.sub import Thing` correlates with `pkg/sub/thing.py`.
    """
    index: dict[str, dict[str, str]] = {}
    for sf in source_files:
        if sf.name != "__init__.py":
            continue
        tree = parse_source(sf)
        if tree is None:
            continue
        package = module_name(sf, project_root)
        names: dict[str, str] = {}
        for node in tree.body:
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.level:
                base = package.split(".")
                if node.level > 1:
                    base = base[: len(base) - (node.level - 1)]
                module = ".".join(base + ([node.module] if node.module else []))
            else:
                module = node.module or ""
            for alias in node.names:
                if module and alias.name != "*":
                    names[alias.asname or alias.name] = f"{module}.{alias.name}"
        if names:
            index[package] = names
    return index


def resolve_imported_sources(
    tree: ast.AST,
    index: dict[str, list[str]],
    package: str = "",
    reexports: dict[str, dict[str, str]] | None = None,
) -> dict[str, str]:
    """Source files a module imports, as `{source_rel_path: import_name}`.

    Handles `import a.b`, `import a.b as x`, `from a.b import c` (where `c` may
    be a submodule or a name defined in `a.b`), and relative imports resolved
    against *package* (the importing module's package). Imports inside
    functions count too: pytest tests often import there. With *reexports*
    (see `build_reexport_index`), `from pkg import Thing` also reaches the
    submodule `pkg/__init__.py` took `Thing` from.
    """
    found: dict[str, str] = {}
    reexports = reexports or {}

    def add(name: str) -> bool:
        for src in index.get(name, ()):
            found.setdefault(src, name)
        return name in index

    def add_reexport(module: str, name: str) -> None:
        for _ in range(3):  # a re-export of a re-export, at most
            target = reexports.get(module, {}).get(name)
            if target is None:
                return
            module, _, name = target.rpartition(".")
            if add(target):  # `from .sub import submodule`
                return
            if add(module) and name not in reexports.get(module, {}):
                return

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                # `import a.b.c` where only `a.b` is a source file.
                while parts and not add(".".join(parts)):
                    parts.pop()
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".") if package else []
                if node.level > 1:
                    base = base[: len(base) - (node.level - 1)]
                module = ".".join(base + ([node.module] if node.module else []))
            else:
                module = node.module or ""
            if not module:
                continue
            submodule_hit = False
            for alias in node.names:
                if alias.name != "*" and add(f"{module}.{alias.name}"):
                    submodule_hit = True
                elif alias.name != "*":
                    add_reexport(module, alias.name)
            if not submodule_hit or any(
                f"{module}.{a.name}" not in index for a in node.names
            ):
                add(module)
    return found


def analyze(target: str, *, max_files: int = 0) -> dict:
    """Correlate the source and test files under *target*; return the report."""
    target_path = Path(target).resolve()
    project_root = find_project_root(target_path)
    scan_root = target_path if target_path.is_dir() else project_root
    all_files = sorted(discover_python_files(scan_root))
    files_total = len(all_files)
    if max_files > 0 and files_total > max_files:
        all_files = all_files[:max_files]
    files = all_files

    test_named_sources: list[Path] = []
    source_files, test_files = classify_files(
        files, project_root, test_named_sources
    )
    warnings: list[str] = []
    if test_named_sources:
        warnings.append(
            "Named like test modules but define no tests, so counted as source: "
            + ", ".join(str(f.relative_to(project_root)) for f in test_named_sources)
        )

    # A package is very often reviewed on its own (`coverage/`, `Lib/_pyrepl`)
    # while its tests live in a SIBLING tree. Scanning only the target then finds
    # zero test files and reports 0% coverage for a well-tested project -- which
    # reads as a finding about the project when it is an artefact of the scope.
    # Test files that define no test (a lone `tests/__init__.py`) do not count.
    external_test_roots: list[str] = []
    if not any(defines_tests(f) for f in test_files) and scan_root != project_root:
        discovered, external_test_roots, how = find_external_tests(
            scan_root, project_root
        )
        if discovered:
            test_files = discovered
            warnings.append(
                f"No tests under the scan root; tests were taken from "
                f"{', '.join(external_test_roots)} ({how})."
            )
    if not test_files:
        warnings.append(
            "No test files found under the scan root or in the project's test "
            "directories (pytest testpaths, tests/, test/). Coverage was not "
            "measured: this is not a clean result."
        )

    # Analyze source files.
    source_info: dict[str, dict] = {}
    for sf in source_files:
        rel = str(sf.relative_to(project_root))
        info = _extract_source_info(sf)
        source_info[rel] = info

    # Analyze test files and build correlations.
    module_index = build_module_index(source_files, project_root)
    reexports = build_reexport_index(source_files, project_root)
    routes = build_route_index(source_files, project_root)
    test_info: dict[str, dict] = {}
    support_info: dict[str, dict] = {}
    for tf in test_files:
        rel = str(tf.relative_to(project_root))
        tinfo = _extract_test_info(tf)
        # Only files that define tests are test files. conftest.py, helpers
        # and a test package's __init__.py are support files: their imports
        # are reported (a conftest building the app imports half the
        # project) but never make a source module "tested".
        has_tests = bool(
            tinfo["standalone_tests"]
            or tinfo["skipped_standalone_tests"]
            or any(c["total_test_methods"] for c in tinfo["classes"])
        )
        tree = None if tinfo["parse_error"] else parse_source(tf)
        imported_sources = sorted(
            resolve_imported_sources(
                tree,
                module_index,
                module_name(tf, project_root).rpartition(".")[0],
                reexports,
            )
            if tree is not None
            else []
        )
        if not has_tests and not tinfo["parse_error"]:
            if tf.name == "conftest.py":
                kind = "conftest"
            elif tf.name == "__init__.py":
                kind = "package_init"
            else:
                kind = "helper"
            support_info[rel] = {"kind": kind, "imported_sources": imported_sources}
            continue
        tinfo["matched_sources"] = _match_test_to_source(tf, source_files, project_root)
        tinfo["imported_modules"] = _read_test_imports(tf, project_root)
        # Import-based correspondence. Feature-named suites (`test_*_race.py`
        # for `storage/engine.py`) never match by file name, but they import
        # the module under test.
        tinfo["imported_sources"] = imported_sources
        # Route handlers driven through a test client: the test imports the
        # app, never the handler's module, but its URL literals name it.
        tinfo["route_matched_sources"] = sorted(
            route_matched_sources(tree, routes) if tree is not None else []
        )
        test_info[rel] = tinfo

    # Build the correlation map: source → tests, remembering how each matched.
    source_to_tests: dict[str, list[str]] = {s: [] for s in source_info}
    by_support: dict[str, list[str]] = {s: [] for s in source_info}
    for support_path, sinfo in support_info.items():
        for src in sinfo["imported_sources"]:
            if src in by_support:
                by_support[src].append(support_path)
    by_name: dict[str, list[str]] = {s: [] for s in source_info}
    by_import: dict[str, list[str]] = {s: [] for s in source_info}
    by_route: dict[str, list[str]] = {s: [] for s in source_info}
    for test_path, tinfo in test_info.items():
        for src in tinfo["matched_sources"]:
            if src in by_name:
                by_name[src].append(test_path)
        for src in tinfo["imported_sources"]:
            if src in by_import:
                by_import[src].append(test_path)
        for src in tinfo["route_matched_sources"]:
            if src in by_route:
                by_route[src].append(test_path)
    for src in source_info:
        source_to_tests[src] = sorted(
            set(by_name[src]) | set(by_import[src]) | set(by_route[src])
        )

    # Identify untested source files.
    untested = [s for s, tests in source_to_tests.items() if not tests]

    # Compute per-source test counts.
    source_coverage: list[dict] = []
    for src, tests in sorted(source_to_tests.items()):
        total_test_methods = 0
        total_skipped = 0
        for t in tests:
            ti = test_info[t]
            for cls in ti["classes"]:
                total_test_methods += len(cls["test_methods"])
                total_skipped += len(cls["skipped_methods"])
            total_test_methods += len(ti["standalone_tests"])
            total_skipped += len(ti["skipped_standalone_tests"])

        src_info = source_info[src]
        public_surface = (
            len(src_info["functions"])
            + sum(len(c["public_methods"]) for c in src_info["classes"])
        )

        match_method = "+".join(
            kind
            for kind, hits in (
                ("name", by_name[src]),
                ("import", by_import[src]),
                ("route", by_route[src]),
            )
            if hits
        ) or None
        source_coverage.append({
            "source_file": src,
            "test_files": tests,
            # How the correspondence was found: the test file's name matches
            # the source's ("name"), a test file imports it ("import"), a
            # test's URL literal reaches one of its route decorators
            # ("route"), or a "+"-joined combination. A name match is the
            # strongest signal.
            "match_method": match_method,
            "name_matched_test_files": by_name[src],
            "import_matched_test_files": by_import[src],
            "route_matched_test_files": by_route[src],
            # conftest.py / helper files importing this module. Not counted
            # in has_tests: an import for a fixture tests nothing by itself.
            "support_imported_by": by_support[src],
            "test_method_count": total_test_methods,
            "skipped_test_count": total_skipped,
            "public_classes": len(src_info["classes"]),
            "public_functions": len(src_info["functions"]),
            "public_surface_size": public_surface,
            "has_tests": bool(tests),
        })

    # Summary.
    total_source = len(source_info)
    tested = total_source - len(untested)
    # Suite-wide counts. These used to be summed over `source_coverage`, which
    # counts only correlated test files (about a fifth of a feature-named
    # pytest suite) and counts
    # a test file twice when it correlates with two sources.
    suite_test_methods = 0
    suite_skipped = 0
    marked: dict[str, int] = {}
    runtime_skips = 0
    module_skips: list[str] = []
    for rel, ti in test_info.items():
        for name, count in ti["markers"].items():
            marked[name] = marked.get(name, 0) + count
        runtime_skips += len(ti["runtime_skip_tests"])
        if ti["module_skip"]:
            module_skips.append(rel)
        suite_test_methods += len(ti["standalone_tests"])
        suite_skipped += len(ti["skipped_standalone_tests"])
        for cls in ti["classes"]:
            suite_test_methods += len(cls["test_methods"])
            suite_skipped += len(cls["skipped_methods"])

    return {
        "project_root": str(project_root),
        "scan_root": str(scan_root),
        "files_total": files_total,
        "files_analyzed": len(all_files),
        "files_capped": max_files > 0 and files_total > max_files,
        # "no_tests_found": no test file anywhere, so every coverage number
        # below is unmeasured, not zero.
        "status": "ok" if test_files else "no_tests_found",
        "warnings": warnings,
        "summary": {
            "source_files": total_source,
            # Files that define tests; conftest.py, helpers and test-package
            # __init__.py files are counted separately.
            "test_files": len(test_info),
            "support_files": len(support_info),
            "source_files_with_tests": tested,
            "source_files_without_tests": len(untested),
            "source_files_with_tests_by_name": sum(1 for v in by_name.values() if v),
            "source_files_with_tests_by_import_only": sum(
                1 for s in source_info if by_import[s] and not by_name[s]
            ),
            "source_files_with_tests_by_route_only": sum(
                1
                for s in source_info
                if by_route[s] and not by_name[s] and not by_import[s]
            ),
            "coverage_percentage": (
                round(100 * tested / total_source, 1)
                if total_source and test_files
                else None
            ),
            # Every test function and method in every test file found
            # (pytest-style module-level functions and plain-class methods
            # included), skipped ones excluded; correlated or not.
            "total_test_methods": suite_test_methods,
            "total_skipped_tests": suite_skipped,
            # Tests carrying each pytest mark (`slow`, `network`, ...): the
            # ones a `-m "not slow"` run leaves out.
            "marked_tests": dict(sorted(marked.items())),
            # Skips decided at run time, invisible to the counts above: tests
            # whose body calls pytest.skip(), and files that may skip at
            # import (module-level pytest.skip / importorskip).
            "tests_with_runtime_skip": runtime_skips,
            "files_with_module_level_skip": sorted(module_skips),
            # Non-empty when the tests were found OUTSIDE the scanned tree, so a
            # reader can tell "this package has no tests" from "the tests live
            # somewhere else and the scope missed them".
            "external_test_roots": external_test_roots,
            # Source modules named `test_*.py` that define no tests.
            "test_named_source_files": sorted(
                str(f.relative_to(project_root)) for f in test_named_sources
            ),
        },
        "untested_sources": sorted(untested),
        "source_coverage": source_coverage,
        "test_details": test_info,
        "support_files": support_info,
    }


def main() -> None:
    from scan_common import emit, parse_common_args

    target, max_files = parse_common_args(sys.argv[1:], __doc__)
    emit(analyze(target, max_files=max_files))


if __name__ == "__main__":
    main()
