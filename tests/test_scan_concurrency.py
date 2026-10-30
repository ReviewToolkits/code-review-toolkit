"""Tests for scan_concurrency.py.

Each shape gets a true positive and its guarded twin, which must stay silent:
the twin is the documented fix, and a scanner that fires on the fix is worse than
none. Several shapes need a call chain, so fixtures span functions and modules.
"""

import unittest
from textwrap import dedent

from helpers import TempProject, import_script

mod = import_script("scan_concurrency")


def scan_project(files: dict[str, str], **kw) -> dict:
    with TempProject({k: dedent(v) for k, v in files.items()}) as root:
        return mod.analyze(str(root), **kw)


def scan(source: str, **kw) -> list[dict]:
    return scan_project({"pkg/mod.py": source}, **kw)["findings"]


def of(findings: list[dict], shape: str) -> list[dict]:
    return [f for f in findings if f["shape"] == shape]


_GUARDED_CLASS = """
import threading

class Cache:
    def __init__(self):
        self._lock = threading.Lock()
        self._items = {{}}

    def put(self, k, v):
        with self._lock:
            self._items[k] = v

    def drop(self, k):
        with self._lock:
            self._items.pop(k, None)

    def get(self, k):
        with self._lock:
            return self._items.get(k)

    def clear_all(self):
        {body}
"""


class TestUnguardedAccess(unittest.TestCase):
    shape = "unguarded-access-to-lock-guarded-state"

    def test_mutation_without_the_guarding_lock(self):
        found = of(scan(_GUARDED_CLASS.format(body="self._items.clear()")), self.shape)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["function"], "Cache.clear_all")
        self.assertIn("Cache._lock", found[0]["message"])

    def test_guarded_twin_is_silent(self):
        src = _GUARDED_CLASS.format(
            body="with self._lock:\n            self._items.clear()"
        )
        self.assertEqual(of(scan(src), self.shape), [])

    def test_iteration_without_lock_is_reported(self):
        src = _GUARDED_CLASS.format(body="for k in self._items:\n            print(k)")
        found = of(scan(src), self.shape)
        self.assertEqual(len(found), 1)
        self.assertIn("iterates", found[0]["message"])

    def test_private_helper_called_only_under_lock_inherits_it(self):
        src = _GUARDED_CLASS.format(
            body="with self._lock:\n            self._clear_locked()\n\n"
            "    def _clear_locked(self):\n        self._items.clear()"
        )
        self.assertEqual(of(scan(src), self.shape), [])

    def test_helper_also_called_without_lock_is_reported(self):
        src = dedent(
            """
            import threading

            class Cache:
                def __init__(self):
                    self._lock = threading.Lock()
                    self._items = {}

                def put(self, k, v):
                    with self._lock:
                        self._items[k] = v

                def drop(self, k):
                    with self._lock:
                        self._items.pop(k, None)

                def locked_clear(self):
                    with self._lock:
                        self._clear()

                def fast_clear(self):
                    self._clear()

                def _clear(self):
                    self._items.clear()
            """
        )
        found = of(scan(src), self.shape)
        self.assertEqual([f["function"] for f in found], ["Cache._clear"])

    def test_init_writes_do_not_count(self):
        # __init__ runs before the object is shared; it is not an unguarded site.
        result = scan_project({"pkg/mod.py": _GUARDED_CLASS.format(body="pass")})
        self.assertEqual(of(result["findings"], self.shape), [])
        guards = {g["state"]: g["lock"] for g in result["guards"]}
        self.assertEqual(guards.get("pkg.mod:Cache._items"), "pkg.mod:Cache._lock")

    def test_module_global_guarded_by_module_lock(self):
        src = """
        import threading
        _lock = threading.Lock()
        _counts = {}

        def inc(k):
            with _lock:
                _counts[k] = _counts.get(k, 0) + 1

        def snapshot():
            with _lock:
                return dict(_counts)

        def reset():
            _counts.clear()
        """
        found = of(scan(src), self.shape)
        self.assertEqual([f["function"] for f in found], ["reset"])

    def test_unguarded_reads_are_not_reported(self):
        src = _GUARDED_CLASS.format(body="return len(self._items)")
        self.assertEqual(of(scan(src), self.shape), [])


