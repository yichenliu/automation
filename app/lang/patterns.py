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
import re
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


DONE_WORDS = {"sent", "done", "paid", "sold", "won", "lost", "built", "held", "made", "taken", "given", "written", "booked", "set",
              "shut", "cut", "put", "read", "left", "kept", "met", "brought", "bought", "caught", "taught", "seen", "drawn", "shown"}


def stamp_label(stage):
    """The label of the day a record got to a stage: “Sent on” for what was done to it, “Unpaid since” for how it is (so
    nobody reads “Unpaid on” as “still unpaid as of”)."""
    last = str(stage).strip().split()[-1].lower() if str(stage).strip() else ""
    done = last.endswith("ed") or last in DONE_WORDS
    return f"{stage} on" if done else f"{stage} since"


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


def _wanted(d, t, keep=()):
    """What the list's rules (add_validation) ask of a record, where it can be said simply: [(path, value)], a path being
    the field's key, or a link's key then the linked record's ("quote.status == 'Approved'": (["quote", "status"],
    "Approved")). `keep`: more of the same, asked by whoever adds it."""
    import ast
    out = list(keep)

    def path(node):
        if isinstance(node, ast.Name):
            return [node.id]
        if isinstance(node, ast.Attribute):
            head = path(node.value)
            return head + [node.attr] if head else None
        return None

    def const(node):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub) and isinstance(node.operand, ast.Constant):
            return -node.operand.value
        if isinstance(node, ast.Name) and node.id in ("True", "False", "true", "false"):
            return node.id.lower() == "true"
        if isinstance(node, (ast.List, ast.Tuple)) and node.elts and isinstance(node.elts[0], ast.Constant):
            return [e.value for e in node.elts if isinstance(e, ast.Constant)]
        return KeyError  # not a constant

    turned = {ast.Gt: ast.Lt, ast.Lt: ast.Gt, ast.GtE: ast.LtE, ast.LtE: ast.GtE, ast.Eq: ast.Eq, ast.NotEq: ast.NotEq}

    def walk(node):
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And):
            for v in node.values:
                walk(v)
        elif isinstance(node, ast.Compare) and len(node.ops) == 1:
            left, op, right = node.left, type(node.ops[0]), node.comparators[0]
            if path(left) is None or const(right) is KeyError:
                left, right, op = right, left, turned.get(op)
            p, v = path(left), const(right)
            if p is None or v is KeyError or op is None:
                return
            if op is ast.Eq:
                out.append((p, v))
            elif op is ast.In and isinstance(v, list) and v:
                out.append((p, v[0]))
            elif op in (ast.GtE, ast.LtE) and isinstance(v, (int, float)):
                out.append((p, v))
            elif op is ast.Gt and isinstance(v, (int, float)):
                out.append((p, v + 1))
            elif op is ast.Lt and isinstance(v, (int, float)):
                out.append((p, v - 1))
            elif op is ast.NotEq:
                out.append((p, ("not", v)))
    for rule in t.get("rules") or []:
        if str(rule.get("check") or "").strip().lower() in ("", "false"):
            continue
        try:
            walk(expr.parse(schema.percents_fixed(t, rule["check"])).body)
        except Exception:  # noqa: BLE001 - a rule that can't be read simply: left to the record's own values
            continue
    return out


def _keeping(d, f, value):
    """A value of the field that keeps to what a rule asks (("not", x): anything but x)."""
    if isinstance(value, tuple) and value[:1] == ("not",):
        if f["type"] == "select":
            return next((o for o in f.get("options") or [] if str(o).lower() != str(value[1]).lower()), None)
        if f["type"] == "checkbox":
            return not bool(value[1])
        if d.kind(f) == "number" and isinstance(value[1], (int, float)):
            return value[1] + 1
        return None
    if f["type"] == "select":
        return d.choice(f, str(value)) or value
    return value


def adding(d, t, alias, values=None, by=None, depth=0, made=None, through=(), keep=(), chain=()):
    """Steps that add a valid record of t as `alias`: its required fields filled in (a record it must link to added
    first, however far that goes: an invoice's order's contract's quote's enquiry's client; and one it links to
    `through` even if it needn't: the customer an email goes to), what the list's rules ask of it (a job's quote
    Approved: that quote added so), and these values. `keep`: more such asks, from the record that links to it.
    `chain`: the lists being added on the way here (a list that must link back to one of them can't be added)."""
    steps, values = [], dict(values or {})
    chain = tuple(chain) + (t["key"],)
    through = tuple(((d.field(t, k) or {}).get("link") or k) if (d.field(t, k) or {}).get("type") == "lookup" else k
                    for k in through)  # an email worked out from the customer's: that customer added
    made = made if made is not None else {}
    flow_field = (t.get("workflow") or {}).get("field")
    asked, onward = {}, {}  # its own fields' values the rules ask for; and, by link, what they ask of the linked record
    for p, v in _wanted(d, t, keep):
        f = d.field(t, p[0])
        if f is None or f["key"] in values:
            continue
        if len(p) == 1 and f["type"] in schema.STORED and f["type"] != "link":
            got = _keeping(d, f, v)
            if got is not None:
                asked.setdefault(f["key"], got)
        elif len(p) > 1 and f["type"] == "link" and not f.get("multiple"):
            onward.setdefault(f["key"], []).append((p[1:], v))
    values.update(asked)
    for i, f in enumerate(t.get("fields", [])):
        if f["key"] in values or f["type"] in schema.CALCULATED | schema.FILES or f["key"] == flow_field:
            continue
        if not f.get("required") and not f.get("required_if") and not (f["type"] == "link" and (f["key"] in through or f["key"] in onward)):
            if f["type"] == "email":  # an address to send to, so what emails it can be seen to
                values[f["key"]] = f"{alias}@example.com"
            continue
        if f["type"] == "link":
            other = d.list(f.get("table"))
            if other is None or other["key"] in chain or depth > 8:
                continue
            if other["key"] in made and f["key"] not in onward:
                values[f["key"]] = made[other["key"]]
                continue
            other_alias = f"{alias}_{other['key'][:8]}"
            steps += adding(d, other, other_alias, depth=depth + 1, made=made, keep=onward.get(f["key"], ()), chain=chain)
            made.setdefault(other["key"], other_alias)
            values[f["key"]] = other_alias
        else:
            values[f["key"]] = _sample(d, f, i, alias)
            if f["type"] in ("date", "datetime"):  # a day apart, in the order they come: a contract ends after it starts
                values[f["key"]] = f"today+{7 + sum(1 for g in t['fields'][:i] if g['type'] in ('date', 'datetime') and g['key'] in values)}"
    start = (t.get("workflow") or {}).get("start") or next(iter(d.stages(t) or []), None) if flow_field else None
    staged = asked.get(flow_field) if flow_field in asked and asked.get(flow_field) != start else None
    if staged is not None:  # a record starts at its first stage: taken to the one asked for by its buttons, as people would
        values.pop(flow_field, None)
    step = {"do": "add", "table": t["key"], "as": alias, "values": values}
    if by:
        step["by"] = by
    return steps + [step] + ([{"do": "move", "record": alias, "to": staged}] if staged is not None else [])


def holding(d, f, c, yes):
    """A value of a field people fill in that keeps to the condition (yes) or doesn't, for a check to write; None when
    it can't be said for sure."""
    if f is None or f["type"] not in schema.STORED:
        return None
    kind, op, v = d.kind(f), c.op, c.value
    if op in ("is_empty", "not_empty"):
        filled = _sample(d, f, 0, "x") if kind != "date" else "today"
        return "" if (op == "is_empty") == yes else filled  # "": left empty
    if kind == "date":
        n = int(v or 0) if isinstance(v, (int, float)) or str(v or "").lstrip("-").isdigit() else 0
        table = {"before_today": ("today-1", "today+1"), "after_today": ("today+1", "today-1"), "today": ("today", "today+3"),
                 "within_next_days": ("today+1", f"today+{n + 5}"), "within_last_days": ("today-1", f"today-{n + 5}"),
                 "more_than_days_ago": (f"today-{n + 2}", "today")}
        pair = table.get(op)
        return pair[0 if yes else 1] if pair else None
    if kind == "number":
        try:
            n = float(v)
        except (TypeError, ValueError):
            return None
        table = {"greater_than": (n + 1, n - 1), "less_than": (n - 1, n + 1), "at_least": (n, n - 1), "at_most": (n, n + 1),
                 "equals": (n, n + 1), "not_equals": (n + 1, n)}
        pair = table.get(op)
        return pair[0 if yes else 1] if pair else None
    if kind == "yes_no" and op in ("equals", "not_equals"):
        want = v in (True, "true", "yes", 1)
        return want if (op == "equals") == yes else (not want)
    if kind == "choice" and op in ("equals", "not_equals", "one_of"):
        values = v if isinstance(v, list) else [v]
        others = [o for o in f.get("options") or [] if o not in values]
        inside = d.choice(f, values[0]) if values else None
        if op == "not_equals":
            return (others[0] if others else None) if yes else inside
        return inside if yes else (others[0] if others else None)
    return None


