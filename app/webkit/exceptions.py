"""HTTP errors a view can raise (`abort(404)`), each with the plain page it shows when the app has no page of its own
for it. The wording is the one the platform has always shown, so nothing changes for the people who see them."""
from http import HTTPStatus

from markupsafe import Markup, escape

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


class HTTPException(Exception):
    """An answer other than the page: a status, what went wrong, and (for `abort(response)`) the response itself."""

    code = None
    description = None

    def __init__(self, description=None, response=None):
        super().__init__()
        if description is not None:
            self.description = description
        self.response = response

    @property
    def name(self):
        try:
            return HTTPStatus(self.code).phrase
        except ValueError:
            return "Unknown Error"

    def get_headers(self):
        return [("Content-Type", "text/html; charset=utf-8")]

    def get_body(self):
        text = escape(self.description or "").replace("\n", Markup("<br>"))
        return ("<!doctype html>\n<html lang=en>\n"
                f"<title>{self.code} {escape(self.name)}</title>\n"
                f"<h1>{escape(self.name)}</h1>\n"
                f"<p>{text}</p>\n")

    def get_response(self):
        if self.response is not None:
            return self.response
        from .wrappers import Response
        return Response(self.get_body(), self.code, headers=self.get_headers())

    def __str__(self):
        code = self.code if self.code is not None else "???"
        return f"{code} {self.name}: {self.description}"

    def __repr__(self):
        return f"<{type(self).__name__} '{self}'>"


def _make(code):
    return type(HTTPStatus(code).phrase.replace(" ", "").replace("-", ""), (HTTPException,),
                {"code": code, "description": DESCRIPTIONS.get(code, "")})


class BadRequest(HTTPException):
    code, description = 400, DESCRIPTIONS[400]


class BadRequestKeyError(BadRequest, KeyError):
    """A form or query value the view needed wasn't sent (`request.form["x"]`): 400, and still a KeyError."""

    def __init__(self, key=None):
        BadRequest.__init__(self)
        KeyError.__init__(self, key)

    def __str__(self):
        return BadRequest.__str__(self)


class Unauthorized(HTTPException):
    code, description = 401, DESCRIPTIONS[401]


class Forbidden(HTTPException):
    code, description = 403, DESCRIPTIONS[403]


class NotFound(HTTPException):
    code, description = 404, DESCRIPTIONS[404]


class MethodNotAllowed(HTTPException):
    code, description = 405, DESCRIPTIONS[405]

    def __init__(self, valid_methods=None, description=None, response=None):
        super().__init__(description, response)
        self.valid_methods = valid_methods

    def get_headers(self):
        headers = super().get_headers()
        if self.valid_methods:
            headers.append(("Allow", ", ".join(self.valid_methods)))
        return headers


class RequestEntityTooLarge(HTTPException):
    code, description = 413, DESCRIPTIONS[413]


class UnsupportedMediaType(HTTPException):
    code, description = 415, DESCRIPTIONS[415]


class InternalServerError(HTTPException):
    code, description = 500, DESCRIPTIONS[500]

    def __init__(self, description=None, response=None, original_exception=None):
        super().__init__(description, response)
        self.original_exception = original_exception


class RequestRedirect(HTTPException):
    """The address without its closing slash: sent on to the one with it (308 keeps the method and the form)."""

    code = 308

    def __init__(self, new_url):
        super().__init__()
        self.new_url = new_url

    def get_response(self):
        from .helpers import redirect
        return redirect(self.new_url, self.code)


_BY_CODE = {cls.code: cls for cls in (BadRequest, Unauthorized, Forbidden, NotFound, MethodNotAllowed, RequestEntityTooLarge,
                                      UnsupportedMediaType, InternalServerError)}


def for_code(code):
    """The exception class for a status (made the first time for one without its own class)."""
    if code not in _BY_CODE:
        _BY_CODE[code] = _make(code)
    return _BY_CODE[code]


def abort(status, *args, **kwargs):
    """Stop the view here: `abort(404)`, `abort(403, "why")`, or `abort(response)` to answer with that response as it is."""
    if not isinstance(status, int):
        raise HTTPException(response=status)
    raise for_code(status)(*args, **kwargs)
