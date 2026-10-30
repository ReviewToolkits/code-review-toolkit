"""Tests for check_known_findings.py -- the known-findings regression check."""

import ast
import unittest

from helpers import TempProject, import_script

mod = import_script("check_known_findings")


class TestParseLocations(unittest.TestCase):
    """Every one of these shapes appears in a real catalog row."""

    def test_single_line(self):
        self.assertEqual(mod.parse_locations("sqldata.py:390"), [("sqldata.py", [390])])

    def test_range(self):
        self.assertEqual(mod.parse_locations("a.py:10-12"), [("a.py", [10, 11, 12])])

    def test_comma_separated_lines_on_one_file(self):
        self.assertEqual(
            mod.parse_locations("autocomplete.py:117,134"),
            [("autocomplete.py", [117, 134])],
        )

    def test_continuation_segment_keeps_the_file(self):
        # `patch.py:56-57, :74-75` -- the second segment has no filename.
        # Reading it as a path made this row report `file_missing`.
        self.assertEqual(
            mod.parse_locations("patch.py:56-57, :74-75"),
            [("patch.py", [56, 57, 74, 75])],
        )

    def test_two_different_files(self):
        self.assertEqual(
            mod.parse_locations("tests/a.py:256-259, tests/b.py:290"),
            [("tests/a.py", [256, 257, 258, 259]), ("tests/b.py", [290])],
        )

    def test_bare_path_with_no_line(self):
        self.assertEqual(mod.parse_locations("a.py"), [("a.py", [])])


class TestQualnames(unittest.TestCase):
    SOURCE = (
        "def top():\n"
        "    pass\n"
        "\n"
        "class C:\n"
        "    def m(self):\n"
        "        def inner():\n"
        "            pass\n"
        "        return inner\n"
    )

    def setUp(self):
        self.index = mod.qualname_index(ast.parse(self.SOURCE))

    def test_module_level_line_has_no_qualname(self):
        self.assertEqual(mod.qualname_at(self.index, 3), "")

    def test_function(self):
        self.assertEqual(mod.qualname_at(self.index, 2), "top")

    def test_method_is_dotted(self):
        self.assertEqual(mod.qualname_at(self.index, 8), "C.m")

    def test_innermost_wins(self):
        self.assertEqual(mod.qualname_at(self.index, 7), "C.m.inner")


class TestVerdicts(unittest.TestCase):
    """The verdict order encodes 'strongest regression signal first'."""

    def test_present_is_strongest(self):
        self.assertEqual(mod._ORDER[0], "present")

    def test_not_checked_verdicts_are_last(self):
        # out_of_scope and not_scannable must never outrank a real observation:
        # a multi-site finding takes the strongest verdict, and "we didn't look"
        # must lose to anything we did look at.
        self.assertEqual(mod._ORDER[-2:], ("file_missing", "not_scannable"))
        self.assertIn("out_of_scope", mod._ORDER)
        self.assertGreater(mod._ORDER.index("out_of_scope"), mod._ORDER.index("absent"))

    def test_agent_only_shapes_are_not_scannable(self):
        scannable = mod._scannable_shapes()
        # A shape the scanner implements.
        self.assertIn("mutable-default-argument", scannable)
        # A shape confirmed only by cross-artifact judgement.
        self.assertNotIn("fix-not-propagated-to-sibling-path", scannable)


class TestErrors(unittest.TestCase):
    def test_missing_catalog_is_an_error(self):
        result = mod.analyze(".", catalog="/no/such/catalog.tsv")
        self.assertIn("error", result)

    def test_catalog_is_required(self):
        self.assertIn("error", mod.analyze("."))


class TestSuppressedVerdict(unittest.TestCase):
    """A finding a differential now settles is `suppressed`, not `absent`."""

    SOURCE = (
        "def first(items=[]):\n"
        "    return items\n"
        "\n"
        "def second(items=[]):\n"
        "    return items\n"
    )

    def _run(self, findings, suppressed):
        def fake_scan(target, max_files=0):
            return {
                "files_analyzed": 1,
                "findings": findings,
                "suppressed": suppressed,
            }

        catalog = (
            "# id\tlocation\tshape\tseverity\tstatus\n"
            "K-1\tmod.py:2\tmutable-default-argument\tFIX\treproduced\n"
        )
        original = mod.scan_python_pitfalls.analyze
        mod.scan_python_pitfalls.analyze = fake_scan
        try:
            with TempProject({"pkg/mod.py": self.SOURCE}) as root:
                (root / "known.tsv").write_text(catalog, encoding="utf-8")
                return mod.analyze(str(root / "pkg"), catalog=str(root / "known.tsv"))
        finally:
            mod.scan_python_pitfalls.analyze = original

    @staticmethod
    def _entry(line, **extra):
        return {
            "file": "pkg/mod.py",
            "line": line,
            "shape": "mutable-default-argument",
            **extra,
        }

    def test_moved_to_suppressed_is_suppressed_not_absent(self):
        result = self._run([], [self._entry(1, reason="returns-error-result")])
        (record,) = result["results"]
        self.assertEqual(record["verdict"], "suppressed")
        self.assertIn("returns-error-result", record["note"])
        self.assertEqual(result["summary"]["suppressed"], 1)
        self.assertEqual(result["summary"]["possibly_fixed"], 0)
        self.assertEqual(result["findings"], [])

    def test_suppressed_in_another_function_is_still_absent(self):
        result = self._run([], [self._entry(4, reason="returns-error-result")])
        self.assertEqual(result["results"][0]["verdict"], "absent")

    def test_reported_finding_still_wins(self):
        result = self._run(
            [self._entry(1)], [self._entry(1, reason="returns-error-result")]
        )
        self.assertEqual(result["results"][0]["verdict"], "present")


if __name__ == "__main__":
    unittest.main()
