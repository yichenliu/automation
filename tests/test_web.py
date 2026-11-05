"""Every screen, end to end through the web app: sign up, start an ERP, chat, then use everything that was built."""
import io

from app import ai, worker
from app.builder import chat
from app.blueprint import ops
from app.engine.records import Engine
from app.extensions import db
from app.models import Approval, File, Invite, Job, Membership, Message, Notification, Run, User, Workspace

from .conftest import ScriptedAI, login

QUOTING = [
    {"op": "create_table", "table": "customers", "label": "Customers", "module": "sales", "icon": "users", "fields": [
        {"key": "name", "label": "Name", "type": "text", "required": True, "unique": True},
        {"key": "email", "label": "Email", "type": "email"}, {"key": "tags", "label": "Tags", "type": "multi_select", "options": ["VIP", "Trade"]},
        {"key": "rating", "label": "Rating", "type": "rating"}, {"key": "logo", "label": "Logo", "type": "image"},
        {"key": "owner", "label": "Account manager", "type": "user"}, {"key": "notes", "label": "Notes", "type": "rich_text"}]},
    {"op": "create_table", "table": "quotes", "label": "Quotes", "module": "sales", "icon": "document", "fields": [
        {"key": "number", "label": "Quote", "type": "autonumber", "prefix": "Q-"},
        {"key": "customer", "label": "Customer", "type": "link", "table": "customers", "required": True},
        {"key": "customer_email", "label": "Customer email", "type": "lookup", "link": "customer", "field": "email"},
        {"key": "issue_date", "label": "Date", "type": "date", "default": "today"},
        {"key": "valid_until", "label": "Valid until", "type": "date"},
        {"key": "status", "label": "Status", "type": "select", "options": ["Draft", "Sent", "Accepted", "Declined"], "default": "Draft"},
        {"key": "reason", "label": "Why declined", "type": "text", "show_if": [{"field": "status", "op": "equals", "value": "Declined"}]},
        {"key": "subtotal", "label": "Subtotal", "type": "rollup", "table": "quote_lines", "via": "quote", "aggregate": "sum", "field": "amount"},
        {"key": "gst", "label": "GST", "type": "formula", "expression": "round(subtotal * 0.1, 2)", "format": "money"},
        {"key": "total", "label": "Total", "type": "formula", "expression": "subtotal + gst", "format": "money"},
        {"key": "pdf", "label": "PDF", "type": "file"}],
     "views": [{"key": "calendar", "label": "Calendar", "type": "calendar", "date_field": "issue_date"},
               {"key": "timeline", "label": "Timeline", "type": "timeline", "start_field": "issue_date", "end_field": "valid_until"}],
     "documents": [{"key": "quote_pdf", "label": "Quote", "title": "Quote", "number_field": "number", "fields": ["issue_date"],
                    "to": {"field": "customer", "details": ["email"]}, "lines": {"table": "quote_lines", "via": "quote", "columns": ["description", "quantity", "unit_price", "amount"]},
                    "totals": ["subtotal", "gst", "total"], "notes": "Valid until {{record.valid_until}}"}]},
    {"op": "create_table", "table": "quote_lines", "label": "Quote lines", "embedded": True, "fields": [
        {"key": "quote", "label": "Quote", "type": "link", "table": "quotes", "required": True},
        {"key": "description", "label": "Description", "type": "text", "required": True},
        {"key": "quantity", "label": "Qty", "type": "number", "default": 1}, {"key": "unit_price", "label": "Unit price", "type": "money"},
        {"key": "amount", "label": "Amount", "type": "formula", "expression": "quantity * unit_price", "format": "money"}]},
    {"op": "add_view", "table": "customers", "view": {"key": "cards", "label": "Cards", "type": "gallery", "image_field": "logo"}},
    {"op": "add_automation", "automation": {"key": "big_quotes", "name": "Big quotes need approval", "trigger": {"type": "button", "table": "quotes", "label": "Send"},
        "actions": [{"type": "request_approval", "approver": "admin", "message": "Send {{record.number}} for {{record.total}}?", "only_if": [{"field": "total", "op": "greater_than", "value": 1000}]},
                    {"type": "generate_document", "document": "quote_pdf", "field": "pdf"},
                    {"type": "update_record", "values": {"status": "Sent"}}]}},
    {"op": "set_public_form", "table": "customers", "form": {"title": "Become a customer", "fields": ["name", "email"]}},
    {"op": "set_theme", "theme": {"accent": "teal"}},
    {"op": "add_page", "page": {"key": "overview", "label": "Overview", "widgets": [
        {"type": "metric", "label": "Open quotes", "table": "quotes", "filters": [{"field": "status", "op": "equals", "value": "Sent"}]},
        {"type": "goal", "label": "Quoted this year", "table": "quotes", "aggregate": "sum", "field": "total", "target": 100000},
        {"type": "chart", "label": "Quotes by status", "table": "quotes", "group_by": "status", "style": "donut"},
        {"type": "chart", "label": "Quoted over time", "table": "quotes", "group_by": "issue_date", "aggregate": "sum", "field": "total", "style": "line"},
        {"type": "list", "label": "Latest", "table": "quotes", "limit": 5}, {"type": "text", "text": "**Welcome** back"}]}},
]


