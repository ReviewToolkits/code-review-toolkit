#!/usr/bin/env python3
"""Scan files bound for publication for leaks, and rewrite internal finding ids.

Patterns written by hand for each run had both false positives and misses: a
path pattern matched inside an unrelated word, an internal-id pattern matched
inside the public ids, and suffixed ids and ids spelled as code identifiers
escaped the rewrite. This module is the tested replacement.

``scan`` -- report every occurrence of a forbidden term, a regex, or an
internal finding id, in file contents and in the files' relative paths.
Terms match on word boundaries: ``reports/`` does not match inside
``bugreports/``, and the internal id ``AB-12`` does not match inside the
public id ``XY-AB-0012``.

``sensitive`` -- the publication rule for a findings.json: a finding that is
not ``publication_sensitive`` must not mention the id of one that is, in any
field. Otherwise the public record reveals that the withheld finding exists.

Internal ids are ``<PREFIX>-<number>``, optionally followed by a lowercase
suffix (``F-012``, ``F-012a``), plus the spelling used in code
(``test_f_012``, ``f_012b``, any case). Numbers compare as integers, so
``F-12`` and ``F-012`` are the same id. The library functions (``term_regex``,
``id_regex``, ``rewrite_ids``, ``scan_text``, ``sensitive_refs``) are what
build_public_tree.py uses.

Usage:
    python leak_scan.py scan PATH [PATH ...] [--term TEXT ...]
        [--terms-file FILE] [--pattern REGEX ...] [--id-prefix PREFIX ...]
        [--allow REGEX ...] [--public-prefix PREFIX ...] [--case-sensitive]
    python leak_scan.py sensitive FINDINGS.json [--flag publication_sensitive]

Options:
    --term TEXT         A literal that must not appear (a project name, a
                        private path). Case-insensitive unless
                        --case-sensitive. Repeatable.
    --terms-file FILE   One term per line; blank lines and # comments skipped
    --pattern REGEX     A raw regular expression that must not match
    --id-prefix PREFIX  An internal finding-id prefix (``F`` for ``F-012``)
    --allow REGEX       A hit whose matched text fully matches this is ignored
    --public-prefix P   The public id prefix (``XY-AB``): internal ids are not
                        looked for inside ``XY-AB-0012`` or ``test_xy_ab_0012``
    --flag NAME         The field marking a withheld finding
                        (default: publication_sensitive)

Exit status: 0 clean, 1 something found, 2 bad input. Output is JSON.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scan_common import emit, exit_on_help

DEFAULT_FLAG = "publication_sensitive"
_SKIP_DIRS = {".git", "__pycache__"}

# An id of the form PREFIX-NUMBER[suffix], split into its parts.
_ID_PARTS = re.compile(r"^(?P<prefix>.+?)[-_](?P<num>\d+)(?P<suffix>[a-z]?)$")


def term_regex(term: str, case_sensitive: bool = False) -> re.Pattern[str]:
    """A regex for a literal *term* that matches only on word boundaries.

    A boundary is required only on a side where the term starts or ends with
    a word character: ``reports/`` must not match in ``bugreports/``, but
    ``/home/`` needs no boundary of its own.
    """
    if not term:
        raise ValueError("empty term")
    body = re.escape(term)
    if re.match(r"\w", term[0]):
        body = r"(?<!\w)" + body
    if re.match(r"\w", term[-1]):
        body += r"(?!\w)"
    return re.compile(body, 0 if case_sensitive else re.IGNORECASE)


def id_regex(
    prefixes: Iterable[str], public_prefixes: Iterable[str] = ()
) -> re.Pattern[str] | None:
    """One regex for every internal id spelling of *prefixes*.

    Groups: ``prefix``/``num`` for ``PREFIX-NUM`` (case-sensitive, as
    written in prose), ``iprefix``/``inum`` for the identifier spelling
    ``prefix_num`` (any case, as in ``test_prefix_012``). A following
    lowercase suffix is kept outside the match, so rewriting preserves it.
    The hyphen form may not follow a letter, digit or hyphen: that is what
    keeps ``AB-12`` from matching inside ``XY-AB-0012``. A public id in any
    spelling (``XY-AB-0012``, ``test_xy_ab_0012``) matches the ``public``
    group first, so the internal spellings are never looked for inside it.
    """
    alts = sorted({p for p in prefixes if p}, key=len, reverse=True)
    if not alts:
        return None
    names = "|".join(re.escape(p) for p in alts)
    public = ""
    pubs = sorted({p for p in public_prefixes if p}, key=len, reverse=True)
    if pubs:
        spelled = "|".join(
            "[-_]".join(re.escape(part) for part in p.split("-")) for p in pubs
        )
        public = rf"(?P<public>(?<![A-Za-z0-9])(?i:{spelled})[-_]\d+)|"
    return re.compile(
        public
        + rf"(?<![A-Za-z0-9-])(?P<prefix>{names})-(?P<num>\d+)(?!\d)"
        + rf"|(?<![A-Za-z0-9])(?P<iprefix>(?i:{names}))_(?P<inum>\d+)(?!\d)"
    )


def split_id(fid: str) -> tuple[str, int, str] | None:
    """``("F", 12, "a")`` for ``F-012a``; None for an id of another form."""
    m = _ID_PARTS.match(fid)
    if not m:
        return None
    return m.group("prefix"), int(m.group("num")), m.group("suffix")


def id_prefixes(ids: Iterable[str]) -> set[str]:
    """The prefixes of every ``PREFIX-NUMBER`` id in *ids*."""
    out = set()
    for fid in ids:
        parts = split_id(str(fid))
        if parts:
            out.add(parts[0])
    return out


def _canonical_prefix(raw: str, prefixes: Iterable[str]) -> str:
    """Map an identifier spelling (``ab``) back to its declared prefix (``AB``)."""
    for p in prefixes:
        if p.lower() == raw.lower():
            return p
    return raw


def iter_ids(
    text: str, rx: re.Pattern[str], prefixes: Iterable[str]
) -> Iterator[tuple[re.Match[str], tuple[str, int]]]:
    """Every internal-id match in *text* with its canonical ``(prefix, number)``."""
    prefixes = list(prefixes)
    for m in rx.finditer(text):
        if "public" in rx.groupindex and m.group("public") is not None:
            continue
        if m.group("prefix") is not None:
            yield m, (m.group("prefix"), int(m.group("num")))
        else:
            yield (
                m,
                (_canonical_prefix(m.group("iprefix"), prefixes), int(m.group("inum"))),
            )


def rewrite_ids(
    text: str,
    idmap: dict[str, str],
    prefixes: Iterable[str] | None = None,
    public_prefixes: Iterable[str] | None = None,
) -> tuple[str, list[str]]:
    """Replace internal ids in *text* with their public ids from *idmap*.

    ``F-012`` becomes ``PUB-0003``; ``F-012a`` becomes ``PUB-0003a``; the
    identifier spelling ``test_f_012`` becomes ``test_pub_0003``. Public ids
    already in the text are left alone (*public_prefixes* defaults to the
    prefixes of *idmap*'s values). Returns the new text and the internal ids
    found that have no public id (left as they were: the caller decides
    whether that is an error).
    """
    table: dict[tuple[str, int], str] = {}
    for internal, public in idmap.items():
        parts = split_id(internal)
        if parts:
            table[(parts[0], parts[1])] = public
    if prefixes is None:
        prefixes = {k[0] for k in table}
    prefixes = list(prefixes)
    if public_prefixes is None:
        public_prefixes = id_prefixes(idmap.values())
    rx = id_regex(prefixes, public_prefixes)
    if rx is None:
        return text, []
    unmapped: list[str] = []

    def sub(m: re.Match[str]) -> str:
        if "public" in rx.groupindex and m.group("public") is not None:
            return m.group(0)
        if m.group("prefix") is not None:
            key = (m.group("prefix"), int(m.group("num")))
            public = table.get(key)
            if public is None:
                unmapped.append(m.group(0))
                return m.group(0)
            return public
        key = (_canonical_prefix(m.group("iprefix"), prefixes), int(m.group("inum")))
        public = table.get(key)
        if public is None:
            unmapped.append(m.group(0))
            return m.group(0)
        spelled = public.replace("-", "_")
        return spelled.lower() if m.group("iprefix").islower() else spelled

    return rx.sub(sub, text), unmapped


def build_matchers(
    terms: Iterable[str] = (),
    patterns: Iterable[str] = (),
    id_prefixes_: Iterable[str] = (),
    case_sensitive: bool = False,
    public_prefixes: Iterable[str] = (),
) -> list[tuple[str, str, re.Pattern[str]]]:
    """``(kind, label, regex)`` for every forbidden term, pattern and id prefix."""
    id_prefixes_ = list(id_prefixes_)
    out = [("term", t, term_regex(t, case_sensitive)) for t in terms if t]
    out += [("pattern", p, re.compile(p)) for p in patterns if p]
    rx = id_regex(id_prefixes_, public_prefixes)
    if rx is not None:
        out.append(("internal-id", ",".join(sorted(set(id_prefixes_))), rx))
    return out


def scan_text(
    text: str,
    matchers: list[tuple[str, str, re.Pattern[str]]],
    allow: Iterable[re.Pattern[str]] = (),
) -> list[dict]:
    """Every hit in *text*: ``{line, col, kind, label, match}`` (1-based)."""
    allow = list(allow)
    starts = [0] + [m.end() for m in re.finditer("\n", text)]
    hits = []
    for kind, label, rx in matchers:
        for m in rx.finditer(text):
            if not m.group(0) or any(a.fullmatch(m.group(0)) for a in allow):
                continue
            if "public" in rx.groupindex and m.group("public") is not None:
                continue
            line = _line_of(starts, m.start())
            hits.append(
                {
                    "line": line,
                    "col": m.start() - starts[line - 1] + 1,
                    "kind": kind,
                    "label": label,
                    "match": m.group(0),
                }
            )
    hits.sort(key=lambda h: (h["line"], h["col"]))
    return hits


def _line_of(starts: list[int], offset: int) -> int:
    lo, hi = 0, len(starts) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if starts[mid] <= offset:
            lo = mid
        else:
            hi = mid - 1
    return lo + 1


def read_text_file(path: Path) -> str | None:
    """The file's text, or None for a binary file (a NUL in the first 8 KB)."""
    data = path.read_bytes()
    if b"\0" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace")


def _walk(paths: Iterable[Path]) -> Iterator[tuple[Path, str]]:
    """``(file, display path)`` for every file under *paths*."""
    for root in paths:
        if root.is_file():
            yield root, root.name
            continue
        for p in sorted(root.rglob("*")):
            rel = p.relative_to(root)
            if p.is_file() and not any(part in _SKIP_DIRS for part in rel.parts):
                yield p, rel.as_posix()


def scan_paths(
    paths: Iterable[Path],
    matchers: list[tuple[str, str, re.Pattern[str]]],
    allow: Iterable[re.Pattern[str]] = (),
) -> dict:
    """Scan every file under *paths*, contents and relative path both."""
    allow = list(allow)
    hits, binary = [], []
    scanned = 0
    for path, shown in _walk(paths):
        for h in scan_text(shown, matchers, allow):
            hits.append({"file": shown, "where": "path", **h})
        text = read_text_file(path)
        if text is None:
            binary.append(shown)
            continue
        scanned += 1
        hits.extend(
            {"file": shown, "where": "content", **h}
            for h in scan_text(text, matchers, allow)
        )
    return {
        "files_scanned": scanned,
        "files_binary": binary,
        "hits": hits,
        "clean": not hits,
    }


def _strings(value: Any, path: str = "") -> Iterator[tuple[str, str]]:
    """``(dotted field path, text)`` for every string inside *value*."""
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _strings(v, f"{path}.{k}" if path else str(k))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _strings(v, f"{path}[{i}]")


def findings_list(data: Any) -> list[dict]:
    """The findings in a JSON list or in an object's ``findings`` list."""
    if isinstance(data, dict):
        data = data.get("findings", [])
    if not isinstance(data, list):
        raise TypeError("expected a list of findings or {'findings': [...]}")
    return [f for f in data if isinstance(f, dict)]


def sensitive_refs(findings: list[dict], flag: str = DEFAULT_FLAG) -> dict:
    """Mentions of a withheld finding's id inside a publishable finding.

    A finding is withheld when *flag* is truthy on it. Every string field of
    every other finding (its own ``id`` excepted) is searched for the
    withheld ids, in any spelling ``iter_ids`` knows, and for ids of another
    form as exact, word-bounded literals.
    """
    sensitive = [str(f["id"]) for f in findings if f.get(flag) and f.get("id")]
    keyed: dict[tuple[str, int], str] = {}
    literal: list[str] = []
    for fid in sensitive:
        parts = split_id(fid)
        if parts:
            keyed[(parts[0], parts[1])] = fid
        else:
            literal.append(fid)
    prefixes = {k[0] for k in keyed}
    rx = id_regex(prefixes)
    lit_rx = [(fid, term_regex(fid, case_sensitive=True)) for fid in literal]
    violations = []
    for f in findings:
        if f.get(flag):
            continue
        for field, text in _strings(
            {k: v for k, v in f.items() if k not in {"id", flag}}
        ):
            refs = []
            if rx is not None:
                refs += [
                    keyed[key]
                    for _, key in iter_ids(text, rx, prefixes)
                    if key in keyed
                ]
            refs += [fid for fid, r in lit_rx if r.search(text)]
            if refs:
                violations.append(
                    {"id": f.get("id"), "field": field, "references": sorted(set(refs))}
                )
    return {
        "findings_total": len(findings),
        "withheld": len(sensitive),
        "violations": violations,
        "clean": not violations,
    }


def _parse(
    argv: list[str], flags: set[str], multi: set[str]
) -> tuple[list[str], dict[str, Any]]:
    positional: list[str] = []
    opts: dict[str, Any] = {k: [] for k in multi}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in flags:
            opts[arg] = True
            i += 1
        elif arg.startswith("--"):
            if i + 1 >= len(argv):
                raise ValueError(f"{arg} needs a value")
            if arg in multi:
                opts[arg].append(argv[i + 1])
            else:
                opts[arg] = argv[i + 1]
            i += 2
        else:
            positional.append(arg)
            i += 1
    return positional, opts


def read_terms_file(path: str) -> list[str]:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    exit_on_help(argv, __doc__)
    if not argv or argv[0] not in {"scan", "sensitive"}:
        emit({"error": "first argument must be `scan` or `sensitive` (see --help)"})
        sys.exit(2)
    cmd, rest = argv[0], argv[1:]
    try:
        positional, opts = _parse(
            rest,
            flags={"--case-sensitive"},
            multi={
                "--term",
                "--pattern",
                "--id-prefix",
                "--allow",
                "--terms-file",
                "--public-prefix",
            },
        )
        if cmd == "sensitive":
            if len(positional) != 1:
                raise ValueError("sensitive needs exactly one findings.json")
            data = json.loads(Path(positional[0]).read_text(encoding="utf-8"))
            result = sensitive_refs(
                findings_list(data), opts.get("--flag", DEFAULT_FLAG)
            )
            emit(result)
            sys.exit(0 if result["clean"] else 1)
        if not positional:
            raise ValueError("scan needs at least one path")
        paths = [Path(p) for p in positional]
        for p in paths:
            if not p.exists():
                raise ValueError(f"no such path: {p}")
        terms = list(opts["--term"])
        for tf in opts["--terms-file"]:
            terms += read_terms_file(tf)
        if not (terms or opts["--pattern"] or opts["--id-prefix"]):
            raise ValueError(
                "nothing to scan for: give --term, --terms-file, --pattern or --id-prefix"
            )
        matchers = build_matchers(
            terms,
            opts["--pattern"],
            opts["--id-prefix"],
            case_sensitive=bool(opts.get("--case-sensitive")),
            public_prefixes=opts["--public-prefix"],
        )
        allow = [re.compile(a) for a in opts["--allow"]]
    except (OSError, ValueError, TypeError, re.error, json.JSONDecodeError) as exc:
        emit({"error": str(exc)})
        sys.exit(2)
    result = scan_paths(paths, matchers, allow)
    result["looked_for"] = {
        "terms": len(terms),
        "patterns": len(opts["--pattern"]),
        "id_prefixes": sorted(set(opts["--id-prefix"])),
    }
    emit(result)
    sys.exit(0 if result["clean"] else 1)


if __name__ == "__main__":
    main()
