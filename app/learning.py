"""The analyst keeps learning after the ERP is built. It watches how the business really uses it (what people fill in
and skip, what they type again and again, what they do by hand after something changes, what sits stuck, which
automations fail, what they keep asking the assistant, what kind of email keeps arriving) and turns what it sees into
improvements. Every improvement carries its evidence, and automations are replayed against the business's own history
before they're offered ("when a job became Done, someone created its invoice by hand 16 of 18 times; this would have
done exactly that"). Admins apply one with a click (a new design version, undoable) or dismiss it (it isn't suggested
again).

`learn(workspace)` runs the detectors and, once a week, the analyst's own review; the worker runs it daily.
"""
import json
import logging
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from . import ai, jobs
from .blueprint import ops, schema
from .blueprint.expr import to_date
from .engine.records import Engine
from .extensions import db
from .models import Chunk, Message, Record, RecordChange, Run, Suggestion, Version, Workspace, utcnow

log = logging.getLogger("learning")
ENOUGH = 20  # records needed before saying anything about how a field is used
FINAL = ("done", "closed", "paid", "complete", "completed", "cancel", "cancelled", "canceled", "lost", "won", "delivered",
         "archived", "finished", "rejected", "declined", "resolved", "void", "inactive", "former", "converted", "accepted")
REVIEW_EVERY = timedelta(days=7)


class Finding:
    def __init__(self, kind, signature, title, why, evidence, ops_, proof=None):
        self.kind, self.signature, self.title, self.why, self.evidence, self.ops, self.proof = kind, signature, title, why, evidence, ops_, proof


# --- what happened ---------------------------------------------------------------------------------------------------

class History:
    """The workspace's recent record changes, with which table each record is in and who the people are (as opposed
    to automations, imports, the AI or the API)."""

    def __init__(self, engine, days=120):
        self.engine = engine
        since = utcnow() - timedelta(days=days)
        self.changes = (RecordChange.query.filter(RecordChange.workspace_id == engine.ws.id, RecordChange.created_at >= since)
                        .order_by(RecordChange.id).all())
        self.table_of = dict(db.session.query(Record.id, Record.table).filter(Record.workspace_id == engine.ws.id))
        self.people = set(engine.members().values())
        self.versions = Version.query.filter_by(workspace_id=engine.ws.id).order_by(Version.number).all()

    def by_person(self, change):
        return change.by in self.people

    def born(self, table, key):
        """When a field first appeared in the design (records made before can't be expected to have it)."""
        for v in self.versions:
            t = schema.table(v.blueprint or {}, table)
            if t and schema.field(t, key):
                return v.created_at
        return None


def _rows(engine, t):
    """The records the nightly look reads: not sealed ones (their bids aren't for anyone's eyes yet, the AI's included)."""
    return [r for r in engine.rows(t["key"]) if not engine.sealed_from_me(r)]


def _filled(value):
    return value not in (None, "", [], False)


def _plural(n, word, words=None):
    return f"{n} {word if n == 1 else (words or word + 's')}"


def _has_automation(bp, table, field=None, value=None, creates=None, sets=None):
    for a in bp.get("automations", []):
        trig = a["trigger"]
        if trig.get("table") != table or trig["type"] not in ("record_updated", "record_created_or_updated"):
            continue
        cond = next((c for c in schema.condition_leaves(a["conditions"]) if c["field"] == field), None) if field else True
        if not cond or (value is not None and str(cond.get("value", "")).lower() != str(value).lower() and cond["op"] == "changed_to"):
            continue
        for act in schema.steps_of(a["actions"]):
            if creates and act["type"] in ("create_record", "ai_create_record") and act.get("table") == creates:
                return True
            if sets and act["type"] == "update_record" and not act.get("target") and sets in act["values"]:
                return True
    return False


# --- detectors -----------------------------------------------------------------------------------------------------

def unused_fields(engine, history):
    """Fields almost nobody fills in: hiding them makes every form shorter (the values are kept)."""
    out = []
    for t in engine.tables.values():
        for f in t["fields"]:
            if f["type"] not in schema.STORED or f["type"] in ("autonumber", "checkbox") or f.get("required") or f.get("ai") \
                    or f.get("readonly") or f["key"] == t.get("title_field") or f.get("show_if"):
                continue
            born = history.born(t["key"], f["key"])
            rows = [r for r in _rows(engine, t) if born is None or r.created_at >= born]
            if len(rows) < ENOUGH or any(f["key"] in (s.get("fields") or []) for s in t.get("sections", []) if s.get("label", "").lower().startswith("more")):
                continue
            filled = sum(1 for r in rows if _filled((r.data or {}).get(f["key"])))
            if filled <= max(1, len(rows) * 0.03):
                out.append(Finding(
                    "unused_field", f"unused:{t['key']}.{f['key']}",
                    f"Take “{f['label']}” off the {schema.lc(t['singular'])} form",
                    f"It's filled in on {filled} of the {len(rows)} {schema.lc(t['label'])} added since it was made, so it's only "
                    f"making the form longer. Its values are kept and it comes back if you undo.",
                    [f"Filled in on {filled} of {len(rows)} {schema.lc(t['label'])}"],
                    [{"op": "remove_field", "table": t["key"], "key": f["key"]}]))
    return out


