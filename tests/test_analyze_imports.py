"""Tests for analyze_imports.py."""

import json
import unittest
from pathlib import Path

from helpers import TempProject, import_script

mod = import_script("analyze_imports")


class TestIsStdlib(unittest.TestCase):
    """Test stdlib detection."""

    def test_common_stdlib_modules(self):
        for name in (
            "os",
            "sys",
            "json",
            "pathlib",
            "ast",
            "unittest",
            "collections",
            "typing",
            "functools",
            "itertools",
        ):
            with self.subTest(name=name):
                self.assertTrue(mod._is_stdlib(name))

    def test_not_stdlib(self):
        for name in ("requests", "numpy", "yaml", "click", "flask"):
            with self.subTest(name=name):
                self.assertFalse(mod._is_stdlib(name))


class TestResolveRelativeImport(unittest.TestCase):
    """Relative-import resolution, checked against real interpreter semantics.

    `from .X import Y` imports X from the package CONTAINING the importing
    module; each additional dot strips one more level. These expectations were
    verified by building the package layout on disk and importing it, not by
    reading the implementation -- the previous versions of these tests asserted
    the implementation's off-by-one instead of the language's behaviour.
    """

    def _resolve(self, source_rel, level, module):
        root = Path("/project")
        source = root / source_rel
        return mod._resolve_relative_import(source, root, level, module)

    def test_level_1_with_module(self):
        # `from .core import X` in pkg/sub/file.py -> pkg.sub.core
        self.assertEqual(self._resolve("pkg/sub/file.py", 1, "core"), "pkg.sub.core")

    def test_level_1_no_module(self):
        # `from . import X` in pkg/sub/file.py -> pkg.sub
        self.assertEqual(self._resolve("pkg/sub/file.py", 1, None), "pkg.sub")

    def test_level_2(self):
        # `from ..utils import X` in pkg/sub/deep/file.py -> pkg.sub.utils
        self.assertEqual(
            self._resolve("pkg/sub/deep/file.py", 2, "utils"), "pkg.sub.utils"
        )

    def test_top_level_relative(self):
        # `from .sibling import X` in pkg/file.py -> pkg.sibling
        self.assertEqual(self._resolve("pkg/file.py", 1, "sibling"), "pkg.sibling")

    def test_level_exceeds_depth(self):
        # More dots than there are packages to strip.
        self.assertIsNone(self._resolve("pkg/file.py", 3, "something"))


class TestAnalyzeFile(unittest.TestCase):
    """Test single-file analysis."""

    def test_basic_imports(self):
        with TempProject(
            {
                "pkg/__init__.py": "",
                "pkg/core.py": (
                    "import os\n"
                    "import json\n"
                    "from pathlib import Path\n"
                    "import requests\n"
                    "from . import utils\n"
                ),
                "pkg/utils.py": "",
            }
        ) as root:
            result = mod.analyze_file(root / "pkg/core.py", root, {"pkg"})
            self.assertIsNone(result["parse_error"])
            imports = result["imports"]
            self.assertEqual(len(imports), 5)

            # Check categories.
            categories = {
                i["module"] or i.get("resolved_module", ""): i["category"]
                for i in imports
            }
            self.assertEqual(categories["os"], "stdlib")
            self.assertEqual(categories["json"], "stdlib")
            self.assertEqual(categories["pathlib"], "stdlib")
            self.assertEqual(categories["requests"], "external")

            # The relative import should be internal.
            relative_imports = [i for i in imports if i["is_relative"]]
            self.assertEqual(len(relative_imports), 1)
            self.assertEqual(relative_imports[0]["category"], "internal")

    def test_type_checking_detection(self):
        with TempProject(
            {
                "pkg/__init__.py": "",
                "pkg/core.py": (
                    "from __future__ import annotations\n"
                    "from typing import TYPE_CHECKING\n"
                    "\n"
                    "if TYPE_CHECKING:\n"
                    "    from pkg.models import SomeType\n"
                    "\n"
                    "import os\n"
                ),
            }
        ) as root:
            result = mod.analyze_file(root / "pkg/core.py", root, {"pkg"})
            imports = result["imports"]
            tc_imports = [i for i in imports if i["type_checking_only"]]
            non_tc = [i for i in imports if not i["type_checking_only"]]

            # "from pkg.models import SomeType" should be type-checking-only.
            self.assertEqual(len(tc_imports), 1)
            self.assertEqual(tc_imports[0]["module"], "pkg.models")

            # os and TYPE_CHECKING itself should not be type-checking-only.
            non_tc_modules = {i["module"] for i in non_tc}
            self.assertIn("os", non_tc_modules)

    def test_conditional_import_detection(self):
        with TempProject(
            {
                "pkg/__init__.py": "",
                "pkg/core.py": (
                    "try:\n"
                    "    import rapidjson as json_mod\n"
                    "except ImportError:\n"
                    "    import json as json_mod\n"
                ),
            }
        ) as root:
            result = mod.analyze_file(root / "pkg/core.py", root, {"pkg"})
            conditional = [i for i in result["imports"] if i["conditional"]]
            # The "try" branch import should be conditional.
            self.assertTrue(len(conditional) >= 1)

    def test_all_declaration(self):
        with TempProject(
            {
                "pkg/__init__.py": '__all__ = ["foo", "Bar"]\n',
            }
        ) as root:
            result = mod.analyze_file(root / "pkg/__init__.py", root, {"pkg"})
            self.assertEqual(result["all_declaration"], ["foo", "Bar"])
            self.assertTrue(result["is_init"])

    def test_syntax_error_handled(self):
        with TempProject(
            {
                "bad.py": "def broken(\n",
            }
        ) as root:
            result = mod.analyze_file(root / "bad.py", root, set())
            self.assertIsNotNone(result["parse_error"])
            self.assertEqual(result["imports"], [])


