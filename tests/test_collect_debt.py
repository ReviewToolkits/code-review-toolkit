"""Tests for collect_debt.py."""

import json
import unittest
from datetime import datetime, timedelta, timezone
from typing import ClassVar

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

    def test_pytest_skip_spellings(self):
        src = (
            "import pytest\n"
            "from pytest import mark\n"
            "@pytest.mark.skip(reason='flaky')\n"
            "def test_a(): pass\n"
            "@mark.skip('later')\n"
            "def test_b(): pass\n"
            "@pytest.mark.skipif(True, reason='x')\n"
            "def test_c(): pass\n"
            "def test_d():\n"
            "    pytest.skip('not on CI')\n"
            "class T:\n"
            "    def test_e(self):\n"
            "        self.skipTest('needs network')\n"
        )
        items = self._scan(src, "test_mod.py")
        self.assertEqual(
            [(i["line"], i["category"]) for i in items],
            [(3, "SKIP"), (5, "SKIP"), (7, "SKIP"), (10, "SKIP"), (13, "SKIP")],
        )

    def test_xfail_spellings(self):
        src = (
            "import pytest, unittest\n"
            "@pytest.mark.xfail(strict=True)\n"
            "def test_a(): pass\n"
            "def test_b():\n    pytest.xfail('known bug')\n"
            "@unittest.expectedFailure\n"
            "def test_c(): pass\n"
        )
        items = self._scan(src, "test_mod.py")
        self.assertEqual(
            [(i["line"], i["category"]) for i in items],
            [(2, "XFAIL"), (5, "XFAIL"), (6, "XFAIL")],
        )

    def test_skip_lookalikes_are_not_markers(self):
        src = (
            "bookmark.skip(1)\n"
            "self.mark.skip = True\n"
            "# pytest.skip('in a comment')\n"
            "def skip_rows(n): pass\n"
        )
        self.assertEqual(self._scan(src), [])

    def test_nosec_and_pylint_disable(self):
        src = (
            "result = eval(code)  # nosec B307\n"
            "import pickle  # pylint: disable=import-outside-toplevel\n"
        )
        items = self._scan(src)
        self.assertEqual(
            [(i["category"], i["text"]) for i in items],
            [("NOSEC", "B307"), ("PYLINT_DISABLE", "import-outside-toplevel")],
        )

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


def _run_main(argv: list[str]) -> dict:
    import contextlib
    import io
    import sys

    out = io.StringIO()
    old_argv = sys.argv
    sys.argv = ["collect_debt.py", *argv]
    try:
        with contextlib.redirect_stdout(out):
            mod.main()
    finally:
        sys.argv = old_argv
    return json.loads(out.getvalue())


_KEYED_PROJECT = {
    "pkg/__init__.py": '"""Package docstring citing ABC-7."""\n',
    "pkg/store.py": (
        "def flush():\n"
        "    # Deferred until ABC-12 lands; see roadmap.\n"
        "    return None\n"
        "x = 'ABC-99 in a string is not a citation'\n"
    ),
    "pkg/cache.py": "# ABC-3: done, kept as a pointer\nCACHE = {}\n",
    "ROADMAP.md": (
        "| key | item | target |\n|---|---|---|\n"
        "| ABC-12 | deferred flush | v1.2 |\n"
        "| ~~ABC-5~~ | shipped thing | v1.0 |\n"
        "| ABC-20 | planned work | v1.3 |\n"
    ),
    "CHANGELOG.md": "## 1.0\n- ABC-3 fixed the cache\n",
}


class TestTrackerKeys(unittest.TestCase):
    """Debt tracked by tracker key, not by TODO marker, was invisible."""

    def test_keys_from_comments_docstrings_and_docs(self):
        with TempProject(_KEYED_PROJECT) as root:
            out = _run_main([str(root), "--key-regex", r"\bABC-\d+\b"])
        tk = out["tracker_keys"]
        keys = {k["key"]: k for k in tk["keys"]}
        self.assertEqual(set(keys), {"ABC-3", "ABC-5", "ABC-7", "ABC-12", "ABC-20"})
        # Only comments and docstrings count as code citations.
        self.assertNotIn("ABC-99", keys)
        self.assertEqual(keys["ABC-12"]["code_refs"][0]["line"], 2)
        self.assertEqual(keys["ABC-7"]["code_ref_count"], 1)
        self.assertTrue(keys["ABC-3"]["in_changelog"])
        self.assertTrue(keys["ABC-5"]["struck_through"])
        self.assertIn("v1.2", keys["ABC-12"]["doc_refs"][0]["text"])
        # Cited in code with no resolution signal: open debt.
        self.assertEqual(tk["open_in_code"], ["ABC-7", "ABC-12"])
        self.assertEqual(tk["open_in_planning_docs"], ["ABC-12", "ABC-20"])
        # No .git in the temp project: commits were not searched, and say so.
        self.assertFalse(tk["git_log_searched"])
        self.assertIsNone(keys["ABC-12"]["commits"])

    def test_keys_are_sorted_naturally(self):
        files = {"m.py": "# ABC-10\n# ABC-9\n# ABC-100\n"}
        with TempProject(files) as root:
            out = _run_main([str(root), "--key-regex=ABC-\\d+"])
        self.assertEqual(
            [k["key"] for k in out["tracker_keys"]["keys"]],
            ["ABC-9", "ABC-10", "ABC-100"],
        )

    def test_not_requested_is_null_with_a_reason(self):
        with TempProject(_KEYED_PROJECT) as root:
            out = _run_main([str(root)])
        self.assertIsNone(out["tracker_keys"])
        self.assertIn("--key-regex", out["tracker_keys_reason"])

    def test_regex_value_is_not_taken_as_the_target(self):
        with TempProject(_KEYED_PROJECT) as root:
            out = _run_main([str(root), "--key-regex", "ABC-1"])
        self.assertEqual(out["scan_root"], str(root.resolve()))

    def test_invalid_regex_is_a_json_error(self):
        with TempProject({"m.py": ""}) as root, self.assertRaises(SystemExit) as ctx:
            _run_main([str(root), "--key-regex", "("])
        self.assertEqual(ctx.exception.code, 2)


