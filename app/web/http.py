"""The platform's pages on FastAPI and Starlette, with nothing in between (Keel v2, D9): what a page module needs that
FastAPI doesn't already give. docs/WEB.md says how a module is written with it, and how an old one is converted.

    router = APIRouter(route_class=PageRoute, dependencies=[Depends(check_csrf)])

    @router.api_route("/account/", methods=["GET", "POST"], name="account.page")
    def page(request: Request, user=Depends(signed_in), form: Form = Depends(form_of)):
        if request.method == "POST":
            flash(request, "Saved.", "success")
            return redirect(url_for("account.page"))
        return render(request, "account.html", user=user)

Every route of a page router is a PageRoute: each request is served in the platform's context (its app, settings,
a database session of its own, who is acting), with the browser's session in `request.state.session`, and finished as
every page is: the session and "keep me signed in" cookies written, the security headers, its time kept for
Settings › Health, an error shown as its page (JSON under /api/), and its database session given back.

    the request       current_request(), form_of, json_of, body_of (dependencies), full_path(request)
    the session       request.state.session: the signed cookie the platform has always used
    signing in        current_user(request), signed_in (dependency), login_user, logout_user
    CSRF              csrf_token(request), check_csrf (dependency)
    messages          flash(request, ...), flashed(request)
    addresses         url_for("module.function", **values)
    answers           render, redirect, json_response, send_file; raise Redirect(url), Answer(response) or
                      fastapi.HTTPException(code) (its page; JSON under /api/)
    templates         @template_global, @context_processor

While the old web kit (app/webkit) still serves the pages not converted yet, the two share one session cookie, one
sign-in, one CSRF token and one queue of messages, and each finds the other's addresses (url_for). The old layer's
requests are seen here as native ones too (current_request()), so the helpers both use (app/security.py, the
template globals) are written once, natively. The few lines that make that so are marked "transition"; they go with
app/webkit.
"""
import contextlib
import contextvars
import dataclasses
import decimal
import functools
import hashlib
import hmac
import io
import json
import logging
import mimetypes
import os
import time
import unicodedata
import uuid
import zlib
from contextlib import ExitStack
from datetime import date, datetime, timedelta, timezone
from email.utils import format_datetime, parsedate_to_datetime
from http import HTTPStatus
from urllib.parse import parse_qs, quote, urlencode, urlparse, urlsplit, urlunsplit

from fastapi import HTTPException, Request
from fastapi.routing import APIRoute
from itsdangerous import BadData, BadSignature, SignatureExpired, URLSafeTimedSerializer
from markupsafe import Markup, escape
from starlette.concurrency import run_in_threadpool
from starlette.convertors import IntegerConvertor, PathConvertor
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import FileResponse, HTMLResponse, Response
from starlette.routing import Match, Mount

from ..core import actor, context as core_context
from ..core.settings import settings, using as using_settings

_log = logging.getLogger(__name__)

# --- the request being served -------------------------------------------------------------------------------------------

_request = contextvars.ContextVar("keel_http_request", default=None)


def current_request():
    """The request being served, or None. For the few helpers far from any view (app/security.py, the template
    globals); a view passes its `request` on instead."""
    return _request.get()


def full_path(request):
    """`/w/1/app/t/orders?page=2`: where the request went, with its query (`?` alone when it has none, as it always was)."""
    return f"{request.url.path}?{request.url.query}"


# --- the session: a small dict in a signed cookie ----------------------------------------------------------------------

SESSION_SALT = "cookie-session"


class _JSON:
    """Plain JSON, compact; a tuple, Markup or bytes written by the old cookie format reads back as it was."""

    @staticmethod
    def dumps(value):
        return json.dumps(value, separators=(",", ":"), default=_tagged)

    @staticmethod
    def loads(value):
        return json.loads(value, object_hook=_untag)


def _tagged(value):
    if isinstance(value, bytes):
        import base64
        return {" b": base64.b64encode(value).decode("ascii")}
    raise TypeError(f"Object of type {type(value).__name__} can't be kept in the session")


def _untag(obj):
    if len(obj) == 1:
        key, value = next(iter(obj.items()))
        if key == " t":
            return tuple(value)
        if key == " m":
            return Markup(value)
        if key == " b":
            import base64
            return base64.b64decode(value)
        if key == " di":
            return value
    return obj


class Session(dict):
    """The session's values. Setting, removing or adding one marks it changed, so the cookie is written again; reading
    it marks it read, so caches are told the page depends on the cookie."""

    def __init__(self, initial=None):
        super().__init__(initial or {})
        self.modified = False
        self.accessed = False
        self.new = initial is None
        self.permanent = bool(self.get("_permanent"))

    def _changed(self):
        self.modified = True
        self.accessed = True

    def __getitem__(self, key):
        self.accessed = True
        return super().__getitem__(key)

    def get(self, key, default=None):
        self.accessed = True
        return super().get(key, default)

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self._changed()

    def __delitem__(self, key):
        super().__delitem__(key)
        self._changed()

    def setdefault(self, key, default=None):
        self.accessed = True
        if key not in self:
            super().__setitem__(key, default)
            self._changed()
        return super().__getitem__(key)

    def pop(self, key, *default):
        self.accessed = True
        if key in self:
            self._changed()
        return super().pop(key, *default)

    def popitem(self):
        self._changed()
        return super().popitem()

    def update(self, *args, **kwargs):
        super().update(*args, **kwargs)
        self._changed()

    def clear(self):
        super().clear()
        self._changed()


def _session_signer(config):
    secret = config.get("SECRET_KEY")
    if not secret:
        return None
    return URLSafeTimedSerializer(secret, salt=SESSION_SALT, serializer=_JSON,
                                  signer_kwargs={"key_derivation": "hmac", "digest_method": hashlib.sha1})


def session_lifetime(config):
    value = config.get("PERMANENT_SESSION_LIFETIME") or timedelta(days=31)
    return value if isinstance(value, timedelta) else timedelta(seconds=int(value))


