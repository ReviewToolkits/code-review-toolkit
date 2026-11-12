# Boot the app and diff its live registries against the source

On web applications most real dead-code findings are wiring gaps: a route whose view was deleted, a
setting nothing reads, a template nothing renders, a task nobody enqueues, a permission granted and
never checked. `map_wiring.py` finds them statically. This recipe confirms them against what the
framework actually registered: boot the app with the project's **own** test fixture, dump the live
registries to JSON, and diff that against the static output.

Use it when `map_wiring.py` reports a gap you cannot settle by reading, or when the app registers
things dynamically (a `config.scan()`, a plugin loop, an `include()` with a computed name) that a
static scan cannot follow.

---

## 1. Run the static pass

```bash
d=$(mktemp -d "${TMPDIR:-/tmp}/crt-wiring.XXXXXX") && echo "$d" &&
python3 <plugin_root>/scripts/map_wiring.py <package> > "$d/static.json" 2> "$d/static.err"; echo "exit=$?"
```

Read `frameworks` first, then each section's `status`. A section that says `not_detected` means the
script found none of that framework's idioms: say so in the report, never "no gaps".

## 2. Make an export and find the project's fixture

Work in an exported copy, never the checkout (see `<plugin_root>/docs/reproduction-convention.md`):

```bash
git -C <project> archive HEAD | tar -x -C "$d/copy"
```

Then find the fixture the project's own tests use to build a configured app, in its `conftest.py`.
It is usually named something like `app`, `application`, `client`, `test_app` or `config`, and
it already knows how to reach the test database and stub external services. Reuse it: an app
booted by hand misses settings that only the fixture supplies, and the diff then reports gaps that
do not exist.

## 3. Dump the registries

Copy a test like the one below into the export's test directory and run only it, with
`TMPDIR="$d"`. Keep the parts for the framework you found and delete the rest. Each block writes
plain names, so the JSON can be diffed without importing the app again.

```python
# test_dump_registry.py: copy into <export>/tests/, run with
#   DUMP_OUT="$d/live.json" python -m pytest <export>/tests/test_dump_registry.py -p no:randomly -q
import json
import os


def _name(obj):
    if obj is None or isinstance(obj, str):
        return obj
    return f"{getattr(obj, '__module__', '?')}:{getattr(obj, '__qualname__', repr(obj))}"


def test_dump_registry(configured_app):  # FILL IN: the project's own fixture
    out = {}

    # --- Pyramid-style: the introspector holds routes, views, permissions, templates
    config = configured_app
    intr = config.registry.introspector
    out["settings"] = sorted(map(str, config.registry.settings))
    out["routes"] = [
        {"name": i["introspectable"]["name"], "pattern": i["introspectable"]["pattern"]}
        for i in intr.get_category("routes") or []
    ]
    out["views"] = [
        {
            "route_name": i["introspectable"].get("route_name"),
            "callable": _name(i["introspectable"].get("callable")),
            "permission": next(
                (str(r["value"]) for r in i["related"] if r.category_name == "permissions"),
                None,
            ),
            "templates": [r["name"] for r in i["related"] if r.category_name == "templates"],
        }
        for i in intr.get_category("views") or []
    ]
    out["subscribers"] = [
        _name(i["introspectable"].get("subscriber")) for i in intr.get_category("subscribers") or []
    ]

    # --- Flask-style
    # app = configured_app
    # out["routes"] = [{"name": r.endpoint, "pattern": r.rule, "methods": sorted(r.methods)}
    #                  for r in app.url_map.iter_rules()]
    # out["settings"] = sorted(app.config)
    # out["templates"] = app.jinja_env.list_templates()

    # --- Django-style (run under the project's settings; pytest-django's `settings` fixture)
    # from django.conf import settings as dj
    # from django.urls import get_resolver
    # out["settings"] = sorted(k for k in dir(dj) if k.isupper())
    # out["routes"] = sorted(str(k) for k in get_resolver().reverse_dict if isinstance(k, str))

    # --- FastAPI / Starlette-style
    # out["routes"] = [{"name": r.name, "pattern": r.path, "methods": sorted(getattr(r, "methods", []) or [])}
    #                  for r in app.routes]

    # --- Celery-style task registry (project tasks only)
    # celery_app = ...  # FILL IN: the project's Celery app
    # out["tasks"] = sorted(n for n in celery_app.tasks if not n.startswith("celery."))
    # out["beat"] = {k: v["task"] for k, v in (celery_app.conf.beat_schedule or {}).items()}

    with open(os.environ["DUMP_OUT"], "w") as fh:
        json.dump(out, fh, indent=1, default=str)
```

A failing fixture is not a finding. If the app does not boot in the export (a missing service,
a secret), stub what the project's own test settings stub, or fall back to the static pass and say
the live diff was not run.

## 4. Diff

```bash
python3 - "$d/static.json" "$d/live.json" <<'EOF'
import json, sys
static, live = (json.load(open(p)) for p in sys.argv[1:])
declared = {r["name"] for r in static["routes"].get("declared", [])}
registered = {r["name"] for r in live.get("routes", [])}
print("registered but not seen statically:", sorted(registered - declared))
print("declared statically but not registered:", sorted(declared - registered))
views = {v["route_name"] for v in live.get("views", []) if v.get("route_name")}
print("live routes with no view:", sorted(registered - views))
written = {e["key"] for e in static["settings"].get("written_never_read", [])}
print("never-read settings present at runtime:", sorted(written & set(live.get("settings", []))))
EOF
```

Read the two directions differently:

- **Registered but not seen statically**: the static scan missed a registration idiom (a custom
  directive, a computed name). Not a finding; note it so the static leads in that area are read
  with care.
- **Declared statically but not registered**: a route or view module that is never included, a
  `config.scan()` that skips a package, a condition that is always false at boot. A strong
  dead-code lead.
- **A live route with no view**, a permission no live view requires, a task in the registry with no
  call site in `map_wiring.py`'s `tasks.declared[].call_sites`: dead wiring, confirmed.

Report each gap with both sides: the static `file:line` and the live registry entry. One boot is
one configuration: a route registered only in production settings looks dead under the test
fixture, so check the settings that gate it before calling it dead.
