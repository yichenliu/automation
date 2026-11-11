"""Automations: rules on records. "When a Project's Status changes to Done, create its Invoice, produce the PDF, email it
to the client and tell me."

A change to a record (`on_change`), a schedule (`due`), a button (`press`), an email, a new OneDrive file or a call to
the automation's webhook from another system (`start_from`) starts a Run. Steps run in order; each can be limited by its own conditions ("only if"). Runs without AI
steps happen at once, so people see the result as they save; runs with AI steps go to the background worker and, if the
AI can't be reached, wait and carry on from the step they stopped at. A step that asks a person to approve pauses the
run until they decide. Every run keeps its steps, so what an automation did (or why it didn't) can always be seen.
"""
import logging
import base64
import contextvars
import hashlib
import random
import hmac
import json
import re
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from sqlalchemy import event, inspect as sa_inspect, or_
from sqlalchemy.exc import SQLAlchemyError

from .. import ai, leases, outbox
from ..blueprint import expr, schema
from ..extensions import db
from ..models import Approval, File, Membership, Notification, Record, Run, ScheduleRun, Workspace, utcnow
from .records import Engine, FieldErrors, RecordContext, RecordProxy, automations_words
from ..core.settings import settings

_log = logging.getLogger(__name__)

MAX_DEPTH = 5  # automations started by automations, at most this deep (stops loops)
AI_RETRY_SECONDS = 30
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
SLOW = {"ai_fill", "ai_extract", "ai_create_record"}  # steps done by the background worker
# A try-out's answers for its AI steps, answer(record, field key) -> value or None: the AI isn't asked in a copy, so an AI
# step answers as the example expects (a price check "Pass") and what comes after it is tried. Unset everywhere else.
stand_in = contextvars.ContextVar("stand_in", default=None)


AI_HOLD = timedelta(minutes=15)  # how long an unchecked AI step (a brief, a summary) holds up the steps after it when the AI is away
SPOT_CHECK = 0.1  # of the AI's sure answers in a "check when unsure" step, the share a person still checks (keeps its record honest)
CHECK_EVERY = timedelta(minutes=15)  # how often a run waiting for a record to meet conditions looks again (saves look at once)


class Paused(Exception):
    """A step is waiting for a person (an approval or a task); `until` is when to look again (a reminder is due)."""

    def __init__(self, message, until=None):
        super().__init__(message)
        self.until = until


class _Sleep(Exception):
    """A step is waiting for a time, or for the record to meet conditions: the run sleeps until `until`."""

    def __init__(self, until):
        super().__init__(str(until))
        self.until = until


class _Stop(Exception):
    """The run ends here: "rejected" (after its "if rejected" steps) or "failed"."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


def _by(automation):
    return f"the rule “{automation['name']}”"


# --- which rules a change starts: an index of the design's rules -----------------------------------------------------
# Every save asks which rules watch its list. Instead of reading every rule of the design each time, the rules are
# indexed by (list, trigger) once for each design version of each workspace, and the index is kept (the most recently
# used RULES_KEPT of them) for as long as the design stays that version.
RULES_KEPT = 256
_rule_index = OrderedDict()  # key -> (the rules it was made from, or None for a saved version; {(table, trigger): ((n, key), …)})
_rule_lock = threading.Lock()
CREATED = ("record_created", "record_created_or_updated")
UPDATED = ("record_updated", "record_created_or_updated")


def _rules_key(engine, found):
    """A saved design is known by its database, workspace and version (each change of design makes a new version). Any
    other (a design being looked at, one not saved yet, a simulation on SQLite) by the very list of its rules."""
    ws = engine.ws
    if isinstance(ws, Workspace) and db.engine.dialect.name == "postgresql":
        state = sa_inspect(ws)
        if state.persistent and not state.attrs.blueprint.history.has_changes() and not state.attrs.version.history.has_changes():
            return ("saved", db.engine.url.database, ws.id, ws.version, ws.created_at)
    return ("design", id(found))


def _index_of(found):
    index = {}
    for n, a in enumerate(found):
        trig = a.get("trigger") or {}
        index.setdefault((trig.get("table"), trig.get("type")), []).append((n, a.get("key")))
    return {k: tuple(v) for k, v in index.items()}


def rules(engine, table, types):
    """The design's rules on `table` started by any of `types` of trigger, in the design's order (on or off, and before
    their conditions are looked at): found in the index, which is made the first time it's needed."""
    found = engine.bp.get("automations") or []
    key = _rules_key(engine, found)
    with _rule_lock:
        kept = _rule_index.get(key)
        if kept is not None and key[0] == "design" and kept[0] is not found:
            kept = None
        if kept is not None:
            _rule_index.move_to_end(key)
    if kept is not None:
        picked = sorted(x for t in types for x in kept[1].get((table, t), ()))
        out = [found[n] for n, k in picked if n < len(found) and found[n].get("key") == k]
        if len(out) == len(picked) and all((a.get("trigger") or {}).get("table") == table for a in out):
            return out
    index = _index_of(found)  # not made yet, or this design isn't the one it was made from: made from this one
    with _rule_lock:
        _rule_index[key] = (found if key[0] == "design" else None, index)  # (holding the list keeps its id its own)
        _rule_index.move_to_end(key)
        while len(_rule_index) > RULES_KEPT:
            _rule_index.popitem(last=False)
    return [found[n] for n, _ in sorted(x for t in types for x in index.get((table, t), ()))]


@event.listens_for(Workspace.blueprint, "set")
def _forget_rules(target, value, old, initiator):
    """A design set without a new version (a test, a repair by hand): its index is made again when it's next needed."""
    identity = sa_inspect(target).identity
    if not identity:
        return
    with _rule_lock:
        for key in [k for k in _rule_index if k[0] == "saved" and k[2] == identity[0]]:
            del _rule_index[key]


def on_change(engine, record, old, depth=0):
    """A record was created (old is None) or changed: start every automation it triggers, and have the runs waiting for
    this record to meet conditions look again."""
    if old is not None:
        for waiting in Run.query.filter_by(workspace_id=engine.ws.id, record_id=record.id, status="waiting").all():
            waiting.next_try = utcnow()
    if depth >= MAX_DEPTH:
        return []
    created = old is None
    # Which rules this change starts is decided before any of them runs: one that runs at once can change the record (a
    # check that approves it), and that's a change of its own, starting its own rules. Asked again afterwards, the record
    # would already look approved, and the rule for "changed to Approved" would run twice.
    due = [a for a in rules(engine, record.table, CREATED if created else UPDATED)
           if a.get("enabled", True) and engine.matches(record, a["conditions"], old=old)]
    t = engine.tables[record.table]
    what = f"{t['singular']} “{engine.title(record)}” {'was added' if created else 'was changed'}"
    return [start(engine, a, record, what, depth) for a in due]


def on_delete(engine, record, depth=0):
    """A record was deleted: start the automations that watch for that (tidy up, tell someone, update a total elsewhere)."""
    started = []
    for a in rules(engine, record.table, ("record_deleted",)):
        if a.get("enabled", True) and engine.matches(record, a["conditions"]):
            t = engine.tables[record.table]
            started.append(start(engine, a, record, f"{t['singular']} “{engine.title(record)}” was deleted", depth, context={"deleted": True}))
    return started


def _run_other(engine, automation, action, record, run, ctx):
    """Start another automation (a sub-flow several automations share) for this record, the one made earlier in the run,
    or a linked one. Its trigger and conditions are skipped: it's asked to run."""
    other = next((a for a in engine.bp.get("automations", []) if a["key"] == action["automation"]), None)
    if other is None:
        raise ValueError(f"the rule “{action['automation']}” no longer exists")
    if not other.get("enabled", True):
        return {"result": f"“{other['name']}” is turned off, so it didn't run"}
    if (run.depth or 0) + 1 >= MAX_DEPTH:
        raise ValueError("automations started each other too many times in a row")
    target = record
    if action.get("target") == "created":
        target = ctx.names["created"].record if ctx.names.get("created") else None
    elif action.get("target") and record is not None:
        target = _linked(engine, record, action["target"])
    if action.get("target") and target is None:
        return {"result": f"“{other['name']}” didn't run: there was no record to run it for"}
    sub = start(engine, other, target, f"Started by “{automation['name']}”", depth=(run.depth or 0) + 1)
    state = {"done": "done", "waiting_approval": "is waiting for a decision", "waiting": "is waiting", "failed": "failed"}.get(sub.status, "is running")
    return {"result": f"Started “{other['name']}”, which {state}" if state != "done" else f"Ran “{other['name']}”",
            **({"link": _link(target.table, target.id)} if target is not None else {})}


def _at_once(automation):
    """Whether a rule's run is carried out in the save that starts it: all but rules with AI steps (the worker does those)."""
    return stand_in.get() is not None or not any(act["type"] in SLOW for act in schema.steps_of(automation["actions"]))


def start(engine, automation, record, trigger, depth=0, context=None):
    now = _at_once(automation)
    # a run carried out here is held by this save from the moment it's made, so no worker picks it up beside it
    held = {"claimed_by": leases.me(), "lease_until": utcnow() + timedelta(seconds=leases.HOLD_SECONDS)} if now else {}
    run = Run(workspace_id=engine.ws.id, automation=automation["key"], name=automation["name"], trigger=trigger[:300],
              record_id=record.id if record is not None else None, table=record.table if record is not None else None,
              depth=depth, steps=[], status="queued", context=context or {}, **held)
    db.session.add(run)
    db.session.commit()
    engine.started.append(run)  # so the screen can say what the save set off
    if now:
        execute(engine.system(), run)
    return run


def hook_token(workspace_id, key):
    """The secret part of an automation's webhook address: only someone given the address can start it."""
    secret = str(settings()["SECRET_KEY"]).encode()
    return hmac.new(secret, f"hook:{workspace_id}:{key}".encode(), hashlib.sha256).hexdigest()[:32]


def hook_url(workspace_id, key):
    return f"{settings()['BASE_URL']}/hooks/{workspace_id}/{key}/{hook_token(workspace_id, key)}"


def press(engine, automation, record, by):
    """Someone clicked an automation's button on a record. While its conditions don't hold it isn't pressed: it's
    refused (FieldErrors), saying what it waits for, as the record's page says beside it."""
    held = button_held(engine, automation, record)
    if held:
        raise FieldErrors({"_": held})
    return start(engine, automation, record, f"{by} clicked “{automation['trigger'].get('label') or automation['name']}”")


def button_held(engine, automation, record):
    """What a button waits for before it can be pressed on this record (“You can press “Send specs to supplier” once
    the design's Approval status is Approved”), or None when it can be."""
    plain = [c for c in automation.get("conditions") or [] if not (isinstance(c, dict) and c.get("op") in ("changed", "changed_to"))]
    if record is None or not plain or engine.matches(record, plain):
        return None
    label = automation["trigger"].get("label") or automation["name"]
    return f"You can press “{label}” once {automations_words(engine, plain, record.table)}"


def start_from(engine, automation, kind, item, trigger, record=None):
    """An email arrived, a file was added or a webhook was called (kind "email", "file" or "payload"); `item` is what
    steps see as {{email}}, {{file}} or {{payload}}. record: the one a webhook call is about (hook_record)."""
    return start(engine, automation, record, trigger, context={kind: item, "files": item.get("file_ids", [])})


def hook_record(engine, automation, payload):
    """The record a webhook call is about, for a webhook rule that names its list: the one whose id the call sends
    ("id"), or whose number or name it sends ("record", "number" or "reference": INV-0042). None when the rule names no
    list; raises LookupError when it does and the call names none of its records."""
    table = automation["trigger"].get("table")
    if not table:
        return None
    sent = payload if isinstance(payload, dict) else {}
    rid = sent.get("id")
    if isinstance(rid, int) or str(rid or "").isdigit():
        found = engine.get(int(rid), table)
        if found is not None:
            return found
    for key in ("record", "number", "reference"):
        if sent.get(key):
            found = engine.by_code(str(sent[key]))
            if found is not None and found.table == table:
                return found
    raise LookupError(f"the call names no record of {engine.tables[table]['label'] if table in engine.tables else table}: send its \"id\" or its "
                      "\"number\"")


class _Item:
    """{{email.subject}}, {{file.name}}: a dict that expressions can read with dots."""

    def __init__(self, data):
        self.data = data or {}

    def field(self, name):
        value = self.data.get(name) if isinstance(self.data, dict) else None
        return _Item(value) if isinstance(value, dict) else value

    def __str__(self):
        if not isinstance(self.data, dict):
            return str(self.data)
        return str(self.data.get("subject") or self.data.get("name") or json.dumps(self.data, ensure_ascii=False, default=str)[:500])


def _mail_ready(engine):
    """Whether the business's mailbox is connected (Microsoft 365), so emails can go out by themselves. A copy being tried
    out acts as if it were (it reaches nothing outside): it's the app's rules being tried, not the mailbox."""
    if settings().get("TRYING_OUT"):
        return True
    from ..connectors import microsoft
    try:
        return bool(microsoft.status(engine.ws).get("connected"))
    except Exception:  # noqa: BLE001 - not set up at all
        return False


def _context(engine, run, record, item=None):
    names = {"record": RecordProxy(engine, record) if record is not None else None, "created": None,
             "item": RecordProxy(engine, item) if item is not None else None}
    created = (run.context or {}).get("created") or []
    if created:
        last = engine.get(created[-1]["id"])
        names["created"] = RecordProxy(engine, last) if last else None
    for kind in ("email", "file", "payload", "flow"):  # what started it: {{email.subject}}; a flow's answers: {{flow.count.reason}}
        names[kind] = _Item((run.context or {}).get(kind)) if (run.context or {}).get(kind) is not None else None
    names["join_link"] = engine.join_link()  # the portal's sign-up page, for an invitation to join
    names["approver"] = (run.context or {}).get("approver")  # who said yes to the run's last approval (its "Approved by")
    names["reason"] = (run.context or {}).get("reason")  # what they wrote when they answered it (why they said no)
    own = {f["key"] for f in (engine.tables.get(record.table) or {}).get("fields", [])} if record is not None else set()
    names = {k: v for k, v in names.items() if v is not None or k not in own}  # nothing started it by email: {{email}} is the record's
    return RecordContext(engine, record, names)


def _render(value, ctx):
    if isinstance(value, dict):  # a record named by what it's for ({"product": "{{item.product}}", "warehouse": ...})
        return {k: _render(v, ctx) for k, v in value.items()}
    if not isinstance(value, str) or "{{" not in value:
        return value
    out = expr.render(value, ctx)
    return re.sub(r"([$€£¥])\s*\1", r"\1", out) if isinstance(out, str) else out  # "${{record.total}}": the amount brings its own $


def _parent(engine, record, source, run):
    """The record a step's related records hang off: the triggering one, the one its "of" link reaches (a work order's
    product), or the latest one made in this run of the table they link to (the new invoice)."""
    if not source.get("of"):
        return record.id
    if source["of"] == "created":
        via = engine.field(source["table"], source["via"])
        made = [c["id"] for c in (run.context or {}).get("created") or [] if via and c["table"] == via["table"]]
        return made[-1] if made else None
    linked = _reached(engine, record, source["of"])
    return int(linked) if str(linked).isdigit() else linked


def _reached(engine, record, path):
    """The value at the end of a path through a record's links ("order.product"), or of one of its fields (a lookup shows
    the linked record's own)."""
    head, _, rest = path.partition(".")
    value = engine.value(record, head)
    if not rest:
        return value
    value = value[0] if isinstance(value, list) and value else value
    nxt = engine.get(int(value)) if str(value).isdigit() else None
    return _reached(engine, nxt, rest) if nxt is not None else None


def _linked(engine, record, path):
    """The record a step's "target" names: through one link field, or a path of them (a payment's "invoice.couple")."""
    value = _reached(engine, record, path) if record is not None else None
    value = value[0] if isinstance(value, list) and value else value
    return engine.get(int(value)) if str(value).isdigit() else None


def _target_words(engine, table, path):
    """A step's "target" in words: “Invoice”, or “Invoice’s Couple” for a path."""
    links = schema.link_path(engine.tables.get(table), path, engine.tables)
    return "’s ".join(f["label"] for f in links) if links else path


def _lines(engine, source, parent):
    """The records a "for each" step goes through. With "nested", a line naming a sub-assembly that has lines of its own
    stands for them, their quantities multiplied by its: a cabinet's drawer unit brings in the drawer unit's parts."""
    nested = schema.nested_link(engine.tables[source["table"]], source)
    if not nested:
        return engine.children(source["table"], source["via"], parent)
    quantity = next((k for k in ("quantity", "qty", "qty_per", "quantity_per") if engine.field(source["table"], k)), None)
    out = []

    def walk(at, times, seen):
        for row in engine.children(source["table"], source["via"], at):
            inner = (row.data or {}).get(nested["key"])
            inner = int(inner) if str(inner).isdigit() else None
            many = expr.to_number((row.data or {}).get(quantity)) if quantity else None
            many = 1 if many is None else many
            if inner and inner not in seen and len(seen) < 8 and engine.children(source["table"], source["via"], inner):
                walk(inner, times * many, seen | {inner})
            elif times != 1 and quantity:  # a part of a sub-assembly: as many as the whole needs (not saved)
                out.append(Record(workspace_id=row.workspace_id, table=row.table, data={**row.data, quantity: many * times}))
            else:
                out.append(row)
    walk(parent, 1, {parent})
    return out


def _render_values(values, ctx, engine=None, table=None):
    """A step's values worked out. One for a date that comes out a plain number was worked out from a date left empty
    (sent_on + 3 before it's sent: empty and 3 make 3): not a date yet, so that field is left as it is, rather than the
    rule stopping on “3 isn't a date”."""
    out = {k: _render(v, ctx) for k, v in values.items()}
    if engine is None or not table:
        return out
    for k, v in list(out.items()):
        f = engine.field(table, k)
        if f is not None and engine.kind(f) in ("date", "datetime") and not isinstance(v, bool) \
                and (isinstance(v, (int, float)) or isinstance(v, str) and re.fullmatch(r"\s*-?\d+(?:\.\d+)?\s*", v)):
            out.pop(k)
    return out


