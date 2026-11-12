#!/usr/bin/env python3
"""Collect a project's own security record: the threat model a security claim is judged against.

A review run labels findings "security" far more readily than the project's
maintainers would. The cure is to judge each claim against what the project
itself has said about security: its policy and scope, the audits it published
(with the severity it accepted for each finding), its advisories, and the
security fixes in its history. This script gathers those sources; the
`security-claims-skeptic` agent and the `security-gate` command use them.

Sources, in order of weight:

- **policy**: `SECURITY.md` (root, `.github/`, `docs/`), security policy pages
  in docs or templates, and security-model documents. Scope sections (in scope,
  out of scope, accepted risks, exclusions) are extracted; URLs to an external
  policy page are listed so a human or agent reads them.
- **audits**: documents that report a security audit or assessment -- a known
  audit firm, a link to a published report, or a finding table with a
  severity column. Table rows become **precedents**: past findings with the
  severity the project itself recorded and their status (remediated/accepted).
- **advisories**: GitHub security advisories (`gh api
  repos/OWNER/REPO/security-advisories`), and CVE/GHSA ids referenced in the
  project's docs and changelogs.
- **security issues and commits**: issues labelled `security` (via `gh`), and
  commits whose message mentions security, a vulnerability, a CVE or a GHSA.
- **incidents**: incident reports and post-mortems in the docs.

`status` is `found` (a scope statement or at least one precedent with a
severity), `partial` (some security record, but nothing that anchors a
severity), or `missing`. **A `missing` threat model blocks the gate**: a human
must supply one rather than an agent guessing it.

A network source that could not be queried is reported in `errors` -- never
as an empty result. "No advisories" and "could not ask" are different facts.

Usage:
    python find_threat_model.py [scope] [--repo OWNER/REPO] [--no-network]
        [--advisories-json PATH] [--issues-json PATH] [--format json|md]
        [--max-files N]

`--advisories-json` / `--issues-json` read a saved `gh api` response instead of
querying (offline runs and tests). `--repo` defaults to the `origin` remote
when it is a GitHub URL.
"""

from __future__ import annotations

import html
import itertools
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scan_common import (  # noqa: E402
    EXCLUDE_DIRS,
    build_envelope,
    emit,
    exit_on_help,
    relative_to_root,
    resolve_target,
)

# Documents worth reading. Code is not a security record; prose is.
TEXT_SUFFIXES = frozenset({".md", ".rst", ".txt", ".html", ".htm", ".jinja", ".j2", ""})
# Files larger than this are data, not documents.
MAX_DOC_BYTES = 1_000_000
# Directories that hold someone else's documents or generated output.
SKIP_DIRS = EXCLUDE_DIRS | {
    "site-packages",
    "htmlcov",
    "_build",
    "vendor",
    "third_party",
}

POLICY_NAMES = frozenset(
    {"security.md", "security.rst", "security.txt", "security.html"}
)
POLICY_NAME_RE = re.compile(
    r"security[-_ ]?(policy|model|considerations|disclosure)|"
    r"(vulnerability|responsible)[-_ ]disclosure|bug[-_ ]?bounty|threat[-_ ]?model",
    re.IGNORECASE,
)
# Headings whose sections define what the project does and does not consider a
# vulnerability. The skeptic needs exactly these.
SCOPE_HEADING_RE = re.compile(
    r"\b(in[- ]scope|out[- ]of[- ]scope|scope|not (considered|eligible|qualify|in scope)|"
    r"non[- ]qualifying|exclusions?|excluded|accepted risks?|known (issues|limitations|risks)|"
    r"threat model|trust(ed)? (boundar(y|ies)|model|parties)|what (is|isn't|is not) a "
    r"(security|vulnerability)|qualifying vulnerabilit|security model|limitations)\b",
    re.IGNORECASE,
)

AUDIT_RE = re.compile(
    r"\b(security (audit|assessment|review)|penetration test|pentest|audit(ed)? by|"
    r"(first|second|third|external|independent|third[- ]party) (security )?audit|"
    r"completes? .{0,20}audit)\b",
    re.IGNORECASE,
)
# Firms that publish security audits of open-source projects. A name match is
# evidence the document reports an audit, not proof; the table is the proof.
AUDIT_FIRMS = (
    "Trail of Bits",
    "NCC Group",
    "Cure53",
    "Include Security",
    "X41 D-Sec",
    "Radically Open Security",
    "Quarkslab",
    "Doyensec",
    "Atredis",
    "Least Authority",
    "Kudelski",
    "Bishop Fox",
    "Ada Logics",
    "Chainguard",
    "Open Source Technology Improvement Fund",
    "OSTIF",
    "Securing Open Source",
    "7ASecurity",
    "Shielder",
    "Sonar",
)
AUDIT_LINK_RE = re.compile(
    r"https?://[^\s)\]>\"']*(?:"
    r"github\.com/trailofbits/publications|"
    r"cure53\.de/[^\s)\]>\"']*\.pdf|"
    r"ostif\.org|"
    r"[^\s)\]>\"']*(?:audit|assessment|pentest)[^\s)\]>\"']*\.pdf"
    r")[^\s)\]>\"']*",
    re.IGNORECASE,
)
INCIDENT_RE = re.compile(
    r"\b(incident report|post[- ]?mortem|security incident)\b", re.IGNORECASE
)

