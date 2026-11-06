"""The App map (app/appmap.py): the whole app drawn from its design, its processes and the data model behind them, with
each list's stages, each rule and each step-by-step flow drawn in full (app/studio.py) and changed by hand.

Everyone who can see the design can look; admins change it. Every change is made in the Keel language, as the AI
builder's are (app/lang), checked and saved as a version in one place (_save), and the studio can take it back again
(each change answers with the version it was made from).
"""
import copy
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import IntegrityError

from .. import appmap, lang, studio
from ..blueprint import ops, schema
from ..blueprint.schema import SchemaError
from ..builder import chat
from ..core.context import current_app
from ..extensions import db
from ..models import Record, Run
from .erp import ctx
from ..engine import automations
from .filters import KIND_WORDS, about_field, ago, pill, status_words
from .http import PageRoute, check_csrf, current_user, json_of, json_response, redirect, render, signed_in, url_for

router = APIRouter(prefix="/w/{wid:int}/app", route_class=PageRoute, dependencies=[Depends(check_csrf), Depends(signed_in)])


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
    text = re.sub(r"^(?:patch|change) \d+ \([^)]*\)[^:]*: ", "", problem, count=1)  # which patch it was is ours, not theirs
    try:
        text = chat.plain(text, bp)
    except Exception:  # noqa: BLE001 - the words as they are, if they can't be made plainer
        pass
    return text[:1].upper() + text[1:]


def _carried(op):
    """Whether Keel carries this design op as it is (a design_op patch: a flow, a number's colours)."""
    from ..lang.compile import ESCAPE, POLICY_KEYS
    return op.get("op") in ESCAPE and (op["op"] != "update_table" or set(op) <= POLICY_KEYS)


def _in_keel(design, op):
    """A design op the rule editor makes, as the Keel patch that makes it: a rule written whole (the rule read back into
    the language), a rule taken away, the AI filling a field in. [op] when the language has no words for it."""
    from ..lang import back
    if "do" in op:
        return [op]
    kind = op.get("op")
    if kind in ("add_automation", "update_automation") and isinstance(op.get("automation"), dict):
        from ..blueprint import schema
        try:  # as the design keeps it first: a trigger's "changes to Done" is its condition, never left behind
            return [{"do": "add_rule", "rule": back.rule_in_keel(design or {}, schema.normalize_automation(op["automation"]))}]
        except (back.Unsaid, schema.SchemaError):
            return [op]
    if kind == "remove_automation" and op.get("key"):
        return [{"do": "remove_rule", "rule": op["key"]}]
    if kind == "update_field" and set(op.get("changes") or {}) == {"ai"}:
        ai = op["changes"]["ai"]
        return [{"do": "set_field", "list": op["table"], "field": op["key"],
                 "settings": {"ai": (ai or {}).get("instructions", "") if isinstance(ai, dict) or ai is None else ai}}]
    return [op]


def _make(design, made, filled):
    """What the App map's edit makes of a design: (the new design, the changes said), or Refused saying why. `made` is
    Keel language patches, made through app/lang as the AI builder's are (with `filled` counting the records, so what's
    in use isn't thrown away), and design ops for what the language has no words for yet (a rule's steps, a field the
    AI fills in: studio.rule_ops, studio.data_patches), made after them; what was wrong already isn't this change's."""
    from ..lang import compile as keel
    made = [p for x in made for p in _in_keel(design, x)]  # a rule, a rule taken away, a field the AI fills in: said in Keel
    patches = [x if "do" in x else {"do": "design_op", "op": x} for x in made if "do" in x or _carried(x)]
    rest = [x for x in made if "do" not in x and not _carried(x)]
    bp, changes = copy.deepcopy(design or {}), []
    if patches:
        counting = keel.FILLED.set(filled)
        try:
            out = lang.apply(bp, patches)
        finally:
            keel.FILLED.reset(counting)
        if out.problems:
            raise Refused(_plain(out.problems[0], design))
        bp, changes = out.blueprint, list(out.changes)
    if rest:
        result = ops.apply(bp, rest, already=ops.wrong_already(design))
        if result.problems:
            raise Refused(_plain(result.problems[0], design))
        bp, changes = result.blueprint, changes + list(result.changes)
    if not changes:
        raise Refused("That's how it is already")
    return bp, changes


