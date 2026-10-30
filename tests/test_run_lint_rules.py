"""Tests for run_lint_rules.py -- the tiered, pinned ruff selection."""

import unittest
from pathlib import Path

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


class TestSuppressionDetails(unittest.TestCase):
    """A suppression is a dismissal signal only where it was calibrated."""

    def test_a_security_rule_under_nosec_is_not_a_dismissal_signal(self):
        out = mod.suppression_details(
            "S307", "result = eval(code, ctx)  # nosec B307 - sandboxed"
        )
        self.assertEqual(out["suppression_comment"], "# nosec B307 - sandboxed")
        # B307 is bandit's name for S307.
        self.assertTrue(out["suppression_names_rule"])
        self.assertFalse(out["suppression_is_dismissal_signal"])

    def test_a_security_rule_under_noqa_is_not_a_dismissal_signal(self):
        out = mod.suppression_details("S608", "q = f'...{t}'  # noqa: S608")
        self.assertTrue(out["suppression_names_rule"])
        self.assertFalse(out["suppression_is_dismissal_signal"])

    def test_a_suppression_naming_another_rule_is_flagged(self):
        out = mod.suppression_details("S307", "x = eval(s)  # nosec B301")
        self.assertFalse(out["suppression_names_rule"])
        out = mod.suppression_details("E722", "except:  # noqa: E501")
        self.assertFalse(out["suppression_names_rule"])
        self.assertFalse(out["suppression_is_dismissal_signal"])

    def test_a_non_security_suppression_stays_a_signal(self):
        out = mod.suppression_details(
            "PLW0127", "open = open  # pylint: disable=redefined-builtin"
        )
        self.assertIsNone(out["suppression_names_rule"])
        self.assertTrue(out["suppression_is_dismissal_signal"])
        out = mod.suppression_details("E722", "except:  # noqa: E722")
        self.assertTrue(out["suppression_is_dismissal_signal"])

    def test_no_comment(self):
        out = mod.suppression_details("S307", "x = eval(s)")
        self.assertIsNone(out["suppression_comment"])
        self.assertFalse(out["suppression_is_dismissal_signal"])

    def test_summary_counts_the_claims_to_verify(self):
        from helpers import TempProject

        source = "x = eval(s)  # nosec B307\ntry:\n    f()\nexcept:  # noqa: E722\n    pass\n"
        with TempProject({"m.py": source}) as root:
            hits = [
                _fake_hit(root, "m.py", 1, "eval", code="S307"),
                _fake_hit(root, "m.py", 4, "except", code="E722"),
            ]
            result = _analyze_with_fake_ruff(root, hits)
        self.assertEqual(result["summary"]["has_suppression_comment"], 2)
        self.assertEqual(result["summary"]["suppression_needs_verification"], 1)


class TestNormalize(unittest.TestCase):
    def test_null_filename_and_location_are_tolerated(self):
        # ruff 0.16.0's JSON notes that filename and location "may now be null".
        from pathlib import Path

        out = mod.normalize(
            {"code": "B006", "filename": None, "location": None, "message": "m"},
            Path("/p"),
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
            cache={},
        )
        self.assertIsNone(out["shape_id"])


class TestPerFindingTier(unittest.TestCase):
    """A finding's `tier` is its rule's tier, not the run's --tier."""

    def test_tier_one_rule_in_a_tier_two_run_is_tagged_tier_one(self):
        from helpers import TempProject

        source = "import yaml\n\ndef f(s, x):\n    raise ValueError(x)\n"
        with TempProject({"m.py": source}) as root:
            hits = [
                _fake_hit(root, "m.py", 1, "yaml", code="S506"),
                _fake_hit(root, "m.py", 4, "raise", code="B904"),
                _fake_hit(root, "m.py", 4, "raise", code="RUF069"),
            ]
            result = _analyze_with_fake_ruff(root, hits, tier=2)
        self.assertEqual(result["tier"], 2)
        tiers = {f["code"]: f["tier"] for f in result["findings"]}
        self.assertEqual(tiers, {"S506": 1, "B904": 2, "RUF069": None})

    def test_rule_tier(self):
        self.assertEqual(mod.rule_tier("F821"), 1)
        self.assertEqual(mod.rule_tier("B904"), 2)
        self.assertIsNone(mod.rule_tier("B909"))

    def test_docstring_names_only_the_tiers_argparse_accepts(self):
        self.assertNotIn("1|2|3", mod.__doc__)


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
        "message": f"hit on `{needle}`",
    }


