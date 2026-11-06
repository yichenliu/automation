"""The platform as a web app: its settings, its pages (gathered from blueprints), and how one request is served.

Serving a request, in order (the order the views were written for):
1. the request's body is read, and its form and files taken apart, while no thread waits;
2. in a worker thread, the platform's context is entered (its settings, its database session, who is acting), the
   session cookie is read and the address matched to a view;
3. the before-request hooks run (the first to answer stops the rest), then the view;
4. an error raised on the way becomes its page (the app's own for 403, 404, 413 and 500; a plain one otherwise);
5. the after-request hooks run (newest first), the session cookie is written if it changed, then the teardown hooks,
   and the context is left: the request's database session goes back to the pool.

Under TESTING an error no page is made for is raised to the caller (the test, or the try-out walking the screens),
instead of becoming the 500 page.
"""
import logging
import sys
from contextlib import ExitStack
from datetime import timedelta
from pathlib import Path

from ..core import actor, context as core_context, settings as core_settings
from . import local
from .exceptions import HTTPException, InternalServerError, NotFound, RequestEntityTooLarge, RequestRedirect
from .helpers import dumps, get_flashed_messages, make_response, send_file, url_for
from .routing import Map, Rule
from .sessions import SessionInterface
from .wrappers import Request

_sentinel = object()

DEFAULTS = {
    "DEBUG": False, "TESTING": False, "PROPAGATE_EXCEPTIONS": None, "SECRET_KEY": None,
    "PERMANENT_SESSION_LIFETIME": timedelta(days=31), "SERVER_NAME": None, "APPLICATION_ROOT": "/",
    "SESSION_COOKIE_NAME": "session", "SESSION_COOKIE_DOMAIN": None, "SESSION_COOKIE_PATH": None,
    "SESSION_COOKIE_HTTPONLY": True, "SESSION_COOKIE_SECURE": False, "SESSION_COOKIE_SAMESITE": None,
    "SESSION_REFRESH_EACH_REQUEST": True, "MAX_CONTENT_LENGTH": None, "MAX_FORM_MEMORY_SIZE": 500_000,
    "MAX_FORM_PARTS": 1_000, "SEND_FILE_MAX_AGE_DEFAULT": None, "PREFERRED_URL_SCHEME": "http",
    "TEMPLATES_AUTO_RELOAD": None,
}


class Config(dict):
    """The app's settings: a dict, filled from a class's UPPERCASE attributes (app.config.Config) and then overrides."""

    def from_object(self, obj):
        for key in dir(obj):
            if key.isupper():
                self[key] = getattr(obj, key)

    def from_mapping(self, mapping=None, **kwargs):
        for key, value in {**(mapping or {}), **kwargs}.items():
            if key.isupper():
                self[key] = value
        return True


class _Globals:
    """`g`: a place for what one request (or one app context) works out and shares (the workspace, the engine)."""

    def get(self, name, default=None):
        return self.__dict__.get(name, default)

    def pop(self, name, default=_sentinel):
        if default is _sentinel:
            return self.__dict__.pop(name)
        return self.__dict__.pop(name, default)

    def setdefault(self, name, default=None):
        return self.__dict__.setdefault(name, default)

    def __contains__(self, item):
        return item in self.__dict__

    def __iter__(self):
        return iter(self.__dict__)

    def __repr__(self):
        return f"<g {self.__dict__!r}>"


class Blueprint:
    """A group of pages under one name ("app", "settings"), and often one address prefix (/w/<int:wid>/app)."""

    def __init__(self, name, import_name, url_prefix=None, **_):
        self.name, self.import_name, self.url_prefix = name, import_name, url_prefix
        self.rules = []  # (rule, endpoint, view, methods)
        self.app_context_processors, self.app_template_globals, self.app_template_filters = [], {}, {}
        self.before_request_funcs, self.after_request_funcs, self.teardown_request_funcs = [], [], []
        self.error_handlers = {}

    def route(self, rule, **options):
        def decorator(view):
            self.add_url_rule(rule, options.pop("endpoint", None), view, **options)
            return view
        return decorator

    def add_url_rule(self, rule, endpoint=None, view_func=None, methods=None, **_):
        self.rules.append((rule, endpoint or view_func.__name__, view_func, methods))

    def app_context_processor(self, f):
        self.app_context_processors.append(f)
        return f

    def app_template_global(self, name=None):
        def decorator(f):
            self.app_template_globals[name or f.__name__] = f
            return f
        return decorator

    def app_template_filter(self, name=None):
        def decorator(f):
            self.app_template_filters[name or f.__name__] = f
            return f
        return decorator

    def before_request(self, f):
        self.before_request_funcs.append(f)
        return f

    def after_request(self, f):
        self.after_request_funcs.append(f)
        return f

    def teardown_request(self, f):
        self.teardown_request_funcs.append(f)
        return f

    def errorhandler(self, code):
        def decorator(f):
            self.error_handlers[code] = f
            return f
        return decorator


