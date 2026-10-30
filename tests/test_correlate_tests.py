"""Tests for correlate_tests.py."""

import json
import unittest

from helpers import TempProject, import_script

mod = import_script("correlate_tests")


class TestClassifyFiles(unittest.TestCase):
    """Test source vs. test file classification."""

    def test_test_prefix(self):
        with TempProject({
            "pkg/core.py": "",
            "tests/test_core.py": "",
        }) as root:
            files = mod.discover_python_files(root)
            source, test = mod.classify_files(files, root)
            source_names = {f.name for f in source}
            test_names = {f.name for f in test}
            self.assertIn("core.py", source_names)
            self.assertIn("test_core.py", test_names)

    def test_test_suffix(self):
        with TempProject({
            "pkg/core.py": "",
            "pkg/core_test.py": "",
        }) as root:
            files = mod.discover_python_files(root)
            source, test = mod.classify_files(files, root)
            test_names = {f.name for f in test}
            self.assertIn("core_test.py", test_names)

    def test_test_directory(self):
        with TempProject({
            "pkg/core.py": "",
            "test/helpers.py": "",
        }) as root:
            files = mod.discover_python_files(root)
            source, test = mod.classify_files(files, root)
            test_names = {f.name for f in test}
            self.assertIn("helpers.py", test_names)

    def test_setup_py_excluded(self):
        with TempProject({
            "setup.py": "from setuptools import setup\nsetup()\n",
            "pkg/__init__.py": "",
        }) as root:
            files = mod.discover_python_files(root)
            source, test = mod.classify_files(files, root)
            source_names = {f.name for f in source}
            self.assertNotIn("setup.py", source_names)


class TestExtractTestInfo(unittest.TestCase):
    """Test extraction of test classes and methods."""

    def test_basic_test_class(self):
        with TempProject({
            "test_foo.py": (
                "import unittest\n"
                "\n"
                "class TestFoo(unittest.TestCase):\n"
                "    def test_bar(self):\n"
                "        pass\n"
                "\n"
                "    def test_baz(self):\n"
                "        pass\n"
                "\n"
                "    def setUp(self):\n"
                "        pass\n"
            ),
        }) as root:
            result = mod._extract_test_info(root / "test_foo.py")
            self.assertFalse(result["parse_error"])
            self.assertEqual(len(result["classes"]), 1)
            cls = result["classes"][0]
            self.assertEqual(cls["name"], "TestFoo")
            self.assertEqual(len(cls["test_methods"]), 2)
            self.assertIn("test_bar", cls["test_methods"])
            self.assertIn("test_baz", cls["test_methods"])

    def test_skipped_tests_detected(self):
        with TempProject({
            "test_foo.py": (
                "import unittest\n"
                "\n"
                "class TestFoo(unittest.TestCase):\n"
                "    def test_active(self):\n"
                "        pass\n"
                "\n"
                "    @unittest.skip('reason')\n"
                "    def test_skipped(self):\n"
                "        pass\n"
            ),
        }) as root:
            result = mod._extract_test_info(root / "test_foo.py")
            cls = result["classes"][0]
            self.assertEqual(len(cls["test_methods"]), 1)
            self.assertEqual(len(cls["skipped_methods"]), 1)
            self.assertIn("test_skipped", cls["skipped_methods"])

    def test_standalone_test_functions(self):
        with TempProject({
            "test_foo.py": (
                "def test_something():\n"
                "    assert True\n"
                "\n"
                "def helper():\n"
                "    pass\n"
            ),
        }) as root:
            result = mod._extract_test_info(root / "test_foo.py")
            self.assertEqual(len(result["standalone_tests"]), 1)
            self.assertIn("test_something", result["standalone_tests"])