# the same app as the builder writes it, in the Keel language (the data step's patches, then the behaviour step's)
QUOTING_DATA = [
    {"do": "add_list", "key": "customers", "label": "Customers", "singular": "Customer", "fields": [
        {"type": "text", "key": "name", "label": "Name", "required": True, "unique": True}, {"type": "email", "key": "email", "label": "Email"},
        {"type": "choices", "key": "tags", "label": "Tags", "options": ["VIP", "Trade"]}, {"type": "rating", "key": "rating", "label": "Rating"},
        {"type": "image", "key": "logo", "label": "Logo"}, {"type": "person", "key": "owner", "label": "Account manager"},
        {"type": "rich_text", "key": "notes", "label": "Notes"}]},
    {"do": "add_list", "key": "quotes", "label": "Quotes", "singular": "Quote", "fields": [
        {"type": "auto_number", "key": "number", "label": "Quote", "prefix": "Q-"},
        {"type": "link", "key": "customer", "label": "Customer", "to": "customers", "required": True},
        {"type": "lookup", "key": "customer_email", "label": "Customer email", "path": "customer.email"},
        {"type": "date", "key": "issue_date", "label": "Date", "default": "today"}, {"type": "date", "key": "valid_until", "label": "Valid until"},
        {"type": "choice", "key": "status", "label": "Status", "options": ["Draft", "Sent", "Accepted", "Declined"], "default": "Draft"},
        {"type": "text", "key": "reason", "label": "Why declined", "show_if": [{"field": "status", "op": "equals", "value": "Declined"}]},
        {"type": "total", "key": "subtotal", "label": "Subtotal", "of": "quote_lines", "link": "quote", "how": "sum", "field": "amount"},
        {"type": "formula", "key": "gst", "label": "GST", "formula": "round(subtotal * 0.1, 2)", "shows": "money"},
        {"type": "formula", "key": "total", "label": "Total", "formula": "subtotal + gst", "shows": "money"},
        {"type": "file", "key": "pdf", "label": "PDF"}]},
    {"do": "add_list", "key": "quote_lines", "label": "Quote lines", "singular": "Quote line", "inside": "quotes", "fields": [
        {"type": "link", "key": "quote", "label": "Quote", "to": "quotes", "required": True},
        {"type": "text", "key": "description", "label": "Description", "required": True},
        {"type": "number", "key": "quantity", "label": "Qty", "default": 1}, {"type": "money", "key": "unit_price", "label": "Unit price"},
        {"type": "formula", "key": "amount", "label": "Amount", "formula": "quantity * unit_price", "shows": "money"}]},
    {"do": "add_view", "list": "quotes", "view": {"type": "calendar", "label": "Calendar", "date_field": "issue_date"}},
    {"do": "add_view", "list": "quotes", "view": {"type": "timeline", "label": "Timeline", "date_field": "issue_date", "end_field": "valid_until"}},
    {"do": "add_view", "list": "customers", "view": {"type": "gallery", "label": "Cards", "image_field": "logo"}},
]
QUOTING_BEHAVIOUR = [
    {"do": "design_op", "op": next(op for op in QUOTING if op.get("table") == "quotes" and op["op"] == "create_table")["documents"][0] | {}},
    {"do": "add_rule", "rule": {"key": "big_quotes", "name": "Big quotes need approval", "when": {"on": "button", "list": "quotes", "label": "Send"},
                                "steps": [{"do": "approval", "who": "admin", "message": "Send {{number}} for {{total}}?",
                                           "only_if": [{"field": "total", "op": "greater_than", "value": 1000}]},
                                          {"do": "document", "document": "quote_pdf", "save_to": "pdf"},
                                          {"do": "set", "values": [{"field": "status", "to": "Sent"}]}]}},
] + [{"do": "design_op", "op": op} for op in QUOTING if op["op"] in ("set_public_form", "set_theme", "add_page")]
QUOTING_BEHAVIOUR[0] = {"do": "design_op", "op": {"op": "add_document", "table": "quotes", "document": QUOTING_BEHAVIOUR[0]["op"]}}


