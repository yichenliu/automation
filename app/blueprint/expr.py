"""A small, safe expression language for formulas and automation values.

    hours * rate                      iif(status == "Paid", 0, amount)
    client.payment_terms              add_days(today(), 30)
    round(total * 0.1, 2)             concat(first_name, " ", last_name)

Expressions are parsed with Python's own parser but only a whitelist of node types is evaluated: no attribute access
except to fields of records, no imports, no calls except the functions below. Spreadsheet habits are accepted too:
IF(...), AND/OR, {field}, a single = for comparison, <> for "not equal".
"""
import ast
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
    n = to_number(v)
    return 0 if n is None else n


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


FUNCTIONS = {
    "iif": _fn_iif,
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
    "date": to_date,
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
ALIASES = {"if": "iif", "datediff": "days_between", "dateadd": "add_days", "edate": "add_months", "today": "today", "now": "now"}
RECORD_FUNCTIONS = ("convert", "workday", "add_workdays", "workdays_between", "networkdays")  # a record's context gives these
KNOWN = set(FUNCTIONS) | set(ALIASES) | {"iif", "today", "now"} | set(RECORD_FUNCTIONS)


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
    return text


_cache = {}


def parse(source):
    text = normalise(source)
    if text in _cache:
        return _cache[text]
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise ExprError(f"can't read “{source}”: {exc.msg}") from exc
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


def unknown_calls(source):
    """The functions an expression calls that there are none of (“all(depends_on, ...)”): it would never work out."""
    return sorted({n.func.id for n in ast.walk(parse(source)) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                   and n.func.id.lower() not in KNOWN})


def evaluate(source, context):
    return _eval(parse(source).body, context)


def _attr(value, name):
    if value is None:
        return None
    getter = getattr(value, "field", None)
    if callable(getter):
        return getter(name)
    if isinstance(value, dict):
        return value.get(name)
    raise ExprError(f"“.{name}” only works on a linked record")


def _arith(op, a, b):
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
            return (to_date(a) - to_date(b)).days
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
        fn = ctx.functions.get(name) or FUNCTIONS.get(name)
        if fn is None:
            raise ExprError(f"there's no function called {node.func.id}()")
        args = [_eval(a, ctx) for a in node.args]
        try:
            return fn(*args)
        except ExprError:
            raise
        except Exception as exc:
            raise ExprError(f"{node.func.id}() couldn't work with those values ({exc})") from exc
    raise ExprError("unsupported expression")


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
