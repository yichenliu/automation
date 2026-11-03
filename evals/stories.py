"""What each business asked its app to do, checked by doing it: in a throwaway copy of whatever the analyst built.

A story finds what it needs by meaning, not by name (the quotes table, whatever it's called; its total; the move that
sends it), does what the business would (a quote with two lines and 15% off; a $25,000 purchase request sent for
approval) and checks what the app then does. It passes, fails (the app did something else: said why), or is missing
(the design has nothing for it). Stories run on a design, so a design saved by an earlier run can be checked again.
"""
import re

from app.blueprint import schema
from app.builder import trial as trials
from app.engine import automations
from app.engine.records import FieldErrors, to_number
from app.extensions import db
from app.models import Approval, Membership, User

GST = 0.1


class Missing(Exception):
    """The design has nothing for what the story needs."""


class Wrong(Exception):
    """The app did something other than what the business asked for."""


class Shop:
    """A copy of the app, as a story uses it."""

    def __init__(self, trial):
        self.trial, self.engine, self.made, self.n = trial, trial.engine(), {}, 0

    # finding things by meaning
    def table(self, *words, embedded=None, exclude=()):
        found = []
        for t in self.engine.tables.values():
            if embedded is not None and bool(t.get("embedded")) != embedded:
                continue
            text = f"{t['key']} {t['label']} {t['singular']}".lower().replace("_", " ")
            if any(x in text for x in exclude):
                continue
            rank = next((i for i, w in enumerate(words) if re.search(rf"\b{w}", text)), None)
            if rank is not None:
                found.append((rank, bool(t.get("embedded")), len(t["key"]), t))
        if not found:
            raise Missing(f"no list of {words[0]}s")
        return min(found, key=lambda x: x[:3])[3]

    def field(self, t, *words, kinds=None, calculated=None, exclude=(), required=True):
        for w in words:
            for f in t["fields"]:
                name = f"{f['key']} {f['label']}".lower().replace("_", " ")
                if re.search(rf"\b{w}", name) and not any(x in name for x in exclude) \
                        and (kinds is None or self.engine.kind(f) in kinds) \
                        and (calculated is None or (f["type"] in schema.CALCULATED) == calculated):
                    return f
        if required:
            raise Missing(f"no {words[0]} on {t['label'].lower()}")
        return None

    def link(self, child, parent):
        return next((f for f in child["fields"] if f["type"] == "link" and f["table"] == parent["key"]), None)

    def lines(self, parent):
        """The line items of a document (a quote's lines): a table linking to it, with a quantity and a price."""
        found = []
        for t in self.engine.tables.values():
            if t["key"] != parent["key"] and self.link(t, parent):
                money = self.field(t, "price", "rate", "unit", "amount", "cost", kinds=("money",), required=False)
                found.append((not t.get("embedded"), money is None, t))
        if not found:
            raise Missing(f"no lines on {parent['label'].lower()}")
        return min(found, key=lambda x: x[:2])[2]

    def status(self, t):
        """Where a record is: its workflow's field, or a Status choice people set themselves."""
        if t.get("workflow"):
            return schema.field(t, t["workflow"]["field"])
        return next((f for f in t["fields"] if f["type"] == "select" and re.search(r"status|stage|state", f["key"])), None)

    def stages(self, t):
        f = self.status(t)
        return list(f.get("options") or []) if f else []

    def stage_like(self, t, *words):
        return next((s for w in words for s in self.stages(t) if re.search(rf"\b{w}", s.lower())), None)

    def route(self, t, start, target):
        """The stages to go through, move by move, from one stage to another (the shortest way), or None."""
        moves = (t.get("workflow") or {}).get("transitions", [])
        ways, seen = [[start]], {start}
        while ways:
            way = ways.pop(0)
            if way[-1] == target:
                return way[1:]
            for m in moves:
                if (not m["from"] or way[-1] in m["from"]) and m["to"] not in seen:
                    seen.add(m["to"])
                    ways.append(way + [m["to"]])
        return None

    def take_to(self, record, target):
        """Take a record to a stage as people would: by its buttons along the way (answering what waits for an OK), or by
        setting its Status where there are no buttons. Wrong if it can't get there."""
        t = self.engine.tables[record.table]
        if not t.get("workflow"):
            try:
                self.engine.update(self.get(record), {self.status(t)["key"]: target})
            except FieldErrors as exc:
                raise Wrong(f"a {t['singular'].lower()} couldn't be set to {target}: {'; '.join(exc.errors.values())}")
            return
        way = self.route(t, self.stage(record), target)
        if way is None:
            raise Wrong(f"a {t['singular'].lower()} can't get from {self.stage(record)} to {target}: no buttons lead there")
        for stage in way:
            for _ in range(4):
                if self.stage(record) == stage:
                    break
                waiting = self.waiting(record)
                if waiting:
                    self.answer(waiting[0])
                    continue
                self.fill_for(record, stage)
                self.move(record, re.escape(stage.lower()))
            if self.stage(record) != stage:
                raise Wrong(f"a {t['singular'].lower()} got stuck at {self.stage(record)} on its way to {target}")

    # doing things
    def person(self, role, name=None, teams=None):
        """Someone in the business with this role (and teams): an engine acting as them."""
        user_id = (db.session.query(db.func.max(User.id)).scalar() or 0) + 1
        user = User(id=user_id, email=f"person{user_id}@example.com", name=name or f"{role.title()} {user_id}")
        user.password_hash = "!"
        db.session.add(user)
        if teams is None:  # in every team, as whoever does that work would be (the preparer of a run is in Finance)
            teams = [tm["key"] for tm in schema.teams(self.engine.bp)] or None
        db.session.add(Membership(workspace_id=self.trial.ws_id, user_id=user_id, role=role, teams=teams))
        db.session.commit()
        return self.trial.engine(user_id)

    def someone_else(self):
        """Who person fields (an approver, a team lead) name: not whoever raises things, as in a real business."""
        other = User.query.filter_by(name=trials.STAND_IN).first()
        return other.id if other else self.trial.owner_id

    def add(self, t, values=None, engine=None):
        """A record with these values ({field key: value}) and ordinary ones for the rest; what it must link to is added
        first."""
        engine = engine or self.engine
        given = dict(values or {})
        self.n += 1
        out, nth = {}, 0
        for f in t["fields"]:
            if f["key"] in given or f["type"] not in schema.STORED or f["type"] == "autonumber" or f.get("readonly"):
                continue
            if f["type"] == "link":
                if f["table"] in self.made:
                    out[f["key"]] = [self.made[f["table"]]] if f.get("multiple") else self.made[f["table"]]
                elif f.get("required") and f["table"] != t["key"]:
                    out[f["key"]] = self.add(self.engine.tables[f["table"]]).id
                continue
            v = trials.sample_value(self.engine, t, f, self.someone_else(), self.made, self.n, nth)
            nth += self.engine.kind(f) in ("date", "datetime")
            if v is not None and not (self.engine.kind(f) in ("money", "number", "percent") and not f.get("required")):
                out[f["key"]] = v  # numbers left empty unless they must be filled: a sample price shouldn't change a story's sums
        out.update(given)
        try:
            record = engine.create(t["key"], out, ai=False)
        except FieldErrors as exc:
            raise Wrong(f"a new {t['singular'].lower()} couldn't be saved: {'; '.join(exc.errors.values())}")
        self.made.setdefault(t["key"], record.id)
        return record

    def get(self, record):
        self.engine.reset()
        return self.engine.get(record.id)

    def value(self, record, f):
        return self.engine.value(self.get(record), f["key"] if isinstance(f, dict) else f)

    def number(self, record, f):
        return to_number(self.value(record, f))

    def stage(self, record):
        f = self.status(self.engine.tables[record.table])
        return (self.get(record).data or {}).get(f["key"]) if f else None

    def move(self, record, *words, engine=None):
        """Take the open move to a stage matching the words, as the button would (filling in what it needs first, as the
        person would); Wrong if there's none."""
        engine = engine or self.engine
        engine.reset()
        record = engine.get(record.id)
        for w in words:
            for i, m, problem in engine.moves(record):
                if re.search(rf"\b{w}", m["to"].lower()) or re.search(rf"\b{w}", str(m.get("label") or "").lower()):
                    if problem and problem.startswith("Fill in"):
                        self.fill_for(record, m["to"])
                        engine.reset()
                        record = engine.get(record.id)
                        problem = engine.move_problem(engine.tables[record.table], record, m)
                    if problem:
                        raise Wrong(f"“{m.get('label') or m['to']}” was refused: {problem}")
                    engine.move(record, i)
                    return m["to"]
        offered = [m.get("label") or m["to"] for _, m, _ in engine.moves(record)]
        raise Wrong(f"at {self.stage(record)} there's no way on to {words[0]} (it offers: {', '.join(offered) or 'nothing'})")

    def fill_for(self, record, stage):
        """Fill in what a move to this stage needs first (the tradie, before "Assign tradie"), as the person would."""
        t = self.engine.tables[record.table]
        needs = {k for m in (t.get("workflow") or {}).get("transitions", []) if m["to"] == stage for k in m.get("requires", [])}
        current = self.get(record).data or {}
        values = {}
        for key in needs:
            f = schema.field(t, key)
            if f is None or current.get(key) not in (None, "", []):
                continue
            if f["type"] in schema.FILES:  # attached, as a person would
                from app.storage import save
                save(self.trial.ws_id, b"%PDF-1.4 sample", f"{key}.pdf", record_id=record.id, field=key, by="Someone")
            elif f["type"] == "link":
                linked = self.made.get(f["table"]) or self.add(self.engine.tables[f["table"]]).id
                values[key] = [linked] if f.get("multiple") else linked
            else:
                values[key] = trials.sample_value(self.engine, t, f, self.trial.owner_id, self.made, self.n) or "Filled in"
        if values:
            self.engine.update(self.get(record), values)

    def with_lines(self, t, values=None):
        """A record and, if it has line items (an order's lines), one line on it."""
        record = self.add(t, values)
        try:
            lines = self.lines(t)
        except Missing:
            return record
        if lines.get("embedded"):
            self.add(lines, {self.link(lines, t)["key"]: record.id})
        return record

    def can_move(self, record, *words, engine=None):
        engine = engine or self.engine
        engine.reset()
        record = engine.get(record.id)
        return any(not problem and any(re.search(rf"\b{w}", f"{m['to']} {m.get('label') or ''}".lower()) for w in words)
                   for _, m, problem in engine.moves(record))

    def waiting(self, record):
        return Approval.query.filter(Approval.workspace_id == self.trial.ws_id, Approval.record_id == record.id,
                                     Approval.status == "pending", Approval.kind == "approval").order_by(Approval.id).all()

    def answer(self, approval, yes=True, record=None):
        """Someone allowed to (not whoever raised it, if that's the rule) says yes or no."""
        people = [approval.assignee_id, self.trial.owner_id] + [m.user_id for m in Membership.query.filter_by(workspace_id=self.trial.ws_id)]
        record = record or self.engine.get(approval.record_id)
        who = next((u for u in people if u and automations.may_decide(approval, self.trial.member(u), u, record, self.trial.engine(u))), None)
        if who is None:
            raise Wrong(f"nobody could answer “{approval.message}”")
        automations.decide(self.trial.engine(who), approval, yes, "Someone")

    def approve_through(self, record, done_words=("approved", "ordered", "accepted", "sent", "complete")):
        """Say yes to every approval it waits for, and take every approving move, until it's through: how many OKs it took."""
        oks = 0
        for _ in range(8):
            waiting = self.waiting(record)
            if waiting:
                self.answer(waiting[0])
                oks += 1
                continue
            stage = (self.stage(record) or "").lower()
            if any(w in stage for w in done_words):
                break
            if self.can_move(record, "approv", "endorse", "sign"):
                self.move(record, "approv", "endorse", "sign")
                oks += 1
                continue
            break
        return oks

    def count(self, t):
        self.engine.reset()
        return len(self.engine.rows(t["key"]))


