"""Tests for merge_fragments.py and apply_verdicts.py (the Phase 3 verdict pipeline)."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from helpers import import_script

mf = import_script("merge_fragments")
av = import_script("apply_verdicts")


def _findings():
    return [
        {
            "id": "RV-001",
            "title": "Counter drifts",
            "classification": "FIX",
            "status": "confirmed",
            "locations": ["pkg/count.py:10"],
            "notes": "merged from two reports",
        },
        {
            "id": "RV-002",
            "title": "Cache never expires",
            "classification": "CONSIDER",
            "status": "confirmed",
            "locations": ["pkg/cache.py:4"],
        },
        {
            "id": "RV-003",
            "title": "Docs claim a flag",
            "classification": "CONSIDER",
            "status": "confirmed",
            "locations": ["README.md:9"],
        },
        {
            "id": "RV-004",
            "title": "Style choice",
            "classification": "POLICY",
            "status": "confirmed",
            "locations": ["pkg/x.py:1"],
        },
    ]


def _run(module, argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        try:
            module.main(argv)
            code = 0
        except SystemExit as exc:
            code = exc.code
    return json.loads(buf.getvalue()), code


class _RunDir(unittest.TestCase):
    """A run directory with findings, two slice fragments and reproducers."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        (self.root / "artifacts").mkdir()
        for name in ("RV-001.py", "RV-002.py", "RV-003.py"):
            (self.root / "artifacts" / name).write_text("print('BUG: x')\n")
        self.findings_path = self.root / "findings.pre-repro.json"
        self.findings_path.write_text(
            json.dumps({"project": "demo", "findings": _findings()})
        )
        self.frags = self.root / "fragments"
        self.frags.mkdir()
        self.assignments = {"S01": ["RV-001", "RV-002"], "S02": ["RV-003", "RV-004"]}
        (self.root / "slices.json").write_text(json.dumps(self.assignments))

    def write_fragment(self, name, data):
        (self.frags / f"slice-{name}.json").write_text(json.dumps(data))

    def good_fragments(self):
        self.write_fragment(
            "S01",
            [
                {
                    "id": "RV-001",
                    "verdict": "reproduced",
                    "main": "BUG",
                    "files": ["artifacts/RV-001.py"],
                    "branches": {"origin/fix-count": "OK"},
                    "branch_status": "fixed on origin/fix-count",
                    "corrections": {
                        "locations": ["pkg/count.py:10-12"],
                        "severity_note": "worse under load",
                    },
                },
                {
                    "id": "RV-002",
                    "verdict": "reproduced",
                    "main": "BUG",
                    "files": ["artifacts/RV-002.py"],
                    "proposed_classification": "FIX",
                    "classification_reason": "stale value served",
                },
            ],
        )
        self.write_fragment(
            "S02",
            {
                "slice": "S02",
                "verdicts": [
                    {
                        "id": "RV-003",
                        "verdict": "not-reproduced",
                        "main": "OK",
                        "files": ["artifacts/RV-003.py"],
                        "notes": "the flag exists",
                    },
                    {
                        "id": "RV-004",
                        "status": "no-mre-possible",
                        "proposed_classification": "CONSIDER",
                    },
                    {
                        "id": "RV-101",
                        "verdict": "reproduced",
                        "main": "BUG",
                        "files": ["artifacts/RV-001.py"],
                    },
                ],
                "new_findings": [
                    {
                        "id": "RV-101",
                        "title": "Sibling of RV-001",
                        "classification": "FIX",
                        "status": "confirmed",
                        "locations": ["pkg/count.py:30"],
                    }
                ],
            },
        )


