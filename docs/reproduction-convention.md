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

**Always work on a copy:**

```bash
# from the project root, at the commit under review
mkdir -p /tmp/repro && git archive HEAD | tar -x -C /tmp/repro
# or, if you need git history in the copy:
git worktree add /tmp/repro HEAD
```

Then patch, run, and observe in `/tmp/repro`.

**Verify the target tree yourself afterwards.** `git -C <project> status --short` must be clean, or
show only what was there before you started. Do not accept an agent's claim of restoration; check.

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
networked such as model downloads) and one runner that exports a git ref and runs a reproducer
against that export:

```bash
./run.sh F-012.py main              # the default branch: must print BUG
./run.sh F-012.py origin/fix-branch  # a branch that claims a fix: should print OK
```

Every reproducer ends by printing exactly one line, `BUG: <what went wrong, with the observed
value>` or `OK: <what correct looks like>`. That makes a sweep over all reproducers and all
branches mechanical, and it turns "fixed on branch X" into an observation rather than a reading.
Keep each reproducer to the fewest lines that show the defect (5–25 is typical; a race may need
50), and say in a comment what, if anything, is simulated.

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
