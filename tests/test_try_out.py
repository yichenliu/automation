"""The builder tries an app out before handing it over: mistakes found by reading the design (a stage a record gets
stuck at, a choice that isn't one, a stage nothing reaches), a copy of the app walked through, worked examples acted out
in it, and what's wrong sent back to the AI to fix, with what was checked shown to the owner and kept for training."""
import json

from app import ai
from app.blueprint import expr, ops, schema
from app.builder import chat, checks, trial
from app.engine.records import Engine
from app.extensions import db
from app.models import Message, Record, Run

from .conftest import ScriptedAI

QUOTES = [
    {"op": "create_table", "table": "quotes", "label": "Quotes", "fields": [
        {"key": "title", "type": "text", "required": True}, {"key": "discount_percent", "label": "Discount %", "type": "percent"},
        {"key": "subtotal", "type": "rollup", "aggregate": "sum", "table": "quote_lines", "field": "amount", "via": "quote"},
        {"key": "discount", "type": "formula", "format": "money", "expression": "round(subtotal * discount_percent / 100, 2)"},
        {"key": "gst", "label": "GST", "type": "formula", "format": "money", "expression": "round((subtotal - discount) * 0.1, 2)"},
        {"key": "total", "type": "formula", "format": "money", "expression": "subtotal - discount + gst"},
        {"key": "status", "type": "select", "options": ["Draft", "Awaiting approval", "Sent", "Declined"]}],
     "workflow": {"field": "status", "transitions": [
         {"from": ["Draft"], "to": "Awaiting approval", "label": "Send for approval",
          "only_if": [{"field": "discount_percent", "op": "greater_than", "value": 10}]},
         {"from": ["Draft"], "to": "Sent", "label": "Send", "only_if": [{"field": "discount_percent", "op": "less_than", "value": 10}]},
         {"from": ["Awaiting approval"], "to": "Sent", "label": "Approve & send", "who": ["owner"]}]}},
    {"op": "create_table", "table": "quote_lines", "label": "Quote lines", "embedded": True, "fields": [
        {"key": "quote", "type": "link", "table": "quotes", "required": True}, {"key": "item", "type": "text"},
        {"key": "quantity", "type": "number"}, {"key": "unit_price", "type": "money"},
        {"key": "amount", "type": "formula", "format": "money", "expression": "quantity * unit_price"}]},
]
CHASE = {"op": "add_automation", "automation": {
    "key": "chase", "name": "Chase emailed quotes", "trigger": {"type": "record_updated", "table": "quotes"},
    "conditions": [{"field": "status", "op": "changed_to", "value": "Emailed"}],
    "actions": [{"type": "notify", "to": "everyone", "message": "Chase {{record.title}}"}]}}


def _built(ws, design):
    result = ops.apply(ws.blueprint, design)
    assert not result.problems, result.problems
    chat.save_version(ws, result.blueprint, result.changes, "chat")


def _issues(ws):
    return [i["text"] for i in checks.run(Engine(ws))]


HOLD = {"op": "add_automation", "automation": {
    "key": "hold", "name": "Hold failed checks", "trigger": {"type": "record_updated", "table": "quotes"},
    "conditions": [{"field": "status", "op": "equals", "value": "draft"}, {"field": "status", "op": "equals", "value": "Sent"}],
    "actions": [{"type": "notify", "to": "everyone", "message": "Held"}]}}


def test_mistakes_with_one_sensible_reading_are_put_right_as_the_design_comes_in(app, owner):
    _, ws = owner
    result = ops.apply(ws.blueprint, QUOTES + [CHASE, HOLD])
    assert not result.problems, result.problems
    quotes = result.blueprint["tables"][0]
    moves = {m.get("label"): m for m in quotes["workflow"]["transitions"]}
    assert moves["Send"]["only_if"][0]["op"] == "at_most" and quotes["fields"][1]["default"] == 0  # exactly 10%, or none, can move on
    assert moves["Mark as declined"]["from"] == ["Sent"] and moves["Mark as declined"]["to"] == "Declined"
    hold = result.blueprint["automations"][1]["conditions"]
    assert hold == [{"any": [{"field": "status", "op": "equals", "value": "Draft"}, {"field": "status", "op": "equals", "value": "Sent"}]}]
    assert any("can always move on" in c for c in result.changes) and any("“Mark as declined”" in c for c in result.changes)
    chat.save_version(ws, result.blueprint, result.changes, "chat")
    assert [i["text"] for i in checks.run(Engine(ws))] == [
        "the rule “Chase emailed quotes” checks status changed_to “Emailed”, but quotes.status has no such choice (its "
        "choices: Draft, Awaiting approval, Sent, Declined): use one of them, add it to the field's options, or, if it means a "
        "linked record's field, write its path (as order.status)"]


def test_reading_the_design_finds_a_stuck_stage_a_choice_that_isnt_one_and_a_stage_nothing_reaches(app, owner):
    """What slips past the corrections (or a design saved before them) is found by the try-out."""
    _, ws = owner
    result = ops.apply(ws.blueprint, QUOTES + [CHASE, HOLD])
    broken = json.loads(json.dumps(result.blueprint))
    quotes = broken["tables"][0]
    quotes["fields"][1].pop("default")
    quotes["workflow"]["transitions"] = [m for m in quotes["workflow"]["transitions"] if m["to"] != "Declined"]
    quotes["workflow"]["transitions"][1]["only_if"][0]["op"] = "less_than"
    broken["automations"][1]["conditions"] = HOLD["automation"]["conditions"]
    chat.save_version(ws, broken, ["broken by hand"], "chat")
    found = _issues(ws)
    assert any("at “Draft” with discount_percent" in t and "has no move it can take" in t for t in found), found  # exactly 10%, or none
    assert any("“Emailed”" in t and "no such choice" in t for t in found)
    assert any("to the stage “Declined”" in t for t in found)
    assert any("needs status to equal both “draft” and “Sent”" in t for t in found)


def test_walking_a_copy_finds_a_rule_that_stops_and_leaves_the_real_app_alone(app, owner):
    _, ws = owner
    _built(ws, QUOTES + [{"op": "add_automation", "automation": {
        "key": "stamp", "name": "Number new quotes", "trigger": {"type": "record_created", "table": "quotes"},
        "actions": [{"type": "update_record", "values": {"discount_percent": "{{record.title}}"}}]}}])
    Engine(ws, by="Ethan").create("quotes", {"title": "Real quote", "discount_percent": 5}, automations=False)
    before = (Record.query.count(), Run.query.count())
    walked, _ = trial.try_out(ws)
    stopped = [i for i in walked["issues"] if "Number new quotes" in i["text"]]
    assert stopped and stopped[0]["level"] == "fix", walked["issues"]
    assert not walked["crashes"], walked["crashes"]
    assert (Record.query.count(), Run.query.count()) == before  # the copy was thrown away; nothing reached the real app


EXAMPLES = [
    {"title": "A wedding quote with 15% off", "steps": [
        {"do": "add", "table": "quotes", "as": "q", "values": {"title": "Sarah's wedding", "discount_percent": 15}},
        {"do": "add", "table": "quote_lines", "values": {"quote": "q", "item": "Platter", "quantity": 80, "unit_price": 25}},
        {"do": "add", "table": "quote_lines", "values": {"quote": "q", "item": "Lamb", "quantity": 80, "unit_price": 38}}],
     "expect": [{"record": "q", "field": "subtotal", "equals": 5040}, {"record": "q", "field": "discount", "equals": 756},
                {"record": "q", "field": "gst", "equals": 428.4}, {"record": "q", "field": "total", "equals": "$4,712.40"}]},
    {"title": "Big discounts wait for an OK", "steps": [
        {"do": "add", "table": "quotes", "as": "q", "values": {"title": "Gala", "discount_percent": 20}},
        {"do": "move", "record": "q", "to": "Sent", "refused": True},
        {"do": "move", "record": "q", "to": "Awaiting approval"}, {"do": "move", "record": "q", "to": "Sent"}],
     "expect": [{"record": "q", "field": "status", "equals": "Sent"}, {"table": "quotes", "count": 1}]},
    {"title": "A sum expected wrong", "steps": [{"do": "add", "table": "quotes", "as": "q", "values": {"title": "Lunch", "discount_percent": 10}},
                                                {"do": "add", "table": "quote_lines", "values": {"quote": "q", "quantity": 10, "unit_price": 20}}],
     "expect": [{"record": "q", "field": "discount", "equals": 2}]},
    {"title": "Names a field that isn't there", "steps": [{"do": "add", "table": "quotes", "as": "q", "values": {"title": "X", "colour": "red"}}],
     "expect": [{"record": "q", "field": "title", "equals": "X"}]},
]


def test_worked_examples_are_acted_out_and_compared(app, owner):
    _, ws = owner
    _built(ws, QUOTES)
    _, (wedding, big, wrong, unknown) = trial.try_out(ws, EXAMPLES, screens=False)
    assert wedding["ok"], wedding
    assert [line for _, line in wedding["lines"]][1] == "Quote’s Discount: $756.00"
    assert big["ok"], big
    assert not wrong["ok"] and wrong["kind"] == "app" and wrong["plain"] == "Quote’s Discount came out $20.00, expected $2.00"
    assert unknown["kind"] == "example" and "no field “colour”" in unknown["problem"]


FIXED = {"op": "update_field", "table": "quotes", "key": "discount", "changes": {"expression": "round(subtotal * discount_percent, 2)"}}






def test_someone_says_something_isnt_right_and_the_owner_has_the_builder_fix_it(app, owner, client):
    from app.models import Feedback, Notification
    from .conftest import login
    _, ws = owner
    _built(ws, QUOTES)
    login(client)
    page = client.get(f"/w/{ws.id}/app/t/quotes").get_data(as_text=True)
    assert "Something not right here?" in page
    client.post(f"/w/{ws.id}/app/feedback", data={"text": "I pressed Send but the quote stayed as Draft", "page": f"/w/{ws.id}/app/t/quotes"})
    said = Feedback.query.one()
    assert said.text.startswith("I pressed Send") and said.version == ws.version and said.status == "open"
    assert Notification.query.filter(Notification.text.contains("isn't right")).count() == 1
    assert "I pressed Send but the quote stayed as Draft" in client.get(f"/w/{ws.id}/app/improvements").get_data(as_text=True)
    client.post(f"/w/{ws.id}/app/feedback/{said.id}", data={"decision": "fix"})
    asked = Message.query.filter_by(role="user").one()
    assert "said this isn't right" in asked.text and "stayed as Draft" in asked.text and said.status == "asked" and said.message_id


def test_approvals_written_inside_a_no_are_put_in_turn(app, owner):
    _, ws = owner
    rejected = lambda: [{"type": "update_record", "values": {"status": "Declined"}}]  # noqa: E731
    result = ops.apply(ws.blueprint, QUOTES + [{"op": "add_automation", "automation": {
        "key": "ok", "name": "Approvals by amount", "trigger": {"type": "record_updated", "table": "quotes"},
        "conditions": [{"field": "status", "op": "changed_to", "value": "Awaiting approval"}],
        "actions": [{"type": "request_approval", "approvers": ["owner"], "message": "Lead OK?", "if_rejected": rejected() + [
            {"type": "request_approval", "approvers": ["admin"], "message": "Finance OK?", "if_rejected": rejected()}]},
            {"type": "update_record", "values": {"status": "Sent"}}]}}])
    assert not result.problems, result.problems
    steps = result.blueprint["automations"][0]["actions"]
    assert [(x["type"], x.get("message")) for x in steps] == [("request_approval", "Lead OK?"), ("request_approval", "Finance OK?"),
                                                              ("update_record", None)]
    assert steps[0]["if_rejected"] == rejected() and any("one after another" in c for c in result.changes)


