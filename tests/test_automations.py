"""Automations: started by changes, buttons and schedules; AI steps that wait for the AI; runs that show their steps."""
from datetime import datetime, timedelta

from app import ai
from app.blueprint import ops
from app.builder import chat
from app.engine import automations
from app.engine.records import Engine
from app.extensions import db
from app.models import Notification, Run

from .conftest import ScriptedAI
from .designs import CONSULTANCY


def _built(ws, extra=()):
    result = ops.apply(ws.blueprint, CONSULTANCY + list(extra))
    assert not result.problems, result.problems
    chat.save_version(ws, result.blueprint, result.changes, "chat")
    return Engine(ws, by="Ethan")


def test_finishing_a_project_drafts_its_invoice_and_says_so(app, owner):
    _, ws = owner
    e = _built(ws)
    client = e.create("clients", {"name": "Acme", "payment_terms": 14})
    project = e.create("projects", {"name": "Survey", "client": client.id, "hours": 10, "rate": 150})
    assert not e.started  # created as Planned: the rule is about changing to Done
    e.update(project, {"status": "Done"})
    run = e.started[0]
    assert run.status == "done" and run.name == "Invoice when project done"
    invoice = e.rows("invoices")[0]
    assert invoice.data["project"] == project.id and invoice.data["amount"] == 1500.0
    assert invoice.data["due_date"] == (e.today + timedelta(days=14)).isoformat()
    assert [s["result"] for s in run.steps] == ["Created Invoice “INV-0001 · Survey”", "Notified: “Invoice drafted for Survey”"]
    assert Notification.query.one().text == "Invoice drafted for Survey"
    e.update(e.get(project.id), {"hours": 11})  # still Done, but not *changed to* Done: no second invoice
    assert len(e.rows("invoices")) == 1
    words = automations.describe(e, ws.blueprint["automations"][0])
    assert words["when"] == "When a project is moved to Done" and words["only_if"] == []  # the stage it waits for, said once
    assert words["then"][0].startswith("Create an invoice for this project: Amount its value · Due date ")


def test_a_button_and_a_linked_update(app, owner):
    _, ws = owner
    e = _built(ws, [{"op": "add_automation", "automation": {"name": "Mark paid", "trigger": {"type": "button", "table": "invoices", "label": "Mark paid"},
                                                            "conditions": [{"field": "status", "op": "not_equals", "value": "Paid"}],
                                                            "actions": [{"type": "update_record", "values": {"status": "Paid"}},
                                                                        {"type": "update_record", "target": "project", "values": {"status": "Done"}}]}}])
    project = e.create("projects", {"name": "Survey", "hours": 2, "rate": 100})
    invoice = e.create("invoices", {"project": project.id, "amount": 200})
    automation = next(a for a in ws.blueprint["automations"] if a["key"] == "mark_paid")
    run = automations.press(e, automation, invoice, "Ethan")
    assert run.status == "done"
    assert e.get(invoice.id).data["status"] == "Paid" and e.get(project.id).data["status"] == "Done"
    assert len(e.rows("invoices")) == 2  # the project turning Done drafted its invoice, one automation level down
    assert Run.query.filter_by(automation="invoice_when_project_done").one().depth == 1


def test_a_schedule_runs_once_per_slot_for_matching_records(app, owner):
    _, ws = owner
    e = _built(ws, [{"op": "add_automation", "automation": {"name": "Chase overdue", "trigger": {"type": "schedule", "every": "day", "at": "08:00", "table": "invoices"},
                                                            "conditions": [{"field": "due_date", "op": "before_today"}, {"field": "status", "op": "not_equals", "value": "Paid"}],
                                                            "actions": [{"type": "notify", "message": "{{record.number}} is overdue"}]}}])
    project = e.create("projects", {"name": "Survey"})
    e.create("invoices", {"project": project.id, "amount": 5, "due_date": (e.today - timedelta(days=3)).isoformat()})
    e.create("invoices", {"project": project.id, "amount": 5, "due_date": (e.today + timedelta(days=3)).isoformat()})
    assert automations.due(e) == []  # a new schedule waits for its first time
    ws.state = {"schedules": {"chase_overdue": (e.now - timedelta(days=1, hours=1)).isoformat()}}
    db.session.commit()
    started = automations.due(Engine(ws))
    assert len(started) == 1 and Notification.query.one().text == "INV-0001 is overdue"
    assert automations.due(Engine(ws)) == []  # already ran for this slot
    assert automations._last_slot({"every": "week", "at": "09:30", "weekday": "mon"}, datetime(2026, 11, 4, 12)) == datetime(2026, 11, 2, 9, 30)


def test_an_ai_step_waits_for_the_ai_and_carries_on(app, owner):
    _, ws = owner
    e = _built(ws, [{"op": "add_field", "table": "clients", "field": {"key": "industry", "type": "select", "options": ["Mining", "Retail", "Other"]}},
                    {"op": "add_automation", "automation": {"name": "Classify new clients", "trigger": {"type": "record_created", "table": "clients"},
                                                            "actions": [{"type": "notify", "message": "New client {{record.name}}"},
                                                                        {"type": "ai_fill", "field": "industry", "instructions": "Which industry is this client in?"}]}}])
    ai.set_backend(ScriptedAI(ai.AIUnavailable("the AI at http://127.0.0.1:11434 isn't answering (ConnectTimeout)"), {"value": "Mining"}))
    client = e.create("clients", {"name": "Pilbara Iron Ore"})
    run = e.started[0]
    assert run.status == "queued"  # AI steps happen in the background
    automations.execute(Engine(ws), run)
    run = db.session.get(Run, run.id)
    assert run.status == "waiting_ai" and run.steps[0]["done"] and "isn't answering" in run.error
    automations.execute(Engine(ws), run)
    run = db.session.get(Run, run.id)
    assert run.status == "done" and Notification.query.count() == 1  # the notify step wasn't repeated
    assert Engine(ws).get(client.id).data["industry"] == "Mining"
    assert run.steps[1]["result"] == "The AI set Industry to “Mining”"
