"""What the first wave of beginner testers found (a netball club, a dental clinic, an electrical contractor and a
landscaper building their own apps), each kept fixed. Every fix is the platform's, for any app: the designs here are
shaped like the ones the builder made for them."""
from app import ai, worker
from app.blueprint import ops
from app.builder import chat
from app.engine import automations
from app.engine.records import Engine, FieldErrors
from app.extensions import db
from app.models import Approval, Job, Membership, Message, Run, User

import pytest


def _person(ws, email, name, role, teams=None):
    user = User(email=email, name=name)
    user.set_password("password1234")
    db.session.add(user)
    db.session.flush()
    db.session.add(Membership(workspace_id=ws.id, user_id=user.id, role=role, teams=teams))
    db.session.commit()
    return user


def _as(ws, user):
    return Engine(ws, by=user.name, member=Membership.query.filter_by(workspace_id=ws.id, user_id=user.id).first())


def _built(ws, design):
    result = ops.apply(ws.blueprint, design)
    assert not result.problems, result.problems
    chat.save_version(ws, result.blueprint, result.changes, "chat")
    return result


# The netball club's expenses: new ones start at Pending, and the committee's approval was asked for only when one was
# "moved to Pending" (so never), with nothing after it to say it was approved; an Approve button let anyone on the
# committee skip the approval, and the New form's main button pressed it.
EXPENSES = [
    {"op": "create_table", "table": "expenses", "label": "Expenses", "fields": [
        {"key": "description", "type": "text", "required": True}, {"key": "amount", "type": "money", "required": True},
        {"key": "status", "type": "select", "default": "Pending", "options": ["Pending", "Approved", "Paid", "Rejected"]},
        {"key": "approved_by", "label": "Approved by", "type": "user"}, {"key": "approved_date", "label": "Approved date", "type": "date"}],
     "workflow": {"field": "status", "transitions": [
         {"from": ["Pending"], "to": "Approved", "label": "Approve", "who": ["admin"],
          "only_if": [{"field": "amount", "op": "greater_than", "value": 500}]},
         {"from": ["Pending"], "to": "Paid", "label": "Mark as paid", "who": ["admin"],
          "only_if": [{"field": "amount", "op": "at_most", "value": 500}]},
         {"from": ["Pending"], "to": "Rejected", "label": "Reject", "who": ["admin"]},
         {"from": ["Approved"], "to": "Paid", "label": "Mark as paid", "who": ["admin"]}]}},
    {"op": "add_automation", "automation": {
        "key": "expense_approval", "name": "Committee approval for expenses over $500",
        "trigger": {"type": "record_updated", "table": "expenses"},
        "conditions": [{"field": "status", "op": "changed_to", "value": "Pending"}, {"field": "amount", "op": "greater_than", "value": 500}],
        "actions": [{"type": "request_approval", "approver": "admin", "message": "Please approve this.", "not_by": ["creator"],
                     "if_rejected": [{"type": "notify", "message": "Expense {{record.description}} was rejected.", "to": "admin"}]}]}},
]


