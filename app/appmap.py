"""The App map: the whole app as one picture, worked out from its design, never drawn by hand, so it can't disagree with
what the app does.

A process is a chain of lists that work hands on: a quote that becomes a job, a job that's invoiced, an invoice that's
paid. The hand-offs are the rules that make (or move) a record of another list, and the links between lists that each have
stages of their own (an invoice raised from its job). Lists nothing hands work to or from, and that have no stages of
their own (customers, products), are what the processes look things up in: the map shows them apart, as reference lists.

Each list on the map carries what the layers show: its stages and how many records are at each (Live), who moves them on
and who approves (People), where the AI fills things in or acts (AI), and what the design checks found wrong with it.
"""
from .blueprint import schema

MAKES = ("create_record", "create_for_each", "copy_record", "ai_create_record")  # a step that makes a record of a list
ENTRIES = {"email_received": ("mail", "Emails"), "webhook_received": ("plug", "Another system"), "file_added": ("file", "Files dropped in")}
STATUS = ("status", "stage", "state")


def _status(t):
    """A list's stage field: its workflow's, or a plain Status choice with a few steps to it."""
    if t.get("workflow"):
        return schema.field(t, t["workflow"]["field"])
    return next((f for f in t["fields"] if f["type"] == "select" and any(w in f["key"] for w in STATUS) and len(f.get("options") or []) >= 3), None)


def _stages(t):
    f = _status(t)
    return schema.stages(t) if t.get("workflow") else list((f or {}).get("options") or [])


def _at_stage(a, t):
    """The stage a rule acts at (“when a job changes to Done”), or None."""
    f = _status(t) if t else None
    if f is None:
        return None
    return next((c.get("value") for c in a.get("conditions") or [] if isinstance(c, dict) and c.get("field") == f["key"]
                 and c.get("op") in ("changed_to", "equals") and isinstance(c.get("value"), str)), None)


def _ai(a):
    return [s for s in schema.steps_of(a["actions"]) if s.get("type") in schema.AI_STEPS]


def _asks(a):
    return any(s.get("type") == "request_approval" for s in schema.steps_of(a["actions"]))


def handoffs(bp, tables):
    """Work handed from one list to another: by a rule (or a rule's button) making a record of the other, or setting a
    linked record's stage (a payment marking its invoice Paid). Lines a rule makes for its own record aren't hand-offs."""
    out = []
    for a in bp.get("automations", []):
        src = tables.get(a["trigger"].get("table") or "")
        if src is None:
            continue
        at, ai, asks = _at_stage(a, src), bool(_ai(a)), _asks(a)
        for s in schema.steps_of(a["actions"]):
            to = None
            if s.get("type") in MAKES and s.get("table") in tables and s["table"] != src["key"] and not tables[s["table"]].get("embedded"):
                to, how = s["table"], "makes"
            elif s.get("type") == "update_record" and s.get("target"):
                link = schema.field(src, s["target"])
                if link and link["type"] == "link" and link.get("table") in tables and link["table"] != src["key"]:
                    to, how = link["table"], "updates"
            if to and not any(h["rule"] == a["key"] and h["to"] == to for h in out):
                out.append({"from": src["key"], "at": at, "to": to, "how": how, "rule": a["key"], "label": a["name"],
                            "on": a.get("enabled", True), "button": a["trigger"]["type"] == "button", "ai": ai, "asks": asks,
                            "event": a["trigger"]["type"], "press": a["trigger"].get("label") or "", "every": a["trigger"].get("every") or "",
                            "sets": _sets(tables[to], s) if how == "updates" else [], "moves_to": _moves_to(a, src),
                            "step_if": s.get("only_if") or [],
                            "tells": [x.get("to") or "everyone" for x in schema.steps_of(a["actions"]) if x.get("type") == "notify"]})
    return out


def _moves_to(a, t):
    """The stage a rule moves its own record on to (a job invoiced: “Invoiced”), or None."""
    f = _status(t)
    for s in schema.steps_of(a["actions"]) if f else []:
        v = (s.get("values") or {}).get(f["key"]) if s.get("type") == "update_record" and not s.get("target") else None
        if isinstance(v, str) and "{{" not in v:
            return v
    return None


