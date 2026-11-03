"""What an ERP needs underneath: saving a record and its lines together under rules, locks, refusing out-of-date
saves, permissions by table, record and field, dates with times, currencies, pivot reports, the API and webhooks, and
the AI finding things in the business's documents, mail and records (RAG) without showing anyone what they may not see."""
import re

import numpy as np
import pytest
from sqlalchemy import text

from app import ai, knowledge, migrate
from app.blueprint import ops
from app.builder import chat
from app.engine import automations
from app.engine.records import Conflict, Engine, FieldErrors
from app.extensions import db
from app.models import Chunk, Membership, Message, Record, User
from app.storage import save

from .conftest import ScriptedAI, login

DESIGN = [
    {"op": "create_table", "table": "accounts", "label": "Accounts", "module": "finance", "fields": [
        {"key": "name", "type": "text", "required": True},
        {"key": "type", "type": "select", "options": ["Asset", "Liability", "Income", "Expense"]},
        {"key": "balance", "type": "rollup", "table": "journal_lines", "via": "account", "aggregate": "sum", "field": "net"}]},
    {"op": "create_table", "table": "journal_entries", "label": "Journal entries", "singular": "Journal entry", "title_field": "number",
     "fields": [{"key": "number", "type": "autonumber", "prefix": "JE-"}, {"key": "memo", "type": "text"},
                {"key": "status", "type": "select", "options": ["Draft", "Posted"], "default": "Draft"},
                {"key": "note", "type": "text"},
                {"key": "total_debit", "type": "rollup", "table": "journal_lines", "via": "entry", "aggregate": "sum", "field": "debit"},
                {"key": "total_credit", "type": "rollup", "table": "journal_lines", "via": "entry", "aggregate": "sum", "field": "credit"}],
     "rules": [{"check": "total_debit == total_credit", "message": "Debits must equal credits",
                "when": [{"field": "status", "op": "equals", "value": "Posted"}]}],
     "lock_if": [{"field": "status", "op": "equals", "value": "Posted"}], "lock_except": ["note"]},
    {"op": "create_table", "table": "journal_lines", "label": "Journal lines", "embedded": True, "fields": [
        {"key": "entry", "type": "link", "table": "journal_entries"}, {"key": "account", "type": "link", "table": "accounts"},
        {"key": "debit", "type": "money"}, {"key": "credit", "type": "money"},
        {"key": "net", "type": "formula", "expression": "debit - credit", "format": "money"}]},
    {"op": "create_table", "table": "staff", "label": "Staff", "access": {"view": "admin"}, "fields": [
        {"key": "name", "type": "text"}, {"key": "pay_rate", "type": "money"}, {"key": "notes", "type": "long_text"}]},
    {"op": "create_table", "table": "deals", "label": "Deals", "row_access": {"field": "rep", "roles": ["editor"]}, "fields": [
        {"key": "name", "type": "text"}, {"key": "rep", "type": "user"},
        {"key": "amount", "type": "money", "currency_field": "currency"},
        {"key": "currency", "type": "select", "options": ["AUD", "USD"], "default": "AUD"},
        {"key": "amount_aud", "type": "formula", "expression": "convert(amount, currency)", "format": "money"},
        {"key": "cost", "type": "money", "access": "admin"},
        {"key": "meeting", "type": "datetime"}, {"key": "meeting_end", "type": "datetime"},
        {"key": "meeting_hours", "type": "formula", "expression": "hours_between(meeting, meeting_end)"}]},
    {"op": "add_page", "page": {"label": "Reports", "widgets": [
        {"type": "pivot", "table": "deals", "rows": "name", "across": "currency", "aggregate": "sum", "field": "amount_aud", "label": "Pipeline"}]}},
    {"op": "add_automation", "automation": {"key": "shop_orders", "name": "Shop orders", "trigger": {"type": "webhook"}, "actions": [
        {"type": "create_record", "table": "deals", "values": {"name": "{{payload.customer.name}} order", "amount": "{{payload.total}}"}}]}},
]


