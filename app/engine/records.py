"""Records: reading, writing and calculating them according to the workspace's blueprint.

An Engine is made per request (or per background task) for one workspace. It caches what it reads, so a list of 500
invoices with formulas and totals is worked out once. Every write goes through `create`, `update` or `delete`, which
clean the values, keep the history, and start any automation the write should trigger.

Writes are safe for an ERP:
- `with engine.atomic():` makes several writes (an invoice and its lines) happen together or not at all;
- a table's rules ("debits equal credits") are checked before anything is committed;
- a save made from an out-of-date copy of a record is refused (Conflict) instead of overwriting someone else's change;
- records can lock (a posted invoice): people below admin can't change or delete them.

An engine made for a person (`member=`) also applies what they may see: tables by role, records by owner, and fields by
role. Engines for automations and background work see everything.
"""
import logging
import re
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text as sql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError

from ..blueprint import expr, schema
from ..blueprint.expr import to_bool, to_date, to_datetime, to_number
from ..extensions import db
from ..models import ROLES, Counter, File, Membership, Record, RecordChange, User, utcnow
from ..core.context import current_app

log = logging.getLogger("engine")
STEP_UP = "Confirm it's you first: enter the code from your authenticator app (or your password)"
APPROVED = re.compile(r"\bapproved\b|\bauthori[sz]ed\b|\bsigned off\b", re.I)  # a stage where it's been approved (not one waiting for it)
WAITING = re.compile(r"wait|pending|awaiting|for approval|to approve|needs", re.I)
DONE_WORDS = {"done", "sent", "paid", "won", "lost", "shot", "built", "made", "given", "taken", "chosen", "set", "put", "met", "held"}  # a stage word that says it's happened
AMOUNT = re.compile(r"total|amount|value|cost|price|\bsum\b|budget|spend", re.I)  # the money an approval is given for
DOING = re.compile(r"(send|submit|approve|reject|start|mark|close|cancel|complete|accept|decline|assign|pay|issue|deliver|ship|book|"
                   r"confirm|schedule|raise|post|review|finish|open|reopen|return|move|put|record|log|request|invite|order|receive|"
                   r"pick|pack|check|sign|publish|archive|hold|resume|release|lodge|file|quote|invoice|bill|dispatch|escalate|award|"
                   r"renew|end|terminate|withdraw|verify|inspect|rework|pass|fail|collect|refund|go|begin|add|make|take|hand)", re.I)


def _doing(move):
    """A move as something to do: "submit for approval" when its button says what to do, else press “Issues found”."""
    label = move.get("label") or ""
    if not label:
        return f"move it to {move['to']}"
    first = label.split()[0]
    return label if DOING.fullmatch(first) else f"press “{label}”"


PLACE_WORDS = re.compile(r"-?\d+(?:\.\d+)?|\b(lat(itude)?|lng|lon(gitude)?)\b|[\s,:;°/()]", re.I)  # what's left is a place's name
RAISED_BY = re.compile(r"\b(raised|created|requested|submitted|entered|logged|recorded|prepared|reported|booked) by\b", re.I)
NOT_AMOUNT = re.compile(r"paid|receiv|balance|\bdue\b|outstanding|owing|refund|deposit|credit", re.I)  # money that's meant to change later
SYMBOLS = {"USD": "US$", "AUD": "A$", "NZD": "NZ$", "CAD": "C$", "SGD": "S$", "HKD": "HK$", "EUR": "€", "GBP": "£", "JPY": "¥",
           "CNY": "CN¥", "INR": "₹", "KRW": "₩", "CHF": "CHF ", "ZAR": "R", "MYR": "RM", "THB": "฿", "PHP": "₱", "IDR": "Rp",
           "AED": "AED ", "SEK": "kr ", "NOK": "kr ", "DKK": "kr ", "BRL": "R$", "MXN": "MX$", "VND": "₫", "TWD": "NT$"}


def parse_duration(raw):
    """Hours as a number from 1.5, "1:30", "01:30:00", "1h 30m", "90m", "2 hours"."""
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip().lower()
    match = re.fullmatch(r"(\d+):(\d{1,2})(?::(\d{1,2}))?", text)
    if match:
        return round(int(match.group(1)) + int(match.group(2)) / 60 + int(match.group(3) or 0) / 3600, 4)
    parts = re.findall(r"(\d+(?:\.\d+)?)\s*(h|hr|hrs|hour|hours|m|min|mins|minute|minutes)", text)
    if parts:
        return round(sum(float(n) / (60 if unit.startswith("m") else 1) for n, unit in parts), 4)
    number = to_number(text)
    return float(number) if number is not None else None


def mask(text):
    """A sensitive value as people see it: •••• and its last four characters."""
    text = re.sub(r"\s+", "", str(text or ""))
    return "••••" + text[-4:] if len(text) > 6 else "••••"


def masked(stored):
    """What a sensitive field shows: its mask (stored with it), never the value."""
    if stored in (None, "", []):
        return None
    return stored.get("masked") if isinstance(stored, dict) else mask(stored)


def reveal(stored):
    """A sensitive field's real value (for the people allowed to see it, and the bank file)."""
    from .. import security
    if isinstance(stored, dict) and stored.get("secret"):
        value = security.decrypt(stored["secret"])
        return value if isinstance(value, str) else None
    return None if stored in (None, "", []) else str(stored)


def seal_value(text):
    from .. import security
    return {"masked": mask(text), "secret": security.encrypt(str(text))}


class NoRate(expr.ExprError):
    """An exchange rate the owner hasn't set yet (Settings › Business details): theirs to add, not a fault of the design."""


class FieldErrors(ValueError):
    """Values that can't be saved, by field: {"due_date": "“next week” isn't a date"}."""

    def __init__(self, errors):
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


class Conflict(FieldErrors):
    """The record changed since this copy of it was read (someone else saved it in the meantime)."""

    def __init__(self, message="Someone else changed this record while you were editing it. Reload to see their change, then make yours."):
        super().__init__({"_": message})


class Person(str):
    """A person field's value as expressions see it: their name, as before (it compares and sets another person field
    as it did), and `record.physio.email` or `record.physio.first_name` in a message."""

    def __new__(cls, name, uid, engine):
        person = super().__new__(cls, name)
        person.uid, person._engine = uid, engine
        return person

    def field(self, name):
        if name in ("name", "full_name", "display_name"):
            return str(self)
        if name == "first_name":
            return str(self).split(" ")[0]
        if name == "email":
            self._engine.members()
            return self._engine._emails.get(self.uid)
        if name == "id":
            return self.uid
        if name == "contact":  # an outside person's own record (the seller's person who asked): None for staff
            member = Membership.query.filter_by(workspace_id=self._engine.ws.id, user_id=self.uid, role="portal").first() if self.uid else None
            found = self._engine.get(member.contact_id) if member is not None and member.contact_id else None
            return RecordProxy(self._engine, found) if found is not None else None
        raise expr.ExprError(f"“.{name}” isn't something a person has: use .name, .email, or .contact (an outside person's record)")


class Files(str):
    """A file field's files as expressions see them: their names, as before, and `signed_pdf.fingerprint` (the SHA-256 of
    the latest, as it was stored: a copy is checked against it), `.name`, `.size` (bytes), `.count`."""

    def __new__(cls, files):
        files = list(files or [])
        out = super().__new__(cls, ", ".join(x["name"] for x in files))
        out.files = files
        return out

    def field(self, name):
        last = self.files[-1] if self.files else {}
        if name in ("fingerprint", "sha256", "hash"):
            return last.get("sha256")
        if name in ("name", "file_name"):
            return last.get("name")
        if name == "size":
            return last.get("size")
        if name == "count":
            return len(self.files)
        raise expr.ExprError(f"“.{name}” isn't something a file has: use .fingerprint, .name, .size or .count")


class RecordProxy:
    """A record as expressions see it: `client.payment_terms`, `record.name`, or the record itself (its title)."""

    def __init__(self, engine, record):
        self._engine, self.record = engine, record

    def field(self, name):
        if name not in ("id", "created_at", "updated_at", "created_by") and self._engine.field(self.record.table, name) is None:
            many = self._engine.linking(self.record, name)  # a client's invoices
            if many is not None:
                return many
        return self._engine.value(self.record, name, typed=True)

    def has(self, name):
        """Whether `name` is something of this record: a field, or the records linking to it (its invoices)."""
        return name in ("id", "created_at", "updated_at", "created_by") or self._engine.field(self.record.table, name) is not None \
            or schema.backlink(self._engine.tables, self._engine.tables.get(self.record.table), name) is not None

    def __str__(self):
        return self._engine.title(self.record)

    def __eq__(self, other):
        if isinstance(other, RecordProxy):
            return other.record.id == self.record.id
        return str(self).lower() == str(other).strip().lower() or str(other) == str(self.record.id)

    def __hash__(self):
        return hash(self.record.id)


class Rows(expr.Lazy):
    """The records of a list a formula looks at (a whole list, or those linking to one record), read only when they
    must be: narrowed first by what of a `where` SQL can test (stylist == this.stylist; status == "Unpaid"), and counted
    and added up by SQL where it can. Every record counts, whoever's save it is: a rule against double-booking counts
    the bookings this person can't see too."""

    def __init__(self, engine, table, filters=(), without=()):
        self.e, self.table, self.filters, self.without = engine, table, list(filters), frozenset(without)
        self._read = None

    def narrowed_by(self, node, ctx):
        pushed, without, whole = _pushable(self.e, self.e.tables[self.table], node, ctx)
        if not pushed and not without:
            return self, False
        return Rows(self.e, self.table, self.filters + pushed, self.without | without), whole

    def _found(self):
        if not self.filters:
            return self.e.rows(self.table)
        return self.e.query(self.table, self.filters, everyone=True)

    def read(self):
        if self._read is None:
            self._read = expr.Many(RecordProxy(self.e, r) for r in sorted(self._found(), key=lambda r: r.id or 0)
                                   if r.id not in self.without)
        return self._read

    def answer(self, how, key=None):
        if self._read is not None:
            return expr.UNSURE  # read already: worked out from what's read
        left_out = [r for r in (self.e.get(i, self.table) for i in self.without) if r is not None and self.e.matches(r, self.filters)]
        if how == "count" and key is None:
            return self.e.count(self.table, self.filters, everyone=True) - len(left_out)
        f = self.e.field(self.table, key) if key else None
        if left_out or f is None or f["type"] not in schema.STORED or how not in ("sum", "avg", "min", "max"):
            return expr.UNSURE
        from .planner import UNSURE
        got = self.e.total(self.table, how, key, self.filters, everyone=True)
        return expr.UNSURE if got is UNSURE else (0 if got is None and how == "sum" else got)


PUSHED_OPS = {expr.ast.Eq: "equals", expr.ast.NotEq: "not_equals", expr.ast.Gt: "greater_than", expr.ast.Lt: "less_than",
              expr.ast.GtE: "at_least", expr.ast.LtE: "at_most"}
TURNED_OPS = {"greater_than": "less_than", "less_than": "greater_than", "at_least": "at_most", "at_most": "at_least"}


def _pushable(engine, t, node, ctx):
    """What of a count(...)'s `where` SQL can test, as conditions on the records looked at: (conditions, ids left out,
    whether that's all of it). Only a field of theirs compared with what doesn't depend on them (a value, this.x,
    today()); the rest is worked out on what SQL finds."""
    parts = node.values if isinstance(node, expr.ast.BoolOp) and isinstance(node.op, expr.ast.And) else [node]
    pushed, without, whole = [], set(), True
    for part in parts:
        got = _pushed(engine, t, part, ctx) or _overlapping(engine, t, part, ctx)
        if got is None:
            whole = False
        elif got[0] == "without":
            without.add(got[1])
        elif got[0] == "near":  # narrows to those that could hold; worked out exactly on what's found
            pushed += got[1]
            whole = False
        else:
            pushed.append(got[1])
    return pushed, without, whole


def _overlapping(engine, t, part, ctx):
    """overlaps(starts_at, ends_at, this.starts_at, this.ends_at) as SQL can narrow it: those starting no later than this
    one ends, and ending (or, with no end, starting) no earlier than it starts."""
    if not (isinstance(part, expr.ast.Call) and isinstance(part.func, expr.ast.Name) and part.func.id.lower() in ("overlaps", "overlap")
            and len(part.args) == 4):
        return None
    start, end = part.args[0], part.args[1]
    if not all(isinstance(n, expr.ast.Name) and schema.field(t, n.id) and schema.field(t, n.id)["type"] in ("date", "datetime")
               for n in (start, end)) or any(_theirs(engine, t, n) for n in part.args[2:]):
        return None
    try:
        theirs = [expr._eval(n, ctx) for n in part.args[2:]]
    except expr.ExprError:
        return None
    lo = expr.to_datetime(theirs[0])
    hi = expr.to_datetime(theirs[1]) or lo
    if lo is None:
        return None
    kind = schema.field(t, start.id)["type"]
    said = (lambda d: d.date().isoformat()) if kind == "date" else (lambda d: d.isoformat(sep=" ", timespec="minutes"))
    return "near", [{"field": start.id, "op": "at_most", "value": said(hi)},
                    {"any": [{"field": end.id, "op": "at_least", "value": said(lo)},
                             {"all": [{"field": end.id, "op": "is_empty"}, {"field": start.id, "op": "at_least", "value": said(lo)}]}]}]


def _theirs(engine, t, node):
    """Whether an expression reads anything of the records looked at (a bare name that's one of their fields)."""
    for n in expr.ast.walk(node):
        if isinstance(n, expr.ast.Name) and n.id not in ("this", "record", "today", "now", "True", "False", "None") \
                and (n.id in ("item", "it", "id") or schema.field(t, n.id) or schema.backlink(engine.tables, t, n.id)):
            if not any(isinstance(c, expr.ast.Call) and c.func is n for c in expr.ast.walk(node)):
                return True
    return False


def _pushed(engine, t, part, ctx):
    if not isinstance(part, expr.ast.Compare) or len(part.ops) != 1:
        return None
    op, left, right = part.ops[0], part.left, part.comparators[0]
    for mine, other, turned in ((left, right, False), (right, left, True)):
        if not isinstance(mine, expr.ast.Name) or _theirs(engine, t, other):
            continue
        try:
            value = expr._eval(other, ctx)
        except expr.ExprError:
            return None
        if isinstance(value, RecordProxy):
            value = value.record.id
        if isinstance(value, timedelta):  # a length of time on its own (minutes > 2h): worked out here
            return None
        if mine.id == "id":
            return ("without", value) if isinstance(op, expr.ast.NotEq) and isinstance(value, int) else None
        f = schema.field(t, mine.id)
        if f is None or f["type"] not in schema.STORED or f.get("sensitive") or isinstance(value, (expr.Many, expr.Lazy, list, dict)) \
                and not isinstance(op, (expr.ast.In, expr.ast.NotIn)):
            return None
        if isinstance(value, datetime):
            value = value.isoformat(sep=" ", timespec="minutes")
        elif isinstance(value, date):
            value = value.isoformat()
        if isinstance(op, expr.ast.In) and not turned and isinstance(value, list):
            return "where", {"field": f["key"], "op": "one_of", "value": [str(v) for v in value]}
        said = PUSHED_OPS.get(type(op))
        if said is None:
            return None
        if value in (None, "") and said in ("equals", "not_equals"):
            return "where", {"field": f["key"], "op": "is_empty" if said == "equals" else "not_empty"}
        if value in (None, ""):
            return None
        said = TURNED_OPS.get(said, said) if turned else said
        return "where", {"field": f["key"], "op": said, "value": value}
    return None


class RecordContext(expr.Context):
    """Names in a formula or template: the record's own fields, `record` itself, plus today/now."""

    def __init__(self, engine, record, extra=None):
        super().__init__(extra or {}, today=engine.today, now=engine.now)
        self.engine, self.record = engine, record

    @property
    def functions(self):
        return {"convert": self.engine.convert, "workday": self.engine.workday, "add_workdays": self.engine.workday,
                "workdays_between": self.engine.workdays_between, "networkdays": self.engine.workdays_between,
                "moved_by": self._moved_by}

    def _moved_by(self, stage):
        """Who moved this record to a stage, from its history (the last of them): who approved the run, who recommended
        the award. None if nobody has."""
        if self.record is None or self.record.id is None:
            return None
        from ..models import RecordChange
        flow = (self.engine.tables.get(self.record.table) or {}).get("workflow")
        if not flow:
            return None
        rows = RecordChange.query.filter_by(record_id=self.record.id).filter(RecordChange.kind.in_(("created", "updated"))) \
            .order_by(RecordChange.id.desc()).all()
        uid = next((r.by_id for r in rows if r.by_id and str(((r.changes or {}).get(flow["field"]) or [None, None])[-1]).lower()
                    == str(stage).strip().lower()), None)
        return self.engine._person(uid) if uid else None

    def resolve(self, name):
        if name in self.names:
            return self.names[name]
        if name in ("record", "this"):
            return RecordProxy(self.engine, self.record) if self.record is not None else None
        if self.record is not None:
            t = self.engine.tables.get(self.record.table)
            if name in ("id", "created_at", "updated_at", "created_by") or (t and schema.field(t, name)):
                return self.engine.value(self.record, name, typed=True)
            many = self.engine.linking(self.record, name)  # this job's payments
            if many is not None:
                return many
            if t and name == schema.slug(t.get("singular") or ""):  # "client.name" in a rule about clients: that client
                return RecordProxy(self.engine, self.record)
        if name == "owner":  # the business's owner, as a person a field can hold ("approved_by": owner)
            return getattr(self.engine.ws, "owner_id", None)
        whole = schema.list_named(self.engine.tables, name) if name not in ("today", "now") else None
        if whole is not None:  # a whole list: any(bookings where …), read only as far as it must be
            return Rows(self.engine, whole["key"])
        return super().resolve(name)


