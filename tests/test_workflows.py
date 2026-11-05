"""Workflows: conditions with "or", fields that show, hide, lock or narrow their choices on conditions, records that move
through stages by buttons, branches, approval flows (several approvers, reminders, passing up, what a no does), tasks,
waiting until something is true, and automations that run others."""
from datetime import timedelta

from app import worker
from app.blueprint import ops, schema
from app.builder import chat, prompts
from app.engine import automations
from app.engine.records import Engine, FieldErrors
from app.extensions import db
from app.models import Approval, Membership, Notification, Run, User, utcnow

import pytest


def _built(ws, design):
    result = ops.apply(ws.blueprint, design)
    assert not result.problems, result.problems
    chat.save_version(ws, result.blueprint, result.changes, "chat")
    return Engine(ws, by="Ethan"), result


def _person(ws, email, name, role):
    user = User(email=email, name=name)
    user.set_password("password1234")
    db.session.add(user)
    db.session.flush()
    member = Membership(workspace_id=ws.id, user_id=user.id, role=role)
    db.session.add(member)
    db.session.commit()
    return user, member


def _login(client, email):
    client.post("/logout")
    return client.post("/login", data={"email": email, "password": "password1234"})


REQUESTS = {"op": "create_table", "table": "requests", "label": "Purchase requests", "singular": "Purchase request", "fields": [
    {"key": "item", "type": "text", "required": True}, {"key": "amount", "type": "money"},
    {"key": "status", "type": "select", "options": ["Draft", "Submitted", "Approved", "Rejected", "Ordered"]},
    {"key": "approver", "type": "user"}, {"key": "quote_file", "type": "text"}, {"key": "reason", "type": "long_text"},
    {"key": "supplier_ref", "type": "text"}],
    "workflow": {"field": "status", "transitions": [
        {"from": ["draft", "Rejected"], "to": "Submitted", "label": "Submit for approval", "requires": ["amount", "quote_file"]},
        {"from": ["Submitted"], "to": "Approved", "label": "Approve", "who": ["approver"]},
        {"from": ["Submitted"], "to": "Rejected", "label": "Reject", "who": "approver"},
        {"from": ["Approved"], "to": "Ordered", "label": "Mark ordered", "who": ["admin"]}]}}


def test_conditions_can_say_or_and_group_ands_inside(app, owner):
    _, ws = owner
    e, _ = _built(ws, [{"op": "create_table", "table": "jobs", "label": "Jobs", "fields": [
        {"key": "title", "type": "text"}, {"key": "priority", "type": "select", "options": ["Low", "Urgent"]},
        {"key": "due", "type": "date"}, {"key": "value", "type": "money"}],
        "views": [{"key": "attention", "label": "Needs attention", "type": "list", "filters": [{"or": [
            {"field": "priority", "op": "equals", "value": "Urgent"},
            {"and": [{"field": "value", "op": ">", "value": 5000}, {"field": "due", "op": "before_today"}]}]}]}]}])
    view = schema.table(ws.blueprint, "jobs")["views"][0]
    assert view["filters"][0]["any"][1]["all"][0]["op"] == "greater_than"  # normalised, inside the group
    urgent = e.create("jobs", {"title": "Burst pipe", "priority": "Urgent"})
    big_late = e.create("jobs", {"title": "Reroof", "value": 9000, "due": "2020-01-01"})
    e.create("jobs", {"title": "Small late", "value": 100, "due": "2020-01-01"})
    e.create("jobs", {"title": "Big later", "value": 9000, "due": "2099-01-01"})
    assert {r.id for r in Engine(ws).query("jobs", view["filters"])} == {urgent.id, big_late.id}
    assert automations.describe_condition(e, view["filters"][0], "jobs") == "either Priority is “Urgent” or (Value is more than $5,000.00 and Due is before today)"
    bad = ops.apply(ws.blueprint, [{"op": "add_view", "table": "jobs", "view": {"key": "x", "label": "X", "type": "list", "filters": [
        {"any": [{"field": "nope", "op": "equals", "value": 1}, {"field": "priority", "op": "equals", "value": "Low"}]}]}}])
    assert any("“nope”" in p for p in bad.problems)


