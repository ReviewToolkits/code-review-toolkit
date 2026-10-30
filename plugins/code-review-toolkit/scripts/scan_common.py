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
import json
import sys
from collections.abc import Generator, Iterable, Sequence
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


def parse_common_args(argv: Sequence[str], doc: str | None = None) -> tuple[str, int]:
    """Parse the CLI arguments every analysis script accepts.

    Recognizes a single positional target path (default ``.``) and
    ``--max-files N``. Unknown ``--flags`` are ignored so individual scripts can
    add their own without re-implementing this loop. A non-integer
    ``--max-files`` exits with a JSON error rather than an unhandled traceback.
    ``-h``/``--help`` prints *doc* (default: the running script's docstring)
    and exits 0 without scanning -- see :func:`exit_on_help`.
    """
    exit_on_help(argv, doc)
    max_files = 0
    positional: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--max-files" and i + 1 < len(argv):
            try:
                max_files = int(argv[i + 1])
            except ValueError:
                emit({"error": f"--max-files requires an integer, got '{argv[i + 1]}'"})
                sys.exit(2)
            i += 2
        elif arg.startswith("--"):
            i += 1
        else:
            positional.append(arg)
            i += 1
    return (positional[0] if positional else "."), max_files


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
) -> dict:
    """Build the JSON envelope every analysis script shares.

    ``files_capped`` is a boolean the agents use to warn that results are
    partial because ``--max-files`` truncated the scan.
    """
    return {
        "project_root": str(project_root),
        "scan_root": str(scan_root),
        "files_total": files_total,
        "files_analyzed": files_analyzed,
        "files_capped": files_analyzed < files_total,
    }


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
