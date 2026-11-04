"""The App map (app/appmap.py): the whole app drawn from its design, its processes and the data model behind them, with
each list's stages, each rule and each step-by-step flow drawn in full (app/studio.py) and changed by hand.

Everyone who can see the design can look; admins change it. Every change is checked like the builder's and saved as a
version, and the studio can take it back again (each change answers with the version it was made from).
"""
import copy

from flask import abort, current_app, g, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from .. import appmap, studio
from ..blueprint import ops, schema
from ..blueprint.schema import SchemaError
from ..builder import chat
from ..extensions import db
from ..models import Record, Run
from .erp import bp, ctx
from ..engine import automations
from .filters import KIND_WORDS, about_field, ago, pill, status_words


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


def _save(ws, ops_, what):
    """Apply the changes to the design, check it, and save it as a version (a choice renamed takes its records with it:
    chat.save_version). Returns (the changes said, the version it was made from)."""
    # what's wrong already isn't this change's doing: what the design fails now, and what saving it as it is would
    # (an app made before a newer way of saying something, brought up to date on any save)
    already = schema.check(ws.blueprint or {}) + ops.apply(ws.blueprint or {}, [], always_check=True).problems
    result = ops.apply(copy.deepcopy(ws.blueprint or {}), ops_, already=already)
    if result.problems:
        raise Refused(_plain(result.problems[0], ws.blueprint))
    changes = list(result.changes)
    if not changes:
        raise Refused("That's how it is already")
    before = ws.version or 0
    chat.save_version(ws, result.blueprint, changes, "studio", by=current_user.id)
    return changes, before


def _change(ws, what, make):
    """Run an edit: the ops `make` returns, saved; a refusal answered as {"error"} with status 400."""
    if not g.member.can("admin"):
        abort(403)
    try:
        ops_, _ = make()
        changes, before = _save(ws, ops_, what)
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
    model["colours"] = schema.CHOICE_COLOURS
    filled = {}
    for table, data in db.session.query(Record.table, Record.data).filter(Record.workspace_id == ws.id, Record.deleted_at.is_(None)):
        counts = filled.setdefault(table, {})
        for k, v in (data or {}).items():
            if v not in (None, "", []):
                counts[k] = counts.get(k, 0) + 1
    for t in model["tables"]:
        for f in t["fields"]:
            f["filled"] = filled.get(t["key"], {}).get(f["key"], 0)  # what a kind can change to depends on it
            if f["options"]:  # each choice's colour as the screens show it
                raw = schema.field(engine.tables[t["key"]], f["key"])
                f["colours"] = {o: pill(raw, o) for o in f["options"]}
    model["version"] = ws.version
    return model


@bp.route("/studio/data")
@login_required
def studio_data(wid):
    """The data model: the App map's Data view (a list named in ?open= selected)."""
    ctx(wid)
    opened = request.args.get("open")
    add = {"add": "column"} if opened and request.args.get("add") == "column" else {}  # its add-a-column form open
    return redirect(url_for("app.studio_map", wid=wid, view="data", open=f"data:{opened}" if opened else "data", **add))


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
    try:  # (restore names the records as they were then too: choices renamed since get their old names back)
        chat.restore(ws, to, by=current_user.id, why=f"Took back a change made in a studio (back to how it was at version {to})")
    except ValueError:
        return jsonify(error="That version isn't there any more"), 400
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
        drawn = studio.rule_diagram(engine, a, runs)
        # what the rules list shows, here too: its last runs step by step, its address for other systems, running it now
        drawn["recent"] = [{"status": r.status, "said": status_words(r.status), "trigger": r.trigger or "", "when": ago(r.created_at),
                            "error": r.error if r.status not in ("done",) else "",
                            "steps": [{"text": x.get("result") or x.get("action") or "", "skipped": bool(x.get("skipped"))} for x in (r.steps or [])][:12]}
                           for r in runs[:8]]
        drawn["hook"] = automations.hook_url(ws.id, key) if a["trigger"]["type"] == "webhook_received" and g.member.can("admin") else None
        drawn["can_run"] = a["trigger"]["type"] == "schedule" and g.member.can("admin" if automations.reaches_out(a) else "editor")
        return drawn
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
    """The processes: the App map, with what's named in ?open= (or a list's own, ?table=) opened."""
    ctx(wid)
    return redirect(url_for("app.studio_map", wid=wid, **{k: v for k, v in (("open", request.args.get("open")), ("table", request.args.get("table"))) if v}))


# --- the App map ---------------------------------------------------------------------------------------------------------

