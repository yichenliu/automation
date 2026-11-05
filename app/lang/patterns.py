"""Features: ready-made behaviour a builder asks for by name, with its parameters.

A feature compiles to the rules, fields and stage moves that do it, the same way every time. Each part it makes is
tagged with its key (`"feature": key`), so changing the feature remakes them and removing it removes them. Each
feature also writes its own checks, worked examples the simulator acts out, so whether it works is never left to an
example the AI wrote.

A feature is a patch: {"do": "stage_alert", "list": "invoices", "stage": "Paid", "tell": "owner"}. The design keeps
each one under "features": {key, do, params, undo} (undo puts back what it changed in place, such as who may make a
stage move).
"""
import copy
from typing import Literal

from pydantic import Field

from ..blueprint import expr, schema
from .compile import Problem, _apply_ops, _list, ir_conditions
from .design import Design, NameProblem
from .model import Base, Condition, Value
from .registry import items, register

# --- the parts every feature uses -------------------------------------------------------------------------------------


class Feature(Base):
    key: str | None = Field(None, description="left out: the platform names it")

    def feature_key(self):
        return schema.slug(self.key or self.default_key())

    def default_key(self):
        return self.do

    def build(self, d, where):
        """A Made: what this feature needs in the design."""
        raise NotImplementedError

    def checks(self, d):
        """Worked examples that show it works (in the try-out's format)."""
        return []

    def said(self, d):
        return ""


class Made:
    def __init__(self):
        self.fields = []  # (list key, design field): made if missing, changed if different
        self.rules = []  # design automations
        self.ops = []  # design ops on lists (stage moves, limits, locks, documents)
        self.undo = {}  # what to put back on removal: {"workflow": {list: transitions}, "limits": {...}, "lock_if": {...}}


def _t(d, name, where, what="list"):
    return _list(d, name, f"{where} › {what}")


def _stage(d, t, stage, where):
    f = d.stage_field(t)
    if f is None:
        raise Problem(f"{where}: {t['label']} has no stages yet: give it stages first (set_stages)")
    found = d.choice(f, stage)
    if found is None:
        raise Problem(f"{where}: “{stage}” isn't a stage of {t['label']} (its stages: {', '.join(f.get('options') or [])})")
    return f, found


def _who(d, t, who, where):
    try:
        return d.who(t, who)
    except NameProblem as exc:
        raise Problem(f"{where} › who: {exc}") from None


def _email_to(d, t, to, where):
    if "@" in to or "{{" in to:
        return to
    try:
        f, _, keys = d.path(t, to)
    except NameProblem as exc:
        reach = d.paths_to(t, {"email"})
        raise Problem(f"{where} › email_to: {exc}. The email addresses {_a(t['singular'].lower())} can reach: "
                      f"{', '.join(reach) or 'none (add an email field where the person is kept)'}; to tell the business's own people "
                      f"in the app instead, leave email_to out and set tell") from None
    if f.get("type") not in ("email", "text", "lookup", "formula"):
        raise Problem(f"{where} › email_to: {to} isn't an email address field ({f.get('type')})")
    return "{{" + ".".join(keys) + "}}"


def _title(t):
    return "{{" + (t.get("title_field") or schema.title_field(t) or "id") + "}}"


def _a(word):
    return f"{'an' if str(word)[:1].lower() in 'aeiou' else 'a'} {str(word)[:1].lower() + str(word)[1:] if str(word)[1:2].islower() else word}"


def _tell_step(d, t, tell, email_to, subject, message, where):
    if email_to:
        return {"type": "send_email", "to": _email_to(d, t, email_to, where), "subject": subject or message[:120], "body": message}
    return {"type": "notify", "to": _who(d, t, tell or "owner", where), "message": message}


def _sample(d, f, n, alias):
    """An ordinary value for a field people fill in."""
    kind, ftype = d.kind(f), f.get("type")
    if ftype == "select":
        return f.get("default") or (f.get("options") or [None])[0]
    if ftype == "multi_select":
        return (f.get("options") or [])[:1]
    if ftype == "email":
        return f"{alias}@example.com"
    if ftype == "phone":
        return "0400 000 00" + str(n % 10)
    if ftype == "url":
        return "https://example.com"
    if ftype == "checkbox":
        return False
    if ftype == "user":
        return "owner"
    if ftype in ("date",):
        return "today+7"
    if ftype == "datetime":
        return "today+7"
    if ftype == "time":
        return "10:00"
    if kind == "number":
        return 100 + n
    return f"Sample {f.get('label') or f['key']} {alias}"


def adding(d, t, alias, values=None, by=None, depth=0, made=None, through=()):
    """Steps that add a valid record of t as `alias`: its required fields filled in (a record it must link to added
    first, and one it links to `through` even if it needn't: the customer an email goes to), and these values."""
    steps, values = [], dict(values or {})
    made = made if made is not None else {}
    flow_field = (t.get("workflow") or {}).get("field")
    for i, f in enumerate(t.get("fields", [])):
        if f["key"] in values or f["type"] in schema.CALCULATED | schema.FILES or f["key"] == flow_field:
            continue
        if not f.get("required") and not (f["type"] == "link" and f["key"] in through):
            if f["type"] == "email":  # an address to send to, so what emails it can be seen to
                values[f["key"]] = f"{alias}@example.com"
            continue
        if f["type"] == "link":
            other = d.list(f.get("table"))
            if other is None or depth > 2:
                continue
            if other["key"] in made:
                values[f["key"]] = made[other["key"]]
                continue
            other_alias = f"{alias}_{other['key'][:8]}"
            steps += adding(d, other, other_alias, depth=depth + 1, made=made)
            made[other["key"]] = other_alias
            values[f["key"]] = other_alias
        else:
            values[f["key"]] = _sample(d, f, i, alias)
    step = {"do": "add", "table": t["key"], "as": alias, "values": values}
    if by:
        step["by"] = by
    return steps + [step]


def _through(path):
    """The link an email path goes through ("customer" for customer.email), for a check to fill it in."""
    return (str(path or "").split(".")[0],) if "." in str(path or "") else ()


def setting(d, t, field_key, value, alias, depth=0):
    """(values for the add, steps after it) that make this field come out at `value` or more (exactly 0 for 0):
    a field people fill in directly; a sum over lines (one line made to come out at it); a formula (its inputs set so
    the sum works out, tried out where they're all typed in). None when it can't be done for sure."""
    f = d.field(t, field_key)
    if f is None or depth > 3:
        return None
    if f["type"] in schema.STORED:
        return ({f["key"]: value}, []) if d.kind(f) == "number" else None
    if f["type"] == "rollup" and f.get("aggregate") == "sum" and f.get("field") and "." not in str(f.get("via") or "."):
        lines = d.list(f.get("table"))
        got = setting(d, lines, f["field"], value, f"{alias}_line", depth + 1) if lines else None
        if got is None:
            return None
        values, after = got
        return {}, adding(d, lines, f"{alias}_line", {f["via"]: alias, **values}) + after
    if f["type"] == "formula":
        try:
            names = [n for n in expr.names_in(f["expression"]) if d.field(t, n)]
        except expr.ExprError:
            return None
        typed = [n for n in names if d.field(t, n)["type"] in schema.STORED and d.kind(d.field(t, n)) == "number"]
        if typed and len(typed) == len(names):  # all typed in: tried out until the sum works out
            for rest in (1, 0):
                for n in typed:
                    values = {k: (value if k == n else rest) for k in typed}
                    try:
                        got = expr.evaluate(f["expression"], expr.Context(values))
                    except Exception:  # noqa: BLE001 - this way of filling it in doesn't work it out
                        continue
                    if got is not None and (got >= value if value else got == 0):
                        return {k: v for k, v in values.items()}, []
            return None
        for n in names:  # one input worked out itself (a subtotal of lines): that one carries it
            got = setting(d, t, n, value, alias, depth + 1)
            if got is not None:
                return got
    return None


