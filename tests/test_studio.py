"""The data model and workflow studios: the design drawn as diagrams and changed by hand, every change checked and saved as
a version that can be taken back; choices renamed on the records too; rules tried on a record without doing anything."""
import json

from app import studio
from app.blueprint import schema
from app.engine.records import Engine
from app.extensions import db
from app.models import Run

from .conftest import login
from .test_wave2 import _built, _person

SHOP = [
    {"op": "create_table", "table": "customers", "label": "Customers", "fields": [
        {"key": "name", "type": "text", "required": True}, {"key": "email", "type": "email"}]},
    {"op": "create_table", "table": "orders", "label": "Orders", "fields": [
        {"key": "number", "type": "text", "required": True}, {"key": "customer", "type": "link", "table": "customers"},
        {"key": "total", "type": "money"}, {"key": "notes", "type": "text"},
        {"key": "status", "type": "select", "options": ["New", "Packed", "Shipped"], "default": "New"}],
     "workflow": {"field": "status", "start": "New", "transitions": [{"from": ["New"], "to": "Packed", "label": "Pack it"},
                                                                     {"from": ["Packed"], "to": "Shipped", "label": "Ship it"}]}},
    {"op": "add_automation", "automation": {"key": "tell_shipped", "name": "Tell the customer it's shipped",
                                            "trigger": {"type": "record_updated", "table": "orders", "field": "status", "to": "Shipped"},
                                            "actions": [{"type": "notify", "to": "admin", "message": "{{record.number}} shipped"}]}},
]


def _change(client, ws, body):
    return client.post(f"/w/{ws.id}/app/studio/data/change", json=body)


def _wf(client, ws, item, body):
    return client.post(f"/w/{ws.id}/app/studio/workflows/change", json={"id": item, "change": body})


def test_the_data_model_is_drawn_with_its_links_and_changed_by_hand(app, client, owner):
    user, ws = owner
    _built(ws, SHOP)
    login(client)
    assert client.get(f"/w/{ws.id}/app/studio/data").headers["Location"].endswith("/studio?view=data&open=data")  # the old address
    page = client.get(f"/w/{ws.id}/app/studio?view=data").get_data(as_text=True)
    assert "App map" in page and "What it keeps" in page and "data-model" in page and "studio.js" in page
    model = client.get(f"/w/{ws.id}/app/studio/data.json").get_json()
    assert {t["key"] for t in model["tables"]} == {"customers", "orders"}
    assert model["links"] == [{"from": "orders", "field": "customer", "to": "customers", "many": False, "label": "Customer"}]
    status = next(f for f in next(t for t in model["tables"] if t["key"] == "orders")["fields"] if f["key"] == "status")
    assert status["stage"] and status["options"] == ["New", "Packed", "Shipped"]

    made = _change(client, ws, {"do": "add_table", "label": "Suppliers"}).get_json()
    assert made["ok"] and made["before"] == ws.version - 1 and any("Suppliers" in c for c in made["said"])
    assert _change(client, ws, {"do": "add_table", "label": "suppliers"}).get_json()["error"] == "There's already a list called suppliers"
    made = _change(client, ws, {"do": "link", "from": "orders", "to": "suppliers"}).get_json()  # dragged from Orders' + onto Suppliers
    link = schema.field(schema.table(ws.blueprint, "orders"), "supplier")
    assert link["type"] == "link" and link["table"] == "suppliers" and any(l["to"] == "suppliers" for l in made["model"]["links"])
    _change(client, ws, {"do": "add_field", "table": "orders", "label": "Priority", "type": "select", "options": "Low\nHigh"})
    assert schema.field(schema.table(ws.blueprint, "orders"), "priority")["options"] == ["Low", "High"]
    _change(client, ws, {"do": "update_field", "table": "orders", "key": "notes", "label": "Packing notes", "type": "long_text", "help": "For the packer"})
    notes = schema.field(schema.table(ws.blueprint, "orders"), "notes")
    assert (notes["label"], notes["type"], notes["help"]) == ("Packing notes", "long_text", "For the packer")
    keys = [f["key"] for f in schema.table(ws.blueprint, "orders")["fields"]]
    _change(client, ws, {"do": "order_fields", "table": "orders", "keys": [keys[-1]] + keys[:-1]})
    assert schema.table(ws.blueprint, "orders")["fields"][0]["key"] == keys[-1]
    assert not schema.check(ws.blueprint)


