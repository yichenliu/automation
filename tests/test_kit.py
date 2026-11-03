"""The rest of the kit: links to many records, addresses, locations, barcodes and signatures, colour rules, step-by-step
forms, the map, tree, spreadsheet and Gantt views, free dashboard layouts with forms, buttons, activity, comparisons,
stacked and funnel charts, per-role home pages, report and statement PDFs, and the portal for customers."""
import base64
import re

import pytest

from app.blueprint import ops, schema
from app.builder import chat
from app.engine.records import Engine
from app.extensions import db
from app.models import Comment, Membership, Notification, Record, User

from .conftest import login

PNG = base64.b64encode(bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                                     "1f15c4890000000d49444154789c6360000002000100e221bc330000000049454e44ae426082")).decode()

DESIGN = [
    {"op": "set_portal", "table": "customers", "email_field": "email", "title": "Customer portal", "welcome": "Your jobs and invoices."},
    {"op": "create_table", "table": "customers", "label": "Customers", "fields": [
        {"key": "name", "type": "text", "required": True}, {"key": "email", "type": "email"}, {"key": "address", "type": "address"},
        {"key": "site", "type": "location"}, {"key": "tier", "type": "select", "options": ["Standard", "VIP"]}],
     "colors": [{"when": [{"field": "tier", "op": "equals", "value": "VIP"}], "color": "violet"}]},
    {"op": "create_table", "table": "staff", "label": "Staff", "fields": [{"key": "name", "type": "text"}]},
    {"op": "create_table", "table": "jobs", "label": "Jobs", "wizard": True,
     "sections": [{"label": "Customer", "fields": ["customer", "title"]}, {"label": "Schedule", "fields": ["start", "finish", "crew"]}],
     "portal": {"via": "customer", "fields": ["title", "status", "start"], "can_add": True},
     "fields": [{"key": "title", "type": "text", "required": True}, {"key": "customer", "type": "link", "table": "customers"},
                {"key": "crew", "type": "link", "table": "staff", "multiple": True}, {"key": "helpers", "type": "user", "multiple": True},
                {"key": "status", "type": "select", "options": ["Booked", "Done"], "default": "Booked"},
                {"key": "start", "type": "date"}, {"key": "finish", "type": "date"},
                {"key": "after", "type": "link", "table": "jobs", "multiple": True},
                {"key": "asset_tag", "type": "barcode", "unique": True}, {"key": "sign_off", "type": "signature"},
                {"key": "amount", "type": "money"}],
     "views": [{"key": "gantt", "type": "timeline", "start_field": "start", "end_field": "finish", "depends_field": "after"},
               {"key": "sheet", "type": "sheet", "columns": ["title", "status", "amount"]}],
     "colors": [{"when": [{"field": "status", "op": "equals", "value": "Done"}], "color": "green", "field": "status"}]},
    {"op": "create_table", "table": "invoices", "label": "Invoices", "portal": {"via": "customer"}, "fields": [
        {"key": "number", "type": "autonumber", "prefix": "INV-"}, {"key": "customer", "type": "link", "table": "customers"},
        {"key": "total", "type": "money"}, {"key": "status", "type": "select", "options": ["Unpaid", "Paid"], "default": "Unpaid"}]},
    {"op": "update_table", "table": "customers", "changes": {"documents": None}},
    {"op": "add_document", "table": "customers", "document": {"key": "statement", "label": "Statement", "title": "Statement",
                                                              "lines": {"table": "invoices", "via": "customer", "columns": ["number", "total"],
                                                                        "filters": [{"field": "status", "op": "equals", "value": "Unpaid"}],
                                                                        "total": "total"}}},
    {"op": "add_view", "table": "customers", "view": {"key": "map", "type": "map", "location_field": "site"}},
    {"op": "create_table", "table": "accounts", "label": "Accounts", "fields": [
        {"key": "name", "type": "text"}, {"key": "parent", "type": "link", "table": "accounts"}],
     "views": [{"key": "tree", "type": "tree", "parent_field": "parent"}]},
    {"op": "add_automation", "automation": {"key": "nightly", "name": "Nightly check", "trigger": {"type": "schedule", "every": "day"},
                                            "actions": [{"type": "notify", "message": "Checked"}]}},
    {"op": "add_page", "page": {"key": "desk", "label": "Front desk", "home_for": ["editor"], "widgets": [
        {"type": "heading", "label": "Today"},
        {"type": "metric", "table": "jobs", "label": "Jobs", "compare": "month"},
        {"type": "form", "table": "jobs", "label": "Quick job", "fields": ["title", "customer"], "submit": "Book it", "width": 2},
        {"type": "actions", "label": "Shortcuts", "buttons": [{"kind": "new", "table": "jobs", "label": "New job", "values": {"status": "Booked"}},
                                                              {"kind": "view", "table": "jobs", "view": "sheet", "label": "Edit jobs"},
                                                              {"kind": "automation", "automation": "nightly", "label": "Run the check"}]},
        {"type": "chart", "table": "jobs", "group_by": "status", "stack_by": "customer", "label": "Jobs by status"},
        {"type": "chart", "table": "jobs", "group_by": "status", "style": "funnel", "label": "Pipeline"},
        {"type": "activity", "label": "What's happening"}]}},
    {"op": "add_page", "page": {"key": "boss", "label": "Owner's view", "access": "admin", "widgets": [{"type": "metric", "table": "invoices"}]}},
]


