"""Tests for scan_python_pitfalls.py.

Each shape gets a true positive, its guarded twin (which must stay silent), and
an edge case. The guarded-twin assertions matter most: a scanner that fires on
the documented fix is worse than no scanner.
"""

import ast
import unittest

from helpers import TempProject, import_script

mod = import_script("scan_python_pitfalls")


def scan(source: str, **kw) -> list[dict]:
    """Scan a single module and return its findings."""
    with TempProject({"mod.py": source}) as root:
        return mod.analyze(str(root / "mod.py"), **kw)["findings"]


def shapes(source: str) -> list[str]:
    return [f["shape"] for f in scan(source)]


class TestMutableDefault(unittest.TestCase):
    def test_list_default_mutated(self):
        f = scan("def f(x, items=[]):\n    items.append(x)\n")
        self.assertEqual(f[0]["shape"], "mutable-default-argument")
        self.assertEqual(f[0]["confidence"], "high")

    def test_dict_and_set_defaults(self):
        self.assertIn(
            "mutable-default-argument", shapes("def f(a={}):\n    a['k']=1\n")
        )
        self.assertIn(
            "mutable-default-argument", shapes("def f(a=set()):\n    a.add(1)\n")
        )

    def test_evaluated_once_call(self):
        src = "import datetime\ndef f(now=datetime.datetime.now()):\n    return now\n"
        self.assertIn("mutable-default-argument", shapes(src))

    def test_readonly_default_downgraded(self):
        f = scan("def f(opts={}):\n    print(opts)\n")
        self.assertEqual(f[0]["confidence"], "medium")

    def test_returned_default_is_high(self):
        f = scan("def f(items=[]):\n    return items\n")
        self.assertEqual(f[0]["confidence"], "high")

    def test_keyword_only_default(self):
        self.assertIn(
            "mutable-default-argument", shapes("def f(*, x=[]):\n    x.append(1)\n")
        )

    def test_none_sentinel_is_silent(self):
        src = "def f(x, items=None):\n    items = [] if items is None else items\n    items.append(x)\n"
        self.assertNotIn("mutable-default-argument", shapes(src))

    def test_immutable_defaults_silent(self):
        src = "def f(a=1, b='s', c=(), d=None, e=frozenset()):\n    return a\n"
        self.assertNotIn("mutable-default-argument", shapes(src))


class TestLateBindingClosure(unittest.TestCase):
    def test_lambda_in_loop(self):
        src = "def f():\n    out = []\n    for i in range(3):\n        out.append(lambda: i)\n    return out\n"
        self.assertIn("late-binding-closure-in-loop", shapes(src))

    def test_nested_def_in_loop(self):
        src = "def f(names):\n    for n in names:\n        def go():\n            return n\n        register(go)\n"
        self.assertIn("late-binding-closure-in-loop", shapes(src))

    def test_default_arg_binding_is_silent(self):
        src = "def f():\n    return [lambda i=i: i for i in range(3)]\n"
        self.assertNotIn("late-binding-closure-in-loop", shapes(src))

    def test_closure_not_using_loop_var_is_silent(self):
        src = "def f(k):\n    out = []\n    for i in range(3):\n        out.append(lambda: k)\n    return out\n"
        self.assertNotIn("late-binding-closure-in-loop", shapes(src))


class TestExceptOrdering(unittest.TestCase):
    def test_broad_before_narrow(self):
        src = "def f():\n    try:\n        pass\n    except Exception:\n        pass\n    except ValueError:\n        pass\n"
        # The same fixture also legitimately triggers except-exception-too-broad,
        # so select by shape rather than by position.
        f = [x for x in scan(src) if x["shape"] == "except-clause-ordering-unreachable"]
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0]["confidence"], "high")

    def test_oserror_before_filenotfound(self):
        src = "def f():\n    try:\n        pass\n    except OSError:\n        pass\n    except FileNotFoundError:\n        pass\n"
        self.assertIn("except-clause-ordering-unreachable", shapes(src))

    def test_correct_order_is_silent(self):
        src = "def f():\n    try:\n        pass\n    except ValueError:\n        pass\n    except Exception:\n        pass\n"
        self.assertNotIn("except-clause-ordering-unreachable", shapes(src))

    def test_unrelated_types_silent(self):
        src = "def f():\n    try:\n        pass\n    except ValueError:\n        pass\n    except KeyError:\n        pass\n"
        self.assertNotIn("except-clause-ordering-unreachable", shapes(src))

    def test_user_defined_exceptions_not_guessed(self):
        # Hierarchy is unknowable statically -- must not fabricate a finding.
        src = "def f():\n    try:\n        pass\n    except MyBase:\n        pass\n    except MySub:\n        pass\n"
        self.assertNotIn("except-clause-ordering-unreachable", shapes(src))


class TestReturnInFinally(unittest.TestCase):
    def test_return_in_finally(self):
        src = "def f():\n    try:\n        raise ValueError\n    finally:\n        return 1\n"
        self.assertIn("return-or-break-in-finally", shapes(src))

    def test_break_in_finally(self):
        src = "def f():\n    for _ in range(3):\n        try:\n            pass\n        finally:\n            break\n"
        self.assertIn("return-or-break-in-finally", shapes(src))

    def test_plain_finally_is_silent(self):
        src = "def f():\n    try:\n        return compute()\n    finally:\n        release()\n"
        self.assertNotIn("return-or-break-in-finally", shapes(src))

    def test_return_in_nested_def_is_silent(self):
        # The return belongs to the inner function, not to the finally.
        src = "def f():\n    try:\n        pass\n    finally:\n        def helper():\n            return 1\n        helper()\n"
        self.assertNotIn("return-or-break-in-finally", shapes(src))


class TestEqWithoutHash(unittest.TestCase):
    def test_eq_only(self):
        self.assertIn(
            "eq-without-hash",
            shapes("class C:\n    def __eq__(self, o):\n        return True\n"),
        )

    def test_eq_and_hash_silent(self):
        src = "class C:\n    def __eq__(self, o):\n        return True\n    def __hash__(self):\n        return 1\n"
        self.assertNotIn("eq-without-hash", shapes(src))

    def test_frozen_dataclass_silent(self):
        src = "from dataclasses import dataclass\n@dataclass(frozen=True)\nclass C:\n    x: int\n    def __eq__(self, o):\n        return True\n"
        self.assertNotIn("eq-without-hash", shapes(src))

    def test_explicit_hash_none_silent(self):
        src = "class C:\n    __hash__ = None\n    def __eq__(self, o):\n        return True\n"
        self.assertNotIn("eq-without-hash", shapes(src))


class TestMutationDuringIteration(unittest.TestCase):
    def test_del_during_dict_iteration(self):
        self.assertIn(
            "mutation-during-iteration",
            shapes("def f(d):\n    for k in d:\n        del d[k]\n"),
        )

    def test_remove_during_list_iteration(self):
        self.assertIn(
            "mutation-during-iteration",
            shapes("def f(lst):\n    for x in lst:\n        lst.remove(x)\n"),
        )

    def test_iterating_items_view(self):
        self.assertIn(
            "mutation-during-iteration",
            shapes("def f(d):\n    for k, v in d.items():\n        d.pop(k)\n"),
        )

    def test_snapshot_is_silent(self):
        self.assertNotIn(
            "mutation-during-iteration",
            shapes("def f(d):\n    for k in list(d):\n        del d[k]\n"),
        )

    def test_slice_copy_is_silent(self):
        self.assertNotIn(
            "mutation-during-iteration",
            shapes("def f(lst):\n    for x in lst[:]:\n        lst.remove(x)\n"),
        )

    def test_rewriting_existing_key_is_silent(self):
        # d[k] = v with k from the iteration rewrites an existing entry: the
        # size is unchanged, so CPython does not raise. Observed on idlelib,
        # where all four raw hits were this safe pattern.
        self.assertNotIn(
            "mutation-during-iteration",
            shapes("def f(d):\n    for k in d:\n        d[k] = transform(d[k])\n"),
        )

    def test_rewriting_via_items_is_silent(self):
        src = "def f(d):\n    for k, v in d.items():\n        d[k] = v + 1\n"
        self.assertNotIn("mutation-during-iteration", shapes(src))

    def test_inserting_a_new_key_is_flagged(self):
        # A different key can insert -> size change -> RuntimeError.
        self.assertIn(
            "mutation-during-iteration",
            shapes("def f(d):\n    for k in d:\n        d[k + '_x'] = 1\n"),
        )

    def test_list_index_assignment_is_silent(self):
        src = "def f(lst):\n    for i, x in enumerate(lst):\n        lst[i] = x * 2\n"
        self.assertNotIn("mutation-during-iteration", shapes(src))

    def test_mutating_a_different_container_silent(self):
        self.assertNotIn(
            "mutation-during-iteration",
            shapes("def f(a, b):\n    for x in a:\n        b.append(x)\n"),
        )


