#!/usr/bin/env python3
"""The security-claims gate: blind the claims, then check the skeptic's verdicts and sign off.

Two subcommands sit on each side of the `security-claims-skeptic` agent, and
two more check their inputs and outputs:

``blind`` -- select the findings that make a security claim and strip what
would anchor the skeptic on the finder's opinion: severity, classification,
``kind``, the impact prose and notes. What is left is the mechanism, the
locations, the evidence, and the claim itself (``security_claim: {attacker,
preconditions, gain}``), which the skeptic must check rather than accept. With
``--threat-model``, each claim also gets the project's precedents whose wording
overlaps it most (``precedent_hints``, at most two, ranked by distinctive words
and shared mechanism class): the cheapest way to catch a claim that repeats an
audit finding the project already published or accepted.

``signoff`` -- validate the verdicts against the threat model and print the
machine-readable line, the first line of `SECURITY-GATE.md`:

    SECURITY-GATE: SOLID=6 ARGUABLE=25 BUG=53 THREAT-MODEL=found PRECEDENTS=42 -- PASS

The terminal state is computed, not chosen:

- ``BLOCKED`` when the threat model is ``missing``; a claim has no verdict (or
  a verdict names no claim); a verdict is malformed; a severity above ``low``
  is not anchored to a named precedent of at least that severity; a ``solid``
  claim duplicates a precedent; or more than ``--max-disclosure`` (default 5)
  claims are ``solid`` without ``--override``.
- ``NEEDS-SIGN-OFF`` when the threat model is only ``partial``, an override was
  used, or any ``solid`` claim is rated above ``low``: a human confirms those.
- ``PASS`` otherwise. PASS means the disclosure set is defensible, not that it
  must be sent: sending is always a human decision.

Verdict records (the skeptic's output, a JSON list):

    {"id": "...", "verdict": "solid|arguable|bug", "severity": "high|medium|low|
     informational|n/a", "precedent": "<precedent id or null>", "duplicates":
     "<accepted/open precedent id or null>", "incomplete_fix_of": "<remediated
     precedent id or null>", "reason": "...", "strongest_counterargument": "...",
     "overstatements": ["..."], "evidence": "..."}

``score`` -- compare verdicts with reference verdicts (a fixture's
``expected.json``, or the maintainers' eventual answers): agreement, missed
duplicates, severities above the reference ceiling. Agreement with the people
who own the threat model is the calibration number that matters.

``lint`` -- publication hygiene of a consolidated findings.json, run before
anything leaves the run directory. Problems: a maintainer-facing prose field
(title, mechanism, failure_scenario, introduced_by) that names a review agent,
carries a ``||`` merge separator, or cites an id that is not a finding in the
file (an agent's own id, an earlier run's label); and a finding without
``publication_sensitive`` that mentions the id of one with it (``sensitive-ref``).
Exits 1 on any problem. ``--allow A,B`` accepts id-like tokens that are not ids.

Usage:
    python security_gate.py blind FINDINGS.json [--threat-model TM.json] [--all]
    python security_gate.py lint FINDINGS.json [--allow TOKEN,TOKEN]
    python security_gate.py score --verdicts V.json --expected EXPECTED.json
    python security_gate.py signoff --threat-model TM.json --verdicts V.json
        [--claims CLAIMS.json] [--max-disclosure N] [--override REASON]

``blind`` selects findings with a non-empty ``security_claim``, or the legacy
``security: true`` / ``security_vulnerability: true`` / ``kind`` of
vulnerability or hardening; ``--all`` takes every finding.
"""

from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scan_common import emit, exit_on_help  # noqa: E402

VERDICTS = ("solid", "arguable", "bug")
SEVERITY_RANK = {
    "n/a": -1,
    "none": 0,
    "informational": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}
DEFAULT_MAX_DISCLOSURE = 5

# Fields a finder writes that carry its opinion of the finding. The skeptic
# must form its own.
OPINION_FIELDS = frozenset(
    {
        "severity",
        "classification",
        "kind",
        "impact",
        "notes",
        "security",
        "security_vulnerability",
        "publication_sensitive",
        "cvss",
        "priority",
        "risk",
    }
)
KEEP_FIELDS = (
    "id",
    "title",
    "locations",
    "location",
    "sites",
    "mechanism",
    "failure_scenario",
    "security_claim",
    "status",
    "reproduction",
    "repro_output",
    "evidence",
    "prior_art",
    "related_public",
)