def _person(ws, email, name, role):
    user = User(email=email, name=name)
    user.set_password("password1234")
    db.session.add(user)
    db.session.flush()
    member = Membership(workspace_id=ws.id, user_id=user.id, role=role)
    db.session.add(member)
    db.session.commit()
    return member


@pytest.fixture
def erp(app, owner):
    user, ws = owner
    result = ops.apply(ws.blueprint, DESIGN)
    assert not result.problems, result.problems
    chat.save_version(ws, result.blueprint, result.changes, "chat")
    ws.settings = {**ws.settings, "money": {"base": "AUD", "rates": {"USD": 1.5}}}
    db.session.commit()
    me = Membership.query.filter_by(workspace_id=ws.id, user_id=user.id).first()
    ava = _person(ws, "ava@example.com", "Ava", "editor")
    vic = _person(ws, "vic@example.com", "Vic", "viewer")
    return {"ws": ws, "owner": me, "ava": ava, "vic": vic}


def _as(erp, who):
    return Engine(erp["ws"], by=erp[who].user.display, member=erp[who])


def _entry(engine, status, lines, **extra):
    with engine.atomic():
        entry = engine.create("journal_entries", {"memo": "Rent", "status": status, **extra})
        for account, debit, credit in lines:
            engine.create("journal_lines", {"entry": entry.id, "account": account, "debit": debit, "credit": credit})
    return entry


def test_a_record_and_its_lines_save_together_only_when_its_rules_hold(erp):
    engine = Engine(erp["ws"], by="Ethan")
    engine.create("accounts", {"name": "Rent", "type": "Expense"})
    engine.create("accounts", {"name": "Bank", "type": "Asset"})
    with pytest.raises(FieldErrors) as unbalanced:
        _entry(engine, "Posted", [("Rent", 100, None), ("Bank", None, 90)])
    assert "Debits must equal credits" in str(unbalanced.value)
    assert Record.query.filter(Record.table.in_(["journal_entries", "journal_lines"])).count() == 0  # nothing half-saved
    draft = _entry(engine, "Draft", [("Rent", 100, None)])  # the rule only applies once posted
    posted = _entry(engine, "Posted", [("Rent", 2640, None), ("Bank", None, 2640)])
    engine.reset()
    assert [engine.value(engine.get(r.id), "number") for r in (draft, posted)] == ["JE-0001", "JE-0002"]  # no gap from the refused one
    rent = next(r for r in engine.rows("accounts") if r.data["name"] == "Rent")
    assert engine.value(rent, "balance") == 2740
    with pytest.raises(FieldErrors, match="Debits must equal credits"):  # posting the unbalanced draft is refused too
        engine.update(engine.get(draft.id), {"status": "Posted"})
    assert engine.get(draft.id).data["status"] == "Draft"


def test_posted_records_lock_for_people_below_admin(erp, client):
    system = Engine(erp["ws"], by="Ethan")
    system.create("accounts", {"name": "Rent"})
    posted = _entry(system, "Posted", [("Rent", 50, 50)])
    ava = _as(erp, "ava")
    record = ava.get(posted.id)
    assert "locked" in ava.locked(record)
    with pytest.raises(FieldErrors, match="locked"):
        ava.update(record, {"memo": "Changed"})
    with pytest.raises(FieldErrors, match="locked"):
        ava.delete(record)
    with pytest.raises(FieldErrors, match="locked"):  # nor can lines be added to it
        ava.create("journal_lines", {"entry": posted.id, "account": "Rent", "debit": 1})
    assert ava.update(record, {"note": "Checked by Ava"})  # the fields left editable still are
    admin = _as(erp, "owner")
    assert admin.locked(admin.get(posted.id)) is None
    admin.update(admin.get(posted.id), {"memo": "Office rent"})
    client.post("/login", data={"email": "ava@example.com", "password": "password1234"})
    page = client.get(f"/w/{erp['ws'].id}/app/t/journal_entries/{posted.id}").get_data(as_text=True)
    assert "only an admin can change it" in page and f"/t/journal_entries/{posted.id}/edit" not in page
    assert client.post(f"/w/{erp['ws'].id}/app/t/journal_entries/{posted.id}/delete").status_code == 302
    assert Record.query.get(posted.id).deleted_at is None


