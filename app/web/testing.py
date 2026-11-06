"""Requests without a browser, for the tests and the builder's try-outs: `app.test_client()`, on Starlette's TestClient.

    client = app.test_client()
    page = client.post("/login", data={"email": ..., "password": ...}, follow_redirects=True)
    page.status_code, page.get_data(as_text=True), page.get_json(), page.headers["Location"], page.location

A request made while the caller is in this app's context (a test with the database open, a try-out in its copy) is
served in that context, sharing its database session, so what the request saved is what the caller reads next: the
caller's context is handed over with a one-use token in a header the pages never see (app/web/http.py, serving).
`a_request()` is a page's request with no browser at all, for a test that calls a page's helper directly.
"""
import contextlib
import contextvars
import functools
import itertools
import json as jsonlib
from urllib.parse import urlsplit

from . import http

HINT = "x-keel-test-context"
_hints = {}
_numbers = itertools.count(1)


class TestResponse:
    """An answer, read the way the tests read it."""

    __test__ = False

    def __init__(self, response, history=()):
        self.status_code, self.headers, self.data = response.status_code, response.headers, response.content
        self.history = list(history)

    @property
    def location(self):
        return self.headers.get("location")

    @property
    def mimetype(self):
        found = self.headers.get("content-type")
        return found.split(";")[0].strip().lower() if found else None

    def get_data(self, as_text=False):
        return self.data.decode("utf-8", "replace") if as_text else self.data

    @property
    def text(self):
        return self.get_data(as_text=True)

    def get_json(self, silent=False):
        """The answer as JSON (None with `silent` when it isn't JSON)."""
        mimetype = self.mimetype or ""
        if mimetype == "application/json" or (mimetype.startswith("application/") and mimetype.endswith("+json")):
            try:
                return jsonlib.loads(self.data)
            except ValueError:
                if not silent:
                    raise
        elif not silent:
            raise ValueError(f"The answer isn't JSON ({mimetype})")
        return None

    @property
    def json(self):
        return self.get_json()

    def __repr__(self):
        return f"<TestResponse {self.status_code}>"


class Client:
    """`app.test_client()`: get, post, put, patch, delete, head and options, each with `data` (a form: a dict whose values
    may be lists, or `(file, "name.csv")` for an upload; or bytes), `json`, `headers`, `content_type`, `query_string` and
    `follow_redirects`; `session_transaction()` to read or change the browser's session between requests."""

    __test__ = False

    def __init__(self, app):
        from starlette.testclient import TestClient
        self.application = app
        self._client = TestClient(_Hinted(app), base_url="http://localhost", follow_redirects=False,
                                  raise_server_exceptions=True, client=("127.0.0.1", 50000))

    def open(self, path="/", method="GET", data=None, json=None, headers=None, content_type=None, follow_redirects=False,
             query_string=None):
        method, history = method.upper(), []
        response = self._send(method, path, data, json, headers, content_type, query_string)
        while follow_redirects and response.status_code in (301, 302, 303, 307, 308) and response.headers.get("location"):
            history.append(TestResponse(response))
            if response.status_code not in (307, 308):  # the browser asks for the next page plainly
                method = "HEAD" if method == "HEAD" else "GET"
                data = json = content_type = None
            parts = urlsplit(response.headers["location"])
            response = self._send(method, parts.path + (f"?{parts.query}" if parts.query else ""), data, json, headers, content_type, None)
        return TestResponse(response, history)

    def get(self, path="/", **kwargs):
        return self.open(path, "GET", **kwargs)

    def post(self, path="/", **kwargs):
        return self.open(path, "POST", **kwargs)

    def put(self, path="/", **kwargs):
        return self.open(path, "PUT", **kwargs)

    def patch(self, path="/", **kwargs):
        return self.open(path, "PATCH", **kwargs)

    def delete(self, path="/", **kwargs):
        return self.open(path, "DELETE", **kwargs)

    def head(self, path="/", **kwargs):
        return self.open(path, "HEAD", **kwargs)

    def options(self, path="/", **kwargs):
        return self.open(path, "OPTIONS", **kwargs)

    def _send(self, method, path, data, json, headers, content_type, query_string):
        sent, kwargs = dict(headers or {}), {}
        if query_string:
            kwargs["params"] = query_string
        if json is not None:
            kwargs["json"] = json
        elif isinstance(data, (str, bytes, bytearray)):
            kwargs["content"] = data.encode() if isinstance(data, str) else bytes(data)
            sent.setdefault("Content-Type", content_type or "application/octet-stream")
        elif data:
            kwargs["data"], files = _split(data)
            if files:
                kwargs["files"] = files
        elif content_type:
            sent.setdefault("Content-Type", content_type)
        token = str(next(_numbers))
        _hints[token] = contextvars.copy_context()
        sent[HINT] = token
        try:
            return self._client.request(method, path, headers=sent, **kwargs)
        finally:
            _hints.pop(token, None)

    @contextlib.contextmanager
    def session_transaction(self):
        """The browser's session, to read or change before the next request (as if a page had)."""
        config = self.application.config
        name, jar = http.SESSION_COOKIE, self._client.cookies.jar
        found = next((c for c in jar if c.name == name), None)
        session = http.load_session(config, found.value if found else None)
        yield session
        if session.modified or found is None:
            if found is not None:
                jar.clear(found.domain, found.path, found.name)
            if session:
                self._client.cookies.set(name, http.dump_session(config, session), domain=found.domain if found else "localhost.local",
                                         path=found.path if found else "/")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._client.close()


class _Hinted:
    """The app as the client calls it: the caller's context, named by the token header, put where serving looks."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            token = next((v.decode() for k, v in scope.get("headers", []) if k == HINT.encode()), None)
            if token is not None:
                scope = {**scope, "headers": [(k, v) for k, v in scope["headers"] if k != HINT.encode()]}
                hint = _hints.pop(token, None)
                if hint is not None:
                    scope["keel.context"] = hint
        await self.app(scope, receive, send)


def _split(data):
    """A form given as a dict: its plain values (key → list), and its files (`(BytesIO, "name.csv")` or an open file)."""
    fields, files = {}, []
    for key, value in (data.items() if hasattr(data, "items") else data):
        for item in (value if isinstance(value, list) else [value]):
            if isinstance(item, tuple) and item and hasattr(item[0], "read"):
                stream, name = item[0], (item[1] if len(item) > 1 else getattr(item[0], "name", "file"))
                files.append((key, (name, stream.read(), item[2]) if len(item) > 2 and item[2] else (name, stream.read())))
            elif hasattr(item, "read"):
                files.append((key, (getattr(item, "name", "file"), item.read())))
            elif item is not None:
                fields.setdefault(key, []).append(item if isinstance(item, (str, bytes)) else str(item))
    return fields, files


@contextlib.contextmanager
def a_request(path="/", user=None):
    """A page's request with no browser (in the platform's context), for a test that calls a page's helper directly:
    current_request() while the block runs, with an empty session and `user` signed in; who is acting is asked of it, as
    of a page's (the workspace once request.state.ws is set)."""
    from ..core import actor
    parts = urlsplit(path)
    request = http.PageRequest({"type": "http", "method": "GET", "scheme": "http", "path": parts.path or "/",
                                "query_string": parts.query.encode(), "root_path": "", "headers": [(b"host", b"localhost")],
                                "client": ("127.0.0.1", 50000), "server": ("localhost", 80), "state": {}})
    request.state.session = http.Session()
    with http.answering(request), actor.reading(functools.partial(http._who, request)):
        if user is not None:
            http.login_user(request, user)
        yield request
