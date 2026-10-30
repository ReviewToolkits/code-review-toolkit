# Code Review Toolkit

A comprehensive collection of specialized agents for exploring and analyzing existing codebases. While a PR review toolkit reviews changes in a PR, this toolkit answers a different question: **where are the problems in this codebase, and what should I fix first?**

## Overview

This plugin bundles 19 expert analysis agents and 8 commands. Each agent focuses on a specific aspect of code quality and is designed for codebase-scale analysis — scanning entire modules or projects rather than reviewing diffs.

### Key Design Principles

- **Discovery-first**: Agents do a broad sweep, then offer ranked findings for drill-down
- **Context-aware**: Two foundational agents — architecture-mapper (structural) and git-history-context (temporal) — run first and feed their output to all other agents, so findings are contextualized by module importance and change patterns
- **Prioritized output**: Each agent caps its output to avoid overwhelming reports, ranking by severity and offering deeper analysis on request
- **Python-calibrated**: All agents are tuned for Python idioms (gradual typing, unittest, dynamic dispatch, `__init__.py` conventions, etc.)

## Installation

### Marketplace install

This is the recommended method. It registers the repository as a Claude Code marketplace so you can install and update the plugin through the built-in plugin manager.

```bash
# Add the marketplace (one-time)
claude plugin marketplace add ReviewToolkits/code-review-toolkit

# Install the plugin
claude plugin install code-review-toolkit@code-review-toolkit

# Update when new versions are available
claude plugin marketplace update
/plugin  # Go to Updates tab
```

### Local install (for development or testing)

```bash
git clone https://github.com/ReviewToolkits/code-review-toolkit.git
cd code-review-toolkit
```

Then in Claude Code:

```bash
/plugin install plugins/code-review-toolkit
```

### Manual install

Copy the `plugins/code-review-toolkit/` directory to one of:

- **User scope** (available in all projects): `~/.claude/plugins/code-review-toolkit/`
- **Project scope** (available in one project): `<project>/.claude/plugins/code-review-toolkit/`

### Using without installing

Load the plugin for a single session using `--plugin-dir` — nothing is persisted:

```bash
git clone https://github.com/ReviewToolkits/code-review-toolkit.git
claude --plugin-dir code-review-toolkit/plugins/code-review-toolkit
```

All slash commands and agents work for that session. Nothing is written to your Claude Code configuration.

### Prerequisites

- **Claude Code**: The plugin requires Claude Code to be installed and running.
- **Python 3.10+**: The analysis scripts use AST features and type syntax from Python 3.10+.
- **Git**: Required for the git-history-context and git-history-analyzer agents, and for tech-debt-inventory's age analysis.

No third-party Python packages are required — all scripts use only the standard library.

## Commands

### `/code-review-toolkit:explore [scope] [aspects] [options]`

The primary command. Runs the foundational context providers (architecture-mapper + git-history-context) first, then dispatches selected agents with both structural and temporal context.

```bash
# Full exploration (all 19 agents)
/code-review-toolkit:explore

# Specific directory
/code-review-toolkit:explore src/benchmarks

# Specific aspects only
/code-review-toolkit:explore . complexity errors tests

# Quick summary mode
/code-review-toolkit:explore . all summary
```

**Aspects**: `architecture`, `history-context`, `history`, `history-full`, `consistency`, `complexity`, `tests`, `pitfalls`, `concurrency`, `lint`, `errors`, `docs`, `project-docs`, `types`, `typing`, `dead-code`, `tech-debt`, `test-invariants`, `patterns`, `api`, `all`

**Options**: `deep` (full detail), `summary` (top-level only), `parallel` (concurrent agents), `--runs N` (independent naive passes, deduplicated), `--informed-reruns` (with `--runs 3`, the third pass targets adjacent code and siblings of what runs 1–2 confirmed)

### `/code-review-toolkit:informed-explore [scope] [aspects] [options]`

Same coverage as `explore`, but every agent first reads a briefing assembled from the toolkit's
catalogs: recurring Python bug **shapes** (each with its *guarded twin* — the fix pattern — and a
sibling-hunt directive), the false-positive taxonomy, and the cross-cutting triage rules.

The effect is fix-propagation: instead of re-deriving that a mutable default argument is a defect,
the agent starts from the shape and hunts every un-found sibling of it.

```bash
# Thorough audit, or a re-review of a codebase already analyzed
/code-review-toolkit:informed-explore

# Just check whether the known shapes are present
/code-review-toolkit:informed-explore . --shapes-only
```

Use `explore` for a first, unbiased look; use `informed-explore` when fix-propagation matters more
than independence. For a high-stakes review, run both — naive passes first, informed pass last.