def session_cookie_name(config):
    return config.get("SESSION_COOKIE_NAME") or "session"


def load_session(config, value):
    """The session in a cookie's value (an empty one if it's missing, tampered with or too old)."""
    signer = _session_signer(config)
    if signer is None or not value:
        return Session()
    try:
        return Session(signer.loads(value, max_age=int(session_lifetime(config).total_seconds())))
    except (BadSignature, ValueError, TypeError):
        return Session()


def dump_session(config, session):
    return _session_signer(config).dumps(dict(session))


def save_session(config, session, response):
    """The cookie written on the response when the session changed (or, kept for a while, on every response); taken
    away when it was emptied. `response` is a Starlette response (or, until it goes, the old layer's)."""
    name = session_cookie_name(config)
    domain = config.get("SESSION_COOKIE_DOMAIN") or None
    path = config.get("SESSION_COOKIE_PATH") or config.get("APPLICATION_ROOT") or "/"
    secure = bool(config.get("SESSION_COOKIE_SECURE"))
    httponly = bool(config.get("SESSION_COOKIE_HTTPONLY", True))
    samesite = config.get("SESSION_COOKIE_SAMESITE")
    if session.accessed:
        _vary(response, "Cookie")
    if not session:
        if session.modified:
            response.delete_cookie(name, domain=domain, path=path, secure=secure, samesite=samesite, httponly=httponly)
            _vary(response, "Cookie")
        return
    if not (session.modified or (session.permanent and config.get("SESSION_REFRESH_EACH_REQUEST", True))):
        return
    if _session_signer(config) is None:
        raise RuntimeError("The session can't be kept without a SECRET_KEY")
    expires = datetime.now(timezone.utc) + session_lifetime(config) if session.permanent else None
    response.set_cookie(name, dump_session(config, session), expires=expires, httponly=httponly, domain=domain, path=path,
                        secure=secure, samesite=samesite)
    _vary(response, "Cookie")


def _vary(response, value):
    found = [v.strip() for v in (response.headers.get("Vary") or "").split(",") if v.strip()]
    if value.lower() not in {v.lower() for v in found}:
        response.headers["Vary"] = ", ".join(found + [value])


# --- signing in ---------------------------------------------------------------------------------------------------------

REMEMBER_COOKIE = "remember_token"
REMEMBER_FOR = timedelta(days=365)


class AnonymousUser:
    """Nobody signed in."""

    is_authenticated = False
    is_active = False
    is_anonymous = True

    def get_id(self):
        return None


class Redirect(Exception):
    """Stop here and send the browser on: `raise Redirect(url_for("auth.login"))`, from a dependency or a helper."""

    def __init__(self, url, code=302):
        super().__init__(url)
        self.url, self.code = url, code


class Answer(Exception):
    """Stop here and answer with this response (the old `abort(response)`): `raise Answer(json_response({...}, 404))`."""

    def __init__(self, response):
        super().__init__(response.status_code)
        self.response = response


class SignInRequired(Redirect):
    """Nobody is signed in on a page that needs someone (`signed_in`): to the sign-in page, and back here after."""


def _remember_digest(config, payload):
    key = config["SECRET_KEY"]
    key = key.encode("latin1") if isinstance(key, str) else key
    return hmac.new(key, payload.encode("utf-8"), hashlib.sha512).hexdigest()


def remember_value(config, user_id):
    """The "keep me signed in" cookie's value: the person's id, signed."""
    payload = str(user_id)
    return f"{payload}|{_remember_digest(config, payload)}"


def remembered(config, cookie):
    """The id a "keep me signed in" cookie holds, or None when it isn't one this platform signed."""
    try:
        payload, digest = cookie.rsplit("|", 1)
    except ValueError:
        return None
    return payload if hmac.compare_digest(_remember_digest(config, payload), digest) else None


def _identifier(request):
    """Where the session is used from (its address and browser), so a session carried elsewhere isn't fresh."""
    address = request.headers.get("X-Forwarded-For", request.client.host if request.client else None)
    if address is not None:
        address = address.encode("utf-8").split(b",")[0].strip()
    agent = request.headers.get("User-Agent")
    if agent is not None:
        agent = agent.encode("utf-8")
    return hashlib.sha512(f"{address}|{agent}".encode("utf8")).hexdigest()


def _user(user_id):
    from ..extensions import db
    from ..models import User
    try:
        return db.session.get(User, int(user_id))
    except (TypeError, ValueError):
        return None


def current_user(request):
    """Who the request is for: the signed-in person, or an AnonymousUser (`is_authenticated` False). Looked up once."""
    found = getattr(request.state, "_login_user", None)
    if found is None:
        found = _load_user(request)
        request.state._login_user = found
    return found


def _load_user(request):
    config, session = settings(), request.state.session
    if session and _identifier(request) != session.get("_id") and session.get("_fresh") is not False:
        session["_fresh"] = False  # the session came from elsewhere: still signed in, but not freshly
    user = None
    user_id = session.get("_user_id")
    if user_id is not None:
        user = _user(user_id)
    if user is None:
        name = config.get("REMEMBER_COOKIE_NAME", REMEMBER_COOKIE)
        if name in request.cookies and session.get("_remember") != "clear":
            user_id = remembered(config, request.cookies[name])
            if user_id is not None:
                session["_user_id"] = user_id
                session["_fresh"] = False
                user = _user(user_id)
    return user if user is not None else AnonymousUser()


def signed_in(request: Request):
    """A dependency: the signed-in person. Anyone else is sent to the sign-in page, and comes back here after."""
    user = current_user(request)
    if user.is_authenticated or request.method == "OPTIONS" or settings().get("LOGIN_DISABLED"):
        return user
    raise SignInRequired(_login_url(url_for("auth.login"), str(request.url)))


