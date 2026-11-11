"""The ERP itself: every screen is drawn from the workspace's blueprint. Its pages live in erp_records (tables and
records), erp_flows (step by step), erp_learn (checks and suggestions), erp_pages (dashboards, automations, approvals,
history), erp_studio (the App map) and erp_ai (the assistant, reading documents, imports), each a router under
/w/{wid}/app with names "app.<function>"; this module holds what they share.

What a page has looked up is on request.state (ws, member, engine), and the person is current_user(request); the
template globals and the menu find the request being served with current_request()."""
from sqlalchemy import or_

from ..blueprint import schema
from ..engine.records import Engine
from ..extensions import db
from ..models import Approval, Notification
from ..security import step_up, stepped_up, workspace
from .http import context_processor, current_request, current_user, template_global, url_for


def ctx(wid, role="viewer", request=None):
    """The workspace (checked against the person's role) and an engine acting as them: it shows them only the tables,
    records and fields their role allows. Both are kept on request.state (ws, member, engine)."""
    request = request or current_request()
    ws = workspace(wid, role, request)
    engine = Engine(ws, by=current_user(request).display, member=request.state.member)
    engine.stepped_up = stepped_up(request)
    request.state.engine = engine
    return ws, engine


def confirm_step_up(code, request=None):
    """A form that needs a confirmed sign-in carries the person's code (or password) as "step_up_code" (`code` here):
    checked, it counts for the engine of this request too. Returns whether they're confirmed now."""
    request = request or current_request()
    if stepped_up(request):
        return True
    ok = step_up(current_user(request), code, request)
    engine = getattr(request.state, "engine", None)
    if ok and engine is not None:
        engine.stepped_up = True
    return ok


def _here(name):
    """What the page being served has looked up (request.state: its ws, member or engine), or None."""
    request = current_request()
    return getattr(request.state, name, None) if request is not None else None


def _me():
    """The person the page being served is for."""
    return current_user(current_request())


def my_space(engine):
    """The team menu this person sees (spaces in the design), or None for the whole menu: admins and the owner, people
    in no team that has one, and anyone who chose to see everything."""
    if engine is None or engine.role is None or engine._at_least("admin"):
        return None
    if engine.ws.id in (current_request().state.session.get("whole_menu") or []):
        return None
    return next((sp for sp in schema.spaces(engine.bp) if set(sp["for"]) & engine.teams or engine.role in sp["for"]), None)


def home_page_for(ws):
    """The key of the page that is this person's home (Home opens it), or None."""
    engine = _here("engine")
    if engine is None or engine.ws.id != ws.id:
        return None
    return next((p["key"] for p in engine.visible_blueprint().get("pages", []) if _home_of(engine, p)), None)


def _home_of(engine, page):
    """Whether a page is this person's home (Home opens it), so it isn't in their menu a second time."""
    who = set(page.get("home_for") or [])
    return bool(who) and engine is not None and (engine.role in who or bool(who & engine.teams))


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
                count = engine.count(t["key"], view["filters"]) if view and view.get("filters") else None  # counted by SQL
                items.append({"kind": "table", "key": t["key"], "view": item.get("view") if view else None, "icon": t.get("icon") or "table",
                              "label": item.get("label") or (view["label"] if view and view["label"] not in ("All", t["label"]) else t["label"]),
                              "count": count})
            elif item.get("page") in pages and not _home_of(engine, pages[item["page"]]):  # (their home is what Home opens)
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
    engine = _here("engine")
    space = my_space(engine) if engine is not None and engine.ws.id == ws.id else None
    if space is not None:
        return _space_sections(engine, space)
    bp_ = engine.visible_blueprint() if engine is not None and engine.ws.id == ws.id else ws.blueprint or {}
    tables = [t for t in bp_.get("tables", []) if not t.get("embedded")]
    if engine is not None and engine.ws.id == ws.id and len(tables) > MANY_LISTS and schema.spaces(bp_):
        return _by_spaces(engine, bp_, tables)
    sections, used = [], set()
    for m in bp_.get("modules", []):
        items = [{"kind": "page", "key": p["key"], "label": p["label"], "icon": p.get("icon") or "layout-dashboard"}
                 for p in bp_.get("pages", []) if p.get("module") == m["key"]]
        items += [{"kind": "table", "key": t["key"], "label": t["label"], "icon": t.get("icon") or "table"} for t in tables if t.get("module") == m["key"]]
        if items:
            sections.append({"label": m["label"], "items": items})
            used |= {(i["kind"], i["key"]) for i in items}
    loose = [{"kind": "page", "key": p["key"], "label": p["label"], "icon": p.get("icon") or "layout-dashboard"}
             for p in bp_.get("pages", []) if ("page", p["key"]) not in used and not _home_of(engine, p)]
    loose += [{"kind": "table", "key": t["key"], "label": t["label"], "icon": t.get("icon") or "table"} for t in tables if ("table", t["key"]) not in used]
    if loose:
        sections.insert(0, {"label": None, "items": loose})
    if (engine is not None and engine.ws.id == ws.id and bp_.get("flows")) and _flows_offered(engine):
        sections.insert(0, {"label": None, "items": [{"kind": "flows", "key": "flows", "label": "Step by step", "icon": "list-todo"}]})
    if engine is not None and engine.ws.id == ws.id and engine.role is not None and not engine._at_least("admin") \
            and any(set(sp["for"]) & engine.teams or engine.role in sp["for"] for sp in schema.spaces(engine.bp)):
        sections.append({"label": None, "items": [{"kind": "mine", "label": "Just my team's", "icon": "users"}]})
    return sections