def test_fields_show_hide_lock_and_narrow_their_choices_on_conditions(app, owner, client):
    user, ws = owner
    e, _ = _built(ws, [{"op": "create_table", "table": "jobs", "label": "Jobs", "fields": [
        {"key": "title", "type": "text"}, {"key": "category", "type": "select", "options": ["Plumbing", "Electrical"]},
        {"key": "kind", "type": "select", "options_by": {"field": "category", "options": {
            "Plumbing": ["Leak", "Blocked drain"], "Electrical": ["Power point", "Lighting"]}}},
        {"key": "status", "type": "select", "options": ["Open", "Approved", "Delivered"]},
        {"key": "amount", "type": "money", "readonly_if": [{"field": "status", "op": "equals", "value": "Approved"}]},
        {"key": "internal_note", "type": "text", "hide_if": [{"field": "status", "op": "equals", "value": "Delivered"}]},
        {"key": "delivered_to", "type": "text", "required": True}, {"key": "signed_by", "type": "text"}],
        "sections": [{"label": "Job", "fields": ["title", "category", "kind", "status", "amount", "internal_note"]},
                     {"label": "Delivery", "fields": ["delivered_to", "signed_by"], "show_if": [{"field": "status", "op": "equals", "value": "Delivered"}]}]}])
    t = schema.table(ws.blueprint, "jobs")
    assert t["fields"][2]["options"] == ["Leak", "Blocked drain", "Power point", "Lighting"]  # every choice, from the map
    job = e.create("jobs", {"title": "Kitchen", "category": "Plumbing", "kind": "Leak", "status": "Open"})  # Delivery is hidden: not required
    assert e.choices(t, schema.field(t, "kind"), job) == ["Leak", "Blocked drain"]
    with pytest.raises(FieldErrors) as no:
        e.update(job, {"kind": "Lighting"})
    assert "Lighting isn't a choice for Category Plumbing" in no.value.errors["kind"]
    with pytest.raises(FieldErrors) as needed:
        e.update(job, {"status": "Delivered", "delivered_to": ""})  # now the Delivery section shows, so what it requires is required
    assert "delivered_to" in needed.value.errors
    with pytest.raises(FieldErrors):
        e.create("jobs", {"title": "Shed", "status": "Delivered"})
    e.update(job, {"status": "Approved", "amount": 400})
    editor, member = _person(ws, "sam@example.com", "Sam", "editor")
    as_sam = Engine(ws, member=member)
    with pytest.raises(FieldErrors) as locked:
        as_sam.update(as_sam.get(job.id), {"amount": 500})
    assert "can't be changed now (Status is Approved)" in locked.value.errors["amount"]
    Engine(ws, member=Membership.query.filter_by(user_id=user.id).one()).update(job, {"amount": 450})  # the owner may
    _login(client, "ethan@example.com")
    form = client.get(f"/w/{ws.id}/app/t/jobs/{job.id}/edit").get_data(as_text=True)
    assert "data-options-by=" in form and "data-hide-if=" in form and "data-readonly-if=" in form
    assert form.count("data-show-if=") >= 1  # the Delivery section
    page = client.get(f"/w/{ws.id}/app/t/jobs/{job.id}").get_data(as_text=True)
    assert "Delivered to" not in page and "Internal note" in page  # the hidden section isn't on the record either


