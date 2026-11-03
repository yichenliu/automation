"""What people entered is kept when the app's design changes under it. Records keep their values under each field's key,
so a field that changes what it holds would otherwise lose them quietly, or worse, show the wrong thing:

- a link that now points to another list (a shift's volunteer, once a member, now one of the new Volunteers): each value
  becomes the record of that name in the new list, which is added (copying the details both lists have) when it isn't
  there yet. An id left as it was would show another volunteer, or none;
- a field that was worked out and is now kept (an invoice's customer, looked up from its job, now picked on the
  invoice): each record keeps what it showed, rather than starting empty;
- a field that changes kind (a link now a name typed in, a choice now text): the value as it was shown.

`keep` runs with every new version of the design (save_version) and returns what it did, in words, for the change list.
So does `repriced`: money on records people already have that the new design works out differently (a GST fix changing
a sent invoice's total) is kept in each record's history and said, so nothing already sent changes without anyone knowing.
"""
from types import SimpleNamespace

from ..blueprint import schema
from ..extensions import db
from ..models import Record, RecordChange
from .records import Engine, FieldErrors, to_number

COMPUTED = {"lookup", "formula", "rollup"}
BY = "the builder (keeping what was entered)"
WORKED_OUT = "the builder (worked out differently since the app changed)"
REPRICED = "Amounts changed on"  # how the change list's sentence starts (the builder's reply says it first)


def _as_was(workspace, blueprint):
    """An engine reading the records as the old design saw them."""
    return Engine(SimpleNamespace(id=workspace.id, blueprint=blueprint, timezone=workspace.timezone, currency=workspace.currency,
                                  settings=workspace.settings, name=workspace.name, about=getattr(workspace, "about", None)), by=BY)


def keep(workspace, old_bp, new_bp):
    """Carry each record's values across the change from old_bp to new_bp (already the workspace's design). Returns
    sentences for the change list."""
    old_tables = {t["key"]: t for t in (old_bp or {}).get("tables", [])}
    work = []
    for t in new_bp.get("tables", []):
        before = old_tables.get(t["key"])
        for f in t["fields"] if before else []:
            g = schema.field(before, f["key"])
            if g is None or f["type"] not in schema.STORED:
                continue
            if f["type"] == "link" and g["type"] == "link" and f.get("table") != g.get("table"):
                work.append((t, f, g, "relinked"))
            elif g["type"] in COMPUTED:
                work.append((t, f, g, "kept"))
            elif g["type"] != f["type"] and f["type"] in ("text", "long_text") and g["type"] in ("link", "user", "select", "multi_select"):
                work.append((t, f, g, "as text"))
    if not work:
        return []
    was, now = _as_was(workspace, old_bp), Engine(workspace, by=BY)
    said = []
    for t, f, g, how in work:
        rows = Record.query.filter_by(workspace_id=workspace.id, table=t["key"], deleted_at=None).all()
        moved, added, lost = 0, [], 0
        for r in rows:
            shown = was.value(r, g["key"])
            if shown in (None, "", []):
                continue
            if how == "relinked":
                value, made, ok = _relink(was, now, g, f, shown)
                added += made
                lost += not ok
            elif how == "kept":
                value = _fits(now, f, shown)
            else:
                value = was.display(g, shown)
            before_value = (r.data or {}).get(f["key"])
            if value in (None, "", []) or value == before_value:
                continue
            data = dict(r.data or {})
            data[f["key"]] = value
            r.data = data
            db.session.add(RecordChange(workspace_id=workspace.id, record_id=r.id, kind="updated", by=BY,
                                        changes={f["key"]: [before_value, value]}))
            moved += 1
        if moved or lost:
            many = t["label"].lower() if moved != 1 else t["singular"].lower()
            line = f"Kept {f['label']} on {moved} {many} through the change"
            if added:
                line += f" (added {_names(added)} to {now.tables[f['table']]['label']}, so they're still there)"
            if lost:
                line += f"; {lost} couldn't be matched and {'was' if lost == 1 else 'were'} left empty (undo the change to see them)"
            said.append(line)
    db.session.commit()
    return said