def _through(path):
    """The link an email path goes through ("customer" for customer.email; a field worked out from one, such as a
    lookup of the customer's email, goes through that customer), for a check to fill it in."""
    return (str(path).split(".")[0],) if path else ()


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
    field is above an amount (anyone may below it). Who moved it there and when is recorded. tell_approver: tell them
    when one is waiting."""
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
        slug = schema.slug(stage)  # who let it through and when, filled in by the move itself (the engine's stamps)
        mine = self.feature_key()  # what it made itself last time is still its own to make, never someone else's to keep
        if not any(d.kind(x) == "user" and x["key"].endswith("_by") and x.get("feature") != mine for x in t["fields"]):
            made.fields.append((t["key"], {"key": f"{slug}_by", "label": f"{stage} by", "type": "user"}))
        if not any(d.kind(x) == "date" and x["key"] in (f"{slug}_on", f"{slug}_date", f"date_{slug}") and x.get("feature") != mine
                   for x in t["fields"]):
            made.fields.append((t["key"], {"key": f"{slug}_on", "label": f"{stage} on", "type": "date"}))
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
    tell in the app or email_to an email field. Once, unless every_days repeats it. A list with no stages: since alone,
    some days after that date (a check-up six months after the last visit: since last_visit, days 182)."""
    do: Literal["chase"] = "chase"
    list: str
    stage: str | None = Field(None, description="the stage it's still at; left out on a list with no stages (then since)")
    days: int
    since: str | None = Field(None, description="a date field to count from; left out: the day it got to the stage")
    tell: str = "owner"
    email_to: str | None = None
    message: str | None = None
    every_days: int | None = None

    def default_key(self):
        return f"chase_{self.list}_{self.stage or self.since}"

    def _what(self, d, t, where):
        """(stage field, stage) or (None, None) for a chase from a date alone."""
        if self.stage:
            return _stage(d, t, self.stage, where)
        if not self.since:
            raise Problem(f"{where}: say the stage it's still at (stage), or on a list with no stages the date to count from (since)")
        return None, None

    def build(self, d, where):
        t = _t(d, self.list, where)
        f, stage = self._what(d, t, where)
        if self.days < 1 or (self.every_days is not None and self.every_days < 1):
            raise Problem(f"{where} › days: a chase counts whole days, 1 or more (it looks once a day); to tell someone "
                          f"straight away when a record gets to {stage or 'a stage'}, use stage_alert")
        made, key = Made(), self.feature_key()
        if self.since:
            sf = d.field(t, self.since)
            if sf is None or d.kind(sf) != "date":
                raise Problem(f"{where} › since: {self.since} isn't a date field of {t['label']}")
            since = sf["key"]
        else:
            since = schema.slug(f"{stage}_on")
            made.fields.append((t["key"], {"key": since, "label": stamp_label(stage), "type": "date", "help": f"The day it got to {stage}."}))
            made.rules.append({"key": f"{key}_stamp", "name": f"Note the day {_a(t['singular'].lower())} gets to {stage}",
                               "trigger": {"type": "record_created_or_updated", "table": t["key"]},
                               "conditions": [{"field": f["key"], "op": "changed_to", "value": stage}],
                               "actions": [{"type": "update_record", "values": {since: "{{today()}}", schema.slug(f"chased_{stage}"): None}}]})
        last = schema.slug(f"chased_{stage or since}")
        made.fields.append((t["key"], {"key": last, "label": f"Last reminded ({stage or (schema.field(t, since) or {}).get('label') or since})",
                                       "type": "date"}))
        conds = ([{"field": f["key"], "op": "equals", "value": stage}] if stage else []) + \
            [{"field": since, "op": "more_than_days_ago", "value": max(self.days - 1, 0)}]
        if self.every_days:
            conds.append({"any": [{"field": last, "op": "is_empty"}, {"field": last, "op": "more_than_days_ago", "value": max(self.every_days - 1, 0)}]})
        elif stage:
            conds.append({"field": last, "op": "is_empty"})
        else:  # once for each date: reminded before that date came round, or never
            conds.append({"any": [{"field": last, "op": "is_empty"}, {"formula": f"{last} < {since}"}]})
        label = (schema.field(t, since) or {"label": since.replace("_", " ")})["label"].lower()
        message = self.message or (f"{_title(t)} has been {stage} for {self.days} days" if stage else f"{_title(t)}: {self.days} days since {label}")
        made.rules.append({"key": key, "name": f"Chase {t['label'].lower()} still {stage} after {self.days} days" if stage else
                           f"Remind {self.days} days after {t['singular'].lower()}’s {label}",
                           "trigger": {"type": "schedule", "table": t["key"], "every": "day", "at": "08:00"}, "conditions": conds,
                           "actions": [_tell_step(d, t, self.tell, self.email_to, f"Reminder: {t['singular'].lower()} {_title(t)}", message, where),
                                       {"type": "update_record", "values": {last: "{{today()}}"}}]})
        return made

    def checks(self, d):
        t = d.list(self.list)
        stage = d.choice(d.stage_field(t), self.stage) if self.stage else None
        values = {}
        if self.since:
            values[d.field(t, self.since)["key"]] = "today"
        moved = [{"do": "move", "record": "r1", "to": stage}] if stage else []
        steps = adding(d, t, "r1", values, through=_through(self.email_to)) + moved
        out = [{"title": f"{_a(t['singular'].lower()).capitalize()} still {stage} after {self.days} days is chased" if stage else
                f"{_a(t['singular'].lower()).capitalize()} is reminded about {self.days} days on",
                "steps": steps + [{"do": "wait", "days": self.days}, {"do": "run", "rule": self.feature_key()}],
                "expect": [{"told": True, "record": "r1"}]}]
        if self.days >= 2:
            out.append({"title": f"One {stage} for less than {self.days} days isn't chased yet" if stage else
                        f"One less than {self.days} days on isn't reminded yet",
                        "steps": adding(d, t, "r2", dict(values), through=_through(self.email_to))
                        + ([{"do": "move", "record": "r2", "to": stage}] if stage else [])
                        + [{"do": "wait", "days": self.days - 1}, {"do": "run", "rule": self.feature_key()}],
                        "expect": [{"told": False, "record": "r2"}]})
        return out

    def said(self, d):
        if not self.stage:
            return f"reminds {self.days} days after {self.list}' {self.since}"
        return f"chases {self.list} still {self.stage} after {self.days} days"


