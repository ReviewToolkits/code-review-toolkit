"""Shared utilities for code-review-toolkit analysis scripts.

Every analysis script imports from here rather than re-implementing file
discovery, project-root detection, and CLI parsing. Before this module existed
`find_project_root` was byte-identical in all nine scripts and
`discover_python_files` had drifted into three divergent variants -- the exact
decay this module prevents.

Scripts add this directory to ``sys.path`` (the scripts directory is not a
package -- there is no ``__init__.py``) and then ``import scan_common``.
"""

from __future__ import annotations

import ast
import configparser
import fnmatch
import json
import re
import sys
from collections.abc import Generator, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

# Directories never worth walking into when discovering source files.
EXCLUDE_DIRS = frozenset(
    {
        ".git",
        ".tox",
        ".venv",
        "venv",
        "__pycache__",
        "node_modules",
        ".claude",
        ".eggs",
        "build",
        "dist",
    }
)

# Markers that identify the root of a Python project.
PROJECT_ROOT_MARKERS = frozenset({"pyproject.toml", "setup.cfg", "setup.py", ".git"})

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def find_project_root(start: Path) -> Path:
    """Walk upward from *start* to find the project root.

    Returns the first ancestor containing a project marker, or the starting
    directory if no marker is found.
    """
    current = start if start.is_dir() else start.parent
    while current != current.parent:
        if any((current / m).exists() for m in PROJECT_ROOT_MARKERS):
            return current
        current = current.parent
    return start if start.is_dir() else start.parent


def _is_excluded(path: Path, root: Path) -> bool:
    """True if *path* lives under an excluded directory relative to *root*."""
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        parts = path.parts
    if set(parts) & EXCLUDE_DIRS:
        return True
    return any(part.endswith(".egg-info") for part in parts)


def discover_python_files(root: Path) -> Generator[Path, None, None]:
    """Yield ``.py`` files under *root*, excluding common non-source dirs.

    Accepts a file as well as a directory: a single ``.py`` file yields itself,
    anything else yields nothing. Results are sorted for deterministic output.
    """
    if root.is_file():
        if root.suffix == ".py":
            yield root
        return
    for path in sorted(root.rglob("*.py")):
        if _is_excluded(path, root):
            continue
        yield path


def collect_python_files(root: Path, max_files: int = 0) -> tuple[list[Path], int]:
    """Return ``(files, files_total)`` under *root*, capped at *max_files*.

    ``files_total`` is the count *before* the cap, so callers can report how
    many files were skipped. ``max_files <= 0`` means no limit.
    """
    all_files = list(discover_python_files(root))
    files_total = len(all_files)
    if max_files > 0 and files_total > max_files:
        all_files = all_files[:max_files]
    return all_files, files_total


# ---------------------------------------------------------------------------
# --exclude: one shared path filter for every script that discovers files
# ---------------------------------------------------------------------------

_GLOB_CHARS = frozenset("*?[")


def split_patterns(values: Iterable[str] | str | None) -> list[str]:
    """Flatten repeatable, comma-separated pattern arguments into a list.

    ``["a,b", "c"]`` -> ``["a", "b", "c"]``. Empty items are dropped and the
    order is kept (duplicates removed), so the per-pattern match counts read
    in the order the user wrote them.
    """
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    out: list[str] = []
    for value in values:
        for item in value.split(","):
            item = item.strip()
            if item and item not in out:
                out.append(item)
    return out


def path_matches(rel: str, pattern: str) -> bool:
    """True when the project-relative path *rel* matches one ``--exclude`` pattern.

    - A pattern with no glob character is a SUBSTRING of the path (``vendor``
      matches ``pkg/vendor/x.py``). This is what the scanners' ``--exclude``
      always meant, so existing invocations keep working.
    - A glob (``*``, ``?``, ``[...]``) is matched against the whole path and
      against every trailing sub-path, like an unanchored ``.gitignore`` line:
      ``migrations/*`` matches ``pkg/migrations/versions/a.py``, and
      ``*.pyi`` matches a stub at any depth. ``*`` crosses ``/``.
    - A trailing ``/`` means a directory: ``build/`` matches everything under
      any directory named ``build``.
    """
    rel = rel.replace("\\", "/")
    if not _GLOB_CHARS & set(pattern):
        if pattern.endswith("/"):
            return f"/{pattern}" in f"/{rel}"
        return pattern in rel
    pattern = pattern.rstrip("/") if pattern.endswith("/") else pattern
    parts = rel.split("/")
    for i in range(len(parts)):
        tail = "/".join(parts[i:])
        if fnmatch.fnmatchcase(tail, pattern) or fnmatch.fnmatchcase(
            tail, pattern + "/*"
        ):
            return True
    return False