def text_to_choices(engine, history):
    """A text field where people keep typing the same few things: a choice list is quicker and keeps spelling consistent."""
    out = []
    for t in engine.tables.values():
        for f in t["fields"]:
            if f["type"] != "text" or f.get("unique") or f.get("pattern") or f.get("sensitive") or f["key"] == t.get("title_field"):
                continue
            values = [str((r.data or {}).get(f["key"])).strip() for r in _rows(engine, t) if _filled((r.data or {}).get(f["key"]))]
            if len(values) < ENOUGH or sum(len(v) for v in values) / len(values) > 30:
                continue
            spellings = defaultdict(Counter)
            for v in values:
                spellings[v.lower()][v] += 1
            counts = Counter({k: sum(c.values()) for k, c in spellings.items()})
            if not 2 <= len(counts) <= 12:
                continue
            options = [spellings[k].most_common(1)[0][0] for k, _ in counts.most_common()]
            out.append(Finding(
                "text_to_select", f"choices:{t['key']}.{f['key']}",
                f"Make “{f['label']}” a choice list",
                f"People have typed it {len(values)} times but only ever as {len(options)} different things"
                + (f" (sometimes spelt differently: {', '.join(sorted({v for c in spellings.values() for v in c if len(c) > 1})[:4])})"
                   if any(len(c) > 1 for c in spellings.values()) else "")
                + ". Picking from a list is quicker and keeps reports tidy.",
                [f"{o}: {counts[o.lower()]} times" for o in options],
                [{"op": "update_field", "table": t["key"], "key": f["key"], "changes": {"type": "select", "options": options}}],
                {"summary": f"These {len(options)} choices cover all {len(values)} values typed so far.", "fired": len(values), "matched": len(values)}))
    return out


def defaults(engine, history):
    """A field people set to the same thing on almost every new record: start it there."""
    out = []
    starts = defaultdict(lambda: defaultdict(Counter))  # table -> field -> starting value -> count
    made = Counter()
    for c in history.changes:
        if c.kind != "created" or not history.by_person(c):
            continue
        table = history.table_of.get(c.record_id)
        t = engine.tables.get(table)
        if t is None or t.get("embedded"):
            continue
        made[table] += 1
        for key, pair in (c.changes or {}).items():
            f = schema.field(t, key)
            if f and f["type"] in ("select", "user", "checkbox") and f.get("default") in (None, "") and _filled(pair[1]):
                starts[table][key][json.dumps(pair[1])] += 1
    for table, fields in starts.items():
        t = engine.tables[table]
        for key, counter in fields.items():
            value, n = counter.most_common(1)[0]
            if made[table] >= 15 and n / made[table] >= 0.8:
                f = schema.field(t, key)
                shown = engine.display(f, json.loads(value))
                out.append(Finding(
                    "default_value", f"default:{table}.{key}={value}",
                    f"Start {f['label']} at “{shown}” on new {schema.lc(t['label'])}",
                    f"People chose “{shown}” on {n} of the {made[table]} {schema.lc(t['label'])} they added. Starting there saves "
                    f"a click every time; it can still be changed.",
                    [f"“{shown}” on {n} of {made[table]} new {schema.lc(t['label'])}"],
                    [{"op": "update_field", "table": table, "key": key, "changes": {"default": json.loads(value)}}],
                    {"summary": f"Right for {n} of the last {made[table]} {schema.lc(t['label'])} ({round(100 * n / made[table])}%).",
                     "fired": made[table], "matched": n}))
    return out