CVE_RE = re.compile(r"\bCVE-\d{4}-\d{4,}\b")
GHSA_RE = re.compile(r"\bGHSA(?:-[23456789cfghjmpqrvwx]{4}){3}\b")
URL_RE = re.compile(r"https?://[^\s)\]>\"'<]+")

# Not bare "advisory": "advisory lock" is a database term and floods the list.
SECURITY_COMMIT_GREP = r"security|vulnerab|CVE-[0-9]|GHSA-|security advisory|exploit"
# Rules of engagement: what the project forbids researchers to do (testing
# production, research without authorization). A review must respect them and
# its disclosure should say it did.
ENGAGEMENT_RE = re.compile(
    r"(security research|bug bount|pentest|penetration test|red team|dependency confusion)"
    r"[^\n]{0,160}(without|written|authori[sz]ation|not permitted|prohibit|do not)|"
    r"(do not|don't|never)[^\n]{0,80}(test|scan|attack)[^\n]{0,80}(production|live|our (site|service|servers))",
    re.IGNORECASE,
)

SEVERITIES = (
    "critical",
    "high",
    "medium",
    "moderate",
    "low",
    "informational",
    "info",
    "none",
)

# Table header cells -> canonical field.
_HEADER_FIELDS = {
    "id": "id",
    "finding": "id",
    "finding id": "id",
    "ref": "id",
    "#": "id",
    "title": "title",
    "name": "title",
    "description": "title",
    "issue": "title",
    "summary": "title",
    "severity": "severity",
    "risk": "severity",
    "impact": "severity",
    "rating": "severity",
    "difficulty": "difficulty",
    "likelihood": "difficulty",
    "exploitability": "difficulty",
    "status": "status",
    "resolution": "status",
    "remediation": "status",
    "state": "status",
}

_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]*)\)")
_MD_REFLINK_RE = re.compile(r"\[([^\]]*)\]\[[^\]]*\]")
_TAG_RE = re.compile(r"<[^>]+>")
_CODE_URL_RE = re.compile(
    r"\.(py|js|ts|go|rs|c|h|rb|java|php)$|/blob/[^ ]+\.(py|js|ts)$", re.IGNORECASE
)


# --------------------------------------------------------------------------- helpers


def _iter_docs(root: Path):
    """Yield candidate prose documents under *root*, skipping vendored trees."""
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir():
                hidden = entry.name.startswith(".") and entry.name != ".github"
                if entry.name in SKIP_DIRS or hidden:
                    continue
                stack.append(entry)
            elif entry.is_file() and entry.suffix.lower() in TEXT_SUFFIXES:
                if entry.suffix == "" and entry.name.upper() not in {
                    "SECURITY",
                    "CHANGES",
                    "CHANGELOG",
                    "NEWS",
                    "HISTORY",
                }:
                    continue
                try:
                    if entry.stat().st_size > MAX_DOC_BYTES:
                        continue
                except OSError:
                    continue
                yield entry


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _is_html(path: Path) -> bool:
    return path.suffix.lower() in {".html", ".htm", ".jinja", ".j2"}


def _html_to_text(text: str) -> str:
    """Crude HTML -> Markdown-ish text: headings become `#` lines, tables pipe
    tables, other tags dropped."""
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", "", text)
    text = _html_tables_to_pipe(text)
    text = re.sub(r"(?is)\{#.*?#\}", "", text)
    text = re.sub(
        r"(?is)<h([1-6])\b[^>]*>(.*?)</h\1>",
        lambda m: (
            "\n"
            + "#" * int(m.group(1))
            + " "
            + _TAG_RE.sub("", m.group(2)).strip()
            + "\n"
        ),
        text,
    )
    text = re.sub(r"(?i)<li\b[^>]*>", "\n- ", text)
    text = re.sub(r"(?i)<(br|p|div|tr)\b[^>]*>", "\n", text)
    text = re.sub(r"(?s)\{%.*?%\}", "", text)
    text = _TAG_RE.sub("", text)
    lines = [" ".join(line.split()) for line in html.unescape(text).splitlines()]
    # Rejoin a bullet marker with its text when the markup put them on separate lines.
    out: list[str] = []
    for line in lines:
        if out and out[-1] == "-" and line:
            out[-1] = "- " + line
        elif line:
            out.append(line)
    return "\n".join(out)


