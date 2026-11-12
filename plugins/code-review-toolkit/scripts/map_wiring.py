#!/usr/bin/env python3
"""Cross-reference a web application's framework wiring, statically.

On web applications most real dead-code findings are wiring gaps that no
name-reference scan sees: a setting written but never read, a route with no
view, a template nothing renders, a task nobody enqueues. This script lists
both ends of each kind of wiring and the gaps between them. Patterns for
Pyramid-, Flask-, Django- and FastAPI/Starlette-style applications and
Celery-style task queues are detected declaratively; a section whose
framework idioms are absent says ``"status": "not_detected"`` with a reason,
never an empty list that looks like a clean result.

Sections:

- ``settings``: keys written (``settings["k"] = v``, ``setdefault``,
  ``add_settings``, ``app.config.update``, a ``*set*(settings, "k", ...)``
  helper, UPPER_CASE names in a ``settings`` module or a ``*Config`` class,
  ``BaseSettings`` fields, ``[app:*]`` keys of top-level ``.ini`` files)
  against keys read (subscripts, ``.get``, ``getattr``, ``in``, attribute
  reads on ``settings``, and ``settings[...]`` in templates). Leads:
  ``written_never_read``, ``written_read_only_indirectly`` (the literal
  appears elsewhere, or a dynamic read could match), ``read_never_written``
  (``default_at_every_read`` false means a missing key raises).
- ``routes``: declared routes (``add_route``, a custom ``add_*(name,
  "/pattern")`` directive, ``@app.route``/``@bp.get``, ``add_url_rule``,
  ``path(..., name=)``, ``Route(..., name=)``, ``@router.get``) with their
  pattern, methods, permission and view; views attached to route names
  (``@view_config(route_name=)``, ``add_view``, ``@view_defaults``); names
  used to build URLs (``route_path``, ``route_url``, ``url_for``,
  ``url_path_for``, ``reverse``, ``{% url %}``). Leads: ``routes_without_view``,
  ``views_for_undeclared_routes``, ``url_names_not_declared``,
  ``declared_never_linked`` (informational: API routes are rarely linked).
- ``templates``: files under ``templates``/``jinja2`` directories against
  references from Python (any template-looking string literal, with render
  calls and ``renderer=`` marked explicit; f-strings become patterns) and
  from other templates (``extends``/``include``/``import``/``from``),
  followed transitively. Leads: ``unreferenced``, ``matched_only_dynamically``,
  ``missing`` (an explicit reference with no file).
- ``tasks``: functions decorated as tasks (``@app.task``, ``@shared_task``,
  ``@actor``, ...) with every call site (``.delay``, ``.apply_async``,
  ``.s``/``.si``, ``wrapper(task).delay``, ``send_task("name")``,
  ``enqueue(task, ...)``, periodic registration). Each call site's argument
  expressions are bound to the task's parameters and listed next to how the
  body uses each parameter, so a caller passing a normalised name to a body
  that looks it up by display name is visible. A leading ``request``/
  ``task``/``ctx`` parameter the call does not supply is reported as
  ``injected_params``. Leads: ``never_called``, ``binding_errors``,
  ``normalisation_differs``.
- ``signals``: project signals (``Signal()``, blinker ``signal("n")``) and
  event classes (``@subscriber(Event)``, ``add_subscriber``, ``notify(Event(...))``)
  with their emitters and handlers. Leads: ``emitted_never_handled``,
  ``handled_never_emitted``, ``declared_unused``, ``keys_read_not_sent``
  (a handler reads a keyword no sender passes) and ``attributes_read_not_set``
  (a subscriber reads ``event.x`` the event class never sets).
- ``request_methods``: ``add_request_method`` declarations against
  ``request.<name>`` reads in code and templates. Lead: ``declared_never_read``.
- ``permissions``: permissions required (``permission=``,
  ``permission_required``, ``has_permission``, ``has_perm``, also in
  templates) against permissions granted (``(Allow, principal, perm)`` ACL
  entries, including a list variable built in the same scope, Django
  ``Meta.permissions``) and members of the permission class. Leads:
  ``required_never_granted``, ``granted_never_required``, ``declared_never_used``.

Everything here is a lead for the agent to confirm: a setting may be read by
a library under its own name, a route may be linked from JavaScript, a
template may be chosen by a naming convention. ``frameworks`` says which
framework imports were seen. The plugin's live-registry recipe
(``docs/registry-diff.md``) confirms these leads against the booted app.

Limits: names are matched within the scanned files (point it at the package
and its templates); dotted resolution follows imports, falling back to a
unique simple name. Dynamic keys (f-strings, concatenation) are reported, not
resolved.

Usage:
    python map_wiring.py [path] [--max-files N]
"""

from __future__ import annotations

import ast
import configparser
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scan_common import (
    EXCLUDE_DIRS,
    build_envelope,
    collect_python_files,
    emit,
    parse_common_args,
    parse_source,
    relative_to_root,
    resolve_target,
)

_LOCS_CAP = 5
_USES_CAP = 8
_SNIPPET = 120

FRAMEWORK_PACKAGES = {
    "pyramid": "pyramid",
    "flask": "flask",
    "django": "django",
    "fastapi": "fastapi",
    "starlette": "starlette",
    "celery": "celery",
    "dramatiq": "dramatiq",
    "rq": "rq",
    "huey": "huey",
    "jinja2": "jinja2",
    "blinker": "blinker",
    "sqlalchemy": "sqlalchemy",
    "zope": "zope",
}

# --- settings ---------------------------------------------------------------
_MAPPING_METHODS = frozenset(
    {
        "get",
        "setdefault",
        "update",
        "items",
        "keys",
        "values",
        "pop",
        "copy",
        "configure",
        "configured",
        "from_object",
        "from_mapping",
        "from_envvar",
        "from_pyfile",
        "from_prefixed_env",
        "from_file",
        "model_dump",
        "dict",
        "json",
        "model_copy",
        "model_fields",
    }
)
# `set_from_env(settings, "key", ...)`, `set_default(settings, "key", ...)`
_SETTER_HELPER = re.compile(r"(?:^|_)set(?:_|$)")
_KEYLIKE = re.compile(r"^[A-Za-z_][\w.:-]{0,120}$")
_TEMPLATE_SETTING_READ = re.compile(
    r"\bsettings(?:\[\s*|\.get\(\s*)[\"']([^\"']+)[\"']|\bsettings\.([A-Za-z_]\w*)"
)
# Settings a framework supplies defaults for: a read with no project write is
# expected. Marked, not dropped.
_FRAMEWORK_DEFAULT_SETTINGS = frozenset(
    {
        "DEBUG",
        "TESTING",
        "SECRET_KEY",
        "ALLOWED_HOSTS",
        "INSTALLED_APPS",
        "MIDDLEWARE",
        "DATABASES",
        "CACHES",
        "TEMPLATES",
        "ROOT_URLCONF",
        "TIME_ZONE",
        "USE_TZ",
        "LANGUAGE_CODE",
        "STATIC_URL",
        "STATIC_ROOT",
        "MEDIA_URL",
        "MEDIA_ROOT",
        "AUTH_USER_MODEL",
        "LOGIN_URL",
        "LOGIN_REDIRECT_URL",
        "DEFAULT_FROM_EMAIL",
        "EMAIL_BACKEND",
        "SESSION_ENGINE",
        "DEFAULT_AUTO_FIELD",
        "SERVER_NAME",
        "APPLICATION_ROOT",
        "PREFERRED_URL_SCHEME",
        "MAX_CONTENT_LENGTH",
        "SESSION_COOKIE_NAME",
        "SESSION_COOKIE_SECURE",
        "CSRF_COOKIE_SECURE",
    }
)

# --- routes -----------------------------------------------------------------
_ROUTE_DECORATOR_METHODS = frozenset(
    {"route", "get", "post", "put", "delete", "patch", "head", "options"}
)
_FASTAPI_ONLY_METHODS = frozenset({"api_route", "websocket"})
_URL_BUILDERS = frozenset(
    {"route_path", "route_url", "url_for", "url_path_for", "reverse", "reverse_lazy"}
)
_TEMPLATE_URL_USE = re.compile(
    r"\b(?:route_path|route_url|url_for|url_path_for)\(\s*[\"']([^\"']+)[\"']"
    r"|\{%-?\s*url\s+[\"']([^\"']+)[\"']"
)
_NOT_ROUTE_DIRECTIVES = frozenset(
    {"add_route", "add_url_rule", "add_static_view", "add_settings", "add_view"}
)
_FASTAPI_FACTORIES = frozenset({"FastAPI", "APIRouter", "Starlette", "Router"})

# --- templates --------------------------------------------------------------
TEMPLATE_DIR_NAMES = frozenset({"templates", "jinja2"})
TEMPLATE_EXTS = (
    ".html",
    ".htm",
    ".jinja",
    ".jinja2",
    ".j2",
    ".txt",
    ".xml",
    ".pt",
    ".mako",
    ".mak",
    ".tmpl",
)
_TEMPLATE_LITERAL = re.compile(
    r"^[\w@./:{}%-]+\.(?:html|htm|jinja2?|j2|txt|xml|pt|mako|mak|tmpl)$"
)
_RENDER_CALLS = frozenset(
    {
        "render_template",
        "render_template_string",
        "render",
        "render_to_response",
        "render_to_string",
        "TemplateResponse",
        "get_template",
        "select_template",
    }
)
_SEGMENT = "([^/]+)"
_TEMPLATE_KEYWORDS = frozenset({"renderer", "template_name", "template"})
_TEMPLATE_EDGE = re.compile(
    r"\{%-?\s*(?:extends|include|import|from)\s+[\"']([^\"']+)[\"']"
)
_TEMPLATE_DYNAMIC_EDGE = re.compile(
    r"\{%-?\s*(?:extends|include|import|from)\s+(?![\"'])([^%]+?)-?%\}"
)

# --- tasks ------------------------------------------------------------------
_TASK_DECORATORS = frozenset(
    {
        "task",
        "shared_task",
        "actor",
        "periodic_task",
        "db_task",
        "db_periodic_task",
        "job",
    }
)
_ENQUEUE_ATTRS = frozenset(
    {"delay", "apply_async", "s", "si", "signature", "send", "send_with_options"}
)
_INJECTED_NAMES = frozenset({"request", "task", "ctx", "context", "self", "job"})
_NORMALISERS = re.compile(
    r"normali[sz]|canonical|lower|casefold|strip|slug|upper", re.IGNORECASE
)

