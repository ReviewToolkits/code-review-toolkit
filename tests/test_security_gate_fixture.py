"""The security-gate regression fixture: deterministic half (see tests/fixtures/security_gate/README.md)."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from helpers import import_script

threat_model = import_script("find_threat_model")
gate = import_script("security_gate")

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "security_gate"


class TestFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Copy out of the toolkit's own git repository, so the history scan and
        # project-root detection see only the fixture.
        cls._tmp = tempfile.mkdtemp(prefix="crt_gate_")
        cls.project = Path(cls._tmp) / "quayside"
        shutil.copytree(FIXTURE / "project", cls.project)
        cls.tm = threat_model.analyze(str(cls.project), network=False)
        cls.findings = json.loads(
            (FIXTURE / "findings.json").read_text(encoding="utf-8")
        )["findings"]
        cls.expected = json.loads(
            (FIXTURE / "expected.json").read_text(encoding="utf-8")
        )

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def test_threat_model_found(self):
        self.assertEqual(self.tm["status"], "found")
        self.assertEqual(len(self.tm["precedents"]), 8)
        self.assertEqual({a["id"] for a in self.tm["accepted_risks"]}, {"QS-6", "QS-7"})
        scope = self.tm["policy"][0]["scope_sections"]
        self.assertTrue(any(s["heading"] == "Out of scope" for s in scope))
        self.assertEqual(len(self.tm["engagement_rules"]), 1)

    def test_blind_strips_opinion_and_hints_duplicates(self):
        claims = {
            c["id"]: c
            for c in gate.blind(self.findings, precedents=self.tm["precedents"])
        }
        self.assertEqual(len(claims), 12)
        for claim in claims.values():
            self.assertNotIn("severity", claim)
            self.assertNotIn("impact", claim)
        for cid, ref in self.expected.items():
            if "duplicates" in ref:
                with self.subTest(claim=cid):
                    hints = [h["id"] for h in claims[cid]["precedent_hints"]]
                    self.assertIn(ref["duplicates"], hints)

    def _reference_verdicts(self):
        verdicts = []
        for cid, ref in self.expected.items():
            verdict = ref["verdict"][0]
            severity = ref.get("max_severity", "n/a") if verdict != "bug" else "n/a"
            verdicts.append(
                {
                    "id": cid,
                    "verdict": verdict,
                    "severity": severity,
                    "precedent": ref.get("precedent"),
                    "duplicates": ref.get("duplicates"),
                    "incomplete_fix_of": ref.get("incomplete_fix_of"),
                    "reason": ref["why"],
                    "strongest_counterargument": "fixture",
                }
            )
        return verdicts

    def test_reference_verdicts_pass_and_score_perfectly(self):
        claims = gate.blind(self.findings)
        verdicts = self._reference_verdicts()
        result = gate.signoff(self.tm, verdicts, claims=claims)
        # C07 is solid at medium, anchored to QS-2: a human confirms it.
        self.assertEqual(
            result["signoff_line"],
            "SECURITY-GATE: SOLID=3 ARGUABLE=2 BUG=7 THREAT-MODEL=found PRECEDENTS=8 -- NEEDS-SIGN-OFF",
        )
        self.assertEqual(result["blocking"], [])
        scored = gate.score(verdicts, self.expected)
        self.assertEqual(scored["agreement"], "12/12")
        self.assertEqual(scored["missed_duplicates"], [])

    def test_claims_as_the_finder_rated_them_are_blocked(self):
        as_rated = [
            {
                "id": f["id"],
                "verdict": "solid",
                "severity": f["severity"],
                "reason": "as reported",
                "strongest_counterargument": "none given",
            }
            for f in self.findings
        ]
        result = gate.signoff(self.tm, as_rated, claims=gate.blind(self.findings))
        self.assertEqual(result["state"], "BLOCKED")
        self.assertTrue(any("not anchored" in b for b in result["blocking"]))
        self.assertTrue(
            any("exceed the disclosure cap" in b for b in result["blocking"])
        )
        scored = gate.score(as_rated, self.expected)
        self.assertLess(scored["agreement_rate"], 0.5)
        self.assertEqual(len(scored["missed_duplicates"]), 2)


if __name__ == "__main__":
    unittest.main()
