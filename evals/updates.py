"""Changing an app after it's built, the two ways an owner can, each tried as they would.

By hand, on the App map: the requests the page itself sends, signed in as the owner, in a throwaway copy of the app. A
column added to a list; a rule made to tell them when one reaches a stage (then tried: are they told?) and the same
pressed twice; a stage put after the first and renamed; a stage that rule waits for removed (it must say no, naming the
rule); the rename undone. After each, the App map is worked out again and must show it, and the server must never fail;
after them all, the business's stories must still pass.

By asking the AI, in the builder: the same three changes in the owner's words, one turn each, through the real builder
(chat.ask + chat.work, as the app does). After each: did it make the change, did it say so plainly, and do the stories
that passed before still pass.

Each change is {"how": "map" | "ai", "change", "ok", "why", "said", "seconds"?}. What it says (the App map's notes, the
builder's replies) is looked at as a beginner would by ux.words_found.
"""
import re
import time

from app import appmap
from app.builder import trial as trials
from app.extensions import db

from . import stories, ux

DONE = re.compile(r"done|finish|complete|signed off|delivered|paid|closed|won|accepted", re.I)


def target(engine):
    """The list the changes are made to: the busiest step of the first process with three stages or more (the one an
    owner would most likely want to change), or None when the app has no list that moves along stages."""
    drawn = appmap.build(engine)
    cards = [c for p in drawn["processes"] for c in p["cards"] if len(c["stages"]) >= 3 and c["field"]]
    cards = cards or [c for c in drawn["reference"] if len(c["stages"]) >= 3 and c["field"]]
    if not cards:
        return None
    c = max(cards, key=lambda c: (len(c["rules"]), len(c["stages"])))
    names = [st["name"] for st in c["stages"]]
    told = next((n for n in names[1:] if DONE.search(n)), names[-1])
    column = next(label for label in ("Team note", "Note for the team", "Inside note") if not any(f["label"].lower() == label.lower()
                                                                                             for f in engine.tables[c["key"]]["fields"]))
    held = next(n for n in ("On hold", "Paused", "Waiting a while") if n not in names)
    return {"table": c["key"], "label": c["label"], "singular": c["singular"], "field": c["field"], "stages": names, "workflow": c["workflow"],
            "told": told, "column": column, "held": held, "after": names[0], "renamed": "Waiting" if "Waiting" not in names else "Held up"}


def a(word):
    return f"{'an' if word[:1].lower() in 'aeiou' else 'a'} {word}"


# --- by hand, on the App map ------------------------------------------------------------------------------------------