# --- the features ---------------------------------------------------------------------------------------------------

@register("feature", {"do": "stage_alert", "list": "invoices", "stage": "Paid", "tell": "owner"})
class StageAlert(Feature):
    """Tell someone whenever a record gets to a stage, however it gets there (a button, the board, a rule). tell: who
    in the app; or email_to: an email field (customer.email) to email them instead."""
    do: Literal["stage_alert"] = "stage_alert"
    list: str
    stage: str
    tell: str = "owner"
    email_to: str | None = None
    message: str | None = None

    def default_key(self):
        return f"alert_{self.list}_{self.stage}"

    def build(self, d, where):
        t = _t(d, self.list, where)
        f, stage = _stage(d, t, self.stage, where)
        made = Made()
        message = self.message or f"{_title(t)} is now {stage}"
        made.rules.append({"key": self.feature_key(), "name": f"Tell when {_a(t['singular'].lower())} gets to {stage}",
                           "trigger": {"type": "record_created_or_updated", "table": t["key"]},
                           "conditions": [{"field": f["key"], "op": "changed_to", "value": stage}],
                           "actions": [_tell_step(d, t, self.tell, self.email_to, f"{t['singular']} {stage}", message, where)]})
        return made

    def checks(self, d):
        t = d.list(self.list)
        f, stage = d.stage_field(t), d.choice(d.stage_field(t), self.stage)
        steps = adding(d, t, "r1", through=_through(self.email_to)) + [{"do": "move", "record": "r1", "to": stage}]
        out = [{"title": f"Someone is told when {_a(t['singular'].lower())} gets to {stage}", "steps": steps,
                "expect": [{"told": True, "record": "r1"}]}]
        others = [s for s in f.get("options") or [] if s != stage and s != (t.get("workflow") or {}).get("start", (f.get("options") or [None])[0])]
        before = [s for s in (f.get("options") or [])[:(f.get("options") or []).index(stage)] if s in others]
        if before:
            out.append({"title": f"Nobody is told when {_a(t['singular'].lower())} only gets to {before[0]}",
                        "steps": adding(d, t, "r2") + [{"do": "move", "record": "r2", "to": before[0]}],
                        "expect": [{"told": False, "record": "r2"}]})
        return out

    def said(self, d):
        return f"tells {self.email_to or self.tell} whenever {_a(self.list.rstrip('s'))} gets to {self.stage}"


@register("feature", {"do": "approval", "list": "quotes", "stage": "Sent", "by": "owner", "over": {"field": "total", "amount": 5000}})
class ApprovalNeeded(Feature):
    """Only the approver (by: owner, admin or a team) may move a record into a stage; with over, only when a number
    field is above an amount (anyone may below it). tell_approver: tell them when one is waiting."""
    do: Literal["approval"] = "approval"
    list: str
    stage: str
    by: str = "owner"
    over: "Over | None" = None
    tell_approver: bool = True

    def default_key(self):
        return f"approval_{self.list}_{self.stage}"

    def build(self, d, where):
        t = _t(d, self.list, where)
        f, stage = _stage(d, t, self.stage, where)
        by = _who(d, t, self.by, where)
        flow = copy.deepcopy(t.get("workflow") or {})
        moves = flow.get("transitions") or []
        into = [mv for mv in moves if mv["to"] == stage]
        if not into:
            raise Problem(f"{where}: no move takes {_a(t['singular'].lower())} to {stage} by hand, so there's nothing to approve; add a move to it (set_stages)")
        entry = next((e for e in d.bp.get("features", []) if e["key"] == self.feature_key()), None)
        original = (entry or {}).get("undo", {}).get("workflow", {}).get(t["key"]) or moves
        rebuilt, made = [], Made()
        made.undo = {"workflow": {t["key"]: original}}
        over = None
        if self.over:
            try:
                of, _, keys = d.path(t, self.over.field)
            except NameProblem as exc:
                raise Problem(f"{where} › over: {exc}") from None
            if d.kind(of) != "number":
                raise Problem(f"{where} › over: {self.over.field} isn't a number")
            over = ".".join(keys)
        for mv in copy.deepcopy(original):
            if mv["to"] != stage:
                rebuilt.append(mv)
                continue
            key = self.feature_key()
            if over:
                rebuilt.append({**mv, "feature": key, "only_if": list(mv.get("only_if") or []) + [  # none given yet is under it
                    {"any": [{"field": over, "op": "is_empty"}, {"field": over, "op": "at_most", "value": self.over.amount}]}]})
                rebuilt.append({**mv, "feature": key, "who": [by], "only_if": list(mv.get("only_if") or []) + [{"field": over, "op": "greater_than", "value": self.over.amount}]})
            else:
                rebuilt.append({**mv, "feature": key, "who": [by]})
        flow["transitions"] = rebuilt
        made.ops.append({"op": "update_table", "table": t["key"], "workflow": flow})
        if self.tell_approver:
            waits = sorted({s for mv in into for s in mv.get("from") or []})
            if waits:
                conds = [{"field": f["key"], "op": "one_of" if len(waits) > 1 else "changed_to", "value": waits if len(waits) > 1 else waits[0]}]
                if len(waits) > 1:
                    conds = [{"field": f["key"], "op": "changed"}, {"field": f["key"], "op": "one_of", "value": waits}]
                if over:
                    conds.append({"field": over, "op": "greater_than", "value": self.over.amount})
                made.rules.append({"key": f"{self.feature_key()}_waiting", "name": f"Tell {by} {_a(t['singular'].lower())} needs their OK",
                                   "trigger": {"type": "record_created_or_updated", "table": t["key"]}, "conditions": conds,
                                   "actions": [{"type": "notify", "to": by, "message": f"{_title(t)} needs your OK before it goes to {stage}"}]})
        return made

    def checks(self, d):
        t = d.list(self.list)
        stage = d.choice(d.stage_field(t), self.stage)
        approver = None if self.by in ("owner", "me") else self.by
        big = setting(d, t, schema.slug(self.over.field), self.over.amount * 10 + 100, "q1") if self.over else ({}, [])
        if big is None:  # what makes it big can't be set for sure: only the approver's own move is checked
            return [{"title": f"The approver can move {_a(t['singular'].lower())} to {stage}",
                     "steps": adding(d, t, "q1") + [{"do": "move", "record": "q1", "to": stage, **({"by": approver} if approver else {})}],
                     "expect": [{"record": "q1", "field": d.stage_field(t)["key"], "equals": stage}]}]
        values, after = big
        out = [{"title": f"Staff can't move {_a(t['singular'].lower())} to {stage} without the OK" + (f" (over {self.over.amount:g})" if self.over else ""),
                "steps": adding(d, t, "q1", values) + after + [{"do": "move", "record": "q1", "to": stage, "by": "staff", "refused": True}],
                "expect": [{"record": "q1", "field": d.stage_field(t)["key"], "not_equals": stage}]}]
        if approver is None:
            values2, after2 = setting(d, t, schema.slug(self.over.field), self.over.amount * 10 + 100, "q2") if self.over else ({}, [])
            out.append({"title": f"The owner can move it to {stage}",
                        "steps": adding(d, t, "q2", values2) + after2 + [{"do": "move", "record": "q2", "to": stage}],
                        "expect": [{"record": "q2", "field": d.stage_field(t)["key"], "equals": stage}]})
        if self.over:
            small = setting(d, t, schema.slug(self.over.field), 0, "q3")
            if small is not None:
                values3, after3 = small
                out.append({"title": f"Staff can move a small one ({self.over.amount:g} or less) to {stage} themselves",
                            "steps": adding(d, t, "q3", values3) + [s for s in after3 if False] + [{"do": "move", "record": "q3", "to": stage, "by": "staff"}],
                            "expect": [{"record": "q3", "field": d.stage_field(t)["key"], "equals": stage}]})
        return out

    def said(self, d):
        return f"{_a(self.list.rstrip('s'))} needs {self.by}'s OK to go to {self.stage}" + (f" when its {self.over.field} is over {self.over.amount:g}" if self.over else "")