def _build(ws):
    result = ops.apply(ws.blueprint, QUOTING)
    assert not result.problems, result.problems
    chat.save_version(ws, result.blueprint, result.changes, "chat")


def test_sign_up_start_an_erp_and_invite_someone(client, app):
    page = client.post("/register", data={"name": "Ethan", "email": "e@x.com", "password": "password1234"}, follow_redirects=True)
    assert "Start your app" in page.get_data(as_text=True)
    page = client.post("/workspaces/new", data={"name": "Eth Analytic", "about": "Consultancy", "start": "chat"}, follow_redirects=True)
    html = page.get_data(as_text=True)
    assert "Let's build your app" in html and "Learn from my Microsoft 365" in html
    ws = Workspace.query.one()
    assert Membership.query.one().role == "owner"
    client.post(f"/w/{ws.id}/settings/members", data={"action": "invite", "email": "sam@x.com", "role": "editor"})
    invite = Invite.query.one()
    other = app.test_client()
    other.get(f"/invite/{invite.token}")
    other.post("/register", data={"name": "Sam", "email": "sam@x.com", "password": "password5678"})
    sam = User.query.filter_by(email="sam@x.com").one()
    assert Membership.query.filter_by(user_id=sam.id).one().role == "editor"
    assert other.get(f"/w/{ws.id}/build").status_code == 403  # editors don't design
    assert other.get(f"/w/{ws.id}/app/").status_code == 200