MANY_LISTS = 15  # past this, someone who sees everything (the owner) gets it in the app's own areas, folded


def _by_spaces(engine, bp_, tables):
    """Everything, for someone who sees it all, in the app's own areas (each team's menu), each folded, then what no
    area has: one long list of every list is where nobody finds anything."""
    by_key = {t["key"]: t for t in tables}
    pages = {p["key"]: p for p in bp_.get("pages", [])}
    sections, placed = [], set()
    for sp in schema.spaces(bp_):
        items = []
        for g in sp["groups"]:
            for i in g["items"]:
                if i.get("table") in by_key:
                    t = by_key[i["table"]]
                    view = next((v for v in t.get("views", []) if v["key"] == i.get("view")), None)
                    items.append({"kind": "table", "key": t["key"], "view": i.get("view") if view else None, "icon": t.get("icon") or "table",
                                  "label": i.get("label") or (view["label"] if view and view["label"] not in ("All", t["label"]) else t["label"])})
                    placed.add(t["key"])
                elif i.get("page") in pages and not _home_of(engine, pages[i["page"]]):
                    p = pages[i["page"]]
                    items.append({"kind": "page", "key": p["key"], "label": p["label"], "icon": p.get("icon") or "layout-dashboard"})
        if items:
            sections.append({"label": sp["label"], "items": list({(x["kind"], x["key"], x.get("view")): x for x in items}.values()), "fold": True})
    rest = [{"kind": "table", "key": t["key"], "label": t["label"], "icon": t.get("icon") or "table"} for t in tables if t["key"] not in placed]
    if rest:
        sections.append({"label": "Everything else", "items": rest, "fold": True})
    return sections


def _flows_offered(engine):
    """Whether "Step by step" is in this person's menu: a flow they can start from it, or one they left part way."""
    from ..engine import flows
    return bool(flows.in_menu(engine)) or bool(flows.unfinished(engine, _me().id))


def unread(ws):
    me = _me()
    items = Notification.query.filter(Notification.workspace_id == ws.id,
                                      or_(Notification.user_id.is_(None), Notification.user_id == me.id)).order_by(Notification.id.desc()).limit(200).all()
    return sum(1 for n in items if me.id not in (n.read_by or []) and n.kind != "approval")  # those are counted under To do


def my_approvals(ws, member):
    """The approvals and tasks waiting for this person (not one they asked for themselves, through their role)."""
    from ..engine.automations import may_decide
    from ..models import Record
    pending = Approval.query.filter_by(workspace_id=ws.id, status="pending").order_by(Approval.id).all()
    engine = _here("engine")
    engine = engine if engine is not None and engine.ws.id == ws.id else None
    me = _me()
    needs_record = lambda a: a.record_id and (a.assignee_id is None or (a.proposal or {}).get("not_by"))  # noqa: E731
    return [a for a in pending if may_decide(a, member, me.id, db.session.get(Record, a.record_id) if needs_record(a) else None,
                                             engine=engine) and in_my_queue(a)]