def repriced(workspace, old_bp, new_bp, most=500):
    """Money worked out on records people already have (totals, tax) that comes out differently with the new design:
    each change kept in the record's history, and said, those past their first stage (sent, part paid) named first."""
    old_tables = {t["key"]: t for t in (old_bp or {}).get("tables", [])}
    was, now, said = _as_was(workspace, old_bp), Engine(workspace, by=WORKED_OUT), []
    for t in new_bp.get("tables", []):
        before = old_tables.get(t["key"])
        money = [f for f in t["fields"] if before and f["type"] in COMPUTED and now.kind(f) == "money" and schema.field(before, f["key"])]
        if not money:
            continue
        flow, start = t.get("workflow"), schema.start_stage(t)
        moved = []
        for r in Record.query.filter_by(workspace_id=workspace.id, table=t["key"], deleted_at=None).order_by(Record.id.desc()).limit(most):
            diff = {}
            for f in money:
                try:
                    a, b = to_number(was.value(r, f["key"])), to_number(now.value(r, f["key"]))
                except Exception:  # noqa: BLE001 - a sum the old design couldn't work out: nothing to compare
                    continue
                if a is not None and b is not None and abs(a - b) > 0.005:
                    diff[f["key"]] = [round(a, 2), round(b, 2)]
            if diff:
                db.session.add(RecordChange(workspace_id=workspace.id, record_id=r.id, kind="updated", by=WORKED_OUT, changes=diff))
                moved.append((r, diff, bool(flow) and (r.data or {}).get(flow["field"]) not in (None, "", start)))
        if not moved:
            continue
        on = sorted(moved, key=lambda m: not m[2])
        shown = "; ".join(f"{now.title(r)}'s " + ", ".join(f"{schema.field(t, k)['label']} {now.display(schema.field(t, k), a)} → "
                                                             f"{now.display(schema.field(t, k), b)}" for k, (a, b) in diff.items())
                          for r, diff, _ in on[:3]) + (f" and {len(on) - 3} more" if len(on) > 3 else "")
        past = sum(1 for m in moved if m[2])
        line = f"{REPRICED} {len(moved)} {schema.lc(t['singular'] if len(moved) == 1 else t['label'])} you already have, as they're worked out differently now: {shown}"
        if past:
            line += (f" ({'it is' if past == 1 else f'{past} are'} past {start}, so check any copy already sent; the history of each "
                     f"shows the change)")
        said.append(line)
    db.session.commit()
    return said


def _relink(was, now, g, f, shown):
    """The records in f's list standing for what g linked to: found by name, or added. Returns (value, added names, all
    found)."""
    ids = shown if isinstance(shown, list) else [shown]
    out, added, ok = [], [], True
    target = now.tables.get(f["table"])
    for i in ids:
        old = was.get(int(i)) if str(i).isdigit() else None
        if old is None or target is None:
            ok = False
            continue
        name = was.title(old).strip().lower()
        found = next((x for x in now.rows(target["key"]) if now.title(x).strip().lower() == name
                      or _full(now, x) == _full(was, old)), None)
        if found is None:
            values = {k: v for k, v in (old.data or {}).items() if schema.field(target, k) and schema.field(target, k)["type"] in schema.STORED
                      and schema.field(target, k)["type"] not in ("link", "autonumber")}
            key = schema.title_field(target)
            if key and values.get(key) in (None, ""):
                values[key] = was.title(old)
            try:
                found = now.create(target["key"], values, by=BY, automations=False, ai=False)
                added.append(now.title(found))
            except FieldErrors:
                ok = False
                continue
        out.append(found.id)
    if not out:
        return None, added, ok
    return (out if f.get("multiple") else out[0]), added, ok


def _full(engine, record):
    """A person's whole name when the list keeps first and last apart (two Sarahs are told apart)."""
    data = record.data or {}
    first, last = data.get("first_name"), data.get("last_name")
    return f"{first} {last}".strip().lower() if first and last else None


def _fits(engine, f, value):
    """A worked-out value as the kept field holds it (a lookup of a link is the linked record's id)."""
    if f["type"] == "link":
        ids = [int(v) for v in (value if isinstance(value, list) else [value]) if str(v).isdigit() and engine.get(int(v))]
        return (ids if f.get("multiple") else ids[0]) if ids else None
    if isinstance(value, (dict, list)) and f["type"] not in ("multi_select", "tags", "json", "user"):
        return None
    return value


def _names(names):
    names = list(dict.fromkeys(names))
    shown = names[:3] + ([f"{len(names) - 3} more"] if len(names) > 3 else [])
    return shown[0] if len(shown) == 1 else ", ".join(shown[:-1]) + " and " + shown[-1]