def test_a_save_from_an_out_of_date_copy_is_refused(erp, client):
    engine = Engine(erp["ws"], by="Ethan")
    entry = engine.create("journal_entries", {"memo": "Rent"})
    seen = entry.version
    engine.update(entry, {"memo": "Rent for May"}, expected_version=seen)
    assert entry.version == seen + 1
    with pytest.raises(Conflict):
        engine.update(entry, {"memo": "Rent for June"}, expected_version=seen)
    login(client)
    wid = erp["ws"].id
    form = client.get(f"/w/{wid}/app/t/journal_entries/{entry.id}/edit").get_data(as_text=True)
    version = re.search(r'name="_version" value="(\d+)"', form).group(1)
    engine.update(engine.get(entry.id), {"memo": "Changed by someone else"})
    stale = client.post(f"/w/{wid}/app/t/journal_entries/{entry.id}/edit", data={"memo": "Mine", "status": "Draft", "_version": version})
    assert stale.status_code == 409 and "Someone else saved this" in stale.get_data(as_text=True)
    db.session.expire_all()
    assert Record.query.get(entry.id).data["memo"] == "Changed by someone else"


def test_people_see_only_the_tables_records_and_fields_their_role_allows(erp, client):
    ws, system = erp["ws"], Engine(erp["ws"], by="Ethan")
    system.create("staff", {"name": "Bob", "pay_rate": 45})
    mine = system.create("deals", {"name": "Ava's deal", "rep": erp["ava"].user_id, "amount": 100, "cost": 60})
    theirs = system.create("deals", {"name": "Vic's deal", "rep": erp["vic"].user_id, "amount": 200, "cost": 90})
    ava = _as(erp, "ava")
    assert not ava.can("staff") and ava.can("deals", "edit")
    assert [r.id for r in ava.query("deals")] == [mine.id]
    assert "cost" not in [f["key"] for f in ava.visible(ava.tables["deals"])["fields"]]
    assert "staff" not in [t["key"] for t in ava.visible_blueprint()["tables"]]
    client.post("/login", data={"email": "ava@example.com", "password": "password1234"})
    base = f"/w/{ws.id}/app"
    assert client.get(f"{base}/t/staff").status_code == 403  # kept from them, and said so (not "moved or deleted")
    listing = client.get(f"{base}/t/deals").get_data(as_text=True)
    assert "Ava&#39;s deal" in listing and "Vic&#39;s deal" not in listing and "Cost" not in listing and "Staff" not in listing
    assert client.get(f"{base}/t/deals/{theirs.id}").status_code == 404
    assert client.post(f"{base}/t/deals/{mine.id}/set", json={"field": "cost", "value": 1}).status_code == 400
    export = client.get(f"{base}/t/deals/export.csv").get_data(as_text=True)
    assert "Vic" not in export and "Cost" not in export
    client.post("/logout")
    client.post("/login", data={"email": "vic@example.com", "password": "password1234"})
    assert client.get(f"{base}/t/deals/new").status_code == 403
    assert "Vic&#39;s deal" in client.get(f"{base}/t/deals").get_data(as_text=True)  # viewers aren't limited to their own here