class TestFireAndForgetTask(unittest.TestCase):
    def test_bare_create_task(self):
        src = "import asyncio\nasync def f():\n    asyncio.create_task(go())\n"
        self.assertIn("asyncio-fire-and-forget-task", shapes(src))

    def test_retained_task_is_silent(self):
        src = "import asyncio\nasync def f(tasks):\n    t = asyncio.create_task(go())\n    tasks.add(t)\n"
        self.assertNotIn("asyncio-fire-and-forget-task", shapes(src))

    def test_awaited_is_silent(self):
        src = "import asyncio\nasync def f():\n    await asyncio.create_task(go())\n"
        self.assertNotIn("asyncio-fire-and-forget-task", shapes(src))


class TestBlockingInAsync(unittest.TestCase):
    def test_time_sleep(self):
        src = "import time\nasync def f():\n    time.sleep(1)\n"
        self.assertIn("blocking-call-in-async-function", shapes(src))

    def test_requests_get(self):
        src = "import requests\nasync def f():\n    requests.get('u')\n"
        self.assertIn("blocking-call-in-async-function", shapes(src))

    def test_asyncio_sleep_is_silent(self):
        src = "import asyncio\nasync def f():\n    await asyncio.sleep(1)\n"
        self.assertNotIn("blocking-call-in-async-function", shapes(src))

    def test_blocking_in_sync_function_is_silent(self):
        src = "import time\ndef f():\n    time.sleep(1)\n"
        self.assertNotIn("blocking-call-in-async-function", shapes(src))

    def test_nested_sync_def_offloaded_to_executor_is_silent(self):
        # The recommended fix itself: the closure runs in a worker thread.
        src = (
            "import asyncio, time\n"
            "async def f():\n"
            "    def work():\n"
            "        time.sleep(1)\n"
            "    await asyncio.get_running_loop().run_in_executor(None, work)\n"
        )
        self.assertNotIn("blocking-call-in-async-function", shapes(src))

    def test_lambda_offloaded_to_executor_is_silent(self):
        src = (
            "import asyncio, time\n"
            "async def f(loop):\n"
            "    await loop.run_in_executor(None, lambda: time.sleep(1))\n"
        )
        self.assertNotIn("blocking-call-in-async-function", shapes(src))

    def test_direct_call_still_found_beside_nested_def(self):
        src = (
            "import time\n"
            "async def f():\n"
            "    def work():\n"
            "        time.sleep(1)\n"
            "    time.sleep(2)\n"
        )
        hits = [x for x in scan(src) if x["shape"] == "blocking-call-in-async-function"]
        self.assertEqual([x["line"] for x in hits], [5])

    def test_nested_async_def_reported_once(self):
        src = (
            "import time\n"
            "async def outer():\n"
            "    async def inner():\n"
            "        time.sleep(1)\n"
            "    await inner()\n"
        )
        hits = [x for x in scan(src) if x["shape"] == "blocking-call-in-async-function"]
        self.assertEqual(len(hits), 1)
        self.assertIn("inner()", hits[0]["message"])


class TestUnawaitedCoroutine(unittest.TestCase):
    def test_bare_call_to_local_coroutine(self):
        src = "async def work():\n    return 1\nasync def caller():\n    work()\n"
        self.assertIn("unawaited-coroutine", shapes(src))

    def test_awaited_is_silent(self):
        src = "async def work():\n    return 1\nasync def caller():\n    await work()\n"
        self.assertNotIn("unawaited-coroutine", shapes(src))

    def test_gathered_is_silent(self):
        src = "import asyncio\nasync def work():\n    return 1\nasync def caller():\n    await asyncio.gather(work(), work())\n"
        self.assertNotIn("unawaited-coroutine", shapes(src))

    def test_sync_function_call_is_silent(self):
        src = "def work():\n    return 1\nasync def caller():\n    work()\n"
        self.assertNotIn("unawaited-coroutine", shapes(src))


class TestLruCacheOnMethod(unittest.TestCase):
    def test_lru_cache_with_self(self):
        src = "import functools\nclass C:\n    @functools.lru_cache\n    def m(self, k):\n        return k\n"
        self.assertIn("lru-cache-on-method", shapes(src))

    def test_cached_property_is_silent(self):
        src = "import functools\nclass C:\n    @functools.cached_property\n    def m(self):\n        return 1\n"
        self.assertNotIn("lru-cache-on-method", shapes(src))

    def test_module_level_function_is_silent(self):
        src = "import functools\n@functools.lru_cache\ndef f(k):\n    return k\n"
        self.assertNotIn("lru-cache-on-method", shapes(src))

    def test_staticmethod_is_silent(self):
        src = "import functools\nclass C:\n    @staticmethod\n    @functools.lru_cache\n    def m(k):\n        return k\n"
        self.assertNotIn("lru-cache-on-method", shapes(src))


class TestClassLevelMutable(unittest.TestCase):
    def test_class_list(self):
        f = scan("class C:\n    items = []\n")
        self.assertEqual(f[0]["shape"], "class-level-mutable-attribute")

    def test_all_caps_unmutated_is_low(self):
        f = scan("class C:\n    DEFAULTS = {}\n")
        self.assertEqual(f[0]["confidence"], "low")

    def test_declarative_config_downgraded(self):
        # Never mutated in the module: the menu_specs/Meta pattern, seen
        # throughout idlelib. Real, but not a shared-state defect.
        f = scan("class C:\n    specs = [('a', 1), ('b', 2)]\n")
        self.assertEqual(f[0]["confidence"], "medium")

    def test_mutated_attribute_is_high(self):
        src = (
            "class C:\n    items = []\n"
            "    def add(self, x):\n        self.items.append(x)\n"
        )
        f = [x for x in scan(src) if x["shape"] == "class-level-mutable-attribute"]
        self.assertEqual(f[0]["confidence"], "high")

    def test_instance_attribute_is_silent(self):
        src = "class C:\n    def __init__(self):\n        self.items = []\n"
        self.assertNotIn("class-level-mutable-attribute", shapes(src))

    def test_dataclass_field_is_silent(self):
        src = "from dataclasses import dataclass, field\n@dataclass\nclass C:\n    items: list = field(default_factory=list)\n"
        self.assertNotIn("class-level-mutable-attribute", shapes(src))

    def test_immutable_class_attribute_is_silent(self):
        self.assertNotIn(
            "class-level-mutable-attribute", shapes("class C:\n    NAME = 'x'\n")
        )


class TestBareExcept(unittest.TestCase):
    def test_bare_except(self):
        f = scan("def f():\n    try:\n        pass\n    except:\n        pass\n")
        self.assertEqual(f[0]["shape"], "bare-except-swallows-control-flow")
        self.assertEqual(f[0]["confidence"], "high")

    def test_base_exception(self):
        src = "def f():\n    try:\n        pass\n    except BaseException:\n        pass\n"
        self.assertIn("bare-except-swallows-control-flow", shapes(src))

    def test_reraise_is_silent(self):
        src = "def f():\n    try:\n        pass\n    except BaseException:\n        raise\n"
        self.assertNotIn("bare-except-swallows-control-flow", shapes(src))

    def test_captured_for_caller_downgraded(self):
        # The thread/worker-body pattern: capture now, re-raise in the caller.
        src = "def f(errs):\n    try:\n        pass\n    except BaseException as e:\n        errs.append(e)\n"
        f = [x for x in scan(src) if x["shape"] == "bare-except-swallows-control-flow"]
        self.assertEqual(f[0]["confidence"], "medium")

    def test_earlier_systemexit_reraise_downgrades(self):
        # `except SystemExit: raise` then bare except -- the idlelib rpc.py idiom.
        src = (
            "def f():\n    try:\n        pass\n    except SystemExit:\n"
            "        raise\n    except:\n        log()\n"
        )
        f = [x for x in scan(src) if x["shape"] == "bare-except-swallows-control-flow"]
        self.assertEqual(f[0]["confidence"], "medium")
        self.assertIn("KeyboardInterrupt", f[0]["detail"])

    def test_all_control_flow_reraised_is_silent(self):
        src = (
            "def f():\n    try:\n        pass\n"
            "    except (SystemExit, KeyboardInterrupt, GeneratorExit):\n        raise\n"
            "    except:\n        log()\n"
        )
        self.assertNotIn("bare-except-swallows-control-flow", shapes(src))

    def test_except_exception_is_silent(self):
        src = "def f():\n    try:\n        pass\n    except Exception:\n        pass\n"
        self.assertNotIn("bare-except-swallows-control-flow", shapes(src))


class TestExceptionInDel(unittest.TestCase):
    def test_unguarded_del(self):
        src = "class C:\n    def __del__(self):\n        self.handle.close()\n"
        self.assertIn("exception-in-del-or-finalizer", shapes(src))

    def test_guarded_del_is_silent(self):
        src = "class C:\n    def __del__(self):\n        try:\n            self.handle.close()\n        except Exception:\n            pass\n"
        self.assertNotIn("exception-in-del-or-finalizer", shapes(src))

    def test_trivial_del_is_silent(self):
        src = "class C:\n    def __del__(self):\n        self.closed = True\n"
        self.assertNotIn("exception-in-del-or-finalizer", shapes(src))


