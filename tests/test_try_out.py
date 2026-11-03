"""The builder tries an app out before handing it over: mistakes found by reading the design (a stage a record gets
stuck at, a choice that isn't one, a stage nothing reaches), a copy of the app walked through, worked examples acted out
in it, and what's wrong sent back to the AI to fix, with what was checked shown to the owner and kept for training."""
import json

from app import ai
from app.blueprint import ops, schema
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
        "the automation “Chase emailed quotes” checks status changed_to “Emailed”, but quotes.status has no such choice (its "
        "choices: Draft, Awaiting approval, Sent, Declined): use one of them, or add it to the field's options"]


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


def test_a_turn_is_tried_out_and_what_it_finds_is_fixed_before_its_handed_over(app, owner, monkeypatch):
    _, ws = owner
    monkeypatch.setattr(chat, "TRY_OUT", True)
    broken = json.loads(json.dumps(QUOTES))
    broken[0]["fields"][3]["expression"] = "round(subtotal + discount_percent, 2)"  # a sum the AI got wrong
    broken[0]["workflow"]["transitions"][1]["only_if"][0]["op"] = "at_most"
    broken[0]["fields"][1]["default"] = 0
    broken[0]["workflow"]["transitions"].append({"from": ["Awaiting approval"], "to": "Declined", "label": "Decline"})
    scripted = ScriptedAI({"ops": broken, "reply": "I've set up quotes with their lines, discount and GST.", "more": False},
                          {"examples": EXAMPLES[:1]},
                          {"ops": [FIXED], "reply": "Fixed the discount.", "more": False})
    ai.set_backend(scripted)
    reply = chat.ask(ws, "Quotes with lines, a discount % and GST at 10%")
    chat.work(reply.id)
    m = db.session.get(Message, reply.id)
    assert m.status == "done" and len(scripted.asked) == 3, m.error
    assert "A wedding quote with 15% off" in scripted.asked[2][-1]["content"] and "discount" in scripted.asked[2][-1]["content"]
    assert m.checks["examples"] == [{"title": "A wedding quote with 15% off", "ok": True, "problem": "", "lines": [
        [True, "Quote’s Subtotal: $5,040.00"], [True, "Quote’s Discount: $756.00"], [True, "Quote’s GST: $428.40"],
        [True, "Quote’s Total: $4,712.40"]], "example": EXAMPLES[0]}] and m.checks["fixes"] == 1 and not m.checks["issues"]
    assert m.checks["first"]["wrong"] == 1 and m.checks["first"]["examples"] == [0, 1]  # not right first time: the discount sum
    assert any("Discount" in c for c in m.changes) and m.version_after == ws.version == 2
    assert [x["title"] for x in ws.state["examples"]] == ["A wedding quote with 15% off"]  # played again after later changes
    kept = [json.loads(line) for f in (app.config["DATA_DIR"] / "training").glob("*.jsonl") for line in f.read_text().splitlines()]
    assert [c["kind"] for c in kept[0]["calls"]] == ["design", "examples", "fix"] and kept[0]["outcome"]["examples"] == [1, 1]
    from app.builder import training
    folder = app.config["DATA_DIR"] / "training"
    assert list(training.examples([folder])) == []  # its own examples passing isn't proof: nobody independent has checked it
    assert [row["kind"] for row in training.examples([folder], "silver")] == ["design", "examples", "fix"]
    training.verdict(folder, True, by="owner checks", workspace=ws.id)
    # verified now: the examples and the fix are gold; the design needed fixing after the try-out, so it stays silver
    assert [row["kind"] for row in training.examples([folder])] == ["examples", "fix"]
    training.verdict(folder, False, "the discount is wrong", by="a tester", workspace=ws.id)
    assert list(training.examples([folder], "bronze")) == []  # anyone's no outweighs every yes
    said = training.report([folder])
    assert "found wrong 1" in said and "Ready to try fine-tuning: not yet" in said, said


def test_what_trying_it_out_couldnt_fix_is_said_plainly(app, owner, monkeypatch):
    _, ws = owner
    monkeypatch.setattr(chat, "TRY_OUT", True)
    monkeypatch.setattr(chat, "TRY_FIXES", 1)
    wrong = {"ops": [{"op": "update_field", "table": "quotes", "key": "title", "changes": {"label": "Name"}}], "reply": "Renamed.", "more": False}
    ai.set_backend(ScriptedAI({"ops": QUOTES, "reply": "I've set up quotes.", "more": False}, {"examples": [EXAMPLES[2]]}, wrong))
    reply = chat.ask(ws, "Quotes with a discount")
    chat.work(reply.id)
    m = db.session.get(Message, reply.id)
    assert m.status == "done" and m.checks["examples"][0]["ok"] is False
    assert m.text.startswith("Not everything works yet. When I tried it out, this didn't come out right: “A sum expected wrong”. "
                             "The details are below."), m.text  # by name: the numbers are in the details, not the reply
    assert "came out" not in m.text and "Quote’s Discount came out $20.00, expected $2.00" in m.checks["examples"][0]["problem"]
    assert m.text.endswith("What I changed: I've set up quotes.")  # what it meant to do comes after what doesn't work
    assert chat._unclaimed("Fixed the family discount so Ava pays $108.") == "I changed the family discount so Ava pays $108."


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
    assert not other["ok"] and other["plain"] == "Treatment note’s Patient came out Casey, expected p2"


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
    run; where the way is blocked, it says at which stage and why."""
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
    blocked = {"title": "No tracking", "steps": [
        {"do": "add", "table": "orders", "as": "o2", "values": {"title": "Sugar"}},
        {"do": "move", "record": "o2", "to": "Invoiced"}],
        "expect": [{"record": "o2", "field": "status", "equals": "Invoiced"}]}
    _, (went, stuck) = trial.try_out(ws, [along, blocked], screens=False)
    assert went["ok"], went
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
    _, (refused, allowed, sum_) = trial.try_out(ws, [locked, let_in, due], screens=False)
    assert refused["ok"], refused
    assert not allowed["ok"] and "should have been refused" in allowed["problem"]
    assert not sum_["ok"] and "(worked out as `add_days(issue_date, terms)` from issue_date = '2026-10-01', terms = 30)" in sum_["problem"]


def test_a_field_required_when_its_empty_is_simply_required():
    f = schema.normalize_field({"key": "reorder_level", "type": "number", "required_if": [{"field": "reorder_level", "op": "is_empty"}]})
    assert f.get("required") is True and "required_if" not in f
    g = schema.normalize_field({"key": "asset_tag", "type": "text", "required_if": [{"field": "is_capital", "op": "equals", "value": True}]})
    assert not g.get("required") and g["required_if"][0]["field"] == "is_capital"
