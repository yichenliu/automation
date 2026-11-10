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
from fastapi import APIRouter, Depends, HTTPException, Request

from ..blueprint import schema
from ..engine import automations, outside
from ..engine.records import Conflict, Engine, FieldErrors
from ..extensions import db
from ..models import Approval, Comment, File, Membership, Notification, User, Workspace
from ..security import client_ip, form_posts
from ..storage import path_of
from .http import Form, PageRoute, check_csrf, flash, form_of, login_user, redirect, render, send_file, signed_in, url_for

router = APIRouter(prefix="/portal/{wid:int}", route_class=PageRoute, dependencies=[Depends(check_csrf)])


def _ctx(wid, user):
    ws = db.session.get(Workspace, wid)
    member = Membership.query.filter_by(workspace_id=wid, user_id=user.id).first() if ws else None
    try:
        engine, portal, contact = outside.enter(ws, member, user.display)
    except outside.Gone:
        raise HTTPException(403) from None
    except outside.NotTheirs:
        raise HTTPException(404) from None
    return ws, engine, portal, contact


# what an outside person may see and do is the engine's (app/engine/outside.py), the same for the simulator
_tables, _fields, _mine, _locked, _editable, _lines, _options, _ids_reaching = (
    outside.tables, outside.fields, outside.mine, outside.locked, outside.editable, outside.lines, outside.options, outside.ids_reaching)


def _record(engine, contact, table, rid):
    try:
        return outside.find(engine, contact, table, rid)
    except outside.NotTheirs:
        raise HTTPException(404) from None


def _save_lines(form, engine, t, parent, editable_lines):
    from .erp_records import _posted_lines
    problems = []
    for child in editable_lines:
        key, via = child["table"]["key"], child["via"]["key"]
        for row in _posted_lines(child, form):
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


def _waiting(engine, ws, contact, user):
    """What's waiting for them: approvals and tasks sent to them, and changes to their records they can still cancel."""
    mine = Approval.query.filter_by(workspace_id=ws.id, status="pending", assignee_id=user.id).order_by(Approval.id).all()
    changes = [a for a in Approval.query.filter_by(workspace_id=ws.id, status="pending", kind="change").all()
               if (a.proposal or {}).get("token") and not (a.proposal or {}).get("confirmed") and (rec := engine.get(a.record_id)) is not None
               and (rec.id == contact.id or engine.portal_reaches(rec, contact.id))]
    return mine, list({a.run_id: a for a in changes}.values())


@router.api_route("/", methods=["GET"], name="portal.home")
def home(request: Request, wid: int, user=Depends(signed_in)):
    ws, engine, portal, contact = _ctx(wid, user)
    sections = []
    for t in _tables(engine, portal):
        rows = sorted(_mine(engine, t, contact), key=lambda r: r.id, reverse=True)
        sections.append({"t": t, "rows": rows[:100], "total": len(rows),
                         "fields": [f for f in _fields(t) if f["key"] != t["portal"]["via"].partition(".")[0]][:5],
                         "moves": {r.id: [m for _, m, problem in engine.moves(r) if not problem] for r in rows[:100]}})
    asks, changes = _waiting(engine, ws, contact, user)
    return render(request, "portal/home.html", ws=ws, e=engine, portal=portal, contact=contact, sections=sections, asks=asks,
                  changes=changes)