def _save(ws, made, plain, user):
    """The one place the App map changes an app: made (_make) and saved as one version (a choice renamed
    takes its records with it: chat.save_version), noted as `plain` says it if given, as `user`'s. Returns (the changes
    said, the version it was made from)."""
    bp, changes = _make(ws.blueprint, made, _filled(ws))
    before = ws.version or 0
    changes = [plain] if plain else changes
    chat.save_version(ws, bp, changes, "studio", by=user.id)
    return changes, before


def _change(request, ws, make, plain=None):
    """Run an edit: the patches (and design ops) `make` returns, saved; a refusal answered as {"error"} with status 400."""
    if not request.state.member.can("admin"):
        raise HTTPException(403)
    try:
        changes, before = _save(ws, make(), plain, current_user(request))
    except (SchemaError, Refused) as exc:
        db.session.rollback()
        show = getattr(exc, "show", None)  # the records it's about, to go and see
        return None, json_response({"error": str(exc), **({"show": {"label": show["label"], "url": _records_at(ws, show)}} if show else {})}, 400)
    except IntegrityError:  # two changes at the same moment (a double click): the first stands
        db.session.rollback()
        return None, json_response({"error": "That arrived twice at once: the first one was saved. Have a look, and change it again if you need to"}, 409)
    return {"said": changes, "before": before, "version": ws.version}, None


def _records_at(ws, show):
    """The list's page showing only the records with that value, in a tab that doesn't leave any out."""
    t = schema.table(ws.blueprint, show["table"]) or {}
    views = t.get("views") or []
    whole = next((v["key"] for v in views if not v.get("filters")), "_all" if views else None)
    return url_for("app.table", wid=ws.id, table=show["table"], **({"view": whole} if whole else {}), **{f"f_{show['field']}": show["value"]})


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


@router.api_route("/studio/data", methods=["GET"], name="app.studio_data")
def studio_data(request: Request, wid: int):
    """The data model: the App map's Data view (a list named in ?open= selected)."""
    ctx(wid, "viewer", request)
    opened = request.query_params.get("open")
    add = {"add": "column"} if opened and request.query_params.get("add") == "column" else {}  # its add-a-column form open
    return redirect(url_for("app.studio_map", wid=wid, view="data", open=f"data:{opened}" if opened else "data", **add))


@router.api_route("/studio/data.json", methods=["GET"], name="app.studio_data_json")
def studio_data_json(request: Request, wid: int):
    ws, engine = ctx(wid, "viewer", request)
    return json_response(_model(ws, engine))


@router.api_route("/studio/data/change", methods=["POST"], name="app.studio_data_change")
def studio_data_change(request: Request, wid: int, data=Depends(json_of)):
    ws, engine = ctx(wid, "viewer", request)
    change = data or {}
    done, refused = _change(request, ws, lambda: studio.data_patches(ws.blueprint, change, _filled(ws)))
    if refused:
        return refused
    ws, engine = ctx(wid, "viewer", request)
    return json_response({"ok": True, **done, "model": _model(ws, engine)})


@router.api_route("/studio/data/place", methods=["POST"], name="app.studio_data_place")
def studio_data_place(request: Request, wid: int, data=Depends(json_of)):
    """Where the lists are drawn: kept for the workspace, apart from the design (moving a box isn't a change to it)."""
    ws, engine = ctx(wid, "admin", request)
    keys = {t["key"] for t in ws.blueprint.get("tables", [])}
    given = (data or {}).get("positions") or {}
    places = dict(((ws.settings or {}).get("studio") or {}).get("positions") or {})
    for key, at in given.items():
        if key in keys and isinstance(at, list) and len(at) == 2 and all(isinstance(v, (int, float)) for v in at):
            places[key] = [round(at[0]), round(at[1])]
    if (data or {}).get("reset"):
        places = {}
    ws.settings = {**(ws.settings or {}), "studio": {**((ws.settings or {}).get("studio") or {}), "positions": places}}
    db.session.commit()
    return json_response({"ok": True})


