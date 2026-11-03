"""Business documents (an invoice, a quote, a purchase order) produced as PDFs from records.

A table's document template says which fields go where; `produce` fills it from a record (its values, the linked
customer's details, its line items, its totals) and the workspace's business details and logo, renders it with
`pdf.render`, and keeps the PDF with the record.
"""
import hashlib
import re

from ..blueprint import expr, schema
from ..engine.records import RecordContext, RecordProxy
from ..extensions import db
from ..models import File
from ..storage import path_of, save
from . import pdf

NUMERIC = {"number", "money", "percent", "duration", "rating"}


def _template(engine, record, key):
    t = engine.tables[record.table]
    doc = next((d for d in t.get("documents", []) if d["key"] == key), None)
    if doc is None:
        raise ValueError(f"{t['label']} has no document “{key}”")
    return t, doc


def _pairs(engine, record, keys):
    out = []
    for k in keys:
        f = engine.field(record.table, k)
        if f is None:
            continue
        value = engine.display(f, engine.value(record, k), record)
        if value != "—":
            out.append({"label": f["label"], "value": value})
    return out


def _listing(engine, record, lines, sums):
    child = engine.tables[lines["table"]]
    columns = [schema.field(child, c) for c in lines["columns"] if schema.field(child, c)]
    if not columns:
        columns = [f for f in child["fields"] if f["key"] != lines["via"].split(".")[0] and f["type"] not in schema.FILES][:5]
    rows = engine.related(record, lines["table"], lines["via"])
    if lines.get("filters"):  # {{record.from}} in them is the record the document is of (a statement's period)
        rows = [r for r in rows if engine.matches(r, lines["filters"], about=record)]
    total_field = schema.field(child, lines.get("total") or "")
    if total_field is not None:  # a statement: what's owed across the listed records
        amount = engine.aggregate(rows, "sum", total_field["key"]) or 0
        name = (lines.get("label") or total_field["label"]).lower()
        sums.append((f"Less {name}" if lines.get("subtract") else f"Total {name}", -amount if lines.get("subtract") else amount, total_field))
    return {"columns": [{"label": c["label"], "align": "right" if engine.kind(c) in NUMERIC else "left",
                         "width": 1 if engine.kind(c) in NUMERIC or c["type"] in ("date", "select") else 3} for c in columns],
            "rows": [[engine.display(c, engine.value(r, c["key"]), r).replace("—", "") for c in columns] for r in rows]}


def build(engine, record, key):
    """The input for pdf.render: everything already formatted for people."""
    t, doc = _template(engine, record, key)
    settings = engine.ws.settings or {}
    business = dict(settings.get("business") or {})
    business.setdefault("name", engine.bp.get("name") or engine.ws.name)
    theme = {"accent": (engine.bp.get("theme") or {}).get("accent") or "#5b5bd6"}
    logo = db.session.get(File, settings.get("logo_file_id")) if settings.get("logo_file_id") else None
    if logo is not None and logo.deleted_at is None:
        theme["logo_path"] = str(path_of(logo))
    out = {"business": business, "theme": theme, "title": doc["title"], "page_size": settings.get("page_size", "A4")}
    if doc.get("number_field"):
        out["number"] = engine.display(engine.field(record.table, doc["number_field"]), engine.value(record, doc["number_field"]))
    out["fields"] = _pairs(engine, record, doc.get("fields", []))
    out["details"] = _pairs(engine, record, doc.get("details", []))
    to = doc.get("to")
    if to and to["field"] == "record":  # a statement to the customer or owner it's about
        lines = [engine.title(record)]
        for k in to.get("details", []):
            shown = engine.display(engine.field(record.table, k), engine.value(record, k))
            if shown != "—":
                lines += shown.splitlines()
        out["to"] = {"label": to.get("label") or "To", "lines": lines}
    elif to:
        f = engine.field(record.table, to["field"])
        value = engine.value(record, to["field"])
        if f is not None and value not in (None, ""):
            lines = []
            if f["type"] == "link":
                linked = engine.get(value, f["table"])
                if linked is not None:
                    lines.append(engine.title(linked))
                    for k in to.get("details", []):
                        shown = engine.display(engine.field(linked.table, k), engine.value(linked, k))
                        if shown != "—":
                            lines += shown.splitlines()
            else:
                lines = engine.display(f, value).splitlines()
            if lines:
                out["to"] = {"label": to.get("label") or "To", "lines": lines}
    sums = []  # (label, amount, field) of each list with a total: a statement's balance, an owner's rent less expenses
    lines = doc.get("lines")
    if lines and lines.get("table") in engine.tables:
        out["lines"] = _listing(engine, record, lines, sums)
    for section in doc.get("sections", []):
        if section.get("table") in engine.tables:
            listed = _listing(engine, record, section, sums)
            out.setdefault("sections", []).append({"heading": section.get("label") or engine.tables[section["table"]]["label"], **listed,
                                                   "note": "" if listed["rows"] else "None in this period."})
    totals = _pairs(engine, record, doc.get("totals", []))
    totals += [{"label": label, "value": engine.display(f, amount)} for label, amount, f in sums]
    if len(sums) > 1:
        totals.append({"label": doc.get("net") or "Net", "value": engine.display(sums[0][2], round(sum(a for _, a, _ in sums), 2))})
    if totals:
        totals[-1]["strong"] = True
        out["totals"] = totals
    ctx = RecordContext(engine, record)
    for part in ("notes", "footer"):
        if doc.get(part):
            try:
                out[part] = str(expr.render(doc[part], ctx))
            except expr.ExprError:
                out[part] = doc[part]
    return out, doc


