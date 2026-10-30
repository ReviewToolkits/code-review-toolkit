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
    """Return True if the script has exceeded its timeout."""
    return (time.monotonic() - _SCRIPT_START) > _SCRIPT_TIMEOUT


def parse_git_log(
    lines: Iterable[str],
    max_commits: int,
    project_root: Path | None = None,
    key_types: dict[str, str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Parse git log with numstat from an iterable of lines.

    Returns (commits, file_stats) where commits is a list of commit dicts
    and file_stats is accumulated per-file change data.
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
            parts = line[7:].split("|", 3)
            if len(parts) < 4:
                current_commit = None
                continue
            commit_hash, date_str, author, message = parts
            detail = classify_commit_detail(message, key_types)
            current_commit = {
                "hash": commit_hash,
                "date": date_str,
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


def get_function_boundaries(filepath: Path) -> list[dict]:
    """Parse a Python file with AST and return function/method boundaries."""
    try:
        source = filepath.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(filepath))
    except (SyntaxError, OSError):
        return []

    functions: list[dict] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end_lineno = getattr(node, "end_lineno", node.lineno)
            functions.append(
                {
                    "name": node.name,
                    "line_start": node.lineno,
                    "line_end": end_lineno,
                }
            )
    return functions


def compute_function_churn_level2(
    commits: list[dict],
    scan_root: Path,
    project_root: Path,
    max_files: int = 0,
) -> list[dict]:
    """Level 2: Map diff hunks to function boundaries using AST.

    Analyzes already-parsed commits to map file changes to functions.
    """
    # Build function boundaries for all Python files in scope.
    file_functions: dict[str, list[dict]] = {}
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
    filtered_py: list[Path] = []
    for pf in py_files:
        try:
            rel_parts = set(pf.relative_to(project_root).parts)
        except ValueError:
            continue
        if rel_parts & exclude:
            continue
        if any(
            part.endswith(".egg-info") for part in pf.relative_to(project_root).parts
        ):
            continue
        filtered_py.append(pf)

    if max_files > 0 and len(filtered_py) > max_files:
        filtered_py = filtered_py[:max_files]

    for pf in filtered_py:
        rel_path = str(pf.relative_to(project_root))
        boundaries = get_function_boundaries(pf)
        if boundaries:
            file_functions[rel_path] = boundaries

    if not file_functions:
        return []

    # For each commit, get the diff and map hunks to functions.
    func_commits: dict[tuple[str, str], set[str]] = defaultdict(set)

    for commit in commits:
        if _check_script_timeout():
            break
        for file_path in commit["files"]:
            if file_path not in file_functions:
                continue
            try:
                diff_result = _run_git(
                    ["show", "--format=", "-U0", commit["hash"], "--", file_path],
                    project_root,
                )
                if diff_result.returncode != 0:
                    continue
            except subprocess.TimeoutExpired:
                continue

            # Parse diff hunks to find changed line ranges.
            changed_lines: set[int] = set()
            for line in diff_result.stdout.splitlines():
                hunk_match = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
                if hunk_match:
                    start = int(hunk_match.group(1))
                    count = int(hunk_match.group(2)) if hunk_match.group(2) else 1
                    changed_lines.update(range(start, start + count))

            # Map changed lines to functions.
            for func in file_functions[file_path]:
                func_range = set(range(func["line_start"], func["line_end"] + 1))
                if changed_lines & func_range:
                    key = (file_path, func["name"])
                    func_commits[key].add(commit["hash"])

    # Build result.
    results: list[dict] = []
    for (file_path, func_name), commit_hashes in func_commits.items():
        # Find function boundaries in current version.
        boundaries = file_functions.get(file_path, [])
        func_info = next((f for f in boundaries if f["name"] == func_name), None)
        lines_changed = 0
        if func_info:
            lines_changed = func_info["line_end"] - func_info["line_start"] + 1
        results.append(
            {
                "function": func_name,
                "file": file_path,
                "line_start": func_info["line_start"] if func_info else 0,
                "line_end": func_info["line_end"] if func_info else 0,
                "commits": len(commit_hashes),
                "lines_changed": lines_changed,
            }
        )

    results.sort(key=lambda x: x["commits"], reverse=True)
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
) -> list[dict]:
    """Get detailed info for commits of a specific type."""
    typed_commits = [c for c in commits if c["type"] == commit_type]
    results: list[dict] = []

    rel_scope = _relative_scope(scan_root, project_root)

    for commit in typed_commits:
        if _check_script_timeout():
            break
        # Get the diff.
        diff_args = ["show", "--format=", "--patch", commit["hash"], "--"]
        if rel_scope != ".":
            diff_args.append(rel_scope)
        try:
            diff_result = _run_git(diff_args, project_root)
            diff_text = diff_result.stdout if diff_result.returncode == 0 else ""
        except subprocess.TimeoutExpired:
            diff_text = "[diff unavailable: git show timed out]"

        diff_text = _truncate_diff(diff_text, max_diff_lines)

        # Map to functions if possible.
        functions_modified: list[dict] = []
        for file_path in commit["files"]:
            full_path = project_root / file_path
            if full_path.suffix == ".py" and full_path.exists():
                boundaries = get_function_boundaries(full_path)
                # Parse the diff to find which functions were modified.
                try:
                    func_diff = _run_git(
                        ["show", "--format=", "-U0", commit["hash"], "--", file_path],
                        project_root,
                    )
                    if func_diff.returncode == 0:
                        changed_lines: set[int] = set()
                        for line in func_diff.stdout.splitlines():
                            hunk = re.match(
                                r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line
                            )
                            if hunk:
                                start = int(hunk.group(1))
                                count = int(hunk.group(2)) if hunk.group(2) else 1
                                changed_lines.update(range(start, start + count))
                        for func in boundaries:
                            func_range = set(
                                range(func["line_start"], func["line_end"] + 1)
                            )
                            if changed_lines & func_range:
                                functions_modified.append(
                                    {
                                        "function": func["name"],
                                        "file": file_path,
                                    }
                                )
                except subprocess.TimeoutExpired:
                    pass

        results.append(
            {
                "commit": commit["hash"],
                "commit_short": commit["hash"][:7],
                "message": commit["message"],
                "date": commit["date"],
                "author": commit["author"],
                "files": commit["files"],
                "functions_modified": functions_modified,
                "diff": diff_text,
            }
        )

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
        "--format=COMMIT:%H|%aI|%an|%s",
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
        since = commits[-1]["date"]
        until = commits[0]["date"]
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

    # Function-level churn.
    function_churn: list[dict] = []
    function_churn_note: str | None = None
    if args["no_function"] or _check_script_timeout():
        function_churn_note = (
            "Function-level churn skipped (--no-function flag or performance fallback)"
        )
    else:
        # Count total functions to decide strategy.
        function_churn = compute_function_churn_level2(
            commits,
            scan_root,
            project_root,
            max_files=args["max_files"],
        )
        if _check_script_timeout():
            function_churn_note = (
                "Function-level churn partially computed (script timeout)"
            )

    # Get recent fixes, features, refactors.
    recent_fixes = get_commit_details(
        commits, "fix", project_root, scan_root, _MAX_DIFF_LINES_FIX
    )
    recent_features = get_commit_details(
        commits, "feature", project_root, scan_root, _MAX_DIFF_LINES_FIX
    )
    recent_refactors = get_commit_details(
        commits, "refactor", project_root, scan_root, _MAX_DIFF_LINES_REFACTOR
    )

    # Co-change clusters.
    co_change_clusters = compute_co_change_clusters(commits)

    # Build result.
    result: dict = {
        "project_root": str(project_root),
        "scan_root": str(scan_root),
        "time_range": {
            "start": since,
            "end": until,
            "days": days,
            "commit_cap_applied": commit_cap_applied,
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
        "recent_fixes": recent_fixes,
        "recent_features": recent_features,
        "recent_refactors": recent_refactors,
        "co_change_clusters": co_change_clusters,
        "notes": [],
    }

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
    result = analyze()
    if "error" in result:
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
        sys.exit(1)
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
