---
description: "Gate security claims before disclosure: collect the project's own threat model and published audits, triage every claimed finding blind with the security-claims-skeptic, anchor severities to the project's precedents, and emit a machine-readable sign-off. Nothing goes to a security team without it."
argument-hint: "<findings.json> [project path] [--repo OWNER/REPO] [--max-disclosure N] [--override REASON]"
allowed-tools: ["Bash", "Read", "Grep", "Glob", "Task", "Write"]
---

# Security gate

Run this **before** anything from a review is reported to a project's security team, and before any
finding is called a vulnerability in a report, an issue or a publication.

**Why it exists.** In one real review, 84 items were reported to a project's security team as
vulnerabilities, 13 of them rated "medium". A strict re-triage against the project's own published
audit kept **6**, all Low. It also found that one item repeated a finding the project had already
published and accepted, and that none of the mediums was medium on the project's own scale. The
re-triage took minutes, but it ran after the report had been sent and the team's time spent. This
command makes that pass mandatory, and makes it happen first.

**Input:** "$ARGUMENTS". That is a `findings.json` (a consolidated run, or any list of findings) and
the project checkout (default: the current directory). Write everything to `<run_dir>/gate/`, never
into the checkout. Security material stays private: never commit the gate directory to a public repo.

## Line 1 of `SECURITY-GATE.md`

```
SECURITY-GATE: SOLID=6 ARGUABLE=25 BUG=53 THREAT-MODEL=found PRECEDENTS=42 -- NEEDS-SIGN-OFF
```

`security_gate.py signoff` computes the terminal state. Do not choose it yourself:

| State | When |
|---|---|
| `PASS` | The threat model was found; every claim has a well-formed verdict; every severity above low names a precedent of at least that severity; no `solid` claim duplicates a precedent; at most `--max-disclosure` (default 5) claims are `solid` |
| `NEEDS-SIGN-OFF` | As PASS, but the threat model is only `partial`, an `--override` was used, or a `solid` claim is rated above low: a human confirms |
| `BLOCKED` | No threat model; a claim without a verdict; an unanchored severity; a `solid` duplicate; or too many `solid` items without an override |

PASS means the disclosure set is defensible. It does not mean it must be sent: **sending is always the
user's decision**, made after reading `SECURITY-GATE.md`.

## Workflow

### 1. Collect the threat model

```bash
mkdir -p <run_dir>/gate
python3 <plugin_root>/scripts/find_threat_model.py <project> [--repo OWNER/REPO] \
  > <run_dir>/gate/threat-model.json
python3 <plugin_root>/scripts/find_threat_model.py <project> [--repo OWNER/REPO] --format md \
  > <run_dir>/gate/threat-model.md
```

Run both with a 300000 ms Bash timeout. Then:

- **Read every entry in `errors`.** "Advisories not queried" is not "no advisories": fix the cause
  (`gh auth status`, `--repo`) or say in the report that the source was not checked.
- **Read every URL in `external_policy_urls`** (a `SECURITY.md` often only points to a web page) and
  add what it says about scope to `threat-model.md` under a `## From <url>` heading.
- **Look for audits outside the tree.** Search the web for "<project> security audit" and the
  audit-firm publication repositories. If you find one, add its finding table (id, title,
  severity, status) to `threat-model.md`, and its rows to `precedents` in `threat-model.json`.
- `status: missing` stops here with `BLOCKED`. Ask the user for the project's threat model, or for
  explicit permission to proceed without one, which then caps every severity at low and needs sign-off.

### 2. Blind the claims

```bash
python3 <plugin_root>/scripts/security_gate.py blind <findings.json> \
  --threat-model <run_dir>/gate/threat-model.json > <run_dir>/gate/claims-blind.json
```

This keeps findings with a non-empty `security_claim`, plus legacy `security: true` or a `kind` of
vulnerability or hardening. It strips the finder's severity, classification and impact prose, and
attaches `precedent_hints`. If the user says some findings without a claim are security-relevant,
add `security_claim` to them first: an unclaimed finding is not a security item.

