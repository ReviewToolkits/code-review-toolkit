"""Tests for run_external_tools.py."""

import json
import os
import subprocess
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from helpers import TempProject, import_script

mod = import_script("run_external_tools")


# ---------------------------------------------------------------------------
# Project configuration detection
# ---------------------------------------------------------------------------


class TestHasProjectConfig(unittest.TestCase):
    """Test has_project_config for various tools and config file layouts."""

    def test_ruff_in_pyproject(self):
        with TempProject({}) as root:
            (root / "pyproject.toml").write_text(
                '[project]\nname = "x"\n\n[tool.ruff]\nline-length = 99\n',
                encoding="utf-8",
            )
            self.assertTrue(mod.has_project_config("ruff", root))

    def test_ruff_toml(self):
        with TempProject({}) as root:
            (root / "ruff.toml").write_text("line-length = 99\n", encoding="utf-8")
            self.assertTrue(mod.has_project_config("ruff", root))

    def test_ruff_dot_toml(self):
        with TempProject({}) as root:
            (root / ".ruff.toml").write_text("line-length = 99\n", encoding="utf-8")
            self.assertTrue(mod.has_project_config("ruff", root))

    def test_ruff_no_config(self):
        with TempProject({}) as root:
            self.assertFalse(mod.has_project_config("ruff", root))

    def test_mypy_in_pyproject(self):
        with TempProject({}) as root:
            (root / "pyproject.toml").write_text(
                '[project]\nname = "x"\n\n[tool.mypy]\nstrict = true\n',
                encoding="utf-8",
            )
            self.assertTrue(mod.has_project_config("mypy", root))

    def test_mypy_ini(self):
        with TempProject({}) as root:
            (root / "mypy.ini").write_text("[mypy]\nstrict = True\n", encoding="utf-8")
            self.assertTrue(mod.has_project_config("mypy", root))

    def test_mypy_in_setup_cfg(self):
        with TempProject({}) as root:
            (root / "setup.cfg").write_text("[mypy]\nstrict = True\n", encoding="utf-8")
            self.assertTrue(mod.has_project_config("mypy", root))

    def test_mypy_no_config(self):
        with TempProject({}) as root:
            self.assertFalse(mod.has_project_config("mypy", root))

    def test_vulture_in_pyproject(self):
        with TempProject({}) as root:
            (root / "pyproject.toml").write_text(
                '[project]\nname = "x"\n\n[tool.vulture]\nmin_confidence = 80\n',
                encoding="utf-8",
            )
            self.assertTrue(mod.has_project_config("vulture", root))

    def test_vulture_whitelist(self):
        with TempProject({}) as root:
            (root / ".vulture_whitelist.py").write_text("", encoding="utf-8")
            self.assertTrue(mod.has_project_config("vulture", root))

    def test_vulture_no_config(self):
        with TempProject({}) as root:
            self.assertFalse(mod.has_project_config("vulture", root))

    def test_coverage_in_pyproject(self):
        with TempProject({}) as root:
            (root / "pyproject.toml").write_text(
                '[project]\nname = "x"\n\n[tool.coverage]\n',
                encoding="utf-8",
            )
            self.assertTrue(mod.has_project_config("coverage", root))

    def test_coveragerc(self):
        with TempProject({}) as root:
            (root / ".coveragerc").write_text("[run]\nbranch = true\n", encoding="utf-8")
            self.assertTrue(mod.has_project_config("coverage", root))

    def test_coverage_in_setup_cfg(self):
        with TempProject({}) as root:
            (root / "setup.cfg").write_text(
                "[coverage:run]\nbranch = true\n", encoding="utf-8"
            )
            self.assertTrue(mod.has_project_config("coverage", root))

    def test_coverage_no_config(self):
        with TempProject({}) as root:
            self.assertFalse(mod.has_project_config("coverage", root))

    def test_unknown_tool(self):
        with TempProject({}) as root:
            self.assertFalse(mod.has_project_config("nonexistent", root))


class TestFindConfigFile(unittest.TestCase):
    """Test find_config_file returns the correct file path."""

    def test_ruff_toml(self):
        with TempProject({}) as root:
            (root / "ruff.toml").write_text("", encoding="utf-8")
            self.assertEqual(mod.find_config_file("ruff", root), "ruff.toml")

    def test_ruff_pyproject(self):
        with TempProject({}) as root:
            (root / "pyproject.toml").write_text(
                '[project]\nname = "x"\n\n[tool.ruff]\n', encoding="utf-8"
            )
            self.assertEqual(mod.find_config_file("ruff", root), "pyproject.toml")

    def test_ruff_none(self):
        with TempProject({}) as root:
            self.assertIsNone(mod.find_config_file("ruff", root))

    def test_mypy_ini(self):
        with TempProject({}) as root:
            (root / "mypy.ini").write_text("[mypy]\n", encoding="utf-8")
            self.assertEqual(mod.find_config_file("mypy", root), "mypy.ini")

    def test_coverage_coveragerc(self):
        with TempProject({}) as root:
            (root / ".coveragerc").write_text("[run]\n", encoding="utf-8")
            self.assertEqual(mod.find_config_file("coverage", root), ".coveragerc")

    def test_unknown_tool(self):
        with TempProject({}) as root:
            self.assertIsNone(mod.find_config_file("nonexistent", root))


# ---------------------------------------------------------------------------
# Coverage artifact discovery
# ---------------------------------------------------------------------------