@pytest.fixture
def kit(app, owner):
    user, ws = owner
    result = ops.apply(ws.blueprint, DESIGN)
    assert not result.problems, result.problems
    chat.save_version(ws, result.blueprint, result.changes, "chat")
    ava = User(email="ava@example.com", name="Ava")
    ava.set_password("password1234")
    db.session.add(ava)
    db.session.flush()
    db.session.add(Membership(workspace_id=ws.id, user_id=ava.id, role="editor"))
    db.session.commit()
    engine = Engine(ws, by="Ethan")
    acme = engine.create("customers", {"name": "Acme", "email": "acme@example.com", "tier": "VIP",
                                       "address": "1 Hay St\nPerth WA 6000", "site": "-31.9523, 115.8613"})
    bolt = engine.create("customers", {"name": "Bolt", "email": "bolt@example.com"})
    for name in ("Mike", "Sam"):
        engine.create("staff", {"name": name})
    return {"ws": ws, "engine": engine, "acme": acme, "bolt": bolt, "owner": user, "ava": ava}


def test_links_to_many_records_and_people(kit):
    engine, ws = kit["engine"], kit["ws"]
    job = engine.create("jobs", {"title": "Fit-out", "customer": "Acme", "crew": "Mike, Sam", "helpers": [kit["owner"].id, kit["ava"].id]})
    job = engine.get(job.id)
    crew = engine.field("jobs", "crew")
    assert len(job.data["crew"]) == 2 and engine.display(crew, job.data["crew"]) == "Mike, Sam"
    assert engine.display(engine.field("jobs", "helpers"), job.data["helpers"]) == "Ethan, Ava"
    assert engine.matches(job, [{"field": "crew", "op": "equals", "value": "Sam"}])
    assert not engine.matches(job, [{"field": "crew", "op": "equals", "value": "Nobody"}])
    mike = next(r for r in engine.rows("staff") if r.data["name"] == "Mike")
    assert [r.id for r in engine.children("jobs", "crew", mike.id)] == [job.id]  # totals over a link to many count it under each
    ava_engine = Engine(ws, by="Ava", member=Membership.query.filter_by(user_id=kit["ava"].id).first())
    assert ava_engine.matches(job, [{"field": "helpers", "op": "equals", "value": "Ava"}])


def test_addresses_locations_barcodes_and_signatures(kit, client):
    engine, acme = kit["engine"], kit["acme"]
    assert acme.data["site"] == {"lat": -31.9523, "lng": 115.8613}
    assert engine.display(engine.field("customers", "address"), acme.data["address"]) == "1 Hay St, Perth WA 6000"
    with pytest.raises(Exception, match="isn't a location"):
        engine.create("customers", {"name": "Nowhere", "site": "north-ish"})
    engine.create("jobs", {"title": "A", "asset_tag": "ABC 123"})
    assert next(r for r in engine.rows("jobs")).data["asset_tag"] == "ABC123"
    with pytest.raises(Exception, match="already has this"):
        engine.create("jobs", {"title": "B", "asset_tag": "ABC123"})
    login(client)
    wid = kit["ws"].id
    form = client.get(f"/w/{wid}/app/t/jobs/new").get_data(as_text=True)
    assert "data-wizard" in form and 'data-step="Schedule"' in form and "data-signature" in form and "data-scan" in form
    made = client.post(f"/w/{wid}/app/t/jobs/new", data={"title": "Signed job", "crew": [str(r.id) for r in engine.rows("staff")],
                                                         "sign_off": f"data:image/png;base64,{PNG}", "status": "Booked"})
    assert made.status_code == 302
    job = Record.query.filter_by(table="jobs").order_by(Record.id.desc()).first()
    assert len(job.data["crew"]) == 2
    page = client.get(f"/w/{wid}/app/t/jobs/{job.id}").get_data(as_text=True)
    assert "signature-img" in page and "linkchip" in page
    customer = client.get(f"/w/{wid}/app/t/customers/{acme.id}").get_data(as_text=True)
    assert "openstreetmap.org" in customer and "tint-dot violet" in customer


