"""Tests for scripts/pytest_plugins/crt_leaktrack.py (run without pytest)."""

import gc
import importlib.util
import json
import os
import sqlite3
import tempfile
import types
import unittest
import warnings
from pathlib import Path

_PLUGIN = (
    Path(__file__).resolve().parent.parent
    / "plugins"
    / "code-review-toolkit"
    / "scripts"
    / "pytest_plugins"
    / "crt_leaktrack.py"
)
_HERE = str(Path(__file__).resolve().parent)


def _load():
    spec = importlib.util.spec_from_file_location("crt_leaktrack", _PLUGIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _open_and_drop():
    conn = sqlite3.connect(":memory:")
    conn.execute("select 1")


def _open_with_context_manager():
    with sqlite3.connect(":memory:") as conn:
        conn.execute("select 1")


def _open_and_close():
    conn = sqlite3.connect(":memory:")
    conn.close()


class TestTracker(unittest.TestCase):
    def setUp(self):
        # The leaks below are deliberate; keep their warnings out of the run.
        catcher = warnings.catch_warnings()
        catcher.__enter__()
        self.addCleanup(catcher.__exit__, None, None, None)
        warnings.simplefilter("ignore", ResourceWarning)
        self.mod = _load()
        self.original = sqlite3.connect
        self.tracker = self.mod.Tracker(_HERE, depth=1)
        self.tracker.install()

    def tearDown(self):
        self.tracker.uninstall()
        sqlite3.connect = self.original

    def _leaks(self):
        gc.collect()
        return dict(self.tracker.leaked)

    def test_importing_the_plugin_patches_nothing(self):
        self.tracker.uninstall()
        _load()
        self.assertIs(sqlite3.connect, self.original)

    def test_unclosed_connection_is_attributed_to_its_creation_site(self):
        _open_and_drop()
        ((origin, count),) = self._leaks().items()
        self.assertTrue(origin.startswith("test_crt_leaktrack.py:"), origin)
        self.assertEqual(count, 1)

    def test_context_manager_does_not_close_so_it_leaks(self):
        _open_with_context_manager()
        self.assertEqual(sum(self._leaks().values()), 1)

    def test_closed_connection_is_not_a_leak(self):
        _open_and_close()
        self.assertEqual(self._leaks(), {})
        self.assertEqual((self.tracker.created, self.tracker.closed), (1, 1))

    def test_custom_factory_is_preserved(self):
        class Custom(sqlite3.Connection):
            marker = True

        conn = sqlite3.connect(":memory:", factory=Custom)
        self.assertIsInstance(conn, Custom)
        conn.close()

    def test_still_referenced_connection_is_open_at_session_end(self):
        conn = sqlite3.connect(":memory:")
        report = self.tracker.report()
        self.assertEqual(report["leaked_total"], 0)
        self.assertEqual(len(report["open_at_session_end"]), 1)
        conn.close()

    def test_frames_outside_root_are_not_named(self):
        tracker = self.mod.Tracker(tempfile.gettempdir())
        self.assertEqual(tracker.origin(), "<outside root>")

    def test_uninstall_restores_connect(self):
        self.tracker.uninstall()
        self.assertIs(sqlite3.connect, self.original)


class TestPytestHooks(unittest.TestCase):
    """The hooks pytest calls, driven with stand-in objects."""

    def test_configure_finish_and_unconfigure(self):
        catcher = warnings.catch_warnings()
        catcher.__enter__()
        self.addCleanup(catcher.__exit__, None, None, None)
        warnings.simplefilter("ignore", ResourceWarning)
        mod = _load()
        original = sqlite3.connect
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "leaks.json")
            env = {mod.ENV_OUT: out, mod.ENV_ROOT: _HERE}
            saved = {k: os.environ.get(k) for k in env}
            os.environ.update(env)
            try:
                mod.pytest_configure(types.SimpleNamespace(rootpath=Path(tmp)))
                self.assertIsNot(sqlite3.connect, original)
                _open_and_drop()
                mod.pytest_sessionfinish(None, 0)
                mod.pytest_unconfigure(None)
            finally:
                for key, value in saved.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                sqlite3.connect = original
            with open(out, encoding="utf-8") as fh:
                report = json.load(fh)
        self.assertIs(sqlite3.connect, original)
        self.assertEqual(report["leaked_total"], 1)
        self.assertIn("test_crt_leaktrack.py:", report["leaked"][0]["origin"])


if __name__ == "__main__":
    unittest.main()
