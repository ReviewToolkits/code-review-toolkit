"""Tests for find_dead_symbols.py."""

import json
import unittest

from helpers import TempProject, import_script

mod = import_script("find_dead_symbols")


class TestUnusedImports(unittest.TestCase):
    """Test unused import detection."""

    def _find_unused(self, source: str, filename: str = "mod.py") -> list[dict]:
        with TempProject({filename: source}) as root:
            analysis = mod.analyze_file(root / filename, root)
            return mod.find_unused_imports(analysis)

    def test_used_import_not_flagged(self):
        unused = self._find_unused(
            "import os\n"
            "\n"
            "print(os.getcwd())\n"
        )
        self.assertEqual(len(unused), 0)

    def test_unused_import_flagged(self):
        unused = self._find_unused(
            "import os\n"
            "import json\n"
            "\n"
            "print(os.getcwd())\n"
        )
        self.assertEqual(len(unused), 1)
        self.assertEqual(unused[0]["name"], "json")

    def test_from_import_unused(self):
        unused = self._find_unused(
            "from pathlib import Path, PurePath\n"
            "\n"
            "p = Path('.')\n"
        )
        self.assertEqual(len(unused), 1)
        self.assertEqual(unused[0]["name"], "PurePath")

    def test_init_imports_not_flagged(self):
        """__init__.py imports are potential re-exports — skip them."""
        unused = self._find_unused(
            "from .core import main\n",
            filename="pkg/__init__.py",
        )
        # Even though main isn't used in this file, it's an __init__.py
        # so it could be a re-export.
        self.assertEqual(len(unused), 0)

    def test_all_protects_import(self):
        unused = self._find_unused(
            '__all__ = ["helper"]\n'
            "from .utils import helper\n"
            "from .utils import unused_thing\n"
        )
        # helper is in __all__ so it's protected.
        # unused_thing is not in __all__ and not referenced.
        # But this file isn't __init__.py, so unused_thing should be flagged.
        names = {u["name"] for u in unused}
        self.assertNotIn("helper", names)

    def test_multiline_from_import_reports_the_name_line_like_ruff(self):
        """#43: ruff F401 reports the line of the name; the statement line made
        findings unmatchable by file:line."""
        unused = self._find_unused(
            "from os.path import (\n"
            "    join,\n"
            "    split,\n"
            ")\n"
            "\n"
            "join('a', 'b')\n"
        )
        self.assertEqual(len(unused), 1)
        self.assertEqual(unused[0]["name"], "split")
        self.assertEqual(unused[0]["line"], 3)
        self.assertEqual(unused[0]["statement_line"], 1)

    def test_single_line_import_line_is_unchanged(self):
        unused = self._find_unused("x = 1\nimport os, json\n\nos.getcwd()\n")
        self.assertEqual([(u["name"], u["line"]) for u in unused], [("json", 2)])
        self.assertEqual(unused[0]["statement_line"], 2)

    def test_aliased_import_used(self):
        unused = self._find_unused(
            "import numpy as np\n"
            "\n"
            "arr = np.array([1, 2, 3])\n"
        )
        self.assertEqual(len(unused), 0)

    def test_aliased_import_unused(self):
        unused = self._find_unused(
            "import numpy as np\n"
            "\n"
            "x = 42\n"
        )
        self.assertEqual(len(unused), 1)
        self.assertEqual(unused[0]["name"], "np")