@router.api_route("/me", methods=["GET", "POST"], name="portal.profile")
def profile(request: Request, wid: int, user=Depends(signed_in), form: Form = Depends(form_of)):
    """Their own details (their phone, their bank account): a change to fields whose changes need approval waits for it."""
    ws, engine, portal, contact = _ctx(wid, user)
    t = engine.tables[contact.table]
    stage = (t.get("workflow") or {}).get("field")
    fields = [f for f in (schema.field(t, k) for k in portal.get("profile") or []) if f and not f.get("access") and f["key"] != stage]
    if "profile" not in portal or not fields:
        raise HTTPException(404)
    errors = {}
    if request.method == "POST":
        values = {f["key"]: (bool(form.get(f["key"])) if f["type"] == "checkbox" else form.get(f["key"])) for f in fields}
        try:
            engine.update(contact, values, expected_version=form.get("_version"))
        except Conflict:
            errors = {"_": "These details changed while you were looking. Here they are now; make your change again."}
        except FieldErrors as exc:
            errors = exc.errors
        else:
            if engine.held:
                flash(request, f"Thanks. A change to {', '.join(engine.held)} is checked by {engine.bp.get('name') or ws.name} before it's used; "
                      "we'll tell you when it is.", "info")
            else:
                flash(request, "Saved.", "success")
            return redirect(url_for("portal.profile", wid=wid))
    contact = engine.get(contact.id)
    pending = [a for a in Approval.query.filter_by(workspace_id=ws.id, record_id=contact.id, kind="change", status="pending").all()]
    return render(request, "portal/profile.html", status_code=400 if errors else 200, ws=ws, e=engine, portal=portal, contact=contact, t=t,
                  fields=fields, errors=errors, pending=pending[:1], options=_options(engine, fields, contact))


@router.api_route("/t/{table}/{rid:int}", methods=["GET"], name="portal.record")
def record(request: Request, wid: int, table: str, rid: int, user=Depends(signed_in)):
    ws, engine, portal, contact = _ctx(wid, user)
    t, r = _record(engine, contact, table, rid)
    from .erp_records import audited
    if audited(engine, t):
        from ..models import RecordChange
        db.session.add(RecordChange(workspace_id=ws.id, record_id=r.id, kind="viewed", by=f"{user.display} (through the portal)",
                                    by_id=user.id))
        db.session.commit()
    editable = _editable(engine, t, r)
    lines = []
    for child in _lines(engine, t):
        lines.append({**child, "rows": engine.children(child["t"]["key"], child["via"]["key"], r.id)})
    shown = {f["key"] for f in _fields(t) if f["type"] in schema.FILES}
    files = [x for x in File.query.filter(File.record_id == r.id, File.deleted_at.is_(None)).order_by(File.id.desc()) if x.field in shown]
    messages = Comment.query.filter_by(record_id=r.id, shared=True).order_by(Comment.id).all()
    asks = Approval.query.filter_by(workspace_id=ws.id, record_id=r.id, status="pending", assignee_id=user.id).all()
    return render(request, "portal/record.html", ws=ws, e=engine, portal=portal, contact=contact, t=t, r=r,
                  fields=[f for f in _fields(t) if f["key"] != t["portal"]["via"].partition(".")[0]], lines=lines, files=files,
                  messages=messages, documents=[d for d in t.get("documents", []) if d.get("format") != "text"],
                  editable=editable, moves=engine.moves(r), asks=asks, locked=_locked(engine, t, r),
                  sealed=engine.seal_note(r), options=_options(engine, editable + [f for c in lines for f in c["cols"]], contact),
                  edit_lines=bool(editable) and bool(lines))


@router.api_route("/t/{table}/{rid:int}/edit", methods=["POST"], name="portal.edit")
def edit(request: Request, wid: int, table: str, rid: int, user=Depends(signed_in), form: Form = Depends(form_of)):
    """Their part of a record (a bid's prices, while the tender is open), and its lines."""
    ws, engine, portal, contact = _ctx(wid, user)
    t, r = _record(engine, contact, table, rid)
    editable = _editable(engine, t, r)
    if not editable:
        flash(request, _locked(engine, t, r) or "This can't be changed here now.", "error")
        return redirect(url_for("portal.record", wid=wid, table=table, rid=rid))
    values = {f["key"]: (bool(form.get(f["key"])) if f["type"] == "checkbox" else
                         form.getlist(f["key"]) if f["type"] == "multi_select" else form.get(f["key"])) for f in editable}
    try:
        with engine.atomic():
            outside.change(engine, contact, t, r, values, expected_version=form.get("_version"))
            problems = _save_lines(form, engine, t, r, _lines(engine, t))
            if problems:
                raise FieldErrors({"_": "; ".join(problems)})
    except Conflict:
        flash(request, "This changed while you were editing it. Here it is now; make your change again.", "error")
    except FieldErrors as exc:
        flash(request, "; ".join(exc.errors.values()), "error")
    else:
        held = f" A change to {', '.join(engine.held)} is checked before it counts." if engine.held else ""
        flash(request, "Saved." + held, "success")
    return redirect(url_for("portal.record", wid=wid, table=table, rid=rid))