def _analyze_with_fake_ruff(root, hits, project_hits=(), **kwargs):
    from unittest.mock import patch

    with (
        patch.object(mod, "_run_project_ruff", return_value=(list(project_hits), None)),
        patch.object(mod, "ruff_version", return_value=mod.PINNED_RUFF_VERSION),
        patch.object(
            mod,
            "validate_rules",
            return_value={"ok": [], "unknown": [], "removed": [], "preview_gated": []},
        ),
        patch.object(mod, "_run_ruff", return_value=(hits, "", None)),
    ):
        return mod.analyze(str(root), **kwargs)


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


class TestExceptClosureFlag(unittest.TestCase):
    """F821 on an except binding read by a closure in its handler (ruff#6878)."""

    SOURCE = (
        "def f(job, retry):\n"
        "    try:\n"
        "        job()\n"
        "    except Exception as exc:\n"
        "        retry(lambda c: str(exc))\n"
        "    return missing\n"
    )

    def test_closure_read_is_flagged_but_kept_for_triage(self):
        from helpers import TempProject

        with TempProject({"m.py": self.SOURCE}) as root:
            result = _analyze_with_fake_ruff(
                root,
                [
                    _fake_hit(root, "m.py", 5, "exc", code="F821"),
                    _fake_hit(root, "m.py", 6, "missing", code="F821"),
                ],
            )
        by_line = {f["line"]: f for f in result["findings"]}
        # Flagged, not dismissed: an escaping closure makes the same F821 real.
        self.assertEqual(len(result["findings"]), 2)
        self.assertTrue(by_line[5]["suspect_tool_bug"])
        self.assertEqual(by_line[5]["known_tool_issue"], "ruff#6878")
        self.assertNotIn("suspect_tool_bug", by_line[6])
        self.assertEqual(result["summary"]["suspect_tool_bug"], 1)
        # The genuine F821 keeps F821 a novel-shape candidate ...
        self.assertEqual(result["summary"]["novel_shape_candidates"], ["F821"])

    def test_flagged_hits_carry_the_catalogued_shape(self):
        from helpers import TempProject

        with TempProject({"m.py": self.SOURCE}) as root:
            result = _analyze_with_fake_ruff(
                root,
                [
                    _fake_hit(root, "m.py", 5, "exc", code="F821"),
                    _fake_hit(root, "m.py", 4, "exc", code="F841"),
                ],
                tier=2,
            )
        for hit in result["findings"]:
            self.assertEqual(hit["shape_id"], "linter-autofix-breaks-closure-capture")
            self.assertEqual(hit["except_lines"][0], 4)

    def _project_f841(self, root, fix):
        return {
            "code": "F841",
            "filename": str(root / "m.py"),
            "location": {"row": 4, "column": 25},
            "message": "Local variable `exc` is assigned to but never used",
            "fix": fix,
        }

    def test_the_projects_own_safe_autofix_is_reported(self):
        from helpers import TempProject

        with TempProject({"m.py": self.SOURCE}) as root:
            result = _analyze_with_fake_ruff(
                root,
                [_fake_hit(root, "m.py", 5, "exc", code="F821")],
                project_hits=[self._project_f841(root, {"applicability": "safe"})],
            )
        (hit,) = result["findings"]
        # The tier-1 F821 is enough: the project's F841 fix is the defect.
        self.assertTrue(hit["project_config_autofixes"])
        self.assertEqual(hit["project_fix_applicability"], "safe")
        check = result["project_autofix_check"]
        self.assertTrue(check["ran"])
        self.assertEqual(check["autofixed_by_project_config"], 1)

    def test_an_unfixable_or_unselected_f841_is_not_an_autofix(self):
        from helpers import TempProject

        # Not selected by the project (no F841), or listed as unfixable (ruff
        # then reports the hit with no fix).
        for unfixable in (False, True):
            with TempProject({"m.py": self.SOURCE}) as root:
                project_hits = [self._project_f841(root, None)] if unfixable else []
                result = _analyze_with_fake_ruff(
                    root,
                    [_fake_hit(root, "m.py", 5, "exc", code="F821")],
                    project_hits=project_hits,
                )
            (hit,) = result["findings"]
            self.assertFalse(hit["project_config_autofixes"])
            check = result["project_autofix_check"]
            self.assertEqual(check["autofixed_by_project_config"], 0)

    def test_a_failed_project_run_is_not_measured_not_clean(self):
        from unittest.mock import patch

        from helpers import TempProject

        with TempProject({"m.py": self.SOURCE}) as root:
            hit = _fake_hit(root, "m.py", 5, "exc", code="F821")
            finding = mod.normalize(hit, root, {})
            finding.update(
                mod.except_closure_annotation(
                    "F821", hit["message"], 5, root / "m.py", {}
                )
            )
            with patch.object(mod, "_run_project_ruff", return_value=([], "boom")):
                check = mod.project_autofix_check("ruff", [finding], root)
        self.assertFalse(check["ran"])
        self.assertEqual(check["error"], "boom")
        self.assertIsNone(finding["project_config_autofixes"])

    def test_no_except_closure_hits_skips_the_check(self):
        check = mod.project_autofix_check("ruff", [], Path("/p"))
        self.assertFalse(check["ran"])
        self.assertIn("skipped", check)

    def test_a_known_tool_bug_alone_is_not_a_novel_shape(self):
        from helpers import TempProject

        with TempProject({"m.py": self.SOURCE}) as root:
            result = _analyze_with_fake_ruff(
                root, [_fake_hit(root, "m.py", 5, "exc", code="F821")]
            )
        self.assertEqual(result["summary"]["novel_shape_candidates"], [])


