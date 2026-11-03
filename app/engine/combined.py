"""Combined lists: one list put together from several (a stock outlook from what's moved, what's on order and what's
promised; every money movement from sales, refunds and expenses), kept up to date by the platform.

Its records are made from the lists it combines ("combines": [{"table", "where", "values"}], schema._combine_arm): one
for each source record that meets "where", with "values" worked out from it ({{record.quantity}}, a fixed "Planned
in", a sum). As a source record is added, changed, deleted or brought back, its combined record follows. People read a
combined list like any other (views, filters, charts, totals, exports, the API) but never change it by hand: they change
the lists it comes from. Each combined record remembers where it came from ("_from": "<arm>:<table>:<id>").
"""
from flask import current_app

from ..extensions import db
from ..models import Record
from .records import FieldErrors

FROM = "_from"


def arms_for(bp, table):
    """[(the combined table, the arm's number, the arm)] of every combined list that a table's records go into."""
    return [(t, n, arm) for t in (bp or {}).get("tables", []) if t.get("combines")
            for n, arm in enumerate(t["combines"]) if arm["table"] == table]


def sources(bp, t):
    """The labels of the lists a combined list is put together from."""
    names = {x["key"]: x["label"] for x in (bp or {}).get("tables", [])}
    return list(dict.fromkeys(names.get(a["table"], a["table"]) for a in t.get("combines") or []))


def _maker(engine):
    """An engine that may write a combined list: the platform itself, flagged as doing the combining."""
    from .records import Engine
    maker = Engine(engine.ws, by="put together by the platform")
    maker.combining = True
    return maker


def _one(ws_id, table, tag):
    return (Record.query.filter(Record.workspace_id == ws_id, Record.table == table, Record.deleted_at.is_(None),
                                Record.data[FROM].astext == tag).first())


def _values(engine, arm, record):
    from .automations import _render
    from .records import RecordContext, RecordProxy
    ctx = RecordContext(engine, record, {"record": RecordProxy(engine, record)})
    return {k: _render(v, ctx) for k, v in arm["values"].items()}


def sync(engine, record):
    """A source record was added, changed, deleted or brought back: its combined records follow it."""
    arms = arms_for(engine.bp, record.table)
    if not arms:
        return
    maker = _maker(engine)
    for t, n, arm in arms:
        tag = f"{n}:{record.table}:{record.id}"
        existing = _one(engine.ws.id, t["key"], tag)
        wanted = record.deleted_at is None and (not arm.get("where") or maker.matches(record, arm["where"]))
        try:
            if wanted:
                _put(maker, t, tag, _values(maker, arm, record), existing)
            elif existing is not None:
                maker.delete(existing, automations=True)
        except FieldErrors as exc:  # one that doesn't fit the combined list is left out of it, never stops the change itself
            current_app.logger.warning("Left %s %s out of %s: %s", record.table, record.id, t["key"], exc.errors)
    db.session.commit()


def _put(maker, t, tag, values, existing):
    values = {k: v for k, v in values.items() if v not in (None, "")}
    if existing is not None:
        cleared = {f["key"]: None for f in t["fields"] if f["key"] in (existing.data or {}) and f["key"] not in values
                   and f["key"] in {k for arm in t["combines"] for k in arm["values"]}}
        maker.update(existing, {**cleared, **values})
        return existing
    made = maker.create(t["key"], values)
    made.data = {**(made.data or {}), FROM: tag}
    db.session.flush()
    return made


def rebuild(engine, t, rules=False):
    """Put a combined list together again from scratch (made, or its make-up changed). Returns how many it holds.
    `rules`: whether its own rules run for each (not on a rebuild: nothing new happened to the business)."""
    maker = _maker(engine)
    for r in Record.query.filter_by(workspace_id=engine.ws.id, table=t["key"], deleted_at=None).all():
        maker.delete(r, automations=False)
    n = 0
    for i, arm in enumerate(t.get("combines") or []):
        for record in Record.query.filter_by(workspace_id=engine.ws.id, table=arm["table"], deleted_at=None).order_by(Record.id).all():
            if arm.get("where") and not maker.matches(record, arm["where"]):
                continue
            values = {k: v for k, v in _values(maker, arm, record).items() if v not in (None, "")}
            try:
                made = maker.create(t["key"], values, automations=rules)
            except FieldErrors:
                continue  # it doesn't fit: left out, as when it's synced
            made.data = {**(made.data or {}), FROM: f"{i}:{arm['table']}:{record.id}"}
            n += 1
    db.session.commit()
    return n


def changed(before, after):
    """The combined tables of `after` that are new, or put together differently from `before`."""
    was = {t["key"]: (t.get("combines"), [f["key"] for f in t["fields"]]) for t in (before or {}).get("tables", [])}
    return [t for t in (after or {}).get("tables", []) if t.get("combines") and was.get(t["key"]) != (t["combines"], [f["key"] for f in t["fields"]])]