class TestFindCoverageArtifacts(unittest.TestCase):
    """Test find_coverage_artifacts finds various coverage formats."""

    def test_finds_coverage_xml(self):
        with TempProject({}) as root:
            (root / "coverage.xml").write_text("<coverage/>", encoding="utf-8")
            artifacts = mod.find_coverage_artifacts(root)
            self.assertEqual(len(artifacts), 1)
            self.assertEqual(artifacts[0]["format"], "cobertura_xml")
            self.assertEqual(artifacts[0]["path"], "coverage.xml")

    def test_finds_coverage_json(self):
        with TempProject({}) as root:
            (root / "coverage.json").write_text("{}", encoding="utf-8")
            artifacts = mod.find_coverage_artifacts(root)
            self.assertEqual(len(artifacts), 1)
            self.assertEqual(artifacts[0]["format"], "json")

    def test_finds_dot_coverage(self):
        with TempProject({}) as root:
            (root / ".coverage").write_text("", encoding="utf-8")
            artifacts = mod.find_coverage_artifacts(root)
            self.assertEqual(len(artifacts), 1)
            self.assertEqual(artifacts[0]["format"], "sqlite")

    def test_skips_nonexistent(self):
        with TempProject({}) as root:
            artifacts = mod.find_coverage_artifacts(root)
            self.assertEqual(artifacts, [])

    def test_finds_in_subdirectory(self):
        with TempProject({}) as root:
            reports = root / "reports"
            reports.mkdir()
            (reports / "coverage.xml").write_text("<coverage/>", encoding="utf-8")
            artifacts = mod.find_coverage_artifacts(root)
            self.assertEqual(len(artifacts), 1)
            self.assertIn("reports/coverage.xml", artifacts[0]["path"])

    def test_finds_multiple_formats(self):
        with TempProject({}) as root:
            (root / "coverage.xml").write_text("<coverage/>", encoding="utf-8")
            (root / "coverage.json").write_text("{}", encoding="utf-8")
            artifacts = mod.find_coverage_artifacts(root)
            self.assertEqual(len(artifacts), 2)


# ---------------------------------------------------------------------------
# Coverage freshness
# ---------------------------------------------------------------------------


class TestCoverageFreshness(unittest.TestCase):
    """Test assess_coverage_freshness for fresh, stale, and unknown cases."""

    def test_fresh_coverage(self):
        with TempProject({"src/main.py": "x = 1\n"}) as root:
            # Source file exists (just created).  Make coverage newer.
            (root / "coverage.xml").write_text("<coverage/>", encoding="utf-8")
            future_time = time.time() + 100
            os.utime(root / "coverage.xml", (future_time, future_time))

            artifacts = mod.find_coverage_artifacts(root)
            freshness = mod.assess_coverage_freshness(artifacts, root)
            self.assertEqual(freshness["status"], "fresh")

    def test_slightly_stale_coverage(self):
        with TempProject({"src/main.py": "x = 1\n"}) as root:
            (root / "coverage.xml").write_text("<coverage/>", encoding="utf-8")
            old_time = time.time() - (2 * 86400)
            os.utime(root / "coverage.xml", (old_time, old_time))

            artifacts = mod.find_coverage_artifacts(root)
            freshness = mod.assess_coverage_freshness(artifacts, root)
            self.assertEqual(freshness["status"], "slightly_stale")

    def test_stale_coverage(self):
        with TempProject({"src/main.py": "x = 1\n"}) as root:
            (root / "coverage.xml").write_text("<coverage/>", encoding="utf-8")
            old_time = time.time() - (5 * 86400)
            os.utime(root / "coverage.xml", (old_time, old_time))

            artifacts = mod.find_coverage_artifacts(root)
            freshness = mod.assess_coverage_freshness(artifacts, root)
            self.assertEqual(freshness["status"], "stale")

    def test_no_python_files(self):
        with TempProject({}) as root:
            (root / "coverage.xml").write_text("<coverage/>", encoding="utf-8")
            artifacts = mod.find_coverage_artifacts(root)
            freshness = mod.assess_coverage_freshness(artifacts, root)
            self.assertEqual(freshness["status"], "unknown")

    def test_no_artifacts(self):
        with TempProject({"src/main.py": "x = 1\n"}) as root:
            freshness = mod.assess_coverage_freshness([], root)
            self.assertEqual(freshness["status"], "unknown")


# ---------------------------------------------------------------------------
# Configured-but-not-installed detection
# ---------------------------------------------------------------------------


class TestDetectConfiguredMissing(unittest.TestCase):
    """Test detect_configured_missing reports tools with config but no binary."""

    def test_configured_but_missing(self):
        availability = {
            "ruff": {
                "available": False,
                "project_config": True,
                "config_file": "pyproject.toml",
            },
            "mypy": {"available": True, "project_config": False},
            "vulture": {"available": False, "project_config": False},
            "coverage": {"available": False},
        }
        with TempProject({}) as root:
            missing = mod.detect_configured_missing(root, availability)
            self.assertEqual(len(missing), 1)
            self.assertEqual(missing[0]["tool"], "ruff")
            self.assertIn("not installed", missing[0]["message"])

    def test_nothing_missing(self):
        availability = {
            "ruff": {"available": True, "project_config": True},
            "mypy": {"available": True, "project_config": False},
            "vulture": {"available": False, "project_config": False},
            "coverage": {"available": False},
        }
        with TempProject({}) as root:
            missing = mod.detect_configured_missing(root, availability)
            self.assertEqual(missing, [])


# ---------------------------------------------------------------------------
# Ruff normalization
# ---------------------------------------------------------------------------


class TestRuffCategoryClassification(unittest.TestCase):
    """Test classify_ruff_category maps rule prefixes correctly."""

    def test_f401_is_dead_code(self):
        self.assertEqual(mod.classify_ruff_category("F401"), "dead-code")

    def test_f841_is_dead_code(self):
        self.assertEqual(mod.classify_ruff_category("F841"), "dead-code")

    def test_f811_is_dead_code(self):
        self.assertEqual(mod.classify_ruff_category("F811"), "dead-code")

    def test_f821_is_bug_risk(self):
        self.assertEqual(mod.classify_ruff_category("F821"), "bug-risk")

    def test_b_prefix_is_bug_risk(self):
        self.assertEqual(mod.classify_ruff_category("B017"), "bug-risk")

    def test_sim_prefix(self):
        self.assertEqual(mod.classify_ruff_category("SIM102"), "simplification")

    def test_s_prefix(self):
        self.assertEqual(mod.classify_ruff_category("S101"), "security")

    def test_up_prefix(self):
        self.assertEqual(mod.classify_ruff_category("UP006"), "deprecated")

    def test_perf_prefix(self):
        self.assertEqual(mod.classify_ruff_category("PERF401"), "performance")

    def test_pie_prefix(self):
        self.assertEqual(mod.classify_ruff_category("PIE810"), "dead-code")

    def test_unknown_prefix(self):
        self.assertEqual(mod.classify_ruff_category("E501"), "style")