### 3. Triage blind

Dispatch **security-claims-skeptic** with the paths to `threat-model.md`, `threat-model.json`,
`claims-blind.json`, the project checkout, any reproducers, and its output path
`<run_dir>/gate/verdicts.json`. For more than about 40 claims, split them across two or three
skeptic instances by id range and concatenate their outputs. Don't give them the finder's report,
severity or reasoning: the blindness is the point.

### 4. Sign off

```bash
python3 <plugin_root>/scripts/security_gate.py signoff \
  --threat-model <run_dir>/gate/threat-model.json --verdicts <run_dir>/gate/verdicts.json \
  --claims <run_dir>/gate/claims-blind.json [--max-disclosure 5] [--override "<reason>"] \
  > <run_dir>/gate/signoff.json
```

It exits 1 when `BLOCKED`. Fix what `blocking` lists: re-run the skeptic on the claims it missed, or
cap the unanchored severities. Don't edit verdicts to pass the gate. An `--override` needs a reason the
user gave, such as "six independent Low items, each sent as its own short report". It is recorded in
the sign-off and always forces `NEEDS-SIGN-OFF`.

### 5. Write `SECURITY-GATE.md`

```markdown
SECURITY-GATE: SOLID=n ARGUABLE=n BUG=n THREAT-MODEL=<status> PRECEDENTS=n -- <STATE>

# Security gate — <project> @ <commit>

## Threat model
[Sources found, sources not checked (the `errors`), the scope statements and rules of engagement
 that decided verdicts, precedents used as anchors.]

## Disclose (solid) — one short report each
| Claim | Attacker | Gain | Severity | Precedent | Strongest counterargument |

## Ask (arguable) — titles only, "would you like details?"
| Claim | Severity | Why it is arguable |

## Not security (bug) — normal channel, subject to publication coordination
| Claim | Rubric question it fails |

## Duplicates of the project's own record
## Overstatements to correct
[Every overstatement from the verdicts. If a report was already sent, these become the corrections
 owed in a follow-up.]

## Sign-off
[`blocking` / `needs_review` from signoff.json, one line each.]
```

## Disclosure guidance

- **Lead small.** Send the `solid` items as separate short reports, each with its attacker,
  preconditions, gain, a minimal reproducer inline and the precedent it mirrors. List the `arguable`
  titles at the end and offer details. A long list buries the items that matter.
- **No unsolicited archives.** Put reproducers inline. Send an archive only if the team asks for one.
- **Say how you tested.** Say that nothing was run against the project's live services, and that the
  project's rules of engagement were respected.
- **Separate the two questions.** For items that are `bug` but might still be sensitive to publish,
  ask the team whether they want them held back. Don't claim them as vulnerabilities.
- **Don't rush.** A day's delay costs nothing. An overclaimed report costs the security team's time,
  and the reporter's credibility with them.

## Findings schema

The gate reads two fields from findings, and the consolidation step of `explore` writes them:

- `security_claim: {attacker, preconditions, gain}`: empty unless the finder can name all three. An
  empty claim means the finding is not a security item, whatever module it touches.
- `publication_sensitive: true|false`: whether publication should wait for coordination with the
  project. Cheap, and right under doubt. It never makes a finding a security claim.

The gate writes `security_vulnerability` (`solid` → true, `arguable` → "arguable", `bug` → false)
back into the findings only after sign-off. Nothing upstream of the gate sets it.

## Rules

- **Never send, file or post anything.** The gate produces a verdict and a draft. Disclosure is the
  user's decision.
- The sign-off line is parsed by other tools. Keep it on line 1, and don't reformat it or add fields
  before the terminal state.
- **Report the denominator.** "6 solid" means something only next to "of 84 claims, against a threat
  model with 42 precedents".
