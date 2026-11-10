"""How long a list's records are kept (its "keep_for": {"years": 7, "from": "invoice_date", "unless": [a legal hold]}).

Once a record's time is up it's deleted, as anyone's delete is (it can be brought back for a while), unless one of the
"unless" conditions holds: a legal hold on it, an open dispute. A record of such a list deleted more than ERASE_AFTER
ago (by the retention or by anyone) is then erased for good: its values, its files and its comments; its history keeps
one line saying it was erased and when, nothing of what it held. Lists without keep_for keep what's deleted, as before.
The scheduler runs this for every app once an hour (app/scheduler.py: retention)."""
import logging
from datetime import date, datetime, timedelta

from ..blueprint import expr, schema
from ..extensions import db
from ..models import Comment, File, Record, RecordChange, utcnow

log = logging.getLogger("retention")
ERASE_AFTER = timedelta(days=30)
BY = "Data retention"


def _amount(engine, record, value):
    """A length of keep_for: a number, or a formula worked out for the record (none when it can't be: kept)."""
    if not isinstance(value, str):
        return int(value or 0)
    from .records import RecordContext
    try:
        n = expr.to_number(expr.evaluate(value, RecordContext(engine, record)))
    except expr.ExprError:
        return None
    return int(n) if n is not None else None


def _ends(keep, start, engine=None, record=None):
    """The day a record's time is up, counted from its start date (None: not known, so it's kept)."""
    if start is None:
        return None
    years, months, days = (_amount(engine, record, keep.get(u)) for u in ("years", "months", "days"))
    if None in (years, months, days):
        return None
    end = expr.FUNCTIONS["add_months"](start, years * 12 + months) if years * 12 + months else start
    end = expr.to_date(end) if not isinstance(end, date) else end
    return end + timedelta(days=days) if end else None


def _start(engine, record, keep):
    since = keep.get("from") or "created_at"
    if since in ("created_at", "updated_at"):
        when = getattr(record, since, None)
        return when.date() if isinstance(when, datetime) else when
    return expr.to_date((record.data or {}).get(since))


def due(engine, t):
    """The records of a list whose time is up and that nothing keeps (a legal hold)."""
    keep = t.get("keep_for")
    if not keep:
        return []
    out = []
    for r in engine.rows(t["key"]):
        end = _ends(keep, _start(engine, r, keep), engine, r)
        if end is None or end > engine.today:
            continue
        if keep.get("unless") and any(engine.matches(r, [c]) for c in keep["unless"]):
            continue
        out.append(r)
    return out


ERASED = "erased"  # where an erased record's empty row is kept (its history still points at it)


def erase(engine, record):
    """A deleted record erased for good: its values, files and comments, and what its history held; the history keeps
    one line saying it was, and the row stays, empty, under ERASED (so nothing can bring it back)."""
    from ..storage import backend
    for f in File.query.filter_by(record_id=record.id).all():
        try:
            backend().delete(f.path)
        except Exception:  # noqa: BLE001 - the row goes all the same: nothing points at the file any more
            log.warning("Couldn't remove the stored file %s", f.path)
        db.session.delete(f)
    Comment.query.filter_by(record_id=record.id).delete(synchronize_session=False)
    for change in RecordChange.query.filter_by(record_id=record.id).all():
        change.changes = None  # what it held, gone; that something changed and when, kept
    t = engine.tables.get(record.table) or {}
    db.session.add(RecordChange(workspace_id=engine.ws.id, record_id=record.id, kind="erased", by=BY,
                                changes={"_": [None, f"{t.get('singular') or 'record'} erased: its time to be kept was up"]}))
    record.data, record.table = {"erased_from": record.table, "erased_at": utcnow().isoformat()}, ERASED


def run(ws, now=None):
    """Delete what's past its time in one app, and erase what was deleted long enough ago. Returns (deleted, erased)."""
    from .records import Engine
    engine = Engine(ws, by=BY)
    deleted = erased = 0
    kept_lists = [t for t in engine.tables.values() if t.get("keep_for")]
    for t in kept_lists:
        for r in due(engine, t):
            engine.delete(r, by=BY, automations=False)
            deleted += 1
    cutoff = (now or utcnow()) - ERASE_AFTER
    for t in kept_lists:
        for r in Record.query.filter(Record.workspace_id == ws.id, Record.table == t["key"], Record.deleted_at.isnot(None),
                                     Record.deleted_at < cutoff).limit(500).all():
            erase(engine, r)
            erased += 1
    db.session.commit()
    if deleted or erased:
        from .. import audit
        audit.log(ws.id, "retention", f"deleted {deleted} record{'s' if deleted != 1 else ''} past their time; erased {erased} "
                                      f"deleted more than {ERASE_AFTER.days} days ago")
    return deleted, erased
