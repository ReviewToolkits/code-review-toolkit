"""Scope flags shared across scripts: --exclude, migration layouts, --git-dir, caps.

The helpers live in scan_common (see test_scan_common.py); these tests check
that each script wires them in: excluded files are neither counted nor charged
to --max-files, migration revisions stop showing up as dead/untested/complex
code, history can come from a separate checkout, and hard-coded caps can be
raised.
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
from unittest.mock import MagicMock, patch

from helpers import TempProject, import_script

ALEMBIC_REVISION = (
    '"""add a column"""\n'
    "from alembic import op\n"
    "import sqlalchemy as sa\n\n"
    'revision = "a1b2c3"\n'
    'down_revision = "f0e1d2"\n\n\n'
    "def upgrade():\n"
    '    op.add_column("item", sa.Column("size", sa.Integer()))\n\n\n'
    "def downgrade():\n"
    '    op.drop_column("item", "size")\n'
)
ALEMBIC_ENV = (
    "from alembic import context\n\n\n"
    "def run_migrations_online():\n"
    "    context.run_migrations()\n"
)


def run_main(mod, argv: list[str]) -> tuple[dict | None, str, int]:
    """Run *mod*.main() with *argv*; return (parsed stdout JSON, stderr, exit code)."""
    out, err = io.StringIO(), io.StringIO()
    old_argv = sys.argv
    sys.argv = [f"{mod.__name__}.py", *argv]
    code = 0
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                mod.main()
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
    finally:
        sys.argv = old_argv
    text = out.getvalue()
    return (json.loads(text) if text.strip() else None), err.getvalue(), code


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


MIGRATION_PROJECT = {
    "pkg/__init__.py": "",
    "pkg/core.py": "def work(x):\n    return x + 1\n",
    "pkg/migrations/env.py": ALEMBIC_ENV,
    "pkg/migrations/versions/a1b2c3_add.py": ALEMBIC_REVISION,
    "pkg/migrations/versions/f0e1d2_init.py": ALEMBIC_REVISION.replace(
        '"a1b2c3"', '"f0e1d2"'
    ).replace('"f0e1d2"\n\n', "None\n\n"),
    "tests/test_core.py": (
        "from pkg.core import work\n\n\ndef test_work():\n    assert work(1) == 2\n"
    ),
}


EXCLUDE_FILES = {
    "pkg/__init__.py": "",
    "pkg/core.py": "# TODO: tidy\ndef run(a: int) -> int:\n    return a\n",
    "pkg/vendor/lib.py": "# TODO: upstream\ndef v(a, b):\n    return a\n",
}


def check_exclude(tc: unittest.TestCase, name: str, extra: tuple[str, ...] = ()):
    """Run script *name* on EXCLUDE_FILES with --exclude vendor/*; check the envelope."""
    mod = import_script(name)
    with TempProject(EXCLUDE_FILES) as root:
        out, err, code = run_main(
            mod, [str(root / "pkg"), "--exclude", "vendor/*", *extra]
        )
    tc.assertEqual(code, 0, err)
    tc.assertNotIn("unknown flag", err)
    tc.assertEqual(out["files_excluded"], 1, name)
    tc.assertEqual(out["exclude_matches"], {"vendor/*": 1}, name)
    tc.assertEqual(out["files_total"], 2, name)
    return out


def assert_no_unknown_flag_warning(tc, name: str, flags: list[str]) -> None:
    with TempProject({"m.py": "x = 1\n"}) as root:
        out, err, code = run_main(import_script(name), [str(root), *flags])
    tc.assertEqual(code, 0, err)
    tc.assertNotIn("unknown flag", err)
    tc.assertIsNotNone(out)


def _history_repo(root: Path) -> None:
    """A repo with one fix commit that adds a test."""
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "Add core")
    test = root / "tests/test_core.py"
    test.write_text(
        test.read_text(encoding="utf-8")
        + "\n\ndef test_regression():\n    assert work(2) == 3\n",
        encoding="utf-8",
    )
    core = root / "pkg/core.py"
    core.write_text(
        "def work(x):\n    if x is None:\n        return 0\n    return x + 1\n",
        encoding="utf-8",
    )
    _git(root, "commit", "-q", "-am", "Fix the off-by-one in work")


class _ExportAndCheckout:
    """An export (no .git) beside the checkout it came from."""

    FILES: ClassVar[dict[str, str]] = {
        "pkg/__init__.py": "",
        "pkg/core.py": "def work(x):\n    return x + 1\n",
        "tests/test_core.py": (
            "from pkg.core import work\n\n\ndef test_work():\n    assert work(1) == 2\n"
        ),
    }

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="crt_scope_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "checkout"
        self.export = self.tmp / "export"
        for rel, text in {**self.FILES, "pyproject.toml": "[project]\n"}.items():
            for base in (self.repo, self.export):
                (base / rel).parent.mkdir(parents=True, exist_ok=True)
                (base / rel).write_text(text, encoding="utf-8")
        _history_repo(self.repo)
        for rel in ("tests/test_core.py", "pkg/core.py"):
            shutil.copy(self.repo / rel, self.export / rel)
        # Keep git from finding any repository above the export.
        self.env = patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(self.tmp)})
        self.env.start()
        self.addCleanup(self.env.stop)


class TestScannersCountAfterExclusion(unittest.TestCase):
    """--exclude filters before counting and before the --max-files cap."""

    FILES: ClassVar[dict[str, str]] = {
        "src/a.py": "def f(x=[]):\n    x.append(1)\n",
        "generated/b.py": "def g(y=[]):\n    y.append(1)\n",
        "generated/c.py": "def h(z=[]):\n    z.append(1)\n",
        "generated/d.py": "def k(w=[]):\n    w.append(1)\n",
    }

    def test_pitfalls_counts_exclude_excluded_files(self):
        mod = import_script("scan_python_pitfalls")
        with TempProject(self.FILES) as root:
            result = mod.analyze(str(root), exclude=["generated/"])
        self.assertEqual(result["files_total"], 1)
        self.assertEqual(result["files_analyzed"], 1)
        self.assertEqual(result["files_excluded"], 3)
        self.assertEqual(result["exclude_matches"], {"generated/": 3})

    def test_pitfalls_excluded_files_do_not_use_the_cap(self):
        # generated/ sorts before src/: charged to the cap, it left src/ unscanned.
        mod = import_script("scan_python_pitfalls")
        with TempProject(self.FILES) as root:
            result = mod.analyze(str(root), max_files=1, exclude=["generated/*"])
        self.assertEqual(result["summary"]["total_findings"], 1)
        self.assertEqual(result["findings"][0]["file"], "src/a.py")
        self.assertFalse(result["files_capped"])

    def test_concurrency_counts_exclude_excluded_files(self):
        mod = import_script("scan_concurrency")
        with TempProject(self.FILES) as root:
            result = mod.analyze(str(root), exclude=["generated"])
        self.assertEqual((result["files_total"], result["files_analyzed"]), (1, 1))
        self.assertEqual(result["files_excluded"], 3)
        self.assertEqual(result["summary"]["excluded_patterns"], ["generated"])

    def test_pattern_matching_nothing_shows_zero(self):
        mod = import_script("scan_python_pitfalls")
        with TempProject(self.FILES) as root:
            result = mod.analyze(str(root), exclude=["generated/", "vendored/"])
        self.assertEqual(result["exclude_matches"], {"generated/": 3, "vendored/": 0})

    def test_cli_exclude_is_repeatable_and_comma_separated(self):
        mod = import_script("scan_python_pitfalls")
        with TempProject(self.FILES) as root:
            out, err, code = run_main(
                mod,
                [str(root), "--exclude", "generated/b.py,generated/c.py"]
                + ["--exclude=generated/d.py"],
            )
        self.assertEqual(code, 0)
        self.assertEqual(out["files_excluded"], 3)
        self.assertEqual(err, "")

    def test_concurrency_cli_keeps_its_own_flags(self):
        mod = import_script("scan_concurrency")
        with TempProject(self.FILES) as root:
            out, err, code = run_main(
                mod, [str(root), "--all-blocking", "--blocking", "slow_io"]
            )
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(
            out["summary"]["blocking_low_signal"]["reported_as"], "findings"
        )


if __name__ == "__main__":
    unittest.main()