class TestUnguardedHelperCall(unittest.TestCase):
    shape = "unguarded-access-to-lock-guarded-state"

    # Every public method goes through the lock before calling the helper,
    # the state lock, except one that calls the helper bare.
    _SRC = """
    import threading

    class Store:
        def __init__(self):
            self._state = threading.RLock()
            self._index = None

        def _ensure_index(self):
            return self._index

        def a(self):
            with self._state:
                return self._ensure_index()

        def b(self):
            with self._state:
                return self._ensure_index()

        def c(self):
            with self._state:
                return self._ensure_index()

        def charge(self):
            return self._ensure_index()
    """

    def test_one_bare_caller_among_locked_ones(self):
        found = of(scan(self._SRC), self.shape)
        self.assertEqual([f["function"] for f in found], ["Store.charge"])
        self.assertIn("3 of its 4 call sites", found[0]["message"])

    def test_static_helper_needs_no_lock(self):
        src = self._SRC.replace(
            "def _ensure_index(self):\n            return self._index",
            "@staticmethod\n        def _ensure_index():\n            return 1",
        )
        self.assertEqual(of(scan(src), self.shape), [])


class TestCheckThenAct(unittest.TestCase):
    shape = "check-then-act-without-lock"

    _SRC = """
    import threading

    class Pool:
        def __init__(self):
            self._lock = threading.Lock()
            self._conn = None

        def close(self):
            with self._lock:
                self._conn = None

        def reset(self):
            with self._lock:
                self._conn = None

        def conn(self):
            {body}
            return self._conn
    """

    def test_lazy_init_outside_lock(self):
        body = "if self._conn is None:\n                self._conn = object()"
        found = of(scan(self._SRC.format(body=body)), self.shape)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["confidence"], "medium")

    def test_double_checked_locking_is_silent(self):
        body = (
            "if self._conn is None:\n"
            "                with self._lock:\n"
            "                    if self._conn is None:\n"
            "                        self._conn = object()"
        )
        self.assertEqual(of(scan(self._SRC.format(body=body)), self.shape), [])

    def test_check_then_insert(self):
        src = """
        import threading
        _lock = threading.Lock()
        _cache = {}

        def put(k, v):
            with _lock:
                _cache[k] = v

        def drop(k):
            with _lock:
                _cache.pop(k, None)

        def get_or_build(k):
            if k not in _cache:
                _cache[k] = object()
            return _cache[k]
        """
        found = of(scan(src), self.shape)
        self.assertEqual(len(found), 1)
        self.assertIn("check-then-insert", found[0]["message"])

    def test_class_without_any_lock_is_silent(self):
        src = """
        class Lazy:
            def __init__(self):
                self._x = None

            def x(self):
                if self._x is None:
                    self._x = 1
                return self._x
        """
        self.assertEqual(of(scan(src), self.shape), [])


