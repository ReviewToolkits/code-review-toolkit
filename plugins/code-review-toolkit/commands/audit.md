---
description: "Release-gating audit: run every scanner, require a machine-readable sign-off line, and BLOCK on an incomplete run. Converts the toolkit from exploratory to gating."
argument-hint: "[scope]"
allowed-tools: ["Bash", "Read", "Grep", "Glob", "Task"]
---

# Audit

An **audit** differs from an `explore` in one way that matters: it produces a verdict something else
can act on, and it refuses to produce a clean verdict from an incomplete run.

**An incomplete audit is worse than a dirty one.** A dirty audit gets fixed. An incomplete audit that
reports clean gets shipped.

**Scope:** "$ARGUMENTS" (path; default the whole project).

## Line 1 of `AUDIT.md`

The first line is machine-readable and is the whole point:

```
AUDIT-RESULT: FIX=3 CONSIDER=11 POLICY=2 SCANNERS=8/8 -- NEEDS-SIGN-OFF
```

Terminal state is one of:

| State | When |
|---|---|
| `CLEAN` | Every scanner ran, zero FIX findings |
| `NEEDS-SIGN-OFF` | Every scanner ran, and there are FIX or CONSIDER findings a human must accept |
| `BLOCKED` | **Any scanner failed, timed out, crashed, or analyzed zero files** |

`BLOCKED` is not a severity judgement — it means the audit does not know. A run where
`check_typing.py` returned `status: FAILED`, or a scanner raised, or `files_analyzed` is 0, is
`BLOCKED` **even if every finding it did produce was clean.**

## Workflow

### 1. Run every scanner, and record whether each one completed

```bash
for s in scan_python_pitfalls scan_concurrency analyze_imports find_dead_symbols measure_complexity \
         collect_debt correlate_tests count_types extract_test_invariants; do
  python3 <plugin_root>/scripts/$s.py [scope] > reports/audit/$s.json 2>reports/audit/$s.err
  echo "$s exit=$?"
done
python3 <plugin_root>/scripts/run_lint_rules.py [scope] --tier 1 > reports/audit/lint.json
python3 <plugin_root>/scripts/check_typing.py [scope] > reports/audit/typing.json
```

For each: a non-zero exit, an `error` key, a `status: FAILED`, or `files_analyzed: 0` is a
**scanner failure**. Count them. `SCANNERS=n/m` in the sign-off line is that count, and any `n < m`
forces `BLOCKED`.

**Read the stderr files.** `run_lint_rules.py` reports preview-gated and remapped rules only on
stderr; a rule that silently did nothing means its absence from the findings proves nothing.

### 2. Regression-check against the known catalog, if one exists

```bash
python3 <plugin_root>/scripts/check_known_findings.py [scope] --catalog <catalog.tsv>
```

Anything `present` is a **regression or a never-fixed finding** and belongs in the FIX count
regardless of what the fresh scan says about it.

### 3. Prior art before any novelty claim

Run `/prior-art` over the FIX findings. An audit that calls something novel without having searched
the tracker is making a claim it has not checked.

### 4. Triage — and count only what survives

Dispatch the owning agent for each finding class. **The counts in the sign-off line are
post-triage**: a raw scanner count is not an audit result. Every FIX must carry `file:line` and a
concrete failure scenario, or it is not a FIX.

### 5. Write `AUDIT.md`

```markdown
AUDIT-RESULT: FIX=n CONSIDER=n POLICY=n SCANNERS=n/m -- <STATE>

# Audit — <project> @ <commit>

## Scanner completeness
| Scanner | Status | Files | Note |
|---|---|---|---|
[Every scanner. A failure here is the reason for BLOCKED.]

## Coverage caveats
[Preview-gated lint rules that did not run. `not_scannable` catalog entries.
 Anything out of scope. State what was NOT examined before what was.]

## FIX ([n])
| # | Finding | File:line | Failure scenario | Status | Prior art |
|---|---|---|---|---|---|

## CONSIDER ([n])
## POLICY ([n])

## Known-findings regression
[still present / possibly fixed / not checked]

## Sign-off
[What a human must accept, one line each. Blank if CLEAN.]
```

## Rules

- **Never emit `CLEAN` with a scanner failure.** That is the failure mode this command exists to
  prevent.
- **Report the denominator with every zero.** "0 findings" and "0 findings across 44 files with 2
  scanners blocked" are different claims, and only one of them is an audit.
- **`status` on every FIX.** Reproduced and merely-confirmed findings must be distinguishable; see
  `<plugin_root>/docs/reproduction-convention.md`.
- The sign-off line is parsed by other tools. Do not reformat it, do not add fields before the
  terminal state, and keep it on line 1.
