"""The AI inside the ERP: the assistant, what it knows (documents, mail, records it can look through), adding a record
from a document (an invoice, a receipt, an email), and importing spreadsheets."""
from fastapi import APIRouter, Depends, HTTPException, Request

from .. import ai, assistant, importer, jobs, knowledge
from ..blueprint import schema
from ..extensions import db
from ..models import File, Job, Message
from ..storage import FileRejected, save
from .erp import ctx
from .erp_records import _render_form, _table
from .http import Form, PageRoute, check_csrf, flash, form_of, json_of, json_response, redirect, render, signed_in, url_for

router = APIRouter(prefix="/w/{wid:int}/app", route_class=PageRoute, dependencies=[Depends(check_csrf), Depends(signed_in)])


def _assistant_json(request, ws, m):
    return {"id": m.id, "status": m.status,
            "html": render(request, "app/_assistant_message.html", ws=ws, m=m, ai=ai.describe()).body.decode("utf-8")}


@router.api_route("/assistant", methods=["GET"], name="app.assistant_history")
def assistant_history(request: Request, wid: int, user=Depends(signed_in)):
    ws, engine = ctx(wid, "viewer", request)
    messages = (Message.query.filter_by(workspace_id=ws.id, channel="assistant", user_id=user.id)
                .order_by(Message.id.desc()).limit(30).all())
    return json_response({"html": "".join(_assistant_json(request, ws, m)["html"] for m in reversed(messages)),
                          "pending": next((m.id for m in messages if m.role == "assistant" and m.status in ("queued", "thinking", "waiting_ai")), None)})


@router.api_route("/assistant", methods=["POST"], name="app.assistant_ask")
def assistant_ask(request: Request, wid: int, user=Depends(signed_in), body=Depends(json_of), form: Form = Depends(form_of)):
    ws, engine = ctx(wid, "viewer", request)
    data = body or form
    text = (data.get("text") or "").strip()
    if not text:
        raise HTTPException(400)
    reply = assistant.ask(ws, user, text, page=(data.get("page") or "")[:300])
    question = Message.query.filter_by(workspace_id=ws.id, channel="assistant", user_id=user.id, role="user").order_by(Message.id.desc()).first()
    return json_response({"user": _assistant_json(request, ws, question), "reply": _assistant_json(request, ws, reply)})


@router.api_route("/assistant/{mid:int}", methods=["GET"], name="app.assistant_poll")
def assistant_poll(request: Request, wid: int, mid: int, user=Depends(signed_in)):
    ws, engine = ctx(wid, "viewer", request)
    m = db.session.get(Message, mid)
    if m is None or m.workspace_id != ws.id or m.user_id != user.id:
        raise HTTPException(404)
    return json_response(_assistant_json(request, ws, m))


@router.api_route("/assistant/{mid:int}/confirm/{index:int}", methods=["POST"], name="app.assistant_confirm")
def assistant_confirm(request: Request, wid: int, mid: int, index: int, user=Depends(signed_in)):
    ws, engine = ctx(wid, "editor", request)
    m = db.session.get(Message, mid)
    if m is None or m.workspace_id != ws.id or m.user_id != user.id or index >= len(m.proposals or []):
        raise HTTPException(404)
    item, engine = assistant.confirm(m, index, user)
    navigate = url_for("app.record", wid=wid, table=item["table"], rid=item["record_id"]) if item.get("record_id") else None
    return json_response({**_assistant_json(request, ws, m), "navigate": navigate})


@router.api_route("/assistant/{mid:int}/dismiss/{index:int}", methods=["POST"], name="app.assistant_dismiss")
def assistant_dismiss(request: Request, wid: int, mid: int, index: int, user=Depends(signed_in)):
    ws, engine = ctx(wid, "viewer", request)
    m = db.session.get(Message, mid)
    if m is None or m.workspace_id != ws.id or m.user_id != user.id or index >= len(m.proposals or []):
        raise HTTPException(404)
    proposals = list(m.proposals)
    proposals[index] = {**proposals[index], "status": "dismissed"}
    m.proposals = proposals
    db.session.commit()
    return json_response(_assistant_json(request, ws, m))


# --- adding a record from a document ---------------------------------------------------------------------------------

