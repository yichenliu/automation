"""The platform's own small web kit, on Starlette (Keel v2, D9): what the pages not converted yet are written with.
It's being retired a module at a time for native FastAPI routes (app/web/http.py; docs/WEB.md), and shares its
session cookie, sign-in, CSRF token, messages and addresses with them meanwhile.

It gives the views the handful of things they use, under the names they have always used, with the same behaviour:
a Blueprint and its routes, `request`, `session`, `g`, `url_for`, `redirect`, `abort`, `flash`, `render_template`,
`jsonify`, `send_file`; signing in (`current_user`, `login_required`); CSRF protection; and the app itself, with its
hooks, error pages and test client. A view keeps its code; only where it imports these from changed.

    app.py        the app: settings, blueprints, hooks, contexts, and serving a request (ASGI, a worker thread each)
    routing.py    addresses: matching one to its view, building one with url_for
    wrappers.py   the request and the response
    sessions.py   the signed session cookie
    login.py      who is signed in
    csrf.py       the token every form posts back
    helpers.py    what views call
    testing.py    app.test_client()
"""
from ..core.context import current_app
from .app import App, AppContext, Blueprint, Config, RequestContext
from .csrf import CSRFError, CSRFProtect, generate_csrf
from .datastructures import FileStorage, Headers, MultiDict
from .exceptions import HTTPException, abort
from .helpers import flash, get_flashed_messages, jsonify, make_response, redirect, render_template, send_file, url_for
from .local import Proxy, _app_ctx, _request_ctx, has_app_context, has_request_context
from .login import AnonymousUser, LoginManager, current_user, login_required, login_user, logout_user
from .wrappers import Request, Response

request = Proxy(lambda: _request_ctx().request)
session = Proxy(lambda: _request_ctx().session)
g = Proxy(lambda: _app_ctx().g)

__all__ = ["App", "AppContext", "AnonymousUser", "Blueprint", "CSRFError", "CSRFProtect", "Config", "FileStorage", "HTTPException",
           "Headers", "LoginManager", "MultiDict", "Request", "RequestContext", "Response", "abort", "current_app",
           "current_user", "flash", "g", "generate_csrf", "get_flashed_messages", "has_app_context", "has_request_context", "jsonify",
           "login_required", "login_user", "logout_user", "make_response", "redirect", "render_template", "request", "send_file",
           "session", "url_for"]