@router.api_route("/studio/undo", methods=["POST"], name="app.studio_undo")
def studio_undo(request: Request, wid: int, user=Depends(signed_in), data=Depends(json_of)):
    """Take back a change made in a studio: the design goes back to the version it was made from."""
    ws, engine = ctx(wid, "admin", request)
    to = (data or {}).get("to")
    if not isinstance(to, int) or to < 1 or to >= (ws.version or 0):
        return json_response({"error": "There's nothing to take back"}, 400)
    try:  # (restore names the records as they were then too: choices renamed since get their old names back)
        chat.restore(ws, to, by=user.id, why="Undid a change made on the App map")
    except ValueError:
        return json_response({"error": "That version isn't there any more"}, 400)
    return json_response({"ok": True, "version": ws.version})


# --- workflows -----------------------------------------------------------------------------------------------------------

def _item(request, ws, engine, item_id):
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
        member = request.state.member
        drawn["hook"] = automations.hook_url(ws.id, key) if a["trigger"]["type"] == "webhook_received" and member.can("admin") else None
        drawn["can_run"] = a["trigger"]["type"] == "schedule" and member.can("admin" if automations.reaches_out(a) else "editor")
        return drawn
    if kind == "flow":
        f = next((x for x in engine.bp.get("flows") or [] if x["key"] == key), None)
        if f is None or not engine.can(engine.tables.get(f["table"])):
            return None
        return studio.flow_diagram(engine, f)
    return None


DAYS_SAID = {"within_next_days": "is within the next … days", "within_last_days": "is within the last … days",
             "more_than_days_ago": "is more than … days ago"}  # each test said once, the days in it


def _workflow_page_data(ws, engine):
    tables = [{"key": t["key"], "label": t["label"], "singular": t["singular"],
               "fields": [{"key": f["key"], "label": f["label"], "type": f["type"], "options": f.get("options") or [],
                           "stored": f["type"] in schema.STORED} for f in t["fields"]],
               "stages": bool(t.get("workflow"))} for t in engine.visible_blueprint().get("tables", [])]
    from ..engine.automations import OP_WORDS
    return {"items": studio.workflow_items(engine), "tables": tables, "version": ws.version,
            "steps": [{"type": k, "label": studio.STEP_WORDS[k]} for k in studio.STEP_TYPES],
            "ops": [{"op": k, "label": DAYS_SAID.get(k, v), "value": k not in schema.NO_VALUE} for k, v in OP_WORDS.items() if k in schema.CONDITION_OPS],
            "teams": [{"key": tm["key"], "name": tm["name"]} for tm in schema.teams(engine.bp)]}


@router.api_route("/studio/workflows", methods=["GET"], name="app.studio_workflows")
def studio_workflows(request: Request, wid: int):
    """The processes: the App map, with what's named in ?open= (or a list's own, ?table=) opened."""
    ctx(wid, "viewer", request)
    args = request.query_params
    return redirect(url_for("app.studio_map", wid=wid, **{k: v for k, v in (("open", args.get("open")), ("table", args.get("table"))) if v}))


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


@router.api_route("/studio", methods=["GET"], name="app.studio_map")
def studio_map(request: Request, wid: int):
    """The App map: every process the app runs, from the first list to the last, drawn from its design, with the data
    model behind it; click into any part to see it in full and (admins) change it."""
    ws, engine = ctx(wid, "viewer", request)
    data = _workflow_page_data(ws, engine)
    table = request.query_params.get("table")  # a list's own: its stages, else its first rule or flow
    own = next((i["id"] for grp in data["items"] if grp["table"] == table for i in grp["items"]), None) if table else None
    opened = request.query_params.get("open") or own or "map"
    item = _item(request, ws, engine, opened) if opened.partition(":")[0] in ("stages", "rule", "flow") else None
    drawn = _map(ws, engine)
    return render(request, "app/studio.html", ws=ws, e=engine, data=data, opened=item, open_id=opened, map=drawn,
                  model=_model(ws, engine), view="data" if request.query_params.get("view") == "data" or opened.startswith("data") else "map",
                  can_edit=request.state.member.can("admin"), ask_example=_ask_example(drawn), quiet_ask=True, email_on=_email_on(ws))