def _who(name):
    """Who the request is for, as the core asks (core.actor): the signed-in person and their address while a request is
    served, and the workspace the page has looked up (g.ws, or the engine's), in a request or not."""
    if name == "workspace_id":
        actx = local.app_ctx.get()
        if actx is None:
            return None
        ws = actx.g.get("ws") or getattr(actx.g.get("engine"), "ws", None)
        return actor._identity(ws) if ws is not None else None
    rctx = local.request_ctx.get()
    if rctx is None:
        return None
    if name == "user":
        from .login import _get_user
        try:
            who = _get_user()
        except Exception:  # noqa: BLE001 - nobody can be signed in here (no sign-in set up)
            return None
        return who if who is not None and who.is_authenticated else None
    if name == "ip":
        r = rctx.request
        return (r.headers.get("X-Forwarded-For", r.remote_addr or "").split(",")[0].strip()[:64]) or None
    return None


class AppContext:
    """The platform's context for code that runs outside a request too (a worker, a test, a script): the app, its settings,
    its database session (one per context, given back when it ends), `g`, and who is acting."""

    def __init__(self, app):
        self.app = app
        self.g = _Globals()
        self._stacks = []

    def push(self):
        stack = ExitStack()
        token = local.app_ctx.set(self)
        stack.callback(local.app_ctx.reset, token)
        stack.enter_context(core_context.entered(self.app))
        stack.enter_context(core_settings.using(self.app.config))
        database = self.app.extensions.get("sqlalchemy")
        if database is not None and getattr(self.app, "engine", None) is not None:
            stack.enter_context(database.using(self.app.engine))
        stack.enter_context(actor.reading(_who))
        self._stacks.append(stack)
        return self

    def pop(self, exc=_sentinel):
        try:
            if len(self._stacks) == 1:
                if exc is _sentinel:
                    exc = sys.exc_info()[1]
                self.app.do_teardown_appcontext(exc)
        finally:
            self._stacks.pop().close()

    def __enter__(self):
        return self.push()

    def __exit__(self, exc_type, exc, tb):
        self.pop(exc)


class RequestContext:
    """One request's context: the request, its session, its messages, and the app context it runs in (its own, unless the
    caller is already in one of this app's, as a test is)."""

    def __init__(self, app, request, session=None):
        self.app, self.request, self.session = app, request, session
        self.flashes = None
        self._tokens = []
        self._native_tokens = []

    def match_request(self):
        try:
            rule, values = self.app.url_map.match(self.request.path, self.request.method)
        except RequestRedirect as exc:
            query = self.request.query_string.decode("latin-1")
            exc.new_url = self.request.script_root + exc.new_url + (f"?{query}" if query else "")
            self.request.routing_exception = exc
        except HTTPException as exc:
            self.request.routing_exception = exc
        else:
            self.request.url_rule, self.request.view_args, self.request.endpoint = rule, values, rule.endpoint

    def push(self):
        actx = local.app_ctx.get()
        if actx is None or actx.app is not self.app:
            actx = self.app.app_context()
            actx.push()
        else:
            actx = None
        self._tokens.append((local.request_ctx.set(self), actx))
        if self.session is None:
            self.session = self.app.session_interface.open_session(self.app, self.request)
        # transition: the request seen as a native one too, so the helpers both layers use are written once (app/web/http.py)
        from ..web import http
        native = http.seen_natively(self.request.scope, local.app_ctx.get().g.__dict__, self.session)
        self._native_tokens.append(http._request.set(native))
        self.match_request()
        return self

    def pop(self, exc=_sentinel):
        clear = len(self._tokens) == 1
        try:
            if clear:
                if exc is _sentinel:
                    exc = sys.exc_info()[1]
                self.app.do_teardown_request(exc)
                self.request.close()
        finally:
            if self._native_tokens:
                from ..web import http
                http._request.reset(self._native_tokens.pop())
            token, actx = self._tokens.pop()
            local.request_ctx.reset(token)
            if actx is not None:
                actx.pop(exc)

    def __enter__(self):
        return self.push()

    def __exit__(self, exc_type, exc, tb):
        self.pop(exc)