def find(engine, key):
    """An automation by its key; "flow:<key>" is a flow's "then" steps, run when someone finishes it."""
    if str(key).startswith("flow:"):
        from .flows import as_automation, of
        flow = of(engine, key[5:])
        return as_automation(flow) if flow and flow.get("then") else None
    return next((a for a in engine.bp.get("automations", []) if a["key"] == key), None)


def execute(engine, run):
    """Do a run's steps in order, carrying on after the last one it finished. A step's place in the run is its "at": "2",
    or "3.1.0" for the first step of a branch's second path, so a run waiting inside a branch picks up where it was.

    The run is held while this goes on (leases.py): one that a worker or another save is carrying out right now is left
    to them, and returned as it is."""
    if not (leases.mine(run) or leases.hold(run)):
        return run
    try:
        return _execute(engine, run)
    finally:
        _let_go(run)


def _let_go(run):
    try:
        leases.let_go(run)
    except SQLAlchemyError:  # the run's own failure left the session to be rolled back first
        db.session.rollback()
        try:
            leases.let_go(run)
        except SQLAlchemyError:
            _log.exception("Couldn't let go of run %s (it's free again once its claim runs out)", run.id)
            db.session.rollback()


def _execute(engine, run):
    automation = find(engine, run.automation)
    if automation is None:
        run.status, run.error, run.finished_at = "skipped", "This automation no longer exists.", utcnow()
        db.session.commit()
        return run
    record = engine.get(run.record_id) if run.record_id else None
    if run.record_id and record is None and (run.context or {}).get("deleted"):
        from ..models import Record
        record = db.session.get(Record, run.record_id)  # an automation about a deletion still reads what was deleted
    if run.record_id and record is None:
        run.status, run.error, run.finished_at = "skipped", "The record it was started for was deleted.", utcnow()
        db.session.commit()
        return run
    run.status, run.next_try = "running", None
    db.session.commit()
    log = [dict(step, at=step.get("at", str(i))) for i, step in enumerate(run.steps or [])]  # older runs: a step per place
    try:
        _steps(engine, automation, automation["actions"], "", {"record": record}, run, log)
    except Paused as exc:
        run.steps, run.status, run.next_try = log, "waiting_approval", exc.until
    except _Sleep as exc:
        run.steps, run.status, run.next_try = log, "waiting", exc.until
    except ai.AIUnavailable as exc:
        run.steps, run.status, run.error = log, "waiting_ai", f"Waiting for the AI: {exc}"
        run.next_try = utcnow() + timedelta(seconds=AI_RETRY_SECONDS)
    except _Stop as exc:
        run.steps, run.status, run.error, run.finished_at, run.next_try = log, exc.status, exc.message, utcnow(), None
        if exc.status == "failed":  # someone should know a rule of theirs stopped half way, and nothing half made is left about
            undone = _undo_made(engine, run)
            if undone:
                run.error = f"{exc.message} (what it had made so far, {undone}, was put in the bin)"
            db.session.commit()
            notify(engine, f"The rule “{automation['name']}” stopped: {run.error}", "automations", "admin", kind="error")
    else:
        run.steps, run.status, run.error, run.finished_at, run.next_try = log, "done", None, utcnow(), None
    db.session.commit()
    return run


def _undo_made(engine, run):
    """The records a run made before it stopped half way (an invoice with none of its lines) go in the bin, where they can
    be brought back. Returns what they were, in words, or ""."""
    made = [c for c in (run.context or {}).get("created") or [] if isinstance(c, dict) and c.get("id")]
    said, system = [], engine.system()
    for c in reversed(made):
        record = system.get(c["id"])
        if record is None:
            continue
        try:
            system.delete(record, by=f"“{run.name}” (stopped half way)", automations=False)
        except FieldErrors:
            continue
        t = engine.tables.get(record.table) or {}
        if not t.get("embedded"):
            said.append(f"{t.get('singular', 'record').lower()} “{engine.title(record)}”")
    run.context = {**(run.context or {}), "created": []}
    return ", ".join(reversed(said)) or ("its lines" if made else "")


def _entry(log, at):
    return next((step for step in log if step.get("at") == at), None)


def _save(run, log):
    run.steps = [dict(step) for step in log]
    db.session.commit()


def _steps(engine, automation, actions, prefix, state, run, log):
    """Do these steps in order (an automation's, a branch path's or an approval's "if rejected"). Raises Paused or _Sleep
    when one waits, _Stop when the run ends early."""
    for i, action in enumerate(actions or []):
        at = f"{prefix}{i}"
        entry = _entry(log, at)
        if entry is not None and entry.get("done"):
            continue
        record = state["record"]
        if entry is None:
            entry = {"at": at, "action": describe_action(engine, automation, action), "done": False}
            log.append(entry)
        table = record.table if record is not None else None
        if not entry.get("started"):  # a step's conditions are looked at once, when the run reaches it
            if action.get("only_if") and (record is None or not engine.matches(record, action["only_if"])):
                entry.update(result="Skipped: " + automations_words(engine, action["only_if"], table) + " isn't true", done=True, skipped=True)
                _save(run, log)
                continue
            entry["started"] = True
        kind = action["type"]
        if kind == "wait":
            _wait(engine, action, record, entry, run, log)
        elif kind == "branch":
            if "path" not in entry:
                chosen = next((n for n, path in enumerate(action["paths"]) if record is not None and engine.matches(record, path["when"])), None)
                entry["path"] = chosen if chosen is not None else ("otherwise" if action.get("otherwise") else None)
                entry["result"] = (f"It's “{_path_name(engine, action, chosen, table)}”, so that's what was done" if chosen is not None
                                   else "None of the cases fitted, so it did what's set for anything else" if action.get("otherwise")
                                   else "None of the cases fitted, so nothing more was done")
                _save(run, log)
            path = entry["path"]
            steps = action["paths"][path]["steps"] if isinstance(path, int) else action.get("otherwise") if path == "otherwise" else []
            _steps(engine, automation, steps, f"{at}.{path if isinstance(path, int) else 'o'}.", state, run, log)
            entry["done"] = True
        elif kind == "send_email" and record is not None and not _mail_ready(engine) \
                and str(_render(action["to"], _context(engine, run, record)) or "").strip():
            # no mailbox connected: a person sends it themselves (the PDF is kept on the record), and the run carries on after
            # (with nobody to send it to, it's the step's own: not sent, said, and the rule carries on)
            to = str(_render(action["to"], _context(engine, run, record)) or "").strip()
            subject = str(_render(action["subject"], _context(engine, run, record)))
            for key in action.get("attach", []):
                _attachments(engine, record, key, run)  # made now, kept on the record, ready to download and attach
            outcome = _ask(engine, automation, {"type": "task", "to": "creator", "message":
                           f"Email isn't set up, so please send this yourself: “{subject}” to {to or 'them'}"
                           + (" (the PDF is on it, ready to download)" if action.get("attach") else "") + ", then press Done."}, record, run, at)
            entry.update(outcome, done=True)
            entry["result"] = f"Sent by hand (email isn't set up): “{subject}” to {to}"
        elif kind in ("request_approval", "task"):
            if not entry.get("rejected"):
                outcome = _ask(engine, automation, action, record, run, at)
                if outcome.get("rejected"):
                    entry.update(rejected=True, result=outcome["result"])
                    _save(run, log)
                else:
                    entry.update(outcome, done=True)
            if entry.get("rejected"):  # a no: its "if rejected" steps, then the run ends there
                _steps(engine, automation, action.get("if_rejected"), f"{at}.r.", state, run, log)
                entry["done"] = True
                _save(run, log)
                raise _Stop("rejected", entry["result"])
        elif kind in schema.AI_STEPS:
            _ai_step(engine, automation, action, record, run, entry, log)
        else:
            entry.update(_attempt(engine, automation, action, record, run, entry, log))
            entry["done"] = True
        _save(run, log)
        if record is not None:
            engine.reset()
            state["record"] = engine.get(record.id) or record


def _attempt(engine, automation, action, record, run, entry, log, do=None):
    """One step; a failure ends the run with what went wrong."""
    try:
        return (do or _do)(engine, automation, action, record, run)
    except ai.AIUnavailable:
        raise
    except FieldErrors as exc:
        message = "; ".join(exc.errors.values())
    except (expr.ExprError, ai.AIError, ValueError, KeyError, requests.RequestException) as exc:
        message = str(exc) or type(exc).__name__
    except Exception as exc:  # Microsoft 365 not connected, or a fault of ours: the run fails, never the save that started it
        from ..connectors.microsoft import MicrosoftProblem
        if isinstance(exc, MicrosoftProblem):
            message = str(exc)
        else:
            _log.exception("Automation %s failed at step %s", run.automation, entry["at"])
            if isinstance(exc, SQLAlchemyError):
                db.session.rollback()
            message = f"Something went wrong on our side ({type(exc).__name__}); it's been logged"
    entry["result"] = message
    raise _Stop("failed", message)


def _ai_step(engine, automation, action, record, run, entry, log):
    """An AI step. What the AI made of it counts straight away, or first goes to a person to check (every time, when the
    AI isn't sure, or now and then at random when it is), as the step says. Raises Paused while it's being checked."""
    if action.get("manual") or entry.get("fallback"):  # a person does the job instead (the AI is off, or couldn't), and the run waits
        task = manual_task(engine, automation, action, record, run)
        if entry.get("fallback"):
            task["message"] = f"The AI couldn't do this ({entry['fallback']}). {task['message']}"[:1000]
        entry.update(_ask(engine, automation, task, record, run, entry["at"]), done=True, ai="manual")
        return
    if entry.get("ai") != "checking":
        try:
            work = _attempt(engine, automation, action, record, run, entry, log,
                            do=lambda *args: _ai_work(*args, at=entry["at"]))
        except ai.AIUnavailable:
            # nobody checks it (a summary, a brief) and steps come after it (the approval it was to brief): it holds them up
            # for a while at most, then they go on without it. Checked work, or a rule's last step, waits for the AI.
            since = entry.setdefault("ai_waiting_since", utcnow().isoformat())
            last = str(entry.get("at")) == str(len(automation.get("actions") or []) - 1)
            held = utcnow() - datetime.fromisoformat(since)
            if action.get("review", "never") != "never" or last or (held < AI_HOLD and not settings().get("TRYING_OUT")):
                raise
            entry.update(result="The AI wasn't available, so this was left as it was and the rest went on", done=True, ai="skipped")
            return
        if "proposal" not in work:  # nothing the AI could do (no files to read, nothing it could tell)
            if work.get("fallback") is not None and action.get("review", "never") != "never" and ai_fields(action):
                entry.update(work, fallback=work["fallback"] or "it couldn't tell", ai="manual")  # a person whose work it is does it instead
                _save(run, log)
                return _ai_step(engine, automation, action, record, run, entry, log)
            entry.update({k: v for k, v in work.items() if k != "fallback"}, done=True)
            return
        proposal, mode = work["proposal"], action.get("review", "never")
        entry.update(why=proposal.get("why"), sure=proposal["sure"])
        if mode == "never" or mode == "when_unsure" and proposal["sure"] and (stand_in.get() is not None or random.random() >= SPOT_CHECK):
            entry.update(_attempt(engine, automation, action, record, run, entry, log, do=lambda *_: _ai_apply(engine, automation, proposal)),
                         done=True, ai="applied")
            return
        target = engine.get(proposal["record_id"])
        message = _check_words(engine, automation, action)
        for uid, role in dict.fromkeys(_assignees(engine, action["reviewer"], record)):
            row = Approval(workspace_id=engine.ws.id, run_id=run.id, record_id=proposal["record_id"], table=proposal["table"],
                           message=message[:1000], assignee_id=uid, role=role, kind="review", step=entry["at"],
                           fields=list(proposal["values"]), proposal=proposal, history=[])
            db.session.add(row)
            db.session.commit()
            _tell(engine, row, target, message + (" (it wasn't sure)" if not proposal["sure"] else ""))
        entry.update(ai="checking", result="Waiting for a person to check the AI's work" + (" (it wasn't sure)" if not proposal["sure"] else ""))
        _save(run, log)
    rows = [r for r in Approval.query.filter_by(run_id=run.id, step=entry["at"], kind="review").all() if r.status not in ("cancelled", "escalated")]
    done = next((r for r in rows if r.status == "done"), None)
    no = next((r for r in rows if r.status == "rejected"), None)
    if done is None and no is None:
        raise Paused(entry["result"])
    _cancel(r for r in rows if r.status == "pending")
    if done is not None:
        changed = (done.proposal or {}).get("changed") or []
        entry.update(done=True, ai="checked", result=f"{done.decided_by} checked the AI's work: "
                     + (f"corrected {', '.join(changed)}" if changed else "right as it was"), link=_link(done.table, done.record_id))
        return
    made = (no.proposal or {}).get("created") and engine.get(no.record_id)
    if made:  # what the AI made from it wasn't real (spam, a duplicate): it goes to the bin
        engine.delete(made, by=no.decided_by)
    entry.update(done=True, ai="checked", result=f"{no.decided_by} turned down the AI's work" + (f" (“{no.comment}”)" if no.comment else "")
                 + ("; what it made was deleted" if made else ""))
    _save(run, log)
    raise _Stop("rejected", entry["result"])


def ai_title(engine, automation, step):
    """An AI helper's job in a few words: Writes the Statement of work, Checks and sets Price check."""
    table = automation["trigger"].get("table")
    f = engine.field(table, step.get("field") or "") if table else None
    kind = step["type"]
    if kind == "ai_fill" and f:
        verb = {"long_text": "Writes", "rich_text": "Writes", "select": "Chooses", "multi_select": "Chooses", "link": "Matches"}.get(f["type"], "Fills in")
        return f"{verb} {f['label']}"
    if kind == "ai_check" and f:
        return f"Checks and sets {f['label']}"
    if kind == "ai_extract":
        t = engine.tables.get(table) or {"singular": "record"}
        return f"Reads attached files into the {schema.lc(t['singular'])}"
    if kind == "ai_create_record":
        t = engine.tables.get(step["table"]) or {"label": step["table"]}
        source = {"email_received": "emails", "file_added": "files", "webhook_received": "what's sent in"}.get(automation["trigger"]["type"], "what comes in")
        return f"Makes {schema.lc(t['label'])} from {source}"
    return describe_action(engine, automation, step)


def _check_words(engine, automation, action):
    """What a person is asked when they check an AI helper's work: Check the AI's Statement of work."""
    table = automation["trigger"].get("table")
    f = engine.field(table, action.get("field") or "") if table else None
    if action["type"] in ("ai_fill", "ai_check") and f:
        return f"Check the AI's {f['label']}"
    if action["type"] == "ai_extract":
        return "Check what the AI read from the files"
    t = engine.tables.get(action.get("table") or "") or {"singular": "record"}
    return f"Check the {schema.lc(t['singular'])} the AI made"


def ai_fields(action):
    """The fields an AI step fills (and a person fills in its place when it's switched off)."""
    if action["type"] == "ai_fill":
        return [action["field"]]
    if action["type"] == "ai_check":
        return [action["field"]] + ([action["notes_field"]] if action.get("notes_field") else [])
    return list(action.get("fields") or [])


def manual_task(engine, automation, action, record, run=None):
    """The task a person gets for an AI step that's switched off: the same job, in words, with the same fields."""
    table = automation["trigger"].get("table")
    label = lambda k: (engine.field(table, k) or {"label": k})["label"]  # noqa: E731
    kind, said = action["type"], (action.get("instructions") or "").strip()
    if kind == "ai_fill":
        message = f"Fill in {label(action['field'])}" + (f": {said}" if said else "")
    elif kind == "ai_check":
        message = f"Check it: {said}. Then set {' and '.join(label(k) for k in ai_fields(action))}"
    elif kind == "ai_extract":
        message = "Read the attached files and fill in the details" + (f": {said}" if said else "")
    else:
        t = engine.tables.get(action["table"]) or {"singular": "record", "label": "records"}
        email = ((run.context or {}).get("email") if run is not None else None) or {}
        what = f"the email “{email.get('subject', '')}” from {email.get('from', 'someone')}" if email else "what came in"
        message = f"Add a {schema.lc(t['singular'])} for {what}" + (f": {str(email.get('body', ''))[:500]}" if email.get("body") else "")
    return {"type": "task", "to": action.get("reviewer") or "editor", "message": message[:1000], "fields": ai_fields(action)}