def follow_ups(engine, history):
    """What people do by hand after something changes: when a job becomes Done they create its invoice; when an invoice
    becomes Paid they fill in the paid date. Each is an automation waiting to happen, proved on what they actually did."""
    out = []
    events = []  # (change, table, field, new value) for a person moving a choice field
    for c in history.changes:
        if c.kind != "updated" or not history.by_person(c):
            continue
        t = engine.tables.get(history.table_of.get(c.record_id))
        if t is None or t.get("embedded"):
            continue
        for key, pair in (c.changes or {}).items():
            f = schema.field(t, key)
            if f and f["type"] == "select" and _filled(pair[1]):
                events.append((c, t, f, pair[1]))
    totals = Counter((t["key"], f["key"], v) for _, t, f, v in events)
    creations = [c for c in history.changes if c.kind == "created" and history.by_person(c)]
    later_updates = [c for c in history.changes if c.kind == "updated" and history.by_person(c)]
    made = defaultdict(list)   # (table, field, value, other table, link) -> [(event change, created change)]
    filled = defaultdict(list)  # (table, field, value, other field) -> [new values (or "today")]
    for c, t, f, v in events:
        for other in creations:
            if other.created_at < c.created_at or other.created_at - c.created_at > timedelta(minutes=30) or other.by != c.by:
                continue
            o = engine.tables.get(history.table_of.get(other.record_id))
            if o is None or o["key"] == t["key"] or o.get("embedded"):
                continue
            link = next((x for x in o["fields"] if x["type"] == "link" and x["table"] == t["key"]
                         and str((other.changes or {}).get(x["key"], [None, None])[1]) == str(c.record_id)), None)
            if link:
                made[(t["key"], f["key"], v, o["key"], link["key"])].append((c, other))
        for other in later_updates:
            if other.record_id != c.record_id or other.id == c.id or other.created_at < c.created_at \
                    or other.created_at - c.created_at > timedelta(minutes=15) or other.by != c.by:
                continue
            for key, pair in (other.changes or {}).items():
                g = schema.field(t, key)
                if key == f["key"] or g is None or not _filled(pair[1]):
                    continue
                on = c.created_at.replace(tzinfo=timezone.utc).astimezone(engine.now_tz).date()  # the business's day, not UTC's
                value = "{{today()}}" if g["type"] in schema.DATES and to_date(pair[1]) == on else pair[1]
                filled[(t["key"], f["key"], v, key)].append(value)
    bp = engine.bp
    for (table, key, value, other, link), pairs in made.items():
        total = totals[(table, key, value)]
        hits = len({c.record_id for c, _ in pairs})
        if hits < 3 or hits / total < 0.6 or _has_automation(bp, table, key, value, creates=other):
            continue
        t, o = engine.tables[table], engine.tables[other]
        values = {link: "{{record}}"}
        for x in o["fields"]:  # what people copied from the record, or always typed: learnt from what they did
            if x["key"] == link or x["type"] not in schema.STORED or x["type"] == "autonumber":
                continue
            typed = [(created.changes or {}).get(x["key"], [None, None])[1] for _, created in pairs]
            same = schema.field(t, x["key"])
            if same and same["type"] == x["type"]:
                copied = sum(1 for (c, _), v in zip(pairs, typed)
                             if v not in (None, "") and str(v) == str(((engine.get(c.record_id) or Record(data={})).data or {}).get(x["key"])))
                if copied / len(pairs) >= 0.8:
                    values[x["key"]] = f"{{{{record.{x['key']}}}}}"
                    continue
            common, n = Counter(json.dumps(v) for v in typed).most_common(1)[0]
            if common != "null" and n / len(pairs) >= 0.8:
                values[x["key"]] = json.loads(common)
        f = schema.field(t, key)
        automation = {"key": schema.slug(f"{t['singular']} {value} makes {o['singular']}")[:40], "name": f"{t['singular']} {value.lower()} → new {schema.lc(o['singular'])}",
                      "trigger": {"type": "record_updated", "table": table}, "conditions": [{"field": key, "op": "changed_to", "value": value}],
                      "actions": [{"type": "create_record", "table": other, "values": values}]}
        proof = backtest(engine, automation, history)
        out.append(Finding(
            "follow_up", f"makes:{table}.{key}={value}->{other}",
            f"Create the {schema.lc(o['singular'])} automatically when a {schema.lc(t['singular'])} becomes {value}",
            f"When a {schema.lc(t['singular'])} became {value}, someone created its {schema.lc(o['singular'])} by hand within half an hour "
            f"{hits} of {total} times. This does it for them, filled in the same way.",
            [f"{hits} of {total} times by hand", "Usually by " + ", ".join(sorted({c.by for c, _ in pairs})[:3])],
            [{"op": "add_automation", "automation": automation}], proof))
    for (table, key, value, other_key), seen in filled.items():
        total = totals[(table, key, value)]
        common, n = Counter(json.dumps(s) for s in seen).most_common(1)[0]
        if n < 3 or n / total < 0.6 or _has_automation(bp, table, key, value, sets=other_key):
            continue
        t = engine.tables[table]
        f, g = schema.field(t, key), schema.field(t, other_key)
        new = json.loads(common)
        automation = {"key": schema.slug(f"{t['singular']} {value} sets {g['label']}")[:40], "name": f"{t['singular']} {value.lower()} → {schema.lc(g['label'])}",
                      "trigger": {"type": "record_updated", "table": table}, "conditions": [{"field": key, "op": "changed_to", "value": value}],
                      "actions": [{"type": "update_record", "values": {other_key: new}}]}
        what = "today's date" if new == "{{today()}}" else f"“{engine.display(g, new)}”"
        out.append(Finding(
            "follow_up", f"sets:{table}.{key}={value}->{other_key}",
            f"Fill in {g['label']} automatically when a {schema.lc(t['singular'])} becomes {value}",
            f"Straight after setting a {schema.lc(t['singular'])} to {value}, people set {g['label']} to {what} {n} of {total} times.",
            [f"{n} of {total} times by hand"], [{"op": "add_automation", "automation": automation}],
            backtest(engine, automation, history)))
    return out


