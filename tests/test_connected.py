"""Everything joined up: a change made in one place reaches everything that depends on it. A stage renamed in the
Workflows studio keeps its rule firing; a step added there runs on the next record; a field renamed in the Data model
studio reads that way in the records, the AI's search and the builder's view of the app; a field made private leaves
the AI's search at once; stages added in Workflows are a field in the Data model; an AI step's checking set in the
studio is what runs; and the builder hears of every change made by hand, so it builds on them."""
from app import knowledge
from app.blueprint import schema
from app.builder import chat, prompts
from app.engine.records import Engine
from app.extensions import db
from app.models import Chunk, Message

from .conftest import login
from .test_wave2 import _built

APP = [
    {"op": "create_table", "table": "customers", "label": "Customers", "fields": [{"key": "name", "type": "text", "required": True}]},
    {"op": "create_table", "table": "orders", "label": "Orders", "fields": [
        {"key": "number", "type": "text", "required": True}, {"key": "customer", "type": "link", "table": "customers"},
        {"key": "notes", "type": "text"}, {"key": "summary", "type": "text"},
        {"key": "status", "type": "select", "options": ["New", "Packed", "Shipped"], "default": "New"}],
     "workflow": {"field": "status", "start": "New", "transitions": [{"from": ["New"], "to": "Packed", "label": "Pack it"},
                                                                     {"from": ["Packed"], "to": "Shipped", "label": "Ship it"}]}},
    {"op": "add_automation", "automation": {"key": "on_ship", "name": "When an order ships", "trigger": {
        "type": "record_updated", "table": "orders", "field": "status", "to": "Shipped"},
        "actions": [{"type": "update_record", "values": {"notes": "Shipped by the rule"}}]}},
]


def _move(e, rid, stage):
    e.reset()
    e.move(e.get(rid), next(i for i, m, _ in e.moves(e.get(rid)) if m["to"] == stage))
    e.reset()


def _wf(client, ws, item, body):
    said = client.post(f"/w/{ws.id}/app/studio/workflows/change", json={"id": item, "change": body}).get_json()
    assert said.get("ok"), said
    return said


def _dm(client, ws, body):
    said = client.post(f"/w/{ws.id}/app/studio/data/change", json=body).get_json()
    assert said.get("ok"), said
    return said