class TestUnreferencedSymbols(unittest.TestCase):
    """Test detection of defined-but-never-referenced symbols."""

    def _find_unreferenced(self, files: dict[str, str]) -> list[dict]:
        with TempProject(files) as root:
            all_files = mod.discover_python_files(root)
            analyses = [mod.analyze_file(f, root) for f in all_files]
            return mod.find_unreferenced_symbols(analyses)

    def test_used_function_not_flagged(self):
        unreferenced = self._find_unreferenced({
            "mod.py": "def helper():\n    pass\n",
            "main.py": "from mod import helper\nhelper()\n",
        })
        names = {u["name"] for u in unreferenced}
        self.assertNotIn("helper", names)

    def test_unused_function_flagged(self):
        unreferenced = self._find_unreferenced({
            "mod.py": (
                "def used():\n    pass\n"
                "\n"
                "def unused_orphan():\n    pass\n"
            ),
            "main.py": "from mod import used\nused()\n",
        })
        names = {u["name"] for u in unreferenced}
        self.assertIn("unused_orphan", names)

    def test_magic_methods_not_flagged(self):
        unreferenced = self._find_unreferenced({
            "mod.py": (
                "class Foo:\n"
                "    def __init__(self):\n"
                "        pass\n"
                "\n"
                "    def __repr__(self):\n"
                "        return 'Foo()'\n"
                "\n"
                "    def __enter__(self):\n"
                "        return self\n"
                "\n"
                "    def __exit__(self, *args):\n"
                "        pass\n"
            ),
        })
        names = {u["name"] for u in unreferenced}
        for magic in ("__init__", "__repr__", "__enter__", "__exit__"):
            self.assertNotIn(f"Foo.{magic}", names)

    def test_test_methods_not_flagged(self):
        unreferenced = self._find_unreferenced({
            "tests/test_foo.py": (
                "import unittest\n"
                "\n"
                "class TestFoo(unittest.TestCase):\n"
                "    def test_something(self):\n"
                "        pass\n"
            ),
        })
        names = {u["name"] for u in unreferenced}
        self.assertNotIn("test_something", names)

    def test_setup_teardown_not_flagged(self):
        unreferenced = self._find_unreferenced({
            "tests/test_foo.py": (
                "import unittest\n"
                "\n"
                "class TestFoo(unittest.TestCase):\n"
                "    def setUp(self):\n"
                "        pass\n"
                "    def tearDown(self):\n"
                "        pass\n"
                "    def test_x(self):\n"
                "        pass\n"
            ),
        })
        names = {u["name"] for u in unreferenced}
        self.assertNotIn("setUp", names)
        self.assertNotIn("tearDown", names)

    def test_all_protects_symbol(self):
        unreferenced = self._find_unreferenced({
            "mod.py": (
                '__all__ = ["protected"]\n'
                "\n"
                "def protected():\n"
                "    pass\n"
            ),
        })
        names = {u["name"] for u in unreferenced}
        self.assertNotIn("protected", names)

    def test_main_guard_protects(self):
        unreferenced = self._find_unreferenced({
            "script.py": (
                "def run():\n"
                "    print('running')\n"
                "\n"
                'if __name__ == "__main__":\n'
                "    run()\n"
            ),
        })
        names = {u["name"] for u in unreferenced}
        self.assertNotIn("run", names)


class TestOrphanFiles(unittest.TestCase):
    """Test orphan file detection."""

    def _find_orphans(self, files: dict[str, str]) -> list[dict]:
        with TempProject(files) as root:
            all_files = mod.discover_python_files(root)
            analyses = [mod.analyze_file(f, root) for f in all_files]
            return mod.find_orphan_files(analyses, root)

    def test_imported_file_not_orphan(self):
        orphans = self._find_orphans({
            "pkg/__init__.py": "",
            "pkg/core.py": "from pkg.utils import helper\n",
            "pkg/utils.py": "def helper(): pass\n",
        })
        orphan_files = {o["file"] for o in orphans}
        self.assertNotIn("pkg/utils.py", orphan_files)

    def test_unimported_file_is_orphan(self):
        orphans = self._find_orphans({
            "pkg/__init__.py": "",
            "pkg/core.py": "x = 1\n",
            "pkg/forgotten.py": "def old_thing(): pass\n",
        })
        orphan_files = {o["file"] for o in orphans}
        self.assertIn("pkg/forgotten.py", orphan_files)

    def test_test_files_excluded(self):
        orphans = self._find_orphans({
            "pkg/__init__.py": "",
            "tests/test_core.py": "import unittest\n",
        })
        orphan_files = {o["file"] for o in orphans}
        self.assertNotIn("tests/test_core.py", orphan_files)

    def test_main_guard_excluded(self):
        orphans = self._find_orphans({
            "script.py": (
                "def main(): pass\n"
                'if __name__ == "__main__":\n'
                "    main()\n"
            ),
        })
        orphan_files = {o["file"] for o in orphans}
        self.assertNotIn("script.py", orphan_files)

    def test_init_excluded(self):
        orphans = self._find_orphans({
            "pkg/__init__.py": "",
        })
        orphan_files = {o["file"] for o in orphans}
        self.assertNotIn("pkg/__init__.py", orphan_files)