def stuck(engine, history):
    """Records that sit in an open stage for weeks: a daily nudge stops them being forgotten."""
    out = []
    for t in engine.tables.values():
        if t.get("embedded") or any(a["trigger"]["type"] == "schedule" and a["trigger"].get("table") == t["key"] for a in engine.bp.get("automations", [])):
            continue
        status = next((f for f in t["fields"] if f["type"] == "select" and f["key"] in ("status", "stage", "state")), None)
        if status is None:
            continue
        open_ = [o for o in status["options"] if not any(w in o.lower() for w in FINAL)]
        rows = [r for r in _rows(engine, t) if (r.data or {}).get(status["key"]) in open_]
        quiet = [r for r in rows if (utcnow() - (r.updated_at or r.created_at)).days >= 14]
        if len(quiet) < 5 or len(quiet) < 0.2 * len(rows):
            continue
        person = next((f for f in t["fields"] if f["type"] == "user" and not f.get("multiple")), None)
        automation = {"key": schema.slug(f"nudge {t['label']}")[:40], "name": f"Nudge {schema.lc(t['label'])} that have gone quiet",
                      "trigger": {"type": "schedule", "every": "day", "at": "08:00", "table": t["key"]},
                      "conditions": [{"field": status["key"], "op": "one_of", "value": open_},
                                     {"field": "updated_at", "op": "more_than_days_ago", "value": 7}],
                      "actions": [{"type": "notify", "to": person["key"] if person else "admin",
                                   "message": f"{{{{record}}}} has been {{{{record.{status['key']}}}}} for over a week with nothing happening"}]}
        worst = sorted(quiet, key=lambda r: r.updated_at or r.created_at)[:3]
        out.append(Finding(
            "stuck", f"stuck:{t['key']}",
            f"Nudge whoever has {schema.lc(t['label'])} that have gone quiet",
            f"{_plural(len(quiet), schema.lc(t['singular']), schema.lc(t['label']))} have sat in an open stage for over two weeks "
            f"without a change. A morning reminder after a week stops them being forgotten.",
            [f"{engine.title(r)}: {engine.display(status, (r.data or {}).get(status['key']))} since {(r.updated_at or r.created_at):%-d %b}" for r in worst],
            [{"op": "add_automation", "automation": automation}],
            {"summary": f"It would remind about {_plural(len(quiet), schema.lc(t['singular']), schema.lc(t['label']))} today.", "fired": len(quiet), "matched": len(quiet)}))
    return out


def failing(engine, history):
    """Automations that keep failing: the errors, for the analyst to fix."""
    since = utcnow() - timedelta(days=14)
    runs = Run.query.filter(Run.workspace_id == engine.ws.id, Run.created_at >= since).all()
    by = defaultdict(list)
    for r in runs:
        by[r.automation].append(r)
    found = []
    for key, rs in by.items():
        bad = [r for r in rs if r.status == "failed"]
        a = next((x for x in engine.bp.get("automations", []) if x["key"] == key), None)
        if a and len(bad) >= 3 and len(bad) / len(rs) >= 0.3:
            errors = Counter(r.error or "unknown" for r in bad).most_common(3)
            found.append({"automation": a, "failed": len(bad), "runs": len(rs), "errors": errors})
    return found


TRUST_AFTER = 20  # checks of an AI step's work before it can be trusted to run on its own when it's sure
TRUST_LOWER = 0.9  # how good its work must surely be (the lower bound of its share right)


