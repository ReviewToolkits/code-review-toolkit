"""Tests for check_doc_refs.py."""

import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from typing import ClassVar

from helpers import TempProject, import_script

mod = import_script("check_doc_refs")


_PROJECT = {
    "shop/__init__.py": "",
    "shop/orders.py": (
        "def place_order(cart):\n"
        "    return cart\n"
        "class Ledger:\n"
        "    def post(self):\n"
        "        return 1\n"
    ),
    "shop/views.py": (
        "def help_link(request):\n"
        "    return request.guide_url('/ordering/', anchor='refunds')\n"
        "def other_link(request):\n"
        "    return request.guide_url('/ordering/', anchor='no-such-section')\n"
        "def missing_page(request):\n"
        "    return request.guide_url('/gone/')\n"
        "def unmapped(request):\n"
        "    return request.api_docs_url('/x/')\n"
    ),
    "shop/templates/help.html": (
        '<a href="https://guide.example.test/ordering/#placing-an-order">help</a>\n'
        '<a href="https://guide.example.test/ordering/#stale-anchor">old</a>\n'
    ),
    "mkdocs.yml": "site_name: Guide\nsite_url: https://guide.example.test/\ndocs_dir: guide\n",
    "guide/ordering.md": (
        "# Ordering\n\n"
        "## Placing an order\n\n"
        "## Refunds {#refunds}\n\n"
        "## 📦 Shipping options\n\n"
        "## Placing an order\n"
    ),
    "README.md": (
        "# Shop\n\n"
        "See [ordering](guide/ordering.md#placing-an-order) and\n"
        "[the duplicate](guide/ordering.md#placing-an-order_1),\n"
        "[shipping](guide/ordering.md#-shipping-options) (GitHub's anchor),\n"
        "[missing](guide/nothing.md), [bad anchor](guide/ordering.md#nowhere)\n"
        "and [upstream](https://example.test/x).\n\n"
        "Call `place_order()` or `shop.orders.Ledger.post`; not `shop.orders.cancel`\n"
        "nor `refund_all()`. `PayPal` is a product name, `shop.example.org` a host.\n"
        "Settings live in `shop/settings.py`; templates in `shop/templates/help.html`.\n"
        "The repo is `someorg/shop`; release notes go in `docs/RELEASE-vX.Y.md`.\n\n"
        "[^1]: A footnote, not a link.\n\n"
        "```console\n"
        "$ make tests\n"
        "$ make build\n"
        "$ LEVEL=debug make run\n"
        "$ PORT=9000 make run\n"
        "$ make dist/shop.tar.gz\n"
        "$ make run TIMEOUT=5 WORKERS=2\n"
        "[a link in a code block](nowhere.md)\n"
        "```\n"
    ),
    "CHANGELOG.md": "- Removed `shop.orders.legacy_checkout()`.\n",
    "Makefile": (
        ".PHONY: run\n"
        "LEVEL := info\n"
        "PORT ?= 8000\n"
        "build:\n\tpython -m build\n"
        "run:\n\tserve --level $(LEVEL) --port $(PORT)\n"
        "dist/%.tar.gz:\n\ttar czf $@ shop\n"
    ),
    "tests/test_orders.py": "def test_x():\n    pass\n",
    "compose.yaml": "services:\n  web:\n    environment:\n      - WORKERS\n",
}


def _run(files=None, **kwargs) -> dict:
    with TempProject(files or _PROJECT) as root:
        return mod.analyze(str(root / "shop"), **kwargs)


class TestSourceLinks(unittest.TestCase):
    out: ClassVar[dict]

    @classmethod
    def setUpClass(cls):
        cls.out = _run(doc_helpers=["guide_url=guide"])

    def test_missing_files_and_anchors(self):
        broken = {(b["target"], b["kind"]) for b in self.out["links"]["broken"]}
        self.assertEqual(
            broken,
            {
                ("guide/nothing.md", "missing_file"),
                ("guide/ordering.md#nowhere", "missing_anchor"),
            },
        )

    def test_external_links_are_counted_not_checked(self):
        self.assertEqual(self.out["links"]["external_skipped"], 1)

    def test_slugify_matches_the_generators(self):
        self.assertEqual(mod.slugify("Placing an `order()`"), "placing-an-order")
        self.assertEqual(
            mod.slugify("changelog_since(serial)"), "changelog_sinceserial"
        )
        self.assertEqual(mod.github_slug("📦 Shipping options"), "-shipping-options")


