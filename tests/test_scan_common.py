"""Tests for scan_common.py -- the shared utilities module."""

import ast
import contextlib
import io
import os
import subprocess
import sys
import unittest
from pathlib import Path
from typing import ClassVar

from helpers import TempProject, import_script

mod = import_script("scan_common")


class TestFindProjectRoot(unittest.TestCase):
    """Project-root detection walks upward looking for markers."""

    def test_finds_root_from_nested_file(self):
        with TempProject({"pkg/sub/deep.py": "x = 1"}) as root:
            found = mod.find_project_root(root / "pkg" / "sub" / "deep.py")
            self.assertEqual(found, root)

    def test_finds_root_from_directory(self):
        with TempProject({"pkg/mod.py": "x = 1"}) as root:
            self.assertEqual(mod.find_project_root(root / "pkg"), root)

    def test_root_itself_is_a_marker_dir(self):
        with TempProject({"mod.py": "x = 1"}) as root:
            self.assertEqual(mod.find_project_root(root), root)

    def test_no_marker_returns_start_dir(self):
        # /tmp has no project marker; the start dir comes back unchanged.
        found = mod.find_project_root(Path("/tmp"))
        self.assertTrue(found.is_dir())


class TestDiscoverPythonFiles(unittest.TestCase):
    """File discovery skips non-source dirs and is deterministic."""

    def test_finds_nested_python_files(self):
        with TempProject({"a.py": "", "pkg/b.py": "", "pkg/sub/c.py": ""}) as root:
            names = {p.name for p in mod.discover_python_files(root)}
            self.assertEqual(names, {"a.py", "b.py", "c.py"})

    def test_excludes_noise_directories(self):
        with TempProject(
            {
                "real.py": "",
                ".venv/lib/fake.py": "",
                "build/gen.py": "",
                "__pycache__/cached.py": "",
                "node_modules/dep.py": "",
            }
        ) as root:
            names = {p.name for p in mod.discover_python_files(root)}
            self.assertEqual(names, {"real.py"})

    def test_excludes_egg_info(self):
        with TempProject({"real.py": "", "thing.egg-info/pkg.py": ""}) as root:
            names = {p.name for p in mod.discover_python_files(root)}
            self.assertEqual(names, {"real.py"})

    def test_single_file_target_yields_itself(self):
        with TempProject({"only.py": "x = 1"}) as root:
            found = list(mod.discover_python_files(root / "only.py"))
            self.assertEqual([p.name for p in found], ["only.py"])

    def test_non_python_file_target_yields_nothing(self):
        with TempProject({"data.txt": "hi"}) as root:
            self.assertEqual(list(mod.discover_python_files(root / "data.txt")), [])

    def test_results_are_sorted(self):
        with TempProject({"c.py": "", "a.py": "", "b.py": ""}) as root:
            found = list(mod.discover_python_files(root))
            self.assertEqual(found, sorted(found))

    def test_returns_a_generator_not_a_list(self):
        # Callers rely on laziness; a regression to list() would be silent.
        with TempProject({"a.py": ""}) as root:
            self.assertFalse(isinstance(mod.discover_python_files(root), list))


class TestCollectPythonFiles(unittest.TestCase):
    """The capped collector reports the pre-cap total."""

    def test_reports_total_before_cap(self):
        with TempProject({f"m{i}.py": "" for i in range(5)}) as root:
            files, total = mod.collect_python_files(root, max_files=2)
            self.assertEqual(len(files), 2)
            self.assertEqual(total, 5)

    def test_no_cap_returns_everything(self):
        with TempProject({f"m{i}.py": "" for i in range(3)}) as root:
            files, total = mod.collect_python_files(root, max_files=0)
            self.assertEqual(len(files), 3)
            self.assertEqual(total, 3)

    def test_cap_larger_than_corpus_is_a_noop(self):
        with TempProject({"a.py": "", "b.py": ""}) as root:
            files, total = mod.collect_python_files(root, max_files=99)
            self.assertEqual((len(files), total), (2, 2))