def test_an_example_naming_records_it_never_named_is_understood(app, owner):
    """The local model often skips "as" and then uses names it meant to give ("q_wed"; "ql_wed" for the quote's line)."""
    _, ws = owner
    _built(ws, QUOTES)
    example = {"title": "Names never given", "steps": [
        {"do": "add", "table": "quotes", "values": {"title": "Wedding", "discount_percent": 0}},
        {"do": "add", "table": "quote_lines", "values": {"quote": "q_wed", "quantity": 2, "unit_price": 100}}],
        "expect": [{"record": "q_wed", "field": "subtotal", "equals": 200}, {"record": "ql_wed", "field": "amount", "equals": 200}]}
    _, (played,) = trial.try_out(ws, [example], screens=False)
    assert played["ok"], played


def test_an_example_can_let_days_pass(app, owner):
    """Reminders and chasing need time: "wait" moves the copy's dates back, as if the days had gone by."""
    _, ws = owner
    _built(ws, [{"op": "create_table", "table": "invoices", "label": "Invoices", "fields": [
        {"key": "number", "type": "text"}, {"key": "due", "type": "date"},
        {"key": "status", "type": "select", "options": ["Sent", "Overdue", "Paid"]}]},
        {"op": "add_automation", "automation": {"key": "chase", "name": "Mark overdue invoices", "trigger": {
            "type": "schedule", "every": "day", "at": "08:00", "table": "invoices"},
            "conditions": [{"field": "status", "op": "equals", "value": "Sent"}, {"field": "due", "op": "before_today"}],
            "actions": [{"type": "update_record", "values": {"status": "Overdue"}}]}}])
    today = Engine(ws).today.isoformat()
    example = {"title": "An unpaid invoice goes overdue", "steps": [
        {"do": "add", "table": "invoices", "as": "i1", "values": {"number": "INV-1", "due": today, "status": "Sent"}},
        {"do": "run", "as": "", "rule": "chase"}, {"do": "wait", "as": "", "days": 3}, {"do": "run", "as": "", "rule": "chase"}],
        "expect": [{"record": "i1", "field": "status", "equals": "Overdue"}]}
    _, (played,) = trial.try_out(ws, [example], screens=False)
    assert played["ok"], played


def test_every_app_keeps_its_checks_and_runs_them_after_any_change(app, owner, client):
    """An undo, a suggestion applied or an edit by hand is checked too; what stops working is said; owners add their own."""
    from app import jobs
    from app.builder import checking
    from app.models import Job, Notification
    from .conftest import login
    _, ws = owner
    _built(ws, QUOTES)
    ws.state = {**(ws.state or {}), "examples": [dict(EXAMPLES[0], by="builder")]}
    db.session.commit()
    assert checking.check(ws, "a test") == [] and ws.state["checks"]["examples"][0]["ok"]
    broken = json.loads(json.dumps(ws.blueprint))
    broken["tables"][0]["fields"][3]["expression"] = "round(subtotal * discount_percent / 1000, 2)"
    chat.save_version(ws, broken, ["Changed Discount by hand"], "manual")
    job = Job.query.filter_by(kind="check").one()  # any change that isn't the builder's own is checked after
    jobs.run(job)
    assert job.result["broke"] == ["A wedding quote with 15% off"]
    told = Notification.query.filter(Notification.text.contains("don’t any more") | Notification.text.contains("doesn’t any more")).one()
    assert "the change “Changed Discount by hand”" in told.text and told.link == f"/w/{ws.id}/app/checks"
    login(client)
    page = client.get(f"/w/{ws.id}/app/checks").get_data(as_text=True)
    assert "1 thing not right yet" in page and "Quote’s Discount came out $0.76, expected $756.00" in page
    client.post(f"/w/{ws.id}/app/checks", data={"do": "add", "said": "A quote of $200 with no discount comes to $220 with GST"})
    added = Job.query.filter_by(kind="check_add").one()
    ai.set_backend(ScriptedAI({"examples": [{"title": "x", "steps": [
        {"do": "add", "as": "q", "table": "quotes", "values": {"title": "Small", "discount_percent": 0}},
        {"do": "add", "as": "", "table": "quote_lines", "values": {"quote": "q", "quantity": 2, "unit_price": 100}}],
        "expect": [{"record": "q", "field": "total", "equals": 220}]}]}))
    jobs.run(added)
    assert added.result == {"ok": True}
    titles = [x["title"] for x in ws.state["examples"]]
    assert titles[0] == "A quote of $200 with no discount comes to $220 with GST" and ws.state["examples"][0]["by"] == "Ethan"
    client.post(f"/w/{ws.id}/app/checks", data={"do": "remove", "title": titles[0]})
    assert [x["title"] for x in ws.state["examples"]] == ["A wedding quote with 15% off"]


def test_apps_are_checked_again_after_the_platform_is_updated(app, owner, monkeypatch):
    from app.builder import checking
    from app.models import Job
    _, ws = owner
    _built(ws, QUOTES)
    ws.state = {**(ws.state or {}), "examples": [EXAMPLES[0]]}
    db.session.commit()
    checking.check(ws, "a test")
    checking.after_platform_update()
    assert Job.query.filter_by(kind="check").count() == 0  # checked on this platform already
    monkeypatch.setitem(checking._PLATFORM, "v", "newer")
    checking.after_platform_update()
    assert Job.query.filter_by(kind="check").one().payload == {"why": "the platform was updated"}


def test_stages_nothing_leads_to_are_chained_in_order_and_a_step_finds_the_record_its_value_belongs_to(app, owner):
    _, ws = owner
    result = ops.apply(ws.blueprint, [
        {"op": "create_table", "table": "bids", "label": "Bids", "fields": [
            {"key": "title", "type": "text"},
            {"key": "status", "type": "select", "options": ["Draft", "Submitted", "Scored", "Recommended", "Awarded", "Not awarded"]}],
         "workflow": {"field": "status", "transitions": [{"from": ["Draft"], "to": "Submitted", "label": "Submit"}]}},
        {"op": "create_table", "table": "invoices", "label": "Invoices", "fields": [
            {"key": "number", "type": "text"}, {"key": "status", "type": "select", "options": ["Sent", "Paid"]}]},
        {"op": "create_table", "table": "payments", "label": "Payments", "fields": [
            {"key": "invoice", "type": "link", "table": "invoices"}, {"key": "status", "type": "select", "options": ["Unmatched", "Matched"]}]},
        {"op": "add_automation", "automation": {"key": "paid", "name": "Mark invoices paid", "trigger": {"type": "record_created", "table": "payments"},
                                                "actions": [{"type": "update_record", "values": {"status": "Paid"}}]}}])
    assert not result.problems, result.problems
    moves = [(m["from"], m["to"]) for m in result.blueprint["tables"][0]["workflow"]["transitions"]]
    assert moves == [(["Draft"], "Submitted"), (["Submitted"], "Scored"), (["Scored"], "Recommended"), (["Recommended"], "Awarded"),
                     (["Recommended"], "Not awarded")]
    assert result.blueprint["automations"][0]["actions"][0]["target"] == "invoice"  # Paid is the invoice's, not the payment's


def test_an_example_naming_the_record_a_lookup_shows_is_compared_by_that_record(app, owner):
    """A treatment note's patient, looked up through its appointment, expected as the example's own patient p1."""
    _, ws = owner
    _built(ws, [
        {"op": "create_table", "table": "patients", "label": "Patients", "fields": [{"key": "name", "type": "text", "required": True}]},
        {"op": "create_table", "table": "appointments", "label": "Appointments", "fields": [
            {"key": "patient", "type": "link", "table": "patients", "required": True}, {"key": "when", "type": "date"}]},
        {"op": "create_table", "table": "notes", "label": "Treatment notes", "fields": [
            {"key": "appointment", "type": "link", "table": "appointments", "required": True},
            {"key": "patient", "type": "lookup", "link": "appointment", "field": "patient"}, {"key": "text", "type": "text"}]}])
    example = {"title": "A note shows its patient", "steps": [
        {"do": "add", "table": "patients", "as": "p1", "values": {"name": "Casey"}},
        {"do": "add", "table": "patients", "as": "p2", "values": {"name": "Robin"}},
        {"do": "add", "table": "appointments", "as": "a1", "values": {"patient": "p1", "when": "2026-11-10"}},
        {"do": "add", "table": "notes", "as": "n1", "values": {"appointment": "a1", "text": "Knee"}}],
        "expect": [{"record": "n1", "field": "patient", "equals": "p1"}]}
    wrong = {**example, "title": "Expected the other patient", "expect": [{"record": "n1", "field": "patient", "equals": "p2"}]}
    _, (right, other) = trial.try_out(ws, [example, wrong], screens=False)
    assert right["ok"], right
    assert not other["ok"] and other["plain"] == "Treatment note’s Patient came out Casey, expected Robin"  # by name, not the example’s “p2”


def test_an_example_refused_by_a_rule_tells_the_builder_what_the_rule_saw(app, owner):
    """“Capital items need an asset tag” written as is_empty(asset_tag): a capital item with its tag is refused, and the builder
    hears the check and the values, so it can tell a rule written backwards from an example that left something out."""
    _, ws = owner
    _built(ws, [{"op": "create_table", "table": "requests", "label": "Requests", "fields": [
        {"key": "title", "type": "text", "required": True}, {"key": "is_capital", "type": "checkbox"},
        {"key": "asset_tag", "type": "text"}],
        "rules": [{"check": "is_empty(asset_tag)", "message": "Capital items need an asset tag",
                   "when": [{"field": "is_capital", "op": "equals", "value": True}]}]}])
    example = {"title": "A capital item with its tag", "steps": [
        {"do": "add", "table": "requests", "as": "r1", "values": {"title": "Lathe", "is_capital": True, "asset_tag": "AT-1"}}],
        "expect": [{"record": "r1", "field": "asset_tag", "equals": "AT-1"}]}
    _, (refused,) = trial.try_out(ws, [example], screens=False)
    assert not refused["ok"] and refused["kind"] == "app"
    assert "checks `is_empty(asset_tag)`" in refused["problem"] and 'asset_tag = "AT-1"' in refused["problem"]
    assert "written backwards" in refused["problem"]
    assert refused["plain"] == "Adding a request wasn't allowed: Capital items need an asset tag"  # people see the rule's own words


def test_an_example_expecting_what_the_ai_drafts_hears_when_its_waiting_to_be_checked(app, owner):
    """A reply the AI drafts and a person always checks stays empty until it's checked: the example is told so, and an
    “approve” step (the person keeping the draft) gets it through."""
    _, ws = owner
    _built(ws, [{"op": "create_table", "table": "complaints", "label": "Complaints", "fields": [
        {"key": "title", "type": "text", "required": True}, {"key": "draft_reply", "type": "long_text"}]},
        {"op": "add_automation", "automation": {
            "key": "draft", "name": "Draft a reply", "trigger": {"type": "record_created", "table": "complaints"},
            "actions": [{"type": "ai_fill", "field": "draft_reply", "instructions": "Draft a reply", "review": "always",
                         "reviewer": "admin"}]}}])
    steps = [{"do": "add", "table": "complaints", "as": "c1", "values": {"title": "Broken mug"}}]
    expect = [{"record": "c1", "field": "draft_reply", "equals": "non-empty"}]
    waiting = {"title": "A reply is drafted", "steps": steps, "expect": expect}
    checked = {"title": "A reply is drafted and kept", "steps": steps + [{"do": "approve", "record": "c1"}], "expect": expect}
    _, (held, kept) = trial.try_out(ws, [waiting, checked], screens=False)
    assert not held["ok"] and "waiting for someone to check it" in held["problem"] and "“approve” step for c1" in held["problem"]
    assert held["plain"].endswith("(the AI's work is waiting for someone to check it)")
    assert kept["ok"], kept


