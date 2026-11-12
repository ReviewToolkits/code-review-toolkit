"""Tests for map_wiring.py."""

import json
import subprocess
import sys
import textwrap
import unittest

from helpers import _SCRIPTS_DIR, TempProject, import_script

mod = import_script("map_wiring")


def _map(files: dict[str, str], target: str = "app") -> dict:
    files = {name: textwrap.dedent(src) for name, src in files.items()}
    with TempProject(files) as root:
        return mod.analyze(str(root / target))


def _keys(entries: list[dict], field: str) -> list[str]:
    return [e[field] for e in entries]


PYRAMID_APP = {
    "app/__init__.py": "",
    "app/config.py": """
        from pyramid.config import Configurator

        def set_from_env(settings, name, envvar):
            pass

        def configure(settings):
            set_from_env(settings, "app.mail_host", "MAIL_HOST")
            settings.setdefault("app.page_size", 20)
            settings["app.orphan_flag"] = True
            config = Configurator(settings=settings)
            config.add_settings({"app.timeout": 5})
            config.add_route("home", "/")
            config.add_route("lonely", "/lonely/")
            config.add_route("items.detail", "/items/{id}")
            config.add_page_view("about", "/about/", "about.html")
            config.add_request_method(lambda r: 1, "widget_count", reify=True)
            config.add_request_method(lambda r: 2, "never_used", reify=True)
            return config
    """,
    "app/views.py": """
        from pyramid.view import view_config, view_defaults

        @view_config(route_name="home", renderer="app:templates/home.html",
                     permission="view")
        def home(request):
            size = request.registry.settings["app.page_size"]
            timeout = request.registry.settings.get("app.timeout", 3)
            missing = request.registry.settings["app.never_written"]
            n = request.widget_count
            return {"url": request.route_path("items.detail", id=1)}

        @view_config(route_name="ghost", renderer="json")
        def ghost(request):
            return request.route_url("nowhere")

        @view_defaults(route_name="items.detail", permission="edit")
        class ItemViews:
            def __init__(self, request):
                self.request = request

            @view_config(request_method="GET", renderer="items/detail.html")
            def get(self):
                return {}
    """,
    "app/templates/home.html": (
        '{% extends "base.html" %}\n'
        "{{ request.registry.settings['app.mail_host'] }}\n"
        "<a href=\"{{ request.route_path('home') }}\">home</a>\n"
    ),
    "app/templates/base.html": '{% include "partials/nav.html" %}\n',
    "app/templates/partials/nav.html": "nav\n",
    "app/templates/about.html": "about\n",
    "app/templates/items/detail.html": "detail\n",
    "app/templates/orphan.html": '{% include "partials/only_from_orphan.html" %}\n',
    "app/templates/partials/only_from_orphan.html": "x\n",
}


class TestPyramidStyleWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = _map(PYRAMID_APP)

    def test_framework_detected(self):
        self.assertIn("pyramid", self.out["frameworks"])

    def test_settings_written_never_read_and_read_never_written(self):
        s = self.out["settings"]
        self.assertEqual(s["status"], "detected")
        self.assertEqual(_keys(s["written_never_read"], "key"), ["app.orphan_flag"])
        self.assertEqual(_keys(s["read_never_written"], "key"), ["app.never_written"])
        self.assertFalse(s["read_never_written"][0]["default_at_every_read"])

    def test_helper_and_template_reads_count(self):
        s = self.out["settings"]
        flagged = _keys(s["written_never_read"], "key") + _keys(
            s["written_read_only_indirectly"], "key"
        )
        # Written by the setter helper, read only in a template.
        self.assertNotIn("app.mail_host", flagged)
        self.assertNotIn("app.timeout", flagged)

    def test_routes(self):
        r = self.out["routes"]
        self.assertEqual(_keys(r["routes_without_view"], "name"), ["lonely"])
        self.assertEqual(
            _keys(r["views_for_undeclared_routes"], "route_name"), ["ghost"]
        )
        self.assertEqual(_keys(r["url_names_not_declared"], "name"), ["nowhere"])
        declared = {d["name"]: d for d in r["declared"]}
        # The custom directive declares a route with its own view.
        self.assertIn("about", declared)
        self.assertEqual(declared["items.detail"]["permissions"], ["edit"])
        self.assertEqual(declared["items.detail"]["methods"], ["GET"])
        self.assertIn("lonely", r["declared_never_linked"])
        self.assertNotIn("home", r["declared_never_linked"])

    def test_templates(self):
        t = self.out["templates"]
        self.assertEqual(t["status"], "detected")
        unreferenced = _keys(t["unreferenced"], "template")
        self.assertEqual(
            sorted(unreferenced), ["orphan.html", "partials/only_from_orphan.html"]
        )
        self.assertEqual(t["missing"], [])

    def test_request_methods(self):
        rm = self.out["request_methods"]
        self.assertEqual(_keys(rm["declared_never_read"], "name"), ["never_used"])

    def test_permissions(self):
        p = self.out["permissions"]
        self.assertEqual(
            sorted(_keys(p["required_never_granted"], "permission")), ["edit", "view"]
        )


