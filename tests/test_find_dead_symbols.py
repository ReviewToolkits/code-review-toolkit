"""Tests for find_dead_symbols.py."""

import json
import unittest
from typing import ClassVar

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


def _run_main(root, target) -> dict:
    import io
    import sys

    old_stdout, old_argv = sys.stdout, sys.argv
    sys.stdout = io.StringIO()
    sys.argv = ["find_dead_symbols.py", str(target)]
    try:
        mod.main()
        return json.loads(sys.stdout.getvalue())
    finally:
        sys.stdout, sys.argv = old_stdout, old_argv


def _unreferenced(files: dict[str, str], excluded: dict | None = None) -> dict:
    with TempProject(files) as root:
        analyses = [mod.analyze_file(f, root) for f in mod.discover_python_files(root)]
        kwargs = {} if excluded is None else {"excluded": excluded}
        return {
            u["name"]: u for u in mod.find_unreferenced_symbols(analyses, **kwargs)
        }


class TestUnreferencedMethods(unittest.TestCase):
    """#39: every method was skipped while the summary read
    `unreferenced_symbols: 0`, an absence presented as a clean result."""

    def test_private_method_with_no_reference_is_reported(self):
        found = _unreferenced({
            "pkg/db.py": (
                "class DB:\n"
                "    def write(self):\n"
                "        return self._writer()\n"
                "    def _writer(self):\n"
                "        return 1\n"
                "    def _require_conn(self):\n"
                "        return 2\n"
            ),
            "pkg/use.py": "from pkg.db import DB\nDB().write()\n",
        })
        self.assertIn("DB._require_conn", found)
        self.assertEqual(found["DB._require_conn"]["type"], "method")
        self.assertEqual(found["DB._require_conn"]["confidence"], "medium")
        self.assertNotIn("DB._writer", found)
        self.assertNotIn("DB.write", found)

    def test_public_method_is_reported_at_low_confidence(self):
        # It may be API called from outside the project.
        found = _unreferenced({"pkg/a.py": "class A:\n    def export(self): pass\n"})
        self.assertEqual(found["A.export"]["confidence"], "low")

    def test_dunder_and_sunder_hooks_are_excluded(self):
        excluded: dict = {}
        found = _unreferenced(
            {
                "pkg/a.py": (
                    "import enum\n"
                    "class Color(enum.Enum):\n"
                    "    RED = 1\n"
                    "    def _missing_(cls, value): pass\n"
                    "    def __rich_repr__(self): pass\n"
                ),
            },
            excluded,
        )
        self.assertFalse([n for n in found if n.startswith("Color.")])
        self.assertEqual(excluded, {"dunder": 1, "sunder_hook": 1})

    def test_override_of_a_framework_hook_is_excluded(self):
        # A subclass of a class defined outside the project may override a hook
        # the framework calls by name: invisible to a reference scan.
        excluded: dict = {}
        found = _unreferenced(
            {
                "pkg/srv.py": (
                    "import uvicorn\n"
                    "class Server(uvicorn.Server):\n"
                    "    def handle_exit(self, sig, frame): pass\n"
                    "class Quiet(Server):\n"
                    "    def install_signal_handlers(self): pass\n"
                ),
            },
            excluded,
        )
        self.assertNotIn("Server.handle_exit", found)
        self.assertNotIn("Quiet.install_signal_handlers", found)
        self.assertEqual(excluded.get("framework_base"), 2)

    def test_subclass_of_an_inert_base_is_still_checked(self):
        found = _unreferenced({
            "pkg/a.py": (
                "from abc import ABC\n"
                "class Base(ABC):\n    pass\n"
                "class Impl(Base):\n    def _stale(self): pass\n"
                "class Boom(ValueError):\n    def _stale2(self): pass\n"
            ),
        })
        self.assertIn("Impl._stale", found)
        self.assertIn("Boom._stale2", found)

    def test_registering_decorator_excludes_but_property_does_not(self):
        excluded: dict = {}
        found = _unreferenced(
            {
                "pkg/a.py": (
                    "class Model:\n"
                    "    @validator('name')\n"
                    "    def _check_name(cls, v): return v\n"
                    "    @property\n"
                    "    def _unused_prop(self): return 1\n"
                    "    @staticmethod\n"
                    "    def _unused_static(): return 1\n"
                ),
            },
            excluded,
        )
        self.assertNotIn("Model._check_name", found)
        self.assertIn("Model._unused_prop", found)
        self.assertIn("Model._unused_static", found)
        self.assertEqual(excluded, {"decorated": 1})

    def test_getattr_with_a_string_constant_keeps_a_method_alive(self):
        found = _unreferenced({
            "pkg/a.py": (
                "class Jobs:\n"
                "    def pause_job(self): pass\n"
                "    def _orphan(self): pass\n"
                "def act(jobs, action):\n"
                "    return getattr(jobs, action)()\n"
                "def pause(jobs):\n"
                "    return act(jobs, 'pause_job')\n"
            ),
        })
        self.assertNotIn("Jobs.pause_job", found)
        self.assertIn("Jobs._orphan", found)

    def test_computed_getattr_prefix_excludes_matching_methods(self):
        excluded: dict = {}
        found = _unreferenced(
            {
                "pkg/cli.py": (
                    "class CLI:\n"
                    "    def cmd_run(self): pass\n"
                    "    def _other(self): pass\n"
                    "    def dispatch(self, name):\n"
                    "        return getattr(self, f'cmd_{name}')()\n"
                    "CLI().dispatch('run')\n"
                ),
            },
            excluded,
        )
        self.assertNotIn("CLI.cmd_run", found)
        self.assertIn("CLI._other", found)
        self.assertEqual(excluded, {"dynamic_dispatch": 1})

    def test_methods_in_test_files_are_excluded(self):
        excluded: dict = {}
        found = _unreferenced(
            {"tests/test_a.py": "class Helper:\n    def _build(self): pass\n"},
            excluded,
        )
        self.assertNotIn("Helper._build", found)
        self.assertEqual(excluded, {"test_file": 1})

    def test_summary_says_methods_were_checked_and_what_was_left_out(self):
        files = {
            "pkg/__init__.py": "",
            "pkg/a.py": (
                "class A:\n"
                "    def __init__(self): pass\n"
                "    def _dead(self): pass\n"
            ),
        }
        with TempProject(files) as root:
            out = _run_main(root, root)
        summary = out["summary"]
        self.assertTrue(summary["methods_checked"])
        self.assertEqual(summary["methods_defined"], 2)
        self.assertEqual(summary["methods_excluded"], 1)
        self.assertEqual(summary["methods_excluded_by_reason"], {"dunder": 1})
        self.assertIn(
            "A._dead", [u["name"] for u in out["unreferenced_symbols"]]
        )


