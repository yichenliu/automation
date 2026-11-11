"""What an outside person (a client, a supplier's person, a subcontractor, signed in to the portal) may see and do: the
records that reach them, the fields the portal shows them, what they may change and while when, the moves meant for
them, the records and lines they may add. The portal's pages ask here, and so does the simulator when an example acts
one of them out, so what an app is tried with is what its outside people get.

The engine an outside person works through acts as the platform, with `portal_contact` set to their record: what they
may touch is decided here, before it's asked to change anything."""
from ..blueprint import schema
from .records import Engine, FieldErrors


class NotTheirs(LookupError):
    """Not a record, or a list, that's open to this outside person (the portal's "not found")."""


class Gone(PermissionError):
    """Their own record is gone, so they're no one here now."""


def enter(ws, member, name):
    """(engine, portal, contact) for an outside person's membership of a workspace."""
    if ws is None or member is None or member.role != "portal":
        raise NotTheirs("not an outside person of this app")
    engine = Engine(ws, by=f"{name} (through the portal)")
    engine.user_id = member.user_id  # who they are: what they add is theirs (who asked), and they're held apart as anyone is
    contact = engine.get(member.contact_id)
    if contact is None:
        raise Gone("their record is gone")
    portal = schema.portal_of(engine.bp, contact.table)
    if portal is None or not portal.get("enabled", True):
        raise NotTheirs("their portal is off")
    engine.portal_contact = contact.id  # sealed records they made open to them; their changes may wait for approval
    return engine, portal, contact


def tables(engine, portal):
    """The tables this person's portal shows: those reaching their kind (a subcontractor's jobs, not clients' projects)."""
    return [t for t in engine.bp.get("tables", []) if t.get("portal") and not t.get("embedded")
            and schema.portal_shows(engine.tables, t, portal["table"])]


def fields(t):
    """What the portal shows of a table: the fields the design lists, or everything people type in. A field kept from
    staff below admin, or to a team, only when the portal names it (a seller's own bank details, which buyers' staff
    never see)."""
    named = _named(t)
    chosen = [schema.field(t, k) for k in t["portal"].get("fields") or [] if schema.field(t, k)]
    out = [f for f in (chosen or t["fields"]) if not f.get("access") or f["key"] in named]
    return out + [f for f in t["fields"] if f.get("access") and f["key"] in named and f not in out]


def _named(t):
    """The fields a list's portal names outright, to show or to change."""
    portal = t.get("portal") or {}
    return set(portal.get("fields") or []) | set(portal.get("edit") or [])


def mine(engine, t, contact):
    return [r for r in engine.rows(t["key"]) if engine.portal_reaches(r, contact.id)]


def find(engine, contact, table, rid):
    """(t, record) for a record of theirs, or NotTheirs."""
    t = engine.tables.get(table)
    if t is None or not t.get("portal") or t.get("embedded"):
        raise NotTheirs(table)
    r = engine.get(rid, table)
    if r is None or not engine.portal_reaches(r, contact.id):
        raise NotTheirs(rid)
    return t, r


def locked(engine, t, r):
    """Why they can't change it now (it's locked, sent, closed), or None."""
    reason = engine.locked(r)
    if reason:
        return reason
    if t.get("lock_if") and engine.matches(r, t["lock_if"]):
        return f"This {schema.lc(t['singular'])} is locked, so it can't be changed here."
    return None


def editable(engine, t, r):
    """The fields they may change on a record now: the portal's "edit" fields, while its "edit_if" holds."""
    portal = t["portal"]
    if "edit" not in portal or (portal.get("edit_if") and not engine.matches(r, portal["edit_if"])) or locked(engine, t, r):
        return []
    keys = portal["edit"] or [f["key"] for f in fields(t)]
    flow = (t.get("workflow") or {}).get("field")
    return [f for f in (schema.field(t, k) for k in keys) if f and f["type"] in (schema.STORED - {"autonumber"}) | UPLOADS
            and (not f.get("access") or f["key"] in _named(t)) and f["key"] != flow and f["key"] != t["portal"]["via"].partition(".")[0]]