class TestFlaskStyleWiring(unittest.TestCase):
    def test_blueprint_endpoints_config_and_templates(self):
        out = _map(
            {
                "app/__init__.py": "",
                "app/web.py": """
                    from flask import Blueprint, Flask, current_app, render_template, url_for

                    app = Flask(__name__)
                    bp = Blueprint("shop", __name__)
                    app.config["CART_LIMIT"] = 10
                    app.config.update(UNUSED_FLAG=True)

                    @bp.route("/cart")
                    def cart():
                        limit = current_app.config["CART_LIMIT"]
                        return render_template("cart.html", next=url_for(".checkout"))

                    @bp.post("/checkout")
                    def checkout():
                        return render_template("missing.html", u=url_for("shop.gone"))
                """,
                "app/templates/cart.html": "cart\n",
            }
        )
        self.assertEqual(
            _keys(out["settings"]["written_never_read"], "key"), ["UNUSED_FLAG"]
        )
        declared = {d["name"]: d for d in out["routes"]["declared"]}
        self.assertEqual(sorted(declared), ["shop.cart", "shop.checkout"])
        self.assertEqual(declared["shop.checkout"]["methods"], ["POST"])
        self.assertEqual(
            _keys(out["routes"]["url_names_not_declared"], "name"), ["shop.gone"]
        )
        self.assertNotIn("shop.checkout", out["routes"]["declared_never_linked"])
        self.assertEqual(
            _keys(out["templates"]["missing"], "template"), ["missing.html"]
        )


class TestDjangoStyleWiring(unittest.TestCase):
    def test_settings_module_namespaced_urls_and_template_url_tag(self):
        out = _map(
            {
                "app/__init__.py": "",
                "app/settings.py": "DEBUG = True\nFEATURE_X = False\nPAGE = 5\n",
                "app/urls.py": """
                    from django.urls import path
                    from . import views

                    app_name = "blog"
                    urlpatterns = [
                        path("", views.index, name="index"),
                        path("<int:pk>/", views.detail, name="detail"),
                    ]
                """,
                "app/views.py": """
                    from django.conf import settings
                    from django.shortcuts import render, redirect
                    from django.urls import reverse

                    def index(request):
                        n = settings.PAGE
                        debug = getattr(settings, "DEBUG", False)
                        return render(request, "blog/index.html", {"n": n})

                    def detail(request, pk):
                        if settings.MISSING_OPT:
                            return redirect("blog:index")
                        return redirect(reverse("blog:archive"))
                """,
                "app/templates/blog/index.html": "{% url 'blog:detail' 1 %}\n",
            }
        )
        s = out["settings"]
        self.assertEqual(_keys(s["written_never_read"], "key"), ["FEATURE_X"])
        self.assertEqual(_keys(s["read_never_written"], "key"), ["MISSING_OPT"])
        r = out["routes"]
        self.assertEqual(
            sorted(d["name"] for d in r["declared"]), ["blog:detail", "blog:index"]
        )
        self.assertEqual(_keys(r["url_names_not_declared"], "name"), ["blog:archive"])
        self.assertEqual(r["declared_never_linked"], [])
        self.assertEqual(out["templates"]["unreferenced"], [])


class TestFastApiStyleRoutes(unittest.TestCase):
    def test_router_routes_get_function_names_and_methods(self):
        out = _map(
            {
                "app/__init__.py": "",
                "app/api.py": """
                    from fastapi import APIRouter, Request

                    router = APIRouter()

                    @router.get("/items/{item_id}")
                    async def read_item(item_id: int):
                        return {}

                    @router.post("/items", name="create")
                    async def create_item(request: Request):
                        return request.url_for("read_item", item_id=1)
                """,
            }
        )
        declared = {d["name"]: d for d in out["routes"]["declared"]}
        self.assertEqual(sorted(declared), ["create", "read_item"])
        self.assertEqual(declared["read_item"]["pattern"], "/items/{item_id}")
        self.assertEqual(declared["create"]["methods"], ["POST"])
        self.assertEqual(out["routes"]["routes_without_view"], [])
        self.assertEqual(out["templates"]["status"], "not_detected")


