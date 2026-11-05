"""The platform's settings, with no web framework: `settings()` is the mapping of the context the code runs in.

`using(mapping)` enters a context (a worker, a FastAPI request, a script). While Flask is still here, an app context's
config is the settings when no other context has been entered, so the engine, the builder and the workers read their
settings the same way whatever runs them.
"""
import contextlib
import contextvars

_current = contextvars.ContextVar("keel_settings", default=None)


def settings():
    found = _current.get()
    if found is not None:
        return found
    try:
        from flask import current_app
        return current_app.config
    except (ImportError, RuntimeError):
        raise RuntimeError("No settings here: run inside the platform's context (core.settings.using, or an app context)") from None


@contextlib.contextmanager
def using(mapping):
    token = _current.set(mapping)
    try:
        yield mapping
    finally:
        _current.reset(token)
