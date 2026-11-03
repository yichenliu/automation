"""Flows: work done a screen at a time, on a phone or at a bench (schema.normalize_flow). A run keeps what's been
entered, scanned and counted so far; nothing reaches the records until the last step, and then it all does together:
the record gets what was filled in and decided, scanned items become its lines, counts go on its lines, photos and
signatures are kept with it, and the flow's "then" steps run as a rule (with {{flow.<step>.<…>}}), so waits, approvals
and the run's history work as they do for any rule.
"""
from ..blueprint import expr, schema
from ..extensions import db
from ..models import Approval, File, FlowRun, utcnow
from . import automations
from .records import FieldErrors

KEEP_OPEN_DAYS = 14  # an unfinished run is offered to carry on with for this long


def of(engine, key):
    return next((f for f in schema.flows(engine.bp) if f["key"] == key), None)


def as_automation(flow):
    """The flow's "then" steps as a rule run can carry them out (automations.execute finds it by "flow:<key>")."""
    return {"key": f"flow:{flow['key']}", "name": flow["label"], "enabled": True,
            "trigger": {"type": "button", "table": flow["table"], "label": flow["label"]}, "conditions": [], "actions": flow.get("then") or []}


def offered(engine, flow, record=None):
    """Whether this person may do the flow (on this record): they can change its table, they're who it's for, and the
    record is at the point it's for (Receive goods once the order is sent)."""
    t = engine.tables.get(flow["table"])
    if t is None or not engine.can(t, "edit" if flow["start"] == "record" else "create"):
        return False
    if flow.get("who") and engine.role is not None and not engine._at_least("admin") \
            and not any(engine.is_who(w, record) for w in flow["who"]):
        return False
    if flow["start"] == "record":
        return record is not None and engine.allowed(record) and (not flow.get("when") or engine.matches(record, flow["when"]))
    return True


def for_record(engine, record):
    return [f for f in schema.flows(engine.bp) if f["start"] == "record" and f["table"] == record.table and offered(engine, f, record)]


def in_menu(engine):
    return [f for f in schema.flows(engine.bp) if f["start"] == "new" and offered(engine, f)]


def begin(engine, flow, user_id, record=None):
    if not offered(engine, flow, record):
        raise FieldErrors({"_": f"“{flow['label']}” isn't open to you" + (" for this one" if record is not None else "")})
    run = FlowRun(workspace_id=engine.ws.id, flow=flow["key"], record_id=record.id if record is not None else None,
                  user_id=user_id, at=0, data={}, status="open")
    db.session.add(run)
    db.session.commit()
    return run


def unfinished(engine, user_id):
    from datetime import timedelta
    since = utcnow() - timedelta(days=KEEP_OPEN_DAYS)
    return FlowRun.query.filter(FlowRun.workspace_id == engine.ws.id, FlowRun.user_id == user_id, FlowRun.status == "open",
                                FlowRun.created_at >= since).order_by(FlowRun.id.desc()).limit(10).all()


# --- what each step shows --------------------------------------------------------------------------------------------

def record_of(engine, run):
    return engine.get(run.record_id) if run.record_id else None


def pick_lines(engine, flow, step, record):
    """The lines to count off: [{"id", "title", "target", "count", "codes"}]. A line's codes are its code field's value
    (a product's barcode through its link), and the names it goes by, so a scan or a typed name finds it."""
    if record is None:
        return []
    lines = engine.related(record, step["lines"]["table"], step["lines"]["via"])
    t = engine.tables[step["lines"]["table"]]
    out = []
    for line in lines:
        title = engine.title(line)
        codes = {title.strip().lower()}
        if step.get("code"):
            first, _, rest = step["code"].partition(".")
            value = engine.value(line, first)
            f = schema.field(t, first)
            if rest and f is not None and f["type"] == "link" and str(value or "").isdigit():
                linked = engine.get(int(value))
                if linked is not None:  # the line goes by what it's for: Flour 25kg, not the order's name and the flour's
                    title = engine.title(linked)
                    codes |= {str(engine.value(linked, rest) or "").strip().lower(), title.strip().lower()}
            elif value not in (None, ""):
                codes.add(str(value).strip().lower())
        target = engine.value(line, step["target"]) if step.get("target") else None
        out.append({"id": line.id, "title": title, "target": float(target) if isinstance(target, (int, float)) else None,
                    "count": float(engine.value(line, step["count_into"]) or 0), "codes": sorted(c for c in codes if c)})
    return out