def near(a, b, cents=0.02):
    return a is not None and b is not None and abs(a - b) <= cents


def money(v):
    return f"${v:,.2f}" if isinstance(v, (int, float)) else str(v)


# --- shared stories ---------------------------------------------------------------------------------------------------

def totals_add_up(shop, words, discount=None, gst=True):
    """A document with two lines (2 x $100 and 1 x $50) and, if asked, a discount: its total is the lines less the
    discount plus GST (or the same with GST inside the prices)."""
    doc = shop.table(*words, embedded=False)
    lines = shop.lines(doc)
    qty = shop.field(lines, "quantity", "qty", "hours", "units", "guests", kinds=("number", "duration"), calculated=False)
    price = shop.field(lines, "unit price", "price", "rate", "unit", "cost", kinds=("money",), calculated=False)
    values = {}
    if discount is not None:
        values[shop.field(doc, "discount", kinds=("percent", "number"), calculated=False)["key"]] = discount * 100
    record = shop.add(doc, values)
    link = shop.link(lines, doc)
    for q, p in ((2, 100), (1, 50)):
        shop.add(lines, {link["key"]: record.id, qty["key"]: q, price["key"]: p})
    total_f = shop.field(doc, "grand total", "total inc", "total", kinds=("money",), exclude=("sub", "paid", "deposit", "balance", "line"))
    total = shop.number(record, total_f)
    base = 250 * (1 - (discount or 0))
    fits = [base * (1 + GST), base] if gst else [base]
    if not any(near(total, x) for x in fits):
        raise Wrong(f"its {total_f['label']} came out {money(total)}; the lines are $250.00" + (f" less {discount:.0%}" if discount else "")
                    + (f", so it should be {money(base * (1 + GST))} with GST (or {money(base)} if prices include it)" if gst else f", so {money(base)}"))
    return record


