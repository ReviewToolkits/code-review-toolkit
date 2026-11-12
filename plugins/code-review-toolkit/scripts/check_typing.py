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
``--check-untyped-defs`` runs when the config leaves the bodies of unannotated
functions unchecked (mypy's default): ``unchecked_untyped_bodies`` says how many
bodies a clean baseline never looked at, and the pass says what is in them.
When the config sets ``ignore_missing_imports`` (globally or per module), a
pass on a derived copy of the config with it off lists ``silenced_imports``:
the modules that silently became ``Any``. ``--disallow-any-unimported`` only
fires where an annotation names such a type, so it can add 0 while whole
packages are unchecked. ``mypy_python`` records the interpreter whose
site-packages mypy resolved against, since every import result depends on it.
The passes are independent and run concurrently.

Each phantom hit is traced to the module its annotation imports from and
tagged ``phantom_origin``; ``phantom_imports_real`` counts the hits whose
module does not exist. ``stale_stub_packages`` lists installed ``types-*`` /
``*-stubs`` distributions that shadow a library's own inline types. Through
``analyze()``: ``extra_passes`` adds labelled passes for any mypy flags,
``cache_dir`` keeps the cache for later runs, and ``boundary_map`` runs the
opt-in typed-boundary overlay -- bare boundary parameters (``request``)
annotated in an exported copy, so mypy checks what flows through them.

Usage:
    python check_typing.py [path] [--mypy-bin PATH] [--max-files N]
        [--no-strict-pass] [--no-unreachable-pass] [--no-unused-ignores-pass]
        [--no-untyped-defs-pass] [--no-silenced-imports-pass]
"""

from __future__ import annotations

import argparse
import ast
import configparser
import functools
import json
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
    EXCLUDE_DIRS,
    build_envelope,
    discover_python_files,
    emit,
    find_project_root,
    parse_source,
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

# `misc` is a grab bag, so it is split by message. "Invalid base class" and
# "Class cannot subclass ... (has type "Any")" make the class an Any subclass:
# every attribute access on its instances stops being checked, which is the
# finding. `"None" not callable` is mostly a narrowing mypy lost (a callable
# probed through a bool alias), so it is triaged before it is believed.
_MISC_FAMILIES = (
    (
        "any-subclass",
        re.compile(r'^(Invalid base class|Class cannot subclass .*"Any")'),
    ),
    ("none-not-callable", re.compile(r'^"None" not callable')),
)

# Errors whose RESULT silently becomes Any, so the code after them is unchecked
# even when the error itself is ignored or baselined: an Any base class, and a
# call no overload accepts (mypy types the result as Any or a guess).
_SILENT_ANY_OVERLOAD = "failed-overload"


def misc_family(message: str) -> str:
    """The family of a `misc` message: any-subclass, none-not-callable or other."""
    for family, pattern in _MISC_FAMILIES:
        if pattern.search(message):
            return family
    return "other"


def silent_any(code: str, message: str) -> str | None:
    """Why this error's result is silently Any, or None if it is not."""
    if code == "misc" and misc_family(message) == "any-subclass":
        return "any-subclass"
    if code == "call-overload":
        return _SILENT_ANY_OVERLOAD
    return None


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
            [mypy_bin, "--version"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
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
        if code == "misc":
            current["misc_family"] = misc_family(m["message"])
        why_any = silent_any(code, m["message"])
        if why_any:
            current["silent_any"] = why_any
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
    mypy_path: str | None = None,
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
    if mypy_path:
        # Searched before the config's own mypy_path (the overlay's stubs).
        env["MYPYPATH"] = os.pathsep.join(
            p for p in (mypy_path, os.environ.get("MYPYPATH", "")) if p
        )
    try:
        out = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_MYPY_TIMEOUT,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "", f"mypy timed out after {_MYPY_TIMEOUT}s", None
    except OSError as exc:
        return "", f"could not run mypy: {exc}", None
    return out.stdout, out.stderr, out.returncode


_SILENCED_PASS = "ignore-missing-imports-off"
_IMPORT_CODES = frozenset({"import-untyped", "import-not-found", "import"})
_QUOTED = re.compile(r'"([^"]+)"')


def ignore_missing_imports_scope(config: str | None) -> str | None:
    """`"global"`, `"per-module"` or None: where the config sets the option."""
    if not config:
        return None
    path = Path(config)
    if path.suffix == ".toml":
        data = _load_toml(path)
        if data is None:
            return None
        mypy = data.get("tool", {}).get("mypy", {})
        if mypy.get("ignore_missing_imports") is True:
            return "global"
        overrides = mypy.get("overrides", [])
        if isinstance(overrides, list) and any(
            isinstance(o, dict) and o.get("ignore_missing_imports") is True
            for o in overrides
        ):
            return "per-module"
        return None
    parser = configparser.RawConfigParser()
    try:
        parser.read(path, encoding="utf-8")
    except (OSError, configparser.Error):
        return None
    scope = None
    for section in parser.sections():
        if section != "mypy" and not section.startswith("mypy-"):
            continue
        try:
            on = parser.getboolean(section, "ignore_missing_imports", fallback=False)
        except ValueError:
            continue
        if on and section == "mypy":
            return "global"
        if on:
            scope = "per-module"
    return scope


def _load_toml(path: Path) -> dict | None:
    try:
        import tomllib
    except ImportError:  # Python 3.10
        return None
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _absolutize(value: str, base: Path, *, plugin: bool = False) -> str:
    """A config value as it reads from *base*, for a copy that lives elsewhere."""
    value = value.replace("$MYPY_CONFIG_FILE_DIR", str(base))
    if plugin:
        parts = []
        for item in value.split(","):
            item = item.strip()
            if (
                item
                and ("/" in item or item.endswith(".py"))
                and not os.path.isabs(item)
            ):
                item = str(base / item)
            parts.append(item)
        value = ", ".join(parts)
    return value


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    raise ValueError(f"cannot copy a {type(value).__name__} value")


def derive_config_without_ignores(
    config: str | None, out_dir: str
) -> tuple[str | None, str | None]:
    """Write a copy of *config* with every ignore_missing_imports turned off.

    Returns `(path, None)`, or `(None, reason)` when the config cannot be
    copied faithfully -- an unmeasured result, never a silently different run.
    Paths the copy would resolve differently ($MYPY_CONFIG_FILE_DIR, relative
    plugin paths) are made absolute against the original's directory.
    """
    if not config:
        return None, "no config"
    src = Path(config)
    base = src.resolve().parent
    os.makedirs(out_dir, exist_ok=True)
    if src.suffix == ".toml":
        data = _load_toml(src)
        if data is None:
            return None, "could not read the project's TOML config"
        mypy = dict(data.get("tool", {}).get("mypy", {}))
        overrides = mypy.pop("overrides", [])
        lines = ["[tool.mypy]"]
        try:
            for key, value in mypy.items():
                if key == "ignore_missing_imports":
                    continue
                if isinstance(value, str):
                    value = _absolutize(value, base, plugin=key == "plugins")
                elif key == "plugins" and isinstance(value, list):
                    value = [_absolutize(v, base, plugin=True) for v in value]
                lines.append(f"{key} = {_toml_value(value)}")
            lines.append("ignore_missing_imports = false")
            for override in overrides:
                lines += ["", "[[tool.mypy.overrides]]"]
                for key, value in override.items():
                    if key != "ignore_missing_imports":
                        lines.append(f"{key} = {_toml_value(value)}")
                lines.append("ignore_missing_imports = false")
        except ValueError as exc:
            return None, f"could not copy the project's config ({exc})"
        out = Path(out_dir) / "pyproject.toml"
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return str(out), None
    parser = configparser.RawConfigParser()
    try:
        parser.read(src, encoding="utf-8")
    except (OSError, configparser.Error) as exc:
        return None, f"could not read the project's config ({exc})"
    copy = configparser.RawConfigParser()
    copy.add_section("mypy")
    for section in parser.sections():
        if section != "mypy" and not section.startswith("mypy-"):
            continue
        if not copy.has_section(section):
            copy.add_section(section)
        for key, value in parser.items(section):
            copy.set(section, key, _absolutize(value, base, plugin=key == "plugins"))
        copy.set(section, "ignore_missing_imports", "False")
    copy.set("mypy", "ignore_missing_imports", "False")
    out = Path(out_dir) / "mypy.ini"
    with open(out, "w", encoding="utf-8") as fh:
        copy.write(fh)
    return str(out), None


_PYTHON_PROBE = (
    "import json, sys, sysconfig; print(json.dumps({"
    "'executable': sys.executable, "
    "'version': '.'.join(map(str, sys.version_info[:3])), "
    "'prefix': sys.prefix, "
    "'site_packages': sysconfig.get_paths()['purelib']}))"
)


def mypy_python_info(mypy_bin: str, config: str | None) -> dict:
    """The interpreter mypy resolves imports against, and its site-packages.

    mypy uses ``python_executable`` from the config if set, else the
    interpreter it runs under -- found from the launcher's ``#!`` line. The
    interpreter is asked with ``-I -B``, so nothing is imported from or
    written to the caller's directory.
    """
    exe = None
    source = None
    if config:
        path = Path(config)
        if path.suffix == ".toml":
            data = _load_toml(path) or {}
            exe = data.get("tool", {}).get("mypy", {}).get("python_executable")
        else:
            parser = configparser.RawConfigParser()
            try:
                parser.read(path, encoding="utf-8")
                exe = parser.get("mypy", "python_executable", fallback=None)
            except (OSError, configparser.Error):
                exe = None
        if exe:
            source = "config"
    if not exe:
        launcher = shutil.which(mypy_bin) or mypy_bin
        try:
            with open(launcher, "rb") as fh:
                first = fh.readline(512).decode("utf-8", "replace").strip()
        except OSError:
            first = ""
        if first.startswith("#!"):
            parts = first[2:].split()
            if parts and os.path.basename(parts[0]) == "env" and len(parts) > 1:
                exe = shutil.which(parts[1])
            elif parts:
                exe = parts[0]
            source = "mypy-launcher"
    if not exe:
        return {
            "executable": None,
            "source": None,
            "reason": "could not tell which interpreter mypy runs under",
        }
    try:
        out = subprocess.run(
            [exe, "-I", "-B", "-c", _PYTHON_PROBE],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        info = json.loads(out.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return {
            "executable": exe,
            "source": source,
            "reason": "the interpreter could not be queried",
        }
    return {**info, "source": source, "reason": None}


# Codes where a third-party callee or base class rejecting the call can mean
# the installed version of a dependency changed under the project.
_DRIFT_CODES = frozenset(
    {
        "call-arg",
        "call-overload",
        "arg-type",
        "attr-defined",
        "override",
        "misc",
        "valid-type",
        "type-var",
    }
)
_DEFINED_IN = re.compile(r'defined in "([\w.]+)"')
_MODULE_ATTR = re.compile(r'^Module "([\w.]+)" has no attribute')
_SUPERTYPE = re.compile(r'supertype "([\w.]+)\.\w+"')
_SITE_DIR = re.compile(
    r"[/\\](?:site|dist)-packages[/\\](.+?)(?:[/\\]__init__)?\.pyi?$"
)
_REQUIREMENT = re.compile(
    r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*([^;#]*)"
)

_DIST_PROBE = (
    "import json, sys\n"
    "import importlib.metadata as m\n"
    "pd = m.packages_distributions()\n"
    "out = {}\n"
    "for mod in sys.argv[1:]:\n"
    "    dists = []\n"
    "    for d in pd.get(mod, []):\n"
    "        try:\n"
    "            dists.append([d, m.version(d)])\n"
    "        except Exception:\n"
    "            dists.append([d, None])\n"
    "    out[mod] = dists\n"
    "print(json.dumps(out))\n"
)


def _normalize_dist(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def declared_dependencies(project_root: Path) -> dict[str, str]:
    """Normalized distribution name -> the requirement string that declares it.

    Read from `[project]` (dependencies and optional-dependencies), setup.cfg
    `install_requires`, a literal `install_requires`/`extras_require` in
    setup.py, and requirements*.txt at the root. Best effort: what is not
    found is reported as undeclared, never guessed.
    """
    specs: list[str] = []
    pyproject = project_root / "pyproject.toml"
    data = _load_toml(pyproject) if pyproject.is_file() else None
    if data:
        project = data.get("project", {})
        specs += [x for x in project.get("dependencies", []) if isinstance(x, str)]
        for group in project.get("optional-dependencies", {}).values():
            specs += [x for x in group if isinstance(x, str)]
    setup_cfg = project_root / "setup.cfg"
    if setup_cfg.is_file():
        parser = configparser.RawConfigParser()
        try:
            parser.read(setup_cfg, encoding="utf-8")
            specs += parser.get("options", "install_requires", fallback="").splitlines()
        except (OSError, configparser.Error):
            pass
    tree = parse_source(project_root / "setup.py")
    if tree is not None:
        for node in ast.walk(tree):
            if not isinstance(node, ast.keyword) or node.arg not in (
                "install_requires",
                "extras_require",
            ):
                continue
            for const in ast.walk(node.value):
                if isinstance(const, ast.Constant) and isinstance(const.value, str):
                    specs.append(const.value)
    for req in sorted(project_root.glob("requirements*.txt")):
        try:
            specs += req.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
    declared: dict[str, str] = {}
    for spec in specs:
        m = _REQUIREMENT.match(spec)
        if m and not spec.lstrip().startswith(("#", "-")):
            declared.setdefault(_normalize_dist(m.group(1)), spec.strip())
    return declared


def _upper_bounded(spec: str) -> bool:
    m = _REQUIREMENT.match(spec)
    rest = m.group(2) if m else spec
    return bool(re.search(r"<|==|~=", rest))


def _third_party_module(finding: dict, own: set[str]) -> str | None:
    """The non-project, non-stdlib module a finding's callee or base lives in."""
    candidates: list[str] = []
    for note in finding["notes"]:
        m = _DEFINED_IN.search(note["message"])
        if m:
            candidates.append(m.group(1))
        m = _SITE_DIR.search(note["file"])
        if m:
            candidates.append(re.sub(r"[/\\]", ".", m.group(1)))
    for pattern in (_MODULE_ATTR, _SUPERTYPE):
        m = pattern.search(finding["message"])
        if m:
            candidates.append(m.group(1))
    stdlib = set(getattr(sys, "stdlib_module_names", ())) | {
        "builtins",
        "_typeshed",
    }
    for module in candidates:
        top = module.split(".")[0]
        if top and top not in own and top not in stdlib:
            return module
    return None


def _distributions(python: str | None, modules: list[str]) -> dict[str, list]:
    if not python or not modules:
        return {}
    try:
        out = subprocess.run(
            [python, "-I", "-B", "-c", _DIST_PROBE, *modules],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        return json.loads(out.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}


def tag_dependency_drift(
    findings: list[dict],
    project_root: Path,
    own_packages: set[str],
    python: str | None,
) -> list[dict]:
    """Tag findings where a third-party callee or base rejects the project's code.

    A `call-arg` against a class defined in an installed dependency is either
    a project bug or the dependency changing under the project; when the
    dependency is declared without an upper bound, the second is likely and
    is the more valuable finding. Each tagged finding gets `dependency`:
    module, distribution, installed version, the declared requirement (null
    if undeclared), and whether it is upper-bounded. Returns the tagged ones.
    """
    hits = [
        (f, mod)
        for f in findings
        if f["code"] in _DRIFT_CODES
        and (mod := _third_party_module(f, own_packages)) is not None
    ]
    if not hits:
        return []
    tops = sorted({mod.split(".")[0] for _, mod in hits})
    dists = _distributions(python, tops)
    declared = declared_dependencies(project_root)
    tagged = []
    for f, module in hits:
        top = module.split(".")[0]
        installed = dists.get(top) or []
        names = [d[0] for d in installed] or [top]
        spec = next(
            (
                declared[_normalize_dist(n)]
                for n in names
                if _normalize_dist(n) in declared
            ),
            None,
        )
        f["dependency"] = {
            "module": module,
            "distribution": installed[0][0] if installed else None,
            "installed_version": installed[0][1] if installed else None,
            "declared": spec,
            "upper_bounded": None if spec is None else _upper_bounded(spec),
        }
        tagged.append(f)
    return tagged


def _own_packages(project_root: Path, scan_root: Path) -> set[str]:
    """Top-level import names that belong to the project itself."""
    own = {scan_root.name}
    for base in (project_root, project_root / "src"):
        try:
            entries = list(base.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir() and (entry / "__init__.py").is_file():
                own.add(entry.name)
            elif entry.suffix == ".py":
                own.add(entry.stem)
    return own


# ---------------------------------------------------------------------------
# Phantom imports: which hits are real?
# ---------------------------------------------------------------------------
#
# `--disallow-any-unimported` fires wherever an annotation names a type from a
# module mypy did not follow. On a project that silences untyped libraries
# with ignore_missing_imports that is hundreds of hits, none of them a phantom:
# the module exists, the config just turned it into Any on purpose. A REAL
# phantom names a module that does not exist at all (a misspelled or moved
# `TYPE_CHECKING` import). Each hit is traced from its annotation to the
# import that bound the name, and that module is looked up -- without being
# imported -- on the search path of the interpreter mypy resolves against.

_ARG_HIT = re.compile(r'^Argument (\d+) to "[^"]*" becomes')
_BASE_HIT = re.compile(r"^Base type (\S+) becomes")

_FIND_PROBE = (
    "import importlib.machinery as im, json, os, sys\n"
    "req = json.loads(sys.stdin.read())\n"
    "paths = req['paths'] + [p for p in sys.path if p]\n"
    "suffixes = im.all_suffixes() + ['.pyi']\n"
    "builtin = set(sys.builtin_module_names)\n"
    "def find(mod):\n"
    "    parts = mod.split('.')\n"
    "    if len(parts) == 1 and parts[0] in builtin:\n"
    "        return True\n"
    "    search = paths\n"
    "    for i, part in enumerate(parts):\n"
    "        last = i == len(parts) - 1\n"
    "        names = [part, part + '-stubs'] if i == 0 else [part]\n"
    "        dirs = [os.path.join(b, n) for b in search for n in names\n"
    "                if os.path.isdir(os.path.join(b, n))]\n"
    "        is_file = any(os.path.isfile(os.path.join(b, part + s))\n"
    "                      for b in search for s in suffixes)\n"
    "        if last:\n"
    "            return bool(dirs) or is_file\n"
    "        if not dirs:\n"
    "            return False\n"
    "        search = dirs\n"
    "    return True\n"
    "print(json.dumps({m: find(m) for m in req['modules']}))\n"
)


def find_modules(
    python: str | None, modules: list[str], paths: list[str]
) -> dict[str, bool] | None:
    """Whether each module exists on *python*'s search path (plus *paths*).

    Nothing is imported: the probe walks the path entries like the import
    system would (packages, namespace directories, module files, stubs
    packages). None when the interpreter cannot be asked.
    """
    if not python:
        return None
    if not modules:
        return {}
    try:
        out = subprocess.run(
            [python, "-I", "-B", "-c", _FIND_PROBE],
            input=json.dumps({"modules": modules, "paths": paths}),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        found = json.loads(out.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return found if isinstance(found, dict) else None


def _file_module(path: Path) -> str:
    """The dotted module name of *path*, from the packages above it."""
    parts = [] if path.stem == "__init__" else [path.stem]
    parent = path.parent
    while (parent / "__init__.py").is_file():
        parts.insert(0, parent.name)
        parent = parent.parent
    return ".".join(parts)


def _imports_of(tree: ast.Module, module: str, is_pkg: bool) -> dict[str, str]:
    """Local name -> the module that must exist for the binding to be real.

    `from a.b import C` binds C to module `a.b` (C may itself be a submodule,
    which `a.b` existing does not need); `import a.b` binds `a` and needs
    `a.b`; `import a.b as m` binds `m`. Relative imports are made absolute.
    """
    package = module.split(".") if is_pkg else module.split(".")[:-1]
    bound: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                keep = len(package) - (node.level - 1)
                if keep < 0:
                    continue
                source = ".".join(
                    package[:keep] + ([node.module] if node.module else [])
                )
            else:
                source = node.module or ""
            for alias in node.names:
                if alias.name != "*" and source:
                    bound[alias.asname or alias.name] = source
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    bound[alias.asname] = alias.name
                else:
                    top = alias.name.split(".")[0]
                    # The longest `import a.b.c` is what must exist.
                    if len(alias.name) > len(bound.get(top, "")):
                        bound[top] = alias.name
    return bound


def _annotation_names(node: ast.AST | None) -> list[str]:
    """Root names an annotation uses (`a` for `a.b.C`), quoted ones included."""
    if node is None:
        return []
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            node = ast.parse(node.value, mode="eval").body
        except (SyntaxError, ValueError, RecursionError):
            return []
    names: list[str] = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            names.append(sub.id)
        elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            names.extend(_annotation_names(sub))
    return names


def _hit_annotations(tree: ast.Module, line: int, message: str) -> list[ast.AST]:
    """The annotation (or base class) a phantom hit at *line* is about."""
    out: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.lineno != line:
                continue
            a = node.args
            ordered = [*a.posonlyargs, *a.args]
            ordered += [a.vararg] if a.vararg else []
            ordered += a.kwonlyargs + ([a.kwarg] if a.kwarg else [])
            m = _ARG_HIT.match(message)
            if m and 0 < int(m.group(1)) <= len(ordered):
                out.append(ordered[int(m.group(1)) - 1].annotation)
            elif message.startswith("Return type"):
                out.append(node.returns)
            else:
                out += [x.annotation for x in ordered] + [node.returns]
        elif isinstance(node, ast.ClassDef) and node.lineno == line:
            m = _BASE_HIT.match(message)
            bases = node.bases
            if m:
                named = [b for b in bases if ast.unparse(b).endswith(m.group(1))]
                bases = named or bases
            out += bases
        elif isinstance(node, ast.AnnAssign) and node.lineno == line:
            out.append(node.annotation)
    return [x for x in out if x is not None]


def _local_definitions(tree: ast.Module) -> dict[str, list[ast.AST]]:
    """Module-level classes (their bases) and aliases (their value), by name."""
    defs: dict[str, list[ast.AST]] = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            defs[node.name] = list(node.bases)
        elif (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            defs[node.targets[0].id] = [node.value]
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value is not None
        ):
            defs[node.target.id] = [node.value]
    return defs


def phantom_hit_modules(path: Path, line: int, message: str, cache: dict) -> list[str]:
    """Modules the annotation behind one phantom hit takes its names from.

    A name defined in the same file (a local base class or alias) is followed
    to where its own names come from, a few levels deep.
    """
    if path not in cache:
        tree = parse_source(path)
        if tree is None:
            cache[path] = None
        else:
            module = _file_module(path)
            cache[path] = (
                tree,
                _imports_of(tree, module, path.stem == "__init__"),
                _local_definitions(tree),
            )
    entry = cache[path]
    if entry is None:
        return []
    tree, bound, local = entry
    modules: list[str] = []
    seen: set[str] = set()
    frontier = [
        (n, 0)
        for a in _hit_annotations(tree, line, message)
        for n in _annotation_names(a)
    ]
    while frontier:
        name, depth = frontier.pop(0)
        if name in seen:
            continue
        seen.add(name)
        if name in bound:
            if bound[name] not in modules:
                modules.append(bound[name])
        elif name in local and depth < 3:
            frontier += [
                (n, depth + 1) for x in local[name] for n in _annotation_names(x)
            ]
    return modules


def _covers(module: str, silenced: set[str]) -> bool:
    return any(
        module == s or module.startswith(s + ".") or s.startswith(module + ".")
        for s in silenced
    )


def classify_phantoms(
    findings: list[dict],
    silenced: set[str] | None,
    own: set[str],
    python: str | None,
    search_paths: list[str],
    roots: list[Path],
) -> dict:
    """Tag each phantom hit with where its Any comes from; return the summary.

    `phantom_origin` is one of:

    - ``module-missing``: the module the name is imported from does not exist
      on mypy's search path. ``real_phantom`` is true unless the config also
      silences it for an outside package (a dependency simply not installed
      in this environment, which `module_found: false` still shows);
    - ``silenced-by-config``: the modules exist, and one of them is one
      ignore_missing_imports turned into Any on purpose (an annotation mixing
      `typing` or project types with it gets its Any from that one);
    - ``importable-unfollowed``: the module exists and is not silenced by the
      config (a `# type: ignore` on its import, follow_imports, no stubs);
    - ``unresolved-name``: the annotation could not be traced to an import.
    """
    cache: dict = {}
    per_hit: list[tuple[dict, list[str]]] = []
    wanted: set[str] = set()
    for f in findings:
        path = Path(f["file"])
        if not path.is_absolute():
            path = next(
                (r / path for r in roots if (r / path).is_file()), roots[0] / path
            )
        modules = phantom_hit_modules(path, f["line"], f["message"], cache)
        per_hit.append((f, modules))
        wanted.update(modules)
    found = find_modules(python, sorted(wanted), search_paths)
    by_origin: dict[str, int] = {}
    missing: set[str] = set()
    for f, modules in per_hit:
        f["phantom_modules"] = modules
        if not modules:
            origin, real = "unresolved-name", False
        elif found is None:
            origin, real = "not-measured", False
        else:
            absent = [m for m in modules if not found.get(m, False)]
            if absent:
                origin = "module-missing"
                real = any(
                    m.split(".")[0] in own or not (silenced and _covers(m, silenced))
                    for m in absent
                )
                if real:
                    missing.update(absent)
            elif silenced is not None and any(_covers(m, silenced) for m in modules):
                origin, real = "silenced-by-config", False
            else:
                origin, real = "importable-unfollowed", False
            f["module_found"] = not absent
        f["phantom_origin"] = origin
        f["real_phantom"] = real
        by_origin[origin] = by_origin.get(origin, 0) + 1
    return {
        "real": sum(1 for f, _ in per_hit if f["real_phantom"]),
        "by_origin": dict(sorted(by_origin.items())),
        "missing_modules": sorted(missing),
        "basis": (
            "not-measured: mypy's interpreter could not be asked"
            if found is None and wanted
            else "silenced-imports-pass"
            if silenced is not None
            else "no-silenced-imports-pass"
        ),
    }


# ---------------------------------------------------------------------------
# Stale stub packages
# ---------------------------------------------------------------------------
#
# A `types-foo` / `foo-stubs` distribution installs `foo-stubs/`, which mypy
# prefers over the package's own inline types (PEP 561 resolution order). Once
# `foo` ships `py.typed` itself, the stub package is at best redundant and at
# worst describes an older API: calls the real library rejects pass, and
# calls it accepts fail. Nothing in mypy's output says the stub won.

_STUB_PROBE = (
    "import importlib.metadata as m, json, os, sys\n"
    "paths = sys.argv[1:] or [p for p in sys.path if p]\n"
    "out, seen = [], set()\n"
    "for d in m.distributions(path=paths):\n"
    "    name = d.metadata['Name'] or ''\n"
    # Stub-only distributions are named `types-*` (typeshed) or `*-stubs`
    # (PEP 561); reading every distribution's file list would be slow.
    "    if not (name.lower().startswith('types-') or name.lower().endswith('-stubs')):\n"
    "        continue\n"
    "    tops = sorted({f.parts[0] for f in d.files or []\n"
    "                   if f.parts and f.parts[0].endswith('-stubs')})\n"
    "    for top in tops:\n"
    "        mod = top[:-len('-stubs')]\n"
    "        if (name, mod) in seen:\n"
    "            continue\n"
    "        seen.add((name, mod))\n"
    "        typed = os.path.join(str(d.locate_file(top)), 'py.typed')\n"
    "        try:\n"
    "            with open(typed) as fh:\n"
    "                partial = 'partial' in fh.read()\n"
    "        except OSError:\n"
    "            partial = False\n"
    "        runtime = next((os.path.join(b, mod) for b in paths\n"
    "                        if os.path.isdir(os.path.join(b, mod))), None)\n"
    "        if runtime is None:\n"
    "            continue\n"
    "        if not os.path.isfile(os.path.join(runtime, 'py.typed')):\n"
    "            continue\n"
    "        out.append({'stub_distribution': name, 'stub_version': d.version,\n"
    "                    'module': mod, 'stub_partial': partial,\n"
    "                    'runtime_distribution': None, 'runtime_version': None})\n"
    "if out:\n"
    "    owners = {}\n"
    "    for d in m.distributions(path=paths):\n"
    "        for f in d.files or []:\n"
    "            if f.parts and not f.parts[0].endswith(('-stubs', '.dist-info')):\n"
    "                owners.setdefault(f.parts[0].split('.')[0], set()).add(\n"
    "                    (d.metadata['Name'] or '', d.version))\n"
    "    for e in out:\n"
    "        dist = sorted(o for o in owners.get(e['module'], ())\n"
    "                      if o[0] != e['stub_distribution'])\n"
    "        if dist:\n"
    "            e['runtime_distribution'], e['runtime_version'] = dist[0]\n"
    "print(json.dumps(sorted(out, key=lambda e: (e['module'], e['stub_distribution']))))\n"
)


def stale_stub_packages(
    python: str | None, paths: list[str] | None = None
) -> tuple[list[dict] | None, str | None]:
    """Installed stub distributions whose runtime package ships `py.typed`.

    Asked of the interpreter mypy resolves against (``-I -B``: nothing is
    imported from the caller's directory or written anywhere). *paths*
    replaces its ``sys.path`` (tests). Returns ``(entries, None)`` or
    ``(None, reason)``. A partial stub package (``partial`` in its own
    ``py.typed``) is meant to be merged with the inline types, so it is
    listed with ``stub_partial: true`` rather than left out.
    """
    if not python:
        return None, "not measured: could not tell which interpreter mypy runs under"
    entries, reason = _stub_probe(python, tuple(paths or ()))
    return (None if entries is None else [dict(e) for e in entries]), reason


@functools.lru_cache(maxsize=8)
def _stub_probe(python: str, paths: tuple[str, ...]) -> tuple[list | None, str | None]:
    # Cached per interpreter: an environment does not change within one run,
    # and the probe reads every installed distribution's metadata.
    try:
        out = subprocess.run(
            [python, "-I", "-B", "-c", _STUB_PROBE, *paths],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        entries = json.loads(out.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None, "not measured: mypy's interpreter could not be queried"
    if not isinstance(entries, list):
        return None, "not measured: unexpected probe output"
    return entries, None


def _count_untyped_defs(files: list[Path]) -> int:
    """Functions with no parameter or return annotation (mypy's "untyped")."""
    count = 0
    for path in files:
        tree = parse_source(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            a = node.args
            params = a.posonlyargs + a.args + a.kwonlyargs
            params += [x for x in (a.vararg, a.kwarg) if x is not None]
            if node.returns is None and all(p.annotation is None for p in params):
                count += 1
    return count


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


# ---------------------------------------------------------------------------
# User-chosen passes and the typed-boundary overlay
# ---------------------------------------------------------------------------

# Flags an extra pass may not carry: they change where the output goes or how
# it reads, and a pass whose output does not parse would report nothing.
_EXTRA_PASS_DENY = (
    "--config-file",
    "--cache-dir",
    "--output",
    "-O",
    "--pretty",
    "--no-error-summary",
    "--color-output",
    "--force-color",
    "--version",
    "--help",
)


def validate_extra_passes(flags: list[str]) -> tuple[list[tuple[str, str]], list[dict]]:
    """`(passes, rejected)`: each accepted flag as `(label, flag)`.

    A flag must be one long option (``--strict-optional``,
    ``--always-true=DEBUG``); one this script already runs as a labelled pass
    is accepted once.
    """
    passes: list[tuple[str, str]] = []
    rejected: list[dict] = []
    for flag in flags:
        flag = flag.strip()
        why = None
        if not flag.startswith("--") or " " in flag:
            why = "not a single --long-option"
        elif flag.split("=", 1)[0] in _EXTRA_PASS_DENY:
            why = "changes where the output goes or how it parses"
        if why:
            rejected.append({"flag": flag, "reason": why})
            continue
        label = flag[2:]
        if label not in {lbl for lbl, _ in passes}:
            passes.append((label, flag))
    return passes, rejected


def _pass_dir(name: str) -> str:
    return re.sub(r"[^\w.-]", "_", name)


_OVERLAY_PASS = "boundary-overlay"
_OVERLAY_ALIAS = "_crt_boundary_{}"
_CONFIG_NAMES = ("mypy.ini", ".mypy.ini", "setup.cfg", "pyproject.toml")


def _export_tree(project_root: Path, export: Path) -> None:
    """Copy the project's Python sources and config files to *export*."""
    for dirpath, dirnames, filenames in os.walk(project_root):
        dirnames[:] = [
            d for d in dirnames if d not in EXCLUDE_DIRS and not d.startswith(".")
        ]
        rel = Path(dirpath).relative_to(project_root)
        for name in filenames:
            if not (
                name.endswith((".py", ".pyi"))
                or name == "py.typed"
                or (rel == Path(".") and name in _CONFIG_NAMES)
            ):
                continue
            dest = export / rel / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(Path(dirpath) / name, dest)


def _boundary_edits(
    tree: ast.Module, rel: str, by_name: dict[str, str], sites: list[dict]
) -> list[tuple[int, int, str, str]]:
    """`(line, byte_col, param, qualified_type)` for each bare parameter to annotate."""
    wanted = {
        (s["line"], s["param"]): s["annotation"] for s in sites if s.get("file") == rel
    }
    edits = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        a = node.args
        for arg in a.posonlyargs + a.args + a.kwonlyargs:
            if arg.annotation is not None:
                continue
            qualified = wanted.get((node.lineno, arg.arg)) or by_name.get(arg.arg)
            if qualified and "." in qualified and arg.end_col_offset is not None:
                edits.append((arg.lineno, arg.end_col_offset, arg.arg, qualified))
    return edits


def apply_boundary_overlay(
    export: Path,
    project_root: Path,
    files: list[Path],
    boundary_map: dict[str, str] | list[dict],
) -> dict:
    """Annotate bare boundary parameters in the exported copy of *files*.

    *boundary_map* is either ``{param_name: "pkg.mod.Type"}`` (every bare
    parameter of that name) or a list of sites ``{file, line, param,
    annotation}`` -- `file` project-relative, `line` the def's line, as in
    count_types' `untyped_boundaries` once a qualified type is filled in. Each
    annotation is a quoted ``"_crt_boundary_N.Type"`` with ``import pkg.mod
    as _crt_boundary_N`` appended at the end of the file, so no line moves and
    every finding maps back to the original line.
    """
    by_name = boundary_map if isinstance(boundary_map, dict) else {}
    sites = boundary_map if isinstance(boundary_map, list) else []
    annotated: list[dict] = []
    used: set[str] = set()
    for path in files:
        rel = str(path.relative_to(project_root))
        tree = parse_source(path)
        if tree is None:
            continue
        edits = _boundary_edits(tree, rel, by_name, sites)
        if not edits:
            continue
        target = export / rel
        lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
        aliases: dict[str, str] = {}
        for line, col, param, qualified in sorted(edits, reverse=True):
            module, _, name = qualified.rpartition(".")
            alias = aliases.setdefault(module, _OVERLAY_ALIAS.format(len(aliases)))
            raw = lines[line - 1].encode("utf-8")
            text = f': "{alias}.{name}"'.encode()
            lines[line - 1] = (raw[:col] + text + raw[col:]).decode("utf-8")
            annotated.append(
                {"file": rel, "line": line, "param": param, "type": qualified}
            )
            used.add(param)
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines += [
            f"import {m} as {a}  # crt boundary overlay\n" for m, a in aliases.items()
        ]
        target.write_text("".join(lines), encoding="utf-8")
    wanted_names = set(by_name) | {s.get("param") for s in sites}
    return {
        "sites_annotated": sorted(annotated, key=lambda e: (e["file"], e["line"])),
        "unmatched": sorted(n for n in wanted_names - used if n),
    }


def _overlay_pass(
    mypy_bin: str,
    project_root: Path,
    files: list[Path],
    targets: list[str],
    config: str | None,
    flags: list[str],
    cache_dir: str,
    baseline: set[tuple],
    boundary_map: dict[str, str] | list[dict],
    stubs: str | None = None,
) -> dict:
    """Annotate the boundaries in an export and report what mypy then adds.

    *stubs* is a directory searched first for stubs (``MYPYPATH``): a minimal
    ``.pyi`` for a library the config silences is what gives the annotated
    parameter attributes worth checking.
    """
    export = Path(cache_dir) / "export"
    shutil.rmtree(export, ignore_errors=True)
    export.mkdir(parents=True)
    _export_tree(project_root, export)
    applied = apply_boundary_overlay(export, project_root, files, boundary_map)
    result = {"ran": False, "error": None, "findings": [], **applied}
    if not applied["sites_annotated"]:
        result["error"] = "no bare parameter matched the boundary map"
        return result

    def moved(path: str) -> str:
        p = Path(path)
        try:
            return str(export / p.resolve().relative_to(project_root))
        except ValueError:
            return path

    out, err, rc = _run_mypy(
        mypy_bin,
        [moved(t) for t in targets],
        moved(config) if config else None,
        flags,
        os.path.join(cache_dir, "mypy"),
        mypy_path=stubs,
    )
    found, stats = parse_output(out)
    if rc is None or not stats["files_checked"]:
        result["error"] = _diagnose_failure(err, out, rc)
        return result
    edited = {(e["file"], e["line"]) for e in applied["sites_annotated"]}
    for f in found:
        # mypy prints a path under the caller's cwd relative to it, so a
        # cache directory below the cwd yields `cache/.../export/pkg/m.py`.
        path = Path(f["file"])
        if not path.is_absolute() and (Path.cwd() / path).is_file():
            path = Path.cwd() / path
        try:
            f["file"] = str(project_root / path.resolve().relative_to(export.resolve()))
        except ValueError:
            pass
        if _finding_key(f) in baseline:
            continue
        rel = f["file"]
        if Path(rel).is_absolute():
            try:
                rel = str(Path(rel).relative_to(project_root))
            except ValueError:
                pass
        # `request: Request = None` is the overlay's own doing, not a defect.
        f["overlay_artifact"] = (rel, f["line"]) in edited and f["code"] == "assignment"
        f["pass"] = _OVERLAY_PASS
        result["findings"].append(f)
    result["ran"] = True
    return result


def analyze(
    target: str,
    *,
    max_files: int = 0,
    mypy_bin: str = "mypy",
    strict_pass: bool = True,
    unreachable_pass: bool = True,
    unused_ignores_pass: bool = True,
    untyped_defs_pass: bool = True,
    silenced_imports_pass: bool = True,
    extra_passes: list[str] | None = None,
    cache_dir: str | None = None,
    boundary_map: dict[str, str] | list[dict] | None = None,
    boundary_stubs: str | None = None,
    boundary_flags: list[str] | None = None,
) -> dict:
    """Run mypy over *target*, plus labelled extra passes.

    *extra_passes* adds one labelled pass per mypy flag, reported like the
    built-in ones. *cache_dir* replaces the private temporary cache (kept
    afterwards, so later runs reuse it; never inside the reviewed project).
    *boundary_map* runs the opt-in typed-boundary overlay: see
    `apply_boundary_overlay`; *boundary_stubs* is a stub directory for it,
    and *boundary_flags* extra mypy flags for it (``--check-untyped-defs``:
    what a labelled pass with the same flags finds is subtracted, so the
    overlay reports only what the annotations expose).

    The passes are ``--disallow-any-unimported`` (phantom imports),
    ``--warn-unreachable`` (code a wrong annotation has silently removed from
    checking), ``--warn-unused-ignores`` when the project's own config does
    not already enable it (stale ignores), and ``--check-untyped-defs`` when
    the config leaves the bodies of unannotated functions unchecked. Each is diffed against the baseline
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
    extra, rejected = validate_extra_passes(extra_passes or [])
    overlay_flags, rejected_overlay = validate_extra_passes(boundary_flags or [])
    rejected += [{**r, "for": _OVERLAY_PASS} for r in rejected_overlay]
    # mypy's cache goes to a private temporary directory, never the reviewed
    # project or the caller's cwd, and is removed however the run ends -- or
    # to the caller's own --cache-dir, which is kept.
    keep_cache = cache_dir is not None
    if keep_cache:
        cache_path = Path(cache_dir).expanduser().resolve()
        if cache_path == project_root or project_root in cache_path.parents:
            return {
                **envelope,
                "status": "FAILED",
                "error": (
                    f"cache dir {cache_path} is inside the reviewed project; "
                    "a review must not write there"
                ),
                "findings": [],
            }
        cache_path.mkdir(parents=True, exist_ok=True)
        cache_root = str(cache_path)
    else:
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
                "check-untyped-defs": untyped_defs_pass,
                _SILENCED_PASS: silenced_imports_pass,
            },
            mypy_python=mypy_python_info(mypy_bin, config),
            untyped_defs=_count_untyped_defs(files[:max_files] if capped else files),
            cache_root=cache_root,
            extra_passes=extra,
            rejected_passes=rejected,
            overlay=(
                None
                if boundary_map is None
                else (
                    boundary_map,
                    files[:max_files] if capped else files,
                    boundary_stubs,
                    [flag for _, flag in overlay_flags],
                )
            ),
            cache_kept=keep_cache,
        )
    finally:
        if not keep_cache:
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
    untyped_defs: int | None = None,
    mypy_python: dict | None = None,
    extra_passes: list[tuple[str, str]] | None = None,
    rejected_passes: list[dict] | None = None,
    overlay: tuple | None = None,
    cache_kept: bool = False,
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
    wanted: dict[str, tuple[list[str], str | None]] = {
        name: (["--" + name], config)
        for name, enabled in passes.items()
        if enabled and status == "OK" and name != _SILENCED_PASS
    }
    # Imports that ignore_missing_imports turned into Any: re-run with that
    # option off everywhere (a derived copy of the config) and see which
    # import errors appear. Without the option set there is nothing to find.
    ignore_scope = ignore_missing_imports_scope(config)
    silenced_reason = None
    if not passes.get(_SILENCED_PASS):
        silenced_reason = "not measured: the pass was skipped"
    elif status != "OK":
        silenced_reason = "not measured: the baseline run failed"
    elif ignore_scope is not None:
        derived, why = derive_config_without_ignores(
            config, os.path.join(cache_root, _SILENCED_PASS + "-config")
        )
        if derived is None:
            silenced_reason = f"not measured: {why}"
        else:
            wanted[_SILENCED_PASS] = ([], derived)
    if ignores_in_config:
        wanted.pop("warn-unused-ignores", None)
    # Same for unreachable code: when the project's config already enables
    # warn_unreachable the baseline reports it, and a labelled pass would add
    # nothing -- an `added: 0` that reads like a measurement but is a no-op.
    unreachable_in_config = config_enables(config, "warn_unreachable")
    if unreachable_in_config:
        wanted.pop("warn-unreachable", None)
    # Without check_untyped_defs, mypy skips the body of every function that
    # has no annotation at all, and a clean run says nothing about them.
    untyped_checked = config_enables(config, "check_untyped_defs")
    if untyped_checked:
        wanted.pop("check-untyped-defs", None)
    extra_names: dict[str, str] = {}
    if status == "OK":
        for label, flag in extra_passes or []:
            if label in wanted or label in passes:
                continue  # a built-in pass already covers this flag
            extra_names["extra:" + label] = label
            wanted["extra:" + label] = ([flag], config)
    baseline = {_finding_key(f) for f in findings}
    labelled: dict[str, dict] = {}
    if wanted:
        with ThreadPoolExecutor(max_workers=len(wanted)) as pool:
            futures = {
                name: pool.submit(
                    _labelled_pass,
                    mypy_bin,
                    targets,
                    pass_config,
                    [*cap_flags, *flags],
                    name,
                    os.path.join(cache_root, _pass_dir(name)),
                    baseline,
                )
                for name, (flags, pass_config) in wanted.items()
            }
            labelled = {name: fut.result() for name, fut in futures.items()}

    overlay_result = None
    if overlay is not None:
        if status != "OK":
            overlay_result = {
                "ran": False,
                "error": "not run: the baseline run failed",
                "findings": [],
                "sites_annotated": [],
                "unmatched": [],
            }
        else:
            overlay_map, overlay_files, overlay_stubs, overlay_extra = overlay
            # What a labelled pass with a subset of the overlay's flags found
            # is the flags' doing, not the annotations'.
            known = set(baseline)
            for name, (flags, pass_config) in wanted.items():
                result = labelled.get(name)
                if (
                    result
                    and result["ran"]
                    and pass_config == config
                    and flags
                    and set(flags) <= set(overlay_extra)
                ):
                    known |= {_finding_key(f) for f in result["findings"]}
            overlay_result = _overlay_pass(
                mypy_bin,
                Path(envelope["project_root"]),
                overlay_files,
                targets,
                config,
                [*cap_flags, *overlay_extra],
                os.path.join(cache_root, _OVERLAY_PASS),
                known,
                overlay_map,
                overlay_stubs,
            )
        labelled[_OVERLAY_PASS] = overlay_result
    extra_findings = [
        f
        for name in extra_names
        if labelled.get(name, {}).get("ran")
        for f in labelled[name]["findings"]
    ]

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

    untyped_body = pass_findings("check-untyped-defs")

    silenced: list[dict] | None
    if ignore_scope is None and silenced_reason is None:
        silenced, silenced_source = [], "not-configured"
    else:
        silenced = pass_findings(_SILENCED_PASS)
        if silenced is not None:
            silenced = [f for f in silenced if f["code"] in _IMPORT_CODES]
            for f in silenced:
                m = _QUOTED.search(f["message"])
                f["module"] = m.group(1) if m else None
            silenced_source = "ignore-missing-imports-off-pass"
        else:
            silenced_source = None
            if silenced_reason is None:
                ran = labelled.get(_SILENCED_PASS)
                silenced_reason = (
                    f"not measured: the pass failed ({ran['error']})"
                    if ran
                    else "not measured: the pass did not run"
                )

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

    project_root = Path(envelope["project_root"])
    scan_root = Path(envelope["scan_root"])
    own = _own_packages(project_root, scan_root)
    phantom_summary = None
    if phantom_imports:
        phantom_summary = classify_phantoms(
            phantom_imports,
            None
            if silenced is None
            else {f["module"] for f in silenced if f.get("module")},
            own,
            (mypy_python or {}).get("executable"),
            [str(project_root), str(project_root / "src"), str(scan_root.parent)],
            [Path.cwd(), project_root, scan_root.parent],
        )

    stale_stubs, stale_stubs_reason = stale_stub_packages(
        (mypy_python or {}).get("executable")
    )

    drift = tag_dependency_drift(
        findings,
        Path(envelope["project_root"]),
        _own_packages(Path(envelope["project_root"]), Path(envelope["scan_root"])),
        (mypy_python or {}).get("executable"),
    )

    by_code: dict[str, int] = {}
    misc_by_family: dict[str, int] = {}
    for f in findings:
        by_code[f["code"] or "(none)"] = by_code.get(f["code"] or "(none)", 0) + 1
        if "misc_family" in f:
            family = f["misc_family"]
            misc_by_family[family] = misc_by_family.get(family, 0) + 1

    return {
        **envelope,
        "status": status,
        "failure_reason": failure_reason,
        "mypy_version": version,
        "config_file": config,
        "uses_project_config": config is not None,
        # What the baseline ran with, for the options this script reasons
        # about. With no config these are mypy's defaults (all off), and a
        # clean run means correspondingly less.
        "effective_flags": {
            "source": "project-config" if config else "mypy-defaults",
            "check_untyped_defs": untyped_checked,
            "warn_unreachable": unreachable_in_config,
            "warn_unused_ignores": config_enables(config, "warn_unused_ignores"),
            # "global", "per-module" or null: where the config makes missing
            # imports silently Any.
            "ignore_missing_imports": ignore_scope,
        },
        # The interpreter mypy resolved third-party imports against. Every
        # import-related result depends on it; null with a reason if unknown.
        "mypy_python": mypy_python,
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
            # Findings where an installed dependency's callee or base class
            # rejects the project's code -- see dependency_drift_findings.
            "dependency_drift_candidates": len(drift),
            # ... of which the dependency is declared without an upper bound,
            # so the installed version can have moved past what the code used.
            "dependency_drift_unbounded": sum(
                1 for f in drift if f["dependency"]["upper_bounded"] is False
            ),
            "distinct_lines": len({(f["file"], f["line"]) for f in findings}),
            "defect_lines": len(
                {(f["file"], f["line"]) for f in findings if f["kind"] == "defect"}
            ),
            "by_code": dict(sorted(by_code.items(), key=lambda kv: -kv[1])),
            # `misc` split by message (see _MISC_FAMILIES): any-subclass is a
            # real loss of checking, none-not-callable mostly lost narrowing.
            "misc_by_family": dict(sorted(misc_by_family.items())),
            # Findings whose result is silently Any (`silent_any` on each):
            # an Any base class, or a call no overload accepts. Code after
            # them is unchecked even if the error is ignored.
            "silent_any": sum(1 for f in findings if f.get("silent_any")),
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
            # Functions with no annotation at all, whose bodies mypy skips
            # unless check_untyped_defs is on. Counted from the AST of the
            # files handed to mypy (a config `exclude` is not applied).
            "untyped_defs": untyped_defs,
            "unchecked_untyped_bodies": (
                None if untyped_defs is None else 0 if untyped_checked else untyped_defs
            ),
            # Errors inside those bodies, from the labelled
            # --check-untyped-defs pass; null if it did not run (including
            # when the config already checks them: they are then in findings).
            "untyped_body_errors": (
                None if untyped_body is None else len(untyped_body)
            ),
            # Distinct modules that ignore_missing_imports turned into Any
            # (every name imported from one is unchecked). [] when the config
            # does not set the option; null when not measured -- see
            # silenced_imports_reason.
            "silenced_imports": (
                None
                if silenced is None
                else len({f["module"] or f["message"] for f in silenced})
            ),
            "silenced_imports_source": silenced_source,
            "silenced_imports_reason": silenced_reason,
            "parse_mismatch": parse_mismatch,
            # Of phantom_imports: hits whose module does not exist at all (a
            # misspelled or moved import), the ones worth reading first. The
            # rest come from modules that exist but were silenced or not
            # followed -- see phantom_origin on each finding. null when the
            # pass did not run or the modules could not be looked up.
            "phantom_imports_real": (
                None
                if phantom_imports is None
                else 0
                if phantom_summary is None
                else None
                if phantom_summary["basis"].startswith("not-measured")
                else phantom_summary["real"]
            ),
            "phantom_imports_by_origin": (
                None if phantom_summary is None else phantom_summary["by_origin"]
            ),
            "phantom_missing_modules": (
                None if phantom_summary is None else phantom_summary["missing_modules"]
            ),
            "phantom_origin_basis": (
                None if phantom_summary is None else phantom_summary["basis"]
            ),
            # Installed `types-*` / `*-stubs` packages that shadow a library
            # which now ships its own inline types (py.typed): mypy checks the
            # project against the stub, not the library. null when the
            # interpreter could not be asked -- see stale_stub_packages_reason.
            "stale_stub_packages": (None if stale_stubs is None else len(stale_stubs)),
            "stale_stub_packages_reason": stale_stubs_reason,
            # Passes the caller added (extra_passes): label -> findings added,
            # null when the pass failed; rejected flags with the reason.
            "extra_passes": {
                label: len(labelled[name]["findings"])
                if labelled.get(name, {}).get("ran")
                else None
                for name, label in extra_names.items()
            },
            "extra_passes_rejected": rejected_passes or [],
            # The opt-in typed-boundary overlay: what was annotated, and the
            # errors mypy reports once it is (overlay_artifact marks errors the
            # annotation itself causes, such as a None default). null if it
            # did not run.
            "boundary_overlay": (
                None
                if overlay_result is None
                else {
                    "ran": overlay_result["ran"],
                    "error": overlay_result["error"],
                    "sites_annotated": len(overlay_result["sites_annotated"]),
                    "unmatched": overlay_result["unmatched"],
                    "added": (
                        sum(
                            1
                            for f in overlay_result["findings"]
                            if not f["overlay_artifact"]
                        )
                        if overlay_result["ran"]
                        else None
                    ),
                }
            ),
            "cache_dir_kept": cache_kept,
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
        "dependency_drift_findings": drift,
        "phantom_import_findings": phantom_imports or [],
        "unreachable_findings": unreachable or [],
        "stale_ignore_findings": stale_ignores or [],
        "untyped_body_findings": untyped_body or [],
        "silenced_import_findings": silenced or [],
        "stale_stub_packages": stale_stubs or [],
        # One list for every pass added with extra_passes (`pass` names it).
        "extra_pass_findings": extra_findings,
        "boundary_overlay_findings": (overlay_result or {}).get("findings", []),
        "boundary_overlay_sites": (overlay_result or {}).get("sites_annotated", []),
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
        "--no-untyped-defs-pass",
        action="store_true",
        help="skip the labelled --check-untyped-defs pass",
    )
    parser.add_argument(
        "--no-silenced-imports-pass",
        action="store_true",
        help="skip the labelled pass that turns ignore_missing_imports off",
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
            untyped_defs_pass=not args.no_untyped_defs_pass,
            silenced_imports_pass=not args.no_silenced_imports_pass,
        )
        emit(result)
        sys.exit(1 if result["status"] == "FAILED" else 0)
    except Exception as exc:  # noqa: BLE001 -- top-level guard
        emit({"error": str(exc), "type": type(exc).__name__, "status": "FAILED"})
        sys.exit(1)


if __name__ == "__main__":
    main()
