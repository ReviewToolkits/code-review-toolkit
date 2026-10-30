"""Tests for count_types.py."""

import json
import unittest
from typing import ClassVar

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


class TestPerFileCoverage(unittest.TestCase):
    """`files[].coverage` counts methods, matching the summary's denominator."""

    def test_methods_counted_in_file_coverage(self):
        with TempProject({
            "mod.py": (
                "def helper(x: int) -> int:\n"
                "    return x\n"
                "\n"
                "class Store:\n"
                "    def get(self, k):\n"
                "        return k\n"
                "\n"
                "    def put(self, k, v):\n"
                "        pass\n"
                "\n"
                "    def _evict(self) -> None:\n"
                "        pass\n"
            ),
        }) as root:
            result = mod.analyze_file(root / "mod.py", root)
        cov = result["coverage"]
        # `functions` stays module-level only; coverage covers methods too.
        self.assertEqual(len(result["functions"]), 1)
        self.assertEqual(cov["total_functions"], 4)
        self.assertEqual(cov["fully_annotated_functions"], 2)
        self.assertEqual(cov["annotation_coverage_all"], 50.0)
        self.assertEqual(cov["public_functions"], 3)
        self.assertEqual(cov["annotation_coverage_public"], 33.3)

    def test_file_coverage_sums_to_summary(self):
        with TempProject({
            "pkg/a.py": "class A:\n    def m(self) -> None:\n        pass\n",
            "pkg/b.py": "def f(x):\n    return x\n",
        }) as root:
            out = _run_main(root / "pkg")
        self.assertEqual(
            sum(f["coverage"]["total_functions"] for f in out["files"]),
            out["summary"]["total_functions"],
        )

    def test_empty_file_coverage_is_null_not_zero(self):
        with TempProject({"mod.py": "X = 1\n"}) as root:
            cov = mod.analyze_file(root / "mod.py", root)["coverage"]
        self.assertEqual(cov["total_functions"], 0)
        self.assertIsNone(cov["annotation_coverage_all"])
        self.assertIsNone(cov["annotation_coverage_public"])

    def test_unparseable_file_coverage_is_null(self):
        with TempProject({"mod.py": "def broken(:\n"}) as root:
            result = mod.analyze_file(root / "mod.py", root)
        self.assertIsNotNone(result["parse_error"])
        self.assertIsNone(result["coverage"])


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



def _analyze_tree(files: dict[str, str], scope: str = "pkg") -> dict:
    with TempProject(files) as root:
        return mod.analyze(str(root / scope))


class TestEnumMembersAndInstanceAttributes(unittest.TestCase):
    """Enum members are not unannotated attributes; `self.x` in __init__ is seen."""

    def _cls(self, source: str) -> dict:
        with TempProject({"mod.py": source}) as root:
            return mod.analyze_file(root / "mod.py", root)["classes"][0]

    def test_enum_members_are_not_unannotated_attributes(self):
        cls = self._cls(
            "from enum import Enum\nclass Color(Enum):\n    RED = 1\n    GREEN = 2\n"
        )
        self.assertEqual(cls["unannotated_attributes"], [])
        self.assertEqual(cls["enum_members"], ["RED", "GREEN"])

    def test_plain_class_constants_stay_unannotated(self):
        cls = self._cls("class C:\n    limit = 3\n    def f(self) -> None: ...\n")
        self.assertEqual(cls["unannotated_attributes"], ["limit"])
        self.assertNotIn("enum_members", cls)

    def test_init_attributes_and_where_their_type_comes_from(self):
        cls = self._cls(
            "class Box:\n"
            "    size: int\n"
            "    def __init__(self, name: str, size: int, raw) -> None:\n"
            "        self.index_map: dict[str, int] = {}\n"
            "        self.name = name\n"
            "        self.size = size\n"
            "        self.raw = raw\n"
            "        self.count = 0\n"
            "        other = Box\n"
            "        other.x = 1\n"
            "        local: int = 3\n"
        )
        got = {a["name"]: a["typed_via"] for a in cls["instance_attributes"]}
        self.assertEqual(
            got,
            {
                "index_map": "annotation",
                "name": "parameter",
                "size": "class_annotation",
                "raw": None,
                "count": None,
            },
        )
        by_name = {a["name"]: a for a in cls["instance_attributes"]}
        self.assertEqual(by_name["name"]["annotation"], "str")

    def test_attributes_outside_init_are_not_collected(self):
        cls = self._cls(
            "class C:\n    def reset(self) -> None:\n        self.state = 0\n"
        )
        self.assertEqual(cls["instance_attributes"], [])

    def test_any_in_an_instance_attribute_annotation_is_an_any_usage(self):
        with TempProject(
            {
                "mod.py": (
                    "from typing import Any\n"
                    "class C:\n"
                    "    def __init__(self) -> None:\n"
                    "        self.reg: dict[str, Any] = {}\n"
                )
            }
        ) as root:
            usages = mod.analyze_file(root / "mod.py", root)["any_usages"]
        self.assertEqual(
            [(u["kind"], u["attribute"], u["line"]) for u in usages],
            [("instance_attribute", "reg", 4)],
        )

    def test_summary_counts_classes_whose_attributes_are_typed(self):
        out = _analyze_tree(
            {
                "pkg/m.py": (
                    "from enum import Enum\n"
                    "class Color(Enum):\n"
                    "    RED = 1\n"
                    "class Typed:\n"
                    "    def __init__(self, n: int) -> None:\n"
                    "        self.n = n\n"
                    "class Untyped:\n"
                    "    def __init__(self, n) -> None:\n"
                    "        self.n = n\n"
                ),
            }
        )
        self.assertEqual(out["summary"]["classes_with_attributes"], 2)
        self.assertEqual(out["summary"]["classes_attributes_fully_typed"], 1)


