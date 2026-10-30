#!/usr/bin/env python3
"""Collect and categorize technical debt markers from a Python codebase.

Searches for TODO, FIXME, HACK, XXX, NOQA, type: ignore, pragma: no cover,
and unittest.skip markers. Cross-references with git blame for age data.

Optionally (``--key-regex``) inventories tracker keys -- ``ABC-12``-style
issue keys or a project's own scheme -- cited in comments, docstrings and
docs, with local resolution signals (changelog mention, strike-through in a
roadmap, commits that mention the key). Never touches the network.

Usage:
    python collect_debt.py [path] [--max-files N] [--key-regex PATTERN]
"""

import ast
import io
import json
import re
import subprocess
import sys
import tokenize
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scan_common import (  # noqa: E402
    discover_python_files,
    find_project_root,
    parse_common_args,
)


# Patterns to search for, grouped by category.
_MARKER_PATTERNS: dict[str, re.Pattern[str]] = {
    "TODO": re.compile(r"#\s*TODO\b[:\s]*(.*)", re.IGNORECASE),
    "FIXME": re.compile(r"#\s*FIXME\b[:\s]*(.*)", re.IGNORECASE),
    "HACK": re.compile(r"#\s*(?:HACK|WORKAROUND)\b[:\s]*(.*)", re.IGNORECASE),
    "XXX": re.compile(r"#\s*XXX\b[:\s]*(.*)", re.IGNORECASE),
    "NOQA": re.compile(r"#\s*noqa\b[:\s]*(.*)", re.IGNORECASE),
    "TYPE_IGNORE": re.compile(r"#\s*type:\s*ignore\b(.*)", re.IGNORECASE),
    "PRAGMA_NO_COVER": re.compile(r"#\s*pragma:\s*no\s*cover\b(.*)", re.IGNORECASE),
}

# Decorator-based markers (searched in AST-like fashion via regex).
_SKIP_PATTERN = re.compile(r"@(?:unittest\.)?skip(?:If|Unless)?\s*\(", re.IGNORECASE)


def _has_git(project_root: Path) -> bool:
    """Check if the project is in a git repository."""
    return (project_root / ".git").exists()


def _git_blame_line(filepath: Path, lineno: int, project_root: Path) -> dict | None:
    """Get git blame info for a specific line."""
    try:
        result = subprocess.run(
            ["git", "blame", "-L", f"{lineno},{lineno}", "--porcelain", str(filepath)],
            capture_output=True,
            text=True,
            cwd=str(project_root),
            timeout=10,
        )
        if result.returncode != 0:
            return None

        author = None
        timestamp = None
        for line in result.stdout.splitlines():
            if line.startswith("author "):
                author = line[7:]
            elif line.startswith("author-time "):
                try:
                    ts = int(line[12:])
                    timestamp = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
                except (ValueError, OSError):
                    pass

        return {"author": author, "date": timestamp}
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def _classify_age(date_str: str | None) -> str:
    """Classify debt age into fresh/growing/stale/ancient."""
    if not date_str:
        return "unknown"
    try:
        dt = datetime.fromisoformat(date_str)
        now = datetime.now(tz=timezone.utc)
        days = (now - dt).days
        if days < 30:
            return "fresh"
        elif days < 180:
            return "growing"
        elif days < 365:
            return "stale"
        else:
            return "ancient"
    except (ValueError, TypeError):
        return "unknown"


