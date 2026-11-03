"""The AI inside the ERP: the assistant, what it knows (documents, mail, records it can look through), adding a record
from a document (an invoice, a receipt, an email), and importing spreadsheets."""
from flask import abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from .. import ai, assistant, importer, jobs, knowledge
from ..blueprint import schema
from ..extensions import db
from ..models import File, Job, Message
from ..storage import FileRejected, save
from .erp import bp, ctx
from .erp_records import _render_form, _table, _ran


def _assistant_json(ws, m):
    return {"id": m.id, "status": m.status,
            "html": render_template("app/_assistant_message.html", ws=ws, m=m, ai=ai.describe())}


@bp.route("/assistant")
@login_required
def assistant_history(wid):
    ws, engine = ctx(wid)
    messages = (Message.query.filter_by(workspace_id=ws.id, channel="assistant", user_id=current_user.id)
                .order_by(Message.id.desc()).limit(30).all())
    return jsonify(html="".join(_assistant_json(ws, m)["html"] for m in reversed(messages)),
                   pending=next((m.id for m in messages if m.role == "assistant" and m.status in ("queued", "thinking", "waiting_ai")), None))


@bp.route("/assistant", methods=["POST"])
@login_required
def assistant_ask(wid):
    ws, engine = ctx(wid)
    data = request.get_json(silent=True) or request.form
    text = (data.get("text") or "").strip()
    if not text:
        abort(400)
    reply = assistant.ask(ws, current_user, text, page=(data.get("page") or "")[:300])
    question = Message.query.filter_by(workspace_id=ws.id, channel="assistant", user_id=current_user.id, role="user").order_by(Message.id.desc()).first()
    return jsonify(user=_assistant_json(ws, question), reply=_assistant_json(ws, reply))


@bp.route("/assistant/<int:mid>")
@login_required
def assistant_poll(wid, mid):
    ws, engine = ctx(wid)
    m = db.session.get(Message, mid)
    if m is None or m.workspace_id != ws.id or m.user_id != current_user.id:
        abort(404)
    return jsonify(_assistant_json(ws, m))


@bp.route("/assistant/<int:mid>/confirm/<int:index>", methods=["POST"])
@login_required
def assistant_confirm(wid, mid, index):
    ws, engine = ctx(wid, "editor")
    m = db.session.get(Message, mid)
    if m is None or m.workspace_id != ws.id or m.user_id != current_user.id or index >= len(m.proposals or []):
        abort(404)
    item, engine = assistant.confirm(m, index, current_user)
    navigate = url_for("app.record", wid=wid, table=item["table"], rid=item["record_id"]) if item.get("record_id") else None
    return jsonify({**_assistant_json(ws, m), "navigate": navigate})


@bp.route("/assistant/<int:mid>/dismiss/<int:index>", methods=["POST"])
@login_required
def assistant_dismiss(wid, mid, index):
    ws, engine = ctx(wid)
    m = db.session.get(Message, mid)
    if m is None or m.workspace_id != ws.id or m.user_id != current_user.id or index >= len(m.proposals or []):
        abort(404)
    proposals = list(m.proposals)
    proposals[index] = {**proposals[index], "status": "dismissed"}
    m.proposals = proposals
    db.session.commit()
    return jsonify(_assistant_json(ws, m))


# --- adding a record from a document ---------------------------------------------------------------------------------

@bp.route("/t/<table>/from-document", methods=["GET", "POST"])
@login_required
def intake(wid, table):
    ws, engine = ctx(wid, "editor")
    t = _table(engine, table, "edit")
    if request.method == "POST":
        ids = []
        for up in request.files.getlist("files")[:6]:
            if up and up.filename:
                try:
                    ids.append(save(ws.id, up, up.filename, source="upload", by=current_user.display).id)
                except FileRejected as exc:
                    flash(str(exc), "error")
        if not ids:
            flash("Choose a document to read (a PDF, a photo or scan, a Word file or an email).", "error")
            return redirect(url_for("app.intake", wid=wid, table=table))
        job = jobs.enqueue("intake", {"table": t["key"], "file_ids": ids, "instructions": (request.form.get("instructions") or "").strip()[:500]},
                           workspace_id=ws.id, by=current_user.id)
        return redirect(url_for("app.intake_review", wid=wid, table=table, jid=job.id))
    return render_template("app/intake.html", ws=ws, e=engine, t=t)


