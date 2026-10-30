#!/usr/bin/env python3
"""Run mypy against a project and report the type errors that indicate defects.

**One checker, its own config, one extra flag.** That is the whole design, and it
is a measured conclusion rather than a preference (docs/decision-log.md D-03).
Across three corpora, mypy on the project's own config found 0 / 0 / 61 errors
while pyright, ty and pyrefly found 60 / 50 / 1463 and 90 / 51 / 1266 and
18 / 25 / 53. **Every consensus cluster among the other three was a shared blind
spot** -- all ten in `_pyrepl` were `if False:` TYPE_CHECKING blocks that only
mypy special-cases; seven of ten in coverage.py were `hasattr()`-guarded
assignments that only mypy narrows. Consensus-among-the-others means *mypy is
right and the others are misconfigured*, so cross-referencing checkers is
anti-correlated with truth and this script does not do it.

The load-bearing addition is a single flag. On coverage.py, plain mypy reports 0
errors and ``--disallow-any-unimported`` reports **exactly 3, all in one file**,
every one caused by `from coverage.plugins import FileReporter` -- a module that
does not exist. The same flag on `_pyrepl` adds **0**. It is a real finding, not
a flag that fires everywhere. It runs as a labelled SECOND pass so the baseline
count stays honest.

Two hard rules learned the same way:

* **``files_checked == 0`` is reported as FAILED, not clean.** A checker whose
  config discovery excluded the tree reports success having analyzed nothing.
  pyrefly does exactly this on idlelib -- `0 errors`, exit 0.
* **Stale-ignore counts come only from mypy's own ``unused-ignore`` code.**
  Never from grep. Grepping `# type: ignore` and calling the total "stale
  ignores" is precisely the false alarm that produced a "36 stale ignores"
  claim with no evidence behind it.

``ty`` and ``pyrefly`` are deliberately NOT integrated. ``ty`` is experimental;
``pyrefly``'s silent-zero mode makes a clean result indistinguishable from a
misconfigured one, which is the one failure this script exists to prevent.

Two more labelled passes follow the same rule. ``--warn-unreachable`` finds the
blocks a wrong annotation has silently removed from checking -- a module-level
`x = None` with no annotation narrows every `if x is not None:` body away, and
the phantom-import pass cannot see that. ``--warn-unused-ignores``
measures stale ignores when the project's config does not; without it a 0 would
mean "not measured", so an unmeasured count is reported as null with a reason.
The passes are independent and run concurrently.

Usage:
    python check_typing.py [path] [--mypy-bin PATH] [--max-files N]
        [--no-strict-pass] [--no-unreachable-pass] [--no-unused-ignores-pass]
"""

from __future__ import annotations

import argparse
import configparser
import os
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scan_common import (  # noqa: E402
    build_envelope,
    discover_python_files,
    emit,
    find_project_root,
)

_MYPY_TIMEOUT = 600

# mypy error codes that mean a probable DEFECT rather than a missing annotation.
# An unannotated codebase produces thousands of the latter and none of the
# former, which is why they are separated rather than counted together.
_DEFECT_CODES = frozenset(
    {
        "attr-defined",
        "union-attr",
        "call-arg",
        "arg-type",
        "return-value",
        "assignment",
        "index",
        "operator",
        "call-overload",
        "misc",
        "override",
        "unreachable",
        "redundant-expr",
        "comparison-overlap",
        "truthy-bool",
        "truthy-function",
        "unused-coroutine",
        "await-not-async",
        "func-returns-value",
        "safe-super",
        "used-before-def",
        "possibly-undefined",
        "no-redef",
        "valid-type",
        "type-var",
        "has-type",
        # A name that does not resolve (often a quoted forward reference that
        # silently stops checking), a path that falls off the end of a function
        # declared to return a value, and a method replaced on an instance:
        # each was a real root cause on review, not noise.
        "name-defined",
        "return",
        "method-assign",
    }
)

# Codes that describe MISSING typing rather than wrong typing.
_ANNOTATION_CODES = frozenset(
    {"no-untyped-def", "no-untyped-call", "no-any-return", "var-annotated", "type-arg"}
)

_LINE = re.compile(
    r"^(?P<file>.+?):(?P<line>\d+)(?::(?P<col>\d+))?: "
    r"(?P<severity>error|warning|note): (?P<message>.*?)"
    r"(?:\s+\[(?P<code>[a-z-]+)\])?$"
)

