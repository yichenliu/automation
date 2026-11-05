"""The analyst keeps learning: from a business's real use of its ERP it finds what to improve, proves automations on
the business's own history, and admins apply (undoably) or dismiss each suggestion."""
from datetime import timedelta

import pytest

from app import ai, learning
from app.blueprint import ops
from app.builder import chat
from app.engine.records import Engine
from app.extensions import db
from app.models import Message, Notification, Record, Suggestion, utcnow

from .conftest import ScriptedAI, login

DESIGN = [
    {"op": "create_table", "table": "jobs", "label": "Jobs", "fields": [
        {"key": "title", "type": "text", "required": True}, {"key": "region", "type": "text"},
        {"key": "fax", "type": "text"}, {"key": "priority", "type": "select", "options": ["Low", "Normal", "Urgent"]},
        {"key": "status", "type": "select", "options": ["Booked", "In progress", "Done"]},
        {"key": "paid", "type": "select", "options": ["No", "Paid"]}, {"key": "paid_on", "type": "date"}, {"key": "amount", "type": "money"}]},
    {"op": "create_table", "table": "invoices", "label": "Invoices", "fields": [
        {"key": "job", "type": "link", "table": "jobs"}, {"key": "amount", "type": "money"},
        {"key": "status", "type": "select", "options": ["Draft", "Sent"]}]},
]


@pytest.fixture
def busy(app, owner):
    """Two months of a plumbing business using its ERP the long way round."""
    _, ws = owner
    result = ops.apply(ws.blueprint, DESIGN)
    assert not result.problems, result.problems
    chat.save_version(ws, result.blueprint, result.changes, "chat")
    db.session.query(type(ws)).filter_by(id=ws.id).first()
    engine = Engine(ws, by="Ethan")
    regions = ["North", "South", "north", "East"]
    jobs = []
    for n in range(24):
        job = engine.create("jobs", {"title": f"Job {n}", "region": regions[n % 4], "priority": "Normal" if n % 10 else "Urgent", "status": "Booked"})
        jobs.append(job)
    for job in jobs[:12]:  # done jobs: the invoice is made by hand straight after, 10 times out of 12
        engine.update(engine.get(job.id), {"status": "Done", "amount": 300})
        if job.id % 6:
            engine.create("invoices", {"job": job.id, "amount": 300, "status": "Draft"})
    for job in jobs[:8]:  # paid: the paid date is filled in by hand straight after
        engine.update(engine.get(job.id), {"paid": "Paid"})
        engine.update(engine.get(job.id), {"paid_on": engine.today.isoformat()})
    for job in jobs[12:20]:  # left sitting for a month
        r = db.session.get(Record, job.id)
        r.updated_at = utcnow() - timedelta(days=30)
    db.session.commit()
    return ws


def test_the_analyst_learns_from_how_the_erp_is_used(busy):
    fresh = learning.learn(busy, with_ai=False)
    kinds = {s.kind for s in fresh}
    assert {"unused_field", "text_to_select", "default_value", "follow_up", "stuck"} <= kinds
    by = {s.signature: s for s in fresh}
    fax = by["unused:jobs.fax"]
    assert "0 of the 24 jobs" in fax.why
    region = by["choices:jobs.region"]
    assert region.ops[0]["field"]["options"] == ["North", "South", "East"] and "spelt differently" in region.why
    assert all("do" in s.ops[0] for s in fresh)  # every suggestion is written in the Keel language
    priority = by['default:jobs.priority="Normal"']
    assert "21 of the 24" in priority.why or "of the 24" in priority.why
    invoice = by["makes:jobs.status=Done->invoices"]
    assert "by hand within half an hour 10 of 12 times" in invoice.why
    assert invoice.proof["fired"] == 12 and invoice.proof["matched"] == 10
    values = {v["field"]: v for v in invoice.ops[0]["rule"]["steps"][0]["values"]}
    assert values["job"] == {"field": "job", "formula": "record"} and values["amount"] == {"field": "amount", "formula": "record.amount"}
    assert values["status"] == {"field": "status", "to": "Draft"}  # always made as a draft by hand
    paid = by["sets:jobs.paid=Paid->paid_on"]
    assert paid.ops[0]["rule"]["steps"][0]["values"] == [{"field": "paid_on", "formula": "today()"}]
    stuck = by["stuck:jobs"]
    assert "8 jobs have sat" in stuck.why
    assert Notification.query.filter(Notification.text.contains("new suggestion")).count() == 1
    assert learning.learn(busy, with_ai=False) == []  # the same findings aren't suggested twice


