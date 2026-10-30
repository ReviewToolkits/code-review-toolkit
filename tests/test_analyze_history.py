"""Tests for analyze_history.py."""

import os
import subprocess
import unittest
from pathlib import Path

from helpers import TempProject, import_script

mod = import_script("analyze_history")


class GitTempProject(TempProject):
    """TempProject that initializes a git repo with commits.

    Each commit dict should have:
      - files: dict of {path: content} to create/modify
      - message: commit message
      - date: optional ISO date string (defaults to sequential dates)
      - committer_date: optional, when it should differ from `date`
      - author: optional author name (defaults to "Test Author")
    """

    def __init__(self, commits: list[dict]):
        # Collect all files from all commits for initial file set.
        all_files: dict[str, str] = {}
        for commit in commits:
            all_files.update(commit["files"])
        super().__init__(all_files)
        self._commits = commits

    def __enter__(self) -> Path:
        root = super().__enter__()
        # Initialize git repo.
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=str(root),
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=str(root),
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test Author"],
            cwd=str(root),
            capture_output=True,
        )
        # Create commits in order.
        for i, commit in enumerate(self._commits):
            author = commit.get("author", "Test Author")
            date = commit.get("date", f"2025-01-{i + 1:02d}T12:00:00+00:00")
            for relpath, content in commit["files"].items():
                filepath = root / relpath
                filepath.parent.mkdir(parents=True, exist_ok=True)
                filepath.write_text(content, encoding="utf-8")
            subprocess.run(
                ["git", "add", "-A"],
                cwd=str(root),
                capture_output=True,
            )
            env = os.environ.copy()
            env["GIT_AUTHOR_DATE"] = date
            env["GIT_COMMITTER_DATE"] = commit.get("committer_date", date)
            env["GIT_AUTHOR_NAME"] = author
            env["GIT_COMMITTER_NAME"] = author
            env["GIT_AUTHOR_EMAIL"] = "test@test.com"
            env["GIT_COMMITTER_EMAIL"] = "test@test.com"
            subprocess.run(
                ["git", "commit", "-m", commit["message"], "--allow-empty"],
                cwd=str(root),
                capture_output=True,
                env=env,
            )
        return root


class TestCommitClassification(unittest.TestCase):
    """Test commit message classification."""

    def test_fix(self):
        self.assertEqual(mod.classify_commit("Fix null check in parser"), "fix")

    def test_feature(self):
        self.assertEqual(
            mod.classify_commit("Add support for --verbose flag"), "feature"
        )

    def test_refactor(self):
        self.assertEqual(
            mod.classify_commit("Refactor config loading into separate module"),
            "refactor",
        )

    def test_docs(self):
        self.assertEqual(
            mod.classify_commit("Update README with installation instructions"), "docs"
        )

    def test_test(self):
        self.assertEqual(
            mod.classify_commit("Add tests for edge cases in runner"), "test"
        )

    def test_chore(self):
        self.assertEqual(mod.classify_commit("Bump version to 1.2.3"), "chore")

    def test_unknown(self):
        self.assertEqual(mod.classify_commit("Miscellaneous changes"), "unknown")

    def test_case_insensitive(self):
        self.assertEqual(mod.classify_commit("FIX: crash on startup"), "fix")

    def test_first_match_wins(self):
        # "fix" comes before "test" in rules.
        self.assertEqual(mod.classify_commit("Fix bug and add test"), "fix")

    def test_bug_keyword(self):
        self.assertEqual(mod.classify_commit("Address bug in runner"), "fix")

    def test_hotfix(self):
        self.assertEqual(mod.classify_commit("hotfix: critical issue"), "fix")

    def test_implement(self):
        self.assertEqual(mod.classify_commit("implement new parser"), "feature")

    def test_merge(self):
        self.assertEqual(mod.classify_commit("Merge branch 'main'"), "chore")


class TestSubjectSignals(unittest.TestCase):
    """Pre-conventional subjects: domain nouns, leading verbs, lint sweeps."""

    def _detail(self, subject):
        detail = mod.classify_commit_detail(subject)
        return detail["type"], detail["source"]

    def test_domain_noun_document_is_not_docs(self):
        # Where documents are the data, "documents" is not the docs.
        self.assertNotEqual(
            mod.classify_commit("[api] Paginate the documents listing"), "docs"
        )
        self.assertNotEqual(
            mod.classify_commit("Purge expired documents from the archive"),
            "docs",
        )

    def test_domain_noun_version_is_not_chore(self):
        self.assertNotEqual(
            mod.classify_commit("Show the schema version on the status page"),
            "chore",
        )

    def test_document_as_leading_verb_is_still_docs(self):
        self.assertEqual(self._detail("Document the timeout flag"), ("docs", "verb"))

    def test_leading_verb_beats_a_later_keyword(self):
        # "failure" is a fix keyword, but the commit adds a feature.
        self.assertEqual(
            self._detail("Add a reconnect option for socket failures"),
            ("feature", "verb"),
        )

    def test_explicit_fix_word_beats_a_feature_verb(self):
        self.assertEqual(
            self._detail("Add a guard to fix the crash on empty input"),
            ("fix", "verb"),
        )

    def test_lint_sweep_is_not_a_fix(self):
        self.assertEqual(
            self._detail("Fix the flake8 warnings in utils"), ("chore", "strong")
        )
        self.assertEqual(
            self._detail("run black and format the package"), ("chore", "strong")
        )
        self.assertEqual(self._detail("Add a mypy job"), ("chore", "strong"))

    def test_docstring_work_is_docs(self):
        self.assertEqual(
            self._detail("Add docstrings to the parser module"), ("docs", "strong")
        )
        self.assertEqual(
            self._detail("fix spelling in the user guide"), ("docs", "strong")
        )

    def test_a_bug_word_keeps_a_lint_or_doc_subject_a_fix(self):
        self.assertEqual(mod.classify_commit("Fix crash in the lint command"), "fix")
        self.assertEqual(
            mod.classify_commit("Fix crash when a docstring is missing"), "fix"
        )

    def test_unclosed_conventional_scope_still_states_the_type(self):
        self.assertEqual(
            self._detail("feat(export: write timestamps in UTC"),
            ("feature", "conventional"),
        )

    def test_conventional_chore_with_fix_inside_stays_chore(self):
        self.assertEqual(
            self._detail("chore: fix spelling in comments"), ("chore", "conventional")
        )
        self.assertEqual(
            self._detail("ci: fix the release job"), ("chore", "conventional")
        )

    def test_area_prefix_is_skipped_before_the_verb(self):
        self.assertEqual(self._detail("cli: add a --quiet flag"), ("feature", "verb"))

    def test_plain_format_noun_is_not_a_lint_sweep(self):
        self.assertEqual(mod.classify_commit("Fix date format parsing"), "fix")