_SUMMARY = re.compile(
    r"Found (?P<errors>\d+) errors? in (?P<files>\d+) files? "
    r"\(checked (?P<checked>\d+) source files?\)"
)
_CLEAN = re.compile(r"Success: no issues found in (?P<checked>\d+) source files?")
# Belt and braces with --no-color-output: any ANSI a future mypy or wrapper emits
# would otherwise sit between `file:line:` and `error:` and defeat _LINE.
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\x1b\([A-Za-z]")


def mypy_version(mypy_bin: str) -> str | None:
    try:
        out = subprocess.run(
            [mypy_bin, "--version"], capture_output=True, text=True, timeout=60
        )
    except (OSError, subprocess.SubprocessError):
        return None
    parts = out.stdout.split()
    return parts[1] if len(parts) > 1 else None


def find_mypy_config(project_root: Path) -> str | None:
    """The project's own mypy configuration file, if it has one.

    The project's config is used as-is and NEVER overridden: a review that
    reports errors the project has deliberately configured away is reporting
    the reviewer's preferences, not the project's defects.
    """
    for name in ("mypy.ini", ".mypy.ini", "setup.cfg", "pyproject.toml"):
        path = project_root / name
        if not path.is_file():
            continue
        if name in ("mypy.ini", ".mypy.ini"):
            return str(path)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        marker = "[tool.mypy]" if name == "pyproject.toml" else "[mypy]"
        if marker in text:
            return str(path)
    return None


# What `strict = true` turns on, among the options this script asks about.
# `warn_unreachable` is deliberately absent: --strict does not enable it.
_STRICT_IMPLIES = frozenset({"warn_unused_ignores", "check_untyped_defs"})


def config_enables(config: str | None, option: str) -> bool:
    """Whether the project's global mypy section turns *option* on.

    ``strict`` implies it when it is one of ``_STRICT_IMPLIES``. Per-module
    sections are deliberately ignored: they enable the option for part of the
    tree only, so a count taken under them is not a whole-tree measurement.
    Anything unreadable answers False, which costs at most one extra labelled
    pass and never reports an unmeasured value as measured.
    """
    if not config:
        return False
    path = Path(config)
    if path.suffix == ".toml":
        try:
            import tomllib
        except ImportError:  # Python 3.10
            return False
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        mypy = data.get("tool", {}).get("mypy", {})
        return any(mypy.get(key) is True for key in _keys_enabling(option))
    parser = configparser.ConfigParser()
    try:
        parser.read(path, encoding="utf-8")
    except (OSError, configparser.Error):
        return False
    if not parser.has_section("mypy"):
        return False
    for key in _keys_enabling(option):
        try:
            if parser.getboolean("mypy", key, fallback=False):
                return True
        except ValueError:
            continue
    return False


def _keys_enabling(option: str) -> tuple[str, ...]:
    return (option, "strict") if option in _STRICT_IMPLIES else (option,)


# Notes that introduce the error AFTER them rather than explaining the one before.
_CONTEXT_NOTE = re.compile(
    r"^(In module imported here|In function |In member |In class |At top level)"
)


def parse_output(
    stdout: str, standalone_notes: list[dict] | None = None
) -> tuple[list[dict], dict]:
    """Parse mypy's text output into findings plus its own summary line.

    A ``note:`` line is part of the error it follows, not a finding of its own:
    the Superclass/Subclass signatures under an ``override`` error and the
    candidate list under ``call-overload`` are what make those errors
    triageable. Each finding carries its notes in ``notes``. A note with an
    error code of its own (``annotation-unchecked``) stands alone, as does one
    with no error before it; those are appended to *standalone_notes* when a
    list is given. Context notes ("In module imported here:") precede the error
    they introduce and are attached to that one.
    """
    findings: list[dict] = []
    # The finding a following code-less note belongs to; None after anything
    # that breaks the chain (a summary line, an unparseable line, a coded note).
    current: dict | None = None
    pending_context: list[dict] = []
    stats: dict[str, int | None] = {
        "errors": None,
        "files_with_errors": None,
        "files_checked": None,
    }
    for raw in stdout.splitlines():
        line = _ANSI.sub("", raw).rstrip()
        clean = _CLEAN.search(line)
        if clean:
            stats.update(
                errors=0, files_with_errors=0, files_checked=int(clean["checked"])
            )
            current = None
            continue
        summary = _SUMMARY.search(line)
        if summary:
            stats.update(
                errors=int(summary["errors"]),
                files_with_errors=int(summary["files"]),
                files_checked=int(summary["checked"]),
            )
            current = None
            continue
        m = _LINE.match(line)
        if not m:
            current = None
            continue
        code = m["code"] or ""
        if m["severity"] == "note":
            note = {"file": m["file"], "line": int(m["line"]), "message": m["message"]}
            if code:
                note["code"] = code
            if _CONTEXT_NOTE.match(m["message"].strip()) and not code:
                pending_context.append(note)
            elif current is not None and not code:
                current["notes"].append(note)
            else:
                if standalone_notes is not None:
                    standalone_notes.append(note)
                current = None
            continue
        current = {
            "file": m["file"],
            "line": int(m["line"]),
            "column": int(m["col"]) if m["col"] else 0,
            "severity": m["severity"],
            "code": code,
            "message": m["message"],
            "kind": (
                "defect"
                if code in _DEFECT_CODES
                else "missing-annotation"
                if code in _ANNOTATION_CODES
                else "other"
            ),
            "notes": pending_context,
        }
        findings.append(current)
        pending_context = []
    if standalone_notes is not None:
        standalone_notes.extend(pending_context)
    return findings, stats


def _run_mypy(
    mypy_bin: str,
    targets: list[str],
    config: str | None,
    extra: list[str],
    cache_dir: str,
) -> tuple[str, str, int | None]:
    # --no-color-output is load-bearing, not cosmetic. mypy honours FORCE_COLOR
    # even when stdout is a pipe, and the ANSI codes it injects sit between the
    # `file:line:` prefix and `error:`, so _LINE stops matching and every finding
    # is silently dropped -- while the summary line still parses, because its
    # regex only needs the digits. The result is a confident "0 type errors" on
    # any machine with FORCE_COLOR set. Found on coverage.py, where mypy reported
    # 3 errors and this script reported none.
    #
    # --cache-dir is load-bearing too. Without it mypy writes `.mypy_cache/` into
    # whatever directory the caller happens to run from -- the reviewed checkout,
    # if the agent's shell is there, which a read-only review must never touch,
    # and a directory concurrent `parallel` agents share. The caller passes a
    # private temporary directory and removes it afterwards.
    cmd = [
        mypy_bin,
        "--show-error-codes",
        "--no-color-output",
        "--cache-dir",
        cache_dir,
    ]
    if config:
        cmd.extend(["--config-file", config])
    cmd.extend(extra)
    cmd.extend(targets)
    env = {**os.environ, "FORCE_COLOR": "0", "MYPY_FORCE_COLOR": "0", "NO_COLOR": "1"}
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_MYPY_TIMEOUT, env=env
        )
    except subprocess.TimeoutExpired:
        return "", f"mypy timed out after {_MYPY_TIMEOUT}s", None
    except OSError as exc:
        return "", f"could not run mypy: {exc}", None
    return out.stdout, out.stderr, out.returncode