@register("feature", {"do": "overdue", "list": "invoices", "date_field": "due_date", "from_stages": ["Sent"], "to_stage": "Overdue"})
class Overdue(Feature):
    """Each day, move records from these stages to a late stage (made if it isn't there yet) once days_after have gone
    by since their date_field (a due date), or, with no date_field, since they got to the first of from_stages ("30
    days after we send it": from_stages Sent, days_after 30). tell: who to tell, if anyone."""
    do: Literal["overdue"] = "overdue"
    list: str
    date_field: str | None = Field(None, description="the date to count from; left out: the day it got to the first of from_stages")
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
        froms = []
        for s in self.from_stages:
            found = d.choice(f, s)
            if found is None:
                raise Problem(f"{where} › from_stages: “{s}” isn't a stage of {t['label']} ({', '.join(f.get('options') or [])})")
            froms.append(found)
        made = Made()
        if self.date_field:
            df = d.field(t, self.date_field)
            if df is None or d.kind(df) != "date":
                raise Problem(f"{where} › date_field: {self.date_field} isn't a date field of {t['label']} (its dates: "
                              f"{', '.join(x['key'] for x in t['fields'] if d.kind(x) == 'date') or 'none'}); leave it out to count "
                              f"from the day it got to {froms[0] if froms else 'its stage'}")
        else:  # the day it got to the first of its stages, noted as it gets there
            df = {"key": schema.slug(f"{froms[0]}_on"), "label": stamp_label(froms[0]), "type": "date", "help": f"The day it got to {froms[0]}."}
            if d.field(t, df["key"]) is None or (d.field(t, df["key"]) or {}).get("feature") == self.feature_key():
                made.fields.append((t["key"], df))
            made.rules.append({"key": f"{self.feature_key()}_stamp", "name": f"Note the day {_a(t['singular'].lower())} gets to {froms[0]}",
                               "trigger": {"type": "record_created_or_updated", "table": t["key"]},
                               "conditions": [{"field": f["key"], "op": "changed_to", "value": froms[0]}],
                               "actions": [{"type": "update_record", "values": {df["key"]: "{{today()}}"}}]})
        late = d.choice(f, self.to_stage)
        entry = next((e for e in d.bp.get("features", []) if e["key"] == self.feature_key()), None)
        if late is None or entry is None:  # placed when it's made: after that, where the owner puts the stages is kept
            options = [o for o in f.get("options") or [] if o != late]
            at = max(options.index(s) for s in froms) + 1
            placed = options[:at] + [late or self.to_stage.strip()] + options[at:]  # straight after the ones it comes from
            if placed != list(f.get("options") or []):
                late = late or self.to_stage.strip()
                made.ops.append({"op": "update_field", "table": t["key"], "key": f["key"], "changes": {**f, "options": placed}})
                made.undo = {"options": {t["key"]: f.get("options")}}
        flow = copy.deepcopy(t.get("workflow") or {})
        if flow.get("transitions"):  # a late one moves on as the ones it came from do (an overdue invoice can still be paid)
            original = (entry or {}).get("undo", {}).get("workflow", {}).get(t["key"]) or flow["transitions"]
            moves = copy.deepcopy(original)
            for mv in moves:
                if set(mv.get("from") or []) & set(froms) and late not in mv["from"] and mv["to"] != late:
                    mv["from"] = list(mv["from"]) + [late]
            flow["transitions"] = moves
            made.ops.append({"op": "update_table", "table": t["key"], "workflow": flow})
            made.undo = {**made.undo, "workflow": {t["key"]: original}}
        # on the day days_after have gone by (30 days after it's sent: on the 30th day), or the day after the date itself
        cond_date = {"field": df["key"], "op": "before_today"} if not self.days_after else \
            {"field": df["key"], "op": "more_than_days_ago", "value": self.days_after - 1}
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
        late = d.choice(f, self.to_stage) or self.to_stage
        start = d.choice(f, self.from_stages[0])
        move = [{"do": "move", "record": "r1", "to": start}] if start != ((t.get("workflow") or {}).get("start") or (f.get("options") or [None])[0]) else []
        if not self.date_field:  # counted from the day it got to its stage
            out = [{"title": f"{_a(t['singular'])} still {start} {self.days_after or 1} days on becomes {late}",
                    "steps": adding(d, t, "r1") + move + [{"do": "wait", "days": self.days_after or 1}, {"do": "run", "rule": self.feature_key()}],
                    "expect": [{"record": "r1", "field": f["key"], "equals": late}]}]
            if self.days_after >= 2:
                out.append({"title": f"One {start} for less than {self.days_after} days stays {start}",
                            "steps": adding(d, t, "r2") + [{**s, "record": "r2"} for s in move]
                            + [{"do": "wait", "days": self.days_after - 1}, {"do": "run", "rule": self.feature_key()}],
                            "expect": [{"record": "r2", "field": f["key"], "equals": start}]})
            return out
        df = d.field(t, self.date_field)
        due = {df["key"]: "today+2"} if df["type"] in schema.STORED else {}
        # a date worked out from others (due 14 days after it's issued) can't be written in: the example waits for it
        wait = {"do": "wait", "days": 3 + self.days_after} if due else {"do": "wait", "record": "r1", "until": df["key"], "days": max(self.days_after, 1)}
        out = [{"title": f"{_a(t['singular'])} past its {df['label'].lower()} becomes {late}",
                "steps": adding(d, t, "r1", due) + move + [wait, {"do": "run", "rule": self.feature_key()}],
                "expect": [{"record": "r1", "field": f["key"], "equals": late}]}]
        if not due:
            out.append({"title": f"One on its {df['label'].lower()} stays {start}",
                        "steps": adding(d, t, "r2") + [{**s, "record": "r2"} for s in move]
                        + [{"do": "wait", "record": "r2", "until": df["key"], "days": max(self.days_after, 1) - 1}, {"do": "run", "rule": self.feature_key()}],
                        "expect": [{"record": "r2", "field": f["key"], "equals": start}]})
        if due:
            move2 = [{**s, "record": "r2"} for s in move]
            out.append({"title": f"One not yet due stays {start}",
                        "steps": adding(d, t, "r2", {df["key"]: "today+10"}) + move2 + [{"do": "wait", "days": 3}, {"do": "run", "rule": self.feature_key()}],
                        "expect": [{"record": "r2", "field": f["key"], "equals": start}]})
        return out

    def said(self, d):
        if not self.date_field:
            return f"marks {self.list} {self.to_stage} once they've been {', '.join(self.from_stages)} for {self.days_after} days"
        return f"marks {self.list} {self.to_stage} once their {self.date_field} has passed" + (f" by {self.days_after} days" if self.days_after else "")


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
    only_if: list[Condition | str] = Field(default_factory=list)
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
        same, counted, first = {}, None, []
        for i, name in enumerate(self.per):
            f = d.field(t, name)
            if f["type"] == "lookup" and d.field(t, f.get("link") or ""):  # the job's date: shared by sharing the job
                f = d.field(t, f["link"])
            if f["key"] in same:
                continue
            if f["type"] == "link":  # the record they share is added first (the technician they're all booked with)
                other = d.list(f.get("table"))
                if other is None:
                    return []
                first += adding(d, other, f"same_{other['key'][:8]}")
                same[f["key"]] = f"same_{other['key'][:8]}"
            else:
                same[f["key"]] = "today+5" if d.kind(f) == "date" else _sample(d, f, i, "same")
        for c in self.only_if:  # the ones counted: a stage they're moved to (every new one starts at the first), or a value
            if stage is not None and schema.slug(c.field) == stage["key"]:
                stages = [s for s in (c.value if isinstance(c.value, list) else [c.value]) if s] if c.op in ("equals", "one_of") else \
                    [s for s in stage.get("options") or [] if s != c.value] if c.op == "not_equals" else []
                if start not in stages and stages:
                    counted = d.choice(stage, stages[0])
            elif c.op == "equals":
                same[schema.slug(c.field)] = c.value
        steps, made = list(first), {}
        for n in range(1, self.max + 2):
            steps += adding(d, t, f"b{n}", dict(same), made=made)
            if counted:
                steps.append({"do": "move", "record": f"b{n}", "to": counted})
        steps[-1]["refused"] = True
        named = [schema.lc((d.field(t, n) or {}).get("label") or n) for n in self.per]  # in the owner's words, not keys
        what = f"for the same {' and '.join(named)}" if self.per else "at a time"
        expect = [{"record": f"b{self.max + 1}", "field": stage["key"], "not_equals": counted}] if counted else \
            [{"table": t["key"], "count": self.max + sum(1 for s in steps if s.get("table") == t["key"]) - (self.max + 1)}]
        return [{"title": f"No more than {self.max} {t['label'].lower()} {what}", "steps": steps, "expect": expect}]

    def said(self, d):
        t = d.list(self.list) or {"label": self.list, "fields": []}
        named = [schema.lc((d.field(t, n) or {}).get("label") or n) for n in self.per]  # in the owner's words, not keys
        return f"allows at most {self.max} {schema.lc(t['label'])}" + (f" for the same {' and '.join(named)}" if self.per else " at a time")


@register("feature", {"do": "no_overlap", "list": "appointments", "per": ["client"], "starts": "starts_on"})
class NoOverlap(Feature):
    """No double-booking: no two records at overlapping times among those with the same per (a stylist booked once at
    a time: per stylist, starts starts_at, length duration; a room once a night: per room, starts check_in, ends
    check_out; per left empty: none may overlap at all). starts: a date, date-and-time or time field; then ends: the
    field it ends at, or length: a duration field of how long each takes (neither: each takes its day or moment). The
    day one ends, the next can start. only_if: the ones that count (status not_equals Cancelled)."""
    do: Literal["no_overlap"] = "no_overlap"
    list: str
    per: list[str] = Field(default_factory=list)
    starts: str
    ends: str | None = None
    length: str | None = None
    only_if: list[Condition | str] = Field(default_factory=list)
    message: str | None = None

    def default_key(self):
        return f"no_overlap_{self.list}" + "".join(f"_{p}" for p in self.per)

    def _timed(self, d, t, name, what, kinds, where):
        f = d.field(t, name)
        if f is None or f["type"] not in kinds:
            have = [x["key"] for x in t["fields"] if x["type"] in kinds]
            raise Problem(f"{where} › {what}: {name} isn't a {' or '.join(kinds)} field of {t['label']}"
                          + (f" (its {' or '.join(kinds)} fields: {', '.join(have)})" if have else f": add one first"))
        return f["key"]

    def build(self, d, where):
        t = _t(d, self.list, where)
        per = []
        for name in self.per:
            f = d.field(t, name)
            if f is None:
                raise Problem(f"{where} › per: {name} isn't a field of {t['label']} (its fields: {', '.join(d.field_names(t))})")
            per.append(f["key"])
        if self.ends and self.length:
            raise Problem(f"{where}: give ends (when each ends) or length (how long each takes), not both")
        overlap = {"start": self._timed(d, t, self.starts, "starts", ("date", "datetime", "time"), where)}
        if self.ends:
            overlap["end"] = self._timed(d, t, self.ends, "ends", ("date", "datetime", "time"), where)
        if self.length:
            overlap["length"] = self._timed(d, t, self.length, "length", ("duration",), where)
        limit = {"per": per, "overlap": overlap}
        if self.only_if:
            limit["where"] = ir_conditions(d, t, self.only_if, f"{where} › only_if")
        named = " and ".join(schema.lc((d.field(t, k) or {}).get("label") or k) for k in per)
        limit["message"] = self.message or (f"That time overlaps another {schema.lc(t['singular'])}"
                                            + (f" for the same {named}" if named else "") + ": pick another time")
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
        same, counted, first = {}, None, []
        for i, name in enumerate(self.per):
            f = d.field(t, name)
            if f["type"] == "link":  # the one they're all booked with, added first
                other = d.list(f.get("table"))
                if other is None:
                    return []
                first += adding(d, other, f"same_{other['key'][:8]}")
                same[f["key"]] = f"same_{other['key'][:8]}"
            elif f["type"] in schema.STORED:
                same[f["key"]] = _sample(d, f, i, "same")
            else:
                return []
        for c in self.only_if:  # the ones counted: a stage they're moved to, or a value
            if stage is not None and schema.slug(c.field) == stage["key"]:
                stages = [s for s in (c.value if isinstance(c.value, list) else [c.value]) if s] if c.op in ("equals", "one_of") else \
                    [s for s in stage.get("options") or [] if s != c.value] if c.op == "not_equals" else []
                if start not in stages and stages:
                    counted = d.choice(stage, stages[0])
            elif c.op == "equals":
                same[schema.slug(c.field)] = c.value
        kind = d.field(t, self.starts)["type"]

        def at(day):
            values = {d.field(t, self.starts)["key"]: "10:00" if kind == "time" else f"today+{day}"}
            if self.ends:
                values[d.field(t, self.ends)["key"]] = "11:00" if kind == "time" else f"today+{day + 1}"
            if self.length:
                values[d.field(t, self.length)["key"]] = 1  # an hour
            return values
        steps, made = list(first), {}
        for alias, day in (("b1", 5), ("b2", 5), ("b3", 8)):
            if kind == "time" and alias == "b3":
                continue  # a time of day: any other day is told apart by per
            added = adding(d, t, alias, {**same, **at(day)}, made=made)
            steps += added
            if counted:
                steps.append({"do": "move", "record": alias, "to": counted})
            if alias == "b2":
                (steps[-1] if counted else next(s for s in reversed(steps) if s.get("as") == "b2"))["refused"] = True
        named = [schema.lc((d.field(t, n) or {}).get("label") or n) for n in self.per]
        what = f" for the same {' and '.join(named)}" if named else ""
        adds = sum(1 for s in steps if s.get("do") == "add" and s.get("table") == t["key"])
        expect = [{"record": "b2", "field": stage["key"], "not_equals": counted}] if counted else \
            [{"table": t["key"], "count": adds - 1}]
        return [{"title": f"Two {t['label'].lower()} at the same time{what} aren't allowed", "steps": steps, "expect": expect}]

    def said(self, d):
        return f"no two {self.list} at overlapping times" + (f" per {', '.join(self.per)}" if self.per else "")


