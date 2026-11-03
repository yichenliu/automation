"""AI as a stage of a process, under the business's control: a purchasing front door where the AI drafts the statement of
work, reads a supplier's quote and checks it against the contract prices; each AI helper switched off (a person does it),
set up, tried out and added from the AI at work page."""
from app import ai, worker
from app.blueprint import ops, schema
from app.builder import chat
from app.engine import automations
from app.engine.records import Engine
from app.extensions import db
from app.models import Approval, Run
from app.storage import save

from .conftest import ScriptedAI, login

FRONT_DOOR = [
    {"op": "create_table", "table": "suppliers", "label": "Suppliers", "fields": [{"key": "name", "type": "text"}]},
    {"op": "create_table", "table": "contract_prices", "label": "Contract prices", "fields": [
        {"key": "supplier", "type": "link", "table": "suppliers"}, {"key": "item", "type": "text"}, {"key": "unit_price", "type": "money"}]},
    {"op": "create_table", "table": "requests", "label": "Purchase requests", "singular": "Purchase request", "fields": [
        {"key": "title", "type": "text"}, {"key": "brief", "type": "long_text"}, {"key": "supplier", "type": "link", "table": "suppliers"},
        {"key": "status", "type": "select", "options": ["Draft", "Submitted", "Quoted", "Approved"]},
        {"key": "sow", "label": "Statement of work", "type": "rich_text"},
        {"key": "price_check", "type": "select", "options": ["Pass", "Issues"]}, {"key": "price_notes", "type": "long_text"}]},
    {"op": "create_table", "table": "quote_lines", "label": "Quote lines", "embedded": True, "fields": [
        {"key": "request", "type": "link", "table": "requests"}, {"key": "item", "type": "text"}, {"key": "quantity", "type": "number"},
        {"key": "unit_price", "type": "money"}]},
    {"op": "add_automation", "automation": {
        "key": "draft_sow", "name": "Draft the statement of work", "trigger": {"type": "record_updated", "table": "requests"},
        "conditions": [{"field": "status", "op": "changed_to", "value": "Submitted"}],
        "actions": [{"type": "ai_fill", "field": "sow", "instructions": "Draft a statement of work: scope, deliverables, timeline, acceptance",
                     "read_files": True, "review": "always", "reviewer": "creator"}]}},
    {"op": "add_automation", "automation": {
        "key": "check_quote", "name": "Check the quote", "trigger": {"type": "button", "table": "requests", "label": "Check the quote"},
        "actions": [{"type": "ai_extract", "from": "file"},
                    {"type": "ai_check", "field": "price_check", "notes_field": "price_notes", "review": "when_unsure",
                     "instructions": "Compare each quoted line's unit price with the contract price; flag anything over by more than 2%",
                     "reads": [{"table": "contract_prices", "where": [{"field": "supplier", "op": "equals", "value": "{{record.supplier}}"}]}]},
                    {"type": "branch", "paths": [{"label": "Over contract", "when": [{"field": "price_check", "op": "equals", "value": "Issues"}],
                                                  "steps": [{"type": "request_approval", "approver": "admin", "message": "{{record.title}} is quoted over contract"}]}],
                     "otherwise": [{"type": "update_record", "values": {"status": "Approved"}}]}]}},
]


def _built(ws, design):
    result = ops.apply(ws.blueprint, design)
    assert not result.problems, result.problems
    chat.save_version(ws, result.blueprint, result.changes, "chat")
    return Engine(ws, by="Ethan")


def _work(app):
    for _ in range(3):
        worker.automation_tick(app)


def _front_door(ws):
    e = _built(ws, FRONT_DOOR)
    acme, other = e.create("suppliers", {"name": "Acme Office"}), e.create("suppliers", {"name": "Other Co"})
    e.create("contract_prices", {"supplier": acme.id, "item": "Toner", "unit_price": 82})
    e.create("contract_prices", {"supplier": acme.id, "item": "Paper A4", "unit_price": 6})
    e.create("contract_prices", {"supplier": other.id, "item": "Toner", "unit_price": 50})
    from app.models import Membership
    me = Engine(ws, by="Ethan", member=Membership.query.filter_by(workspace_id=ws.id, role="owner").first())  # added by a person
    request = me.create("requests", {"title": "Printer supplies", "brief": "Toner and paper for level 3", "supplier": acme.id, "status": "Draft"})
    return e, acme, request