def reaches(shop, words, *stage_words):
    """A record can be taken, as people would, from where it starts to a stage matching the words."""
    t = shop.table(*words, embedded=False)
    target = shop.stage_like(t, *stage_words)
    if target is None:
        raise Missing(f"{t['label'].lower()} have no {stage_words[0]} stage")
    shop.take_to(shop.add(t), target)


def makes(shop, words, made_words, *stage_words):
    """Taking a record to a stage (a job done, an order confirmed) makes a record of another kind (its invoice)."""
    t = shop.table(*words, embedded=False)
    made = shop.table(*made_words, embedded=False, exclude=(t["label"].lower(),))
    target = shop.stage_like(t, *stage_words)
    if target is None:
        raise Missing(f"{t['label'].lower()} have no {stage_words[0]} stage")
    record = shop.with_lines(t)
    before = shop.count(made)
    shop.take_to(record, target)
    if shop.count(made) <= before:
        raise Wrong(f"a {t['singular'].lower()} at “{target}” didn't make a {made['singular'].lower()}")


def hidden_from(shop, role, words, field_words, teams=None):
    """Someone with this role can't see these fields (or the whole list)."""
    t = shop.table(*words)
    fields = [f for f in t["fields"] if any(re.search(rf"\b{w}", f"{f['key']} {f['label']}".lower().replace("_", " ")) for w in field_words)]
    if not fields:
        raise Missing(f"no {field_words[0]} on {t['label'].lower()}")
    them = shop.person(role, teams=teams)
    if not them.can(t, "view"):
        return
    shown = [f["label"] for f in fields if them.sees(f)]
    if shown:
        raise Wrong(f"{'an' if role[0] in 'aeiou' else 'a'} {role} can see {', '.join(shown)} on {t['label'].lower()}")


