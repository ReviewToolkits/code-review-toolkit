"""Tests for check_typing.py -- mypy integrity, not mypy coverage."""

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

from helpers import TempProject, import_script

mod = import_script("check_typing")

# A stand-in for mypy, so the orchestration (cache, passes, capping) is tested
# without mypy installed. It logs its argv and cwd, mimics mypy's cache
# behaviour (`.mypy_cache` in the cwd unless --cache-dir says otherwise), and
# prints the canned output registered for the labelled pass it was run as.
_FAKE_MYPY = """#!{python}
import json, os, sys
args = sys.argv[1:]
if args == ["--version"]:
    print("mypy 9.9.9 (compiled: no)")
    sys.exit(0)
with open(os.environ["FAKE_MYPY_LOG"], "a") as log:
    log.write(json.dumps({{"argv": args, "cwd": os.getcwd()}}) + "\\n")
cache = args[args.index("--cache-dir") + 1] if "--cache-dir" in args else ".mypy_cache"
os.makedirs(cache, exist_ok=True)
with open(os.path.join(cache, "marker"), "w") as fh:
    fh.write("x")
with open(os.environ["FAKE_MYPY_RESPONSES"]) as fh:
    responses = json.load(fh)
key = ""
for flag in ("--disallow-any-unimported", "--warn-unreachable", "--warn-unused-ignores"):
    if flag in args:
        key = flag
out = responses.get(key, responses.get("", ""))
sys.stdout.write(out)
sys.exit(1 if ": error:" in out else 0)
"""


class FakeMypy:
    """Context manager: a fake mypy binary plus a log of its invocations."""

    def __init__(self, responses: dict[str, str]):
        self.responses = responses

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="crt_fake_mypy_")
        d = Path(self._tmp.name)
        self.bin = d / "mypy"
        self.bin.write_text(_FAKE_MYPY.format(python=sys.executable))
        self.bin.chmod(self.bin.stat().st_mode | stat.S_IXUSR)
        self.log = d / "log.jsonl"
        self.log.write_text("")
        responses = d / "responses.json"
        responses.write_text(json.dumps(self.responses))
        self._old = {
            k: os.environ.get(k) for k in ("FAKE_MYPY_LOG", "FAKE_MYPY_RESPONSES")
        }
        os.environ["FAKE_MYPY_LOG"] = str(self.log)
        os.environ["FAKE_MYPY_RESPONSES"] = str(responses)
        return self

    def calls(self) -> list[dict]:
        return [json.loads(x) for x in self.log.read_text().splitlines() if x]

    def __exit__(self, *exc):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()


def run_in(cwd: str, fn, *args, **kwargs):
    old = os.getcwd()
    os.chdir(cwd)
    try:
        return fn(*args, **kwargs)
    finally:
        os.chdir(old)


_CLEAN_RUN = {"": "Success: no issues found in 1 source file\n"}