def _email_on(ws):
    """Whether the app can send email (Microsoft 365 connected): “email me” is offered only then."""
    from ..connectors import microsoft
    conn = microsoft.connection(ws)
    return bool(conn is not None and conn.secret and conn.status != "not connected")


def _ask_example(drawn):
    """What someone might ask for, said with this app's own lists: “when a job reaches Signed off, tell me”."""
    staged = [c for p in drawn["processes"] for c in p["cards"] if len(c["stages"]) >= 2]
    if staged:
        c = staged[min(1, len(staged) - 1)]
        return f"when {appmap.a(c['singular'].lower())} reaches {c['stages'][-2 if len(c['stages']) > 2 else -1]['name']}, tell me"
    lists = [c for p in drawn["processes"] for c in p["cards"]] + drawn["reference"]
    return f"when a new {lists[0]['singular'].lower()} is added, tell me" if lists else "keep a list of our customers and their jobs"


@router.api_route("/studio/map.json", methods=["GET"], name="app.studio_map_json")
def studio_map_json(request: Request, wid: int):
    """The map worked out again, with the stages, rules and flows its panels open (after a change made anywhere)."""
    ws, engine = ctx(wid, "viewer", request)
    return json_response({"map": _map(ws, engine), "data": _workflow_page_data(ws, engine)})


@router.api_route("/studio/map/name", methods=["POST"], name="app.studio_map_name")
def studio_map_name(request: Request, wid: int, data=Depends(json_of)):
    """A process named as the business calls it (“Quote to cash”): kept with the workspace, apart from the design."""
    ws, engine = ctx(wid, "admin", request)
    body = data or {}
    key, name = str(body.get("process") or ""), " ".join(str(body.get("name") or "").split())[:60]
    if key not in {p["key"] for p in appmap.processes(engine.bp)[0]}:
        return json_response({"error": "That process isn't there any more"}, 400)
    names = dict(((ws.settings or {}).get("map") or {}).get("names") or {})
    if name:
        names[key] = name
    else:
        names.pop(key, None)  # named after its lists again
    ws.settings = {**(ws.settings or {}), "map": {**((ws.settings or {}).get("map") or {}), "names": names}}
    db.session.commit()
    return json_response({"ok": True, "map": _map(ws, engine)})


@router.api_route("/studio/map/play", methods=["POST"], name="app.studio_map_play")
def studio_map_play(request: Request, wid: int, data=Depends(json_of)):
    """One of the app's kept examples acted out in a throwaway copy: what it went through (each record's stages, in
    order, and the rules that ran) and whether it came out as expected. Nothing in the real app changes."""
    ws, engine = ctx(wid, "editor", request)
    kept = (ws.state or {}).get("examples") or []
    n = (data or {}).get("example")
    if not isinstance(n, int) or not 0 <= n < len(kept):
        return json_response({"error": "That example isn't there any more"}, 400)
    from ..builder import trial
    try:
        played = trial.walkthrough(ws, kept[n])
    except Exception:  # noqa: BLE001 - a fault of ours: said, never the app's
        current_app.logger.exception("Playing an example for workspace %s failed", ws.id)
        db.session.rollback()
        return json_response({"error": "Something went wrong on our side playing that: it's been logged"}, 500)
    return json_response({"title": played["title"], "ok": played["ok"], "lines": [[ok, line] for ok, line in played["lines"]],
                          "problem": played.get("plain") or "", "went": played["went"], "ran": played["ran"]})


@router.api_route("/studio/workflows/item", methods=["GET"], name="app.studio_workflow_item")
def studio_workflow_item(request: Request, wid: int):
    ws, engine = ctx(wid, "viewer", request)
    item = _item(request, ws, engine, request.query_params.get("id"))
    if item is None:
        raise HTTPException(404)
    return json_response(item)


