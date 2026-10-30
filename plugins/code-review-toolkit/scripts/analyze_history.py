#!/usr/bin/env python3
"""Analyze git history for churn metrics, commit classification, and co-change data.

Queries git history and produces structured JSON with file/function churn,
commit classification, recent fixes/features/refactors, and co-change clusters.

Usage:
    python analyze_history.py [path] [options]

Options:
    --days N          Analyze last N days (default: 90)
    --since DATE      Start date (ISO format, overrides --days)
    --until DATE      End date (ISO format, default: today)
    --last N          Analyze exactly the last N commits (overrides time range)
    --max-commits N   Cap total commits analyzed (default: 2000)
    --no-function     Skip function-level churn (file-level only, faster)
    --no-branches     Skip the unmerged remote branch listing
    --key-type SPEC   Map tracker-key prefixes to commit types, e.g.
                      B=fix,F=feature (repeatable)
"""

import ast
import json
import re
import subprocess
import sys
import time
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scan_common import (  # noqa: E402
    find_project_root,
)

# Keyword fallback for commit classification -- first CATEGORY wins. Each entry
# is a regex fragment matched on word boundaries with its inflections spelled
# out. Plain substring matching was wrong in both directions on a validation
# project:
# "prefixes" counted as a fix, "policies" as a chore (it contains "ci").
CLASSIFICATION_RULES: list[tuple[str, list[str]]] = [
    (
        "fix",
        [
            "fix(?:es|ed|ing)?",
            "bugs?",
            "bugfix(?:es)?",
            "hotfix(?:es)?",
            "patch(?:es|ed)?",
            "resolve[sd]?",
            "issues?",
            "crash(?:es|ed|ing)?",
            "errors?",
            "broken",
            "repair(?:s|ed)?",
            "correct(?:s|ed|ion)?",
            "regressions?",
            "workarounds?",
            "wrong(?:ly)?",
            "incorrect(?:ly)?",
            "leak(?:s|ed|ing)?",
            "races?",
            "segfaults?",
            "fail(?:s|ed|ing|ure|ures)?",
        ],
    ),
    (
        "docs",
        [
            "docs?",
            "readme",
            "comments?",
            "typos?",
            "spelling",
            "changelog",
            "documentation",
            "document(?:s|ed)?",
        ],
    ),
    (
        "test",
        [
            "tests?",
            "testing",
            "coverage",
            "asserts?",
            "mocks?",
            "fixtures?",
            "unittests?",
            "pytest",
        ],
    ),
    (
        "refactor",
        [
            "refactor(?:s|ed|ing)?",
            "clean(?:s|ed|up)?",
            "cleanups?",
            "simplif(?:y|ies|ied)",
            "reorgani[sz](?:e|es|ed)",
            "restructur(?:e|es|ed)",
            "renam(?:e|es|ed)",
            "mov(?:e|es|ed)",
            "extract(?:s|ed)?",
            "deduplicat(?:e|es|ed)",
            "inline[sd]?",
        ],
    ),
    (
        "chore",
        [
            "bump(?:s|ed)?",
            "dependenc(?:y|ies)",
            "update[sd]?",
            "upgrade[sd]?",
            "ci",
            "config",
            "lint",
            "format",
            "version",
            "release",
            "merge",
            "revert(?:s|ed)?",
        ],
    ),
    (
        "feature",
        [
            "add(?:s|ed)?",
            "implement(?:s|ed)?",
            "new",
            "features?",
            "introduc(?:e|es|ed)",
            "support(?:s|ed)?",
            "enable[sd]?",
            "create[sd]?",
        ],
    ),
]

_KEYWORD_RES: list[tuple[str, re.Pattern[str]]] = [
    (category, re.compile(r"\b(?:" + "|".join(words) + r")\b", re.IGNORECASE))
    for category, words in CLASSIFICATION_RULES
]

COMMIT_TYPES = ("fix", "feature", "refactor", "docs", "test", "chore")

# A Conventional Commits type, or a leading bracketed tag, states the type
# outright. Only tags that NAME a type are mapped; area tags such as `[store]`
# or `[api]` carry no type and fall through to the next signal. `perf` is left
# unmapped on purpose: a performance commit is as often new capability as it
# is a rewrite, so the remaining signals decide.
_TYPE_WORDS: dict[str, str] = {
    "fix": "fix",
    "fixes": "fix",
    "bug": "fix",
    "bugfix": "fix",
    "hotfix": "fix",
    "security": "fix",
    "sec": "fix",
    "regression": "fix",
    "feat": "feature",
    "feature": "feature",
    "doc": "docs",
    "docs": "docs",
    "documentation": "docs",
    "test": "test",
    "tests": "test",
    "testing": "test",
    "bench": "test",
    "refactor": "refactor",
    "cleanup": "refactor",
    "chore": "chore",
    "build": "chore",
    "ci": "chore",
    "deps": "chore",
    "release": "chore",
    "style": "chore",
    "revert": "chore",
    "infra": "chore",
    "pkg": "chore",
}

_CONVENTIONAL_RE = re.compile(r"^\s*([A-Za-z]+)(?:\([^)]*\))?!?:\s")
_LEADING_TAGS_RE = re.compile(r"^\s*((?:\[[^\[\]\n]+\]\s*)+)")
_TAG_RE = re.compile(r"\[([^\[\]]+)\]")
# Tracker keys: `ABC-123` (Jira style) or a single capital followed by digits,
# optionally dash-separated (`K7`, `T1-20`).
_TRACKER_KEY_RE = re.compile(
    r"(?<![\w-])([A-Z][A-Z0-9]*-\d+(?:-\d+)*|[A-Z]\d+(?:-\d+)*)(?![\w-])"
)

# Precedence when one commit carries several typed signals of equal rank.
_TYPE_PRIORITY = {t: i for i, t in enumerate(COMMIT_TYPES)}

_GIT_TIMEOUT = 30
_SCRIPT_START: float = 0.0
_SCRIPT_TIMEOUT = 300  # 5 minutes
_MAX_DIFF_LINES_FIX = 150
_MAX_DIFF_LINES_REFACTOR = 80
_FUNCTION_COUNT_THRESHOLD = 500
# Each historical file version is parsed once to find its functions. This caps
# the parses per run; past it, the remaining commits are left unattributed and
# the output says so.
_MAX_BLOB_PARSES = 4000
_MAX_UNMERGED_BRANCHES = 30
_MAX_UNMERGED_COMMITS = 20
_MAX_UNMERGED_FILES = 30


def tracker_key_prefix(key: str) -> str:
    """`T1-20` -> `T`, `ABC-12` -> `ABC`."""
    match = re.match(r"[A-Z]+", key)
    return match.group(0) if match else key