def test_what_people_entered_is_never_thrown_away_by_a_click(app, client, owner):
    user, ws = owner
    _built(ws, SHOP)
    e = Engine(ws)
    e.create("orders", {"number": "#1", "notes": "fragile", "total": 20})
    login(client)
    said = _change(client, ws, {"do": "update_field", "table": "orders", "key": "notes", "type": "number"}).get_json()["error"]
    assert "add a new field instead" in said  # text that would be misread as a number
    assert _change(client, ws, {"do": "update_field", "table": "orders", "key": "total", "type": "number"}).get_json()["ok"]  # money ↔ number keeps
    said = _change(client, ws, {"do": "update_field", "table": "orders", "key": "status", "options": ["Packed", "Shipped"]}).get_json()["error"]
    assert "1 order is “New”" in said and "rename the choice instead" in said
    said = _change(client, ws, {"do": "delete_table", "table": "customers"}).get_json()["error"]
    assert "still used by Orders › Customer" in said
    assert _change(client, ws, {"do": "remove_field", "table": "orders", "key": "status"}).get_json()["error"].startswith("Status holds the stages")


def test_renaming_a_choice_renames_it_on_the_records_and_in_the_rules_and_taking_it_back_undoes_both(app, client, owner):
    user, ws = owner
    _built(ws, SHOP)
    e = Engine(ws)
    order = e.create("orders", {"number": "#1"})
    e.move(e.get(order.id), next(i for i, m, _ in e.moves(e.get(order.id)) if m["to"] == "Packed"))
    e.move(e.get(order.id), next(i for i, m, _ in e.moves(e.get(order.id)) if m["to"] == "Shipped"))
    login(client)
    made = _change(client, ws, {"do": "rename_option", "table": "orders", "key": "status", "old": "Shipped", "new": "Dispatched"}).get_json()
    assert made["ok"] and "1 record now say “Dispatched”" in made["said"]
    orders = schema.table(ws.blueprint, "orders")
    assert schema.field(orders, "status")["options"] == ["New", "Packed", "Dispatched"]
    assert orders["workflow"]["transitions"][1]["to"] == "Dispatched"
    rule = next(a for a in ws.blueprint["automations"] if a["key"] == "tell_shipped")
    assert rule["conditions"][0]["value"] == "Dispatched"
    e.reset()
    assert e.get(order.id).data["status"] == "Dispatched"
    assert client.post(f"/w/{ws.id}/app/studio/undo", json={"to": made["before"]}).get_json()["ok"]
    e.reset()
    assert e.get(order.id).data["status"] == "Shipped" and "Shipped" in schema.field(schema.table(ws.blueprint, "orders"), "status")["options"]


def test_only_admins_change_it_and_others_see_only_what_they_may(app, client, owner):
    user, ws = owner
    _built(ws, SHOP + [{"op": "update_table", "table": "customers", "access": {"view": ["admin"], "edit": ["admin"]}}])
    _person(ws, "wes@example.com", "Wes", "editor")
    client.post("/login", data={"email": "wes@example.com", "password": "password1234"})
    model = client.get(f"/w/{ws.id}/app/studio/data.json").get_json()
    assert [t["key"] for t in model["tables"]] == ["orders"] and model["links"] == []
    page = client.get(f"/w/{ws.id}/app/studio?view=data").get_data(as_text=True)
    assert "New list" not in page and "New rule" not in page and 'data-can-edit=""' in page
    assert _change(client, ws, {"do": "add_table", "label": "Sneaky"}).status_code == 403
    assert client.get(f"/w/{ws.id}/app/studio").status_code == 200
    assert _wf(client, ws, "rule:tell_shipped", {"do": "set_enabled", "on": False}).status_code == 403


