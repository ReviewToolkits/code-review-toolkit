"""Tests for survey_error_handling.py."""

import json
import subprocess
import sys
import textwrap
import unittest

from helpers import _SCRIPTS_DIR, TempProject, import_script

mod = import_script("survey_error_handling")


def _survey(source: str, *, implicit: bool = True) -> dict:
    with TempProject({"pkg/mod.py": textwrap.dedent(source)}) as root:
        return mod.analyze(str(root / "pkg"), implicit=implicit)


def _handler(source: str, line: int | None = None) -> dict:
    handlers = _survey(source)["handlers"]
    if line is None:
        assert len(handlers) == 1, handlers
        return handlers[0]
    return next(h for h in handlers if h["line"] == line)


class TestHandlerOutcome(unittest.TestCase):
    def _handle(self, body: str) -> dict:
        src = "def f(work, log):\n    try:\n        work()\n    except Exception as exc:\n"
        src += textwrap.indent(textwrap.dedent(body), "        ")
        return _handler(src)

    def test_pass(self):
        h = self._handle("pass\n")
        self.assertEqual(h["outcome"], "pass")
        self.assertFalse(h["propagates"])

    def test_bare_reraise_and_reraise_by_name(self):
        self.assertEqual(self._handle("raise\n")["outcome"], "reraise")
        self.assertEqual(self._handle("raise exc\n")["outcome"], "reraise")

    def test_raise_chained_unchained_and_from_none(self):
        self.assertEqual(
            self._handle("raise ValueError('x') from exc\n")["outcome"],
            "raise-chained",
        )
        self.assertEqual(
            self._handle("raise ValueError('x')\n")["outcome"], "raise-unchained"
        )
        self.assertEqual(
            self._handle("raise ValueError('x') from None\n")["outcome"],
            "raise-from-none",
        )

    def test_exit(self):
        self.assertEqual(self._handle("sys.exit(1)\n")["outcome"], "exit")
        self.assertEqual(self._handle("raise SystemExit(2)\n")["outcome"], "exit")

    def test_return_continue_and_fall_through(self):
        self.assertEqual(self._handle("return None\n")["outcome"], "return")
        self.assertEqual(self._handle("x = 0\n")["outcome"], "fall-through")

    def test_log_without_and_with_traceback(self):
        h = self._handle("log.warning('failed: %s', exc)\n")
        self.assertEqual(h["outcome"], "report-and-continue")
        self.assertEqual(h["logs"][0]["level"], "warning")
        self.assertFalse(h["logs_traceback"])
        self.assertTrue(self._handle("log.exception('failed')\n")["logs_traceback"])
        self.assertTrue(
            self._handle("log.error('failed', exc_info=True)\n")["logs_traceback"]
        )
        self.assertFalse(
            self._handle("log.error('failed', exc_info=False)\n")["logs_traceback"]
        )

    def test_conditional_raise_is_marked(self):
        h = self._handle("if exc.args:\n    raise\nreturn None\n")
        self.assertEqual(h["outcome"], "reraise")
        self.assertTrue(h["outcome_conditional"])
        self.assertEqual(h["actions"], ["reraise", "return"])

    def test_raise_in_nested_def_is_not_the_handlers(self):
        # The nested function runs later, if ever: the handler swallows.
        h = self._handle("def later():\n    raise RuntimeError()\nreturn later\n")
        self.assertEqual(h["outcome"], "return")
        self.assertFalse(h["propagates"])

    def test_non_logger_attribute_call_is_not_a_log(self):
        h = self._handle("self.metrics.error('x')\n")
        self.assertEqual(h["logs"], [])


