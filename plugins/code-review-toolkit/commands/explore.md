---
description: "Comprehensive codebase exploration and analysis using specialized agents"
argument-hint: "[scope] [aspects] [options]"
allowed-tools: ["Bash", "Glob", "Grep", "Read", "Task"]
---

# Comprehensive Codebase Exploration

Run a comprehensive analysis of an existing codebase using multiple specialized agents, each focusing on a different aspect of code quality. Two foundational context providers run first — architecture-mapper (structural context) and git-history-context (temporal context) — and their output is fed to all subsequent agents.

**Arguments:** "$ARGUMENTS"

## Argument Parsing

Parse arguments into three categories:

**Scope** (path or glob):
- `.` or omitted → entire project (default)
- `src/module` → specific directory tree
- `src/file.py` → specific file
- `**/*.py` → glob pattern

**Aspects** (which agents to run):
- `architecture` → architecture-mapper only
- `consistency` → consistency-auditor
- `complexity` → complexity-simplifier
- `tests` → test-coverage-analyzer
- `errors` → silent-failure-hunter
- `pitfalls` → python-pitfall-scanner (concrete correctness defects)
- `concurrency` → concurrency-auditor (lock discipline, races, deadlocks, event-loop stalls)
- `lint` → lint-rule-triager (defect-grade linter rules, tiered)
- `docs` → documentation-auditor
- `types` → type-design-analyzer
- `typing` → typing-integrity-auditor (mypy defects + phantom imports)
- `dead-code` → dead-code-finder
- `tech-debt` → tech-debt-inventory
- `test-invariants` → test-investigation-agent
- `patterns` → pattern-consistency-checker
- `api` → api-surface-reviewer
- `project-docs` → project-docs-auditor
- `history-context` → git-history-context
- `history` → git-history-analyzer
- `history-full` → both git-history-context AND git-history-analyzer
- `all` → all agents (default)

**Options**:
- `deep` → full detail, no output truncation
- `summary` → summary tier only (faster)
- `parallel` → run agents concurrently where possible
- `--max-parallel N` → cap concurrent agents per group (default: 2)
- `--runs N` → run each agent N times (default: 1). Runs 2+ are **independent naive passes**: each
  starts cold, with no knowledge of what earlier runs found. Deduplicate findings across runs by
  `(file, line, finding type)`. If run 2 surfaces nothing new, report "high confidence — converged on
  the first pass"; if it surfaces a lot, the analysis is under-saturated and a third run is warranted.
- `--informed-reruns` → with `--runs 3`, the third pass receives a summary of runs 1–2 and this
  instruction: *"These findings are already established. Look at ADJACENT code, unexplored error
  paths, and patterns structurally similar to the confirmed findings that the prior passes missed."*
  Targets fix-propagation rather than re-discovery.

**On multi-pass reviews.** Independent naive passes work because a single pass saturates — one agent
run explores one path through the codebase and stops. A second cold pass takes a different path.
Only after the independent passes should an informed pass run, because a briefing biases toward what
is already known; running it first would cost the independence that makes passes 1 and 2 worth doing.
For a full audit the recommended shape is **2 naive passes + 1 informed pass**, synthesized into a
single combined report:

```
/code-review-toolkit:explore . all --runs 3 --informed-reruns
```

If you want the informed pass seeded from the toolkit's shape catalog rather than only from this
run's own findings, use [`informed-explore`](informed-explore.md) for the third pass instead.

**Tool options** (passed to run_external_tools.py):
- `--skip-tools` → skip external tool analysis entirely
- `--skip-tools TOOL[,TOOL]` → skip specific external tools
- `--tools TOOL[,TOOL]` → run only specific external tools

## Execution Workflow

### Phase 0: Project Discovery

Before launching any agents:
1. Identify the project root (look for pyproject.toml, setup.cfg, setup.py, .git)
2. Count source files and test files in scope
3. Check for CLAUDE.md or equivalent project documentation
4. Determine project language(s) and framework(s)
5. Print a brief project summary to confirm scope

### Phase 0.5: External Tool Analysis (optional)

If external analysis tools are available on the system, run them to gather additional data for subsequent agents:

```bash
python <plugin_root>/scripts/run_external_tools.py [scope]
```