class Over(Base):
    """A number field and the amount above which the approval is needed."""
    field: str
    amount: float


@register("feature", {"do": "chase", "list": "invoices", "stage": "Sent", "days": 14, "email_to": "customer.email",
                      "message": "A friendly reminder: invoice {{number}} is still unpaid."})
class Chase(Feature):
    """If a record is still at a stage some days after it got there (or after a date field, since), remind someone:
    tell in the app or email_to an email field. Once, unless every_days repeats it."""
    do: Literal["chase"] = "chase"
    list: str
    stage: str
    days: int
    since: str | None = Field(None, description="a date field to count from; left out: the day it got to the stage")
    tell: str = "owner"
    email_to: str | None = None
    message: str | None = None
    every_days: int | None = None

    def default_key(self):
        return f"chase_{self.list}_{self.stage}"

    def build(self, d, where):
        t = _t(d, self.list, where)
        f, stage = _stage(d, t, self.stage, where)
        made, key = Made(), self.feature_key()
        if self.since:
            sf = d.field(t, self.since)
            if sf is None or d.kind(sf) != "date":
                raise Problem(f"{where} › since: {self.since} isn't a date field of {t['label']}")
            since = sf["key"]
        else:
            since = schema.slug(f"{stage}_on")
            made.fields.append((t["key"], {"key": since, "label": f"{stage} on", "type": "date", "help": f"The day it got to {stage}."}))
            made.rules.append({"key": f"{key}_stamp", "name": f"Note the day {_a(t['singular'].lower())} gets to {stage}",
                               "trigger": {"type": "record_created_or_updated", "table": t["key"]},
                               "conditions": [{"field": f["key"], "op": "changed_to", "value": stage}],
                               "actions": [{"type": "update_record", "values": {since: "{{today()}}", schema.slug(f"chased_{stage}"): None}}]})
        last = schema.slug(f"chased_{stage}")
        made.fields.append((t["key"], {"key": last, "label": f"Last reminded ({stage})", "type": "date"}))
        conds = [{"field": f["key"], "op": "equals", "value": stage},
                 {"field": since, "op": "more_than_days_ago", "value": max(self.days - 1, 0)}]
        if self.every_days:
            conds.append({"any": [{"field": last, "op": "is_empty"}, {"field": last, "op": "more_than_days_ago", "value": max(self.every_days - 1, 0)}]})
        else:
            conds.append({"field": last, "op": "is_empty"})
        message = self.message or f"{_title(t)} has been {stage} for {self.days} days"
        made.rules.append({"key": key, "name": f"Chase {t['label'].lower()} still {stage} after {self.days} days",
                           "trigger": {"type": "schedule", "table": t["key"], "every": "day", "at": "08:00"}, "conditions": conds,
                           "actions": [_tell_step(d, t, self.tell, self.email_to, f"Reminder: {t['singular'].lower()} {_title(t)}", message, where),
                                       {"type": "update_record", "values": {last: "{{today()}}"}}]})
        return made

    def checks(self, d):
        t = d.list(self.list)
        stage = d.choice(d.stage_field(t), self.stage)
        values = {}
        if self.since:
            values[d.field(t, self.since)["key"]] = "today"
        steps = adding(d, t, "r1", values, through=_through(self.email_to)) + [{"do": "move", "record": "r1", "to": stage}]
        out = [{"title": f"{_a(t['singular'].lower()).capitalize()} still {stage} after {self.days} days is chased",
                "steps": steps + [{"do": "wait", "days": self.days}, {"do": "run", "rule": self.feature_key()}],
                "expect": [{"told": True, "record": "r1"}]}]
        if self.days >= 2:
            out.append({"title": f"One {stage} for less than {self.days} days isn't chased yet",
                        "steps": adding(d, t, "r2", dict(values), through=_through(self.email_to)) + [{"do": "move", "record": "r2", "to": stage}, {"do": "wait", "days": self.days - 1},
                                                                    {"do": "run", "rule": self.feature_key()}],
                        "expect": [{"told": False, "record": "r2"}]})
        return out

    def said(self, d):
        return f"chases {self.list} still {self.stage} after {self.days} days"