def test_the_ai_reads_a_quote_and_checks_it_against_the_suppliers_contract_prices(app, owner, monkeypatch):
    _, ws = owner
    monkeypatch.setattr(automations, "SPOT_CHECK", 0)
    e, acme, request = _front_door(ws)
    quote = save(ws.id, b"Acme Office quote Q-77\nToner x4 @ $95.00\nPaper A4 x10 @ $6.00\n", "quote.txt", source="upload")
    quote.record_id, quote.field = request.id, "file"
    db.session.commit()
    scripted = ScriptedAI(
        {"title": None, "brief": None, "status": None, "sow": None, "price_check": None, "price_notes": None,
         "quote_lines": [{"item": "Toner", "quantity": 4, "unit_price": 95}, {"item": "Paper A4", "quantity": 10, "unit_price": 6}], "unsure": []},
        {"findings": ["Toner quoted at $95.00, contract $82.00 (16% over)"], "verdict": "Issues", "sure": True, "why": "One line over contract."})
    ai.set_backend(scripted)
    automations.press(e, ws.blueprint["automations"][1], request, "Ethan")
    _work(app)
    record = Engine(ws).get(request.id)
    lines = Engine(ws).children("quote_lines", "request", request.id)
    assert sorted((x.data["item"], x.data["unit_price"]) for x in lines) == [("Paper A4", 6), ("Toner", 95)]  # read from the quote
    assert record.data["price_check"] == "Issues" and record.data["price_notes"] == "• Toner quoted at $95.00, contract $82.00 (16% over)"
    asked = scripted.asked[1][1]["content"]
    assert "Contract prices" in asked and "$82.00" in asked and "$50.00" not in asked  # only this supplier's contract
    assert "Quote lines" in asked and "Supplier (the linked supplier)" in asked
    run = Run.query.one()
    assert run.status == "waiting_approval" and Approval.query.filter_by(kind="approval").one().message == "Printer supplies is quoted over contract"
    assert any(s.get("result", "").startswith("It's “Over contract”") for s in run.steps)


def test_a_drafted_sow_is_checked_by_whoever_asked_and_the_ai_learns_from_their_corrections(app, owner, client):
    user, ws = owner
    e, acme, request = _front_door(ws)
    brief = save(ws.id, b"We need toner for 4 printers on level 3, delivered by Friday.", "brief.txt", source="upload")
    brief.record_id = request.id
    db.session.commit()
    scripted = ScriptedAI({"value": "Scope: supply toner.", "sure": True, "why": "From the brief."},
                          {"value": "Scope: supply toner.", "sure": True, "why": "From the brief."})
    ai.set_backend(scripted)
    e.update(request, {"status": "Submitted"})
    _work(app)
    assert "toner for 4 printers on level 3" in scripted.asked[0][1]["content"]  # it read the attached brief
    check = Approval.query.filter_by(kind="review").one()
    assert check.assignee_id == user.id  # whoever added the request
    automations.complete_review(Engine(ws), check, "Ethan", {"sow": "Scope: supply 4 toner cartridges to level 3 by Friday. Acceptance: delivered and signed for."})
    _work(app)
    assert check.proposal["outcome"] == "corrected" and check.proposal["corrected"]["sow"].startswith("Scope: supply 4 toner")

    second = e.create("requests", {"title": "More toner", "supplier": acme.id, "status": "Draft"})
    e.update(second, {"status": "Submitted"})
    _work(app)
    prompt = scripted.asked[1][1]["content"]
    assert "People corrected your earlier work on this" in prompt and "Acceptance: delivered and signed for." in prompt
    login(client)
    page = client.get(f"/w/{ws.id}/app/t/requests/{request.id}").get_data(as_text=True)
    assert "AI on this purchase request" in page and "Checked" in page


def test_switched_off_a_person_does_the_check_and_the_process_carries_on(app, owner, client):
    user, ws = owner
    e, acme, request = _front_door(ws)
    login(client)
    base = f"/w/{ws.id}/app"
    page = client.get(base + "/ai").get_data(as_text=True)
    assert "Writes Statement of work" in page and "Checks and sets Price check" in page and "Reads attached files into the purchase request" in page
    assert "Who checks" in page and "Set it up and try it" in page
    client.post(base + "/ai/check_quote/1/switch", data={"on": "0"})
    step = schema.step_at(ws.blueprint["automations"][1]["actions"], "1")
    assert step["manual"] is True
    assert "Switched off · Set up and try it" in client.get(base + "/automations").get_data(as_text=True)

    e = Engine(ws, by="Ethan")
    e.update(request, {"title": "Printer supplies"})  # nothing to read: the AI reading step finds no files and moves on
    ai.set_backend(ScriptedAI())
    automations.press(e, ws.blueprint["automations"][1], e.get(request.id), "Ethan")
    _work(app)
    task = Approval.query.filter_by(kind="task").one()
    assert task.message.startswith("Check it: Compare each quoted line") and task.fields == ["price_check", "price_notes"]
    client.post(base + f"/approvals/{task.id}", data={"decision": "done", "price_check": "Pass", "price_notes": "All on contract."})
    _work(app)
    assert Engine(ws).get(request.id).data["status"] == "Approved"  # the process went on from what the person decided
    run = Run.query.one()
    assert run.steps[1]["ai"] == "manual" and run.status == "done"
    client.post(base + "/ai/check_quote/1/switch", data={"on": "1"})
    assert "manual" not in schema.step_at(ws.blueprint["automations"][1]["actions"], "1")