def one_entry(shop, words, field_words):
    """The same value can't be entered twice (a supplier's invoice number)."""
    t = shop.table(*words, embedded=False)
    f = shop.field(t, *field_words, calculated=False)
    shop.add(t, {f["key"]: "INV-7781"})
    try:
        shop.add(t, {f["key"]: "INV-7781"})
    except Wrong:
        return
    raise Wrong(f"a second {t['singular'].lower()} with {f['label']} INV-7781 was let in")


def approvals_for(shop, words, amount, expected, amount_words=("amount", "total", "value", "cost", "estimate")):
    """A request for this amount, sent for approval and approved by whoever may: it takes `expected` OKs."""
    t = shop.table(*words, embedded=False)
    amount_f = shop.field(t, *amount_words, kinds=("money",), calculated=False, required=False)
    values = {amount_f["key"]: amount} if amount_f else {}
    record = shop.add(t, values)
    if amount_f is None:  # the amount is the sum of its lines
        lines = shop.lines(t)
        qty = shop.field(lines, "quantity", "qty", kinds=("number",), calculated=False, required=False)
        price = shop.field(lines, "unit price", "price", "amount", "cost", kinds=("money",), calculated=False)
        shop.add(lines, {shop.link(lines, t)["key"]: record.id, **({qty["key"]: 1} if qty else {}), price["key"]: amount})
    shop.move(record, "submit", "pending", "approv", "review", "sent")
    oks = shop.approve_through(record)
    if oks != expected:
        raise Wrong(f"a {money(amount)} {t['singular'].lower()} took {oks} approval{'s' if oks != 1 else ''}; it should take {expected}")