class TestDetectCycles(unittest.TestCase):
    """Test circular dependency detection."""

    def test_direct_cycle(self):
        graph = {
            "a.py": [
                {"target": "b", "type_checking_only": False, "conditional": False}
            ],
            "b.py": [
                {"target": "a", "type_checking_only": False, "conditional": False}
            ],
        }
        cycles = mod.detect_cycles(graph)
        self.assertEqual(len(cycles), 1)
        # Cycle should contain both files.
        cycle_set = set(cycles[0])
        self.assertEqual(cycle_set, {"a.py", "b.py"})

    def test_no_cycles(self):
        graph = {
            "a.py": [
                {"target": "b", "type_checking_only": False, "conditional": False}
            ],
            "b.py": [
                {"target": "c", "type_checking_only": False, "conditional": False}
            ],
        }
        cycles = mod.detect_cycles(graph)
        self.assertEqual(len(cycles), 0)

    def test_indirect_cycle(self):
        graph = {
            "a.py": [
                {"target": "b", "type_checking_only": False, "conditional": False}
            ],
            "b.py": [
                {"target": "c", "type_checking_only": False, "conditional": False}
            ],
            "c.py": [
                {"target": "a", "type_checking_only": False, "conditional": False}
            ],
        }
        cycles = mod.detect_cycles(graph)
        self.assertGreaterEqual(len(cycles), 1)
        # All three files should appear in the cycle.
        all_nodes = set()
        for cycle in cycles:
            all_nodes.update(cycle)
        self.assertIn("a.py", all_nodes)
        self.assertIn("b.py", all_nodes)
        self.assertIn("c.py", all_nodes)


class TestIdentifyProjectPackages(unittest.TestCase):
    """Test project package discovery."""

    def test_finds_packages_with_init(self):
        with TempProject(
            {
                "mypkg/__init__.py": "",
                "mypkg/core.py": "",
                "other/__init__.py": "",
            }
        ) as root:
            packages = mod.identify_project_packages(root)
            self.assertIn("mypkg", packages)
            self.assertIn("other", packages)

    def test_ignores_test_dirs(self):
        with TempProject(
            {
                "mypkg/__init__.py": "",
                "tests/__init__.py": "",
            }
        ) as root:
            packages = mod.identify_project_packages(root)
            self.assertIn("mypkg", packages)
            self.assertNotIn("tests", packages)

    def test_src_layout(self):
        with TempProject(
            {
                "src/mypkg/__init__.py": "",
                "src/mypkg/core.py": "",
            }
        ) as root:
            packages = mod.identify_project_packages(root)
            self.assertIn("mypkg", packages)


