"""CLI flags wired at integration: each reaches the analyze() argument it names."""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import import_script


class TestAnalyzeHistoryMaxBranches(unittest.TestCase):
    def test_parsed(self):
        mod = import_script("analyze_history")
        self.assertEqual(mod.parse_args(["--max-branches", "5"])["max_branches"], 5)
        self.assertIsNone(mod.parse_args([])["max_branches"])

    def test_non_integer_exits(self):
        mod = import_script("analyze_history")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            mod.parse_args(["--max-branches", "many"])


class TestExtractTestInvariantsKeyType(unittest.TestCase):
    def test_specs_merge(self):
        mod = import_script("extract_test_invariants")
        self.assertEqual(
            mod._key_types(["B=fix", "F=feature"]), {"B": "fix", "F": "feature"}
        )
        self.assertIsNone(mod._key_types(None))

    def test_flag_reaches_analyze(self):
        mod = import_script("extract_test_invariants")
        seen = {}

        def fake(target, **kwargs):
            seen.update(kwargs)
            return {}

        with (
            mock.patch.object(mod, "analyze", fake),
            mock.patch.object(sys, "argv", ["x", ".", "--key-type", "B=fix"]),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            mod.main()
        self.assertEqual(seen["key_types"], {"B": "fix"})


class TestCheckTypingFlags(unittest.TestCase):
    def test_flags_reach_analyze(self):
        mod = import_script("check_typing")
        seen = {}

        def fake(target, **kwargs):
            seen.update(kwargs)
            return {"status": "OK"}

        with tempfile.TemporaryDirectory() as tmp:
            bmap = Path(tmp) / "map.json"
            bmap.write_text(json.dumps({"request": "pkg.Request"}))
            argv = [
                "x",
                ".",
                "--extra-pass=--strict-equality",
                "--extra-pass=--warn-return-any",
                "--cache-dir",
                tmp,
                "--boundary-map",
                str(bmap),
                "--boundary-flag=--check-untyped-defs",
            ]
            with (
                mock.patch.object(mod, "analyze", fake),
                mock.patch.object(sys, "argv", argv),
                contextlib.redirect_stdout(io.StringIO()),
                self.assertRaises(SystemExit) as cm,
            ):
                mod.main()
        self.assertEqual(cm.exception.code, 0)
        self.assertEqual(
            seen["extra_passes"], ["--strict-equality", "--warn-return-any"]
        )
        self.assertEqual(seen["cache_dir"], tmp)
        self.assertEqual(seen["boundary_map"], {"request": "pkg.Request"})
        self.assertEqual(seen["boundary_flags"], ["--check-untyped-defs"])


class TestCountTypesMypyJson(unittest.TestCase):
    def test_flag_reaches_analyze(self):
        mod = import_script("count_types")
        seen = {}

        def fake(target, **kwargs):
            seen.update(kwargs)
            return {}

        with (
            mock.patch.object(mod, "analyze", fake),
            mock.patch.object(sys, "argv", ["x", ".", "--mypy-json", "m.json"]),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            mod.main()
        self.assertEqual(seen["mypy_json"], "m.json")


class TestRunExternalToolsDeadSymbols(unittest.TestCase):
    def test_parsed(self):
        mod = import_script("run_external_tools")
        self.assertEqual(
            mod.parse_args([".", "--dead-symbols", "d.json"]).dead_symbols, "d.json"
        )


if __name__ == "__main__":
    unittest.main()