def _login_url(base, current):
    """`/login?next=/w/1/app/`: the sign-in page, and where to go after (this address, without its host)."""
    parts = urlsplit(base)
    query = parse_qs(parts.query, keep_blank_values=True)
    here = urlsplit(current)
    query["next"] = urlunsplit(("", "", here.path, here.query, "")) if (not parts.scheme or parts.scheme == here.scheme) and \
        (not parts.netloc or parts.netloc == here.netloc) else current
    return urlunsplit(parts._replace(query=urlencode(query, doseq=True)))


def login_user(request, user, remember=False, duration=None, force=False, fresh=True):
    """Sign this person in for the rest of the session (and, with `remember`, the browser keeps them signed in)."""
    if not force and not user.is_active:
        return False
    session = request.state.session
    session["_user_id"] = user.get_id()
    session["_fresh"] = fresh
    session["_id"] = _identifier(request)
    if remember:
        session["_remember"] = "set"
        if duration is not None:
            session["_remember_seconds"] = int(duration.total_seconds())
    request.state._login_user = user
    return True


def logout_user(request):
    session = request.state.session
    for key in ("_user_id", "_fresh", "_id"):
        if key in session:
            session.pop(key)
    if settings().get("REMEMBER_COOKIE_NAME", REMEMBER_COOKIE) in request.cookies:
        session["_remember"] = "clear"
        if "_remember_seconds" in session:
            session.pop("_remember_seconds")
    request.state._login_user = AnonymousUser()
    return True


def _remember_cookie(config, session, response):
    """The "keep me signed in" cookie, set or taken away as signing in or out asked."""
    if "_remember" not in session and config.get("REMEMBER_COOKIE_REFRESH_EACH_REQUEST"):
        if "_user_id" in session:
            session["_remember"] = "set"
    if "_remember" not in session:
        return
    operation = session.pop("_remember", None)
    name = config.get("REMEMBER_COOKIE_NAME", REMEMBER_COOKIE)
    domain = config.get("REMEMBER_COOKIE_DOMAIN")
    path = config.get("REMEMBER_COOKIE_PATH", "/")
    if operation == "set" and "_user_id" in session:
        duration = (timedelta(seconds=session["_remember_seconds"]) if "_remember_seconds" in session
                    else config.get("REMEMBER_COOKIE_DURATION", REMEMBER_FOR))
        if isinstance(duration, int):
            duration = timedelta(seconds=duration)
        response.set_cookie(name, remember_value(config, session["_user_id"]), expires=datetime.now(timezone.utc) + duration,
                            domain=domain, path=path, secure=config.get("REMEMBER_COOKIE_SECURE", False),
                            httponly=config.get("REMEMBER_COOKIE_HTTPONLY", True), samesite=config.get("REMEMBER_COOKIE_SAMESITE"))
    elif operation == "clear":
        response.delete_cookie(name, domain=domain, path=path, samesite=None)


# --- CSRF: the token every form posts back ------------------------------------------------------------------------------

CSRF_SALT = "wtf-csrf-token"
UNSAFE_METHODS = ("POST", "PUT", "PATCH", "DELETE")


def csrf_token(request):
    """This session's token, for a form's hidden `csrf_token` field (made the first time it's asked for)."""
    config = settings()
    field = config.get("WTF_CSRF_FIELD_NAME", "csrf_token")
    token = getattr(request.state, field, None)
    if token is None:
        session = request.state.session
        signer = URLSafeTimedSerializer(config.get("WTF_CSRF_SECRET_KEY") or config.get("SECRET_KEY"), salt=CSRF_SALT)
        if field not in session:
            session[field] = hashlib.sha1(os.urandom(64)).hexdigest()
        try:
            token = signer.dumps(session[field])
        except TypeError:
            session[field] = hashlib.sha1(os.urandom(64)).hexdigest()
            token = signer.dumps(session[field])
        setattr(request.state, field, token)
    return token


async def check_csrf(request: Request):
    """A dependency for a router whose forms people post from their browser: a POST, PUT, PATCH or DELETE without this
    session's token (the `csrf_token` field, or an X-CSRFToken header) is refused with a 400. A router called by other
    systems (the API, webhooks, a public form) simply doesn't use it. WTF_CSRF_ENABLED=False turns it off (the tests)."""
    config = settings()
    if not config.get("WTF_CSRF_ENABLED", True) or not config.get("WTF_CSRF_CHECK_DEFAULT", True):
        return
    if request.method not in set(config.get("WTF_CSRF_METHODS") or UNSAFE_METHODS):
        return
    field = config.get("WTF_CSRF_FIELD_NAME", "csrf_token")
    form = await form_of(request)
    token = form.get(field) or next((form[k] for k in form if k.endswith(field) and form[k]), None)  # "<prefix>-csrf_token"
    for header in config.get("WTF_CSRF_HEADERS", ["X-CSRFToken", "X-CSRF-Token"]):
        token = token or request.headers.get(header)
    problem = _csrf_problem(config, request.state.session, field, token)
    if problem:
        _log.info(problem)
        raise HTTPException(400, problem)
    if request.url.scheme in ("https", "wss") and config.get("WTF_CSRF_SSL_STRICT", True):
        referrer = request.headers.get("Referer")
        if not referrer:
            raise HTTPException(400, "The referrer header is missing.")
        here = urlparse(f"https://{request.headers.get('Host') or request.url.netloc}/")
        there = urlparse(referrer)
        if (there.scheme, there.hostname, there.port) != (here.scheme, here.hostname, here.port):
            raise HTTPException(400, "The referrer does not match the host.")
    request.state.csrf_valid = True


def _csrf_problem(config, session, field, token):
    if not token:
        return "The CSRF token is missing."
    if field not in session:
        return "The CSRF session token is missing."
    signer = URLSafeTimedSerializer(config.get("WTF_CSRF_SECRET_KEY") or config.get("SECRET_KEY"), salt=CSRF_SALT)
    try:
        sent = signer.loads(token, max_age=config.get("WTF_CSRF_TIME_LIMIT", 3600))
    except SignatureExpired:
        return "The CSRF token has expired."
    except BadData:
        return "The CSRF token is invalid."
    if not hmac.compare_digest(session[field], sent):
        return "The CSRF tokens do not match."
    return None


