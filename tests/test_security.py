"""Each hole an independent review found stays closed: nobody sees or changes what their role, rows or fields forbid,
files can't run script, sign-in can't be talked round, and saves don't damage what they don't touch."""

import pytest

from app import ai
from app.blueprint import ops
from app.builder import chat
from app.engine.records import Engine, FieldErrors
from app.extensions import db
from app.models import Membership, Record, User, Workspace
from app.storage import save

from .conftest import ScriptedAI, login

DESIGN = [
    {"op": "create_table", "table": "staff", "label": "Staff", "access": {"view": "admin"}, "fields": [
        {"key": "name", "type": "text"}, {"key": "pay_rate", "type": "money"}]},
    {"op": "create_table", "table": "orders", "label": "Orders", "row_access": {"field": "created_by", "roles": ["editor"]},
     "lock_if": [{"field": "status", "op": "equals", "value": "Posted"}],
     "fields": [{"key": "ref", "type": "text"}, {"key": "status", "type": "select", "options": ["Draft", "Posted"]},
                {"key": "cost", "type": "money", "access": "admin"}, {"key": "stamp", "type": "text", "readonly": True},
                {"key": "attachments", "type": "file"}],
     "portal": {"via": "customer", "fields": ["ref", "status"]}},
    {"op": "create_table", "table": "order_lines", "label": "Order lines", "embedded": True, "fields": [
        {"key": "order", "type": "link", "table": "orders"}, {"key": "item", "type": "text"}]},
    {"op": "create_table", "table": "customers", "label": "Customers", "fields": [{"key": "name", "type": "text"}, {"key": "email", "type": "email"}]},
    {"op": "update_table", "table": "orders", "changes": {"fields": [{"key": "customer", "type": "link", "table": "customers"}]}},
    {"op": "set_portal", "table": "customers", "email_field": "email"},
    {"op": "add_automation", "automation": {"key": "stamp", "name": "Stamp", "trigger": {"type": "record_created", "table": "orders"},
                                            "actions": [{"type": "update_record", "values": {"stamp": "auto"}}]}},
]


def _member(ws, email, name, role, contact=None):
    user = User(email=email, name=name)
    user.set_password("password1234")
    db.session.add(user)
    db.session.flush()
    db.session.add(Membership(workspace_id=ws.id, user_id=user.id, role=role, contact_id=contact))
    db.session.commit()
    return user


def _as(client, email):
    client.post("/logout")
    client.post("/login", data={"email": email, "password": "password1234"})


@pytest.fixture
def shop(app, owner):
    user, ws = owner
    result = ops.apply(ws.blueprint, DESIGN)
    assert not result.problems, result.problems
    chat.save_version(ws, result.blueprint, result.changes, "chat")
    ava = _member(ws, "ava@example.com", "Ava", "editor")
    zed = _member(ws, "zed@example.com", "Zed", "editor")
    vic = _member(ws, "vic@example.com", "Vic", "viewer")
    system = Engine(ws, by="Ethan")
    acme = system.create("customers", {"name": "Acme", "email": "acme@example.com"})
    member = lambda u: Membership.query.filter_by(user_id=u.id).first()  # noqa: E731
    zeds = Engine(ws, by="Zed", member=member(zed)).create("orders", {"ref": "Z-1", "customer": acme.id, "cost": 5})
    avas = Engine(ws, by="Ava", member=member(ava)).create("orders", {"ref": "A-1", "customer": acme.id})
    line = Engine(ws, by="Zed", member=member(zed)).create("order_lines", {"order": zeds.id, "item": "Zed's widget"})
    system.create("staff", {"name": "Bob", "pay_rate": 123456})
    return {"ws": ws, "ava": ava, "zed": zed, "vic": vic, "acme": acme, "zeds": zeds, "avas": avas, "line": line}


def test_people_only_reach_their_own_records_lines_and_files(shop, client):
    base = f"/w/{shop['ws'].id}/app"
    _as(client, "ava@example.com")
    client.post(f"{base}/t/orders/bulk", data={"ids": [str(shop["zeds"].id)], "action": "set", "field": "ref", "value": "pwned"})
    client.post(f"{base}/t/orders/bulk", data={"ids": [str(shop["zeds"].id)], "action": "delete"})
    zeds = db.session.get(Record, shop["zeds"].id)
    assert zeds.data["ref"] == "Z-1" and zeds.deleted_at is None
    client.post(f"{base}/t/orders/{shop['avas'].id}/edit", data={"ref": "A-1", "status": "Draft", "_version": shop["avas"].version + 1,
                                                                  "lines-order_lines-0-id": str(shop["line"].id),
                                                                  "lines-order_lines-0-item": "hijacked"})
    assert db.session.get(Record, shop["line"].id).data["item"] == "Zed's widget"
    client.post("/account/", data={"action": "profile", "name": "Zed"})  # renaming yourself doesn't make Zed's records yours
    assert client.get(f"{base}/t/orders/{shop['zeds'].id}").status_code == 404


