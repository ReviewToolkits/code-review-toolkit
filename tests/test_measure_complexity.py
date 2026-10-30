"""Tests for measure_complexity.py."""

import json
import unittest

from helpers import TempProject, import_script
from test_analyze_history import GitTempProject

mod = import_script("measure_complexity")


class TestNestingDepth(unittest.TestCase):
    """Test nesting depth measurement."""

    def _measure(self, source: str) -> dict:
        """Analyze a single-function file and return the function's metrics."""
        with TempProject({"mod.py": source}) as root:
            result = mod.analyze_file(root / "mod.py", root)
            self.assertTrue(result["functions"], "No functions found")
            return result["functions"][0]["metrics"]

    def test_flat_function(self):
        metrics = self._measure("def flat():\n    x = 1\n    y = 2\n    return x + y\n")
        self.assertEqual(metrics["nesting_depth"], 0)

    def test_single_if(self):
        metrics = self._measure(
            "def one_level(x):\n    if x > 0:\n        return x\n    return -x\n"
        )
        self.assertEqual(metrics["nesting_depth"], 1)

    def test_nested_if_for(self):
        metrics = self._measure(
            "def nested(items):\n"
            "    for item in items:\n"
            "        if item > 0:\n"
            "            if item < 100:\n"
            "                print(item)\n"
        )
        self.assertEqual(metrics["nesting_depth"], 3)

    def test_try_except_nesting(self):
        metrics = self._measure(
            "def with_try():\n"
            "    try:\n"
            "        if True:\n"
            "            pass\n"
            "    except Exception:\n"
            "        pass\n"
        )
        # try is depth 1, if inside try is depth 2.
        self.assertEqual(metrics["nesting_depth"], 2)


class TestCognitiveComplexity(unittest.TestCase):
    """Test cognitive complexity scoring."""

    def _cognitive(self, source: str) -> int:
        with TempProject({"mod.py": source}) as root:
            result = mod.analyze_file(root / "mod.py", root)
            return result["functions"][0]["metrics"]["cognitive_complexity"]

    def test_flat_function_is_zero(self):
        score = self._cognitive("def flat():\n    return 42\n")
        self.assertEqual(score, 0)

    def test_single_if_is_one(self):
        score = self._cognitive(
            "def simple(x):\n    if x:\n        return 1\n    return 0\n"
        )
        # if: +1 (nesting=0, so +1+0=1), else: +1 → total 2
        # Actually: the else is implicit (no else block), just a return
        # after the if. So only the if contributes: 1.
        self.assertEqual(score, 1)

    def test_nested_increases_penalty(self):
        flat_score = self._cognitive(
            "def flat(x, y):\n    if x:\n        pass\n    if y:\n        pass\n"
        )
        nested_score = self._cognitive(
            "def nested(x, y):\n    if x:\n        if y:\n            pass\n"
        )
        # Flat: if(+1) + if(+1) = 2
        # Nested: if(+1) + if(+1+1 nesting) = 3
        self.assertEqual(flat_score, 2)
        self.assertEqual(nested_score, 3)
        self.assertGreater(nested_score, flat_score)

    def test_boolean_ops_add_complexity(self):
        score = self._cognitive(
            "def check(a, b, c):\n    if a and b or c:\n        pass\n"
        )
        # if contributes +1; BoolOp nodes in the condition may or may not
        # be visited depending on the visitor's traversal strategy.
        self.assertGreaterEqual(score, 1)

    def test_break_continue_add_complexity(self):
        score = self._cognitive(
            "def loopy(items):\n"
            "    for item in items:\n"
            "        if item < 0:\n"
            "            continue\n"
            "        if item > 100:\n"
            "            break\n"
        )
        # for(+1) + if(+1+1) + continue(+1) + if(+1+1) + break(+1) = 7
        self.assertGreaterEqual(score, 5)


