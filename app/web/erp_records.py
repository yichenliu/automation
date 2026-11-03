"""Tables and records: the five views (list, board, calendar, gallery, timeline), record pages with their line items,
files, comments and history, the add/edit form, quick edits, bulk changes and exports."""
import calendar as cal
import csv
import io
import itertools
import re
from datetime import date, datetime, timedelta

from flask import Response, abort, current_app, flash, g, jsonify, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required

from ..blueprint import expr, schema
from ..blueprint.expr import to_number
from ..engine import automations
from ..engine.records import Conflict, FieldErrors
from ..extensions import db
from ..models import Approval, Comment, File, Membership, Record, RecordChange, Run, User, utcnow  # noqa: F401
from ..storage import FileRejected, path_of, save
from .filters import status_words
from .erp import bp, confirm_step_up, ctx, my_approvals

PAGE_SIZE = 50
INLINE = {"text", "number", "money", "percent", "date", "select", "checkbox", "email", "phone", "duration", "rating", "user", "barcode"}
SHEET = INLINE | {"datetime", "url", "link", "long_text", "address"}  # what the spreadsheet view edits in its cells


def _table(engine, key, action="view"):
    """The table as this person sees it (only fields they may see); 404 if they can't see it, 403 if they can't change
    it and want to."""
    t = engine.tables.get(key)
    if t is None:
        abort(404)
    if not engine.can(t):  # there, but kept from them: never "moved or deleted", which reads as lost work
        teams = {tm["key"]: tm["name"] for tm in schema.teams(engine.bp)}
        kept = [teams.get(k, k) for k in (t.get("access") or {}).get("teams") or []]
        abort(403, description=f"{t['label']} {'are' if t['label'].endswith('s') else 'is'} kept to "
                               + (" and ".join(kept) + "; ask the person who runs your app to add you to "
                                  + ("it" if len(kept) == 1 else "the one you need") if kept else "the people who run your app") + ".")
    if action == "edit" and not engine.can(t, "edit"):
        abort(403)
    return engine.visible(t)


SAFE_INLINE = {"image/png", "image/jpeg", "image/gif", "image/webp", "application/pdf"}


def may_open_file(engine, f):
    """A record's files are as private as the record (and a file field kept to a role, to that role). A file on no
    record: documents given to the AI are for everyone; mail and OneDrive files for editors up; anything else (an
    import, an upload being read) for whoever put it there and the admins."""
    if f.record_id:
        owner = engine.get(f.record_id)
        if owner is None or not engine.allowed(owner) or engine.sealed_from_me(owner):  # a sealed bid's attachments too
            return False
        field = engine.field(owner.table, f.field) if f.field else None
        return field is None or engine.sees(field)
    if f.source == "knowledge":
        return True
    if f.source in ("email", "onedrive"):
        return engine._at_least("editor")
    return engine._at_least("admin") or (f.uploaded_by_id is not None and f.uploaded_by_id == current_user.id)


def send_stored(f, download=False):
    """A kept file, served so it can't run script on this site: only images and PDFs open in the browser; anything
    else downloads, and nothing is served as a page."""
    import hashlib
    import mimetypes
    if f.sha256:  # checked on every opening: a stored file that's been changed on disk isn't served as if it were the original
        digest = hashlib.sha256()
        with open(path_of(f), "rb") as stream:
            for chunk in iter(lambda: stream.read(1 << 20), b""):
                digest.update(chunk)
        if digest.hexdigest() != f.sha256:
            current_app.logger.error("The file %s (id %s) doesn't match its fingerprint", f.name, f.id)
            abort(409, description=f"“{f.name}” has changed since it was stored, so it isn't shown. Tell an admin.")
    mime = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
    inline = mime in SAFE_INLINE and not download
    response = send_file(path_of(f), mimetype=mime if inline else "application/octet-stream", as_attachment=not inline, download_name=f.name)
    if mime != "application/pdf":
        response.headers["Content-Security-Policy"] = "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; sandbox"
    return response


def _record(engine, t, rid):
    r = engine.get(rid, t["key"])
    if r is None or not engine.allowed(r):  # not theirs: as if it weren't there (whether it is isn't theirs to know)
        abort(404)
    return r


def _ran(engine):
    """Tell the person what their change set off."""
    for run in engine.started:
        run = db.session.get(Run, run.id)
        if run.status == "done":
            what = "; ".join(s.get("result", "") for s in run.steps or [] if s.get("result") and not s.get("skipped") and not s.get("quiet"))
            flash(f"⚡ Then, automatically ({run.name}): {what}" if what else f"⚡ Then, automatically: {run.name}", "auto")
        elif run.status in ("queued", "waiting_ai"):
            flash(f"⚡ Then the AI started “{run.name}”: it finishes in a moment, in the background.", "auto")
        elif run.status == "waiting_approval":
            people = Approval.query.filter_by(run_id=run.id, status="pending").all()
            flash(f"⚡ Then: it's waiting for {automations._named(engine, people) if people else 'an answer'}"
                  + (" to approve it." if people and people[0].kind == "approval" else "."), "auto")
        elif run.status == "failed":
            flash(f"⚡ “{run.name}” couldn't finish: {run.error}", "error")


def columns(t, view):
    keys = (view or {}).get("columns") or [f["key"] for f in t["fields"] if f["type"] not in ("long_text", "rich_text", "file")][:7]
    return [schema.field(t, k) for k in keys if schema.field(t, k)]


def children(engine, t):
    """The line-item tables shown inside records of t, with their link field."""
    out = []
    for other in engine.bp.get("tables", []):
        # its parent is the record its first link reaches (a payment line's run, not the supplier it also names)
        parent = next((f for f in other["fields"] if f["type"] == "link" and not (engine.tables.get(f["table"]) or {}).get("embedded")), None)
        link = parent if parent is not None and parent["table"] == t["key"] else None
        if other.get("embedded") and link and engine.can(other):
            other = engine.visible(other)
            out.append({"table": other, "via": link, "fields": [f for f in other["fields"] if f["key"] != link["key"] and f["type"] not in schema.FILES]})
    return out


def _filters_from_args(t, view):
    filters, quick = list(view.get("filters") or []), {}
    for f in t["fields"]:
        wanted = request.args.get(f"f_{f['key']}")
        if wanted not in (None, ""):
            quick[f["key"]] = wanted
            if wanted == "__none__":
                filters.append({"field": f["key"], "op": "is_empty"})
            elif f["type"] == "checkbox":
                filters.append({"field": f["key"], "op": "equals" if wanted == "Yes" else "is_empty", **({"value": True} if wanted == "Yes" else {})})
            else:
                filters.append({"field": f["key"], "op": "equals", "value": wanted})
    return filters, quick


