"""Tests for sweep_fix_pattern.py."""

import unittest
from pathlib import Path

from helpers import import_script
from test_analyze_history import GitTempProject, _git

mod = import_script("sweep_fix_pattern")

_ORDERS = (
    "def ship(order):\n"
    "    if order.paid:\n"
    "        order.send()\n"
    "    total = price(order.items)\n"
    "    return total\n"
)
_ORDERS_FIXED = (
    "def ship(order):\n"
    "    if order.paid and not order.cancelled:\n"
    "        order.send()\n"
    "    total = price(order.items, currency=order.currency)\n"
    "    return total\n"
)
_REFUNDS = (
    "def refund(ticket):\n"
    "    if ticket.paid:\n"
    "        ticket.reverse()\n"
    "    amount = price(ticket.items)\n"
    "    if ticket.paid and not ticket.cancelled:\n"
    "        ticket.notify()\n"
    "    return amount\n"
)


class SweepTestCase(unittest.TestCase):
    def _project(self, commits):
        project = GitTempProject(commits)
        root = project.__enter__()
        self.addCleanup(project.__exit__, None, None, None)
        return root

    def _sweep(self, root, commit="HEAD"):
        return mod.analyze(str(root), commit=commit)

    def _forms(self, result):
        return {f["code"]: f for f in result["removed_forms"]}


class TestRemovedForms(SweepTestCase):
    def setUp(self):
        self.root = self._project(
            [
                {
                    "files": {"shop/orders.py": _ORDERS, "shop/refunds.py": _REFUNDS},
                    "message": "Add orders and refunds",
                },
                {
                    "files": {"shop/orders.py": _ORDERS_FIXED},
                    "message": "Do not ship cancelled orders",
                },
            ]
        )
        self.result = self._sweep(self.root)

    def test_rewritten_condition_and_call_are_removed_forms(self):
        forms = self._forms(self.result)
        self.assertEqual(
            set(forms), {"if order.paid:", "total = price(order.items)"}, self.result
        )
        self.assertEqual(forms["if order.paid:"]["file"], "shop/orders.py")
        self.assertEqual(forms["if order.paid:"]["line"], 2)

    def test_unfixed_siblings_are_found_with_other_variable_names(self):
        forms = self._forms(self.result)
        cond = forms["if order.paid:"]["matches"]
        self.assertEqual(
            [(m["file"], m["line"], m["function"]) for m in cond],
            [("shop/refunds.py", 2, "refund")],
        )
        self.assertFalse(cond[0]["same_file"])
        call = forms["total = price(order.items)"]["matches"]
        self.assertEqual([m["code"] for m in call], ["amount = price(ticket.items)"])

    def test_already_fixed_sibling_is_not_a_match(self):
        codes = [m["code"] for f in self.result["removed_forms"] for m in f["matches"]]
        self.assertNotIn("if ticket.paid and not ticket.cancelled:", codes)

    def test_envelope(self):
        self.assertEqual(self.result["subject"], "Do not ship cancelled orders")
        self.assertFalse(self.result["files_capped"])
        self.assertEqual(self.result["notes"], [])


class TestNegatives(SweepTestCase):
    def test_pure_addition_has_no_removed_form(self):
        root = self._project(
            [
                {"files": {"m.py": "def f(x):\n    return x.run()\n"}, "message": "a"},
                {
                    "files": {
                        "m.py": "def f(x):\n    if x is None:\n        return None\n"
                        "    return x.run()\n"
                    },
                    "message": "Guard f against None",
                },
            ]
        )
        result = self._sweep(root)
        self.assertEqual(result["removed_forms"], [])
        self.assertIn("only added code", result["notes"][0])

    def test_moved_statement_is_not_removed(self):
        before = "def f(x):\n    a = load(x.path)\n    b = 1\n    return a, b\n"
        after = "def f(x):\n    b = 1\n    a = load(x.path)\n    return a, b\n"
        root = self._project(
            [
                {"files": {"m.py": before}, "message": "a"},
                {"files": {"m.py": after}, "message": "Reorder"},
            ]
        )
        self.assertEqual(self._sweep(root)["removed_forms"], [])

    def test_trivial_forms_are_skipped(self):
        root = self._project(
            [
                {"files": {"m.py": "def f(x, y):\n    return x\n"}, "message": "a"},
                {"files": {"m.py": "def f(x, y):\n    return y\n"}, "message": "b"},
            ]
        )
        self.assertEqual(self._sweep(root)["removed_forms"], [])

    def test_different_callee_is_not_a_match(self):
        forms, _ = mod.removed_forms(
            "v = load(p)\n", "v = load(p, strict=True)\n", [(1, 1)]
        )
        self.assertEqual(len(forms), 1)
        self.assertNotEqual(
            mod.form_of(mod.ast.parse("w = loads(q)").body[0])[0], forms[0]["form"]
        )
        self.assertEqual(
            mod.form_of(mod.ast.parse("w = load(q)").body[0])[0], forms[0]["form"]
        )

    def test_unparseable_before_is_reported(self):
        forms, reason = mod.removed_forms("def (:\n", "x = 1\n", [(1, 1)])
        self.assertEqual(forms, [])
        self.assertIn("does not parse", reason)


class TestErrors(SweepTestCase):
    def test_commit_is_required(self):
        root = self._project([{"files": {"m.py": "x = 1\n"}, "message": "a"}])
        self.assertIn("error", mod.analyze(str(root)))

    def test_unknown_commit(self):
        root = self._project([{"files": {"m.py": "x = 1\n"}, "message": "a"}])
        result = mod.analyze(str(root), commit="no-such-rev")
        self.assertIn("unknown commit", result["error"])

    def test_root_commit_replaced_nothing(self):
        root = self._project(
            [{"files": {"m.py": "x = load(1)\n"}, "message": "Initial import"}]
        )
        sha = _git(root, "rev-parse", "HEAD")
        result = mod.analyze(str(root), commit=sha)
        self.assertEqual(result["removed_forms"], [])
        self.assertIn("no parent", result["notes"][0])

    def test_not_a_repository(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "pyproject.toml").write_text("", encoding="utf-8")
            result = mod.analyze(tmp, commit="HEAD")
        self.assertIn("error", result)


if __name__ == "__main__":
    unittest.main()