def rejected_goes_back(shop, words):
    """A request someone says no to goes back to whoever raised it, who can fix it and send it again."""
    t = shop.table(*words, embedded=False)
    record = shop.add(t)
    shop.move(record, "submit", "pending", "approv", "review")
    waiting = shop.waiting(record)
    if waiting:
        shop.answer(waiting[0], yes=False)
    elif shop.can_move(record, "reject", "declin", "return", "back"):
        shop.move(record, "reject", "declin", "return", "back")
    else:
        raise Missing(f"no way to say no to a {t['singular'].lower()}")
    if not shop.can_move(record, "submit", "resubmit", "pending", "approv", "review", "draft"):
        raise Wrong(f"a rejected {t['singular'].lower()} (now {shop.stage(record)}) can't be fixed and sent again")


def not_by_raiser(shop, words):
    """Whoever raised something can't approve it themselves (even an admin)."""
    t = shop.table(*words, embedded=False)
    raiser = shop.person("admin", name="Riley Raiser")
    record = shop.add(t, engine=raiser)
    try:
        shop.move(record, "submit", "pending", "review", "send", engine=raiser)
    except Wrong:
        pass  # nothing to send it on with: it may wait for approval as it is
    waiting = shop.waiting(record)
    if waiting:
        if automations.may_decide(waiting[0], shop.trial.member(raiser.user_id), raiser.user_id, shop.get(record), raiser):
            raise Wrong(f"whoever raised a {t['singular'].lower()} could approve it themselves")
        return
    if shop.can_move(record, "approv", engine=raiser):
        raise Wrong(f"whoever raised a {t['singular'].lower()} could approve it themselves")


def colour_when(shop, words, field_words, value_words, colour):
    """A record with this value stands out in this colour."""
    t = shop.table(*words, embedded=False)
    f = shop.field(t, *field_words, calculated=False)
    if f["type"] in ("select", "multi_select"):
        option = next((o for o in f["options"] if any(w in o.lower() for w in value_words)), None)
        if option is None:
            raise Missing(f"{f['label']} has no {value_words[0]} choice")
        value = [option] if f["type"] == "multi_select" else option
    else:
        value = True if f["type"] == "checkbox" else value_words[0]
    record = shop.get(shop.add(t, {f["key"]: value}))
    shown = shop.engine.colours(t, record)
    if colour not in (shown["row"], *shown["fields"].values()):
        raise Wrong(f"a {value_words[0]} {t['singular'].lower()} isn't {colour} (it's {shown['row'] or 'not coloured'})")


