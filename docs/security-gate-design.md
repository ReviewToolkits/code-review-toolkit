# Security-claims gate — design

Status: implemented on branch `feat/security-gate` (scripts `find_threat_model.py`, `security_gate.py`; agent `security-claims-skeptic`; command `security-gate`; fixture `tests/fixtures/security_gate/`).

## Why

A review run can label findings "security" far more readily than the project's maintainers would. In one real
review, 84 items were reported to a project's security team as vulnerabilities; a strict re-triage against the
project's own published audit kept **6** (all Low), found that one item duplicated a finding the project had already
published and accepted, and showed that none of the 13 items rated "medium" was medium on the project's own scale.
The re-triage took minutes; it ran after the report was sent. The gate makes that pass mandatory and earlier.

Root causes the gate addresses:

1. **"Security" is a cheap label.** Agents flag anything touching auth, rate limits, malware handling or audit
   events; nothing asks who the attacker is, what they gain, and whether the project considers it in scope.
2. **Two different questions get conflated:** "is it safe to publish this now?" (coordination) and "is this a
   vulnerability?" (a claim). Withholding from publication is cheap and right under doubt; claiming a vulnerability
   under doubt costs the security team's time.
3. **Severity has no anchor.** Without the project's own precedents, severity is intuition.
4. **Prior art misses the project's security record** (published audits, advisories), not just its tracker.
5. **No adversarial pass before disclosure.**
6. **Disclosure format:** dozens of items plus an archive in one email, instead of a few strong reports and a list.

## Components

### 1. `scripts/find_threat_model.py` + threat-model step

Collects the project's own security record into `threat-model.md` / `threat-model.json`:

- `SECURITY.md` (and `.github/SECURITY.md`), security policy pages in the docs or templates, bug-bounty scope
  documents (in scope / out of scope / accepted risks);
- **published audits**: docs and blog posts mentioning "audit", links to audit-firm publication repos and PDFs,
  finding tables (id, title, severity, status: remediated/accepted);
- GitHub security advisories (`gh api repos/O/R/security-advisories`) and CVE references;
- issues/PRs labelled security; merged commits whose message says security / vulnerability / CVE / GHSA.

Output: trusted parties (staff, CI, infrastructure), explicit out-of-scope and accepted risks, and **precedents** —
past findings with the severity the project itself assigned. If no source is found the gate **blocks** and asks a
human for the threat model rather than guessing.

### 2. `agents/security-claims-skeptic.md`

Runs **blind**: sees each claimed finding's mechanism and evidence, not the finder's severity or framing. Fixed rubric:

- attacker and required privilege; preconditions;
- needs staff action? needs the operator's own infrastructure to fail? needs an attacker who already controls the
  session or account? (each → not a vulnerability by default);
- gain beyond what the attacker could already do;
- demonstrated vs inferred impact (installer behaviour, production config);
- already public (open issue, source TODO), intended, documented, or accepted;
- duplicates an audit/advisory finding;
- cost asymmetry for availability claims (cheap and site-wide vs as costly to the attacker as to the server).

Per finding: `verdict` (`solid` / `arguable` / `bug`), `severity` **anchored to a named precedent** (anything above
Low without a precedent is capped at Low), the **strongest counterargument**, and **overstatements** in the claim.

### 3. `commands/security-gate.md`

Machine-readable sign-off, like `audit`:

```
SECURITY-GATE: SOLID=6 ARGUABLE=25 BUG=53 THREAT-MODEL=found PRECEDENTS=14 -- PASS|NEEDS-SIGN-OFF|BLOCKED
```

- disclosure may contain only `solid` items, one short report each;
- `arguable` items go as a title list ("would you like details?");
- `bug` items go to the normal channel (subject to publication coordination);
- `BLOCKED` when there is no threat-model source, a severity lacks a precedent, or a disclosure would exceed ~5
  items without an explicit override.

## Upstream changes

- Findings schema: replace boolean `security` with a required `security_claim: {attacker, preconditions, gain}`
  (empty by default), and split `publication_sensitive` (coordinate before publishing) from `security_vulnerability`
  (report as one). Gray-zone items may be the former without being the latter — ask the security team whether they
  consider them sensitive instead of claiming them.
- `prior-art`: published audits and advisories become a required source.
- Disclosure guidance: lead small; no unsolicited archives (inline reproducers); don't rush.

## Validation

- Regression fixture (anonymized): a claimed set of ~84 items and a project with a published audit; expected ≈6
  `solid`, the audit duplicate caught, no "medium" without a precedent.
- Unit tests for `find_threat_model.py` on synthetic repos (SECURITY.md variants, an audit table in a blog post,
  advisories JSON).
- Over time: record agreement with maintainers' eventual verdicts — the calibration number that matters.