def find_code(engine, step, code):
    """A scan step's scanned code: the record it is, among those the scanned items' link can point at."""
    t = engine.tables[step["into"]["table"]]
    item = schema.field(t, step["into"]["item"])
    wanted = str(code or "").strip().lower()
    if item is None or not wanted:
        return None
    target = engine.tables.get(item["table"])
    codes = [f for f in target["fields"] if f["type"] in ("barcode", "autonumber", "text")] if target else []
    for r in engine.visible_rows(item["table"]):
        if wanted == engine.title(r).strip().lower() or any(
                wanted in (str((r.data or {}).get(f["key"]) or "").strip().lower(), engine.display(f, (r.data or {}).get(f["key"])).lower())
                for f in codes if f["type"] != "text" or f.get("unique")):
            return r
    return None


# --- each step's answers ---------------------------------------------------------------------------------------------

def answer(engine, run, step, values, files=None):
    """Check and keep one step's answers in the run. Returns {} or {field or "_": problem}. `values` come from the step's
    screen: a form's fields, the scanned items, the counts, the chosen button; `files` the photo or signature."""
    flow = of(engine, run.flow)
    t = engine.tables[flow["table"]]
    record = record_of(engine, run)
    kind, kept, errors = step["kind"], {}, {}
    if kind == "form":
        for k in step["fields"]:
            f = schema.field(t, k)
            raw = values.get(k)
            if raw in (None, "", []):
                if f.get("required") and (flow["start"] == "new" or (record is not None and engine.value(record, k) in (None, ""))):
                    errors[k] = f"{f['label']} is needed"
                continue
            try:
                kept[k] = engine.coerce(f, raw)
            except ValueError as exc:
                errors[k] = str(exc)
    elif kind == "scan":
        seen, items = {}, []
        for row in values.get("scanned") or []:
            rid, qty = int(row.get("id") or 0), float(row.get("qty") or 1)
            target = engine.get(rid)
            if target is None or not engine.allowed(target):
                continue
            if rid in seen and step["duplicates"] == "reject":
                errors["_"] = f"{engine.title(target)} was scanned twice; each one is scanned once"
                continue
            if rid in seen and step["duplicates"] == "count":
                items[seen[rid]]["qty"] += qty
                continue
            seen[rid] = len(items)
            items.append({"id": rid, "title": engine.title(target), "qty": qty})
        if step.get("min") and sum(x["qty"] for x in items) < step["min"]:
            errors["_"] = f"Scan at least {step['min']:g}; {sum(x['qty'] for x in items):g} so far"
        kept = {"scanned": items}
    elif kind == "pick":
        lines = {line["id"]: line for line in pick_lines(engine, flow, step, record)}
        counts = {int(k): max(0.0, float(v or 0)) for k, v in (values.get("counts") or {}).items() if str(k).isdigit() and int(k) in lines}
        short = [line for lid, line in lines.items() if line["target"] is not None and counts.get(lid, 0) < line["target"]]
        if short and step["short"] == "block":
            errors["_"] = f"Not everything's been counted off: {', '.join(x['title'] for x in short[:3])}{'…' if len(short) > 3 else ''}"
        elif short and step["short"] == "ask" and not str(values.get("reason") or "").strip():
            errors["reason"] = (f"{len(short)} line{'s are' if len(short) != 1 else ' is'} short: say why before going on "
                                "(it's kept with the record)")
        kept = {"counts": {str(k): v for k, v in counts.items()}, "reason": str(values.get("reason") or "").strip()[:500]}
    elif kind == "photo":
        fids = [x for x in (files or []) if x]
        if not fids and not (run.data or {}).get(step["key"], {}).get("files") and not step.get("optional"):
            errors["_"] = "Take the photo to go on"
        kept = {"files": fids or (run.data or {}).get(step["key"], {}).get("files") or []}
    elif kind == "signature":
        fids = [x for x in (files or []) if x]
        if not fids and not step.get("optional"):
            errors["_"] = "Sign in the box to go on"
        kept = {"files": fids}
    elif kind == "decide":
        try:
            chosen = step["options"][int(values.get("choice"))]
        except (TypeError, ValueError, IndexError):
            errors["_"] = "Choose one"
            chosen = None
        kept = {"choice": chosen["value"] if chosen else None, "finish": bool(chosen and chosen.get("finish"))}
    if not errors:
        run.data = {**(run.data or {}), step["key"]: kept}
        db.session.commit()
    return errors


# --- finishing ---------------------------------------------------------------------------------------------------------