def lines(engine, t):
    """The line items of a table as the portal shows and edits them (a bid's prices): not fields kept from them."""
    out = []
    for child in engine.bp.get("tables", []):
        parent = next((f for f in child["fields"] if f["type"] == "link" and not (engine.tables.get(f["table"]) or {}).get("embedded")), None)
        link = parent if parent is not None and parent["table"] == t["key"] else None
        if child.get("embedded") and link:
            cols = [f for f in child["fields"] if f["key"] != link["key"] and not f.get("access") and f["type"] not in schema.FILES]
            out.append({"t": child, "table": child, "via": link, "cols": cols, "fields": cols})
    return out


def open_lines(engine, t, r):
    """The line lists of a record an outside person may change while they may change the record: the lines of a record
    they added themselves (their invoice's lines), and those its portal names among what they edit (a bid's prices, on a
    bid the tender made for them). Never a buyer's order's lines because the seller may add a note to it."""
    portal = t.get("portal") or {}
    if "edit" not in portal or (portal.get("edit_if") and not engine.matches(r, portal["edit_if"])) or locked(engine, t, r):
        return []
    theirs = engine.user_id is not None and getattr(r, "created_by_id", None) == engine.user_id
    named = set((t.get("portal") or {}).get("edit") or [])
    return [c for c in lines(engine, t) if theirs or c["t"]["key"] in named]


def options(engine, fields_, contact=None):
    """The choices of the link fields they fill in: records that are theirs (the tenders they're invited to), or reference
    records anyone may name (an item on a bid line); never other outside people's."""
    out = {}
    contact = contact if contact is not None else engine.get(engine.portal_contact) if engine.portal_contact else None
    people = contact.table if contact is not None else None
    for f in fields_:
        if f["type"] != "link":
            continue
        target = engine.tables.get(f["table"]) or {}
        to_people = [g for g in target.get("fields", []) if g["type"] == "link" and g["table"] == people]
        rows = engine.rows(f["table"])
        if f["table"] == people:
            rows = [x for x in rows if contact is not None and x.id == contact.id]
        elif target.get("portal"):
            rows = [x for x in rows if contact is not None and engine.portal_reaches(x, contact.id)]
        elif to_people:  # theirs when one of its links names them (a tender's invited suppliers)
            ids = lambda v: v if isinstance(v, list) else [v]  # noqa: E731
            rows = [x for x in rows if contact is not None and any(str(contact.id) in [str(i) for i in ids((x.data or {}).get(g["key"]))]
                                                                   for g in to_people)]
        out[f["key"]] = [(x.id, engine.title(x)) for x in rows[:500]]
    return out


def line_options(engine, lines_, contact=None):
    """The choices of the link fields on a record's lines, by "lines.key" (a line's Unit isn't its record's Unit)."""
    out = {}
    for c in lines_:
        for key, choices in options(engine, c["cols"], contact).items():
            out[f"{c['t']['key']}.{key}"] = choices
    return out


def ids_reaching(engine, t, middle):
    """The outside people a record that a table's records hang off reaches (a tender's invited suppliers): along the
    rest of the portal's route from it."""
    portal_people = next((p["table"] for p in schema.portals(engine.bp) if schema.portal_shows(engine.tables, t, p["table"])), None)
    links = schema.portal_route(engine.tables, t)
    if not links or portal_people is None:
        return []
    rest = links[1:]
    ends = engine._along(middle, rest) if rest else {middle.id}
    return sorted(engine.portal_people_of(ends, (rest[-1] if rest else links[0])["table"], portal_people))


