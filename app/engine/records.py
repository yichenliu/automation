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

from flask import current_app
from sqlalchemy import text as sql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError

from ..blueprint import expr, schema
from ..blueprint.expr import to_bool, to_date, to_datetime, to_number
from ..extensions import db
from ..models import ROLES, Counter, File, Membership, Record, RecordChange, User, utcnow

log = logging.getLogger("engine")
STEP_UP = "Confirm it's you first: enter the code from your authenticator app (or your password)"
APPROVED = re.compile(r"\bapproved\b|\bauthori[sz]ed\b|\bsigned off\b", re.I)  # a stage where it's been approved (not one waiting for it)
WAITING = re.compile(r"wait|pending|awaiting|for approval|to approve|needs", re.I)
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


class FieldErrors(ValueError):
    """Values that can't be saved, by field: {"due_date": "“next week” isn't a date"}."""

    def __init__(self, errors):
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


class Conflict(FieldErrors):
    """The record changed since this copy of it was read (someone else saved it in the meantime)."""

    def __init__(self, message="Someone else changed this record while you were editing it. Reload to see their change, then make yours."):
        super().__init__({"_": message})


class RecordProxy:
    """A record as expressions see it: `client.payment_terms`, `record.name`, or the record itself (its title)."""

    def __init__(self, engine, record):
        self._engine, self.record = engine, record

    def field(self, name):
        return self._engine.value(self.record, name, typed=True)

    def __str__(self):
        return self._engine.title(self.record)

    def __eq__(self, other):
        if isinstance(other, RecordProxy):
            return other.record.id == self.record.id
        return str(self).lower() == str(other).strip().lower() or str(other) == str(self.record.id)

    def __hash__(self):
        return hash(self.record.id)


