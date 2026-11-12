#!/usr/bin/env python3
"""Build the public findings tree from a run's findings.json.

Selects the findings that may be published, gives them stable public ids,
rewrites every internal id in their text and reproducers, and writes one
directory per finding. Everything is built in memory and leak-scanned first;
nothing is written while any check fails.

Withheld (never published): ``publication_sensitive`` findings; findings
with a security claim (a non-empty ``security_claim``, or a legacy
``security`` / ``security_vulnerability`` flag) -- those go through
security-gate and coordinated disclosure, not a public tree; ``candidate``
findings; and ``refuted`` ones unless ``--include-refuted``.

Public ids are ``<PREFIX>-NNNN`` in FIX, CONSIDER, POLICY order (the input
order within a class). With ``--idmap`` an earlier map is reused, so a
published id never changes: new findings get the next free numbers, and a
mapped finding that is no longer public is reported as ``withdrawn`` (its
number is not reused).

Errors: a text field or reproducer that mentions a finding which is not
public (a structured ``related`` entry pointing at one is dropped instead,
so the withheld finding's existence is not revealed); a missing or binary
reproducer file; any leak-scan hit (``--term``/``--pattern``, or an
internal id left after the rewrite).

Output, under ``--out`` (which must be absent or empty):

    findings.json                     {"project", "schema_version", "commit", "findings"}
    reports/<ID>-<slug>/report.md     the finding, readable on its own
    reports/<ID>-<slug>/<reproducer>  internal ids in names and code rewritten

The id map is private: it is written only to ``--idmap-out``.

Usage:
    python build_public_tree.py FINDINGS.json --out DIR --prefix PREFIX
        [--idmap FILE] [--idmap-out FILE] [--root DIR] [--project NAME]
        [--commit SHA] [--term TEXT ...] [--terms-file FILE]
        [--pattern REGEX ...] [--allow REGEX ...] [--id-prefix PREFIX ...]
        [--include-refuted] [--include-field NAME ...] [--check]

Options:
    --prefix PREFIX     Public id prefix (`PUB` gives PUB-0001)
    --root DIR          Base for reproducer paths in `repro` (default:
                        FINDINGS.json's directory)
    --term/--terms-file/--pattern/--allow
                        Leak-scan inputs, as for leak_scan.py scan
    --id-prefix PREFIX  Another internal id prefix to rewrite or reject, such
                        as the agents' finding prefixes from brief.md (the
                        prefixes of findings.json's own ids are always used)
    --include-field N   Publish this extra finding field as well
    --check             Run every check, write nothing
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from leak_scan import (
    build_matchers,
    findings_list,
    id_prefixes,
    read_terms_file,
    rewrite_ids,
    scan_text,
    split_id,
)
from scan_common import emit, exit_on_help

CLASS_ORDER = {"FIX": 0, "CONSIDER": 1, "POLICY": 2, "ACCEPTABLE": 3}
# Prose and context fields published by default; internal bookkeeping
# (agents' report refs, dedup notes, pre-reproduction originals) is not.
TEXT_FIELDS = [
    "title",
    "mechanism",
    "failure_scenario",
    "fix",
    "evidence",
    "guarded_twin",
    "branch_status",
    "prior_art",
    "introduced_by",
    "caveats",
]
RELATION_FIELDS = ["related", "propagation_gap_of"]


def withheld_reason(f: dict, include_refuted: bool) -> str | None:
    """Why *f* stays out of the public tree, or None when it may go in."""
    if f.get("publication_sensitive"):
        return "publication_sensitive"
    claim = f.get("security_claim")
    if (isinstance(claim, dict) and any(claim.values())) or (
        claim and not isinstance(claim, dict)
    ):
        return "security_claim"
    if f.get("security") or f.get("security_vulnerability"):
        return "security_claim"
    status = str(f.get("status") or "candidate")
    if status == "candidate":
        return "candidate"
    if status == "refuted" and not include_refuted:
        return "refuted"
    return None


def assign_ids(
    public: list[dict], prefix: str, previous: dict[str, str], width: int = 4
) -> tuple[dict[str, str], list[str]]:
    """Public ids for *public*, reusing *previous*; returns (map, new internal ids)."""
    used = []
    for pid in previous.values():
        parts = split_id(pid)
        if parts and parts[0] == prefix:
            used.append(parts[1])
    nxt = max(used, default=0) + 1
    idmap: dict[str, str] = {}
    new = []
    for f in public:
        fid = str(f["id"])
        if fid in previous:
            idmap[fid] = previous[fid]
        else:
            idmap[fid] = f"{prefix}-{nxt:0{width}d}"
            nxt += 1
            new.append(fid)
    return idmap, new


def slug(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (title or "finding").lower()).strip("-")
    return "-".join(s.split("-")[:7])[:60].strip("-") or "finding"


def _locations(f: dict) -> list[str]:
    locs = f.get("locations")
    if isinstance(locs, str):
        locs = [locs]
    if not locs and f.get("location"):
        locs = [f["location"]] + list(f.get("sites") or [])
    return [str(x) for x in locs or []]


def _rewrite(
    value: Any,
    idmap: dict[str, str],
    prefixes: list[str],
    where: str,
    errors: list[str],
) -> Any:
    """Rewrite ids in every string inside *value*; unmapped ids are errors."""
    if isinstance(value, str):
        text, unmapped = rewrite_ids(value, idmap, prefixes)
        for u in unmapped:
            errors.append(f"{where}: mentions {u}, which is not public")
        return text
    if isinstance(value, dict):
        return {
            k: _rewrite(v, idmap, prefixes, f"{where}.{k}", errors)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [
            _rewrite(v, idmap, prefixes, f"{where}[{i}]", errors)
            for i, v in enumerate(value)
        ]
    return value


def _flat(value: Any) -> str:
    if isinstance(value, dict):
        return "; ".join(
            f"{k}: {_flat(v)}" for k, v in value.items() if v not in (None, "", [], {})
        )
    if isinstance(value, list):
        return ", ".join(_flat(v) for v in value)
    return "" if value is None else str(value)


def report_md(entry: dict, files: dict[str, str]) -> str:
    """A finding's ``report.md``: readable without the findings.json."""
    rows = [
        ("Severity", entry.get("severity")),
        ("Status", entry.get("status")),
        ("Location", f"`{entry['location']}`" if entry.get("location") else None),
        ("Also", ", ".join(f"`{s}`" for s in entry.get("sites") or []) or None),
        ("Shape", f"`{entry['shape']}`" if entry.get("shape") else None),
        ("Branches", _flat(entry.get("branch_status")) or None),
        ("Prior art", _flat(entry.get("prior_art")) or None),
        ("Introduced by", _flat(entry.get("introduced_by")) or None),
        ("Related", ", ".join(entry.get("related") or []) or None),
        ("Commit", f"`{entry['commit'][:12]}`" if entry.get("commit") else None),
    ]
    out = [f"# {entry['id']}: {entry.get('title') or ''}", "", "| | |", "|---|---|"]
    out += [f"| {k} | {str(v).replace('|', '/')} |" for k, v in rows if v]
    for heading, key in (("What happens", "failure_scenario"), ("Why", "mechanism")):
        if entry.get(key):
            out += ["", f"## {heading}", "", _flat(entry[key])]
    if entry.get("guarded_twin"):
        out += [
            "",
            f"The code already gets this right at {_flat(entry['guarded_twin'])}.",
        ]
    if entry.get("fix"):
        out += ["", "## Suggested fix", "", _flat(entry["fix"])]
    if files:
        out += ["", "## Reproducer"]
        for name, text in files.items():
            lang = (
                "bash"
                if name.endswith(".sh")
                else "python"
                if name.endswith(".py")
                else ""
            )
            out += ["", f"`{name}`:", "", f"```{lang}", text.rstrip(), "```"]
    if entry.get("evidence"):
        out += ["", "## Evidence", "", _flat(entry["evidence"])]
    if entry.get("caveats"):
        out += ["", "## Caveats", "", _flat(entry["caveats"])]
    return "\n".join(out) + "\n"


