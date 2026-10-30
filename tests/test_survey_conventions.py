"""Tests for survey_conventions.py."""

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest import mock

from helpers import TempProject, import_script

mod = import_script("survey_conventions")


def _survey(files: dict[str, str], **kwargs) -> dict:
    with TempProject(files) as root:
        return mod.analyze(str(root), **kwargs)["categories"]


def _counts(category: dict) -> dict[str, int]:
    return {k: v["count"] for k, v in category["variants"].items()}


class TestStringFormatting(unittest.TestCase):
    def test_variants_majority_and_minority_sites(self):
        cats = _survey(
            {
                "pkg/a.py": "def f(x):\n    return f'{x}', f'{x}!'\n",
                "pkg/b.py": "def g(x):\n    return '{}'.format(x), '%s' % x, 'id-' + x\n",
            }
        )
        fmt = cats["string_formatting"]
        self.assertEqual(
            _counts(fmt), {"fstring": 2, "format_method": 1, "percent": 1, "concat": 1}
        )
        self.assertEqual(fmt["majority"], "fstring")
        self.assertEqual(fmt["files_mixing_variants"], 1)
        self.assertEqual(
            sorted(s["variant"] for s in fmt["sites"]),
            ["concat", "format_method", "percent"],
        )
        self.assertEqual(fmt["sites_listed"], "minority")

    def test_chained_concat_is_one_site_and_numeric_add_is_none(self):
        cats = _survey({"pkg/a.py": "def f(a, n):\n    return 'x' + a + 'y', n + 1\n"})
        self.assertEqual(_counts(cats["string_formatting"]), {"concat": 1})

    def test_format_spec_and_placeholderless_fstring_not_counted(self):
        cats = _survey({"pkg/a.py": "def f(x):\n    return f'{x:>{10}}', f'plain'\n"})
        self.assertEqual(_counts(cats["string_formatting"]), {"fstring": 1})


class TestLogging(unittest.TestCase):
    def test_message_styles_and_no_double_count(self):
        cats = _survey(
            {
                "pkg/a.py": (
                    "import logging\n"
                    "logger = logging.getLogger(__name__)\n"
                    "def f(x, msg):\n"
                    "    logger.info('got %s', x)\n"
                    "    logger.info(f'got {x}')\n"
                    "    logger.error(msg)\n"
                    "    logger.warning('done')\n"
                    "    self_log = logging.getLogger('app')\n"
                    "    logging.log(10, 'v=%s' % x)\n"
                )
            }
        )
        self.assertEqual(
            _counts(cats["logging_message"]),
            {
                "lazy_percent_args": 1,
                "fstring": 1,
                "prebuilt": 1,
                "constant": 1,
                "percent_operator": 1,
            },
        )
        # The f-string and `%` inside logger calls are not string_formatting sites.
        self.assertEqual(cats["string_formatting"]["total"], 0)
        self.assertEqual(
            _counts(cats["logger_acquisition"]), {"dunder_name": 1, "literal": 1}
        )

    def test_non_logger_receiver_is_ignored(self):
        cats = _survey({"pkg/a.py": "def f(d):\n    d.info('x')\n    d.get('k')\n"})
        self.assertEqual(cats["logging_message"]["total"], 0)


class TestTyping(unittest.TestCase):
    def test_generic_and_union_styles(self):
        cats = _survey(
            {
                "pkg/a.py": (
                    "from typing import Dict, List, Optional, Union\n"
                    "def f(a: List[int], b: 'Dict[str, int]') -> Optional[int]:\n"
                    "    pass\n"
                    "def g(a: list[int], b: int | str | None, c: Union[int, str]) -> dict[str, int]:\n"
                    "    pass\n"
                )
            }
        )
        self.assertEqual(
            _counts(cats["generic_style"]), {"typing_alias": 2, "builtin": 2}
        )
        # `int | str | None` is one union, not two.
        self.assertEqual(
            _counts(cats["union_style"]), {"optional": 1, "union": 1, "pipe": 1}
        )

    def test_annotation_coverage(self):
        cats = _survey(
            {
                "pkg/a.py": (
                    "class C:\n"
                    "    def __init__(self, x: int):\n        pass\n"
                    "    def m(self, y):\n        pass\n"
                    "    @staticmethod\n"
                    "    def s(z: int) -> int:\n        return z\n"
                    "def partial(a: int, b):\n    pass\n"
                )
            }
        )
        self.assertEqual(
            _counts(cats["annotation_coverage"]), {"full": 2, "none": 1, "partial": 1}
        )