def by_hand(workspace, scenario_key):
    """The changes made as the App map makes them, in a copy. Returns [change]."""
    out = []
    with trials.copy_of(workspace) as trial:
        walked = trials.walk(trial, screens=False)  # a record in each list, as people would have
        aim = target(trial.engine())
        if aim is None:
            return [{"how": "map", "change": "anything", "ok": None, "why": "no list moves along stages: nothing to change on the map"}], {}
        client, wid, key = trial.client(), trial.ws_id, aim["table"]
        base = f"/w/{wid}/app/studio"

        def send(path, body):
            r = client.post(f"{base}/{path}", json=body)
            got = r.get_json(silent=True) or {}
            return r.status_code, got

        def design():
            db.session.expire_all()
            return trial.ws.blueprint

        def table():
            return next(t for t in design()["tables"] if t["key"] == key)

        def options():
            return next(f for f in table()["fields"] if f["key"] == aim["field"])["options"]

        def drawn():
            r = client.get(f"{base}/map.json")
            return r.status_code, (r.get_json(silent=True) or {}).get("map") or {}

        def card(m):
            return next((c for p in m.get("processes", []) for c in p["cards"] if c["key"] == key),
                        next((c for c in m.get("reference", []) if c["key"] == key), None))

        def note(change, ok, why="", said=None):
            out.append({"how": "map", "change": change, "ok": ok, "why": why, "said": said or []})

        # a column, added from the list's card
        status, got = send("data/change", {"do": "add_field", "table": key, "label": aim["column"], "type": "text"})
        has = any(f["label"] == aim["column"] for f in table()["fields"])
        note(f"add a column “{aim['column']}” to {aim['label']}", status == 200 and has,
             "" if status == 200 and has else f"the server said {status}: {got.get('error') or 'nothing'}", got.get("said"))

        # tell me when one reaches a stage; pressed twice; then is the owner told?
        rule = {"id": "", "change": {"do": "add_rule", "name": f"When {a(aim['singular'].lower())} reaches {aim['told']}, tell the owner",
                                     "trigger": {"type": "record_updated", "table": key, "field": aim["field"], "to": aim["told"]},
                                     "step": {"type": "notify", "to": "owner", "message": "{{record}} is now " + aim["told"]}}}
        status, got = send("workflows/change", rule)
        made = (got.get("id") or "").partition(":")[2]
        _, m = drawn()
        shown = bool(made) and any(r["key"] == made for r in (card(m) or {}).get("rules", []))
        note(f"tell me when {a(aim['singular'].lower())} reaches {aim['told']}", status == 200 and shown,
             "" if status == 200 and shown else f"the server said {status}: {got.get('error') or ''}" if status != 200 else "it isn't on the map",
             got.get("said"))
        again, got2 = send("workflows/change", rule)
        twice = sum(1 for x in design().get("automations", []) if x["name"] == rule["change"]["name"])
        note("the same rule pressed twice", again == 400 and twice == 1, "" if again == 400 and twice == 1 else
             f"pressed twice it answered {again} and there are {twice} of it", [got2.get("error")] if got2.get("error") else [])
        if made:
            note(f"the owner is told when {a(aim['singular'].lower())} reaches {aim['told']}", *_told(trial, aim, walked["made"]))

        # a stage put after the first, then renamed
        stages = f"stages:{key}"
        status, got = send("workflows/change", {"id": stages, "change": {"do": "add_stage", "table": key, "field": aim["field"],
                                                                         "name": aim["held"], "after": aim["after"]}})
        now = options()
        placed = aim["held"] in now and now.index(aim["held"]) == now.index(aim["after"]) + 1
        leads = True
        if placed and aim["workflow"]:  # a stage of its own: it's reached, and it leads on
            moves = table()["workflow"]["transitions"]
            nxt = now[now.index(aim["held"]) + 1] if now.index(aim["held"]) + 1 < len(now) else None
            leads = any(aim["held"] == mv["to"] for mv in moves) and (nxt is None or any(aim["held"] in (mv.get("from") or []) and mv["to"] == nxt for mv in moves))
        _, m = drawn()
        on_map = aim["held"] in [st["name"] for st in (card(m) or {}).get("stages", [])]
        ok = status == 200 and placed and leads and on_map
        note(f"add a stage “{aim['held']}” after {aim['after']}", ok, "" if ok else
             f"the server said {status}: {got.get('error') or ''}" if status != 200 else "it isn't after it" if not placed
             else "nothing moves into it, or on from it" if not leads else "it isn't on the map", got.get("said"))
        before_rename = trial.ws.version
        status, got = send("workflows/change", {"id": stages, "change": {"do": "rename_stage", "table": key, "field": aim["field"],
                                                                         "old": aim["held"], "new": aim["renamed"]}})
        now = options()
        ok = status == 200 and aim["renamed"] in now and aim["held"] not in now
        note(f"rename the stage “{aim['held']}” to “{aim['renamed']}”", ok, "" if ok else f"the server said {status}: {got.get('error') or ''}", got.get("said"))

        # a stage the new rule waits for can't be removed without saying which rule
        if made:
            status, got = send("workflows/change", {"id": stages, "change": {"do": "remove_stage", "table": key, "field": aim["field"], "name": aim["told"]}})
            ok = status == 400 and bool(got.get("error")) and aim["told"] in options()  # refused, saying why (a rule waits for it, or records are at it)
            note(f"remove the stage {aim['told']}, which a rule waits for", ok, "" if ok else
                 f"it answered {status}: {got.get('error') or 'and took it away'}", [got.get("error")] if got.get("error") else [])

        # undo the rename
        status, got = send("undo", {"to": before_rename})
        now = options()
        ok = status == 200 and aim["held"] in now and aim["renamed"] not in now
        note("undo the rename", ok, "" if ok else f"it answered {status}: {got.get('error') or 'and it is as it was'}")

        # the map is drawn, and the stories that passed still pass
        status, _ = drawn()
        if status != 200:
            note("the App map is drawn after the changes", False, f"it answered {status}")
        db.session.expire_all()
        after = {st["story"]: st["result"] for st in stories.run(trial.ws, scenario_key)}
    return out, after


def _told(trial, aim, made):
    """(ok, why): a record moved to the stage, as people would, and the owner has a message saying so."""
    from app.engine.records import FieldErrors
    from app.models import Notification
    record_id = made.get(aim["table"])
    if record_id is None:
        return None, "there was no record to move"
    engine = trial.engine()
    t = engine.tables[aim["table"]]
    before = Notification.query.filter_by(workspace_id=trial.ws_id, user_id=trial.owner_id).count()
    try:
        record = engine.get(record_id)
        if (record.data or {}).get(aim["field"]) == aim["told"]:  # trying it out took it there already: a new one, as people would add
            record, _ = trials._add(engine, t, trials.sample_values(engine, t, trial.owner_id, made, 99))
            if record is None:
                return None, "there was no record to move"
            before = Notification.query.filter_by(workspace_id=trial.ws_id, user_id=trial.owner_id).count()
        if t.get("workflow"):
            trials._take_along(engine, record, t, aim["told"])
        else:
            engine.update(record, {aim["field"]: aim["told"]})
    except FieldErrors as exc:
        return None, f"it couldn't be moved there to see: {'; '.join(exc.errors.values())[:160]}"
    db.session.expire_all()
    told = Notification.query.filter_by(workspace_id=trial.ws_id, user_id=trial.owner_id).count() - before
    return told > 0, "" if told > 0 else "it reached the stage and the owner wasn't told"


