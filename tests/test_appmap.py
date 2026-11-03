"""The App map: the whole app drawn from its design as processes (chains of lists that hand work on), the lists they look
things up in, who does each part, where the AI helps and what the checks found, with the data model as its other view;
a process named as the business calls it; an example played through it in a copy."""
from app import appmap
from app.engine.records import Engine
from app.models import Record

from .conftest import login
from .test_wave2 import _built

TRADE = [
    {"op": "create_table", "table": "customers", "label": "Customers", "fields": [
        {"key": "name", "type": "text", "required": True},
        {"key": "status", "type": "select", "options": ["Lead", "Active", "Inactive"]}]},
    {"op": "create_table", "table": "products", "label": "Products", "fields": [{"key": "name", "type": "text", "required": True}]},
    {"op": "create_table", "table": "quotes", "label": "Quotes", "fields": [
        {"key": "title", "type": "text", "required": True}, {"key": "customer", "type": "link", "table": "customers"},
        {"key": "status", "type": "select", "options": ["Draft", "Sent", "Accepted"], "default": "Draft"}],
     "public_form": {"title": "Ask for a quote", "fields": ["title"]},
     "workflow": {"field": "status", "transitions": [{"from": ["Draft"], "to": "Sent", "label": "Send"},
                                                     {"from": ["Sent"], "to": "Accepted", "label": "Customer accepts"}]}},
    {"op": "create_table", "table": "jobs", "label": "Jobs", "fields": [
        {"key": "title", "type": "text", "required": True}, {"key": "quote", "type": "link", "table": "quotes"},
        {"key": "customer", "type": "link", "table": "customers"}, {"key": "notes", "type": "long_text", "ai": {"instructions": "Sum it up"}},
        {"key": "status", "type": "select", "options": ["Booked", "Done", "Signed off"], "default": "Booked"}],
     "workflow": {"field": "status", "transitions": [{"from": ["Booked"], "to": "Done", "label": "Finish"},
                                                     {"from": ["Done"], "to": "Signed off", "label": "Manager approves", "who": ["admin"]}]}},
    {"op": "create_table", "table": "invoices", "label": "Invoices", "fields": [
        {"key": "title", "type": "text", "required": True}, {"key": "job", "type": "link", "table": "jobs"},
        {"key": "customer", "type": "link", "table": "customers"},
        {"key": "status", "type": "select", "options": ["Draft", "Sent", "Paid"], "default": "Draft"}]},
    {"op": "create_table", "table": "payments", "label": "Payments", "fields": [
        {"key": "invoice", "type": "link", "table": "invoices", "required": True}, {"key": "amount", "type": "money"}]},
    {"op": "create_table", "table": "stock_movements", "label": "Stock movements", "fields": [
        {"key": "product", "type": "link", "table": "products"}, {"key": "quantity", "type": "number"}]},
    {"op": "create_table", "table": "restocks", "label": "Restocks", "fields": [
        {"key": "title", "type": "text", "required": True},
        {"key": "status", "type": "select", "options": ["Ordered", "Received", "Shelved"], "default": "Ordered"}],
     "workflow": {"field": "status", "transitions": [{"from": ["Ordered"], "to": "Received", "label": "Receive"},
                                                     {"from": ["Received"], "to": "Shelved", "label": "Shelve"}]}},
    {"op": "add_automation", "automation": {"key": "to_job", "name": "Accepted quotes become jobs", "trigger": {"type": "record_updated", "table": "quotes"},
                                            "conditions": [{"field": "status", "op": "changed_to", "value": "Accepted"}],
                                            "actions": [{"type": "create_record", "table": "jobs", "values": {"title": "{{record.title}}", "quote": "{{record}}"}}]}},
    {"op": "add_automation", "automation": {"key": "to_invoice", "name": "Signed-off jobs are invoiced", "trigger": {"type": "record_updated", "table": "jobs"},
                                            "conditions": [{"field": "status", "op": "changed_to", "value": "Signed off"}],
                                            "actions": [{"type": "create_record", "table": "invoices", "values": {"title": "{{record.title}}", "job": "{{record}}"}},
                                                        {"type": "create_record", "table": "stock_movements", "values": {"quantity": -1}}]}},
    {"op": "add_automation", "automation": {"key": "paid", "name": "A payment marks its invoice paid", "trigger": {"type": "record_created", "table": "payments"},
                                            "actions": [{"type": "update_record", "target": "invoice", "values": {"status": "Paid"}}]}},
    {"op": "add_automation", "automation": {"key": "stock_in", "name": "Received stock is counted in", "trigger": {"type": "record_updated", "table": "restocks"},
                                            "conditions": [{"field": "status", "op": "changed_to", "value": "Received"}],
                                            "actions": [{"type": "create_record", "table": "stock_movements", "values": {"quantity": 5}}]}},
]