def parse_key_types(spec: str) -> dict[str, str]:
    """Parse `B=fix,F=feature` into {prefix: type}. Raises ValueError."""
    result: dict[str, str] = {}
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        prefix, sep, ctype = item.partition("=")
        prefix, ctype = prefix.strip(), ctype.strip().lower()
        ctype = _TYPE_WORDS.get(ctype, ctype)
        if not sep or not re.fullmatch(r"[A-Z]+", prefix) or ctype not in COMMIT_TYPES:
            raise ValueError(
                f"bad --key-type entry {item!r}: expected PREFIX=TYPE with an "
                f"uppercase prefix and TYPE one of {', '.join(COMMIT_TYPES)}"
            )
        result[prefix] = ctype
    return result


def _best_type(types: Iterable[str]) -> str | None:
    found = [t for t in types if t in _TYPE_PRIORITY]
    return min(found, key=_TYPE_PRIORITY.__getitem__) if found else None


def classify_commit_detail(
    message: str,
    key_types: dict[str, str] | None = None,
) -> dict:
    """Classify a commit subject and say which signal decided it.

    Signals, strongest first: a Conventional Commits type (`fix(cli): ...`), a
    leading bracketed tag that names a type (`[fix][cli] ...`), a tracker key
    whose prefix is mapped by `key_types` (`... (K7)` with `K=fix`), and last
    a word-boundary keyword match. Returns
    {"type", "source", "tags", "keys"}; source is one of conventional / tag /
    key / keyword / None.
    """
    tags: list[str] = []
    tag_match = _LEADING_TAGS_RE.match(message)
    if tag_match:
        for raw in _TAG_RE.findall(tag_match.group(1)):
            tags.extend(t for t in re.split(r"[\s,/|]+", raw.strip().lower()) if t)
    keys = list(dict.fromkeys(_TRACKER_KEY_RE.findall(message)))
    detail: dict = {"type": "unknown", "source": None, "tags": tags, "keys": keys}

    conv = _CONVENTIONAL_RE.match(message)
    if conv and conv.group(1).lower() in _TYPE_WORDS:
        detail.update(type=_TYPE_WORDS[conv.group(1).lower()], source="conventional")
        return detail
    tag_type = _best_type(_TYPE_WORDS.get(t, "") for t in tags)
    if tag_type:
        detail.update(type=tag_type, source="tag")
        return detail
    if key_types:
        key_type = _best_type(key_types.get(tracker_key_prefix(k), "") for k in keys)
        if key_type:
            detail.update(type=key_type, source="key")
            return detail
    for category, pattern in _KEYWORD_RES:
        if pattern.search(message):
            detail.update(type=category, source="keyword")
            return detail
    return detail


def classify_commit(message: str, key_types: dict[str, str] | None = None) -> str:
    """Classify a commit message. See classify_commit_detail for the rules."""
    return classify_commit_detail(message, key_types)["type"]


def _run_git(
    args: list[str],
    cwd: Path,
    timeout: int = _GIT_TIMEOUT,
) -> subprocess.CompletedProcess[str]:
    """Run a git command with timeout handling."""
    # errors="replace": commit messages and diffs are not guaranteed UTF-8.
    # Under the default strict decoding a single pre-UTF-8 commit anywhere in
    # the window raises UnicodeDecodeError and takes down the entire analysis
    # (observed on CPython: one 2015 commit killed a 9,203-commit run). A
    # mangled character in one old subject line is the better trade.
    return subprocess.run(
        ["git"] + args,
        capture_output=True,
        text=True,
        errors="replace",
        cwd=str(cwd),
        timeout=timeout,
    )


def _run_git_streaming(
    args: list[str],
    cwd: Path,
) -> subprocess.Popen[str]:
    """Run a git command with streaming stdout.

    stderr goes to DEVNULL rather than a pipe. Nothing reads this process's
    stderr, and an undrained pipe deadlocks once git writes more than the pipe
    buffer to it -- git blocks writing stderr while we block reading stdout.
    """
    # See _run_git: a non-UTF-8 commit message must not kill the stream.
    return subprocess.Popen(
        ["git"] + args,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        errors="replace",
        cwd=str(cwd),
    )


def _is_git_repo(path: Path) -> bool:
    """Check if path is inside a git repository."""
    try:
        result = _run_git(["rev-parse", "--is-inside-work-tree"], path, timeout=5)
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _check_script_timeout() -> bool:
    """Return True if the script has exceeded its timeout.

    The clock starts in `analyze()`. Called as a library, before or without
    `analyze()`, there is no budget to exceed: comparing against an unset
    start of 0.0 made every check report a timeout, so `find_unmerged_branches`
    returned no branches at all.
    """
    if not _SCRIPT_START:
        return False
    return (time.monotonic() - _SCRIPT_START) > _SCRIPT_TIMEOUT


_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T")

# `git log` record header: hash, author date, committer date, author, subject.
LOG_FORMAT = "COMMIT:%H|%aI|%cI|%an|%s"


def _parse_commit_header(payload: str) -> tuple[str, str, str, str, str] | None:
    """Split a COMMIT: header into (hash, author_date, committer_date, author, subject).

    Also accepts the older four-field form (no committer date), in which case
    the author date stands in for both.
    """
    parts = payload.split("|", 4)
    if len(parts) == 5 and _ISO_DATE_RE.match(parts[2]):
        return parts[0], parts[1], parts[2], parts[3], parts[4]
    parts = payload.split("|", 3)
    if len(parts) < 4:
        return None
    return parts[0], parts[1], parts[1], parts[2], parts[3]


