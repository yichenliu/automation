"""A client for tests and try-outs: `app.test_client()`, with the calls tests have always made (get, post with a form,
files, JSON or headers, follow_redirects, session_transaction) and answers to read (status_code, data, get_json()…).

Requests go through the real ASGI app (Starlette's TestClient), the way a browser's do. One thing is kept from how the
platform was tested before: a request made while the caller is in this app's context (a test with the database open,
a try-out in its copy) runs in that context, sharing its database session and `g`, so what the request saved is what
the test reads next. The caller's context is handed to the request with a one-use token in a header the app never sees."""
import contextlib
import contextvars
import itertools
import json as jsonlib
from http import HTTPStatus
from urllib.parse import urlsplit

HINT = "x-keel-test-context"
_hints = {}
_numbers = itertools.count(1)


class _Hinted:
    """The app as the test client calls it: the caller's context, named by the token header, put where the app looks."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            token, kept = None, []
            for key, value in scope.get("headers", []):
                if key == HINT.encode():
                    token = value.decode()
                else:
                    kept.append((key, value))
            if token is not None:
                scope = {**scope, "headers": kept}
                hint = _hints.pop(token, None)
                if hint is not None:
                    scope["keel.context"] = hint
        await self.app(scope, receive, send)


class TestResponse:
    """An answer, read the way tests read it."""

    __test__ = False

    def __init__(self, response, history=()):
        self._response = response
        self.status_code = response.status_code
        self.headers = response.headers
        self.data = response.content
        self.history = list(history)
        self.request = response.request

    @property
    def status(self):
        try:
            return f"{self.status_code} {HTTPStatus(self.status_code).phrase.upper()}"
        except ValueError:
            return str(self.status_code)

    @property
    def content_type(self):
        return self.headers.get("content-type")

    @property
    def mimetype(self):
        found = self.headers.get("content-type")
        return found.split(";")[0].strip().lower() if found else None

    @property
    def location(self):
        return self.headers.get("location")

    @property
    def is_json(self):
        mt = self.mimetype or ""
        return mt == "application/json" or (mt.startswith("application/") and mt.endswith("+json"))

    def get_data(self, as_text=False):
        return self.data.decode("utf-8", "replace") if as_text else self.data

    @property
    def text(self):
        return self.get_data(as_text=True)

    def get_json(self, force=False, silent=False):
        if not (force or self.is_json):
            if silent:
                return None
            raise ValueError(f"The answer isn't JSON ({self.mimetype})")
        try:
            return jsonlib.loads(self.data)
        except ValueError:
            if silent:
                return None
            raise

    @property
    def json(self):
        return self.get_json()

    def __repr__(self):
        return f"<TestResponse [{self.status}]>"


class Client:
    """`app.test_client()`."""

    __test__ = False

    def __init__(self, app):
        from starlette.testclient import TestClient
        self.application = app
        self._client = TestClient(_Hinted(app), base_url="http://localhost", follow_redirects=False,
                                  raise_server_exceptions=True, client=("127.0.0.1", 50000))

    # making requests ----------------------------------------------------------------------------------------------------
    def open(self, path="/", method="GET", data=None, json=None, headers=None, content_type=None, follow_redirects=False,
             query_string=None, **_):
        method, history = method.upper(), []
        response = self._send(method, path, data, json, headers, content_type, query_string)
        while follow_redirects and response.status_code in (301, 302, 303, 305, 307, 308) and response.headers.get("location"):
            history.append(TestResponse(response))
            keep = response.status_code in (307, 308)
            if not keep:
                method = "HEAD" if method == "HEAD" else "GET"
                data = json = content_type = None
            target = response.headers["location"]
            if target.startswith(("http://", "https://")):
                parts = urlsplit(target)
                target = parts.path + (f"?{parts.query}" if parts.query else "")
            response = self._send(method, target, data, json, headers, content_type, None)
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
        sent = dict(headers or {})
        kwargs = {}
        if query_string:
            kwargs["params"] = query_string
        if json is not None:
            kwargs["json"] = json
        elif isinstance(data, (str, bytes, bytearray)):
            kwargs["content"] = data.encode() if isinstance(data, str) else bytes(data)
            sent.setdefault("Content-Type", content_type or "application/octet-stream")
        elif data:
            fields, files = _split(data)
            kwargs["data"] = fields
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

    # the session --------------------------------------------------------------------------------------------------------
    @contextlib.contextmanager
    def session_transaction(self):
        """The browser's session, to read or change before the next request (as if a page had)."""
        app = self.application
        interface = app.session_interface
        name = interface.cookie_name(app)
        jar = self._client.cookies.jar
        found = next((c for c in jar if c.name == name), None)
        session = interface.load(app, found.value if found else None)
        yield session
        if session.modified or found is None:
            if found is not None:
                jar.clear(found.domain, found.path, found.name)
            if session:
                self._client.cookies.set(name, interface.dump(app, session), domain=found.domain if found else "localhost.local",
                                         path=found.path if found else "/")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._client.close()


def _split(data):
    """A form given as a dict: its plain values (key → list), and its files (`(BytesIO, "name.csv")` or an open file)."""
    fields, files = {}, []
    for key, value in (data.items() if hasattr(data, "items") else data):
        for item in (value if isinstance(value, list) else [value]):
            if isinstance(item, tuple) and item and hasattr(item[0], "read"):
                stream, name = item[0], (item[1] if len(item) > 1 else getattr(item[0], "name", "file"))
                kind = item[2] if len(item) > 2 else None
                files.append((key, (name, stream.read(), kind) if kind else (name, stream.read())))
            elif hasattr(item, "read"):
                files.append((key, (getattr(item, "name", "file"), item.read())))
            elif item is not None:
                fields.setdefault(key, []).append(item if isinstance(item, (str, bytes)) else str(item))
    return fields, files
