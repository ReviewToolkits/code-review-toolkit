"""Tests for security_gate.py: blinding claims and computing the sign-off line."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

from helpers import import_script

mod = import_script("security_gate")

THREAT_MODEL = {
    "status": "found",
    "precedents": [
        {
            "id": "AUD-1",
            "title": "Missing authorization on the account export endpoint",
            "severity": "high",
            "status": "remediated",
        },
        {
            "id": "AUD-2",
            "title": "Session cookie lacks the SameSite attribute",
            "severity": "low",
            "status": "remediated",
        },
        {
            "id": "AUD-3",
            "title": "Address ban not enforced for API token requests",
            "severity": "informational",
            "status": "accepted",
        },
        {
            "id": "AUD-4",
            "title": "Weak signature verification on webhooks",
            "severity": "medium",
            "status": "remediated",
        },
    ],
}


def verdict(vid, v="bug", severity="n/a", **extra):
    return {
        "id": vid,
        "verdict": v,
        "severity": severity,
        "reason": "r",
        "strongest_counterargument": "c",
        **extra,
    }


class TestBlind(unittest.TestCase):
    FINDINGS: ClassVar[list] = [
        {
            "id": "F-1",
            "title": "Admin IP ban does not stop API token requests",
            "mechanism": "the ban check runs only in the password login path",
            "severity": "medium",
            "classification": "FIX",
            "impact": "anyone can bypass a ban",
            "notes": "finder thinks this is serious",
            "security_claim": {
                "attacker": "a banned user",
                "preconditions": "a valid API token",
                "gain": "keeps uploading",
            },
            "locations": ["pkg/auth.py:10"],
        },
        {
            "id": "F-2",
            "title": "Typo in a log message",
            "severity": "low",
            "security_claim": {"attacker": "", "gain": ""},
        },
        {
            "id": "F-3",
            "title": "Legacy flagged item",
            "security": True,
            "kind": "vulnerability",
        },
        {"id": "F-4", "title": "Hardening item", "kind": "hardening"},
    ]

    def test_selects_claims_and_strips_opinion(self):
        claims = mod.blind(self.FINDINGS)
        self.assertEqual([c["id"] for c in claims], ["F-1", "F-3", "F-4"])
        first = claims[0]
        for field in ("severity", "classification", "impact", "notes", "kind"):
            self.assertNotIn(field, first)
        self.assertEqual(first["security_claim"]["attacker"], "a banned user")
        self.assertEqual(first["locations"], ["pkg/auth.py:10"])
        # A legacy finding with no claim gets an empty one for the skeptic to fill.
        self.assertEqual(
            claims[1]["security_claim"],
            {"attacker": "", "preconditions": "", "gain": ""},
        )

    def test_take_all(self):
        self.assertEqual(len(mod.blind(self.FINDINGS, take_all=True)), 4)

    def test_precedent_hints_catch_an_audit_duplicate(self):
        claims = mod.blind(self.FINDINGS, precedents=THREAT_MODEL["precedents"])
        hints = [h["id"] for h in claims[0]["precedent_hints"]]
        self.assertEqual(hints[0], "AUD-3")
        self.assertEqual(claims[0]["precedent_hints"][0]["status"], "accepted")

    def test_hints_need_real_overlap(self):
        finding = {"id": "X", "title": "Cache key collision in template rendering"}
        self.assertEqual(mod.precedent_hints(finding, THREAT_MODEL["precedents"]), [])

    def test_stemming_matches_plural_forms(self):
        finding = {"id": "X", "title": "Webhook signatures verified with a weak hash"}
        self.assertEqual(
            mod.precedent_hints(finding, THREAT_MODEL["precedents"])[0]["id"], "AUD-4"
        )


class TestPrecedentHintRanking(unittest.TestCase):
    """Hints rank by distinctive words and mechanism class, and stay few."""

    # A domain where most audit titles share the same nouns.
    PRECEDENTS: ClassVar[list] = [
        {"id": "P-1", "title": "Account token expiry not enforced", "severity": "low"},
        {"id": "P-2", "title": "Account token shown in logs", "severity": "low"},
        {"id": "P-3", "title": "Account settings form lacks CSRF protection"},
        {"id": "P-4", "title": "Denial of service on compressed archive uploads"},
        {"id": "P-5", "title": "Token reuse after account deletion", "severity": "low"},
    ]

    def test_shared_domain_nouns_alone_give_no_hint(self):
        # "account" and "token" are in most titles: sharing them says nothing.
        finding = {"id": "X", "title": "Account token count is wrong on the dashboard"}
        self.assertEqual(mod.precedent_hints(finding, self.PRECEDENTS), [])

    def test_mechanism_class_matches_across_wording(self):
        finding = {
            "id": "X",
            "title": "Archive upload has no decompressed-size cap (decompression bomb)",
        }
        hints = mod.precedent_hints(finding, self.PRECEDENTS)
        self.assertEqual([h["id"] for h in hints], ["P-4"])
        self.assertEqual(hints[0]["shared_mechanisms"], ["denial-of-service"])
        self.assertIn("score", hints[0])

    def test_at_most_two_hints(self):
        precedents = [
            {"id": f"R-{i}", "title": f"Replay of signed webhook payloads variant {i}"}
            for i in range(5)
        ]
        finding = {"id": "X", "title": "Signed webhook payloads accept replay"}
        self.assertEqual(len(mod.precedent_hints(finding, precedents)), 2)

    def test_blind_weights_words_across_all_claims(self):
        findings = [
            {
                "id": f"F-{i}",
                "title": f"Account token problem number {i}",
                "security_claim": {"attacker": "a", "preconditions": "p", "gain": "g"},
            }
            for i in range(3)
        ]
        claims = mod.blind(findings, precedents=self.PRECEDENTS)
        self.assertTrue(all(c["precedent_hints"] == [] for c in claims))


class TestLint(unittest.TestCase):
    """Publication hygiene of a consolidated findings.json."""

    def _kinds(self, findings, **kw):
        return sorted(
            (p["id"], p["kind"], p["detail"])
            for p in mod.lint(findings, agent_names=["silent-failure-hunter"], **kw)[
                "problems"
            ]
        )

    def test_clean_findings_pass(self):
        findings = [
            {
                "id": "PRJ-001",
                "title": "Retry drops the task after a timeout",
                "failure_scenario": "a UTF-8 body over HTTP2 and a SHA1 digest; see PRJ-002",
                "agents": ["silent-failure-hunter"],
                "source_refs": ["agents/silent-failure-hunter.md#SFH-3"],
            },
            {"id": "PRJ-002", "title": "Base64 field is parsed as ZIP64"},
        ]
        result = mod.lint(findings, agent_names=["silent-failure-hunter"])
        self.assertTrue(result["clean"], result["problems"])

    def test_agent_names_ids_and_separators_in_prose(self):
        findings = [
            {
                "id": "PRJ-001",
                "title": "Lost retry || swallowed error",
                "mechanism": "silent-failure-hunter saw this as SFH-3 (same as SF1)",
            }
        ]
        self.assertEqual(
            self._kinds(findings),
            [
                ("PRJ-001", "agent-name", "silent-failure-hunter"),
                ("PRJ-001", "internal-id", "SF1"),
                ("PRJ-001", "internal-id", "SFH-3"),
                ("PRJ-001", "merge-separator", "`||` in maintainer-facing text"),
            ],
        )

    def test_suffixed_finding_id_is_a_known_id(self):
        findings = [
            {"id": "PRJ-001", "title": "Split from PRJ-002a"},
            {"id": "PRJ-002", "title": "Original"},
        ]
        self.assertEqual(self._kinds(findings), [])

    def test_allow_list(self):
        findings = [{"id": "PRJ-001", "title": "Breaks on WCAG-21 and on KV9"}]
        self.assertEqual(self._kinds(findings), [("PRJ-001", "internal-id", "KV9")])
        self.assertEqual(self._kinds(findings, allow={"KV9"}), [])

    def test_public_finding_citing_a_sensitive_one(self):
        findings = [
            {
                "id": "PRJ-001",
                "title": "Public",
                "notes": "merged the twin into PRJ-007a",
            },
            {"id": "PRJ-002", "title": "Public", "propagation_gap_of": ["PRJ-007"]},
            {"id": "PRJ-007", "title": "Private", "publication_sensitive": True},
            # Sensitive findings may cite each other, and themselves.
            {
                "id": "PRJ-008",
                "title": "Private",
                "notes": "sibling of PRJ-007, see PRJ-008",
                "publication_sensitive": True,
            },
            # PRJ-0070 is a different finding.
            {"id": "PRJ-009", "title": "Public", "notes": "unlike PRJ-0070"},
        ]
        problems = mod.lint(findings, agent_names=[])["problems"]
        self.assertEqual(
            sorted((p["id"], p["field"], p["kind"], p["detail"]) for p in problems),
            [
                ("PRJ-001", "notes", "sensitive-ref", "PRJ-007"),
                ("PRJ-002", "propagation_gap_of", "sensitive-ref", "PRJ-007"),
            ],
        )

    def test_default_agent_names_come_from_the_plugin(self):
        findings = [{"id": "PRJ-001", "title": "Seen by the concurrency-auditor"}]
        self.assertEqual(mod.lint(findings)["counts"], {"agent-name": 1})


class TestSignoff(unittest.TestCase):
    def test_pass(self):
        result = mod.signoff(
            THREAT_MODEL,
            [
                verdict("A", "solid", "low"),
                verdict("B", "arguable", "informational"),
                verdict("C"),
            ],
        )
        self.assertEqual(
            result["signoff_line"],
            "SECURITY-GATE: SOLID=1 ARGUABLE=1 BUG=1 THREAT-MODEL=found PRECEDENTS=4 -- PASS",
        )
        self.assertEqual(result["disclosure"][0]["id"], "A")
        self.assertEqual(result["titles_only"], ["B"])
        self.assertEqual(result["not_security"], ["C"])

    def test_missing_threat_model_blocks(self):
        result = mod.signoff(
            {"status": "missing", "precedents": []}, [verdict("A", "solid", "low")]
        )
        self.assertEqual(result["state"], "BLOCKED")
        self.assertIn("threat model missing", result["blocking"][0])

    def test_partial_threat_model_needs_sign_off(self):
        result = mod.signoff(
            {"status": "partial", "precedents": []}, [verdict("A", "solid", "low")]
        )
        self.assertEqual(result["state"], "NEEDS-SIGN-OFF")

    def test_unanchored_medium_blocks(self):
        result = mod.signoff(THREAT_MODEL, [verdict("A", "solid", "medium")])
        self.assertEqual(result["state"], "BLOCKED")
        self.assertIn("not anchored", result["blocking"][0])

    def test_severity_above_its_precedent_blocks(self):
        result = mod.signoff(
            THREAT_MODEL, [verdict("A", "solid", "high", precedent="AUD-4")]
        )
        self.assertIn("exceeds its precedent AUD-4", result["blocking"][0])

    def test_anchored_medium_solid_needs_sign_off(self):
        result = mod.signoff(
            THREAT_MODEL, [verdict("A", "solid", "medium", precedent="AUD-4")]
        )
        self.assertEqual(result["state"], "NEEDS-SIGN-OFF")
        self.assertIn("anchored to AUD-4", result["needs_review"][0])

    def test_solid_duplicate_of_accepted_finding_blocks(self):
        result = mod.signoff(
            THREAT_MODEL, [verdict("A", "solid", "low", duplicates="AUD-3")]
        )
        self.assertEqual(result["state"], "BLOCKED")
        self.assertIn("duplicates AUD-3 (accepted)", result["blocking"][0])

    def test_remediated_duplicate_is_an_incomplete_fix_not_a_block(self):
        result = mod.signoff(
            THREAT_MODEL, [verdict("A", "solid", "low", duplicates="AUD-2")]
        )
        self.assertEqual(result["state"], "NEEDS-SIGN-OFF")
        self.assertIn("incomplete_fix_of", result["needs_review"][0])
        result = mod.signoff(
            THREAT_MODEL, [verdict("A", "solid", "low", incomplete_fix_of="AUD-2")]
        )
        self.assertEqual(result["state"], "PASS")
        self.assertEqual(
            result["incomplete_fixes"], [{"id": "A", "incomplete_fix_of": "AUD-2"}]
        )

    def test_incomplete_fix_must_name_a_precedent(self):
        result = mod.signoff(
            THREAT_MODEL, [verdict("A", "solid", "low", incomplete_fix_of="NOPE")]
        )
        self.assertEqual(result["state"], "BLOCKED")

    def test_arguable_duplicate_is_recorded_not_blocking(self):
        result = mod.signoff(
            THREAT_MODEL,
            [verdict("A", "arguable", "informational", duplicates="AUD-3")],
        )
        self.assertEqual(result["state"], "PASS")
        self.assertEqual(result["duplicates"], [{"id": "A", "duplicates": "AUD-3"}])

    def test_disclosure_cap_and_override(self):
        six = [verdict(f"S{i}", "solid", "low") for i in range(6)]
        self.assertEqual(mod.signoff(THREAT_MODEL, six)["state"], "BLOCKED")
        result = mod.signoff(THREAT_MODEL, six, override="six independent items")
        self.assertEqual(result["state"], "NEEDS-SIGN-OFF")
        self.assertEqual(
            mod.signoff(THREAT_MODEL, six, max_disclosure=6)["state"], "PASS"
        )

    def test_coverage_against_claims(self):
        result = mod.signoff(
            THREAT_MODEL,
            [verdict("A"), verdict("Z")],
            claims=[{"id": "A"}, {"id": "B"}],
        )
        self.assertIn("B: claim has no verdict", result["blocking"])
        self.assertIn("Z: verdict for an id that is not a claim", result["blocking"])

    def test_malformed_verdicts_block(self):
        cases = [
            (
                {
                    "id": "A",
                    "verdict": "maybe",
                    "severity": "low",
                    "reason": "r",
                    "strongest_counterargument": "c",
                },
                "not one of",
            ),
            (
                {
                    "id": "A",
                    "verdict": "solid",
                    "severity": "severe",
                    "reason": "r",
                    "strongest_counterargument": "c",
                },
                "not on the scale",
            ),
            (
                {"id": "A", "verdict": "solid", "severity": "low", "reason": "r"},
                "missing strongest_counterargument",
            ),
            (verdict("A", "bug", "low"), "a `bug` verdict carries security severity"),
        ]
        for record, message in cases:
            with self.subTest(message=message):
                result = mod.signoff(THREAT_MODEL, [record])
                self.assertEqual(result["state"], "BLOCKED")
                self.assertTrue(
                    any(message in b for b in result["blocking"]), result["blocking"]
                )

    def test_duplicate_verdict_ids_block(self):
        result = mod.signoff(THREAT_MODEL, [verdict("A"), verdict("A")])
        self.assertIn("A: more than one verdict", result["blocking"])


class TestRegressionShape(unittest.TestCase):
    """The incident this gate exists for, as numbers (synthetic; no real text).

    An 84-item report went out as 56 vulnerabilities (13 rated medium, none
    anchored to a precedent) and 28 other items. A strict re-triage against the
    project's published audit kept 6 Low items, called 25 arguable and 53 bugs,
    and found one claim that repeated an accepted audit finding.
    """

    def setUp(self):
        self.as_sent = (
            [verdict(f"V{i:02d}", "solid", "medium") for i in range(13)]
            + [verdict(f"V{i:02d}", "solid", "low") for i in range(13, 56)]
            + [verdict(f"V{i:02d}", "arguable", "low") for i in range(56, 84)]
        )
        self.retriaged = (
            [verdict(f"V{i:02d}", "solid", "low") for i in range(6)]
            + [verdict(f"V{i:02d}", "arguable", "informational") for i in range(6, 30)]
            + [verdict("V30", "arguable", "informational", duplicates="AUD-3")]
            + [verdict(f"V{i:02d}") for i in range(31, 84)]
        )
        self.claims = [{"id": f"V{i:02d}"} for i in range(84)]

    def test_report_as_sent_is_blocked(self):
        result = mod.signoff(THREAT_MODEL, self.as_sent, claims=self.claims)
        self.assertEqual(result["state"], "BLOCKED")
        unanchored = [b for b in result["blocking"] if "not anchored" in b]
        self.assertEqual(len(unanchored), 13)
        self.assertTrue(
            any(
                "56 solid items exceed the disclosure cap" in b
                for b in result["blocking"]
            )
        )

    def test_retriaged_set_needs_an_explicit_override(self):
        result = mod.signoff(THREAT_MODEL, self.retriaged, claims=self.claims)
        self.assertEqual(
            result["signoff_line"],
            "SECURITY-GATE: SOLID=6 ARGUABLE=25 BUG=53 THREAT-MODEL=found PRECEDENTS=4 -- BLOCKED",
        )
        result = mod.signoff(
            THREAT_MODEL,
            self.retriaged,
            claims=self.claims,
            override="six short reports",
        )
        self.assertEqual(result["state"], "NEEDS-SIGN-OFF")
        self.assertEqual(result["duplicates"], [{"id": "V30", "duplicates": "AUD-3"}])


class TestCli(unittest.TestCase):
    def _run(self, argv):
        buf = io.StringIO()
        code = 0
        with contextlib.redirect_stdout(buf):
            try:
                mod.main(argv)
            except SystemExit as exc:
                code = exc.code
        return code, json.loads(buf.getvalue())

    def test_blind_and_signoff_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "findings.json").write_text(
                json.dumps({"findings": TestBlind.FINDINGS})
            )
            (tmp / "tm.json").write_text(json.dumps(THREAT_MODEL))
            code, blinded = self._run(
                [
                    "blind",
                    str(tmp / "findings.json"),
                    "--threat-model",
                    str(tmp / "tm.json"),
                ]
            )
            self.assertEqual(code, 0)
            self.assertEqual(blinded["findings_total"], 4)
            (tmp / "claims.json").write_text(json.dumps(blinded))
            verdicts = [verdict(c["id"]) for c in blinded["claims"]]
            (tmp / "v.json").write_text(json.dumps(verdicts))
            code, result = self._run(
                [
                    "signoff",
                    "--threat-model",
                    str(tmp / "tm.json"),
                    "--verdicts",
                    str(tmp / "v.json"),
                    "--claims",
                    str(tmp / "claims.json"),
                ]
            )
            self.assertEqual(code, 0)
            self.assertEqual(result["state"], "PASS")
            verdicts[0]["severity"] = "high"
            verdicts[0]["verdict"] = "solid"
            (tmp / "v.json").write_text(json.dumps(verdicts))
            code, result = self._run(
                [
                    "signoff",
                    "--threat-model",
                    str(tmp / "tm.json"),
                    "--verdicts",
                    str(tmp / "v.json"),
                ]
            )
            self.assertEqual(code, 1)  # BLOCKED exits non-zero
            self.assertEqual(result["state"], "BLOCKED")

    def test_lint_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "findings.json"
            path.write_text(json.dumps({"findings": [{"id": "A-1", "title": "ok"}]}))
            code, result = self._run(["lint", str(path)])
            self.assertEqual(code, 0)
            self.assertTrue(result["clean"])
            path.write_text(
                json.dumps({"findings": [{"id": "A-1", "title": "dup of SF-2"}]})
            )
            code, result = self._run(["lint", str(path)])
            self.assertEqual(code, 1)
            code, result = self._run(["lint", str(path), "--allow", "SF-2"])
            self.assertEqual(code, 0)

    def test_bad_subcommand(self):
        code, result = self._run(["frobnicate"])
        self.assertEqual(code, 2)
        self.assertIn("error", result)


if __name__ == "__main__":
    unittest.main()
