"""Protection against another website posting a form here in the name of someone signed in (cross-site request
forgery): every POST, PUT, PATCH and DELETE carries a token only this site's pages know (the `csrf_token` form field,
or an X-CSRFToken header from the page's scripts), and anything without the right one is refused with a 400.

The token is the one the platform has always used: a secret kept in the session, signed with the app's secret key, so
pages open while the platform is upgraded still post. Views (or whole blueprints) that are called by other systems,
not by people's browsers (the API, webhooks), are exempt; WTF_CSRF_ENABLED=False turns it off (the tests)."""
import hashlib
import hmac
import logging
import os
from urllib.parse import urlparse

from itsdangerous import BadData, SignatureExpired, URLSafeTimedSerializer

from .exceptions import BadRequest
from .local import app_ctx, request_ctx

_log = logging.getLogger(__name__)
SALT = "wtf-csrf-token"


class CSRFError(BadRequest):
    """The token was missing or wrong: a 400 with what was wrong."""


class _Invalid(Exception):
    pass


def _config(name, default=None):
    return app_ctx.get().app.config.get(name, default)


def generate_csrf():
    """This session's token, for a form's hidden `csrf_token` field (made the first time it's asked for)."""
    g = app_ctx.get().g
    field = _config("WTF_CSRF_FIELD_NAME", "csrf_token")
    if field not in g:
        session = request_ctx.get().session
        signer = URLSafeTimedSerializer(_config("WTF_CSRF_SECRET_KEY") or _config("SECRET_KEY"), salt=SALT)
        if field not in session:
            session[field] = hashlib.sha1(os.urandom(64)).hexdigest()
        try:
            token = signer.dumps(session[field])
        except TypeError:
            session[field] = hashlib.sha1(os.urandom(64)).hexdigest()
            token = signer.dumps(session[field])
        setattr(g, field, token)
    return g.get(field)


def validate_csrf(data):
    field = _config("WTF_CSRF_FIELD_NAME", "csrf_token")
    session = request_ctx.get().session
    if not data:
        raise _Invalid("The CSRF token is missing.")
    if field not in session:
        raise _Invalid("The CSRF session token is missing.")
    signer = URLSafeTimedSerializer(_config("WTF_CSRF_SECRET_KEY") or _config("SECRET_KEY"), salt=SALT)
    try:
        token = signer.loads(data, max_age=_config("WTF_CSRF_TIME_LIMIT", 3600))
    except SignatureExpired as exc:
        raise _Invalid("The CSRF token has expired.") from exc
    except BadData as exc:
        raise _Invalid("The CSRF token is invalid.") from exc
    if not hmac.compare_digest(session[field], token):
        raise _Invalid("The CSRF tokens do not match.")


def _same_origin(one, other):
    a, b = urlparse(one), urlparse(other)
    return a.scheme == b.scheme and a.hostname == b.hostname and a.port == b.port


class CSRFProtect:
    def __init__(self):
        self._exempt_views = set()
        self._exempt_blueprints = set()

    def init_app(self, app):
        app.extensions["csrf"] = self
        app.config.setdefault("WTF_CSRF_ENABLED", True)
        app.config.setdefault("WTF_CSRF_CHECK_DEFAULT", True)
        app.config["WTF_CSRF_METHODS"] = set(app.config.get("WTF_CSRF_METHODS", ["POST", "PUT", "PATCH", "DELETE"]))
        app.config.setdefault("WTF_CSRF_FIELD_NAME", "csrf_token")
        app.config.setdefault("WTF_CSRF_HEADERS", ["X-CSRFToken", "X-CSRF-Token"])
        app.config.setdefault("WTF_CSRF_TIME_LIMIT", 3600)
        app.config.setdefault("WTF_CSRF_SSL_STRICT", True)
        app.jinja_env.globals["csrf_token"] = generate_csrf
        app.template_context_processors.append(lambda: {"csrf_token": generate_csrf})

        @app.before_request
        def csrf_protect():
            from .local import request_ctx as current
            request = current.get().request
            if not app.config["WTF_CSRF_ENABLED"] or not app.config["WTF_CSRF_CHECK_DEFAULT"]:
                return
            if request.method not in app.config["WTF_CSRF_METHODS"] or not request.endpoint:
                return
            if request.blueprint in self._exempt_blueprints:
                return
            view = app.view_functions.get(request.endpoint)
            if f"{view.__module__}.{view.__name__}" in self._exempt_views:
                return
            self.protect()

    def exempt(self, view):
        """No token needed: a blueprint, a view, or a view's "module.function"."""
        from .app import Blueprint
        if isinstance(view, Blueprint):
            self._exempt_blueprints.add(view.name)
            return view
        self._exempt_views.add(view if isinstance(view, str) else f"{view.__module__}.{view.__name__}")
        return view

    def _token(self, request):
        field = _config("WTF_CSRF_FIELD_NAME", "csrf_token")
        token = request.form.get(field)
        if token:
            return token
        for key in request.form:  # a form with a prefix names it "<prefix>-csrf_token"
            if key.endswith(field) and request.form[key]:
                return request.form[key]
        for header in _config("WTF_CSRF_HEADERS", ["X-CSRFToken", "X-CSRF-Token"]):
            token = request.headers.get(header)
            if token:
                return token
        return None

    def protect(self):
        request = request_ctx.get().request
        if request.method not in _config("WTF_CSRF_METHODS"):
            return
        try:
            validate_csrf(self._token(request))
        except _Invalid as exc:
            _log.info(exc.args[0])
            raise CSRFError(exc.args[0]) from None
        if request.is_secure and _config("WTF_CSRF_SSL_STRICT", True):
            if not request.referrer:
                raise CSRFError("The referrer header is missing.")
            if not _same_origin(request.referrer, f"https://{request.host}/"):
                raise CSRFError("The referrer does not match the host.")
        app_ctx.get().g.csrf_valid = True