class TestSourceTestMatching(unittest.TestCase):
    """Test the heuristic matching of test files to source files."""

    def test_simple_match(self):
        with TempProject({
            "pkg/runner.py": "",
            "tests/test_runner.py": "",
        }) as root:
            source_files = [root / "pkg/runner.py"]
            test_file = root / "tests/test_runner.py"
            matches = mod._match_test_to_source(
                test_file, source_files, root
            )
            self.assertEqual(len(matches), 1)
            self.assertIn("pkg/runner.py", matches)

    def test_suffix_match(self):
        with TempProject({
            "pkg/core.py": "",
            "tests/core_test.py": "",
        }) as root:
            source_files = [root / "pkg/core.py"]
            test_file = root / "tests/core_test.py"
            matches = mod._match_test_to_source(
                test_file, source_files, root
            )
            self.assertEqual(len(matches), 1)

    def test_no_match(self):
        with TempProject({
            "pkg/core.py": "",
            "tests/test_unrelated.py": "",
        }) as root:
            source_files = [root / "pkg/core.py"]
            test_file = root / "tests/test_unrelated.py"
            matches = mod._match_test_to_source(
                test_file, source_files, root
            )
            self.assertEqual(len(matches), 0)

    def test_multiple_matches(self):
        # If two source files have the same stem in different packages.
        with TempProject({
            "pkg1/utils.py": "",
            "pkg2/utils.py": "",
            "tests/test_utils.py": "",
        }) as root:
            source_files = [root / "pkg1/utils.py", root / "pkg2/utils.py"]
            test_file = root / "tests/test_utils.py"
            matches = mod._match_test_to_source(
                test_file, source_files, root
            )
            self.assertEqual(len(matches), 2)


class TestExtractSourceInfo(unittest.TestCase):
    """Test extraction of public API from source files."""

    def test_public_functions(self):
        with TempProject({
            "mod.py": (
                "def public_func():\n"
                "    pass\n"
                "\n"
                "def _private_func():\n"
                "    pass\n"
            ),
        }) as root:
            result = mod._extract_source_info(root / "mod.py")
            self.assertEqual(len(result["functions"]), 1)
            self.assertEqual(result["functions"][0], "public_func")

    def test_all_overrides_public(self):
        with TempProject({
            "mod.py": (
                '__all__ = ["_special"]\n'
                "\n"
                "def _special():\n"
                "    pass\n"
                "\n"
                "def public_func():\n"
                "    pass\n"
            ),
        }) as root:
            result = mod._extract_source_info(root / "mod.py")
            names = result["functions"]
            self.assertIn("_special", names)
            self.assertIn("public_func", names)


class TestEndToEnd(unittest.TestCase):
    """Integration test for the full pipeline."""

    def test_small_project(self):
        with TempProject({
            "pkg/__init__.py": "",
            "pkg/core.py": (
                "def main():\n"
                "    pass\n"
                "\n"
                "def helper():\n"
                "    pass\n"
            ),
            "pkg/utils.py": (
                "def format_output(data):\n"
                "    pass\n"
            ),
            "tests/test_core.py": (
                "import unittest\n"
                "from pkg.core import main\n"
                "\n"
                "class TestCore(unittest.TestCase):\n"
                "    def test_main(self):\n"
                "        main()\n"
            ),
        }) as root:
            files = mod.discover_python_files(root)
            source_files, test_files = mod.classify_files(files, root)

            # utils.py should be untested.
            self.assertTrue(
                any(f.name == "utils.py" for f in source_files),
                "utils.py should be in source files"
            )
            # test_core.py should match core.py.
            self.assertTrue(
                any(f.name == "test_core.py" for f in test_files),
                "test_core.py should be in test files"
            )