class TestParseArgs(unittest.TestCase):
    """Test CLI argument parsing."""

    def test_defaults(self):
        args = mod.parse_args([])
        self.assertEqual(args["path"], ".")
        self.assertEqual(args["days"], 90)
        self.assertIsNone(args["since"])
        self.assertIsNone(args["until"])
        self.assertIsNone(args["last"])
        self.assertEqual(args["max_commits"], 2000)
        self.assertFalse(args["no_function"])

    def test_days(self):
        args = mod.parse_args(["--days", "30"])
        self.assertEqual(args["days"], 30)

    def test_last(self):
        args = mod.parse_args(["--last", "5"])
        self.assertEqual(args["last"], 5)

    def test_max_commits(self):
        args = mod.parse_args(["--max-commits", "10"])
        self.assertEqual(args["max_commits"], 10)

    def test_no_function(self):
        args = mod.parse_args(["--no-function"])
        self.assertTrue(args["no_function"])

    def test_since_until(self):
        args = mod.parse_args(["--since", "2025-01-01", "--until", "2025-02-01"])
        self.assertEqual(args["since"], "2025-01-01")
        self.assertEqual(args["until"], "2025-02-01")

    def test_path(self):
        args = mod.parse_args(["src/"])
        self.assertEqual(args["path"], "src/")

    def test_combined(self):
        args = mod.parse_args(["src/", "--days", "60", "--no-function"])
        self.assertEqual(args["path"], "src/")
        self.assertEqual(args["days"], 60)
        self.assertTrue(args["no_function"])


class TestDiffTruncation(unittest.TestCase):
    """Test diff truncation logic."""

    def test_short_diff_unchanged(self):
        diff = "line1\nline2\nline3"
        self.assertEqual(mod._truncate_diff(diff, 10), diff)

    def test_exact_limit_unchanged(self):
        diff = "\n".join(f"line{i}" for i in range(10))
        self.assertEqual(mod._truncate_diff(diff, 10), diff)

    def test_long_diff_truncated(self):
        diff = "\n".join(f"line{i}" for i in range(200))
        result = mod._truncate_diff(diff, 150)
        lines = result.splitlines()
        self.assertEqual(len(lines), 151)  # 150 + truncation notice
        self.assertIn("diff truncated", lines[-1])

    def test_truncation_notice(self):
        diff = "\n".join(f"line{i}" for i in range(200))
        result = mod._truncate_diff(diff, 5)
        self.assertIn("[diff truncated, full diff available via git show HASH]", result)


class TestFunctionBoundaries(unittest.TestCase):
    """Test AST-based function boundary detection."""

    def test_simple_function(self):
        with TempProject({"mod.py": "def foo():\n    return 1\n"}) as root:
            boundaries = mod.get_function_boundaries(root / "mod.py")
            self.assertEqual(len(boundaries), 1)
            self.assertEqual(boundaries[0]["name"], "foo")
            self.assertEqual(boundaries[0]["line_start"], 1)

    def test_multiple_functions(self):
        source = "def foo():\n    pass\n\ndef bar():\n    pass\n"
        with TempProject({"mod.py": source}) as root:
            boundaries = mod.get_function_boundaries(root / "mod.py")
            self.assertEqual(len(boundaries), 2)
            names = [b["name"] for b in boundaries]
            self.assertIn("foo", names)
            self.assertIn("bar", names)

    def test_class_methods(self):
        source = "class C:\n    def method(self):\n        pass\n"
        with TempProject({"mod.py": source}) as root:
            boundaries = mod.get_function_boundaries(root / "mod.py")
            self.assertEqual(len(boundaries), 1)
            self.assertEqual(boundaries[0]["name"], "method")

    def test_syntax_error_returns_empty(self):
        with TempProject({"bad.py": "def foo(\n"}) as root:
            boundaries = mod.get_function_boundaries(root / "bad.py")
            self.assertEqual(boundaries, [])


class TestCoChangeDetection(unittest.TestCase):
    """Test co-change cluster detection."""

    def test_frequent_co_changes_detected(self):
        commits = [
            {"files": ["a.py", "b.py"], "type": "fix", "message": ""},
            {"files": ["a.py", "b.py"], "type": "fix", "message": ""},
            {"files": ["a.py", "b.py"], "type": "fix", "message": ""},
        ]
        clusters = mod.compute_co_change_clusters(commits, min_co_changes=3)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0]["co_change_count"], 3)

    def test_below_threshold_filtered(self):
        commits = [
            {"files": ["a.py", "b.py"], "type": "fix", "message": ""},
            {"files": ["a.py", "b.py"], "type": "fix", "message": ""},
        ]
        clusters = mod.compute_co_change_clusters(commits, min_co_changes=3)
        self.assertEqual(len(clusters), 0)

    def test_commit_counts_included(self):
        commits = [
            {"files": ["a.py", "b.py"], "type": "fix", "message": ""},
            {"files": ["a.py", "b.py"], "type": "fix", "message": ""},
            {"files": ["a.py", "b.py"], "type": "fix", "message": ""},
            {"files": ["a.py", "c.py"], "type": "fix", "message": ""},
        ]
        clusters = mod.compute_co_change_clusters(commits, min_co_changes=3)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0]["total_commits_a"], 4)  # a.py in all 4
        self.assertEqual(clusters[0]["total_commits_b"], 3)  # b.py in 3

    def test_max_pairs_cap(self):
        # Create many co-changing pairs.
        commits = []
        for i in range(5):
            commits.append(
                {"files": [f"f{j}.py" for j in range(10)], "type": "fix", "message": ""}
            )
        clusters = mod.compute_co_change_clusters(
            commits, min_co_changes=3, max_pairs=5
        )
        self.assertLessEqual(len(clusters), 5)

    def test_sorted_by_count(self):
        commits = [
            {"files": ["a.py", "b.py"], "type": "fix", "message": ""},
            {"files": ["a.py", "b.py"], "type": "fix", "message": ""},
            {"files": ["a.py", "b.py"], "type": "fix", "message": ""},
            {"files": ["c.py", "d.py"], "type": "fix", "message": ""},
            {"files": ["c.py", "d.py"], "type": "fix", "message": ""},
            {"files": ["c.py", "d.py"], "type": "fix", "message": ""},
            {"files": ["c.py", "d.py"], "type": "fix", "message": ""},
        ]
        clusters = mod.compute_co_change_clusters(commits, min_co_changes=3)
        self.assertEqual(len(clusters), 2)
        self.assertGreaterEqual(
            clusters[0]["co_change_count"], clusters[1]["co_change_count"]
        )