def rolls_up(shop, parent_words, child_words, value_words, amounts=(5, 3)):
    """Children's amounts add up on their parent (hours logged on a project)."""
    parent = shop.table(*parent_words, embedded=False)
    child = shop.table(*child_words)
    link = shop.link(child, parent)
    if link is None:
        raise Missing(f"{child['label'].lower()} don't link to {parent['label'].lower()}")
    amount = shop.field(child, *value_words, kinds=("number", "duration", "money"), calculated=False)
    signed = {f["key"]: f for f in child["fields"] if f["type"] == "formula" and re.search(rf"\b{amount['key']}\b", f.get("expression") or "")}
    total = next((f for f in parent["fields"] if f["type"] == "rollup" and f.get("table") == child["key"]
                  and f.get("aggregate") == "sum" and (f.get("field") == amount["key"] or f.get("field") in signed)), None)
    if total is None:
        raise Missing(f"no total of {amount['label'].lower()} on {parent['label'].lower()}")
    way = None  # summed as worked out from the quantity and its In or Out (2 sold is 2, Out): entered as people would
    if total.get("field") in signed:
        way = next((f for f in child["fields"] if f["type"] == "select" and re.search(rf"\b{f['key']}\b", signed[total["field"]]["expression"])), None)
    record = shop.add(parent)
    for n in amounts:
        values = {link["key"]: record.id, amount["key"]: abs(n) if way else n}
        if way:
            options = way.get("options") or []
            values[way["key"]] = next((o for o in options if re.search(r"\b(out|used|minus|remove|sold|issue)", o, re.I)), options[-1]) if n < 0 \
                else next((o for o in options if re.search(r"\b(in|add|plus|receiv|received)", o, re.I)), options[0])
        shop.add(child, values)
    got = shop.number(record, total)
    if not near(got, sum(amounts)):
        raise Wrong(f"{parent['singular'].lower()}’s {total['label']} came out {got}; the {child['label'].lower()} add up to {sum(amounts)}")


def document_made(shop, words):
    """The business's document (a tax receipt, an invoice) can be made."""
    t = shop.table(*words, embedded=False)
    if not t.get("documents"):
        raise Missing(f"no document on {t['label'].lower()}")
    from app.documents import produce
    record = shop.add(t)
    try:
        produce(shop.engine, shop.get(record), t["documents"][0]["key"], by="Someone")
    except Exception as exc:  # noqa: BLE001
        raise Wrong(f"the {t['documents'][0]['label']} couldn't be made: {exc}")


def locked_once(shop, words, stage_words, allowed_words=("paid", "payment")):
    """Once a record reaches a stage (an invoice sent), staff can't change it, except to record payment."""
    t = shop.table(*words, embedded=False)
    target = shop.stage_like(t, *stage_words)
    if target is None:
        raise Missing(f"{t['label'].lower()} have no {stage_words[0]} stage")
    staff = shop.person("editor")
    record = shop.add(t)
    shop.engine.update(shop.get(record), {shop.status(t)["key"]: target}, automations=False)
    other = next((f for f in t["fields"] if f["type"] in ("text", "long_text", "date") and not f.get("readonly")
                  and not any(w in f["key"] for w in allowed_words) and f["key"] != shop.status(t)["key"]), None)
    if other is None:
        raise Missing(f"nothing on a {t['singular'].lower()} to try changing")
    staff.reset()
    try:
        staff.update(staff.get(record.id), {other["key"]: "2027-01-01" if other["type"] == "date" else "Changed after sending"})
    except FieldErrors:
        pass
    else:
        raise Wrong(f"staff could still change {other['label']} on a {target.lower()} {t['singular'].lower()}")
    paid = shop.field(t, *allowed_words, calculated=False, required=False)
    if paid is not None:
        staff.reset()
        try:
            value = True if paid["type"] == "checkbox" else 10 if shop.engine.kind(paid) in ("money", "number") else \
                (paid.get("options") or ["Paid"])[-1] if paid["type"] == "select" else shop.engine.today.isoformat()
            staff.update(staff.get(record.id), {paid["key"]: value})
        except FieldErrors as exc:
            raise Wrong(f"recording payment on a {target.lower()} {t['singular'].lower()} was refused: {'; '.join(exc.errors.values())}")


