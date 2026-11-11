# Security-gate regression fixture

A fictional project (`project/`: a security policy with an out-of-scope list and rules of
engagement, and a published audit with eight findings, two of them accepted) and twelve synthetic
security claims (`findings.json`), written the way an over-eager review writes them: "anyone",
"no account needed", medium severities without precedents. Nothing here is taken from a real
project or report.

Each claim exercises one rubric question in `agents/security-claims-skeptic.md`:

| Claim | What a careful triager concludes |
|---|---|
| C01 | **solid**: the fixed twin of a security-motivated audit fix (QS-1); bypasses a guard meant for session holders |
| C02 | duplicate of the accepted audit finding QS-6 |
| C03 | bug: a staff tool, no attacker |
| C04 | bug: needs the operator's own infrastructure to fail |
| C05 | bug: the attacker already holds the session |
| C06 | bug: authenticated, rate-limited, cost-symmetric DoS; out of scope by policy |
| C07 | **solid**: cheap, unauthenticated and site-wide; at most medium, anchored to QS-2 |
| C08 | duplicate of the accepted QS-7; a public TODO |
| C09 | arguable: a parser differential with installer impact inferred, not shown |
| C10 | bug: an "oracle" that gives nothing beyond the login form |
| C11 | bug: out of scope by policy |
| C12 | **solid**: an incomplete fix of the remediated QS-4 (the same leak through a sibling path), recorded as `incomplete_fix_of`, not a duplicate |

`expected.json` holds the acceptable verdicts, severity ceilings and required duplicates.

## Deterministic checks (unit tests)

`tests/test_security_gate_fixture.py` checks that `find_threat_model.py` finds the policy, the
eight precedents, the two accepted risks and the rule of engagement; that `security_gate.py blind`
strips the finder's severity and impact and hints QS-6 / QS-7 for the duplicates; that the
expected verdicts pass `signoff`; and that the claims as the finder rated them are `BLOCKED`.

## Agent evaluation (manual)

To measure the skeptic itself, copy `project/` somewhere outside any git repository and run:

```bash
S=plugins/code-review-toolkit/scripts
python3 $S/find_threat_model.py /tmp/quayside --no-network --format md > /tmp/gate/threat-model.md
python3 $S/find_threat_model.py /tmp/quayside --no-network > /tmp/gate/threat-model.json
python3 $S/security_gate.py blind tests/fixtures/security_gate/findings.json \
  --threat-model /tmp/gate/threat-model.json > /tmp/gate/claims-blind.json
# dispatch security-claims-skeptic on those files (there is no code: judge from the claim text),
# writing /tmp/gate/verdicts.json, then:
python3 $S/security_gate.py score --verdicts /tmp/gate/verdicts.json \
  --expected tests/fixtures/security_gate/expected.json
```

A healthy skeptic agrees on at least 10 of 12, names both duplicates and exceeds no ceiling.
First run (before `incomplete_fix_of` existed): 11/12, both duplicates named, no ceiling exceeded.
The miss was C12, treated as a duplicate of the remediated QS-4; that led to the new field.
Record the result in the changelog entry of any change to the agent's prompt.