class TestGitIntegration(unittest.TestCase):
    """Integration tests using a real git repo."""

    def test_not_a_git_repo(self):
        with TempProject({"mod.py": "x = 1\n"}) as root:
            result = mod.analyze(["--last", "5", str(root)])
            self.assertIn("error", result)
            self.assertIn("Not a git repository", result["error"])

    def test_empty_repo_no_commits(self):
        with GitTempProject(
            [
                {"files": {"mod.py": "x = 1\n"}, "message": "init"},
            ]
        ) as root:
            # Analyze with a time range that excludes the commit.
            result = mod.analyze(
                [
                    str(root),
                    "--since",
                    "2030-01-01",
                    "--until",
                    "2030-02-01",
                ]
            )
            self.assertNotIn("error", result)
            self.assertEqual(result["summary"]["total_commits"], 0)
            self.assertEqual(result["file_churn"], [])

    def test_file_churn_basic(self):
        with GitTempProject(
            [
                {
                    "files": {"a.py": "x = 1\n"},
                    "message": "Add a",
                    "date": "2025-01-01T12:00:00+00:00",
                },
                {
                    "files": {"a.py": "x = 2\n"},
                    "message": "Fix a",
                    "date": "2025-01-02T12:00:00+00:00",
                },
                {
                    "files": {"b.py": "y = 1\n"},
                    "message": "Add b",
                    "date": "2025-01-03T12:00:00+00:00",
                },
            ]
        ) as root:
            result = mod.analyze(
                [
                    str(root),
                    "--since",
                    "2024-12-01",
                    "--until",
                    "2025-02-01",
                ]
            )
            self.assertNotIn("error", result)
            self.assertEqual(result["summary"]["total_commits"], 3)
            # a.py should have 2 commits, b.py should have 1.
            churn_map = {f["file"]: f for f in result["file_churn"]}
            self.assertIn("a.py", churn_map)
            self.assertEqual(churn_map["a.py"]["commits"], 2)
            self.assertIn("b.py", churn_map)
            self.assertEqual(churn_map["b.py"]["commits"], 1)

    def test_commit_classification_in_analysis(self):
        with GitTempProject(
            [
                {
                    "files": {"a.py": "x = 1\n"},
                    "message": "Fix crash in parser",
                    "date": "2025-01-01T12:00:00+00:00",
                },
                {
                    "files": {"a.py": "x = 2\n"},
                    "message": "Add new feature",
                    "date": "2025-01-02T12:00:00+00:00",
                },
            ]
        ) as root:
            result = mod.analyze(
                [
                    str(root),
                    "--since",
                    "2024-12-01",
                    "--until",
                    "2025-02-01",
                ]
            )
            by_type = result["summary"]["commits_by_type"]
            self.assertEqual(by_type.get("fix", 0), 1)
            self.assertEqual(by_type.get("feature", 0), 1)

    def test_recent_fixes_populated(self):
        with GitTempProject(
            [
                {
                    "files": {"a.py": "x = 1\n"},
                    "message": "Fix null check",
                    "date": "2025-01-01T12:00:00+00:00",
                },
            ]
        ) as root:
            result = mod.analyze(
                [
                    str(root),
                    "--since",
                    "2024-12-01",
                    "--until",
                    "2025-02-01",
                ]
            )
            self.assertEqual(len(result["recent_fixes"]), 1)
            self.assertEqual(result["recent_fixes"][0]["message"], "Fix null check")

    def test_no_function_flag(self):
        with GitTempProject(
            [
                {
                    "files": {"a.py": "def foo():\n    return 1\n"},
                    "message": "init",
                    "date": "2025-01-01T12:00:00+00:00",
                },
            ]
        ) as root:
            result = mod.analyze(
                [
                    str(root),
                    "--since",
                    "2024-12-01",
                    "--until",
                    "2025-02-01",
                    "--no-function",
                ]
            )
            self.assertEqual(result["function_churn"], [])
            self.assertIn("function_churn_note", result)

    def test_last_n_commits(self):
        with GitTempProject(
            [
                {
                    "files": {"a.py": f"x = {i}\n"},
                    "message": f"Commit {i}",
                    "date": f"2025-01-{i + 1:02d}T12:00:00+00:00",
                }
                for i in range(10)
            ]
        ) as root:
            result = mod.analyze([str(root), "--last", "3"])
            # Should process exactly 3 most recent commits.
            self.assertEqual(result["summary"]["total_commits"], 3)

    def test_max_commits_cap(self):
        with GitTempProject(
            [
                {
                    "files": {"a.py": f"x = {i}\n"},
                    "message": f"Commit {i}",
                    "date": f"2025-01-{i + 1:02d}T12:00:00+00:00",
                }
                for i in range(10)
            ]
        ) as root:
            result = mod.analyze(
                [
                    str(root),
                    "--since",
                    "2024-12-01",
                    "--until",
                    "2025-02-01",
                    "--max-commits",
                    "5",
                ]
            )
            self.assertLessEqual(result["summary"]["total_commits"], 5)
            self.assertTrue(result["time_range"]["commit_cap_applied"])

    def test_author_count(self):
        with GitTempProject(
            [
                {
                    "files": {"a.py": "x = 1\n"},
                    "message": "Commit 1",
                    "author": "Alice",
                    "date": "2025-01-01T12:00:00+00:00",
                },
                {
                    "files": {"a.py": "x = 2\n"},
                    "message": "Commit 2",
                    "author": "Bob",
                    "date": "2025-01-02T12:00:00+00:00",
                },
            ]
        ) as root:
            result = mod.analyze(
                [
                    str(root),
                    "--since",
                    "2024-12-01",
                    "--until",
                    "2025-02-01",
                ]
            )
            self.assertEqual(result["summary"]["authors"], 2)

    def test_function_churn_detected(self):
        with GitTempProject(
            [
                {
                    "files": {"a.py": "def foo():\n    return 1\n"},
                    "message": "Add foo",
                    "date": "2025-01-01T12:00:00+00:00",
                },
                {
                    "files": {"a.py": "def foo():\n    return 2\n"},
                    "message": "Update foo",
                    "date": "2025-01-02T12:00:00+00:00",
                },
            ]
        ) as root:
            result = mod.analyze(
                [
                    str(root),
                    "--since",
                    "2024-12-01",
                    "--until",
                    "2025-02-01",
                ]
            )
            func_names = [f["function"] for f in result["function_churn"]]
            self.assertIn("foo", func_names)

    def test_churn_rate_calculation(self):
        with GitTempProject(
            [
                {
                    "files": {"a.py": "line1\nline2\nline3\nline4\nline5\n"},
                    "message": "Add a",
                    "date": "2025-01-01T12:00:00+00:00",
                },
                {
                    "files": {"a.py": "line1\nmodified\nline3\nline4\nline5\n"},
                    "message": "Fix a",
                    "date": "2025-01-02T12:00:00+00:00",
                },
            ]
        ) as root:
            result = mod.analyze(
                [
                    str(root),
                    "--since",
                    "2024-12-01",
                    "--until",
                    "2025-02-01",
                ]
            )
            churn_map = {f["file"]: f for f in result["file_churn"]}
            self.assertIn("a.py", churn_map)
            # Churn rate should be > 0.
            self.assertGreater(churn_map["a.py"]["churn_rate"], 0)