class TestIsLiteral(unittest.TestCase):
    def test_is_int_literal(self):
        self.assertIn(
            "is-comparison-with-literal", shapes("def f(x):\n    return x is 256\n")
        )

    def test_is_str_literal(self):
        self.assertIn(
            "is-comparison-with-literal", shapes("def f(x):\n    return x is 'name'\n")
        )

    def test_is_none_is_silent(self):
        self.assertNotIn(
            "is-comparison-with-literal", shapes("def f(x):\n    return x is None\n")
        )

    def test_is_bool_is_silent(self):
        src = "def f(x):\n    return x is True or x is False\n"
        self.assertNotIn("is-comparison-with-literal", shapes(src))

    def test_equality_is_silent(self):
        self.assertNotIn(
            "is-comparison-with-literal", shapes("def f(x):\n    return x == 256\n")
        )


class TestEnvelopeAndOptions(unittest.TestCase):
    def test_envelope_keys(self):
        with TempProject({"m.py": "x = 1\n"}) as root:
            result = mod.analyze(str(root))
        for key in (
            "project_root",
            "scan_root",
            "files_total",
            "files_analyzed",
            "files_capped",
        ):
            self.assertIn(key, result)
        for key in (
            "by_shape",
            "by_severity",
            "by_confidence",
            "by_directory",
            "checks_run",
        ):
            self.assertIn(key, result["summary"])

    def test_file_target_scans_only_that_file(self):
        # Pointing at one file must not silently scan the whole project.
        with TempProject(
            {
                "a.py": "def f(x=[]):\n    x.append(1)\n",
                "b.py": "def g(y=[]):\n    y.append(1)\n",
            }
        ) as root:
            result = mod.analyze(str(root / "a.py"))
        self.assertEqual(result["files_analyzed"], 1)
        self.assertEqual(result["summary"]["total_findings"], 1)

    def test_check_filter(self):
        src = "def f(x=[]):\n    x.append(1)\ndef g():\n    return 1 is 2\n"
        with TempProject({"m.py": src}) as root:
            result = mod.analyze(str(root), checks=["is-comparison-with-literal"])
        found = {f["shape"] for f in result["findings"]}
        self.assertEqual(found, {"is-comparison-with-literal"})

    def test_exclude_pattern(self):
        files = {
            "src/a.py": "def f(x=[]):\n    x.append(1)\n",
            "generated/b.py": "def g(y=[]):\n    y.append(1)\n",
        }
        with TempProject(files) as root:
            result = mod.analyze(str(root), exclude=["generated/"])
        self.assertEqual(result["summary"]["total_findings"], 1)
        self.assertIn("src", result["summary"]["by_directory"])

    def test_findings_are_sorted(self):
        with TempProject(
            {"m.py": "def f(x=[]):\n    x.append(1)\ndef g(y=[]):\n    y.append(1)\n"}
        ) as root:
            findings = mod.analyze(str(root))["findings"]
        keys = [(f["file"], f["line"], f["shape"]) for f in findings]
        self.assertEqual(keys, sorted(keys))

    def test_syntax_error_file_does_not_abort(self):
        files = {
            "bad.py": "def broken( :\n",
            "good.py": "def f(x=[]):\n    x.append(1)\n",
        }
        with TempProject(files) as root:
            result = mod.analyze(str(root))
        self.assertEqual(result["summary"]["total_findings"], 1)

    def test_every_check_maps_to_a_catalogued_shape(self):
        catalog = import_script("build_informed_briefing")
        ids = {s["id"] for s in catalog._load_shapes()}
        self.assertEqual(set(mod._CHECKS) - ids, set())

    def test_every_finding_carries_required_fields(self):
        findings = scan("def f(x=[]):\n    x.append(1)\n")
        for key in ("shape", "severity", "confidence", "file", "line", "message"):
            self.assertIn(key, findings[0])


class TestExceptExceptionTooBroad(unittest.TestCase):
    """The gist's #1 family: ~50% of 40 confirmed CPython stdlib findings."""

    def test_narrow_body_swallowed_is_high(self):
        src = (
            "def f(o):\n    try:\n        x = o.a.b.c\n"
            "    except Exception:\n        x = None\n    return x\n"
        )
        f = [x for x in scan(src) if x["shape"] == "except-exception-too-broad"]
        self.assertEqual(f[0]["confidence"], "high")

    def test_narrow_body_with_pass_is_high(self):
        src = "def f(o):\n    try:\n        o.flush()\n    except Exception:\n        pass\n"
        f = [x for x in scan(src) if x["shape"] == "except-exception-too-broad"]
        self.assertEqual(f[0]["confidence"], "high")

    def test_base_exception_also_flagged(self):
        src = "def f(o):\n    try:\n        o.flush()\n    except BaseException:\n        pass\n"
        self.assertIn("except-exception-too-broad", shapes(src))

    def test_loud_logging_downgrades(self):
        src = (
            "import logging\nlog = logging.getLogger(__name__)\n"
            "def f(o):\n    try:\n        o.flush()\n"
            "    except Exception:\n        log.exception('failed')\n"
        )
        f = [x for x in scan(src) if x["shape"] == "except-exception-too-broad"]
        self.assertEqual(f[0]["confidence"], "medium")

    def test_large_body_boundary_is_low(self):
        src = (
            "def f(p):\n    try:\n        p.setup()\n        p.configure()\n"
            "        p.start()\n        p.verify()\n    except Exception:\n        pass\n"
        )
        f = [x for x in scan(src) if x["shape"] == "except-exception-too-broad"]
        self.assertEqual(f[0]["confidence"], "low")

    def test_narrow_exception_type_is_silent(self):
        src = (
            "def f(o):\n    try:\n        return o.a.b\n"
            "    except AttributeError:\n        return None\n"
        )
        self.assertNotIn("except-exception-too-broad", shapes(src))

    def test_reraising_handler_is_silent(self):
        src = "def f(o):\n    try:\n        o.flush()\n    except Exception:\n        raise\n"
        self.assertNotIn("except-exception-too-broad", shapes(src))


def broad(source: str) -> list[dict]:
    return [x for x in scan(source) if x["shape"] == "except-exception-too-broad"]


def scan_result(source: str) -> dict:
    with TempProject({"mod.py": source}) as root:
        return mod.analyze(str(root / "mod.py"))


class TestExceptExceptionTooBroadGrading(unittest.TestCase):
    """Graded by what the handler does with the failure. On a real web-service
    codebase the shape was 77 of 100 findings, ~95% dismissable, when graded
    by try-body size."""

    def test_error_envelope_is_suppressed_but_counted(self):
        # A service-layer boundary: log at warning, return {"ok": False, ...}.
        src = (
            "import logging\nlog = logging.getLogger(__name__)\n"
            "def get(store, k):\n    try:\n        return {'ok': True, 'v': store.get(k)}\n"
            "    except Exception as e:\n        log.warning('get failed: %s', e)\n"
            "        return {'ok': False, 'code': 'get_failed'}\n"
        )
        result = scan_result(src)
        self.assertEqual(
            [
                f
                for f in result["findings"]
                if f["shape"] == "except-exception-too-broad"
            ],
            [],
        )
        self.assertEqual(
            result["summary"]["suppressed_by_differential"],
            {"except-exception-too-broad": {"returns-error-result": 1}},
        )
        self.assertEqual(result["suppressed"][0]["line"], 6)

    def test_returned_error_response_builder_is_suppressed(self):
        # A route handler: `return error(500, ...)` is a response, not a log call.
        src = (
            "def route(svc, request):\n    try:\n        out = svc.run()\n"
            "    except Exception as exc:\n"
            "        return error(500, 'failed', str(exc), request=request)\n"
            "    return out\n"
        )
        self.assertEqual(broad(src), [])

    def test_exception_carried_as_value_is_suppressed(self):
        src = (
            "class Job:\n    def finish(self):\n        try:\n"
            "            self.cleanup_dir()\n        except Exception as exc:\n"
            "            self.last_error = exc\n"
        )
        self.assertEqual(broad(src), [])

    def test_catch_all_in_del_is_suppressed(self):
        # A raising __del__ is its own shape; the broad catch is its guard.
        src = (
            "class R:\n    def __del__(self):\n        try:\n            self.close()\n"
            "        except Exception:\n            pass\n"
        )
        self.assertEqual(broad(src), [])

    def test_best_effort_close_is_low(self):
        src = (
            "def reset(self):\n    try:\n        self._db.close()\n"
            "    except Exception:\n        pass\n    self._db = None\n"
        )
        f = broad(src)
        self.assertEqual((f[0]["confidence"], f[0]["severity"]), ("low", "CONSIDER"))

    def test_parse_with_default_is_low(self):
        src = (
            "import json\ndef meta(payload):\n    try:\n        loaded = json.loads(payload)\n"
            "    except Exception:\n        return {}\n    return loaded\n"
        )
        self.assertEqual(broad(src)[0]["confidence"], "low")

    def test_empty_fallback_read_as_no_history_is_high(self):
        # Confirmed shape: a failed read becomes an empty result the caller trusts.
        src = (
            "def snapshot(store):\n    try:\n        stored = store.load_counters()\n"
            "    except Exception:\n        stored = {}\n    return merge(stored)\n"
        )
        f = broad(src)
        self.assertEqual((f[0]["confidence"], f[0]["severity"]), ("high", "FIX"))

    def test_false_read_as_consistent_is_high(self):
        # Confirmed shape: a read error becomes a `False` the caller acts on.
        src = (
            "def needs_rebuild(self, backend, db):\n    if self.dirty:\n        return True\n"
            "    try:\n        rids = db.list_ids()\n    except Exception:\n"
            "        return False\n    return backend.rids() != rids\n"
        )
        self.assertEqual(broad(src)[0]["confidence"], "high")

    def test_fallback_matching_an_established_return_is_medium(self):
        # flush() already returns False for "not flushed" on a guard path.
        src = (
            "def flush(store):\n    if store is None:\n        return False\n"
            "    try:\n        gen = store.generation()\n    except Exception:\n"
            "        return False\n    return store.apply(gen)\n"
        )
        f = broad(src)
        self.assertEqual(f[0]["confidence"], "medium")
        self.assertIn("line 3", f[0]["detail"])

    def test_none_fallback_checked_right_after_is_medium(self):
        # `dim = None` then `if dim is None: probe`.
        src = (
            "def load(model):\n    try:\n        dim = int(model.dimension())\n"
            "    except Exception:\n        dim = None\n    if dim is None:\n"
            "        dim = probe(model)\n    return dim\n"
        )
        self.assertEqual(broad(src)[0]["confidence"], "medium")

    def test_swallowed_flush_in_a_large_body_is_reported_above_low(self):
        # Confirmed shape: flush/close/bookkeeping swallowed by `pass`.
        src = (
            "def evict(self, backend, key):\n    try:\n        backend.flush()\n"
            "        backend.close()\n        self._clear_dirty(key)\n"
            "        self._count('evictions')\n    except Exception:\n        pass\n"
        )
        f = broad(src)
        self.assertEqual(f[0]["confidence"], "medium")
        self.assertIn("flush", f[0]["detail"])

    def test_logger_warning_downgrades_a_skipped_side_effect_to_low(self):
        src = (
            "import logging\nlog = logging.getLogger(__name__)\n"
            "def warm(store):\n    try:\n        store.warm()\n"
            "    except Exception as exc:\n        log.warning('warm-up failed: %s', exc)\n"
        )
        self.assertEqual(broad(src)[0]["confidence"], "low")

    def test_bare_error_call_is_not_a_loud_log(self):
        # `error(...)` without a logger receiver must not count as logging.
        src = (
            "def f(o):\n    try:\n        o.flush()\n    except Exception:\n"
            "        error('flush failed')\n"
        )
        self.assertEqual(broad(src)[0]["confidence"], "high")


