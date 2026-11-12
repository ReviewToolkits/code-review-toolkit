"""Tests for build_informed_briefing.py -- the informed-explore briefing."""

import json
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

from helpers import TempProject, import_script

mod = import_script("build_informed_briefing")


class TestCatalogLoading(unittest.TestCase):
    """The shipped data files load and are well-formed."""

    def test_shapes_load(self):
        shapes = mod._load_shapes()
        self.assertGreater(len(shapes), 0)

    def test_every_shape_has_required_fields(self):
        required = {
            "id",
            "title",
            "agent",
            "severity",
            "pattern",
            "guarded_twin",
            "hunt",
            "expected",
            "caught_as",
            "validation",
        }
        for shape in mod._load_shapes():
            self.assertTrue(
                required <= set(shape),
                f"{shape.get('id')} missing {required - set(shape)}",
            )

    def test_shape_ids_are_unique(self):
        ids = [s["id"] for s in mod._load_shapes()]
        self.assertEqual(len(ids), len(set(ids)))

    def test_severities_are_valid(self):
        for shape in mod._load_shapes():
            self.assertIn(
                shape["severity"], {"FIX", "CONSIDER", "POLICY", "ACCEPTABLE"}
            )

    def test_fp_taxonomy_loads(self):
        self.assertIn("false-positive", mod._load_fp_taxonomy().lower())


class TestGrouping(unittest.TestCase):
    """Shapes group under their owning agent."""

    def test_groups_by_agent(self):
        grouped = mod._shapes_by_agent(
            [
                {"id": "a", "agent": "x"},
                {"id": "b", "agent": "x"},
                {"id": "c", "agent": "y"},
            ]
        )
        self.assertEqual(len(grouped["x"]), 2)
        self.assertEqual(len(grouped["y"]), 1)

    def test_missing_agent_becomes_unassigned(self):
        grouped = mod._shapes_by_agent([{"id": "a"}])
        self.assertIn("unassigned", grouped)


class TestBuildBriefing(unittest.TestCase):
    """The briefing includes rules, shapes, and the FP taxonomy."""

    def _shape(self, **over):
        base = {
            "id": "demo-shape",
            "title": "Demo",
            "agent": "silent-failure-hunter",
            "severity": "FIX",
            "pattern": "P",
            "guarded_twin": "T",
            "hunt": "H",
            "expected": "E",
            "caught_as": "C",
            "validation": "documented",
        }
        base.update(over)
        return base

    def test_includes_triage_rules(self):
        out = mod.build_briefing([], "", [])
        self.assertIn("Guarded twin", out)
        self.assertIn("Cite or drop", out)

    def test_includes_shape_fields(self):
        out = mod.build_briefing([self._shape()], "", [])
        for token in (
            "demo-shape",
            "Guarded twin (the fix)",
            "Sibling hunt",
            "Surfaces as",
        ):
            self.assertIn(token, out)

    def test_agent_filter_scopes_shapes(self):
        # The excluded shape's id must not be a substring of the static triage
        # rules: "other" used to be, via rule 7's "every other agent", so this
        # test failed on prose rather than on the filter it exists to check.
        shapes = [
            self._shape(),
            self._shape(id="unrelated-shape", agent="dead-code-finder"),
        ]
        out = mod.build_briefing(shapes, "", [], agent="silent-failure-hunter")
        self.assertIn("demo-shape", out)
        self.assertNotIn("unrelated-shape", out)

    def test_unknown_agent_yields_empty_notice(self):
        out = mod.build_briefing([self._shape()], "", [], agent="nope")
        self.assertIn("No shapes catalogued", out)

    def test_differential_rendered_as_do_not_flag(self):
        out = mod.build_briefing(
            [self._shape(differential="only when mutated")], "", []
        )
        self.assertIn("Do NOT flag when", out)

    def test_confirmed_examples_rendered(self):
        out = mod.build_briefing(
            [self._shape(confirmed_examples=["PROJ-1 (a.py:1)"])], "", []
        )
        self.assertIn("PROJ-1", out)

    def test_fp_taxonomy_appended(self):
        out = mod.build_briefing([], "# T\nintro\n---\n### 1. Thing\nbody", [])
        self.assertIn("Known false positives", out)
        self.assertIn("Thing", out)

    def test_no_findings_section_without_memory(self):
        self.assertNotIn("Already established", mod.build_briefing([], "", []))


