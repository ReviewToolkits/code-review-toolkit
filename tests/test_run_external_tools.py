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
        self.assertEqual(args.vulture_min_confidence, 80)
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
        args = mod.parse_args(["--vulture-min-confidence", "60"])
        self.assertEqual(args.vulture_min_confidence, 60)

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

    def test_mypy_cache_is_removed_even_when_mypy_times_out(self):
        seen: dict = {}

        def fake_run(cmd, **kwargs):
            seen["cache"] = Path(cmd[cmd.index("--cache-dir") + 1])
            raise subprocess.TimeoutExpired(cmd, 300)

        with TempProject({"m.py": "x = 1\n"}) as root:
            with patch.object(mod.subprocess, "run", side_effect=fake_run):
                result = mod.run_tool_safely(
                    "mypy", mod.parse_args([str(root)]), root, root
                )
        self.assertIn("timed out", result["error"])
        self.assertFalse(seen["cache"].exists())


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
        self.assertEqual(reasons["coverage"], "not installed")


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
