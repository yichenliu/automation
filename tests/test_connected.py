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


def test_the_ai_in_a_process_is_added_switched_off_handed_a_job_and_checked_from_the_studios(app, client, owner):
    user, ws = owner
    _built(ws, APP + [{"op": "add_field", "table": "orders", "field": {"key": "photo", "type": "image"}}, {"op": "add_automation", "automation": {
        "key": "review", "name": "Review a packed order", "trigger": {"type": "record_updated", "table": "orders", "field": "status", "to": "Packed"},
        "actions": [{"type": "task", "to": "editor", "message": "Write the packing summary", "fields": ["summary"]}]}}])
    login(client)
    drawn = client.get(f"/w/{ws.id}/app/studio/workflows/item?id=rule:review").get_json()
    assert drawn["steps"][0]["ai_could"] == {"field": "Summary"}  # a job by hand the AI could do
    _wf(client, ws, "rule:review", {"do": "ai_take_over", "at": "0", "instructions": "Sum up what was packed"})
    step = next(a for a in ws.blueprint["automations"] if a["key"] == "review")["actions"][0]
    assert (step["type"], step["field"], step["review"], step["reviewer"]) == ("ai_fill", "summary", "always", "editor")
    _wf(client, ws, "rule:review", {"do": "set_ai_on", "at": "0", "on": False})  # switched off: a person does it instead
    assert next(a for a in ws.blueprint["automations"] if a["key"] == "review")["actions"][0]["manual"] is True
    assert client.get(f"/w/{ws.id}/app/studio/workflows/item?id=rule:review").get_json()["steps"][0]["ai"]["manual"]
    _wf(client, ws, "rule:review", {"do": "set_ai_on", "at": "0", "on": True})
    assert "manual" not in next(a for a in ws.blueprint["automations"] if a["key"] == "review")["actions"][0]
    _wf(client, ws, "rule:review", {"do": "set_review", "at": "0", "mode": "never"})  # trusted: it runs on its own
    assert "review" not in next(a for a in ws.blueprint["automations"] if a["key"] == "review")["actions"][0]
    # a field the AI fills in, from the Data model; listed with the AI helpers; stopped again
    _dm(client, ws, {"do": "update_field", "table": "orders", "key": "notes", "ai": "Note anything fragile"})
    assert schema.field(schema.table(ws.blueprint, "orders"), "notes")["ai"]["instructions"] == "Note anything fragile"
    page = client.get(f"/w/{ws.id}/app/ai").get_data(as_text=True)
    assert "Fields the AI fills in" in page and "Note anything fragile" in page and "See the whole process, in Workflows" in page
    _dm(client, ws, {"do": "update_field", "table": "orders", "key": "notes", "ai": ""})
    assert "ai" not in schema.field(schema.table(ws.blueprint, "orders"), "notes")
    said = client.post(f"/w/{ws.id}/app/studio/data/change", json={"do": "update_field", "table": "orders", "key": "photo", "ai": "Guess"}).get_json()
    assert "The AI can't fill in Photo" in said["error"]


def test_going_back_by_any_way_names_the_records_as_they_were(app, client, owner):
    user, ws = owner
    _built(ws, APP)
    e = Engine(ws)
    order = e.create("orders", {"number": "SO-1"})
    login(client)
    made = _wf(client, ws, "stages:orders", {"do": "rename_stage", "old": "New", "new": "Received"})
    e.reset()
    assert e.get(order.id).data["status"] == "Received"
    client.post(f"/w/{ws.id}/app/design/restore/{made['before']}")  # Versions' Go back, not the studio's own undo
    e.reset()
    assert e.get(order.id).data["status"] == "New" and "New" in schema.field(schema.table(ws.blueprint, "orders"), "status")["options"]


