"""Who is acting, from where, and in which app, in the context the code runs in: set by whatever serves it (a page, the
API, MCP, a worker doing a job), read by the core (the audit log, who uploaded a file, whose turn it is with the AI).

    with acting(user=..., ip=..., workspace_id=...):
        ...

A page's request learns who it is for as it goes: the signed-in person once the session is read, the workspace only
once the page has looked it up. So the platform app's context enters `reading(source)` instead, and `source(name)` is
asked each time ("user", "ip" or "workspace_id"); an `acting(...)` inside it says it outright and comes first.
"""
import contextlib
import contextvars

_actor = contextvars.ContextVar("keel_actor", default=None)  # a dict said outright, or a source asked as it goes


@contextlib.contextmanager
def acting(user=None, ip=None, workspace_id=None):
    token = _actor.set({"user": user, "ip": ip, "workspace_id": workspace_id})
    try:
        yield
    finally:
        _actor.reset(token)


@contextlib.contextmanager
def reading(source):
    """Who is acting is asked of `source(name)` while the block runs (a request, which knows more as it goes)."""
    token = _actor.set(source)
    try:
        yield
    finally:
        _actor.reset(token)


def _identity(obj):
    """A stored object's id without touching the database (it may be asked while a session is starting a transaction)."""
    from sqlalchemy import inspect
    state = inspect(obj, raiseerr=False)
    key = state.identity if state is not None else None
    return key[0] if key else obj.__dict__.get("id")


def _get(name):
    now = _actor.get()
    if now is None:
        return None
    return now.get(name) if isinstance(now, dict) else now(name)


def user():
    """The person acting, or None (a rule, a worker, an API key)."""
    return _get("user")


def user_id():
    who = user()
    return getattr(who, "id", None)


def ip():
    return _get("ip")


def workspace_id():
    return _get("workspace_id")