def ready_for(engine, most=20):
    """The records whose next move is this person's own (what the original "Needs you" lists besides approvals): a stage
    button only their team, role or a person field naming them may press (an order the seller proposed changes to, for
    the purchasing officer: Send again), or, on their own record turned down, the button that takes it on again. Oldest
    waiting first: [{"r", "t", "move", "stage"}]."""
    if engine is None or engine.role is None or engine.portal_contact is not None:
        return []
    from ..models import Approval
    asked = {a.record_id for a in Approval.query.filter_by(workspace_id=engine.ws.id, status="pending").all()}
    teams = {tm["key"] for tm in schema.teams(engine.bp)}
    out = []
    for t in engine.visible_blueprint().get("tables", []):
        flow = t.get("workflow")
        if not flow or t.get("embedded") or not engine.can(t, "edit"):
            continue
        field, start = flow["field"], schema.start_stage(t)
        for m in flow.get("transitions") or []:
            who = [w for w in m.get("who") or [] if w != "portal"]
            if schema.says_no(m["to"]) or not m.get("from"):
                continue
            named = [w for w in who if w in teams and w in engine.teams or "." in w or (w not in teams and w not in schema.WHO_WORDS and w != "creator")]
            again = not who and any(schema.says_no(x) for x in m["from"])  # their own, turned down: changed and sent again
            if not named and not again:
                continue
            for r in engine.query(t["key"], [{"field": field, "op": "one_of", "value": list(m["from"])}])[:most]:
                if r.id in asked or any(x["r"].id == r.id for x in out):
                    continue
                if again and getattr(r, "created_by_id", None) != engine.user_id:
                    continue
                if named and not any(engine.is_who(w, r) for w in named):
                    continue
                out.append({"r": r, "t": t, "move": m.get("label") or f"Move to {m['to']}", "stage": (r.data or {}).get(field)})
    out.sort(key=lambda x: x["r"].updated_at or x["r"].created_at)
    return out[:most]


def next_up(engine, t, r):
    """Who takes a record on next when it isn't this person, in words ("someone in Purchasing officer (Ready to order)"):
    a move on from its stage kept for a team, or a team whose own menu has a view it's now in. None when nobody's named."""
    flow = t.get("workflow")
    if not flow or r is None:
        return None
    stage = (r.data or {}).get(flow["field"])
    names = {tm["key"]: tm.get("name") or tm["key"] for tm in schema.teams(engine.bp)}
    for m in flow.get("transitions") or []:
        if stage in (m.get("from") or []) and not schema.says_no(m["to"]):
            teams = [names[w] for w in m.get("who") or [] if w in names]
            if teams:
                return f"{' or '.join(teams)}, to {(m.get('label') or 'move it to ' + m['to']).lower()}"
    for sp in schema.spaces(engine.bp):
        for g in sp["groups"]:
            for i in g["items"]:
                if i.get("table") != t["key"] or not i.get("view"):
                    continue
                view = next((v for v in t.get("views", []) if v["key"] == i["view"] and v.get("filters")), None)
                if view and engine.matches(r, view["filters"]):
                    who = [names.get(w, w) for w in sp["for"]]
                    return f"{' or '.join(who)} (their “{i.get('label') or view['label']}” list)"
    return None


def in_my_queue(a, later=False):
    """A task someone else in its team took isn't in anyone else's queue; one put off ("not now") comes back on its day.
    With `later`, the ones put off instead."""
    from ..models import utcnow
    if a.taken_by_id and a.taken_by_id != _me().id:
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


@context_processor
def chrome():
    return {"nav_for": nav, "unread_for": unread, "approvals_for": my_approvals, "ready_for": ready_for, "home_page_for": home_page_for,
            "focused_for": lambda ws: (lambda e: e is not None and e.ws.id == ws.id and my_space(e) is not None)(_here("engine")), "suggestions_for": open_suggestions, "ai_count_for": ai_count,
            "feedback_for": lambda ws: url_for("app.feedback", wid=ws.id), "checks_for": lambda ws: url_for("app.checks", wid=ws.id)}


@template_global
def target_url(ws, target):
    """'table:invoices:record:7' (as automation steps record it) -> a link."""
    from ..builder.chat import navigate_url
    return navigate_url(ws, target) if target else None


@template_global
def source_url(ws, link):
    """Where a passage the AI found came from: a record, a kept file, or an email in Outlook."""
    if not link:
        return None
    if link.startswith(("https://", "http://")):
        return link
    if link.startswith("file:"):
        return url_for("app.file", wid=ws.id, fid=int(link.split(":", 1)[1]))
    return target_url(ws, link)

