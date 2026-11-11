"""Template helpers shared by every page."""
import json
import re
from datetime import datetime

import markdown as md
import nh3
from markupsafe import Markup, escape

from ..blueprint.schema import lc
from ..models import utcnow
from ..core.context import current_app
from ..storage import human_size
from .http import context_processor, current_request

PILL_COLOURS = ["slate", "blue", "amber", "green", "violet", "red", "teal", "pink"]
SPECIAL = {"done": "green", "paid": "green", "complete": "green", "completed": "green", "won": "green", "active": "blue",
           "received": "green", "approved": "green", "accepted": "green", "delivered": "green", "closed": "slate",
           "overdue": "red", "lost": "red", "cancelled": "red", "canceled": "red", "rejected": "red", "declined": "red",
           "on hold": "amber", "pending": "amber", "waiting": "amber", "sent": "violet", "draft": "slate",
           "in progress": "blue", "open": "blue", "new": "teal", "lead": "teal", "scheduled": "violet", "urgent": "red",
           "high": "red", "medium": "amber", "low": "slate"}


def ago(stamp):
    if not stamp:
        return ""
    seconds = int((utcnow() - stamp).total_seconds())
    if seconds < 45:
        return "just now"
    for size, word in ((86400 * 30, "month"), (86400 * 7, "week"), (86400, "day"), (3600, "hour"), (60, "minute")):
        if seconds >= size:
            n = seconds // size
            return f"{n} {word}{'s' if n != 1 else ''} ago"
    return "just now"


def pill(field, value):
    """A select option's badge colour: by meaning when it's a common word, else stable by its position in the list."""
    if value in (None, ""):
        return "slate"
    chosen = ((field or {}).get("option_colors") or {}).get(str(value))
    if chosen:
        return chosen
    special = SPECIAL.get(str(value).strip().lower())
    if special:
        return special
    options = [o.lower() for o in (field or {}).get("options", [])]
    try:
        return PILL_COLOURS[options.index(str(value).lower()) % len(PILL_COLOURS)]
    except ValueError:
        return "slate"


def icon(name, cls=""):
    """An inline SVG icon from the sprite."""
    return Markup(f'<svg class="i {cls}" aria-hidden="true"><use href="/static/icons.svg#i-{name}"></use></svg>')


def render_markdown(text):
    html = md.markdown(str(text or ""), extensions=["nl2br", "sane_lists", "tables"])
    return Markup(nh3.clean(html, tags={"p", "br", "strong", "em", "b", "i", "ul", "ol", "li", "a", "code", "pre", "blockquote",
                                        "h1", "h2", "h3", "h4", "table", "thead", "tbody", "tr", "th", "td", "hr"},
                            attributes={"a": {"href", "title"}}, url_schemes={"http", "https", "mailto"}, link_rel="noopener noreferrer"))


def _here(name):
    """What the page being served has looked up (request.state: its engine, its workspace), or None outside a page."""
    request = current_request()
    return getattr(request.state, name, None) if request is not None else None


def when(stamp):
    """A stored UTC time (or its ISO text) as people read it, in the business's time: "9 Nov 2026, 8:41 am"."""
    from zoneinfo import ZoneInfo
    if isinstance(stamp, str):
        try:
            stamp = datetime.fromisoformat(stamp)
        except ValueError:
            return stamp
    if stamp is None:
        return ""
    engine = _here("engine")
    if engine is not None:
        stamp = stamp.replace(tzinfo=ZoneInfo("UTC")).astimezone(engine.now_tz)
    return f"{stamp.day} {stamp:%b %Y}, {stamp.hour % 12 or 12}:{stamp:%M} {'am' if stamp.hour < 12 else 'pm'}"


STATUS_WORDS = {"queued": "Starting", "running": "Running", "waiting_ai": "Waiting for the AI", "waiting_approval": "Waiting for an answer",
                "done": "Done", "skipped": "Didn't apply", "failed": "Stopped by a problem", "rejected": "Turned down",
                "pending": "Waiting", "approved": "Approved", "cancelled": "Cancelled", "escalated": "Passed up"}


