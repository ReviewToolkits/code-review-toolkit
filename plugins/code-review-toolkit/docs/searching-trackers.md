# Reliable issue/PR search on a project tracker (prior-art step)

_Vendored from [`fusil-extensions-findings/notes/searching-trackers.md`](https://github.com/devdanzin/fusil-extensions-findings). Keep in sync with the family copy; do not fork the recipe._

Before you call a finding **novel** — or file it upstream — check the extension's (or PyO3's, or
CPython's) tracker. The `informed-explore` briefing points here for the "is this already reported?"
step. `gh search` has two footguns that silently return empty; use the **search API** and you can
depend on it, even for large repos. (A concrete instance: `gh search issues "some_c_identifier"`
tokenizes identifiers oddly and can return a misleading empty result — never trust a bare-identifier
null; use human-language terms + labels, or the API below.)

## The recipe — `gh api search/issues`

```bash
R=OWNER/REPO

# count + first page (30 results) — enough to answer "is this reported?"
gh api -X GET search/issues -f q="repo:$R is:issue TERM1 TERM2" \
  --jq '.total_count, (.items[] | "#\(.number) [\(.state)] \(.title)")'

# exhaustive (large repo, >30 hits): paginate at 100/page
gh api --paginate -X GET search/issues -f per_page=100 \
  -f q="repo:$R is:issue TERM" \
  --jq '.items[] | "#\(.number) [\(.state)] \(.title)"'
```

- `-f q=...` is a form field, so spaces/quotes pass through cleanly — prefer it over hand-building
  the URL.
- A `total_count: 0` here is **trustworthy** (search ran and matched nothing).
- The API returns full item objects (`.state`, `.title`, `.body`, `.labels`, …) and `total_count`;
  `gh search` returns fewer fields.

## Query semantics (the two footguns)

1. **Space-separated terms = AND**, not a phrase. `q="Pubkey from_bytes"` finds issues containing
   **both** words (anywhere). Good for narrowing.
2. **Quotes = exact adjacent phrase.** `q='"Pubkey from_bytes"'` requires the literal string
   `Pubkey from_bytes` — almost always 0 hits. **This is the "multi-word search doesn't work" trap.**
   Only quote when you truly want a phrase.

Put every filter **inside `q`**, not as a `gh` flag:
`is:issue` / `is:pr`, `state:open` / `state:closed`, `in:title` / `in:body` / `in:comments`,
`label:X`, `author:X`, `repo:O/R`. (All validated against large + small trackers.)

## `gh search issues` (if you use it instead of the API)

- `--state` accepts **only `open` | `closed`** — **NOT `all`.** `--state all` *errors* ("invalid
  argument"), and if you pipe stdout to `jq`/`grep` you just see empty output and mistake it for "no
  results." Omit `--state` to get both states.
- Positional multi-word args are AND (fine); do **not** wrap them in quotes.
- Bare C/Rust identifiers tokenize unreliably — search human-language terms (e.g. "panic on empty
  input") plus a label (`label:bug`), not just `some_fn_name`.

## Fallback for small repos

For a tracker with ≲100 issues, a full listing + local grep is also reliable and index-independent:

```bash
gh issue list --repo "$R" --state all --limit 300 --json number,title,state,body \
  --jq '.[] | "#\(.number) [\(.state)] \(.title)  \(.body // "" | gsub("[\\n\\r]";" ") | .[:200])"' \
  | grep -iE 'term1|term2'
```

Note `--state all` **is** valid for `gh issue list` (unlike `gh search issues`) — that inconsistency
is what caused the confusion in the first place.

## Whole-catalog sweeps: dump once, search locally (toolkit-local)

_This section is code-review-toolkit's own addition and is not in the family copy yet. Keep it
below the vendored recipe, and drop it here once the family copy carries it._

The recipe above is right for a handful of findings. A sweep over a whole `findings.json` (hundreds
of findings, three or more queries each) hits two limits:

- **The search API allows 30 requests a minute**, shared by every agent using the same token. A
  sweep stalls on HTTP 403 long before it finishes.
- **REST list endpoints stop returning items at about 30,000, without an error.** Past that point a
  page comes back empty, and `gh api --paginate` ends normally, so a truncated listing reads
  exactly like a complete one. Issue comments on a large tracker pass that mark.

Dump the tracker once in update-time windows (each well under the cap, every item de-duplicated by
id), then search the dump:

```bash
d=$(mktemp -d "${TMPDIR:-/tmp}/crt-tracker.XXXXXX") && echo "$d"
python3 <plugin_root>/scripts/dump_tracker.py dump --repo OWNER/REPO --out "$d"
python3 <plugin_root>/scripts/dump_tracker.py search "$d" retry timeout          # terms are AND
python3 <plugin_root>/scripts/dump_tracker.py search "$d" '~upload\s+limit' --pr --open
```

- `dump` writes `issues.jsonl` (issues and PRs, with `merged`), `comments.jsonl` and
  `manifest.json`. It costs one REST request per 100 items (about 500 for 20,000 issues and 30,000
  comments), against the 5,000-an-hour core limit, not the search limit.
- **Read `complete` in the manifest.** `true` means the dump holds as many issues and PRs as the
  search API counts for the repository; `false` means items are missing; `null` means the count
  couldn't be checked or a request failed (see `errors`). Never call a finding novel from an
  incomplete dump.
- `search` matches each term as a case-insensitive substring of the title, body or any
  conversation comment (`~` makes a term a regular expression); hits rank title > body > comment.
  Filters: `--title`, `--pr`/`--issue`, `--open`/`--closed`, `--since YEAR`, `--no-comments`.
- Use at least three queries per finding (the symptom, the mechanism, the location or symbol), then
  read every plausible hit in full (`gh api repos/OWNER/REPO/issues/N` and its comments).
- A dump doesn't hold PR review (inline) comments, Discussions or private security advisories. For
  a FIX or security finding with no hit, double-check once with the search API above before calling
  it novel, and record what the dump didn't cover.