def _diagnose_failure(stderr: str, stdout: str, returncode: int | None) -> str:
    """Turn a raw mypy failure into something a reviewer can act on.

    A FAILED status is only useful if it says what to do next; otherwise the
    reviewer either ignores it or, worse, reports the project as unanalyzable.
    """
    blob = f"{stderr}\n{stdout}"
    if returncode is None:
        return "mypy did not run to completion (timeout or exec failure)"
    if "shadows library module" in blob:
        return (
            "the tree shadows a stdlib module, so mypy refuses to analyze it. "
            "Stage the package under a directory that is NOT on the stdlib path "
            "(a symlink farm works) and re-run with --no-namespace-packages. "
            "This is expected for any in-tree CPython Lib/ subpackage."
        )
    if "Duplicate module named" in blob:
        return (
            "two files map to the same module name -- usually a scan root that "
            "contains both a package and a loose copy of its modules. Narrow the "
            "target or add an exclude."
        )
    if "Cannot find implementation or library stub" in blob:
        return (
            "unresolved imports: mypy is running outside the project's "
            "environment. Run it from an interpreter that can import the project."
        )
    if "error: Cannot parse" in blob or "syntax error" in blob.lower():
        return "the target contains a file mypy cannot parse"
    return (
        "mypy exited without reporting how many files it checked; treat this run "
        "as having produced no information, not as clean"
    )


