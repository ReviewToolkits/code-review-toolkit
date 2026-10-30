"""Tests for survey_docstrings.py."""

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest import mock

from helpers import TempProject, import_script

mod = import_script("survey_docstrings")


def _survey(files: dict[str, str], **kwargs) -> dict:
    with TempProject(files) as root:
        return mod.analyze(str(root), **kwargs)


def _issues(result: dict, kind: str | None = None) -> list[dict]:
    return [i for i in result["contract_issues"] if kind is None or i["kind"] == kind]


def _one(source: str) -> dict:
    return _survey({"pkg/mod.py": source})


class TestCoverage(unittest.TestCase):
    def test_counts_public_and_skips_private(self):
        result = _one(
            '"""Module doc."""\n'
            "def documented():\n"
            '    """Compute the weighted total of the basket."""\n'
            "def undocumented():\n"
            "    pass\n"
            "def _private():\n"
            "    pass\n"
            "class Widget:\n"
            "    def method(self):\n"
            "        pass\n"
            "    def __repr__(self):\n"
            "        return ''\n"
        )
        totals = result["coverage"]["totals"]
        self.assertEqual(
            totals["function"], {"documented": 1, "total": 2, "percent": 50.0}
        )
        self.assertEqual(totals["method"]["total"], 1)  # dunder excluded
        self.assertEqual(totals["class"]["documented"], 0)
        names = {u["name"] for u in result["coverage"]["undocumented"]}
        self.assertEqual(names, {"undocumented", "Widget", "Widget.method"})

    def test_dunder_all_restricts_the_public_set(self):
        result = _one(
            "__all__ = ['exported']\n"
            "def exported():\n    pass\n"
            "def helper():\n    pass\n"
        )
        self.assertEqual(result["coverage"]["totals"]["function"]["total"], 1)

    def test_route_documented_by_decorator_counts_even_when_nested(self):
        result = _one(
            "def build(router):\n"
            "    @router.get('/items', summary='List the items')\n"
            "    def list_items():\n"
            "        return []\n"
            "    @router.post('/items')\n"
            "    def create_item():\n"
            "        return {}\n"
        )
        totals = result["coverage"]["totals"]
        self.assertEqual(
            totals["route"], {"documented": 1, "total": 2, "percent": 50.0}
        )
        undocumented = [u["name"] for u in result["coverage"]["undocumented"]]
        self.assertIn("build.<locals>.create_item", undocumented)

    def test_trivial_docstring(self):
        result = _one('def get_value():\n    """Get the value."""\n    return 1\n')
        self.assertEqual(
            [t["name"] for t in result["coverage"]["trivial"]], ["get_value"]
        )

    def test_percent_is_null_not_zero_when_nothing_to_count(self):
        result = _one("x = 1\n")
        self.assertNotIn("function", result["coverage"]["totals"])
        result = _survey({"pkg/_private.py": "def f():\n    pass\n"})
        self.assertEqual(result["coverage"]["totals"], {})


