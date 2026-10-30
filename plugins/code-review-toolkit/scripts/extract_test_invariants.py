#!/usr/bin/env python3
"""Extract test invariants for propagation analysis.

Parses test files to extract assertions, classify the invariants they encode,
identify the source functions they test, and find structurally similar functions
that should satisfy the same invariants but may not be tested.

Usage:
    python extract_test_invariants.py [path] [--max-files N] [--with-git]
"""

import ast
import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scan_common import (  # noqa: E402
    discover_python_files,
    find_project_root,
)


# ---------------------------------------------------------------------------
# Project discovery (adapted from correlate_tests.py)
# ---------------------------------------------------------------------------

def classify_files(
    files: list[Path], project_root: Path
) -> tuple[list[Path], list[Path]]:
    """Split files into (source, test) lists."""
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
        elif f.name not in ("setup.py", "conftest.py"):
            source.append(f)
    return source, test


# ---------------------------------------------------------------------------
# Assertion extraction
# ---------------------------------------------------------------------------

# Assertion method → invariant type mapping
_ASSERT_INVARIANT_MAP: dict[str, str] = {
    "assertRaises": "error_condition",
    "assertRaisesRegex": "error_condition",
    "assertWarns": "warning_condition",
    "assertWarnsRegex": "warning_condition",
    "assertEqual": "equality",
    "assertNotEqual": "inequality",
    "assertTrue": "truthiness",
    "assertFalse": "falsiness",
    "assertIs": "identity",
    "assertIsNot": "non_identity",
    "assertIsNone": "nullability",
    "assertIsNotNone": "non_nullability",
    "assertIn": "membership",
    "assertNotIn": "non_membership",
    "assertIsInstance": "type_check",
    "assertNotIsInstance": "type_exclusion",
    "assertGreater": "ordering",
    "assertGreaterEqual": "ordering",
    "assertLess": "ordering",
    "assertLessEqual": "ordering",
    "assertAlmostEqual": "approximate_equality",
    "assertRegex": "pattern_match",
    "assertNotRegex": "pattern_exclusion",
    "assertCountEqual": "collection_equality",
    "assertSequenceEqual": "sequence_equality",
    "assertListEqual": "sequence_equality",
    "assertTupleEqual": "sequence_equality",
    "assertSetEqual": "set_equality",
    "assertDictEqual": "dict_equality",
}

# pytest patterns
_PYTEST_RAISES_NAMES = {"raises", "warns", "deprecated_call"}

# High-signal test name patterns
_HIGH_SIGNAL_PATTERNS = re.compile(
    r"(?:error|invalid|empty|none|null|negative|overflow|underflow|"
    r"boundary|edge|corner|fail|crash|corrupt|malform|bad|broken|"
    r"cleanup|close|teardown|shutdown|concurrent|thread|async|race)",
    re.IGNORECASE,
)


def _get_call_name(node: ast.expr) -> str | None:
    """Extract the function/method name from a Call node's func."""
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def extract_assertions(func_node: ast.FunctionDef) -> list[dict]:
    """Extract assertion calls and classify them from a test function AST."""
    assertions: list[dict] = []

    for node in ast.walk(func_node):
        if not isinstance(node, ast.Call):
            continue

        call_name = _get_call_name(node.func)
        if call_name is None:
            continue

        # unittest-style: self.assertXxx(...)
        if call_name in _ASSERT_INVARIANT_MAP:
            inv_type = _ASSERT_INVARIANT_MAP[call_name]
            # For assertRaises, extract the exception type
            detail = ""
            if call_name in ("assertRaises", "assertRaisesRegex") and node.args:
                exc_arg = node.args[0]
                if isinstance(exc_arg, ast.Name):
                    detail = exc_arg.id
                elif isinstance(exc_arg, ast.Attribute):
                    detail = exc_arg.attr
            assertions.append({
                "method": call_name,
                "invariant_type": inv_type,
                "line": node.lineno,
                "detail": detail,
                "is_implementation_detail": False,
            })

        # pytest.raises / pytest.warns
        elif call_name in _PYTEST_RAISES_NAMES:
            exc_detail = ""
            if node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Name):
                    exc_detail = arg.id
                elif isinstance(arg, ast.Attribute):
                    exc_detail = arg.attr
            assertions.append({
                "method": f"pytest.{call_name}",
                "invariant_type": "error_condition" if call_name == "raises" else "warning_condition",
                "line": node.lineno,
                "detail": exc_detail,
                "is_implementation_detail": False,
            })

        # Plain assert statements are captured too (via ast.Assert, not Call)
        # Mock assertions are implementation details
        elif call_name in (
            "assert_called_with", "assert_called_once_with",
            "assert_called", "assert_called_once",
            "assert_not_called", "assert_any_call",
        ):
            assertions.append({
                "method": call_name,
                "invariant_type": "mock_interaction",
                "line": node.lineno,
                "detail": "",
                "is_implementation_detail": True,
            })

    # Also capture bare assert statements
    for node in ast.walk(func_node):
        if isinstance(node, ast.Assert):
            assertions.append({
                "method": "assert",
                "invariant_type": "general",
                "line": node.lineno,
                "detail": "",
                "is_implementation_detail": False,
            })

    return assertions


