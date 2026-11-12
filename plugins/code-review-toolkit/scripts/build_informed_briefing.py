#!/usr/bin/env python3
"""Assemble the informed-explore briefing from the project-independent catalogs.

Emits a **Markdown briefing** (to stdout) that every agent in the
``informed-explore`` command reads before its own analysis. It seeds each agent
with

  (a) the bug-SHAPE catalog (``data/python_bug_shapes.json``) as sibling-hunt
      templates, scoped to the shapes that agent owns,
  (b) the false-positive taxonomy (``data/python_non_bugs.md``) to suppress, and
  (c) the cross-cutting triage rules (guarded-twin, systemic-root,
      behavioural-divergence-outranks-stylistic, silent-beats-loud).

These are project-INDEPENDENT shapes, so one briefing applies to any Python
codebase. If the target carries a per-project findings memory at
``<target>/.code-review/findings.json`` (confirmed findings accumulated by prior
runs), those are folded in as "confirm, don't re-litigate" entries so a re-run of
the same project is genuinely informed rather than cold.

That findings file uses the same schema as the ``project-local/findings.json`` in
the ``*-review-findings`` companion repositories, so a project's accumulated
memory can be lifted into a findings repo without transformation -- and, via
``--catalog``, read straight back out of one.

Usage:
    python build_informed_briefing.py [path] [--agent NAME] [--catalog PATH]
                                      [--area PREFIX ...] [--budget BYTES]
                                      [--known-out PATH] [--max-files N]

``--catalog`` accepts any of three things, so it can point at whatever level of a
findings repo is convenient:

  * a ``findings.json`` file,
  * a project directory containing ``project-local/findings.json``, or
  * a findings-repo root containing ``*/project-local/findings.json``.

Entries whose project matches the target are folded in as "verify, then move on".
Entries from OTHER projects are folded in separately as cross-project evidence:
they are not claims about this codebase, they are shapes confirmed elsewhere that
are worth hunting here.

Narrowing (a large project memory makes every briefing 100 KB or more):

  * ``--agent`` keeps this project's established findings whose shape the agent
    owns -- by ``shape``, by ``family`` through the family-to-shape map, or by
    the agent being listed in ``agents`` -- plus the false-positive sections of
    the agent's topic; other sections are listed by entry title.
  * ``--area PREFIX`` (repeatable) keeps established findings located under
    the prefix, whatever their shape.
  * ``--budget BYTES`` caps the briefing (default 80000 with ``--agent``, none
    without; 0 disables). Over budget, it trims in this order: other projects'
    findings, implemented shapes to their triage lines, established findings,
    the agent's own false-positive sections to titles, every shape to its
    triage lines.
  * ``--known-out PATH`` writes every established finding, one tab-separated
    line each, for grepping; omitted entries are counted in the briefing and
    point there (or to the memory file).

A finding's ``family`` is the consolidation family it belongs to; the map from
families to catalog shapes comes from the memory's top-level ``families``
object and from findings that carry both. A legacy ``shape`` value that is not a
catalog shape id is read as a family. Id-like tokens in titles are resolved to
the finding they name (prefixed with its ``run``) or replaced, because an
earlier run's agent ids resolve to nothing for the reader.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scan_common import emit, parse_common_args  # noqa: E402
from security_gate import ID_TOKEN_RE, id_tokens  # noqa: E402

_DATA = Path(__file__).resolve().parent.parent / "data"

# Findings whose status means "already established -- confirm, don't re-derive".
_CONFIRMED_STATUS = {"confirmed", "reported", "fixed"}

# Where a reviewed project accumulates its own findings memory between runs.
_PROJECT_MEMORY = Path(".code-review") / "findings.json"

# A per-agent briefing above this many bytes has to be read in pages.
DEFAULT_AGENT_BUDGET = 80_000

_SEVERITY_ORDER = {"FIX": 0, "CONSIDER": 1, "POLICY": 2}

# False-positive taxonomy sections (by heading prefix) and the agents that read
# them in full. Every other agent gets the entry titles and the file path.
_FP_TOPIC_AGENTS = {
    "dead code": {"dead-code-finder"},
    "error handling": {
        "silent-failure-hunter",
        "python-pitfall-scanner",
        "lint-rule-triager",
    },
    "complexity": {"complexity-simplifier"},
    "types": {"type-design-analyzer", "typing-integrity-auditor"},
    "consistency": {"consistency-auditor", "pattern-consistency-checker"},
    "documentation": {"documentation-auditor", "project-docs-auditor"},
    "git history": {"git-history-analyzer", "git-history-context"},
    "learned from validation": {
        "python-pitfall-scanner",
        "lint-rule-triager",
        "concurrency-auditor",
    },
}

# For an agent that owns no shape: whose shapes are closest to its remit.
_ADJACENT_AGENTS = {
    "consistency-auditor": ["pattern-consistency-checker"],
    "dead-code-finder": ["tech-debt-inventory"],
    "test-coverage-analyzer": ["test-investigation-agent"],
    "git-history-context": ["git-history-analyzer"],
    "architecture-mapper": ["pattern-consistency-checker"],
}


def _load_shapes() -> list[dict]:
    """Load the bug-shape catalog; empty list if the data file is absent."""
    try:
        with (_DATA / "python_bug_shapes.json").open(encoding="utf-8") as handle:
            return json.load(handle).get("shapes", [])
    except (OSError, json.JSONDecodeError):
        return []


def _load_fp_taxonomy() -> str:
    """Load the false-positive taxonomy markdown; empty string if absent."""
    try:
        return (_DATA / "python_non_bugs.md").read_text(encoding="utf-8")
    except OSError:
        return ""


def _catalog_findings_files(catalog: Path) -> list[Path]:
    """Every findings.json reachable from *catalog*, whatever level it names."""
    if catalog.is_file():
        return [catalog]
    direct = catalog / "project-local" / "findings.json"
    if direct.is_file():
        return [direct]
    return sorted(catalog.glob("*/project-local/findings.json"))


def _tag_file_context(finding: dict, data: object, path: Path) -> dict:
    """Copy a memory file's run id, family map and path onto one of its findings."""
    finding = dict(finding)
    if isinstance(data, dict):
        if data.get("run") and not finding.get("run"):
            finding["run"] = data["run"]
        if isinstance(data.get("families"), dict):
            finding["_families"] = data["families"]
    finding["_source"] = str(path)
    return finding