class TestDocstringStyle(unittest.TestCase):
    def test_sectioned_styles_and_plain_extra(self):
        cats = _survey(
            {
                "pkg/a.py": (
                    "def g(x):\n"
                    '    """G.\n\n    Args:\n        x: x\n    """\n'
                    "def s(x):\n"
                    '    """S.\n\n    :param x: x\n    """\n'
                    "def p():\n"
                    '    """Just prose."""\n'
                    "def n():\n    pass\n"
                )
            }
        )
        style = cats["docstring_style"]
        self.assertEqual(_counts(style), {"google": 1, "sphinx": 1})
        self.assertEqual(style["extra"], {"none": 1, "plain": 1})


class TestInventories(unittest.TestCase):
    def test_exits_and_output_list_every_site(self):
        cats = _survey(
            {
                "pkg/cli.py": (
                    "import sys, os\n"
                    "def main(ctx, console):\n"
                    "    print('a')\n"
                    "    print('b', file=sys.stderr)\n"
                    "    sys.stdout.write('c')\n"
                    "    console.print('d')\n"
                    "    if ctx:\n"
                    "        ctx.exit(2)\n"
                    "    if os:\n"
                    "        raise SystemExit(1)\n"
                    "    sys.exit(3)\n"
                    "    raise typer.Exit(code=4)\n"
                )
            }
        )
        self.assertEqual(
            _counts(cats["exits"]),
            {"framework_exit": 2, "raise_systemexit": 1, "sys_exit": 1},
        )
        self.assertEqual(len(cats["exits"]["sites"]), 4)
        self.assertEqual(cats["exits"]["sites_listed"], "all")
        self.assertEqual(
            _counts(cats["output"]),
            {
                "print_stdout": 1,
                "print_stderr": 1,
                "stream_write": 1,
                "console_print": 1,
            },
        )


class TestFiles(unittest.TestCase):
    def test_tests_skipped_unless_asked(self):
        files = {"pkg/a.py": "print(1)\n", "tests/test_a.py": "print(2)\n"}
        self.assertEqual(_survey(files)["output"]["total"], 1)
        self.assertEqual(_survey(files, include_tests=True)["output"]["total"], 2)

    def test_empty_category_has_null_majority(self):
        cats = _survey({"pkg/a.py": "x = 1\n"})
        self.assertIsNone(cats["exits"]["majority"])
        self.assertIsNone(cats["exits"]["majority_share"])

    def test_site_cap_reports_truncation(self):
        typed = "".join(f"def t{i}(x: int) -> int:\n    return x\n" for i in range(200))
        untyped = "".join(f"def u{i}(x):\n    pass\n" for i in range(105))
        cov = _survey({"pkg/a.py": typed + untyped})["annotation_coverage"]
        self.assertEqual(cov["majority"], "full")
        self.assertEqual(len(cov["sites"]), 100)
        self.assertEqual(cov["sites_truncated"], 5)

    def test_main_prints_json(self):
        with TempProject({"pkg/a.py": "print(1)\n"}) as root:
            out = io.StringIO()
            argv = ["survey_conventions.py", str(root)]
            with mock.patch("sys.argv", argv), redirect_stdout(out):
                mod.main()
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["summary"]["files_surveyed"], 1)
        self.assertIn("files_capped", payload)


if __name__ == "__main__":
    unittest.main()