class TestMissingRuff(unittest.TestCase):
    def test_absent_binary_is_an_error_not_an_empty_result(self):
        result = mod.analyze(".", ruff_bin="definitely-not-a-real-ruff-binary")
        self.assertIn("error", result)
        self.assertEqual(result["findings"], [])


FAKE_RUFF = """\
#!{python}
import sys

args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("ruff 0.16.0")
elif args[:1] == ["rule"]:
    print("[]")
else:
    if {fail}:
        # What a sandboxed (snap) ruff prints for a path it cannot see.
        sys.stderr.write(
            "warning: Failed to lint " + args[-1] + ": No such file or directory"
            " (os error 2)\\n"
        )
    sys.stdout.write("[]")
"""


def write_fake_ruff(directory, *, fail):
    """An executable `ruff` in *directory*; *fail* makes it skip every path."""
    import os
    import sys
    from pathlib import Path

    path = Path(directory) / "ruff"
    path.write_text(
        FAKE_RUFF.format(python=sys.executable, fail=fail), encoding="utf-8"
    )
    os.chmod(path, 0o755)
    return path


class TestUnlintedPaths(unittest.TestCase):
    """ruff exits 0 with no findings for a path it could not read."""

    def _analyze(self, *, fail):
        import tempfile

        from helpers import TempProject

        with (
            tempfile.TemporaryDirectory() as bindir,
            TempProject({"m.py": "x = 1\n"}) as root,
        ):
            ruff = write_fake_ruff(bindir, fail=fail)
            return mod.analyze(str(root), ruff_bin=str(ruff)), root

    def test_failed_to_lint_is_an_error_not_a_clean_result(self):
        result, root = self._analyze(fail=True)
        self.assertIn("could not read", result["error"])
        self.assertEqual(result["unlinted"][0]["path"], str(root.resolve()))
        self.assertIn("No such file", result["unlinted"][0]["reason"])

    def test_a_clean_run_has_no_error(self):
        result, _ = self._analyze(fail=False)
        self.assertIsNone(result["error"])
        self.assertEqual(result["unlinted"], [])

    def test_parses_coloured_and_plain_warnings(self):
        stderr = (
            "\x1b[33mwarning\x1b[0m: Failed to lint /a/b.py: Permission denied\n"
            "warning: Selection `X` has no effect\n"
        )
        self.assertEqual(
            mod.unlinted_paths(stderr),
            [{"path": "/a/b.py", "reason": "Permission denied"}],
        )


if __name__ == "__main__":
    unittest.main()


def _dismissed_lines(result):
    return {f["line"]: f["dismissal_reason"] for f in result["dismissed"]}


def _kept_lines(result):
    return {f["line"] for f in result["findings"]}