@register("feature", {"do": "overdue", "list": "invoices", "date_field": "due_date", "from_stages": ["Sent"], "to_stage": "Overdue"})
class Overdue(Feature):
    """Each day, move records whose date field has passed (by days_after) from these stages to a late stage (made if
    it isn't there yet); tell: who to tell, if anyone."""
    do: Literal["overdue"] = "overdue"
    list: str
    date_field: str
    from_stages: list[str]
    to_stage: str = "Overdue"
    days_after: int = 0
    tell: str | None = None

    def default_key(self):
        return f"overdue_{self.list}"

    def build(self, d, where):
        t = _t(d, self.list, where)
        f = d.stage_field(t)
        if f is None:
            raise Problem(f"{where}: {t['label']} has no stages yet: give it stages first (set_stages)")
        df = d.field(t, self.date_field)
        if df is None or d.kind(df) != "date":
            raise Problem(f"{where} › date_field: {self.date_field} isn't a date field of {t['label']} (its dates: "
                          f"{', '.join(x['key'] for x in t['fields'] if d.kind(x) == 'date') or 'none'})")
        froms = []
        for s in self.from_stages:
            found = d.choice(f, s)
            if found is None:
                raise Problem(f"{where} › from_stages: “{s}” isn't a stage of {t['label']} ({', '.join(f.get('options') or [])})")
            froms.append(found)
        made = Made()
        late = d.choice(f, self.to_stage)
        options = [o for o in f.get("options") or [] if o != late]
        at = max(options.index(s) for s in froms) + 1
        placed = options[:at] + [late or self.to_stage.strip()] + options[at:]  # the late stage, straight after the ones it comes from
        if placed != list(f.get("options") or []):
            late = late or self.to_stage.strip()
            made.ops.append({"op": "update_field", "table": t["key"], "key": f["key"], "changes": {**f, "options": placed}})
            made.undo = {"options": {t["key"]: f.get("options")}}
        flow = copy.deepcopy(t.get("workflow") or {})
        if flow.get("transitions"):  # a late one moves on as the ones it came from do (an overdue invoice can still be paid)
            entry = next((e for e in d.bp.get("features", []) if e["key"] == self.feature_key()), None)
            original = (entry or {}).get("undo", {}).get("workflow", {}).get(t["key"]) or flow["transitions"]
            moves = copy.deepcopy(original)
            for mv in moves:
                if set(mv.get("from") or []) & set(froms) and late not in mv["from"] and mv["to"] != late:
                    mv["from"] = list(mv["from"]) + [late]
            flow["transitions"] = moves
            made.ops.append({"op": "update_table", "table": t["key"], "workflow": flow})
            made.undo = {**made.undo, "workflow": {t["key"]: original}}
        cond_date = {"field": df["key"], "op": "before_today"} if not self.days_after else \
            {"field": df["key"], "op": "more_than_days_ago", "value": self.days_after}
        actions = [{"type": "update_record", "values": {f["key"]: late}}]
        if self.tell:
            actions.append({"type": "notify", "to": _who(d, t, self.tell, where), "message": f"{_title(t)} is {late}"})
        made.rules.append({"key": self.feature_key(), "name": f"Mark {t['label'].lower()} {late.lower()}",
                           "trigger": {"type": "schedule", "table": t["key"], "every": "day", "at": "07:00"},
                           "conditions": [{"field": f["key"], "op": "one_of", "value": froms}, cond_date], "actions": actions})
        return made

    def checks(self, d):
        t = d.list(self.list)
        f = d.stage_field(t)
        df = d.field(t, self.date_field)
        late = d.choice(f, self.to_stage) or self.to_stage
        start = d.choice(f, self.from_stages[0])
        due = {df["key"]: "today+2"} if df["type"] in schema.STORED else {}
        move = [{"do": "move", "record": "r1", "to": start}] if start != ((t.get("workflow") or {}).get("start") or (f.get("options") or [None])[0]) else []
        out = [{"title": f"{_a(t['singular'])} past its {df['label'].lower()} becomes {late}",
                "steps": adding(d, t, "r1", due) + move + [{"do": "wait", "days": 3 + self.days_after}, {"do": "run", "rule": self.feature_key()}],
                "expect": [{"record": "r1", "field": f["key"], "equals": late}]}]
        if due:
            move2 = [{**s, "record": "r2"} for s in move]
            out.append({"title": f"One not yet due stays {start}",
                        "steps": adding(d, t, "r2", {df["key"]: "today+10"}) + move2 + [{"do": "wait", "days": 3}, {"do": "run", "rule": self.feature_key()}],
                        "expect": [{"record": "r2", "field": f["key"], "equals": start}]})
        return out

    def said(self, d):
        return f"marks {self.list} {self.to_stage} once their {self.date_field} has passed"


@register("feature", {"do": "line_totals", "list": "invoices", "lines": "invoice_lines", "tax_percent": 10, "tax_label": "GST"})
class LineTotals(Feature):
    """Lines with a quantity and a price make each line's amount; the record's subtotal is the sum of its lines, plus
    tax at a percent (if any) makes its total. Makes the fields that aren't there yet."""
    do: Literal["line_totals"] = "line_totals"
    list: str
    lines: str
    quantity: str = "quantity"
    price: str = "unit_price"
    amount: str = "amount"
    subtotal: str = "subtotal"
    tax_percent: float | None = None
    tax_label: str = "Tax"
    total: str = "total"

    def default_key(self):
        return f"totals_{self.list}"

    def build(self, d, where):
        t = _t(d, self.list, where)
        lines = _t(d, self.lines, where, "lines")
        link = next((x for x in lines["fields"] if x["type"] == "link" and x.get("table") == t["key"]), None)
        if link is None:
            raise Problem(f"{where}: {lines['label']} has no link to {t['label']}: add one (a link field to {t['key']}) first")
        made = Made()
        q, p, a = (schema.slug(x) for x in (self.quantity, self.price, self.amount))
        sub, total = schema.slug(self.subtotal), schema.slug(self.total)
        made.fields += [(lines["key"], {"key": q, "label": "Quantity", "type": "number", "default": 1}),
                        (lines["key"], {"key": p, "label": "Unit price", "type": "money"}),
                        (lines["key"], {"key": a, "label": "Amount", "type": "formula", "expression": f"{q} * {p}", "format": "money"}),
                        (t["key"], {"key": sub, "label": "Subtotal", "type": "rollup", "table": lines["key"], "via": link["key"],
                                    "aggregate": "sum", "field": a})]
        keep = lambda k, f: (lines["key"] if k in (q, p, a) else t["key"], f)  # noqa: E731
        for i, (tk, f) in enumerate(made.fields):  # a field it has already (a quantity of its own) stays as it is
            here = d.list(tk)
            have = d.field(here, f["key"])
            if have is not None and have["type"] in schema.STORED and f["type"] in schema.STORED:
                made.fields[i] = keep(f["key"], {**have})
        if self.tax_percent:
            tax = schema.slug(self.tax_label) or "tax"
            made.fields.append((t["key"], {"key": tax, "label": self.tax_label, "type": "formula",
                                           "expression": f"round({sub} * {self.tax_percent / 100:g}, 2)", "format": "money"}))
            made.fields.append((t["key"], {"key": total, "label": "Total", "type": "formula", "expression": f"{sub} + {tax}", "format": "money"}))
        else:
            made.fields.append((t["key"], {"key": total, "label": "Total", "type": "formula", "expression": sub, "format": "money"}))
        return made

    def checks(self, d):
        t, lines = d.list(self.list), d.list(self.lines)
        link = next(x for x in lines["fields"] if x["type"] == "link" and x.get("table") == t["key"])
        q, p = schema.slug(self.quantity), schema.slug(self.price)
        steps = adding(d, t, "r1") + adding(d, lines, "l1", {link["key"]: "r1", q: 2, p: 100}) + adding(d, lines, "l2", {link["key"]: "r1", q: 1, p: 50})
        expect = [{"record": "r1", "field": schema.slug(self.subtotal), "equals": "=2*100+1*50"}]
        if self.tax_percent:
            expect.append({"record": "r1", "field": schema.slug(self.total), "equals": f"=round((2*100+1*50)*(1+{self.tax_percent}/100), 2)"})
        else:
            expect.append({"record": "r1", "field": schema.slug(self.total), "equals": "=2*100+1*50"})
        return [{"title": f"{_a(t['singular'])}'s lines add up to its total", "steps": steps, "expect": expect}]

    def said(self, d):
        return f"adds up each {self.list.rstrip('s')}'s lines" + (f" with {self.tax_label} at {self.tax_percent:g}%" if self.tax_percent else "")