# --- messages for the next page -----------------------------------------------------------------------------------------

def flash(request, message, category="message"):
    """A message shown on the next page the person sees ("Saved.", "That code isn't right.")."""
    session = request.state.session
    flashes = session.get("_flashes", [])
    flashes.append((category, message))
    session["_flashes"] = flashes


def flashed(request, with_categories=False, category_filter=()):
    """The messages waiting for this page (taken from the session: each is shown once)."""
    flashes = getattr(request.state, "_flashes", None)
    if flashes is None:
        session = request.state.session
        flashes = session.pop("_flashes") if "_flashes" in session else []
        request.state._flashes = flashes
    if category_filter:
        flashes = [f for f in flashes if f[0] in category_filter]
    if not with_categories:
        return [f[1] for f in flashes]
    return [tuple(f) for f in flashes]


# --- addresses ----------------------------------------------------------------------------------------------------------

_URL_SAFE = "!$&'()*+,/:;=@"


class BuildError(LookupError):
    def __init__(self, endpoint, values, why):
        super().__init__(f"Could not build url for endpoint {endpoint!r} with values {sorted(values)}: {why}.")
        self.endpoint, self.values = endpoint, values


def url_for(endpoint, *, _anchor=None, _external=False, _method=None, **values):
    """The address of a page by its name: `url_for("app.record", wid=1, table="orders", rid=7)`. Values the address
    doesn't name go in its query string; `_external` puts BASE_URL in front; `_anchor` adds `#...`. A page not converted
    yet is found in the old layer's addresses (transition)."""
    platform = core_context.app()
    if platform is None:
        raise RuntimeError("Working outside the platform's context: run inside app.app_context()")
    path = path_for(platform, endpoint, values, _method)
    if _external:
        path = f"{platform.config['BASE_URL'].rstrip('/')}{path}"
    if _anchor is not None:
        path = f"{path}#{quote(str(_anchor), safe=_URL_SAFE + '%#?')}"
    return path


def path_for(platform, endpoint, values, method=None):
    """The path (and query) of a page by its name, native first, then the old layer's (transition)."""
    routes = (platform.extensions.get("pages") or {}).get(endpoint)
    if routes:
        return _build(endpoint, routes, values)
    return platform.url_map.build(endpoint, values, method=method)  # transition: the pages app/webkit still serves


def _build(endpoint, routes, values):
    given = {k: v for k, v in values.items() if v is not None}
    for route in sorted(routes, key=lambda r: -len(r.param_convertors)):
        names = set(route.param_convertors)
        if names <= set(given):
            path = route.path_format
            for name, convertor in route.param_convertors.items():
                value = given[name]
                text = str(int(value)) if isinstance(convertor, IntegerConvertor) else quote(str(value), safe=_URL_SAFE)
                path = path.replace("{" + name + "}", text)
            query = _query({k: v for k, v in given.items() if k not in names})
            return path + (f"?{query}" if query else "")
    missing = sorted(set(routes[0].param_convertors) - set(given))
    raise BuildError(endpoint, values, f"it needs {', '.join(missing)}" if missing else "no route fits")


def _query(values):
    items = []
    for key, value in values.items():
        if isinstance(value, (list, tuple, set)):
            items.extend((key, v) for v in value if v is not None)
        elif value is not None:
            items.append((key, value))
    return urlencode(items, safe="!$'()*,/:;?@")


# --- answers ------------------------------------------------------------------------------------------------------------

_template_globals, _context_processors = {}, []


def template_global(fn):
    """A function every template can call by its name (`@template_global` on it; it's given to the templates when the
    front is made, `install`)."""
    _template_globals[fn.__name__] = fn
    return fn


def context_processor(fn):
    """`@context_processor`: fn() returns what every page's template gets besides (the workspace's menu, say)."""
    _context_processors.append(fn)
    return fn


def render(request, name, /, status_code=200, **context):
    """A page from app/templates, with what every page gets (the request, the session, the person, the CSRF token, the
    messages waiting, the workspace and its menu from the context processors) and what's given."""
    platform = core_context.app()
    template = platform.jinja_env.get_or_select_template(name)
    values = {}
    for processor in platform.template_context_processors:  # the modules' own (the workspace, its menu, the platform's name)
        values.update(processor())
    values.update(request=request, session=request.state.session, g=request.state, current_user=current_user(request),
                  config=platform.config, csrf_token=functools.partial(csrf_token, request),
                  get_flashed_messages=functools.partial(flashed, request), url_for=url_for)
    values.update(context)
    return HTMLResponse(template.render(values), status_code=status_code)


def redirect(location, code=302):
    """Send the browser on to `location` (a 302 unless said)."""
    location = str(location)
    shown = escape(location)
    body = ("<!doctype html>\n<html lang=en>\n<title>Redirecting...</title>\n<h1>Redirecting...</h1>\n"
            "<p>You should be redirected automatically to the target URL: "
            f'<a href="{shown}">{shown}</a>. If not, click the link.\n')
    return Response(body, status_code=code, media_type="text/html",
                    headers={"Location": quote(location, safe="/:?#[]@!$&'()*+,;=%~")})


def _json_default(o):
    """What JSON can't say on its own: dates as HTTP dates, decimals and ids as text."""
    if isinstance(o, date):
        return http_date(o)
    if isinstance(o, (decimal.Decimal, uuid.UUID)):
        return str(o)
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        return dataclasses.asdict(o)
    if hasattr(o, "__html__"):
        return str(o.__html__())
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")


