"""The audit log: who did what that matters, across the whole app (signing in, people and their roles, exports and
backups, private values revealed, API keys, changes to how the app works, printers). Each entry carries a fingerprint
of the one before it, so an entry changed or taken out afterwards shows: `verify` says whether the chain holds.

Records keep their own history (every change to each field); this is the rest."""
import logging
import hashlib
import json


from .extensions import db
from .models import AuditEvent, utcnow
from .core import actor

_log = logging.getLogger(__name__)

WORDS = {  # what each kind of entry is, for people reading the log
    "signed_in": "Signed in", "sign_in_failed": "Sign-in refused", "signed_out": "Signed out", "password_reset": "Password reset",
    "two_step": "Two-step sign-in changed", "sign_in_method": "Sign-in method changed", "invited": "Invited someone", "joined": "Joined", "role_changed": "Role changed",
    "teams_changed": "Teams changed", "removed": "Removed someone", "exported": "Exported", "backup": "Backup",
    "revealed": "Revealed a private value", "api_key": "API key", "design": "Changed how the app works", "settings": "Settings changed",
    "printers": "Printers changed", "deleted": "Deleted", "restored": "Restored", "connected": "Connection",
}


def log(workspace_id, action, detail="", user=None, by=None):
    """Add an entry, chained to the last one of its workspace (or of the platform's own, for sign-ins before one is
    chosen). Never fails the thing being done: a log that can't be written is itself logged by the app."""
    try:
        who = user if user is not None else actor.user()
        last = AuditEvent.query.filter_by(workspace_id=workspace_id).order_by(AuditEvent.id.desc()).first()
        entry = AuditEvent(workspace_id=workspace_id, user_id=getattr(who, "id", None), at=utcnow(),
                           by=(by or getattr(who, "display", None) or "the app")[:200], action=action[:32], detail=str(detail or "")[:1000],
                           ip=actor.ip())
        entry.previous = last.fingerprint if last else ""
        entry.fingerprint = _fingerprint(entry)
        db.session.add(entry)
        db.session.commit()
        return entry
    except Exception:  # noqa: BLE001
        db.session.rollback()
        _log.exception("Couldn't write to the audit log: %s %s", action, detail)
        return None


def _fingerprint(e):
    body = json.dumps([e.previous, e.workspace_id, e.user_id, e.by, e.action, e.detail, e.ip, e.at.isoformat(timespec="seconds")],
                      ensure_ascii=False)
    return hashlib.sha256(body.encode()).hexdigest()


def verify(workspace_id):
    """(True, n) if every entry is as it was written and none is missing between them; (False, the first one that
    isn't) otherwise."""
    previous, n = "", 0
    for e in AuditEvent.query.filter_by(workspace_id=workspace_id).order_by(AuditEvent.id).yield_per(500):
        if e.previous != previous or e.fingerprint != _fingerprint(e):
            return False, e
        previous, n = e.fingerprint, n + 1
    return True, n
