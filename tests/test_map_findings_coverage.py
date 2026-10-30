"""Tests for map_findings_coverage.py."""

import io
import json
import sys
import unittest
from pathlib import Path

from helpers import TempProject, import_script

mod = import_script("map_findings_coverage")

_SOURCE = (
    "def load(path):\n"  # 1
    "    try:\n"  # 2
    "        data = read(path)\n"  # 3
    "    except OSError:\n"  # 4
    "        return None\n"  # 5
    "    if data:\n"  # 6
    "        return data\n"  # 7
    "    return {}\n"  # 8
    "\n"  # 9
    "\n"  # 10
    "def save(path, data):\n"  # 11
    "    write(path,\n"  # 12
    "          data)\n"  # 13
    "    # done\n"  # 14
    "    return True\n"  # 15
)

_COVERAGE = {
    "meta": {"branch_coverage": True},
    "files": {
        "pkg/store.py": {
            "executed_lines": [1, 2, 3, 6, 7, 11],
            "missing_lines": [4, 5, 8, 12, 15],
            "missing_branches": [[6, 8]],
        },
        "pkg/other.py": {"executed_lines": [1], "missing_lines": []},
    },
}


def _write(root: Path, name: str, data) -> str:
    path = root / name
    text = data if isinstance(data, str) else json.dumps(data)
    path.write_text(text, encoding="utf-8")
    return str(path)


class TestCoverageMode(unittest.TestCase):
    def _run(self, findings_text: str, coverage=None, name="findings.md") -> dict:
        with TempProject({"pkg/__init__.py": "", "pkg/store.py": _SOURCE}) as root:
            cov = _write(root, "coverage.json", coverage or _COVERAGE)
            findings = _write(root, name, findings_text)
            return mod.analyze(str(root), coverage=cov, findings=[findings])

    def _by_range(self, out: dict) -> dict:
        return {(r["file"], r["start"], r["end"]): r for r in out["refs"]}

    def test_verdicts(self):
        out = self._run(
            "- swallowed error at pkg/store.py:4-5\n"
            "- empty result pkg/store.py:6-8 and pkg/store.py:3\n"
            "- happy path pkg/store.py:7\n"
        )
        rows = self._by_range(out)
        self.assertEqual(rows[("pkg/store.py", 4, 5)]["verdict"], "never_run")
        self.assertEqual(rows[("pkg/store.py", 6, 8)]["verdict"], "partly_run")
        self.assertEqual(rows[("pkg/store.py", 6, 8)]["missing_lines"], ["8"])
        self.assertEqual(rows[("pkg/store.py", 3, 3)]["verdict"], "ran")
        self.assertEqual(rows[("pkg/store.py", 6, 8)]["missing_branches"], [[6, 8]])
        self.assertEqual(out["summary"]["never_run"], 1)
        self.assertEqual(out["summary"]["partly_run"], 1)
        self.assertEqual(out["status"], "ok")

    def test_branch_missing_on_fully_run_lines(self):
        out = self._run("pkg/store.py:6-7\n")
        (row,) = out["refs"]
        self.assertEqual(row["verdict"], "ran_branches_missing")

    def test_enclosing_function_coverage(self):
        out = self._run("pkg/store.py:5\n")
        (row,) = out["refs"]
        self.assertEqual(row["function"]["name"], "load")
        self.assertEqual(row["function"]["statements"], 8)
        self.assertEqual(row["function"]["executed"], 5)
        self.assertEqual(row["function"]["percent"], 62.5)

    def test_continuation_line_expands_to_its_statement(self):
        out = self._run("pkg/store.py:13\n")
        (row,) = out["refs"]
        self.assertEqual(row["expanded_to"], 12)
        self.assertEqual(row["verdict"], "never_run")

    def test_unmeasured_is_null_not_clean(self):
        out = self._run("pkg/missing.py:3\npkg/store.py:14\n")
        rows = self._by_range(out)
        self.assertIsNone(rows[("pkg/missing.py", 3, 3)]["verdict"])
        self.assertIn(
            "not in the coverage data", rows[("pkg/missing.py", 3, 3)]["reason"]
        )
        self.assertIsNone(rows[("pkg/store.py", 14, 14)]["verdict"])
        self.assertEqual(out["summary"]["not_measured"], 2)
        self.assertEqual(out["summary"]["ran"], 0)

    def test_json_findings_and_absolute_coverage_keys(self):
        coverage = {
            "meta": {"branch_coverage": False},
            "files": {"/tmp/export/pkg/store.py": _COVERAGE["files"]["pkg/store.py"]},
        }
        findings = json.dumps(
            {
                "findings": [
                    {"id": "F1", "file": "pkg/store.py", "line": 4, "end_line": 5},
                    {"id": "F2", "location": "pkg/store.py:7"},
                ]
            }
        )
        out = self._run(findings, coverage=coverage, name="findings.json")
        rows = {r["id"]: r for r in out["refs"]}
        self.assertEqual(rows["F1"]["verdict"], "never_run")
        self.assertEqual(rows["F2"]["verdict"], "ran")
        self.assertIsNone(rows["F2"]["missing_branches"])
        self.assertFalse(out["branch_coverage"])
        self.assertTrue(any("branch" in w for w in out["warnings"]))

    def test_ambiguous_file_name_is_not_guessed(self):
        self.assertIsNone(
            mod.match_coverage_key("util.py", ["a/util.py", "b/util.py"], Path("/x"))
        )
        self.assertEqual(
            mod.match_coverage_key("a/util.py", ["a/util.py", "b/util.py"], Path("/x")),
            "a/util.py",
        )

    def test_no_refs_is_not_ok(self):
        out = self._run("nothing to see here\n")
        self.assertEqual(out["status"], "no_refs")
        self.assertTrue(out["warnings"])