# ---------------------------------------------------------------------------
# Test → source mapping
# ---------------------------------------------------------------------------

def _extract_imports(tree: ast.Module) -> dict[str, str]:
    """Extract import-name → module-path mapping from a module AST."""
    imports: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.asname or alias.name
                imports[name] = alias.name
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                name = alias.asname or alias.name
                imports[name] = f"{module}.{alias.name}" if module else alias.name
    return imports


def _first_significant_call(func_node: ast.FunctionDef) -> str | None:
    """Find the first non-assertion, non-setup call in a test function body."""
    for node in ast.walk(func_node):
        if not isinstance(node, ast.Call):
            continue
        name = _get_call_name(node.func)
        if name is None:
            continue
        # Skip assertions, setup, and common test helpers
        if name.startswith("assert") or name.startswith("mock") or name in (
            "setUp", "tearDown", "patch", "MagicMock", "Mock",
            "skipTest", "skipIf", "skipUnless",
        ):
            continue
        return name
    return None


def resolve_tested_function(
    test_file: Path, func_node: ast.FunctionDef, tree: ast.Module,
    source_functions: dict[str, list[dict]],
) -> dict | None:
    """Try to identify the source function being tested.

    Strategy: check test function name (test_X → X), then check imports,
    then check first significant call in the test body.
    """
    # Strategy 1: test name convention (test_foo → foo)
    name = func_node.name
    if name.startswith("test_"):
        candidate = name[5:]  # strip "test_"
        # Strip trailing qualifiers like _empty, _invalid, _returns_none
        base = candidate.split("_")[0] if "_" in candidate else candidate
        for full_name, locations in source_functions.items():
            short = full_name.rsplit(".", 1)[-1] if "." in full_name else full_name
            if short == candidate or short == base:
                loc = locations[0]
                return {
                    "function": full_name,
                    "file": loc["file"],
                    "line": loc["line"],
                    "match_method": "test_name_convention",
                }

    # Strategy 2: first significant call in the test body
    first_call = _first_significant_call(func_node)
    if first_call:
        for full_name, locations in source_functions.items():
            short = full_name.rsplit(".", 1)[-1] if "." in full_name else full_name
            if short == first_call:
                loc = locations[0]
                return {
                    "function": full_name,
                    "file": loc["file"],
                    "line": loc["line"],
                    "match_method": "first_call",
                }

    return None


# ---------------------------------------------------------------------------
# Source function extraction and similarity
# ---------------------------------------------------------------------------

def extract_source_functions(source_files: list[Path], project_root: Path) -> dict[str, list[dict]]:
    """Extract all function/method definitions from source files.

    Returns {qualified_name: [{file, line, params, is_method}]}.
    """
    functions: dict[str, list[dict]] = defaultdict(list)

    for filepath in source_files:
        try:
            source = filepath.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=str(filepath))
        except SyntaxError:
            continue

        rel = str(filepath.relative_to(project_root))

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Build qualified name
                params = [a.arg for a in node.args.args if a.arg != "self"]
                is_method = any(
                    a.arg == "self" for a in node.args.args
                )

                # Get parent class name if method
                # (simplified — checks direct parent)
                parent_name = ""
                for parent in ast.walk(tree):
                    if isinstance(parent, ast.ClassDef):
                        if node in ast.iter_child_nodes(parent):
                            parent_name = parent.name
                            break

                qualified = f"{parent_name}.{node.name}" if parent_name else node.name

                functions[qualified].append({
                    "file": rel,
                    "line": node.lineno,
                    "params": params,
                    "param_count": len(params),
                    "is_method": is_method,
                    "is_async": isinstance(node, ast.AsyncFunctionDef),
                    "name": node.name,
                })

    return dict(functions)