def test_files_follow_the_record_and_never_run_script(shop, client):
    ws, base = shop["ws"], f"/w/{shop['ws'].id}/app"
    login(client)
    payroll = save(ws.id, b"name,pay\nBob,250000\n", "payroll.csv")
    _as(client, "vic@example.com")
    assert client.get(f"{base}/files/{payroll.id}").status_code == 404
    _as(client, "ava@example.com")
    assert client.get(f"{base}/t/orders/import/{payroll.id}").status_code == 404
    assert client.post(f"{base}/files/{payroll.id}/delete").status_code == 404
    svg = save(ws.id, b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', "x.png", record_id=shop["avas"].id,
               field="attachments", mime="image/svg+xml")
    served = client.get(f"{base}/files/{svg.id}")
    assert served.headers["Content-Type"] in ("image/png",) and "sandbox" in served.headers["Content-Security-Policy"]
    trick = save(ws.id, b"<html><script>alert(1)</script></html>", "notes.txt", record_id=shop["avas"].id, field="attachments")
    served = client.get(f"{base}/files/{trick.id}")
    assert served.headers["Content-Type"] == "application/octet-stream" and "attachment" in served.headers["Content-Disposition"]


def test_the_assistant_and_the_portal_only_see_what_they_may(shop, client):
    from app import assistant
    ws = shop["ws"]
    staff = Engine(ws).rows("staff")[0]
    scripted = ScriptedAI({"queries": [], "search": [], "proposals": [], "reply": "I can't see that."})
    ai.set_backend(scripted)
    asked = assistant.ask(ws, shop["vic"], "what's on this record?", page=f"/w/{ws.id}/app/t/staff/{staff.id}")
    assistant.work(asked.id)
    assert "123" not in scripted.asked[0][-1]["content"] and "Pay rate" not in scripted.asked[0][-1]["content"]
    internal = save(ws.id, b"internal margin notes", "margins.txt", record_id=shop["avas"].id, field="")
    customer = _member(ws, "acme@example.com", "Acme buyer", "portal", contact=shop["acme"].id)
    _as(client, customer.email)
    page = client.get(f"/portal/{ws.id}/t/orders/{shop['avas'].id}").get_data(as_text=True)
    assert "margins.txt" not in page
    assert client.get(f"/portal/{ws.id}/files/{internal.id}").status_code == 404


def test_saves_keep_readonly_fields_locks_and_automations_right(shop, client):
    ws, base = shop["ws"], f"/w/{shop['ws'].id}/app"
    avas = db.session.get(Record, shop["avas"].id)
    assert avas.data["stamp"] == "auto"  # the automation filled the read-only field, though an editor's save started it
    _as(client, "ava@example.com")
    client.post(f"{base}/t/orders/{avas.id}/edit", data={"ref": "A-1b", "status": "Draft", "_version": avas.version})
    db.session.expire_all()
    assert db.session.get(Record, avas.id).data["stamp"] == "auto"  # not wiped by a form that doesn't show it
    member = Membership.query.filter_by(user_id=shop["zed"].id).first()
    zed = Engine(ws, by="Zed", member=member)
    zed.update(zed.get(shop["zeds"].id), {"status": "Posted"})
    mine = zed.create("orders", {"ref": "Z-2"})
    loose = zed.create("order_lines", {"order": mine.id, "item": "Loose"})
    with pytest.raises(FieldErrors, match="locked"):  # a line can't be moved into a posted order
        zed.update(zed.get(loose.id), {"order": shop["zeds"].id})


def test_reset_links_and_sign_in_limits_cant_be_talked_round(shop, client, app):
    ws = shop["ws"]
    other = Workspace(owner_id=shop["zed"].id, name="Zed's own", blueprint={}, settings={}, state={})
    db.session.add(other)
    db.session.flush()
    db.session.add(Membership(workspace_id=other.id, user_id=shop["zed"].id, role="owner"))
    db.session.commit()
    login(client)
    made = client.post(f"/w/{ws.id}/settings/members", data={"action": "reset_link", "member": Membership.query.filter_by(user_id=shop["zed"].id, workspace_id=ws.id).first().id},
                       follow_redirects=True).get_data(as_text=True)
    assert "/reset/" not in made and "other apps" in made
    client.post("/logout")
    for n in range(11):
        client.post("/login", data={"email": "ava@example.com", "password": "wrong"}, headers={"X-Forwarded-For": f"10.0.0.{n}"})
    blocked = client.post("/login", data={"email": "ava@example.com", "password": "password1234"}, headers={"X-Forwarded-For": "10.9.9.9"})
    assert blocked.status_code == 429  # a new forwarded address each time doesn't reset the limit
