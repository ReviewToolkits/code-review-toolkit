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


class TestHttpErrorStatusAssertions(unittest.TestCase):
    """Asserting a 4xx/5xx response status is an error-condition invariant."""

    def _types(self, body: str) -> list[str]:
        import ast
        tree = ast.parse(body)
        func = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
        return [a["invariant_type"] for a in mod.extract_assertions(func)]

    def test_status_code_comparisons(self):
        for check in (
            "assert r.status_code == 400",
            "assert 404 == r.status_code",
            "assert r.status_code in (400, 422)",
            "assert r.status_code >= 400",
            "assert r.status_code == HTTPStatus.CONFLICT",
            "assert r.status_code == status.HTTP_413_REQUEST_ENTITY_TOO_LARGE",
            "assert r.status == 503",
            "self.assertEqual(r.status_code, 403)",
            "self.assertIn(r.status_code, {401, 403})",
        ):
            with self.subTest(check=check):
                body = f"def test_x(self, r):\n    {check}\n"
                self.assertIn("error_condition", self._types(body))

    def test_success_statuses_are_not_errors(self):
        for check in (
            "assert r.status_code == 200",
            "assert r.status_code in (200, 201)",
            "assert r.status_code < 400",
            "assert r.status_code != 500",
            "self.assertEqual(r.status_code, 204)",
            "assert r.json()['count'] == 404",
        ):
            with self.subTest(check=check):
                body = f"def test_x(self, r):\n    {check}\n"
                self.assertNotIn("error_condition", self._types(body))

    def test_detail_names_the_status(self):
        import ast
        tree = ast.parse("def test_x(r):\n    assert r.status_code == 409\n")
        func = tree.body[0]
        (assertion,) = mod.extract_assertions(func)
        self.assertEqual(assertion["detail"], "HTTP 409")


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


def _fn(name: str, file: str, params: int = 1, method: bool = False) -> dict:
    return {"file": file, "line": 1, "param_count": params, "is_method": method,
            "is_async": False, "name": name.rsplit(".", 1)[-1]}


class TestSimilarFunctionPrecision(unittest.TestCase):
    """Similarity needs a name relation; structure alone is noise."""

    def _similar(self, tested: str, funcs: dict[str, dict]) -> list[str]:
        table = {name: [info] for name, info in funcs.items()}
        return [
            s["function"]
            for s in mod.find_similar_functions(tested, funcs[tested], table)
        ]

    def test_same_method_on_sibling_class(self):
        funcs = {
            "MemoryStore.close": _fn("MemoryStore.close", "pkg/memory.py", 0, True),
            "FileStore.close": _fn("FileStore.close", "pkg/files.py", 0, True),
        }
        self.assertEqual(
            self._similar("MemoryStore.close", funcs), ["FileStore.close"]
        )

    def test_parameter_count_alone_is_not_similarity(self):
        funcs = {
            "validate_input": _fn("validate_input", "pkg/a.py"),
            "record": _fn("record", "pkg/b.py"),
            "summary": _fn("summary", "pkg/c.py"),
        }
        self.assertEqual(self._similar("validate_input", funcs), [])

    def test_generic_first_word_needs_a_second(self):
        funcs = {
            "get_config": _fn("get_config", "pkg/a.py"),
            "get_logger": _fn("get_logger", "pkg/b.py"),
            "get_config_path": _fn("get_config_path", "pkg/c.py"),
        }
        self.assertEqual(self._similar("get_config", funcs), ["get_config_path"])

    def test_other_top_level_dirs_and_private_helpers_skipped(self):
        funcs = {
            "ensure_key": _fn("ensure_key", "pkg/auth.py"),
            "ensure_label": _fn("ensure_label", "scripts/sync.py"),
            "_ensure_dir": _fn("_ensure_dir", "pkg/util.py"),
            "ensure_token": _fn("ensure_token", "pkg/tokens.py"),
        }
        self.assertEqual(self._similar("ensure_key", funcs), ["ensure_token"])


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