@register("feature", {"do": "allocate", "list": "bookings", "field": "staff_member", "among": [{"field": "role", "op": "equals", "value": "Driver"}],
                      "free_on": "date"})
class AllocateFeature(Feature):
    """The app picks who or what does each record, from a list (a driver for each job, two helpers, a room for each
    booking, a cleaner for each clinic visit): fills the link field with how_many of the linked list's records that
    match among, free that free_on day (not on another of this list's records that day, in any of its links to that
    list), the one on the fewest so far first; when one is added, or when it gets to when_stage (Confirmed). What people
    filled in themselves is left; too few free: the owner is told to pick. The link field is made if it isn't there
    (pick_from: the list it picks from)."""
    do: Literal["allocate"] = "allocate"
    list: str
    field: str
    how_many: int = Field(1, ge=1, le=50)
    among: list[Condition | str] = Field(default_factory=list, description="which may be picked (role equals Driver)")
    free_on: str | None = Field(None, description="a date field of list: those on another record that day aren't free")
    when_stage: str | None = Field(None, description="left out: when one is added")
    pick_from: str | None = Field(None, description="the list to pick from, when the link field isn't there yet")
    only_if: list[Condition | str] = Field(default_factory=list)

    def default_key(self):
        return f"allocate_{self.list}_{self.field}"

    def _field(self, d, t, where):
        f = d.field(t, self.field)
        if f is not None:
            if f["type"] != "link" or d.list(f.get("table")) is None:
                raise Problem(f"{where} › field: {self.field} isn't a link field of {t['label']} (its links: "
                              f"{', '.join(x['key'] for x in t['fields'] if x['type'] == 'link') or 'none'}): name one, or a new "
                              f"field's key with pick_from, the list to pick from")
            return f, None
        if not self.pick_from:
            raise Problem(f"{where} › field: {self.field} isn't a field of {t['label']}: give pick_from (the list to pick from) to "
                          f"make it, or name its link field ({', '.join(x['key'] for x in t['fields'] if x['type'] == 'link') or 'none yet'})")
        other = _t(d, self.pick_from, where, "pick_from")
        f = {"key": schema.slug(self.field), "label": schema.label_of(schema.slug(self.field)), "type": "link", "table": other["key"]}
        if self.how_many > 1:
            f["multiple"] = True
        return f, f

    def build(self, d, where):
        t = _t(d, self.list, where)
        f, new = self._field(d, t, where)
        other = d.list(f["table"])
        made = Made()
        if new:
            made.fields.append((t["key"], new))
        elif self.how_many > 1 and not f.get("multiple"):
            made.fields.append((t["key"], {**f, "multiple": True}))  # two helpers: it holds several
        action = {"type": "allocate", "field": f["key"], "count": self.how_many}
        if self.among:
            action["where"] = ir_conditions(d, other, self.among, f"{where} › among")
        if self.free_on:
            df = d.field(t, self.free_on)
            if df is None or d.kind(df) != "date":
                raise Problem(f"{where} › free_on: {self.free_on} isn't a date field of {t['label']} (its dates: "
                              f"{', '.join(x['key'] for x in t['fields'] if d.kind(x) == 'date') or 'none'})")
            action["free_on"] = df["key"]
        conds = [{"field": f["key"], "op": "is_empty"}] + ir_conditions(d, t, self.only_if, f"{where} › only_if")
        trigger = {"type": "record_created", "table": t["key"]}
        when = f"each new {t['singular'].lower()}"
        if self.when_stage:
            stage = d.stage_field(t)
            got = d.choice(stage, self.when_stage) if stage else None
            if got is None:
                raise Problem(f"{where} › when_stage: “{self.when_stage}” isn't a stage of {t['label']}"
                              + (f" ({', '.join(stage.get('options') or [])})" if stage else ": it has no stages yet"))
            trigger = {"type": "record_created_or_updated", "table": t["key"]}
            conds.insert(0, {"field": stage["key"], "op": "changed_to", "value": got})
            when = f"{_a(t['singular'].lower())} when it gets to {got}"
        many = "" if self.how_many == 1 else f"{self.how_many} "
        made.rules.append({"key": self.feature_key(), "name": f"Pick {many or 'the '}{f['label'].lower()} for {when}"[:80],
                           "trigger": trigger, "conditions": conds, "actions": [action]})
        return made

    def checks(self, d):
        t = d.list(self.list)
        f = d.field(t, self.field) or {"key": schema.slug(self.field), "label": schema.label_of(schema.slug(self.field)),
                                         "table": schema.slug(self.pick_from or "")}
        other = d.list(f.get("table"))
        if other is None or any(isinstance(c, str) or c.op not in ("equals", "one_of") for c in self.among):
            return []  # which ones may be picked isn't something an example can make for sure: the builder's examples prove it
        kind = {schema.slug(c.field): (c.value[0] if isinstance(c.value, list) else c.value) for c in self.among}
        n, made = self.how_many, {}
        steps = []
        for i in range(1, 2 * n + 1):  # enough for two records, each picked its own
            steps += adding(d, other, f"c{i}", dict(kind), made=made)
        df = d.field(t, self.free_on) if self.free_on else None
        day = {df["key"]: "today+5"} if df is not None and df["type"] in schema.STORED else {}
        if df is not None and not day:
            return []
        stage = d.stage_field(t) if self.when_stage else None
        to = d.choice(stage, self.when_stage) if stage else None
        for alias in ("r1", "r2"):
            steps += adding(d, t, alias, dict(day), made=made)
            if to:
                steps.append({"do": "move", "record": alias, "to": to})
        first, then = [f"c{i}" for i in range(1, n + 1)], [f"c{i}" for i in range(n + 1, 2 * n + 1)]
        one = (lambda xs: xs) if n > 1 else (lambda xs: xs[0])
        what = f"{other['label'].lower()}" if n > 1 else f"{other['singular'].lower()}"
        title = (f"Each {t['singular'].lower()}" + (f" that gets to {to}" if to else " added") + f" is given {'its own ' if day else 'in turn a '}"
                 f"{what}" + (" free that day" if day else ""))
        return [{"title": title, "steps": steps,
                 "expect": [{"record": "r1", "field": f["key"], "equals": one(first)}, {"record": "r2", "field": f["key"], "equals": one(then)}]}]

    def said(self, d):
        return (f"picks {self.how_many} of {self.pick_from or 'the linked list'} for each {self.list.rstrip('s')}'s {self.field}"
                + (f", free that {self.free_on}" if self.free_on else ""))


@register("feature", {"do": "convert", "from_list": "quotes", "to_list": "jobs", "when_stage": "Accepted", "from_lines": "quote_lines", "to_lines": "job_lines"})
class Convert(Feature):
    """Make a record of another list from a record, linked back, with the fields they share copied (and its lines
    copied to the new one's lines, if given): when it gets to when_stage; or when its button is pressed; with both,
    by its button, shown once it's at that stage (a completed job's Invoice job button)."""
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
        if self.when_stage and not self.button:
            f, stage = _stage(d, src, self.when_stage, where)
            trigger, conds = {"type": "record_created_or_updated", "table": src["key"]}, [{"field": f["key"], "op": "changed_to", "value": stage}]
            name = f"Make {_a(dst['singular'].lower())} when {_a(src['singular'].lower())} is {stage}"
        else:  # its button; with a stage, shown once it's there
            trigger, conds = {"type": "button", "table": src["key"], "label": self.button[:40]}, []
            if self.when_stage:
                f, stage = _stage(d, src, self.when_stage, where)
                conds = [{"field": f["key"], "op": "equals", "value": stage}]
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
        if self.button:
            steps.append({"do": "press", "record": "s1", "button": self.button})
        expect = [{"table": dst["key"], "count": 1}]
        if self.to_lines:
            expect.append({"table": d.list(self.to_lines)["key"], "count": 1})
        return [{"title": f"{_a(src['singular'])} makes {_a(dst['singular'].lower())}", "steps": steps, "expect": expect}]

    def said(self, d):
        return f"makes {_a(self.to_list.rstrip('s'))} from {_a(self.from_list.rstrip('s'))}"