def scan_file(filepath: Path, project_root: Path, use_git: bool) -> list[dict]:
    """Scan a file for debt markers."""
    try:
        lines = filepath.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []

    rel = str(filepath.relative_to(project_root))
    items: list[dict] = []

    for i, line in enumerate(lines, 1):
        # Check comment-based markers.
        for category, pattern in _MARKER_PATTERNS.items():
            match = pattern.search(line)
            if match:
                text = match.group(1).strip() if match.group(1) else ""

                # Get surrounding context (1 line before, 1 after).
                context_before = lines[i - 2].strip() if i > 1 else ""
                context_after = lines[i].strip() if i < len(lines) else ""

                item: dict = {
                    "file": rel,
                    "line": i,
                    "category": category,
                    "text": text,
                    "full_line": line.rstrip(),
                    "context_before": context_before,
                    "context_after": context_after,
                }

                if use_git:
                    blame = _git_blame_line(filepath, i, project_root)
                    if blame:
                        item["author"] = blame["author"]
                        item["date"] = blame["date"]
                        item["age"] = _classify_age(blame["date"])
                    else:
                        item["age"] = "unknown"
                else:
                    item["age"] = "unknown"

                items.append(item)

        # Check skip decorators.
        if _SKIP_PATTERN.search(line):
            item = {
                "file": rel,
                "line": i,
                "category": "SKIP",
                "text": line.strip(),
                "full_line": line.rstrip(),
                "context_before": lines[i - 2].strip() if i > 1 else "",
                "context_after": lines[i].strip() if i < len(lines) else "",
                "age": "unknown",
            }
            if use_git:
                blame = _git_blame_line(filepath, i, project_root)
                if blame:
                    item["author"] = blame["author"]
                    item["date"] = blame["date"]
                    item["age"] = _classify_age(blame["date"])
            items.append(item)

    return items


# --- tracker keys ----------------------------------------------------------
#
# Many projects track deferred work by KEY (`ABC-12`, `PROJ-7`, `R2-4`) in an
# issue tracker or a ROADMAP file, and cite the key in a code comment instead
# of writing TODO. Marker counting alone then reports "1 TODO" for a codebase
# with dozens of open, keyed items. Opt-in (`--key-regex`), because no default
# pattern tells a tracker key from `UTF-8` or `SHA-256`, and local-only: files
# and `git log`, never the network.

# Files whose mention of a key says the work shipped.
_CHANGELOG_STEMS = ("changelog", "changes", "history", "news", "release")
# Files that list planned work: a key here that is neither struck through
# nor in a changelog is an open item.
_PLANNING_STEMS = ("roadmap", "todo", "backlog", "plan")
_DOC_SUFFIXES = frozenset({".md", ".rst", ".txt", ".adoc"})
_DOC_DIRS = ("docs", "doc")
_MAX_DOC_FILES = 300
_MAX_DOC_BYTES = 2_000_000
_MAX_REFS_PER_KEY = 20
_GIT_LOG_MAX_COUNT = 20000
_STRIKE = re.compile(r"~~(.+?)~~")


def _doc_files(project_root: Path) -> list[Path]:
    """Planning docs at the project root plus everything under docs/ or doc/."""
    found: list[Path] = []
    for item in sorted(project_root.iterdir()):
        if not item.is_file():
            continue
        name = item.name.lower()
        is_doc = item.suffix.lower() in _DOC_SUFFIXES and not name.startswith(
            "requirements"
        )
        # A planning file is read whatever its extension (`TODO`, `CHANGES`).
        if is_doc or name.startswith(_PLANNING_STEMS + _CHANGELOG_STEMS):
            found.append(item)
    for name in _DOC_DIRS:
        base = project_root / name
        if base.is_dir():
            found.extend(
                p
                for p in sorted(base.rglob("*"))
                if p.is_file() and p.suffix.lower() in _DOC_SUFFIXES
            )
    return found[:_MAX_DOC_FILES]


def _python_comment_lines(path: Path) -> list[tuple[int, str]]:
    """``(line, text)`` for every comment and docstring line in *path*."""
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[tuple[int, str]] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT:
                out.append((tok.start[0], tok.string))
    except (tokenize.TokenError, SyntaxError):
        # Untokenizable file: fall back to the text after a `#` on each line.
        out = [
            (number, line[line.index("#") :])
            for number, line in enumerate(source.splitlines(), 1)
            if "#" in line
        ]
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return out
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            first = body[0].lineno
            for offset, text in enumerate(body[0].value.value.splitlines()):
                out.append((first + offset, text))
    return out