class TestMaxFiles(unittest.TestCase):
    """Test --max-files caps file processing."""

    def test_max_files_caps_output(self):
        files = {
            f"pkg/mod{i}.py": f"def func{i}():\n    return {i}\n"
            for i in range(10)
        }
        files["pkg/__init__.py"] = ""
        with TempProject(files) as root:
            import io
            import sys
            old_stdout = sys.stdout
            sys.stdout = io.StringIO()
            old_argv = sys.argv
            sys.argv = [
                "correlate_tests.py", str(root),
                "--max-files", "3",
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


def _run_main(target) -> dict:
    """Run correlate_tests.main() on *target* and return its parsed JSON."""
    import io
    import sys

    old_stdout, old_argv = sys.stdout, sys.argv
    sys.stdout = io.StringIO()
    sys.argv = ["correlate_tests.py", str(target)]
    try:
        mod.main()
        return json.loads(sys.stdout.getvalue())
    finally:
        sys.stdout, sys.argv = old_stdout, old_argv


class TestSuiteWideCounts(unittest.TestCase):
    """`summary.total_test_methods` counts the whole suite, not matched files."""

    def test_uncorrelated_pytest_functions_are_counted(self):
        with TempProject({
            "pkg/__init__.py": "",
            "pkg/core.py": "def run():\n    pass\n",
            # Name-matched to core.py.
            "tests/test_core.py": "def test_run():\n    pass\n",
            # Feature-named: matches no source by name.
            "tests/test_feature.py": (
                "def test_a():\n    pass\n"
                "\n"
                "def test_b():\n    pass\n"
                "\n"
                "class TestPlain:\n"
                "    def test_c(self):\n        pass\n"
            ),
        }) as root:
            out = _run_main(root)
        self.assertEqual(out["summary"]["total_test_methods"], 4)

    def test_file_matching_two_sources_counted_once(self):
        with TempProject({
            "pkg1/utils.py": "",
            "pkg2/utils.py": "",
            "tests/test_utils.py": "def test_x():\n    pass\n",
        }) as root:
            out = _run_main(root)
        self.assertEqual(out["summary"]["total_test_methods"], 1)

    def test_skipped_pytest_function_counted_as_skipped(self):
        with TempProject({
            "tests/test_feature.py": (
                "import pytest\n"
                "\n"
                "@pytest.mark.skip(reason='x')\n"
                "def test_skipped():\n    pass\n"
                "\n"
                "def test_active():\n    pass\n"
            ),
        }) as root:
            out = _run_main(root)
            info = mod._extract_test_info(root / "tests/test_feature.py")
        self.assertEqual(info["standalone_tests"], ["test_active"])
        self.assertEqual(info["skipped_standalone_tests"], ["test_skipped"])
        self.assertEqual(out["summary"]["total_test_methods"], 1)
        self.assertEqual(out["summary"]["total_skipped_tests"], 1)


_IMPORT_PROJECT = {
    "pkg/__init__.py": "",
    "pkg/registry.py": "def connect():\n    pass\n",
    "pkg/service.py": "def serve():\n    pass\n",
    "pkg/storage/__init__.py": "",
    "pkg/storage/engine.py": "class Engine:\n    pass\n",
    "pkg/storage/base.py": "class Base:\n    pass\n",
    "pkg/untouched.py": "def idle():\n    pass\n",
}


class TestImportCorrelation(unittest.TestCase):
    """A test file that imports a module correlates with it, whatever its name."""

    def _coverage(self, out: dict) -> dict:
        return {sc["source_file"]: sc for sc in out["source_coverage"]}

    def test_feature_named_tests_correlate_by_import(self):
        files = dict(_IMPORT_PROJECT)
        files["tests/test_engine_shutdown_race.py"] = (
            "from pkg.storage.engine import Engine\n"
            "\n"
            "def test_close():\n    Engine()\n"
        )
        files["tests/test_catalog.py"] = (
            "import pkg.registry\n"
            "\n"
            "def test_open():\n    pkg.registry.connect()\n"
        )
        files["tests/test_api.py"] = (
            "def test_serve():\n"
            "    from pkg import service\n"
            "    service.serve()\n"
        )
        with TempProject(files) as root:
            out = _run_main(root)
        cov = self._coverage(out)
        for src in ("pkg/storage/engine.py", "pkg/registry.py", "pkg/service.py"):
            self.assertEqual(cov[src]["match_method"], "import", src)
            self.assertTrue(cov[src]["has_tests"], src)
            self.assertNotIn(src, out["untested_sources"])
        # True negatives: nothing imports these.
        self.assertIn("pkg/untouched.py", out["untested_sources"])
        self.assertIn("pkg/storage/base.py", out["untested_sources"])
        self.assertIsNone(cov["pkg/untouched.py"]["match_method"])
        # `from pkg import service` imports the submodule, not pkg/__init__.
        self.assertIn("pkg/__init__.py", out["untested_sources"])
        self.assertEqual(
            out["summary"]["source_files_with_tests_by_import_only"], 3
        )

    def test_name_and_import_match_reported_together(self):
        files = dict(_IMPORT_PROJECT)
        files["tests/test_registry.py"] = (
            "from pkg.registry import connect\n\ndef test_c():\n    connect()\n"
        )
        files["tests/test_service.py"] = "def test_s():\n    pass\n"
        with TempProject(files) as root:
            cov = self._coverage(_run_main(root))
        self.assertEqual(cov["pkg/registry.py"]["match_method"], "name+import")
        self.assertEqual(cov["pkg/service.py"]["match_method"], "name")
        self.assertEqual(cov["pkg/service.py"]["import_matched_test_files"], [])

    def test_conftest_imports_do_not_count(self):
        files = dict(_IMPORT_PROJECT)
        files["tests/conftest.py"] = "import pkg.service\n"
        files["tests/helpers.py"] = "from pkg.registry import connect\n"
        with TempProject(files) as root:
            out = _run_main(root)
        self.assertIn("pkg/service.py", out["untested_sources"])
        self.assertIn("pkg/registry.py", out["untested_sources"])

    def test_src_layout_and_relative_import(self):
        with TempProject({
            "src/lib/__init__.py": "",
            "src/lib/core.py": "def f():\n    pass\n",
            "src/lib/extra.py": "def g():\n    pass\n",
            "src/lib/tests/__init__.py": "",
            "src/lib/tests/test_things.py": (
                "from lib.core import f\n"
                "from ..extra import g\n"
                "\n"
                "def test_f():\n    f()\n"
            ),
        }) as root:
            out = _run_main(root)
        self.assertNotIn("src/lib/core.py", out["untested_sources"])
        self.assertNotIn("src/lib/extra.py", out["untested_sources"])


class TestExternalTestDiscovery(unittest.TestCase):
    """A package scope finds the project's tests outside it."""

    def test_pytest_testpaths_preferred(self):
        with TempProject({
            "pkg/__init__.py": "",
            "pkg/core.py": "def f():\n    pass\n",
            "checks/core_checks.py": (
                "from pkg.core import f\n\ndef test_f():\n    f()\n"
            ),
            # A conventional dir that pytest would NOT collect.
            "tests/test_core.py": "def test_other():\n    pass\n",
        }) as root:
            # TempProject writes its own pyproject.toml; replace it.
            (root / "pyproject.toml").write_text(
                '[project]\nname = "x"\n'
                '[tool.pytest.ini_options]\ntestpaths = ["checks"]\n',
                encoding="utf-8",
            )
            out = _run_main(root / "pkg")
        self.assertEqual(out["summary"]["external_test_roots"], ["checks"])
        self.assertNotIn("pkg/core.py", out["untested_sources"])

    def test_conventional_dir_without_config(self):
        with TempProject({
            "pkg/__init__.py": "",
            "pkg/core.py": "def f():\n    pass\n",
            "tests/test_core.py": "def test_f():\n    pass\n",
        }) as root:
            out = _run_main(root / "pkg")
        self.assertEqual(out["summary"]["external_test_roots"], ["tests"])


class TestModuleNames(unittest.TestCase):
    def test_package_rooted_names(self):
        with TempProject({
            "src/pkg/__init__.py": "",
            "src/pkg/sub/__init__.py": "",
            "src/pkg/sub/mod.py": "",
            "script.py": "",
        }) as root:
            self.assertEqual(
                mod.module_name(root / "src/pkg/sub/mod.py", root), "pkg.sub.mod"
            )
            self.assertEqual(
                mod.module_name(root / "src/pkg/sub/__init__.py", root), "pkg.sub"
            )
            self.assertEqual(mod.module_name(root / "script.py", root), "script")


if __name__ == "__main__":
    unittest.main()