def test_an_approval_moves_the_record_on_and_says_who_approved(app, owner):
    user, ws = owner
    result = _built(ws, EXPENSES)
    said = " ".join(result.changes)
    rule = ws.blueprint["automations"][0]
    assert rule["trigger"]["type"] == "record_created_or_updated" and "now also starts when a new expense is added" in said
    approval = rule["actions"][0]
    assert approval["if_rejected"][0] == {"type": "update_record", "values": {"status": "Rejected"}}
    assert rule["actions"][1] == {"type": "update_record", "values": {"status": "Approved", "approved_by": "{{approver}}",
                                                                      "approved_date": "{{today()}}"}}
    assert "moves an expense to Approved once it's approved, filling in Approved by and Approved date" in said
    fields = {f["key"]: f for f in ws.blueprint["tables"][0]["fields"]}
    assert fields["approved_by"].get("readonly") and fields["approved_date"].get("readonly")  # filled in by the approval, not typed

    sam = _person(ws, "sam@example.com", "Sam", "admin")
    grace = _as(ws, user)
    balls = grace.create("expenses", {"description": "New balls", "amount": 820})
    ask = Approval.query.one()
    assert ask.status == "pending" and "New balls" in ask.message  # which expense, not just "Please approve this."
    approve = next(i for i, m, _ in grace.moves(Engine(ws).get(balls.id)) if m["label"] == "Approve")
    with pytest.raises(FieldErrors, match="you asked for it, so someone else has to"):  # the owner raised it: not theirs to approve
        grace.move(Engine(ws).get(balls.id), approve)
    _as(ws, sam).move(Engine(ws).get(balls.id), approve)  # the record's Approve button is the approval
    done = Engine(ws).get(balls.id)
    assert done.data["status"] == "Approved" and done.data["approved_by"] == sam.id
    assert done.data["approved_date"] == Engine(ws).today.isoformat()
    assert db.session.get(Approval, ask.id).status == "approved" and Run.query.one().status == "done"

    cheap = grace.create("expenses", {"description": "Chalk", "amount": 40})  # under $500: nobody's asked
    assert Approval.query.count() == 1 and Engine(ws).get(cheap.id).data["status"] == "Pending"


def test_a_no_on_the_record_is_the_approvers_no_and_a_cancel_withdraws_it(app, owner):
    user, ws = owner
    _built(ws, EXPENSES + [{"op": "update_table", "table": "expenses", "workflow": {"field": "status", "transitions": [
        {"from": ["Pending"], "to": "Approved", "label": "Approve", "who": ["admin"]},
        {"from": ["Pending"], "to": "Rejected", "label": "Reject", "who": ["admin"]},
        {"from": ["Pending"], "to": "Cancelled", "label": "Cancel", "who": ["creator"]},
        {"from": ["Approved"], "to": "Paid", "label": "Mark as paid", "who": ["admin"]}]}}])
    sam = _person(ws, "sam@example.com", "Sam", "admin")
    jo = _person(ws, "jo@example.com", "Jo", "editor")
    first = _as(ws, jo).create("expenses", {"description": "Bus hire", "amount": 900})
    reject = next(i for i, m, _ in _as(ws, sam).moves(Engine(ws).get(first.id)) if m["label"] == "Reject")
    _as(ws, sam).move(Engine(ws).get(first.id), reject)
    assert Engine(ws).get(first.id).data["status"] == "Rejected" and Approval.query.one().status == "rejected"

    second = _as(ws, jo).create("expenses", {"description": "Hall hire", "amount": 700})
    onward = {m["label"]: problem for _, m, problem in _as(ws, jo).moves(Engine(ws).get(second.id))}
    assert onward["Cancel"] is None
    cancel = next(i for i, m, _ in _as(ws, jo).moves(Engine(ws).get(second.id)) if m["label"] == "Cancel")
    _as(ws, jo).move(Engine(ws).get(second.id), cancel)
    ask = Approval.query.filter_by(record_id=second.id).one()
    assert ask.status == "cancelled" and not Engine(ws).waiting_for(Engine(ws).get(second.id))
    run = Run.query.filter_by(record_id=second.id).one()
    automations.execute(Engine(ws), run)
    assert run.status == "rejected" and "Withdrawn" in run.error  # the rule stops: nobody is left waiting to be asked