def _ai_work(engine, automation, action, record, run, at=None, learned=None):
    """What the AI makes of an AI step: {"proposal": {"table", "record_id", "values", "sure", "why", "unsure",
    "created", "lines"}, "result"}, or just {"result"} when there's nothing for it to do. A record it creates is made now
    (so it can be seen and checked), its own automations waiting until it's checked when it will be. It's shown how
    people corrected its work at this step before, unless the step says not to learn."""
    from .. import intelligence
    kind = action["type"]
    answer = stand_in.get()
    if answer is not None and record is not None:  # a try-out: answered as the example expects, so what follows is tried
        keys = [action["field"]] if kind in ("ai_fill", "ai_check") else list(action.get("fields") or []) if kind == "ai_extract" else []
        values = {k: v for k in keys for v in [answer(record, k)] if v not in (None, "", [])
                  and (kind != "ai_extract" or (record.data or {}).get(k) in (None, "", []))}
        if not values:
            return {"result": "Not tried: the AI isn't asked while an app is tried out"}
        sure, said = True, answer(record, action["confidence"]) if action.get("confidence") and kind in ("ai_fill", "ai_check") else None
        if said not in (None, ""):  # as sure as the example says (92%: sure above 80; 60%: a person checks it)
            judged = _sureness(action, {"confidence": expr.to_number(str(said).rstrip("% ")), "sure": True}, values, _percent(engine, record, action))
            values, sure = judged["values"], judged["sure"]
        return {"proposal": {"table": record.table, "record_id": record.id, "values": values, "sure": sure,
                             "unsure": [] if sure else [engine.field(record.table, action["field"])["label"]],
                             "why": "As the example expects (the AI isn't asked while an app is tried out)"}}
    if learned is None:
        learned = corrections(engine, automation["key"], at) if at is not None and action.get("learn", True) else []
    action = {**action, "_examples": learned}
    if kind == "ai_check":
        answer = intelligence.check_record(engine, record, action)
        said = answer["values"].get(action["field"])
        if said is None:
            return {"result": "The AI couldn't come to a verdict" + (f": {answer['why']}" if answer["why"] else ""),
                    "link": _link(record.table, record.id), "fallback": answer["why"]}
        f = engine.field(record.table, action["field"])
        found = answer["findings"]
        answer = {**answer, **_sureness(action, answer, answer["values"], _percent(engine, record, action))}
        return {"proposal": {"table": record.table, "record_id": record.id, "values": answer["values"], "sure": answer["sure"],
                             "why": (f"Found {len(found)} problem{'s' if len(found) != 1 else ''}: " + "; ".join(found[:3]) if found
                                     else "No problems found") + (f". {answer['why']}" if answer["why"] else ""),
                             "unsure": [] if answer["sure"] else [f["label"]]}}
    if kind == "ai_fill":
        f = engine.field(record.table, action["field"])
        answer = intelligence.propose_field(engine, record, f, action["instructions"], use_knowledge=action.get("use_knowledge"), action=action)
        if answer["value"] in (None, "", []):
            return {"result": f"The AI couldn't tell {f['label']}" + (f": {answer['why']}" if answer["why"] else " from what the record says"),
                    "link": _link(record.table, record.id), "fallback": answer["why"]}
        answer = {**answer, **_sureness(action, answer, {f["key"]: answer["value"]}, _percent(engine, record, action))}
        return {"proposal": {"table": record.table, "record_id": record.id, "values": answer["values"], "sure": answer["sure"],
                             "why": answer["why"], "unsure": [] if answer["sure"] else [f["label"]]}}
    if kind == "ai_extract":
        files = _source_files(run, record, action["from"])
        if not files:
            return {"result": "Nothing to read: there are no files"}
        result = intelligence.extract(engine, record.table, intelligence.read_files(files), action.get("instructions"), action.get("fields"),
                                      use_knowledge=action.get("use_knowledge"), examples=learned)
        empty = {k: v for k, v in result["values"].items() if (record.data or {}).get(k) in (None, "", [])}
        lines = {}  # line items it read (a quote's lines), for a record that hasn't any yet
        for child, link in intelligence._children(engine, engine.tables[record.table]):
            rows = result["lines"].get(child["key"]) or []
            if rows and not engine.children(child["key"], link["key"], record.id):
                lines[child["key"]] = rows
        if not empty and not lines:
            return {"result": f"The AI read {len(files)} file{'s' if len(files) != 1 else ''} and found nothing new", "link": _link(record.table, record.id)}
        labels = {engine.field(record.table, k)["label"] for k in empty if engine.field(record.table, k)}
        unsure = [u for u in result["unsure"] if u in labels]
        count = sum(len(rows) for rows in lines.values())
        return {"proposal": {"table": record.table, "record_id": record.id, "values": empty, "sure": not unsure, "unsure": unsure,
                             "lines": lines, "why": f"Read from {', '.join(f.name for f in files[:3])}"
                             + (f", with {count} line{'s' if count != 1 else ''}" if count else "")}}
    from ..files.readers import Content
    email = (run.context or {}).get("email")
    payload = (run.context or {}).get("payload")
    source = "email" if email else "webhook" if payload is not None else "file"
    files = _source_files(run, record, "email" if email else "file")
    contents = intelligence.read_files(files)
    if email:
        contents.insert(0, Content(kind="email", name=email.get("subject") or "email", text=email.get("body", ""),
                                   meta={k: email.get(k) for k in ("from", "to", "subject", "received")}))
    if payload is not None:
        contents.insert(0, Content(kind="text", name="data sent to the webhook", text=json.dumps(payload, ensure_ascii=False, indent=1, default=str)[:40000]))
    result = intelligence.extract(engine, action["table"], contents, action.get("instructions"), use_knowledge=action.get("use_knowledge"),
                                  examples=learned)
    checked = action.get("review", "never") == "always" or action.get("review") == "when_unsure" and result["unsure"]
    created = intelligence.save_extracted(engine, action["table"], result, by=_by(automation), files=files,
                                          attach_to=action.get("attach_to"), automations=not checked)
    _remember(run, created)
    t = engine.tables[action["table"]]
    return {"proposal": {"table": action["table"], "record_id": created.id, "values": {k: v for k, v in result["values"].items()
                                                                                      if engine.field(action["table"], k)},
                         "sure": not result["unsure"], "unsure": result["unsure"], "why": f"Made from the {source}", "created": True,
                         "said": f"The AI created {t['singular']} “{engine.title(created)}” from the {source}",
                         "automations": not checked}}


def _ai_apply(engine, automation, proposal):
    """An AI step's work, counted without a person checking it."""
    if proposal.get("created"):
        if not proposal.get("automations"):  # a sure answer spot-checked away from: its own automations start now
            on_change(engine, engine.get(proposal["record_id"]), None)
        note = f"; check {', '.join(proposal['unsure'])}" if proposal["unsure"] else ""
        return {"result": proposal["said"] + note, "link": _link(proposal["table"], proposal["record_id"])}
    record = engine.get(proposal["record_id"])
    diff = engine.update(record, proposal["values"], by=_by(automation), ai_filled=True) if proposal["values"] else {}
    made = _add_lines(engine, record, proposal.get("lines"), _by(automation))
    t = engine.tables[record.table]
    if len(proposal["values"]) == 1:
        f = engine.field(t["key"], next(iter(proposal["values"])))
        shown = engine.display(f, engine.value(engine.get(record.id), f["key"]))
        return {"result": (f"The AI set {f['label']} to “{shown[:300]}”" if diff else f"The AI agreed {f['label']} is “{shown[:300]}”")
                + (f" ({proposal['why']})" if proposal.get("why") else ""), "link": _link(record.table, record.id)}
    filled = ", ".join(engine.field(t["key"], k)["label"] for k in proposal["values"] if engine.field(t["key"], k))
    note = f" (check: {', '.join(proposal['unsure'])})" if proposal["unsure"] else ""
    lines = f"{' and ' if filled else ''}added {made} line{'s' if made != 1 else ''}" if made else ""
    return {"result": f"The AI {'filled ' + filled if filled else ''}{lines}{note}", "link": _link(record.table, record.id)}


def _add_lines(engine, record, lines, by):
    """Line items the AI read (a quote's lines) added to a record that had none; how many."""
    from .. import intelligence
    made = 0
    for child, link in intelligence._children(engine, engine.tables[record.table]):
        if not (lines or {}).get(child["key"]) or engine.children(child["key"], link["key"], record.id):
            continue
        with engine.atomic():
            for row in lines[child["key"]]:
                engine.create(child["key"], {**row, link["key"]: record.id}, by=by, automations=False, ai=False)
                made += 1
    return made


LEARN_FROM = 3  # people's latest corrections of an AI step's work it's shown next time


def corrections(engine, automation_key, at, n=LEARN_FROM):
    """How people corrected an AI step's work lately, newest first: [{"ai": {field: its value}, "person": {field:
    theirs}}], just the fields they changed."""
    rows = (Approval.query.join(Run, Approval.run_id == Run.id)
            .filter(Run.workspace_id == engine.ws.id, Run.automation == automation_key, Approval.kind == "review", Approval.step == at,
                    Approval.status == "done").order_by(Approval.id.desc()).limit(40).all())
    out = []
    for r in rows:
        p = r.proposal or {}
        if p.get("outcome") != "corrected" or not p.get("corrected"):
            continue
        label = lambda k: (engine.field(p.get("table"), k) or {"label": k})["label"]  # noqa: E731
        changed = [k for k in p["corrected"] if label(k) in (p.get("changed") or [])]
        out.append({"ai": {label(k): (p.get("values") or {}).get(k) for k in changed}, "person": {label(k): p["corrected"][k] for k in changed}})
        if len(out) >= n:
            break
    return out


def complete_review(engine, review, by, values=None, comment=None):
    """A person checked the AI's work: what they confirm (corrected or as it was; no values is as it was) is saved as theirs,
    the check is kept for the step's track record, and the run carries on."""
    proposal = dict(review.proposal or {})
    record = engine.get(review.record_id)
    if record is None:
        raise FieldErrors({"_": "The record the AI worked on was deleted."})
    given = (proposal.get("values") or {}) if values is None else values  # nothing said: kept as the AI had it
    wanted = {k: given.get(k) for k in review.fields or []}
    changed = []
    for k, v in wanted.items():
        f = engine.field(record.table, k)
        if f is None:
            continue
        try:
            mine, its = engine.coerce(f, v), engine.coerce(f, (proposal.get("values") or {}).get(k))
        except ValueError:
            mine, its = v, None
        if str(mine if mine is not None else "").strip().lower() != str(its if its is not None else "").strip().lower():
            changed.append(f["label"])
    engine.update(record, wanted, by=by, automations=not proposal.get("created"))
    _add_lines(engine, engine.get(record.id), proposal.get("lines"), by)
    review.status, review.decided_by, review.decided_at = "done", by, utcnow()
    review.comment = (comment or "").strip()[:1000] or None
    review.proposal = {**proposal, "outcome": "corrected" if changed else "right", "changed": changed,
                       "corrected": {k: v for k, v in wanted.items()} if changed else None}
    db.session.commit()
    if proposal.get("created") and not proposal.get("automations"):  # checked: the new record's own automations start
        on_change(engine.system(), engine.get(record.id), None)
    run = db.session.get(Run, review.run_id)
    if run.status != "waiting_approval":
        return run
    return _carry_on(engine.system(), run)


def track_record(engine, automation_key, at):
    """How the AI has done at one step, from the times people checked it: {"checked", "right", "corrected", "wrong",
    "unchecked", "share", "lower"}, "lower" being how good it surely is (the one-sided 95% Wilson lower bound)."""
    rows = (Approval.query.join(Run, Approval.run_id == Run.id)
            .filter(Run.workspace_id == engine.ws.id, Run.automation == automation_key, Approval.kind == "review", Approval.step == at,
                    Approval.status.in_(["done", "rejected"])).all())
    right = sum(1 for r in rows if r.status == "done" and (r.proposal or {}).get("outcome") == "right")
    corrected = sum(1 for r in rows if r.status == "done" and (r.proposal or {}).get("outcome") == "corrected")
    wrong = sum(1 for r in rows if r.status == "rejected")
    runs = Run.query.filter_by(workspace_id=engine.ws.id, automation=automation_key).order_by(Run.id.desc()).limit(1000).all()
    unchecked = sum(1 for r in runs for step in r.steps or [] if step.get("at") == at and step.get("ai") == "applied")
    checked = right + corrected + wrong
    return {"checked": checked, "right": right, "corrected": corrected, "wrong": wrong, "unchecked": unchecked,
            "share": right / checked if checked else None, "lower": _wilson_lower(right, checked)}


def _wilson_lower(right, n, z=1.645):
    if not n:
        return 0.0
    p = right / n
    return max(0.0, (p + z * z / (2 * n) - z * ((p * (1 - p) + z * z / (4 * n)) / n) ** 0.5) / (1 + z * z / n))


REVIEW_WORDS = {"always": "a person checks every one", "when_unsure": "a person checks it when the AI isn't sure (and 1 in 10 of the rest)",
                "never": "nobody checks it: it runs on its own"}


def with_review(automation, at, mode):
    """A copy of an automation with one AI step's checking changed (always, when_unsure or never), or None."""
    copy = json.loads(json.dumps(automation))
    step = schema.step_at(copy["actions"], at)
    if step is None or step["type"] not in schema.AI_STEPS or mode not in schema.REVIEW_MODES:
        return None
    if mode == "never":
        step.pop("review", None)
        step.pop("reviewer", None)
    else:
        step["review"] = mode
        step.setdefault("reviewer", "editor")
    return copy


def ai_steps(automation):
    """Each AI step of an automation with its place: [(at, step)]."""
    out = []

    def walk(steps, prefix):
        for i, act in enumerate(steps or []):
            at = f"{prefix}{i}"
            if act["type"] in schema.AI_STEPS:
                out.append((at, act))
            for n, path in enumerate(act.get("paths") or []):
                walk(path["steps"], f"{at}.{n}.")
            walk(act.get("otherwise"), f"{at}.o.")
            walk(act.get("if_rejected"), f"{at}.r.")
    walk(automation.get("actions"), "")
    return out


def _wait(engine, action, record, entry, run, log):
    """A wait step: done once its time comes (or, with "until_met", once the record meets the conditions, or the most
    it waits is up); until then the run sleeps."""
    now = utcnow()
    if action.get("until_met"):
        if "until" not in entry:
            most = timedelta(days=action.get("days", 0), hours=action.get("hours", 0), minutes=action.get("minutes", 0))
            entry["until"] = (now + most).isoformat() if most else None
            entry["result"] = f"Waiting until {automations_words(engine, action['until_met'], record.table if record is not None else None)}"
        if record is not None and engine.matches(record, action["until_met"]):
            entry.update(done=True, result=f"Went on: {automations_words(engine, action['until_met'], record.table)}")
            return
        deadline = datetime.fromisoformat(entry["until"]) if entry.get("until") else None
        if deadline is not None and deadline <= now:
            entry.update(done=True, result="Stopped waiting: it still isn't so")
            return
        _save(run, log)
        raise _Sleep(min(deadline, now + CHECK_EVERY) if deadline else now + CHECK_EVERY)
    if entry.get("until") is None:
        until = _wait_until(engine, action, record)
        entry.update(until=until.isoformat(), result=f"Waiting until {until:%-d %b %Y, %-I:%M %p}")
        _save(run, log)
        raise _Sleep(until)
    until = datetime.fromisoformat(entry["until"])
    if until > now:
        raise _Sleep(until)
    entry.update(done=True, result=f"Waited until {until:%-d %b %Y, %-I:%M %p}")


OUTWARD = {"send_email", "webhook", "send_sms"}  # steps that reach people or systems outside the business


def reaches_out(automation):
    """Whether a rule sends anything outside the business (emails to customers or owners, another system)."""
    return any(act.get("type") in OUTWARD for act in schema.steps_of(automation.get("actions")))


def _why(engine, automation, action, at, table):
    """Why a step asks, in words: the way of each branch it's on ("Over $500"), its own condition, or the rule's (what
    isn't just a stage being reached)."""
    said, steps, parts = [], automation["actions"], str(at).split(".")
    for i in range(0, len(parts) - 1, 2):  # "3.1.0": step 3 is a branch, its way 1, and step 0 of that way
        if not parts[i].isdigit() or int(parts[i]) >= len(steps):
            break
        branch, way = steps[int(parts[i])], parts[i + 1]
        if branch.get("type") != "branch" or not (way.isdigit() or way == "o"):
            break
        if way == "o":
            steps = branch.get("otherwise") or []
            continue
        said.append(_path_name(engine, branch, int(way), table))
        steps = branch["paths"][int(way)]["steps"]
    if action.get("only_if"):
        said.append(automations_words(engine, action["only_if"], table))
    if not said:
        plain = [c for c in automation.get("conditions") or [] if isinstance(c, dict) and c.get("op") not in ("changed", "changed_to")]
        if plain:
            said.append(automations_words(engine, plain, table))
    return "; ".join(x for x in said if x)


def _path_name(engine, action, n, table):
    path = action["paths"][n]
    return path.get("label") or automations_words(engine, path["when"], table)


def _wait_until(engine, action, record):
    """When a wait step ends (in UTC): after a while, or at a date of the record (the day a quote expires)."""
    if action.get("until"):
        when = expr.to_datetime(engine.value(record, action["until"])) if record is not None else None
        if when is None:
            return utcnow()
        return when.replace(tzinfo=engine.now_tz).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    return utcnow() + timedelta(days=action.get("days", 0), hours=action.get("hours", 0), minutes=action.get("minutes", 0))


def _link(table, record_id):
    return f"table:{table}:record:{record_id}"


def _remember(run, record):
    context = dict(run.context or {})
    context["created"] = (context.get("created") or []) + [{"table": record.table, "id": record.id}]
    run.context = context
    db.session.commit()


def _recipients(engine, to, record):
    """Who a notification is for: [user ids], or [None] for everyone in the workspace."""
    if to in (None, "", "everyone"):
        return [None]
    if to in ("owner", "admin", "editor", "viewer"):
        members = Membership.query.filter_by(workspace_id=engine.ws.id).all()
        return [m.user_id for m in members if m.can(to)] or [None]
    if to.startswith("team:") or to in team_keys(engine):
        return team_members(engine, to.removeprefix("team:")) or [m.user_id for m in Membership.query.filter_by(
            workspace_id=engine.ws.id).all() if m.can("admin")] or [None]
    if to == "creator":  # whoever added the record (asked for the purchase, booked the leave)
        maker = getattr(record, "created_by_id", None) if record is not None else None
        return [maker] if maker else [None]
    if to == "portal":
        return portal_people(engine, record) or [None]
    return engine.people_at(record, to) or [None]


def _ids(value):
    return [int(x) for x in (value if isinstance(value, list) else [value]) if str(x).strip().isdigit()]