def find_similar_functions(
    func_name: str, func_info: dict,
    all_functions: dict[str, list[dict]],
    max_similar: int = 5,
) -> list[dict]:
    """Find functions structurally similar to the given one.

    Similarity is based on name pattern and parameter count.
    """
    results: list[dict] = []
    base_name = func_info.get("name", func_name.rsplit(".", 1)[-1])
    base_params = func_info.get("param_count", 0)

    # Extract the verb prefix (validate_, parse_, process_, handle_, etc.)
    parts = base_name.split("_")
    prefix = parts[0] if parts else ""

    for qname, locations in all_functions.items():
        if qname == func_name:
            continue

        short = qname.rsplit(".", 1)[-1] if "." in qname else qname
        loc = locations[0]

        # Same file is less interesting — different files are higher signal
        same_file = loc["file"] == func_info.get("file", "")

        # Similarity scoring
        score = 0

        # Name prefix match (validate_input ↔ validate_config)
        other_parts = short.split("_")
        if other_parts and other_parts[0] == prefix and len(prefix) > 2:
            score += 3

        # Similar parameter count (±1)
        if abs(loc.get("param_count", 0) - base_params) <= 1:
            score += 1

        # Same async nature
        if loc.get("is_async") == func_info.get("is_async"):
            score += 1

        # Same method-ness
        if loc.get("is_method") == func_info.get("is_method"):
            score += 1

        # Different file bonus (cross-module invariant propagation is higher value)
        if not same_file:
            score += 1

        if score >= 3:
            results.append({
                "function": qname,
                "file": loc["file"],
                "line": loc["line"],
                "similarity_score": score,
                "similarity_reason": _similarity_reason(prefix, short, base_params, loc, same_file),
            })

    # Sort by score descending, take top N
    results.sort(key=lambda x: x["similarity_score"], reverse=True)
    return results[:max_similar]


def _similarity_reason(
    prefix: str, other_name: str, base_params: int, loc: dict, same_file: bool
) -> str:
    """Build a human-readable similarity explanation."""
    reasons: list[str] = []
    other_parts = other_name.split("_")
    if other_parts and other_parts[0] == prefix and len(prefix) > 2:
        reasons.append(f"same '{prefix}_' prefix")
    if abs(loc.get("param_count", 0) - base_params) <= 1:
        reasons.append("similar parameter count")
    if not same_file:
        reasons.append("different module")
    return ", ".join(reasons) if reasons else "structural similarity"


# ---------------------------------------------------------------------------
# Test selection (three-tier)
# ---------------------------------------------------------------------------

def _is_high_signal_test(name: str) -> bool:
    """Check if test name matches high-signal patterns."""
    return bool(_HIGH_SIGNAL_PATTERNS.search(name))