@router.api_route("/t/{table}/{rid:int}/move/{index:int}", methods=["POST"], name="portal.move")
def move(request: Request, wid: int, table: str, rid: int, index: int, user=Depends(signed_in)):
    """A move meant for them: Acknowledge the order, Accept the variation, Submit the bid."""
    ws, engine, portal, contact = _ctx(wid, user)
    t, r = _record(engine, contact, table, rid)
    try:
        outside.move(engine, contact, t, r, index, by=f"{user.display} (through the portal)")
    except (FieldErrors, Conflict) as exc:
        flash(request, "; ".join(exc.errors.values()) if isinstance(exc, FieldErrors) else "This changed just now; try again.", "error")
    else:
        flash(request, f"Done: {engine.get(r.id).data.get(t['workflow']['field'])}.", "success")
    return redirect(url_for("portal.record", wid=wid, table=table, rid=rid))


@router.api_route("/asks/{aid:int}", methods=["POST"], name="portal.answer")
def answer(request: Request, wid: int, aid: int, user=Depends(signed_in), form: Form = Depends(form_of)):
    """An approval or task sent to them (sign the contract, confirm the delivery date), or a change to their records a
    colleague made that waits for someone else of theirs to approve it (a second person confirms new bank details)."""
    ws, engine, portal, contact = _ctx(wid, user)
    ask = db.session.get(Approval, aid)
    if ask is None or ask.workspace_id != ws.id or ask.assignee_id != user.id or ask.status != "pending":
        raise HTTPException(404)
    decision = form.get("decision")
    by = f"{user.display} (through the portal)"
    try:
        if decision == "done" and ask.kind == "task":
            record = engine.get(ask.record_id) if ask.record_id else None
            values = {k: form.get(k) for k in ask.fields or []} if record is not None else {}
            automations.complete(engine, ask, by, values, form.get("comment"))
        elif decision in ("approve", "reject") and (ask.kind or "approval") == "approval":
            automations.decide(engine, ask, decision == "approve", by, form.get("comment"))
        elif decision in ("approve", "reject") and ask.kind == "change":  # never their own: the maker isn't asked
            automations.decide_change(engine, ask, decision == "approve", by, form.get("comment"))
        else:
            raise HTTPException(400)
    except FieldErrors as exc:
        db.session.rollback()
        flash(request, "; ".join(exc.errors.values()), "error")
    else:
        flash(request, "Thanks: that's done.", "success")
    back = form.get("back")
    return redirect(back if back and back.startswith("/portal/") else url_for("portal.home", wid=wid))


@router.api_route("/changes/{aid:int}/confirm", methods=["POST"], name="portal.confirm")
def confirm(request: Request, wid: int, aid: int, user=Depends(signed_in)):
    """"Yes, it was me": the change still waits for the business's approval; it's no longer asked about here."""
    ws, engine, portal, contact = _ctx(wid, user)
    _, changes = _waiting(engine, ws, contact, user)
    change = next((a for a in changes if a.id == aid), None)
    if change is None:
        raise HTTPException(404)
    change.proposal = {**change.proposal, "confirmed": f"{user.display} said it was them"}
    change.history = (change.history or []) + [{"when": automations.utcnow().isoformat(), "what": f"{user.display} confirmed it was them"}]
    db.session.commit()
    flash(request, "Thanks. We'll start using it once our team has checked it.", "success")
    return redirect(url_for("portal.home", wid=wid))


