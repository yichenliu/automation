"""What views call: addresses (url_for), redirects, JSON, files, messages for the next page, and pages from templates."""
import dataclasses
import decimal
import io
import json
import mimetypes
import os
import unicodedata
import uuid
import zlib
from datetime import date
from pathlib import Path
from urllib.parse import quote

from markupsafe import escape

from .exceptions import abort  # noqa: F401 (views import it from here)
from .local import app_ctx, request_ctx
from .wrappers import Response, http_date


# --- JSON ----------------------------------------------------------------------------------------------------------------

def _default(o):
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
    kwargs.setdefault("default", _default)
    kwargs.setdefault("ensure_ascii", True)
    kwargs.setdefault("sort_keys", True)
    return json.dumps(obj, **kwargs)


def jsonify(*args, **kwargs):
    if args and kwargs:
        raise TypeError("jsonify() takes either arguments or keywords, not both")
    data = args[0] if len(args) == 1 else (list(args) if args else kwargs)
    return Response(dumps(data, separators=(",", ":")) + "\n", mimetype="application/json")


# --- addresses ---------------------------------------------------------------------------------------------------------

def url_for(endpoint, *, _anchor=None, _method=None, _scheme=None, _external=None, **values):
    """The address of a view: `url_for("app.record", wid=1, table="orders", rid=7)`. Values the address doesn't name go in
    its query string; `_external` gives the whole address with its host; `.name` is a view of the current blueprint.
    A page served natively (app/web/http.py) is found by its name too, and on a native page this is http.url_for."""
    ctx = request_ctx.get()
    if ctx is None:
        from ..web import http
        if http.current_request() is not None:  # transition: a native page's request
            return http.url_for(endpoint, _anchor=_anchor, _external=bool(_external), _method=_method, **values)
    if ctx is not None:
        app, request = ctx.app, ctx.request
        if endpoint[:1] == ".":
            endpoint = f"{request.blueprint}{endpoint}" if request.blueprint else endpoint[1:]
        if _external is None:
            _external = _scheme is not None
        host, scheme, root = request.host, _scheme or request.scheme, request.script_root
    else:
        actx = app_ctx.get()
        if actx is None:
            raise RuntimeError("Working outside the platform's context: run inside app.app_context()")
        app = actx.app
        if not app.config.get("SERVER_NAME"):
            raise RuntimeError("Unable to build URLs outside an active request without 'SERVER_NAME' configured. Also configure "
                               "'APPLICATION_ROOT' and 'PREFERRED_URL_SCHEME' as needed.")
        if _external is None:
            _external = True
        host = app.config["SERVER_NAME"]
        scheme = _scheme or app.config.get("PREFERRED_URL_SCHEME") or "http"
        root = (app.config.get("APPLICATION_ROOT") or "/").rstrip("/")
    path = root + _path(app, endpoint, values, _method)
    if _external:
        path = f"{scheme}://{host}{path}"
    if _anchor is not None:
        path = f"{path}#{quote(str(_anchor), safe='%!#$&' + chr(39) + '()*+,/:;=?@')}"
    return path


def _path(app, endpoint, values, method):
    """The path of a view of this layer, or (transition) of a page served natively (app/web/http.py)."""
    if endpoint not in app.url_map.by_endpoint and endpoint in (app.extensions.get("pages") or {}):
        from ..web import http
        return http.path_for(app, endpoint, values, method)
    return app.url_map.build(endpoint, values, method=method)


def redirect(location, code=302):
    location = str(location)
    shown = escape(location)
    response = Response("<!doctype html>\n<html lang=en>\n<title>Redirecting...</title>\n<h1>Redirecting...</h1>\n"
                        "<p>You should be redirected automatically to the target URL: "
                        f'<a href="{shown}">{shown}</a>. If not, click the link.\n', code, mimetype="text/html")
    response.headers["Location"] = location
    return response


# --- responses ----------------------------------------------------------------------------------------------------------

def make_response(*args):
    """A view's answer as a Response, the forms a view may return: a Response, text, a dict (JSON), and any of those
    with a status and headers: `(page, 400)`, `("", 405, {"Allow": "POST"})`."""
    from .exceptions import HTTPException
    if not args:
        return Response()
    rv = args[0] if len(args) == 1 else args
    status = headers = None
    if isinstance(rv, tuple):
        if len(rv) == 3:
            rv, status, headers = rv
        elif len(rv) == 2:
            if isinstance(rv[1], (dict, tuple, list)) or hasattr(rv[1], "items"):
                rv, headers = rv
            else:
                rv, status = rv
        else:
            raise TypeError("A view's answer as a tuple is (body, status), (body, headers) or (body, status, headers)")
    if rv is None:
        raise TypeError("The view didn't return an answer: it ended without a return statement")
    rv = _from_native(rv)
    if isinstance(rv, HTTPException):
        rv = rv.get_response()
    if not isinstance(rv, Response):
        if isinstance(rv, (str, bytes, bytearray)) or hasattr(rv, "__next__"):
            rv = Response(rv, status=status, headers=headers)
            status = headers = None
        elif isinstance(rv, (dict, list)):
            rv = jsonify(rv)
        else:
            raise TypeError(f"A view can't answer with a {type(rv).__name__}")
    if status is not None:
        if isinstance(status, (str, bytes)):
            rv.status = status.decode() if isinstance(status, bytes) else status
        else:
            rv.status_code = status
    if headers:
        rv.headers.update(headers)
    return rv