@register("feature", {"do": "payments", "list": "invoices", "payments": "payments", "paid_stage": "Paid", "part_paid_stage": "Part paid"})
class Payments(Feature):
    """Payments against a record (an invoice): what's paid is the sum of its payments, the balance is its total less
    that, and it moves to paid_stage when nothing is left to pay (to part_paid_stage, if given, when some is)."""
    do: Literal["payments"] = "payments"
    list: str
    payments: str
    amount: str = "amount"
    total: str = "total"
    paid_stage: str = "Paid"
    part_paid_stage: str | None = None

    def default_key(self):
        return f"payments_{self.list}"

    def build(self, d, where):
        t = _t(d, self.list, where)
        pay = _t(d, self.payments, where, "payments")
        link = next((x for x in pay["fields"] if x["type"] == "link" and x.get("table") == t["key"]), None)
        if link is None:
            raise Problem(f"{where}: {pay['label']} has no link to {t['label']}: add one first")
        amount = d.field(pay, self.amount)
        if amount is None or d.kind(amount) != "number":
            raise Problem(f"{where} › amount: {pay['label']} has no number field “{self.amount}”")
        total = d.field(t, self.total)
        if total is None or d.kind(total) != "number":
            raise Problem(f"{where} › total: {t['label']} has no number field “{self.total}” (its numbers: "
                          f"{', '.join(x['key'] for x in t['fields'] if d.kind(x) == 'number') or 'none'})")
        f, paid = _stage(d, t, self.paid_stage, where)
        made = Made()
        made.fields += [(t["key"], {"key": "paid", "label": "Paid", "type": "rollup", "table": pay["key"], "via": link["key"],
                                    "aggregate": "sum", "field": amount["key"]}),
                        (t["key"], {"key": "balance", "label": "Balance", "type": "formula", "expression": f"{total['key']} - paid", "format": "money"})]
        steps = [{"type": "update_record", "target": link["key"], "values": {f["key"]: paid},
                  "only_if": [{"field": f"{link['key']}.balance", "op": "at_most", "value": 0}]}]
        if self.part_paid_stage:
            _, part = _stage(d, t, self.part_paid_stage, where)
            steps.append({"type": "update_record", "target": link["key"], "values": {f["key"]: part},
                          "only_if": [{"field": f"{link['key']}.balance", "op": "greater_than", "value": 0},
                                      {"field": f"{link['key']}.paid", "op": "greater_than", "value": 0}]})
        made.rules.append({"key": self.feature_key(), "name": f"Mark {t['label'].lower()} {paid.lower()} when paid",
                           "trigger": {"type": "record_created_or_updated", "table": pay["key"]}, "conditions": [], "actions": steps})
        return made

    def checks(self, d):
        t, pay = d.list(self.list), d.list(self.payments)
        link = next(x for x in pay["fields"] if x["type"] == "link" and x.get("table") == t["key"])
        f = d.stage_field(t)
        made = setting(d, t, d.field(t, self.total)["key"], 1000, "i1")
        if made is None:
            return []
        values, after = made
        amount = d.field(pay, self.amount)["key"]
        out = [{"title": f"{_a(t['singular'])} paid in full is {d.choice(f, self.paid_stage)}",
                "steps": adding(d, t, "i1", values) + after + adding(d, pay, "p1", {link["key"]: "i1", amount: 10 ** 7}),
                "expect": [{"record": "i1", "field": f["key"], "equals": d.choice(f, self.paid_stage)}]}]
        if self.part_paid_stage:
            values2, after2 = setting(d, t, d.field(t, self.total)["key"], 1000, "i2")
            out.append({"title": f"Part paid is {d.choice(f, self.part_paid_stage)}",
                        "steps": adding(d, t, "i2", values2) + after2 + adding(d, pay, "p2", {link["key"]: "i2", amount: 1}),
                        "expect": [{"record": "i2", "field": f["key"], "equals": d.choice(f, self.part_paid_stage)}]})
        return out

    def said(self, d):
        return f"keeps each {self.list.rstrip('s')}'s balance from its payments and marks it {self.paid_stage}"


@register("feature", {"do": "limit", "list": "bookings", "per": ["date"], "max": 6, "only_if": [{"field": "status", "op": "not_equals", "value": "Cancelled"}]})
class Limit(Feature):
    """At most max records may share the same values of per (six bookings a day; one per room per night), counting
    only those that match only_if; with per left empty, at most max of them in all (five active cases at a time)."""
    do: Literal["limit"] = "limit"
    list: str
    per: list[str] = Field(default_factory=list)
    max: int
    only_if: list[Condition] = Field(default_factory=list)
    message: str | None = None

    def default_key(self):
        return f"limit_{self.list}_{'_'.join(self.per)}"

    def build(self, d, where):
        t = _t(d, self.list, where)
        per = []
        stage = d.stage_field(t)
        for name in self.per:
            f = d.field(t, name)
            if f is None:
                raise Problem(f"{where} › per: {name} isn't a field of {t['label']} (its fields: {', '.join(d.field_names(t))})")
            if stage is not None and f["key"] == stage["key"]:
                raise Problem(f"{where} › per: a limit isn't per stage (every new {t['singular'].lower()} starts at the same one): "
                              f"to count only those at some stages, leave per empty (or per a date) and give only_if "
                              f"{{\"field\": \"{stage['key']}\", \"op\": \"one_of\", \"value\": [the stages that count]}}")
            per.append(f["key"])
        limit = {"per": per, "max": self.max}
        if self.only_if:
            limit["where"] = ir_conditions(d, t, self.only_if, f"{where} › only_if")
        limit["message"] = self.message or (f"There can be at most {self.max} {t['label'].lower()} for the same {', '.join(per)}" if per
                                            else f"There can be at most {self.max} {t['label'].lower()} at a time")
        entry = next((e for e in d.bp.get("features", []) if e["key"] == self.feature_key()), None)
        mine = (entry or {}).get("made", {}).get("limit")
        others = [x for x in t.get("limits") or [] if x != mine]
        made = Made()
        made.ops.append({"op": "update_table", "table": t["key"], "limits": others + [limit]})
        made.undo = {"made": {"limit": limit}}
        return made

    def checks(self, d):
        t = d.list(self.list)
        stage = d.stage_field(t)
        start = ((t.get("workflow") or {}).get("start") or (stage.get("options") or [None])[0]) if stage else None
        same, counted = {}, None
        for i, name in enumerate(self.per):
            f = d.field(t, name)
            same[f["key"]] = "today+5" if d.kind(f) == "date" else _sample(d, f, i, "same")
        for c in self.only_if:  # the ones counted: a stage they're moved to (every new one starts at the first), or a value
            if stage is not None and schema.slug(c.field) == stage["key"]:
                stages = [s for s in (c.value if isinstance(c.value, list) else [c.value]) if s] if c.op in ("equals", "one_of") else \
                    [s for s in stage.get("options") or [] if s != c.value] if c.op == "not_equals" else []
                if start not in stages and stages:
                    counted = d.choice(stage, stages[0])
            elif c.op == "equals":
                same[schema.slug(c.field)] = c.value
        steps, made = [], {}
        for n in range(1, self.max + 2):
            steps += adding(d, t, f"b{n}", dict(same), made=made)
            if counted:
                steps.append({"do": "move", "record": f"b{n}", "to": counted})
        steps[-1]["refused"] = True
        what = f"for the same {', '.join(self.per)}" if self.per else "at a time"
        expect = [{"record": f"b{self.max + 1}", "field": stage["key"], "not_equals": counted}] if counted else \
            [{"table": t["key"], "count": self.max + sum(1 for s in steps if s.get("table") == t["key"]) - (self.max + 1)}]
        return [{"title": f"No more than {self.max} {t['label'].lower()} {what}", "steps": steps, "expect": expect}]

    def said(self, d):
        return f"allows at most {self.max} {self.list}" + (f" per {', '.join(self.per)}" if self.per else " at a time")


