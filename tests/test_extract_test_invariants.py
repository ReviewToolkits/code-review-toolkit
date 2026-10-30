"""Tests for extract_test_invariants.py — test invariant extraction and propagation."""

import unittest
from helpers import import_script, TempProject

mod = import_script("extract_test_invariants")


SIMPLE_SOURCE = """\
def validate_input(data):
    if not data:
        raise ValueError("empty input")
    return data.strip()

def validate_config(config):
    if not config:
        return None
    return config.strip()

def process_data(items):
    return [x * 2 for x in items]
"""

SIMPLE_TESTS = """\
import unittest

class TestValidateInput(unittest.TestCase):
    def test_validate_input_empty(self):
        with self.assertRaises(ValueError):
            validate_input("")

    def test_validate_input_normal(self):
        result = validate_input("  hello  ")
        self.assertEqual(result, "hello")

    def test_validate_input_not_none(self):
        result = validate_input("x")
        self.assertIsNotNone(result)
"""

MOCK_HEAVY_TESTS = """\
import unittest
from unittest.mock import MagicMock, patch

class TestWithMocks(unittest.TestCase):
    def test_calls_backend(self):
        mock_backend = MagicMock()
        process(mock_backend)
        mock_backend.send.assert_called_once_with("data")

    def test_no_assertions(self):
        # Just exercises code, no assertions
        process_data([1, 2, 3])
"""

PYTEST_STYLE_TESTS = """\
import pytest

def test_parse_raises_on_invalid():
    with pytest.raises(ValueError):
        parse_input("")

def test_parse_returns_dict():
    result = parse_input("key=value")
    assert isinstance(result, dict)
    assert "key" in result
"""


class TestExtractAssertions(unittest.TestCase):
    """Test assertion extraction from test functions."""

    def _parse_test_func(self, source: str) -> list:
        import ast
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test"):
                return mod.extract_assertions(node)
        return []

    def test_assert_raises_extracts_error_condition(self):
        """assertRaises correctly identified as error_condition invariant."""
        source = """\
def test_validate_empty(self):
    with self.assertRaises(ValueError):
        validate_input("")
"""
        assertions = self._parse_test_func(source)
        error_assertions = [a for a in assertions if a["invariant_type"] == "error_condition"]
        self.assertTrue(len(error_assertions) > 0)
        self.assertEqual(error_assertions[0]["detail"], "ValueError")

    def test_assert_equal_extracts_equality(self):
        """assertEqual correctly identified as equality invariant."""
        source = """\
def test_result(self):
    self.assertEqual(func(), 42)
"""
        assertions = self._parse_test_func(source)
        eq_assertions = [a for a in assertions if a["invariant_type"] == "equality"]
        self.assertEqual(len(eq_assertions), 1)

    def test_assert_is_not_none(self):
        """assertIsNotNone correctly identified as non_nullability invariant."""
        source = """\
def test_not_none(self):
    self.assertIsNotNone(func())
"""
        assertions = self._parse_test_func(source)
        nn_assertions = [a for a in assertions if a["invariant_type"] == "non_nullability"]
        self.assertEqual(len(nn_assertions), 1)

    def test_mock_assertions_are_implementation_details(self):
        """Mock assertions flagged as implementation details."""
        source = """\
def test_calls(self):
    mock.assert_called_once_with("data")
"""
        assertions = self._parse_test_func(source)
        self.assertTrue(len(assertions) > 0)
        self.assertTrue(assertions[0]["is_implementation_detail"])

    def test_pytest_raises(self):
        """pytest.raises correctly identified as error_condition."""
        source = """\
def test_raises():
    with pytest.raises(TypeError):
        func(None)
"""
        assertions = self._parse_test_func(source)
        error_assertions = [a for a in assertions if a["invariant_type"] == "error_condition"]
        self.assertTrue(len(error_assertions) > 0)
        self.assertEqual(error_assertions[0]["detail"], "TypeError")

    def test_no_assertions_yields_empty(self):
        """Test with no assertions produces no invariants."""
        source = """\
def test_just_exercise():
    func()
    other_func()
"""
        assertions = self._parse_test_func(source)
        self.assertEqual(len(assertions), 0)