def build(
    findings: list[dict],
    *,
    prefix: str,
    root: Path,
    previous_idmap: dict[str, str] | None = None,
    project: str | None = None,
    commit: str | None = None,
    terms: list[str] | None = None,
    patterns: list[str] | None = None,
    allow: list[str] | None = None,
    include_refuted: bool = False,
    include_fields: list[str] | None = None,
    extra_prefixes: list[str] | None = None,
) -> dict:
    """Build the tree in memory: ``{files, idmap, report}``; nothing is written."""
    errors: list[str] = []
    withheld: Counter = Counter()
    public = []
    for f in findings:
        if not f.get("id"):
            errors.append(f"a finding has no id: {str(f.get('title'))[:60]!r}")
            continue
        reason = withheld_reason(f, include_refuted)
        if reason:
            withheld[reason] += 1
        else:
            public.append(f)
    public.sort(key=lambda f: CLASS_ORDER.get(str(f.get("classification")), 9))
    previous = previous_idmap or {}
    idmap, new_ids = assign_ids(public, prefix, previous)
    withdrawn = sorted(set(previous) - set(idmap))
    prefixes = sorted(
        id_prefixes(str(f.get("id")) for f in findings) | set(extra_prefixes or [])
    )
    if prefix in prefixes:
        errors.append(
            f"public prefix {prefix} is also an internal id prefix; choose another"
        )
    fields = TEXT_FIELDS + [k for k in include_fields or [] if k not in TEXT_FIELDS]

    files: dict[str, str] = {}
    entries = []
    for f in public:
        pid = idmap[str(f["id"])]
        where = pid
        entry: dict[str, Any] = {"id": pid}
        locs = _locations(f)
        entry["severity"] = f.get("classification")
        entry["status"] = f.get("status")
        entry["location"] = locs[0] if locs else None
        entry["sites"] = locs[1:]
        if f.get("shape"):
            entry["shape"] = f["shape"]
        for key in fields:
            if f.get(key) not in (None, "", [], {}):
                entry[key] = _rewrite(f[key], idmap, prefixes, f"{where}.{key}", errors)
        related: list[str] = []
        for key in RELATION_FIELDS:
            value = f.get(key)
            for r in [value] if isinstance(value, str) else value or []:
                if str(r) in idmap and idmap[str(r)] not in related:
                    related.append(idmap[str(r)])
        entry["related"] = related
        if commit:
            entry["commit"] = commit
        dirname = f"{pid}-{slug(str(entry.get('title') or ''))}"
        repro_files: dict[str, str] = {}
        repro = f.get("repro")
        for src in [repro] if isinstance(repro, str) else repro or []:
            if not isinstance(src, str) or src.endswith(".md"):
                continue
            path = root / src
            if not path.is_file():
                errors.append(f"{where}: reproducer {src} not found under {root}")
                continue
            data = path.read_bytes()
            if b"\0" in data[:8192]:
                errors.append(f"{where}: reproducer {src} is binary")
                continue
            name, _ = rewrite_ids(path.name, idmap, prefixes)
            if name in repro_files:
                # Two reproducers with one basename (slice-a/repro.py and
                # slice-b/repro.py): keep both, named after their directory.
                parent, _ = rewrite_ids(path.parent.name or "repro", idmap, prefixes)
                base, n = f"{parent}-{name}", 2
                name = base
                while name in repro_files:
                    name, n = f"{n}-{base}", n + 1
            text = _rewrite(
                data.decode("utf-8", errors="replace"),
                idmap,
                prefixes,
                f"{where}:{path.name}",
                errors,
            )
            repro_files[name] = text
            files[f"reports/{dirname}/{name}"] = text
        if repro_files:
            entry["repro"] = sorted(repro_files)
        entry["report"] = f"reports/{dirname}/report.md"
        files[entry["report"]] = report_md(entry, repro_files)
        entries.append(entry)

    meta = {
        "project": project,
        "schema_version": 1,
        "commit": commit,
        "findings": entries,
    }
    files["findings.json"] = json.dumps(meta, indent=1, ensure_ascii=False) + "\n"

    # The tree's own layout (`reports/<id>-<slug>/`, the `report` field) is
    # not scanned: a private run directory is often also called `reports/`,
    # and that term must stay usable. Everything taken from the findings is.
    matchers = build_matchers(
        terms or [], patterns or [], prefixes, public_prefixes=[prefix]
    )
    allow_rx = [re.compile(a) for a in allow or []]
    scanned = {name: text for name, text in files.items() if name != "findings.json"}
    scanned["findings.json"] = json.dumps(
        {
            **meta,
            "findings": [
                {k: v for k, v in e.items() if k != "report"} for e in entries
            ],
        },
        indent=1,
        ensure_ascii=False,
    )
    leaks = []
    for name, text in sorted(scanned.items()):
        for h in scan_text(name.split("/", 2)[-1], matchers, allow_rx):
            leaks.append({"file": name, "where": "path", **h})
        for h in scan_text(text, matchers, allow_rx):
            leaks.append({"file": name, "where": "content", **h})
    for leak in leaks:
        errors.append(
            f"leak: {leak['file']}:{leak['line']}: {leak['kind']} {leak['match']!r}"
        )
    report = {
        "findings_total": len(findings),
        "public": len(entries),
        "withheld": dict(withheld),
        "by_severity": dict(Counter(e["severity"] for e in entries)),
        "by_status": dict(Counter(e["status"] for e in entries)),
        "new_ids": len(new_ids),
        "withdrawn": withdrawn,
        "leaks": leaks,
        "errors": errors,
    }
    return {"files": files, "idmap": idmap, "report": report}


