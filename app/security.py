"""Who may do what, keeping secrets secret, and the browser-side protections every response carries."""
import base64
import hashlib
import json
import time
from collections import defaultdict, deque
from functools import wraps

from cryptography.fernet import Fernet, InvalidToken
from flask import abort, current_app, g, request
from flask_login import current_user

from .extensions import db
from .models import Membership, Workspace


def _fernet():
    key = hashlib.sha256(("tokens:" + current_app.config["SECRET_KEY"]).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt(data):
    return _fernet().encrypt(json.dumps(data).encode()).decode()


def decrypt(token):
    if not token:
        return {}
    try:
        return json.loads(_fernet().decrypt(token.encode()))
    except (InvalidToken, ValueError):
        current_app.logger.error("A stored sign-in couldn't be decrypted (was the secret key in the data folder replaced?)")
        return {}


STEP_UP_SECONDS = 10 * 60  # how long a confirmed sign-in counts for steps that need one


def stepped_up():
    """Whether the person confirmed their sign-in in the last ten minutes (approving a payment run, a bank change)."""
    from flask import session
    at = session.get("stepped_up_at")
    return bool(at) and time.time() - float(at) < STEP_UP_SECONDS


def step_up(user, code):
    """Confirm it's them: the code from their authenticator app when two-step sign-in is on, else their password.
    Returns whether it was right (and if so, it counts for the next ten minutes)."""
    from flask import session

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
    if not member.can(role):
        abort(403)
    g.ws, g.member = ws, member
    return ws


def role_required(role):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            workspace(kwargs["wid"], role)
            return view(*args, **kwargs)
        return wrapped
    return decorator


class Throttle:
    """At most `limit` attempts per key in `window` seconds (sign-ins, public form posts)."""

    def __init__(self, limit, window):
        self.limit, self.window, self.hits = limit, window, defaultdict(deque)

    def _recent(self, key):
        now, hits = time.monotonic(), self.hits[key]
        while hits and now - hits[0] > self.window:
            hits.popleft()
        return hits

    def blocked(self, key):
        return len(self._recent(key)) >= self.limit

    def hit(self, key):
        self._recent(key).append(time.monotonic())

    def allow(self, key):
        """Count this attempt; False once there have been `limit` within the window."""
        if self.blocked(key):
            return False
        self.hit(key)
        return True


sign_ins = Throttle(10, 900)
form_posts = Throttle(30, 3600)


def client_ip():
    """Who's connecting. X-Forwarded-For is only believed when the platform runs behind a proxy that sets it
    (TRUST_PROXY=1); otherwise anyone could send a new one each time and dodge the sign-in limits."""
    from flask import current_app
    if current_app.config.get("TRUST_PROXY"):
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
