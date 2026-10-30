"""Tests for collect_debt.py."""

import json
import unittest
from datetime import datetime, timedelta, timezone

from helpers import TempProject, import_script

mod = import_script("collect_debt")


class TestMarkerDetection(unittest.TestCase):
    """Test that debt markers are correctly identified."""

    def _scan(self, source: str, filename: str = "mod.py") -> list[dict]:
        with TempProject({filename: source}) as root:
            return mod.scan_file(root / filename, root, use_git=False)

    def test_todo(self):
        items = self._scan("x = 1  # TODO: fix this later\n")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["category"], "TODO")
        self.assertEqual(items[0]["text"], "fix this later")

    def test_fixme(self):
        items = self._scan("# FIXME: race condition here\n")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["category"], "FIXME")

    def test_hack(self):
        items = self._scan("# HACK: temporary workaround for #123\n")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["category"], "HACK")

    def test_workaround(self):
        items = self._scan("# WORKAROUND: upstream bug\n")
        self.assertEqual(len(items), 1)
        self.assertEqual(
            items[0]["category"], "HACK"
        )  # HACK and WORKAROUND share category

    def test_xxx(self):
        items = self._scan("# XXX: needs review\n")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["category"], "XXX")

    def test_noqa(self):
        items = self._scan("x = something_long  # noqa: E501\n")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["category"], "NOQA")

    def test_type_ignore(self):
        items = self._scan("x = foo()  # type: ignore[assignment]\n")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["category"], "TYPE_IGNORE")

    def test_pragma_no_cover(self):
        items = self._scan("if DEBUG:  # pragma: no cover\n")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["category"], "PRAGMA_NO_COVER")

    def test_case_insensitive(self):
        items = self._scan("# todo: lowercase\n# Todo: mixed case\n# TODO: uppercase\n")
        self.assertEqual(len(items), 3)
        for item in items:
            self.assertEqual(item["category"], "TODO")

    def test_skip_decorator(self):
        items = self._scan(
            "import unittest\n\n@unittest.skip('broken')\ndef test_foo():\n    pass\n"
        )
        skip_items = [i for i in items if i["category"] == "SKIP"]
        self.assertEqual(len(skip_items), 1)

    def test_multiple_markers(self):
        items = self._scan(
            "# TODO: first thing\n"
            "x = 1\n"
            "# FIXME: second thing\n"
            "y = 2  # HACK: third thing\n"
        )
        categories = [i["category"] for i in items]
        self.assertIn("TODO", categories)
        self.assertIn("FIXME", categories)
        self.assertIn("HACK", categories)

    def test_no_false_positives(self):
        items = self._scan(
            "# This is a regular comment\n"
            "x = 1\n"
            "# Another regular comment about the algorithm\n"
            "def process(data):\n"
            "    return data\n"
        )
        self.assertEqual(len(items), 0)

    def test_context_captured(self):
        items = self._scan(
            "def process():\n    # TODO: optimize this loop\n    for x in range(100):\n"
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["context_before"], "def process():")
        self.assertEqual(items[0]["context_after"], "for x in range(100):")

    def test_line_numbers_correct(self):
        items = self._scan("line1 = 1\nline2 = 2\n# TODO: on line 3\nline4 = 4\n")
        self.assertEqual(items[0]["line"], 3)


class TestAgeClassification(unittest.TestCase):
    """Test the age classification logic."""

    def test_fresh(self):
        now = datetime.now(tz=timezone.utc)
        recent = (now - timedelta(days=5)).isoformat()
        self.assertEqual(mod._classify_age(recent), "fresh")

    def test_growing(self):
        now = datetime.now(tz=timezone.utc)
        months_ago = (now - timedelta(days=90)).isoformat()
        self.assertEqual(mod._classify_age(months_ago), "growing")

    def test_stale(self):
        now = datetime.now(tz=timezone.utc)
        half_year = (now - timedelta(days=250)).isoformat()
        self.assertEqual(mod._classify_age(half_year), "stale")

    def test_ancient(self):
        now = datetime.now(tz=timezone.utc)
        old = (now - timedelta(days=500)).isoformat()
        self.assertEqual(mod._classify_age(old), "ancient")

    def test_none_is_unknown(self):
        self.assertEqual(mod._classify_age(None), "unknown")

    def test_invalid_date_is_unknown(self):
        self.assertEqual(mod._classify_age("not-a-date"), "unknown")


class TestEndToEnd(unittest.TestCase):
    """Integration test: scan a small project without git."""

    def test_project_scan(self):
        with TempProject(
            {
                "pkg/core.py": (
                    "# TODO: refactor this\ndef main():\n    pass  # FIXME: implement\n"
                ),
                "pkg/utils.py": ("def helper():\n    return 42\n"),
                "tests/test_core.py": ("import unittest\n# TODO: add more tests\n"),
            }
        ) as root:
            files = mod.discover_python_files(root)
            all_items = []
            for f in files:
                all_items.extend(mod.scan_file(f, root, use_git=False))

            categories = [i["category"] for i in all_items]
            self.assertEqual(categories.count("TODO"), 2)
            self.assertEqual(categories.count("FIXME"), 1)

            # All should have unknown age (no git).
            for item in all_items:
                self.assertEqual(item["age"], "unknown")


class TestMaxFiles(unittest.TestCase):
    """Test --max-files caps file processing."""

    def test_max_files_caps_output(self):
        files = {
            f"pkg/mod{i}.py": f"# TODO: fix mod{i}\nx{i} = {i}\n" for i in range(10)
        }
        files["pkg/__init__.py"] = ""
        with TempProject(files) as root:
            import io
            import sys

            old_stdout = sys.stdout
            sys.stdout = io.StringIO()
            old_argv = sys.argv
            sys.argv = [
                "collect_debt.py",
                str(root),
                "--max-files",
                "3",
            ]
            try:
                mod.main()
                output = json.loads(sys.stdout.getvalue())
            finally:
                sys.stdout = old_stdout
                sys.argv = old_argv
            self.assertEqual(output["files_analyzed"], 3)
            self.assertTrue(output["files_capped"])
            self.assertGreater(output["files_total"], 3)


if __name__ == "__main__":
    unittest.main()
