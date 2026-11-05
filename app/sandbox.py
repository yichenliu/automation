"""A test copy of an app: the same design, records (their links pointing at the copies), numbering and people, to try
changes on without touching the live app. Changes are made there as anywhere (by asking the builder, by hand); then
"Publish" brings the copy's design to the live app as a new version, after saying what changes and what that does to the
live app's records. Records made in the test copy stay there. An app has one test copy at a time; making another
starts again from the live app."""
import copy as copying

from . import audit
from .blueprint import schema
from .extensions import db
from .models import Counter, Membership, Record, Workspace, utcnow

KEPT_SETTINGS = ("business", "page_size", "money", "holidays", "theme", "idle_minutes")  # not printers, tokens, connections


def live_of(ws):
    return db.session.get(Workspace, (ws.settings or {}).get("test_copy_of")) if (ws.settings or {}).get("test_copy_of") else None


def test_copy_of(live):
    copy_id = (live.settings or {}).get("test_copy")
    found = db.session.get(Workspace, copy_id) if copy_id else None
    return found if found is not None and (found.settings or {}).get("test_copy_of") == live.id else None


def make(live, user):
    """A fresh test copy (an earlier one is replaced)."""
    from .core import tenancy
    with tenancy.wide(db.session):  # from one workspace (the live app) into another (its copy): rightly both
        return _make(live, user)


def _make(live, user):
    old = test_copy_of(live)
    if old is not None:
        _remove(old)
    ws = Workspace(owner_id=live.owner_id, name=f"{live.name} (test copy)", about=live.about, blueprint=copying.deepcopy(live.blueprint),
                   version=live.version, timezone=live.timezone, currency=live.currency, state={},
                   settings={**{k: copying.deepcopy(v) for k, v in (live.settings or {}).items() if k in KEPT_SETTINGS},
                             "test_copy_of": live.id, "based_on": live.version})
    db.session.add(ws)
    db.session.flush()
    for m in Membership.query.filter_by(workspace_id=live.id):
        if m.role != "portal":  # outside people are never in a test copy
            db.session.add(Membership(workspace_id=ws.id, user_id=m.user_id, role=m.role, teams=list(m.teams or []) or None))
    ids = {}
    for r in Record.query.filter_by(workspace_id=live.id, deleted_at=None).order_by(Record.id):
        copy = Record(workspace_id=ws.id, table=r.table, data=copying.deepcopy(r.data or {}), created_by=r.created_by,
                      created_by_id=r.created_by_id, updated_by=r.updated_by, created_at=r.created_at, updated_at=r.updated_at)
        db.session.add(copy)
        db.session.flush()
        ids[r.id] = copy.id
    links = {t["key"]: [f for f in t["fields"] if f["type"] == "link"] for t in (ws.blueprint or {}).get("tables", []) if any(
        f["type"] == "link" for f in t["fields"])}
    for r in Record.query.filter_by(workspace_id=ws.id):
        fields = links.get(r.table) or []
        if not fields:
            continue
        data = dict(r.data or {})
        for f in fields:
            value = data.get(f["key"])
            if isinstance(value, list):
                data[f["key"]] = [ids.get(int(v), v) if str(v).isdigit() else v for v in value]
            elif str(value or "").isdigit():
                data[f["key"]] = ids.get(int(value), value)
        r.data = data
    for c in Counter.query.filter_by(workspace_id=live.id):
        db.session.add(Counter(workspace_id=ws.id, table=c.table, field=c.field, value=c.value))
    live.settings = {**(live.settings or {}), "test_copy": ws.id}
    db.session.commit()
    audit.log(live.id, "settings", f"made a test copy ({len(ids)} records copied)", user=user)
    return ws


def _remove(ws):
    Record.query.filter_by(workspace_id=ws.id).delete()
    Counter.query.filter_by(workspace_id=ws.id).delete()
    Membership.query.filter_by(workspace_id=ws.id).delete()
    db.session.delete(ws)
    db.session.flush()