def first_matching_pattern(rel: str, patterns: Iterable[str]) -> str | None:
    """The first of *patterns* that matches *rel*, or None."""
    for pattern in patterns:
        if path_matches(rel, pattern):
            return pattern
    return None


# ---------------------------------------------------------------------------
# Migration revision layouts (Alembic, Django)
# ---------------------------------------------------------------------------

# Only files under one of these directory names are read for the content test.
_MIGRATION_DIRS = frozenset({"migrations", "migration", "alembic", "versions"})
_ALEMBIC_REVISION = re.compile(r"^revision\s*(?::[^=\n]*)?=", re.MULTILINE)
_ALEMBIC_DOWN_REVISION = re.compile(r"^down_revision\s*(?::[^=\n]*)?=", re.MULTILINE)
_DJANGO_MIGRATION = re.compile(
    r"^class\s+Migration\s*\(\s*(?:[\w.]*\.)?Migration\s*\)", re.MULTILINE
)
_ALEMBIC_ENV = re.compile(r"^\s*from\s+alembic\s+import\s+.*\bcontext\b", re.MULTILINE)


def migration_kind(path: Path) -> str | None:
    """``"revision"``/``"env"`` when *path* is a schema-migration file, else None.

    A migration framework loads these files by path, never by import, so a
    static scan sees them as orphan modules with dead ``upgrade``/``downgrade``
    functions, untested sources, and hundreds of near-identical functions that
    swamp complexity and annotation counts.

    - ``revision``: an Alembic revision (module-level ``revision`` AND
      ``down_revision``) or a Django migration (``class Migration(...Migration)``).
    - ``env``: an Alembic ``env.py`` (imports ``context`` from alembic, or sits
      beside a ``versions/`` directory).

    Only files under a ``migrations``/``migration``/``alembic``/``versions``
    directory are considered, so a module that happens to define a
    ``revision`` variable elsewhere is never skipped.
    """
    if path.suffix != ".py" or path.name == "__init__.py":
        return None
    if not _MIGRATION_DIRS & set(path.parent.parts):
        return None
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            head = handle.read(65536)
    except OSError:
        return None
    if path.name == "env.py":
        if (path.parent / "versions").is_dir() or _ALEMBIC_ENV.search(head):
            return "env"
        return None
    if _ALEMBIC_REVISION.search(head) and _ALEMBIC_DOWN_REVISION.search(head):
        return "revision"
    if _DJANGO_MIGRATION.search(head):
        return "revision"
    return None


# ---------------------------------------------------------------------------
# File selection: discovery + --exclude + migration skipping + --max-files
# ---------------------------------------------------------------------------


@dataclass
class FileSelection:
    """The files a script analyzes, and what was left out and why.

    ``files_total`` counts the files left AFTER ``--exclude`` and migration
    skipping and BEFORE ``--max-files``, so ``files_capped`` (analyzed <
    total) still means only "the cap truncated this scan", and an excluded
    file never consumes a slot of the cap.
    """

    files: list[Path]
    files_total: int
    exclude_patterns: list[str] = field(default_factory=list)
    exclude_matches: dict[str, int] = field(default_factory=dict)
    excluded: list[str] = field(default_factory=list)
    # None: this script does not look for migrations at all.
    migrations: list[str] | None = None
    migrations_skipped: bool = False

    def envelope_fields(self) -> dict:
        """Fields to merge into the JSON envelope (see :func:`build_envelope`)."""
        fields: dict = {
            "files_excluded": len(self.excluded),
            "exclude_patterns": list(self.exclude_patterns),
            # Per pattern, in the order given: a pattern that matched nothing
            # shows 0, so a typo is visible rather than looking applied.
            "exclude_matches": dict(self.exclude_matches),
        }
        if self.migrations is not None:
            dirs = sorted({str(Path(rel).parent) for rel in self.migrations})
            fields["migration_files_detected"] = len(self.migrations)
            fields["migration_files_skipped"] = (
                len(self.migrations) if self.migrations_skipped else 0
            )
            fields["migration_dirs"] = dirs
        return fields