def test_the_processes_are_worked_out_from_the_design_in_the_order_work_goes(app, owner):
    """Quotes → jobs → invoices → payments by the rules and links between them; customers (looked up by every one) and
    products are reference; stock movements, which two processes each add to, joins neither into the other."""
    _, ws = owner
    _built(ws, TRADE)
    found, reference = appmap.processes(ws.blueprint)
    assert [(p["name"], p["lists"]) for p in found] == [("Quotes to payments", ["quotes", "jobs", "invoices", "payments"]),
                                                       ("Restocks", ["restocks"])]
    assert reference == ["customers", "products", "stock_movements"]
    sale = found[0]
    assert {(h["from"], h["at"], h["to"], h["how"]) for h in sale["handoffs"]} == {
        ("quotes", "Accepted", "jobs", "makes"), ("jobs", "Signed off", "invoices", "makes"), ("jobs", "Signed off", "stock_movements", "makes"),
        ("payments", None, "invoices", "updates")}
    assert {(h["from"], h["to"]) for h in found[1]["handoffs"]} == {("restocks", "stock_movements")}  # its own, shown on its row


def test_the_map_shows_who_where_the_ai_helps_what_waits_and_what_the_checks_found(app, owner):
    _, ws = owner
    _built(ws, TRADE + [{"op": "add_automation", "automation": {
        "key": "broken", "name": "Chase emailed quotes", "trigger": {"type": "record_updated", "table": "quotes"},
        "conditions": [{"field": "status", "op": "changed_to", "value": "Emailed"}], "actions": [{"type": "notify", "to": "admin", "message": "Chase"}]}}])
    from app.builder import checks
    engine = Engine(ws)
    engine.create("jobs", {"title": "Fix the tap", "status": "Done"})
    drawn = appmap.build(engine, checks.run(engine))
    sale = drawn["processes"][0]
    quotes, jobs = sale["cards"][0], sale["cards"][1]
    assert quotes["entries"] == [{"icon": "globe", "label": "A form on your website"}]  # work comes in from the website
    assert [(s["name"], s["count"], s["approves"]) for s in jobs["stages"]] == [("Booked", 0, False), ("Done", 1, False), ("Signed off", 0, True)]
    assert jobs["people"] == ["An admin"] and jobs["ai"]["fields"] == ["Notes"]
    assert any("Emailed" in x["text"] and x["bad"] for x in quotes["problems"])  # the checks' finding, on the list it's about
    assert [r["label"] for r in drawn["reference"]] == ["Customers", "Products", "Stock movements"]


def test_the_app_map_is_one_page_for_how_work_flows_and_what_each_list_keeps(app, client, owner):
    _, ws = owner
    _built(ws, TRADE)
    login(client)
    page = client.get(f"/w/{ws.id}/app/studio").get_data(as_text=True)
    assert "<h1>" in page and "App map" in page and "How work flows" in page and "What it keeps" in page
    assert 'id="studio-map"' in page and "data-model" in page and "Quotes to payments" in page
    assert client.get(f"/w/{ws.id}/app/studio/workflows?open=rule:to_job").headers["Location"].endswith("/studio?open=rule:to_job")
    assert '"key": "to_job"' in client.get(f"/w/{ws.id}/app/studio?open=rule:to_job").get_data(as_text=True)  # opened in it
    fresh = client.get(f"/w/{ws.id}/app/studio/map.json").get_json()
    assert [p["key"] for p in fresh["map"]["processes"]] == ["quotes", "restocks"] and fresh["data"]["items"]
    named = client.post(f"/w/{ws.id}/app/studio/map/name", json={"process": "quotes", "name": "Quote to cash"}).get_json()
    assert named["map"]["processes"][0]["name"] == "Quote to cash" and named["map"]["processes"][0]["named"]
    again = client.post(f"/w/{ws.id}/app/studio/map/name", json={"process": "quotes", "name": ""}).get_json()
    assert again["map"]["processes"][0]["name"] == "Quotes to payments"  # named after its lists again
    assert client.post(f"/w/{ws.id}/app/studio/map/name", json={"process": "nope", "name": "X"}).status_code == 400


def test_an_example_played_through_a_process_lights_up_where_it_went_and_changes_nothing(app, client, owner):
    _, ws = owner
    _built(ws, TRADE)
    ws.state = {**(ws.state or {}), "examples": [{"title": "A quote becomes a job", "steps": [
        {"do": "add", "table": "quotes", "as": "q", "values": {"title": "New kitchen"}},
        {"do": "move", "record": "q", "to": "Accepted"}], "expect": [{"table": "jobs", "count": 1}]}]}
    from app.extensions import db
    db.session.commit()
    login(client)
    drawn = client.get(f"/w/{ws.id}/app/studio/map.json").get_json()["map"]
    assert drawn["examples"] == [{"n": 0, "title": "A quote becomes a job", "tables": ["quotes"]}]
    played = client.post(f"/w/{ws.id}/app/studio/map/play", json={"example": 0}).get_json()
    assert played["ok"] and played["ran"] == ["Accepted quotes become jobs"]
    assert [(w["table"], w["stage"]) for w in played["went"]] == [("quotes", "Draft"), ("quotes", "Sent"), ("quotes", "Accepted"), ("jobs", "Booked")]
    assert Record.query.filter_by(workspace_id=ws.id).count() == 0  # played in a copy
    assert client.post(f"/w/{ws.id}/app/studio/map/play", json={"example": 5}).status_code == 400