def _load_catalog_findings(catalog: str, target: str) -> tuple[list[dict], list[str]]:
    """Load an external findings catalog, tagging each entry with its project.

    Returns ``(findings, notes)``; *notes* records what was read and what could
    not be, because a catalog path that silently resolves to nothing would make
    the briefing look complete while carrying none of the memory it promised.
    """
    root = Path(catalog).expanduser()
    if not root.exists():
        return [], [f"catalog path does not exist: {root}"]

    files = _catalog_findings_files(root)
    if not files:
        return [], [f"no findings.json found under {root}"]

    target_name = Path(target).expanduser().resolve().name
    findings: list[dict] = []
    notes: list[str] = []
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            notes.append(f"unreadable: {path} ({type(exc).__name__})")
            continue
        project = (
            data.get("project") if isinstance(data, dict) else None
        ) or path.parent.parent.name
        entries = data.get("findings", []) if isinstance(data, dict) else data
        entries = [f for f in entries if isinstance(f, dict)]
        for finding in entries:
            finding = _tag_file_context(finding, data, path)
            finding["_project"] = project
            # A catalog project matches the target when either name contains the
            # other: `coveragepy` vs `coverage`, `cpython-idlelib` vs `idlelib`.
            finding["_is_target"] = (
                target_name in project or project in target_name
            ) and bool(target_name)
            findings.append(finding)
        notes.append(f"{project}: {len(entries)} finding(s) from {path}")
    return findings, notes


def _load_project_findings(target: str) -> list[dict]:
    """Load a target project's accumulated findings memory, if any."""
    path = Path(target).resolve()
    if path.is_file():
        path = path.parent
    memory = path / _PROJECT_MEMORY
    try:
        with memory.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(data, list):
        findings = data
    elif isinstance(data, dict):
        findings = data.get("findings", [])
    else:
        findings = []
    return [_tag_file_context(f, data, memory) for f in findings if isinstance(f, dict)]


def _shapes_by_agent(shapes: list[dict]) -> dict[str, list[dict]]:
    """Group shapes under the agent that owns them."""
    grouped: dict[str, list[dict]] = {}
    for shape in shapes:
        grouped.setdefault(shape.get("agent", "unassigned"), []).append(shape)
    return grouped


# How each `detectability` value changes what the reading agent has to do.
_DETECTABILITY_NOTE = {
    "implemented": "a scanner check emits candidates for this — triage them, don't re-derive",
    "implementable": "AST-decidable but NOT yet implemented — hunt it by hand this run",
    "agent-only": "no scanner will ever hand you this — the sibling hunt below IS the method",
}


