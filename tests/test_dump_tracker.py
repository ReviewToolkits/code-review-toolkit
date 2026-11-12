"""Tests for dump_tracker.py: windowed tracker dumps and local search."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from helpers import import_script

mod = import_script("dump_tracker")


class FakeTracker:
    """A list endpoint that behaves like GitHub's, including the silent cap.

    Items are sorted by ``updated_at``; ``since`` keeps items updated at or
    after it; a page past ``cap_pages`` comes back empty, with no error, the
    way comment listing goes quiet after about 30,000 items.
    """

    def __init__(self, issues, comments, cap_pages=3, total=None, fail=None):
        self.issues = issues
        self.comments = comments
        self.cap_pages = cap_pages
        self.total = len(issues) if total is None else total
        self.fail = fail
        self.calls = []

    def __call__(self, path):
        self.calls.append(path)
        if self.fail and self.fail in path:
            return None, "HTTP 502"
        parts = urlsplit(path)
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        if parts.path.startswith("search/issues"):
            return {"total_count": self.total, "items": []}, None
        rows = self.comments if parts.path.endswith("/comments") else self.issues
        since = query.get("since")
        rows = sorted(
            (r for r in rows if not since or r["updated_at"] >= since),
            key=lambda r: r["updated_at"],
        )
        page, per_page = int(query["page"]), int(query["per_page"])
        if page > self.cap_pages:
            return [], None
        return rows[(page - 1) * per_page : page * per_page], None


def _ts(i):
    return f"2024-01-01T{i // 3600:02d}:{i // 60 % 60:02d}:{i % 60:02d}Z"


def _issue(n, **over):
    item = {
        "number": n,
        "title": f"Issue {n}",
        "body": "",
        "state": "open",
        "created_at": "2023-05-01T00:00:00Z",
        "updated_at": _ts(n),
        "labels": [],
        "user": {"login": "someone"},
    }
    item.update(over)
    return item


def _comment(cid, n, body, **over):
    item = {
        "id": cid,
        "issue_url": f"https://api.example.invalid/repos/o/r/issues/{n}",
        "body": body,
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": _ts(cid),
        "user": {"login": "someone"},
    }
    item.update(over)
    return item


class TestDump(unittest.TestCase):
    def test_windows_get_past_the_silent_cap(self):
        # 450 items, a cap at 300 (3 pages): one paginated listing would stop
        # at 300 and look finished.
        issues = [_issue(n) for n in range(1, 451)]
        comments = [_comment(c, 1, f"c{c}") for c in range(1, 351)]
        fake = FakeTracker(issues, comments, cap_pages=3)
        with tempfile.TemporaryDirectory() as out:
            manifest = mod.dump("o/r", out, fetch=fake, max_pages=2)
            rows = mod._read_jsonl(Path(out) / "issues.jsonl")
            crows = mod._read_jsonl(Path(out) / "comments.jsonl")
        self.assertEqual([r["n"] for r in rows], list(range(1, 451)))
        self.assertEqual(len(crows), 350)
        self.assertEqual(manifest["complete"], True)
        self.assertEqual(manifest["errors"], [])
        self.assertGreater(manifest["windows"], 1)

    def test_a_too_large_window_is_reported_incomplete(self):
        issues = [_issue(n) for n in range(1, 451)]
        fake = FakeTracker(issues, [], cap_pages=3)
        with tempfile.TemporaryDirectory() as out:
            manifest = mod.dump("o/r", out, fetch=fake, max_pages=10, comments=False)
        self.assertEqual(manifest["issues_and_prs"], 300)
        self.assertIs(manifest["complete"], False)

    def test_records_are_compact_and_mark_merged_prs(self):
        issues = [
            _issue(1, pull_request={"merged_at": "2024-02-01T00:00:00Z"}),
            _issue(2, pull_request={"merged_at": None}, state="closed"),
            _issue(3, labels=[{"name": "bug"}]),
        ]
        fake = FakeTracker(issues, [])
        with tempfile.TemporaryDirectory() as out:
            mod.dump("o/r", out, fetch=fake, comments=False)
            rows = {r["n"]: r for r in mod._read_jsonl(Path(out) / "issues.jsonl")}
        self.assertTrue(rows[1]["pr"] and rows[1]["merged"])
        self.assertTrue(rows[2]["pr"])
        self.assertFalse(rows[2]["merged"])
        self.assertFalse(rows[3]["pr"])
        self.assertEqual(rows[3]["labels"], ["bug"])

    def test_items_with_one_timestamp_beyond_a_window_are_an_error(self):
        issues = [_issue(n, updated_at="2024-01-01T00:00:00Z") for n in range(1, 301)]
        fake = FakeTracker(issues, [], cap_pages=10)
        with tempfile.TemporaryDirectory() as out:
            manifest = mod.dump("o/r", out, fetch=fake, max_pages=2, comments=False)
        self.assertTrue(any("no progress" in e for e in manifest["errors"]))
        self.assertIsNone(manifest["complete"])

    def test_a_failed_request_is_an_error_not_an_empty_tracker(self):
        fake = FakeTracker([_issue(1)], [], fail="issues/comments")
        with tempfile.TemporaryDirectory() as out:
            manifest = mod.dump("o/r", out, fetch=fake)
        self.assertIn("HTTP 502", manifest["errors"][0])
        self.assertIsNone(manifest["complete"])


class TestSearch(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        issues = [
            _issue(1, title="Retry lost after timeout", body="the task is dropped"),
            _issue(
                2,
                title="Docs: upload limits",
                body="nothing here",
                pull_request={"merged_at": "2024-01-02T00:00:00Z"},
            ),
            _issue(3, title="Unrelated", body="text", state="closed"),
        ]
        comments = [_comment(10, 3, "the retry is lost here too, on timeout")]
        mod.dump("o/r", self.dir, fetch=FakeTracker(issues, comments))

    def tearDown(self):
        self._tmp.cleanup()

    def _ns(self, terms, **kw):
        return [i["n"] for i in mod.search(self.dir, terms, **kw)["items"]]

    def test_terms_are_and_and_rank_title_first(self):
        self.assertEqual(self._ns(["retry", "timeout"]), [1, 3])

    def test_comments_can_be_excluded(self):
        self.assertEqual(self._ns(["retry", "timeout"], comments=False), [1])

    def test_filters(self):
        self.assertEqual(self._ns(["retry"], state="closed"), [3])
        self.assertEqual(self._ns(["upload"], kind="pr"), [2])
        self.assertEqual(self._ns(["upload"], kind="issue"), [])
        self.assertEqual(self._ns(["retry"], title_only=True), [1])

    def test_regex_term_and_merged_state(self):
        result = mod.search(self.dir, ["~upload\\s+limits"])
        self.assertEqual(result["items"][0]["state"], "merged")
        self.assertEqual(result["complete"], True)


class TestCli(unittest.TestCase):
    def _run(self, argv):
        buf = io.StringIO()
        code = 0
        with contextlib.redirect_stdout(buf):
            try:
                mod.main(argv)
            except SystemExit as exc:
                code = exc.code
        return code, json.loads(buf.getvalue())

    def test_bad_subcommand(self):
        code, result = self._run(["nope"])
        self.assertEqual(code, 2)
        self.assertIn("error", result)

    def test_dump_needs_repo_and_out(self):
        code, _ = self._run(["dump", "--repo", "o/r"])
        self.assertEqual(code, 2)

    def test_search_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            mod.dump("o/r", tmp, fetch=FakeTracker([_issue(1, title="Lost retry")], []))
            code, result = self._run(["search", tmp, "retry", "--limit", "5"])
        self.assertEqual(code, 0)
        self.assertEqual(result["matches"], 1)


if __name__ == "__main__":
    unittest.main()