_CONFIG_PROJECT = {
    "pkg/__init__.py": "",
    "pyproject.toml": (
        "[project]\n"
        'name = "demo"\n'
        "dependencies = [\n"
        '  "alpha==1.3.0",  # fork until upstream merges the fix\n'
        '  "beta>=1.2,<2.0.0",\n'
        '  "gamma>=1.0,<1.4",\n'
        "  # pinned below 3 until the new API is adopted\n"
        '  "delta>=2,<3",\n'
        '  "epsilon!=2.1.0",\n'
        '  "zeta>=1.0",\n'
        "]\n"
        "[tool.ruff.lint]\n"
        "ignore = [\n"
        '  "E501",\n'
        '  "B008",\n'
        "]\n"
        "[tool.ruff.lint.per-file-ignores]\n"
        '"tests/*" = ["S101"]\n'
        "[tool.mypy]\n"
        "ignore_missing_imports = true\n"
        "ignore_errors = false\n"
        "[tool.pytest.ini_options]\n"
        'filterwarnings = ["error", "ignore::DeprecationWarning"]\n'
        "[tool.codespell]\n"
        'ignore-words-list = "te"\n'
    ),
    "setup.cfg": "[flake8]\nextend-ignore =\n    E203,\n    W503\nmax-line-length = 100\n",
    "tox.ini": "[tox]\n# TODO: drop py38 once the CI image is updated\nenvlist = py310\n",
    "requirements-lock.txt": "".join(f"pkg{i}==1.{i}\n" for i in range(25)),
    ".github/workflows/ci.yml": (
        "jobs:\n"
        "  test:\n"
        "    continue-on-error: true\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "      - uses: some-org/shared-action@main\n"
        "      - uses: ./local-action\n"
        "      - run: make lint || true\n"
        "      # continue-on-error: true\n"
    ),
}


class TestConfigDebt(unittest.TestCase):
    """Pins, project-wide suppressions and CI failure allowances live outside
    the .py files the marker scan reads."""

    @classmethod
    def setUpClass(cls):
        files = dict(_CONFIG_PROJECT)
        pyproject = files.pop("pyproject.toml")
        with TempProject(files) as root:
            # TempProject writes its own minimal pyproject.toml last.
            (root / "pyproject.toml").write_text(pyproject, encoding="utf-8")
            cls.out = _run_main([str(root / "pkg")])
        cls.debt = cls.out["config_debt"]

    def test_pins_with_a_decision_are_listed_with_their_comment(self):
        pins = {p["name"]: p for p in self.debt["pins"]}
        self.assertEqual(set(pins), {"alpha", "gamma", "delta", "epsilon"})
        self.assertEqual(pins["alpha"]["kinds"], ["exact"])
        self.assertEqual(pins["alpha"]["comment"], "fork until upstream merges the fix")
        self.assertEqual(
            pins["delta"]["comment_above"],
            "pinned below 3 until the new API is adopted",
        )
        self.assertEqual(pins["epsilon"]["kinds"], ["exclusion"])

    def test_routine_major_caps_are_listed_compactly(self):
        self.assertEqual(
            self.debt["routine_caps"], ["beta>=1.2,<2.0.0 (pyproject.toml:5)"]
        )

    def test_lockfile_is_skipped_not_reported_pin_by_pin(self):
        self.assertEqual(self.debt["skipped_lockfiles"], ["requirements-lock.txt"])

    def test_project_wide_suppressions(self):
        found = {(s["tool"], s["setting"]) for s in self.debt["suppressions"]}
        self.assertEqual(
            found,
            {
                ("ruff", "ignore"),
                ("ruff", "tests/*"),
                ("mypy", "ignore_missing_imports"),
                ("pytest", "filterwarnings"),
                ("flake8", "extend-ignore"),
            },
        )
        ruff = next(s for s in self.debt["suppressions"] if s["setting"] == "ignore")
        self.assertIn("B008", ruff["value"])
        flake8 = next(s for s in self.debt["suppressions"] if s["tool"] == "flake8")
        self.assertIn("W503", flake8["value"])

    def test_ci_failure_allowances_and_floating_actions(self):
        self.assertEqual(
            [(c["line"], c["kind"]) for c in self.debt["ci"]],
            [(3, "continue_on_error"), (6, "unpinned_action"), (8, "error_ignored")],
        )

    def test_todo_in_a_config_comment(self):
        self.assertEqual(
            [(m["file"], m["category"]) for m in self.debt["markers"]],
            [("tox.ini", "TODO")],
        )

    def test_summary_counts(self):
        self.assertEqual(
            self.out["summary"]["config_debt"],
            {"pins": 4, "routine_caps": 1, "suppressions": 5, "ci": 3, "markers": 1},
        )