Confirmed findings are recorded to `<scope>/.code-review/findings.json`, so the next informed run
verifies them and moves on rather than re-litigating.

### `/code-review-toolkit:map [scope]`

Quick architecture mapping only. The fastest way to get oriented.

```bash
/code-review-toolkit:map
/code-review-toolkit:map src/runner
```

### `/code-review-toolkit:hotspots [scope]`

Find cleanup targets: churn hotspots, complexity hotspots, dead code, and tech debt. Answers "where should I focus my cleanup efforts?"

```bash
/code-review-toolkit:hotspots
/code-review-toolkit:hotspots src/
```

### `/code-review-toolkit:health [scope]`

Quick health dashboard — all agents in summary mode, producing a scored table across every dimension.

```bash
/code-review-toolkit:health
```

### `/code-review-toolkit:audit [scope]`

Release-gating audit: runs every scanner, requires a machine-readable sign-off line, and **blocks**
on an incomplete run. This turns the toolkit from exploratory into gating.

### `/code-review-toolkit:known-issues [scope] --catalog <catalog.tsv>`

Cross-references a catalog of previously found findings against a fresh scan, so a fix, or a
regression after a revert, is detected. Static and drift-tolerant: no repros are run.

### `/code-review-toolkit:prior-art [finding | findings.json]`

Searches the project's tracker before a finding is called novel. It refutes wrong findings, and it
turns "file a new issue" into "comment on the existing one" when the issue is already reported.

## Agents

### Tier 1: Foundational

| Agent | Focus | Adapted From |
|-------|-------|-------------|
| **architecture-mapper** | Module structure, dependency graph, layering, circular deps | New |
| **git-history-context** | Churn metrics, change velocity, co-change clusters, module stability | New |
| **consistency-auditor** | Pattern divergence across codebase, convention discovery | code-reviewer |
| **complexity-simplifier** | Complexity hotspots ranked by severity, with simplification strategies | code-simplifier |

### Tier 2: Targeted Analysis

| Agent | Focus | Adapted From |
|-------|-------|-------------|
| **test-coverage-analyzer** | Source↔test correlation, undertested modules, coverage gaps | pr-test-analyzer |
| **pattern-consistency-checker** | Same concern solved different ways across modules | New |
| **python-pitfall-scanner** | Concrete correctness defects: mutable defaults, late-binding closures, `except` ordering, `return`-in-`finally`, asyncio pitfalls, mutation-during-iteration | New |
| **concurrency-auditor** | Lock-guard inference (state and helpers touched without the lock that guards them elsewhere), check-then-act, lock discipline and lock-order cycles, re-entry and RW upgrades, `Condition` misuse, blocking work under locks, and event-loop stalls followed through the call graph | ft-review-toolkit |
| **silent-failure-hunter** | Swallowed exceptions, bare except, silent error patterns | silent-failure-hunter |
| **lint-rule-triager** | Defect-grade ruff rules in explicit tiers, merged with the shape catalog; harvests rules with no shape yet | New |
| **typing-integrity-auditor** | mypy-provable defects, and annotations silently degraded to `Any` by phantom `TYPE_CHECKING` imports | New |
| **test-investigation-agent** | Tests read as invariant specifications, checked against untested paths and analogous code | New |
| **git-history-analyzer** | Fix completeness, similar bug detection, churn×quality risk matrix, historical context | New |

### Tier 3: Specific Concerns

| Agent | Focus | Adapted From |
|-------|-------|-------------|
| **documentation-auditor** | Docstring coverage, comment accuracy, stale docs | comment-analyzer |
| **type-design-analyzer** | Type hint coverage, dataclass design, Any overuse | type-design-analyzer |
| **dead-code-finder** | Unused imports, unreferenced functions, orphan files | New |
| **tech-debt-inventory** | TODOs/FIXMEs, deprecated usage, debt age analysis | New |
| **api-surface-reviewer** | Public API naming consistency, parameter conventions, learnability | New |
| **project-docs-auditor** | README/docs accuracy, cross-file consistency, reference validation | New |

## Recommended Workflows

### First Time Exploring a Codebase

```
1. /code-review-toolkit:map            → Understand structure
2. /code-review-toolkit:health         → Quick health assessment
3. /code-review-toolkit:explore . [weakest area] deep  → Deep dive
```

### Planning a Cleanup Sprint

```
1. /code-review-toolkit:hotspots       → Find cleanup targets
2. Address quick wins (dead code, unused imports)
3. Tackle complexity hotspots
4. /code-review-toolkit:health         → Verify improvement
```

### Before a Release

```
1. /code-review-toolkit:explore . api docs project-docs types  → Public surface quality
2. Fix API inconsistencies and documentation gaps
3. /code-review-toolkit:explore . errors tests history  → Reliability + fix completeness
```

