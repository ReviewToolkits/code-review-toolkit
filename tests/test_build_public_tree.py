"""Tests for build_public_tree.py: selection, stable ids, rewriting, leak gate."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from helpers import import_script

bpt = import_script("build_public_tree")


def _findings():
    return [
        {
            "id": "RV-001",
            "title": "Retry loop drops the last item",
            "classification": "CONSIDER",
            "status": "reproduced",
            "locations": ["pkg/retry.py:40-44", "pkg/batch.py:12"],
            "mechanism": "Same off-by-one as RV-003.",
            "failure_scenario": "5 items in, 4 out.",
            "repro": ["artifacts/RV-001a.py", "artifacts/RV-001.md"],
            "related": ["RV-003", "RV-002"],
            "source_refs": ["agents/x.md#L3"],
            "notes": "dedup with another report",
        },
        {
            "id": "RV-002",
            "title": "Token check skipped",
            "classification": "FIX",
            "status": "reproduced",
            "locations": ["pkg/auth.py:9"],
            "publication_sensitive": True,
        },
        {
            "id": "RV-003",
            "title": "Off-by-one in pager",
            "classification": "FIX",
            "status": "confirmed",
            "locations": ["pkg/pager.py:7"],
            "fix": "use <=",
            "prior_art": {"verdict": "none"},
        },
        {
            "id": "RV-004",
            "title": "Upload size unchecked",
            "classification": "FIX",
            "status": "reproduced",
            "locations": ["pkg/up.py:3"],
            "security_claim": {
                "attacker": "remote user",
                "preconditions": "none",
                "gain": "disk fill",
            },
        },
        {
            "id": "RV-005",
            "title": "Flag documented but absent",
            "classification": "CONSIDER",
            "status": "refuted",
            "locations": ["README.md:4"],
        },
        {
            "id": "RV-006",
            "title": "Unverified idea",
            "classification": "CONSIDER",
            "status": "candidate",
            "locations": ["pkg/y.py:1"],
        },
        {
            "id": "RV-007",
            "title": "Naming policy",
            "classification": "POLICY",
            "status": "confirmed",
            "locations": ["pkg/"],
            "security_claim": {"attacker": "", "preconditions": "", "gain": ""},
        },
    ]


REPRO = "# reproducer for RV-001a\ndef test_rv_001_drops_item():\n    print('BUG: 4 of 5')\n"


def _run(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        try:
            bpt.main(argv)
            code = 0
        except SystemExit as exc:
            code = exc.code
    return json.loads(buf.getvalue()), code


class TestBuildPublicTree(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.run_dir = Path(self._tmp.name)
        (self.run_dir / "artifacts").mkdir()
        (self.run_dir / "artifacts" / "RV-001a.py").write_text(REPRO)
        self.findings = self.run_dir / "findings.json"
        self.findings.write_text(json.dumps({"findings": _findings()}))
        self.out = self.run_dir / "public"

    def build(self, *extra, findings=None):
        if findings is not None:
            self.findings.write_text(json.dumps(findings))
        return _run(
            [
                str(self.findings),
                "--out",
                str(self.out),
                "--prefix",
                "PUB",
                "--commit",
                "abc123def456",
                "--project",
                "demo",
                *extra,
            ]
        )

    def test_selection_order_ids_and_layout(self):
        report, code = self.build("--idmap-out", str(self.run_dir / "idmap.json"))
        self.assertEqual(code, 0, report["errors"])
        self.assertEqual(
            report["withheld"],
            {
                "publication_sensitive": 1,
                "security_claim": 1,
                "refuted": 1,
                "candidate": 1,
            },
        )
        idmap = json.loads((self.run_dir / "idmap.json").read_text())
        # FIX before CONSIDER before POLICY; an all-empty security_claim is no claim.
        self.assertEqual(
            idmap, {"RV-003": "PUB-0001", "RV-001": "PUB-0002", "RV-007": "PUB-0003"}
        )
        meta = json.loads((self.out / "findings.json").read_text())
        self.assertEqual(
            (meta["project"], meta["schema_version"], meta["commit"]),
            ("demo", 1, "abc123def456"),
        )
        entry = {e["id"]: e for e in meta["findings"]}["PUB-0002"]
        self.assertEqual(entry["location"], "pkg/retry.py:40-44")
        self.assertEqual(entry["sites"], ["pkg/batch.py:12"])
        self.assertEqual(entry["mechanism"], "Same off-by-one as PUB-0001.")
        self.assertEqual(entry["related"], ["PUB-0001"])  # RV-002 is withheld: dropped
        self.assertNotIn("source_refs", entry)
        self.assertNotIn("notes", entry)
        self.assertEqual(entry["repro"], ["PUB-0002a.py"])
        rdir = self.out / "reports" / "PUB-0002-retry-loop-drops-the-last-item"
        code_text = (rdir / "PUB-0002a.py").read_text()
        self.assertIn("reproducer for PUB-0002a", code_text)
        self.assertIn("def test_pub_0002_drops_item", code_text)
        md = (rdir / "report.md").read_text()
        self.assertIn("# PUB-0002: Retry loop drops the last item", md)
        self.assertIn("5 items in, 4 out.", md)
        self.assertIn("def test_pub_0002_drops_item", md)
        self.assertFalse((self.out / "idmap.json").exists())
        self.assertFalse(any("RV-" in p.name for p in self.out.rglob("*")))

    def test_previous_idmap_keeps_published_ids(self):
        previous = {"RV-001": "PUB-0001", "RV-005": "PUB-0002"}
        (self.run_dir / "old.json").write_text(json.dumps(previous))
        report, code = self.build(
            "--idmap",
            str(self.run_dir / "old.json"),
            "--idmap-out",
            str(self.run_dir / "new.json"),
        )
        self.assertEqual(code, 0, report["errors"])
        idmap = json.loads((self.run_dir / "new.json").read_text())
        self.assertEqual(
            idmap, {"RV-003": "PUB-0003", "RV-001": "PUB-0001", "RV-007": "PUB-0004"}
        )
        self.assertEqual(report["withdrawn"], ["RV-005"])

    def test_text_naming_a_withheld_finding_is_an_error(self):
        findings = _findings()
        findings[2]["mechanism"] = "Compare RV-002, which is similar."
        report, code = self.build(findings=findings)
        self.assertEqual(code, 1)
        self.assertFalse(report["written"])
        self.assertTrue(any("mentions RV-002" in e for e in report["errors"]))
        self.assertFalse(self.out.exists())

    def test_reproducer_naming_a_withheld_finding_is_an_error(self):
        (self.run_dir / "artifacts" / "RV-001a.py").write_text("# like test_rv_002\n")
        report, code = self.build()
        self.assertEqual(code, 1)
        self.assertTrue(
            any("RV-001a.py: mentions rv_002" in e for e in report["errors"])
        )

    def test_leak_terms_and_extra_prefixes(self):
        findings = _findings()
        findings[2]["fix"] = "see reports/run_v2/agents/x.md; agent id CA-12 agrees"
        findings[0]["failure_scenario"] = "bugreports/ are unaffected"
        report, code = self.build(
            "--term", "reports/", "--id-prefix", "CA", "--check", findings=findings
        )
        self.assertEqual(code, 1)
        errors = "\n".join(report["errors"])
        self.assertIn("mentions CA-12", errors)
        self.assertIn("'reports/'", errors)
        self.assertNotIn("bugreports", errors)

    def test_layout_and_public_id_spellings_are_not_leaks(self):
        # `reports/` names the tree's own directories, and test_pub_0002 is a
        # public id: neither may trip the scan for a private `reports/` dir
        # or for an internal prefix that the public prefix contains.
        findings = _findings()
        for f in findings:
            f["id"] = f["id"].replace("RV-", "AB-")
            f["related"] = [r.replace("RV-", "AB-") for r in f.get("related", [])]
            f["mechanism"] = f.get("mechanism", "").replace("RV-", "AB-")
        findings[0]["repro"] = ["artifacts/AB-001a.py"]
        (self.run_dir / "artifacts" / "AB-001a.py").write_text(
            "def test_ab_001():\n    pass\n"
        )
        self.findings.write_text(json.dumps(findings))
        report, code = _run(
            [
                str(self.findings),
                "--out",
                str(self.out),
                "--prefix",
                "XY-AB",
                "--term",
                "reports/",
            ]
        )
        self.assertEqual(code, 0, report["errors"])
        rdir = next((self.out / "reports").glob("XY-AB-0002-*"))
        self.assertIn("def test_xy_ab_0002():", (rdir / "XY-AB-0002a.py").read_text())

    def test_missing_reproducer_is_an_error(self):
        (self.run_dir / "artifacts" / "RV-001a.py").unlink()
        report, code = self.build()
        self.assertEqual(code, 1)
        self.assertTrue(any("not found" in e for e in report["errors"]))

    def test_check_writes_nothing(self):
        report, code = self.build("--check")
        self.assertEqual(code, 0)
        self.assertFalse(report["written"])
        self.assertFalse(self.out.exists())

    def test_include_refuted(self):
        report, _code = self.build("--include-refuted")
        self.assertEqual(report["public"], 4)

    def test_refuses_a_non_empty_out_dir(self):
        self.out.mkdir()
        (self.out / "keep.txt").write_text("x")
        _report, code = self.build()
        self.assertEqual(code, 2)
        self.assertTrue((self.out / "keep.txt").exists())

    def test_public_prefix_colliding_with_internal_is_an_error(self):
        report, code = _run(
            [str(self.findings), "--out", str(self.out), "--prefix", "RV"]
        )
        self.assertEqual(code, 1)
        self.assertTrue(any("public prefix" in e for e in report["errors"]))


if __name__ == "__main__":
    unittest.main()
