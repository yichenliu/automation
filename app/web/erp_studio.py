"""The data model studio and the workflow studio (app/studio.py): the design drawn as diagrams, changed by hand.

Everyone who can see the design can look; admins change it. Every change is checked like the builder's and saved as a
version, and the studio can take it back again (each change answers with the version it was made from).
"""
import copy

from flask import abort, g, jsonify, render_template, request
from flask_login import current_user, login_required

from .. import studio
from ..blueprint import ops, schema
from ..blueprint.schema import SchemaError
from ..builder import chat
from ..extensions import db
from ..models import Record, Run
from .erp import bp, ctx
from .filters import KIND_WORDS, about_field


class Refused(Exception):
    """A change the design can't take, said plainly."""


def _filled(ws):
    """How many records have a value in a field (or that value): what's in use isn't thrown away by a click."""
    def filled(table, key, value=None):
        n = 0
        for (data,) in db.session.query(Record.data).filter(Record.workspace_id == ws.id, Record.table == table, Record.deleted_at.is_(None)):
            v = (data or {}).get(key)
            if value is None:
                n += v not in (None, "", [])
            else:
                n += v == value or (isinstance(v, list) and value in v)
        return n
    return filled


def _plain(problem, bp):
    text = problem.split(": ", 1)[1] if problem.startswith("change ") and ": " in problem else problem
    try:
        text = chat.plain(text, bp)
    except Exception:  # noqa: BLE001 - the words as they are, if they can't be made plainer
        pass
    return text[:1].upper() + text[1:]


def _save(ws, ops_, renames, what):
    """Apply the changes (and renamed choices) to the design, check it, and save it as a version. Returns (the changes
    said, the version it was made from)."""
    bp = copy.deepcopy(ws.blueprint or {})
    said = []
    for table, key, old, new in renames:
        studio.rename_choice(bp, table, key, old, new)
        t = schema.table(bp, table)
        said.append(f"Renamed {schema.field(t, key)['label']} “{old}” to “{new}” in {t['label']}")
    # what's wrong already isn't this change's doing: what the design fails now, and what saving it as it is would
    # (an app made before a newer way of saying something, brought up to date on any save)
    already = schema.check(ws.blueprint or {}) + ops.apply(ws.blueprint or {}, [], always_check=True).problems
    result = ops.apply(bp, ops_, already=already)
    if result.problems:
        raise Refused(_plain(result.problems[0], ws.blueprint))
    changes = said + list(result.changes)
    if not changes:
        raise Refused("That's how it is already")
    before = ws.version or 0
    for table, key, old, new in renames:  # the records too, before the version is saved (it carries values across)
        n = _rename_records(ws, table, key, old, new)
        if n:
            changes.append(f"{n} record{'s' if n != 1 else ''} now say “{new}”")
    chat.save_version(ws, result.blueprint, changes, "studio", by=current_user.id)
    if renames:  # kept, so taking the change back renames the records back too
        kept = dict(((ws.settings or {}).get("studio") or {}).get("renamed") or {})
        kept[str(ws.version)] = [list(r) for r in renames]
        kept = dict(sorted(kept.items(), key=lambda kv: int(kv[0]))[-40:])
        ws.settings = {**(ws.settings or {}), "studio": {**((ws.settings or {}).get("studio") or {}), "renamed": kept}}
        db.session.commit()
    return changes, before


def _rename_records(ws, table, key, old, new):
    n = 0
    for r in Record.query.filter_by(workspace_id=ws.id, table=table).all():  # the bin's too, so they come back right
        v = (r.data or {}).get(key)
        if v == old:
            r.data = {**r.data, key: new}
        elif isinstance(v, list) and old in v:
            r.data = {**r.data, key: [new if x == old else x for x in v]}
        else:
            continue
        n += 1
    db.session.flush()
    return n


def _change(ws, what, make):
    """Run an edit: (ops, renames) from `make`, saved; a refusal answered as {"error"} with status 400."""
    if not g.member.can("admin"):
        abort(403)
    try:
        ops_, renames = make()
        changes, before = _save(ws, ops_, renames, what)
    except (SchemaError, Refused) as exc:
        db.session.rollback()
        return None, (jsonify(error=str(exc)), 400)
    return {"said": changes, "before": before, "version": ws.version}, None


def _body():
    return request.get_json(silent=True) or {}


# --- the data model ------------------------------------------------------------------------------------------------------

