"""The portal: outside people (customers, suppliers, subcontractors) sign in and see only their own records: their
invoices and quotes with the PDFs, their jobs, their orders. They send messages on a record and, where the design
allows, do their part of the work: add a record with its lines (a bid, an invoice), change some of its fields while a
condition holds (until the tender closes), make the workflow moves meant for them (Acknowledge, Accept), answer the
approvals and tasks sent to them (sign the contract), and keep their own details up to date (a bank account change
waits for the business's approval). Where the portal allows it, they can join by themselves.

Which tables appear and which of their fields is set in the design: the blueprint's "portals" say who the people are
(a table with an email field: clients, subcontractors, each with a portal of their own), and each table's "portal"
says how its records reach them ({"via": "customer"}, or through the record it links to: {"via": "tender.invited"}).
A person is invited from their record (Invite with role "portal"), so their account is tied to that record, and so to
their kind's portal.
"""
from flask import Blueprint, abort, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required, login_user

from ..blueprint import schema
from ..engine import automations
from ..engine.records import Conflict, Engine, FieldErrors
from ..extensions import db
from ..models import Approval, Comment, File, Membership, Notification, User, Workspace
from ..security import client_ip, form_posts
from ..storage import path_of

bp = Blueprint("portal", __name__, url_prefix="/portal/<int:wid>")


def _ctx(wid):
    ws = db.session.get(Workspace, wid)
    member = Membership.query.filter_by(workspace_id=wid, user_id=current_user.id).first() if ws else None
    if member is None or member.role != "portal":
        abort(404)
    engine = Engine(ws, by=f"{current_user.display} (through the portal)")
    contact = engine.get(member.contact_id)
    if contact is None:
        abort(403)
    portal = schema.portal_of(engine.bp, contact.table)
    if portal is None or not portal.get("enabled", True):
        abort(404)
    engine.portal_contact = contact.id  # sealed records they made open to them; their changes may wait for approval
    return ws, engine, portal, contact


def _tables(engine, portal):
    """The tables this person's portal shows: those reaching their kind (a subcontractor's jobs, not clients' projects)."""
    return [t for t in engine.bp.get("tables", []) if t.get("portal") and not t.get("embedded")
            and schema.portal_shows(engine.tables, t, portal["table"])]


def _fields(t):
    """What the portal shows of a table: the fields the design lists, or everything people type in (never fields kept
    from staff below admin, or to a team)."""
    chosen = [schema.field(t, k) for k in t["portal"].get("fields") or [] if schema.field(t, k)]
    return [f for f in (chosen or t["fields"]) if not f.get("access")]


def _mine(engine, t, contact):
    return [r for r in engine.rows(t["key"]) if engine.portal_reaches(r, contact.id)]


def _record(engine, contact, table, rid):
    t = engine.tables.get(table)
    if t is None or not t.get("portal") or t.get("embedded"):
        abort(404)
    r = engine.get(rid, table)
    if r is None or not engine.portal_reaches(r, contact.id):
        abort(404)
    return t, r


def _locked(engine, t, r):
    """Why they can't change it now (it's locked, sent, closed), or None."""
    reason = engine.locked(r)
    if reason:
        return reason
    if t.get("lock_if") and engine.matches(r, t["lock_if"]):
        return f"This {schema.lc(t['singular'])} is locked, so it can't be changed here."
    return None


def _editable(engine, t, r):
    """The fields they may change on a record now: the portal's "edit" fields, while its "edit_if" holds."""
    portal = t["portal"]
    if "edit" not in portal or (portal.get("edit_if") and not engine.matches(r, portal["edit_if"])) or _locked(engine, t, r):
        return []
    keys = portal["edit"] or [f["key"] for f in _fields(t)]
    flow = (t.get("workflow") or {}).get("field")
    return [f for f in (schema.field(t, k) for k in keys) if f and f["type"] in schema.STORED - {"autonumber"} and not f.get("access")
            and f["key"] != flow and f["key"] != t["portal"]["via"].partition(".")[0]]