class TestRuffSeverityClassification(unittest.TestCase):
    """Test classify_ruff_severity maps rules to severity levels."""

    def test_f821_is_error(self):
        self.assertEqual(mod.classify_ruff_severity("F821"), "error")

    def test_b017_is_error(self):
        self.assertEqual(mod.classify_ruff_severity("B017"), "error")

    def test_s101_is_warning(self):
        self.assertEqual(mod.classify_ruff_severity("S101"), "warning")

    def test_f401_is_warning(self):
        self.assertEqual(mod.classify_ruff_severity("F401"), "warning")


class TestNormalizeRuffFinding(unittest.TestCase):
    """Test normalize_ruff_finding produces correct output."""

    def test_fixable_finding(self):
        finding = {
            "code": "F401",
            "message": "os imported but unused",
            "filename": "/project/src/main.py",
            "location": {"row": 1, "column": 8},
            "fix": {"applicability": "safe"},
        }
        result = mod.normalize_ruff_finding(finding, Path("/project"))
        self.assertEqual(result["rule"], "F401")
        self.assertEqual(result["file"], "src/main.py")
        self.assertEqual(result["line"], 1)
        self.assertEqual(result["category"], "dead-code")
        self.assertTrue(result["fixable"])
        self.assertEqual(result["significance"], "reduced")

    def test_non_fixable_finding(self):
        finding = {
            "code": "B017",
            "message": "assertRaises(Exception)",
            "filename": "/project/tests/test_x.py",
            "location": {"row": 10, "column": 1},
            "fix": None,
        }
        result = mod.normalize_ruff_finding(finding, Path("/project"))
        self.assertFalse(result["fixable"])
        self.assertEqual(result["significance"], "normal")
        self.assertEqual(result["severity"], "error")


# ---------------------------------------------------------------------------
# Mypy parsing
# ---------------------------------------------------------------------------


class TestMypyTextParsing(unittest.TestCase):
    """Test parse_mypy_text_line with various mypy output formats."""

    def test_standard_error(self):
        line = 'src/main.py:10: error: Incompatible types [assignment]'
        result = mod.parse_mypy_text_line(line)
        self.assertIsNotNone(result)
        self.assertEqual(result["file"], "src/main.py")
        self.assertEqual(result["line"], 10)
        self.assertEqual(result["severity"], "error")
        self.assertEqual(result["code"], "assignment")

    def test_with_column(self):
        line = 'src/main.py:10:5: error: Name "x" is not defined [name-defined]'
        result = mod.parse_mypy_text_line(line)
        self.assertIsNotNone(result)
        self.assertEqual(result["column"], 5)
        self.assertEqual(result["code"], "name-defined")

    def test_warning(self):
        line = 'src/main.py:5: warning: Unused "type: ignore" comment'
        result = mod.parse_mypy_text_line(line)
        self.assertIsNotNone(result)
        self.assertEqual(result["severity"], "warning")
        self.assertEqual(result["code"], "")

    def test_note(self):
        line = 'src/main.py:1: note: See https://example.com'
        result = mod.parse_mypy_text_line(line)
        self.assertIsNotNone(result)
        self.assertEqual(result["severity"], "note")

    def test_non_matching_line(self):
        self.assertIsNone(mod.parse_mypy_text_line("Success: no issues found"))

    def test_empty_line(self):
        self.assertIsNone(mod.parse_mypy_text_line(""))


class TestMypyOutputParsing(unittest.TestCase):
    """Test parse_mypy_output handles JSON and text output."""

    def test_json_lines(self):
        stdout = (
            '{"file": "a.py", "line": 1, "column": 0, '
            '"severity": "error", "message": "bad", "code": "misc"}\n'
        )
        findings = mod.parse_mypy_output(stdout)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["file"], "a.py")

    def test_text_lines(self):
        stdout = 'a.py:1: error: Something wrong [misc]\n'
        findings = mod.parse_mypy_output(stdout)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["code"], "misc")

    def test_mixed_output(self):
        stdout = (
            '{"file": "a.py", "line": 1, "severity": "error", '
            '"message": "bad", "code": "misc"}\n'
            'b.py:2: error: Also bad [misc]\n'
        )
        findings = mod.parse_mypy_output(stdout)
        self.assertEqual(len(findings), 2)

    def test_empty_output(self):
        self.assertEqual(mod.parse_mypy_output(""), [])


# ---------------------------------------------------------------------------
# Vulture parsing
# ---------------------------------------------------------------------------


class TestVultureParsing(unittest.TestCase):
    """Test parse_vulture_output parses vulture's text format."""

    def test_unused_function(self):
        stdout = "src/main.py:42: unused function 'helper' (90% confidence)\n"
        findings = mod.parse_vulture_output(stdout, Path("/project"))
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["type"], "unused-function")
        self.assertEqual(findings[0]["name"], "helper")
        self.assertEqual(findings[0]["line"], 42)
        self.assertEqual(findings[0]["confidence"], 90)
        self.assertEqual(findings[0]["category"], "dead-code")

    def test_unused_variable(self):
        stdout = "src/main.py:10: unused variable 'x' (80% confidence)\n"
        findings = mod.parse_vulture_output(stdout, Path("/project"))
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["type"], "unused-variable")

    def test_unused_import(self):
        stdout = "src/main.py:1: unused import 'os' (90% confidence)\n"
        findings = mod.parse_vulture_output(stdout, Path("/project"))
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["type"], "unused-import")

    def test_multiple_findings(self):
        stdout = (
            "a.py:1: unused import 'os' (90% confidence)\n"
            "b.py:2: unused function 'foo' (80% confidence)\n"
        )
        findings = mod.parse_vulture_output(stdout, Path("/project"))
        self.assertEqual(len(findings), 2)

    def test_empty_output(self):
        self.assertEqual(mod.parse_vulture_output("", Path("/")), [])

    def test_non_matching_lines_skipped(self):
        stdout = "some random output\na.py:1: unused import 'os' (90% confidence)\n"
        findings = mod.parse_vulture_output(stdout, Path("/project"))
        self.assertEqual(len(findings), 1)


# ---------------------------------------------------------------------------
# Coverage XML parsing
# ---------------------------------------------------------------------------