This phase is OPTIONAL — if no external tools are installed, skip it silently. Do not warn the user about missing tools unless they specifically request external tool analysis.

Pass the output to relevant agents in Phases 1-3:
- ruff `dead-code` findings → dead-code-finder
- ruff `bug-risk` + `security` findings → silent-failure-hunter
- ruff `simplification` + `performance` findings → complexity-simplifier
- ruff `deprecated` findings → tech-debt-inventory
- mypy findings → type-design-analyzer
- coverage data → test-coverage-analyzer
- vulture findings → dead-code-finder

If the output includes `configured_but_not_installed` entries, mention them once in the Phase 0 Project Discovery summary:
"Note: This project is configured for [ruff, mypy] but these tools are not installed. Install them for deeper analysis."

If `--skip-tools` is specified with no arguments, skip this phase entirely. If `--skip-tools TOOL` or `--tools TOOL` is specified, pass the corresponding `--skip` or `--tools` flag to `run_external_tools.py`.

### Phase 1: Foundational Context (always runs first)

Launch both foundational context providers with the specified scope:

**Group 0 — Foundational context (always runs first)**:
0a. **architecture-mapper** (structural context) — module boundaries, dependency graph, layering
0b. **git-history-context** (temporal context) — churn metrics, change velocity, co-change clusters

Both are foundational — architecture-mapper provides structural context and git-history-context provides temporal context. Their output is fed to ALL subsequent agents.

If `parallel` is specified, run both concurrently since they are independent.

Store both outputs for injection into Phase 2 agents.

### Phase 2: Targeted Analysis

Based on the requested aspects (default: all), launch the appropriate agents. Each agent receives:
- The specified scope
- The architecture-mapper output as structural context
- The git-history-context output as temporal context
- Any relevant CLAUDE.md content

**Agent dispatch order** (sequential by default):

**Group A — Structural analysis** (benefits most from architecture context):
1. consistency-auditor
2. pattern-consistency-checker

**Group B — Correctness and code quality** (highest-value findings):
3. **python-pitfall-scanner** — concrete correctness defects (runs first in this group: its findings are behavioural bugs, not smells)
4. **concurrency-auditor** — lock discipline, races, deadlocks and event-loop stalls, followed through the call graph
5. **lint-rule-triager** — defect-grade lint rules; merges with the scanner's findings where a rule overlaps a catalogued shape
6. silent-failure-hunter
7. complexity-simplifier
8. dead-code-finder

**Group C — Interface and documentation**:
9. test-coverage-analyzer
10. documentation-auditor
11. project-docs-auditor
12. type-design-analyzer
13. **typing-integrity-auditor** — mypy-provable defects and phantom imports
14. api-surface-reviewer

**Group D — Inventory and investigation**:
15. tech-debt-inventory
16. test-investigation-agent

**Group E — Temporal analysis (runs last)**:
17. git-history-analyzer

If `parallel` is specified, run agents within each group concurrently. Run at most `--max-parallel` agents concurrently within each group (default: 2). On memory-constrained systems, use `--max-parallel 1` to run agents sequentially. Groups still execute sequentially because later groups may benefit from earlier findings. Group E runs last because it cross-references all other agents' output.

### Phase 3: Synthesis

After all agents complete, perform deduplication and conflict resolution, then produce a unified summary.

#### Deduplication and Conflict Resolution

Before writing the summary:

1. **Merge overlapping findings**: When two or more agents flag the same file:line (or overlapping line ranges), merge them into a single finding that credits all contributing agents and combines their perspectives:

   ```
   - [consistency-auditor, pattern-consistency-checker]: Env-parsing logic
     duplicated across 3 modules [src/runner.py:42, src/bench.py:18,
     src/config.py:7]
   ```

   Not separate entries from each agent.

2. **Surface contradictions explicitly**: When agents disagree, don't silently pick one — present the tension:

   ```
   ## Tensions
   - **Exception breadth** at src/runner.py:142:
     silent-failure-hunter recommends narrowing `except Exception` to
     specific types.  complexity-simplifier notes the broad catch
     simplifies control flow.
     → Judgment call: narrower is safer, broader is simpler.
   ```

3. **Attribute to the most specific agent**: When a finding appears in both a general agent and a specialized agent, attribute it to the specialized agent in the summary.