@router.api_route("/t/{table}/from-document", methods=["GET", "POST"], name="app.intake")
def intake(request: Request, wid: int, table: str, user=Depends(signed_in), form: Form = Depends(form_of)):
    ws, engine = ctx(wid, "editor", request)
    t = _table(engine, table, "edit")
    if request.method == "POST":
        ids = []
        for up in form.files.getlist("files")[:6]:
            if up and up.filename:
                try:
                    ids.append(save(ws.id, up, up.filename, source="upload", by=user.display).id)
                except FileRejected as exc:
                    flash(request, str(exc), "error")
        if not ids:
            flash(request, "Choose a document to read (a PDF, a photo or scan, a Word file or an email).", "error")
            return redirect(url_for("app.intake", wid=wid, table=table))
        job = jobs.enqueue("intake", {"table": t["key"], "file_ids": ids, "instructions": (form.get("instructions") or "").strip()[:500]},
                           workspace_id=ws.id, by=user.id)
        return redirect(url_for("app.intake_review", wid=wid, table=table, jid=job.id))
    return render(request, "app/intake.html", ws=ws, e=engine, t=t)


@router.api_route("/t/{table}/from-document/{jid:int}", methods=["GET"], name="app.intake_review")
def intake_review(request: Request, wid: int, table: str, jid: int):
    ws, engine = ctx(wid, "editor", request)
    t = _table(engine, table, "edit")
    job = db.session.get(Job, jid)
    if job is None or job.workspace_id != ws.id or job.kind != "intake":
        raise HTTPException(404)
    files = File.query.filter(File.id.in_(job.payload["file_ids"])).all()
    if job.status != "done":
        return render(request, "app/intake_wait.html", ws=ws, e=engine, t=t, job=job, files=files)
    result = job.result or {}
    line_rows = {k: [{"id": None, "values": v} for v in rows] for k, rows in (result.get("lines") or {}).items()}
    unsure = set(result.get("unsure") or [])
    return _render_form(ws, engine, t, None, result.get("values") or {}, result.get("problems") or {}, line_rows, request=request,
                        intake={"files": files, "unsure": unsure}, attach=[f.id for f in files])[0]


# --- importing a spreadsheet -----------------------------------------------------------------------------------------

@router.api_route("/t/{table}/import", methods=["GET", "POST"], name="app.import_start")
def import_start(request: Request, wid: int, table: str, user=Depends(signed_in), form: Form = Depends(form_of)):
    ws, engine = ctx(wid, "editor", request)
    t = _table(engine, table, "edit")
    if request.method == "POST":
        up = form.files.get("file")
        if not up or not up.filename:
            flash(request, "Choose a CSV or Excel file.", "error")
            return redirect(url_for("app.import_start", wid=wid, table=table))
        try:
            f = save(ws.id, up, up.filename, source="upload", by=user.display)
            importer.preview(f)
        except (FileRejected, ValueError) as exc:
            flash(request, str(exc), "error")
            return redirect(url_for("app.import_start", wid=wid, table=table))
        return redirect(url_for("app.import_map", wid=wid, table=table, fid=f.id))
    return render(request, "app/import.html", ws=ws, e=engine, t=t, step="upload")


@router.api_route("/t/{table}/import/{fid:int}", methods=["GET", "POST"], name="app.import_map")
def import_map(request: Request, wid: int, table: str, fid: int, user=Depends(signed_in), form: Form = Depends(form_of)):
    ws, engine = ctx(wid, "editor", request)
    t = _table(engine, table, "edit")
    f = db.session.get(File, fid)
    from .erp_records import may_open_file
    if f is None or f.workspace_id != ws.id or not may_open_file(engine, f, request):
        raise HTTPException(404)
    sheet = request.query_params["sheet"] if "sheet" in request.query_params else form.get("sheet")
    try:
        info = importer.preview(f, sheet)
    except ValueError as exc:
        flash(request, str(exc), "error")
        return redirect(url_for("app.import_start", wid=wid, table=table))
    if request.method == "POST" and form.get("go"):
        mapping = {h: form.get(f"map-{i}") for i, h in enumerate(info["headers"]) if form.get(f"map-{i}")}
        if not mapping:
            flash(request, "Match at least one column to a field.", "error")
        else:
            match_on = form.get("match_on") if schema.field(t, form.get("match_on") or "") else None
            job = importer.start(ws, f.id, t["key"], mapping, info["sheet"], by=user.id, match_on=match_on)
            return redirect(url_for("app.import_progress", wid=wid, table=table, jid=job.id))
    mapping = importer.suggest(engine, t["key"], info["headers"], info["rows"])
    fields = [x for x in t["fields"] if x["type"] in importer.IMPORTABLE]
    return render(request, "app/import.html", ws=ws, e=engine, t=t, step="map", file=f, info=info, mapping=mapping, fields=fields)