class TestSourceModuleNamedLikeATest(unittest.TestCase):
    """A package's own `test_*.py` module with no tests does not stop discovery."""

    _FILES = {
        "app/__init__.py": "",
        "app/test_outcome.py": "class Outcome:\n    __test__ = False\n    OK = 1\n",
        "app/core.py": "def parse(text):\n    return text\n",
        "tests/test_core.py": (
            "from app.core import parse\n"
            "\n"
            "def test_parse_empty():\n"
            "    assert parse('') == ''\n"
        ),
    }

    def test_falls_back_to_project_tests(self):
        with TempProject(self._FILES) as root:
            result = mod.analyze(str(root / "app"))
        self.assertEqual(result["test_discovery"]["method"], "conventional_dir")
        self.assertEqual(result["summary"]["total_test_functions"], 1)
        self.assertEqual(result["summary"]["test_files"], 1)
        self.assertTrue(any("test_outcome.py" in w for w in result["warnings"]))

    def test_envelope_fields_present(self):
        with TempProject(self._FILES) as root:
            result = mod.analyze(str(root / "app"), max_files=2)
        self.assertTrue(result["files_capped"])
        self.assertEqual(result["files_analyzed"], 2)
        self.assertEqual(result["files_total"], 3)


def _tested(result: dict, test_name: str) -> dict | None:
    for inv in result["invariants"]:
        if inv["function"] == test_name:
            return inv["tested_function"]
    raise AssertionError(f"{test_name} not selected")


_MAPPING_SRC = {
    "pkg/__init__.py": "from pkg.store import Store\n",
    "pkg/store.py": (
        "class Store:\n"
        "    def __init__(self, path=None):\n"
        "        self.items = []\n"
        "\n"
        "    def add(self, item):\n"
        "        self.items.append(item)\n"
        "\n"
        "    def count(self):\n"
        "        return len(self.items)\n"
        "\n"
        "    def lookup(self, key):\n"
        "        return None\n"
    ),
    "pkg/core.py": "def parse(text):\n    return text\n",
    "pkg/shapes.py": "def dim(v):\n    return len(v)\n",
    "pkg/api.py": (
        "from fastapi import APIRouter\n"
        "\n"
        "router = APIRouter()\n"
        "\n"
        "@router.get('/items/{item_id}')\n"
        "def read_item(item_id):\n"
        "    return {}\n"
        "\n"
        "@router.post('/items')\n"
        "def create_item(body):\n"
        "    return {}\n"
    ),
}


class TestResolveTestedFunction(unittest.TestCase):
    """pytest-style tests map to the code they exercise."""

    def _analyze(self, tests: dict) -> dict:
        with TempProject({**_MAPPING_SRC, **tests}) as root:
            return mod.analyze(str(root))

    def test_method_on_fixture_object(self):
        result = self._analyze({
            "tests/conftest.py": (
                "import pytest\n"
                "from pkg.store import Store\n"
                "\n"
                "@pytest.fixture\n"
                "def store(tmp_path):\n"
                "    s = Store(tmp_path)\n"
                "    yield s\n"
            ),
            "tests/test_things.py": (
                "def test_unknown_key_is_none(store):\n"
                "    assert store.lookup('x') is None\n"
            ),
        })
        tested = _tested(result, "test_unknown_key_is_none")
        self.assertEqual(
            (tested["function"], tested["file"], tested["match_method"]),
            ("Store.lookup", "pkg/store.py", "method_call"),
        )

    def test_asserted_call_beats_setup_call(self):
        result = self._analyze({
            "tests/test_things.py": (
                "from pkg import Store\n"
                "\n"
                "def test_behaviour():\n"
                "    s = Store()\n"
                "    s.add(1)\n"
                "    assert s.count() == 1\n"
            ),
        })
        tested = _tested(result, "test_behaviour")
        self.assertEqual(tested["function"], "Store.count")
        self.assertTrue(tested["result_asserted"])

    def test_http_route_through_test_client(self):
        result = self._analyze({
            "tests/test_api.py": (
                "def test_missing_returns_404(client):\n"
                "    client.post('/api/items', json={})\n"
                "    r = client.get(f'/api/items/{42}')\n"
                "    assert r.status_code == 404\n"
            ),
        })
        tested = _tested(result, "test_missing_returns_404")
        self.assertEqual(
            (tested["function"], tested["file"], tested["match_method"]),
            ("read_item", "pkg/api.py", "http_route"),
        )

    def test_module_alias_call(self):
        result = self._analyze({
            "tests/test_core.py": (
                "import pkg.core as c\n"
                "\n"
                "def test_empty_text():\n"
                "    assert c.parse('') == ''\n"
            ),
        })
        tested = _tested(result, "test_empty_text")
        self.assertEqual(
            (tested["function"], tested["match_method"]),
            ("parse", "imported_call"),
        )

    def test_first_word_of_test_name_is_not_enough(self):
        # `test_dim_*` names no function the test calls or imports: matching
        # on the first word alone mapped it to an unrelated `dim`.
        result = self._analyze({
            "tests/test_api.py": (
                "def test_dim_mismatch_is_rejected():\n"
                "    assert 1 + 1 == 2\n"
            ),
        })
        self.assertIsNone(_tested(result, "test_dim_mismatch_is_rejected"))