class TestMerge(_RunDir):
    def test_clean_merge_reports_coverage_counts_and_proposals(self):
        self.good_fragments()
        result, code = _run(
            mf,
            [
                str(self.findings_path),
                str(self.frags),
                "--assignments",
                str(self.root / "slices.json"),
            ],
        )
        self.assertEqual(code, 0, result["problems"])
        self.assertTrue(result["complete"])
        self.assertEqual(result["coverage"]["covered"], 5)
        self.assertEqual(result["new_findings"], 1)
        self.assertEqual(
            result["counts"]["verdict"],
            {"reproduced": 3, "not-reproduced": 1, "no-mre-possible": 1},
        )
        self.assertEqual(
            {(r["id"], r["from"], r["to"]) for r in result["reclassifications"]},
            {("RV-002", "CONSIDER", "FIX"), ("RV-004", "POLICY", "CONSIDER")},
        )
        self.assertEqual(result["corrections"], ["RV-001"])
        self.assertNotIn("merged", result)

    def test_new_finding_outside_assignments_is_not_flagged(self):
        # RV-101 is reported by S02 itself: it cannot have been assigned.
        self.good_fragments()
        result, _code = _run(
            mf,
            [
                str(self.findings_path),
                str(self.frags),
                "--assignments",
                str(self.root / "slices.json"),
            ],
        )
        self.assertNotIn("not-assigned", {p["kind"] for p in result["problems"]})

    def test_pending_slice_is_not_a_gap(self):
        self.write_fragment(
            "S01",
            [
                {
                    "id": "RV-001",
                    "verdict": "reproduced",
                    "main": "BUG",
                    "files": ["artifacts/RV-001.py"],
                },
                {"id": "RV-002", "verdict": "no-mre-possible"},
            ],
        )
        result, code = _run(
            mf,
            [
                str(self.findings_path),
                str(self.frags),
                "--assignments",
                str(self.root / "slices.json"),
            ],
        )
        self.assertEqual(code, 0)
        self.assertFalse(result["complete"])
        self.assertEqual(result["pending_slices"], ["S02"])
        self.assertEqual(
            result["coverage"]["uncovered_in_pending_slices"], ["RV-003", "RV-004"]
        )

    def test_problems_are_reported(self):
        self.write_fragment(
            "S01",
            [
                {
                    "id": "RV-001",
                    "verdict": "reproduced",
                    "main": "OK",
                    "files": ["artifacts/RV-001.py"],
                },
                {
                    "id": "RV-003",
                    "verdict": "reproduced",
                    "main": "BUG",
                    "files": ["artifacts/missing.py"],
                },
                {"id": "RV-999", "verdict": "reproduced"},
            ],
        )
        self.write_fragment(
            "S02",
            [
                {"id": "RV-003", "verdict": "maybe"},
                {
                    "id": "RV-004",
                    "verdict": "not-reproduced",
                    "main": "BUG",
                    "proposed_classification": "MINOR",
                },
            ],
        )
        result, code = _run(
            mf,
            [
                str(self.findings_path),
                str(self.frags),
                "--assignments",
                str(self.root / "slices.json"),
            ],
        )
        self.assertEqual(code, 1)
        kinds = {(p["kind"], p["id"]) for p in result["problems"]}
        self.assertEqual(
            kinds,
            {
                ("verdict-main-mismatch", "RV-001"),
                ("not-assigned", "RV-003"),
                ("file-missing", "RV-003"),
                ("unknown-id", "RV-999"),
                ("duplicate", "RV-003"),
                ("verdict-main-mismatch", "RV-004"),
                ("bad-classification", "RV-004"),
                ("missing-from-fragment", "RV-002"),
            },
        )

    def test_reproduced_needs_files_and_warns_without_main(self):
        self.write_fragment("S01", [{"id": "RV-001", "verdict": "reproduced"}])
        result, _ = _run(
            mf, [str(self.findings_path), str(self.frags / "slice-S01.json")]
        )
        self.assertIn(
            ("reproduced-without-files", "RV-001"),
            {(p["kind"], p["id"]) for p in result["problems"]},
        )
        self.assertEqual(result["warnings"][0]["kind"], "reproduced-without-main")

    def test_path_relative_to_the_fragment_is_normalised(self):
        (self.frags / "RV-001.py").write_text("print('BUG: x')\n")
        self.write_fragment(
            "S01",
            [
                {
                    "id": "RV-001",
                    "verdict": "reproduced",
                    "main": "BUG",
                    "files": ["RV-001.py"],
                }
            ],
        )
        out = self.root / "merged.json"
        result, code = _run(
            mf, [str(self.findings_path), str(self.frags), "--out", str(out)]
        )
        self.assertEqual(code, 0, result["problems"])
        self.assertEqual(result["warnings"][0]["kind"], "path-normalized")
        self.assertEqual(
            json.loads(out.read_text())["verdicts"][0]["files"], ["fragments/RV-001.py"]
        )

    def test_new_finding_id_clash(self):
        self.write_fragment(
            "S01", {"verdicts": [], "new_findings": [{"id": "RV-002"}, {"title": "x"}]}
        )
        result, code = _run(mf, [str(self.findings_path), str(self.frags)])
        self.assertEqual(code, 1)
        self.assertEqual(
            {p["kind"] for p in result["problems"]},
            {"new-finding-id-taken", "new-finding-without-id"},
        )

    def test_directory_takes_only_slice_files(self):
        self.good_fragments()
        (self.frags / "slices.json").write_text(json.dumps(self.assignments))
        (self.frags / "decisions.json").write_text("{}")
        result, code = _run(mf, [str(self.findings_path), str(self.frags)])
        self.assertEqual(code, 0, result["problems"])
        self.assertEqual(result["fragments"], ["S01", "S02"])

    def test_no_fragments_is_an_error(self):
        result, code = _run(mf, [str(self.findings_path), str(self.frags)])
        self.assertEqual(code, 2)
        self.assertIn("error", result)