def dumps(obj, **kwargs):
    """JSON as the platform has always written it (sorted keys, ASCII, dates and decimals as text)."""
    kwargs.setdefault("default", _json_default)
    kwargs.setdefault("ensure_ascii", True)
    kwargs.setdefault("sort_keys", True)
    return json.dumps(obj, **kwargs)


def json_response(data, status_code=200, headers=None):
    """`jsonify` as it was: the data as compact JSON, with a closing newline."""
    return Response(dumps(data, separators=(",", ":")) + "\n", status_code=status_code, media_type="application/json", headers=headers)


def http_date(when):
    """A date as HTTP writes it: `Wed, 05 Nov 2026 08:41:00 GMT`. A plain time is UTC."""
    if isinstance(when, (int, float)):
        when = datetime.fromtimestamp(when, timezone.utc)
    elif not isinstance(when, datetime):  # a date
        when = datetime(when.year, when.month, when.day, tzinfo=timezone.utc)
    elif when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return format_datetime(when.astimezone(timezone.utc), usegmt=True)


def send_file(request, path_or_file, mimetype=None, as_attachment=False, download_name=None, conditional=True, etag=True,
              last_modified=None, max_age=None):
    """A file as the answer: one on disk (sent in pieces, a part of it when the browser asks) or bytes in memory. The
    browser checks it again each time (no-cache), and gets a "not changed" when it already has it."""
    path = size = mtime = None
    if isinstance(path_or_file, (str, os.PathLike)):
        from pathlib import Path
        path = Path(path_or_file)
        if not path.is_absolute():
            path = Path(core_context.app().root_path) / path
        stat = path.stat()
        size, mtime = stat.st_size, stat.st_mtime
    if download_name is None and path is not None:
        download_name = path.name
    if mimetype is None:
        if download_name is None:
            raise TypeError("Unable to detect the MIME type because a file name is not available. Either set 'download_name', "
                            "pass a path instead of a file, or set 'mimetype'.")
        mimetype = mimetypes.guess_type(download_name)[0] or "application/octet-stream"
    headers = {"Cache-Control": "no-cache"}
    if download_name is not None:
        headers["Content-Disposition"] = _disposition("attachment" if as_attachment else "inline", download_name)
    elif as_attachment:
        raise TypeError("No name provided for attachment.")
    if last_modified is not None or mtime is not None:
        headers["Last-Modified"] = http_date(last_modified if last_modified is not None else mtime)
    if max_age is not None:
        headers["Cache-Control"] = f"public, max-age={int(max_age)}" if max_age > 0 else f"no-cache, max-age={int(max_age)}"
    tag = etag if isinstance(etag, str) else (f"{mtime}-{size}-{zlib.adler32(str(path).encode()) & 0xFFFFFFFF}"
                                              if etag and path is not None else None)
    if tag:
        headers["ETag"] = f'"{tag}"'
    if conditional:
        headers["Accept-Ranges"] = "bytes"
        if request.method in ("GET", "HEAD") and _not_modified(request, tag, headers.get("Last-Modified")):
            return Response(status_code=304, headers=headers, media_type=mimetype)
    if path is not None:
        return FileResponse(path, media_type=mimetype, headers=headers, stat_result=stat)
    if isinstance(path_or_file, io.TextIOBase):
        raise ValueError("Files must be opened in binary mode or use BytesIO.")
    data = path_or_file.getvalue() if isinstance(path_or_file, io.BytesIO) else path_or_file.read()
    return Response(data, media_type=mimetype, headers=headers)


def _not_modified(request, tag, modified):
    wanted = request.headers.get("If-None-Match")
    if wanted:
        if not tag:
            return False
        tags = {t.strip().removeprefix("W/").strip('"') for t in wanted.split(",")}
        return "*" in tags or tag in tags
    since = request.headers.get("If-Modified-Since")
    if since and modified:
        try:
            return parsedate_to_datetime(modified) <= parsedate_to_datetime(since)
        except (TypeError, ValueError):
            return False
    return False


_TOKEN = set("!#$%&'*+-.^_`|~0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")


def _disposition(kind, name):
    """`attachment; filename=orders.csv`, with the name also in UTF-8 when it isn't plain ASCII."""
    def quoted(value):
        return value if value and set(value) <= _TOKEN else '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    try:
        name.encode("ascii")
    except UnicodeEncodeError:
        simple = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
        return f"{kind}; filename={quoted(simple)}; filename*=UTF-8''{quote(name, safe='!#$&+-.^_`|~')}"
    return f"{kind}; filename={quoted(name)}"


# --- what the request carries: its form, its files, its JSON ------------------------------------------------------------

class MissingValue(HTTPException, KeyError):
    """A form value the view needed wasn't sent (`form["x"]`): a 400 to the browser, and still a KeyError."""

    def __init__(self, key):
        HTTPException.__init__(self, 400)
        KeyError.__init__(self, key)

    def __str__(self):
        return HTTPException.__str__(self)


class Form(dict):
    """A form's values by name, where a name may come more than once (ticked boxes, several files). As a dict it holds
    each name's first value; `getlist` gives them all; `get(name, default, type=int)` converts (the default when it
    can't). A name that wasn't sent is a 400 when read as `form[name]`. Its uploaded files are in `form.files`."""

    def __init__(self, items=()):
        super().__init__()
        self._lists = {}
        self.files = None
        for key, value in items:
            self.add(key, value)

    def __getitem__(self, key):
        try:
            return dict.__getitem__(self, key)
        except KeyError:
            raise MissingValue(key) from None

    def add(self, key, value):
        if key not in self._lists:
            dict.__setitem__(self, key, value)
            self._lists[key] = []
        self._lists[key].append(value)

    def get(self, key, default=None, type=None):  # noqa: A002 - the name every view uses
        try:
            value = dict.__getitem__(self, key)
        except KeyError:
            return default
        if type is not None:
            try:
                return type(value)
            except (ValueError, TypeError):
                return default
        return value

    def getlist(self, key, type=None):  # noqa: A002
        values = list(self._lists.get(key, []))
        if type is None:
            return values
        out = []
        for value in values:
            try:
                out.append(type(value))
            except (ValueError, TypeError):
                pass
        return out

    def items(self, multi=False):
        if multi:
            return [(k, v) for k, values in self._lists.items() for v in values]
        return list(dict.items(self))

    def to_dict(self, flat=True):
        return dict(dict.items(self)) if flat else {k: list(v) for k, v in self._lists.items()}