class TestSimilarFunctions(unittest.TestCase):
    """Test similar function discovery."""

    def test_finds_same_prefix(self):
        """Functions with same verb prefix are ranked higher."""
        all_funcs = {
            "validate_input": [{"file": "a.py", "line": 1, "param_count": 1,
                                "is_method": False, "is_async": False, "name": "validate_input"}],
            "validate_config": [{"file": "b.py", "line": 1, "param_count": 1,
                                 "is_method": False, "is_async": False, "name": "validate_config"}],
            "process_data": [{"file": "c.py", "line": 10, "param_count": 1,
                              "is_method": False, "is_async": False, "name": "process_data"}],
        }
        similar = mod.find_similar_functions(
            "validate_input",
            all_funcs["validate_input"][0],
            all_funcs,
        )
        names = [s["function"] for s in similar]
        self.assertIn("validate_config", names)
        # validate_config should rank higher than process_data (prefix match)
        if "process_data" in names:
            vc_idx = names.index("validate_config")
            pd_idx = names.index("process_data")
            self.assertLess(vc_idx, pd_idx, "validate_config should rank above process_data")

    def test_respects_max_similar(self):
        """At most max_similar results returned."""
        all_funcs = {
            f"validate_{i}": [{"file": f"{i}.py", "line": 1, "param_count": 1,
                                "is_method": False, "is_async": False, "name": f"validate_{i}"}]
            for i in range(20)
        }
        similar = mod.find_similar_functions(
            "validate_0",
            all_funcs["validate_0"][0],
            all_funcs,
            max_similar=3,
        )
        self.assertLessEqual(len(similar), 3)


class TestSelectTests(unittest.TestCase):
    """Test the three-tier test selection algorithm."""

    def _make_test(self, name: str, file: str = "tests/test_a.py",
                   inv_type: str = "equality") -> dict:
        return {
            "file": file,
            "function": name,
            "line": 1,
            "assertions": [{"invariant_type": inv_type, "is_implementation_detail": False}],
        }

    def test_selects_within_budget(self):
        """Selection respects max_tests budget."""
        tests = [self._make_test(f"test_{i}") for i in range(100)]
        selected = mod.select_tests(tests, [], max_tests=30)
        self.assertLessEqual(len(selected), 30)

    def test_prioritizes_error_tests(self):
        """Error-condition tests are selected in tier 2."""
        tests = [
            self._make_test("test_normal_case", inv_type="equality"),
            self._make_test("test_invalid_input", inv_type="error_condition"),
        ]
        selected = mod.select_tests(tests, [], max_tests=30)
        tiers = {s["function"]: s["selection_tier"] for s in selected}
        self.assertEqual(tiers["test_invalid_input"], "error_boundary")

    def test_prioritizes_bug_fix_tests(self):
        """Tests from bug-fix commits are selected in tier 1."""
        tests = [
            self._make_test("test_a", file="tests/test_a.py"),
            self._make_test("test_b", file="tests/test_b.py"),
        ]
        bug_fixes = [{"test_file": "tests/test_a.py", "fix_commit": "abc", "fix_message": "fix"}]
        selected = mod.select_tests(tests, bug_fixes, max_tests=30)
        tiers = {s["function"]: s["selection_tier"] for s in selected}
        self.assertEqual(tiers["test_a"], "bug_fix")

    def test_skips_mock_only_tests(self):
        """Tests with only mock assertions are not selected."""
        tests = [{
            "file": "tests/test_a.py",
            "function": "test_mock_only",
            "line": 1,
            "assertions": [{"invariant_type": "mock_interaction", "is_implementation_detail": True}],
        }]
        selected = mod.select_tests(tests, [], max_tests=30)
        self.assertEqual(len(selected), 0)

    def test_handles_fewer_than_budget(self):
        """Works correctly when there are fewer tests than the budget."""
        tests = [self._make_test(f"test_{i}") for i in range(5)]
        selected = mod.select_tests(tests, [], max_tests=30)
        self.assertEqual(len(selected), 5)


