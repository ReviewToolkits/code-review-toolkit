#!/usr/bin/env python3
"""Annotate review findings with their git history.

For each finding (a file and a line), report:
- `introduced_by`: the newest commit that changed the finding's lines in a
  way that is not layout only (`git log -L`, which follows renames). Commits
  that only re-indented, re-wrapped or re-commented the lines are skipped
  and listed in `skipped_layout_only`.
- `blame`: the cross-check from `git blame -w -C -C` (whitespace-insensitive,
  follows moves and copies).
- `pickaxe` (when the finding gives `pattern`): the oldest commit that added
  that exact string (`git log -S`).
- `first_release`: the first release tag containing `introduced_by`.
- `related_fixes`: fix commits that changed the enclosing function.
- `branches`: for each unmerged remote branch, whether the enclosing
  function is unchanged, changed, or gone there.

An empty result is reported as null with a reason. It never falls back to
HEAD.

Usage:
    python annotate_findings.py [path] --findings FILE [options]

Options:
    --findings FILE       JSON list of findings, or {"findings": [...]}. Each
                          needs `file` (or `path`) and `line` (or
                          `line_start`); optional: `id`, `end_line`,
                          `pattern` (a code string for `git log -S`),
                          `signature` (a code substring to look for on each
                          branch)
    --max-files N         Annotate findings in at most N distinct files
    --base REF            Ref unmerged branches are compared against
                          (default: origin/HEAD, else origin/main,
                          origin/master, main, master)
    --no-branches         Skip the per-branch presence check
    --max-branches N      Branches checked, most recent first (default: 30)
    --release-pattern RE  Tags that count as releases
                          (default: ^v?[0-9]+(\\.[0-9]+)+$)
    --since DATE          Only look for related fixes since DATE
    --key-type SPEC       Tracker-key prefixes or keys that mark a commit's
                          type, as in analyze_history.py (e.g. B=fix)
"""

import ast
import copy
import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from analyze_history import (  # noqa: E402
    _default_branch,
    _strip_docstrings,
    classify_commit_detail,
    def_start_line,
    git_toplevel,
    iter_qualified_defs,
    parse_key_types,
)
from scan_common import (  # noqa: E402
    build_envelope,
    emit,
    exit_on_help,
    find_project_root,
)

_GIT_TIMEOUT = 60
_TIME_BUDGET = 270  # seconds; the agent's Bash call allows 300
_MAX_BRANCHES = 30
_MAX_LINE_HISTORY = 10
_MAX_RELATED = 15
_DEFAULT_RELEASE = r"^v?[0-9]+(\.[0-9]+)+$"