def _allocate(engine, automation, action, record, depth):
    """Fill the record's link field with records of the list it links to (a driver for the job, two helpers, a room for
    the booking): those that match `where`, not already on another of the list's records the same `free_on` day (in any
    of its links to that list, as a helper or as the driver), and not on this one in another link; the one on the
    fewest so far first, then the first added. A field already filled in is left as people put it. Too few free: as
    many as there are, and the owner is told to pick the rest."""
    t = engine.tables[record.table]
    f = engine.field(record.table, action["field"])
    if f is None or f.get("type") != "link":
        raise ValueError(f"{action['field']} isn't a link field of {t['label']}")
    if _ids((record.data or {}).get(f["key"])):
        return {"result": f"{f['label']} already filled in: left as it is"}
    target = f["table"]
    links = [x["key"] for x in t["fields"] if x.get("type") == "link" and x.get("table") == target]
    stage = (t.get("workflow") or {}).get("field")
    day = expr.to_date(engine.value(record, action["free_on"], typed=True)) if action.get("free_on") else None
    busy, load = set(), {}
    for r in engine.rows(record.table):
        if r.id == record.id or (stage and schema.says_no((r.data or {}).get(stage))):  # a cancelled job keeps nobody busy
            continue
        theirs = {i for k in links for i in _ids((r.data or {}).get(k))}
        for i in theirs:
            load[i] = load.get(i, 0) + 1
        if day is not None and expr.to_date(engine.value(r, action["free_on"], typed=True)) == day:
            busy |= theirs
    mine = {i for k in links if k != f["key"] for i in _ids((record.data or {}).get(k))}
    pool = [c for c in engine.rows(target) if engine.matches(c, action.get("where") or [])]
    free = sorted((c for c in pool if c.id not in busy and c.id not in mine), key=lambda c: (load.get(c.id, 0), c.id))
    wanted = max(int(action.get("count") or 1), 1)
    chosen = free[:wanted]
    if chosen:
        ids = [c.id for c in chosen]
        engine.update(record, {f["key"]: ids if f.get("multiple") else ids[0]}, by=_by(automation), depth=depth)
    names = ", ".join(engine.title(c) for c in chosen)
    if len(chosen) < wanted:
        when = f" on {engine.display(engine.field(record.table, action['free_on']), day.isoformat())}" if day else ""
        short = "nobody" if not chosen else f"only {len(chosen)} of the {wanted} wanted"
        notify(engine, f"{engine.title(record)}: {short} free for {f['label']}{when}. Pick {'them' if wanted > 1 else 'one'} yourself.",
               _link(record.table, record.id), "owner", record)
    return {"result": f"{f['label']}: {names}" if chosen else f"Nobody free for {f['label']}", "link": _link(record.table, record.id)}


def notify(engine, text, link=None, to="everyone", record=None, kind="info"):
    for user_id in _recipients(engine, to, record):
        db.session.add(Notification(workspace_id=engine.ws.id, user_id=user_id, kind=kind, text=str(text)[:2000], link=link))
    db.session.commit()


def email_notifications(limit=40):
    """Email new notifications to the people who asked for that (Account › Notifications), through the workspace's
    Microsoft 365 mailbox. Each notification is looked at once."""
    from ..connectors import microsoft
    from ..models import User, Workspace
    pending = (Notification.query.filter(Notification.emailed_at.is_(None), Notification.created_at >= utcnow() - timedelta(days=1))
               .order_by(Notification.id).limit(limit).all())
    for n in pending:
        n.emailed_at = utcnow()
        ws = db.session.get(Workspace, n.workspace_id)
        if ws is None or not microsoft.status(ws).get("connected"):
            continue
        members = Membership.query.filter_by(workspace_id=ws.id).all()
        wanted = [m.user_id for m in members if m.role != "portal" and (n.user_id is None or m.user_id == n.user_id)]
        people = User.query.filter(User.id.in_(wanted), User.notify_email.is_(True)).all() if wanted else []
        if not people:
            continue
        from ..builder.chat import navigate_url
        link = navigate_url(ws, n.link) if n.link else None
        body = n.text + (f"\n\n[Open it]({settings()['BASE_URL']}{link})" if link else "")
        for person in people:
            try:
                microsoft.send_mail(ws, person.email, f"{(ws.blueprint or {}).get('name') or ws.name}: {n.text[:90]}", body)
            except Exception as exc:  # a mailbox problem doesn't stop the platform; the notification is still in the app
                _log.warning("Couldn't email a notification to %s: %s", person.email, exc)
    if pending:
        db.session.commit()
    return len(pending)


def _do(engine, automation, action, record, run):
    depth = (run.depth or 0) + 1
    ctx = _context(engine, run, record)
    kind = action["type"]
    if kind == "update_record":
        target = record
        if action.get("target") == "created":
            target = ctx.names["created"].record if ctx.names.get("created") else None
            if target is None:
                return {"result": "Nothing to update: no record was created earlier in this run"}
        elif action.get("target"):
            target = _linked(engine, record, action["target"])
            if target is None:
                return {"result": f"Nothing to update: {_target_words(engine, record.table, action['target'])} is empty"}
        diff = engine.update(target, _render_values(action["values"], ctx, engine, target.table), by=_by(automation), depth=depth)
        t = engine.tables[target.table]
        target = engine.get(target.id)
        if not diff:
            return {"result": f"{t['singular']} “{engine.title(target)}” was already set that way", "link": _link(target.table, target.id),
                    "quiet": True}  # nothing to tell anyone
        changed = ", ".join(f"{engine.field(target.table, k)['label']} → {engine.display(engine.field(target.table, k), v[1])}" for k, v in diff.items())
        return {"result": f"Updated {t['singular']} “{engine.title(target)}”: {changed}", "link": _link(target.table, target.id)}
    if kind == "create_record":
        values = {**copy_matching(engine, action["table"], record), **_render_values(action["values"], ctx, engine, action["table"])} \
            if action.get("copy_matching") else _render_values(action["values"], ctx, engine, action["table"])
        created = engine.create(action["table"], values, by=_by(automation), depth=depth, unfinished=True)
        left = list(getattr(engine, "left_unfilled", None) or [])
        _remember(run, created)
        t = engine.tables[action["table"]]
        if left:  # what the rule couldn't know (which supplier): the record's there, for people to finish
            said = f"{', '.join(left[:-1])} and {left[-1]}" if len(left) > 1 else left[0]
            text = f"{t['singular']} “{engine.title(created)}” was added by “{automation.get('name') or automation['key']}”: fill in {said} to finish it."
            notify(engine, text, link=_link(action["table"], created.id), to="admin", kind="info")
            return {"result": f"Created {t['singular']} “{engine.title(created)}”, for people to finish: {said} still to fill in",
                    "link": _link(action["table"], created.id)}
        return {"result": f"Created {t['singular']} “{engine.title(created)}”", "link": _link(action["table"], created.id)}
    if kind == "copy_record":
        return _copy(engine, automation, action, record, run, ctx, depth)
    if kind == "create_for_each" and action["source"].get("field"):  # one for each invited supplier, each panel member
        f = engine.field(record.table, action["source"]["field"])
        chosen = engine.value(record, f["key"])
        chosen = [x for x in (chosen if isinstance(chosen, list) else [chosen]) if str(x).isdigit()]
        made = []
        with engine.atomic():
            for one in chosen:
                linked = engine.get(one, f["table"]) if f["type"] == "link" else None
                if f["type"] == "link" and (linked is None or action.get("where") and not engine.matches(linked, action["where"])):
                    continue
                item_ctx = _context(engine, run, record, item=linked)
                if f["type"] == "user":
                    item_ctx.names["item"] = engine._person(int(one)) or int(one)  # {{item}}: the person (item.email theirs)
                values = _render_values(action["values"], item_ctx, engine, action["table"])
                if action.get("copy_matching"):
                    values = {**copy_matching(engine, action["table"], linked or record, also=[record]), **values}
                made.append(engine.create(action["table"], values, by=_by(automation), depth=depth, automations=True, unfinished=True))
                _to_finish(engine, automation, action["table"], made[-1])
        t = engine.tables[action["table"]]
        if not made:  # nothing to make them from: said, never “Created 0 lines”
            return {"result": f"No {schema.lc(t['label'])} were added: there was nothing in {f['label']} to add them for"}
        return {"result": f"Created {len(made)} {schema.lc(t['label']) if len(made) != 1 else schema.lc(t['singular'])}, one for each in {f['label']}"}
    if kind == "create_for_each" and action["source"].get("all"):  # each customer whose delivery day is the new round's
        source, made = action["source"], []
        rule = RecordProxy(engine, record) if record is not None else None
        names = {"record": rule, "this": rule} if rule is not None else None
        with engine.atomic():  # all of them or none
            for row in [r for r in engine.rows(source["table"]) if (record is None or r.id != record.id)
                        and engine.matches(r, action.get("where") or [], about=record, names=names)]:
                values = _render_values(action["values"], _context(engine, run, record, item=row), engine, action["table"])
                if action.get("copy_matching"):
                    values = {**copy_matching(engine, action["table"], row, also=[record]), **values}
                made.append(engine.create(action["table"], values, by=_by(automation), depth=depth, unfinished=True))
                _to_finish(engine, automation, action["table"], made[-1])
        t, s = engine.tables[action["table"]], engine.tables[source["table"]]
        if not made:
            return {"result": f"No {schema.lc(t['label'])} were added: no {schema.lc(s['singular'])} matched"}
        return {"result": f"Created {len(made)} {schema.lc(t['label']) if len(made) != 1 else schema.lc(t['singular'])}, "
                          f"one for each matching {schema.lc(s['singular'])}"}
    if kind == "create_for_each":
        source = action["source"]
        made = []
        with engine.atomic():  # all of them or none
            for row in [r for r in _lines(engine, source, _parent(engine, record, source, run))
                        if not action.get("where") or engine.matches(r, action["where"])]:
                values = _render_values(action["values"], _context(engine, run, record, item=row), engine, action["table"])
                if action.get("copy_matching"):
                    made_for = (_context(engine, run, record).names.get("created") or None)
                    values = {**copy_matching(engine, action["table"], row, also=[made_for.record if made_for else None, record]), **values}
                made.append(engine.create(action["table"], values, by=_by(automation), depth=depth, unfinished=True))
                _to_finish(engine, automation, action["table"], made[-1])
        t, s = engine.tables[action["table"]], engine.tables[source["table"]]
        if not made:
            return {"result": f"No {schema.lc(t['label'])} were added: it had no {schema.lc(s['label'])} to copy"}
        return {"result": f"Created {len(made)} {schema.lc(t['label']) if len(made) != 1 else schema.lc(t['singular'])} from its {schema.lc(s['label'])}"}
    if kind == "delete_record":
        target = record
        if action.get("target") == "created":
            target = ctx.names["created"].record if ctx.names.get("created") else None
        elif action.get("target"):
            target = _linked(engine, record, action["target"])
        if target is None:
            return {"result": "Nothing to delete"}
        name, t = engine.title(target), engine.tables[target.table]
        engine.delete(target, by=_by(automation))
        return {"result": f"Deleted {schema.lc(t['singular'])} “{name}” (it can be brought back from its history)"}
    if kind == "update_each":
        source = action["source"]
        rows = [r for r in engine.children(source["table"], source["via"], _parent(engine, record, source, run))
                if not action.get("where") or engine.matches(r, action["where"])]
        changed = 0
        with engine.atomic():  # all of them or none
            for row in list(rows):
                if engine.update(row, _render_values(action["values"], _context(engine, run, record, item=row), engine, row.table), by=_by(automation),
                                 depth=depth):
                    changed += 1
        s = engine.tables[source["table"]]
        return {"result": f"Updated {changed} of {len(rows)} {schema.lc(s['label'])}"}
    if kind == "assign":
        f = engine.field(record.table, action["field"])
        members = Membership.query.filter_by(workspace_id=engine.ws.id).order_by(Membership.user_id).all()
        pool = [m.user_id for m in members if m.role != "portal" and (action["from"] in (m.teams or []) if action["from"] in team_keys(engine)
                                                                      else m.can(action["from"] if action["from"] in ("viewer", "editor", "admin", "owner") else "editor"))]
        if not pool:
            return {"result": "Nobody to assign it to"}
        counts = {uid: 0 for uid in pool}  # whoever has the fewest open records of this table gets the next one
        for r in engine.rows(record.table):
            holder = (r.data or {}).get(f["key"])
            for uid in (holder if isinstance(holder, list) else [holder]):
                if uid in counts and r.id != record.id:
                    counts[uid] += 1
        chosen = min(pool, key=lambda uid: (counts[uid], pool.index(uid)))
        engine.update(record, {f["key"]: [chosen] if f.get("multiple") else chosen}, by=_by(automation), depth=depth)
        return {"result": f"Assigned {f['label']} to {engine.members().get(chosen, 'someone')}", "link": _link(record.table, record.id)}
    if kind == "allocate":
        return _allocate(engine, automation, action, record, depth)
    if kind == "notify":
        text = _render(action["message"], ctx)
        if record is not None:
            link = _link(record.table, record.id)
        else:
            created = ctx.names.get("created")
            link = _link(created.record.table, created.record.id) if created else None
        notify(engine, text, link, action.get("to", "everyone"), record)
        return {"result": f"Notified: “{text}”", "link": link}
    if kind == "send_email":
        sent_to = _render(action["to"], ctx)  # several (every invited seller's email): one email to them all
        to = (", ".join(str(x).strip() for x in sent_to if str(x or "").strip()) if isinstance(sent_to, list) else str(sent_to or "")).strip()
        if not to and "{{" in str(action["to"]):  # the address it's sent to is empty (a customer with no email): not sent, said
            whose = humanize(engine, action["to"], record.table if record is not None else None)
            what = f"{engine.title(record)}: " if record is not None else ""
            notify(engine, f"{what}an email wasn't sent (“{_render(action['subject'], ctx)}”): {whose} is empty. Fill it in and send it "
                           f"from the record.", _link(record.table, record.id) if record is not None else None, "admin", record)
            return {"result": f"Not emailed: {whose} is empty"}
        if not re.fullmatch(r"[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+(\s*[,;]\s*[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+)*", to):
            raise ValueError(f"“{to or 'nothing'}” isn't an email address to send to")
        cc = str(_render(action.get("cc") or "", ctx) or "").strip()
        subject = str(_render(action["subject"], ctx))
        body = str(_render(action["body"], ctx))
        attachments = []
        for key in action.get("attach", []):
            attachments += _attachments(engine, record, key, run)
        # into the outbox with this step's save: the worker sends it (a test or a try-out there and then), and the rule
        # carries on now instead of waiting on the mailbox
        return outbox.step(outbox.add(engine.ws.id, run.id, "email", to, subject=subject, body=body, cc=cc, attachments=attachments))
    if kind == "send_sms":
        to = str(_render(action.get("to") or "", ctx) or "").strip()
        message = str(_render(action.get("message") or action.get("body") or "", ctx))
        if not to and "{{" in str(action.get("to") or ""):  # the number it's sent to is empty: not sent, said
            whose = humanize(engine, action["to"], record.table if record is not None else None)
            what = f"{engine.title(record)}: " if record is not None else ""
            notify(engine, f"{what}a text message wasn't sent: {whose} is empty. Fill it in and send it from the record.",
                   _link(record.table, record.id) if record is not None else None, "admin", record)
            return {"result": f"Not texted: {whose} is empty"}
        number = re.sub(r"[\s().\-]", "", to)
        if not re.fullmatch(r"\+?\d{6,15}", number):
            raise ValueError(f"“{to or 'nothing'}” isn't a phone number to send a text message to")
        return outbox.step(outbox.add(engine.ws.id, run.id, "sms", number, body=message))
    if kind == "generate_document":
        from ..documents import produce
        target = record
        if action.get("target") == "created":
            target = ctx.names["created"].record if ctx.names.get("created") else None
        elif action.get("target"):
            target = _linked(engine, record, action["target"])
        if target is None:
            return {"result": "No document produced: there was no record to produce it for"}
        file = produce(engine, target, action["document"], field=action.get("field", ""), by=_by(automation))
        return {"result": f"Produced {file.name}", "file": file.id, "link": _link(target.table, target.id)}
    if kind == "run_automation":
        return _run_other(engine, automation, action, record, run, ctx)
    if kind == "payment_link":  # a card payment page (Stripe) for the amount, kept on the record; paying it sets "paid"
        from ..connectors import apps
        if record is None:
            raise ValueError("a payment link is for a record (an invoice)")
        amount = expr.to_number(_render(action["amount"], ctx))
        if not amount or amount <= 0:
            return {"result": "No payment link: there's nothing to pay"}
        currency = (str(_render(action["currency"], ctx)) if action.get("currency") else "") or engine.base_currency  # set, or where it is
        if not currency or not re.fullmatch(r"[A-Za-z]{3}", currency):
            raise ValueError("a payment link needs the currency: set your currency's code under Settings › Business (AUD, GBP, USD…)")
        if settings().get("TRYING_OUT"):
            return {"result": f"Would make a payment link for {currency.upper()} {amount:,.2f} (not while trying the app out)"}
        stripe, conn = apps.get("stripe"), apps.connection(engine.ws, "stripe")
        if conn is None or conn.status != "connected":
            raise ValueError("Stripe isn't connected: connect it under Settings › Connections")
        description = str(_render(action["description"], ctx)) if action.get("description") else engine.title(record)
        try:
            url, _ = stripe.checkout(apps.creds(conn), amount, currency, description,
                                     f"{settings()['BASE_URL']}/paid",
                                     {"workspace": engine.ws.id, "record": record.id, "paid": json.dumps(action.get("paid") or {})})
        except apps.AppProblem as exc:
            raise ValueError(str(exc)) from exc
        engine.update(record, {action["save_to"]: url}, by=_by(automation), depth=depth)
        return {"result": f"Made a payment link for {currency.upper()} {amount:,.2f}"}
    if kind == "import_rows":  # a seller's price list brought into its catalogue, row by row, each row's outcome kept
        from .. import importer
        fixed = _render_values(action.get("values") or {}, ctx, engine, action["table"])
        return importer.rows_into(engine, action, record, fixed, by=_by(automation), depth=depth + 1)
    if kind == "webhook" and action.get("app"):  # a connected app (Xero, Shopify, EasyPost…): its address and sign-in added
        from ..connectors import apps
        payload = _deep(engine, action.get("payload") or {}, ctx, record)
        path = _render(action["url"], ctx) if "{{" in action["url"] else action["url"]
        app = apps.get(action["app"])
        if settings().get("TRYING_OUT"):
            return {"result": f"Would send to {app.name if app else action['app']} {path} (not sent while trying the app out)"}
        try:
            answer = apps.call(engine.ws, action["app"], action.get("method", "POST"), path, payload=payload)
        except apps.AppProblem as exc:
            raise ValueError(str(exc)) from exc
        if action.get("save") and record is not None:
            values = {key: _walk(answer, p) for key, p in action["save"].items()}
            engine.update(record, values, by=_by(automation), depth=depth)
            return {"result": f"Sent to {app.name} and saved {', '.join(engine.field(record.table, k)['label'] for k in values if engine.field(record.table, k))}"}
        return {"result": f"Sent to {app.name}"}
    if kind == "webhook":
        payload = action.get("payload")
        if payload:
            payload = {k: _plain(_render(v, ctx)) for k, v in payload.items()}
        elif record is not None:
            payload = {"id": record.id, "table": record.table,
                       "record": {f["key"]: _plain(engine.value(record, f["key"])) for f in engine.tables[record.table]["fields"]}}
        url = str(_render(action["url"], ctx) if "{{" in action["url"] else action["url"]).strip()
        _reachable(engine, action, ctx, url)  # only the hosts it (and the app) may reach: refused before anything is sent
        if settings().get("TRYING_OUT"):  # a copy being tried reaches nothing outside
            return {"result": f"Would send to {url} (not sent while trying the app out)"}
        headers = _headers(engine, action, ctx)
        if action.get("method", "POST") == "GET":
            response = requests.get(url, params={k: v for k, v in (payload or {}).items() if not isinstance(v, (dict, list))}, headers=headers,
                                    timeout=15)
        else:
            response = requests.request(action.get("method", "POST"), url, json={"automation": automation["name"], **(payload or {})},
                                        headers=headers, timeout=15)
        if response.status_code >= 400:
            raise ValueError(f"{url} answered {response.status_code}")
        if action.get("save") and record is not None:  # keep parts of the answer (an exchange rate, a tracking number)
            try:
                answer = response.json()
            except ValueError:
                wrapped = re.fullmatch(r"\s*[\w.$]+\((.*)\)\s*;?\s*", response.text or "", re.S)  # JSONP: callback({...})
                try:
                    answer = json.loads(wrapped.group(1)) if wrapped else None
                except ValueError:
                    answer = None
                if answer is None:
                    raise ValueError(f"{url} didn't answer with JSON, so nothing could be saved from it") from None
            values = {key: _walk(answer, path) for key, path in action["save"].items()}
            engine.update(record, values, by=_by(automation), depth=depth)
            return {"result": f"Called {url} and saved {', '.join(engine.field(record.table, k)['label'] for k in values if engine.field(record.table, k))}"}
        return {"result": f"Sent to {url} (it answered {response.status_code})"}
    raise ValueError(f"unknown action {kind}")


