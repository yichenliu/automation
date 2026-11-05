"""Queries in SQL: a list's filters, search, order, pages, counts and totals worked out by the database, so a list of a
million invoices isn't loaded into Python to show fifty of them.

Engine.matches stays the definition of what a condition means, and the planner never guesses at it. Each condition
becomes two SQL tests:
- `yes`: the records it certainly holds for;
- `maybe`: every record it could hold for: `yes`, and the records SQL can't decide (a number kept as text after a field
  changed kind, a date written another way, a list where one value belongs).
For the values SQL tells apart by type alone (nothing, "", an empty list, true, false) the answer is Engine.matches' own,
asked once about a record holding just that value, so SQL never re-implements those rules.

A query reads the records `maybe` finds, and checks in Python only those `yes` doesn't settle. When every record found is
settled (the usual case: data saved through the engine), the page, the count and the totals come straight from SQL
(LIMIT/OFFSET, COUNT, SUM); when some aren't, the records found are checked and sorted in Python exactly as before. What
SQL can't read (formulas, rollups, lookups, a field through a link, a value worked out from the record itself) is
checked in Python on the records found ("residual").

Everything here runs on PostgreSQL and on the simulator's SQLite. JSON is read with SQLAlchemy's accessors and each
database's own type test (jsonb_typeof, json_type); only PostgreSQL uses containment (data @> ...), where its GIN index
helps. Where SQLite can't read text as Python does (its lower() folds only A-Z), those records are left for Python there.
"""
import json
import math
import re
from datetime import date, datetime, timedelta

from sqlalchemy import Float, Numeric, String, and_, case, cast, false, func, literal, literal_column, not_, or_, select, true
from sqlalchemy.dialects.postgresql import JSONB

from ..blueprint import expr, schema
from ..blueprint.expr import to_date, to_datetime, to_number
from ..extensions import db
from ..models import Record

WHITESPACE = ("\t\n\x0b\x0c\r\x1c\x1d\x1e\x1f \x85\xa0            "
              "    　")  # what Python's strip() takes off, so SQL's trim takes off the same
TRUTHY = ("true", "yes", "y", "1", "checked", "on", "done")  # expr.to_bool's words for yes
KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")  # a field key that can go in a JSON path as it is
TEXTLIKE = {"text", "long_text", "rich_text", "email", "phone", "url", "address", "barcode", "color", "time", "select", "autonumber"}
PLAIN = {"text", "long_text", "email", "phone", "url", "barcode", "color", "select", "autonumber"}  # shown just as kept
NUMERIC = {"number", "money", "percent", "duration", "rating"}
SEARCHED = {"text", "long_text", "rich_text", "email", "phone", "select", "address", "barcode", "multi_select", "user", "autonumber",
            "link", "url", "lookup"}  # as Engine.search
COMPARE = {"greater_than": "__gt__", "less_than": "__lt__", "at_least": "__ge__", "at_most": "__le__"}
COMPARE_AST = {"greater_than": expr.ast.Gt(), "less_than": expr.ast.Lt(), "at_least": expr.ast.GtE(), "at_most": expr.ast.LtE()}
RELATIVE = {"before_today", "after_today", "within_next_days", "within_last_days", "more_than_days_ago", *schema.PERIOD_OPS}
CONSTANT = {"none": None, "": "", "[]": [], "true": True, "false": False}  # values told apart by type alone, as kept
KINDS = (*CONSTANT, "number", "text", "list", "object")  # and those whose value matters: every kind of value there is
NEAR = 1000  # parents whose lines are fetched together at most; more than that and the line table is read whole
UNSURE = object()  # SQL can't tell for this kind of value: the record is checked in Python


# --- tests that are true or false ----------------------------------------------------------------------------------------
def _all(*xs):
    """AND, with Python's True and False folded away (a plan with nothing to test adds nothing to the SQL)."""
    xs = [x for x in xs if x is not True]
    if any(x is False for x in xs):
        return False
    return True if not xs else xs[0] if len(xs) == 1 else and_(*xs)


def _any(*xs):
    xs = [x for x in xs if x is not False]
    if any(x is True for x in xs):
        return True
    return False if not xs else xs[0] if len(xs) == 1 else or_(*xs)


def _not(x):
    return (not x) if isinstance(x, bool) else not_(x)


def _sure(x):
    """A test that is true or false, never NULL: SQL's comparisons with a missing value are NULL, and NOT NULL is NULL,
    where Python's `not` would say True."""
    return x if isinstance(x, bool) else case((x, true()), else_=false())


def _sql(x):
    return true() if x is True else false() if x is False else x


def _either(outcomes):
    """One kind of value against several wanted ones (one_of): it matches when it matches any of them."""
    if any(o is True for o in outcomes):
        return True
    yes, unsure = [], []
    for o in outcomes:
        if o is UNSURE:
            unsure.append(True)
        elif o is not False:
            yes.append(o[0])
            unsure.append(o[1])
    y, u = _any(*yes), _any(*unsure)
    if y is False and u is False:
        return False
    return UNSURE if y is False and u is True else (y, u)


