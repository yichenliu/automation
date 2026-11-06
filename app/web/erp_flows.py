"""Flows on screen: one step at a time, made for a phone in the hand or a bench with a scanner (engine/flows.py)."""
import base64
import json

from fastapi import APIRouter, Depends, HTTPException, Request

from ..blueprint import expr, schema
from ..engine import flows
from ..engine.records import FieldErrors, RecordContext
from ..extensions import db
from ..models import FlowRun
from ..storage import FileRejected, save
from .erp import ctx
from .erp_records import _options
from .http import Form, PageRoute, check_csrf, current_user, flash, form_of, json_response, redirect, render, signed_in, url_for

router = APIRouter(prefix="/w/{wid:int}/app", route_class=PageRoute, dependencies=[Depends(check_csrf)])


def _run(request, engine, run_id):
    run = db.session.get(FlowRun, run_id)
    if run is None or run.workspace_id != engine.ws.id or (run.user_id != current_user(request).id and not request.state.member.can("admin")):
        raise HTTPException(404)
    flow = flows.of(engine, run.flow)
    if flow is None:
        raise HTTPException(404)
    return run, flow


@router.api_route("/flows", methods=["GET"], name="app.flow_list")
def flow_list(request: Request, wid: int, user=Depends(signed_in)):
    """The work done step by step that this person can start, and what they left part way."""
    ws, engine = ctx(wid, "viewer", request)
    started = [(r, flows.of(engine, r.flow)) for r in flows.unfinished(engine, user.id)]
    return render(request, "app/flows.html", ws=ws, e=engine, offered=flows.in_menu(engine),
                           started=[(r, f) for r, f in started if f is not None])


@router.api_route("/flows/{key}/start", methods=["POST"], name="app.flow_start")
def flow_start(request: Request, wid: int, key: str, user=Depends(signed_in), form: Form = Depends(form_of)):
    ws, engine = ctx(wid, "editor", request)
    flow = flows.of(engine, key)
    if flow is None:
        raise HTTPException(404)
    record = None
    if flow["start"] == "record":
        rid = request.query_params["record"] if "record" in request.query_params else form.get("record", "")
        record = engine.get(int(rid), flow["table"]) if rid.isdigit() else None
        if record is None or not engine.allowed(record):
            raise HTTPException(404)
    try:
        run = flows.begin(engine, flow, user.id, record)
    except FieldErrors as exc:
        flash(request, "; ".join(exc.errors.values()), "error")
        return redirect(request.headers.get("referer") or url_for("app.flow_list", wid=wid))
    return redirect(url_for("app.flow_run", wid=wid, run_id=run.id))


@router.api_route("/flows/run/{run_id:int}", methods=["GET", "POST"], name="app.flow_run")
def flow_run(request: Request, wid: int, run_id: int, user=Depends(signed_in), form: Form = Depends(form_of)):
    ws, engine = ctx(wid, "editor", request)
    run, flow = _run(request, engine, run_id)
    if run.status == "done":
        return redirect(url_for("app.flow_done", wid=wid, run_id=run.id))
    if run.status != "open":
        flash(request, "That was cancelled.", "info")
        return redirect(url_for("app.flow_list", wid=wid))
    steps = flow["steps"]
    run.at = min(max(run.at or 0, 0), len(steps) - 1)
    step = steps[run.at]
    errors = {}
    if request.method == "POST":
        action = form.get("go", "next")
        if action == "cancel":
            run.status, run.finished_at = "cancelled", None
            db.session.commit()
            flash(request, f"Stopped “{flow['label']}”. Nothing was saved.", "info")
            record = flows.record_of(engine, run)
            return redirect(url_for("app.record", wid=wid, table=flow["table"], rid=record.id) if record else url_for("app.flow_list", wid=wid))
        if action == "back":
            run.at = max(0, run.at - 1)
            db.session.commit()
            return redirect(url_for("app.flow_run", wid=wid, run_id=run.id))
        values, files = _posted(request, form, engine, ws, flow, step)
        if isinstance(values, dict) and "_upload" in values:
            errors = {"_": values["_upload"]}
        else:
            errors = flows.answer(engine, run, step, values, files)
        if not errors:
            ending = step["kind"] == "decide" and (run.data.get(step["key"]) or {}).get("finish")
            following = [n for n in range(run.at + 1, len(steps)) if steps[n]["kind"] != "print"]  # prints come once it's saved
            if following and not ending:
                run.at = following[0]
                db.session.commit()
                return redirect(url_for("app.flow_run", wid=wid, run_id=run.id))
            try:
                record = flows.finish(engine, run, user.display)
            except FieldErrors as exc:
                errors = exc.errors
                back = next((n for n, s in enumerate(steps) if s["kind"] == "form" and set(s["fields"]) & set(exc.errors)), None)
                if back is not None and back != run.at:  # the step that asked for it, to put it right
                    run.at = back
                    db.session.commit()
                    flash(request, "; ".join(exc.errors.values()), "error")
                    return redirect(url_for("app.flow_run", wid=wid, run_id=run.id))
            else:
                for s_ in steps:  # what prints by itself, where there's a printer for it
                    if s_["kind"] == "print" and s_.get("auto") and printer_chosen(ws, _role(engine, flow, s_, record)):
                        _print_step(request, engine, flow, s_, record)
                return redirect(url_for("app.flow_done", wid=wid, run_id=run.id, rid=record.id))
    record = flows.record_of(engine, run)
    shown = [n for n, s in enumerate(steps) if s["kind"] != "print"]
    return render(request, "app/flow.html", ws=ws, e=engine, flow=flow, run=run, step=step, errors=errors, record=record,
                           t=engine.tables[flow["table"]], text=_text(engine, record, step.get("text")),
                           lines=flows.pick_lines(engine, flow, step, record) if step["kind"] == "pick" else [],
                           options=_options(engine, engine.tables[flow["table"]]) if step["kind"] == "form" else {},
                           got=(run.data or {}).get(step["key"]) or {}, number=shown.index(run.at) + 1 if run.at in shown else 1,
                           of=len(shown), first=run.at == (shown[0] if shown else 0))