class TestSystemExitDismissal(unittest.TestCase):
    """B904 on `raise SystemExit(...)`: no traceback is ever shown."""

    SOURCE = (
        "def main(run):\n"
        "    try:\n"
        "        run()\n"
        "    except KeyError:\n"
        "        raise SystemExit(2)\n"
        "    except OSError:\n"
        "        raise SystemExit\n"
        "    except ValueError as e:\n"
        "        raise RuntimeError(str(e))\n"
    )

    def test_systemexit_is_dismissed_and_other_raises_kept(self):
        from helpers import TempProject

        with TempProject({"cli.py": self.SOURCE}) as root:
            hits = [
                _fake_hit(root, "cli.py", n, "raise", code="B904") for n in (5, 7, 9)
            ]
            result = _analyze_with_fake_ruff(root, hits, tier=2)
        self.assertEqual(
            _dismissed_lines(result),
            {5: "raise-systemexit-in-except", 7: "raise-systemexit-in-except"},
        )
        self.assertEqual(_kept_lines(result), {9})
        self.assertIn("raise-systemexit-in-except", result["dismissal_reasons"])

    def test_a_shadowed_systemexit_is_not_the_builtin(self):
        from helpers import TempProject

        source = "class SystemExit(Exception):\n    pass\n\n" + self.SOURCE
        with TempProject({"cli.py": source}) as root:
            result = _analyze_with_fake_ruff(
                root, [_fake_hit(root, "cli.py", 8, "raise", code="B904")], tier=2
            )
        self.assertEqual(result["dismissed"], [])


class TestYamlSafeLoaderSubclassDismissal(unittest.TestCase):
    """S506 on a Loader that subclasses a safe yaml loader."""

    SOURCE = (
        "import yaml\n"
        "from yaml import SafeLoader as SL\n"
        "\n"
        "class Safe(yaml.SafeLoader):\n"
        "    pass\n"
        "\n"
        "class Safer(Safe):\n"
        "    pass\n"
        "\n"
        "class Aliased(SL):\n"
        "    pass\n"
        "\n"
        "class Unsafe(yaml.Loader):\n"
        "    pass\n"
        "\n"
        "class Mixed(yaml.SafeLoader, Helper):\n"
        "    pass\n"
        "\n"
        "def load(f, chosen):\n"
        "    a = yaml.load(f, Safe)\n"
        "    b = yaml.load(f, Loader=Safer)\n"
        "    c = yaml.load(f, Aliased)\n"
        "    d = yaml.load(f, Unsafe)\n"
        "    e = yaml.load(f, Mixed)\n"
        "    g = yaml.load(f, chosen)\n"
    )

    def test_only_all_safe_bases_are_dismissed(self):
        from helpers import TempProject

        with TempProject({"cfg.py": self.SOURCE}) as root:
            hits = [
                _fake_hit(root, "cfg.py", 20, "Safe)", code="S506"),
                _fake_hit(root, "cfg.py", 21, "Safer", code="S506"),
                _fake_hit(root, "cfg.py", 22, "Aliased", code="S506"),
                _fake_hit(root, "cfg.py", 23, "Unsafe", code="S506"),
                _fake_hit(root, "cfg.py", 24, "Mixed", code="S506"),
                _fake_hit(root, "cfg.py", 25, "chosen", code="S506"),
            ]
            result = _analyze_with_fake_ruff(root, hits)
        reason = "yaml-loader-subclasses-safeloader"
        self.assertEqual(_dismissed_lines(result), {20: reason, 21: reason, 22: reason})
        self.assertEqual(_kept_lines(result), {23, 24, 25})


