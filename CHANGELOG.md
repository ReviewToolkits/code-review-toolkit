# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Fixed — ruff and external-tool runs (#55)

- `run_lint_rules.py` and `run_external_tools.py` no longer leave `.ruff_cache/` or `.mypy_cache/`
  in the reviewed project (`--no-cache`; mypy gets a private `--cache-dir`, removed afterwards).
- ruff's "Failed to lint" (a sandboxed ruff that can't read the path exits 0 with no findings) is
  an `error` with the `unlinted` paths, not a clean result.
- `RULE_TO_SHAPE` maps B905 → `zip-truncates-on-length-mismatch` and DTZ005/DTZ006 →
  `naive-datetime-in-a-persisted-artifact`.
- B008 on FastAPI dependency markers (`Depends`, `Query`, `Body`, ...) is dismissed with a recorded
  reason instead of flooding tier 1 (95 → 0 on a FastAPI service, all counted in `dismissed`).
- F821/F841 on an `except ... as exc` name read by a closure are labelled a known ruff false
  positive (ruff#6878), and that F841's autofix is flagged `autofix_unsafe`.
- `findings_capped` is reported honestly for mypy and vulture.

### Enhanced

- **`run_external_tools.py` runs a labelled toolkit ruff pass** when the project's own selection is
  narrow, so the bug-risk, security, simplification, performance and deprecated (UP) feeds are no
  longer empty on a project that selects only E/F/W/I. Every finding carries `selection`.
- **vulture runs at confidence 60**, with confidence buckets, so unused methods are reported.

### Enhanced — agent prompts, from a full review of a threaded application

- **silent-failure-hunter**: correct ruff codes (`S110`/`SIM105` are try-except-pass, `B025` is a
  duplicate handler, `S101` is `assert` and not a FIX), a note that the rules it can see depend on
  the selection that ran, a new step for handlers and `finally` blocks that *raise* (cleanup that
  triggers a rollback, side channels in `finally`, `CancelledError` bypassing `except Exception`),
  plain-text severity labels, and `Status` / `Failure scenario` / `Guarded twin` fields.
- **dead-code-finder** and **silent-failure-hunter** never recommend the F841 autofix on an
  `except ... as NAME` used by a closure.
- **architecture-mapper** tests each cycle member's import in a fresh interpreter instead of
  guessing whether a cycle crashes.
- **pattern-consistency-checker** checks whether a fix reached every sibling implementation, and
  diffs producer/consumer maps; **consistency-auditor** compares close siblings' *behavior*
  (offloading, locking, validation, cleanup).
- **documentation-auditor** treats concurrency and availability claims in docstrings as testable.
- **api-surface-reviewer** runs one operation through every transport and diffs the results, audits
  the REST/OpenAPI surface, and has a `breaks-generated-clients` change class.
- **project-docs-auditor** audits Dockerfiles, compose files, `*.example`, Makefiles and CI
  configuration, and no longer assumes `pyproject.toml` holds the metadata.
- **tech-debt-inventory** covers dependency debt (no upper bound plus reliance on internals,
  runtime deprecations, tracker keys).
- **test-coverage-analyzer** no longer assumes `unittest` (a leftover from an earlier target) and
  recommends measured coverage of other agents' FIX paths; **type-design-analyzer** reports
  annotation accuracy as "not measured" without mypy data; **git-history-analyzer** annotates every
  FIX in explore runs and checks unmerged branches.
- **explore** requires a `status` and a failure scenario on every finding, from every agent.

### Enhanced

- **`explore` runs into a run directory, and its synthesis consolidates, re-anchors and
  reproduces** (feedback from a full review of a threaded application). Phase 0 creates
  `<run_dir>/` outside the reviewed checkout (agent reports, kept artifacts, `findings.json`) and
  hands every agent the same rules: the checkout is read-only and left without tool caches, scratch
  work goes in a private `mktemp -d` with `TMPDIR` pointing there, and reproducers worth keeping go
  in `artifacts/<agent>/`. Phase 3 gains three steps: a consolidation agent writes a deduplicated
  `findings.json` with a fixed schema (in-context synthesis of a full run doesn't fit), every FIX
  finding is re-anchored against the project's unmerged branches, and findings are promoted to
  `reproduced` or `refuted`. The summary template gains a Concurrency line.
- **`informed-explore` no longer writes `.code-review/findings.json` into the reviewed checkout** by
  default: the memory goes into the run directory and comes back with `--catalog`.
- **`prior-art` covers trackers that aren't GitHub**: GitLab (`glab`, or the anonymous REST API),
  local tracker keys in commits and roadmap files, and unmerged branches, with two new verdicts,
  `fixed-unmerged` and `tracked-locally`.
- **`docs/reproduction-convention.md` covers races and stalls**: forced interleavings with
  `Event`s and `Barrier`s, timing a trivial coroutine to prove an event-loop stall, `faulthandler`
  for deadlocks, being honest about injected cancellation, and a shared harness whose reproducers
  each end in one `BUG:`/`OK:` line, runnable against any git ref.

### Added

- **Three shapes**: `cleanup-inside-try-triggers-rollback` and `side-channel-raise-in-finally`
  (silent-failure-hunter), and `linter-autofix-breaks-closure-capture` (lint-rule-triager: ruff's
  F841 "safe" fix deletes an `except ... as exc` binding that a lambda still uses).

### Added

- **`concurrency-auditor` agent and `scan_concurrency.py`: the catalog's first threading shapes** —
  ports three ideas from ft-review-toolkit's C-extension scanners to Python's `ast`, as 12
  project-level checks, each a new shape (#52):
  - *Lock-guard inference.* For each attribute and module global written after construction, the
    lock held at most of its sites is its guard; writes, mutations and iterations without it are
    `unguarded-access-to-lock-guarded-state`. A private helper called only with the lock held
    inherits it (an entry lockset over its callers), so `_foo_locked` helpers are not flagged. The
    same inference runs on calls: a private helper that three-quarters of its callers invoke under
    one lock, called bare by another. `check-then-act-without-lock` covers lazy init and
    check-then-insert outside the lock.
  - *Lock discipline.* `acquire-without-finally-release`, `lock-acquire-result-ignored`,
    `lock-order-inversion` (cycles in a graph built from nested `with` blocks and from calls made
    while holding a lock), `non-reentrant-lock-reacquired` (including a read-to-write upgrade on a
    project's own reader/writer lock; owner-tracking locks are recognized as reentrant), and the
    `Condition` pair `condition-wait-outside-predicate-loop` / `condition-notify-without-lock`.
  - *Forbidden work in critical regions, through the call graph.* `blocking-call-under-lock`,
    `await-while-holding-thread-lock`, `transitive-blocking-call-in-async` and
    `signal-handler-does-real-work`. Locks held somewhere across blocking work are *slow locks*;
    an `async def` that reaches one through sync helpers is reported with a `chain` that includes
    the other thread's path (`meanwhile, another thread holds X here`), and at high confidence when
    a sibling coroutine in the same module already offloads its sync call (the guarded twin).
  - Locks are recognized from `threading`/`multiprocessing`/`asyncio` factories, dataclass
    `field(default_factory=Lock)` fields, lock-like names, and the project's own context managers
    that hold a lock at `yield`. Calls resolve through `self.`, module functions, import aliases,
    constructor-typed attributes and annotations; a method name defined by at most three project
    classes resolves to all of them as *weak* edges, which lower confidence by one level.
  - Validated on a private threaded/async FastAPI application (~930 functions, ~1 s): it finds,
    unprompted, the event-loop stalls and the lock-skipping helper call that the earlier review of
    that project had to find by hand (and that the direct `blocking-call-in-async-function` check
    missed).
- **13 shapes in `python_bug_shapes.json`** — the 12 above plus the agent-only
  `request-reply-channel-desync-after-timeout` (a pipe reused after a timeout returns the previous
  caller's reply). All 13 are `documented` for now.

### Fixed

- **`tests/helpers.import_script` registers the module while it executes**, so a script using
  `@dataclass` with `from __future__ import annotations` can be imported by the tests.

## [1.14.0] - 2026-10-29

### Documentation

- **Component counts and inventories brought back in line with the tree** — 18 agents, 8 commands,
  16 scripts, which `test_release_discipline` already enforced while the prose drifted. `CLAUDE.md`
  gains the four unlisted scripts (`check_typing`, `run_lint_rules`, `check_known_findings`,
  `diff_findings`), the three unlisted commands, and a rewritten *Known gaps* (five corpora, 82/101
  shapes confirmed, 39/101 executable, `known-issues` done). The plugin README gains the `lint` and
  `typing` aspects, sections for `audit`/`known-issues`/`prior-art`, rows for `lint-rule-triager`,
  `typing-integrity-auditor` and `test-investigation-agent`, and a structure tree generated from the
  directory. `marketplace.json` moves from 1.3.0 and "14 agents and 4 commands" to the plugin's
  version. Install instructions point at `ReviewToolkits/code-review-toolkit` (#50).

### Added — four shapes from the coverage.py v3 run, every instance verified before it was written

- **`env-flag-parsed-as-strict-int`** [FIX] — a debug toggle read as `bool(int(os.getenv(...)))`. The
  `bool()` says boolean; the parser accepts only decimal digits, so `true`, `yes` and **the empty
  string `export FLAG=` produces** all raise `ValueError` at import, before any of the project's own
  error handling exists. The differential separates it from `unvalidated-numeric-from-environment`:
  the `bool()` wrapper tells you the author meant a flag and reached for the wrong parser.
- **`naive-datetime-in-a-persisted-artifact`** [CONSIDER] — the hunt directive is what makes it
  usable: **do not grep `datetime.now()` and report the count.** Follow the value to its
  *destination* and keep only what reaches an artifact another program reads. A naive timestamp in a
  log line or a filename is fine.
- **`diagnostic-channel-controlled-by-the-subject-under-observation`** [FIX] — an instrumentation
  tool reporting its own failures through `warnings`/`logging`, which the observed program owns and
  reconfigures. Needs the observer/observed split: a library warning its *caller* is correct design.
- **`atexit-registration-never-unregistered`** [CONSIDER] — a bound method registered with `atexit`
  and never removed, pinning the object for the process. The guarded twin is usually in the same
  function: a signal handler that IS saved and restored.

`assertion-against-a-stub-that-cannot-fail` gains a **second confirmed example on a second
codebase**, found by mutation rather than by reading — coverage.py's `files.py:35`, whose only test
patches `os.path.normcase` into a tautology. The mutant survives on both tracer cores; the inverse
mutant kills 92 tests, proving non-equivalence. Catalog **97 → 101 shapes**.

### Enhanced

- **Every script-backed agent now says how to run its script** — a `## Running the script` section
  ported from ft-review-toolkit. It sets a 300000 ms Bash timeout (`run_in_background` beyond that)
  and gives each run a private `mktemp -d` directory with stdout and stderr in separate files, so
  agents in `parallel` mode cannot overwrite each other. It also requires checking the exit status
  and the JSON `status`/`error` fields before reading output, and forbids retrying a failed script:
  fall back to Grep/Read and say the run did not complete, never "no findings". It deliberately
  departs from ft's `$$` file suffix: each Bash tool call is a fresh shell, so `$$` changes between
  the call that writes the file and the call that reads it. `informed-explore`'s briefing moves
  off its fixed `/tmp/informed_briefing.md` the same way. A release-discipline test now fails if a
  script-backed agent lacks the section (#48).

### Fixed

- **`blocking-call-in-async-function` flagged blocking calls inside nested sync `def`s and
  `lambda`s** — the very closure an `async def` hands to `run_in_executor`, i.e. the fix the finding
  recommends. The check meant to skip nested defs, but `continue` inside `ast.walk` only skips the
  `def` node; its body was already queued. It now walks with `_walk_same_scope`, which also stops a
  blocking call in a nested `async def` being reported twice (once per enclosing coroutine). Found
  while preparing a pavedb review, whose routes offload work through exactly this shape (#46).
- **`check_typing.py` reported `0 type errors` whenever `FORCE_COLOR` is set in the
  environment.** mypy honours `FORCE_COLOR` even when its stdout is a pipe, and the ANSI codes land
  between the `file:line:` prefix and `error:`, so `_LINE` stopped matching and **every finding was
  dropped** — while the summary line still parsed, because its regex only needs the digits. Found by
  running the toolkit against coverage.py: mypy reported 3 `no-any-unimported` errors and the script
  reported none, silently losing the one defect this script exists to find (a `TYPE_CHECKING` import
  of a module that does not exist — `coverage/html.py:42` imports `coverage.plugins`, which is
  spelled `coverage.plugin`). Fixed three ways: `--no-color-output`, a `FORCE_COLOR=0`/`NO_COLOR=1`
  subprocess environment, and a defensive ANSI strip in `parse_output`.
- **`check_typing.py` now cross-checks its own parse against mypy's error count** and reports
  `FAILED` with a `parse_mismatch` reason when they disagree. This is the durable half of the fix:
  the script *had* the right number in `stats["errors"]` the whole time and reported the smaller one.
  Any future output-format change now fails loudly instead of silently returning zero.

### Added

- **`per-redraw-binding-never-released`** (shape 97) — a binding, callback, or native handle created
  once per element per redraw with no matching release, so registrations grow with redraws rather
  than with elements. Written from the last un-shaped finding of the idlelib migration
  (CRF-IDLELIB-0057, `tree.py:234`, under an XXX that has been correct since 1999). The
  differential is what makes it usable: Tk deletes a widget's Tcl commands in `Misc.destroy`, so the
  common case is a non-issue — the real instance is a **container that outlives its elements**, and
  the measurement, not the stale XXX, is the proof.

### Fixed

- `test_agent_filter_scopes_shapes` asserted `"other" not in briefing` while triage rule 7's own
  prose says "every **other** agent", so it failed on wording rather than on the filter it exists to
  check. The excluded fixture shape is now `unrelated-shape`. This had been failing at HEAD since
  rule 7 landed.

## [1.13.0] - 2026-07-27

The write-back from the idlelib run. Catalog **90 → 96 shapes**, FP taxonomy **37 → 47 classes**.
Findings in `reports/idlelib_v4/FINDINGS.md`; reasoning in `docs/decision-log.md` D-19.

### Added — six shapes, every one confirmed against a verified idlelib instance

- **`zip-truncates-on-length-mismatch`** — the standing top candidate, now confirmed. The guarded
  twin is often in the project's own tests for the very function that lacks it.
- **`guard-conjoined-with-a-preference-flag`** — a validity guard ANDed with a notification flag, so
  turning the preference off removes the guard. Unreachable in every default configuration and every
  test, which is why it survives.
- **`per-line-property-derived-from-the-aggregate`** — a per-element property computed once from the
  joined whole. Verified: Format Paragraph turns `result` into `esult` and writes it back.
- **`viewport-scoped-index-consumed-globally`** — a render cache doubling as a data model; correct
  whenever the model fits on one screen.
- **`structured-index-tested-by-string-suffix`** — `sellast[-1] != '0'` standing in for
  `column != 0`. Fails for one value in ten, which reads as an intermittent glitch.
- **`assertion-against-a-stub-that-cannot-fail`** — the high-value variant of `test-cannot-fail`.
  The assertion is real; the FIXTURE cannot fail. This is what hid CRF-IDLELIB-0025, and mutation
  testing is the hunt directive: a surviving mutant is proof, not a guess.

### Added — ten false-positive classes (38-47)

Class **39** is the valuable one: a commented-out block that is prose rather than code. The
discriminator kills the whole class mechanically — **strip the `#` and `ast.parse()`; prose raises
`SyntaxError`**. Class **44** runs the other way: `ChangeLog` / `HISTORY*` / `NEWS*` mentions are
records of an old change, not references, and treating them as references *suppresses* real
dead-code findings.

Class **38** accounts for **163 of idlelib's 164** unreferenced symbols by itself: `unittest`
discovery selects by base class, never by name.

## [1.12.0] - 2026-07-27

Fixes found by running a full 18-agent `informed-explore` against CPython's idlelib. Findings in
`reports/idlelib_v4/FINDINGS.md`; reasoning in `docs/decision-log.md` D-19.

### Fixed — four scanners reported a failure as a result

- **`analyze_imports` reported a resolution failure as `cycles: []`.** When the reviewed tree IS a
  stdlib package (`Lib/idlelib`, `Lib/asyncio`), `_is_stdlib()` was consulted before
  `project_packages`, so every internal import was classified `stdlib` and dropped — and the empty
  graph was reported as a clean result. Three fixes: the scanned package is detected as a package in
  its own right; **project packages now win over the stdlib list**; and a `resolution` field reports
  `FAILED` / `PARTIAL` / `ok`. idlelib **0 → 113 edges**; **asyncio 0 → 19 cycles**, so the 1.10.0
  asyncio run had the same silent failure; coverage.py unchanged.
- **Four lists were capped and presented as counts.** `count_types.unannotated_public_functions`
  read 50 where the true figure for idlelib is **2079**; `extract_test_invariants` capped at 15 and
  20 (the "20 untested similar functions" is really **20 of 53**). All now emit `*_total` and
  `*_capped` beside the list.

### Changed — reproduction discipline moved to where agents will read it

- **`git archive` is now triage rule 7 in the informed briefing**, not only in
  `docs/reproduction-convention.md`. Two agents patch-tested the live checkout during this run
  despite that doc existing — agent prompts do not read it. The briefing they all do read.

## [1.11.0] - 2026-07-27

Phase 6 — the regression gates. Both gates caught a defect in the gate itself.

### Measured — the FP-regression gate passes

- **idlelib v2 → v3: `101 → 101, added 0, gone 0, unchanged 101`.** A clean pass across everything
  Phases 0–5 shipped: one new shape, three scanner bug fixes, and a retuned lint pass. This is what
  the gate was built for and it is the first time it has been run in anger.
- Complexity hotspots `12 → 9` on idlelib and `18 → 14` on coverage.py — the documented `elif`/`if
  False:` nesting recalibration, not a finding change.
- **coverage.py v1 → v2 at the same commit (`d37859cd`), so every difference is the toolkit.**
  `scan_python_pitfalls` `26 → 26` with zero movement. `find_dead_symbols` `42/9/2 → 0/0/0` and
  `analyze_imports.cycles` `20 → 9`, both the previously-recorded fixes now visible as a diff.

### Fixed — in `diff_findings.py`, both found by running it

- **`message` was part of the finding key.** Improving a check's wording re-split every finding it
  produces into one `gone` plus one `added` — the same spurious pair the line-number exclusion
  exists to prevent, arriving by another route. Caught on idlelib v2 → v3, where a message dedup fix
  reported a regression *and* a fix at the same file, line and shape.
- **Two runs over different trees were silently diffed.** coverage.py v1's
  `extract_test_invariants` had been run against `coveragepy/tests` and v2 against
  `coveragepy/coverage`; the diff reported **30 findings "gone"** from a tree the second run never
  looked at. Mismatched `scan_root`s are now refused with a note rather than compared.
- `run_lint_rules`, `check_typing` and `check_known_findings` are now registered finding sources, so
  the Phase 1 and 3 scanners participate in the gate instead of being silently skipped.

## [1.10.0] - 2026-07-27

Phase 5 of `docs/improvement-plan.md` — the yield runs. Full reports in
`reports/tkinter_v1/` and `reports/asyncio_v1/`.

### Fixed — a toolkit defect each corpus exposed

- **`--isolated` was suppressing the entire `ASYNC` rule family.** It is on by default so a project's
  ruff config cannot silently change the rule selection — but it also discards `requires-python`, and
  ruff then assumes its oldest supported version. asyncio's first run reported **zero** ASYNC
  findings; with `--target-version` derived from the project (or the running interpreter when
  undeclared), **`ASYNC109` fires 4 times** and four `F821` false positives on `ExceptionGroup` /
  `BaseExceptionGroup` disappear. *Isolation should control the rule set, not the language level.*
  This was nearly missed on the one corpus built to validate those rules.
- **`_returns` matched any occurrence of a name inside a returned expression**, so
  `return self._grid_configure('columnconfigure', index, cnf, kw)` counted as returning `cnf`. Seven
  tkinter methods were reported at HIGH confidence with "the shared object is returned to callers", a
  claim the code does not support. It now asks whether the object actually escapes — directly, or via
  a container literal, `or`-default, or conditional. High-confidence mutable-default findings on
  tkinter went **7 → 3**; idlelib unchanged at 101.

### Measured

- **tkinter** (13 files): 62 scanner findings, 52 tier-1 lint. **46 of each are the same idiom** —
  `def method(self, cnf={}, **kw)` across the widget API — and scanner and ruff `B006` agree
  exactly on all 46, the clearest validation the merge design has had. Verdict ACCEPTABLE: `_cnfmerge`
  returns a fresh dict, so the shared default is read-only on the ordinary path.
- **asyncio** (35 files): 97 scanner findings, 14 tier-1 lint, 11 complexity hotspots (2
  `active-risk`, 3 `settled`). **`asyncio-fire-and-forget-task` fires twice** — the async shape family
  validated on the async corpus, which is what the run existed to do.

### Added

- Two false-positive classes (now 37): the Tk-style option-dict convention, and `F821` on a name
  bound in an enclosing function and read in a nested closure (six instances in
  `asyncio/staggered.py` — a ruff scope limitation, not a defect).

## [1.9.0] - 2026-07-27

Phase 4 items 4.6 and 4.5b.

### Added

- **`measure_complexity.py` crosses complexity with git fix history** (item 4.6). Each hotspot gains
  `fix_commits_2y`, `fix_density`, `risk_rank` and a `verdict` of `active-risk` / `settled` /
  `quiet`, emitted as a first-class output so the crossing cannot be forgotten. On coverage.py the
  most-repaired function in the codebase (`sysmon_py_start`, 5 fix commits) scores **7.5** while
  `pytracer._trace` scores 9.0 with 2 — complexity alone ranks them backwards, which is what this
  exists to correct.
  - `active-risk` is gated on the **fix history alone**. A first cut required high complexity *as
    well* and duly labelled the single most-repaired function `quiet`.
  - The threshold is the constant `ACTIVE_RISK_FIXES = 2`, not a median: over a handful of hotspots a
    median-derived gate lands on whichever value sits in the middle and moves with it.
  - **A missing or shallow history yields `unknown`, never zero.** Treating absent history as zero
    fix commits would mark every hotspot `settled` and reproduce the inversion exactly.
- **`tools/`** (item 4.5b), with the boundary stated: `scripts/` answers questions about a reviewed
  project, `tools/` about the toolkit itself. First inhabitant `shape_coverage.py` measures Phase 0's
  metric on demand, since it decays silently otherwise.

### Changed

- **`nesting_depth` recalibrated to reader-facing depth.** An `elif` chain is modelled by the AST as
  an `If` inside each `orelse`, so a flat if/elif/elif/else was counted as **depth 3**; and
  `if False:` debug blocks counted their whole contents. `pytracer._trace` moved 10.0 → 9.0 as a
  result, so complexity scores are **not comparable across this release**.

## [1.8.0] - 2026-07-27

Phase 3 of `docs/improvement-plan.md` — verification infrastructure. The toolkit can now gate a
release rather than only explore.

### Added

- **`/prior-art`** (item 3.1) and `docs/searching-trackers.md`, vendored verbatim from the family
  rather than paraphrased: both of `gh search issues`' footguns produce a *silent empty result* that
  reads exactly like "not reported". Adds the verdict vocabulary — `none` / `known` / `known-sharper`
  / `partial` / `reverted` / `refuted`. `reverted` exists because a merged-then-backed-out fix reads
  as closed on the tracker while being live in the code.
- **`docs/reproduction-convention.md`** (item 3.2). The status ladder
  (`candidate` → `confirmed` → `reproduced` → `reported` → `fixed`), and the rule that cost a
  session: **never patch-test in a live checkout** — `git archive HEAD | tar -x -C /tmp/repro`, and
  verify the target tree yourself afterwards. Also: prove the repro exercised the tree it thinks it
  did (editable installs, stale `__pycache__`, `sys.path` order), and **a negative result is a real
  result**.
- **`check_known_findings.py` + `/known-issues`** (item 3.3). Keyed on `(file, shape, qualname)`,
  never on line numbers, so a finding whose line moved is `present` — which removes the sibling C
  toolkit's whole `line_drifted` triage class.
- **`/audit`** (item 3.4). `AUDIT-RESULT: FIX=n CONSIDER=n POLICY=n SCANNERS=n/m -- <STATE>` on line
  1. **`BLOCKED` fires on any scanner that failed, timed out, crashed, or analyzed zero files, even
  when every finding it did produce was clean** — an incomplete audit reporting clean is the failure
  mode that gets shipped.

### Measured

- **The known-findings check found its own ceiling on the first run.** Against coverage.py's
  60-entry catalog: `present 2 | absent_in_qualname 1 | absent 2 | out_of_scope 2 |
  not_scannable 53`. **53 of 60 catalogued findings name an `agent-only` shape**, so a scanner
  cannot regression-check them. `not_scannable` and `out_of_scope` are therefore counted as
  `not_checked`, never as `absent` — a summary reading "2 still present, 58 clear" would have
  misrepresented a run that examined 7 entries.

### Fixed

- **Two catalog rows were silently lost to a location-parsing bug**, both reported `file_missing`:
  `patch.py:56-57, :74-75` (a continuation segment with no filename) and
  `tests/a.py:256-259, tests/b.py:290` (two files in one location). Real catalogs use four location
  shapes and the parser handled one.
- `absent` was being reported for files outside the scanned scope. `absent` claims we looked; the new
  `out_of_scope` verdict says we did not.

## [1.7.0] - 2026-07-27

Phase 1 of `docs/improvement-plan.md` (external tools) and Phase 2 item 2.1.

### Added — `unformatted-format-string-literal` (item 2.1)

- **New shape and scanner check.** A `{name}` literal reaching a message sink that never formats it,
  so the braces are shown to the reader verbatim — almost always a dropped `f` prefix. **Calibrated
  over CPython's `Lib/` (1,847 files): 4 raw candidates, 1 finding, 0 false positives.** idlelib,
  `_pyrepl` and coverage.py each produce 0, so it adds no false-positive pressure to the benchmark
  corpora. The confirmed instance is `Lib/test/test_tarfile.py:3871`, in the `else` branch of a
  platform check.
- Three differentials carry it, each matching a real guarded twin in CPython: an extra argument on
  the call (`_pyrepl/trace.py` formats only `if k or kw`), the literal being the receiver of
  `.format` (`runpy.py:125`), and requiring the field name to be an **identifier** — `{}` and `{0}`
  are indistinguishable from a regex quantifier or a literal brace in a character class, and those
  two classes alone were ~90% of the raw candidates.

### Added — the tiered lint pass (items 1.1, 1.4)

- **`run_lint_rules.py` + the `lint-rule-triager` agent.** A defect pass that uses ruff as the
  engine rather than a style pass: tier 1 means *the program does something the author did not
  intend*. Measured tier-1 totals — **idlelib 67, `_pyrepl` 7, coverage.py 19**; tier-1+2 across the
  three corpora is 997, closely reproducing the 989 the pre-plan survey measured.
- **The tier lists are now recorded in the source.** They had been measured once and lost, which is
  why this item had to re-derive them.
- `rule_validation` inspects each selected rule's `status` rather than testing membership, because
  removed rules are still listed by `ruff rule --all`. **It earned its keep on the first run**,
  catching two tier-1 codes (`PLW1514`, `RUF055`) that were preview-gated and therefore silently
  doing nothing — corroborated by the stderr capture, which is the only place ruff reports it.
- `shape_id` marks the 13 rules that overlap a catalogued shape so they are merged rather than
  double-reported. The first novel-shape harvest immediately found `B019` in this category
  (`lru-cache-on-method` had been catalogued since the first wave) and it is now mapped.
- `has_suppression_comment` is the measured dismissal signal: 6 of 19 tier-1 findings on coverage.py
  carry one, and **every one is a deliberate idiom** — `open = open  # pylint:
  disable=redefined-builtin`, which captures the builtin before mocking can replace it.

### Added — type integrity (item 1.3)

- **`check_typing.py` + the `typing-integrity-auditor` agent.** mypy only, with the project's own
  config, never overridden. Reproduces the survey exactly on coverage.py: plain mypy 0 errors, and
  the labelled `--disallow-any-unimported` second pass **3 phantom imports in one file**, all from
  `from coverage.plugins import FileReporter` — a module that does not exist, silently degrading
  three annotations to `Any`.
- **`files_checked == 0` is reported as FAILED, not clean**, and `failure_reason` names the known
  landmines with the fix for each. `_pyrepl` correctly reports FAILED with the stdlib-shadowing
  diagnosis rather than a false clean.
- Stale-ignore counts come **only** from mypy's own `unused-ignore` code, never from grep.

### Changed — item 1.2

- **`run_external_tools.py` no longer ships a competing curated ruff selection.** Its
  `F,B,SIM,S,RET,PIE,UP,PERF` default measured 65-92% style-grade across the three corpora while
  missing a quarter of the tier-1 defects. That script now answers "what does the project's own
  tooling say?" and defers defect-grade selection to `run_lint_rules.py`; leaving both in place would
  have shipped two contradictory ruff configurations in one toolkit.

## [1.6.0] - 2026-07-27

Phase 0 of `docs/improvement-plan.md` in full — the calibration loop is repaired in both directions.

### Added — the false-positive regression gate and the external-catalog read path

- **`diff_findings.py`** (item 0.5) — diffs two report directories and reports added / gone / moved /
  unchanged per finding list. Findings are keyed **without line numbers**, so a finding that shifted
  because lines were inserted above it is `moved`, not one regression plus one fix. Every list it
  compares is registered explicitly rather than sniffed, and anything unregistered, unreadable, or
  present in only one run lands in `notes` — a report that could not be compared must never read as
  "no change". It emits a `verdict` string rather than a pass/fail boolean, because nothing
  mechanical can tell a new true positive from a false-positive regression; only triage can.
  Verified against the shipped benchmarks: it reproduces idlelib v1→v2 as `100 → 101, added 1,
  gone 0, unchanged 100`, matching `docs/decision-log.md` D-02 exactly.
- **`--catalog PATH` in `build_informed_briefing.py`** (item 0.2) — folds an external findings repo
  into the briefing. `informed-explore.md` had documented this flag since the command was written and
  nothing implemented it. Accepts a `findings.json`, a project directory, or a findings-repo root.
  Entries for the target project are rendered as "verify, then move on"; entries from *other*
  projects are rendered separately as cross-project evidence — explicitly **not** claims about this
  codebase, but shapes confirmed elsewhere that are worth hunting here. The cross-project list is
  narrowed to the shapes the requested agent owns (with the dropped count stated); the target-project
  list never is, because dropping an entry from the do-not-re-derive set invites re-derivation.
  `--catalog-dir` is accepted as a synonym, since the plan named it that way.
- **`tests/test_release_discipline.py`** (item 0.6) — the tripwire for the incident where two agents
  were added without a release and were invisible to the agent registry for a session. Asserts the
  agent/command/script inventory against explicit counts, so adding one fails the suite and the fix
  is to bump `plugin.json`. Also checks what makes an agent reachable at all: frontmatter present,
  `name` matching the filename, dispatch from some command, and that every agent and script the shape
  catalog points at exists.

### Added — the calibration loop's write-back direction (improvement plan, Phase 0)

- **`data/python_bug_shapes.json`: 40 → 89 shapes, schema 1 → 2.** Forty-nine shapes that the
  idlelib, `_pyrepl` and coverage.py runs had produced existed only as prose in the findings repo,
  where no scanner and no briefing could reach them. Every one is now a catalog entry with the full
  `pattern` / `guarded_twin` / `hunt` / `expected` / `caught_as` / `differential` set, and cites the
  findings that confirmed it. **Findings mapping to a catalogued shape: 40/111 (36%) → 111/111.**
- **Two new schema-2 fields.** `detectability` records the standing decision for each shape —
  `implemented` (38), `implementable` (19, AST-decidable and queued), `agent-only` (32, where the
  `hunt` directive *is* the deliverable). `aliases` carries a shape's earlier names so findings and
  reports written before a merge still resolve.
- **`build_informed_briefing.py` renders `detectability`**, so an agent reading an `agent-only`
  shape is told outright that no scanner will hand it the candidate.

### Changed

- **Shape ownership rebalanced by detectability.** A scanner-backed shape belongs to
  `python-pitfall-scanner`, which triages the scanner's output; an `agent-only` shape belongs to the
  agent whose method finds it. Thirteen shapes moved, taking `python-pitfall-scanner` from 58 of 89
  to 45 and giving `pattern-consistency-checker` 14, `silent-failure-hunter` 7 and
  `api-surface-reviewer` its first.
- **Two duplicate shape names merged.** `divergent-capability-across-parallel-modules` →
  `one-concern-implemented-per-backend` (now 7 findings, the corpus's most productive shape), and
  `guard-names-abbreviated-sibling` → `isinstance-on-container-not-element`, whose own
  `guarded_twin` text already cited that finding's twin. Both recorded as `aliases`.

### Fixed — three bugs surfaced while building the Phase 0 tooling

- **`analyze_history.py` leaked its `git log` pipes and could deadlock.** `_run_git_streaming`
  returned a `Popen` whose `stdout` was never closed, so a `ResourceWarning` was emitted at
  collection time — *after* the JSON had been written — and landed inside
  `reports/coveragepy_v1/analyze_history.json`, making that report unparseable. The pipes are now
  closed via a `with` block, and stderr goes to `DEVNULL` rather than an undrained `PIPE`: nothing
  read that pipe, so `git` would block writing stderr once it filled while the script blocked reading
  stdout. The corrupt report artifact has been repaired.
- **`analyze_history.py` passed a possibly-`None` stream to `parse_git_log`.** `Popen.stdout` is
  `Optional`; a failed spawn would have crashed with a `TypeError` inside the parser rather than
  reporting a git problem. (Pre-existing; mypy had been flagging it.)
- **`duplicated-guard-wrong-operand` printed every simple name twice** and without a bound, producing
  messages like *"though text, text, line, col, chars, chars, m, m, … was computed in between"*. A
  plain assignment target was yielded both by `_dotted_name` and by the `ast.walk` beside it. Names
  are now deduplicated in source order and the list is capped with an "and N more" tail. Detection is
  unaffected — idlelib still reports 101 findings.

### Fixed — three bugs found by the coverage.py benchmark

- **`analyze_imports` fan-in absorbed every submodule import into the package `__init__`.** The
  prefix match `t.startswith(f_module + ".")` made `coverage/__init__.py` match every `coverage.*`
  target, reporting a fan-in of **209 in a 44-file package where the true figure is 24**. This is the
  same prefix fallback that `detect_cycles` had already dropped after the `_pyrepl` run — the fix was
  never propagated to its sibling function, which is exactly the shape the toolkit's own
  `git-history-analyzer` hunts.
- **Every reported import cycle carried a duplicated node.** The reconstruction seeded the path with
  the closing node and then walked back onto it, so each cycle came out one element too long and a
  2-cycle rendered as three nodes (`a -> b -> b`). **All 26** cycles reported for coverage.py were
  affected.
- **`TYPE_CHECKING`-only imports were counted as runtime cycles.** A guarded import does not run —
  avoiding the cycle is precisely why the guard is there. coverage.py has 20 such edges, and dropping
  them takes its cycle count from 26 to 20. `detect_cycles` already parsed the flag and then ignored
  it; `include_type_checking=True` keeps the type-time graph available.
- **`correlate_tests` reported 0% for any package whose tests live in a sibling tree.** Second
  occurrence of this gap (`_pyrepl` was the first), and it reads as a finding about the project when
  it is an artefact of the scope. It now searches the project root *and* the scanned tree's own
  parent, preferring a `test_<package>/` subdirectory. coverage.py: 0% → **61.4%** (101 test files);
  `_pyrepl`: 0% → **36.0%** via `Lib/test/test_pyrepl`; idlelib unchanged at 84.8%. The new
  `external_test_roots` field distinguishes "no tests" from "tests found elsewhere".

## [1.5.0]

### Added

- **The remaining banked shapes from the `_pyrepl` benchmark** — catalog is now **40 shapes, 21 of
  them `confirmed`**. Ten are executable checks; two are git-shaped and assigned to
  `git-history-analyzer` with no backing script, which is the honest classification rather than a
  forced AST approximation.
  - `api-value-domain-mismatch` — a guard compared against a value the API can never return
    (`unicodedata.category(k) == "C"`; the API returns two-letter subclasses). The guard reads like
    validation and never fires.
  - `isinstance-on-container-not-element` — `isinstance(cmd, T)` where `cmd` was subscripted earlier
    in the same scope, so it holds the spec tuple, not the object.
  - `mock-callable-as-spec` — `MagicMock(lambda ...)`; the first positional parameter is `spec`, so
    the callable is never called and every assertion downstream passes vacuously.
  - `decode-error-treated-as-incomplete` — a decode failure handled as "need more bytes", so invalid
    input grows the buffer forever and the stream goes permanently deaf. A silent hang, not an error.
  - `unvalidated-numeric-from-environment` — `int(os.environ[...])` used as a dimension with no range
    check, typically the branch that got less scrutiny than the syscall beside it.
  - `wrapper-mutates-foreign-collection` — mutating a collection reached through another object,
    leaving the owner's bookkeeping stale.
  - `save-state-clobbered-by-reentry` — a snapshot-then-modify method with no idempotence guard, so a
    second call saves the already-modified state as the "original".
  - `return-ignored-against-checked-family` — an FFI status return discarded where its siblings are
    all checked.
  - `divergent-sentinel-across-parallel-modules` — **project-level**: parallel per-platform modules
    constructing one type with different empty-value sentinels.
  - `unguarded-inverse-of-guarded-operation` — **project-level**: an add guarded by a policy flag with
    its inverse unguarded.
  - `coverage-claiming-commit-that-reduced-coverage` and `incomplete-fix-residue-at-an-answered-todo`
    — catalogued for `git-history-analyzer`; both need a diff, not a tree.
- **Project-level checks.** `analyze()` now collects the parsed corpus and runs `_PROJECT_CHECKS`
  after the per-file pass, so a shape can compare files against each other. `analyze_file` is
  unchanged for callers that do not want it.
- Five more false-positive classes in `data/python_non_bugs.md` (now 30).

### Fixed

- `_call_name` returns `""` when a call sits in the receiver chain, so `bytes(buf).decode(...)` — the
  archetypal instance of `decode-error-treated-as-incomplete` — was invisible to its own check. Method
  names are now read off the `Attribute` directly.
- The env-numeric check looked for the validating comparison on the *call expression* rather than on
  the name the value was bound to, so every correctly-guarded instance was reported.
- Resolving a handler's parent `Try` by walking the tree per handler is quadratic; on stdlib-sized
  files it alone added minutes to a full run. Same for marking conditional bodies with a per-`if`
  `ast.walk`, now a single flag-carrying DFS.

### Calibration

Calibrated over CPython's `Lib/` (1847 files) across five passes: **1962 raw → 56**. Every runaway
was a mechanical defect in the check rather than a bad shape, and each fix is now a regression test:

| Check | raw | final | what was wrong |
|---|---|---|---|
| `return-ignored-against-checked-family` | 1414 | 9 | keyed on get/set stems, collapsing `self.__setstate` and `self.state` into one family; 720 of the survivors were test modules constructing objects |
| `unguarded-inverse-of-guarded-operation` | 341 | 7 | matched bare local names, pairing `glob.py` against `argparse.py` |
| `wrapper-mutates-foreign-collection` | 77 | 2 | any call in the receiver chain counted, including ordinary use of a returned object |
| `save-state-clobbered-by-reentry` | 60 | 0 | fired on `__init__`/`__enter__`, which are supposed to snapshot |
| `isinstance-on-container-not-element` | 40 | 8 | ignored ordering, so `Counter.__add__`'s guard-then-subscript matched |

The `isinstance` shape was also **reframed**: the original "second argument is not a type" form is
undecidable statically — 31 of 40 matches at stdlib scale were legitimate lowercase class names — so
the transposed-argument variant is now catalogued for the agent and the scanner checks only the
decidable container-vs-element form.

## [1.4.0]

### Added

- **The three highest-value shapes banked from the `_pyrepl` benchmark**, taking the catalog to 28
  shapes with 10 `confirmed`:
  - `signed-length-from-untrusted-header` — a length/offset unpacked with a *signed* `struct` code
    and never checked for negativity. In C this is the classic signed-overflow read; in Python it is
    harder to spot and worse in one respect, because a negative bound does not raise — negative
    slicing re-anchors, so a crafted file parses cleanly and yields attacker-chosen bytes.
    Exemplar: `_pyrepl/terminfo.py:373`, five header counts unpacked `<hhhhhh` with only
    upper-bound checks, where ncurses range-checks all six.
  - `asymmetric-encode-decode-pair` — the same path read and written with different
    `encoding=`/`errors=`, so the program's own round-trip destroys data. Exemplar:
    `_pyrepl/readline.py:443` vs `:460`, where a latin-1 `~/.python_history` is destroyed
    unrecoverably on first exit; `Modules/readline.c` uses `surrogateescape` on both sides.
  - `one-lifecycle-hook-two-meanings` — a commit-semantic hook (`finish`/`commit`/`save`) invoked
    on an abort path, where the override implements only the success meaning. Exemplar:
    `_pyrepl/commands.py:225-229`, where Ctrl-C persists the abandoned line to `~/.python_history`.
- Four false-positive classes in `data/python_non_bugs.md` (now 25), all learned from the
  stdlib-scale calibration: codec-varying test suites, predicates read as lifecycle hooks,
  outcome-parameterized hooks, and self-written headers round-tripped by a test.

### Fixed

- `_manual_codec` read `self.encode(text)` — a method taking *data* — as `str.encode` taking a codec
  name, inventing a mismatch in `idlelib/iomenu.py`.
- The signed-header check tainted every name in a tuple unpack when any field was signed. Struct
  formats are now expanded to one type code per produced value (handling repeat counts, `Ns`
  consuming N bytes for one value, and `Nx` producing none) and aligned positionally with the
  targets, so only names that actually received a signed field are flagged.

### Calibration

- All three shapes were calibrated over CPython's `Lib/` (1847 files) rather than on the target
  package, per the established methodology. The raw pass produced **904 findings; the calibrated
  pass produces 17**, of which all 9 high-confidence hits are real instances of their shape. The
  reduction came almost entirely from `asymmetric-encode-decode-pair` (876 → 3): it paired every
  reader against every writer of a path, and 543 findings came from `test_io.py` alone. Requiring
  exactly one distinct codec per side — which is what "the two sides disagree" presupposes —
  removed the class outright.

> **Note.** The entries below were written under an `[Unreleased]` heading that was never renamed
> when 1.4.0 was cut, leaving the file with two `[Unreleased]` sections. The work shipped in 1.4.0.
> It is kept as a separate block rather than merged into the one above so the two calibration batches
> stay distinguishable: this batch took the catalog to 22 shapes, the block above took it to 28.
> A test now enforces at most one `[Unreleased]` heading (see `tests/test_release_discipline.py`).

### Added — earlier in the 1.4.0 cycle

- New shape `test-cannot-fail` — tests that pass regardless of what the code under test does: empty
  bodies, constant-only assertions, `assertTrue(all(filter(...)))` (where `filter` already dropped
  everything the predicate rejects), asserting methods that lost their `test` prefix, and classes with
  fixtures but no tests. Calibrated on idlelib, where the raw pass was 133 findings and ~85% were two
  false-positive classes: assertions aliased to locals (`Equal = self.assertEqual`, ubiquitous in
  CPython's tests) and DRY assertion helpers called from real tests. After encoding both, 21 findings
  with all five high-confidence hits matching an agent's independent findings.
- **Three more shapes from the idlelib agent benchmark**: `flag-not-reset-on-early-exit` (a guard
  flag set at entry but reset only on the success path, so every later call silently no-ops),
  `guard-rechecks-call-receiver` (`m = prog.match(...)` followed by `if not prog:` — the guard names
  the receiver, not the result), and `falsy-check-for-none-default`. Catalog is now 22 shapes, 4 of
  them `confirmed` against real findings.
- **Five new bug shapes derived from a 40-bug audit of CPython's pure-Python stdlib** — the catalog
  previously covered *none* of that audit's pattern families. Added `except-exception-too-broad`
  (~50% of the audit's confirmed findings: `except Exception:` around a narrow operation with a
  swallowing handler), `cleanup-only-on-success-path` (~20%: `close()`/`quit()` at the end of a `try`
  instead of in `finally`), `error-reported-below-warning` (~17%: failures logged only at
  debug/info, invisible under default configuration), `except-in-loop-without-exit` (a persistent
  failure inside `while True:` becomes a silent hang), and `raise-without-from-in-except`. Catalog is
  now 19 shapes.
- **New agent + script: `python-pitfall-scanner` / `scan_python_pitfalls.py`** — the toolkit's first
  dedicated bug-finding capability. Fourteen AST checks mapping 1:1 to the shapes in
  `python_bug_shapes.json`, each emitting a confidence level (`high`/`medium`/`low`) derived from that
  shape's differential. The builtin exception hierarchy is read from the running interpreter rather
  than a hardcoded table, so `except`-ordering analysis is always correct for the Python in use.
  Options: `--check ID[,ID...]` to select shapes and `--exclude PAT[,PAT...]` to drop generated trees.
  Output includes a `by_directory` breakdown, because real-world runs showed generated content
  (report artifacts, golden fixtures) is the dominant false-positive source and is best triaged a
  directory at a time.
- Registered the `pitfalls` aspect in `explore`, and put `python-pitfall-scanner` first in Group B —
  behavioural bugs rank above code smells.
- New shared module: `scan_common.py` — the utilities every analysis script needs (project-root
  detection, file discovery, AST parsing, CLI parsing, the JSON envelope, finding deduplication).
  Every script now imports from it. Previously `find_project_root` was byte-identical in all nine
  scripts and `discover_python_files` had drifted into three divergent variants.
- New data catalog: `data/python_bug_shapes.json` — 14 reusable Python bug *shapes* (not file:line),
  each with its guarded twin (the fix pattern), a sibling-hunt directive, expected behavior, how the
  defect surfaces, and a differential for when *not* to flag it. Covers mutable default arguments,
  late-binding closures, unreachable `except` ordering, `return`-in-`finally`, `__eq__` without
  `__hash__`, mutation during iteration, the asyncio family (fire-and-forget tasks, blocking calls in
  `async def`, un-awaited coroutines), `lru_cache` on methods, shared class-level mutable attributes,
  bare `except`, exceptions in `__del__`, and `is`-with-a-literal.
- New data catalog: `data/python_non_bugs.md` — false-positive taxonomy in 15 classes, each stating
  the symptom, why it is a non-bug, and what the real bug looks like so genuine instances are never
  suppressed.
- New script: `build_informed_briefing.py` — assembles the informed-review briefing (bug shapes
  scoped per agent + false-positive taxonomy + cross-cutting triage rules) as Markdown. Folds in a
  target project's accumulated findings memory from `.code-review/findings.json` when present, using
  a schema wire-compatible with the `*-review-findings` companion repositories.
- New command: `informed-explore` — same coverage as `explore`, but every agent reads the briefing
  first, so a run hunts un-found siblings of established shapes instead of re-deriving basics.
  Records confirmed findings to `.code-review/findings.json` for the next run.
- New agent: `test-investigation-agent` — finds bugs by treating tests as invariant specifications. Reads existing tests to extract what developers believe should be true, maps those beliefs to structurally similar code, and checks whether the invariants hold everywhere they should.
- New script: `extract_test_invariants.py` — supporting script that extracts assertions from test files, classifies invariant types, maps tests to source functions, and finds structurally similar functions using name-pattern and signature matching. Three-tier test selection (bug-fix tests, error/boundary tests, churn-guided) with 30-test budget cap.
- Added `test-invariants` aspect to the explore command (Group D).
- `explore` now supports `--runs N` (independent naive passes, deduplicated across runs) and
  `--informed-reruns` (with `--runs 3`, the third pass targets adjacent code and structural siblings
  of what the earlier passes confirmed). Documents the 2-naive-plus-1-informed review shape.
- `CLAUDE.md` — development guide covering architecture, conventions, the data catalogs and their
  `validation` grades, gotchas, and an explicit list of known gaps.

### Fixed — earlier in the 1.4.0 cycle

- `analyze_imports.py`: `from .X import Y` resolved to the *parent* package instead of the containing
  one (`Lib.terminfo` rather than `Lib._pyrepl.terminfo`), so resolved targets matched no file and
  **`fan_in` was zero for every file** in any project using relative imports. On `_pyrepl`, 0 of 25
  files had a nonzero fan-in; after the fix, 22 do. The four existing tests asserted the wrong values —
  rewritten against ground truth obtained by building the package layout on disk and importing it.
- `analyze_imports.py`: `detect_cycles` fabricated edges. `module_to_file` covers only files that have
  imports, so a target naming an import-free module fell through to a prefix match that resolved it to
  the enclosing package's `__init__`. A three-file DAG reported a phantom cycle. On `_pyrepl`, reported
  cycles went from 15 phantom to 1 real.
- `scan_python_pitfalls.py`: `except-in-loop-without-exit` no longer fires when the handler reports
  loudly (the shape's complaint is "no diagnostic"), and reserves `high` for a `while True:` whose
  entire body is the guarded operation. A REPL or accept loop that does other work each iteration makes
  progress even when one operation keeps failing.

- `--max-files` with a non-integer argument now exits with a JSON error instead of an unhandled
  `ValueError` traceback.
- Documentation drift: both READMEs claimed 14 agents / 4 commands / 7 helper scripts; the actual
  counts are 16 / 5 / 12 as of this release.
- `correlate_tests.py` omitted `scan_root` from its JSON envelope, unlike every other script.
- `scan_python_pitfalls.py` scopes a single-file target to that file. Several sibling scripts instead
  fall back to the project root, silently turning "scan this file" into "scan everything".
- `extract_test_invariants.py` emitted `invariant_types` in set-iteration order, so output varied
  between runs with `PYTHONHASHSEED`. Output is now sorted and reproducible — non-reproducible output
  would otherwise defeat cross-run deduplication under `explore --runs N`.

## [1.3.0] - 2026-03-16

### Enhanced

- Memory reduction: all 7 file-processing scripts now accept `--max-files N` to cap file processing (default: unlimited).
- Memory reduction: `discover_python_files` converted to generators across 6 scripts.
- Memory reduction: `analyze_history.py` streams git log output instead of buffering.
- Memory reduction: `analyze_history.py` uses `-U0` diffs for function churn (zero context lines).
- Memory reduction: `analyze_imports.py` prunes intermediate fields after graph building.
- Memory reduction: `find_dead_symbols.py` drops per-file referenced_names after global accumulation.
- Memory reduction: `run_external_tools.py` early-stops parsing at max_findings limit.
- Memory reduction: explore/health/hotspots commands default to max 2 concurrent agents.
- Memory reduction: git-history-analyzer reuses git-history-context output instead of re-running script.

## [1.2.0] - 2026-03-16

### Enhanced

- External tool integration: 6 agents now incorporate findings from ruff, mypy, vulture, and coverage.py artifacts when available. Tools are optional — all agents work fully without them.
- explore command: Phase 0.5 runs external tools when available, with --skip-tools and --tools flags for control.
- dead-code-finder: merges ruff F401/F811/F841 and vulture findings with script output, deduplicating overlaps.
- silent-failure-hunter: incorporates ruff B (bugbear) and S (security) findings as additional bug-risk signals.
- complexity-simplifier: uses ruff SIM/RET/PERF findings as concrete simplification targets, with readability override.
- tech-debt-inventory: adds ruff UP (pyupgrade) deprecated-syntax findings to debt inventory.
- type-design-analyzer: incorporates mypy type errors to validate annotation accuracy and type design ratings.
- test-coverage-analyzer: uses coverage.py artifacts (XML/JSON) for precise line-level coverage when available, with freshness assessment.

## [1.1.0] - 2026-03-16

### Added

- `run_external_tools.py` script: detects, runs, and normalizes output from ruff, mypy, vulture, and reads coverage artifacts. Works when no tools are installed.
- Test suite for `run_external_tools.py` with coverage XML/JSON parsing, freshness assessment, tool detection, and CLI tests.
- Marketplace file (`.claude-plugin/marketplace.json`) for plugin discovery and installation.
- Installation instructions in both top-level README and plugin README (marketplace, direct, local, and manual methods).
- Prerequisites section documenting Python 3.10+ and Git requirements.
- Task-workflow skill for standardized development workflow (issue, branch, code, test, commit, PR, merge).
- CHANGELOG.md to track all notable changes.
- README.md with overview, quick start, and links to detailed plugin docs.
- MIT LICENSE crediting original and adapted authors.
- .gitignore (Python template).
- Test suite for all 6 plugin scripts (116 tests).
- project-docs-auditor agent for auditing out-of-code documentation (README, CLAUDE.md, config files) accuracy against the codebase.
- git-history-context agent: runs first in explore pipeline, provides churn metrics, change velocity, co-change clusters, and per-module stability as temporal context for all subsequent agents.
- git-history-analyzer agent: runs last in explore pipeline, performs fix completeness review, similar bug detection (fix propagation), feature review, churn×quality risk matrix, historical context annotation, and co-change coupling analysis.
- analyze_history.py script: queries git history for file/function churn, commit classification, recent fixes/features/refactors, and co-change clusters.
- Test suite for analyze_history.py (45 tests) including GitTempProject helper for git-based tests.

### Enhanced

- 6 agent prompts now invoke their corresponding analysis scripts for precise, machine-verified data before qualitative analysis: architecture-mapper, complexity-simplifier, test-coverage-analyzer, tech-debt-inventory, type-design-analyzer, dead-code-finder.
- All 11 agents now include a Classification Guide (FIX/CONSIDER/POLICY/ACCEPTABLE) for consistent finding categorization.
- consistency-auditor: split severity into correctness vs. readability dimensions with examples.
- complexity-simplifier: added "When NOT to Simplify" section (heterogeneous cases, intentional duplication, readable complexity) and abstraction cost validation.
- test-coverage-analyzer: risk-weighted ratings based on failure impact, code complexity, and change frequency.
- pattern-consistency-checker: behavioral similarity verification before flagging divergence, abstraction qualification for missing abstraction suggestions.
- api-surface-reviewer: breaking change classification ([breaking]/[additive]/[deprecation]) with migration path guidance.
- explore command: deduplication and conflict resolution in synthesis phase, classification-based summary template with "Tensions" section.
- health command: calibrated scoring rubric with anchor points, FIX count column, deduplication before scoring.
- architecture-mapper: classification tags on circular dependency findings.

### Fixed

- `typing_extensions` removed from `_STDLIB_TOP_LEVEL` in `analyze_imports.py` — it is a third-party package, not stdlib.
- Dead `elif` branch in `correlate_tests.py` `_match_test_to_source` — duplicate condition made subpackage matching unreachable.
- Missing trailing newline after `json.dump` in `analyze_history.py` output (6/7 scripts had it).
- Missing `.egg-info` directory exclusion in `analyze_history.py` `compute_function_churn_level2`.
- Unused variable `scores` removed from `measure_complexity.py`.
- 10 unused imports removed across `correlate_tests.py`, `helpers.py`, and 6 test files.
- Dead `"*.egg-info"` entry removed from `analyze_imports.py` exclude set (glob pattern in set intersection never matches).
- Unprotected `int()` calls in `analyze_history.py` `parse_args` now catch `ValueError` with clear error messages.
- Unknown CLI flags in `analyze_history.py` now produce a warning instead of being silently ignored.
- Broken `../pr-review-toolkit/` links removed from plugin README.
- Test helper `_SCRIPTS_DIR` path to point to `plugins/code-review-toolkit/scripts/`.