class TestHandlerContext(unittest.TestCase):
    def test_bare_tuple_and_broad(self):
        result = _survey(
            """
            def f():
                try:
                    pass
                except (KeyError, ValueError):
                    pass
                except:
                    pass
            """
        )
        narrow, bare = result["handlers"]
        self.assertEqual(narrow["caught"], ["KeyError", "ValueError"])
        self.assertFalse(narrow["broad"])
        self.assertTrue(bare["bare"])
        self.assertTrue(bare["broad"])

    def test_qualified_function_and_loop(self):
        h = _handler(
            """
            class C:
                def run(self, items):
                    for item in items:
                        try:
                            item()
                        except OSError:
                            continue
            """
        )
        self.assertEqual(h["function"], "C.run")
        self.assertTrue(h["in_loop"])
        self.assertEqual(h["outcome"], "continue")

    def test_loop_depth_does_not_leak_into_nested_function(self):
        h = _handler(
            """
            def outer(items):
                for item in items:
                    def inner():
                        try:
                            pass
                        except OSError:
                            return None
            """
        )
        self.assertFalse(h["in_loop"])

    def test_exception_text_exposed_in_result(self):
        h = _handler(
            """
            def f(work):
                try:
                    work()
                except Exception as exc:
                    return {"ok": False, "error": str(exc)}
            """
        )
        self.assertTrue(h["exception_exposed"])

    def test_structured_exception_field_is_not_exposure(self):
        h = _handler(
            """
            def f(work):
                try:
                    work()
                except Exception as exc:
                    return {"ok": False, "code": exc.code}
            """
        )
        self.assertFalse(h["exception_exposed"])


class TestMaybeUnbound(unittest.TestCase):
    def test_name_assigned_only_in_try_is_flagged(self):
        h = _handler(
            """
            def f(load, raw):
                try:
                    parsed = load(raw)
                    return parsed
                except Exception:
                    log.warning("failed on %s", parsed)
            """
        )
        self.assertEqual(h["maybe_unbound"], ["parsed"])

    def test_constant_first_assignment_is_always_bound(self):
        h = _handler(
            """
            def f(load, raw):
                try:
                    stage = "parse"
                    load(raw)
                except Exception:
                    log.warning("failed at %s", stage)
            """
        )
        self.assertEqual(h["maybe_unbound"], [])

    def test_bound_before_try_or_parameter_or_in_handler(self):
        h = _handler(
            """
            def f(load, raw):
                name = None
                try:
                    name = load(raw)
                    raw = name
                except Exception:
                    other = 1
                    log.warning("%s %s %s", name, raw, other)
            """
        )
        self.assertEqual(h["maybe_unbound"], [])


class TestCleanupLastInTry(unittest.TestCase):
    def _flags(self, source: str) -> list[str]:
        return _handler(source)["flags"]

    def test_cleanup_ending_the_try_body_is_flagged(self):
        h = _handler(
            """
            def swap(src, dst, backup):
                try:
                    shutil.move(src, dst)
                    shutil.rmtree(backup)
                except Exception:
                    shutil.move(backup, dst)
                    raise
            """
        )
        self.assertEqual(h["flags"], ["cleanup-last-in-try"])
        self.assertEqual(h["cleanup_call"], "shutil.rmtree")

    def test_cleanup_before_return_in_a_branch_is_flagged(self):
        flags = self._flags(
            """
            def export(build, out, tmp):
                try:
                    build(tmp)
                    if out is not None:
                        write(tmp, out)
                        remove_tree(tmp)
                        return out
                    return tmp
                except Exception:
                    remove_tree(tmp)
                    raise
            """
        )
        self.assertEqual(flags, ["cleanup-last-in-try"])

    def test_try_around_only_the_cleanup_is_not_flagged(self):
        flags = self._flags(
            """
            def tidy(path):
                try:
                    os.unlink(path)
                except OSError:
                    log.warning("could not remove %s", path)
            """
        )
        self.assertEqual(flags, [])

    def test_cleanup_followed_by_more_work_is_not_flagged(self):
        flags = self._flags(
            """
            def step(job):
                try:
                    job.run()
                    if job.cancelled:
                        job.cleanup_dir()
                    job.settle()
                except Exception:
                    job.mark_failed()
            """
        )
        self.assertEqual(flags, [])

    def test_trivial_reraise_or_pass_handler_is_not_flagged(self):
        for body in ("raise", "pass"):
            with self.subTest(body=body):
                flags = self._flags(
                    "def f(tmp):\n    try:\n        work()\n        os.remove(tmp)\n"
                    f"    except Exception:\n        {body}\n"
                )
                self.assertEqual(flags, [])