def write_tree(out: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = out / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        if name.endswith(".sh"):
            path.chmod(0o755)


def _parse(argv: list[str]) -> tuple[list[str], dict[str, Any]]:
    multi = {
        "--term",
        "--terms-file",
        "--pattern",
        "--allow",
        "--include-field",
        "--id-prefix",
    }
    flags = {"--include-refuted", "--check"}
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


def run(argv: list[str]) -> tuple[dict, int]:
    positional, opts = _parse(argv)
    if (
        len(positional) != 1
        or "--prefix" not in opts
        or ("--out" not in opts and not opts.get("--check"))
    ):
        raise ValueError(
            "usage: build_public_tree.py FINDINGS.json --out DIR --prefix PREFIX (see --help)"
        )
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", opts["--prefix"]):
        raise ValueError(
            "--prefix must be letters, digits and hyphens, starting with a letter"
        )
    src = Path(positional[0])
    findings = findings_list(json.loads(src.read_text(encoding="utf-8")))
    previous = (
        json.loads(Path(opts["--idmap"]).read_text(encoding="utf-8"))
        if "--idmap" in opts
        else None
    )
    terms = list(opts["--term"])
    for tf in opts["--terms-file"]:
        terms += read_terms_file(tf)
    out = Path(opts["--out"]) if "--out" in opts else None
    if out is not None and out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise ValueError(
            f"--out {out} exists and is not an empty directory; choose a new one"
        )
    built = build(
        findings,
        prefix=opts["--prefix"],
        root=Path(opts.get("--root") or src.resolve().parent),
        previous_idmap=previous,
        project=opts.get("--project"),
        commit=opts.get("--commit"),
        terms=terms,
        patterns=opts["--pattern"],
        allow=opts["--allow"],
        include_refuted=bool(opts.get("--include-refuted")),
        include_fields=opts["--include-field"],
        extra_prefixes=opts["--id-prefix"],
    )
    report = built["report"]
    report["written"] = False
    if report["errors"] or opts.get("--check"):
        return report, 1 if report["errors"] else 0
    write_tree(out, built["files"])
    if "--idmap-out" in opts:
        Path(opts["--idmap-out"]).write_text(
            # Withdrawn findings keep their entries: a published id is never
            # reassigned, and a finding that becomes public again gets it back.
            json.dumps({**(previous or {}), **built["idmap"]}, indent=1) + "\n",
            encoding="utf-8",
        )
        report["idmap_out"] = opts["--idmap-out"]
    report["written"] = True
    report["out"] = str(out)
    report["files_written"] = len(built["files"])
    return report, 0


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    exit_on_help(argv, __doc__)
    try:
        report, code = run(argv)
    except (
        OSError,
        ValueError,
        TypeError,
        re.error,
        json.JSONDecodeError,
        AttributeError,
    ) as exc:
        emit({"error": str(exc)})
        sys.exit(2)
    emit(report)
    sys.exit(code)


if __name__ == "__main__":
    main()
