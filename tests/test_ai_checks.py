"""The AI doing the work, and people checking it where it matters: AI steps whose work a person checks every time, when
the AI isn't sure, or never; the check itself (correct it, or turn it down); each step's track record; and the analyst
suggesting less checking once the AI has earned it (or more when it slips)."""
from app import ai, learning, worker
from app.blueprint import ops, schema
from app.builder import chat
from app.engine import automations
from app.engine.records import Engine
from app.extensions import db
from app.models import Approval, Run, Suggestion

from .conftest import ScriptedAI, login


def _work(app):
    """What the background worker would do now (AI steps run there, not in the save)."""
    for _ in range(3):
        worker.automation_tick(app)


def _built(ws, design):
    result = ops.apply(ws.blueprint, design)
    assert not result.problems, result.problems
    chat.save_version(ws, result.blueprint, result.changes, "chat")
    return Engine(ws, by="Ethan")


COMPLAINTS = [{"op": "create_table", "table": "complaints", "label": "Complaints", "fields": [
    {"key": "customer", "type": "text"}, {"key": "message", "type": "long_text"}, {"key": "reply", "type": "long_text"},
    {"key": "category", "type": "select", "options": ["Late delivery", "Damaged", "Billing"]},
    {"key": "status", "type": "select", "options": ["New", "Replied"]}]}]


def test_a_drafted_reply_is_checked_by_a_person_before_it_counts_and_their_correction_is_kept(app, owner, client):
    _, ws = owner
    e = _built(ws, COMPLAINTS + [{"op": "add_automation", "automation": {
        "name": "Draft a reply", "trigger": {"type": "record_created", "table": "complaints"},
        "actions": [{"type": "ai_fill", "field": "reply", "instructions": "Draft a kind reply", "review": "always", "reviewer": "editor"},
                    {"type": "update_record", "values": {"status": "Replied"}}]}}])
    ai.set_backend(ScriptedAI({"value": "Sorry your order was late; we've sent a voucher.", "sure": True, "why": "It says the order came a week late."}))
    complaint = e.create("complaints", {"customer": "Acme", "message": "My order was a week late!", "status": "New"})
    _work(app)
    run = Run.query.one()
    check = Approval.query.one()
    assert run.status == "waiting_approval" and check.kind == "review" and check.fields == ["reply"]
    assert Engine(ws).get(complaint.id).data.get("reply") is None  # not saved until a person says so
    assert check.message == "Check the AI's Reply"

    login(client)
    base = f"/w/{ws.id}/app"
    inbox = client.get(base + "/approvals").get_data(as_text=True)
    assert "Check the AI" in inbox and "It says the order came a week late." in inbox and "we&#39;ve sent a voucher" in inbox
    record_page = client.get(base + f"/t/complaints/{complaint.id}").get_data(as_text=True)
    assert "<b>Waiting for you:</b> Check the AI" in record_page and "Check it" in record_page  # the one thing to do, on the record
    client.post(base + f"/approvals/{check.id}", data={"decision": "done", "reply": "Sorry it was late: here's 20% off your next order."})
    _work(app)  # the run carries on in the background
    record = Engine(ws).get(complaint.id)
    assert record.data["reply"].startswith("Sorry it was late") and record.data["status"] == "Replied"
    assert "reply" not in (record.data.get("_ai") or [])  # the person's words now: the AI won't rewrite them
    run = db.session.get(Run, run.id)
    assert run.status == "done" and run.steps[0]["result"] == "Ethan checked the AI's work: corrected Reply"
    assert automations.track_record(Engine(ws), ws.blueprint["automations"][0]["key"], "0") == {
        "checked": 1, "right": 0, "corrected": 1, "wrong": 0, "unchecked": 0, "share": 0.0, "lower": 0.0}


