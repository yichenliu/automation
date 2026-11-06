"""The pages on FastAPI and Starlette (app/web/http.py, docs/WEB.md): addresses, forms and query values, the session,
signing in, CSRF, messages, errors and closing slashes, files, and the test client the tests and try-outs use."""
import io
import re

import pytest
from fastapi import APIRouter, Depends, Request
from starlette.responses import Response

from app.core.passwords import check_password, hash_password
from app.extensions import db
from app.models import User
from app.web import http
from app.web.testing import a_request

from .conftest import login


def _token(page):
    return re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)


@pytest.fixture
def kit(app, owner):
    """A few pages of the tests' own beside the platform's, written as every page is."""
    user, _ = owner
    pages = APIRouter(prefix="/kit/{wid:int}", route_class=http.PageRoute, dependencies=[Depends(http.check_csrf)])
    hooks = APIRouter(route_class=http.PageRoute)  # called by other systems: no CSRF token

    @pages.api_route("/t/{table}/new", methods=["GET"], name="kit.new")
    def new(request: Request, wid: int, table: str):
        return Response(f"new {table}")

    @pages.api_route("/t/{table}/{rid:int}", methods=["GET"], name="kit.record")
    def record(request: Request, wid: int, table: str, rid: int):
        return Response(f"record {rid}")

    @pages.api_route("/t/{table}/{key}", methods=["GET"], name="kit.view")
    def view(request: Request, wid: int, table: str, key: str):
        return Response(f"view {key}")

    @pages.api_route("/form", methods=["POST"], name="kit.form")
    def form(request: Request, wid: int, form: http.Form = Depends(http.form_of)):
        upload = form.files.get("file")
        return http.json_response({"n": form.get("n", type=int), "many": form.getlist("x"), "must": form["must"],
                                   "file": [upload.filename, upload.read().decode()] if upload else None})

    @pages.api_route("/query", methods=["GET"], name="kit.query")
    def query(request: Request, wid: int):
        q = request.query_params
        return http.json_response({"sort": q.get("sort"), "first": q["sort"], "all": q.getlist("sort"), "dict": dict(q),
                                   "items": sorted(q.items())})

    @pages.api_route("/sign-in", methods=["POST"], name="kit.sign_in")
    def sign_in(request: Request, wid: int):
        http.login_user(request, user, remember=True)
        http.flash(request, "Welcome back.", "success")
        return http.redirect(http.url_for("kit.mine", wid=wid))

    @pages.api_route("/mine", methods=["GET"], name="kit.mine")
    def mine(request: Request, wid: int, who=Depends(http.signed_in)):
        return Response(f"{who.id} {http.flashed(request, with_categories=True)}")

    @pages.api_route("/broken", methods=["GET"], name="kit.broken")
    def broken(request: Request, wid: int):
        raise RuntimeError("the view broke")

    @hooks.api_route("/kit-hooks/{token}", methods=["POST"], name="kit.hook")
    def hook(request: Request, token: str):
        return Response(status_code=202)

    http.install(app.asgi, app, [pages, hooks])
    return app.test_client()


def test_signing_in_keeps_the_person_and_signing_out_forgets_them(client, owner):
    user, ws = owner
    assert client.get(f"/w/{ws.id}/app/").status_code == 302  # not signed in yet
    answer = login(client)
    assert answer.status_code == 302 and answer.headers["Location"] == "/"
    cookies = answer.headers.get_list("set-cookie")
    assert any(c.startswith("session=") for c in cookies) and any(c.startswith("remember_token=") for c in cookies)
    assert client.get(f"/w/{ws.id}/app/").status_code == 200
    assert client.get("/account/").status_code == 200
    client.post("/logout")
    assert client.get(f"/w/{ws.id}/app/").status_code == 302
    gone = client.get("/account/")
    assert gone.status_code == 302 and gone.headers["Location"] == "/login?next=%2Faccount%2F"