class TestCleanupOnlyOnSuccess(unittest.TestCase):
    def test_close_last_in_try_is_flagged(self):
        src = (
            "def f(c):\n    try:\n        c.send(1)\n        c.check()\n"
            "        c.close()\n    except OSError:\n        pass\n"
        )
        self.assertIn("cleanup-only-on-success-path", shapes(src))

    def test_close_in_finally_is_silent(self):
        src = (
            "def f(c):\n    try:\n        c.send(1)\n    finally:\n        c.close()\n"
        )
        self.assertNotIn("cleanup-only-on-success-path", shapes(src))

    def test_handler_also_closes_is_silent(self):
        src = (
            "def f(c):\n    try:\n        c.send(1)\n        c.close()\n"
            "    except OSError:\n        c.close()\n"
        )
        self.assertNotIn("cleanup-only-on-success-path", shapes(src))

    def test_non_release_call_is_silent(self):
        src = (
            "def f(c):\n    try:\n        c.send(1)\n        c.flush()\n"
            "    except OSError:\n        pass\n"
        )
        self.assertNotIn("cleanup-only-on-success-path", shapes(src))


class TestErrorReportedBelowWarning(unittest.TestCase):
    def test_debug_only_is_flagged(self):
        src = (
            "import logging\nlog = logging.getLogger(__name__)\n"
            "def f(o):\n    try:\n        o.decref()\n"
            "    except Exception:\n        log.debug('failed')\n"
        )
        self.assertIn("error-reported-below-warning", shapes(src))

    def test_warning_is_silent(self):
        src = (
            "import logging\nlog = logging.getLogger(__name__)\n"
            "def f(o):\n    try:\n        o.decref()\n"
            "    except Exception:\n        log.warning('failed')\n"
        )
        self.assertNotIn("error-reported-below-warning", shapes(src))

    def test_reraise_is_silent(self):
        src = (
            "import logging\nlog = logging.getLogger(__name__)\n"
            "def f(o):\n    try:\n        o.decref()\n"
            "    except Exception:\n        log.debug('x')\n        raise\n"
        )
        self.assertNotIn("error-reported-below-warning", shapes(src))


class TestExceptInLoopWithoutExit(unittest.TestCase):
    def test_swallow_in_while_true_is_flagged(self):
        src = (
            "def f(p):\n    while True:\n        try:\n            return listdir(p)\n"
            "        except OSError:\n            pass\n"
        )
        self.assertIn("except-in-loop-without-exit", shapes(src))

    def test_break_in_handler_is_silent(self):
        src = (
            "def f(p):\n    while True:\n        try:\n            return listdir(p)\n"
            "        except OSError:\n            break\n"
        )
        self.assertNotIn("except-in-loop-without-exit", shapes(src))

    def test_raise_in_handler_is_silent(self):
        src = (
            "def f(p):\n    while True:\n        try:\n            return listdir(p)\n"
            "        except OSError:\n            raise\n"
        )
        self.assertNotIn("except-in-loop-without-exit", shapes(src))

    def test_queue_empty_poll_loop_is_silent(self):
        # idlelib rpc.py/run.py: "nothing queued right now" is the design.
        src = (
            "import queue\ndef f(q):\n    while True:\n        try:\n"
            "            msg = q.get(0)\n        except queue.Empty:\n            pass\n"
            "        do_other_work()\n"
        )
        self.assertNotIn("except-in-loop-without-exit", shapes(src))

    def test_timeout_poll_loop_is_silent(self):
        src = (
            "def f(s):\n    while True:\n        try:\n            return s.recv()\n"
            "        except TimeoutError:\n            pass\n"
        )
        self.assertNotIn("except-in-loop-without-exit", shapes(src))

    def test_oserror_scan_loop_still_flagged(self):
        # The gist's genuine instance: a real failure, not a poll signal.
        src = (
            "def f(p):\n    while True:\n        try:\n            return listdir(p)\n"
            "        except OSError:\n            pass\n"
        )
        self.assertIn("except-in-loop-without-exit", shapes(src))

    def test_bounded_loop_is_silent(self):
        src = (
            "def f(p):\n    for _ in range(3):\n        try:\n            return listdir(p)\n"
            "        except OSError:\n            pass\n"
        )
        self.assertNotIn("except-in-loop-without-exit", shapes(src))


class TestRaiseWithoutFrom(unittest.TestCase):
    def test_missing_from_is_flagged(self):
        src = (
            "def f(t):\n    try:\n        return int(t)\n"
            "    except ValueError as err:\n        raise TypeError('bad')\n"
        )
        self.assertIn("raise-without-from-in-except", shapes(src))

    def test_from_err_is_silent(self):
        src = (
            "def f(t):\n    try:\n        return int(t)\n"
            "    except ValueError as err:\n        raise TypeError('bad') from err\n"
        )
        self.assertNotIn("raise-without-from-in-except", shapes(src))

    def test_from_none_is_silent(self):
        src = (
            "def f(t):\n    try:\n        return int(t)\n"
            "    except ValueError:\n        raise TypeError('bad') from None\n"
        )
        self.assertNotIn("raise-without-from-in-except", shapes(src))

    def test_bare_reraise_is_silent(self):
        src = "def f(t):\n    try:\n        return int(t)\n    except ValueError:\n        raise\n"
        self.assertNotIn("raise-without-from-in-except", shapes(src))

    def test_reraising_caught_name_is_silent(self):
        src = (
            "def f(t):\n    try:\n        return int(t)\n"
            "    except ValueError as err:\n        raise err\n"
        )
        self.assertNotIn("raise-without-from-in-except", shapes(src))


class TestFlagNotResetOnEarlyExit(unittest.TestCase):
    """idlelib pyshell.py:488 -- a stuck guard flag wedges the component."""

    def test_early_return_skips_reset(self):
        src = (
            "class C:\n    def go(self):\n        if self.busy:\n            return\n"
            "        self.busy = True\n        if not self.ready():\n            return\n"
            "        self.work()\n        self.busy = False\n"
        )
        f = [x for x in scan(src) if x["shape"] == "flag-not-reset-on-early-exit"]
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0]["confidence"], "high")

    def test_finally_reset_is_silent(self):
        src = (
            "class C:\n    def go(self):\n        try:\n            self.busy = True\n"
            "            if not self.ready():\n                return\n            self.work()\n"
            "        finally:\n            self.busy = False\n"
        )
        self.assertNotIn("flag-not-reset-on-early-exit", shapes(src))

    def test_no_early_exit_is_silent(self):
        src = (
            "class C:\n    def go(self):\n        self.busy = True\n"
            "        self.work()\n        self.busy = False\n"
        )
        self.assertNotIn("flag-not-reset-on-early-exit", shapes(src))

    def test_local_variable_is_silent(self):
        # A bare local dies with the frame -- rebinding it wedges nothing.
        src = (
            "def f(t):\n    line = t.get()\n    if not line:\n        return None\n"
            "    line = ''\n    return line\n"
        )
        self.assertNotIn("flag-not-reset-on-early-exit", shapes(src))

    def test_same_value_twice_is_silent(self):
        src = (
            "class C:\n    def go(self):\n        self.x = True\n"
            "        if self.q():\n            return\n        self.x = True\n"
        )
        self.assertNotIn("flag-not-reset-on-early-exit", shapes(src))


