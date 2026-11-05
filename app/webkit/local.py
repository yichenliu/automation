"""Stand-ins for what belongs to the request being served (request, session, g, the signed-in person): a module can
import `request` once, and each use finds the request of the context it's used in. Context variables keep requests
served at the same time, in different threads, apart."""
import contextvars

app_ctx = contextvars.ContextVar("keel_web_app_ctx", default=None)  # the AppContext pushed (app.app_context())
request_ctx = contextvars.ContextVar("keel_web_request_ctx", default=None)  # the RequestContext of the request served


def _app_ctx():
    ctx = app_ctx.get()
    if ctx is None:
        raise RuntimeError("Working outside the platform's context: run inside app.app_context()")
    return ctx


def _request_ctx():
    ctx = request_ctx.get()
    if ctx is None:
        raise RuntimeError("Working outside of a request: this needs a page's request (or app.test_request_context())")
    return ctx


def has_app_context():
    return app_ctx.get() is not None


def has_request_context():
    return request_ctx.get() is not None


class Proxy:
    """Behaves as the object `lookup()` returns now: attributes, items, comparisons, truth, iteration and isinstance."""

    __slots__ = ("_lookup",)

    def __init__(self, lookup):
        object.__setattr__(self, "_lookup", lookup)

    def _get_current_object(self):
        return self._lookup()

    @property
    def __class__(self):  # isinstance(proxy, Thing) is about what it stands for
        try:
            return type(self._lookup())
        except RuntimeError:
            return Proxy

    def __getattr__(self, name):
        return getattr(self._lookup(), name)

    def __setattr__(self, name, value):
        setattr(self._lookup(), name, value)

    def __delattr__(self, name):
        delattr(self._lookup(), name)

    def __getitem__(self, key):
        return self._lookup()[key]

    def __setitem__(self, key, value):
        self._lookup()[key] = value

    def __delitem__(self, key):
        del self._lookup()[key]

    def __contains__(self, item):
        return item in self._lookup()

    def __iter__(self):
        return iter(self._lookup())

    def __len__(self):
        return len(self._lookup())

    def __bool__(self):
        try:
            return bool(self._lookup())
        except RuntimeError:
            return False

    def __eq__(self, other):
        return self._lookup() == other

    def __ne__(self, other):
        return self._lookup() != other

    def __hash__(self):
        return hash(self._lookup())

    def __call__(self, *args, **kwargs):
        return self._lookup()(*args, **kwargs)

    def __str__(self):
        return str(self._lookup())

    def __repr__(self):
        try:
            return repr(self._lookup())
        except RuntimeError:
            return "<unbound proxy>"