def test_an_approval_with_nothing_after_it_still_moves_the_record_on(app, owner):
    """The dental clinic's refunds and the contractor's purchase requests: the practice manager (a team) approved, and
    the refund stayed Pending, so she had to press Approve again, and Approved by stayed empty."""
    user, ws = owner
    _built(ws, [
        {"op": "set_team", "team": {"key": "pm", "name": "Practice manager"}},
        {"op": "create_table", "table": "refunds", "label": "Refunds", "fields": [
            {"key": "reason", "type": "text", "required": True}, {"key": "amount", "type": "money", "required": True},
            {"key": "status", "type": "select", "default": "Pending", "options": ["Pending", "Approved", "Rejected", "Processed"]},
            {"key": "approved_by", "label": "Approved by", "type": "user"}],
         "workflow": {"field": "status", "transitions": [
             {"from": ["Pending"], "to": "Approved", "label": "Approve refund", "who": ["pm"]},
             {"from": ["Pending"], "to": "Rejected", "label": "Reject refund", "who": ["pm"]},
             {"from": ["Approved"], "to": "Processed", "label": "Process refund", "who": ["editor"]}]}},
        {"op": "add_automation", "automation": {
            "key": "refund_approval", "name": "Approve refunds over $200", "trigger": {"type": "record_created", "table": "refunds"},
            "conditions": [{"field": "amount", "op": "greater_than", "value": 200}],
            "actions": [{"type": "request_approval", "approver": "pm", "message": "Please approve this."},
                        {"type": "notify", "message": "Refund {{record.reason}} needs approval", "to": "everyone"}]}}])
    assert ws.blueprint["automations"][0]["actions"][1]["values"] == {"status": "Approved", "approved_by": "{{approver}}"}
    sarah = _person(ws, "sarah@example.com", "Sarah", "editor", teams=["pm"])
    jess = _person(ws, "jess@example.com", "Jess", "editor")
    refund = _as(ws, jess).create("refunds", {"reason": "Bridge deposit", "amount": 280})
    with pytest.raises(FieldErrors):  # "Approved by" isn't hers to fill in
        _as(ws, jess).update(Engine(ws).get(refund.id), {"approved_by": sarah.id})
    ask = Approval.query.one()
    automations.decide(Engine(ws), ask, True, "Sarah", by_id=sarah.id)  # Approve in her To do
    done = Engine(ws).get(refund.id)
    assert done.data["status"] == "Approved" and done.data["approved_by"] == sarah.id


def test_the_new_form_never_makes_someone_elses_decision(app, owner, client):
    """A new expense's main button was "Approve" (then "Save and send on", which approved it); a new safety incident's was
    "Close incident". The form saves and sends on; deciding and closing are for later, by whoever does them."""
    user, ws = owner
    _built(ws, EXPENSES + [
        {"op": "create_table", "table": "requests", "label": "Requests", "fields": [
            {"key": "item", "type": "text", "required": True},
            {"key": "status", "type": "select", "options": ["Draft", "Submitted", "Approved", "Rejected"]}],
         "workflow": {"field": "status", "transitions": [
             {"from": ["Draft"], "to": "Submitted", "label": "Send for approval", "who": ["creator"]},
             {"from": ["Submitted"], "to": "Approved", "label": "Approve", "who": ["admin"]},
             {"from": ["Submitted"], "to": "Rejected", "label": "Reject", "who": ["admin"]}]}},
        {"op": "create_table", "table": "incidents", "label": "Incidents", "fields": [
            {"key": "what_happened", "type": "text", "required": True},
            {"key": "status", "type": "select", "options": ["Open", "Closed"]}],
         "workflow": {"field": "status", "transitions": [{"from": ["Open"], "to": "Closed", "label": "Close incident"}]}}])
    client.post("/login", data={"email": "ethan@example.com", "password": "password1234"})
    form = client.get(f"/w/{ws.id}/app/t/expenses/new").get_data(as_text=True)
    assert 'name="then"' not in form and ">Approve<" not in form
    form = client.get(f"/w/{ws.id}/app/t/incidents/new").get_data(as_text=True)
    assert 'name="then"' not in form and "Close incident" not in form
    form = client.get(f"/w/{ws.id}/app/t/requests/new").get_data(as_text=True)
    assert 'name="then"' in form and "Send for approval" in form  # sending on is still one click
    client.post(f"/w/{ws.id}/app/t/expenses/new", data={"description": "Bibs", "amount": "640", "then": "-1"})
    assert Engine(ws).rows("expenses")[-1].data["status"] == "Pending" and Approval.query.count() == 1


