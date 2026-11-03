"""The ERP itself: every screen is drawn from the workspace's blueprint. The routes live in erp_records (tables and
records), erp_pages (dashboards, automations, approvals, history) and erp_ai (the assistant, reading documents, imports);
this module holds what they share."""
from flask import Blueprint, g, url_for
from flask_login import current_user
from sqlalchemy import or_

from ..blueprint import schema
from ..engine.records import Engine
from ..extensions import db
from ..models import Approval, Notification
from ..security import step_up, stepped_up, workspace

bp = Blueprint("app", __name__, url_prefix="/w/<int:wid>/app")


def ctx(wid, role="viewer"):
    """The workspace (checked against the person's role) and an engine acting as them: it shows them only the tables,
    records and fields their role allows."""
    ws = workspace(wid, role)
    g.engine = Engine(ws, by=current_user.display, member=g.member)
    g.engine.stepped_up = stepped_up()
    return ws, g.engine


def confirm_step_up():
    """A form that needs a confirmed sign-in carries the person's code (or password) as "step_up_code": checked here, it
    counts for the engine of this request too. Returns whether they're confirmed now."""
    from flask import request
    if stepped_up():
        return True
    ok = step_up(current_user, request.form.get("step_up_code"))
    if ok and g.get("engine") is not None:
        g.engine.stepped_up = True
    return ok


def my_space(engine):
    """The team menu this person sees (spaces in the design), or None for the whole menu: admins and the owner, people
    in no team that has one, and anyone who chose to see everything."""
    from flask import session
    if engine is None or engine.role is None or engine._at_least("admin"):
        return None
    if engine.ws.id in (session.get("whole_menu") or []):
        return None
    return next((sp for sp in schema.spaces(engine.bp) if set(sp["for"]) & engine.teams or engine.role in sp["for"]), None)


def _space_sections(engine, space):
    from ..engine import flows
    visible = engine.visible_blueprint()
    tables = {t["key"]: t for t in visible.get("tables", [])}
    pages = {p["key"]: p for p in visible.get("pages", [])}
    starts = {f["key"] for f in flows.in_menu(engine)}
    sections = []
    for group in space["groups"]:
        items = []
        for item in group["items"]:
            if item.get("table") in tables:
                t = tables[item["table"]]
                view = next((v for v in t.get("views", []) if v["key"] == item.get("view")), None)
                # a view that picks out work (to pick, low on stock, waiting for approval) says how many are in it now
                count = len(engine.query(t["key"], view["filters"])) if view and view.get("filters") else None
                items.append({"kind": "table", "key": t["key"], "view": item.get("view") if view else None, "icon": t.get("icon") or "table",
                              "label": item.get("label") or (view["label"] if view and view["label"] not in ("All", t["label"]) else t["label"]),
                              "count": count})
            elif item.get("page") in pages:
                p = pages[item["page"]]
                items.append({"kind": "page", "key": p["key"], "label": p["label"], "icon": p.get("icon") or "layout-dashboard"})
            elif item.get("flow") in starts:
                flow = flows.of(engine, item["flow"])
                items.append({"kind": "flow", "key": flow["key"], "label": flow["label"], "icon": flow.get("icon") or "list-todo"})
        if items:
            sections.append({"label": group["label"] or None, "items": items})
    if space["links"]:
        sections.append({"label": "Useful links", "items": [{"kind": "link", "url": x["url"], "label": x["label"], "icon": "external-link"}
                                                            for x in space["links"]]})
    sections.append({"label": None, "items": [{"kind": "all", "label": "Everything else", "icon": "layout-grid"}]})
    return sections