def _model(ws, engine):
    model = studio.data_model(engine, lambda t, f: about_field(engine, t, f), engine.counts(),
                              ((ws.settings or {}).get("studio") or {}).get("positions"))
    model["types"] = [{"type": t, "label": KIND_WORDS.get(t, t)} for t in studio.NEW_TYPES]
    model["version"] = ws.version
    return model


@bp.route("/studio/data")
@login_required
def studio_data(wid):
    ws, engine = ctx(wid)
    return render_template("app/studio_data.html", ws=ws, e=engine, model=_model(ws, engine), can_edit=g.member.can("admin"))


@bp.route("/studio/data.json")
@login_required
def studio_data_json(wid):
    ws, engine = ctx(wid)
    return jsonify(_model(ws, engine))


@bp.route("/studio/data/change", methods=["POST"])
@login_required
def studio_data_change(wid):
    ws, engine = ctx(wid)
    change = _body()
    done, refused = _change(ws, "data model", lambda: studio.data_ops(ws.blueprint, change, _filled(ws)))
    if refused:
        return refused
    ws, engine = ctx(wid)
    return jsonify(ok=True, **done, model=_model(ws, engine))


@bp.route("/studio/data/place", methods=["POST"])
@login_required
def studio_data_place(wid):
    """Where the lists are drawn: kept for the workspace, apart from the design (moving a box isn't a change to it)."""
    ws, engine = ctx(wid, "admin")
    keys = {t["key"] for t in ws.blueprint.get("tables", [])}
    given = _body().get("positions") or {}
    places = dict(((ws.settings or {}).get("studio") or {}).get("positions") or {})
    for key, at in given.items():
        if key in keys and isinstance(at, list) and len(at) == 2 and all(isinstance(v, (int, float)) for v in at):
            places[key] = [round(at[0]), round(at[1])]
    if _body().get("reset"):
        places = {}
    ws.settings = {**(ws.settings or {}), "studio": {**((ws.settings or {}).get("studio") or {}), "positions": places}}
    db.session.commit()
    return jsonify(ok=True)


@bp.route("/studio/undo", methods=["POST"])
@login_required
def studio_undo(wid):
    """Take back a change made in a studio: the design goes back to the version it was made from."""
    ws, engine = ctx(wid, "admin")
    to = _body().get("to")
    if not isinstance(to, int) or to < 1 or to >= (ws.version or 0):
        return jsonify(error="There's nothing to take back"), 400
    kept = dict(((ws.settings or {}).get("studio") or {}).get("renamed") or {})
    undone = sorted((int(v) for v in kept if to < int(v) <= (ws.version or 0)), reverse=True)
    try:
        chat.restore(ws, to, by=current_user.id, why=f"Took back a change made in a studio (back to how it was at version {to})")
    except ValueError:
        return jsonify(error="That version isn't there any more"), 400
    for v in undone:  # choices renamed since: the records get their old names back
        for table, key, old, new in reversed(kept.pop(str(v))):
            _rename_records(ws, table, key, new, old)
    if undone:
        ws.settings = {**(ws.settings or {}), "studio": {**((ws.settings or {}).get("studio") or {}), "renamed": kept}}
    db.session.commit()
    return jsonify(ok=True, version=ws.version)


# --- workflows -----------------------------------------------------------------------------------------------------------

def _item(ws, engine, item_id):
    kind, _, key = str(item_id or "").partition(":")
    if kind == "stages":
        t = engine.visible_blueprint() and next((x for x in engine.visible_blueprint()["tables"] if x["key"] == key), None)
        if t is None or not t.get("workflow"):
            return None
        return studio.stage_diagram(engine, engine.tables[key], studio.counts_by_stage(engine, t))
    if kind == "rule":
        a = next((x for x in engine.bp.get("automations", []) if x["key"] == key), None)
        if a is None or (a["trigger"].get("table") and not engine.can(engine.tables.get(a["trigger"]["table"]))):
            return None
        runs = Run.query.filter_by(workspace_id=ws.id, automation=key).order_by(Run.id.desc()).limit(200).all()
        return studio.rule_diagram(engine, a, runs)
    if kind == "flow":
        f = next((x for x in engine.bp.get("flows") or [] if x["key"] == key), None)
        if f is None or not engine.can(engine.tables.get(f["table"])):
            return None
        return studio.flow_diagram(engine, f)
    return None


