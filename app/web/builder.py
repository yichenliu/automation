"""The builder (chat on the left, the ERP on the right) and designing from Microsoft 365. Admins and owners only."""
from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from .. import ai, discovery, jobs
from ..builder import chat
from ..connectors import microsoft
from ..extensions import db
from ..models import Job, Message
from ..security import workspace
from ..storage import FileRejected, save

bp = Blueprint("builder", __name__)


@bp.app_template_global()
def build_minutes(first):
    """“15 to 45 minutes”: what a builder turn takes on this computer (chat.typical_minutes)."""
    low, high = chat.typical_minutes(bool(first))
    return f"{low} to {high} minutes"

STARTERS = [
    "I run a small data consultancy: clients, projects, timesheets and invoices with GST.",
    "A café with two locations: suppliers, stock orders, a weekly stocktake and daily sales.",
    "Rental properties: properties, tenants, leases, rent payments and maintenance jobs.",
    "A trades business: customer enquiries, quotes with line items, scheduled jobs and invoices.",
]


@bp.route("/w/<int:wid>/build")
@login_required
def build(wid):
    ws = workspace(wid, "admin")
    messages = Message.query.filter_by(workspace_id=ws.id, channel="builder").order_by(Message.id).all()
    return render_template("builder/builder.html", ws=ws, messages=messages, starters=STARTERS, ai=ai.describe(),
                           ms=microsoft.status(ws), start=request.args.get("open") or f"/w/{ws.id}/app",
                           focus=request.args.get("start"),
                           say=(request.args.get("say") or ("" if messages or (ws.blueprint or {}).get("tables") else ws.about or ""))[:2000])


def _message_json(ws, m):
    return {"id": m.id, "status": m.status, "navigate": m.navigate, "version_after": m.version_after,
            "html": render_template("builder/_message.html", ws=ws, m=m, ai=ai.describe())}


@bp.route("/w/<int:wid>/chat", methods=["POST"])
@login_required
def send(wid):
    ws = workspace(wid, "admin")
    text = (request.form.get("text") or (request.get_json(silent=True) or {}).get("text") or "").strip()
    page = (request.form.get("page") or (request.get_json(silent=True) or {}).get("page") or "")[:300]
    files = []
    for upload in request.files.getlist("files")[:5]:
        if upload and upload.filename:
            try:
                files.append(save(ws.id, upload, upload.filename, source="chat", by=current_user.display).id)
            except FileRejected as exc:
                return jsonify(error=str(exc)), 400
    if not text and not files:
        abort(400)
    reply = chat.ask(ws, text or "Here's a file from my business.", page=page, user=current_user, files=files)
    user = Message.query.filter(Message.workspace_id == ws.id, Message.channel == "builder", Message.role == "user").order_by(Message.id.desc()).first()
    return jsonify(user=_message_json(ws, user), reply=_message_json(ws, reply))


@bp.route("/w/<int:wid>/chat/<int:mid>")
@login_required
def message(wid, mid):
    ws = workspace(wid, "admin")
    m = db.session.get(Message, mid)
    if m is None or m.workspace_id != ws.id or m.channel != "builder":
        abort(404)
    return jsonify(_message_json(ws, m))


@bp.route("/w/<int:wid>/chat/<int:mid>/undo", methods=["POST"])
@login_required
def undo(wid, mid):
    ws = workspace(wid, "admin")
    m = db.session.get(Message, mid)
    if m is None or m.workspace_id != ws.id or m.version_before is None or not m.version_after:
        abort(404)
    number = chat.restore(ws, m.version_before, by=current_user.id)
    binned, kept = chat.unmade(ws, m)
    text = "Undone. The design is back to how it was before that change."
    if binned:
        text += (f" What it added went in the bin too: {', '.join(binned[:4])}{f' and {len(binned) - 4} more' if len(binned) > 4 else ''} "
                 f"(you can bring {'it' if len(binned) == 1 else 'them'} back from the bin).")
    if kept:
        text += f" Kept {', '.join(kept[:3])}: {'it has' if len(kept) == 1 else 'they have'} been used since."
    text += " Nothing your people entered was touched."
    note = Message(workspace_id=ws.id, channel="builder", role="assistant", status="done", user_id=current_user.id,
                   text=text,
                   changes=[f"Went back to the design as it was at version {m.version_before}"],
                   version_before=number - 1, version_after=number)
    db.session.add(note)
    db.session.commit()
    return jsonify(_message_json(ws, note))


@bp.route("/w/<int:wid>/chat/<int:mid>/retry", methods=["POST"])
@login_required
def retry(wid, mid):
    ws = workspace(wid, "admin")
    m = db.session.get(Message, mid)
    if m is None or m.workspace_id != ws.id or m.status not in ("failed", "waiting_ai"):
        abort(404)
    m.status, m.error, m.next_try = "queued", None, None
    db.session.commit()
    return jsonify(_message_json(ws, m))


# --- designing from Microsoft 365 ------------------------------------------------------------------------------------

def _latest_discovery(ws):
    return Job.query.filter_by(workspace_id=ws.id, kind="discover").order_by(Job.id.desc()).first()


@bp.route("/w/<int:wid>/discover")
@login_required
def discover(wid):
    ws = workspace(wid, "admin")
    return render_template("builder/discover.html", ws=ws, ms=microsoft.status(ws), job=_latest_discovery(ws))


@bp.route("/w/<int:wid>/discover/start", methods=["POST"])
@login_required
def discover_start(wid):
    ws = workspace(wid, "admin")
    if not microsoft.status(ws).get("connected"):
        flash("Connect Microsoft 365 first.", "error")
        return redirect(url_for(".discover", wid=wid))
    jobs.enqueue("discover", {}, workspace_id=ws.id, dedupe=f"discover:{ws.id}", by=current_user.id)
    return redirect(url_for(".discover", wid=wid))


@bp.route("/w/<int:wid>/jobs/<int:jid>")
@login_required
def job_status(wid, jid):
    ws = workspace(wid, "viewer")
    job = db.session.get(Job, jid)
    if job is None or job.workspace_id != ws.id:
        abort(404)
    return jsonify(status=job.status, stage=job.stage, progress=job.progress, error=job.error)


@bp.route("/w/<int:wid>/discover/<int:jid>/build", methods=["POST"])
@login_required
def discover_build(wid, jid):
    ws = workspace(wid, "admin")
    job = db.session.get(Job, jid)
    if job is None or job.workspace_id != ws.id or job.kind != "discover" or job.status != "done":
        abort(404)
    try:
        number, imports = discovery.build(ws, job, current_user)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for(".discover", wid=wid))
    proposal = job.result or {}
    db.session.add(Message(workspace_id=ws.id, channel="builder", role="assistant", status="done", user_id=current_user.id,
                           text=proposal.get("reply") or "I built your app from what's in your Microsoft 365.",
                           changes=proposal.get("changes", []) + [f"Importing {i.payload.get('rows')} rows from {i.payload.get('name')}" for i in imports],
                           version_before=number - 1, version_after=number))
    db.session.commit()
    return redirect(url_for(".build", wid=wid))
