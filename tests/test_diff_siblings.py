"""Tests for diff_siblings.py."""

import io
import json
import sys
import unittest
from contextlib import redirect_stdout

from helpers import TempProject, import_script

mod = import_script("diff_siblings")


_STORE = (
    "class Store:\n"
    "    def __init__(self):\n"
    "        self._ledger = None\n"
    "        self._lock = None\n"
    "    def _open_ledger(self):\n"
    "        return self._ledger\n"
    "    def _with_ledger(self, fn):\n"
    "        with self._lock:\n"
    "            return fn(self._open_ledger())\n"
    "    def get(self, key):\n"
    "        return self._with_ledger(lambda c: c.get(key))\n"
    "    def put(self, key, value):\n"
    "        return self._with_ledger(lambda c: c.put(key, value))\n"
    "    def delete(self, key):\n"
    "        return self._with_ledger(lambda c: c.delete(key))\n"
    "    def count(self):\n"
    "        return self._with_ledger(lambda c: c.count())\n"
    "    def refund(self, key):\n"
    "        return self._open_ledger().refund(key)\n"
    "    def close(self):\n"
    "        self._ledger = None\n"
)

_VIEWS = (
    "from app import route, audit\n"
    "@route('/a', permission='admin')\n"
    "def view_a(request):\n"
    "    audit(request)\n"
    "    return request.fetch('a', timeout=5)\n"
    "@route('/b', permission='admin')\n"
    "def view_b(request):\n"
    "    audit(request)\n"
    "    return request.fetch('b', timeout=5)\n"
    "@route('/c', permission='admin')\n"
    "def view_c(request):\n"
    "    audit(request)\n"
    "    return request.fetch('c', timeout=5)\n"
    "@route('/d')\n"
    "def view_d(request):\n"
    "    return request.fetch('d')\n"
)


def _analyze(files: dict[str, str], **kwargs) -> dict:
    with TempProject(files) as root:
        return mod.analyze(str(root), **kwargs)


class TestHelperBypass(unittest.TestCase):
    def test_method_reaching_the_wrapped_resource_directly(self):
        out = _analyze({"pkg/store.py": _STORE})
        found = {
            (b["helper"], b["wrapped_attribute"]): [m["method"] for m in b["bypassing"]]
            for b in out["helper_bypasses"]
        }
        self.assertEqual(found[("_with_ledger", "_open_ledger()")], ["refund"])
        # close() is lifecycle, _open_ledger() is the helper's own
        # sub-helper, and __init__ is a dunder: none is a bypass.
        for methods in found.values():
            self.assertNotIn("close", methods)
            self.assertNotIn("_open_ledger", methods)
            self.assertNotIn("__init__", methods)

    def test_no_bypass_when_callers_use_the_resource_too(self):
        source = _STORE.replace(
            "        return self._with_ledger(lambda c: c.get(key))\n",
            "        self._open_ledger()\n        return self._with_ledger(lambda c: c)\n",
        ).replace(
            "        return self._with_ledger(lambda c: c.count())\n",
            "        self._open_ledger()\n        return self._with_ledger(len)\n",
        )
        out = _analyze({"pkg/store.py": source})
        wrapped = {b["wrapped_attribute"] for b in out["helper_bypasses"]}
        self.assertNotIn("_open_ledger()", wrapped)


class TestSiblingOutliers(unittest.TestCase):
    def test_decorator_keyword_call_and_kwarg_a_minority_lacks(self):
        out = _analyze({"pkg/views.py": _VIEWS}, min_share=0.75)
        found = {
            o["feature"]: [m["function"] for m in o["missing_from"]]
            for o in out["sibling_outliers"]
            if o["group"] == "decorator @route"
        }
        self.assertEqual(found["decorator_kwarg:route(permission=)"], ["view_d"])
        self.assertEqual(found["call:audit"], ["view_d"])
        self.assertEqual(found["kwarg:request.fetch(timeout=)"], ["view_d"])
        # Shared by every sibling: never an outlier.
        self.assertNotIn("call:request.fetch", found)

    def test_decorators_are_not_counted_as_calls(self):
        out = _analyze({"pkg/views.py": _VIEWS})
        features = {o["feature"] for o in out["sibling_outliers"]}
        self.assertFalse(any(f.startswith("call:route") for f in features))

    def test_small_groups_and_stubs_are_left_out(self):
        source = (
            "class Base:\n"
            "    def a(self):\n        raise NotImplementedError\n"
            "    def b(self):\n        pass\n"
            "    def c(self):\n        return self.x()\n"
        )
        out = _analyze({"pkg/base.py": source})
        self.assertEqual(out["summary"]["groups_examined"], 0)
        self.assertEqual(out["sibling_outliers"], [])

    def test_test_files_are_skipped_unless_asked(self):
        files = {"tests/test_views.py": _VIEWS}
        self.assertEqual(_analyze(files)["summary"]["files_scanned"], 0)
        self.assertEqual(
            _analyze(files, include_tests=True)["summary"]["files_scanned"], 1
        )

    def test_main_reads_its_own_options(self):
        with TempProject({"pkg/views.py": _VIEWS}) as root:
            argv = sys.argv
            sys.argv = ["diff_siblings.py", str(root), "--min-group", "5"]
            buf = io.StringIO()
            try:
                with redirect_stdout(buf):
                    mod.main()
            finally:
                sys.argv = argv
        out = json.loads(buf.getvalue())
        self.assertEqual(out["parameters"]["min_group"], 5)
        self.assertEqual(out["sibling_outliers"], [])  # 4 views < 5

    def test_include_tests_is_not_reported_unknown(self):
        from contextlib import redirect_stderr

        with TempProject({"pkg/views.py": _VIEWS}) as root:
            argv = sys.argv
            sys.argv = ["diff_siblings.py", str(root), "--include-tests"]
            out, err = io.StringIO(), io.StringIO()
            try:
                with redirect_stdout(out), redirect_stderr(err):
                    mod.main()
            finally:
                sys.argv = argv
        self.assertNotIn("unknown flag", err.getvalue())


if __name__ == "__main__":
    unittest.main()