_WORD_RE = re.compile(r"[a-z][a-z0-9]{2,}")
_STOP = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "from",
        "into",
        "when",
        "are",
        "not",
        "can",
        "has",
        "have",
        "its",
        "was",
        "via",
        "any",
        "all",
        "but",
        "one",
        "two",
        "out",
        "use",
        "used",
        "uses",
        "using",
        "does",
        "each",
        "than",
        "then",
        "also",
        "only",
        "upon",
        "over",
        "under",
        "which",
        "while",
        "what",
        "where",
        "their",
        "there",
        "them",
        "they",
        "been",
        "being",
        "would",
        "could",
        "should",
    }
)


def _load(path: str):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _findings(data) -> list[dict]:
    if isinstance(data, dict):
        data = data.get("findings", [])
    return [f for f in data if isinstance(f, dict)]


def makes_claim(finding: dict) -> bool:
    claim = finding.get("security_claim")
    if isinstance(claim, dict) and any(str(v).strip() for v in claim.values() if v):
        return True
    if finding.get("security") is True or finding.get("security_vulnerability") is True:
        return True
    return str(finding.get("kind", "")).lower() in {"vulnerability", "hardening"}


def _stem(word: str) -> str:
    """Crude suffix stripping: enough that "flags"/"flag", "uploads"/"upload" match."""
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def _words(text: str) -> set[str]:
    return {_stem(w) for w in _WORD_RE.findall(text.lower()) if w not in _STOP}


# Mechanism classes: the vulnerability mechanism a title names, whatever the
# wording ("decompression bomb" and "denial of service" are one class). A shared
# class weighs more than a shared noun: two claims about tokens share a domain,
# two claims about a replay share a mechanism.
MECHANISM_CLASSES = {
    "denial-of-service": ("denial", "dos", "bomb", "exhaust", "redos", "unbounded"),
    "race": ("race", "toctou", "concurren"),
    "authorization": ("authoriz", "permission", "privileg", "escalat", "idor", "acl"),
    "authentication": ("authent", "mfa", "totp", "2fa"),
    "injection": ("inject", "xss", "escap", "unescap", "sanitiz", "shell"),
    "disclosure": ("leak", "disclos", "expos", "reveal", "enumerat"),
    "replay": ("replay", "nonce"),
    "bypass": ("bypass", "circumvent", "evad"),
    "rate-limit": ("rate", "throttl", "brute"),
    "signature": ("signatur", "hmac", "verif"),
    "redirect": ("redirect", "ssrf"),
    "audit-trail": ("audit",),
    "parser-mismatch": ("parser", "confus", "mismatch", "normaliz"),
    "weak-crypto": ("hash", "sha1", "md5", "weak"),
}
# A hint must carry this much of the precedent title's (weighted) wording.
HINT_MIN_SCORE = 0.4
# Hints scoring below this fraction of the best one for the same claim are noise.
HINT_RELATIVE_CUTOFF = 0.6
HINT_LIMIT = 2


def _mechanisms(words: set[str]) -> set[str]:
    return {
        name
        for name, stems in MECHANISM_CLASSES.items()
        if any(w.startswith(stems) for w in words)
    }


def _claim_text(finding: dict) -> str:
    return " ".join(
        str(finding.get(k) or "") for k in ("title", "mechanism", "failure_scenario")
    )


def _terms(text: str) -> set[str]:
    """Words of *text* plus a ``~class`` term for each mechanism class it names."""
    words = _words(text)
    return words | {f"~{m}" for m in _mechanisms(words)}


def term_weights(documents: list[str]) -> dict[str, float]:
    """Inverse document frequency over *documents* (precedent titles and claims).

    A word in half the documents ("token", "account" in a web application's
    audit) tells two texts apart far less than a word in two of them.
    """
    df: dict[str, int] = {}
    for doc in documents:
        for term in _terms(doc):
            df[term] = df.get(term, 0) + 1
    n = len(documents)
    return {t: math.log((n + 1) / (c + 1)) + 0.1 for t, c in df.items()}


