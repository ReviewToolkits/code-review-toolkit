---
description: "Search a project's tracker before calling a finding novel. The cheapest high-value step in a review: it refutes wrong findings, and it redirects 'file a new issue' to 'comment on the existing one'."
argument-hint: "[finding description, or a path to a findings.json]"
allowed-tools: ["Bash", "Read", "Grep", "Glob"]
---

# Prior art

Run this **before** claiming any finding is new, and before proposing that one be filed upstream.

It is the cheapest high-value step in the whole review process, and it pays in three distinct ways —
all three observed on a single corpus:

1. **It refutes wrong findings.** One claim that a released changelog entry described behaviour the
   code lacked died immediately: a later release explicitly documented the restore.
2. **It redirects "file new" to "comment on existing".** Three findings turned out to belong on open
   issues, including one open since 2023 where our diagnosis was *sharper than the reporter's* — a
   comment there is worth more than a duplicate.
3. **It finds the fix that was reverted.** A closed issue whose fix was merged and then backed out,
   with no follow-up, reads as fixed on the tracker and is live in the code.

**Input:** "$ARGUMENTS" — a finding description, or a path to a `findings.json` to sweep.

## The recipe

Read [`<plugin_root>/docs/searching-trackers.md`](../docs/searching-trackers.md) — it ships with the plugin
and is the authority. The essentials, because getting them wrong returns a *silent* empty
result that reads exactly like "not reported":

```bash
R=OWNER/REPO
gh api -X GET search/issues -f q="repo:$R is:issue TERM1 TERM2" \
  --jq '.total_count, (.items[] | "#\(.number) [\(.state)] \(.title)")'
```

- **Use `gh api search/issues`, never `gh search issues`.** The latter's `--state all` *errors*, and
  piping the failure into `jq` shows empty output that is indistinguishable from no results.
- **A `total_count: 0` from the API is trustworthy.** An empty result from anything else is not.
- **Space-separated terms are AND, not a phrase.** Quotes mean an exact adjacent phrase and almost
  always return zero — this is the "multi-word search doesn't work" trap.
- **Search human language plus a label**, not a bare identifier. Identifiers tokenize unreliably.
- Put every filter inside `q`: `is:issue`, `state:open`, `in:title`, `label:bug`, `repo:O/R`.

### Not on GitHub

- **GitLab.** `glab issue list --search "TERM1 TERM2" --all` if `glab` is installed; otherwise the
  REST API works anonymously for public projects (URL-encode the project path, `/` → `%2F`):

  ```bash
  P=group%2Fproject
  curl -s "https://gitlab.com/api/v4/projects/$P/issues?search=TERM1+TERM2&scope=all&per_page=100" |
    python3 -c 'import json,sys; [print("#%s [%s] %s" % (i["iid"], i["state"], i["title"])) for i in json.load(sys.stdin)]'
  # merge requests: .../merge_requests?search=...&state=all
  ```

  GitLab "work items" (tasks, epics) may not appear in the issues endpoint; check the project's
  web UI once to see where its tracking lives.
- **No public tracker, or a private one.** Many projects track work by **keys** in commit subjects
  and docs (`[fix] ... (B92)`, `ABC-123`, a `ROADMAP.md` or `TODO.md` with key → status). That is
  prior art too, and it's local:

  ```bash
  git -C <project> log --all --oneline -i -E --grep 'TERM1|TERM2'   # all branches, not just HEAD
  grep -rn -E '\b[A-Z]{1,3}[0-9]*-?[0-9]+\b' ROADMAP.md CHANGELOG.md docs/ | grep -i TERM
  ```

### Unmerged branches are prior art

The most consequential prior art is often a fix that already exists on a branch nobody merged yet
(a release branch, a long-running feature branch). Search them before calling anything novel:

```bash
git -C <project> branch -r --no-merged origin/HEAD          # what's unmerged
git -C <project> log --oneline origin/HEAD..<branch> -- <file of the finding>
git -C <project> diff origin/HEAD...<branch> -- <file>      # what the branch changed there
```

A finding fixed on an unmerged branch gets the verdict `fixed-unmerged` (below), with the branch
name, and should be checked by re-running its reproducer against an export of that branch.

## Workflow

1. **Identify the tracker.** From the project's `pyproject.toml` / `setup.cfg` / `setup.py` URLs,
   its README, or its git remote. Record it; a search against the wrong repo is worse than no
   search. Note whether the project also uses local tracker keys, and list its unmerged branches.

2. **Search each finding three ways.** One query is not a search:
   - the **symptom** in human language ("coverage reports 0% for symlinked files")
   - the **mechanism** ("relative_files realpath")
   - the **location** (the module or function name, plus `in:title`)

   Search `is:issue` *and* `is:pr` — a fix may exist as an unmerged or reverted PR, which is a
   different and more interesting answer than "not reported".

3. **Read what you find.** A title match is not prior art. Confirm it is the same mechanism, not
   merely the same symptom: adjacent findings routinely share a symptom and have distinct causes,
   and folding them together loses the sharper one.

4. **Classify each finding.**

   | Verdict | Meaning | What to do |
   |---|---|---|
   | `none` | Searched, nothing found | Safe to call novel — say which queries you ran |
   | `known` | An open issue describes the same mechanism | **Comment there.** Never open a duplicate |
   | `known-sharper` | An open issue, and our diagnosis is more precise | Comment with the sharper diagnosis; this is high-value |
   | `partial` | A related fix landed and did not cover this case | Frame as a follow-up on that PR, not as a fresh bug |
   | `reverted` | Fixed, then backed out, no follow-up | The tracker says fixed and the code says otherwise. Re-open |
   | `refuted` | The tracker shows the finding is wrong | **Withdraw it.** Record the refutation |
   | `fixed-unmerged` | An unmerged branch already fixes it | Report as "fixed on `<branch>`, not merged"; confirm with the reproducer |
   | `tracked-locally` | A local key (commit, ROADMAP) already tracks it | Cite the key; the maintainer knows, so it's context, not news |

5. **Write the verdict into the finding.** Add a `prior_art` field with the verdict, the issue
   numbers, and one line on why. A finding without one is not ready to be called novel.

## Reporting

```markdown
## Prior art ([N] findings checked against OWNER/REPO)

| Finding | Verdict | Issues | Note |
|---|---|---|---|

### Refuted ([N]) — remove these from the report
### Comment, do not file ([N])
### Novel ([N]) — queries run, so the negative is auditable
```

**A negative result is a real result.** State the queries you ran. "Not reported" with no evidence
is an assertion; "not reported — three queries, `total_count: 0` each" is a finding.

## Do not

- Do not treat a bare-identifier search returning nothing as evidence. It tokenizes unreliably.
- Do not conclude from a closed issue that the bug is fixed. Check whether the fix is still in the
  tree; the `reverted` verdict exists because that exact thing happened.
- Do not file anything. This command produces a verdict; filing is a separate, human decision.