4. **External tool findings**: When merging findings from external tools with agent findings:
   - Findings sourced from external tools should note their source (e.g., "Source: ruff F821")
   - When an external tool and an agent's script flag the same issue, merge into one finding noting both sources
   - Auto-fixable findings (significance: "reduced") should appear in the summary but not dominate the action plan — prioritize findings that require human judgment

#### Summary Template

```markdown
# Codebase Exploration Report

## Project: [name]
## Scope: [what was analyzed]
## Agents Run: [list]

## Executive Summary

[3-5 sentence overview of codebase health across all dimensions analyzed]

## Key Metrics
- Change Velocity: [active/maintenance/low] — [fix:feature ratio]
- Architecture: [healthy/concerning/problematic] — [1-line summary]
- Consistency: [healthy/concerning/problematic] — [1-line summary]
- Complexity: [N hotspots found, N critical]
- Test Coverage: [N modules untested, N critical gaps]
- Error Handling: [N silent failures, N critical]
- Documentation: [X% coverage, N accuracy issues]
- Project Docs: [N broken references, N contradictions]
- Type System: [X% annotated, N design issues]
- Dead Code: [N items identified]
- Tech Debt: [N markers, age distribution summary]
- API Surface: [learnability score X/10]
- Fix Quality: [N fixes reviewed, N incomplete, N propagation gaps]

## Findings by Priority

### Must Fix (FIX)
[Unambiguously wrong — bugs, crash risks, clear violations]
- [agent(s)]: Issue description [file:line]

### Should Consider (CONSIDER)
[Judgment calls — improvement likely but has trade-offs]
- [agent(s)]: Issue description [file:line]

### Tensions
[Where agents disagree — present both sides and the trade-off]

### Policy Decisions (POLICY)
[Require team/project-level decisions, not local fixes]
- [agent(s)]: Issue description

### Acceptable / No Action (ACCEPTABLE)
[Count only: N items classified as acceptable across all agents]

## Strengths

[What the codebase does well — aggregated positive findings]

## Recommended Action Plan

### Immediate (this week)
[Prioritize items from the git-history-analyzer risk matrix — highest risk items first]
1. [FIX item]
2. [FIX item]

### Short-term (this month)
1. [CONSIDER item]
2. [CONSIDER item]

### Ongoing
1. [POLICY decision to make]
2. [Convention to establish]
```

## Usage Examples

**Full exploration (default):**
```
/code-review-toolkit:explore
```

**Specific scope:**
```
/code-review-toolkit:explore src/benchmarks
```

**Specific aspects:**
```
/code-review-toolkit:explore . complexity errors
# Only complexity and error handling analysis

/code-review-toolkit:explore . tests docs
# Only test coverage and documentation
```

**Quick summary:**
```
/code-review-toolkit:explore . all summary
# All agents, summary output only
```

**Deep dive on a module:**
```
/code-review-toolkit:explore src/runner.py all deep
# All agents, full detail, single file
```

## How Foundational Context Flows

When the explore command passes foundational context to other agents, frame it as:

```
[Include architecture-mapper output]

The above is the architecture analysis of this project. Use it to:
- Understand module boundaries and responsibilities
- Prioritize findings in high-traffic/high-fan-in modules
- Identify when an issue is localized vs. cross-cutting
- Calibrate severity based on where code sits in the dependency graph

[Include git-history-context output]

The above is the git history analysis of this project. Use it to:
- Prioritize findings in high-churn code (recently volatile code is more likely to have bugs and more likely to be modified again)
- Note when code you're flagging was recently changed (it may have been changed intentionally)
- Pay extra attention to modules flagged as "volatile"
- Consider co-change patterns when assessing coupling

The project-docs-auditor also benefits from consistency-auditor output when checking whether CLAUDE.md conventions match actual code patterns. If running both, dispatch consistency-auditor (Group A) before project-docs-auditor (Group C) — this is already the case in the dispatch order.
```

## Tips

- **Start broad, then narrow**: Run with `summary` first, then drill into specific aspects
- **Architecture first**: Even if you only want one aspect, the architecture context significantly improves the analysis
- **Prioritize by criticality**: Focus on critical issues from the synthesis before medium/low
- **Re-run after fixes**: Use specific aspects to verify that addressed issues are resolved
- **Scope to what changed**: After fixing issues, run specific agents on the modified scope