@register("feature", {"do": "send_document", "list": "invoices", "button": "Send invoice", "email_to": "customer.email", "then_stage": "Sent"})
class SendDocument(Feature):
    """Makes the record's document (a PDF; the list's own, or a standard one made for it) and emails it to an email
    field: with then_stage, whenever a record gets to that stage (its button is the move to it, which keeps the
    stage's own checks); without, from a button of its own."""
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
        if self.then_stage:  # the move to the stage is the button, called as asked; getting there (however) sends it
            f, stage = _stage(d, t, self.then_stage, where)
            flow = copy.deepcopy(t.get("workflow") or {"field": f["key"], "transitions": []})
            entry = _entry(d.bp, self.feature_key())
            original = copy.deepcopy(((entry or {}).get("undo") or {}).get("workflow", {}).get(t["key"], flow.get("transitions") or []))
            moves = copy.deepcopy(original)
            into = [mv for mv in moves if mv["to"] == stage]
            for mv in into:  # “Send delivery note” is the button people press to get there
                mv["label"] = self.button[:60]
            if not into:
                options = list(f.get("options") or [])
                before = options[options.index(stage) - 1] if options.index(stage) > 0 else None
                moves.append({"from": [before] if before else [], "to": stage, "label": self.button[:60], "feature": self.feature_key()})
            if moves != (t.get("workflow") or {}).get("transitions"):
                made.ops.append({"op": "update_table", "table": t["key"], "workflow": {**flow, "transitions": moves}})
            if t.get("workflow"):  # kept over the list's moves when they're laid out anew, and given back when it's taken away
                made.undo = {"workflow": {t["key"]: original}}
            made.rules.append({"key": self.feature_key(), "name": f"Send the {t['singular'].lower()} when it's {stage}",
                               "trigger": {"type": "record_created_or_updated", "table": t["key"]},
                               "conditions": [{"field": f["key"], "op": "changed_to", "value": stage}], "actions": actions})
            return made
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
        if f is not None and len(keys) == 1 and f["type"] in schema.STORED:
            steps[-1]["values"][keys[0]] = "r1@example.com"
        expect = [{"told": True, "record": "r1"}]
        if self.then_stage:
            stage = d.choice(d.stage_field(t), self.then_stage)
            steps.append({"do": "move", "record": "r1", "to": stage})
            expect.append({"record": "r1", "field": d.stage_field(t)["key"], "equals": stage})
            return [{"title": f"{_a(t['singular'])} that gets to {stage} is sent", "steps": steps, "expect": expect}]
        steps.append({"do": "press", "record": "r1", "button": self.button})
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
    days: int = Field(ge=0, description="how many days before the date; 0: on the day itself")
    tell: str = "owner"
    email_to: str | None = None
    message: str | None = None
    only_if: list[Condition | str] = Field(default_factory=list)

    def default_key(self):
        return f"remind_{self.list}_{self.date_field}"

    def _ahead(self, label):
        return f"on the {label}" if not self.days else f"{self.days} day{'s' if self.days != 1 else ''} before the {label}"

    def build(self, d, where):
        t = _t(d, self.list, where)
        df = d.field(t, self.date_field)
        if df is None or d.kind(df) != "date":
            raise Problem(f"{where} › date_field: {self.date_field} isn't a date field of {t['label']}")
        made = Made()
        # each reminder keeps its own mark: one 30 days before and another on the day are both sent
        flag = schema.slug(f"reminded_{df['key']}" + (f"_{self.days}_days" if self.days else "_on_the_day"))
        made.fields.append((t["key"], {"key": flag, "label": f"Reminded {self._ahead(df['label'].lower())}", "type": "date"}))
        # the days before it, not the day itself; 0 days before: the day itself
        when = [{"field": df["key"], "op": "within_next_days", "value": self.days}, {"field": df["key"], "op": "after_today"}] \
            if self.days else [{"field": df["key"], "op": "today"}]
        conds = when + [{"field": flag, "op": "is_empty"}] + ir_conditions(d, t, self.only_if, f"{where} › only_if")
        message = self.message or (f"{_title(t)}: {df['label']} is in {self.days} day{'s' if self.days != 1 else ''}" if self.days
                                   else f"{_title(t)}: {df['label']} is today")
        made.rules.append({"key": self.feature_key(), "name": f"Remind {self._ahead(df['label'].lower())}",
                           "trigger": {"type": "schedule", "table": t["key"], "every": "day", "at": "08:00"}, "conditions": conds,
                           "actions": [_tell_step(d, t, self.tell, self.email_to, f"Reminder: {t['singular']} {_title(t)}", message, where),
                                       {"type": "update_record", "values": {flag: "{{today()}}"}}]})
        return made

    def checks(self, d):
        t = d.list(self.list)
        df = d.field(t, self.date_field)
        extra = {schema.slug(c.field): c.value for c in self.only_if if c.op == "equals"}
        title = f"A reminder goes {self._ahead(df['label'].lower())}"
        if df["type"] not in schema.STORED:  # worked out from others: the example waits for it to come round
            return [{"title": title, "steps": adding(d, t, "r1", extra, through=_through(self.email_to))
                     + [{"do": "wait", "record": "r1", "until": df["key"], "days": -self.days}, {"do": "run", "rule": self.feature_key()}],
                     "expect": [{"told": True, "record": "r1"}]}]
        return [{"title": title,
                 "steps": adding(d, t, "r1", {df["key"]: f"today+{self.days + 3}", **extra}, through=_through(self.email_to)) + [{"do": "wait", "days": 3}, {"do": "run", "rule": self.feature_key()}],
                 "expect": [{"told": True, "record": "r1"}]},
                {"title": "No reminder while it's further off",
                 "steps": adding(d, t, "r2", {df["key"]: f"today+{self.days + 10}", **extra}, through=_through(self.email_to)) + [{"do": "run", "rule": self.feature_key()}],
                 "expect": [{"told": False, "record": "r2"}]}]

    def said(self, d):
        return f"reminds {self.email_to or self.tell} {self._ahead(self.date_field)} of each {self.list.rstrip('s')}"


@register("feature", {"do": "recurring", "from_list": "members", "to_list": "invoices", "every": "month",
                      "only_if": [{"field": "status", "op": "equals", "value": "Active"}]})
class Recurring(Feature):
    """Every week or month, make a record of to_list for each record of from_list that matches only_if (a monthly
    invoice for each active member), linked to it, with the fields they share copied and these values."""
    do: Literal["recurring"] = "recurring"
    from_list: str
    to_list: str
    every: Literal["week", "month"] = "month"
    only_if: list[Condition | str] = Field(default_factory=list)
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


TURNED = {"equals": "not_equals", "not_equals": "equals", "is_empty": "not_empty", "not_empty": "is_empty",
          "greater_than": "at_most", "at_most": "greater_than", "less_than": "at_least", "at_least": "less_than"}


def _turned(conditions, where):
    """What holds when not all of these do: one of them turned round ({"any": [...]}, or the one condition itself)."""
    out = []
    for i, c in enumerate(conditions, start=1):
        if "formula" in c:  # a formula turned round: it doesn't hold
            out.append({"formula": _negated(c["formula"])})
            continue
        if "any" in c or "all" in c or c.get("op") not in TURNED:
            raise Problem(f"{where} {i}: “{c.get('op')}” can't be turned round to say when it's allowed: write what must hold "
                          f"for it to be allowed in only_if instead")
        out.append({**c, "op": TURNED[c["op"]]})
    return out[0] if len(out) == 1 else {"any": out}


# what a field made by a feature shows people, which they may change as they like: the feature keeps it when it's made again
LOOKS = ("label", "help", "placeholder", "unit", "decimals", "currency", "prefix", "digits", "option_colors", "before", "after",
         "yes_label", "no_label")

FLIPPED = {expr.ast.Eq: expr.ast.NotEq, expr.ast.NotEq: expr.ast.Eq, expr.ast.Lt: expr.ast.GtE, expr.ast.GtE: expr.ast.Lt,
           expr.ast.Gt: expr.ast.LtE, expr.ast.LtE: expr.ast.Gt, expr.ast.In: expr.ast.NotIn, expr.ast.NotIn: expr.ast.In}


def _negated(source):
    """A formula that holds when this one doesn't, as plainly as it can be said: a comparison flipped (status !=
    'Paid' for status == 'Paid'), a "not" taken off, else not (…)."""
    try:
        tree = expr.parse(source).body
    except expr.ExprError:
        return f"not ({source})"
    if isinstance(tree, expr.ast.UnaryOp) and isinstance(tree.op, expr.ast.Not):
        return expr.ast.unparse(tree.operand)
    if isinstance(tree, expr.ast.Compare) and len(tree.ops) == 1 and type(tree.ops[0]) in FLIPPED:
        return expr.ast.unparse(expr.ast.Compare(left=tree.left, ops=[FLIPPED[type(tree.ops[0])]()], comparators=tree.comparators))
    return f"not ({source})"


def _done_choice(f, where):
    """For a choice a gate needs (Deposit: Not received / Received): the one answer that says it's done, or None for
    anything that isn't a choice. More than one that could be: said, so the builder names which (only_if)."""
    if f is None or f.get("type") != "select" or not f.get("options"):
        return None
    done = [o for o in f["options"] if not schema.says_no(o) and not re.search(r"\b(pending|waiting|due|unpaid|outstanding|none|no|to do|todo|draft)\b", str(o), re.I)]
    if len(done) == 1:
        return done[0]
    raise Problem(f"{where} › needs: {f['key']} is a choice ({', '.join(f['options'])}), always filled in: say which answer it "
                  f"must be in only_if ({f['key']} equals …)")


def _words_of(d, t, conditions):
    from ..engine.automations import formula_words
    out = []
    for c in conditions:
        if isinstance(c, str):
            out.append(formula_words(c))
            continue
        f = d.field(t, c.field) if "." not in c.field else None
        name = (f or {}).get("label") or c.field.replace("_", " ").replace(".", "'s ")
        value = "" if c.value in (None, "") else f" {c.value}"
        out.append(f"{name} {c.op.replace('_', ' ')}{value}".strip())
    return " and ".join(out)


