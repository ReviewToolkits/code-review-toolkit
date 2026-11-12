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
    _is_excluded,
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


# --- markers outside `#` comments -------------------------------------------
#
# A TODO is not always a `#` comment in a .py file: templates, stylesheets and
# scripts have their own comment syntaxes, a docstring can carry a `Todo:`
# section, and a placeholder string (`"TODO: fill in"`) is debt that ships.

_DEBT_WORDS = r"(TODO|FIXME|HACK|WORKAROUND|XXX)"
# A marker that opens a comment line, as `#\s*TODO` does for Python.
_LINE_MARKER = re.compile(rf"^[\s*!-]*{_DEBT_WORDS}\b[:\s]*(.*)", re.IGNORECASE)
# A placeholder string: the value itself starts with the marker.
_STRING_MARKER = re.compile(r"^\s*(TODO|FIXME)\b[:\s]*(.*)", re.DOTALL)

_TEMPLATE_SUFFIXES = frozenset({".html", ".htm", ".jinja", ".jinja2", ".j2", ".xml"})
_STYLESHEET_SUFFIXES = frozenset({".css", ".scss", ".sass", ".less"})
_SCRIPT_SUFFIXES = frozenset({".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx"})
_VENDORED_DIRS = frozenset({"vendor", "vendored", "third_party", "bower_components"})
_BLOCK_COMMENT = re.compile(r"/\*(.*?)\*/", re.DOTALL)
_LINE_COMMENT = re.compile(r"(?<![:\\'\"\w])//(.*)")
_TEMPLATE_COMMENT = re.compile(
    r"\{#(.*?)#\}|<!--(.*?)-->|\{%-?\s*comment\s*-?%\}(.*?)\{%-?\s*endcomment",
    re.DOTALL,
)


def _text_kind(path: Path) -> str | None:
    suffix = path.suffix.lower()
    if suffix in _TEMPLATE_SUFFIXES:
        return "template"
    if suffix in _STYLESHEET_SUFFIXES:
        return "stylesheet"
    if suffix in _SCRIPT_SUFFIXES:
        return "script"
    return None


def discover_text_debt_files(scan_root: Path) -> list[Path]:
    """Templates, stylesheets and scripts under *scan_root*, minus excluded,
    vendored and minified files."""
    if not scan_root.is_dir():
        return []
    found: list[Path] = []
    for path in sorted(scan_root.rglob("*")):
        if _text_kind(path) is None or not path.is_file():
            continue
        if _is_excluded(path, scan_root) or ".min." in path.name:
            continue
        parts = path.relative_to(scan_root).parts
        if set(parts) & _VENDORED_DIRS:
            continue
        found.append(path)
    return found


def _comment_spans(text: str, kind: str, line_comments: bool) -> list[tuple[int, str]]:
    """``(offset, body)`` of every comment in a template/stylesheet/script.
    *line_comments*: `//` starts a comment (scripts, SCSS, Less; not CSS)."""
    spans: list[tuple[int, str]] = []
    if kind == "template":
        for match in _TEMPLATE_COMMENT.finditer(text):
            for group in (1, 2, 3):
                if match.group(group) is not None:
                    spans.append((match.start(group), match.group(group)))
        return spans
    for match in _BLOCK_COMMENT.finditer(text):
        spans.append((match.start(1), match.group(1)))
    if line_comments:
        covered = [(m.start(), m.end()) for m in _BLOCK_COMMENT.finditer(text)]
        for match in _LINE_COMMENT.finditer(text):
            if not any(a <= match.start() < b for a, b in covered):
                spans.append((match.start(1), match.group(1)))
    return spans


def _marker_item(
    rel: str, lines: list[str], number: int, category: str, text: str, source: str
) -> dict:
    return {
        "file": rel,
        "line": number,
        "category": category.upper().replace("WORKAROUND", "HACK"),
        "text": text.strip()[:200],
        "full_line": lines[number - 1].rstrip() if number <= len(lines) else "",
        "context_before": lines[number - 2].strip() if number > 1 else "",
        "context_after": lines[number].strip() if number < len(lines) else "",
        "source": source,
    }


def _add_age(item: dict, filepath: Path, project_root: Path, use_git: bool) -> dict:
    blame = _git_blame_line(filepath, item["line"], project_root) if use_git else None
    if blame:
        item["author"] = blame["author"]
        item["date"] = blame["date"]
        item["age"] = _classify_age(blame["date"])
    else:
        item["age"] = "unknown"
    return item


def scan_text_file(filepath: Path, project_root: Path, use_git: bool) -> list[dict]:
    """Debt markers in the comments of a template, stylesheet or script."""
    kind = _text_kind(filepath)
    if kind is None:
        return []
    try:
        if filepath.stat().st_size > _MAX_DOC_BYTES:
            return []
        text = filepath.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = text.splitlines()
    rel = str(filepath.relative_to(project_root))
    items: list[dict] = []
    seen: set[int] = set()
    line_comments = kind == "script" or filepath.suffix.lower() != ".css"
    for offset, body in _comment_spans(text, kind, line_comments):
        first = text.count("\n", 0, offset) + 1
        for index, line in enumerate(body.splitlines()):
            match = _LINE_MARKER.match(line)
            number = first + index
            if match and number not in seen:
                seen.add(number)
                item = _marker_item(
                    rel, lines, number, match.group(1), match.group(2), kind
                )
                items.append(_add_age(item, filepath, project_root, use_git))
    return sorted(items, key=lambda i: i["line"])


