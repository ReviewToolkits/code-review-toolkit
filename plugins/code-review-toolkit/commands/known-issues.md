---
description: "Cross-reference a catalog of previously-found findings against a fresh scan, so a fix — or a regression after a revert — is detected. Static and drift-tolerant: no repros are run."
argument-hint: "[scope] --catalog <known_findings.tsv>"
allowed-tools: ["Bash", "Read", "Grep", "Glob"]
---

# Known-findings regression

Answers one question: **of the findings we already know about, which are still in this tree?**

**Scope:** "$ARGUMENTS" — a path plus `--catalog <path to known_findings.tsv>`. The catalogs live in
the findings repo at `<project>/catalog/known_findings.tsv`.

## Run it

```bash
python3 <plugin_root>/scripts/check_known_findings.py [scope] --catalog <catalog.tsv>
```

Record the commit you checked (`git -C <project> rev-parse --short HEAD`). The catalog was captured
at a different commit, so the tree has moved — that is the normal case, not a problem.

## Verdicts

Keyed on **`(file, shape, qualname)`**, never on line numbers. Python's `ast` gives stable qualified
names, so a finding whose line moved is still `present` — there is no "drifted" verdict to triage.

| Verdict | Meaning | Confidence |
|---|---|---|
| `present` | A finding of that shape, in that qualname, in that file | High — still unfixed |
| `present_elsewhere` | The shape is in the file but the qualname is gone | Medium — read it |
| `absent_in_qualname` | The qualname still exists and carries no finding, while the file has others | Weak — *possibly* fixed here |
| `absent` | The file was scanned and has no finding of that shape anywhere | Weak — read the file |
| `out_of_scope` | The file exists but sits outside the scanned scope | **Not checked.** Widen the scope |
| `file_missing` | The path no longer exists | Read the history; renamed or deleted |
| `not_scannable` | The shape is `agent-only` — no scanner check exists | **Not checked.** Cannot be automated |

Multi-site findings take their **strongest** verdict: one site still present means the finding is
still present.

## The caveat, which must reach the report

**An `absent` verdict is not proof of a fix.** Two verdicts mean *we did not look*, and the summary
separates them into `not_checked` for exactly that reason:

- **`not_scannable` dominates.** 32 of the 90 catalogued shapes are `agent-only`, and on the
  coverage.py catalog **53 of 60 entries** land here. This command can regression-check the scanner
  shapes and nothing else; the rest need the agent that found them. Say so in the report — a summary
  reading "2 still present, 58 clear" would be a serious misrepresentation.
- **`out_of_scope`** means a catalog entry in `tests/` was checked against a scope of the package
  only. Widen the scope and re-run rather than reporting it.

For every `absent` and `absent_in_qualname`: **read the file** before concluding anything. A shape
whose instance carries no local token — anything project-level or cross-file — can be live and
unscannable at the same site.

## Report

```markdown
## Known-findings regression ([catalog], [N] entries, commit [sha])

**Checked: [N] of [M].** [not_scannable] entries name an agent-only shape and
[out_of_scope] sit outside the scope — neither was examined.

### Still present ([N]) — regressions or never-fixed
| ID | Shape | Was | Now | Severity |
|---|---|---|---|---|

### Possibly fixed ([N]) — each verified by reading the file
| ID | Shape | Verdict | Confirmed fixed? |
|---|---|---|---|

### Not checked ([N])
| ID | Shape | Why |
|---|---|---|
```

Finish by proposing catalog updates: a confirmed fix becomes `status: fixed` with the commit; a
still-present finding keeps its status and gains a re-confirmation date.