class TestApply(_RunDir):
    def _apply(self, *extra):
        out = self.root / "findings.json"
        result, code = _run(
            av, [str(self.findings_path), str(self.frags), "--out", str(out), *extra]
        )
        data = json.loads(out.read_text()) if out.exists() else None
        return result, code, data

    def test_applies_status_classification_corrections_and_repro_fields(self):
        self.good_fragments()
        result, code, data = self._apply()
        self.assertEqual(code, 0, result)
        self.assertEqual(data["project"], "demo")
        by_id = {f["id"]: f for f in data["findings"]}
        f1 = by_id["RV-001"]
        self.assertEqual(f1["status"], "reproduced")
        self.assertEqual(f1["locations"], ["pkg/count.py:10-12"])
        self.assertEqual(
            f1["pre_repro"],
            {
                "status": "confirmed",
                "locations": ["pkg/count.py:10"],
                "branch_status": None,
            },
        )
        self.assertEqual(f1["repro"], ["artifacts/RV-001.py"])
        self.assertEqual(f1["repro_main"], "BUG")
        self.assertEqual(f1["repro_branches"], {"origin/fix-count": "OK"})
        self.assertEqual(f1["repro_slice"], "S01")
        self.assertIn("severity_note", f1["notes"])
        self.assertTrue(f1["notes"].startswith("merged from two reports\n"))
        self.assertEqual(by_id["RV-002"]["classification"], "FIX")
        self.assertIn("stale value served", by_id["RV-002"]["classification_decision"])
        self.assertEqual(by_id["RV-003"]["status"], "refuted")
        self.assertEqual(by_id["RV-003"]["repro_notes"], "the flag exists")
        self.assertEqual(by_id["RV-004"]["status"], "confirmed")
        self.assertEqual(by_id["RV-101"]["status"], "reproduced")
        self.assertEqual(by_id["RV-101"]["repro_slice"], "S02")
        self.assertNotIn("_fragment", by_id["RV-101"])
        self.assertEqual(result["fix_by_status"], {"reproduced": 3})
        self.assertEqual(result["unverified"], [])

    def test_decisions_reject_override_and_note(self):
        self.good_fragments()
        decisions = {
            "reject": {"RV-002": "argued from reading only"},
            "override": {
                "RV-003": {"classification": "POLICY", "reason": "design choice"}
            },
            "notes": {"RV-004": "pairs with RV-003"},
        }
        (self.root / "decisions.json").write_text(json.dumps(decisions))
        result, code, data = self._apply(
            "--decisions", str(self.root / "decisions.json")
        )
        self.assertEqual(code, 0, result)
        by_id = {f["id"]: f for f in data["findings"]}
        self.assertEqual(by_id["RV-002"]["classification"], "CONSIDER")
        self.assertIn(
            "argued from reading only", by_id["RV-002"]["classification_decision"]
        )
        self.assertEqual(by_id["RV-003"]["classification"], "POLICY")
        self.assertEqual(by_id["RV-003"]["pre_repro"]["classification"], "CONSIDER")
        self.assertEqual(by_id["RV-004"]["notes"], "pairs with RV-003")
        self.assertEqual(
            result["rejected"],
            [{"id": "RV-002", "kept": "CONSIDER", "proposed": "FIX"}],
        )

    def test_reject_matches_a_numeric_id(self):
        # decisions.json keys are strings; a finding id may be an int.
        findings = [{"id": 12, "classification": "CONSIDER"}]
        verdicts = [
            {
                "id": 12,
                "verdict": "reproduced",
                "main": "BUG",
                "proposed_classification": "FIX",
            }
        ]
        log = av.apply(findings, verdicts, [], {"reject": {"12": "kept on review"}})
        self.assertEqual(findings[0]["classification"], "CONSIDER")
        self.assertEqual(log["rejected"][0]["kept"], "CONSIDER")

    def test_repeatable_from_the_same_input(self):
        self.good_fragments()
        _, _, first = self._apply()
        (self.root / "findings.json").unlink()
        _, _, second = self._apply()
        self.assertEqual(first, second)
        self.assertEqual(
            json.loads(self.findings_path.read_text())["findings"], _findings()
        )

    def test_refuses_to_overwrite_its_input(self):
        self.good_fragments()
        result, code = _run(
            av,
            [
                str(self.findings_path),
                str(self.frags),
                "--out",
                str(self.findings_path),
            ],
        )
        self.assertEqual(code, 2)
        self.assertIn("must not be the input", result["error"])

    def test_merge_problems_block_the_write(self):
        self.write_fragment(
            "S01",
            [
                {
                    "id": "RV-001",
                    "verdict": "reproduced",
                    "main": "OK",
                    "files": ["artifacts/RV-001.py"],
                }
            ],
        )
        result, code, data = self._apply()
        self.assertEqual(code, 1)
        self.assertFalse(result["written"])
        self.assertIsNone(data)

    def test_unknown_decision_id_blocks_the_write(self):
        self.good_fragments()
        (self.root / "d.json").write_text(
            json.dumps({"override": {"RV-777": {"classification": "FIX"}}})
        )
        _result, code, data = self._apply("--decisions", str(self.root / "d.json"))
        self.assertEqual(code, 1)
        self.assertIsNone(data)

    def test_unverified_findings_are_listed_or_required(self):
        self.write_fragment(
            "S01",
            [
                {
                    "id": "RV-001",
                    "verdict": "reproduced",
                    "main": "BUG",
                    "files": ["artifacts/RV-001.py"],
                }
            ],
        )
        result, code, data = self._apply()
        self.assertEqual(code, 0)
        self.assertEqual(result["unverified"], ["RV-002", "RV-003", "RV-004"])
        self.assertEqual(
            {f["id"]: f["status"] for f in data["findings"]}["RV-002"], "confirmed"
        )
        (self.root / "findings.json").unlink()
        result, code, data = self._apply("--require-all")
        self.assertEqual(code, 1)
        self.assertIsNone(data)

    def test_accepts_the_merged_file(self):
        self.good_fragments()
        merged = self.root / "merged.json"
        _run(mf, [str(self.findings_path), str(self.frags), "--out", str(merged)])
        out = self.root / "findings.json"
        result, code = _run(
            av, [str(self.findings_path), str(merged), "--out", str(out)]
        )
        self.assertEqual(code, 0, result)
        by_id = {f["id"]: f for f in json.loads(out.read_text())["findings"]}
        self.assertEqual(by_id["RV-003"]["repro_slice"], "S02")
        self.assertEqual(by_id["RV-101"]["repro_slice"], "S02")

    def test_list_input_stays_a_list(self):
        self.findings_path.write_text(json.dumps(_findings()))
        self.good_fragments()
        _, code, data = self._apply()
        self.assertEqual(code, 0)
        self.assertIsInstance(data, list)


if __name__ == "__main__":
    unittest.main()