def test_sure_answers_go_straight_through_and_unsure_ones_go_to_a_person(app, owner, monkeypatch):
    _, ws = owner
    monkeypatch.setattr(automations, "SPOT_CHECK", 0)
    e = _built(ws, COMPLAINTS + [{"op": "add_automation", "automation": {
        "name": "Sort complaints", "trigger": {"type": "record_created", "table": "complaints"},
        "actions": [{"type": "ai_fill", "field": "category", "instructions": "Which kind of complaint is it?", "review": "when unsure"}]}}])
    ai.set_backend(ScriptedAI({"value": "Late delivery", "sure": True, "why": "It says late."},
                              {"value": "Billing", "sure": False, "why": "It might be about a charge, or a refund."}))
    late = e.create("complaints", {"message": "Late again"})
    _work(app)
    assert Engine(ws).get(late.id).data["category"] == "Late delivery" and Run.query.one().status == "done"
    assert Run.query.one().steps[0]["ai"] == "applied" and "(It says late.)" in Run.query.one().steps[0]["result"]
    murky = e.create("complaints", {"message": "Something's wrong with what you took"})
    _work(app)
    check = Approval.query.one()
    assert check.proposal["unsure"] == ["Category"] and Engine(ws).get(murky.id).data.get("category") is None
    automations.complete_review(Engine(ws), check, "Ethan", {"category": "Billing"})
    assert Engine(ws).get(murky.id).data["category"] == "Billing"
    key = ws.blueprint["automations"][0]["key"]
    record = automations.track_record(Engine(ws), key, "0")
    assert (record["checked"], record["right"], record["unchecked"]) == (1, 1, 1)
    monkeypatch.setattr(automations, "SPOT_CHECK", 1)  # every sure answer checked too (normally 1 in 10)
    ai.set_backend(ScriptedAI({"value": "Damaged", "sure": True, "why": "Broken on arrival."}))
    e.create("complaints", {"message": "It arrived broken"})
    _work(app)
    assert Approval.query.filter_by(status="pending").count() == 1


def test_an_order_the_ai_made_from_a_webhook_waits_for_its_check_and_a_no_bins_it(app, owner):
    _, ws = owner
    e = _built(ws, [{"op": "create_table", "table": "orders", "label": "Orders", "fields": [
        {"key": "customer", "type": "text"}, {"key": "total", "type": "money"}, {"key": "confirmed", "type": "checkbox"}]},
        {"op": "add_automation", "automation": {"name": "Shop orders in", "trigger": {"type": "webhook_received"},
                                                "actions": [{"type": "ai_create_record", "table": "orders", "review": "always", "reviewer": "admin"}]}},
        {"op": "add_automation", "automation": {"name": "Confirm new orders", "trigger": {"type": "record_created", "table": "orders"},
                                                "actions": [{"type": "update_record", "values": {"confirmed": True}}]}}])
    shop = ws.blueprint["automations"][0]
    ai.set_backend(ScriptedAI({"customer": "Spam Co", "total": 1, "unsure": ["Customer"]}, {"customer": "Cafe Uno", "total": 240, "unsure": []}))
    automations.start_from(e, shop, "payload", {"junk": True}, "The shop sent an order")
    _work(app)
    junk = Engine(ws).rows("orders")[0]
    assert junk.data.get("confirmed") is None  # its own automations wait for the check
    no = Approval.query.one()
    automations.decide(Engine(ws), no, False, "Ethan", "Not a real order")
    _work(app)
    run = db.session.get(Run, no.run_id)
    assert run.status == "rejected" and "what it made was deleted" in run.error and Engine(ws).rows("orders") == []

    automations.start_from(Engine(ws, by="Ethan"), shop, "payload", {"order": 1}, "The shop sent an order")
    _work(app)
    real = Engine(ws).rows("orders")[0]
    yes = Approval.query.filter_by(status="pending").one()
    automations.complete_review(Engine(ws), yes, "Ethan", {"customer": "Cafe Uno", "total": "240"})
    _work(app)
    assert yes.proposal["outcome"] == "right" and Engine(ws).get(real.id).data["confirmed"] is True  # now it ran


