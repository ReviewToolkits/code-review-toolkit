# Reproduction convention

How a finding earns the word **reproduced**, and what the `status` field means.

This formalises what already worked: 47 of 60 coverage.py findings reached `status: reproduced` and
six harnesses were preserved. It also formalises what went wrong — see *Never patch-test in a live
checkout*, which cost a session and produced one confident, wrong finding.

---

## The status ladder

| `status` | What it claims | What is required |
|---|---|---|
| `candidate` | A scanner or an agent flagged it | Nothing beyond the flag. **Not** a finding yet |
| `confirmed` | A human read the code and the mechanism holds | A cited `file:line` and a concrete failure scenario: inputs → wrong outcome |
| `reproduced` | The failure was **observed** | A command and its output. Someone else can run it |
| `reported` | Filed or commented upstream | An issue or PR link |
| `fixed` | The fix landed | The commit |

**`confirmed` → `reproduced` is the expensive step and the one that matters.** A confirmed finding is
an argument; a reproduced finding is a fact. When a report mixes them, label every row — a reader
cannot tell them apart from the prose.

A finding that **fails** to reproduce does not disappear. It becomes `refuted`, and it stays in the
report with the evidence. **A negative result is a real result**: it is worth as much as a positive
one and it prevents the same hypothesis being re-derived next session.

---

## Never patch-test in a live checkout

**This is the rule that exists because it was broken.**

To prove a bug is load-bearing, the natural move is to re-introduce it (or apply the fix and watch
the symptom vanish). Doing that in the checkout under review corrupts the review itself:

- Another reviewer — or you, twenty minutes later — reads the modified file and produces a
  **confident, wrong finding** about code that was never in the tree.
- An agent that claims to have restored the file may not have. One did exactly that.

**Always work on a copy**, in a directory of your own (see *Where scratch lives*):

```bash
d=$(mktemp -d -p <run_dir>/tmp repro.XXXXXX)
git -C <project> archive HEAD | tar -x -C "$d"
# or, if you need git history in the copy (a clone only reads the original):
git clone -q <project> "$d/repo" && git -C "$d/repo" checkout -q <commit>
```

Then patch, run, and observe in `"$d"`. Don't use `git worktree add` on the checkout under review:
it writes into that repository's `.git`.

**Verify the target tree yourself afterwards.** `git -C <project> status --short` must be clean, or
show only what was there before you started. Do not accept an agent's claim of restoration; check.

---

## Where scratch lives

- **A run-owned area, not a shared `/tmp`.** Create `<run_dir>/tmp/` once (outside an `explore`
  run, any directory you own), and make every scratch directory inside it:
  `d=$(mktemp -d -p <run_dir>/tmp <name>.XXXXXX)`. Directories under a shared `/tmp` have vanished
  mid-run (another agent's cleanup, the system's temp cleaner), and a fixed name such as
  `/tmp/repro` is overwritten by whichever agent runs next.
- **The harness sets `TMPDIR`** to its private directory before it runs project code. Uploads,
  caches and `tempfile` output then land there and go when it goes. Reproducers of upload paths
  have left files in a shared `/tmp` that outlived the run.
- **A loop that creates directories removes them on every exit path.** A runner that exports one
  ref per branch, without a trap, leaked hundreds of exports in one pass:

  ```bash
  d=$(mktemp -d -p <run_dir>/tmp export.XXXXXX); trap 'rm -rf "$d"' EXIT
  ```

  In Python, `with tempfile.TemporaryDirectory(dir=...)`.
- **Delete `<run_dir>/tmp` when the run ends**, after anything worth keeping is in `artifacts/`.

---

## Running code from the export