@bp.route("/t/<table>/from-document/<int:jid>")
@login_required
def intake_review(wid, table, jid):
    ws, engine = ctx(wid, "editor")
    t = _table(engine, table, "edit")
    job = db.session.get(Job, jid)
    if job is None or job.workspace_id != ws.id or job.kind != "intake":
        abort(404)
    files = File.query.filter(File.id.in_(job.payload["file_ids"])).all()
    if job.status != "done":
        return render_template("app/intake_wait.html", ws=ws, e=engine, t=t, job=job, files=files)
    result = job.result or {}
    line_rows = {k: [{"id": None, "values": v} for v in rows] for k, rows in (result.get("lines") or {}).items()}
    unsure = set(result.get("unsure") or [])
    return _render_form(ws, engine, t, None, result.get("values") or {}, result.get("problems") or {}, line_rows,
                        intake={"files": files, "unsure": unsure}, attach=[f.id for f in files])[0]


# --- importing a spreadsheet -----------------------------------------------------------------------------------------

@bp.route("/t/<table>/import", methods=["GET", "POST"])
@login_required
def import_start(wid, table):
    ws, engine = ctx(wid, "editor")
    t = _table(engine, table, "edit")
    if request.method == "POST":
        up = request.files.get("file")
        if not up or not up.filename:
            flash("Choose a CSV or Excel file.", "error")
            return redirect(url_for("app.import_start", wid=wid, table=table))
        try:
            f = save(ws.id, up, up.filename, source="upload", by=current_user.display)
            importer.preview(f)
        except (FileRejected, ValueError) as exc:
            flash(str(exc), "error")
            return redirect(url_for("app.import_start", wid=wid, table=table))
        return redirect(url_for("app.import_map", wid=wid, table=table, fid=f.id))
    return render_template("app/import.html", ws=ws, e=engine, t=t, step="upload")


@bp.route("/t/<table>/import/<int:fid>", methods=["GET", "POST"])
@login_required
def import_map(wid, table, fid):
    ws, engine = ctx(wid, "editor")
    t = _table(engine, table, "edit")
    f = db.session.get(File, fid)
    from .erp_records import may_open_file
    if f is None or f.workspace_id != ws.id or not may_open_file(engine, f):
        abort(404)
    sheet = request.values.get("sheet")
    try:
        info = importer.preview(f, sheet)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("app.import_start", wid=wid, table=table))
    if request.method == "POST" and request.form.get("go"):
        mapping = {h: request.form.get(f"map-{i}") for i, h in enumerate(info["headers"]) if request.form.get(f"map-{i}")}
        if not mapping:
            flash("Match at least one column to a field.", "error")
        else:
            match_on = request.form.get("match_on") if schema.field(t, request.form.get("match_on") or "") else None
            job = importer.start(ws, f.id, t["key"], mapping, info["sheet"], by=current_user.id, match_on=match_on)
            return redirect(url_for("app.import_progress", wid=wid, table=table, jid=job.id))
    mapping = importer.suggest(engine, t["key"], info["headers"], info["rows"])
    fields = [x for x in t["fields"] if x["type"] in importer.IMPORTABLE]
    return render_template("app/import.html", ws=ws, e=engine, t=t, step="map", file=f, info=info, mapping=mapping, fields=fields)


@bp.route("/t/<table>/import/progress/<int:jid>")
@login_required
def import_progress(wid, table, jid):
    ws, engine = ctx(wid, "editor")
    t = _table(engine, table, "edit")
    job = db.session.get(Job, jid)
    if job is None or job.workspace_id != ws.id or job.kind != "import":
        abort(404)
    return render_template("app/import.html", ws=ws, e=engine, t=t, step="progress", job=job)