class TestAcquireDiscipline(unittest.TestCase):
    def test_release_only_on_success_path(self):
        src = """
        import threading
        lock = threading.Lock()

        def work():
            lock.acquire()
            do_something()
            lock.release()
        """
        found = of(scan(src), "acquire-without-finally-release")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["confidence"], "high")

    def test_try_finally_release_is_silent(self):
        src = """
        import threading
        lock = threading.Lock()

        def work():
            lock.acquire()
            try:
                do_something()
            finally:
                lock.release()
        """
        self.assertEqual(of(scan(src), "acquire-without-finally-release"), [])

    def test_enter_exit_protocol_is_silent(self):
        src = """
        import threading

        class Guard:
            def __init__(self):
                self._lock = threading.Lock()

            def __enter__(self):
                self._lock.acquire()
                return self

            def __exit__(self, *exc):
                self._lock.release()
        """
        self.assertEqual(of(scan(src), "acquire-without-finally-release"), [])

    def test_timeout_result_ignored(self):
        src = """
        import threading
        lock = threading.Lock()

        def work():
            lock.acquire(timeout=5)
            try:
                do_something()
            finally:
                lock.release()
        """
        found = of(scan(src), "lock-acquire-result-ignored")
        self.assertEqual(len(found), 1)

    def test_timeout_result_checked_is_silent(self):
        src = """
        import threading
        lock = threading.Lock()

        def work():
            if not lock.acquire(timeout=5):
                raise TimeoutError
            try:
                do_something()
            finally:
                lock.release()
        """
        self.assertEqual(of(scan(src), "lock-acquire-result-ignored"), [])

    def test_blocking_true_is_not_a_timeout(self):
        src = """
        import threading
        lock = threading.Lock()

        def work():
            lock.acquire(True)
            try:
                do_something()
            finally:
                lock.release()
        """
        self.assertEqual(of(scan(src), "lock-acquire-result-ignored"), [])


class TestLockOrder(unittest.TestCase):
    shape = "lock-order-inversion"

    def test_opposite_nesting_orders(self):
        src = """
        import threading
        a = threading.Lock()
        b = threading.Lock()

        def one():
            with a:
                with b:
                    pass

        def two():
            with b:
                with a:
                    pass
        """
        found = of(scan(src), self.shape)
        self.assertEqual(len(found), 1)
        self.assertIn("a -> b -> a", found[0]["message"])

    def test_cycle_through_a_call(self):
        src = """
        import threading

        class Store:
            def __init__(self):
                self._meta = threading.Lock()
                self._data = threading.Lock()

            def write(self):
                with self._meta:
                    self._touch_data()

            def _touch_data(self):
                with self._data:
                    pass

            def compact(self):
                with self._data:
                    with self._meta:
                        pass
        """
        found = of(scan(src), self.shape)
        self.assertEqual(len(found), 1)
        self.assertTrue(any("_touch_data" in step for step in found[0]["chain"]))

    def test_consistent_order_is_silent(self):
        src = """
        import threading
        a = threading.Lock()
        b = threading.Lock()

        def one():
            with a:
                with b:
                    pass

        def two():
            with a, b:
                pass
        """
        self.assertEqual(of(scan(src), self.shape), [])


class TestReentry(unittest.TestCase):
    shape = "non-reentrant-lock-reacquired"

    _SRC = """
    import threading

    class Counter:
        def __init__(self):
            self._lock = threading.{kind}()
            self._n = 0

        def add(self):
            with self._lock:
                self._n += 1
                self._log()

        def _log(self):
            with self._lock:
                print(self._n)
    """

    def test_plain_lock_reacquired_through_call(self):
        found = of(scan(self._SRC.format(kind="Lock")), self.shape)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["confidence"], "high")

    def test_rlock_is_silent(self):
        self.assertEqual(of(scan(self._SRC.format(kind="RLock")), self.shape), [])

    def test_read_to_write_upgrade_on_project_rw_lock(self):
        src = """
        import threading
        from contextlib import contextmanager
        from threading import get_ident

        class RWLock:
            def __init__(self):
                self._cond = threading.Condition(threading.Lock())
                self._readers = {}

            @contextmanager
            def read_lock(self):
                tid = get_ident()
                with self._cond:
                    self._readers[tid] = 1
                try:
                    yield
                finally:
                    with self._cond:
                        self._readers.pop(tid)
                        self._cond.notify_all()

            @contextmanager
            def write_lock(self):
                with self._cond:
                    while self._readers:
                        self._cond.wait()
                yield

        class Store:
            def __init__(self):
                self._state = RWLock()

            def query(self):
                with self._state.read_lock():
                    self._repair()

            def _repair(self):
                with self._state.write_lock():
                    pass

            def nested_read(self):
                with self._state.read_lock():
                    with self._state.read_lock():
                        pass
        """
        found = of(scan(src), self.shape)
        # The upgrade is flagged; read-inside-read on an owner-tracking
        # (reentrant) lock is not.
        self.assertEqual([f["function"] for f in found], ["Store.query"])
        self.assertIn("upgrade", found[0]["message"])

    def test_other_instance_is_not_reentry(self):
        src = """
        import threading

        class Node:
            def __init__(self, parent=None):
                self._lock = threading.Lock()
                self.parent: "Node | None" = parent

            def bump(self):
                with self._lock:
                    if self.parent is not None:
                        self.parent.bump_one()

            def bump_one(self):
                with self._lock:
                    pass
        """
        self.assertEqual(of(scan(src), self.shape), [])