def test_the_ai_is_never_asked_while_unsaved_writes_hold_the_database(app, owner):
    """The database takes one writer at a time: a builder turn that had written something and then waited minutes for the
    AI kept everyone else from saving ("database is locked"), so a check and a question to Ask failed half way."""
    user, ws = owner
    ai.set_backend(lambda messages, schema_, on_progress: ("{}", ""))
    ws.name = "Renamed"
    db.session.flush()
    assert ai._holding_writes()
    with pytest.raises(AssertionError, match="commit first"):  # tests fail loudly; in use it's saved first and logged
        ai.chat([{"role": "user", "content": "hi"}])
    db.session.commit()
    assert not ai._holding_writes() and ai.chat([{"role": "user", "content": "hi"}])[0] == "{}"


def test_work_left_half_done_goes_back_in_the_queue(app, owner):
    """A failure while the database was busy left a check "running" and a question "Working out what to look up…" for
    over an hour, and no new question could be asked. Work nobody is doing goes back in the queue, and is given up on
    (saying so) if it keeps stopping."""
    user, ws = owner
    question = Message(workspace_id=ws.id, channel="assistant", user_id=user.id, role="assistant", status="thinking",
                       started_at=db.func.now(), progress=[])
    job = Job(kind="check_add", workspace_id=ws.id, status="running", attempts=1, payload={})
    tired = Job(kind="check_add", workspace_id=ws.id, status="running", attempts=3, payload={})
    busy = Job(kind="check", workspace_id=ws.id, status="running", attempts=1, payload={})
    db.session.add_all([question, job, tired, busy])
    db.session.commit()
    worker._working["jobs"] = ("job", busy.id)
    try:
        worker.unstick()
    finally:
        worker._working.clear()
    assert question.status == "queued" and job.status == "queued"
    assert tired.status == "failed" and "given up" in tired.error and busy.status == "running"


def test_the_try_out_also_fills_in_only_what_is_required(app, owner):
    """Accepting a treatment plan made an empty $0 invoice ("Description is required") in real use, while the try-out,
    which filled in every field of every line, said it worked: people pick a procedure and leave the rest empty."""
    from app.builder import trial
    user, ws = owner
    _built(ws, [
        {"op": "create_table", "table": "plans", "label": "Treatment plans", "fields": [
            {"key": "patient", "type": "text", "required": True},
            {"key": "status", "type": "select", "options": ["Proposed", "Accepted"]}],
         "workflow": {"field": "status", "transitions": [{"from": ["Proposed"], "to": "Accepted", "label": "Accept"}]}},
        {"op": "create_table", "table": "plan_items", "label": "Plan items", "embedded": True, "fields": [
            {"key": "plan", "type": "link", "table": "plans", "required": True},
            {"key": "procedure", "type": "select", "options": ["Check-up", "Filling"], "required": True},
            {"key": "description", "type": "text"}, {"key": "price", "type": "money"}]},
        {"op": "create_table", "table": "invoices", "label": "Invoices", "fields": [{"key": "patient", "type": "text"}]},
        {"op": "create_table", "table": "invoice_lines", "label": "Invoice lines", "embedded": True, "fields": [
            {"key": "invoice", "type": "link", "table": "invoices", "required": True},
            {"key": "description", "type": "text", "required": True}, {"key": "price", "type": "money"}]},
        {"op": "add_automation", "automation": {
            "key": "invoice_plan", "name": "Invoice an accepted plan", "trigger": {"type": "record_updated", "table": "plans"},
            "conditions": [{"field": "status", "op": "changed_to", "value": "Accepted"}],
            "actions": [{"type": "create_record", "table": "invoices", "values": {"patient": "{{record.patient}}"}},
                        {"type": "create_for_each", "table": "invoice_lines", "source": {"table": "plan_items", "via": "plan"},
                         "values": {"invoice": "{{created}}", "description": "{{item.description}}", "price": "{{item.price}}"}}]}}])
    walked, _ = trial.try_out(ws, screens=False)
    plain = [i["plain"] for i in walked["issues"]]
    assert any("“Invoice an accepted plan” adds invoice lines without their description" in p for p in plain), plain  # added to finish


