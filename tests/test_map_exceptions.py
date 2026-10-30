"""Tests for map_exceptions.py."""

import json
import subprocess
import sys
import textwrap
import unittest

from helpers import _SCRIPTS_DIR, TempProject, import_script

mod = import_script("map_exceptions")


def _map(files: dict[str, str], **kwargs) -> dict:
    files = {name: textwrap.dedent(src) for name, src in files.items()}
    with TempProject(files) as root:
        return mod.analyze(str(root / "app"), **kwargs)


ERRORS = """
    class AppError(Exception):
        pass

    class NotFound(AppError):
        code = "not_found"

    class _Internal(LookupError):
        pass
"""


class TestHierarchy(unittest.TestCase):
    def test_ancestors_cross_project_and_builtin_classes(self):
        out = _map({"app/errors.py": ERRORS})
        self.assertEqual(
            out["classes"]["AppError"]["ancestors"], ["Exception", "BaseException"]
        )
        self.assertEqual(
            out["classes"]["NotFound"]["ancestors"],
            ["AppError", "Exception", "BaseException"],
        )
        self.assertEqual(out["classes"]["_Internal"]["ancestors"][0], "LookupError")

    def test_non_exception_class_is_not_listed(self):
        out = _map({"app/m.py": "class Config(dict):\n    pass\n"})
        self.assertEqual(out["classes"], {})

    def test_handler_for_a_base_catches_the_subclass(self):
        out = _map(
            {
                "app/errors.py": ERRORS,
                "app/use.py": """
                    from app.errors import AppError, NotFound

                    def find():
                        raise NotFound()

                    def main():
                        try:
                            find()
                        except AppError:
                            return None
                """,
            }
        )
        caught = out["exceptions"]["NotFound"]["caught_at"]
        self.assertEqual(caught, [{"at": "app/use.py:10", "via": "AppError"}])
        self.assertEqual(out["leads"]["raised_never_caught"], [])


class TestRaiseSites(unittest.TestCase):
    def test_escapes_unless_a_local_try_catches_it(self):
        out = _map(
            {
                "app/m.py": """
                    def f(x):
                        try:
                            raise KeyError(x)
                        except LookupError:
                            raise ValueError(x)

                    def g(x):
                        raise KeyError(x)

                    def h(x):
                        try:
                            def inner():
                                raise KeyError(x)
                            return inner
                        except KeyError:
                            pass
                """
            }
        )
        sites = {(s["function"], s["exception"]): s for s in out["raise_sites"]}
        self.assertFalse(sites[("f", "KeyError")]["escapes_function"])
        self.assertEqual(sites[("f", "KeyError")]["caught_locally_at"], 5)
        # A raise in the handler is not protected by its own try.
        self.assertTrue(sites[("f", "ValueError")]["escapes_function"])
        self.assertTrue(sites[("g", "KeyError")]["escapes_function"])
        # The nested function runs later, outside h's try.
        self.assertTrue(sites[("h.inner", "KeyError")]["escapes_function"])

    def test_private_class_and_reraise_of_a_variable(self):
        out = _map(
            {
                "app/m.py": """
                    class _Stop(Exception):
                        pass

                    def f(exc):
                        raise _Stop()

                    def g(exc):
                        raise exc
                """
            }
        )
        self.assertEqual([s["exception"] for s in out["raise_sites"]], ["_Stop"])


class TestLeads(unittest.TestCase):
    def test_caught_never_raised(self):
        out = _map(
            {
                "app/m.py": """
                    class Gone(Exception):
                        pass

                    def f(work):
                        try:
                            work()
                        except Gone:
                            pass
                """
            }
        )
        self.assertEqual(out["leads"]["caught_never_raised"], ["Gone"])

    def test_subclass_raised_or_factory_built_is_not_a_lead(self):
        out = _map(
            {
                "app/m.py": """
                    class Base(Exception):
                        pass

                    class Child(Base):
                        pass

                    class Built(Exception):
                        pass

                    def make():
                        return Built("x")

                    def f(work):
                        try:
                            raise Child()
                        except Base:
                            pass
                        try:
                            raise make()
                        except Built:
                            pass
                """
            }
        )
        self.assertEqual(out["leads"]["caught_never_raised"], [])
        self.assertEqual(out["exceptions"]["Built"]["constructed_at"], ["app/m.py:12"])

    def test_raised_never_caught_ignores_broad_handlers(self):
        out = _map(
            {
                "app/m.py": """
                    class SpecError(Exception):
                        pass

                    def load():
                        raise SpecError("bad spec")

                    def main():
                        try:
                            load()
                        except Exception:
                            pass
                """
            }
        )
        self.assertEqual(out["leads"]["raised_never_caught"], ["SpecError"])
        self.assertEqual(out["summary"]["broad_handlers"], 1)