class TestUntypedContainers(unittest.TestCase):
    """Found from the annotation nodes: `-> tuple` included, prose excluded."""

    def _found(self, source: str) -> list[tuple[int, str]]:
        with TempProject({"mod.py": source}) as root:
            result = mod.analyze_file(root / "mod.py", root)
        return [(u["line"], u["annotation"]) for u in result["untyped_containers"]]

    def test_bare_tuple_return_is_found(self):
        self.assertEqual(
            self._found("def split(self, text: str) -> tuple:\n    return ()\n"),
            [(1, "tuple")],
        )

    def test_parameter_variable_and_typing_alias_are_found(self):
        self.assertEqual(
            self._found(
                "from typing import List\n"
                "def f(a: dict, b: List) -> None:\n"
                "    c: set = set()\n"
            ),
            [(2, "dict"), (2, "List"), (3, "set")],
        )

    def test_parameterized_and_non_annotation_uses_are_not_found(self):
        self.assertEqual(
            self._found(
                "def f(a: list[int], b: dict[str, tuple[int, ...]]) -> None:\n"
                '    """Args:\n'
                "        a: list\n"
                '    """\n'
                "    x = {'kind': list}\n"
            ),
            [],
        )

    def test_top_level_list_carries_the_file(self):
        out = _analyze_tree({"pkg/m.py": "def f() -> list:\n    return []\n"})
        self.assertEqual(out["summary"]["untyped_containers"], 1)
        self.assertEqual(out["untyped_containers"][0]["file"], "pkg/m.py")
        self.assertEqual(out["untyped_containers_total"], 1)


class TestContainerCategories(unittest.TestCase):
    """Pseudo-enum constant classes and dict/list subclasses are inventoried."""

    def _type(self, source: str) -> str | None:
        with TempProject({"mod.py": source}) as root:
            return mod.analyze_file(root / "mod.py", root)["classes"][0][
                "container_type"
            ]

    def test_constants_class_is_a_pseudo_enum(self):
        self.assertEqual(
            self._type(
                "class Level:\n"
                '    """Levels."""\n'
                "    __slots__ = ()\n"
                '    """Attribute docstring."""\n'
                '    LOW = "low"\n'
                '    HIGH = "high"\n'
            ),
            "pseudo-enum",
        )

    def test_class_with_a_method_or_one_constant_is_not(self):
        self.assertIsNone(
            self._type("class C:\n    A = 1\n    B = 2\n    def f(self): ...\n")
        )
        self.assertIsNone(self._type("class C:\n    A = 1\n"))
        self.assertIsNone(self._type("class C:\n    a = 1\n    b = 2\n"))
        self.assertIsNone(self._type("class C(Base):\n    A = 1\n    B = 2\n"))

    def test_dict_and_list_subclasses(self):
        self.assertEqual(self._type("class S(dict):\n    pass\n"), "dict-subclass")
        self.assertEqual(self._type("class S(list[int]):\n    pass\n"), "list-subclass")

    def test_enum_still_wins_over_its_mixin(self):
        self.assertEqual(
            self._type("from enum import Enum\nclass S(str, Enum):\n    A = 'a'\n"),
            "Enum",
        )


