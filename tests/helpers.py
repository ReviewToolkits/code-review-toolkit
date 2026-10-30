"""Helpers for importing scripts as modules and creating test fixtures."""

import importlib.util
import sys
import tempfile
from pathlib import Path

_SCRIPTS_DIR = (
    Path(__file__).resolve().parent.parent
    / "plugins"
    / "code-review-toolkit"
    / "scripts"
)


def import_script(name: str):
    """Import a script from the scripts/ directory as a module.

    Usage:
        mod = import_script("analyze_imports")
        result = mod.analyze_file(path, root, packages)
    """
    script_path = _SCRIPTS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, script_path)
    module = importlib.util.module_from_spec(spec)
    # Registered only while it executes: `@dataclass` resolves string annotations
    # (`from __future__ import annotations`) through sys.modules[cls.__module__].
    # Removed afterwards so tests don't share module state.
    previous = sys.modules.get(name)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous
    return module


class TempProject:
    """Context manager that creates a temporary Python project on disk.

    Usage:
        with TempProject({
            "pkg/__init__.py": "from .core import main",
            "pkg/core.py": "def main(): pass",
            "tests/test_core.py": "import unittest\\nclass TestCore(unittest.TestCase):\\n    def test_main(self): pass",
        }) as root:
            # root is a Path to the temp directory
            mod = import_script("analyze_imports")
            result = mod.analyze_file(root / "pkg/core.py", root, {"pkg"})
    """

    def __init__(self, files: dict[str, str]):
        self._files = files
        self._tmpdir = None

    def __enter__(self) -> Path:
        self._tmpdir = tempfile.mkdtemp(prefix="crt_test_")
        root = Path(self._tmpdir)
        for relpath, content in self._files.items():
            filepath = root / relpath
            filepath.parent.mkdir(parents=True, exist_ok=True)
            filepath.write_text(content, encoding="utf-8")
        # Create a pyproject.toml so find_project_root works.
        (root / "pyproject.toml").write_text(
            '[project]\nname = "test-project"\n', encoding="utf-8"
        )
        return root

    def __exit__(self, *args):
        import shutil
        if self._tmpdir:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