# --- by asking the AI ---------------------------------------------------------------------------------------------------

def by_asking(ws, owner, scenario_key, before):
    """The same changes asked of the builder, in the owner's words, one turn each, on the app itself. `before`: the
    stories as they were when it was handed over. Returns [change]."""
    from app.builder import chat
    from app.models import Message, Workspace
    aim = target(_engine(ws))
    if aim is None:
        return []
    one = aim["singular"].lower()
    asks = [
        (f"add a column “{aim['column']}”", f"Can you add a “{aim['column']}” to {aim['label'].lower()}, so we can leave each other a note?",
         lambda bp: any(f["label"].lower() == aim["column"].lower() for f in _table(bp, aim["table"])["fields"])),
        (f"tell me when {a(one)} reaches {aim['told']}", f"When {a(one)} gets to {aim['told']}, let me know.",
         lambda bp: _tells(bp, aim)),
        (f"add a stage “{aim['held']}” after {aim['after']}", f"We need a stage “{aim['held']}” for {aim['label'].lower()}, straight after {aim['after']}.",
         lambda bp: _placed(bp, aim)),
    ]
    out, worked = [], {k for k, v in before.items() if v == "pass"}
    for change, say, done in asks:
        reply = chat.ask(ws, say, user=owner)
        started = time.time()
        try:
            chat.work(reply.id)
        except Exception as exc:  # noqa: BLE001 - the builder failing is the finding
            db.session.rollback()
            out.append({"how": "ai", "change": change, "ok": False, "why": f"the builder failed: {type(exc).__name__}: {exc}"[:200], "said": []})
            continue
        db.session.expire_all()
        reply = db.session.get(Message, reply.id)
        ws = db.session.get(Workspace, ws.id)
        made = reply.status == "done" and done(ws.blueprint)
        now = {st["story"]: st["result"] for st in stories.run(ws, scenario_key)}
        broke = sorted(name for name in worked if now.get(name) != "pass")
        why = [] if made else [f"it didn't make the change ({reply.status}{': ' + (reply.error or '')[:120] if reply.error else ''})"]
        why += [f"it broke the story “{name}”" for name in broke]
        out.append({"how": "ai", "change": change, "asked": say, "ok": made and not broke, "why": "; ".join(why), "said": [reply.text or ""],
                    "seconds": round(time.time() - started), "questions": (reply.text or "").count("?")})
    return out


def _engine(ws):
    from app.engine.records import Engine
    return Engine(ws)


def _table(bp, key):
    return next((t for t in bp.get("tables", []) if t["key"] == key), {"fields": []})


def _tells(bp, aim):
    """A rule on the list that acts when one reaches the stage and tells someone (in the app or by email)."""
    from app.blueprint import schema
    t = _table(bp, aim["table"])
    for x in bp.get("automations", []):
        if x["trigger"].get("table") == aim["table"] and appmap._at_stage(x, t) == aim["told"] \
                and any(s.get("type") in ("notify", "send_email", "task") for s in schema.steps_of(x["actions"])):
            return True
    return False


def _placed(bp, aim):
    t = _table(bp, aim["table"])
    f = next((x for x in t["fields"] if x["key"] == aim["field"]), None)
    names = [o.lower() for o in (f or {}).get("options") or []]
    return aim["held"].lower() in names and names.index(aim["held"].lower()) == names.index(aim["after"].lower()) + 1


# --- what they're told, looked at as a beginner would -------------------------------------------------------------------

def findings(changes, theirs):
    """Beginner's-eyes findings on what each change said: code, words they won't know, a long or questioning reply,
    and a change that failed (a blocker: they're stuck)."""
    out = []
    for c in changes:
        where = f"{'the App map' if c['how'] == 'map' else 'the builder'}: {c['change']}"
        if c["ok"] is False:
            out.append(ux._finding("blocker", "change didn't work", "the owner", where, c["why"][:180] or "it didn't happen"))
        for text in c.get("said") or []:
            out += ux.words_found(str(text), theirs, "the owner", where)
        if c["how"] == "ai" and c.get("questions", 0) > 2:
            out.append(ux._finding("confusing", "too many questions", "the owner", where, f"asked {c['questions']} questions for a small change"))
    return ux._unique(out)