class TestNestedLockContextManagers(unittest.TestCase):
    """A @contextmanager that enters another lock context manager holds its lock."""

    _NESTED = """
    import threading
    from contextlib import contextmanager

    class Pool:
        def __init__(self, model):
            self._lock = threading.{kind}()
            self.model = model

        @contextmanager
        def _guards(self, *keys):
            with self._lock:
                yield

        @contextmanager
        def _guard(self, key):
            with self._guards(key):
                yield

        @contextmanager
        def _outer_guard(self, key):
            with self._guard(key):
                yield

        def job(self, texts):
            with self._guard("k"):
                return self.model.embed_many(texts)

        def deep_job(self, texts):
            with self._outer_guard("k"):
                return self.model.embed_many(texts)
    """

    def test_nested_cm_is_a_lock_cm(self):
        result = scan_project(
            {"pkg/mod.py": self._NESTED.format(kind="Lock")}, blocking=["embed_many"]
        )
        cms = result["summary"]["lock_context_managers"]
        self.assertEqual(set(cms), {"Pool._guards", "Pool._guard", "Pool._outer_guard"})
        found = of(result["findings"], "blocking-call-under-lock")
        self.assertEqual(
            sorted(f["function"] for f in found), ["Pool.deep_job", "Pool.job"]
        )
        self.assertEqual(
            result["summary"]["lock_context_managers_wrapping"]["Pool._guard"],
            "self._guards(key)",
        )
        self.assertEqual(result["summary"]["possible_lock_cms_missed"], [])

    def test_wrapper_and_wrapped_cm_are_one_guard(self):
        # Most callers take the single-key wrapper; one takes the multi-key CM
        # it wraps. Both hold the same lock, so the helper is guarded everywhere.
        src = """
        import threading
        from contextlib import contextmanager

        class Shelf:
            def __init__(self):
                self._lock = threading.Lock()
                self._items = {}

            @contextmanager
            def _guards(self, *keys):
                with self._lock:
                    yield

            @contextmanager
            def _guard(self, key):
                with self._guards(key):
                    yield

            def _load(self, key):
                return self._items.get(key)

            def a(self, k):
                with self._guard(k):
                    return self._load(k)

            def b(self, k):
                with self._guard(k):
                    return self._load(k)

            def c(self, k):
                with self._guard(k):
                    return self._load(k)

            def move(self, k1, k2):
                with self._guards(k1, k2):
                    return self._load(k1)
        """
        found = of(scan(src), "unguarded-access-to-lock-guarded-state")
        self.assertEqual(found, [])

    def test_cm_over_a_non_lock_cm_is_not_a_lock(self):
        src = """
        from contextlib import contextmanager

        class Timer:
            @contextmanager
            def _timed(self, op):
                yield

            @contextmanager
            def _scope(self, op):
                with self._timed(op):
                    yield

            @contextmanager
            def _opened(self, path):
                with open(path) as fh:
                    yield fh

            def run(self, model):
                with self._scope("x"):
                    model.embed_many([])
        """
        result = scan_project({"pkg/mod.py": src}, blocking=["embed_many"])
        self.assertEqual(result["summary"]["lock_context_managers"], [])
        self.assertEqual(of(result["findings"], "blocking-call-under-lock"), [])
        # Unresolved CMs with a `with` around `yield` are listed for a manual check.
        missed = result["summary"]["possible_lock_cms_missed"]
        self.assertEqual(len(missed), 2)
        self.assertTrue(any(m.startswith("Timer._scope: with") for m in missed))