def _render_shape(shape: dict, compact: bool = False) -> str:
    """Render one bug shape as a briefing entry.

    *compact* keeps what triage needs (title, how it is found, when not to
    flag) and drops the hunt prose; the budget uses it.
    """
    detectability = shape.get("detectability")
    lines = [
        f"#### `{shape.get('id', '?')}` — {shape.get('title', '')}",
        "",
        f"- **Default severity:** {shape.get('severity', 'CONSIDER')} "
        f"(before triage) · **grounding:** {shape.get('validation', 'unknown')}",
    ]
    if detectability:
        note = _DETECTABILITY_NOTE.get(detectability, "")
        lines.append(
            f"- **How you find it:** {detectability}" + (f" — {note}" if note else "")
        )
    if compact:
        if shape.get("differential"):
            lines.append(f"- **Do NOT flag when:** {shape['differential']}")
        lines.append(
            "- _Shortened for the size budget; the full template is in "
            f"`{_DATA / 'python_bug_shapes.json'}`._"
        )
        return "\n".join(lines)
    lines += [
        f"- **Pattern:** {shape.get('pattern', '')}",
        f"- **Guarded twin (the fix):** {shape.get('guarded_twin', '')}",
        f"- **Sibling hunt:** {shape.get('hunt', '')}",
        f"- **Expected behaviour:** {shape.get('expected', '')}",
        f"- **Surfaces as:** {shape.get('caught_as', '')}",
    ]
    if shape.get("differential"):
        lines.append(f"- **Do NOT flag when:** {shape['differential']}")
    confirmed = shape.get("confirmed_examples") or []
    if confirmed:
        lines.append("- **Confirmed instances:** " + "; ".join(confirmed))
    return "\n".join(lines)


TRIAGE_RULES = """\
1. **Guarded twin.** Most real defects have a sibling in the same codebase that
   already does it right. Find that twin — it is both proof the shape is a bug
   *in this project's own judgement* and the exact fix to propose. A shape with
   no twin anywhere may be a deliberate project-wide convention.
2. **Systemic root over instance count.** Ten instances of one shape is one
   finding with ten sites, not ten findings. Report the root and enumerate the
   sites; that is what a maintainer can act on in a single change.
3. **Silent beats loud.** A defect that raises is already visible to its
   authors; a defect that silently produces wrong results is not. When ranking,
   weight the silent shapes above the noisy ones even when severity ties.
4. **Behavioural divergence outranks stylistic.** Two modules formatting
   differently is noise. Two modules *handling the same error case differently*
   is a finding — one of them is wrong.
5. **Confirm, don't re-litigate.** Anything listed below as already-confirmed is
   settled. Verify it still exists and move on; spend the run on un-found
   siblings, not on re-deriving known results.
6. **Cite or drop.** Every finding needs `file:line` and a concrete failure
   scenario (inputs → wrong outcome). A finding you cannot make concrete is a
   hypothesis; label it as one or drop it.
7. **NEVER edit the reviewed tree.** Not to patch-test, not to mutation-test,
   not "just to check". Work on a copy:

       mkdir -p /tmp/repro && git -C <project> archive HEAD | tar -x -C /tmp/repro

   A review often runs many agents over one checkout at once. A file you
   modified is a file every other agent then reads wrongly, and one of them
   will report a confident, false finding about code that was never there.
   Restoring it afterwards does not undo that — the window is what does the
   damage. If you did edit the tree, say so plainly in your report; it will be
   verified independently either way.
"""


# --------------------------------------------------------------------------- narrowing


def _as_list(value) -> list:
    if value is None or value == "":
        return []
    return list(value) if isinstance(value, (list, tuple, set)) else [value]


def _locations(finding: dict) -> list[str]:
    locs = [
        *_as_list(finding.get("location")),
        *_as_list(finding.get("locations")),
        *_as_list(finding.get("file")),
    ]
    return [str(loc).removeprefix("./") for loc in locs if loc]