def _lines(engine, t):
    """The line items of a table as the portal shows and edits them (a bid's prices): not fields kept from them."""
    out = []
    for child in engine.bp.get("tables", []):
        parent = next((f for f in child["fields"] if f["type"] == "link" and not (engine.tables.get(f["table"]) or {}).get("embedded")), None)
        link = parent if parent is not None and parent["table"] == t["key"] else None
        if child.get("embedded") and link:
            fields = [f for f in child["fields"] if f["key"] != link["key"] and not f.get("access") and f["type"] not in schema.FILES]
            out.append({"t": child, "table": child, "via": link, "cols": fields, "fields": fields})
    return out


def _options(engine, fields, contact=None):
    """The choices of the link fields they fill in: records that are theirs (the tenders they're invited to), or reference
    records anyone may name (an item on a bid line); never other outside people's."""
    out = {}
    contact = contact if contact is not None else engine.get(engine.portal_contact) if engine.portal_contact else None
    people = contact.table if contact is not None else None
    for f in fields:
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


def _save_lines(engine, t, parent, editable_lines):
    from .erp_records import _posted_lines
    problems = []
    for child in editable_lines:
        key, via = child["table"]["key"], child["via"]["key"]
        for row in _posted_lines(child):
            existing = engine.get(row["id"], key) if row["id"] else None
            if existing is not None and str((existing.data or {}).get(via)) != str(parent.id):
                continue
            values = {k: v for k, v in row["values"].items() if (schema.field(child["table"], k) or {}).get("type") not in schema.CALCULATED}
            try:
                if existing is not None and (row["remove"] or row["empty"]):
                    engine.delete(existing)
                elif existing is not None:
                    engine.update(existing, values)
                elif not row["remove"] and not row["empty"]:
                    engine.create(key, {**values, via: parent.id})
            except FieldErrors as exc:
                problems.append("; ".join(exc.errors.values()))
    return problems


def _waiting(engine, ws, contact):
    """What's waiting for them: approvals and tasks sent to them, and changes to their records they can still cancel."""
    mine = Approval.query.filter_by(workspace_id=ws.id, status="pending", assignee_id=current_user.id).order_by(Approval.id).all()
    changes = [a for a in Approval.query.filter_by(workspace_id=ws.id, status="pending", kind="change").all()
               if (a.proposal or {}).get("token") and not (a.proposal or {}).get("confirmed") and (rec := engine.get(a.record_id)) is not None
               and (rec.id == contact.id or engine.portal_reaches(rec, contact.id))]
    return mine, list({a.run_id: a for a in changes}.values())


@bp.route("/")
@login_required
def home(wid):
    ws, engine, portal, contact = _ctx(wid)
    sections = []
    for t in _tables(engine, portal):
        rows = sorted(_mine(engine, t, contact), key=lambda r: r.id, reverse=True)
        sections.append({"t": t, "rows": rows[:100], "total": len(rows),
                         "fields": [f for f in _fields(t) if f["key"] != t["portal"]["via"].partition(".")[0]][:5],
                         "moves": {r.id: [m for _, m, problem in engine.moves(r) if not problem] for r in rows[:100]}})
    asks, changes = _waiting(engine, ws, contact)
    return render_template("portal/home.html", ws=ws, e=engine, portal=portal, contact=contact, sections=sections, asks=asks,
                           changes=changes)


@bp.route("/me", methods=["GET", "POST"])
@login_required
def profile(wid):
    """Their own details (their phone, their bank account): a change to fields whose changes need approval waits for it."""
    ws, engine, portal, contact = _ctx(wid)
    t = engine.tables[contact.table]
    stage = (t.get("workflow") or {}).get("field")
    fields = [f for f in (schema.field(t, k) for k in portal.get("profile") or []) if f and not f.get("access") and f["key"] != stage]
    if "profile" not in portal or not fields:
        abort(404)
    errors = {}
    if request.method == "POST":
        values = {f["key"]: (bool(request.form.get(f["key"])) if f["type"] == "checkbox" else request.form.get(f["key"])) for f in fields}
        try:
            engine.update(contact, values, expected_version=request.form.get("_version"))
        except Conflict:
            errors = {"_": "These details changed while you were looking. Here they are now; make your change again."}
        except FieldErrors as exc:
            errors = exc.errors
        else:
            if engine.held:
                flash(f"Thanks. A change to {', '.join(engine.held)} is checked by {engine.bp.get('name') or ws.name} before it's used; "
                      "we'll tell you when it is.", "info")
            else:
                flash("Saved.", "success")
            return redirect(url_for("portal.profile", wid=wid))
    contact = engine.get(contact.id)
    pending = [a for a in Approval.query.filter_by(workspace_id=ws.id, record_id=contact.id, kind="change", status="pending").all()]
    return render_template("portal/profile.html", ws=ws, e=engine, portal=portal, contact=contact, t=t, fields=fields, errors=errors,
                           pending=pending[:1], options=_options(engine, fields, contact)), 400 if errors else 200