def parse_git_log(
    lines: Iterable[str],
    max_commits: int,
    project_root: Path | None = None,
    key_types: dict[str, str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Parse git log with numstat from an iterable of lines.

    Returns (commits, file_stats) where commits is a list of commit dicts
    and file_stats is accumulated per-file change data.

    The window is selected with `--since`/`--until`, which git applies to the
    COMMITTER date, so the per-file first/last dates use the committer date
    too. Using the author date put `first_commit_in_range` a month before the
    window's start on a validation project (a commit authored five weeks
    before it was committed).
    """
    commits: list[dict] = []
    file_changes: dict[str, dict] = {}
    current_commit: dict | None = None
    commit_count = 0

    for line in lines:
        line = line.rstrip("\n")
        if line.startswith("COMMIT:"):
            if current_commit is not None:
                commits.append(current_commit)
            commit_count += 1
            if commit_count > max_commits:
                break
            header = _parse_commit_header(line[7:])
            if header is None:
                current_commit = None
                continue
            commit_hash, author_date, date_str, author, message = header
            detail = classify_commit_detail(message, key_types)
            current_commit = {
                "hash": commit_hash,
                "date": author_date,
                "committer_date": date_str,
                "author": author,
                "message": message,
                "type": detail["type"],
                "type_source": detail["source"],
                "keys": detail["keys"],
                "files": [],
                "stats": [],
            }
        elif line.strip() and current_commit is not None:
            parts = line.split("\t", 2)
            if len(parts) == 3:
                added_str, removed_str, filepath = parts
                try:
                    added = int(added_str) if added_str != "-" else 0
                    removed = int(removed_str) if removed_str != "-" else 0
                except ValueError:
                    continue
                current_commit["files"].append(filepath)
                current_commit["stats"].append(
                    {
                        "file": filepath,
                        "added": added,
                        "removed": removed,
                    }
                )
                if filepath not in file_changes:
                    file_changes[filepath] = {
                        "commits": 0,
                        "lines_added": 0,
                        "lines_removed": 0,
                        "authors": set(),
                        "first_date": date_str,
                        "last_date": date_str,
                    }
                fc = file_changes[filepath]
                fc["commits"] += 1
                fc["lines_added"] += added
                fc["lines_removed"] += removed
                fc["authors"].add(author)
                # Track date range.
                if date_str < fc["first_date"]:
                    fc["first_date"] = date_str
                if date_str > fc["last_date"]:
                    fc["last_date"] = date_str

    if current_commit is not None and commit_count <= max_commits:
        commits.append(current_commit)

    # Build file_stats list.
    file_stats: list[dict] = []
    for filepath, fc in file_changes.items():
        if project_root is not None:
            line_count = _get_file_line_count(project_root / filepath)
        else:
            line_count = 0
        churn_rate = (
            round((fc["lines_added"] + fc["lines_removed"]) / line_count, 2)
            if line_count > 0
            else 0.0
        )
        file_stats.append(
            {
                "file": filepath,
                "commits": fc["commits"],
                "lines_added": fc["lines_added"],
                "lines_removed": fc["lines_removed"],
                "churn_rate": churn_rate,
                "authors": len(fc["authors"]),
                # Committer dates: the same clock the window was selected on.
                "first_commit_in_range": fc["first_date"],
                "last_modified": fc["last_date"],
            }
        )

    # Sort by commits descending.
    file_stats.sort(key=lambda x: x["commits"], reverse=True)
    return commits, file_stats


def _get_file_line_count(filepath: Path) -> int:
    """Count lines in a file, returning 0 if file doesn't exist."""
    try:
        return len(filepath.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return 0


def _relative_scope(scan_root: Path, project_root: Path) -> str:
    """Get the relative path of scan_root from project_root."""
    try:
        rel = scan_root.resolve().relative_to(project_root.resolve())
        return str(rel) if str(rel) != "." else "."
    except ValueError:
        return "."


def iter_qualified_defs(tree: ast.AST):
    """Yield (qualname, node, enclosing_kinds) for every function at any depth.

    The qualname mirrors `__qualname__` without `<locals>`: `Class.method`,
    `outer.inner`, `factory.Handler.get`. enclosing_kinds lists "class" /
    "function" for each enclosing scope, outermost first. measure_complexity
    uses the same names, so the two scripts' function keys agree.
    """
    stack: list[tuple[ast.AST, str, tuple[str, ...]]] = [(tree, "", ())]
    while stack:
        node, prefix, kinds = stack.pop()
        found: list[tuple[ast.AST, str, tuple[str, ...]]] = []
        todo = list(ast.iter_child_nodes(node))
        while todo:
            child = todo.pop(0)
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qual = prefix + child.name
                yield qual, child, kinds
                found.append((child, qual + ".", kinds + ("function",)))
            elif isinstance(child, ast.ClassDef):
                found.append((child, prefix + child.name + ".", kinds + ("class",)))
            else:
                # Defs inside if/try/with/for blocks belong to this scope.
                todo[0:0] = list(ast.iter_child_nodes(child))
        stack.extend(reversed(found))


def def_start_line(node: ast.AST) -> int:
    """First line of a def, counting its decorators."""
    decorators = getattr(node, "decorator_list", [])
    return min([d.lineno for d in decorators] + [node.lineno])


def function_spans(source: str) -> list[tuple[str, int, int]] | None:
    """(qualname, first_line, last_line) for every function; None if unparseable."""
    try:
        tree = ast.parse(source.removeprefix("﻿"))
    except (SyntaxError, ValueError):
        return None
    return [
        (qual, def_start_line(node), getattr(node, "end_lineno", None) or node.lineno)
        for qual, node, _ in iter_qualified_defs(tree)
    ]


def get_function_boundaries(filepath: Path) -> list[dict]:
    """Parse a Python file with AST and return function/method boundaries."""
    try:
        source = filepath.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    spans = function_spans(source)
    if spans is None:
        return []
    return [
        {
            "name": qual.rsplit(".", 1)[-1],
            "qualname": qual,
            "line_start": start,
            "line_end": end,
        }
        for qual, start, end in spans
    ]


class _FileVersion:
    """One historical version of a file: its functions and line ownership.

    `owners[line]` is the index into `names` of the INNERMOST function
    containing that line, or -1. A change inside a nested route handler is
    credited to the handler, not to the router factory around it.
    """

    __slots__ = ("names", "owners", "spans")

    def __init__(self, spans: list[tuple[str, int, int]]) -> None:
        self.spans = spans
        self.names = [qual for qual, _, _ in spans]
        last = max((end for _, _, end in spans), default=0)
        self.owners = [-1] * (last + 2)
        # Outer spans first, so a nested span overwrites its parent's lines.
        for idx in sorted(range(len(spans)), key=lambda i: (spans[i][1], -spans[i][2])):
            _, start, end = spans[idx]
            for line in range(start, end + 1):
                self.owners[line] = idx

    def owner(self, line: int) -> str | None:
        if 0 <= line < len(self.owners) and self.owners[line] >= 0:
            return self.names[self.owners[line]]
        return None


class BlobFunctionIndex:
    """Parse historical file versions on demand, via one `git cat-file --batch`.

    Versions are keyed by blob id, so a version shared by consecutive commits
    (the post-image of one is the pre-image of the next) is parsed once.
    """

    def __init__(self, git_root: Path, max_parses: int | None = None) -> None:
        self._git_root = git_root
        self._proc: subprocess.Popen[bytes] | None = None
        self._cache: dict[str, _FileVersion | None] = {}
        self.max_parses = _MAX_BLOB_PARSES if max_parses is None else max_parses
        self.parses = 0
        self.unparseable = 0
        self.unreadable = 0
        self.broken = False

    @property
    def exhausted(self) -> bool:
        return self.broken or self.parses >= self.max_parses

    def _read_blob(self, blob: str) -> str | None:
        if self._proc is None:
            try:
                self._proc = subprocess.Popen(
                    ["git", "cat-file", "--batch"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    cwd=str(self._git_root),
                )
            except OSError:
                self.broken = True
                return None
        proc = self._proc
        assert proc.stdin is not None and proc.stdout is not None
        try:
            proc.stdin.write(blob.encode("ascii") + b"\n")
            proc.stdin.flush()
            header = proc.stdout.readline()
            parts = header.split()
            if len(parts) != 3:
                if not header:
                    self.broken = True
                return None
            size = int(parts[2])
            data = proc.stdout.read(size)
            proc.stdout.read(1)  # trailing newline
        except (OSError, ValueError):
            self.broken = True
            return None
        return data.decode("utf-8", errors="replace")

    def get(self, blob: str | None) -> _FileVersion | None:
        if not blob or not blob.strip("0"):
            return None  # absent side of an add/delete
        if blob in self._cache:
            return self._cache[blob]
        if self.exhausted:
            return None
        source = self._read_blob(blob)
        version: _FileVersion | None = None
        if source is None:
            self.unreadable += 1
        else:
            self.parses += 1
            spans = function_spans(source)
            if spans is None:
                self.unparseable += 1
            else:
                version = _FileVersion(spans)
        if len(self._cache) > 512:
            self._cache.clear()
        self._cache[blob] = version
        return version

    def close(self) -> None:
        if self._proc is not None:
            try:
                if self._proc.stdin:
                    self._proc.stdin.close()
                self._proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                self._proc.kill()
            if self._proc.stdout:
                self._proc.stdout.close()
            self._proc = None

    def __enter__(self):  # unannotated: typing.Self needs 3.11
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# Diff options that keep the output parseable whatever the user's git config
# says (color.ui=always, diff.noprefix, an external diff driver, quoted paths).
DIFF_OPTIONS = [
    "--no-color",
    "--no-ext-diff",
    "--full-index",
    "-U0",
    "--src-prefix=a/",
    "--dst-prefix=b/",
]
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_INDEX_RE = re.compile(r"^index ([0-9a-f]+)\.\.([0-9a-f]+)")


def parse_unified_diff(lines: Iterable[str]) -> list[dict]:
    """Parse `-U0 --full-index` diff text into per-file hunk lists.

    Each entry: {"path", "old_path", "old_blob", "new_blob", "hunks"}, where
    hunks are (old_start, old_count, new_start, new_count). `path` is the
    post-image path (the pre-image path for a deletion).
    """
    files: list[dict] = []
    current: dict | None = None
    in_header = False
    for line in lines:
        line = line.rstrip("\n")
        if line.startswith("diff --git "):
            current = {
                "path": None,
                "old_path": None,
                "old_blob": None,
                "new_blob": None,
                "hunks": [],
            }
            files.append(current)
            in_header = True
            continue
        if current is None:
            continue
        if in_header:
            index = _INDEX_RE.match(line)
            if index:
                current["old_blob"], current["new_blob"] = index.groups()
            elif line.startswith("--- "):
                current["old_path"] = line[6:] if line.startswith("--- a/") else None
            elif line.startswith("+++ "):
                current["path"] = line[6:] if line.startswith("+++ b/") else None
        hunk = _HUNK_RE.match(line)
        if hunk:
            in_header = False
            old_start, old_count, new_start, new_count = hunk.groups()
            current["hunks"].append(
                (
                    int(old_start),
                    1 if old_count is None else int(old_count),
                    int(new_start),
                    1 if new_count is None else int(new_count),
                )
            )
    for entry in files:
        if entry["path"] is None:
            entry["path"] = entry["old_path"]
    return [f for f in files if f["path"]]


def attribute_file_diff(
    file_diff: dict,
    index: BlobFunctionIndex,
) -> dict[str, list[int]] | None:
    """Map one file's hunks onto the functions they touched AT THAT COMMIT.

    Added lines are looked up in the post-image, removed lines in the
    pre-image, each parsed from the blob the diff names. Returns
    {qualname: [lines_added, lines_removed]}, or None when a version the hunks
    need could not be parsed (so the caller can count it as unattributed
    rather than as "touched no function").
    """
    hunks = file_diff["hunks"]
    need_new = any(nc for _, _, _, nc in hunks)
    need_old = any(oc for _, oc, _, _ in hunks)
    post = index.get(file_diff["new_blob"]) if need_new else None
    pre = index.get(file_diff["old_blob"]) if need_old else None
    has_new = bool(file_diff["new_blob"] and file_diff["new_blob"].strip("0"))
    has_old = bool(file_diff["old_blob"] and file_diff["old_blob"].strip("0"))
    if (need_new and has_new and post is None) or (
        need_old and has_old and pre is None
    ):
        return None
    touched: dict[str, list[int]] = {}
    for old_start, old_count, new_start, new_count in hunks:
        if post is not None:
            for line in range(new_start, new_start + new_count):
                name = post.owner(line)
                if name:
                    touched.setdefault(name, [0, 0])[0] += 1
        if pre is not None:
            for line in range(old_start, old_start + old_count):
                name = pre.owner(line)
                if name:
                    touched.setdefault(name, [0, 0])[1] += 1
    return touched


def git_toplevel(path: Path) -> Path:
    """The repository's top-level directory (diff paths are relative to it)."""
    try:
        result = _run_git(["rev-parse", "--show-toplevel"], path, timeout=10)
    except (subprocess.TimeoutExpired, OSError):
        return path
    if result.returncode != 0 or not result.stdout.strip():
        return path
    return Path(result.stdout.strip()).resolve()


def attribute_commit(
    commit_hash: str,
    paths: list[str],
    git_root: Path,
    index: BlobFunctionIndex,
) -> dict[str, dict[str, list[int]] | None] | None:
    """Per path, {qualname: [added, removed]} for one commit (None per path
    that could not be attributed). Returns None if `git show` failed."""
    if not paths:
        return {}
    wanted = set(paths)
    # A huge commit would overflow the argument list; filter afterwards instead.
    pathspec = paths if len(paths) <= 200 else []
    try:
        result = _run_git(
            [
                "-c",
                "core.quotepath=off",
                "show",
                "--format=",
                *DIFF_OPTIONS,
                commit_hash,
                "--",
                *pathspec,
            ],
            git_root,
        )
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    per_path: dict[str, dict[str, list[int]] | None] = {}
    for file_diff in parse_unified_diff(result.stdout.splitlines()):
        if file_diff["path"] in wanted:
            per_path[file_diff["path"]] = attribute_file_diff(file_diff, index)
    return per_path


class CommitAttributor:
    """Lazily attribute commits to functions, caching per commit."""

    def __init__(self, git_root: Path) -> None:
        self.git_root = git_root
        self.index = BlobFunctionIndex(git_root)
        self._cache: dict[str, dict[str, dict[str, list[int]] | None] | None] = {}
        self.commits_attributed = 0
        self.commits_skipped = 0
        self.files_unattributed = 0

    def get(self, commit: dict) -> dict[str, dict[str, list[int]] | None] | None:
        sha = commit["hash"]
        if sha in self._cache:
            return self._cache[sha]
        paths = [f for f in commit["files"] if f.endswith(".py")]
        if not paths:
            self._cache[sha] = {}
            return self._cache[sha]
        if self.index.exhausted or _check_script_timeout():
            self.commits_skipped += 1
            return None
        attr = attribute_commit(sha, paths, self.git_root, self.index)
        if attr is None:
            self.commits_skipped += 1
        else:
            self.commits_attributed += 1
            self.files_unattributed += sum(1 for v in attr.values() if v is None)
        self._cache[sha] = attr
        return attr

    def meta(self) -> dict:
        return {
            "method": (
                "each diff hunk is mapped onto the function that contained "
                "those lines in that commit's own version of the file "
                "(post-image for added lines, pre-image for removed lines); "
                "a nested function is credited instead of its enclosing one"
            ),
            "commits_attributed": self.commits_attributed,
            "commits_unattributed": self.commits_skipped,
            "file_versions_parsed": self.index.parses,
            "file_versions_unparseable": self.index.unparseable,
            "files_unattributed": self.files_unattributed,
            "parse_cap": self.index.max_parses,
            "parse_cap_reached": self.index.parses >= self.index.max_parses,
        }

    def close(self) -> None:
        self.index.close()


def compute_function_churn_level2(
    commits: list[dict],
    scan_root: Path,
    project_root: Path,
    max_files: int = 0,
    attributor: CommitAttributor | None = None,
) -> list[dict]:
    """Level 2: map each commit's hunks onto the functions of THAT commit.

    The old implementation mapped every historical hunk onto function
    boundaries parsed from HEAD. On a file that more than doubled in the
    window, old hunks landed in unrelated functions: one method was credited
    with 9 commits instead of 1, and the most-changed one was under-counted.
    """
    git_root = attributor.git_root if attributor else git_toplevel(project_root)
    owned = attributor is None
    if attributor is None:
        attributor = CommitAttributor(git_root)
    try:
        return _function_churn(commits, scan_root, git_root, max_files, attributor)
    finally:
        if owned:
            attributor.close()


def _function_churn(
    commits: list[dict],
    scan_root: Path,
    git_root: Path,
    max_files: int,
    attributor: CommitAttributor,
) -> list[dict]:
    # The in-scope Python files that exist at HEAD, keyed by repo path.
    if scan_root.is_file():
        py_files = [scan_root] if scan_root.suffix == ".py" else []
    else:
        py_files = sorted(scan_root.rglob("*.py"))

    exclude = {
        ".git",
        ".tox",
        ".venv",
        "venv",
        "__pycache__",
        "node_modules",
        ".eggs",
        "build",
        "dist",
    }
    allowed: list[str] = []
    for pf in py_files:
        try:
            parts = pf.relative_to(git_root).parts
        except ValueError:
            continue
        if set(parts) & exclude or any(p.endswith(".egg-info") for p in parts):
            continue
        allowed.append(pf.relative_to(git_root).as_posix())

    if max_files > 0 and len(allowed) > max_files:
        allowed = allowed[:max_files]
    allowed_set = set(allowed)
    if not allowed_set:
        return []

    # (file, qualname) -> commits, lines added, lines removed.
    func_commits: dict[tuple[str, str], set[str]] = defaultdict(set)
    func_lines: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])

    for commit in commits:
        if not any(f in allowed_set for f in commit["files"]):
            continue
        attr = attributor.get(commit)
        if not attr:
            continue
        for file_path, touched in attr.items():
            if file_path not in allowed_set or not touched:
                continue
            for qual, (added, removed) in touched.items():
                key = (file_path, qual)
                func_commits[key].add(commit["hash"])
                func_lines[key][0] += added
                func_lines[key][1] += removed

    # Where each function is today, for the files that appear in the result.
    head_spans: dict[str, dict[str, tuple[int, int]]] = {}
    for file_path in {fp for fp, _ in func_commits}:
        spans = get_function_boundaries(git_root / file_path)
        head_spans[file_path] = {
            b["qualname"]: (b["line_start"], b["line_end"]) for b in spans
        }

    results: list[dict] = []
    for (file_path, qual), commit_hashes in func_commits.items():
        added, removed = func_lines[(file_path, qual)]
        span = head_spans.get(file_path, {}).get(qual)
        results.append(
            {
                "function": qual,
                "file": file_path,
                "line_start": span[0] if span else None,
                "line_end": span[1] if span else None,
                "exists_at_head": span is not None,
                "commits": len(commit_hashes),
                # Lines added plus lines removed inside this function, summed
                # over the window. (Until 1.14 this field held the function's
                # current length; that is now `current_length`.)
                "lines_changed": added + removed,
                "lines_added": added,
                "lines_removed": removed,
                "current_length": span[1] - span[0] + 1 if span else None,
            }
        )

    results.sort(
        key=lambda x: (-x["commits"], -x["lines_changed"], x["file"], x["function"])
    )
    return results


def _truncate_diff(diff_text: str, max_lines: int) -> str:
    """Truncate a diff to max_lines, adding a notice if truncated."""
    lines = diff_text.splitlines()
    if len(lines) <= max_lines:
        return diff_text
    truncated = "\n".join(lines[:max_lines])
    return truncated + "\n[diff truncated, full diff available via git show HASH]"


def get_commit_details(
    commits: list[dict],
    commit_type: str,
    project_root: Path,
    scan_root: Path,
    max_diff_lines: int,
    attributor: CommitAttributor | None = None,
) -> list[dict]:
    """Get detailed info for commits of a specific type.

    `functions_modified` names the functions each commit touched in its own
    version of each file (see CommitAttributor), so a function that has since
    moved or been deleted is still named, and an unrelated function now
    sitting at the old line numbers is not. `functions_modified_complete` is
    False when a file could not be attributed (unparseable at that commit, or
    the attribution budget ran out).
    """
    typed_commits = [c for c in commits if c["type"] == commit_type]
    results: list[dict] = []

    rel_scope = _relative_scope(scan_root, project_root)
    owned = attributor is None
    if attributor is None:
        attributor = CommitAttributor(git_toplevel(project_root))

    try:
        for commit in typed_commits:
            if _check_script_timeout():
                break
            # Get the diff.
            diff_args = [
                "show",
                "--format=",
                "--patch",
                "--no-color",
                "--no-ext-diff",
                commit["hash"],
                "--",
            ]
            if rel_scope != ".":
                diff_args.append(rel_scope)
            try:
                diff_result = _run_git(diff_args, project_root)
                diff_text = diff_result.stdout if diff_result.returncode == 0 else ""
            except subprocess.TimeoutExpired:
                diff_text = "[diff unavailable: git show timed out]"

            diff_text = _truncate_diff(diff_text, max_diff_lines)

            functions_modified: list[dict] = []
            attr = attributor.get(commit)
            complete = attr is not None
            for file_path, touched in (attr or {}).items():
                if touched is None:
                    complete = False
                    continue
                for qual in touched:
                    functions_modified.append({"function": qual, "file": file_path})

            results.append(
                {
                    "commit": commit["hash"],
                    "commit_short": commit["hash"][:7],
                    "message": commit["message"],
                    "date": commit["date"],
                    "committer_date": commit.get("committer_date", commit["date"]),
                    "author": commit["author"],
                    "type_source": commit.get("type_source"),
                    "keys": commit.get("keys", []),
                    "files": commit["files"],
                    "functions_modified": functions_modified,
                    "functions_modified_complete": complete,
                    "diff": diff_text,
                }
            )
    finally:
        if owned:
            attributor.close()

    return results


def compute_co_change_clusters(
    commits: list[dict],
    min_co_changes: int = 3,
    max_pairs: int = 30,
) -> list[dict]:
    """Detect file pairs that frequently change together."""
    # Count per-file commits.
    file_commit_counts: dict[str, int] = defaultdict(int)
    # Count co-occurrences.
    co_changes: dict[tuple[str, str], int] = defaultdict(int)

    for commit in commits:
        files = sorted(set(commit["files"]))
        for f in files:
            file_commit_counts[f] += 1
        for i in range(len(files)):
            for j in range(i + 1, len(files)):
                pair = (files[i], files[j])
                co_changes[pair] += 1

    # Filter and sort.
    results: list[dict] = []
    for (file_a, file_b), count in co_changes.items():
        if count >= min_co_changes:
            results.append(
                {
                    "file_a": file_a,
                    "file_b": file_b,
                    "co_change_count": count,
                    "total_commits_a": file_commit_counts[file_a],
                    "total_commits_b": file_commit_counts[file_b],
                }
            )

    results.sort(key=lambda x: x["co_change_count"], reverse=True)
    return results[:max_pairs]


def summarize_tracker_keys(commits: list[dict], max_repeated: int = 30) -> dict:
    """Group commits by tracker key; flag keys that recur across commits.

    A bug key on several commits is either a multi-part fix or a fix that had
    to be redone -- both worth a look when checking fix completeness.
    """
    by_key: dict[str, list[dict]] = defaultdict(list)
    for commit in commits:
        for key in commit.get("keys", []):
            by_key[key].append(commit)
    by_prefix: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for key, key_commits in by_key.items():
        for commit in key_commits:
            by_prefix[tracker_key_prefix(key)][commit["type"]] += 1
    repeated = [
        {
            "key": key,
            "commits": len(key_commits),
            "types": sorted({c["type"] for c in key_commits}),
            "commit_shorts": [c["hash"][:7] for c in key_commits],
        }
        for key, key_commits in by_key.items()
        if len(key_commits) > 1
    ]
    repeated.sort(key=lambda r: (-r["commits"], r["key"]))
    return {
        "keys_seen": len(by_key),
        "commits_with_keys": sum(1 for c in commits if c.get("keys")),
        # Per key prefix, how the carrying commits were classified. A prefix
        # whose commits are mostly `unknown` is a candidate for --key-type.
        "by_prefix": {p: dict(t) for p, t in sorted(by_prefix.items())},
        "repeated": repeated[:max_repeated],
        "repeated_total": len(repeated),
    }


def _default_branch(git_root: Path) -> str | None:
    """The ref unmerged work is measured against.

    `origin/HEAD` when set, else the first of origin/main, origin/master,
    main, master that exists. None if none does.
    """
    try:
        result = _run_git(
            ["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
            git_root,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        for candidate in ("origin/main", "origin/master", "main", "master"):
            result = _run_git(
                ["rev-parse", "--verify", "--quiet", candidate + "^{commit}"],
                git_root,
                timeout=10,
            )
            if result.returncode == 0:
                return candidate
    except (subprocess.TimeoutExpired, OSError):
        return None
    return None


def find_unmerged_branches(
    git_root: Path,
    rel_scope: str = ".",
    key_types: dict[str, str] | None = None,
    max_branches: int = _MAX_UNMERGED_BRANCHES,
    max_commits: int = _MAX_UNMERGED_COMMITS,
) -> dict:
    """Remote branches with commits the default branch does not have.

    Unmerged work is prior art: on one validation project the most
    consequential fact for the review -- a branch already fixing several
    findings -- lived only on an unmerged remote branch. Read-only: this never
    fetches, so it reflects the remote-tracking refs as of the last fetch.
    """
    base = _default_branch(git_root)
    if base is None:
        return {
            "available": False,
            "reason": "no default branch found "
            "(no origin/HEAD, origin/main, origin/master, main or master)",
            "branches": [],
        }
    try:
        refs = _run_git(
            [
                "for-each-ref",
                "--sort=-committerdate",
                "--format=%(refname:short)%00%(symref)%00%(committerdate:iso-strict)",
                "refs/remotes",
            ],
            git_root,
        )
    except subprocess.TimeoutExpired:
        return {
            "available": False,
            "reason": "git for-each-ref timed out",
            "base": base,
            "branches": [],
        }
    if refs.returncode != 0:
        return {
            "available": False,
            "reason": "git for-each-ref failed",
            "base": base,
            "branches": [],
        }

    candidates: list[tuple[str, str]] = []
    for line in refs.stdout.splitlines():
        name, symref, date = (line.split("\x00") + ["", ""])[:3]
        if symref or name == base or not name:
            continue  # origin/HEAD, or the base itself
        candidates.append((name, date))

    scope_args = ["--", rel_scope] if rel_scope != "." else []
    branches: list[dict] = []
    examined = 0
    for name, date in candidates:
        if len(branches) >= max_branches or _check_script_timeout():
            break
        examined += 1
        try:
            counts = _run_git(
                ["rev-list", "--left-right", "--count", f"{base}...{name}"],
                git_root,
            )
            if counts.returncode != 0:
                continue
            behind, ahead = (int(x) for x in counts.stdout.split()[:2])
            if ahead == 0:
                continue
            # `=` marks a branch commit whose patch is already in base (a
            # cherry-pick or a rebased copy), `+` one whose patch is not.
            marks = _run_git(
                ["rev-list", "--cherry-mark", "--right-only", f"{base}...{name}"],
                git_root,
            )
            log = _run_git(
                [
                    "log",
                    f"-{max_commits}",
                    "--format=%H%x00%cI%x00%s",
                    f"{base}..{name}",
                ],
                git_root,
            )
            files = _run_git(
                [
                    "diff",
                    "--name-only",
                    "--no-color",
                    "--no-ext-diff",
                    f"{base}...{name}",
                    *scope_args,
                ],
                git_root,
            )
        except (subprocess.TimeoutExpired, ValueError):
            continue
        in_base: set[str] | None = None
        if marks.returncode == 0:
            in_base = {
                line[1:].strip()
                for line in marks.stdout.splitlines()
                if line.startswith("=")
            }
        commits = []
        for entry in log.stdout.splitlines():
            sha, cdate, subject = (entry.split("\x00") + ["", ""])[:3]
            detail = classify_commit_detail(subject, key_types)
            commits.append(
                {
                    "commit_short": sha[:7],
                    "date": cdate,
                    "message": subject,
                    "type": detail["type"],
                    "keys": detail["keys"],
                    # True: the same patch is already in base, so this commit
                    # is not unmerged work. None: the check failed.
                    "patch_in_base": None if in_base is None else sha in in_base,
                }
            )
        changed = (
            [f for f in files.stdout.splitlines() if f] if files.returncode == 0 else []
        )
        branches.append(
            {
                "branch": name,
                # Raw commit count (`rev-list base..branch`). A branch rebased
                # or cherry-picked onto base still counts every copy here;
                # `ahead_patch_unique` is the work base does not have.
                "ahead": ahead,
                "ahead_patch_unique": (
                    None if in_base is None else max(ahead - len(in_base), 0)
                ),
                "ahead_patch_in_base": None if in_base is None else len(in_base),
                "behind": behind,
                "last_commit_date": date,
                "fix_commits_shown": sum(1 for c in commits if c["type"] == "fix"),
                "fix_commits_shown_unique": sum(
                    1
                    for c in commits
                    if c["type"] == "fix" and c["patch_in_base"] is not True
                ),
                "commits": commits,
                "commits_truncated": ahead > len(commits),
                "files_in_scope_changed": len(changed),
                "files_in_scope": changed[:_MAX_UNMERGED_FILES],
            }
        )
    return {
        "available": True,
        "base": base,
        "remote_branches_total": len(candidates),
        "remote_branches_examined": examined,
        "unmerged_count": len(branches),
        "truncated": examined < len(candidates),
        "note": (
            "Commits on remote branches that are not in the base branch, as "
            "of the last fetch (this script never fetches). Treat them as "
            "prior art: a finding may already be fixed, or attempted, there. "
            "A commit with patch_in_base true is a copy of a commit base "
            "already has (cherry-pick or rebase), so it is not unmerged work; "
            "`ahead_patch_unique` counts the rest."
        ),
        "branches": branches,
    }


def parse_args(argv: list[str]) -> dict:
    """Parse command-line arguments."""
    args: dict = {
        "path": ".",
        "days": 90,
        "since": None,
        "until": None,
        "last": None,
        "max_commits": 2000,
        "max_files": 0,
        "no_function": False,
        "no_branches": False,
        "key_types": {},
        "unknown_args": [],
    }

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--days" and i + 1 < len(argv):
            try:
                args["days"] = int(argv[i + 1])
            except ValueError:
                print(
                    f"Error: --days requires an integer, got '{argv[i + 1]}'",
                    file=sys.stderr,
                )
                sys.exit(2)
            i += 2
        elif arg == "--since" and i + 1 < len(argv):
            args["since"] = argv[i + 1]
            i += 2
        elif arg == "--until" and i + 1 < len(argv):
            args["until"] = argv[i + 1]
            i += 2
        elif arg == "--last" and i + 1 < len(argv):
            try:
                args["last"] = int(argv[i + 1])
            except ValueError:
                print(
                    f"Error: --last requires an integer, got '{argv[i + 1]}'",
                    file=sys.stderr,
                )
                sys.exit(2)
            i += 2
        elif arg == "--max-commits" and i + 1 < len(argv):
            try:
                args["max_commits"] = int(argv[i + 1])
            except ValueError:
                print(
                    f"Error: --max-commits requires an integer, got '{argv[i + 1]}'",
                    file=sys.stderr,
                )
                sys.exit(2)
            i += 2
        elif arg == "--max-files" and i + 1 < len(argv):
            try:
                args["max_files"] = int(argv[i + 1])
            except ValueError:
                print(
                    f"Error: --max-files requires an integer, got '{argv[i + 1]}'",
                    file=sys.stderr,
                )
                sys.exit(2)
            i += 2
        elif arg == "--no-function":
            args["no_function"] = True
            i += 1
        elif arg == "--no-branches":
            args["no_branches"] = True
            i += 1
        elif arg == "--key-type" and i + 1 < len(argv):
            try:
                args["key_types"].update(parse_key_types(argv[i + 1]))
            except ValueError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                sys.exit(2)
            i += 2
        elif not arg.startswith("-"):
            args["path"] = arg
            i += 1
        else:
            # A stderr warning alone is easy to lose in an agent transcript, so
            # also carry the flag into the JSON envelope: a silently-ignored
            # window flag otherwise yields a confident, wrong analysis at the
            # default window.
            print(f"Warning: unknown flag '{arg}', ignoring", file=sys.stderr)
            args["unknown_args"].append(arg)
            i += 1

    return args


def analyze(argv: list[str] | None = None) -> dict:
    """Main analysis function. Returns the result dict."""
    global _SCRIPT_START
    _SCRIPT_START = time.monotonic()

    if argv is None:
        argv = sys.argv[1:]
    args = parse_args(argv)

    scan_root = Path(args["path"]).resolve()
    project_root = find_project_root(scan_root)

    if not _is_git_repo(project_root):
        return {"error": "Not a git repository", "project_root": str(project_root)}

    # Determine time range.
    now = datetime.now(timezone.utc)
    if args["since"]:
        since = args["since"]
    else:
        since = (now - timedelta(days=args["days"])).isoformat()
    until = args["until"] if args["until"] else now.isoformat()

    last_n = args["last"]
    max_commits = args["max_commits"]

    key_types = args["key_types"]

    # Parse git log.
    git_args = [
        "log",
        "--numstat",
        f"--format={LOG_FORMAT}",
    ]
    if last_n is not None:
        git_args.append(f"-{last_n}")
    else:
        git_args.extend([f"--since={since}", f"--until={until}"])
    git_args.append("--")
    rel_scope = _relative_scope(scan_root, project_root)
    if rel_scope != ".":
        git_args.append(rel_scope)

    # `with` closes the stdout pipe. Leaking it emitted a ResourceWarning that
    # landed in reports/coveragepy_v1/analyze_history.json and made the report
    # unparseable -- the warning is written when the object is collected, which
    # is after the JSON has been dumped.
    with _run_git_streaming(git_args, project_root) as proc:
        commits, file_churn = parse_git_log(
            proc.stdout or [],
            max_commits,
            project_root,
            key_types,
        )

    # If using --last, derive time range from actual commits.
    commit_cap_applied = len(commits) >= max_commits
    if last_n is not None and commits:
        since = commits[-1]["committer_date"]
        until = commits[0]["committer_date"]
        # Calculate days.
        try:
            start_dt = datetime.fromisoformat(since)
            end_dt = datetime.fromisoformat(until)
            days = max(1, (end_dt - start_dt).days)
        except ValueError:
            days = args["days"]
    else:
        days = args["days"]

    # Classify commits.
    commits_by_type: dict[str, int] = defaultdict(int)
    classified_by: dict[str, int] = defaultdict(int)
    authors: set[str] = set()
    for commit in commits:
        commits_by_type[commit["type"]] += 1
        classified_by[commit.get("type_source") or "none"] += 1
        authors.add(commit["author"])

    git_root = git_toplevel(project_root)
    attributor = CommitAttributor(git_root)
    try:
        # Function-level churn.
        function_churn: list[dict] = []
        function_churn_note: str | None = None
        if args["no_function"] or _check_script_timeout():
            function_churn_note = (
                "Function-level churn skipped (--no-function flag or performance "
                "fallback)"
            )
        else:
            function_churn = compute_function_churn_level2(
                commits,
                scan_root,
                project_root,
                max_files=args["max_files"],
                attributor=attributor,
            )
            if _check_script_timeout():
                function_churn_note = (
                    "Function-level churn partially computed (script timeout)"
                )

        # Get recent fixes, features, refactors.
        recent_fixes = get_commit_details(
            commits,
            "fix",
            project_root,
            scan_root,
            _MAX_DIFF_LINES_FIX,
            attributor,
        )
        recent_features = get_commit_details(
            commits,
            "feature",
            project_root,
            scan_root,
            _MAX_DIFF_LINES_FIX,
            attributor,
        )
        recent_refactors = get_commit_details(
            commits,
            "refactor",
            project_root,
            scan_root,
            _MAX_DIFF_LINES_REFACTOR,
            attributor,
        )
    finally:
        attributor.close()
    attribution_meta = attributor.meta()

    # Co-change clusters.
    co_change_clusters = compute_co_change_clusters(commits)

    if args["no_branches"]:
        unmerged = {
            "available": False,
            "reason": "skipped (--no-branches)",
            "branches": [],
        }
    else:
        unmerged = find_unmerged_branches(
            git_root,
            _relative_scope(scan_root, git_root),
            key_types,
        )

    # Build result.
    result: dict = {
        "project_root": str(project_root),
        "scan_root": str(scan_root),
        "time_range": {
            "start": since,
            "end": until,
            "days": days,
            "commit_cap_applied": commit_cap_applied,
            # --since/--until select on the committer date; every in-window
            # date below that is not labelled `date` (author) is a committer
            # date too.
            "date_basis": "committer",
        },
        "summary": {
            "total_commits": len(commits),
            "commits_by_type": dict(commits_by_type),
            # Which signal decided each commit's type: conventional / tag /
            # key / keyword / none (= unknown).
            "classified_by": dict(classified_by),
            "key_types": key_types,
            "files_changed": len(file_churn),
            "functions_changed": len(function_churn),
            "authors": len(authors),
        },
        "tracker_keys": summarize_tracker_keys(commits),
        "file_churn": file_churn,
        "function_churn": function_churn,
        "function_attribution": attribution_meta,
        "recent_fixes": recent_fixes,
        "recent_features": recent_features,
        "recent_refactors": recent_refactors,
        "co_change_clusters": co_change_clusters,
        "unmerged_branches": unmerged,
        "notes": [],
    }

    if (
        attribution_meta["commits_unattributed"]
        or attribution_meta["files_unattributed"]
    ):
        result["notes"].append(
            f"Function attribution is INCOMPLETE: "
            f"{attribution_meta['commits_unattributed']} commit(s) and "
            f"{attribution_meta['files_unattributed']} file change(s) could not "
            "be mapped to functions (timeout, parse cap, or a file version that "
            "does not parse). Function counts are lower bounds."
        )
    unknown = commits_by_type.get("unknown", 0)
    if commits and unknown / len(commits) > 0.15:
        result["notes"].append(
            f"{unknown} of {len(commits)} commits are `unknown`. If the project "
            "marks commits with tracker keys (see tracker_keys.by_prefix), "
            "re-run with --key-type PREFIX=TYPE (e.g. --key-type B=fix,F=feature) "
            "before quoting a fix:feature ratio."
        )

    if function_churn_note:
        result["function_churn_note"] = function_churn_note
        result["notes"].append(
            "Function-level churn was skipped -- `function_churn` is empty by "
            "request, not because nothing changed."
        )

    if args["unknown_args"]:
        result["unknown_args"] = args["unknown_args"]
        result["notes"].append(
            "Unrecognized argument(s) IGNORED: "
            + ", ".join(args["unknown_args"])
            + ". The analysis below ran with default values for whatever those "
            "flags were meant to set -- re-run with the correct flag before "
            "trusting any temporal claim."
        )

    if commit_cap_applied:
        result["notes"].append(
            f"COMMIT CAP APPLIED: analysis stopped at --max-commits "
            f"{max_commits}; the window contains at least that many commits, "
            "so everything below is a truncated prefix of the real history. "
            "Raise --max-commits or narrow the window."
        )

    return result


def main() -> None:
    """Entry point."""
    from scan_common import exit_on_help

    exit_on_help(sys.argv[1:], __doc__)
    result = analyze()
    if "error" in result:
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
        sys.exit(1)
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