def ai_trust(engine, history):
    """AI steps whose track record says how much checking they need: one people have found right nearly every time can
    work on its own when it's sure; one they keep having to fix should be checked every time again."""
    from .engine import automations
    out = []
    for a in engine.bp.get("automations", []):
        for at, step in automations.ai_steps(a):
            r = automations.track_record(engine, a["key"], at)
            mode = step.get("review", "never")
            what = automations.describe_action(engine, a, {k: v for k, v in step.items() if k not in ("review", "reviewer")})
            record = f"Checked {r['checked']} times: right {r['right']}, corrected {r['corrected']}, wrong {r['wrong']}"
            if mode == "always" and r["checked"] >= TRUST_AFTER and r["lower"] >= TRUST_LOWER:
                out.append(Finding("ai_trust", f"ai_trust:{a['key']}:{at}:when_unsure", f"Let the AI work on its own in “{a['name']}” when it's sure",
                                   f"People have checked its work {r['checked']} times and it was right {r['right']} times "
                                   f"({round(100 * r['share'])}%). It would still ask a person whenever it isn't sure, and a person would "
                                   f"still check 1 in 10 of the rest.", [what, record],
                                   [{"op": "update_automation", "automation": automations.with_review(a, at, "when_unsure")}]))
            elif mode != "always" and r["checked"] >= 10 and r["share"] < 0.8:
                out.append(Finding("ai_trust", f"ai_trust:{a['key']}:{at}:always", f"Have a person check everything the AI does in “{a['name']}”",
                                   f"Of the {r['checked']} times people checked its work, they had to fix {r['checked'] - r['right']}. "
                                   f"Until it does better, a person should check every one.", [what, record],
                                   [{"op": "update_automation", "automation": automations.with_review(a, at, "always")}]))
    return out


def questions(engine, days=60):
    """Questions people keep asking the assistant: grouped when they mean the same thing."""
    asked = (Message.query.filter(Message.workspace_id == engine.ws.id, Message.channel == "assistant", Message.role == "user",
                                  Message.created_at >= utcnow() - timedelta(days=days)).order_by(Message.id).all())
    words = [set(_stem(w) for w in re.findall(r"[a-z]{3,}", (m.text or "").lower()) if w not in _STOP) for m in asked]
    groups = []
    for i, m in enumerate(asked):
        for g in groups:
            if len(words[i] & g["words"]) / max(len(words[i] | g["words"]), 1) >= 0.45:
                g["asked"].append(m)
                g["words"] |= words[i]
                break
        else:
            groups.append({"asked": [m], "words": set(words[i])})
    return [g for g in groups if len(g["asked"]) >= 3]


def _stem(word):
    """owed → ow, owe → ow, invoices → invoic: enough to see two questions are about the same thing."""
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 2:
            word = word[:-len(suffix)]
            break
    return word[:-1] if word.endswith("e") and len(word) > 2 else word


_STOP = set("the and for what how many much are our is was were which who whose this that with from have has did does can "
            "you your there their show tell list give all any per".split())


def emails(engine, days=45):
    """Kinds of email that keep arriving (by sender and subject), from what the AI has read of the mailbox."""
    chunks = Chunk.query.filter(Chunk.workspace_id == engine.ws.id, Chunk.source == "email", Chunk.position == 0,
                                Chunk.created_at >= utcnow() - timedelta(days=days)).all()
    groups = defaultdict(list)
    for c in chunks:
        sender = re.search(r"From: [^<\n]*<([^>]*)>", c.text or "")
        domain = sender.group(1).split("@")[-1].lower() if sender and "@" in sender.group(1) else ""
        subject = re.sub(r"[\d#/:-]+", " ", (c.title or "").lower())
        key_words = " ".join(sorted(w for w in re.findall(r"[a-z]{4,}", subject) if w not in _STOP)[:3])
        groups[(domain, key_words)].append(c)
    return [{"domain": d, "words": w, "emails": cs} for (d, w), cs in groups.items() if len(cs) >= 4 and (d or w)]


# --- replaying an automation on the business's history ----------------------------------------------------------------

