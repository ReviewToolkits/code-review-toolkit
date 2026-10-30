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
            env["GIT_COMMITTER_DATE"] = date
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

    def test_area_tag_falls_through_to_keywords(self):
        # [store] names an area, not a type.
        detail = mod.classify_commit_detail("[db] Add schema migrations (T1-32)")
        self.assertEqual((detail["type"], detail["source"]), ("feature", "keyword"))

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


if __name__ == "__main__":
    unittest.main()