def test_an_example_moving_a_record_several_stages_on_takes_it_through_the_stages_between(app, owner):
    """“Move the order to Invoiced” from New goes by Picking and Delivered, as the person would, and the rules on the way
    run; what a button waits for that the example left alone is filled in on the way (“once Tracking is filled in”), as
    the person told so would; where the example's own values block the way, it says at which stage and why."""
    _, ws = owner
    _built(ws, [{"op": "create_table", "table": "orders", "label": "Orders", "fields": [
        {"key": "title", "type": "text", "required": True}, {"key": "tracking", "type": "text"}, {"key": "delivered_on", "type": "date"},
        {"key": "status", "type": "select", "options": ["New", "Picking", "Delivered", "Invoiced"]}],
        "workflow": {"field": "status", "transitions": [
            {"from": ["New"], "to": "Picking", "label": "Pick"},
            {"from": ["Picking"], "to": "Delivered", "label": "Deliver", "only_if": [{"field": "tracking", "op": "is_not_empty"}]},
            {"from": ["Delivered"], "to": "Invoiced", "label": "Invoice"}]}},
        {"op": "add_automation", "automation": {
            "key": "delivered", "name": "Note the delivery", "trigger": {"type": "record_updated", "table": "orders"},
            "conditions": [{"field": "status", "op": "changed_to", "value": "Delivered"}],
            "actions": [{"type": "update_record", "values": {"delivered_on": "today"}}]}}])
    along = {"title": "Invoiced at last", "steps": [
        {"do": "add", "table": "orders", "as": "o1", "values": {"title": "Flour", "tracking": "TRK1"}},
        {"do": "move", "record": "o1", "to": "Invoiced"}],
        "expect": [{"record": "o1", "field": "status", "equals": "Invoiced"}, {"record": "o1", "field": "delivered_on", "equals": "non-empty"}]}
    filled = {"title": "Tracking filled in on the way", "steps": [
        {"do": "add", "table": "orders", "as": "o3", "values": {"title": "Salt"}},
        {"do": "move", "record": "o3", "to": "Invoiced"}],
        "expect": [{"record": "o3", "field": "status", "equals": "Invoiced"}, {"record": "o3", "field": "tracking", "equals": "non-empty"}]}
    blocked = {"title": "No tracking", "steps": [
        {"do": "add", "table": "orders", "as": "o2", "values": {"title": "Sugar", "tracking": ""}},
        {"do": "move", "record": "o2", "to": "Invoiced"}],
        "expect": [{"record": "o2", "field": "status", "equals": "Invoiced"}]}
    _, (went, along_filled, stuck) = trial.try_out(ws, [along, filled, blocked], screens=False)
    assert went["ok"], went
    assert along_filled["ok"], along_filled
    assert not stuck["ok"] and "on its way from New to Invoiced" in stuck["problem"] and "from Picking to Delivered" in stuck["problem"]


def test_a_change_kept_from_staff_is_tried_on_a_record_they_cant_see_and_a_sum_says_how_it_was_worked_out(app, owner):
    """A sent invoice locked from staff is refused to them even where only its rep sees it; and an expected due date the
    example got wrong (1 Oct + 30 days) is told with the formula and what it read, after the days waited."""
    _, ws = owner
    _built(ws, [{"op": "create_table", "table": "invoices", "label": "Invoices", "row_access": {"field": "rep", "roles": ["editor"]},
                 "lock_if": [{"field": "status", "op": "equals", "value": "Sent"}], "lock_except": ["status"], "fields": [
        {"key": "title", "type": "text", "required": True}, {"key": "rep", "type": "user"}, {"key": "total", "type": "money"},
        {"key": "issue_date", "type": "date"}, {"key": "terms", "type": "number"},
        {"key": "due_date", "type": "formula", "format": "date", "expression": "add_days(issue_date, terms)"},
        {"key": "status", "type": "select", "options": ["Draft", "Sent", "Paid"]}]}])
    add = {"do": "add", "table": "invoices", "as": "i1", "values": {"title": "Flour", "rep": "Sam", "total": 100, "status": "Sent",
                                                                    "issue_date": "2026-10-01", "terms": 30}}
    locked = {"title": "A sent invoice is locked", "steps": [add, {"do": "change", "record": "i1", "values": {"total": 90}, "refused": True}],
              "expect": [{"record": "i1", "field": "total", "equals": 100}]}
    let_in = {"title": "Its status isn't", "steps": [add, {"do": "change", "record": "i1", "values": {"status": "Paid"}, "refused": True}],
              "expect": [{"record": "i1", "field": "status", "equals": "Sent"}]}
    due = {"title": "Due a month on", "steps": [add, {"do": "wait", "days": 3}],
           "expect": [{"record": "i1", "field": "due_date", "equals": "2026-11-01"}]}
    same = {"title": "Changed to what it was", "steps": [add, {"do": "change", "record": "i1", "values": {"total": 100}, "refused": True}],
            "expect": [{"record": "i1", "field": "total", "equals": 100}]}
    _, (refused, allowed, sum_, nothing) = trial.try_out(ws, [locked, let_in, due, same], screens=False)
    assert refused["ok"], refused
    assert nothing["kind"] == "example" and "nothing would change" in nothing["problem"]  # the example's to correct
    assert not allowed["ok"] and "should have been refused" in allowed["problem"]
    assert not sum_["ok"] and "(worked out as `add_days(issue_date, terms)` from issue_date = '2026-10-01', terms = 30)" in sum_["problem"]


def test_a_field_required_when_its_empty_is_simply_required():
    f = schema.normalize_field({"key": "reorder_level", "type": "number", "required_if": [{"field": "reorder_level", "op": "is_empty"}]})
    assert f.get("required") is True and "required_if" not in f
    g = schema.normalize_field({"key": "asset_tag", "type": "text", "required_if": [{"field": "is_capital", "op": "equals", "value": True}]})
    assert not g.get("required") and g["required_if"][0]["field"] == "is_capital"


def test_a_rule_calling_a_function_there_isnt_is_sent_back_rather_than_never_holding(app, owner):
    """“all(depends_on, status == 'Complete')” can never be worked out, so a rule written with it would quietly hold
    nothing back on every save: the design is refused, naming the functions there are."""
    from app.engine.records import RecordContext
    _, ws = owner
    stages = {"op": "create_table", "table": "stages", "label": "Stages", "fields": [
        {"key": "name", "type": "text"}, {"key": "status", "type": "select", "options": ["Not started", "In progress", "Complete"]},
        {"key": "depends_on", "type": "link", "table": "stages"}],
        "rules": [{"check": "is_empty(depends_on) or all(depends_on, status == 'Complete')",
                   "message": "A stage can't start before the one it depends on is complete",
                   "when": [{"field": "status", "op": "equals", "value": "In progress"}]}]}
    result = ops.apply(ws.blueprint, [stages], always_check=True)
    assert any("there's no function all()" in p and "add_days" in p for p in result.problems), result.problems
    stages["rules"][0]["check"] = "is_empty(depends_on) or depends_on.status == 'Complete'"
    assert not ops.apply(ws.blueprint, [stages], always_check=True).problems
    assert set(RecordContext(Engine(ws), None).functions) == set(expr.RECORD_FUNCTIONS)  # what a record gives is what's known


def test_a_rule_that_routes_a_record_to_its_approver_and_straight_past_them_is_found(app, owner):
    """Routed to the team lead and, in the same run, on to Approved: nobody ever gives the OK. Routing alone, or a rule that
    asks for the OK before moving on, is fine."""
    _, ws = owner
    flow = {"op": "create_table", "table": "requests", "label": "Requests", "fields": [
        {"key": "title", "type": "text"}, {"key": "total", "type": "money"},
        {"key": "status", "type": "select", "options": ["Draft", "Submitted", "Waiting for team lead", "Approved"]}],
        "workflow": {"field": "status", "transitions": [
            {"from": ["Draft"], "to": "Submitted", "label": "Submit"},
            {"from": ["Submitted"], "to": "Waiting for team lead", "label": "Route"},
            {"from": ["Waiting for team lead"], "to": "Approved", "label": "Team lead approves"}]}}
    route = {"key": "route", "name": "Route by amount", "trigger": {"type": "record_updated", "table": "requests"},
             "conditions": [{"field": "status", "op": "changed_to", "value": "Submitted"}],
             "actions": [{"type": "branch", "paths": [{"label": "Any amount", "when": [{"field": "total", "op": "at_least", "value": 0}],
                                                       "steps": [{"type": "update_record", "values": {"status": "Waiting for team lead"}}]}]},
                         {"type": "update_record", "values": {"status": "Approved"}}]}
    _built(ws, [flow, {"op": "add_automation", "automation": route}])
    assert any("puts requests records at “Waiting for team lead” and takes them straight on to “Approved”" in i for i in _issues(ws))
    _built(ws, [{"op": "update_automation", "automation": {**route, "actions": route["actions"][:1]}}])
    assert not any("straight on" in i for i in _issues(ws))
    _built(ws, [{"op": "add_automation", "automation": {  # its own rule, firing when the request gets to the team lead
        "key": "lead", "name": "Team lead approves", "trigger": {"type": "record_updated", "table": "requests"},
        "conditions": [{"field": "status", "op": "changed_to", "value": "Waiting for team lead"}],
        "actions": [{"type": "update_record", "values": {"status": "Approved"}}]}}])
    assert any("starts when requests records get to “Waiting for team lead” and takes them straight on to “Approved”" in i for i in _issues(ws)), _issues(ws)


def test_an_approval_kept_from_the_only_one_it_is_for_is_found(app, owner):
    """The owner's OK, kept from whoever added the quote, would leave what the owner adds waiting for ever: it's saved as
    the owner's OK alone (staff can't give it anyway). A team kept from its own approval is found, said plainly."""
    _, ws = owner
    quotes = {"op": "create_table", "table": "quotes", "label": "Quotes", "fields": [
        {"key": "title", "type": "text"}, {"key": "status", "type": "select", "options": ["Draft", "Needs approval", "Sent"]}],
        "workflow": {"field": "status", "transitions": [{"from": ["Draft"], "to": "Needs approval", "label": "Ask"},
                                                        {"from": ["Needs approval"], "to": "Sent", "label": "Send"}]}}
    rule = {"key": "ok", "name": "Big discounts need the OK", "trigger": {"type": "record_updated", "table": "quotes"},
            "conditions": [{"field": "status", "op": "changed_to", "value": "Needs approval"}],
            "actions": [{"type": "request_approval", "approver": "owner", "not_by": ["creator"], "message": "Please approve this."}]}
    _built(ws, [quotes, {"op": "add_automation", "automation": rule}, {"op": "set_team", "team": {"key": "sales", "name": "Sales"}}])
    assert "not_by" not in ws.blueprint["automations"][0]["actions"][0]
    assert not any("could never be" in i for i in _issues(ws))
    _built(ws, [{"op": "update_automation", "automation": {**rule, "actions": [{**rule["actions"][0], "approver": "sales", "not_by": ["sales"]}]}}])
    plain = [i["plain"] for i in checks.run(Engine(ws))]
    assert "A quote could never be approved by “Big discounts need the OK”: the only people it's for may not answer it" in plain, plain