def own_records_only(shop, words, owner_words=("rep", "sales", "account manager", "owner", "assigned")):
    """Staff see only their own customers' records (a sales rep's orders)."""
    t = shop.table(*words, embedded=False)
    rep = shop.person("editor", name="Robin Rep")
    person = shop.field(t, *owner_words, required=False)
    if person is None or person["type"] not in ("user", "link"):
        customers = next((shop.engine.tables[f["table"]] for f in t["fields"] if f["type"] == "link"
                          and shop.field(shop.engine.tables[f["table"]], *owner_words, required=False)), None)
        if customers is None:
            raise Missing(f"nothing says whose {t['label'].lower()} are whose")
        who = shop.field(customers, *owner_words)
        if who["type"] != "user":
            raise Missing(f"{customers['label'].lower()}’s {who['label']} isn't a person in the app")
        mine, theirs = shop.add(customers, {who["key"]: rep.user_id}), shop.add(customers, {who["key"]: shop.trial.owner_id})
        link = shop.link(t, customers)
        shop.add(t, {link["key"]: mine.id})
        shop.add(t, {link["key"]: theirs.id})
    else:
        if person["type"] != "user":
            raise Missing(f"{t['label'].lower()}’s {person['label']} isn't a person in the app")
        shop.add(t, {person["key"]: rep.user_id})
        shop.add(t, {person["key"]: shop.trial.owner_id})
    rep.reset()
    seen = len([r for r in rep.rows(t["key"]) if rep.allowed(r)])
    if seen != 1:
        raise Wrong(f"a sales rep sees {seen} of 2 {t['label'].lower()}, not just their own")


STORIES = {
    "plumbing": [
        ("A quote's lines add up, with GST", lambda s: totals_add_up(s, ("quote",))),
        ("A finished job drafts its invoice", lambda s: makes(s, ("job",), ("invoice",), "done", "complete", "finish", "signed off", "sign off")),
        ("Urgent jobs stand out in red", lambda s: colour_when(s, ("job",), ("priority", "urgent", "type", "urgency"), ("urgent", "emergency"), "red")),
    ],
    "wholesale": [
        ("An invoice's lines add up, with GST", lambda s: totals_add_up(s, ("invoice",), gst=True)),
        ("A sent invoice can't be edited, except to record payment", lambda s: locked_once(s, ("invoice",), ("sent", "issued", "posted"))),
        ("Sales reps see only their own customers' orders", lambda s: own_records_only(s, ("order",))),
    ],
    "consultancy": [
        ("Hours logged add up on the project", lambda s: rolls_up(s, ("project",), ("timesheet", "time entr", "time log"), ("hours", "duration"))),
        ("Consultants don't see rates or margins", lambda s: hidden_from(s, "editor", ("project", "consultant", "staff"), ("rate", "margin", "profit"))),
    ],
    "candles": [
        ("Stock added for a scent adds up", lambda s: rolls_up(s, ("product", "candle", "scent", "item"), ("stock movement", "movement", "pour", "batch"), ("quantity", "qty", "change"), (12, 6))),
    ],
    "furniture": [
        ("A customer order becomes a work order", lambda s: makes(s, ("customer order", "order"), ("work order", "production", "job"), "confirm", "approv", "accept", "progress")),
        ("Materials used add up on the material", lambda s: rolls_up(s, ("material", "component", "part"), ("stock movement", "movement", "usage", "consum"), ("quantity", "qty", "change"), (20, -6))),
    ],
    "physio": [
        ("Treatment notes are kept from the front desk", lambda s: hidden_from(s, "viewer", ("note", "treatment"), ("note", "treatment", "assessment", "plan"))),
        ("An invoice can be made", lambda s: document_made(s, ("invoice",))),
    ],
    "builder": [
        ("A variation can be signed off", lambda s: reaches(s, ("variation",), "sign", "approv", "accept")),
        ("A progress payment can be claimed for a stage", lambda s: _claim_for_stage(s)),
    ],
    "rescue": [
        ("A donation gets its tax receipt", lambda s: document_made(s, ("donation",))),
        ("An animal can be adopted", lambda s: reaches(s, ("animal",), "adopt") if s.stage_like(s.table("animal", embedded=False), "adopt")
                                               else s.add(s.table("adoption", embedded=False))),
    ],
    "property": [
        ("A maintenance request can be finished", lambda s: reaches(s, ("maintenance", "repair", "request"), "complete", "done", "closed", "resolved")),
        ("An owner statement can be made", lambda s: document_made(s, ("statement", "owner"))),
    ],
    "cleaner": [
        ("An invoice can be made", lambda s: document_made(s, ("invoice",))),
    ],
    "approvals": [
        ("Under $2,000 the team lead approves", lambda s: approvals_for(s, ("request", "requisition", "purchase"), 1500, 1)),
        ("Over $2,000 finance approves as well", lambda s: approvals_for(s, ("request", "requisition", "purchase"), 5000, 2)),
        ("Over $20,000 the director approves too", lambda s: approvals_for(s, ("request", "requisition", "purchase"), 25000, 3)),
        ("A rejected request goes back to be fixed", lambda s: rejected_goes_back(s, ("request", "requisition", "purchase"))),
    ],
    "procurement": [
        ("Nobody approves their own request", lambda s: not_by_raiser(s, ("request", "requisition"))),
        ("A supplier invoice number can't be entered twice", lambda s: one_entry(s, ("supplier invoice", "invoice", "bill"), ("invoice number", "invoice no", "supplier invoice", "number", "reference"))),
        ("Whoever prepares a payment run can't approve it", lambda s: not_by_raiser(s, ("payment run", "payment"))),
    ],
    "catering": [
        ("A quote with 15% off adds up, with GST", lambda s: totals_add_up(s, ("quote",), discount=0.15)),
        ("A quote with no discount can be sent straight away", lambda s: s.move(s.add(s.table("quote", embedded=False)), "sent", "send")),
        ("Over 10% off waits for Maria's OK", lambda s: _big_discount(s)),
        ("Staff can be put on an event", lambda s: _staff_on_event(s)),
    ],
}