class TestProjectMemory(unittest.TestCase):
    """Prior-run findings are folded in as confirm-don't-relitigate entries."""

    MEMORY = {
        "findings": [
            {
                "id": "CRT-0001",
                "severity": "FIX",
                "title": "Mutable default",
                "location": "a.py:1",
                "status": "confirmed",
            },
            {
                "id": "CRT-0002",
                "severity": "FIX",
                "title": "Unproven idea",
                "location": "b.py:2",
                "status": "candidate",
            },
        ]
    }

    def test_loads_memory_file(self):
        with TempProject(
            {".code-review/findings.json": json.dumps(self.MEMORY)}
        ) as root:
            self.assertEqual(len(mod._load_project_findings(str(root))), 2)

    def test_only_confirmed_are_listed(self):
        out = mod.build_briefing([], "", self.MEMORY["findings"])
        self.assertIn("CRT-0001", out)
        self.assertNotIn("CRT-0002", out)
        self.assertIn("1 confirmed of 2 recorded", out)

    def test_missing_memory_is_empty(self):
        with TempProject({"mod.py": ""}) as root:
            self.assertEqual(mod._load_project_findings(str(root)), [])

    def test_malformed_memory_is_empty_not_fatal(self):
        with TempProject({".code-review/findings.json": "{not json"}) as root:
            self.assertEqual(mod._load_project_findings(str(root)), [])

    def test_bare_list_schema_accepted(self):
        payload = json.dumps([{"id": "X", "status": "confirmed", "title": "t"}])
        with TempProject({".code-review/findings.json": payload}) as root:
            self.assertEqual(len(mod._load_project_findings(str(root))), 1)

    def test_file_target_resolves_to_its_directory(self):
        with TempProject(
            {"pkg.py": "x=1", ".code-review/findings.json": json.dumps(self.MEMORY)}
        ) as root:
            found = mod._load_project_findings(str(root / "pkg.py"))
            self.assertEqual(len(found), 2)


class TestAnalyze(unittest.TestCase):
    """The analyze() entry point reports what went into the briefing."""

    def test_returns_counts_and_markdown(self):
        with TempProject({"m.py": ""}) as root:
            result = mod.analyze(str(root))
        self.assertGreater(result["shapes_total"], 0)
        self.assertTrue(result["has_fp_taxonomy"])
        self.assertIn("Informed-review briefing", result["briefing_markdown"])

    def test_agent_filter_reduces_included_count(self):
        with TempProject({"m.py": ""}) as root:
            everything = mod.analyze(str(root))
            scoped = mod.analyze(str(root), agent="silent-failure-hunter")
        self.assertLessEqual(scoped["shapes_included"], everything["shapes_total"])


class TestArgExtraction(unittest.TestCase):
    """--agent and --catalog are stripped before the shared arg parser sees argv."""

    def test_extracts_agent(self):
        rest, agent, catalog = mod._extract_flags(
            ["path", "--agent", "x", "--max-files", "5"]
        )
        self.assertEqual(agent, "x")
        self.assertIsNone(catalog)
        self.assertEqual(rest, ["path", "--max-files", "5"])

    def test_absent_flags_are_none(self):
        rest, agent, catalog = mod._extract_flags(["path"])
        self.assertIsNone(agent)
        self.assertIsNone(catalog)
        self.assertEqual(rest, ["path"])

    def test_extracts_catalog(self):
        rest, agent, catalog = mod._extract_flags(["p", "--catalog", "/some/where"])
        self.assertEqual(catalog, "/some/where")
        self.assertEqual(rest, ["p"])

    def test_catalog_dir_is_accepted_as_a_synonym(self):
        # The plan named the flag --catalog-dir; informed-explore.md documents
        # --catalog. Both work so neither document is wrong.
        _, _, catalog = mod._extract_flags(["p", "--catalog-dir", "/some/where"])
        self.assertEqual(catalog, "/some/where")