class TestParseGitLogStreaming(unittest.TestCase):
    """Test that parse_git_log works with a list of lines (iterable)."""

    def test_parse_from_list(self):
        lines = [
            "COMMIT:abc123|2025-01-01T12:00:00+00:00|Alice|Add feature\n",
            "3\t1\ta.py\n",
            "\n",
            "COMMIT:def456|2025-01-02T12:00:00+00:00|Bob|Fix bug\n",
            "1\t1\tb.py\n",
        ]
        commits, file_stats = mod.parse_git_log(lines, max_commits=2000)
        self.assertEqual(len(commits), 2)
        self.assertEqual(commits[0]["hash"], "abc123")
        self.assertEqual(commits[0]["author"], "Alice")
        self.assertEqual(commits[1]["hash"], "def456")
        # File stats should have a.py and b.py.
        stat_files = {s["file"] for s in file_stats}
        self.assertIn("a.py", stat_files)
        self.assertIn("b.py", stat_files)

    def test_parse_respects_max_commits(self):
        lines = [
            f"COMMIT:hash{i}|2025-01-{i + 1:02d}T12:00:00+00:00|Author|Commit {i}\n"
            for i in range(10)
        ]
        commits, _ = mod.parse_git_log(lines, max_commits=3)
        self.assertLessEqual(len(commits), 3)


class TestMaxFilesArg(unittest.TestCase):
    """Test --max-files argument parsing."""

    def test_max_files_default(self):
        args = mod.parse_args([])
        self.assertEqual(args["max_files"], 0)

    def test_max_files_set(self):
        args = mod.parse_args(["--max-files", "50"])
        self.assertEqual(args["max_files"], 50)

    def _files_repo(self):
        return GitTempProject(
            [
                {
                    "files": {f"m{i}.py": f"def f{i}():\n    return {i}\n"},
                    "message": f"Add m{i}",
                }
                for i in range(3)
            ]
        )

    def test_cap_is_reported_in_the_envelope(self):
        with self._files_repo() as root:
            result = mod.analyze([str(root), *_WINDOW, "--max-files", "1"])
        self.assertEqual((result["files_total"], result["files_analyzed"]), (3, 1))
        self.assertTrue(result["files_capped"])
        self.assertTrue(any(n.startswith("FILES CAPPED") for n in result["notes"]))

    def test_no_cap_is_not_capped(self):
        with self._files_repo() as root:
            result = mod.analyze([str(root), *_WINDOW])
        self.assertEqual((result["files_total"], result["files_analyzed"]), (3, 3))
        self.assertFalse(result["files_capped"])

    def test_skipped_function_churn_counts_nothing(self):
        with self._files_repo() as root:
            result = mod.analyze([str(root), *_WINDOW, "--no-function"])
        self.assertIsNone(result["files_total"])
        self.assertFalse(result["files_capped"])

    def test_help_documents_max_files(self):
        self.assertIn("--max-files", mod.__doc__)


def _latin1_repo(testcase) -> Path:
    """A one-commit git repo whose diff payload is not valid UTF-8."""
    import shutil
    import tempfile

    tmpdir = tempfile.mkdtemp(prefix="crt_latin1_")
    testcase.addCleanup(shutil.rmtree, tmpdir, True)
    root = Path(tmpdir)
    # git never transcodes diff payload, so latin-1 bytes in file *content* are
    # what actually reach the decoder.
    (root / "mod.py").write_bytes(
        b"# Alejandro L\xf3pez-Ortiz\ndef f():\n    return 1\n"
    )
    for args in (
        ["init", "-q"],
        ["config", "user.name", "Test Author"],
        ["config", "user.email", "test@example.invalid"],
        ["config", "commit.gpgsign", "false"],
        ["add", "-A"],
        ["commit", "-q", "-m", "gh-1: Fix segfault in listsort"],
    ):
        subprocess.run(
            ["git", *args],
            cwd=tmpdir,
            check=True,
            capture_output=True,
            text=True,
            errors="replace",
        )
    return root