def test_dates_with_times_currencies_and_pivot_reports(erp, client):
    engine = Engine(erp["ws"], by="Ethan")
    deal = engine.create("deals", {"name": "Export", "amount": 100, "currency": "USD",
                                   "meeting": "2026-11-03 2:30 pm", "meeting_end": "03/11/2026 16:00"})
    engine.create("deals", {"name": "Local", "amount": 80})
    deal = engine.get(deal.id)
    assert deal.data["meeting"] == "2026-11-03T14:30"
    f = engine.field("deals", "meeting")
    assert engine.display(f, deal.data["meeting"]) == "3 Nov 2026, 2:30 pm"
    assert engine.value(deal, "meeting_hours") == 1.5
    assert engine.matches(deal, [{"field": "meeting", "op": "at_least", "value": "2026-11-03 12:00"}])
    assert not engine.matches(deal, [{"field": "meeting", "op": "at_least", "value": "2026-11-03 15:00"}])
    assert engine.value(deal, "amount_aud") == 150.0
    assert engine.display(engine.field("deals", "amount"), 100, deal) == "US$100.00"
    login(client)
    page = client.get(f"/w/{erp['ws'].id}/app/p/reports").get_data(as_text=True)
    assert "Pipeline" in page and "$150.00" in page and "$230.00" in page  # the pivot's grand total


def _concepts(texts):
    """A tiny stand-in for the embedding model: words that mean the same thing point the same way."""
    groups = [("refund", "money back", "return", "returns"), ("delivery", "shipping", "freight"), ("pay", "salary", "wage"),
              ("leak", "drip", "water")]
    out = []
    for t in texts:
        low = t.lower()
        v = np.array([sum(low.count(w) for w in g) for g in groups] + [0.05], dtype=np.float32)
        out.append((v / np.linalg.norm(v)).tolist())
    return out


def test_the_ai_finds_documents_mail_and_records_by_meaning_and_never_shows_what_people_may_not_see(erp, app, client):
    app.config["OLLAMA_EMBED_MODEL"] = "test-embed"
    ai.set_embedder(_concepts)
    ws, system = erp["ws"], Engine(erp["ws"], by="Ethan")
    policy = save(ws.id, b"Returns policy\n\nCustom orders can be returned within 14 days for store credit only.", "Returns policy.txt",
                  source="knowledge")
    system.create("staff", {"name": "Bob", "pay_rate": 45, "notes": "Salary review due in March"})
    knowledge.touch(ws.id, "email", "m1", data={"subject": "Late delivery", "from": "acme@example.com", "body": "Our shipping is late again."})
    while knowledge.work():
        pass
    assert Chunk.query.filter(Chunk.embedding.is_(None)).count() == 0
    owner = _as(erp, "owner")
    hits = knowledge.search(owner, "can I get my money back?")
    assert hits[0]["title"] == "Returns policy.txt" and hits[0]["link"] == f"file:{policy.id}"
    assert any(h["title"].startswith("Staff Bob") for h in knowledge.search(owner, "wage review"))
    ava, vic = _as(erp, "ava"), _as(erp, "vic")
    assert not any(h["source"] == "record" for h in knowledge.search(ava, "wage review"))  # Staff is admins only
    assert [h["title"] for h in knowledge.search(ava, "freight")] == ["Late delivery"]
    assert knowledge.search(vic, "freight") == []  # mail is for editors and above
    # the assistant looks the question up first, and cites what it used
    scripted = ScriptedAI({"queries": [], "search": ["store credit"], "proposals": [], "reply": ""},
                          {"reply": "Custom orders can be returned within 14 days, for store credit only [1]."})
    ai.set_backend(scripted)
    from app import assistant
    question = assistant.ask(ws, erp["ava"].user, "Can a customer get their money back on a custom order?")
    assistant.work(question.id)
    reply = db.session.get(Message, question.id)
    assert "Returns policy" in scripted.asked[0][-1]["content"]
    assert reply.results["sources"][0]["title"] == "Returns policy.txt"
    client.post("/login", data={"email": "ava@example.com", "password": "password1234"})
    found = client.get(f"/w/{ws.id}/app/search?q=money back on returns").get_data(as_text=True)
    assert "Returns policy.txt" in found
    # an AI step told to use what the business knows gets the passages
    from app import intelligence
    deal = system.create("deals", {"name": "Refund request"})
    scripted = ScriptedAI({"value": "Store credit within 14 days."})
    ai.set_backend(scripted)
    intelligence.fill_field(system, deal, system.field("deals", "name"), "Draft our answer about the refund", use_knowledge=True)
    assert "store credit only" in scripted.asked[0][-1]["content"]
    # a new document is found straight after it's read (the vectors in memory are topped up, not reloaded)
    save(ws.id, b"Freight: deliveries over $500 ship free.", "Freight.txt", source="knowledge")
    while knowledge.work():
        pass
    assert knowledge.search(owner, "shipping costs")[0]["title"] == "Freight.txt"
    # removing the document removes it from what the AI knows
    policy.deleted_at = policy.created_at
    db.session.commit()
    assert knowledge.search(owner, "money back") == [] or knowledge.search(owner, "money back")[0]["source"] != "file"