def _line_groups(findings: list[dict]) -> list[dict]:
    """Lines that produced more than one finding, most findings first.

    One inferred type or one misused name can produce many errors on a single
    line, so the finding count
    overstates the number of problems; `summary.distinct_lines` is the
    honest headline and these groups are the place to start.
    """
    groups: dict[tuple[str, int], dict] = {}
    for f in findings:
        g = groups.setdefault(
            (f["file"], f["line"]),
            {"file": f["file"], "line": f["line"], "count": 0, "codes": {}},
        )
        g["count"] += 1
        code = f["code"] or "(none)"
        g["codes"][code] = g["codes"].get(code, 0) + 1
    multi = [g for g in groups.values() if g["count"] > 1]
    multi.sort(key=lambda g: (-g["count"], g["file"], g["line"]))
    return multi


def _finding_key(f: dict) -> tuple:
    return (f["file"], f["line"], f["code"], f["message"])


def _labelled_pass(
    mypy_bin: str,
    targets: list[str],
    config: str | None,
    flags: list[str],
    name: str,
    cache_dir: str,
    baseline: set[tuple],
) -> dict:
    """Run one LABELLED extra pass and return only what it adds to the baseline.

    A pass that analyzed nothing, or whose parse disagrees with mypy's own
    count, reports ``ran: False`` with the reason -- never an empty,
    clean-looking list.
    """
    out, err, rc = _run_mypy(mypy_bin, targets, config, flags, cache_dir)
    found, stats = parse_output(out)
    if rc is None or not stats["files_checked"]:
        return {"ran": False, "error": _diagnose_failure(err, out, rc), "findings": []}
    if stats["errors"] is not None and len(found) != stats["errors"]:
        return {
            "ran": False,
            "error": (
                f"parsed {len(found)} finding(s) but mypy reported "
                f"{stats['errors']} error(s)"
            ),
            "findings": [],
        }
    added = []
    for f in found:
        if _finding_key(f) in baseline:
            continue
        f["pass"] = name
        added.append(f)
    return {"ran": True, "error": None, "findings": added}


def analyze(
    target: str,
    *,
    max_files: int = 0,
    mypy_bin: str = "mypy",
    strict_pass: bool = True,
    unreachable_pass: bool = True,
    unused_ignores_pass: bool = True,
) -> dict:
    """Run mypy over *target*, plus labelled extra passes.

    The passes are ``--disallow-any-unimported`` (phantom imports),
    ``--warn-unreachable`` (code a wrong annotation has silently removed from
    checking), and ``--warn-unused-ignores`` when the project's own config does
    not already enable it (stale ignores). Each is diffed against the baseline
    and reported separately, so the baseline count stays the project's own.
    """
    scan_root = Path(target).resolve()
    project_root = find_project_root(scan_root)
    # --max-files is honoured, not just accepted: a capped run hands mypy the
    # first N discovered files instead of the directory, with imports followed
    # silently so errors in the files left out are not reported either. The
    # envelope's files_capped says the result is partial.
    files = sorted(discover_python_files(scan_root))
    files_total = len(files)
    capped = max_files > 0 and files_total > max_files
    envelope = build_envelope(
        project_root, scan_root, files_total, max_files if capped else files_total
    )
    if capped:
        targets = [str(p) for p in files[:max_files]]
        cap_flags = ["--follow-imports=silent"]
    else:
        targets = [str(scan_root)]
        cap_flags = []
    version = mypy_version(mypy_bin)
    if version is None:
        return {
            **envelope,
            "status": "FAILED",
            "error": f"mypy not runnable as {mypy_bin!r}",
            "findings": [],
        }

    config = find_mypy_config(project_root)
    # mypy's cache goes to a private temporary directory, never the reviewed
    # project or the caller's cwd, and is removed however the run ends.
    cache_root = tempfile.mkdtemp(prefix="crt-mypy-cache-")
    try:
        return _analyze_with_cache(
            mypy_bin=mypy_bin,
            envelope=envelope,
            targets=targets,
            cap_flags=cap_flags,
            version=version,
            config=config,
            passes={
                "disallow-any-unimported": strict_pass,
                "warn-unreachable": unreachable_pass,
                "warn-unused-ignores": unused_ignores_pass,
            },
            cache_root=cache_root,
        )
    finally:
        shutil.rmtree(cache_root, ignore_errors=True)