def precedent_hints(
    finding: dict,
    precedents: list[dict],
    limit: int = HINT_LIMIT,
    *,
    weights: dict[str, float] | None = None,
) -> list[dict]:
    """Precedents whose title shares the most distinctive wording with the claim.

    Each shared word counts by its rarity (``weights``, IDF over the precedent
    titles and claims), and a shared mechanism class counts double. A hint needs
    two shared words, a score of at least ``HINT_MIN_SCORE`` (the weighted
    fraction of the precedent title covered), and a score within
    ``HINT_RELATIVE_CUTOFF`` of the claim's best hint; at most *limit* are kept.
    """
    if weights is None:
        weights = term_weights(
            [p.get("title", "") for p in precedents] + [_claim_text(finding)]
        )
    default = max(weights.values(), default=1.0)

    def weight(term: str) -> float:
        w = weights.get(term, default)
        return 2 * w if term.startswith("~") else w

    words = _terms(_claim_text(finding))
    scored = []
    for p in precedents:
        pw = _terms(p.get("title", ""))
        if not pw or not words:
            continue
        shared = pw & words
        plain = sorted(t for t in shared if not t.startswith("~"))
        if len(plain) < 2:
            continue
        score = sum(weight(t) for t in shared) / sum(weight(t) for t in pw)
        if score >= HINT_MIN_SCORE:
            mechanisms = sorted(t[1:] for t in shared if t.startswith("~"))
            scored.append((score, p, plain, mechanisms))
    scored.sort(key=lambda t: -t[0])
    if scored:
        best = scored[0][0]
        scored = [t for t in scored if t[0] >= HINT_RELATIVE_CUTOFF * best]
    return [
        {
            "id": p.get("id"),
            "title": p.get("title"),
            "severity": p.get("severity"),
            "status": p.get("status") or p.get("state"),
            "score": round(score, 2),
            "shared_words": plain,
            "shared_mechanisms": mechanisms,
        }
        for score, p, plain, mechanisms in scored[:limit]
    ]


def blind(
    findings: list[dict],
    *,
    precedents: list[dict] | None = None,
    take_all: bool = False,
) -> list[dict]:
    out = []
    weights = (
        term_weights(
            [p.get("title", "") for p in precedents]
            + [_claim_text(f) for f in findings]
        )
        if precedents
        else None
    )
    for f in findings:
        if not take_all and not makes_claim(f):
            continue
        record = {
            k: f[k] for k in KEEP_FIELDS if k in f and f[k] not in (None, "", [], {})
        }
        record.setdefault(
            "security_claim", {"attacker": "", "preconditions": "", "gain": ""}
        )
        if precedents:
            record["precedent_hints"] = precedent_hints(f, precedents, weights=weights)
        out.append(record)
    return out


def _precedent_index(threat_model: dict) -> dict[str, dict]:
    return {
        str(p.get("id")): p for p in threat_model.get("precedents", []) if p.get("id")
    }


