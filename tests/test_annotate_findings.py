"""Tests for annotate_findings.py."""

import io
import json
import sys
import unittest
from pathlib import Path

from helpers import import_script
from test_analyze_history import GitTempProject, _git

mod = import_script("annotate_findings")

_V1 = "def scale(x):\n    total = x\n    return total\n\n\ndef other():\n    return 0\n"
_V2 = _V1.replace("    return total\n", "    return total + 1\n")
_V3 = _V1.replace("    return total\n", "    return  total + 1  # adjusted\n")


def _repo(testcase) -> Path:
    project = GitTempProject(
        [
            {"files": {"pkg/calc.py": _V1}, "message": "Add scale"},
            {"files": {"pkg/calc.py": _V2}, "message": "fix: off by one in scale (K3)"},
            {"files": {"pkg/calc.py": _V3}, "message": "style: tidy scale"},
        ]
    )
    root = project.__enter__()
    testcase.addCleanup(project.__exit__, None, None, None)
    _git(root, "tag", "v1.0rc1", "HEAD~2")
    _git(root, "tag", "v1.0", "HEAD~1")
    _git(root, "tag", "nightly", "HEAD~1")
    main_sha = _git(root, "rev-parse", "HEAD")
    # Branch that rewrites scale, branch that only touches other(),
    # branch that deletes scale.
    for name, text in (
        ("rewrite", _V3.replace("total + 1", "max(total, 0) + 1")),
        ("elsewhere", _V3.replace("return 0", "return 1")),
        ("dropped", "def other():\n    return 0\n"),
    ):
        _git(root, "checkout", "-q", "-b", name, main_sha)
        (root / "pkg" / "calc.py").write_text(text, encoding="utf-8")
        _git(root, "commit", "-qam", f"Work on {name}")
        _git(root, "update-ref", f"refs/remotes/origin/{name}", "HEAD")
    _git(root, "checkout", "-q", "main")
    _git(root, "update-ref", "refs/remotes/origin/main", main_sha)
    _git(root, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    return root


def _line(text: str, needle: str) -> int:
    return next(i for i, line in enumerate(text.splitlines(), 1) if needle in line)


class TestAnnotateFindings(unittest.TestCase):
    def setUp(self):
        self.root = _repo(self)
        self.line = _line(_V3, "return  total")

    def _annotate(self, findings, **kwargs):
        return mod.analyze(str(self.root), findings=findings, **kwargs)

    def test_introducing_commit_skips_layout_only_changes(self):
        result = self._annotate(
            [{"id": "F1", "file": "pkg/calc.py", "line": self.line}]
        )
        ann = result["annotations"][0]
        self.assertEqual(ann["function"], "scale")
        self.assertEqual(
            ann["introduced_by"]["message"], "fix: off by one in scale (K3)"
        )
        self.assertEqual(len(ann["skipped_layout_only"]), 1)
        self.assertEqual(ann["line_history"][0]["message"], "style: tidy scale")
        self.assertTrue(ann["line_history"][0]["layout_only"])
        # blame -w is a cross-check: it ignores whitespace but sees comments.
        self.assertIsNotNone(ann["blame"])

    def test_first_release_ignores_non_release_tags(self):
        ann = self._annotate([{"file": "pkg/calc.py", "line": self.line}])[
            "annotations"
        ][0]
        self.assertEqual(ann["first_release"], "v1.0")

    def test_related_fixes_touching_the_function(self):
        ann = self._annotate([{"file": "pkg/calc.py", "line": self.line}])[
            "annotations"
        ][0]
        self.assertEqual(
            [f["message"] for f in ann["related_fixes"]],
            ["fix: off by one in scale (K3)"],
        )
        self.assertEqual(ann["related_fixes"][0]["keys"], ["K3"])

    def test_branch_presence_compares_the_function(self):
        result = self._annotate(
            [
                {
                    "file": "pkg/calc.py",
                    "line": self.line,
                    "signature": "return total + 1",
                }
            ]
        )
        self.assertEqual(result["unmerged_branches"]["base"], "origin/main")
        by_branch = {b["branch"]: b for b in result["annotations"][0]["branches"]}
        self.assertEqual(by_branch["origin/rewrite"]["status"], "changed")
        self.assertFalse(by_branch["origin/rewrite"]["signature_present"])
        self.assertEqual(by_branch["origin/elsewhere"]["status"], "unchanged")
        self.assertTrue(by_branch["origin/elsewhere"]["signature_present"])
        self.assertEqual(by_branch["origin/dropped"]["status"], "function-absent")

    def test_empty_pickaxe_is_null_not_head(self):
        ann = self._annotate(
            [{"file": "pkg/calc.py", "line": self.line, "pattern": "never written"}],
            no_branches=True,
        )["annotations"][0]
        self.assertIsNone(ann["pickaxe"])
        self.assertIn("not falling back to HEAD", ann["pickaxe_reason"])

    def test_pickaxe_finds_the_adding_commit(self):
        ann = self._annotate(
            [{"file": "pkg/calc.py", "line": self.line, "pattern": "total = x"}],
            no_branches=True,
        )["annotations"][0]
        self.assertEqual(ann["pickaxe"]["message"], "Add scale")

    def test_module_level_line_has_no_function(self):
        ann = self._annotate([{"file": "pkg/calc.py", "line": 4}], no_branches=True)[
            "annotations"
        ][0]
        self.assertIsNone(ann["function"])
        self.assertEqual(ann["related_fixes"], [])

    def test_bad_inputs_are_reported_not_guessed(self):
        result = self._annotate(
            [
                {"id": "missing", "file": "pkg/nope.py", "line": 1},
                {"id": "far", "file": "pkg/calc.py", "line": 999},
            ],
            no_branches=True,
        )
        self.assertEqual(result["skipped"][0]["id"], "missing")
        self.assertIn("outside the file", result["annotations"][0]["error"])

    def test_max_files_caps_distinct_files(self):
        (self.root / "pkg" / "b.py").write_text("y = 1\n", encoding="utf-8")
        _git(self.root, "add", "pkg/b.py")
        _git(self.root, "commit", "-qm", "Add b")
        result = self._annotate(
            [
                {"file": "pkg/calc.py", "line": self.line},
                {"file": "pkg/b.py", "line": 1},
            ],
            max_files=1,
            no_branches=True,
        )
        self.assertTrue(result["files_capped"])
        self.assertEqual(result["findings_annotated"], 1)
        self.assertEqual(result["skipped"][0]["reason"], "--max-files cap")

    def test_cli_reads_a_findings_file(self):
        findings = self.root / "findings.json"
        findings.write_text(
            json.dumps({"findings": [{"file": "pkg/calc.py", "line": self.line}]}),
            encoding="utf-8",
        )
        old_stdout, old_argv = sys.stdout, sys.argv
        sys.stdout = io.StringIO()
        sys.argv = [
            "annotate_findings.py",
            str(self.root),
            "--findings",
            str(findings),
            "--no-branches",
        ]
        try:
            mod.main()
            output = json.loads(sys.stdout.getvalue())
        finally:
            sys.stdout, sys.argv = old_stdout, old_argv
        self.assertEqual(output["findings_annotated"], 1)
        self.assertEqual(output["files_total"], 1)


class TestLayoutDetection(unittest.TestCase):
    def test_layout_key(self):
        self.assertEqual(
            mod._layout_key(["    return  x"]), mod._layout_key(["return x"])
        )
        self.assertNotEqual(
            mod._layout_key(["return x"]), mod._layout_key(["return x + 1"])
        )
        # A comment-only line, or a trailing comment, is layout.
        self.assertEqual(
            mod._layout_key(["# why", "return x  # note"]),
            mod._layout_key(["return x"]),
        )
        # A "#" inside a string is code.
        self.assertNotEqual(
            mod._layout_key(["s = 'a#b'"]), mod._layout_key(["s = 'a'"])
        )

    def test_missing_findings_file_is_an_error(self):
        result = mod.analyze(".", findings_path="/definitely/not/there.json")
        self.assertIn("error", result)


if __name__ == "__main__":
    unittest.main()