def test_an_ai_step_that_picks_a_record_answers_with_the_one_the_example_names(app, owner):
    """The AI matches a bank payment to its invoice: in the try-out it answers with what the example expects, inv1, which
    is the invoice the example added under that name, not one called “inv1”."""
    _, ws = owner
    _built(ws, [{"op": "create_table", "table": "invoices", "label": "Invoices", "fields": [{"key": "title", "type": "text", "required": True}]},
                {"op": "create_table", "table": "payments", "label": "Payments", "fields": [
                    {"key": "reference", "type": "text"}, {"key": "invoice", "type": "link", "table": "invoices"}]},
                {"op": "add_automation", "automation": {
                    "key": "match", "name": "Match payments", "trigger": {"type": "record_created", "table": "payments"},
                    "actions": [{"type": "ai_fill", "field": "invoice", "instructions": "Find the invoice this pays"}]}}])
    example = {"title": "A payment finds its invoice", "steps": [
        {"do": "add", "table": "invoices", "as": "inv1", "values": {"title": "Chairs"}},
        {"do": "add", "table": "payments", "as": "pay1", "values": {"reference": "INV-001"}}],
        "expect": [{"record": "pay1", "field": "invoice", "equals": "inv1"}]}
    _, (matched,) = trial.try_out(ws, [example], screens=False)
    assert matched["ok"], matched


def test_item_in_a_step_done_once_reads_the_rules_own_record(app, owner):
    """A received PO line adds stock with {{item.product}}: there's no item outside a step done for each line, so it was
    always empty and the step stopped. It reads the line itself; a for-each step keeps its item."""
    _, ws = owner
    _built(ws, [{"op": "create_table", "table": "products", "label": "Products", "fields": [{"key": "name", "type": "text", "required": True}]},
                {"op": "create_table", "table": "po_lines", "label": "PO lines", "fields": [
                    {"key": "product", "type": "link", "table": "products", "required": True}, {"key": "received_qty", "type": "number"}]},
                {"op": "create_table", "table": "stock_movements", "label": "Stock movements", "fields": [
                    {"key": "product", "type": "link", "table": "products", "required": True}, {"key": "quantity", "type": "number", "required": True}]}])
    result = ops.apply(ws.blueprint, [{"op": "add_automation", "automation": {
        "key": "receive", "name": "Receiving adds stock", "trigger": {"type": "record_updated", "table": "po_lines"},
        "conditions": [{"field": "received_qty", "op": "changed"}],
        "actions": [{"type": "create_record", "table": "stock_movements",
                     "values": {"product": "{{item.product}}", "quantity": "{{ item.received_qty * 1 }}"}}]}}])
    assert not result.problems, result.problems
    made = result.blueprint["automations"][0]["actions"][0]["values"]
    assert made == {"product": "{{record.product}}", "quantity": "{{ record.received_qty * 1 }}"}
    assert any("reads its own record where it said “item”" in c for c in result.changes)
    chat.save_version(ws, result.blueprint, result.changes, "chat")
    engine = Engine(ws)
    line = engine.create("po_lines", {"product": engine.create("products", {"name": "Cheddar"}).id})
    engine.update(engine.get(line.id), {"received_qty": 10})
    assert [r.data["quantity"] for r in Engine(ws).rows("stock_movements")] == [10]


def test_an_expected_duration_is_read_as_its_typed_in(app, owner):
    """8:00 of hours logged, expected as "8:00" (as a timesheet takes it), is 8 hours, not nothing."""
    _, ws = owner
    _built(ws, [{"op": "create_table", "table": "projects", "label": "Projects", "fields": [
        {"key": "name", "type": "text", "required": True},
        {"key": "hours_logged", "type": "rollup", "aggregate": "sum", "table": "timesheets", "field": "hours", "via": "project"}]},
        {"op": "create_table", "table": "timesheets", "label": "Timesheets", "fields": [
            {"key": "project", "type": "link", "table": "projects", "required": True}, {"key": "hours", "type": "duration"}]}])
    steps = [{"do": "add", "table": "projects", "as": "p1", "values": {"name": "Audit"}},
             {"do": "add", "table": "timesheets", "as": "t1", "values": {"project": "p1", "hours": "8:00"}}]
    right = {"title": "Hours add up", "steps": steps, "expect": [{"record": "p1", "field": "hours_logged", "equals": "8:00"}]}
    wrong = {"title": "Hours expected wrong", "steps": steps, "expect": [{"record": "p1", "field": "hours_logged", "equals": "7:30"}]}
    _, (ok, off) = trial.try_out(ws, [right, wrong], screens=False)
    assert ok["ok"], ok
    assert off["plain"] == "Project’s Hours logged came out 8h, expected 7h 30m", off["plain"]


def test_trying_it_out_signs_what_a_move_needs_signed_before_moving_on(app, owner):
    """“Client signs off” needs the client's signature: the try-out signs it, as the person would, rather than saying the
    button was offered and then refused."""
    _, ws = owner
    _built(ws, [{"op": "create_table", "table": "variations", "label": "Variations", "fields": [
        {"key": "title", "type": "text", "required": True}, {"key": "client_signature", "type": "signature"}, {"key": "signed_on", "type": "date"},
        {"key": "status", "type": "select", "options": ["Proposed", "Client signed"], "default": "Proposed"}],
        "workflow": {"field": "status", "transitions": [
            {"from": ["Proposed"], "to": "Client signed", "label": "Client signs off", "requires": ["client_signature", "signed_on"]}]}}])
    walked, _ = trial.try_out(ws, [], screens=False)
    assert not [i for i in walked["issues"] if "refused although the move showed as open" in i["text"]], walked["issues"]


def test_the_last_gift_a_donor_gave_is_the_latest_date(app, owner):
    """A total that takes the latest (or earliest) of a date: the last gift, the first order. It came out empty, as if
    dates were numbers that weren't there."""
    _, ws = owner
    _built(ws, [{"op": "create_table", "table": "donors", "label": "Donors", "fields": [
        {"key": "name", "type": "text", "required": True},
        {"key": "last_gift", "type": "rollup", "table": "donations", "via": "donor", "aggregate": "max", "field": "date"},
        {"key": "first_gift", "type": "rollup", "table": "donations", "via": "donor", "aggregate": "min", "field": "date"}]},
        {"op": "create_table", "table": "donations", "label": "Donations", "fields": [
            {"key": "donor", "type": "link", "table": "donors", "required": True}, {"key": "date", "type": "date"}, {"key": "amount", "type": "money"}]}])
    e = Engine(ws)
    mark = e.create("donors", {"name": "Mark"})
    for day in ("2026-11-04", "2026-11-01", "2026-10-20"):
        e.create("donations", {"donor": mark.id, "date": day, "amount": 50})
    e.reset()
    assert e.value(e.get(mark.id), "last_gift") == "2026-11-04" and e.value(e.get(mark.id), "first_gift") == "2026-10-20"
    assert e.display(e.field("donors", "last_gift"), e.value(e.get(mark.id), "last_gift")) == "4 Nov 2026"


def test_a_link_kept_to_matching_records_must_name_fields_both_sides_have(app, owner):
    """Only a tenant of the request's property: tenants with no property field refused every tenant there was."""
    _, ws = owner
    design = [{"op": "create_table", "table": "properties", "label": "Properties", "fields": [{"key": "address", "type": "text", "required": True}]},
              {"op": "create_table", "table": "tenants", "label": "Tenants", "fields": [{"key": "name", "type": "text", "required": True}]},
              {"op": "create_table", "table": "requests", "label": "Requests", "fields": [
                  {"key": "property", "type": "link", "table": "properties"},
                  {"key": "tenant", "type": "link", "table": "tenants", "match": {"property": "property"}}]}]
    result = ops.apply(ws.blueprint, design, always_check=True)
    assert any("Tenants has no field “property”" in p for p in result.problems), result.problems
    design[1]["fields"].append({"key": "property", "type": "link", "table": "properties"})
    assert not ops.apply(ws.blueprint, design, always_check=True).problems


def test_an_example_expecting_what_only_a_rule_started_from_outside_fills_in_is_told_so(app, owner):
    """A bank feed's payments are matched to their invoices when another system sends them: a payment added by hand in an
    example never starts that, and the builder hears so rather than only that the invoice came out empty."""
    _, ws = owner
    _built(ws, [{"op": "create_table", "table": "invoices", "label": "Invoices", "fields": [{"key": "title", "type": "text", "required": True}]},
                {"op": "create_table", "table": "payments", "label": "Payments", "fields": [
                    {"key": "reference", "type": "text"}, {"key": "invoice", "type": "link", "table": "invoices"}]},
                {"op": "add_automation", "automation": {
                    "key": "feed", "name": "Bank feed payments", "trigger": {"type": "webhook_received", "table": "payments"},
                    "actions": [{"type": "ai_create_record", "table": "payments"}, {"type": "ai_fill", "field": "invoice", "instructions": "Match it"}]}}])
    example = {"title": "A payment is matched", "steps": [
        {"do": "add", "table": "invoices", "as": "inv1", "values": {"title": "Chairs"}},
        {"do": "add", "table": "payments", "as": "pay1", "values": {"reference": "INV-001"}}],
        "expect": [{"record": "pay1", "field": "invoice", "equals": "inv1"}]}
    _, (result,) = trial.try_out(ws, [example], screens=False)
    assert not result["ok"] and "only “Bank feed payments” fills it in" in result["problem"] and "leave this out of the example" in result["problem"]


def test_a_record_added_then_its_lines_has_its_rules_run_once_the_lines_are_in(app, owner):
    """A sale, then its two lines, as an example writes it: the sale's own rule (stock out for each line) runs as the form
    runs it, once the lines are in, not on a sale with none yet."""
    _, ws = owner
    _built(ws, [{"op": "create_table", "table": "sales", "label": "Sales", "fields": [{"key": "customer", "type": "text"}]},
                {"op": "create_table", "table": "sale_lines", "label": "Sale lines", "embedded": True, "fields": [
                    {"key": "sale", "type": "link", "table": "sales", "required": True}, {"key": "item", "type": "text"},
                    {"key": "quantity", "type": "number"}]},
                {"op": "create_table", "table": "stock_movements", "label": "Stock movements", "fields": [
                    {"key": "item", "type": "text"}, {"key": "quantity", "type": "number"}]},
                {"op": "add_automation", "automation": {
                    "key": "out", "name": "Sales take stock out", "trigger": {"type": "record_created", "table": "sales"},
                    "actions": [{"type": "create_for_each", "table": "stock_movements", "source": {"table": "sale_lines", "via": "sale"},
                                 "values": {"item": "{{item.item}}", "quantity": "{{item.quantity}}"}}]}}])
    example = {"title": "A sale of two things takes them out of stock", "steps": [
        {"do": "add", "table": "sales", "as": "s1", "values": {"customer": "Jane"}},
        {"do": "add", "table": "sale_lines", "as": "l1", "values": {"sale": "s1", "item": "Beans", "quantity": 2}},
        {"do": "add", "table": "sale_lines", "as": "l2", "values": {"sale": "s1", "item": "Mug", "quantity": 1}}],
        "expect": [{"table": "stock_movements", "count": 2}]}
    _, (sold,) = trial.try_out(ws, [example], screens=False)
    assert sold["ok"], sold


