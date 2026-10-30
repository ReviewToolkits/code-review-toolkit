"""Tests for run_lint_rules.py -- the tiered, pinned ruff selection."""

import unittest

from helpers import import_script

mod = import_script("run_lint_rules")


class TestTiers(unittest.TestCase):
    """The tier lists are data the toolkit is calibrated against."""

    def test_tier_1_is_a_subset_of_tier_2(self):
        self.assertTrue(set(mod._tier_codes(1)) < set(mod._tier_codes(2)))

    def test_no_code_is_in_both_tier_lists(self):
        self.assertEqual(set(mod.TIER_1) & set(mod.TIER_2), set())

    def test_no_tiered_code_is_also_preview_only(self):
        # A preview-gated code in a normal tier silently does nothing -- ruff
        # warns on stderr and returns no findings for it.
        both = (set(mod.TIER_1) | set(mod.TIER_2)) & set(mod.PREVIEW_ONLY)
        self.assertEqual(both, set())

    def test_tiers_have_no_duplicates(self):
        for name, codes in (("TIER_1", mod.TIER_1), ("TIER_2", mod.TIER_2)):
            with self.subTest(tier=name):
                self.assertEqual(len(codes), len(set(codes)))

    def test_unknown_tier_raises(self):
        with self.assertRaises(ValueError):
            mod._tier_codes(3)


class TestRuleToShape(unittest.TestCase):
    """The overlap map is what stops the same defect being reported twice."""

    def test_every_mapped_rule_is_actually_selected(self):
        selected = set(mod._tier_codes(2))
        for code in mod.RULE_TO_SHAPE:
            with self.subTest(code=code):
                self.assertIn(code, selected, "mapped but never run")

    def test_every_mapped_shape_exists_in_the_catalog(self):
        import json
        from pathlib import Path

        data = Path(mod.__file__).resolve().parent.parent / "data"
        catalog = json.loads(
            (data / "python_bug_shapes.json").read_text(encoding="utf-8")
        )
        known = {s["id"] for s in catalog["shapes"]}
        for code, shape in mod.RULE_TO_SHAPE.items():
            with self.subTest(code=code):
                self.assertIn(shape, known)

    def test_catalogued_zip_and_datetime_shapes_are_mapped(self):
        # Unmapped, these were harvested as novel shapes although the catalog
        # already had them (the B019 failure, repeated).
        expected = {
            "B905": "zip-truncates-on-length-mismatch",
            "DTZ005": "naive-datetime-in-a-persisted-artifact",
            "DTZ006": "naive-datetime-in-a-persisted-artifact",
        }
        for code, shape in expected.items():
            with self.subTest(code=code):
                self.assertEqual(mod.RULE_TO_SHAPE.get(code), shape)


class TestSuppressionDetection(unittest.TestCase):
    """`has_suppression_comment` is the measured dismissal signal."""

    def test_recognises_the_common_forms(self):
        for line in (
            "x = 1  # noqa: E501",
            "open = open  # pylint: disable=redefined-builtin",
            "y = z  # type: ignore[assignment]",
            "run(cmd)  # nosec",
            "if x:  # pragma: no cover",
        ):
            with self.subTest(line=line):
                self.assertTrue(mod._SUPPRESSION.search(line), line)

    def test_ordinary_comment_is_not_a_suppression(self):
        self.assertIsNone(mod._SUPPRESSION.search("x = 1  # this is fine"))


class TestNormalize(unittest.TestCase):
    def test_null_filename_and_location_are_tolerated(self):
        # ruff 0.16.0's JSON notes that filename and location "may now be null".
        from pathlib import Path

        out = mod.normalize(
            {"code": "B006", "filename": None, "location": None, "message": "m"},
            Path("/p"),
            tier=1,
            cache={},
        )
        self.assertEqual(out["file"], "")
        self.assertEqual(out["line"], 0)
        self.assertEqual(out["shape_id"], "mutable-default-argument")

    def test_unmapped_code_has_a_null_shape_id(self):
        from pathlib import Path

        out = mod.normalize(
            {"code": "S608", "filename": None, "location": None, "message": "m"},
            Path("/p"),
            tier=1,
            cache={},
        )
        self.assertIsNone(out["shape_id"])


class TestRuffInvocation(unittest.TestCase):
    """The ruff command line: what the reviewed project is exposed to."""

    def _cmd(self):
        from pathlib import Path
        from unittest.mock import MagicMock, patch

        with patch.object(mod.subprocess, "run") as run:
            run.return_value = MagicMock(stdout="[]", stderr="")
            mod._run_ruff("ruff", ["F821"], Path("/p"), preview=False)
        return run.call_args.args[0]

    def test_ruff_never_writes_a_cache_into_the_project(self):
        # Without --no-cache ruff leaves `.ruff_cache/` in the reviewed checkout.
        self.assertIn("--no-cache", self._cmd())

    def test_selection_is_still_isolated_and_explicit(self):
        cmd = self._cmd()
        self.assertIn("--isolated", cmd)
        self.assertIn("--select", cmd)
        self.assertNotIn("--extend-select", cmd)