def _posted(request, form, engine, ws, flow, step):
    """A step's answers as its screen sent them, and the ids of any photo or signature kept for it."""
    kind = step["kind"]
    if kind == "form":
        t = engine.tables[flow["table"]]
        out = {}
        for k in step["fields"]:
            f = schema.field(t, k)
            out[k] = form.getlist(k) if f and f["type"] in ("multi_select", "tags") or (f and f.get("multiple")) else form.get(k)
            if f and f["type"] == "checkbox":
                out[k] = bool(form.get(k))
        return out, []
    if kind in ("scan", "pick"):
        try:
            return json.loads(form.get("answer") or "{}"), []
        except ValueError:
            return {}, []
    if kind == "decide":
        return {"choice": form.get("choice")}, []
    if kind == "photo":
        fids = []
        for upload in [u for u in form.files.getlist("photo") if u and u.filename][:6]:
            try:
                fids.append(save(ws.id, upload, upload.filename, by=current_user(request).display).id)
            except FileRejected as exc:
                return {"_upload": str(exc)}, []
        return {}, fids
    if kind == "signature":
        drawn = form.get("signature") or ""
        if drawn.startswith("data:image/png;base64,") and len(drawn) < 3_000_000:
            return {}, [save(ws.id, base64.b64decode(drawn.split(",", 1)[1]), f"{step['label']}.png", by=current_user(request).display,
                             mime="image/png").id]
        return {}, []
    return {}, []


def _text(engine, record, text):
    """A step's words, with {{record.x}} filled in from the record it's about."""
    if not text:
        return ""
    try:
        return expr.render(text, RecordContext(engine, record, {})) if record is not None else text
    except Exception:  # noqa: BLE001 - words as written rather than a broken screen
        return text


@router.api_route("/flows/run/{run_id:int}/code", methods=["GET"], name="app.flow_code", dependencies=[Depends(signed_in)])
def flow_code(request: Request, wid: int, run_id: int):
    """What a scanned code is, for a scan step: {"id", "title"}, or 404 with what to say."""
    ws, engine = ctx(wid, "editor", request)
    run, flow = _run(request, engine, run_id)
    step = flow["steps"][min(run.at or 0, len(flow["steps"]) - 1)]
    if step["kind"] != "scan":
        raise HTTPException(400)
    found = flows.find_code(engine, step, request.query_params.get("code"))
    if found is None:
        return json_response({"error": f"Nothing has the code “{request.query_params.get('code', '')}”"}, 404)
    return json_response({"id": found.id, "title": engine.title(found)})