class App:
    """The platform app: what create_app() returns. It is an ASGI app (`uvicorn asgi:app`), and it opens the platform's
    context for workers and scripts (`with app.app_context():`)."""

    session_interface = SessionInterface()

    def __init__(self, import_name, root_path=None, template_folder="templates", static_folder="static"):
        self.import_name = self.name = import_name
        self.root_path = str(root_path or Path(sys.modules[import_name].__file__).resolve().parent)
        self.template_folder = str(Path(self.root_path) / template_folder)
        self.static_folder = str(Path(self.root_path) / static_folder) if static_folder else None
        self.config = Config(DEFAULTS)
        self.extensions = {}
        self.blueprints = {}
        self.view_functions = {}
        self.url_map = Map()
        self.before_request_funcs, self.after_request_funcs = {None: []}, {None: []}
        self.teardown_request_funcs, self.teardown_appcontext_funcs = {None: []}, []
        self.error_handlers = {None: {}}
        self.template_context_processors = [self._default_context]
        self._template_globals, self._template_filters = {}, {}
        self._jinja_env = None
        self.engine = None
        self.asgi = self.serve  # what an ASGI server calls: create_app puts the FastAPI front (with this inside) here
        self._logger = None
        if self.static_folder:
            self.add_url_rule("/static/<path:filename>", "static", self.send_static_file)

    # settings ----------------------------------------------------------------------------------------------------------
    @property
    def logger(self):
        """Made the first time it's used, so it finds the log files create_app set up by then."""
        if self._logger is None:
            self._logger = _logger(self)
        return self._logger

    @property
    def debug(self):
        return bool(self.config.get("DEBUG"))

    @debug.setter
    def debug(self, value):
        self.config["DEBUG"] = bool(value)

    @property
    def testing(self):
        return bool(self.config.get("TESTING"))

    @property
    def secret_key(self):
        return self.config.get("SECRET_KEY")

    # pages -------------------------------------------------------------------------------------------------------------
    def add_url_rule(self, rule, endpoint=None, view_func=None, methods=None, **_):
        endpoint = endpoint or view_func.__name__
        existing = self.view_functions.get(endpoint)
        if existing is not None and existing is not view_func:
            raise AssertionError(f"View function mapping is overwriting an existing endpoint function: {endpoint}")
        self.view_functions[endpoint] = view_func
        self.url_map.add(Rule(rule, endpoint, view_func, methods))

    def route(self, rule, **options):
        def decorator(view):
            self.add_url_rule(rule, options.pop("endpoint", None), view, **options)
            return view
        return decorator

    def register_blueprint(self, bp):
        if bp.name in self.blueprints and self.blueprints[bp.name] is not bp:
            raise ValueError(f"The name {bp.name!r} is already registered for a different blueprint")
        self.blueprints[bp.name] = bp
        for rule, endpoint, view, methods in bp.rules:
            if bp.url_prefix is not None:
                rule = "/".join((bp.url_prefix.rstrip("/"), rule.lstrip("/"))) if rule else bp.url_prefix
            self.add_url_rule(rule, f"{bp.name}.{endpoint}", view, methods)
        self.template_context_processors.extend(bp.app_context_processors)
        self._template_globals.update(bp.app_template_globals)
        self._template_filters.update(bp.app_template_filters)
        if self._jinja_env is not None:
            self._jinja_env.globals.update(bp.app_template_globals)
            self._jinja_env.filters.update(bp.app_template_filters)
        self.before_request_funcs.setdefault(bp.name, []).extend(bp.before_request_funcs)
        self.after_request_funcs.setdefault(bp.name, []).extend(bp.after_request_funcs)
        self.teardown_request_funcs.setdefault(bp.name, []).extend(bp.teardown_request_funcs)
        self.error_handlers.setdefault(bp.name, {}).update(bp.error_handlers)

    def send_static_file(self, filename):
        root = Path(self.static_folder).resolve()
        target = (root / filename).resolve()
        if root not in target.parents or not target.is_file():
            raise NotFound()
        return send_file(target, max_age=self.config.get("SEND_FILE_MAX_AGE_DEFAULT"))

    # hooks -------------------------------------------------------------------------------------------------------------
    def before_request(self, f):
        self.before_request_funcs[None].append(f)
        return f

    def after_request(self, f):
        self.after_request_funcs[None].append(f)
        return f

    def teardown_request(self, f):
        self.teardown_request_funcs[None].append(f)
        return f

    def teardown_appcontext(self, f):
        self.teardown_appcontext_funcs.append(f)
        return f

    def errorhandler(self, code_or_exception):
        def decorator(f):
            self.error_handlers[None][code_or_exception] = f
            return f
        return decorator

    def register_error_handler(self, code_or_exception, f):
        self.error_handlers[None][code_or_exception] = f

    def context_processor(self, f):
        self.template_context_processors.append(f)
        return f

    def template_global(self, name=None):
        def decorator(f):
            self.jinja_env.globals[name or f.__name__] = f
            return f
        return decorator

    # templates ---------------------------------------------------------------------------------------------------------
    @property
    def jinja_env(self):
        if self._jinja_env is None:
            self._jinja_env = self._make_jinja_env()
        return self._jinja_env

    def _make_jinja_env(self):
        import jinja2
        auto_reload = self.config.get("TEMPLATES_AUTO_RELOAD")
        env = jinja2.Environment(loader=jinja2.FileSystemLoader(self.template_folder), autoescape=_autoescape,
                                 auto_reload=self.debug if auto_reload is None else bool(auto_reload))
        from . import g, request, session
        env.globals.update(url_for=url_for, get_flashed_messages=get_flashed_messages, config=self.config, request=request,
                           session=session, g=g)
        env.globals.update(self._template_globals)
        env.filters.update(self._template_filters)
        env.policies["json.dumps_function"] = dumps
        return env

    def _default_context(self):
        out = {}
        actx = local.app_ctx.get()
        if actx is not None:
            out["g"] = actx.g
        rctx = local.request_ctx.get()
        if rctx is not None:
            out["request"], out["session"] = rctx.request, rctx.session
        return out

    def update_template_context(self, context):
        given = dict(context)
        for processor in self.template_context_processors:
            context.update(processor())
        context.update(given)

    # contexts ----------------------------------------------------------------------------------------------------------
    def app_context(self):
        return AppContext(self)

    def request_context(self, request):
        return RequestContext(self, request)

    def test_request_context(self, path="/", method="GET", headers=None, data=None, base_url="http://localhost", query_string=None):
        """A request's context without a request (a script making a link, a test calling a view's helper)."""
        from urllib.parse import urlencode, urlsplit
        host = urlsplit(base_url)
        path, _, query = path.partition("?")
        if query_string:
            query = query_string if isinstance(query_string, str) else urlencode(query_string, doseq=True)
        raw = [(b"host", host.netloc.encode())] + [(k.lower().encode(), str(v).encode()) for k, v in (headers or {}).items()]
        body = data.encode() if isinstance(data, str) else (data or b"")
        scope = {"type": "http", "method": method.upper(), "scheme": host.scheme or "http", "path": path or "/",
                 "root_path": "", "query_string": query.encode(), "headers": raw, "client": ("127.0.0.1", 0),
                 "server": (host.hostname or "localhost", host.port or 80)}
        return RequestContext(self, Request(scope, body))

    def test_client(self, use_cookies=True, **_):
        from .testing import Client
        return Client(self)

    def do_teardown_request(self, exc=None):
        rctx = local.request_ctx.get()
        names = [*(rctx.request.blueprints if rctx is not None else []), None]
        for name in names:
            for f in reversed(self.teardown_request_funcs.get(name, [])):
                f(exc)

    def do_teardown_appcontext(self, exc=None):
        for f in reversed(self.teardown_appcontext_funcs):
            f(exc)

    # serving a request ---------------------------------------------------------------------------------------------------
    def handle(self, request):
        """One request, start to finish, in this thread: its Response (or, under TESTING, the error no page was made for)."""
        ctx = self.request_context(request)
        error = None
        try:
            try:
                ctx.push()
                return self.full_dispatch_request(ctx)
            except Exception as exc:
                error = exc
                return self.handle_exception(exc)
            except BaseException:
                error = sys.exc_info()[1]
                raise
        finally:
            ctx.pop(error)

    def full_dispatch_request(self, ctx):
        try:
            rv = self.preprocess_request(ctx)
            if rv is None:
                rv = self.dispatch_request(ctx)
        except Exception as exc:
            rv = self.handle_user_exception(ctx, exc)
        return self.finalize_request(ctx, rv)

    def preprocess_request(self, ctx):
        for name in (None, *reversed(ctx.request.blueprints)):
            for f in self.before_request_funcs.get(name, []):
                rv = f()
                if rv is not None:
                    return rv
        return None

    def dispatch_request(self, ctx):
        request = ctx.request
        if request.routing_exception is not None:
            raise request.routing_exception
        if request.method == "OPTIONS":
            from .wrappers import Response
            response = Response()
            response.headers["Allow"] = ", ".join(sorted(request.url_rule.methods))
            return response
        return self.view_functions[request.endpoint](**request.view_args)

    def finalize_request(self, ctx, rv, from_error_handler=False):
        response = make_response(rv)
        try:
            response = self.process_response(ctx, response)
        except Exception:
            if not from_error_handler:
                raise
            self.logger.exception("Request finalizing failed with an error while handling an error")
        return response

    def process_response(self, ctx, response):
        for name in (*ctx.request.blueprints, None):
            for f in reversed(self.after_request_funcs.get(name, [])):
                response = f(response)
        self.session_interface.save_session(self, ctx.session, response)
        return response

    def _find_error_handler(self, ctx, exc):
        code = exc.code if isinstance(exc, HTTPException) else None
        for name in (*ctx.request.blueprints, None):
            handlers = self.error_handlers.get(name) or {}
            if code is not None and code in handlers:
                return handlers[code]
            for cls in type(exc).__mro__:
                if cls in handlers:
                    return handlers[cls]
        return None

    def handle_user_exception(self, ctx, exc):
        exc = _from_native(exc)
        if isinstance(exc, HTTPException):
            if exc.code is None or isinstance(exc, RequestRedirect):
                return exc
            handler = self._find_error_handler(ctx, exc)
            return exc if handler is None else handler(exc)
        handler = self._find_error_handler(ctx, exc)
        if handler is None:
            raise exc
        return handler(exc)

    def handle_exception(self, exc):
        propagate = self.config.get("PROPAGATE_EXCEPTIONS")
        if propagate is None:
            propagate = self.testing or self.debug
        if propagate:
            raise exc
        ctx = local.request_ctx.get()
        self.logger.error(f"Exception on {ctx.request.path} [{ctx.request.method}]", exc_info=(type(exc), exc, exc.__traceback__))
        server_error = InternalServerError(original_exception=exc)
        handler = self._find_error_handler(ctx, server_error)
        rv = handler(server_error) if handler is not None else server_error
        return self.finalize_request(ctx, rv, from_error_handler=True)

    # ASGI -------------------------------------------------------------------------------------------------------------
    async def __call__(self, scope, receive, send):
        await self.asgi(scope, receive, send)

    async def serve(self, scope, receive, send):
        """The pages, as an ASGI app: the body read here, the rest of the request served in a worker thread."""
        import anyio
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        request = await self._read(scope, receive)
        hint = scope.get("keel.context")  # the test client's: carry on in the caller's context, as Flask's did
        if hint is not None:
            response = await anyio.to_thread.run_sync(hint.run, self.handle, request)
        else:
            response = await anyio.to_thread.run_sync(self.handle, request)
        await response.send_to(send, request.method, request.headers.get("Range"))

    async def _read(self, scope, receive):
        """The request with its body, and its form taken apart (a too-large body isn't read: the view gets a 413)."""
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        limit = self.config.get("MAX_CONTENT_LENGTH")
        try:
            declared = int(headers.get("content-length", ""))
        except ValueError:
            declared = None
        too_large = limit is not None and declared is not None and declared > limit
        chunks, size = [], 0
        if not too_large:
            while True:
                message = await receive()
                if message["type"] != "http.request":
                    break
                chunk = message.get("body", b"")
                size += len(chunk)
                if limit is not None and size > limit:
                    too_large = True
                    break
                chunks.append(chunk)
                if not message.get("more_body"):
                    break
        body = b"".join(chunks)
        form = files = problem = None
        mimetype = headers.get("content-type", "").split(";")[0].strip().lower()
        if not too_large and mimetype in ("application/x-www-form-urlencoded", "multipart/form-data"):
            try:
                form, files = await _parse_form(scope, body, mimetype, self.config)
            except RequestEntityTooLarge:
                too_large = True
            except ValueError as exc:
                problem = str(exc) or None
                form = files = None
        return Request(scope, body, form, files, too_large, problem)