class TestGuardRechecksCallReceiver(unittest.TestCase):
    """idlelib replace.py:214 -- `m = prog.match(..)` then `if not prog:`."""

    def test_receiver_checked_instead_of_result(self):
        src = (
            "def f(prog, chars):\n    m = prog.match(chars)\n"
            "    if not prog:\n        return None\n    return m.expand()\n"
        )
        f = [x for x in scan(src) if x["shape"] == "guard-rechecks-call-receiver"]
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0]["confidence"], "high")

    def test_is_none_form_also_caught(self):
        src = (
            "def f(prog, chars):\n    m = prog.match(chars)\n"
            "    if prog is None:\n        return None\n    return m\n"
        )
        self.assertIn("guard-rechecks-call-receiver", shapes(src))

    def test_correct_guard_is_silent(self):
        src = (
            "def f(prog, chars):\n    m = prog.match(chars)\n"
            "    if not m:\n        return None\n    return m.expand()\n"
        )
        self.assertNotIn("guard-rechecks-call-receiver", shapes(src))

    def test_unrelated_name_is_silent(self):
        src = (
            "def f(prog, chars, other):\n    m = prog.match(chars)\n"
            "    if not other:\n        return None\n    return m\n"
        )
        self.assertNotIn("guard-rechecks-call-receiver", shapes(src))


class TestFalsyCheckForNoneDefault(unittest.TestCase):
    def test_not_on_none_default(self):
        src = "def f(path=None):\n    if not path:\n        return 'default'\n    return path\n"
        self.assertIn("falsy-check-for-none-default", shapes(src))

    def test_is_none_is_silent(self):
        src = "def f(path=None):\n    if path is None:\n        return 'default'\n    return path\n"
        self.assertNotIn("falsy-check-for-none-default", shapes(src))

    def test_non_none_default_is_silent(self):
        src = "def f(path=''):\n    if not path:\n        return 'default'\n    return path\n"
        self.assertNotIn("falsy-check-for-none-default", shapes(src))

    def test_reassigned_parameter_is_silent(self):
        src = (
            "def f(path=None):\n    path = path or compute()\n"
            "    if not path:\n        return 'd'\n    return path\n"
        )
        self.assertNotIn("falsy-check-for-none-default", shapes(src))


class TestTestCannotFail(unittest.TestCase):
    """Tests that pass no matter what the code under test does."""

    def test_empty_test_body(self):
        src = (
            "import unittest\nclass T(unittest.TestCase):\n"
            "    def test_thing(self):\n        pass\n"
        )
        f = [x for x in scan(src) if x["shape"] == "test-cannot-fail"]
        self.assertEqual(f[0]["confidence"], "high")

    def test_constant_assertion(self):
        src = (
            "import unittest\nclass T(unittest.TestCase):\n"
            "    def test_thing(self):\n        self.assertTrue(True)\n"
        )
        self.assertIn("test-cannot-fail", shapes(src))

    def test_all_filter_is_vacuous(self):
        src = (
            "import unittest\nclass T(unittest.TestCase):\n"
            "    def test_thing(self):\n"
            "        self.assertTrue(all(filter(lambda x: x.startswith('_'), self.s)))\n"
        )
        self.assertIn("test-cannot-fail", shapes(src))

    def test_orphan_asserting_method(self):
        src = (
            "import unittest\nclass T(unittest.TestCase):\n"
            "    def check_thing(self):\n        self.assertEqual(1, self.x)\n"
        )
        f = [x for x in scan(src) if x["shape"] == "test-cannot-fail"]
        self.assertTrue(any("never runs it" in x["message"] for x in f))

    def test_called_helper_is_silent(self):
        # DRY assertion helper invoked from a real test -- correct design.
        src = (
            "import unittest\nclass T(unittest.TestCase):\n"
            "    def check_thing(self, v):\n        self.assertEqual(1, v)\n"
            "    def test_thing(self):\n        self.check_thing(self.x)\n"
        )
        self.assertNotIn("test-cannot-fail", shapes(src))

    def test_aliased_assertion_counts(self):
        # `Equal = self.assertEqual` is ubiquitous in CPython's own tests.
        src = (
            "import unittest\nclass T(unittest.TestCase):\n"
            "    def test_thing(self):\n        Equal = self.assertEqual\n"
            "        Equal(1, self.x)\n"
        )
        self.assertNotIn("test-cannot-fail", shapes(src))

    def test_real_assertion_is_silent(self):
        src = (
            "import unittest\nclass T(unittest.TestCase):\n"
            "    def test_thing(self):\n        self.assertEqual(compute(), 3)\n"
        )
        self.assertNotIn("test-cannot-fail", shapes(src))

    def test_fixtures_without_tests(self):
        src = (
            "import unittest\nclass T(unittest.TestCase):\n"
            "    def setUp(self):\n        self.x = 1\n"
        )
        f = [x for x in scan(src) if x["shape"] == "test-cannot-fail"]
        self.assertTrue(any("no test methods" in x["message"] for x in f))

    def test_non_testcase_class_ignored(self):
        src = "class Helper:\n    def test_thing(self):\n        pass\n"
        self.assertNotIn("test-cannot-fail", shapes(src))

    def test_loop_over_empty_literal(self):
        # CPython test_keymap.py:37 builds 60 cases and runs zero.
        src = (
            "import unittest\nclass T(unittest.TestCase):\n"
            "    def test_thing(self):\n"
            "        cases = [(k, k) for k in []]\n"
            "        for a, b in cases:\n            self.assertEqual(a, b)\n"
        )
        f = [x for x in scan(src) if "literal empty container" in x["message"]]
        self.assertEqual(len(f), 1)

    def test_loop_over_real_iterable_is_silent(self):
        src = (
            "import unittest\nclass T(unittest.TestCase):\n"
            "    def test_thing(self):\n"
            "        for k in ['a', 'b']:\n            self.assertTrue(k)\n"
        )
        self.assertNotIn("test-cannot-fail", shapes(src))


class TestSelfReferentialAccumulate(unittest.TestCase):
    """_pyrepl unix_console.py:545 -- `e.raw += e.raw` beside `e.data += e2.data`."""

    def test_adjacent_twin_makes_it_high(self):
        src = (
            "def f(q):\n    e = Ev()\n    while q:\n        e2 = q.get()\n"
            "        e.data += e2.data\n        e.raw += e.raw\n"
        )
        f = [x for x in scan(src) if x["shape"] == "self-referential-accumulate"]
        self.assertEqual(f[0]["confidence"], "high")
        self.assertIn("e2.data", f[0]["detail"])

    def test_without_twin_is_medium(self):
        src = "def f():\n    total = 0\n    total += total\n"
        f = [x for x in scan(src) if x["shape"] == "self-referential-accumulate"]
        self.assertEqual(f[0]["confidence"], "medium")

    def test_correct_source_is_silent(self):
        src = (
            "def f(q):\n    e = Ev()\n    while q:\n        e2 = q.get()\n"
            "        e.data += e2.data\n        e.raw += e2.raw\n"
        )
        self.assertNotIn("self-referential-accumulate", shapes(src))


class TestDuplicatedGuard(unittest.TestCase):
    """_pyrepl terminfo.py:401 -- a bounds check copied without its operand."""

    def test_repeated_guard_with_new_value(self):
        src = (
            "def f(data, offset, n):\n    if offset > len(data):\n        raise ValueError\n"
            "    end = offset + 2 * n\n    if offset > len(data):\n        raise ValueError\n"
            "    return data[offset:end]\n"
        )
        f = [x for x in scan(src) if x["shape"] == "duplicated-guard-wrong-operand"]
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0]["confidence"], "high")

    def test_guard_checking_new_value_is_silent(self):
        src = (
            "def f(data, offset, n):\n    if offset > len(data):\n        raise ValueError\n"
            "    end = offset + 2 * n\n    if end > len(data):\n        raise ValueError\n"
            "    return data[offset:end]\n"
        )
        self.assertNotIn("duplicated-guard-wrong-operand", shapes(src))

    def test_operand_rebound_between_is_silent(self):
        # The `path = ...; if path.is_file(): return ...` loop idiom.
        src = (
            "def f(dirs, name):\n    for d in dirs:\n        path = d / name\n"
            "        if path.is_file():\n            return path\n"
            "        path = d / 'hex' / name\n        if path.is_file():\n"
            "            return path\n"
        )
        self.assertNotIn("duplicated-guard-wrong-operand", shapes(src))

    def test_tuple_target_rebinding_is_silent(self):
        src = (
            "def f(value):\n    if not value:\n        raise ValueError\n"
            "    token, value = get(value)\n    if not value:\n        raise ValueError\n"
            "    return value\n"
        )
        self.assertNotIn("duplicated-guard-wrong-operand", shapes(src))

    def test_nested_rebinding_is_silent(self):
        src = (
            "def f(value):\n    if not value:\n        raise ValueError\n"
            "    if value[0] == 'x':\n        leader, value = get(value)\n"
            "    if not value:\n        raise ValueError\n    return value\n"
        )
        self.assertNotIn("duplicated-guard-wrong-operand", shapes(src))