def _git(args: list[str], cwd: Path, timeout: int = _GIT_TIMEOUT) -> str | None:
    """stdout of a git command, or None when it failed or timed out."""
    try:
        out = subprocess.run(
            ["git", *args],
            check=False,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout


def _strip_comment(line: str) -> str:
    """`line` without a trailing `# comment` (a `#` inside a string stays)."""
    quote = ""
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif quote:
            if char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
        elif char == "#":
            return line[:index]
    return line


def _layout_key(lines: list[str]) -> str:
    """The lines with whitespace and comments removed.

    Two sides with the same key differ only in layout: indentation, wrapping,
    spacing or comments.
    """
    return "".join("".join(_strip_comment(line) for line in lines).split())


# --- line history ---------------------------------------------------------


def parse_line_log(text: str) -> list[dict]:
    """Parse `git log -L` output (format `%x00%H%x00%cI%x00%s`, with patches).

    Returns newest first: {"sha", "date", "subject", "removed", "added",
    "path"}; removed/added are the range's lines on each side.
    """
    commits: list[dict] = []
    current: dict | None = None
    for line in text.splitlines():
        if line.startswith("\x00"):
            parts = line.split("\x00")
            current = {
                "sha": parts[1] if len(parts) > 1 else "",
                "date": parts[2] if len(parts) > 2 else "",
                "subject": parts[3] if len(parts) > 3 else "",
                "removed": [],
                "added": [],
                "path": None,
            }
            commits.append(current)
            continue
        if current is None:
            continue
        if line.startswith("+++ "):
            current["path"] = line[6:] if line.startswith("+++ b/") else None
        elif line.startswith(("--- ", "diff --git")):
            continue
        elif line.startswith("+"):
            current["added"].append(line[1:])
        elif line.startswith("-"):
            current["removed"].append(line[1:])
    return commits


def line_history(
    git_root: Path, rel: str, start: int, end: int, key_types: dict[str, str]
) -> list[dict] | None:
    """Commits that changed lines start..end of `rel` (HEAD numbering)."""
    out = _git(
        [
            "-c",
            "core.quotepath=off",
            "log",
            f"-L{start},{end}:{rel}",
            "--no-color",
            "--no-ext-diff",
            "--format=%x00%H%x00%cI%x00%s",
            "HEAD",
        ],
        git_root,
    )
    if out is None:
        return None
    history = parse_line_log(out)
    for commit in history:
        commit["layout_only"] = bool(commit["removed"]) and _layout_key(
            commit["removed"]
        ) == _layout_key(commit["added"])
        commit["type"] = classify_commit_detail(commit["subject"], key_types)["type"]
    return history


def _commit_ref(sha: str, date: str, subject: str, ctype: str | None = None) -> dict:
    ref = {"commit": sha, "commit_short": sha[:7], "date": date, "message": subject}
    if ctype is not None:
        ref["type"] = ctype
    return ref


def blame_line(git_root: Path, rel: str, line: int) -> dict | None:
    """`git blame -w -C -C` of one line at HEAD, or None."""
    out = _git(
        [
            "blame",
            "-w",
            "-C",
            "-C",
            "--porcelain",
            f"-L{line},{line}",
            "HEAD",
            "--",
            rel,
        ],
        git_root,
    )
    if not out:
        return None
    lines = out.splitlines()
    sha = lines[0].split()[0] if lines and lines[0].split() else ""
    if not sha or not sha.strip("0"):
        return None
    summary, date = "", ""
    for entry in lines[1:]:
        if entry.startswith("summary "):
            summary = entry[8:]
        elif entry.startswith("committer-time "):
            try:
                date = time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(entry.split()[1]))
                )
            except (ValueError, IndexError):
                date = ""
    return _commit_ref(sha, date, summary)


def pickaxe(git_root: Path, rel: str, pattern: str) -> dict | None:
    """Oldest commit whose diff changes the count of `pattern` in the file."""
    out = _git(
        [
            "log",
            "--follow",
            "--reverse",
            "-S",
            pattern,
            "--format=%H%x00%cI%x00%s",
            "HEAD",
            "--",
            rel,
        ],
        git_root,
    )
    if not out or not out.strip():
        return None
    sha, date, subject = (out.splitlines()[0].split("\x00") + ["", ""])[:3]
    return _commit_ref(sha, date, subject)


def first_release(
    git_root: Path, sha: str, pattern: re.Pattern[str], cache: dict
) -> str | None:
    """The oldest tag matching `pattern` that contains `sha`."""
    if sha in cache:
        return cache[sha]
    out = _git(["tag", "--contains", sha, "--sort=creatordate"], git_root)
    tag = None
    if out:
        tag = next((t for t in out.splitlines() if pattern.search(t.strip())), None)
    cache[sha] = tag
    return tag


# --- functions and branches ----------------------------------------------


def _function_nodes(source: str) -> dict[str, ast.AST] | None:
    try:
        tree = ast.parse(source.removeprefix("﻿"))
    except (SyntaxError, ValueError):
        return None
    return {qual: node for qual, node, _ in iter_qualified_defs(tree)}


def enclosing_function(
    nodes: dict[str, ast.AST], line: int
) -> tuple[str, int, int] | None:
    """Innermost (qualname, start, end) whose span contains `line`."""
    best: tuple[str, int, int] | None = None
    for qual, node in nodes.items():
        start = def_start_line(node)
        end = getattr(node, "end_lineno", None) or node.lineno
        if start <= line <= end and (best is None or start >= best[1]):
            best = (qual, start, end)
    return best


def function_fingerprint(node: ast.AST) -> str:
    """The def's AST without docstrings or layout."""
    clone = ast.Module(body=[copy.deepcopy(node)], type_ignores=[])
    _strip_docstrings(clone)
    return ast.dump(clone, include_attributes=False)


