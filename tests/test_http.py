"""The pages served natively (app/web/http.py, docs/WEB.md) and the old web kit's, side by side: one session, one
sign-in, one CSRF token and one queue of messages; each finds the other's addresses; errors, methods and closing
slashes answered as they always were."""
import re

from app.extensions import db
from app.models import User

from .conftest import login


def _token(page):
    return re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)


def test_signing_in_natively_is_signing_in_everywhere_and_out_again(client, owner):
    user, ws = owner
    assert client.get(f"/w/{ws.id}/app/").status_code == 302  # an old layer's page: not signed in yet
    answer = login(client)  # a native page
    assert answer.status_code == 302 and answer.headers["Location"] == "/"
    cookies = answer.headers.get_list("set-cookie")
    assert any(c.startswith("session=") for c in cookies) and any(c.startswith("remember_token=") for c in cookies)
    assert client.get(f"/w/{ws.id}/app/").status_code == 200  # the old layer reads the native session
    assert client.get("/account/").status_code == 200
    client.post("/logout")
    assert client.get(f"/w/{ws.id}/app/").status_code == 302
    gone = client.get("/account/")
    assert gone.status_code == 302 and gone.headers["Location"] == "/login?next=%2Faccount%2F"


def test_messages_cross_from_one_layer_to_the_other(client, owner):
    user, ws = owner
    login(client)
    client.post("/account/", data={"action": "profile", "name": "Ethan B"})  # flashed natively
    assert "Saved." in client.get(f"/w/{ws.id}/app/").get_data(as_text=True)  # shown by the old layer
    assert "Saved." not in client.get("/account/").get_data(as_text=True)  # once
    db.session.refresh(user)
    assert user.name == "Ethan B"


def test_forms_carry_the_token_the_page_gave(app, client, owner):
    app.config["WTF_CSRF_ENABLED"] = True
    try:
        refused = client.post("/login", data={"email": "ethan@example.com", "password": "password1234"})
        assert refused.status_code == 400 and "The CSRF token is missing." in refused.get_data(as_text=True)
        token = _token(client.get("/login").get_data(as_text=True))
        signed = client.post("/login", data={"email": "ethan@example.com", "password": "password1234", "csrf_token": token})
        assert signed.status_code == 302
        _, ws = owner
        page = client.get(f"/w/{ws.id}/app/").get_data(as_text=True)
        assert _token(page) == token  # the old layer's pages give the same token
        assert client.post("/account/", data={"action": "profile", "name": "X"}, headers={"X-CSRFToken": token}).status_code == 302
        assert client.post("/account/", data={"action": "profile", "name": "X", "csrf_token": "forged"}).status_code == 400
    finally:
        app.config["WTF_CSRF_ENABLED"] = False


def test_each_layer_finds_the_others_addresses(app, client, owner):
    from app.web import http
    from app.webkit import url_for as old_url_for
    _, ws = owner
    assert http.url_for("auth.login", next="/w/1/app/") == "/login?next=/w/1/app/"
    assert http.url_for("auth.reset", token="a b") == "/reset/a%20b"
    assert http.url_for("app.record", wid=ws.id, table="orders", rid=7) == f"/w/{ws.id}/app/t/orders/7"  # an old page's
    assert http.url_for("account.page", _external=True, _anchor="two-step") == "http://localhost:5050/account/#two-step"
    with app.test_request_context():
        assert old_url_for("workspaces.index") == "/workspaces"  # a native page's, from the old layer
    login(client)
    page = client.get(f"/w/{ws.id}/app/").get_data(as_text=True)  # the old layer's menu links to the native pages
    assert 'href="/account/"' in page and 'action="/logout"' in page and 'href="/workspaces"' in page


def test_methods_slashes_and_errors_are_answered_as_before(app, client, owner):
    assert client.get("/logout").status_code == 405  # a native page asked with a method it doesn't take
    moved = client.get("/account?x=1")
    assert moved.status_code == 308 and moved.headers["Location"] == "/account/?x=1"
    assert client.head("/login").status_code == 200
    gone = client.get("/reset/not-a-token")
    assert gone.status_code == 404 and "This link isn" in gone.get_data(as_text=True)
    assert client.get("/login").headers["X-Frame-Options"] == "SAMEORIGIN"
    assert client.get("/api/v2/app").get_json() == {"detail": "Send your API key as: Authorization: Bearer keel_..."}


