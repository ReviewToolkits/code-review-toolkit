"""Tests for count_types.py."""

import json
import unittest

from helpers import TempProject, import_script

mod = import_script("count_types")


class TestAnnotationCoverage(unittest.TestCase):
    """Test function annotation detection."""

    def _analyze(self, source: str) -> dict:
        with TempProject({"mod.py": source}) as root:
            return mod.analyze_file(root / "mod.py", root)

    def test_fully_annotated(self):
        result = self._analyze(
            "def add(a: int, b: int) -> int:\n"
            "    return a + b\n"
        )
        func = result["functions"][0]
        self.assertTrue(func["fully_annotated"])
        self.assertEqual(func["annotated_params"], 2)
        self.assertTrue(func["has_return_annotation"])

    def test_partially_annotated(self):
        result = self._analyze(
            "def add(a: int, b) -> int:\n"
            "    return a + b\n"
        )
        func = result["functions"][0]
        self.assertFalse(func["fully_annotated"])
        self.assertEqual(func["annotated_params"], 1)
        self.assertEqual(func["total_params"], 2)

    def test_no_annotations(self):
        result = self._analyze(
            "def add(a, b):\n"
            "    return a + b\n"
        )
        func = result["functions"][0]
        self.assertFalse(func["fully_annotated"])
        self.assertEqual(func["annotated_params"], 0)
        self.assertFalse(func["has_return_annotation"])

    def test_no_params_with_return(self):
        result = self._analyze(
            "def get_value() -> int:\n"
            "    return 42\n"
        )
        func = result["functions"][0]
        self.assertTrue(func["fully_annotated"])

    def test_self_excluded_from_count(self):
        result = self._analyze(
            "class C:\n"
            "    def method(self, x: int) -> None:\n"
            "        pass\n"
        )
        cls = result["classes"][0]
        method = cls["methods"][0]
        self.assertEqual(method["total_params"], 1)  # x only, not self
        self.assertTrue(method["fully_annotated"])

    def test_public_detection(self):
        result = self._analyze(
            "def public_func(): pass\n"
            "def _private_func(): pass\n"
        )
        funcs = {f["name"]: f["is_public"] for f in result["functions"]}
        self.assertTrue(funcs["public_func"])
        self.assertFalse(funcs["_private_func"])


class TestAnyDetection(unittest.TestCase):
    """Test detection of Any in annotations."""

    def _analyze(self, source: str) -> dict:
        with TempProject({"mod.py": source}) as root:
            return mod.analyze_file(root / "mod.py", root)

    def test_any_in_param(self):
        result = self._analyze(
            "from typing import Any\n"
            "\n"
            "def process(data: Any) -> None:\n"
            "    pass\n"
        )
        self.assertTrue(len(result["any_usages"]) > 0)

    def test_any_in_return(self):
        result = self._analyze(
            "from typing import Any\n"
            "\n"
            "def get_data() -> Any:\n"
            "    pass\n"
        )
        func = result["functions"][0]
        self.assertTrue(func["any_in_return"])

    def test_any_nested_in_dict(self):
        result = self._analyze(
            "from typing import Any\n"
            "\n"
            "def process(data: dict[str, Any]) -> None:\n"
            "    pass\n"
        )
        func = result["functions"][0]
        self.assertTrue(len(func["any_in_params"]) > 0)

    def test_no_any(self):
        result = self._analyze(
            "def add(a: int, b: int) -> int:\n"
            "    return a + b\n"
        )
        self.assertEqual(len(result["any_usages"]), 0)


class TestAnyUsageSchema(unittest.TestCase):
    """Every `any_usages[]` entry shares `kind`, `file` and `line`."""

    def _usages(self, source: str) -> list[dict]:
        with TempProject({"mod.py": source}) as root:
            return mod.analyze_file(root / "mod.py", root)["any_usages"]

    def test_function_method_and_attribute_share_keys(self):
        usages = self._usages(
            "from typing import Any\n"
            "\n"
            "def f(x: Any) -> None:\n"
            "    pass\n"
            "\n"
            "class C:\n"
            "    attr: Any\n"
            "\n"
            "    def m(self) -> Any:\n"
            "        pass\n"
        )
        by_kind = {u["kind"]: u for u in usages if u["kind"] != "function"}
        functions = [u for u in usages if u["kind"] == "function"]
        self.assertEqual(
            sorted((u["file"], u["line"]) for u in functions),
            [("mod.py", 3), ("mod.py", 9)],
        )
        attr = by_kind["class_attribute"]
        # The attribute's own line, not the class line.
        self.assertEqual((attr["file"], attr["line"]), ("mod.py", 7))
        self.assertEqual(attr["attribute"], "attr")
        # The legacy `location` key is kept for existing consumers.
        self.assertTrue(all("location" in u for u in usages))

    def test_return_only_any_has_empty_details(self):
        (usage,) = self._usages(
            "from typing import Any\n\ndef g() -> Any:\n    pass\n"
        )
        self.assertEqual(usage["details"], [])
        self.assertTrue(usage["in_return"])


