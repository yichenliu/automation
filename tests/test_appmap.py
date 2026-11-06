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
    assert "<h1>" in page and "App map" in page and "How work flows" in page and "What each list keeps" in page
    assert 'id="studio-map"' in page and "data-model" in page and "Quotes to payments" in page
    assert "“when a job reaches Done, tell me”" in page  # what to ask for, said with this app's own lists
    assert "Looking is safe" in page and "data-editing-bar" in page and 'class="ask-fab" type="button" hidden' in page
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


def test_a_portal_customers_only_see_isnt_where_work_comes_in_and_a_rule_that_keeps_stopping_is_said(app, owner):
    """Jobs customers can see in their portal don't come in from it; a rule that stopped lately is counted on its list."""
    _, ws = owner
    _built(ws, TRADE + [{"op": "update_table", "table": "jobs", "portal": {"via": "customer"}},
                        {"op": "update_table", "table": "quotes", "portal": {"via": "customer", "can_add": True}}])
    from app.extensions import db
    from app.models import Run
    db.session.add(Run(workspace_id=ws.id, automation="to_invoice", name="Signed-off jobs are invoiced", status="failed"))
    db.session.commit()
    drawn = appmap.build(Engine(ws))
    quotes, jobs = drawn["processes"][0]["cards"][:2]
    assert [e["label"] for e in quotes["entries"]] == ["A form on your website", "Your customers' portal"]
    assert jobs["entries"] == [] and jobs["seen_in_portal"]
    assert [(r["key"], r["stopped"]) for r in jobs["rules"]] == [("to_invoice", 1)]
    assert [(m["label"], m["to"]) for m in jobs["stages"][1]["moves"]] == [("Manager approves", "Signed off")]
    assert jobs["stages"][1]["moves"][0]["who"] == ["An admin"] and jobs["field_label"] == "Status"
    paid = next(h for h in drawn["processes"][0]["handoffs"] if h["rule"] == "paid")
    assert paid["sets"] == [["Status", "Paid"]] and paid["event"] == "record_created"  # said: its invoice's Status is set to Paid


def test_stages_are_changed_where_they_are_even_a_plain_status_and_one_put_between_leads_on(app, client, owner):
    """A stage added between two gets a way on to the next; a list whose Status is a plain choice has its stages
    changed as its choices, never a second Status made beside it."""
    _, ws = owner
    _built(ws, TRADE)
    login(client)
    url = f"/w/{ws.id}/app/studio/workflows/change"
    done = client.post(url, json={"id": "stages:jobs", "change": {"do": "add_stage", "name": "Waiting on parts", "after": "Booked"}}).get_json()
    assert done["ok"]
    from app.extensions import db
    db.session.refresh(ws)
    jobs = next(t for t in ws.blueprint["tables"] if t["key"] == "jobs")
    assert next(f for f in jobs["fields"] if f["key"] == "status")["options"] == ["Booked", "Waiting on parts", "Done", "Signed off"]
    moves = {(tuple(m["from"]), m["to"]) for m in jobs["workflow"]["transitions"]}
    assert (("Booked",), "Waiting on parts") in moves  # reached from the stage before it,
    assert any("Waiting on parts" in frm and to == "Done" for frm, to in moves)  # and on from it to the next
    plain = {"id": "stages:invoices", "change": {"field": "status", "table": "invoices"}}
    assert client.post(url, json={**plain, "change": {**plain["change"], "do": "add_stage", "name": "Chased", "after": "Sent"}}).get_json()["ok"]
    assert client.post(url, json={**plain, "change": {**plain["change"], "do": "rename_stage", "old": "Draft", "new": "Being written"}}).get_json()["ok"]
    assert client.post(url, json={**plain, "change": {**plain["change"], "do": "order_stages", "names": ["Being written", "Sent", "Paid", "Chased"]}}).get_json()["ok"]
    db.session.refresh(ws)
    invoices = next(t for t in ws.blueprint["tables"] if t["key"] == "invoices")
    assert not invoices.get("workflow") and [f["key"] for f in invoices["fields"]].count("status") == 1
    assert next(f for f in invoices["fields"] if f["key"] == "status")["options"] == ["Being written", "Sent", "Paid", "Chased"]
    assert client.post(url, json={**plain, "change": {**plain["change"], "do": "add_move", "to": "Paid"}}).status_code == 400  # no moves: anyone picks any