class TestEndToEnd(unittest.TestCase):
    """Integration test: full pipeline on a small project."""

    def test_small_project(self):
        with TempProject(
            {
                "mypkg/__init__.py": "from .core import main\n",
                "mypkg/core.py": (
                    "import os\n"
                    "from .utils import helper\n"
                    "\n"
                    "def main():\n"
                    "    return helper(os.getcwd())\n"
                ),
                "mypkg/utils.py": ("def helper(path):\n    return str(path)\n"),
                "tests/test_core.py": (
                    "import unittest\n"
                    "from mypkg.core import main\n"
                    "\n"
                    "class TestCore(unittest.TestCase):\n"
                    "    def test_main(self):\n"
                    "        self.assertIsNotNone(main())\n"
                ),
            }
        ) as root:
            files = sorted(mod.discover_python_files(root))
            project_packages = mod.identify_project_packages(root)
            analyses = [mod.analyze_file(f, root, project_packages) for f in files]

            self.assertEqual(len(analyses), 4)

            graph = mod.build_internal_graph(analyses)
            # mypkg/core.py should depend on mypkg/utils.
            core_edges = graph.get("mypkg/core.py", [])
            targets = {e["target"] for e in core_edges}
            self.assertTrue(
                any("utils" in t for t in targets),
                f"Expected utils dependency, got {targets}",
            )

            # No cycles in this project.
            cycles = mod.detect_cycles(graph)
            self.assertEqual(len(cycles), 0)