def _git_log_mentions(project_root: Path, pattern: re.Pattern[str]) -> dict | None:
    """``{key: {"commits": n, "last_date": iso}}`` from one local `git log` pass.

    Returns None when git is unavailable or fails, so the caller can say the
    lookup was not made rather than report zero commits.
    """
    try:
        result = subprocess.run(
            [
                "git",
                "log",
                f"--max-count={_GIT_LOG_MAX_COUNT}",
                "--format=%aI%x00%s%x00%b%x1e",
            ],
            capture_output=True,
            text=True,
            cwd=str(project_root),
            timeout=60,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    mentions: dict[str, dict] = {}
    for record in result.stdout.split("\x1e"):
        parts = record.strip("\n").split("\x00")
        if len(parts) < 2:
            continue
        date = parts[0]
        for key in {m.group(0) for m in pattern.finditer(" ".join(parts[1:]))}:
            entry = mentions.setdefault(key, {"commits": 0, "last_date": None})
            entry["commits"] += 1
            # git log is newest-first, so the first date seen is the latest.
            entry["last_date"] = entry["last_date"] or date
    return mentions


def _natural_key(key: str) -> list:
    """Sort `B2` before `B10`."""
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", key)]


def collect_tracker_keys(
    project_root: Path,
    py_files: list[Path],
    pattern: re.Pattern[str],
    use_git: bool,
) -> dict:
    """Inventory every tracker key cited in code comments, docstrings and docs.

    Per key: where code cites it, where docs cite it (with the line, so a
    roadmap row's target version is visible), whether a changelog mentions it,
    whether a doc strikes it through (`~~ABC-12~~`, a common "done" mark in
    roadmap tables), and how many commits mention it. A key cited in code with
    none of those resolution signals is listed in ``open_in_code``.
    """
    keys: dict[str, dict] = {}

    def entry(key: str) -> dict:
        return keys.setdefault(
            key,
            {
                "key": key,
                "code_ref_count": 0,
                "doc_ref_count": 0,
                "in_changelog": False,
                "in_planning_doc": False,
                "struck_through": False,
                "code_refs": [],
                "doc_refs": [],
            },
        )

    for path in py_files:
        rel = str(path.relative_to(project_root))
        for line, text in _python_comment_lines(path):
            for match in pattern.finditer(text):
                item = entry(match.group(0))
                item["code_ref_count"] += 1
                if len(item["code_refs"]) < _MAX_REFS_PER_KEY:
                    item["code_refs"].append(
                        {"file": rel, "line": line, "text": text.strip()[:200]}
                    )

    docs = _doc_files(project_root)
    for path in docs:
        try:
            if path.stat().st_size > _MAX_DOC_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = str(path.relative_to(project_root))
        is_changelog = path.name.lower().startswith(_CHANGELOG_STEMS)
        is_planning = path.name.lower().startswith(_PLANNING_STEMS)
        for number, line in enumerate(text.splitlines(), 1):
            struck = {
                m.group(0)
                for span in _STRIKE.finditer(line)
                for m in pattern.finditer(span.group(1))
            }
            for match in pattern.finditer(line):
                key = match.group(0)
                item = entry(key)
                item["doc_ref_count"] += 1
                item["in_changelog"] |= is_changelog
                item["in_planning_doc"] |= is_planning
                item["struck_through"] |= key in struck
                if len(item["doc_refs"]) < _MAX_REFS_PER_KEY:
                    item["doc_refs"].append(
                        {
                            "file": rel,
                            "line": number,
                            "struck": key in struck,
                            "text": line.strip()[:200],
                        }
                    )

    git = _git_log_mentions(project_root, pattern) if use_git else None
    for key, item in keys.items():
        found = (git or {}).get(key)
        # None, not 0, when git was not searched: unmeasured is not "no commits".
        item["commits"] = (
            found["commits"] if found else (0 if git is not None else None)
        )
        item["last_commit_date"] = found["last_date"] if found else None

    ordered = sorted(keys.values(), key=lambda k: _natural_key(k["key"]))
    open_in_code = [
        k["key"]
        for k in ordered
        if k["code_ref_count"]
        and not k["in_changelog"]
        and not k["struck_through"]
        and not k["commits"]
    ]
    open_in_planning_docs = [
        k["key"]
        for k in ordered
        if k["in_planning_doc"] and not k["in_changelog"] and not k["struck_through"]
    ]
    return {
        "regex": pattern.pattern,
        "doc_files_scanned": [str(p.relative_to(project_root)) for p in docs],
        "git_log_searched": git is not None,
        "summary": {
            "keys_total": len(ordered),
            "keys_cited_in_code": sum(1 for k in ordered if k["code_ref_count"]),
            "keys_in_changelog": sum(1 for k in ordered if k["in_changelog"]),
            "keys_struck_through": sum(1 for k in ordered if k["struck_through"]),
            "open_in_code": len(open_in_code),
            "open_in_planning_docs": len(open_in_planning_docs),
        },
        # Cited in code, but no changelog entry, strike-through or commit
        # mentions it: the keyed equivalent of an open TODO.
        "open_in_code": open_in_code,
        # Listed in a roadmap/todo/backlog/plan file, not struck through and in
        # no changelog: planned work still open. `doc_refs[].text` carries the
        # row, which usually names the target version.
        "open_in_planning_docs": open_in_planning_docs,
        "keys": ordered,
    }


def _extract_options(argv: list[str]) -> tuple[list[str], str | None]:
    """Pull ``--key-regex PATTERN`` out of argv, returning the remainder."""
    rest: list[str] = []
    key_regex: str | None = None
    i = 0
    while i < len(argv):
        if argv[i] == "--key-regex" and i + 1 < len(argv):
            key_regex = argv[i + 1]
            i += 2
        elif argv[i].startswith("--key-regex="):
            key_regex = argv[i].split("=", 1)[1]
            i += 1
        else:
            rest.append(argv[i])
            i += 1
    return rest, key_regex


def main() -> None:
    argv, key_regex = _extract_options(sys.argv[1:])
    target_arg, max_files = parse_common_args(argv, __doc__)
    target = Path(target_arg).resolve()
    key_pattern: re.Pattern[str] | None = None
    if key_regex is not None:
        try:
            key_pattern = re.compile(key_regex)
        except re.error as exc:
            json.dump({"error": f"invalid --key-regex: {exc}"}, sys.stdout, indent=2)
            sys.stdout.write("\n")
            sys.exit(2)

    project_root = find_project_root(target)
    scan_root = target if target.is_dir() else project_root
    all_files = sorted(discover_python_files(scan_root))
    files_total = len(all_files)
    if max_files > 0 and files_total > max_files:
        all_files = all_files[:max_files]
    use_git = _has_git(project_root)

    all_items: list[dict] = []
    for f in all_files:
        all_items.extend(scan_file(f, project_root, use_git))

    # Aggregate stats.
    by_category: dict[str, int] = {}
    by_age: dict[str, int] = {}
    by_file: dict[str, int] = {}
    for item in all_items:
        cat = item["category"]
        by_category[cat] = by_category.get(cat, 0) + 1
        age = item.get("age", "unknown")
        by_age[age] = by_age.get(age, 0) + 1
        by_file[item["file"]] = by_file.get(item["file"], 0) + 1

    # Top files by debt count.
    top_files = sorted(by_file.items(), key=lambda x: -x[1])[:10]

    output = {
        "project_root": str(project_root),
        "scan_root": str(scan_root),
        "files_total": files_total,
        "files_analyzed": len(all_files),
        "files_capped": max_files > 0 and files_total > max_files,
        "git_available": use_git,
        "summary": {
            "total_markers": len(all_items),
            "by_category": dict(sorted(by_category.items(), key=lambda x: -x[1])),
            "by_age": dict(sorted(by_age.items(), key=lambda x: -x[1])),
            "files_with_debt": len(by_file),
            "top_files": [{"file": f, "count": c} for f, c in top_files],
        },
        "items": all_items,
    }
    if key_pattern is not None:
        output["tracker_keys"] = collect_tracker_keys(
            project_root, all_files, key_pattern, use_git
        )
    else:
        # Not measured -- say so instead of omitting the section, so an agent
        # does not read "1 TODO" as the project's whole debt.
        output["tracker_keys"] = None
        output["tracker_keys_reason"] = (
            "not requested: pass --key-regex PATTERN (e.g. '\\b[A-Z]+-\\d+\\b') "
            "to inventory tracker keys cited in comments, docstrings and docs"
        )

    json.dump(output, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