class TestCommentedCodeDetection(unittest.TestCase):
    """Test commented-out code block detection."""

    def test_detects_commented_code(self):
        with TempProject({
            "mod.py": (
                "x = 1\n"
                "# def old_function():\n"
                "#     if True:\n"
                "#         return 42\n"
                "#     return 0\n"
                "y = 2\n"
            ),
        }) as root:
            blocks = mod.find_commented_code(root / "mod.py", root)
            self.assertGreaterEqual(len(blocks), 1)

    def test_ignores_documentation_comments(self):
        with TempProject({
            "mod.py": (
                "# This module handles data processing.\n"
                "# It provides utilities for formatting output.\n"
                "# Author: someone\n"
                "\n"
                "x = 1\n"
            ),
        }) as root:
            blocks = mod.find_commented_code(root / "mod.py", root)
            # These are documentation comments, not code.
            self.assertEqual(len(blocks), 0)


class TestNameCollection(unittest.TestCase):
    """Test that referenced name collection is thorough."""

    def test_attribute_access_collected(self):
        with TempProject({
            "mod.py": (
                "import os\n"
                "x = os.path.join('a', 'b')\n"
            ),
        }) as root:
            analysis = mod.analyze_file(root / "mod.py", root)
            names = analysis["referenced_names"]
            self.assertIn("os", names)
            self.assertIn("path", names)
            self.assertIn("join", names)


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
                "find_dead_symbols.py", str(root),
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


class TestDeadCodeFalsePositives(unittest.TestCase):
    """Found by the coverage.py benchmark, where the scanner scored 0 of 51:
    every high-confidence item was a false positive."""

    def test_future_import_is_never_unused(self):
        # `from __future__ import annotations` is a COMPILER DIRECTIVE, not an
        # import: it binds no name, so a reference scanner always calls it
        # unused. It was 42 of the 42 unused imports reported for coverage.py.
        with TempProject({"m.py": "from __future__ import annotations\nx = 1\n"}) as root:
            out = mod.analyze_file(root / "m.py", root)
        self.assertEqual(mod.find_unused_imports(out), [])

    def test_genuinely_unused_import_still_reported(self):
        with TempProject({"m.py": "import os\nx = 1\n"}) as root:
            out = mod.analyze_file(root / "m.py", root)
        self.assertEqual([u["name"] for u in mod.find_unused_imports(out)], ["os"])

    def test_debugging_pragma_symbol_is_not_dead(self):
        src = "def pp(x):  # pragma: debugging\n    print(x)\n"
        with TempProject({"m.py": src}) as root:
            out = mod.analyze_file(root / "m.py", root)
        self.assertEqual(mod.find_unreferenced_symbols([out]), [])

    def test_external_references_keep_a_symbol_alive(self):
        # A public helper used only by tests/ or declared in setup.py is not
        # dead; scoping references to the reviewed package alone reported 9
        # such symbols for coverage.py, all of them referenced elsewhere.
        src = "def import_local_file(name):\n    return name\n"
        with TempProject({"m.py": src}) as root:
            out = mod.analyze_file(root / "m.py", root)
        self.assertTrue(mod.find_unreferenced_symbols([out]))
        self.assertEqual(
            mod.find_unreferenced_symbols([out], {"import_local_file"}), []
        )

    def test_main_module_is_not_an_orphan(self):
        # `python -m pkg` executes __main__.py; being unimported is what it IS for.
        with TempProject({"pkg/__main__.py": "print(1)\n"}) as root:
            out = mod.analyze_file(root / "pkg" / "__main__.py", root)
        self.assertEqual(mod.find_orphan_files([out], root), [])


if __name__ == "__main__":
    unittest.main()