def _sets(t, step):
    """What a step sets on the record it updates, as people would say it (“Status” to “Paid”): only plain values."""
    out = []
    for k, v in (step.get("values") or {}).items():
        f = schema.field(t, k)
        if f and isinstance(v, (str, int, float)) and not isinstance(v, bool) and "{{" not in str(v):
            out.append([f["label"], str(v)])
    return out


def entries(bp, tables):
    """Where work comes in from outside: a public form, an email, another system, a portal."""
    out = {}
    for t in tables.values():
        if t.get("public_form"):
            out.setdefault(t["key"], []).append({"icon": "globe", "label": "A form on your website"})
        if (t.get("portal") or {}).get("can_add"):  # one people only see there isn't where work comes in
            out.setdefault(t["key"], []).append({"icon": "users", "label": "Your customers' portal"})
    for a in bp.get("automations", []):
        kind = a["trigger"]["type"]
        if kind not in ENTRIES:
            continue
        made = [s.get("table") for s in schema.steps_of(a["actions"]) if s.get("type") in MAKES] + [a["trigger"].get("table")]
        for key in dict.fromkeys(k for k in made if k in tables):
            icon, label = ENTRIES[kind]
            if not any(e["label"] == label for e in out.get(key, [])):
                out.setdefault(key, []).append({"icon": icon, "label": label, "rule": a["key"]})
    return out


def processes(bp, names=None):
    """The processes: chains of lists that work hands on, in the order work goes through them, each named by its first
    and last list (or as the owner renamed it). Returns (processes, reference lists)."""
    tables = {t["key"]: t for t in bp.get("tables", []) if not t.get("embedded") and not t.get("combines")}
    hands, comes = handoffs(bp, tables), entries(bp, tables)
    linked_by = {k: {t["key"] for t in tables.values() if t["key"] != k and any(f["type"] == "link" and f.get("table") == k for f in t["fields"])}
                 for k in tables}
    # a list with a plain Status that many others look things up in (customers: Lead, Active, Inactive) is reference,
    # not a step of their work; one with stages of its own, or that work is handed to or from, is a step
    # a list with no stages that only has work handed to it, from two or more lists (stock movements, from purchase orders
    # received and orders delivered), is a ledger they each add to: kept apart, so it doesn't join their work into one
    sources = {k: {h["from"] for h in hands if h["to"] == k and h["how"] == "makes"} for k in tables}
    ledgers = {k for k, t in tables.items() if not _stages(t) and len(sources[k]) >= 2 and not any(h["from"] == k for h in hands)}
    moving = ({k for k, t in tables.items() if t.get("workflow") or (_stages(t) and len(linked_by[k]) < 2)}
              | {h["from"] for h in hands} | {h["to"] for h in hands} | set(comes)) - ledgers
    # a payment marking its invoice Paid comes after the invoice: work goes from the record it updates to it
    edges = [(h["from"], h["to"]) if h["how"] == "makes" else (h["to"], h["from"]) for h in hands
             if h["from"] in moving and h["to"] in moving]
    for t in tables.values():  # an invoice raised from its job: both have stages, so it's the job's work going on
        if t["key"] not in moving:
            continue
        for f in t["fields"]:
            if f["type"] == "link" and f.get("table") in moving and f["table"] != t["key"] and _stages(t) and _stages(tables[f["table"]]):
                edges.append((f["table"], t["key"]))
    groups, seen = [], set()
    for key in [k for k in tables if k in moving]:  # joined up by any hand-off or link, whichever way it goes
        if key in seen:
            continue
        group, todo = [], [key]
        while todo:
            k = todo.pop()
            if k in seen:
                continue
            seen.add(k)
            group.append(k)
            todo += [b for a, b in edges if a == k] + [a for a, b in edges if b == k]
        groups.append(_in_order(group, edges, list(tables)))
    names = names or {}
    out = []
    for group in groups:
        first, last = tables[group[0]], tables[group[-1]]
        key = group[0]
        name = names.get(key) or (f"{first['label']} to {last['label'].lower()}" if len(group) > 1 else first["label"])
        out.append({"key": key, "name": name, "named": key in names, "lists": group,
                    "handoffs": [h for h in hands if h["from"] in group or h["to"] in group],
                    "links": [{"from": a, "to": b} for a, b in edges if a in group and b in group
                              and not any(h["from"] == a and h["to"] == b for h in hands)]})
    reference = [k for k in tables if k not in moving]
    return out, reference