def test_a_formula_that_gives_words_is_shown_as_words_and_compared_as_them(app, owner):
    """“Low” or “OK”, worked out by a formula marked as a number, showed and compared as no number at all."""
    _, ws = owner
    result = ops.apply(ws.blueprint, [{"op": "create_table", "table": "stock_items", "label": "Stock items", "fields": [
        {"key": "name", "type": "text", "required": True}, {"key": "on_hand", "type": "number"}, {"key": "reorder_level", "type": "number"},
        {"key": "low_stock", "type": "formula", "format": "number", "expression": "iif(on_hand <= reorder_level, 'Low', 'OK')"},
        {"key": "value", "type": "formula", "format": "money", "expression": "iif(on_hand > 0, on_hand * 2, 0)"}]}])
    fields = {f["key"]: f for f in result.blueprint["tables"][0]["fields"]}
    assert fields["low_stock"]["format"] == "text" and fields["value"]["format"] == "money"
    assert any("Low stock is shown as words" in c for c in result.changes)
    chat.save_version(ws, result.blueprint, result.changes, "chat")
    engine = Engine(ws)
    as_number = {"type": "formula", "format": "number", "expression": "iif(on_hand <= reorder_level, 'Low', 'OK')"}  # an app saved before
    assert trial._same(engine, as_number, "Low", "Low", {}) and not trial._same(engine, as_number, "OK", "Low", {})


def test_a_person_looked_up_through_a_link_is_compared_as_a_person(app, owner):
    """A request's approver, looked up from its cost centre, holds who it is: “Sam” expected is Sam, not “came out Sam,
    expected Sam”. And a percentage written as 1 for 1% matches 1%."""
    user, ws = owner
    result = ops.apply(ws.blueprint, [
        {"op": "create_table", "table": "cost_centres", "label": "Cost centres", "fields": [
            {"key": "name", "type": "text", "required": True}, {"key": "approver", "type": "user"}]},
        {"op": "create_table", "table": "requests", "label": "Requests", "fields": [
            {"key": "title", "type": "text", "required": True}, {"key": "cost_centre", "type": "link", "table": "cost_centres"},
            {"key": "approver", "type": "lookup", "via": "cost_centre", "field": "approver"}, {"key": "variance", "type": "percent"}]}])
    assert not result.problems, result.problems
    chat.save_version(ws, result.blueprint, result.changes, "chat")
    engine = Engine(ws)
    looked_up = schema.field(engine.tables["requests"], "approver")
    assert engine.kind(looked_up) == "user"
    assert trial._same(engine, looked_up, user.id, user.name, {}) and trial._same(engine, looked_up, [user.id], user.name, {})
    assert not trial._same(engine, looked_up, user.id + 1000, user.name, {})
    variance = schema.field(engine.tables["requests"], "variance")
    assert trial._same(engine, variance, 0.01, 1, {}) and trial._same(engine, variance, 1, 1, {}) and not trial._same(engine, variance, 0.02, 1, {})


def test_an_example_that_expects_two_values_of_one_field_is_its_own_mistake(app, owner):
    """Expected at “Waiting for team lead” and at “Approved”: what an example expects is looked at once, at its end, so
    it's the example that's wrong, told how to put it right, not the app."""
    _, ws = owner
    _built(ws, QUOTES)
    example = {"title": "A quote goes out", "steps": [{"do": "add", "as": "q1", "table": "quotes", "values": {"title": "Wedding"}}],
               "expect": [{"record": "q1", "field": "status", "equals": "Draft"}, {"record": "q1", "field": "status", "equals": "Sent"}]}
    _, (result,) = trial.try_out(ws, [example], screens=False)
    assert result["kind"] == "example" and "both “Draft” and “Sent”" in result["problem"] and "its own example" in result["problem"]
    example["expect"] = example["expect"][:1] * 2  # the same twice is only said twice
    _, (result,) = trial.try_out(ws, [example], screens=False)
    assert result["kind"] != "example", result


def test_a_rule_that_never_ran_on_the_record_is_said_with_why(app, owner):
    """The quote's status is set by a rule that starts when a quote is changed: added with its lines, it never is, so the
    builder is told the rule never ran and how to have it start."""
    _, ws = owner
    rule = {"key": "mark", "name": "Mark it sent", "trigger": {"type": "record_updated", "table": "quotes"},
            "conditions": [{"field": "status", "op": "equals", "value": "Draft"}], "actions": [{"type": "update_record", "values": {"status": "Sent"}}]}
    _built(ws, QUOTES + [{"op": "add_automation", "automation": rule}])
    example = {"title": "A quote is sent", "steps": [{"do": "add", "as": "q1", "table": "quotes", "values": {"title": "Wedding"}},
                                                     {"do": "add", "as": "l1", "table": "quote_lines", "values": {"quote": "q1", "item": "Cake", "quantity": 1, "unit_price": 10}}],
               "expect": [{"record": "q1", "field": "status", "equals": "Sent"}]}
    _, (result,) = trial.try_out(ws, [example], screens=False)
    assert not result["ok"] and any("“Mark it sent” sets it, but never ran on q1: it starts when a quote is changed" in d for d in result["detail"]), result


def test_a_stage_added_by_its_move_goes_after_the_one_it_comes_from(app, owner):
    """“On hold, straight after Applied”, written as moves only: it's put after Applied, so the journey reads Applied → On
    hold → Approved, and nothing thinks Adopted comes before it. A stop (Withdrawn) still goes last."""
    _, ws = owner
    result = ops.apply(ws.blueprint, [{"op": "create_table", "table": "adoptions", "label": "Adoptions", "fields": [
        {"key": "animal", "type": "text", "required": True}, {"key": "status", "type": "select", "options": ["Applied", "Approved", "Adopted"]}],
        "workflow": {"field": "status", "transitions": [
            {"from": ["Applied"], "to": "On hold", "label": "Put on hold"}, {"from": ["Applied", "On hold"], "to": "Approved", "label": "Approve"},
            {"from": ["Approved"], "to": "Adopted", "label": "Adopt"}, {"from": ["Applied"], "to": "Withdrawn", "label": "Withdraw"}]}}])
    assert not result.problems, result.problems
    status = result.blueprint["tables"][0]["fields"][1]
    assert status["options"] == ["Applied", "On hold", "Approved", "Adopted", "Withdrawn"]
    assert schema.a_one("Adoption", start=True) == "An adoption" and schema.a_one("GST invoice") == "a GST invoice"


def _me(ws, user):
    from app.models import Membership
    return Engine(ws, by=user.name, member=Membership.query.filter_by(workspace_id=ws.id, user_id=user.id).first())


def test_a_move_to_a_done_stage_fills_in_its_last_words_fields(app, owner):
    """Moving an agreement to “Owner approved” fills in Approved by and Owner approved date as the person would have; a
    move to “Waiting for payment” never fills in a payment date (it's not paid yet)."""
    user, ws = owner
    _built(ws, [{"op": "create_table", "table": "agreements", "label": "Agreements", "fields": [
        {"key": "title", "type": "text", "required": True}, {"key": "approved_by", "label": "Approved by", "type": "user"},
        {"key": "owner_approved_date", "label": "Owner approved date", "type": "date"},
        {"key": "payment_date", "label": "Payment date", "type": "date"},
        {"key": "status", "type": "select", "options": ["Draft", "Owner approved", "Waiting for payment"]}],
        "workflow": {"field": "status", "transitions": [{"from": ["Draft"], "to": "Owner approved", "label": "Approve"},
                                                        {"from": ["Owner approved"], "to": "Waiting for payment", "label": "Bill it"}]}}])
    me = _me(ws, user)
    a = me.create("agreements", {"title": "Jane's plan"})
    me.move(me.get(a.id), next(i for i, m, _ in me.moves(me.get(a.id)) if m["to"] == "Owner approved"))
    data = me.get(a.id).data
    assert data["approved_by"] == user.id and data["owner_approved_date"] == me.today.isoformat()
    me.move(me.get(a.id), next(i for i, m, _ in me.moves(me.get(a.id)) if m["to"] == "Waiting for payment"))
    assert me.get(a.id).data.get("payment_date") in (None, "")


def test_the_approver_named_where_nobody_was_asked_is_found(app, owner):
    """An order approved on its own within its credit limit, “Approved by” set to {{approver}} there: nobody was asked, so
    it's always empty. After the owner's OK on the other way it's who said yes, and isn't found."""
    _, ws = owner
    orders = {"op": "create_table", "table": "orders", "label": "Orders", "fields": [
        {"key": "title", "type": "text", "required": True}, {"key": "total", "type": "money"},
        {"key": "approved_by", "label": "Approved by", "type": "user"},
        {"key": "status", "type": "select", "options": ["Draft", "Pending approval", "Approved", "Rejected"]}]}
    rule = {"key": "credit", "name": "Order approval and credit check", "trigger": {"type": "record_updated", "table": "orders"},
            "conditions": [{"field": "status", "op": "changed_to", "value": "Pending approval"}],
            "actions": [{"type": "branch", "paths": [
                {"label": "Within credit limit", "when": [{"field": "total", "op": "at_most", "value": 5000}],
                 "steps": [{"type": "update_record", "values": {"status": "Approved", "approved_by": "{{approver}}"}}]},
                {"label": "Over credit limit", "when": [{"field": "total", "op": "greater_than", "value": 5000}],
                 "steps": [{"type": "request_approval", "approver": "owner", "message": "Please approve this."},
                           {"type": "update_record", "values": {"status": "Approved", "approved_by": "{{approver}}"}}]}]}]}
    _built(ws, [orders, {"op": "add_automation", "automation": rule}])
    found = checks.nobody_asked(Engine(ws))
    assert [i["plain"] for i in found] == [
        "In “Order approval and credit check” on the path “Within credit limit”, approved by is always left empty: nobody is "
        "asked for their OK there"], found
    assert "no request_approval comes before it" in found[0]["text"]


def test_a_sample_refused_by_a_rule_on_two_fields_is_tried_with_other_values_of_them(app, owner):
    """“An Active coach must have signed the waiver”: the sample coach (Active, no waiver) is refused, then tried On leave,
    which is fine, so the rule isn't reported as one an ordinary coach can't get past."""
    _, ws = owner
    _built(ws, [{"op": "create_table", "table": "coaches", "label": "Coaches", "fields": [
        {"key": "name", "type": "text", "required": True}, {"key": "waiver", "type": "file"},
        {"key": "status", "type": "select", "options": ["Active", "On leave", "Inactive"]}],
        "rules": [{"check": "not is_empty(waiver)", "message": "A coach must sign the waiver before being Active",
                   "when": [{"field": "status", "op": "equals", "value": "Active"}]}]}])
    with trial.copy_of(ws) as copy:
        found = trial.walk(copy, screens=False)
    assert not [i for i in found["issues"] if "couldn't be saved" in i["plain"]], found["issues"]
    assert "coaches" in found["made"]