def _big_discount(shop):
    quote = shop.table("quote", embedded=False)
    pct = shop.field(quote, "discount", kinds=("percent", "number"), calculated=False)
    record = shop.add(quote, {pct["key"]: 15})
    if shop.can_move(record, "sent", "send"):
        engine = shop.engine
        moves = [m for _, m, problem in engine.moves(shop.get(record)) if not problem]
        if any(re.search(r"\bsent?\b", f"{m['to']} {m.get('label') or ''}".lower()) and not re.search("approv", f"{m['to']} {m.get('label') or ''}".lower())
               for m in moves):
            stage = shop.move(record, "sent", "send")
            if not shop.waiting(record) and "approv" not in (stage or "").lower():
                raise Wrong("a quote with 15% off went out without Maria's OK")
            return
    shop.move(record, "approv", "review", "sent", "send")
    if not shop.waiting(record) and "approv" not in (shop.stage(record) or "").lower():
        raise Wrong("a quote with 15% off didn't wait for Maria's OK")


def _claim_for_stage(shop):
    stage = shop.table("stage", "milestone", embedded=False)
    claim = shop.table("progress", "claim", embedded=False, exclude=("subcontract",))
    project = next((shop.engine.tables[f["table"]] for f in stage["fields"] if f["type"] == "link"), None)
    link = shop.link(claim, stage) or (shop.link(claim, project) if project else None)
    if link is None:
        raise Missing(f"{claim['label'].lower()} aren't tied to a stage or project")
    shop.add(claim, {link["key"]: shop.add(shop.engine.tables[link["table"]]).id})


def _staff_on_event(shop):
    event = shop.table("event", "booking", "job", embedded=False)
    staff = shop.table("staff", "employee", "team member", "worker", exclude=("event",))
    if any(f["type"] == "link" and f["table"] == staff["key"] for f in event["fields"]):
        return
    if any(shop.link(t, event) and shop.link(t, staff) for t in shop.engine.tables.values()):
        return
    raise Missing("nothing puts staff on an event")


def run(workspace, key):
    """Each story of a scenario in its own copy of the app: [{"story", "result": pass | fail | missing | error, "why"}]."""
    out = []
    for name, story in STORIES.get(key, []):
        with trials.copy_of(workspace) as trial:
            shop = Shop(trial)
            try:
                story(shop)
                out.append({"story": name, "result": "pass", "why": ""})
            except Missing as exc:
                out.append({"story": name, "result": "missing", "why": str(exc)})
            except Wrong as exc:
                out.append({"story": name, "result": "fail", "why": str(exc)})
            except Exception as exc:  # noqa: BLE001 - the story's own fault or the platform's: shown, never hidden
                db.session.rollback()
                out.append({"story": name, "result": "error", "why": f"{type(exc).__name__}: {exc}"[:300]})
    return out