class TestAnalyze(unittest.TestCase):
    """Integration tests for the full analyze pipeline."""

    def test_basic_analysis(self):
        """Analyze returns expected envelope structure."""
        with TempProject({
            "pkg/__init__.py": "",
            "pkg/validator.py": SIMPLE_SOURCE,
            "tests/__init__.py": "",
            "tests/test_validator.py": SIMPLE_TESTS,
        }) as root:
            result = mod.analyze(str(root))
            self.assertIn("summary", result)
            self.assertIn("invariants", result)
            self.assertIn("untested_similar_functions", result)
            self.assertGreater(result["summary"]["test_files"], 0)
            self.assertGreater(result["summary"]["total_test_functions"], 0)

    def test_finds_similar_validate_functions(self):
        """validate_input test invariants are propagated to validate_config."""
        with TempProject({
            "pkg/__init__.py": "",
            "pkg/validator.py": SIMPLE_SOURCE,
            "tests/__init__.py": "",
            "tests/test_validator.py": SIMPLE_TESTS,
        }) as root:
            result = mod.analyze(str(root))
            # Check that similar functions were found
            all_similar = []
            for inv in result["invariants"]:
                all_similar.extend(inv.get("similar_functions", []))
            similar_names = [s["function"] for s in all_similar]
            # validate_config should be found as similar to validate_input
            self.assertTrue(
                any("validate_config" in n for n in similar_names),
                f"Expected validate_config in similar functions, got: {similar_names}"
            )

    def test_handles_syntax_error(self):
        """Syntax errors in test files are handled gracefully."""
        with TempProject({
            "pkg/__init__.py": "",
            "pkg/core.py": "def main(): pass",
            "tests/test_broken.py": "def test_broken(\n    syntax error here",
        }) as root:
            result = mod.analyze(str(root))
            self.assertIn("summary", result)

    def test_mock_heavy_tests_filtered(self):
        """Tests with only mock assertions produce fewer invariants."""
        with TempProject({
            "pkg/__init__.py": "",
            "pkg/core.py": SIMPLE_SOURCE,
            "tests/__init__.py": "",
            "tests/test_mocks.py": MOCK_HEAVY_TESTS,
        }) as root:
            result = mod.analyze(str(root))
            # The mock-only test and no-assertion test should be filtered out
            selected = result["invariants"]
            functions = [s["function"] for s in selected]
            self.assertNotIn("test_no_assertions", functions)


class TestDiscoverTestsOutsideScope(unittest.TestCase):
    """Tests beside the scanned package are found, and the output says so."""

    _SRC = "def parse(text):\n    return text\n"
    _TEST = (
        "from pkg.core import parse\n"
        "\n"
        "def test_parse_empty():\n"
        "    assert parse('') == ''\n"
    )

    def test_sibling_tests_dir_used_for_package_scope(self):
        with TempProject({
            "pkg/__init__.py": "",
            "pkg/core.py": self._SRC,
            "tests/test_core.py": self._TEST,
        }) as root:
            result = mod.analyze(str(root / "pkg"))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["summary"]["test_files"], 1)
        self.assertEqual(result["summary"]["total_test_functions"], 1)
        self.assertEqual(
            result["test_discovery"],
            {"method": "conventional_dir", "roots": ["tests"]},
        )
        self.assertEqual(len(result["warnings"]), 1)
        self.assertIn("tests", result["warnings"][0])
        # Source functions stay limited to the scan root.
        self.assertEqual(result["summary"]["source_files"], 2)

    def test_pytest_testpaths_config_is_honoured(self):
        with TempProject({
            "pkg/__init__.py": "",
            "pkg/core.py": self._SRC,
            "pytest.ini": "[pytest]\ntestpaths = spec\n",
            "spec/check_core.py": self._TEST,
        }) as root:
            result = mod.analyze(str(root / "pkg"))
        self.assertEqual(result["test_discovery"]["method"], "pytest_testpaths")
        self.assertEqual(result["test_discovery"]["roots"], ["spec"])
        self.assertEqual(result["summary"]["total_test_functions"], 1)

    def test_no_tests_anywhere_is_flagged(self):
        with TempProject({
            "pkg/__init__.py": "",
            "pkg/core.py": self._SRC,
        }) as root:
            result = mod.analyze(str(root / "pkg"))
        self.assertEqual(result["status"], "no_tests_found")
        self.assertEqual(result["summary"]["test_files"], 0)
        self.assertEqual(len(result["warnings"]), 1)

    def test_tests_under_scan_root_need_no_warning(self):
        with TempProject({
            "pkg/__init__.py": "",
            "pkg/core.py": self._SRC,
            "tests/test_core.py": self._TEST,
        }) as root:
            result = mod.analyze(str(root))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["test_discovery"]["method"], "scan_root")
        self.assertEqual(result["warnings"], [])


if __name__ == "__main__":
    unittest.main()