class TestNonUtf8History(unittest.TestCase):
    """A non-UTF-8 byte anywhere in a diff must not abort the analysis.

    Both git subprocess sites pass errors="replace". Without it, one pre-UTF-8
    commit raises UnicodeDecodeError and takes down the whole run — observed on
    CPython, where a single 2015 commit killed a 9,203-commit analysis.
    """

    def test_strict_decoding_would_have_failed(self):
        # Guard the guard: if this stops raising, the fixture no longer
        # exercises the bug and the tests below prove nothing.
        root = _latin1_repo(self)
        with self.assertRaises(UnicodeDecodeError):
            subprocess.run(
                ["git", "show", "--format=", "--patch", "HEAD"],
                cwd=str(root),
                capture_output=True,
                text=True,
                check=False,
            )

    def test_run_git_survives_non_utf8_diff(self):
        root = _latin1_repo(self)
        result = mod._run_git(["show", "--format=", "--patch", "HEAD"], root)
        self.assertEqual(result.returncode, 0)
        self.assertIn("Alejandro", result.stdout)

    def test_run_git_streaming_survives_non_utf8_diff(self):
        root = _latin1_repo(self)
        proc = mod._run_git_streaming(["log", "--patch"], root)
        try:
            text = "".join(proc.stdout)
        finally:
            proc.wait(timeout=10)
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()
        self.assertIn("Alejandro", text)


class TestUnknownArgumentsAndCommitCap(unittest.TestCase):
    """Silent truncation must be visible in the envelope, not only on stderr."""

    def test_unknown_flag_is_collected(self):
        args = mod.parse_args(["src/", "--months", "420"])
        self.assertIn("--months", args["unknown_args"])
        self.assertEqual(args["days"], 90)

    def test_known_flags_leave_unknown_args_empty(self):
        self.assertEqual(mod.parse_args(["src/", "--days", "30"])["unknown_args"], [])

    def test_unknown_flag_reaches_the_envelope(self):
        root = _latin1_repo(self)
        result = mod.analyze([str(root), "--months", "420", "--no-function"])
        self.assertIn("--months", result["unknown_args"])
        self.assertTrue(
            any("Unrecognized argument" in n for n in result["notes"]),
            result.get("notes"),
        )

    def test_commit_cap_is_noted(self):
        root = _latin1_repo(self)
        result = mod.analyze([str(root), "--max-commits", "1", "--no-function"])
        self.assertTrue(result["time_range"]["commit_cap_applied"])
        self.assertTrue(
            any("COMMIT CAP APPLIED" in n for n in result["notes"]),
            result.get("notes"),
        )


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


_WINDOW = ["--since", "2024-12-01", "--until", "2025-02-01"]


class TestTaggedAndKeyedClassification(unittest.TestCase):
    """Tags, Conventional Commits types and tracker keys are parsed,
    and keywords match on word boundaries."""

    def test_bracketed_fix_tag(self):
        detail = mod.classify_commit_detail(
            "[fix][cli] Accept -q as a short --quiet alias (K7)"
        )
        self.assertEqual(detail["type"], "fix")
        self.assertEqual(detail["source"], "tag")
        self.assertEqual(detail["tags"], ["fix", "cli"])
        self.assertEqual(detail["keys"], ["K7"])

    def test_fix_inside_a_word_is_not_a_fix(self):
        # "prefixes" contains "fix"; the substring classifier called it a fix.
        self.assertNotEqual(
            mod.classify_commit("[parser] Strip URL prefixes before matching (T1-30)"),
            "fix",
        )

    def test_ci_inside_a_word_is_not_a_chore(self):
        # "policies" contains "ci"; the substring classifier called it a chore.
        self.assertEqual(
            mod.classify_commit("[core] Add retry and backoff policies (T1-31)"),
            "feature",
        )

    def test_conventional_type_beats_keywords(self):
        # "correct" is a fix keyword, but the commit says it is a chore.
        detail = mod.classify_commit_detail("chore(docs): correct the install guide")
        self.assertEqual((detail["type"], detail["source"]), ("chore", "conventional"))
        self.assertEqual(mod.classify_commit("feat(api)!: drop v1 routes"), "feature")

    def test_area_tag_falls_through_to_the_subject(self):
        # [db] names an area, not a type; the subject's own verb decides.
        detail = mod.classify_commit_detail("[db] Add schema migrations (T1-32)")
        self.assertEqual((detail["type"], detail["source"]), ("feature", "verb"))

    def test_unmapped_key_stays_unknown(self):
        subject = "[db] Validate column types (T1-33)"
        self.assertEqual(mod.classify_commit(subject), "unknown")

    def test_mapped_key_decides(self):
        key_types = mod.parse_key_types("K=fix,T=feature")
        self.assertEqual(
            mod.classify_commit("[db] Validate column types (T1-33)", key_types),
            "feature",
        )
        detail = mod.classify_commit_detail(
            "[api] Keep query options on redirect (K12)", key_types
        )
        self.assertEqual((detail["type"], detail["source"]), ("fix", "key"))

    def test_tag_beats_key(self):
        # A [fix] commit on plan work is still a fix.
        self.assertEqual(
            mod.classify_commit(
                "[fix] Reject duplicate column names (T1-33)", {"T": "feature"}
            ),
            "fix",
        )

    def test_inflections_still_match(self):
        self.assertEqual(mod.classify_commit("Fixed a race in the pool"), "fix")
        self.assertEqual(mod.classify_commit("Fixes #12"), "fix")
        self.assertEqual(mod.classify_commit("Refactored the loader"), "refactor")

    def test_tracker_key_extraction(self):
        self.assertEqual(
            mod.classify_commit_detail("Close two leaks (K41, K42)")["keys"],
            ["K41", "K42"],
        )
        self.assertEqual(
            mod.classify_commit_detail("JIRA-123 and T2-8")["keys"],
            ["JIRA-123", "T2-8"],
        )
        # Words with digits that are not a key shape.
        self.assertEqual(
            mod.classify_commit_detail("Use SHA256 for UTF8 names")["keys"], []
        )

    def test_parse_key_types_rejects_bad_specs(self):
        for spec in ("B", "b=fix", "B=whatever"):
            with self.assertRaises(ValueError, msg=spec):
                mod.parse_key_types(spec)
        self.assertEqual(
            mod.parse_key_types("B=bug, P=feat"), {"B": "fix", "P": "feature"}
        )

    def test_key_type_flag(self):
        args = mod.parse_args(["--key-type", "B=fix", "--key-type", "P=feature"])
        self.assertEqual(args["key_types"], {"B": "fix", "P": "feature"})

    def test_repeated_keys_are_flagged(self):
        commits = [
            {"hash": "a" * 40, "type": "fix", "keys": ["K33"]},
            {"hash": "b" * 40, "type": "fix", "keys": ["K33", "K34"]},
            {"hash": "c" * 40, "type": "unknown", "keys": ["T1-50"]},
        ]
        summary = mod.summarize_tracker_keys(commits)
        self.assertEqual(summary["keys_seen"], 3)
        self.assertEqual([r["key"] for r in summary["repeated"]], ["K33"])
        self.assertEqual(summary["by_prefix"], {"K": {"fix": 3}, "T": {"unknown": 1}})

    def test_key_types_reach_the_analysis(self):
        with GitTempProject(
            [
                {"files": {"a.py": "x = 1\n"}, "message": "[db] Validate rows (T1-65)"},
                {
                    "files": {"a.py": "x = 2\n"},
                    "message": "[api] Keep query options (K24)",
                },
            ]
        ) as root:
            plain = mod.analyze([str(root), *_WINDOW])
            keyed = mod.analyze(
                [
                    str(root),
                    *_WINDOW,
                    "--key-type",
                    "K=fix,T=feature",
                ]
            )
        self.assertEqual(plain["summary"]["commits_by_type"], {"unknown": 2})
        self.assertTrue(any("--key-type" in n for n in plain["notes"]))
        self.assertEqual(keyed["summary"]["commits_by_type"], {"feature": 1, "fix": 1})
        self.assertEqual(keyed["summary"]["classified_by"], {"key": 2})

    def test_single_key_entry_overrides_its_prefix(self):
        types = mod.parse_key_types("T=feature,T4-12=fix")
        self.assertEqual(types, {"T": "feature", "T4-12": "fix"})
        self.assertEqual(
            mod.classify_commit("[db] Bound the queue (T4-12)", types), "fix"
        )
        self.assertEqual(
            mod.classify_commit("[db] Stream the export (T4-20)", types), "feature"
        )
        args = mod.parse_args(["--key-type", "T4-12=fix"])
        self.assertEqual(args["key_types"], {"T4-12": "fix"})

    def test_unknown_commit_inherits_its_keys_majority(self):
        commits = [
            {"type": "fix", "keys": ["T4-12"]},
            {"type": "fix", "keys": ["T4-12"]},
            {"type": "feature", "keys": ["T4-12"]},
            {"type": "unknown", "keys": ["T4-12"]},
            {"type": "unknown", "keys": ["T4-13"]},
        ]
        self.assertEqual(mod.inherit_key_majority(commits), 1)
        self.assertEqual(commits[3]["type"], "fix")
        self.assertEqual(commits[3]["type_source"], "key-majority")
        self.assertEqual(commits[4]["type"], "unknown")

    def test_no_majority_or_single_vote_inherits_nothing(self):
        tie = [
            {"type": "fix", "keys": ["K1"]},
            {"type": "feature", "keys": ["K1"]},
            {"type": "unknown", "keys": ["K1"]},
        ]
        lone = [
            {"type": "fix", "keys": ["K2"]},
            {"type": "unknown", "keys": ["K2"]},
        ]
        self.assertEqual(mod.inherit_key_majority(tie), 0)
        self.assertEqual(mod.inherit_key_majority(lone), 0)
        self.assertEqual(tie[2]["type"], "unknown")

    def test_key_majority_reaches_the_analysis(self):
        with GitTempProject(
            [
                {"files": {"a.py": "x = 1\n"}, "message": "[fix] Bound reads (K9)"},
                {"files": {"a.py": "x = 2\n"}, "message": "[fix] Bound writes (K9)"},
                {"files": {"a.py": "x = 3\n"}, "message": "[db] Queue limits (K9)"},
            ]
        ) as root:
            result = mod.analyze([str(root), *_WINDOW, "--no-function"])
        self.assertEqual(result["summary"]["commits_by_type"], {"fix": 3})
        self.assertEqual(
            result["summary"]["classified_by"], {"tag": 2, "key-majority": 1}
        )


