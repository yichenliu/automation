"""The builder (chat on the left, the ERP on the right) and designing from Microsoft 365. Admins and owners only."""
from fastapi import APIRouter, Depends, HTTPException, Request

from .. import ai, discovery, jobs
from ..builder import chat
from ..connectors import microsoft
from ..extensions import db
from ..models import Job, Message
from ..security import member_of
from ..storage import FileRejected, save
from .http import (Form, PageRoute, check_csrf, current_user, flash, form_of, json_of, json_response, redirect, render, template_global,
                   url_for)

router = APIRouter(route_class=PageRoute, dependencies=[Depends(check_csrf)])


@template_global
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


@router.api_route("/w/{wid:int}/build", methods=["GET"], name="builder.build")
def build(request: Request, wid: int, ws=Depends(member_of("admin"))):
    args = request.query_params
    messages = Message.query.filter_by(workspace_id=ws.id, channel="builder").order_by(Message.id).all()
    return render(request, "builder/builder.html", ws=ws, messages=messages, starters=STARTERS, ai=ai.describe(),
                  ms=microsoft.status(ws), start=args.get("open") or f"/w/{ws.id}/app",
                  focus=args.get("start"),
                  say=(args.get("say") or ("" if messages or (ws.blueprint or {}).get("tables") else ws.about or ""))[:2000])


def _message_json(request, ws, m):
    return {"id": m.id, "status": m.status, "navigate": m.navigate, "version_after": m.version_after,
            "html": render(request, "builder/_message.html", ws=ws, m=m, ai=ai.describe()).body.decode("utf-8")}


@router.api_route("/w/{wid:int}/chat", methods=["POST"], name="builder.send")
def send(request: Request, wid: int, ws=Depends(member_of("admin")), form: Form = Depends(form_of), data=Depends(json_of)):
    user = current_user(request)
    text = (form.get("text") or (data or {}).get("text") or "").strip()
    page = (form.get("page") or (data or {}).get("page") or "")[:300]
    files = []
    for upload in form.files.getlist("files")[:5]:
        if upload and upload.filename:
            try:
                files.append(save(ws.id, upload, upload.filename, source="chat", by=user.display).id)
            except FileRejected as exc:
                return json_response({"error": str(exc)}, 400)
    if not text and not files:
        raise HTTPException(400)
    reply = chat.ask(ws, text or "Here's a file from my business.", page=page, user=user, files=files)
    said = Message.query.filter(Message.workspace_id == ws.id, Message.channel == "builder", Message.role == "user").order_by(Message.id.desc()).first()
    return json_response({"user": _message_json(request, ws, said), "reply": _message_json(request, ws, reply)})


@router.api_route("/w/{wid:int}/chat/{mid:int}", methods=["GET"], name="builder.message")
def message(request: Request, wid: int, mid: int, ws=Depends(member_of("admin"))):
    m = db.session.get(Message, mid)
    if m is None or m.workspace_id != ws.id or m.channel != "builder":
        raise HTTPException(404)
    return json_response(_message_json(request, ws, m))


@router.api_route("/w/{wid:int}/chat/{mid:int}/undo", methods=["POST"], name="builder.undo")
def undo(request: Request, wid: int, mid: int, ws=Depends(member_of("admin")), data=Depends(json_of)):
    user = current_user(request)
    m = db.session.get(Message, mid)
    if m is None or m.workspace_id != ws.id or m.version_before is None or not m.version_after:
        raise HTTPException(404)
    from ..models import Version
    later = Version.query.filter(Version.workspace_id == ws.id, Version.number > m.version_after).order_by(Version.number).all()
    what = (m.changes or ["that change"])[0]
    if later and not (data or {}).get("all"):  # an older change: what came after it goes too, said first
        named = "; ".join(f"“{(v.changes or ['a change'])[0][:80]}”" for v in later[:3]) + (f" and {len(later) - 3} more" if len(later) > 3 else "")
        return json_response({"confirm": f"Undoing “{what[:80]}” also undoes {'the change' if len(later) == 1 else f'the {len(later)} changes'} made after it: "
                                         f"{named}. Undo {'both' if len(later) == 1 else 'them all'}?"}, 409)
    try:
        number = chat.restore(ws, m.version_before, by=user.id, why=f"Undid “{what[:120]}”" + (f" and {len(later)} later change{'s' if len(later) != 1 else ''}" if later else ""))
    except ValueError:
        return json_response({"error": "That change can't be undone from here: the version before it isn't kept. Set up › Versions shows what can be."}, 400)
    binned, kept = chat.unmade(ws, m)
    text = "Undone. Your app is back to how it was before that change."
    if binned:
        text += (f" What it added went in the bin too: {', '.join(binned[:4])}{f' and {len(binned) - 4} more' if len(binned) > 4 else ''} "
                 f"(you can bring {'it' if len(binned) == 1 else 'them'} back from the bin).")
    if kept:
        text += f" Kept {', '.join(kept[:3])}: {'it has' if len(kept) == 1 else 'they have'} been used since."
    text += " Nothing your people entered was deleted."
    note = Message(workspace_id=ws.id, channel="builder", role="assistant", status="done", user_id=user.id,
                   text=text,
                   changes=[f"Undid “{what[:120]}”"],
                   version_before=number - 1, version_after=number)
    db.session.add(note)
    db.session.commit()
    return json_response(_message_json(request, ws, note))