class TestClosureCalledSynchronouslyDismissal(unittest.TestCase):
    """B023 on a lambda that a same-file helper calls before returning."""

    SOURCE = (
        "import threading\n"
        "\n"
        "def run_now(label, fn):\n"
        "    for _ in range(2):\n"
        "        try:\n"
        "            return fn(label)\n"
        "        except OSError:\n"
        "            pass\n"
        "\n"
        "def keep(fn):\n"
        "    PENDING.append(fn)\n"
        "\n"
        "async def run_async(fn):\n"
        "    return fn(1)\n"
        "\n"
        "def run_gen(fn):\n"
        "    yield fn(1)\n"
        "\n"
        "def run_later(fn):\n"
        "    threading.Timer(1, lambda: fn(1)).start()\n"
        "\n"
        "class Store:\n"
        "    def _retry(self, op, fn):\n"
        "        return fn(op)\n"
        "\n"
        "    def update(self, items, external):\n"
        "        for item in items:\n"
        "            self._retry('u', lambda c: c.put(item))\n"
        "            run_now('x', fn=lambda c: c.put(item))\n"
        "            keep(lambda c: c.put(item))\n"
        "            run_async(lambda c: c.put(item))\n"
        "            run_gen(lambda c: c.put(item))\n"
        "            run_later(lambda c: c.put(item))\n"
        "            external(lambda c: c.put(item))\n"
        "            self._retry('u', (lambda c: c.put(item)) if item else None)\n"
    )

    def test_verified_callees_are_dismissed_and_others_hinted(self):
        from helpers import TempProject

        with TempProject({"store.py": self.SOURCE}) as root:
            hits = [
                _fake_hit(root, "store.py", n, "item)", code="B023")
                for n in range(28, 36)
            ]
            result = _analyze_with_fake_ruff(root, hits)
        reason = "closure-called-synchronously"
        self.assertEqual(_dismissed_lines(result), {28: reason, 29: reason})
        kept = {f["line"]: f for f in result["findings"]}
        self.assertEqual(set(kept), {30, 31, 32, 33, 34, 35})
        # Stored, async, generator, deferred, unresolvable: the agent checks
        # the one callee each names.
        for line, callee in ((30, "keep"), (31, "run_async"), (34, "external")):
            self.assertTrue(kept[line]["closure_passed_as_argument"])
            self.assertEqual(kept[line]["callee"], callee)
        # Not a direct argument: no hint.
        self.assertNotIn("closure_passed_as_argument", kept[35])


class TestSqlFromLiteralsDismissal(unittest.TestCase):
    """S608 on a query whose interpolated values are all literals."""

    SOURCE = (
        "TABLE = 'events'\n"
        "\n"
        "def q(conn, ids, kind, table, extra):\n"
        "    marks = ','.join(['?'] * len(ids))\n"
        "    gen = ', '.join('?' for _ in ids)\n"
        "    name = 'a_table' if kind else 'b_table'\n"
        "    where = 'id=?'\n"
        "    if kind:\n"
        "        where += f' AND kind IN ({gen})'\n"
        "    sets = ['updated=1']\n"
        "    sets.append('kind=?')\n"
        "    conn.execute(f'SELECT * FROM {name} WHERE id IN ({marks})', ids)\n"
        "    conn.execute('UPDATE ' + TABLE + ' SET ' + ', '.join(sets) + ' WHERE ' + where)\n"
        "    conn.execute('SELECT * FROM t WHERE id IN (%s)' % marks, ids)\n"
        "    conn.execute(f'SELECT * FROM {table}')\n"
        "    bad = ['a=1']\n"
        "    bad.append(extra)\n"
        "    conn.execute('UPDATE t SET ' + ', '.join(bad))\n"
        "    shared = ['a=1']\n"
        "    fill(shared)\n"
        "    conn.execute('UPDATE t SET ' + ', '.join(shared))\n"
        "    conn.execute('SELECT {}'.format(extra))\n"
        '    conn.execute(f"SELECT {3 * extra}")\n'
    )

    def test_literal_only_queries_are_dismissed_and_others_kept(self):
        from helpers import TempProject

        with TempProject({"db.py": self.SOURCE}) as root:
            hits = [
                _fake_hit(root, "db.py", 12, "f'SELECT", code="S608"),
                _fake_hit(root, "db.py", 13, "'UPDATE", code="S608"),
                _fake_hit(root, "db.py", 14, "'SELECT", code="S608"),
                _fake_hit(root, "db.py", 15, "f'SELECT", code="S608"),
                _fake_hit(root, "db.py", 18, "'UPDATE", code="S608"),
                _fake_hit(root, "db.py", 21, "'UPDATE", code="S608"),
                _fake_hit(root, "db.py", 22, "'SELECT", code="S608"),
                _fake_hit(root, "db.py", 23, 'f"SELECT', code="S608"),
            ]
            result = _analyze_with_fake_ruff(root, hits)
        reason = "sql-built-from-literals"
        self.assertEqual(_dismissed_lines(result), {12: reason, 13: reason, 14: reason})
        # A parameter, a list fed a parameter, a list handed to another
        # function, a .format() of a parameter, a number times a parameter.
        self.assertEqual(_kept_lines(result), {15, 18, 21, 22, 23})