def select_python_files(
    root: Path,
    *,
    project_root: Path | None = None,
    max_files: int = 0,
    exclude: Iterable[str] | None = None,
    migrations: str | None = None,
) -> FileSelection:
    """Discover ``.py`` files under *root*, filter them, then cap them.

    *exclude* patterns (see :func:`path_matches`) are matched against each
    file's path relative to *project_root* (default: *root*). *migrations*
    is None (do not look), ``"detect"`` (report them, keep them) or
    ``"skip"`` (report them and leave them out). Filtering happens before
    the *max_files* cap and before counting.
    """
    base = project_root or (root if root.is_dir() else root.parent)
    patterns = split_patterns(exclude)
    matches = {p: 0 for p in patterns}
    excluded: list[str] = []
    found_migrations: list[str] | None = [] if migrations else None
    kept: list[Path] = []
    for path in discover_python_files(root):
        rel = relative_to_root(path, base).replace("\\", "/")
        if patterns:
            hit = first_matching_pattern(rel, patterns)
            if hit is not None:
                matches[hit] += 1
                excluded.append(rel)
                continue
        if found_migrations is not None and migration_kind(path):
            found_migrations.append(rel)
            if migrations == "skip":
                continue
        kept.append(path)
    files_total = len(kept)
    if max_files > 0 and files_total > max_files:
        kept = kept[:max_files]
    return FileSelection(
        files=kept,
        files_total=files_total,
        exclude_patterns=patterns,
        exclude_matches=matches,
        excluded=excluded,
        migrations=found_migrations,
        migrations_skipped=migrations == "skip",
    )


# ---------------------------------------------------------------------------
# Coverage configuration: the project's own `omit`
# ---------------------------------------------------------------------------


def coverage_omit_patterns(project_root: Path) -> tuple[list[str], str | None]:
    """The project's coverage ``[run] omit`` patterns and where they came from.

    Read in coverage.py's own order: ``.coveragerc``, ``setup.cfg``
    (``[coverage:run]``), ``tox.ini`` (``[coverage:run]``), then
    ``pyproject.toml`` (``[tool.coverage.run]``); the first file that
    sets ``omit`` wins. Returns ``([], None)`` when none does.
    """
    ini_sources = (
        (".coveragerc", "run"),
        ("setup.cfg", "coverage:run"),
        ("tox.ini", "coverage:run"),
    )
    for name, section in ini_sources:
        path = project_root / name
        if not path.is_file():
            continue
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(path, encoding="utf-8")
        except (configparser.Error, UnicodeDecodeError, OSError):
            continue
        if parser.has_option(section, "omit"):
            raw = parser.get(section, "omit")
            patterns = [p.strip() for p in re.split(r"[,\n]", raw) if p.strip()]
            return patterns, f"{name} [{section}]"
    pyproject = project_root / "pyproject.toml"
    if pyproject.is_file():
        try:
            import tomllib
        except ImportError:  # Python 3.10: no tomllib, so not measured
            return [], None
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError):
            return [], None
        omit = data.get("tool", {}).get("coverage", {}).get("run", {}).get("omit")
        if isinstance(omit, str):
            omit = [omit]
        if isinstance(omit, list) and omit:
            return [str(p) for p in omit], "pyproject.toml [tool.coverage.run]"
    return [], None


def coverage_omitted(path: Path, patterns: Iterable[str], config_dir: Path) -> bool:
    """True when coverage.py would omit *path* under *patterns*.

    As coverage.py does, a pattern that does not start with ``*`` or a path
    separator is taken relative to the configuration's directory. ``*``
    matches across ``/``.
    """
    target = str(path.resolve())
    for pattern in patterns:
        if pattern.startswith(("*", "/")):
            full = pattern
        else:
            full = str(config_dir.resolve() / pattern)
        if fnmatch.fnmatchcase(target, full):
            return True
    return False


def parse_source(path: Path) -> ast.Module | None:
    """Parse *path* into an AST, returning None on read or syntax errors.

    Analysis scripts run over arbitrary third-party trees where an unparseable
    file must never abort the whole scan.
    """
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    try:
        return ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return None