# --- JSON values as SQL reads them -----------------------------------------------------------------------------------------
class Scalar:
    """A JSON value as SQL reads it: what a record keeps under a key, or one item of a list it keeps. `kind` is the
    database's own name for its type (jsonb_typeof on PostgreSQL, json_type in SQLite), and "missing" when there's
    nothing under the key: never NULL, so every test on it is true or false and NOT means what Python's `not` means."""

    def __init__(self, pg, kind, text, number, length=None, node=None, path=None):
        self.pg, self.kind, self.text, self._number, self._length, self.node, self.path = pg, kind, text, number, length, node, path

    @classmethod
    def field(cls, pg, key):
        node = Record.data[key]
        if pg:
            return cls(pg, func.coalesce(func.jsonb_typeof(node), "missing"), node.as_string(), node.as_float(),
                       func.jsonb_array_length(node), node)
        path = f'$."{key}"'
        return cls(pg, func.coalesce(func.json_type(Record.data, path), "missing"), node.as_string(), node.as_float(),
                   func.json_array_length(Record.data, path), node, path)

    def missing(self):
        return self.kind == "missing"

    def null(self):
        return self.kind == "null"

    def none(self):
        return _any(self.missing(), self.null())

    def string(self):
        return self.kind == ("string" if self.pg else "text")

    def number(self):
        return self.kind == "number" if self.pg else self.kind.in_(("integer", "real"))

    def integer(self):
        """A whole number written as one (12, not 12.0), as a link or a person is kept."""
        return and_(self.kind == "number", self.text.op("~")(r"^-?[0-9]+$")) if self.pg else self.kind == "integer"

    def true(self):
        return and_(self.kind == "boolean", self.text == "true") if self.pg else self.kind == "true"

    def false(self):
        return and_(self.kind == "boolean", self.text == "false") if self.pg else self.kind == "false"

    def boolean(self):
        return self.kind == "boolean" if self.pg else self.kind.in_(("true", "false"))

    def array(self):
        return self.kind == "array"

    def object(self):
        return self.kind == "object"

    @property
    def num(self):
        """Its number, read only when it is one (PostgreSQL would refuse to read "abc" as a number)."""
        return case((self.number(), self._number))

    def length(self):
        return case((self.array(), self._length), else_=-1)

    def low(self):
        return func.lower(self.text)

    def norm(self):
        """As Python's s.strip().lower() reads it."""
        return func.lower(func.btrim(self.text, WHITESPACE) if self.pg else func.trim(self.text, WHITESPACE))

    def ordered(self, x):
        """Text compared as Python compares it: by character, not by the database's language rules."""
        return x.collate("C") if self.pg else x

    def foreign(self):
        """Text with letters SQL may not fold as Python does: SQLite's lower() folds only A-Z, so text with any other
        letter is left for Python there. PostgreSQL's lower() folds them as Python does."""
        return False if self.pg else self.text.op("GLOB")("*[^\x01-\x7f]*")

    def iso_date(self):
        """A date kept as the engine keeps it (2026-11-30, and a real day): in order as text, as Python reads it."""
        if self.pg:
            return _sure(and_(self.string(), self.text.op("~")(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"), func.pg_input_is_valid(self.text, "date")))
        return _sure(and_(self.string(), func.date(self.text) == self.text, self.text >= "0001-01-01"))  # date() is NULL for no date

    def iso_datetime(self):
        """A date and time kept as the engine keeps it (2026-11-30T14:30)."""
        if self.pg:
            return _sure(and_(self.string(), self.text.op("~")(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T([01][0-9]|2[0-3]):[0-5][0-9]$"),
                              func.pg_input_is_valid(self.text, "timestamp")))
        return _sure(and_(self.string(), func.strftime("%Y-%m-%dT%H:%M", self.text) == self.text, self.text >= "0001-01-01",
                          func.substr(self.text, 12, 2) < "24"))

    def items(self):
        """(the items of the list kept here, to look through with EXISTS, and one item as a Scalar). Anything but a list
        reads as an empty one (PostgreSQL refuses to take a single value apart)."""
        if self.pg:
            listed = case((self.array(), self.node), else_=cast(literal("[]"), JSONB))
            each = func.jsonb_array_elements(listed).table_valued("value").alias()
            value = each.c.value
            text = value.op("#>>", return_type=String)(literal_column("'{}'"))
            kind = func.jsonb_typeof(value)
            return each, Scalar(True, kind, text, cast(text, Float))
        listed = case((self.array(), func.json_extract(Record.data, self.path)), else_="[]")
        each = func.json_each(listed).table_valued("value", "type").alias()
        return each, Scalar(False, each.c.type, each.c.value, each.c.value)

    def some(self, test):
        """Whether any item of the list kept here passes `test` (a function of one item)."""
        each, item = self.items()
        cond = test(item)
        if cond is False:
            return False
        return select(literal(1)).select_from(each).where(_sql(cond)).exists()

    def by_kind(self, of):
        """One test made of a test for each kind of value (KINDS): CASE on the value's type, so the type is read once a
        record rather than once for each kind (on a list of a million, that's most of the work)."""
        if all(of[k] is True for k in KINDS) or all(of[k] is False for k in KINDS):
            return of["none"]

        def split(test, a, b):
            return a if a is b else case((test, _sql(a)), else_=_sql(b))
        text, listed = split(self.text == "", of[""], of["text"]), split(self._length == 0, of["[]"], of["list"])
        if self.pg:
            whens = {"missing": of["none"], "null": of["none"], "string": text, "number": of["number"],
                     "boolean": split(self.text == "true", of["true"], of["false"]), "array": listed, "object": of["object"]}
        else:
            whens = {"missing": of["none"], "null": of["none"], "text": text, "integer": of["number"], "real": of["number"],
                     "true": of["true"], "false": of["false"], "array": listed, "object": of["object"]}
        return _sure(case({k: _sql(x) for k, x in whens.items()}, value=self.kind, else_=false()))


def combine(v, outcomes):
    """(yes, maybe) from what each kind of value gives: True, False, UNSURE, or (yes, unsure) tests for that kind."""
    yes, maybe = {}, {}
    for kind in KINDS:
        out = outcomes.get(kind, False)
        if out is UNSURE:
            yes[kind], maybe[kind] = False, True
        elif out is True or out is False:
            yes[kind] = maybe[kind] = out
        else:
            yes[kind], maybe[kind] = out[0], _any(out[0], out[1])
    return v.by_kind(yes), v.by_kind(maybe)


class Planner:
    """One table's conditions, search, access and order as SQL, for one engine (one person's view of one workspace)."""

    def __init__(self, engine, table):
        self.e, self.table = engine, table
        self.t = engine.tables.get(table)
        self.pg = db.session.get_bind().dialect.name == "postgresql"

    def usable(self):
        """Whether SQL can read this table's records as the engine shows them: a sealed bid's values are hidden from all
        but its maker until it opens, which only Python knows."""
        t = self.t
        if t is None or t.get("sealed"):
            return False
        return not (t.get("embedded") and any((self.e.tables.get(f.get("table")) or {}).get("sealed") for f in t["fields"] if f["type"] == "link"))

    def value(self, key):
        return Scalar.field(self.pg, key)

    def probe(self, c, kind, value=None):
        """What Engine.matches says about a record keeping just this kind of value (or nothing) under c's field."""
        data = {} if kind == "none" else {c["field"]: CONSTANT[kind] if kind in CONSTANT else value}
        r = Record(workspace_id=self.e.ws.id, table=self.table, data=data)
        try:
            return bool(self.e.matches(r, [c]))
        except Exception:  # noqa: BLE001 (what Python can't decide is left for it: those records are checked as before)
            return UNSURE
        finally:
            for k in (id(r), ("hidden", id(r))):  # a passing record's id may be reused by another: nothing kept under it
                self.e._seals.pop(k, None)

    def constants(self, c):
        return {kind: self.probe(c, kind) for kind in CONSTANT}

    # --- conditions ------------------------------------------------------------------------------------------------------
    def condition(self, c):
        """(yes, maybe) for one condition (or a group of them), or None when it's left for Python."""
        if not isinstance(c, dict) or "formula" in c:  # a formula: worked out in Python
            return None
        if "any" in c or "all" in c:
            parts = [self.condition(x) for x in (c["any"] if "any" in c else c["all"]) or []]
            if any(p is None for p in parts):
                return None
            join = _any if "any" in c else _all
            return join(*[p[0] for p in parts]), join(*[p[1] for p in parts])
        key, op = c.get("field"), c.get("op")
        if not isinstance(key, str) or not KEY.match(key) or not isinstance(op, str):
            return None
        f = self.e.field(self.table, key)
        if f is None or f["type"] not in schema.STORED or f.get("sensitive") or (f["type"] == "url" and f.get("template")):
            return None
        if self._from_record(c.get("value"), f):
            return None
        wanted = self.e._target(None, c.get("value"), f)
        if self.e.kind(f) == "percent" and op in ("equals", "not_equals", *COMPARE):
            wanted = schema.as_fraction(wanted)  # as Engine.matches does
        v = self.value(key)
        negate = op in ("not_equals", "not_empty")
        positive = {**c, "op": {"not_equals": "equals", "not_empty": "is_empty"}.get(op, op)}
        if op in ("is_empty", "not_empty"):
            outcomes = self._empty(positive, f, v)
        elif op in ("equals", "not_equals", "one_of"):
            outcomes = self._equals(positive, f, v, wanted if op == "one_of" and isinstance(wanted, list) else [wanted])
        elif op in COMPARE:
            outcomes = self._compare(c, f, v, op, wanted)
        elif op == "contains":
            outcomes = self._contains(c, f, v, wanted)
        elif op in RELATIVE:
            outcomes = self._relative(c, f, v, op, wanted)
        else:
            return None  # changed / changed_to: about a change, never a list
        if outcomes is None:
            return None
        yes, maybe = combine(v, outcomes)
        return (_not(maybe), _not(yes)) if negate else (yes, maybe)

    def _from_record(self, value, f):
        """Whether a condition's value is worked out from each record ({{record.budget}}, "budget * 0.9", another of its
        fields named bare), as Engine._target reads it: then it's checked in Python, record by record."""
        if not isinstance(value, str):
            return False
        if f["type"] == "user" and value.strip().lower() in ("me", "{{me}}", "current_user", "{{user}}"):
            return False
        if "{{" in value:
            return True
        if re.search(r"[-+*/]", value) and re.fullmatch(r"[\w\s.+\-*/()]+", value):
            names = re.findall(r"[A-Za-z_]\w*", value)
            if names and all(schema.field(self.t, n) for n in names):
                return True
        return self.e._names_field(self.table, value, f)

    def _empty(self, c, f, v):
        out = self.constants(c)
        if f["type"] == "checkbox":  # an unticked box is empty: 0, "no" and the like too
            out.update(number=(v.num == 0, False), text=(_not(v.norm().in_(TRUTHY)), v.foreign()), list=self.probe(c, "list", ["x"]),
                       object=UNSURE)
        else:
            out.update(number=self.probe(c, "number", 1), text=self.probe(c, "text", "x"), list=self.probe(c, "list", ["x"]),
                       object=self.probe(c, "object", {"x": 1}))
        return out

    def _equals(self, c, f, v, targets):
        kind = f["type"]
        out = self.constants(c)
        if kind in TEXTLIKE:
            each = lambda w: {"number": self._eq_number(v, w), "text": self._eq_text(v, w),  # noqa: E731
                              "list": UNSURE if isinstance(w, (bool, list, dict)) else False, "object": UNSURE}
        elif kind in NUMERIC:
            each = lambda w: {"number": self._eq_number(v, w), "text": UNSURE,  # noqa: E731
                              "list": self._as_none(w), "object": self._as_none(w)}
        elif kind == "date":
            each = lambda w: {"number": UNSURE, "text": self._eq_date(v, w), "list": self._as_none(w), "object": self._as_none(w)}  # noqa: E731
        elif kind == "datetime":
            each = lambda w: {"number": UNSURE, "text": self._eq_datetime(v, w), "list": self._as_none(w), "object": self._as_none(w)}  # noqa: E731
        elif kind == "checkbox":
            each = lambda w: self._eq_checkbox(v, w)  # noqa: E731
        elif kind == "multi_select":
            each = lambda w: {"number": UNSURE, "text": UNSURE, "object": UNSURE, "list": self._eq_choices(v, w)}  # noqa: E731
        elif kind == "tags":
            each = lambda w: {k: UNSURE if isinstance(w, (list, dict)) else expr._compare(expr.ast.Eq(), ["x"], w)  # noqa: E731
                              for k in ("number", "text", "list", "object")}
        elif kind == "user":
            each = lambda w: self._eq_person(v, w)  # noqa: E731
        elif kind == "link":
            each = lambda w: self._eq_link(v, f, w)  # noqa: E731
        else:
            return None  # a place, JSON: left for Python
        per = [each(w) for w in targets]
        for k in ("number", "text", "list", "object"):
            out[k] = _either([p[k] for p in per])
        return out

    @staticmethod
    def _as_none(w):
        """A list or an object in a number or date field reads as nothing there: what nothing equals."""
        return expr._compare(expr.ast.Eq(), None, w)

    @staticmethod
    def _eq_number(v, w):
        """A kept number against a wanted value, as expr._compare reads them (12 equals "12" and 12.0)."""
        if w is None:
            return False
        if isinstance(w, (bool, date, list, dict)):
            return UNSURE
        n = to_number(w)
        return False if n is None else (v.num == n, False)

    @staticmethod
    def _eq_text(v, w):
        if w is None:
            return False
        if isinstance(w, str):
            return v.norm() == w.strip().lower(), v.foreign()
        return UNSURE

    @staticmethod
    def _eq_date(v, w):
        iso = v.iso_date()
        if isinstance(w, datetime) or not (w is None or isinstance(w, (str, date))):
            return UNSURE
        wanted = to_date(w) if w is not None else None
        return (_all(iso, v.text == wanted.isoformat()) if wanted else False), _not(iso)

    @staticmethod
    def _eq_datetime(v, w):
        iso = v.iso_datetime()
        if w is None:
            return False, _not(iso)
        if isinstance(w, datetime) or (isinstance(w, str) and len(w.strip()) != 10):
            wanted = to_datetime(w)
            return (_all(iso, v.text == wanted.isoformat(timespec="minutes")) if wanted else False), _not(iso)
        if isinstance(w, (str, date)):  # a day: an event at 6 pm is on the day asked about
            wanted = to_date(w)
            return (_all(iso, func.substr(v.text, 1, 10) == wanted.isoformat()) if wanted else False), _not(iso)
        return UNSURE

    @staticmethod
    def _eq_checkbox(v, w):
        if isinstance(w, date):
            return {k: UNSURE for k in ("number", "text", "list", "object")}
        ticked = expr.to_bool(w)
        truthy = v.norm().in_(TRUTHY)
        return {"number": (v.num != 0 if ticked else v.num == 0, False), "text": (truthy if ticked else _not(truthy), v.foreign()),
                "list": ticked, "object": UNSURE}

    @staticmethod
    def _eq_choices(v, w):
        if w in (None, ""):
            return False
        wanted = str(w).strip().lower()
        return (v.some(lambda i: and_(i.string(), func.lower(i.text) == wanted)),
                _any(v.some(lambda i: _not(i.string())), v.some(lambda i: _all(i.string(), i.foreign()))))

    def _people(self, w):
        """The ids a person field's wanted value names (Engine._equal), or None when it could be anyone (a blank name is
        every former member's)."""
        if isinstance(w, int) or str(w).isdigit():
            return {int(w)}
        said = str(w).strip().lower()
        if said == "":
            return None
        members, emails = self.e.members(), self.e._emails
        ids = {uid for uid, name in members.items() if name.lower() == said or (emails.get(uid) or "").lower() == said}
        if re.fullmatch(r"-?\d+", said):
            ids.add(int(said))
        return ids

    def _eq_person(self, v, w):
        ids = self._people(w)
        if ids is None:
            return {k: UNSURE for k in ("number", "text", "list", "object")}
        found = sorted(ids)
        whole = lambda x: x.num == func.round(x.num)  # noqa: E731 (int() would cut 5.5 to 5: left for Python)
        one = (x_in(v, found), _not(whole(v)))
        many = (_all(_not(v.some(lambda i: _not(i.number()))), v.some(lambda i: x_in(i, found))),
                v.some(lambda i: _any(_not(i.number()), _not(whole(i)))))
        return {"number": one, "text": UNSURE, "list": many, "object": UNSURE}

    def _eq_link(self, v, f, w):
        said = str(w).strip()
        ids = set(self.e._titled_ids(f["table"], said.lower()))
        if re.fullmatch(r"-?\d+", said) and str(int(said)) == said:
            ids.add(int(said))
        found = sorted(ids)
        return {"number": (_all(v.integer(), x_in(v, found)), _not(v.integer())), "text": UNSURE, "object": UNSURE,
                "list": (v.some(lambda i: _all(i.integer(), x_in(i, found))), v.some(lambda i: _not(i.integer())))}

    def _compare(self, c, f, v, op, w):
        kind = f["type"]
        out = self.constants(c)
        go = COMPARE[op]
        if kind in NUMERIC or kind in TEXTLIKE:
            if isinstance(w, (bool, date, list, dict)):
                out["number"] = UNSURE
            else:
                n = to_number(w)
                out["number"] = False if n is None else (getattr(v.num, go)(n), False)
        if kind in NUMERIC:
            out.update(text=UNSURE, list=False, object=False)  # a list or object is no number: nothing to compare
        elif kind in TEXTLIKE:
            if isinstance(w, str):
                out["text"] = getattr(v.ordered(v.norm()), go)(w.strip().lower()), v.foreign()
            else:
                out["text"] = False if w is None else UNSURE
            out["list"] = expr._compare(COMPARE_AST[op], ["x"], w)
            out["object"] = UNSURE if isinstance(w, bool) else expr._compare(COMPARE_AST[op], {"x": 1}, w)
        elif kind in ("date", "datetime"):
            iso = v.iso_date() if kind == "date" else v.iso_datetime()
            wanted = to_date(w) if kind == "date" else to_datetime(w)
            shown = None if wanted is None else wanted.isoformat() if kind == "date" else wanted.isoformat(timespec="minutes")
            out.update(number=UNSURE, list=False, object=False,
                       text=((_all(iso, getattr(v.text, go)(shown)) if shown else False), _not(iso)))
        else:
            return None
        return out

    def _contains(self, c, f, v, wanted):
        if f["type"] not in PLAIN or f.get("before") or f.get("after"):
            return None  # shown differently from how it's kept (a time as 2:30 pm, a address on one line): Python
        said = str(wanted or "").lower()
        out = self.constants(c)
        if not said:
            out.update(number=True, text=True, list=True, object=True)
        else:
            out.update(number=UNSURE, list=UNSURE, object=UNSURE, text=(v.low().contains(said, autoescape=True), v.foreign()))
        return out

    def _relative(self, c, f, v, op, wanted):
        if f["type"] not in ("date", "datetime"):
            return None
        span = self._span(op, wanted)
        if span is None:
            return None
        out = self.constants(c)
        iso = v.iso_date() if f["type"] == "date" else v.iso_datetime()
        if span is False:
            text = False
        else:
            day = func.substr(v.text, 1, 10)
            lo, hi = span
            text = _all(iso, day >= lo.isoformat() if lo else True, day <= hi.isoformat() if hi else True)
        out.update(number=UNSURE, list=False, object=False, text=(text, _not(iso)))
        return out

    def _span(self, op, wanted):
        """The days a relative condition takes in (first, last; None for open), False for none, None if it can't say."""
        today, n = self.e.today, to_number(wanted) or 0
        try:
            if op == "before_today":
                return None, today - timedelta(days=1)
            if op == "after_today":
                return today + timedelta(days=1), None
            if op == "within_next_days":  # 0 <= days ahead <= n
                return (today, today + timedelta(days=math.floor(n))) if n >= 0 else False
            if op == "within_last_days":
                return (today - timedelta(days=math.floor(n)), today) if n >= 0 else False
            if op == "more_than_days_ago":  # days ago > n
                return None, today - timedelta(days=math.floor(n) + 1)
            found = schema.period(op, today, self.e.fy_start)
            return found if found else None
        except (OverflowError, ValueError):
            return None

    # --- search ----------------------------------------------------------------------------------------------------------
    def search(self, q):
        """(yes, maybe) for Engine.search: a record matches when a field it shows contains what was typed."""
        said = (q or "").strip().lower()
        if not said:
            return True, True
        parts = [self._found(f, said) for f in self.t["fields"] if f["type"] in SEARCHED and self.e.sees(f)]
        return _any(*[p[0] for p in parts]), _any(*[p[1] for p in parts])

    def _found(self, f, said):
        key = f["key"]
        if f["type"] == "lookup":
            return self._found_lookup(f, said)
        if not KEY.match(key) or f.get("sensitive") or (f["type"] == "url" and f.get("template")):
            return False, True
        v = self.value(key)
        out = {}
        for kind, raw in CONSTANT.items():
            try:
                out[kind] = said in self.e.display(f, raw).lower()
            except Exception:  # noqa: BLE001
                out[kind] = UNSURE
        unsure = dict(number=UNSURE, text=UNSURE, list=UNSURE, object=UNSURE)
        out.update(unsure)
        kind = f["type"]
        if f.get("before") or f.get("after"):
            pass  # shown with words around it: left for Python
        elif kind in PLAIN or (kind == "address" and "," not in said and len(said.splitlines()) == 1):
            out["text"] = v.low().contains(said, autoescape=True), v.foreign()
        elif kind == "rich_text":  # shown without its markup, so only a hint: anything that has the words, or markup
            marks = [v.text.contains(m, autoescape=True) for m in ("#", "*", "_", "`", ">", "[", "]", "(http")]
            out["text"] = False, _any(v.low().contains(said, autoescape=True), *marks)
        elif kind == "multi_select":
            out["text"] = v.low().contains(said, autoescape=True), v.foreign()
            out["list"] = (v.some(lambda i: and_(i.string(), func.lower(i.text).contains(said, autoescape=True))),
                           _any("," in said, v.some(lambda i: _not(i.string())), v.some(lambda i: _all(i.string(), i.foreign()))))
        elif kind == "user":
            members = self.e.members()
            named = sorted(uid for uid, name in members.items() if said in name.lower())
            gone = said in "former member"  # who isn't a member any more is shown as that
            counted = _all(v.integer(), v.num >= 0)
            out["number"] = (_all(counted, _any(x_in(v, named), _all(gone, _not(x_in(v, sorted(members)))))), _not(counted))
        elif kind == "link":
            if said not in "—":
                found = sorted(self.e._titled_ids(f["table"], said, part=True))
                out["number"] = _all(v.integer(), x_in(v, found)), _not(v.integer())
                out["list"] = (v.some(lambda i: _all(i.integer(), x_in(i, found))),
                               _any("," in said, v.some(lambda i: _not(i.integer()))))
        return combine(v, out)

    def _found_lookup(self, f, said):
        """A lookup of a field through one link (an invoice's customer email): the records whose linked one shows it."""
        link = self.e.field(self.table, f.get("link") or "")
        if f.get("where") or "." in str(f.get("field")) or not link or link["type"] != "link" or link.get("multiple") \
                or not KEY.match(link["key"]) or said in "—" or link["table"] not in self.e.tables:
            return False, True
        shows = {r.id for r in self.e.rows(link["table"]) if said in self.e.display(f, self.e.value(r, f["field"])).lower()}
        v = self.value(link["key"])
        empty = _any(v.none(), and_(v.string(), v.text == ""), and_(v.array(), v.length() == 0))
        return _sure(_all(v.integer(), x_in(v, sorted(shows)))), _sure(_any(_all(v.integer(), x_in(v, sorted(shows))),
                                                                          _not(_any(v.integer(), empty))))

    def titled(self, said, part):
        """The records whose name may be `said` (or with `part`, may have it in it), as a superset Engine._titled_ids
        checks one by one: read by SQL when the name is a field shown as kept, else the whole table."""
        t = self.t
        key = schema.title_field(t) if t and t["fields"] else None
        f = schema.field(t, key) if key else None
        if not self.usable() or f is None or f["type"] not in PLAIN - {"autonumber"} or f.get("before") or f.get("after") \
                or f.get("sensitive") or not KEY.match(key):
            return self.e.rows(self.table)
        v = self.value(key)
        named = v.low().contains(said, autoescape=True) if part else v.low() == said
        nameless = v.text.in_(("", "—"))  # named "Supplier #12" instead
        test = _sure(_any(and_(v.string(), _any(named, v.foreign(), nameless)), _not(v.string())))
        return db.session.query(Record).filter(*live(self.e, self.table), test).order_by(Record.id).all()

    def coded(self, codes, wanted):
        """A test finding at least every record one of whose codes (a barcode, a number such as INV-0042) may read as
        `wanted`, as Engine.by_code reads them: kept as text, or shown with what goes before and after it."""
        tests = []
        for f in codes:
            if not KEY.match(f["key"]) or f.get("sensitive"):
                return True
            v = self.value(f["key"])
            before, after = (f.get("before") or "").lower(), (f.get("after") or "").lower()
            said = {wanted}
            if (before or after) and wanted.startswith(before) and wanted.endswith(after) and len(wanted) >= len(before) + len(after):
                said.add(wanted[len(before):len(wanted) - len(after)])  # what's kept, when it's shown as "SKU-" + it
            numbers = [n for n in (to_number(s) for s in said) if n is not None]
            tests.append(_any(and_(v.string(), _any(v.norm().in_(sorted(said)), v.low().in_(sorted(said)), v.foreign())),
                              _all(v.number(), v.num.in_(numbers)) if numbers else False,
                              v.boolean() if said & {"true", "false"} else False,
                              _any(v.array(), v.object()) if any(s[:1] in ("[", "{") and s for s in said) else False))
        return _sure(_any(*tests))

    # --- who sees which records ---------------------------------------------------------------------------------------
    def access(self):
        """(yes, maybe) for the records this person may see (Engine.visible_rows), or None when only Python can tell (a
        record that's theirs through a link or a lookup)."""
        e = self.e
        if e.role is None:
            return True, True
        if not e.can(self.t):
            return False, False
        owner = e._owner_rule(self.t)
        if owner is None:
            return True, True
        if owner == "created_by":
            mine = Record.created_by_id == e.user_id if e.user_id is not None else False
        else:
            f = e.field(self.table, owner)
            if f is None or f["type"] not in schema.STORED or f.get("sensitive") or not KEY.match(owner) \
                    or (f["type"] == "url" and f.get("template")):
                return None
            v, me = self.value(owner), str(e.user_id)  # Engine.allowed: str(value) == str(user_id), or in the list
            if self.pg:
                mine = _any(v.text == me, v.some(lambda i: i.text == me))
            else:
                kept = lambda x: _all(x.kind.in_(("text", "integer", "real")), cast(x.text, String) == me)  # noqa: E731 (true reads as 1)
                mine = _any(kept(v), v.some(kept))
        asked = sorted(e._asked())
        test = _sure(_any(Record.id.in_(asked) if asked else False, mine))
        return test, test

    # --- order ------------------------------------------------------------------------------------------------------------
    def order(self, sort):
        """(ORDER BY terms, a test for records SQL can't place) for Engine.sort, or None when only Python can sort."""
        if not sort:
            return [Record.id], False
        key, down = sort.get("field"), sort.get("dir") == "desc"
        if key in ("created_at", "updated_at", "id"):
            col = getattr(Record, key)
            return self._terms(col.is_(None), col, down), False
        f = self.e.field(self.table, key) if isinstance(key, str) else None
        if f is None:
            return [Record.id], False  # every value is None: they stay as they were
        if f["type"] not in schema.STORED or f.get("sensitive") or not KEY.match(key) or (f["type"] == "url" and f.get("template")):
            return None
        v, kind = self.value(key), self.e.kind(f)
        empty_text = and_(v.string(), v.text == "")
        if kind in ("number", "money", "percent"):  # to_number: nothing, "", lists and objects are None; true is 1
            value = case((v.number(), v.num), (v.true(), 1), (v.false(), 0))
            return self._terms(value.is_(None), value, down), _all(v.string(), v.text != "")
        if kind in ("duration", "rating", "user", "checkbox"):  # read as kept: numbers and true/false compare; text with them can't
            if kind == "user" and f.get("multiple"):
                return None
            value = case((v.number(), v.num), (v.true(), 1), (v.false(), 0))
            return self._terms(v.none(), value, down), _not(_any(v.none(), v.number(), v.boolean()))
        if kind in ("date", "datetime"):  # to_datetime: kept as the engine keeps them, they're in order as text
            iso = v.iso_date() if kind == "date" else v.iso_datetime()
            unread = _any(v.none(), empty_text, v.array(), v.object(), v.boolean())
            return self._terms(unread, case((iso, v.text)), down), _not(_any(iso, unread))
        if kind in TEXTLIKE:  # lower-case text; anything else among text can't be put in order with it (Python gives up)
            return self._terms(v.none(), v.ordered(case((v.string(), v.low()))), down), _not(_any(v.none(), _all(v.string(), _not(v.foreign()))))
        return None

    @staticmethod
    def _terms(unknown, value, down):
        """Python's key (value is None, value), reversed for descending, ties kept in the order they were (by id)."""
        last = case((unknown, 1), else_=0)
        return ([last.desc(), value.desc()] if down else [last, value]) + [Record.id]

    # --- the same value (unique checks and limits) -------------------------------------------------------------------
    def same(self, key, wanted):
        """A test finding at least every record whose value under `key` reads as `wanted` does, as text
        (str(value).strip().lower()): what a unique field, fields unique together and a limit compare. JSON null reads
        as "None" to one and "" to the other, so it's let through for either."""
        said = str(wanted if wanted is not None else "").strip().lower()
        v = self.value(key)
        n = to_number(said)
        return _sure(_any(
            and_(v.string(), _any(v.norm() == said, v.foreign())),
            _all(v.number(), v.num == n) if n is not None else False,
            v.boolean() if said in ("true", "false") else False,
            v.missing() if said == "" else False,
            v.null() if said in ("", "none") else False,
            _any(v.array(), v.object()) if said[:1] in ("[", "{") else False))


def x_in(v, ids):
    """Whether a kept number is one of these (none: never)."""
    return v.num.in_(ids) if ids else False


def live(engine, table):
    return [Record.workspace_id == engine.ws.id, Record.table == table, Record.deleted_at.is_(None)]


def children_of(engine, table, via, ids):
    """At least every live record of `table` whose link `via` points at one of these ids (as a number, as its digits, or
    in a list), in the order they were added. Engine.children sorts them under their parents.

    On PostgreSQL a few parents are found by containment (data @> {"order": 12}), which the GIN index answers; many are
    read from the table's records by their link's value. SQLite reads the link's value either way. A link kept as a
    number (12 or 12.0), as its digits ("12"), or as true (which Python counts as 1) is found, alone or in a list."""
    pg = db.session.get_bind().dialect.name == "postgresql"
    ids = sorted(ids)
    v = Scalar.field(pg, via)
    if pg and len(ids) <= 50:
        forms = [form for i in ids for form in (i, [i], str(i), [str(i)])] + ([True, [True]] if 1 in ids else [])
        test = or_(*[Record.data.op("@>")(cast(literal(json.dumps({via: form})), JSONB)) for form in forms])
    else:
        said = [str(i) for i in ids]
        points = lambda x: _any(_all(x.string(), x.text.in_(said)), _all(x.number(), x.num.in_(ids)),  # noqa: E731
                                x.true() if 1 in ids else False)
        test = _any(points(v), v.some(points))
    return db.session.query(Record).filter(*live(engine, table), _sql(test)).order_by(Record.id).all()




class Plan:
    """A query of one table (Engine.query's filters, search and order) and how to answer it: straight from SQL when every
    record it finds is settled, else by checking the records it finds in Python."""

    def __init__(self, engine, table, filters=None, q=None, sort=None, everyone=False):
        """everyone: every record counts, whoever's asking (a rule's count of bookings includes those this person can't
        see)."""
        self.e, self.table, self.filters, self.q, self.sort = engine, table, list(filters or []), q, sort
        self.everyone = everyone
        p = self.planner = Planner(engine, table)
        self.usable = p.usable()
        if not self.usable:
            return
        self.access = (True, True) if everyone else p.access()
        pushed, self.residual = [], []
        for c in self.filters:
            got = p.condition(c)
            if got is None:
                self.residual.append(c)
            else:
                pushed.append(got)
        self.yes_f, self.maybe_f = _all(*[y for y, _ in pushed]), _all(*[m for _, m in pushed])
        self.yes_q, self.maybe_q = p.search(q) if (q or "").strip() else (True, True)
        self.order = p.order(sort)
        seen = (self.access or (True, True))[1]
        self.where = live(engine, table) + [_sql(x) for x in (seen, self.maybe_f, self.maybe_q) if x is not True]

    @property
    def narrows(self):
        """Whether SQL leaves out any record at all (if not, reading the whole table, and keeping it, is no worse)."""
        return self.usable and len(self.where) > 3

    def _stats(self, sums=()):
        """(how many it finds, how many of those SQL didn't settle, how many SQL can't put in order). A page and its
        totals ask the same: what's found is counted once a request (until something is written), and the sums of the
        columns a page will total (`sums`) are added up in the same reading of the table."""
        settled = _all((self.access or (True, True))[0], self.yes_f, self.yes_q)
        unordered = self.order[1] if self.order else False
        asked = (self.table, repr(self.filters), self.q, self.everyone)
        adding = [k for k in sums if (asked, k) not in self.e._counted and self._adding(k)]
        if unordered is False and asked in self.e._counted and not adding:
            return (*self.e._counted[asked], 0)
        cols = [func.count(Record.id),
                literal(0) if settled is True else func.sum(case((_sql(settled), 0), else_=1)),
                literal(0) if unordered is False else func.sum(case((_sql(unordered), 1), else_=0))]
        for k in adding:
            amount, floats, odd = self._adding(k)
            cols += [func.sum(amount), func.count(amount), floats, func.sum(case((_sql(odd), 1), else_=0))]
        found = db.session.query(*cols).filter(*self.where).one()
        self.e._counted[asked] = tuple(found[:2])
        for i, k in enumerate(adding):
            self.e._counted[(asked, k)] = tuple(found[3 + 4 * i: 7 + 4 * i])
        return found[:3]

    def _adding(self, key):
        """(each record's amount as SQL adds it, whether any was written with a point, whether any is one Python reads
        its own way) for a kept number field; None for anything else."""
        f = self.e.field(self.table, key) if key else None
        if f is None or f["type"] not in NUMERIC or f.get("sensitive") or not KEY.match(key):
            return None
        v = self.planner.value(key)
        odd = _any(and_(v.string(), v.text != ""), v.boolean())  # to_number reads text and true/false its own way
        if self.planner.pg:  # added as decimals, exactly; whether any was written with a point decides int or float
            return case((v.number(), cast(v.text, Numeric))), func.bool_or(_all(v.number(), v.text.op("~")("[.eE]"))), odd
        return v.num, func.max(case((v.kind == "real", 1), else_=0)), odd

    def exact(self, sums=()):
        """(how many it finds, whether SQL alone settles which, whether SQL alone puts them in order)."""
        total, unsettled, unordered = self._stats(sums)
        whole = not self.residual and self.access is not None and not unsettled
        return total or 0, whole, whole and self.order is not None and not unordered

    def count(self):
        total, whole, _ = self.exact()
        return total if whole else len(self.rows(ordered=False))

    def page(self, offset, limit, sums=()):
        """(the records on this page, how many there are in all). `sums`: the columns it will be totalled by."""
        total, _, ordered = self.exact(sums)
        if ordered:
            rows = db.session.query(Record).filter(*self.where).order_by(*self.order[0]).offset(offset).limit(limit).all()
            self.e._found(self.table, rows)
            return rows, total
        rows = self.rows()
        return rows[offset:offset + limit], len(rows)

    def rows(self, ordered=True):
        """Every record it finds, as Engine.query gives them: checked in Python where SQL didn't settle them, in order."""
        if ordered and self.order is not None and not self.residual and self.access is not None:
            total, whole, in_order = self.exact()
            if in_order:
                rows = db.session.query(Record).filter(*self.where).order_by(*self.order[0]).all()
                self.e._found(self.table, rows, fetch=False)
                return rows
        out = self._checked(self._candidates().all())
        return self.e.sort(out, self.sort) if ordered else out

    def _candidates(self):
        return db.session.query(Record, _sql(self.yes_f).label("f"), _sql(self.yes_q).label("q")).filter(*self.where).order_by(Record.id)

    def _checked(self, found):
        """The records found that Engine.matches (and Engine.search, and who may see what) agree with. Those SQL settled
        are only checked for what SQL couldn't read."""
        e = self.e
        e._found(self.table, [r for r, _, _ in found], fetch=False)
        out = []
        for r, sure, sure_q in found:
            if self.access is None and not self.everyone and not e.allowed(r):
                continue
            if not (e.matches(r, self.residual) if sure else e.matches(r, self.filters)):
                continue
            if (self.q or "").strip() and not sure_q and not e.search([r], self.q):
                continue
            out.append(r)
        return out

    def first(self, limit, cap=2000):
        """(the first `limit` records it finds, how many there are). The count is exact, or, when more were found than
        `cap` and Python had to check them, how many of the first `cap` matched, as "180+"."""
        total, whole, ordered = self.exact()
        if ordered:
            return db.session.query(Record).filter(*self.where).order_by(*self.order[0]).limit(limit).all(), total
        if self.sort:
            rows = self.rows()
            return rows[:limit], len(rows)
        found, seen, after = [], 0, 0
        while True:
            batch = self._candidates().filter(Record.id > after).limit(cap).all()
            found += self._checked(batch)
            seen += len(batch)
            if len(batch) < cap or (len(found) >= limit and seen >= cap):
                break
            after = batch[-1][0].id
        return found[:limit], (f"{len(found)}+" if seen < total else len(found))

    def total(self, aggregate, key):
        """Engine.aggregate over the records found, from SQL when the field is a kept number; UNSURE when SQL can't say."""
        if aggregate == "count":
            return self.count()
        if aggregate not in ("sum", "avg", "min", "max", "median") or self._adding(key) is None:
            return UNSURE
        total, whole, _ = self.exact([key])
        if not whole:
            return UNSURE
        added, n, fraction, odd_ones = self.e._counted[((self.table, repr(self.filters), self.q, self.everyone), key)]
        if odd_ones:
            return UNSURE
        v = self.planner.value(key)
        numbers = and_(*self.where, v.number())
        if aggregate in ("min", "max"):  # the first lowest (or highest), as Python's min and max give
            pick = v.num.asc() if aggregate == "min" else v.num.desc()
            row = db.session.query(Record.data[key]).filter(numbers).order_by(pick, Record.id).first()
            return row[0] if row else None
        if aggregate == "median":
            return self.e.of_numbers([x for (x,) in db.session.query(Record.data[key]).filter(numbers).order_by(Record.id)], "median")
        if not n:
            return 0 if aggregate == "sum" else None
        if aggregate == "sum":
            return round(float(added), 2) if fraction else int(added)
        return round(float(added) / n, 2)