def test_a_request_moves_through_its_stages_by_buttons_and_only_the_right_people_move_it(app, owner, client):
    user, ws = owner
    e, result = _built(ws, [REQUESTS])
    assert any("moving through Draft → Submitted → Approved → Rejected → Ordered by its buttons (Submit for approval, Approve" in c
               for c in result.changes)
    flow = schema.table(ws.blueprint, "requests")["workflow"]
    assert flow["transitions"][0]["from"] == ["Draft", "Rejected"]  # spelled as the options are
    sam, sam_m = _person(ws, "sam@example.com", "Sam", "editor")
    pat, pat_m = _person(ws, "pat@example.com", "Pat", "editor")
    as_sam = Engine(ws, member=sam_m, by="Sam")
    with pytest.raises(FieldErrors) as starts:
        as_sam.create("requests", {"item": "Laptop", "status": "Approved"})
    assert "A new purchase request starts at Draft" in starts.value.errors["status"]
    req = as_sam.create("requests", {"item": "Laptop", "approver": pat.id})
    assert req.data["status"] == "Draft"
    with pytest.raises(FieldErrors) as jump:
        as_sam.update(req, {"status": "Approved"})
    assert "Status can't go from Draft to Approved (from Draft it can go to Submitted)" in jump.value.errors["status"]
    moves = as_sam.moves(req)
    assert [(m["label"], problem) for _, m, problem in moves] == [("Submit for approval", "Fill in Amount and Quote file first, then you can submit for approval")]
    with pytest.raises(FieldErrors):
        as_sam.move(req, 0)
    as_sam.update(req, {"amount": 2400, "quote_file": "Q-77"})
    as_sam.move(as_sam.get(req.id), 0)
    assert Engine(ws).get(req.id).data["status"] == "Submitted"
    assert as_sam.moves(as_sam.get(req.id)) == []  # Sam isn't the approver
    as_pat = Engine(ws, member=pat_m, by="Pat")
    assert [m["label"] for _, m, _ in as_pat.moves(as_pat.get(req.id))] == ["Approve", "Reject"]

    _login(client, "pat@example.com")
    base = f"/w/{ws.id}/app"
    page = client.get(f"{base}/t/requests/{req.id}").get_data(as_text=True)
    assert 'class="stage-path"' in page and 'aria-current="step">Submitted' in page and "Approve" in page
    assert 'data-field="status"' not in page  # the stage isn't edited in place
    form = client.get(f"{base}/t/requests/{req.id}/edit").get_data(as_text=True)
    assert "changed with the buttons on it" in form and 'name="status"' not in form
    client.post(f"{base}/t/requests/{req.id}/move/1", follow_redirects=True)
    assert Engine(ws).get(req.id).data["status"] == "Approved"
    drag = client.post(f"{base}/t/requests/{req.id}/set", json={"field": "status", "value": "Draft"})  # a board drag
    assert drag.status_code == 400 and "can't go from Approved to Draft" in drag.get_json()["error"]
    owner_engine = Engine(ws, member=Membership.query.filter_by(user_id=user.id).one())
    owner_engine.update(owner_engine.get(req.id), {"status": "Draft"})  # the owner can put it anywhere
    assert Engine(ws).get(req.id).data["status"] == "Draft"
    wrong = ops.apply(ws.blueprint, [{"op": "update_table", "table": "requests", "changes": {"workflow": {"field": "item", "transitions": [{"to": "X"}]}}}])
    assert any("must be a choice (select) field" in p for p in wrong.problems)