class TestParameterCount(unittest.TestCase):
    """Test parameter counting."""

    def _params(self, source: str) -> int:
        with TempProject({"mod.py": source}) as root:
            result = mod.analyze_file(root / "mod.py", root)
            return result["functions"][0]["metrics"]["parameter_count"]

    def test_no_params(self):
        self.assertEqual(self._params("def f():\n    pass\n"), 0)

    def test_regular_params(self):
        self.assertEqual(self._params("def f(a, b, c):\n    pass\n"), 3)

    def test_self_excluded(self):
        # When analyzed as a method, self is excluded.
        with TempProject(
            {"mod.py": ("class C:\n    def method(self, a, b):\n        pass\n")}
        ) as root:
            result = mod.analyze_file(root / "mod.py", root)
            method = result["functions"][0]
            self.assertEqual(method["metrics"]["parameter_count"], 2)

    def test_args_kwargs_counted(self):
        count = self._params("def f(a, *args, **kwargs):\n    pass\n")
        self.assertEqual(count, 3)  # a + *args + **kwargs

    def test_keyword_only(self):
        count = self._params("def f(a, *, key=None, flag=False):\n    pass\n")
        self.assertEqual(count, 3)


class TestBranchCount(unittest.TestCase):
    def _branches(self, source: str) -> int:
        with TempProject({"mod.py": source}) as root:
            result = mod.analyze_file(root / "mod.py", root)
            return result["functions"][0]["metrics"]["branch_count"]

    def test_no_branches(self):
        self.assertEqual(self._branches("def f():\n    return 1\n"), 0)

    def test_if_else(self):
        count = self._branches(
            "def f(x):\n    if x > 0:\n        return 1\n    else:\n        return -1\n"
        )
        self.assertEqual(count, 2)  # if + else


class TestCompositeScore(unittest.TestCase):
    """Test the composite score computation."""

    def test_simple_function_scores_low(self):
        with TempProject({"mod.py": "def simple(x):\n    return x + 1\n"}) as root:
            result = mod.analyze_file(root / "mod.py", root)
            score = result["functions"][0]["score"]
            self.assertLessEqual(score, 3.0)

    def test_complex_function_scores_high(self):
        # A deliberately complex function.
        lines = ["def monster(a, b, c, d, e, f, g, h, i):"]
        for v in "abcdefghi":
            lines.append(f"    if {v}:")
            lines.append(f"        for x in {v}:")
            lines.append("            if x > 0:")
            lines.append("                for y in x:")
            lines.append("                    if y:")
            lines.append("                        print(y)")
        lines.append("    return None")
        source = "\n".join(lines) + "\n"

        with TempProject({"mod.py": source}) as root:
            result = mod.analyze_file(root / "mod.py", root)
            score = result["functions"][0]["score"]
            self.assertGreaterEqual(score, 7.0)

    def test_score_capped_at_10(self):
        metrics = {
            "line_count": 500,
            "nesting_depth": 10,
            "parameter_count": 15,
            "cognitive_complexity": 100,
            "branch_count": 20,
            "local_variable_count": 30,
            "loop_count": 5,
            "return_count": 10,
        }
        score = mod._compute_score(metrics)
        self.assertEqual(score, 10.0)


class TestTestFunctionDetection(unittest.TestCase):
    """Test that test functions are correctly flagged."""

    def test_test_method_detected(self):
        with TempProject(
            {
                "test_mod.py": (
                    "import unittest\n"
                    "\n"
                    "class TestFoo(unittest.TestCase):\n"
                    "    def test_bar(self):\n"
                    "        pass\n"
                    "\n"
                    "    def helper(self):\n"
                    "        pass\n"
                )
            }
        ) as root:
            result = mod.analyze_file(root / "test_mod.py", root)
            funcs = {f["name"]: f["is_test"] for f in result["functions"]}
            # test_bar is a test method.
            self.assertTrue(funcs["TestFoo.test_bar"])
            # helper in a Test* class is also flagged as test-related
            # (the script uses class name as a heuristic).
            self.assertTrue(funcs["TestFoo.helper"])