def test_colour_rules_views_and_the_spreadsheet(kit, client):
    engine, ws = kit["engine"], kit["ws"]
    first = engine.create("jobs", {"title": "Rough-in", "start": "2026-11-02", "finish": "2026-11-06", "status": "Done"})
    engine.create("jobs", {"title": "Fit-off", "start": "2026-11-05", "finish": "2026-11-09", "after": [first.id]})
    engine.create("accounts", {"name": "Assets"})
    engine.create("accounts", {"name": "Bank", "parent": "Assets"})
    login(client)
    base = f"/w/{ws.id}/app/t"
    assert "cell-tint green" in client.get(f"{base}/jobs").get_data(as_text=True)
    assert "tint violet" in client.get(f"{base}/customers").get_data(as_text=True)
    gantt = client.get(f"{base}/jobs?view=gantt").get_data(as_text=True)
    assert "tl-deps" in gantt and "starts before Rough-in ends" in gantt
    pins = client.get(f"{base}/customers?view=map").get_data(as_text=True)
    assert '&#34;lat&#34;: -31.9523' in pins and "1 without a location" in pins
    tree = client.get(f"{base}/accounts?view=tree").get_data(as_text=True)
    assert tree.index("Assets") < tree.index("Bank") and "1 under it" in tree
    sheet = client.get(f"{base}/jobs?view=sheet").get_data(as_text=True)
    assert 'data-sheet=' in sheet and 'data-field="status"' in sheet and "data-new" in sheet
    added = client.post(f"{base}/jobs/quick", json={"title": "From the sheet", "amount": "120"})
    assert added.status_code == 200 and added.get_json()["ok"]
    refused = client.post(f"{base}/jobs/quick", json={"amount": "5"})
    assert refused.status_code == 400 and "Title is required" in refused.get_json()["error"]


def test_dashboard_layouts_widgets_role_homes_and_pdf_reports(kit, client):
    engine, ws = kit["engine"], kit["ws"]
    engine.create("jobs", {"title": "One", "customer": "Acme", "status": "Done"})
    engine.create("jobs", {"title": "Two", "customer": "Bolt"})
    login(client)
    page = client.get(f"/w/{ws.id}/app/p/desk").get_data(as_text=True)
    for bit in ("widget-heading", "Book it", "action-tile", "Run the check", "bar stacked", "funnel-bar", "What&#39;s happening",
                "vs", "w4", "w2"):
        assert bit in page, bit
    booked = client.post(f"/w/{ws.id}/app/t/jobs/quick", data={"title": "Walk-in", "customer": str(kit["acme"].id), "back": f"/w/{ws.id}/app/p/desk"})
    assert booked.status_code == 302 and booked.headers["Location"].endswith("/p/desk")
    assert any(r.data.get("title") == "Walk-in" for r in Engine(ws).rows("jobs"))
    ran = client.post(f"/w/{ws.id}/app/automations/nightly/run", data={"back": f"/w/{ws.id}/app/p/desk"})
    assert ran.headers["Location"].endswith("/p/desk")
    report = client.get(f"/w/{ws.id}/app/p/desk.pdf")
    assert report.status_code == 200 and report.mimetype == "application/pdf" and report.data[:4] == b"%PDF"
    home = client.get(f"/w/{ws.id}/app/")  # the owner starts at Home: what's waiting, buttons to start things, recent work
    assert home.status_code == 200 and "Waiting for you" in home.get_data(as_text=True) and "Start something" in home.get_data(as_text=True)
    client.post("/logout")
    client.post("/login", data={"email": "ava@example.com", "password": "password1234"})
    assert client.get(f"/w/{ws.id}/app/").headers["Location"].endswith("/p/desk")  # editors land on their home page
    assert client.get(f"/w/{ws.id}/app/p/boss").status_code == 404  # an admin-only page
    assert "Owner&#39;s view" not in client.get(f"/w/{ws.id}/app/p/desk").get_data(as_text=True)