def status_words(status):
    """A run's or a job's status as people say it: "Waiting for an answer", not "waiting_approval"."""
    return STATUS_WORDS.get(status or "", str(status or "").replace("_", " ").capitalize())


KIND_WORDS = {"text": "Text", "long_text": "Notes", "rich_text": "Formatted notes", "number": "Number", "money": "Money", "percent": "Percent",
              "duration": "Hours", "rating": "Stars", "date": "Date", "datetime": "Date and time", "time": "Time of day",
              "checkbox": "Yes or no", "select": "Choice", "multi_select": "Several choices", "tags": "Labels", "email": "Email",
              "phone": "Phone", "url": "Web address", "address": "Address", "location": "Place on a map", "barcode": "Barcode",
              "color": "Colour", "json": "Data from another system", "link": "Link", "user": "Person", "file": "Files",
              "image": "Picture", "signature": "Signature", "autonumber": "Numbered for you", "formula": "Worked out",
              "rollup": "Totalled for you", "lookup": "Shown from elsewhere", "created_time": "When added", "modified_time": "When changed",
              "created_by": "Added by", "modified_by": "Changed by", "button": "Button"}
AGGREGATE_WORDS = {"count": "How many", "sum": "Total", "avg": "Average", "min": "Smallest", "max": "Largest"}


def about_field(e, t, f):
    """A field as the setup page shows it to someone who isn't technical: (what kind it is, one line on where its value
    comes from), e.g. ("Totalled for you", "Total of its bid lines' Amount")."""
    from ..blueprint import expr, schema
    kind = KIND_WORDS.get(f["type"], f["type"].replace("_", " "))
    label = lambda table, key: (schema.field(e.tables.get(table), key) or {"label": key.replace("_", " ")})["label"]  # noqa: E731
    other = lambda key: (e.tables.get(key) or {"label": key.replace("_", " ")})["label"]  # noqa: E731
    if f["type"] in ("select", "multi_select"):
        return kind, " · ".join(f["options"])
    if f["type"] == "link":
        return "Picks from another list", f"{'several' if f.get('multiple') else 'one'} of your {other(f['table']).lower()}"
    if f["type"] == "lookup" and "where" in f:
        return kind, f"found in {other(f['table'])}: its {label(f['table'], f['field'].split('.')[-1]) if f['field'] else '…'}"
    if f["type"] == "lookup":
        link = schema.field(t, f["link"]) or {"label": f["link"], "table": ""}
        return kind, f"its {link['label']}'s {label(link.get('table'), f['field'].split('.')[-1]) if f['field'] else '…'}"
    if f["type"] == "formula":
        try:
            used = [schema.field(t, n)["label"] for n in sorted(expr.names_in(f["expression"])) if schema.field(t, n)]
        except Exception:  # noqa: BLE001 - a formula that can't be read is still shown, just without its parts
            used = []
        from ..engine import automations
        said = automations.expression_words(e, t["key"], f["expression"]) if t else ""
        if said and not said.startswith("worked out") and len(said) <= 60:  # “Total − Paid”, as it's worked out
            return kind, said
        return kind, ("from " + ", ".join(used[:-1]) + (" and " if len(used) > 1 else "") + used[-1]) if used else f["expression"]
    if f["type"] == "rollup":
        what = AGGREGATE_WORDS.get(f["aggregate"], f["aggregate"])
        of = f" {label(f['table'], f['field'])}" if f.get("field") else ""
        return kind, f"{what}{of} of {'its ' if f.get('via') else 'all the matching '}{other(f['table']).lower()}"
    if f["type"] == "autonumber":
        return kind, f"{f.get('prefix', '')}0001, {f.get('prefix', '')}0002…"
    return kind, ""


def rule_words(text):
    """A rule's sentence for the screen: each [detail] filled in from the record shown as a chip with what it is."""
    import re

    from markupsafe import escape
    out = str(escape(text or ""))
    return Markup(re.sub(r"\[([^\[\]]{1,60})\]", lambda m: f'<span class="chip-field" title="Filled in each time: the {m.group(1)}">'
                                                         f'{m.group(1)}</span>', out))