def _plain(cell: str) -> tuple[str, list[str]]:
    """Strip Markdown links from a table cell; return (text, link targets)."""
    links = [m.group(2) for m in _MD_LINK_RE.finditer(cell)]
    text = _MD_LINK_RE.sub(r"\1", cell)
    text = _MD_REFLINK_RE.sub(r"\1", text)
    text = re.sub(r"[*_`]", "", text)
    return " ".join(text.split()), links


def _title_of(text: str, path: Path) -> str:
    for line in text.splitlines()[:40]:
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
        m = re.match(r"title:\s*[\"']?(.+?)[\"']?\s*$", stripped)
        if m:
            return m.group(1)
    return path.stem


def _sections(text: str) -> list[tuple[str, list[str]]]:
    """Split Markdown (or RST with underlined headings) into (heading, lines)."""
    lines = text.splitlines()
    out: list[tuple[str, list[str]]] = []
    heading, body = "", []
    for i, line in enumerate(lines):
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        md = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", line)
        rst = (
            line.strip()
            and re.fullmatch(r"([=\-~^\"'`#*+])\1{2,}", nxt.strip() or "x")
            and len(nxt.strip()) >= len(line.strip())
        )
        if md or rst:
            out.append((heading, body))
            heading, body = (md.group(1) if md else line.strip()), []
        elif (
            re.fullmatch(r"([=\-~^\"'`#*+])\1{2,}", line.strip() or "x") and body == []
        ):
            continue  # the underline of an RST heading just consumed
        else:
            body.append(line)
    out.append((heading, body))
    return [(h, b) for h, b in out if h or any(x.strip() for x in b)]


def _bullets(body: list[str], limit: int = 25) -> list[str]:
    """Bullets of a section, or its first paragraph when it has none."""
    items: list[str] = []
    for line in body:
        m = re.match(r"^\s*(?:[-*+]|\d+[.)])\s+(.+)", line)
        if m:
            items.append(" ".join(m.group(1).split()))
        elif items and line.startswith(("  ", "\t")) and line.strip():
            items[-1] += " " + " ".join(line.split())
    if not items:
        para = " ".join(" ".join(body).split())
        if para:
            items = [para[:600]]
    return items[:limit]


# --------------------------------------------------------------------------- tables

# Audit reports publish their finding tables in whatever markup the site uses.
# Each converter below rewrites one kind as a pipe table, which the one parser
# (`parse_finding_tables`) then reads.

_HTML_TABLE_RE = re.compile(r"(?is)<table\b[^>]*>(.*?)</table>")
_HTML_ROW_RE = re.compile(r"(?is)<tr\b[^>]*>(.*?)</tr>")
_HTML_CELL_RE = re.compile(r"(?is)<t([hd])\b[^>]*>(.*?)</t\1>")
_HTML_LINK_RE = re.compile(r"(?is)<a\b[^>]*\bhref=[\"']([^\"']*)[\"'][^>]*>(.*?)</a>")
_GRID_BORDER_RE = re.compile(r"^\s*\+(?:[-=]+\+)+\s*$")
_SIMPLE_BORDER_RE = re.compile(r"^\s*=+(?:\s+=+)+\s*$")


def _pipe_row(cells: list[str]) -> str:
    return "| " + " | ".join(c.replace("|", "/") for c in cells) + " |"


def _pipe_table(header: list[str], rows: list[list[str]]) -> list[str]:
    return [
        _pipe_row(header),
        "|" + "---|" * len(header),
        *(_pipe_row(r) for r in rows),
        "",
    ]


def _html_tables_to_pipe(text: str) -> str:
    """Rewrite every HTML ``<table>`` as a pipe table; links become Markdown links."""

    def cell_text(raw: str) -> str:
        raw = _HTML_LINK_RE.sub(lambda m: f"[{m.group(2)}]({m.group(1)})", raw)
        return " ".join(html.unescape(_TAG_RE.sub(" ", raw)).split())

    def convert(match: re.Match) -> str:
        rows = [
            [cell_text(c.group(2)) for c in _HTML_CELL_RE.finditer(row.group(1))]
            for row in _HTML_ROW_RE.finditer(match.group(1))
        ]
        rows = [r for r in rows if r]
        if len(rows) < 2:
            return match.group(0)
        return "\n" + "\n".join(_pipe_table(rows[0], rows[1:])) + "\n"

    return _HTML_TABLE_RE.sub(convert, text)