@router.api_route("/t/{table}/import/progress/{jid:int}", methods=["GET"], name="app.import_progress")
def import_progress(request: Request, wid: int, table: str, jid: int):
    ws, engine = ctx(wid, "editor", request)
    t = _table(engine, table, "edit")
    job = db.session.get(Job, jid)
    if job is None or job.workspace_id != ws.id or job.kind != "import":
        raise HTTPException(404)
    return render(request, "app/import.html", ws=ws, e=engine, t=t, step="progress", job=job)


# --- what the AI knows -----------------------------------------------------------------------------------------------

@router.api_route("/knowledge", methods=["GET", "POST"], name="app.knowledge_page")
def knowledge_page(request: Request, wid: int, user=Depends(signed_in), form: Form = Depends(form_of)):
    """Documents the AI should know (policies, price lists, contracts, manuals), learning from Microsoft 365, and a
    place to try a question and see what the AI would find."""
    from ..connectors import microsoft
    from ..models import utcnow
    ws, engine = ctx(wid, "viewer", request)
    member = request.state.member
    if request.method == "POST":
        action = form.get("action")
        if action == "upload":
            if not member.can("editor"):
                raise HTTPException(403)
            added = []
            for up in form.files.getlist("files")[:20]:
                if up and up.filename:
                    try:
                        added.append(save(ws.id, up, up.filename, source="knowledge", by=user.display).name)
                    except FileRejected as exc:
                        flash(request, str(exc), "error")
            if added:
                flash(request, f"Added {', '.join(added)}. The AI is reading {'it' if len(added) == 1 else 'them'} now; it takes a moment.", "success")
        elif action == "remove":
            if not member.can("editor"):
                raise HTTPException(403)
            f = db.session.get(File, int(form.get("file") or 0))
            if f is None or f.workspace_id != ws.id or f.source != "knowledge":
                raise HTTPException(404)
            f.deleted_at = utcnow()
            db.session.commit()
            knowledge.touch(ws.id, "file", f.id)
            flash(request, f"Removed {f.name}; the AI no longer uses it.", "success")
        elif action in ("mail_on", "mail_off", "mail_history", "onedrive", "rebuild"):
            if not member.can("admin"):
                raise HTTPException(403)
            if action in ("mail_on", "mail_off"):
                settings = dict(ws.settings or {})
                settings["knowledge"] = {**(settings.get("knowledge") or {}), "mail": action == "mail_on"}
                ws.settings = settings
                db.session.commit()
                flash(request, "The AI will read new mail as it arrives." if action == "mail_on" else "The AI won't read new mail.", "success")
            elif action == "mail_history":
                jobs.enqueue("knowledge_mail", {"days": 180}, workspace_id=ws.id, dedupe=f"knowledge_mail:{ws.id}", by=user.id)
                flash(request, "Reading the last six months of mail in the background.", "success")
            elif action == "onedrive":
                folder = (form.get("folder") or "").strip().strip("/")
                jobs.enqueue("knowledge_onedrive", {"folder": folder}, workspace_id=ws.id, dedupe=f"knowledge_onedrive:{ws.id}:{folder}", by=user.id)
                flash(request, f"Reading the documents in {folder or 'your OneDrive'} in the background.", "success")
            else:
                jobs.enqueue("knowledge_rebuild", workspace_id=ws.id, dedupe=f"knowledge_rebuild:{ws.id}", by=user.id)
                flash(request, "Reading everything again in the background.", "success")
        return redirect(url_for("app.knowledge_page", wid=wid))
    q = (request.query_params.get("q") or "").strip()
    hits = knowledge.search(engine, q, k=8) if q else []
    documents = File.query.filter_by(workspace_id=ws.id, source="knowledge", deleted_at=None).order_by(File.id.desc()).all()
    from ..models import Chunk
    read = {ref for (ref,) in db.session.query(Chunk.ref).filter(Chunk.workspace_id == ws.id, Chunk.source == "file").distinct()}
    background = Job.query.filter(Job.workspace_id == ws.id, Job.kind.in_(["knowledge_mail", "knowledge_onedrive", "knowledge_rebuild"])) \
        .order_by(Job.id.desc()).limit(5).all()
    return render(request, "app/knowledge.html", ws=ws, e=engine, member=member, status=knowledge.status(ws), q=q, hits=hits,
                  documents=documents, read=read, background=background,
                  mail_on=bool(((ws.settings or {}).get("knowledge") or {}).get("mail")),
                  connected=microsoft.status(ws).get("connected") if microsoft.configured() else False)