def _fake_hit(root, relpath, line, needle, code="B008"):
    """A ruff JSON finding at the first *needle* on *line* (1-based columns)."""
    path = root / relpath
    text = path.read_text(encoding="utf-8").splitlines()[line - 1]
    return {
        "code": code,
        "filename": str(path),
        "location": {"row": line, "column": text.index(needle) + 1},
        "message": f"hit on {needle}",
    }


def _analyze_with_fake_ruff(root, hits):
    from unittest.mock import patch

    with (
        patch.object(mod, "ruff_version", return_value=mod.PINNED_RUFF_VERSION),
        patch.object(
            mod,
            "validate_rules",
            return_value={"ok": [], "unknown": [], "removed": [], "preview_gated": []},
        ),
        patch.object(mod, "_run_ruff", return_value=(hits, "", None)),
    ):
        return mod.analyze(str(root))


class TestFastapiMarkerDismissal(unittest.TestCase):
    """B008 on a FastAPI dependency marker is the framework's idiom (#26)."""

    ROUTES = (
        "from fastapi import Depends, Query as Q\n"
        "import fastapi\n"
        "\n"
        "def route(db=Depends(get_db), q: str = Q(None)):\n"
        "    pass\n"
        "\n"
        "def other(x=fastapi.Security(scheme)):\n"
        "    pass\n"
        "\n"
        "def café(n=Depends(get_n)):\n"
        "    pass\n"
        "\n"
        "def real(items=make_list()):\n"
        "    pass\n"
    )

    def test_markers_are_dismissed_with_a_reason_and_counted(self):
        from helpers import TempProject

        with TempProject({"routes.py": self.ROUTES}) as root:
            hits = [
                _fake_hit(root, "routes.py", 4, "Depends("),
                _fake_hit(root, "routes.py", 4, "Q("),
                _fake_hit(root, "routes.py", 7, "fastapi.Security("),
                # Non-ASCII before the call: ruff counts characters, ast bytes.
                _fake_hit(root, "routes.py", 10, "Depends("),
            ]
            result = _analyze_with_fake_ruff(root, hits)
        self.assertEqual(result["findings"], [])
        self.assertEqual(len(result["dismissed"]), 4)
        for hit in result["dismissed"]:
            self.assertEqual(hit["dismissal_reason"], "fastapi-dependency-marker")
        summary = result["summary"]
        self.assertEqual(summary["total"], 4)
        self.assertEqual(summary["reported"], 0)
        self.assertEqual(summary["dismissed"], 4)
        self.assertEqual(
            summary["dismissed_by_reason"], {"fastapi-dependency-marker": 4}
        )
        # A dismissed hit is not a shape overlap to merge.
        self.assertEqual(summary["overlaps_a_shape"], 0)
        self.assertIn("fastapi-dependency-marker", result["dismissal_reasons"])

    def test_a_real_call_default_in_a_fastapi_file_is_still_reported(self):
        from helpers import TempProject

        with TempProject({"routes.py": self.ROUTES}) as root:
            result = _analyze_with_fake_ruff(
                root, [_fake_hit(root, "routes.py", 13, "make_list(")]
            )
        self.assertEqual(len(result["findings"]), 1)
        self.assertEqual(result["dismissed"], [])

    def test_a_marker_name_not_imported_from_fastapi_is_still_reported(self):
        from helpers import TempProject

        source = (
            "from fastapi import APIRouter\n"
            "from mylib import Query\n"
            "\n"
            "class Depends:\n"
            "    pass\n"
            "\n"
            "def f(a=Depends(), b=Query()):\n"
            "    pass\n"
        )
        with TempProject({"m.py": source}) as root:
            result = _analyze_with_fake_ruff(
                root,
                [
                    _fake_hit(root, "m.py", 7, "Depends("),
                    _fake_hit(root, "m.py", 7, "Query("),
                ],
            )
        self.assertEqual(len(result["findings"]), 2)
        self.assertEqual(result["summary"]["dismissed"], 0)

    def test_other_codes_on_a_marker_line_are_not_dismissed(self):
        from helpers import TempProject

        with TempProject({"routes.py": self.ROUTES}) as root:
            result = _analyze_with_fake_ruff(
                root, [_fake_hit(root, "routes.py", 4, "Depends(", code="B006")]
            )
        self.assertEqual(len(result["findings"]), 1)


class TestMissingRuff(unittest.TestCase):
    def test_absent_binary_is_an_error_not_an_empty_result(self):
        result = mod.analyze(".", ruff_bin="definitely-not-a-real-ruff-binary")
        self.assertIn("error", result)
        self.assertEqual(result["findings"], [])


if __name__ == "__main__":
    unittest.main()