class Upload:
    """An uploaded file: its name, its type, and its contents to read (or save) once, without waiting on anything."""

    def __init__(self, upload):
        self.stream = upload.file
        self.filename = upload.filename
        self.content_type = upload.content_type
        self.headers = upload.headers

    @property
    def mimetype(self):
        return (self.content_type or "").split(";")[0].strip().lower()

    def read(self, *args):
        return self.stream.read(*args)

    def save(self, dst, buffer_size=16384):
        import shutil
        if isinstance(dst, (str, bytes)) or hasattr(dst, "__fspath__"):
            with open(dst, "wb") as out:
                shutil.copyfileobj(self.stream, out, buffer_size)
        else:
            shutil.copyfileobj(self.stream, dst, buffer_size)

    def close(self):
        with contextlib.suppress(Exception):
            self.stream.close()

    def __bool__(self):
        return bool(self.filename)


FORM_TYPES = ("application/x-www-form-urlencoded", "multipart/form-data")


async def body_of(request: Request) -> bytes:
    """A dependency: the request's body as it came (a 413 when it's larger than MAX_CONTENT_LENGTH)."""
    found = getattr(request.state, "_body", None)
    if found is not None:
        return found
    limit = settings().get("MAX_CONTENT_LENGTH")
    try:
        declared = int(request.headers.get("content-length", ""))
    except ValueError:
        declared = None
    if limit is not None and declared is not None and declared > limit:
        raise HTTPException(413)
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if limit is not None and size > limit:
            raise HTTPException(413)
        chunks.append(chunk)
    request.state._body = body = b"".join(chunks)
    return body


async def form_of(request: Request) -> Form:
    """A dependency: the form posted (`form.get("email")`), with its uploaded files in `form.files`. Empty when the
    request carries no form. Values larger than MAX_FORM_MEMORY_SIZE are a 413; a form that can't be read, a 400."""
    found = getattr(request.state, "_form", None)
    if found is not None:
        return found
    mimetype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    form = await _parse_form(request, await body_of(request), mimetype) if mimetype in FORM_TYPES else Form()
    if form.files is None:
        form.files = Form()
    request.state._form = form
    return form


async def json_of(request: Request):
    """A dependency: the body as JSON, or None when it isn't JSON (or can't be read as it)."""
    mimetype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if not (mimetype == "application/json" or (mimetype.startswith("application/") and mimetype.endswith("+json"))):
        return None
    try:
        return json.loads(await body_of(request))
    except ValueError:
        return None


async def _parse_form(request, body, mimetype):
    from starlette.datastructures import UploadFile
    from starlette.formparsers import FormParser, MultiPartException, MultiPartParser
    config = settings()
    most = config.get("MAX_FORM_MEMORY_SIZE") or 10 ** 12
    parts = config.get("MAX_FORM_PARTS") or 1000

    async def stream():
        yield body
        yield b""

    if mimetype == "multipart/form-data":
        parser = MultiPartParser(request.headers, stream(), max_files=parts, max_fields=parts, max_part_size=most)
    else:
        if len(body) > most:
            raise HTTPException(413)
        parser = FormParser(request.headers, stream(), max_fields=parts, max_part_size=most)
    try:
        data = await parser.parse()
    except MultiPartException as exc:
        if "maximum size" in exc.message:
            raise HTTPException(413) from None
        raise HTTPException(400, exc.message) from None
    except Exception as exc:  # noqa: BLE001 - a body that isn't the form it says it is
        raise HTTPException(400, f"The form couldn't be read: {exc}") from None
    form, files = Form(), Form()
    for key, value in data.multi_items():
        if isinstance(value, UploadFile):
            value.file.seek(0)
            files.add(key, Upload(value))
        else:
            form.add(key, value)
    form.files = files
    return form


# --- errors -------------------------------------------------------------------------------------------------------------

DESCRIPTIONS = {
    400: "The browser (or proxy) sent a request that this server could not understand.",
    401: "The server could not verify that you are authorized to access the URL requested. You either supplied the wrong "
         "credentials (e.g. a bad password), or your browser doesn't understand how to supply the credentials required.",
    403: "You don't have the permission to access the requested resource. It is either read-protected or not readable by "
         "the server.",
    404: "The requested URL was not found on the server. If you entered the URL manually please check your spelling and try "
         "again.",
    405: "The method is not allowed for the requested URL.",
    409: "A conflict happened while processing the request. The resource might have been modified while the request was "
         "being processed.",
    410: "The requested URL is no longer available on this server and there is no forwarding address. If you followed a link "
         "from a foreign page, please contact the author of this page.",
    413: "The data value transmitted exceeds the capacity limit.",
    415: "The server does not support the media type transmitted in the request.",
    422: "The request was well-formed but was unable to be followed due to semantic errors.",
    429: "This user has exceeded an allotted request count. Try again later.",
    500: "The server encountered an internal error and was unable to complete your request. Either the server is overloaded "
         "or there is an error in the application.",
    501: "The server does not support the action requested by the browser.",
    503: "The server is temporarily unable to service your request due to maintenance downtime or capacity problems. Please "
         "try again later.",
}


def _phrase(code):
    try:
        return HTTPStatus(code).phrase
    except ValueError:
        return "Unknown Error"