def allowed_hosts(engine, action, ctx):
    """The lists of hosts a call may reach, each of which must allow it: the step's own (a list, or a field holding one per
    line: each buyer's ERP), and the app's (Settings). [] when neither says."""
    lists = []
    said = action.get("hosts")
    if said:
        text = _render(said, ctx) if isinstance(said, str) and "{{" in said else said
        items = text if isinstance(text, list) else re.split(r"[\s,;]+", str(text or ""))
        lists.append({str(h).strip().lower() for h in items if str(h or "").strip()})  # a field left empty reaches nothing
    app_wide = (engine.ws.settings or {}).get("outbound_hosts") or []
    if app_wide:
        lists.append({str(h).strip().lower() for h in app_wide if str(h).strip()})
    return lists


def _host_allowed(host, allowed):
    """sap.example.com is allowed by itself, or by *.example.com."""
    return any(host == h or (h.startswith("*.") and host.endswith(h[1:])) for h in allowed)


def _reachable(engine, action, ctx, url):
    """Refuses a call to a host the step or the app doesn't allow (an address from a record must stay within them)."""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not host:
        raise ValueError(f"“{url[:200]}” isn't a web address")
    for allowed in allowed_hosts(engine, action, ctx):
        if not _host_allowed(host, allowed):
            raise ValueError(f"{host} isn't one of the hosts this may reach ({', '.join(sorted(allowed)) or 'none are set'})")


def _headers(engine, action, ctx):
    """The step's headers, a key kept secret in a field written in full: only ever sent to the hosts allowed."""
    if not action.get("headers"):
        return {}
    was, engine.revealing = engine.revealing, True
    try:
        return {k: str(_render(v, ctx) if "{{" in str(v) else v) for k, v in action["headers"].items()}
    finally:
        engine.revealing = was


def _sureness(action, answer, values, fraction=False):
    """An AI step's answer with how sure it was kept (the step's confidence field: a percentage field keeps 92 as 0.92)
    and judged by the step's threshold: under sure_above (or with no number at all) it isn't sure, whatever it said."""
    confidence, values = answer.get("confidence"), dict(values)
    if action.get("confidence") and confidence is not None:
        values[action["confidence"]] = confidence / 100 if fraction else confidence
    sure = answer["sure"]
    if action.get("sure_above") is not None:
        sure = sure and confidence is not None and confidence >= action["sure_above"]
    return {"values": values, "sure": sure}


def _percent(engine, record, action):
    """Whether an AI step keeps its confidence in a percentage field."""
    f = engine.field(record.table, action["confidence"]) if action.get("confidence") and record is not None else None
    return f is not None and engine.kind(f) == "percent"


def _walk(answer, path):
    """A part of an answer by its path: "Invoices.0.InvoiceID", "postage_label.label_url"."""
    value = answer
    for part in [p for p in str(path).replace("[", ".").replace("]", "").split(".") if p]:
        value = value[int(part)] if isinstance(value, list) and part.isdigit() and int(part) < len(value) else \
            value.get(part) if isinstance(value, dict) else None
    return value


def _deep(engine, value, ctx, record):
    """What's sent to an app, every template in it filled in, however deep. {"each": "invoice_lines", "item": {...}}
    becomes one item for each of the record's lines, with {{line.x}} for the line's details."""
    if isinstance(value, dict) and "each" in value and "item" in value:
        table = engine.tables.get(str(value["each"]))
        link = next((f["key"] for f in (table or {"fields": []})["fields"] if f["type"] == "link" and record is not None
                     and f.get("table") == record.table), None)
        if table is None or link is None:
            return []
        lines = engine.related(record, table["key"], link)
        return [_deep(engine, value["item"], RecordContext(engine, record, {**ctx.names, "line": RecordProxy(engine, line)}), record)
                for line in lines]
    if isinstance(value, dict):
        return {k: _deep(engine, v, ctx, record) for k, v in value.items()}
    if isinstance(value, list):
        return [_deep(engine, v, ctx, record) for v in value]
    if isinstance(value, str) and "{{" in value:
        return _plain(_render(value, ctx))
    return value


def _to_finish(engine, automation, table, created):
    """A record a rule added without something it must have (which supplier): it's there, and people are told to finish it."""
    left = list(getattr(engine, "left_unfilled", None) or [])
    if not left or created is None:
        return
    t = engine.tables[table]
    said = f"{', '.join(left[:-1])} and {left[-1]}" if len(left) > 1 else left[0]
    notify(engine, f"{t['singular']} “{engine.title(created)}” was added by “{automation.get('name') or automation['key']}”: fill in "
                   f"{said} to finish it.", link=_link(table, created.id), to="admin", kind="info")


def _copy(engine, automation, action, record, run, ctx, depth):
    """A copy of the record and its lines (a contract's next revision, a renewal, a repeat order): its own values, less
    its number and stage (it starts again), with the step's values on top and a link back to the original."""
    t = engine.tables[record.table]
    flow = (t.get("workflow") or {}).get("field")
    values = {f["key"]: (record.data or {}).get(f["key"]) for f in t["fields"]
              if f["type"] in schema.STORED and f["type"] != "autonumber" and f["key"] != flow and not f.get("unique")}
    values.update(_render_values(action["values"], ctx, engine, record.table))
    if action.get("link_back"):
        values[action["link_back"]] = record.id
    lines = 0
    with engine.atomic():
        copy = engine.create(t["key"], values, by=_by(automation), depth=depth, unfinished=True)
        _to_finish(engine, automation, t["key"], copy)
        for child in engine.bp.get("tables", []) if action.get("lines", True) else []:
            link = next((f for f in child["fields"] if f["type"] == "link" and f["table"] == t["key"] and not f.get("multiple")), None)
            if not child.get("embedded") or link is None:
                continue
            for line in engine.children(child["key"], link["key"], record.id):
                kept = {f["key"]: (line.data or {}).get(f["key"]) for f in child["fields"] if f["type"] in schema.STORED and f["type"] != "autonumber"}
                engine.create(child["key"], {**kept, link["key"]: copy.id}, by=_by(automation), depth=depth, automations=False, unfinished=True)
                lines += 1
    _remember(run, copy)
    return {"result": f"Made {t['singular']} “{engine.title(copy)}” from “{engine.title(record)}”"
                      + (f" with its {lines} line{'s' if lines != 1 else ''}" if lines else ""), "link": _link(t["key"], copy.id)}


def propose_change(engine, record, values, by):
    """A person's change to fields whose changes need approval (a supplier's bank account): kept aside, not saved, until
    someone else approves it (never the person who made it); after a cooling-off period if one is set; the record's
    contact told, with a link to cancel it."""
    import secrets
    t = engine.tables[record.table]
    held = t["approve_changes"]
    for older in Approval.query.filter_by(workspace_id=engine.ws.id, record_id=record.id, kind="change", status="pending").all():
        older.status = "cancelled"  # a newer change takes its place
        older.history = (older.history or []) + [{"when": utcnow().isoformat(), "what": "replaced by a newer change"}]
    shown = {}
    for k, new in values.items():
        f = engine.field(record.table, k)
        old = (record.data or {}).get(k)
        if f.get("sensitive"):
            from .records import masked
            shown[f["label"]] = [masked(old) or "—", masked(new) or "—"]
        else:
            shown[f["label"]] = [engine.display(f, old, record), engine.display(f, new, record)]
    ready = utcnow() + timedelta(days=held["wait_days"]) if held.get("wait_days") else None
    proposal = {"values": values, "shown": shown, "by": by, "by_id": engine.user_id, "contact": engine.portal_contact,
                "ready_at": ready.isoformat() if ready else None, "step_up": bool(held.get("step_up")),
                "told_first": bool(held.get("told_first"))}
    if held.get("tell"):
        proposal["token"] = secrets.token_urlsafe(24)
    title = engine.title(record)
    what = _and(list(shown))
    run = Run(workspace_id=engine.ws.id, automation="_change", name=f"Change to {t['singular']} “{title}”", record_id=record.id,
              table=record.table, trigger=f"{by} changed {what}", status="waiting_approval", steps=[], context={})
    db.session.add(run)
    db.session.flush()
    people = [(uid, role) for uid, role in _assignees(engine, held["approver"], record) if uid is None or uid != engine.user_id] or [(None, "admin")]
    message = f"Approve the change to {what} of {schema.lc(t['singular'])} “{title}”, made by {by}"
    rows = []
    for uid, role in dict.fromkeys(people):
        rows.append(Approval(workspace_id=engine.ws.id, run_id=run.id, record_id=record.id, table=record.table, message=message,
                             assignee_id=uid, role=role, kind="change", step="change", history=[], proposal=proposal))
        db.session.add(rows[-1])
    db.session.flush()

    def tell():
        for r in rows:
            _tell(engine, r, record, f"Approval needed: {message}" + (f" (it can be approved from {ready:%d %b})" if ready else ""))
        if held.get("tell"):
            how = _tell_contact(engine, record, held["tell"], proposal["token"], what, by)
            if how:  # the notice went out: kept on the change (and, with told_first, it may now be approved)
                for r in rows:
                    r.proposal = {**(r.proposal or {}), "told_at": utcnow().isoformat(), "told_by": how}
                db.session.commit()
    if engine._depth:
        engine._after.append(tell)
    else:
        db.session.commit()
        tell()


def _tell_contact(engine, record, field_key, token, what, by):
    """Tell the record's registered contact about a change to it, with a link that cancels it (the "this wasn't me" link):
    by email when the business's mailbox is connected, and in the portal when they use it."""
    from ..connectors import microsoft
    address = engine.value(record, field_key) if "." not in field_key else engine._at_path(record, field_key)[1]
    url = f"{settings().get('BASE_URL', '')}/c/{token}"
    text = (f"{by} changed {what} on your record with {(engine.bp.get('name') or engine.ws.name)}. If this wasn't you or "
            f"someone you trust, cancel it here before it's approved: {url}")
    sent = False
    if isinstance(address, str) and "@" in address and microsoft.status(engine.ws).get("connected"):
        try:
            microsoft.send_mail(engine.ws, address, f"A change to {what}: please check it", text)
            sent = True
        except Exception as exc:  # the change still waits for approval; the approver sees nobody was told by email
            _log.warning("Couldn't tell %s about a change: %s", address, exc)
    told = portal_people(engine, record)
    for uid in told:
        db.session.add(Notification(workspace_id=engine.ws.id, user_id=uid, kind="approval", text=text[:2000], link=f"cancel:{token}"))
    db.session.commit()
    return " and ".join(x for x in ("by email" if sent else "", "in the portal" if told else "") if x)  # how it went out ("": it didn't)


def decide_change(engine, change, approved, by, comment=None):
    """Approve (it's saved now, as the change it was) or turn down a change waiting for approval."""
    proposal = change.proposal or {}
    if approved and proposal.get("told_first") and not proposal.get("told_at"):
        raise FieldErrors({"_": "This change can't be approved yet: the registered contact hasn't been told of it (no email went "
                                "out and they aren't in the portal). Tell them first, then approve it."})
    if approved and proposal.get("ready_at") and datetime.fromisoformat(proposal["ready_at"]) > utcnow():
        raise FieldErrors({"_": f"This change can be approved from {datetime.fromisoformat(proposal['ready_at']):%d %b %Y %H:%M} "
                                "(its cooling-off period)."})
    change.status = "approved" if approved else "rejected"
    change.decided_by, change.comment, change.decided_at = by, (comment or "").strip()[:1000] or None, utcnow()
    for other in Approval.query.filter_by(run_id=change.run_id, status="pending").all():
        if other.id != change.id:
            other.status = "cancelled"
    run = db.session.get(Run, change.run_id)
    record = engine.get(change.record_id)
    if approved and record is not None:
        system = engine.system()
        system.update(record, proposal.get("values") or {}, by=f"{by}, approving {proposal.get('by', 'someone')}'s change")
    run.status, run.finished_at = ("done" if approved else "rejected"), utcnow()
    run.steps = [{"at": "change", "result": f"{'Approved' if approved else 'Turned down'} by {by}" + (f" (“{comment.strip()}”)" if comment and comment.strip() else "")}]
    db.session.commit()
    maker = proposal.get("by_id")
    if maker:
        db.session.add(Notification(workspace_id=engine.ws.id, user_id=maker, kind="approval", link=_link(change.table, change.record_id),
                                    text=f"{by} {'approved' if approved else 'turned down'} your change: {change.message}"))
        db.session.commit()
    return run


def cancel_change(token):
    """The registered contact's "this wasn't me": the change is cancelled before anyone approves it. Returns it, or None."""
    for change in Approval.query.filter_by(kind="change", status="pending").all():
        if token and hmac.compare_digest(str((change.proposal or {}).get("token") or ""), token):
            change.status = "cancelled"
            change.history = (change.history or []) + [{"when": utcnow().isoformat(), "what": "cancelled by the registered contact"}]
            run = db.session.get(Run, change.run_id)
            run.status, run.finished_at, run.error = "rejected", utcnow(), "Cancelled by the registered contact"
            db.session.add(Notification(workspace_id=change.workspace_id, kind="approval", link=_link(change.table, change.record_id),
                                        text=f"The registered contact cancelled a change: {change.message}"))
            db.session.commit()
            return change
    return None


def copy_matching(engine, table, source, also=()):
    """What a new record copies when the step doesn't say: fields with the same name and type as the source's, and
    each link filled with the record it points to among the source and `also` (the invoice a line belongs on)."""
    t = engine.tables.get(table)
    if t is None or source is None:
        return {}
    out = {}
    nearby = [r for r in [source, *also] if r is not None]
    for f in t["fields"]:
        if f["type"] not in schema.STORED or f["type"] == "autonumber":
            continue
        if f["type"] == "link":
            match = next((r for r in nearby if r.table == f["table"]), None)
            if match is not None:
                out[f["key"]] = match.id
                continue
        # the source's own field of that name, else the record it came with (a PO line's stock movement takes the PO's
        # warehouse; an order line's, the order's): the nearest one that has it filled in
        for r in nearby:
            theirs = engine.field(r.table, f["key"])
            if theirs is None or theirs["type"] != f["type"] or (f["type"] == "link" and theirs.get("table") != f["table"]):
                continue
            value = (r.data or {}).get(f["key"])
            if not _filled_value(value):
                continue
            if f["type"] in ("select", "multi_select") and set(theirs.get("options", [])) - set(f.get("options", [])):
                break  # their choices don't all exist here (a job's Done isn't an invoice status)
            out[f["key"]] = value
            break
    return out


def _filled_value(v):
    return v not in (None, "", [])


def _plain(value):
    if isinstance(value, RecordProxy):
        return str(value)
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def _source_files(run, record, source):
    """The files a step reads: the email's or the arriving file's, a file field's, or (for "file" on a record) every
    file attached to the record."""
    if source in ("email", "file"):
        ids = (run.context or {}).get("files") or []
        if ids:
            return File.query.filter(File.id.in_(ids), File.deleted_at.is_(None)).all()
        if source == "file" and record is not None:
            return File.query.filter_by(record_id=record.id, deleted_at=None).order_by(File.id).all()
        return []
    return File.query.filter_by(record_id=record.id, field=source, deleted_at=None).all() if record is not None else []


