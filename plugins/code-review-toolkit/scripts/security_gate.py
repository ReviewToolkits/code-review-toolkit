#!/usr/bin/env python3
"""The security-claims gate: blind the claims, then check the skeptic's verdicts and sign off.

Two subcommands, one on each side of the `security-claims-skeptic` agent:

``blind`` -- select the findings that make a security claim and strip what
would anchor the skeptic on the finder's opinion: severity, classification,
``kind``, the impact prose and notes. What is left is the mechanism, the
locations, the evidence, and the claim itself (``security_claim: {attacker,
preconditions, gain}``), which the skeptic must check rather than accept. With
``--threat-model``, each claim also gets the project's precedents whose wording
overlaps it most (``precedent_hints``): the cheapest way to catch a claim that
repeats an audit finding the project already published or accepted.

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

Usage:
    python security_gate.py blind FINDINGS.json [--threat-model TM.json] [--all]
    python security_gate.py score --verdicts V.json --expected EXPECTED.json
    python security_gate.py signoff --threat-model TM.json --verdicts V.json
        [--claims CLAIMS.json] [--max-disclosure N] [--override REASON]

``blind`` selects findings with a non-empty ``security_claim``, or the legacy
``security: true`` / ``security_vulnerability: true`` / ``kind`` of
vulnerability or hardening; ``--all`` takes every finding.
"""

from __future__ import annotations

import json
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


def precedent_hints(
    finding: dict, precedents: list[dict], limit: int = 3
) -> list[dict]:
    """Precedents whose title shares the most words with the claim (Jaccard-like)."""
    text = " ".join(
        str(finding.get(k) or "") for k in ("title", "mechanism", "failure_scenario")
    )
    words = _words(text)
    scored = []
    for p in precedents:
        pw = _words(p.get("title", ""))
        if not pw or not words:
            continue
        shared = pw & words
        score = len(shared) / len(pw)
        if len(shared) >= 2 and score >= 0.33:  # two of a six-word title
            scored.append((score, p, sorted(shared)))
    scored.sort(key=lambda t: -t[0])
    return [
        {
            "id": p.get("id"),
            "title": p.get("title"),
            "severity": p.get("severity"),
            "status": p.get("status") or p.get("state"),
            "shared_words": shared,
        }
        for _, p, shared in scored[:limit]
    ]


def blind(
    findings: list[dict],
    *,
    precedents: list[dict] | None = None,
    take_all: bool = False,
) -> list[dict]:
    out = []
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
            record["precedent_hints"] = precedent_hints(f, precedents)
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


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    exit_on_help(argv, __doc__)
    if not argv or argv[0] not in {"blind", "signoff", "score"}:
        emit(
            {
                "error": "first argument must be `blind`, `signoff` or `score` (see --help)"
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
