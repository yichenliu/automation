"""Importing a spreadsheet (CSV, Excel) into a table.

The columns are matched to the table's fields: by name first, then the AI suggests matches for the rest. A person can
change the matches before importing. Rows are imported in the background; each is cleaned like anything typed in, and
rows that can't be imported are listed with the reason, so nothing goes missing silently.
"""
import json
import re
from difflib import SequenceMatcher

from . import ai, jobs
from .blueprint import schema
from .extensions import db
from .models import File, Workspace
from .storage import path_of

IMPORTABLE = schema.STORED - {"autonumber"}


def _norm(text):
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def preview(file, sheet=None, rows=8):
    from .files import readers
    content = readers.read(path_of(file), name=file.name, max_rows=rows)
    if content.error:
        raise ValueError(content.error)
    if not content.sheets:
        raise ValueError(f"“{file.name}” isn't a spreadsheet (CSV or Excel).")
    chosen = next((s for s in content.sheets if sheet and s.name.lower() == str(sheet).lower()), content.sheets[0])
    return {"sheets": [s.name for s in content.sheets], "sheet": chosen.name, "headers": chosen.headers,
            "rows": chosen.rows[:rows], "row_count": chosen.row_count}


def match_by_name(table, headers):
    fields = [f for f in table["fields"] if f["type"] in IMPORTABLE]
    mapping, used = {}, set()
    for h in headers:
        best, score = None, 0.0
        for f in fields:
            if f["key"] in used:
                continue
            for name in (f["label"], f["key"]):
                s = 1.0 if _norm(name) == _norm(h) else SequenceMatcher(None, _norm(name), _norm(h)).ratio()
                if s > score:
                    best, score = f, s
        if best is not None and score >= 0.82:
            mapping[h] = best["key"]
            used.add(best["key"])
    return mapping


def suggest(engine, table_key, headers, rows):
    """{column: field key} for every column that fits a field: matched by name, then by the AI for the rest."""
    t = engine.tables[table_key]
    mapping = match_by_name(t, headers)
    left = [h for h in headers if h not in mapping]
    free = [f for f in t["fields"] if f["type"] in IMPORTABLE and f["key"] not in mapping.values()]
    if not left or not free:
        return mapping
    samples = {h: [r[headers.index(h)] for r in rows[:5] if headers.index(h) < len(r)] for h in left}
    schema_ = {"type": "object", "properties": {"matches": {"type": "array", "items": {"type": "object", "required": ["column", "field"], "properties": {
        "column": {"type": "string", "enum": left}, "field": {"type": "string", "enum": [f["key"] for f in free] + ["none"]}}}}}, "required": ["matches"]}
    prompt = (f"Match spreadsheet columns to the fields of the {t['label']} table. Only match a column to a field that means "
              f"the same thing; use \"none\" when nothing fits.\n\nColumns with example values:\n{json.dumps(samples, default=str, ensure_ascii=False)}"
              f"\n\nFields:\n" + "\n".join(f"- {f['key']}: {f['label']} ({f['type']}{', options: ' + ', '.join(f['options']) if f.get('options') else ''})" for f in free))
    try:
        content, _ = ai.chat([{"role": "user", "content": prompt}], schema=schema_, think=False)
        for m in ai.parse_json(content).get("matches") or []:
            if m.get("column") in left and m.get("field") not in (None, "none") and m["field"] not in mapping.values():
                mapping[m["column"]] = m["field"]
    except ai.AIError:
        pass  # matching by name still stands; the person can match the rest
    return {h: mapping[h] for h in headers if h in mapping}


def start(workspace, file_id, table, mapping=None, sheet=None, by=None, match_on=None):
    file = db.session.get(File, file_id)
    if file is None or file.workspace_id != workspace.id:
        raise ValueError("That file isn't in this workspace.")
    info = preview(file, sheet)
    return jobs.enqueue("import", {"file_id": file.id, "table": table, "mapping": mapping, "sheet": info["sheet"], "match_on": match_on,
                                   "rows": info["row_count"], "name": file.name}, workspace_id=workspace.id, by=by)