def _shown(self, source, value):
    """A value put into text ("Invoice {{record.number}} for {{record.total}}"), as people expect to read it: through the
    field it came from when it's a plain field path, so money reads $2,640.00 and dates 30 Nov 2026."""
    parts = source.strip().split(".")
    if not all(p.isidentifier() for p in parts):
        return expr._text(str(value) if isinstance(value, RecordProxy) else value)
    head = self.names.get(parts[0], RecordProxy(self.engine, self.record) if self.record is not None and parts[0] == "record" else None)
    if parts[0] == "record" or parts[0] in self.names:
        holder, rest = head, parts[1:]
    else:
        holder, rest = (RecordProxy(self.engine, self.record) if self.record is not None else None), parts
    f = None
    for i, part in enumerate(rest):
        if not isinstance(holder, RecordProxy):
            f = None
            break
        f = self.engine.field(holder.record.table, part)
        if f is None:
            break
        if i < len(rest) - 1:
            holder = holder.field(part)
    if isinstance(value, RecordProxy):
        return str(value)
    if f is not None and value is not None:
        return self.engine.display(f, value)
    return expr._text(value)


RecordContext.shown = _shown


def automations_words(engine, conditions, table):
    from .automations import describe_condition
    return " and ".join(describe_condition(engine, c, table).replace("“", "").replace("”", "") for c in conditions)


_overlap = expr.spans_overlap  # two spans of dates or times: one with no end is that day or moment


CURRENCIES = (("Australia/", "AUD"), ("Pacific/Auckland", "NZD"), ("Pacific/Chatham", "NZD"), ("Europe/London", "GBP"),
              ("Europe/Dublin", "EUR"), ("Europe/Zurich", "CHF"), ("Europe/Oslo", "NOK"), ("Europe/Stockholm", "SEK"),
              ("Europe/Copenhagen", "DKK"), ("Europe/Warsaw", "PLN"), ("Europe/Prague", "CZK"), ("Europe/Budapest", "HUF"),
              ("Europe/", "EUR"), ("America/Toronto", "CAD"), ("America/Vancouver", "CAD"), ("America/Edmonton", "CAD"),
              ("America/Winnipeg", "CAD"), ("America/Halifax", "CAD"), ("America/St_Johns", "CAD"), ("America/Regina", "CAD"),
              ("America/Mexico_City", "MXN"), ("America/Sao_Paulo", "BRL"), ("America/", "USD"), ("US/", "USD"),
              ("Pacific/Honolulu", "USD"), ("Asia/Singapore", "SGD"), ("Asia/Hong_Kong", "HKD"), ("Asia/Tokyo", "JPY"),
              ("Asia/Kolkata", "INR"), ("Asia/Calcutta", "INR"), ("Asia/Dubai", "AED"), ("Asia/Kuala_Lumpur", "MYR"),
              ("Asia/Manila", "PHP"), ("Asia/Jakarta", "IDR"), ("Asia/Bangkok", "THB"), ("Asia/Shanghai", "CNY"),
              ("Asia/Seoul", "KRW"), ("Africa/Johannesburg", "ZAR"), ("Pacific/Fiji", "FJD"))


def currency_for(timezone):
    """The currency code of where a business is, from its time zone ("Australia/Perth" → AUD), or "" if unknown."""
    return next((code for start, code in CURRENCIES if str(timezone or "").startswith(start)), "")


FILLED = {"not_empty", "not empty", "filled", "filled in", "filled_in", "any", "anything", "*", "set", "entered", "any value"}
EMPTIED = {"empty", "is_empty", "blank", "nothing", "cleared", "none"}


