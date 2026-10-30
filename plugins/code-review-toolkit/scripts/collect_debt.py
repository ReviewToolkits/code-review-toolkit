#!/usr/bin/env python3
"""Collect and categorize technical debt markers from a Python codebase.

Searches for TODO, FIXME, HACK, XXX, NOQA, type: ignore, pragma: no cover,
and unittest.skip markers. Cross-references with git blame for age data.

Usage:
    python collect_debt.py [path]
"""

import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scan_common import (  # noqa: E402
    discover_python_files,
    find_project_root,
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


def main() -> None:
    max_files = 0  # 0 = no limit
    positional: list[str] = []
    argv = sys.argv[1:]
    i = 0
    while i < len(argv):
        if argv[i] == "--max-files" and i + 1 < len(argv):
            max_files = int(argv[i + 1])
            i += 2
        elif argv[i].startswith("--"):
            i += 1
        else:
            positional.append(argv[i])
            i += 1
    target = Path(positional[0]) if positional else Path(".")
    target = target.resolve()

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

    json.dump(output, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