def test_the_same_message_twice_in_a_row_is_sent_once(app, owner):
    from app.blueprint import schema
    tell = {"type": "notify", "to": "everyone", "message": "Quote accepted: job made"}
    a = schema.normalize_automation({"key": "x", "name": "X", "trigger": {"type": "record_created", "table": "quotes"},
                                     "actions": [tell, dict(tell), {"type": "notify", "to": "admin", "message": "Quote accepted: job made"}, dict(tell)]})
    assert [s["to"] for s in a["actions"]] == ["everyone", "admin", "everyone"]  # only the repeat straight after itself goes


def test_a_hand_off_says_what_else_must_be_true_and_where_its_own_record_goes_on_to(app, client, owner):
    """“When a job reaches Signed off, an invoice is made and the job moves on to Invoiced”; “a payment …, if its balance
    is nothing”; a rule's steps named for what they make and change; email offered only when it can be sent."""
    _, ws = owner
    design = [dict(op) for op in TRADE]
    design[-3] = {"op": "add_automation", "automation": {**TRADE[-3]["automation"], "actions": TRADE[-3]["automation"]["actions"] + [
        {"type": "update_record", "values": {"status": "Done"}}]}}
    design[-2] = {"op": "add_automation", "automation": {**TRADE[-2]["automation"], "conditions": [{"field": "amount", "op": "greater_than", "value": 0}]}}
    _built(ws, design)
    drawn = appmap.build(Engine(ws))
    hands = {h["rule"]: h for h in drawn["processes"][0]["handoffs"]}
    assert hands["to_invoice"]["moves_to"] == "Done" and hands["to_job"]["moves_to"] is None
    assert hands["paid"]["only_if"] and "Amount" in hands["paid"]["only_if"][0]
    from app import studio
    rule = next(a for a in ws.blueprint["automations"] if a["key"] == "to_invoice")
    kinds = [n["kind"] for n in studio.rule_diagram(Engine(ws), rule, [])["steps"]]
    assert kinds == ["Add an invoice", "Add a stock movement", "Change the job"]
    login(client)
    page = client.get(f"/w/{ws.id}/app/studio").get_data(as_text=True)
    assert 'data-email-on=""' in page and 'data-me="' in page  # not connected: “email me” shown, but not offered


def test_a_message_sent_twice_in_a_row_from_before_is_sent_once_after_any_change(app, owner):
    from app.blueprint import ops
    _, ws = owner
    _built(ws, TRADE)
    bp = dict(ws.blueprint)
    rule = next(a for a in bp["automations"] if a["key"] == "to_job")
    tell = {"type": "notify", "to": "everyone", "message": "Job made"}
    rule["actions"] = rule["actions"] + [tell, dict(tell)]  # saved like this by an older version
    done = ops.apply(bp, [{"op": "update_table", "table": "customers", "label": "Clients"}])
    assert [x["type"] for x in next(a for a in done.blueprint["automations"] if a["key"] == "to_job")["actions"]] == ["create_record", "notify"]


def test_versions_undo_a_change_back_to_just_before_it_and_count_stages_as_the_map_does(app, client, owner):
    _, ws = owner
    _built(ws, TRADE)
    login(client)
    before = ws.version
    assert client.post(f"/w/{ws.id}/app/studio/data/change", json={"do": "add_field", "table": "payments", "label": "Paid by card", "type": "checkbox"}).get_json()["ok"]
    page = client.get(f"/w/{ws.id}/app/design").get_data(as_text=True)
    assert f'action="/w/{ws.id}/app/design/restore/{before}"' in page and "Undo this</button>" in page  # this change, undone: as it was before it
    assert "Undo “Added the column “Paid by card”" in page and "changed on the App map" in page
    assert "5 that move along stages" in page  # quotes, jobs, restocks with stages of their own; invoices and customers a plain Status
    client.post(f"/w/{ws.id}/app/design/restore/{before}")
    from app.extensions import db
    db.session.refresh(ws)
    assert not any(f["key"] == "paid_by_card" for t in ws.blueprint["tables"] if t["key"] == "payments" for f in t["fields"])