def changes(before, after):
    """What publishing would change, in words: lists, fields, choices, rules, flows, pages, menus."""
    out = []
    old_t, new_t = {t["key"]: t for t in before.get("tables", [])}, {t["key"]: t for t in after.get("tables", [])}
    out += [f"Adds the list {new_t[k]['label']}" for k in new_t if k not in old_t]
    out += [f"Removes the list {old_t[k]['label']}" for k in old_t if k not in new_t]
    for k in [k for k in new_t if k in old_t]:
        was, now = {f["key"]: f for f in old_t[k]["fields"]}, {f["key"]: f for f in new_t[k]["fields"]}
        label = new_t[k]["label"]
        out += [f"{label}: adds {now[f]['label']}" for f in now if f not in was]
        out += [f"{label}: removes {was[f]['label']}" for f in was if f not in now]
        for f in [f for f in now if f in was]:
            a, b = was[f], now[f]
            if a["type"] != b["type"]:
                out.append(f"{label}: {b['label']} becomes {b['type'].replace('_', ' ')} (was {a['type'].replace('_', ' ')})")
            if (a.get("options") or []) != (b.get("options") or []):
                out.append(f"{label}: {b['label']}'s choices become {', '.join(b.get('options') or []) or 'none'}")
            if bool(a.get("required")) != bool(b.get("required")):
                out.append(f"{label}: {b['label']} {'must be filled in' if b.get('required') else 'may be left empty'}")
        if old_t[k].get("workflow") != new_t[k].get("workflow"):
            out.append(f"{label}: its stages or moves change")
        for part, word in (("views", "views"), ("documents", "documents"), ("rules", "rules every record must keep"), ("access", "who may see it")):
            if old_t[k].get(part) != new_t[k].get(part):
                out.append(f"{label}: its {word} change")
    for part, word, name in (("automations", "rule", "name"), ("pages", "page", "label"), ("flows", "flow", "label"), ("spaces", "team menu", "label")):
        was, now = {x["key"]: x for x in before.get(part) or []}, {x["key"]: x for x in after.get(part) or []}
        out += [f"Adds the {word} “{now[k][name]}”" for k in now if k not in was]
        out += [f"Removes the {word} “{was[k][name]}”" for k in was if k not in now]
        out += [f"Changes the {word} “{now[k][name]}”" for k in now if k in was and was[k] != now[k]]
    if before.get("teams") != after.get("teams"):
        out.append("Changes the teams")
    return out


def impact(live, after):
    """What publishing would do to the live app's records: [(level "warn" | "note", sentence)]."""
    from .engine.records import Engine
    engine = Engine(live)
    out = []
    old_t = {t["key"]: t for t in (live.blueprint or {}).get("tables", [])}
    for t in after.get("tables", []):
        before = old_t.get(t["key"])
        if before is None:
            continue
        rows = engine.rows(t["key"])
        was = {f["key"]: f for f in before["fields"]}
        for f in t["fields"]:
            old = was.get(f["key"])
            if f.get("required") and f["type"] in schema.STORED and (old is None or not old.get("required")) and f.get("default") in (None, ""):
                missing = sum(1 for r in rows if (r.data or {}).get(f["key"]) in (None, "", []))
                if missing:
                    out.append(("warn", f"{_n(missing, t)} {'has' if missing == 1 else 'have'} no {schema.lc(f['label'])}, which must now be "
                                        f"filled in: {'it' if missing == 1 else 'they'}'ll ask for it the next time {'it is' if missing == 1 else 'they are'} changed"))
            if old is not None and f["type"] in ("select", "multi_select") and old.get("options"):
                gone = set(old["options"]) - set(f.get("options") or [])
                for option in sorted(gone):
                    using = sum(1 for r in rows if option == (r.data or {}).get(f["key"]) or option in ((r.data or {}).get(f["key"]) or [] if isinstance((r.data or {}).get(f["key"]), list) else []))
                    if using:
                        out.append(("warn", f"{_n(using, t)} {'is' if using == 1 else 'are'} “{option}” in {schema.lc(f['label'])}, which is no longer a choice"))
        for key, old in was.items():
            if key not in {f["key"] for f in t["fields"]} and old["type"] in schema.STORED:
                having = sum(1 for r in rows if (r.data or {}).get(key) not in (None, "", []))
                if having:
                    out.append(("note", f"{_n(having, t)} {'has' if having == 1 else 'have'} a {schema.lc(old['label'])}: no longer shown, but "
                                        "kept, and back if the change is undone"))
    gone = [t for k, t in old_t.items() if k not in {x["key"] for x in after.get("tables", [])}]
    for t in gone:
        n = len(engine.rows(t["key"]))
        if n:
            out.append(("note", f"{_n(n, t)} won't be shown any more: kept, and back if the change is undone"))
    return out


def _n(n, t):
    return f"{n} {schema.lc(t['singular'] if n == 1 else t['label'])}"


def publish(live, test_copy, user):
    """The test copy's design, as the live app's next version (undoable like any other). Its records stay where they are."""
    from .core import tenancy
    with tenancy.wide(db.session):  # asked from the test copy, it changes the live app: rightly both
        return _publish(live, test_copy, user)


def _publish(live, test_copy, user):
    from . import studio
    from .builder import chat
    said = changes(live.blueprint or {}, test_copy.blueprint or {})
    if not said:
        return None
    # choices renamed in the test copy since it was made (or last published): the live records get the new names too
    base = int((test_copy.settings or {}).get("published_from") or (test_copy.settings or {}).get("based_on") or 0)  # its own numbering
    kept = ((test_copy.settings or {}).get("studio") or {}).get("renamed") or {}
    renames = [tuple(r) for v in sorted(kept, key=int) if int(v) > base for r in kept[v]]
    named = sum(studio.rename_records(live, *r) for r in renames)
    told = [f"Published from the test copy: {s}" for s in said] + ([f"{named} live record{'s' if named != 1 else ''} named as in the test copy"] if named else [])
    chat.save_version(live, copying.deepcopy(test_copy.blueprint), told, "published", by=user.id)
    studio.remember_renames(live, renames)  # going back on the live app names them as they were
    test_copy.settings = {**(test_copy.settings or {}), "based_on": live.version, "published_from": test_copy.version}
    db.session.commit()
    return live.version
