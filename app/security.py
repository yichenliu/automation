"""Who may do what, keeping secrets secret, and the browser-side protections every response carries."""
import logging
import base64
import hashlib
import json
import time
from collections import defaultdict, deque

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException
from sqlalchemy import text

from .extensions import db
from .models import Membership, Workspace
from .core.settings import settings
from .web.http import Redirect, current_request, current_user, flash, full_path, logout_user, url_for

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


def platform_admin(user):
    """Whether this person looks after the platform itself, not one business on it: they choose the AI every business
    here uses (Settings › AI). The emails in PLATFORM_ADMINS; when that's empty, whoever made the first account (on one
    business's own computer, its owner)."""
    if not getattr(user, "is_authenticated", False):
        return False
    named = {e.strip().lower() for e in (settings().get("PLATFORM_ADMINS") or "").split(",") if e.strip()}
    if named:
        return (user.email or "").strip().lower() in named
    from sqlalchemy import func
    from .models import User
    return user.id == db.session.query(func.min(User.id)).scalar()


STEP_UP_SECONDS = 10 * 60  # how long a confirmed sign-in counts for steps that need one


def stepped_up(request=None):
    """Whether the person confirmed their sign-in in the last ten minutes (approving a payment run, a bank change)."""
    at = (request or current_request()).state.session.get("stepped_up_at")
    return bool(at) and time.time() - float(at) < STEP_UP_SECONDS


def step_up(user, code, request=None):
    """Confirm it's them: the code from their authenticator app when two-step sign-in is on, else their password.
    Returns whether it was right (and if so, it counts for the next ten minutes)."""
    from . import totp
    session = (request or current_request()).state.session
    code = (code or "").strip()
    if not code or user is None:
        return False
    secret = decrypt(user.totp_secret) if user.totp_secret else None
    ok = totp.verify(secret, code) if isinstance(secret, str) else user.check_password(code)
    if ok:
        session["stepped_up_at"] = time.time()
    return ok


def membership(ws, user=None):
    if user is None:
        request = current_request()
        user = current_user(request) if request is not None else None
    if not getattr(user, "is_authenticated", False):
        return None
    return Membership.query.filter_by(workspace_id=ws.id, user_id=user.id).first()


def workspace(wid, role="viewer", request=None):
    """The workspace, if the signed-in person belongs to it with at least `role` (404 if they don't belong at all, so
    other people's workspaces can't even be confirmed to exist; 403 if their role is too low). It's kept for the rest of
    the request as request.state.ws (and the person's membership as request.state.member)."""
    request = request or current_request()
    ws = db.session.get(Workspace, wid)
    member = membership(ws, current_user(request)) if ws else None
    if member is None:
        raise HTTPException(404)
    _signed_out_if_idle(request, ws)
    if not member.can(role):
        raise HTTPException(403)
    if two_step_needed(ws, current_user(request)):
        flash(request, f"{ws.name} asks everyone to use two-step sign-in: turn it on here, then carry on.", "info")
        raise Redirect(url_for("account.page"))
    request.state.ws, request.state.member = ws, member
    return ws


def two_step_needed(ws, user):
    """The business asks everyone to sign in with a second step, and this person hasn't one yet (single sign-on brings
    its own)."""
    if not (ws.settings or {}).get("require_two_step") or user is None:
        return False
    return not getattr(user, "totp_secret", None) and not getattr(user, "sso_identity", None)


IDLE_CHOICES = (0, 15, 30, 60, 240)  # minutes; 0: never


def _signed_out_if_idle(request, ws):
    """Shared computers on a shop floor: after the minutes the business chose without anyone using the app, the next
    click asks for the password again."""
    import time
    session = request.state.session
    minutes = int((ws.settings or {}).get("idle_minutes") or 0)
    now, last = time.time(), session.get("active_at")
    if minutes and last and now - last > minutes * 60:
        from . import audit
        audit.log(ws.id, "signed_out", f"after {minutes} minutes without use")
        logout_user(request)
        session.pop("active_at", None)
        flash(request, f"You were signed out after {minutes} minutes without use. Sign in again to carry on.", "info")
        raise Redirect(url_for("auth.login", next=full_path(request) if request.method == "GET" else None))
    session["active_at"] = now


def member_of(role="viewer"):
    """A dependency for a page under /w/{wid:int}/...: the workspace, if the signed-in person may open it with at least
    `role` (as workspace(wid, role); request.state.ws and .member are set). Someone not signed in is sent to sign in
    first (signed_in), before the workspace is looked up.

        def settings_page(request: Request, wid: int, ws=Depends(member_of("admin"))): ..."""
    from fastapi import Depends, Request

    from .web.http import signed_in

    def dependency(request: Request, wid: int, user=Depends(signed_in)):
        return workspace(wid, role, request)
    return dependency


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


def client_ip(request=None):
    """Who's connecting. X-Forwarded-For is only believed when the platform runs behind a proxy that sets it
    (TRUST_PROXY=1); otherwise anyone could send a new one each time and dodge the sign-in limits."""
    request = request or current_request()
    address = request.client.host if request.client else ""
    if settings().get("TRUST_PROXY"):
        return request.headers.get("X-Forwarded-For", address or "").split(",")[0].strip()
    return address or ""


CSP = ("default-src 'self'; img-src 'self' data: blob: https://tile.openstreetmap.org; media-src 'self' blob:; style-src 'self' 'unsafe-inline'; "
       "script-src 'self'; frame-src 'self' https:; object-src 'none'; base-uri 'self'; frame-ancestors 'self'; "
       # signing in to Microsoft 365 is a form whose redirect goes to Microsoft
       "form-action 'self' https://login.microsoftonline.com")


def page_headers(path, mimetype):
    """The browser-side protections every response carries, as (name, value) to set where the view didn't: a page
    (text/html) also gets the content security policy, and a workspace's pages aren't kept in the browser's cache."""
    out = [("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "same-origin"), ("X-Frame-Options", "SAMEORIGIN")]
    if mimetype != "text/html":  # a PDF or image opened in the browser needs its own viewer
        return out
    out.append(("Content-Security-Policy", CSP))
    if path.startswith(("/w/", "/workspaces")):
        out.append(("Cache-Control", "no-store"))
    return out