def backtest(engine, automation, history=None, days=90):
    """What an automation would have done over the last months: how often it would have run, and (when it creates
    records) how often people did the same thing by hand. {"summary", "fired", "matched", "examples"}."""
    history = history or History(engine, days)
    trig = automation["trigger"]
    t = engine.tables.get(trig.get("table"))
    if trig["type"] == "schedule":
        rows = [r for r in engine.rows(t["key"]) if engine.matches(r, automation["conditions"])] if t else []
        return {"summary": f"Today it would run for {_plural(len(rows), schema.lc(t['singular']) if t else 'record', schema.lc(t['label']) if t else 'records')}.",
                "fired": len(rows), "matched": len(rows), "examples": [engine.title(r) for r in rows[:3]]}
    if trig["type"] == "email_received":
        mails = Chunk.query.filter(Chunk.workspace_id == engine.ws.id, Chunk.source == "email", Chunk.position == 0).all()

        def hit(c):
            text = (c.text or "").lower()
            return all(str(trig.get(k, "")).lower() in text for k in ("from_contains", "subject_contains", "body_contains") if trig.get(k))
        hits = [c for c in mails if hit(c)]
        return {"summary": f"It would have picked up {_plural(len(hits), 'email')} of the {len(mails)} the AI has read.",
                "fired": len(hits), "matched": len(hits), "examples": [c.title for c in hits[:3]]}
    if t is None or trig["type"] not in ("record_created", "record_updated", "record_created_or_updated"):
        return None
    fired = []
    for c in history.changes:
        if history.table_of.get(c.record_id) != t["key"] or c.kind not in ("created", "updated"):
            continue
        if (trig["type"] == "record_created" and c.kind != "created") or (trig["type"] == "record_updated" and c.kind != "updated"):
            continue
        record = engine.get(c.record_id) or db.session.get(Record, c.record_id)
        if record is None:
            continue
        after = {**(record.data or {}), **{k: v[1] for k, v in (c.changes or {}).items()}}
        before = None if c.kind == "created" else {**after, **{k: v[0] for k, v in (c.changes or {}).items()}}
        draft = Record(workspace_id=engine.ws.id, table=t["key"], data=after)
        draft.id, draft.created_at, draft.updated_at = record.id, record.created_at, c.created_at
        if engine.matches(draft, automation["conditions"], old=before):
            fired.append(c)
    makes = next((a for a in automation["actions"] if a["type"] == "create_record"), None)
    matched = len(fired)
    if makes:
        link = next((k for k, v in makes["values"].items() if v == "{{record}}"), None)
        made_by_hand = [x for x in history.changes if x.kind == "created" and history.table_of.get(x.record_id) == makes["table"]]
        matched = sum(1 for c in fired if any(link and str((x.changes or {}).get(link, [None, None])[1]) == str(c.record_id)
                                              and timedelta(0) <= x.created_at - c.created_at <= timedelta(hours=2) for x in made_by_hand))
        o = engine.tables.get(makes["table"]) or {"singular": "record", "label": "records"}
        summary = (f"Over the last {days} days it would have run {_plural(len(fired), 'time')}; people made the {schema.lc(o['singular'])} "
                   f"by hand {matched} of those times, so it would have saved {matched} {schema.lc(o['label']) if matched != 1 else schema.lc(o['singular'])} "
                   f"of typing and made {len(fired) - matched} that weren't made by hand.")
    else:
        summary = f"Over the last {days} days it would have run {_plural(len(fired), 'time')}."
    return {"summary": summary, "fired": len(fired), "matched": matched,
            "examples": [engine.title(engine.get(c.record_id) or db.session.get(Record, c.record_id)) for c in fired[:3]]}


# --- the analyst's part ---------------------------------------------------------------------------------------------

REVIEW_SCHEMA = {"type": "object", "required": ["suggestions"], "properties": {"suggestions": {"type": "array", "items": {
    "type": "object", "required": ["title", "why", "ops"], "properties": {"title": {"type": "string"}, "why": {"type": "string"},
                                                                         "ops": {"type": "array", "items": {"type": "object"}}}}}}}


def _usage(engine, history):
    """How the business uses its ERP, compactly, for the analyst to read."""
    lines = []
    since = utcnow() - timedelta(days=30)
    for t in engine.tables.values():
        rows = _rows(engine, t)
        recent = sum(1 for r in rows if r.created_at >= since)
        empty = [f["label"] for f in t["fields"] if f["type"] in schema.STORED and rows
                 and sum(1 for r in rows if _filled((r.data or {}).get(f["key"]))) / len(rows) < 0.1]
        lines.append(f"- {t['label']}: {len(rows)} records, {recent} added in the last 30 days" + (f"; rarely filled: {', '.join(empty[:6])}" if empty else ""))
    runs = Run.query.filter(Run.workspace_id == engine.ws.id, Run.created_at >= since).all()
    for key, n in Counter(r.automation for r in runs).most_common(8):
        failed = sum(1 for r in runs if r.automation == key and r.status == "failed")
        lines.append(f"- automation {key}: ran {n} times in 30 days, failed {failed}")
    for g in questions(engine)[:5]:
        lines.append(f"- people asked the assistant {len(g['asked'])} times: “{g['asked'][-1].text[:160]}”")
    for g in emails(engine)[:5]:
        lines.append(f"- {len(g['emails'])} emails from {g['domain'] or 'various senders'} like “{g['emails'][-1].title[:100]}”")
    movers = Counter(c.by for c in history.changes if history.by_person(c))
    if movers:
        lines.append("- most active people: " + ", ".join(f"{who} ({n} changes)" for who, n in movers.most_common(5)))
    return "\n".join(lines)