def test_what_a_native_page_raises_is_answered_as_its_page(app, client, monkeypatch):
    from fastapi import HTTPException

    from app.web import auth, http

    def raising(exc):
        def view(*args, **kwargs):
            raise exc
        return view

    monkeypatch.setattr(auth, "render", raising(HTTPException(403, "Only the owner can see this.")))
    page = client.get("/login")
    assert page.status_code == 403 and "Only the owner can see this." in page.get_data(as_text=True)
    monkeypatch.setattr(auth, "render", raising(http.Answer(http.json_response({"error": "teapot"}, 418))))
    assert client.get("/login").status_code == 418 and client.get("/login").get_json() == {"error": "teapot"}
    monkeypatch.setattr(auth, "render", raising(http.Redirect("/somewhere")))
    assert client.get("/login").headers["Location"] == "/somewhere"
    monkeypatch.setattr(auth, "render", raising(RuntimeError("a bug")))
    app.config["PROPAGATE_EXCEPTIONS"] = False
    try:
        page = client.get("/login")
        assert page.status_code == 500 and "Sorry, something went wrong" in page.get_data(as_text=True)
    finally:
        app.config["PROPAGATE_EXCEPTIONS"] = None


def test_a_native_request_shares_the_tests_database_session(client, owner):
    user, _ = owner
    login(client)
    client.post("/account/", data={"action": "profile", "name": "Renamed", "notify_email": "1"})
    assert user.name == "Renamed" and user.notify_email  # the same object the request changed
    assert db.session.get(User, user.id) is user


def test_every_native_page_is_served_by_one_layer_and_reached_by_its_own_address(app):
    """A guard for each module converted (docs/WEB.md): its pages are gone from the old layer, and none is hidden behind
    a route tried before it (Starlette tries routes in the order they were added: fixed text before a {parameter})."""
    from starlette.convertors import IntegerConvertor, PathConvertor
    from starlette.routing import Match

    from app.web import http
    ordered = app.extensions["page_routes"]
    assert {r.name for r in ordered} >= {"auth.login", "account.page", "workspaces.home"}
    for route in ordered:
        assert route.name not in app.url_map.by_endpoint, f"{route.name} is still registered in the old layer (app/__init__.py)"
        sample = {k: 7 if isinstance(c, IntegerConvertor) else "a/b" if isinstance(c, PathConvertor) else "sample"
                  for k, c in route.param_convertors.items()}
        path = http.path_for(app, route.name, sample).split("?")[0]
        method = sorted(route.methods - {"HEAD"})[0]
        scope = {"type": "http", "path": path, "method": method, "headers": [], "query_string": b"", "root_path": ""}
        first = next(r for r in ordered if r.matches(dict(scope))[0] == Match.FULL)
        assert first is route, f"{path} ({route.name}) is reached by {first.name}, added before it: put {route.name} first"


def test_no_native_page_takes_an_address_the_old_layer_still_serves(app):
    """The native pages are tried first: none may answer an address that's still an old page's (a {parameter} where the
    old address has fixed text, say). Transition: goes with app/webkit."""
    from starlette.routing import Match

    from app.webkit.routing import IntConverter, PathConverter
    ordered = app.extensions["page_routes"]
    for rule in app.url_map.rules:
        sample = {k: 7 if isinstance(c, IntConverter) else "a/b" if isinstance(c, PathConverter) else "sample" for k, c in rule.converters.items()}
        path, _ = rule.build(sample)
        for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
            scope = {"type": "http", "path": path, "method": method, "headers": [], "query_string": b"", "root_path": ""}
            taken = next((r for r in ordered if r.matches(dict(scope))[0] == Match.FULL), None)
            assert taken is None, f"{method} {path} is still the old layer's {rule.endpoint}, but the native {taken.name} answers it"
