---
description: "Like `explore`, but INFORMED: every agent first reads a briefing of recurring Python bug SHAPES (sibling-hunt templates with their guarded twins), the false-positive taxonomy, and the cross-cutting triage rules — so it confirms-without-relitigating, suppresses known FPs, and hunts un-found siblings of established shapes instead of re-discovering from scratch. Use for a thorough audit, a re-review of a codebase already analyzed, or whenever fix-propagation matters more than raw speed."
argument-hint: "[scope] [aspects] [options]"
allowed-tools: ["Bash", "Glob", "Grep", "Read", "Task"]
---

# Informed Codebase Review

Same coverage as [`explore`](explore.md), but **informed**: agents are seeded with the toolkit's
accumulated knowledge before they triage. That is what turns a cold, re-discovering pass into a
**fix-propagation sweep** — instead of rediscovering that a mutable default argument is a bug, the
agent starts from the shape and hunts every un-found sibling of it.

**Arguments:** "$ARGUMENTS"

**Plugin root:** `<plugin_root>` refers to the directory containing this command file's parent —
i.e. the `plugins/code-review-toolkit/` directory. Resolve it relative to this file's location.

## Argument Parsing

Identical to [`explore`](explore.md) — scope, aspects, and options are parsed the same way. The
informed run additionally honors:

- `--catalog <path>` → also fold in an external findings catalog (see *Phase 1.5*). Accepts a
  `findings.json`, a project directory, or a findings-repo root. `--catalog-dir` is a synonym.
- `--shapes-only` → restrict each agent to hunting catalogued shapes and their siblings, skipping
  open-ended analysis. Fastest way to answer "does this codebase have any of the known shapes?"

## Execution Workflow

### Phase 0: Project Discovery

As in `explore`: identify the project layout, languages, and package structure.

### Phase 0.5: External Tool Analysis (optional)

As in `explore`: run ruff/mypy/vulture/coverage when available and keep the output for
cross-referencing.

### Phase 1: Foundational Context

As in `explore`: run **architecture-mapper** and **git-history-context** first; their output feeds
every later agent.

### Phase 1.5: Build the informed briefing ← the step that makes this command different

Generate the briefing once, then hand it to every agent. Write it to a fresh directory, not a fixed
path, so two concurrent runs cannot overwrite each other's briefing; reuse the literal path `echo`
prints, because each Bash call is a fresh shell:

```bash
d=$(mktemp -d "${TMPDIR:-/tmp}/crt-informed-explore.XXXXXX") && echo "$d" &&
python <plugin_root>/scripts/build_informed_briefing.py [scope] > "$d/briefing.md"; echo "exit=$?"
```

For a per-agent briefing scoped to just the shapes that agent owns (shorter, more focused):

```bash
python <plugin_root>/scripts/build_informed_briefing.py [scope] --agent silent-failure-hunter
```

The briefing assembles three things:

1. **Bug-shape templates** from `data/python_bug_shapes.json` — each with its *pattern*, its
   *guarded twin* (the fix pattern, usually already present elsewhere in the same codebase), a
   *sibling-hunt* directive, the *expected* behavior, and how the defect *surfaces* (most are
   silent).
2. **The false-positive taxonomy** from `data/python_non_bugs.md` — the classes to dismiss *with a
   stated reason*, each paired with what the real bug looks like so a genuine instance is never
   suppressed.
3. **Cross-cutting triage rules** — guarded-twin, systemic-root-over-instance-count, silent-beats-
   loud, behavioural-divergence-outranks-stylistic, confirm-don't-re-litigate, cite-or-drop.

If the target codebase carries a findings memory at `<scope>/.code-review/findings.json` from prior
runs, its **confirmed** entries are folded in automatically as "verify, then move on" items.

`--catalog <path>` folds in an external findings repo as well — the usual case being one of the
`*-review-findings` companions:

```bash
python <plugin_root>/scripts/build_informed_briefing.py [scope] \
    --agent pattern-consistency-checker \
    --catalog ~/projects/code-review-findings
```

It splits what it finds in two, and the distinction matters:

- **Findings recorded for THIS project** — settled. Verify each still exists, then move on. Never
  narrowed by `--agent`, because an entry dropped from the do-not-re-derive set is one the agent will
  cheerfully re-derive.
- **Findings from OTHER projects** — *not* claims about this codebase. Each is a shape confirmed
  somewhere else, so it is worth a targeted look here. A hit is a new finding; a miss is not a
  finding at all, so absence is never reported. This list IS narrowed to the requested agent's
  shapes, with the dropped count stated.

Problems reading a catalog (a path that resolved to nothing, an unreadable file) go to **stderr**, so
a `--catalog` that silently found no memory cannot masquerade as a complete briefing on stdout.

### Phase 2: Targeted Analysis (informed)