class TestWindowWarnings(unittest.TestCase):
    """An empty window and rebased dates are said out loud."""

    def _commits(self, n, rebased):
        commits = []
        for i in range(n):
            commit = {
                "files": {"a.py": f"x = {i}\n"},
                "message": f"Change a {i}",
                "date": f"2025-01-{i + 1:02d}T12:00:00+00:00",
            }
            if i < rebased:
                commit["committer_date"] = "2025-01-20T12:00:00+00:00"
            commits.append(commit)
        return commits

    def test_empty_window_is_flagged_with_the_last_commit_date(self):
        with GitTempProject(self._commits(2, 0)) as root:
            result = mod.analyze(
                [str(root), "--since", "2025-06-01", "--until", "2025-07-01"]
            )
        self.assertEqual(result["summary"]["total_commits"], 0)
        self.assertTrue(result["time_range"]["empty"])
        self.assertTrue(
            result["time_range"]["last_commit_in_scope"].startswith("2025-01-02")
        )
        self.assertTrue(any(n.startswith("EMPTY WINDOW") for n in result["notes"]))

    def test_non_empty_window_has_no_empty_note(self):
        with GitTempProject(self._commits(2, 0)) as root:
            result = mod.analyze([str(root), *_WINDOW, "--no-function"])
        self.assertNotIn("empty", result["time_range"])
        self.assertFalse(any("EMPTY WINDOW" in n for n in result["notes"]))

    def test_rebased_history_is_flagged(self):
        with GitTempProject(self._commits(8, 5)) as root:
            result = mod.analyze([str(root), *_WINDOW, "--no-function"])
        self.assertEqual(result["summary"]["author_committer_date_divergent"], 5)
        self.assertTrue(any("author and committer" in n for n in result["notes"]))

    def test_a_few_rebased_commits_are_counted_but_not_flagged(self):
        with GitTempProject(self._commits(8, 1)) as root:
            result = mod.analyze([str(root), *_WINDOW, "--no-function"])
        self.assertEqual(result["summary"]["author_committer_date_divergent"], 1)
        self.assertFalse(any("author and committer" in n for n in result["notes"]))