def test_other_systems_use_the_api_with_keys_and_start_automations_by_webhook(erp, client):
    from app.web.api import make_key
    ws = erp["ws"]
    Engine(ws, by="Ethan").create("accounts", {"name": "Rent"})
    Engine(ws, by="Ethan").create("accounts", {"name": "Bank"})
    _, key = make_key(ws, "Books", "editor", erp["owner"].user_id)
    auth = {"Authorization": f"Bearer {key}"}
    tables = client.get("/api/v1/tables", headers=auth).get_json()["tables"]
    assert "staff" not in [t["key"] for t in tables] and "journal_entries" in [t["key"] for t in tables]
    made = client.post("/api/v1/tables/journal_entries/records", headers=auth, json={
        "memo": "Rent", "status": "Posted", "lines": {"journal_lines": [{"account": "Rent", "debit": 500}, {"account": "Bank", "credit": 500}]}})
    assert made.status_code == 201, made.get_json()
    body = made.get_json()
    assert body["values"]["number"] == "JE-0001" and len(body["lines"]["journal_lines"]) == 2
    refused = client.post("/api/v1/tables/journal_entries/records", headers=auth, json={
        "memo": "Bad", "status": "Posted", "lines": {"journal_lines": [{"account": "Rent", "debit": 5}]}})
    assert refused.status_code == 400 and "Debits must equal credits" in refused.get_json()["error"]
    draft = client.post("/api/v1/tables/journal_entries/records", headers=auth, json={"memo": "Draft"}).get_json()
    assert client.patch(f"/api/v1/tables/journal_entries/records/{draft['id']}", headers={**auth, "If-Match": "99"},
                        json={"memo": "x"}).status_code == 409
    changed = client.patch(f"/api/v1/tables/journal_entries/records/{draft['id']}", headers={**auth, "If-Match": str(draft["version"])},
                           json={"memo": "Electricity"})
    assert changed.status_code == 200 and changed.get_json()["values"]["memo"] == "Electricity"
    listed = client.get("/api/v1/tables/journal_entries/records?status=Posted&sort=-created_at", headers=auth).get_json()
    assert listed["total"] == 1 and listed["records"][0]["display"]["status"] == "Posted"
    assert client.patch(f"/api/v1/tables/journal_entries/records/{body['id']}", headers=auth, json={"memo": "x"}).status_code == 400  # locked
    _, read_only = make_key(ws, "BI", "viewer", erp["owner"].user_id)
    assert client.post("/api/v1/tables/accounts/records", headers={"Authorization": f"Bearer {read_only}"}, json={"name": "X"}).status_code == 403
    assert client.get("/api/v1/tables", headers={"Authorization": "Bearer keel_nope_nope"}).status_code == 401
    # a webhook automation: another system sends JSON, the automation runs with it
    url = automations.hook_url(ws.id, "shop_orders").replace("http://localhost:5050", "")
    started = client.post(url, json={"customer": {"name": "Acme"}, "total": 250})
    assert started.status_code == 202 and started.get_json()["status"] == "done"
    deal = next(r for r in Engine(ws).rows("deals") if r.data["name"] == "Acme order")
    assert deal.data["amount"] == 250
    assert client.post(url[:-4] + "0000", json={}).status_code == 404