def _grid_table(lines: list[str]) -> list[str]:
    """An RST grid table (``+---+`` borders) as pipe-table lines."""
    border = lines[0]
    cuts = [i for i, ch in enumerate(border) if ch == "+"]
    header: list[str] | None = None
    rows: list[list[str]] = []
    current: list[list[str]] = []
    for line in lines[1:]:
        if _GRID_BORDER_RE.match(line):
            if current:
                rows.append([" ".join(" ".join(parts).split()) for parts in current])
            current = []
            if "=" in line and header is None and rows:
                header = rows.pop()
            continue
        cells = [line[a + 1 : b] for a, b in itertools.pairwise(cuts)]
        if not current:
            current = [[] for _ in cells]
        for parts, cell in zip(current, cells):
            parts.append(cell.strip().strip("|").strip())
    if header is None and rows:
        header = rows.pop(0)
    return _pipe_table(header, rows) if header and rows else []


def _simple_table(lines: list[str]) -> list[str]:
    """An RST simple table (``=====  =====`` borders) as pipe-table lines."""
    spans = [m.span() for m in re.finditer(r"=+", lines[0])]
    starts = [a for a, _ in spans]

    def split(line: str) -> list[str]:
        bounds = [*starts[1:], None]
        return [line[a:b].strip() for a, b in zip(starts, bounds)]

    borders = [i for i, line in enumerate(lines) if _SIMPLE_BORDER_RE.match(line)]
    body_from = borders[1] + 1 if len(borders) >= 3 else 1
    header_lines = lines[1 : borders[1]] if len(borders) >= 3 else []
    rows: list[list[str]] = []
    for line in lines[body_from:]:
        if _SIMPLE_BORDER_RE.match(line) or not line.strip():
            continue
        cells = split(line)
        if rows and not cells[0]:  # continuation of the previous row
            rows[-1] = [" ".join(x for x in pair if x) for pair in zip(rows[-1], cells)]
        else:
            rows.append(cells)
    header = split(header_lines[0]) if header_lines else (rows.pop(0) if rows else [])
    return _pipe_table(header, rows) if header and rows else []


def _list_table(lines: list[str]) -> list[str]:
    """An RST ``.. list-table::`` directive as pipe-table lines."""
    rows: list[list[str]] = []
    for line in lines[1:]:
        m = re.match(r"^\s*\*\s+-\s*(.*)$", line)
        if m:
            rows.append([m.group(1).strip()])
            continue
        m = re.match(r"^\s*-\s*(.*)$", line)
        if m and rows:
            rows[-1].append(m.group(1).strip())
        elif line.strip() and rows and not line.lstrip().startswith(":"):
            rows[-1][-1] = (rows[-1][-1] + " " + line.strip()).strip()
    return _pipe_table(rows[0], rows[1:]) if len(rows) >= 2 else []


def _rst_tables_to_pipe(text: str) -> str:
    """Rewrite RST grid, simple and list tables as pipe tables."""
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if _GRID_BORDER_RE.match(line):
            j = i + 1
            while j < len(lines) and lines[j].lstrip().startswith(("+", "|")):
                j += 1
            converted = _grid_table(lines[i:j])
            out += converted or lines[i:j]
            i = j
            continue
        if _SIMPLE_BORDER_RE.match(line):
            # Header border, header, border, rows, closing border; or no header.
            borders, j = 1, i + 1
            while j < len(lines):
                if _SIMPLE_BORDER_RE.match(lines[j]):
                    borders += 1
                    nxt = lines[j + 1] if j + 1 < len(lines) else ""
                    if borders >= 2 and (not nxt.strip() or borders == 3):
                        j += 1
                        break
                j += 1
            converted = _simple_table(lines[i:j])
            out += converted or lines[i:j]
            i = j
            continue
        if re.match(r"^\s*\.\.\s+list-table::", line):
            j = i + 1
            while j < len(lines) and (not lines[j].strip() or lines[j][:1].isspace()):
                j += 1
            converted = _list_table(lines[i:j])
            out += converted or lines[i:j]
            i = j
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


