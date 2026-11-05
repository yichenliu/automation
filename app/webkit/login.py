"""Signing in: who the request is for (`current_user`), signing someone in and out, and pages only signed-in people
may open (`login_required`, which sends anyone else to the sign-in page and back again after).

Who is signed in is kept in the session under the keys it always has been (`_user_id`, `_fresh`, `_id`), and "keep me
signed in" in a `remember_token` cookie of the same form, so nobody is signed out by the move to this kit."""
import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from functools import wraps
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from .local import Proxy, app_ctx, request_ctx

COOKIE_NAME = "remember_token"
COOKIE_DURATION = timedelta(days=365)


class AnonymousUser:
    """Nobody signed in."""

    is_authenticated = False
    is_active = False
    is_anonymous = True

    def get_id(self):
        return None


def _digest(app, payload):
    key = app.config["SECRET_KEY"]
    key = key.encode("latin1") if isinstance(key, str) else key
    return hmac.new(key, payload.encode("utf-8"), hashlib.sha512).hexdigest()


def encode_cookie(app, payload):
    return f"{payload}|{_digest(app, payload)}"


def decode_cookie(app, cookie):
    try:
        payload, digest = cookie.rsplit("|", 1)
    except ValueError:
        return None
    return payload if hmac.compare_digest(_digest(app, payload), digest) else None


def _identifier(request):
    """Where the session is used from (its address and browser), so a session carried elsewhere isn't fresh."""
    address = request.headers.get("X-Forwarded-For", request.remote_addr)
    if address is not None:
        address = address.encode("utf-8").split(b",")[0].strip()
    agent = request.headers.get("User-Agent")
    if agent is not None:
        agent = agent.encode("utf-8")
    return hashlib.sha512(f"{address}|{agent}".encode("utf8")).hexdigest()


class LoginManager:
    def __init__(self):
        self.login_view = None
        self.login_message = "Please log in to access this page."
        self.login_message_category = "message"
        self._user_callback = None
        self.anonymous_user = AnonymousUser

    def init_app(self, app):
        app.login_manager = self
        app.after_request(self._update_remember_cookie)
        app.template_context_processors.append(lambda: {"current_user": _get_user()})

    def user_loader(self, callback):
        self._user_callback = callback
        return callback

    # who the request is for -------------------------------------------------------------------------------------------
    def _load_user(self, ctx):
        g = app_ctx.get().g
        session, request, app = ctx.session, ctx.request, ctx.app
        if session and _identifier(request) != session.get("_id") and session.get("_fresh") is not False:
            session["_fresh"] = False  # the session came from elsewhere: still signed in, but not freshly
        user = None
        user_id = session.get("_user_id")
        if user_id is not None and self._user_callback is not None:
            user = self._user_callback(user_id)
        if user is None:
            name = app.config.get("REMEMBER_COOKIE_NAME", COOKIE_NAME)
            if name in request.cookies and session.get("_remember") != "clear":
                user = self._from_cookie(app, session, request.cookies[name])
        g._login_user = user if user is not None else self.anonymous_user()

    def _from_cookie(self, app, session, cookie):
        user_id = decode_cookie(app, cookie)
        if user_id is None:
            return None
        session["_user_id"] = user_id
        session["_fresh"] = False
        return self._user_callback(user_id) if self._user_callback else None

    def unauthorized(self):
        from .helpers import abort, flash, redirect, url_for
        ctx = request_ctx.get()
        if not self.login_view:
            abort(401)
        if self.login_message:
            flash(self.login_message, category=self.login_message_category)
        base = url_for(self.login_view)
        return redirect(_login_url(base, ctx.request.url))

    # the "keep me signed in" cookie -----------------------------------------------------------------------------------
    def _update_remember_cookie(self, response):
        ctx = request_ctx.get()
        session, app = ctx.session, ctx.app
        if "_remember" not in session and app.config.get("REMEMBER_COOKIE_REFRESH_EACH_REQUEST"):
            if "_user_id" in session:
                session["_remember"] = "set"
        if "_remember" in session:
            operation = session.pop("_remember", None)
            config = app.config
            name = config.get("REMEMBER_COOKIE_NAME", COOKIE_NAME)
            domain = config.get("REMEMBER_COOKIE_DOMAIN")
            path = config.get("REMEMBER_COOKIE_PATH", "/")
            if operation == "set" and "_user_id" in session:
                duration = (timedelta(seconds=session["_remember_seconds"]) if "_remember_seconds" in session
                            else config.get("REMEMBER_COOKIE_DURATION", COOKIE_DURATION))
                if isinstance(duration, int):
                    duration = timedelta(seconds=duration)
                response.set_cookie(name, encode_cookie(app, str(session["_user_id"])),
                                    expires=datetime.now(timezone.utc) + duration, domain=domain, path=path,
                                    secure=config.get("REMEMBER_COOKIE_SECURE", False),
                                    httponly=config.get("REMEMBER_COOKIE_HTTPONLY", True),
                                    samesite=config.get("REMEMBER_COOKIE_SAMESITE"))
            elif operation == "clear":
                response.delete_cookie(name, domain=domain, path=path)
        return response


def _login_url(base, current):
    """`/login?next=/w/1/app/`: the sign-in page, and where to go after (this address, without its host)."""
    parts = urlsplit(base)
    query = parse_qs(parts.query, keep_blank_values=True)
    here = urlsplit(current)
    query["next"] = urlunsplit(("", "", here.path, here.query, "")) if (not parts.scheme or parts.scheme == here.scheme) and \
        (not parts.netloc or parts.netloc == here.netloc) else current
    return urlunsplit(parts._replace(query=urlencode(query, doseq=True)))


def _get_user():
    ctx = request_ctx.get()
    if ctx is None:
        return None
    g = app_ctx.get().g
    if "_login_user" not in g:
        manager = getattr(ctx.app, "login_manager", None)
        if manager is None:
            g._login_user = AnonymousUser()
        else:
            manager._load_user(ctx)
    return g._login_user


current_user = Proxy(_get_user)


def login_user(user, remember=False, duration=None, force=False, fresh=True):
    """Sign this person in for the rest of the session (and, with `remember`, the browser keeps them signed in)."""
    if not force and not user.is_active:
        return False
    ctx = request_ctx.get()
    session = ctx.session
    session["_user_id"] = user.get_id()
    session["_fresh"] = fresh
    session["_id"] = _identifier(ctx.request)
    if remember:
        session["_remember"] = "set"
        if duration is not None:
            session["_remember_seconds"] = int(duration.total_seconds())
    app_ctx.get().g._login_user = user
    return True


def logout_user():
    ctx = request_ctx.get()
    session, app = ctx.session, ctx.app
    for key in ("_user_id", "_fresh", "_id"):
        if key in session:
            session.pop(key)
    if app.config.get("REMEMBER_COOKIE_NAME", COOKIE_NAME) in ctx.request.cookies:
        session["_remember"] = "clear"
        if "_remember_seconds" in session:
            session.pop("_remember_seconds")
    manager = getattr(app, "login_manager", None)
    app_ctx.get().g._login_user = manager.anonymous_user() if manager else AnonymousUser()
    return True


def login_required(view):
    """Only for someone signed in; anyone else goes to the sign-in page, and comes back here after."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        ctx = request_ctx.get()
        if ctx.request.method == "OPTIONS" or ctx.app.config.get("LOGIN_DISABLED"):
            pass
        elif not current_user.is_authenticated:
            return ctx.app.login_manager.unauthorized()
        return view(*args, **kwargs)
    return wrapped