class TestExternalCatalog(unittest.TestCase):
    """--catalog folds an external findings repo into the briefing."""

    def _repo(self, root: Path) -> None:
        for project, fid, title in (
            ("coveragepy", "CRF-COVPY-0001", "A coverage finding"),
            ("cpython-idlelib", "CRF-IDLELIB-0001", "An idlelib finding"),
        ):
            path = root / project / "project-local"
            path.mkdir(parents=True)
            (path / "findings.json").write_text(
                json.dumps(
                    {
                        "project": project,
                        "findings": [
                            {
                                "id": fid,
                                "title": title,
                                "severity": "FIX",
                                "status": "reproduced",
                                "location": "mod.py:1",
                                "shape": "mutable-default-argument",
                            }
                        ],
                    }
                )
            )

    def test_repo_root_finds_every_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._repo(root)
            findings, notes = mod._load_catalog_findings(str(root), "/x/coveragepy")
            self.assertEqual(len(findings), 2)
            self.assertEqual(len(notes), 2)

    def test_target_project_is_distinguished_from_the_others(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._repo(root)
            findings, _ = mod._load_catalog_findings(str(root), "/src/coverage")
            mine = [f for f in findings if f["_is_target"]]
            self.assertEqual([f["_project"] for f in mine], ["coveragepy"])

    def test_a_single_project_directory_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._repo(root)
            findings, notes = mod._load_catalog_findings(
                str(root / "coveragepy"), "/x/coveragepy"
            )
            self.assertEqual(len(findings), 1)

    def test_a_missing_catalog_is_reported_not_silently_empty(self):
        findings, notes = mod._load_catalog_findings("/no/such/path", "/x/y")
        self.assertEqual(findings, [])
        self.assertTrue(any("does not exist" in n for n in notes))

    def test_a_directory_with_no_findings_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            findings, notes = mod._load_catalog_findings(tmp, "/x/y")
            self.assertEqual(findings, [])
            self.assertTrue(any("no findings.json" in n for n in notes))

    def test_other_projects_are_narrowed_to_the_agent_shapes(self):
        theirs = [
            {"_is_target": False, "_project": "p", "id": "A", "shape": "mine"},
            {"_is_target": False, "_project": "p", "id": "B", "shape": "someone-elses"},
        ]
        rendered = "\n".join(mod._render_catalog_section(theirs, {"mine"}))
        self.assertIn("**A**", rendered)
        self.assertNotIn("**B**", rendered)
        # The dropped count must be stated, not silently omitted.
        self.assertIn("1 further cross-project finding(s) were omitted", rendered)

    def test_target_findings_without_a_scope_are_kept(self):
        mine = [{"_is_target": True, "_project": "p", "id": "A", "shape": "not-owned"}]
        rendered = "\n".join(mod._render_catalog_section(mine, {"something-else"}))
        self.assertIn("**A**", rendered)


def _shape(sid, agent, detectability="agent-only", **over):
    shape = {
        "id": sid,
        "title": f"Title of {sid}",
        "agent": agent,
        "severity": "FIX",
        "pattern": "PATTERN-" + "p" * 400,
        "guarded_twin": "TWIN",
        "hunt": "HUNT-" + "h" * 400,
        "expected": "E",
        "caught_as": "C",
        "validation": "documented",
        "detectability": detectability,
    }
    shape.update(over)
    return shape


def _mine(fid, **over):
    finding = {
        "_is_target": True,
        "_project": "proj",
        "_source": "/memory/findings.json",
        "id": fid,
        "severity": "CONSIDER",
        "title": f"Finding {fid}",
        "location": "pkg/other.py:1",
        "status": "confirmed",
    }
    finding.update(over)
    return finding


class TestNarrowing(unittest.TestCase):
    """Per-agent and per-area briefings carry only the relevant established findings."""

    SHAPES: ClassVar[list] = [
        _shape("owned-shape", "silent-failure-hunter"),
        _shape("their-shape", "dead-code-finder"),
    ]

    def _brief(self, catalog, **kw):
        kw.setdefault("budget", 0)
        return mod.build_briefing(self.SHAPES, "", [], catalog_findings=catalog, **kw)

    def test_this_project_list_is_narrowed_to_the_agents_shapes(self):
        catalog = [_mine("P-1", shape="owned-shape"), _mine("P-2", shape="their-shape")]
        out = self._brief(catalog, agent="silent-failure-hunter")
        self.assertIn("**P-1**", out)
        self.assertNotIn("**P-2**", out)
        self.assertIn("(1 of 2)", out)
        # The omission is counted and points at the full list.
        self.assertIn("1 further established finding(s) omitted", out)
        self.assertIn("/memory/findings.json", out)

    def test_area_prefix_keeps_findings_under_it(self):
        catalog = [
            _mine("P-1", location="pkg/billing/charge.py:10"),
            _mine("P-2", locations=["./pkg/billing/refund.py:3"]),
            _mine("P-3", location="pkg/auth/login.py:5"),
        ]
        out = self._brief(catalog, areas=["pkg/billing/"])
        self.assertIn("**P-1**", out)
        self.assertIn("**P-2**", out)
        self.assertNotIn("**P-3**", out)

    def test_agent_listed_in_agents_is_kept(self):
        catalog = [_mine("P-1", agents=["silent-failure-hunter"])]
        out = self._brief(catalog, agent="silent-failure-hunter")
        self.assertIn("**P-1**", out)

    def test_no_agent_and_no_area_keeps_everything(self):
        catalog = [_mine("P-1", shape="their-shape"), _mine("P-2")]
        out = self._brief(catalog)
        self.assertIn("**P-1**", out)
        self.assertIn("**P-2**", out)

    def test_project_memory_is_narrowed_too(self):
        memory = [_mine("M-1", shape="owned-shape"), _mine("M-2", shape="their-shape")]
        out = mod.build_briefing(
            self.SHAPES, "", memory, agent="silent-failure-hunter", budget=0
        )
        self.assertIn("**M-1**", out)
        self.assertNotIn("**M-2**", out)
        self.assertIn("2 confirmed of 2 recorded", out)

    def test_fixes_are_listed_first(self):
        catalog = [_mine("P-1"), _mine("P-2", severity="FIX")]
        out = self._brief(catalog)
        self.assertLess(out.index("**P-2**"), out.index("**P-1**"))


class TestFamilies(unittest.TestCase):
    """A finding's consolidation family maps to catalog shapes."""

    SHAPES: ClassVar[list] = [
        _shape("owned-shape", "silent-failure-hunter", aliases=["old-name"]),
        _shape("their-shape", "dead-code-finder"),
    ]

    def _kept(self, catalog):
        out = mod.build_briefing(
            self.SHAPES, "", [], "silent-failure-hunter", catalog, budget=0
        )
        return sorted(f["id"] for f in catalog if f"**{f['id']}**" in out)

    def test_explicit_family_map(self):
        families = {"retry-lost": ["owned-shape"]}
        catalog = [
            _mine("P-1", family="retry-lost", _families=families),
            _mine("P-2", family="docs-drift", _families=families),
        ]
        self.assertEqual(self._kept(catalog), ["P-1"])

    def test_map_inferred_from_findings_carrying_both(self):
        catalog = [
            _mine("P-1", family="retry-lost", shape="owned-shape"),
            _mine("P-2", family="retry-lost"),
            _mine("P-3", family="docs-drift"),
        ]
        self.assertEqual(self._kept(catalog), ["P-1", "P-2"])

    def test_legacy_family_in_the_shape_field(self):
        # Older memories put the family in `shape`; it is read as a family.
        catalog = [
            _mine("P-1", shape="retry-lost", _families={"retry-lost": ["old-name"]}),
            _mine("P-2", shape="docs-drift"),
        ]
        self.assertEqual(self._kept(catalog), ["P-1"])

    def test_family_rendered(self):
        out = mod.build_briefing(
            self.SHAPES, "", [], catalog_findings=[_mine("P-1", family="retry-lost")]
        )
        self.assertIn("family `retry-lost`", out)


class TestBudget(unittest.TestCase):
    """A size budget trims in a fixed order and says what it trimmed."""

    def test_established_findings_are_trimmed_with_a_count(self):
        catalog = [_mine(f"P-{i}", title="x" * 200) for i in range(200)]
        out = mod.build_briefing([], "", [], catalog_findings=catalog, budget=12_000)
        self.assertLessEqual(len(out.encode()), 12_000)
        self.assertIn("omitted (size budget)", out)
        self.assertIn("Briefing trimmed to a 12000-byte budget", out)

    def test_implemented_shapes_are_shortened_before_findings_are_dropped(self):
        shapes = [
            _shape(f"s{i}", "python-pitfall-scanner", "implemented") for i in range(6)
        ] + [_shape("by-hand", "python-pitfall-scanner", "implementable")]
        catalog = [_mine("P-1", agents=["python-pitfall-scanner"])]
        report: dict = {}
        out = mod.build_briefing(
            shapes,
            "",
            [],
            "python-pitfall-scanner",
            catalog,
            budget=8_000,
            report=report,
        )
        self.assertEqual(report["trimmed"], ["implemented shapes shortened"])
        self.assertIn("**P-1**", out)
        # The shape to hunt by hand keeps its full template.
        self.assertEqual(out.count("HUNT-"), 1)

    def test_agent_mode_has_a_default_budget(self):
        catalog = [_mine(f"P-{i}", agents=["x"], title="y" * 500) for i in range(400)]
        out = mod.build_briefing([], "", [], "x", catalog)
        self.assertLessEqual(len(out.encode()), mod.DEFAULT_AGENT_BUDGET)

    def test_zero_disables_the_budget(self):
        catalog = [_mine(f"P-{i}", agents=["x"], title="y" * 500) for i in range(400)]
        out = mod.build_briefing([], "", [], "x", catalog, budget=0)
        self.assertGreater(len(out.encode()), mod.DEFAULT_AGENT_BUDGET)


class TestFalsePositiveNarrowing(unittest.TestCase):
    TAXONOMY = (
        "# T\nintro\n\n---\n\n## Dead code / unused symbols\n\n### 1. Dynamic name\n"
        "- body one\n\n---\n\n## Error handling\n\n### 5. Narrow suppression\n"
        "- body five\n"
    )

    def test_agent_reads_its_topic_in_full_and_titles_of_the_rest(self):
        out = mod.build_briefing([], self.TAXONOMY, [], "dead-code-finder")
        self.assertIn("- body one", out)
        self.assertNotIn("- body five", out)
        self.assertIn("- 5. Narrow suppression", out)
        self.assertIn("python_non_bugs.md", out)

    def test_full_briefing_keeps_the_whole_taxonomy(self):
        out = mod.build_briefing([], self.TAXONOMY, [])
        self.assertIn("- body one", out)
        self.assertIn("- body five", out)


class TestZeroShapeAgent(unittest.TestCase):
    def test_fallback_names_adjacent_shapes(self):
        shapes = [_shape("pattern-shape", "pattern-consistency-checker")]
        out = mod.build_briefing(shapes, "", [], "consistency-auditor")
        self.assertIn("steps 1–3 of the informed instructions", out)
        self.assertIn("`pattern-shape`", out)
        self.assertNotIn("PATTERN-", out)  # titles only, not the templates


class TestReferences(unittest.TestCase):
    """Cross-run labels are run-prefixed; ids nothing resolves are replaced."""

    def test_run_prefix_and_resolution(self):
        catalog = [
            _mine("CRT-0001", run="v2", title="Same root as CRT-0002 and SF-7"),
            _mine("CRT-0002", run="v2", title="Retry dropped over UTF-8 input"),
        ]
        report: dict = {}
        out = mod.build_briefing(
            [], "", [], catalog_findings=catalog, budget=0, report=report
        )
        self.assertIn("**v2/CRT-0001**", out)
        self.assertIn("Same root as v2/CRT-0002 and [earlier-run ref]", out)
        self.assertIn("over UTF-8 input", out)
        self.assertEqual(report["unresolved_refs"], ["SF-7"])

    def test_file_level_run_is_applied(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "findings.json"
            path.write_text(
                json.dumps(
                    {"project": "proj", "run": "r3", "findings": [{"id": "A-1"}]}
                )
            )
            findings, _ = mod._load_catalog_findings(str(path), "/x/proj")
        self.assertEqual(findings[0]["run"], "r3")


class TestKnownOut(unittest.TestCase):
    def test_writes_every_established_finding(self):
        memory = {
            "project": "proj",
            "findings": [
                {
                    "id": f"M-{i}",
                    "status": "confirmed",
                    "title": f"t{i}",
                    "location": f"pkg/m{i}.py:1",
                    "evidence": "multi\nline",
                }
                for i in range(3)
            ]
            + [{"id": "M-9", "status": "candidate", "title": "unproven"}],
        }
        with (
            TempProject({".code-review/findings.json": json.dumps(memory)}) as root,
            tempfile.TemporaryDirectory() as tmp,
        ):
            known = Path(tmp) / "known.tsv"
            result = mod.analyze(
                str(root), agent="dead-code-finder", known_out=str(known)
            )
            lines = known.read_text().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0].split("\t")[:3], ["M-0", "?", "pkg/m0.py:1"])
        self.assertTrue(lines[0].endswith("multi line"))
        self.assertIn(f"grep -i <file-or-symbol> {known}", result["briefing_markdown"])


class TestNarrowingFlags(unittest.TestCase):
    def test_extracts_area_budget_and_known_out(self):
        rest, areas, budget, known = mod._extract_narrowing_flags(
            [
                "p",
                "--area",
                "a/",
                "--agent",
                "x",
                "--area",
                "b/",
                "--budget",
                "500",
                "--known-out",
                "/k.tsv",
            ]
        )
        self.assertEqual(rest, ["p", "--agent", "x"])
        self.assertEqual(areas, ["a/", "b/"])
        self.assertEqual(budget, 500)
        self.assertEqual(known, "/k.tsv")


if __name__ == "__main__":
    unittest.main()