class TestLockContextManagerKind(unittest.TestCase):
    """A CM-derived lock inherits the reentrancy of the lock it wraps."""

    shape = "non-reentrant-lock-reacquired"

    _READING = """
    import threading
    from contextlib import contextmanager

    class Table:
        def __init__(self):
            self._read_mutex = threading.{kind}()
            self._rows = {{}}

        @contextmanager
        def _reading(self):
            self._read_mutex.acquire()
            try:
                yield self._rows
            finally:
                self._read_mutex.release()

        def get_many(self, keys):
            with self._reading():
                return [self.get(k) for k in keys]

        def get(self, key):
            with self._reading() as rows:
                return rows.get(key)
    """

    def test_reader_over_rlock_is_silent(self):
        self.assertEqual(of(scan(self._READING.format(kind="RLock")), self.shape), [])

    def test_reader_over_plain_lock_is_flagged_as_non_reentrant(self):
        result = scan_project({"pkg/mod.py": self._READING.format(kind="Lock")})
        found = of(result["findings"], self.shape)
        self.assertEqual(len(found), 1)
        self.assertIn("non-reentrant Lock", found[0]["message"])
        self.assertEqual(
            result["summary"]["lock_context_manager_kinds"]["Table._reading"], "Lock"
        )

    _WRITING = """
    import threading
    from contextlib import contextmanager

    class Table:
        def __init__(self):
            self._write_mutex = threading.{kind}()
            self._rows = {{}}

        @contextmanager
        def _writing_with(self, lock):
            lock.acquire()
            try:
                yield self._rows
            finally:
                lock.release()

        @contextmanager
        def _writing(self):
            with self._writing_with(self._write_mutex) as rows:
                yield rows

        def put_many(self, items):
            with self._writing():
                for k, v in items:
                    self.put(k, v)

        def put(self, key, value):
            with self._writing() as rows:
                rows[key] = value
    """

    def test_nested_writer_over_lock_parameter(self):
        found = of(scan(self._WRITING.format(kind="Lock")), self.shape)
        self.assertEqual([f["function"] for f in found], ["Table.put_many"])
        self.assertEqual(of(scan(self._WRITING.format(kind="RLock")), self.shape), [])

    def test_cm_holding_a_reentrant_and_a_plain_lock_is_not_reentrant(self):
        src = """
        import threading
        from contextlib import contextmanager

        class Box:
            def __init__(self):
                self._outer = threading.RLock()
                self._inner = threading.Lock()

            @contextmanager
            def _both(self):
                with self._outer, self._inner:
                    yield

            def a(self):
                with self._both():
                    self.b()

            def b(self):
                with self._both():
                    pass
        """
        result = scan_project({"pkg/mod.py": src})
        self.assertEqual(
            result["summary"]["lock_context_manager_kinds"]["Box._both"], "Lock"
        )
        self.assertEqual(len(of(result["findings"], self.shape)), 1)

    def test_cm_with_an_unresolved_item_is_not_taken_as_reentrant(self):
        # The ExitStack may enter a non-reentrant lock the scanner cannot see.
        src = """
        import threading
        from contextlib import ExitStack, contextmanager

        class Box:
            def __init__(self):
                self._outer = threading.RLock()

            @contextmanager
            def _all(self, items):
                with self._outer, ExitStack() as stack:
                    for item in items:
                        stack.enter_context(item.write_lock())
                    yield
        """
        result = scan_project({"pkg/mod.py": src})
        self.assertEqual(
            result["summary"]["lock_context_manager_kinds"]["Box._all"], "custom"
        )