def addable(engine, portal, contact, table):
    """What adding a record to a list from the portal takes: {t, via, direct, fields, lines, options}, or NotTheirs. A
    record linking straight to them (their own invoice) is theirs from the start; one for a record of theirs (a question
    on a tender they're invited to) names which."""
    t = engine.tables.get(table)
    if t is None or not (t.get("portal") or {}).get("can_add"):
        raise NotTheirs(table)
    head = t["portal"]["via"].partition(".")[0]
    via = schema.field(t, head)
    if via is None or via["type"] != "link":  # (a list shown to everyone in the portal isn't added to from it)
        raise NotTheirs(table)
    direct = via["table"] == portal["table"]
    fields_ = [f for f in fields(t) if f["key"] != (head if direct else None) and f["type"] in (schema.STORED - {"autonumber", "user"}) | UPLOADS
               and f["key"] != (t.get("workflow") or {}).get("field")]
    if not direct and via not in fields_:
        fields_ = [via] + fields_
    lines_ = lines(engine, t)
    choices = {**options(engine, fields_ + [f for c in lines_ for f in c["cols"]], contact), **line_options(engine, lines_, contact)}
    if not direct:
        choices[via["key"]] = [(x.id, engine.title(x)) for x in engine.rows(via["table"]) if contact.id in ids_reaching(engine, t, x)]
    return {"t": t, "via": via, "direct": direct, "fields": fields_, "lines": lines_, "options": choices}


def add(engine, portal, contact, table, values, then=None, automations=True):
    """Add a record from the portal (a job request, a bid, an invoice): only the fields it shows them, each link one
    offered to them, and theirs from the start. then(record, lines): its lines, saved with it (problems said, or none).
    automations=False: its rules wait (for its lines, added next)."""
    form = addable(engine, portal, contact, table)
    t, via, offered = form["t"], form["via"], {f["key"]: f for f in form["fields"]}
    errors = {k: f"{schema.field(t, k)['label'] if schema.field(t, k) else k} isn't filled in from the portal"
              for k in values if k not in offered and not (form["direct"] and k == via["key"])}
    if form["direct"] and values.get(via["key"]) not in (None, "", contact.id, str(contact.id)):  # never someone else's
        errors[via["key"]] = f"What's added here is always {schema.lc(via['label'])} {engine.title(contact)}'s own"
    values = {k: v for k, v in values.items() if k in offered}
    if form["direct"]:
        values[via["key"]] = contact.id
    for key, f in offered.items():  # a link they choose must be one offered to them
        if f["type"] == "link" and values.get(key) not in (None, "") and \
                str(values[key]) not in {str(i) for i, _ in form["options"].get(key, [])}:
            errors[key] = f"Choose a {schema.lc(f['label'])} that's open to you"
    sent = {k: values.pop(k) for k in [k for k, f in offered.items() if f["type"] in UPLOADS and k in values]}
    for key, f in offered.items():  # a document the record can't be without (the certificate itself)
        if f["type"] in UPLOADS and f.get("required") and not _uploads(sent.get(key)):
            errors[key] = f"{f['label']} is required"
    if errors:
        raise FieldErrors(errors)
    with engine.atomic():
        record = engine.create(t["key"], values, automations=automations)
        for key, given in sent.items():
            attach(engine, record, offered[key], given)
        problems = then(record, form["lines"]) if then else []
        if problems:
            raise FieldErrors({"_": "; ".join(problems)})
    return record


UPLOADS = {"file", "image"}  # what an outside person can send in the portal (a signature is drawn, at a move that needs it)


def _uploads(given):
    """What was sent for a file field, as a list: uploaded files, (name, bytes), or a file's name (the simulator's
    stand-in for one)."""
    items = given if isinstance(given, (list, tuple)) and not (len(given) == 2 and isinstance(given[1], (bytes, bytearray))) else [given]
    return [x for x in items if x not in (None, "", b"") and (not hasattr(x, "filename") or x.filename)]