def _print_target(engine, flow, step, record):
    """(table, records) a print step prints: the record, or the lines a scan or pick step worked on."""
    if step["of"] == "record":
        return record.table, [record]
    spec = next((s.get("into") or s.get("lines") for s in flow["steps"] if s["key"] == step["of"]), None) or \
        {"table": step["of"], "via": schema._line_link(engine.tables.get(step["of"]) or {"fields": []}, flow["table"]) or ""}
    return spec["table"], (engine.related(record, spec["table"], spec["via"]) if spec.get("via") else [])


def _print_step(request, engine, flow, step, record):
    """Send a print step's labels or document to the printer; a message for the person either way."""
    from .. import printing
    from ..documents import build, labels
    from ..documents import pdf as pdf_
    table, rows = _print_target(engine, flow, step, record)
    try:
        if _is_label(engine, table, step["document"]):
            doc = next((d for d in engine.tables[table].get("documents", []) if d["key"] == step["document"]), None)
            link = lambda r: url_for("app.record", wid=engine.ws.id, table=table, rid=r.id, _external=True)  # noqa: E731
            flash(request, printing.print_labels(engine.ws, labels.for_records(engine, rows, doc, link), (doc or {}).get("size")), "success")
        else:
            for r in rows:
                data, _ = build(engine, r, step["document"])
                flash(request, printing.print_pdf(engine.ws, pdf_.render(data)), "success")
    except printing.PrintProblem as exc:
        flash(request, str(exc), "error")


def _role(engine, flow, step, record):
    table, _ = _print_target(engine, flow, step, record)
    return "labels" if _is_label(engine, table, step["document"]) else "documents"


def printer_chosen(ws, role):
    from .. import printing
    return printing.chosen(ws, role) is not None


@router.api_route("/flows/run/{run_id:int}/print/{n:int}", methods=["POST"], name="app.flow_print", dependencies=[Depends(signed_in)])
def flow_print(request: Request, wid: int, run_id: int, n: int):
    ws, engine = ctx(wid, "editor", request)
    run, flow = _run(request, engine, run_id)
    record = engine.get(run.result_id) if run.result_id else None
    if run.status != "done" or record is None or not 0 <= n < len(flow["steps"]) or flow["steps"][n]["kind"] != "print":
        raise HTTPException(404)
    _print_step(request, engine, flow, flow["steps"][n], record)
    return redirect(url_for("app.flow_done", wid=wid, run_id=run.id))


@router.api_route("/flows/run/{run_id:int}/done", methods=["GET"], name="app.flow_done", dependencies=[Depends(signed_in)])
def flow_done(request: Request, wid: int, run_id: int):
    ws, engine = ctx(wid, "viewer", request)
    run, flow = _run(request, engine, run_id)
    if run.status != "done":
        return redirect(url_for("app.flow_run", wid=wid, run_id=run.id))
    record = engine.get(run.result_id) if run.result_id else None
    prints = []
    for n, step in enumerate(flow["steps"]):
        if step["kind"] != "print" or record is None:
            continue
        table, rows = _print_target(engine, flow, step, record)
        if rows and printer_chosen(ws, _role(engine, flow, step, record)):  # straight to the printer
            prints.append({"label": step["label"], "send": url_for("app.flow_print", wid=wid, run_id=run.id, n=n), "count": len(rows)})
            continue
        if step["of"] == "record":
            prints.append({"label": step["label"], "url": url_for("app.label", wid=wid, table=record.table, rid=record.id, doc=step["document"] or None)
                           if _is_label(engine, record.table, step["document"]) else None, "doc": step["document"], "record": record})
        else:
            spec = next((s.get("into") or s.get("lines") for s in flow["steps"] if s["key"] == step["of"]),
                        {"table": step["of"], "via": schema._line_link(engine.tables.get(step["of"]) or {"fields": []}, flow["table"]) or ""})
            lines = engine.related(record, spec["table"], spec["via"]) if spec and spec.get("via") else []
            if lines:
                prints.append({"label": step["label"], "lines": [x.id for x in lines], "table": spec["table"], "doc": step["document"]})
    from ..models import Run
    rule = db.session.get(Run, run.run_id) if run.run_id else None
    return render(request, "app/flow_done.html", ws=ws, e=engine, flow=flow, run=run, record=record, prints=prints, rule=rule)


def _is_label(engine, table, key):
    return not key or any(d["key"] == key and d.get("format") == "label" for d in engine.tables[table].get("documents", []))