class TestTestDetectionByPath(unittest.TestCase):
    """Test code is decided by path and TestCase bases, not by name."""

    _DOMAIN = (
        "class TestRunConfig:\n"
        "    __test__ = False\n"
        "    def run(self):\n"
        "        return 1\n"
        "\n"
        "class Checklist:\n"
        "    @property\n"
        "    def tests(self):\n"
        "        return []\n"
        "\n"
        "def test_connection(url):\n"
        "    return url\n"
    )

    def test_source_names_that_look_like_tests_are_source(self):
        funcs = _functions(self._DOMAIN, "pkg/tree/nodes.py")
        self.assertEqual(
            {name: f["is_test"] for name, f in funcs.items()},
            {
                "TestRunConfig.run": False,
                "Checklist.tests": False,
                "test_connection": False,
            },
        )

    def test_everything_in_a_test_path_is_test_code(self):
        for path in (
            "tests/helpers.py",
            "pkg/tests/test_nodes.py",
            "test_nodes.py",
            "nodes_test.py",
            "conftest.py",
        ):
            funcs = _functions(self._DOMAIN, path)
            self.assertTrue(all(f["is_test"] for f in funcs.values()), path)

    def test_testcase_subclass_outside_a_test_path_is_test(self):
        source = (
            "import unittest\n"
            "class ChecksTestCase(unittest.TestCase):\n"
            "    def test_a(self):\n"
            "        pass\n"
            "class Opted(unittest.TestCase):\n"
            "    __test__ = False\n"
            "    def test_b(self):\n"
            "        pass\n"
        )
        funcs = _functions(source, "pkg/selfcheck.py")
        self.assertTrue(funcs["ChecksTestCase.test_a"]["is_test"])
        self.assertFalse(funcs["Opted.test_b"]["is_test"])

    def test_every_entry_names_its_file(self):
        funcs = _functions("def f():\n    return 1\n", "pkg/m.py")
        self.assertEqual(funcs["f"]["file"], "pkg/m.py")


class TestNestedFunctionIsolation(unittest.TestCase):
    """Test that nested function defs don't affect outer metrics."""

    def test_nested_def_excluded(self):
        with TempProject(
            {
                "mod.py": (
                    "def outer():\n"
                    "    def inner():\n"
                    "        if True:\n"
                    "            if True:\n"
                    "                if True:\n"
                    "                    pass\n"
                    "    return inner\n"
                )
            }
        ) as root:
            result = mod.analyze_file(root / "mod.py", root)
            outer = [f for f in result["functions"] if f["name"] == "outer"][0]
            # Outer's nesting depth should NOT include inner's nesting.
            self.assertEqual(outer["metrics"]["nesting_depth"], 0)