class TestModuleCompleteness(unittest.TestCase):
    def test_modules_fully_annotated_is_counted(self):
        out = _analyze_tree(
            {
                "pkg/a.py": "def f(x: int) -> int:\n    return x\n",
                "pkg/b.py": "def g(x):\n    return x\n",
                "pkg/c.py": "X = 1\n",
            }
        )
        self.assertEqual(out["summary"]["modules_with_functions"], 2)
        self.assertEqual(out["summary"]["modules_fully_annotated"], 1)


class TestAnyThroughAliases(unittest.TestCase):
    """`body: Payload` with `Payload = dict[str, Any]` is an Any usage too."""

    _FILES: ClassVar[dict[str, str]] = {
        "pkg/__init__.py": "from .types import Meta\n",
        "pkg/types.py": (
            "from typing import Any, TypeAlias\n"
            "Meta = dict[str, Any]\n"
            "Rows: TypeAlias = list[Meta]\n"
            "Pair = tuple[int, int]\n"
            "MaybeInt = int | None\n"
            "value = CONFIG['key']\n"
            "FLAGS = READ | WRITE\n"
        ),
        "pkg/use.py": (
            "from pkg.types import Rows, Pair\n"
            "from . import Meta\n"
            "from . import types as t\n"
            "def f(m: Meta, p: Pair) -> Rows:\n"
            "    return []\n"
            "def g(m: 't.Meta') -> None:\n"
            "    pass\n"
            "class C:\n"
            "    meta: Meta\n"
        ),
    }

    def test_aliases_resolve_across_modules(self):
        out = _analyze_tree(self._FILES)
        entries = {
            (e["kind"], e.get("function") or e.get("attribute")): e
            for e in out["any_via_alias_usages"]
        }
        self.assertEqual(
            set(entries),
            {("function", "f"), ("function", "g"), ("attribute", "meta")},
        )
        f = entries[("function", "f")]
        self.assertEqual([d["param"] for d in f["details"]], ["m"])
        self.assertTrue(f["in_return"])
        self.assertEqual(f["return_aliases"], ["Rows"])
        self.assertEqual(out["summary"]["any_via_alias_sites"], 4)
        # Direct Any usages keep their meaning: none of these is direct.
        self.assertEqual(out["summary"]["total_any_usages"], 0)

    def test_alias_inventory_is_narrow(self):
        out = _analyze_tree(self._FILES)
        inventory = {a["name"]: a for a in out["alias_inventory"]}
        self.assertEqual(set(inventory), {"Meta", "Rows", "Pair", "MaybeInt"})
        self.assertTrue(inventory["Meta"]["contains_any"])
        self.assertTrue(inventory["Rows"]["contains_any"])
        self.assertTrue(inventory["Rows"]["explicit"])
        self.assertFalse(inventory["Pair"]["contains_any"])
        self.assertEqual(inventory["Meta"]["definition"], "dict[str, Any]")

    def test_direct_any_is_not_double_counted(self):
        out = _analyze_tree(
            {
                "pkg/m.py": (
                    "from typing import Any\n"
                    "Meta = dict[str, Any]\n"
                    "def f(m: Meta | Any) -> None:\n"
                    "    pass\n"
                )
            }
        )
        self.assertEqual(out["summary"]["total_any_usages"], 1)
        self.assertEqual(out["any_via_alias_usages"], [])


class TestNestedFunctions(unittest.TestCase):
    """Route handlers defined inside a factory are counted, separately."""

    _SRC = (
        "def build_router(app) -> None:\n"
        "    def list_items(limit: int) -> list[int]:\n"
        "        return []\n"
        "    def get_item(item_id):\n"
        "        def inner():\n"
        "            pass\n"
        "        return item_id\n"
        "class Factory:\n"
        "    def make(self) -> None:\n"
        "        def handler(x: int) -> int:\n"
        "            return x\n"
    )

    def test_nested_functions_are_listed_with_qualified_names(self):
        with TempProject({"mod.py": self._SRC}) as root:
            result = mod.analyze_file(root / "mod.py", root)
        self.assertEqual(
            [f["name"] for f in result["nested_functions"]],
            [
                "build_router.list_items",
                "build_router.get_item",
                "build_router.get_item.inner",
                "Factory.make.handler",
            ],
        )
        cov = result["coverage"]
        self.assertEqual(cov["total_functions"], 2)
        self.assertEqual(cov["nested_functions"], 4)
        self.assertEqual(cov["fully_annotated_nested"], 2)

    def test_summary_keeps_total_functions_and_adds_nested(self):
        out = _analyze_tree({"pkg/m.py": self._SRC})
        self.assertEqual(out["summary"]["total_functions"], 2)
        self.assertEqual(out["summary"]["nested_functions"], 4)
        self.assertEqual(out["summary"]["annotation_coverage_nested"], 50.0)

    def test_any_in_a_nested_function_is_marked(self):
        with TempProject(
            {
                "mod.py": (
                    "from typing import Any\n"
                    "def outer() -> None:\n"
                    "    def inner(x: Any) -> None:\n"
                    "        pass\n"
                )
            }
        ) as root:
            usages = mod.analyze_file(root / "mod.py", root)["any_usages"]
        self.assertEqual(
            [(u["function"], u.get("nested")) for u in usages],
            [("outer.inner", True)],
        )