# --- what the AI knows -----------------------------------------------------------------------------------------------

@bp.route("/knowledge", methods=["GET", "POST"])
@login_required
def knowledge_page(wid):
    """Documents the AI should know (policies, price lists, contracts, manuals), learning from Microsoft 365, and a
    place to try a question and see what the AI would find."""
    from ..connectors import microsoft
    from ..models import utcnow
    from flask import g
    ws, engine = ctx(wid)
    member = g.member
    if request.method == "POST":
        action = request.form.get("action")
        if action == "upload":
            if not member.can("editor"):
                abort(403)
            added = []
            for up in request.files.getlist("files")[:20]:
                if up and up.filename:
                    try:
                        added.append(save(ws.id, up, up.filename, source="knowledge", by=current_user.display).name)
                    except FileRejected as exc:
                        flash(str(exc), "error")
            if added:
                flash(f"Added {', '.join(added)}. The AI is reading {'it' if len(added) == 1 else 'them'} now; it takes a moment.", "success")
        elif action == "remove":
            if not member.can("editor"):
                abort(403)
            f = db.session.get(File, int(request.form.get("file") or 0))
            if f is None or f.workspace_id != ws.id or f.source != "knowledge":
                abort(404)
            f.deleted_at = utcnow()
            db.session.commit()
            knowledge.touch(ws.id, "file", f.id)
            flash(f"Removed {f.name}; the AI no longer uses it.", "success")
        elif action in ("mail_on", "mail_off", "mail_history", "onedrive", "rebuild"):
            if not member.can("admin"):
                abort(403)
            if action in ("mail_on", "mail_off"):
                settings = dict(ws.settings or {})
                settings["knowledge"] = {**(settings.get("knowledge") or {}), "mail": action == "mail_on"}
                ws.settings = settings
                db.session.commit()
                flash("The AI will read new mail as it arrives." if action == "mail_on" else "The AI won't read new mail.", "success")
            elif action == "mail_history":
                jobs.enqueue("knowledge_mail", {"days": 180}, workspace_id=ws.id, dedupe=f"knowledge_mail:{ws.id}", by=current_user.id)
                flash("Reading the last six months of mail in the background.", "success")
            elif action == "onedrive":
                folder = (request.form.get("folder") or "").strip().strip("/")
                jobs.enqueue("knowledge_onedrive", {"folder": folder}, workspace_id=ws.id, dedupe=f"knowledge_onedrive:{ws.id}:{folder}", by=current_user.id)
                flash(f"Reading the documents in {folder or 'your OneDrive'} in the background.", "success")
            else:
                jobs.enqueue("knowledge_rebuild", workspace_id=ws.id, dedupe=f"knowledge_rebuild:{ws.id}", by=current_user.id)
                flash("Reading everything again in the background.", "success")
        return redirect(url_for("app.knowledge_page", wid=wid))
    q = (request.args.get("q") or "").strip()
    hits = knowledge.search(engine, q, k=8) if q else []
    documents = File.query.filter_by(workspace_id=ws.id, source="knowledge", deleted_at=None).order_by(File.id.desc()).all()
    from ..models import Chunk
    read = {ref for (ref,) in db.session.query(Chunk.ref).filter(Chunk.workspace_id == ws.id, Chunk.source == "file").distinct()}
    background = Job.query.filter(Job.workspace_id == ws.id, Job.kind.in_(["knowledge_mail", "knowledge_onedrive", "knowledge_rebuild"])) \
        .order_by(Job.id.desc()).limit(5).all()
    return render_template("app/knowledge.html", ws=ws, e=engine, member=member, status=knowledge.status(ws), q=q, hits=hits,
                           documents=documents, read=read, background=background,
                           mail_on=bool(((ws.settings or {}).get("knowledge") or {}).get("mail")),
                           connected=microsoft.status(ws).get("connected") if microsoft.configured() else False)