class TestCallResolution(unittest.TestCase):
    """Two classes share a method name: resolve to the receiver's class, and say
    so in the chain when only the name was used."""

    def test_local_provenance_avoids_a_false_lock_cycle(self):
        src = """
        import threading

        class Registry:
            def __init__(self):
                self._lock = threading.Lock()

            def get_config(self, name):
                with self._lock:
                    return {}

        class Store:
            def __init__(self, registry=None):
                self._state = threading.Lock()
                self._guard = threading.Lock()
                self._registry = registry or Registry()

            def get_config(self, name):
                with self._state:
                    return self._bootstrap()

            def _bootstrap(self):
                registry = self._registry
                with self._guard:
                    return registry.get_config("x")
        """
        self.assertEqual(of(scan(src), "lock-order-inversion"), [])

    _SHARED = """
    import threading, time
    from typing import Protocol

    class ShelfBase(Protocol):
        def metrics(self) -> dict: ...

    class DiskShelf{base}:
        def metrics(self):
            time.sleep(1)
            return {{}}

    class Registry:
        def metrics(self):
            return {{}}

    lock = threading.Lock()

    def refresh({param}):
        with lock:
            return shelf.metrics()
    """

    def _found(self, base: str, param: str) -> list[dict]:
        src = self._SHARED.format(base=base, param=param)
        return of(scan(src), "blocking-call-under-lock")

    def test_receiver_name_picks_the_class_and_hop_is_marked_weak(self):
        found = self._found("", "shelf")
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0]["chain"][0].endswith("[weak: name-only]"))
        self.assertIn("DiskShelf.metrics", found[0]["chain"][1])

    def test_ambiguous_hop_names_its_candidate_count(self):
        src = (
            self._SHARED.format(base="", param="shelf")
            .replace("shelf.metrics()", "thing.metrics()")
            .replace("def refresh(shelf)", "def refresh(thing)")
        )
        found = of(scan(src), "blocking-call-under-lock")
        self.assertEqual(len(found), 1)
        self.assertIn("[weak: name-only, 1 of 2 candidates]", found[0]["chain"][0])

    def test_protocol_annotation_resolves_to_its_one_implementation(self):
        found = self._found("(ShelfBase)", "shelf: ShelfBase")
        self.assertEqual(len(found), 1)
        self.assertNotIn("[weak", " ".join(found[0]["chain"]))
        self.assertIn("DiskShelf.metrics", found[0]["chain"][1])


class TestConditions(unittest.TestCase):
    def test_wait_without_predicate_loop(self):
        src = """
        import threading

        class Q:
            def __init__(self):
                self._cv = threading.Condition()
                self._items = []

            def take(self):
                with self._cv:
                    if not self._items:
                        self._cv.wait()
                    return self._items.pop()
        """
        self.assertEqual(len(of(scan(src), "condition-wait-outside-predicate-loop")), 1)

    def test_wait_in_while_is_silent(self):
        src = """
        import threading

        class Q:
            def __init__(self):
                self._cv = threading.Condition()
                self._items = []

            def take(self):
                with self._cv:
                    while not self._items:
                        self._cv.wait()
                    return self._items.pop()
        """
        self.assertEqual(of(scan(src), "condition-wait-outside-predicate-loop"), [])

    def test_event_wait_is_not_a_condition(self):
        src = """
        import threading

        def go(evt: threading.Event):
            evt.wait()
        """
        self.assertEqual(of(scan(src), "condition-wait-outside-predicate-loop"), [])

    def test_notify_without_lock(self):
        src = """
        import threading

        class Q:
            def __init__(self):
                self._cv = threading.Condition()

            def poke(self):
                self._cv.notify_all()

            def poke_ok(self):
                with self._cv:
                    self._cv.notify_all()
        """
        found = of(scan(src), "condition-notify-without-lock")
        self.assertEqual([f["function"] for f in found], ["Q.poke"])