class _Scope:
    """Which established findings a per-agent / per-area briefing keeps."""

    def __init__(
        self,
        shapes: list[dict],
        findings: list[dict],
        agent: str | None,
        areas: list[str] | None,
    ):
        self.agent = agent
        self.areas = [a.removeprefix("./") for a in areas or [] if a]
        self.shape_ids = {s["id"] for s in shapes if s.get("id")}
        self.alias = {
            alias: s["id"] for s in shapes for alias in s.get("aliases", []) or []
        }
        self.owned = {s["id"] for s in shapes if s.get("agent") == agent}
        self.family_map = self._family_map(findings)

    @property
    def active(self) -> bool:
        return bool(self.agent or self.areas)

    def _split(self, finding: dict) -> tuple[set[str], set[str]]:
        """(catalog shapes, families) a finding names directly."""
        shapes, families = set(), set()
        for value in [
            *_as_list(finding.get("shape")),
            *_as_list(finding.get("shapes")),
        ]:
            value = str(value)
            if value in self.shape_ids:
                shapes.add(value)
            elif value in self.alias:
                shapes.add(self.alias[value])
            else:  # legacy: a consolidation family recorded as the shape
                families.add(value)
        families |= {str(v) for v in _as_list(finding.get("family"))}
        return shapes, families

    def _family_map(self, findings: list[dict]) -> dict[str, set[str]]:
        """Family -> catalog shapes, from explicit maps and from findings carrying both."""
        mapping: dict[str, set[str]] = {}
        for finding in findings:
            for family, shapes in (finding.get("_families") or {}).items():
                mapping.setdefault(str(family), set()).update(
                    self.alias.get(str(s), str(s)) for s in _as_list(shapes)
                )
            shapes, families = self._split(finding)
            for family in families:
                if shapes:
                    mapping.setdefault(family, set()).update(shapes)
        return mapping

    def shapes_of(self, finding: dict) -> set[str]:
        shapes, families = self._split(finding)
        for family in families:
            shapes |= self.family_map.get(family, set())
        return shapes

    def keeps(self, finding: dict) -> bool:
        if not self.active:
            return True
        if self.agent and (
            self.shapes_of(finding) & self.owned
            or self.agent in _as_list(finding.get("agents"))
        ):
            return True
        return any(
            loc.startswith(area) for loc in _locations(finding) for area in self.areas
        )


# --------------------------------------------------------------------------- references


class _Labels:
    """Run-prefixed labels for every loaded finding, and id-token resolution."""

    def __init__(self, findings: list[dict]):
        self.by_id: dict[str, str] = {}
        for finding in findings:
            fid = str(finding.get("id") or "")
            if fid:
                self.by_id.setdefault(fid, self.label(finding))
        self.unresolved: set[str] = set()

    @staticmethod
    def label(finding: dict) -> str:
        fid = str(finding.get("id") or "?")
        run = str(finding.get("run") or "")
        return f"{run}/{fid}" if run and not fid.startswith(run) else fid

    def clean(self, text: str, own_id: str = "") -> str:
        """Resolve id tokens to labels; replace the ones nothing resolves."""

        def sub(match: re.Match) -> str:
            token = match.group(1)
            if not id_tokens(token) or token == own_id:
                return token
            base = re.sub(r"(?<=\d)[a-z]$", "", token)
            if token in self.by_id or base in self.by_id:
                return self.by_id.get(token) or self.by_id[base]
            self.unresolved.add(token)
            return "[earlier-run ref]"

        return ID_TOKEN_RE.sub(sub, text)


# --------------------------------------------------------------------------- sections


def _established_line(
    finding: dict, labels: _Labels, with_project: bool = False
) -> str:
    loc = (_locations(finding) or ["?"])[0]
    prefix = f"[{finding['_project']}] " if with_project else ""
    refs = ""
    shape = finding.get("shape")
    if shape:
        refs += f" · shape `{', '.join(map(str, _as_list(shape)))}`"
    if finding.get("family"):
        refs += f" · family `{', '.join(map(str, _as_list(finding['family'])))}`"
    title = labels.clean(str(finding.get("title", "")), str(finding.get("id") or ""))
    return (
        f"- {prefix}**{labels.label(finding)}** [{finding.get('severity', '?')}] "
        f"{title} — `{loc}`{refs}"
    )


def _by_severity(findings: list[dict]) -> list[dict]:
    return sorted(
        findings, key=lambda f: _SEVERITY_ORDER.get(str(f.get("severity")), 9)
    )


def _omission_note(count: int, why: str, pointer: str) -> list[str]:
    if not count:
        return []
    return [f"_{count} further established finding(s) omitted ({why}). {pointer}_", ""]