### Ongoing Maintenance

```
# Monthly health check
/code-review-toolkit:health

# After major feature work
/code-review-toolkit:explore src/new_feature all

# Quarterly debt review
/code-review-toolkit:explore . tech-debt dead-code
```

## How Foundational Context Works

Two agents provide foundational context for all other agents:

**architecture-mapper** produces a structural model: module boundaries, dependency graph, fan-in/fan-out metrics, and layering. Other agents use it to:
- **Prioritize**: Findings in high fan-in modules (used by many others) are more critical
- **Contextualize**: A broad except in a CLI entry point is different from one in a core library module
- **Detect cross-cutting issues**: Patterns that span module boundaries vs. localized concerns

**git-history-context** produces a temporal model: churn metrics, change velocity, co-change clusters, and module stability. Other agents use it to:
- **Prioritize by volatility**: Findings in high-churn code are more urgent (and more likely to regress)
- **Detect recent changes**: Code flagged by another agent may have been recently changed intentionally
- **Focus testing effort**: High-churn + low-coverage areas are the highest-risk testing gaps

## Agent Configuration

All agents use `model: opus` for highest analysis quality. Each agent:

- Accepts a scope parameter (directory, file, glob, or whole project)
- Produces a summary tier (always shown, capped output) and a detail tier (available on request)
- Provides confidence ratings and severity levels for findings
- Includes specific file:line references and concrete fix suggestions

## Comparison with pr-review-toolkit

| Dimension | pr-review-toolkit | code-review-toolkit |
|-----------|------------------|-------------------|
| **Scope** | Git diff (recent changes) | Entire codebase or targeted scope |
| **Question** | "Are these changes good?" | "Where are the problems?" |
| **Discovery** | Knows what changed | Must discover structure first |
| **Output** | All findings shown | Ranked, capped, drill-down available |
| **Architecture** | Not needed (small diff) | Foundation for all analysis |
| **Agents** | 6 | 19 |
| **Use when** | Before creating a PR | Exploring, planning cleanup, health checks |

## Tips

- **Start with map or health** before deep exploration — orientation saves time
- **Use scope narrowing** after initial exploration to focus on problem areas
- **Run specific agents** for targeted questions rather than always running all
- **Address critical issues first** — the synthesis prioritizes findings
- **Re-run after fixes** to verify improvements, scoped to what you changed
- **Architecture context matters** — even if you only want one agent, the architecture context (via explore) significantly improves analysis quality

## Plugin Structure

```
code-review-toolkit/
├── .claude-plugin/
│   └── plugin.json
├── README.md
├── agents/
│   ├── api-surface-reviewer.md
│   ├── architecture-mapper.md
│   ├── complexity-simplifier.md
│   ├── concurrency-auditor.md
│   ├── consistency-auditor.md
│   ├── dead-code-finder.md
│   ├── documentation-auditor.md
│   ├── git-history-analyzer.md
│   ├── git-history-context.md
│   ├── lint-rule-triager.md
│   ├── pattern-consistency-checker.md
│   ├── project-docs-auditor.md
│   ├── python-pitfall-scanner.md
│   ├── silent-failure-hunter.md
│   ├── tech-debt-inventory.md
│   ├── test-coverage-analyzer.md
│   ├── test-investigation-agent.md
│   ├── type-design-analyzer.md
│   └── typing-integrity-auditor.md
├── commands/
│   ├── audit.md
│   ├── explore.md
│   ├── health.md
│   ├── hotspots.md
│   ├── informed-explore.md
│   ├── known-issues.md
│   ├── map.md
│   └── prior-art.md
├── data/
│   ├── python_bug_shapes.json
│   └── python_non_bugs.md
├── docs/
│   ├── reproduction-convention.md
│   └── searching-trackers.md
└── scripts/
    ├── pytest_plugins/
    │   └── crt_leaktrack.py
    ├── analyze_history.py
    ├── analyze_imports.py
    ├── annotate_findings.py
    ├── build_informed_briefing.py
    ├── check_known_findings.py
    ├── check_typing.py
    ├── collect_debt.py
    ├── correlate_tests.py
    ├── count_types.py
    ├── diff_findings.py
    ├── extract_test_invariants.py
    ├── find_dead_symbols.py
    ├── map_exceptions.py
    ├── map_findings_coverage.py
    ├── measure_complexity.py
    ├── run_external_tools.py
    ├── run_lint_rules.py
    ├── scan_common.py
    ├── scan_concurrency.py
    ├── scan_python_pitfalls.py
    ├── survey_conventions.py
    ├── survey_docstrings.py
    └── survey_error_handling.py
```

## Author

Danzin — adapted from Daisy's pr-review-toolkit
