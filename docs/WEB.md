# The pages on FastAPI: how a view module is written, and how an old one is converted

The platform's pages are moving off the home-made Flask imitation (`app/webkit`) onto FastAPI and Starlette with
nothing in between. **Phase 1 (done)** built the native foundation, `app/web/http.py`, and converted `auth`, `account`
and `workspaces`. **Phase 2** converts the other view modules, one module at a time, in parallel, by following this
page literally. **Phase 3** deletes `app/webkit` and the few lines marked "transition".

During the transition both layers serve pages from one ASGI app (`app/api/__init__.py`, `front()`):

```
FastAPI front ── /api/v2/...            the REST v2 routes
             ├── native page routers    app/web/auth.py, account.py, workspaces.py, ... (PageRoute)
             └── OldPages mount         everything else: app/webkit's blueprints, the v1 API, MCP, /static
```

They share everything a person notices: **one session cookie** (same signer, salt, JSON tagging, name, flags, lifetime),
**one sign-in** (`_user_id`, `_fresh`, `_id` session keys and the `remember_token` cookie), **one CSRF token**, **one
queue of flashed messages**, and **addresses** (`url_for` on either side finds pages on both). Nobody is signed out
when a module moves, and a page converted today links to one converted next week without a change.

## A converted route, before and after

Before (`app/webkit`, Flask-shaped):

```python
from ..webkit import Blueprint, current_user, flash, login_required, redirect, render_template, request, session, url_for

bp = Blueprint("account", __name__, url_prefix="/account")

@bp.route("/", methods=["GET", "POST"])
@login_required
def page():
    if request.method == "POST":
        if request.form.get("action") == "2fa_start":
            session["totp_setup"] = totp.new_secret()
        flash("Saved.", "success")
        return redirect(url_for("account.page"))
    return render_template("account.html", setup=session.get("totp_setup"))
```

After (native):

```python
from fastapi import APIRouter, Depends, Request

from .http import Form, PageRoute, check_csrf, flash, form_of, redirect, render, signed_in, url_for

router = APIRouter(route_class=PageRoute, dependencies=[Depends(check_csrf)])

@router.api_route("/account/", methods=["GET", "POST"], name="account.page")
def page(request: Request, user=Depends(signed_in), form: Form = Depends(form_of)):
    session = request.state.session
    if request.method == "POST":
        if form.get("action") == "2fa_start":
            session["totp_setup"] = totp.new_secret()
        flash(request, "Saved.", "success")
        return redirect(url_for("account.page"))
    return render(request, "account.html", setup=session.get("totp_setup"))
```