def finish(engine, run, by):
    """Everything the run gathered, saved together, then its "then" steps run. Returns the record; raises FieldErrors
    (saying which step) if the app refuses any of it, and nothing is saved."""
    flow = of(engine, run.flow)
    t = engine.tables[flow["table"]]
    data = run.data or {}
    values, files, stage = {}, [], {}
    stage_field = (t.get("workflow") or {}).get("field")
    for step in flow["steps"]:
        got = data.get(step["key"]) or {}
        if step["kind"] == "form":
            values.update(got)
        elif step["kind"] == "decide" and step.get("field") and got.get("choice") is not None:
            # a button that sets the stage moves it on as a rule would (Received once it's counted in), not by a move a
            # person could press without doing the flow
            (stage if step["field"] == stage_field else values)[step["field"]] = got["choice"]
        elif step["kind"] in ("photo", "signature"):
            files += [(fid, step["field"]) for fid in got.get("files") or []]
        if step["kind"] == "decide" and got.get("finish"):
            break  # a button that ends the flow there (Reject): the steps after it weren't done
    try:
        with engine.atomic():
            if flow["start"] == "new":
                record = engine.create(t["key"], values)
            else:
                record = engine.get(run.record_id)
                if record is None:
                    raise FieldErrors({"_": f"the {schema.lc(t['singular'])} this was for has been deleted"})
                if values:
                    engine.update(record, values)
            for step in flow["steps"]:
                got = data.get(step["key"]) or {}
                if step["kind"] == "scan":
                    into = step["into"]
                    for item in got.get("scanned") or []:
                        already = next((r for r in engine.children(into["table"], into["via"], record.id)
                                        if str((r.data or {}).get(into["item"])) == str(item["id"])), None) if into.get("quantity") else None
                        if already is not None:  # on it already (a PO's line, a sale's): the count adds to that line
                            engine.update(already, {into["quantity"]: (expr.to_number((already.data or {}).get(into["quantity"])) or 0) + item["qty"]})
                            continue
                        line = {into["via"]: record.id, into["item"]: item["id"]}
                        if into.get("quantity"):
                            line[into["quantity"]] = item["qty"]
                        engine.create(into["table"], line)
                elif step["kind"] == "pick":
                    for lid, count in (got.get("counts") or {}).items():
                        line = engine.get(int(lid))
                        if line is not None and line.table == step["lines"]["table"]:
                            engine.update(line, {step["count_into"]: count})
    except FieldErrors as exc:
        raise FieldErrors({k: v for k, v in exc.errors.items()}) from exc
    if stage and stage[stage_field] != (engine.get(record.id).data or {}).get(stage_field):
        engine.system().update(engine.get(record.id), stage, by=by)
        engine.reset()
    for fid, field in files:
        file = db.session.get(File, fid)
        if file is not None and file.workspace_id == engine.ws.id:
            if schema.field(t, field) and schema.field(t, field)["type"] in ("image", "signature"):
                for old in File.query.filter_by(record_id=record.id, field=field, deleted_at=None):
                    old.deleted_at = utcnow()
            file.record_id, file.field = record.id, field
    run.status, run.result_id, run.finished_at, run.record_id = "done", record.id, utcnow(), record.id
    db.session.commit()
    task = db.session.get(Approval, run.task_id) if run.task_id else None
    if task is not None and task.status == "pending":  # the task was to do this flow: done, with what the flow filled in
        saved = engine.get(record.id)
        try:
            automations.complete(engine, task, by, {k: engine.value(saved, k) for k in task.fields or []}, None)
        except FieldErrors:
            pass  # it asks for more than the flow filled in: it stays on the To do list to finish there
    if flow.get("then"):
        summary = {step["key"]: _summary(step, data.get(step["key"]) or {}) for step in flow["steps"]}
        started = automations.start(engine, as_automation(flow), engine.get(record.id), f"{by} finished “{flow['label']}”",
                                    context={"flow": summary})
        run.run_id = started.id
        db.session.commit()
    return engine.get(record.id)


def _summary(step, got):
    """A step's answers as the "then" steps read them: {{flow.count.short}}, {{flow.check.choice}}."""
    if step["kind"] == "scan":
        items = got.get("scanned") or []
        return {"count": sum(x["qty"] for x in items), "items": ", ".join(f"{x['title']} × {x['qty']:g}" for x in items)}
    if step["kind"] == "pick":
        return {"reason": got.get("reason") or "", "counted": sum((got.get("counts") or {}).values())}
    if step["kind"] == "decide":
        return {"choice": got.get("choice")}
    if step["kind"] == "form":
        return dict(got)
    return {}
