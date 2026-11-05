"""The web kit the pages are written with (app/webkit): addresses, forms, the session, sign-in, CSRF and errors behave
as the views were written for."""
import pytest

from app.core.passwords import check_password, hash_password
from app.webkit import (App, Blueprint, CSRFProtect, LoginManager, abort, current_user, flash, g, get_flashed_messages, jsonify,
                        login_required, login_user, redirect, request, session, url_for)


class Person:
    is_active = is_authenticated = True
    is_anonymous = False

    def __init__(self, id):  # noqa: A002
        self.id = id

    def get_id(self):
        return str(self.id)


def _app(**config):
    app = App(__name__, static_folder=None)
    app.config.update({"SECRET_KEY": "test", "TESTING": True, **config})
    logins = LoginManager()
    logins.login_view = "sign_in"
    logins.login_message = None
    logins.init_app(app)
    logins.user_loader(lambda user_id: Person(int(user_id)))
    csrf = CSRFProtect()
    csrf.init_app(app)
    pages = Blueprint("pages", __name__, url_prefix="/w/<int:wid>")
    hooks = Blueprint("hooks", __name__)
    csrf.exempt(hooks)

    @pages.route("/")
    def home(wid):
        return f"home {wid}"

    @pages.route("/t/<table>/new")
    def new(wid, table):
        return f"new {table}"

    @pages.route("/t/<table>/<int:rid>")
    def record(wid, table, rid):
        return f"record {rid}"

    @pages.route("/t/<table>/<key>")
    def view(wid, table, key):
        return f"view {key}"

    @pages.route("/p/<key>.pdf")
    def pdf(wid, key):
        return f"pdf {key}"

    @pages.route("/p/<key>")
    def page(wid, key):
        return f"page {key}"

    @pages.route("/form", methods=["POST"])
    def form(wid):
        return jsonify(n=request.form.get("n", type=int), many=request.form.getlist("x"), must=request.form["must"])

    @pages.route("/link")
    def link(wid):
        return url_for(".record", wid=wid, table="orders", rid=7, back="/x y", tags=["a", "b"], gone=None, _anchor="top")

    @pages.route("/mine")
    @login_required
    def mine(wid):
        return f"{current_user.id} {get_flashed_messages(with_categories=True)}"

    @pages.route("/stop")
    def stop(wid):
        abort(redirect("/elsewhere"))

    @pages.route("/broken")
    def broken(wid):
        raise RuntimeError("the view broke")

    @hooks.route("/hooks/<token>", methods=["POST"])
    def hook(token):
        return "", 202

    app.register_blueprint(pages)
    app.register_blueprint(hooks)

    @app.route("/sign-in", methods=["GET", "POST"])
    def sign_in():
        if request.method == "POST":
            login_user(Person(5), remember=True)
            flash("Welcome back.", "success")
            return redirect(url_for("pages.mine", wid=request.args.get("wid", 1)))
        return "sign in"

    @app.errorhandler(404)
    def not_found(_):
        return "not here", 404

    return app


def test_an_address_finds_its_view_fixed_words_first():
    client = _app(WTF_CSRF_ENABLED=False).test_client()
    assert client.get("/w/1/t/orders/new").data == b"new orders"
    assert client.get("/w/1/t/orders/7").data == b"record 7"
    assert client.get("/w/1/t/orders/open").data == b"view open"
    assert client.get("/w/1/p/sales.pdf").data == b"pdf sales"
    assert client.get("/w/1/p/sales").data == b"page sales"
    moved = client.get("/w/1?tab=2")
    assert moved.status_code == 308 and moved.headers["Location"] == "/w/1/?tab=2"
    assert client.get("/w/x/").data == b"not here"
    wrong = client.get("/w/1/form")
    assert wrong.status_code == 405 and "POST" in wrong.headers["Allow"]


