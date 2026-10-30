#!/usr/bin/env python3
"""Collect and categorize technical debt markers from a Python codebase.

Searches for TODO, FIXME, HACK, XXX, NOQA, type: ignore, pragma: no cover,
nosec and pylint: disable comments, and skipped or xfail tests (unittest and
pytest spellings). Cross-references with git blame for age data.

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
    # Linter suppressions: a project whose only inline suppressions are
    # bandit's must not read as "suppresses nothing".
    "NOSEC": re.compile(r"#\s*nosec\b[:\s]*(.*)", re.IGNORECASE),
    "PYLINT_DISABLE": re.compile(r"#\s*pylint:\s*disable\b[=:\s]*(.*)", re.IGNORECASE),
}

# Skipped and expected-to-fail tests, in unittest and pytest spellings.
_SKIP_PATTERN = re.compile(
    r"@(?:unittest\.)?skip(?:If|Unless)?\s*\("  # @unittest.skip(...), @skipIf(...)
    r"|(?<![\w.])(?:pytest\.)?mark\.skip(?:if)?\b"  # @pytest.mark.skip, @mark.skipif
    r"|(?<![\w.])pytest\.(?:skip|importorskip)\s*\("  # pytest.skip("...")
    r"|\.skipTest\s*\(",  # self.skipTest("...")
    re.IGNORECASE,
)
_XFAIL_PATTERN = re.compile(
    r"(?<![\w.])(?:pytest\.)?mark\.xfail\b"
    r"|(?<![\w.])pytest\.xfail\s*\("
    r"|@(?:unittest\.)?expectedFailure\b"
)


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

        # Check skip and xfail markers (not in a comment-only line).
        if line.lstrip().startswith("#"):
            continue
        test_category = (
            "SKIP"
            if _SKIP_PATTERN.search(line)
            else "XFAIL"
            if _XFAIL_PATTERN.search(line)
            else None
        )
        if test_category:
            item = {
                "file": rel,
                "line": i,
                "category": test_category,
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


# --- configuration and CI debt ---------------------------------------------
#
# Debt that lives outside .py files: version pins with a comment explaining
# why, tool suppressions configured once for the whole project, and CI steps
# allowed to fail. Read from the project root, whatever the scan scope.

_CONFIG_FILES = (
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "tox.ini",
    "mypy.ini",
    ".mypy.ini",
    "ruff.toml",
    ".ruff.toml",
    ".flake8",
    ".bandit",
    "pytest.ini",
    ".coveragerc",
    ".pylintrc",
    "pylintrc",
)
_REQUIREMENT_GLOBS = ("requirements*.txt", "constraints*.txt", "requirements/*.txt")
_CI_GLOBS = (
    ".github/workflows/*.yml",
    ".github/workflows/*.yaml",
    ".gitlab-ci.yml",
    "gitlab-ci.yml",
    ".circleci/config.yml",
    "azure-pipelines.yml",
    ".travis.yml",
)
_CONFIG_MARKER = re.compile(
    r"#\s*(TODO|FIXME|HACK|WORKAROUND|XXX)\b[:\s]*(.*)", re.IGNORECASE
)
_REQUIREMENT = re.compile(
    r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*((?:[<>=!~]=?=?\s*[^,;\s]+\s*,?\s*)+)"
)
_QUOTED = re.compile(r'"([^"\n]*)"|\'([^\'\n]*)\'')
_SECTION = re.compile(r"^\s*\[+\s*([^\]]+?)\s*\]+\s*$")

# (tool, setting) pairs that switch a check off project-wide. A section whose
# name ends in `per-file-ignores` is a suppression on every line.
_SUPPRESSION_KEYS: dict[str, frozenset[str]] = {
    "ruff": frozenset(
        {"ignore", "extend-ignore", "per-file-ignores", "extend-per-file-ignores"}
    ),
    "flake8": frozenset(
        {"ignore", "extend-ignore", "per-file-ignores", "extend_ignore"}
    ),
    "mypy": frozenset(
        {
            "ignore_missing_imports",
            "ignore_errors",
            "disable_error_code",
            "follow_imports",
        }
    ),
    "pylint": frozenset({"disable"}),
    "bandit": frozenset({"skips"}),
    "coverage": frozenset({"omit", "exclude_lines", "exclude_also"}),
    "pytest": frozenset({"filterwarnings", "addopts"}),
    "pyright": frozenset({"ignore"}),
}


def _tool_of(section: str, filename: str) -> str | None:
    """Which tool a config section configures."""
    name = section.lower().removeprefix("tool.").removeprefix("tool:")
    for tool in (
        "ruff",
        "flake8",
        "mypy",
        "pylint",
        "bandit",
        "coverage",
        "pytest",
        "pyright",
    ):
        if name.startswith(tool):
            return tool
    if filename in ("ruff.toml", ".ruff.toml"):
        return "ruff"
    if filename == ".coveragerc":
        return "coverage"
    if filename in (".pylintrc", "pylintrc") or name == "messages control":
        return "pylint"
    if filename == ".bandit":
        return "bandit"
    return None


def _value_lines(lines: list[str], start: int, is_toml: bool) -> tuple[str, int]:
    """The (joined) value of the setting on ``lines[start]`` and its last line
    index: a TOML array runs to its closing bracket, an INI value continues on
    indented lines."""
    _, _, value = lines[start].partition("=")
    parts = [value.strip()]
    end = start
    if is_toml:
        depth = value.count("[") - value.count("]")
        while depth > 0 and end + 1 < len(lines):
            end += 1
            parts.append(lines[end].strip())
            depth += lines[end].count("[") - lines[end].count("]")
    else:
        while end + 1 < len(lines) and lines[end + 1][:1] in (" ", "\t"):
            if not lines[end + 1].strip():
                break
            end += 1
            parts.append(lines[end].strip())
    return " ".join(p for p in parts if p)[:300], end


def _suppressions(path: Path, rel: str, lines: list[str]) -> list[dict]:
    is_toml = path.suffix == ".toml"
    section = ""
    out: list[dict] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        header = _SECTION.match(line)
        if header:
            section = header.group(1)
            i += 1
            continue
        key, sep, _ = line.partition("=")
        key = key.strip().strip('"')
        tool = _tool_of(section, path.name)
        if not sep or not key or key.startswith("#") or tool is None:
            i += 1
            continue
        per_file_section = section.lower().endswith("per-file-ignores")
        if per_file_section or key.lower() in _SUPPRESSION_KEYS.get(tool, ()):
            value, end = _value_lines(lines, i, is_toml)
            setting = "per-file-ignores" if per_file_section else key
            # These two settings suppress only in some values.
            relevant = (
                (setting.lower() == "filterwarnings" and "ignore" in value.lower())
                or (
                    setting.lower() == "addopts"
                    and re.search(
                        r"disable-warnings|-p\s*no:warnings|-W\s*ignore", value
                    )
                )
                or (
                    setting.lower() == "follow_imports"
                    and re.search(r"skip|silent", value)
                )
                or (
                    setting.lower()
                    not in ("filterwarnings", "addopts", "follow_imports")
                    and value.lower() not in ("false", "[]", '""', "''")
                )
            )
            if relevant:
                out.append(
                    {
                        "file": rel,
                        "line": i + 1,
                        "tool": tool,
                        "section": section or None,
                        "setting": key if per_file_section else setting,
                        "value": value,
                    }
                )
            i = end + 1
            continue
        i += 1
    return out


def _pin_kinds(spec: str) -> list[str]:
    kinds = []
    if re.search(r"(?<![<>!~=])===?", spec):
        kinds.append("exact")
    if "~=" in spec:
        kinds.append("compatible")
    if "<" in spec:
        kinds.append("upper_bound")
    if "!=" in spec:
        kinds.append("exclusion")
    return kinds


def _is_routine_cap(spec: str, kinds: list[str]) -> bool:
    """An upper bound at the next breaking boundary (`<2`, `<2.0.0`, `<0.3`):
    semver hygiene rather than a workaround. A cap mid-series (`<1.4`), an
    exact pin or an exclusion is not routine."""
    if kinds != ["upper_bound"] or "<=" in spec:
        return False
    caps = re.findall(r"<\s*([\d.]+)", spec)
    for cap in caps:
        parts = [int(p) for p in cap.split(".") if p.isdigit()]
        while parts and parts[0] == 0:
            parts.pop(0)
        if not parts or any(parts[1:]):
            return False
    return bool(caps)


def _comment_of(line: str) -> str | None:
    """The `# ...` comment of a line, ignoring `#` inside quotes."""
    in_quote = None
    for i, ch in enumerate(line):
        if in_quote:
            if ch == in_quote:
                in_quote = None
        elif ch in "\"'":
            in_quote = ch
        elif ch == "#":
            return line[i + 1 :].strip() or None
    return None


def _is_lockfile(lines: list[str]) -> bool:
    """pip-compile/uv export output: every line is an exact pin, by design."""
    text = "\n".join(lines[:200])
    if "--hash=" in text or "# via" in text or "autogenerated" in text.lower():
        return True
    reqs = [m for m in map(_REQUIREMENT.match, lines) if m]
    exact = sum(1 for m in reqs if "==" in m.group(3))
    return len(reqs) >= 20 and exact / len(reqs) > 0.8


def _pins(path: Path, rel: str, lines: list[str]) -> list[dict]:
    """Version pins (`==`, `~=`, `<`, `!=`) and the comment that explains them."""
    out: list[dict] = []
    quoted_only = path.suffix in (".toml", ".py")
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "-", ";")):
            continue
        if quoted_only:
            candidates = [a or b for a, b in _QUOTED.findall(line)]
        else:
            candidates = [line.split("#", 1)[0]]
        for candidate in candidates:
            match = _REQUIREMENT.match(candidate)
            if not match or not re.search(r"\d", match.group(3)):
                continue
            kinds = _pin_kinds(match.group(3))
            if not kinds:
                continue
            above = lines[i - 1].strip() if i else ""
            comment = _comment_of(line)
            out.append(
                {
                    "file": rel,
                    "line": i + 1,
                    "name": match.group(1),
                    "spec": match.group(3).strip().rstrip(","),
                    "kinds": kinds,
                    "comment": comment,
                    # A comment on its own line just above often explains it.
                    "comment_above": above[1:].strip()
                    if above.startswith("#")
                    else None,
                    "routine": _is_routine_cap(match.group(3), kinds)
                    and not comment
                    and not above.startswith("#"),
                }
            )
    return out


_CI_CHECKS = (
    (
        "continue_on_error",
        re.compile(r"^\s*continue-on-error:\s*true\b", re.IGNORECASE),
    ),
    ("allow_failure", re.compile(r"^\s*allow_failure:\s*true\b", re.IGNORECASE)),
    ("error_ignored", re.compile(r"\|\|\s*(?:true|:|exit 0)\b")),
)
_USES = re.compile(r"^\s*-?\s*uses:\s*['\"]?([^'\"\s#]+)")
_FLOATING_REFS = frozenset(
    {"main", "master", "latest", "head", "develop", "dev", "trunk"}
)


def _ci_items(rel: str, lines: list[str]) -> list[dict]:
    out: list[dict] = []
    for i, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            continue
        for kind, pattern in _CI_CHECKS:
            if pattern.search(line):
                out.append(
                    {
                        "file": rel,
                        "line": i + 1,
                        "kind": kind,
                        "text": line.strip()[:200],
                    }
                )
        uses = _USES.match(line)
        if uses:
            action = uses.group(1)
            if action.startswith(("./", "docker://")):
                continue
            _, at, ref = action.partition("@")
            if not at or ref.lower() in _FLOATING_REFS:
                out.append(
                    {
                        "file": rel,
                        "line": i + 1,
                        "kind": "unpinned_action",
                        "text": line.strip()[:200],
                    }
                )
    return out


def collect_config_debt(project_root: Path) -> dict:
    """Pins, tool suppressions, CI failure allowances and TODO-style comments
    in the project's configuration, requirements and CI files."""
    files: list[Path] = [project_root / name for name in _CONFIG_FILES]
    for pattern in (*_REQUIREMENT_GLOBS, *_CI_GLOBS):
        files.extend(sorted(project_root.glob(pattern)))
    result: dict = {
        "files_scanned": [],
        # Exact pins, exclusions, compatible-release pins, mid-series caps
        # and any pin with a comment: the ones that encode a decision.
        "pins": [],
        # `name<next-major>` caps with no comment, listed compactly.
        "routine_caps": [],
        "suppressions": [],
        "ci": [],
        "markers": [],
        "skipped_lockfiles": [],
    }
    seen: set[Path] = set()
    for path in files:
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        try:
            if path.stat().st_size > _MAX_DOC_BYTES:
                continue
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        rel = str(path.relative_to(project_root))
        result["files_scanned"].append(rel)
        is_ci = path.suffix in (".yml", ".yaml")
        if is_ci:
            result["ci"].extend(_ci_items(rel, lines))
        elif path.suffix == ".txt" and _is_lockfile(lines):
            result["skipped_lockfiles"].append(rel)
        else:
            if path.suffix == ".txt" or path.name in (
                "pyproject.toml",
                "setup.cfg",
                "setup.py",
            ):
                for pin in _pins(path, rel, lines):
                    if pin.pop("routine"):
                        result["routine_caps"].append(
                            f"{pin['name']}{pin['spec']} ({rel}:{pin['line']})"
                        )
                    else:
                        result["pins"].append(pin)
            if path.suffix not in (".txt", ".py"):
                result["suppressions"].extend(_suppressions(path, rel, lines))
        if path.suffix == ".py":
            continue  # its comments are scanned with the code when in scope
        for i, line in enumerate(lines):
            marker = _CONFIG_MARKER.search(line)
            if marker:
                result["markers"].append(
                    {
                        "file": rel,
                        "line": i + 1,
                        "category": marker.group(1)
                        .upper()
                        .replace("WORKAROUND", "HACK"),
                        "text": marker.group(2).strip()[:200],
                    }
                )
    return result


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
        # Read from the project root whatever the scan scope: pins with their
        # comments, project-wide tool suppressions, CI failure allowances and
        # TODO-style comments in config, requirements and CI files.
        "config_debt": collect_config_debt(project_root),
    }
    output["summary"]["config_debt"] = {
        key: len(output["config_debt"][key])
        for key in ("pins", "routine_caps", "suppressions", "ci", "markers")
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