@register("feature", {"do": "convert", "from_list": "quotes", "to_list": "jobs", "when_stage": "Accepted", "from_lines": "quote_lines", "to_lines": "job_lines"})
class Convert(Feature):
    """When a record gets to a stage (or its button is pressed), make a record of another list from it, linked back,
    with the fields they share copied (and its lines copied to the new one's lines, if given)."""
    do: Literal["convert"] = "convert"
    from_list: str
    to_list: str
    when_stage: str | None = None
    button: str | None = None
    from_lines: str | None = None
    to_lines: str | None = None
    values: list[Value] = Field(default_factory=list)

    def default_key(self):
        return f"convert_{self.from_list}_{self.to_list}"

    def build(self, d, where):
        from .compile import ir_values
        src = _t(d, self.from_list, where, "from_list")
        dst = _t(d, self.to_list, where, "to_list")
        made, key = Made(), self.feature_key()
        if not self.when_stage and not self.button:
            raise Problem(f"{where}: say when: when_stage (a stage of {src['key']}) or button (its label)")
        back = next((x for x in dst["fields"] if x["type"] == "link" and x.get("table") == src["key"]), None)
        back_key = back["key"] if back else schema.slug(src.get("singular") or src["key"])
        if back is None:
            made.fields.append((dst["key"], {"key": back_key, "label": src.get("singular") or src["label"], "type": "link", "table": src["key"]}))
            d = Design(copy.deepcopy(d.bp))
            schema.table(d.bp, dst["key"])["fields"].append({"key": back_key, "label": src["singular"], "type": "link", "table": src["key"]})
            dst = d.list(dst["key"])
        values = {back_key: "{{record}}", **ir_values(d, src, dst, self.values, where)}
        actions = [{"type": "create_record", "table": dst["key"], "values": values, "copy_matching": True}]
        if self.from_lines or self.to_lines:
            lines_of = lambda t: [x["key"] for x in d.lists if any(f["type"] == "link" and f.get("table") == t["key"] for f in x["fields"]) and x.get("embedded")]  # noqa: E731
            hint = (f" ({src['key']}'s lines: {', '.join(lines_of(src)) or 'none'}; {dst['key']}'s lines: {', '.join(lines_of(dst)) or 'none'}; "
                    f"to copy just the record, leave out from_lines and to_lines)")
            if not self.from_lines or not self.to_lines:
                raise Problem(f"{where}: give both from_lines and to_lines, or neither" + hint)
            fl, tl = d.list(self.from_lines), d.list(self.to_lines)
            if fl is None or tl is None:
                raise Problem(f"{where}: {self.from_lines if fl is None else self.to_lines} isn't a list" + hint)
            via = next((x for x in fl["fields"] if x["type"] == "link" and x.get("table") == src["key"]), None)
            up = next((x for x in tl["fields"] if x["type"] == "link" and x.get("table") == dst["key"]), None)
            if via is None or up is None:
                raise Problem(f"{where}: from_lines must be {src['key']}'s lines and to_lines {dst['key']}'s" + hint)
            actions.append({"type": "create_for_each", "table": tl["key"], "source": {"table": fl["key"], "via": via["key"]},
                            "values": {up["key"]: "{{created}}"}, "copy_matching": True})
        if self.when_stage:
            f, stage = _stage(d, src, self.when_stage, where)
            trigger, conds = {"type": "record_created_or_updated", "table": src["key"]}, [{"field": f["key"], "op": "changed_to", "value": stage}]
            name = f"Make {_a(dst['singular'].lower())} when {_a(src['singular'].lower())} is {stage}"
        else:
            trigger, conds = {"type": "button", "table": src["key"], "label": self.button[:40]}, []
            name = self.button
        made.rules.append({"key": key, "name": name, "trigger": trigger, "conditions": conds, "actions": actions})
        return made

    def checks(self, d):
        src, dst = d.list(self.from_list), d.list(self.to_list)
        steps = adding(d, src, "s1")
        if self.from_lines:
            fl = d.list(self.from_lines)
            via = next(x for x in fl["fields"] if x["type"] == "link" and x.get("table") == src["key"])
            steps += adding(d, fl, "s1_line", {via["key"]: "s1"})
        if self.when_stage:
            steps.append({"do": "move", "record": "s1", "to": d.choice(d.stage_field(src), self.when_stage)})
        else:
            steps.append({"do": "press", "record": "s1", "button": self.button})
        expect = [{"table": dst["key"], "count": 1}]
        if self.to_lines:
            expect.append({"table": d.list(self.to_lines)["key"], "count": 1})
        return [{"title": f"{_a(src['singular'])} makes {_a(dst['singular'].lower())}", "steps": steps, "expect": expect}]

    def said(self, d):
        return f"makes {_a(self.to_list.rstrip('s'))} from {_a(self.from_list.rstrip('s'))}"