class TestSignedLengthFromHeader(unittest.TestCase):
    """_pyrepl terminfo.py:373 -- five header counts unpacked signed."""

    HEADER = (
        "import struct\n"
        "def parse(data):\n"
        "    name_size, str_count = struct.unpack('<hh', data[:4])\n"
        "    offset = 12 + name_size\n"
        "    return data[offset:offset + str_count]\n"
    )

    def test_signed_extent_reaching_a_slice(self):
        f = [
            x
            for x in scan(self.HEADER)
            if x["shape"] == "signed-length-from-untrusted-header"
        ]
        self.assertEqual({x["confidence"] for x in f}, {"high"})
        self.assertEqual(
            {"name_size", "str_count"}, {x["message"].split("'")[1] for x in f}
        )

    def test_unsigned_format_is_silent(self):
        src = self.HEADER.replace("'<hh'", "'<HH'")
        self.assertNotIn("signed-length-from-untrusted-header", shapes(src))

    def test_negative_check_is_silent(self):
        # The guarded twin: ncurses range-checks every header field.
        src = self.HEADER.replace(
            "    offset = 12",
            "    if name_size < 0 or str_count < 0:\n"
            "        raise ValueError\n    offset = 12",
        )
        self.assertNotIn("signed-length-from-untrusted-header", shapes(src))

    def test_clamping_is_silent(self):
        src = self.HEADER.replace("12 + name_size", "12 + max(0, name_size)")
        f = [
            x for x in scan(src) if x["shape"] == "signed-length-from-untrusted-header"
        ]
        self.assertNotIn("name_size", {x["message"].split("'")[1] for x in f})

    def test_only_the_signed_field_of_a_mixed_format_is_flagged(self):
        # '<Hh' -- count is unsigned, size is signed. Flagging both by
        # association is what made test_zipfile emit eight findings.
        src = (
            "import struct\n"
            "def parse(data):\n"
            "    count, size = struct.unpack('<Hh', data[:4])\n"
            "    return data[:size], data[:count]\n"
        )
        f = [
            x for x in scan(src) if x["shape"] == "signed-length-from-untrusted-header"
        ]
        self.assertEqual([x["message"].split("'")[1] for x in f], ["size"])

    def test_padding_and_string_fields_do_not_shift_alignment(self):
        # '4s' consumes four bytes for ONE value and '2x' produces none.
        src = (
            "import struct\n"
            "def parse(data):\n"
            "    magic, flags, size = struct.unpack('<4s2xHh', data[:10])\n"
            "    return data[:size]\n"
        )
        f = [
            x for x in scan(src) if x["shape"] == "signed-length-from-untrusted-header"
        ]
        self.assertEqual([x["message"].split("'")[1] for x in f], ["size"])

    def test_non_extent_name_is_silent(self):
        src = (
            "import struct\n"
            "def parse(data):\n"
            "    version, = struct.unpack('<h', data[:2])\n    return version\n"
        )
        self.assertNotIn("signed-length-from-untrusted-header", shapes(src))


class TestAsymmetricEncodeDecode(unittest.TestCase):
    """_pyrepl readline.py:443 vs :460 -- lenient read, strict write-back."""

    def test_lenient_read_strict_write(self):
        src = (
            "def read(p):\n    with open(p, encoding='utf-8', errors='replace') as f:\n"
            "        return f.read()\n"
            "def write(p, s):\n    with open(p, 'w', encoding='utf-8') as f:\n"
            "        f.write(s)\n"
        )
        f = [x for x in scan(src) if x["shape"] == "asymmetric-encode-decode-pair"]
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0]["confidence"], "high")

    def test_binary_read_with_manual_decode(self):
        # The real _pyrepl form: `open(p, 'rb')` plus `.decode(enc, errors=...)`.
        src = (
            "def read(p):\n    with open(p, 'rb') as f:\n"
            "        return f.read().decode('utf-8', errors='replace')\n"
            "def write(p, s):\n    with open(p, 'w', encoding='utf-8') as f:\n"
            "        f.write(s)\n"
        )
        f = [x for x in scan(src) if x["shape"] == "asymmetric-encode-decode-pair"]
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0]["confidence"], "high")

    def test_symmetric_codec_is_silent(self):
        # The guarded twin: Modules/readline.c uses surrogateescape on both sides.
        src = (
            "def read(p):\n"
            "    with open(p, encoding='utf-8', errors='surrogateescape') as f:\n"
            "        return f.read()\n"
            "def write(p, s):\n"
            "    with open(p, 'w', encoding='utf-8', errors='surrogateescape') as f:\n"
            "        f.write(s)\n"
        )
        self.assertNotIn("asymmetric-encode-decode-pair", shapes(src))

    def test_codec_varying_suite_is_silent(self):
        # A module that opens one path under many codecs is VARYING them on
        # purpose. This class was 876 of 904 findings in the raw stdlib pass.
        src = (
            "def t(p):\n"
            "    open(p, 'w', encoding='utf-8').write('x')\n"
            "    open(p, 'w', encoding='latin-1').write('x')\n"
            "    open(p, encoding='utf-8').read()\n"
            "    open(p, encoding='ascii').read()\n"
        )
        self.assertNotIn("asymmetric-encode-decode-pair", shapes(src))

    def test_binary_on_both_sides_is_silent(self):
        src = (
            "def read(p):\n    return open(p, 'rb').read()\n"
            "def write(p, b):\n    open(p, 'wb').write(b)\n"
        )
        self.assertNotIn("asymmetric-encode-decode-pair", shapes(src))

    def test_self_encode_is_not_a_codec(self):
        # `self.encode(text)` is a method taking DATA, not str.encode taking a
        # codec name -- reading its argument as an encoding invented a mismatch
        # in idlelib's iomenu.py.
        src = (
            "class M:\n"
            "    def read(self, p):\n        return open(p, 'rb').read()\n"
            "    def write(self, p, text):\n"
            "        chars = self.encode(text)\n"
            "        with open(p, 'wb') as f:\n            f.write(chars)\n"
        )
        self.assertNotIn("asymmetric-encode-decode-pair", shapes(src))


class TestLifecycleHookTwoMeanings(unittest.TestCase):
    """_pyrepl commands.py:225-229 -- Ctrl-C calls the line-accepted hook."""

    def test_commit_hook_on_abort_path(self):
        src = (
            "class accept(Command):\n    def do(self):\n        self.reader.finish()\n"
            "class ctrl_c(Command):\n    def do(self):\n        self.reader.finish()\n"
        )
        f = [x for x in scan(src) if x["shape"] == "one-lifecycle-hook-two-meanings"]
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0]["confidence"], "high")
        self.assertIn("ctrl_c.do", f[0]["message"])

    def test_hook_parameterized_by_outcome_is_silent(self):
        # The guarded twin, from tkinter/dnd.py: `finish` takes a commit flag, so
        # it implements BOTH meanings.
        src = (
            "class DndHandler:\n"
            "    def on_release(self, event):\n        self.finish(event, 1)\n"
            "    def cancel(self, event=None):\n        self.finish(event, 0)\n"
            "    def finish(self, event, commit=0):\n        pass\n"
        )
        self.assertNotIn("one-lifecycle-hook-two-meanings", shapes(src))

    def test_predicate_call_is_silent(self):
        # asyncio's Future.done() is a QUERY. Read as a hook it was the largest
        # false-positive class in the raw pass.
        src = (
            "class Task:\n    def cancel(self):\n        if self.done():\n"
            "            return False\n        return True\n"
        )
        self.assertNotIn("one-lifecycle-hook-two-meanings", shapes(src))

    def test_release_hook_is_not_checked(self):
        # `close`/`cleanup`/`flush` mean "let go of the resource", correct on
        # both paths -- including them buries the real signal.
        src = (
            "class R:\n    def accept(self):\n        self.f.close()\n"
            "    def cancel(self):\n        self.f.close()\n"
        )
        self.assertNotIn("one-lifecycle-hook-two-meanings", shapes(src))

    def test_resource_receiver_is_downgraded(self):
        src = (
            "class a(Command):\n    def do(self):\n        self.reader.console.finish()\n"
            "class ctrl_c(Command):\n    def do(self):\n        self.reader.console.finish()\n"
        )
        f = [x for x in scan(src) if x["shape"] == "one-lifecycle-hook-two-meanings"]
        self.assertEqual([x["confidence"] for x in f], ["medium"])

    def test_test_scope_is_silent(self):
        src = (
            "class InterruptedSendTimeoutTest:\n"
            "    def setUp(self):\n        self.serv.accept()\n"
            "    def other(self):\n        self.serv.accept()\n"
        )
        self.assertNotIn("one-lifecycle-hook-two-meanings", shapes(src))