def _design(engine, task):
    """Ask the analyst for design changes for one task; returns (ops, reply) once they fit, or None."""
    from .builder import chat, prompts
    convo = [{"role": "system", "content": prompts.system()},
             {"role": "user", "content": f"About the business: {engine.ws.about or engine.ws.name}\nToday: {engine.today.isoformat()}\n\n"
                                         f"The ERP right now:\n{prompts.outline(engine.bp, engine.counts())}"},
             {"role": "assistant", "content": "Understood. What would you like?"},
             {"role": "user", "content": task + "\nMake only the changes needed for this; \"more\": false."}]
    answer, result = chat.design(engine.ws, convo)
    if result.problems or not result.changed:
        return None
    return answer.get("ops") or [], str(answer.get("reply") or "")


def ai_findings(engine, history):
    """Improvements that need the analyst: fixing failing automations, answering what people keep asking, handling the
    email that keeps arriving, and its weekly review of the whole business."""
    out = []
    for f in failing(engine, history):
        a = f["automation"]
        errors = "; ".join(f"{e} ({n}×)" for e, n in f["errors"])
        made = _design(engine, f"The automation “{a['name']}” failed {f['failed']} of its last {f['runs']} runs with: {errors}. Fix it "
                               f"(update_automation with the whole corrected automation, or add what it needs).")
        if made:
            out.append(Finding("failing", f"fix:{a['key']}:{f['errors'][0][0][:80]}", f"Fix “{a['name']}”, which keeps failing",
                               f"It failed {f['failed']} of its last {f['runs']} runs. {made[1]}",
                               [f"{e} ({_plural(n, 'time')})" for e, n in f["errors"]], made[0]))
    for g in questions(engine):
        asked = [m.text for m in g["asked"]][-5:]
        made = _design(engine, "People keep asking the assistant questions like: " + " | ".join(f"“{q[:200]}”" for q in asked)
                       + ". Add what answers this at a glance (a number, chart, list or pivot on the most fitting page, or a new page).")
        if made:
            out.append(Finding("question", "ask:" + " ".join(sorted(g["words"]))[:180], f"Answer “{asked[-1][:80]}” at a glance",
                               f"People asked this {_plural(len(g['asked']), 'time')} in the last two months. {made[1]}",
                               [f"“{q[:140]}”" for q in asked], made[0]))
    handled = [a for a in engine.bp.get("automations", []) if a["trigger"]["type"] == "email_received"]
    for g in emails(engine):
        if any(backtest(engine, a)["fired"] >= len(g["emails"]) * 0.6 for a in handled):
            continue
        samples = "\n".join(f"- {c.title} — {c.text[:300]}" for c in g["emails"][:4])
        made = _design(engine, f"{len(g['emails'])} emails like these arrived in the last weeks and nothing handles them:\n{samples}\n"
                               "Make an email_received automation (with from_contains / subject_contains filters that catch them) that "
                               "turns each into the right record with ai_create_record, adding a table if the ERP has none for them.")
        if made:
            automation = next((op.get("automation") for op in made[0] if op.get("op") in ("add_automation", "update_automation") and op.get("automation")), None)
            proof = None
            if automation:
                try:
                    proof = backtest(engine, schema.normalize_automation(automation))
                except schema.SchemaError:
                    proof = None
            out.append(Finding("email", f"mail:{g['domain']}:{g['words']}", f"Handle the {g['words'] or 'emails'} from {g['domain'] or 'your mailbox'} automatically",
                               f"{len(g['emails'])} of them arrived recently and someone had to deal with each by hand. {made[1]}",
                               [c.title for c in g["emails"][:5]], made[0], proof))
    return out