def test_a_rule_that_stops_half_way_leaves_nothing_half_made(app, owner):
    """Each accepted plan left an empty $0 invoice behind when its lines couldn't be made: it goes in the bin, and the
    owner is told so. (A line whose must-have it copies was left empty is added for people to finish, not a stop.)"""
    user, ws = owner
    _built(ws, [
        {"op": "create_table", "table": "jobs", "label": "Jobs", "fields": [
            {"key": "title", "type": "text", "required": True}, {"key": "notes", "type": "text"},
            {"key": "status", "type": "select", "options": ["Open", "Won"]}],
         "workflow": {"field": "status", "transitions": [{"from": ["Open"], "to": "Won", "label": "Mark as won"}]}},
        {"op": "create_table", "table": "invoices", "label": "Invoices", "fields": [{"key": "job", "type": "text"}]},
        {"op": "create_table", "table": "invoice_lines", "label": "Invoice lines", "embedded": True, "fields": [
            {"key": "invoice", "type": "link", "table": "invoices", "required": True},
            {"key": "description", "type": "text", "required": True}, {"key": "hours", "type": "number"}]},
        {"op": "add_automation", "automation": {
            "key": "bill", "name": "Invoice a won job", "trigger": {"type": "record_updated", "table": "jobs"},
            "conditions": [{"field": "status", "op": "changed_to", "value": "Won"}],
            "actions": [{"type": "create_record", "table": "invoices", "values": {"job": "{{record.title}}"}},
                        {"type": "create_record", "table": "invoice_lines", "values": {
                            "invoice": "{{created}}", "description": "{{record.notes}}", "hours": "{{record.title}}"}}]}}])
    e = Engine(ws, by="Ethan")
    job = e.create("jobs", {"title": "Rewire"})
    e.move(Engine(ws).get(job.id), 0)
    run = Run.query.one()
    assert run.status == "failed" and "was put in the bin" in run.error, run.error
    assert Engine(ws).rows("invoices") == []
    later = e.create("jobs", {"title": "12"})  # hours it can read; the description it copies left empty: for people to finish
    e.move(Engine(ws).get(later.id), 0)
    assert len(Engine(ws).rows("invoices")) == 1 and len(Engine(ws).rows("invoice_lines")) == 1


def test_a_notification_opens_the_page_it_is_about(app, owner, client):
    """“Something not right here?” told the owner, and the notification opened /w/1/app/None: “We can't find this page”."""
    user, ws = owner
    client.post("/login", data={"email": "ethan@example.com", "password": "password1234"})
    client.post(f"/w/{ws.id}/app/feedback", data={"text": "Parents can't be added as volunteers", "page": f"/w/{ws.id}/app/"})
    page = client.get(f"/w/{ws.id}/app/notifications").get_data(as_text=True)
    assert f'href="/w/{ws.id}/app/improvements#said"' in page and "/app/None" not in page
    assert chat.navigate_url(ws, f"/w/{ws.id}/app/checks") == f"/w/{ws.id}/app/checks" and chat.navigate_url(ws, "//evil.example") is None


