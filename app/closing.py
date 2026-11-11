"""Deleting a whole app, for its owner: everything it keeps (its design and versions, records and their history, files,
people's memberships and invitations, rules' runs, approvals, messages, keys, connections) goes for good. The audit log
keeps its lines (it's chained: nothing is taken out of it), one of them saying the app was deleted, by whom."""
import logging

from sqlalchemy import delete

from .extensions import db

log = logging.getLogger("closing")
KEPT = {"audit_event"}  # the audit log's chain stays whole


def delete_app(ws, by):
    """Delete the app `ws` and everything in it. `by`: the owner's name, for the audit log."""
    from . import audit
    from .models import File, Workspace
    from .storage import backend
    wid, name = ws.id, ws.name
    audit.log(wid, "app_deleted", f"the app “{name}” and everything in it was deleted by {by}")
    for f in File.query.filter_by(workspace_id=wid).all():
        try:
            backend().delete(f.path)
        except Exception:  # noqa: BLE001 - its row goes all the same
            log.warning("Couldn't remove the stored file %s", f.path)
    for table in reversed(db.metadata.sorted_tables):  # what points at a row goes before the row it points at
        if table.name in KEPT or table.name == "workspace" or "workspace_id" not in table.c:
            continue
        db.session.execute(delete(table).where(table.c.workspace_id == wid))
    db.session.delete(db.session.get(Workspace, wid))
    db.session.commit()
