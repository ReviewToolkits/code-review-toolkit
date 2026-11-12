#!/usr/bin/env python3
"""Dump a GitHub tracker once, then search it locally: prior art for a whole catalog.

Checking a few findings against a tracker is a few `gh api search/issues`
calls. Checking hundreds is not: the search API allows 30 requests a minute,
and listing comments through the REST API stops returning items at about
30,000 without an error -- a later page is simply empty, which reads exactly
like the end of the list. This script avoids both:

``dump`` -- list issues (and pull requests, which the issues endpoint
includes) and their conversation comments, sorted by update time, in
**windows**: at most ``--max-pages`` pages (default 100, i.e. 10,000 items) per
window, the next window starting from the last ``updated_at`` seen, every item
de-duplicated by id. No window gets near the cap. It writes ``issues.jsonl``,
``comments.jsonl`` and ``manifest.json`` to ``--out``, and compares the issue
count with the search API's total for the repository: ``complete: false``
means items are missing, never "the tracker is smaller".

``search`` -- full-text search over a dump: every term must appear (in the
title, the body, or a comment); a term is a case-insensitive substring, or a
regular expression when it starts with ``~``. Hits rank title > body >
comment.

Not covered: pull-request review (inline) comments, Discussions, and private
security advisories. Say so when a dump is the evidence for "not reported".

Usage:
    python dump_tracker.py dump --repo OWNER/REPO --out DIR [--since ISO8601]
        [--no-comments] [--max-pages N]
    python dump_tracker.py search DIR TERM [TERM ...] [--title] [--pr | --issue]
        [--open | --closed] [--since YEAR] [--no-comments] [--limit N]
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scan_common import emit, exit_on_help  # noqa: E402

PER_PAGE = 100
# 100 pages of 100 is a third of the point where comment listing goes quiet.
DEFAULT_MAX_PAGES = 100

Fetch = Callable[[str], tuple[object | None, str | None]]


def gh_fetch(path: str) -> tuple[object | None, str | None]:
    """One REST request through `gh api` (one page, no --paginate)."""
    try:
        proc = subprocess.run(
            ["gh", "api", "-H", "Accept: application/vnd.github+json", path],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"gh api {path}: {type(exc).__name__}: {exc}"
    if proc.returncode != 0:
        return None, f"gh api {path}: {proc.stderr.strip()[:300]}"
    try:
        return json.loads(proc.stdout or "null"), None
    except json.JSONDecodeError as exc:
        return None, f"gh api {path}: bad JSON ({exc})"


def _issue_record(item: dict) -> dict:
    pull = item.get("pull_request")
    return {
        "n": item.get("number"),
        "pr": pull is not None,
        "merged": bool(pull and pull.get("merged_at")),
        "state": item.get("state"),
        "title": item.get("title") or "",
        "body": item.get("body") or "",
        "labels": [lbl.get("name") for lbl in item.get("labels") or [] if lbl],
        "user": (item.get("user") or {}).get("login"),
        "created": item.get("created_at"),
        "updated": item.get("updated_at"),
        "closed": item.get("closed_at"),
    }


def _comment_record(item: dict) -> dict:
    issue_url = item.get("issue_url") or ""
    tail = issue_url.rsplit("/", 1)[-1]
    return {
        "id": item.get("id"),
        "n": int(tail) if tail.isdigit() else None,
        "body": item.get("body") or "",
        "user": (item.get("user") or {}).get("login"),
        "created": item.get("created_at"),
        "updated": item.get("updated_at"),
    }


def dump_endpoint(
    fetch: Fetch,
    base: str,
    key: str,
    *,
    since: str | None = None,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> dict:
    """Every item of a list endpoint, in update-time windows, de-duplicated by *key*."""
    items: dict = {}
    errors: list[str] = []
    windows = requests = 0
    exhausted = False
    while not exhausted:
        windows += 1
        last = None
        new = 0
        pages = 0
        while pages < max_pages:
            query = f"sort=updated&direction=asc&per_page={PER_PAGE}&page={pages + 1}"
            if since:
                query += f"&since={since}"
            data, err = fetch(f"{base}{'&' if '?' in base else '?'}{query}")
            requests += 1
            pages += 1
            if err is not None or not isinstance(data, list):
                errors.append(
                    err or f"{base}: expected a list, got {type(data).__name__}"
                )
                return {
                    "items": list(items.values()),
                    "windows": windows,
                    "requests": requests,
                    "errors": errors,
                }
            for item in data:
                ident = item.get(key)
                if ident not in items:
                    new += 1
                items[ident] = item  # a later copy is the more recent one
            if data:
                last = data[-1].get("updated_at")
            if len(data) < PER_PAGE:
                exhausted = True
                break
        if exhausted:
            break
        if not new or not last or last == since:
            errors.append(
                f"{base}: window from {since} made no progress; more than "
                f"{max_pages * PER_PAGE} items share one update time"
            )
            break
        since = last
    return {
        "items": list(items.values()),
        "windows": windows,
        "requests": requests,
        "errors": errors,
    }


def dump(
    repo: str,
    out: str,
    *,
    fetch: Fetch = gh_fetch,
    since: str | None = None,
    comments: bool = True,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> dict:
    """Dump *repo*'s issues, PRs and conversation comments into *out*."""
    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    issues = dump_endpoint(
        fetch,
        f"repos/{repo}/issues?state=all",
        "number",
        since=since,
        max_pages=max_pages,
    )
    errors += issues["errors"]
    records = sorted(
        (_issue_record(i) for i in issues["items"]), key=lambda r: r["n"] or 0
    )
    _write_jsonl(out_dir / "issues.jsonl", records)
    requests = issues["requests"]

    comment_count: int | None = None
    if comments:
        result = dump_endpoint(
            fetch,
            f"repos/{repo}/issues/comments",
            "id",
            since=since,
            max_pages=max_pages,
        )
        errors += result["errors"]
        requests += result["requests"]
        rows = sorted(
            (_comment_record(c) for c in result["items"]), key=lambda r: r["id"] or 0
        )
        _write_jsonl(out_dir / "comments.jsonl", rows)
        comment_count = len(rows)

    # The search API's total is the one independent count of issues + PRs.
    api_total = None
    if not since:
        data, err = fetch(f"search/issues?q=repo:{repo}&per_page=1")
        requests += 1
        if err is None and isinstance(data, dict):
            api_total = data.get("total_count")
        else:
            errors.append(f"total not checked: {err}")
    manifest = {
        "repo": repo,
        "dumped_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "since": since,
        "issues_and_prs": len(records),
        "pull_requests": sum(1 for r in records if r["pr"]),
        "comments": comment_count,
        "api_total": api_total,
        "complete": (
            None if api_total is None or errors else len(records) >= api_total
        ),
        "windows": issues["windows"],
        "requests": requests,
        "not_covered": [
            "pull-request review comments",
            "discussions",
            "private security advisories",
        ]
        + ([] if comments else ["conversation comments (--no-comments)"]),
        "errors": errors,
        "out": str(out_dir),
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _matcher(term: str) -> re.Pattern:
    if term.startswith("~") and len(term) > 1:
        return re.compile(term[1:], re.IGNORECASE)
    return re.compile(re.escape(term.strip('"')), re.IGNORECASE)


def search(
    dump_dir: str,
    terms: list[str],
    *,
    title_only: bool = False,
    kind: str | None = None,
    state: str | None = None,
    since_year: int | None = None,
    comments: bool = True,
    limit: int = 40,
) -> dict:
    """Items of a dump in which every term matches; best first."""
    root = Path(dump_dir)
    issues = {r["n"]: {**r, "comments": []} for r in _read_jsonl(root / "issues.jsonl")}
    if comments:
        for c in _read_jsonl(root / "comments.jsonl"):
            if c.get("n") in issues:
                issues[c["n"]]["comments"].append(c.get("body") or "")
    patterns = [_matcher(t) for t in terms]
    hits = []
    for n, item in issues.items():
        if kind == "pr" and not item["pr"] or kind == "issue" and item["pr"]:
            continue
        if state and item["state"] != state:
            continue
        if since_year and int((item.get("created") or "0")[:4] or 0) < since_year:
            continue
        joined = "\n".join(item["comments"])
        where = []
        for pattern in patterns:
            w = "t" if pattern.search(item["title"]) else ""
            if not title_only:
                w += "b" if pattern.search(item["body"]) else ""
                w += "c" if joined and pattern.search(joined) else ""
            if not w:
                break
            where.append(w)
        else:
            score = sum(3 * ("t" in w) + 2 * ("b" in w) + ("c" in w) for w in where)
            hits.append((score, n, item, where))
    hits.sort(key=lambda h: (-h[0], -(h[1] or 0)))
    manifest_path = root / "manifest.json"
    manifest = (
        json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest_path.is_file()
        else {}
    )
    return {
        "dump": str(root),
        "dumped_at": manifest.get("dumped_at"),
        "complete": manifest.get("complete"),
        "terms": terms,
        "searched": len(issues),
        "matches": len(hits),
        "items": [
            {
                "n": n,
                "kind": "pr" if item["pr"] else "issue",
                "state": "merged" if item["merged"] else item["state"],
                "created": (item.get("created") or "")[:10],
                "title": item["title"],
                "where": ",".join(where),
            }
            for _, n, item, where in hits[:limit]
        ],
    }


def _parse(argv: list[str]) -> tuple[list[str], dict[str, str]]:
    flags = {"--no-comments", "--title", "--pr", "--issue", "--open", "--closed"}
    opts: dict[str, str] = {}
    positional: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in flags:
            opts[arg] = "1"
            i += 1
        elif arg.startswith("--") and i + 1 < len(argv):
            opts[arg] = argv[i + 1]
            i += 2
        else:
            positional.append(arg)
            i += 1
    return positional, opts


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    exit_on_help(argv, __doc__)
    if not argv or argv[0] not in {"dump", "search"}:
        emit({"error": "first argument must be `dump` or `search` (see --help)"})
        sys.exit(2)
    cmd, (positional, opts) = argv[0], _parse(argv[1:])
    try:
        if cmd == "dump":
            if "--repo" not in opts or "--out" not in opts:
                emit({"error": "dump needs --repo OWNER/REPO and --out DIR"})
                sys.exit(2)
            result = dump(
                opts["--repo"],
                opts["--out"],
                since=opts.get("--since"),
                comments="--no-comments" not in opts,
                max_pages=int(opts.get("--max-pages", DEFAULT_MAX_PAGES)),
            )
            emit(result)
            sys.exit(1 if result["errors"] else 0)
        if len(positional) < 2:
            emit({"error": "search needs a dump directory and at least one term"})
            sys.exit(2)
        kind = "pr" if "--pr" in opts else ("issue" if "--issue" in opts else None)
        state = (
            "open" if "--open" in opts else ("closed" if "--closed" in opts else None)
        )
        emit(
            search(
                positional[0],
                positional[1:],
                title_only="--title" in opts,
                kind=kind,
                state=state,
                since_year=int(opts["--since"]) if "--since" in opts else None,
                comments="--no-comments" not in opts,
                limit=int(opts.get("--limit", 40)),
            )
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        emit({"error": str(exc)})
        sys.exit(2)


if __name__ == "__main__":
    main()