def _from_native(exc):
    """An error a native helper raised (app/web/http.py; app/security.py), as this layer's own (transition): an
    HTTPException(403, "why") is a 403 with its words, a Redirect is the redirect."""
    from starlette.exceptions import HTTPException as NativeHTTPException

    from ..web import http
    from .exceptions import for_code
    from .helpers import _from_native as response_of, redirect
    if isinstance(exc, http.Redirect):
        return HTTPException(response=redirect(exc.url, exc.code))
    if isinstance(exc, http.Answer):
        return HTTPException(response=response_of(exc.response))
    if isinstance(exc, NativeHTTPException):
        out = for_code(exc.status_code)(description=http.said(exc))
        if exc.headers and hasattr(out, "get_headers"):
            extra = list(exc.headers.items())
            plain = out.get_headers
            out.get_headers = lambda: plain() + extra
        return out
    return exc


async def _parse_form(scope, body, mimetype, config):
    """The form's values and files, as MultiDicts. Text values larger than MAX_FORM_MEMORY_SIZE are refused (413)."""
    from starlette.datastructures import Headers as StarletteHeaders, UploadFile
    from starlette.formparsers import FormParser, MultiPartException, MultiPartParser

    from .datastructures import FileStorage, Headers, MultiDict
    most = config.get("MAX_FORM_MEMORY_SIZE") or 10 ** 12
    parts = config.get("MAX_FORM_PARTS") or 1000

    async def stream():
        yield body
        yield b""

    headers = StarletteHeaders(raw=scope.get("headers", []))
    if mimetype == "multipart/form-data":
        parser = MultiPartParser(headers, stream(), max_files=parts, max_fields=parts, max_part_size=most)
    else:
        if len(body) > most:
            raise RequestEntityTooLarge()
        parser = FormParser(headers, stream(), max_fields=parts, max_part_size=most)
    try:
        data = await parser.parse()
    except MultiPartException as exc:
        if "maximum size" in exc.message:
            raise RequestEntityTooLarge() from None
        raise ValueError(exc.message) from None
    except Exception as exc:  # noqa: BLE001 - a body that isn't the form it says it is
        raise ValueError(f"The form couldn't be read: {exc}") from None
    form, files = MultiDict(), MultiDict()
    for key, value in data.multi_items():
        if isinstance(value, UploadFile):
            value.file.seek(0)
            files.add(key, FileStorage(value.file, value.filename, key, value.content_type,
                                       Headers(list(value.headers.items()))))
        else:
            form.add(key, value)
    return form, files


def _autoescape(name):
    return True if name is None else name.endswith((".html", ".htm", ".xml", ".xhtml", ".svg"))


def _logger(app):
    """The app's log, as app.logger (errors on pages, with what went wrong): to the platform's log files once
    create_app has set them up (through the root logger), and to the terminal when nothing else would show it."""
    logger = logging.getLogger(app.name)
    level, current = logger.getEffectiveLevel(), logger
    while current:
        if any(h.level <= level for h in current.handlers):
            return logger
        if not current.propagate:
            break
        current = current.parent
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s in %(module)s: %(message)s"))
    logger.addHandler(handler)
    return logger