def attach(engine, record, f, given):
    """Keep the files an outside person sent for a file field of a record of theirs (a picture replaces the one before)."""
    from ..models import File
    from ..models import utcnow
    from ..storage import save
    items = _uploads(given)
    if f["type"] == "image" and items:
        for old in File.query.filter_by(record_id=record.id, field=f["key"], deleted_at=None):
            old.deleted_at = utcnow()
        items = items[:1]
    for item in items[:20]:
        if isinstance(item, str):  # a stand-in, as the simulator sends: a small file of that name
            save(engine.ws.id, b"%PDF-1.4 sample", item if "." in item else f"{item}.pdf", record_id=record.id, field=f["key"], by=engine.by)
        elif isinstance(item, tuple):
            save(engine.ws.id, item[1], item[0], record_id=record.id, field=f["key"], by=engine.by)
        else:
            save(engine.ws.id, item, item.filename, record_id=record.id, field=f["key"], by=engine.by)
    engine.reset()


def _parent_of(engine, contact, t, values=None, record=None):
    """For a line (a bid's price): the record it's on, which must be theirs and open to change now, and its columns."""
    via = next((f for f in t["fields"] if f["type"] == "link" and not (engine.tables.get(f["table"]) or {}).get("embedded")), None)
    if via is None:
        raise NotTheirs(t["key"])
    parent_id = (values or {}).get(via["key"]) if record is None else (record.data or {}).get(via["key"])
    pt, parent = find(engine, contact, via["table"], parent_id)
    child = next((c for c in lines(engine, pt) if c["t"]["key"] == t["key"]), None)
    if child is None:
        raise NotTheirs(t["key"])
    if not any(c["t"]["key"] == t["key"] for c in open_lines(engine, pt, parent)):
        if locked(engine, pt, parent) or not editable(engine, pt, parent):
            raise FieldErrors({"_": locked(engine, pt, parent) or f"This {schema.lc(pt['singular'])} can't be changed here now."})
        raise FieldErrors({"_": f"The {schema.lc(t['label'])} of this {schema.lc(pt['singular'])} are {schema.lc(pt['singular'])}'s own: "
                                f"propose a change instead"})
    return via, child


def add_line(engine, contact, table, values):
    """A line added to a record of theirs that's open to change (a price on their bid while the tender is open)."""
    t = engine.tables.get(table)
    if t is None or not t.get("embedded"):
        raise NotTheirs(table)
    via, child = _parent_of(engine, contact, t, values=values)
    cols = {f["key"] for f in child["cols"]}
    kept = [k for k in values if k not in cols and k != via["key"]]
    if kept:
        raise FieldErrors({k: f"{schema.field(t, k)['label'] if schema.field(t, k) else k} isn't filled in from the portal" for k in kept})
    return engine.create(t["key"], {k: v for k, v in values.items() if k in cols or k == via["key"]})


def change(engine, contact, t, r, values, expected_version=None):
    """Their part of a record (the notes on their bid while the tender is open), or a line on one: nothing else."""
    if t.get("embedded"):
        via, child = _parent_of(engine, contact, t, record=r)
        allowed = {f["key"] for f in child["cols"] if f["type"] in schema.STORED - {"autonumber"}}
    else:
        if not engine.portal_reaches(r, contact.id):
            raise NotTheirs(r.id)
        open_ = editable(engine, t, r)
        if not open_:
            raise FieldErrors({"_": locked(engine, t, r) or "This can't be changed here now."})
        allowed = {f["key"] for f in open_}
    kept = [k for k in values if k not in allowed]
    if kept:
        raise FieldErrors({k: f"{schema.field(t, k)['label'] if schema.field(t, k) else k} can't be changed here" for k in kept})
    sent = {k: values.pop(k) for k in [k for k in values if (schema.field(t, k) or {}).get("type") in UPLOADS]}
    with engine.atomic():
        for key, given in sent.items():
            attach(engine, r, schema.field(t, key), given)
        return engine.update(r, values, expected_version=expected_version) if values or not sent else engine.get(r.id)


def move(engine, contact, t, r, index, by):
    """A move meant for them (Acknowledge the order, Submit the bid). A record locked against changes still moves by its
    buttons, as it does for staff (a sent order is acknowledged): each move's own conditions hold it back."""
    if not engine.portal_reaches(r, contact.id):
        raise NotTheirs(r.id)
    return engine.move(r, index, by=by)