@register("feature", {"do": "gate", "list": "jobs", "stage": "In progress", "needs": ["assigned_to", "billable"]})
class Gate(Feature):
    """A record can't get to a stage (or a choice of another field: a payment status of Paid) until these fields are
    filled in (a yes/no field: ticked; a linked record's through dots, job.deposit_paid), only while only_if all hold,
    and never while blocked_if all hold, whoever moves it (the owner and the app's own rules too). "Can't assign a unit
    until the ID is checked": needs. "A steer under 450kg can't be signed off": blocked_if type equals Steer and weight
    less_than 450. "Only once the deposit is at least $50": only_if deposit at_least 50. A condition can be a formula,
    looking at other records: "not while the client owes on an invoice over 30 days": blocked_if ["count(client.invoices
    where status = 'Unpaid' and days_since(due_date) > 30) > 0"]; "not until its quote is accepted":
    only_if ["quote.status == 'Accepted'"]."""
    do: Literal["gate"] = "gate"
    list: str
    stage: str
    needs: list[str] = Field(default_factory=list)
    only_if: list[Condition | str] = Field(default_factory=list, description="it's allowed only while all of these hold")
    blocked_if: list[Condition | str] = Field(default_factory=list, description="it's refused while all of these hold")

    def default_key(self):
        return f"gate_{self.list}_{self.stage}"

    def _target(self, d, t, where):
        """(the field, the value): the list's stage, or else a choice of another of its fields with that name."""
        f = d.stage_field(t)
        found = d.choice(f, self.stage) if f else None
        if found is not None:
            return f, found
        for x in t["fields"]:
            if x is not f and d.kind(x) == "choice" and x["type"] == "select" and d.choice(x, self.stage) is not None:
                return x, d.choice(x, self.stage)
        stages = ", ".join(f.get("options") or []) if f else "none"
        raise Problem(f"{where}: “{self.stage}” isn't a stage of {t['label']} (its stages: {stages}), nor a choice of one of its fields")

    def build(self, d, where):
        t = _t(d, self.list, where)
        f, stage = self._target(d, t, where)
        fields, only_if = [], list(self.only_if)
        for name in self.needs:
            nf = d.field(t, name)
            if nf is None and re.search(r"[()<>=!]|\b(and|or|not)\b", name):  # a formula among what it needs: what must hold
                only_if.append(name)
                continue
            done = _done_choice(nf, where)
            if done is not None:  # a choice is always filled in (Not received): what it needs is its done answer
                only_if.append(Condition(field=nf["key"], op="equals", value=done))
                continue
            if nf is None and "." in name:  # a linked record's field (the job's invoice's deposit): filled in there first
                try:
                    pf, _, _ = d.path(t, name)
                except NameProblem:
                    pf = None
                if pf is not None and pf["type"] not in schema.CALCULATED:
                    only_if.append(Condition(field=name, op="equals", value=True) if pf["type"] == "checkbox" else
                                   Condition(field=name, op="not_empty"))
                    continue
            if nf is None:
                raise Problem(f"{where} › needs: {name} isn't a field of {t['label']} (its fields: {', '.join(d.field_names(t))})")
            if nf["type"] in schema.CALCULATED and nf["type"] not in ("formula", "rollup", "lookup"):
                raise Problem(f"{where} › needs: {nf['key']} is worked out, so nobody fills it in: name the fields people fill in")
            if nf["type"] in ("formula", "rollup", "lookup"):  # worked out (invoice_paid): what it works out to must hold
                yes = nf["type"] == "formula" and nf.get("format") == "checkbox"
                only_if.append(Condition(field=nf["key"], op="equals", value=True) if yes else
                               Condition(field=nf["key"], op="greater_than", value=0) if nf["type"] == "rollup" else
                               Condition(field=nf["key"], op="not_empty"))
                continue
            if nf.get("required") and nf["type"] != "checkbox":
                raise Problem(f"{where} › needs: {nf['key']} must be filled in to add {_a(t['singular'].lower())} at all, so it's always "
                              f"done before {self.stage}: if it's filled in later, make it not required (change_field); otherwise "
                              f"leave it out of needs")
            fields.append(nf)
        if not fields and not only_if and not self.blocked_if:
            raise Problem(f"{where}: say what must be done first: needs (fields filled in or ticked), only_if (what must hold) "
                          f"and/or blocked_if (what stops it)")
        ticks = [nf["key"] for nf in fields if nf["type"] == "checkbox"]
        filled = [nf["key"] for nf in fields if nf["type"] != "checkbox"]
        conditions = ir_conditions(d, t, only_if, f"{where} › only_if") if only_if else []
        blocked = ir_conditions(d, t, self.blocked_if, f"{where} › blocked_if") if self.blocked_if else []
        allowed = conditions + ([_turned(blocked, f"{where} › blocked_if")] if blocked else [])
        allowed = [c for i, c in enumerate(allowed) if c not in allowed[:i]]  # only_if and blocked_if saying the same: said once
        entry = next((e for e in d.bp.get("features", []) if e["key"] == self.feature_key()), None)
        mine = ((entry or {}).get("made") or {}).get("rules") or {}
        made = Made()
        rules = [r for r in t.get("rules") or [] if r not in (mine.get(t["key"]) or [])]  # what this gate made before goes
        flow = copy.deepcopy(t.get("workflow") or {})
        original = (entry or {}).get("undo", {}).get("workflow", {}).get(t["key"]) or flow.get("transitions") or []
        # what it holds back, as rules every save keeps: whoever makes the change (the owner, a rule, an import, the
        # board), never only the buttons of those who may press them
        on_stage = f is d.stage_field(t)
        at = {"field": f["key"], "op": "equals", "value": stage}
        one = _a(t["singular"].lower()) if on_stage else ""
        before = (lambda what: f"{what} before {one} can move to {stage}") if on_stage else (lambda what: f"{what} before {f['label']} is {stage}")
        new = [{"check": "false", "message": before(f"{nf['label']} must be filled in"),
                "when": [at, {"field": nf["key"], "op": "is_empty"}], "field": nf["key"]} for nf in fields if nf["key"] in filled]
        new += [{"check": "false", "message": before(f"{nf['label']} must be ticked"),
                 "when": [at, {"field": nf["key"], "op": "not_equals", "value": True}], "field": nf["key"]} for nf in fields if nf["key"] in ticks]
        if conditions:
            new.append({"check": "true", "holds": conditions, "message": (f"{one[:1].upper() + one[1:]} can only move to {stage} when "
                                                                          if on_stage else f"{f['label']} can only be {stage} when ")
                        + _words_of(d, t, only_if), "when": [at], "field": f["key"]})
        if blocked:
            new.append({"check": "false", "message": (f"{one[:1].upper() + one[1:]} can't move to {stage} while " if on_stage else
                                                      f"{f['label']} can't be {stage} while ") + _words_of(d, t, self.blocked_if),
                        "when": [at] + blocked, "field": f["key"]})
        made.ops.append({"op": "update_table", "table": t["key"], "rules": rules + new})
        if not on_stage or not any(mv["to"] == stage for mv in original):
            if (entry or {}).get("undo", {}).get("workflow", {}).get(t["key"]) is not None and flow:  # it gated a stage before
                made.ops.append({"op": "update_table", "table": t["key"], "workflow": {**flow, "transitions": original}})
            made.undo = {"made": {"rules": {t["key"]: new}}}
            return made
        moves = copy.deepcopy(original)  # its buttons say what they wait for, and ask for what must be filled in
        for mv in moves:
            if mv["to"] != stage:
                continue
            mv["feature"] = self.feature_key()
            if filled:
                mv["requires"] = list(dict.fromkeys(list(mv.get("requires") or []) + filled))
            if ticks or allowed:
                mv["only_if"] = list(mv.get("only_if") or []) + [{"field": k, "op": "equals", "value": True} for k in ticks] + allowed
        flow["transitions"] = moves
        made.ops.append({"op": "update_table", "table": t["key"], "workflow": flow})
        made.undo = {"workflow": {t["key"]: original}, "made": {"rules": {t["key"]: new}}}
        return made

    def checks(self, d):
        t = d.list(self.list)
        f, stage = self._target(d, t, "gate")
        on_stage = f is d.stage_field(t)
        empty, full, linked = {}, {}, []
        if any(d.field(t, name) is None for name in self.needs):
            return []  # what a linked record must have can't be written for sure (as a linked only_if)
        choices = [(name, _done_choice(d.field(t, name), "gate")) for name in self.needs]
        if any(done is not None for _, done in choices):  # a choice it needs is its done answer: checked as only_if
            return self.model_copy(update={"needs": [n for n, done in choices if done is None],
                                           "only_if": list(self.only_if) + [Condition(field=d.field(t, n)["key"], op="equals", value=done)
                                                                            for n, done in choices if done is not None]}).checks(d)
        for name in self.needs:
            nf = d.field(t, name)
            if nf["type"] == "checkbox":
                empty[nf["key"]] = False
        for i, name in enumerate(self.needs):
            nf = d.field(t, name)
            if nf["type"] == "link":  # a record of the list it links to, added first
                linked.append(nf["key"])
                continue
            full[nf["key"]] = True if nf["type"] == "checkbox" else ("today" if d.kind(nf) == "date" else _sample(d, nf, i, "r2"))
        for c in self.only_if:  # one that keeps to each condition, and (for the first) one that doesn't
            nf = d.field(t, c.field)
            yes, no = holding(d, nf, c, True), holding(d, nf, c, False)
            if nf is None or yes is None or (not self.needs and no is None):
                return []  # what keeps to it can't be written for sure (a linked record's field, a worked-out one)
            full[nf["key"]] = yes
            if no is not None:
                empty[nf["key"]] = no
        for i, c in enumerate(self.blocked_if):  # the first: all of them hold; the second: the first doesn't
            nf = d.field(t, c.field)
            yes, no = holding(d, nf, c, True), holding(d, nf, c, False)
            if nf is None or yes is None or no is None:
                return []
            if not self.only_if and not self.needs:
                empty[nf["key"]] = yes
            full[nf["key"]] = no if i == 0 else yes
        if on_stage:
            flow = t.get("workflow") or {}
            before = next((mv["from"][0] for mv in flow.get("transitions", []) if mv["to"] == stage and mv.get("from")), None)
            start = flow.get("start") or d.stages(t)[0]
            lead = [{"do": "move", "record": "{alias}", "to": before}] if before and before != start else []
            go = lambda r, **x: [{**s, "record": r} for s in lead] + [{"do": "move", "record": r, "to": stage, **x}]  # noqa: E731
        else:
            other = next((o for o in f.get("options") or [] if o != stage), None)
            if other is not None:
                empty.setdefault(f["key"], other)
                full.setdefault(f["key"], other)
            go = lambda r, **x: [{"do": "change", "record": r, "values": {f["key"]: stage}, **x}]  # noqa: E731
        steps1 = adding(d, t, "r1", empty) + go("r1", refused=True)
        steps2 = adding(d, t, "r2", full, through=tuple(linked)) + go("r2")
        for s_ in steps1:  # what the gate needs is left out of the first, whatever adding() would have filled in
            if s_.get("do") == "add" and s_.get("as") == "r1":
                for name in self.needs:
                    nf = d.field(t, name)
                    if nf["type"] != "checkbox":
                        s_["values"].pop(nf["key"], None)
        what = " and ".join(([f"{', '.join(self.needs)} {'is' if len(self.needs) == 1 else 'are'} done"] if self.needs else [])
                            + (["it's time"] if self.only_if else []) + ([f"it's not {_words_of(d, t, self.blocked_if)}"] if self.blocked_if else []))
        to = f"move to {stage}" if on_stage else f"be {stage}"
        return [{"title": f"{_a(t['singular'])} can't {to} until {what}",
                 "steps": steps1, "expect": [{"record": "r1", "field": f["key"], "not_equals": stage}]},
                {"title": f"Once {what}, it can {to}",
                 "steps": steps2, "expect": [{"record": "r2", "field": f["key"], "equals": stage}]}]

    def said(self, d):
        until = [f"{', '.join(self.needs)} {'is' if len(self.needs) == 1 else 'are'} done"] if self.needs else []
        if self.only_if:
            until.append(_words_of(d, d.list(self.list) or {"fields": []}, self.only_if))
        out = f"keeps {self.list} from {self.stage}" + (f" until {' and '.join(until)}" if until else "")
        if self.blocked_if:
            out += f" while {_words_of(d, d.list(self.list) or {'fields': []}, self.blocked_if)}"
        return out


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
        # moved there as its own button allows: by someone of the team it's for, never whoever raised it when that's
        # kept apart (a clause version approved by a second person)
        into = next((m for m in (t.get("workflow") or {}).get("transitions") or [] if m["to"] == stage), None) or {}
        team = next((w for w in into.get("who") or [] if w in d.teams()), None)
        mover = {"by": team} if team and into.get("not_by") else {}
        return [{"title": f"{_a(t['singular'])} at {stage} can't be changed by staff",
                 "steps": adding(d, t, "r1") + [{"do": "move", "record": "r1", "to": stage, **mover},
                                                 {"do": "change", "record": "r1", "by": "staff", "values": {editable["key"]: value}, "refused": True}],
                 "expect": [{"record": "r1", "field": f["key"], "equals": stage}]}]

    def said(self, d):
        return f"locks {self.list} once they're {', '.join(self.stages)}"