@bp.route("/t/<table>")
@login_required
def table(wid, table):
    ws, engine = ctx(wid)
    t = _table(engine, table)
    if t.get("embedded"):
        abort(404)
    views = t.get("views") or [{"key": "all", "label": "All", "type": "list"}]
    if all(v.get("filters") for v in views):  # every tab shows only some: one shows them all, so nothing looks missing
        views = views + [{"key": "_all", "label": "All", "type": "list"}]
    view = next((v for v in views if v["key"] == request.args.get("view")), views[0])
    filters, quick = _filters_from_args(t, view)
    sort = view.get("sort")
    if request.args.get("sort") and schema.field(t, request.args["sort"]) or request.args.get("sort") in ("created_at", "updated_at"):
        sort = {"field": request.args["sort"], "dir": "desc" if request.args.get("dir") == "desc" else "asc"}
    q = request.args.get("q", "")
    rows = engine.query(t["key"], filters, q=q, sort=sort)
    filterable = [f for f in t["fields"] if f["type"] in ("select", "link", "checkbox", "user")][:4]
    choices = {}
    for f in filterable:
        if f["type"] == "select":
            choices[f["key"]] = [(o, o) for o in f["options"]]
        elif f["type"] == "checkbox":
            choices[f["key"]] = [("Yes", "Yes"), ("No", "No")]
        elif f["type"] == "user":
            choices[f["key"]] = [(n, n) for n in engine.members().values()]
        else:
            choices[f["key"]] = [(engine.title(r), engine.title(r)) for r in engine.visible_rows(f["table"])][:300]
    from ..models import SavedView
    mine = SavedView.query.filter_by(workspace_id=ws.id, user_id=current_user.id, table=t["key"]).order_by(SavedView.id).all()
    common = dict(ws=ws, e=engine, t=t, views=views, view=view, total=len(rows), quick=quick, q=q, filterable=filterable, mine=mine,
                  here_query=request.query_string.decode()[:1000],
                  choices=choices, args={"view": view["key"], "q": q or None, **{f"f_{k}": v for k, v in quick.items()}})
    kind = view["type"]
    if kind == "board":
        group = schema.field(t, view["group_by"])
        lanes = [{"value": o, "label": o, "rows": []} for o in group["options"]] + [{"value": "", "label": f"No {schema.lc(group['label'])}", "rows": []}]
        by_value = {lane["value"].lower(): lane for lane in lanes}
        for r in rows:
            by_value.get(str((r.data or {}).get(group["key"]) or "").lower(), lanes[-1])["rows"].append(r)
        if not lanes[-1]["rows"]:
            lanes.pop()
        card_fields = [f for f in columns(t, None) if f["key"] not in (group["key"], t.get("title_field"))][:3]
        return render_template("app/board.html", lanes=lanes, group=group, card_fields=card_fields, **common)
    if kind == "calendar":
        return render_template("app/calendar.html", **common, **_calendar(engine, t, view, rows), feed=feed_url(engine, t, view))
    if kind == "gallery":
        image = schema.field(t, view.get("image_field") or "") or next((f for f in t["fields"] if f["type"] == "image"), None)
        card_fields = [f for f in columns(t, view) if f["key"] != t.get("title_field") and f["type"] not in schema.FILES][:4]
        return render_template("app/gallery.html", rows=rows[:200], image=image, card_fields=card_fields, **common)
    if kind == "timeline":
        return render_template("app/timeline.html", **common, **_timeline(engine, t, view, rows))
    if kind == "map":
        return render_template("app/map.html", **common, **_map(engine, t, view, rows))
    if kind == "tree":
        return render_template("app/tree.html", **common, **_tree(engine, t, view, rows))
    if kind == "sheet":
        cols = [f for f in columns(t, view) if f["type"] not in schema.FILES]
        return render_template("app/sheet.html", rows=rows[:500], columns=cols, editable=SHEET, more=max(len(rows) - 500, 0),
                               options=_options(engine, t), **common)
    page = max(int(request.args.get("page", 1)) if str(request.args.get("page", "1")).isdigit() else 1, 1)
    shown = rows[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
    cols = columns(t, view)
    totals = {f["key"]: engine.aggregate(rows, "sum", f["key"]) for f in cols if engine.summable(f)}
    group_field = schema.field(t, request.args.get("group") or view.get("group_by") or "")
    groups = None
    if group_field is not None:  # grouped under headings, each with its count and subtotals
        from .erp_pages import _label
        by = {}
        for r in rows:
            by.setdefault(_label(engine, t, group_field["key"], r), []).append(r)
        order = [label for label, _ in engine.group(rows, group_field["key"], "count")]
        groups = [{"label": label, "rows": by[label][:500], "count": len(by[label]),
                   "totals": {f["key"]: engine.aggregate(by[label], "sum", f["key"]) for f in cols if engine.summable(f)}}
                  for label in order if label in by]
        shown, page = rows, 1
    return render_template("app/table.html", rows=shown, columns=cols, sort=sort, page=page, totals=totals, groups=groups,
                           group_field=group_field, groupable=[f for f in t["fields"] if f["type"] in ("select", "link", "user", "checkbox", "date", "multi_select", "tags")],
                           tints={r.id: engine.colours(t, r) for r in shown} if t.get("colors") else {},
                           pages=max(1, -(-len(rows) // PAGE_SIZE)), inline=INLINE, **common)


def _map(engine, t, view, rows):
    """Records with a location, as pins: [{id, title, lat, lng, colour}] (the browser draws the map)."""
    colour = schema.field(t, view.get("color_by") or "") if view.get("color_by") else None
    pins, missing = [], 0
    for r in rows:
        where = engine.value(r, view["location_field"])
        if not isinstance(where, dict) or "lat" not in where:
            missing += 1
            continue
        tint = engine.colours(t, r)["row"] if t.get("colors") else None
        pins.append({"id": r.id, "title": engine.title(r), "lat": where["lat"], "lng": where["lng"],
                     "colour": tint or ((engine.display(colour, engine.value(r, colour["key"])) if colour else None)),
                     "url": url_for("app.record", wid=engine.ws.id, table=t["key"], rid=r.id)})
    return {"pins": pins, "missing": missing}


def _tree(engine, t, view, rows):
    """Records under their parents (accounts under their group, staff under their manager), any depth."""
    parent = view["parent_field"]
    ids = {r.id for r in rows}
    kids = {}
    for r in rows:
        up = (r.data or {}).get(parent)
        up = up[0] if isinstance(up, list) and up else up
        kids.setdefault(int(up) if str(up).isdigit() and int(up) in ids and int(up) != r.id else None, []).append(r)
    cols = [f for f in columns(t, view) if f["key"] not in (parent, t.get("title_field"))][:4]

    def grow(record, depth, seen):
        if record.id in seen or depth > 12:
            return None
        seen = seen | {record.id}
        children = [n for n in (grow(c, depth + 1, seen) for c in kids.get(record.id, [])) if n]
        return {"r": record, "children": children, "count": len(children) + sum(c["count"] for c in children)}

    roots = [n for n in (grow(r, 0, frozenset()) for r in kids.get(None, [])) if n]
    return {"roots": roots, "tree_columns": cols}


def _calendar(engine, t, view, rows):
    today = engine.today
    try:
        month = date.fromisoformat((request.args.get("month") or today.strftime("%Y-%m")) + "-01")
    except ValueError:
        month = today.replace(day=1)
    first = month - timedelta(days=month.weekday())
    weeks, day = [], first
    last = date(month.year, month.month, cal.monthrange(month.year, month.month)[1])
    colour_field = schema.field(t, view.get("color_by") or "")
    events = {}
    for r in rows:
        start = expr.to_date(engine.value(r, view["date_field"]))
        if start is None:
            continue
        end = expr.to_date(engine.value(r, view["end_field"])) if view.get("end_field") else None
        end = end if end and end >= start else start
        d = max(start, first)
        while d <= min(end, last + timedelta(days=6)) and (d - start).days < 62:
            events.setdefault(d, []).append({"r": r, "colour": (engine.value(r, colour_field["key"]) if colour_field else None)})
            d += timedelta(days=1)
    while day <= last or day.weekday() != 0:
        weeks.append([{"date": day + timedelta(days=i), "in_month": (day + timedelta(days=i)).month == month.month,
                       "events": events.get(day + timedelta(days=i), [])} for i in range(7)])
        day += timedelta(days=7)
    prev = (month - timedelta(days=1)).replace(day=1)
    nxt = (last + timedelta(days=1))
    undated = sum(1 for r in rows if engine.value(r, view["date_field"]) in (None, ""))
    out = {"weeks": weeks, "month": month, "prev": prev.strftime("%Y-%m"), "next": nxt.strftime("%Y-%m"), "today": today,
           "colour_field": colour_field, "undated": undated, "mode": "month"}
    if (request.args.get("mode") or view.get("mode")) == "week":  # a week, each day's records in time order: bookings, shifts, visits
        try:
            anchor = date.fromisoformat(request.args.get("week") or today.isoformat())
        except ValueError:
            anchor = today
        start = anchor - timedelta(days=anchor.weekday())
        days = []
        for i in range(7):
            d = start + timedelta(days=i)
            items = []
            for r in rows:
                when = expr.to_datetime(engine.value(r, view["date_field"]))
                until = expr.to_datetime(engine.value(r, view["end_field"])) if view.get("end_field") else None
                if when is None or not (when.date() <= d <= max((until or when).date(), when.date())):
                    continue
                timed = (when.hour, when.minute) != (0, 0) and when.date() == d
                items.append({"r": r, "time": f"{when.hour % 12 or 12}:{when:%M} {'am' if when.hour < 12 else 'pm'}" if timed else "",
                              "sort": when if timed else datetime.combine(d, datetime.min.time()),
                              "colour": engine.value(r, colour_field["key"]) if colour_field else None})
            days.append({"date": d, "items": sorted(items, key=lambda x: x["sort"])})
        out.update(mode="week", days=days, prev_week=(start - timedelta(days=7)).isoformat(), next_week=(start + timedelta(days=7)).isoformat())
    return out


def _timeline(engine, t, view, rows):
    items = []
    for r in rows:
        start = expr.to_date(engine.value(r, view["start_field"]))
        end = (expr.to_date(engine.value(r, view["end_field"])) if view.get("end_field") else None) or start
        if start:
            items.append({"r": r, "start": start, "end": max(end, start)})
    today = engine.today
    lo = min([i["start"] for i in items] + [today - timedelta(days=14)])
    hi = max([i["end"] for i in items] + [today + timedelta(days=30)])
    lo, hi = max(lo, today - timedelta(days=200)).replace(day=1), min(hi, today + timedelta(days=400))
    span = max((hi - lo).days, 1)
    months, m = [], lo
    while m <= hi:
        months.append({"label": m.strftime("%b %Y" if m.month == 1 or m == lo else "%b"), "left": 100 * (m - lo).days / span})
        m = (m.replace(day=28) + timedelta(days=4)).replace(day=1)
    colour_field = schema.field(t, view.get("color_by") or "") or next((f for f in t["fields"] if f["type"] == "select"), None)
    for i in items:
        s, e = max(i["start"], lo), min(i["end"], hi)
        i["left"], i["width"] = 100 * (s - lo).days / span, max(100 * ((e - s).days + 1) / span, 0.8)
        i["colour"] = engine.value(i["r"], colour_field["key"]) if colour_field else None
    items.sort(key=lambda i: i["start"])
    lane_field = schema.field(t, view.get("group_by") or "")
    if lane_field is not None:  # a resource schedule: one lane per technician, truck or room (a crew member's jobs in each)
        laned = []
        for i in items:
            value = engine.value(i["r"], lane_field["key"])
            names = [engine.display(lane_field, [v]) if lane_field["type"] in ("link", "user") else engine.display(lane_field, v)
                     for v in value] if isinstance(value, list) and value else [engine.display(lane_field, value) if value not in (None, "") else "Nobody"]
            laned += [(name, i) for name in names]
        order = sorted({name for name, _ in laned}, key=lambda n: (n == "Nobody", n.lower()))
        items = []
        for name in order:
            items.append({"lane": name})
            items += [dict(i) for n, i in laned if n == name]
    depends = schema.field(t, view.get("depends_field") or "")
    links = []
    if depends:  # what each task waits for: drawn as a line from the end of the one before to its start
        row_of = {i["r"].id: n for n, i in enumerate(items) if "r" in i}
        for n, i in enumerate(items):
            if "r" not in i:
                continue
            for before in engine.linked(i["r"], depends):
                if before.id in row_of:
                    b = items[row_of[before.id]]
                    late = i["start"] <= b["end"]
                    links.append({"x1": b["left"] + b["width"], "y1": row_of[before.id] + 0.5, "x2": i["left"], "y2": n + 0.5, "late": late})
                    if late:
                        i["late"] = f"starts before {engine.title(before)} ends"
    return {"items": items, "links": links, "months": months, "today_left": 100 * (today - lo).days / span if lo <= today <= hi else None,
            "colour_field": colour_field, "undated": sum(1 for r in rows if expr.to_date(engine.value(r, view["start_field"])) is None)}


@bp.route("/t/<table>/export.xlsx")
@login_required
def export_xlsx(wid, table):
    """The table as an Excel workbook: numbers, money and dates as real values, so formulas and pivots work on them."""
    from openpyxl import Workbook
    from openpyxl.styles import Font
    ws, engine = ctx(wid)
    t = _table(engine, table)
    book = Workbook()
    sheet = book.active
    sheet.title = t["label"][:31]
    fields = [f for f in t["fields"] if f["type"] not in schema.FILES and f["type"] != "button"]
    sheet.append(["id"] + [f["label"] for f in fields])
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    for r in engine.query(t["key"], *_filters_from_args(t, {})[:1]):
        row = [r.id]
        for f in fields:
            v = engine.value(r, f["key"])
            kind = engine.kind(f)
            if kind in ("number", "money", "percent", "duration", "rating") and expr.to_number(v) is not None and not isinstance(v, list):
                row.append(expr.to_number(v))
            elif kind == "date" and expr.to_date(v):
                row.append(expr.to_date(v))
            elif kind == "datetime" and expr.to_datetime(v):
                row.append(expr.to_datetime(v))
            elif kind == "checkbox":
                row.append(bool(v))
            else:
                shown = engine.display(f, v, r)
                row.append("" if shown == "—" else shown)
        sheet.append(row)
    for i, f in enumerate(fields, start=2):
        if engine.kind(f) == "money":
            for (cell,) in sheet.iter_rows(min_row=2, min_col=i, max_col=i):
                cell.number_format = "#,##0.00"
        elif engine.kind(f) == "percent":
            for (cell,) in sheet.iter_rows(min_row=2, min_col=i, max_col=i):
                cell.number_format = "0.0%"
    sheet.freeze_panes = "B2"
    out = io.BytesIO()
    book.save(out)
    out.seek(0)
    return send_file(out, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", as_attachment=True,
                     download_name=f"{t['key']}-{engine.today.isoformat()}.xlsx")


@bp.route("/t/<table>/deleted")
@login_required
def deleted(wid, table):
    """The table's recycle bin: deleted records, each one click from coming back."""
    ws, engine = ctx(wid, "editor")
    t = _table(engine, table, "edit")
    rows = Record.query.filter(Record.workspace_id == ws.id, Record.table == t["key"], Record.deleted_at.isnot(None)) \
        .order_by(Record.deleted_at.desc()).limit(300).all()
    rows = [r for r in rows if engine.allowed(r)]
    return render_template("app/deleted.html", ws=ws, e=engine, t=t, rows=rows)


@bp.route("/t/<table>/calendar.ics")
def calendar_feed(wid, table):
    """A calendar feed to subscribe to from Outlook, Google or Apple Calendar: the view's records on their dates. The
    address carries a signed token (made for one person), so it works without signing in and stops if they leave."""
    from itsdangerous import BadSignature, URLSafeSerializer
    from ..models import Workspace
    try:
        data = URLSafeSerializer(current_app.config["SECRET_KEY"], salt="calendar").loads(request.args.get("token", ""))
    except BadSignature:
        abort(404)
    ws = db.session.get(Workspace, wid)
    member = Membership.query.filter_by(workspace_id=wid, user_id=data.get("user")).first() if ws else None
    if member is None or member.role == "portal" or data.get("table") != table:
        abort(404)
    from ..engine.records import Engine
    engine = Engine(ws, by="calendar feed", member=member)
    t = engine.tables.get(table)
    view = next((v for v in (t or {}).get("views", []) if v["key"] == data.get("view") and v["type"] in ("calendar", "timeline")), None)
    if t is None or view is None or not engine.can(t):
        abort(404)
    start_key, end_key = view.get("date_field") or view.get("start_field"), view.get("end_field")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:-//{current_app.config['PLATFORM_NAME']}//EN", f"X-WR-CALNAME:{t['label']}"]
    stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
    for r in engine.query(table, view.get("filters")):
        start = expr.to_datetime(engine.value(r, start_key))
        if start is None:
            continue
        end = expr.to_datetime(engine.value(r, end_key)) if end_key else None
        timed = engine.kind(engine.field(table, start_key)) == "datetime"
        title = engine.title(r).replace(",", "\\,").replace(";", "\\;")
        url = f"{current_app.config['BASE_URL']}{url_for('app.record', wid=wid, table=table, rid=r.id)}"
        lines += ["BEGIN:VEVENT", f"UID:{ws.id}-{table}-{r.id}@keel", f"DTSTAMP:{stamp}", f"SUMMARY:{title}", f"URL:{url}"]
        if timed:
            lines += [f"DTSTART:{start:%Y%m%dT%H%M%S}", f"DTEND:{(end if end and end > start else start + timedelta(hours=1)):%Y%m%dT%H%M%S}"]
        else:
            last = (end or start).date() if end and end >= start else start.date()
            lines += [f"DTSTART;VALUE=DATE:{start:%Y%m%d}", f"DTEND;VALUE=DATE:{last + timedelta(days=1):%Y%m%d}"]
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return Response("\r\n".join(lines) + "\r\n", mimetype="text/calendar", headers={"Content-Disposition": f"inline; filename={table}.ics"})


def feed_url(engine, t, view):
    from itsdangerous import URLSafeSerializer
    token = URLSafeSerializer(current_app.config["SECRET_KEY"], salt="calendar").dumps({"user": current_user.id, "table": t["key"], "view": view["key"]})
    return f"{current_app.config['BASE_URL']}{url_for('app.calendar_feed', wid=engine.ws.id, table=t['key'], token=token)}"


@bp.route("/t/<table>/export.csv")
@login_required
def export(wid, table):
    ws, engine = ctx(wid)
    t = _table(engine, table)
    out = io.StringIO()
    writer = csv.writer(out)
    fields = [f for f in t["fields"] if f["type"] not in schema.FILES]
    writer.writerow(["id"] + [f["label"] for f in fields] + ["Created", "Updated"])
    for r in engine.visible_rows(t["key"]):
        writer.writerow([r.id] + [engine.display(f, engine.value(r, f["key"])).replace("—", "") for f in fields]
                        + [r.created_at.strftime("%Y-%m-%d %H:%M"), (r.updated_at or r.created_at).strftime("%Y-%m-%d %H:%M")])
    return Response("﻿" + out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={t['key']}-{engine.today.isoformat()}.csv"})


def _related(engine, t, record):
    """Lists of other records that link to this one (a client's projects, a project's invoices), not line items."""
    out = []
    for other in engine.bp.get("tables", []):
        if other.get("embedded") or not engine.can(other):
            continue
        other = engine.visible(other)
        for f in other["fields"]:
            if f["type"] == "link" and f["table"] == t["key"]:
                rows = [r for r in engine.children(other["key"], f["key"], record.id) if engine.allowed(r)]
                cols = [c for c in columns(other, next((v for v in other["views"] if v["type"] == "list"), {})) if c["key"] != f["key"]][:5]
                totals = {c["key"]: engine.aggregate(rows, "sum", c["key"]) for c in cols if engine.summable(c)}
                out.append({"table": other, "via": f, "rows": rows, "columns": cols, "totals": totals})
    return out


def _sections(t):
    """The record's fields grouped for its page and form: the table's sections, then any field not in one."""
    sections, placed = [], set()
    for s in t.get("sections", []):
        fields = [schema.field(t, k) for k in s["fields"] if schema.field(t, k)]
        if fields:
            sections.append({"label": s["label"], "fields": fields, "show_if": s.get("show_if")})
            placed |= {f["key"] for f in fields}
    rest = [f for f in t["fields"] if f["key"] not in placed]
    if rest and sections:
        sections.append({"label": "More", "fields": rest})
    elif rest:
        sections.append({"label": None, "fields": rest})
    return sections


def visible(engine, record, f):
    return engine.shown(engine.tables[record.table], f, record) if record is not None else not (f.get("show_if") or f.get("hide_if"))


@bp.app_template_global()
def field_visible(engine, record, f):
    return visible(engine, record, f)


@bp.route("/t/<table>/<int:rid>")
@login_required
def record(wid, table, rid):
    ws, engine = ctx(wid)
    t = _table(engine, table)
    r = _record(engine, t, rid)
    if t.get("embedded"):
        parent_link = next((f for f in t["fields"] if f["type"] == "link"), None)
        parent = engine.get(engine.value(r, parent_link["key"])) if parent_link else None
        if parent is not None:
            return redirect(url_for("app.record", wid=wid, table=parent.table, rid=parent.id))
    if audited(engine, t):  # who opened it is kept (bids, bank details)
        seen = RecordChange.query.filter(RecordChange.record_id == r.id, RecordChange.kind == "viewed", RecordChange.by_id == current_user.id,
                                         RecordChange.created_at >= utcnow() - timedelta(minutes=10)).first()
        if seen is None:
            db.session.add(RecordChange(workspace_id=ws.id, record_id=r.id, kind="viewed", by=current_user.display, by_id=current_user.id))
            db.session.commit()
    history = RecordChange.query.filter_by(record_id=r.id).order_by(RecordChange.id.desc()).limit(60).all()
    shown = {f["key"] for f in t["fields"]} | {"_files", "_shown"}
    for h in history:  # changes to fields this person can't see aren't shown to them either
        if h.changes and set(h.changes) - shown:
            db.session.expunge(h)
            h.changes = {k: v for k, v in h.changes.items() if k in shown}
    runs = Run.query.filter(Run.workspace_id == ws.id, Run.record_id == r.id, Run.automation != "_change").order_by(Run.id.desc()).limit(10).all()
    ai_work = []  # what the AI did here, and why
    for run in runs:
        a = next((x for x in engine.bp.get("automations", []) if x["key"] == run.automation), None)
        for step in run.steps or []:
            if step.get("ai"):
                found = schema.step_at(a["actions"], step["at"]) if a and step.get("at") else None
                ai_work.append({"run": run, "step": step, "title": automations.ai_title(engine, a, found) if found else step["action"].split(" — ")[0]})
    member = Membership.query.filter_by(workspace_id=ws.id, user_id=current_user.id).first()
    buttons = [a for a in engine.bp.get("automations", []) if a.get("enabled", True) and a["trigger"]["type"] == "button"
               and a["trigger"].get("table") == t["key"] and engine.matches(r, a["conditions"])]
    approvals = Approval.query.filter_by(workspace_id=ws.id, record_id=r.id, status="pending").order_by(Approval.id).all()
    decisions = (Approval.query.filter(Approval.workspace_id == ws.id, Approval.record_id == r.id, Approval.status.in_(["approved", "rejected", "done"]))
                 .order_by(Approval.id.desc()).limit(20).all())
    flow = t.get("workflow")
    stages = schema.stages(t) if flow else []
    stage = (r.data or {}).get(flow["field"]) if stages else None
    moves = engine.moves(r) if stages and engine.can(t, "edit") else []
    fills = {}  # moves that only need something filled in first: their button asks for it ("Delivered" asks the delivery date)
    for i, m, problem in moves:
        need = engine.to_fill(t, r, m) if problem else []
        if need and all(f["type"] in FILLABLE and engine.sees(f) for f in need) and not engine.move_problem(t, r, m, filled=True):
            fills[i] = [{"f": f, "options": (engine.choices(t, f, r) if f.get("options_by") else f.get("options")) if f["type"] == "select"
                         else list(engine.members().items()) if f["type"] == "user"
                         else [(x.id, engine.title(x)) for x in engine.visible_rows(f["table"])[:500]] if f["type"] == "link" else None}
                        for f in need]
    moves = sorted(((i, m, None if i in fills else problem) for i, m, problem in moves), key=lambda x: schema.says_no(x[1]["to"]))
    hidden = engine.sealed_from_me(r)
    comments = [] if hidden else Comment.query.filter_by(record_id=r.id).order_by(Comment.id).all()
    files = [] if hidden else File.query.filter_by(record_id=r.id, deleted_at=None).order_by(File.id.desc()).all()
    lines = []
    for child in children(engine, t):
        rows = engine.children(child["table"]["key"], child["via"]["key"], r.id)
        lines.append({**child, "rows": rows, "totals": {f["key"]: engine.aggregate(rows, "sum", f["key"]) for f in child["fields"] if engine.summable(f)}})
    locked = engine.locked(r)
    from ..models import Follow
    following = Follow.query.filter_by(user_id=current_user.id, record_id=r.id).first() is not None
    order = [x.id for x in engine.visible_rows(t["key"])]
    at = order.index(r.id) if r.id in order else -1
    neighbours = {"prev": order[at - 1] if at > 0 else None, "next": order[at + 1] if 0 <= at < len(order) - 1 else None,
                  "at": at + 1, "of": len(order)}
    live = [p for p in schema.portals(engine.bp) if p.get("enabled", True)]
    own = next((p for p in live if p["table"] == t["key"]), None)  # a client: who of theirs is in the client portal
    portal = own or next((p for p in live if t.get("portal") and schema.portal_shows(engine.tables, t, p["table"])), None)
    portal_on = portal is not None
    portal_people = None
    if own is not None:
        portal_people = [m.user.display for m in Membership.query.filter_by(workspace_id=ws.id, role="portal", contact_id=r.id)]
    sealed = engine.seal_note(r)
    others = [a for a in approvals if a.id not in ({x.id for x in my_approvals(ws, member)} if member else set())]
    waiting_text = (f"Waiting for {automations._named(engine, others)} to "
                    + {"task": "do their part", "review": "check the AI's work", "change": "approve a change"}.get(others[0].kind, "approve it") + ".") if others else ""
    return render_template("app/record.html", ws=ws, e=engine, t=t, r=r, sections=_sections(t), related=_related(engine, t, r),
                           sealed=sealed, hidden=hidden, waiting_text=waiting_text, journey=journey(engine, t, r), stepped=engine.stepped_up,
                           teams={tm["key"]: tm["name"] for tm in schema.teams(engine.bp)},
                           lines=lines, history=history, runs=runs, buttons=buttons, approvals=approvals, comments=comments,
                           files=files, member=member, documents=[d for d in t.get("documents", []) if not d.get("only_if") or engine.matches(r, d["only_if"])],
                           inline=INLINE, locked=locked, path=main_path(t, stage) if stages else [],
                           tint=engine.colours(t, r) if t.get("colors") else None,
                           tint_why=next((automations.describe_condition(engine, c, t["key"]) for rule in t.get("colors", []) if not rule.get("field")
                                          and engine.matches(r, rule["when"]) for c in rule["when"][:1]), None), portal_on=portal_on, portal=portal, portal_people=portal_people,
                           following=following, neighbours=neighbours, decisions=decisions, ai_work=ai_work, stages=stages, stage=stage, moves=moves,
                           fills=fills,
                           mine={a.id for a in my_approvals(ws, member)} if member else set(),
                           can_edit=engine.can(t, "edit") and not locked,
                           still_editable=set(engine.tables[t["key"]].get("lock_except") or []) if locked and engine.can(t, "edit") else set())


SIDE_STAGES = ("reject", "declin", "cancel", "suspend", "lost", "void", "withdraw", "hold", "fail", "refus", "closed lost", "not ")
FILLABLE = {"text", "long_text", "email", "phone", "url", "number", "money", "percent", "duration", "date", "datetime", "select",
            "user", "link", "rating"}  # what a move's button can ask for beside it


def main_path(t, stage):
    """The stages a record normally goes through, in order (Draft → Submitted → Approved → Ordered), for the strip on its
    page: side turns (Rejected, Suspended, On hold) only appear when the record is in one."""
    names, flow = schema.stages(t), t["workflow"]
    start = schema.start_stage(t) or (names[0] if names else None)
    path, at = [], start
    while at and at not in path and len(path) < 12:
        path.append(at)
        onward = [m["to"] for m in flow["transitions"] if (not m["from"] or at in m["from"]) and m["to"] not in path
                  and not any(w in m["to"].lower() for w in SIDE_STAGES)]
        at = onward[0] if onward else None
    if stage and stage not in path:
        path.append(stage)
    return path


def audited(engine, t):
    """Whether opening a table's records is kept in their history (it's sealed, or set so)."""
    if t.get("audit_views") or t.get("sealed"):
        return True
    return bool(t.get("embedded")) and any((engine.tables.get(f["table"]) or {}).get("sealed") for f in t["fields"] if f["type"] == "link")


def journey(engine, t, r):
    """The record's journey across the business (Submitted → Approved → Ordered → Delivered → Paid): each step done or
    not, with the records that did it (the purchase order, the delivery)."""
    steps = []
    for step in t.get("journey", []):
        found = []
        if step.get("table") in engine.tables:
            rows = [x for x in engine.related(r, step["table"], step.get("via") or "") if engine.allowed(x)]
            found = [x for x in rows if not step.get("when") or engine.matches(x, step["when"])]
            done = bool(found)
        else:
            done = not step.get("when") or engine.matches(r, step["when"])
        steps.append({"label": step["label"], "done": done, "records": [
            {"title": engine.title(x), "url": url_for("app.record", wid=engine.ws.id, table=x.table, rid=x.id),
             "stage": (x.data or {}).get((engine.tables[x.table].get("workflow") or {}).get("field", ""))} for x in found[:3]]})
    current = next((i for i, x in enumerate(steps) if not x["done"]), None)
    for i, x in enumerate(steps):
        x["current"] = i == current
    return steps


@bp.route("/t/<table>/suggest/<key>", methods=["POST"])
@login_required
def suggest(wid, table, key):
    """The AI's suggestion for one field while a person fills in the form (the request's description from what they
    typed, its category): nothing is saved; they keep it, change it or ignore it."""
    from .. import ai, intelligence
    ws, engine = ctx(wid, "editor")
    t = _table(engine, table, "edit")
    f = schema.field(t, key)
    if f is None or not f.get("ai") or not engine.sees(f):
        abort(404)
    data = {}
    for k, v in _form_values(t, engine).items():
        field = schema.field(t, k)
        if field is None or field["type"] not in schema.STORED or field.get("sensitive") or k == key:
            continue
        try:
            data[k] = engine.coerce(field, v)
        except ValueError:
            continue
    rid = request.form.get("_record", type=int)
    base = engine.get(rid, t["key"]) if rid else None
    draft = Record(workspace_id=ws.id, table=t["key"], data={**((base.data or {}) if base is not None else {}), **data})
    draft.id = base.id if base is not None else None
    try:
        answer = intelligence.propose_field(engine, draft, f, use_knowledge=f["ai"].get("use_knowledge"))
    except ai.AIUnavailable:
        return jsonify(ok=False, error="The AI isn't reachable right now; fill it in yourself, or try again in a minute.")
    except Exception as exc:  # noqa: BLE001 - a suggestion that fails leaves the form as it was
        current_app.logger.warning("A suggestion for %s failed: %s", key, exc)
        return jsonify(ok=False, error="The AI couldn't suggest anything this time.")
    value = answer.get("value")
    if value in (None, "", []):
        return jsonify(ok=False, error=answer.get("why") or "The AI had nothing to suggest from what's filled in so far.")
    return jsonify(ok=True, value=value, shown=engine.display(f, value), why=answer.get("why"), sure=answer.get("sure"))


@bp.route("/t/<table>/compare", methods=["GET", "POST"])
@login_required
def compare(wid, table):
    """Records side by side (bids for a tender, quotes, a contract's versions): each field across them, what differs
    marked, the lowest and highest numbers, and their lines lined up by item with each one's total."""
    ws, engine = ctx(wid)
    t = _table(engine, table)
    if request.method == "POST":
        return redirect(url_for("app.compare", wid=wid, table=t["key"], ids=request.form.getlist("ids")))
    ids = [int(x) for x in request.args.getlist("ids") if str(x).isdigit()][:8]
    rows = [r for r in (engine.get(x, t["key"]) for x in ids) if r is not None and engine.allowed(r)]
    if len(rows) < 2:
        flash(f"Choose two or more {schema.lc(t['label'])} to compare.", "info")
        return redirect(url_for("app.table", wid=wid, table=t["key"]))
    vt = engine.visible(t)
    fields = []
    for f in vt["fields"]:
        if f["type"] in schema.FILES | {"button", "rich_text", "json"} or f.get("sensitive"):
            continue
        values = [engine.value(r, f["key"]) for r in rows]
        if all(v in (None, "", []) for v in values):
            continue
        numbers = [to_number(v) if engine.kind(f) in ("money", "number", "percent", "duration", "rating") else None for v in values]
        known = [n for n in numbers if n is not None]
        shown = [engine.display(f, v, r) for v, r in zip(values, rows)]
        fields.append({"f": f, "shown": shown, "differs": len(set(shown)) > 1,
                       "low": min(known) if len(known) > 1 and len(set(known)) > 1 else None,
                       "high": max(known) if len(known) > 1 and len(set(known)) > 1 else None, "numbers": numbers})
    lines = []
    for child in children(engine, t):
        ct, via = child["table"], child["via"]
        keyf = next((f for f in ct["fields"] if f["type"] == "link" and f["key"] != via["key"]), None) \
            or next((f for f in ct["fields"] if f["type"] == "text"), None)
        amount = next((f for f in ct["fields"] if engine.summable(f)), None) \
            or next((f for f in ct["fields"] if engine.kind(f) == "money"), None)
        if keyf is None or amount is None:
            continue
        table_rows, order = {}, []
        for i, r in enumerate(rows):
            for line in engine.children(ct["key"], via["key"], r.id):
                label = engine.display(keyf, engine.value(line, keyf["key"]), line)
                if label not in table_rows:
                    table_rows[label] = [None] * len(rows)
                    order.append(label)
                value = to_number(engine.value(line, amount["key"]))
                table_rows[label][i] = (table_rows[label][i] or 0) + value if value is not None else table_rows[label][i]
        items = []
        for label in order:
            known = [v for v in table_rows[label] if v is not None]
            items.append({"label": label, "values": table_rows[label], "low": min(known) if len(set(known)) > 1 else None,
                          "missing": sum(1 for v in table_rows[label] if v is None)})
        totals = [sum(v for v in (table_rows[label][i] for label in order) if v is not None) for i in range(len(rows))]
        lines.append({"table": ct, "key": keyf, "amount": amount, "items": items, "totals": totals,
                      "low": min(totals) if len(set(totals)) > 1 else None})
    heads = [engine.title(r) for r in rows]
    if len(set(heads)) < len(heads):  # bids named after their tender: told apart by the first field that differs (the supplier)
        apart = next((x for x in fields if x["f"]["type"] in ("link", "text", "autonumber", "select") and len(set(x["shown"])) == len(rows)), None)
        heads = apart["shown"] if apart else [f"{h} #{r.id}" for h, r in zip(heads, rows)]
    return render_template("app/compare.html", ws=ws, e=engine, t=t, rows=rows, fields=fields, lines=lines, heads=heads,
                           only_differences=request.args.get("differences") == "1")


@bp.route("/t/<table>/<int:rid>/reveal/<key>", methods=["POST"])
@login_required
def reveal(wid, table, rid, key):
    """A sensitive field's real value (a bank account), for people who may change the record; kept in its history."""
    from ..engine.records import reveal as real
    ws, engine = ctx(wid, "editor")
    t = _table(engine, table, "edit")
    r = _record(engine, t, rid)
    f = schema.field(t, key)
    if f is None or not f.get("sensitive") or not engine.sees(f) or engine.sealed_from_me(r):
        abort(404)
    db.session.add(RecordChange(workspace_id=ws.id, record_id=r.id, kind="viewed", by=current_user.display, by_id=current_user.id,
                                changes={"_shown": [None, f["label"]]}))
    db.session.commit()
    return jsonify(value=real((r.data or {}).get(key)) or "")


def _form_fields(t):
    return [f for f in t["fields"] if (f["type"] in schema.STORED and f["type"] != "autonumber") or f["type"] in schema.FILES]


def _options(engine, t, extra_tables=()):
    out = {}
    for table_ in [t, *extra_tables]:
        for f in table_["fields"]:
            if f["type"] == "link" and f["table"] in engine.tables and f["key"] not in out:
                target = engine.tables[f["table"]]
                rows = engine.query(f["table"], sort={"field": schema.title_field(target), "dir": "asc"})[:1000]
                out[f["key"]] = [(x.id, engine.title(x)) for x in rows]
                parent = next((g for g in table_["fields"] if g["type"] == "link" and not (engine.tables.get(g["table"]) or {}).get("embedded")), None)
                fills = schema.fill_from(table_, f, target) if table_.get("embedded") and f is not parent else {}
                if fills:  # what each choice fills in beside it, for the form to do as it's picked
                    out[f["key"] + "__fill"] = {x.id: {ours: (x.data or {}).get(theirs) for ours, theirs in fills.items()
                                                       if (x.data or {}).get(theirs) not in (None, "", [])} for x in rows}
                if f.get("match"):  # each choice says what it belongs to, so the form can offer only the right ones
                    theirs, ours = next(iter(f["match"].items()))
                    out[f["key"] + "__match"] = {"field": ours, "by": {x.id: str((x.data or {}).get(theirs) or "") for x in rows}}
    return out


def _stage_locked(engine, t):
    """Whether this person moves the table's records between stages only with the workflow's buttons (all but the owner)."""
    return bool(t.get("workflow") and schema.stages(t) and engine.role not in (None, "owner"))


def _form_values(t, engine=None):
    data = {}
    skip = {t["workflow"]["field"]} if engine is not None and t.get("workflow") and schema.stages(t) else set()
    for f in _form_fields(t):
        if f["type"] in schema.FILES or f.get("readonly") or f["key"] in skip:
            continue
        if f["type"] == "checkbox":
            data[f["key"]] = bool(request.form.get(f["key"]))
        elif f["type"] == "multi_select" or (f["type"] in ("link", "user") and f.get("multiple")):
            data[f["key"]] = [v for v in request.form.getlist(f["key"]) if v]
        elif f["type"] == "percent" and request.form.get(f["key"], "").strip() and not request.form[f["key"]].strip().endswith("%"):
            data[f["key"]] = request.form[f["key"]].strip() + "%"
        else:
            data[f["key"]] = request.form.get(f["key"])
    return data


def _posted_lines(child):
    """Line items from the form: lines-<table>-<n>-<field>, with -id for existing ones and -remove to delete."""
    prefix = f"lines-{child['table']['key']}-"
    numbers = sorted({int(m.group(1)) for k in request.form for m in [re.match(re.escape(prefix) + r"(\d+)-", k)] if m})
    rows = []
    for n in numbers:
        get = lambda name: request.form.get(f"{prefix}{n}-{name}")  # noqa: E731
        values = {}
        for f in child["fields"]:
            if f["type"] in schema.CALCULATED:
                continue
            if f["type"] == "checkbox":
                values[f["key"]] = bool(get(f["key"]))
            else:
                values[f["key"]] = get(f["key"])
        rows.append({"id": get("id"), "remove": bool(get("remove")), "values": values,
                     "empty": all(v in (None, "", False) for v in values.values())})
    return rows


def _save_lines(engine, t, parent):
    """Create, change and remove the parent's line items as the form says. Returns {table: {row index: errors}}."""
    problems = {}
    for child in children(engine, t):
        key, via = child["table"]["key"], child["via"]["key"]
        for i, row in enumerate(_posted_lines(child)):
            existing = engine.get(row["id"], key) if row["id"] else None
            if existing is not None and str((existing.data or {}).get(via)) != str(parent.id):
                continue  # a line of another record: never touched from this one's form
            try:
                if existing is not None and (row["remove"] or row["empty"]):
                    engine.delete(existing)
                elif existing is not None:
                    engine.update(existing, row["values"])
                elif not row["remove"] and not row["empty"]:
                    engine.create(key, {**row["values"], via: parent.id})
            except FieldErrors as exc:
                problems.setdefault(key, {})[i] = "; ".join(exc.errors.values())
    return problems


def _save_files(ws, t, record_id):
    import base64
    for f in t["fields"]:
        if f["type"] == "signature":  # drawn in the browser: a PNG as a data URL
            drawn = request.form.get(f["key"]) or ""
            if drawn.startswith("data:image/png;base64,") and len(drawn) < 3_000_000:
                for old in File.query.filter_by(record_id=record_id, field=f["key"], deleted_at=None):
                    old.deleted_at = utcnow()
                save(ws.id, base64.b64decode(drawn.split(",", 1)[1]), f"{f['label']}.png", record_id=record_id, field=f["key"],
                     by=current_user.display, mime="image/png")
            continue
        if f["type"] not in schema.FILES:
            continue
        uploads = [u for u in request.files.getlist(f["key"]) if u and u.filename]
        if f["type"] == "image" and uploads:
            for old in File.query.filter_by(record_id=record_id, field=f["key"], deleted_at=None):
                old.deleted_at = utcnow()
            uploads = uploads[:1]
        for upload in uploads[:20]:
            save(ws.id, upload, upload.filename, record_id=record_id, field=f["key"], by=current_user.display)
    for fid in request.form.getlist("attach_file"):
        file = db.session.get(File, int(fid)) if str(fid).isdigit() else None
        if file is not None and file.workspace_id == ws.id and file.record_id is None:
            target = next((f["key"] for f in t["fields"] if f["type"] == "file"), "")
            file.record_id, file.field = record_id, target
    db.session.commit()


def _render_form(ws, engine, t, r, values, errors, line_rows=None, status=200, **extra):
    back = extra.get("back") or ""
    extra["back"] = back if back.startswith("/") and not back.startswith("//") else None  # only ever back into this site
    kids = children(engine, t)
    if line_rows is None:
        line_rows = {c["table"]["key"]: [{"id": x.id, "values": dict(x.data or {})} for x in engine.children(c["table"]["key"], c["via"]["key"], r.id)]
                     if r else [] for c in kids}
    stage_lock = None
    if t.get("workflow") and schema.stages(t):  # the stage moves by the record's buttons, for everyone; new ones start at the start
        key = t["workflow"]["field"]
        stage_lock = {"key": key, "value": (r.data or {}).get(key) if r is not None else schema.start_stage(t),
                      "note": "changed with the buttons on it" if r is not None else f"every new {schema.lc(t['singular'])} starts here"}
    first_move = None
    if r is None and t.get("workflow") and schema.stages(t):  # what a new one is usually sent on to straight away
        ways = [(i, m) for i, m in schema.sending_on(engine.bp, t)
                if (not m.get("who") or "creator" in m["who"] or any(engine.is_who(w, None) for w in m["who"] if w in ("owner", "admin", "editor")))
                and not m.get("step_up")]
        first_move = ways[0] if ways else None
        if len(ways) > 1 and any(m.get("only_if") for _, m in ways):  # which way depends on what's filled in: worked out once it's saved
            labels = [(m.get("label") or "").split() for _, m in ways]
            same = [words[0] for words in itertools.takewhile(lambda words: len(set(words)) == 1, zip(*labels))]
            first_move = (-1, {"label": " ".join(same) if same else "Save and send on", "to": ""})
    shown_keys = {f["key"] for s_ in _sections(t) for f in s_["fields"]} | {c["table"]["key"] for c in kids}
    hidden = {k: v for k, v in (errors or {}).items() if k != "_" and (k not in shown_keys or (stage_lock and k == stage_lock["key"] and r is None))}
    if hidden:  # a problem with something the form doesn't show (the stage, a rule on two fields): said at the top
        errors = {**{k: v for k, v in errors.items() if k not in hidden}, "_": "; ".join(([errors["_"]] if errors.get("_") else []) + list(hidden.values()))}
    return render_template("app/form.html", ws=ws, e=engine, t=t, r=r, values=values, errors=errors, sections=_sections(t), stage_lock=stage_lock,
                           first_move=first_move,
                           options=_options(engine, t, [c["table"] for c in kids]), children=kids, line_rows=line_rows,
                           members=engine.members(), locked=engine.locked(r) if r is not None else None, **extra), status


@bp.route("/t/<table>/new", methods=["GET", "POST"])
@login_required
def new(wid, table):
    ws, engine = ctx(wid, "editor")
    t = _table(engine, table, "edit")
    if request.method == "POST":
        try:
            with engine.atomic():  # the record and its lines are saved together, once its rules hold
                r = engine.create(t["key"], _form_values(t, engine), automations=False)
                line_problems = _save_lines(engine, t, r)
        except FieldErrors as exc:
            posted = {c["table"]["key"]: [{"id": x["id"], "values": x["values"]} for x in _posted_lines(c)] for c in children(engine, t)}
            return _render_form(ws, engine, t, None, request.form, exc.errors, posted, 400, back=request.form.get("back"),
                                attach=request.form.getlist("attach_file"))
        try:
            _save_files(ws, t, r.id)
        except FileRejected as exc:
            flash(str(exc), "error")
        engine.reset()
        automations.on_change(engine, engine.get(r.id), None)
        then = request.form.get("then", type=int)
        moved = None
        flow = t.get("workflow")
        now = (engine.get(r.id).data or {}).get(flow["field"]) if flow else None
        if then is not None and flow and now != schema.start_stage(t):  # a rule sent it on as it was saved: that was the send
            then, moved = None, now
        if then == -1:  # whichever way on its conditions allow ("Send": for approval over a 10% discount, straight out under)
            onward = {i for i, _ in schema.sending_on(engine.bp, t)}
            then = next((i for i, m, problem in engine.moves(engine.get(r.id)) if not problem and not m.get("step_up") and i in onward), None) \
                if t.get("workflow") else None
            if then is None:
                problem = next((p_ for _, _, p_ in engine.moves(engine.get(r.id)) if p_), "nothing it can move on to yet")
                flash(f"Saved as {engine.get(r.id).data.get(t['workflow']['field'])}, but not sent on yet: {problem[:1].lower() + problem[1:].rstrip('.')}.", "error")
                moved = False
        if then is not None:  # "Submit for approval" straight from the form
            try:
                engine.move(engine.get(r.id), then)
                moved = engine.get(r.id).data.get(t["workflow"]["field"])
            except FieldErrors as exc:
                stage = engine.get(r.id).data.get(t["workflow"]["field"])
                reason = "; ".join(exc.errors.values())
                flash(f"Saved as {stage}, but not sent on yet: {reason[:1].lower() + reason[1:].rstrip('.')}.", "error")
                moved = False
        if moved is not False:
            flash(f"Done: {schema.lc(t['singular'])} “{engine.title(engine.get(r.id))}” is {'now ' + moved if moved else 'saved'}.", "success")
        for table_, rows in line_problems.items():
            flash(f"Some {schema.lc(engine.tables[table_]['label'])} weren't saved: " + "; ".join(rows.values()), "error")
        _ran(engine)
        back = request.form.get("back")
        if request.form.get("another"):
            return redirect(url_for("app.new", wid=wid, table=t["key"]))
        saved = engine.get(r.id)
        if saved is None or not engine.allowed(saved):  # saved, but not theirs to open: never "We can't find this page"
            flash(f"Your role can't open this {schema.lc(t['singular'])} now it's saved; whoever looks after {schema.lc(t['label'])} can.", "info")
            if not engine.can(t, "view"):
                return redirect(url_for("app.home", wid=wid))
            return redirect(url_for("app.table", wid=wid, table=t["key"]))
        return redirect(back if back and back.startswith("/") and not back.startswith("//") else url_for("app.record", wid=wid, table=t["key"], rid=r.id))
    values = {k[2:]: v for k, v in request.args.items() if k.startswith("f_")}
    for f in _form_fields(t):
        if f["key"] not in values and f.get("default") not in (None, ""):
            default = f["default"]
            values[f["key"]] = engine.today.isoformat() if f["type"] == "date" and str(default).lower() in ("today", "{{today()}}", "now") else default
    return _render_form(ws, engine, t, None, values, {}, back=request.args.get("back"))[0]


@bp.route("/t/<table>/quick", methods=["POST"])
@login_required
def quick_add(wid, table):
    """Add a record from a few values: the spreadsheet view's new row (JSON) or a form on a dashboard (a form post that
    comes back to the page)."""
    ws, engine = ctx(wid, "editor")
    t = _table(engine, table, "edit")
    data = request.get_json(silent=True)
    values = data if isinstance(data, dict) else {f["key"]: (request.form.getlist(f["key"]) if f["type"] == "multi_select" or f.get("multiple")
                                                             else request.form.get(f["key"]) if f["type"] != "checkbox" else bool(request.form.get(f["key"])))
                                                  for f in _form_fields(t) if f["key"] in request.form or f["type"] == "checkbox" and f"has_{f['key']}" in request.form}
    values = {k: v for k, v in values.items() if schema.field(t, k)}
    back = request.form.get("back") if data is None else None
    try:
        record = engine.create(t["key"], values)
    except FieldErrors as exc:
        if data is not None:
            return jsonify(ok=False, errors=exc.errors, error="; ".join(exc.errors.values())), 400
        flash(f"{t['singular']} not added: " + "; ".join(exc.errors.values()), "error")
        return redirect(back if back and back.startswith("/") and not back.startswith("//") else url_for("app.table", wid=wid, table=table))
    if data is not None:
        engine.reset()
        r = engine.get(record.id)
        return jsonify(ok=True, id=record.id, url=url_for("app.record", wid=wid, table=table, rid=record.id),
                       shown={f["key"]: engine.display(f, engine.value(r, f["key"]), r) for f in t["fields"]})
    _ran(engine)
    flash(f"Added {schema.lc(t['singular'])} “{engine.title(engine.get(record.id))}”.", "success")
    return redirect(back if back and back.startswith("/") and not back.startswith("//") else url_for("app.record", wid=wid, table=table, rid=record.id))


@bp.route("/t/<table>/<int:rid>/edit", methods=["GET", "POST"])
@login_required
def edit(wid, table, rid):
    ws, engine = ctx(wid, "editor")
    t = _table(engine, table, "edit")
    r = _record(engine, t, rid)
    if request.method == "POST":
        try:
            with engine.atomic():  # refused whole if someone else saved it since the form was opened, or a rule fails
                diff = engine.update(r, _form_values(t, engine), expected_version=request.form.get("_version"))
                line_problems = _save_lines(engine, t, r)
        except FieldErrors as exc:
            posted = {c["table"]["key"]: [{"id": x["id"], "values": x["values"]} for x in _posted_lines(c)] for c in children(engine, t)}
            r = _record(engine, t, rid)
            errors = exc.errors
            if isinstance(exc, Conflict):
                errors = {"_": "Someone else saved this " + t["singular"].lower() + " while you were editing. Your changes are below: "
                               "check them against theirs (open it in another tab), then save again to keep yours."}
            return _render_form(ws, engine, t, r, request.form, errors, posted, 409 if isinstance(exc, Conflict) else 400)
        try:
            _save_files(ws, t, r.id)
        except FileRejected as exc:
            flash(str(exc), "error")
        held = engine.held
        engine.reset()
        flash(f"Saved {schema.lc(t['singular'])} “{engine.title(engine.get(r.id))}”." if diff or not line_problems else "Saved.", "success")
        if held:
            flash(f"{', '.join(held)}: your change waits for someone else's approval before it counts.", "info")
        for table_, rows in line_problems.items():
            flash(f"Some {schema.lc(engine.tables[table_]['label'])} weren't saved: " + "; ".join(rows.values()), "error")
        _ran(engine)
        return redirect(url_for("app.record", wid=wid, table=t["key"], rid=r.id))
    return _render_form(ws, engine, t, r, dict(r.data or {}), {})[0]


@bp.route("/t/<table>/<int:rid>/set", methods=["POST"])
@login_required
def set_value(wid, table, rid):
    """One field, from a board drag or an edit in place."""
    ws, engine = ctx(wid, "editor")
    t = _table(engine, table, "edit")
    r = _record(engine, t, rid)
    data = request.get_json(silent=True) or request.form
    f = schema.field(t, data.get("field"))
    if f is None or f["type"] not in schema.STORED or f["type"] == "autonumber":
        abort(400)
    try:
        engine.update(r, {f["key"]: data.get("value")})
    except FieldErrors as exc:
        return jsonify(ok=False, error="; ".join(exc.errors.values())), 400
    r = engine.get(r.id)
    messages = [f"{', '.join(engine.held)}: your change waits for someone else's approval before it counts"] if engine.held else []
    for run in engine.started:
        run = db.session.get(Run, run.id)
        messages.append(f"⚡ {run.name}: " + ("; ".join(s.get("result", "") for s in run.steps or [] if s.get("result")) or status_words(run.status)))
    return jsonify(ok=True, shown=engine.display(f, engine.value(r, f["key"])), ran=messages)


@bp.route("/t/<table>/bulk", methods=["POST"])
@login_required
def bulk(wid, table):
    ws, engine = ctx(wid, "editor")
    t = _table(engine, table, "edit")
    ids = [int(i) for i in request.form.getlist("ids") if i.isdigit()]
    rows = [r for r in (engine.get(i, t["key"]) for i in ids) if r is not None and engine.allowed(r)]
    action = request.form.get("action")
    if action == "delete":
        done, kept = 0, []
        for r in rows:
            try:
                engine.delete(r)
                done += 1
            except FieldErrors:
                kept.append(engine.title(r))
        flash(f"Deleted {done} {schema.lc(t['label']) if done != 1 else schema.lc(t['singular'])}."
              + (f" Locked, so not deleted: {', '.join(kept[:5])}{'…' if len(kept) > 5 else ''}" if kept else ""), "success" if not kept else "error")
    elif action and (action == "labels" or action.startswith("labels:")):  # a label for each one selected, ready to print
        return _labels_pdf(engine, t, rows, action[7:] or None, request.form.get("size"))
    elif action and action.startswith("pdf:"):  # one PDF per selected record, in a zip (mail merge, month-end statements)
        import zipfile
        from ..documents import build
        from ..documents import pdf as pdf_
        key = action[4:]
        if not any(d["key"] == key for d in t.get("documents", [])):
            abort(404)
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for r in rows[:500]:
                data, doc = build(engine, r, key)
                name = f"{doc['label']} {data.get('number') or engine.title(r)}.pdf".replace("/", "-")
                z.writestr(name, pdf_.render(data))
        out.seek(0)
        return send_file(out, mimetype="application/zip", as_attachment=True, download_name=f"{t['key']}-{key}-{engine.today.isoformat()}.zip")
    elif action == "set":
        f = schema.field(t, request.form.get("field"))
        if f is None or f["type"] not in schema.STORED:
            abort(400)
        done, failed = 0, []
        for r in rows:
            try:
                engine.update(r, {f["key"]: request.form.get("value")})
                done += 1
            except FieldErrors as exc:
                failed.append(f"{engine.title(r)}: {'; '.join(exc.errors.values())}")
        flash(f"Set {f['label']} on {done} {schema.lc(t['label']) if done != 1 else schema.lc(t['singular'])}." + (f" Not changed: {'; '.join(failed[:3])}" if failed else ""),
              "success" if not failed else "error")
        _ran(engine)
    return redirect(request.referrer or url_for("app.table", wid=wid, table=t["key"]))


@bp.route("/t/<table>/<int:rid>/delete", methods=["POST"])
@login_required
def delete(wid, table, rid):
    ws, engine = ctx(wid, "editor")
    t = _table(engine, table, "edit")
    r = _record(engine, t, rid)
    name = engine.title(r)
    try:
        engine.delete(r)
    except FieldErrors as exc:
        flash("; ".join(exc.errors.values()), "error")
        return redirect(url_for("app.record", wid=wid, table=t["key"], rid=r.id))
    flash(f"Deleted {schema.lc(t['singular'])} “{name}”.|{url_for('app.restore', wid=wid, table=t['key'], rid=rid)}", "undo")
    return redirect(url_for("app.table", wid=wid, table=t["key"]))


@bp.route("/t/<table>/<int:rid>/restore", methods=["POST"])
@login_required
def restore(wid, table, rid):
    ws, engine = ctx(wid, "editor")
    _table(engine, table, "edit")
    r = db.session.get(Record, rid)
    if r is None or r.workspace_id != ws.id or r.table != table or not engine.allowed(r):
        abort(404)
    engine.restore(r)
    flash(f"Brought back “{engine.title(r)}”.", "success")
    return redirect(url_for("app.record", wid=wid, table=table, rid=rid))


@bp.route("/t/<table>/<int:rid>/press/<key>", methods=["POST"])
@login_required
def press(wid, table, rid, key):
    ws, engine = ctx(wid, "editor")
    t = _table(engine, table, "edit")
    r = _record(engine, t, rid)
    automation = next((a for a in engine.bp.get("automations", []) if a["key"] == key and a["trigger"]["type"] == "button"), None)
    if automation is None or not automation.get("enabled", True):
        abort(404)
    automations.press(engine, automation, r, current_user.display)
    _ran(engine)
    return redirect(url_for("app.record", wid=wid, table=t["key"], rid=r.id))


@bp.route("/t/<table>/<int:rid>/move/<int:index>", methods=["POST"])
@login_required
def move(wid, table, rid, index):
    """A workflow button on the record: Submit, Approve, Mark paid."""
    ws, engine = ctx(wid, "editor")
    t = _table(engine, table, "edit")
    r = _record(engine, t, rid)
    flow = (t.get("workflow") or {}).get("transitions") or []
    if 0 <= index < len(flow) and flow[index].get("step_up") and request.form.get("step_up_code") and not confirm_step_up():
        flash("That code (or password) wasn't right.", "error")
        return redirect(url_for("app.record", wid=wid, table=t["key"], rid=r.id))
    filled = {k: request.form[f"fill_{k}"] for k in (flow[index].get("requires") or [] if 0 <= index < len(flow) else [])
              if request.form.get(f"fill_{k}", "").strip()}  # what the button asked for beside it
    try:
        if filled:
            engine.update(r, filled)
            r = engine.get(r.id)
        engine.move(r, index, by=current_user.display)
    except FieldErrors as exc:
        flash("; ".join(exc.errors.values()), "error")
        return redirect(url_for("app.record", wid=wid, table=t["key"], rid=r.id))
    except Conflict:
        flash("Someone else changed this record just now. Here's the latest; try again.", "error")
        return redirect(url_for("app.record", wid=wid, table=t["key"], rid=r.id))
    flash(f"Done: this {schema.lc(t['singular'])} is now {engine.get(r.id).data.get(t['workflow']['field'])}.", "success")
    _ran(engine)
    return redirect(url_for("app.record", wid=wid, table=t["key"], rid=r.id))


@bp.route("/t/<table>/<int:rid>/stage", methods=["POST"])
@login_required
def set_stage(wid, table, rid):
    """The owner putting a record in another stage by hand (fixing a mistake); everyone else uses its buttons."""
    ws, engine = ctx(wid, "owner")
    t = _table(engine, table, "edit")
    r = _record(engine, t, rid)
    stage = request.form.get("stage")
    if not t.get("workflow") or stage not in schema.stages(t):
        abort(400)
    try:
        engine.update(r, {t["workflow"]["field"]: stage})
    except FieldErrors as exc:
        flash("; ".join(exc.errors.values()), "error")
    else:
        flash(f"Done: this {schema.lc(t['singular'])} is now {stage}.", "success")
        _ran(engine)
    return redirect(url_for("app.record", wid=wid, table=t["key"], rid=r.id))


@bp.route("/t/<table>/<int:rid>/document/<key>", methods=["POST"])
@login_required
def document(wid, table, rid, key):
    from ..documents import produce
    ws, engine = ctx(wid, "editor")
    t = _table(engine, table, "edit")
    r = _record(engine, t, rid)
    doc = next((d for d in t.get("documents", []) if d["key"] == key), None)
    if doc is not None and doc.get("only_if") and not engine.matches(r, doc["only_if"]):
        flash(f"{doc['label']} isn't ready yet: only when {automations.describe_condition(engine, doc['only_if'][0], t['key']).lower()}.", "error")
        return redirect(url_for("app.record", wid=wid, table=t["key"], rid=r.id))
    if doc is not None and doc.get("format") == "text":  # a file in another system's layout: made now, downloaded, not kept
        from ..documents import text_file
        if any(f.get("sensitive") and not engine.sees(f) for f in t["fields"]):
            abort(403)
        try:
            name, content, sha, _ = text_file(engine, r, key)
        except expr.ExprError as exc:
            flash(f"{doc['label']} couldn't be made: its layout has a mistake ({exc}). Ask the builder to fix it.", "error")
            return redirect(url_for("app.record", wid=wid, table=t["key"], rid=r.id))
        db.session.add(RecordChange(workspace_id=ws.id, record_id=r.id, kind="updated", by=current_user.display, by_id=current_user.id,
                                    changes={"_files": [None, f"Made {name} (fingerprint {sha[:12]})"]}))
        db.session.commit()
        response = Response(content, mimetype="application/octet-stream")
        response.headers["Content-Disposition"] = f'attachment; filename="{name}"'
        response.headers["X-Content-SHA256"] = sha
        return response
    try:
        file = produce(engine, r, key, by=current_user.display)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("app.record", wid=wid, table=t["key"], rid=r.id))
    flash(f"Produced {file.name}.", "success")
    return redirect(url_for("app.file", wid=wid, fid=file.id))


@bp.route("/t/<table>/<int:rid>/label")
@login_required
def label(wid, table, rid):
    """The record's label as a PDF to print: its own design ("doc"), or the plain one every list has."""
    ws, engine = ctx(wid)
    t = _table(engine, table)
    r = _record(engine, t, rid)
    return _labels_pdf(engine, t, [r], request.args.get("doc") or None, request.args.get("size"))


def _labels_pdf(engine, t, rows, key, size):
    from flask import session
    from ..documents import labels
    doc = next((d for d in t.get("documents", []) if d["key"] == key and d.get("format") == "label"), None)
    if key and doc is None:
        abort(404)
    size = labels.size_of(size or (doc or {}).get("size") or session.get("label_size"))
    session["label_size"] = size  # the printer they have: chosen once
    link = lambda r: url_for("app.record", wid=engine.ws.id, table=t["key"], rid=r.id, _external=True)  # noqa: E731
    content = labels.render(labels.for_records(engine, rows, doc, link), size)
    name = f"{(doc or {}).get('label') or t['singular']} label{'s' if len(rows) != 1 else ''}.pdf"
    response = Response(content, mimetype="application/pdf")
    response.headers["Content-Disposition"] = f'inline; filename="{name}"'  # opens to print, rather than downloading
    return response


@bp.route("/scan")
@login_required
def scan(wid):
    """What a scanned code is: one of this app's own labels (its QR code is the record's address), a barcode a record
    has, or a record's number or name. Straight to it; to the search otherwise."""
    ws, engine = ctx(wid)
    code = (request.args.get("code") or "").strip()
    if not code:
        return redirect(url_for("app.home", wid=wid))
    own = re.search(rf"/w/{wid}/app/t/(\w+)/(\d+)\b", code)
    if own:
        return redirect(url_for("app.record", wid=wid, table=own.group(1), rid=int(own.group(2))))
    found = engine.by_code(code)
    if found is not None:
        return redirect(url_for("app.record", wid=wid, table=found.table, rid=found.id))
    flash(f"Nothing has the code “{code}”, so here's everything that mentions it.", "info")
    return redirect(url_for("app.search", wid=wid, q=code))


@bp.route("/t/<table>/<int:rid>/files", methods=["POST"])
@login_required
def upload(wid, table, rid):
    ws, engine = ctx(wid, "editor")
    t = _table(engine, table, "edit")
    r = _record(engine, t, rid)
    field = request.form.get("field", "")
    if field and (schema.field(t, field) or {}).get("type") not in schema.FILES:
        abort(400)
    reason = engine.locked(r)
    if reason and field not in (t.get("lock_except") or []):  # a signed contract's files stay as they were signed
        flash(reason, "error")
        return redirect(url_for("app.record", wid=wid, table=t["key"], rid=r.id))
    added = 0
    for up in request.files.getlist("files")[:20]:
        if up and up.filename:
            try:
                save(ws.id, up, up.filename, record_id=r.id, field=field, by=current_user.display)
                added += 1
            except FileRejected as exc:
                flash(str(exc), "error")
    if added:
        db.session.add(RecordChange(workspace_id=ws.id, record_id=r.id, kind="updated", by=current_user.display,
                                    changes={"_files": [None, f"added {added} file{'s' if added != 1 else ''}"]}))
        db.session.commit()
    return redirect(url_for("app.record", wid=wid, table=t["key"], rid=r.id))


@bp.route("/files/<int:fid>")
@login_required
def file(wid, fid):
    ws, engine = ctx(wid)
    f = db.session.get(File, fid)
    if f is None or f.workspace_id != ws.id or f.deleted_at is not None:
        abort(404)
    if not may_open_file(engine, f):
        abort(404)
    return send_stored(f, download=request.args.get("download") == "1")


@bp.route("/files/<int:fid>/delete", methods=["POST"])
@login_required
def delete_file(wid, fid):
    ws, engine = ctx(wid, "editor")
    f = db.session.get(File, fid)
    if f is None or f.workspace_id != ws.id or not may_open_file(engine, f):
        abort(404)
    owner = engine.get(f.record_id) if f.record_id else None
    if owner is not None:
        if not engine.can(owner.table, "edit") or engine.locked(owner):
            abort(403)
    elif not (g.member.can("admin") or f.uploaded_by_id == current_user.id):
        abort(403)
    f.deleted_at = utcnow()
    db.session.commit()
    flash(f"Removed {f.name}.", "success")
    return redirect(request.referrer or url_for("app.home", wid=wid))


@bp.route("/t/<table>/<int:rid>/comments", methods=["POST"])
@login_required
def comment(wid, table, rid):
    ws, engine = ctx(wid)
    t = _table(engine, table)
    r = _record(engine, t, rid)
    text = (request.form.get("text") or "").strip()[:5000]
    if text:
        db.session.add(Comment(workspace_id=ws.id, record_id=r.id, user_id=current_user.id, text=text, shared=bool(request.form.get("shared"))))
        db.session.commit()
        from ..models import Notification
        members = {name.lower(): uid for uid, name in engine.members().items()}
        lowered = text.lower()
        for name, uid in members.items():
            if uid != current_user.id and f"@{name}" in lowered:
                db.session.add(Notification(workspace_id=ws.id, user_id=uid, kind="mention", link=f"table:{t['key']}:record:{r.id}",
                                            text=f"{current_user.display} mentioned you on {t['singular']} “{engine.title(r)}”: {text[:200]}"))
        db.session.commit()
    return redirect(url_for("app.record", wid=wid, table=t["key"], rid=r.id) + "#comments")


@bp.route("/t/<table>/my-views", methods=["POST"])
@login_required
def save_view(wid, table):
    """Keep the current filters, search, sorting and grouping as one of this person's own views (or forget one)."""
    from urllib.parse import parse_qsl, urlencode
    from ..models import SavedView
    ws, engine = ctx(wid)
    t = _table(engine, table)
    if request.form.get("forget"):
        gone = db.session.get(SavedView, int(request.form["forget"])) if request.form["forget"].isdigit() else None
        if gone is None or gone.user_id != current_user.id or gone.workspace_id != ws.id:
            abort(404)
        db.session.delete(gone)
        db.session.commit()
        return redirect(url_for("app.table", wid=wid, table=table))
    label = (request.form.get("label") or "").strip()[:80] or "My view"
    query = urlencode([(k, v) for k, v in parse_qsl(request.form.get("query") or "") if k not in ("page", "my")])
    saved = SavedView(workspace_id=ws.id, user_id=current_user.id, table=t["key"], label=label, params=query)
    db.session.add(saved)
    db.session.commit()
    flash(f"Saved “{label}” as one of your views.", "success")
    return redirect(url_for("app.table", wid=wid, table=table) + f"?{query}&my={saved.id}".replace("?&", "?"))


@bp.route("/t/<table>/<int:rid>/follow", methods=["POST"])
@login_required
def follow(wid, table, rid):
    from ..models import Follow
    ws, engine = ctx(wid)
    t = _table(engine, table)
    r = _record(engine, t, rid)
    existing = Follow.query.filter_by(user_id=current_user.id, record_id=r.id).first()
    if existing:
        db.session.delete(existing)
        flash(f"You've stopped following “{engine.title(r)}”.", "success")
    else:
        db.session.add(Follow(workspace_id=ws.id, user_id=current_user.id, record_id=r.id))
        flash(f"Following “{engine.title(r)}”: you'll be told when someone changes it.", "success")
    db.session.commit()
    return redirect(url_for("app.record", wid=wid, table=table, rid=rid))


@bp.route("/t/<table>/<int:rid>/duplicate", methods=["POST"])
@login_required
def duplicate(wid, table, rid):
    """A copy of a record and its lines (not its number, unique values or files), opened for editing."""
    ws, engine = ctx(wid, "editor")
    t = _table(engine, table, "edit")
    r = _record(engine, t, rid)
    full = engine.tables[t["key"]]
    values = {f["key"]: (r.data or {}).get(f["key"]) for f in full["fields"]
              if f["type"] in schema.STORED and f["type"] != "autonumber" and not f.get("unique") and not f.get("readonly")}
    title = schema.title_field(full)
    if title and isinstance(values.get(title), str):
        values[title] = f"{values[title]} (copy)"[:1000]
    try:
        with engine.atomic():
            copy = engine.create(t["key"], values, automations=False)
            for child in children(engine, t):
                key, via = child["table"]["key"], child["via"]["key"]
                for line in engine.children(key, via, r.id):
                    data = {k: v for k, v in (line.data or {}).items() if not k.startswith("_") and k != via
                            and (schema.field(engine.tables[key], k) or {}).get("type") in schema.STORED - {"autonumber"}}
                    engine.create(key, {**data, via: copy.id}, automations=False, ai=False)
    except FieldErrors as exc:
        flash("Couldn't copy it: " + "; ".join(exc.errors.values()), "error")
        return redirect(url_for("app.record", wid=wid, table=table, rid=rid))
    flash("Copied. This is the copy: change what's different and save.", "success")
    return redirect(url_for("app.edit", wid=wid, table=table, rid=copy.id))


@bp.route("/t/<table>/<int:rid>/portal-invite", methods=["POST"])
@login_required
def portal_invite(wid, table, rid):
    """Invite the person a record is (a customer, a supplier) to the portal: their account sees only their records."""
    import secrets
    from ..models import Invite
    ws, engine = ctx(wid, "admin")
    t = _table(engine, table)
    r = _record(engine, t, rid)
    portal = schema.portal_of(engine.bp, t["key"])
    if portal is None:
        abort(404)
    email = str(engine.value(r, portal["email_field"]) or "").strip().lower()
    if "@" not in email:
        flash(f"Give {engine.title(r)} an email address first; that's how they sign in.", "error")
        return redirect(url_for("app.record", wid=wid, table=table, rid=rid))
    invite = Invite(workspace_id=ws.id, email=email, role="portal", contact_id=r.id, token=secrets.token_urlsafe(24), invited_by=current_user.id)
    db.session.add(invite)
    db.session.commit()
    link = f"{current_app.config['BASE_URL']}/invite/{invite.token}"
    sent = False
    try:
        from ..connectors import microsoft
        if microsoft.status(ws).get("connected"):
            microsoft.send_mail(ws, email, f"Your access to the {portal['title']}",
                                f"Hello {engine.title(r)},\n\nYou can now see your records with us online. Set up your access here:\n\n{link}")
            sent = True
    except Exception as exc:  # the link still works; it just has to be sent by hand
        current_app.logger.warning("Couldn't email the portal invitation: %s", exc)
    flash(f"Invitation for {email} made" + (" and emailed." if sent else f". Send them this link: {link}"), "success")
    return redirect(url_for("app.record", wid=wid, table=table, rid=rid))


@bp.route("/people")
@login_required
def people(wid):
    """Members for @mentions and person pickers (JSON)."""
    ws, engine = ctx(wid)
    return jsonify([{"id": uid, "name": name} for uid, name in engine.members().items()])


def user_name(uid):
    u = db.session.get(User, uid)
    return u.display if u else "someone"