@bp.route("/t/<table>/<int:rid>")
@login_required
def record(wid, table, rid):
    ws, engine, portal, contact = _ctx(wid)
    t, r = _record(engine, contact, table, rid)
    from .erp_records import audited
    if audited(engine, t):
        from ..models import RecordChange
        db.session.add(RecordChange(workspace_id=ws.id, record_id=r.id, kind="viewed", by=f"{current_user.display} (through the portal)",
                                    by_id=current_user.id))
        db.session.commit()
    editable = _editable(engine, t, r)
    lines = []
    for child in _lines(engine, t):
        lines.append({**child, "rows": engine.children(child["t"]["key"], child["via"]["key"], r.id)})
    shown = {f["key"] for f in _fields(t) if f["type"] in schema.FILES}
    files = [x for x in File.query.filter(File.record_id == r.id, File.deleted_at.is_(None)).order_by(File.id.desc()) if x.field in shown]
    messages = Comment.query.filter_by(record_id=r.id, shared=True).order_by(Comment.id).all()
    asks = Approval.query.filter_by(workspace_id=ws.id, record_id=r.id, status="pending", assignee_id=current_user.id).all()
    return render_template("portal/record.html", ws=ws, e=engine, portal=portal, contact=contact, t=t, r=r,
                           fields=[f for f in _fields(t) if f["key"] != t["portal"]["via"].partition(".")[0]], lines=lines, files=files,
                           messages=messages, documents=[d for d in t.get("documents", []) if d.get("format") != "text"],
                           editable=editable, moves=engine.moves(r), asks=asks, locked=_locked(engine, t, r),
                           sealed=engine.seal_note(r), options=_options(engine, editable + [f for c in lines for f in c["cols"]], contact),
                           edit_lines=bool(editable) and bool(lines))


@bp.route("/t/<table>/<int:rid>/edit", methods=["POST"])
@login_required
def edit(wid, table, rid):
    """Their part of a record (a bid's prices, while the tender is open), and its lines."""
    ws, engine, portal, contact = _ctx(wid)
    t, r = _record(engine, contact, table, rid)
    editable = _editable(engine, t, r)
    if not editable:
        flash(_locked(engine, t, r) or "This can't be changed here now.", "error")
        return redirect(url_for("portal.record", wid=wid, table=table, rid=rid))
    values = {f["key"]: (bool(request.form.get(f["key"])) if f["type"] == "checkbox" else
                         request.form.getlist(f["key"]) if f["type"] == "multi_select" else request.form.get(f["key"])) for f in editable}
    try:
        with engine.atomic():
            engine.update(r, values, expected_version=request.form.get("_version"))
            problems = _save_lines(engine, t, r, _lines(engine, t))
            if problems:
                raise FieldErrors({"_": "; ".join(problems)})
    except Conflict:
        flash("This changed while you were editing it. Here it is now; make your change again.", "error")
    except FieldErrors as exc:
        flash("; ".join(exc.errors.values()), "error")
    else:
        held = f" A change to {', '.join(engine.held)} is checked before it counts." if engine.held else ""
        flash("Saved." + held, "success")
    return redirect(url_for("portal.record", wid=wid, table=table, rid=rid))