def unmerged_branches(git_root: Path, base: str, limit: int) -> list[str]:
    """Remote branches with commits `base` lacks, most recent first."""
    out = _git(
        [
            "for-each-ref",
            "--sort=-committerdate",
            "--format=%(refname:short)%00%(symref)",
            "refs/remotes",
        ],
        git_root,
    )
    if out is None:
        return []
    names: list[str] = []
    for line in out.splitlines():
        name, symref = (line.split("\x00") + [""])[:2]
        if symref or not name or name == base:
            continue
        ahead = _git(["rev-list", "--count", f"{base}..{name}"], git_root)
        if ahead is None or not ahead.strip().isdigit() or int(ahead) == 0:
            continue
        names.append(name)
        if len(names) >= limit:
            break
    return names


class _Blobs:
    """`ref:path` contents through one `git cat-file --batch` process."""

    def __init__(self, git_root: Path) -> None:
        self._root = git_root
        self._proc: subprocess.Popen[bytes] | None = None
        self._cache: dict[str, str | None] = {}

    def get(self, spec: str) -> str | None:
        if spec in self._cache:
            return self._cache[spec]
        text = self._read(spec)
        self._cache[spec] = text
        return text

    def _read(self, spec: str) -> str | None:
        if self._proc is None:
            try:
                self._proc = subprocess.Popen(
                    ["git", "cat-file", "--batch"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    cwd=str(self._root),
                )
            except OSError:
                return None
        proc = self._proc
        assert proc.stdin is not None and proc.stdout is not None
        try:
            proc.stdin.write(spec.encode("utf-8") + b"\n")
            proc.stdin.flush()
            header = proc.stdout.readline().split()
            if len(header) != 3:
                return None
            data = proc.stdout.read(int(header[2]))
            proc.stdout.read(1)
        except (OSError, ValueError):
            return None
        return data.decode("utf-8", errors="replace")

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


def branch_presence(
    blobs: _Blobs,
    branch: str,
    rel: str,
    function: str | None,
    head_fingerprint: str | None,
    anchor_text: str,
    signature: str | None,
) -> dict:
    """How the finding's code looks on `branch`.

    `status` is `file-absent`, `function-absent`, `unchanged` (the function
    is AST-identical to HEAD's: the finding still applies there), or
    `changed` (the branch modifies the function: read the diff; it may fix
    the finding). Without an enclosing function the anchor line is looked
    up instead: `line-present` / `line-absent`. Comparing the whole function
    matters: a branch that fixes the code by adding lines around an
    unchanged line still contains that line.
    """
    entry: dict = {"branch": branch}
    source = blobs.get(f"{branch}:{rel}")
    if source is None:
        entry["status"] = "file-absent"
        return entry
    scope_text = source
    if function is not None:
        nodes = _function_nodes(source)
        node = nodes.get(function) if nodes else None
        if nodes is None:
            entry["status"] = "unparseable"
            return entry
        if node is None:
            entry["status"] = "function-absent"
            return entry
        entry["status"] = (
            "unchanged" if function_fingerprint(node) == head_fingerprint else "changed"
        )
        scope_text = ast.get_source_segment(source, node) or ""
    else:
        target = "".join(anchor_text.split())
        present = any("".join(line.split()) == target for line in source.splitlines())
        entry["status"] = "line-present" if present else "line-absent"
    if signature:
        entry["signature_present"] = "".join(signature.split()) in "".join(
            scope_text.split()
        )
    return entry


# --- driver ----------------------------------------------------------------


def load_findings(path: Path) -> list[dict]:
    """Findings from a JSON list or an object with a `findings` list."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("findings", [])
    if not isinstance(data, list):
        raise TypeError("expected a list of findings or {'findings': [...]}")
    return [f for f in data if isinstance(f, dict)]


def _resolve_file(raw: str, project_root: Path, git_root: Path) -> str | None:
    """The finding's file as a path relative to the repository root."""
    candidates = [Path(raw)] if Path(raw).is_absolute() else []
    candidates += [project_root / raw, git_root / raw]
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
            rel = resolved.relative_to(git_root)
        except (OSError, ValueError):
            continue
        if resolved.is_file():
            return rel.as_posix()
    return None


def annotate_one(
    finding: dict,
    rel: str,
    git_root: Path,
    blobs: _Blobs,
    branches: list[str] | None,
    options: dict,
    release_cache: dict,
) -> dict:
    line = int(finding.get("line") or finding.get("line_start") or 0)
    end = int(finding.get("end_line") or finding.get("line_end") or line)
    out: dict = {"id": finding.get("id"), "file": rel, "line": line, "end_line": end}
    head = blobs.get(f"HEAD:{rel}")
    if head is None:
        out["error"] = "file is not tracked at HEAD"
        return out
    head_lines = head.splitlines()
    if not 1 <= line <= len(head_lines) or end < line:
        out["error"] = (
            f"line {line} is outside the file at HEAD ({len(head_lines)} lines)"
        )
        return out
    end = min(end, len(head_lines))
    nodes = _function_nodes(head) or {}
    span = enclosing_function(nodes, line)
    out["function"] = span[0] if span else None
    out["function_span"] = [span[1], span[2]] if span else None

    # Introducing commit: newest change to the lines that is not layout only.
    history = line_history(git_root, rel, line, end, options["key_types"])
    if history is None or not history:
        out["introduced_by"] = None
        out["introduced_reason"] = "git log -L found no history for these lines"
        out["line_history"] = []
    else:
        skipped = []
        introduced = None
        for commit in history:
            if commit["layout_only"]:
                skipped.append(commit["sha"][:7])
                continue
            introduced = commit
            break
        out["line_history"] = [
            {
                **_commit_ref(c["sha"], c["date"], c["subject"], c["type"]),
                "layout_only": c["layout_only"],
            }
            for c in history[:_MAX_LINE_HISTORY]
        ]
        out["line_history_total"] = len(history)
        out["skipped_layout_only"] = skipped
        if introduced is None:
            out["introduced_by"] = None
            out["introduced_reason"] = "every change to these lines was layout only"
        else:
            out["introduced_by"] = _commit_ref(
                introduced["sha"],
                introduced["date"],
                introduced["subject"],
                introduced["type"],
            )
            out["introduced_by"]["method"] = "git log -L, layout-only changes skipped"
    out["blame"] = blame_line(git_root, rel, line)

    pattern = finding.get("pattern")
    if pattern:
        hit = pickaxe(git_root, rel, str(pattern))
        out["pickaxe"] = hit
        if hit is None:
            out["pickaxe_reason"] = (
                "git log -S found no commit adding this exact string (it is "
                "whitespace-sensitive and single-line); not falling back to HEAD"
            )

    anchor = out["introduced_by"] or out.get("pickaxe")
    if anchor:
        tag = first_release(
            git_root, anchor["commit"], options["release_re"], release_cache
        )
        out["first_release"] = tag
        if tag is None:
            out["first_release_reason"] = "no release tag contains it (unreleased)"
    else:
        out["first_release"] = None
        out["first_release_reason"] = "no introducing commit"

    # Fix commits that changed the enclosing function.
    if span:
        args = [
            "-c",
            "core.quotepath=off",
            "log",
            f"-L{span[1]},{span[2]}:{rel}",
            "-s",
            "--format=%H%x00%cI%x00%s",
        ]
        if options["since"]:
            args.append(f"--since={options['since']}")
        listing = _git([*args, "HEAD"], git_root)
        fixes = []
        for entry in (listing or "").splitlines():
            sha, date, subject = (entry.split("\x00") + ["", ""])[:3]
            if not sha:
                continue
            detail = classify_commit_detail(subject, options["key_types"])
            if detail["type"] == "fix":
                ref = _commit_ref(sha, date, subject)
                ref["keys"] = detail["keys"]
                fixes.append(ref)
        out["related_fixes"] = fixes[:_MAX_RELATED]
        out["related_fixes_total"] = len(fixes)
        if listing is None:
            out["related_fixes_reason"] = "git log -L failed for the function"
    else:
        out["related_fixes"] = []
        out["related_fixes_reason"] = "the line is not inside a function"

    if branches is not None:
        head_fp = function_fingerprint(nodes[span[0]]) if span else None
        out["branches"] = [
            branch_presence(
                blobs,
                branch,
                rel,
                span[0] if span else None,
                head_fp,
                head_lines[line - 1],
                finding.get("signature"),
            )
            for branch in branches
        ]
    return out


def analyze(
    target: str,
    *,
    max_files: int = 0,
    findings_path: str | None = None,
    findings: list[dict] | None = None,
    base: str | None = None,
    no_branches: bool = False,
    max_branches: int = _MAX_BRANCHES,
    release_pattern: str = _DEFAULT_RELEASE,
    since: str | None = None,
    key_types: dict[str, str] | None = None,
) -> dict:
    """Annotate every finding; see the module docstring."""
    started = time.monotonic()
    scan_root = Path(target).resolve()
    project_root = find_project_root(scan_root)
    git_root = git_toplevel(project_root)
    if findings is None:
        if not findings_path:
            return {"error": "--findings FILE is required"}
        try:
            findings = load_findings(Path(findings_path))
        except (OSError, ValueError, TypeError) as exc:
            return {"error": f"cannot read findings: {exc}"}
    if _git(["rev-parse", "--verify", "--quiet", "HEAD^{commit}"], git_root) is None:
        return {
            "error": "not a git repository with commits",
            "project_root": str(project_root),
        }
    try:
        release_re = re.compile(release_pattern)
    except re.error as exc:
        return {"error": f"bad --release-pattern: {exc}"}

    resolved: list[tuple[dict, str | None]] = []
    for finding in findings:
        raw = finding.get("file") or finding.get("path") or ""
        resolved.append((finding, _resolve_file(str(raw), project_root, git_root)))
    distinct = list(dict.fromkeys(rel for _, rel in resolved if rel))
    allowed = set(distinct[:max_files] if max_files > 0 else distinct)

    branch_info: dict
    branches: list[str] | None = None
    if no_branches:
        branch_info = {"available": False, "reason": "skipped (--no-branches)"}
    else:
        base_ref = base or _default_branch(git_root)
        if base_ref is None:
            branch_info = {"available": False, "reason": "no default branch found"}
        else:
            branches = unmerged_branches(git_root, base_ref, max_branches)
            branch_info = {
                "available": True,
                "base": base_ref,
                "branches": branches,
                "note": (
                    "As of the last fetch (this script never fetches). Per "
                    "finding: `unchanged` means the branch has the same function, "
                    "so the finding applies there too; `changed` means the "
                    "branch modifies it: read that diff before calling the "
                    "finding unfixed or fixed."
                ),
            }

    options = {"key_types": key_types or {}, "release_re": release_re, "since": since}
    annotations: list[dict] = []
    skipped: list[dict] = []
    release_cache: dict = {}
    blobs = _Blobs(git_root)
    try:
        for finding, rel in resolved:
            ident = finding.get("id")
            if rel is None:
                skipped.append(
                    {
                        "id": ident,
                        "file": finding.get("file") or finding.get("path"),
                        "reason": "file not found in the repository",
                    }
                )
                continue
            if rel not in allowed:
                skipped.append({"id": ident, "file": rel, "reason": "--max-files cap"})
                continue
            if time.monotonic() - started > _TIME_BUDGET:
                skipped.append(
                    {"id": ident, "file": rel, "reason": "time budget exhausted"}
                )
                continue
            annotations.append(
                annotate_one(
                    finding, rel, git_root, blobs, branches, options, release_cache
                )
            )
    finally:
        blobs.close()

    result = build_envelope(project_root, scan_root, len(distinct), len(allowed))
    result.update(
        {
            "findings_total": len(findings),
            "findings_annotated": len(annotations),
            "skipped": skipped,
            "release_pattern": release_pattern,
            "unmerged_branches": branch_info,
            "annotations": annotations,
        }
    )
    return result


def main() -> None:
    argv = sys.argv[1:]
    exit_on_help(argv, __doc__)
    opts: dict = {"key_types": {}}
    positional: list[str] = []
    value_flags = {
        "--findings": "findings_path",
        "--base": "base",
        "--release-pattern": "release_pattern",
        "--since": "since",
    }
    int_flags = {"--max-files": "max_files", "--max-branches": "max_branches"}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in value_flags and i + 1 < len(argv):
            opts[value_flags[arg]] = argv[i + 1]
            i += 2
        elif arg in int_flags and i + 1 < len(argv):
            try:
                opts[int_flags[arg]] = int(argv[i + 1])
            except ValueError:
                emit({"error": f"{arg} requires an integer, got '{argv[i + 1]}'"})
                sys.exit(2)
            i += 2
        elif arg == "--key-type" and i + 1 < len(argv):
            try:
                opts["key_types"].update(parse_key_types(argv[i + 1]))
            except ValueError as exc:
                emit({"error": str(exc)})
                sys.exit(2)
            i += 2
        elif arg == "--no-branches":
            opts["no_branches"] = True
            i += 1
        elif arg.startswith("--"):
            emit({"error": f"unknown flag {arg!r}"})
            sys.exit(2)
        else:
            positional.append(arg)
            i += 1
    result = analyze(positional[0] if positional else ".", **opts)
    emit(result)
    if "error" in result:
        sys.exit(1)


if __name__ == "__main__":
    main()