class TestAppDocLinks(unittest.TestCase):
    def test_absolute_urls_and_helper_calls_resolve_to_pages_and_anchors(self):
        out = _run(doc_helpers=["guide_url=guide"])
        app = out["app_doc_links"]
        problems = {(p["file"], p["line"], p["status"]) for p in app["problems"]}
        self.assertEqual(
            problems,
            {
                ("shop/views.py", 4, "missing_anchor"),
                ("shop/views.py", 6, "missing_page"),
                ("shop/views.py", 8, "unchecked"),
                ("shop/templates/help.html", 2, "missing_anchor"),
            },
        )
        self.assertEqual(app["ok"], 2)
        self.assertEqual(out["docs_bases"], {"https://guide.example.test/": "guide"})

    def test_a_helper_without_a_mapping_is_unchecked_not_clean(self):
        app = _run()["app_doc_links"]
        # A `*docs_url(...)` helper is recognised by name even unmapped.
        unchecked = [p for p in app["problems"] if p["status"] == "unchecked"]
        self.assertEqual([p["via"] for p in unchecked], ["helper:api_docs_url"])
        self.assertIn("--doc-helper", unchecked[0]["reason"])


class TestNamesAndPaths(unittest.TestCase):
    out: ClassVar[dict]

    @classmethod
    def setUpClass(cls):
        cls.out = _run()

    def test_unresolved_code_names(self):
        found = {
            (i["name"], i["confidence"]) for i in self.out["identifiers"]["unresolved"]
        }
        self.assertEqual(
            found, {("shop.orders.cancel", "high"), ("refund_all()", "medium")}
        )

    def test_missing_paths_only_where_the_path_starts_somewhere_real(self):
        self.assertEqual(
            [p["path"] for p in self.out["paths"]["missing"]], ["shop/settings.py"]
        )


class TestMake(unittest.TestCase):
    out: ClassVar[dict]

    @classmethod
    def setUpClass(cls):
        cls.make = _run()["make"]

    def test_missing_target_flags_the_silent_no_op(self):
        self.assertEqual(
            [(m["target"], m["path_exists"]) for m in self.make["missing_targets"]],
            [("tests", True)],
        )

    def test_env_override_of_a_plain_assignment_is_ignored(self):
        self.assertEqual(
            [
                (i["variable"], i["makefile_assignment"])
                for i in self.make["env_overrides_ignored"]
            ],
            [("LEVEL", "LEVEL := (line 2)")],
        )

    def test_variable_no_one_reads(self):
        # WORKERS reaches the compose file through the environment.
        self.assertEqual(
            [v["variable"] for v in self.make["variables_not_in_makefile"]], ["TIMEOUT"]
        )

    def test_build_target_colliding_with_a_path(self):
        files = dict(_PROJECT)
        files["build/.keep"] = ""
        make = _run(files)["make"]
        self.assertEqual(
            [c["target"] for c in make["not_phony_path_collisions"]], ["build"]
        )


class TestSiteAndAbsences(unittest.TestCase):
    def test_built_site_links_and_anchors(self):
        files = {
            "shop/__init__.py": "",
            "site/index.html": (
                '<h2 id="intro">x</h2><a href="guide/">g</a>'
                '<a href="guide/#setup">s</a><a href="#intro">i</a>'
                '<a href="gone.html">x</a><a href="guide/#nope">n</a>'
            ),
            "site/guide/index.html": '<h2 id="setup">Setup</h2>',
        }
        with TempProject(files) as root:
            out = mod.analyze(str(root), site=str(root / "site"))
        broken = {(b["target"], b["kind"]) for b in out["site"]["broken"]}
        self.assertEqual(
            broken, {("gone.html", "missing_file"), ("guide/#nope", "missing_anchor")}
        )

    def test_no_site_and_no_makefile_are_null_with_a_reason(self):
        with TempProject({"shop/__init__.py": "", "README.md": "# x\n"}) as root:
            out = mod.analyze(str(root))
        self.assertIsNone(out["site"])
        self.assertIn("--site", out["site_reason"])
        self.assertIsNone(out["make"])
        self.assertIsNone(out["summary"]["missing_make_targets"])

    def test_main_parses_its_own_flags(self):
        with TempProject(_PROJECT) as root:
            argv = sys.argv
            sys.argv = [
                "check_doc_refs.py", str(root / "shop"), "--doc-helper", "guide_url=guide",
                "--max-files", "1",
            ]  # fmt: skip
            buf = io.StringIO()
            try:
                with redirect_stdout(buf):
                    mod.main()
            finally:
                sys.argv = argv
        out = json.loads(buf.getvalue())
        self.assertEqual(out["doc_helpers"], {"guide_url": "guide"})
        self.assertTrue(out["files_capped"])


if __name__ == "__main__":
    unittest.main()