@bp.route("/t/<table>/<int:rid>/move/<int:index>", methods=["POST"])
@login_required
def move(wid, table, rid, index):
    """A move meant for them: Acknowledge the order, Accept the variation, Submit the bid."""
    ws, engine, portal, contact = _ctx(wid)
    t, r = _record(engine, contact, table, rid)
    reason = _locked(engine, t, r) if (t.get("lock_if") and engine.matches(r, t["lock_if"])) else None
    try:
        if reason:
            raise FieldErrors({"_": reason})
        engine.move(r, index, by=f"{current_user.display} (through the portal)")
    except (FieldErrors, Conflict) as exc:
        flash("; ".join(exc.errors.values()) if isinstance(exc, FieldErrors) else "This changed just now; try again.", "error")
    else:
        flash(f"Done: {engine.get(r.id).data.get(t['workflow']['field'])}.", "success")
    return redirect(url_for("portal.record", wid=wid, table=table, rid=rid))


@bp.route("/asks/<int:aid>", methods=["POST"])
@login_required
def answer(wid, aid):
    """An approval or task sent to them (sign the contract, confirm the delivery date)."""
    ws, engine, portal, contact = _ctx(wid)
    ask = db.session.get(Approval, aid)
    if ask is None or ask.workspace_id != ws.id or ask.assignee_id != current_user.id or ask.status != "pending" or ask.kind == "change":
        abort(404)
    decision = request.form.get("decision")
    by = f"{current_user.display} (through the portal)"
    try:
        if decision == "done" and ask.kind == "task":
            record = engine.get(ask.record_id) if ask.record_id else None
            values = {k: request.form.get(k) for k in ask.fields or []} if record is not None else {}
            automations.complete(engine, ask, by, values, request.form.get("comment"))
        elif decision in ("approve", "reject") and (ask.kind or "approval") == "approval":
            automations.decide(engine, ask, decision == "approve", by, request.form.get("comment"))
        else:
            abort(400)
    except FieldErrors as exc:
        db.session.rollback()
        flash("; ".join(exc.errors.values()), "error")
    else:
        flash("Thanks: that's done.", "success")
    back = request.form.get("back")
    return redirect(back if back and back.startswith("/portal/") else url_for("portal.home", wid=wid))


@bp.route("/changes/<int:aid>/confirm", methods=["POST"])
@login_required
def confirm(wid, aid):
    """"Yes, it was me": the change still waits for the business's approval; it's no longer asked about here."""
    ws, engine, portal, contact = _ctx(wid)
    _, changes = _waiting(engine, ws, contact)
    change = next((a for a in changes if a.id == aid), None)
    if change is None:
        abort(404)
    change.proposal = {**change.proposal, "confirmed": f"{current_user.display} said it was them"}
    change.history = (change.history or []) + [{"when": automations.utcnow().isoformat(), "what": f"{current_user.display} confirmed it was them"}]
    db.session.commit()
    flash("Thanks. We'll start using it once our team has checked it.", "success")
    return redirect(url_for("portal.home", wid=wid))


@bp.route("/changes/<int:aid>/cancel", methods=["POST"])
@login_required
def cancel(wid, aid):
    """"This wasn't me": a change to their details that's still waiting is cancelled."""
    ws, engine, portal, contact = _ctx(wid)
    _, changes = _waiting(engine, ws, contact)
    change = next((a for a in changes if a.id == aid), None)
    if change is None:
        abort(404)
    automations.cancel_change(change.proposal["token"])
    flash("Cancelled. Nothing was changed, and we've told the team.", "success")
    return redirect(url_for("portal.home", wid=wid))


@bp.route("/t/<table>/<int:rid>/message", methods=["POST"])
@login_required
def message(wid, table, rid):
    ws, engine, portal, contact = _ctx(wid)
    t, r = _record(engine, contact, table, rid)
    text = (request.form.get("text") or "").strip()[:5000]
    if text:
        db.session.add(Comment(workspace_id=ws.id, record_id=r.id, user_id=current_user.id, text=text, shared=True))
        db.session.add(Notification(workspace_id=ws.id, kind="mention", link=f"table:{t['key']}:record:{r.id}",
                                    text=f"{engine.title(contact)} wrote on {t['singular']} “{engine.title(r)}”: {text[:200]}"))
        db.session.commit()
        flash("Sent. We'll reply here.", "success")
    return redirect(url_for("portal.record", wid=wid, table=table, rid=rid) + "#messages")