def signoff(
    threat_model: dict,
    verdicts: list[dict],
    *,
    claims: list[dict] | None = None,
    max_disclosure: int = DEFAULT_MAX_DISCLOSURE,
    override: str | None = None,
) -> dict:
    precedents = _precedent_index(threat_model)
    tm_status = threat_model.get("status", "missing")
    blocking: list[str] = []
    review: list[str] = []
    seen: set[str] = set()

    if tm_status == "missing":
        blocking.append(
            "threat model missing: no policy, audit, advisory or security history found — a human must supply one"
        )
    elif tm_status == "partial":
        review.append(
            "threat model partial: nothing anchors a severity above low; a human confirms the scale"
        )

    for v in verdicts:
        vid = str(v.get("id", ""))
        if not vid:
            blocking.append("a verdict has no id")
            continue
        if vid in seen:
            blocking.append(f"{vid}: more than one verdict")
        seen.add(vid)
        verdict = v.get("verdict")
        if verdict not in VERDICTS:
            blocking.append(f"{vid}: verdict {verdict!r} is not one of {VERDICTS}")
            continue
        severity = str(v.get("severity") or "n/a").lower()
        if severity not in SEVERITY_RANK:
            blocking.append(f"{vid}: severity {severity!r} is not on the scale")
            continue
        for field in ("reason", "strongest_counterargument"):
            if not str(v.get(field) or "").strip():
                blocking.append(f"{vid}: missing {field}")
        if verdict == "bug" and SEVERITY_RANK[severity] > 0:
            blocking.append(
                f"{vid}: a `bug` verdict carries security severity {severity!r}"
            )
        if SEVERITY_RANK[severity] > SEVERITY_RANK["low"]:
            anchor = precedents.get(str(v.get("precedent") or ""))
            if anchor is None:
                blocking.append(
                    f"{vid}: severity {severity} is not anchored — name a precedent from the threat model, or cap at low"
                )
            elif (
                SEVERITY_RANK.get(str(anchor.get("severity")), -1)
                < SEVERITY_RANK[severity]
            ):
                blocking.append(
                    f"{vid}: severity {severity} exceeds its precedent {anchor['id']} ({anchor.get('severity')})"
                )
            elif verdict == "solid":
                review.append(
                    f"{vid}: solid at {severity} (anchored to {anchor['id']}) — a human confirms"
                )
        fix = v.get("incomplete_fix_of")
        if fix and str(fix) not in precedents:
            blocking.append(
                f"{vid}: incomplete_fix_of names {fix}, which is not a precedent in the threat model"
            )
        dup = v.get("duplicates")
        known = precedents.get(str(dup)) if dup else None
        status = (known.get("status") or known.get("state")) if known else None
        if dup and verdict == "solid" and status == "remediated":
            # Same outcome through a path the fix missed: news, not a duplicate.
            review.append(
                f"{vid}: names remediated {dup} as a duplicate — if the fix missed this path, "
                "record it as incomplete_fix_of instead"
            )
        elif dup and verdict == "solid":
            what = f"{dup} ({status})" if status else str(dup)
            blocking.append(
                f"{vid}: `solid` but duplicates {what} — the project already knows; it is not a new report"
            )

    if claims is not None:
        claim_ids = {str(c.get("id")) for c in claims}
        for missing in sorted(claim_ids - seen):
            blocking.append(f"{missing}: claim has no verdict")
        for extra in sorted(seen - claim_ids):
            blocking.append(f"{extra}: verdict for an id that is not a claim")

    by = {k: [v for v in verdicts if v.get("verdict") == k] for k in VERDICTS}
    if len(by["solid"]) > max_disclosure:
        if override:
            review.append(
                f"{len(by['solid'])} solid items exceed the disclosure cap of {max_disclosure}; override: {override}"
            )
        else:
            blocking.append(
                f"{len(by['solid'])} solid items exceed the disclosure cap of {max_disclosure}: lead with the strongest, "
                "or pass --override with a reason"
            )

    state = "BLOCKED" if blocking else ("NEEDS-SIGN-OFF" if review else "PASS")
    line = (
        f"SECURITY-GATE: SOLID={len(by['solid'])} ARGUABLE={len(by['arguable'])} BUG={len(by['bug'])} "
        f"THREAT-MODEL={tm_status} PRECEDENTS={len(precedents)} -- {state}"
    )
    return {
        "signoff_line": line,
        "state": state,
        "blocking": blocking,
        "needs_review": review,
        "disclosure": [
            {
                "id": v["id"],
                "severity": v.get("severity"),
                "precedent": v.get("precedent"),
            }
            for v in by["solid"]
        ],
        "titles_only": [v["id"] for v in by["arguable"]],
        "not_security": [v["id"] for v in by["bug"]],
        "duplicates": [
            {"id": v["id"], "duplicates": v["duplicates"]}
            for v in verdicts
            if v.get("duplicates")
        ],
        "incomplete_fixes": [
            {"id": v["id"], "incomplete_fix_of": v["incomplete_fix_of"]}
            for v in verdicts
            if v.get("incomplete_fix_of")
        ],
    }


def score(verdicts: list[dict], expected: dict[str, dict]) -> dict:
    """Agreement of *verdicts* with reference verdicts.

    *expected* maps claim id to ``{"verdict": [acceptable, ...], "max_severity":
    ..., "duplicates": ..., "precedent": ...}``; the first acceptable verdict is
    the reference. A verdict outside the acceptable list is a disagreement; a
    reference duplicate the verdict does not name is a missed duplicate.
    """
    by_id = {str(v.get("id")): v for v in verdicts}
    disagreements, missed_dups, too_severe, missing = [], [], [], []
    for cid, ref in sorted(expected.items()):
        v = by_id.get(cid)
        if v is None:
            missing.append(cid)
            continue
        acceptable = ref.get("verdict", [])
        if isinstance(acceptable, str):
            acceptable = [acceptable]
        if v.get("verdict") not in acceptable:
            disagreements.append(
                {"id": cid, "got": v.get("verdict"), "expected": acceptable}
            )
        if (
            ref.get("duplicates")
            and str(v.get("duplicates") or "") != ref["duplicates"]
        ):
            missed_dups.append(
                {"id": cid, "expected": ref["duplicates"], "got": v.get("duplicates")}
            )
        ceiling = ref.get("max_severity")
        sev = str(v.get("severity") or "n/a").lower()
        if ceiling and SEVERITY_RANK.get(sev, -1) > SEVERITY_RANK[ceiling]:
            too_severe.append({"id": cid, "got": sev, "ceiling": ceiling})
    judged = len(expected) - len(missing)
    agree = judged - len(disagreements)
    return {
        "agreement": f"{agree}/{len(expected)}",
        "agreement_rate": round(agree / len(expected), 3) if expected else None,
        "disagreements": disagreements,
        "missed_duplicates": missed_dups,
        "too_severe": too_severe,
        "missing": missing,
        "counts": {
            k: sum(1 for v in verdicts if v.get("verdict") == k) for k in VERDICTS
        },
    }