class TestInconsistentParameters(unittest.TestCase):
    """A parameter name annotated at some definitions and bare at others."""

    def test_partly_annotated_name_is_reported(self):
        out = _analyze_tree(
            {
                "pkg/a.py": (
                    "def f(session: Session, n: int) -> None: ...\n"
                    "def g(session, n: int) -> None: ...\n"
                ),
                "pkg/b.py": (
                    "class C:\n"
                    "    def h(self, session, n: int) -> None: ...\n"
                    "def build() -> None:\n"
                    "    def route(session: Session) -> None: ...\n"
                ),
            }
        )
        (entry,) = out["inconsistent_parameters"]
        self.assertEqual(entry["param"], "session")
        self.assertEqual((entry["annotated"], entry["unannotated"]), (2, 2))
        self.assertEqual(entry["dominant_annotation"], "Session")
        self.assertTrue(entry["consistent"])
        self.assertEqual(
            sorted((s["file"], s["function"]) for s in entry["unannotated_sites"]),
            [("pkg/a.py", "g"), ("pkg/b.py", "C.h")],
        )
        self.assertEqual(out["summary"]["inconsistent_parameters"], 1)

    def test_self_and_uniformly_typed_or_bare_names_are_not_reported(self):
        out = _analyze_tree(
            {
                "pkg/a.py": (
                    "class C:\n"
                    "    def f(self, n: int) -> None: ...\n"
                    "    def g(self, n: int, raw) -> None: ...\n"
                    "def h(raw, *args, **kwargs): ...\n"
                    "def k(*args: int, **kwargs: str) -> None: ...\n"
                ),
            }
        )
        self.assertEqual(out["inconsistent_parameters"], [])


class TestUntypedBoundaries(unittest.TestCase):
    """Typed code passing values into a bare parameter of an in-scope function."""

    _FILES: ClassVar[dict[str, str]] = {
        "pkg/__init__.py": "",
        "pkg/util.py": (
            "def redact(message, keep: bool = False):\n    return message\n"
        ),
        "pkg/run.py": (
            "from pkg.util import redact\n"
            "from pkg import util\n"
            "class Node:\n"
            "    def handle(self, msg: Message) -> None:\n"
            "        redact(msg, keep=True)\n"
            "        self._post(msg)\n"
            "        util.redact(message=make())\n"
            "    def _post(self, value):\n"
            "        pass\n"
            "def untyped_caller(msg):\n"
            "    redact(msg)\n"
        ),
    }

    def test_typed_callers_into_bare_parameters_are_listed(self):
        out = _analyze_tree(self._FILES)
        got = {(b["callee"], b["param"]): b for b in out["untyped_boundaries"]}
        self.assertEqual(set(got), {("redact", "message"), ("Node._post", "value")})
        redact = got[("redact", "message")]
        self.assertEqual(redact["file"], "pkg/util.py")
        # Two calls from the typed method; the untyped caller's is not counted
        # (mypy does not check its body by default).
        self.assertEqual(redact["call_sites"], 2)
        self.assertEqual(redact["arg_annotations"], {"Message": 1})
        self.assertEqual(
            {s["caller"] for s in redact["sample_call_sites"]}, {"Node.handle"}
        )

    def test_annotated_parameter_is_not_a_boundary(self):
        out = _analyze_tree(
            {
                "pkg/m.py": (
                    "def helper(x: int) -> int:\n"
                    "    return x\n"
                    "def caller(y: int) -> int:\n"
                    "    return helper(y) + len([y])\n"
                )
            }
        )
        self.assertEqual(out["untyped_boundaries"], [])


if __name__ == "__main__":
    unittest.main()