class TestMaxFiles(unittest.TestCase):
    """Test --max-files caps file processing."""

    def test_max_files_caps_output(self):
        files = {f"pkg/mod{i}.py": f"x{i} = {i}\n" for i in range(10)}
        files["pkg/__init__.py"] = ""
        with TempProject(files) as root:
            import io
            import sys

            old_stdout = sys.stdout
            sys.stdout = io.StringIO()
            old_argv = sys.argv
            sys.argv = [
                "analyze_imports.py",
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


def _graph(root):
    """Build the internal import graph for a temp project."""
    files = sorted(mod.discover_python_files(root))
    packages = mod.identify_project_packages(root)
    return mod.build_internal_graph(
        [mod.analyze_file(f, root, packages) for f in files]
    )


class TestRelativeImportResolution(unittest.TestCase):
    """`from .X import Y` must resolve to the CONTAINING package, not its parent.

    Getting this wrong produces module paths that match no file, which silently
    zeroes every fan_in metric for any project using relative imports.
    """

    def test_single_dot_resolves_within_package(self):
        files = {
            "pkg/__init__.py": "",
            "pkg/a.py": "from .b import thing\n",
            "pkg/b.py": "thing = 1\n",
        }
        with TempProject(files) as root:
            graph = _graph(root)
        targets = [e["target"] for e in graph.get("pkg/a.py", [])]
        self.assertIn("pkg.b", targets)

    def test_double_dot_resolves_to_parent(self):
        files = {
            "pkg/__init__.py": "",
            "pkg/sub/__init__.py": "",
            "pkg/sub/a.py": "from ..b import thing\n",
            "pkg/b.py": "thing = 1\n",
        }
        with TempProject(files) as root:
            graph = _graph(root)
        targets = [e["target"] for e in graph.get("pkg/sub/a.py", [])]
        self.assertIn("pkg.b", targets)

    def test_two_modules_target_the_same_sibling(self):
        files = {
            "pkg/__init__.py": "",
            "pkg/a.py": "from .shared import thing\n",
            "pkg/c.py": "from .shared import thing\n",
            "pkg/shared.py": "thing = 1\n",
        }
        with TempProject(files) as root:
            graph = _graph(root)
        targets = [e["target"] for edges in graph.values() for e in edges]
        self.assertEqual(targets.count("pkg.shared"), 2)

    def test_no_phantom_cycle_through_package_init(self):
        # An import-free sibling must not resolve to the package __init__.
        files = {
            "mypkg/__init__.py": "from .core import main\n",
            "mypkg/core.py": "from .utils import helper\n",
            "mypkg/utils.py": "def helper():\n    return 1\n",
        }
        with TempProject(files) as root:
            graph = _graph(root)
        self.assertEqual(mod.detect_cycles(graph), [])


class TestFanInResolution(unittest.TestCase):
    """Found by the coverage.py benchmark: `coverage/__init__.py` reported a
    fan-in of 209 in a 44-file package where the true figure is 24. A prefix
    match made a package's __init__ absorb every import of every module inside
    it -- the same fallback detect_cycles had already dropped, never propagated."""

    FILES = ["pkg/__init__.py", "pkg/a.py", "pkg/b.py", "pkg/c.py"]

    def test_package_init_does_not_absorb_every_submodule_import(self):
        graph = {
            "pkg/__init__.py": [{"target": "pkg.a"}],
            "pkg/a.py": [{"target": "pkg.b"}],
            "pkg/c.py": [{"target": "pkg.b"}],
        }
        fan_in = mod.compute_metrics(graph, self.FILES)["fan_in"]
        self.assertEqual(fan_in["pkg/b.py"], 2)
        self.assertEqual(fan_in["pkg/__init__.py"], 0)

    def test_bare_package_import_counts_against_init(self):
        graph = {"pkg/a.py": [{"target": "pkg"}]}
        fan_in = mod.compute_metrics(graph, self.FILES)["fan_in"]
        self.assertEqual(fan_in["pkg/__init__.py"], 1)

    def test_unresolvable_target_is_not_attributed(self):
        graph = {"pkg/a.py": [{"target": "third_party.thing"}]}
        fan_in = mod.compute_metrics(graph, self.FILES)["fan_in"]
        self.assertEqual(sum(fan_in.values()), 0)


class TestCyclePathShape(unittest.TestCase):
    """Found by the coverage.py benchmark: ALL 26 reported cycles had a
    duplicated node, because the reconstruction seeded the list with the closing
    node and then walked back onto it."""

    def test_two_cycle_has_exactly_two_nodes(self):
        graph = {
            "a.py": [{"target": "b", "type_checking_only": False}],
            "b.py": [{"target": "a", "type_checking_only": False}],
        }
        cycles = mod.detect_cycles(graph)
        self.assertEqual(len(cycles), 1)
        self.assertEqual(len(cycles[0]), 2)
        self.assertEqual(len(cycles[0]), len(set(cycles[0])))

    def test_three_cycle_has_no_duplicate(self):
        graph = {
            "a.py": [{"target": "b", "type_checking_only": False}],
            "b.py": [{"target": "c", "type_checking_only": False}],
            "c.py": [{"target": "a", "type_checking_only": False}],
        }
        for cycle in mod.detect_cycles(graph):
            self.assertEqual(len(cycle), len(set(cycle)), cycle)

    def test_type_checking_only_cycle_is_not_a_cycle(self):
        # A TYPE_CHECKING import does not run -- avoiding the cycle is exactly
        # why the guard is there. coverage.py had 20 such edges.
        graph = {
            "a.py": [{"target": "b", "type_checking_only": False}],
            "b.py": [{"target": "a", "type_checking_only": True}],
        }
        self.assertEqual(mod.detect_cycles(graph), [])
        self.assertEqual(len(mod.detect_cycles(graph, include_type_checking=True)), 1)


class TestSubmoduleVsBoundName(unittest.TestCase):
    """`from pkg import submodule` binds the SUBMODULE, so the dependency is on
    that file -- not on pkg/__init__.py. Attributing it to the package
    manufactured a cycle through the facade: 16 of the 20 cycles first reported
    for coverage.py were this one idiom (`from coverage import env`)."""

    INDEX = {"pkg": "pkg/__init__.py", "pkg.env": "pkg/env.py"}

    def test_submodule_import_resolves_to_the_submodule(self):
        edge = {"target": "pkg", "names": [{"name": "env"}]}
        self.assertEqual(mod.resolve_edge_targets(edge, self.INDEX), {"pkg/env.py"})

    def test_bound_name_import_resolves_to_the_package(self):
        # `Coverage` is bound in __init__.py, so this edge IS on the package
        # and is order-sensitive.
        edge = {"target": "pkg", "names": [{"name": "Coverage"}]}
        self.assertEqual(
            mod.resolve_edge_targets(edge, self.INDEX), {"pkg/__init__.py"}
        )

    def test_mixed_import_resolves_to_both(self):
        edge = {"target": "pkg", "names": [{"name": "env"}, {"name": "Coverage"}]}
        self.assertEqual(
            mod.resolve_edge_targets(edge, self.INDEX),
            {"pkg/env.py", "pkg/__init__.py"},
        )

    def test_bare_package_import_resolves_to_the_package(self):
        edge = {"target": "pkg", "names": []}
        self.assertEqual(
            mod.resolve_edge_targets(edge, self.INDEX), {"pkg/__init__.py"}
        )

    def test_leaf_module_is_indexed_even_with_no_imports_of_its_own(self):
        # env.py has fan-out 0, so it is absent from the graph KEYS. Indexing
        # from those alone left `pkg.env` unresolvable and the bare-package
        # fallback then blamed the facade -- the same root cause as the prefix
        # fallback removed earlier: an incomplete index, not a bad matching rule.
        graph = {"pkg/a.py": [{"target": "pkg", "names": [{"name": "env"}]}]}
        all_files = ["pkg/__init__.py", "pkg/a.py", "pkg/env.py"]
        self.assertEqual(mod.detect_cycles(graph, all_files=all_files), [])


if __name__ == "__main__":
    unittest.main()
