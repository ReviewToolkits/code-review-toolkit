"""Tests for find_threat_model.py: a project's own security record, from synthetic repos."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

from helpers import TempProject, import_script

mod = import_script("find_threat_model")

SECURITY_MD = """\
# Security Policy

Report vulnerabilities to security@example.org.

## In scope

- The web application and its API
- The upload pipeline

## Out of scope

- Denial of service that needs an authenticated account
- Issues that require a compromised administrator account
- Findings from automated scanners without a demonstrated impact

## Accepted risks

- Usernames are public by design.
"""

AUDIT_POST = """\
---
title: "Example completes its first security audit"
---

# Example completes its first security audit

The audit was performed by Trail of Bits. The full report is at
https://github.com/trailofbits/publications/blob/master/reviews/example.pdf

| ID | Title | Severity | Difficulty | Status |
| :-- | :-- | :-- | :-- | :-- |
| [EX-1](#ex-1) | Missing authorization on the account export endpoint | High | Medium | [Remediated](https://example.org/pull/10) |
| EX-2 | Session cookie lacks the SameSite attribute | Low | Low | [Remediated](https://example.org/pull/11) |
| EX-3 | Login throttle bypass via a spoofed forwarding header | Informational | High | Accepted |
| EX-4 | Unescaped wildcard in admin search | Moderate | Low | Accepted risk |
"""


def run(root, **kwargs):
    kwargs.setdefault("network", False)
    return mod.analyze(str(root), **kwargs)


class TestPolicy(unittest.TestCase):
    def test_security_md_scope_sections_extracted(self):
        with TempProject({"SECURITY.md": SECURITY_MD}) as root:
            result = run(root)
        self.assertEqual(result["status"], "found")
        policy = result["policy"][0]
        self.assertEqual(policy["path"], "SECURITY.md")
        self.assertEqual(policy["kind"], "policy")
        headings = [s["heading"] for s in policy["scope_sections"]]
        self.assertEqual(headings, ["In scope", "Out of scope", "Accepted risks"])
        out_of_scope = policy["scope_sections"][1]["items"]
        self.assertIn(
            "Denial of service that needs an authenticated account", out_of_scope
        )
        self.assertFalse(policy["pointer_only"])

    def test_github_dir_pointer_only_policy(self):
        # A SECURITY.md that only points at a web page has no scope of its own:
        # the URL must be surfaced, and it alone does not make the model `found`.
        text = "# Security Policy\n\nSee https://example.org/security/ for how to report.\n"
        with TempProject({".github/SECURITY.md": text}) as root:
            result = run(root)
        self.assertEqual(result["status"], "partial")
        self.assertTrue(result["policy"][0]["pointer_only"])
        self.assertIn("https://example.org/security/", result["external_policy_urls"])

    def test_code_urls_are_not_policy_pages(self):
        text = (
            "# Security Policy\n\nSee https://example.org/security/ and "
            "https://github.com/o/r/blob/main/pkg/security_policy.py\n"
        )
        with TempProject({"SECURITY.md": text}) as root:
            result = run(root)
        self.assertEqual(
            result["external_policy_urls"], ["https://example.org/security/"]
        )

    def test_html_policy_page_sections(self):
        page = """
        <h1>Reporting a security issue</h1>
        <p>Do not report in public:</p>
        <ul><li>
          the issue tracker
        </li><li>chat</li></ul>
        <h2>If you found an issue with the service itself</h2>
        <p>Email security@example.org.</p>
        """
        with TempProject({"templates/pages/security.html": page}) as root:
            result = run(root)
        policy = result["policy"][0]
        headings = [s["heading"] for s in policy["sections"]]
        self.assertIn("If you found an issue with the service itself", headings)
        first = next(
            s
            for s in policy["sections"]
            if s["heading"] == "Reporting a security issue"
        )
        self.assertEqual(first["items"], ["the issue tracker", "chat"])

    def test_rst_policy_headings(self):
        text = "Security\n========\n\nOut of scope\n------------\n\n- self-XSS\n- clickjacking on static pages\n"
        with TempProject({"SECURITY.rst": text}) as root:
            result = run(root)
        scope = result["policy"][0]["scope_sections"]
        self.assertEqual(scope[0]["heading"], "Out of scope")
        self.assertEqual(
            scope[0]["items"], ["self-XSS", "clickjacking on static pages"]
        )

    def test_security_model_doc_is_classified(self):
        text = "# Security model\n\n## Threat model\n\n- Maintainers are trusted.\n"
        with TempProject({"docs/security-model.md": text}) as root:
            result = run(root)
        self.assertEqual(result["policy"][0]["kind"], "security-model")
        self.assertEqual(
            result["policy"][0]["scope_sections"][0]["items"],
            ["Maintainers are trusted."],
        )


class TestAudits(unittest.TestCase):
    def test_audit_table_becomes_precedents(self):
        with TempProject({"docs/blog/2024-01-01-audit.md": AUDIT_POST}) as root:
            result = run(root)
        self.assertEqual(result["status"], "found")
        audit = result["audits"][0]
        self.assertEqual(audit["firms"], ["Trail of Bits"])
        self.assertTrue(audit["report_links"][0].endswith("example.pdf"))
        by_id = {p["id"]: p for p in result["precedents"]}
        self.assertEqual(set(by_id), {"EX-1", "EX-2", "EX-3", "EX-4"})
        # Markdown links are stripped from cells, and the fix PR is kept.
        self.assertEqual(
            by_id["EX-1"]["title"], "Missing authorization on the account export endpoint"
        )
        self.assertEqual(by_id["EX-1"]["severity"], "high")
        self.assertEqual(by_id["EX-1"]["status"], "remediated")
        self.assertEqual(by_id["EX-1"]["fix_refs"], ["https://example.org/pull/10"])
        self.assertEqual(by_id["EX-3"]["severity"], "informational")
        self.assertEqual(by_id["EX-4"]["severity"], "medium")  # "Moderate" normalized
        accepted = {a["id"] for a in result["accepted_risks"]}
        self.assertEqual(accepted, {"EX-3", "EX-4"})
        self.assertEqual(
            result["severity_scale"],
            {"high": 1, "low": 1, "informational": 1, "medium": 1},
        )

    def test_table_without_severity_column_is_ignored(self):
        text = "# Our security audit\n\nAudited by NCC Group.\n\n| ID | Title | Status |\n|---|---|---|\n| A-1 | x | Fixed |\n"
        with TempProject({"docs/audit.md": text}) as root:
            result = run(root)
        self.assertEqual(len(result["audits"]), 1)  # the firm still marks it an audit
        self.assertEqual(result["precedents"], [])

    def test_severity_table_outside_an_audit_is_ignored(self):
        # A log-level table is not a security record.
        text = "# Logging\n\n| Name | Severity |\n|---|---|\n| warn | Low |\n"
        with TempProject({"docs/logging.md": text}) as root:
            result = run(root)
        self.assertEqual(result["audits"], [])
        self.assertEqual(result["status"], "missing")

    def test_incident_report_listed(self):
        text = "# Incident report: leaked token\n\nWhat happened.\n"
        with TempProject({"docs/blog/2024-incident-report.md": text}) as root:
            result = run(root)
        self.assertEqual(
            result["incidents"][0]["title"], "Incident report: leaked token"
        )
        self.assertEqual(result["status"], "partial")

    def test_vendored_docs_are_skipped(self):
        with TempProject({"node_modules/pkg/SECURITY.md": SECURITY_MD}) as root:
            result = run(root)
        self.assertEqual(result["policy"], [])
        self.assertEqual(result["status"], "missing")


class TestNetworkSources(unittest.TestCase):
    ADVISORIES: ClassVar[list] = [
        {
            "ghsa_id": "GHSA-abcd-efgh-ijkl",
            "cve_id": "CVE-2025-0001",
            "summary": "Path traversal in upload",
            "severity": "moderate",
            "state": "published",
            "published_at": "2025-01-01T00:00:00Z",
            "html_url": "https://github.com/o/r/security/advisories/GHSA-abcd-efgh-ijkl",
        },
        {"not": "an advisory"},
    ]

    def _write(self, root, name, data):
        path = Path(root) / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return str(path)

    def test_advisories_from_saved_response(self):
        with TempProject({}) as root, tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "adv.json", self.ADVISORIES)
            result = run(root, advisories_path=path)
        self.assertEqual(result["status"], "found")
        (adv,) = result["advisories"]
        self.assertEqual(adv["id"], "GHSA-abcd-efgh-ijkl")
        self.assertEqual(adv["severity"], "medium")
        self.assertEqual(result["precedents"], [adv])

    def test_paginated_slurp_is_flattened(self):
        pages = [self.ADVISORIES[:1], self.ADVISORIES[:1]]
        self.assertEqual(len(mod.parse_advisories(pages)), 2)

    def test_issue_search_parsed(self):
        data = {
            "total_count": 2,
            "items": [
                {
                    "number": 5,
                    "state": "closed",
                    "title": "Fix XSS",
                    "pull_request": {},
                },
                {"number": 6, "state": "open", "title": "Harden cookies"},
            ],
        }
        parsed = mod.parse_issue_search(data)
        self.assertEqual(parsed["total"], 2)
        self.assertEqual([i["is_pr"] for i in parsed["items"]], [True, False])

    def test_unreadable_saved_response_is_an_error_not_empty(self):
        with TempProject({}) as root:
            result = run(root, advisories_path=str(Path(root) / "missing.json"))
        self.assertTrue(any("could not read" in e for e in result["errors"]))

    def test_no_network_is_reported(self):
        # "No advisories" and "did not ask" are different facts.
        with TempProject({}) as root:
            result = run(root)
        self.assertTrue(any("--no-network" in e for e in result["errors"]))
        self.assertFalse(result["security_issues"]["queried"])


class TestHistoryAndReferences(unittest.TestCase):
    def test_security_commits_and_references(self):
        files = {
            "CHANGELOG.md": "## 1.2\n\n- Fix CVE-2024-12345 and GHSA-2345-6789-cfgh.\n",
            "app.py": "x = 1\n",
        }
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.org",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.org",
        }
        with TempProject(files) as root:
            for args in (
                ["init", "-q"],
                ["add", "."],
                ["commit", "-q", "-m", "Fix a security issue in the upload path"],
                [
                    "commit",
                    "-q",
                    "--allow-empty",
                    "-m",
                    "Fix the advisory lock deadlock",
                ],
            ):
                subprocess.run(["git", "-C", str(root), *args], check=True, env=env)
            result = run(root)
        self.assertEqual(result["security_commits"]["total"], 1)  # not "advisory lock"
        self.assertIn(
            "security issue", result["security_commits"]["recent"][0]["subject"]
        )
        self.assertIn("CVE-2024-12345", result["references"]["cve"])
        self.assertIn("GHSA-2345-6789-cfgh", result["references"]["ghsa"])
        self.assertEqual(result["status"], "partial")

    def test_engagement_rules(self):
        page = "<li>Do not use Example for security research of any kind without written authorization.</li>\n"
        with TempProject({"templates/register.html": page}) as root:
            result = run(root)
        (rule,) = result["engagement_rules"]
        self.assertEqual(rule["line"], 1)
        self.assertNotIn("<li>", rule["text"])


class TestStatusAndRendering(unittest.TestCase):
    def test_empty_project_is_missing(self):
        with TempProject({"pkg/__init__.py": ""}) as root:
            result = run(root)
        self.assertEqual(result["status"], "missing")
        self.assertEqual(result["precedents"], [])

    def test_markdown_header_line_and_unanchored_warning(self):
        with TempProject({"SECURITY.md": SECURITY_MD}) as root:
            text = mod.render_markdown(run(root))
        self.assertIn("THREAT-MODEL: found PRECEDENTS=0", text)
        self.assertIn("No severity above Low can be anchored", text)
        self.assertIn("Out of scope", text)

    def test_markdown_lists_precedents_and_accepted(self):
        with TempProject({"docs/audit.md": AUDIT_POST}) as root:
            text = mod.render_markdown(run(root))
        self.assertIn(
            "| EX-1 | Missing authorization on the account export endpoint | high | remediated |",
            text,
        )
        self.assertIn("## Accepted risks", text)

    def test_main_md_format(self):
        import contextlib
        import io

        with TempProject({"SECURITY.md": SECURITY_MD}) as root:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                mod.main([str(root), "--no-network", "--format", "md"])
        self.assertTrue(buf.getvalue().startswith("# Threat model sources"))


if __name__ == "__main__":
    unittest.main()