def test_a_branch_sends_big_requests_to_two_approvers_and_small_ones_straight_through(app, owner, client):
    user, ws = owner
    sam, sam_m = _person(ws, "sam@example.com", "Sam", "admin")
    e, _ = _built(ws, [REQUESTS, {"op": "add_automation", "automation": {
        "name": "Approve purchase requests", "trigger": {"type": "record_updated", "table": "requests"},
        "conditions": [{"field": "status", "op": "changed_to", "value": "Submitted"}],
        "actions": [{"type": "if", "if": [{"field": "amount", "op": "greater_than", "value": 5000}],
                     "then": [{"type": "request_approval", "approvers": ["approver", "owner"], "mode": "all",
                               "message": "Approve {{record.item}} for {{record.amount}}?",
                               "if_rejected": [{"type": "update_record", "values": {"status": "Rejected"}},
                                               {"type": "notify", "to": "creator", "message": "{{record.item}} was rejected"}]},
                              {"type": "update_record", "values": {"status": "Approved"}}],
                     "else": [{"type": "update_record", "values": {"status": "Approved"}}]},
                    {"type": "notify", "to": "everyone", "message": "{{record.item}} is through"}]}}])
    words = automations.describe(e, ws.blueprint["automations"][0])
    assert words["flow"][0]["ways"][0]["label"] == "If Amount is more than $5,000.00"
    assert "Ask both the purchase request's approver and the owner to approve it, and wait for every answer" in words["flow"][0]["ways"][0]["steps"][0]["text"]
    assert words["flow"][0]["ways"][0]["steps"][0]["ways"][0]["label"] == "If they say no (and then it stops)"

    small = e.create("requests", {"item": "Mouse", "amount": 40, "quote_file": "Q1", "approver": sam.id})
    e.update(small, {"status": "Submitted"})
    run = Run.query.order_by(Run.id.desc()).first()
    assert run.status == "done" and Engine(ws).get(small.id).data["status"] == "Approved"
    assert run.steps[0]["result"] == "None of the cases fitted, so it did what's set for anything else" and run.steps[1]["at"] == "0.o.0"

    big = e.create("requests", {"item": "Server", "amount": 9000, "quote_file": "Q2", "approver": sam.id})
    e.update(big, {"status": "Submitted"})
    run = Run.query.order_by(Run.id.desc()).first()
    asked = Approval.query.filter_by(run_id=run.id).all()
    assert run.status == "waiting_approval" and len(asked) == 2 and {a.step for a in asked} == {"0.0.0"}
    assert "Approve Server for $9,000.00?" == asked[0].message
    sam_asks = next(a for a in asked if a.assignee_id == sam.id)
    automations.decide(Engine(ws), sam_asks, True, "Sam", "fine")
    run = db.session.get(Run, run.id)
    assert run.status == "waiting_approval"  # the owner must approve too
    owners = next(a for a in asked if a.assignee_id == user.id)  # "all": the owner by name
    automations.decide(Engine(ws), owners, True, "Ethan")
    run = db.session.get(Run, run.id)
    assert run.status == "done" and Engine(ws).get(big.id).data["status"] == "Approved"
    assert any(s.get("result", "").startswith("Approved by Sam (“fine”), Ethan") for s in run.steps)

    rejected = e.create("requests", {"item": "Car", "amount": 50000, "quote_file": "Q3", "approver": sam.id})
    e.update(rejected, {"status": "Submitted"})
    run = Run.query.order_by(Run.id.desc()).first()
    _login(client, "sam@example.com")
    mine = next(a for a in Approval.query.filter_by(run_id=run.id) if a.assignee_id == sam.id)
    client.post(f"/w/{ws.id}/app/approvals/{mine.id}", data={"decision": "reject", "comment": "Too dear"})
    run = db.session.get(Run, run.id)
    assert run.status == "rejected" and "Rejected by Sam (“Too dear”)" in run.error
    assert Engine(ws).get(rejected.id).data["status"] == "Rejected"
    assert Approval.query.filter_by(run_id=run.id, assignee_id=user.id).one().status == "cancelled"  # nobody else need answer
    assert not any(s.get("result", "").endswith("is through") for s in run.steps)  # it stopped there