def _render_catalog_section(
    catalog_findings: list[dict],
    owned_shapes: set[str] | None = None,
    *,
    scope: _Scope | None = None,
    labels: _Labels | None = None,
    mine_limit: int | None = None,
    theirs_limit: int | None = None,
    pointer: str = "",
) -> list[str]:
    """Render an external findings catalog, split by whether it is this project.

    When *owned_shapes* is given (a per-agent run), the OTHER-projects list is
    narrowed to shapes this agent owns -- that list is speculative "worth hunting
    here" material, so an entry the agent could not act on is pure noise. The
    THIS-project list is narrowed by *scope* (agent shapes and families, areas):
    every entry dropped from it is counted and points at the full list, so the
    do-not-re-derive set stays one grep away.
    """
    if not catalog_findings:
        return []
    labels = labels or _Labels(catalog_findings)

    mine = [f for f in catalog_findings if f.get("_is_target")]
    theirs = [f for f in catalog_findings if not f.get("_is_target")]
    dropped = 0
    if owned_shapes is not None:
        resolve = scope.shapes_of if scope else (lambda f: {f.get("shape")})
        kept = [f for f in theirs if resolve(f) & owned_shapes]
        dropped = len(theirs) - len(kept)
        theirs = kept
    narrowed = 0
    if scope is not None and scope.active:
        kept = [f for f in mine if scope.keeps(f)]
        narrowed = len(mine) - len(kept)
        mine = kept
    mine = _by_severity(mine)
    trimmed = 0
    if mine_limit is not None and len(mine) > mine_limit:
        trimmed = len(mine) - mine_limit
        mine = mine[:mine_limit]
    budget_dropped = 0
    if theirs_limit is not None and len(theirs) > theirs_limit:
        budget_dropped = len(theirs) - theirs_limit
        theirs = theirs[:theirs_limit]
    parts: list[str] = []

    if mine or narrowed or trimmed:
        total = len(mine) + narrowed + trimmed
        parts += [
            (
                "## Already recorded for THIS project in the findings catalog "
                f"({len(mine)} of {total})"
            ),
            "",
            "These are settled. Verify each still exists, then move on — do not "
            "re-derive them, and do not report them as new.",
            "",
        ]
        if mine:
            parts += [*[_established_line(f, labels) for f in mine], ""]
        parts += _omission_note(narrowed, "outside your shapes and areas", pointer)
        parts += _omission_note(trimmed, "size budget", pointer)
    if theirs:
        shapes_seen = sorted({str(f["shape"]) for f in theirs if f.get("shape")})
        parts += [
            f"## Confirmed in OTHER projects ({len(theirs)}) — hunt them here",
            "",
            "These are **not** claims about this codebase. Each is a shape that "
            "was confirmed somewhere else, which makes it worth a targeted look "
            "here. A hit is a new finding for this project; a miss is not a "
            "finding at all, so do not report absence.",
            "",
            *[_established_line(f, labels, with_project=True) for f in theirs],
            "",
        ]
        if shapes_seen:
            parts += [
                "Shapes represented above, in catalog terms: "
                + ", ".join(f"`{s}`" for s in shapes_seen),
                "",
            ]
    # Report the denominator: a narrowed list must not read as the whole catalog.
    if dropped:
        parts += [
            f"_{dropped} further cross-project finding(s) were omitted here "
            f"because they belong to shapes another agent owns._",
            "",
        ]
    if budget_dropped:
        parts += [
            (
                f"_{budget_dropped} further cross-project finding(s) were omitted "
                "for the size budget._"
            ),
            "",
        ]
    return parts


def _fp_sections(fp_taxonomy: str) -> list[tuple[str, str]]:
    """``(heading, block)`` per ``##`` section of the taxonomy body."""
    body = (
        fp_taxonomy.split("---", 1)[-1] if "---" in fp_taxonomy else fp_taxonomy
    ).strip()
    chunks = re.split(r"(?m)^(?=## )", body)
    out = []
    for chunk in chunks:
        if not chunk.strip():
            continue
        first = chunk.splitlines()[0]
        heading = first[3:].strip() if first.startswith("## ") else ""
        block = re.sub(r"(?:\n\s*---\s*)+$", "", chunk.strip())
        out.append((heading, block))
    return out


def _fp_reads_in_full(heading: str, agent: str) -> bool:
    lowered = heading.lower()
    return any(
        lowered.startswith(topic) and agent in agents
        for topic, agents in _FP_TOPIC_AGENTS.items()
    )


def _fp_index(block: str) -> str:
    """A section reduced to its heading and one line per entry title."""
    lines = block.splitlines()
    titles = [f"- {line[4:].strip()}" for line in lines if line.startswith("### ")]
    return "\n".join([lines[0], "", *titles, ""]) if titles else lines[0] + "\n"


def _render_fp(fp_taxonomy: str, agent: str | None, own_full: bool = True) -> list[str]:
    if not fp_taxonomy:
        return []
    parts = [
        "## Known false positives — suppress these",
        "",
        "If a candidate matches one of these classes, dismiss it *with the "
        "stated reason* rather than reporting it. Each entry also states what "
        "the REAL bug looks like, so a genuine instance is never suppressed.",
        "",
    ]
    if not agent:
        body = (
            fp_taxonomy.split("---", 1)[-1] if "---" in fp_taxonomy else fp_taxonomy
        ).strip()
        return [*parts, body, ""]
    sections = _fp_sections(fp_taxonomy)
    full = [b for h, b in sections if own_full and _fp_reads_in_full(h, agent)]
    index = [
        _fp_index(b)
        for h, b in sections
        if not (own_full and _fp_reads_in_full(h, agent))
    ]
    parts += [part for block in full for part in (block, "")]
    if index:
        parts += [
            "",
            (
                "The sections below are listed by entry title only. Before you "
                "report or dismiss a candidate that matches one of these titles, "
                f"read the entry in `{_DATA / 'python_non_bugs.md'}`."
            ),
            "",
            *index,
        ]
    return [*parts, ""]