def parse_finding_tables(text: str) -> list[dict]:
    """Rows of every finding table whose header has a severity-like column.

    Pipe tables are read directly; HTML tables and RST grid, simple and list
    tables are rewritten as pipe tables first. Each row becomes ``{id, title,
    severity, difficulty, status, fix_refs}`` with Markdown links stripped;
    ``fix_refs`` keeps their targets (the PR that remediated a finding is often
    only a link in the status cell).
    """
    rows: list[dict] = []
    lines = _rst_tables_to_pipe(_html_tables_to_pipe(text)).splitlines()
    i = 0
    while i < len(lines) - 1:
        header, sep = lines[i], lines[i + 1]
        if "|" in header and re.fullmatch(
            r"\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*", sep
        ):
            cells = [_plain(c)[0].lower() for c in header.strip().strip("|").split("|")]
            fields = [_HEADER_FIELDS.get(c) for c in cells]
            if "severity" in fields and ("title" in fields or "id" in fields):
                j = i + 2
                while j < len(lines) and "|" in lines[j] and lines[j].strip():
                    raw = lines[j].strip().strip("|").split("|")
                    row: dict = {"fix_refs": []}
                    for field, cell in zip(fields, raw):
                        if not field:
                            continue
                        value, links = _plain(cell)
                        row.setdefault(field, value)
                        if field == "status":
                            row["fix_refs"].extend(links)
                    sev = (row.get("severity") or "").strip().lower()
                    if sev and any(sev.startswith(s) for s in SEVERITIES):
                        row["severity"] = _norm_severity(sev)
                        rows.append(row)
                    j += 1
                i = j
                continue
        i += 1
    return rows


def _norm_severity(value: str) -> str:
    v = value.strip().lower()
    for canonical, prefixes in (
        ("critical", ("critical",)),
        ("high", ("high",)),
        ("medium", ("medium", "moderate")),
        ("low", ("low",)),
        ("informational", ("informational", "info", "none")),
    ):
        if v.startswith(prefixes):
            return canonical
    return v


def _norm_status(value: str) -> str:
    v = (value or "").lower()
    if any(w in v for w in ("remediat", "fixed", "resolved", "patched", "mitigated")):
        return "remediated"
    if any(w in v for w in ("accept", "won't fix", "wontfix", "not fixed", "risk")):
        return "accepted"
    if any(w in v for w in ("partial",)):
        return "partially-remediated"
    if any(w in v for w in ("open", "unresolved", "pending", "in progress")):
        return "open"
    return v or "unknown"


# --------------------------------------------------------------------------- sources


def find_policy(root: Path, docs: list[Path]) -> list[dict]:
    """Security policy, disclosure and security-model documents."""
    out = []
    for path in docs:
        name = path.name.lower()
        if not (name in POLICY_NAMES or POLICY_NAME_RE.search(path.stem)):
            continue
        raw = _read(path)
        text = _html_to_text(raw) if _is_html(path) else raw
        kind = (
            "policy"
            if name in POLICY_NAMES or "polic" in name or "disclos" in name
            else ("bounty" if "bount" in name else "security-model")
        )
        scope = []
        for heading, body in _sections(text):
            items = _bullets(body)
            if heading and items and SCOPE_HEADING_RE.search(heading):
                scope.append({"heading": heading, "items": items})
        rel = relative_to_root(path, root)
        urls = sorted(
            {
                u.rstrip(".,;")
                for u in URL_RE.findall(raw)
                if not _CODE_URL_RE.search(u.rstrip(".,;"))
            }
        )
        out.append(
            {
                "path": rel,
                "kind": kind,
                "title": _title_of(text, path),
                "scope_sections": scope,
                # A short policy page states its scope in headings that match no
                # pattern ("If you found an issue with the service itself"):
                # keep every section of a policy document, it is the scope.
                "sections": [
                    {"heading": h, "items": _bullets(b, limit=8)}
                    for h, b in _sections(text)[:15]
                ]
                if kind != "security-model"
                else [],
                "urls": urls[:20],
                # A policy that only points elsewhere ("see https://.../security/")
                # carries no scope of its own: the URL must be read.
                "pointer_only": not scope and len(text.split()) < 120 and bool(urls),
            }
        )
    return out