def test_approvals_remind_pass_up_and_can_be_passed_on_but_not_self_approved(app, owner, client):
    user, ws = owner
    sam, sam_m = _person(ws, "sam@example.com", "Sam", "admin")
    lee, lee_m = _person(ws, "lee@example.com", "Lee", "admin")
    e, _ = _built(ws, [REQUESTS, {"op": "add_automation", "automation": {
        "name": "Ask an admin", "trigger": {"type": "record_updated", "table": "requests"},
        "conditions": [{"field": "status", "op": "changed_to", "value": "Submitted"}],
        "actions": [{"type": "request_approval", "approver": "admin", "message": "OK {{record.item}}?", "remind_after_days": 1,
                     "escalate_after_days": 3, "escalate_to": "owner"}]}}])
    as_sam = Engine(ws, member=sam_m, by="Sam")
    req = as_sam.create("requests", {"item": "Desk", "amount": 300, "quote_file": "Q"})
    as_sam.update(req, {"status": "Submitted"})
    run = Run.query.one()
    ask = Approval.query.one()
    assert ask.role == "admin" and run.status == "waiting_approval" and run.next_try is not None  # the reminder is due later

    _login(client, "sam@example.com")  # Sam asked: an admin, but not the one to approve their own request
    assert client.post(f"/w/{ws.id}/app/approvals/{ask.id}", data={"decision": "approve"}).status_code == 403
    _login(client, "lee@example.com")
    assert "OK Desk?" in client.get(f"/w/{ws.id}/app/approvals").get_data(as_text=True)

    ask.created_at = utcnow() - timedelta(days=1, hours=1)  # a day without an answer: a reminder
    run.next_try = utcnow() - timedelta(minutes=1)
    db.session.commit()
    worker.automation_tick(app)
    assert Notification.query.filter(Notification.text.like("Reminder, still waiting for you: OK Desk?")).count() == 3  # each admin
    ask.created_at = utcnow() - timedelta(days=3, hours=1)  # three days: passed up to the owner
    db.session.get(Run, run.id).next_try = utcnow() - timedelta(minutes=1)
    db.session.commit()
    worker.automation_tick(app)
    up = Approval.query.filter_by(role="owner").one()
    assert up.status == "pending" and up.history[0]["what"] == "escalation"
    assert Notification.query.filter(Notification.text.like("Passed up to you (no answer in 3 days): OK Desk?")).count() == 1

    client.post(f"/w/{ws.id}/app/approvals/{ask.id}", data={"decision": "pass", "to": str(user.id), "comment": "Your call"})
    ask = db.session.get(Approval, ask.id)
    assert ask.assignee_id == user.id and ask.history[-1]["what"] == "passed on" and ask.history[-1]["note"] == "Your call"
    _login(client, "ethan@example.com")
    inbox = client.get(f"/w/{ws.id}/app/approvals").get_data(as_text=True)
    assert "passed on by Lee: “Your call”" in inbox
    client.post(f"/w/{ws.id}/app/approvals/{up.id}", data={"decision": "approve", "comment": "Go"})
    run = db.session.get(Run, run.id)
    assert run.status == "done" and db.session.get(Approval, ask.id).status == "cancelled"
    page = client.get(f"/w/{ws.id}/app/t/requests/{req.id}").get_data(as_text=True)
    assert "Approved" in page and "“Go”" in page  # the decision is on the record


def test_a_task_asks_for_what_to_fill_in_and_the_run_waits_until_its_done(app, owner, client):
    user, ws = owner
    e, _ = _built(ws, [REQUESTS, {"op": "add_automation", "automation": {
        "name": "Order it", "trigger": {"type": "record_updated", "table": "requests"},
        "conditions": [{"field": "status", "op": "changed_to", "value": "Approved"}],
        "actions": [{"type": "task", "to": "admin", "message": "Order {{record.item}} from the supplier", "fields": ["supplier_ref"]},
                    {"type": "update_record", "values": {"status": "Ordered"}}]}}])
    req = e.create("requests", {"item": "Chair", "amount": 200, "quote_file": "Q"})
    e.update(req, {"status": "Approved"})
    run = Run.query.one()
    task = Approval.query.one()
    assert run.status == "waiting_approval" and task.kind == "task" and task.fields == ["supplier_ref"]
    _login(client, "ethan@example.com")
    inbox = client.get(f"/w/{ws.id}/app/approvals").get_data(as_text=True)
    assert "Order Chair from the supplier" in inbox and 'name="supplier_ref"' in inbox and "To do" in inbox
    missing = client.post(f"/w/{ws.id}/app/approvals/{task.id}", data={"decision": "done"}, follow_redirects=True)
    assert "Fill in Supplier ref to finish it." in missing.get_data(as_text=True)
    assert db.session.get(Run, run.id).status == "waiting_approval"
    client.post(f"/w/{ws.id}/app/approvals/{task.id}", data={"decision": "done", "supplier_ref": "PO-55"})
    record = Engine(ws).get(req.id)
    assert record.data["supplier_ref"] == "PO-55" and record.data["status"] == "Ordered"
    assert db.session.get(Run, run.id).status == "done"