A page inside a workspace (phase 2's usual case), before and after:

```python
@bp.route("/security", methods=["GET", "POST"])     # bp = Blueprint("settings", ..., url_prefix="/w/<int:wid>/settings")
@login_required
def security(wid):
    ws = workspace(wid, "admin")
    if request.method == "POST":
        ws.settings = {**ws.settings, "idle_minutes": request.form.get("idle_minutes", type=int) or 0}
        db.session.commit()
        return redirect(url_for("settings.security", wid=wid))
    return render_template("settings/security.html", ws=ws, member=g.member)
```

```python
router = APIRouter(prefix="/w/{wid:int}/settings", route_class=PageRoute, dependencies=[Depends(check_csrf)])

@router.api_route("/security", methods=["GET", "POST"], name="settings.security")
def security(request: Request, wid: int, ws=Depends(member_of("admin")), form: Form = Depends(form_of)):
    if request.method == "POST":
        ws.settings = {**ws.settings, "idle_minutes": form.get("idle_minutes", type=int) or 0}
        db.session.commit()
        return redirect(url_for("settings.security", wid=wid))
    return render(request, "settings/security.html", ws=ws, member=request.state.member)
```

`app/web/auth.py`, `account.py` and `workspaces.py` are the worked examples: read each beside its old version
(`git show 66559f3:app/web/auth.py`).

## What `app/web/http.py` gives a view

FastAPI gives the routing (`APIRouter`, `Depends`, typed path parameters) and Starlette the request and responses.
`http.py` adds only what the platform's pages need beyond them.

| Need | Native | Notes |
|---|---|---|
| A page router | `APIRouter(route_class=PageRoute, dependencies=[Depends(check_csrf)])` | every page router uses `PageRoute`; `prefix=` for a shared address start |
| A route | `@router.api_route(path, methods=[...], name="blueprint.function")` | the name is the old endpoint name the templates use |
| The request | `request: Request` (first parameter) | `from fastapi import Request` |
| What the page looked up (`g`) | `request.state.ws`, `.member`, `.engine`, ... | per request |
| The session | `request.state.session` | a dict; the same cookie as before |
| Who is signed in | `current_user(request)` | an `AnonymousUser` (`is_authenticated` False) when nobody is |
| Only for someone signed in | `user=Depends(signed_in)`, or `dependencies=[Depends(signed_in)]` | anyone else is sent to `/login?next=...` |
| Signing in and out | `login_user(request, user, remember=True)`, `logout_user(request)` | |
| A workspace page's access check | `ws=Depends(member_of("editor"))` (app/security.py), or `workspace(wid, "editor", request)` | sets `request.state.ws` and `.member`; 404 or 403 as before; signs in first |
| The ERP's engine | `ws, engine = ctx(wid, "editor", request)` (app/web/erp.py) | sets `request.state.engine` too |
| The form | `form: Form = Depends(form_of)` | `form.get(k, default, type=int)`, `form.getlist(k)`, `form[k]` (a 400 if missing), `form.to_dict()` |
| Uploaded files | `form.files.getlist("files")`, `form.files.get("file")` | each an `Upload`: `.filename`, `.read()`, `.save(path)`, `.mimetype`, `.content_type`, `.stream` |
| JSON posted | `data=Depends(json_of)` | the body as JSON, or None (as `get_json(silent=True)` was) |
| The raw body | `body: bytes = Depends(body_of)` | a 413 over MAX_CONTENT_LENGTH |
| CSRF | the router's `Depends(check_csrf)`; `csrf_token(request)` | templates call `csrf_token()` as before |
| Messages | `flash(request, message, category)`; `flashed(request, with_categories=True)` | templates call `get_flashed_messages()` as before |
| Addresses | `url_for("blueprint.function", **values)` | path values fill the path, the rest the query; `_external=True` (BASE_URL), `_anchor=` |
| A page | `render(request, "x.html", status_code=200, **context)` | the same Jinja environment, filters, globals and context processors |
| A redirect | `redirect(url, code=302)` | |
| JSON | `json_response(data, status_code=200, headers=None)` | the old `jsonify` encoding (sorted keys, dates, decimals) |
| A file | `send_file(request, path_or_bytesio, mimetype=None, as_attachment=False, download_name=None, ...)` | the same disposition, ETag, 304 and byte-range rules |
| Any other response | `from starlette.responses import Response`: `Response(body, media_type="text/csv", headers={...})` | `text/*` gets `charset=utf-8` |
| Stop with an error page | `raise HTTPException(404)`, `raise HTTPException(403, "why")` | `from fastapi import HTTPException`; the platform's error page; JSON under /api/ |
| Stop with a redirect | `raise Redirect(url)` | from a helper or a dependency |
| Stop with a response | `raise Answer(response)` | the old `abort(response)` |
| A template global | `@template_global` on the function | was `@bp.app_template_global()` |
| A context processor | `@context_processor` on the function | was `@bp.app_context_processor` |
| The request, far from any view | `current_request()` | only where passing `request` would ripple far |
| The app | `from ..core.context import current_app` | unchanged |

What a `PageRoute` does for every request, so a view never does it: it enters the platform's context exactly as
`AppContext.push` did (`core.context.entered(app)`, `core.settings.using(app.config)`, `db.using(app.engine)`, and
`actor.reading(...)`, so the audit log and row-level security know the person and the workspace), sets
`current_request()`, reads the session cookie, runs the view, turns any error into its page, then writes the
"keep me signed in" and session cookies, adds the security headers (`security.page_headers`), records the time for
Settings › Health, and gives the database session back. Sync views (`def`) run in a worker thread, as before.

Tests are unchanged: `app.test_client()` goes through the FastAPI front, and a request made while the test holds the
app's context shares the test's database session, as it always did.

A helper both layers call (app/security.py, app/web/erp.py, app/web/filters.py, the template globals) is written once,
natively. On an old page `current_request()` is that page's request seen natively: its `request.state` is the old
`g` and its `request.state.session` the old `session`. An `HTTPException`, `Redirect` or `Answer` a native helper raises
on an old page becomes the old layer's own error page or redirect, and a native response it returns (`render`,
`redirect`, `json_response`) is answered as is. So a helper converted with its module keeps working for the modules
that call it and aren't converted yet.