def test_what_people_entered_is_kept_when_a_field_changes(app, owner):
    """A fix that added a Volunteers list wiped the volunteers off every canteen shift (their member ids would now have
    named other volunteers, or nobody); another made an invoice's customer something picked rather than looked up, and
    every invoice's customer went blank."""
    user, ws = owner
    _built(ws, [
        {"op": "create_table", "table": "members", "label": "Members", "fields": [
            {"key": "first_name", "type": "text", "required": True}, {"key": "last_name", "type": "text"},
            {"key": "email", "type": "email"}]},
        {"op": "create_table", "table": "shifts", "label": "Canteen shifts", "fields": [
            {"key": "day", "type": "text", "required": True}, {"key": "volunteer", "type": "link", "table": "members"}]},
        {"op": "create_table", "table": "customers", "label": "Customers", "fields": [{"key": "name", "type": "text", "required": True}]},
        {"op": "create_table", "table": "jobs", "label": "Jobs", "fields": [
            {"key": "title", "type": "text", "required": True}, {"key": "customer", "type": "link", "table": "customers"}]},
        {"op": "create_table", "table": "invoices", "label": "Invoices", "fields": [
            {"key": "number", "type": "text", "required": True}, {"key": "job", "type": "link", "table": "jobs"},
            {"key": "customer", "type": "lookup", "link": "job", "field": "customer"}]}])
    e = Engine(ws, by="Grace")
    sarah = e.create("members", {"first_name": "Sarah", "last_name": "Thompson", "email": "sarah@example.com"})
    shift = e.create("shifts", {"day": "Saturday 9am", "volunteer": sarah.id})
    acme = e.create("customers", {"name": "Acme"})
    invoice = e.create("invoices", {"number": "INV-1", "job": e.create("jobs", {"title": "Paving", "customer": acme.id}).id})
    e.create("customers", {"name": "Someone else"})  # ids line up with nothing on purpose

    result = ops.apply(ws.blueprint, [
        {"op": "create_table", "table": "volunteers", "label": "Volunteers", "fields": [
            {"key": "first_name", "type": "text", "required": True}, {"key": "last_name", "type": "text"}, {"key": "phone", "type": "phone"}]},
        {"op": "update_field", "table": "shifts", "key": "volunteer", "changes": {"table": "volunteers"}},
        {"op": "update_field", "table": "invoices", "key": "customer", "changes": {"type": "link", "table": "customers"}}])
    assert not result.problems, result.problems
    changes = list(result.changes)
    chat.save_version(ws, result.blueprint, changes, "chat")
    now = Engine(ws)
    kept = now.get(now.get(shift.id).data["volunteer"])
    assert kept.table == "volunteers" and kept.data["first_name"] == "Sarah" and kept.data["last_name"] == "Thompson"
    assert now.get(invoice.id).data["customer"] == acme.id
    said = " ".join(changes)
    assert "Kept Volunteer on 1 canteen shift through the change (added Sarah Thompson to Volunteers" in said, said
    assert "Kept Customer on 1 invoice" in said

    chat.restore(ws, ws.version - 1)  # undoing it puts Sarah the member back on the shift
    assert Engine(ws).get(shift.id).data["volunteer"] == sarah.id


def test_a_quote_over_the_limit_is_never_sent_without_the_owners_ok(app, owner, client):
    """The landscaper's quotes: a stage button "Send to customer" beside the rule's own "Send to customer" (which asks
    the owner over $20,000, and otherwise makes the PDF, emails it and marks it Sent). The New form pressed the stage
    button, so $26,400 quotes went straight out; and an approved quote was never sent, as the approved way stopped there."""
    user, ws = owner
    result = _built(ws, [
        {"op": "create_table", "table": "quotes", "label": "Quotes", "fields": [
            {"key": "customer", "type": "text", "required": True}, {"key": "total", "type": "money"},
            {"key": "status", "type": "select", "default": "Draft", "options": ["Draft", "Sent", "Accepted"]}],
         "workflow": {"field": "status", "transitions": [
             {"from": ["Draft"], "to": "Sent", "label": "Send to customer"},
             {"from": ["Sent"], "to": "Accepted", "label": "Mark as accepted"}]}},
        {"op": "add_automation", "automation": {
            "key": "send_quote", "name": "Send quote to customer", "trigger": {"type": "button", "table": "quotes", "label": "Send to customer"},
            "actions": [{"type": "branch", "paths": [
                {"label": "Over $20k", "when": [{"field": "total", "op": "greater_than", "value": 20000}],
                 "steps": [{"type": "request_approval", "approver": "owner", "message": "Please approve this."}]},
                {"label": "$20k or under", "when": [{"field": "total", "op": "at_most", "value": 20000}],
                 "steps": [{"type": "notify", "message": "Quote for {{record.customer}} sent", "to": "everyone"},
                           {"type": "update_record", "values": {"status": "Sent"}}]}]}]}}])
    said = " ".join(result.changes)
    assert "Quotes now have one “Send to customer” button: the rule's" in said
    assert "“Send quote to customer” now carries on once it's approved (Over $20k)" in said
    quotes = ws.blueprint["tables"][0]
    assert [m["label"] for m in quotes["workflow"]["transitions"]] == ["Mark as accepted"]
    big_way = ws.blueprint["automations"][0]["actions"][0]["paths"][0]["steps"]
    assert [s["type"] for s in big_way] == ["request_approval", "notify", "update_record"]

    client.post("/login", data={"email": "ethan@example.com", "password": "password1234"})
    assert 'name="then"' not in client.get(f"/w/{ws.id}/app/t/quotes/new").get_data(as_text=True)
    e = Engine(ws, by="Dave")
    big = e.create("quotes", {"customer": "Harbour Homes", "total": 26400})
    automations.press(e, ws.blueprint["automations"][0], Engine(ws).get(big.id), "Dave")
    assert Engine(ws).get(big.id).data["status"] == "Draft"  # waiting for the owner's OK
    automations.decide(Engine(ws), Approval.query.one(), True, "Ethan", by_id=user.id)
    assert Engine(ws).get(big.id).data["status"] == "Sent"  # and once approved, it's sent as a small one is