def _t(name: str, file: str, inv_type: str = "equality") -> dict:
    return {
        "file": file,
        "function": name,
        "line": 1,
        "assertions": [{"invariant_type": inv_type, "is_implementation_detail": False}],
    }


class TestSelectionSpread(unittest.TestCase):
    """Selection is not alphabetical and knows concurrency vocabulary."""

    def test_round_robin_across_files(self):
        tests = [_t(f"test_invalid_{i}", "tests/test_a.py") for i in range(5)]
        tests += [_t(f"test_invalid_{i}", "tests/test_b.py") for i in range(5)]
        selected = mod.select_tests(tests, [], max_tests=2)
        self.assertEqual(
            sorted(s["file"] for s in selected),
            ["tests/test_a.py", "tests/test_b.py"],
        )

    def test_file_name_carries_signal(self):
        tests = [
            _t("test_get_returns_value", "tests/test_cache_race.py"),
            # True negative: `race` inside `trace` is not the word race.
            _t("test_get_returns_value", "tests/test_trace.py"),
        ]
        tiers = {
            s["file"]: s["selection_tier"]
            for s in mod.select_tests(tests, [], max_tests=30)
        }
        self.assertEqual(tiers["tests/test_cache_race.py"], "error_boundary")
        self.assertEqual(tiers["tests/test_trace.py"], "general")

    def test_concurrency_words_selected_before_generic_error_words(self):
        tests = [_t("test_invalid_name", "tests/test_a.py", "error_condition")]
        tests += [
            _t(name, "tests/test_z.py", "error_condition")
            for name in (
                "test_cancelled_request_keeps_slot",
                "test_rollback_on_failure",
                "test_does_not_deadlock",
                "test_lock_released_after_timeout",
            )
        ]
        selected = mod.select_tests(tests, [], max_tests=1)
        self.assertEqual(selected[0]["file"], "tests/test_z.py")

    def test_bug_fix_tier_is_per_function(self):
        tests = [
            _t("test_old_behaviour", "tests/test_a.py"),
            _t("test_regression_for_fix", "tests/test_a.py"),
        ]
        fixes = [{
            "test_file": "tests/test_a.py",
            "fix_commit": "abc",
            "fix_message": "fix: x",
            "test_functions": ["test_regression_for_fix"],
            "attribution": "function",
        }]
        tiers = {
            s["function"]: s["selection_tier"]
            for s in mod.select_tests(tests, fixes, max_tests=30)
        }
        self.assertEqual(tiers["test_regression_for_fix"], "bug_fix")
        self.assertEqual(tiers["test_old_behaviour"], "general")

    def test_fix_commit_vocabulary(self):
        for subject in (
            "[fix] handle empty input",
            "Avoid a deadlock when closing the pool",
            "Close the race between flush and read",
            "Roll back on cancel: rollback partial writes",
            "Bound the request timeout",
        ):
            self.assertTrue(mod._FIX_COMMIT_RE.search(subject), subject)
        for subject in ("Add a prefix option", "Refactor the dispatch table"):
            self.assertIsNone(mod._FIX_COMMIT_RE.search(subject), subject)


def _git(root, *args, env=None):
    import os
    import subprocess

    full_env = dict(os.environ)
    full_env.update({
        "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.com",
    })
    subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, check=True, env=full_env,
    )


class TestBugFixAttribution(unittest.TestCase):
    """A fix commit marks the test functions it added, not the whole file."""

    def test_added_test_function_is_named(self):
        old = (
            "def test_existing():\n"
            "    assert True\n"
        )
        new = old + (
            "\n"
            "\n"
            "def test_flush_race_regression():\n"
            "    assert True\n"
        )
        with TempProject({"pkg/core.py": "X = 1\n", "tests/test_core.py": old}) as root:
            _git(root, "init", "-q", "-b", "main")
            _git(root, "add", "-A")
            _git(root, "commit", "-q", "-m", "Add core")
            (root / "tests/test_core.py").write_text(new, encoding="utf-8")
            _git(root, "commit", "-q", "-am", "Fix the flush race")
            entries, total = mod._get_bug_fix_tests(
                [root / "tests/test_core.py"], root
            )
        self.assertEqual(total, 1)
        self.assertEqual(entries[0]["test_functions"], ["test_flush_race_regression"])
        self.assertEqual(entries[0]["attribution"], "function")


if __name__ == "__main__":
    unittest.main()