class TestSuppressExitFinally(unittest.TestCase):
    def test_suppress_blocks(self):
        result = _survey(
            """
            from contextlib import suppress
            import contextlib

            def f(a, b):
                with suppress(Exception):
                    a()
                    b()
                with contextlib.suppress(FileNotFoundError):
                    a()
            """
        )
        wide, narrow = result["suppress"]
        self.assertTrue(wide["broad"])
        self.assertEqual(wide["statements"], 2)
        self.assertEqual(narrow["caught"], ["FileNotFoundError"])
        self.assertFalse(narrow["broad"])
        self.assertEqual(result["summary"]["broad_suppress_multi_statement"], 1)

    def test_exit_methods(self):
        result = _survey(
            """
            class Always:
                def __exit__(self, *exc):
                    return True

            class Sometimes:
                def __exit__(self, exc_type, exc, tb):
                    return exc_type is KeyError

            class Never:
                def __exit__(self, *exc):
                    self.close()
                    return False
            """
        )
        exits = {e["function"]: e["suppresses"] for e in result["exit_methods"]}
        self.assertEqual(
            exits, {"Always.__exit__": "always", "Sometimes.__exit__": "conditional"}
        )

    def test_finally_calls_and_guards(self):
        result = _survey(
            """
            def f(lock, audit):
                try:
                    work()
                finally:
                    lock.release()
                    try:
                        audit.write("done")
                    except OSError:
                        pass
                    with suppress(Exception):
                        audit.flush()
            """
        )
        (block,) = result["finally"]
        self.assertEqual(block["unguarded_calls"], ["lock.release"])
        self.assertEqual(block["control_flow_exit"], [])

    def test_finally_control_flow_exit(self):
        result = _survey(
            """
            def f(items):
                for item in items:
                    try:
                        work(item)
                    finally:
                        for other in items:
                            break
                        continue

            def g():
                try:
                    work()
                finally:
                    return 1
            """
        )
        loop_exit, ret = result["finally"]
        self.assertEqual(loop_exit["control_flow_exit"], ["continue"])
        self.assertEqual(ret["control_flow_exit"], ["return"])


class TestImplicitAndSummary(unittest.TestCase):
    SOURCE = """
        import subprocess

        def f(obj, cfg):
            name = getattr(obj, "name", None)
            ok = hasattr(obj, "run")
            section = cfg.get("section", {})
            plain = cfg.get("key", None)
            subprocess.run(["true"])
            subprocess.run(["true"], check=True)
            try:
                pass
            except ValueError:
                pass
    """

    def test_implicit_kinds(self):
        result = _survey(self.SOURCE)
        self.assertEqual(
            result["summary"]["implicit_by_kind"],
            {
                "getattr-default": 1,
                "hasattr": 1,
                "get-empty-default": 1,
                "subprocess-unchecked": 1,
            },
        )
        self.assertEqual(result["summary"]["implicit_sites"], 4)

    def test_no_implicit_is_null_not_zero(self):
        result = _survey(self.SOURCE, implicit=False)
        self.assertIsNone(result["implicit"])
        self.assertIsNone(result["summary"]["implicit_sites"])
        self.assertIn("implicit: --no-implicit", result["skipped"])
        self.assertEqual(result["summary"]["explicit_sites"], 1)

    def test_unparseable_file_is_listed(self):
        with TempProject({"pkg/bad.py": "def (:\n", "pkg/ok.py": "x = 1\n"}) as root:
            result = mod.analyze(str(root / "pkg"))
        self.assertEqual(result["files_unparsed"], ["pkg/bad.py"])
        self.assertEqual(result["files_analyzed"], 2)


class TestCli(unittest.TestCase):
    def test_cli_emits_json_and_honours_max_files(self):
        files = {
            f"pkg/m{i}.py": "try:\n    pass\nexcept OSError:\n    pass\n"
            for i in range(3)
        }
        with TempProject(files) as root:
            proc = subprocess.run(
                [
                    sys.executable,
                    str(_SCRIPTS_DIR / "survey_error_handling.py"),
                    str(root / "pkg"),
                    "--max-files",
                    "2",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
        out = json.loads(proc.stdout)
        self.assertTrue(out["files_capped"])
        self.assertEqual(out["summary"]["handlers"], 2)
        self.assertEqual(out["handlers"][0]["function"], "<module>")


if __name__ == "__main__":
    unittest.main()