class TestParseOutput(unittest.TestCase):
    def test_parses_an_error_with_a_code(self):
        findings, stats = mod.parse_output(
            'src/a.py:12:5: error: Item "None" has no attribute "x" '
            "[union-attr]\n"
            "Found 1 error in 1 file (checked 45 source files)\n"
        )
        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertEqual((f["file"], f["line"], f["column"]), ("src/a.py", 12, 5))
        self.assertEqual(f["code"], "union-attr")
        self.assertEqual(f["kind"], "defect")
        self.assertEqual(stats["files_checked"], 45)

    def test_ansi_coloured_output_still_parses(self):
        """mypy honours FORCE_COLOR even when piped, and the codes land between
        `file:line:` and `error:`. Before --no-color-output this parsed to ZERO
        findings while the summary line still read 3 -- a confident "0 type
        errors" on any machine with FORCE_COLOR set. Found on coverage.py, where
        it hid three real `no-any-unimported` errors."""
        coloured = (
            "coverage/html.py:134: \x1b[1m\x1b[31merror:\x1b(B\x1b[m Argument 2 to "
            '\x1b[1m"data_for_file"\x1b(B\x1b[m becomes \x1b[1m"Any"\x1b(B\x1b[m '
            "due to an unfollowed import  \x1b(B\x1b[33m[no-any-unimported]\x1b(B\x1b[m\n"
            "\x1b[1m\x1b[31mFound 1 error in 1 file (checked 45 source files)\x1b(B\x1b[m\n"
        )
        findings, stats = mod.parse_output(coloured)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["code"], "no-any-unimported")
        self.assertEqual(findings[0]["line"], 134)
        # The whole point: our count must agree with mypy's own.
        self.assertEqual(len(findings), stats["errors"])

    def test_run_mypy_forces_colour_off(self):
        """Two independent belts: the flag and the environment."""
        import inspect

        src = inspect.getsource(mod._run_mypy)
        self.assertIn("--no-color-output", src)
        self.assertIn("FORCE_COLOR", src)

    def test_parses_the_clean_summary(self):
        _, stats = mod.parse_output("Success: no issues found in 45 source files\n")
        self.assertEqual(
            stats, {"errors": 0, "files_with_errors": 0, "files_checked": 45}
        )

    def test_notes_are_not_findings(self):
        findings, _ = mod.parse_output(
            'src/a.py:1: note: Revealed type is "builtins.int"\n'
        )
        self.assertEqual(findings, [])

    def test_override_notes_stay_with_their_error(self):
        """#63: the Superclass/Subclass signatures are what make an `override`
        error triageable; they used to be dropped."""
        out = (
            'a.py:5: error: Signature of "f" incompatible with supertype "A"  '
            "[override]\n"
            "a.py:5: note:      Superclass:\n"
            "a.py:5: note:          def f(self, x: int) -> int\n"
            "a.py:5: note:      Subclass:\n"
            "a.py:5: note:          def f(self) -> int\n"
            'a.py:9: error: Name "y" is not defined  [name-defined]\n'
            "Found 2 errors in 1 file (checked 1 source file)\n"
        )
        findings, stats = mod.parse_output(out)
        self.assertEqual(len(findings), stats["errors"])
        self.assertEqual(
            [n["message"] for n in findings[0]["notes"]],
            [
                "     Superclass:",
                "         def f(self, x: int) -> int",
                "     Subclass:",
                "         def f(self) -> int",
            ],
        )
        self.assertEqual(findings[1]["notes"], [])

    def test_a_note_pointing_elsewhere_still_belongs_to_its_error(self):
        findings, _ = mod.parse_output(
            'a.py:3: error: Unexpected keyword argument "z" for "f"  [call-arg]\n'
            'b.py:10: note: "f" defined here\n'
        )
        self.assertEqual(findings[0]["notes"][0]["file"], "b.py")
        self.assertEqual(findings[0]["notes"][0]["line"], 10)

    def test_a_coded_note_stands_alone(self):
        """`annotation-unchecked` notes carry their own code and follow whatever
        error came before them in the output; they are not that error's."""
        standalone: list[dict] = []
        findings, _ = mod.parse_output(
            "a.py:3: error: bad  [arg-type]\n"
            "a.py:40: note: By default the bodies of untyped functions are not "
            "checked, consider using --check-untyped-defs  [annotation-unchecked]\n"
            "a.py:41: note: a stray note after a coded note\n",
            standalone,
        )
        self.assertEqual(findings[0]["notes"], [])
        self.assertEqual(
            [n.get("code") for n in standalone], ["annotation-unchecked", None]
        )

    def test_context_note_attaches_to_the_error_it_introduces(self):
        findings, _ = mod.parse_output(
            "a.py:1: error: first  [misc]\n"
            "b.py:1: note: In module imported here:\n"
            "c.py:4: error: second  [misc]\n"
        )
        self.assertEqual(findings[0]["notes"], [])
        self.assertEqual(findings[1]["notes"][0]["message"], "In module imported here:")

    def test_defect_and_annotation_codes_are_distinguished(self):
        findings, _ = mod.parse_output(
            "a.py:1: error: bad [arg-type]\n"
            "a.py:2: error: missing [no-untyped-def]\n"
            "a.py:3: error: other [some-new-code]\n"
        )
        self.assertEqual(
            [f["kind"] for f in findings],
            ["defect", "missing-annotation", "other"],
        )


    def test_root_cause_codes_are_defects(self):
        """`name-defined`, `return` and `method-assign` used to land in `other`."""
        findings, _ = mod.parse_output(
            'a.py:1: error: Name "Sequnce" is not defined  [name-defined]\n'
            "a.py:2: error: Missing return statement  [return]\n"
            "a.py:3: error: Cannot assign to a method  [method-assign]\n"
        )
        self.assertEqual([f["kind"] for f in findings], ["defect"] * 3)


