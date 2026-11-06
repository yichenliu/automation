"""The platform app the code runs in, with no web framework: `current_app` is the app of the context entered with
`entered(app)` (its config, its logger), however the code got there: a page, the API, a worker's job, a try-out copy.

The platform (app/platform.py) enters this in its context (`app.app_context()`, and each page's request), along with
its settings and its database (core.settings, core.database), so code that asks for "the app" gets the same one the
rest of the context runs against. Most of the core asks for what it needs instead (settings(), db); this is for the few
places that need the app itself, e.g. to open its context again in another thread.
"""
import contextlib
import contextvars

_app = contextvars.ContextVar("keel_app", default=None)


class _CurrentApp:
    """A stand-in for the app of the current context, looked up each time it's used."""

    __slots__ = ()

    def _get_current_object(self):
        app = _app.get()
        if app is None:
            raise RuntimeError("Working outside the platform's context: run inside app.app_context()")
        return app

    def __getattr__(self, name):
        return getattr(self._get_current_object(), name)

    def __bool__(self):
        return _app.get() is not None

    def __eq__(self, other):
        return _app.get() is other

    def __hash__(self):
        return id(self)

    def __repr__(self):
        app = _app.get()
        return f"<current_app {app!r}>" if app is not None else "<current_app: none>"


current_app = _CurrentApp()


def app():
    """The app of this context, or None."""
    return _app.get()


@contextlib.contextmanager
def entered(app_):
    token = _app.set(app_)
    try:
        yield app_
    finally:
        _app.reset(token)