_CALC = "def add(a, b):\n    return a + b\n\n\ndef scale(x):\n    return x * 2\n"
_CALC_TESTS = (
    "import unittest\n"
    "from pkg.calc import add, scale\n"
    "\n"
    "\n"
    "class TestCalc(unittest.TestCase):\n"
    "    def test_add(self):\n"
    "        self.assertEqual(add(1, 2), 3)\n"
    "\n"
    "    def test_scale_runs(self):\n"
    "        scale(3)  # executed, never checked\n"
)
_MUTATIONS = [
    {"id": "M1", "file": "pkg/calc.py", "old": "a + b", "new": "a - b"},
    {"id": "M2", "file": "pkg/calc.py", "old": "x * 2", "new": "x * 3"},
    {"id": "M3", "file": "pkg/calc.py", "old": "a * b", "new": "a"},
    {
        "id": "M4",
        "file": "pkg/calc.py",
        "old": "return a + b",
        "new": "return a + b + 0",
        "kind": "fix",
    },
]


class TestMutationMode(unittest.TestCase):
    def _cmd(self) -> str:
        return f"{sys.executable} -m unittest discover -s tests -t ."

    def test_refuses_the_project_root(self):
        with TempProject(
            {
                "pkg/__init__.py": "",
                "pkg/calc.py": _CALC,
                "tests/__init__.py": "",
                "tests/test_calc.py": _CALC_TESTS,
            }
        ) as root:
            muts = _write(root, "mutations.json", _MUTATIONS)
            out = mod.analyze(
                str(root),
                mutations=muts,
                export=str(root),
                test_cmd=self._cmd(),
                timeout=60,
            )
            restored = (root / "pkg/calc.py").read_text(encoding="utf-8")
        # analyze() refuses the project root itself as the export.
        self.assertEqual(out["status"], "FAILED")
        self.assertEqual(restored, _CALC)

    def test_probe_on_an_export(self):
        with (
            TempProject({"project/pyproject.toml": ""}) as root,
            TempProject(
                {
                    "pkg/__init__.py": "",
                    "pkg/calc.py": _CALC,
                    "tests/__init__.py": "",
                    "tests/test_calc.py": _CALC_TESTS,
                }
            ) as export,
        ):
            muts = _write(root, "mutations.json", _MUTATIONS)
            out = mod.analyze(
                str(root / "project"),
                mutations=muts,
                export=str(export),
                test_cmd=self._cmd(),
                timeout=60,
            )
            restored = (export / "pkg/calc.py").read_text(encoding="utf-8")
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["baseline"]["returncode"], 0)
        rows = {r["id"]: r for r in out["mutations"]}
        self.assertEqual(rows["M1"]["verdict"], "killed")
        self.assertEqual(rows["M1"]["meaning"], "checked")
        self.assertEqual(rows["M2"]["verdict"], "survived")
        self.assertEqual(rows["M2"]["meaning"], "covered_but_unchecked")
        self.assertEqual(rows["M3"]["verdict"], "skipped")
        self.assertEqual(rows["M4"]["meaning"], "fix_not_detected")
        self.assertEqual(out["summary"]["killed"], 1)
        self.assertEqual(restored, _CALC)

    def test_failing_baseline_judges_nothing(self):
        broken = _CALC_TESTS.replace("3)", "4)")
        with (
            TempProject({"project/pyproject.toml": ""}) as root,
            TempProject(
                {
                    "pkg/__init__.py": "",
                    "pkg/calc.py": _CALC,
                    "tests/__init__.py": "",
                    "tests/test_calc.py": broken,
                }
            ) as export,
        ):
            muts = _write(root, "mutations.json", _MUTATIONS[:1])
            out = mod.analyze(
                str(root / "project"),
                mutations=muts,
                export=str(export),
                test_cmd=self._cmd(),
                timeout=60,
            )
        self.assertEqual(out["status"], "baseline_failed")
        self.assertIsNone(out["mutations"][0]["verdict"])
        self.assertEqual(out["summary"]["killed"], 0)

    def test_refuses_a_git_checkout(self):
        with TempProject(
            {"project/pyproject.toml": "", "checkout/.git/HEAD": ""}
        ) as root:
            muts = _write(root, "mutations.json", _MUTATIONS[:1])
            out = mod.analyze(
                str(root / "project"),
                mutations=muts,
                export=str(root / "checkout"),
                test_cmd=self._cmd(),
            )
        self.assertEqual(out["status"], "FAILED")
        self.assertIn("exported copy", out["error"])