def test_stages_are_drawn_and_changed_and_a_stage_in_use_isnt_thrown_away(app, client, owner):
    user, ws = owner
    _built(ws, SHOP)
    e = Engine(ws)
    e.create("orders", {"number": "#1"})
    login(client)
    item = client.get(f"/w/{ws.id}/app/studio/workflows/item?id=stages:orders").get_json()
    assert [s["name"] for s in item["stages"]] == ["New", "Packed", "Shipped"] and item["stages"][0]["count"] == 1
    assert item["stages"][2]["rules"] == [{"key": "tell_shipped", "name": "Tell the customer it's shipped", "on": True}]
    assert [m["label"] for m in item["moves"]] == ["Pack it", "Ship it"]
    done = _wf(client, ws, "stages:orders", {"do": "add_stage", "name": "Delivered", "after": "Shipped"}).get_json()
    assert done["ok"] and [s["name"] for s in done["item"]["stages"]][-1] == "Delivered"
    assert any(m["from"] == ["Shipped"] and m["to"] == "Delivered" for m in done["item"]["moves"])
    said = _wf(client, ws, "stages:orders", {"do": "remove_stage", "name": "New"}).get_json()["error"]
    assert said == "1 order is at New: move it on first"
    done = _wf(client, ws, "stages:orders", {"do": "add_move", "from": [], "to": "New", "label": "Start again", "who": ["admin"], "requires": ["notes"]}).get_json()
    move = done["item"]["moves"][-1]
    assert (move["label"], move["from"], move["who_keys"], move["requires_keys"]) == ("Start again", [], ["admin"], ["notes"])
    done = _wf(client, ws, "stages:orders", {"do": "update_move", "i": move["i"], "label": "Reopen", "from": ["Shipped"], "to": "New"}).get_json()
    assert done["item"]["moves"][-1]["label"] == "Reopen"
    _wf(client, ws, "stages:orders", {"do": "remove_move", "i": move["i"]})
    _wf(client, ws, "stages:orders", {"do": "order_stages", "names": ["New", "Packed", "Delivered", "Shipped"]})
    assert schema.field(schema.table(ws.blueprint, "orders"), "status")["options"] == ["New", "Packed", "Delivered", "Shipped"]
    assert not schema.check(ws.blueprint)


def test_a_list_gets_stages_of_its_own(app, client, owner):
    user, ws = owner
    _built(ws, SHOP)
    login(client)
    done = _wf(client, ws, "stages:customers", {"do": "add_stages", "table": "customers", "stages": "Lead\nActive\nLost"}).get_json()
    assert done["id"] == "stages:customers" and [s["name"] for s in done["item"]["stages"]] == ["Lead", "Active", "Lost"]
    customers = schema.table(ws.blueprint, "customers")
    assert customers["workflow"]["field"] == "status" and schema.field(customers, "status")["default"] == "Lead"
    assert _wf(client, ws, "stages:customers", {"do": "remove_stages"}).get_json()["ok"]
    assert not schema.table(ws.blueprint, "customers").get("workflow")


def test_a_rule_is_built_step_by_step_in_the_diagram(app, client, owner):
    user, ws = owner
    _built(ws, SHOP)
    login(client)
    done = _wf(client, ws, "", {"do": "add_rule", "name": "Big orders", "trigger": {"type": "record_created", "table": "orders"},
                                "step": {"type": "notify", "to": "admin", "message": "A big one: {{record.number}}"}}).get_json()
    assert done["id"] == "rule:big_orders" and done["item"]["steps"][0]["type"] == "notify"
    rule = "rule:big_orders"
    _wf(client, ws, rule, {"do": "set_conditions", "conditions": [{"field": "total", "op": "greater_than", "value": "1000"}]})
    done = _wf(client, ws, rule, {"do": "add_step", "into": "", "index": 1, "step": {"type": "branch", "label": "To a customer",
                                  "when": [{"field": "customer", "op": "not_empty"}]}}).get_json()
    branch = done["item"]["steps"][1]
    assert branch["type"] == "branch" and [w["into"] for w in branch["ways"]] == ["1.0.", "1.o."]
    done = _wf(client, ws, rule, {"do": "add_step", "into": "1.0.", "index": 0, "step": {"type": "update_record", "values": {"notes": "Check stock first"}}}).get_json()
    assert done["item"]["steps"][1]["ways"][0]["steps"][0]["at"] == "1.0.0"
    done = _wf(client, ws, rule, {"do": "add_way", "at": "1", "label": "Packed already", "when": [{"field": "status", "op": "equals", "value": "Packed"}]}).get_json()
    assert [w["label"] for w in done["item"]["steps"][1]["ways"]] == ["To a customer", "Packed already", "Anything else"]
    done = _wf(client, ws, rule, {"do": "update_step", "at": "0", "step": {"type": "notify", "to": "owner", "message": "Big: {{record.total}}"}}).get_json()
    assert ws.blueprint["automations"][-1]["actions"][0] == {"type": "notify", "message": "Big: {{record.total}}", "to": "owner"}
    _wf(client, ws, rule, {"do": "move_step", "at": "0", "by": 1})
    assert ws.blueprint["automations"][-1]["actions"][1]["type"] == "notify"
    _wf(client, ws, rule, {"do": "remove_way", "at": "0", "n": 1})
    assert len(ws.blueprint["automations"][-1]["actions"][0]["paths"]) == 1
    _wf(client, ws, rule, {"do": "remove_step", "at": "0"})
    said = _wf(client, ws, rule, {"do": "remove_step", "at": "0"}).get_json()["error"]
    assert said == "A rule needs at least one step: remove the rule instead"
    _wf(client, ws, rule, {"do": "set_trigger", "trigger": {"type": "button", "table": "orders", "label": "Flag it"}})
    assert ws.blueprint["automations"][-1]["trigger"] == {"type": "button", "table": "orders", "label": "Flag it"}
    _wf(client, ws, rule, {"do": "set_enabled", "on": False})
    assert ws.blueprint["automations"][-1]["enabled"] is False
    assert not schema.check(ws.blueprint)
    page = client.get(f"/w/{ws.id}/app/studio?open={rule}").get_data(as_text=True)
    assert "App map" in page and "Big orders" in page
    assert _wf(client, ws, rule, {"do": "remove_rule"}).get_json()["id"] == ""
    assert not any(a["key"] == "big_orders" for a in ws.blueprint["automations"])