class TestReturns(unittest.TestCase):
    def test_parameter_returned_unchanged_under_a_narrower_documented_type(self):
        result = _one(
            "from typing import Any\n"
            "def label_for(table: dict, key: Any) -> Any:\n"
            '    """Look up a label.\n\n'
            "    Returns:\n"
            "        label (string): the display label\n"
            '    """\n'
            "    if key not in table:\n"
            "        return key\n"
            "    return table[key].title()\n"
        )
        [issue] = _issues(result, "returns-parameter-unchanged")
        self.assertEqual(issue["function"], "label_for")
        self.assertIn("return key", issue["evidence"][0])

    def test_parameter_whose_annotation_matches_is_not_flagged(self):
        result = _one(
            "def clean(text: str) -> str:\n"
            '    """Clean.\n\n    Returns:\n        str: the text\n    """\n'
            "    if not text:\n"
            "        return text\n"
            "    return text.strip()\n"
        )
        self.assertEqual(_issues(result), [])

    def test_reassigned_parameter_is_not_a_passthrough(self):
        result = _one(
            "def clean(text):\n"
            '    """Clean.\n\n    Returns:\n        str: the text\n    """\n'
            "    text = str(text).strip()\n"
            "    return text\n"
        )
        self.assertEqual(_issues(result), [])

    def test_annotation_contradicts_documented_type(self):
        result = _one(
            "def results() -> list[dict]:\n"
            '    """Collect.\n\n    Returns:\n        dict: one result\n    """\n'
            "    return [{}]\n"
        )
        [issue] = _issues(result, "returns-type-mismatch")
        self.assertEqual(issue["confidence"], "high")

    def test_optional_annotation_is_compatible(self):
        result = _one(
            "def find(key: str) -> str | None:\n"
            '    """Find.\n\n    Returns:\n        str: the match\n    """\n'
            "    return key or None\n"
        )
        self.assertEqual(_issues(result), [])

    def test_bool_return_satisfies_documented_int(self):
        result = _one(
            "def count():\n"
            '    """Count.\n\n    Returns:\n        int: how many\n    """\n'
            "    return True\n"
        )
        self.assertEqual(_issues(result), [])

    def test_literal_return_contradicts_documented_type(self):
        result = _one(
            "def pair():\n"
            '    """Pair.\n\n    Returns:\n        tuple: a pair\n    """\n'
            "    return [1, 2]\n"
        )
        [issue] = _issues(result, "returns-type-mismatch")
        self.assertEqual(issue["confidence"], "medium")

    def test_documented_return_with_no_valued_return(self):
        result = _one(
            "def write(path):\n"
            '    """Write.\n\n    Returns:\n        bool: True on success\n    """\n'
            "    open(path, 'w').close()\n"
        )
        self.assertEqual(len(_issues(result, "returns-documented-no-value")), 1)

    def test_documented_none_return_is_not_flagged(self):
        result = _one(
            "def write(path):\n"
            '    """Write.\n\n    Returns:\n        None\n    """\n'
            "    open(path, 'w').close()\n"
        )
        self.assertEqual(_issues(result), [])

    def test_generator_and_abstract_stub_are_skipped(self):
        result = _one(
            "import abc\n"
            "def items():\n"
            '    """Items.\n\n    Returns:\n        list: items\n    """\n'
            "    yield 1\n"
            "class Base(abc.ABC):\n"
            "    @abc.abstractmethod\n"
            "    def load(self):\n"
            '        """Load.\n\n        Returns:\n            dict: data\n        """\n'
        )
        self.assertEqual(_issues(result), [])

    def test_numpy_and_sphinx_return_types(self):
        result = _one(
            "def a():\n"
            '    """A.\n\n    Returns\n    -------\n    dict\n        data\n    """\n'
            "    return []\n"
            "def b():\n"
            '    """B.\n\n    :rtype: tuple\n    """\n'
            "    return 'x'\n"
        )
        self.assertEqual(
            sorted(i["function"] for i in _issues(result, "returns-type-mismatch")),
            ["a", "b"],
        )


class TestRaises(unittest.TestCase):
    def test_documented_exception_that_nothing_raises(self):
        result = _one(
            "def verify(rule):\n"
            '    """Verify.\n\n    Raises:\n        AssertionError: if the rule fails\n    """\n'
            "    return apply_rule(rule)\n"
            "def apply_rule(rule):\n"
            "    return bool(rule)\n"
        )
        [issue] = _issues(result, "raises-documented-not-raised")
        self.assertEqual(issue["documented"], "AssertionError")
        self.assertEqual(issue["confidence"], "medium")

    def test_raised_through_a_project_callee_or_a_subclass(self):
        result = _one(
            "class AppError(Exception):\n    pass\n"
            "class ConfigError(AppError):\n    pass\n"
            "def load(path):\n"
            '    """Load.\n\n    Raises:\n        AppError: bad config\n    """\n'
            "    return _parse(path)\n"
            "def _parse(path):\n"
            "    raise ConfigError(path)\n"
        )
        self.assertEqual(_issues(result), [])

    def test_assert_statement_raises_assertion_error(self):
        result = _one(
            "def check(x):\n"
            '    """Check.\n\n    Raises:\n        AssertionError: if false\n    """\n'
            "    assert x\n"
        )
        self.assertEqual(_issues(result), [])

    def test_dynamic_reraise_suppresses_the_check(self):
        result = _one(
            "def run(fn):\n"
            '    """Run.\n\n    Raises:\n        TimeoutError: when slow\n    """\n'
            "    try:\n"
            "        return fn()\n"
            "    except Exception as exc:\n"
            "        raise exc\n"
        )
        self.assertEqual(_issues(result), [])

    def test_library_style_exception_with_external_calls_is_low_confidence(self):
        result = _one(
            "import json\n"
            "def parse(text):\n"
            '    """Parse.\n\n    Raises:\n        ValueError: bad input\n    """\n'
            "    return json.loads(text)\n"
        )
        [issue] = _issues(result, "raises-documented-not-raised")
        self.assertEqual(issue["confidence"], "low")

    def test_raises_undocumented(self):
        result = _one(
            "def parse(text):\n"
            '    """Parse.\n\n    Raises:\n        ValueError: bad input\n    """\n'
            "    if not text:\n"
            "        raise ValueError(text)\n"
            "    raise KeyError(text)\n"
        )
        [issue] = _issues(result, "raises-undocumented")
        self.assertIn("KeyError", issue["detail"])

    def test_raise_caught_inside_try_is_not_undocumented(self):
        result = _one(
            "def parse(text):\n"
            '    """Parse.\n\n    Raises:\n        ValueError: bad input\n    """\n'
            "    try:\n"
            "        raise KeyError(text)\n"
            "    except KeyError:\n"
            "        raise ValueError(text)\n"
        )
        self.assertEqual(_issues(result), [])

    def test_class_docstring_prose_raise(self):
        result = _one(
            "class CacheMissError(Exception):\n    pass\n"
            "class Client:\n"
            '    """Reads the cache. Raises `CacheMissError` when a key has expired."""\n'
            "    def call(self):\n"
            "        return self._send()\n"
            "    def _send(self):\n"
            "        return 1\n"
        )
        [issue] = _issues(result, "raises-documented-not-raised")
        self.assertEqual(issue["function"], "Client")

    def test_class_docstring_prose_raise_kept(self):
        result = _one(
            "class CacheMissError(Exception):\n    pass\n"
            "class Client:\n"
            '    """Reads the cache. Raises `CacheMissError` when a key has expired."""\n'
            "    def call(self):\n"
            "        raise CacheMissError()\n"
        )
        self.assertEqual(_issues(result), [])


