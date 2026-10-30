#!/usr/bin/env python3
"""Correlate source modules with their test files.

Maps each source file to its corresponding test file(s), counts test classes
and methods, and identifies source files with no test coverage.

Usage:
    python correlate_tests.py [path]
"""

import ast
import json
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


def _extract_test_info(filepath: Path) -> dict:
    """Extract test classes and methods from a test file."""
    try:
        source = filepath.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(filepath))
    except SyntaxError:
        return {"classes": [], "standalone_tests": [], "parse_error": True}

    classes: list[dict] = []
    standalone: list[str] = []

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            methods = []
            skipped = []
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if child.name.startswith("test"):
                        is_skipped = any(
                            (isinstance(d, ast.Attribute) and "skip" in d.attr.lower())
                            or (isinstance(d, ast.Call)
                                and isinstance(d.func, ast.Attribute)
                                and "skip" in d.func.attr.lower())
                            or (isinstance(d, ast.Name) and "skip" in d.id.lower())
                            for d in child.decorator_list
                        )
                        if is_skipped:
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
                standalone.append(node.name)

    return {
        "classes": classes,
        "standalone_tests": standalone,
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


def main() -> None:
    from scan_common import exit_on_help

    exit_on_help(sys.argv[1:], __doc__)
    max_files = 0  # 0 = no limit
    positional: list[str] = []
    argv = sys.argv[1:]
    i = 0
    while i < len(argv):
        if argv[i] == "--max-files" and i + 1 < len(argv):
            max_files = int(argv[i + 1])
            i += 2
        elif argv[i].startswith("--"):
            i += 1
        else:
            positional.append(argv[i])
            i += 1
    target = Path(positional[0]) if positional else Path(".")
    target = target.resolve()

    project_root = find_project_root(target)
    scan_root = target if target.is_dir() else project_root
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
        package = scan_root.name
        # Search the project root AND the scanned tree's own parent: CPython puts
        # `Lib/test/` beside `Lib/_pyrepl/`, not at the repository root.
        for base in (project_root, scan_root.parent):
            for name in ("tests", "test", "testing"):
                root = base / name
                if not root.is_dir():
                    continue
                # Prefer a subdirectory dedicated to this package -- scanning all
                # of CPython's Lib/test to review one package would be both slow
                # and wrong.
                targets = [
                    root / f"test_{package}",
                    root / f"test_{package.lstrip('_')}",
                    root / package,
                ]
                candidate = next((c for c in targets if c.is_dir()), root)
                found = sorted(discover_python_files(candidate))
                _, discovered = classify_files(found, project_root)
                if not discovered:
                    continue
                test_files.extend(discovered)
                external_test_roots.append(str(candidate.relative_to(project_root)))
                break
            if external_test_roots:
                break

    # Analyze source files.
    source_info: dict[str, dict] = {}
    for sf in source_files:
        rel = str(sf.relative_to(project_root))
        info = _extract_source_info(sf)
        source_info[rel] = info

    # Analyze test files and build correlations.
    test_info: dict[str, dict] = {}
    for tf in test_files:
        rel = str(tf.relative_to(project_root))
        tinfo = _extract_test_info(tf)
        matched_sources = _match_test_to_source(tf, source_files, project_root)
        imported_modules = _read_test_imports(tf, project_root)
        tinfo["matched_sources"] = matched_sources
        tinfo["imported_modules"] = imported_modules
        test_info[rel] = tinfo

    # Build the correlation map: source → tests.
    source_to_tests: dict[str, list[str]] = {s: [] for s in source_info}
    for test_path, tinfo in test_info.items():
        for src in tinfo["matched_sources"]:
            if src in source_to_tests:
                source_to_tests[src].append(test_path)

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

        src_info = source_info[src]
        public_surface = (
            len(src_info["functions"])
            + sum(len(c["public_methods"]) for c in src_info["classes"])
        )

        source_coverage.append({
            "source_file": src,
            "test_files": tests,
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

    output = {
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
            "coverage_percentage": (
                round(100 * tested / total_source, 1) if total_source else 0
            ),
            "total_test_methods": sum(
                sc["test_method_count"] for sc in source_coverage
            ),
            "total_skipped_tests": sum(
                sc["skipped_test_count"] for sc in source_coverage
            ),
            # Non-empty when the tests were found OUTSIDE the scanned tree, so a
            # reader can tell "this package has no tests" from "the tests live
            # somewhere else and the scope missed them".
            "external_test_roots": external_test_roots,
        },
        "untested_sources": sorted(untested),
        "source_coverage": source_coverage,
        "test_details": test_info,
    }

    json.dump(output, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