TASKS_APP = {
    "app/__init__.py": "",
    "app/tasks.py": """
        from celery import shared_task

        def _lookup(db, name):
            return db.query(Thing).filter(Thing.display_name == name).one()

        @shared_task
        def refresh(name):
            return _lookup(None, name)

        @shared_task(bind=True)
        def notify(self, user_id, channel="email"):
            return user_id

        @shared_task
        def orphan(x):
            return x

        @shared_task
        def tidy(request, path):
            return canonicalize(path)

        @shared_task
        def callback_only(key):
            return key
    """,
    "app/views.py": """
        from app import tasks
        from app.tasks import notify as _notify, refresh, tidy, callback_only

        def handler(request, thing):
            refresh.delay(thing.canonical_name)
            _notify.apply_async(args=[thing.owner_id], kwargs={"channel": "sms"})
            tasks.notify.delay(1, 2, 3)
            queue.wrap(tidy).delay(thing.path)
            register(callback=queue.wrap(callback_only).delay)
    """,
}


class TestTaskCrossReference(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = _map(TASKS_APP)["tasks"]
        cls.by_name = {t["task"]: t for t in cls.out["declared"]}

    def test_call_sites_bind_arguments_to_parameters(self):
        refresh = self.by_name["app.tasks.refresh"]
        self.assertEqual(
            refresh["call_sites"][0]["bound"], {"name": "thing.canonical_name"}
        )
        notify = self.by_name["app.tasks.notify"]
        self.assertTrue(notify["bind"])
        bound = [c.get("bound") for c in notify["call_sites"]]
        self.assertIn({"user_id": "thing.owner_id", "channel": "'sms'"}, bound)

    def test_binding_error_reported(self):
        errors = self.out["binding_errors"]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["task"], "app.tasks.notify")
        self.assertIn("3 positional", errors[0]["error"])

    def test_injected_request_parameter(self):
        tidy = self.by_name["app.tasks.tidy"]
        self.assertEqual(tidy["injected_params"], ["request"])
        self.assertEqual(tidy["params"], ["path"])
        self.assertEqual(tidy["call_sites"][0]["bound"], {"path": "thing.path"})

    def test_body_uses_followed_into_a_helper(self):
        uses = self.by_name["app.tasks.refresh"]["param_uses"]["name"]
        helper = uses["followed_into"][0]
        self.assertEqual(helper["helper"], "_lookup")
        self.assertTrue(
            any("display_name == name" in u["code"] for u in helper["uses"])
        )

    def test_normalisation_mismatch_is_a_lead(self):
        leads = {(x["task"], x["kind"]) for x in self.out["normalisation_differs"]}
        self.assertIn(("app.tasks.refresh", "caller_normalises_body_does_not"), leads)
        # A body that normalises what the caller passes raw is the safe direction.
        self.assertNotIn("app.tasks.tidy", {t for t, _ in leads})

    def test_never_called_and_passed_as_callable(self):
        self.assertEqual(_keys(self.out["never_called"], "task"), ["app.tasks.orphan"])
        site = self.by_name["app.tasks.callback_only"]["call_sites"][0]
        self.assertIsNone(site["bound"])


class TestSignalsAndEvents(unittest.TestCase):
    def test_signal_and_event_class_leads(self):
        out = _map(
            {
                "app/__init__.py": "",
                "app/signals.py": """
                    from blinker import signal

                    order_paid = signal("order-paid")
                    order_lost = signal("order-lost")
                    never_used = signal("never")

                    class ItemSaved:
                        def __init__(self, item):
                            self.item = item
                """,
                "app/handlers.py": """
                    from pyramid.events import subscriber
                    from app.signals import order_paid, order_lost, ItemSaved

                    @order_paid.connect
                    def on_paid(sender, amount, currency):
                        return amount

                    @order_lost.connect
                    def on_lost(sender, **kwargs):
                        return kwargs["reason"]

                    @subscriber(ItemSaved)
                    def on_saved(event):
                        return event.item, event.request

                    def pay(registry):
                        order_paid.send("shop", amount=3)
                        registry.notify(ItemSaved(1))
                """,
            }
        )
        sig = out["signals"]
        self.assertEqual(
            _keys(sig["handled_never_emitted"], "signal"), ["app.signals.order_lost"]
        )
        self.assertEqual(
            _keys(sig["declared_unused"], "signal"), ["app.signals.never_used"]
        )
        self.assertEqual(
            sig["keys_read_not_sent"],
            [
                {
                    "signal": "app.signals.order_paid",
                    "handler": "app.handlers.on_paid",
                    "keys": ["currency"],
                }
            ],
        )
        self.assertEqual(
            sig["attributes_read_not_set"],
            [
                {
                    "event": "app.signals.ItemSaved",
                    "handler": "app.handlers.on_saved",
                    "attributes": ["request"],
                }
            ],
        )