class TestReferencedOnlyFromTests(unittest.TestCase):
    """#40: a mention in a test counted as a use, so production code kept alive
    only by its own tests was invisible."""

    _FILES: ClassVar[dict[str, str]] = {
        "pkg/__init__.py": "",
        "pkg/store.py": (
            "class Store:\n"
            "    def get(self, k): return k\n"
            "    def has_key(self, k): return True\n"
            "def percentiles(xs): return xs\n"
            "def used(): return 1\n"
            "def registered(): return 2\n"
        ),
        "pkg/app.py": (
            "from pkg.store import Store, used\n"
            "Store().get(1)\n"
            "used()\n"
        ),
        "tests/test_store.py": (
            "from pkg.store import Store, percentiles, used, registered\n"
            "def test_it():\n"
            "    assert Store().has_key(1)\n"
            "    assert percentiles([1])\n"
            "    assert used()\n"
            "    assert registered()\n"
        ),
        "docs/api.md": "`has_key` checks membership.\n",
    }

    def test_package_review_with_tests_outside_the_scan(self):
        files = dict(self._FILES)
        files["pkg/store.py"] = files["pkg/store.py"].replace(
            "def registered", "@app.on_event('startup')\ndef registered"
        )
        with TempProject(files) as root:
            out = _run_main(root, root / "pkg")
        test_only = {u["name"]: u for u in out["referenced_only_from_tests"]}
        self.assertEqual(set(test_only), {"Store.has_key", "percentiles"})
        self.assertTrue(test_only["Store.has_key"]["mentioned_in_docs"])
        self.assertFalse(test_only["percentiles"]["mentioned_in_docs"])
        self.assertEqual(out["summary"]["referenced_only_from_tests"], 2)
        self.assertEqual(
            out["summary"]["external_reference_scope_kinds"].get("tests"), "test"
        )
        # Not dead: a test still uses them.
        names = {u["name"] for u in out["unreferenced_symbols"]}
        self.assertNotIn("percentiles", names)
        self.assertNotIn("Store.has_key", names)

    def test_whole_project_review_classifies_test_files_too(self):
        with TempProject(self._FILES) as root:
            out = _run_main(root, root)
        test_only = {u["name"] for u in out["referenced_only_from_tests"]}
        self.assertIn("percentiles", test_only)
        self.assertNotIn("used", test_only)
        self.assertNotIn("Store.get", test_only)
        # Test-file symbols are never "production code used only by tests".
        self.assertNotIn("test_it", test_only)

    def test_production_reference_from_scripts_counts(self):
        files = dict(self._FILES)
        files["scripts/report.py"] = "from pkg.store import percentiles\npercentiles([])\n"
        with TempProject(files) as root:
            out = _run_main(root, root / "pkg")
        test_only = {u["name"] for u in out["referenced_only_from_tests"]}
        self.assertNotIn("percentiles", test_only)