SERVICE = """
    class ApiError(Exception):
        def __init__(self, code, message):
            super().__init__(message)
            self.code = code

    class Plain(Exception):
        pass

    def get(key):
        if not key:
            raise ApiError("invalid_key", "key must not be empty")
        raise ApiError("not_found", f"no {key}")

    def put(key):
        if key is None:
            raise Plain("key is missing")
        return {"ok": False, "code": "conflict", "error": "exists"}

    def check(value):
        raise ValueError("invalid")
"""


class TestCodes(unittest.TestCase):
    def test_code_producers(self):
        out = _map(
            {
                "app/service.py": SERVICE,
                "app/more.py": """
                    from app.errors import NotFound

                    def f(result):
                        raise NotFound()

                    def g(result):
                        code = result.get("code", "unknown_error")
                        return error(status=500, code="internal")
                """,
                "app/errors.py": ERRORS,
            }
        )
        produced = set(out["codes"])
        self.assertEqual(
            produced,
            {"invalid_key", "not_found", "conflict", "unknown_error", "internal"},
        )
        vias = {p["via"] for info in out["codes"].values() for p in info["produced_at"]}
        self.assertIn("raise ApiError (first argument)", vias)
        self.assertIn("raise NotFound (class attribute)", vias)
        self.assertIn("dict key 'code'", vias)
        self.assertIn(".get('code') default", vias)
        self.assertIn("keyword code=", vias)

    def test_first_argument_heuristic_without_init(self):
        out = _map(
            {
                "app/m.py": """
                    class Coded(Exception):
                        pass

                    class Worded(Exception):
                        pass

                    def f(x):
                        if x:
                            raise Coded("bad_input")
                        raise Coded("too_large")

                    def g(x):
                        if x:
                            raise Worded("bad")
                        raise Worded("something went wrong")
                """
            }
        )
        self.assertEqual(set(out["codes"]), {"bad_input", "too_large"})
        self.assertTrue(out["classes"]["Coded"]["code_first_arg"])
        self.assertFalse(out["classes"]["Worded"]["code_first_arg"])

    def test_mappings_and_consumers(self):
        out = _map(
            {
                "app/service.py": SERVICE,
                "app/routes.py": """
                    STATUS = {"not_found": 404, "conflict": 409, "gone": 410}

                    def respond(result):
                        summary = {"ok": True, "conflict": 0, "count": 3, "total": 4}
                        return STATUS.get(result["code"], 500)

                    def classify(exc):
                        if exc.code == "invalid_key":
                            return 400
                        if exc.code.endswith("_found"):
                            return 404
                        match exc.code:
                            case "conflict":
                                return 409
                """,
            }
        )
        (mapping,) = out["mappings"]
        self.assertEqual(mapping["name"], "STATUS")
        self.assertEqual(mapping["keys_not_produced"], ["gone"])
        self.assertEqual(out["leads"]["codes_consumed_not_produced"], ["gone"])
        self.assertEqual(out["leads"]["codes_produced_not_consumed"], [])
        matches = {c["match"] for c in out["codes"]["not_found"]["consumed_at"]}
        self.assertEqual(matches, {"mapping key", "endswith"})
        self.assertEqual(
            out["by_module"]["app/service.py"]["produced"],
            ["conflict", "invalid_key", "not_found"],
        )

    def test_produced_but_never_consumed(self):
        out = _map({"app/service.py": SERVICE})
        self.assertEqual(
            out["leads"]["codes_produced_not_consumed"],
            ["conflict", "invalid_key", "not_found"],
        )

    def test_extra_code_key(self):
        src = "def f():\n    return {'reason': 'quota_exceeded'}\n"
        self.assertEqual(_map({"app/m.py": src})["codes"], {})
        out = _map({"app/m.py": src}, code_keys=["reason"])
        self.assertEqual(list(out["codes"]), ["quota_exceeded"])


class TestCli(unittest.TestCase):
    def test_cli_code_key_and_max_files(self):
        files = {
            "app/a.py": "def f():\n    return {'reason': 'quota_exceeded'}\n",
            "app/b.py": "x = 1\n",
        }
        with TempProject(files) as root:
            proc = subprocess.run(
                [
                    sys.executable,
                    str(_SCRIPTS_DIR / "map_exceptions.py"),
                    str(root / "app"),
                    "--code-key",
                    "reason",
                    "--max-files",
                    "1",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
        out = json.loads(proc.stdout)
        self.assertTrue(out["files_capped"])
        self.assertIn("reason", out["code_keys"])
        self.assertEqual(list(out["codes"]), ["quota_exceeded"])


if __name__ == "__main__":
    unittest.main()
