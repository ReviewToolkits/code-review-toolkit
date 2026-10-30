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