def find_audits(root: Path, docs: list[Path]) -> tuple[list[dict], list[dict]]:
    """Audit reports (with their finding tables) and incident reports."""
    audits, incidents = [], []
    for path in docs:
        raw = _read(path)
        if not raw:
            continue
        text = _html_to_text(raw) if _is_html(path) else raw
        title = _title_of(text, path)
        head = title + " " + path.stem.replace("-", " ").replace("_", " ")
        firms = sorted({f for f in AUDIT_FIRMS if f.lower() in text.lower()})
        links = sorted({u.rstrip(".,;") for u in AUDIT_LINK_RE.findall(raw)})
        rows = parse_finding_tables(text)
        mentions_audit = bool(AUDIT_RE.search(text)) or "audit" in head.lower()
        if mentions_audit and (rows or firms or links):
            precedents = [
                {
                    "id": r.get("id") or "",
                    "title": r.get("title") or "",
                    "severity": r["severity"],
                    "difficulty": r.get("difficulty"),
                    "status": _norm_status(r.get("status", "")),
                    "status_raw": r.get("status"),
                    "fix_refs": [u for u in r["fix_refs"] if u.startswith("http")],
                    "source": relative_to_root(path, root),
                }
                for r in rows
            ]
            audits.append(
                {
                    "path": relative_to_root(path, root),
                    "title": title,
                    "firms": firms,
                    "report_links": links[:10],
                    "findings": precedents,
                    "severity_counts": _count(p["severity"] for p in precedents),
                    "accepted": [
                        p["id"] or p["title"]
                        for p in precedents
                        if p["status"] == "accepted"
                    ],
                }
            )
        elif INCIDENT_RE.search(head):
            incidents.append({"path": relative_to_root(path, root), "title": title})
    return audits, incidents


def find_engagement_rules(root: Path, docs: list[Path], limit: int = 20) -> list[dict]:
    """Lines that restrict security research against the project's services."""
    out = []
    for path in docs:
        for lineno, line in enumerate(_read(path).splitlines(), 1):
            if ENGAGEMENT_RE.search(line):
                text = " ".join(_TAG_RE.sub("", line).split())
                out.append(
                    {
                        "path": relative_to_root(path, root),
                        "line": lineno,
                        "text": text[:300],
                    }
                )
                if len(out) >= limit:
                    return out
    return out


def find_references(root: Path, docs: list[Path]) -> dict:
    """CVE and GHSA ids mentioned anywhere in the project's prose."""
    cves: dict[str, list[str]] = {}
    ghsas: dict[str, list[str]] = {}
    for path in docs:
        text = _read(path)
        rel = relative_to_root(path, root)
        for found, bucket in (
            (CVE_RE.findall(text), cves),
            (GHSA_RE.findall(text), ghsas),
        ):
            for ref in set(found):
                bucket.setdefault(ref, []).append(rel)
    return {
        "cve": {k: sorted(v)[:5] for k, v in sorted(cves.items())},
        "ghsa": {k: sorted(v)[:5] for k, v in sorted(ghsas.items())},
    }


def find_security_commits(root: Path, limit: int = 60) -> dict:
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "log",
                "-i",
                "-E",
                f"--grep={SECURITY_COMMIT_GREP}",
                "--format=%h%x09%ad%x09%s",
                "--date=short",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"error": f"git log failed: {exc}"}
    if proc.returncode != 0:
        return {"error": f"git log failed: {proc.stderr.strip()[:300]}"}
    commits = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3:
            commits.append({"commit": parts[0], "date": parts[1], "subject": parts[2]})
    return {"total": len(commits), "recent": commits[:limit]}