def test_the_menus_put_each_thing_in_one_place_and_join_them_up(app, client, owner):
    user, ws = owner
    _built(ws, APP)
    login(client)
    home = client.get(f"/w/{ws.id}/app/").get_data(as_text=True)
    for label in ("Your app", "Data model", "Workflows", "Versions", "AI helpers", "What the AI knows", "Keep it right", "Checks", "Suggestions"):
        assert f">{label}<" in home, label
    assert "How it's set up" not in home and ">Rules<" not in home  # rules live in Workflows now
    page = client.get(f"/w/{ws.id}/app/design").get_data(as_text=True)
    assert "<h1>" in page and "Versions" in page and "Your app now" in page
    listing = client.get(f"/w/{ws.id}/app/t/orders").get_data(as_text=True)
    assert f"/w/{ws.id}/app/studio/data?open=orders" in listing and f"/w/{ws.id}/app/studio/workflows?table=orders" in listing
    opened = client.get(f"/w/{ws.id}/app/studio/workflows?table=orders").get_data(as_text=True)
    assert '"kind": "stages"' in opened and '"table": "orders"' in opened
    drawn = client.get(f"/w/{ws.id}/app/studio/workflows/item?id=rule:on_ship").get_json()
    assert drawn["recent"] == [] and drawn["hook"] is None and drawn["can_run"] is False
    for place in ("Data model", "Workflows", "Versions", "AI helpers", "Settings"):  # ⌘K finds each by name
        assert place in [i["label"] for i in client.get(f"/w/{ws.id}/app/jump?q={place.lower()}").get_json()["items"]], place
    settings = client.get(f"/w/{ws.id}/settings/").get_data(as_text=True)
    for label in ("Your business", "Business details", "People and teams", "Connected", "API and AI assistants", "Looking after it",
                  "AI on this computer", "Data and backups", "Security", "Audit log", "Health"):
        assert label in settings, label
    assert "Sign people out after" not in settings  # it's under Security now
    assert "Sign people out after" in client.get(f"/w/{ws.id}/settings/security").get_data(as_text=True)
    assert "PostgreSQL" in client.get(f"/w/{ws.id}/settings/data").get_data(as_text=True)
    assert "AI helpers" in client.get(f"/w/{ws.id}/settings/ai").get_data(as_text=True)
    assert "Design" in client.get(f"/w/{ws.id}/settings/health").get_data(as_text=True)
    assert prompts.looking_at(f"/w/{ws.id}/app/studio/workflows?open=rule:on_ship").endswith("the automation on_ship (\"this\" means it).")


def test_a_choice_renamed_by_the_builder_takes_its_records_and_rules_with_it_and_undo_brings_them_back(app, client, owner):
    from app.blueprint import ops
    user, ws = owner
    _built(ws, APP)
    e = Engine(ws)
    order = e.create("orders", {"number": "SO-1"})
    before = ws.version
    # the AI says it as a rename: the stage, its moves and the rule on it are renamed, and the records follow
    result = _built(ws, [{"op": "rename_choice", "table": "orders", "key": "status", "old": "Shipped", "new": "Sent"},
                         {"op": "rename_choice", "table": "orders", "key": "status", "old": "New", "new": "Received"}])
    assert "Renamed Status “Shipped” to “Sent” in Orders" in result.changes and "1 record now say “Received”" in result.changes
    assert next(a for a in ws.blueprint["automations"] if a["key"] == "on_ship")["conditions"][0]["value"] == "Sent"
    e.reset()
    assert e.get(order.id).data["status"] == "Received"
    # or by rewriting the options with one changed in its place: the records still follow
    _built(ws, [{"op": "update_field", "table": "orders", "key": "status", "changes": {"options": ["Received", "Boxed", "Sent"]}}])
    e.reset()
    assert schema.field(schema.table(ws.blueprint, "orders"), "status")["options"] == ["Received", "Boxed", "Sent"]
    # undoing goes all the way back: the records are named as they were
    chat.restore(ws, before, by=user.id)
    e.reset()
    assert e.get(order.id).data["status"] == "New"
    assert not ops.apply(ws.blueprint, []).problems


def test_staff_see_only_what_they_may_open_and_lists_they_cant_add_to_offer_no_add(app, client, owner):
    from .test_combined import OUTLOOK
    from .test_wave2 import _person
    user, ws = owner
    _built(ws, OUTLOOK)
    e = Engine(ws)
    flour = e.create("items", {"name": "Flour"})
    e.create("movements", {"item": flour.id, "quantity": 5})
    _person(ws, "wes@example.com", "Wes", "editor")
    client.post("/login", data={"email": "wes@example.com", "password": "password1234"})
    home = client.get(f"/w/{ws.id}/app/").get_data(as_text=True)
    assert ">Data model<" in home and ">AI helpers<" in home
    assert ">Checks<" not in home and ">Keep it right<" not in home and ">Settings<" not in home  # an admin's
    item = client.get(f"/w/{ws.id}/app/t/items/{flour.id}").get_data(as_text=True)
    assert "/t/outlook/new" not in item  # put together by itself: nothing to add to it from here
    assert "/t/movements/new" in item