def _analyze_with_cache(
    *,
    mypy_bin: str,
    envelope: dict,
    targets: list[str],
    cap_flags: list[str],
    version: str,
    config: str | None,
    passes: dict[str, bool],
    cache_root: str,
) -> dict:
    stdout, stderr, returncode = _run_mypy(
        mypy_bin, targets, config, cap_flags, os.path.join(cache_root, "baseline")
    )
    standalone_notes: list[dict] = []
    findings, stats = parse_output(stdout, standalone_notes)

    # A checker that analyzed nothing is a FAILED run, not a clean one. This is
    # the single most important line in the script: pyrefly reports `0 errors`
    # exit 0 on a tree its config discovery excluded, and a review that reads
    # that as "no type errors" is reporting a lie with full confidence.
    checked = stats["files_checked"]
    status = "OK" if returncode is not None and checked else "FAILED"
    failure_reason = None
    if status == "FAILED":
        failure_reason = _diagnose_failure(stderr, stdout, returncode)

    # Stale ignores come ONLY from mypy's own unused-ignore code. Never grep.
    # Under the project's config they are measured only if the config turns
    # warn_unused_ignores on; otherwise a labelled pass measures them. A zero
    # from a run where the warning was off is "not measured", not "none".
    ignores_in_config = config_enables(config, "warn_unused_ignores") or any(
        f["code"] == "unused-ignore" for f in findings
    )

    # LABELLED passes. Kept separate so the baseline count stays honest:
    # folding them in would make every project look worse by a flag change.
    # They are independent, so they run concurrently, each with its own cache.
    wanted = {
        name: ["--" + name]
        for name, enabled in passes.items()
        if enabled and status == "OK"
    }
    if ignores_in_config:
        wanted.pop("warn-unused-ignores", None)
    # Same for unreachable code: when the project's config already enables
    # warn_unreachable the baseline reports it, and a labelled pass would add
    # nothing -- an `added: 0` that reads like a measurement but is a no-op.
    unreachable_in_config = config_enables(config, "warn_unreachable")
    if unreachable_in_config:
        wanted.pop("warn-unreachable", None)
    baseline = {_finding_key(f) for f in findings}
    labelled: dict[str, dict] = {}
    if wanted:
        with ThreadPoolExecutor(max_workers=len(wanted)) as pool:
            futures = {
                name: pool.submit(
                    _labelled_pass,
                    mypy_bin,
                    targets,
                    config,
                    [*cap_flags, *flags],
                    name,
                    os.path.join(cache_root, name),
                    baseline,
                )
                for name, flags in wanted.items()
            }
            labelled = {name: fut.result() for name, fut in futures.items()}

    def pass_findings(name: str, code: str | None = None) -> list[dict] | None:
        result = labelled.get(name)
        if result is None or not result["ran"]:
            return None
        return [f for f in result["findings"] if code is None or f["code"] == code]

    phantom_imports = pass_findings("disallow-any-unimported")
    unreachable: list[dict] | None
    if unreachable_in_config and status == "OK":
        unreachable = [f for f in findings if f["code"] == "unreachable"]
        unreachable_source = "project-config"
    else:
        unreachable = pass_findings("warn-unreachable")
        unreachable_source = None if unreachable is None else "warn-unreachable-pass"

    stale_reason = None
    stale_ignores: list[dict] | None
    if ignores_in_config:
        stale_ignores = [f for f in findings if f["code"] == "unused-ignore"]
        stale_source = "project-config"
    else:
        stale_ignores = pass_findings("warn-unused-ignores", "unused-ignore")
        stale_source = None
        if stale_ignores is not None:
            stale_source = "warn-unused-ignores-pass"
        else:
            ran = labelled.get("warn-unused-ignores")
            how = f"failed ({ran['error']})" if ran else "did not run"
            stale_reason = (
                "not measured: the project's config does not enable "
                f"warn_unused_ignores and the labelled --warn-unused-ignores pass {how}"
            )

    # mypy's summary line states how many errors it found. If our parse produced a
    # different number we dropped findings, and silently reporting the smaller one
    # is exactly how three real errors went unreported on coverage.py. Fail loudly.
    parse_mismatch = None
    if (
        status == "OK"
        and stats["errors"] is not None
        and len(findings) != stats["errors"]
    ):
        parse_mismatch = (
            f"parsed {len(findings)} finding(s) but mypy reported "
            f"{stats['errors']} error(s) -- output format not understood"
        )
        status = "FAILED"
        failure_reason = parse_mismatch

    by_code: dict[str, int] = {}
    for f in findings:
        by_code[f["code"] or "(none)"] = by_code.get(f["code"] or "(none)", 0) + 1

    return {
        **envelope,
        "status": status,
        "failure_reason": failure_reason,
        "mypy_version": version,
        "config_file": config,
        "uses_project_config": config is not None,
        "returncode": returncode,
        "stderr": stderr.strip(),
        "files_checked": checked,
        "summary": {
            "total": len(findings),
            "defects": sum(1 for f in findings if f["kind"] == "defect"),
            "missing_annotations": sum(
                1 for f in findings if f["kind"] == "missing-annotation"
            ),
            "other": sum(1 for f in findings if f["kind"] == "other"),
            # Root causes are closer to lines than to findings: see line_groups.
            "distinct_lines": len({(f["file"], f["line"]) for f in findings}),
            "defect_lines": len(
                {(f["file"], f["line"]) for f in findings if f["kind"] == "defect"}
            ),
            "by_code": dict(sorted(by_code.items(), key=lambda kv: -kv[1])),
            # Only mypy's own unused-ignore counts. A grep for `# type: ignore`
            # measures how many ignores EXIST, not how many are stale. null when
            # nothing measured it -- see stale_ignores_reason.
            "stale_ignores": None if stale_ignores is None else len(stale_ignores),
            "stale_ignores_source": stale_source,
            "stale_ignores_reason": stale_reason,
            # Pre-correlated deliverable: an import that resolves to Any silently
            # disables every annotation that uses the name. null if the pass
            # did not run.
            "phantom_imports": (
                None if phantom_imports is None else len(phantom_imports)
            ),
            # Code mypy proves unreachable -- often because an annotation is
            # wrong, so the block is silently never type-checked. null if the
            # pass did not run.
            "unreachable": None if unreachable is None else len(unreachable),
            # `project-config` (the baseline reports it; no extra pass ran) or
            # `warn-unreachable-pass`; null when nothing measured it.
            "unreachable_source": unreachable_source,
            "parse_mismatch": parse_mismatch,
            "strict_pass_ran": phantom_imports is not None,
            "labelled_passes": {
                name: {
                    "ran": r["ran"],
                    "error": r["error"],
                    "added": len(r["findings"]),
                }
                for name, r in labelled.items()
            },
        },
        "findings": findings,
        "line_groups": _line_groups(findings),
        "phantom_import_findings": phantom_imports or [],
        "unreachable_findings": unreachable or [],
        "stale_ignore_findings": stale_ignores or [],
        # Notes that belong to no error: `annotation-unchecked` (an annotated
        # variable inside an untyped function body mypy skips), reveal_type.
        "standalone_notes": standalone_notes,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", nargs="?", default=".")
    parser.add_argument("--mypy-bin", default="mypy")
    parser.add_argument(
        "--no-strict-pass",
        action="store_true",
        help="skip the labelled --disallow-any-unimported pass",
    )
    parser.add_argument(
        "--no-unreachable-pass",
        action="store_true",
        help="skip the labelled --warn-unreachable pass",
    )
    parser.add_argument(
        "--no-unused-ignores-pass",
        action="store_true",
        help="skip the labelled --warn-unused-ignores pass (stale_ignores is "
        "then null unless the project's config enables the warning)",
    )
    parser.add_argument(
        "--max-files",
        type=int,
        default=0,
        help="check only the first N discovered files (files_capped says so)",
    )
    args = parser.parse_args()
    try:
        result = analyze(
            args.target,
            max_files=args.max_files,
            mypy_bin=args.mypy_bin,
            strict_pass=not args.no_strict_pass,
            unreachable_pass=not args.no_unreachable_pass,
            unused_ignores_pass=not args.no_unused_ignores_pass,
        )
        emit(result)
        sys.exit(1 if result["status"] == "FAILED" else 0)
    except Exception as exc:  # noqa: BLE001 -- top-level guard
        emit({"error": str(exc), "type": type(exc).__name__, "status": "FAILED"})
        sys.exit(1)


if __name__ == "__main__":
    main()