def test_the_session_and_a_message_for_the_next_page(kit, owner):
    user, _ = owner
    away = kit.get("/kit/1/mine")
    assert away.status_code == 302 and away.location == "/login?next=%2Fkit%2F1%2Fmine"
    signed = kit.post("/kit/1/sign-in", follow_redirects=True)
    assert signed.get_data(as_text=True) == f"{user.id} [('success', 'Welcome back.')]" and len(signed.history) == 1
    assert kit.get("/kit/1/mine").get_data(as_text=True) == f"{user.id} []"  # a message is shown once
    with kit.session_transaction() as session:
        assert session["_user_id"] == str(user.id) and "_id" in session
        del session["_user_id"]
    remembered = kit.get("/kit/1/mine")  # the session forgot them, the "keep me signed in" cookie didn't
    assert remembered.get_data(as_text=True) == f"{user.id} []"
    fresh = kit.application.test_client()  # signed in without the form, as a try-out's people are
    with fresh.session_transaction() as session:
        session["_user_id"], session["_fresh"] = str(user.id), True
    assert fresh.get("/kit/1/mine").status_code == 200
    kit.application.config["SECRET_KEY"] = "another"  # a cookie signed with another key is no session at all
    try:
        assert fresh.get("/kit/1/mine").status_code == 302
    finally:
        kit.application.config["SECRET_KEY"] = "test"


def test_messages_are_shown_on_the_next_page_once(client, owner):
    user, ws = owner
    login(client)
    client.post("/account/", data={"action": "profile", "name": "Ethan B"})
    assert "Saved." in client.get(f"/w/{ws.id}/app/").get_data(as_text=True)
    assert "Saved." not in client.get("/account/").get_data(as_text=True)
    db.session.refresh(user)
    assert user.name == "Ethan B"


def test_forms_carry_the_token_their_page_gave_and_other_systems_are_exempt(app, kit, owner):
    app.config["WTF_CSRF_ENABLED"] = True
    try:
        refused = kit.post("/login", data={"email": "ethan@example.com", "password": "password1234"})
        assert refused.status_code == 400 and "The CSRF token is missing." in refused.get_data(as_text=True)
        token = _token(kit.get("/login").get_data(as_text=True))
        assert kit.post("/login", data={"email": "ethan@example.com", "password": "password1234", "csrf_token": token}).status_code == 302
        _, ws = owner
        again = _token(kit.get(f"/w/{ws.id}/app/").get_data(as_text=True))
        assert again.split(".")[0] == token.split(".")[0]  # one secret for the session, signed with the time each page asked
        assert kit.post("/account/", data={"action": "profile", "name": "X"}, headers={"X-CSRFToken": token}).status_code == 302
        assert kit.post("/account/", data={"action": "profile", "name": "X", "csrf_token": "forged"}).status_code == 400
        assert kit.post("/kit/1/form", data={"must": "x"}).status_code == 400
        assert kit.post("/kit-hooks/abc").status_code == 202
    finally:
        app.config["WTF_CSRF_ENABLED"] = False


def test_url_for_finds_a_page_by_its_name_outside_a_request_too(app, client, owner):
    _, ws = owner
    assert http.url_for("auth.login", next="/w/1/app/") == "/login?next=/w/1/app/"
    assert http.url_for("auth.reset", token="a b") == "/reset/a%20b"
    assert http.url_for("app.record", wid=ws.id, table="orders", rid=7, back="/x y", tags=["a", "b"], gone=None, _anchor="top") == \
        f"/w/{ws.id}/app/t/orders/7?back=/x+y&tags=a&tags=b#top"
    assert http.url_for("account.page", _external=True, _anchor="two-step") == "http://localhost:5050/account/#two-step"
    assert http.url_for("static", filename="vendor/leaflet/leaflet.css") == "/static/vendor/leaflet/leaflet.css"
    with pytest.raises(http.BuildError, match="it needs wid"):
        http.url_for("app.record", table="orders", rid=7)
    with pytest.raises(http.BuildError, match="no page has that name"):
        http.url_for("app.nowhere")
    login(client)
    page = client.get(f"/w/{ws.id}/app/").get_data(as_text=True)
    assert 'href="/account/"' in page and 'action="/logout"' in page and 'href="/workspaces"' in page


def test_an_address_finds_its_page_fixed_words_first(kit):
    assert kit.get("/kit/1/t/orders/new").data == b"new orders"
    assert kit.get("/kit/1/t/orders/7").data == b"record 7"
    assert kit.get("/kit/1/t/orders/open").data == b"view open"
    assert kit.get("/kit/x/t/orders/7").status_code == 404


def test_forms_values_files_and_a_missing_one(kit):
    got = kit.post("/kit/1/form", data={"n": "12", "x": ["a", "b"], "must": "yes", "file": (io.BytesIO(b"hello"), "a.txt")}).get_json()
    assert got == {"n": 12, "many": ["a", "b"], "must": "yes", "file": ["a.txt", "hello"]}
    assert kit.post("/kit/1/form", data={"n": "twelve", "must": ""}).get_json()["n"] is None
    missing = kit.post("/kit/1/form", data={"n": "1"})
    assert missing.status_code == 400 and b"Bad Request" in missing.data