def test_an_existing_database_is_upgraded_in_place(app):
    with db.engine.begin() as conn:
        conn.execute(text("ALTER TABLE record DROP COLUMN version"))
        conn.execute(text("PRAGMA user_version = 1"))
    migrate.upgrade()
    with db.engine.begin() as conn:
        columns = [row[1] for row in conn.execute(text("PRAGMA table_info(record)"))]
        assert "version" in columns
        assert conn.execute(text("PRAGMA user_version")).scalar() == len(migrate.STEPS)


def test_the_knowledge_api_and_money_settings_screens(erp, client):
    import io
    login(client)
    wid = erp["ws"].id
    page = client.get(f"/w/{wid}/app/knowledge")
    assert page.status_code == 200 and "What the AI knows" in page.get_data(as_text=True)
    added = client.post(f"/w/{wid}/app/knowledge", data={"action": "upload", "files": (io.BytesIO(b"Freight is free over $500."), "Freight.txt")},
                        content_type="multipart/form-data", follow_redirects=True).get_data(as_text=True)
    assert "Added Freight.txt" in added and "reading…" in added
    while knowledge.work():
        pass
    assert "Freight.txt" in client.get(f"/w/{wid}/app/knowledge?q=free freight").get_data(as_text=True)
    made = client.post(f"/w/{wid}/settings/api", data={"name": "Web shop", "role": "editor"}).get_data(as_text=True)
    key = re.search(r'value="(keel_[^"]+)"', made).group(1)
    assert client.get("/api/v1/tables", headers={"Authorization": f"Bearer {key}"}).status_code == 200
    listed = client.get(f"/w/{wid}/settings/api").get_data(as_text=True)
    assert "Web shop" in listed and key not in listed  # shown once only
    assert "/hooks/" in listed  # the webhook automation's address
    from app.models import ApiKey
    client.post(f"/w/{wid}/settings/api", data={"revoke": str(ApiKey.query.first().id)})
    assert client.get("/api/v1/tables", headers={"Authorization": f"Bearer {key}"}).status_code == 401
    client.post(f"/w/{wid}/settings/", data={"name": "Eth Analytic", "currency": "$", "base_currency": "aud", "rates": "USD 1.6\nEUR: 1.7\nnonsense"})
    db.session.refresh(erp["ws"])
    assert erp["ws"].settings["money"] == {"base": "AUD", "rates": {"USD": 1.6, "EUR": 1.7}}
    assert client.get(f"/w/{wid}/app/design").status_code == 200
    client.post("/logout")
    client.post("/login", data={"email": "ava@example.com", "password": "password1234"})
    design = client.get(f"/w/{wid}/app/design").get_data(as_text=True)
    assert "Staff" not in design and "Deals" in design


def test_reps_see_the_orders_of_their_own_customers(erp):
    ws = erp["ws"]
    result = ops.apply(ws.blueprint, [
        {"op": "create_table", "table": "customers", "fields": [{"key": "name", "type": "text"}, {"key": "rep", "type": "user"}]},
        {"op": "create_table", "table": "orders", "row_access": {"field": "customer.rep", "roles": ["editor"]},
         "fields": [{"key": "ref", "type": "text"}, {"key": "customer", "type": "link", "table": "customers"}]}])
    assert not result.problems, result.problems
    chat.save_version(ws, result.blueprint, result.changes, "chat")
    system = Engine(ws, by="Ethan")
    system.create("customers", {"name": "Cafe A", "rep": erp["ava"].user_id})
    system.create("customers", {"name": "Cafe B", "rep": erp["vic"].user_id})
    system.create("orders", {"ref": "A1", "customer": "Cafe A"})
    system.create("orders", {"ref": "B1", "customer": "Cafe B"})
    assert [r.data["ref"] for r in _as(erp, "ava").query("orders")] == ["A1"]
    assert len(_as(erp, "owner").query("orders")) == 2