def test_a_stage_a_rule_uses_cant_be_removed_unseen_and_a_rule_made_twice_is_made_once(app, client, owner):
    """Removing Signed off would quietly stop invoicing: refused, the rule named. The same rule sent twice (a double
    click) is made once. A rule's start changed to a stage waits for that stage."""
    _, ws = owner
    _built(ws, TRADE)
    login(client)
    url = f"/w/{ws.id}/app/studio/workflows/change"
    gone = client.post(url, json={"id": "stages:jobs", "change": {"do": "remove_stage", "name": "Signed off"}})
    assert gone.status_code == 400 and "Signed-off jobs are invoiced" in gone.get_json()["error"]
    plain = client.post(url, json={"id": "stages:invoices", "change": {"do": "remove_stage", "field": "status", "name": "Paid"}})  # a plain Status
    assert plain.status_code == 400 and "A payment marks its invoice paid" in plain.get_json()["error"]
    choice = client.post(f"/w/{ws.id}/app/studio/data/change", json={"do": "update_field", "table": "invoices", "key": "status", "options": ["Draft", "Sent"]})
    assert choice.status_code == 400 and "A payment marks its invoice paid" in choice.get_json()["error"]
    rule = {"id": "", "change": {"do": "add_rule", "name": "Tell me", "trigger": {"type": "record_updated", "table": "quotes", "field": "status", "to": "Accepted"},
                                 "step": {"type": "notify", "to": "owner", "message": "{{record}} is now Accepted"}}}
    made = client.post(url, json=rule).get_json()
    assert made["ok"] and client.post(url, json=rule).status_code == 400  # the second press
    key = made["id"].split(":")[1]
    moved = client.post(url, json={"id": made["id"], "change": {"do": "set_trigger", "trigger": {"type": "record_updated", "table": "quotes", "field": "status", "to": "Sent"}}})
    assert moved.get_json()["ok"]
    from app.extensions import db
    db.session.refresh(ws)
    a = next(x for x in ws.blueprint["automations"] if x["key"] == key)
    assert [c for c in a["conditions"] if c["op"] == "changed_to"] == [{"field": "status", "op": "changed_to", "value": "Sent"}]


def test_undoing_an_older_change_from_the_builder_names_what_goes_with_it_and_never_fails(app, client, owner):
    from app.extensions import db
    from app.models import Message
    _, ws = owner
    _built(ws, TRADE)
    login(client)
    first = ws.version
    url = f"/w/{ws.id}/app/studio/data/change"
    client.post(url, json={"do": "add_field", "table": "jobs", "label": "Gate code", "type": "text"})
    client.post(url, json={"do": "add_field", "table": "jobs", "label": "Site contact", "type": "text"})
    db.session.refresh(ws)
    m = Message(workspace_id=ws.id, channel="builder", role="assistant", status="done", text="Added Gate code",
                changes=["Added Gate code to Jobs"], version_before=first, version_after=first + 1)
    db.session.add(m)
    db.session.commit()
    asked = client.post(f"/w/{ws.id}/chat/{m.id}/undo", json={})
    assert asked.status_code == 409 and "Added the column “Site contact” to Jobs" in asked.get_json()["confirm"]  # what goes with it, said first
    done = client.post(f"/w/{ws.id}/chat/{m.id}/undo", json={"all": True})
    assert done.status_code == 200 and "Undid “Added Gate code to Jobs”" in done.get_json()["html"]
    gone = Message(workspace_id=ws.id, channel="builder", role="assistant", status="done", changes=["x"], version_before=999, version_after=1000)
    db.session.add(gone)
    db.session.commit()
    assert client.post(f"/w/{ws.id}/chat/{gone.id}/undo", json={"all": True}).status_code in (400, 409)  # never a server error