def relative_to_root(path: Path, root: Path) -> str:
    """Return *path* relative to *root* as a string, falling back to absolute."""
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


HELP_FLAGS = frozenset({"-h", "--help"})


def exit_on_help(argv: Sequence[str], doc: str | None = None) -> None:
    """Print usage and exit 0 when *argv* asks for help; otherwise return.

    Without this, ``--help`` fell into the "unknown flag, ignore it" branch and
    ``-h`` became the target path, so asking a script for its usage silently
    ran a full scan of the current directory. *doc* defaults to the running
    script's module docstring, which every script opens with a ``Usage:``
    section.
    """
    if not any(arg in HELP_FLAGS for arg in argv):
        return
    if doc is None:
        doc = getattr(sys.modules.get("__main__"), "__doc__", None)
    script = Path(sys.argv[0]).name if sys.argv and sys.argv[0] else "script"
    text = (doc or "").strip() or f"Usage: python {script} [path] [--max-files N]"
    sys.stdout.write(text + "\n")
    sys.exit(0)


def warn(message: str) -> None:
    """Write a one-line warning to stderr -- never to stdout, which carries JSON."""
    script = Path(sys.argv[0]).name if sys.argv and sys.argv[0] else "script"
    sys.stderr.write(f"{script}: warning: {message}\n")


@dataclass
class ScanArgs:
    """Parsed command line of an analysis script (see :func:`parse_scan_args`)."""

    target: str = "."
    max_files: int = 0
    exclude: list[str] = field(default_factory=list)
    # Boolean flags given, e.g. {"--include-tests"}.
    flags: set[str] = field(default_factory=set)
    # Value flags given, every occurrence in order: {"--key-type": ["B=fix"]}.
    values: dict[str, list[str]] = field(default_factory=dict)
    unknown: list[str] = field(default_factory=list)

    def value(self, flag: str, default: str | None = None) -> str | None:
        """The last value given for *flag*, or *default*."""
        given = self.values.get(flag)
        return given[-1] if given else default


def _is_flag(arg: str) -> bool:
    return arg.startswith("-") and len(arg) > 1 and not arg[1:2].isdigit()


def parse_scan_args(
    argv: Sequence[str],
    doc: str | None = None,
    *,
    flags: Iterable[str] = (),
    value_flags: Iterable[str] = (),
    exclude: bool = True,
) -> ScanArgs:
    """Parse an analysis script's command line.

    Always known: one positional target (default ``.``), ``--max-files N``
    and, when *exclude* is true, ``--exclude PAT[,PAT...]`` (repeatable; see
    :func:`path_matches`). The script declares its own boolean *flags* and
    *value_flags* (which take the next argument, or ``--flag=value``).

    Anything else starting with ``-`` is reported on stderr as an unknown
    flag and ignored, and so is a second positional argument. Silently
    dropping them made a mistyped or unsupported flag (``--exclude`` on a
    script without it) look accepted while the scan ran unfiltered.
    """
    exit_on_help(argv, doc)
    known_bool = set(flags)
    known_value = set(value_flags) | {"--max-files"}
    if exclude:
        known_value.add("--exclude")
    parsed = ScanArgs()
    positional: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        name, eq, inline = arg.partition("=")
        if name in known_value and _is_flag(name):
            if eq:
                value = inline
                i += 1
            elif i + 1 < len(argv):
                value = argv[i + 1]
                i += 2
            else:
                warn(f"{name} needs a value; ignored")
                parsed.unknown.append(arg)
                i += 1
                continue
            parsed.values.setdefault(name, []).append(value)
        elif arg in known_bool:
            parsed.flags.add(arg)
            i += 1
        elif _is_flag(arg):
            parsed.unknown.append(arg)
            i += 1
        else:
            positional.append(arg)
            i += 1
    if parsed.unknown:
        known = sorted(known_bool | known_value)
        warn(
            f"unknown flag(s) ignored: {' '.join(parsed.unknown)} "
            f"(this script accepts: {' '.join(known)})"
        )
    if len(positional) > 1:
        warn(
            f"only one target path is used ({positional[0]}); ignored: "
            f"{' '.join(positional[1:])}"
        )
    parsed.target = positional[0] if positional else "."
    raw_max = parsed.value("--max-files")
    if raw_max is not None:
        try:
            parsed.max_files = int(raw_max)
        except ValueError:
            emit({"error": f"--max-files requires an integer, got '{raw_max}'"})
            sys.exit(2)
    parsed.exclude = split_patterns(parsed.values.get("--exclude"))
    return parsed


