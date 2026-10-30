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
)


def classify_files(
    files: list[Path], project_root: Path
) -> tuple[list[Path], list[Path]]:
    """Split files into source and test files."""
    source: list[Path] = []
    test: list[Path] = []
    for f in files:
        rel = f.relative_to(project_root)
        parts = rel.parts
        is_test = (
            f.name.startswith("test_")
            or f.name.endswith("_test.py")
            or "test" in parts[:-1]
            or "tests" in parts[:-1]
        )
        if is_test:
            test.append(f)
        else:
            # Skip setup.py, conftest.py, and __init__.py in test dirs.
            if f.name in ("setup.py", "conftest.py"):
                continue
            source.append(f)
    return source, test


def _is_skipped(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True for `@unittest.skip...`, `@pytest.mark.skip/skipif` and the like."""
    return any(
        (isinstance(d, ast.Attribute) and "skip" in d.attr.lower())
        or (isinstance(d, ast.Call)
            and isinstance(d.func, ast.Attribute)
            and "skip" in d.func.attr.lower())
        or (isinstance(d, ast.Name) and "skip" in d.id.lower())
        for d in func.decorator_list
    )


def _extract_test_info(filepath: Path) -> dict:
    """Extract test classes and methods from a test file.

    Counts unittest `TestCase` methods, methods of plain pytest classes and
    pytest-style module-level `test*` functions alike.
    """
    try:
        source = filepath.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(filepath))
    except SyntaxError:
        return {
            "classes": [],
            "standalone_tests": [],
            "skipped_standalone_tests": [],
            "parse_error": True,
        }

    classes: list[dict] = []
    standalone: list[str] = []
    skipped_standalone: list[str] = []

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            methods = []
            skipped = []
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if child.name.startswith("test"):
                        if _is_skipped(child):
                            skipped.append(child.name)
                        else:
                            methods.append(child.name)

            classes.append({
                "name": node.name,
                "test_methods": methods,
                "skipped_methods": skipped,
                "total_test_methods": len(methods) + len(skipped),
                "line_start": node.lineno,
            })
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test"):
                if _is_skipped(node):
                    skipped_standalone.append(node.name)
                else:
                    standalone.append(node.name)

    return {
        "classes": classes,
        "standalone_tests": standalone,
        "skipped_standalone_tests": skipped_standalone,
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


def _match_test_to_source(
    test_file: Path, source_files: list[Path], project_root: Path
) -> list[str]:
    """Find source files that a test file likely corresponds to.

    Heuristics:
    - test_runner.py → runner.py
    - tests/test_runner.py → src/runner.py or package/runner.py
    - tests/subpkg/test_foo.py → package/subpkg/foo.py
    """
    matches: list[str] = []
    test_rel = test_file.relative_to(project_root)
    test_name = test_file.stem  # e.g. "test_runner"

    # Strip test_ prefix or _test suffix.
    if test_name.startswith("test_"):
        source_stem = test_name[5:]
    elif test_name.endswith("_test"):
        source_stem = test_name[:-5]
    else:
        source_stem = test_name

    for sf in source_files:
        sf_rel = sf.relative_to(project_root)
        # Direct stem match.
        if sf.stem == source_stem:
            matches.append(str(sf_rel))
            continue
        # Also try matching subpackage structure.
        # e.g. tests/benchmarks/test_runner.py → package/benchmarks/runner.py
        test_parts = list(test_rel.parts[:-1])  # directory parts of test
        source_parts = list(sf_rel.parts[:-1])
        # Check if the non-test/tests parts match.
        test_clean = [p for p in test_parts if p not in ("test", "tests")]
        source_clean = [p for p in source_parts if p not in ("src",)]
        if (sf.stem == source_stem
                and test_clean and source_clean
                and test_clean[-len(source_clean):] == source_clean[-len(test_clean):]):
            matches.append(str(sf_rel))

    # Deduplicate preserving order.
    seen: set[str] = set()
    deduped: list[str] = []
    for m in matches:
        if m not in seen:
            seen.add(m)
            deduped.append(m)
    return deduped


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


def resolve_imported_sources(
    tree: ast.AST, index: dict[str, list[str]], package: str = ""
) -> dict[str, str]:
    """Source files a module imports, as `{source_rel_path: import_name}`.

    Handles `import a.b`, `import a.b as x`, `from a.b import c` (where `c` may
    be a submodule or a name defined in `a.b`), and relative imports resolved
    against *package* (the importing module's package). Imports inside
    functions count too: pytest tests often import there.
    """
    found: dict[str, str] = {}

    def add(name: str) -> bool:
        for src in index.get(name, ()):
            found.setdefault(src, name)
        return name in index

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

    source_files, test_files = classify_files(files, project_root)

    # A package is very often reviewed on its own (`coverage/`, `Lib/_pyrepl`)
    # while its tests live in a SIBLING tree. Scanning only the target then finds
    # zero test files and reports 0% coverage for a well-tested project -- which
    # reads as a finding about the project when it is an artefact of the scope.
    external_test_roots: list[str] = []
    if not test_files and scan_root != project_root:
        discovered, external_test_roots, _ = find_external_tests(
            scan_root, project_root
        )
        test_files.extend(discovered)

    # Analyze source files.
    source_info: dict[str, dict] = {}
    for sf in source_files:
        rel = str(sf.relative_to(project_root))
        info = _extract_source_info(sf)
        source_info[rel] = info

    # Analyze test files and build correlations.
    module_index = build_module_index(source_files, project_root)
    test_info: dict[str, dict] = {}
    for tf in test_files:
        rel = str(tf.relative_to(project_root))
        tinfo = _extract_test_info(tf)
        matched_sources = _match_test_to_source(tf, source_files, project_root)
        imported_modules = _read_test_imports(tf, project_root)
        tinfo["matched_sources"] = matched_sources
        tinfo["imported_modules"] = imported_modules
        # Import-based correspondence. Feature-named suites (`test_*_race.py`
        # for `storage/engine.py`) never match by file name, but they import
        # the module under test. Only files that define tests count: a
        # conftest.py or helper module importing the app tests nothing.
        has_tests = bool(
            tinfo["standalone_tests"]
            or tinfo["skipped_standalone_tests"]
            or any(c["total_test_methods"] for c in tinfo["classes"])
        )
        tree = None
        if has_tests and not tinfo["parse_error"]:
            try:
                tree = ast.parse(tf.read_text(encoding="utf-8", errors="replace"))
            except (SyntaxError, ValueError):
                tree = None
        tinfo["imported_sources"] = sorted(
            resolve_imported_sources(
                tree, module_index, module_name(tf, project_root).rpartition(".")[0]
            )
            if tree is not None
            else []
        )
        test_info[rel] = tinfo

    # Build the correlation map: source → tests, remembering how each matched.
    source_to_tests: dict[str, list[str]] = {s: [] for s in source_info}
    by_name: dict[str, list[str]] = {s: [] for s in source_info}
    by_import: dict[str, list[str]] = {s: [] for s in source_info}
    for test_path, tinfo in test_info.items():
        for src in tinfo["matched_sources"]:
            if src in by_name:
                by_name[src].append(test_path)
        for src in tinfo["imported_sources"]:
            if src in by_import:
                by_import[src].append(test_path)
    for src in source_info:
        source_to_tests[src] = sorted(set(by_name[src]) | set(by_import[src]))

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

        if by_name[src] and by_import[src]:
            match_method = "name+import"
        elif by_name[src]:
            match_method = "name"
        elif by_import[src]:
            match_method = "import"
        else:
            match_method = None
        source_coverage.append({
            "source_file": src,
            "test_files": tests,
            # How the correspondence was found: the test file's name matches
            # the source's ("name"), a test file imports it ("import"), or
            # both. A name match is the stronger signal.
            "match_method": match_method,
            "name_matched_test_files": by_name[src],
            "import_matched_test_files": by_import[src],
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
    for ti in test_info.values():
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
        "summary": {
            "source_files": total_source,
            "test_files": len(test_info),
            "source_files_with_tests": tested,
            "source_files_without_tests": len(untested),
            "source_files_with_tests_by_name": sum(1 for v in by_name.values() if v),
            "source_files_with_tests_by_import_only": sum(
                1 for s in source_info if by_import[s] and not by_name[s]
            ),
            "coverage_percentage": (
                round(100 * tested / total_source, 1) if total_source else 0
            ),
            # Every test function and method in every test file found
            # (pytest-style module-level functions and plain-class methods
            # included), skipped ones excluded; correlated or not.
            "total_test_methods": suite_test_methods,
            "total_skipped_tests": suite_skipped,
            # Non-empty when the tests were found OUTSIDE the scanned tree, so a
            # reader can tell "this package has no tests" from "the tests live
            # somewhere else and the scope missed them".
            "external_test_roots": external_test_roots,
        },
        "untested_sources": sorted(untested),
        "source_coverage": source_coverage,
        "test_details": test_info,
    }


def main() -> None:
    from scan_common import emit, parse_common_args

    target, max_files = parse_common_args(sys.argv[1:], __doc__)
    emit(analyze(target, max_files=max_files))


if __name__ == "__main__":
    main()