def test_people_go_by_their_whole_name(app, owner):
    """Members and patients were titled by first name only ("Emma"): two Emmas would be mixed up."""
    user, ws = owner
    result = _built(ws, [{"op": "create_table", "table": "patients", "label": "Patients", "fields": [
        {"key": "first_name", "type": "text", "required": True}, {"key": "last_name", "type": "text", "required": True},
        {"key": "email", "type": "email"}]}])
    assert "Patients go by their whole name" in " ".join(result.changes)
    e = Engine(ws, by="Priya")
    emma = e.create("patients", {"first_name": "Emma", "last_name": "Walsh"})
    assert Engine(ws).title(Engine(ws).get(emma.id)) == "Emma Walsh"
    again = ops.apply(ws.blueprint, [{"op": "update_field", "table": "patients", "key": "email", "changes": {"label": "Email address"}}])
    assert not any("whole name" in c for c in again.changes)  # once


def test_a_30_minute_appointment_is_30_minutes(app, owner):
    """Appointments defaulted to “30h”: the builder said 30 with minutes as the unit, and durations are kept in hours."""
    user, ws = owner
    _built(ws, [{"op": "create_table", "table": "appointments", "label": "Appointments", "fields": [
        {"key": "patient", "type": "text", "required": True},
        {"key": "duration", "type": "duration", "default": 30, "unit": "minutes"},
        {"key": "break_length", "type": "duration", "default": "15 minutes"}, {"key": "session", "type": "duration", "default": "1:30"}]}])
    fields = {f["key"]: f for f in ws.blueprint["tables"][0]["fields"]}
    assert fields["duration"]["default"] == 0.5 and "unit" not in fields["duration"]
    assert fields["break_length"]["default"] == 0.25 and fields["session"]["default"] == 1.5
    e = Engine(ws, by="Jess")
    booked = e.create("appointments", {"patient": "Emma"})
    assert e.display(fields["duration"], Engine(ws).get(booked.id).data["duration"]) == "30m"


def test_today_means_today(app, owner):
    """“Today's appointments” and a dentist's “My day” counted the whole week: there was no “today” to filter by."""
    user, ws = owner
    _built(ws, [{"op": "create_table", "table": "appointments", "label": "Appointments", "fields": [
        {"key": "patient", "type": "text", "required": True}, {"key": "start_time", "type": "datetime", "required": True}],
        "views": [{"key": "today", "label": "Today", "type": "list", "filters": [{"field": "start_time", "op": "is_today"}]}]}])
    from datetime import timedelta
    e = Engine(ws, by="Jess")
    now = e.today
    for n, day in enumerate((now, now, now + timedelta(days=1), now - timedelta(days=1))):
        e.create("appointments", {"patient": f"P{n}", "start_time": f"{day.isoformat()} 09:00"})
    view = ws.blueprint["tables"][0]["views"][0]
    assert view["filters"][0]["op"] == "today"
    rows = Engine(ws).rows("appointments")
    assert [r.data["patient"] for r in rows if Engine(ws).matches(r, view["filters"])] == ["P0", "P1"]
    assert [r.data["patient"] for r in rows if Engine(ws).matches(r, [{"field": "start_time", "op": "tomorrow"}])] == ["P2"]
    assert automations.describe_condition(Engine(ws), view["filters"][0], "appointments") == "Start time is today"