def parse_common_args(
    argv: Sequence[str],
    doc: str | None = None,
    *,
    flags: Iterable[str] = (),
    value_flags: Iterable[str] = (),
) -> tuple[str, int]:
    """Parse the CLI arguments every analysis script accepts.

    Recognizes a single positional target path (default ``.``) and
    ``--max-files N``, plus the script's own *flags* / *value_flags* (so
    they are neither warned about nor taken as the target). Any other flag
    is ignored WITH a warning on stderr -- see :func:`parse_scan_args`,
    which scripts that support ``--exclude`` use instead. A non-integer
    ``--max-files`` exits with a JSON error rather than an unhandled
    traceback. ``-h``/``--help`` prints *doc* (default: the running script's
    docstring) and exits 0 without scanning -- see :func:`exit_on_help`.
    """
    parsed = parse_scan_args(
        argv, doc, flags=flags, value_flags=value_flags, exclude=False
    )
    return parsed.target, parsed.max_files


def history_root_arg(
    value: str | None, project_root: Path, scan_root: Path
) -> tuple[Path, Path]:
    """Where to read git history from: ``(history_root, scan_root_in_history)``.

    *value* is a ``--git-dir``/``--repo`` argument: a checkout directory, or
    its ``.git`` directory, that corresponds to *project_root*. An export (a
    ``git archive`` copy) has no ``.git``, so history-weighted outputs went
    silently empty; with this, the tree is scanned from the export while the
    history comes from the checkout, files matched by their path relative to
    the project root. Without *value* both are the scanned tree's own.
    """
    if not value:
        return project_root, scan_root
    root = Path(value).expanduser().resolve()
    if root.name == ".git":
        root = root.parent
    try:
        mapped = root / scan_root.resolve().relative_to(project_root.resolve())
    except ValueError:
        mapped = root
    return root, mapped


def resolve_target(target: str) -> tuple[Path, Path, Path]:
    """Resolve a target path into ``(target, project_root, scan_root)``.

    ``scan_root`` is the target itself when it is a directory, otherwise the
    project root -- so pointing a script at a single file still reports the
    surrounding project correctly.
    """
    resolved = Path(target).resolve()
    project_root = find_project_root(resolved)
    scan_root = resolved if resolved.is_dir() else project_root
    return resolved, project_root, scan_root


def build_envelope(
    project_root: Path,
    scan_root: Path,
    files_total: int,
    files_analyzed: int,
    selection: FileSelection | None = None,
) -> dict:
    """Build the JSON envelope every analysis script shares.

    ``files_capped`` is a boolean the agents use to warn that results are
    partial because ``--max-files`` truncated the scan. With a *selection*
    (from :func:`select_python_files`) the envelope also says what
    ``--exclude`` and migration skipping left out.
    """
    envelope = {
        "project_root": str(project_root),
        "scan_root": str(scan_root),
        "files_total": files_total,
        "files_analyzed": files_analyzed,
        "files_capped": files_analyzed < files_total,
    }
    if selection is not None:
        envelope.update(selection.envelope_fields())
    return envelope


def load_data(name: str) -> dict:
    """Load a JSON file from the plugin's ``data/`` directory.

    Returns an empty dict when the file is missing so a script keeps working
    against an older plugin checkout.
    """
    path = _DATA_DIR / name
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}


def deduplicate_findings(findings: Iterable[dict]) -> list[dict]:
    """Drop findings that repeat the same ``(file, line, type)`` triple.

    Order is preserved so the first (usually highest-confidence) occurrence of a
    duplicate wins.
    """
    seen: set[tuple] = set()
    result: list[dict] = []
    for finding in findings:
        key = (
            finding.get("file"),
            finding.get("line"),
            finding.get("type") or finding.get("kind"),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(finding)
    return result


def emit(payload: dict) -> None:
    """Write *payload* to stdout as JSON -- the output contract for all scripts."""
    json.dump(payload, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
