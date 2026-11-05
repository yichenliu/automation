"""The platform's settings, with no web framework: `settings()` is the mapping of the context the code runs in.

`using(mapping)` enters a context (a worker, a request, a script); the platform app's own context
(`app.app_context()`) enters its config, so the engine, the builder and the workers read their settings the same way
whatever runs them.
"""
import contextlib
import contextvars

_current = contextvars.ContextVar("keel_settings", default=None)


def settings():
    found = _current.get()
    if found is None:
        raise RuntimeError("No settings here: run inside the platform's context (core.settings.using, or an app context)")
    return found


@contextlib.contextmanager
def using(mapping):
    token = _current.set(mapping)
    try:
        yield mapping
    finally:
        _current.reset(token)