class TestBlockingUnderLock(unittest.TestCase):
    shape = "blocking-call-under-lock"

    def test_sleep_under_lock(self):
        src = """
        import threading, time
        lock = threading.Lock()

        def retry():
            with lock:
                time.sleep(1)
        """
        found = of(scan(src), self.shape)
        self.assertEqual(len(found), 1)
        self.assertEqual(
            (found[0]["severity"], found[0]["confidence"]), ("FIX", "high")
        )

    def test_through_a_call_chain_across_modules(self):
        files = {
            "pkg/net.py": """
            import requests

            def fetch(url):
                return requests.get(url)
            """,
            "pkg/svc.py": """
            import threading
            from pkg.net import fetch

            _lock = threading.Lock()

            def refresh():
                with _lock:
                    return fetch("http://x")
            """,
        }
        found = of(scan_project(files)["findings"], self.shape)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["file"], "pkg/svc.py")
        self.assertEqual(len(found[0]["chain"]), 2)

    def test_work_outside_the_lock_is_silent(self):
        src = """
        import threading, time
        lock = threading.Lock()
        state = {}

        def refresh():
            time.sleep(1)
            with lock:
                state["x"] = 1
        """
        self.assertEqual(of(scan(src), self.shape), [])

    def test_condition_wait_on_the_held_condition_is_silent(self):
        src = """
        import threading

        class Q:
            def __init__(self):
                self._cv = threading.Condition()

            def take(self):
                with self._cv:
                    while True:
                        self._cv.wait()
        """
        self.assertEqual(of(scan(src), self.shape), [])

    def test_str_join_is_not_a_thread_join(self):
        src = """
        import threading
        lock = threading.Lock()

        def render(parts):
            with lock:
                return ", ".join(parts)
        """
        self.assertEqual(of(scan(src), self.shape), [])

    def test_extra_blocking_names(self):
        src = """
        import threading
        lock = threading.Lock()

        def go(model):
            with lock:
                model.embed_many([])
        """
        self.assertEqual(of(scan(src), self.shape), [])
        found = of(scan(src, blocking=["embed_many"]), self.shape)
        self.assertEqual(len(found), 1)


class TestAwaitUnderLock(unittest.TestCase):
    shape = "await-while-holding-thread-lock"

    def test_await_inside_sync_with(self):
        src = """
        import asyncio, threading
        lock = threading.Lock()

        async def handler():
            with lock:
                await asyncio.sleep(0)
        """
        found = of(scan(src), self.shape)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["confidence"], "high")

    def test_async_with_asyncio_lock_is_silent(self):
        src = """
        import asyncio
        lock = asyncio.Lock()

        async def handler():
            async with lock:
                await asyncio.sleep(0)
        """
        self.assertEqual(of(scan(src), self.shape), [])