def test_an_example_about_something_being_stopped_is_told_to_expect_the_refusal(app, owner):
    """The rule stopped what the example is about (“A coach without a waiver is blocked”): the builder is told to mark that
    step refused, the rule being right, not to turn the rule round."""
    _, ws = owner
    _built(ws, [{"op": "create_table", "table": "coaches", "label": "Coaches", "fields": [
        {"key": "name", "type": "text", "required": True}, {"key": "waiver", "type": "file"},
        {"key": "status", "type": "select", "options": ["Active", "On leave"]}],
        "rules": [{"check": "not is_empty(waiver)", "message": "A coach must sign the waiver before being Active",
                   "when": [{"field": "status", "op": "equals", "value": "Active"}]}]}])
    example = {"title": "A coach without a waiver is blocked from being Active", "steps": [
        {"do": "add", "table": "coaches", "as": "c1", "values": {"name": "Alex", "status": "Active"}}],
        "expect": [{"record": "c1", "field": "waiver", "equals": "non-empty"}]}
    _, (result,) = trial.try_out(ws, [example], screens=False)
    assert 'mark step 1 "refused": true' in result["problem"] and "the rule is right" in result["problem"]
    assert result["problem"].index('"refused": true') < result["problem"].index("written backwards"), result["problem"]
    example["steps"][0]["refused"] = True
    example["expect"] = [{"table": "coaches", "count": 0}]
    _, (result,) = trial.try_out(ws, [example], screens=False)
    assert result["ok"], result


def test_a_wrong_total_says_what_it_added_up(app, owner):
    """10 × $150 and 20 × $50 is $2,500, not $2,000: the builder sees each line's sum and what made it, and a total built on
    another total says how that was worked out, to tell a wrong sum in the example from a wrong total."""
    _, ws = owner
    _built(ws, QUOTES)
    example = {"title": "A big quote", "steps": [
        {"do": "add", "table": "quotes", "as": "q1", "values": {"title": "Frame"}},
        {"do": "add", "table": "quote_lines", "as": "l1", "values": {"quote": "q1", "item": "Beams", "quantity": 10, "unit_price": 150}},
        {"do": "add", "table": "quote_lines", "as": "l2", "values": {"quote": "q1", "item": "Welding", "quantity": 20, "unit_price": 50}}],
        "expect": [{"record": "q1", "field": "subtotal", "equals": 2000}, {"record": "q1", "field": "total", "equals": 2200}]}
    _, (result,) = trial.try_out(ws, [example], screens=False)
    said = " ".join(result["detail"])
    assert "the sum of quote_lines.amount over its 2 quote_lines: 1500.0 (from quantity = 10, unit_price = 150.0)" in said, said
    assert "subtotal = 2500.0 (the sum of quote_lines.amount over its 2 quote_lines: 1500.0, 1000.0)" in said, said
    assert "gst = 250.0 (`round((subtotal - discount) * 0.1, 2)`)" in said, said


def test_a_rule_that_adds_a_record_without_the_person_it_must_have_is_found(app, owner):
    """A weekly family update must name its nurse, and the rule that adds one each week doesn't: it stops every time."""
    _, ws = owner
    _built(ws, [{"op": "create_table", "table": "residents", "label": "Residents", "fields": [{"key": "name", "type": "text", "required": True}]},
                {"op": "create_table", "table": "updates", "label": "Family updates", "fields": [
                    {"key": "resident", "type": "link", "table": "residents", "required": True},
                    {"key": "nurse", "type": "user", "required": True}, {"key": "summary", "type": "long_text"}]},
                {"op": "add_automation", "automation": {
                    "key": "weekly", "name": "Weekly family update", "trigger": {"type": "schedule", "table": "residents", "every": "week"},
                    "actions": [{"type": "create_record", "table": "updates", "values": {"resident": "{{record}}"}}]}}])
    plain = [i["plain"] for i in checks.left_unfilled(Engine(ws))]
    assert plain == ["“Weekly family update” can't add a family update: it doesn't fill in nurse, which must be filled in"], plain


def test_a_step_said_again_straight_after_a_branch_is_done_only_on_its_way(app, owner):
    """“On hold: they owe $X” told for a customer who owes, then again straight after the branch, for every job: the copy
    after goes, so it's said once, and only when they owe."""
    _, ws = owner
    told = {"type": "notify", "to": "everyone", "message": "Job {{record.title}} is on hold: the customer owes money"}
    result = ops.apply(ws.blueprint, [
        {"op": "create_table", "table": "jobs", "label": "Jobs", "fields": [
            {"key": "title", "type": "text", "required": True}, {"key": "owed", "type": "money"}]},
        {"op": "add_automation", "automation": {
            "key": "hold", "name": "Block new jobs for unpaid customers", "trigger": {"type": "record_created", "table": "jobs"},
            "actions": [{"type": "branch", "paths": [{"label": "Customer owes", "when": [{"field": "owed", "op": "greater_than", "value": 0}],
                                                       "steps": [dict(told)]}]}, dict(told)]}}])
    assert not result.problems, result.problems
    actions = result.blueprint["automations"][0]["actions"]
    assert len(actions) == 1 and actions[0]["paths"][0]["steps"] == [told]
    assert any("only when Customer owes: it was done again straight after" in c for c in result.changes), result.changes


def test_a_long_form_asks_what_matters_first_counting_its_lines(app, owner, client):
    """A hire order (eight details over three sections) and its lines: the optional details still empty past the first
    few wait under “More details”, named when they're few, and a section with none left up front shows no empty box."""
    from .conftest import login
    _, ws = owner
    _built(ws, [{"op": "create_table", "table": "orders", "label": "Hire orders", "singular": "Hire order", "fields": [
        {"key": "customer", "type": "text", "required": True}, {"key": "site", "type": "address"},
        {"key": "starts", "type": "date", "required": True}, {"key": "ends", "type": "date"},
        {"key": "delivery", "label": "Delivery date", "type": "date"}, {"key": "collection", "label": "Collection date", "type": "date"},
        {"key": "deposit", "type": "money"}, {"key": "notes", "type": "long_text"}],
        "sections": [{"label": "Hire details", "fields": ["customer", "site", "starts", "ends", "delivery", "collection"]},
                     {"label": "Money", "fields": ["deposit"]}, {"label": "Notes", "fields": ["notes"]}]},
        {"op": "create_table", "table": "lines", "label": "Hire lines", "embedded": True, "fields": [
            {"key": "order", "type": "link", "table": "orders", "required": True}, {"key": "item", "type": "text"},
            {"key": "quantity", "type": "number"}, {"key": "days", "type": "number"}, {"key": "rate", "type": "money"}]}])
    login(client)
    html = client.get(f"/w/{ws.id}/app/t/orders/new").get_data(as_text=True)
    start = html.index('id="record-form"')
    form = html[start:html.index("</form>", start)]
    upfront = __import__("re").sub(r'(?is)<details class="more-fields".*?</details>', " ", form)
    assert 'name="customer"' in upfront and 'name="starts"' in upfront and 'name="lines-lines-0-item"' in upfront
    assert 'name="delivery"' not in upfront and 'name="deposit"' not in upfront and 'name="notes"' not in upfront
    assert "More details (optional): Delivery date, Collection date" in form and "More details (optional): Deposit" in form
    assert form.count('<div class="card-body form-grid">') == 1 + 3  # what's up front once, and inside each More details


def test_an_owners_check_is_always_written_and_a_step_acts_on_the_one_in_front_of_them(app, owner):
    """“A $6,000 order waits for my sign-off” on an app that doesn't do it yet: the check is still written (one, always),
    so it says what the app doesn't do yet (nothing there could stop it), rather than “couldn't be made a check”. A move that doesn't say which quote moves the one
    just added, as a person would on the quote in front of them."""
    from app.builder import checking, prompts
    _, ws = owner
    _built(ws, QUOTES)
    asked = []

    def backend(messages, schema_, on_progress):
        asked.append((messages, schema_))
        if schema_ is prompts.CHECK_AGAIN_SCHEMA:  # looked at again: the app's, not the example's
            return json.dumps({"verdict": "app", "missing": "nothing waits for your sign-off", "examples": []}), ""
        return json.dumps({"examples": [{"title": "x", "steps": [
            {"do": "add", "table": "quotes", "as": "q1", "values": {"title": "Big job", "discount_percent": 5}},
            {"do": "add", "table": "quote_lines", "as": "l1", "values": {"quote": "q1", "item": "Marquee", "quantity": 1, "unit_price": 6000}},
            {"do": "move", "as": "", "to": "Sent", "refused": True}],
            "expect": [{"record": "q1", "field": "status", "equals": "Draft"}]}]}), ""
    ai.set_backend(backend)
    result = checking.add(ws, "A $6,000 quote waits for my sign-off before it goes out", by="owner")
    (messages, schema_), (again, _) = asked
    assert prompts.ALWAYS_ONE in messages[0]["content"] and prompts.NONE_IF_LOOKS not in messages[0]["content"]
    assert schema_["properties"]["examples"]["minItems"] == 1
    assert "should have been refused, but the app allowed it" in again[-1]["content"]  # shown what happened
    assert result["kind"] == "missing" and not result["ok"] and result["looked_again"] == "app", result
    assert result["plain"] == "your app doesn't do this yet: nothing waits for your sign-off", result  # nothing there could stop it


def test_a_failing_owners_check_is_looked_at_again_before_the_owner_hears_it(app, owner):
    """“A quote over 10% off waits for my OK” on an app that does it: the first example has the owner, who may approve,
    try the approval and expects it refused. Looked at again, it's corrected to a staff member trying it, refused as it
    should be, so the check passes. One about restock orders the app hasn't got says what the app doesn't do yet, and
    isn't kept as a check that could never be acted out."""
    from app.builder import checking, prompts
    _, ws = owner
    _built(ws, QUOTES)
    steps = [{"do": "add", "table": "quotes", "as": "q1", "values": {"title": "Big job", "discount_percent": 15}},
             {"do": "move", "as": "", "record": "q1", "to": "Awaiting approval"},
             {"do": "move", "as": "", "record": "q1", "to": "Sent", "refused": True}]
    corrected = steps[:2] + [dict(steps[2], by="staff")]
    seen = []

    def backend(messages, schema_, on_progress):
        seen.append(messages[-1]["content"])
        if schema_ is prompts.CHECK_AGAIN_SCHEMA:
            return json.dumps({"verdict": "example", "missing": "", "examples": [
                {"title": "x", "steps": corrected, "expect": [{"record": "q1", "field": "status", "equals": "Awaiting approval"}]}]}), ""
        return json.dumps({"examples": [{"title": "x", "steps": steps,
                                         "expect": [{"record": "q1", "field": "status", "equals": "Awaiting approval"}]}]}), ""
    ai.set_backend(backend)
    result = checking.add(ws, "A quote over 10% off waits for my OK before it goes out", by="owner")
    assert result["ok"] and result["looked_again"] == "example", result
    assert result["example"]["steps"][2]["by"] == "staff"
    assert [x["title"] for x in checking.examples(ws)] == ["A quote over 10% off waits for my OK before it goes out"]

    def backend2(messages, schema_, on_progress):
        if schema_ is prompts.CHECK_AGAIN_SCHEMA:
            assert "there's no table" in messages[-1]["content"]
            return json.dumps({"verdict": "app", "missing": "there are no restock orders in it", "examples": []}), ""
        return json.dumps({"examples": [{"title": "y", "steps": [
            {"do": "add", "table": "purchase_orders", "as": "po1", "values": {"total": 620}}],
            "expect": [{"record": "po1", "field": "status", "equals": "Pending"}]}]}), ""
    ai.set_backend(backend2)
    result = checking.add(ws, "A restock order over $500 waits for my OK", by="owner")
    assert result["kind"] == "missing" and not result["ok"], result
    assert result["plain"] == "your app doesn't do this yet: there are no restock orders in it"
    assert len(checking.examples(ws)) == 1  # not kept: it could never be acted out