# --- signals and permissions ----------------------------------------------
_SIGNAL_FACTORIES = frozenset({"Signal", "signal", "ModelSignal"})
_SEND_ATTRS = frozenset({"send", "send_robust", "asend", "send_async"})
_TEMPLATE_REQUEST_ATTR = re.compile(r"\brequest\.([A-Za-z_]\w*)")
_TEMPLATE_PERMISSION_CHECK = re.compile(
    r"\b(?:has_permission|has_perm)\(\s*(?:[\"']([^\"']+)[\"']|([A-Za-z_][\w.]*))"
)
_NO_PERMISSION = frozenset({"NO_PERMISSION_REQUIRED", "__no_permission_required__"})
_DJANGO_DEFAULT_PERM = re.compile(r"^(?:[\w]+\.)?(?:add|change|delete|view)_\w+$")


# ---------------------------------------------------------------------------
# Small AST helpers
# ---------------------------------------------------------------------------


def _str(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _dotted(node: ast.AST | None) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _terminal(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _call_name(node: ast.AST | None) -> str | None:
    return _terminal(node.func) if isinstance(node, ast.Call) else None


def _kw(call: ast.Call, name: str) -> ast.AST | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _snippet(node: ast.AST) -> str:
    text = " ".join(ast.unparse(node).split())
    return text if len(text) <= _SNIPPET else text[: _SNIPPET - 3] + "..."


def _perm_key(node: ast.AST | None) -> str | None:
    value = _str(node)
    if value is not None:
        return value
    return _dotted(node) if isinstance(node, ast.Attribute) else None


def _is_settings_receiver(node: ast.AST) -> bool:
    """`settings`, `x.settings`, `x_settings`, `get_settings()`, `app.config`."""
    if isinstance(node, ast.Call):
        name = _call_name(node) or ""
        return name.endswith("settings")
    name = _terminal(node)
    if name is None:
        return False
    if name == "settings" or name.endswith("_settings"):
        return True
    return isinstance(node, ast.Attribute) and name == "config"


def _fstring_pattern(node: ast.JoinedStr, value: str = ".+") -> tuple[str, str]:
    """(regex, literal prefix) for an f-string; formatted values match *value*."""
    regex, prefix, seen_value = "", "", False
    for piece in node.values:
        if isinstance(piece, ast.Constant) and isinstance(piece.value, str):
            regex += re.escape(piece.value)
            if not seen_value:
                prefix += piece.value
        else:
            regex += value
            seen_value = True
    return regex, prefix


def _module_name(rel: str) -> tuple[str, bool]:
    parts = list(Path(rel).with_suffix("").parts)
    if parts and parts[0] == "src":
        parts = parts[1:]
    is_pkg = bool(parts) and parts[-1] == "__init__"
    if is_pkg:
        parts = parts[:-1]
    return ".".join(parts), is_pkg


# ---------------------------------------------------------------------------
# Per-file collection
# ---------------------------------------------------------------------------


class _FileScan(ast.NodeVisitor):
    def __init__(self, rel: str, tree: ast.Module) -> None:
        self.rel = rel
        self.module, self.is_pkg = _module_name(rel)
        self.imports: dict[str, str] = {}
        self.frameworks: set[str] = set()
        self.parents: dict[ast.AST, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                self.parents[child] = node
        self.is_settings_module = "settings" in Path(rel).with_suffix("").parts
        # settings
        self.writes: list[tuple] = []
        self.reads: list[tuple] = []
        self.dynamic_reads: list[dict] = []
        self.dynamic_writes: list[dict] = []
        self.strings: dict[str, list[int]] = defaultdict(list)
        # routes
        self.route_decls: list[dict] = []
        self.views: list[dict] = []
        self.url_uses: list[tuple] = []
        self.app_objects: dict[str, str] = {}  # local name -> factory
        self.blueprints: dict[str, str] = {}  # local name -> blueprint name
        self.django_app_name: str | None = None
        # templates
        self.template_refs: list[dict] = []
        self.dynamic_template_refs: list[dict] = []
        # tasks
        self.tasks: list[dict] = []
        self.enqueues: list[dict] = []
        self.name_loads: list[tuple[str, int]] = []
        self.task_name_strings: list[tuple[str, int]] = []
        # signals
        self.signals: list[dict] = []
        self.sends: list[dict] = []
        self.connects: list[dict] = []
        self.subscribers: list[dict] = []
        self.notifies: list[dict] = []
        self.functions: dict[str, ast.AST] = {}
        self.classes: dict[str, ast.ClassDef] = {}
        # request methods: (name, line), and `request.<attr>` reads
        self.request_methods: list[tuple[str, int]] = []
        self.request_attr_reads: list[tuple[str, int]] = []
        # permissions
        self.perm_required: list[tuple] = []
        self.perm_granted: list[tuple] = []
        # methods whose @view_config already got their class's @view_defaults
        self._defaults_applied: set[int] = set()
        self.scan(tree)

    # -- resolution --------------------------------------------------------
    def _collect_imports(self, tree: ast.Module) -> None:
        pkg_parts = self.module.split(".") if self.module else []
        if not self.is_pkg and pkg_parts:
            pkg_parts = pkg_parts[:-1]
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    if top in FRAMEWORK_PACKAGES:
                        self.frameworks.add(FRAMEWORK_PACKAGES[top])
                    local = alias.asname or alias.name.split(".")[0]
                    self.imports[local] = alias.name if alias.asname else top
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    base_parts = pkg_parts[: len(pkg_parts) - (node.level - 1)]
                    base = ".".join(base_parts + ([node.module] if node.module else []))
                else:
                    base = node.module or ""
                top = base.split(".")[0]
                if top in FRAMEWORK_PACKAGES:
                    self.frameworks.add(FRAMEWORK_PACKAGES[top])
                from_settings = "settings" in base.split(".")
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    self.imports[alias.asname or alias.name] = (
                        f"{base}.{alias.name}" if base else alias.name
                    )
                    # `from app.settings import TIMEOUT` reads that setting.
                    if from_settings and alias.name.isupper():
                        self.reads.append(
                            (alias.name, self.rel, node.lineno, "import", False)
                        )

    def resolve(self, node: ast.AST | None) -> str | None:
        """Dotted name a Name/Attribute refers to, through this file's imports."""
        dotted = _dotted(node)
        if dotted is None:
            return None
        head, _, rest = dotted.partition(".")
        if head in self.imports:
            base = self.imports[head]
        elif head in self.functions or head in self.classes or not rest:
            base = f"{self.module}.{head}" if self.module else head
        else:
            base = head
        return f"{base}.{rest}" if rest else base

    # -- driver --------------------------------------------------------------
    def scan(self, tree: ast.Module) -> None:
        self._collect_imports(tree)
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.functions[node.name] = node
            elif isinstance(node, ast.ClassDef):
                self.classes[node.name] = node
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        self.functions.setdefault(f"{node.name}.{child.name}", child)
            elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                factory = _call_name(node.value)
                for target in node.targets:
                    if not isinstance(target, ast.Name):
                        continue
                    if (
                        factory in ("Flask", "Blueprint")
                        or factory in _FASTAPI_FACTORIES
                    ):
                        self.app_objects[target.id] = factory
                    if factory == "Blueprint":
                        name = _str(node.value.args[0]) if node.value.args else None
                        self.blueprints[target.id] = name or target.id
            if (
                isinstance(node, ast.Assign)
                and any(
                    isinstance(t, ast.Name) and t.id == "app_name" for t in node.targets
                )
                and _str(node.value)
            ):
                self.django_app_name = _str(node.value)
        self._module_level_settings(tree)
        self.visit(tree)

    def _loc(self, node: ast.AST) -> tuple[str, int]:
        return self.rel, getattr(node, "lineno", 0)

    # -- settings definitions that are declarations, not calls ---------------
    def _module_level_settings(self, tree: ast.Module) -> None:
        if self.is_settings_module:
            for node in tree.body:
                for name in _assigned_names(node):
                    if name.isupper() and not name.startswith("_"):
                        self.writes.append(
                            (name, self.rel, node.lineno, "settings_module")
                        )
        for cls in self.classes.values():
            bases = {_terminal(b) for b in cls.bases}
            if "BaseSettings" in bases:
                via = "settings_class"
                for child in cls.body:
                    for name in _assigned_names(child):
                        self.writes.append((name, self.rel, child.lineno, via))
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        self.writes.append((child.name, self.rel, child.lineno, via))
            elif cls.name.endswith(("Config", "Settings")):
                for child in cls.body:
                    for name in _assigned_names(child):
                        if name.isupper():
                            self.writes.append(
                                (name, self.rel, child.lineno, "config_class")
                            )

    # -- visitors ------------------------------------------------------------
    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            value = node.value
            if _KEYLIKE.match(value):
                self.strings[value].append(node.lineno)
            if _TEMPLATE_LITERAL.match(value) and "://" not in value:
                self._template_literal(node, value)

    def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
        last = node.values[-1] if node.values else None
        tail = _str(last) or ""
        head = _str(node.values[0]) if node.values else None
        if (
            tail.endswith(TEMPLATE_EXTS)
            and "://" not in tail
            and not (head or "").startswith("/")
        ):
            # A formatted value stands for one path segment: `f"{code}.html"`
            # matches `404.html`, not every template in every directory.
            regex, _prefix = _fstring_pattern(node, _SEGMENT)
            self.dynamic_template_refs.append(
                {
                    "pattern": regex,
                    "text": _snippet(node),
                    "file": self.rel,
                    "line": node.lineno,
                }
            )
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self.name_loads.append((node.id, node.lineno))

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.ctx, ast.Load):
            dotted = _dotted(node)
            if dotted:
                self.name_loads.append((dotted, node.lineno))
            if (_terminal(node.value) or "").endswith("request"):
                self.request_attr_reads.append((node.attr, node.lineno))
            parent = self.parents.get(node)
            if node.attr in ("delay", "apply_async") and not (
                isinstance(parent, ast.Call) and parent.func is node
            ):
                # `callback=wrapper(task).delay`: enqueued later by whoever
                # calls the bound method; the arguments are not visible here.
                target = node.value
                if isinstance(target, ast.Call) and target.args:
                    target = target.args[0]
                if isinstance(target, (ast.Name, ast.Attribute)):
                    rec = self._enqueue_record(
                        node, target, "passed as a callable", "none"
                    )
                    rec["kind"] = "callable"
                    self.enqueues.append(rec)
        if _is_settings_receiver(node.value) and node.attr not in _MAPPING_METHODS:
            parent = self.parents.get(node)
            is_call = isinstance(parent, ast.Call) and parent.func is node
            if isinstance(node.ctx, ast.Store):
                self.writes.append((node.attr, self.rel, node.lineno, "attribute"))
            elif (
                isinstance(node.ctx, ast.Load)
                and not is_call
                and _terminal(node.value) != "config"
            ):
                self.reads.append(
                    (node.attr, self.rel, node.lineno, "attribute", False)
                )
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if _is_settings_receiver(node.value):
            key = _str(node.slice)
            if key is not None:
                if isinstance(node.ctx, ast.Store):
                    self.writes.append((key, self.rel, node.lineno, "subscript"))
                elif isinstance(node.ctx, ast.Load):
                    self.reads.append((key, self.rel, node.lineno, "subscript", False))
            elif isinstance(node.ctx, ast.Store):
                self._dynamic_key(node.slice, node, self.dynamic_writes)
            else:
                self._dynamic_key(node.slice, node, self.dynamic_reads)
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        if (
            len(node.ops) == 1
            and isinstance(node.ops[0], (ast.In, ast.NotIn))
            and _is_settings_receiver(node.comparators[0])
            and _str(node.left) is not None
        ):
            self.reads.append((_str(node.left), self.rel, node.lineno, "in", True))
        self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict) -> None:
        for key, value in zip(node.keys, node.values):
            if _str(key) == "task" and _str(value):
                self.task_name_strings.append((_str(value), node.lineno))
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        factory = _call_name(node.value)
        if (
            factory in _SIGNAL_FACTORIES
            and _dotted(getattr(node.value, "func", None)) != "signal.signal"
        ):
            owner = self._enclosing_class(node)
            for target in node.targets:
                if isinstance(target, ast.Name):
                    name = f"{owner}.{target.id}" if owner else target.id
                    self.signals.append(
                        {
                            "dotted": f"{self.module}.{name}",
                            "name": name,
                            "file": self.rel,
                            "line": node.lineno,
                        }
                    )
        # Django `permission_required = "app.perm"` on a view class.
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "permission_required":
                for elt in _iter_values(node.value):
                    if _perm_key(elt):
                        self.perm_required.append(
                            (
                                _perm_key(elt),
                                self.rel,
                                node.lineno,
                                "permission_required",
                            )
                        )
            if (
                isinstance(target, ast.Name)
                and target.id == "permissions"
                and self._enclosing_class(node) == "Meta"
            ):
                for elt in _iter_values(node.value):
                    if isinstance(elt, ast.Tuple) and elt.elts and _str(elt.elts[0]):
                        self.perm_granted.append(
                            (
                                _str(elt.elts[0]),
                                self.rel,
                                node.lineno,
                                "Meta.permissions",
                            )
                        )
        self.generic_visit(node)

    def visit_Tuple(self, node: ast.Tuple) -> None:
        # Pyramid ACL entry: (Allow, principal, permission-or-permissions)
        if len(node.elts) >= 3 and _terminal(node.elts[0]) == "Allow":
            for elt in self._acl_values(node, node.elts[2]):
                key = _perm_key(elt)
                if key and key != "ALL_PERMISSIONS":
                    self.perm_granted.append((key, self.rel, node.lineno, "acl"))
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._function_decorators(node)
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        defaults: dict[str, ast.AST] = {}
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call) and _call_name(dec) == "view_defaults":
                defaults = {kw.arg: kw.value for kw in dec.keywords if kw.arg}
            if isinstance(dec, ast.Call) and _is_view_config(_call_name(dec)):
                self._view_config(dec, node, f"{self.module}.{node.name}", {})
        if defaults:
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for dec in child.decorator_list:
                        if isinstance(dec, ast.Call) and _is_view_config(
                            _call_name(dec)
                        ):
                            self._view_config(
                                dec,
                                child,
                                f"{self.module}.{node.name}.{child.name}",
                                defaults,
                            )
            self._defaults_applied |= {id(c) for c in node.body}
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name = _terminal(func)
        if isinstance(func, ast.Attribute):
            self._attribute_call(node, func)
        if name is not None:
            self._named_call(node, name)
        self.generic_visit(node)

    # -- call handlers -------------------------------------------------------
    def _acl_values(self, entry: ast.AST, value: ast.AST) -> list[ast.AST]:
        """Permissions of an ACL entry; a variable is resolved to the list
        literals assigned (or appended/extended) to it in the same scope."""
        if not isinstance(value, ast.Name):
            return list(_iter_values(value))
        scope = self.parents.get(entry)
        while scope is not None and not isinstance(
            scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)
        ):
            scope = self.parents.get(scope)
        out: list[ast.AST] = []
        for sub in ast.walk(scope) if scope is not None else ():
            if isinstance(sub, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == value.id for t in sub.targets
            ):
                out.extend(
                    _iter_values(sub.value)
                    if isinstance(sub.value, (ast.List, ast.Tuple, ast.Set))
                    else []
                )
            elif (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and isinstance(sub.func.value, ast.Name)
                and sub.func.value.id == value.id
                and sub.func.attr in ("append", "add", "extend")
                and sub.args
            ):
                arg = sub.args[0]
                out.extend(_iter_values(arg) if sub.func.attr == "extend" else [arg])
        return out

    def _is_decorator(self, node: ast.AST) -> bool:
        parent = self.parents.get(node)
        return (
            isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node in parent.decorator_list
        )

    def _attribute_call(self, node: ast.Call, func: ast.Attribute) -> None:
        attr, recv = func.attr, func.value
        settings_recv = _is_settings_receiver(recv)
        # settings reads/writes through mapping methods
        if settings_recv and attr in ("get", "pop") and node.args:
            key = _str(node.args[0])
            if key is not None:
                has_default = len(node.args) > 1 or bool(node.keywords)
                self.reads.append((key, self.rel, node.lineno, attr, has_default))
            else:
                self._dynamic_key(node.args[0], node, self.dynamic_reads)
        elif (
            settings_recv and attr == "setdefault" and node.args and _str(node.args[0])
        ):
            self.writes.append(
                (_str(node.args[0]), self.rel, node.lineno, "setdefault")
            )
        elif settings_recv and attr in ("update", "from_mapping", "configure"):
            self._dict_writes(node, attr)
        if attr == "add_settings":
            self._dict_writes(node, "add_settings")
        # routes
        if attr == "add_route" and node.args and _str(node.args[0]):
            self._route(
                node, _str(node.args[0]), _str(_arg(node, 1, "pattern")), "add_route"
            )
        elif _custom_route_directive(attr, node):
            # `add_page_view("name", "/pattern", ...)` registers its own
            # view; `add_<x>_route("name", ...)` only a route, like add_route.
            pattern = _str(_arg(node, 1, "pattern"))
            self._route(
                node,
                _str(node.args[0]),
                pattern if (pattern or "").startswith("/") else None,
                attr,
                implicit_view=not attr.endswith("_route"),
            )
        elif attr == "add_url_rule" and node.args and _str(node.args[0]):
            endpoint = _str(_arg(node, 1, "endpoint"))
            view = _arg(node, 2, "view_func")
            if endpoint is None and view is not None:
                endpoint = _terminal(view)
            if endpoint:
                self._route(
                    node,
                    self._blueprint_endpoint(recv, endpoint),
                    _str(node.args[0]),
                    "add_url_rule",
                    implicit_view=True,
                )
        elif attr == "add_request_method" and node.args:
            name = _str(_arg(node, 1, "name")) or _terminal(node.args[0])
            if name:
                self.request_methods.append((name, node.lineno))
        elif attr == "add_view":
            route = _str(_kw(node, "route_name"))
            view = node.args[0] if node.args else _kw(node, "view")
            self.views.append(
                {
                    "route_name": route,
                    "view": self.resolve(view) if view is not None else None,
                    "file": self.rel,
                    "line": node.lineno,
                    "renderer": _str(_kw(node, "renderer")),
                    "permission": _perm_key(_kw(node, "permission")),
                    "request_method": _methods(_kw(node, "request_method")),
                }
            )
        elif attr == "add_subscriber" and node.args:
            handler = node.args[0]
            event = _arg(node, 1, "iface")
            if event is not None:
                self.subscribers.append(
                    {
                        "event": self.resolve(event),
                        "handler": self.resolve(handler),
                        "handler_local": _dotted(handler),
                        "file": self.rel,
                        "line": node.lineno,
                    }
                )
        elif attr == "notify" and node.args and isinstance(node.args[0], ast.Call):
            self.notifies.append(
                {
                    "event": self.resolve(node.args[0].func),
                    "file": self.rel,
                    "line": node.lineno,
                }
            )
        # signals
        if attr in _SEND_ATTRS:
            self.sends.append(
                {
                    "target": self.resolve(recv),
                    "keys": sorted(kw.arg for kw in node.keywords if kw.arg),
                    "file": self.rel,
                    "line": node.lineno,
                }
            )
        if (
            attr in ("connect", "connect_via")
            and node.args
            and not self._is_decorator(node)
        ):
            self.connects.append(
                {
                    "signal": self.resolve(recv),
                    "handler": self.resolve(node.args[0]),
                    "handler_local": _dotted(node.args[0]),
                    "file": self.rel,
                    "line": node.lineno,
                }
            )
        # tasks: X.delay(...), wrapper(X).delay(...)
        if attr in _ENQUEUE_ATTRS:
            target, via = recv, attr
            if isinstance(recv, ast.Call) and recv.args:
                target, via = recv.args[0], f"{_call_name(recv)}(...).{attr}"
            self._enqueue(node, target, via, attr)

    def _named_call(self, node: ast.Call, name: str) -> None:
        # settings: getattr(settings, "K"[, default]) / hasattr
        if name in ("getattr", "hasattr") and len(node.args) >= 2:
            if _is_settings_receiver(node.args[0]) and _str(node.args[1]):
                has_default = name == "hasattr" or len(node.args) > 2
                self.reads.append(
                    (_str(node.args[1]), self.rel, node.lineno, name, has_default)
                )
        # settings: a setter helper taking the settings mapping and a key
        elif (
            _SETTER_HELPER.search(name.lower())
            and len(node.args) >= 2
            and _is_settings_receiver(node.args[0])
            and _str(node.args[1])
        ):
            self.writes.append(
                (_str(node.args[1]), self.rel, node.lineno, f"helper:{name}")
            )
        # routes: Django path()/re_path()/url(), Starlette Route()
        if name in ("path", "re_path", "url") and _str(_kw(node, "name")):
            route = _str(_kw(node, "name"))
            if self.django_app_name:
                route = f"{self.django_app_name}:{route}"
            pattern = _str(node.args[0]) if node.args else None
            view = node.args[1] if len(node.args) > 1 else None
            self._route(node, route, pattern, name, implicit_view=True, view=view)
        elif name in ("Route", "WebSocketRoute") and node.args and _str(node.args[0]):
            endpoint = _arg(node, 1, "endpoint")
            route = _str(_kw(node, "name")) or _terminal(endpoint)
            if route:
                self._route(
                    node,
                    route,
                    _str(node.args[0]),
                    name,
                    implicit_view=True,
                    view=endpoint,
                )
        # URL builders
        first = _str(node.args[0]) if node.args else None
        # Django's `redirect("name")` takes a route name or a URL.
        is_django_redirect = (
            name == "redirect"
            and "django" in self.frameworks
            and "/" not in (first or "/")
        )
        if first and (name in _URL_BUILDERS or is_django_redirect):
            self.url_uses.append((first, self.rel, node.lineno, name))
        # tasks (explicit template render calls are handled by visit_Constant)
        if name == "send_task" and node.args and _str(node.args[0]):
            self.enqueues.append(
                self._enqueue_record(
                    node, None, "send_task", "apply_async", _str(node.args[0])
                )
            )
        elif name == "enqueue" and node.args:
            self._enqueue(node, node.args[0], "enqueue", "enqueue")
        elif name == "add_periodic_task":
            for arg in node.args:
                if isinstance(arg, (ast.Name, ast.Attribute)):
                    rec = self._enqueue_record(node, arg, "add_periodic_task", "none")
                    self.enqueues.append(rec)
        # permissions
        perm = _kw(node, "permission")
        if (
            perm is not None
            and _perm_key(perm)
            and _perm_key(perm) not in _NO_PERMISSION
        ):
            self.perm_required.append(
                (_perm_key(perm), self.rel, node.lineno, f"{name}(permission=)")
            )
        if (
            name in ("has_permission", "permission_required", "has_perm", "permits")
            and node.args
        ):
            for elt in _iter_values(node.args[0]):
                if _perm_key(elt):
                    self.perm_required.append(
                        (_perm_key(elt), self.rel, node.lineno, name)
                    )

    def _function_decorators(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> None:
        qual = self._qualname(node)
        for dec in node.decorator_list:
            call = dec if isinstance(dec, ast.Call) else None
            target = dec.func if call else dec
            dname = _terminal(target)
            # Pyramid view_config on a function (class-level defaults handled
            # in visit_ClassDef).
            if (
                call
                and _is_view_config(dname)
                and id(node) not in self._defaults_applied
            ):
                self._view_config(call, node, f"{self.module}.{qual}", {})
            # Flask / FastAPI route decorators
            elif (
                call
                and isinstance(target, ast.Attribute)
                and (
                    dname in _ROUTE_DECORATOR_METHODS or dname in _FASTAPI_ONLY_METHODS
                )
            ):
                owner = _terminal(target.value) or ""
                factory = self.app_objects.get(owner)
                is_fastapi = (
                    dname in _FASTAPI_ONLY_METHODS
                    or factory in _FASTAPI_FACTORIES
                    or (
                        factory is None
                        and (
                            "fastapi" in self.frameworks
                            or "starlette" in self.frameworks
                        )
                    )
                )
                is_flask = factory in ("Flask", "Blueprint") or (
                    factory is None and "flask" in self.frameworks
                )
                if not (is_fastapi or is_flask):
                    continue
                pattern = (
                    _str(call.args[0])
                    if call.args
                    else _str(_kw(call, "path") or _kw(call, "rule"))
                )
                if pattern is None:
                    continue
                if is_fastapi:
                    route = _str(_kw(call, "name")) or node.name
                else:
                    route = self._blueprint_endpoint(
                        target.value, _str(_kw(call, "endpoint")) or node.name
                    )
                methods = (
                    [dname.upper()]
                    if dname not in ("route", "api_route", "websocket")
                    else _methods(_kw(call, "methods"))
                )
                self._route(
                    call,
                    route,
                    pattern,
                    f"@{owner}.{dname}",
                    implicit_view=True,
                    view_name=f"{self.module}.{qual}",
                    methods=methods,
                )
            # tasks
            if dname in _TASK_DECORATORS:
                self._task(node, call, qual)
            # signal handlers
            if dname == "receiver" and call and call.args:
                for sig in _iter_values(call.args[0]):
                    self.connects.append(
                        {
                            "signal": self.resolve(sig),
                            "handler": f"{self.module}.{qual}",
                            "handler_local": qual,
                            "file": self.rel,
                            "line": node.lineno,
                        }
                    )
            elif dname in ("connect", "connect_via") and isinstance(
                target, ast.Attribute
            ):
                self.connects.append(
                    {
                        "signal": self.resolve(target.value),
                        "handler": f"{self.module}.{qual}",
                        "handler_local": qual,
                        "file": self.rel,
                        "line": node.lineno,
                    }
                )
            elif dname == "subscriber" and call and call.args:
                for event in call.args:
                    self.subscribers.append(
                        {
                            "event": self.resolve(event),
                            "handler": f"{self.module}.{qual}",
                            "handler_local": qual,
                            "file": self.rel,
                            "line": node.lineno,
                        }
                    )

    # -- record builders -----------------------------------------------------
    def _qualname(self, node: ast.AST) -> str:
        owner = self._enclosing_class(node)
        return f"{owner}.{node.name}" if owner else node.name  # type: ignore[attr-defined]

    def _enclosing_class(self, node: ast.AST) -> str | None:
        parent = self.parents.get(node)
        while parent is not None:
            if isinstance(parent, ast.ClassDef):
                return parent.name
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return None
            parent = self.parents.get(parent)
        return None

    def _blueprint_endpoint(self, recv: ast.AST, endpoint: str) -> str:
        owner = _terminal(recv)
        if owner in self.blueprints and "." not in endpoint:
            return f"{self.blueprints[owner]}.{endpoint}"
        return endpoint

    def _route(
        self,
        node: ast.Call,
        name: str,
        pattern: str | None,
        via: str,
        *,
        implicit_view: bool = False,
        view: ast.AST | None = None,
        view_name: str | None = None,
        methods: list[str] | None = None,
    ) -> None:
        self.route_decls.append(
            {
                "name": name,
                "pattern": pattern,
                "via": via,
                "file": self.rel,
                "line": node.lineno,
                "implicit_view": implicit_view,
                "view": view_name or (self.resolve(view) if view is not None else None),
                "methods": methods
                if methods is not None
                else _methods(_kw(node, "request_method")),
            }
        )

    def _view_config(
        self, call: ast.Call, node: ast.AST, view: str, defaults: dict
    ) -> None:
        def get(key: str) -> ast.AST | None:
            value = _kw(call, key)
            return value if value is not None else defaults.get(key)

        self.views.append(
            {
                "route_name": _str(get("route_name")),
                "view": view,
                "file": self.rel,
                "line": call.lineno,
                "renderer": _str(get("renderer")),
                "permission": _perm_key(get("permission")),
                "request_method": _methods(get("request_method")),
            }
        )
        if _kw(call, "permission") is None and defaults.get("permission") is not None:
            key = _perm_key(defaults["permission"])
            if key and key not in _NO_PERMISSION:
                self.perm_required.append(
                    (key, self.rel, call.lineno, "view_defaults(permission=)")
                )

    def _template_literal(self, node: ast.Constant, value: str) -> None:
        parent = self.parents.get(node)
        explicit = False
        if isinstance(parent, ast.keyword) and parent.arg in _TEMPLATE_KEYWORDS:
            explicit = True
        elif isinstance(parent, ast.Call) and _call_name(parent) in _RENDER_CALLS:
            explicit = parent.args[:1] == [node]
        if value.startswith("/"):
            return  # a URL path such as "/feed/{name}.xml"
        if "{" in value or "%s" in value:
            regex = re.sub(r"\\\{[^}]*\\\}|%s", _SEGMENT, re.escape(value))
            self.dynamic_template_refs.append(
                {"pattern": regex, "text": value, "file": self.rel, "line": node.lineno}
            )
            return
        self.template_refs.append(
            {"ref": value, "file": self.rel, "line": node.lineno, "explicit": explicit}
        )

    def _dict_writes(self, node: ast.Call, via: str) -> None:
        for arg in node.args:
            if isinstance(arg, ast.Dict):
                for key in arg.keys:
                    if _str(key) is not None:
                        self.writes.append((_str(key), self.rel, node.lineno, via))
        for kw in node.keywords:
            if kw.arg:
                self.writes.append((kw.arg, self.rel, node.lineno, via))

    def _dynamic_key(self, key: ast.AST, node: ast.AST, into: list[dict]) -> None:
        if isinstance(key, ast.JoinedStr):
            regex, prefix = _fstring_pattern(key)
        elif (
            isinstance(key, ast.BinOp)
            and isinstance(key.op, ast.Add)
            and _str(key.left) is not None
        ):
            regex, prefix = re.escape(_str(key.left)) + ".+", _str(key.left)
        else:
            return
        into.append(
            {
                "pattern": regex,
                "prefix": prefix,
                "text": _snippet(key),
                "file": self.rel,
                "line": node.lineno,
            }
        )

    def _task(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        call: ast.Call | None,
        qual: str,
    ) -> None:
        bind = bool(
            call is not None
            and isinstance(_kw(call, "bind"), ast.Constant)
            and _kw(call, "bind").value is True
        )
        self.tasks.append(
            {
                "node": node,
                "qualname": qual,
                "dotted": f"{self.module}.{qual}",
                "task_name": _str(_kw(call, "name")) if call is not None else None,
                "bind": bind,
                "file": self.rel,
                "line": node.lineno,
            }
        )

    def _enqueue(self, node: ast.Call, target: ast.AST, via: str, attr: str) -> None:
        if isinstance(target, (ast.Name, ast.Attribute)):
            self.enqueues.append(self._enqueue_record(node, target, via, attr))

    def _enqueue_record(
        self,
        node: ast.AST,
        target: ast.AST | None,
        via: str,
        attr: str,
        name: str | None = None,
    ) -> dict:
        positional: list[ast.AST] | None
        keywords: dict[str, ast.AST] | None
        splat = False
        if attr in ("apply_async", "signature"):
            args_node = _arg(node, 1 if via == "send_task" else 0, "args")
            kwargs_node = _arg(node, 2 if via == "send_task" else 1, "kwargs")
            positional = (
                list(args_node.elts)
                if isinstance(args_node, (ast.List, ast.Tuple))
                else ([] if args_node is None else None)
            )
            if isinstance(kwargs_node, ast.Dict) and all(
                _str(k) is not None for k in kwargs_node.keys
            ):
                keywords = {
                    _str(k): v for k, v in zip(kwargs_node.keys, kwargs_node.values)
                }
            else:
                keywords = {} if kwargs_node is None else None
        elif attr == "none":
            positional, keywords = [], {}
        else:
            args = node.args[1:] if attr == "enqueue" else node.args
            positional = [a for a in args if not isinstance(a, ast.Starred)]
            keywords = {kw.arg: kw.value for kw in node.keywords if kw.arg}
            splat = len(positional) != len(args) or any(
                kw.arg is None for kw in node.keywords
            )
        return {
            "target": self.resolve(target) if target is not None else None,
            "target_local": _dotted(target) if target is not None else None,
            "task_name": name,
            "via": via,
            "kind": attr,
            "file": self.rel,
            "line": node.lineno,
            "positional": positional,
            "keywords": keywords,
            "splat": splat,
        }


def _is_view_config(name: str | None) -> bool:
    """`view_config` and project wrappers of it (`api_view_config`)."""
    return bool(name) and name.endswith("view_config")


def _custom_route_directive(attr: str, node: ast.Call) -> bool:
    """A project directive declaring a named route: `add_x_route("name", ...)`,
    or any `add_*("name", "/pattern")` / `add_*("name", pattern="/p")`."""
    if not attr.startswith("add_") or attr in _NOT_ROUTE_DIRECTIVES:
        return False
    name = _str(node.args[0]) if node.args else None
    if not name or name.startswith("/"):
        return False
    if attr.endswith("_route"):
        return True
    return (_str(_arg(node, 1, "pattern")) or "").startswith("/")


def _arg(call: ast.Call, index: int, keyword: str) -> ast.AST | None:
    if len(call.args) > index and not isinstance(call.args[index], ast.Starred):
        return call.args[index]
    return _kw(call, keyword)


def _iter_values(node: ast.AST | None):
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        yield from node.elts
    elif node is not None:
        yield node


def _methods(node: ast.AST | None) -> list[str] | None:
    if node is None:
        return None
    values = [_str(v) for v in _iter_values(node)]
    return [v.upper() for v in values if v] or None


def _assigned_names(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Assign):
        return [t.id for t in node.targets if isinstance(t, ast.Name)]
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return [node.target.id]
    return []


# ---------------------------------------------------------------------------
# Project-level assembly
# ---------------------------------------------------------------------------


def _locs(items: list[tuple], cap: int = _LOCS_CAP) -> list[str]:
    return [f"{f}:{line}" for f, line in items[:cap]]


def _inside_quotes(text: str, pos: int) -> bool:
    """True when *pos* sits inside a quoted string on its line."""
    line = text[text.rfind("\n", 0, pos) + 1 : pos]
    return line.count("'") % 2 == 1 or line.count('"') % 2 == 1


def _not_detected(reason: str) -> dict:
    return {"status": "not_detected", "reason": reason}


def _ini_writes(project_root: Path) -> list[tuple]:
    out: list[tuple] = []
    for path in sorted(project_root.glob("*.ini")):
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            parser.read(path, encoding="utf-8")
        except (configparser.Error, OSError, UnicodeDecodeError):
            continue
        for section in parser.sections():
            if not section.startswith("app:"):
                continue
            for key in parser[section]:
                # `use = egg:...` is a PasteDeploy directive, not a setting.
                if key in parser.defaults() or key == "use":
                    continue
                out.append((key, path.name, 0, "ini"))
    return out


def _settings_section(
    scans: list[_FileScan], templates: dict, project_root: Path, frameworks: set[str]
) -> dict:
    writes: dict[str, list[tuple]] = defaultdict(list)
    for s in scans:
        for key, f, line, via in s.writes:
            writes[key].append((f, line, via))
    for key, f, line, via in _ini_writes(project_root):
        writes[key].append((f, line, via))
    reads: dict[str, list[tuple]] = defaultdict(list)
    for s in scans:
        for key, f, line, via, has_default in s.reads:
            reads[key].append((f, line, via, has_default))
        # A settings-module constant used by that module itself is read.
        own = {k for k, _f, _l, via in s.writes if via == "settings_module"}
        for name, line in s.name_loads:
            if name in own:
                reads[name].append((s.rel, line, "same_module", False))
    for rel, text in templates["texts"].items():
        for m in _TEMPLATE_SETTING_READ.finditer(text):
            key = m.group(1) or m.group(2)
            if not key or key in _MAPPING_METHODS or _inside_quotes(text, m.start()):
                continue  # `'x.settings.y'` is a route name, not a read
            line = text.count("\n", 0, m.start()) + 1
            reads[key].append((rel, line, "template", m.group(1) is None))
    dynamic = [d for s in scans for d in s.dynamic_reads]
    dynamic_writes = [d for s in scans for d in s.dynamic_writes]
    if not writes and not reads:
        return _not_detected(
            "no settings writes or reads recognised (no `settings[...]`, "
            "`app.config[...]`, settings module, *Config class or [app:*] ini section)"
        )

    # A dynamic key with almost no literal text (`f"{base}.{name}"`) matches
    # every key; it is listed but does not make any key "possibly read".
    def anchored(d: dict) -> bool:
        return len(re.sub(r"[^A-Za-z0-9]", "", d["prefix"])) >= 3

    def matcher(items: list[dict]):
        compiled = [(d, re.compile(d["pattern"])) for d in items if anchored(d)]

        def match(key: str) -> list[dict]:
            return [
                {"text": d["text"], "at": f"{d['file']}:{d['line']}"}
                for d, rx in compiled
                if rx.fullmatch(key)
            ]

        return match

    dynamic_match = matcher(dynamic)
    dynamic_write_match = matcher(dynamic_writes)

    write_lines = {(f, line) for v in writes.values() for f, line, _ in v}
    strings: dict[str, list[tuple]] = defaultdict(list)
    for s in scans:
        for value, lines in s.strings.items():
            for line in lines:
                if (s.rel, line) not in write_lines:
                    strings[value].append((s.rel, line))

    never_read, indirect = [], []
    for key in sorted(writes):
        if key in reads:
            continue
        if any(r.startswith(key + ".") for r in reads):
            continue  # a compound base: `a` written, `a.b` read
        entry = {
            "key": key,
            "written_at": [f"{f}:{line}" for f, line, _ in writes[key][:_LOCS_CAP]],
            "via": sorted({v for _, _, v in writes[key]}),
        }
        mentions = strings.get(key, [])
        dyn = dynamic_match(key)
        if mentions or dyn:
            entry["mentioned_at"] = _locs(mentions)
            entry["dynamic_reads"] = dyn[:_LOCS_CAP]
            indirect.append(entry)
        else:
            never_read.append(entry)

    namespaces = frameworks | {"pyramid", "jinja2", "celery", "sqlalchemy"}
    not_written = []
    for key in sorted(reads):
        if key in writes or any(key.startswith(w + ".") for w in writes):
            continue
        sites = reads[key]
        entry = {
            "key": key,
            "read_at": [f"{f}:{line}" for f, line, _, _ in sites[:_LOCS_CAP]],
            "default_at_every_read": all(d for _, _, _, d in sites),
            "framework_default": key in _FRAMEWORK_DEFAULT_SETTINGS
            or key.split(".")[0] in namespaces,
        }
        maybe = dynamic_write_match(key)
        if maybe:
            entry["possibly_written_dynamically"] = maybe[:_LOCS_CAP]
        not_written.append(entry)
    not_written.sort(
        key=lambda e: (e["default_at_every_read"], e["framework_default"], e["key"])
    )
    return {
        "status": "detected",
        "keys_written": len(writes),
        "keys_read": len(reads),
        "dynamic_reads": [
            {
                "text": d["text"],
                "at": f"{d['file']}:{d['line']}",
                "anchored": anchored(d),
            }
            for d in dynamic
        ],
        "written_never_read": never_read,
        "written_read_only_indirectly": indirect,
        "read_never_written": not_written,
    }


def _routes_section(scans: list[_FileScan], templates: dict) -> dict:
    decls = [d for s in scans for d in s.route_decls]
    views = [v for s in scans for v in s.views]
    uses = [u for s in scans for u in s.url_uses]
    for rel, text in templates["texts"].items():
        for m in _TEMPLATE_URL_USE.finditer(text):
            uses.append(
                (
                    m.group(1) or m.group(2),
                    rel,
                    text.count("\n", 0, m.start()) + 1,
                    "template",
                )
            )
    if not decls and not views:
        return _not_detected(
            "no route declarations recognised (add_route, @app.route/@bp.get, "
            "add_url_rule, path(name=), Route(name=), @router.get, @view_config)"
        )
    declared: dict[str, dict] = {}
    for d in decls:
        declared.setdefault(d["name"], d)
    views_by_route: dict[str, list[dict]] = defaultdict(list)
    for v in views:
        if v["route_name"]:
            views_by_route[v["route_name"]].append(v)
    used: dict[str, list[tuple]] = defaultdict(list)
    for name, f, line, _via in uses:
        used[name].append((f, line))

    def is_used(name: str) -> bool:
        if name in used:
            return True
        # Flask `url_for(".endpoint")` inside a blueprint.
        return any(u.startswith(".") and name.endswith(u) for u in used)

    routes = []
    for name, d in sorted(declared.items()):
        attached = views_by_route.get(name, [])
        routes.append(
            {
                "name": name,
                "pattern": d["pattern"],
                "methods": d["methods"]
                or sorted({m for v in attached for m in (v["request_method"] or [])})
                or None,
                "via": d["via"],
                "declared_at": f"{d['file']}:{d['line']}",
                "views": [v["view"] for v in attached][:_LOCS_CAP]
                or ([d["view"]] if d["view"] else []),
                "permissions": sorted(
                    {v["permission"] for v in attached if v["permission"]}
                ),
                "renderers": sorted({v["renderer"] for v in attached if v["renderer"]}),
            }
        )
    without_view = [
        {"name": n, "declared_at": f"{d['file']}:{d['line']}", "pattern": d["pattern"]}
        for n, d in sorted(declared.items())
        if not d["implicit_view"] and n not in views_by_route
    ]
    undeclared_views = [
        {
            "route_name": v["route_name"],
            "view": v["view"],
            "at": f"{v['file']}:{v['line']}",
        }
        for v in views
        if v["route_name"] and v["route_name"] not in declared
    ]
    unknown_uses = []
    for name, sites in sorted(used.items()):
        if name in declared or (
            name.startswith(".") and any(n.endswith(name) for n in declared)
        ):
            continue
        unknown_uses.append(
            {
                "name": name,
                "used_at": _locs(sites),
                "framework_route": name in ("static",) or name.startswith("admin:"),
            }
        )
    never_linked = [n for n in sorted(declared) if not is_used(n)]
    return {
        "status": "detected",
        "declared": routes,
        "routes_without_view": without_view,
        "views_for_undeclared_routes": undeclared_views,
        "url_names_not_declared": unknown_uses,
        "declared_never_linked": never_linked,
    }


def _discover_templates(scan_root: Path, project_root: Path) -> dict:
    def roots_under(base: Path) -> list[Path]:
        if not base.is_dir():
            return []
        found = []
        for path in sorted(base.rglob("*")):
            if not path.is_dir() or path.name not in TEMPLATE_DIR_NAMES:
                continue
            rel_parts = path.relative_to(base).parts
            if set(rel_parts) & EXCLUDE_DIRS or "node_modules" in rel_parts:
                continue
            if any(
                p.name in TEMPLATE_DIR_NAMES
                for p in path.parents
                if p != base and base in p.parents
            ):
                continue  # nested inside another template root
            found.append(path)
        return found

    roots = roots_under(scan_root) or roots_under(project_root)
    files: dict[str, list[str]] = defaultdict(list)  # name -> [project-relative paths]
    texts: dict[str, str] = {}
    for root in roots:
        for path in sorted(root.rglob("*")):
            if not path.is_file() or not path.name.endswith(TEMPLATE_EXTS):
                continue
            name = path.relative_to(root).as_posix()
            rel = relative_to_root(path, project_root)
            files[name].append(rel)
            try:
                texts[rel] = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
    return {
        "roots": [relative_to_root(r, project_root) for r in roots],
        "files": files,
        "texts": texts,
    }


def _normalise_template_ref(ref: str) -> str:
    ref = ref.rsplit(":", 1)[-1]
    if "templates/" in ref:
        ref = ref.rsplit("templates/", 1)[-1]
    return ref.removeprefix("./")


def _templates_section(scans: list[_FileScan], templates: dict) -> dict:
    files: dict[str, list[str]] = templates["files"]
    if not templates["roots"]:
        return _not_detected(
            "no `templates` or `jinja2` directory under the scan or project root"
        )
    if not files:
        return _not_detected(
            f"template directories {templates['roots']} hold no template files"
        )
    rel_to_name = {rel: name for name, rels in files.items() for rel in rels}
    edges: dict[str, set[str]] = defaultdict(set)
    explicit_refs: list[tuple] = []
    dynamic_edges = []
    for rel, text in templates["texts"].items():
        source = rel_to_name.get(rel)
        for m in _TEMPLATE_EDGE.finditer(text):
            target = _normalise_template_ref(m.group(1))
            # Some loaders resolve a name relative to the including template
            # first (`{% extends "base.xml" %}` inside `feeds/`).
            sibling = (
                f"{source.rsplit('/', 1)[0]}/{target}"
                if source and "/" in source
                else None
            )
            if target not in files and sibling in files:
                target = sibling
            edges[source].add(target)
            explicit_refs.append((target, rel, text.count("\n", 0, m.start()) + 1))
        for m in _TEMPLATE_DYNAMIC_EDGE.finditer(text):
            dynamic_edges.append(
                {
                    "text": " ".join(m.group(0).split())[:_SNIPPET],
                    "at": f"{rel}:{text.count(chr(10), 0, m.start()) + 1}",
                }
            )
    python_refs: dict[str, list[tuple]] = defaultdict(list)
    for s in scans:
        for r in s.template_refs:
            name = _normalise_template_ref(r["ref"])
            python_refs[name].append((r["file"], r["line"]))
            if r["explicit"]:
                explicit_refs.append((name, r["file"], r["line"]))
    # A pattern is normalised like a literal (asset-spec prefix and leading
    # `.../templates/` dropped) before matching template names.
    patterns = [
        (d, re.compile(_normalise_template_ref(d["pattern"])))
        for s in scans
        for d in s.dynamic_template_refs
    ]

    def dynamic_hits(name: str) -> list[str]:
        return [f"{d['file']}:{d['line']}" for d, rx in patterns if rx.fullmatch(name)]

    mentioned = {v for s in scans for v in s.strings} | {
        v for s in scans for r in s.template_refs for v in [r["ref"]]
    }

    def captured(name: str) -> list[str]:
        values: list[str] = []
        for _d, rx in patterns:
            m = rx.fullmatch(name)
            if m:
                values.extend(g for g in m.groups() if g)
        return sorted(set(values))

    roots = {n for n in python_refs if n in files}
    dynamic_only = {}
    for name in files:
        if name not in roots:
            hits = dynamic_hits(name)
            if hits:
                dynamic_only[name] = hits
    reached = set(roots) | set(dynamic_only)
    frontier = list(reached)
    while frontier:
        current = frontier.pop()
        for target in edges.get(current, ()):
            if target in files and target not in reached:
                reached.add(target)
                frontier.append(target)
    included_by: dict[str, set[str]] = defaultdict(set)
    for source, targets in edges.items():
        for t in targets:
            if source:
                included_by[t].add(source)
    unreferenced = [
        {
            "template": name,
            "paths": files[name],
            "included_by": sorted(included_by.get(name, ()))[:_LOCS_CAP],
        }
        for name in sorted(files)
        if name not in reached
    ]
    missing = []
    seen_missing = set()
    for name, f, line in explicit_refs:
        if name in files or name in seen_missing or dynamic_hits(name):
            continue
        seen_missing.add(name)
        missing.append({"template": name, "referenced_at": f"{f}:{line}"})
    return {
        "status": "detected",
        "template_roots": templates["roots"],
        "template_files": sum(len(v) for v in files.values()),
        "referenced_from_python": len(roots),
        "unreferenced": unreferenced,
        # `captured_mentioned` false: the value the pattern's placeholder
        # stands for (`mail/{kind}/message.html` -> `welcome`) appears nowhere
        # as a string literal, so nothing visibly selects this template.
        "matched_only_dynamically": sorted(
            (
                {
                    "template": n,
                    "patterns_at": hits[:_LOCS_CAP],
                    "captured": captured(n),
                    "captured_mentioned": any(v in mentioned for v in captured(n)),
                }
                for n, hits in dynamic_only.items()
                # also extended/included by a reached template: not "only"
                if not (included_by.get(n, set()) & reached)
            ),
            key=lambda e: (e["captured_mentioned"], e["template"]),
        ),
        "missing": missing,
        "dynamic_template_edges": dynamic_edges[:50],
        "dynamic_python_refs": [
            {"text": d["text"], "at": f"{d['file']}:{d['line']}"} for d, _ in patterns
        ][:50],
    }


def _params(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[list[tuple[str, bool]], list[tuple[str, bool]], bool, bool]:
    args = node.args
    positional = args.posonlyargs + args.args
    n_defaults = len(args.defaults)
    pos = [(a.arg, i >= len(positional) - n_defaults) for i, a in enumerate(positional)]
    kwonly = [(a.arg, d is not None) for a, d in zip(args.kwonlyargs, args.kw_defaults)]
    return pos, kwonly, args.vararg is not None, args.kwarg is not None


def _bind(
    pos, kwonly, has_var, has_kw, n_pos: int, keywords: list[str]
) -> tuple[dict | None, str | None]:
    """Map call arguments onto parameters; (mapping, error)."""
    mapping: dict[str, object] = {}
    if n_pos > len(pos) and not has_var:
        return None, f"{n_pos} positional arguments for {len(pos)} parameters"
    for i in range(min(n_pos, len(pos))):
        mapping[pos[i][0]] = i
    names = {p for p, _ in pos} | {p for p, _ in kwonly}
    for key in keywords:
        if key in mapping:
            return None, f"multiple values for '{key}'"
        if key not in names and not has_kw:
            return None, f"unexpected keyword '{key}'"
        mapping[key] = key
    missing = [p for p, d in pos + kwonly if not d and p not in mapping]
    if missing:
        return None, f"missing {', '.join(repr(m) for m in missing)}"
    return mapping, None


def _param_uses(
    node: ast.AST, names: list[str], helpers: dict[str, ast.AST] | None = None
) -> dict[str, dict]:
    """How a function body uses each of *names*.

    With *helpers* (the module's own functions), a parameter passed straight
    to one of them is followed one level: the helper parameter's uses are
    listed too, because the lookup that matters is often there.
    """
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(node):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    uses: dict[str, list[dict]] = {n: [] for n in names}
    normalised: dict[str, set[str]] = {n: set() for n in names}
    followed: dict[str, list[dict]] = {n: [] for n in names}
    body = node.body  # type: ignore[attr-defined]
    for stmt in body:
        for sub in ast.walk(stmt):
            if not (
                isinstance(sub, ast.Name)
                and isinstance(sub.ctx, ast.Load)
                and sub.id in uses
            ):
                continue
            parent = parents.get(sub)
            kind = "other"
            context = parent
            if isinstance(parent, ast.Call) and sub is not parent.func:
                callee = _terminal(parent.func) or "call"
                kind = f"argument of {callee}"
                if _NORMALISERS.search(callee):
                    normalised[sub.id].add(callee)
                helper = (
                    (helpers or {}).get(callee)
                    if isinstance(parent.func, ast.Name)
                    else None
                )
                if helper is not None and sub in parent.args:
                    params = helper.args.posonlyargs + helper.args.args  # type: ignore[attr-defined]
                    index = parent.args.index(sub)
                    if index < len(params):
                        inner = _param_uses(helper, [params[index].arg])[
                            params[index].arg
                        ]
                        followed[sub.id].append(
                            {
                                "helper": callee,
                                "helper_param": params[index].arg,
                                "line": helper.lineno,  # type: ignore[attr-defined]
                                "uses": inner["uses"],
                                "normalised_by": inner["normalised_by"],
                            }
                        )
                        normalised[sub.id].update(inner["normalised_by"])
            elif isinstance(parent, ast.keyword):
                call = parents.get(parent)
                callee = _call_name(call) or "call"
                kind = f"keyword {parent.arg}= of {callee}"
                context = call or parent
                if _NORMALISERS.search(callee):
                    normalised[sub.id].add(callee)
            elif isinstance(parent, ast.Compare):
                outer = parents.get(parent)
                kind = "compared"
                if isinstance(outer, ast.Call):
                    kind = f"compared inside {_call_name(outer) or 'call'}"
            elif isinstance(parent, ast.Attribute):
                call = parents.get(parent)
                if isinstance(call, ast.Call) and call.func is parent:
                    kind = f"method .{parent.attr}()"
                    context = call
                    if _NORMALISERS.search(parent.attr):
                        normalised[sub.id].add(parent.attr)
                else:
                    kind = f"attribute .{parent.attr}"
            elif isinstance(parent, ast.Subscript):
                kind = "subscripted" if parent.value is sub else "used as a key"
            if context is None:
                context = sub
            uses[sub.id].append(
                {"line": sub.lineno, "use": kind, "code": _snippet(context)}
            )
    out: dict[str, dict] = {}
    for n in names:
        out[n] = {
            "uses": uses[n][:_USES_CAP],
            "use_count": len(uses[n]),
            "normalised_by": sorted(normalised[n]),
        }
        if followed[n]:
            out[n]["followed_into"] = followed[n][:_LOCS_CAP]
    return out


def _tasks_section(scans: list[_FileScan]) -> dict:
    tasks = [t for s in scans for t in s.tasks]
    if not tasks:
        return _not_detected(
            "no task declarations recognised (@app.task, @shared_task, @actor, @job, ...)"
        )
    helpers_by_file = {
        s.rel: {name: fn for name, fn in s.functions.items() if "." not in name}
        for s in scans
    }
    by_dotted = {t["dotted"]: t for t in tasks}
    by_simple: dict[str, list[dict]] = defaultdict(list)
    by_task_name: dict[str, dict] = {}
    for t in tasks:
        by_simple[t["qualname"].rsplit(".", 1)[-1]].append(t)
        if t["task_name"]:
            by_task_name[t["task_name"]] = t
        by_task_name.setdefault(t["dotted"], t)

    def find(record: dict) -> dict | None:
        if record["task_name"]:
            return by_task_name.get(record["task_name"])
        target = record["target"]
        if target in by_dotted:
            return by_dotted[target]
        simple = (record["target_local"] or "").rsplit(".", 1)[-1]
        candidates = by_simple.get(simple, [])
        if not candidates and target:
            candidates = by_simple.get(target.rsplit(".", 1)[-1], [])
        return candidates[0] if len(candidates) == 1 else None

    sites: dict[str, list[dict]] = defaultdict(list)
    for s in scans:
        for rec in s.enqueues:
            task = find(rec)
            if task is not None:
                sites[task["dotted"]].append(rec)
    scheduled_names: dict[str, list[str]] = defaultdict(list)
    for s in scans:
        for name, line in s.task_name_strings:
            if name in by_task_name:
                scheduled_names[by_task_name[name]["dotted"]].append(f"{s.rel}:{line}")

    declared, never_called, binding_errors, norm_differs = [], [], [], []
    for t in sorted(tasks, key=lambda t: (t["file"], t["line"])):
        node = t["node"]
        pos, kwonly, has_var, has_kw = _params(node)
        skip = 1 if t["bind"] else 0
        call_sites, injected_names = [], set()
        scheduled = list(scheduled_names.get(t["dotted"], []))
        site_args: dict[str, list[dict]] = defaultdict(list)
        for rec in sites.get(t["dotted"], []):
            loc = f"{rec['file']}:{rec['line']}"
            if rec["kind"] == "none":
                scheduled.append(loc)
                continue
            if rec["kind"] == "callable":
                call_sites.append(
                    {
                        "at": loc,
                        "via": rec["via"],
                        "bound": None,
                        "reason": "arguments supplied by the eventual caller",
                    }
                )
                continue
            entry: dict = {"at": loc, "via": rec["via"]}
            if rec["positional"] is None or rec["keywords"] is None or rec["splat"]:
                entry["bound"] = None
                entry["reason"] = (
                    "arguments are not literal (splat or computed args/kwargs)"
                )
                call_sites.append(entry)
                continue
            kw_names = list(rec["keywords"])
            offset = skip
            mapping, error = _bind(
                pos[offset:], kwonly, has_var, has_kw, len(rec["positional"]), kw_names
            )
            if error and len(pos) > offset and pos[offset][0] in _INJECTED_NAMES:
                retry, retry_error = _bind(
                    pos[offset + 1 :],
                    kwonly,
                    has_var,
                    has_kw,
                    len(rec["positional"]),
                    kw_names,
                )
                if retry_error is None:
                    mapping, error = retry, None
                    injected_names.add(pos[offset][0])
            if error:
                entry["binding_error"] = error
                binding_errors.append({"task": t["dotted"], "at": loc, "error": error})
            bound = {}
            for param, source in (mapping or {}).items():
                expr = (
                    rec["positional"][source]
                    if isinstance(source, int)
                    else rec["keywords"][source]
                )
                code = _snippet(expr)
                bound[param] = code
                site_args[param].append(
                    {
                        "at": loc,
                        "code": code,
                        "normalised": bool(_NORMALISERS.search(code)),
                    }
                )
            entry["bound"] = bound
            call_sites.append(entry)
        injected = [p for p, _ in pos[skip : skip + 1] if p in injected_names]
        params = [p for p, _ in pos[skip + len(injected) :]] + [p for p, _ in kwonly]
        uses = _param_uses(node, params, helpers_by_file.get(t["file"]))
        for param in params:
            body_norm = bool(uses[param]["normalised_by"])
            flags = {a["normalised"] for a in site_args.get(param, [])}
            # Callers disagree, or a caller normalises a value the body then
            # uses raw (a lookup by a different form). A body that normalises
            # what callers pass raw is the safe direction and is not listed.
            if len(flags) > 1 or (True in flags and not body_norm):
                norm_differs.append(
                    {
                        "task": t["dotted"],
                        "param": param,
                        "kind": "callers_disagree"
                        if len(flags) > 1
                        else "caller_normalises_body_does_not",
                        "normalised_in_body_by": uses[param]["normalised_by"],
                        "call_site_args": site_args[param][:_LOCS_CAP],
                    }
                )
        record = {
            "task": t["dotted"],
            "task_name": t["task_name"],
            "defined_at": f"{t['file']}:{t['line']}",
            "bind": t["bind"],
            "injected_params": injected,
            "params": params,
            "call_sites": call_sites,
            "scheduled_at": scheduled[:_LOCS_CAP],
            "param_uses": uses,
        }
        declared.append(record)
        if not call_sites and not scheduled:
            refs = _other_references(scans, t)
            never_called.append(
                {
                    "task": t["dotted"],
                    "defined_at": record["defined_at"],
                    "other_references": refs,
                }
            )
    return {
        "status": "detected",
        "declared": declared,
        "never_called": never_called,
        "binding_errors": binding_errors,
        "normalisation_differs": norm_differs,
    }


def _other_references(scans: list[_FileScan], task: dict) -> list[str]:
    simple = task["qualname"].rsplit(".", 1)[-1]
    out = []
    for s in scans:
        for name, line in s.name_loads:
            if name.rsplit(".", 1)[-1] != simple:
                continue
            if s.rel == task["file"] and line <= task["line"]:
                continue  # its own decorator line
            out.append(f"{s.rel}:{line}")
    return sorted(set(out))[:_LOCS_CAP]


def _class_attributes(cls: ast.ClassDef) -> set[str]:
    attrs: set[str] = set()
    for stmt in cls.body:
        attrs.update(_assigned_names(stmt))
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            attrs.add(stmt.name)
            for sub in ast.walk(stmt):
                if (
                    isinstance(sub, ast.Attribute)
                    and isinstance(sub.ctx, ast.Store)
                    and isinstance(sub.value, ast.Name)
                    and sub.value.id == "self"
                ):
                    attrs.add(sub.attr)
    return attrs


def _signals_section(scans: list[_FileScan]) -> dict:
    signals = {d["dotted"]: d for s in scans for d in s.signals}
    classes: dict[str, tuple[_FileScan, ast.ClassDef]] = {}
    for s in scans:
        for name, cls in s.classes.items():
            classes[f"{s.module}.{name}"] = (s, cls)
    sends = [x for s in scans for x in s.sends if x["target"] in signals]
    connects = [x for s in scans for x in s.connects]
    subscribers = [x for s in scans for x in s.subscribers]
    notifies = [x for s in scans for x in s.notifies]
    if not signals and not subscribers and not notifies:
        return _not_detected(
            "no project signals (Signal(), signal('name')) or event subscribers "
            "(@subscriber, add_subscriber, notify(Event(...))) recognised"
        )
    functions: dict[str, ast.AST] = {}
    for s in scans:
        for name, fn in s.functions.items():
            functions[f"{s.module}.{name}"] = fn

    entries, emitted_unhandled, handled_unemitted, unused, keys_not_sent = (
        [],
        [],
        [],
        [],
        [],
    )
    for dotted, sig in sorted(signals.items()):
        sig_sends = [x for x in sends if x["target"] == dotted]
        sig_handlers = [c for c in connects if c["signal"] == dotted]
        entry = {
            "signal": dotted,
            "kind": "signal",
            "declared_at": f"{sig['file']}:{sig['line']}",
            "emitted_at": [f"{x['file']}:{x['line']}" for x in sig_sends][:_LOCS_CAP],
            "handlers": [c["handler"] for c in sig_handlers][:_LOCS_CAP],
            "keys_sent": sorted({k for x in sig_sends for k in x["keys"]}),
        }
        entries.append(entry)
        _classify(
            entry, sig_sends, sig_handlers, emitted_unhandled, handled_unemitted, unused
        )
        if sig_sends:
            sent = set(entry["keys_sent"])
            for c in sig_handlers:
                fn = functions.get(c["handler"] or "")
                if fn is None:
                    continue
                read = _handler_keys(fn) - sent
                if read:
                    keys_not_sent.append(
                        {
                            "signal": dotted,
                            "handler": c["handler"],
                            "keys": sorted(read),
                        }
                    )

    events = {x["event"] for x in subscribers} | {x["event"] for x in notifies}
    attrs_not_set = []
    for event in sorted(e for e in events if e in classes):
        ev_subs = [x for x in subscribers if x["event"] == event]
        ev_notifies = [x for x in notifies if x["event"] == event]
        s, cls = classes[event]
        entry = {
            "signal": event,
            "kind": "event_class",
            "declared_at": f"{s.rel}:{cls.lineno}",
            "emitted_at": [f"{x['file']}:{x['line']}" for x in ev_notifies][:_LOCS_CAP],
            "handlers": [x["handler"] for x in ev_subs][:_LOCS_CAP],
        }
        entries.append(entry)
        _classify(
            entry, ev_notifies, ev_subs, emitted_unhandled, handled_unemitted, unused
        )
        known = _inherited_attributes(event, classes)
        if known is None:
            continue
        for sub in ev_subs:
            fn = functions.get(sub["handler"] or "")
            if fn is None:
                continue
            missing = _event_reads(fn) - known
            if missing:
                attrs_not_set.append(
                    {
                        "event": event,
                        "handler": sub["handler"],
                        "attributes": sorted(missing),
                    }
                )
    # Signals the project handles but does not declare (`post_save`): only
    # connections whose handler is a project function, so `sqlite3.connect(path)`
    # is not mistaken for one.
    framework_handled = sorted(
        {
            c["signal"]
            for c in connects
            if c["signal"] and c["signal"] not in signals and c["handler"] in functions
        }
    )
    return {
        "status": "detected",
        "declared": entries,
        "emitted_never_handled": emitted_unhandled,
        "handled_never_emitted": handled_unemitted,
        "declared_unused": unused,
        "keys_read_not_sent": keys_not_sent,
        "attributes_read_not_set": attrs_not_set,
        "framework_signals_handled": framework_handled[:50],
    }


def _classify(
    entry: dict,
    emits: list,
    handlers: list,
    unhandled: list,
    unemitted: list,
    unused: list,
) -> None:
    item = {"signal": entry["signal"], "declared_at": entry["declared_at"]}
    if emits and not handlers:
        unhandled.append(item)
    elif handlers and not emits:
        unemitted.append(item)
    elif not emits and not handlers:
        unused.append(item)


def _handler_keys(fn: ast.AST) -> set[str]:
    """Keywords a signal handler requires: named params after `sender`, and
    `kwargs["k"]` reads on its ** parameter."""
    args = fn.args  # type: ignore[attr-defined]
    params = args.posonlyargs + args.args
    if params and params[0].arg in ("self", "cls"):
        params = params[1:]
    n_defaults = len(args.defaults)
    required = {
        a.arg for i, a in enumerate(params[1:], start=1) if i < len(params) - n_defaults
    }
    required |= {a.arg for a, d in zip(args.kwonlyargs, args.kw_defaults) if d is None}
    if args.kwarg is not None:
        star = args.kwarg.arg
        for sub in ast.walk(fn):
            if (
                isinstance(sub, ast.Subscript)
                and _dotted(sub.value) == star
                and _str(sub.slice)
            ):
                required.add(_str(sub.slice))
    return required


def _event_reads(fn: ast.AST) -> set[str]:
    args = fn.args  # type: ignore[attr-defined]
    params = args.posonlyargs + args.args
    if params and params[0].arg in ("self", "cls"):
        params = params[1:]
    if not params:
        return set()
    event = params[0].arg
    return {
        sub.attr
        for sub in ast.walk(fn)
        if isinstance(sub, ast.Attribute)
        and isinstance(sub.value, ast.Name)
        and sub.value.id == event
        and isinstance(sub.ctx, ast.Load)
        and not sub.attr.startswith("__")
    }


def _inherited_attributes(dotted: str, classes: dict) -> set[str] | None:
    """Attributes an event class and its project bases set; None if a base is
    outside the project (its attributes are unknown)."""
    s, cls = classes[dotted]
    attrs = _class_attributes(cls)
    for base in cls.bases:
        resolved = s.resolve(base)
        if resolved in ("object", None) or _terminal(base) == "object":
            continue
        if resolved not in classes:
            return None
        inherited = _inherited_attributes(resolved, classes)
        if inherited is None:
            return None
        attrs |= inherited
    return attrs


def _request_methods_section(scans: list[_FileScan], templates: dict) -> dict:
    declared = [(n, s.rel, line) for s in scans for n, line in s.request_methods]
    if not declared:
        return _not_detected("no add_request_method() declarations")
    reads: dict[str, list[tuple]] = defaultdict(list)
    for s in scans:
        for name, line in s.request_attr_reads:
            reads[name].append((s.rel, line))
    for rel, text in templates["texts"].items():
        for m in _TEMPLATE_REQUEST_ATTR.finditer(text):
            reads[m.group(1)].append((rel, text.count("\n", 0, m.start()) + 1))
    return {
        "status": "detected",
        "declared": [
            {"name": n, "declared_at": f"{f}:{line}", "reads": len(reads.get(n, []))}
            for n, f, line in declared
        ],
        "declared_never_read": [
            {"name": n, "declared_at": f"{f}:{line}"}
            for n, f, line in declared
            if n not in reads
        ],
    }


def _permissions_section(scans: list[_FileScan], templates: dict) -> dict:
    required: dict[str, list[tuple]] = defaultdict(list)
    granted: dict[str, list[tuple]] = defaultdict(list)
    for s in scans:
        for key, f, line, via in s.perm_required:
            required[key].append((f, line, via))
        for key, f, line, via in s.perm_granted:
            granted[key].append((f, line, via))
    for rel, text in templates["texts"].items():
        for m in _TEMPLATE_PERMISSION_CHECK.finditer(text):
            key = m.group(1) or m.group(2)
            line = text.count("\n", 0, m.start()) + 1
            required[key].append((rel, line, "template"))
    if not required and not granted:
        return _not_detected(
            "no permissions recognised (permission=, permission_required, has_permission, "
            "(Allow, principal, perm) ACL entries, Meta.permissions)"
        )

    def short(key: str) -> str:
        return key.rsplit(".", 1)[-1] if "." in key and not key[0].isupper() else key

    granted_short = {short(k) for k in granted}
    required_short = {short(k) for k in required}
    never_granted = [
        {
            "permission": k,
            "required_at": [f"{f}:{line}" for f, line, _ in v[:_LOCS_CAP]],
            "framework_default": bool(_DJANGO_DEFAULT_PERM.match(k)),
        }
        for k, v in sorted(required.items())
        if k not in granted and short(k) not in granted_short
    ]
    never_required = [
        {"permission": k, "granted_at": [f"{f}:{line}" for f, line, _ in v[:_LOCS_CAP]]}
        for k, v in sorted(granted.items())
        if k not in required and short(k) not in required_short
    ]
    # Members of the classes permissions are named through (`Permissions.X`).
    owners = {
        k.rsplit(".", 1)[0]
        for k in list(required) + list(granted)
        if "." in k and k[0].isupper()
    }
    used = set(required) | set(granted)
    declared_unused = []
    for s in scans:
        for name, cls in s.classes.items():
            if name not in owners:
                continue
            for stmt in cls.body:
                for member in _assigned_names(stmt):
                    key = f"{name}.{member}"
                    if key not in used and not member.startswith("_"):
                        declared_unused.append(
                            {"permission": key, "declared_at": f"{s.rel}:{stmt.lineno}"}
                        )
    return {
        "status": "detected",
        "required": len(required),
        "granted": len(granted),
        "required_never_granted": never_granted,
        "granted_never_required": never_required,
        "declared_never_used": declared_unused,
    }


_SECTIONS = (
    "settings",
    "routes",
    "templates",
    "tasks",
    "signals",
    "request_methods",
    "permissions",
)


def analyze(target: str, *, max_files: int = 0) -> dict:
    """Cross-reference settings, routes, templates, tasks, signals and permissions."""
    resolved, project_root, scan_root = resolve_target(target)
    files, files_total = collect_python_files(resolved, max_files)
    scans: list[_FileScan] = []
    unparsed: list[str] = []
    for path in files:
        rel = relative_to_root(path, project_root)
        tree = parse_source(path)
        if tree is None:
            unparsed.append(rel)
            continue
        scans.append(_FileScan(rel, tree))
    frameworks: dict[str, int] = defaultdict(int)
    for s in scans:
        for fw in s.frameworks:
            frameworks[fw] += 1
    templates = _discover_templates(scan_root, project_root)
    result = build_envelope(project_root, scan_root, files_total, len(files))
    result["unparsed_files"] = unparsed
    result["frameworks"] = dict(sorted(frameworks.items()))
    if not frameworks:
        result["frameworks_note"] = (
            "no web framework or task-queue import seen; sections below use generic idioms only"
        )
    result["settings"] = _settings_section(
        scans, templates, project_root, set(frameworks)
    )
    result["routes"] = _routes_section(scans, templates)
    result["templates"] = _templates_section(scans, templates)
    result["tasks"] = _tasks_section(scans)
    result["signals"] = _signals_section(scans)
    result["request_methods"] = _request_methods_section(scans, templates)
    result["permissions"] = _permissions_section(scans, templates)
    summary: dict[str, object] = {}
    for section in _SECTIONS:
        data = result[section]
        if data["status"] != "detected":
            summary[section] = "not_detected"
            continue
        summary[section] = {
            k: len(v)
            for k, v in data.items()
            if isinstance(v, list)
            and k
            not in (
                "declared",
                "dynamic_reads",
                "template_roots",
                "dynamic_template_edges",
                "dynamic_python_refs",
            )
        }
    result["summary"] = summary
    return result


def main() -> None:
    target, max_files = parse_common_args(sys.argv[1:])
    emit(analyze(target, max_files=max_files))


if __name__ == "__main__":
    main()