@register("feature", {"do": "send_document", "list": "invoices", "button": "Send invoice", "email_to": "customer.email", "then_stage": "Sent"})
class SendDocument(Feature):
    """A button that makes the record's document (a PDF; the list's own, or a standard one made for it), emails it to
    an email field, and moves the record to then_stage."""
    do: Literal["send_document"] = "send_document"
    list: str
    button: str
    email_to: str
    document: str | None = None
    then_stage: str | None = None
    subject: str | None = None
    body: str | None = None

    def default_key(self):
        return f"send_{self.list}"

    def build(self, d, where):
        t = _t(d, self.list, where)
        made = Made()
        docs = {x["key"]: x for x in t.get("documents", [])}
        key = schema.slug(self.document) if self.document else (next(iter(docs)) if docs else schema.slug(t["singular"]))
        if key not in docs:
            made.ops.append({"op": "add_document", "table": t["key"], "document": {"key": key, "label": t["singular"], "title": t["singular"]}})
        to = _email_to(d, t, self.email_to, where)
        actions = [{"type": "generate_document", "document": key},
                   {"type": "send_email", "to": to, "subject": self.subject or f"{t['singular']} {_title(t)}",
                    "body": self.body or f"Please find your {t['singular'].lower()} {_title(t)} attached.", "attach": [key]}]
        if self.then_stage:
            f, stage = _stage(d, t, self.then_stage, where)
            actions.append({"type": "update_record", "values": {f["key"]: stage}})
        made.rules.append({"key": self.feature_key(), "name": self.button, "trigger": {"type": "button", "table": t["key"], "label": self.button[:40]},
                           "conditions": [], "actions": actions})
        return made

    def checks(self, d):
        t = d.list(self.list)
        steps = adding(d, t, "r1", through=_through(self.email_to))
        try:
            f, _, keys = d.path(t, self.email_to)
        except NameProblem:
            f = None
        if f is not None and len(keys) == 1:
            steps[-1]["values"][keys[0]] = "r1@example.com"
        steps.append({"do": "press", "record": "r1", "button": self.button})
        expect = [{"told": True, "record": "r1"}]
        if self.then_stage:
            expect.append({"record": "r1", "field": d.stage_field(t)["key"], "equals": d.choice(d.stage_field(t), self.then_stage)})
        return [{"title": f"“{self.button}” sends the {t['singular'].lower()}", "steps": steps, "expect": expect}]

    def said(self, d):
        return f"“{self.button}” emails the {self.list.rstrip('s')} as a PDF"


@register("feature", {"do": "reminder_before", "list": "appointments", "date_field": "starts_on", "days": 1, "email_to": "client.email",
                      "message": "A reminder: your appointment is tomorrow."})
class ReminderBefore(Feature):
    """Remind someone some days before a record's date (an appointment tomorrow, a renewal in 30 days): tell in the
    app, or email_to an email field. Once per record."""
    do: Literal["reminder_before"] = "reminder_before"
    list: str
    date_field: str
    days: int
    tell: str = "owner"
    email_to: str | None = None
    message: str | None = None
    only_if: list[Condition] = Field(default_factory=list)

    def default_key(self):
        return f"remind_{self.list}_{self.date_field}"

    def build(self, d, where):
        t = _t(d, self.list, where)
        df = d.field(t, self.date_field)
        if df is None or d.kind(df) != "date":
            raise Problem(f"{where} › date_field: {self.date_field} isn't a date field of {t['label']}")
        made = Made()
        flag = schema.slug(f"reminded_{df['key']}")
        made.fields.append((t["key"], {"key": flag, "label": f"Reminded ({df['label']})", "type": "date"}))
        conds = [{"field": df["key"], "op": "within_next_days", "value": self.days}, {"field": df["key"], "op": "after_today"},
                 {"field": flag, "op": "is_empty"}] + ir_conditions(d, t, self.only_if, f"{where} › only_if")
        message = self.message or f"{_title(t)}: {df['label']} is in {self.days} day{'s' if self.days != 1 else ''}"
        made.rules.append({"key": self.feature_key(), "name": f"Remind {self.days} day{'s' if self.days != 1 else ''} before {df['label'].lower()}",
                           "trigger": {"type": "schedule", "table": t["key"], "every": "day", "at": "08:00"}, "conditions": conds,
                           "actions": [_tell_step(d, t, self.tell, self.email_to, f"Reminder: {t['singular']} {_title(t)}", message, where),
                                       {"type": "update_record", "values": {flag: "{{today()}}"}}]})
        return made

    def checks(self, d):
        t = d.list(self.list)
        df = d.field(t, self.date_field)
        if df["type"] not in schema.STORED:
            return []
        extra = {schema.slug(c.field): c.value for c in self.only_if if c.op == "equals"}
        return [{"title": f"A reminder goes {self.days} day{'s' if self.days != 1 else ''} before the {df['label'].lower()}",
                 "steps": adding(d, t, "r1", {df["key"]: f"today+{self.days + 3}", **extra}, through=_through(self.email_to)) + [{"do": "wait", "days": 3}, {"do": "run", "rule": self.feature_key()}],
                 "expect": [{"told": True, "record": "r1"}]},
                {"title": "No reminder while it's further off",
                 "steps": adding(d, t, "r2", {df["key"]: f"today+{self.days + 10}", **extra}, through=_through(self.email_to)) + [{"do": "run", "rule": self.feature_key()}],
                 "expect": [{"told": False, "record": "r2"}]}]

    def said(self, d):
        return f"reminds {self.email_to or self.tell} {self.days} days before each {self.list.rstrip('s')}'s {self.date_field}"


@register("feature", {"do": "recurring", "from_list": "members", "to_list": "invoices", "every": "month",
                      "only_if": [{"field": "status", "op": "equals", "value": "Active"}]})
class Recurring(Feature):
    """Every week or month, make a record of to_list for each record of from_list that matches only_if (a monthly
    invoice for each active member), linked to it, with the fields they share copied and these values."""
    do: Literal["recurring"] = "recurring"
    from_list: str
    to_list: str
    every: Literal["week", "month"] = "month"
    only_if: list[Condition] = Field(default_factory=list)
    values: list[Value] = Field(default_factory=list)

    def default_key(self):
        return f"recurring_{self.to_list}"

    def build(self, d, where):
        from .compile import ir_values
        src = _t(d, self.from_list, where, "from_list")
        dst = _t(d, self.to_list, where, "to_list")
        made = Made()
        back = next((x for x in dst["fields"] if x["type"] == "link" and x.get("table") == src["key"]), None)
        back_key = back["key"] if back else schema.slug(src.get("singular") or src["key"])
        if back is None:
            made.fields.append((dst["key"], {"key": back_key, "label": src.get("singular") or src["label"], "type": "link", "table": src["key"]}))
            d = Design(copy.deepcopy(d.bp))
            schema.table(d.bp, dst["key"])["fields"].append({"key": back_key, "label": src["singular"], "type": "link", "table": src["key"]})
            dst = d.list(dst["key"])
        trigger = {"type": "schedule", "table": src["key"], "every": self.every, "at": "07:00"}
        if self.every == "week":
            trigger["weekday"] = "mon"
        made.rules.append({"key": self.feature_key(), "name": f"Make {dst['label'].lower()} every {self.every}", "trigger": trigger,
                           "conditions": ir_conditions(d, src, self.only_if, f"{where} › only_if"),
                           "actions": [{"type": "create_record", "table": dst["key"], "copy_matching": True,
                                        "values": {back_key: "{{record}}", **ir_values(d, src, dst, self.values, where)}}]})
        return made

    def checks(self, d):
        src, dst = d.list(self.from_list), d.list(self.to_list)
        extra = {schema.slug(c.field): c.value for c in self.only_if if c.op == "equals"}
        return [{"title": f"Each {self.every}, {_a(dst['singular'].lower())} is made for each one", "steps": adding(d, src, "s1", extra)
                 + [{"do": "run", "rule": self.feature_key()}], "expect": [{"table": dst["key"], "count": 1}]}]

    def said(self, d):
        return f"makes {self.to_list} for each {self.from_list.rstrip('s')} every {self.every}"