def test_waiting_until_paid_goes_on_when_the_record_is_saved_so(app, owner):
    _, ws = owner
    e, _ = _built(ws, [{"op": "create_table", "table": "invoices", "label": "Invoices", "fields": [
        {"key": "number", "type": "text"}, {"key": "status", "type": "select", "options": ["Sent", "Paid"]}, {"key": "thanked", "type": "checkbox"}]},
        {"op": "add_automation", "automation": {"name": "Thank on payment", "trigger": {"type": "record_created", "table": "invoices"},
                                                "actions": [{"type": "wait", "until": [{"field": "status", "op": "equals", "value": "Paid"}], "days": 30},
                                                            {"type": "update_record", "values": {"thanked": True}}]}}])
    invoice = e.create("invoices", {"number": "INV-1", "status": "Sent"})
    run = Run.query.one()
    assert run.status == "waiting" and run.next_try <= utcnow() + timedelta(minutes=16)
    assert run.steps[0]["result"] == "Waiting until Status is Paid"
    e.update(invoice, {"status": "Paid"})
    assert db.session.get(Run, run.id).next_try <= utcnow()  # the save had it look again
    worker.automation_tick(app)
    run = db.session.get(Run, run.id)
    assert run.status == "done" and Engine(ws).get(invoice.id).data["thanked"] is True


def test_an_automation_runs_another_for_the_record_it_made_and_failures_are_told(app, owner):
    _, ws = owner
    e, _ = _built(ws, [{"op": "create_table", "table": "jobs", "label": "Jobs", "fields": [{"key": "title", "type": "text"}]},
                       {"op": "create_table", "table": "invoices", "label": "Invoices", "fields": [
                           {"key": "job", "type": "link", "table": "jobs"}, {"key": "status", "type": "select", "options": ["Draft", "Sent"]}]},
                       {"op": "add_automation", "automation": {"key": "send_it", "name": "Send an invoice", "trigger": {"type": "button", "table": "invoices", "label": "Send"},
                                                               "actions": [{"type": "update_record", "values": {"status": "Sent"}}]}},
                       {"op": "add_automation", "automation": {"name": "Invoice a job", "trigger": {"type": "record_created", "table": "jobs"},
                                                               "actions": [{"type": "create_record", "table": "invoices", "values": {"job": "{{record}}"}},
                                                                           {"type": "run_automation", "automation": "Send an invoice", "target": "created"}]}}])
    assert ws.blueprint["automations"][1]["actions"][1]["automation"] == "send_it"  # named by its name: its key
    e.create("jobs", {"title": "Fix tap"})
    assert Engine(ws).rows("invoices")[0].data["status"] == "Sent"
    assert Run.query.filter_by(automation="send_it").one().trigger == "Started by “Invoice a job”"
    loop = ops.apply(ws.blueprint, [{"op": "add_automation", "automation": {"name": "Nope", "trigger": {"type": "record_created", "table": "jobs"},
                                                                          "actions": [{"type": "run_automation", "automation": "missing"}]}}])
    assert any("runs “missing”, which isn't an automation" in p for p in loop.problems)
    _built(ws, [{"op": "create_table", "table": "notes", "label": "Notes", "fields": [{"key": "text", "type": "text", "required": True},
                                                                                     {"key": "job", "type": "link", "table": "jobs"}]},
                {"op": "add_automation", "automation": {"name": "Broken", "trigger": {"type": "record_created", "table": "jobs"},
                                                        "actions": [{"type": "create_record", "table": "notes", "values": {"job": "{{record}}"}}]}}])
    Engine(ws, by="Ethan").create("jobs", {"title": "Oops"})
    run = Run.query.filter_by(name="Broken").one()
    assert run.status == "failed" and "Text is required" in run.error
    assert Notification.query.filter(Notification.text.like("The rule “Broken” stopped: Text is required%"), Notification.kind == "error").count() == 1