# --------------------------------------------------------------------------- lint

# An id-like token: `SF-1`, `R2-004`, `CRT-0001`, `PCC-3a`, `TI1`. Not inside a
# path or URL, and not the first group of a longer id (`CVE-2024-1234`).
ID_TOKEN_RE = re.compile(
    r"(?<![\w/.#-])([A-Z][A-Z0-9]{0,5}-\d{1,4}[a-z]?|[A-Z]{2,5}\d{1,2}[a-z]?)(?![\w-])"
)
# Prefixes of public identifiers and technical terms that look like ids.
_NOT_ID_PREFIXES = frozenset(
    {
        "AES", "ASCII", "CP", "CVE", "CWE", "EC", "ES", "GHSA", "HS", "HTTP",
        "IPV", "ISO", "LATIN", "MD", "OAUTH", "PEP", "PS", "PY", "RFC", "RS",
        "RSA", "SHA", "SSL", "TLS", "UTF", "WIN", "WCAG", "X", "BASE", "INT",
        "UINT", "ZIP",
    }
)  # fmt: skip
# Bit widths: `ZIP64`, `INT32`, `BASE64` are formats, not finding ids.
_BIT_WIDTHS = frozenset({"8", "16", "32", "64"})
# Fields a maintainer reads. Attribution belongs in `agents` / `source_refs`.
PROSE_FIELDS = ("title", "mechanism", "failure_scenario", "introduced_by")
_AGENTS_DIR = Path(__file__).resolve().parent.parent / "agents"


def id_tokens(text: str) -> list[str]:
    """Id-like tokens in *text*, minus standards and technical terms (`UTF-8`, `SHA1`)."""
    out = []
    for token in ID_TOKEN_RE.findall(text):
        prefix = token.split("-", 1)[0] if "-" in token else token
        digits = re.sub(r"^\D+|[a-z]$", "", prefix)
        prefix = re.sub(r"\d+[a-z]?$", "", prefix)
        if prefix in _NOT_ID_PREFIXES or ("-" not in token and digits in _BIT_WIDTHS):
            continue
        out.append(token)
    return out


