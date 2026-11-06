"""A small, safe expression language for formulas and automation values.

    hours * rate                      iif(status == "Paid", 0, amount)
    client.payment_terms              add_days(today(), 30)
    round(total * 0.1, 2)             concat(first_name, " ", last_name)

Expressions are parsed with Python's own parser but only a whitelist of node types is evaluated: no attribute access
except to fields of records, no imports, no calls except the functions below. Spreadsheet habits are accepted too:
IF(...), AND/OR, {field}, a single = for comparison, <> for "not equal".

Formulas can look at other records too: the records that link to this one (a client's invoices: client.invoices), or a
whole list (bookings), counted, added up or tested one by one, with what each must be after `where`:

    count(client.invoices where status = "Unpaid" and days_since(due_date) > 30) == 0
    sum(job.payments, amount)          any(bookings where stylist == this.stylist and id != this.id)
    overlaps(starts_at, ends_at, this.starts_at, this.ends_at)

Inside count/sum/any/all/…, a name is the field of the record being looked at; `this` is the record the formula is
about (its own fields are read too when the one looked at has no field of that name).

Lengths of time are written as people write them, and added to or taken from dates and times, or compared:

    returned_at > due_back_at + 2h     confirmed_at < now() - 48 hours     ends_at - starts_at > 30m
    hours_since(sent_at) > 48          starts_on + 2w

A plain number with a date is days (due_date + 14), as in a spreadsheet.
"""
import ast
import keyword
import math
import re
from datetime import date, datetime, time, timedelta


class ExprError(Exception):
    pass


