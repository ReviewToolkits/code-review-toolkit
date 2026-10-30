# Code Review Toolkit

A [Claude Code](https://docs.anthropic.com/en/docs/claude-code) plugin that bundles 19 specialized agents and 8 commands for exploring and analyzing existing codebases. It answers the question: **where are the problems in this codebase, and what should I fix first?**

## Installation

### From the marketplace (recommended)

Add this repository as a Claude Code marketplace, then install the plugin:

```bash
# Add the marketplace (one-time setup)
claude plugin marketplace add ReviewToolkits/code-review-toolkit

# Install the plugin
claude plugin install code-review-toolkit@code-review-toolkit
```

Or use the interactive plugin manager:

```bash
# Open the plugin manager
/plugin

# Go to the Discover tab, find code-review-toolkit, and install
```

### Direct install from GitHub

Install the plugin directly without adding the marketplace:

```bash
claude plugin install code-review-toolkit --source github:ReviewToolkits/code-review-toolkit --path plugins/code-review-toolkit
```

### Without installing (try it first)

Clone the repo and launch Claude Code with `--plugin-dir` — the plugin is loaded for that session only, nothing is installed:

```bash
# Clone the repository
git clone https://github.com/ReviewToolkits/code-review-toolkit.git

# Run Claude Code with the plugin loaded for this session
claude --plugin-dir code-review-toolkit/plugins/code-review-toolkit
```

## Quick Start

After installation, these commands are immediately available in Claude Code:

```bash
/code-review-toolkit:map        # Understand project structure
/code-review-toolkit:health     # Quick health assessment
/code-review-toolkit:hotspots   # Find cleanup targets
/code-review-toolkit:explore    # Full exploration (all agents)
```

For your first time, start with `map` to understand the architecture, then `health` for a quick overview, then drill into specific areas with `explore`.

## What's Included

- **19 analysis agents** covering concrete correctness defects (mutable defaults, late-binding closures, unreachable `except` ordering, asyncio pitfalls), concurrency defects (lock-guard inference, lock-order cycles, blocking work under locks and on the event loop), defect-grade lint triage, type-checker-provable defects, architecture, git history context, consistency, complexity, test coverage, error handling, documentation, project documentation accuracy, type design, dead code, tech debt, pattern consistency, API surface review, test-invariant investigation, and git history analysis (fix propagation, churn×quality risk).
- **8 commands** (`explore`, `informed-explore`, `map`, `hotspots`, `health`, `audit`, `known-issues`, `prior-art`) for exploration, release gating, regression tracking, and tracker searches.
- **17 helper scripts** for correctness scanning, concurrency scanning, tiered lint triage, mypy integrity checks, complexity measurement, import analysis, dead symbol detection, test correlation, type counting, debt collection, test-invariant extraction, git history analysis, external-tool integration, known-findings cross-referencing, report diffing, shared utilities, and informed-briefing assembly.
- **A bug-shape catalog and false-positive taxonomy** (`data/`) that power `informed-explore`, so a re-review hunts un-found siblings of known defect shapes instead of re-deriving the basics.

For detailed usage, agent descriptions, and recommended workflows, see the [plugin README](plugins/code-review-toolkit/README.md).

## License

MIT — see [LICENSE](LICENSE) for details.

## Credits

Originally created by Daisy (Anthropic). Adapted by Daniel Diniz.