class TestTrackerKeyResolution(unittest.TestCase):
    """Done marks, changelog-only history and keys that live on branches."""

    FILES: ClassVar[dict[str, str]] = {
        "pkg/__init__.py": "",
        "ROADMAP.md": (
            "| key | item | target |\n|---|---|---|\n"
            "| ABC-1 | ~~Faster index~~ | v1.0 |\n"
            "| ABC-2 | Plugin registry | v1.6 |\n"
            "| ABC-4 | ~~Bulk export (needs ABC-2 first)~~ | v1.0 |\n"
            "| ABC-6 | Docs site | v2.0 |\n"
            "\n"
            "- [x] ABC-8 ticked item, see ABC-9\n"
            "- [ ] ABC-9 open item\n"
        ),
        "CHANGELOG.md": "## 1.0\n- ABC-6 first slice shipped\n- ABC-50 old fix\n",
    }

    def _tracker(self, files=None):
        with TempProject(files or self.FILES) as root:
            return _run_main([str(root), "--key-regex", r"\bABC-\d+\b"])["tracker_keys"]

    def test_struck_task_cell_marks_the_row_key_done(self):
        tk = self._tracker()
        self.assertEqual(tk["open_in_planning_docs"], ["ABC-2", "ABC-9"])

    def test_a_struck_row_does_not_close_a_key_it_only_mentions(self):
        keys = {k["key"]: k for k in self._tracker()["keys"]}
        self.assertTrue(keys["ABC-4"]["struck_through"])
        self.assertFalse(keys["ABC-2"]["struck_through"])
        self.assertTrue(keys["ABC-8"]["struck_through"])
        self.assertFalse(keys["ABC-9"]["struck_through"])

    def test_open_row_with_a_changelog_mention_is_a_conflicting_signal(self):
        self.assertEqual(self._tracker()["conflicting_signals"], ["ABC-6"])

    def test_changelog_only_keys_are_collapsed(self):
        tk = self._tracker()
        self.assertEqual(tk["changelog_only_keys"], ["ABC-50"])
        self.assertNotIn("ABC-50", [k["key"] for k in tk["keys"]])
        self.assertEqual(tk["summary"]["keys_only_in_changelog"], 1)
        self.assertEqual(tk["summary"]["keys_total"], 7)

    def test_branches_not_searched_without_git(self):
        tk = self._tracker()
        self.assertFalse(tk["unmerged_branches_searched"])
        self.assertIsNone(tk["branch_only"])
        self.assertIsNone(tk["summary"]["branch_only"])

    def test_key_only_on_an_unmerged_branch(self):
        import subprocess

        def git(root, *args):
            subprocess.run(
                ["git", "-c", "user.name=T", "-c", "user.email=t@t", *args],
                cwd=root,
                check=True,
                capture_output=True,
            )

        with TempProject(self.FILES) as root:
            git(root, "init", "-q", "-b", "main")
            git(root, "add", "-A")
            git(root, "commit", "-q", "-m", "roadmap; ABC-1 shipped")
            git(root, "checkout", "-q", "-b", "feat/retry")
            git(
                root, "commit", "-q", "--allow-empty", "-m", "fix: ABC-77 retry backoff"
            )
            git(root, "commit", "-q", "--allow-empty", "-m", "ABC-2: wip")
            git(root, "checkout", "-q", "main")
            tk = _run_main([str(root), "--key-regex", r"\bABC-\d+\b"])["tracker_keys"]
        self.assertTrue(tk["unmerged_branches_searched"])
        (only,) = tk["branch_only"]
        self.assertEqual(
            (only["key"], only["commits"], only["refs"]),
            ("ABC-77", 1, ["feat/retry"]),
        )
        keys = {k["key"]: k for k in tk["keys"]}
        self.assertEqual(keys["ABC-2"]["unmerged_commits"], 1)
        self.assertEqual(keys["ABC-1"]["unmerged_commits"], 0)
        self.assertEqual(keys["ABC-1"]["commits"], 1)


if __name__ == "__main__":
    unittest.main()