class RecordContext(expr.Context):
    """Names in a formula or template: the record's own fields, `record` itself, plus today/now."""

    def __init__(self, engine, record, extra=None):
        super().__init__(extra or {}, today=engine.today, now=engine.now)
        self.engine, self.record = engine, record

    @property
    def functions(self):
        return {"convert": self.engine.convert, "workday": self.engine.workday, "add_workdays": self.engine.workday,
                "workdays_between": self.engine.workdays_between, "networkdays": self.engine.workdays_between}

    def resolve(self, name):
        if name in self.names:
            return self.names[name]
        if name == "record":
            return RecordProxy(self.engine, self.record) if self.record is not None else None
        if self.record is not None:
            t = self.engine.tables.get(self.record.table)
            if name in ("id", "created_at", "updated_at") or (t and schema.field(t, name)):
                return self.engine.value(self.record, name, typed=True)
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
        self.base_currency = (money.get("base") or "").upper()
        self.rates = {str(k).upper(): float(v) for k, v in (money.get("rates") or {}).items() if to_number(v)}
        self._rows, self._by_id, self._memo, self._index, self._computing, self._seals = {}, {}, {}, {}, set(), {}
        self.started = []  # automation runs this engine started (so a screen can say what ran)
        self._members, self._files, self._sources = None, {}, None
        self._depth, self._after, self._touched, self._failed = 0, [], set(), False
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
        need = (t.get("access") or {}).get(action) or ("viewer" if action == "view" else "editor")
        if action == "edit":
            need = max(need, (t.get("access") or {}).get("view") or "viewer", "editor", key=ROLES.index)
        teams = (t.get("access") or {}).get("teams")
        if teams and self.role is not None and not self._at_least("admin") and not self.teams & set(teams):
            return False  # kept to a team (Payments for Finance), and admins
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
        rows = t.get("row_access") if t else None
        if not rows or self.role is None or self.role not in rows["roles"] or self.teams & set(rows.get("teams") or []):
            return None
        return rows["field"]

    def allowed(self, record):
        """Whether this person may see this record (tables can limit people to the records that are theirs)."""
        if record is None:
            return False
        t = self.tables.get(record.table)
        if not self.can(t):
            return False
        owner = self._owner_rule(t)
        if owner is None or record.id in self._asked():
            return True
        if owner == "created_by":  # by who they are, not their name (anyone can change their name)
            return record.created_by_id is not None and record.created_by_id == self.user_id
        head, _, rest = owner.partition(".")
        if rest:  # a linked record's person: an order is a rep's when its customer is
            f = self.field(record.table, head)
            linked = self.get((record.data or {}).get(head), f["table"]) if f and f["type"] == "link" else None
            value = (linked.data or {}).get(rest) if linked is not None else None
        else:
            value = self.value(record, owner)  # a person field, or a lookup of one
        if isinstance(value, list):
            return str(self.user_id) in [str(v) for v in value]
        return value is not None and str(value) == str(self.user_id)

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
        return self if self.role is None else Engine(self.ws, by=self.by)

    def visible_rows(self, table):
        rows = self.rows(table)
        if self.role is None:
            return rows
        t = self.tables.get(table)
        if not self.can(t):
            return []
        return rows if self._owner_rule(t) is None else [r for r in rows if self.allowed(r)]

    # --- reading ------------------------------------------------------------------------------------------------
    def reset(self):
        self._rows, self._by_id, self._memo, self._index, self._files = {}, {}, {}, {}, {}
        self._seals, self._asked_ids = {}, None

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
        people = next((p["table"] for p in schema.portals(self.bp) if schema.portal_shows(self.tables, t, p["table"])), None)
        head, _, hop = portal["via"].partition(".")
        via = self.field(t["key"], head)
        if via is None or via["type"] != "link" or people is None:
            return False
        ids = lambda v: [int(x) for x in (v if isinstance(v, list) else [v]) if str(x).isdigit()]  # noqa: E731
        targets = ids((record.data or {}).get(head))
        if via["table"] == people and not hop:
            found = contact_id in targets
        else:
            middle = self.tables.get(via["table"]) or {}
            on = schema.field(middle, hop) if hop else next((f for f in middle.get("fields", []) if f["type"] == "link" and f["table"] == people), None)
            found = on is not None and any(contact_id in ids((m.data or {}).get(on["key"]))
                                           for m in (self.get(x, via["table"]) for x in targets) if m is not None)
        return found and (not portal.get("show_if") or self.matches(record, portal["show_if"]))

    def members(self):
        """{user id: name} of the people in this workspace (for person fields)."""
        if self._members is None:
            rows = db.session.query(User).join(Membership, Membership.user_id == User.id).filter(Membership.workspace_id == self.ws.id).all()
            self._members = {u.id: u.display for u in rows}
            self._emails = {u.id: u.email for u in rows}
        return self._members

    def files(self, record, key):
        """The files in a file or image field: [{"id", "name", "mime", "size"}]."""
        if record is None or record.id is None:
            return []
        if (record.id, key) not in self._files:
            rows = File.query.filter_by(record_id=record.id, field=key, deleted_at=None).order_by(File.id).all()
            self._files[(record.id, key)] = [{"id": x.id, "name": x.name, "mime": x.mime, "size": x.size} for x in rows]
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
            if text not in ("", "—") and kind in ("link", "autonumber") and record.id is not None and len(self._naming) == 1:
                others = [self.display(f, self.value(record, f["key"])) for f in t["fields"]
                          if f["type"] == "link" and f["key"] != key and not f.get("multiple") and (record.data or {}).get(f["key"])][:2]
                text = " · ".join([text] + [o for o in others if o not in ("", "—")])
        finally:
            self._naming.discard(record.id)
        return text if text not in ("", "—") else f"{t['singular'] if t else 'Record'} #{record.id}"

    def value(self, record, key, typed=False):
        """A field's value: stored ones as saved, formulas and rollups worked out. typed: dates as dates, links as records."""
        if key == "id":
            return record.id
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
        else:
            raw = (record.data or {}).get(key)
        if not typed:
            return raw
        return self._typed(f, raw)

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
            return [self.members().get(int(x)) for x in (raw if isinstance(raw, list) else [raw]) if str(x).isdigit()]
        if kind == "user":
            return self.members().get(int(raw)) if str(raw).isdigit() else None
        if kind == "location":
            return raw if isinstance(raw, dict) else None
        if kind in ("duration", "rating"):
            return to_number(raw)
        if kind in schema.FILES:
            return ", ".join(x["name"] for x in raw)
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
            value = expr.evaluate(schema.percents_fixed(self.tables.get(record.table), f["expression"]), RecordContext(self, record))
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
        """Records of `table` whose link field `via` points at parent_id."""
        index_key = (table, via)
        if index_key not in self._index:
            index = {}
            for r in self.rows(table):
                target = (r.data or {}).get(via)
                for one in (target if isinstance(target, list) else [target]):  # a link to many counts under each
                    if one is not None:
                        index.setdefault(int(one) if str(one).isdigit() else one, []).append(r)
            self._index[index_key] = index
        return self._index[index_key].get(parent_id, [])

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
        numbers = [n for n in (to_number(self.value(r, key)) for r in rows) if n is not None]
        if not numbers:
            return 0 if aggregate == "sum" else None
        if aggregate == "sum":
            total = sum(numbers)
            return round(total, 2) if isinstance(total, float) else total
        if aggregate == "avg":
            return round(sum(numbers) / len(numbers), 2)
        return min(numbers) if aggregate == "min" else max(numbers)

    def summable(self, f):
        """Whether a column's total means something: amounts and hours yes; prices, rates and percentages per unit no;
        amounts in different currencies no."""
        if f is None or self.kind(f) not in ("money", "duration", "number") or f.get("currency_field"):
            return False
        if f["type"] == "rollup" and f.get("aggregate") in ("count", "avg", "min", "max"):  # adding up counts or averages means nothing
            return False
        if self.kind(f) == "number" and (f.get("unit") or any(w in f["key"] for w in ("on_hand", "stock", "level", "balance"))):
            return False  # kilos of flour and of butter don't add up to anything
        words = f"{f['key']} {f['label']}".lower()
        if any(w in words for w in ("unit", "price", "rate", "per ", "per_", "each", "fee", "percent", "discount", "terms", "days",
                                    "qty", "quantity", "rating", "age", "year", "number", "phone")):
            return f["type"] == "rollup" and f.get("aggregate") == "sum"
        return self.kind(f) in ("money", "duration") or (f["type"] in ("rollup", "formula") and self.kind(f) == "number")

    def kind(self, f):
        """How a field's values look: its type, a formula's format, or the type of what a rollup totals."""
        if f is None:
            return "text"
        if f["type"] == "formula":
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
        rates in Settings › General."""
        n = to_number(amount)
        if n is None:
            return None
        source = str(source or self.base_currency).strip().upper()[:3]
        target = str(target or self.base_currency).strip().upper()[:3]
        rates = {**self.rates, **({self.base_currency: 1.0} if self.base_currency else {})}
        for code in (source, target):
            if code not in rates:
                raise expr.ExprError(f"there's no exchange rate for {code or 'that currency'} (add it in Settings › General)")
        return round(n * rates[source] / rates[target], 2)

    def display(self, f, value, record=None):
        """A value as text for people: $1,234.50, 3 Nov 2026, 15%, Yes, the linked record's name."""
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
            return f"{value['lat']:.5f}, {value['lng']:.5f}" if isinstance(value, dict) and "lat" in value else str(value)
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
            return "Yes" if to_bool(value) else "No"
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

    def matches(self, record, conditions, old=None, about=None):
        """Whether a record meets every condition. `old` (the values before a change) makes changed/changed_to work;
        `about` is the record {{record.x}} in the values means, when that isn't the one checked (a statement's lines).
        A group {"any": [...]} holds when one of its conditions does; {"all": [...]} when every one does."""
        for c in conditions or []:
            if "any" in c or "all" in c:
                if not (any(self.matches(record, [x], old, about) for x in c["any"]) if "any" in c else self.matches(record, c["all"], old, about)):
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
                if ok and op == "changed_to":
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

    def query(self, table, filters=None, q=None, sort=None):
        """Records of a table (those this person may see) meeting the filters, matching the search, sorted."""
        rows = self.visible_rows(table)
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
        if kind == "location":
            if isinstance(raw, dict):
                lat, lng = to_number(raw.get("lat", raw.get("latitude"))), to_number(raw.get("lng", raw.get("lon", raw.get("longitude"))))
            else:
                numbers = re.findall(r"-?\d+(?:\.\d+)?", str(raw))
                lat, lng = (to_number(numbers[0]), to_number(numbers[1])) if len(numbers) >= 2 else (None, None)
            if lat is None or lng is None or not -90 <= lat <= 90 or not -180 <= lng <= 180:
                raise ValueError(f"“{raw}” isn't a location (give latitude, longitude like -31.95, 115.86)")
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
            if isinstance(raw, str) and raw.strip().lower() in ("today", "now"):
                return self.today.isoformat()
            d = to_date(raw)
            if d is None:
                raise ValueError(f"“{raw}” isn't a date (try 2026-11-30 or 30/11/2026)")
            return d.isoformat()
        if kind == "datetime":
            if isinstance(raw, str) and raw.strip().lower() == "now":
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
        text = str(raw).strip().lower()
        rows = self.rows(table)
        exact = [r for r in rows if self.title(r).lower() == text]
        if len(exact) == 1:
            return exact[0]
        partial = [r for r in rows if text and text in self.title(r).lower()]
        return partial[0] if len(partial) == 1 else (exact[0] if exact else None)

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

    def _clean(self, t, data, creating, record=None):
        values, errors = {}, {}
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
                try:
                    default = f["default"]
                    if isinstance(default, str) and "{{" in default:
                        default = expr.render(default, RecordContext(self, record))
                    values[f["key"]] = self.coerce(f, default)
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
                clash = next((r for r in self.rows(t["key"]) if (record is None or r.id != record.id)
                              and str((r.data or {}).get(key, "")).strip().lower() == str(value).strip().lower()), None)
                if clash is not None:
                    errors[key] = f"{self.title(clash)} already has this {f['label']}"
        for group in t.get("unique_together", []):  # a supplier's invoice number, once
            if not set(group) & set(values) or any(merged.get(k) in (None, "", []) for k in group):
                continue
            same = lambda r: all(str((r.data or {}).get(k, "")).strip().lower() == str(merged.get(k)).strip().lower() for k in group)  # noqa: E731
            clash = next((r for r in self.rows(t["key"]) if (record is None or r.id != record.id) and same(r)), None)
            if clash is not None:
                labels = [self.field(t["key"], k)["label"] for k in group if self.field(t["key"], k)]
                errors[group[-1]] = f"{t['singular']} “{self.title(clash)}” already has this {' and '.join(labels).lower()}"
        if flow and flow["field"] in values and flow["field"] not in errors and (self.role is not None or self.portal_contact is not None):
            self._check_move(t, record, draft, values[flow["field"]], creating, errors)
        if errors:
            raise FieldErrors(errors)
        return values

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

    def is_who(self, who, record):
        """Whether this person is `who` for a record: has the role, is in the team, added the record, or is named on it
        (in a person field, or one through its links: the cost centre's manager)."""
        if self.role is None:
            return True
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
        """The fields a move needs filled in before it's made (its button asks for them): [field]."""
        return [f for f in (self.field(t["key"], k) for k in move.get("requires", []))  # a file field's files aren't in the data
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
        moved = self.update(record, {key: found[0]["to"], **self._stamps(record, found[0]["to"])}, by=by)
        if schema.says_no(found[0]["to"]) and self.waiting_for(record):
            from . import automations
            automations.withdraw(self, record, by or self.by)
        return moved

    def _stamps(self, record, stage):
        """What a stage's own fields say that nobody has filled in: who moved it there ("Approved by") and when
        ("Approved on"), so a button press records them as the person would have."""
        t, word = self.tables[record.table], re.sub(r"\W+", "_", str(stage).strip().lower()).strip("_")
        out = {}
        for f in t["fields"] if word else []:
            if f["type"] not in schema.STORED or (record.data or {}).get(f["key"]) not in (None, "", []):
                continue
            names = {f["key"].lower(), re.sub(r"\W+", "_", f["label"].strip().lower()).strip("_")}
            if f["type"] == "user" and f"{word}_by" in names and self.user_id:
                out[f["key"]] = [self.user_id] if f.get("multiple") else self.user_id
            elif self.kind(f) in ("date", "datetime") and names & {f"{word}_on", f"{word}_at", f"{word}_date", f"date_{word}"}:
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
            moved = db.session.execute(sql('UPDATE counter SET value = MAX(value, :n) WHERE workspace_id = :w AND "table" = :t AND field = :f'), params)
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
        indexing start, once the whole block is saved."""
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

    def _abandon(self):
        db.session.rollback()
        self._touched, self._after, self._failed, self._open = set(), [], False, set()
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
            db.session.add(RecordChange(workspace_id=self.ws.id, record_id=other.id, kind="updated", by=f"{by or self.by} (linked from its {f['label'].lower()})",
                                        by_id=self.user_id, changes={back[0]["key"]: [None, record.id]}))

    def _saved(self, record, after=None):
        """A write is ready: commit it now, or at the end of the atomic block it's part of."""
        self._touched.add(record.id)
        if after:
            self._after.append(after)
        if self._depth:
            try:
                db.session.flush()
            except StaleDataError as exc:
                self._failed = True  # the whole block is refused when it ends, even if this is caught inside it
                raise Conflict() from exc
            self.reset()
            return
        self._finish()

    def _finish(self):
        try:
            db.session.flush()
            self.reset()
            self._check_rules()
            db.session.commit()
        except StaleDataError as exc:
            self._abandon()
            raise Conflict() from exc
        except BaseException:
            self._abandon()
            raise
        touched, after = self._touched, self._after
        self._touched, self._after, self._open = set(), [], set()
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

    def _check_rules(self):
        """Every rule of every record just written (and of the records whose lines were written) must hold, and what was
        approved stays as approved (see _held)."""
        seen = set()
        for rid in sorted(self._touched):
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
                        ok = to_bool(expr.evaluate(schema.percents_fixed(t, rule["check"]), RecordContext(self, target)))
                    except expr.ExprError as exc:
                        log.warning("The rule %r on %s couldn't be worked out: %s", rule["check"], t.get("key"), exc)
                        continue
                    if not ok:
                        where = rule.get("field") if target is r and rule.get("field") else "_"
                        prefix = "" if target is r else f"{t['singular']} “{self.title(target)}”: "
                        raise FieldErrors({where: prefix + rule["message"]})

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

    def _check_lock(self, record, changing):
        sealed = self._sealed_lock(record) if record is not None else None
        reason = sealed[0] if sealed else self.locked(record)
        if reason is None:
            if self._depth and record is not None:
                self._open.add(record.id)
            return
        t = self.tables.get(record.table) or {}
        allowed = sealed[1] if sealed else set(t.get("lock_except") or []) if not t.get("embedded") else set()
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

    def create(self, table, data, *, by=None, automations=True, depth=0, ai=True, keep_numbers=False):
        """keep_numbers: an autonumber given in `data` is kept (past invoices brought in keep theirs) and new records
        carry on after it."""
        t = self.tables.get(table)
        if t is None:
            raise FieldErrors({"_": f"there's no table “{table}”"})
        if self.role is not None and not self.can(t, "edit"):
            raise FieldErrors({"_": f"Your role can't add {schema.lc(t['label'])}."})
        values = self._clean(t, data or {}, creating=True)
        for f in self._mine_when_blank(t):
            if values.get(f["key"]) in (None, "", []):
                values[f["key"]] = [self.user_id] if f.get("multiple") else self.user_id
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
        self._saved(record, after)
        return record

    def update(self, record, data, *, by=None, automations=True, depth=0, ai_filled=False, expected_version=None):
        """Change a record. ai_filled: the AI wrote these values (it may rewrite them later); otherwise a person or a rule
        did, and the AI leaves those fields alone from now on. expected_version: the version the person was looking at;
        if the record has changed since, the save is refused (Conflict)."""
        if expected_version not in (None, "") and str(expected_version) != str(record.version):
            raise Conflict()
        t = self.tables.get(record.table)
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
        self._check_lock(record, list(diff))
        if t.get("embedded"):  # a line moved to another record mustn't land in one that's locked
            link = next((f for f in t["fields"] if f["type"] == "link" and f["key"] in diff and not (self.tables.get(f["table"]) or {}).get("embedded")), None)
            if link is not None:
                self._check_lock(self.get(values[link["key"]], link["table"]), None)
        owned = set(old.get("_ai") or [])
        owned = owned | set(diff) if ai_filled else owned - set(diff)
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
        if self.role is not None and not self.can(t, "edit"):
            raise FieldErrors({"_": f"Your role can't delete {schema.lc(t['label'])}."})
        self._check_lock(record, None)
        record.deleted_at = utcnow()
        db.session.add(RecordChange(workspace_id=self.ws.id, record_id=record.id, kind="deleted", by=by or self.by, by_id=self.user_id))

        def after():
            if automations:
                from . import automations as auto
                auto.on_delete(self, record, depth=depth)
        self._saved(record, after)

    def restore(self, record, *, by=None):
        record.deleted_at = None
        db.session.add(RecordChange(workspace_id=self.ws.id, record_id=record.id, kind="restored", by=by or self.by))
        self._saved(record)

    def counts(self):
        rows = (db.session.query(Record.table, db.func.count(Record.id))
                .filter(Record.workspace_id == self.ws.id, Record.deleted_at.is_(None)).group_by(Record.table).all())
        return {table: n for table, n in rows if table in self.tables}