class TestDecoratedTopLevelFunctions(unittest.TestCase):
    """A registering decorator is how a top-level function is reached."""

    FILES: ClassVar[dict[str, str]] = {
        "pkg/__init__.py": "",
        "pkg/render.py": (
            "from functools import singledispatch\n"
            "@singledispatch\n"
            "def render(value):\n"
            "    return str(value)\n"
            "@render.register(int)\n"
            "@render.register(float)\n"
            "def _render_number(value):\n"
            "    return format(value, '.2f')\n"
            "@staticmethod\n"
            "def _inert_but_dead():\n"
            "    return 1\n"
            "def _plain_dead():\n"
            "    return 2\n"
        ),
        "pkg/app.py": "from pkg.render import render\nprint(render(1.5))\n",
    }

    def test_singledispatch_overload_is_not_dead(self):
        with TempProject(self.FILES) as root:
            out = _run_main(root, root)
        names = {u["name"] for u in out["unreferenced_symbols"]}
        self.assertNotIn("_render_number", names)
        # `render` (@singledispatch) and the overload.
        self.assertEqual(
            out["summary"]["functions_excluded_by_reason"], {"decorated": 2}
        )

    def test_inert_decorator_and_plain_function_are_still_reported(self):
        with TempProject(self.FILES) as root:
            out = _run_main(root, root)
        names = {u["name"] for u in out["unreferenced_symbols"]}
        self.assertIn("_plain_dead", names)
        self.assertIn("_inert_but_dead", names)


class TestTemplateReferences(unittest.TestCase):
    """A Jinja template inside the scanned tree is a production reference."""

    FILES: ClassVar[dict[str, str]] = {
        "pkg/__init__.py": "",
        "pkg/run.py": (
            "class Run:\n"
            "    def passed(self):\n"
            "        return True\n"
            "    def failed(self):\n"
            "        return False\n"
            "    def summary(self):\n"
            "        return ''\n"
        ),
        "pkg/report.py": "from pkg.run import Run\nRun().summary()\n",
        "pkg/views/page.jinja2": (
            "<p>failed</p>\n{% if run.passed() %}<b>ok</b>{% endif %}\n"
        ),
        "tests/test_run.py": (
            "from pkg.run import Run\n"
            "def test_run():\n"
            "    assert Run().passed()\n"
            "    assert not Run().failed()\n"
        ),
    }

    def test_attribute_read_in_a_template_is_a_production_use(self):
        with TempProject(self.FILES) as root:
            out = _run_main(root, root / "pkg")
        test_only = {u["name"] for u in out["referenced_only_from_tests"]}
        self.assertNotIn("Run.passed", test_only)
        self.assertEqual(
            out["summary"]["template_files_scanned"], ["views/page.jinja2"]
        )

    def test_template_text_outside_blocks_is_not_a_reference(self):
        # "failed" appears in the HTML, but not inside {{ }} or {% %}.
        with TempProject(self.FILES) as root:
            out = _run_main(root, root / "pkg")
        test_only = {u["name"] for u in out["referenced_only_from_tests"]}
        self.assertIn("Run.failed", test_only)

if __name__ == "__main__":
    unittest.main()