@router.api_route("/w/{wid:int}/chat/{mid:int}/retry", methods=["POST"], name="builder.retry")
def retry(request: Request, wid: int, mid: int, ws=Depends(member_of("admin"))):
    m = db.session.get(Message, mid)
    if m is None or m.workspace_id != ws.id or m.status not in ("failed", "waiting_ai"):
        raise HTTPException(404)
    m.status, m.error, m.next_try = "queued", None, None
    db.session.commit()
    return json_response(_message_json(request, ws, m))


# --- designing from Microsoft 365 ------------------------------------------------------------------------------------

def _latest_discovery(ws):
    return Job.query.filter_by(workspace_id=ws.id, kind="discover").order_by(Job.id.desc()).first()


@router.api_route("/w/{wid:int}/discover", methods=["GET"], name="builder.discover")
def discover(request: Request, wid: int, ws=Depends(member_of("admin"))):
    return render(request, "builder/discover.html", ws=ws, ms=microsoft.status(ws), job=_latest_discovery(ws))


@router.api_route("/w/{wid:int}/discover/start", methods=["POST"], name="builder.discover_start")
def discover_start(request: Request, wid: int, ws=Depends(member_of("admin"))):
    if not microsoft.status(ws).get("connected"):
        flash(request, "Connect Microsoft 365 first.", "error")
        return redirect(url_for("builder.discover", wid=wid))
    jobs.enqueue("discover", {}, workspace_id=ws.id, dedupe=f"discover:{ws.id}", by=current_user(request).id)
    return redirect(url_for("builder.discover", wid=wid))


@router.api_route("/w/{wid:int}/jobs/{jid:int}", methods=["GET"], name="builder.job_status")
def job_status(request: Request, wid: int, jid: int, ws=Depends(member_of("viewer"))):
    job = db.session.get(Job, jid)
    if job is None or job.workspace_id != ws.id:
        raise HTTPException(404)
    return json_response({"status": job.status, "stage": job.stage, "progress": job.progress, "error": job.error})


@router.api_route("/w/{wid:int}/discover/{jid:int}/build", methods=["POST"], name="builder.discover_build")
def discover_build(request: Request, wid: int, jid: int, ws=Depends(member_of("admin"))):
    user = current_user(request)
    job = db.session.get(Job, jid)
    if job is None or job.workspace_id != ws.id or job.kind != "discover" or job.status != "done":
        raise HTTPException(404)
    try:
        number, imports = discovery.build(ws, job, user)
    except ValueError as exc:
        flash(request, str(exc), "error")
        return redirect(url_for("builder.discover", wid=wid))
    proposal = job.result or {}
    db.session.add(Message(workspace_id=ws.id, channel="builder", role="assistant", status="done", user_id=user.id,
                           text=proposal.get("reply") or "I built your app from what's in your Microsoft 365.",
                           changes=proposal.get("changes", []) + [f"Importing {i.payload.get('rows')} rows from {i.payload.get('name')}" for i in imports],
                           version_before=number - 1, version_after=number))
    db.session.commit()
    return redirect(url_for("builder.build", wid=wid))