class TestCommitterDateWindow(unittest.TestCase):
    """The window selects on the committer date, so the reported
    first/last dates must be committer dates too."""

    def test_first_commit_in_range_is_inside_the_window(self):
        with GitTempProject(
            [
                {
                    "files": {"a.py": "x = 1\n"},
                    "message": "Add a",
                    # Authored before the window, committed inside it (a rebase).
                    "date": "2024-11-01T12:00:00+00:00",
                    "committer_date": "2025-01-10T12:00:00+00:00",
                },
            ]
        ) as root:
            result = mod.analyze([str(root), *_WINDOW])
        churn = {f["file"]: f for f in result["file_churn"]}["a.py"]
        self.assertTrue(churn["first_commit_in_range"].startswith("2025-01-10"))
        self.assertTrue(churn["last_modified"].startswith("2025-01-10"))
        self.assertEqual(result["time_range"]["date_basis"], "committer")
        feature = result["recent_features"][0]
        self.assertTrue(feature["date"].startswith("2024-11-01"))
        self.assertTrue(feature["committer_date"].startswith("2025-01-10"))

    def test_old_four_field_header_still_parses(self):
        commits, _ = mod.parse_git_log(
            ["COMMIT:abc|2025-01-01T12:00:00+00:00|Alice|Add a | b\n"],
            10,
        )
        self.assertEqual(commits[0]["message"], "Add a | b")
        self.assertEqual(commits[0]["committer_date"], "2025-01-01T12:00:00+00:00")


# Three versions of one file. v2 changes b(); v3 prepends three lines, so v2's
# hunk (post-image line 5) sits inside a() at HEAD.
_V1 = "def a():\n    return 1\n\ndef b():\n    return 2\n"
_V2 = "def a():\n    return 1\n\ndef b():\n    return 3\n"
_V3 = "def z():\n    return 0\n\n" + _V2
_GROWING = [
    {"files": {"m.py": _V1}, "message": "Add a and b"},
    {"files": {"m.py": _V2}, "message": "Fix b"},
    {"files": {"m.py": _V3}, "message": "Add z"},
]


class TestFunctionAttributionAtCommit(unittest.TestCase):
    """Hunks are attributed to the function that held those
    lines AT THAT COMMIT, keyed by qualified name; lines_changed counts lines."""

    def _churn(self, commits):
        with GitTempProject(commits) as root:
            result = mod.analyze([str(root), *_WINDOW])
        churn = {(f["file"], f["function"]): f for f in result["function_churn"]}
        return result, churn

    def test_old_hunk_is_not_credited_to_whatever_sits_there_now(self):
        result, churn = self._churn(_GROWING)
        # HEAD-line mapping credited "Fix b" to a().
        self.assertEqual(churn[("m.py", "a")]["commits"], 1)
        self.assertEqual(churn[("m.py", "b")]["commits"], 2)
        self.assertEqual(churn[("m.py", "z")]["commits"], 1)
        fix = result["recent_fixes"][0]
        self.assertEqual(fix["functions_modified"], [{"function": "b", "file": "m.py"}])
        self.assertTrue(fix["functions_modified_complete"])

    def test_lines_changed_counts_lines_not_length(self):
        _, churn = self._churn(_GROWING)
        b = churn[("m.py", "b")]
        # Created (2 lines added), then one line replaced (+1 -1).
        self.assertEqual((b["lines_added"], b["lines_removed"]), (3, 1))
        self.assertEqual(b["lines_changed"], 4)
        self.assertEqual(b["current_length"], 2)
        self.assertEqual((b["line_start"], b["line_end"]), (7, 8))

    def test_pure_deletion_is_attributed(self):
        before = "def f():\n    x = 1\n    y = 2\n    return x\n"
        after = "def f():\n    x = 1\n    return x\n"
        _, churn = self._churn(
            [
                {"files": {"m.py": before}, "message": "Add f"},
                {"files": {"m.py": after}, "message": "Drop y"},
            ]
        )
        f = churn[("m.py", "f")]
        self.assertEqual(f["commits"], 2)
        self.assertEqual(f["lines_removed"], 1)

    def test_nested_handler_is_credited_not_its_factory(self):
        v1 = (
            "def build_router():\n"
            "    def handler():\n"
            "        return 1\n"
            "    return handler\n"
        )
        v2 = v1.replace("return 1", "return 2")
        _, churn = self._churn(
            [
                {"files": {"r.py": v1}, "message": "Add router"},
                {"files": {"r.py": v2}, "message": "Change handler"},
            ]
        )
        self.assertEqual(churn[("r.py", "build_router.handler")]["commits"], 2)
        self.assertEqual(churn[("r.py", "build_router")]["commits"], 1)

    def test_same_method_name_in_two_classes_is_two_entries(self):
        v1 = (
            "class A:\n    def run(self):\n        return 1\n\n"
            "class B:\n    def run(self):\n        return 1\n"
        )
        v2 = v1[: v1.rindex("return 1")] + "return 2\n"
        _, churn = self._churn(
            [
                {"files": {"m.py": v1}, "message": "Add A and B"},
                {"files": {"m.py": v2}, "message": "Change B.run"},
            ]
        )
        self.assertEqual(churn[("m.py", "A.run")]["commits"], 1)
        self.assertEqual(churn[("m.py", "B.run")]["commits"], 2)

    def test_deleted_function_is_reported_as_gone(self):
        _, churn = self._churn(
            [
                {"files": {"m.py": _V1}, "message": "Add a and b"},
                {"files": {"m.py": "def a():\n    return 1\n"}, "message": "Remove b"},
            ]
        )
        b = churn[("m.py", "b")]
        self.assertFalse(b["exists_at_head"])
        self.assertIsNone(b["line_start"])
        self.assertTrue(churn[("m.py", "a")]["exists_at_head"])

    def test_unparseable_version_is_counted_not_hidden(self):
        result, _ = self._churn(
            [
                {"files": {"m.py": _V1}, "message": "Add a and b"},
                {"files": {"m.py": "def a(:\n"}, "message": "Break it"},
            ]
        )
        meta = result["function_attribution"]
        self.assertEqual(meta["file_versions_unparseable"], 1)
        self.assertEqual(meta["files_unattributed"], 1)
        self.assertTrue(any("INCOMPLETE" in n for n in result["notes"]))

    def test_parse_cap_leaves_commits_unattributed(self):
        old_cap = mod._MAX_BLOB_PARSES
        mod._MAX_BLOB_PARSES = 1
        try:
            result, _ = self._churn(_GROWING)
        finally:
            mod._MAX_BLOB_PARSES = old_cap
        meta = result["function_attribution"]
        self.assertTrue(meta["parse_cap_reached"])
        self.assertGreater(meta["commits_unattributed"] + meta["files_unattributed"], 0)
        self.assertTrue(any("INCOMPLETE" in n for n in result["notes"]))

    def test_diff_parser_reads_blobs_and_hunks(self):
        diff = [
            "diff --git a/m.py b/m.py",
            "index " + "1" * 40 + ".." + "2" * 40 + " 100644",
            "--- a/m.py",
            "+++ b/m.py",
            "@@ -3,0 +4,2 @@ def f():",
            "+    x = 1",
            "+++ b/not-a-header",
            "@@ -9 +10 @@",
        ]
        (entry,) = mod.parse_unified_diff(diff)
        self.assertEqual(entry["path"], "m.py")
        self.assertEqual(entry["old_blob"], "1" * 40)
        self.assertEqual(entry["hunks"], [(3, 0, 4, 2), (9, 1, 10, 1)])

    def test_qualified_boundaries(self):
        source = (
            "class C:\n    def m(self):\n        def inner():\n            pass\n"
            "if True:\n    def guarded():\n        pass\n"
        )
        with TempProject({"mod.py": source}) as root:
            quals = [
                b["qualname"] for b in mod.get_function_boundaries(root / "mod.py")
            ]
        self.assertEqual(sorted(quals), ["C.m", "C.m.inner", "guarded"])


