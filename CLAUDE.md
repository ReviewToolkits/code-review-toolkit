# CLAUDE.md — code-review-toolkit development guide

## Project overview
code-review-toolkit is a [Claude Code](https://docs.anthropic.com/en/docs/claude-code) plugin for
reviewing **Python source code**. It answers: *where are the problems in this codebase, and what
should I fix first?*

The original member of a family of review toolkits:

| Toolkit | Target | Parsing |
|---|---|---|
| **code-review-toolkit** | Python source (this project) | `ast` |
| [cext-review-toolkit](https://github.com/devdanzin/cext-review-toolkit) | CPython C extensions | Tree-sitter |
| [cpython-review-toolkit](https://github.com/devdanzin/cpython-review-toolkit) | CPython runtime C | Tree-sitter |
| [ft-review-toolkit](https://github.com/devdanzin/ft-review-toolkit) | Free-threading safety in C ext | Tree-sitter |
| [rust-ext-review-toolkit](https://github.com/devdanzin/rust-ext-review-toolkit) | PyO3 extensions | Tree-sitter (Rust) |
| [pyo3-review-toolkit](https://github.com/devdanzin/pyo3-review-toolkit) | PyO3 itself | Tree-sitter (Rust) |
| [rustpy-review-toolkit](https://github.com/devdanzin/rustpy-review-toolkit) | RustPython interpreter | Tree-sitter (Rust) |

Key difference from the siblings: they target *compiled-language* code where the dominant defects are
crashes (segfaults, refcount errors, data races, panics). This toolkit targets Python, where the
dominant defects are **silent** — wrong results, swallowed errors, shared mutable state — and where
the analysis surface is `ast` rather than Tree-sitter.

## Prerequisites
- Python 3.10+ (uses `X | Y` union syntax)
- **No required dependencies** — all scripts use only the standard library
- Optional, for richer analysis: `ruff`, `mypy`, `vulture`, `coverage` (integrated via
  `run_external_tools.py`; every agent works fully without them)

## Dev commands
```bash
# Run all tests (must be run via discover — tests import helpers.py from tests/)
python -m unittest discover tests -v

# Run one test module
python -m unittest discover tests -p "test_scan_common.py" -v

# Run a script standalone (all output JSON to stdout)
python plugins/code-review-toolkit/scripts/measure_complexity.py /path/to/project
python plugins/code-review-toolkit/scripts/build_informed_briefing.py /path/to/project

# Lint and format (scope to the files you changed)
ruff format <changed-files>
ruff check <changed-files>
```

> The repository has no ruff config and is **not** uniformly `ruff format`-clean — running
> `ruff format` across the whole tree rewrites ~1500 lines of untouched code. Format only what you
> changed, so a review diff stays reviewable. Normalizing the whole tree is worth doing, but as its
> own formatting-only commit.

> Tests **must** run through `unittest discover`; invoking `python -m unittest tests.test_x` fails
> with `ModuleNotFoundError: No module named 'helpers'` because discovery is what puts `tests/` on
> `sys.path`.

## Project structure

This is a Claude Code plugin, not a pip-installable package.

```
code-review-toolkit/
├── CLAUDE.md                        # This file
├── README.md                        # User-facing docs
├── CHANGELOG.md                     # Keep a Changelog format
├── plugins/code-review-toolkit/
│   ├── .claude-plugin/plugin.json
│   ├── agents/                      # 19 agent prompt definitions (markdown)
│   ├── commands/                    # 8 command definitions (markdown)
│   ├── scripts/                     # 17 Python scripts (the core code)
│   └── data/                        # bug-shape catalog + FP taxonomy
└── tests/                           # unittest suite
```

## Architecture

### Scripts

All in `plugins/code-review-toolkit/scripts/`. Every analysis script parses Python with `ast`, finds
candidates, and prints JSON to stdout.

| Script | Purpose |
|---|---|
| `scan_common.py` | **Shared utilities — every other script imports from here** |
| `scan_python_pitfalls.py` | Correctness defects; 39 checks mapping 1:1 to `data/python_bug_shapes.json` (2 of them project-level) |
| `scan_concurrency.py` | Concurrency defects; 12 project-level checks (lock-guard inference, lock discipline, blocking under locks and in `async def`, through the call graph) |
| `analyze_imports.py` | Import graph, module boundaries, circular dependencies |
| `analyze_history.py` | Git history: churn, co-change, fix density |
| `measure_complexity.py` | Per-function complexity metrics |
| `find_dead_symbols.py` | Unused imports, unreferenced symbols, orphan files |
| `correlate_tests.py` | Source ↔ test file correlation |
| `count_types.py` | Type annotation coverage and type design patterns |
| `collect_debt.py` | TODO/FIXME/HACK inventory with git-blame aging |
| `extract_test_invariants.py` | Extracts assertions as invariant specs |
| `run_external_tools.py` | ruff / mypy / vulture / coverage integration |
| `run_lint_rules.py` | Pinned, tiered ruff selection: defect-grade rules only, merged with the shape catalog |
| `check_typing.py` | mypy on the project's own config, plus a `--disallow-any-unimported` phantom-import pass |
| `check_known_findings.py` | Cross-references a catalog of known findings against a fresh scan (`known-issues`) |
| `diff_findings.py` | Diffs two report directories — the false-positive regression gate |
| `build_informed_briefing.py` | Assembles the informed-explore briefing from `data/` |

**Dependency graph:** `scan_common.py` is at the center; every other script imports from it. No
circular dependencies.

**Script calling convention:** each analysis script exposes `analyze(target: str, *, max_files: int
= 0) -> dict` and a `main()` that prints JSON. Exceptions: `analyze_history.py` takes `argv`;
`build_informed_briefing.py` prints **Markdown**, not JSON, because its output *is* the agent briefing.

### The shared JSON envelope

Every analysis script's output starts with the envelope from `scan_common.build_envelope()`:

```json
{"project_root": "...", "scan_root": "...", "files_total": 120,
 "files_analyzed": 120, "files_capped": false}
```

`files_capped` tells an agent the results are partial because `--max-files` truncated the scan — say
so in the report rather than presenting partial results as complete.

### Data files (`plugins/code-review-toolkit/data/`)

This is the toolkit's **memory**, and the thing that makes `informed-explore` work:

- `python_bug_shapes.json` — reusable bug *shapes* (not file:line). Each carries its `pattern`, its
  `guarded_twin` (the fix, usually already present elsewhere in the same codebase), a `hunt`
  directive for finding siblings, `expected` behavior, how it `caught_as` surfaces, a `differential`
  (when NOT to flag), and a `validation` grade.
- `python_non_bugs.md` — the false-positive taxonomy: what to dismiss, *why*, and what the real bug
  looks like so a genuine instance is never suppressed.

**`validation` grades:** `documented` = the semantics are specified in the Python docs/FAQ or enforced
by a mainstream linter rule (real, but not yet confirmed by this toolkit); `confirmed` = a validation
run found a true positive — cite it in `confirmed_examples`. **Promoting `documented` → `confirmed`
is the calibration loop.**

### Agents

19 markdown files with YAML frontmatter (name, description) plus a structured prompt. Agents contain
no analysis logic — they run a script, read the JSON, then do the qualitative review the script
cannot. Scripts find candidates (with a real false-positive rate); agents confirm or dismiss them.

Seven agents are **qualitative** (no backing script — they use Grep/Read directly):
api-surface-reviewer, consistency-auditor, documentation-auditor, git-history-analyzer,
pattern-consistency-checker, project-docs-auditor, silent-failure-hunter. The other twelve are
script-backed, and each ends with a `## Running the script` section (timeout, private `mktemp -d`
output, no retry after a failure); `test_release_discipline` fails if a new one omits it.

### Commands

- `explore` — full review; supports `--runs N` and `--informed-reruns` for multi-pass
- `informed-explore` — same coverage, but every agent is seeded with the `data/` briefing first
- `map` — architecture only, fast
- `health` — scored dashboard, all agents in summary mode
- `hotspots` — complexity + dead code + debt, ranked
- `audit` — release-gating: every scanner, a machine-readable sign-off, BLOCK on an incomplete run
- `known-issues` — cross-references a catalog of earlier findings against a fresh scan
- `prior-art` — searches the project's tracker before a finding is called novel

### Classification system

Every finding is tagged:
- **FIX** — a real defect: wrong behavior, silent data loss, a crash
- **CONSIDER** — likely improvement, may carry migration cost
- **POLICY** — a design decision for the maintainer
- **ACCEPTABLE** — noted, no action needed

## Testing notes
- All tests use `unittest` — **never pytest**
- `tests/helpers.py` provides `TempProject` (context manager writing a temp project, including a
  `pyproject.toml` so `find_project_root` works) and `import_script()` (importlib loader)
- New script → add at minimum: a true positive, a true negative, and an edge case

## Adding a new analysis script

1. Create `plugins/code-review-toolkit/scripts/scan_newcheck.py`
2. `sys.path.insert(0, str(Path(__file__).resolve().parent))`, then import from `scan_common` —
   **never re-implement `find_project_root` or `discover_python_files`**
3. Implement `analyze(target, *, max_files=0) -> dict` using `build_envelope()`
4. Add `main()` using `parse_common_args()`
5. Add `tests/test_scan_newcheck.py`
6. Add `plugins/code-review-toolkit/agents/<name>.md`
7. Register the agent in the right phase group in `commands/explore.md`
8. Update `CHANGELOG.md` **and the agent/script/command counts in both READMEs**

## Gotchas

- **`scan_common.py` exists because of real decay.** Before it, `find_project_root` was
  byte-identical in all nine scripts and `discover_python_files` had already drifted into three
  divergent variants. Import; do not copy.
- **Tests need `discover`.** See the note under *Dev commands*.
- **`discover_python_files` is a generator.** Callers that slice or `len()` it must wrap in `list()`
  (or use `collect_python_files`, which returns `(files, files_total)`).
- **`parse_source()` returns `None` on bad input, and never raises.** Scans run over arbitrary
  third-party trees; one unparseable file must never abort the run.
- **`analyze_history.py` has a different `analyze()` signature** — it takes `argv`, matching the
  sibling toolkits' convention but differing from every other script here.
- **`build_informed_briefing.py` prints Markdown, not JSON.** It is the one deliberate exception to
  the output contract.
- **Keep the README counts honest.** Both READMEs state agent/script/command counts; they have drifted
  before. Update them in the same commit that adds a component.

## Known gaps (as of this writing)

Tracked honestly so the next session does not have to re-derive them:

1. **Five validation corpora** (`reports/`): idlelib (v1–v4), `_pyrepl`, coverage.py (v1–v3),
   asyncio and tkinter. They are deliberately different — idlelib is mature and careful, `_pyrepl`
   is young and takes chances, coverage.py is a large third-party project. A threaded/async
   application has since been reviewed privately for its maintainer; its report stays outside this
   repo, so the threading shapes it motivated are `documented` until its findings can be cited.
2. **82 of 117 shapes are `confirmed`; 35 remain `documented`**, including all 13 threading
   shapes.
3. **51 of 117 shapes are executable.** `scan_python_pitfalls.py` implements 39 (37 per-file, 2
   project-level) and `scan_concurrency.py` 12 (all project-level). The rest are `implementable` or
   `agent-only` — `detectability` in the catalog says which.
4. **The companion findings repository is `devdanzin/code-review-findings`** (`coveragepy/`,
   `cpython-idlelib/`, `cpython-pyrepl/`). The `.code-review/findings.json` schema written by
   `informed-explore` is wire-compatible with its `project-local/findings.json`, so a project's
   accumulated memory lifts over unchanged.
5. ~~`known-issues` command not implemented~~ **Done** — `check_known_findings.py`.

## Workflow
- Use `/task-workflow <description>` for the issue → branch → code → test → commit → PR → merge cycle