def test_a_step_can_change_a_record_two_links_away(app, owner):
    """A payment's rule sets “Deposit paid” on its invoice, whose Status has no such choice: its invoice's couple's has,
    so the step is put right to go there (“invoice.couple”), says so in words, and does it."""
    user, ws = owner
    _built(ws, [
        {"op": "create_table", "table": "couples", "label": "Couples", "fields": [
            {"key": "name", "type": "text", "required": True}, {"key": "status", "type": "select", "options": ["Enquiry", "Deposit paid"]}]},
        {"op": "create_table", "table": "contracts", "label": "Contracts", "fields": [
            {"key": "couple", "type": "link", "table": "couples", "required": True},
            {"key": "status", "type": "select", "options": ["Draft", "Signed"]}]},
        {"op": "create_table", "table": "invoices", "label": "Invoices", "fields": [
            {"key": "couple", "type": "link", "table": "couples", "required": True}, {"key": "contract", "type": "link", "table": "contracts"},
            {"key": "status", "type": "select", "options": ["Draft", "Sent", "Paid"]}]},
        {"op": "create_table", "table": "payments", "label": "Payments", "fields": [
            {"key": "invoice", "type": "link", "table": "invoices", "required": True}, {"key": "amount", "type": "money"}]},
        {"op": "add_automation", "automation": {"key": "locks", "name": "Deposit locks the date", "trigger": {"type": "record_created", "table": "payments"},
                                                "actions": [{"type": "update_record", "target": "invoice", "values": {"status": "Paid"}},
                                                            {"type": "update_record", "target": "invoice", "values": {"status": "Deposit paid"}},
                                                            {"type": "update_record", "target": "invoice", "values": {"status": "Signed"}}]}}])
    steps = ws.blueprint["automations"][0]["actions"]
    assert [x["target"] for x in steps] == ["invoice", "invoice.couple", "invoice.contract"]
    from app.engine import automations
    me = _me(ws, user)
    assert automations.describe_action(me, ws.blueprint["automations"][0], steps[1]) == "On its Invoice’s Couple, set Status to “Deposit paid”"
    couple = me.create("couples", {"name": "Ana & Ben", "status": "Enquiry"})
    contract = me.create("contracts", {"couple": couple.id, "status": "Draft"})
    invoice = me.create("invoices", {"couple": couple.id, "contract": contract.id, "status": "Sent"})
    me.create("payments", {"invoice": invoice.id, "amount": 500})
    db.session.expire_all()
    assert [Record.query.get(x.id).data["status"] for x in (invoice, couple, contract)] == ["Paid", "Deposit paid", "Signed"]
    assert not _issues(ws) or not any("isn't one of its choices" in i for i in _issues(ws))


def test_a_field_named_after_a_word_python_keeps_can_be_read_in_a_formula(app, owner):
    """A class's capacity, an email's from, a pass mark: fields a business may well name so are read in formulas."""
    user, ws = owner
    _built(ws, [
        {"op": "create_table", "table": "classes", "label": "Classes", "fields": [
            {"key": "name", "type": "text", "required": True}, {"key": "capacity", "type": "number"}]},
        {"op": "create_table", "table": "sessions", "label": "Sessions", "fields": [
            {"key": "class", "type": "link", "table": "classes", "required": True}, {"key": "booked", "type": "number"},
            {"key": "pass", "type": "number"},
            {"key": "fill_rate", "type": "formula", "format": "percent",
             "expression": "iif(class.capacity > 0, round(booked / class.capacity * 100, 0), 0)"},
            {"key": "note", "type": "formula", "expression": "iif(pass >= 50, 'Pass from ' + class.name, 'Fail')"}]}])
    me = _me(ws, user)
    yoga = me.create("classes", {"name": "Yoga", "capacity": 20})
    s = me.create("sessions", {"class": yoga.id, "booked": 5, "pass": 60})
    assert me.value(s, "fill_rate") == 0.25 and me.value(s, "note") == "Pass from Yoga"  # a percentage is kept a fraction
    assert expr.names_in("iif(pass >= 50, 'class from ' + class.name, 'x')") == {"pass", "class"}


def test_limits_keep_how_many_share_a_day_and_that_bookings_dont_overlap(app, owner):
    """Five jobs a Saturday (cancelled ones take no place), a class's capacity through its session, and a room booked
    once at a time, the day one stay ends free for the next: refused when they're saved, said in words, and checked."""
    from app.engine.records import FieldErrors
    user, ws = owner
    _built(ws, [
        {"op": "create_table", "table": "jobs", "label": "Jobs", "fields": [
            {"key": "client", "type": "text", "required": True}, {"key": "event_date", "type": "date", "required": True},
            {"key": "status", "type": "select", "options": ["Booked", "Cancelled"], "default": "Booked"}],
         "limits": [{"per": ["event_date"], "max": 2, "where": [{"field": "status", "op": "not_equals", "value": "Cancelled"}],
                     "message": "We've only got the vans for two jobs that day"}]},
        {"op": "create_table", "table": "classes", "label": "Classes", "fields": [
            {"key": "name", "type": "text", "required": True}, {"key": "capacity", "type": "number"}]},
        {"op": "create_table", "table": "sessions", "label": "Sessions", "fields": [
            {"key": "class", "type": "link", "table": "classes", "required": True}, {"key": "on", "type": "date"}]},
        {"op": "create_table", "table": "class_bookings", "label": "Class bookings", "fields": [
            {"key": "session", "type": "link", "table": "sessions", "required": True}, {"key": "member", "type": "text", "required": True}],
         "capacity": {"per": "session", "max": "{{record.session.class.capacity}}"}},
        {"op": "create_table", "table": "stays", "label": "Stays", "fields": [
            {"key": "guest", "type": "text", "required": True}, {"key": "room", "type": "select", "options": ["1", "2"], "required": True},
            {"key": "check_in", "type": "date", "required": True}, {"key": "check_out", "type": "date"}],
         "limits": [{"per": ["room"], "overlap": ["check_in", "check_out"]}]}])
    t = schema.table(ws.blueprint, "class_bookings")
    assert t["limits"] == [{"per": ["session"], "max": "session.class.capacity"}]
    me = _me(ws, user)
    me.create("jobs", {"client": "A", "event_date": "2026-11-07"})
    third = me.create("jobs", {"client": "B", "event_date": "2026-11-07", "status": "Cancelled"})
    me.create("jobs", {"client": "C", "event_date": "2026-11-07"})
    try:
        me.create("jobs", {"client": "D", "event_date": "2026-11-07"})
        raise AssertionError("a third job that day was let in")
    except FieldErrors as exc:
        assert exc.errors["_"] == "We've only got the vans for two jobs that day"
    me.create("jobs", {"client": "D", "event_date": "2026-11-08"})
    try:
        me.update(third, {"status": "Booked"})  # brought back: it takes a place again
        raise AssertionError("an un-cancelled job went past the limit")
    except FieldErrors:
        pass
    yoga = me.create("classes", {"name": "Yoga", "capacity": 1})
    monday = me.create("sessions", {"class": yoga.id, "on": "2026-11-09"})
    me.create("class_bookings", {"session": monday.id, "member": "Ana"})
    try:
        me.create("class_bookings", {"session": monday.id, "member": "Ben"})
        raise AssertionError("the class went past its capacity")
    except FieldErrors as exc:
        assert exc.errors["_"] == "There can't be more than 1 class bookings for the same session: there are 1 already"
    me.create("stays", {"guest": "Ana", "room": "1", "check_in": "2026-11-05", "check_out": "2026-11-07"})
    me.create("stays", {"guest": "Ben", "room": "1", "check_in": "2026-11-07", "check_out": "2026-11-09"})  # the day Ana leaves
    me.create("stays", {"guest": "Cy", "room": "2", "check_in": "2026-11-06", "check_out": "2026-11-08"})
    try:
        me.create("stays", {"guest": "Di", "room": "1", "check_in": "2026-11-08"})
        raise AssertionError("a room was booked twice")
    except FieldErrors as exc:
        assert "already has this room then" in exc.errors["_"]
    from app.blueprint.ops import _policies_said
    assert _policies_said(schema.table(ws.blueprint, "stays")) == ["no two at overlapping times for the same room"]
    assert _policies_said(schema.table(ws.blueprint, "class_bookings")) == ["at most its session’s class’s capacity for the same session"]
    bad = ops.apply(ws.blueprint, [{"op": "update_table", "table": "stays", "changes": {"limits": [{"per": ["bed"], "max": "guest"}]}}])
    assert any("“bed”, which isn't a field" in x for x in bad.problems) and any("isn't a number field" in x for x in bad.problems), bad.problems


def test_an_example_is_acted_out_as_people_would_press_its_buttons(app, owner):
    """A Check out button asks for Checked out by, and Pickup date too (once it's Picked up it must be filled in), so
    the example's move fills them in as the person would rather than being refused; a document is pressed as its
    button; a staff member trying a move only the owner may make, expected to leave it where it was, shows it stopped.
    A formula given as a number field's value without {{ }} is worked out, not saved as its words."""
    from app.builder import trial as trial_
    from app.models import File
    user, ws = owner
    _built(ws, [
        {"op": "create_table", "table": "bookings", "label": "Bookings", "fields": [
            {"key": "customer", "type": "text", "required": True}, {"key": "rate", "type": "money"}, {"key": "fee", "type": "money"},
            {"key": "checked_out_by", "type": "user"},
            {"key": "pickup_date", "type": "date", "required_if": [{"field": "status", "op": "one_of", "value": ["Picked up"]}]},
            {"key": "status", "type": "select", "options": ["Confirmed", "Picked up", "Approved"]}],
         "workflow": {"field": "status", "transitions": [
             {"from": ["Confirmed"], "to": "Picked up", "label": "Check out", "requires": ["checked_out_by"]},
             {"from": ["Picked up"], "to": "Approved", "label": "Approve", "who": ["owner"]}]},
         "documents": [{"key": "docket", "label": "Hire docket"}]},
        {"op": "add_automation", "automation": {"key": "fee", "name": "Work out the fee", "trigger": {"type": "record_created", "table": "bookings"},
                                                "actions": [{"type": "update_record", "values": {"fee": "round(rate * 1.1, 2)"}}]}}])
    assert ws.blueprint["automations"][0]["actions"][0]["values"]["fee"] == "{{round(rate * 1.1, 2)}}"
    t = schema.table(ws.blueprint, "bookings")
    me = _me(ws, user)
    b = me.create("bookings", {"customer": "Ana", "rate": 100, "status": "Confirmed"})
    move = t["workflow"]["transitions"][0]
    assert [f["key"] for f in me.to_fill(t, me.get(b.id), move)] == ["checked_out_by", "pickup_date"]  # the button asks for both
    assert me.value(me.get(b.id), "fee") == 110
    example = {"title": "Checked out, its docket made, approved only by the owner", "steps": [
        {"do": "add", "as": "b1", "table": "bookings", "values": {"customer": "Ben", "rate": 50, "status": "Confirmed"}},
        {"do": "move", "as": "", "record": "b1", "to": "Picked up"},
        {"do": "press", "as": "", "record": "b1", "button": "docket"},
        {"do": "move", "as": "", "record": "b1", "to": "Approved", "by": "staff"}],
        "expect": [{"record": "b1", "field": "status", "equals": "Picked up"}, {"record": "b1", "field": "fee", "equals": 55}]}
    with trial_.copy_of(ws) as copy:
        result = trial_.play(copy, example)
        made = File.query.filter_by(workspace_id=copy.ws_id).count()
    assert result["ok"], result
    assert made == 1
    allowed = dict(example, steps=example["steps"][:3] + [dict(example["steps"][3], by="", refused=True)])
    with trial_.copy_of(ws) as copy:
        result = trial_.play(copy, allowed)
    assert not result["ok"] and "the owner did it, who may do nearly anything" in result["problem"], result