def error_words(code, description=None):
    """The platform's own page for an error it has one for (403, 404, 413, 500): its code, title and text for
    templates/error.html; None for any other code (a plain page is shown)."""
    if code == 403:
        said = description or ""
        why = said if said and not said.startswith("You don't have the permission") else "Ask the person who runs your app if you need it."
        return {"code": 403, "title": "This isn't open to you", "text": why}
    if code == 404:
        return {"code": 404, "title": "We can't find this page", "text": "It may have been moved or deleted, or it may not be open to you."}
    if code == 413:
        return {"code": 413, "title": "That file is too large", "text": f"Files can be up to {settings()['MAX_UPLOAD_MB']} MB."}
    if code == 500:
        return {"code": 500, "title": "Sorry, something went wrong",
                "text": "It's not your fault, and nothing you entered was lost. Go back and try again."}
    return None


def plain_error(code, description=None, headers=None):
    """The plain page for an error the platform has no page of its own for (a 400 with what was wrong, a 405, a 410)."""
    name = _phrase(code)
    text = escape(description or DESCRIPTIONS.get(code, "")).replace("\n", Markup("<br>"))
    body = f"<!doctype html>\n<html lang=en>\n<title>{code} {escape(name)}</title>\n<h1>{escape(name)}</h1>\n<p>{text}</p>\n"
    return Response(body, status_code=code, media_type="text/html", headers=headers)


def said(exc):
    """What an HTTPException says beyond its status's name ("why" in HTTPException(403, "why")), or None."""
    detail = exc.detail if isinstance(exc.detail, str) else None
    return detail if detail and detail != _phrase(exc.status_code) else None


def error_page(request, code, description=None, headers=None):
    """An error as the person sees it: the platform's page for it (templates/error.html), or the plain one."""
    words = error_words(code, description)
    if words is None:
        return plain_error(code, description, headers)
    response = render(request, "error.html", status_code=code, **words)
    for key, value in (headers or {}).items():
        response.headers[key] = value
    return response


def _answer(request, exc):
    """An exception raised on the way, as the response: a redirect, an error's page, or (a bug) the 500 page. Under
    TESTING (or DEBUG) a bug is raised to the caller instead, so the test shows where it went wrong."""
    if isinstance(exc, Redirect):
        return redirect(exc.url, exc.code)
    if isinstance(exc, Answer):
        return exc.response
    if isinstance(exc, StarletteHTTPException) and request.url.path.startswith("/api/"):  # an API's errors stay JSON
        from starlette.responses import JSONResponse
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
    if isinstance(exc, StarletteHTTPException):
        return error_page(request, exc.status_code, said(exc), exc.headers)
    from fastapi.exceptions import RequestValidationError
    if isinstance(exc, RequestValidationError):
        return plain_error(400)
    config = settings()
    propagate = config.get("PROPAGATE_EXCEPTIONS")
    if propagate is None:
        propagate = config.get("TESTING") or config.get("DEBUG")
    if propagate:
        raise exc
    _log.error("Exception on %s [%s]", request.url.path, request.method, exc_info=(type(exc), exc, exc.__traceback__))
    return error_page(request, 500)


# --- serving a page -----------------------------------------------------------------------------------------------------

class PageRoute(APIRoute):
    """A page's route: `APIRouter(route_class=PageRoute)`. Its requests are served in the platform's context, with the
    session, and finished the way every page is (see the module's notes). A GET page answers HEAD too; pages aren't
    part of the API's schema."""

    def __init__(self, path, endpoint, *, methods=None, include_in_schema=False, **kwargs):
        methods = {m.upper() for m in (methods or ["GET"])}
        if "GET" in methods:
            methods.add("HEAD")
        super().__init__(path, endpoint, methods=methods, include_in_schema=False, **kwargs)

    def get_route_handler(self):
        handler = super().get_route_handler()
        rule = _rule_text(self.path_format, self.param_convertors)

        async def serve(request: Request):
            began = time.perf_counter()
            async with serving(request):
                try:
                    response = await handler(request)
                except Exception as exc:  # noqa: BLE001 - every error is answered with its page (or raised under TESTING)
                    response = await run_in_threadpool(_answer, request, exc)
                await run_in_threadpool(_finish, request, response, rule, began)
                return response

        return serve


def _rule_text(path_format, convertors):
    """`/w/{wid}/app` as Settings › Health groups screens: `/w/<int:wid>/app`."""
    for name, convertor in convertors.items():
        kind = "int:" if isinstance(convertor, IntegerConvertor) else "path:" if isinstance(convertor, PathConvertor) else ""
        path_format = path_format.replace("{" + name + "}", f"<{kind}{name}>")
    return path_format


@contextlib.asynccontextmanager
async def serving(request):
    """The platform's context for one request, as the old layer's AppContext.push entered it: the app, its settings,
    its database (a session of the request's own, given back at the end), who is acting, the request itself, and the
    session read from its cookie.

    A request made from a test or a try-out (app.test_client()) carries the caller's context; it's entered first, and
    when the caller is in this app's context already the request shares its database session (so what the request
    saved is what the test reads next), and leaves it open, told it's no longer one workspace's."""
    platform = request.app.state.platform
    with ExitStack() as stack:
        caller = request.scope.get("keel.context")
        if caller is not None:
            for var, value in caller.items():
                stack.callback(var.reset, var.set(value))
        shared = core_context.app() is platform
        stack.enter_context(core_context.entered(platform))
        stack.enter_context(using_settings(platform.config))
        database = platform.extensions.get("sqlalchemy")
        if not shared and database is not None and platform.engine is not None:
            stack.enter_context(database.using(platform.engine))
        stack.enter_context(actor.reading(functools.partial(_who, request)))
        stack.callback(_request.reset, _request.set(request))
        if getattr(request.state, "session", None) is None:
            request.state.session = load_session(platform.config, request.cookies.get(session_cookie_name(platform.config)))
        try:
            yield
        finally:
            await run_in_threadpool(_leave, request, database, shared)