def _map(ws, engine):
    """The whole app as one picture (app/appmap.py), with what the design checks find wrong where, and the examples
    kept to play through it."""
    from ..builder import checks
    try:
        problems = checks.run(engine)
    except Exception:  # noqa: BLE001 - the map is still drawn; the checks page says what went wrong
        current_app.logger.exception("Checking the design of workspace %s for the App map failed", ws.id)
        problems = []
    found = appmap.build(engine, problems, ((ws.settings or {}).get("map") or {}).get("names"))
    tables = {t["key"] for t in engine.visible_blueprint().get("tables", [])}
    kept = []
    for n, x in enumerate((ws.state or {}).get("examples") or []):
        named = {s.get("table") for s in x.get("steps") or [] if isinstance(s, dict) and s.get("table")}
        if named and named <= tables | {None}:  # what someone can't see isn't played to them
            kept.append({"n": n, "title": str(x.get("title") or "An example"), "tables": sorted(k for k in named if k)})
    last = (ws.state or {}).get("checks") or {}
    found.update(examples=kept, version=ws.version,
                 last={"at": last.get("at"), "failing": [x["title"] for x in last.get("examples", []) if not x["ok"]][:6],
                       "issues": list(last.get("issues") or [])[:6]} if last else None)
    return found


@bp.route("/studio")
@login_required
def studio_map(wid):
    """The App map: every process the app runs, from the first list to the last, drawn from its design, with the data
    model behind it; click into any part to see it in full and (admins) change it."""
    ws, engine = ctx(wid)
    data = _workflow_page_data(ws, engine)
    table = request.args.get("table")  # a list's own: its stages, else its first rule or flow
    own = next((i["id"] for grp in data["items"] if grp["table"] == table for i in grp["items"]), None) if table else None
    opened = request.args.get("open") or own or "map"
    item = _item(ws, engine, opened) if opened.partition(":")[0] in ("stages", "rule", "flow") else None
    drawn = _map(ws, engine)
    return render_template("app/studio.html", ws=ws, e=engine, data=data, opened=item, open_id=opened, map=drawn,
                           model=_model(ws, engine), view="data" if request.args.get("view") == "data" or opened.startswith("data") else "map",
                           can_edit=g.member.can("admin"), ask_example=_ask_example(drawn), quiet_ask=True)


def _ask_example(drawn):
    """What someone might ask for, said with this app's own lists: “when a job reaches Signed off, tell me”."""
    staged = [c for p in drawn["processes"] for c in p["cards"] if len(c["stages"]) >= 2]
    if staged:
        c = staged[min(1, len(staged) - 1)]
        return f"when {appmap.a(c['singular'].lower())} reaches {c['stages'][-2 if len(c['stages']) > 2 else -1]['name']}, tell me"
    lists = [c for p in drawn["processes"] for c in p["cards"]] + drawn["reference"]
    return f"when a new {lists[0]['singular'].lower()} is added, tell me" if lists else "keep a list of our customers and their jobs"


@bp.route("/studio/map.json")
@login_required
def studio_map_json(wid):
    """The map worked out again, with the stages, rules and flows its panels open (after a change made anywhere)."""
    ws, engine = ctx(wid)
    return jsonify(map=_map(ws, engine), data=_workflow_page_data(ws, engine))


@bp.route("/studio/map/name", methods=["POST"])
@login_required
def studio_map_name(wid):
    """A process named as the business calls it (“Quote to cash”): kept with the workspace, apart from the design."""
    ws, engine = ctx(wid, "admin")
    body = _body()
    key, name = str(body.get("process") or ""), " ".join(str(body.get("name") or "").split())[:60]
    if key not in {p["key"] for p in appmap.processes(engine.bp)[0]}:
        return jsonify(error="That process isn't there any more"), 400
    names = dict(((ws.settings or {}).get("map") or {}).get("names") or {})
    if name:
        names[key] = name
    else:
        names.pop(key, None)  # named after its lists again
    ws.settings = {**(ws.settings or {}), "map": {**((ws.settings or {}).get("map") or {}), "names": names}}
    db.session.commit()
    return jsonify(ok=True, map=_map(ws, engine))


@bp.route("/studio/map/play", methods=["POST"])
@login_required
def studio_map_play(wid):
    """One of the app's kept examples acted out in a throwaway copy: what it went through (each record's stages, in
    order, and the rules that ran) and whether it came out as expected. Nothing in the real app changes."""
    ws, engine = ctx(wid, "editor")
    kept = (ws.state or {}).get("examples") or []
    n = _body().get("example")
    if not isinstance(n, int) or not 0 <= n < len(kept):
        return jsonify(error="That example isn't there any more"), 400
    from ..builder import trial
    try:
        played = trial.walkthrough(ws, kept[n])
    except Exception:  # noqa: BLE001 - a fault of ours: said, never the app's
        current_app.logger.exception("Playing an example for workspace %s failed", ws.id)
        db.session.rollback()
        return jsonify(error="Something went wrong on our side playing that: it's been logged"), 500
    return jsonify(title=played["title"], ok=played["ok"], lines=[[ok, line] for ok, line in played["lines"]],
                   problem=played.get("plain") or "", went=played["went"], ran=played["ran"])


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
