#!/usr/bin/env python3
"""Check that every finding's locations exist at the reviewed commit.

Consolidation re-reads a sample of locations by hand, and entries whose line
ranges ran past the end of the file got through. This is the mechanical
check, for every finding whatever its classification: each ``path``,
``path:N``, ``path:N-M`` or ``path:N,M`` must name a file (or, without
lines, a directory) that exists at the commit, and every line must be
within the file.

Locations are read from ``locations`` (a list or a string), ``location``
and ``sites`` (the findings-memory schema), and ``file``/``path`` with
``line``/``line_start`` and ``end_line`` (the annotate_findings input).
Paths are relative to the repository root or to the project root.

Problems: ``file-missing`` (with ``did you mean`` when exactly one file
ends with the given path), ``line-past-eof`` (the file has fewer lines),
``bad-range`` (line 0, or an end before the start), ``lines-on-directory``,
``unparseable`` (prose where a location belongs), ``no-locations``.
Exit 1 when there is any problem.

Usage:
    python check_locations.py [path] --findings FILE [--commit REF] [--max-files N]

Options:
    --findings FILE   findings.json (a list, or {"findings": [...]})
    --commit REF      The reviewed commit (default: HEAD). Outside a git
                      repository the working tree is read instead.
    --max-files N     Check locations in at most N distinct files
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from annotate_findings import _Blobs
from leak_scan import findings_list
from scan_common import build_envelope, emit, exit_on_help, find_project_root

_LOC = re.compile(
    r"^(?P<path>[^\s:]+?)(?::(?P<lines>L?\d+(?:-L?\d+)?(?:\s*,\s*L?\d+(?:-L?\d+)?)*))?$"
)


def parse_location(loc: str) -> tuple[str, list[tuple[int, int]]] | None:
    """``("pkg/a.py", [(3, 5)])`` for ``pkg/a.py:3-5``; None for prose."""
    m = _LOC.match(loc.strip())
    if not m:
        return None
    ranges = []
    for part in (m.group("lines") or "").split(","):
        part = part.strip().replace("L", "")
        if not part:
            continue
        a, _, b = part.partition("-")
        ranges.append((int(a), int(b or a)))
    return m.group("path"), ranges


def locations_of(f: dict) -> list[str]:
    """Every location string a finding carries, in the formats listed above."""
    out: list[str] = []
    locs = f.get("locations")
    out += (
        [locs]
        if isinstance(locs, str)
        else [str(x) for x in locs or [] if not isinstance(x, dict)]
    )
    for x in locs if isinstance(locs, list) else []:
        if isinstance(x, dict):
            out += locations_of(x)
    if f.get("location"):
        out.append(str(f["location"]))
    out += [str(s) for s in f.get("sites") or []]
    path = f.get("file") or f.get("path")
    if path:
        line = f.get("line") or f.get("line_start")
        end = f.get("end_line") or f.get("line_end")
        out.append(
            f"{path}:{line}-{end}"
            if line and end
            else f"{path}:{line}"
            if line
            else str(path)
        )
    return out


def _git(args: list[str], cwd: Path) -> str | None:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 else None


class _Tree:
    """File listing and contents, at a commit or in the working tree."""

    def __init__(self, project_root: Path, commit: str | None) -> None:
        top = _git(["rev-parse", "--show-toplevel"], project_root)
        self.git_root = Path(top.strip()) if top else None
        self.commit: str | None = None
        self._blobs: _Blobs | None = None
        if self.git_root is not None:
            sha = _git(
                ["rev-parse", "--verify", f"{commit or 'HEAD'}^{{commit}}"],
                self.git_root,
            )
            if sha is None:
                raise ValueError(f"cannot resolve commit {commit or 'HEAD'}")
            self.commit = sha.strip()
            listing = (
                _git(["ls-tree", "-r", "-z", "--name-only", self.commit], self.git_root)
                or ""
            )
            self.files = {p for p in listing.split("\0") if p}
            self._blobs = _Blobs(self.git_root)
            self.root = self.git_root
        else:
            if commit:
                raise ValueError(
                    "--commit given, but the project is not in a git repository"
                )
            self.root = project_root
            self.files = {
                p.relative_to(project_root).as_posix()
                for p in project_root.rglob("*")
                if p.is_file()
            }
        self.dirs = {str(Path(p).parent.as_posix()) for p in self.files}
        for d in list(self.dirs):
            parts = d.split("/")
            self.dirs.update("/".join(parts[:i]) for i in range(1, len(parts)))
        try:
            self.prefix = (
                project_root.resolve().relative_to(self.root.resolve()).as_posix()
            )
        except ValueError:
            self.prefix = "."
        self._lines: dict[str, int | None] = {}

    def resolve(self, raw: str) -> tuple[str, str] | None:
        """``(kind, repo-relative path)``: kind ``file`` or ``dir``; None if absent."""
        raw = raw.strip()
        while raw.startswith("./"):
            raw = raw[2:]
        candidates = [raw.rstrip("/")]
        if self.prefix not in ("", "."):
            candidates.append(f"{self.prefix}/{raw.rstrip('/')}")
        for c in candidates:
            if c in self.files:
                return "file", c
        for c in candidates:
            if c in self.dirs:
                return "dir", c
        return None

    def suggest(self, raw: str) -> str | None:
        """The one file whose path ends with *raw*, when exactly one does."""
        tail = "/" + raw.strip().lstrip("./").rstrip("/")
        hits = [p for p in self.files if p.endswith(tail)]
        return hits[0] if len(hits) == 1 else None

    def line_count(self, rel: str) -> int | None:
        if rel not in self._lines:
            if self._blobs is not None:
                text = self._blobs.get(f"{self.commit}:{rel}")
            else:
                try:
                    text = (self.root / rel).read_text(
                        encoding="utf-8", errors="replace"
                    )
                except OSError:
                    text = None
            self._lines[rel] = None if text is None else len(text.splitlines())
        return self._lines[rel]

    def close(self) -> None:
        if self._blobs is not None:
            self._blobs.close()


def _problem(
    problems: list[dict], fid, f: dict, loc: str, kind: str, detail: str = ""
) -> None:
    problems.append(
        {
            "id": fid,
            "classification": f.get("classification"),
            "location": loc,
            "problem": kind,
            "detail": detail,
        }
    )


def check(findings: list[dict], tree: _Tree, max_files: int = 0) -> dict:
    """Every location of every finding, checked against *tree*."""
    problems = []
    checked_files: set[str] = set()
    seen_files: set[str] = set()
    skipped = 0
    total = ok = 0
    for i, f in enumerate(findings):
        fid = f.get("id", f"#{i}")
        locs = locations_of(f)
        if not locs:
            problems.append(
                {
                    "id": fid,
                    "classification": f.get("classification"),
                    "location": None,
                    "problem": "no-locations",
                }
            )
            continue
        for loc in locs:
            total += 1

            parsed = parse_location(loc)
            if parsed is None:
                _problem(problems, fid, f, loc, "unparseable")
                continue
            path, ranges = parsed
            found = tree.resolve(path)
            if found is None:
                guess = tree.suggest(path)
                _problem(
                    problems,
                    fid,
                    f,
                    loc,
                    "file-missing",
                    f"did you mean {guess}?" if guess else "",
                )
                continue
            kind, rel = found
            if kind == "file":
                seen_files.add(rel)
            if kind == "dir":
                if ranges:
                    _problem(problems, fid, f, loc, "lines-on-directory")
                else:
                    ok += 1
                continue
            if (
                max_files
                and rel not in checked_files
                and len(checked_files) >= max_files
            ):
                skipped += 1
                continue
            checked_files.add(rel)
            n = tree.line_count(rel)
            if n is None:
                _problem(problems, fid, f, loc, "file-missing", "unreadable")
                continue
            errs = []
            for a, b in ranges:
                if a < 1 or b < a:
                    errs.append(("bad-range", f"{a}-{b}"))
                elif b > n:
                    errs.append(("line-past-eof", f"line {b}, file has {n} lines"))
            if errs:
                _problem(problems, fid, f, loc, *errs[0])
            else:
                ok += 1
    return {
        "findings_total": len(findings),
        "locations_total": total,
        "locations_ok": ok,
        "locations_skipped": skipped,
        "files_referenced": len(seen_files),
        "files_checked": len(checked_files),
        "problem_counts": dict(Counter(p["problem"] for p in problems)),
        "problems_by_classification": dict(
            Counter(str(p["classification"]) for p in problems)
        ),
        "problems": problems,
        "clean": not problems,
    }


def _parse(argv: list[str]) -> tuple[list[str], dict[str, str]]:
    positional: list[str] = []
    opts: dict[str, str] = {}
    i = 0
    while i < len(argv):
        if argv[i].startswith("--"):
            if i + 1 >= len(argv):
                raise ValueError(f"{argv[i]} needs a value")
            opts[argv[i]] = argv[i + 1]
            i += 2
        else:
            positional.append(argv[i])
            i += 1
    return positional, opts


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    exit_on_help(argv, __doc__)
    tree = None
    try:
        positional, opts = _parse(argv)
        if "--findings" not in opts:
            raise ValueError("--findings FILE is required")
        max_files = int(opts.get("--max-files", 0))
        target = Path(positional[0] if positional else ".").resolve()
        project_root = find_project_root(target)
        findings = findings_list(
            json.loads(Path(opts["--findings"]).read_text(encoding="utf-8"))
        )
        tree = _Tree(project_root, opts.get("--commit"))
        result = check(findings, tree, max_files)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        emit({"error": str(exc)})
        sys.exit(2)
    finally:
        if tree is not None:
            tree.close()
    envelope = build_envelope(
        project_root, tree.root, result["files_referenced"], result["files_checked"]
    )
    emit({**envelope, "commit": tree.commit, **result})
    sys.exit(0 if result["clean"] else 1)


if __name__ == "__main__":
    main()
