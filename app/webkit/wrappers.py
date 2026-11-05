"""The request a view reads and the response it gives back.

A request's body is read before the view runs (it arrives over the network, so it's read while no thread waits for
it), and its form and files are taken apart then too. Everything a view asks of it after that is already there."""
import json
import os
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from http import HTTPStatus
from urllib.parse import parse_qsl, quote

from .datastructures import CombinedMultiDict, Headers, MultiDict
from .exceptions import BadRequest, RequestEntityTooLarge, UnsupportedMediaType

_PATH_SAFE = "/:@!$&'()*+,;=~"
_CHARSET_TYPES = {"application/ecmascript", "application/javascript", "application/sql", "application/xml",
                  "application/xml-dtd", "application/xml-external-parsed-entity"}


def content_type_for(mimetype):
    """`text/csv` → `text/csv; charset=utf-8`: text is always sent as UTF-8, and says so."""
    if "charset" not in mimetype and (mimetype.startswith("text/") or mimetype in _CHARSET_TYPES or mimetype.endswith("+xml")):
        return f"{mimetype}; charset=utf-8"
    return mimetype


def http_date(when):
    """A date as HTTP writes it (cookies, Last-Modified): `Wed, 05 Nov 2026 08:41:00 GMT`. A plain time is UTC."""
    if isinstance(when, (int, float)):
        when = datetime.fromtimestamp(when, timezone.utc)
    elif not isinstance(when, datetime):  # a date
        when = datetime(when.year, when.month, when.day, tzinfo=timezone.utc)
    elif when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return format_datetime(when.astimezone(timezone.utc), usegmt=True)


def _parse_cookies(header):
    out = {}
    for chunk in (header or "").split(";"):
        key, sep, value = chunk.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1]
        out.setdefault(key, value)
    return out


class Request:
    """One request, as the view sees it."""

    def __init__(self, scope, body=b"", form=None, files=None, too_large=False, form_problem=None):
        self.scope = scope
        self.method = scope.get("method", "GET").upper()
        self.scheme = scope.get("scheme", "http")
        root = scope.get("root_path", "") or ""
        path = scope.get("path", "/") or "/"
        self.script_root = root.rstrip("/")
        self.path = path[len(self.script_root):] if self.script_root and path.startswith(self.script_root) else path
        self.query_string = scope.get("query_string", b"") or b""
        self.headers = Headers([(k.decode("latin-1"), v.decode("latin-1")) for k, v in scope.get("headers", [])])
        self.args = MultiDict(parse_qsl(self.query_string.decode("utf-8", "replace"), keep_blank_values=True, errors="replace"))
        self.cookies = _parse_cookies(self.headers.get("Cookie"))
        client = scope.get("client")
        self.remote_addr = client[0] if client else None
        self._body, self._form, self._files = body, form, files
        self._too_large, self._form_problem = too_large, form_problem
        # what routing found: set before the view runs
        self.url_rule = None
        self.endpoint = None
        self.view_args = None
        self.routing_exception = None

    # where it was sent ------------------------------------------------------------------------------------------------
    @property
    def host(self):
        host = self.headers.get("Host")
        if host:
            return host
        server = self.scope.get("server")
        if not server:
            return "localhost"
        name, port = server[0], server[1]
        default = {"http": 80, "https": 443}.get(self.scheme)
        return name if port in (None, default) else f"{name}:{port}"

    @property
    def host_url(self):
        return f"{self.scheme}://{self.host}/"

    @property
    def url_root(self):
        return f"{self.scheme}://{self.host}{self.script_root}/"

    @property
    def base_url(self):
        return f"{self.scheme}://{self.host}{quote(self.script_root + self.path, safe=_PATH_SAFE)}"

    @property
    def url(self):
        query = self.query_string.decode("latin-1")
        return self.base_url + (f"?{query}" if query else "")

    @property
    def full_path(self):
        return f"{self.path}?{self.query_string.decode('utf-8', 'replace')}"

    @property
    def is_secure(self):
        return self.scheme in ("https", "wss")

    @property
    def referrer(self):
        return self.headers.get("Referer")

    @property
    def user_agent(self):
        return self.headers.get("User-Agent", "")

    @property
    def blueprint(self):
        endpoint = self.endpoint or ""
        return endpoint.rsplit(".", 1)[0] if "." in endpoint else None

    @property
    def blueprints(self):
        return [self.blueprint] if self.blueprint else []

    # what it carries ----------------------------------------------------------------------------------------------------
    @property
    def content_type(self):
        return self.headers.get("Content-Type", "")

    @property
    def mimetype(self):
        return self.content_type.split(";")[0].strip().lower()

    @property
    def content_length(self):
        return self.headers.get("Content-Length", type=int)

    def _check_size(self):
        if self._too_large:
            raise RequestEntityTooLarge()

    def get_data(self, cache=True, as_text=False, parse_form_data=False):
        self._check_size()
        return self._body.decode("utf-8", "replace") if as_text else self._body

    @property
    def data(self):
        return self.get_data()

    @property
    def form(self):
        self._check_size()
        if self._form_problem:
            raise BadRequest(self._form_problem)
        return self._form if self._form is not None else MultiDict()

    @property
    def files(self):
        self._check_size()
        if self._form_problem:
            raise BadRequest(self._form_problem)
        return self._files if self._files is not None else MultiDict()

    @property
    def values(self):
        if self.method == "GET":
            return CombinedMultiDict([self.args])
        return CombinedMultiDict([self.args, self.form])

    @property
    def is_json(self):
        mt = self.mimetype
        return mt == "application/json" or (mt.startswith("application/") and mt.endswith("+json"))

    def get_json(self, force=False, silent=False, cache=True):
        if not (force or self.is_json):
            if silent:
                return None
            raise UnsupportedMediaType("Did not attempt to load JSON data because the request Content-Type was not 'application/json'.")
        try:
            return json.loads(self.get_data())
        except ValueError as exc:
            if silent:
                return None
            raise BadRequest(f"Failed to decode JSON object: {exc}") from None

    @property
    def json(self):
        return self.get_json()

    def close(self):
        for upload in (self._files.items(multi=True) if self._files else []):
            upload[1].close()

    def __repr__(self):
        return f"<Request {self.url!r} [{self.method}]>"


