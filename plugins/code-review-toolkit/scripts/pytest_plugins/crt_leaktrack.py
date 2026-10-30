"""pytest plugin: attribute sqlite3 connections that are never closed.

An unclosed ``sqlite3.Connection`` is reported (Python 3.13+: as a
``ResourceWarning``) when it is garbage-collected, which is long after -- and
far from -- the code that opened it, so the warning names no useful source.
``PYTHONTRACEMALLOC`` can attribute it, but can slow a suite many times over.
This plugin records where each connection was created instead, at normal speed,
and counts the ones collected without ``close()``.

Nothing happens on import. On ``pytest_configure`` it wraps ``sqlite3.connect``
so every connection is built from a subclass of the requested ``factory`` that
remembers its creation site; ``pytest_unconfigure`` restores it. Note that
``with sqlite3.connect(...) as conn:`` commits or rolls back but does NOT close,
so such connections are reported as leaked -- correctly.

Usage, in an exported copy of the project (never the checkout)::

    PYTHONPATH=<plugin_root>/scripts/pytest_plugins PYTHONDONTWRITEBYTECODE=1 \\
    CRT_LEAKTRACK_OUT="$d/leaks.json" CRT_LEAKTRACK_ROOT=<copy> \\
    python -m pytest -p crt_leaktrack -p no:cacheprovider <copy>/tests -q

Environment:
    CRT_LEAKTRACK_ROOT   only frames under this directory name a creation site
                         (default: pytest's rootdir)
    CRT_LEAKTRACK_OUT    write the JSON report here (default: terminal only)
    CRT_LEAKTRACK_DEPTH  project frames per site, innermost first (default 3)
"""

from __future__ import annotations

import gc
import json
import os
import sqlite3
import traceback
from collections import Counter

ENV_ROOT = "CRT_LEAKTRACK_ROOT"
ENV_OUT = "CRT_LEAKTRACK_OUT"
ENV_DEPTH = "CRT_LEAKTRACK_DEPTH"

_THIS_FILE = os.path.realpath(__file__)


class Tracker:
    """Records the creation site of every sqlite3 connection it builds."""

    def __init__(self, root: str, depth: int = 3) -> None:
        self.root = os.path.realpath(root)
        self.depth = max(1, depth)
        self.created = 0
        self.closed = 0
        self.leaked: Counter[str] = Counter()
        self._live: dict[int, str] = {}
        self._classes: dict[type, type] = {}
        self._original = None

    def origin(self) -> str:
        """``file:line <- file:line`` for the innermost project frames."""
        sites = []
        for frame in reversed(traceback.extract_stack()):
            path = os.path.realpath(frame.filename)
            if path == _THIS_FILE or not path.startswith(self.root + os.sep):
                continue
            sites.append(f"{os.path.relpath(path, self.root)}:{frame.lineno}")
            if len(sites) == self.depth:
                break
        return " <- ".join(sites) or "<outside root>"

    def tracked_class(self, factory: type) -> type:
        """A subclass of *factory* that reports itself when leaked."""
        cls = self._classes.get(factory)
        if cls is not None:
            return cls
        tracker = self

        class Tracked(factory):  # type: ignore[misc, valid-type]
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self._crt_origin = tracker.origin()
                self._crt_closed = False
                tracker.created += 1
                tracker._live[id(self)] = self._crt_origin

            def close(self):
                if not self._crt_closed:
                    self._crt_closed = True
                    tracker.closed += 1
                    tracker._live.pop(id(self), None)
                return super().close()

            def __del__(self):
                if not getattr(self, "_crt_closed", True):
                    tracker.leaked[self._crt_origin] += 1
                    tracker._live.pop(id(self), None)
                parent = getattr(super(), "__del__", None)
                if parent is not None:
                    parent()

        Tracked.__name__ = Tracked.__qualname__ = f"Tracked{factory.__name__}"
        self._classes[factory] = Tracked
        return Tracked

    def install(self) -> None:
        if self._original is not None:
            return
        original = sqlite3.connect
        tracker = self

        def connect(*args, **kwargs):
            # `factory` is the sixth positional parameter; leave such rare
            # calls alone rather than guess.
            if len(args) < 6:
                factory = kwargs.get("factory") or sqlite3.Connection
                kwargs["factory"] = tracker.tracked_class(factory)
            return original(*args, **kwargs)

        self._original = original
        sqlite3.connect = connect

    def uninstall(self) -> None:
        if self._original is not None:
            sqlite3.connect = self._original
            self._original = None

    def report(self) -> dict:
        gc.collect()
        still_open = Counter(self._live.values())
        return {
            "tool": "crt_leaktrack",
            "root": self.root,
            "created": self.created,
            "closed": self.closed,
            "leaked_total": sum(self.leaked.values()),
            # Collected without close(): each is a ResourceWarning at an
            # arbitrary later point. Sorted by count.
            "leaked": [
                {"origin": origin, "count": count}
                for origin, count in self.leaked.most_common()
            ],
            # Still referenced when the session ended (module-level caches,
            # fixtures that outlive the session): not leaked yet, never closed.
            "open_at_session_end": [
                {"origin": origin, "count": count}
                for origin, count in still_open.most_common()
            ],
        }


_tracker: Tracker | None = None
_report: dict | None = None


def pytest_configure(config) -> None:
    global _tracker
    root = os.environ.get(ENV_ROOT) or str(config.rootpath)
    try:
        depth = int(os.environ.get(ENV_DEPTH, "3"))
    except ValueError:
        depth = 3
    _tracker = Tracker(root, depth)
    _tracker.install()


def pytest_sessionfinish(session, exitstatus) -> None:
    global _report
    if _tracker is None:
        return
    _report = _tracker.report()
    out = os.environ.get(ENV_OUT)
    if out:
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(_report, fh, indent=2)
            fh.write("\n")


def pytest_terminal_summary(terminalreporter) -> None:
    if not _report:
        return
    terminalreporter.section("crt_leaktrack: sqlite3 connections")
    terminalreporter.write_line(
        f"created {_report['created']}, closed {_report['closed']}, "
        f"leaked {_report['leaked_total']}"
    )
    for entry in _report["leaked"][:15]:
        terminalreporter.write_line(f"{entry['count']:6d}  {entry['origin']}")


def pytest_unconfigure(config) -> None:
    if _tracker is not None:
        _tracker.uninstall()