def ask_facts(e, record):
    """The few facts someone needs to answer an approval at a glance: who asked, how much, by when ("Sam Ortiz · $12,800.00
    · by 9 Nov 2026")."""
    if record is None:
        return ""
    t = e.tables.get(record.table) or {"fields": []}
    bits = [record.created_by] if record.created_by and "(" not in str(record.created_by) else []
    def rank(f):  # the whole amount (Total, with its tax) before a subtotal or a price
        key = f["key"]
        return (0 if re.fullmatch(r"(grand_)?total(_inc\w*)?", key) else 1 if re.search(r"total|amount|value", key) and "sub" not in key
                else 2 if re.search(r"total|price|cost", key) else 3, t["fields"].index(f))
    money = sorted((f for f in t["fields"] if e.kind(f) == "money" and e.sees(f)), key=rank)
    for f in money[:1]:
        v = e.value(record, f["key"])
        if v not in (None, "", 0):
            bits.append(f"{f['label']} {e.display(f, v, record)}")
    when = next((f for f in t["fields"] if f["type"] in ("date", "datetime") and any(w in f["key"] for w in ("due", "need", "by", "deadline", "close"))), None)
    if when is not None and e.value(record, when["key"]):
        bits.append(f"{lc(when['label'])} {e.display(when, e.value(record, when['key']), record)}")
    return " · ".join(str(b) for b in bits)


MORE_AFTER = 8  # a long form shows this many of its details before "More details (optional)"


def blank_in(f, values, errors):
    """A detail that can wait under "More details": nothing in it yet, and nothing to put right."""
    return values.get(f["key"]) in (None, "", [], False) and not errors.get(f["key"])


def _printer_for(ws, role):
    """The printer chosen for labels or documents, in words ("Brother QL-820NWB"), or None: print buttons send to it."""
    from .. import printing
    chosen = printing.chosen(ws, role)
    return printing.describe(chosen) if chosen else None


def _combined_from(t):
    """“Stock movements, purchase order lines and sales order lines”: what a combined list is put together from."""
    from ..engine.combined import sources
    engine = _here("engine")
    bp = engine.bp if engine is not None else {}
    names = [n.lower() for n in sources(bp, t)]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1] if names else "other lists"


def unnumbered(label):
    """A step's label without the number its builder put on it ("2 What to price"): the steps are numbered already."""
    return re.sub(r"^\s*(step\s*)?\d+\s*[.):-]?\s+", "", str(label or ""), flags=re.I) or label


def filled_on(f, engine, record):
    """Whether a record has something in a field worth showing (a total of 0 and a "No" still say something; an empty
    one is said in one line instead). One that must be filled in is always shown."""
    if f.get("required") or f["type"] in ("checkbox",):
        return True
    v = engine.value(record, f["key"])
    return v not in (None, "", [], "—")


def field_visible_on(f, engine, record):
    from .erp_records import visible
    return visible(engine, record, f)


def register(app):
    """The filters and globals every template can use, put into the platform's Jinja environment (create_app)."""
    app.jinja_env.tests["blank_in"] = blank_in
    app.jinja_env.tests["filled_on"] = filled_on
    app.jinja_env.tests["field_visible_on"] = field_visible_on
    app.jinja_env.tests["can_add_in"] = lambda t, engine: engine.can(t, "edit")
    app.jinja_env.filters.update(lc=lc, unnumbered=unnumbered, ago=ago, markdown=render_markdown, size=human_size, when=when, status_words=status_words, rule_words=rule_words,
                                 json=lambda v: escape(json.dumps(v, default=str)))  # for data-* attributes: quotes escaped, decoded by the browser
    from ..documents.labels import SIZES as LABEL_SIZES
    from ..blueprint.schema import says_no
    app.jinja_env.globals.update(says_no=says_no, pill=pill, icon=icon, now=datetime.now, about_field=about_field, ask_facts=ask_facts, MORE_AFTER=MORE_AFTER,
                                 LABEL_SIZES={k: v[0] for k, v in LABEL_SIZES.items()}, printer_for=_printer_for, combined_from=_combined_from)


@context_processor
def platform():
    """What every page's template knows: the platform's name, and the workspace and membership the page looked up."""
    return {"platform_name": current_app.config["PLATFORM_NAME"], "member": _here("member"), "ws": _here("ws")}