## The conversion checklist

Do these in order for one module. Each item says what to look for in the module and what to write instead.

1. **The router.** Replace `bp = Blueprint("name", __name__, url_prefix="/w/<int:wid>/x")` with
   `router = APIRouter(prefix="/w/{wid:int}/x", route_class=PageRoute, dependencies=[Depends(check_csrf)])`
   (no `prefix=` when the blueprint had none). Leave the `check_csrf` dependency out only for the exempt routers
   (item 9). A module with two blueprints (portal.py's `bp` and `join`) gets two routers.
2. **Each route.** `@bp.route("/t/<table>/<int:rid>", methods=["GET", "POST"])` becomes
   `@router.api_route("/t/{table}/{rid:int}", methods=["GET", "POST"], name="<blueprint name>.<function>")`.
   - `<int:x>` → `{x:int}` (so `/t/orders/new` never reaches a view expecting a number); `<x>` → `{x}`;
     `<path:x>` → `{x:path}`.
   - The name is exactly the old endpoint: the blueprint's name (not the module's) and the function's, such as
     `app.record`, `settings.members`, `join.join`. Templates and other modules keep calling `url_for` with it.
   - No `methods=` meant GET: write `methods=["GET"]`. A GET route answers HEAD too.
   - Several `@bp.route` lines on one function: one `@router.api_route` line each, all with the same `name`
     (url_for picks the one whose path values it has, as before).
   - **Order matters.** Starlette tries routes in the order they're added; the old layer tried the most specific
     first. Put the more particular address first: `/p/{key}.pdf` before `/p/{key}` (which would also take `x.pdf`),
     fixed text before a parameter in the same place (`/approvals/away` before `/approvals/{aid}`; with `{aid:int}`
     they can't clash at all). `tests/test_http.py` checks that no native page is hidden behind another, and that none
     takes an address the old layer still serves.
3. **The parameters.** The view takes `request: Request` first, then its path values typed (`wid: int, table: str,
   rid: int`), then its dependencies. Remove `@login_required`; add `user=Depends(signed_in)` (or, when the view
   doesn't use the person, `dependencies=[Depends(signed_in)]` on the route).
4. **The request.**
   - `request.form` → `form` (`form: Form = Depends(form_of)`); `.get`, `.getlist`, `type=`, `[...]`, `.to_dict()` and
     `.items(multi=True)` work as before.
   - `request.files` → `form.files` (`Upload` objects, with the old `FileStorage` methods).
   - `request.args` → `request.query_params`: `.get(k)`, `.get(k, default)`, `.getlist(k)`, `in`. It has no `type=`:
     convert with a small helper (`_int(request.query_params.get("page"))`). With a key sent twice `.get` gives the
     last value (the old one gave the first); use `.getlist` wherever a key can repeat.
   - `request.values.get(k)` → `request.query_params[k] if k in request.query_params else form.get(k)`.
   - `request.get_json(silent=True)` → `data=Depends(json_of)`. `request.get_data()` → `body: bytes = Depends(body_of)`
     (`as_text=True` → `body.decode("utf-8", "replace")`).
   - `request.method`, `request.headers.get(...)` and `request.cookies` are the same.
   - `request.path` → `request.url.path`; `request.full_path` → `full_path(request)`; `request.query_string.decode()`
     → `request.url.query`; `request.url` → `str(request.url)`; `request.referrer` → `request.headers.get("referer")`;
     `request.host` → `request.headers.get("host")`; `request.is_secure` → `request.url.scheme == "https"`;
     `request.remote_addr` → `security.client_ip(request)` (or `request.client.host`); `request.endpoint` →
     `request.scope["route"].name`.
5. **`g`, the session and the person.** `g.x` → `request.state.x` (`getattr(request.state, "x", None)` for
   `g.get("x")`); `session` → `request.state.session`; `current_user` → `current_user(request)` (or the `user` from
   `signed_in`); `login_user(user, ...)` → `login_user(request, user, ...)`; `logout_user()` → `logout_user(request)`.
6. **The shared helpers** are already native; pass the request:
   `workspace(wid, role)` → `workspace(wid, role, request)` or `ws=Depends(member_of(role))`;
   `ctx(wid, role)` → `ctx(wid, role, request)`; `stepped_up()` → `stepped_up(request)`;
   `step_up(user, code)` → `step_up(user, code, request)`; `confirm_step_up(code)` → `confirm_step_up(code, request)`;
   `client_ip()` → `client_ip(request)`. The module's own helpers that read the request implicitly take `request` as
   their first parameter; use `current_request()` only where passing it would ripple through many modules.
7. **The answers.**
   - `render_template(name, **ctx)` → `render(request, name, **ctx)`; `return render_template(...), 400` →
     `return render(request, ..., status_code=400)`.
   - `jsonify(x)` → `json_response(x)`; `return jsonify(x), 400` → `return json_response(x, 400)`.
   - `redirect(url)` is the same (from `.http`). `flash(msg, cat)` → `flash(request, msg, cat)`.
   - `send_file(path, ...)` → `send_file(request, path, ...)`. `Response(body, mimetype=m, headers=h)` →
     `Response(body, media_type=m, headers=h)` from `starlette.responses`. A bare `return "", 204` →
     `return Response(status_code=204)`.
   - `abort(404)` → `raise HTTPException(404)`; `abort(403, "why")` → `raise HTTPException(403, "why")`;
     `abort(redirect(url))` → `raise Redirect(url)`; `abort(response)` → `raise Answer(response)` (the v1 API's
     `abort(_error(404, ...))` → `raise Answer(_error(404, ...))`, with `_error` returning a `json_response`).
   - `url_for` keeps its name (from `.http`). There's no `".function"` shorthand: write the full name.
     `_external=True` gives `BASE_URL` and the path.
8. **Hooks.** A blueprint's `@bp.before_request` becomes a dependency on its router
   (`APIRouter(..., dependencies=[Depends(check_csrf), Depends(the_hook)])`), which raises `HTTPException` or
   `Redirect` to stop the request instead of returning a response. The app-wide hooks (timing, security headers, CSRF,
   the sign-in cookie) are done by `PageRoute`: nothing to write. No remaining module has a blueprint hook today.
   `@bp.app_template_global()` → `@template_global`; `@bp.app_context_processor` → `@context_processor`.
9. **CSRF exemptions.** These are exempt today, so their routers have no `check_csrf`: `api.bp` (the v1 API, with the
   MCP routes mcp.py adds to it), `api.hooks` (webhooks), and `connect_apps.stripe_events` (put that one route on a
   second router in connect_apps.py, without `check_csrf`). Delete their `csrf.exempt(...)` lines. Every other router
   has `dependencies=[Depends(check_csrf)]`, public forms included (they post the token their page gave).
10. **Templates.** In the templates the module renders: `request.args` → `request.query_params`, `request.path` →
    `request.url.path`, `request.full_path` → `full_path(request)`. These work whichever layer renders the template, so
    a template shared with a module not converted yet can be changed now. Today's uses: app/_studio_ask.html,
    app/_views.html, app/ai.html, app/layout.html, app/page.html, app/record.html, app/table.html,
    builder/discover.html and portal/record.html. Nothing else in a template changes.
11. **Wire it in.** In `app/api/__init__.py` (`front()`), import the module on the import line and add `module.router`
    (one a line) to the list given to `http.install`. In `app/__init__.py` (`create_app`), remove its blueprint lines
    from the old layer's list. Other modules that import a helper from it keep working; if the helper now takes
    `request`, update its callers (on the old layer's pages, pass `current_request()`).
12. **Run the tests**: the module's own, `tests/test_http.py`, then the whole suite
    (`PYTHONPATH=. .venv/bin/python -m pytest tests -q -p no:cacheprovider`). A conversion changes no test.

Small differences from the old layer, all accepted: an OPTIONS request to a native page is a 405 (it was an empty
200); a 405's `Allow` header isn't sorted; deleting the remember-me cookie writes `remember_token=""`.

## What's left to convert

151 routes in 15 modules (6,115 lines). `erp.py` and the six `erp_*` modules share one blueprint, `app`
(`/w/<int:wid>/app`): each `erp_*` module gets its own router with `prefix="/w/{wid:int}/app"` and names
`app.<function>`, and `erp.bp` (and its line in `create_app`) goes when the last of them is converted. erp.py's helpers
(`ctx`, `confirm_step_up`, the menu, the counts), its template globals and its context processor are already native.

| Module | Lines | Routes | What to watch |
|---|---|---|---|
| erp_records.py | 1,616 | 33 | `send_file` and `Response` (CSV, ICS, PDF, ZIP, files), uploads, `request.referrer`, `request.query_string`, `@bp.app_template_global` (field_visible), `confirm_step_up` |
| erp_pages.py | 1,289 | 27 | `Response` (PDF), `confirm_step_up`, `request.referrer`, the approvals queue, route order (`/p/{key}.pdf` before `/p/{key}`) |
| erp_studio.py | 480 | 14 | JSON in and out (`json_of`, `json_response`) |
| portal.py | 504 | 14 | two blueprints (`portal` under `/portal/{wid:int}`, and `join`), `login_user`, `send_file` |
| settings.py | 455 | 16 | `send_file` (backups), the logo upload, `current_app` |
| api.py | 258 | 7 | the v1 API: CSRF-exempt, Bearer keys, `abort(_error(...))` → `raise Answer(...)`; its `hooks` blueprint (webhooks, exempt) |
| erp_flows.py | 262 | 6 | photo uploads, `request.values`, `request.referrer` |
| erp_ai.py | 247 | 11 | uploads, `json_of`, `request.values` |
| mcp.py | 246 | 2 | adds `/api/v1/mcp` and `/api/v1/openapi.json` to `api.bp`: convert with api.py, on its router |
| erp.py | 203 | 0 | holds `bp` for the `erp_*` modules until the last is converted |
| builder.py | 174 | 9 | uploads, `json_of`, `@bp.app_template_global` (build_minutes) |
| erp_learn.py | 164 | 5 | |
| connect_apps.py | 145 | 4 | `stripe_events` exempt, its raw body (`body_of`) for the signature |
| forms.py | 55 | 1 | the public form (keeps CSRF), uploads, `client_ip(request)` |
| connect.py | 38 | 2 | the callback hands Microsoft sign-ins on: `auth.finish_microsoft_login(request, request.query_params)` |

## Phase 3: deleting the old layer

When `create_app` registers no blueprint:
- Delete `app/webkit/` and `tests/test_webkit.py`. In `app/web/http.py` delete what's marked "transition": `OldPages`,
  `seen_natively`, `path_for`'s fallback to the old `url_map`, and the closing-slash redirect (Starlette's
  `redirect_slashes` then sends `/account` on to `/account/`, with a 307). In `app/__init__.py` delete
  `_old_layer_hooks`; in `app/extensions.py`, `LoginManager` and `CSRFProtect`; in the Jinja environment, the
  `request`, `session` and `g` globals.
- Keep what isn't web. `webkit.App` also gives workers and scripts `app.app_context()`, `app.config`,
  `app.extensions`, `app.jinja_env` and `app.logger`, and the tests `app.test_client()` (webkit/testing.py, which
  hands the test's context to the request in `scope["keel.context"]`, which `http.serving` honours). Move those into a
  small platform class (and `app/web/testing.py`) before deleting.
- Rewrite what uses the old layer directly: `tests/conftest.py` (its `before_request` and `teardown_request` hooks
  aren't needed natively: `request.state` is per request, and `serving` forgets the workspace), `test_queues.py`,
  `test_procurement.py` and `test_ai_gateway.py` (`app.test_request_context()`, `webkit.g`, `webkit.login_user`),
  `app/tools.py` (`test_request_context`) and `evals/mapux.py` (`webkit.serving`).