- **`python -I` ignores `PYTHONPATH`.** `PYTHONPATH="$d" python -I repro.py` imports the installed
  package, not the export. Use `-P` (it skips the script's directory but honours `PYTHONPATH`), or
  keep `-I` and put `sys.path.insert(0, "<export>")` at the top of the harness. Either way, print
  `module.__file__` (next section).
- **The cheapest reproducer is a test in the export's own tests directory.** Copy it into
  `"$d"/tests/` and run the project's own test command on that one file. It gets the conftest
  fixtures (app, database, factories) and the test configuration for free, with no harness to
  write. Keep the file in `artifacts/`: the export is disposable.
- **`import pkg.sub as m` can bind the wrong object.** When `pkg/__init__.py` re-exports a name
  that shadows the submodule (`from .sub import sub`), `m` is that object, not the module, and a
  monkeypatch of `m.helper` patches nothing. Take the module from `sys.modules["pkg.sub"]` or
  `importlib.import_module("pkg.sub")`.

---

## Test configuration that changes what you observe

- **`filterwarnings = error`.** When the project's pytest configuration turns warnings into
  errors, a deprecation warning from a dependency becomes an exception in your reproducer, and you
  observe a crash no user sees. Check the configuration first. Scope a targeted ignore to the
  reproducer (`@pytest.mark.filterwarnings("ignore:<message>:DeprecationWarning")`, or
  `warnings.catch_warnings()` around the call) and say in a comment which warning and why. If the
  warning *is* the finding, keep it.
- **Logger levels set at import.** `caplog.set_level(logging.DEBUG)` sets the root logger. A
  project logger that set its own level when imported still drops lower records, so an assertion
  that nothing was logged passes without capturing anything. Use
  `caplog.set_level(logging.DEBUG, logger="pkg.mod")`, or assert only at WARNING and above. Prove
  the capture works first with one record you know is emitted.

---

## Databases and web requests

- **Savepoint fixtures hide missing rollbacks.** Many suites wrap each test in an outer transaction
  and join the session to it through a SAVEPOINT (SQLAlchemy's
  `join_transaction_mode="create_savepoint"`, Django's `TestCase`). A missing `rollback()` after a
  caught `IntegrityError` is invisible there, because the fixture restores the session. Reproduce
  rollback and commit findings with a session of your own on a real transaction (Django:
  `TransactionTestCase`), and show the next statement failing.
- **A shared session poisons later requests.** A functional-test fixture that serves every request
  in a test from one database session lets one request's database error (a 500) break every later
  request in that test. The second probe then fails with "transaction aborted", not with the bug.
  Give each error-inducing probe its own test.
- **Go through the request pipeline.** Calling a view function directly skips its decorators and
  whatever the framework wraps around views: permission checks, CSRF, allowed methods, rate limits.
  A view-level finding reproduced that way has turned out to be a false positive. Reproduce
  view-level findings through the app's test client or test app with a real request. Call the function directly only for logic
  below the view, and say so.
- **Parallel reproducers need their own resources.** Reproducers that use the project's fixtures
  share its test database name, ports, cache keys and lock files, and collide when run in
  parallel. Give each run its own: a database-name prefix from an environment variable (patch it
  into the export's conftest if the project has no such setting: the export, never the checkout),
  a port from the OS (`bind(("127.0.0.1", 0))`), a private cache namespace. A killed teardown
  leaves its databases behind: at the end of the run, list them by prefix and drop them. External
  services (a cache server, a broker) still carry state between runs; when a verdict changes from
  one run to the next, suspect them first.

---

## Branch runs

Re-running reproducers against exports of older or unmerged branches: a branch may pin a test-only
dependency (a mocking library, a pytest plugin) that the default branch has since dropped. Its
conftest import then fails, and the reproducer reports `ERROR`, which says nothing about the fix.
Fetch that dependency at the exact version the branch's lockfile pins
(`pip download --no-deps <pkg>==<pin> -d "$d/vendor"`), check its hash against the lockfile, and
put it on the reproducer's path. `ERROR` on a branch is never "fixed".

---

## Prove the repro exercised the tree it thinks it did

A reproduction that ran against different code than you believe is worse than no reproduction,
because it carries the authority of an observation. Three ways this happens, all seen:

- **Editable installs.** `pip install -e` means `import coverage` resolves to the working tree, not
  the copy you patched. Print `module.__file__` inside the harness and check it.
- **Stale `__pycache__`.** A `.pyc` from before your edit will be used if the mtime granularity
  works against you. Run with `-B`, or delete `__pycache__` in the copy.
- **`sys.path` order.** A test run from the project root puts the project first regardless of what
  you installed. Print `sys.path[:3]`.

Every preserved harness should begin by printing the resolved module path. It costs one line and it
converts "I think this ran the patched code" into evidence.

---

## What a preserved harness looks like

Store it under `repros/<FINDING-ID>/` in the findings repo, with:

- `repro.py` (or `repro.sh`) — self-contained, no arguments, exits non-zero on the bug
- `README.md` — the command, the expected output on a buggy tree, the expected output on a fixed
  tree, and the exact commit it was run against
- **no machine-specific paths.** One captured artifact embedded a local venv path and a CPython
  build path and had to be removed before the first push. Check before committing.

The two-tree contrast is what makes a harness durable: "prints 75% here, 100% there" survives a
refactor that moves every line number.

---

## A shared harness, and one verdict line

When a review produces more than a few reproducers, give them one small harness module (isolated
`HOME` and data directories, the project's test configuration, fakes for anything slow or
networked such as model downloads) and one runner that exports a git ref into a private directory
(with the `trap` above), sets `TMPDIR`, and runs a reproducer against that export:

```bash
./run.sh F-012.py main              # the default branch: must print BUG
./run.sh F-012.py origin/fix-branch  # a branch that claims a fix: should print OK
```

Every reproducer ends by printing exactly one line, `BUG: <what went wrong, with the observed
value>` or `OK: <what correct looks like>`; the runner records a run that printed neither as
`ERROR`. That makes a sweep over all reproducers and all branches mechanical, and it turns
"fixed on branch X" into an observation rather than a reading. Keep each reproducer to the fewest
lines that show the defect (5–25 is typical; a race may need 50), and say in a comment what, if
anything, is simulated.

---

## Reproducing races and stalls

A race you've argued is a hypothesis; a race you've forced is a fact. **Never loop over random
timing until something happens**: a reproducer that fails one run in fifty proves nothing when it
passes.

- **Force the interleaving.** Monkeypatch a function on path A so it blocks on a
  `threading.Event` at the critical point; start path A in a thread, wait until it's parked, run
  path B, then release A. The window is now explicit and the result deterministic.
- **Start threads together** with a `threading.Barrier` when the question is "can these two
  operations overlap at all".
- **Event-loop stalls:** hold the lock (or run the slow operation) from a thread, then time a
  trivial coroutine on the loop (`asyncio.sleep(0)`, or a health endpoint through an in-process
  client) with a short timeout. "Did not return within 2 s" is the observation.
- **Deadlocks:** run the scenario in a subprocess with a timeout, and call
  `faulthandler.dump_traceback_later(timeout, exit=True)` inside it so a hang leaves every thread's
  stack behind instead of a silent timeout.
- **Cancellation:** cancel the task explicitly (`task.cancel()`) at the point under test, but then
  establish separately whether anything in production actually cancels it there. Frameworks often
  don't (anyio's `run_in_threadpool` defers cancellation until the thread finishes), and a finding
  whose only trigger is the injected cancel must say so.
- **Slow constants:** scale them down and say so (`TIMEOUT = 0.2  # real: 300 s`).
- **Environmental failures** (disk full, a locked database, a failing `unlink`): inject the real
  exception type with the narrowest monkeypatch, and name what it stands for.
- `sys.setswitchinterval(1e-6)` and a free-threaded build (`python3.14t`) raise the odds of an
  unforced race showing up in a stress run. They're for exploration; the reproducer itself still
  forces the interleaving.
- **Keep a reference to a lock you hold by hand.** `acquire_cm().__enter__()` with the context
  manager object dropped releases the lock when that object is garbage-collected, which is often
  at once. Path B then never contends, and the race "doesn't reproduce". Keep the object in a
  variable and call its `__exit__` yourself, or use `contextlib.ExitStack`.
- **`asyncio.run()` waits for executor threads.** It shuts down the default executor before it
  returns, so work handed to `run_in_executor(None, ...)` or `asyncio.to_thread()` has finished by
  then. An assertion made after `asyncio.run()` about detached work (it completed; the loop was not
  stalled) passes vacuously. Assert inside the running loop, while the work is still in flight.

---

## Many slices: one verdict fragment each

A large run splits the findings into slices, one reproduction agent each. Every slice writes
exactly one fragment, `<run_dir>/repro/slice-<NAME>.json`, and nothing else outside its own
reproducer files:

```json
{"slice": "S07", "ids": ["F-031", "F-032"],
 "verdicts": [
   {"id": "F-031", "verdict": "reproduced", "main": "BUG",
    "files": ["artifacts/repro/F-031.py"], "branches": {"origin/fix-x": "OK"},
    "branch_status": "fixed on origin/fix-x, not merged",
    "proposed_classification": "FIX", "classification_reason": "data loss observed",
    "corrections": {"locations": ["pkg/mod.py:40-44"]}, "notes": "..."},
   {"id": "F-032", "verdict": "no-mre-possible", "notes": "why no reproducer can exist"}],
 "new_findings": []}
```

`verdict` is `reproduced` (status `reproduced`), `not-reproduced` (status `refuted`) or
`no-mre-possible` (status `confirmed`, with the structural evidence in `notes`). `main` is the
reproducer's verdict line on the default branch. A slice proposes reclassifications and
corrections; it never edits `findings.json`. The orchestrator merges the fragments with
`<plugin_root>/scripts/merge_fragments.py` (coverage and consistency) and folds them in with
`<plugin_root>/scripts/apply_verdicts.py` (see `explore`, *Phase 3 pipeline*).

---

## Windowed fault injection

For a finding that depends on an allocation or syscall failing, injecting failure **everywhere** is
the wrong experiment: it masks the specific failure under a flood of unrelated ones, and the crash
you observe is usually not the one you are testing.

Use a **windowed, single-shot** injection — fail the Nth call and only the Nth — and sweep N. Then
patch-test to attribute: apply the candidate fix, re-run the same N, and confirm the crash moves or
vanishes. Attribution is what turns "it crashed under fault injection" into "this line is the cause".

---

## The `status` gate in synthesis

When a report is assembled, every finding must carry a `status`, and the summary must state the
mix — "38 FIX, of which 31 reproduced" rather than "38 FIX". A report that does not distinguish
argued findings from observed ones invites the reader to trust both equally, and the argued ones are
where the errors are.

**A finding with `status: candidate` does not belong in a report at all.** It belongs in the working
notes until someone reads the code.