def test_a_name_sent_twice_in_the_query_reads_as_its_first_value(kit):
    got = kit.get("/kit/1/query?sort=name&sort=total&q=x").get_json()
    assert got == {"sort": "name", "first": "name", "all": ["name", "total"], "dict": {"sort": "name", "q": "x"},
                   "items": [["q", "x"], ["sort", "name"]]}


def test_methods_slashes_and_errors(app, client, kit):
    assert client.get("/logout").status_code == 405  # a page asked with a method it doesn't take
    moved = client.get("/account?x=1")  # a page's address without its closing slash
    assert moved.status_code == 308 and moved.location == "/account/?x=1" and moved.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert client.head("/login").status_code == 200
    gone = client.get("/reset/not-a-token")
    assert gone.status_code == 404 and "This link isn" in gone.get_data(as_text=True)
    nowhere = client.get("/no/such/page")
    assert nowhere.status_code == 404 and "find this page" in nowhere.get_data(as_text=True)
    assert nowhere.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert client.get("/login").headers["X-Frame-Options"] == "SAMEORIGIN"
    assert client.get("/api/v2/app").get_json() == {"detail": "Send your API key as: Authorization: Bearer keel_..."}
    with pytest.raises(RuntimeError, match="the view broke"):  # under TESTING a bug is raised to the test
        kit.get("/kit/1/broken")
    app.config["PROPAGATE_EXCEPTIONS"] = False
    try:
        shown = kit.get("/kit/1/broken")
        assert shown.status_code == 500 and "Sorry, something went wrong" in shown.get_data(as_text=True)
    finally:
        app.config["PROPAGATE_EXCEPTIONS"] = None


def test_what_a_page_raises_is_answered_as_its_page(app, client, monkeypatch):
    from fastapi import HTTPException

    from app.web import auth

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


def test_the_platforms_own_files(client):
    css = client.get("/static/app.css")
    assert css.status_code == 200 and css.mimetype == "text/css" and css.headers["Cache-Control"] == "no-cache"
    assert client.get("/static/app.css", headers={"If-None-Match": css.headers["ETag"]}).status_code == 304
    assert client.get("/static/../config.py").status_code == 404 and client.get("/static/nothing.js").status_code == 404


def test_a_request_shares_the_tests_database_session(client, owner):
    user, _ = owner
    login(client)
    client.post("/account/", data={"action": "profile", "name": "Renamed", "notify_email": "1"})
    assert user.name == "Renamed" and user.notify_email  # the same object the request changed
    assert db.session.get(User, user.id) is user


def test_a_pages_helper_called_with_no_browser(owner):
    from app.core import actor
    user, ws = owner
    with a_request("/w/1/app/?sort=a&sort=b", user=user) as request:
        assert http.current_request() is request and http.current_user(request) is user
        assert request.query_params["sort"] == "a"
        request.state.ws = ws
        assert actor.workspace_id() == ws.id and actor.user() is user
    assert http.current_request() is None and actor.workspace_id() is None


def test_every_page_is_reached_by_its_own_address(app):
    """Starlette tries routes in the order they were added: a page hidden behind one tried before it (a {parameter}
    where it has fixed text) is never reached. Fixed text goes first."""
    from starlette.convertors import IntegerConvertor, PathConvertor
    from starlette.routing import Match

    ordered = app.extensions["page_routes"]
    assert {r.name for r in ordered} >= {"auth.login", "account.page", "workspaces.home", "app.record", "static"}
    for route in ordered:
        sample = {k: 7 if isinstance(c, IntegerConvertor) else "a/b" if isinstance(c, PathConvertor) else "sample"
                  for k, c in route.param_convertors.items()}
        path = http.path_for(app, route.name, sample).split("?")[0]
        method = sorted(route.methods - {"HEAD"})[0]
        scope = {"type": "http", "path": path, "method": method, "headers": [], "query_string": b"", "root_path": ""}
        first = next(r for r in ordered if r.matches(dict(scope))[0] == Match.FULL)
        assert first is route, f"{path} ({route.name}) is reached by {first.name}, added before it: put {route.name} first"


def test_passwords_are_kept_in_the_same_form():
    stored = hash_password("password1234")
    assert stored.startswith("scrypt:32768:8:1$") and check_password(stored, "password1234") and not check_password(stored, "nope")
    old = "pbkdf2:sha256:1000$salt$" + __import__("hashlib").pbkdf2_hmac("sha256", b"pw", b"salt", 1000).hex()
    assert check_password(old, "pw") and not check_password("!", "pw")