def _render_shapes(
    grouped: dict[str, list[dict]],
    agent: str | None,
    all_shapes: list[dict],
    compact: int,
) -> list[str]:
    """Shape templates; *compact* 1 shortens implemented shapes, 2 every shape."""
    total = sum(len(v) for v in grouped.values())
    parts = [
        "## Bug-shape templates" + (f" for `{agent}`" if agent else "") + f" ({total})",
        "",
        "Each shape is a reusable *pattern*, not a location. For each one, hunt "
        "this codebase for instances, then follow the sibling-hunt directive to "
        "find the ones a cold pass would miss.",
        "",
    ]
    if not total:
        parts.append("_No shapes catalogued for this agent yet._\n")
        if agent:
            parts += _zero_shape_fallback(agent, all_shapes)
    for owner, owned in sorted(grouped.items()):
        if not owned:
            continue
        if not agent:
            parts.append(f"### Owned by `{owner}`")
            parts.append("")
        for shape in owned:
            short = compact >= 2 or (
                compact == 1 and shape.get("detectability") == "implemented"
            )
            parts.append(_render_shape(shape, compact=short))
            parts.append("")
    return parts


def _zero_shape_fallback(agent: str, shapes: list[dict]) -> list[str]:
    """What an agent that owns no shape does instead of hunting its own."""
    adjacent = _ADJACENT_AGENTS.get(agent, [])
    near = [s for s in shapes if s.get("agent") in adjacent]
    parts = [
        (
            "You own no catalogued shape, so steps 1–3 of the informed instructions "
            "(hunt your shapes, follow their sibling hunts, find their guarded "
            "twins) do not apply. Go straight to suppressing known false "
            "positives, confirming established findings, and your own open-ended "
            "analysis."
        ),
        "",
    ]
    if near:
        parts += [
            "Shapes owned by adjacent agents ("
            + ", ".join(f"`{a}`" for a in adjacent)
            + "): hunt any that falls inside your remit, and report a hit under "
            "that shape's id.",
            "",
            *[f"- `{s['id']}` — {s.get('title', '')}" for s in near],
            "",
        ]
    return parts


def _render_memory(
    project_findings: list[dict],
    scope: _Scope,
    labels: _Labels,
    limit: int | None,
    pointer: str,
) -> list[str]:
    if not project_findings:
        return []
    confirmed = [
        f
        for f in project_findings
        if str(f.get("status", "")).lower() in _CONFIRMED_STATUS
    ]
    kept = [f for f in confirmed if scope.keeps(f)]
    narrowed = len(confirmed) - len(kept)
    kept = _by_severity(kept)
    trimmed = 0
    if limit is not None and len(kept) > limit:
        trimmed = len(kept) - limit
        kept = kept[:limit]
    parts = [
        "## Already established in this project "
        f"({len(confirmed)} confirmed of {len(project_findings)} recorded)",
        "",
        "From prior runs' accumulated memory (`.code-review/findings.json`). "
        "Verify these still exist, then move on — do not re-derive them.",
        "",
    ]
    if kept:
        parts += [*[_established_line(f, labels) for f in kept], ""]
    parts += _omission_note(narrowed, "outside your shapes and areas", pointer)
    parts += _omission_note(trimmed, "size budget", pointer)
    return parts


# --------------------------------------------------------------------------- assembly


def _known_lines(findings: list[dict], labels: _Labels) -> list[str]:
    """One tab-separated, greppable line per established finding."""
    lines = []
    for f in _by_severity(findings):
        tags = [
            *map(str, _as_list(f.get("shape"))),
            *map(str, _as_list(f.get("family"))),
        ]
        own = str(f.get("id") or "")
        cells = [
            labels.label(f),
            str(f.get("severity", "?")),
            ",".join(_locations(f)) or "?",
            ",".join(tags),
            labels.clean(str(f.get("title", "")), own),
            labels.clean(" ".join(str(f.get("evidence", "")).split()), own),
        ]
        lines.append("\t".join(c.replace("\t", " ") for c in cells))
    return lines


def established_findings(
    project_findings: list[dict], catalog_findings: list[dict]
) -> list[dict]:
    """The do-not-re-derive set: confirmed memory entries and this project's catalog."""
    return [
        f
        for f in project_findings
        if str(f.get("status", "")).lower() in _CONFIRMED_STATUS
    ] + [f for f in catalog_findings if f.get("_is_target")]