def _attachments(engine, record, key, run):
    """What an email attaches, as the outbox keeps it: a document template's PDF (the one produced earlier in this run,
    or a fresh one) or the files in a file field, by their file ({"file", "name"}); a page's report, made now, by its
    contents ({"name", "mime", "data"})."""
    from ..documents import produce
    if key.startswith("page:"):  # a dashboard as a report: the weekly sales page, emailed every Monday
        from ..web.erp_pages import page_report
        page = next((p for p in engine.bp.get("pages", []) if p["key"] == key[5:]), None)
        if page is None:
            raise ValueError(f"there's no page “{key[5:]}” to attach")
        return [{"name": f"{page['label']} {engine.today.isoformat()}.pdf", "mime": "application/pdf",
                 "data": base64.b64encode(page_report(engine, page)).decode()}]
    t = engine.tables[record.table] if record is not None else {}
    created = (run.context or {}).get("created") or []
    if not any(d["key"] == key for d in t.get("documents", [])) and created:  # the new invoice's PDF, after making the invoice
        made_by_run = engine.get(created[-1]["id"])
        if made_by_run is not None and any(d["key"] == key for d in engine.tables[made_by_run.table].get("documents", [])):
            record, t = made_by_run, engine.tables[made_by_run.table]
    if any(d["key"] == key for d in t.get("documents", [])):
        made = next((db.session.get(File, s["file"]) for s in reversed(run.steps or []) if s.get("file")), None)
        file = made if made is not None and made.record_id == record.id else produce(engine, record, key, by=f"automation run {run.id}")
        return [{"file": file.id, "name": file.name}]
    files = File.query.filter_by(record_id=record.id, field=key, deleted_at=None).all()
    return [{"file": f.id, "name": f.name} for f in files]


ROLE_WORDS = {"owner": "the owner", "admin": "an admin", "editor": "an editor"}


def team_keys(engine):
    return {tm["key"] for tm in schema.teams(engine.bp)}


def team_name(engine, key):
    return next((tm["name"] for tm in schema.teams(engine.bp) if tm["key"] == key), schema.label_of(key))


def team_members(engine, key):
    return [m.user_id for m in Membership.query.filter_by(workspace_id=engine.ws.id).all() if m.role != "portal" and key in (m.teams or [])]


def portal_people(engine, record):
    """The outside people (user ids) a record is for: those signed in to the portal whose record it reaches."""
    if record is None:
        return []
    return [m.user_id for m in Membership.query.filter_by(workspace_id=engine.ws.id, role="portal").all()
            if m.contact_id and engine.portal_reaches(record, m.contact_id)]


def covering(engine, user_id, day=None):
    """Who covers for someone away today (their approvals and tasks go there meanwhile), or None."""
    m = Membership.query.filter_by(workspace_id=engine.ws.id, user_id=user_id).first() if user_id else None
    day = day or engine.today
    if m is None or not m.cover_id or not m.away_from or not m.away_until or not (m.away_from <= day <= m.away_until):
        return None
    return m.cover_id if m.cover_id != user_id else None


def _kept_out(engine, user_id, apart, record):
    """Why the person an approval or task is for may not answer it (separation of duties: they raised it), or None."""
    m = Membership.query.filter_by(workspace_id=engine.ws.id, user_id=user_id).first() if user_id and apart and record is not None else None
    if m is None:
        return None
    why = Engine(engine.ws, by=engine.members().get(user_id), member=m).kept_from(record, apart)
    return re.sub(r"^you\b", "they", why) if why else None


def _assignees(engine, who, record, each=False):
    """Who a step's approval or task goes to, as [(user id, role)]: anyone with a role (with `each`, every one of them by
    name), anyone in a team, the person who added the record, its outside person (in the portal), or the people named
    on it (a person field, or one through its links). Nobody named: an admin."""
    if who in ROLE_WORDS:
        if each:
            people = [m.user_id for m in Membership.query.filter_by(workspace_id=engine.ws.id).all() if m.role != "portal" and m.can(who)]
            if people:
                return [(uid, None) for uid in people]
        return [(None, who)]
    if who in team_keys(engine):
        people = team_members(engine, who)
        if each and people:
            return [(uid, None) for uid in people]
        return [(None, f"team:{who}")] if people else [(None, "admin")]
    if who == "creator":
        maker = getattr(record, "created_by_id", None) if record is not None else None
        return [(maker, None)] if maker else [(None, "admin")]
    if who == "portal":
        return [(uid, None) for uid in portal_people(engine, record)] or [(None, "admin")]
    return [(uid, None) for uid in engine.people_at(record, who)] or [(None, "admin")]


def _priority(engine, action, record):
    """How pressing a task is, 0 (low) to 3 (urgent): as the step says, or as a field of the record says (Urgency:
    Emergency)."""
    said = action.get("priority")
    if not said:
        return 1
    if said in schema.PRIORITIES:
        return schema.PRIORITIES[said]
    value = str(engine.value(record, said) or "").strip().lower() if record is not None and engine.field(record.table, said) else ""
    return next((n for word, n in sorted(schema.PRIORITIES.items(), key=lambda x: -x[1]) if word in value), 1)


def _named(engine, rows):
    names = []
    for r in rows:
        if r.assignee_id:
            names.append(engine.members().get(r.assignee_id, "someone"))
        elif (r.role or "").startswith("team:"):
            names.append(team_name(engine, r.role[5:]))
        else:
            names.append(ROLE_WORDS.get(r.role, "an admin"))
    names = list(dict.fromkeys(names))
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _names_it(engine, record, message):
    """Whether a message already says which record it's about: its name, its number or code, or what it's called."""
    t = engine.tables.get(record.table) or {}
    key = schema.title_field(t) if t else None
    said = [engine.title(record), str((record.data or {}).get(key) or "") if key else ""]
    own = schema.name_field(t, key) if t and key else None
    said.append(str((record.data or {}).get(own) or "") if own else "")
    return any(x and len(x) >= 3 and x in message for x in said)


def _ask(engine, automation, action, record, run, at):
    """An approval or a task: asked of its people the first time the run reaches it, then the answer read on each look.
    Returns {"result"} once approved or done, {"rejected": True, "result"} on a no; raises Paused while waiting."""
    task = action["type"] == "task"
    kind = "task" if task else "approval"
    rows = Approval.query.filter(Approval.run_id == run.id, or_(Approval.step == at, Approval.step.is_(None))).order_by(Approval.id).all()
    rows = [r for r in rows if (r.kind or "approval") == kind]
    if not rows:
        ctx = _context(engine, run, record)
        message = str(_render(action["message"], ctx))
        if record is not None and not task and not _names_it(engine, record, message):  # "Please approve this": which one, and why
            t = engine.tables.get(record.table) or {}
            why = _why(engine, automation, action, at, record.table)
            message = f"{message.rstrip()} ({t.get('singular', 'record').lower()} “{engine.title(record)}”{': ' + why if why else ''})"
        each = action.get("mode") == "all"
        people = [p for who in (([action["to"]] if task else action.get("approvers") or [action["approver"]])) for p in _assignees(engine, who, record, each)]
        rules = {k: action[k] for k in ("not_by", "step_up") if action.get(k)}  # kept with it: checked when someone answers
        for uid, role in dict.fromkeys(people):
            history, cover = [], covering(engine, uid)
            if cover:  # away: it goes to whoever covers for them
                history.append({"when": utcnow().isoformat(), "what": "covering", "for": engine.members().get(uid, "someone")})
                uid = cover
            why_not = _kept_out(engine, uid, rules.get("not_by"), record)
            if why_not:  # the one it names may not answer it (they raised it): an admin does, or it would wait for ever
                history.append({"when": utcnow().isoformat(),
                                "what": f"passed to an admin, as {engine.members().get(uid, 'the person named')} can't answer it: {why_not}"})
                uid, role = None, "admin"
                if any(r.assignee_id is None and r.role == "admin" for r in rows):
                    continue
            rows.append(Approval(workspace_id=engine.ws.id, run_id=run.id, record_id=record.id if record is not None else None,
                                 table=record.table if record is not None else None, message=message, assignee_id=uid, role=role,
                                 kind=kind, step=at, fields=action.get("fields") if task else None, history=history,
                                 proposal=rules or None, priority=_priority(engine, action, record),
                                 flow=action.get("flow") if task else None))
            db.session.add(rows[-1])
        db.session.commit()
        for r in rows:
            _tell(engine, r, record, f"{'To do' if task else 'Approval needed'}: {message}")
    live = [r for r in rows if r.status not in ("cancelled", "escalated")]
    if not live and any((h.get("what") == "withdrawn") for r in rows for h in r.history or []):
        raise _Stop("rejected", "Withdrawn before anyone answered")  # moved to Cancelled while it waited: nothing to ask now
    said = lambda r: f" (“{r.comment}”)" if r.comment else ""  # noqa: E731
    no = next((r for r in live if r.status == "rejected"), None)
    if no is not None:
        _cancel(r for r in live if r.status == "pending")
        run.context = {**(run.context or {}), "reason": no.comment or ""}  # why they said no: {{reason}} in the steps after
        return {"rejected": True, "result": f"Rejected by {no.decided_by}{said(no)}"}
    yes = [r for r in live if r.status in ("approved", "done")]
    pending = [r for r in live if r.status == "pending"]
    if yes and (task or action.get("mode") != "all" or not pending):
        _cancel(pending)
        if task:
            return {"result": f"Done by {yes[0].decided_by}{said(yes[0])}"}
        if yes[-1].decided_by_id:
            run.context = {**(run.context or {}), "approver": yes[-1].decided_by_id}
        run.context = {**(run.context or {}), "reason": yes[-1].comment or ""}
        return {"result": "Approved by " + ", ".join(f"{r.decided_by}{said(r)}" for r in yes)}
    until = _follow_up(engine, action, record, run, pending, at, kind)
    waiting_for = _named(engine, [r for r in live if r.status == "pending"])
    raise Paused(f"Waiting for {waiting_for} to {'do it' if task else 'approve'}", until)


def _tell(engine, row, record, text):
    link = _link(record.table, record.id) if record is not None else "approvals"
    if row.assignee_id:
        db.session.add(Notification(workspace_id=engine.ws.id, user_id=row.assignee_id, kind="approval", text=text[:2000], link=link))
        db.session.commit()
    else:
        notify(engine, text, link, row.role or "admin", record, kind="approval")


def _cancel(rows):
    for r in rows:
        r.status = "cancelled"
    db.session.commit()


def withdraw(engine, record, by):
    """A record moved to a stop (Cancelled, Withdrawn) while it waited for approval: nobody is asked any more."""
    for r in engine.waiting_for(record):
        r.status = "cancelled"
        r.history = (r.history or []) + [{"when": utcnow().isoformat(), "what": "withdrawn", "by": by}]
    db.session.commit()


def _follow_up(engine, action, record, run, pending, at, kind):
    """Remind people who haven't answered, and pass overdue ones up to "escalate_to". Returns when to look again."""
    now, times = utcnow(), []
    for r in list(pending):
        done = {h.get("what") for h in (r.history or [])}
        if action.get("remind_after_days") and "reminded" not in done:
            due = r.created_at + timedelta(days=action["remind_after_days"])
            if due <= now:
                _tell(engine, r, record, f"Reminder, still waiting for you: {r.message}")
                r.history = (r.history or []) + [{"when": now.isoformat(), "what": "reminded"}]
            else:
                times.append(due)
        if action.get("escalate_after_days") and "escalated" not in done and "escalation" not in done:
            due = r.created_at + timedelta(days=action["escalate_after_days"])
            if due > now:
                times.append(due)
                continue
            r.history = (r.history or []) + [{"when": now.isoformat(), "what": "escalated"}]
            if action.get("mode") == "all":  # in their place
                r.status = "escalated"
            for uid, role in _assignees(engine, action["escalate_to"], record):
                up = Approval(workspace_id=engine.ws.id, run_id=run.id, record_id=r.record_id, table=r.table, message=r.message,
                              assignee_id=uid, role=role, kind=kind, step=at, fields=r.fields,
                              history=[{"when": now.isoformat(), "what": "escalation", "by": _named(engine, [r])}])
                db.session.add(up)
                db.session.commit()
                _tell(engine, up, record, f"Passed up to you (no answer in {_days(action['escalate_after_days'])}): {r.message}")
    db.session.commit()
    return min(times) if times else None


def _days(n):
    return f"{int(n) if float(n).is_integer() else n} day{'s' if n != 1 else ''}"


def may_decide(approval, member, user_id, record=None, engine=None):
    """Whether this person can answer an approval or do a task: theirs by name (or covering for whoever it's for while
    they're away), for their team, or for their role. Someone who asked for something can't approve it themselves through
    their role (the owner can); a step kept apart from people (whoever raised it, whoever recommended it) never lets them,
    the owner included; nor does anyone approve their own change."""
    if approval.status != "pending" or member is None:
        return False
    if approval.kind == "change" and (approval.proposal or {}).get("by_id") == user_id:
        return False
    if engine is not None and record is not None and kept_from(engine, approval, record):
        return False
    if approval.assignee_id is not None:
        if approval.assignee_id == user_id:
            return True
        return engine is not None and covering(engine, approval.assignee_id) == user_id
    if (approval.role or "").startswith("team:"):
        return approval.role[5:] in (member.teams or []) or member.role == "owner"
    if not member.can(approval.role or "admin"):
        return False
    return not ((approval.kind or "approval") == "approval" and record is not None and getattr(record, "created_by_id", None) == user_id
                and member.role != "owner")


def kept_from(engine, approval, record):
    """Why this person may not answer an approval or task kept apart from them (separation of duties), or None."""
    apart = (approval.proposal or {}).get("not_by") if (approval.kind or "approval") in ("approval", "task") else None
    return engine.kept_from(record, apart) if apart and record is not None else None


def _carry_on(engine, run):
    automation = find(engine, run.automation)
    if automation and _at_once(automation):
        # held as it's queued, so no worker takes it in between; a worker looking at it this moment (a reminder) has a few
        # seconds to finish, and if it's still on it, it's asked to look again as soon as it's done
        if leases.hold(run, wait=5, status="queued", next_try=None):
            execute(engine.system(), run)  # the rule carries on as itself: the person who answered moves records only by its buttons
        else:
            leases.when_free(run, wait=10, next_try=utcnow())
    elif not leases.when_free(run, wait=5, status="queued", next_try=None):
        leases.when_free(run, wait=10, next_try=utcnow())
    return run


def _wait_for_worker(run):
    """A worker may be looking at this run at the very moment someone answers it (its reminder is due): its look is over
    in a moment, and then the run is read again, so the answer carries it on instead of being missed."""
    if run.status == "running" and not leases.mine(run):
        db.session.commit()
        deadline = utcnow() + timedelta(seconds=5)
        while utcnow() < deadline:
            db.session.refresh(run)
            if run.lease_until is None or run.lease_until < utcnow():
                break
            time.sleep(0.2)
    return run


APPROVES = re.compile(r"\b(approv|sign(s|ed)? off|authori[sz])", re.I)  # a move that gives the OK (Approve, Client signs off)


def decide(engine, approval, approved, by, comment=None, by_id=None, onward=True):
    """A person approved or rejected: the run carries on (when everyone it needs has answered), or goes the "if rejected"
    way and stops there. Approved (from To do, `onward`), the record then goes on as its Approve button would take it,
    if the rule didn't move it itself."""
    if approval.kind == "change":
        return decide_change(engine, approval, approved, by, comment)
    approval.status = "approved" if approved else "rejected"
    approval.decided_by, approval.comment, approval.decided_at = by, (comment or "").strip()[:1000] or None, utcnow()
    approval.decided_by_id = by_id
    _tell_raiser(engine, approval, approved, by, by_id, comment)
    record = engine.get(approval.record_id) if approved and approval.record_id and (approval.kind or "approval") == "approval" else None
    if record is not None:  # held to what it was approved at
        engine.mark_approved(record, by=by, by_id=by_id)
    run = _wait_for_worker(db.session.get(Run, approval.run_id))
    if run.status != "waiting_approval":
        db.session.commit()
        return run
    flow = (engine.tables.get(record.table) or {}).get("workflow") if record is not None else None
    stage = (record.data or {}).get(flow["field"]) if flow else None
    run = _carry_on(engine, run)
    if onward and stage and run.status == "done":
        _approved_onward(engine, record.id, stage, by)
    return run


def _tell_raiser(engine, approval, approved, by, by_id, comment):
    """Whoever raised the record (a request) hears it was approved or turned down, unless it was them who answered."""
    if (approval.kind or "approval") != "approval" or not approval.record_id:
        return
    record = engine.get(approval.record_id)
    maker = getattr(record, "created_by_id", None) if record is not None else None
    if not maker or maker == by_id:
        return
    t = engine.tables.get(record.table) or {}
    said = f" (“{comment.strip()[:200]}”)" if comment and comment.strip() else ""
    db.session.add(Notification(workspace_id=engine.ws.id, user_id=maker, kind="info", link=_link(record.table, record.id),
                                text=f"{by} {'approved' if approved else 'said no to'} your {schema.lc(t.get('singular') or 'record')} "
                                     f"“{engine.title(record)}”{said}"))


def _approved_onward(engine, record_id, stage, by):
    """An OK given from To do on a rule that doesn't move the record on afterwards (one built before the builder was
    made to add that step): it goes on as its one Approve button would, as pressing that button does."""
    engine.reset()
    record = engine.get(record_id)
    flow = (engine.tables.get(record.table) or {}).get("workflow") if record is not None else None
    if flow is None or (record.data or {}).get(flow["field"]) != stage or engine.waiting_for(record):
        return
    onward = {m["to"] for m in flow["transitions"] if (not m["from"] or stage in m["from"]) and m["to"] != stage
              and not schema.says_no(m["to"]) and APPROVES.search(f"{m.get('label') or ''} {m['to']}")}
    if len(onward) == 1:
        engine.system().update(record, {flow["field"]: onward.pop()}, by=by)