def to_date(value):
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def to_datetime(value):
    """A date and time from 2026-11-03T14:30, "2026-11-03 2:30 pm", 03/11/2026 14:30, or a date (at midnight)."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None, second=0, microsecond=0)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    text = re.sub(r"\s+", " ", str(value).strip().upper().replace(".", ""))
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None, second=0, microsecond=0)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %I:%M %p", "%Y-%m-%d %I %p", "%d/%m/%Y %H:%M", "%d/%m/%Y %I:%M %p", "%d/%m/%Y %I %p", "%d %b %Y %H:%M",
                "%d %b %Y, %H:%M", "%d %b %Y %I:%M %p", "%d %b %Y, %I:%M %p", "%d %B %Y %H:%M", "%d %B %Y %I:%M %p", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    d = to_date(value)
    return datetime(d.year, d.month, d.day) if d else None


def to_bool(value):
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "y", "1", "checked", "on", "done")
    return bool(value)


def to_number(value):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float)):
        return value
    text = re.sub(r"[,$€£¥\s]", "", str(value))
    if text.endswith("%"):
        try:
            return float(text[:-1]) / 100
        except ValueError:
            return None
    try:
        number = float(text)
    except ValueError:
        return None
    return int(number) if number.is_integer() and "." not in text else number


def _num(v):
    if isinstance(v, timedelta):
        raise ExprError("a length of time (2h, 3 days) goes with a date or a time, not a plain number: hours_between(a, b) "
                        "gives the hours from one to another as a number")
    n = to_number(v)
    return 0 if n is None else n


class Days(int):
    """Days from one date to another, a whole number as a date take a date has always been (checkout - checkin is the
    nights), that keeps the exact time between them too, for a comparison with a length of time
    (returned_at - due_back_at > 2h)."""
    span = None


def _days_apart(a, b):
    out = Days((to_date(a) - to_date(b)).days)
    out.span = to_datetime(a) - to_datetime(b)
    return out


UNITS = {"h": "hours", "hr": "hours", "hrs": "hours", "hour": "hours", "hours": "hours", "m": "minutes", "min": "minutes",
         "mins": "minutes", "minute": "minutes", "minutes": "minutes", "d": "days", "day": "days", "days": "days",
         "w": "weeks", "wk": "weeks", "wks": "weeks", "week": "weeks", "weeks": "weeks"}
# 48h, 30m, 2d, 1w right after the number; 48 hours, 2 hrs, 3 days with or without a space
DURATION = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)(?:([hmdw])|\s*(hours?|hrs?|minutes?|mins?|days?|weeks?|wks?))(?![\w(])", re.I)


def _durations(text):
    """2h, 48 hours, 30m, 3 days, 1w read as hours(2), minutes(30)…, outside quotes."""
    return "".join(part if i % 2 else DURATION.sub(lambda m: f"{UNITS[(m.group(2) or m.group(3)).lower()]}({m.group(1)})", part)
                   for i, part in enumerate(_QUOTED.split(text)))


def _length(unit):
    def make(n=1):
        n = to_number(n)
        return None if n is None else timedelta(**{unit: n})
    return make


def _span(v):
    """A value as a length of time, to compare with one: a length as it is, days apart as their exact time, a number
    as days."""
    if v is None or isinstance(v, timedelta):
        return v
    if isinstance(v, Days) and v.span is not None:
        return v.span
    n = to_number(v)
    return None if n is None else timedelta(days=n)


class Context:
    """Names an expression can use. `resolve(name)` gives a value; record-like values support `.field`. `functions` are
    ones that need to know the workspace (convert() needs its exchange rates)."""
    functions = {}

    def __init__(self, names=None, today=None, now=None):
        self.names = dict(names or {})
        self.today = today or date.today()
        self.now = now or datetime.now()

    def resolve(self, name):
        if name in self.names:
            return self.names[name]
        if name == "today":
            return self.today
        if name == "now":
            return self.now
        raise ExprError(f"unknown name '{name}'")


class Many(list):
    """Several records (a client's invoices) or their values (their amounts): `.field` of each, as Many again."""

    def field(self, name):
        out = Many()
        for x in self:
            v = _attr(x, name)
            if isinstance(v, list):
                out.extend(v)
            elif v is not None:
                out.append(v)
        return out

    def __str__(self):
        return ", ".join(_text(x) for x in self)


UNSURE = object()  # what the database can't answer for sure: the records are read and it's worked out here


class Lazy:
    """Several records not read yet (a whole list, a client's invoices): narrowed by what each must be, and counted or
    added up, by the database where it can (the engine's Rows); read only when they must be."""

    def narrowed_by(self, node, ctx):
        """(the records narrowed by this condition as far as the database can, whether that's all of it)."""
        return self, False

    def answer(self, how, key=None):
        """count (key None), or sum/avg/min/max of a kept field, from the database; UNSURE when it can't say."""
        return UNSURE

    def read(self):
        return Many()

    def field(self, name):
        return self.read().field(name)

    def __iter__(self):
        return iter(self.read())

    def __len__(self):
        return len(self.read())

    def __bool__(self):
        return len(self) > 0

    def __str__(self):
        return str(self.read())


def _record_like(x):
    return callable(getattr(x, "field", None)) and not isinstance(x, (Many, Lazy))


class ItemContext(Context):
    """Names inside count(...), sum(...), any(...): the record looked at first, then what's outside."""

    def __init__(self, outer, item):
        self.outer, self.item = outer, item
        self.names, self.today, self.now = {}, outer.today, outer.now

    @property
    def functions(self):
        return self.outer.functions

    def resolve(self, name):
        if name in ("item", "it"):
            return self.item
        if name != "this":
            has = getattr(self.item, "has", None)
            if callable(has) and has(name):
                return self.item.field(name)
            if isinstance(self.item, dict) and name in self.item:
                return self.item[name]
        return self.outer.resolve(name)


def _fn_iif(cond, a=None, b=None):
    return a if cond else b


def _days_between(a, b):
    a, b = to_date(a), to_date(b)
    return None if a is None or b is None else (b - a).days


def _add_days(d, n):
    d = to_date(d)
    return None if d is None else d + timedelta(days=int(_num(n)))


def _add_months(d, n):
    d = to_date(d)
    if d is None:
        return None
    months = d.month - 1 + int(_num(n))
    year, month = d.year + months // 12, months % 12 + 1
    day = min(d.day, [31, 29 if year % 4 == 0 and (year % 100 or year % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    return date(year, month, day)


def _date(*parts):
    """date("2026-11-05") reads a date; date(2026, 11, 5) makes one as a spreadsheet's DATE does (month 13 is next
    January, day 0 the month before's last)."""
    if len(parts) == 1:
        return to_date(parts[0])
    if len(parts) != 3:
        raise ExprError(f"date() takes a date, or a year, month and day, not {len(parts)} values")
    year, month, day = (to_number(p) for p in parts)
    if None in (year, month, day):
        return None
    months = int(year) * 12 + int(month) - 1
    return date(months // 12, months % 12 + 1, 1) + timedelta(days=int(day) - 1)


def _end_of_month(d, n=0):
    """The last day of the month a date is in, or n months on (a spreadsheet's EOMONTH)."""
    d = to_date(d)
    if d is None:
        return None
    months = d.year * 12 + d.month + int(_num(n))
    return date(months // 12, months % 12 + 1, 1) - timedelta(days=1)


def _text(v):
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, datetime):
        return f"{v:%Y-%m-%d %H:%M}" if (v.hour, v.minute) != (0, 0) else v.isoformat()[:10]
    if isinstance(v, date):
        return v.isoformat()
    return str(v)


CLOCK = re.compile(r"(\d{1,2})(?::(\d{2}))?(?::\d{2})?\s*([ap]\.?m\.?)?", re.I)


def _clock(v):
    """Minutes into the day of a time on its own ("09:00", "2:30 pm", "5pm"), or None for anything else."""
    if isinstance(v, time):
        return v.hour * 60 + v.minute
    m = CLOCK.fullmatch(str(v).strip()) if isinstance(v, str) and (":" in v or re.search(r"[ap]\.?m", v, re.I)) else None
    if not m:
        return None
    hour, minute, half = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower().replace(".", "")
    hour = hour % 12 + (12 if half == "pm" else 0) if half else hour
    return hour * 60 + minute if hour < 24 and minute < 60 else None


def _hours_between(a, b):
    """Hours from one date-and-time to another, or between two times of day (a shift from 22:00 to 06:00 is 8)."""
    start, end = _clock(a), _clock(b)
    if start is not None and end is not None:
        return round(((end - start) % (24 * 60)) / 60, 4)
    a, b = to_datetime(a), to_datetime(b)
    return None if a is None or b is None else round((b - a).total_seconds() / 3600, 4)


def _add_hours(d, n):
    d = to_datetime(d)
    return None if d is None else d + timedelta(hours=_num(n))


def _round(x, n=0):
    x = to_number(x)
    return None if x is None else round(x, int(_num(n)))


def _moment(v):
    """A date, a date and time, or a time of day ("09:00"), as one point to compare."""
    if v in (None, ""):
        return None
    clock = _clock(v) if not isinstance(v, (date, datetime)) else None
    if clock is not None:
        return datetime(2000, 1, 1) + timedelta(minutes=clock)
    return to_datetime(v)


def spans_overlap(a, b):
    """Whether two spans of dates or times overlap; one with no end is that one day or moment. The day one ends, the
    next can start (a checkout and a check-in)."""
    (s1, e1), (s2, e2) = a, b
    if e1 <= s1 and e2 <= s2:
        return s1 == s2
    if e1 <= s1:
        return s2 <= s1 < e2
    if e2 <= s2:
        return s1 <= s2 < e1
    return s1 < e2 and s2 < e1


def _overlaps(start1, end1, start2=None, end2=None):
    """overlaps(starts, ends, other_starts, other_ends): whether two bookings' times overlap (an end left empty: that
    one day or moment)."""
    s1, s2 = _moment(start1), _moment(start2)
    if s1 is None or s2 is None:
        return False
    e1, e2 = _moment(end1) or s1, _moment(end2) or s2
    return spans_overlap((s1, max(e1, s1)), (s2, max(e2, s2)))


FUNCTIONS = {
    "iif": _fn_iif,
    "overlaps": _overlaps,
    "round": _round,
    "abs": lambda x: abs(_num(x)),
    "min": lambda *a: min(_num(x) for x in a) if a else None,
    "max": lambda *a: max(_num(x) for x in a) if a else None,
    "sum": lambda *a: sum(_num(x) for x in (a[0] if len(a) == 1 and isinstance(a[0], (list, tuple)) else a)),
    "ceil": lambda x: math.ceil(_num(x)),
    "floor": lambda x: math.floor(_num(x)),
    "days_between": _days_between,
    "add_days": _add_days,
    "add_months": _add_months,
    "hours_between": _hours_between,
    "add_hours": _add_hours,
    "hours": _length("hours"),
    "minutes": _length("minutes"),
    "days": _length("days"),
    "weeks": _length("weeks"),
    "weekday": lambda d: to_date(d).isoweekday() if to_date(d) else None,
    "day": lambda d: to_date(d).day if to_date(d) else None,
    "datetime": to_datetime,
    "year": lambda d: to_date(d).year if to_date(d) else None,
    "month": lambda d: to_date(d).month if to_date(d) else None,
    "concat": lambda *a: "".join(_text(x) for x in a),
    "upper": lambda s: _text(s).upper(),
    "lower": lambda s: _text(s).lower(),
    "text": _text,
    "number": to_number,
    "date": _date,
    "end_of_month": _end_of_month,
    "coalesce": lambda *a: next((x for x in a if x not in (None, "")), None),
    "is_empty": lambda x: x in (None, "", []),
    "len": lambda x: len(x) if x is not None else 0,
    # for files in another system's layout (a bank's fixed-width payment file)
    "pad": lambda s, n, c=" ": _text(s)[:int(_num(n))].ljust(int(_num(n)), str(c)[:1] or " "),
    "lpad": lambda s, n, c="0": _text(s)[-int(_num(n)):].rjust(int(_num(n)), str(c)[:1] or "0"),
    "digits": lambda s: re.sub(r"\D", "", _text(s)),
    "cents": lambda x: int(round((to_number(x) or 0) * 100)),
    "format_date": lambda d, f="%d%m%y": to_date(d).strftime(str(f)) if to_date(d) else "",
}
ALIASES = {"if": "iif", "datediff": "days_between", "dateadd": "add_days", "edate": "add_months", "eomonth": "end_of_month",
           "today": "today", "now": "now", "countif": "count", "countifs": "count", "exists": "any", "filter": "where",
           "sumif": "sumif", "sumifs": "sumif", "average": "avg", "averageif": "avgif", "mean": "avg", "overlap": "overlaps"}
# what looks at several records: count(client.invoices where status = "Unpaid"), sum(job.payments, amount)
QUERIES = {"count", "sum", "sumif", "avg", "avgif", "min", "max", "any", "all", "none", "where", "first", "last"}
TODAY_FUNCTIONS = {"days_since", "days_until"}  # days from a date to today, and from today to a date
NOW_FUNCTIONS = {"hours_since", "hours_until"}  # hours from a time to now, and from now to a time
RECORD_FUNCTIONS = ("convert", "workday", "add_workdays", "workdays_between", "networkdays")  # a record's context gives these
KNOWN = set(FUNCTIONS) | set(ALIASES) | {"iif", "today", "now"} | set(RECORD_FUNCTIONS) | QUERIES | TODAY_FUNCTIONS | NOW_FUNCTIONS

# Every function, as the AI is told it (the catalog's FORMULAS): tests/test_queries.py fails for one with no words here.
SAID = {
    "today": "today() the date today; now() the date and time now",
    "now": None,
    "days_since": "days_since(sent_on) days from a date to today; days_until(starts_on) days from today to it",
    "days_until": None,
    "hours_since": "hours_since(sent_at) hours from a time to now; hours_until(starts_at) hours from now to it",
    "hours_until": None,
    "days_between": "days_between(a, b) days from date a to date b (b - a)",
    "hours_between": "hours_between(a, b) hours from a to b, two dates and times, or two times of day (22:00 to 06:00 is 8)",
    "add_days": "add_days(d, n) n days on from d (d + n says the same)",
    "add_months": "add_months(d, n) the same day n months on",
    "add_hours": "add_hours(t, n) n hours on from t (t + nh says the same)",
    "hours": "hours(n), minutes(n), days(n), weeks(n) a length of time from a number or a field (starts_at + hours(duration))",
    "minutes": None, "days": None, "weeks": None,
    "workday": "workday(d, n) the date n working days on, weekends and the business's holidays skipped (add_workdays the same)",
    "add_workdays": None,
    "workdays_between": "workdays_between(a, b) working days from a to b (networkdays the same)",
    "networkdays": None,
    "end_of_month": "end_of_month(d) the month's last day; end_of_month(d, 1) next month's",
    "date": "date(\"2026-11-05\") or date(2026, 11, 5) a date; datetime(\"2026-11-05 14:30\") a date and time",
    "datetime": None,
    "weekday": "weekday(d) 1 (Monday) to 7 (Sunday); day(d), month(d), year(d) its parts",
    "day": None, "month": None, "year": None,
    "format_date": "format_date(d, \"%d/%m/%Y\") a date written a set way",
    "overlaps": "overlaps(starts, ends, this.starts, this.ends) whether two spans of dates or times overlap",
    "iif": "if(cond, a, b) (iif the same) a when cond holds, otherwise b",
    "round": "round(x, 2); ceil(x); floor(x); abs(x)",
    "ceil": None, "floor": None, "abs": None,
    "coalesce": "coalesce(a, b, …) the first one that isn't empty",
    "is_empty": "is_empty(x) whether x has nothing in it",
    "concat": "concat(a, b, …) text joined (a + b says the same); upper(s), lower(s), text(x), number(s), len(s)",
    "upper": None, "lower": None, "text": None, "number": None, "len": None,
    "convert": "convert(total, \"USD\") into the business's own currency at its exchange rates (or convert(x, \"USD\", \"EUR\"))",
    "count": "count(client.invoices where status = \"Unpaid\") how many; any(…), all(…), none(…) whether some, each or none of them hold",
    "any": None, "all": None, "none": None,
    "sum": "sum(job.payments, amount) added up; avg(…), min(…), max(…) the same way (min/max of plain values too: max(a, b))",
    "avg": None, "min": None, "max": None,
    "where": "where(…), first(…), last(…) the records that hold, the first or last of them (first(client.jobs where …).finished_on)",
    "first": None, "last": None,
    "sumif": "sumif(items, cond, value), avgif(…) a spreadsheet's SUMIF / AVERAGEIF",
    "avgif": None,
    "pad": "pad(s, n), lpad(s, n, \"0\"), digits(s), cents(x) for files in another system's layout (a bank's payment file)",
    "lpad": None, "digits": None, "cents": None,
}


def reference():
    """The formula language in a few lines, for the AI (the catalog's FORMULAS)."""
    lines = ["FORMULAS (a formula field, a condition written as text, a value a step works out):",
             "- a record's fields by key (quantity * unit_price), a linked record's through dots (customer.discount), "
             "the records linking to it by their list (client.invoices), a whole list by its key (bookings); "
             "inside count/sum/any/…, names are the looked-at record's fields and this.x the formula's own record",
             "- and, or, not; == != < > <= >=; text in quotes; x in [\"A\", \"B\"]; if(cond, a, b)",
             "- a plain number with a date is days (due_date + 14; returned_on - due_on is days). Lengths of time: 2h, "
             "48 hours, 30m, 3 days, 2w, on a date or a time or compared (returned_at > due_back_at + 2h; "
             "confirmed_at < now() - 48h; ends_at - starts_at > 30m)"]
    lines += [f"- {said}" for said in SAID.values() if said]
    return "\n".join(lines)


def normalise(source):
    """Accept spreadsheet-style writing: IF(), AND/OR/NOT, {field}, = for equality, <> for inequality, & to join text."""
    text = str(source).strip()
    if text.startswith("="):
        text = text[1:]
    text = re.sub(r"\{\{\s*(.*?)\s*\}\}", r"\1", text)
    text = re.sub(r"\{([A-Za-z_][\w.]*)\}", r"\1", text)
    text = re.sub(r"\bIF\s*\(", "iif(", text, flags=re.I)
    text = re.sub(r"\bAND\b", "and", text)
    text = re.sub(r"\bOR\b", "or", text)
    text = re.sub(r"\bNOT\b", "not", text)
    text = re.sub(r"\bTRUE\b", "True", text, flags=re.I)
    text = re.sub(r"\bFALSE\b", "False", text, flags=re.I)
    text = re.sub(r"\bnull\b", "None", text)
    text = text.replace("<>", "!=").replace("&&", " and ").replace("||", " or ")
    text = re.sub(r"(?<![<>!=])=(?!=)", "==", text)
    text = re.sub(r"\s&\s", " + ", text)  # "a & b" joins text in spreadsheets
    return _wheres(_durations(text))


WHERE = re.compile(r"\bwhere\b(?!\s*\()", re.I)


def _wheres(text):
    """`client.invoices where status = "Unpaid"` read as where(client.invoices, status == "Unpaid"): what's before it
    back to the bracket or comma it starts after, what's after it up to the bracket or comma it ends at."""
    for _ in range(12):
        quoted = [m.span() for m in _QUOTED.finditer(text)]
        at = next((m for m in WHERE.finditer(text) if not any(a <= m.start() < b for a, b in quoted)), None)
        if at is None:
            return text

        def outside(i):
            return not any(a <= i < b for a, b in quoted)
        depth, start = 0, 0
        for i in range(at.start() - 1, -1, -1):
            ch = text[i]
            if not outside(i):
                continue
            if ch in ")]":
                depth += 1
            elif ch in "([":
                if depth == 0:
                    start = i + 1
                    break
                depth -= 1
            elif ch == "," and depth == 0:
                start = i + 1
                break
        depth, end = 0, len(text)
        for i in range(at.end(), len(text)):
            ch = text[i]
            if not outside(i):
                continue
            if ch in "([":
                depth += 1
            elif ch in ")]":
                if depth == 0:
                    end = i
                    break
                depth -= 1
            elif ch == "," and depth == 0:
                end = i
                break
        left, right = text[start:at.start()].strip(), text[at.end():end].strip()
        text = f"{text[:start]}{' ' if start and text[start - 1] == ',' else ''}where({left}, {right}){text[end:]}"
    return text


_cache = {}
# words Python keeps for itself that a business may well name a field (a class's "class", an email's "from", a "pass"
# mark): read as names. The ones formulas use as words (and, or, not, in, is, x if y else z) stay words.
RESERVED = frozenset(keyword.kwlist) - {"and", "or", "not", "in", "is", "if", "else", "True", "False", "None"}
_RESERVED = re.compile(r"\b(" + "|".join(sorted(RESERVED)) + r")\b")
_QUOTED = re.compile(r"(\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*')")


def _as_names(text):
    """`class.capacity` read as the field it is, not Python's `class`: such words, outside quotes, get a mark taken off again
    once the formula is read."""
    return "".join(part if i % 2 else _RESERVED.sub(r"\1__kw", part) for i, part in enumerate(_QUOTED.split(text)))


def parse(source):
    text = normalise(source)
    if text in _cache:
        return _cache[text]
    try:
        tree = ast.parse(_as_names(text), mode="eval")
    except SyntaxError as exc:
        raise ExprError(f"can't read “{source}”: {exc.msg}") from exc
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id.endswith("__kw"):
            node.id = node.id[:-4]
        elif isinstance(node, ast.Attribute) and node.attr.endswith("__kw"):
            node.attr = node.attr[:-4]
    _check(tree)
    _cache[text] = tree
    return tree


ALLOWED = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.IfExp, ast.Call, ast.Name, ast.Load,
           ast.Constant, ast.Attribute, ast.List, ast.Tuple, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.FloorDiv,
           ast.Pow, ast.USub, ast.UAdd, ast.Not, ast.And, ast.Or, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
           ast.In, ast.NotIn)


def _check(tree):
    for node in ast.walk(tree):
        if not isinstance(node, ALLOWED):
            raise ExprError(f"“{type(node).__name__}” isn't allowed in a formula")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.keywords:
                raise ExprError("only simple function calls are allowed, like round(x, 2)")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ExprError("names starting with _ aren't allowed")


def names_in(source):
    """The plain names (and first parts of dotted names) an expression refers to."""
    tree = parse(source)
    called = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and id(n) not in called}


def outer_names(source):
    """The plain names an expression reads on its own record: those inside count(...), sum(...), any(...) (after the
    records looked at) are the looked-at records' own, and aren't. `this` is the record itself."""
    tree = parse(source)
    out = set()

    def walk(node):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and ALIASES.get(node.func.id.lower(), node.func.id.lower()) in QUERIES \
                and node.args:
            walk(node.args[0])
            return
        if isinstance(node, ast.Name):
            out.add(node.id)
            return
        for child in ast.iter_child_nodes(node):
            if not (isinstance(node, ast.Call) and child is node.func):
                walk(child)
    walk(tree.body)
    return out


def unknown_calls(source):
    """The functions an expression calls that there are none of (“all(depends_on, ...)”): it would never work out."""
    return sorted({n.func.id for n in ast.walk(parse(source)) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                   and n.func.id.lower() not in KNOWN})


def evaluate(source, context):
    out = _eval(parse(source).body, context)
    if isinstance(out, timedelta):  # a length of time worked out on its own: in days, as a date take a date is
        return round(out.total_seconds() / 86400, 4)
    return out


def _attr(value, name):
    if value is None:
        return None
    getter = getattr(value, "field", None)
    if callable(getter):
        return getter(name)
    if isinstance(value, dict):
        return value.get(name)
    if name == "days" and isinstance(value, (int, float)) and not isinstance(value, bool):
        return value  # one date take another is already a number of days: (today() - due_date).days is that number
    raise ExprError(f"“.{name}” only works on a linked record")


def _with_length(op, a, b):
    """Arithmetic with a length of time (2h, 3 days): on a date or a time, with another length, or by a number."""
    a, b = (to_datetime(x) if isinstance(x, str) and to_datetime(x) else x for x in (a, b))  # "2026-11-03 08:00" as written
    if a is None or b is None:
        return None
    add, sub = isinstance(op, ast.Add), isinstance(op, ast.Sub)
    if isinstance(a, timedelta) and isinstance(b, timedelta):
        if add or sub:
            return a + b if add else a - b
        if isinstance(op, ast.Div):
            return None if not b else a / b
    elif isinstance(a, date) or isinstance(b, date):
        when, length = (a, b) if isinstance(a, date) else (b, a)
        if sub and when is b:
            raise ExprError("a length of time can't take a date away: write the date first (due_date - 3 days)")
        if add or sub:
            length = length if add else -length
            if not isinstance(when, datetime) and length % timedelta(days=1):
                when = datetime(when.year, when.month, when.day)  # hours on a day: from its start
            return when + length
    elif isinstance(op, (ast.Mult, ast.Div)):
        length, n = (a, b) if isinstance(a, timedelta) else (b, a)
        if isinstance(op, ast.Div) and length is b:
            raise ExprError("a number can't be divided by a length of time")
        n = to_number(n)
        if n is None:
            return None
        return length * n if isinstance(op, ast.Mult) else (None if n == 0 else length / n)
    raise ExprError("a length of time (2h, 3 days) is added to or taken from a date or a time, or another length")


def _arith(op, a, b):
    if isinstance(a, timedelta) or isinstance(b, timedelta):
        return _with_length(op, a, b)
    if isinstance(op, ast.Add):
        if isinstance(a, date) and not isinstance(b, date):
            return a + timedelta(days=_num(b))
        if isinstance(b, date) and not isinstance(a, date):
            return b + timedelta(days=_num(a))
        if (isinstance(a, str) or isinstance(b, str)) and (to_number(a) is None or to_number(b) is None):
            return _text(a) + _text(b)
        return _num(a) + _num(b)
    if isinstance(op, ast.Sub):
        if isinstance(a, date) and isinstance(b, date):
            return _days_apart(a, b)
        if isinstance(a, date):
            return a - timedelta(days=_num(b))
        return _num(a) - _num(b)
    if isinstance(op, ast.Mult):
        return _num(a) * _num(b)
    if isinstance(op, ast.Div):
        return None if _num(b) == 0 else _num(a) / _num(b)
    if isinstance(op, ast.FloorDiv):
        return None if _num(b) == 0 else _num(a) // _num(b)
    if isinstance(op, ast.Mod):
        return None if _num(b) == 0 else _num(a) % _num(b)
    if isinstance(op, ast.Pow):
        return _num(a) ** min(_num(b), 100)
    raise ExprError("unsupported arithmetic")


def _comparable(a, b):
    if isinstance(a, timedelta) or isinstance(b, timedelta):
        return _span(a), _span(b)
    if isinstance(a, datetime) or isinstance(b, datetime):
        return to_datetime(a), to_datetime(b)
    if isinstance(a, date) or isinstance(b, date):
        return to_date(a), to_date(b)
    if isinstance(a, bool) or isinstance(b, bool):
        return to_bool(a), to_bool(b)
    if isinstance(a, (int, float)) or isinstance(b, (int, float)):
        return to_number(a), to_number(b)
    if isinstance(a, str) and isinstance(b, str):
        return a.strip().lower(), b.strip().lower()
    return a, b


def _compare(op, a, b):
    if isinstance(op, (ast.In, ast.NotIn)):
        if isinstance(b, (list, tuple)):
            found = any(_compare(ast.Eq(), a, x) for x in b)
        else:
            found = _text(a).lower() in _text(b).lower()
        return found if isinstance(op, ast.In) else not found
    if isinstance(op, (ast.Eq, ast.NotEq)):
        if a in (None, "") and b in (None, ""):
            equal = True
        else:
            x, y = _comparable(a, b)
            equal = x == y
        return equal if isinstance(op, ast.Eq) else not equal
    x, y = _comparable(a, b)
    if x is None or y is None:
        return False
    try:
        return {ast.Lt: x < y, ast.LtE: x <= y, ast.Gt: x > y, ast.GtE: x >= y}[type(op)]
    except TypeError:
        return False


def _eval(node, ctx):
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in ("True", "False", "None"):
            return {"True": True, "False": False, "None": None}[node.id]
        return ctx.resolve(node.id)
    if isinstance(node, ast.Attribute):
        return _attr(_eval(node.value, ctx), node.attr)
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_eval(e, ctx) for e in node.elts]
    if isinstance(node, ast.UnaryOp):
        value = _eval(node.operand, ctx)
        if isinstance(node.op, ast.Not):
            return not value
        return -_num(value) if isinstance(node.op, ast.USub) else _num(value)
    if isinstance(node, ast.BinOp):
        return _arith(node.op, _eval(node.left, ctx), _eval(node.right, ctx))
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            result = True
            for v in node.values:
                result = _eval(v, ctx)
                if not result:
                    return result
            return result
        result = False
        for v in node.values:
            result = _eval(v, ctx)
            if result:
                return result
        return result
    if isinstance(node, ast.Compare):
        left = _eval(node.left, ctx)
        for op, comparator in zip(node.ops, node.comparators):
            right = _eval(comparator, ctx)
            if not _compare(op, left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.IfExp):
        return _eval(node.body, ctx) if _eval(node.test, ctx) else _eval(node.orelse, ctx)
    if isinstance(node, ast.Call):
        name = node.func.id.lower()
        name = ALIASES.get(name, name)
        if name == "iif":  # only evaluate the branch that's taken
            args = node.args
            if not args:
                raise ExprError("iif needs a condition")
            cond = _eval(args[0], ctx)
            branch = args[1] if cond else (args[2] if len(args) > 2 else None)
            return _eval(branch, ctx) if branch is not None else None
        if name == "today":
            return ctx.today
        if name == "now":
            return ctx.now
        if name in TODAY_FUNCTIONS:  # days_since(sent_on): how long ago; days_until(starts_on): how long to go
            if len(node.args) != 1:
                raise ExprError(f"{node.func.id}() takes one date")
            d = to_date(_eval(node.args[0], ctx))
            if d is None:
                return None
            return (ctx.today - d).days if name == "days_since" else (d - ctx.today).days
        if name in NOW_FUNCTIONS:  # hours_since(sent_at) > 48: how many hours ago; hours_until(starts_at): to go
            if len(node.args) != 1:
                raise ExprError(f"{node.func.id}() takes one date and time")
            d = to_datetime(_eval(node.args[0], ctx))
            if d is None:
                return None
            hours = (ctx.now - d).total_seconds() / 3600
            return round(hours if name == "hours_since" else -hours, 4)
        if name in QUERIES and node.args:
            first = _eval(node.args[0], ctx)
            if _record_like(first) and name not in ("count", "sum", "min", "max", "avg") or (_record_like(first) and len(node.args) > 1):
                first = Many([first])  # one linked record: the records looked at are that one
            if _is_many(first):
                return _query(name, node, first, ctx)
            if name in ("where", "any", "all", "none", "first", "last", "sumif", "avgif"):
                if first is None:  # nothing linked: nothing to look at
                    return _query(name, node, Many(), ctx)
                raise ExprError(f"{node.func.id}() looks at several records (a client's invoices: client.invoices, or a whole list), "
                                f"and what's given isn't")
            rest = [_eval(a, ctx) for a in node.args[1:]]
            if name == "count":  # a spreadsheet's COUNT: the values given that are there
                return sum(1 for x in [first, *rest] for y in (x if isinstance(x, list) else [x]) if y not in (None, ""))
            if name == "avg":
                values = [_num(y) for x in [first, *rest] for y in (x if isinstance(x, list) else [x]) if y not in (None, "")]
                return sum(values) / len(values) if values else None
            args = [first, *rest]
            if name in ("min", "max") and len(args) == 1 and isinstance(first, list):
                args = [y for y in first if y not in (None, "")]
                if not args:
                    return None
            return FUNCTIONS[name](*args)
        fn = ctx.functions.get(name) or FUNCTIONS.get(name)
        if fn is None:
            raise ExprError(f"there's no function called {node.func.id}()")
        args = [_eval(a, ctx) for a in node.args]
        try:
            return fn(*args)
        except ExprError:
            raise
        except TypeError as exc:  # said in the formula's words, never the code's ("to_date() takes 1 positional argument")
            raise ExprError(f"{node.func.id}() can't take {len(args)} value{'s' if len(args) != 1 else ''} like that") from exc
        except Exception as exc:
            raise ExprError(f"{node.func.id}() couldn't work with those values ({exc})") from exc
    raise ExprError("unsupported expression")


def _is_many(value):
    return isinstance(value, (Many, Lazy)) or (isinstance(value, (list, tuple)) and any(_record_like(x) for x in value))


CONDITIONED = ("count", "any", "none", "where", "first", "last", "sumif", "avgif")  # their second value says which count


def _query(name, node, items, ctx):
    """count/sum/avg/min/max/any/all/none/where/first/last over several records: `where` (or a second value) says which
    of them count; sum, avg, min and max say what of each to add up. Records not read yet (Lazy) are narrowed, counted
    and added up by the database where it can, and read only for what it can't."""
    args = list(node.args[1:])
    if isinstance(items, Lazy):
        got = _from_database(name, args, items, ctx)
        if got[0] is not UNSURE:
            return got[0]
        items = got[1].read()
    items = Many(items)

    def holds(item, cond):
        return to_bool(_eval(cond, ItemContext(ctx, item)))

    def value(item, of):
        return _eval(of, ItemContext(ctx, item))
    if name == "where":
        if len(args) != 1:
            raise ExprError("where takes what each must be: client.invoices where status = \"Unpaid\"")
        return Many(x for x in items if holds(x, args[0]))
    if name in ("sumif", "avgif"):  # SUMIF(range, which, what to add up)
        if len(args) != 2:
            raise ExprError(f"{node.func.id}() takes the records, which of them count, and what of each to add up")
        items, args, name = Many(x for x in items if holds(x, args[0])), args[1:], name[:-2]
    if name in ("count", "any", "all", "none", "first", "last"):
        if len(args) > 1:
            raise ExprError(f"{node.func.id}() takes the records and, if only some count, what each must be")
        chosen = [x for x in items if holds(x, args[0])] if args and name != "all" else list(items)
        if name == "count":
            return len(chosen)
        if name == "any":
            return bool(chosen)
        if name == "none":
            return not chosen
        if name == "all":
            return all(holds(x, args[0]) for x in items) if args else all(to_bool(x) for x in items)
        return (chosen[0] if name == "first" else chosen[-1]) if chosen else None
    # sum, avg, min, max: of each record what's given (sum(job.payments, amount)), or of values (sum(job.payments.amount))
    if len(args) > 1:
        raise ExprError(f"{node.func.id}() takes the records and what of each to add up: sum(job.payments, amount)")
    if args:
        values = [value(x, args[0]) for x in items]
    elif any(_record_like(x) for x in items):
        raise ExprError(f"say what of each to use: {node.func.id}(job.payments, amount)")
    else:
        values = list(items)
    values = [v for v in values if v not in (None, "")]
    if name == "sum":
        return sum(_num(v) for v in values)
    if not values:
        return None
    if name == "avg":
        return sum(_num(v) for v in values) / len(values)
    pick = min if name == "min" else max
    if not any(isinstance(v, (date, datetime)) for v in values) and all(to_number(v) is not None for v in values):
        return pick(to_number(v) for v in values)
    if all(to_datetime(v) is not None for v in values):  # the latest due date, the earliest booking
        return pick(values, key=to_datetime)
    return pick(values, key=lambda v: _text(v).lower())


def _from_database(name, args, items, ctx):
    """(the answer, or UNSURE; the records, narrowed as far as the database could). What's read after is still checked
    against the whole condition here: narrowing only leaves out what can't count."""
    plain = lambda n: n.id if isinstance(n, ast.Name) else None  # noqa: E731  (a field of each: sum(payments, amount))
    if name in CONDITIONED and args:
        items, whole = items.narrowed_by(args[0], ctx)
        if whole:
            if name == "where":
                return items, items
            if name in ("count", "any", "none") and len(args) == 1:
                n = items.answer("count")
                if n is not UNSURE:
                    return (n if name == "count" else n > 0 if name == "any" else n == 0), items
            if name in ("sumif", "avgif") and len(args) == 2 and plain(args[1]):
                return items.answer(name[:-2], plain(args[1])), items
        return UNSURE, items
    if not args and name in ("count", "any", "none"):
        n = items.answer("count")
        return ((n if name == "count" else n > 0 if name == "any" else n == 0) if n is not UNSURE else UNSURE), items
    if name in ("sum", "avg", "min", "max") and len(args) == 1 and plain(args[0]):
        return items.answer(name, plain(args[0])), items
    return UNSURE, items


TEMPLATE = re.compile(r"\{\{\s*(.+?)\s*\}\}")


def render(value, context):
    """Fill {{ }} in a value. A value that is nothing but one {{ expression }} keeps its type (a number, a date, a
    record); otherwise each {{ }} is replaced by its text."""
    if not isinstance(value, str):
        return value
    whole = re.fullmatch(r"\s*\{\{\s*(.+?)\s*\}\}\s*", value)
    if whole and len(TEMPLATE.findall(value)) == 1:  # "{{a}},{{b}}" starts and ends with braces but is two of them
        return evaluate(whole.group(1), context)
    shown = getattr(context, "shown", None)  # the context may know how people expect a value to look ($1,200.00, 3 Nov 2026)
    return TEMPLATE.sub(lambda m: shown(m.group(1), evaluate(m.group(1), context)) if shown else _text(evaluate(m.group(1), context)), value)