def test_build_by_chat_then_use_every_screen(client, app, owner):
    user, ws = owner
    login(client)
    from .test_lang import Model
    ai.set_backend(Model({"understand": {"needs": [{"said": "We quote customers", "does": "Keeps customers and quotes"}], "reply": "Built customers and quotes."},
                          "data": {"lists": ["customers", "quotes", "quote_lines"], "patches": QUOTING_DATA},
                          "behaviour": {"work": [{"need": 1, "patches": QUOTING_BEHAVIOUR}]}}))
    sent = client.post(f"/w/{ws.id}/chat", data={"text": "We quote customers", "page": f"/w/{ws.id}/app",
                                                "files": (io.BytesIO(b"name,email\nAcme,a@acme.com\n"), "customers.csv")},
                       content_type="multipart/form-data").get_json()
    assert sent["reply"]["status"] == "queued" and Message.query.filter_by(role="user").one().files
    assert worker.builder_tick() is True
    done = client.get(f"/w/{ws.id}/chat/{sent['reply']['id']}").get_json()
    assert done["status"] == "done" and "Created the line items Quote lines" in done["html"]
    base = f"/w/{ws.id}/app"

    r = client.post(base + "/t/customers/new", data={"name": "Acme Pty Ltd", "email": "ap@acme.com", "tags": ["VIP"], "rating": "4",
                                                     "owner": str(user.id), "notes": "**Big** client",
                                                     "logo": (io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"0" * 64), "logo.png")},
                    content_type="multipart/form-data")
    assert r.status_code == 302
    acme = Engine(ws).rows("customers")[0]
    assert acme.data["tags"] == ["VIP"] and acme.data["rating"] == 4 and File.query.filter_by(record_id=acme.id, field="logo").count() == 1
    dup = client.post(base + "/t/customers/new", data={"name": "acme pty ltd"})
    assert dup.status_code == 400 and "already has this Name" in dup.get_data(as_text=True)

    r = client.post(base + "/t/quotes/new", data={
        "customer": str(acme.id), "issue_date": "2026-11-01", "valid_until": "2026-11-30", "status": "Draft",
        "lines-quote_lines-0-description": "Survey", "lines-quote_lines-0-quantity": "10", "lines-quote_lines-0-unit_price": "150",
        "lines-quote_lines-1-description": "Report", "lines-quote_lines-1-quantity": "1", "lines-quote_lines-1-unit_price": "900",
        "lines-quote_lines-2-description": "", "lines-quote_lines-2-quantity": ""})
    assert r.status_code == 302
    e = Engine(ws)
    quote = e.rows("quotes")[0]
    assert e.value(quote, "subtotal") == 2400.0 and e.value(quote, "total") == 2640.0 and e.value(quote, "customer_email") == "ap@acme.com"
    assert len(e.rows("quote_lines")) == 2

    record = client.get(base + f"/t/quotes/{quote.id}").get_data(as_text=True)
    assert "Q-0001" in record and "$2,640.00" in record and "Survey" in record and "Quote PDF" in record and "Send" in record
    assert "Why declined" not in record  # only shown when declined
    form = client.get(base + f"/t/quotes/{quote.id}/edit").get_data(as_text=True)
    assert "lines-quote_lines-1-description" in form and "data-show-if" in form

    # The button asks an admin to approve (the total is over $1,000); approving produces the PDF and marks it sent.
    client.post(base + f"/t/quotes/{quote.id}/press/big_quotes")
    run = Run.query.one()
    assert run.status == "waiting_approval" and Approval.query.one().status == "pending"
    assert "<b>Waiting for you:</b>" in client.get(base + f"/t/quotes/{quote.id}").get_data(as_text=True)
    assert "Send Q-0001 for $2,640.00?" in client.get(base + "/approvals").get_data(as_text=True)
    client.post(base + f"/approvals/{Approval.query.one().id}", data={"decision": "approve", "comment": "ok"})
    run = db.session.get(Run, run.id)
    assert run.status == "done" and Engine(ws).get(quote.id).data["status"] == "Sent"
    pdf = File.query.filter_by(record_id=quote.id, field="pdf").one()
    assert pdf.mime == "application/pdf" and client.get(base + f"/files/{pdf.id}").data[:4] == b"%PDF"

    for view in ("", "?view=board", "?view=calendar&month=2026-11", "?view=timeline"):
        assert client.get(base + "/t/quotes" + view).status_code == 200, view
    assert "Q-0001" in client.get(base + "/t/quotes?view=calendar&month=2026-11").get_data(as_text=True)
    assert client.get(base + "/t/customers?view=cards").status_code == 200
    assert client.get(base + "/t/quote_lines").status_code == 404  # line items live inside their quote
    overview = client.get(base + "/p/overview").get_data(as_text=True)
    assert "Open quotes" in overview and "polyline" in overview and "donut" in overview and "<strong>Welcome</strong>" in overview
    assert client.post(base + f"/t/quotes/{quote.id}/set", json={"field": "status", "value": "Declined"}).get_json()["ok"]
    client.post(base + f"/t/customers/{acme.id}/comments", data={"text": "Call @Ethan tomorrow"})
    assert "Call @Ethan tomorrow" in client.get(base + f"/t/customers/{acme.id}").get_data(as_text=True)
    assert "Acme" in client.get(base + "/search?q=acme").get_data(as_text=True)
    for path in ("/automations", "/design", "/notifications", "/approvals"):
        assert client.get(base + path).status_code == 200, path
    csv = client.get(base + "/t/quotes/export.csv").get_data(as_text=True)
    assert "Q-0001" in csv and "2,640.00" in csv
    client.post(base + "/t/customers/bulk", data={"ids": [str(acme.id)], "action": "set", "field": "rating", "value": "5"})
    assert Engine(ws).get(acme.id).data["rating"] == 5

    # Settings, public form and the Microsoft 365 page (not set up on this platform).
    for path in ("/", "/appearance", "/members", "/connections", "/forms", "/data"):
        assert client.get(f"/w/{ws.id}/settings{path}").status_code == 200, path
    forms_page = client.get(f"/w/{ws.id}/settings/forms").get_data(as_text=True)
    token = db.session.get(Workspace, ws.id).settings["forms"]["customers"]
    assert f"/f/{token}" in forms_page
    public = app.test_client()
    assert "Become a customer" in public.get(f"/f/{token}").get_data(as_text=True)
    public.post(f"/f/{token}", data={"name": "Bright Co", "email": "hi@bright.co"})
    assert any(r.data["name"] == "Bright Co" for r in Engine(ws).rows("customers"))
    assert "isn't set up on this platform" in client.get(f"/w/{ws.id}/discover").get_data(as_text=True)
    zip_ = client.get(f"/w/{ws.id}/settings/data/export.zip")
    assert zip_.status_code == 200 and zip_.data[:2] == b"PK"