def _strings(value) -> list[str]:
    """Every string inside a JSON value."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def _id_pattern(fid: str) -> re.Pattern:
    # A suffixed id (`X-001a`) is the same finding; `X-0010` is not.
    return re.compile(r"(?<![A-Za-z0-9_])" + re.escape(fid) + r"[a-z]?(?![A-Za-z0-9_])")


def lint(
    findings: list[dict],
    *,
    agent_names: list[str] | None = None,
    allow: set[str] | frozenset[str] = frozenset(),
) -> dict:
    """Publication hygiene of a consolidated findings.json.

    - ``agent-name``, ``merge-separator``, ``internal-id``: a prose field
      (``PROSE_FIELDS``) names a review agent, carries a ``||`` merge
      separator, or cites an id that is not a finding in this file (an agent's
      own finding id, an earlier run's label). Prose is for the maintainer;
      attribution goes in ``agents`` / ``source_refs``.
    - ``sensitive-ref``: a finding without ``publication_sensitive`` mentions,
      in any field, the id of one that has it. Publishing the first would
      reveal that the second exists.
    """
    if agent_names is None:
        agent_names = sorted(p.stem for p in _AGENTS_DIR.glob("*.md"))
    agent_res = [
        (
            name,
            re.compile(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", re.IGNORECASE),
        )
        for name in agent_names
    ]
    ids = {str(f.get("id")) for f in findings if f.get("id")}
    sensitive = {
        str(f["id"]): _id_pattern(str(f["id"]))
        for f in findings
        if f.get("id") and f.get("publication_sensitive") is True
    }
    problems: list[dict] = []

    def add(fid, field, kind, detail):
        problems.append({"id": fid, "field": field, "kind": kind, "detail": detail})

    for f in findings:
        fid = str(f.get("id", "?"))
        for field in PROSE_FIELDS:
            for text in _strings(f.get(field)):
                if "||" in text:
                    add(fid, field, "merge-separator", "`||` in maintainer-facing text")
                for name, rx in agent_res:
                    if rx.search(text):
                        add(fid, field, "agent-name", name)
                for token in id_tokens(text):
                    base = re.sub(r"(?<=\d)[a-z]$", "", token)
                    if token not in ids and base not in ids and token not in allow:
                        add(fid, field, "internal-id", token)
        if f.get("publication_sensitive") is True:
            continue
        for field, value in f.items():
            if field == "id":
                continue
            for text in _strings(value):
                for sid, rx in sensitive.items():
                    if rx.search(text):
                        add(fid, field, "sensitive-ref", sid)
    counts: dict[str, int] = {}
    for p in problems:
        counts[p["kind"]] = counts.get(p["kind"], 0) + 1
    return {
        "findings_total": len(findings),
        "sensitive_total": len(sensitive),
        "problems": problems,
        "counts": counts,
        "clean": not problems,
    }


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    exit_on_help(argv, __doc__)
    if not argv or argv[0] not in {"blind", "signoff", "score", "lint"}:
        emit(
            {
                "error": "first argument must be `blind`, `signoff`, `score` or `lint` (see --help)"
            }
        )
        sys.exit(2)
    cmd, rest = argv[0], argv[1:]
    opts: dict[str, str] = {}
    positional: list[str] = []
    flags = {"--all"}
    i = 0
    while i < len(rest):
        if rest[i] in flags:
            opts[rest[i]] = "1"
            i += 1
        elif rest[i].startswith("--") and i + 1 < len(rest):
            opts[rest[i]] = rest[i + 1]
            i += 2
        else:
            positional.append(rest[i])
            i += 1
    try:
        tm = _load(opts["--threat-model"]) if "--threat-model" in opts else None
        if cmd == "blind":
            if not positional:
                emit({"error": "blind needs a findings.json path"})
                sys.exit(2)
            findings = _findings(_load(positional[0]))
            emit(
                {
                    "claims": blind(
                        findings,
                        precedents=(tm or {}).get("precedents"),
                        take_all="--all" in opts,
                    ),
                    "findings_total": len(findings),
                }
            )
            return
        if cmd == "lint":
            if not positional:
                emit({"error": "lint needs a findings.json path"})
                sys.exit(2)
            allow = {t for t in opts.get("--allow", "").split(",") if t}
            result = lint(_findings(_load(positional[0])), allow=allow)
            emit(result)
            sys.exit(0 if result["clean"] else 1)
        if cmd == "score":
            if "--verdicts" not in opts or "--expected" not in opts:
                emit({"error": "score needs --verdicts and --expected"})
                sys.exit(2)
            verdicts = _load(opts["--verdicts"])
            if isinstance(verdicts, dict):
                verdicts = verdicts.get("verdicts", [])
            emit(score(verdicts, _load(opts["--expected"])))
            return
        if tm is None or "--verdicts" not in opts:
            emit({"error": "signoff needs --threat-model and --verdicts"})
            sys.exit(2)
        verdicts = _load(opts["--verdicts"])
        if isinstance(verdicts, dict):
            verdicts = verdicts.get("verdicts", [])
        claims = None
        if "--claims" in opts:
            data = _load(opts["--claims"])
            claims = data.get("claims", []) if isinstance(data, dict) else data
        max_disclosure = int(opts.get("--max-disclosure", DEFAULT_MAX_DISCLOSURE))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        emit({"error": str(exc)})
        sys.exit(2)
    result = signoff(
        tm,
        verdicts,
        claims=claims,
        max_disclosure=max_disclosure,
        override=opts.get("--override"),
    )
    emit(result)
    sys.exit(0 if result["state"] != "BLOCKED" else 1)


if __name__ == "__main__":
    main()