def test_a_change_in_either_studio_reaches_everything_that_depends_on_it(app, client, owner):
    user, ws = owner
    _built(ws, APP)
    db.session.add_all([Message(workspace_id=ws.id, channel="builder", role="user", text="Make me an order tracker", status="done"),
                        Message(workspace_id=ws.id, channel="builder", role="assistant", text="Done.", status="done")])
    db.session.commit()
    login(client)
    e = Engine(ws)
    cafe = e.create("customers", {"name": "Cafe Uno"})

    # Workflows: the stage renamed, and the rule on it still fires (its condition was renamed with it)
    _wf(client, ws, "stages:orders", {"do": "rename_stage", "old": "Shipped", "new": "Dispatched"})
    assert next(a for a in ws.blueprint["automations"] if a["key"] == "on_ship")["conditions"][0]["value"] == "Dispatched"
    e = Engine(ws)
    first = e.create("orders", {"number": "SO-1", "customer": cafe.id})
    _move(e, first.id, "Packed")
    _move(e, first.id, "Dispatched")
    assert e.get(first.id).data["notes"] == "Shipped by the rule"
    stages = client.get(f"/w/{ws.id}/app/studio/workflows/item?id=stages:orders").get_json()
    assert stages["stages"][2]["rules"][0]["key"] == "on_ship" and stages["stages"][2]["count"] == 1

    # Workflows: a step added to the rule runs on the next order
    _wf(client, ws, "rule:on_ship", {"do": "add_step", "into": "", "index": 1,
                                     "step": {"type": "update_record", "values": {"summary": "{{record.number}} for {{record.customer}}"}}})
    e = Engine(ws)
    second = e.create("orders", {"number": "SO-2", "customer": cafe.id})
    _move(e, second.id, "Packed")
    _move(e, second.id, "Dispatched")
    assert e.get(second.id).data["summary"] == "SO-2 for Cafe Uno"

    # an AI step added in the studio, its checking set there, is what runs: a person checks every one
    _wf(client, ws, "rule:on_ship", {"do": "add_step", "into": "", "index": 2,
                                     "step": {"type": "ai_fill", "field": "summary", "instructions": "One line on the order"}})
    _wf(client, ws, "rule:on_ship", {"do": "set_review", "at": "2", "mode": "always"})
    step = next(a for a in ws.blueprint["automations"] if a["key"] == "on_ship")["actions"][2]
    assert (step["type"], step["review"]) == ("ai_fill", "always")
    drawn = client.get(f"/w/{ws.id}/app/studio/workflows/item?id=rule:on_ship").get_json()
    assert drawn["steps"][2]["ai"]["review"] == "always"

    # Data model: a field renamed reads that way everywhere: on screens, in the builder's view, in the AI's search
    _dm(client, ws, {"do": "update_field", "table": "orders", "key": "notes", "label": "Packing notes"})
    while knowledge.work():
        pass
    passage = Chunk.query.filter_by(workspace_id=ws.id, source="record", record_id=first.id).one()
    assert "Packing notes: Shipped by the rule" in passage.text
    assert 'notes: text "Packing notes"' in prompts.outline(ws.blueprint, {})
    assert "Packing notes" in client.get(f"/w/{ws.id}/app/t/orders/{first.id}").get_data(as_text=True)
    assert e.get(first.id).data["notes"] == "Shipped by the rule"  # the rule and the records go by the field's key: untouched

    # Data model: a new field with its colours is known to the builder, the API and the MCP server
    _dm(client, ws, {"do": "add_field", "table": "orders", "label": "Priority", "type": "select", "options": "Low\nHigh"})
    _dm(client, ws, {"do": "update_field", "table": "orders", "key": "priority", "option_colors": {"High": "red"}})
    assert 'priority: select [Low, High] colours {"High": "red"}' in prompts.outline(ws.blueprint, {})
    assert "priority" in client.get("/api/v1/openapi.json", headers=_key(client, ws)).get_json()["components"]["schemas"]["Orders"]["properties"]

    # Data model: a field made private leaves the AI's search at once, before the records are read again without it
    _dm(client, ws, {"do": "update_field", "table": "orders", "key": "notes", "access": "admin"})
    assert not Chunk.query.filter_by(workspace_id=ws.id, source="record", table="orders").count()
    while knowledge.work():
        pass
    assert all("Shipped by the rule" not in c.text for c in Chunk.query.filter_by(workspace_id=ws.id, source="record", table="orders"))

    # Workflows → Data model: stages given to a list are a field of it, drawn as its stages
    _wf(client, ws, "stages:customers", {"do": "add_stages", "table": "customers", "stages": "Lead\nActive"})
    model = client.get(f"/w/{ws.id}/app/studio/data.json").get_json()
    status = next(f for f in next(t for t in model["tables"] if t["key"] == "customers")["fields"] if f["key"] == "status")
    assert status["stage"] and status["options"] == ["Lead", "Active"]

    # the builder hears of each change made by hand, so it builds on them rather than putting them back
    history = chat._history(ws, 10 ** 9)
    said = " ".join(m["content"] for m in history)
    assert history[0]["content"] == "Make me an order tracker"
    assert "Meanwhile the owner changed by hand in a studio" in said and "“Shipped” to “Dispatched”" in said and "Keep it." in said
    assert "Dispatched" in prompts.outline(ws.blueprint, {})
    assert not schema.check(ws.blueprint)


def _key(client, ws):
    import re
    made = client.post(f"/w/{ws.id}/settings/api", data={"name": "Check", "role": "admin"}).get_data(as_text=True)
    return {"Authorization": f"Bearer {re.search(r'value=\"(keel_[^\"]+)\"', made).group(1)}"}


def test_nothing_but_its_lists_writes_to_a_combined_list(app, owner):
    from .test_combined import OUTLOOK
    from app.blueprint import ops
    user, ws = owner
    problems = ops.apply(ws.blueprint, OUTLOOK + [{"op": "add_automation", "automation": {
        "key": "poke", "name": "Poke the outlook", "trigger": {"type": "record_created", "table": "items"},
        "actions": [{"type": "create_record", "table": "outlook", "values": {"kind": "Committed", "item": "{{record.id}}"}}]}}]).problems
    assert any("“Poke the outlook” changes Stock outlook, which is put together from other lists" in p for p in problems)