def test_an_ai_helper_is_set_up_tried_and_added_from_the_ai_at_work_page(app, owner, client):
    user, ws = owner
    e, acme, request = _front_door(ws)
    login(client)
    base = f"/w/{ws.id}/app"
    client.post(base + "/ai/check_quote/1", data={
        "edited": "1", "instructions": "Flag any line more than 5% over contract", "reads": ["contract_prices"],
        "review": "always", "reviewer": "admin", "learn": "1"})
    step = schema.step_at(ws.blueprint["automations"][1]["actions"], "1")
    assert step["instructions"] == "Flag any line more than 5% over contract" and step["review"] == "always" and step["reviewer"] == "admin"
    assert step["reads"][0]["where"][0]["value"] == "{{record.supplier}}"  # the builder's filter stays with its table

    ai.set_backend(ScriptedAI({"findings": ["Toner 16% over"], "verdict": "Issues", "sure": False, "why": "No quote lines yet."}))
    tried = client.post(base + "/ai/check_quote/1/try", data={"record": str(request.id), "edited": "1", "instructions": "Be strict",
                                                              "reads": ["contract_prices"], "review": "always", "learn": "1"}).get_json()
    assert tried["ok"] and tried["values"][0] == ["Price check", "Issues"] and tried["findings"] == ["Toner 16% over"] and tried["sure"] is False
    assert Engine(ws).get(request.id).data.get("price_check") is None and Approval.query.count() == 0  # trying saves nothing

    form = client.get(base + "/ai/new?table=requests").get_data(as_text=True)
    assert "What does the AI do?" in form and "Who checks its work?" in form
    client.post(base + "/ai/new?table=requests", data={
        "table": "requests", "when": "becomes", "when_field": "status", "when_value": "Quoted", "job": "fill", "field": "brief",
        "instructions": "Summarise the request in two sentences", "review": "never"})
    added = ws.blueprint["automations"][-1]
    assert added["name"] == "AI fills in Brief" and added["conditions"][0]["value"] == "Quoted" and added["actions"][0]["type"] == "ai_fill"
    assert "Writes Brief" in client.get(base + "/ai").get_data(as_text=True)


def test_a_job_done_by_hand_can_be_handed_to_the_ai(app, owner, client):
    user, ws = owner
    e = _built(ws, FRONT_DOOR[:3] + [{"op": "add_automation", "automation": {
        "key": "write_up", "name": "Write it up", "trigger": {"type": "record_created", "table": "requests"},
        "actions": [{"type": "task", "to": "editor", "message": "Write the statement of work", "fields": ["sow"]}]}}])
    login(client)
    base = f"/w/{ws.id}/app"
    assert "Jobs people do by hand that the AI could take over" in client.get(base + "/ai").get_data(as_text=True)
    client.post(base + "/ai/take-over/write_up/0", data={"instructions": "Draft the statement of work from the brief"})
    step = ws.blueprint["automations"][-1]["actions"][0]
    assert step == {"type": "ai_fill", "field": "sow", "instructions": "Draft the statement of work from the brief", "review": "always", "reviewer": "editor"}


def test_when_the_ai_cant_do_a_checked_job_the_person_does_it_with_its_reason(app, owner):
    user, ws = owner
    e, acme, request = _front_door(ws)
    ai.set_backend(ScriptedAI({"value": None, "sure": False, "why": "The request has no brief or scope to work from."}))
    e.update(request, {"status": "Submitted", "brief": ""})
    _work(app)
    task = Approval.query.one()
    assert task.kind == "task" and task.assignee_id == user.id and task.fields == ["sow"]
    assert task.message.startswith("The AI couldn't do this (The request has no brief or scope to work from.). Fill in Statement of work")
    automations.complete(Engine(ws), task, "Ethan", {"sow": "Scope: toner for level 3."})
    _work(app)
    run = Run.query.one()
    assert run.status == "done" and run.steps[0]["ai"] == "manual" and Engine(ws).get(request.id).data["sow"] == "Scope: toner for level 3."