def test_trying_a_rule_on_a_record_shows_the_way_it_would_go_and_does_nothing(app, client, owner):
    user, ws = owner
    _built(ws, SHOP + [{"op": "add_automation", "automation": {
        "key": "route", "name": "Route new orders", "trigger": {"type": "record_created", "table": "orders"},
        "conditions": [{"field": "total", "op": "greater_than", "value": 10}],
        "actions": [{"type": "branch", "paths": [{"label": "Big", "when": [{"field": "total", "op": "greater_than", "value": 1000}],
                                                   "steps": [{"type": "request_approval", "approver": "owner", "message": "OK?",
                                                              "if_rejected": [{"type": "notify", "to": "creator", "message": "No"}]}]}],
                     "otherwise": [{"type": "notify", "to": "admin", "message": "Small one"}]},
                    {"type": "update_record", "only_if": [{"field": "customer", "op": "not_empty"}], "values": {"notes": "Seen"}}]}}])
    e = Engine(ws)
    big, small = e.create("orders", {"number": "#1", "total": 5000}), e.create("orders", {"number": "#2", "total": 5})
    ran = Run.query.count()
    login(client)
    said = client.post(f"/w/{ws.id}/app/studio/workflows/try", json={"id": "rule:route", "record": big.id}).get_json()
    assert said["starts"] and said["steps"]["0"]["why"] == "It goes the way “Big”"
    assert (said["steps"]["0.0.0"]["state"], said["steps"]["0.0.0.r.0"]["state"], said["steps"]["0.o.0"]["state"]) == ("runs", "maybe", "not_taken")
    assert said["steps"]["1"]["state"] == "skipped"
    said = client.post(f"/w/{ws.id}/app/studio/workflows/try", json={"id": "rule:route", "record": small.id}).get_json()
    assert not said["starts"] and said["why"] == ["Total is more than $10.00"]
    e.reset()
    assert Run.query.count() == ran and e.get(big.id).data.get("notes") is None  # nothing was done
    items = client.get(f"/w/{ws.id}/app/studio/workflows/records?table=orders&q=%232").get_json()["items"]
    assert [i["title"] for i in items] == ["#2"]


def test_how_often_each_step_ran_is_shown_on_it(app, owner):
    user, ws = owner
    _built(ws, SHOP)
    e = Engine(ws)
    order = e.create("orders", {"number": "#1"})
    for stage in ("Packed", "Shipped"):
        e.move(e.get(order.id), next(i for i, m, _ in e.moves(e.get(order.id)) if m["to"] == stage))
    runs = Run.query.filter_by(workspace_id=ws.id, automation="tell_shipped").all()
    a = next(x for x in ws.blueprint["automations"] if x["key"] == "tell_shipped")
    drawn = studio.rule_diagram(e, a, runs)
    assert drawn["runs"]["total"] == 1 and drawn["steps"][0]["counts"] == {"ran": 1, "skipped": 0, "stopped": 0}
    assert drawn["when"] == "When an order is moved to Shipped"


def test_a_flows_screens_are_changed_in_order(app, client, owner):
    user, ws = owner
    _built(ws, SHOP + [{"op": "add_flow", "flow": {"key": "pack", "label": "Pack an order", "table": "orders", "steps": [
        {"kind": "info", "label": "Get the box"}, {"kind": "form", "label": "Notes", "fields": ["notes"]}]}}])
    login(client)
    item = client.get(f"/w/{ws.id}/app/studio/workflows/item?id=flow:pack").get_json()
    assert [s["label"] for s in item["steps"]] == ["Get the box", "Notes"] and item["steps"][1]["detail"] == "Fill in Notes"
    _wf(client, ws, "flow:pack", {"do": "add_step", "kind": "info", "label": "Seal it", "text": "Tape twice"})
    _wf(client, ws, "flow:pack", {"do": "move_step", "step": "seal_it", "by": -1})
    done = _wf(client, ws, "flow:pack", {"do": "update_step", "step": "get_the_box", "label": "Fetch a box", "text": "The small ones"}).get_json()
    assert [(s["label"], s["text"]) for s in done["item"]["steps"]] == [("Fetch a box", "The small ones"), ("Seal it", "Tape twice"), ("Notes", "")]
    _wf(client, ws, "flow:pack", {"do": "remove_step", "step": "seal_it"})
    assert [s["key"] for s in schema.flows(ws.blueprint)[0]["steps"]] == ["get_the_box", "notes"]
    assert not schema.check(ws.blueprint)