def test_applying_and_dismissing_suggestions(busy, client):
    learning.learn(busy, with_ai=False)
    login(client)
    page = client.get(f"/w/{busy.id}/app/improvements").get_data(as_text=True)
    assert "Tested on your history" in page and "Create the invoice automatically" in page
    invoice = Suggestion.query.filter_by(signature="makes:jobs.status=Done->invoices").first()
    client.post(f"/w/{busy.id}/app/improvements/{invoice.id}", data={"decision": "apply"})
    db.session.refresh(busy)
    assert invoice.status == "applied" and any(a["actions"][0]["type"] == "create_record" for a in busy.blueprint["automations"])
    engine = Engine(busy, by="Ethan")
    job = engine.create("jobs", {"title": "New one", "status": "Booked", "amount": 450})
    engine.update(engine.get(job.id), {"status": "Done"})
    made = [r for r in Engine(busy).rows("invoices") if r.data.get("job") == job.id]
    assert len(made) == 1 and made[0].data["amount"] == 450 and made[0].data["status"] == "Draft"
    fax = Suggestion.query.filter_by(signature="unused:jobs.fax").first()
    client.post(f"/w/{busy.id}/app/improvements/{fax.id}", data={"decision": "dismiss"})
    assert fax.status == "dismissed"
    learning.learn(busy, with_ai=False)
    assert Suggestion.query.filter_by(signature="unused:jobs.fax").count() == 1  # not suggested again
    chat.restore(busy, invoice.version_after - 1)  # and an applied one can be undone like any design change
    assert not any(a["actions"][0]["type"] == "create_record" for a in busy.blueprint.get("automations", []))


def test_the_analyst_answers_repeated_questions_and_reviews_the_business(busy):
    for text in ("how much do customers owe us", "how much is owed by customers?", "what do customers owe us now"):
        db.session.add(Message(workspace_id=busy.id, channel="assistant", role="user", text=text))
    db.session.commit()
    widget = {"patches": [{"do": "design_op", "op": {"op": "add_page", "page": {"key": "money", "label": "Money", "widgets": [
        {"type": "metric", "table": "jobs", "aggregate": "sum", "field": "amount", "label": "Owed",
         "filters": [{"field": "paid", "op": "not_equals", "value": "Paid"}]}]}}}], "reply": "Added an Owed number to a Money page."}
    review = {"suggestions": [{"title": "Colour urgent jobs red", "why": "Urgent jobs are mixed in with the rest.",
                               "patches": [{"do": "design_op", "op": {"op": "update_table", "table": "jobs", "colors": [
                                   {"when": [{"field": "priority", "op": "equals", "value": "Urgent"}], "color": "red"}]}}]},
                              {"title": "A broken idea", "why": "x", "patches": [{"do": "add_field", "list": "nowhere",
                                                                                   "field": {"type": "text", "key": "x", "label": "X"}}]}]}
    scripted = ScriptedAI(widget, review)
    ai.set_backend(scripted)
    fresh = learning.learn(busy)
    titles = {s.kind: s for s in fresh}
    assert titles["question"].title.startswith("Answer “") and "asked this 3 times" in titles["question"].why
    assert titles["analyst"].title == "Colour urgent jobs red"
    assert "A broken idea" not in [s.title for s in fresh]  # suggestions that don't fit are never shown
    assert "jobs: 24 records" in scripted.asked[1][-1]["content"].lower() or "Jobs: 24 records" in scripted.asked[1][-1]["content"]
    busy = db.session.get(type(busy), busy.id)
    assert busy.state.get("last_review")