@router.api_route("/changes/{aid:int}/cancel", methods=["POST"], name="portal.cancel")
def cancel(request: Request, wid: int, aid: int, user=Depends(signed_in)):
    """"This wasn't me": a change to their details that's still waiting is cancelled."""
    ws, engine, portal, contact = _ctx(wid, user)
    _, changes = _waiting(engine, ws, contact, user)
    change = next((a for a in changes if a.id == aid), None)
    if change is None:
        raise HTTPException(404)
    automations.cancel_change(change.proposal["token"])
    flash(request, "Cancelled. Nothing was changed, and we've told the team.", "success")
    return redirect(url_for("portal.home", wid=wid))


@router.api_route("/t/{table}/{rid:int}/message", methods=["POST"], name="portal.message")
def message(request: Request, wid: int, table: str, rid: int, user=Depends(signed_in), form: Form = Depends(form_of)):
    ws, engine, portal, contact = _ctx(wid, user)
    t, r = _record(engine, contact, table, rid)
    text = (form.get("text") or "").strip()[:5000]
    if text:
        db.session.add(Comment(workspace_id=ws.id, record_id=r.id, user_id=user.id, text=text, shared=True))
        db.session.add(Notification(workspace_id=ws.id, kind="mention", link=f"table:{t['key']}:record:{r.id}",
                                    text=f"{engine.title(contact)} wrote on {t['singular']} “{engine.title(r)}”: {text[:200]}"))
        db.session.commit()
        flash(request, "Sent. We'll reply here.", "success")
    return redirect(url_for("portal.record", wid=wid, table=table, rid=rid) + "#messages")


@router.api_route("/t/{table}/{rid:int}/document/{key}", methods=["GET"], name="portal.document")
def document(request: Request, wid: int, table: str, rid: int, key: str, user=Depends(signed_in)):
    from ..documents import produce
    ws, engine, portal, contact = _ctx(wid, user)
    t, r = _record(engine, contact, table, rid)
    if not any(d["key"] == key and d.get("format") != "text" for d in t.get("documents", [])):
        raise HTTPException(404)
    file = produce(engine, r, key, by=f"{user.display} (through the portal)")
    return send_file(request, path_of(file), mimetype="application/pdf", download_name=file.name)


@router.api_route("/files/{fid:int}", methods=["GET"], name="portal.file")
def file(request: Request, wid: int, fid: int, user=Depends(signed_in)):
    ws, engine, portal, contact = _ctx(wid, user)
    f = db.session.get(File, fid)
    if f is None or f.workspace_id != ws.id or f.deleted_at is not None or not f.record_id:
        raise HTTPException(404)
    owner = engine.get(f.record_id)
    t = engine.tables.get(owner.table) if owner else None
    if owner is None or not t or not t.get("portal") or not engine.portal_reaches(owner, contact.id) \
            or f.field not in {x["key"] for x in _fields(t) if x["type"] in schema.FILES}:
        raise HTTPException(404)
    from .erp_records import send_stored
    return send_stored(f, request=request)


