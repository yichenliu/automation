# Writing the platform's pages

Every page is a FastAPI route on Starlette, with nothing in between (docs/ARCHITECTURE.md, D9). A module in `app/web`
holds a router (or two) of pages; `app/web/http.py` adds only what pages need beyond FastAPI and Starlette: the
session, signing in, CSRF, messages, forms, addresses by name and the answers a page gives. `front()` in
`app/api/__init__.py` puts every router into the one FastAPI app the platform serves (`app.asgi`).

## A page

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

A page inside a workspace:

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

`app/web/account.py`, `settings.py` and `erp_records.py` are good examples to read.

## What a page uses

| Need | Write | Notes |
|---|---|---|
| A page router | `APIRouter(route_class=PageRoute, dependencies=[Depends(check_csrf)])` | `prefix=` for a shared address start, such as `/w/{wid:int}/app` |
| A route | `@router.api_route(path, methods=[...], name="area.function")` | every route has a name; templates and other modules find it by it |
| The request | `request: Request` (first parameter) | `from fastapi import Request`; it is a `PageRequest` |
| What the page looked up | `request.state.ws`, `.member`, `.engine`, ... | per request |
| The session | `request.state.session` | a dict in the signed `session` cookie |
| Who is signed in | `current_user(request)` | an `AnonymousUser` (`is_authenticated` False) when nobody is |
| Only for someone signed in | `user=Depends(signed_in)`, or `dependencies=[Depends(signed_in)]` | anyone else is sent to `/login?next=...` |
| Signing in and out | `login_user(request, user, remember=True)`, `logout_user(request)` | `remember` sets the "keep me signed in" cookie |
| A workspace page's access check | `ws=Depends(member_of("editor"))` (app/security.py), or `workspace(wid, "editor", request)` | sets `request.state.ws` and `.member`; a 404 to someone not in it, a 403 to a role too low |
| The ERP's engine | `ws, engine = ctx(wid, "editor", request)` (app/web/erp.py) | sets `request.state.engine` too |
| The form | `form: Form = Depends(form_of)` | `form.get(k, default, type=int)`, `form.getlist(k)`, `form[k]` (a 400 if missing), `form.to_dict()`, `form.items(multi=True)` |
| Uploaded files | `form.files.getlist("files")`, `form.files.get("file")` | each an `Upload`: `.filename`, `.read()`, `.save(path)`, `.mimetype`, `.content_type`, `.stream` |
| JSON posted | `data=Depends(json_of)` | the body as JSON, or None |
| The raw body | `body: bytes = Depends(body_of)` | a 413 over MAX_CONTENT_LENGTH |
| The query | `request.query_params.get(k)`, `.getlist(k)`, `k in request.query_params` | a name sent twice gives its first value (below); convert with a small helper, as `_int(...)` |
| Where the request went | `request.url.path`, `full_path(request)` (with its query), `request.headers.get("referer")` | `security.client_ip(request)` for who's connecting |
| CSRF | the router's `Depends(check_csrf)`; `csrf_token(request)` | templates call `csrf_token()` |
| Messages | `flash(request, message, category)`; `flashed(request, with_categories=True)` | templates call `flashed(...)` (macros.html shows them) |
| Addresses | `url_for("area.function", **values)` | path values fill the path, the rest the query (lists repeat, None is left out); `_external=True` puts BASE_URL in front, `_anchor=` adds `#...`; it needs the platform's context, not a request |
| A page | `render(request, "x.html", status_code=200, **context)` | app/templates, with what every page gets (below) |
| A redirect | `redirect(url, code=302)` | |
| JSON | `json_response(data, status_code=200, headers=None)` | sorted keys, dates and decimals as text |
| A file | `send_file(request, path_or_bytesio, mimetype=None, as_attachment=False, download_name=None, ...)` | ETag, Last-Modified, 304 and byte ranges; the browser checks it again each time |
| Any other response | `from starlette.responses import Response`: `Response(body, media_type="text/csv", headers={...})` | `text/*` gets `charset=utf-8` |
| Stop with an error page | `raise HTTPException(404)`, `raise HTTPException(403, "why")` | `from fastapi import HTTPException`; the platform's page for it; JSON under /api/ |
| Stop with a redirect | `raise Redirect(url)` | from a helper or a dependency |
| Stop with a response | `raise Answer(response)` | the v1 API's `raise Answer(_error(404, ...))` |
| A check before every page of a router | a dependency: `APIRouter(..., dependencies=[Depends(check_csrf), Depends(the_check)])` | it raises `HTTPException` or `Redirect` to stop the request |
| A template global | `@template_global` on the function | |
| What every template gets besides | `@context_processor` on a function returning a dict | the workspace's menu (erp.py), the platform's name (filters.py) |
| The request, far from any view | `current_request()` | only where passing `request` would ripple far (app/security.py, the template globals) |
| The app | `from ..core.context import current_app` | its `config`, `logger` |