def test_a_change_to_who_can_see_something_is_said_first(app, owner):
    """Asked to let dentists see clinical notes, the builder opened them to the receptionists too, and said the opposite.
    Whatever the AI says, a change to who can see a list or a field is said first, in plain words."""
    from .conftest import ScriptedAI
    user, ws = owner
    _built(ws, [{"op": "create_table", "table": "notes", "label": "Clinical notes", "access": {"view": "admin", "edit": "admin"},
                 "fields": [{"key": "patient", "type": "text", "required": True}, {"key": "diagnosis", "type": "long_text"}]}])
    from .test_lang import Model
    opened = {"work": [{"need": 1, "patches": [{"do": "set_access", "list": "notes", "view": "editor", "edit": "editor"}]}],
              "reply": "Dentists can now see and write clinical notes while receptionists cannot."}
    ai.set_backend(Model({"understand": {"needs": [{"said": "All dentists must see clinical notes", "does": "Lets dentists see clinical notes"}],
                                         "reply": "Dentists can now see and write clinical notes while receptionists cannot."},
                          "data": {"lists": ["notes"], "patches": []}, "behaviour": opened}))
    reply = chat.ask(ws, "All dentists must see clinical notes. Receptionists must never see them.")
    chat.work(reply.id)
    m = db.session.get(Message, reply.id)
    assert m.text.startswith("Who can see what has changed: Clinical notes can now be seen by everyone who can do the work "
                             "(before: only people who can change the app)"), m.text


def test_saving_something_you_cant_open_says_so_instead_of_page_not_found(app, owner, client):
    """An electrician saved his timesheet and got “We can't find this page”, and thought it hadn't saved."""
    user, ws = owner
    _built(ws, [{"op": "create_table", "table": "timesheets", "label": "Timesheets", "fields": [
        {"key": "hours", "type": "number", "required": True}, {"key": "staff", "type": "user"}],
        "row_access": {"field": "staff", "roles": ["editor"]}}])
    _person(ws, "jake@example.com", "Jake", "editor")
    client.post("/login", data={"email": "jake@example.com", "password": "password1234"})
    saved = client.post(f"/w/{ws.id}/app/t/timesheets/new", data={"hours": "8", "staff": str(user.id)}, follow_redirects=True)
    page = saved.get_data(as_text=True)
    assert saved.status_code == 200 and "is saved" in page and "now it&#39;s saved; whoever looks after timesheets can" in page
    assert "find this page" not in page
    assert Engine(ws).rows("timesheets")[0].data["hours"] == 8


def test_ask_says_what_someone_isnt_allowed_to_see(app, owner):
    """A receptionist asked Ask about a patient's clinical notes and was told there were none."""
    from app import assistant
    user, ws = owner
    _built(ws, [{"op": "create_table", "table": "notes", "label": "Clinical notes", "access": {"view": "admin", "edit": "admin"},
                 "fields": [{"key": "patient", "type": "text", "required": True}]},
                {"op": "create_table", "table": "patients", "label": "Patients", "fields": [
                    {"key": "name", "type": "text", "required": True}, {"key": "medicare", "type": "text", "access": "admin"}]}])
    jess = _person(ws, "jess@example.com", "Jess", "editor")
    kept = assistant._kept_from(_as(ws, jess))
    assert "aren't allowed to see them" in kept and "Clinical notes" in kept and "Patients’ Medicare" in kept
    assert assistant._kept_from(_as(ws, user)) == ""