class TestFailureIsNeverClean(unittest.TestCase):
    """The single most important behaviour: analyzed-nothing is not clean."""

    def test_stdlib_shadowing_is_diagnosed(self):
        reason = mod._diagnose_failure(
            'mypy: "Lib/abc.py" shadows library module "abc"', "", 2
        )
        self.assertIn("shadows a stdlib module", reason)
        self.assertIn("--no-namespace-packages", reason)

    def test_duplicate_module_is_diagnosed(self):
        reason = mod._diagnose_failure("Duplicate module named 'a'", "", 2)
        self.assertIn("same module name", reason)

    def test_unresolved_imports_are_diagnosed(self):
        reason = mod._diagnose_failure(
            "Cannot find implementation or library stub for module named 'x'", "", 2
        )
        self.assertIn("outside the project's environment", reason)

    def test_timeout_is_diagnosed(self):
        self.assertIn("did not run to completion", mod._diagnose_failure("", "", None))

    def test_unknown_failure_still_says_no_information(self):
        reason = mod._diagnose_failure("something odd", "", 2)
        self.assertIn("no information", reason)
        self.assertNotIn("clean", reason.replace("not as clean", ""))


class TestMissingMypy(unittest.TestCase):
    def test_absent_binary_is_FAILED(self):
        result = mod.analyze(".", mypy_bin="definitely-not-a-real-mypy-binary")
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["findings"], [])


class TestCacheDirectory(unittest.TestCase):
    """#29/#60: mypy's cache must land in neither the reviewed project nor the
    caller's cwd. Before the fix `.mypy_cache/` appeared in the cwd."""

    def test_cache_goes_to_a_private_dir_that_is_removed(self):
        with TempProject({"pkg/__init__.py": "", "pkg/m.py": "x = 1\n"}) as root:
            with FakeMypy(_CLEAN_RUN) as fake, tempfile.TemporaryDirectory() as cwd:
                result = run_in(
                    cwd, mod.analyze, str(root / "pkg"), mypy_bin=str(fake.bin)
                )
                calls = fake.calls()
                self.assertEqual(result["status"], "OK")
                self.assertTrue(calls)
                for call in calls:
                    self.assertIn("--cache-dir", call["argv"])
                    cache = Path(call["argv"][call["argv"].index("--cache-dir") + 1])
                    self.assertFalse(cache.resolve().is_relative_to(root))
                    self.assertFalse(cache.exists(), "cache dir not removed")
                self.assertEqual(os.listdir(cwd), [])
            self.assertFalse((root / ".mypy_cache").exists())
            self.assertFalse((root / "pkg" / ".mypy_cache").exists())


class TestMaxFiles(unittest.TestCase):
    """#61: --max-files was accepted and silently ignored, and the output had
    no files_capped field."""

    _FILES: ClassVar[dict[str, str]] = {
        "pkg/__init__.py": "",
        **{f"pkg/m{i}.py": "x = 1\n" for i in range(5)},
    }

    def test_cap_hands_mypy_only_the_first_n_files(self):
        with TempProject(self._FILES) as root, FakeMypy(_CLEAN_RUN) as fake:
            result = mod.analyze(str(root / "pkg"), max_files=2, mypy_bin=str(fake.bin))
            self.assertTrue(result["files_capped"])
            self.assertEqual(result["files_total"], 6)
            self.assertEqual(result["files_analyzed"], 2)
            for call in fake.calls():
                targets = [a for a in call["argv"] if a.endswith(".py")]
                self.assertEqual(len(targets), 2)
                self.assertNotIn(str(root / "pkg"), call["argv"])
                self.assertIn("--follow-imports=silent", call["argv"])

    def test_uncapped_run_checks_the_directory_and_says_so(self):
        with TempProject(self._FILES) as root, FakeMypy(_CLEAN_RUN) as fake:
            result = mod.analyze(str(root / "pkg"), mypy_bin=str(fake.bin))
            self.assertFalse(result["files_capped"])
            self.assertEqual(result["files_analyzed"], result["files_total"])
            for call in fake.calls():
                self.assertIn(str(root / "pkg"), call["argv"])
                self.assertNotIn("--follow-imports=silent", call["argv"])

    def test_cap_at_or_above_the_file_count_is_not_capped(self):
        with TempProject(self._FILES) as root, FakeMypy(_CLEAN_RUN) as fake:
            result = mod.analyze(str(root / "pkg"), max_files=6, mypy_bin=str(fake.bin))
            self.assertFalse(result["files_capped"])

    def test_failed_run_still_carries_the_envelope(self):
        result = mod.analyze(".", mypy_bin="definitely-not-a-real-mypy-binary")
        self.assertIn("files_capped", result)