class TestUnmergedBranches(unittest.TestCase):
    """Commits on remote branches that the default branch lacks."""

    def _repo(self) -> Path:
        project = GitTempProject(
            [
                {"files": {"src/a.py": "x = 1\n"}, "message": "Add a"},
                {"files": {"src/a.py": "x = 2\n"}, "message": "Change a"},
            ]
        )
        root = project.__enter__()
        self.addCleanup(project.__exit__, None, None, None)
        main_sha = _git(root, "rev-parse", "HEAD")
        _git(root, "checkout", "-q", "-b", "work")
        (root / "src" / "a.py").write_text("x = 3\n", encoding="utf-8")
        _git(root, "commit", "-qam", "[fix] Guard a (K7)")
        (root / "docs.md").write_text("notes\n", encoding="utf-8")
        _git(root, "add", "docs.md")
        _git(root, "commit", "-qm", "[doc] Notes")
        work_sha = _git(root, "rev-parse", "HEAD")
        _git(root, "checkout", "-q", "main")
        merged_sha = _git(root, "rev-parse", "HEAD~1")
        _git(root, "update-ref", "refs/remotes/origin/main", main_sha)
        _git(root, "update-ref", "refs/remotes/origin/work", work_sha)
        _git(root, "update-ref", "refs/remotes/origin/merged", merged_sha)
        _git(
            root, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"
        )
        return root

    def test_unmerged_branch_is_listed_with_its_commits(self):
        root = self._repo()
        result = mod.analyze([str(root), *_WINDOW, "--no-function"])
        unmerged = result["unmerged_branches"]
        self.assertTrue(unmerged["available"])
        self.assertEqual(unmerged["base"], "origin/main")
        self.assertEqual([b["branch"] for b in unmerged["branches"]], ["origin/work"])
        work = unmerged["branches"][0]
        self.assertEqual((work["ahead"], work["behind"]), (2, 0))
        self.assertEqual(
            [(c["message"], c["type"]) for c in work["commits"]],
            [("[doc] Notes", "docs"), ("[fix] Guard a (K7)", "fix")],
        )
        self.assertEqual(work["commits"][1]["keys"], ["K7"])
        self.assertEqual(work["fix_commits_shown"], 1)
        self.assertEqual(work["files_in_scope"], ["docs.md", "src/a.py"])

    def test_scope_limits_the_files_listed(self):
        root = self._repo()
        result = mod.analyze([str(root / "src"), *_WINDOW, "--no-function"])
        work = result["unmerged_branches"]["branches"][0]
        self.assertEqual(work["files_in_scope"], ["src/a.py"])
        # The ahead count is about the branch, not the scope.
        self.assertEqual(work["ahead"], 2)

    def test_merged_branch_and_symref_are_not_listed(self):
        root = self._repo()
        result = mod.analyze([str(root), *_WINDOW, "--no-function"])
        unmerged = result["unmerged_branches"]
        names = {b["branch"] for b in unmerged["branches"]}
        self.assertEqual(names, {"origin/work"})
        self.assertEqual(unmerged["remote_branches_total"], 2)  # work, merged

    def test_cherry_picked_commit_is_not_unmerged_work(self):
        root = self._repo()
        # Land the branch's fix on main as a cherry-pick: a new sha, same patch.
        fix_sha = _git(root, "rev-parse", "origin/work~1")
        _git(root, "cherry-pick", "-x", fix_sha)
        _git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
        result = mod.analyze([str(root), *_WINDOW, "--no-function"])
        work = result["unmerged_branches"]["branches"][0]
        self.assertEqual(work["ahead"], 2)
        self.assertEqual(work["ahead_patch_unique"], 1)
        self.assertEqual(work["ahead_patch_in_base"], 1)
        by_message = {c["message"]: c["patch_in_base"] for c in work["commits"]}
        self.assertEqual(by_message, {"[doc] Notes": False, "[fix] Guard a (K7)": True})
        self.assertEqual(work["fix_commits_shown"], 1)
        self.assertEqual(work["fix_commits_shown_unique"], 0)

    def test_unique_patches_are_all_counted(self):
        root = self._repo()
        result = mod.analyze([str(root), *_WINDOW, "--no-function"])
        work = result["unmerged_branches"]["branches"][0]
        self.assertEqual(work["ahead_patch_unique"], 2)
        self.assertEqual(work["fix_commits_shown_unique"], 1)

    def test_library_call_without_analyze_lists_branches(self):
        # A fresh module: analyze() has never started the script clock.
        fresh = import_script("analyze_history")
        root = self._repo()
        unmerged = fresh.find_unmerged_branches(root)
        self.assertTrue(unmerged["available"])
        self.assertEqual([b["branch"] for b in unmerged["branches"]], ["origin/work"])
        self.assertFalse(unmerged["truncated"])

    def test_no_branches_flag_skips(self):
        root = self._repo()
        result = mod.analyze([str(root), *_WINDOW, "--no-function", "--no-branches"])
        self.assertFalse(result["unmerged_branches"]["available"])
        self.assertIn("--no-branches", result["unmerged_branches"]["reason"])


if __name__ == "__main__":
    unittest.main()
