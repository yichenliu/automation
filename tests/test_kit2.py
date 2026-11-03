"""The second round of the kit: more field types and field rules, grouped lists, week calendars, resource lanes,
duplicating and following records, page periods, map/embed/AI widgets, more automation steps, account security,
updating imports, Excel export, PDFs in bulk, calendar feeds, the recycle bin, notification emails and ⌘K."""
import base64
import io
import json
import re
import zipfile
from datetime import timedelta

import pytest

from app import ai, totp
from app.blueprint import ops
from app.builder import chat
from app.engine import automations
from app.engine.records import Engine, FieldErrors
from app.extensions import db
from app.models import Follow, Membership, Notification, Record, Run, User, utcnow

from .conftest import ScriptedAI, login

DESIGN = [
    {"op": "create_table", "table": "customers", "label": "Customers", "fields": [
        {"key": "name", "type": "text", "required": True},
        {"key": "abn", "type": "text", "pattern": "^\\d{11}$", "pattern_message": "An ABN is 11 digits", "unique": True},
        {"key": "tags", "type": "tags"}, {"key": "brand", "type": "color"}, {"key": "opens", "type": "time"},
        {"key": "raw", "type": "json"}, {"key": "added", "type": "created_time"}, {"key": "changed_by", "type": "modified_by"},
        {"key": "score", "type": "number", "decimals": 1, "unit": "pts"}, {"key": "owner", "type": "user"}]},
    {"op": "create_table", "table": "contacts", "label": "Contacts", "fields": [
        {"key": "name", "type": "text"}, {"key": "customer", "type": "link", "table": "customers"}]},
    {"op": "create_table", "table": "quotes", "label": "Quotes", "layout": "tabs",
     "sections": [{"label": "Who", "fields": ["customer", "contact"]}, {"label": "What", "fields": ["title", "status", "reason", "total"]}],
     "fields": [{"key": "title", "type": "text"}, {"key": "customer", "type": "link", "table": "customers"},
                {"key": "contact", "type": "link", "table": "contacts", "match": {"customer": "customer"}},
                {"key": "status", "type": "select", "options": ["Draft", "Lost", "Won"], "default": "Draft"},
                {"key": "reason", "type": "text", "required_if": [{"field": "status", "op": "equals", "value": "Lost"}]},
                {"key": "total", "type": "money"}, {"key": "rep", "type": "user"}, {"key": "start", "type": "datetime"},
                {"key": "mark_won", "type": "button", "automation": "win"}, {"key": "score", "type": "number", "readonly": True}],
     "views": [{"key": "all", "type": "list", "group_by": "status", "columns": ["title", "total"]},
               {"key": "week", "type": "calendar", "date_field": "start", "mode": "week"},
               {"key": "lanes", "type": "timeline", "start_field": "start", "end_field": "start", "group_by": "rep"}],
     "documents": [{"key": "quote", "label": "Quote", "title": "Quote", "fields": ["title"], "totals": ["total"]}]},
    {"op": "create_table", "table": "quote_lines", "label": "Quote lines", "embedded": True, "fields": [
        {"key": "quote", "type": "link", "table": "quotes"}, {"key": "item", "type": "text"}, {"key": "shipped", "type": "checkbox"}]},
    {"op": "add_automation", "automation": {"key": "win", "name": "Win it", "trigger": {"type": "button", "table": "quotes", "label": "Won"},
                                            "actions": [{"type": "update_record", "values": {"status": "Won"}},
                                                        {"type": "update_each", "source": {"table": "quote_lines", "via": "quote"}, "values": {"shipped": True}}]}},
    {"op": "add_automation", "automation": {"key": "rotate", "name": "Share out quotes", "trigger": {"type": "record_created", "table": "quotes"},
                                            "conditions": [{"field": "rep", "op": "is_empty"}], "actions": [{"type": "assign", "field": "rep", "from": "editor"}]}},
    {"op": "add_automation", "automation": {"key": "chase", "name": "Chase", "trigger": {"type": "record_updated", "table": "quotes"},
                                            "conditions": [{"field": "status", "op": "changed_to", "value": "Lost"}],
                                            "actions": [{"type": "wait", "days": 3}, {"type": "notify", "message": "Call {{record.title}} back"}]}},
    {"op": "add_automation", "automation": {"key": "gone", "name": "Gone", "trigger": {"type": "record_deleted", "table": "quotes"},
                                            "actions": [{"type": "notify", "message": "{{record.title}} was deleted"}]}},
    {"op": "add_page", "page": {"key": "sales", "label": "Sales", "widgets": [
        {"type": "metric", "table": "quotes", "aggregate": "sum", "field": "total", "label": "Quoted", "date_field": "start"},
        {"type": "embed", "url": "https://example.com/report", "label": "Board report"},
        {"type": "insight", "label": "What stands out"}]}},
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
    return {"ws": ws, "owner": user, "ava": ava, "engine": Engine(ws, by="Ethan")}


def test_more_field_types_and_field_rules(kit):
    e = kit["engine"]
    acme = e.create("customers", {"name": "Acme", "abn": "51824753556", "tags": "vip, wholesale, VIP", "brand": "blue",
                                  "opens": "7:30 am", "raw": '{"id": 7, "tier": "gold"}', "score": 4.25})
    acme = e.get(acme.id)
    assert acme.data["tags"] == ["vip", "wholesale"] and acme.data["brand"] == "#2563eb" and acme.data["opens"] == "07:30"
    assert acme.data["raw"] == {"id": 7, "tier": "gold"}
    assert e.display(e.field("customers", "opens"), "07:30") == "7:30 am"
    assert e.display(e.field("customers", "score"), 4.25) == "4.2 pts"  # one decimal, with its unit
    assert e.value(acme, "changed_by") == "Ethan" and e.value(acme, "added")
    with pytest.raises(FieldErrors, match="An ABN is 11 digits"):
        e.create("customers", {"name": "Bad", "abn": "123"})
    bolt = e.create("customers", {"name": "Bolt"})
    sam = e.create("contacts", {"name": "Sam", "customer": "Acme"})
    e.create("contacts", {"name": "Kim", "customer": "Bolt"})
    with pytest.raises(FieldErrors, match="doesn't belong to the chosen customer"):
        e.create("quotes", {"title": "Q1", "customer": "Bolt", "contact": "Sam"})
    assert e.create("quotes", {"title": "Q2", "customer": "Acme", "contact": sam.id})
    with pytest.raises(FieldErrors, match="Reason is required"):
        e.create("quotes", {"title": "Q3", "status": "Lost"})
    ava = Engine(kit["ws"], by="Ava", member=Membership.query.filter_by(user_id=kit["ava"].id).first())
    with pytest.raises(FieldErrors, match="filled in automatically"):
        ava.create("quotes", {"title": "Q4", "score": 9})
    assert bolt


def test_grouped_lists_week_calendars_lanes_tabs_and_record_tools(kit, client):
    e, ws = kit["engine"], kit["ws"]
    e.create("quotes", {"title": "Fit-out", "total": 100, "status": "Won", "start": "2026-11-03 09:00", "rep": kit["owner"].id})
    q = e.create("quotes", {"title": "Repair", "total": 50, "start": "2026-11-04 14:30", "rep": kit["ava"].id})
    e.create("quote_lines", {"quote": q.id, "item": "Tap"})
    login(client)
    base = f"/w/{ws.id}/app/t/quotes"
    listing = client.get(base).get_data(as_text=True)
    assert "group-row" in listing and "Won" in listing and "$100.00" in listing
    week = client.get(f"{base}?view=week&week=2026-11-02").get_data(as_text=True)
    assert "week-day" in week and "2:30 pm" in week and "Subscribe in Outlook" in week
    lanes = client.get(f"{base}?view=lanes").get_data(as_text=True)
    assert "tl-lane" in lanes and "Ava" in lanes
    page = client.get(f"{base}/{q.id}").get_data(as_text=True)
    assert "data-tabs" in page and "Tell me about changes" in page and "of 2" in page and "Won</button>" in page
    copied = client.post(f"{base}/{q.id}/duplicate")
    copy_id = int(re.search(r"/t/quotes/(\d+)/edit", copied.headers["Location"]).group(1))
    copy = Record.query.get(copy_id)
    assert copy.data["title"] == "Repair (copy)" and len(Engine(ws).children("quote_lines", "quote", copy_id)) == 1
    client.post(f"{base}/{q.id}/follow")
    assert Follow.query.filter_by(record_id=q.id).count() == 1
    Engine(ws, by="Ava", member=Membership.query.filter_by(user_id=kit["ava"].id).first()).update(Engine(ws).get(q.id), {"total": 60})
    assert Notification.query.filter(Notification.user_id == kit["owner"].id, Notification.text.contains("which you follow")).count() == 1
    client.post(f"{base}/{q.id}/press/win")
    lines = Engine(ws).children("quote_lines", "quote", q.id)
    assert Engine(ws).get(q.id).data["status"] == "Won" and all(l.data["shipped"] for l in lines)  # update_each


def test_automation_steps_assign_wait_delete_and_save_answers(kit, monkeypatch):
    e, ws = kit["engine"], kit["ws"]
    first = e.create("quotes", {"title": "A"})
    second = e.create("quotes", {"title": "B"})
    reps = {Engine(ws).get(first.id).data["rep"], Engine(ws).get(second.id).data["rep"]}
    assert reps == {kit["owner"].id, kit["ava"].id}  # shared out in turn
    e.update(e.get(first.id), {"status": "Lost", "reason": "Price"})
    run = Run.query.filter_by(automation="chase").first()
    assert run.status == "waiting" and run.steps[0]["result"].startswith("Waiting until")
    run.steps = [{**run.steps[0], "until": (utcnow() - timedelta(minutes=1)).isoformat()}]
    db.session.commit()
    automations.execute(Engine(ws, by="automation"), run)
    assert run.status == "done" and Notification.query.filter(Notification.text == "Call A back").count() == 1
    e.delete(e.get(second.id))
    assert Notification.query.filter(Notification.text == "B was deleted").count() == 1

    class Answer:
        status_code = 200

        def json(self):
            return {"rates": {"AUD": 1.52}}
    monkeypatch.setattr(automations.requests, "request", lambda *a, **k: Answer())
    result = ops.apply(ws.blueprint, [{"op": "add_automation", "automation": {
        "key": "rate", "name": "Rate", "trigger": {"type": "button", "table": "customers", "label": "Rate"},
        "actions": [{"type": "webhook", "url": "https://rates.example.com", "save": {"score": "rates.AUD"}}]}}])
    assert not result.problems, result.problems
    chat.save_version(ws, result.blueprint, result.changes, "chat")
    engine = Engine(ws, by="Ethan")
    c = engine.create("customers", {"name": "Rated"})
    automations.press(engine, next(a for a in engine.bp["automations"] if a["key"] == "rate"), c, "Ethan")
    assert Engine(ws).get(c.id).data["score"] == 1.52


def test_page_periods_embeds_and_the_ai_reading_the_numbers(kit, client):
    from app import jobs
    e, ws = kit["engine"], kit["ws"]
    e.create("quotes", {"title": "Now", "total": 100, "start": e.today.isoformat() + " 10:00"})
    e.create("quotes", {"title": "Old", "total": 900, "start": "2020-01-01 10:00"})
    login(client)
    everything = client.get(f"/w/{ws.id}/app/p/sales").get_data(as_text=True)
    assert "$1,000.00" in everything and 'name="period"' in everything and "iframe" in everything and "reading today" in everything
    assert "$100.00" in client.get(f"/w/{ws.id}/app/p/sales?period=this_month").get_data(as_text=True)
    ai.set_backend(ScriptedAI({"text": "Quotes are up; chase the old ones."}))
    job = jobs.next_job()
    assert job.kind == "page_insight"
    jobs.run(job)
    assert "chase the old ones" in client.get(f"/w/{ws.id}/app/p/sales").get_data(as_text=True)
    pdf = client.get(f"/w/{ws.id}/app/p/sales.pdf?period=this_month")
    assert pdf.status_code == 200 and pdf.data[:4] == b"%PDF"


def test_two_step_sign_in_reset_links_and_microsoft_sign_in(kit, client, monkeypatch):
    login(client)
    client.post("/account/", data={"action": "2fa_start"})
    with client.session_transaction() as s:
        secret = s["totp_setup"]
    client.post("/account/", data={"action": "2fa_confirm", "code": totp.code(secret)})
    assert User.query.get(kit["owner"].id).totp_secret
    client.post("/logout")
    step = client.post("/login", data={"email": "ethan@example.com", "password": "password1234"})
    assert step.headers["Location"].endswith("/login/code")
    assert client.post("/login/code", data={"code": "000000"}).status_code == 401
    assert client.post("/login/code", data={"code": totp.code(secret)}).status_code == 302
    assert client.get("/account/").status_code == 200
    made = client.post(f"/w/{kit['ws'].id}/settings/members", data={"action": "reset_link",
                                                                     "member": Membership.query.filter_by(user_id=kit["ava"].id).first().id},
                       follow_redirects=True).get_data(as_text=True)
    link = re.search(r"http://localhost:5050(/reset/[\w.\-]+)", made).group(1)
    client.post("/logout")
    client.post(link, data={"password": "a-new-password-1"})
    assert client.post("/login", data={"email": "ava@example.com", "password": "a-new-password-1"}).status_code == 302
    assert client.get(link).status_code == 410  # used once
    client.post("/logout")
    from app.connectors import microsoft
    app_ = client.application
    app_.config.update(MICROSOFT_CLIENT_ID="client-1", MICROSOFT_CLIENT_SECRET="secret")

    def microsoft_says(start, **who):
        state = re.search(r"state=([\w\-]+)", start.headers["Location"]).group(1)
        with client.session_transaction() as s:
            nonce = s["ms_login"]["nonce"]
        claims = {"aud": "client-1", "nonce": nonce, "exp": 9_999_999_999, "tid": "tenant-1", **who}
        token = "x." + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=") + ".y"
        monkeypatch.setattr(microsoft, "_token", lambda form: {"id_token": token})
        return client.get(f"/connect/callback?state={state}&code=abc")
    # an account whose Microsoft sign-in isn't linked can't be reached by an email claim (anyone's tenant can claim any email)
    refused = microsoft_says(client.get("/login/microsoft"), oid="attacker", email="ava@example.com")
    assert refused.headers["Location"].endswith("/login")
    client.post("/login", data={"email": "ava@example.com", "password": "a-new-password-1"})
    linked = microsoft_says(client.post("/account/", data={"action": "ms_link"}), oid="ava-oid", email="ava@example.com")
    assert linked.headers["Location"].endswith("/account/") and User.query.get(kit["ava"].id).ms_identity == "tenant-1:ava-oid"
    client.post("/logout")
    back = microsoft_says(client.get("/login/microsoft"), oid="ava-oid", email="someone-else@example.com")
    assert back.status_code == 302 and "ava@example.com" in client.get("/account/").get_data(as_text=True)
    client.post("/logout")
    # the owner has two-step sign-in on: Microsoft sign-in still asks for the code
    User.query.get(kit["owner"].id).ms_identity = "tenant-1:owner-oid"
    db.session.commit()
    step = microsoft_says(client.get("/login/microsoft"), oid="owner-oid")
    assert step.headers["Location"].endswith("/login/code")


def test_updating_imports_excel_bulk_pdfs_feeds_the_bin_emails_and_jump(kit, client, monkeypatch):
    from app import importer, jobs
    from app.storage import save
    e, ws = kit["engine"], kit["ws"]
    e.create("customers", {"name": "Acme", "abn": "51824753556"})
    sheet = save(ws.id, b"Name,ABN\nAcme Pty,51824753556\nNew Co,12345678901\n", "customers.csv")
    job = importer.start(ws, sheet.id, "customers", {"Name": "name", "ABN": "abn"}, by=kit["owner"].id, match_on="abn")
    jobs.run(job)
    assert job.result["added"] == 1 and job.result["updated"] == 1 and len(Engine(ws).rows("customers")) == 2
    q1 = e.create("quotes", {"title": "Q1", "total": 10, "start": "2026-11-05 09:00"})
    q2 = e.create("quotes", {"title": "Q2", "total": 20})
    login(client)
    base = f"/w/{ws.id}/app/t/quotes"
    from openpyxl import load_workbook
    book = load_workbook(io.BytesIO(client.get(f"{base}/export.xlsx").data))
    assert book.active["A1"].value == "id" and any(c.value == 10 for c in book.active["F"] if c.value is not None) or book.active.max_row == 3
    bundle = client.post(f"{base}/bulk", data={"ids": [str(q1.id), str(q2.id)], "action": "pdf:quote"})
    assert bundle.mimetype == "application/zip" and len(zipfile.ZipFile(io.BytesIO(bundle.data)).namelist()) == 2
    week = client.get(f"{base}?view=week").get_data(as_text=True)
    feed = re.search(r'value="http://localhost:5050(/w/\d+/app/t/quotes/calendar.ics\?token=[^"]+)"', week).group(1).replace("&amp;", "&")
    client.post("/logout")
    ics = client.get(feed).get_data(as_text=True)
    assert "BEGIN:VCALENDAR" in ics and "SUMMARY:Q1" in ics and "DTSTART:20261105T090000" in ics
    assert client.get(feed.split("token=")[0] + "token=forged").status_code == 404
    login(client)
    client.post(f"{base}/{q2.id}/delete")
    assert "Q2" in client.get(f"{base}/deleted").get_data(as_text=True)
    client.post(f"{base}/{q2.id}/restore")
    assert Engine(ws).get(q2.id) is not None
    found = client.get(f"/w/{ws.id}/app/jump?q=q1").get_json()["items"]
    assert any(i["label"] == "Q1" for i in found)
    from app.connectors import microsoft
    sent = []
    monkeypatch.setattr(microsoft, "status", lambda ws_: {"connected": True})
    monkeypatch.setattr(microsoft, "send_mail", lambda ws_, to, subject, body, **k: sent.append((to, subject)))
    User.query.get(kit["owner"].id).notify_email = True
    db.session.add(Notification(workspace_id=ws.id, user_id=kit["owner"].id, text="Approval needed: Q1"))
    db.session.commit()
    automations.email_notifications()
    assert any(to == "ethan@example.com" and "Approval needed: Q1" in subject for to, subject in sent)
    assert Notification.query.filter(Notification.emailed_at.is_(None)).count() == 0  # each looked at once


def test_saved_views_and_emailing_a_dashboard(kit, client, monkeypatch):
    ws = kit["ws"]
    login(client)
    saved = client.post(f"/w/{ws.id}/app/t/quotes/my-views", data={"label": "Lost ones", "query": "f_status=Lost&page=3"})
    assert "f_status=Lost" in saved.headers["Location"] and "page=3" not in saved.headers["Location"]
    listing = client.get(f"/w/{ws.id}/app/t/quotes").get_data(as_text=True)
    assert "Lost ones" in listing and "Save as my tab" in listing
    from app.connectors import microsoft
    sent = []
    monkeypatch.setattr(microsoft, "send_mail", lambda ws_, to, subject, body, cc="", attachments=(): sent.append(attachments))
    result = ops.apply(ws.blueprint, [{"op": "add_automation", "automation": {
        "key": "weekly", "name": "Weekly report", "trigger": {"type": "schedule", "every": "week", "weekday": "mon"},
        "actions": [{"type": "send_email", "to": "boss@example.com", "subject": "Sales", "body": "Attached.", "attach": ["page:sales"]}]}}])
    assert not result.problems, result.problems
    chat.save_version(ws, result.blueprint, result.changes, "chat")
    engine = Engine(ws, by="schedule")
    automations.run_schedule(engine, next(a for a in engine.bp["automations"] if a["key"] == "weekly"), "test")
    assert sent and sent[0][0][0].startswith("Sales") and sent[0][0][1][:4] == b"%PDF"