@router.api_route("/studio/workflows/change", methods=["POST"], name="app.studio_workflow_change")
def studio_workflow_change(request: Request, wid: int, data=Depends(json_of)):
    ws, engine = ctx(wid, "viewer", request)
    body = data or {}
    item_id, change = str(body.get("id") or ""), body.get("change") or {}
    kind, _, key = item_id.partition(":")
    opened = {"id": item_id}

    def make():
        if kind == "stages" or change.get("do") == "add_stages":
            return studio.stage_patches(ws.blueprint, {**change, "table": change.get("table") or key}, _filled(ws))
        if kind == "rule" or change.get("do") == "add_rule":  # a rule's steps: design ops, the language can't say them all yet
            made, rule = studio.rule_ops(ws.blueprint, {**change, "key": change.get("key") or key})
            opened["id"] = f"rule:{rule}" if rule else ""
            return made
        if kind == "flow":  # a flow's screens: design ops Keel carries as they are
            made = studio.flow_ops(ws.blueprint, {**change, "key": key})
            opened["id"] = "" if change.get("do") == "remove_flow" else item_id
            return made
        raise SchemaError("That can't be changed here")
    plain = studio.stage_said(ws.blueprint, {**change, "table": change.get("table") or key}) if kind == "stages" else None
    done, refused = _change(request, ws, make, plain)
    if refused:
        return refused
    if change.get("do") == "add_stages":
        opened["id"] = f"stages:{change.get('table') or key}"
    if change.get("do") == "remove_stages":
        opened["id"] = ""
    ws, engine = ctx(wid, "viewer", request)
    data = _workflow_page_data(ws, engine)
    return json_response({"ok": True, **done, "items": data["items"], "tables": data["tables"], "id": opened["id"],
                          "item": _item(request, ws, engine, opened["id"]) if opened["id"] else None})


@router.api_route("/studio/workflows/try", methods=["POST"], name="app.studio_workflow_try")
def studio_workflow_try(request: Request, wid: int, data=Depends(json_of)):
    """What a rule would do for one record, step by step, without doing any of it."""
    ws, engine = ctx(wid, "viewer", request)
    body = data or {}
    key = str(body.get("id") or "").partition(":")[2]
    a = next((x for x in engine.bp.get("automations", []) if x["key"] == key), None)
    if a is None or not a["trigger"].get("table"):
        return json_response({"error": "Only a rule about a list's records can be tried on one"}, 400)
    record = engine.get(int(body.get("record") or 0)) if str(body.get("record") or "").isdigit() else None
    if record is None or record.table != a["trigger"]["table"]:
        return json_response({"error": "Choose a record to try it on"}, 400)
    return json_response(studio.preview(engine, a, record))


@router.api_route("/studio/workflows/records", methods=["GET"], name="app.studio_workflow_records")
def studio_workflow_records(request: Request, wid: int):
    """Records of a list to try a rule on: the latest, or those whose name has the words typed."""
    ws, engine = ctx(wid, "viewer", request)
    table = request.query_params.get("table", "")
    if table not in engine.tables or not engine.can(engine.tables[table]):
        raise HTTPException(404)
    q = (request.query_params.get("q") or "").strip().lower()
    rows = list(reversed(engine.rows(table)[-400:]))
    found = [{"id": r.id, "title": engine.title(r), "detail": _told_apart(engine, table, r)} for r in rows if not q or q in str(engine.title(r)).lower()][:30]
    return json_response({"items": found})


def _told_apart(engine, table, r):
    """A second line for a record in a list to pick from, so ones named alike (three payments of one invoice) differ:
    when it was added, and its first amount or stage."""
    t = engine.tables[table]
    bits = [f"added {ago(r.created_at)}"] if r.created_at else []
    for f in t["fields"]:
        v = (r.data or {}).get(f["key"])
        if v not in (None, "", []) and (engine.kind(f) == "money" or f["type"] == "select") and f["key"] != t.get("title_field"):
            bits.append(f"{f['label']}: {engine.display(f, v)}")
        if len(bits) >= 3:
            break
    return " · ".join(bits)