def _in_order(group, edges, order):
    """The lists of a process as work goes through them: each after those that hand it work (quotes, then jobs, then
    invoices), ties as the design lists them."""
    inside = set(group)
    before = {k: {a for a, b in edges if b == k and a in inside and a != k} for k in group}
    out = []
    while len(out) < len(group):
        ready = [k for k in group if k not in out and not (before[k] - set(out))]
        if not ready:  # work going round (a refund back to its order): the rest as the design lists them
            ready = [k for k in group if k not in out]
        out.append(min(ready, key=order.index))
    return out


def build(engine, problems=(), names=None):
    """The whole map for this person: the processes, each list's stages and counts, who does what, where the AI acts,
    the reference lists, and what's wrong where. `problems` are the design checks' issues ({"plain", "where"})."""
    bp = engine.visible_blueprint()
    seen = {t["key"]: t for t in bp.get("tables", [])}
    rules = [a for a in engine.bp.get("automations", []) if not a["trigger"].get("table") or a["trigger"]["table"] in seen]
    bp = {**bp, "automations": rules}
    found, reference = processes(bp, names)
    counts = engine.counts()
    waiting = _waiting(engine)
    wrong = {}
    for p in problems:  # "bad": the checks found it wrong; otherwise it may be right for this business, worth a look
        wrong.setdefault(p.get("where") or "", []).append({"text": p["plain"], "bad": p.get("level") == "fix"})
    comes = entries(bp, seen)
    stopped = _stopped(engine)
    only_if = {a["key"]: _only_if(engine, a) for a in rules}  # what else must be true: “its balance is at most $0.00”

    def card(key):
        t = seen[key]
        f = _status(t)
        names_ = _stages(t)
        by_stage = {}
        if f:
            for r in engine.rows(key):
                v = (r.data or {}).get(f["key"])
                if v not in (None, ""):
                    by_stage[v] = by_stage.get(v, 0) + 1
        mine = [a for a in rules if a["trigger"].get("table") == key]
        moves = (t.get("workflow") or {}).get("transitions", [])
        approving = {m["to"] for m in moves if _approves(m)}

        def by_hand(m):  # a button people press to move one on, and who may
            return {"label": m.get("label") or f"Move to {m['to']}", "to": m["to"],
                    "who": [cap(_who(engine, key, w)) for w in m.get("who") or []]}
        people = sorted({cap(_who(engine, key, w)) for m in moves for w in m.get("who") or []}
                        | {cap(_who(engine, key, s.get("approver") or s.get("assignee") or "admin"))
                           for a in mine for s in schema.steps_of(a["actions"]) if s.get("type") in ("request_approval", "task")})
        ai_fields = [g["label"] for g in t["fields"] if g.get("ai")]
        ai_steps = [{"rule": a["key"], "name": a["name"], "checked": s.get("review", "never") != "never"} for a in mine for s in _ai(a)]
        return {"key": key, "label": t["label"], "singular": t["singular"], "icon": t.get("icon") or "table", "count": counts.get(key, 0),
                "workflow": bool(t.get("workflow")), "field": f["key"] if f else None, "field_label": f["label"] if f else None,
                "stages": [{"name": n, "count": by_stage.get(n, 0), "approves": n in approving or any(_asks(a) and _at_stage(a, t) == n for a in mine),
                            "ai": any(_ai(a) and _at_stage(a, t) == n for a in mine),
                            "rules": [a["key"] for a in mine if _at_stage(a, t) == n],
                            "moves": [by_hand(m) for m in moves if n in (m.get("from") or [])]} for n in names_],
                "anytime": [by_hand(m) for m in moves if not m.get("from")],
                "rules": [{"key": a["key"], "name": a["name"], "on": a.get("enabled", True), "at": _at_stage(a, t), "ai": bool(_ai(a)),
                           "asks": _asks(a), "stopped": stopped.get(a["key"], 0), "moves_to": _moves_to(a, t), "only_if": only_if.get(a["key"], []),
                           **_started(a)} for a in mine],
                "seen_in_portal": bool(t.get("portal")) and not (t.get("portal") or {}).get("can_add"),
                "flows": [{"key": fl["key"], "label": fl["label"]} for fl in bp.get("flows") or [] if fl["table"] == key],
                "people": people, "ai": {"fields": ai_fields, "steps": ai_steps}, "waiting": waiting.get(key, 0),
                "entries": comes.get(key, []), "problems": wrong.get(key, [])}

    shown = []
    for p in found:
        lists = [card(k) for k in p["lists"]]
        shown.append({**p, "cards": lists, "count": sum(c["count"] for c in lists), "problems": sum(len(c["problems"]) for c in lists),
                      "ai": sum(len(c["ai"]["steps"]) + len(c["ai"]["fields"]) for c in lists),
                      "handoffs": [{**h, "from_label": seen[h["from"]]["label"], "to_label": seen[h["to"]]["label"],
                                    "only_if": only_if.get(h["rule"], []) + _step_if(engine, h),
                                    "tells": list(dict.fromkeys(_who(engine, h["from"], w) for w in h.get("tells") or []))} for h in p["handoffs"]]})
    on_map = {c["key"] for p in shown for c in p["cards"]} | set(reference)  # a rule on a worked-out list (stock levels) is shown apart
    return {"processes": shown,
            "reference": [{**card(k), "used_by": sorted({t["label"] for t in seen.values() if t["key"] != k and not t.get("embedded")
                                                         and any(f["type"] == "link" and f.get("table") == k for f in t["fields"])})}
                          for k in reference],
            "elsewhere": [{"key": a["key"], "name": a["name"], "on": a.get("enabled", True), "at": None, "stopped": stopped.get(a["key"], 0),
                           **_started(a)} for a in rules if a["trigger"].get("table") not in on_map],
            "problems": wrong.get("", [])}