def test_change_notes_say_what_changed_in_words(app, owner):
    from app.blueprint import ops
    _, ws = owner
    _built(ws, TRADE)
    done = ops.apply(ws.blueprint, [{"op": "update_field", "table": "jobs", "key": "status", "changes": {"options": ["Booked", "Done", "Waiting on parts", "Signed off"]}}])
    assert any("added “Waiting on parts”" in c for c in done.changes) and not any("its choices" in c for c in done.changes)
    mail = ops.apply(ws.blueprint, [{"op": "add_automation", "automation": {"key": "m", "name": "Mail", "trigger": {"type": "record_created", "table": "jobs"},
                                                                            "actions": [{"type": "send_email", "to": "{{record.customer.name}}", "subject": "Hi", "body": "Hi"}]}}])
    assert any("emails its customer's name" in c for c in mail.changes) and not any("{{" in c for c in mail.changes)


def test_a_rule_made_on_the_map_keeps_its_stage_and_a_long_named_one_pressed_twice_is_said_once(app, client, owner):
    """“When a vaccination reaches Overdue, tell the owner”: the stage it waits for is its condition (never left behind on the
    way into the Keel language), so the stage can't be taken away unseen; and its long name's second press is refused,
    never quietly written over the first (keys are cut at 48 letters, and _2 must fit inside them)."""
    from app.extensions import db
    _, ws = owner
    _built(ws, TRADE)
    login(client)
    url = f"/w/{ws.id}/app/studio/workflows/change"
    rule = {"id": "", "change": {"do": "add_rule", "name": "When an invoice reaches Paid, tell the owner and the bookkeeper straight away",
                                 "trigger": {"type": "record_updated", "table": "invoices", "field": "status", "to": "Paid"},
                                 "step": {"type": "notify", "to": "owner", "message": "{{record}} is paid"}}}
    first = client.post(url, json=rule)
    assert first.status_code == 200, first.get_json()
    again = client.post(url, json=rule)
    assert again.status_code == 400 and "already" in again.get_json()["error"]
    db.session.refresh(ws)
    made = [a for a in ws.blueprint["automations"] if a["name"] == rule["change"]["name"]]
    assert len(made) == 1 and {"field": "status", "op": "changed_to", "value": "Paid"} in made[0]["conditions"]


def test_a_stage_put_after_another_on_the_map_keeps_what_a_feature_adds_to_the_moves(app, client, owner):
    """Quotes wait for the owner's OK (a feature on the moves into Sent): “On hold” after Draft is put in its
    place, reached and leading on, and the approval stays."""
    from app import lang
    from app.extensions import db
    _, ws = owner
    _built(ws, TRADE)
    out = lang.apply(ws.blueprint, [{"do": "approval", "list": "quotes", "stage": "Sent", "by": "owner"}])
    assert not out.problems, out.problems
    ws.blueprint = out.blueprint
    db.session.commit()
    login(client)
    done = client.post(f"/w/{ws.id}/app/studio/workflows/change", json={"id": "stages:quotes", "change": {
        "do": "add_stage", "table": "quotes", "field": "status", "name": "On hold", "after": "Draft"}})
    assert done.status_code == 200, done.get_json()
    db.session.refresh(ws)
    quotes = next(t for t in ws.blueprint["tables"] if t["key"] == "quotes")
    names = next(f for f in quotes["fields"] if f["key"] == "status")["options"]
    assert names.index("On hold") == names.index("Draft") + 1
    moves = quotes["workflow"]["transitions"]
    assert any(mv["to"] == "On hold" for mv in moves) and any("On hold" in (mv.get("from") or []) for mv in moves)
    assert any(e["do"] == "approval" for e in ws.blueprint["features"]) and any(mv.get("who") for mv in moves if mv["to"] == "Sent")
