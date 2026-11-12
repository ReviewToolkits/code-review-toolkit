"""Scope flags shared across scripts: --exclude, migration layouts, --git-dir, caps.

The helpers live in scan_common (see test_scan_common.py); these tests check
that each script wires them in: excluded files are neither counted nor charged
to --max-files, migration revisions stop showing up as dead/untested/complex
code, history can come from a separate checkout, and hard-coded caps can be
raised.
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
from unittest.mock import MagicMock, patch

from helpers import TempProject, import_script

ALEMBIC_REVISION = (
    '"""add a column"""\n'
    "from alembic import op\n"
    "import sqlalchemy as sa\n\n"
    'revision = "a1b2c3"\n'
    'down_revision = "f0e1d2"\n\n\n'
    "def upgrade():\n"
    '    op.add_column("item", sa.Column("size", sa.Integer()))\n\n\n'
    "def downgrade():\n"
    '    op.drop_column("item", "size")\n'
)
ALEMBIC_ENV = (
    "from alembic import context\n\n\n"
    "def run_migrations_online():\n"
    "    context.run_migrations()\n"
)


def run_main(mod, argv: list[str]) -> tuple[dict | None, str, int]:
    """Run *mod*.main() with *argv*; return (parsed stdout JSON, stderr, exit code)."""
    out, err = io.StringIO(), io.StringIO()
    old_argv = sys.argv
    sys.argv = [f"{mod.__name__}.py", *argv]
    code = 0
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                mod.main()
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
    finally:
        sys.argv = old_argv
    text = out.getvalue()
    return (json.loads(text) if text.strip() else None), err.getvalue(), code


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


MIGRATION_PROJECT = {
    "pkg/__init__.py": "",
    "pkg/core.py": "def work(x):\n    return x + 1\n",
    "pkg/migrations/env.py": ALEMBIC_ENV,
    "pkg/migrations/versions/a1b2c3_add.py": ALEMBIC_REVISION,
    "pkg/migrations/versions/f0e1d2_init.py": ALEMBIC_REVISION.replace(
        '"a1b2c3"', '"f0e1d2"'
    ).replace('"f0e1d2"\n\n', "None\n\n"),
    "tests/test_core.py": (
        "from pkg.core import work\n\n\ndef test_work():\n    assert work(1) == 2\n"
    ),
}


EXCLUDE_FILES = {
    "pkg/__init__.py": "",
    "pkg/core.py": "# TODO: tidy\ndef run(a: int) -> int:\n    return a\n",
    "pkg/vendor/lib.py": "# TODO: upstream\ndef v(a, b):\n    return a\n",
}


def check_exclude(tc: unittest.TestCase, name: str, extra: tuple[str, ...] = ()):
    """Run script *name* on EXCLUDE_FILES with --exclude vendor/*; check the envelope."""
    mod = import_script(name)
    with TempProject(EXCLUDE_FILES) as root:
        out, err, code = run_main(
            mod, [str(root / "pkg"), "--exclude", "vendor/*", *extra]
        )
    tc.assertEqual(code, 0, err)
    tc.assertNotIn("unknown flag", err)
    tc.assertEqual(out["files_excluded"], 1, name)
    tc.assertEqual(out["exclude_matches"], {"vendor/*": 1}, name)
    tc.assertEqual(out["files_total"], 2, name)
    return out


def assert_no_unknown_flag_warning(tc, name: str, flags: list[str]) -> None:
    with TempProject({"m.py": "x = 1\n"}) as root:
        out, err, code = run_main(import_script(name), [str(root), *flags])
    tc.assertEqual(code, 0, err)
    tc.assertNotIn("unknown flag", err)
    tc.assertIsNotNone(out)


def _history_repo(root: Path) -> None:
    """A repo with one fix commit that adds a test."""
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "Add core")
    test = root / "tests/test_core.py"
    test.write_text(
        test.read_text(encoding="utf-8")
        + "\n\ndef test_regression():\n    assert work(2) == 3\n",
        encoding="utf-8",
    )
    core = root / "pkg/core.py"
    core.write_text(
        "def work(x):\n    if x is None:\n        return 0\n    return x + 1\n",
        encoding="utf-8",
    )
    _git(root, "commit", "-q", "-am", "Fix the off-by-one in work")


class _ExportAndCheckout:
    """An export (no .git) beside the checkout it came from."""

    FILES: ClassVar[dict[str, str]] = {
        "pkg/__init__.py": "",
        "pkg/core.py": "def work(x):\n    return x + 1\n",
        "tests/test_core.py": (
            "from pkg.core import work\n\n\ndef test_work():\n    assert work(1) == 2\n"
        ),
    }

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="crt_scope_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "checkout"
        self.export = self.tmp / "export"
        for rel, text in {**self.FILES, "pyproject.toml": "[project]\n"}.items():
            for base in (self.repo, self.export):
                (base / rel).parent.mkdir(parents=True, exist_ok=True)
                (base / rel).write_text(text, encoding="utf-8")
        _history_repo(self.repo)
        for rel in ("tests/test_core.py", "pkg/core.py"):
            shutil.copy(self.repo / rel, self.export / rel)
        # Keep git from finding any repository above the export.
        self.env = patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(self.tmp)})
        self.env.start()
        self.addCleanup(self.env.stop)


class TestScannersCountAfterExclusion(unittest.TestCase):
    """--exclude filters before counting and before the --max-files cap."""

    FILES: ClassVar[dict[str, str]] = {
        "src/a.py": "def f(x=[]):\n    x.append(1)\n",
        "generated/b.py": "def g(y=[]):\n    y.append(1)\n",
        "generated/c.py": "def h(z=[]):\n    z.append(1)\n",
        "generated/d.py": "def k(w=[]):\n    w.append(1)\n",
    }

    def test_pitfalls_counts_exclude_excluded_files(self):
        mod = import_script("scan_python_pitfalls")
        with TempProject(self.FILES) as root:
            result = mod.analyze(str(root), exclude=["generated/"])
        self.assertEqual(result["files_total"], 1)
        self.assertEqual(result["files_analyzed"], 1)
        self.assertEqual(result["files_excluded"], 3)
        self.assertEqual(result["exclude_matches"], {"generated/": 3})

    def test_pitfalls_excluded_files_do_not_use_the_cap(self):
        # generated/ sorts before src/: charged to the cap, it left src/ unscanned.
        mod = import_script("scan_python_pitfalls")
        with TempProject(self.FILES) as root:
            result = mod.analyze(str(root), max_files=1, exclude=["generated/*"])
        self.assertEqual(result["summary"]["total_findings"], 1)
        self.assertEqual(result["findings"][0]["file"], "src/a.py")
        self.assertFalse(result["files_capped"])

    def test_concurrency_counts_exclude_excluded_files(self):
        mod = import_script("scan_concurrency")
        with TempProject(self.FILES) as root:
            result = mod.analyze(str(root), exclude=["generated"])
        self.assertEqual((result["files_total"], result["files_analyzed"]), (1, 1))
        self.assertEqual(result["files_excluded"], 3)
        self.assertEqual(result["summary"]["excluded_patterns"], ["generated"])

    def test_pattern_matching_nothing_shows_zero(self):
        mod = import_script("scan_python_pitfalls")
        with TempProject(self.FILES) as root:
            result = mod.analyze(str(root), exclude=["generated/", "vendored/"])
        self.assertEqual(result["exclude_matches"], {"generated/": 3, "vendored/": 0})

    def test_cli_exclude_is_repeatable_and_comma_separated(self):
        mod = import_script("scan_python_pitfalls")
        with TempProject(self.FILES) as root:
            out, err, code = run_main(
                mod,
                [str(root), "--exclude", "generated/b.py,generated/c.py"]
                + ["--exclude=generated/d.py"],
            )
        self.assertEqual(code, 0)
        self.assertEqual(out["files_excluded"], 3)
        self.assertEqual(err, "")

    def test_concurrency_cli_keeps_its_own_flags(self):
        mod = import_script("scan_concurrency")
        with TempProject(self.FILES) as root:
            out, err, code = run_main(
                mod, [str(root), "--all-blocking", "--blocking", "slow_io"]
            )
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(
            out["summary"]["blocking_low_signal"]["reported_as"], "findings"
        )


class TestHistoryFilters(unittest.TestCase):
    """analyze_history: --exclude, --exclude-author and generated files."""

    def setUp(self):
        self.mod = import_script("analyze_history")

    def _repo(self, root: Path) -> None:
        _git(root, "init", "-q", "-b", "main")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "Add core")
        for i in range(2):
            (root / "locale/messages.pot").write_text(f"msgid {i}\n", encoding="utf-8")
            (root / "locale/fr.po").write_text(f"msgstr {i}\n", encoding="utf-8")
            _git(root, "add", "-A")
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Catalog Bot",
                    "-c",
                    "user.email=b@example.com",
                    "commit",
                    "-q",
                    "-m",
                    "Update translations",
                ],
                cwd=root,
                check=True,
                capture_output=True,
            )
        (root / "pkg/core.py").write_text(
            "def work():\n    return 2\n", encoding="utf-8"
        )
        (root / "pkg/vendor.py").write_text("V = 2\n", encoding="utf-8")
        _git(root, "commit", "-q", "-am", "Fix work")

    FILES: ClassVar[dict[str, str]] = {
        "pkg/__init__.py": "",
        "pkg/core.py": "def work():\n    return 1\n",
        "pkg/vendor.py": "V = 1\n",
        "locale/messages.pot": "",
        "locale/fr.po": "",
    }

    def _run(self, root: Path, *flags: str) -> dict:
        return self.mod.analyze([str(root), "--no-branches", "--days", "30", *flags])

    def test_generated_catalogs_are_left_out_by_default(self):
        with TempProject(self.FILES) as root:
            self._repo(root)
            result = self._run(root)
            kept = self._run(root, "--include-generated")
        files = {f["file"] for f in result["file_churn"]}
        self.assertFalse({"locale/fr.po", "locale/messages.pot"} & files)
        # The two catalog-only commits are dropped, not left as empty commits.
        self.assertEqual(result["summary"]["total_commits"], 2)
        self.assertEqual(result["history_filters"]["commits_dropped"], 2)
        self.assertEqual(result["history_filters"]["path_matches"]["*.po"], 3)
        self.assertIn("locale/fr.po", {f["file"] for f in kept["file_churn"]})
        self.assertEqual(kept["summary"]["total_commits"], 4)

    def test_exclude_path_and_author(self):
        with TempProject(self.FILES) as root:
            self._repo(root)
            result = self._run(
                root,
                "--include-generated",
                "--exclude",
                "pkg/vendor.py",
                "--exclude-author",
                "*bot*",
            )
        files = {f["file"] for f in result["file_churn"]}
        self.assertNotIn("pkg/vendor.py", files)
        self.assertIn("pkg/core.py", files)
        self.assertEqual(result["history_filters"]["commits_dropped_by_author"], 2)
        self.assertEqual(result["summary"]["total_commits"], 2)
        self.assertNotIn("unknown_args", result)

    def test_filter_is_a_no_op_without_patterns(self):
        lines = [
            "COMMIT:abc|2024-01-01T00:00:00+00:00|2024-01-01T00:00:00+00:00|A|m\n",
            "1\t0\tx.po\n",
        ]
        spec = {"exclude_patterns": [], "generated_patterns": [], "exclude_authors": []}
        self.assertEqual(list(self.mod.filter_log_lines(lines, spec, {})), lines)


class TestVultureCapKeepsTheMostConfident(unittest.TestCase):
    """--max-findings must cut the 60% tier, not the 100% hits printed last."""

    def test_sorted_by_confidence_before_the_cap(self):
        mod = import_script("run_external_tools")
        stdout = (
            "pkg/a.py:1: unused function 'one' (60% confidence)\n"
            "pkg/a.py:2: unused function 'two' (60% confidence)\n"
            "pkg/a.py:3: unused function 'three' (60% confidence)\n"
            "pkg/b.py:9: unused variable 'exc_tb' (100% confidence)\n"
            "pkg/a.py:7: unused import 'os' (90% confidence)\n"
        )
        with (
            TempProject({"pkg/a.py": "x = 1\n"}) as root,
            patch.object(mod.subprocess, "run") as run,
        ):
            run.return_value = MagicMock(stdout=stdout, stderr="")
            out = mod.run_vulture(
                mod.parse_args([str(root), "--max-findings", "2"]), root, root
            )
        self.assertEqual([f["name"] for f in out["findings"]], ["exc_tb", "os"])
        self.assertEqual(out["total_findings"], 5)
        self.assertTrue(out["findings_capped"])


class TestExcludeInAnalysisScripts(unittest.TestCase):
    """The import graph, debt and survey scripts accept --exclude."""

    def test_analyze_imports(self):
        out = check_exclude(self, "analyze_imports")
        self.assertNotIn("pkg/vendor/lib.py", [f["file"] for f in out["files"]])

    def test_collect_debt(self):
        out = check_exclude(self, "collect_debt")
        self.assertEqual(out["summary"]["total_markers"], 1)

    def test_surveys_and_exception_map(self):
        for name in (
            "survey_conventions",
            "survey_docstrings",
            "survey_error_handling",
            "map_exceptions",
        ):
            with self.subTest(name=name):
                check_exclude(self, name)


class TestUnknownFlagWarning(unittest.TestCase):
    """An unknown flag is reported on stderr; stdout stays valid JSON."""

    def test_unknown_flag_warns_without_corrupting_json(self):
        mod = import_script("analyze_imports")
        with TempProject({"m.py": "x = 1\n"}) as root:
            out, err, code = run_main(mod, [str(root), "--exclud", "m.py"])
        self.assertEqual(code, 0)
        self.assertIn("unknown flag(s) ignored: --exclud", err)
        self.assertIn("--exclude", err)  # the accepted flags are listed
        self.assertEqual(out["files_total"], 1)

    def test_scripts_own_flags_are_not_warned_about(self):
        for name, flags in (
            ("survey_conventions", ["--include-tests"]),
            ("survey_error_handling", ["--no-implicit"]),
            ("collect_debt", ["--key-regex", "[A-Z]+-[0-9]+"]),
            ("map_exceptions", ["--code-key", "code"]),
        ):
            with self.subTest(name=name):
                assert_no_unknown_flag_warning(self, name, flags)


class TestExcludeInMigrationAwareScripts(unittest.TestCase):
    """count_types, correlate_tests and find_dead_symbols accept --exclude."""

    def test_count_types(self):
        check_exclude(self, "count_types")

    def test_correlate_tests(self):
        out = check_exclude(self, "correlate_tests")
        self.assertNotIn("pkg/vendor/lib.py", out["untested_sources"])

    def test_find_dead_symbols(self):
        check_exclude(self, "find_dead_symbols")


class TestMigrationLayouts(unittest.TestCase):
    """Migration revisions are entry points, not dead or untested code."""

    def test_dead_symbols_treats_revisions_as_entry_points(self):
        mod = import_script("find_dead_symbols")
        with TempProject(MIGRATION_PROJECT) as root:
            out, _, _ = run_main(mod, [str(root / "pkg")])
        orphans = [o["file"] for o in out["orphan_files"]]
        self.assertFalse([o for o in orphans if "migrations" in o], orphans)
        names = {u["name"] for u in out["unreferenced_symbols"]}
        self.assertNotIn("upgrade", names)
        self.assertNotIn("downgrade", names)
        self.assertEqual(out["summary"]["migration_entry_points"], 3)

    def test_dead_symbols_still_reports_a_real_orphan(self):
        mod = import_script("find_dead_symbols")
        files = dict(MIGRATION_PROJECT, **{"pkg/stray.py": "def lonely():\n    pass\n"})
        with TempProject(files) as root:
            out, _, _ = run_main(mod, [str(root / "pkg")])
        self.assertIn("pkg/stray.py", [o["file"] for o in out["orphan_files"]])

    def test_dead_symbols_include_migrations_restores_old_behaviour(self):
        mod = import_script("find_dead_symbols")
        with TempProject(MIGRATION_PROJECT) as root:
            out, _, _ = run_main(mod, [str(root / "pkg"), "--include-migrations"])
        orphans = [o["file"] for o in out["orphan_files"]]
        self.assertIn("pkg/migrations/versions/a1b2c3_add.py", orphans)

    def test_correlate_tests_skips_revisions_by_default(self):
        mod = import_script("correlate_tests")
        with TempProject(MIGRATION_PROJECT) as root:
            result = mod.analyze(str(root))
            kept = mod.analyze(str(root), include_migrations=True)
        self.assertEqual(result["untested_sources"], ["pkg/__init__.py"])
        self.assertEqual(result["migration_files_skipped"], 3)
        self.assertEqual(
            result["migration_dirs"], ["pkg/migrations", "pkg/migrations/versions"]
        )
        self.assertEqual(kept["migration_files_skipped"], 0)
        self.assertIn("pkg/migrations/versions/a1b2c3_add.py", kept["untested_sources"])

    def test_count_types_skips_revisions(self):
        with TempProject(MIGRATION_PROJECT) as root:
            types = import_script("count_types").analyze(str(root / "pkg"))
            kept = import_script("count_types").analyze(
                str(root / "pkg"), include_migrations=True
            )
        self.assertEqual(types["migration_files_skipped"], 3)
        self.assertEqual(types["summary"]["total_functions"], 1)
        self.assertEqual(kept["summary"]["total_functions"], 6)


class TestCoverageOmit(unittest.TestCase):
    """correlate_tests honours the project's coverage `omit` for sources only."""

    FILES: ClassVar[dict[str, str]] = {
        "pkg/__init__.py": "",
        "pkg/core.py": "def work():\n    return 1\n",
        "pkg/shim.py": "def main():\n    pass\n",
        "tests/test_core.py": "from pkg.core import work\n\n\ndef test_w():\n    work()\n",
    }

    def test_omitted_source_is_not_counted_as_untested(self):
        mod = import_script("correlate_tests")
        with TempProject(self.FILES) as root:
            (root / ".coveragerc").write_text(
                "[run]\nomit =\n    pkg/shim.py\n    tests/*\n", encoding="utf-8"
            )
            result = mod.analyze(str(root))
            ignored = mod.analyze(str(root), coverage_omit=False)
        self.assertNotIn("pkg/shim.py", result["untested_sources"])
        self.assertEqual(result["coverage_omit"]["files"], ["pkg/shim.py"])
        self.assertEqual(result["coverage_omit"]["source"], ".coveragerc [run]")
        # `tests/*` in omit must not remove the tests themselves.
        self.assertEqual(result["summary"]["test_files"], 1)
        self.assertIn("pkg/shim.py", ignored["untested_sources"])

    def test_no_coverage_config_reports_null_source(self):
        mod = import_script("correlate_tests")
        with TempProject(self.FILES) as root:
            result = mod.analyze(str(root))
        self.assertIsNone(result["coverage_omit"]["source"])
        self.assertEqual(result["coverage_omit"]["source_files_omitted"], 0)
        self.assertIn("pkg/shim.py", result["untested_sources"])


class TestComplexityScope(_ExportAndCheckout, unittest.TestCase):
    """measure_complexity: --exclude, migrations, and history from a checkout."""

    def test_exclude(self):
        out = check_exclude(self, "measure_complexity", ("--no-clones",))
        self.assertNotIn("pkg/vendor/lib.py", [f["file"] for f in out["files"]])

    def test_own_flags_are_not_warned_about(self):
        assert_no_unknown_flag_warning(
            self, "measure_complexity", ["--no-clones", "--key-type", "B=fix"]
        )

    def test_skips_revisions(self):
        with TempProject(MIGRATION_PROJECT) as root:
            out, _, _ = run_main(
                import_script("measure_complexity"), [str(root / "pkg"), "--no-clones"]
            )
        self.assertEqual(out["migration_files_skipped"], 3)
        self.assertEqual(out["summary"]["total_functions"], 1)

    def test_warns_on_stderr_without_history(self):
        mod = import_script("measure_complexity")
        out, err, _ = run_main(mod, [str(self.export), "--no-clones"])
        self.assertFalse(out["history_crossing"]["history_available"])
        self.assertIn("no usable git history", err)
        self.assertIn("--git-dir", err)

    def test_git_dir_maps_paths_by_relative_path(self):
        mod = import_script("measure_complexity")
        out, err, _ = run_main(
            mod, [str(self.export), "--no-clones", "--git-dir", str(self.repo)]
        )
        self.assertNotIn("warning", err)
        top = out["history_crossing"]["top_fixed_functions"]
        self.assertTrue(top["available"])
        self.assertEqual(top["functions"][0]["qualified_name"], "pkg/core.py::work")
        self.assertEqual(
            out["history_crossing"]["history_root"], str(self.repo.resolve())
        )


class TestInvariantsScope(_ExportAndCheckout, unittest.TestCase):
    """extract_test_invariants: --exclude, and bug-fix history from a checkout."""

    def test_exclude(self):
        check_exclude(self, "extract_test_invariants")

    def test_own_flags_are_not_warned_about(self):
        assert_no_unknown_flag_warning(
            self, "extract_test_invariants", ["--max-tests", "5", "--all"]
        )

    def test_with_git_on_an_export_is_null_and_warned(self):
        mod = import_script("extract_test_invariants")
        out, err, code = run_main(mod, [str(self.export), "--with-git"])
        self.assertEqual(code, 0)
        self.assertIsNone(out["summary"]["bug_fix_tests_total"])
        self.assertIsNone(out["summary"]["bug_fix_tests_found"])
        self.assertEqual(out["bug_fix_scan"]["status"], "no_git_history")
        self.assertIn("--git-dir", err)

    def test_git_dir_reads_the_checkout(self):
        mod = import_script("extract_test_invariants")
        out, err, code = run_main(
            mod, [str(self.export), "--with-git", "--git-dir", str(self.repo / ".git")]
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(out["summary"]["bug_fix_tests_total"], 1)
        self.assertEqual(out["bug_fix_tests"][0]["test_functions"], ["test_regression"])
        self.assertEqual(out["bug_fix_scan"]["git_root"], str(self.repo.resolve()))


class TestInvariantCaps(unittest.TestCase):
    """The hard-coded selection and history caps can be raised."""

    def _project(self, n_tests: int) -> dict:
        tests = "".join(
            f"def test_case_{i}():\n    assert work({i}) == {i + 1}\n\n"
            for i in range(n_tests)
        )
        return {
            "pkg/__init__.py": "",
            "pkg/core.py": "def work(x):\n    return x + 1\n",
            "tests/test_core.py": "from pkg.core import work\n\n" + tests,
        }

    def test_max_tests_raises_the_selection(self):
        mod = import_script("extract_test_invariants")
        with TempProject(self._project(40)) as root:
            default = mod.analyze(str(root))
            raised = mod.analyze(str(root), max_tests=40)
        self.assertEqual(default["summary"]["selected_test_functions"], 30)
        self.assertTrue(default["summary"]["tests_capped"])
        self.assertEqual(raised["summary"]["selected_test_functions"], 40)
        self.assertFalse(raised["summary"]["tests_capped"])

    def test_commit_window_and_group_cap_flags(self):
        mod = import_script("extract_test_invariants")
        with TempProject(self._project(3)) as root:
            _git(root, "init", "-q", "-b", "main")
            _git(root, "add", "-A")
            _git(root, "commit", "-q", "-m", "Add core")
            test = root / "tests/test_core.py"
            for i in range(3):
                test.write_text(
                    test.read_text(encoding="utf-8")
                    + f"def test_fix_{i}():\n    assert work(0) == 1\n\n",
                    encoding="utf-8",
                )
                _git(root, "commit", "-q", "-am", f"Fix crash number {i}")
            one = mod.analyze(str(root), with_git=True, max_commits=1)
            capped = mod.analyze(str(root), with_git=True, max_bug_fix_groups=2)
            unbounded = mod.analyze(
                str(root), with_git=True, max_commits=None, max_fix_commits=None
            )
        self.assertEqual(one["summary"]["bug_fix_tests_total"], 1)
        self.assertEqual(one["bug_fix_scan"]["commits_scanned"], 1)
        self.assertEqual(capped["summary"]["bug_fix_tests_total"], 3)
        self.assertEqual(len(capped["bug_fix_tests"]), 2)
        self.assertTrue(capped["summary"]["bug_fix_tests_capped"])
        self.assertIsNone(unbounded["bug_fix_scan"]["commit_cap"])
        self.assertEqual(unbounded["bug_fix_scan"]["fix_commits_found"], 3)

    def test_tests_and_source_roots(self):
        mod = import_script("extract_test_invariants")
        files = {
            "lib/core.py": "def work(x):\n    return x + 1\n",
            "checks/check_core.py": (
                "from lib.core import work\n\n\ndef test_work():\n    assert work(1) == 2\n"
            ),
        }
        with TempProject(files) as root:
            result = mod.analyze(
                str(root),
                source_roots=[str(root / "lib")],
                test_roots=[str(root / "checks")],
            )
        self.assertEqual(result["test_discovery"]["method"], "--tests")
        self.assertEqual(result["summary"]["test_files"], 1)
        self.assertEqual(result["summary"]["source_files"], 1)
        self.assertEqual(result["summary"]["total_test_functions"], 1)


if __name__ == "__main__":
    unittest.main()
