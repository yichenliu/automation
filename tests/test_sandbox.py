"""A test copy: changes tried on a copy of the app and its records, then published to the live app."""
from app import sandbox
from app.engine.records import Engine
from app.models import Record, Version, Workspace

from .conftest import login
from .test_wave2 import _built

SHOP = [
    {"op": "create_table", "table": "customers", "label": "Customers", "fields": [{"key": "name", "type": "text", "required": True}]},
    {"op": "create_table", "table": "orders", "label": "Orders", "fields": [
        {"key": "number", "type": "autonumber", "prefix": "ORD-"}, {"key": "customer", "type": "link", "table": "customers"},
        {"key": "status", "type": "select", "options": ["Open", "Held", "Shipped"], "default": "Open"}, {"key": "note", "type": "text"}]},
]


def test_a_test_copy_has_its_own_records_and_publishing_brings_only_the_design(app, client, owner):
    user, ws = owner
    _built(ws, SHOP)
    e = Engine(ws)
    acme = e.create("customers", {"name": "Acme"})
    e.create("orders", {"customer": acme.id, "status": "Held", "note": "Call first"})
    e.create("orders", {"customer": acme.id})
    login(client)
    made = client.post(f"/w/{ws.id}/app/test-copy")
    copy = Workspace.query.filter(Workspace.id != ws.id).one()
    assert made.headers["Location"].endswith(f"/w/{copy.id}/app/")
    assert "Test copy." in client.get(f"/w/{copy.id}/app/").get_data(as_text=True)
    c = Engine(copy)
    copied_acme = next(r for r in c.rows("customers"))
    assert copied_acme.id != acme.id and {r.data["customer"] for r in c.rows("orders")} == {copied_acme.id}  # links point at the copies
    assert c.create("orders", {"customer": copied_acme.id}).data["number"] == "ORD-0003"  # numbering goes on from the live app's
    _built(copy, [{"op": "update_field", "table": "orders", "field": "status",
                   "changes": {"options": ["Open", "Shipped", "Cancelled"]}},
                  {"op": "update_field", "table": "orders", "field": "note", "changes": {"required": True}},
                  {"op": "add_field", "table": "orders", "field": {"key": "carrier", "type": "text"}}])
    page = client.get(f"/w/{copy.id}/app/publish").get_data(as_text=True)
    assert "Orders: adds Carrier" in page and "Orders: Note must be filled in" in page
    assert "1 order is “Held” in status, which is no longer a choice" in page
    assert "1 order has no note, which must now be filled in" in page  # the live app's records (one has a note), before it's done
    client.post(f"/w/{copy.id}/app/publish")
    db_ws = Workspace.query.get(ws.id)
    assert [f["key"] for f in next(t for t in db_ws.blueprint["tables"] if t["key"] == "orders")["fields"]][-1] == "carrier"
    assert Version.query.filter_by(workspace_id=ws.id, source="published").count() == 1
    assert Record.query.filter_by(workspace_id=ws.id, table="orders").count() == 2  # the test copy's order stayed there
    assert "Nothing to publish" in client.post(f"/w/{copy.id}/app/publish", follow_redirects=True).get_data(as_text=True)


def test_the_changes_are_said_plainly():
    before = {"tables": [{"key": "jobs", "label": "Jobs", "fields": [{"key": "a", "label": "A", "type": "text"}]}], "automations": []}
    after = {"tables": [{"key": "jobs", "label": "Jobs", "fields": [{"key": "a", "label": "A", "type": "number"}]},
                        {"key": "staff", "label": "Staff", "fields": []}],
             "automations": [{"key": "x", "name": "Chase", "trigger": {}, "actions": []}]}
    assert sandbox.changes(before, after) == ["Adds the list Staff", "Jobs: A becomes number (was text)", "Adds the rule “Chase”"]