@register("feature", {"do": "lock", "list": "invoices", "stages": ["Sent", "Paid"]})
class Lock(Feature):
    """Records at these stages can't be changed (except by the owner): a sent invoice stays as it was sent."""
    do: Literal["lock"] = "lock"
    list: str
    stages: list[str]

    def default_key(self):
        return f"lock_{self.list}"

    def build(self, d, where):
        t = _t(d, self.list, where)
        f = d.stage_field(t)
        if f is None:
            raise Problem(f"{where}: {t['label']} has no stages yet: give it stages first (set_stages)")
        found = []
        for s in self.stages:
            x = d.choice(f, s)
            if x is None:
                raise Problem(f"{where} › stages: “{s}” isn't a stage of {t['label']} ({', '.join(f.get('options') or [])})")
            found.append(x)
        made = Made()
        made.ops.append({"op": "update_table", "table": t["key"], "lock_if": [{"field": f["key"], "op": "one_of", "value": found}]})
        entry = next((e for e in d.bp.get("features", []) if e["key"] == self.feature_key()), None)
        made.undo = {"lock_if": {t["key"]: (entry or {}).get("undo", {}).get("lock_if", {}).get(t["key"], t.get("lock_if") or [])}}
        return made

    def checks(self, d):
        t = d.list(self.list)
        f = d.stage_field(t)
        stage = d.choice(f, self.stages[0])
        editable = next((x for x in t["fields"] if x["type"] in ("text", "long_text", "number", "money", "date") and x["key"] != f["key"]), None)
        if editable is None:
            return []
        value = "today+30" if d.kind(editable) == "date" else 999 if d.kind(editable) == "number" else "Changed after"
        return [{"title": f"{_a(t['singular'])} at {stage} can't be changed by staff",
                 "steps": adding(d, t, "r1") + [{"do": "move", "record": "r1", "to": stage},
                                                 {"do": "change", "record": "r1", "by": "staff", "values": {editable["key"]: value}, "refused": True}],
                 "expect": [{"record": "r1", "field": f["key"], "equals": stage}]}]

    def said(self, d):
        return f"locks {self.list} once they're {', '.join(self.stages)}"


ApprovalNeeded.model_rebuild()
ALL = items("feature")


# --- applying and removing ------------------------------------------------------------------------------------------

def _entry(bp, key):
    return next((e for e in bp.get("features", []) if e["key"] == key), None)


def apply(out, p, where):
    """Apply one feature (new, or changed: same key) to the outcome's design, all of it or none of it."""
    d = Design(out.blueprint)
    key = p.feature_key()
    made = p.build(d, where)
    ir_ops = []
    wanted_rules = set()
    for a in made.rules:
        a = {**a, "feature": key}
        wanted_rules.add(a["key"])
        ir_ops.append({"op": "add_automation", "automation": a})
    wanted_fields = set()
    for tk, f in made.fields:
        f = {**f, "feature": f.get("feature", key)}
        t = schema.table(out.blueprint, tk)
        have = schema.field(t, f["key"]) if t else None
        wanted_fields.add((tk, f["key"]))
        if have is None:
            ir_ops.append({"op": "add_field", "table": tk, "field": f})
        elif {k: v for k, v in have.items() if k != "feature"} != {k: v for k, v in f.items() if k != "feature"} and have.get("feature") in (None, key):
            ir_ops.append({"op": "update_field", "table": tk, "key": f["key"], "changes": f})
    ir_ops += made.ops
    for a in out.blueprint.get("automations", []):  # what this feature made before and doesn't now
        if a.get("feature") == key and a["key"] not in wanted_rules:
            ir_ops.append({"op": "remove_automation", "key": a["key"]})
    for t in out.blueprint.get("tables", []):
        for f in t.get("fields", []):
            if f.get("feature") == key and (t["key"], f["key"]) not in wanted_fields:
                ir_ops.append({"op": "remove_field", "table": t["key"], "key": f["key"]})
    if not _apply_ops(out, ir_ops, where):
        return False
    entries = [e for e in out.blueprint.get("features", []) if e["key"] != key]
    was = _entry(out.blueprint, key)
    undo = (was or {}).get("undo") or {}
    for k, v in (made.undo or {}).items():
        if k == "made":
            continue
        undo.setdefault(k, {})
        for tk, original in v.items():
            undo[k].setdefault(tk, original)
    entry = {"key": key, "do": p.do, "params": p.model_dump(exclude_none=True, by_alias=True, exclude={"key"}), "undo": undo}
    if (made.undo or {}).get("made"):
        entry["made"] = made.undo["made"]
    entries.append(entry)
    out.blueprint["features"] = entries
    out.changes.append(f"Set up: {p.said(Design(out.blueprint)) or key}")
    return True


def remove(out, key, where):
    entry = _entry(out.blueprint, key)
    if entry is None:
        keys = [e["key"] for e in out.blueprint.get("features", [])]
        raise Problem(f"{where}: there's no feature “{key}” (features: {', '.join(keys) or 'none'})")
    ir_ops = [{"op": "remove_automation", "key": a["key"]} for a in out.blueprint.get("automations", []) if a.get("feature") == key]
    ir_ops += [{"op": "remove_field", "table": t["key"], "key": f["key"]} for t in out.blueprint.get("tables", [])
               for f in t.get("fields", []) if f.get("feature") == key]
    undo = entry.get("undo") or {}
    for tk, transitions in (undo.get("workflow") or {}).items():
        t = schema.table(out.blueprint, tk)
        if t and t.get("workflow"):
            ir_ops.append({"op": "update_table", "table": tk, "workflow": {**t["workflow"], "transitions": transitions}})
    for tk, lock in (undo.get("lock_if") or {}).items():
        ir_ops.append({"op": "update_table", "table": tk, "lock_if": lock})
    mine = (entry.get("made") or {}).get("limit")
    if mine:
        for t in out.blueprint.get("tables", []):
            if mine in (t.get("limits") or []):
                ir_ops.append({"op": "update_table", "table": t["key"], "limits": [x for x in t["limits"] if x != mine]})
    if not _apply_ops(out, ir_ops, where):
        return False
    out.blueprint["features"] = [e for e in out.blueprint.get("features", []) if e["key"] != key]
    out.changes.append(f"Took away: {key}")
    return True


def model_of(entry):
    cls = next((c for c in ALL if c.model_fields["do"].default == entry.get("do")), None)
    return cls.model_validate({**entry.get("params", {}), "key": entry["key"]}) if cls else None


def checks(bp):
    """Every feature's own checks for this design, each marked with its feature."""
    d, out = Design(bp), []
    for entry in bp.get("features", []):
        p = model_of(entry)
        if p is None:
            continue
        try:
            for c in p.checks(d):
                out.append({**c, "by": f"feature:{entry['key']}"})
        except Exception:  # noqa: BLE001 - a feature that can't write its checks for this design has none, said elsewhere
            continue
    return out