_BASE_ERR = "pkg/m.py:2: error: bad  [arg-type]\n"
_ONE_ERROR = "Found 1 error in 1 file (checked 2 source files)\n"
_TWO_ERRORS = "Found 2 errors in 1 file (checked 2 source files)\n"
_UNUSED_IGNORE = 'pkg/m.py:1: error: Unused "type: ignore" comment  [unused-ignore]\n'
_UNREACHABLE = "pkg/m.py:7: error: Statement is unreachable  [unreachable]\n"


def _run(files: dict[str, str], responses: dict[str, str], **kwargs):
    with TempProject(files) as root, FakeMypy(responses) as fake:
        result = mod.analyze(str(root / "pkg"), mypy_bin=str(fake.bin), **kwargs)
        flags_seen = [
            [a for a in c["argv"] if a.startswith("--") and a != "--cache-dir"]
            for c in fake.calls()
        ]
        return result, flags_seen


_PKG = {"pkg/__init__.py": "", "pkg/m.py": "import yaml  # type: ignore\nx = 1\n"}


class TestStaleIgnores(unittest.TestCase):
    """#62: with warn_unused_ignores off, `stale_ignores: 0` meant "not
    measured" and read as "none"."""

    def test_measured_by_a_labelled_pass_when_the_config_is_silent(self):
        result, _ = _run(
            _PKG,
            {
                "": _BASE_ERR + _ONE_ERROR,
                "--warn-unused-ignores": _UNUSED_IGNORE + _BASE_ERR + _TWO_ERRORS,
            },
        )
        self.assertEqual(result["summary"]["stale_ignores"], 1)
        self.assertEqual(
            result["summary"]["stale_ignores_source"], "warn-unused-ignores-pass"
        )
        self.assertEqual(result["stale_ignore_findings"][0]["line"], 1)
        # The baseline stays the project's own.
        self.assertEqual(result["summary"]["total"], 1)

    def test_unmeasured_is_null_with_a_reason_not_zero(self):
        result, _ = _run(
            _PKG,
            {"": "Success: no issues found in 2 source files\n"},
            unused_ignores_pass=False,
        )
        self.assertIsNone(result["summary"]["stale_ignores"])
        self.assertIn("not measured", result["summary"]["stale_ignores_reason"])

    def test_a_failed_pass_is_null_not_zero(self):
        result, _ = _run(
            _PKG,
            {
                "": "Success: no issues found in 2 source files\n",
                "--warn-unused-ignores": "mypy: error: something broke\n",
            },
        )
        self.assertIsNone(result["summary"]["stale_ignores"])
        self.assertIn("failed", result["summary"]["stale_ignores_reason"])
        self.assertFalse(
            result["summary"]["labelled_passes"]["warn-unused-ignores"]["ran"]
        )

    def test_project_config_that_enables_it_is_used_without_an_extra_pass(self):
        files = {**_PKG, "mypy.ini": "[mypy]\nwarn_unused_ignores = True\n"}
        result, flags_seen = _run(files, {"": _UNUSED_IGNORE + _BASE_ERR + _TWO_ERRORS})
        self.assertEqual(result["summary"]["stale_ignores"], 1)
        self.assertEqual(result["summary"]["stale_ignores_source"], "project-config")
        self.assertFalse(any("--warn-unused-ignores" in f for f in flags_seen))


class TestConfigEnables(unittest.TestCase):
    def _check(self, name: str, text: str) -> bool:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / name
            path.write_text(text)
            return mod.config_enables(str(path), "warn_unused_ignores")

    def test_ini_true(self):
        self.assertTrue(self._check("mypy.ini", "[mypy]\nwarn_unused_ignores = True\n"))

    def test_ini_false_is_false(self):
        self.assertFalse(
            self._check("mypy.ini", "[mypy]\nwarn_unused_ignores = False\n")
        )

    def test_strict_implies_it(self):
        self.assertTrue(self._check("setup.cfg", "[mypy]\nstrict = true\n"))

    def test_per_module_only_is_not_whole_tree(self):
        self.assertFalse(
            self._check(
                "mypy.ini", "[mypy]\n[mypy-pkg.*]\nwarn_unused_ignores = True\n"
            )
        )

    @unittest.skipIf(sys.version_info < (3, 11), "tomllib is 3.11+")
    def test_pyproject(self):
        self.assertTrue(
            self._check("pyproject.toml", "[tool.mypy]\nwarn_unused_ignores = true\n")
        )
        self.assertFalse(
            self._check("pyproject.toml", "[tool.ruff]\nline-length = 1\n")
        )

    def test_no_config(self):
        self.assertFalse(mod.config_enables(None, "warn_unused_ignores"))