def _who(request, name):
    """Who is acting, as the core asks (core.actor): the signed-in person, their address, and the workspace the page has
    looked up (request.state.ws, or its engine's)."""
    if name == "user":
        try:
            who = current_user(request)
        except Exception:  # noqa: BLE001 - nobody can be signed in here
            return None
        return who if who.is_authenticated else None
    if name == "ip":
        address = request.headers.get("X-Forwarded-For", request.client.host if request.client else "")
        return address.split(",")[0].strip()[:64] or None
    if name == "workspace_id":
        state = request.state
        ws = getattr(state, "ws", None) or getattr(getattr(state, "engine", None), "ws", None)
        return actor._identity(ws) if ws is not None else None
    return None


def _finish(request, response, rule, began):
    """After the view: "keep me signed in", the security headers, the session cookie, and the time it took."""
    from .. import health
    config = settings()
    session = request.state.session
    _remember_cookie(config, session, response)
    _headed(response, request.url.path)
    save_session(config, session, response)
    health.record(rule, response.status_code, began)


def _leave(request, database, shared):
    """The request's uploads closed, and its database session given back (or, the caller's, told it's no longer one
    workspace's)."""
    form = getattr(request.state, "_form", None)
    for _, upload in (form.files.items(multi=True) if form is not None and form.files else []):
        upload.close()
    if database is None:
        return
    if shared:
        from ..core import tenancy
        tenancy.forget(database.session)
    else:
        database.session.remove()


def _mimetype(response):
    return (response.headers.get("content-type") or "").split(";")[0].strip().lower()


# --- wiring the pages into the FastAPI front ----------------------------------------------------------------------------

def install(api, platform, routers):
    """The page routers, served by the FastAPI front `api` ahead of the old layer's pages: their names for url_for,
    their errors as pages (JSON under /api/), and the template globals that need the request (the CSRF token, the
    messages waiting) read from whichever request is being served."""
    pages, ordered = {}, []
    for router in routers:
        api.include_router(router)
        for route in router.routes:
            if isinstance(route, PageRoute):
                pages.setdefault(route.name, []).append(route)
                ordered.append(route)
    platform.extensions["pages"] = pages  # by name, for url_for
    platform.extensions["page_routes"] = ordered  # in the order they're tried
    api.add_exception_handler(StarletteHTTPException, _http_error)
    api.add_exception_handler(Redirect, _redirect_error)
    api.add_exception_handler(Answer, _answer_error)
    platform.jinja_env.globals.update(url_for=url_for, csrf_token=_csrf_here, get_flashed_messages=_flashed_here, full_path=full_path,
                                      **_template_globals)
    platform.template_context_processors.extend(p for p in _context_processors if p not in platform.template_context_processors)
    api.router.routes.append(OldPages(platform, ordered))


def _csrf_here():
    return csrf_token(current_request())


def _flashed_here(with_categories=False, category_filter=()):
    return flashed(current_request(), with_categories, category_filter)


async def _http_error(request, exc):
    """An HTTP error raised outside a page's own handling (a page asked with a method it doesn't take): JSON under
    /api/, as FastAPI gives it; the page for it everywhere else."""
    if request.url.path.startswith("/api/"):
        from fastapi.exception_handlers import http_exception_handler
        return await http_exception_handler(request, exc)
    if error_words(exc.status_code) is None:
        return _headed(plain_error(exc.status_code, said(exc), exc.headers), request.url.path)
    async with serving(request):
        return _headed(await run_in_threadpool(error_page, request, exc.status_code, said(exc), exc.headers), request.url.path)


async def _redirect_error(request, exc):
    return redirect(exc.url, exc.code)


async def _answer_error(request, exc):
    return exc.response


class OldPages(Mount):
    """The pages the old layer (app/webkit) still serves, mounted behind the native ones (transition). Where the old
    layer has nothing at an address but a native page does, asked with a method the page doesn't take, it steps aside
    (so it's the native page's 405, not a 404); and an address a native page has with a closing slash is sent on to it
    (a 308), as the old layer always did."""

    def __init__(self, platform, pages):
        super().__init__("/", app=platform.serve)
        self.platform, self.pages = platform, pages

    def matches(self, scope):
        if scope["type"] == "http" and not self._old_layer_has(scope["path"], scope["method"]):
            probe = {k: v for k, v in scope.items() if k != "fastapi"}
            if any(route.matches(probe)[0] == Match.PARTIAL for route in self.pages):
                return Match.NONE, {}
            path = scope["path"]
            if path != "/" and not path.endswith("/"):
                slashed = {**probe, "path": path + "/"}
                if any(route.matches(slashed)[0] != Match.NONE for route in self.pages):
                    return Match.FULL, {"keel.add_slash": True}
        return super().matches(scope)

    def _old_layer_has(self, path, method):
        from ..webkit.exceptions import NotFound
        try:
            self.platform.url_map.match(path, method)
        except NotFound:
            return False
        except Exception:  # noqa: BLE001 - its own 405, or its own closing slash: the old layer answers
            return True
        return True

    async def handle(self, scope, receive, send):
        if scope.pop("keel.add_slash", False):
            query = scope.get("query_string", b"").decode("latin-1")
            response = _headed(redirect(scope.get("root_path", "") + scope["path"] + "/" + (f"?{query}" if query else ""), 308), scope["path"])
            await response(scope, receive, send)
            return
        await super().handle(scope, receive, send)


def _headed(response, path):
    """The security headers every response carries (app/security.py), where they aren't set yet."""
    from .. import security
    for key, value in security.page_headers(path, _mimetype(response)):
        response.headers.setdefault(key, value)
    return response


def seen_natively(scope, state, session):
    """The old layer's request as a native one (transition: app/webkit's RequestContext sets it as current_request()),
    so the helpers both layers use are written once: its state is the old layer's `g`, its session the same session."""
    request = Request({**scope, "state": state})
    state.pop("_flashes", None)  # a test's `g` outlives its requests: each request takes its own messages
    request.state.session = session
    return request