class Response:
    """What goes back: a status, headers, and a body (text, bytes, a file on disk, or pieces made as it's sent)."""

    default_mimetype = "text/html"

    def __init__(self, response=None, status=None, headers=None, mimetype=None, content_type=None, direct_passthrough=False):
        self.headers = Headers(headers)
        if content_type is None:
            if mimetype is None and "Content-Type" not in self.headers:
                mimetype = self.default_mimetype
            if mimetype is not None:
                content_type = content_type_for(mimetype)
        if content_type is not None:
            self.headers["Content-Type"] = content_type
        self.status = 200 if status is None else status
        self._path = None  # a file on disk, sent in pieces (send_file)
        self._iterable = None  # pieces made as it's sent
        self._body = b""
        if response is None:
            pass
        elif isinstance(response, (str, bytes, bytearray)):
            self.set_data(response)
        else:
            self._iterable = response

    # status -----------------------------------------------------------------------------------------------------------
    @property
    def status_code(self):
        return self._status_code

    @status_code.setter
    def status_code(self, code):
        self._status_code = int(code)

    @property
    def status(self):
        try:
            return f"{self._status_code} {HTTPStatus(self._status_code).phrase.upper()}"
        except ValueError:
            return f"{self._status_code} UNKNOWN"

    @status.setter
    def status(self, value):
        if isinstance(value, int):
            self._status_code = value
        else:
            self._status_code = int(str(value).split()[0])

    # type -------------------------------------------------------------------------------------------------------------
    @property
    def content_type(self):
        return self.headers.get("Content-Type")

    @content_type.setter
    def content_type(self, value):
        self.headers["Content-Type"] = value

    @property
    def mimetype(self):
        found = self.headers.get("Content-Type")
        return found.split(";")[0].strip().lower() if found else None

    @mimetype.setter
    def mimetype(self, value):
        self.headers["Content-Type"] = content_type_for(value)

    @property
    def location(self):
        return self.headers.get("Location")

    @location.setter
    def location(self, value):
        self.headers["Location"] = value

    # body -------------------------------------------------------------------------------------------------------------
    def set_data(self, value):
        self._path, self._iterable = None, None
        self._body = value.encode("utf-8") if isinstance(value, str) else bytes(value)
        self.headers["Content-Length"] = str(len(self._body))

    def get_data(self, as_text=False):
        if self._path is not None:
            with open(self._path, "rb") as f:
                data = f.read()
        elif self._iterable is not None:
            data = b"".join(x.encode("utf-8") if isinstance(x, str) else bytes(x) for x in self._iterable)
            self.set_data(data)
        else:
            data = self._body
        return data.decode("utf-8", "replace") if as_text else data

    data = property(get_data, set_data)

    @property
    def is_streamed(self):
        return self._iterable is not None

    def get_json(self, silent=False):
        try:
            return json.loads(self.get_data())
        except ValueError:
            if silent:
                return None
            raise

    @property
    def json(self):
        return self.get_json()

    # cookies ----------------------------------------------------------------------------------------------------------
    def set_cookie(self, key, value="", max_age=None, expires=None, path="/", domain=None, secure=False, httponly=False,
                   samesite=None):
        parts = [f"{key}={value}"]
        if domain:
            parts.append(f"Domain={domain}")
        if isinstance(max_age, timedelta):
            max_age = int(max_age.total_seconds())
        if expires is not None:
            parts.append(f"Expires={http_date(expires) if not isinstance(expires, str) else expires}")
        elif max_age is not None:
            parts.append(f"Expires={http_date(datetime.now(timezone.utc) + timedelta(seconds=max_age))}")
        if max_age is not None:
            parts.append(f"Max-Age={int(max_age)}")
        if secure:
            parts.append("Secure")
        if httponly:
            parts.append("HttpOnly")
        if path:
            parts.append(f"Path={path}")
        if samesite:
            parts.append(f"SameSite={str(samesite).title()}")
        self.headers.add("Set-Cookie", "; ".join(parts))

    def delete_cookie(self, key, path="/", domain=None, secure=False, httponly=False, samesite=None):
        self.set_cookie(key, "", max_age=0, expires=0, path=path, domain=domain, secure=secure, httponly=httponly, samesite=samesite)

    def __repr__(self):
        return f"<Response [{self.status}]>"

    # sending ----------------------------------------------------------------------------------------------------------
    def _raw_headers(self):
        out = []
        for key, value in self.headers.items():
            if key.lower() == "location":  # a web address with letters outside ASCII, sent as the web writes them
                value = quote(value, safe="/:?#[]@!$&'()*+,;=%~")
            out.append((key.lower().encode("latin-1"), value.encode("latin-1", "replace")))
        return out

    async def send_to(self, send, method="GET", range_header=None):
        """Send it over ASGI. A file goes in pieces read off the request's event loop; a byte range is sent on its own when
        the browser asks for one (a PDF viewer, a video)."""
        import anyio
        status, headers = self.status_code, self._raw_headers()
        head_only = method == "HEAD"
        if self._path is not None:
            size = os.path.getsize(self._path)
            start, end = 0, size - 1
            if range_header and status == 200:
                wanted = _byte_range(range_header, size)
                if wanted is None:
                    headers = [(k, v) for k, v in headers if k != b"content-length"]
                    headers.append((b"content-range", f"bytes */{size}".encode()))
                    await send({"type": "http.response.start", "status": 416, "headers": headers + [(b"content-length", b"0")]})
                    await send({"type": "http.response.body", "body": b""})
                    return
                if wanted != (0, size - 1):
                    start, end = wanted
                    status = 206
                    headers = [(k, v) for k, v in headers if k != b"content-length"]
                    headers.append((b"content-range", f"bytes {start}-{end}/{size}".encode()))
            headers = [(k, v) for k, v in headers if k != b"content-length"] + [(b"content-length", str(end - start + 1).encode())]
            await send({"type": "http.response.start", "status": status, "headers": headers})
            if head_only:
                await send({"type": "http.response.body", "body": b""})
                return
            with open(self._path, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = await anyio.to_thread.run_sync(f.read, min(64 * 1024, left))
                    if not chunk:
                        break
                    left -= len(chunk)
                    await send({"type": "http.response.body", "body": chunk, "more_body": left > 0})
                if left > 0:
                    await send({"type": "http.response.body", "body": b""})
            return
        if self._iterable is not None:
            headers = [(k, v) for k, v in headers if k != b"content-length"]
            await send({"type": "http.response.start", "status": status, "headers": headers})
            if not head_only:
                iterator = iter(self._iterable)
                done = object()
                while True:
                    chunk = await anyio.to_thread.run_sync(next, iterator, done)
                    if chunk is done:
                        break
                    await send({"type": "http.response.body", "body": chunk.encode("utf-8") if isinstance(chunk, str) else bytes(chunk),
                                "more_body": True})
            await send({"type": "http.response.body", "body": b""})
            return
        if not any(k == b"content-length" for k, _ in headers) and status not in (204, 304) and not 100 <= status < 200:
            headers.append((b"content-length", str(len(self._body)).encode()))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": b"" if head_only or status in (204, 304) else self._body})


def _byte_range(header, size):
    """`bytes=0-1023` → (0, 1023); None for a range that can't be given (one range only, as browsers ask)."""
    unit, _, spec = header.partition("=")
    if unit.strip().lower() != "bytes" or "," in spec or size == 0:
        return None
    first, _, last = spec.strip().partition("-")
    try:
        if first == "":
            start, end = max(size - int(last), 0), size - 1
        else:
            start, end = int(first), (int(last) if last else size - 1)
    except ValueError:
        return None
    end = min(end, size - 1)
    if start > end or start >= size:
        return None
    return start, end