def review(engine, history):
    """The analyst's own look at how the business is using its ERP: up to three improvements, each checked on its own."""
    from .builder import prompts
    usage = _usage(engine, history)
    content, _ = ai.chat([{"role": "system", "content": prompts.system()},
                          {"role": "user", "content": f"About the business: {engine.ws.about or engine.ws.name}\nToday: {engine.today.isoformat()}\n\n"
                                                      f"The ERP right now:\n{prompts.outline(engine.bp, engine.counts())}\n\n"
                                                      f"How it has been used:\n{usage}\n\n"
                                                      "As their analyst, suggest up to three improvements that would save them real time "
                                                      "or prevent mistakes, based on how they actually use it (not things they haven't "
                                                      "shown a need for). Each: a short title, why (citing what you saw), and the ops. "
                                                      "Suggest nothing if it's working well."}],
                         schema={**REVIEW_SCHEMA, "$defs": prompts.DEFS, "properties": {"suggestions": {"type": "array", "items": {
                             **REVIEW_SCHEMA["properties"]["suggestions"]["items"],
                             "properties": {"title": {"type": "string"}, "why": {"type": "string"},
                                            "ops": {"type": "array", "items": prompts._OP}}}}}}, think=False)
    out = []
    for s in (ai.parse_json(content).get("suggestions") or [])[:3]:
        result = ops.apply(engine.bp, s.get("ops") or [])
        if result.problems or not result.changed:
            continue
        automation = next((op.get("automation") for op in s["ops"] if op.get("op") in ("add_automation", "update_automation") and isinstance(op.get("automation"), dict)), None)
        proof = None
        if automation:
            try:
                proof = backtest(engine, schema.normalize_automation(automation), history)
            except schema.SchemaError:
                proof = None
        out.append(Finding("analyst", "review:" + schema.slug(s.get("title") or "")[:120], str(s.get("title") or "An improvement")[:300],
                           str(s.get("why") or ""), [], s["ops"], proof))
    return out


# --- keeping the suggestions ---------------------------------------------------------------------------------------------

def learn(workspace, with_ai=True):
    """Look at how the business uses its ERP and keep what's worth suggesting. Returns the new suggestions."""
    engine = Engine(workspace, by="analyst")
    if not engine.tables:
        return []
    history = History(engine)
    findings = []
    for detector in (unused_fields, text_to_choices, defaults, follow_ups, stuck, ai_trust):
        try:
            findings += detector(engine, history)
        except Exception:  # one detector's trouble doesn't stop the others
            log.exception("The %s detector failed for workspace %s", detector.__name__, workspace.id)
    if with_ai:
        findings += ai_findings(engine, history)
        settings = workspace.state or {}
        last = settings.get("last_review")
        if not last or utcnow() - datetime.fromisoformat(last) >= REVIEW_EVERY:
            findings += review(engine, history)
            workspace = db.session.get(Workspace, workspace.id)
            workspace.state = {**(workspace.state or {}), "last_review": utcnow().isoformat()}
            db.session.commit()
    known = {s.signature: s for s in Suggestion.query.filter_by(workspace_id=workspace.id)}
    fresh = []
    found_now = set()
    for f in findings:
        found_now.add(f.signature)
        check = ops.apply(engine.bp, f.ops)
        if check.problems or not check.changed:
            continue
        old = known.get(f.signature)
        if old is not None and old.status != "stale":
            if old.status == "open":  # the same finding, with today's numbers
                old.why, old.evidence, old.proof, old.ops, old.changes = f.why, f.evidence, f.proof, f.ops, check.changes
            continue
        if old is not None:
            db.session.delete(old)
            db.session.flush()
        s = Suggestion(workspace_id=workspace.id, kind=f.kind, signature=f.signature, title=f.title, why=f.why, evidence=f.evidence,
                       proof=f.proof, ops=f.ops, changes=check.changes)
        db.session.add(s)
        fresh.append(s)
    for s in known.values():  # a pattern that's gone (someone fixed it by hand) stops being suggested
        if s.status == "open" and s.kind in ("unused_field", "text_to_select", "default_value", "follow_up", "stuck", "ai_trust") and s.signature not in found_now:
            s.status = "stale"
    db.session.commit()
    if fresh:
        from .engine import automations
        automations.notify(engine, f"Your analyst has {_plural(len(fresh), 'new suggestion')} for improving how {engine.bp.get('name') or workspace.name} works",
                           "improvements", to="admin")
    return fresh


def apply(workspace, suggestion, by):
    """Make a suggestion's changes, as a new design version. Returns the version, or raises ValueError if the design has
    moved on so they no longer fit."""
    from .builder import chat
    result = ops.apply(workspace.blueprint, suggestion.ops or [])
    if result.problems:
        suggestion.status = "stale"
        db.session.commit()
        raise ValueError("The design has changed since this was suggested, so it no longer fits: " + "; ".join(result.problems[:3]))
    number = chat.save_version(workspace, result.blueprint, result.changes, "improvement", by=by)
    suggestion.status, suggestion.version_after, suggestion.decided_at = "applied", number, utcnow()
    suggestion.decided_by = by if isinstance(by, str) else None
    db.session.commit()
    return number


@jobs.handler("learn")
def _learn(job):
    workspace = db.session.get(Workspace, job.workspace_id)
    jobs.stage(job, "Looking at how your app is being used", 10)
    fresh = learn(workspace, with_ai=job.payload.get("ai", True))
    return {"new": len(fresh), "titles": [s.title for s in fresh]}