def test_each_change_is_a_version_that_says_it_was_made_in_a_studio(app, client, owner):
    user, ws = owner
    _built(ws, SHOP)
    login(client)
    _change(client, ws, {"do": "update_table", "table": "orders", "label": "Sales orders"})
    page = client.get(f"/w/{ws.id}/app/design").get_data(as_text=True)
    assert "changed in a studio" in page and "Renamed the table Orders to Sales orders" in page
    assert client.post(f"/w/{ws.id}/app/studio/data/place", json={"positions": {"orders": [10, 20], "nope": [1, 1]}}).get_json()["ok"]
    assert ws.settings["studio"]["positions"] == {"orders": [10, 20]}


def test_an_app_with_an_old_problem_can_still_be_changed_by_hand_but_not_made_worse(app, client, owner):
    user, ws = owner
    _built(ws, SHOP)
    bp = json.loads(json.dumps(ws.blueprint))
    schema.table(bp, "orders")["workflow"]["start"] = "Nowhere"  # checked by a rule that came in later
    ws.blueprint = bp
    db.session.commit()
    assert schema.check(ws.blueprint)
    login(client)
    assert _change(client, ws, {"do": "add_field", "table": "customers", "label": "Phone", "type": "phone"}).get_json()["ok"]
    said = _change(client, ws, {"do": "remove_field", "table": "orders", "key": "number"}).get_json()["error"]
    assert "number" in said.lower()  # the rule's message uses it: that would be a new problem


def test_how_a_field_shows_is_set_once_for_every_screen(app, client, owner):
    from app.web.filters import pill
    user, ws = owner
    _built(ws, SHOP + [{"op": "add_field", "table": "orders", "field": {"key": "margin", "type": "number"}},
                       {"op": "add_field", "table": "orders", "field": {"key": "in_stock", "type": "checkbox"}}])
    login(client)
    _change(client, ws, {"do": "update_field", "table": "orders", "key": "status", "option_colors": {"Packed": "violet", "Nope": "red"}})
    status = schema.field(schema.table(ws.blueprint, "orders"), "status")
    assert status["option_colors"] == {"Packed": "violet"} and pill(status, "Packed") == "violet"
    model = client.get(f"/w/{ws.id}/app/studio/data.json").get_json()
    shown = next(f for f in next(t for t in model["tables"] if t["key"] == "orders")["fields"] if f["key"] == "status")
    assert shown["colours"]["Packed"] == "violet" and model["colours"][0] == "slate"
    _change(client, ws, {"do": "rename_option", "table": "orders", "key": "status", "old": "Packed", "new": "Boxed"})
    assert schema.field(schema.table(ws.blueprint, "orders"), "status")["option_colors"] == {"Boxed": "violet"}
    _change(client, ws, {"do": "update_field", "table": "orders", "key": "number", "before": "SO-", "after": ""})
    _change(client, ws, {"do": "update_field", "table": "orders", "key": "in_stock", "yes_label": "In stock", "no_label": "Out of stock"})
    _change(client, ws, {"do": "update_field", "table": "orders", "key": "margin", "signs": "normal", "after": " pts"})
    e = Engine(ws)
    orders = schema.table(ws.blueprint, "orders")
    order = e.create("orders", {"number": "1001", "margin": -3, "in_stock": False})
    assert e.display(schema.field(orders, "number"), "1001") == "SO-1001"
    assert e.display(schema.field(orders, "margin"), -3) == "-3 pts"
    assert e.display(schema.field(orders, "in_stock"), False) == "Out of stock"
    assert e.colours(orders, e.get(order.id))["fields"] == {"margin": "red"}
    page = client.get(f"/w/{ws.id}/app/t/orders/{order.id}").get_data(as_text=True)
    assert "SO-1001" in page and "Out of stock" in page
    _change(client, ws, {"do": "update_field", "table": "orders", "key": "margin", "signs": ""})
    assert not schema.table(ws.blueprint, "orders").get("colors")
    assert not schema.check(ws.blueprint)