def text_file(engine, record, key):
    """A file in another system's layout (a bank's payment file, a CSV for the accountant): (name, content, sha256,
    whether it has sensitive values in it). Sensitive values (bank accounts) are written in full: such a file is made
    when someone with access downloads it, and never kept."""
    import hashlib

    from ..engine.records import Engine
    t, doc = _template(engine, record, key)
    full = Engine(engine.ws, by=engine.by)  # its own engine, so the full values never reach a screen
    full.revealing = True
    record = full.get(record.id) or record
    secret = {f["key"] for table in engine.bp.get("tables", []) for f in table["fields"] if f.get("sensitive")}
    used = set(re.findall(r"\.(\w+)", " ".join(doc.get(p) or "" for p in ("header", "line", "footer"))))
    out = []
    ctx = RecordContext(full, record)
    if doc.get("header"):
        out.append(str(expr.render(doc["header"], ctx)))
    rows = full.related(record, doc["lines"]["table"], doc["lines"]["via"]) if doc.get("lines") else []
    if doc.get("lines", {}).get("filters"):
        rows = [r for r in rows if full.matches(r, doc["lines"]["filters"], about=record)]
    for i, row in enumerate(rows, start=1):
        if doc.get("line"):
            out.append(str(expr.render(doc["line"], RecordContext(full, record, {"line": RecordProxy(full, row), "n": i}))))
    if doc.get("footer"):
        out.append(str(expr.render(doc["footer"], RecordContext(full, record, {"count": len(rows)}))))
    content = ("\r\n".join(out) + "\r\n").encode("utf-8")
    number = engine.display(engine.field(record.table, doc["number_field"]), engine.value(record, doc["number_field"])) \
        if doc.get("number_field") else engine.title(record)
    name = f"{doc['label']} {number}.{doc['extension']}".replace("/", "-")
    return name, content, hashlib.sha256(content).hexdigest(), bool(secret & used)


def produce(engine, record, key, field="", by=None):
    """Render the record's document and keep it with the record (in `field`, or with its general attachments)."""
    t, doc = _template(engine, record, key)
    if doc.get("format") == "text":
        name, content, _, sensitive = text_file(engine, record, key)
        if sensitive:
            raise ValueError(f"{doc['label']} has bank details or other private values in it, so it's made when someone downloads it "
                             "and never kept")
        return save(engine.ws.id, content, name, source="generated", record_id=record.id, field=field or "", by=by or engine.by,
                    mime="text/plain")
    data, doc = build(engine, record, key)
    content = pdf.render(data)
    number = data.get("number") or f"{engine.tables[record.table]['singular']} {record.id}"
    name = f"{doc['label']} {number}.pdf".replace("/", "-")
    same = File.query.filter_by(workspace_id=engine.ws.id, record_id=record.id, source="generated", sha256=hashlib.sha256(content).hexdigest(),
                                deleted_at=None).first()
    if same is not None:  # nothing in it has changed since it was last made: that copy, not another
        return same
    return save(engine.ws.id, content, name, source="generated", record_id=record.id, field=field or "", by=by or engine.by,
                mime="application/pdf")