ApprovalNeeded.model_rebuild()
ALL = items("feature")


# --- applying and removing ------------------------------------------------------------------------------------------

def _entry(bp, key):
    return next((e for e in bp.get("features", []) if e["key"] == key), None)


def _staged(out, p, where):
    """An approval or a gate on a stage of a list whose status is still a plain choice (Draft, Sent, Paid, with no
    buttons between them): that choice becomes its stages first, in its order, a button on to each next (as set_stages
    without moves makes them), so there's a move to hold back. False if that couldn't be applied."""
    if p.do not in ("approval", "gate"):
        return True
    d = Design(out.blueprint)
    t = d.list(getattr(p, "list", None) or "")
    f = d.stage_field(t) if t is not None else None
    if f is None or ((t.get("workflow") or {}).get("transitions")) or d.choice(f, p.stage) is None or len(f.get("options") or []) < 2:
        return True
    names = list(f["options"])
    start = f.get("default") if f.get("default") in names else names[0]
    flow = {"field": f["key"], "start": start, "transitions": [{"from": [a], "to": b, "label": f"Move to {b}"[:60]} for a, b in zip(names, names[1:])]}
    return _apply_ops(out, [{"op": "update_table", "table": t["key"], "workflow": flow}], where)


def apply(out, p, where, quiet=False):
    """Apply one feature (new, or changed: same key) to the outcome's design, all of it or none of it. A changed one
    that adds to a list's moves is laid over them again with the others that do, in their order (see overlay)."""
    if not _staged(out, p, where):
        return False
    d = Design(out.blueprint)
    key = p.feature_key()
    again = _entry(out.blueprint, key) is not None and not quiet
    made = p.build(d, where)
    ir_ops = []
    wanted_rules = set()
    given_up = set((_entry(out.blueprint, key) or {}).get("given_up") or [])  # its rules someone has since made their own
    rules = []
    for a in made.rules:
        if a["key"] in given_up:
            continue
        a = {**a, "feature": key}
        wanted_rules.add(a["key"])
        rules.append({"op": "add_automation", "automation": a})
    wanted_fields = set()
    for tk, f in made.fields:
        f = {**f, "feature": f.get("feature", key)}
        t = schema.table(out.blueprint, tk)
        have = schema.field(t, f["key"]) if t else None
        wanted_fields.add((tk, f["key"]))
        if have is not None and have.get("feature") == key:  # how people set it to look (its label, units, help) stays theirs
            f = {**f, **{k: v for k, v in have.items() if k in LOOKS}}
        if have is None:
            ir_ops.append({"op": "add_field", "table": tk, "field": f})
        elif {k: v for k, v in have.items() if k != "feature"} != {k: v for k, v in f.items() if k != "feature"} and have.get("feature") in (None, key):
            ir_ops.append({"op": "update_field", "table": tk, "key": f["key"], "changes": f})
    # its rules after its fields: a rule reading a field the feature makes (when it last reminded) was placed while its
    # list didn't have it yet, and was moved to another list that did (two reminders before two lists' expiry dates)
    ir_ops += rules
    ir_ops += made.ops
    for a in out.blueprint.get("automations", []):  # what this feature made before and doesn't now
        if a.get("feature") == key and a["key"] not in wanted_rules:
            ir_ops.append({"op": "remove_automation", "key": a["key"]})
    read = _read_elsewhere(out.blueprint, key)
    for t in out.blueprint.get("tables", []):
        for f in t.get("fields", []):
            if f.get("feature") == key and (t["key"], f["key"]) not in wanted_fields:
                if re.search(rf"\b{re.escape(f['key'])}\b", read):  # another feature or rule reads it: it stays, unowned
                    f.pop("feature", None)
                else:
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
    if (was or {}).get("given_up"):
        entry["given_up"] = list(was["given_up"])
    if (made.undo or {}).get("made"):
        entry["made"] = made.undo["made"]
    if was is not None:  # in its place among the others
        at = [e["key"] for e in out.blueprint.get("features", [])].index(key)
        entries.insert(at, entry)
    else:
        entries.append(entry)
    out.blueprint["features"] = entries
    if again:
        for tk in list((entry.get("undo") or {}).get("workflow") or {}):
            later = _shapers(out.blueprint, tk)
            if later and later[-1]["key"] != key and not overlay(out, tk, base(out.blueprint, tk), where):
                return False
    if not quiet:
        out.changes.append(f"Set up: {p.said(Design(out.blueprint)) or key}")
    return True


# --- features that add to a list's moves, kept over them --------------------------------------------------------------
# A gate's stops, an approval's approver, an overdue stage's way on: each is laid over the list's moves as they were
# when it was made. When the moves change (new stages, a stage renamed, a feature before it changed or taken away),
# they're laid over the moves again, in their order, from the moves as they'd be without any of them: a feature never
# quietly loses its parts, nor another's.

def _shapers(bp, tk):
    """The features that add to a list's moves, in the order they were made."""
    return [e for e in bp.get("features", []) if tk in ((e.get("undo") or {}).get("workflow") or {})]


def base(bp, tk):
    """A list's moves as they'd be without the features that add to them."""
    mine = _shapers(bp, tk)
    return copy.deepcopy(mine[0]["undo"]["workflow"][tk]) if mine else None


def overlay(out, tk, transitions, where, skip=()):
    """Lay the features that add to a list's moves over these moves, in their order. False (said in out.problems) when
    one can't be any more."""
    t = schema.table(out.blueprint, tk)
    keys = [e["key"] for e in _shapers(out.blueprint, tk) if e["key"] not in skip]
    if t is None or not _apply_ops(out, [{"op": "update_table", "table": tk, "workflow": {**(t.get("workflow") or {}), "transitions": transitions}}], where):
        return False
    for key in keys:
        e = _entry(out.blueprint, key)
        e["undo"]["workflow"].pop(tk, None)
        try:
            ok = apply(out, model_of(e), where, quiet=True)
        except Problem as exc:
            out.problems.append(f"{where}: it leaves the feature {key} without what it needs ({exc}); to go on without it, "
                                f"remove_feature {key}")
            return False
        if not ok:
            return False
    return True