def _only_if(engine, a):
    """What else must be true for a rule to act, in words: not the change that starts it, nor the stage it acts at
    (said already), but “Due is before today”."""
    from .engine import automations
    table = engine.tables.get(a["trigger"].get("table") or "")
    f = _status(table) if table else None
    out = []
    for c in a.get("conditions") or []:
        if not isinstance(c, dict) or c.get("op") in ("changed", "changed_to") or (f and c.get("field") == f["key"] and c.get("op") == "equals"):
            continue
        try:
            out.append(automations.describe_condition(engine, c, table["key"] if table else None))
        except Exception:  # noqa: BLE001 - said without it rather than not drawn
            pass
    return out


def _step_if(engine, h):
    """The step's own “only if” (a payment marks its invoice paid only when nothing's left owing)."""
    from .engine import automations
    if not h.get("step_if"):
        return []
    try:
        return [automations.automations_words(engine, h["step_if"], h["from"])]
    except Exception:  # noqa: BLE001 - said without it rather than not drawn
        return []


def _started(a):
    """What starts a rule, as the map says it: a button people press (by hand), a schedule, or a change to a record."""
    t = a["trigger"]
    return {"event": t["type"], "press": t.get("label") or "" if t["type"] == "button" else "", "every": t.get("every") or ""}


def cap(s):
    return s[:1].upper() + s[1:]


def a(word):
    return f"{'an' if word[:1].lower() in 'aeiou' else 'a'} {word}"


def _stopped(engine, days=14):
    """How many times each rule stopped with a problem lately: a job stuck at Signed off, its invoice never made."""
    from datetime import timedelta
    from .extensions import db
    from .models import Run, utcnow
    since = utcnow() - timedelta(days=days)
    rows = db.session.query(Run.automation, db.func.count(Run.id)).filter(
        Run.workspace_id == engine.ws.id, Run.status == "failed", Run.created_at >= since).group_by(Run.automation).all()
    return {key: n for key, n in rows if key}


def _approves(move):
    from .engine.automations import APPROVES
    return bool(APPROVES.search(str(move.get("label") or "")) or APPROVES.search(str(move.get("to") or "")))


def _who(engine, table, who):
    from .engine import automations
    try:
        return automations._who(engine, table, who)
    except Exception:  # noqa: BLE001 - who it is, as the design says it, if it can't be put in words
        return str(who)


def _waiting(engine):
    """How many approvals and tasks are waiting on each list's records."""
    from .extensions import db
    from .models import Approval
    rows = db.session.query(Approval.table, db.func.count(Approval.id)).filter(
        Approval.workspace_id == engine.ws.id, Approval.status == "pending").group_by(Approval.table).all()
    return {table: n for table, n in rows if table}