def _get_bug_fix_tests(
    test_files: list[Path], project_root: Path
) -> tuple[list[dict], int]:
    """Identify tests added/modified in bug-fix commits using git.

    Returns ``(capped_list, true_total)`` -- the caller reports both, so a
    reader cannot mistake the cap for a measurement.
    """
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "--diff-filter=AM",
             "--format=%H %s", "-100", "--", "*.py"],
            capture_output=True, text=True, timeout=30,
            cwd=str(project_root),
        )
        if result.returncode != 0:
            return [], 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return [], 0

    fix_commits: list[tuple[str, str]] = []
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split(" ", 1)
        if len(parts) < 2:
            continue
        sha, msg = parts
        msg_lower = msg.lower()
        if any(kw in msg_lower for kw in ("fix", "bug", "crash", "leak", "error", "patch")):
            fix_commits.append((sha, msg))

    if not fix_commits:
        return [], 0

    test_rel_paths = set()
    for tf in test_files:
        try:
            test_rel_paths.add(str(tf.relative_to(project_root)))
        except ValueError:
            pass

    bug_fix_tests: list[dict] = []
    for sha, msg in fix_commits[:30]:
        try:
            diff_result = subprocess.run(
                ["git", "diff-tree", "--no-commit-id", "-r", "--name-only", sha],
                capture_output=True, text=True, timeout=10,
                cwd=str(project_root),
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            continue

        for changed_file in diff_result.stdout.strip().split("\n"):
            if changed_file in test_rel_paths:
                bug_fix_tests.append({
                    "test_file": changed_file,
                    "fix_commit": sha[:8],
                    "fix_message": msg[:120],
                })

    # Caller records the total; see `bug_fix_tests_total` in the envelope.
    return bug_fix_tests[:15], len(bug_fix_tests)


def select_tests(
    test_info: list[dict],
    bug_fix_tests: list[dict],
    max_tests: int = 30,
) -> list[dict]:
    """Select high-value tests using three-tier prioritization.

    Each item in test_info is {file, function, line, assertions, ...}.
    Returns selected subset with selection_tier added.
    """
    tier1: list[dict] = []  # bug-fix tests
    tier2: list[dict] = []  # error/boundary tests
    tier3: list[dict] = []  # remaining tests

    fix_test_files = {bt["test_file"] for bt in bug_fix_tests}

    for t in test_info:
        # Skip tests with only implementation-detail assertions
        behavioral = [a for a in t.get("assertions", []) if not a.get("is_implementation_detail")]
        if not behavioral:
            continue

        rel_file = t.get("file", "")

        if rel_file in fix_test_files:
            tier1.append({**t, "selection_tier": "bug_fix"})
        elif _is_high_signal_test(t.get("function", "")) or any(
            a["invariant_type"] == "error_condition" for a in behavioral
        ):
            tier2.append({**t, "selection_tier": "error_boundary"})
        else:
            tier3.append({**t, "selection_tier": "general"})

    # Distribute quota: 10/10/10 by default, redistribute if any tier is short
    quota1 = min(len(tier1), max_tests // 3)
    quota2 = min(len(tier2), max_tests // 3)
    quota3 = max_tests - quota1 - quota2

    # If tier3 also short, redistribute back
    quota3 = min(len(tier3), quota3)
    remaining = max_tests - quota1 - quota2 - quota3
    if remaining > 0:
        # Give remaining to tier2, then tier1
        extra2 = min(len(tier2) - quota2, remaining)
        quota2 += extra2
        remaining -= extra2
        extra1 = min(len(tier1) - quota1, remaining)
        quota1 += extra1

    selected = tier1[:quota1] + tier2[:quota2] + tier3[:quota3]
    return selected


# ---------------------------------------------------------------------------
# Main analysis
# ---------------------------------------------------------------------------

def _extract_all_test_info(
    test_files: list[Path], project_root: Path,
    source_functions: dict[str, list[dict]],
) -> list[dict]:
    """Extract test function info from all test files."""
    all_tests: list[dict] = []

    for filepath in test_files:
        try:
            source = filepath.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=str(filepath))
        except SyntaxError:
            continue

        rel = str(filepath.relative_to(project_root))

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("test"):
                continue

            assertions = extract_assertions(node)
            if not assertions:
                continue

            tested_func = resolve_tested_function(
                filepath, node, tree, source_functions,
            )

            info: dict = {
                "file": rel,
                "function": node.name,
                "line": node.lineno,
                "assertions": assertions,
                "assertion_count": len(assertions),
                "behavioral_assertion_count": sum(
                    1 for a in assertions if not a.get("is_implementation_detail")
                ),
                # Sorted: set iteration order varies with PYTHONHASHSEED, which
                # would make output non-reproducible across runs and defeat
                # cross-run deduplication (see explore's --runs option).
                "invariant_types": sorted({a["invariant_type"] for a in assertions}),
                "tested_function": tested_func,
            }
            all_tests.append(info)

    return all_tests


def analyze(target: str, *, max_files: int = 0, with_git: bool = False) -> dict:
    """Run test invariant extraction and return structured results."""
    target_path = Path(target).resolve()
    project_root = find_project_root(target_path)
    scan_root = target_path if target_path.is_dir() else target_path.parent

    all_files = list(discover_python_files(scan_root))
    if max_files > 0:
        all_files = all_files[:max_files]

    source_files, test_files = classify_files(all_files, project_root)

    # Extract source functions for mapping
    source_functions = extract_source_functions(source_files, project_root)

    # Extract all test info
    all_test_info = _extract_all_test_info(test_files, project_root, source_functions)

    # Get bug-fix tests if git is available
    bug_fix_tests: list[dict] = []
    bug_fix_tests_total = 0
    if with_git:
        bug_fix_tests, bug_fix_tests_total = _get_bug_fix_tests(
            test_files, project_root
        )

    # Select high-value tests
    selected = select_tests(all_test_info, bug_fix_tests, max_tests=30)

    # For each selected test with a resolved tested function, find similar functions
    invariants: list[dict] = []
    for test in selected:
        tested = test.get("tested_function")
        if tested is None:
            invariants.append({
                **test,
                "similar_functions": [],
            })
            continue

        func_name = tested["function"]
        # Find the function info for similarity matching
        func_locations = source_functions.get(func_name, [])
        func_info = func_locations[0] if func_locations else {"param_count": 0}

        similar = find_similar_functions(
            func_name, func_info, source_functions, max_similar=5,
        )

        invariants.append({
            **test,
            "similar_functions": similar,
        })

    # Find untested similar functions (functions similar to tested ones but with no tests)
    tested_func_names = {
        t["tested_function"]["function"]
        for t in all_test_info
        if t.get("tested_function")
    }
    untested_similar: list[dict] = []
    for test in selected:
        tested = test.get("tested_function")
        if tested is None:
            continue
        func_name = tested["function"]
        func_locations = source_functions.get(func_name, [])
        func_info = func_locations[0] if func_locations else {"param_count": 0}

        for sim in find_similar_functions(func_name, func_info, source_functions):
            if sim["function"] not in tested_func_names:
                untested_similar.append({
                    "function": sim["function"],
                    "file": sim["file"],
                    "line": sim["line"],
                    "similar_to": func_name,
                    "tested_in": test["file"],
                    "invariant_types": test["invariant_types"],
                    "similarity_reason": sim["similarity_reason"],
                })

    # Deduplicate untested_similar by function name
    seen: set[str] = set()
    deduped: list[dict] = []
    for item in untested_similar:
        if item["function"] not in seen:
            seen.add(item["function"])
            deduped.append(item)
    untested_similar_total = len(deduped)
    untested_similar = deduped[:20]

    return {
        "project_root": str(project_root),
        "scan_root": str(scan_root),
        "summary": {
            "source_files": len(source_files),
            "test_files": len(test_files),
            "source_functions": sum(len(v) for v in source_functions.values()),
            "total_test_functions": len(all_test_info),
            "selected_test_functions": len(selected),
            "invariants_extracted": sum(
                t["behavioral_assertion_count"] for t in selected
            ),
            "similar_functions_found": sum(
                len(t.get("similar_functions", [])) for t in invariants
            ),
            # Both lists below are CAPPED. Report the true totals beside them:
            # a reviewer who reads `untested_similar_functions: 20` as a count
            # is reading the cap, not the measurement.
            "untested_similar_functions": len(untested_similar),
            "untested_similar_functions_total": untested_similar_total,
            "untested_similar_functions_capped": untested_similar_total > 20,
            "bug_fix_tests_found": len(bug_fix_tests),
            "bug_fix_tests_total": bug_fix_tests_total,
            "bug_fix_tests_capped": bug_fix_tests_total > 15,
        },
        "invariants": invariants,
        "bug_fix_tests": bug_fix_tests,
        "untested_similar_functions": untested_similar,
    }


def main() -> None:
    """CLI entry point."""
    from scan_common import exit_on_help

    exit_on_help(sys.argv[1:], __doc__)
    try:
        max_files = 0
        with_git = False
        positional: list[str] = []
        argv = sys.argv[1:]
        i = 0
        while i < len(argv):
            if argv[i] == "--max-files" and i + 1 < len(argv):
                max_files = int(argv[i + 1])
                i += 2
            elif argv[i] == "--with-git":
                with_git = True
                i += 1
            elif argv[i].startswith("--"):
                i += 1
            else:
                positional.append(argv[i])
                i += 1
        target = positional[0] if positional else "."
        result = analyze(target, max_files=max_files, with_git=with_git)
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
    except Exception as e:
        json.dump({"error": str(e), "type": type(e).__name__},
                  sys.stdout, indent=2)
        sys.stdout.write("\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