@bp.route("/t/<table>/<int:rid>/document/<key>")
@login_required
def document(wid, table, rid, key):
    from ..documents import produce
    ws, engine, portal, contact = _ctx(wid)
    t, r = _record(engine, contact, table, rid)
    if not any(d["key"] == key and d.get("format") != "text" for d in t.get("documents", [])):
        abort(404)
    file = produce(engine, r, key, by=f"{current_user.display} (through the portal)")
    return send_file(path_of(file), mimetype="application/pdf", download_name=file.name)


@bp.route("/files/<int:fid>")
@login_required
def file(wid, fid):
    ws, engine, portal, contact = _ctx(wid)
    f = db.session.get(File, fid)
    if f is None or f.workspace_id != ws.id or f.deleted_at is not None or not f.record_id:
        abort(404)
    owner = engine.get(f.record_id)
    t = engine.tables.get(owner.table) if owner else None
    if owner is None or not t or not t.get("portal") or not engine.portal_reaches(owner, contact.id) \
            or f.field not in {x["key"] for x in _fields(t) if x["type"] in schema.FILES}:
        abort(404)
    from .erp_records import send_stored
    return send_stored(f)


@bp.route("/t/<table>/new", methods=["GET", "POST"])
@login_required
def new(wid, table):
    """Adding a record from the portal (a job request, a bid, an invoice with its lines): it's theirs from the start."""
    ws, engine, portal, contact = _ctx(wid)
    t = engine.tables.get(table)
    if t is None or not (t.get("portal") or {}).get("can_add"):
        abort(404)
    head = t["portal"]["via"].partition(".")[0]
    via = schema.field(t, head)
    if via is None or via["type"] != "link":
        abort(404)
    direct = via["table"] == portal["table"]
    fields = [f for f in _fields(t) if f["key"] != (head if direct else None) and f["type"] in schema.STORED - {"autonumber", "user"}
              and f["key"] != (t.get("workflow") or {}).get("field")]
    if not direct and via not in fields:  # theirs through the record it's for (a question on a tender they're invited to)
        fields = [via] + fields
    lines = _lines(engine, t)
    options = _options(engine, fields + [f for c in lines for f in c["cols"]], contact)
    if not direct:
        options[via["key"]] = [(x.id, engine.title(x)) for x in engine.rows(via["table"])
                               if contact.id in _ids_reaching(engine, t, x)]
    errors, values = {}, dict(request.args) if request.method == "GET" else {}
    if request.method == "POST":
        values = {f["key"]: request.form.get(f["key"]) if f["type"] != "checkbox" else bool(request.form.get(f["key"])) for f in fields}
        if any(f["type"] == "multi_select" for f in fields):
            values.update({f["key"]: request.form.getlist(f["key"]) for f in fields if f["type"] == "multi_select"})
        if direct:
            values[via["key"]] = contact.id
        for f in fields:  # a link they choose must be one offered to them
            if f["type"] == "link" and values.get(f["key"]) not in (None, "") and \
                    str(values[f["key"]]) not in {str(i) for i, _ in options.get(f["key"], [])}:
                errors[f["key"]] = f"Choose a {schema.lc(f['label'])} that's open to you"
        if not errors:
            try:
                with engine.atomic():
                    r = engine.create(t["key"], values)
                    problems = _save_lines(engine, t, r, lines)
                    if problems:
                        raise FieldErrors({"_": "; ".join(problems)})
            except FieldErrors as exc:
                errors = exc.errors
            else:
                db.session.add(Notification(workspace_id=ws.id, link=f"table:{t['key']}:record:{r.id}",
                                            text=f"{engine.title(contact)} added {schema.lc(t['singular'])} “{engine.title(r)}” in the portal"))
                db.session.commit()
                flash(f"Thanks: your {schema.lc(t['singular'])} is in.", "success")
                return redirect(url_for("portal.record", wid=wid, table=table, rid=r.id))
    return render_template("portal/new.html", ws=ws, e=engine, portal=portal, contact=contact, t=t, fields=fields, values=values,
                           errors=errors, lines=lines, options=options), 400 if errors else 200