def test_buttons_on_the_way_are_filled_in_with_dates_that_fit(app, owner):
    """“Move it to Picked up” from Enquiry goes through Confirmed, whose button asks for a deposit; Picked up asks for
    a pickup date, which mustn't be after the hire starts: filled in as a person would, with a date that fits."""
    from app.builder import trial as trial_
    _, ws = owner
    _built(ws, [{"op": "create_table", "table": "bookings", "label": "Bookings", "fields": [
        {"key": "customer", "type": "text", "required": True}, {"key": "hire_start", "type": "date", "required": True},
        {"key": "deposit", "type": "money"}, {"key": "pickup_date", "type": "date"},
        {"key": "status", "type": "select", "options": ["Enquiry", "Confirmed", "Picked up"]}],
        "rules": [{"check": "is_empty(pickup_date) or pickup_date <= hire_start", "message": "Pickup can't be after hire start"}],
        "workflow": {"field": "status", "transitions": [
            {"from": ["Enquiry"], "to": "Confirmed", "label": "Confirm", "requires": ["deposit"]},
            {"from": ["Confirmed"], "to": "Picked up", "label": "Check out", "requires": ["pickup_date"]}]}}])
    example = {"title": "Picked up", "steps": [
        {"do": "add", "as": "b1", "table": "bookings", "values": {"customer": "Ana", "hire_start": "2026-10-25"}},
        {"do": "move", "as": "", "record": "b1", "to": "Picked up"}],
        "expect": [{"record": "b1", "field": "status", "equals": "Picked up"}]}
    with trial_.copy_of(ws) as copy:
        result = trial_.play(copy, example)
    assert result["ok"], result


def _failing_wedding(ws):
    """Quotes whose discount sum is wrong, with the builder's wedding check kept and failing."""
    from app.builder import checking
    broken = json.loads(json.dumps(QUOTES))
    broken[0]["fields"][3]["expression"] = "round(subtotal + discount_percent, 2)"
    _built(ws, broken)
    ws.state = {**(ws.state or {}), "examples": [EXAMPLES[0]]}
    db.session.commit()
    checking.check(ws, "a change")
    assert [x["ok"] for x in ws.state["checks"]["examples"]] == [False]






def test_fix_it_and_yes_please_are_understood_as_asking_for_what_isnt_right_to_be_fixed(app, owner):
    from app.builder import checking
    _, ws = owner
    _failing_wedding(ws)
    ws.state = {**ws.state, "refused_checks": [{"said": "Overdue invoices are chased after 14 days", "why": "there is no reminder", "missing": True}]}
    db.session.commit()

    def said(text, after=None):
        if after:
            db.session.add(Message(workspace_id=ws.id, channel="builder", role="assistant", text=after, status="done"))
        m = Message(workspace_id=ws.id, channel="builder", role="user", text=text, status="done")
        db.session.add(m)
        db.session.commit()
        return chat._mending(ws, m)

    assert said("Can you add a Team note to quotes?") is None  # an ordinary change: not a fix
    assert said("fix it please")["all"] and said("Fix what didn't come out right")["all"]
    assert said("yes please", after="Not everything works yet. When I tried it out, this didn't come out right: “A wedding quote”.")["all"]
    named = said("Make it do this: Overdue invoices are chased after 14 days, by email", after="I've added a Note to quotes.")
    assert not named["all"] and named["found"]["missing"][0]["said"].startswith("Overdue") and not named["found"]["failing"]
    text = checking.fix_request(ws)
    assert "“A wedding quote with 15% off”" in text and "Please also make my app do these (it doesn't yet):\n- “Overdue invoices are chased after 14 days” (there is no reminder)" in text
    assert checking.fix_request(ws, only="Overdue invoices are chased after 14 days").startswith("Please make my app do these")


SHOP = [
    {"op": "create_table", "table": "orders", "label": "Orders", "fields": [
        {"key": "bride", "type": "text", "required": True}, {"key": "deposit", "type": "money"},
        {"key": "status", "type": "select", "options": ["Enquiry", "Deposit paid", "In fitting", "Ready"]}]},
    {"op": "create_table", "table": "alterations", "label": "Alterations", "fields": [
        {"key": "what", "type": "text", "required": True}, {"key": "order", "type": "link", "table": "orders", "required": True},
        {"key": "status", "type": "select", "options": ["Open", "Done"]}]},
    {"op": "add_automation", "automation": {"key": "deposit_in", "name": "Deposit paid moves order on", "trigger": {"type": "record_updated", "table": "orders"},
                                            "conditions": [{"field": "deposit", "op": "changed_to", "value": "not_empty"}],
                                            "actions": [{"type": "update_record", "values": {"status": "Deposit paid"}}]}},
    {"op": "add_automation", "automation": {"key": "done", "name": "Alteration done moves order", "trigger": {"type": "record_updated", "table": "alterations"},
                                            "conditions": [{"field": "status", "op": "changed_to", "value": "Done"}],
                                            "actions": [{"type": "update_record", "target": "order", "values": {"status": "Ready"},
                                                         "only_if": [{"field": "status", "op": "equals", "value": "In fitting"}]}]}},
]


def test_a_step_on_a_linked_record_checks_that_records_stage_and_filled_in_means_any_value(app, owner):
    """“When an alteration is done, move its order to Ready, if it's In fitting” means the order's stage (the alteration
    has no such choice); “when the deposit changes to filled in” runs for any amount, not for the word "not_empty"."""
    _, ws = owner
    result = ops.apply(ws.blueprint, SHOP)
    assert not result.problems, result.problems
    step = next(a for a in result.blueprint["automations"] if a["key"] == "done")["actions"][0]
    assert step["only_if"] == [{"field": "order.status", "op": "equals", "value": "In fitting"}]
    assert any("now checks that order's Status" in c for c in result.changes), result.changes
    chat.save_version(ws, result.blueprint, result.changes, "chat")
    engine = Engine(ws)
    order = engine.create("orders", {"bride": "Ava", "status": "Enquiry"})
    engine.update(order, {"deposit": 0})
    assert engine.value(engine.get(order.id), "status") == "Enquiry"  # a 0 isn't a deposit paid
    engine.update(engine.get(order.id), {"deposit": 900})
    assert engine.value(engine.get(order.id), "status") == "Deposit paid"
    engine.update(engine.get(order.id), {"status": "In fitting"})
    fix = engine.create("alterations", {"what": "Hem", "order": order.id, "status": "Open"})
    engine.update(fix, {"status": "Done"})
    assert engine.value(engine.get(order.id), "status") == "Ready"




def test_the_currency_is_where_the_business_is_until_its_set(app, owner):
    from app.engine.records import currency_for
    _, ws = owner
    assert currency_for("Australia/Perth") == "AUD" and currency_for("Europe/London") == "GBP" and currency_for("America/Toronto") == "CAD"
    assert currency_for("Mars/Base") == ""
    assert Engine(ws).base_currency == currency_for(ws.timezone or app.config["DEFAULT_TIMEZONE"]) == "AUD"
    ws.settings = {**(ws.settings or {}), "money": {"base": "NZD", "rates": {}}}
    db.session.commit()
    assert Engine(ws).base_currency == "NZD"


def test_checks_compare_every_linked_record_and_find_one_the_app_made(app, owner):
    """A crew of five expected by the names the example gave them is each of them; a press on a list the example added
    nothing to is on the record the app made there itself."""
    _, ws = owner
    _built(ws, QUOTES)
    engine = Engine(ws)
    a, b, c = (engine.create("quotes", {"title": t}) for t in ("A", "B", "C"))
    f = {"key": "crew", "type": "link", "table": "quotes"}
    names = {"x": a.id, "y": b.id, "z": c.id}
    assert trial._same(engine, f, [a.id, b.id], ["x", "y"], names) and not trial._same(engine, f, [a.id, b.id], ["x", "z"], names)
    assert trial._same(engine, f, [a.id, b.id], ["A", "B"], names) and trial._named(engine, "x", names) == "A"
    meant = trial._the_one_meant(engine, {"do": "move", "to": "Sent"}, {"_added": []})
    assert meant == c.id  # the latest the app has of the list whose stage it is


def test_nothing_moving_records_to_overdue_while_a_rule_chases_only_those_is_found(app, owner):
    _, ws = owner
    design = [{"op": "create_table", "table": "invoices", "label": "Invoices", "fields": [
                  {"key": "number", "type": "text", "required": True}, {"key": "due_date", "type": "date"},
                  {"key": "status", "type": "select", "options": ["Sent", "Overdue", "Paid"]}]},
              {"op": "add_automation", "automation": {"key": "chase", "name": "Chase overdue invoices", "trigger": {"type": "schedule", "table": "invoices", "every": "day"},
                                                      "conditions": [{"field": "status", "op": "equals", "value": "Overdue"}],
                                                      "actions": [{"type": "notify", "to": "owner", "message": "Chase {{record.number}}"}]}}]
    _built(ws, design)
    found = [i for i in checks.run(Engine(ws)) if "Nothing moves" in i["text"]]
    assert found and "due_date is before_today" in found[0]["text"] and found[0]["plain"].startswith("no invoice ever becomes “Overdue”")
    _built(ws, [{"op": "add_automation", "automation": {"key": "late", "name": "Mark late ones", "trigger": {"type": "schedule", "table": "invoices", "every": "day"},
                                                        "conditions": [{"field": "status", "op": "equals", "value": "Sent"}, {"field": "due_date", "op": "before_today"}],
                                                        "actions": [{"type": "update_record", "values": {"status": "Overdue"}}]}}])
    assert not [i for i in checks.run(Engine(ws)) if "Nothing moves" in i["text"]]


def test_a_rule_across_two_amounts_is_kept_by_the_samples(app, owner):
    """A margin rule (“at least 10% on each session”) refused every sample when both amounts got the same values: they're
    left empty first where they may be, and each gets a different value after."""
    _, ws = owner
    _built(ws, [{"op": "create_table", "table": "sessions", "label": "Sessions", "fields": [
        {"key": "student", "type": "text", "required": True}, {"key": "revenue", "type": "money"}, {"key": "cost", "type": "money"}],
        "rules": [{"check": "is_empty(revenue) or is_empty(cost) or (revenue - cost) / revenue >= 0.1", "message": "Margin must be at least 10% on each session."}]}])
    engine = Engine(ws)
    t = engine.tables["sessions"]
    record, errors = trial._add(engine, t, {"student": "Ava", "revenue": 120.5, "cost": 120.5})
    assert record is not None and not errors, errors


def test_the_owners_checks_are_about_what_they_said_not_asides():
    from evals import fresh
    assert fresh.META.search("A $6,000 order waits for the manager's OK, but I didn't mention that threshold so I can't confirm it")
    assert fresh.META.search("A quote for 10 hours at $90 comes to $990 with GST, but I don't have those numbers")
    assert not fresh.META.search("A quote for 10 hours at $90 comes to $990 with GST")
    assert "{said}" in fresh.CHECKS and "no volumes" in fresh.CHECKS




