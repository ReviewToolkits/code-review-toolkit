"""Tests for check_locations.py: every finding's path:line exists at the commit."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from helpers import TempProject, import_script
from test_analyze_history import GitTempProject, _git

cl = import_script("check_locations")

_FIVE = "a = 1\nb = 2\nc = 3\nd = 4\ne = 5\n"


def _run(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        try:
            cl.main(argv)
            code = 0
        except SystemExit as exc:
            code = exc.code
    return json.loads(buf.getvalue()), code


class TestParseLocation(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(cl.parse_location("pkg/a.py"), ("pkg/a.py", []))
        self.assertEqual(cl.parse_location("pkg/a.py:3"), ("pkg/a.py", [(3, 3)]))
        self.assertEqual(cl.parse_location("pkg/a.py:3-5"), ("pkg/a.py", [(3, 5)]))
        self.assertEqual(
            cl.parse_location("pkg/a.py:3, 9-10"), ("pkg/a.py", [(3, 3), (9, 10)])
        )
        self.assertEqual(cl.parse_location("pkg/a.py:L3-L4"), ("pkg/a.py", [(3, 4)]))
        self.assertEqual(cl.parse_location("pkg/"), ("pkg/", []))

    def test_prose_is_unparseable(self):
        self.assertIsNone(
            cl.parse_location("the helper spans pkg/a.py:1-9 per the report")
        )

    def test_locations_of_every_schema(self):
        self.assertEqual(
            cl.locations_of(
                {
                    "locations": "x.py:1",
                    "location": "y.py:2",
                    "sites": ["z.py:3"],
                    "file": "w.py",
                    "line": 4,
                    "end_line": 6,
                }
            ),
            ["x.py:1", "y.py:2", "z.py:3", "w.py:4-6"],
        )


class TestCheckLocations(unittest.TestCase):
    def setUp(self):
        project = GitTempProject(
            [
                {
                    "files": {"pkg/__init__.py": "", "pkg/mod.py": _FIVE},
                    "message": "Add mod",
                },
                {
                    "files": {"pkg/mod.py": _FIVE + "f = 6\ng = 7\n"},
                    "message": "Grow mod",
                },
            ]
        )
        self.root = project.__enter__()
        self.addCleanup(project.__exit__, None, None, None)
        self.old = _git(self.root, "rev-parse", "HEAD~1")

    def check(self, findings, *extra, target=None):
        fd, name = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        p = Path(name)
        p.write_text(json.dumps({"findings": findings}))
        self.addCleanup(p.unlink)
        return _run([str(target or self.root), "--findings", str(p), *extra])

    def test_every_classification_is_checked(self):
        findings = [
            {"id": "F-1", "classification": "FIX", "locations": ["pkg/mod.py:2-4"]},
            {
                "id": "F-2",
                "classification": "CONSIDER",
                "locations": ["pkg/mod.py:6-9"],
            },
            {"id": "F-3", "classification": "POLICY", "locations": ["pkg/gone.py:1"]},
            {
                "id": "F-4",
                "classification": "ACCEPTABLE",
                "locations": ["pkg/", "pkg/:3"],
            },
            {
                "id": "F-5",
                "classification": "CONSIDER",
                "locations": ["pkg/mod.py:0", "pkg/mod.py:4-2"],
            },
            {
                "id": "F-6",
                "classification": "CONSIDER",
                "locations": ["see the report"],
            },
            {"id": "F-7", "classification": "CONSIDER"},
        ]
        result, code = self.check(findings)
        self.assertEqual(code, 1)
        got = {(p["id"], p["location"], p["problem"]) for p in result["problems"]}
        self.assertEqual(
            got,
            {
                ("F-2", "pkg/mod.py:6-9", "line-past-eof"),
                ("F-3", "pkg/gone.py:1", "file-missing"),
                ("F-4", "pkg/:3", "lines-on-directory"),
                ("F-5", "pkg/mod.py:0", "bad-range"),
                ("F-5", "pkg/mod.py:4-2", "bad-range"),
                ("F-6", "see the report", "unparseable"),
                ("F-7", None, "no-locations"),
            },
        )
        self.assertEqual(result["locations_ok"], 2)
        self.assertEqual(result["commit"], _git(self.root, "rev-parse", "HEAD"))
        self.assertEqual(result["problems_by_classification"]["CONSIDER"], 5)
        result, _ = self.check([{"id": "F-8", "locations": ["mod.py:1"]}])
        self.assertEqual(result["problems"][0]["detail"], "did you mean pkg/mod.py?")

    def test_reads_the_reviewed_commit_not_the_working_tree(self):
        (self.root / "pkg" / "mod.py").write_text("x = 1\n")  # uncommitted edit
        findings = [{"id": "F-1", "locations": ["pkg/mod.py:7"]}]
        result, code = self.check(findings)
        self.assertEqual(code, 0, result["problems"])
        result, code = self.check(findings, "--commit", self.old)
        self.assertEqual(code, 1)
        self.assertEqual(result["problems"][0]["detail"], "line 7, file has 5 lines")

    def test_last_line_is_in_range(self):
        result, code = self.check(
            [{"id": "F-1", "locations": ["pkg/mod.py:7", "./pkg/mod.py:1"]}]
        )
        self.assertEqual(code, 0, result["problems"])

    def test_paths_relative_to_a_subdirectory_project(self):
        sub = self.root / "pkg"
        (sub / "pyproject.toml").write_text("")
        _git(self.root, "add", "pkg/pyproject.toml")
        _git(self.root, "commit", "-qm", "Sub project")
        result, code = self.check(
            [{"id": "F-1", "locations": ["mod.py:3", "pkg/mod.py:3"]}], target=sub
        )
        self.assertEqual(code, 0, result["problems"])

    def test_bad_commit_is_an_error(self):
        result, code = self.check([], "--commit", "no-such-ref")
        self.assertEqual(code, 2)
        self.assertIn("error", result)

    def test_max_files_caps_and_says_so(self):
        _git(self.root, "mv", "pkg/__init__.py", "pkg/other.py")
        (self.root / "pkg" / "other.py").write_text("z = 1\n")
        _git(self.root, "commit", "-qam", "Other")
        findings = [{"id": "F-1", "locations": ["pkg/mod.py:1", "pkg/other.py:1"]}]
        result, _code = self.check(findings, "--max-files", "1")
        self.assertTrue(result["files_capped"])
        self.assertEqual(result["locations_skipped"], 1)


class TestWithoutGit(unittest.TestCase):
    def test_working_tree_is_read(self):
        with TempProject({"pkg/a.py": "x = 1\ny = 2\n"}) as root:
            p = Path(tempfile.mkdtemp()) / "f.json"
            p.write_text(
                json.dumps([{"id": "F-1", "locations": ["pkg/a.py:2", "pkg/a.py:3"]}])
            )
            result, _code = _run([str(root), "--findings", str(p)])
            p.unlink()
            p.parent.rmdir()
        self.assertIsNone(result["commit"])
        self.assertEqual([x["problem"] for x in result["problems"]], ["line-past-eof"])


if __name__ == "__main__":
    unittest.main()