class TestPermissionsAcl(unittest.TestCase):
    def test_acl_variables_and_permission_class_members(self):
        out = _map(
            {
                "app/__init__.py": "",
                "app/perms.py": """
                    import enum

                    class Perms(enum.StrEnum):
                        Read = "read"
                        Write = "write"
                        Unused = "unused"
                        Stale = "stale"
                """,
                "app/models.py": """
                    from pyramid.authorization import Allow
                    from app.perms import Perms

                    class Thing:
                        def __acl__(self):
                            perms = [Perms.Read]
                            if self.owner:
                                perms.append(Perms.Write)
                            return [(Allow, "group:admins", perms), (Allow, "u", Perms.Stale)]
                """,
                "app/views.py": """
                    from pyramid.view import view_config
                    from app.perms import Perms

                    @view_config(route_name="x", permission=Perms.Read)
                    def read(request):
                        return request.has_permission(Perms.Write)
                """,
            }
        )
        p = out["permissions"]
        self.assertEqual(p["required_never_granted"], [])
        self.assertEqual(
            _keys(p["granted_never_required"], "permission"), ["Perms.Stale"]
        )
        self.assertEqual(
            _keys(p["declared_never_used"], "permission"), ["Perms.Unused"]
        )


class TestNotDetected(unittest.TestCase):
    def test_plain_library_says_not_detected_with_reasons(self):
        out = _map(
            {"app/__init__.py": "", "app/core.py": "def add(a, b):\n    return a + b\n"}
        )
        for section in mod._SECTIONS:
            with self.subTest(section=section):
                self.assertEqual(out[section]["status"], "not_detected")
                self.assertTrue(out[section]["reason"])
                self.assertEqual(out["summary"][section], "not_detected")
        self.assertIn("frameworks_note", out)

    def test_dynamic_template_name_matches_one_segment_and_names_its_value(self):
        out = _map(
            {
                "app/__init__.py": "",
                "app/mail.py": """
                    def body(kind):
                        return f"mail/{kind}/message.html"

                    WELCOME = "welcome"
                """,
                "app/templates/mail/welcome/message.html": "hi\n",
                "app/templates/mail/retired/message.html": "old\n",
                "app/templates/mail/retired/deep/message.html": "older\n",
            }
        )
        t = out["templates"]
        dyn = {e["template"]: e for e in t["matched_only_dynamically"]}
        self.assertTrue(dyn["mail/welcome/message.html"]["captured_mentioned"])
        self.assertFalse(dyn["mail/retired/message.html"]["captured_mentioned"])
        # A placeholder stands for one path segment, not a subtree.
        self.assertEqual(
            _keys(t["unreferenced"], "template"), ["mail/retired/deep/message.html"]
        )

    def test_route_name_in_a_template_string_is_not_a_settings_read(self):
        out = _map(
            {
                "app/__init__.py": "",
                "app/config.py": 'def f(settings):\n    settings["a.b"] = 1\n    return settings["a.b"]\n',
                "app/templates/t.html": "{{ request.route_path('account.settings.privacy') }}\n",
            }
        )
        self.assertEqual(out["settings"]["read_never_written"], [])


class TestCli(unittest.TestCase):
    def test_runs_and_prints_json(self):
        with TempProject({"app/__init__.py": "", "app/a.py": "x = 1\n"}) as root:
            proc = subprocess.run(
                [
                    sys.executable,
                    str(_SCRIPTS_DIR / "map_wiring.py"),
                    str(root / "app"),
                ],
                capture_output=True,
                text=True,
                check=True,
            )
        data = json.loads(proc.stdout)
        self.assertEqual(data["files_analyzed"], 2)
        self.assertIn("summary", data)

    def test_help(self):
        proc = subprocess.run(
            [sys.executable, str(_SCRIPTS_DIR / "map_wiring.py"), "--help"],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("Usage:", proc.stdout)


if __name__ == "__main__":
    unittest.main()