def _workflow_page_data(ws, engine):
    tables = [{"key": t["key"], "label": t["label"], "singular": t["singular"],
               "fields": [{"key": f["key"], "label": f["label"], "type": f["type"], "options": f.get("options") or [],
                           "stored": f["type"] in schema.STORED} for f in t["fields"]],
               "stages": bool(t.get("workflow"))} for t in engine.visible_blueprint().get("tables", [])]
    from ..engine.automations import OP_WORDS
    return {"items": studio.workflow_items(engine), "tables": tables, "version": ws.version,
            "steps": [{"type": k, "label": studio.STEP_WORDS[k]} for k in studio.STEP_TYPES],
            "ops": [{"op": k, "label": v, "value": k not in schema.NO_VALUE} for k, v in OP_WORDS.items() if k in schema.CONDITION_OPS],
            "teams": [{"key": tm["key"], "name": tm["name"]} for tm in schema.teams(engine.bp)]}


@bp.route("/studio/workflows")
@login_required
def studio_workflows(wid):
    ws, engine = ctx(wid)
    data = _workflow_page_data(ws, engine)
    first = next((i["id"] for grp in data["items"] for i in grp["items"]), None)
    opened = request.args.get("open") or first
    return render_template("app/studio_workflows.html", ws=ws, e=engine, data=data, opened=_item(ws, engine, opened) if opened else None,
                           can_edit=g.member.can("admin"))


@bp.route("/studio/workflows/item")
@login_required
def studio_workflow_item(wid):
    ws, engine = ctx(wid)
    item = _item(ws, engine, request.args.get("id"))
    if item is None:
        abort(404)
    return jsonify(item)


@bp.route("/studio/workflows/change", methods=["POST"])
@login_required
def studio_workflow_change(wid):
    ws, engine = ctx(wid)
    body = _body()
    item_id, change = str(body.get("id") or ""), body.get("change") or {}
    kind, _, key = item_id.partition(":")
    opened = {"id": item_id}

    def make():
        if kind == "stages" or change.get("do") == "add_stages":
            return studio.stage_ops(ws.blueprint, {**change, "table": change.get("table") or key}, _filled(ws))
        if kind == "rule" or change.get("do") == "add_rule":
            made, rule = studio.rule_ops(ws.blueprint, {**change, "key": change.get("key") or key})
            opened["id"] = f"rule:{rule}" if rule else ""
            return made, []
        if kind == "flow":
            made = studio.flow_ops(ws.blueprint, {**change, "key": key})
            opened["id"] = "" if change.get("do") == "remove_flow" else item_id
            return made, []
        raise SchemaError("That can't be changed here")
    done, refused = _change(ws, "workflow", make)
    if refused:
        return refused
    if change.get("do") == "add_stages":
        opened["id"] = f"stages:{change.get('table') or key}"
    if change.get("do") == "remove_stages":
        opened["id"] = ""
    ws, engine = ctx(wid)
    data = _workflow_page_data(ws, engine)
    return jsonify(ok=True, **done, items=data["items"], tables=data["tables"], id=opened["id"],
                   item=_item(ws, engine, opened["id"]) if opened["id"] else None)


@bp.route("/studio/workflows/try", methods=["POST"])
@login_required
def studio_workflow_try(wid):
    """What a rule would do for one record, step by step, without doing any of it."""
    ws, engine = ctx(wid)
    body = _body()
    key = str(body.get("id") or "").partition(":")[2]
    a = next((x for x in engine.bp.get("automations", []) if x["key"] == key), None)
    if a is None or not a["trigger"].get("table"):
        return jsonify(error="Only a rule about a list's records can be tried on one"), 400
    record = engine.get(int(body.get("record") or 0)) if str(body.get("record") or "").isdigit() else None
    if record is None or record.table != a["trigger"]["table"]:
        return jsonify(error="Choose a record to try it on"), 400
    return jsonify(studio.preview(engine, a, record))


@bp.route("/studio/workflows/records")
@login_required
def studio_workflow_records(wid):
    """Records of a list to try a rule on: the latest, or those whose name has the words typed."""
    ws, engine = ctx(wid)
    table = request.args.get("table", "")
    if table not in engine.tables or not engine.can(engine.tables[table]):
        abort(404)
    q = (request.args.get("q") or "").strip().lower()
    rows = list(reversed(engine.rows(table)[-400:]))
    found = [{"id": r.id, "title": engine.title(r)} for r in rows if not q or q in str(engine.title(r)).lower()][:30]
    return jsonify(items=found)