def test_the_assistant_looks_things_up_and_proposes_changes(client, app, owner):
    user, ws = owner
    _build(ws)
    e = Engine(ws)
    acme = e.create("customers", {"name": "Acme"})
    e.create("quotes", {"customer": acme.id, "status": "Sent"})
    login(client)
    ai.set_backend(ScriptedAI(
        {"queries": [{"table": "quotes", "measure": "count", "filters": [{"field": "status", "op": "equals", "value": "Sent"}], "label": "sent quotes"}],
         "proposals": [{"action": "create", "table": "customers", "values": {"name": "Bright Co"}, "why": "You asked to add them"}], "reply": ""},
        {"reply": "You have 1 sent quote."}))
    sent = client.post(f"/w/{ws.id}/app/assistant", json={"text": "How many quotes are out? Also add Bright Co.", "page": f"/w/{ws.id}/app/t/quotes"}).get_json()
    assert worker.assistant_tick() is True
    m = client.get(f"/w/{ws.id}/app/assistant/{sent['reply']['id']}").get_json()
    assert m["status"] == "done" and "1 sent quote" in m["html"] and "Add customer" in m["html"]
    message = db.session.get(Message, sent["reply"]["id"])
    assert message.results["lookups"][0]["matching"] == 1
    client.post(f"/w/{ws.id}/app/assistant/{message.id}/confirm/0")
    assert any(r.data["name"] == "Bright Co" for r in Engine(ws).rows("customers"))


def test_reading_a_document_and_importing_a_spreadsheet(client, app, owner):
    user, ws = owner
    _build(ws)
    Engine(ws).create("customers", {"name": "Acme Mining"})
    login(client)
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 750, "QUOTE for Acme Mining: Survey 10 x $150")
    c.save()
    ai.set_backend(ScriptedAI({"customer": "Acme Mining", "issue_date": "2026-11-01", "valid_until": None, "status": "Draft", "reason": None,
                               "quote_lines": [{"description": "Survey", "quantity": 10, "unit_price": 150}], "unsure": ["valid_until"]}))
    r = client.post(f"/w/{ws.id}/app/t/quotes/from-document", data={"files": (io.BytesIO(buf.getvalue()), "quote.pdf")}, content_type="multipart/form-data")
    job = Job.query.filter_by(kind="intake").one()
    assert worker.jobs_tick() is True and db.session.get(Job, job.id).status == "done"
    review = client.get(r.headers["Location"]).get_data(as_text=True)
    assert "Check what the AI read" in review and 'value="Survey"' in review and "check this" in review and "quote.pdf" in review

    csv = "Name,E-mail address,Stars\nBeta Co,b@beta.co,3\nGamma,not an email,2\n"
    r = client.post(f"/w/{ws.id}/app/t/customers/import", data={"file": (io.BytesIO(csv.encode()), "people.csv")}, content_type="multipart/form-data")
    ai.set_backend(ScriptedAI({"matches": [{"column": "E-mail address", "field": "email"}, {"column": "Stars", "field": "rating"}]}))
    mapping = client.get(r.headers["Location"]).get_data(as_text=True)
    assert '<option value="email" selected>' in mapping and '<option value="rating" selected>' in mapping
    go = client.post(r.headers["Location"], data={"go": "1", "map-0": "name", "map-1": "email", "map-2": "rating"})
    assert worker.jobs_tick() is True
    result = client.get(go.headers["Location"]).get_data(as_text=True)
    assert "Imported <b>1</b> customers; <b>1</b> row couldn" in result and "an email address" in result


def test_roles_and_other_peoples_erps(client, app, owner):
    user, ws = owner
    _build(ws)
    viewer = User(email="v@x.com", name="Val")
    viewer.set_password("password1234")
    stranger = User(email="s@x.com", name="Stu")
    stranger.set_password("password1234")
    db.session.add_all([viewer, stranger])
    db.session.flush()
    db.session.add(Membership(workspace_id=ws.id, user_id=viewer.id, role="viewer"))
    db.session.commit()
    client.post("/login", data={"email": "v@x.com", "password": "password1234"})
    assert client.get(f"/w/{ws.id}/app/t/customers").status_code == 200
    assert client.get(f"/w/{ws.id}/app/t/customers/new").status_code == 403
    assert client.get(f"/w/{ws.id}/settings/").status_code == 403
    client.post("/logout")
    client.post("/login", data={"email": "s@x.com", "password": "password1234"})
    assert client.get(f"/w/{ws.id}/app/").status_code == 404
    assert client.get(f"/w/{ws.id}/build").status_code == 404