def complete(engine, task, by, values=None, comment=None):
    """A person finished a task: what it asked for is saved on the record (as them: their permissions and the record's
    rules apply), then the run carries on."""
    if task.fields and task.record_id:
        record = engine.get(task.record_id)
        if record is None:
            raise FieldErrors({"_": "The record this task is about was deleted."})
        wanted = {k: (values or {}).get(k) for k in task.fields}
        empty = [engine.field(record.table, k)["label"] for k, v in wanted.items() if v in (None, "", []) and engine.field(record.table, k)]
        if empty:
            raise FieldErrors({"_": f"Fill in {', '.join(empty)} to finish it."})
        engine.update(record, wanted, by=by)
    task.status, task.decided_by, task.decided_at = "done", by, utcnow()
    task.comment = (comment or "").strip()[:1000] or None
    run = _wait_for_worker(db.session.get(Run, task.run_id))
    if run.status != "waiting_approval":
        db.session.commit()
        return run
    return _carry_on(engine.system(), run)


def pass_on(engine, approval, to_user_id, by, note=None):
    """Hand an approval or a task to someone else (away, or better placed to decide)."""
    approval.history = (approval.history or []) + [{"when": utcnow().isoformat(), "what": "passed on", "by": by,
                                                     "note": (note or "").strip()[:300] or None}]
    approval.assignee_id, approval.role = to_user_id, None
    db.session.commit()
    record = engine.get(approval.record_id) if approval.record_id else None
    _tell(engine, approval, record, f"{by} passed this to you: {approval.message}" + (f" (“{note.strip()}”)" if note and note.strip() else ""))


def _last_slot(trigger, now):
    """The most recent moment the schedule was due, at or before now."""
    hour, minute = (int(x) for x in trigger.get("at", "08:00").split(":"))
    every = trigger.get("every", "day")
    if every == "hour":
        slot = now.replace(minute=minute, second=0, microsecond=0)
        return slot if slot <= now else slot - timedelta(hours=1)
    slot = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if every == "day":
        return slot if slot <= now else slot - timedelta(days=1)
    if every == "week":
        back = (now.weekday() - WEEKDAYS.index(trigger.get("weekday", "mon"))) % 7
        slot -= timedelta(days=back)
        return slot if slot <= now else slot - timedelta(days=7)
    slot = slot.replace(day=1)
    if slot > now:
        slot = (slot - timedelta(days=1)).replace(day=1)
    return slot


def _next_slot(trigger, now):
    """The schedule's next time after now (in the workspace's own time)."""
    last, every = _last_slot(trigger, now), trigger.get("every", "day")
    if every in ("hour", "day", "week"):
        return last + {"hour": timedelta(hours=1), "day": timedelta(days=1), "week": timedelta(days=7)}[every]
    return last.replace(year=last.year + (last.month == 12), month=last.month % 12 + 1)


LOOK_AGAIN = timedelta(days=1)  # the longest a workspace's schedules go without being looked at


def _take_slot(engine, key, slot):
    """Take this schedule's time: True for the one process whose row goes in, False if someone has taken it already
    (two schedulers for a moment, as one takes over from another, can't both send the Monday report)."""
    values = {"workspace_id": engine.ws.id, "automation_key": key, "slot": slot, "created_at": utcnow()}
    dialect = db.engine.dialect.name
    if dialect in ("postgresql", "sqlite"):
        if dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        else:
            from sqlalchemy.dialects.sqlite import insert
        table = ScheduleRun.__table__
        done = db.session.execute(insert(table).values(**values).on_conflict_do_nothing(
            index_elements=["workspace_id", "automation_key", "slot"]).returning(table.c.id))
        return done.first() is not None  # a row back: this one went in
    from sqlalchemy.exc import IntegrityError
    try:
        with db.session.begin_nested():
            db.session.add(ScheduleRun(**values))
        return True
    except IntegrityError:
        return False


def due(engine):
    """Start every schedule whose time has come since it last ran. A new schedule waits for its first time. Each time is
    taken first (_take_slot), so it runs once however many processes look. Then the workspace is told when its schedules
    are next due, so the scheduler doesn't look at it again before."""
    state = dict(engine.ws.state or {})
    last = dict(state.get("schedules") or {})
    started, upcoming = [], []
    for a in engine.bp.get("automations", []):
        if not a.get("enabled", True) or a["trigger"]["type"] != "schedule":
            continue
        slot = _last_slot(a["trigger"], engine.now)
        upcoming.append(_next_slot(a["trigger"], engine.now))
        seen = last.get(a["key"])
        if seen is None:
            last[a["key"]] = engine.now.isoformat(timespec="minutes")
            continue
        if datetime.fromisoformat(seen) >= slot:
            continue
        last[a["key"]] = engine.now.isoformat(timespec="minutes")
        if _take_slot(engine, a["key"], slot):
            started += run_schedule(engine, a, f"Scheduled: {describe_trigger(engine, a['trigger'])}")
    if last != (state.get("schedules") or {}):
        engine.ws.state = {**state, "schedules": last}
    if isinstance(engine.ws, Workspace):
        soonest = min(upcoming, default=None)
        later = utcnow() + LOOK_AGAIN
        if soonest is not None:
            soonest = soonest.replace(tzinfo=engine.now_tz).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
        engine.ws.next_due_at = min(soonest, later) if soonest is not None else later
    db.session.commit()
    return started


def run_schedule(engine, automation, trigger_text):
    table = automation["trigger"].get("table")
    if not table:
        return [start(engine, automation, None, trigger_text)]
    rows = [r for r in engine.rows(table) if engine.matches(r, automation["conditions"])]
    return [start(engine, automation, r, trigger_text) for r in rows[:500]]


# --- plain English --------------------------------------------------------------------------------------------------

def _article(word):
    return "an" if word[:1].lower() in "aeiou" else "a"


def describe_trigger(engine, trigger):
    t = engine.tables.get(trigger.get("table"))
    thing = t["singular"] if t else "record"
    kind = trigger["type"]
    if kind == "record_created":
        return f"When {_article(thing)} {thing} is added"
    if kind == "record_updated":
        return f"When {_article(thing)} {thing} is changed"
    if kind == "record_created_or_updated":
        return f"When {_article(thing)} {thing} is added or changed"
    if kind == "button":
        return f"When someone clicks “{trigger.get('label')}” on {_article(thing)} {thing}"
    if kind == "email_received":
        filters = [f"from an address containing “{trigger['from_contains']}”" if trigger.get("from_contains") else "",
                   f"with “{trigger['subject_contains']}” in the subject" if trigger.get("subject_contains") else "",
                   f"mentioning “{trigger['body_contains']}”" if trigger.get("body_contains") else "",
                   "with an attachment" if trigger.get("has_attachment") else ""]
        return "When an email arrives" + "".join(f" {x}" for x in filters if x)
    if kind == "file_added":
        return f"When a file is added to the OneDrive folder “{trigger.get('folder')}”"
    if kind == "webhook_received":
        return "When another system sends data to this rule's web address"
    every = trigger.get("every", "day")
    days = dict(zip(WEEKDAYS, ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]))
    at = _clock(trigger.get("at", "08:00"))
    when = {"hour": f"Every hour at :{trigger.get('at', '00:00')[3:]}", "day": f"Every day at {at}",
            "week": f"Every {days.get(trigger.get('weekday', 'mon'))} at {at}", "month": f"On the 1st of every month at {at}"}[every]
    return f"{when}, for each {thing.lower()}" if t else when


def _clock(at):
    """"08:00" → "8 am", "15:30" → "3:30 pm"."""
    try:
        h, m = (int(x) for x in str(at).split(":")[:2])
    except ValueError:
        return str(at)
    return f"{h % 12 or 12}{f':{m:02d}' if m else ''} {'am' if h < 12 else 'pm'}"


OP_WORDS = {"equals": "is", "not_equals": "is not", "contains": "contains", "is_empty": "is empty", "not_empty": "is filled in",
            "greater_than": "is more than", "less_than": "is less than", "at_least": "is at least", "at_most": "is at most",
            "one_of": "is one of", "before_today": "is before today", "after_today": "is after today",
            "within_next_days": "is within the next", "within_last_days": "is within the last", "more_than_days_ago": "is more than",
            "this_week": "is this week", "last_week": "is last week", "this_month": "is this month", "last_month": "is last month",
            "this_quarter": "is this quarter", "last_quarter": "is last quarter",
            "this_year": "is this year", "last_year": "is last year", "this_financial_year": "is this financial year",
            "last_financial_year": "is last financial year", "today": "is today", "tomorrow": "is tomorrow", "yesterday": "was yesterday",
            "next_week": "is next week", "next_month": "is next month", "changed": "changes", "changed_to": "changes to"}


def _path_label(engine, table, path):
    """record.client.name -> "Client’s name": each step's label, following link fields, as people say it (an
    engagement's "Engagement title" is just its title)."""
    labels, t = [], engine.tables.get(table)
    for part in path:
        f = schema.field(t, part) if t else None
        label = f["label"] if f is not None else part.replace("_", " ")
        if labels:
            prev = labels[-1].lower()
            label = label[len(prev) + 1:] if label.lower().startswith(prev + " ") else label
            label = label[:1].lower() + label[1:] if not label[:2].isupper() else label
        labels.append(label)
        t = engine.tables.get(f["table"]) if f is not None and f["type"] == "link" else None
    return "’s ".join(labels)


def expression_words(engine, table, source):
    """A calculation in words: "budget * 0.9" → "Budget × 0.9", "{{record.total}} - paid" → "Total − Paid"."""
    t = engine.tables.get(table)
    text = re.sub(r"\{\{\s*|\s*\}\}", "", str(source))

    def name(m):
        word = m.group(0)
        if word.startswith("record."):
            word = word[7:]
        for prefix, whose in (("item.", "each line's"), ("created.", "the new record's"), ("email.", "the email's"), ("file.", "the file's")):
            if word.startswith(prefix):  # a line's own quantity, inside a sum: never its code name
                return f"{whose} {word[len(prefix):].replace('_', ' ').replace('.', ' ')}"
        f = schema.field(t, word.split(".")[0]) if t else None
        return _path_label(engine, table, word.split(".")) if f else m.group(0).replace("_", " ")
    text = re.sub(r"(?<![\w.])(?!(?:add_days|add_months|workday|days_between|round|convert|today|now|iif|min|max|sum)\()[A-Za-z_][\w.]*", name, text)
    for _ in range(3):
        for pattern, words in FUNCTION_WORDS:
            text = re.sub(pattern, words, text)
    if re.search(r"\w\(", text):  # anything else is still worked out: say from what
        used = [m for m in re.findall(r"[A-Z][\w’ ]*[a-z]", text)]
        return f"worked out from {', '.join(dict.fromkeys(used))}" if used else "worked out"
    share = re.fullmatch(r"\s*(.+?)\s*\*\s*(0?\.\d+|1(?:\.0+)?)\s*", text) or re.fullmatch(r"\s*(0?\.\d+)\s*\*\s*(.+?)\s*", text)
    if share:  # "Budget * 0.9" is 90% of Budget
        label, number = (share.group(1), share.group(2)) if not share.group(1)[:1].isdigit() and share.group(1)[:1] != "." else (share.group(2), share.group(1))
        return f"{float(number) * 100:g}% of {label}"
    return text.replace("*", "×").replace(" / ", " ÷ ").replace(" - ", " − ")


FUNCTION_WORDS = [  # common calculations as people say them (applied innermost first)
    (r"today\(\)", "today"), (r"now\(\)", "now"),
    (r"add_days\(today, ([^(),]+)\)", r"\1 days from today"), (r"add_days\(([^(),]+), ([^(),]+)\)", r"\2 days after \1"),
    (r"add_months\(([^(),]+), ([^(),]+)\)", r"\2 months after \1"), (r"workday\(([^(),]+), ([^(),]+)\)", r"\2 working days after \1"),
    (r"days_between\(([^(),]+), ([^(),]+)\)", r"days from \1 to \2"), (r"round\(([^(),]+)(?:, \d+)?\)", r"\1"),
    (r"convert\(([^(),]+), [^(),]+\)", r"\1 in your currency")]


def looks_like_sum(engine, table, value):
    """A condition's value written as a calculation over the record's fields without braces ("budget * 0.9")."""
    t = engine.tables.get(table)
    if not t or not isinstance(value, str) or "{{" in value or not re.search(r"[-+*/]", value):
        return False
    names = re.findall(r"[A-Za-z_]\w*", value)
    return bool(names) and all(schema.field(t, n) for n in names) and re.fullmatch(r"[\w\s.+\-*/()]+", value) is not None


def humanize(engine, value, table=None):
    """A value with {{ }} as words. On its own: {{record}} → this Project, {{record.amount}} → its Amount.
    Inside text: "Invoice {{record.number}} is overdue" → “Invoice ‹Invoice number› is overdue”."""
    if value is None or value == []:
        return "empty"  # never “null” to someone who doesn't write code
    if isinstance(value, bool):
        return "yes" if value else "no"
    if not isinstance(value, str):
        return json.dumps(value) if not isinstance(value, (int, float)) else str(value)
    t = engine.tables.get(table)

    def name(inner):
        inner = inner.strip()
        if inner == "record":
            return None
        if inner in ("today()", "today"):
            return "today"
        if inner == "created":
            return "the new record"
        for prefix, word in (("created.", "the new record's"), ("item.", "each line's"), ("email.", "the email's"), ("file.", "the file's")):
            if inner.startswith(prefix) and re.fullmatch(r"[\w.]+", inner[len(prefix):]):
                return f"{word} {inner[len(prefix):].replace('_', ' ')}"
        dotted = re.fullmatch(r"(?:record\.)?([a-z_]\w*(?:\.[a-z_]\w*)*)", inner)
        if dotted and (inner.startswith("record.") or (t and schema.field(t, dotted.group(1).split(".")[0]))):
            return _path_label(engine, table, dotted.group(1).split("."))
        if inner in ("item",):
            return "each one"
        if re.search(r"[-+*/(]", inner):  # a sum or a function: in words
            return expression_words(engine, table, inner)
        return inner
    whole = re.fullmatch(r"\s*\{\{\s*(.+?)\s*\}\}\s*", value)
    if whole:
        label = name(whole.group(1))
        if label is None:
            return f"this {t['singular'] if t else 'record'}"
        if label == "today" or label.startswith(("the ", "each ")) or label == whole.group(1).strip() or not re.fullmatch(r"[\w’' ]+", label):
            return label
        return f"its {label[:1].lower() + label[1:] if not label[:2].isupper() else label}"
    if "{{" not in value:
        if looks_like_sum(engine, table, value):
            return expression_words(engine, table, value)
        if t and engine._names_field(table, value, None):  # another of its own fields: its name ("its reorder level")
            return f"its {schema.lc(schema.field(t, value.strip())['label'])}"
        return value if value.strip().lower() in ("today", "now") else f"“{value}”"
    def money(path):
        f, at = None, t
        for part in path.split("."):
            f = schema.field(at, part) if at else None
            at = engine.tables.get(f["table"]) if f is not None and f["type"] == "link" else None
        return f is not None and engine.kind(f) == "money"
    value = re.sub(r"[$€£¥]\s*(?=\{\{\s*(?:record\.)?([a-z_][\w.]*)\s*\}\})", lambda m: "" if money(m.group(1)) else m.group(0), value)  # the amount brings its own $
    text = expr.TEMPLATE.sub(lambda m: f"[{name(m.group(1)) or (t['singular'] if t else 'record')}]", value)
    return f"“{text}”"


def _whose(engine, table, source):
    """"its" or "its Product's": whose related records a step goes through."""
    if not source.get("of"):
        return "its"
    if source["of"] == "created":
        via = engine.field(source["table"], source["via"])
        made = engine.tables.get(via["table"]) if via else None
        return f"the new {schema.lc(made['singular'])}'s" if made else "the new record's"
    link = schema._path_field(engine.tables[table], source["of"], engine.tables) if table in engine.tables else None
    return f"its {link['label'] if link else source['of'].replace('_', ' ')}'s"


def formula_words(source):
    """A formula as people read it: count(client.invoices where status == "Unpaid") == 0 → the client's invoices
    where status is Unpaid number 0."""
    text = re.sub(r"\s*\band\s+id\s*!=\s*(this|record)\.id\b|\bid\s*!=\s*(this|record)\.id\s+and\s+", " ", expr.normalise(source))
    text = re.sub(r"[\"']", "", text)
    aggregate = {"sum": "the total", "avg": "the average", "average": "the average", "min": "the lowest", "max": "the highest"}
    text = re.sub(r"\b(sum|avg|average|min|max)\(\s*(\w+(?:\.\w+)*)\s*,\s*(\w+)\s*\)",  # sum(criteria, weight): the total weight of criteria
                  lambda m: f"{aggregate[m.group(1)]} {m.group(3)} of {m.group(2)}", text)
    text = re.sub(r"\bcount\(\s*(\w+(?:\.\w+)*)\s*\)", r"how many \1", text)
    for a, b in ((r"\bnot any\(where\(", "no "), (r"\bany\(where\(", "any "), (r"\bcount\(where\(", "how many "),
                 (r"\bsum\(where\(", "the total of "), (r"\bwhere\(", ""), (r"\bthis\.", "its "), (r"\brecord\.", "its "),
                 (r"\s*==\s*", " is "), (r"\s*!=\s*", " isn't "), (r"\s*>=\s*", " at least "), (r"\s*<=\s*", " at most "),
                 (r"\s*>\s*", " over "), (r"\s*<\s*", " under "), (r"days_since\((\w+)\)", r"days since \1"),
                 (r"\bTrue\b", "yes"), (r"\bFalse\b", "no"), (r"[()]", ""), (r",", " where"), (r"(?<=[A-Za-z_])\.(?=[A-Za-z_])", "'s "), (r"_", " ")):
        text = re.sub(a, b, text)
    return re.sub(r"\s+", " ", text).strip()


