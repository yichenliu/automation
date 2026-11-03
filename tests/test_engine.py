"""The engine: designs applied from loose AI output, records cleaned, formulas, totals, numbering and history."""
from datetime import date, timedelta

import pytest

from app.blueprint import ops
from app.builder import chat
from app.engine.records import Engine, FieldErrors
from app.models import RecordChange

from .designs import CONSULTANCY


def _built(ws):
    result = ops.apply(ws.blueprint, CONSULTANCY)
    assert not result.problems, result.problems
    chat.save_version(ws, result.blueprint, result.changes, "chat")
    return Engine(ws, by="Ethan")


def test_a_loosely_written_design_becomes_a_clean_blueprint(app, owner):
    _, ws = owner
    result = ops.apply(ws.blueprint, CONSULTANCY)
    assert not result.problems
    bp = result.blueprint
    clients = next(t for t in bp["tables"] if t["key"] == "clients")
    assert [f["key"] for f in clients["fields"]] == ["name", "status", "email", "payment_terms", "projects"]  # "id" dropped
    assert {f["key"]: f["type"] for f in clients["fields"]}["email"] == "email"
    assert [v["type"] for v in clients["views"]] == ["list", "board"]
    automation = bp["automations"][0]
    assert automation["trigger"] == {"type": "record_updated", "table": "projects"}
    assert automation["conditions"] == [{"field": "status", "op": "changed_to", "value": "Done"}]
    assert [m["key"] for m in bp["modules"]] == ["sales", "projects", "finance"]
    assert "Created the table Clients" in result.changes[0]


def test_problems_are_explained_and_nothing_changes(app, owner):
    _, ws = owner
    bad = ops.apply(ws.blueprint, [{"op": "create_table", "table": "jobs", "fields": [
        {"key": "customer", "type": "link", "table": "customers"}, {"key": "x", "type": "formula", "expression": "hours * 2"}]}])
    assert bad.blueprint == ws.blueprint
    assert any("customers" in p for p in bad.problems) and any("hours" in p for p in bad.problems)


def test_records_are_cleaned_calculated_numbered_and_kept_in_history(app, owner):
    _, ws = owner
    e = _built(ws)
    acme = e.create("clients", {"name": "Acme Pty Ltd", "status": "active", "email": "ap@acme.com.au", "payment_terms": "14"})
    assert acme.data["status"] == "Active" and acme.data["payment_terms"] == 14
    with pytest.raises(FieldErrors) as bad:
        e.create("clients", {"status": "Unknown", "email": "nope"})
    assert set(bad.value.errors) == {"name", "status", "email"}

    project = e.create("projects", {"name": "Pit survey", "client": "acme", "hours": "12.5", "rate": "$180"})
    assert project.data["client"] == acme.id and project.data["status"] == "Planned"
    assert e.value(project, "value") == 2250.0 and e.display(e.field("projects", "value"), 2250.0) == "$2,250.00"
    assert e.value(project, "client_terms") == 14
    assert e.value(e.get(acme.id), "projects") == 1

    first = e.create("invoices", {"project": project.id, "amount": 1000, "due_date": (e.today - timedelta(days=5)).isoformat()})
    second = e.create("invoices", {"project": "Pit survey", "amount": "250.50", "due_date": "30/11/2099"})
    assert (first.data["number"], second.data["number"]) == ("INV-0001", "INV-0002")
    assert second.data["due_date"] == "2099-11-30"
    assert e.value(first, "days_overdue") == 5 and e.value(second, "days_overdue") == 0
    assert e.value(e.get(project.id), "invoiced") == 1250.5

    e.update(first, {"status": "Paid"})
    assert e.value(e.get(first.id), "days_overdue") == 0
    changes = RecordChange.query.filter_by(record_id=first.id).order_by(RecordChange.id).all()
    assert [c.kind for c in changes] == ["created", "updated"] and changes[1].changes == {"status": ["Draft", "Paid"]}

    overdue = e.query("invoices", [{"field": "due_date", "op": "before_today"}, {"field": "status", "op": "not_equals", "value": "Paid"}])
    assert overdue == []
    assert [r.id for r in e.query("invoices", sort={"field": "amount", "dir": "desc"})] == [first.id, second.id]
    assert e.group(e.rows("invoices"), "status") == [("Draft", 1), ("Paid", 1)]


def test_removing_a_field_keeps_its_values_and_going_back_restores_them(app, owner):
    _, ws = owner
    e = _built(ws)
    acme = e.create("clients", {"name": "Acme", "email": "a@acme.com"})
    before = ws.version
    result = ops.apply(ws.blueprint, [{"op": "remove_field", "table": "clients", "key": "email"}])
    chat.save_version(ws, result.blueprint, result.changes, "chat")
    assert Engine(ws).value(acme, "email") is None
    chat.restore(ws, before)
    assert Engine(ws).value(Engine(ws).get(acme.id), "email") == "a@acme.com"


def test_a_new_autonumber_numbers_existing_records(app, owner):
    _, ws = owner
    e = _built(ws)
    a, b = e.create("clients", {"name": "A"}), e.create("clients", {"name": "B"})
    result = ops.apply(ws.blueprint, [{"op": "add_field", "table": "clients", "field": {"key": "code", "type": "autonumber", "prefix": "C"}}])
    chat.save_version(ws, result.blueprint, result.changes, "chat")
    e = Engine(ws)
    assert [e.get(a.id).data["code"], e.get(b.id).data["code"]] == ["C0001", "C0002"]
    assert e.create("clients", {"name": "C"}).data["code"] == "C0003"