def test_starts_with_default_settings_and_security_headers(tmp_path, monkeypatch, database_url):
    from app import create_app
    monkeypatch.delenv("SECRET_KEY", raising=False)
    app = create_app({"WORKER": False, "DATA_DIR": tmp_path, "SQLALCHEMY_DATABASE_URI": database_url})
    response = app.test_client().get("/login")
    assert response.status_code == 200 and "script-src 'self'" in response.headers["Content-Security-Policy"]
    assert (tmp_path / "secret_key").read_text() == app.config["SECRET_KEY"]
    assert app.test_client().post("/login", data={"email": "x@y.com", "password": "nope"}).status_code == 400  # no CSRF token


def test_repeated_wrong_passwords_are_slowed_down(client, app, owner):
    for _ in range(10):
        assert client.post("/login", data={"email": "ethan@example.com", "password": "wrong"}).status_code == 401
    assert client.post("/login", data={"email": "ethan@example.com", "password": "password1234"}).status_code == 429


def test_signing_in_to_microsoft_365_goes_to_microsoft_with_pkce(client, app, owner):
    from urllib.parse import parse_qs, urlparse
    user, ws = owner
    app.config.update(MICROSOFT_CLIENT_ID="app-id", MICROSOFT_CLIENT_SECRET="secret")
    login(client)
    page = client.get(f"/w/{ws.id}/discover").get_data(as_text=True)
    assert "Sign in with Microsoft" in page
    go = client.post(f"/w/{ws.id}/connect/microsoft", data={"next": f"/w/{ws.id}/discover"})
    target = urlparse(go.headers["Location"])
    query = parse_qs(target.query)
    assert target.netloc == "login.microsoftonline.com" and query["client_id"] == ["app-id"]
    assert query["redirect_uri"] == ["http://localhost:5050/connect/callback"] and query["code_challenge_method"] == ["S256"]
    assert "Mail.Send" in query["scope"][0] and "Files.ReadWrite" in query["scope"][0]
    back = client.get(f"/connect/callback?state=wrong&code=x", follow_redirects=True).get_data(as_text=True)
    assert "expired or was already used" in back


def test_it_runs_on_postgresql_only(tmp_path):
    import pytest
    from app import create_app
    with pytest.raises(RuntimeError, match="PostgreSQL only"):
        create_app({"WORKER": False, "DATA_DIR": tmp_path, "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'x.db'}"})


def test_a_button_waiting_for_something_shows_what_it_waits_for_and_isnt_pressed(client, app, owner):
    """A button whose rule only does its work once something holds is shown greyed beside what it waits for (not hidden,
    so nobody wonders where it went); pressed anyway (a page open from before), it says why and nothing runs. A button
    for another stage isn't shown at all, as that stage's moves aren't."""
    _, ws = owner
    result = ops.apply(ws.blueprint, [
        {"op": "create_table", "table": "jobs", "label": "Jobs", "fields": [
            {"key": "title", "label": "Title", "type": "text", "required": True}, {"key": "approved", "label": "Design approved", "type": "checkbox"},
            {"key": "status", "label": "Status", "type": "select", "options": ["New", "Building", "Done"], "default": "New"}],
         "workflow": {"field": "status", "transitions": [{"from": ["New"], "to": "Building", "label": "Start"},
                                                        {"from": ["Building"], "to": "Done", "label": "Finish"}]}},
        {"op": "add_automation", "automation": {"key": "send_specs", "name": "Send specs", "trigger": {"type": "button", "table": "jobs", "label": "Send specs"},
                                                "conditions": [{"field": "approved", "op": "equals", "value": True}],
                                                "actions": [{"type": "notify", "to": "owner", "message": "Specs sent"}]}},
        {"op": "add_automation", "automation": {"key": "invoice_job", "name": "Invoice job", "trigger": {"type": "button", "table": "jobs", "label": "Invoice job"},
                                                "conditions": [{"field": "status", "op": "equals", "value": "Done"}],
                                                "actions": [{"type": "notify", "to": "owner", "message": "Invoiced"}]}}])
    assert not result.problems, result.problems
    chat.save_version(ws, result.blueprint, result.changes, "chat")
    login(client)
    job = Engine(ws).create("jobs", {"title": "Kitchen"})
    base = f"/w/{ws.id}/app/t/jobs/{job.id}"
    page = client.get(base).get_data(as_text=True)
    assert "Send specs" in page and "You can press “Send specs” once Design approved is yes" in page
    assert "Invoice job" not in page  # for the Done stage: not shown at New
    r = client.post(f"{base}/press/send_specs")
    assert r.status_code == 302 and not Run.query.filter_by(automation="send_specs").count()
    assert "You can press “Send specs” once" in client.get(base).get_data(as_text=True)