def test_statements_list_only_whats_owed(kit):
    from app.documents import build
    engine = kit["engine"]
    engine.create("invoices", {"customer": "Acme", "total": 100})
    engine.create("invoices", {"customer": "Acme", "total": 50, "status": "Paid"})
    engine.create("invoices", {"customer": "Acme", "total": 25})
    data, _ = build(engine, engine.get(kit["acme"].id), "statement")
    assert [row[0] for row in data["lines"]["rows"]] == ["INV-0001", "INV-0003"]
    assert data["totals"][-1] == {"label": "Total total", "value": "$125.00", "strong": True}


def test_customers_see_only_their_own_records_in_the_portal(kit, client):
    engine, ws, acme = kit["engine"], kit["ws"], kit["acme"]
    mine = engine.create("invoices", {"customer": "Acme", "total": 100})
    theirs = engine.create("invoices", {"customer": "Bolt", "total": 999})
    job = engine.create("jobs", {"title": "Acme fit-out", "customer": "Acme"})
    login(client)
    invited = client.post(f"/w/{ws.id}/app/t/customers/{acme.id}/portal-invite", follow_redirects=True).get_data(as_text=True)
    token = re.search(r"/invite/([\w-]+)", invited).group(1)
    db.session.add(Comment(workspace_id=ws.id, record_id=job.id, user_id=kit["owner"].id, text="Internal: margin is thin"))
    db.session.add(Comment(workspace_id=ws.id, record_id=job.id, user_id=kit["owner"].id, text="We start Monday", shared=True))
    db.session.commit()
    client.post("/logout")
    client.get(f"/invite/{token}")
    joined = client.post("/register", data={"name": "Alex at Acme", "email": "acme@example.com", "password": "password1234"})
    assert joined.headers["Location"].endswith(f"/portal/{ws.id}/")
    home = client.get(f"/portal/{ws.id}/").get_data(as_text=True)
    assert "Customer portal" in home and "INV-0001" in home and "INV-0002" not in home and "Acme fit-out" in home
    assert client.get(f"/portal/{ws.id}/t/invoices/{theirs.id}").status_code == 404
    page = client.get(f"/portal/{ws.id}/t/jobs/{job.id}").get_data(as_text=True)
    assert "We start Monday" in page and "margin is thin" not in page
    client.post(f"/portal/{ws.id}/t/jobs/{job.id}/message", data={"text": "Great, thanks"})
    assert Comment.query.filter_by(text="Great, thanks", shared=True).count() == 1
    assert Notification.query.filter(Notification.text.contains("Great, thanks")).count() == 1
    asked = client.post(f"/portal/{ws.id}/t/jobs/new", data={"title": "Leaking tap", "status": "Booked"})
    assert asked.status_code == 302
    request_ = next(r for r in Engine(ws).rows("jobs") if r.data["title"] == "Leaking tap")
    assert request_.data["customer"] == acme.id
    assert client.get(f"/w/{ws.id}/app/").status_code == 403  # the ERP itself is for staff
    assert client.get(f"/portal/{ws.id}/t/invoices/{mine.id}").status_code == 200


def test_the_checks_explain_misused_components(app, owner):
    _, ws = owner
    bad = ops.apply(ws.blueprint, [
        {"op": "create_table", "table": "sites", "fields": [{"key": "name", "type": "text"}, {"key": "boss", "type": "link", "table": "sites"}],
         "views": [{"key": "m", "type": "map", "location_field": "name"}, {"key": "t", "type": "tree", "parent_field": "name"}]},
        {"op": "add_automation", "automation": {"key": "on_add", "name": "On add", "trigger": {"type": "record_created", "table": "sites"},
                                                "actions": [{"type": "notify", "message": "x"}]}},
        {"op": "add_page", "page": {"label": "P", "widgets": [{"type": "actions", "buttons": [{"kind": "automation", "automation": "on_add", "label": "Go"}]}]}}])
    text = " ".join(bad.problems)
    assert "must be a location field" in text and "link from Sites to Sites" in text and "only run a scheduled automation" in text
    assert schema.normalize_field({"key": "customer", "type": "link", "table": "c", "access": "admin"}).get("access") is None