def _docstring_nodes(tree: ast.AST) -> set[int]:
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                found.add(id(body[0].value))
    return found


def scan_python_strings(
    filepath: Path, project_root: Path, use_git: bool
) -> list[dict]:
    """Markers that open a docstring line (`TODO: ...`, a `Todo:` section) and
    placeholder strings whose value starts with TODO or FIXME. A line the `#`
    patterns already matched is not counted twice."""
    try:
        source = filepath.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source)
    except (OSError, SyntaxError, ValueError, RecursionError):
        return []
    lines = source.splitlines()
    rel = str(filepath.relative_to(project_root))
    docstrings = _docstring_nodes(tree)
    hash_categories = ("TODO", "FIXME", "HACK", "XXX")

    def counted(number: int) -> bool:
        line = lines[number - 1] if 0 < number <= len(lines) else ""
        return any(_MARKER_PATTERNS[c].search(line) for c in hash_categories)

    items: list[dict] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if id(node) in docstrings:
            for index, line in enumerate(node.value.splitlines()):
                match = _LINE_MARKER.match(line)
                number = node.lineno + index
                if match and not counted(number):
                    items.append(
                        _marker_item(
                            rel,
                            lines,
                            number,
                            match.group(1),
                            match.group(2),
                            "docstring",
                        )
                    )
            continue
        match = _STRING_MARKER.match(node.value)
        if match and not counted(node.lineno):
            items.append(
                _marker_item(
                    rel, lines, node.lineno, match.group(1), match.group(2), "string"
                )
            )
    items.sort(key=lambda i: i["line"])
    return [_add_age(item, filepath, project_root, use_git) for item in items]


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
# A ticked task-list item (`- [x] ABC-3 ...`) is done.
_TICKED_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\[[xX]\]\s")
_STRUCK_CELL = re.compile(r"^~~.+~~$")


# A forge issue or pull-request URL: its key is `#N`, so it meets the `#N`
# mentions in changelogs and commit messages.
_FORGE_URL = re.compile(
    r"https?://\S+?/(?:-/)?(?:issues|pull|pulls|merge_requests)/(\d+)\b\S*"
)
_LEADING_ZEROS = re.compile(r"(?<!\d)0+(?=\d)")


def canonical_key(match: re.Match[str]) -> str:
    """The key a `--key-regex` match stands for.

    A named group `key` (or else the first group that matched) is the key,
    when the regex has groups; a forge URL becomes `#N`; zero padding in a
    number is dropped (`ABC-007` and `ABC-7` are one key).
    """
    text = match.group(0)
    if match.re.groups:
        named = match.groupdict().get("key")
        group = (
            named if named is not None else next((g for g in match.groups() if g), None)
        )
        if group:
            text = group
    forge = _FORGE_URL.fullmatch(text)
    if forge:
        text = f"#{forge.group(1)}"
    return _LEADING_ZEROS.sub("", text)


def _row_keys(line: str, pattern: re.Pattern[str]) -> tuple[set[str], bool]:
    """``(keys the line is about, whether it marks them done)``.

    A table row is about the keys in its first key-bearing cell; other keys
    in the row are cross-references. Roadmaps often strike the TASK cell
    rather than the key (`| ABC-1 | ~~Faster index~~ | v1.0 |`), which only
    `~~ABC-1~~` used to recognise, so such done rows read as open. A ticked
    task-list item is about its first key.
    """
    stripped = line.strip()
    if stripped.startswith("|"):
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        for index, cell in enumerate(cells):
            own = {canonical_key(m) for m in pattern.finditer(cell)}
            if own:
                others = cells[:index] + cells[index + 1 :]
                return own, any(_STRUCK_CELL.match(c) for c in others if c)
        return set(), False
    if _TICKED_ITEM.match(line):
        first = pattern.search(line)
        return ({canonical_key(first)} if first else set()), True
    return set(), False


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