class TestArgs(unittest.TestCase):
    def test_stale_parameter_name(self):
        result = _one(
            "def export(self, target):\n"
            '    """Export.\n\n    :param rows: the rows\n    :param target: where to\n    """\n'
            "    return target\n"
        )
        [issue] = _issues(result, "args-documented-not-in-signature")
        self.assertEqual(issue["documented"], "rows")

    def test_kwargs_and_cli_metavars_are_not_stale(self):
        result = _one(
            "def a(x, **options):\n"
            '    """A.\n\n    Args:\n        x: x\n        timeout: passed through\n    """\n'
            "def b(input_path):\n"
            '    """B.\n\n    Arguments:\n        INPUT_PATH: the file\n    """\n'
        )
        self.assertEqual(_issues(result), [])


class TestClaims(unittest.TestCase):
    def test_concurrency_claim_carries_scan_concurrency_keys(self):
        result = _one(
            "import threading\n"
            "class Counters:\n"
            "    def __init__(self):\n"
            "        self._lock = threading.Lock()\n"
            "    def snapshot(self):\n"
            '        """Return a lock-free copy of the counters."""\n'
            "        with self._lock:\n"
            "            return 1\n"
        )
        [claim] = [c for c in result["claims"] if c["category"] == "concurrency"]
        self.assertEqual(claim["function"], "Counters.snapshot")
        self.assertEqual(claim["function_key"], "pkg.mod:Counters.snapshot")
        self.assertEqual(claim["class_key"], "pkg.mod:Counters")
        self.assertEqual(claim["acquires_directly"], ["self._lock"])
        self.assertEqual(claim["line"], 6)

    def test_lock_names_and_comment_above_a_def(self):
        result = _one(
            "class Index:\n"
            "    # Must be called with `_index_lock` held by the caller.\n"
            "    def _apply(self):\n"
            "        pass\n"
        )
        [claim] = result["claims"]
        self.assertEqual(claim["source"], "comment")
        self.assertEqual(claim["function"], "Index._apply")
        self.assertIn("_index_lock", claim["locks_named"])

    def test_class_docstring_claim_has_no_body_to_measure(self):
        result = _one('class Pool:\n    """Thread-safe pool."""\n')
        [claim] = result["claims"]
        self.assertIsNone(claim["acquires_directly"])

    def test_safety_claims_and_hyphenated_safe(self):
        result = _one(
            "def run(code):\n"
            '    """Safely unpack the archive in a sandbox. Type-safe wrapper."""\n'
        )
        phrases = sorted(c["phrase"].lower() for c in result["claims"])
        self.assertEqual(phrases, ["safely", "sandbox"])

    def test_invariants_in_docstrings_not_comments(self):
        result = _one(
            "def f():\n"
            '    """The cache never holds a stale entry."""\n'
            "    # we always retry\n"
        )
        self.assertEqual([c["category"] for c in result["claims"]], ["invariant"])
        self.assertEqual(result["claims"][0]["source"], "docstring")

    def test_route_and_field_descriptions_are_scanned(self):
        result = _one(
            "class Item:\n"
            "    token: str = Field(description='Masked in every log line')\n"
            "def build(router):\n"
            "    @router.get('/stats', description='Non-blocking statistics view')\n"
            "    def stats():\n"
            "        return {}\n"
        )
        sources = sorted(c["source"] for c in result["claims"])
        self.assertEqual(sources, ["field_description", "route_description"])
        self.assertEqual(result["summary"]["described_fields"], 1)

    def test_repeated_phrase_in_one_docstring_is_one_claim(self):
        result = _one(
            "def scrub(a, b):\n"
            '    """Scrub.\n\n    Args:\n        a: value to be hidden\n'
            '        b: another value to be hidden\n    """\n'
        )
        self.assertEqual(len(result["claims"]), 1)

    def test_snippet_stops_at_entry_boundaries(self):
        result = _one(
            "def ctx():\n"
            '    """Build.\n\n    Returns:\n        dict: Secure session settings\n    """\n'
            "    return {}\n"
        )
        [claim] = result["claims"]
        self.assertEqual(claim["text"], "dict: Secure session settings")