@router.api_route("/t/{table}/new", methods=["GET", "POST"], name="portal.new")
def new(request: Request, wid: int, table: str, user=Depends(signed_in), form: Form = Depends(form_of)):
    """Adding a record from the portal (a job request, a bid, an invoice with its lines): it's theirs from the start."""
    ws, engine, portal, contact = _ctx(wid, user)
    try:
        adding = outside.addable(engine, portal, contact, table)
    except outside.NotTheirs:
        raise HTTPException(404) from None
    t, fields, lines, options = adding["t"], adding["fields"], adding["lines"], adding["options"]
    errors, values = {}, dict(request.query_params) if request.method == "GET" else {}
    if request.method == "POST":
        values = {f["key"]: form.get(f["key"]) if f["type"] != "checkbox" else bool(form.get(f["key"])) for f in fields}
        if any(f["type"] == "multi_select" for f in fields):
            values.update({f["key"]: form.getlist(f["key"]) for f in fields if f["type"] == "multi_select"})
        try:
            r = outside.add(engine, portal, contact, table, values, then=lambda made, lines_: _save_lines(form, engine, t, made, lines_))
        except FieldErrors as exc:
            errors = exc.errors
        else:
            db.session.add(Notification(workspace_id=ws.id, link=f"table:{t['key']}:record:{r.id}",
                                        text=f"{engine.title(contact)} added {schema.lc(t['singular'])} “{engine.title(r)}” in the portal"))
            db.session.commit()
            flash(request, f"Thanks: your {schema.lc(t['singular'])} is in.", "success")
            return redirect(url_for("portal.record", wid=wid, table=table, rid=r.id))
    return render(request, "portal/new.html", status_code=400 if errors else 200, ws=ws, e=engine, portal=portal, contact=contact, t=t,
                  fields=fields, values=values, errors=errors, lines=lines, options=options)


join = APIRouter(route_class=PageRoute, dependencies=[Depends(check_csrf)])


@join.api_route("/join/{wid:int}/{people}", methods=["GET", "POST"], name="join.sign_up")
def sign_up(request: Request, wid: int, people: str, form: Form = Depends(form_of)):
    """Outside people joining by themselves (a supplier registering): their record is made at its first stage (for the
    business to check them), with a portal account tied to it."""
    ws = db.session.get(Workspace, wid)
    portal = schema.portal_of(ws.blueprint or {}, people) if ws else None
    if portal is None or not portal.get("enabled", True) or "sign_up" not in portal:
        raise HTTPException(404)
    engine = Engine(ws, by="sign-up")
    t = engine.tables[people]
    title = schema.title_field(t)
    asked = [k for k in [title] + portal["sign_up"]["fields"] if k and k != portal["email_field"]]
    fields = [f for f in (schema.field(t, k) for k in dict.fromkeys(asked)) if f and f["type"] in schema.STORED - {"autonumber", "user", "link"}]
    errors, values = {}, {}
    if request.method == "POST":
        values = form
        if form.get("website"):  # only robots fill this in
            return render(request, "portal/join.html", ws=ws, portal=portal, t=t, fields=fields, values={}, errors={}, done=True)
        if not form_posts.allow(f"{client_ip(request)}:join:{wid}"):
            errors = {"_": "Too many sign-ups from here; please try again later."}
        email = (form.get("email") or "").strip().lower()
        password = form.get("password") or ""
        if not errors and ("@" not in email or len(email) > 255):
            errors = {"email": "Give your email address"}
        elif not errors and len(password) < 10:
            errors = {"password": "Use at least 10 characters"}
        elif not errors and User.query.filter_by(email=email).first() is not None:
            errors = {"email": "There's already an account with this email: sign in instead"}
        if not errors:
            data = {f["key"]: (bool(form.get(f["key"])) if f["type"] == "checkbox" else form.get(f["key"])) for f in fields}
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
                login_user(request, user)
                flash(request, f"Welcome. {engine.bp.get('name') or ws.name} will check your details; you can carry on meanwhile.", "success")
                return redirect(url_for("portal.home", wid=wid))
    return render(request, "portal/join.html", status_code=400 if errors else 200, ws=ws, portal=portal, t=t, fields=fields, values=values,
                  errors=errors, done=False)


@join.api_route("/c/{token}", methods=["GET", "POST"], name="join.cancel_by_link")
def cancel_by_link(request: Request, token: str):
    """The emailed "this wasn't me" link: a change to a record waiting for approval is cancelled."""
    change = next((a for a in Approval.query.filter_by(kind="change", status="pending").all()
                   if (a.proposal or {}).get("token") == token), None)
    if request.method == "POST":
        done = automations.cancel_change(token) is not None
        return render(request, "portal/cancelled.html", done=done, change=None)
    return render(request, "portal/cancelled.html", done=None, change=change)