class TestParseSource(unittest.TestCase):
    """Parsing never raises on bad input -- scans run over foreign trees."""

    def test_parses_valid_source(self):
        with TempProject({"ok.py": "def f():\n    return 1\n"}) as root:
            self.assertIsNotNone(mod.parse_source(root / "ok.py"))

    def test_syntax_error_returns_none(self):
        with TempProject({"bad.py": "def broken( :\n"}) as root:
            self.assertIsNone(mod.parse_source(root / "bad.py"))

    def test_missing_file_returns_none(self):
        self.assertIsNone(mod.parse_source(Path("/nonexistent/nope.py")))

    def test_null_bytes_return_none(self):
        with TempProject({"nul.py": "x = 1\x00\n"}) as root:
            self.assertIsNone(mod.parse_source(root / "nul.py"))


class TestParseCommonArgs(unittest.TestCase):
    """CLI parsing matches the contract every script shares."""

    def test_defaults_to_cwd_and_no_cap(self):
        self.assertEqual(mod.parse_common_args([]), (".", 0))

    def test_positional_target(self):
        self.assertEqual(mod.parse_common_args(["src/"]), ("src/", 0))

    def test_max_files(self):
        self.assertEqual(
            mod.parse_common_args(["src/", "--max-files", "10"]), ("src/", 10)
        )

    def test_unknown_flags_ignored_with_a_warning_on_stderr(self):
        err, out = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            parsed = mod.parse_common_args(["--verbose", "src/", "--deep"])
        self.assertEqual(parsed, ("src/", 0))
        self.assertIn("unknown flag(s) ignored: --verbose --deep", err.getvalue())
        # stdout carries the script's JSON: never a warning.
        self.assertEqual(out.getvalue(), "")

    def test_exclude_is_unknown_to_parse_common_args(self):
        # A script still on parse_common_args does not filter: --exclude must
        # not look accepted there.
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            mod.parse_common_args(["src/", "--exclude", "x"])
        self.assertIn("--exclude", err.getvalue())

    def test_declared_flags_are_not_warned_about(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            parsed = mod.parse_common_args(
                ["--include-tests", "src/", "--mode", "fast"],
                flags={"--include-tests"},
                value_flags={"--mode"},
            )
        self.assertEqual(parsed, ("src/", 0))
        self.assertEqual(err.getvalue(), "")

    def test_bad_max_files_exits_cleanly(self):
        # Must be SystemExit(2), not an unhandled ValueError traceback.
        with self.assertRaises(SystemExit) as ctx:
            mod.parse_common_args(["--max-files", "abc"])
        self.assertEqual(ctx.exception.code, 2)

    def test_help_prints_usage_and_exits_zero(self):
        # Before: `--help` was ignored as an unknown flag and a full
        # scan of the cwd ran; `-h` became the target path.
        for flag in ("--help", "-h"):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
                mod.parse_common_args(
                    ["src/", flag], "Do a thing.\n\nUsage: x [path]\n"
                )
            self.assertEqual(ctx.exception.code, 0)
            self.assertEqual(out.getvalue(), "Do a thing.\n\nUsage: x [path]\n")

    def test_no_help_flag_returns_normally(self):
        self.assertIsNone(mod.exit_on_help(["src/", "--max-files", "3"]))


_SCRIPTS_DIR = (
    Path(__file__).resolve().parent.parent
    / "plugins"
    / "code-review-toolkit"
    / "scripts"
)


class TestEveryScriptHonorsHelp(unittest.TestCase):
    """`--help`/`-h` prints usage and exits 0 WITHOUT scanning, for every script.

    Run from a directory holding a Python project, so a script that ignores the
    flag scans it and prints a JSON envelope instead of usage.
    """

    def test_every_script(self):
        scripts = sorted(
            p for p in _SCRIPTS_DIR.glob("*.py") if p.name != "scan_common.py"
        )
        self.assertGreater(len(scripts), 10)
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        with TempProject({"pkg/mod.py": "def f(x=[]):\n    return x\n"}) as root:
            for script in scripts:
                for flag in ("--help", "-h"):
                    with self.subTest(script=script.name, flag=flag):
                        proc = subprocess.run(
                            [sys.executable, str(script), flag],
                            cwd=root,
                            env=env,
                            capture_output=True,
                            text=True,
                            timeout=60,
                            check=False,
                        )
                        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                        # argparse usage, or the script's own docstring.
                        doc = ast.get_docstring(ast.parse(script.read_text())) or ""
                        first = proc.stdout.splitlines()[0] if proc.stdout else ""
                        self.assertTrue(
                            first.startswith("usage:") or first == doc.splitlines()[0],
                            proc.stdout[:300],
                        )
                        self.assertIn("usage", proc.stdout.lower())
                        self.assertNotIn('"project_root"', proc.stdout)


class TestResolveTarget(unittest.TestCase):
    """Target resolution distinguishes directory and file targets."""

    def test_directory_target_scans_itself(self):
        with TempProject({"pkg/m.py": ""}) as root:
            _, project_root, scan_root = mod.resolve_target(str(root / "pkg"))
            self.assertEqual(project_root, root)
            self.assertEqual(scan_root, root / "pkg")

    def test_file_target_scans_project_root(self):
        with TempProject({"pkg/m.py": ""}) as root:
            _, project_root, scan_root = mod.resolve_target(str(root / "pkg" / "m.py"))
            self.assertEqual(project_root, root)
            self.assertEqual(scan_root, root)


class TestBuildEnvelope(unittest.TestCase):
    """The shared JSON envelope reports capping accurately."""

    def test_not_capped(self):
        env = mod.build_envelope(Path("/p"), Path("/p/s"), 10, 10)
        self.assertFalse(env["files_capped"])
        self.assertEqual(env["files_total"], 10)
        self.assertEqual(env["project_root"], "/p")

    def test_capped(self):
        env = mod.build_envelope(Path("/p"), Path("/p"), 10, 4)
        self.assertTrue(env["files_capped"])
        self.assertEqual(env["files_analyzed"], 4)

    def test_has_all_shared_keys(self):
        env = mod.build_envelope(Path("/p"), Path("/p"), 1, 1)
        self.assertEqual(
            set(env),
            {
                "project_root",
                "scan_root",
                "files_total",
                "files_analyzed",
                "files_capped",
            },
        )


class TestDeduplicateFindings(unittest.TestCase):
    """Dedup keys on (file, line, type) and preserves first-seen order."""

    def test_removes_exact_duplicates(self):
        findings = [
            {"file": "a.py", "line": 1, "type": "x"},
            {"file": "a.py", "line": 1, "type": "x"},
        ]
        self.assertEqual(len(mod.deduplicate_findings(findings)), 1)

    def test_keeps_distinct_lines(self):
        findings = [
            {"file": "a.py", "line": 1, "type": "x"},
            {"file": "a.py", "line": 2, "type": "x"},
        ]
        self.assertEqual(len(mod.deduplicate_findings(findings)), 2)

    def test_accepts_kind_as_type_alias(self):
        findings = [
            {"file": "a.py", "line": 1, "kind": "x"},
            {"file": "a.py", "line": 1, "kind": "y"},
        ]
        self.assertEqual(len(mod.deduplicate_findings(findings)), 2)

    def test_preserves_first_occurrence(self):
        findings = [
            {"file": "a.py", "line": 1, "type": "x", "note": "first"},
            {"file": "a.py", "line": 1, "type": "x", "note": "second"},
        ]
        self.assertEqual(mod.deduplicate_findings(findings)[0]["note"], "first")


class TestLoadData(unittest.TestCase):
    """Data loading degrades gracefully on an older checkout."""

    def test_missing_file_returns_empty_dict(self):
        self.assertEqual(mod.load_data("definitely_not_here.json"), {})


class TestRelativeToRoot(unittest.TestCase):
    """Path relativization falls back to absolute for foreign paths."""

    def test_relative(self):
        self.assertEqual(
            mod.relative_to_root(Path("/p/pkg/m.py"), Path("/p")), "pkg/m.py"
        )

    def test_outside_root_falls_back_to_absolute(self):
        self.assertEqual(
            mod.relative_to_root(Path("/other/m.py"), Path("/p")), "/other/m.py"
        )


class TestParseScanArgs(unittest.TestCase):
    """The shared parser: --exclude, script flags, unknown-flag warnings."""

    def _parse(self, argv, **kw):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            parsed = mod.parse_scan_args(argv, **kw)
        return parsed, err.getvalue()

    def test_exclude_repeatable_and_comma_separated(self):
        parsed, err = self._parse(
            ["src", "--exclude", "a,b", "--exclude=c", "--exclude", "a"]
        )
        self.assertEqual(parsed.exclude, ["a", "b", "c"])
        self.assertEqual(parsed.target, "src")
        self.assertEqual(err, "")

    def test_value_flag_value_is_not_the_target(self):
        parsed, err = self._parse(
            ["--key-type", "B=fix", "src", "--key-type", "F=feature"],
            value_flags={"--key-type"},
        )
        self.assertEqual(parsed.target, "src")
        self.assertEqual(parsed.values["--key-type"], ["B=fix", "F=feature"])
        self.assertEqual(parsed.value("--key-type"), "F=feature")
        self.assertEqual(err, "")

    def test_unknown_flag_and_extra_positional_warn(self):
        parsed, err = self._parse(["src", "other", "--bogus"])
        self.assertEqual(parsed.target, "src")
        self.assertEqual(parsed.unknown, ["--bogus"])
        self.assertIn("--bogus", err)
        self.assertIn("ignored: other", err)

    def test_value_flag_without_value_warns(self):
        parsed, err = self._parse(["src", "--exclude"])
        self.assertEqual(parsed.exclude, [])
        self.assertIn("--exclude needs a value", err)

    def test_max_files_equals_form(self):
        parsed, _ = self._parse(["--max-files=7"])
        self.assertEqual((parsed.target, parsed.max_files), (".", 7))


class TestPathMatches(unittest.TestCase):
    """--exclude patterns: substrings stay substrings, globs match at any depth."""

    def test_plain_pattern_is_a_substring(self):
        self.assertTrue(mod.path_matches("pkg/vendor/x.py", "vendor"))
        self.assertFalse(mod.path_matches("pkg/core.py", "vendor"))

    def test_trailing_slash_means_a_directory(self):
        self.assertTrue(mod.path_matches("pkg/build/x.py", "build/"))
        self.assertFalse(mod.path_matches("pkg/rebuild/x.py", "build/"))

    def test_glob_matches_at_any_depth(self):
        self.assertTrue(
            mod.path_matches("pkg/migrations/versions/a.py", "migrations/*")
        )
        self.assertTrue(mod.path_matches("a/b/c_pb2.py", "*_pb2.py"))
        self.assertFalse(mod.path_matches("pkg/core.py", "migrations/*"))

    def test_anchored_glob(self):
        self.assertTrue(mod.path_matches("pkg/gen/x.py", "pkg/gen/*"))
        self.assertFalse(mod.path_matches("pkg/core.py", "pkg/gen/*"))

    def test_split_patterns(self):
        self.assertEqual(mod.split_patterns(["a, b", "", "c,a"]), ["a", "b", "c"])
        self.assertEqual(mod.split_patterns(None), [])


_REVISION = (
    'revision = "abc"\ndown_revision: str | None = None\n\ndef upgrade():\n    pass\n'
)


class TestMigrationKind(unittest.TestCase):
    """Migration revisions are recognised by layout AND content."""

    def test_alembic_revision(self):
        with TempProject({"pkg/migrations/versions/abc_add.py": _REVISION}) as root:
            path = root / "pkg/migrations/versions/abc_add.py"
            self.assertEqual(mod.migration_kind(path), "revision")

    def test_alembic_env(self):
        files = {
            "db/migrations/env.py": "from alembic import context\n",
            "db/migrations/versions/__init__.py": "",
        }
        with TempProject(files) as root:
            self.assertEqual(mod.migration_kind(root / "db/migrations/env.py"), "env")
            init = root / "db/migrations/versions/__init__.py"
            self.assertIsNone(mod.migration_kind(init))

    def test_django_migration(self):
        src = (
            "from django.db import migrations\n\n"
            "class Migration(migrations.Migration):\n    dependencies = []\n"
        )
        with TempProject({"shop/migrations/0001_initial.py": src}) as root:
            path = root / "shop/migrations/0001_initial.py"
            self.assertEqual(mod.migration_kind(path), "revision")

    def test_revision_variables_outside_a_migration_dir_are_not_migrations(self):
        with TempProject({"pkg/release.py": _REVISION}) as root:
            self.assertIsNone(mod.migration_kind(root / "pkg/release.py"))

    def test_helper_in_a_migrations_dir_is_not_a_revision(self):
        with TempProject({"pkg/migrations/helpers.py": "def f():\n    pass\n"}) as root:
            self.assertIsNone(mod.migration_kind(root / "pkg/migrations/helpers.py"))


class TestSelectPythonFiles(unittest.TestCase):
    """Exclusion and migration skipping happen before counting and capping."""

    FILES: ClassVar[dict[str, str]] = {
        "a_gen/x.py": "",
        "a_gen/y.py": "",
        "pkg/core.py": "",
        "pkg/migrations/versions/abc.py": _REVISION,
    }

    def test_excluded_files_are_not_counted_or_capped(self):
        with TempProject(self.FILES) as root:
            sel = mod.select_python_files(root, max_files=1, exclude=["a_gen/"])
        self.assertEqual([p.name for p in sel.files], ["core.py"])
        self.assertEqual(sel.files_total, 2)
        self.assertEqual(sel.excluded, ["a_gen/x.py", "a_gen/y.py"])
        self.assertEqual(sel.exclude_matches, {"a_gen/": 2})

    def test_migrations_skip_and_detect(self):
        with TempProject(self.FILES) as root:
            skip = mod.select_python_files(
                root / "pkg", project_root=root, migrations="skip"
            )
            keep = mod.select_python_files(
                root / "pkg", project_root=root, migrations="detect"
            )
            off = mod.select_python_files(root / "pkg", project_root=root)
        self.assertEqual([p.name for p in skip.files], ["core.py"])
        self.assertEqual(skip.envelope_fields()["migration_files_skipped"], 1)
        self.assertEqual(
            skip.envelope_fields()["migration_dirs"], ["pkg/migrations/versions"]
        )
        self.assertEqual(len(keep.files), 2)
        self.assertEqual(keep.envelope_fields()["migration_files_skipped"], 0)
        self.assertEqual(keep.envelope_fields()["migration_files_detected"], 1)
        self.assertNotIn("migration_files_detected", off.envelope_fields())

    def test_envelope_carries_the_selection(self):
        with TempProject(self.FILES) as root:
            sel = mod.select_python_files(root, exclude=["nothing-here"])
            env = mod.build_envelope(root, root, sel.files_total, len(sel.files), sel)
        self.assertEqual(env["files_excluded"], 0)
        self.assertEqual(env["exclude_matches"], {"nothing-here": 0})
        self.assertFalse(env["files_capped"])


class TestCoverageOmitConfig(unittest.TestCase):
    """The project's coverage `omit`, read the way coverage.py reads it."""

    def test_pyproject_omit(self):
        with TempProject({"pkg/a.py": ""}) as root:
            (root / "pyproject.toml").write_text(
                '[tool.coverage.run]\nomit = ["pkg/migrations/*", "*/shim.py"]\n',
                encoding="utf-8",
            )
            patterns, source = mod.coverage_omit_patterns(root)
            self.assertEqual(patterns, ["pkg/migrations/*", "*/shim.py"])
            self.assertEqual(source, "pyproject.toml [tool.coverage.run]")
            self.assertTrue(
                mod.coverage_omitted(root / "pkg/migrations/v/x.py", patterns, root)
            )
            self.assertTrue(mod.coverage_omitted(root / "lib/shim.py", patterns, root))
            self.assertFalse(mod.coverage_omitted(root / "pkg/a.py", patterns, root))

    def test_coveragerc_wins_over_pyproject(self):
        with TempProject({"pkg/a.py": ""}) as root:
            (root / ".coveragerc").write_text(
                "[run]\nomit = a/*, b/*\n", encoding="utf-8"
            )
            (root / "pyproject.toml").write_text(
                '[tool.coverage.run]\nomit = ["c/*"]\n', encoding="utf-8"
            )
            self.assertEqual(
                mod.coverage_omit_patterns(root), (["a/*", "b/*"], ".coveragerc [run]")
            )

    def test_setup_cfg_section(self):
        with TempProject({"pkg/a.py": ""}) as root:
            (root / "setup.cfg").write_text(
                "[coverage:run]\nomit =\n    gen/*\n", encoding="utf-8"
            )
            self.assertEqual(
                mod.coverage_omit_patterns(root),
                (["gen/*"], "setup.cfg [coverage:run]"),
            )

    def test_no_config(self):
        with TempProject({"pkg/a.py": ""}) as root:
            self.assertEqual(mod.coverage_omit_patterns(root), ([], None))


class TestHistoryRootArg(unittest.TestCase):
    """--git-dir maps the scan root into the checkout by relative path."""

    def test_default_is_the_scanned_tree(self):
        root, scan = Path("/e"), Path("/e/pkg")
        self.assertEqual(mod.history_root_arg(None, root, scan), (root, scan))

    def test_dot_git_and_mapping(self):
        with TempProject({"pkg/a.py": ""}) as export:
            got = mod.history_root_arg("/checkout/.git", export, export / "pkg")
        self.assertEqual(got, (Path("/checkout"), Path("/checkout/pkg")))


if __name__ == "__main__":
    unittest.main()