Every template gets `request`, `session`, `current_user`, `config`, `csrf_token()`, `flashed()`, `url_for()`,
`full_path()`, what the context processors give (`ws`, `member`, `platform_name`, the menu) and the filters and globals
of `app/web/filters.py` (`icon`, `pill`, `|when`, `|ago`, ...).

## What `PageRoute` does for every request

So a view never does it: it gives the view a `PageRequest`; enters the platform's context as `app.app_context()` does
(the app, its settings, a database session of the request's own, and who is acting: the signed-in person, their
address and the workspace once the page has looked it up, for the audit log and row-level security); makes the request
`current_request()`; reads the session cookie; runs the view (a `def` view in a worker thread, an `async def` one on
the event loop); turns an error into its page (the platform's own for 403, 404, 413 and 500; JSON under /api/; under
TESTING a bug is raised to the caller instead); then writes the "keep me signed in" and session cookies, adds the
security headers (`security.page_headers`), records the time for Settings › Health, and gives the database session back.

Outside a page's own handling, `install()` answers the same way: an address no page has is the 404 page; an address a
page has with a closing slash (`/account`) is sent on to it with a 308, its query kept; a page asked with a method it
doesn't take is a 405.

## Rules

- **Names.** Every route has `name="area.function"`: the area is the module's part of the platform (`app`, `settings`,
  `portal`, `join`, `auth`, `api`, ...), the function the view's. Several routes may share a name (one view at two
  addresses); `url_for` picks the one whose path values it's given.
- **Typed path values.** `{rid:int}` (so `/t/orders/new` never reaches a view expecting a number), `{key}`,
  `{filename:path}`. A GET route answers HEAD too.
- **Order.** Starlette tries routes in the order they're added. Put the more particular address first: `/p/{key}.pdf`
  before `/p/{key}` (which would also take `x.pdf`), fixed text before a parameter in the same place
  (`/approvals/away` before `/approvals/{aid}`). `tests/test_http.py` checks every page is reached by its own address.
- **CSRF.** Every router whose forms people post from their browser has `dependencies=[Depends(check_csrf)]`, public
  forms included (they post the token their page gave). Only routers called by other systems leave it out: the v1 API
  and MCP (`api.router`, checked by their API key), webhooks (`api.hooks`) and Stripe's events (`connect_apps.stripe`,
  checked by their signature).
- **The query.** A page's request reads a name sent twice (`?sort=name&sort=total`) as its first value in `.get`,
  `[...]`, `dict(...)` and `.items()`, as the pages always have; `.getlist` gives every value.
- **Answers are Starlette responses.** A view returns `render(...)`, `redirect(...)`, `json_response(...)`,
  `send_file(...)` or a `Response`; a bare `return "", 204` is `return Response(status_code=204)`.
- **Helpers take the request.** A module's helpers that read the request take `request` as their first parameter
  (`ctx(wid, role, request)`, `stepped_up(request)`, `client_ip(request)`); `current_request()` is for the few that are
  far from any view.

## A new module

1. Write its router(s) as above, in `app/web/<module>.py`.
2. In `app/api/__init__.py` (`front()`), import the module on the import lines and add `module.router` (one a line) to
   the list given to `http.install`.
3. Its templates go in `app/templates`; `url_for` finds its pages by their names.
4. Test it with `app.test_client()` (below), then run `tests/test_http.py` and the whole suite
   (`PYTHONPATH=. .venv/bin/python -m pytest tests -q -p no:cacheprovider`).

## Testing pages

`app.test_client()` (`app/web/testing.py`) sends requests through the FastAPI app, as a browser does, on Starlette's
TestClient:

```python
client = app.test_client()
page = client.post("/login", data={"email": "ethan@example.com", "password": "password1234"}, follow_redirects=True)
page.status_code, page.get_data(as_text=True), page.get_json(), page.headers["Location"], page.location, page.history
client.post(url, data={"x": ["a", "b"], "file": (io.BytesIO(b"..."), "a.csv")})   # a form, with an upload
client.post(url, json={...}, headers={"Authorization": "Bearer keel_..."})
with client.session_transaction() as session:                                     # the browser's session
    session["_user_id"] = str(user.id)
```

A request made while the test holds the app's context (`with app.app_context():`, as `tests/conftest.py` does) shares
the test's database session, so what the request saved is what the test reads next; each request still has a
`request.state` of its own. To call a page's helper directly, with no browser, use
`with a_request(path, user=user) as request:` (`app/web/testing.py`): it is `current_request()` for the block, with an
empty session and `user` signed in.

The builder's try-outs use the same client on a copy of the app (`app/builder/trial.py`).
