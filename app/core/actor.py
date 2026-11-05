"""Who is acting, from where, and in which app, in the context the code runs in: set by whatever serves it (a page, the
API, MCP, a worker doing a job), read by the core (the audit log, who uploaded a file, whose turn it is with the AI).

    with acting(user=..., ip=..., workspace_id=...):
        ...

While Flask is still here, a Flask request is read when nothing has been set: its signed-in person, its address and
its workspace. That fallback is the only place the core reads Flask's request, and it goes with Flask.
"""
import contextlib
import contextvars

_actor = contextvars.ContextVar("keel_actor", default=None)


@contextlib.contextmanager
def acting(user=None, ip=None, workspace_id=None):
    token = _actor.set({"user": user, "ip": ip, "workspace_id": workspace_id})
    try:
        yield
    finally:
        _actor.reset(token)


def _from_flask():
    try:
        from flask import g, has_app_context, has_request_context, request
    except ImportError:
        return {}
    out = {}
    if has_request_context():
        try:
            from flask_login import current_user
            out["user"] = current_user if current_user and current_user.is_authenticated else None
        except Exception:  # noqa: BLE001 - no sign-in here (an API call, a form)
            out["user"] = None
        out["ip"] = (request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()[:64]) or None
    if has_app_context():
        ws = g.get("ws") or getattr(g.get("engine"), "ws", None)
        if ws is not None:
            out["workspace_id"] = _identity(ws)
    return out


def _identity(obj):
    """A stored object's id without touching the database (it may be asked while a session is starting a transaction)."""
    from sqlalchemy import inspect
    state = inspect(obj, raiseerr=False)
    key = state.identity if state is not None else None
    return key[0] if key else obj.__dict__.get("id")


def _now():
    return _actor.get() or _from_flask()


def user():
    """The person acting, or None (a rule, a worker, an API key)."""
    return _now().get("user")


def user_id():
    who = user()
    return getattr(who, "id", None)


def ip():
    return _now().get("ip")


def workspace_id():
    return _now().get("workspace_id")