LAYOUT = ("set_stages", "add_stage", "rename_choice")  # patches that lay a list's moves out anew: its features kept over them


def kept(out, before, p, where, translate):
    """After a patch that's not a feature: the features that add to a list's moves it changed are laid over them again.
    A new layout (set_stages, add_stage, a stage renamed) keeps them; another patch that would take away what one does
    is refused, saying how to change or remove the feature instead. False (said in out.problems): undo the patch."""
    from .compile import Outcome
    for tk in {tk for e in before.get("features", []) for tk in ((e.get("undo") or {}).get("workflow") or {})}:
        tb, ta = schema.table(before, tk), schema.table(out.blueprint, tk)
        if tb is None or ta is None or not ta.get("workflow"):
            continue
        sf = (ta.get("workflow") or {}).get("field")
        if (tb.get("workflow") == ta.get("workflow") and (schema.field(tb, sf) or {}).get("options") == (schema.field(ta, sf) or {}).get("options")):
            continue
        keys = [e["key"] for e in _shapers(before, tk)]
        if p.do == "rename_choice":  # the features name the stage by its new name
            for e in out.blueprint.get("features", []):
                if e["key"] in keys:
                    e["params"] = _renamed(e["params"], p.old if hasattr(p, "old") else None, getattr(p, "new", None))
        trial = Outcome(blueprint=copy.deepcopy(before))
        schema.table(trial.blueprint, tk)["workflow"]["transitions"] = base(before, tk)
        try:
            ops = translate(Design(trial.blueprint), p, where)
            new_base = schema.table(trial.blueprint, tk)["workflow"]["transitions"] if _apply_ops(trial, ops, where) else None
        except Problem:
            new_base = None
        result = copy.deepcopy(ta["workflow"])
        said = f"what the feature{'s' if len(keys) != 1 else ''} {', '.join(keys)} add{'' if len(keys) != 1 else 's'} to {ta['label']}'s moves"
        if new_base is None:
            out.problems.append(f"{where}: it changes {said}; change a feature by sending it again with its key, or take it away "
                                f"with remove_feature")
            return False
        if not overlay(out, tk, new_base, where):
            return False
        if p.do in LAYOUT and schema.table(out.blueprint, tk)["workflow"] != result:  # said, so nobody thinks they're gone
            d = Design(out.blueprint)
            out.changes += [f"Kept on the new stages: {model_of(_entry(out.blueprint, k)).said(d)}" for k in keys if _entry(out.blueprint, k)]
        if p.do not in LAYOUT and schema.table(out.blueprint, tk)["workflow"] != result:
            out.problems.append(f"{where}: it would undo {said}; to change that, send the feature again with its key; to take it "
                                f"away, remove_feature with its key")
            return False
    return True


def _renamed(params, old, new):
    if not old or not new:
        return params
    if isinstance(params, dict):
        return {k: _renamed(v, old, new) for k, v in params.items()}
    if isinstance(params, list):
        return [_renamed(v, old, new) for v in params]
    return new if isinstance(params, str) and params.strip().lower() == str(old).strip().lower() else params


def _read_elsewhere(bp, key):
    """What the design has that isn't this feature's own (its rules, formulas, rules every save keeps), as text to find a
    field's key in: a field read there must stay when the feature lets it go."""
    import json
    rules = [a for a in bp.get("automations", []) if a.get("feature") != key]
    fields = [f.get("expression") for t in bp.get("tables", []) for f in t.get("fields", []) if f.get("feature") != key and f.get("expression")]
    kept = [t.get("rules") for t in bp.get("tables", [])]
    return json.dumps([rules, fields, kept])


def remove(out, key, where):
    entry = _entry(out.blueprint, key)
    if entry is None:
        keys = [e["key"] for e in out.blueprint.get("features", [])]
        raise Problem(f"{where}: there's no feature “{key}” (features: {', '.join(keys) or 'none'})")
    ir_ops = [{"op": "remove_automation", "key": a["key"]} for a in out.blueprint.get("automations", []) if a.get("feature") == key]
    others = _read_elsewhere(out.blueprint, key)  # a rule it gave up, say, or another feature's
    for t in out.blueprint.get("tables", []):
        for f in t.get("fields", []):
            if f.get("feature") != key:
                continue
            if re.search(rf"\b{re.escape(f['key'])}\b", others):  # still read by a rule now someone's own: it stays, theirs
                f.pop("feature", None)
            else:
                ir_ops.append({"op": "remove_field", "table": t["key"], "key": f["key"]})
    undo = entry.get("undo") or {}
    relay = {}  # the others that add to these moves, laid over them again without this one
    for tk, transitions in (undo.get("workflow") or {}).items():
        t = schema.table(out.blueprint, tk)
        if t and t.get("workflow"):
            others = [e for e in _shapers(out.blueprint, tk) if e["key"] != key]
            if others:
                relay[tk] = base(out.blueprint, tk)
            else:
                ir_ops.append({"op": "update_table", "table": tk, "workflow": {**t["workflow"], "transitions": transitions}})
    for tk, lock in (undo.get("lock_if") or {}).items():
        ir_ops.append({"op": "update_table", "table": tk, "lock_if": lock})
    for tk, rules in ((entry.get("made") or {}).get("rules") or {}).items():  # a gate's rules every save keeps
        t = schema.table(out.blueprint, tk)
        if t is not None:
            ir_ops.append({"op": "update_table", "table": tk, "rules": [r for r in t.get("rules") or [] if r not in rules]})
    mine = (entry.get("made") or {}).get("limit")
    if mine:
        for t in out.blueprint.get("tables", []):
            if mine in (t.get("limits") or []):
                ir_ops.append({"op": "update_table", "table": t["key"], "limits": [x for x in t["limits"] if x != mine]})
    if not _apply_ops(out, ir_ops, where):
        return False
    out.blueprint["features"] = [e for e in out.blueprint.get("features", []) if e["key"] != key]
    for tk, transitions in relay.items():
        if not overlay(out, tk, transitions, where):
            return False
    out.changes.append(f"Took away: {key}")
    return True


REMADE = ("gate",)  # features the platform now makes differently: made again from what they were asked for


def refresh(bp, kinds=REMADE):
    """A design with its features of these kinds made again as the platform now makes them (a gate that held back only
    staff's buttons is now a rule every save keeps), and every stage within reach. Returns (design, what changed in
    plain words); a feature that can't be made again stays as it was."""
    from .compile import apply as apply_patches
    out_bp, said = bp, []
    for entry in list(bp.get("features", [])):
        if entry.get("do") not in kinds or model_of(entry) is None:
            continue
        out = apply_patches(out_bp, [{"do": entry["do"], **(entry.get("params") or {}), "key": entry["key"]}])
        if not out.problems and out.blueprint != out_bp:
            out_bp = out.blueprint
            said.append(f"{entry['key']} now holds for everyone, the owner and the app's own rules too")
    out = apply_patches(out_bp, [])  # stages nothing could reach get their buttons
    if not out.problems and out.blueprint != out_bp:
        out_bp = out.blueprint
        said.append("stages nothing could get to (or leave) have buttons now")
    return out_bp, said


def model_of(entry):
    cls = next((c for c in ALL if c.model_fields["do"].default == entry.get("do")), None)
    return cls.model_validate({**entry.get("params", {}), "key": entry["key"]}) if cls else None


def checks(bp):
    """Every feature's own checks for this design, each marked with its feature."""
    d, out = Design(bp), []
    for entry in bp.get("features", []):
        p = model_of(entry)
        if p is None or has_formula(p):  # what keeps to a formula can't be written for sure: the builder's examples prove it
            continue
        own = [a["key"] for a in bp.get("automations", []) if a.get("feature") == entry["key"]]
        try:
            for c in p.checks(d):
                c = _typed_only(d, c)
                if own:  # whether someone's told is about this feature's own rules: another's message is another's
                    c["expect"] = [{**e, "by_rule": own} if isinstance(e, dict) and e.get("told") is not None else e
                                   for e in c.get("expect") or []]
                out.append({**c, "by": f"feature:{entry['key']}"})
        except Exception:  # noqa: BLE001 - a feature that can't write its checks for this design has none, said elsewhere
            continue
    return out


def has_formula(p):
    """Whether a feature's conditions include a formula (count(client.invoices where …) == 0)."""
    return any(isinstance(c, str) for name in ("only_if", "blocked_if") for c in (getattr(p, name, None) or []))


def _typed_only(d, check):
    """A feature's check as people would act it out: it never types into a field the app works out itself (a lookup of
    the job's date, the customer's email copied onto the invoice); that comes out of what it's worked out from."""
    tables, steps = {}, []
    for s in check.get("steps") or []:
        if s.get("do") == "add":
            tables[s.get("as")] = s.get("table")
        t = d.list(tables.get(s.get("as") if s.get("do") == "add" else s.get("record")) or "")
        if t is not None and isinstance(s.get("values"), dict) and s.get("do") in ("add", "change", "move"):
            s = {**s, "values": {k: v for k, v in s["values"].items() if (d.field(t, k) or {}).get("type") not in schema.CALCULATED}}
        steps.append(s)
    return {**check, "steps": steps}