def describe_condition(engine, c, table):
    if "formula" in c:
        return formula_words(c["formula"])
    if "any" in c:
        return "either " + " or ".join(describe_condition(engine, x, table) for x in c["any"])
    if "all" in c:
        return "(" + " and ".join(describe_condition(engine, x, table) for x in c["all"]) + ")"
    label = _path_label(engine, table, c["field"].split(".")) if table else c["field"].replace("_", " ")
    words = OP_WORDS.get(c["op"], c["op"])
    if c["op"] in ("within_next_days", "within_last_days", "more_than_days_ago"):
        return f"{label} {words} {c.get('value')} days" + (" ago" if c["op"] == "more_than_days_ago" else "")
    if "value" in c:
        f = engine._at_path(None, c["field"], table)[0] if table else None
        if f is not None and engine.kind(f) == "percent" and not isinstance(c["value"], str) or (f is not None and engine.kind(f) == "percent" and str(c["value"]).strip().rstrip("%").replace(".", "", 1).isdigit()):
            return f"{label} {words} {engine.display(f, schema.as_fraction(c['value']))}"
        if f is not None and engine.kind(f) in ("money", "number", "percent") and expr.to_number(c["value"]) is not None \
                and not isinstance(c["value"], str) or f is not None and f["type"] == "money" and str(c["value"]).replace(".", "", 1).isdigit():
            value = engine.display(f, expr.to_number(c["value"]))  # $5,000.00, not 5000
        else:
            value = ", ".join(map(str, c["value"])) if isinstance(c["value"], list) else humanize(engine, c["value"], table)
        return f"{label} {words} {value}"
    return f"{label} {words}"


def describe_action(engine, automation, action):
    table = automation["trigger"].get("table")
    kind = action["type"]
    t = engine.tables.get(table) or {"singular": "record", "label": "records"}
    if kind == "delete_record":
        return f"Delete the {action['target'] if action.get('target') and action['target'] != 'created' else schema.lc(t['singular'])}"
    if kind == "wait":
        if action.get("until_met"):
            most = [f"{_number(action[k])} {k if action[k] != 1 else k[:-1]}" for k in ("days", "hours", "minutes") if action.get(k)]
            return f"Wait until {automations_words(engine, action['until_met'], table)}" + (f" (at most {' and '.join(most)})" if most else "")
        if action.get("until"):
            return f"Wait until its {(engine.field(table, action['until']) or {'label': action['until']})['label']}"
        parts = [f"{int(action[k]) if float(action[k]).is_integer() else action[k]} {k if action[k] != 1 else k[:-1]}" for k in ("days", "hours", "minutes") if action.get(k)]
        return "Wait " + " and ".join(parts)
    if kind == "update_each":
        s = engine.tables.get(action["source"]["table"]) or {"label": action["source"]["table"]}
        parts = [f"{(engine.field(action['source']['table'], k) or {'label': k})['label']} to {humanize(engine, v, table)}" for k, v in action["values"].items()]
        return f"On each of {_whose(engine, table, action['source'])} {schema.lc(s['label'])}, set " + ", ".join(parts)
    if kind == "assign":
        return f"Give {(engine.field(table, action['field']) or {'label': action['field']})['label']} to the {action['from']} with the fewest"
    if kind == "allocate":
        f = engine.field(table, action["field"]) or {"label": action["field"]}
        other = engine.tables.get(f.get("table")) or {"label": "its list"}
        n = int(action.get("count") or 1)
        among = automations_words(engine, action.get("where") or [], f.get("table")) if action.get("where") else ""
        free = f", free that {(engine.field(table, action['free_on']) or {'label': action['free_on']})['label'].lower()}" if action.get("free_on") else ""
        return (f"Fill {f['label']} with {'one' if n == 1 else n} of the {schema.lc(other['label'])}" + (f" where {among}" if among else "")
                + f"{free}, the one with the fewest so far first")
    if kind == "update_record":
        target_table, prefix = table, "Set"
        if action.get("target") == "created":
            prefix = "On the new record, set"
            target_table = next((a["table"] for a in schema.steps_of(automation["actions"]) if a.get("table") and a["type"] in ("create_record", "ai_create_record")), table)
        elif action.get("target"):
            links = schema.link_path(engine.tables.get(table), action["target"], engine.tables)
            target_table = links[-1]["table"] if links else table
            prefix = f"On its {_target_words(engine, table, action['target'])}, set"
        parts = [f"{(engine.field(target_table, k) or {'label': k})['label']} to {humanize(engine, v, table)}" for k, v in action["values"].items()]
        text = f"{prefix} " + ", ".join(parts)
    elif kind in ("create_record", "create_for_each"):
        t = engine.tables.get(action["table"])
        thing = t["singular"] if t else action["table"]
        mine = (engine.tables.get(table) or {}).get("singular", "record").lower()
        back = [k for k, v in action["values"].items() if isinstance(v, str) and v.strip() in ("{{record}}", "{{ record }}")]
        parts = [f"{(engine.field(action['table'], k) or {'label': k})['label']}: {_lower(humanize(engine, v, table))}"
                 for k, v in action["values"].items() if k not in back and str(v).strip() not in ("{{item}}", "{{ item }}")]
        if kind == "create_for_each" and action["source"].get("field"):
            f = engine.field(table, action["source"]["field"]) or {"label": action["source"]["field"]}
            which = f" where {automations_words(engine, action['where'], f['table'])}" if action.get("where") and f.get("table") else ""
            of = (engine.tables.get(f.get("table")) or {}).get("label", "").lower()
            text = f"Add {_article(thing)} {thing.lower()} for each of its {schema.lc(f['label'])}" + (f" ({of})" if of and of not in f["label"].lower() else "") + which
        elif kind == "create_for_each" and action["source"].get("all"):
            s = engine.tables.get(action["source"]["table"]) or {"singular": action["source"]["table"]}
            which = f" where {automations_words(engine, action['where'], action['source']['table'])}" if action.get("where") else ""
            text = f"For each {schema.lc(s['singular'])}{which}, add {_article(thing)} {thing.lower()}"
        elif kind == "create_for_each":
            s = engine.tables.get(action["source"]["table"])
            which = f" where {automations_words(engine, action['where'], action['source']['table'])}" if action.get("where") else ""
            nested = schema.nested_link(s, action["source"]) if s else None
            which = (f" (and its sub-assemblies' own, through {nested['label']})" if nested else "") + which
            text = (f"For each of {_whose(engine, table, action['source'])} {schema.lc(s['label']) if s else action['source']['table']}{which}, "
                    f"add {_article(thing)} {thing.lower()}")
        else:
            text = f"Add {_article(thing)} {thing.lower()}" + (f" for this {mine}" if back else "")
        text += (": " + " · ".join(parts)) if parts else ""
    elif kind == "notify":
        to = action.get("to", "everyone")
        who = {"everyone": "everyone", "owner": "the owner", "admin": "the admins", "editor": "the editors", "viewer": "everyone"}.get(to) \
            or _who(engine, table, to)
        text = f"Tell {who}: {humanize(engine, action['message'], table)}"
    elif kind == "send_sms":
        text = f"Text {humanize(engine, action['to'], table)}: {humanize(engine, action['message'], table)}"
    elif kind == "send_email":
        made = next((a["table"] for a in schema.steps_of(automation["actions"]) if a.get("table") and a["type"] in ("create_record", "ai_create_record")), None)
        docs = {d["key"]: d["label"] for d in (engine.tables.get(made) or {}).get("documents", [])}
        docs.update({d["key"]: d["label"] for d in (engine.tables.get(table) or {}).get("documents", [])})
        names = [f"the {docs[k]} PDF" if k in docs else (engine.field(table, k) or {"label": k})["label"] for k in action.get("attach", [])]
        attach = f", attaching {' and '.join(names)}" if names else ""
        text = f"Email {humanize(engine, action['to'], table)}: {humanize(engine, action['subject'], table)}{attach}"
    elif kind == "generate_document":
        of, suffix = table, ""
        if action.get("target") == "created":
            of = next((a["table"] for a in schema.steps_of(automation["actions"]) if a.get("table") and a["type"] in ("create_record", "ai_create_record")), table)
            suffix = f" of the new {(engine.tables.get(of) or {'singular': 'record'})['singular'].lower()}"
        elif action.get("target"):
            links = schema.link_path(engine.tables.get(table), action["target"], engine.tables)
            of = links[-1]["table"] if links else table
            suffix = f" of its {_target_words(engine, table, action['target'])}"
        t = engine.tables.get(of) or {}
        doc = next((d for d in t.get("documents", []) if d["key"] == action["document"]), {"label": action["document"]})
        into = f" into {(engine.field(of, action['field']) or {'label': action['field']})['label']}" if action.get("field") else ""
        text = f"Produce the {doc['label']} PDF{suffix}{into}"
    elif kind == "request_approval":
        people = [_who(engine, table, x) for x in action.get("approvers") or [action["approver"]]]
        if len(people) == 1:
            asked = people[0]
        else:
            asked = (("both " if len(people) == 2 else "all of ") if action.get("mode") == "all" else "any of ") + _and(people, "or" if action.get("mode") != "all" else "and")
        text = f"Ask {asked} to approve it, and wait for {'every' if action.get('mode') == 'all' and len(people) > 1 else 'their'} answer"
        text += _nudges(engine, action, table) + _apart_words(engine, action, table)
        if action.get("if_rejected"):
            text += "; if rejected: " + "; ".join(_lower(describe_action(engine, automation, x)) for x in action["if_rejected"]) + ", and stop"
    elif kind == "task":
        fields = [(engine.field(table, k) or {"label": k})["label"] for k in action.get("fields", [])]
        text = f"Ask {_who(engine, table, action['to'])} to {_lower(humanize(engine, action['message'], table).strip('“”'))}"
        text += (f" (they fill in {_and(fields)})" if fields else "") + ", and wait until it's done" + _nudges(engine, action, table) \
            + _apart_words(engine, action, table)
    elif kind == "copy_record":
        t = engine.tables.get(table) or {"singular": "record"}
        sets = [f"{(engine.field(table, k) or {'label': k})['label']} {humanize(engine, v, table)}" for k, v in action.get("values", {}).items()]
        text = f"Make a new copy of the {schema.lc(t['singular'])}" + (" with its lines" if action.get("lines", True) else "") \
            + (f" (its {(engine.field(table, action['link_back']) or {'label': action['link_back']})['label'].lower()} points back to this one)" if action.get("link_back") else "") \
            + (f", with {_and(sets)}" if sets else "")
    elif kind == "branch":
        ways = [f"if {_path_words(engine, path, table)}: " + ("; ".join(_lower(describe_action(engine, automation, x)) for x in path["steps"]) or "nothing more")
                for path in action["paths"]]
        if action.get("otherwise"):
            ways.append("otherwise: " + "; ".join(_lower(describe_action(engine, automation, x)) for x in action["otherwise"]))
        text = "Choose the way: " + ". ".join(ways)
    elif kind == "run_automation":
        other = next((a for a in engine.bp.get("automations", []) if a["key"] == action["automation"]), None)
        name = other["name"] if other else action["automation"]
        target = ""
        if action.get("target") == "created":
            target = " for the record made above"
        elif action.get("target"):
            target = f" for its {_target_words(engine, table, action['target'])}"
        text = f"Run “{name}”{target}"
    elif kind == "webhook":
        text = f"Send the details to {action['url']}"
    elif kind == "import_rows":
        f, into = engine.field(table, action["field"]) if table else None, engine.tables.get(action["table"])
        text = (f"Bring the rows of the spreadsheet in {f['label'] if f else action['field']} into "
                f"{into['label'] if into else action['table']}" + (f", updating those with the same {action['match_on'].replace('_', ' ')}"
                                                                   if action.get("match_on") else ""))
    elif kind == "ai_fill":
        f = engine.field(table, action["field"])
        text = f"Have the AI fill {f['label'] if f else action['field']}: “{schema.named(engine.bp, action['instructions'])}”"
    elif kind == "ai_extract":
        source = {"email": "the email", "file": "the file"}.get(action["from"]) or (engine.field(table, action["from"]) or {"label": action["from"]})["label"]
        text = f"Have the AI read {source} and fill in the details"
    elif kind == "ai_create_record":
        t = engine.tables.get(action["table"])
        thing = t["singular"] if t else action["table"]
        text = f"Have the AI create {_article(thing)} {thing} from it"
    elif kind == "ai_check":
        text = f"Have the AI check it: “{schema.named(engine.bp, action.get('instructions')) or 'that it looks right'}”"
    else:
        text = kind.replace("_", " ").capitalize()
    if kind in schema.AI_STEPS and action.get("manual"):
        text += f" — switched off: {_who(engine, table, action.get('reviewer', 'editor'))} does it instead"
    elif action.get("review") == "always":
        text += f" — {_who(engine, table, action.get('reviewer', 'editor'))} checks it first"
    elif action.get("review") == "when_unsure":
        text += f" — {_who(engine, table, action.get('reviewer', 'editor'))} checks it when the AI isn't sure"
    if action.get("only_if"):
        text += " — only if " + " and ".join(describe_condition(engine, c, table) for c in action["only_if"])
    return text


def describe(engine, automation):
    """{"when": ..., "only_if": [...], "then": [...], "flow": [...]} in plain words, for the automations page and the
    builder. "flow" is the steps as a tree: {"text", "ways": [{"label", "steps": [...]}]} for a branch's ways and an
    approval's "if rejected"."""
    table = automation["trigger"].get("table")
    when, rest = describe_trigger(engine, automation["trigger"]), list(automation["conditions"])
    t = engine.tables.get(table)
    moved = next((c for c in rest if c.get("op") == "changed_to" and automation["trigger"]["type"] in ("record_updated", "record_created_or_updated")
                  and "." not in c.get("field", "")), None)
    if t and moved is not None:  # "When a request is changed, if Status changes to Submitted" is "When a request is moved to Submitted"
        f = engine.field(table, moved["field"])
        stage = (t.get("workflow") or {}).get("field") == moved["field"] or (f or {}).get("key") in ("status", "stage")
        value = moved.get("value")
        shown = engine.display(f, value) if f is not None and value not in (None, "") else str(value)
        when = (f"When {_article(t['singular'])} {schema.lc(t['singular'])} is moved to {shown}" if stage
                else f"When {_article(t['singular'])} {schema.lc(t['singular'])}'s {(f or {'label': moved['field']})['label']} becomes “{shown}”")
        rest.remove(moved)
    rows = engine.rows(table) if t else []
    sample = next((r for r in reversed(rows[-50:]) if engine.matches(r, rest)), rows[-1] if rows else None)  # a real one to show
    return {"when": when, "sample": sample,
            "only_if": [describe_condition(engine, c, table) for c in rest],
            "then": [describe_action(engine, automation, a) for a in automation["actions"]],
            "flow": _flow(engine, automation, automation["actions"], table, sample)}


def _example(engine, action, sample):
    """What a step's message says for a real record (a timesheet's "Timesheet for Ann Lee week of 2 Nov 2026…"), so
    nobody has to read placeholders; None when there's no record to show it with."""
    said = action.get("message") if action["type"] in ("notify", "task", "request_approval") else action.get("subject") if action["type"] == "send_email" else None
    if sample is None or not isinstance(said, str) or "{{" not in said:
        return None
    try:
        return str(_render(said, RecordContext(engine, sample)))[:300]
    except Exception:  # noqa: BLE001 - an example that can't be worked out is left out, never shown wrong
        return None


def _flow(engine, automation, actions, table, sample=None):
    out = []
    for a in actions or []:
        if a["type"] == "branch":
            ways = [{"label": "If " + _path_words(engine, p, table), "steps": _flow(engine, automation, p["steps"], table, sample)} for p in a["paths"]]
            if a.get("otherwise"):
                ways.append({"label": "Otherwise", "steps": _flow(engine, automation, a["otherwise"], table, sample)})
            text = "Go one of these ways" + (" (only if " + automations_words(engine, a["only_if"], table) + ")" if a.get("only_if") else "")
            out.append({"text": text, "ways": ways})
        elif a["type"] == "request_approval" and a.get("if_rejected"):
            text = describe_action(engine, automation, {k: v for k, v in a.items() if k != "if_rejected"})
            out.append({"text": text, "example": _example(engine, a, sample),
                        "ways": [{"label": "If they say no (and then it stops)", "steps": _flow(engine, automation, a["if_rejected"], table, sample)}]})
        else:
            out.append({"text": describe_action(engine, automation, a), "ways": [], "example": _example(engine, a, sample)})
    return out


def _who(engine, table, who):
    """Who a step is for, as people say it: "the request's approver", "someone in Finance", "the person who asked for it"."""
    thing = (engine.tables.get(table) or {}).get("singular", "record").lower()
    if who in ROLE_WORDS:
        return ROLE_WORDS[who]
    if who in team_keys(engine):
        return f"someone in {team_name(engine, who)}"
    if who == "creator":
        return f"the person who added the {thing}"
    if who == "portal":
        return "their contact in the portal"
    if "." in who:
        return f"the {thing}'s " + "’s ".join((schema._path_field(engine.tables.get(table), ".".join(who.split(".")[:i + 1]), engine.tables)
                                                 or {"label": part})["label"].lower() for i, part in enumerate(who.split(".")))
    return f"the {thing}'s {(engine.field(table, who) or {'label': who})['label'].lower()}"


def _apart_words(engine, action, table):
    """"; never by whoever raised it", "; after confirming their sign-in": separation of duties and step-up, in words."""
    out = ""
    if action.get("not_by"):
        t = engine.tables.get(table) or {}
        said = []
        for who in action["not_by"]:
            if who == "creator":
                said.append("the person who asked for it")
            elif t.get("workflow") and str(who).lower() in [x.lower() for x in schema.stages(t)]:
                said.append(f"the person who moved it to {who}")
            else:
                said.append(_who(engine, table, who))
        out += f" (never {_and(said, 'or')})"
    if action.get("step_up"):
        out += " (they confirm it's them first)"
    return out


def _and(items, word="and"):
    items = list(items)
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + f" {word} " + items[-1]


def _lower(text):
    return text[:1].lower() + text[1:] if text else text


def _number(n):
    return int(n) if float(n).is_integer() else n


def _path_words(engine, path, table):
    said = automations_words(engine, path["when"], table)
    return f"{path['label']} ({said})" if path.get("label") else said


def _nudges(engine, action, table):
    text = f"; reminds after {_days(action['remind_after_days'])}" if action.get("remind_after_days") else ""
    if action.get("escalate_after_days"):
        text += f"; passes to {_who(engine, table, action['escalate_to'])} after {_days(action['escalate_after_days'])} without an answer"
    return text
