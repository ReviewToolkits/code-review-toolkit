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
            {"code": "B905", "filename": None, "location": None, "message": "m"},
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


class TestMissingRuff(unittest.TestCase):
    def test_absent_binary_is_an_error_not_an_empty_result(self):
        result = mod.analyze(".", ruff_bin="definitely-not-a-real-ruff-binary")
        self.assertIn("error", result)
        self.assertEqual(result["findings"], [])


if __name__ == "__main__":
    unittest.main()