def _pointer(known_out: str | None, sources: list[str]) -> str:
    if known_out:
        return f"Grep the full list before reporting: `grep -i <file-or-symbol> {known_out}`."
    if sources:
        return "The full list is in " + ", ".join(f"`{s}`" for s in sources) + "."
    return ""


def _largest_fitting(n: int, size_of) -> int:
    """Largest k in [0, n] with size_of(k) within budget (size_of is monotone)."""
    lo, hi = 0, n
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if size_of(mid):
            lo = mid
        else:
            hi = mid - 1
    return lo


def build_briefing(
    shapes: list[dict],
    fp_taxonomy: str,
    project_findings: list[dict],
    agent: str | None = None,
    catalog_findings: list[dict] | None = None,
    *,
    areas: list[str] | None = None,
    budget: int | None = None,
    known_out: str | None = None,
    report: dict | None = None,
) -> str:
    """Assemble the full Markdown briefing.

    *budget* (bytes; ``None`` = the default for the mode, 0 = none) trims the
    briefing as the module docstring describes. *report*, when given, receives
    what was narrowed, trimmed and left unresolved.
    """
    catalog_findings = catalog_findings or []
    grouped = _shapes_by_agent(shapes)
    if agent:
        grouped = {agent: grouped.get(agent, [])}
    if budget is None:
        budget = DEFAULT_AGENT_BUDGET if agent else 0
    everything = project_findings + catalog_findings
    scope = _Scope(shapes, everything, agent, areas)
    owned_shapes = (
        {s["id"] for owned in grouped.values() for s in owned} if agent else None
    )
    sources = sorted(
        {
            f["_source"]
            for f in established_findings(project_findings, catalog_findings)
            if f.get("_source")
        }
    )
    pointer = _pointer(known_out, sources)

    def assemble(
        theirs_limit=None, mine_limit=None, compact=0, own_fp=True
    ) -> tuple[str, _Labels]:
        labels = _Labels(everything)
        parts: list[str] = [
            "# Informed-review briefing",
            "",
            "Read this before your own analysis. It exists so you **confirm without "
            "re-litigating**, **suppress known false positives**, and spend the run "
            "hunting un-found siblings of established shapes rather than "
            "rediscovering the basics from scratch.",
            "",
            "## Cross-cutting triage rules",
            "",
            TRIAGE_RULES,
        ]
        parts += _render_shapes(grouped, agent, shapes, compact)
        parts += _render_memory(project_findings, scope, labels, mine_limit, pointer)
        parts += _render_catalog_section(
            catalog_findings,
            owned_shapes,
            scope=scope,
            labels=labels,
            mine_limit=mine_limit,
            theirs_limit=theirs_limit,
            pointer=pointer,
        )
        parts += _render_fp(fp_taxonomy, agent, own_full=own_fp)
        return "\n".join(parts) + "\n", labels

    def size(text: str) -> int:
        return len(text.encode("utf-8"))

    trimmed: list[str] = []
    state: dict = {}
    text, labels = assemble()
    if budget and size(text) > budget:
        n_theirs = sum(
            1
            for f in catalog_findings
            if not f.get("_is_target")
            and (owned_shapes is None or scope.shapes_of(f) & owned_shapes)
        )
        confirmed = [
            f
            for f in project_findings
            if str(f.get("status", "")).lower() in _CONFIRMED_STATUS
        ]
        n_mine = max(
            sum(1 for f in confirmed if scope.keeps(f)),
            sum(1 for f in catalog_findings if f.get("_is_target") and scope.keeps(f)),
        )
        steps = [
            ("cross-project findings", "theirs_limit", n_theirs),
            ("implemented shapes shortened", "compact", 1),
            ("established findings", "mine_limit", n_mine),
            ("own false-positive sections listed by title", "own_fp", False),
            ("every shape shortened", "compact", 2),
        ]
        for name, key, value in steps:
            if size(text) <= budget:
                break
            if key.endswith("_limit"):
                if not value:
                    continue
                keep = _largest_fitting(
                    value,
                    lambda k, key=key: size(assemble(**{**state, key: k})[0]) <= budget,
                )
                state[key] = keep
                trimmed.append(f"{name}: kept {keep} of {value}")
            else:
                state[key] = value
                trimmed.append(name)
            text, labels = assemble(**state)
        if size(text) > budget:
            trimmed.append("still over budget")
        text = text.rstrip("\n") + (
            f"\n\n_Briefing trimmed to a {budget}-byte budget: "
            + "; ".join(trimmed)
            + "._\n"
        )
    if report is not None:
        report.update(
            {
                "bytes": size(text),
                "budget": budget,
                "trimmed": trimmed,
                "unresolved_refs": sorted(labels.unresolved),
                "established_total": len(
                    established_findings(project_findings, catalog_findings)
                ),
            }
        )
    return text


