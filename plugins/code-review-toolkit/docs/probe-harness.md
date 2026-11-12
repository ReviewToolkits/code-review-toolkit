# Probe a web server's wire surface: endpoint × case matrix

For a library, the API is its Python names. For a server, the API is what goes over the wire: every
route, every method, every parameter, every status code and header. Its defects show up as
**inconsistency across endpoints**: one endpoint answers a malformed parameter with a 400 and its
sibling with a 500; an archived object is hidden from the page but served by the JSON API; one
route accepts a duplicated query key and silently takes the last value. Reading the code one view
at a time does not find these. Sending the same case to every endpoint and lining up the outcomes
does.

---

## 1. Enumerate the routes

```bash
d=$(mktemp -d "${TMPDIR:-/tmp}/crt-probe.XXXXXX") && echo "$d" &&
python3 <plugin_root>/scripts/map_wiring.py <package> > "$d/wiring.json" 2> "$d/wiring.err"; echo "exit=$?"
```

`routes.declared` lists each route with its `pattern`, `methods`, `permissions` and `views`. If its
`status` is `not_detected`, take the route table from the live app instead (Flask
`app.url_map.iter_rules()`, Starlette/FastAPI `app.routes`, Django `get_resolver()`, Pyramid's
introspector; see `<plugin_root>/docs/registry-diff.md`) or from the OpenAPI document. Group the
routes by **resource** (everything that takes a `{project}` or `{user_id}`), because the cases
below are compared within a group.

## 2. Choose the cases

Run each case against every route in a group, through the project's own in-process test client:

| Case | What to send | Expect |
|---|---|---|
| baseline | a valid request for an existing, visible object | 2xx (or the documented redirect) |
| missing param | drop each required query/form/body field in turn | 4xx with the project's error shape |
| duplicate param | `?a=1&a=2`, a JSON body with a repeated key | rejected, or a documented rule, the same everywhere |
| malformed param | wrong type, empty, overlong, non-UTF-8, `NaN`, negative page | 4xx, never 5xx |
| wrong method | each method the route does not declare, plus `HEAD` and `OPTIONS` | 405 with an `Allow` header |
| unauthenticated | no credentials; then a valid user without the permission | 401/403 (or 404 to hide existence), the same choice everywhere |
| content type | form body to a JSON endpoint and back; no `Content-Type`; a charset | 415/400, never a silent empty parse |
| CORS | a preflight `OPTIONS` with `Origin` and `Access-Control-Request-Method` | the documented policy, the same on sibling routes |
| normalisation | trailing slash, case variants, Unicode-equivalent names, `%2F` | one canonical answer or a redirect to it |
| hidden objects | an object that is deleted, archived, private, or another tenant's | invisible on **every** route that can reach it |

Hidden objects are the highest-yield row: list every state that is meant to hide an object, then
check that each route which can name the object honours it (HTML page, JSON API, feed, file
download, search, admin export).

## 3. The harness

Fill in the marked parts, save it in your private directory and run it against an export (never
the checkout; see `<plugin_root>/docs/reproduction-convention.md`). It writes one JSON line per
request and prints the rows where routes in one group disagree.

```python
"""Send each case to each route of a group and print where the outcomes diverge."""
import json
import sys

OUT = sys.argv[1]  # e.g. "$d/matrix.jsonl"


def make_client():
    """FILL IN: the project's own in-process test client, built the way its tests build it.

    Flask: app.test_client(); Django: django.test.Client(); FastAPI/Starlette:
    TestClient(app); WSGI apps: webtest.TestApp(app). Return an object with a request method.
    """
    raise NotImplementedError


# FILL IN: one group of routes that take the same resource, as concrete URL templates.
GROUP = {
    "html": "/items/{name}/",
    "json": "/api/items/{name}",
    "feed": "/items/{name}/feed.xml",
}
OBJECTS = {"visible": "widget", "archived": "old-widget", "missing": "no-such-item"}

# FILL IN / extend: (label, method, url-object key, request kwargs)
CASES = [
    ("baseline", "GET", "visible", {}),
    ("archived", "GET", "archived", {}),
    ("missing", "GET", "missing", {}),
    ("wrong method", "DELETE", "visible", {}),
    ("dup query key", "GET", "visible", {"query_string": "page=1&page=2"}),
    ("bad page", "GET", "visible", {"query_string": "page=-1"}),
    ("trailing slash", "GET", "visible", {"_strip_slash": True}),
    ("preflight", "OPTIONS", "visible", {"headers": {
        "Origin": "https://example.invalid", "Access-Control-Request-Method": "GET"}}),
]

HEADERS = ("content-type", "allow", "access-control-allow-origin", "cache-control",
           "surrogate-key", "surrogate-control", "etag", "vary", "location")


def send(client, method, url, kwargs):
    """FILL IN if your client's signature differs (WebTest: expect_errors=True)."""
    kwargs = dict(kwargs)
    if kwargs.pop("_strip_slash", False):
        url = url.rstrip("/") if url.endswith("/") else url + "/"
    try:
        resp = client.open(url, method=method, **kwargs)  # Flask-style; adapt
    except Exception as exc:  # an exception escaping the app is a 5xx-grade outcome
        return {"status": "exception", "error": type(exc).__name__}
    headers = {k.lower(): v for k, v in resp.headers.items()}
    return {"status": resp.status_code, "headers": {h: headers.get(h) for h in HEADERS}}


def main():
    client = make_client()
    rows = []
    with open(OUT, "w") as fh:
        for label, method, key, kwargs in CASES:
            got = {}
            for route, template in GROUP.items():
                result = send(client, method, template.format(name=OBJECTS[key]), kwargs)
                got[route] = result
                fh.write(json.dumps({"case": label, "route": route, **result}) + "\n")
            statuses = {r: g["status"] for r, g in got.items()}
            server_error = any(s == "exception" or (isinstance(s, int) and s >= 500)
                               for s in statuses.values())
            classes = {s if not isinstance(s, int) else s // 100 for s in statuses.values()}
            flag = "!!" if server_error or len(classes) > 1 else "  "
            rows.append(f"{flag} {label:16} {statuses}")
    print("\n".join(rows))


if __name__ == "__main__":
    main()
```

## 4. Read the matrix

- **Any 5xx or escaped exception** for a client-side mistake is a finding on its own (an
  unhandled validation path).
- **A row where routes in one group disagree** (`!!`) is a lead: one route rejects what its sibling
  accepts, or shows what its sibling hides. Decide which one is right from the documented contract,
  not from the majority.
- **Hidden objects visible on any route** are usually the most serious rows: check what the
  response contains, not only its status.
- **Headers**: compare `Content-Type`, `Allow`, the CORS headers and the caching headers across the
  group. A sibling missing `Vary: Accept` or a `Cache-Control: private` is a lead.

**Cache and surrogate headers need a cache backend.** Headers such as `Cache-Control`,
`Surrogate-Key`, `Surrogate-Control` and `ETag` are often added by a cache or CDN integration that
the test settings disable or stub. Under the default test fixture they may be missing everywhere,
which looks like "no caching problems" and is not evidence of anything. Before asserting on them,
configure the cache backend the project uses (or the stub its own cache tests use) in the test app,
and check that the baseline row actually carries the headers; if it does not, say the caching
headers were not observable and leave them out of the findings.

Report each finding with the case, the routes compared, both outcomes and the command that
produced the matrix; keep `matrix.jsonl` as the preserved harness output.