def test_a_move_that_needs_a_file_can_be_made_once_the_file_is_attached(app, owner):
    from app.blueprint import ops
    from app.builder import chat
    from app.storage import save
    _, ws = owner
    result = ops.apply(ws.blueprint, [{"op": "create_table", "table": "requests", "label": "Requests", "fields": [
        {"key": "item", "type": "text"}, {"key": "quote", "label": "Supplier's quote", "type": "file"},
        {"key": "status", "type": "select", "options": ["Draft", "Submitted"]}],
        "workflow": {"field": "status", "transitions": [{"from": ["Draft"], "to": "Submitted", "label": "Submit", "requires": ["quote"]}]}}])
    chat.save_version(ws, result.blueprint, result.changes, "chat")
    e = Engine(ws, by="Ethan")
    req = e.create("requests", {"item": "Pump"})
    assert e.moves(req)[0][2] == "Fill in Supplier's quote first, then you can submit"
    save(ws.id, b"%PDF-1.4 quote", "quote.pdf", record_id=req.id, field="quote", by="Ethan")
    e.reset()
    assert e.moves(e.get(req.id))[0][2] is None


def test_after_an_editor_says_no_the_rule_still_sets_the_stage_its_way(app, owner):
    """A rule's "if they say no" steps run as the rule, not as the approver: an editor's buttons don't limit it."""
    from app.blueprint import ops
    from app.builder import chat
    from app.engine import automations
    from app.models import Approval, Membership, User
    from app.extensions import db
    _, ws = owner
    result = ops.apply(ws.blueprint, [
        {"op": "create_table", "table": "requests", "label": "Requests", "fields": [
            {"key": "item", "type": "text"}, {"key": "lead", "type": "user"},
            {"key": "status", "type": "select", "options": ["Draft", "Submitted", "Approved", "Rejected"]}],
         "workflow": {"field": "status", "transitions": [{"from": ["Draft", "Rejected"], "to": "Submitted", "label": "Submit"}]}},
        {"op": "add_automation", "automation": {"key": "ok", "name": "Approve requests", "trigger": {"type": "record_updated", "table": "requests"},
            "conditions": [{"field": "status", "op": "changed_to", "value": "Submitted"}],
            "actions": [{"type": "request_approval", "approver": "lead", "message": "Approve it?",
                         "if_rejected": [{"type": "update_record", "values": {"status": "Rejected"}}]},
                        {"type": "update_record", "values": {"status": "Approved"}}]}}])
    assert not result.problems, result.problems
    chat.save_version(ws, result.blueprint, result.changes, "chat")
    lead = User(email="lee@example.com", name="Lee")
    lead.set_password("password1234")
    db.session.add(lead)
    db.session.flush()
    member = Membership(workspace_id=ws.id, user_id=lead.id, role="editor")
    db.session.add(member)
    db.session.commit()
    e = Engine(ws, by="Ethan")
    req = e.create("requests", {"item": "Pump", "lead": lead.id})
    e.move(req, 0)
    as_lead = Engine(ws, by="Lee", member=member)
    automations.decide(as_lead, Approval.query.one(), False, "Lee")
    assert Engine(ws).get(req.id).data["status"] == "Rejected"
