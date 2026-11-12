"""Tests for leak_scan.py: word-bounded terms, internal ids, sensitive refs."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import ClassVar

from helpers import import_script

ls = import_script("leak_scan")


def _run(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        try:
            ls.main(argv)
            code = 0
        except SystemExit as exc:
            code = exc.code
    return json.loads(buf.getvalue()), code


class TestTermRegex(unittest.TestCase):
    def test_path_term_does_not_match_inside_a_longer_word(self):
        rx = ls.term_regex("reports/")
        self.assertIsNone(rx.search("see bugreports/2024 for details"))
        self.assertIsNotNone(rx.search("written to reports/run_v2/x.md"))
        self.assertIsNotNone(rx.search("(reports/x)"))

    def test_name_term_is_word_bounded_and_case_insensitive(self):
        rx = ls.term_regex("acmecorp")
        self.assertIsNotNone(rx.search("The AcmeCorp service"))
        self.assertIsNotNone(rx.search("acmecorp/views.py"))
        self.assertIsNone(rx.search("acmecorporation"))

    def test_term_starting_with_a_slash_needs_no_left_boundary(self):
        rx = ls.term_regex("/home/")
        self.assertIsNotNone(rx.search("x=/home/someone/venv"))

    def test_case_sensitive(self):
        rx = ls.term_regex("Acme", case_sensitive=True)
        self.assertIsNone(rx.search("acme"))


class TestIdRegex(unittest.TestCase):
    def setUp(self):
        self.rx = ls.id_regex(["AB"])

    def _found(self, text):
        return [m.group(0) for m, _ in ls.iter_ids(text, self.rx, ["AB"])]

    def test_internal_id_is_not_matched_inside_a_public_id(self):
        self.assertEqual(self._found("see XY-AB-0012 and XYAB-3"), [])

    def test_plain_and_suffixed_ids(self):
        self.assertEqual(
            self._found("AB-012, AB-012a and (AB-7)."), ["AB-012", "AB-012", "AB-7"]
        )

    def test_identifier_spelling_in_test_names(self):
        self.assertEqual(
            self._found("def test_ab_037_case(): ab_030b = 1"), ["ab_037", "ab_030"]
        )

    def test_identifier_spelling_needs_a_boundary(self):
        self.assertEqual(self._found("tab_1 grab_2"), [])

    def test_no_prefixes_gives_no_regex(self):
        self.assertIsNone(ls.id_regex([]))


class TestRewriteIds(unittest.TestCase):
    IDMAP: ClassVar[dict[str, str]] = {"AB-012": "PUB-0003", "AB-030": "PUB-0007"}

    def test_rewrites_every_spelling_and_keeps_suffixes(self):
        text = "AB-012 and AB-030a; def test_ab_012(): pass; AB_030b; XY-AB-0012"
        out, unmapped = ls.rewrite_ids(text, self.IDMAP)
        self.assertEqual(
            out,
            "PUB-0003 and PUB-0007a; def test_pub_0003(): pass; PUB_0007b; XY-AB-0012",
        )
        self.assertEqual(unmapped, [])

    def test_numbers_compare_as_integers(self):
        out, _ = ls.rewrite_ids("AB-12", self.IDMAP)
        self.assertEqual(out, "PUB-0003")

    def test_public_ids_already_in_the_text_are_left_alone(self):
        # The internal prefix AB is part of the public prefix XY-AB.
        out, unmapped = ls.rewrite_ids(
            "AB-012 vs XY-AB-0003 and test_xy_ab_0003", {"AB-012": "XY-AB-0003"}
        )
        self.assertEqual(out, "XY-AB-0003 vs XY-AB-0003 and test_xy_ab_0003")
        self.assertEqual(unmapped, [])

    def test_unmapped_ids_are_reported_and_left(self):
        out, unmapped = ls.rewrite_ids("see AB-099 and ab_099", self.IDMAP)
        self.assertEqual(out, "see AB-099 and ab_099")
        self.assertEqual(unmapped, ["AB-099", "ab_099"])


class TestScan(unittest.TestCase):
    def test_scan_paths_reports_contents_and_file_names(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "sub").mkdir()
            (root / "sub" / "AB-004.py").write_text("print('ok')\n")
            (root / "notes.md").write_text("line one\nsee reports/x and bugreports/y\n")
            (root / "blob.bin").write_bytes(b"\0\1reports/")
            result, code = _run(
                ["scan", str(root), "--term", "reports/", "--id-prefix", "AB"]
            )
        self.assertEqual(code, 1)
        hits = {(h["file"], h["where"], h["kind"], h["line"]) for h in result["hits"]}
        self.assertEqual(
            hits,
            {
                ("notes.md", "content", "term", 2),
                ("sub/AB-004.py", "path", "internal-id", 1),
            },
        )
        self.assertEqual(result["files_binary"], ["blob.bin"])

    def test_public_prefix_hides_ids_inside_public_spellings(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "t.py").write_text("def test_xy_ab_0012(): pass  # XY-AB-0012\n")
            result, code = _run(["scan", d, "--id-prefix", "AB"])
            self.assertEqual(code, 1)  # without it, ab_0012 looks internal
            result, code = _run(
                ["scan", d, "--id-prefix", "AB", "--public-prefix", "XY-AB"]
            )
        self.assertEqual(code, 0, result["hits"])

    def test_clean_tree_exits_zero(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.md").write_text("PUB-0001 is public\n")
            result, code = _run(["scan", d, "--id-prefix", "AB", "--term", "acmecorp"])
        self.assertEqual(code, 0)
        self.assertTrue(result["clean"])

    def test_allow_list_drops_a_hit(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.md").write_text("see /tmp/ here\n")
            result, code = _run(
                ["scan", d, "--pattern", r"/tmp/\S*", "--allow", "/tmp/"]
            )
        self.assertEqual(code, 0, result)

    def test_terms_file(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "terms.txt").write_text("# private names\n\nacmecorp\n")
            Path(d, "out").mkdir()
            Path(d, "out", "a.md").write_text("AcmeCorp\n")
            result, code = _run(
                ["scan", str(Path(d, "out")), "--terms-file", str(Path(d, "terms.txt"))]
            )
        self.assertEqual(code, 1)
        self.assertEqual(result["hits"][0]["match"], "AcmeCorp")

    def test_nothing_to_look_for_is_an_error_not_a_clean_scan(self):
        with tempfile.TemporaryDirectory() as d:
            result, code = _run(["scan", d])
        self.assertEqual(code, 2)
        self.assertIn("error", result)

    def test_unknown_subcommand(self):
        _result, code = _run(["frobnicate"])
        self.assertEqual(code, 2)


class TestSensitiveRefs(unittest.TestCase):
    FINDINGS: ClassVar[list[dict]] = [
        {"id": "AB-001", "title": "a", "publication_sensitive": True},
        {"id": "AB-002", "title": "b", "notes": "same root cause as AB-1"},
        {
            "id": "AB-003",
            "title": "c",
            "related": ["AB-002"],
            "mechanism": "unlike XY-AB-0001",
        },
        {"id": "AB-004", "title": "d", "repro": ["artifacts/test_ab_001_variant.py"]},
        {
            "id": "AB-005",
            "title": "e",
            "publication_sensitive": True,
            "notes": "see AB-001",
        },
    ]

    def test_flags_mentions_in_any_field_and_spelling(self):
        result = ls.sensitive_refs(self.FINDINGS)
        got = {(v["id"], v["field"]) for v in result["violations"]}
        self.assertEqual(got, {("AB-002", "notes"), ("AB-004", "repro[0]")})
        self.assertEqual(result["withheld"], 2)
        self.assertFalse(result["clean"])

    def test_withheld_findings_may_mention_each_other(self):
        result = ls.sensitive_refs([self.FINDINGS[0], self.FINDINGS[4]])
        self.assertTrue(result["clean"])

    def test_ids_of_another_form_match_literally(self):
        findings = [
            {"id": "secret-one", "publication_sensitive": True},
            {"id": "x", "notes": "see secret-one"},
            {"id": "y", "notes": "see secret-ones"},
        ]
        result = ls.sensitive_refs(findings)
        self.assertEqual([v["id"] for v in result["violations"]], ["x"])

    def test_cli(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "findings.json")
            p.write_text(json.dumps({"findings": self.FINDINGS}))
            result, code = _run(["sensitive", str(p)])
        self.assertEqual(code, 1)
        self.assertEqual(len(result["violations"]), 2)


if __name__ == "__main__":
    unittest.main()
