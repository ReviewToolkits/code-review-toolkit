# Finding fields and the classification rubric

Every agent's findings carry the same fields, whatever the agent's own output template looks like,
and are classified by one rubric. Consolidation (`explore` Phase 3) reads these fields; a finding
missing one is treated as a `candidate`.

## Fields, per finding

| Field | Content |
|---|---|
| `status` | `reproduced` (a reproducer showed it), `confirmed` (verified by reading, with the evidence named), `candidate` (unverified), or `refuted`. See `docs/reproduction-convention.md`. |
| Failure scenario | Concrete inputs or interleaving → the wrong outcome. "Could be a problem" is not a scenario. |
| Reproducer | When one exists: the artifact path, the command that runs it, and the observed output (its last `BUG:`/`OK:` line). |
| `branch_status` | When unmerged branches were checked: `fixed` / `partially-fixed` / `unchanged` / `moved` / `worse` on `<branch>`, and whether the reproducer confirmed it. Omit when no branch was checked; never write `unchanged` without having looked. |
| Location | `path:line` (or a range) that exists at the reviewed commit. |

Put them in the agent's own template where it has a slot, or as a short line under each finding:

```
status: reproduced · scenario: two uploads in one second → second overwrites the first's row ·
repro: artifacts/<agent>/test_upload_race.py → "BUG: 1 row, expected 2" · branch: unchanged on release-2
```

## Classification rubric

One rubric for every agent and for consolidation, so counts stay comparable across runs and
versions:

- **FIX**: an **observable wrong outcome** (wrong result, lost or corrupted data, a crash, a silent
  failure, a security-relevant behaviour) **plus a trigger** that reaches it (inputs, an
  interleaving, a configuration a user can have). Both halves must be named; a reproducer or a
  quoted code path supplies them.
- **CONSIDER**: a likely improvement with no demonstrated wrong outcome: maintainability,
  readability, a missing test, a hardening step, a defect whose trigger is unreachable today.
- **POLICY**: a decision only the maintainers can make (a convention, a compatibility trade-off).
- **ACCEPTABLE**: checked and fine; counted, not listed.

A finding moves between classes only by evidence: a reproducer promotes a CONSIDER whose trigger it
reaches; failing to find a trigger demotes a FIX. Severity words in an agent's report ("critical",
"serious") don't change the class.