class TestSuppressions(unittest.TestCase):
    def test_rule_ids_reason_and_scope(self):
        result = _one(
            "import subprocess\n"
            "def load(cmd):\n"
            "    # nosec B404 - only fixed arguments are passed\n"
            "    data = subprocess.run(cmd)  # nosec B603\n"
            "    return data  # noqa\n"
            "x: int = 'a'  # type: ignore[assignment]\n"
        )
        by_line = {s["line"]: s for s in result["suppressions"]}
        self.assertEqual(by_line[3]["scope"], "standalone")
        self.assertEqual(by_line[3]["rules"], ["B404"])
        self.assertEqual(by_line[3]["reason"], "only fixed arguments are passed")
        self.assertEqual(by_line[4]["scope"], "line")
        self.assertEqual(by_line[4]["code"], "data = subprocess.run(cmd)")
        self.assertEqual(by_line[4]["function"], "load")
        self.assertTrue(by_line[5]["blanket"])
        self.assertEqual(by_line[6]["tool"], "mypy")
        self.assertEqual(by_line[6]["rules"], ["assignment"])

    def test_file_level_forms(self):
        result = _one("# type: ignore\n# ruff: noqa: E501\nimport os\n")
        self.assertEqual([s["scope"] for s in result["suppressions"]], ["file", "file"])

    def test_suppression_comment_is_not_a_claim(self):
        result = _one("os.system(CMD)  # nosec B605 - safe, CMD is a constant\n")
        self.assertEqual(result["claims"], [])
        self.assertEqual(len(result["suppressions"]), 1)


class TestFiles(unittest.TestCase):
    def test_tests_skipped_by_default(self):
        files = {
            "pkg/mod.py": "def f():\n    pass\n",
            "tests/test_mod.py": "def test_f():\n    pass\n",
        }
        result = _survey(files)
        self.assertEqual(result["skipped"]["test_files"], 1)
        self.assertEqual(result["coverage"]["totals"]["function"]["total"], 1)
        result = _survey(files, include_tests=True)
        self.assertEqual(result["coverage"]["totals"]["function"]["total"], 2)

    def test_unparseable_file_is_listed(self):
        result = _survey({"pkg/ok.py": "x = 1\n", "pkg/bad.py": "def (:\n"})
        self.assertEqual(result["skipped"]["unparseable"], ["pkg/bad.py"])
        self.assertEqual(result["files_analyzed"], 2)

    def test_main_prints_json_and_honours_include_tests(self):
        with TempProject({"tests/test_x.py": "def test_a():\n    pass\n"}) as root:
            out = io.StringIO()
            argv = ["survey_docstrings.py", str(root), "--include-tests"]
            with mock.patch("sys.argv", argv), redirect_stdout(out):
                mod.main()
        payload = json.loads(out.getvalue())
        self.assertTrue(payload["skipped"]["include_tests"])
        self.assertIn("files_capped", payload)

    def test_help_exits_without_scanning(self):
        out = io.StringIO()
        with (
            mock.patch("sys.argv", ["survey_docstrings.py", "--help"]),
            redirect_stdout(out),
            self.assertRaises(SystemExit) as ctx,
        ):
            mod.main()
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn("Usage:", out.getvalue())


if __name__ == "__main__":
    unittest.main()