def _gh_json(args: list[str]) -> tuple[object | None, str | None]:
    try:
        proc = subprocess.run(
            ["gh", *args], check=False, capture_output=True, text=True, timeout=120
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"gh failed: {exc}"
    if proc.returncode != 0:
        return (
            None,
            f"gh {' '.join(args[:2])} exited {proc.returncode}: {proc.stderr.strip()[:300]}",
        )
    try:
        return json.loads(proc.stdout or "null"), None
    except json.JSONDecodeError as exc:
        return None, f"gh returned non-JSON: {exc}"


def _load_json(path: str) -> tuple[object | None, str | None]:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")), None
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"could not read {path}: {exc}"


def parse_advisories(data: object) -> list[dict]:
    """Normalize a `security-advisories` API response (a list, or pages of lists)."""
    items = data if isinstance(data, list) else []
    if items and all(isinstance(x, list) for x in items):  # gh --paginate --slurp
        items = [a for page in items for a in page]
    out = []
    for adv in items:
        if not isinstance(adv, dict) or not adv.get("ghsa_id"):
            continue
        out.append(
            {
                "id": adv.get("ghsa_id"),
                "cve": adv.get("cve_id"),
                "title": adv.get("summary") or "",
                "severity": _norm_severity(adv.get("severity") or "unknown"),
                "state": adv.get("state"),
                "published": adv.get("published_at"),
                "url": adv.get("html_url"),
                "source": "github-security-advisories",
            }
        )
    return out


def parse_issue_search(data: object, limit: int = 40) -> dict:
    if not isinstance(data, dict):
        return {"total": 0, "items": []}
    items = [
        {
            "number": it.get("number"),
            "state": it.get("state"),
            "title": it.get("title"),
            "is_pr": "pull_request" in it,
        }
        for it in data.get("items", [])
        if isinstance(it, dict)
    ]
    return {"total": data.get("total_count", len(items)), "items": items[:limit]}


def detect_repo(root: Path) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "remote", "get-url", "origin"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(
        r"github\.com[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", proc.stdout.strip()
    )
    return f"{m.group(1)}/{m.group(2)}" if m else None


def _count(values) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return out


# --------------------------------------------------------------------------- main


def analyze(
    target: str,
    *,
    max_files: int = 0,
    repo: str | None = None,
    network: bool = True,
    advisories_path: str | None = None,
    issues_path: str | None = None,
) -> dict:
    _, project_root, scan_root = resolve_target(target)
    all_docs = list(_iter_docs(scan_root))
    docs = all_docs[:max_files] if max_files else all_docs
    errors: list[str] = []

    policy = find_policy(project_root, docs)
    audits, incidents = find_audits(project_root, docs)
    references = find_references(project_root, docs)
    engagement = find_engagement_rules(project_root, docs)
    commits = find_security_commits(project_root)
    if "error" in commits:
        errors.append(commits["error"])

    repo = repo or detect_repo(project_root)
    advisories: list[dict] = []
    issues: dict = {"total": None, "items": [], "queried": False}
    if advisories_path:
        data, err = _load_json(advisories_path)
        advisories = parse_advisories(data) if err is None else []
        if err:
            errors.append(err)
    elif network and repo:
        data, err = _gh_json(
            [
                "api",
                "--paginate",
                "--slurp",
                f"repos/{repo}/security-advisories?per_page=100",
            ]
        )
        if err:
            errors.append(f"advisories not queried: {err}")
        else:
            advisories = parse_advisories(data)
    if issues_path:
        data, err = _load_json(issues_path)
        if err:
            errors.append(err)
        else:
            issues = {**parse_issue_search(data), "queried": True}
    elif network and repo:
        data, err = _gh_json(
            [
                "api",
                "-X",
                "GET",
                "search/issues",
                "-f",
                f"q=repo:{repo} label:security",
                "-f",
                "per_page=100",
            ]
        )
        if err:
            errors.append(f"security-labelled issues not queried: {err}")
        else:
            issues = {**parse_issue_search(data), "queried": True}
    if network is False and not (advisories_path and issues_path):
        errors.append(
            "network sources skipped (--no-network): advisories and labelled issues unknown"
        )
    elif network and not repo and not (advisories_path and issues_path):
        errors.append(
            "no GitHub repo detected (pass --repo): advisories and labelled issues unknown"
        )

    precedents = [p for a in audits for p in a["findings"]] + advisories
    has_scope = any(p["scope_sections"] for p in policy)
    if has_scope or precedents:
        status = "found"
    elif (
        policy
        or audits
        or incidents
        or references["cve"]
        or references["ghsa"]
        or issues["total"]
        or commits.get("total")
    ):
        status = "partial"
    else:
        status = "missing"

    envelope = build_envelope(project_root, scan_root, len(all_docs), len(docs))
    return {
        **envelope,
        "repo": repo,
        "status": status,
        "policy": policy,
        "external_policy_urls": sorted(
            {
                u
                for p in policy
                for u in p["urls"]
                if re.search(r"secur|disclos|bounty|vulnerab", u, re.IGNORECASE)
            }
        ),
        "audits": audits,
        "advisories": advisories,
        "references": references,
        "security_issues": issues,
        "security_commits": commits
        if "error" not in commits
        else {"total": None, "recent": []},
        "incidents": incidents,
        "engagement_rules": engagement,
        "precedents": precedents,
        "severity_scale": _count(p["severity"] for p in precedents),
        "accepted_risks": [
            {
                "id": p["id"],
                "title": p["title"],
                "severity": p["severity"],
                "source": p["source"],
            }
            for p in precedents
            if p.get("status") == "accepted"
        ],
        "errors": errors,
    }


def render_markdown(result: dict) -> str:
    """`threat-model.md`: what the skeptic reads before it judges any claim."""
    lines = [
        f"# Threat model sources — {Path(result['project_root']).name}",
        "",
        (
            f"THREAT-MODEL: {result['status']} PRECEDENTS={len(result['precedents'])} "
            f"AUDITS={len(result['audits'])} ADVISORIES={len(result['advisories'])} "
            f"POLICY={len(result['policy'])}"
        ),
        "",
    ]
    if result["errors"]:
        lines += ["## Not checked", ""] + [f"- {e}" for e in result["errors"]] + [""]
    lines += ["## Policy and scope", ""]
    if not result["policy"]:
        lines.append("- No security policy document found in the tree.")
    for p in result["policy"]:
        note = " (points elsewhere — read the URL)" if p["pointer_only"] else ""
        lines.append(f"- `{p['path']}` — {p['kind']}: {p['title']}{note}")
        shown = p["scope_sections"] or [
            s for s in p.get("sections", []) if s["heading"]
        ]
        for s in shown:
            lines.append(f"  - **{s['heading']}**")
            lines += [f"    - {item}" for item in s["items"]]
    if result["external_policy_urls"]:
        lines += ["", "External policy pages to read:", ""]
        lines += [f"- {u}" for u in result["external_policy_urls"]]
    if result["engagement_rules"]:
        lines += [
            "",
            "## Rules of engagement (respect them; say so in any disclosure)",
            "",
        ]
        lines += [
            f"- `{r['path']}:{r['line']}` {r['text']}"
            for r in result["engagement_rules"]
        ]
    lines += ["", "## Precedents (the project's own severities)", ""]
    if result["precedents"]:
        lines += [
            "| ID | Title | Severity | Status | Source |",
            "|---|---|---|---|---|",
        ]
        for p in result["precedents"]:
            status = p.get("status") or p.get("state") or ""
            lines.append(
                f"| {p['id']} | {p['title'].replace('|', '/')} | {p['severity']} | {status} | {p['source']} |"
            )
        lines += ["", f"Severity scale in use: {result['severity_scale']}"]
    else:
        lines.append(
            "- None. **No severity above Low can be anchored to a precedent.**"
        )
    if result["accepted_risks"]:
        lines += [
            "",
            "## Accepted risks (a claim duplicating one of these is not news)",
            "",
        ]
        lines += [
            f"- {a['id']}: {a['title']} ({a['severity']})"
            for a in result["accepted_risks"]
        ]
    lines += ["", "## Audits", ""]
    for a in result["audits"] or [{"path": None}]:
        if a["path"] is None:
            lines.append("- None found in the tree.")
            break
        firms = ", ".join(a["firms"]) or "firm not named"
        lines.append(
            f"- `{a['path']}` — {a['title']} ({firms}); findings: {a['severity_counts'] or 'no table'}"
        )
        lines += [f"  - report: {u}" for u in a["report_links"]]
    refs = result["references"]
    if refs["cve"] or refs["ghsa"]:
        lines += ["", "## CVE / GHSA references", ""]
        lines += [
            f"- {k}: {', '.join(v)}" for k, v in {**refs["cve"], **refs["ghsa"]}.items()
        ]
    issues = result["security_issues"]
    lines += [
        "",
        f"## Security-labelled issues: {issues['total'] if issues['queried'] else 'not queried'}",
        "",
    ]
    lines += [
        f"- #{i['number']} [{i['state']}] {i['title']}" for i in issues["items"][:20]
    ]
    commits = result["security_commits"]
    lines += ["", f"## Security-related commits: {commits['total']}", ""]
    lines += [
        f"- {c['commit']} {c['date']} {c['subject']}" for c in commits["recent"][:20]
    ]
    if result["incidents"]:
        lines += ["", "## Incident reports", ""]
        lines += [f"- `{i['path']}` — {i['title']}" for i in result["incidents"]]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    exit_on_help(argv, __doc__)
    opts: dict = {
        "repo": None,
        "network": True,
        "advisories_path": None,
        "issues_path": None,
    }
    fmt, max_files, positional = "json", 0, []
    valued = {
        "--repo": "repo",
        "--advisories-json": "advisories_path",
        "--issues-json": "issues_path",
    }
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in valued and i + 1 < len(argv):
            opts[valued[arg]] = argv[i + 1]
            i += 2
        elif arg == "--format" and i + 1 < len(argv):
            fmt = argv[i + 1]
            i += 2
        elif arg == "--max-files" and i + 1 < len(argv):
            try:
                max_files = int(argv[i + 1])
            except ValueError:
                emit({"error": f"--max-files requires an integer, got '{argv[i + 1]}'"})
                sys.exit(2)
            i += 2
        elif arg == "--no-network":
            opts["network"] = False
            i += 1
        elif arg.startswith("--"):
            i += 1
        else:
            positional.append(arg)
            i += 1
    result = analyze(positional[0] if positional else ".", max_files=max_files, **opts)
    if fmt == "md":
        sys.stdout.write(render_markdown(result))
    else:
        emit(result)


if __name__ == "__main__":
    main()