@jobs.handler("import")
def _run(job):
    from .engine.records import Engine, FieldErrors
    from .files import readers
    from .models import User
    ws = db.session.get(Workspace, job.workspace_id)
    who = db.session.get(User, job.created_by)
    engine = Engine(ws, by=f"import of {job.payload['name']}" + (f" by {who.display}" if who else ""))
    table = job.payload["table"]
    file = db.session.get(File, job.payload["file_id"])
    headers, rows = readers.sheet_rows(path_of(file), sheet=job.payload.get("sheet"), name=file.name)
    mapping = job.payload.get("mapping")
    if not mapping:
        jobs.stage(job, "Matching the columns to fields", 5)
        info = preview(file, job.payload.get("sheet"))
        mapping = suggest(engine, table, headers, info["rows"])
        job.payload = {**job.payload, "mapping": mapping}
        db.session.commit()
    columns = [(headers.index(h), key) for h, key in mapping.items() if h in headers and key]
    if not columns:
        raise ValueError("None of the spreadsheet's columns match a field of the table.")
    columns, linked, lines = _shape(engine, table, columns)
    parents, made_linked, line_count = {}, set(), 0
    title = schema.title_field(engine.tables[table])
    total = job.payload.get("rows") or 0
    added, updated, errors = 0, 0, []
    match_on = job.payload.get("match_on")
    existing = {}
    if match_on:  # rows that match a record already there update it (a refreshed price list, a new stock count)
        for r in engine.rows(table):
            value = (r.data or {}).get(match_on)
            if value not in (None, ""):
                existing[str(value).strip().lower()] = r
    for number, row in enumerate(rows, start=2):
        cell = lambda i: row[i] if i < len(row) and row[i] not in (None, "") else None  # noqa: E731
        data = {key: cell(i) for i, key in columns if cell(i) is not None}
        if not data:
            continue
        try:
            with engine.atomic():  # a row's order, its new customer and its line go in together or not at all
                for key, parts in linked.items():  # the customer named on the row: found, or added with its details
                    if data.get(key) is not None:
                        data[key], new = _linked(engine, engine.field(table, key), data[key], {k: cell(i) for i, k in parts if cell(i) is not None})
                        if new is not None:
                            made_linked.add((new.table, new.id))
                group = str(data.get(match_on) or data.get(title) or "").strip().lower() if lines else ""
                parent = parents.get(group) if group else None  # a sheet with a row per line: one order, several lines
                found = existing.get(str(data.get(match_on, "")).strip().lower()) if match_on and data.get(match_on) not in (None, "") else None
                if parent is not None:
                    pass
                elif found is not None:
                    parent = found
                    if engine.update(found, data, automations=False):
                        updated += 1
                else:
                    parent = engine.create(table, data, automations=False, ai=False, keep_numbers=True)
                    if match_on and data.get(match_on) not in (None, ""):
                        existing[str(data[match_on]).strip().lower()] = parent
                    added += 1
                if group:
                    parents[group] = parent
                for child, (via, parts) in lines.items():
                    values = {k: cell(i) for i, k in parts if cell(i) is not None}
                    for k in list(values):  # the product on the line: found, or added
                        f = engine.field(child, k)
                        if f is not None and f["type"] == "link" and not f.get("multiple"):
                            values[k], new = _linked(engine, f, values[k], {})
                            if new is not None:
                                made_linked.add((new.table, new.id))
                    if values:
                        engine.create(child, {**values, via: parent.id}, automations=False, ai=False)
                        line_count += 1
        except FieldErrors as exc:
            errors.append({"row": number, "problem": "; ".join(f"{(engine.field(table, k) or {'label': k})['label']}: {v}" for k, v in exc.errors.items())})
        if (added + updated + len(errors)) % 25 == 0:
            jobs.stage(job, f"Imported {added + updated} of {total} rows", 10 + 85 * (added + updated + len(errors)) / max(total, 1))
    t = engine.tables[table]
    from .engine import automations
    also = [f"{line_count} line{'s' if line_count != 1 else ''}" if line_count else "",
            f"{len(made_linked)} new {'linked records' if len(made_linked) != 1 else 'linked record'}" if made_linked else ""]
    also = [x for x in also if x]
    automations.notify(engine, f"Imported {added} {schema.lc(t['label']) if added != 1 else schema.lc(t['singular'])} from {file.name}"
                       + (f" (with {' and '.join(also)})" if also else "") + (f" and updated {updated}" if updated else "")
                       + (f"; {len(errors)} row{'s' if len(errors) != 1 else ''} couldn't be imported" if errors else ""), f"table:{table}")
    return {"added": added, "updated": updated, "skipped": len(errors), "errors": errors[:500], "table": table, "mapping": mapping,
            "lines": line_count, "linked_added": len(made_linked)}


def _shape(engine, table, columns):
    """Columns for the table itself; "customer.email" for the linked customer; "order_lines.quantity" for a line of the
    record, made on every row."""
    plain, linked, lines = [], {}, {}
    named = {key for _, key in columns}
    for i, key in columns:
        head, _, rest = key.partition(".")
        if _itself(engine, table, key) and head not in named:
            plain.append((i, head))
            continue
        if not rest:
            plain.append((i, key))
            continue
        f = engine.field(table, head)
        child = engine.tables.get(head)
        if f is not None and f["type"] == "link":
            linked.setdefault(head, []).append((i, rest))
        elif child is not None:
            via = next((x["key"] for x in child["fields"] if x["type"] == "link" and x["table"] == table), None)
            if via:
                lines.setdefault(head, (via, []))[1].append((i, _itself(engine, head, rest) or rest))
    for i, key in plain:  # a name in a link column is found, or added, even with no details for it
        f = engine.field(table, key)
        if f is not None and f["type"] == "link" and not f.get("multiple"):
            linked.setdefault(key, [])
    return plain, linked, lines


def _itself(engine, table, key):
    """"customer.name" names the customer itself when "name" is its title: then the link's key, else None."""
    head, _, rest = key.partition(".")
    f = engine.field(table, head)
    if rest and f is not None and f["type"] == "link" and rest == schema.title_field(engine.tables[f["table"]]):
        return head
    return None


def _linked(engine, f, value, details):
    """The record a link column names: the one with that name, or a new one with the row's details for it. Returns
    (its id, the new record or None)."""
    found = engine._find_linked(f["table"], value)
    if found is not None:
        return found.id, None
    target = engine.tables[f["table"]]
    title = schema.title_field(target)
    made = engine.create(f["table"], {**details, **({title: value} if title else {})}, automations=False, ai=False)
    return made.id, made