class TestUnreachablePass(unittest.TestCase):
    """#64: a labelled --warn-unreachable pass finds blocks a wrong annotation
    has removed from checking, which the phantom-import pass cannot see."""

    def test_only_what_the_pass_adds_is_reported_and_labelled(self):
        result, _ = _run(
            _PKG,
            {
                "": _BASE_ERR + _ONE_ERROR,
                "--warn-unreachable": _BASE_ERR + _UNREACHABLE + _TWO_ERRORS,
            },
        )
        self.assertEqual(result["summary"]["unreachable"], 1)
        (f,) = result["unreachable_findings"]
        self.assertEqual(
            (f["line"], f["code"], f["pass"]), (7, "unreachable", "warn-unreachable")
        )
        self.assertEqual(result["summary"]["total"], 1)
        self.assertEqual([x["code"] for x in result["findings"]], ["arg-type"])

    def test_skipped_pass_is_null_not_zero(self):
        result, flags_seen = _run(
            _PKG,
            {"": _BASE_ERR + _ONE_ERROR},
            unreachable_pass=False,
            strict_pass=False,
        )
        self.assertIsNone(result["summary"]["unreachable"])
        self.assertIsNone(result["summary"]["phantom_imports"])
        self.assertFalse(result["summary"]["strict_pass_ran"])
        self.assertFalse(any("--warn-unreachable" in f for f in flags_seen))

    def test_clean_pass_is_a_measured_zero(self):
        result, _ = _run(_PKG, {"": _BASE_ERR + _ONE_ERROR})
        self.assertEqual(result["summary"]["unreachable"], 0)
        self.assertEqual(result["summary"]["phantom_imports"], 0)
        self.assertTrue(result["summary"]["labelled_passes"]["warn-unreachable"]["ran"])


class TestUnreachableSource(unittest.TestCase):
    """With warn_unreachable on in the project's config, the labelled pass is
    a no-op: its `added: 0` read as a measurement. The baseline is the source."""

    def test_project_config_is_the_source_and_no_pass_runs(self):
        files = {**_PKG, "mypy.ini": "[mypy]\nwarn_unreachable = True\n"}
        result, flags_seen = _run(files, {"": _UNREACHABLE + _BASE_ERR + _TWO_ERRORS})
        summary = result["summary"]
        self.assertEqual(summary["unreachable_source"], "project-config")
        self.assertEqual(summary["unreachable"], 1)
        self.assertEqual(result["unreachable_findings"][0]["line"], 7)
        self.assertNotIn("warn-unreachable", summary["labelled_passes"])
        self.assertFalse(any("--warn-unreachable" in f for f in flags_seen))

    def test_strict_does_not_imply_warn_unreachable(self):
        files = {**_PKG, "mypy.ini": "[mypy]\nstrict = True\n"}
        result, flags_seen = _run(files, {"": _BASE_ERR + _ONE_ERROR})
        self.assertEqual(result["summary"]["unreachable_source"], "warn-unreachable-pass")
        self.assertTrue(any("--warn-unreachable" in f for f in flags_seen))

    def test_without_it_the_labelled_pass_is_the_source(self):
        result, _ = _run(_PKG, {"": _BASE_ERR + _ONE_ERROR})
        self.assertEqual(result["summary"]["unreachable_source"], "warn-unreachable-pass")

    def test_skipped_pass_has_no_source(self):
        result, _ = _run(_PKG, {"": _BASE_ERR + _ONE_ERROR}, unreachable_pass=False)
        self.assertIsNone(result["summary"]["unreachable_source"])


class TestConfigDiscovery(unittest.TestCase):
    def test_pyproject_without_a_mypy_section_is_not_a_config(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text("[tool.ruff]\nline-length = 88\n")
            self.assertIsNone(mod.find_mypy_config(root))

    def test_pyproject_with_a_mypy_section_is_a_config(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text("[tool.mypy]\nstrict = true\n")
            self.assertEqual(mod.find_mypy_config(root), str(root / "pyproject.toml"))

    def test_mypy_ini_wins_over_pyproject(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "mypy.ini").write_text("[mypy]\n")
            (root / "pyproject.toml").write_text("[tool.mypy]\n")
            self.assertEqual(mod.find_mypy_config(root), str(root / "mypy.ini"))


if __name__ == "__main__":
    unittest.main()