def test_the_ai_matches_a_payment_to_its_invoice(app, owner):
    _, ws = owner
    e = _built(ws, [{"op": "create_table", "table": "invoices", "label": "Invoices", "fields": [
        {"key": "number", "type": "text"}, {"key": "customer", "type": "text"}, {"key": "total", "type": "money"}]},
        {"op": "create_table", "table": "payments", "label": "Payments", "fields": [
            {"key": "reference", "type": "text"}, {"key": "amount", "type": "money"}, {"key": "invoice", "type": "link", "table": "invoices"}]},
        {"op": "add_automation", "automation": {"name": "Match payments", "trigger": {"type": "record_created", "table": "payments"},
                                                "actions": [{"type": "ai_fill", "field": "invoice", "instructions": "Which invoice does this pay?"}]}}])
    e.create("invoices", {"number": "INV-1", "customer": "Acme", "total": 100})
    second = e.create("invoices", {"number": "INV-2", "customer": "Bolt", "total": 250})
    scripted = ScriptedAI({"value": "INV-2", "sure": True, "why": "Bolt paid $250, INV-2's total."})
    ai.set_backend(scripted)
    payment = e.create("payments", {"reference": "BOLT PTY 250", "amount": 250})
    _work(app)
    assert Engine(ws).get(payment.id).data["invoice"] == second.id
    assert "INV-2: Customer Bolt; Total $250.00" in scripted.asked[0][1]["content"]  # it chose from the invoices, told apart


def test_the_analyst_suggests_less_checking_once_the_ai_has_earned_it(app, owner, client):
    _, ws = owner
    e = _built(ws, COMPLAINTS + [{"op": "add_automation", "automation": {
        "key": "sort", "name": "Sort complaints", "trigger": {"type": "record_created", "table": "complaints"},
        "actions": [{"type": "branch", "paths": [{"when": [{"field": "status", "op": "equals", "value": "New"}], "steps": [
            {"type": "ai_fill", "field": "category", "instructions": "Which kind?", "review": "always"}]}]}]}}])
    run = Run(workspace_id=ws.id, automation="sort", name="Sort complaints", trigger="test", status="done", steps=[])
    db.session.add(run)
    db.session.flush()
    for n in range(24):  # people checked it 24 times; it was right 23
        db.session.add(Approval(workspace_id=ws.id, run_id=run.id, kind="review", step="0.0.0", status="done" if n else "rejected",
                                proposal={"outcome": "right"}, message="Check"))
    db.session.commit()
    record = automations.track_record(e, "sort", "0.0.0")
    assert (record["checked"], record["right"], record["wrong"]) == (24, 23, 1) and record["lower"] > 0.8
    learning.TRUST_LOWER = 0.8
    try:
        learning.learn(ws, with_ai=False)
    finally:
        learning.TRUST_LOWER = 0.9
    suggestion = Suggestion.query.filter_by(kind="ai_trust").one()
    assert suggestion.title == "Let the AI work on its own in “Sort complaints” when it's sure"
    learning.apply(ws, suggestion, "Ethan")
    step = schema.step_at(ws.blueprint["automations"][0]["actions"], "0.0.0")
    assert step["review"] == "when_unsure"

    login(client)
    base = f"/w/{ws.id}/app"
    page = client.get(base + "/automations").get_data(as_text=True)
    assert "People checked it 24 times: right 23, wrong 1 (96% right)" in page and "Who checks its work" in page
    client.post(base + "/automations/sort/ai/0.0.0", data={"mode": "always"})
    assert schema.step_at(ws.blueprint["automations"][0]["actions"], "0.0.0")["review"] == "always"
    assert "checks it first" in automations.describe(Engine(ws), ws.blueprint["automations"][0])["flow"][0]["ways"][0]["steps"][0]["text"]


def test_the_builder_is_told_when_the_ai_works_alone_and_when_a_person_checks(app):
    from app.lang import model as m, registry
    said = registry.catalog(("step",))
    assert "review?" in said and "whether a person checks its work first (never, when_unsure, always)" in said
    assert all(set(cls.model_fields["review"].annotation.__args__) == set(schema.REVIEW_MODES) for cls in (m.AiFill, m.AiExtract, m.AiCreate, m.AiCheck))
    assert schema.normalize_action({"type": "ai_fill", "field": "x", "instructions": "y", "review": True})["review"] == "always"