class TestTransitiveBlockingInAsync(unittest.TestCase):
    shape = "transitive-blocking-call-in-async"

    # Two periodic tasks; one offloads its sync call, the
    # other calls a sync function that waits for a lock held across slow work.
    _FILES = {
        "pkg/store.py": """
            import threading, time

            class Store:
                def __init__(self):
                    self._state = threading.Lock()

                def rebuild(self):
                    with self._state:
                        time.sleep(30)  # stands for a long index rebuild

                def flush_metrics(self):
                    with self._state:
                        pass

                def refresh(self):
                    return 1
            """,
        "pkg/app.py": """
            import asyncio
            from pkg.store import Store

            async def flush_periodically(store: Store):
                while True:
                    await asyncio.sleep(10)
                    {flush}

            async def refresh_periodically(store: Store):
                loop = asyncio.get_running_loop()
                while True:
                    await loop.run_in_executor(None, store.refresh)
            """,
    }

    def _scan(self, flush: str) -> list[dict]:
        files = dict(self._FILES)
        files["pkg/app.py"] = files["pkg/app.py"].replace("{flush}", flush)
        return of(scan_project(files)["findings"], self.shape)

    def test_unguarded_twin_is_high(self):
        found = self._scan("store.flush_metrics()")
        self.assertEqual(len(found), 1)
        f = found[0]
        self.assertEqual(f["function"], "flush_periodically")
        self.assertEqual(f["confidence"], "high")
        self.assertIn("unguarded twin", f["detail"])
        self.assertTrue(
            any("meanwhile" in step and "rebuild" in step for step in f["chain"])
        )

    def test_offloaded_twin_is_silent(self):
        self.assertEqual(
            self._scan("await loop.run_in_executor(None, store.flush_metrics)"), []
        )

    def test_direct_blocking_call_through_helper(self):
        src = """
        import asyncio, subprocess

        def build():
            subprocess.run(["make"])

        async def handler():
            build()
        """
        found = of(scan(src), self.shape)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["confidence"], "medium")

    def test_closure_run_in_executor_is_not_on_the_loop(self):
        src = """
        import asyncio, time

        async def handler():
            def work():
                time.sleep(1)
            await asyncio.to_thread(work)
        """
        self.assertEqual(of(scan(src), self.shape), [])

    def test_quick_lock_is_low(self):
        src = """
        import threading
        _lock = threading.Lock()
        _n = 0

        def inc():
            global _n
            with _lock:
                _n += 1

        async def handler():
            inc()
        """
        found = of(scan(src), self.shape)
        self.assertEqual([f["confidence"] for f in found], ["low"])


class TestSignalHandlers(unittest.TestCase):
    shape = "signal-handler-does-real-work"

    def test_handler_that_logs_and_locks(self):
        src = """
        import logging, signal, threading
        log = logging.getLogger(__name__)
        _lock = threading.Lock()

        def on_term(signum, frame):
            with _lock:
                log.info("stopping")

        def install():
            signal.signal(signal.SIGTERM, on_term)
        """
        found = of(scan(src), self.shape)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["severity"], "FIX")

    def test_flag_only_handler_is_silent(self):
        src = """
        import signal
        stop = False

        def on_term(signum, frame):
            global stop
            stop = True

        def install():
            signal.signal(signal.SIGTERM, on_term)
        """
        self.assertEqual(of(scan(src), self.shape), [])


class TestEnvelopeAndOptions(unittest.TestCase):
    def test_envelope_and_summary(self):
        result = scan_project({"pkg/mod.py": "x = 1\n"})
        for key in (
            "project_root",
            "scan_root",
            "files_total",
            "files_analyzed",
            "files_capped",
        ):
            self.assertIn(key, result)
        self.assertEqual(result["summary"]["total_findings"], 0)
        self.assertEqual(set(result["summary"]["checks_run"]), set(mod._CHECKS))

    def test_check_filter(self):
        src = """
        import threading, time
        lock = threading.Lock()

        def a():
            with lock:
                time.sleep(1)
            lock.acquire()
            lock.release()
        """
        only = scan(src, checks=["acquire-without-finally-release"])
        self.assertEqual(
            {f["shape"] for f in only}, {"acquire-without-finally-release"}
        )

    def test_unparseable_file_is_skipped(self):
        result = scan_project({"pkg/bad.py": "def (:\n", "pkg/ok.py": "x = 1\n"})
        self.assertEqual(result["files_analyzed"], 2)
        self.assertEqual(result["findings"], [])

    def test_every_check_is_a_catalogued_shape(self):
        shapes = import_script("scan_common").load_data("python_bug_shapes.json")[
            "shapes"
        ]
        catalog = {s["id"]: s for s in shapes}
        for check in mod._CHECKS:
            with self.subTest(check=check):
                self.assertIn(check, catalog)
                self.assertEqual(catalog[check]["agent"], "concurrency-auditor")
                self.assertEqual(catalog[check]["detected_by"], "scan_concurrency.py")


if __name__ == "__main__":
    unittest.main()