def _ids_reaching(engine, t, middle):
    """The outside people a record that a table's records hang off reaches (a tender's invited suppliers)."""
    hop = t["portal"]["via"].partition(".")[2]
    portal_people = next((p["table"] for p in schema.portals(engine.bp) if schema.portal_shows(engine.tables, t, p["table"])), None)
    mt = engine.tables.get(middle.table) or {}
    on = schema.field(mt, hop) if hop else next((f for f in mt.get("fields", []) if f["type"] == "link" and f["table"] == portal_people), None)
    value = (middle.data or {}).get(on["key"]) if on else None
    return [int(x) for x in (value if isinstance(value, list) else [value]) if str(x).isdigit()]


join = Blueprint("join", __name__)


@join.route("/join/<int:wid>/<people>", methods=["GET", "POST"])
def sign_up(wid, people):
    """Outside people joining by themselves (a supplier registering): their record is made at its first stage (for the
    business to check them), with a portal account tied to it."""
    ws = db.session.get(Workspace, wid)
    portal = schema.portal_of(ws.blueprint or {}, people) if ws else None
    if portal is None or not portal.get("enabled", True) or "sign_up" not in portal:
        abort(404)
    engine = Engine(ws, by="sign-up")
    t = engine.tables[people]
    title = schema.title_field(t)
    asked = [k for k in [title] + portal["sign_up"]["fields"] if k and k != portal["email_field"]]
    fields = [f for f in (schema.field(t, k) for k in dict.fromkeys(asked)) if f and f["type"] in schema.STORED - {"autonumber", "user", "link"}]
    errors, values = {}, {}
    if request.method == "POST":
        values = request.form
        if request.form.get("website"):  # only robots fill this in
            return render_template("portal/join.html", ws=ws, portal=portal, t=t, fields=fields, values={}, errors={}, done=True)
        if not form_posts.allow(f"{client_ip()}:join:{wid}"):
            errors = {"_": "Too many sign-ups from here; please try again later."}
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        if not errors and ("@" not in email or len(email) > 255):
            errors = {"email": "Give your email address"}
        elif not errors and len(password) < 10:
            errors = {"password": "Use at least 10 characters"}
        elif not errors and User.query.filter_by(email=email).first() is not None:
            errors = {"email": "There's already an account with this email: sign in instead"}
        if not errors:
            data = {f["key"]: (bool(request.form.get(f["key"])) if f["type"] == "checkbox" else request.form.get(f["key"])) for f in fields}
            data[portal["email_field"]] = email
            try:
                with engine.atomic():
                    record = engine.create(people, data)
            except FieldErrors as exc:
                errors = exc.errors
            else:
                user = User(email=email, name=str(data.get(title) or email.split("@")[0])[:120])
                user.set_password(password)
                db.session.add(user)
                db.session.flush()
                db.session.add(Membership(workspace_id=ws.id, user_id=user.id, role="portal", contact_id=record.id))
                db.session.add(Notification(workspace_id=ws.id, link=f"table:{people}:record:{record.id}",
                                            text=f"{engine.title(record)} joined the {portal['title']} by themselves: check them"))
                db.session.commit()
                login_user(user)
                flash(f"Welcome. {engine.bp.get('name') or ws.name} will check your details; you can carry on meanwhile.", "success")
                return redirect(url_for("portal.home", wid=wid))
    return render_template("portal/join.html", ws=ws, portal=portal, t=t, fields=fields, values=values, errors=errors, done=False), \
        400 if errors else 200


@join.route("/c/<token>", methods=["GET", "POST"])
def cancel_by_link(token):
    """The emailed "this wasn't me" link: a change to a record waiting for approval is cancelled."""
    change = next((a for a in Approval.query.filter_by(kind="change", status="pending").all()
                   if (a.proposal or {}).get("token") == token), None)
    if request.method == "POST":
        done = automations.cancel_change(token) is not None
        return render_template("portal/cancelled.html", done=done, change=None)
    return render_template("portal/cancelled.html", done=None, change=change)