def nav(ws):
    """The ERP's menu: its sections, each with its pages and tables, then anything without a section. Line-item tables
    (edited inside their parent) and tables this person may not see aren't listed. Someone whose team has its own menu
    (a space) sees that instead."""
    engine = g.get("engine")
    space = my_space(engine) if engine is not None and engine.ws.id == ws.id else None
    if space is not None:
        return _space_sections(engine, space)
    bp_ = engine.visible_blueprint() if engine is not None and engine.ws.id == ws.id else ws.blueprint or {}
    tables = [t for t in bp_.get("tables", []) if not t.get("embedded")]
    sections, used = [], set()
    for m in bp_.get("modules", []):
        items = [{"kind": "page", "key": p["key"], "label": p["label"], "icon": p.get("icon") or "layout-dashboard"}
                 for p in bp_.get("pages", []) if p.get("module") == m["key"]]
        items += [{"kind": "table", "key": t["key"], "label": t["label"], "icon": t.get("icon") or "table"} for t in tables if t.get("module") == m["key"]]
        if items:
            sections.append({"label": m["label"], "items": items})
            used |= {(i["kind"], i["key"]) for i in items}
    loose = [{"kind": "page", "key": p["key"], "label": p["label"], "icon": p.get("icon") or "layout-dashboard"}
             for p in bp_.get("pages", []) if ("page", p["key"]) not in used]
    loose += [{"kind": "table", "key": t["key"], "label": t["label"], "icon": t.get("icon") or "table"} for t in tables if ("table", t["key"]) not in used]
    if loose:
        sections.insert(0, {"label": None, "items": loose})
    if (engine is not None and engine.ws.id == ws.id and bp_.get("flows")) and _flows_offered(engine):
        sections.insert(0, {"label": None, "items": [{"kind": "flows", "key": "flows", "label": "Step by step", "icon": "list-todo"}]})
    if engine is not None and engine.ws.id == ws.id and engine.role is not None and not engine._at_least("admin") \
            and any(set(sp["for"]) & engine.teams or engine.role in sp["for"] for sp in schema.spaces(engine.bp)):
        sections.append({"label": None, "items": [{"kind": "mine", "label": "Just my team's", "icon": "users"}]})
    return sections


def _flows_offered(engine):
    """Whether "Step by step" is in this person's menu: a flow they can start from it, or one they left part way."""
    from ..engine import flows
    return bool(flows.in_menu(engine)) or bool(flows.unfinished(engine, current_user.id))


def unread(ws):
    items = Notification.query.filter(Notification.workspace_id == ws.id,
                                      or_(Notification.user_id.is_(None), Notification.user_id == current_user.id)).order_by(Notification.id.desc()).limit(200).all()
    return sum(1 for n in items if current_user.id not in (n.read_by or []) and n.kind != "approval")  # those are counted under To do


def my_approvals(ws, member):
    """The approvals and tasks waiting for this person (not one they asked for themselves, through their role)."""
    from ..engine.automations import may_decide
    from ..models import Record
    pending = Approval.query.filter_by(workspace_id=ws.id, status="pending").order_by(Approval.id).all()
    engine = g.get("engine") if g.get("engine") is not None and g.engine.ws.id == ws.id else None
    needs_record = lambda a: a.record_id and (a.assignee_id is None or (a.proposal or {}).get("not_by"))  # noqa: E731
    return [a for a in pending if may_decide(a, member, current_user.id, db.session.get(Record, a.record_id) if needs_record(a) else None,
                                             engine=engine) and in_my_queue(a)]


def in_my_queue(a, later=False):
    """A task someone else in its team took isn't in anyone else's queue; one put off ("not now") comes back on its day.
    With `later`, the ones put off instead."""
    from ..models import utcnow
    if a.taken_by_id and a.taken_by_id != current_user.id:
        return False
    put_off = bool(a.deferred_until and a.deferred_until > utcnow())
    return put_off if later else not put_off


def open_suggestions(ws):
    from ..models import Suggestion
    return Suggestion.query.filter_by(workspace_id=ws.id, status="open").count()


def ai_count(ws):
    """How many jobs the AI does: the AI steps of the business's automations, and the fields it fills in."""
    from ..engine.automations import ai_steps
    return (sum(len(ai_steps(a)) for a in (ws.blueprint or {}).get("automations", []))
            + sum(1 for t in (ws.blueprint or {}).get("tables", []) for f in t["fields"] if f.get("ai")))


@bp.app_context_processor
def chrome():
    return {"nav_for": nav, "unread_for": unread, "approvals_for": my_approvals, "suggestions_for": open_suggestions, "ai_count_for": ai_count,
            "feedback_for": lambda ws: url_for("app.feedback", wid=ws.id), "checks_for": lambda ws: url_for("app.checks", wid=ws.id)}


@bp.app_template_global()
def target_url(ws, target):
    """'table:invoices:record:7' (as automation steps record it) -> a link."""
    from ..builder.chat import navigate_url
    return navigate_url(ws, target) if target else None


@bp.app_template_global()
def source_url(ws, link):
    """Where a passage the AI found came from: a record, a kept file, or an email in Outlook."""
    if not link:
        return None
    if link.startswith(("https://", "http://")):
        return link
    if link.startswith("file:"):
        return url_for("app.file", wid=ws.id, fid=int(link.split(":", 1)[1]))
    return target_url(ws, link)


from . import erp_ai, erp_flows, erp_learn, erp_pages, erp_records, erp_studio  # noqa: E402,F401 (they add their routes to bp)