class Engine:
    def __init__(self, workspace, by=None, member=None):
        self.ws = workspace
        self.bp = workspace.blueprint or schema.empty()
        self.tables = {t["key"]: t for t in self.bp.get("tables", [])}
        self.by = by or "someone"
        self.member = member
        self.role = member.role if member is not None else None  # None: the platform itself (automations, jobs)
        self.user_id = member.user_id if member is not None else None
        self.teams = set(getattr(member, "teams", None) or []) if member is not None else set()
        self.portal_contact = None  # the portal's engine: the outside person (their record) it acts for
        self.stepped_up = False  # this person confirmed their sign-in in the last few minutes (moves that need it)
        self.held = []  # fields of the last update waiting for someone's approval (said on the screen)
        self.revealing = False  # sensitive values in full (only the engine making a bank file)
        cfg = current_app.config
        try:
            tz = ZoneInfo(workspace.timezone or cfg["DEFAULT_TIMEZONE"])
        except Exception:
            tz = ZoneInfo("UTC")
        self.now = datetime.now(tz).replace(tzinfo=None, second=0, microsecond=0)
        self.now_tz = tz
        self.today = self.now.date()
        self.currency = workspace.currency or cfg["CURRENCY_SYMBOL"]
        calendar = (workspace.settings or {}).get("calendar") or {}  # the business's working week, holidays, financial year
        self.workdays = set(calendar.get("workdays") or [0, 1, 2, 3, 4])
        self.holidays = {str(h)[:10] for h in calendar.get("holidays") or []}  # "2026-12-25 Christmas Day": the date counts
        self.fy_start = int(calendar.get("fy_start") or 7)
        money = (workspace.settings or {}).get("money") or {}
        self.base_currency = (money.get("base") or "").upper() or currency_for(str(tz))  # unset: the one where the business is
        self.rates = {str(k).upper(): float(v) for k, v in (money.get("rates") or {}).items() if to_number(v)}
        self._rows, self._by_id, self._memo, self._index, self._computing, self._seals = {}, {}, {}, {}, set(), {}
        self._kids, self._near, self._titled, self._misses, self._queried = {}, {}, {}, {}, {}  # lines per parent, what is shown
        self._counted = {}  # how many a query finds, counted by SQL (engine/planner.py)
        self.started = []  # automation runs this engine started (so a screen can say what ran)
        self._members, self._files, self._sources = None, {}, None
        self._depth, self._after, self._touched, self._failed = 0, [], set(), False
        self._before = {}  # each record written in this save, as it was before it (None: added in it), for its rules
        self._dirty = set()  # tables written since the caches were last brought up to date (only theirs are thrown away)
        self._batch = None  # an import's rows, committed together (see batch())
        self._open = set()  # records that weren't locked when this save began (a locked status set in it applies next time)
        self._asked_ids = None
        self._naming = set()  # records whose names are being worked out (one named by another that's named by it)

    # --- who may see and change what ----------------------------------------------------------------------------
    def _at_least(self, role):
        return self.role is None or ROLES.index(self.role) >= ROLES.index(role)

    def can(self, t, action="view"):
        """Whether this person may see (view) or change (edit) a table's records."""
        t = self.tables.get(t) if isinstance(t, str) else t
        if t is None:
            return False
        if action == "edit" and t.get("combines"):
            return False  # put together from other lists: they're changed there
        need = (t.get("access") or {}).get(action) or ("viewer" if action == "view" else "editor")
        if action == "edit":
            need = max(need, (t.get("access") or {}).get("view") or "viewer", "editor", key=ROLES.index)
        teams = (t.get("access") or {}).get("teams")
        if teams and self.role is not None and not self._at_least("admin") and not self.teams & set(teams):
            return False  # kept to a team (Payments for Finance), and admins
        changers = (t.get("access") or {}).get("edit_teams")
        if action == "edit" and changers and self.role is not None and not self._at_least("admin") and not self.teams & set(changers):
            return False  # everyone sees it; only these teams change it (the curators, the shared catalogue)
        return self._at_least(need)

    def is_workday(self, d):
        return d.weekday() in self.workdays and d.isoformat() not in self.holidays

    def workday(self, start, n):
        """The date n working days after (or before) a date: weekends and the business's holidays skipped."""
        d = to_date(start)
        n = int(to_number(n) or 0)
        if d is None:
            return None
        step = 1 if n >= 0 else -1
        for _ in range(abs(n)):
            d += timedelta(days=step)
            while not self.is_workday(d):
                d += timedelta(days=step)
        return d

    def workdays_between(self, a, b):
        """Working days from one date to another (counting the end, not the start)."""
        a, b = to_date(a), to_date(b)
        if a is None or b is None:
            return None
        sign, (lo, hi) = (1, (a, b)) if a <= b else (-1, (b, a))
        return sign * sum(1 for i in range(1, (hi - lo).days + 1) if self.is_workday(lo + timedelta(days=i)))

    def in_team(self, who):
        """Whether this person is in a team (the platform itself counts as everyone)."""
        return self.role is None or who in self.teams

    def _allowed_to(self, who):
        """A field's or page's "access": a role (admins and up), or a team (and admins)."""
        if who in ROLES:
            return self._at_least(who)
        return self.role is None or self._at_least("admin") or who in self.teams

    def sees(self, f):
        """Whether this person may see a field (pay rates, cost prices can be kept to admins). A lookup of a field they
        can't see is hidden too."""
        if f is None:
            return False
        if f.get("access") and not self._allowed_to(f["access"]):
            return False
        if f["type"] == "lookup":
            source = self.source(f)
            return source is None or not source.get("access") or self._allowed_to(source["access"])
        return True

    def visible(self, t):
        """A table as this person sees it: only the fields they may see (screens, exports, the assistant use this)."""
        if self.role is None or not any(not self.sees(f) for f in t["fields"]):
            return t
        keys = {f["key"] for f in t["fields"] if self.sees(f)}
        out = {**t, "fields": [f for f in t["fields"] if f["key"] in keys]}
        out["views"] = [{**v, "columns": [c for c in v["columns"] if c in keys]} if v.get("columns") else v for v in t.get("views", [])]
        if t.get("sections"):
            out["sections"] = [{**s, "fields": [k for k in s["fields"] if k in keys]} for s in t["sections"]]
        return out

    def visible_blueprint(self):
        """The design as this person sees it: the tables they may see, with the fields they may see."""
        if self.role is None:
            return self.bp
        tables = [self.visible(t) for t in self.bp.get("tables", []) if self.can(t)]
        keys = {t["key"] for t in tables}
        pages = [{**p, "widgets": [w for w in p["widgets"] if not w.get("table") or w["table"] in keys]} for p in self.bp.get("pages", [])
                 if not p.get("access") or self._allowed_to(p["access"])]
        return {**self.bp, "tables": tables, "pages": pages}

    def _owner_rule(self, t):
        rules = self._owner_rules(t)
        return rules[0] if rules else None

    def _owner_rules(self, t):
        """The person fields that decide which of a table's records this person sees, every one of which must name them
        (their organisation's records, and of those their own scores)."""
        return [rows["field"] for rows in self._row_rules(t)]

    def _row_rules(self, t):
        if self.role is None:
            return []
        return [rows for rows in schema.row_rules(t) if self.role in rows["roles"] and not self.teams & set(rows.get("teams") or [])]

    def allowed(self, record):
        """Whether this person may see this record (tables can limit people to the records that are theirs)."""
        if record is None:
            return False
        t = self.tables.get(record.table)
        if not self.can(t):
            return False
        rules = self._row_rules(t)
        if not rules or record.id in self._asked():
            return True
        return all(self._names_me(record, rows["field"], rows.get("shared_when_empty")) for rows in rules)

    def _names_me(self, record, owner, shared_when_empty=False):
        """Whether a record's person field (or a linked record's) is this person; with shared_when_empty, a record with
        nobody there (no organisation: the platform's own) is everyone's."""
        if shared_when_empty and self._nobody_at(record, owner):
            return True
        if owner == "created_by":  # by who they are, not their name (anyone can change their name)
            return record.created_by_id is not None and record.created_by_id == self.user_id
        if "." in owner:  # a linked record's person, through as many links as it takes: an order is a rep's when its
            values = self._people_through(record, owner)  # customer is; an offer is a buyer's members' through its round
        else:
            value = self.value(record, owner)  # a person field, or a lookup of one
            values = value if isinstance(value, list) else [value]
        return str(self.user_id) in [str(v) for v in values if v is not None]

    def _people_through(self, record, path):
        """The people a path through links ends at ("round.buyer.members"): every one named where it leads."""
        parts, reached = path.split("."), [record]
        for part in parts[:-1]:
            further = []
            for r in reached:
                f = self.field(r.table, part)
                value = (r.data or {}).get(part) if f is not None and f["type"] == "link" else None
                ids = value if isinstance(value, list) else [value]
                further += [x for x in (self.get(i, f["table"]) for i in ids if str(i or "").isdigit()) if x is not None]
            reached = further
        out = []
        for r in reached:
            value = (r.data or {}).get(parts[-1])
            out += value if isinstance(value, list) else [value]
        return [v for v in out if v not in (None, "")]

    def _nobody_at(self, record, owner):
        """Whether a record names nobody where a rule looks (its link is empty, or the person field is)."""
        if owner == "created_by":
            return record.created_by_id is None
        head = owner.partition(".")[0]
        if (record.data or {}).get(head) in (None, "", []):
            return True
        return "." in owner and not self._people_through(record, owner)

    def _asked(self):
        """The records this person was asked to approve or do something on, or answered for: they may open those, whoever
        else's they are (finance approving a rep's order)."""
        if self._asked_ids is None:
            from ..models import Approval
            rows = Approval.query.filter(Approval.workspace_id == self.ws.id, Approval.record_id.isnot(None),
                                         db.or_(Approval.assignee_id == self.user_id, Approval.decided_by_id == self.user_id,
                                                Approval.assignee_id.is_(None))).all() if self.user_id else []
            role = lambda r: r[5:] in self.teams if r.startswith("team:") else r in ROLES and self._at_least(r)  # noqa: E731
            self._asked_ids = {a.record_id for a in rows if self.user_id in (a.assignee_id, a.decided_by_id)
                               or (a.assignee_id is None and a.status == "pending" and role(a.role or "admin"))}
        return self._asked_ids

    def system(self):
        """The same workspace, acting as the platform itself: what automations run as, whoever's save started them (they
        fill read-only fields and may change locked records; the people who set them up decided that)."""
        return self if self.role is None and self.portal_contact is None else Engine(self.ws, by=self.by)  # (never an outside person's)

    def by_code(self, code, names=True):
        """The record a scanned or typed code is (its barcode, its number such as INV-0042, or with `names` its name),
        among those this person may see; None if none is, or more than one."""
        wanted, found = str(code or "").strip().lower(), []
        if not wanted:
            return None
        for t in self.visible_blueprint().get("tables", []):
            if t.get("embedded"):
                continue
            codes = [f for f in t["fields"] if f["type"] in ("barcode", "autonumber")]
            if not codes and not names:
                continue  # nothing here it could be
            for r in self._coded(t, codes, wanted, names):
                said = {str((r.data or {}).get(f["key"]) or "").strip().lower() for f in codes} | \
                    {self.display(f, (r.data or {}).get(f["key"])).lower() for f in codes} | ({self.title(r).strip().lower()} if names else set())
                if wanted in said:
                    found.append(r)
            if len(found) > 1:
                return None
        return found[0] if found else None

    def _coded(self, t, codes, wanted, names):
        """The records of t this person may see whose code (or with `names`, name) may be `wanted`: found by SQL, a
        superset by_code checks one by one; the whole table when SQL can't read it."""
        plan = self._sql_plan(t["key"])
        if plan is None or plan.access is None:
            return self.visible_rows(t["key"])
        from .planner import _sql
        test = _sql(plan.planner.coded(codes, wanted))
        rows = {r.id: r for r in db.session.query(Record).filter(*plan.where, test).order_by(Record.id)}
        if names:  # its name has what was typed in it (stripped, it may be all of it)
            seen = plan.planner.access()[1]
            rows.update({r.id: r for r in plan.planner.titled(wanted, True) if seen is True or self.allowed(r)})
        found = [rows[i] for i in sorted(rows)]
        for r in found:
            self._by_id[r.id] = r
        return found

    def visible_rows(self, table):
        rows = self.rows(table)
        if self.role is None:
            return rows
        t = self.tables.get(table)
        if not self.can(t):
            return []
        return rows if not self._owner_rules(t) else [r for r in rows if self.allowed(r)]

    # --- reading ------------------------------------------------------------------------------------------------
    def reset(self):
        self._rows, self._by_id, self._memo, self._index, self._files = {}, {}, {}, {}, {}
        self._seals, self._asked_ids = {}, None
        self._kids, self._near, self._titled, self._misses, self._queried, self._dirty = {}, {}, {}, {}, {}, set()
        self._counted = {}

    def _forget(self, tables):
        """After a write that isn't committed yet (inside atomic()): only what was read from the tables written is read
        again. Records of other tables stay as they were read; what's worked out (formulas, rollups) is worked out again,
        since it may total what changed."""
        for table in tables:
            self._rows.pop(table, None)
            self._near.pop(table, None)
            for kept in (self._index, self._kids, self._misses):
                for key in [k for k in kept if k[0] == table]:
                    del kept[key]
            for key in [k for k in self._titled if k[0] == table]:
                del self._titled[key]
        self._memo, self._files, self._seals, self._asked_ids, self._queried, self._counted = {}, {}, {}, None, {}, {}
        self._dirty = set()

    def _sql_plan(self, table, filters=None, q=None, sort=None, everyone=False):
        """How SQL answers a query of this table (engine/planner.py), or None to read it in Python: a table already read
        whole is checked where it is, and a sealed one only Python can read. everyone: every record, whoever's asking."""
        if table in self._rows or table not in self.tables:
            return None
        from .planner import Plan
        plan = Plan(self, table, filters, q, sort, everyone=everyone)
        return plan if plan.usable else None

    def page(self, table, filters=None, q=None, sort=None, offset=0, limit=50, sums=()):
        """(one page of a query's records, how many it finds in all): query(...)[offset:offset + limit] and its length,
        from SQL alone (LIMIT, COUNT) when SQL can settle every record. `sums`: the columns the page will be totalled by
        (total()), added up as the records are counted."""
        plan = self._sql_plan(table, filters, q, sort)
        if plan is None:
            rows = self.query(table, filters, q=q, sort=sort)
            return rows[offset:offset + limit], len(rows)
        return plan.page(offset, limit, sums)

    def count(self, table, filters=None, q=None, everyone=False):
        """len(query(table, filters, q)), counted by SQL when it can be."""
        plan = self._sql_plan(table, filters, q, everyone=everyone)
        return plan.count() if plan is not None else len(self.query(table, filters, q=q, everyone=everyone))

    def total(self, table, aggregate="count", key=None, filters=None, q=None, everyone=False):
        """aggregate(query(table, filters, q), aggregate, key): a count, sum, average, lowest, highest or middle, from SQL
        when the field is kept (not worked out) and SQL can settle every record."""
        from .planner import UNSURE
        plan = self._sql_plan(table, filters, q, everyone=everyone)
        got = plan.total(aggregate, key) if plan is not None else UNSURE
        if got is not UNSURE:
            return got
        asked = (table, repr(filters), q, everyone)  # a list's worked-out columns totalled one after another: its records read once
        if asked not in self._queried:
            self._queried[asked] = self.query(table, filters, q=q, everyone=everyone)
        return self.aggregate(self._queried[asked], aggregate, key)

    def neighbours(self, table, record):
        """The records before and after this one among those this person may see, in the order they were added, and where
        it is among them: {"prev", "next", "at", "of"}."""
        plan = self._sql_plan(table)
        if plan is None or plan.access is None:
            order = [x.id for x in self.visible_rows(table)]
            at = order.index(record.id) if record.id in order else -1
            return {"prev": order[at - 1] if at > 0 else None, "next": order[at + 1] if 0 <= at < len(order) - 1 else None,
                    "at": at + 1, "of": len(order)}
        rid, n = record.id, db.func
        of, before, prev, nxt, here = db.session.query(
            n.count(Record.id), n.sum(db.case((Record.id < rid, 1), else_=0)), n.max(db.case((Record.id < rid, Record.id))),
            n.min(db.case((Record.id > rid, Record.id))), n.sum(db.case((Record.id == rid, 1), else_=0))).filter(*plan.where).one()
        if not here:
            return {"prev": None, "next": None, "at": 0, "of": of or 0}
        return {"prev": prev, "next": nxt, "at": (before or 0) + 1, "of": of or 0}

    def find(self, table, q, limit=8):
        """(the first records a search finds among those this person may see, how many it finds): search(visible_rows(
        table), q) from SQL. The count is exact, or "2000+" when there are more than that to check in Python."""
        plan = self._sql_plan(table, q=q)
        if plan is None:
            rows = self.search(self.visible_rows(table), q)
            return rows[:limit], len(rows)
        return plan.first(limit)

    def _found(self, table, rows, fetch=True):
        """Records of a table about to be shown or worked on: kept by id, and remembered so the lines and totals of each
        are fetched for all of them at once (children). With `fetch`, the records their links point at are read in one
        go too (a page's customers, for their names and lookups)."""
        for r in rows:
            self._by_id[r.id] = r
        self._near[table] = {r.id for r in rows}
        t = self.tables.get(table)
        if not fetch or not t or not rows:
            return
        wanted = set()
        for f in t["fields"]:
            if f["type"] == "link":
                for r in rows:
                    v = (r.data or {}).get(f["key"])
                    wanted.update(int(x) for x in (v if isinstance(v, list) else [v]) if isinstance(x, int) and not isinstance(x, bool))
        wanted -= set(self._by_id)
        if wanted:
            for r in Record.query.filter(Record.workspace_id == self.ws.id, Record.id.in_(sorted(wanted))).all():
                self._by_id[r.id] = r

    def _titled_ids(self, table, said, part=False):
        """The ids of a table's records whose name (title) is `said`, or with `part` has it in it, lower-cased (a link
        filtered or searched by the name people see)."""
        key = (table, said, part)
        if key not in self._titled:
            from .planner import Planner
            head = said.split(" · ")[0].strip()  # "40141700 · pipe fittings": the code is what's kept
            if table in self._rows or table not in self.tables:
                rows = self.rows(table)
            else:
                rows = Planner(self, table).titled(head, part)
            tk = schema.title_field(self.tables[table]) if table in self.tables else None

            def called(r):  # by its full name, or the number or code alone (CC100 for "CC100 · Maintenance")
                names = {self.title(r).lower(), str((r.data or {}).get(tk) or "").strip().lower()} - {""}
                return any(said in n for n in names) if part else said in names
            self._titled[key] = {r.id for r in rows if called(r)}
        return self._titled[key]

    # --- sealed records (bids until the tender closes) -----------------------------------------------------------------
    def _seal(self, record):
        """(the sealed record: itself, or the one its lines belong to; when it opens, or None while no date is set), or
        None if it isn't sealed at all."""
        if record is None:
            return None
        key = record.id or id(record)
        if key in self._seals:
            return self._seals[key]
        t, target = self.tables.get(record.table), record
        if t and not t.get("sealed") and t.get("embedded"):
            parent = self._parent(record)
            if parent is not None and (self.tables.get(parent.table) or {}).get("sealed"):
                target, t = parent, self.tables[parent.table]
        found = None
        if t and t.get("sealed"):
            at, table = target, t
            parts = t["sealed"]["until"].split(".")
            for part in parts[:-1]:  # through links, read as stored (the record's own values may be sealed)
                f = schema.field(table, part)
                at = self.get((at.data or {}).get(part), f["table"]) if at is not None and f and f["type"] == "link" else None
                table = self.tables.get(f["table"]) if f and f["type"] == "link" else None
            raw = (at.data or {}).get(parts[-1]) if at is not None else None
            when = to_datetime(raw)
            if when is not None and isinstance(raw, str) and len(raw.strip()) <= 10:
                when = when.replace(hour=23, minute=59)  # a closing day: open until its end
            found = (target, when)
        self._seals[key] = found
        return found

    def made_it(self, record):
        """Whether this person made a record (staff who added it, or the outside person it's for, in the portal)."""
        if self.portal_contact is not None:
            return self.portal_reaches(record, self.portal_contact)
        return self.role is not None and self.user_id is not None and record.created_by_id == self.user_id

    def sealed_from_me(self, record):
        """Whether what's in a record is hidden from this person: it's sealed, it hasn't opened, and they didn't make it."""
        seal = self._seal(record)
        if seal is None:
            return False
        key = ("hidden", record.id or id(record))
        if key not in self._seals:
            target, opens = seal
            self._seals[key] = (opens is None or self.now < opens) and not self.made_it(target)
        return self._seals[key]

    def seal_note(self, record):
        """For people: "Sealed until 14 Nov 2026 23:59" / "Opened 14 Nov 2026 23:59", or None."""
        seal = self._seal(record)
        if seal is None:
            return None
        opens = seal[1]
        if opens is None:
            return "Sealed until its closing date is set"
        return f"{'Opened' if self.now >= opens else 'Sealed until'} {opens.day} {opens:%b %Y %H:%M}"

    def _sealed_lock(self, record):
        """(why it can't be changed, the fields that still can) for a sealed record, or None."""
        seal = self._seal(record)
        if seal is None:
            return None
        target, opens = seal
        t = self.tables[target.table]
        if opens is not None and self.now >= opens:
            stage = (t.get("workflow") or {}).get("field")
            keep = set(t["sealed"].get("keep_open", [])) | ({stage} if stage else set())
            return f"{self.seal_note(record)}: what was sent can't be changed now", keep if target is record else set()
        if self.role is not None and not self.made_it(target):
            return f"{self.seal_note(record)}: only whoever made it can change it", set()
        return None

    # --- the portal ------------------------------------------------------------------------------------------------
    def portal_reaches(self, record, contact_id):
        """Whether a record is an outside person's: its portal link points at them, at a record that links to them (a
        job's site's client), or through a named link of that record (a clarification's tender's invited suppliers); its
        lines are theirs when the record is. The portal's "show_if" must hold too."""
        t = self.tables.get(record.table) if record is not None else None
        if t is None:
            return False
        if not t.get("portal") and t.get("embedded"):
            parent = self._parent(record)
            return parent is not None and self.portal_reaches(parent, contact_id)
        portal = t.get("portal")
        if not portal:
            return False
        if portal.get("everyone"):  # every outside person in its portal: those that meet its show_if
            contact = self.get(contact_id)
            return contact is not None and schema.portal_shows(self.tables, t, contact.table) and \
                (not portal.get("show_if") or self.matches(record, portal["show_if"]))
        people = next((p["table"] for p in schema.portals(self.bp) if schema.portal_shows(self.tables, t, p["table"])), None)
        head, _, hop = portal["via"].partition(".")
        via = self.field(t["key"], head)
        own = schema.theirs_by_own_link(self.tables, t, people) if people else None
        if own is not None:  # it's the one their own record names (a seller person's seller)
            return record.id in self._belongs_to(contact_id, t["key"]) and (not portal.get("show_if") or self.matches(record, portal["show_if"]))
        if via is None or via["type"] != "link" or people is None:
            return False
        ids = lambda v: [int(x) for x in (v if isinstance(v, list) else [v]) if str(x).isdigit()]  # noqa: E731
        targets = ids((record.data or {}).get(head))
        if via["table"] == people and not hop:
            found = contact_id in targets
        else:
            middle = self.tables.get(via["table"]) or {}
            on = schema.field(middle, hop) if hop else next((f for f in middle.get("fields", []) if f["type"] == "link" and f["table"] == people), None)
            mids = [m for m in (self.get(x, via["table"]) for x in targets) if m is not None]
            if on is not None and on["type"] == "link" and on["table"] != people:  # one more record on: a document's case's seller
                last = next((f for f in (self.tables.get(on["table"]) or {}).get("fields", []) if f["type"] == "link" and f["table"] == people), None)
                ends = [x for m in mids for x in ids((m.data or {}).get(on["key"]))]
                found = any(contact_id in ids((e.data or {}).get(last["key"])) for e in (self.get(x, on["table"]) for x in ends) if e is not None) \
                    if last is not None else bool(set(ends) & self._belongs_to(contact_id, on["table"]))
            elif on is None and not hop:  # the people belong to it instead (a seller's people, each linked to their seller)
                found = bool(set(targets) & self._belongs_to(contact_id, via["table"]))
            else:
                found = on is not None and any(contact_id in ids((m.data or {}).get(on["key"])) for m in mids)
        return found and (not portal.get("show_if") or self.matches(record, portal["show_if"]))

    def _belongs_to(self, contact_id, table):
        """The records of a list an outside person belongs to, through their own record's links to it (a seller's person:
        their seller)."""
        contact = self.get(contact_id)
        if contact is None:
            return set()
        out = set()
        for f in (self.tables.get(contact.table) or {}).get("fields", []):
            if f["type"] == "link" and f["table"] == table:
                value = (contact.data or {}).get(f["key"])
                out |= {int(x) for x in (value if isinstance(value, list) else [value]) if str(x).isdigit()}
        return out

    def members(self):
        """{user id: name} of the people in this workspace (for person fields)."""
        if self._members is None:
            rows = db.session.query(User).join(Membership, Membership.user_id == User.id).filter(Membership.workspace_id == self.ws.id).all()
            self._members = {u.id: u.display for u in rows}
            self._emails = {u.id: u.email for u in rows}
        return self._members

    def staff(self):
        """{user id: name} of the business's own people, to pick for a person field: never its outside people (a seller's
        person signed in to the portal) nor a stand-in."""
        if getattr(self, "_staff", None) is None:
            rows = db.session.query(User).join(Membership, Membership.user_id == User.id).filter(
                Membership.workspace_id == self.ws.id, Membership.role != "portal", User.email != "stand-in@example.com").all()
            self._staff = {u.id: u.display for u in rows}
        return self._staff

    def files(self, record, key):
        """The files in a file or image field: [{"id", "name", "mime", "size"}]."""
        if record is None or record.id is None:
            return []
        if (record.id, key) not in self._files:
            rows = File.query.filter_by(record_id=record.id, field=key, deleted_at=None).order_by(File.id).all()
            self._files[(record.id, key)] = [{"id": x.id, "name": x.name, "mime": x.mime, "size": x.size, "sha256": x.sha256} for x in rows]
        return self._files[(record.id, key)]

    def rows(self, table):
        if table not in self._rows:
            rows = (Record.query.filter_by(workspace_id=self.ws.id, table=table, deleted_at=None).order_by(Record.id).all())
            self._rows[table] = rows
            for r in rows:
                self._by_id[r.id] = r
        return self._rows[table]

    def get(self, record_id, table=None):
        try:
            record_id = int(record_id)
        except (TypeError, ValueError):
            return None
        if record_id not in self._by_id:
            r = db.session.get(Record, record_id)
            if r is None or r.workspace_id != self.ws.id:
                return None
            self._by_id[record_id] = r
        r = self._by_id[record_id]
        if r.deleted_at is not None or (table and r.table != table):
            return None
        return r

    def field(self, table, key):
        return schema.field(self.tables.get(table), key)

    def title(self, record):
        """What a record is called. One named by a link (a product's stock at a warehouse) is named by its other links
        too, and one named only by its number (L-0003) by what it links to (L-0003 · Danny Cooper), so a picker never
        offers the same name twice or a code nobody knows."""
        if record is None:
            return ""
        t = self.tables.get(record.table)
        key = schema.title_field(t) if t else None
        if record.id in self._naming:  # two records named by each other: the plain name, once round
            key = key if key and schema.field(t, key)["type"] != "link" else None
        self._naming.add(record.id)
        try:
            value = self.value(record, key) if key else None
            text = self.display(schema.field(t, key), value) if key else ""
            kind = schema.field(t, key)["type"] if key else None
            own = schema.name_field(t, key) if key and kind in ("autonumber", "text") else None
            named = str((record.data or {}).get(own) or "").strip() if own else ""
            if text not in ("", "—") and named and (kind == "autonumber" or schema.is_code(key)):
                text = f"{text} · {named}"  # REQ-0001 · Pipe fittings for the wash plant; 40141700 · Pipe fittings
            elif text not in ("", "—") and kind in ("link", "autonumber", "user") and record.id is not None and len(self._naming) == 1:
                choice = [str((record.data or {}).get(f["key"])) for f in t["fields"]  # what kind it is (a limit for Requests)
                          if kind == "user" and f["type"] == "select" and f["key"] != (t.get("workflow") or {}).get("field")
                          and (record.data or {}).get(f["key"]) not in (None, "")][:1]
                others = choice + [self.display(f, self.value(record, f["key"])) for f in t["fields"]
                                   if f["type"] == "link" and f["key"] != key and not f.get("multiple") and (record.data or {}).get(f["key"])][:2 - len(choice)]
                text = " · ".join([text] + [o for o in others if o not in ("", "—")])
        finally:
            self._naming.discard(record.id)
        return text if text not in ("", "—") else f"{t['singular'] if t else 'Record'} #{record.id}"

    def value(self, record, key, typed=False):
        """A field's value: stored ones as saved, formulas and rollups worked out. typed: dates as dates, links as records."""
        if key == "id":
            return record.id
        if key == "created_by":  # who added it: a person, as a person field's value is (whoever added a seller doesn't approve its bank details)
            uid = getattr(record, "created_by_id", None)
            return (self._person(uid) if typed else uid) if uid else None
        if key in ("created_at", "updated_at"):
            stamp = getattr(record, key)
            if typed and stamp:  # stored in UTC; the day is the business's
                return stamp.replace(tzinfo=ZoneInfo("UTC")).astimezone(self.now_tz).date()
            return stamp
        f = self.field(record.table, key)
        if f is None:
            return None
        if f["type"] not in ("link", "autonumber") and f["type"] not in schema.SYSTEM and self.sealed_from_me(record) \
                and key != ((self.tables.get(record.table) or {}).get("workflow") or {}).get("field"):
            return None  # a sealed bid: nobody but its maker sees what's in it until it opens
        if f.get("sensitive"):
            stored = (record.data or {}).get(key)
            return reveal(stored) if self.revealing else masked(stored)
        if f["type"] in schema.SYSTEM:
            raw = {"created_time": record.created_at, "modified_time": record.updated_at or record.created_at,
                   "created_by": record.created_by, "modified_by": record.updated_by or record.created_by}[f["type"]]
            if isinstance(raw, datetime):  # stored in UTC; shown in the workspace's time
                raw = (raw.replace(tzinfo=ZoneInfo("UTC")).astimezone(self.now_tz).replace(tzinfo=None)).isoformat(timespec="minutes")
        elif f["type"] == "button":
            raw = None
        elif f["type"] == "formula":
            raw = self._formula(record, f)
        elif f["type"] == "rollup":
            raw = self._rollup(record, f)
        elif f["type"] == "lookup":
            raw = self._lookup(record, f)
        elif f["type"] in schema.FILES:
            raw = self.files(record, key)
        elif f["type"] == "url" and f.get("template"):
            raw = (record.data or {}).get(key) or self._from_template(record, f)
        else:
            raw = (record.data or {}).get(key)
        if not typed:
            return raw
        return self._typed(f, raw)

    def _from_template(self, record, f):
        """A link made from the record's details ({{record.tracking_number}}, each value made safe for an address);
        none until every detail it uses is filled in."""
        from urllib.parse import quote
        missing = []

        def part(m):
            value = expr.render("{{" + m.group(1) + "}}", RecordContext(self, record, {}))
            if value in (None, "", "None"):
                missing.append(m.group(1))
                return ""
            return quote(str(value), safe="")
        try:
            built = expr.TEMPLATE.sub(part, f["template"])
        except expr.ExprError:
            return None
        return None if missing or not built.startswith(("https://", "http://")) else built

    def _lookup(self, record, f):
        if "where" in f:  # found by its conditions (the limit that covers the amount, the price in force that day)
            memo_key = (record.id or id(record), f["key"])
            if memo_key in self._memo:
                return self._memo[memo_key]
            if memo_key in self._computing:
                return None
            self._computing.add(memo_key)
            try:
                rows = [r for r in self.rows(f["table"]) if r.id != record.id and self.matches(r, f["where"], about=record)]
                if f.get("sort"):
                    rows = self.sort(rows, f["sort"])
                value = self._at_path(rows[0], f["field"])[1] if rows else None
            finally:
                self._computing.discard(memo_key)
            self._memo[memo_key] = value
            return value
        link = self.field(record.table, f["link"])
        raw = (record.data or {}).get(f["link"])
        if link and link.get("multiple"):
            values = [self._at_path(x, f["field"])[1] for x in (self.get(i, link["table"]) for i in (raw or [])) if x is not None]
            return [v for v in values if v not in (None, "")] or None
        linked = self.get(raw, link["table"]) if link else None
        return self._at_path(linked, f["field"])[1] if linked else None

    def linked(self, record, f):
        """The records a link field points at (one, or several for a link to many)."""
        raw = (record.data or {}).get(f["key"]) if record is not None else None
        ids = raw if isinstance(raw, list) else ([raw] if raw not in (None, "") else [])
        return [x for x in (self.get(i, f["table"]) for i in ids) if x is not None]

    def source(self, f):
        """The field a lookup shows (lookups look and behave like it)."""
        if self._sources is None:
            self._sources = {}
            for t in self.tables.values():
                for x in t["fields"]:
                    if x["type"] == "lookup":
                        self._sources[id(x)] = schema.lookup_source(t, x, self.tables)
        return self._sources.get(id(f)) if f is not None else None

    def _person(self, uid):
        name = self.members().get(int(uid))
        return Person(name, int(uid), self) if name is not None else None

    def _typed(self, f, raw):
        if f["type"] == "lookup":
            source = self.source(f)
            return self._typed(source, raw) if source else raw
        kind = f.get("format") if f["type"] == "formula" else f["type"]
        if raw is None or raw == "" or raw == []:
            return None
        if kind == "multi_select":
            return list(raw) if isinstance(raw, list) else [raw]
        if kind == "user" and f.get("multiple"):
            return [self._person(x) for x in (raw if isinstance(raw, list) else [raw]) if str(x).isdigit()]
        if kind == "user":
            return self._person(raw) if str(raw).isdigit() else None
        if kind == "location":
            return raw if isinstance(raw, dict) else None
        if kind in ("duration", "rating"):
            return to_number(raw)
        if kind in schema.FILES:
            return Files(raw)
        if kind == "date":
            return to_date(raw)
        if kind in ("datetime", "created_time", "modified_time"):
            return to_datetime(raw)
        if kind == "tags":
            return list(raw) if isinstance(raw, list) else [raw]
        if kind in schema.NUMERIC or kind == "number" or f["type"] == "rollup":
            return to_number(raw) if not isinstance(raw, (int, float)) else raw
        if kind == "checkbox":
            return to_bool(raw)
        if f["type"] == "link" and f.get("multiple"):
            return [RecordProxy(self, x) for x in (self.get(i, f["table"]) for i in (raw if isinstance(raw, list) else [raw])) if x]
        if f["type"] == "link":
            linked = self.get(raw, f["table"])
            return RecordProxy(self, linked) if linked else None
        return raw

    def _formula(self, record, f):
        memo_key = (record.id or id(record), f["key"])  # a record not saved (a sub-assembly's part) is its own
        if memo_key in self._memo:
            return self._memo[memo_key]
        if memo_key in self._computing:
            return None  # a formula that (indirectly) uses itself
        self._computing.add(memo_key)
        try:
            value = expr.evaluate(schema.percents_fixed(self.tables.get(record.table), f["expression"], self.tables), RecordContext(self, record))
        except expr.ExprError:
            value = None
        finally:
            self._computing.discard(memo_key)
        if isinstance(value, RecordProxy):
            value = str(value)
        if isinstance(value, datetime) and (value.hour, value.minute) != (0, 0):
            value = value.isoformat(timespec="minutes")
        elif isinstance(value, (date, datetime)):
            value = value.isoformat()[:10]
        if isinstance(value, float) and f.get("format") == "money":
            value = round(value, 2)
        self._memo[memo_key] = value
        return value

    def children(self, table, via, parent_id):
        """Records of `table` whose link field `via` points at parent_id. They're fetched for the parents being shown
        together (the customers on a page, for their invoice totals), not by reading the whole of `table`, unless it's
        read already or most of it is wanted."""
        index_key = (table, via)
        if index_key not in self._index:
            kids = self._kids.setdefault(index_key, {})
            if parent_id in kids:
                return kids[parent_id]
            wanted = self._kids_wanted(table, via, parent_id, kids)
            if wanted:
                from .planner import children_of
                found = {i: [] for i in wanted}
                for r in children_of(self, table, via, wanted):
                    self._by_id[r.id] = r
                    target = (r.data or {}).get(via)
                    for one in (target if isinstance(target, list) else [target]):  # as the index below counts them
                        if one is not None and not isinstance(one, (list, dict)):
                            key = int(one) if str(one).isdigit() else one
                            if key in found:
                                found[key].append(r)
                kids.update(found)
                return kids[parent_id]
            index = {}
            for r in self.rows(table):
                target = (r.data or {}).get(via)
                for one in (target if isinstance(target, list) else [target]):  # a link to many counts under each
                    if one is not None:
                        index.setdefault(int(one) if str(one).isdigit() else one, []).append(r)
            self._index[index_key] = index
        return self._index[index_key].get(parent_id, [])

    def _kids_wanted(self, table, via, parent_id, kids):
        """The parents whose lines to fetch now (this one, and the others of its table being shown), or None to read the
        whole line table: it's read already, the parent isn't a plain id, or so many are wanted that reading it whole is
        quicker."""
        f = self.field(table, via)
        if table in self._rows or f is None or f["type"] != "link" or not isinstance(parent_id, int) or isinstance(parent_id, bool) \
                or not re.fullmatch(r"[A-Za-z_]\w*", via):
            return None
        index_key = (table, via)
        self._misses[index_key] = self._misses.get(index_key, 0) + 1
        near = self._near.get(f["table"]) or set()
        wanted = {parent_id} | ({i for i in near if i not in kids} if parent_id in near else set())
        if len(wanted) > 1000 or self._misses[index_key] > 40:  # one by one for most of a table: read it whole instead
            return None
        return wanted

    def related(self, record, table, via):
        """The records of `table` that reach this record through `via` (schema.reach): their link to it, a path through
        their links (an owner's rent, through lease and property), or the same to the record this one links to (a
        statement's owner)."""
        found = schema.reach(self.tables[record.table], self.tables[table], via, self.tables)
        if not found:
            return []
        path, anchor = found
        at = (record.data or {}).get(anchor) if anchor else record.id
        if at in (None, "", []):
            return []
        links = schema.links_on(self.tables[table], path, self.tables)
        ids = [int(at) if str(at).isdigit() else at]
        for i in range(len(links) - 1, -1, -1):  # back from the record reached to the records totalled
            rows = [r for x in ids for r in self.children(links[i - 1]["table"] if i else table, links[i]["key"], x)]
            ids = [r.id for r in rows]
        return rows

    def linking(self, record, name):
        """The records of another list that link to this one, by that list's name (a client's invoices), as a formula
        reads them; or None when no list links here by that name."""
        found = schema.backlink(self.tables, self.tables.get(record.table), name) if record is not None else None
        if found is None:
            return None
        other, f = found
        if record.id is None:
            return expr.Many()
        if other["key"] in self._rows or (other["key"], f["key"]) in self._index or other.get("embedded"):  # read already, or lines
            rows = self.children(other["key"], f["key"], record.id)
            return expr.Many(RecordProxy(self, r) for r in sorted(rows, key=lambda r: r.id or 0))
        return Rows(self, other["key"], [{"field": f["key"], "op": "equals", "value": record.id}])

    def _rollup(self, record, f):
        memo_key = (record.id, f["key"])
        if memo_key in self._memo:
            return self._memo[memo_key]
        rows = self.related(record, f["table"], f["via"]) if f["via"] else [r for r in self.rows(f["table"]) if r.id != record.id]
        if f.get("filters"):  # {{record.from}} in them is this record (a statement's period), not the one totalled
            rows = [r for r in rows if self.matches(r, f["filters"], about=record)]
        value = self.aggregate(rows, f["aggregate"], f.get("field"))
        self._memo[memo_key] = value
        return value

    def aggregate(self, rows, aggregate, key=None):
        if aggregate == "count":
            return len(rows)
        if aggregate in ("min", "max") and rows and self.kind(self.field(rows[0].table, key)) in ("date", "datetime"):
            days = [str(v) for v in (self.value(r, key) for r in rows) if v not in (None, "")]  # the first or last gift: kept as written, ISO, so in order
            return (min(days) if aggregate == "min" else max(days)) if days else None
        return self.of_numbers([n for n in (to_number(self.value(r, key)) for r in rows) if n is not None], aggregate)

    @staticmethod
    def of_numbers(numbers, aggregate):
        """A sum, average, lowest, highest or middle of numbers (none: 0 for a sum, else None)."""
        if not numbers:
            return 0 if aggregate == "sum" else None
        if aggregate == "sum":
            total = sum(numbers)
            return round(total, 2) if isinstance(total, float) else total
        if aggregate == "avg":
            return round(sum(numbers) / len(numbers), 2)
        if aggregate == "median":  # the middle one: a typical delivery time, unmoved by the odd very late one
            ordered, half = sorted(numbers), len(numbers) // 2
            return ordered[half] if len(ordered) % 2 else round((ordered[half - 1] + ordered[half]) / 2, 2)
        return min(numbers) if aggregate == "min" else max(numbers)

    def summable(self, f):
        """Whether a column's total means something: amounts and hours yes; prices, rates and percentages per unit no;
        amounts in different currencies no."""
        if f is None or self.kind(f) not in ("money", "duration", "number") or f.get("currency_field"):
            return False
        if f["type"] == "rollup" and f.get("aggregate") in ("count", "avg", "min", "max", "median"):  # adding up counts or averages means nothing
            return False
        if self.kind(f) == "number" and (f.get("unit") or any(w in f["key"] for w in ("on_hand", "stock", "level", "balance"))):
            return False  # kilos of flour and of butter don't add up to anything
        words = f"{f['key']} {f['label']}".lower()
        if any(w in words for w in ("unit", "price", "rate", "per ", "per_", "each", "fee", "percent", "discount", "terms", "days",
                                    "qty", "quantity", "rating", "age", "year", "number", "phone")):
            return f["type"] == "rollup" and f.get("aggregate") == "sum"
        return self.kind(f) in ("money", "duration") or (f["type"] in ("rollup", "formula") and self.kind(f) == "number")

    def _dated(self, f):
        """Whether a formula field works out a date though it says it shows a number (schema.dated_formula)."""
        cache = self.__dict__.setdefault("_dated_formulas", {})
        if id(f) not in cache:
            t = next((t for t in self.tables.values() if any(g is f for g in t.get("fields", []))), None)
            cache[id(f)] = schema.dated_formula(t, f)
        return cache[id(f)]

    def kind(self, f):
        """How a field's values look: its type, a formula's format, or the type of what a rollup totals."""
        if f is None:
            return "text"
        if f["type"] == "formula":
            if f.get("format") in (None, "", "number") and self._dated(f):
                return "date"  # confirmed_at + 2h, left showing a number: a date
            return f.get("format") or "number"
        if f["type"] == "rollup":
            if f["aggregate"] == "count":
                return "number"
            source = self.field(f["table"], f.get("field"))
            return self.kind(source) if source else "number"
        if f["type"] == "lookup":
            source = self.source(f)
            return self.kind(source) if source else "text"
        if f["type"] in ("created_time", "modified_time"):
            return "datetime"
        if f["type"] in ("created_by", "modified_by"):
            return "text"
        return f["type"]

    def symbol(self, f=None, record=None):
        """How amounts of a money field are marked: the workspace's own symbol, or the field's (or record's) currency."""
        code = (f or {}).get("currency")
        if f is not None and f.get("currency_field") and record is not None:
            code = str(self._at_path(record, f["currency_field"])[1] or "").strip().upper()[:3] or None
        if not code or code == self.base_currency:
            return self.currency
        return SYMBOLS.get(code, f"{code} ")

    def convert(self, amount, source, target=None):
        """convert(total, "USD") into the workspace's own currency, or convert(total, "USD", "EUR"), at the exchange
        rates in Settings › Business details."""
        n = to_number(amount)
        if n is None:
            return None
        source = str(source or self.base_currency).strip().upper()[:3]
        target = str(target or self.base_currency).strip().upper()[:3]
        rates = {**self.rates, **({self.base_currency: 1.0} if self.base_currency else {})}
        for code in (source, target):
            if code not in rates:
                raise NoRate(f"there's no exchange rate for {code or 'that currency'} (add it in Settings › Business details)")
        return round(n * rates[source] / rates[target], 2)

    def display(self, f, value, record=None):
        """A value as text for people: $1,234.50, 3 Nov 2026, 15%, Yes, the linked record's name; with what the field
        shows before and after it ("ABN 51 824 753 556", "12 pcs")."""
        text = self._display(f, value, record)
        if f is not None and (f.get("before") or f.get("after")) and text != "—":
            return f"{f.get('before', '')}{text}{f.get('after', '')}"
        return text

    def _display(self, f, value, record=None):
        if value is None or value == "" or value == []:
            return "—"
        if f is not None and f["type"] == "lookup":
            return self.display(self.source(f), value)
        kind = self.kind(f)
        if f is not None and f["type"] == "link":
            ids = value if isinstance(value, list) else [value]
            names = [self.title(x) for x in (self.get(i, f.get("table")) for i in ids) if x is not None]
            return ", ".join(names) if names else "—"
        if isinstance(value, list) and kind not in ("multi_select", "user") and f is not None and f["type"] == "lookup":
            return ", ".join(self.display(self.source(f), v) for v in value)
        if kind == "user" and isinstance(value, list):
            return ", ".join(self.members().get(int(x), "Former member") for x in value if str(x).isdigit()) or "—"
        if kind == "location":
            if isinstance(value, dict) and "lat" in value:
                return f"{value['lat']:.5f}, {value['lng']:.5f}"
            return str(value.get("place") or "") if isinstance(value, dict) else str(value)
        if kind == "tags":
            return ", ".join(str(x) for x in value) if isinstance(value, list) else str(value)
        if kind == "json":
            import json as json_
            return json_.dumps(value, ensure_ascii=False, default=str)[:300]
        if kind == "time":
            match = re.match(r"(\d{1,2}):(\d{2})", str(value))
            if match:
                h, m = int(match.group(1)), int(match.group(2))
                return f"{h % 12 or 12}:{m:02d} {'am' if h < 12 else 'pm'}"
            return str(value)
        if kind == "address":
            return ", ".join(line.strip() for line in str(value).splitlines() if line.strip())
        if kind == "multi_select":
            return ", ".join(value) if isinstance(value, list) else str(value)
        if kind == "user":
            return self.members().get(int(value), "Former member") if str(value).isdigit() else str(value)
        if kind == "rating":
            n, top = int(to_number(value) or 0), int((f or {}).get("max", 5))
            return "★" * n + "☆" * max(top - n, 0)
        if kind == "duration":
            hours = to_number(value) or 0
            h, m = int(abs(hours)), int(round((abs(hours) - int(abs(hours))) * 60))
            if m == 60:
                h, m = h + 1, 0
            return ("-" if hours < 0 else "") + (f"{h}h {m:02d}m" if h and m else f"{h}h" if h else f"{m}m")
        if kind == "rich_text":
            plain = re.sub(r"[#*_`>\[\]]|\(https?://[^)]*\)", "", str(value)).strip()
            return plain.splitlines()[0][:200] if plain else "—"
        if kind in schema.FILES:
            names = [x["name"] for x in value] if isinstance(value, list) else []
            return names[0] if len(names) == 1 else f"{len(names)} files" if names else "—"
        if kind == "money":
            n = to_number(value)
            return "—" if n is None else f"{'-' if n < 0 else ''}{self.symbol(f, record)}{abs(n):,.2f}"
        if kind == "percent":
            n = to_number(value)
            return "—" if n is None else f"{round(n * 100, 2):g}%"
        if kind == "number":
            n = to_number(value)
            if n is None:
                return str(value)
            places = (f or {}).get("decimals")
            text = f"{n:,.{places}f}" if places is not None else (f"{n:,}" if isinstance(n, int) else f"{round(n, 2):,.2f}".rstrip("0").rstrip("."))
            return f"{text} {f['unit']}" if (f or {}).get("unit") else text
        if kind == "date":
            d = to_date(value)
            return f"{d.day} {d:%b %Y}" if d else str(value)
        if kind == "datetime":
            d = to_datetime(value)
            return f"{d.day} {d:%b %Y}, {d.hour % 12 or 12}:{d:%M} {'am' if d.hour < 12 else 'pm'}" if d else str(value)
        if kind == "checkbox":
            return (f or {}).get("yes_label", "Yes") if to_bool(value) else (f or {}).get("no_label", "No")
        return str(value)

    def colours(self, t, record):
        """A record's colours from its table's colour rules: {"row": a colour or None, "fields": {field: colour}}.
        The first rule that matches wins for each place."""
        out = {"row": None, "fields": {}}
        for rule in (t or {}).get("colors", []):
            if not self.matches(record, rule["when"]):
                continue
            if rule.get("field"):
                out["fields"].setdefault(rule["field"], rule["color"])
            elif out["row"] is None:
                out["row"] = rule["color"]
        return out

    def values(self, record):
        t = self.tables.get(record.table)
        return {f["key"]: self.value(record, f["key"]) for f in (t["fields"] if t else [])}

    # --- conditions, search, sorting -------------------------------------------------------------------------
    def _at_path(self, record, key, table=None):
        """A field and its value on the record, or through its links (an invoice's customer.account_manager, a lease's
        property.owner). `table` is the record's, for when there's no record at the end of a link."""
        table = record.table if record is not None else table
        head, _, rest = key.partition(".")
        if not rest:
            return self.field(table, key), self.value(record, key) if record is not None else None
        link = self.field(table, head)
        if not link or link["type"] != "link":
            return None, None
        linked = self.get((record.data or {}).get(head), link["table"]) if record is not None else None
        return self._at_path(linked, rest, link["table"])

    def _target(self, record, value, f):
        """A condition's value, with {{ }} filled in from the record. "me" on a person field is whoever is looking."""
        if f is not None and f["type"] == "user" and isinstance(value, str) and value.strip().lower() in ("me", "{{me}}", "current_user", "{{user}}"):
            return self.user_id or 0  # the platform itself is nobody's "me"
        value = schema.worked_out(value) or value  # "now() - 48h" without braces, as a design saved before it was read so
        if isinstance(value, str) and "{{" in value:
            try:
                return expr.render(value, RecordContext(self, record))
            except expr.ExprError:
                return None
        if isinstance(value, str) and record is not None and re.search(r"[-+*/]", value) and re.fullmatch(r"[\w\s.+\-*/()]+", value):
            # a sum over the record's own fields written without braces ("budget * 0.9"): worked out, not read as text
            names, t = re.findall(r"[A-Za-z_]\w*", value), self.tables.get(record.table)
            if names and t and all(schema.field(t, n) for n in names):
                try:
                    return expr.evaluate(value, RecordContext(self, record))
                except expr.ExprError:
                    return None
        if isinstance(value, str) and record is not None and self._names_field(record.table, value, f):
            return self.value(record, value.strip())  # another of its own fields, named bare ("on_hand < reorder_level")
        return value

    def _names_field(self, table, value, f):
        """Whether a condition's value is another field of the same record, named without braces: its code name
        (reorder_level), and the field it's compared with isn't one where that could be a word someone types."""
        t = self.tables.get(table)
        other = schema.field(t, str(value).strip()) if t and re.fullmatch(r"[a-z][a-z0-9_]*", str(value).strip()) else None
        return other is not None and (f is None or self.kind(f) not in ("text", "long_text", "select", "multi_select", "email", "phone",
                                                                          "url") or "_" in str(value))

    def _equal(self, f, actual, wanted):
        if f is not None and f["type"] in ("link", "user") and isinstance(actual, list):  # a link to many: any of them
            if not actual:
                return wanted in (None, "")
            return any(self._equal({**f, "multiple": False}, one, wanted) for one in actual)
        if f is not None and f["type"] == "multi_select":
            chosen = [str(x).lower() for x in (actual or [])]
            return not chosen if wanted in (None, "") else str(wanted).strip().lower() in chosen
        if f is not None and f["type"] == "user":
            if actual in (None, ""):
                return wanted in (None, "")
            self.members()
            who = int(actual)
            if isinstance(wanted, int) or str(wanted).isdigit():
                return int(wanted) == who
            return str(wanted).strip().lower() in (self._members.get(who, "").lower(), str(who), self._emails.get(who, "").lower())
        if f is not None and f["type"] == "link":
            if actual in (None, ""):
                return wanted in (None, "")
            if isinstance(wanted, RecordProxy):
                return wanted.record.id == int(actual)
            linked = self.get(actual, f.get("table"))
            return str(wanted).strip() == str(actual) or (linked is not None and self.title(linked).lower() == str(wanted).strip().lower())
        typed = self._typed(f, actual) if f else actual
        if isinstance(typed, datetime) and isinstance(wanted, date) and not isinstance(wanted, datetime):
            typed = typed.date()  # an event at 6 pm is on the day asked about
        elif isinstance(typed, datetime) and isinstance(wanted, str) and len(wanted.strip()) == 10:
            typed, wanted = typed.date(), to_date(wanted)
        return expr._compare(expr.ast.Eq(), typed, wanted)

    def matches(self, record, conditions, old=None, about=None, names=None):
        """Whether a record meets every condition. `old` (the values before a change) makes changed/changed_to work;
        `about` is the record {{record.x}} in the values means, when that isn't the one checked (a statement's lines).
        A group {"any": [...]} holds when one of its conditions does; {"all": [...]} when every one does."""
        for c in conditions or []:
            if "any" in c or "all" in c:
                if not (any(self.matches(record, [x], old, about, names) for x in c["any"]) if "any" in c else self.matches(record, c["all"], old, about, names)):
                    return False
                continue
            if "formula" in c:  # count(client.invoices where …) == 0: a formula that must hold
                # about another record (an AI step's reads): "record" is that one, as {{record.x}} in a value is; the one
                # checked is "this" and its own fields (the rule's buyer, not each contract's own, read as itself)
                named = {**(names or {}), "record": RecordProxy(self, about)} if about is not None and about is not record else names
                try:
                    if not to_bool(expr.evaluate(schema.percents_fixed(self.tables.get(record.table), c["formula"], self.tables), RecordContext(self, record, named))):
                        return False
                except expr.ExprError as exc:
                    log.warning("The condition %r couldn't be worked out: %s", c["formula"], exc)
                    return False
                continue
            f, actual = self._at_path(record, c["field"])
            wanted = self._target(about or record, c.get("value"), f)
            op = c["op"]
            if f is not None and self.kind(f) == "percent" and op in ("equals", "not_equals", "greater_than", "less_than", "at_least", "at_most"):
                wanted = schema.as_fraction(wanted)  # "more than 10" on a percentage means 10%, kept as 0.1
            if actual in (None, "") and f is not None and self.kind(f) in ("number", "money", "percent") and to_number(f.get("default")) is not None \
                    and op in ("equals", "not_equals", "greater_than", "less_than", "at_least", "at_most"):
                actual = f["default"]  # left empty, a number counts as its default: no discount is a 0% discount
            typed = self._typed(f, actual) if f else \
                (actual.replace(tzinfo=ZoneInfo("UTC")).astimezone(self.now_tz).date() if isinstance(actual, datetime) else actual)  # created_at: stored in UTC
            if op == "equals":
                ok = self._equal(f, actual, wanted)
            elif op == "not_equals":
                ok = not self._equal(f, actual, wanted)
            elif op == "contains":
                shown = self.display(f, actual) if f else str(actual or "")
                ok = str(wanted or "").lower() in shown.lower()
            elif op == "is_empty":
                ok = actual in (None, "", []) or (f is not None and f["type"] == "checkbox" and not to_bool(actual))
            elif op == "not_empty":
                ok = actual not in (None, "", []) and not (f is not None and f["type"] == "checkbox" and not to_bool(actual))
            elif op in ("greater_than", "less_than", "at_least", "at_most"):
                node = {"greater_than": expr.ast.Gt(), "less_than": expr.ast.Lt(), "at_least": expr.ast.GtE(), "at_most": expr.ast.LtE()}[op]
                if isinstance(typed, datetime):
                    wanted = to_datetime(wanted)
                elif isinstance(typed, date):
                    wanted = to_date(wanted)
                ok = typed is not None and expr._compare(node, typed, wanted)
            elif op == "one_of":
                ok = any(self._equal(f, actual, w) for w in (wanted if isinstance(wanted, list) else [wanted]))
            elif op in ("before_today", "after_today", "within_next_days", "within_last_days", "more_than_days_ago", *schema.PERIOD_OPS):
                d = to_date(typed)
                if d is None:
                    ok = False
                elif op == "before_today":
                    ok = d < self.today
                elif op == "after_today":
                    ok = d > self.today
                elif op == "within_next_days":
                    ok = 0 <= (d - self.today).days <= (to_number(wanted) or 0)
                elif op == "within_last_days":
                    ok = 0 <= (self.today - d).days <= (to_number(wanted) or 0)
                elif op in schema.PERIOD_OPS:
                    first, last = schema.period(op, self.today, self.fy_start)
                    ok = first <= d <= last
                else:
                    ok = (self.today - d).days > (to_number(wanted) or 0)
            elif op in ("changed", "changed_to"):
                before = (old or {}).get(c["field"]) if old is not None else None
                ok = (old is None and actual not in (None, "")) or (old is not None and before != actual)
                said = c.get("value").strip().lower() if isinstance(c.get("value"), str) else None
                if ok and op == "changed_to" and said in (FILLED | EMPTIED) and not (f and said in {str(o).lower() for o in f.get("options") or []}):
                    # "changed_to": "not_empty" (or "filled in") is a change to anything; a number's 0 isn't filled in
                    ok = (actual in (None, "", [])) if said in EMPTIED else actual not in (None, "", []) and not (
                        f is not None and self.kind(f) in ("number", "money", "percent") and to_number(actual) == 0)
                elif ok and op == "changed_to":
                    ok = self._equal(f, actual, wanted)
            else:
                ok = False
            if not ok:
                return False
        return True

    def search(self, rows, q):
        q = (q or "").strip().lower()
        if not q:
            return rows
        t = self.tables.get(rows[0].table) if rows else None
        keys = [f for f in (t["fields"] if t else []) if self.sees(f) and f["type"] in ("text", "long_text", "rich_text", "email", "phone", "select", "address", "barcode",
                                                                      "multi_select", "user", "autonumber", "link", "url", "lookup")]
        return [r for r in rows if any(q in self.display(f, self.value(r, f["key"])).lower() for f in keys)]

    def sort(self, rows, sort):
        if not sort:
            return rows
        f = self.field(rows[0].table, sort["field"]) if rows else None

        def key(r):
            v = self.value(r, sort["field"])
            if f is not None and f["type"] == "link":
                v = self.title(self.get(v[0] if isinstance(v, list) else v)) if v else None
            elif f is not None and self.kind(f) in ("number", "money", "percent"):
                v = to_number(v)
            elif f is not None and self.kind(f) in schema.DATES:
                v = to_datetime(v)
            elif isinstance(v, str):
                v = v.lower()
            return (v is None, v if v is not None else 0)
        try:
            return sorted(rows, key=key, reverse=sort.get("dir") == "desc")
        except TypeError:
            return rows

    def query(self, table, filters=None, q=None, sort=None, everyone=False):
        """Records of a table (those this person may see; with everyone, all of them) meeting the filters, matching the
        search, sorted. When SQL can leave records out (engine/planner.py), only the rest are read, and only those it
        can't settle are checked here."""
        plan = self._sql_plan(table, filters, q, sort, everyone=everyone)
        if plan is not None and plan.narrows:
            return plan.rows()
        rows = self.rows(table) if everyone else self.visible_rows(table)
        rows = [r for r in rows if self.matches(r, filters)] if filters else list(rows)
        return self.sort(self.search(rows, q), sort)

    def group(self, rows, key, aggregate="count", of=None):
        """[(label, value)] for a chart: rows grouped by a field, in the field's natural order."""
        if not rows:
            return []
        f = self.field(rows[0].table, key)
        dated = key in ("created_at", "updated_at") or (f is not None and self.kind(f) in schema.DATES)
        groups = {}
        for r in rows:
            v = self.value(r, key)
            if dated and v:
                d = to_date(v)
                label = f"{d:%Y-%m}" if d else "—"
            else:
                label = self.display(f, v) if f else str(v)
            groups.setdefault(label, []).append(r)
        out = [(label, self.aggregate(members, aggregate, of)) for label, members in groups.items()]
        if f is not None and f["type"] == "select":
            order = {o: i for i, o in enumerate(f["options"])}
            out.sort(key=lambda x: order.get(x[0], len(order)))
        elif dated:
            out.sort(key=lambda x: x[0])
            out = [(f"{datetime.strptime(l, '%Y-%m'):%b %Y}" if l != "—" else l, v) for l, v in out]
        else:
            out.sort(key=lambda x: -(to_number(x[1]) or 0))
        return out

    # --- writing ------------------------------------------------------------------------------------------------
    def coerce(self, f, raw):
        """A value as it will be stored, or ValueError with a sentence."""
        if isinstance(raw, RecordProxy):
            raw = raw.record.id if f["type"] == "link" else str(raw)
        if raw is None or (isinstance(raw, str) and raw.strip() == "") or raw == []:
            return None
        kind = f["type"]
        if kind in ("text", "phone"):
            return str(raw).strip()[:1000]
        if kind == "barcode":
            return re.sub(r"\s+", "", str(raw))[:200]
        if kind == "time":
            text = str(raw).strip().lower().replace(".", ":")
            match = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text)
            if not match:
                d = to_datetime(raw)
                if d is None:
                    raise ValueError(f"“{raw}” isn't a time (try 14:30 or 2:30 pm)")
                return f"{d:%H:%M}"
            h, m = int(match.group(1)), int(match.group(2) or 0)
            if match.group(3) == "pm" and h < 12:
                h += 12
            if match.group(3) == "am" and h == 12:
                h = 0
            if h > 23 or m > 59:
                raise ValueError(f"“{raw}” isn't a time of day")
            return f"{h:02d}:{m:02d}"
        if kind == "color":
            text = str(raw).strip().lower()
            named = {"red": "#dc2626", "orange": "#ea580c", "amber": "#d97706", "yellow": "#eab308", "green": "#16a34a", "teal": "#0d9488",
                     "blue": "#2563eb", "indigo": "#4f46e5", "violet": "#7c3aed", "purple": "#9333ea", "pink": "#db2777",
                     "gray": "#6b7280", "grey": "#6b7280", "black": "#111111", "white": "#ffffff", "brown": "#92400e"}
            if text in named:
                return named[text]
            if re.fullmatch(r"#?[0-9a-f]{3}", text):
                return "#" + "".join(c * 2 for c in text.lstrip("#"))
            if re.fullmatch(r"#?[0-9a-f]{6}", text):
                return "#" + text.lstrip("#")
            raise ValueError(f"“{raw}” isn't a colour (try #1e90ff or a name like blue)")
        if kind == "tags":
            items = raw if isinstance(raw, list) else re.split(r"\s*[,;\n]\s*", str(raw))
            out = []
            for item in items:
                tag = str(item).strip()[:60]
                if tag and tag.lower() not in [x.lower() for x in out]:
                    out.append(tag)
            return out[:50] or None
        if kind == "json":
            import json as json_
            if isinstance(raw, (dict, list)):
                value = raw
            else:
                try:
                    value = json_.loads(str(raw))
                except ValueError as exc:
                    raise ValueError(f"that isn't valid JSON ({exc})") from exc
            if len(json_.dumps(value, default=str)) > 100_000:
                raise ValueError("that's too much data for one field")
            return value
        if kind == "address":
            return "\n".join(line.strip() for line in str(raw).replace("\r", "").split("\n") if line.strip())[:1000]
        if kind == "location":  # a position (the device's "Where I am"), or a place in words: “Sydney, NSW” is kept as written
            if isinstance(raw, dict) and raw.get("place") and raw.get("lat") is None:
                raw = raw["place"]
            if isinstance(raw, dict):
                lat, lng = to_number(raw.get("lat", raw.get("latitude"))), to_number(raw.get("lng", raw.get("lon", raw.get("longitude"))))
            elif PLACE_WORDS.sub("", str(raw)):
                return {"place": " ".join(str(raw).split())[:300]}  # on a map only once it has a position
            else:
                numbers = re.findall(r"-?\d+(?:\.\d+)?", str(raw))
                lat, lng = (to_number(numbers[0]), to_number(numbers[1])) if len(numbers) >= 2 else (None, None)
            if lat is None or lng is None or not -90 <= lat <= 90 or not -180 <= lng <= 180:
                raise ValueError(f"“{raw}” isn't a place on the map (write the address, or press Where I am)")
            return {"lat": round(float(lat), 6), "lng": round(float(lng), 6)}
        if kind == "long_text":
            return str(raw)[:20000]
        if kind == "rich_text":
            return str(raw)[:50000]
        if kind == "multi_select":
            chosen = raw if isinstance(raw, list) else re.split(r"\s*[,;]\s*", str(raw))
            out = []
            for item in chosen:
                match = next((o for o in f["options"] if o.lower() == str(item).strip().lower()), None)
                if match is None and str(item).strip():
                    raise ValueError(f"“{item}” isn't one of: {', '.join(f['options'])}")
                if match and match not in out:
                    out.append(match)
            return out or None
        if kind in ("user", "link") and f.get("multiple"):
            items = raw if isinstance(raw, list) else [x for x in re.split(r"\s*[;\n]\s*|\s*,\s*(?=\S)", str(raw)) if x]
            if isinstance(raw, str) and kind == "link":  # names may hold commas: try the whole text first
                whole = self._find_linked(f["table"], raw)
                if whole is not None:
                    return [whole.id]
            out = []
            for item in items:
                one = self.coerce({**f, "multiple": False}, item)
                if one is not None and one not in out:
                    out.append(one)
            return out or None
        if kind == "user":
            members = self.members()
            if str(raw).strip().isdigit() and int(raw) in members:
                return int(raw)
            text = str(raw).strip().lower()
            found = [uid for uid, name in members.items() if name.lower() == text or self._emails.get(uid, "").lower() == text]
            if len(found) != 1:
                raise ValueError(f"“{raw}” isn't a member of this workspace")
            return found[0]
        if kind == "rating":
            n = to_number(raw)
            if n is None or not 0 <= n <= f.get("max", 5):
                raise ValueError(f"a rating is a number from 0 to {f.get('max', 5)}")
            return int(round(n))
        if kind == "duration":
            hours = parse_duration(raw)
            if hours is None:
                raise ValueError(f"“{raw}” isn't a duration (try 1:30, 1.5h or 90m)")
            return hours
        if kind in ("number", "money", "percent"):
            n = to_number(raw)
            if n is None:
                raise ValueError(f"“{raw}” isn't a number")
            if kind == "money":
                return round(float(n), 2)
            if kind == "percent":
                if isinstance(raw, str) and raw.strip().endswith("%"):
                    return n
                return n / 100 if abs(n) > 1 else n
            return n
        if kind == "date":
            if isinstance(raw, str) and raw.strip().lower() in ("today", "now", "today()", "now()"):  # as a rule may say it
                return self.today.isoformat()
            d = to_date(raw)
            if d is None:
                raise ValueError(f"“{raw}” isn't a date (try 2026-11-30 or 30/11/2026)")
            return d.isoformat()
        if kind == "datetime":
            if isinstance(raw, str) and raw.strip().lower() in ("now", "now()", "today()"):
                return self.now.isoformat(timespec="minutes")
            d = to_datetime(raw)
            if d is None:
                raise ValueError(f"“{raw}” isn't a date and time (try 2026-11-30 14:30)")
            return d.isoformat(timespec="minutes")
        if kind == "checkbox":
            return to_bool(raw)
        if kind == "select":
            for option in f["options"]:
                if option.lower() == str(raw).strip().lower():
                    return option
            raise ValueError(f"“{raw}” isn't one of: {', '.join(f['options'])}")
        if kind == "email":
            text = str(raw).strip()
            if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", text):
                raise ValueError(f"“{text}” isn't an email address")
            return text
        if kind == "url":
            text = str(raw).strip()
            return text if re.match(r"^[a-z][a-z0-9+.-]*://", text, re.I) else f"https://{text}"
        if kind == "link":
            if isinstance(raw, list):
                raw = raw[0] if raw else None
                if raw is None:
                    return None
            linked = self._find_linked(f["table"], raw)
            if linked is None:
                t = self.tables.get(f["table"])
                if isinstance(raw, dict):
                    raise ValueError(f"there's no {t['singular'] if t else 'record'} with {self._described(t, raw)}")
                raise ValueError(f"there's no {t['singular'] if t else 'record'} called “{raw}”")
            return linked.id
        return None  # autonumber, formula, rollup: worked out, never typed in

    def _find_linked(self, table, raw):
        if isinstance(raw, dict):
            return self._matching(table, raw)
        if isinstance(raw, (int, float)) or (isinstance(raw, str) and raw.strip().isdigit()):
            found = self.get(int(raw), table)
            if found:
                return found
        text = str(raw).strip().lower()  # by name: found by SQL when the name is a field (an import's thousandth row too)
        exact = sorted(self._titled_ids(table, text))
        if len(exact) == 1:
            return self.get(exact[0], table)
        partial = sorted(self._titled_ids(table, text, part=True)) if text else []
        return self.get(partial[0], table) if len(partial) == 1 else (self.get(exact[0], table) if exact else None)

    def _matching(self, table, wanted):
        """The record of a table holding the values given, for a rule that names a record by what it's for rather than by
        its name ({"product": 12, "warehouse": 3}: this product's stock at this warehouse). One that's only the pairing of
        others (every value a link) is added when it isn't there yet, as a person would; one named by words isn't (a
        misspelt name would make another)."""
        t = self.tables.get(table)
        if t is None or not wanted:
            return None
        want = {}
        for k, v in wanted.items():
            f = self.field(table, k)
            if f is None or v in (None, "", []):
                return None
            if f["type"] == "link":
                linked = self._find_linked(f["table"], v[0] if isinstance(v, list) and v else v)
                if linked is None:
                    return None
                v = linked.id
            want[k] = (f, v)

        def holds(r, f, v):
            have = (r.data or {}).get(f["key"]) if f["type"] == "link" else self.value(r, f["key"])
            if isinstance(have, list):
                return str(v) in [str(x) for x in have]
            a, b = to_number(have), to_number(v)
            return a == b if a is not None and b is not None else str(have or "").strip().lower() == str(v).strip().lower()

        found = next((r for r in self.rows(table) if all(holds(r, f, v) for f, v in want.values())), None)
        if found is None and all(f["type"] == "link" for f, _ in want.values()):
            try:
                found = self.create(table, {k: v for k, (f, v) in want.items()}, ai=False)
            except FieldErrors:
                return None
        return found

    def _described(self, t, wanted):
        """{"product": 12, "warehouse": 3} in words: Product “Olive oil” and Warehouse “Welshpool”."""
        said = []
        for k, v in wanted.items():
            f = schema.field(t, k) if t else None
            if f is not None and f["type"] == "link" and str(v).isdigit() and self.get(int(v)):
                v = self.title(self.get(int(v)))
            said.append(f"{f['label'] if f else k} “{v}”")
        return " and ".join(said)

    def _clean(self, t, data, creating, record=None, unfinished=False):
        """unfinished: a record a rule adds, whose must-haves it couldn't know (which supplier), is added all the same;
        those left empty are kept in self.left_unfilled (their labels), for people to fill in."""
        values, errors, read_later = {}, {}, []
        self.left_unfilled = []
        data = {k: v for k, v in data.items() if not str(k).startswith("_")}
        for f in t["fields"]:
            if f["type"] not in schema.STORED or f["type"] == "autonumber":
                continue
            if f["key"] in data and f.get("sensitive"):  # kept encrypted; left blank (or as shown) keeps what's there
                said = data[f["key"]]
                old = (record.data or {}).get(f["key"]) if record is not None else None
                if isinstance(said, dict) and said.get("secret"):
                    values[f["key"]] = said  # already sealed (a change being approved, a copy)
                    continue
                if said in (None, "") or str(said).strip() == masked(old):
                    continue
                try:
                    plain = self.coerce(f, said)
                except ValueError as exc:
                    errors[f["key"]] = str(exc)
                    continue
                if plain is not None and f.get("pattern") and not re.fullmatch(f["pattern"], str(plain)):
                    errors[f["key"]] = f["pattern_message"]  # checked on what was typed, before it's sealed
                    continue
                values[f["key"]] = old if plain is not None and reveal(old) == str(plain) else (seal_value(plain) if plain is not None else None)
                continue
            if f["key"] in data:
                try:
                    values[f["key"]] = self.coerce(f, data[f["key"]])
                except ValueError as exc:
                    errors[f["key"]] = str(exc)
            elif creating and f.get("default") not in (None, ""):
                if isinstance(f["default"], str) and "{{" in f["default"]:  # read once what it's given is known: an
                    read_later.append(f)  # award's buyer, {{tender.buyer}}
                    continue
                try:
                    values[f["key"]] = self.coerce(f, f["default"])
                except ValueError:
                    pass
        for f in read_later:
            draft = Record(workspace_id=self.ws.id, table=t["key"], data=dict(values))
            try:
                values[f["key"]] = self.coerce(f, expr.render(f["default"], RecordContext(self, draft)))
            except (ValueError, expr.ExprError):
                pass
        flow = t.get("workflow")
        if creating and flow and values.get(flow["field"]) in (None, "") and schema.start_stage(t):
            values[flow["field"]] = schema.start_stage(t)
        parent = next((f for f in t["fields"] if f["type"] == "link" and not (self.tables.get(f["table"]) or {}).get("embedded")), None) if t.get("embedded") else None
        for f in t["fields"] if t.get("embedded") else []:  # an item picked on a line brings its name and price (a menu item onto a quote line)
            if f["type"] == "link" and f is not parent and not f.get("multiple") and values.get(f["key"]) not in (None, "", []) and f["table"] in self.tables \
                    and (record is None or (record.data or {}).get(f["key"]) != values[f["key"]]):
                picked = self.get(values[f["key"]], f["table"])
                for ours, theirs in schema.fill_from(t, f, self.tables[f["table"]]).items() if picked is not None else []:
                    now = values.get(ours, (record.data or {}).get(ours) if record is not None else None)
                    if now in (None, "", []) and (picked.data or {}).get(theirs) not in (None, "", []):
                        values[ours] = (picked.data or {}).get(theirs)
        merged = {**((record.data or {}) if record is not None else {}), **values}
        draft = Record(workspace_id=self.ws.id, table=t["key"], data=merged)
        draft.id = record.id if record is not None else None
        for f in t["fields"]:
            key = f["key"]
            if key in errors or f["type"] not in schema.STORED or f["type"] == "autonumber":
                continue
            value = merged.get(key)
            shown = self.shown(t, f, draft)
            needed = f.get("required") or (f.get("required_if") and self.matches(draft, f["required_if"]))
            if needed and shown and value in (None, "", []) and (creating or key in data or f.get("required_if")):
                if unfinished and creating and data.get(key) in (None, "", []) and f.get("required"):
                    # for people to fill in: the rule couldn't know it, or what it copies from was left empty
                    self.left_unfilled.append(f["label"])
                    continue
                errors[key] = f"{f['label']} is required" + (" here" if f.get("required_if") and not f.get("required") else "")
            if f.get("readonly") and key in values and self.role is not None and not self._at_least("admin") \
                    and values[key] != ((record.data or {}).get(key) if record is not None else None) and values[key] not in (None, "", []):
                errors[key] = f"{f['label']} is filled in automatically"
            if f.get("readonly_if") and not creating and key in values and self.role is not None and not self._at_least("admin") \
                    and values[key] != (record.data or {}).get(key) and self.matches(draft, f["readonly_if"]):
                errors[key] = f"{f['label']} can't be changed now ({automations_words(self, f['readonly_if'], t['key'])})"
            if f.get("options_by") and key in values and value not in (None, "", []):
                allowed = self.choices(t, f, draft)
                wrong = [v for v in (value if isinstance(value, list) else [value]) if v not in allowed]
                if wrong:
                    parent = self.field(t["key"], f["options_by"]["field"])
                    errors[key] = f"{wrong[0]} isn't a choice for {parent['label'] if parent else 'this'} " \
                                  f"{self.display(parent, merged.get(parent['key'])) if parent else ''}".rstrip()
            if f.get("pattern") and not f.get("sensitive") and key in values and value not in (None, "") and not re.fullmatch(f["pattern"], str(value)):
                errors[key] = f["pattern_message"]
            if f["type"] == "link" and f.get("match") and key in values and value not in (None, "", []):
                for theirs, ours in f["match"].items():  # a contact must belong to this record's customer
                    want = merged.get(ours)
                    for linked in self.linked(draft, f):
                        if want not in (None, "", []) and str((linked.data or {}).get(theirs)) != str(want):
                            errors[key] = f"{self.title(linked)} doesn't belong to the chosen {self.field(t['key'], ours)['label'].lower()}" \
                                if self.field(t["key"], ours) else f"{self.title(linked)} doesn't match"
            if value in (None, "", []) or key not in values:
                continue
            number = to_number(value) if f["type"] in schema.NUMERIC | {"duration", "rating"} else None
            if number is not None and "min" in f and number < f["min"]:
                errors[key] = f"{f['label']} must be at least {self.display(f, f['min'])}"
            if number is not None and "max" in f and number > f["max"] and f["type"] != "rating":
                errors[key] = f"{f['label']} must be at most {self.display(f, f['max'])}"
            if f.get("unique"):
                clash = next((r for r in self._others(t, record, {key: value}) if str((r.data or {}).get(key, "")).strip().lower()
                              == str(value).strip().lower()), None)
                if clash is not None:
                    errors[key] = f"{self.title(clash)} already has this {f['label']}"
        for group in t.get("unique_together", []):  # a supplier's invoice number, once
            if not set(group) & set(values) or any(merged.get(k) in (None, "", []) for k in group):
                continue
            same = lambda r: all(str((r.data or {}).get(k, "")).strip().lower() == str(merged.get(k)).strip().lower() for k in group)  # noqa: E731
            clash = next((r for r in self._others(t, record, {k: merged.get(k) for k in group}) if same(r)), None)
            if clash is not None:
                labels = [self.field(t["key"], k)["label"] for k in group if self.field(t["key"], k)]
                errors[group[-1]] = f"{t['singular']} “{self.title(clash)}” already has this {' and '.join(labels).lower()}"
        for limit in t.get("limits", []):  # five jobs a day; a room booked once at a time
            touched = set(limit["per"]) | set((limit.get("overlap") or {}).values()) | {c["field"].split(".")[0] for c in schema.condition_leaves(limit.get("where"))}
            if (creating or touched & set(values)) and "_" not in errors:
                over = self._over_limit(t, record, draft, merged, limit)
                if over:
                    errors["_"] = over
        if flow and flow["field"] in values and flow["field"] not in errors and (self.role is not None or self.portal_contact is not None):
            self._check_move(t, record, draft, values[flow["field"]], creating, errors)
        if errors:
            raise FieldErrors(errors)
        return values

    def _others(self, t, record, values, where=None):
        """The other records of t (not `record`) that may hold these values read as text (str(value).strip().lower()):
        what a unique field, fields unique together and a limit compare. Found by SQL, never fewer than hold them; the
        caller checks each one as it always did. With `where` (a limit's conditions), those SQL can rule out are too.
        A table SQL can't read (a sealed one) is read whole."""
        from .planner import Planner, _sql, live
        p = Planner(self, t["key"])
        if t["key"] in self._rows or not p.usable():
            return [r for r in self.rows(t["key"]) if record is None or r.id != record.id]
        tests = [p.same(k, v) for k, v in values.items() if re.fullmatch(r"[A-Za-z_]\w*", k)]
        tests += [got[1] for got in (p.condition(c) for c in where or []) if got is not None]
        found = db.session.query(Record).filter(*live(self, t["key"]), *[_sql(x) for x in tests if x is not True])
        if record is not None and record.id is not None:
            found = found.filter(Record.id != record.id)
        rows = found.order_by(Record.id).all()
        for r in rows:
            self._by_id[r.id] = r
        return rows

    def _over_limit(self, t, record, draft, merged, limit):
        """What's wrong when saving this record would go past one of its table's limits, or None."""
        if limit.get("where") and not self.matches(draft, limit["where"]):
            return None  # a cancelled booking takes no place
        if any(merged.get(k) in (None, "", []) for k in limit["per"]):
            return None
        same = lambda a, b: str(a if a is not None else "").strip().lower() == str(b if b is not None else "").strip().lower()  # noqa: E731
        others = [r for r in self._others(t, record, {k: merged.get(k) for k in limit["per"]}, limit.get("where"))
                  if all(same((r.data or {}).get(k), merged.get(k)) for k in limit["per"])
                  and (not limit.get("where") or self.matches(r, limit["where"]))]
        labels = " and ".join(self.field(t["key"], k)["label"].lower() for k in limit["per"] if self.field(t["key"], k))
        if limit.get("overlap"):
            ov = limit["overlap"]

            def moment(value):  # a date, a date and time, or a time of day ("09:00")
                at = expr.to_datetime(value)
                clock = expr._clock(value) if at is None else None
                return datetime(2000, 1, 1) + timedelta(minutes=clock) if clock is not None else at

            def span(data):  # from its start to its end, or to its start and how long it takes (a 1h 30m appointment)
                start = moment(data.get(ov["start"]))
                if start is None:
                    return None
                end = moment(data.get(ov.get("end") or "")) if ov.get("end") else \
                    start + timedelta(hours=to_number(data.get(ov["length"])) or 0) if ov.get("length") else None
                return start, max(end or start, start)
            mine = span(merged)
            if mine is None:
                return None
            clash = next((r for r in others if span(r.data or {}) is not None and _overlap(mine, span(r.data or {}))), None)
            if clash is None:
                return None
            return limit.get("message") or f"{t['singular']} “{self.title(clash)}” already has {('this ' + labels) if labels else 'that time'} then"
        most = limit["max"] if not isinstance(limit["max"], str) else to_number(self._at_path(draft, limit["max"])[1])
        if most is None or len(others) < most:
            return None
        return limit.get("message") or (f"There can't be more than {most:g} {schema.lc(t['label'])}"
                                         + (f" for the same {labels}" if labels else "") + f": there are {len(others)} already")

    # --- conditional fields and stage workflows --------------------------------------------------------------------
    def shown(self, t, f, record):
        """Whether a field shows on a record: its show_if holds, its hide_if doesn't, and its section's show_if holds."""
        if f.get("show_if") and not self.matches(record, f["show_if"]):
            return False
        if f.get("hide_if") and self.matches(record, f["hide_if"]):
            return False
        section = next((x for x in t.get("sections") or [] if f["key"] in x["fields"]), None)
        return not (section and section.get("show_if")) or self.matches(record, section["show_if"])

    def choices(self, t, f, record):
        """The options a choice field offers on a record: those given for the value of the field they depend on (a
        Plumbing job's types), or all of them."""
        by = f.get("options_by")
        parent = self.field(t["key"], by["field"]) if by else None
        said = self.display(parent, (record.data or {}).get(parent["key"])) if parent and record is not None else ""
        if not said:
            return list(f.get("options") or [])
        found = {str(k).strip().lower(): v for k, v in by["options"].items()}.get(str(said).strip().lower())
        return list(found) if found else list(f.get("options") or [])

    def may_move(self, record, move):
        """Whether this person may make a workflow move on a record: its "who" (a role, a team, the record's creator, its
        outside person in the portal, or people named on it), or anyone who can change the record. The owner (and the
        platform) may make any move."""
        if self.portal_contact is not None:  # the portal: only the moves for its outside person (Acknowledge, Accept)
            return "portal" in (move.get("who") or []) and self.portal_reaches(record, self.portal_contact)
        if self.role is None or self.role == "owner":
            return True
        t = self.tables[record.table] if record is not None else None
        if t is None or not self.can(t, "edit"):
            return False
        return any(self.is_who(who, record) for who in move.get("who") or []) or not move.get("who")

    def _who_words(self, who):
        """“the owner”, “someone in Finance or its manager”: who a move is for, in plain words."""
        teams = {tm["key"]: tm.get("name") or tm["key"] for tm in schema.teams(self.bp)}
        words = []
        for w in who or []:
            if w in ROLES:
                words.append({"owner": "the owner", "admin": "someone who runs the app", "editor": "someone on the team"}.get(w, "someone"))
            elif w in teams:
                words.append(f"someone in {teams[w]}")
            elif w == "creator":
                words.append("whoever added it")
            else:
                words.append(f"its {str(w).replace('_', ' ').replace('.', '’s ')}")
        return " or ".join(dict.fromkeys(words)) or "someone else"

    def is_who(self, who, record):
        """Whether this person is `who` for a record: has the role, is in the team, added the record, or is named on it
        (in a person field, or one through its links: the cost centre's manager)."""
        if self.role is None:
            return True
        if who == "portal":  # the record's outside person (Acknowledge, Accept), in the portal: never the business's own people
            return False
        if who in ROLES:
            return self._at_least(who)
        if who in {tm["key"] for tm in schema.teams(self.bp)}:
            return who in self.teams
        if who == "creator":
            return record is not None and record.id is not None and getattr(record, "created_by_id", None) == self.user_id
        return self.user_id is not None and str(self.user_id) in [str(x) for x in self.people_at(record, who)]

    def people_at(self, record, who):
        """The people (user ids) a person field, a lookup of one, or a path through links ending at one names on a record."""
        if record is None:
            return []
        value = self._at_path(record, who)[1] if "." in who else self.value(record, who)
        values = value if isinstance(value, list) else [value]
        return [int(v) for v in values if str(v).isdigit()]

    def moved_by(self, record, stage):
        """Who (user ids) moved a record to a stage, from its history."""
        flow = (self.tables.get(record.table) or {}).get("workflow")
        if not flow or record.id is None:
            return set()
        rows = RecordChange.query.filter_by(record_id=record.id).filter(RecordChange.kind.in_(("created", "updated"))).all()
        return {r.by_id for r in rows if r.by_id and str(((r.changes or {}).get(flow["field"]) or [None, None])[-1]).lower() == str(stage).lower()}

    def kept_from(self, record, apart):
        """Why this person may not do something kept apart from them (separation of duties), or None: they raised it, they
        moved it to an earlier stage (recommended the award), they're named on it, or they're in the team it's kept from."""
        if self.role is None or not apart:
            return None
        t = self.tables.get(record.table) or {}
        saved = (self.get(record.id) or record) if record.id is not None else record  # a draft of a save: its saved self
        for who in apart:
            if who == "creator":
                if saved.created_by_id is not None and saved.created_by_id == self.user_id:
                    return f"you raised this {t.get('singular', 'record').lower()}"
            elif str(who).lower() in [x.lower() for x in schema.stages(t)] if t.get("workflow") else False:
                if self.user_id in self.moved_by(record, who):
                    return f"you moved it to {next(x for x in schema.stages(t) if x.lower() == str(who).lower())}"
            elif who in {tm["key"] for tm in schema.teams(self.bp)}:
                if who in self.teams:
                    return f"you're in {next(tm['name'] for tm in schema.teams(self.bp) if tm['key'] == who)}"
            elif self.user_id is not None and self.user_id in self.people_at(record, who):
                f = schema._path_field(t, who, self.tables) if "." in who else self.field(record.table, who)
                return f"you're its {(f or {'label': who})['label'].lower()}"
        return None

    def waiting_for(self, record):
        """The approvals a rule asked for on this record that nobody has answered yet."""
        if record is None or record.id is None:
            return []
        from ..models import Approval
        return [a for a in Approval.query.filter_by(workspace_id=self.ws.id, record_id=record.id, status="pending").order_by(Approval.id)
                if (a.kind or "approval") == "approval"]

    def may_answer(self, approval, record):
        from .automations import may_decide
        return self.role is not None and may_decide(approval, self.member, self.user_id, record, self)

    def _waiting_words(self, waiting, record):
        """“It's waiting for Sarah Kim to approve it”, and why it can't be you, for a move made while a record waits."""
        names = []
        for a in waiting:
            if a.assignee_id:
                names.append(self.members().get(a.assignee_id, "someone"))
            elif (a.role or "").startswith("team:"):
                names.append(next((tm["name"] for tm in schema.teams(self.bp) if tm["key"] == a.role[5:]), "someone"))
            else:
                names.append({"owner": "the owner", "admin": "someone who runs the app", "editor": "someone on the team"}.get(a.role or "admin", "someone"))
        from .automations import kept_from
        apart = next((kept_from(self, a, record) for a in waiting if a.assignee_id == self.user_id), None)
        if apart:
            return f"Someone else has to approve it, because {apart}"
        names = list(dict.fromkeys(names))
        who = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]
        mine = record is not None and getattr(record, "created_by_id", None) == self.user_id and self.user_id is not None
        return f"It's waiting for {who} to approve it" + (" (you asked for it, so someone else has to)" if mine else "")

    def to_fill(self, t, record, move):
        """The fields a move needs filled in before it's made (its button asks for them): those it requires, and those its
        stage makes required (a Pickup date once it's Picked up), so the button asks rather than the save refusing: [field]."""
        keys = list(move.get("requires", []))
        stage = (t.get("workflow") or {}).get("field")
        if stage and any(f.get("required_if") for f in t["fields"]):
            after = Record(workspace_id=self.ws.id, table=t["key"], data={**(record.data or {}), stage: move["to"]})
            after.id = record.id
            keys += [f["key"] for f in t["fields"] if f.get("required_if") and f["key"] not in keys and f["type"] in schema.STORED
                     and f["key"] != stage and self.matches(after, f["required_if"]) and self.shown(t, f, after)]
        return [f for f in (self.field(t["key"], k) for k in keys)  # a file field's files aren't in the data
                if f and self.value(record, f["key"]) in (None, "", [])]

    def move_problem(self, t, record, move, filled=False):
        """What stops a move now (fields to fill in first, a condition not met, a separation of duties, an approval it's
        waiting for that this person can't give), or None. With filled, what would stop it once the fields it needs are
        filled in."""
        what = _doing(move)
        missing = [] if filled else [f["label"] for f in self.to_fill(t, record, move)]
        if missing:
            return f"Fill in {', '.join(missing[:-1]) + ' and ' + missing[-1] if len(missing) > 1 else missing[0]} first, then you can {what[:1].lower() + what[1:]}"
        if move.get("only_if") and not self.matches(record, move["only_if"]):
            # the same button for others when it doesn't hold (only the owner approves over 200 cups): said so, not "once"
            twin = next((m for m in (t.get("workflow") or {}).get("transitions") or [] if m is not move and m["to"] == move["to"]
                         and (m.get("label") or m["to"]) == (move.get("label") or move["to"])
                         and (not m["from"] or not move["from"] or set(m["from"]) & set(move["from"]))
                         and (not m.get("only_if") or self.matches(record, m["only_if"])) and not self.may_move(record, m)), None)
            if twin is not None:
                when = f" when {automations_words(self, twin['only_if'], t['key'])}" if twin.get("only_if") else ""
                return f"Only {self._who_words(twin.get('who'))} can {what[:1].lower() + what[1:]}{when}"
            return f"You can {what[:1].lower() + what[1:]} once {automations_words(self, move['only_if'], t['key'])}"
        apart = self.kept_from(record, move.get("not_by"))
        if apart:
            team = next((tm["name"] for tm in schema.teams(self.bp) for w in move.get("who") or [] if tm["key"] == w), None)
            return f"Someone else{' in ' + team if team else ''} has to {what[:1].lower() + what[1:]}, because {apart}"
        if self.role is not None and not schema.says_no(move["to"]):
            waiting = self.waiting_for(record)
            if waiting and not any(self.may_answer(a, record) for a in waiting):
                return self._waiting_words(waiting, record)
        return None

    def moves(self, record):
        """The workflow moves this person may make from the record's stage: [(index, move, what stops it or None)]."""
        t = self.tables.get(record.table)
        flow = (t or {}).get("workflow")
        if not flow or not schema.stages(t):
            return []
        at = (record.data or {}).get(flow["field"])
        return [(i, m, self.move_problem(t, record, m)) for i, m in enumerate(flow["transitions"])
                if (not m["from"] or at in m["from"]) and m["to"] != at and self.may_move(record, m)]

    def move(self, record, index, by=None):
        """Make a workflow move (a button on the record): to its stage, if this person may and nothing stops it. While the
        record waits for an approval, its buttons are the answer (Approve is a yes, Reject a no) for whoever may give it,
        and the rule carries on from there; the stage is then set as the button says if the rule didn't set it."""
        found = next(((m, problem) for i, m, problem in self.moves(record) if i == index), None)
        if found is None:
            raise FieldErrors({"_": "That move isn't open to you from this stage."})
        if found[1]:
            raise FieldErrors({"_": found[1]})
        key = self.tables[record.table]["workflow"]["field"]
        mine = [a for a in self.waiting_for(record) if self.may_answer(a, record)] if self.role is not None else []
        if mine:
            from . import automations
            if not schema.says_no(found[0]["to"]) and (mine[0].proposal or {}).get("step_up") and not self.stepped_up:
                raise FieldErrors({"_": STEP_UP})
            at = (record.data or {}).get(key)
            for approval in mine:
                if approval.status == "pending":
                    automations.decide(self.system(), approval, not schema.says_no(found[0]["to"]), by or self.by, by_id=self.user_id,
                                       onward=False)  # the button says where it goes, below
            self.reset()
            record = self.get(record.id)
            if record is None or (record.data or {}).get(key) != at or self.waiting_for(record):
                return record  # the rule moved it on, or it now waits for the next approver
            return self.system().update(record, {key: found[0]["to"], **self._stamps(record, found[0]["to"])}, by=by or self.by)
        stamps = self._stamps(record, found[0]["to"])
        moved = self.update(record, {key: found[0]["to"], **stamps}, by=by, moving=(key, *stamps))
        if schema.says_no(found[0]["to"]) and self.waiting_for(record):
            from . import automations
            automations.withdraw(self, record, by or self.by)
        return moved

    def _stamps(self, record, stage):
        """What a stage's own fields say that nobody has filled in: who moved it there ("Approved by") and when
        ("Approved on"), so a button press records them as the person would have. The stage's last words count too when
        they say it's done: a move to "Owner approved" fills in "Approved by" (never "Waiting for payment" a payment date)."""
        t, parts = self.tables[record.table], [p for p in re.split(r"\W+", str(stage).strip().lower()) if p]
        words = {"_".join(parts[i:]) for i in range(len(parts)) if i == 0 or parts[i].endswith("ed") or parts[i] in DONE_WORDS}
        out = {}
        for f in t["fields"] if words else []:
            if f["type"] not in schema.STORED or (record.data or {}).get(f["key"]) not in (None, "", []):
                continue
            names = {f["key"].lower(), re.sub(r"\W+", "_", f["label"].strip().lower()).strip("_")}
            if f["type"] == "user" and names & {f"{w}_by" for w in words} and self.user_id:
                out[f["key"]] = [self.user_id] if f.get("multiple") else self.user_id
            elif self.kind(f) in ("date", "datetime") and names & {x for w in words for x in (f"{w}_on", f"{w}_at", f"{w}_date", f"date_{w}")}:
                out[f["key"]] = self.today.isoformat() if self.kind(f) == "date" else self.now.isoformat(sep=" ", timespec="minutes")
        return out

    def _check_move(self, t, record, draft, new, creating, errors):
        """A person changing a record's stage: only by a move of its workflow they may make (a new one starts at the
        first stage)."""
        key, f = t["workflow"]["field"], self.field(t["key"], t["workflow"]["field"])
        if creating:
            start = schema.start_stage(t)
            if new not in (None, "") and start and new != start:
                errors[key] = f"A new {schema.lc(t['singular'])} starts at {start}"
            return
        old = (record.data or {}).get(key)
        if new == old:
            return
        waiting = self.waiting_for(record) if not schema.says_no(new) else []
        if waiting:  # onwards only by the answer (To do, or the record's buttons for whoever may give it)
            errors[key] = self._waiting_words(waiting, record) + (": answer it in To do or with its buttons" if any(self.may_answer(a, record) for a in waiting) else "")
            return
        fits = [m for m in t["workflow"]["transitions"] if (not m["from"] or old in m["from"]) and m["to"] == new and self.may_move(record, m)]
        if self.role == "owner":  # the owner moves anywhere, but not past a separation of duties or a sign-in check
            problems = [self.kept_from(record, m.get("not_by")) and f"Someone else must do this, as {self.kept_from(record, m.get('not_by'))}"
                        or (m.get("step_up") and not self.stepped_up and STEP_UP) for m in fits]
            if fits and all(problems):
                errors[key] = problems[0]
            return
        if not fits:
            onward = [m["to"] for _, m, _ in self.moves(record)]
            errors[key] = f"{f['label']} can't go from {old or 'blank'} to {new}" + (f" (from {old} it can go to {', '.join(onward)})" if old and onward else "")
            return
        problems = [self.move_problem(t, draft, m) or (m.get("step_up") and not self.stepped_up and STEP_UP) for m in fits]
        if all(problems):
            errors[key] = problems[0]

    def _ai_queue(self, record, changed, creating):
        """Fields the AI fills itself: when a record is added, and again when what they're based on changes, unless a
        person has written that field themselves (people's words are never overwritten)."""
        from .. import jobs
        t = self.tables.get(record.table)
        owned = set((record.data or {}).get("_ai") or [])
        for f in t["fields"]:
            spec = f.get("ai")
            if not spec:
                continue
            current = (record.data or {}).get(f["key"])
            empty = current in (None, "", [])
            sources = set(spec.get("from") or []) or {x["key"] for x in t["fields"] if not x.get("ai")}
            if (creating and empty) or (not creating and (empty or f["key"] in owned) and sources & set(changed)):
                jobs.enqueue("ai_field", {"record_id": record.id, "field": f["key"]}, workspace_id=self.ws.id,
                             dedupe=f"ai_field:{record.id}:{f['key']}")

    def _number(self, t, f):
        """The next number of an autonumber field, taken in one statement so two saves at once never get the same one."""
        params = {"w": self.ws.id, "t": t["key"], "f": f["key"]}
        bump = sql('UPDATE counter SET value = value + 1 WHERE workspace_id = :w AND "table" = :t AND field = :f RETURNING value')
        value = db.session.execute(bump, params).scalar()
        if value is None:
            try:
                with db.session.begin_nested():
                    db.session.add(Counter(workspace_id=self.ws.id, table=t["key"], field=f["key"], value=1))
                value = 1
            except IntegrityError:  # someone else numbered the first one at the same moment
                value = db.session.execute(bump, params).scalar()
        return f"{f.get('prefix', '')}{value:0{f.get('digits', 4)}d}"

    def _keep_number(self, t, f, given):
        """A number brought in as it was (1001 → ORD-1001 when that's the field's style); the counter moves past it."""
        digits = re.findall(r"\d+", given)
        if digits:
            n = int(digits[-1])
            params = {"w": self.ws.id, "t": t["key"], "f": f["key"], "n": n}
            moved = db.session.execute(sql('UPDATE counter SET value = CASE WHEN value < :n THEN :n ELSE value END WHERE workspace_id = :w AND "table" = :t AND field = :f'), params)
            if not moved.rowcount:
                db.session.add(Counter(workspace_id=self.ws.id, table=t["key"], field=f["key"], value=n))
                db.session.flush()
        if given.isdigit():
            return f"{f.get('prefix', '')}{int(given):0{f.get('digits', 4)}d}"
        return given

    def number_missing(self, table):
        """Give records made before an autonumber field existed their numbers, oldest first."""
        t = self.tables.get(table)
        for f in (t or {}).get("fields", []):
            if f["type"] != "autonumber":
                continue
            for r in self.rows(table):
                if not (r.data or {}).get(f["key"]):
                    r.data = {**(r.data or {}), f["key"]: self._number(t, f)}

    # --- saving safely -----------------------------------------------------------------------------------------------
    @contextmanager
    def atomic(self):
        """Writes inside the block happen together or not at all. Rules are checked, and automations, AI fields and
        indexing start, once the whole block is saved. Inside batch(), the block is one row of it."""
        if self._depth == 0 and self._batch is not None:
            with self._row():
                yield self
            return
        self._depth += 1
        try:
            yield self
        except BaseException:
            self._depth -= 1
            if self._depth == 0:
                self._abandon()
            raise
        self._depth -= 1
        if self._depth == 0:
            if self._failed:
                self._abandon()
                raise Conflict()
            self._finish()

    @contextmanager
    def batch(self, size=200, committed=None):
        """Many writes that each stand alone (an import's rows), committed `size` at a time rather than one by one. Each
        `with engine.atomic():` inside is one row: it goes in whole or not at all, and its rules are checked as it ends,
        as if it were saved on its own; what was read stays read from one row to the next. Automations, AI fields and
        indexing start as each lot is committed, and `committed()` is called then (an import says how far it's got)."""
        self._batch = {"size": size, "rows": 0, "committed": committed}
        try:
            yield self
        finally:
            done, self._batch = self._batch, None
            if done["rows"]:  # what was saved before anything went wrong stays saved, as it did row by row
                self._finish(rules=False)

    @contextmanager
    def _row(self):
        """One row of a batch, in a savepoint: released when the row is saved whole, rolled back (only it) when not."""
        savepoint = db.session.begin_nested()
        before, self._log = (set(self._touched), list(self._after), set(self._open)), []
        self._depth += 1
        try:
            yield
            if self._failed:
                raise Conflict()
            db.session.flush()
            self._forget(self._dirty)
            self._check_rules(self._touched - before[0])
            savepoint.commit()
        except BaseException as exc:
            savepoint.rollback()
            self._touched, self._after, self._open = before
            self._failed = False
            self._unwrite(self._log)
            if isinstance(exc, StaleDataError):
                raise Conflict() from exc
            raise
        finally:
            self._depth -= 1
            self._log = None
        self._batch["rows"] += 1
        if self._batch["rows"] >= self._batch["size"]:
            self._batch["rows"] = 0
            self._finish(rules=False)
            if self._batch["committed"]:
                self._batch["committed"]()

    def _wrote(self, record, how):
        """Keep what's been read in step with a write not committed yet: a table read whole stays read (the new record
        added to it, a deleted one taken out); what's found by its values (lines, names) is found again."""
        rows = self._rows.get(record.table)
        if rows is not None:
            if how == "created" and (not rows or rows[-1].id < record.id):
                rows.append(record)
            elif how == "deleted":
                self._rows[record.table] = [r for r in rows if r.id != record.id]
            elif how != "updated":
                del self._rows[record.table]
        if record.id is not None:
            self._by_id[record.id] = record
        self._dirty.add(record.table)
        if getattr(self, "_log", None) is not None:
            self._log.append((how, record))

    def _unwrite(self, log):
        """A batch's row rolled back: the records it added are gone again (SQLAlchemy reads back the ones it changed)."""
        if any(how not in ("created", "updated") for how, _ in log):
            self.reset()
            return
        made = {r.id for how, r in log if how == "created"}
        for table in {r.table for _, r in log}:
            if table in self._rows:
                self._rows[table] = [r for r in self._rows[table] if r.id not in made]
            self._dirty.add(table)
        for rid in made:
            self._by_id.pop(rid, None)
        self._forget(self._dirty)

    def _abandon(self):
        db.session.rollback()
        self._touched, self._after, self._failed, self._open = set(), [], False, set()
        self._before = {}
        self.reset()

    def _pair_links(self, t, record, keys, by=None):
        """Two kinds of record that link to each other (a job's quote, and the quote's job) stay in step: setting one side
        fills in the other where it's empty, so a list, a rule or a document reading either side finds the same pair."""
        for key in keys:
            f = schema.field(t, key)
            value = (record.data or {}).get(key)
            if f is None or f["type"] != "link" or f.get("multiple") or not str(value or "").isdigit() or f["table"] == t["key"]:
                continue
            back = [g for g in (self.tables.get(f["table"]) or {"fields": []})["fields"]
                    if g["type"] == "link" and g.get("table") == t["key"] and not g.get("multiple")]
            other = Record.query.filter_by(id=int(value), workspace_id=self.ws.id, table=f["table"], deleted_at=None).first() if len(back) == 1 else None
            if other is None or (other.data or {}).get(back[0]["key"]) not in (None, "", []):
                continue
            other.data = {**(other.data or {}), back[0]["key"]: record.id}  # its version goes up as it's saved
            self._wrote(other, "updated")
            db.session.add(RecordChange(workspace_id=self.ws.id, record_id=other.id, kind="updated", by=f"{by or self.by} (linked from its {f['label'].lower()})",
                                        by_id=self.user_id, changes={back[0]["key"]: [None, record.id]}))

    def _saved(self, record, after=None, how="updated"):
        """A write is ready: commit it now, or at the end of the atomic block it's part of. Until it's committed, only
        what was read from the tables written is read again."""
        self._touched.add(record.id)
        if after:
            self._after.append(after)
        if self._depth:
            try:
                db.session.flush()
            except StaleDataError as exc:
                self._failed = True  # the whole block is refused when it ends, even if this is caught inside it
                raise Conflict() from exc
            self._wrote(record, how)
            self._forget(self._dirty)
            return
        self._wrote(record, how)
        self._finish()

    def _finish(self, rules=True):
        """Commit what's been written. Everything read is thrown away after: committed records are read afresh, which
        reading them again in one go does better than record by record. (rules: whether to check the rules; a batch
        checked each row's as it ended.)"""
        try:
            db.session.flush()
            self._forget(self._dirty)
            if rules:
                self._check_rules()
            db.session.commit()
        except StaleDataError as exc:
            self._abandon()
            raise Conflict() from exc
        except BaseException:
            self._abandon()
            raise
        touched, after = self._touched, self._after
        self._touched, self._after, self._open, self._before = set(), [], set(), {}
        self.reset()
        self._index_later(touched)
        self._tell_followers(touched)
        for fn in after:
            fn()

    def _tell_followers(self, record_ids):
        """People following a record hear when someone else changes it."""
        from ..models import Follow, Notification
        if not record_ids:
            return
        follows = Follow.query.filter(Follow.record_id.in_(list(record_ids))).all()
        for f in follows:
            if f.user_id == self.user_id:
                continue
            r = self.get(f.record_id) or db.session.get(Record, f.record_id)
            t = self.tables.get(r.table) if r is not None else None
            if t is None:
                continue
            what = "deleted" if r.deleted_at else "changed"
            db.session.add(Notification(workspace_id=self.ws.id, user_id=f.user_id, link=f"table:{r.table}:record:{r.id}",
                                        text=f"{self.by} {what} {schema.lc(t['singular'])} “{self.title(r)}”, which you follow"))
        if follows:
            db.session.commit()

    def _index_later(self, record_ids):
        """Records the AI's search should re-read: these, and the records whose lines they are."""
        from .. import knowledge
        refs = set()
        for rid in record_ids:
            refs.add(rid)
            r = self.get(rid) or db.session.get(Record, rid)
            parent = self._parent(r) if r is not None else None
            if parent is not None:
                refs.add(parent.id)
        knowledge.touch(self.ws.id, "record", refs)

    def _parent(self, record):
        """For a line item (an invoice line), the record it belongs to."""
        t = self.tables.get(record.table) if record is not None else None
        if not t or not t.get("embedded"):
            return None
        link = next((f for f in t["fields"] if f["type"] == "link" and not (self.tables.get(f["table"]) or {}).get("embedded")), None)
        return self.get((record.data or {}).get(link["key"]), link["table"]) if link else None

    def _check_rules(self, touched=None):
        """Every rule of every record just written (and of the records whose lines were written) must hold, and what was
        approved stays as approved (see _held). `touched`: only these (one row of a batch)."""
        seen = set()
        for rid in sorted(self._touched if touched is None else touched):
            r = self.get(rid)
            for target in (r, self._parent(r) if r is not None else None):
                if target is None or target.id in seen:
                    continue
                seen.add(target.id)
                t = self.tables.get(target.table) or {}
                held = self._held(t, target)
                if held:
                    raise FieldErrors({"_": held})
                for rule in t.get("rules", []):
                    if rule.get("when") and not self.matches(target, rule["when"]):
                        continue
                    try:
                        ok = self._keeps(t, target, rule)
                    except expr.ExprError as exc:
                        log.warning("The rule %r on %s couldn't be worked out: %s", rule["check"], t.get("key"), exc)
                        continue
                    if not ok and target is r and self._kept_out_of_it(t, target, rule):
                        continue
                    if not ok:
                        where = rule.get("field") if target is r and rule.get("field") else "_"
                        prefix = "" if target is r else f"{t['singular']} “{self.title(target)}”: "
                        refused = FieldErrors({where: prefix + rule["message"]})
                        read = sorted(n for n in expr.names_in(rule["check"]) if self.field(target.table, n))
                        refused.rule = (t["key"], rule["check"], {n: (target.data or {}).get(n) for n in read})  # what it saw
                        raise refused

    def _keeps(self, t, record, rule):
        """Whether a record keeps a rule: its check is true, and what it says must hold does."""
        return to_bool(expr.evaluate(schema.percents_fixed(t, rule["check"], self.tables), RecordContext(self, record))) \
            and (not rule.get("holds") or self.matches(record, rule["holds"]))

    def _kept_out_of_it(self, t, record, rule):
        """Whether a rule broken now was broken before this save too, and this save didn't touch what it's about: then
        it doesn't stop the save (a job already In progress when its client fell behind can still have its notes
        changed). A record added now, a rule this save broke, or a change to what the rule reads is refused."""
        old = self._before.get(record.id, None)
        if record.id not in self._before or old is None:
            return False
        read = set()
        said = list(rule.get("when") or []) + list(rule.get("holds") or [])
        try:
            read |= {n for n in expr.outer_names(rule["check"])}
            for source in schema.formulas(said):
                read |= expr.outer_names(source)
        except expr.ExprError:
            return False
        read |= {c["field"].split(".")[0] for c in schema.condition_leaves(said)}
        now = record.data or {}
        if any(old.get(k) != now.get(k) for k in read if not str(k).startswith("_")) or "this" in read or "record" in read:
            return False
        draft = Record(workspace_id=self.ws.id, table=record.table, data=old)
        draft.id = record.id
        memo, self._memo = self._memo, {}
        try:
            if rule.get("when") and not self.matches(draft, rule["when"]):
                return False  # it didn't apply before: this save made it apply
            return not self._keeps(t, draft, rule)
        except expr.ExprError:
            return False
        finally:
            self._memo = memo

    # --- what was approved stays approved ---------------------------------------------------------------------------
    def _approved_amounts(self, t, record):
        """The money an approval of a record was given for: its totals, and the money the moves and rules about it read
        (a total over $5,000). Not what's meant to change later (paid, received, the balance)."""
        read = set()
        conditions = lambda cs: {c.get("field") for c in cs or [] if isinstance(c, dict)}  # noqa: E731
        for m in (t.get("workflow") or {}).get("transitions", []):
            read |= conditions(m.get("only_if"))
        for a in self.bp.get("automations", []):
            if (a.get("trigger") or {}).get("table") == t["key"]:
                read |= conditions(a.get("conditions"))
                for act in schema.steps_of(a.get("actions")):
                    read |= conditions(act.get("only_if"))
                    for path in act.get("paths") or []:
                        read |= conditions(path.get("when"))
        out = {}
        for f in t["fields"]:
            name = f"{f['key']} {f['label']}".replace("_", " ")
            if self.kind(f) == "money" and (f["key"] in read or (AMOUNT.search(name) and not NOT_AMOUNT.search(name))):
                value = to_number(self.value(record, f["key"]))
                if value is not None:
                    out[f["key"]] = round(value, 2)
        return out

    def mark_approved(self, record, by=None, by_id=None):
        """Note what a record was approved at (an approval given, or a move into an Approved stage, a rule's included: an
        order under the limit that went straight through), so it can't quietly grow afterwards. Kept with the record."""
        t = self.tables.get(record.table)
        flow = (t or {}).get("workflow")
        stage = (record.data or {}).get(flow["field"]) if flow else None
        was = (record.data or {}).get("_approved")
        if was and was.get("by_id") and not by_id:  # a person's approval, then the rule moving it on: still theirs
            record.data = {**record.data, "_approved": {**was, "stage": stage}}
            return
        amounts = self._approved_amounts(t, record) if t else {}
        if amounts:
            record.data = {**(record.data or {}), "_approved": {"amounts": amounts, "by": by, "by_id": by_id, "stage": stage}}

    def _approval_moved(self, t, record, stage, by):
        """A record moving into an Approved stage is held to what it was approved at; moving back before where that was
        (sent back for approval, back to Draft) starts it over."""
        names = schema.stages(t)
        approved = (record.data or {}).get("_approved")
        if approved and stage in names and approved.get("stage") in names and names.index(stage) < names.index(approved["stage"]):
            record.data = {k: v for k, v in record.data.items() if k != "_approved"}
        elif APPROVED.search(str(stage or "")) and not WAITING.search(str(stage)) and not schema.says_no(stage):
            self.mark_approved(record, by=(by or self.by) if self.role is not None else None, by_id=self.user_id)

    def _held(self, t, record):
        """Why this save can't stand: it takes money above what the record was approved at (a $6,000 order made $18,000
        after it was approved), unless it's by whoever approved it, the owner or the rules. Or None."""
        approved = (record.data or {}).get("_approved")
        if not approved or self.role in (None, "owner") or (approved.get("by_id") and approved["by_id"] == self.user_id):
            return None
        for key, was in (approved.get("amounts") or {}).items():
            f = self.field(t["key"], key)
            now = to_number(self.value(record, key)) if f else None
            if now is not None and now > was + 0.005:
                who = approved.get("by")
                return (f"This {schema.lc(t['singular'])} was approved at {self.display(f, was)}{' by ' + who if who else ''}, so its "
                        f"{schema.lc(f['label'])} can't go above that now ({self.display(f, now)}). For more, add another "
                        f"{schema.lc(t['singular'])}{', or ask ' + who + ' to change it' if who else ''}.")
        return None

    def locked(self, record):
        """Why this person can't change a record (it's posted, approved, in a closed period), or None. Admins and owners,
        and the platform's own automations, can still change locked records; every change stays in its history. A
        sealed record can't be changed by anyone but its maker until it opens, nor by anyone after."""
        sealed = self._sealed_lock(record) if record is not None else None
        if sealed:
            return sealed[0]
        if record is None or self._at_least("admin"):
            return None
        for target in (record, self._parent(record)):
            if target is not None and target.id in self._open:
                continue
            t = self.tables.get(target.table) if target is not None else None
            if t and t.get("lock_if") and self.matches(target, t["lock_if"]):
                what = automations_words(self, t["lock_if"], t["key"])
                return f"This {schema.lc(t['singular'])} is locked ({what}), so only an admin can change it."
        return None

    def _check_lock(self, record, changing, also=()):
        """also: what a move on a locked record may change (its stage, what moving stamps): a sent invoice is still marked
        paid by its button; what's in it stays as it was."""
        sealed = self._sealed_lock(record) if record is not None else None
        reason = sealed[0] if sealed else self.locked(record)
        if reason is None:
            if self._depth and record is not None:
                self._open.add(record.id)
            return
        t = self.tables.get(record.table) or {}
        allowed = sealed[1] if sealed else set(t.get("lock_except") or []) if not t.get("embedded") else set()
        allowed = set(allowed) | (set(also) if not sealed else set())
        if changing is None or set(changing) - allowed:
            raise FieldErrors({"_": reason})

    def _mine_when_blank(self, t):
        """The person fields a person adding a record is put in when they leave them empty: the one that decides who sees
        it, when they're among those it limits (a rep's order would vanish from their own list), and one saying who
        raised it ("Raised by", "Requested by")."""
        if self.role is None or not self.user_id:
            return []
        rows = t.get("row_access") or {}
        mine = rows.get("field") if self.role in (rows.get("roles") or []) else None
        return [f for f in t["fields"] if f["type"] == "user" and (f["key"] == mine or RAISED_BY.search(f"{f['key']} {f['label']}".replace("_", " ")))]

    def create(self, table, data, *, by=None, automations=True, depth=0, ai=True, keep_numbers=False, unfinished=False):
        """keep_numbers: an autonumber given in `data` is kept (past invoices brought in keep theirs) and new records
        carry on after it. unfinished: added even with must-haves left empty that nothing given could fill (a rule's,
        for people to finish: self.left_unfilled says which)."""
        t = self.tables.get(table)
        if t is None:
            raise FieldErrors({"_": f"there's no table “{table}”"})
        self._not_combined(t)
        if self.role is not None and not self.can(t, "edit"):
            raise FieldErrors({"_": f"Your role can't add {schema.lc(t['label'])}."})
        values = self._clean(t, data or {}, creating=True, unfinished=unfinished)
        left = list(self.left_unfilled)
        for f in self._mine_when_blank(t):
            if values.get(f["key"]) in (None, "", []):
                values[f["key"]] = [self.user_id] if f.get("multiple") else self.user_id
        left = [f["label"] for f in t["fields"] if f["label"] in left and values.get(f["key"]) in (None, "", [])]
        parent_link = next((f for f in t["fields"] if f["type"] == "link"), None) if t.get("embedded") else None
        if parent_link is not None and values.get(parent_link["key"]):
            self._check_lock(self.get(values[parent_link["key"]], parent_link["table"]), None)
        for f in t["fields"]:
            if f["type"] == "autonumber":
                given = str((data or {}).get(f["key"]) or "").strip() if keep_numbers else ""
                values[f["key"]] = self._keep_number(t, f, given) if given else self._number(t, f)
        record = Record(workspace_id=self.ws.id, table=table, data=values, created_by=by or self.by, updated_by=by or self.by,
                        created_by_id=self.user_id)
        db.session.add(record)
        db.session.flush()
        self._before.setdefault(record.id, None)
        if self._depth:
            self._open.add(record.id)
        db.session.add(RecordChange(workspace_id=self.ws.id, record_id=record.id, kind="created", by=by or self.by, by_id=self.user_id,
                                    changes=self._logged(t, {k: [None, v] for k, v in values.items() if v not in (None, "", [])}, record)))
        self._pair_links(t, record, list(values), by)

        def after():
            if ai:
                self._ai_queue(record, list(values), creating=True)
            if automations:
                from . import automations as auto
                auto.on_change(self, self.get(record.id) or record, None, depth=depth)
            self._combine(record)
        self._saved(record, after, "created")
        self.left_unfilled = left  # (what its own rules added may have said otherwise meanwhile)
        return record

    def _not_combined(self, t):
        if t.get("combines") and not getattr(self, "combining", False):
            from .combined import sources
            raise FieldErrors({"_": f"{t['label']} is put together from {', '.join(sources(self.bp, t))}: change those instead."})

    def _combine(self, record):
        """The combined lists this record goes into follow it (engine/combined.py)."""
        if any(x.get("combines") for x in self.bp.get("tables", [])):
            from . import combined
            combined.sync(self, record)

    def update(self, record, data, *, by=None, automations=True, depth=0, ai_filled=False, expected_version=None, moving=()):
        """Change a record. ai_filled: the AI wrote these values (it may rewrite them later); otherwise a person or a rule
        did, and the AI leaves those fields alone from now on. expected_version: the version the person was looking at;
        if the record has changed since, the save is refused (Conflict)."""
        if expected_version not in (None, "") and str(expected_version) != str(record.version):
            raise Conflict()
        t = self.tables.get(record.table)
        self._not_combined(t)
        if self.role is not None and not self.can(t, "edit"):
            raise FieldErrors({"_": f"Your role can't change {schema.lc(t['label'])}."})
        values = self._clean(t, data or {}, creating=False, record=record)
        old = dict(record.data or {})
        diff = {k: [old.get(k), v] for k, v in values.items() if old.get(k) != v}
        if diff and t.get("approve_changes") and (self.role is not None or self.portal_contact is not None):
            watched = set(t["approve_changes"]["fields"]) or set(diff)  # a person's change to these waits for someone else
            proposed = {k: v[1] for k, v in diff.items() if k in watched}
            if proposed:
                self._check_lock(record, list(proposed))
                from . import automations as auto
                auto.propose_change(self, record, proposed, by or self.by)
                self.held = [self.field(t["key"], k)["label"] for k in proposed]
                diff = {k: v for k, v in diff.items() if k not in proposed}
                values = {k: v for k, v in values.items() if k not in proposed}
        if not diff:
            return {}
        self._check_lock(record, list(diff), also=moving)
        if t.get("embedded"):  # a line moved to another record mustn't land in one that's locked
            link = next((f for f in t["fields"] if f["type"] == "link" and f["key"] in diff and not (self.tables.get(f["table"]) or {}).get("embedded")), None)
            if link is not None:
                self._check_lock(self.get(values[link["key"]], link["table"]), None)
        owned = set(old.get("_ai") or [])
        owned = owned | set(diff) if ai_filled else owned - set(diff)
        self._before.setdefault(record.id, old)
        record.data = {**old, **values, "_ai": sorted(owned)}
        flow = t.get("workflow")
        if flow and flow["field"] in diff:
            self._approval_moved(t, record, diff[flow["field"]][1], by)
        record.updated_at, record.updated_by = utcnow(), (by or self.by)[:200]
        db.session.add(RecordChange(workspace_id=self.ws.id, record_id=record.id, kind="updated", by=by or self.by, by_id=self.user_id,
                                    changes=self._logged(t, diff, record)))
        self._pair_links(t, record, list(diff), by)

        def after():
            if not ai_filled:
                self._ai_queue(record, list(diff), creating=False)
            if automations:
                from . import automations as auto
                auto.on_change(self, self.get(record.id) or record, old, depth=depth)
            self._combine(record)
        try:
            self._saved(record, after)
        except Conflict:
            if expected_version is not None or self._depth or self.role is not None:
                raise
            # the platform's own change (an automation, the AI) met someone else's save: apply it to the new version
            fresh = db.session.get(Record, record.id)
            if fresh is None or fresh.deleted_at is not None:
                raise
            return self.update(fresh, {k: v[1] for k, v in diff.items()}, by=by, automations=automations, depth=depth,
                               ai_filled=ai_filled, expected_version=fresh.version)
        return diff

    def _logged(self, t, diff, record=None):
        """A change as kept in the history: sensitive values only as their masks, those of a sealed record not at all."""
        secret = {f["key"] for f in t["fields"] if f.get("sensitive")}
        seal = self._seal(record) if record is not None else None
        sealed = seal is not None and (seal[1] is None or self.now < seal[1])
        out = {}
        for k, pair in diff.items():
            if k in secret:
                out[k] = [masked(x) for x in pair]
            elif sealed and (self.field(t["key"], k) or {}).get("type") != "link" and k != (t.get("workflow") or {}).get("field"):
                out[k] = ["(sealed)", "(sealed)"]
            else:
                out[k] = pair
        return out

    def delete(self, record, *, by=None, automations=True, depth=0):
        t = self.tables.get(record.table)
        self._not_combined(t)
        if self.role is not None and not self.can(t, "edit"):
            raise FieldErrors({"_": f"Your role can't delete {schema.lc(t['label'])}."})
        self._check_lock(record, None)
        record.deleted_at = utcnow()
        db.session.add(RecordChange(workspace_id=self.ws.id, record_id=record.id, kind="deleted", by=by or self.by, by_id=self.user_id))

        def after():
            if automations:
                from . import automations as auto
                auto.on_delete(self, record, depth=depth)
            self._combine(record)
        self._saved(record, after, "deleted")

    def restore(self, record, *, by=None):
        record.deleted_at = None
        db.session.add(RecordChange(workspace_id=self.ws.id, record_id=record.id, kind="restored", by=by or self.by))
        self._saved(record, lambda: self._combine(record), "restored")

    def counts(self):
        rows = (db.session.query(Record.table, db.func.count(Record.id))
                .filter(Record.workspace_id == self.ws.id, Record.deleted_at.is_(None)).group_by(Record.table).all())
        return {table: n for table, n in rows if table in self.tables}