def _git_log_mentions(
    project_root: Path, pattern: re.Pattern[str], *, unmerged: bool = False
) -> dict | None:
    """``{key: {"commits": n, "last_date": iso, "refs": set}}`` from one local
    `git log` pass over HEAD -- or, with *unmerged*, over the commits on local
    and remote branches that HEAD does not contain (``refs`` names them).

    Returns None when git is unavailable or fails, so the caller can say the
    lookup was not made rather than report zero commits.
    """
    scope = ["--branches", "--remotes", "--not", "HEAD", "--source"] if unmerged else []
    try:
        result = subprocess.run(
            [
                "git",
                "log",
                *scope,
                f"--max-count={_GIT_LOG_MAX_COUNT}",
                "--format=%S%x00%aI%x00%s%x00%b%x1e",
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
        if len(parts) < 3:
            continue
        ref, date = parts[0], parts[1]
        for key in {canonical_key(m) for m in pattern.finditer(" ".join(parts[2:]))}:
            entry = mentions.setdefault(
                key, {"commits": 0, "last_date": None, "refs": set()}
            )
            entry["commits"] += 1
            # git log is newest-first, so the first date seen is the latest.
            entry["last_date"] = entry["last_date"] or date
            if unmerged and ref:
                entry["refs"].add(ref)
    return mentions


def _natural_key(key: str) -> list:
    """Sort `B2` before `B10`."""
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", key)]


def key_variants(
    spellings: dict[str, dict[str, list[str]]], seen: set[str]
) -> list[dict]:
    """Keys written more than one way, and bare keys beside lettered parts."""
    out: list[dict] = []
    for key in sorted(spellings, key=_natural_key):
        forms = spellings[key]
        if len(forms) > 1:
            out.append(
                {
                    "key": key,
                    "kind": "spelling",
                    "spellings": dict(sorted(forms.items())),
                }
            )
    for key in sorted(seen, key=_natural_key):
        if not key[-1:].isdigit():
            continue
        suffixed = sorted(
            k
            for k in seen
            if len(k) == len(key) + 1
            and k.startswith(key)
            and k[-1].isalpha()
            and k[-1].islower()
        )
        if suffixed:
            out.append(
                {
                    "key": key,
                    "kind": "bare_prefix",
                    "suffixed": suffixed,
                    "spellings": dict(sorted(spellings.get(key, {}).items())),
                }
            )
    return out


_MAX_GAP_RANGE = 2000
_MAX_GAPS_LISTED = 100


def unaccounted_numbers(seen: set[str]) -> tuple[dict, dict]:
    """``({prefix: gap info}, {prefix: why skipped})`` for numbered keys.

    `#N` keys are skipped: on a forge, most numbers belong to issues nobody
    cites in the code, so the gaps say nothing.
    """
    numbers: dict[str, set[int]] = {}
    for key in seen:
        match = re.fullmatch(r"(.*?)(\d+)[a-z]?", key)
        if match:
            numbers.setdefault(match.group(1), set()).add(int(match.group(2)))
    gaps: dict[str, dict] = {}
    skipped: dict[str, str] = {}
    for prefix in sorted(numbers):
        seen_numbers = numbers[prefix]
        if prefix in ("#", "") or prefix.endswith("#"):
            skipped[prefix or "(bare number)"] = "forge numbers: gaps are expected"
            continue
        if len(seen_numbers) < 3:
            continue
        low, high = min(seen_numbers), max(seen_numbers)
        if high - low > _MAX_GAP_RANGE:
            skipped[prefix] = f"range {low}-{high} too wide to list"
            continue
        if len(seen_numbers) * 2 < high - low + 1:
            skipped[prefix] = (
                f"sparse: {len(seen_numbers)} of {high - low + 1} numbers seen"
            )
            continue
        missing = [n for n in range(low, high + 1) if n not in seen_numbers]
        if missing:
            gaps[prefix] = {
                "min": low,
                "max": high,
                "seen": len(seen_numbers),
                "missing_count": len(missing),
                "missing": missing[:_MAX_GAPS_LISTED],
            }
    return gaps, skipped


def collect_tracker_keys(
    project_root: Path,
    py_files: list[Path],
    pattern: re.Pattern[str],
    use_git: bool,
    *,
    scan_root: Path | None = None,
) -> dict:
    """Inventory every tracker key cited in code comments, docstrings and docs.

    Per key: where code cites it, where docs cite it (with the line, so a
    roadmap row's target version is visible), whether a changelog mentions it,
    whether a doc strikes it through (`~~ABC-12~~`, a common "done" mark in
    roadmap tables), and how many commits mention it. A key cited in code with
    none of those resolution signals is listed in ``open_in_code``.
    """
    keys: dict[str, dict] = {}
    # canonical key -> spelling -> "file:line" locations
    spellings: dict[str, dict[str, list[str]]] = {}

    def spelled(key: str, match: re.Match[str], where: str) -> None:
        places = spellings.setdefault(key, {}).setdefault(match.group(0), [])
        if len(places) < _MAX_REFS_PER_KEY:
            places.append(where)

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
                "non_changelog_doc_refs": 0,
                "own_rows": 0,
                "own_row_struck": False,
                "code_refs": [],
                "doc_refs": [],
            },
        )

    for path in py_files:
        rel = str(path.relative_to(project_root))
        for line, text in _python_comment_lines(path):
            for match in pattern.finditer(text):
                key = canonical_key(match)
                spelled(key, match, f"{rel}:{line}")
                item = entry(key)
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
                canonical_key(m)
                for span in _STRIKE.finditer(line)
                for m in pattern.finditer(span.group(1))
            }
            own, row_done = _row_keys(line, pattern)
            first_match = pattern.search(line)
            first_key = canonical_key(first_match) if first_match else None
            if row_done:
                struck |= own
            for match in pattern.finditer(line):
                key = canonical_key(match)
                spelled(key, match, f"{rel}:{number}")
                item = entry(key)
                item["doc_ref_count"] += 1
                item["in_changelog"] |= is_changelog
                item["in_planning_doc"] |= is_planning
                if not is_changelog:
                    item["non_changelog_doc_refs"] += 1
                if key in own:
                    item["own_rows"] += 1
                    item["own_row_struck"] |= key in struck
                item["struck_through"] |= key in struck
                if len(item["doc_refs"]) < _MAX_REFS_PER_KEY:
                    item["doc_refs"].append(
                        {
                            "file": rel,
                            "line": number,
                            "struck": key in struck,
                            # The line is ABOUT this key (its table row, or
                            # its first key), not a passing cross-reference.
                            "primary": key in own if own else key == first_key,
                            "text": line.strip()[:200],
                        }
                    )

    for item in keys.values():
        # A key with a row of its own is done when that row says so; a strike
        # through text that merely mentions it in passing is not its status.
        if item.pop("own_rows"):
            item["struck_through"] = item["own_row_struck"]
        del item["own_row_struck"]

    git = _git_log_mentions(project_root, pattern) if use_git else None
    unmerged = (
        _git_log_mentions(project_root, pattern, unmerged=True) if use_git else None
    )
    for key, item in keys.items():
        found = (git or {}).get(key)
        # None, not 0, when git was not searched: unmeasured is not "no commits".
        item["commits"] = (
            found["commits"] if found else (0 if git is not None else None)
        )
        item["last_commit_date"] = found["last_date"] if found else None
        on_branches = (unmerged or {}).get(key)
        item["unmerged_commits"] = (
            on_branches["commits"]
            if on_branches
            else (0 if unmerged is not None else None)
        )
        item["unmerged_refs"] = sorted(on_branches["refs"])[:10] if on_branches else []

    # Keys that only commits on unmerged branches mention: work that exists
    # nowhere on this branch -- not in code, docs or its history.
    branch_only = [
        {
            "key": key,
            "commits": found["commits"],
            "refs": sorted(found["refs"])[:10],
            "last_commit_date": found["last_date"],
        }
        for key, found in sorted(
            (unmerged or {}).items(), key=lambda kv: _natural_key(kv[0])
        )
        if key not in keys and key not in (git or {})
    ]

    # A key whose every mention is a changelog line is history, not debt;
    # on a GitHub project nearly every `#NNN` key is one.
    changelog_only = [
        k
        for k in keys.values()
        if not k["code_ref_count"]
        and k["in_changelog"]
        and not k["non_changelog_doc_refs"]
    ]
    for k in changelog_only:
        del keys[k["key"]]

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
    # Open in a planning doc, yet a changelog mentions it: the row may be
    # stale, or the changelog may record only part of the work. Read both.
    conflicting_signals = [
        k["key"]
        for k in ordered
        if k["in_planning_doc"] and k["in_changelog"] and not k["struck_through"]
    ]
    seen_keys = set(keys) | {k["key"] for k in changelog_only}
    seen_keys |= set(git or {}) | {b["key"] for b in branch_only}
    variants = key_variants(spellings, seen_keys)
    gaps, gaps_skipped = unaccounted_numbers(seen_keys)
    return {
        "regex": pattern.pattern,
        "doc_files_scanned": [str(p.relative_to(project_root)) for p in docs],
        "doc_scope": (
            "docs are read from the project root and docs/ whatever the scan "
            "scope; code citations come from the scanned files only"
        ),
        # True when the scan was narrowed to a sub-directory: the docs (and
        # their keys) still come from the whole project.
        "docs_outside_scan_scope": scan_root is not None
        and scan_root.resolve() != project_root.resolve(),
        "git_log_searched": git is not None,
        "unmerged_branches_searched": unmerged is not None,
        "summary": {
            "keys_total": len(ordered) + len(changelog_only),
            "keys_only_in_changelog": len(changelog_only),
            "keys_cited_in_code": sum(1 for k in ordered if k["code_ref_count"]),
            "keys_in_changelog": sum(1 for k in ordered if k["in_changelog"])
            + len(changelog_only),
            "keys_struck_through": sum(1 for k in ordered if k["struck_through"]),
            "open_in_code": len(open_in_code),
            "open_in_planning_docs": len(open_in_planning_docs),
            "conflicting_signals": len(conflicting_signals),
            "branch_only": None if unmerged is None else len(branch_only),
            "key_variants": len(variants),
            "unaccounted": sum(g["missing_count"] for g in gaps.values()),
        },
        # Cited in code, but no changelog entry, strike-through or commit
        # mentions it: the keyed equivalent of an open TODO. Unresolved
        # LOCALLY: a forge issue or PR it names may be closed or merged.
        "open_in_code": open_in_code,
        "open_in_code_status": "unresolved_locally",
        "open_in_code_note": (
            "only local signals were checked (changelog, strike-through, "
            "commits); the script never contacts the forge. Check each "
            "forge_refs_to_check number (gh issue view N --json state) "
            "before reporting it open"
        ),
        # `#N` keys among open_in_code: issue or PR numbers whose state only
        # the forge knows.
        "forge_refs_to_check": [k for k in open_in_code if re.fullmatch(r"#?\d+", k)],
        # Listed in a roadmap/todo/backlog/plan file, not struck through and in
        # no changelog: planned work still open. `doc_refs[].text` carries the
        # row, which usually names the target version.
        "open_in_planning_docs": open_in_planning_docs,
        "conflicting_signals": conflicting_signals,
        # Mentioned only by commits on branches not merged into HEAD (local
        # and remote): null when the branches were not searched.
        "branch_only": None if unmerged is None else branch_only,
        # One key written several ways (`ABC-07` and `ABC-7`, a URL and `#7`,
        # a capture-group regex), or a bare key beside its lettered parts
        # (`ABC-5` and `ABC-5a`): each spelling with its locations.
        "key_variants": variants,
        # Per key prefix: numbers between the lowest and highest seen that no
        # code, doc or commit mentions -- lost or unrecorded items.
        "unaccounted": gaps,
        "unaccounted_skipped": gaps_skipped,
        # Keys whose every mention is in a changelog; their detail is omitted.
        "changelog_only_keys": [
            k["key"]
            for k in sorted(changelog_only, key=lambda k: _natural_key(k["key"]))
        ],
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
# A section header, optionally followed by a comment (`[tool.x]  # note`).
_SECTION = re.compile(r"^\s*\[+\s*([^\]]+?)\s*\]+\s*(?:#.*)?$")

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


# --- roadmap targets and dependency specs -----------------------------------

_VERSION_TOKEN = re.compile(r"(?<![\w.\-/])v?(\d+(?:\.\d+){1,2})(?![\w.]*\d)(?![\w-])")


def _version_tuple(text: str) -> tuple[int, ...] | None:
    match = re.match(r"v?(\d+(?:\.\d+)*)", text.strip())
    if not match:
        return None
    parts = [int(p) for p in match.group(1).split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()  # 1.2.0 == 1.2
    return tuple(parts)


def project_version(project_root: Path, use_git: bool) -> tuple[str | None, str]:
    """``(version, where it came from)`` -- or ``(None, why not)``.

    Static metadata first (`pyproject.toml`, `setup.cfg`, `setup.py`), then a
    `__version__` in a top-level package, then the latest local git tag.
    """
    sections = {
        "pyproject.toml": ("project", "tool.poetry"),
        "setup.cfg": ("metadata",),
    }
    for name, wanted in sections.items():
        path = project_root / name
        if not path.is_file():
            continue
        section = ""
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            header = _SECTION.match(line)
            if header:
                section = header.group(1)
                continue
            key, sep, value = line.partition("=")
            if sep and key.strip() == "version" and section in wanted:
                value = value.strip().strip("\"'")
                if _version_tuple(value):
                    return value, f"{name} [{section}]"
    setup = project_root / "setup.py"
    if setup.is_file():
        try:
            match = re.search(
                r"""\bversion\s*=\s*["']([^"']+)["']""",
                setup.read_text(encoding="utf-8", errors="replace"),
            )
        except OSError:
            match = None
        if match and _version_tuple(match.group(1)):
            return match.group(1), "setup.py"
    for pattern in ("*/__init__.py", "*/_version.py", "*/version.py", "*/__about__.py",
                    "src/*/__init__.py", "src/*/_version.py", "src/*/version.py"):  # fmt: skip
        for path in sorted(project_root.glob(pattern)):
            if _is_excluded(path, project_root):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            match = re.search(
                r"""^__version__\s*(?::\s*\w+\s*)?=\s*["']([^"']+)["']""",
                text,
                re.MULTILINE,
            )
            if match and _version_tuple(match.group(1)):
                return match.group(1), str(path.relative_to(project_root))
    if use_git:
        try:
            result = subprocess.run(
                ["git", "describe", "--tags", "--abbrev=0"],
                capture_output=True,
                text=True,
                cwd=str(project_root),
                timeout=10,
                check=False,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            result = None
        tag = result.stdout.strip() if result and result.returncode == 0 else ""
        if _version_tuple(tag.lstrip("v")):
            return tag, "latest git tag"
    return None, "no static version, __version__ or version tag found"


def overdue_planned_items(tracker: dict, project_root: Path, use_git: bool) -> dict:
    """Open planning-doc rows whose target version is at or below the
    project's current version: promised for a release that has shipped."""
    version, source = project_version(project_root, use_git)
    result: dict = {
        "current_version": version,
        "current_version_source": source if version else None,
        "items": None if version is None else [],
    }
    if version is None:
        result["reason"] = source
        return result
    current = _version_tuple(version)
    by_key = {k["key"]: k for k in tracker["keys"]}
    for key in tracker["open_in_planning_docs"]:
        for ref in by_key.get(key, {}).get("doc_refs", []):
            if (
                ref["struck"]
                or not ref.get("primary")
                or not Path(ref["file"]).name.lower().startswith(_PLANNING_STEMS)
            ):
                continue
            targets = [
                t
                for t in (
                    _version_tuple(m) for m in _VERSION_TOKEN.findall(ref["text"])
                )
                if t
            ]
            if not targets:
                continue
            target = max(targets)
            if target <= current:
                result["items"].append(
                    {
                        "key": key,
                        "target_version": ".".join(map(str, target)),
                        "file": ref["file"],
                        "line": ref["line"],
                        "text": ref["text"],
                    }
                )
                break
    return result


_LOCKFILE_NAMES = ("uv.lock", "poetry.lock", "Pipfile.lock", "pdm.lock", "pylock.toml")
_INSTALL = re.compile(
    r"\b(?:uv\s+pip\s+install|pip3?\s+install|python3?\s+-m\s+pip\s+install"
    r"|uv\s+sync|poetry\s+install|pdm\s+(?:install|sync)|pipenv\s+(?:install|sync))\b.*"
)
_SPEC_CLAUSE = re.compile(r"(===|==|~=|>=|<=|!=|>|<)\s*([^,;\s]+)")


def _parse_requirement(text: str) -> dict | None:
    text = text.split("#", 1)[0].strip()
    match = re.match(
        r"([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*(\(?[^;@]*\)?)\s*(@\s*\S+)?\s*(;.*)?$",
        text,
    )
    if not match or text.startswith(("-", ".", "/")):
        return None
    clauses = _SPEC_CLAUSE.findall(match.group(3) or "")
    lower = upper = None
    for op, ver in clauses:
        if op in (">=", ">", "~=", "==", "==="):
            lower = ver
        if op in ("<", "<=", "==", "==="):
            upper = ver
        if op == "~=":
            upper = f"next release after {ver}"
    return {
        "name": match.group(1),
        "spec": (match.group(3) or "").strip() or None,
        "lower": lower,
        "upper": upper,
        "direct_reference": bool(match.group(4)),
        "unbounded": not clauses and not match.group(4),
        "floor_only": lower is not None and upper is None,
        "marker": (match.group(5) or "").lstrip(";").strip() or None,
    }


def _site_packages(project_root: Path) -> tuple[Path | None, str]:
    """The project's environment: $VIRTUAL_ENV, else `.venv`/`venv`/`env` in
    the project root. Never the interpreter running this script."""
    import os

    candidates = []
    if os.environ.get("VIRTUAL_ENV"):
        candidates.append(Path(os.environ["VIRTUAL_ENV"]))
    candidates += [project_root / n for n in (".venv", "venv", "env")]
    for env in candidates:
        for site in sorted(env.glob("lib/python*/site-packages")) + sorted(
            env.glob("Lib/site-packages")
        ):
            if site.is_dir():
                return site, str(env)
    return None, (
        "no project environment found ($VIRTUAL_ENV, .venv, venv, env); the "
        "toolkit's own interpreter is not the project's, so it is not used"
    )


def _installed_versions(site: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    for meta in site.glob("*.dist-info"):
        name, _, version = meta.name[: -len(".dist-info")].partition("-")
        found[re.sub(r"[-_.]+", "-", name).lower()] = version
    return found


def _is_lock_path(path: Path) -> bool:
    """A requirements file an install reads that pins everything (hashes,
    pip-compile output) -- installing from it is installing from a lock."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    return path.suffix == ".lock" or _is_lockfile(lines)


def _setup_py_requirements(path: Path) -> list[tuple[int, str, str]]:
    """``(line, requirement, group)`` from literal `install_requires` and
    `extras_require` arguments of a `setup()` call."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError, ValueError, RecursionError):
        return []
    out: list[tuple[int, str, str]] = []

    def strings(node: ast.AST, group: str) -> None:
        if isinstance(node, (ast.List, ast.Tuple)):
            for elt in node.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    out.append((elt.lineno, elt.value, group))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not (
            isinstance(node.func, (ast.Name, ast.Attribute))
            and (getattr(node.func, "id", None) or getattr(node.func, "attr", None))
            == "setup"
        ):
            continue
        for keyword in node.keywords:
            if keyword.arg == "install_requires":
                strings(keyword.value, "dependencies")
            elif keyword.arg == "extras_require" and isinstance(
                keyword.value, ast.Dict
            ):
                for key, value in zip(keyword.value.keys, keyword.value.values):
                    if isinstance(key, ast.Constant):
                        strings(value, f"optional:{key.value}")
    return out


def collect_dependency_specs(project_root: Path) -> dict:
    """Declared dependencies with their bounds and the installed version,
    lockfiles, and how CI and container builds install."""
    declared: list[dict] = []

    def add(rel: str, number: int, text: str, group: str) -> None:
        req = _parse_requirement(text)
        if req:
            declared.append({"file": rel, "line": number, "group": group, **req})

    pyproject = project_root / "pyproject.toml"
    if pyproject.is_file():
        lines = pyproject.read_text(encoding="utf-8", errors="replace").splitlines()
        section, in_list, group = "", False, None
        for i, line in enumerate(lines):
            header = _SECTION.match(line)
            if header:
                section, in_list = header.group(1), False
                continue
            stripped = line.strip()
            key, sep, value = line.partition("=")
            key = key.strip().strip('"')
            if sep and not in_list:
                if section == "project" and key == "dependencies":
                    group = "dependencies"
                elif section == "project.optional-dependencies":
                    group = f"optional:{key}"
                elif section == "dependency-groups":
                    group = f"group:{key}"
                else:
                    continue
                in_list = "[" in value and "]" not in value.split("#", 1)[0]
                for match in _TOML_STRING.finditer(value.split("#", 1)[0]):
                    add(
                        "pyproject.toml",
                        i + 1,
                        next(g for g in match.groups() if g is not None),
                        group,
                    )
                continue
            if in_list:
                for match in _TOML_STRING.finditer(stripped.split("#", 1)[0]):
                    add(
                        "pyproject.toml",
                        i + 1,
                        next(g for g in match.groups() if g is not None),
                        group,
                    )
                if stripped.startswith("]"):
                    in_list = False
    setup_py = project_root / "setup.py"
    if setup_py.is_file():
        for number, text, group in _setup_py_requirements(setup_py):
            add("setup.py", number, text, group)
    setup_cfg = project_root / "setup.cfg"
    if setup_cfg.is_file():
        lines = setup_cfg.read_text(encoding="utf-8", errors="replace").splitlines()
        section = ""
        for i, line in enumerate(lines):
            header = _SECTION.match(line)
            if header:
                section = header.group(1)
                continue
            key, sep, _ = line.partition("=")
            key = key.strip()
            if not sep or line[:1] in (" ", "\t"):
                continue
            if section == "options" and key == "install_requires":
                group = "dependencies"
            elif section == "options.extras_require":
                group = f"optional:{key}"
            else:
                continue
            _value, end = _value_lines(lines, i, False)
            parts = [line.partition("=")[2], *lines[i + 1 : end + 1]]
            for offset, part in enumerate(parts):
                if part.strip():
                    add("setup.cfg", i + offset + 1, part.strip(), group)
    lockfiles = [n for n in _LOCKFILE_NAMES if (project_root / n).is_file()]
    for pattern in _REQUIREMENT_GLOBS:
        for path in sorted(project_root.glob(pattern)):
            rel = str(path.relative_to(project_root))
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            if _is_lockfile(lines):
                lockfiles.append(rel)
                continue
            for i, line in enumerate(lines):
                add(rel, i + 1, line, rel)
    site, env_source = _site_packages(project_root)
    installed = _installed_versions(site) if site else {}
    for dep in declared:
        version = installed.get(re.sub(r"[-_.]+", "-", dep["name"]).lower())
        dep["installed"] = version if site else None
        floor = _version_tuple(dep["lower"]) if dep["lower"] else None
        have = _version_tuple(version) if version else None
        dep["installed_below_floor"] = have < floor if floor and have else None
    install_sites: list[dict] = []
    sources = [p for g in _CI_GLOBS for p in sorted(project_root.glob(g))]
    sources += sorted(project_root.glob("Dockerfile*")) + sorted(
        project_root.glob("*/Dockerfile*")
    )
    for name in ("Makefile", "tox.ini", "noxfile.py"):
        if (project_root / name).is_file():
            sources.append(project_root / name)
    for path in sources:
        if not path.is_file() or _is_excluded(path, project_root):
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        rel = str(path.relative_to(project_root))
        for i, line in enumerate(lines):
            match = _INSTALL.search(line)
            if not match or line.lstrip().startswith("#"):
                continue
            command = match.group(0).strip()
            referenced = re.findall(
                r"(?:-r|--requirement|-c|--constraint)[=\s]+(\S+)", command
            )
            uses_lock = any(
                _is_lock_path(project_root / ref) for ref in referenced
            ) or bool(
                re.search(
                    r"--frozen|--locked|--require-hashes|--deploy|--frozen-lockfile",
                    command,
                )
                or re.search(r"\b(?:poetry install|pipenv sync|pdm sync)\b", command)
                or any(lock in command for lock in lockfiles)
            )
            install_sites.append(
                {
                    "file": rel,
                    "line": i + 1,
                    "command": command[:200],
                    "uses_lockfile": uses_lock,
                }
            )
    return {
        "declared": declared,
        "floor_only": sorted({d["name"] for d in declared if d["floor_only"]}),
        "unbounded": sorted({d["name"] for d in declared if d["unbounded"]}),
        "installed_from": env_source if site else None,
        "installed_reason": None if site else env_source,
        "lockfiles": lockfiles,
        "install_sites": install_sites,
    }


_TOML_STRING = re.compile(
    r"'''(.*?)'''"
    r'|"""(.*?)"""'
    r'|"((?:[^"\\]|\\.)*)"'
    r"|'([^']*)'"
)
_PYTEST_CONFIGS = ("pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini")


def _filter_entries(
    lines: list[str], start: int, is_toml: bool
) -> list[tuple[int, str, str | None]]:
    """``(line index, entry, comment above)`` for each `filterwarnings` entry
    of the setting on ``lines[start]``."""
    entries: list[tuple[int, str, str | None]] = []
    comment: list[str] = []
    _, _, first = lines[start].partition("=")
    if is_toml:
        depth = 0
        index = start
        text = first
        while True:
            stripped = text.strip()
            if stripped.startswith("#"):
                comment.append(stripped.lstrip("#").strip())
            else:
                for match in _TOML_STRING.finditer(text.split(" #", 1)[0]):
                    value = next(g for g in match.groups() if g is not None)
                    entries.append((index, value, " ".join(comment) or None))
                    comment = []
                if not stripped:
                    comment = []
            depth += text.count("[") - text.count("]")
            if depth <= 0 or index + 1 >= len(lines):
                break
            index += 1
            text = lines[index]
        return entries
    if first.strip():
        entries.append((start, first.strip(), None))
    index = start + 1
    while index < len(lines) and lines[index][:1] in (" ", "\t"):
        stripped = lines[index].strip()
        if not stripped:
            break
        if stripped.startswith("#"):
            comment.append(stripped.lstrip("#").strip())
        else:
            entries.append((index, stripped, " ".join(comment) or None))
            comment = []
        index += 1
    return entries


def collect_warning_filters(project_root: Path, use_git: bool) -> list[dict]:
    """Each pytest `filterwarnings` entry on its own, with its age.

    The suppressions list shows the setting as one joined value; a stale
    `ignore` is debt one entry at a time, so each gets its line, its action,
    the comment above it (often a link to the upstream issue) and its age.
    """
    out: list[dict] = []
    for name in _PYTEST_CONFIGS:
        path = project_root / name
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        is_toml = path.suffix == ".toml"
        section = ""
        for i, line in enumerate(lines):
            header = _SECTION.match(line)
            if header:
                section = header.group(1)
                continue
            key, sep, _ = line.partition("=")
            if not sep or key.strip().strip('"').lower() != "filterwarnings":
                continue
            if _tool_of(section, path.name) != "pytest" and not (
                name == "pytest.ini" or section.lower() == "pytest"
            ):
                continue
            for index, entry, comment in _filter_entries(lines, i, is_toml):
                item = {
                    "file": name,
                    "line": index + 1,
                    "entry": entry[:300],
                    "action": entry.split(":", 1)[0].strip().lower(),
                    "comment_above": comment[:200] if comment else None,
                }
                blame = (
                    _git_blame_line(path, index + 1, project_root) if use_git else None
                )
                item["date"] = blame["date"] if blame else None
                item["age"] = _classify_age(blame["date"]) if blame else "unknown"
                out.append(item)
    return out


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
        all_items.extend(scan_python_strings(f, project_root, use_git))
    # Templates, stylesheets and scripts carry markers in their own comments.
    text_files = discover_text_debt_files(scan_root)
    text_files_by_kind: dict[str, int] = {}
    for f in text_files:
        all_items.extend(scan_text_file(f, project_root, use_git))
        kind = _text_kind(f) or "other"
        text_files_by_kind[kind] = text_files_by_kind.get(kind, 0) + 1

    # Aggregate stats.
    by_category: dict[str, int] = {}
    by_age: dict[str, int] = {}
    by_file: dict[str, int] = {}
    by_source: dict[str, int] = {}
    for item in all_items:
        source = item.get("source", "comment")
        by_source[source] = by_source.get(source, 0) + 1
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
            # Where markers were found: `comment` (a `#` comment), `docstring`,
            # `string` (a placeholder value), `template`, `stylesheet`, `script`.
            "by_source": dict(sorted(by_source.items(), key=lambda x: -x[1])),
            "non_python_files_scanned": dict(sorted(text_files_by_kind.items())),
            "top_files": [{"file": f, "count": c} for f, c in top_files],
        },
        "items": all_items,
        # Read from the project root whatever the scan scope: pins with their
        # comments, project-wide tool suppressions, CI failure allowances and
        # TODO-style comments in config, requirements and CI files.
        "config_debt": collect_config_debt(project_root),
        # Declared dependencies (bounds, installed version from the project's
        # own environment), lockfiles and how CI/containers install.
        "dependency_specs": collect_dependency_specs(project_root),
    }
    # Each pytest filterwarnings entry on its own, with its age.
    output["config_debt"]["warning_filters"] = collect_warning_filters(
        project_root, use_git
    )
    output["summary"]["config_debt"] = {
        key: len(output["config_debt"][key])
        for key in (
            "pins", "routine_caps", "suppressions", "ci", "markers",
            "warning_filters",
        )
    }  # fmt: skip
    # What is read from the project root whatever the scan scope.
    output["read_from_project_root"] = [
        "config_debt",
        "dependency_specs",
        *(["tracker_keys (docs)"] if key_pattern is not None else []),
    ]
    output["scan_scope_narrowed"] = scan_root.resolve() != project_root.resolve()
    if key_pattern is not None:
        output["tracker_keys"] = collect_tracker_keys(
            project_root, all_files, key_pattern, use_git, scan_root=scan_root
        )
        output["tracker_keys"]["overdue_planned_items"] = overdue_planned_items(
            output["tracker_keys"], project_root, use_git
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