Dispatch the same agent groups as `explore` (Groups A–E). The difference is the prompt: **prepend
the briefing to every agent's instructions**, followed by:

> Before your own analysis, read the briefing above. Then:
> 1. **Hunt the shapes you own.** For each template, search this codebase for instances. Report
>    `file:line` plus a concrete failure scenario for each.
> 2. **Follow the sibling hunt.** For every instance you confirm, run that shape's hunt directive —
>    this is where a cold pass loses findings. One confirmed instance usually implies several.
> 3. **Find the guarded twin.** Locate the place in this codebase that already handles the shape
>    correctly. Cite it: it proves the shape is a defect by the project's own standard, and it is the
>    fix to propose.
> 4. **Suppress known false positives** using the taxonomy — dismiss with the stated reason rather
>    than reporting. Do not re-litigate what the taxonomy already settles.
> 5. **Do not re-derive established findings.** Confirm they still exist, then spend your effort on
>    what is *not* yet known.
> 6. **Then** do your own open-ended analysis for anything the catalog does not cover — a new shape
>    is the most valuable thing you can return, because it extends the catalog.

Agents must clearly separate the two kinds of output:

- **Catalogued** — instances of a known shape (cite the shape `id`).
- **Novel** — something no shape covers. Propose it as a *new* shape using the
  `python_bug_shapes.json` field structure so it can be added to the catalog.

### Phase 3: Synthesis (+ record to the project memory)

Produce the same report as `explore`, with three additions:

1. **Shape coverage table** — for every catalogued shape: instances found, sibling sites, and
   whether a guarded twin exists in this codebase.

   | Shape | Instances | Sibling sites | Guarded twin present | Verdict |
   |---|---|---|---|---|

2. **Proposed new shapes** — novel findings written in the catalog's field structure, ready to be
   appended to `data/python_bug_shapes.json`.

3. **Proposed new false-positive classes** — anything an agent flagged that triage dismissed, with
   the reason and the real-bug discriminator, ready for `data/python_non_bugs.md`.

Then **write the findings memory** so the next run is informed. Write it into the run directory
(`<run_dir>/findings-memory.json`), **not into the reviewed checkout**: the checkout belongs to
someone else, and a stray `.code-review/` directory in it is a side effect of reviewing. Next time,
pass it back with `--catalog <run_dir>/findings-memory.json` (its `project` must contain, or be
contained in, the target directory's name: that is how the briefing recognizes the entries as
this project's). Only when the user owns the checkout
and asks for it, write it to `<scope>/.code-review/findings.json`, where it is picked up
automatically:

```json
{
  "project": "<name>",
  "schema_version": 1,
  "findings": [
    {
      "id": "CRT-0001",
      "severity": "FIX",
      "title": "Mutable default argument in load_config()",
      "location": "pkg/config.py:42",
      "shape": "mutable-default-argument",
      "status": "confirmed",
      "evidence": "calling load_config() twice accumulates entries from the first call"
    }
  ]
}
```

`status` is one of `confirmed` (verified true positive), `reported` (raised upstream), `fixed`, or
`candidate` (unverified — **not** folded into future briefings). Only the first three are treated as
established. This schema matches `crate-local/findings.json` in the `*-review-findings` companion
repositories, so a project's memory can be lifted into a findings repo unchanged.

## Extending the catalogs

The catalogs are the toolkit's memory, and every run should improve them:

- A **confirmed novel finding** → add a shape to `data/python_bug_shapes.json`. Set
  `"validation": "confirmed"` and cite the instance in `confirmed_examples`.
- A **dismissed false positive** → add a class to `data/python_non_bugs.md`, including the
  discriminator that separates it from the real bug.
- A shape that was `"documented"` and is now confirmed in a real codebase → promote it to
  `"validation": "confirmed"`. **That promotion is the calibration loop.**

## When to use `explore` vs `informed-explore`

| Situation | Command |
|---|---|
| First look at an unfamiliar codebase | `explore` |
| Thorough audit, or fix-propagation matters | `informed-explore` |
| Re-review of a codebase already analyzed | `informed-explore` (it will not re-litigate) |
| You want independent, unbiased passes | `explore` (the briefing deliberately biases toward known shapes) |

The bias is the point — and also the tradeoff. An informed run is better at finding *siblings of
known shapes* and worse at noticing something genuinely unlike anything in the catalog. For a
high-stakes review, run both: naive passes first for independence, then an informed pass for
propagation.

## Usage Examples

```bash
/code-review-toolkit:informed-explore                      # whole project, all agents
/code-review-toolkit:informed-explore src/                 # narrowed scope
/code-review-toolkit:informed-explore . --shapes-only      # just hunt the known shapes
/code-review-toolkit:informed-explore . silent-failures    # one aspect, informed
```