def analyze(
    target: str,
    *,
    max_files: int = 0,
    agent: str | None = None,
    catalog: str | None = None,
    areas: list[str] | None = None,
    budget: int | None = None,
    known_out: str | None = None,
) -> dict:
    """Build the briefing for *target*. ``max_files`` is accepted for interface
    parity with the other scripts; this script does not walk the file tree."""
    shapes = _load_shapes()
    fp_taxonomy = _load_fp_taxonomy()
    project_findings = _load_project_findings(target)
    catalog_findings, catalog_notes = (
        _load_catalog_findings(catalog, target) if catalog else ([], [])
    )
    report: dict = {}
    briefing = build_briefing(
        shapes,
        fp_taxonomy,
        project_findings,
        agent,
        catalog_findings,
        areas=areas,
        budget=budget,
        known_out=known_out,
        report=report,
    )
    if known_out:
        established = established_findings(project_findings, catalog_findings)
        lines = _known_lines(established, _Labels(project_findings + catalog_findings))
        Path(known_out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "target": target,
        "shapes_total": len(shapes),
        "shapes_included": (
            len(_shapes_by_agent(shapes).get(agent, [])) if agent else len(shapes)
        ),
        "project_findings": len(project_findings),
        "catalog": catalog,
        "catalog_findings": len(catalog_findings),
        "catalog_findings_this_project": sum(
            1 for f in catalog_findings if f.get("_is_target")
        ),
        "catalog_notes": catalog_notes,
        "has_fp_taxonomy": bool(fp_taxonomy),
        "known_out": known_out,
        **report,
        "briefing_markdown": briefing,
    }


def _extract_flags(argv: list[str]) -> tuple[list[str], str | None, str | None]:
    """Pull ``--agent NAME`` and ``--catalog PATH`` out of argv."""
    rest: list[str] = []
    agent: str | None = None
    catalog: str | None = None
    i = 0
    while i < len(argv):
        if argv[i] == "--agent" and i + 1 < len(argv):
            agent = argv[i + 1]
            i += 2
        elif argv[i] in ("--catalog", "--catalog-dir") and i + 1 < len(argv):
            catalog = argv[i + 1]
            i += 2
        else:
            rest.append(argv[i])
            i += 1
    return rest, agent, catalog


def _extract_narrowing_flags(
    argv: list[str],
) -> tuple[list[str], list[str], int | None, str | None]:
    """Pull ``--area PREFIX`` (repeatable), ``--budget BYTES`` and ``--known-out PATH``."""
    rest: list[str] = []
    areas: list[str] = []
    budget: int | None = None
    known_out: str | None = None
    i = 0
    while i < len(argv):
        flag = argv[i]
        if flag in ("--area", "--budget", "--known-out") and i + 1 < len(argv):
            value = argv[i + 1]
            if flag == "--area":
                areas.append(value)
            elif flag == "--budget":
                budget = int(value)
            else:
                known_out = value
            i += 2
        else:
            rest.append(flag)
            i += 1
    return rest, areas, budget, known_out


def main() -> None:
    try:
        argv, areas, budget, known_out = _extract_narrowing_flags(sys.argv[1:])
        argv, agent, catalog = _extract_flags(argv)
        target, max_files = parse_common_args(argv)
        result = analyze(
            target,
            max_files=max_files,
            agent=agent,
            catalog=catalog,
            areas=areas,
            budget=budget,
            known_out=known_out,
        )
        # Markdown, not JSON: the output IS the agent briefing. Catalog problems
        # go to stderr so a path that resolved to nothing is visible without
        # corrupting the briefing on stdout.
        for note in result["catalog_notes"]:
            print(f"catalog: {note}", file=sys.stderr)
        if result.get("trimmed"):
            print(
                f"briefing: {result['bytes']} bytes, trimmed to the "
                f"{result['budget']}-byte budget: " + "; ".join(result["trimmed"]),
                file=sys.stderr,
            )
        if result.get("unresolved_refs"):
            print(
                "briefing: replaced id(s) that resolve to no loaded finding: "
                + ", ".join(result["unresolved_refs"]),
                file=sys.stderr,
            )
        sys.stdout.write(result["briefing_markdown"])
    except Exception as exc:  # noqa: BLE001 -- top-level guard
        emit({"error": str(exc), "type": type(exc).__name__})
        sys.exit(1)


if __name__ == "__main__":
    main()
