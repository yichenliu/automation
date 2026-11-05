"""Who may do what, keeping secrets secret, and the browser-side protections every response carries."""
import logging
import base64
import hashlib
import json
import time
from collections import defaultdict, deque
from functools import wraps

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import text

from .extensions import db
from .models import Membership, Workspace
from .core.settings import settings
from .webkit import abort, current_user, g, request

_log = logging.getLogger(__name__)


def _fernet():
    key = hashlib.sha256(("tokens:" + settings()["SECRET_KEY"]).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt(data):
    return _fernet().encrypt(json.dumps(data).encode()).decode()


def decrypt(token):
    if not token:
        return {}
    try:
        return json.loads(_fernet().decrypt(token.encode()))
    except (InvalidToken, ValueError):
        _log.error("A stored sign-in couldn't be decrypted (was the secret key in the data folder replaced?)")
        return {}


STEP_UP_SECONDS = 10 * 60  # how long a confirmed sign-in counts for steps that need one


def stepped_up():
    """Whether the person confirmed their sign-in in the last ten minutes (approving a payment run, a bank change)."""
    from .webkit import session
    at = session.get("stepped_up_at")
    return bool(at) and time.time() - float(at) < STEP_UP_SECONDS


def step_up(user, code):
    """Confirm it's them: the code from their authenticator app when two-step sign-in is on, else their password.
    Returns whether it was right (and if so, it counts for the next ten minutes)."""
    from .webkit import session

    from . import totp
    code = (code or "").strip()
    if not code or user is None:
        return False
    secret = decrypt(user.totp_secret) if user.totp_secret else None
    ok = totp.verify(secret, code) if isinstance(secret, str) else user.check_password(code)
    if ok:
        session["stepped_up_at"] = time.time()
    return ok


def membership(ws, user=None):
    user = user or current_user
    if not getattr(user, "is_authenticated", False):
        return None
    return Membership.query.filter_by(workspace_id=ws.id, user_id=user.id).first()


def workspace(wid, role="viewer"):
    """The workspace, if the signed-in person belongs to it with at least `role` (404 if they don't belong at all, so
    other people's workspaces can't even be confirmed to exist; 403 if their role is too low)."""
    ws = db.session.get(Workspace, wid)
    member = membership(ws) if ws else None
    if member is None:
        abort(404)
    _signed_out_if_idle(ws)
    if not member.can(role):
        abort(403)
    g.ws, g.member = ws, member
    return ws


IDLE_CHOICES = (0, 15, 30, 60, 240)  # minutes; 0: never


def _signed_out_if_idle(ws):
    """Shared computers on a shop floor: after the minutes the business chose without anyone using the app, the next
    click asks for the password again."""
    import time
    from .webkit import flash, logout_user, redirect, request, session, url_for
    minutes = int((ws.settings or {}).get("idle_minutes") or 0)
    now, last = time.time(), session.get("active_at")
    if minutes and last and now - last > minutes * 60:
        from . import audit
        audit.log(ws.id, "signed_out", f"after {minutes} minutes without use")
        logout_user()
        session.pop("active_at", None)
        flash(f"You were signed out after {minutes} minutes without use. Sign in again to carry on.", "info")
        abort(redirect(url_for("auth.login", next=request.full_path if request.method == "GET" else None)))
    session["active_at"] = now


def role_required(role):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            workspace(kwargs["wid"], role)
            return view(*args, **kwargs)
        return wrapped
    return decorator


class Throttle:
    """At most `limit` attempts per key in `window` seconds (sign-ins, public form posts, API calls).

    With several web processes the attempts are counted in PostgreSQL (RateHit), so a limit is the same limit whichever
    process a request reaches: each attempt adds one to the count of the slice of time it falls in (SLICES of them per
    window), and the slices of the last window are added up. Under TESTING, and on anything but PostgreSQL (one process),
    they're counted in memory (`hits`), as they always were. `name` keeps each limit's counts apart (by default its
    numbers, so two limits with the same numbers share them)."""

    SLICES = 30

    def __init__(self, limit, window, name=None, shared=None):
        self.limit, self.window, self.hits = limit, window, defaultdict(deque)
        self.name = name or f"{limit}/{window}"
        self.shared = shared  # None: in PostgreSQL unless testing; True/False: always or never
        self.step = max(1, int(window) // self.SLICES)

    # --- in memory ------------------------------------------------------------------------------------------------
    def _recent(self, key):
        now, hits = time.monotonic(), self.hits[key]
        while hits and now - hits[0] > self.window:
            hits.popleft()
        return hits

    # --- in PostgreSQL -------------------------------------------------------------------------------------------
    def _in_database(self):
        if self.shared is not None:
            return self.shared
        try:
            return not settings().get("TESTING") and db.engine.dialect.name == "postgresql"
        except RuntimeError:  # outside the app
            return False

    def _key(self, key):
        return hashlib.sha256(f"{self.name}|{key}".encode()).hexdigest()[:64]

    def _count(self, conn, key):
        oldest = int(time.time()) - self.window
        return conn.execute(text("SELECT COALESCE(SUM(count), 0) FROM rate_hit WHERE key = :k AND bucket > :oldest"),
                            {"k": self._key(key), "oldest": oldest - self.step}).scalar() or 0

    def _add(self, conn, key):
        now = int(time.time())
        conn.execute(text("INSERT INTO rate_hit (key, bucket, count) VALUES (:k, :b, 1) "
                          "ON CONFLICT (key, bucket) DO UPDATE SET count = rate_hit.count + 1"),
                     {"k": self._key(key), "b": now - now % self.step})

    def _shared(self, fn, key):
        """Counted on a connection of its own, so an attempt counts even when the request's own work is undone."""
        try:
            with db.engine.begin() as conn:
                return fn(conn, key)
        except Exception as exc:  # the database busy for a moment: this attempt is counted here instead
            _log.warning("Couldn't count an attempt for the %s limit in the database: %s", self.name, exc)
            return None

    # --- asked by the screens -----------------------------------------------------------------------------------
    def blocked(self, key):
        if self._in_database():
            count = self._shared(self._count, key)
            if count is not None:
                return count >= self.limit
        return len(self._recent(key)) >= self.limit

    def hit(self, key):
        if self._in_database() and self._shared(lambda conn, k: self._add(conn, k) or True, key):
            return
        self._recent(key).append(time.monotonic())

    def allow(self, key):
        """Count this attempt; False once there have been `limit` within the window."""
        if self.blocked(key):
            return False
        self.hit(key)
        return True


sign_ins = Throttle(10, 900, name="sign_ins")
form_posts = Throttle(30, 3600, name="form_posts")


def forget_old_hits(older_than=86400):
    """Counts older than any limit's window are let go (the scheduler does it every hour)."""
    if db.engine.dialect.name != "postgresql":
        return 0
    with db.engine.begin() as conn:
        return conn.execute(text("DELETE FROM rate_hit WHERE bucket < :t"), {"t": int(time.time()) - older_than}).rowcount


def client_ip():
    """Who's connecting. X-Forwarded-For is only believed when the platform runs behind a proxy that sets it
    (TRUST_PROXY=1); otherwise anyone could send a new one each time and dodge the sign-in limits."""
    if settings().get("TRUST_PROXY"):
        return request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
    return request.remote_addr or ""


def headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    if response.mimetype != "text/html":  # a PDF or image opened in the browser needs its own viewer
        return response
    response.headers.setdefault("Content-Security-Policy",
                                "default-src 'self'; img-src 'self' data: blob: https://tile.openstreetmap.org; media-src 'self' blob:; style-src 'self' 'unsafe-inline'; "
                                "script-src 'self'; frame-src 'self' https:; object-src 'none'; base-uri 'self'; frame-ancestors 'self'; "
                                # signing in to Microsoft 365 is a form whose redirect goes to Microsoft
                                "form-action 'self' https://login.microsoftonline.com")
    if request.path.startswith(("/w/", "/workspaces")):
        response.headers.setdefault("Cache-Control", "no-store")
    return response