class TestContainerTypeDetection(unittest.TestCase):
    """Test detection of dataclass, TypedDict, NamedTuple, etc."""

    def _analyze(self, source: str) -> dict:
        with TempProject({"mod.py": source}) as root:
            return mod.analyze_file(root / "mod.py", root)

    def test_dataclass(self):
        result = self._analyze(
            "from dataclasses import dataclass\n"
            "\n"
            "@dataclass\n"
            "class Point:\n"
            "    x: float\n"
            "    y: float\n"
        )
        cls = result["classes"][0]
        self.assertEqual(cls["container_type"], "dataclass")

    def test_frozen_dataclass(self):
        result = self._analyze(
            "from dataclasses import dataclass\n"
            "\n"
            "@dataclass(frozen=True)\n"
            "class Point:\n"
            "    x: float\n"
            "    y: float\n"
        )
        cls = result["classes"][0]
        self.assertEqual(cls["container_type"], "dataclass")
        self.assertTrue(cls["frozen"])

    def test_typed_dict(self):
        result = self._analyze(
            "from typing import TypedDict\n"
            "\n"
            "class Config(TypedDict):\n"
            "    name: str\n"
            "    value: int\n"
        )
        cls = result["classes"][0]
        self.assertEqual(cls["container_type"], "TypedDict")

    def test_named_tuple(self):
        result = self._analyze(
            "from typing import NamedTuple\n"
            "\n"
            "class Point(NamedTuple):\n"
            "    x: float\n"
            "    y: float\n"
        )
        cls = result["classes"][0]
        self.assertEqual(cls["container_type"], "NamedTuple")

    def test_protocol(self):
        result = self._analyze(
            "from typing import Protocol\n"
            "\n"
            "class Renderable(Protocol):\n"
            "    def render(self) -> str: ...\n"
        )
        cls = result["classes"][0]
        self.assertEqual(cls["container_type"], "Protocol")

    def test_enum(self):
        result = self._analyze(
            "from enum import Enum\n"
            "\n"
            "class Color(Enum):\n"
            "    RED = 1\n"
            "    GREEN = 2\n"
        )
        cls = result["classes"][0]
        self.assertEqual(cls["container_type"], "Enum")

    def test_plain_class(self):
        result = self._analyze(
            "class Regular:\n"
            "    def __init__(self):\n"
            "        self.x = 1\n"
        )
        cls = result["classes"][0]
        self.assertIsNone(cls["container_type"])


class TestClassAttributes(unittest.TestCase):

    def test_annotated_vs_unannotated(self):
        with TempProject({
            "mod.py": (
                "class Foo:\n"
                "    typed: int\n"
                "    also_typed: str = 'hello'\n"
                "    untyped = 42\n"
            ),
        }) as root:
            result = mod.analyze_file(root / "mod.py", root)
            cls = result["classes"][0]
            self.assertEqual(len(cls["annotated_attributes"]), 2)
            self.assertEqual(len(cls["unannotated_attributes"]), 1)
            self.assertEqual(cls["unannotated_attributes"][0], "untyped")


class TestTypeIgnoreCount(unittest.TestCase):

    def test_counts_type_ignore(self):
        with TempProject({
            "mod.py": (
                "x = foo()  # type: ignore\n"
                "y = bar()  # type: ignore[assignment]\n"
                "z = baz()  # this is fine\n"
            ),
        }) as root:
            result = mod.analyze_file(root / "mod.py", root)
            self.assertEqual(result["type_ignore_count"], 2)


class TestMaxFiles(unittest.TestCase):
    """Test --max-files caps file processing."""

    def test_max_files_caps_output(self):
        files = {
            f"pkg/mod{i}.py": f"def func{i}() -> int:\n    return {i}\n"
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
                "count_types.py", str(root),
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


def _run_main(root) -> dict:
    """Run count_types.main() on *root* and return its parsed JSON."""
    import io
    import sys

    old_stdout, old_argv = sys.stdout, sys.argv
    sys.stdout = io.StringIO()
    sys.argv = ["count_types.py", str(root)]
    try:
        mod.main()
        return json.loads(sys.stdout.getvalue())
    finally:
        sys.stdout, sys.argv = old_stdout, old_argv


class TestUnannotatedPublicFunctions(unittest.TestCase):
    """`unannotated_public_functions[]` entries must say which file they are in."""

    def test_same_name_in_two_files_is_distinguishable(self):
        with TempProject({
            "pkg/a.py": "def build(x):\n    return x\n",
            "pkg/b.py": "def build(x):\n    return x\n",
        }) as root:
            out = _run_main(root / "pkg")
        entries = out["unannotated_public_functions"]
        self.assertEqual(
            sorted((e["file"], e["name"]) for e in entries),
            [("pkg/a.py", "build"), ("pkg/b.py", "build")],
        )

    def test_method_entry_carries_its_file(self):
        with TempProject({
            "pkg/a.py": "class C:\n    def run(self, x):\n        return x\n",
        }) as root:
            out = _run_main(root / "pkg")
        self.assertEqual(
            out["unannotated_public_functions"],
            [{"file": "pkg/a.py", "name": "C.run", "line": 2}],
        )

    def test_annotated_function_not_listed(self):
        with TempProject({
            "pkg/a.py": "def build(x: int) -> int:\n    return x\n",
        }) as root:
            out = _run_main(root / "pkg")
        self.assertEqual(out["unannotated_public_functions"], [])


if __name__ == "__main__":
    unittest.main()