def test_url_for_puts_what_the_address_doesnt_name_in_its_query():
    client = _app().test_client()
    assert client.get("/w/3/link").get_data(as_text=True) == "/w/3/t/orders/7?back=/x+y&tags=a&tags=b#top"
    app = _app()
    with app.test_request_context("/w/3/"):
        assert url_for("pages.home", wid=3, _external=True) == "http://localhost/w/3/"
    with app.app_context(), pytest.raises(RuntimeError, match="SERVER_NAME"):
        url_for("pages.home", wid=3)


def test_forms_values_and_a_missing_one():
    client = _app(WTF_CSRF_ENABLED=False).test_client()
    got = client.post("/w/1/form", data={"n": "12", "x": ["a", "b"], "must": "yes"}).get_json()
    assert got == {"n": 12, "many": ["a", "b"], "must": "yes"}
    assert client.post("/w/1/form", data={"n": "twelve", "must": ""}).get_json()["n"] is None
    missing = client.post("/w/1/form", data={"n": "1"})
    assert missing.status_code == 400 and b"Bad Request" in missing.data


def test_signing_in_the_session_and_messages_for_the_next_page():
    client = _app(WTF_CSRF_ENABLED=False).test_client()
    away = client.get("/w/1/mine")
    assert away.status_code == 302 and away.headers["Location"] == "/sign-in?next=%2Fw%2F1%2Fmine"
    signed = client.post("/sign-in", follow_redirects=True)
    assert signed.get_data(as_text=True) == "5 [('success', 'Welcome back.')]"
    assert client.get("/w/1/mine").get_data(as_text=True) == "5 []"  # a message is shown once
    with client.session_transaction() as s:
        assert s["_user_id"] == "5" and "_id" in s
        s["_user_id"] = "9"
    assert client.get("/w/1/mine").get_data(as_text=True).startswith("9 ")
    other = _app(WTF_CSRF_ENABLED=False, SECRET_KEY="another").test_client()
    other._client.cookies = client._client.cookies  # a cookie signed with another key is no session at all
    assert other.get("/w/1/mine").status_code == 302


def test_forms_carry_the_csrf_token_and_other_systems_are_exempt():
    app = _app()
    client = app.test_client()
    refused = client.post("/w/1/form", data={"must": "x"})
    assert refused.status_code == 400 and b"The CSRF token is missing." in refused.data
    with app.test_request_context("/w/1/"):
        from app.webkit import generate_csrf
        token, kept = generate_csrf(), dict(session)
    with client.session_transaction() as s:
        s.update(kept)
    assert client.post("/w/1/form", data={"must": "x", "csrf_token": token}).status_code == 200
    assert client.post("/w/1/form", data={"must": "x"}, headers={"X-CSRFToken": token}).status_code == 200
    assert client.post("/w/1/form", data={"must": "x", "csrf_token": token + "x"}).status_code == 400
    assert client.post("/hooks/abc").status_code == 202


def test_errors_a_response_given_to_abort_and_a_broken_view():
    client = _app().test_client()
    assert client.get("/w/1/stop").headers["Location"] == "/elsewhere"
    with pytest.raises(RuntimeError, match="the view broke"):  # under TESTING it's raised to the test
        client.get("/w/1/broken")
    shown = _app(TESTING=False).test_client().get("/w/1/broken")
    assert shown.status_code == 500 and b"Internal Server Error" in shown.data


def test_g_is_the_requests_and_the_test_sees_what_a_request_left():
    app = _app()

    @app.before_request
    def remember():
        g.seen = request.path

    with app.app_context():
        app.test_client().get("/w/1/")
        assert g.seen == "/w/1/"  # the request ran in the test's context, as before
    app.test_client().get("/w/2/")  # outside one, each request has its own


def test_passwords_are_kept_in_the_same_form():
    stored = hash_password("password1234")
    assert stored.startswith("scrypt:32768:8:1$") and check_password(stored, "password1234") and not check_password(stored, "nope")
    old = "pbkdf2:sha256:1000$salt$" + __import__("hashlib").pbkdf2_hmac("sha256", b"pw", b"salt", 1000).hex()
    assert check_password(old, "pw") and not check_password("!", "pw")