# --------------------------------------------------------------------------
# Shapes banked from the _pyrepl benchmark
# --------------------------------------------------------------------------


class TestApiValueDomain(unittest.TestCase):
    """_pyrepl input.py:94 -- category(k) == "C" can never be true."""

    def test_coarser_value_than_the_api_returns(self):
        src = (
            "import unicodedata\n"
            "def f(k):\n    if unicodedata.category(k) == 'C':\n        return 1\n"
        )
        f = [x for x in scan(src) if x["shape"] == "api-value-domain-mismatch"]
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0]["confidence"], "high")
        self.assertIn("'Cc'", f[0]["detail"])

    def test_valid_value_is_silent(self):
        src = "import unicodedata\ndef f(k):\n    return unicodedata.category(k) == 'Cc'\n"
        self.assertNotIn("api-value-domain-mismatch", shapes(src))

    def test_startswith_is_silent(self):
        # The guarded twin: startswith is how you test a category CLASS.
        src = (
            "import unicodedata\n"
            "def f(k):\n    return unicodedata.category(k).startswith('C')\n"
        )
        self.assertNotIn("api-value-domain-mismatch", shapes(src))

    def test_value_bound_to_a_local(self):
        src = (
            "import unicodedata\n"
            "def f(k):\n    cat = unicodedata.category(k)\n    return cat == 'C'\n"
        )
        self.assertIn("api-value-domain-mismatch", shapes(src))

    def test_open_domain_is_medium(self):
        src = "import sys\ndef f():\n    return sys.platform == 'linux2'\n"
        f = [x for x in scan(src) if x["shape"] == "api-value-domain-mismatch"]
        self.assertEqual(f[0]["confidence"], "medium")


class TestIsinstanceOnContainer(unittest.TestCase):
    """_pyrepl reader.py:675 -- isinstance on the spec tuple, not the object."""

    def test_subscripted_before_the_test(self):
        src = (
            "def f(self, cmd):\n"
            "    if isinstance(cmd[0], str):\n        t = self.get(cmd[0])\n"
            "    command = t(self, *cmd)\n"
            "    if not isinstance(cmd, commands.digit_arg):\n        self.last = t\n"
        )
        f = [
            x for x in scan(src) if x["shape"] == "isinstance-on-container-not-element"
        ]
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0]["confidence"], "high")

    def test_guard_then_subscript_is_silent(self):
        # Counter.__add__: the guard comes FIRST, which is the correct idiom.
        src = (
            "def add(self, other):\n"
            "    if not isinstance(other, Counter):\n        return NotImplemented\n"
            "    return other['x']\n"
        )
        self.assertNotIn("isinstance-on-container-not-element", shapes(src))

    def test_subscript_under_a_type_guard_is_silent(self):
        src = (
            "def f(value):\n"
            "    if isinstance(value, str):\n        return value[0]\n"
            "    if isinstance(value, int):\n        return value\n"
        )
        self.assertNotIn("isinstance-on-container-not-element", shapes(src))

    def test_container_type_is_silent(self):
        src = "def f(x):\n    y = x[0]\n    return isinstance(x, tuple)\n"
        self.assertNotIn("isinstance-on-container-not-element", shapes(src))


class TestMockCallableAsSpec(unittest.TestCase):
    """_pyrepl test_unix_console.py -- MagicMock(lambda...) is inert at 7 sites."""

    def test_lambda_as_first_positional(self):
        src = "from unittest.mock import MagicMock\ndef f(h, w):\n    return MagicMock(lambda _: (h, w))\n"
        f = [x for x in scan(src) if x["shape"] == "mock-callable-as-spec"]
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0]["confidence"], "high")

    def test_side_effect_keyword_is_silent(self):
        src = "from unittest.mock import MagicMock\ndef f(h, w):\n    return MagicMock(side_effect=lambda _: (h, w))\n"
        self.assertNotIn("mock-callable-as-spec", shapes(src))

    def test_spec_class_is_silent(self):
        src = "from unittest.mock import MagicMock\ndef f():\n    return MagicMock(SomeClass)\n"
        self.assertNotIn("mock-callable-as-spec", shapes(src))


class TestDecodeErrorAsIncomplete(unittest.TestCase):
    """_pyrepl base_eventqueue.py:104 -- one bad byte wedges the queue."""

    def test_bare_return_on_decode_error(self):
        src = (
            "def push(self, c):\n    self.buf.append(c)\n    try:\n"
            "        d = bytes(self.buf).decode(self.encoding)\n"
            "    except UnicodeError:\n        return\n    self.insert(d)\n"
        )
        f = [x for x in scan(src) if x["shape"] == "decode-error-treated-as-incomplete"]
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0]["confidence"], "high")

    def test_handler_that_recovers_is_silent(self):
        src = (
            "def push(self, c):\n    try:\n"
            "        d = bytes(self.buf).decode(self.encoding)\n"
            "    except UnicodeError:\n        self.buf.clear()\n        return\n"
        )
        self.assertNotIn("decode-error-treated-as-incomplete", shapes(src))

    def test_non_decode_try_is_silent(self):
        src = "def f(x):\n    try:\n        return int(x)\n    except ValueError:\n        return\n"
        self.assertNotIn("decode-error-treated-as-incomplete", shapes(src))


class TestUnvalidatedEnvNumeric(unittest.TestCase):
    """_pyrepl unix_console.py:471 -- COLUMNS=0 spins the REPL forever."""

    def test_env_branch_unvalidated_beside_a_validated_twin(self):
        src = (
            "import os\n"
            "def getheightwidth(self):\n    try:\n"
            "        return int(os.environ['LINES']), int(os.environ['COLUMNS'])\n"
            "    except (KeyError, ValueError):\n"
            "        size = ioctl(self.fd, TIOCGWINSZ, b'')\n"
            "        height, width = struct.unpack('hhhh', size)[0:2]\n"
            "        if not height:\n            return 25, 80\n"
            "        return height, width\n"
        )
        f = [
            x for x in scan(src) if x["shape"] == "unvalidated-numeric-from-environment"
        ]
        self.assertTrue(f)
        self.assertEqual(f[0]["confidence"], "high")

    def test_validated_env_value_is_silent(self):
        src = (
            "import os\n"
            "def f():\n    n = int(os.environ['COLUMNS'])\n"
            "    if n <= 0:\n        return 80\n    return n\n"
        )
        self.assertNotIn("unvalidated-numeric-from-environment", shapes(src))

    def test_non_env_int_is_silent(self):
        src = "def f(s):\n    return int(s)\n"
        self.assertNotIn("unvalidated-numeric-from-environment", shapes(src))


class TestReturnIgnoredAgainstFamily(unittest.TestCase):
    """_pyrepl windows_console.py:152,156 -- the only unchecked Win32 calls."""

    CHECKED = (
        "    if not ReadConsoleInput(a):\n        raise WinError()\n"
        "    if not WriteConsoleW(b):\n        raise WinError()\n"
        "    if not ScrollConsoleScreenBuffer(c):\n        raise WinError()\n"
    )

    def test_discarded_beside_checked_siblings(self):
        src = (
            "import ctypes\ndef f(a, b, c, h):\n"
            + self.CHECKED
            + "    GetConsoleMode(h)\n"
        )
        f = [
            x
            for x in scan(src)
            if x["shape"] == "return-ignored-against-checked-family"
        ]
        self.assertEqual(len(f), 1)

    def test_non_ffi_module_is_silent(self):
        # Without a ctypes/winapi import the convention argument does not hold;
        # 720 of 787 raw findings were test modules constructing objects.
        src = "def f(a, b, c, h):\n" + self.CHECKED + "    GetConsoleMode(h)\n"
        self.assertNotIn("return-ignored-against-checked-family", shapes(src))

    def test_all_checked_is_silent(self):
        src = (
            "import ctypes\ndef f(a, b, c, h):\n"
            + self.CHECKED
            + "    if not GetConsoleMode(h):\n        raise WinError()\n"
        )
        self.assertNotIn("return-ignored-against-checked-family", shapes(src))


class TestWrapperMutatesForeignCollection(unittest.TestCase):
    """_pyrepl readline.py -- del history[:] past historical_reader's bookkeeping."""

    def test_mutation_through_an_accessor(self):
        src = "class W:\n    def clear(self):\n        self.get_reader().history.append(1)\n"
        self.assertIn("wrapper-mutates-foreign-collection", shapes(src))

    def test_own_attribute_is_silent(self):
        src = "class W:\n    def add(self, x):\n        self.history.append(x)\n"
        self.assertNotIn("wrapper-mutates-foreign-collection", shapes(src))

    def test_using_the_returned_object_is_silent(self):
        src = "class W:\n    def add(self, x):\n        self.get_list().append(x)\n"
        self.assertNotIn("wrapper-mutates-foreign-collection", shapes(src))