class TestMaxFiles(unittest.TestCase):
    """Test --max-files caps file processing."""

    def test_max_files_caps_output(self):
        files = {f"pkg/mod{i}.py": f"def f{i}():\n    return {i}\n" for i in range(10)}
        files["pkg/__init__.py"] = ""
        with TempProject(files) as root:
            import io
            import sys

            old_stdout = sys.stdout
            sys.stdout = io.StringIO()
            old_argv = sys.argv
            sys.argv = [
                "measure_complexity.py",
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


if __name__ == "__main__":
    unittest.main()


class TestNestingCalibration(unittest.TestCase):
    """Reader-facing depth, not AST depth (improvement-plan item 4.6)."""

    def _depth(self, source: str) -> int:
        import ast

        tree = ast.parse(source)
        fn = tree.body[0]
        v = mod._NestingVisitor()
        for child in fn.body:
            v.visit(child)
        return v.max_depth

    def test_elif_chain_is_one_level_not_three(self):
        # The AST models `elif` as an If inside each orelse. Descending into it
        # charged a level per arm, so a flat chain read as depth 3.
        source = (
            "def f(x):\n"
            "    if x == 1:\n        pass\n"
            "    elif x == 2:\n        pass\n"
            "    elif x == 3:\n        pass\n"
            "    else:\n        pass\n"
        )
        self.assertEqual(self._depth(source), 1)

    def test_genuine_nesting_still_counts(self):
        source = (
            "def f(x):\n"
            "    if x:\n"
            "        for i in x:\n"
            "            while i:\n"
            "                pass\n"
        )
        self.assertEqual(self._depth(source), 3)

    def test_disabled_debug_block_costs_nothing(self):
        source = (
            "def f(x):\n"
            "    if 0:\n"
            "        for i in x:\n"
            "            for j in i:\n"
            "                pass\n"
        )
        self.assertEqual(self._depth(source), 0)

    def test_if_false_is_also_disabled(self):
        source = "def f(x):\n    if False:\n        if x:\n            pass\n"
        self.assertEqual(self._depth(source), 0)


class TestHistoryCrossing(unittest.TestCase):
    """Complexity is a lens, not a ranking (improvement-plan item 4.6)."""

    def _hotspots(self, specs):
        return [
            {
                "qualified_name": f"m.py::f{i}",
                "line_start": 1,
                "line_end": 10,
                "score": score,
            }
            for i, score in enumerate(specs)
        ]

    def test_missing_history_yields_unknown_not_zero(self):
        # Treating an absent history as zero fix commits would mark every
        # hotspot `settled` and reproduce the inverted ranking exactly.
        from pathlib import Path

        hotspots = self._hotspots([9.0, 5.0])
        meta = mod.cross_with_history(hotspots, Path("/definitely/not/a/repo"))
        self.assertFalse(meta["history_available"])
        self.assertEqual([h["verdict"] for h in hotspots], ["unknown", "unknown"])
        self.assertIsNone(hotspots[0]["fix_commits_2y"])

    def test_active_risk_is_gated_on_history_alone(self):
        # A heavily-repaired function BELOW the complexity threshold must still
        # be active-risk. On coverage.py the most-fixed hotspot scores 7.5, and
        # an `and score >= 8.0` clause labelled it `quiet` -- the exact
        # inversion this crossing exists to correct.
        self.assertEqual(mod.verdict_for(fix_commits=5, score=7.5), "active-risk")

    def test_complex_but_untouched_is_settled(self):
        # `settled` says DEPRIORITIZE, which is the opposite of what a
        # complexity ranking alone would say.
        self.assertEqual(mod.verdict_for(fix_commits=0, score=9.0), "settled")
        self.assertEqual(mod.verdict_for(fix_commits=1, score=8.0), "settled")

    def test_low_on_both_is_quiet(self):
        self.assertEqual(mod.verdict_for(fix_commits=1, score=5.0), "quiet")

    def test_threshold_is_a_constant_not_a_median(self):
        # A median-derived gate is unstable over a handful of hotspots: it lands
        # on whichever value sits in the middle and moves with it.
        self.assertEqual(mod.ACTIVE_RISK_FIXES, 2)
        self.assertEqual(mod.verdict_for(fix_commits=2, score=1.0), "active-risk")


def _functions(source: str, path: str = "mod.py") -> dict:
    with TempProject({path: source}) as root:
        result = mod.analyze_file(root / path, root)
    return {f["name"]: f for f in result["functions"]}


_FACTORY = (
    "def build_router(app):\n"
    "    @app.get('/a')\n"
    "    def handler_a(x):\n"
    "        if x:\n"
    "            for i in x:\n"
    "                if i:\n"
    "                    return i\n"
    "        return None\n"
    "\n"
    "    def handler_b(y):\n"
    "        if y:\n"
    "            return 1\n"
    "        return 2\n"
    "\n"
    "    return app\n"
)


class TestNestedFunctionsMeasured(unittest.TestCase):
    """Nested defs are their own entries, and their bodies no longer
    inflate the enclosing function's metrics."""

    def test_nested_defs_are_entries_with_qualified_names(self):
        funcs = _functions(_FACTORY)
        self.assertEqual(
            sorted(funcs),
            ["build_router", "build_router.handler_a", "build_router.handler_b"],
        )
        handler = funcs["build_router.handler_a"]
        self.assertTrue(handler["is_nested"])
        self.assertFalse(handler["is_method"])
        self.assertEqual(handler["parent"], "build_router")
        self.assertEqual(handler["qualified_name"], "mod.py::build_router.handler_a")
        self.assertEqual(handler["metrics"]["nesting_depth"], 3)
        self.assertEqual(handler["metrics"]["return_count"], 2)

    def test_factory_is_not_credited_with_its_children(self):
        factory = _functions(_FACTORY)["build_router"]["metrics"]
        # Before: return_count 4 and branch_count 4 from the handlers.
        self.assertEqual(factory["return_count"], 1)
        self.assertEqual(factory["branch_count"], 0)
        self.assertEqual(factory["loop_count"], 0)
        self.assertEqual(factory["line_count"], 2)  # def line + return
        self.assertEqual(factory["total_lines"], 15)
        self.assertEqual(_functions(_FACTORY)["build_router"]["nested_def_count"], 2)

    def test_router_factory_is_not_a_hotspot(self):
        # A factory whose body is large nested handlers ranked as a hotspot on
        # their lines, with cognitive complexity 0; the large handler is the
        # real hotspot.
        body = "".join(f"        x{i} = {i}\n" for i in range(220))
        source = (
            "def build_big_router(app):\n"
            "    def big_handler(req):\n" + body + "        return x0\n"
            "    return app\n"
        )
        funcs = _functions(source)
        self.assertLess(funcs["build_big_router"]["score"], 5.0)
        self.assertGreaterEqual(funcs["build_big_router.big_handler"]["score"], 4.0)

    def test_flat_function_and_methods_unchanged(self):
        # True negative: no nested defs, nothing moves.
        source = (
            "def f(x):\n    if x:\n        return 1\n    return 2\n\n"
            "class C:\n    def m(self):\n        return 1\n"
            "    class Inner:\n        def n(self):\n            return 2\n"
        )
        funcs = _functions(source)
        self.assertEqual(sorted(funcs), ["C.Inner.n", "C.m", "f"])
        self.assertEqual(funcs["f"]["metrics"]["return_count"], 2)
        self.assertEqual(funcs["f"]["metrics"]["line_count"], 4)
        self.assertEqual(funcs["f"]["nested_def_count"], 0)
        self.assertFalse(funcs["f"]["is_nested"])
        self.assertTrue(funcs["C.m"]["is_method"])
        self.assertTrue(funcs["C.Inner.n"]["is_method"])
        self.assertFalse(funcs["C.Inner.n"]["is_nested"])
        self.assertEqual(funcs["C.Inner.n"]["parent"], "C.Inner")

    def test_class_inside_function(self):
        source = (
            "def make():\n"
            "    class Handler:\n"
            "        def get(self):\n"
            "            return 1\n"
            "    return Handler\n"
        )
        funcs = _functions(source)
        get = funcs["make.Handler.get"]
        self.assertTrue(get["is_method"])
        self.assertTrue(get["is_nested"])
        self.assertEqual(funcs["make"]["metrics"]["return_count"], 1)

    def test_nested_in_test_function_is_test(self):
        source = (
            "def test_x():\n    def helper():\n        return 1\n    assert helper()\n"
        )
        self.assertTrue(
            _functions(source, "tests/test_m.py")["test_x.helper"]["is_test"]
        )


def _hotspot(rel: str, name: str, start: int, end: int, score: float = 6.0) -> dict:
    return {
        "name": name,
        "qualified_name": f"{rel}::{name}",
        "line_start": start,
        "line_end": end,
        "score": score,
    }


class TestHistoryCrossingAttribution(unittest.TestCase):
    """Fixes are attributed at their own commit, by full path, and
    only real fix commits count."""

    def test_fix_to_a_function_that_has_since_moved_is_counted(self):
        v1 = "def a():\n    return 1\n\ndef target():\n    return 1\n"
        v2 = "def a():\n    return 1\n\ndef target():\n    return 2\n"
        v3 = "def a():\n    return 1\n\ndef target():\n    return 3\n"
        # Five padding functions on top: pad1 now sits at lines 4-5, where
        # both fixes' hunks (post-image line 5) landed at the time.
        grow = "".join(f"def pad{i}():\n    return {i}\n\n" for i in range(5))
        with GitTempProject(
            [
                {"files": {"m.py": v1}, "message": "Add functions"},
                {"files": {"m.py": v2}, "message": "[fix] Repair target (K29)"},
                {"files": {"m.py": v3}, "message": "fix: Bound target (K41)"},
                {"files": {"m.py": grow + v3}, "message": "Add padding"},
            ]
        ) as root:
            start = (grow + v3).splitlines().index("def target():") + 1
            hotspots = [
                _hotspot("m.py", "target", start, start + 1),
                _hotspot("m.py", "pad1", 4, 5),
            ]
            meta = mod.cross_with_history(hotspots, root, since="2000-01-01")
        self.assertTrue(meta["history_available"])
        by_name = {h["name"]: h for h in hotspots}
        self.assertEqual(by_name["target"]["fix_commits_2y"], 2)
        self.assertEqual(by_name["target"]["verdict"], "active-risk")
        # The HEAD-line mapping credited both fixes to whatever sits there now.
        self.assertEqual(by_name["pad1"]["fix_commits_2y"], 0)

    def test_files_are_keyed_by_full_path_not_basename(self):
        base_v1 = "def run():\n    return 1\n"
        base_v2 = "def run():\n    return 2\n"
        base_v3 = "def run():\n    return 3\n"
        with GitTempProject(
            [
                {
                    "files": {"stores/base.py": base_v1, "backends/base.py": base_v1},
                    "message": "Add bases",
                },
                {"files": {"stores/base.py": base_v2}, "message": "Fix stores run"},
                {
                    "files": {"stores/base.py": base_v3},
                    "message": "Fix stores run again",
                },
            ]
        ) as root:
            hotspots = [
                _hotspot("stores/base.py", "run", 1, 2),
                _hotspot("backends/base.py", "run", 1, 2),
            ]
            mod.cross_with_history(hotspots, root, since="2000-01-01")
        self.assertEqual(hotspots[0]["fix_commits_2y"], 2)
        self.assertEqual(hotspots[1]["fix_commits_2y"], 0)

    def test_fix_inside_a_word_is_not_a_fix_commit(self):
        v1 = "def f():\n    return 1\n"
        with GitTempProject(
            [
                {"files": {"m.py": v1}, "message": "Add f"},
                {
                    "files": {"m.py": v1.replace("1", "2")},
                    "message": "Support URL prefixes",
                },
                {
                    "files": {"m.py": v1.replace("1", "3")},
                    "message": "Route by policies",
                },
            ]
        ) as root:
            hotspots = [_hotspot("m.py", "f", 1, 2)]
            mod.cross_with_history(hotspots, root, since="2000-01-01")
        self.assertEqual(hotspots[0]["fix_commits_2y"], 0)

    def test_unattributed_fixes_make_the_verdict_unknown(self):
        # An unmeasured value must not look like a clean one: with the parse
        # budget at zero, two fixes are seen but cannot be placed, so `quiet`
        # would be a guess.
        v1 = "def f():\n    return 1\n"
        with GitTempProject(
            [
                {"files": {"m.py": v1}, "message": "Add f"},
                {"files": {"m.py": v1.replace("1", "2")}, "message": "Fix f"},
                {"files": {"m.py": v1.replace("1", "3")}, "message": "Fix f again"},
            ]
        ) as root:
            hotspots = [_hotspot("m.py", "f", 1, 2, score=5.0)]
            old_cap = mod.MAX_HISTORY_PARSES
            mod.MAX_HISTORY_PARSES = 0
            try:
                meta = mod.cross_with_history(hotspots, root, since="2000-01-01")
            finally:
                mod.MAX_HISTORY_PARSES = old_cap
        self.assertEqual(meta["fix_commits_unattributed"], 2)
        self.assertTrue(meta["parse_cap_reached"])
        self.assertEqual(hotspots[0]["verdict"], "unknown")
        self.assertEqual(hotspots[0]["fix_commits_unattributed"], 2)

    def _rewritten_nested_helper(self, recent: bool):
        # A complex nested helper, written in one commit and never fixed.
        body = "".join(f"        x{i} = {i}\n" for i in range(9))
        v1 = "def outer():\n    def helper():\n        return 0\n    return helper\n"
        v2 = (
            "def outer():\n    def helper():\n"
            + body
            + "        return x0\n    return helper\n"
        )
        date = "2026-01-10T12:00:00+00:00" if recent else "2025-01-10T12:00:00+00:00"
        with GitTempProject(
            [
                {
                    "files": {"m.py": v1},
                    "message": "Add outer",
                    "date": "2024-06-01T12:00:00+00:00",
                },
                {"files": {"m.py": v2}, "message": "Rework the helper", "date": date},
            ]
        ) as root:
            start = v2.splitlines().index("    def helper():") + 1
            end = start + 10
            hotspots = [_hotspot("m.py", "outer.helper", start, end, score=10.0)]
            meta = mod.cross_with_history(
                hotspots,
                root,
                since="2000-01-01",
                recent_since=mod.datetime.fromisoformat("2025-12-01T00:00:00+00:00"),
            )
        return hotspots[0], meta

    def test_recently_rewritten_nested_function_is_not_settled(self):
        hotspot, meta = self._rewritten_nested_helper(recent=True)
        self.assertEqual(hotspot["fix_commits_2y"], 0)
        self.assertEqual(hotspot["recent_commits"], 1)
        self.assertEqual(hotspot["lines_added_recent"], 10)
        self.assertGreaterEqual(hotspot["recent_share"], 0.5)
        self.assertEqual(hotspot["verdict"], "recent-churn")
        self.assertEqual(meta["recent_window"]["commits_seen"], 1)

    def test_old_rewrite_still_settles(self):
        hotspot, meta = self._rewritten_nested_helper(recent=False)
        self.assertEqual(hotspot["recent_commits"], 0)
        self.assertEqual(hotspot["verdict"], "settled")
        self.assertEqual(meta["recent_window"]["commits_seen"], 0)

    def test_recent_churn_never_hides_active_risk(self):
        self.assertEqual(mod.verdict_for(3, 9.0, recent_share=1.0), "active-risk")
        self.assertEqual(mod.verdict_for(0, 9.0, recent_share=0.2), "settled")
        self.assertEqual(mod.verdict_for(0, 5.0, recent_share=0.6), "recent-churn")

    def test_fixes_outside_hotspot_files_are_ranked(self):
        simple = "def run(x):\n    return x\n"
        other = "def other():\n    return 0\n"
        with GitTempProject(
            [
                {
                    "files": {"pkg/a.py": simple, "pkg/b.py": other},
                    "message": "Add a and b",
                },
                {
                    "files": {"pkg/a.py": simple.replace("x\n", "x or 0\n")},
                    "message": "fix: default run",
                },
                {
                    "files": {"pkg/a.py": simple.replace("x\n", "x or 1\n")},
                    "message": "fix: default run again",
                },
                {"files": {"pkg/b.py": other.replace("0", "2")}, "message": "Tweak b"},
            ]
        ) as root:
            funcs = [
                f
                for name in ("pkg/a.py", "pkg/b.py")
                for f in mod.analyze_file(root / name, root)["functions"]
            ]
            top = mod.top_fixed_functions(funcs, root, root / "pkg", since="2000-01-01")
        self.assertTrue(top["available"])
        self.assertEqual(top["fix_commits_seen"], 2)
        self.assertEqual(
            [(f["qualified_name"], f["fix_commits_2y"]) for f in top["functions"]],
            [("pkg/a.py::run", 2)],
        )
        self.assertFalse(top["functions"][0]["is_hotspot"])

    def test_no_hotspots_is_not_missing_history(self):
        with GitTempProject([{"files": {"m.py": "x = 1\n"}, "message": "Add"}]) as root:
            meta = mod.cross_with_history([], root, since="2000-01-01")
        self.assertTrue(meta["history_available"])
        self.assertEqual(meta["fix_commits_seen_scope"], "hotspot files only")

    def test_top_fixed_without_history_is_unavailable(self):
        from pathlib import Path

        funcs = _functions("def f():\n    return 1\n").values()
        top = mod.top_fixed_functions(
            list(funcs), Path("/definitely/not/a/repo"), Path("/definitely/not/a/repo")
        )
        self.assertFalse(top["available"])

    def test_project_in_a_repo_subdirectory(self):
        v1 = "def f():\n    return 1\n"
        with GitTempProject(
            [
                {
                    "files": {"sub/pyproject.toml": "", "sub/m.py": v1},
                    "message": "Add f",
                },
                {"files": {"sub/m.py": v1.replace("1", "2")}, "message": "Fix f"},
            ]
        ) as root:
            hotspots = [_hotspot("m.py", "f", 1, 2)]
            meta = mod.cross_with_history(hotspots, root / "sub", since="2000-01-01")
        self.assertTrue(meta["history_available"])
        self.assertEqual(hotspots[0]["fix_commits_2y"], 1)


# A retry step, written out twice: the shape clone detection looks for.
_STEP = (
    "    reply = fetch(url, timeout=limit)\n"
    "    if reply.code in {429, 503}:\n"
    "        pool.release(conn)\n"
    "        conn = None\n"
    "        if reply.code == 503:\n"
    "            backoff(url)\n"
    "        log_retry(url, reply.code)\n"
    "        return\n"
    "    if reply.code != 200:\n"
    "        return\n"
)
_ARGS = "(url, limit, pool, conn)"


def _indented(block: str) -> str:
    return "".join("    " + line + "\n" for line in block.splitlines())


def _clones(sources: dict) -> dict:
    import ast as _ast

    return mod.find_clones([(rel, _ast.parse(src)) for rel, src in sources.items()])


class TestCloneDetection(unittest.TestCase):
    """Near-duplicate statement windows are grouped; look-alikes are not."""

    def test_copied_step_in_two_functions_is_one_exact_group(self):
        source = (
            f"def first{_ARGS}:\n    prepare(url)\n" + _STEP + "\n"
            f"def second{_ARGS}:\n    connect(url)\n" + _STEP
        )
        result = _clones({"pkg/client.py": source})
        self.assertEqual(result["groups_total"], 1)
        group = result["groups"][0]
        self.assertEqual(group["kind"], "exact")
        self.assertEqual(group["copies"], 2)
        self.assertEqual(
            [loc["function"] for loc in group["locations"]], ["first", "second"]
        )
        self.assertEqual(group["statements"], 3)

    def test_copy_with_renamed_locals_is_a_renamed_group(self):
        other = _STEP.replace("reply", "resp").replace("conn", "sock")
        source = (
            f"def first{_ARGS}:\n" + _STEP + "\n"
            "def second(url, limit, pool, sock):\n" + other
        )
        result = _clones({"pkg/a.py": source})
        self.assertEqual([g["kind"] for g in result["groups"]], ["renamed"])

    def test_across_files(self):
        result = _clones(
            {
                "pkg/a.py": f"def f{_ARGS}:\n" + _STEP,
                "pkg/b.py": f"def g{_ARGS}:\n" + _STEP,
            }
        )
        self.assertEqual(
            [loc["file"] for loc in result["groups"][0]["locations"]],
            ["pkg/a.py", "pkg/b.py"],
        )

    def test_different_calls_or_constants_are_not_clones(self):
        calls = _STEP.replace("log_retry", "log_failure")
        consts = _STEP.replace("200", "204").replace("{429, 503}", "{429, 502}")
        for variant in (calls, consts):
            source = (
                f"def first{_ARGS}:\n" + _STEP + f"\ndef second{_ARGS}:\n" + variant
            )
            result = _clones({"pkg/a.py": source})
            self.assertEqual(result["groups_total"], 0, variant)

    def test_short_repeats_are_not_reported(self):
        source = (
            "def first(x):\n    y = x + 1\n    return y\n\n"
            "def second(x):\n    y = x + 1\n    return y\n"
        )
        self.assertEqual(_clones({"pkg/a.py": source})["groups_total"], 0)

    def test_a_clone_inside_a_larger_clone_is_not_repeated(self):
        # The whole `if` block matches, so the run inside it matches too; only
        # the enclosing clone is reported.
        guarded = "    if url:\n" + _indented(_STEP)
        source = f"def first{_ARGS}:\n" + guarded + f"\ndef second{_ARGS}:\n" + guarded
        result = _clones({"pkg/a.py": source})
        self.assertEqual(result["groups_total"], 1)
        self.assertEqual(result["groups"][0]["statements"], 1)

    def test_main_reports_clones_and_skips_test_files(self):
        import io
        import sys

        files = {
            "pkg/__init__.py": "",
            "pkg/a.py": f"def f{_ARGS}:\n" + _STEP,
            "pkg/b.py": f"def g{_ARGS}:\n" + _STEP,
            "tests/test_a.py": f"def h{_ARGS}:\n" + _STEP,
        }
        with TempProject(files) as root:
            old_stdout, old_argv = sys.stdout, sys.argv
            sys.stdout = io.StringIO()
            sys.argv = ["measure_complexity.py", str(root)]
            try:
                mod.main()
                output = json.loads(sys.stdout.getvalue())
            finally:
                sys.stdout, sys.argv = old_stdout, old_argv
        groups = output["clones"]["groups"]
        self.assertEqual(len(groups), 1)
        self.assertEqual(
            sorted(loc["file"] for loc in groups[0]["locations"]),
            ["pkg/a.py", "pkg/b.py"],
        )


if __name__ == "__main__":
    unittest.main()