def _from_native(rv):
    """A native (Starlette) response a converted helper gave back (render, redirect, json_response, send_file), as this
    layer's (transition)."""
    from starlette.responses import FileResponse, Response as NativeResponse, StreamingResponse
    if not isinstance(rv, NativeResponse):
        return rv
    if isinstance(rv, StreamingResponse):
        raise TypeError("A streamed native response can't be answered by the old layer's pages")
    headers = [(k.decode("latin-1"), v.decode("latin-1")) for k, v in rv.raw_headers]
    if isinstance(rv, FileResponse):
        out = Response(None, rv.status_code, headers=headers)
        out._path = str(rv.path)
        return out
    return Response(rv.body, rv.status_code, headers=headers)


def send_file(path_or_file, mimetype=None, as_attachment=False, download_name=None, conditional=True, etag=True,
              last_modified=None, max_age=None):
    """A file as the answer: one on disk (sent in pieces, a part of it when the browser asks) or bytes in memory. The
    browser checks it again each time (no-cache), and gets a "not changed" when it already has it."""
    path, file, size, mtime = None, None, None, None
    if isinstance(path_or_file, (str, os.PathLike)):
        path = Path(path_or_file)
        if not path.is_absolute():
            path = Path(app_ctx.get().app.root_path) / path
        stat = path.stat()
        size, mtime = stat.st_size, stat.st_mtime
    else:
        file = path_or_file
    if download_name is None and path is not None:
        download_name = path.name
    if mimetype is None:
        if download_name is None:
            raise TypeError("Unable to detect the MIME type because a file name is not available. Either set 'download_name', "
                            "pass a path instead of a file, or set 'mimetype'.")
        mimetype = mimetypes.guess_type(download_name)[0] or "application/octet-stream"
    response = Response(None, mimetype=mimetype)
    if download_name is not None:
        response.headers["Content-Disposition"] = _disposition("attachment" if as_attachment else "inline", download_name)
    elif as_attachment:
        raise TypeError("No name provided for attachment.")
    if path is not None:
        response._path = str(path)
        response.headers["Content-Length"] = str(size)
    else:
        if isinstance(file, io.TextIOBase):
            raise ValueError("Files must be opened in binary mode or use BytesIO.")
        data = file.getvalue() if isinstance(file, io.BytesIO) else file.read()
        response.set_data(data)
    if last_modified is not None or mtime is not None:
        response.headers["Last-Modified"] = http_date(last_modified if last_modified is not None else mtime)
    response.headers["Cache-Control"] = "no-cache"
    if max_age is not None:
        response.headers["Cache-Control"] = f"public, max-age={int(max_age)}" if max_age > 0 else f"no-cache, max-age={int(max_age)}"
    tag = etag if isinstance(etag, str) else (f"{mtime}-{size}-{zlib.adler32(str(path).encode()) & 0xFFFFFFFF}"
                                              if etag and path is not None else None)
    if tag:
        response.headers["ETag"] = f'"{tag}"'
    if conditional:
        response.headers["Accept-Ranges"] = "bytes"
        ctx = request_ctx.get()
        if ctx is not None and ctx.request.method in ("GET", "HEAD") and _not_modified(ctx.request, tag, response.headers.get("Last-Modified")):
            response.status_code = 304
            response._path = None
            response.set_data(b"")
            del response.headers["Content-Length"]
    return response


def _not_modified(request, tag, modified):
    from email.utils import parsedate_to_datetime
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


def _quoted(value):
    if value and set(value) <= _TOKEN:
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _disposition(kind, name):
    """`attachment; filename=orders.csv`, with the name also in UTF-8 when it isn't plain ASCII."""
    try:
        name.encode("ascii")
    except UnicodeEncodeError:
        simple = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
        return f"{kind}; filename={_quoted(simple)}; filename*=UTF-8''{quote(name, safe='!#$&+-.^_`|~')}"
    return f"{kind}; filename={_quoted(name)}"


# --- messages for the next page ----------------------------------------------------------------------------------------

def flash(message, category="message"):
    """A message shown on the next page the person sees ("Saved.", "That code isn't right.")."""
    session = request_ctx.get().session
    flashes = session.get("_flashes", [])
    flashes.append((category, message))
    session["_flashes"] = flashes


def get_flashed_messages(with_categories=False, category_filter=()):
    ctx = request_ctx.get()
    flashes = ctx.flashes
    if flashes is None:
        flashes = ctx.session.pop("_flashes") if "_flashes" in ctx.session else []
        ctx.flashes = flashes
    if category_filter:
        flashes = [f for f in flashes if f[0] in category_filter]
    if not with_categories:
        return [f[1] for f in flashes]
    return [tuple(f) for f in flashes]


# --- pages -------------------------------------------------------------------------------------------------------------

def render_template(name, **context):
    """A page from app/templates, with what every page gets (the person, the workspace, the menu) and what's given."""
    app = app_ctx.get().app
    template = app.jinja_env.get_or_select_template(name)
    app.update_template_context(context)
    return template.render(context)