class TestSaveStateClobbered(unittest.TestCase):
    """_pyrepl unix_console.py -- Ctrl-Z, fg, exit leaves the terminal raw."""

    def test_snapshot_then_modify_without_a_guard(self):
        src = (
            "class C:\n"
            "    def prepare(self):\n        self.__svtermstate = tcgetattr(self.fd)\n"
            "        raw = self.__svtermstate\n        tcsetattr(self.fd, raw)\n"
            "    def restore(self):\n        tcsetattr(self.fd, self.__svtermstate)\n"
        )
        f = [x for x in scan(src) if x["shape"] == "save-state-clobbered-by-reentry"]
        self.assertEqual(len(f), 1)

    def test_idempotence_guard_is_silent(self):
        src = (
            "class C:\n"
            "    def prepare(self):\n        if self.__svtermstate is None:\n"
            "            self.__svtermstate = tcgetattr(self.fd)\n"
            "        tcsetattr(self.fd, raw)\n"
            "    def restore(self):\n        tcsetattr(self.fd, self.__svtermstate)\n"
        )
        self.assertNotIn("save-state-clobbered-by-reentry", shapes(src))

    def test_dunder_init_is_silent(self):
        # Initialization is SUPPOSED to snapshot; it cannot be re-entered.
        src = (
            "class C:\n"
            "    def __init__(self):\n        self.saved = tcgetattr(self.fd)\n"
            "        tcsetattr(self.fd, raw)\n"
            "    def restore(self):\n        tcsetattr(self.fd, self.saved)\n"
        )
        self.assertNotIn("save-state-clobbered-by-reentry", shapes(src))


class TestProjectLevelShapes(unittest.TestCase):
    """Two shapes compare files against each other, so they need a corpus."""

    def scan_project(self, files):
        with TempProject(files) as root:
            return mod.analyze(str(root))["findings"]

    def test_divergent_sentinel_across_parallel_modules(self):
        f = self.scan_project(
            {
                "unix_console.py": "def get(self):\n    return Event('key', None)\n",
                "windows_console.py": "def get(self):\n    return Event('key', '')\n",
            }
        )
        f = [x for x in f if x["shape"] == "divergent-sentinel-across-parallel-modules"]
        self.assertEqual(len(f), 2)
        self.assertEqual({x["confidence"] for x in f}, {"high"})

    def test_matching_sentinels_are_silent(self):
        f = self.scan_project(
            {
                "unix_console.py": "def get(self):\n    return Event('key', '')\n",
                "windows_console.py": "def get(self):\n    return Event('key', '')\n",
            }
        )
        self.assertNotIn(
            "divergent-sentinel-across-parallel-modules", [x["shape"] for x in f]
        )

    def test_unrelated_modules_are_silent(self):
        # No parallel-platform prefix means no parallel-pair relation.
        f = self.scan_project(
            {
                "alpha.py": "def get(self):\n    return Event('key', None)\n",
                "beta.py": "def get(self):\n    return Event('key', '')\n",
            }
        )
        self.assertNotIn(
            "divergent-sentinel-across-parallel-modules", [x["shape"] for x in f]
        )

    def test_unguarded_inverse_of_guarded_operation(self):
        f = self.scan_project(
            {
                "historical_reader.py": (
                    "class H:\n    def finish(self, ret):\n"
                    "        if ret and should_auto_add_history:\n"
                    "            self.history.append(ret)\n"
                ),
                "simple_interact.py": ("def run(reader):\n    reader.history.pop()\n"),
            }
        )
        f = [x for x in f if x["shape"] == "unguarded-inverse-of-guarded-operation"]
        self.assertEqual(len(f), 1)
        self.assertIn("simple_interact.py", f[0]["file"])

    def test_bare_local_collection_is_silent(self):
        # `parts`/`lines` are generic locals managed by one algorithm; matching
        # them paired glob.py against argparse.py.
        f = self.scan_project(
            {
                "a.py": "def f(flag):\n    parts = []\n    if flag:\n        parts.append(1)\n",
                "b.py": "def g():\n    parts = [1]\n    parts.pop()\n",
            }
        )
        self.assertNotIn(
            "unguarded-inverse-of-guarded-operation", [x["shape"] for x in f]
        )

    def test_same_function_add_and_remove_is_silent(self):
        f = self.scan_project(
            {
                "a.py": (
                    "def f(self, flag):\n"
                    "    if flag:\n        self.cands.add(1)\n"
                    "    self.cands.remove(1)\n"
                )
            }
        )
        self.assertNotIn(
            "unguarded-inverse-of-guarded-operation", [x["shape"] for x in f]
        )


class TestReturnsIsNarrow(unittest.TestCase):
    """`_returns` asks whether the OBJECT escapes, not whether the name appears.

    tkinter has seven `return self._configure(index, cnf, kw)` methods. Matching
    any occurrence of the name inside the returned expression reported every one
    at HIGH confidence with "the shared object is returned to callers" -- a claim
    the code does not support.
    """

    def test_passing_to_a_call_that_is_returned_does_not_escape(self):
        body = ast.parse("def f(cnf={}):\n    return g(1, cnf, 2)\n").body[0].body
        self.assertFalse(mod._returns("cnf", body))

    def test_returning_the_name_escapes(self):
        body = ast.parse("def f(cnf={}):\n    return cnf\n").body[0].body
        self.assertTrue(mod._returns("cnf", body))

    def test_container_literal_around_the_name_escapes(self):
        for src in ("return [cnf]", "return (cnf, 1)", "return {'k': cnf}"):
            with self.subTest(src=src):
                body = ast.parse(f"def f(cnf={{}}):\n    {src}\n").body[0].body
                self.assertTrue(mod._returns("cnf", body))

    def test_or_default_and_conditional_escape(self):
        for src in ("return cnf or {}", "return cnf if x else {}"):
            with self.subTest(src=src):
                body = ast.parse(f"def f(cnf={{}}, x=1):\n    {src}\n").body[0].body
                self.assertTrue(mod._returns("cnf", body))

    def test_attribute_access_on_the_name_does_not_escape(self):
        body = ast.parse("def f(cnf={}):\n    return cnf.copy()\n").body[0].body
        self.assertFalse(mod._returns("cnf", body))


SHAPE = "unformatted-format-string-literal"


class TestUnformattedFormatString(unittest.TestCase):
    """A `{name}` literal in a message nothing formats."""

    def test_raise_with_a_named_field(self):
        f = scan(
            "import sys\n"
            "def f():\n"
            "    raise NotImplementedError('cannot guess for {sys.platform}')\n"
        )
        self.assertEqual(f[0]["shape"], SHAPE)
        self.assertEqual(f[0]["severity"], "FIX")
        # `sys` is bound in the module, so the author meant an f-string.
        self.assertEqual(f[0]["confidence"], "high")

    def test_field_naming_nothing_in_scope_is_medium(self):
        f = scan("def f():\n    raise ValueError('bad {wibble}')\n")
        self.assertEqual(
            [x["confidence"] for x in f if x["shape"] == SHAPE], ["medium"]
        )

    def test_warning_and_logging_sinks(self):
        for call in (
            "warnings.warn('gone in {version}')",
            "logging.error('failed for {user}')",
            "log.info('saw {count}')",
            "print('hello {name}')",
        ):
            with self.subTest(call=call):
                self.assertIn(SHAPE, shapes(f"def f():\n    {call}\n"))

    # ---- the differentials, each a real guarded twin from CPython ----

    def test_format_receiver_is_silent(self):
        # runpy.py:125 -- the literal carries braces and is formatted in place.
        self.assertNotIn(
            SHAPE,
            shapes(
                "def f(mod_name):\n"
                "    raise RuntimeError("
                "'{mod_name!r} found'.format(mod_name=mod_name))\n"
            ),
        )

    def test_any_extra_argument_is_silent(self):
        # _pyrepl/trace.py -- `trace(line, *k)` formats only `if k or kw`.
        self.assertNotIn(SHAPE, shapes("def f(x):\n    log.info('saw {count}', x)\n"))
        self.assertNotIn(
            SHAPE, shapes("def f(x):\n    warnings.warn('{name} gone', remove=x)\n")
        )

    def test_fstring_is_silent(self):
        self.assertNotIn(
            SHAPE,
            shapes(
                "import sys\ndef f():\n    raise ValueError(f'for {sys.platform}')\n"
            ),
        )

    def test_bare_and_numeric_fields_are_silent(self):
        # `{}` and `{0}` are indistinguishable from regex quantifiers and from
        # literal braces in a character class -- they dominated the raw pass.
        self.assertNotIn(SHAPE, shapes("def f():\n    raise ValueError('got {}')\n"))
        self.assertNotIn(SHAPE, shapes("def f():\n    raise ValueError('got {0}')\n"))
        self.assertNotIn(
            SHAPE, shapes(r"def f():" "\n" r"    raise ValueError('\d{4}-\d{2}')" "\n")
        )

    def test_escaped_braces_are_silent(self):
        self.assertNotIn(
            SHAPE, shapes("def f():\n    raise ValueError('literal {{name}} here')\n")
        )

    def test_template_syntax_is_silent(self):
        # `${name}` is string.Template / shell, not str.format.
        self.assertNotIn(
            SHAPE, shapes("def f():\n    raise ValueError('path is ${prefix}')\n")
        )

    def test_non_message_call_is_silent(self):
        # A brace literal on its way to a formatter is the normal case.
        self.assertNotIn(
            SHAPE, shapes("def f():\n    register('{name} is deprecated')\n")
        )


if __name__ == "__main__":
    unittest.main()