class TestCli(unittest.TestCase):
    def _main(self, argv: list[str]) -> tuple[dict | str, int]:
        old_out, old_argv = sys.stdout, sys.argv
        sys.stdout = io.StringIO()
        sys.argv = ["map_findings_coverage.py", *argv]
        code = 0
        try:
            mod.main()
        except SystemExit as exc:
            code = exc.code or 0
        finally:
            text = sys.stdout.getvalue()
            sys.stdout, sys.argv = old_out, old_argv
        try:
            return json.loads(text), code
        except json.JSONDecodeError:
            return text, code

    def test_help(self):
        text, code = self._main(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("Usage:", text)

    def test_no_mode_is_an_error(self):
        out, code = self._main(["."])
        self.assertEqual(code, 1)
        self.assertEqual(out["status"], "FAILED")

    def test_coverage_mode_from_the_command_line(self):
        with TempProject({"pkg/store.py": _SOURCE}) as root:
            cov = _write(root, "coverage.json", _COVERAGE)
            out, code = self._main(
                [
                    str(root),
                    "--coverage",
                    cov,
                    "--ref",
                    "pkg/store.py:4-5",
                    "--ref",
                    "pkg/store.py:7",
                ]
            )
        self.assertEqual(code, 0)
        self.assertEqual(out["summary"]["refs"], 2)
        self.assertEqual(out["files_total"], 1)
        self.assertFalse(out["files_capped"])


if __name__ == "__main__":
    unittest.main()