class TestCoverageXmlParsing(unittest.TestCase):
    """Test parse_coverage_xml reads Cobertura XML correctly."""

    _FIXTURE = """\
<?xml version="1.0" ?>
<coverage version="7.0" timestamp="1234" lines-valid="10" lines-covered="7"
          line-rate="0.7" branches-valid="0" branches-covered="0"
          branch-rate="0" complexity="0">
    <packages>
        <package name="pkg" line-rate="0.7" branch-rate="0" complexity="0">
            <classes>
                <class name="mod.py" filename="pkg/mod.py"
                       line-rate="0.7" branch-rate="0" complexity="0">
                    <lines>
                        <line number="1" hits="1"/>
                        <line number="2" hits="1"/>
                        <line number="3" hits="0"/>
                        <line number="4" hits="1"/>
                        <line number="5" hits="0"/>
                    </lines>
                </class>
            </classes>
        </package>
    </packages>
</coverage>
"""

    def test_basic_parsing(self):
        with TempProject({}) as root:
            (root / "coverage.xml").write_text(self._FIXTURE, encoding="utf-8")
            result = mod.parse_coverage_xml(root / "coverage.xml", root)
            self.assertEqual(result["total_statements"], 5)
            self.assertEqual(result["total_covered"], 3)
            self.assertEqual(result["coverage_percent"], 60.0)
            self.assertEqual(len(result["files"]), 1)
            self.assertEqual(result["files"][0]["file"], "pkg/mod.py")
            self.assertEqual(result["files"][0]["missing_lines"], [3, 5])

    def test_empty_coverage(self):
        with TempProject({}) as root:
            xml = (
                '<?xml version="1.0" ?>'
                '<coverage><packages></packages></coverage>'
            )
            (root / "coverage.xml").write_text(xml, encoding="utf-8")
            result = mod.parse_coverage_xml(root / "coverage.xml", root)
            self.assertEqual(result["total_statements"], 0)
            self.assertEqual(result["coverage_percent"], 0)
            self.assertEqual(result["files"], [])


# ---------------------------------------------------------------------------
# Coverage JSON parsing
# ---------------------------------------------------------------------------


class TestCoverageJsonParsing(unittest.TestCase):
    """Test parse_coverage_json reads coverage.py JSON correctly."""

    def test_basic_parsing(self):
        data = {
            "totals": {
                "num_statements": 20,
                "covered_lines": 18,
                "percent_covered": 90.0,
            },
            "files": {
                "src/main.py": {
                    "summary": {
                        "num_statements": 10,
                        "covered_lines": 8,
                        "percent_covered": 80.0,
                    },
                    "missing_lines": [5, 9],
                },
                "src/util.py": {
                    "summary": {
                        "num_statements": 10,
                        "covered_lines": 10,
                        "percent_covered": 100.0,
                    },
                    "missing_lines": [],
                },
            },
        }
        with TempProject({}) as root:
            (root / "coverage.json").write_text(
                json.dumps(data), encoding="utf-8"
            )
            result = mod.parse_coverage_json(root / "coverage.json", root)
            self.assertEqual(result["total_statements"], 20)
            self.assertEqual(result["total_covered"], 18)
            self.assertEqual(result["coverage_percent"], 90.0)
            self.assertEqual(len(result["files"]), 2)
            # Sorted by coverage_percent ascending.
            self.assertEqual(result["files"][0]["file"], "src/main.py")
            self.assertEqual(result["files"][0]["missing_lines"], [5, 9])

    def test_empty_report(self):
        with TempProject({}) as root:
            (root / "coverage.json").write_text(
                '{"totals": {}, "files": {}}', encoding="utf-8"
            )
            result = mod.parse_coverage_json(root / "coverage.json", root)
            self.assertEqual(result["files"], [])


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------


class TestParseArgs(unittest.TestCase):
    """Test parse_args for correct argument handling."""

    def test_defaults(self):
        args = mod.parse_args([])
        self.assertEqual(args.path, ".")
        self.assertIsNone(args.tools)
        self.assertIsNone(args.skip)
        self.assertEqual(args.max_findings, 200)
        self.assertFalse(args.mypy_strict)
        self.assertEqual(args.vulture_min_confidence, 60)
        self.assertFalse(args.ignore_config)

    def test_tools_flag(self):
        args = mod.parse_args(["--tools", "ruff,mypy"])
        self.assertEqual(args.tools, "ruff,mypy")

    def test_skip_flag(self):
        args = mod.parse_args(["--skip", "vulture"])
        self.assertEqual(args.skip, "vulture")

    def test_tools_and_skip_conflict(self):
        with self.assertRaises(SystemExit):
            mod.parse_args(["--tools", "ruff", "--skip", "mypy"])

    def test_max_findings(self):
        args = mod.parse_args(["--max-findings", "50"])
        self.assertEqual(args.max_findings, 50)

    def test_mypy_strict(self):
        args = mod.parse_args(["--mypy-strict"])
        self.assertTrue(args.mypy_strict)

    def test_vulture_min_confidence(self):
        args = mod.parse_args(["--vulture-min-confidence", "80"])
        self.assertEqual(args.vulture_min_confidence, 80)

    def test_ignore_config(self):
        args = mod.parse_args(["--ignore-config"])
        self.assertTrue(args.ignore_config)

    def test_respect_config(self):
        args = mod.parse_args(["--respect-config"])
        self.assertFalse(args.ignore_config)

    def test_path_argument(self):
        args = mod.parse_args(["src/pkg"])
        self.assertEqual(args.path, "src/pkg")

    def test_coverage_require_fresh(self):
        args = mod.parse_args(["--coverage-require-fresh"])
        self.assertTrue(args.coverage_require_fresh)


# ---------------------------------------------------------------------------
# Tool selection resolution
# ---------------------------------------------------------------------------


class TestResolveToolSelection(unittest.TestCase):
    """Test resolve_tool_selection with various CLI flags and availability."""

    def _make_args(self, tools=None, skip=None):
        return mod.parse_args(
            (["--tools", tools] if tools else [])
            + (["--skip", skip] if skip else [])
        )

    def _availability(self, **available):
        return {
            name: {"available": available.get(name, False)}
            for name in ("ruff", "mypy", "vulture", "coverage")
        }

    def test_all_with_only_ruff(self):
        args = self._make_args()
        avail = self._availability(ruff=True)
        result = mod.resolve_tool_selection(args, avail)
        self.assertEqual(result, ["ruff"])

    def test_tools_mypy_not_available(self):
        args = self._make_args(tools="mypy")
        avail = self._availability(ruff=True)
        result = mod.resolve_tool_selection(args, avail)
        self.assertEqual(result, [])

    def test_skip_ruff(self):
        args = self._make_args(skip="ruff")
        avail = self._availability(ruff=True, mypy=True)
        result = mod.resolve_tool_selection(args, avail)
        self.assertEqual(result, ["mypy"])

    def test_all_available(self):
        args = self._make_args()
        avail = self._availability(ruff=True, mypy=True, vulture=True, coverage=True)
        result = mod.resolve_tool_selection(args, avail)
        self.assertEqual(result, ["ruff", "mypy", "vulture", "coverage"])

    def test_none_available(self):
        args = self._make_args()
        avail = self._availability()
        result = mod.resolve_tool_selection(args, avail)
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------


class TestMakeRelative(unittest.TestCase):
    """Test make_relative with absolute and relative paths."""

    def test_absolute_path(self):
        result = mod.make_relative("/project/src/main.py", Path("/project"))
        self.assertEqual(result, "src/main.py")

    def test_relative_path_passthrough(self):
        result = mod.make_relative("src/main.py", Path("/project"))
        self.assertEqual(result, "src/main.py")

    def test_unrelated_absolute_path(self):
        result = mod.make_relative("/other/main.py", Path("/project"))
        self.assertEqual(result, "/other/main.py")


class TestGetToolVersion(unittest.TestCase):
    """Test get_tool_version with mocked subprocess."""

    def test_returns_version(self):
        with patch.object(mod.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(stdout="ruff 0.8.0\n")
            result = mod.get_tool_version("ruff")
            self.assertEqual(result, "0.8.0")

    def test_returns_none_on_missing(self):
        with patch.object(
            mod.subprocess, "run", side_effect=FileNotFoundError
        ):
            result = mod.get_tool_version("nonexistent")
            self.assertIsNone(result)

    def test_mypy_trailing_build_note_is_not_the_version(self):
        # `mypy --version` ends in "(compiled: yes)"; the last token was taken.
        with patch.object(mod.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(stdout="mypy 1.19.0 (compiled: yes)\n")
            self.assertEqual(mod.get_tool_version("mypy"), "1.19.0")

    def test_other_version_formats(self):
        cases = {
            "vulture 2.16\n": "2.16",
            "mypy 2.4.1+dev.0abc (compiled: no)\n": "2.4.1+dev.0abc",
        }
        for stdout, expected in cases.items():
            with patch.object(mod.subprocess, "run") as mock_run:
                mock_run.return_value = MagicMock(stdout=stdout)
                self.assertEqual(mod.get_tool_version("tool"), expected, stdout)

    def test_no_version_token_is_none(self):
        with patch.object(mod.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(stdout="unknown build\n")
            self.assertIsNone(mod.get_tool_version("tool"))


# ---------------------------------------------------------------------------
# Safe runner
# ---------------------------------------------------------------------------


class TestRunToolSafely(unittest.TestCase):
    """Test run_tool_safely catches errors gracefully."""

    def test_unknown_tool(self):
        args = mod.parse_args([])
        result = mod.run_tool_safely("nonexistent", args, Path("."), Path("."))
        self.assertIn("error", result)
        self.assertEqual(result["total_findings"], 0)

    def test_timeout(self):
        with patch.dict(
            mod._TOOL_RUNNERS,
            {"ruff": MagicMock(side_effect=subprocess.TimeoutExpired("ruff", 120))},
        ):
            args = mod.parse_args([])
            result = mod.run_tool_safely("ruff", args, Path("."), Path("."))
            self.assertIn("timed out", result["error"])

    def test_binary_not_found(self):
        with patch.dict(
            mod._TOOL_RUNNERS,
            {"ruff": MagicMock(side_effect=FileNotFoundError)},
        ):
            args = mod.parse_args([])
            result = mod.run_tool_safely("ruff", args, Path("."), Path("."))
            self.assertIn("not found", result["error"])

    def test_generic_exception(self):
        with patch.dict(
            mod._TOOL_RUNNERS,
            {"ruff": MagicMock(side_effect=RuntimeError("boom"))},
        ):
            args = mod.parse_args([])
            result = mod.run_tool_safely("ruff", args, Path("."), Path("."))
            self.assertIn("boom", result["error"])


# ---------------------------------------------------------------------------
# The reviewed project is left untouched
# ---------------------------------------------------------------------------


class TestNoCacheInReviewedProject(unittest.TestCase):
    """Neither ruff nor mypy may leave a cache directory in the project."""

    def test_ruff_runs_without_a_cache(self):
        with TempProject({"m.py": "x = 1\n"}) as root:
            with patch.object(mod.subprocess, "run") as run:
                run.return_value = MagicMock(stdout="[]", stderr="")
                mod.run_ruff(mod.parse_args([str(root)]), root, root)
            for call in run.call_args_list:
                self.assertIn("--no-cache", call.args[0])

    def test_mypy_cache_is_private_and_removed(self):
        seen: dict = {}

        def fake_run(cmd, **kwargs):
            cache = Path(cmd[cmd.index("--cache-dir") + 1])
            seen["cache"] = cache
            seen["existed_during_run"] = cache.is_dir()
            (cache / "3.14").mkdir()  # mypy populates it
            return MagicMock(stdout="", stderr="")

        with TempProject({"m.py": "x = 1\n"}) as root:
            with patch.object(mod.subprocess, "run", side_effect=fake_run):
                mod.run_mypy(mod.parse_args([str(root)]), root, root)
            self.assertTrue(seen["existed_during_run"])
            self.assertFalse(seen["cache"].is_relative_to(root))
            self.assertFalse(seen["cache"].exists(), "cache left behind")
            self.assertFalse((root / ".mypy_cache").exists())

    def _mypy_call(self, files, argv_extra=()):
        seen: dict = {}

        def fake_run(cmd, **kwargs):
            seen["cmd"] = cmd
            seen["cwd"] = kwargs.get("cwd")
            return MagicMock(stdout="", stderr="")

        with TempProject(files) as root:
            with patch.object(mod.subprocess, "run", side_effect=fake_run):
                result = mod.run_mypy(
                    mod.parse_args([str(root), *argv_extra]), root, root
                )
            return root, seen, result

    def test_mypy_reads_the_project_config_whatever_the_cwd(self):
        # mypy discovers its config from the working directory, which is the
        # agent's private temp dir: the project's [tool.mypy] was ignored.
        root, seen, result = self._mypy_call(
            {"m.py": "x = 1\n", "mypy.ini": "[mypy]\nstrict = True\n"}
        )
        cmd = seen["cmd"]
        self.assertEqual(result["config_source"], "project")
        self.assertEqual(cmd[cmd.index("--config-file") + 1], str(root / "mypy.ini"))
        self.assertEqual(result["config_file"], str(root / "mypy.ini"))
        self.assertEqual(seen["cwd"], str(root))

    def test_curated_mypy_run_reads_no_config_at_all(self):
        # Neither a config in the caller's cwd nor the ignored project one.
        for files, extra in (
            ({"m.py": "x = 1\n"}, ()),
            ({"m.py": "x = 1\n", "mypy.ini": "[mypy]\n"}, ("--ignore-config",)),
        ):
            _root, seen, result = self._mypy_call(files, extra)
            self.assertEqual(result["config_source"], "curated")
            self.assertIn("--config-file=", seen["cmd"])
            self.assertIsNone(result["config_file"])

    def test_mypy_cache_is_removed_even_when_mypy_times_out(self):
        seen: dict = {}

        def fake_run(cmd, **kwargs):
            seen["cache"] = Path(cmd[cmd.index("--cache-dir") + 1])
            raise subprocess.TimeoutExpired(cmd, 300)

        with (
            TempProject({"m.py": "x = 1\n"}) as root,
            patch.object(mod.subprocess, "run", side_effect=fake_run),
        ):
            result = mod.run_tool_safely("mypy", mod.parse_args([str(root)]), root, root)
        self.assertIn("timed out", result["error"])
        self.assertFalse(seen["cache"].exists())


# ---------------------------------------------------------------------------
# Known ruff false positive: except-binding read from a closure (#22)
# ---------------------------------------------------------------------------

EXCEPT_CLOSURE_SOURCE = """\
def retry(fn):
    return fn(None)

def f(job):
    try:
        job()
    except Exception as exc:
        retry(lambda c: str(exc))

def g(job):
    try:
        job()
    except Exception as exc:
        def report():
            return f"failed: {exc}"
        report()

def h():
    return undefined_name

def k(job):
    try:
        job()
    except Exception as exc:
        pass

def m(job):
    try:
        job()
    except Exception as exc:
        pass
    return lambda: exc
"""


def _ruff_hit(root, code, line, name):
    message = {
        "F821": f"Undefined name `{name}`",
        "F841": f"Local variable `{name}` is assigned to but never used",
    }[code]
    return {
        "code": code,
        "filename": str(root / "m.py"),
        "location": {"row": line, "column": 1},
        "message": message,
        "fix": {"applicability": "unsafe"} if code == "F841" else None,
    }


class TestExceptClosureFalsePositive(unittest.TestCase):
    """ruff 0.16.0 flags `exc` read by a closure inside its own handler."""

    def _run(self, hits):
        with TempProject({"m.py": EXCEPT_CLOSURE_SOURCE}) as root:
            hits = [_ruff_hit(root, *h) for h in hits]
            with patch.object(mod.subprocess, "run") as run:
                run.return_value = MagicMock(stdout=json.dumps(hits), stderr="")
                out = mod.run_ruff(
                    mod.parse_args([str(root), "--ruff-rules", "F"]), root, root
                )
        return {(f["rule"], f["line"]): f for f in out["findings"]}, out

    def test_closure_reads_are_labelled_not_bug_risk(self):
        by_site, out = self._run([("F821", 8, "exc"), ("F821", 15, "exc")])
        for site in (("F821", 8), ("F821", 15)):
            with self.subTest(site=site):
                hit = by_site[site]
                self.assertEqual(hit["category"], "suspected-tool-false-positive")
                self.assertTrue(hit["suspect_tool_bug"])
                self.assertEqual(hit["known_tool_issue"], "ruff#6878")
                self.assertIn("escapes", hit["tool_bug_note"])
        self.assertEqual(out["suspected_tool_false_positives"], 2)

    def test_matching_f841_autofix_is_flagged_unsafe(self):
        by_site, _ = self._run([("F841", 7, "exc"), ("F841", 13, "exc")])
        for site in (("F841", 7), ("F841", 13)):
            with self.subTest(site=site):
                hit = by_site[site]
                self.assertTrue(hit["autofix_unsafe"])
                self.assertEqual(hit["category"], "suspected-tool-false-positive")
                self.assertIn("NameError", hit["tool_bug_note"])

    def test_genuine_undefined_name_stays_bug_risk(self):
        by_site, out = self._run([("F821", 19, "undefined_name")])
        self.assertEqual(by_site[("F821", 19)]["category"], "bug-risk")
        self.assertNotIn("suspect_tool_bug", by_site[("F821", 19)])
        self.assertEqual(out["suspected_tool_false_positives"], 0)

    def test_closure_escaping_the_handler_stays_bug_risk(self):
        # `return lambda: exc` AFTER the handler: exc is deleted by then, so
        # this F821 is a real NameError.
        by_site, _ = self._run([("F821", 32, "exc")])
        self.assertEqual(by_site[("F821", 32)]["category"], "bug-risk")

    def test_genuinely_unused_binding_keeps_its_autofix(self):
        by_site, _ = self._run([("F841", 24, "exc")])
        hit = by_site[("F841", 24)]
        self.assertEqual(hit["category"], "dead-code")
        self.assertNotIn("autofix_unsafe", hit)


# ---------------------------------------------------------------------------
# Toolkit-selected second ruff pass for the category feeds (#38/#66)
# ---------------------------------------------------------------------------

FEED_SOURCE = """\
from fastapi import Depends

def route(db=Depends(get_db), items=[]):
    if items:
        return True
    else:
        return False

x = "%s" % db
"""


def _hit(root, code, line, column, message="m"):
    return {
        "code": code,
        "filename": str(root / "app.py"),
        "location": {"row": line, "column": column},
        "message": message,
        "fix": None,
    }


class TestToolkitRuffPass(unittest.TestCase):
    """A narrow project selection must not leave the agents' feeds empty."""

    PYPROJECT = '[project]\nname = "x"\n\n[tool.ruff.lint]\nselect = ["B006"]\n'

    def _run(self, argv=(), pyproject=PYPROJECT, toolkit_error=None):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            toolkit = "--select" in cmd and "UP" in cmd[cmd.index("--select") + 1]
            if toolkit and toolkit_error:
                raise toolkit_error
            hits = [_hit(root, "B006", 3, 37)]  # the project's own rule
            if toolkit:
                hits += [
                    _hit(root, "B008", 3, 14),  # FastAPI marker: dismissed
                    _hit(root, "SIM103", 4, 5),
                    _hit(root, "UP031", 9, 5),
                ]
            return MagicMock(stdout=json.dumps(hits), stderr="")

        with TempProject({"app.py": FEED_SOURCE}) as root:
            (root / "pyproject.toml").write_text(pyproject, encoding="utf-8")
            with patch.object(mod.subprocess, "run", side_effect=fake_run):
                out = mod.run_ruff(mod.parse_args([str(root), *argv]), root, root)
        return out, calls

    def test_feeds_are_filled_and_labelled_as_the_toolkits(self):
        out, _ = self._run()
        by_rule = {f["rule"]: f for f in out["findings"]}
        self.assertEqual(by_rule["B006"]["selection"], "project")
        self.assertEqual(by_rule["SIM103"]["selection"], "toolkit")
        self.assertEqual(by_rule["SIM103"]["category"], "simplification")
        self.assertEqual(by_rule["UP031"]["category"], "deprecated")
        meta = out["toolkit_pass"]
        self.assertTrue(meta["ran"])
        self.assertIn("UP", meta["selection"]["deprecated"])
        self.assertIn("NOT the project's", meta["note"])
        self.assertEqual(meta["by_category"], {"deprecated": 1, "simplification": 1})
        # The project's own numbers still describe the project's pass only.
        self.assertEqual(out["total_findings"], 1)
        self.assertEqual(out["rules_applied"], ["B006"])

    def test_project_hits_are_not_reported_twice(self):
        out, _ = self._run()
        self.assertEqual(sum(f["rule"] == "B006" for f in out["findings"]), 1)
        self.assertEqual(out["toolkit_pass"]["duplicates_of_project_findings"], 1)

    def test_fastapi_markers_are_counted_not_fed(self):
        out, _ = self._run()
        self.assertNotIn("B008", {f["rule"] for f in out["findings"]})
        self.assertEqual(
            out["toolkit_pass"]["dismissed"], {"fastapi-dependency-marker": 1}
        )

    def test_toolkit_command_keeps_the_project_config_and_no_cache(self):
        _, calls = self._run()
        toolkit_cmd = calls[1]
        self.assertIn("--no-cache", toolkit_cmd)
        self.assertNotIn("--isolated", toolkit_cmd)
        # target-version comes from the project's config.
        self.assertNotIn("--target-version", toolkit_cmd)

    def test_without_a_ruff_config_the_target_version_is_pinned(self):
        _, calls = self._run(
            pyproject='[project]\nname = "x"\nrequires-python = ">=3.11"\n'
        )
        toolkit_cmd = calls[1]
        index = toolkit_cmd.index("--target-version")
        self.assertEqual(toolkit_cmd[index + 1], "py311")

    def test_explicit_rules_or_the_flag_skip_the_pass(self):
        for argv, reason in (
            (["--ruff-rules", "F"], "explicit --ruff-rules selection"),
            (["--no-toolkit-ruff-pass"], "--no-toolkit-ruff-pass"),
        ):
            with self.subTest(argv=argv):
                out, calls = self._run(argv)
                self.assertEqual(len(calls), 1)
                self.assertFalse(out["toolkit_pass"]["ran"])
                self.assertEqual(out["toolkit_pass"]["skipped_reason"], reason)

    def test_a_failed_toolkit_pass_keeps_the_project_findings(self):
        out, _ = self._run(toolkit_error=subprocess.TimeoutExpired("ruff", 120))
        self.assertEqual([f["rule"] for f in out["findings"]], ["B006"])
        self.assertFalse(out["toolkit_pass"]["ran"])
        self.assertIn("failed", out["toolkit_pass"]["error"])


# ---------------------------------------------------------------------------
# Integration: full pipeline with mocked tools
# ---------------------------------------------------------------------------


class TestIntegrationNoTools(unittest.TestCase):
    """Test the full pipeline when no tools are installed."""

    def test_no_tools_produces_valid_json(self):
        with patch.object(mod.shutil, "which", return_value=None):
            with TempProject({"src/main.py": "x = 1\n"}) as root:
                result = mod.analyze([str(root)])
                self.assertIn("project_root", result)
                self.assertIn("tools_available", result)
                self.assertIn("tools_run", result)
                self.assertEqual(result["tools_run"], [])
                # Verify JSON serializable.
                json.dumps(result)

    def test_no_tools_reports_skipped(self):
        with patch.object(mod.shutil, "which", return_value=None):
            with TempProject({"src/main.py": "x = 1\n"}) as root:
                result = mod.analyze([str(root)])
                skipped_names = [s["tool"] for s in result["tools_skipped"]]
                self.assertIn("ruff", skipped_names)
                self.assertIn("mypy", skipped_names)
                self.assertIn("vulture", skipped_names)


class TestIntegrationConfiguredMissing(unittest.TestCase):
    """Test that configured-but-not-installed tools are reported."""

    def test_reports_configured_ruff(self):
        with patch.object(mod.shutil, "which", return_value=None):
            with TempProject({}) as root:
                (root / "pyproject.toml").write_text(
                    '[project]\nname = "x"\n\n[tool.ruff]\nline-length = 99\n',
                    encoding="utf-8",
                )
                result = mod.analyze([str(root)])
                configured = result["configured_but_not_installed"]
                self.assertEqual(len(configured), 1)
                self.assertEqual(configured[0]["tool"], "ruff")


class TestBuildSkippedReport(unittest.TestCase):
    """Test build_skipped_report distinguishes reasons."""

    def test_not_installed_vs_excluded(self):
        availability = {
            "ruff": {"available": True},
            "mypy": {"available": False},
            "vulture": {"available": True},
            "coverage": {"available": False},
        }
        skipped = mod.build_skipped_report(availability, ["ruff"])
        reasons = {s["tool"]: s["reason"] for s in skipped}
        self.assertEqual(reasons["mypy"], "not installed")
        self.assertEqual(reasons["vulture"], "excluded by user")
        # Coverage is read from artifacts: a missing artifact is not a missing
        # package.
        self.assertEqual(reasons["coverage"], "no coverage artifacts")

    def test_coverage_installed_without_artifacts(self):
        availability = {
            "ruff": {"available": True},
            "mypy": {"available": True},
            "vulture": {"available": True},
            "coverage": {
                "available": False, "artifacts": [], "coverage_importable": True,
            },
        }
        skipped = mod.build_skipped_report(availability, ["ruff", "mypy", "vulture"])
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["reason"], "no coverage artifacts")
        self.assertTrue(skipped[0]["coverage_importable"])
        self.assertIn("coverage run", skipped[0]["hint"])


FAKE_RUFF = """\
#!{python}
import sys

if {fail}:
    # What a sandboxed (snap) ruff prints for a path it cannot see.
    sys.stderr.write(
        "warning: Failed to lint " + sys.argv[-1] + ": No such file or directory"
        " (os error 2)\\n"
    )
sys.stdout.write("[]")
"""


class TestUnlintedPaths(unittest.TestCase):
    """ruff exits 0 with no findings for a path it could not read."""

    def _run(self, *, fail):
        import sys
        import tempfile

        with (
            tempfile.TemporaryDirectory() as bindir,
            TempProject({"m.py": "x = 1\n"}) as root,
        ):
            ruff = Path(bindir) / "ruff"
            ruff.write_text(
                FAKE_RUFF.format(python=sys.executable, fail=fail), encoding="utf-8"
            )
            os.chmod(ruff, 0o755)
            path = bindir + os.pathsep + os.environ.get("PATH", "")
            with patch.dict(os.environ, {"PATH": path}):
                return mod.run_ruff(mod.parse_args([str(root)]), root, root)

    def test_failed_to_lint_is_an_error_not_a_clean_result(self):
        out = self._run(fail=True)
        self.assertIn("could not read", out["error"])
        self.assertEqual(len(out["unlinted"]), 1)
        self.assertIn("could not read", out["toolkit_pass"]["error"])
        self.assertEqual(len(out["toolkit_pass"]["unlinted"]), 1)

    def test_a_clean_run_has_no_error(self):
        out = self._run(fail=False)
        self.assertNotIn("error", out)
        self.assertEqual(out["unlinted"], [])
        self.assertNotIn("error", out["toolkit_pass"])


VULTURE_OUTPUT = """\
pkg/a.py:3: unused import 'os' (90% confidence)
pkg/a.py:10: unused method '_open_writer' (60% confidence)
pkg/a.py:20: unused function 'helper' (60% confidence)
pkg/a.py:30: unused variable 'exc_tb' (100% confidence)
"""


class TestVultureConfidence(unittest.TestCase):
    """Unused methods are scored 60% by vulture: the default must keep them."""

    def _run(self, argv=()):
        with (
            TempProject({"pkg/a.py": "x = 1\n"}) as root,
            patch.object(mod.subprocess, "run") as run,
        ):
            run.return_value = MagicMock(stdout=VULTURE_OUTPUT, stderr="")
            out = mod.run_vulture(mod.parse_args([str(root), *argv]), root, root)
        return out, run.call_args.args[0]

    def test_default_threshold_keeps_unused_methods(self):
        _, cmd = self._run()
        self.assertIn("--min-confidence=60", cmd)

    def test_confidence_is_reported_per_hit_and_bucketed(self):
        out, _ = self._run()
        self.assertEqual(
            {f["name"]: f["confidence"] for f in out["findings"]},
            {"os": 90, "_open_writer": 60, "helper": 60, "exc_tb": 100},
        )
        self.assertEqual(out["by_confidence"], {"100": 1, "90": 1, "60": 2})
        self.assertEqual(
            out["by_type"],
            {
                "unused-function": 1,
                "unused-import": 1,
                "unused-method": 1,
                "unused-variable": 1,
            },
        )

    def test_capping_is_reported_and_buckets_count_every_hit(self):
        out, _ = self._run(["--max-findings", "2"])
        self.assertEqual(len(out["findings"]), 2)
        self.assertEqual(out["total_findings"], 4)
        self.assertTrue(out["findings_capped"])
        self.assertEqual(sum(out["by_confidence"].values()), 4)

    def test_mypy_capping_is_reported(self):
        lines = "\n".join(f"a.py:{i}: error: Bad [misc]" for i in range(5))
        with (
            TempProject({"a.py": "x = 1\n"}) as root,
            patch.object(mod.subprocess, "run") as run,
        ):
            run.return_value = MagicMock(stdout=lines, stderr="")
            out = mod.run_mypy(
                mod.parse_args([str(root), "--max-findings", "2"]), root, root
            )
        self.assertEqual(len(out["findings"]), 2)
        self.assertEqual(out["total_findings"], 5)
        self.assertTrue(out["findings_capped"])

    def test_an_explicit_threshold_is_still_honoured(self):
        _, cmd = self._run(["--vulture-min-confidence", "80"])
        self.assertIn("--min-confidence=80", cmd)


class TestEarlyStopParsing(unittest.TestCase):
    """Test early-stop parsing at max_findings limit."""

    def test_mypy_early_stop(self):
        lines = "\n".join(
            [f"a.py:{i}: error: Bad [misc]" for i in range(100)]
        )
        findings = mod.parse_mypy_output(lines, max_findings=5)
        self.assertEqual(len(findings), 5)

    def test_mypy_no_limit(self):
        lines = "\n".join(
            [f"a.py:{i}: error: Bad [misc]" for i in range(10)]
        )
        findings = mod.parse_mypy_output(lines, max_findings=0)
        self.assertEqual(len(findings), 10)

    def test_vulture_early_stop(self):
        lines = "\n".join(
            [
                f"a.py:{i}: unused function 'f{i}' (90% confidence)"
                for i in range(100)
            ]
        )
        findings = mod.parse_vulture_output(
            lines, Path("/project"), max_findings=5,
        )
        self.assertEqual(len(findings), 5)

    def test_vulture_no_limit(self):
        lines = "\n".join(
            [
                f"a.py:{i}: unused function 'f{i}' (90% confidence)"
                for i in range(10)
            ]
        )
        findings = mod.parse_vulture_output(
            lines, Path("/project"), max_findings=0,
        )
        self.assertEqual(len(findings), 10)


if __name__ == "__main__":
    unittest.main()
