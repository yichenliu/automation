"""The third round of the kit, from what real businesses asked the analyst for: period totals (hours this week), filters
through a link and on "me" (a rep's own sales), a work order that explodes its product's bill of materials, a job that
becomes an invoice whose PDF is emailed, and an owner statement of rent less expenses across the owner's properties."""
from datetime import date, timedelta

import pytest

from app.blueprint import ops, schema
from app.builder import chat, prompts
from app.documents import build, produce
from app.engine import automations
from app.engine.records import Engine
from app.extensions import db
from app.models import File, Membership, User


def _built(ws, design):
    result = ops.apply(ws.blueprint, design)
    assert not result.problems, result.problems
    chat.save_version(ws, result.blueprint, result.changes, "chat")
    return Engine(ws, by="Ethan")


def test_periods_links_and_me_in_filters(app, owner):
    _, ws = owner
    e = _built(ws, [
        {"op": "create_table", "table": "consultants", "label": "Consultants", "fields": [
            {"key": "name", "type": "text"}, {"key": "login", "type": "user"},
            {"key": "hours_this_week", "type": "rollup", "table": "timesheets", "via": "consultant", "aggregate": "sum",
             "field": "hours", "filters": [{"field": "date", "op": "this_week"}]}]},
        {"op": "create_table", "table": "timesheets", "label": "Timesheets", "fields": [
            {"key": "consultant", "type": "link", "table": "consultants"}, {"key": "date", "type": "date"}, {"key": "hours", "type": "number"}]},
        {"op": "create_table", "table": "sales", "label": "Sales", "fields": [
            {"key": "consultant", "type": "link", "table": "consultants"}, {"key": "amount", "type": "money"}]},
        {"op": "add_page", "page": {"key": "mine", "label": "My sales", "widgets": [
            {"type": "metric", "table": "sales", "aggregate": "sum", "field": "amount",
             "filters": [{"field": "consultant.login", "op": "equals", "value": "me"}]}]}},
    ])
    ava = User(email="ava@example.com", name="Ava")
    ava.set_password("password1234")
    db.session.add(ava)
    db.session.flush()
    db.session.add(Membership(workspace_id=ws.id, user_id=ava.id, role="editor"))
    db.session.commit()
    hers, his = e.create("consultants", {"name": "Ava", "login": ava.id}), e.create("consultants", {"name": "Bo"})
    monday = e.today - timedelta(days=e.today.weekday())
    e.create("timesheets", {"consultant": hers.id, "date": monday.isoformat(), "hours": 7})
    e.create("timesheets", {"consultant": hers.id, "date": (monday - timedelta(days=1)).isoformat(), "hours": 8})  # last Sunday
    assert Engine(ws).value(Engine(ws).get(hers.id), "hours_this_week") == 7
    e.create("sales", {"consultant": hers.id, "amount": 500})
    e.create("sales", {"consultant": his.id, "amount": 900})
    as_ava = Engine(ws, member=Membership.query.filter_by(user_id=ava.id).one())
    mine = as_ava.query("sales", [{"field": "consultant.login", "op": "equals", "value": "me"}])
    assert [r.data["amount"] for r in mine] == [500]
    assert Engine(ws).query("sales", [{"field": "consultant.login", "op": "equals", "value": "me"}]) == []  # the platform is nobody
    assert schema.period("last_month", date(2026, 1, 15)) == (date(2025, 12, 1), date(2025, 12, 31))
    assert schema.period("this_week", date(2026, 11, 1)) == (date(2026, 10, 26), date(2026, 11, 1))  # a Sunday: the week began Monday
    result = ops.apply(ws.blueprint, [{"op": "add_view", "table": "sales", "view": {"key": "x", "type": "list",
                                                                                   "filters": [{"field": "consultant.nope", "op": "equals", "value": 1}]}}])
    assert any("“nope” isn't a field of Consultants" in p for p in result.problems)
    trigger = list(prompts._TRIGGER["properties"])
    assert trigger.index("every") < trigger.index("at") < trigger.index("table")  # the AI writes a schedule's timing first


def test_a_work_order_explodes_its_products_bill_of_materials(app, owner):
    _, ws = owner
    e = _built(ws, [
        {"op": "create_table", "table": "materials", "label": "Materials", "fields": [{"key": "name", "type": "text"}]},
        {"op": "create_table", "table": "products", "label": "Products", "fields": [{"key": "name", "type": "text"}]},
        {"op": "create_table", "table": "bom_lines", "label": "Bill of materials", "embedded": True, "fields": [
            {"key": "product", "type": "link", "table": "products"}, {"key": "material", "type": "link", "table": "materials"},
            {"key": "quantity", "type": "number"}]},
        {"op": "create_table", "table": "work_orders", "label": "Work orders", "fields": [
            {"key": "product", "type": "link", "table": "products"}, {"key": "quantity", "type": "number"}]},
        {"op": "create_table", "table": "needs", "label": "Work order materials", "embedded": True, "fields": [
            {"key": "work_order", "type": "link", "table": "work_orders"}, {"key": "material", "type": "link", "table": "materials"},
            {"key": "required", "type": "number"}]},
        # as the AI writes it: the source's link reaches the product, not the work order; {{created}} before anything is
        # created; the multiplication split across two {{ }}
        {"op": "add_automation", "automation": {"name": "Materials for the work order", "trigger": {"type": "record_created", "table": "work_orders"},
                                                "actions": [{"type": "create_for_each", "source": {"table": "bom_lines", "via": "product"}, "table": "needs",
                                                             "values": {"work_order": "{{created}}", "material": "{{item.material}}",
                                                                        "required": "{{item.quantity}} * {{created.quantity}}"}}]}},
    ])
    timber, screws = e.create("materials", {"name": "Oak"}), e.create("materials", {"name": "Screws"})
    table = e.create("products", {"name": "Dining table"})
    e.create("bom_lines", {"product": table.id, "material": timber.id, "quantity": 4})
    e.create("bom_lines", {"product": table.id, "material": screws.id, "quantity": 24})
    order = e.create("work_orders", {"product": table.id, "quantity": 3})
    assert e.started[0].status == "done", e.started[0].error
    needs = {r.data["material"]: r.data["required"] for r in Engine(ws).rows("needs") if r.data["work_order"] == order.id}
    assert needs == {timber.id: 12, screws.id: 72}
    automation = ws.blueprint["automations"][0]
    assert automations.describe(e, automation)["then"][0].startswith("For each of its Product's bill of materials, create")


def test_a_cabinets_work_order_takes_in_its_drawer_units_own_materials(app, owner):
    _, ws = owner
    e = _built(ws, [
        {"op": "create_table", "table": "materials", "label": "Materials", "fields": [{"key": "name", "type": "text"}]},
        {"op": "create_table", "table": "products", "label": "Products", "fields": [{"key": "name", "type": "text"}]},
        {"op": "create_table", "table": "bom_lines", "label": "Bill of materials", "embedded": True, "fields": [
            {"key": "product", "type": "link", "table": "products"}, {"key": "material", "type": "link", "table": "materials"},
            {"key": "component", "type": "link", "table": "products"}, {"key": "quantity", "type": "number"}]},
        {"op": "create_table", "table": "work_orders", "label": "Work orders", "fields": [
            {"key": "product", "type": "link", "table": "products"}, {"key": "quantity", "type": "number"}]},
        {"op": "create_table", "table": "needs", "label": "Work order materials", "embedded": True, "fields": [
            {"key": "work_order", "type": "link", "table": "work_orders"}, {"key": "material", "type": "link", "table": "materials"},
            {"key": "required", "type": "number"}]},
        {"op": "add_automation", "automation": {"name": "Materials for the work order", "trigger": {"type": "record_created", "table": "work_orders"},
                                                "actions": [{"type": "create_for_each", "source": {"table": "bom_lines", "via": "product", "of": "product",
                                                                                                    "nested": "component"}, "table": "needs",
                                                             "values": {"work_order": "{{record}}", "material": "{{item.material}}",
                                                                        "required": "{{item.quantity * record.quantity}}"},
                                                             "where": [{"field": "material", "op": "not_empty"}]}]}},
    ])
    oak, runner, knob = (e.create("materials", {"name": n}) for n in ("Oak", "Runner", "Knob"))
    drawer, cabinet = e.create("products", {"name": "Drawer unit"}), e.create("products", {"name": "Cabinet"})
    e.create("bom_lines", {"product": drawer.id, "material": runner.id, "quantity": 2})
    e.create("bom_lines", {"product": drawer.id, "material": knob.id, "quantity": 1})
    e.create("bom_lines", {"product": cabinet.id, "material": oak.id, "quantity": 6})
    e.create("bom_lines", {"product": cabinet.id, "component": drawer.id, "quantity": 3})  # three drawer units in each cabinet
    order = e.create("work_orders", {"product": cabinet.id, "quantity": 2})
    assert e.started[0].status == "done", e.started[0].error
    needs = {r.data["material"]: r.data["required"] for r in Engine(ws).rows("needs") if r.data["work_order"] == order.id}
    assert needs == {oak.id: 12, runner.id: 12, knob.id: 6}
    assert {r.data["quantity"] for r in Engine(ws).rows("bom_lines")} == {2, 1, 6, 3}  # the parts lists themselves are as they were
    assert "bill of materials (and its sub-assemblies' own, through Component)" in automations.describe(e, ws.blueprint["automations"][0])["then"][0]
    wrong = ops.apply(ws.blueprint, [{"op": "update_automation", "automation": {**ws.blueprint["automations"][0], "actions": [
        {**ws.blueprint["automations"][0]["actions"][0], "source": {"table": "bom_lines", "via": "product", "of": "product", "nested": "material"}}]}}])
    assert any("“nested” must be the link of Bill of materials to the sub-assembly" in p for p in wrong.problems)


def test_a_finished_job_becomes_an_invoice_whose_pdf_is_emailed(app, owner, monkeypatch):
    from app.connectors import microsoft
    _, ws = owner
    e = _built(ws, [
        {"op": "create_table", "table": "jobs", "label": "Jobs", "fields": [
            {"key": "title", "type": "text"}, {"key": "email", "type": "email"},
            {"key": "status", "type": "select", "options": ["Booked", "Done"]}]},
        {"op": "create_table", "table": "invoices", "label": "Invoices", "fields": [
            {"key": "number", "type": "autonumber", "prefix": "INV-"}, {"key": "job", "type": "link", "table": "jobs"},
            {"key": "amount", "type": "money"}, {"key": "pdf", "type": "file"}],
         "documents": [{"key": "tax_invoice", "label": "Tax invoice", "number_field": "number", "totals": ["amount"]}]},
        {"op": "add_automation", "automation": {"name": "Invoice the job", "trigger": {"type": "record_updated", "table": "jobs"},
                                                "conditions": [{"field": "status", "op": "changed_to", "value": "Done"}],
                                                "actions": [{"type": "create_record", "table": "invoices", "values": {"job": "{{record}}", "amount": 250}},
                                                            {"type": "generate_document", "document": "tax_invoice", "field": "invoice_pdf"},
                                                            {"type": "send_email", "to": "{{record.email}}", "subject": "Your invoice",
                                                             "body": "Attached.", "attach": ["tax_invoice"]}]}},
    ])
    produced = ws.blueprint["automations"][0]["actions"][1]
    assert produced["target"] == "created" and produced["field"] == "pdf"  # the new invoice's PDF, in its one file field
    sent = []
    monkeypatch.setattr(microsoft, "send_mail", lambda ws_, to, subject, body, **k: sent.append((to, [a[0] for a in k.get("attachments", [])])))
    monkeypatch.setattr(microsoft, "status", lambda ws_: {"connected": True})
    job = e.create("jobs", {"title": "Hot water", "email": "kim@example.com", "status": "Booked"})
    e.update(e.get(job.id), {"status": "Done"})
    run = e.started[-1]
    assert run.status == "done", run.error
    invoice = Engine(ws).rows("invoices")[0]
    assert File.query.filter_by(record_id=invoice.id, field="pdf").count() == 1
    assert sent == [("kim@example.com", ["Tax invoice INV-0001.pdf"])]
    assert automations.describe(e, ws.blueprint["automations"][0])["then"][1] == "Produce the Tax invoice PDF of the new invoice into PDF"


def test_an_owner_statement_of_rent_less_expenses_across_their_properties(app, owner):
    _, ws = owner
    e = _built(ws, [
        {"op": "create_table", "table": "owners", "label": "Owners", "singular": "Owner", "fields": [
            {"key": "name", "type": "text"}, {"key": "address", "type": "address"}]},
        {"op": "create_table", "table": "properties", "label": "Properties", "fields": [
            {"key": "address", "type": "text"}, {"key": "owner", "type": "link", "table": "owners"}]},
        {"op": "create_table", "table": "rent_payments", "label": "Rent payments", "fields": [
            {"key": "property", "type": "link", "table": "properties"}, {"key": "paid_on", "type": "date"}, {"key": "amount", "type": "money"}]},
        {"op": "create_table", "table": "expenses", "label": "Expenses", "fields": [
            {"key": "property", "type": "link", "table": "properties"}, {"key": "description", "type": "text"}, {"key": "amount", "type": "money"}]},
        # as the AI writes it: to "owner" on Owners, lines "via" the owner they don't link to, an invoice's totals
        {"op": "add_document", "table": "owners", "document": {
            "key": "statement", "label": "Monthly statement", "to": {"field": "owner", "details": ["address"]},
            "lines": {"table": "rent_payments", "via": "owner", "columns": ["property", "paid_on", "amount"], "total": "amount", "label": "Rent"},
            "sections": [{"label": "Expenses", "table": "expenses", "via": "owner", "columns": ["description", "amount"], "total": "amount",
                          "subtract": True}],
            "totals": ["subtotal", "gst", "total"], "net": "Paid to you"}},
    ])
    doc = ws.blueprint["tables"][0]["documents"][0]
    assert doc["to"]["field"] == "record" and doc["lines"]["via"] == "property.owner" and doc["totals"] == []
    kim, lee = e.create("owners", {"name": "Kim", "address": "1 Bay St"}), e.create("owners", {"name": "Lee"})
    flat, house, other = (e.create("properties", {"address": a, "owner": o.id}) for a, o in (("Flat 2", kim), ("12 Hill Rd", kim), ("Unit 9", lee)))
    for p, amount in ((flat, 2000), (house, 3000), (other, 9999)):
        e.create("rent_payments", {"property": p.id, "paid_on": e.today.isoformat(), "amount": amount})
    e.create("expenses", {"property": house.id, "description": "Plumber", "amount": 400})
    data, _ = build(Engine(ws), Engine(ws).get(kim.id), "statement")
    assert data["to"]["lines"] == ["Kim", "1 Bay St"]
    assert len(data["lines"]["rows"]) == 2 and data["sections"][0]["heading"] == "Expenses"
    assert [(t["label"], t["value"]) for t in data["totals"]] == [("Total rent", "$5,000.00"), ("Less expenses", "-$400.00"),
                                                                  ("Paid to you", "$4,600.00")]
    assert produce(Engine(ws), Engine(ws).get(kim.id), "statement").mime == "application/pdf"


def test_a_statement_of_its_own_totals_and_lists_its_owners_records_in_its_period(app, owner):
    _, ws = owner
    period = [{"field": "paid_on", "op": "at_least", "value": "{{record.period_start}}"},
              {"field": "paid_on", "op": "at_most", "value": "{{record.period_end}}"}]
    e = _built(ws, [  # as the fifth run wrote it: a statement per owner and month, reaching rent through a lookup
        {"op": "create_table", "table": "owners", "label": "Owners", "fields": [{"key": "name", "type": "text"}]},
        {"op": "create_table", "table": "properties", "label": "Properties", "fields": [
            {"key": "address", "type": "text"}, {"key": "owner", "type": "link", "table": "owners"}]},
        {"op": "create_table", "table": "rent_payments", "label": "Rent payments", "fields": [
            {"key": "property", "type": "link", "table": "properties"}, {"key": "paid_on", "type": "date"}, {"key": "amount", "type": "money"},
            {"key": "owner", "type": "lookup", "link": "property", "field": "owner"}]},
        {"op": "create_table", "table": "expenses", "label": "Expenses", "fields": [
            {"key": "property", "type": "link", "table": "properties"}, {"key": "paid_on", "type": "date"}, {"key": "amount", "type": "money"}]},
        {"op": "create_table", "table": "owner_statements", "label": "Owner statements", "fields": [
            {"key": "owner", "type": "link", "table": "owners", "required": True}, {"key": "period_start", "type": "date"},
            {"key": "period_end", "type": "date"},
            {"key": "rent", "type": "rollup", "table": "rent_payments", "via": "owner", "aggregate": "sum", "field": "amount", "filters": period},
            {"key": "spent", "type": "rollup", "table": "expenses", "via": "property.owner", "aggregate": "sum", "field": "amount", "filters": period},
            {"key": "net", "type": "formula", "expression": "rent - spent", "format": "money"}],
         "documents": [{"key": "statement", "label": "Owner statement",
                        "lines": {"table": "rent_payments", "via": "property.owner", "filters": period, "total": "amount", "label": "Rent"},
                        "sections": [{"label": "Expenses", "table": "expenses", "via": "property", "total": "amount", "subtract": True,
                                      "filters": period}]}]},
    ])
    assert ws.blueprint["tables"][-1]["documents"][0]["sections"][0]["via"] == "property.owner"  # the one path to the owner
    kim, lee = e.create("owners", {"name": "Kim"}), e.create("owners", {"name": "Lee"})
    flat, unit = e.create("properties", {"address": "Flat 2", "owner": kim.id}), e.create("properties", {"address": "Unit 9", "owner": lee.id})
    for p, day, amount in ((flat, "2026-10-03", 2000), (flat, "2026-10-17", 2000), (flat, "2026-09-30", 1500), (unit, "2026-10-03", 9999)):
        e.create("rent_payments", {"property": p.id, "paid_on": day, "amount": amount})
    e.create("expenses", {"property": flat.id, "paid_on": "2026-10-20", "amount": 400})
    e.create("expenses", {"property": flat.id, "paid_on": "2026-11-01", "amount": 50})
    october = e.create("owner_statements", {"owner": kim.id, "period_start": "2026-10-01", "period_end": "2026-10-31"})
    e = Engine(ws)
    assert (e.value(october, "rent"), e.value(october, "spent"), e.value(october, "net")) == (4000, 400, 3600)
    data, _ = build(e, e.get(october.id), "statement")
    assert len(data["lines"]["rows"]) == 2 and len(data["sections"][0]["rows"]) == 1
    assert [t["value"] for t in data["totals"]] == ["$4,000.00", "-$400.00", "$3,600.00"]
    assert e.value(e.create("owner_statements", {"period_start": "2026-10-01", "period_end": "2026-10-31", "owner": lee.id}), "spent") == 0


def test_rent_reaches_its_owner_through_lease_and_property_for_their_statement(app, owner):
    _, ws = owner
    period = [{"field": "paid_on", "op": "at_least", "value": "{{record.period_start}}"},
              {"field": "paid_on", "op": "at_most", "value": "{{record.period_end}}"}]
    e = _built(ws, [  # as the seventh run wrote it: a rent payment's owner is its lease's property's owner
        {"op": "create_table", "table": "owners", "label": "Owners", "fields": [{"key": "name", "type": "text"}]},
        {"op": "create_table", "table": "properties", "label": "Properties", "fields": [
            {"key": "address", "type": "text"}, {"key": "owner", "type": "link", "table": "owners"}]},
        {"op": "create_table", "table": "leases", "label": "Leases", "fields": [
            {"key": "tenant", "type": "text"}, {"key": "property", "type": "link", "table": "properties"}]},
        {"op": "create_table", "table": "rent_payments", "label": "Rent payments", "fields": [
            {"key": "lease", "type": "link", "table": "leases"}, {"key": "property", "type": "lookup", "link": "lease", "field": "property"},
            {"key": "owner", "type": "lookup", "link": "lease", "field": "property.owner"},
            {"key": "paid_on", "type": "date"}, {"key": "amount", "type": "money"}]},
        {"op": "create_table", "table": "owner_statements", "label": "Owner statements", "fields": [
            {"key": "owner", "type": "link", "table": "owners"}, {"key": "period_start", "type": "date"}, {"key": "period_end", "type": "date"},
            {"key": "rent", "type": "rollup", "table": "rent_payments", "via": "owner", "aggregate": "sum", "field": "amount", "filters": period},
            {"key": "by_property", "type": "rollup", "table": "rent_payments", "via": "property.owner", "aggregate": "count"}]},
    ])
    kim, lee = e.create("owners", {"name": "Kim"}), e.create("owners", {"name": "Lee"})
    flat, unit = e.create("properties", {"address": "Flat 2", "owner": kim.id}), e.create("properties", {"address": "Unit 9", "owner": lee.id})
    ann, bo = e.create("leases", {"tenant": "Ann", "property": flat.id}), e.create("leases", {"tenant": "Bo", "property": unit.id})
    for lease, day, amount in ((ann, "2026-10-03", 2000), (ann, "2026-10-17", 2000), (ann, "2026-09-30", 1500), (bo, "2026-10-03", 9999)):
        e.create("rent_payments", {"lease": lease.id, "paid_on": day, "amount": amount})
    october = e.create("owner_statements", {"owner": kim.id, "period_start": "2026-10-01", "period_end": "2026-10-31"})
    e = Engine(ws)
    payment = e.rows("rent_payments")[0]
    assert e.value(payment, "owner") == kim.id and e.value(payment, "property") == flat.id
    assert (e.value(october, "rent"), e.value(october, "by_property")) == (4000, 3)
    too_far = ops.apply(ws.blueprint, [{"op": "add_field", "table": "rent_payments", "field": {
        "key": "landlord", "type": "lookup", "link": "lease", "field": "property.owner.name.x"}}])
    assert any("has no field" in p for p in too_far.problems)


def test_saying_again_what_is_already_so_isnt_told_as_a_change(app, owner):
    _, ws = owner
    _built(ws, [{"op": "create_table", "table": "invoices", "label": "Invoices", "fields": [
        {"key": "number", "type": "text"}, {"key": "status", "type": "select", "options": ["Draft", "Sent"]}],
        "lock_if": [{"field": "status", "op": "equals", "value": "Sent"}]}])
    again = ops.apply(ws.blueprint, [{"op": "update_table", "table": "invoices", "changes": {"lock_if": [{"field": "status", "op": "equals", "value": "Sent"}]}}])
    assert not again.problems and not again.changed
    more = ops.apply(ws.blueprint, [{"op": "update_table", "table": "invoices", "changes": {"lock_except": ["status"]}}])
    assert more.changed


def test_a_purchase_order_and_its_lines_show_the_suppliers_currency(app, owner):
    _, ws = owner
    e = _built(ws, [
        {"op": "create_table", "table": "suppliers", "label": "Suppliers", "fields": [
            {"key": "name", "type": "text"}, {"key": "currency", "type": "select", "options": ["AUD", "USD"]}]},
        {"op": "create_table", "table": "purchase_orders", "label": "Purchase orders", "fields": [
            {"key": "supplier", "type": "link", "table": "suppliers"}, {"key": "currency", "type": "lookup", "link": "supplier", "field": "currency"},
            {"key": "subtotal", "type": "rollup", "table": "po_lines", "via": "po", "aggregate": "sum", "field": "amount"},
            {"key": "total_aud", "type": "formula", "expression": "convert(subtotal, currency)", "format": "money"}]},
        {"op": "create_table", "table": "po_lines", "label": "PO lines", "embedded": True, "fields": [
            {"key": "po", "type": "link", "table": "purchase_orders"}, {"key": "quantity", "type": "number"},
            {"key": "unit_price", "type": "money"}, {"key": "amount", "type": "formula", "expression": "quantity * unit_price", "format": "money"}]},
    ])
    po_t, lines_t = ws.blueprint["tables"][1], ws.blueprint["tables"][2]
    assert schema.field(po_t, "subtotal")["currency_field"] == "currency" and "currency_field" not in schema.field(po_t, "total_aud")
    assert schema.field(lines_t, "unit_price")["currency_field"] == "po.currency"
    acme = e.create("suppliers", {"name": "Acme Foods", "currency": "USD"})
    po = e.create("purchase_orders", {"supplier": acme.id})
    line = e.create("po_lines", {"po": po.id, "quantity": 2, "unit_price": 10})
    e = Engine(ws)
    assert e.symbol(schema.field(lines_t, "unit_price"), e.get(line.id)) == "US$"
    assert e.symbol(schema.field(po_t, "subtotal"), e.get(po.id)) == "US$"


def test_an_update_written_with_payload_like_a_webhook_still_updates(app, owner):
    _, ws = owner
    e = _built(ws, [  # as the fifth run wrote it, with the record named first and the values in "payload"
        {"op": "create_table", "table": "animals", "label": "Animals", "fields": [
            {"key": "name", "type": "text"}, {"key": "status", "type": "select", "options": ["Available", "Adopted"]}]},
        {"op": "create_table", "table": "adoptions", "label": "Adoptions", "fields": [
            {"key": "animal", "type": "link", "table": "animals"}, {"key": "approved", "type": "checkbox"}]},
        {"op": "add_automation", "automation": {"key": "adopted", "name": "Animal adopted",
                                                "trigger": {"type": "record_updated", "table": "adoptions"},
                                                "conditions": [{"field": "approved", "op": "changed_to", "value": True}],
                                                "actions": [{"type": "update_record", "target": "animal", "fields": ["status"],
                                                             "method": "PATCH", "payload": {"status": "Adopted"}}]}},
    ])
    rex = e.create("animals", {"name": "Rex", "status": "Available"})
    adoption = e.create("adoptions", {"animal": rex.id})
    e.update(adoption, {"approved": True})
    assert Engine(ws).get(rex.id).data["status"] == "Adopted"


@pytest.mark.parametrize("customer", ["customer", "customer.name"])  # the customer's own name names the customer
def test_a_flat_spreadsheet_comes_in_as_customers_orders_and_lines_keeping_its_numbers(app, owner, customer):
    import io
    from openpyxl import Workbook
    from app import importer, jobs
    from app.storage import save
    _, ws = owner
    e = _built(ws, [
        {"op": "create_table", "table": "customers", "label": "Customers", "fields": [{"key": "name", "type": "text"}, {"key": "email", "type": "email"}]},
        {"op": "create_table", "table": "products", "label": "Products", "fields": [{"key": "name", "type": "text"}]},
        {"op": "create_table", "table": "orders", "label": "Orders", "fields": [
            {"key": "number", "type": "autonumber", "prefix": "ORD-"}, {"key": "date", "type": "date"},
            {"key": "customer", "type": "link", "table": "customers", "required": True}]},
        {"op": "create_table", "table": "order_lines", "label": "Order lines", "embedded": True, "fields": [
            {"key": "order", "type": "link", "table": "orders"}, {"key": "product", "type": "link", "table": "products"},
            {"key": "quantity", "type": "number"}]},
    ])
    e.create("customers", {"name": "Little Fox", "email": "hello@littlefox.com.au"})
    book = Workbook()
    book.active.append(["Order #", "Date", "Customer", "Customer email", "Product", "Qty"])
    for row in (("1001", "2026-09-02", "Bean There Cafe", "orders@beanthere.com.au", "House blend 1kg", 4),
                ("1001", "2026-09-02", "Bean There Cafe", "orders@beanthere.com.au", "Decaf 500g", 2),  # a second line of 1001
                ("1002", "2026-09-03", "Little Fox", "", "Decaf 500g", 6)):
        book.active.append(list(row))
    buffer = io.BytesIO()
    book.save(buffer)
    sheet = save(ws.id, buffer.getvalue(), "orders.xlsx")
    result = ops.apply(ws.blueprint, [{"op": "import_file", "file_id": sheet.id, "table": "orders", "mapping": {
        "Order #": "number", "Date": "date", "Customer": customer, "Customer email": "customer.email",
        "Product": "order_lines.product", "Qty": "order_lines.quantity"}}])
    assert not result.problems and result.data[0]["skipped"] == []
    job = importer.start(ws, sheet.id, "orders", result.data[0]["mapping"])
    jobs.run(job)
    assert job.result["added"] == 2 and job.result["lines"] == 3 and job.result["skipped"] == 0, job.result
    e = Engine(ws)
    orders = {r.data["number"]: r for r in e.rows("orders")}
    assert set(orders) == {"ORD-1001", "ORD-1002"}
    customers = {r.data["name"]: r.data.get("email") for r in e.rows("customers")}
    assert customers == {"Little Fox": "hello@littlefox.com.au", "Bean There Cafe": "orders@beanthere.com.au"}
    assert len(e.children("order_lines", "order", orders["ORD-1001"].id)) == 2
    assert sorted(r.data["name"] for r in e.rows("products")) == ["Decaf 500g", "House blend 1kg"]
    fox = next(r.id for r in e.rows("customers") if r.data["name"] == "Little Fox")
    assert e.create("orders", {"date": "2026-10-01", "customer": fox}).data["number"] == "ORD-1003"  # new orders carry on after the imported ones


def test_the_fourth_runs_shorthand_is_read_the_one_way_it_can_be(app, owner):
    _, ws = owner
    e = _built(ws, [
        {"op": "create_table", "table": "consultants", "label": "Consultants", "fields": [
            {"key": "name", "type": "text"}, {"key": "status", "type": "select", "options": ["Active", "Off"]}]},
        {"op": "create_table", "table": "projects", "label": "Projects", "fields": [
            {"key": "name", "type": "text"}, {"key": "contract_value", "type": "money"}]},
        {"op": "create_table", "table": "entries", "label": "Timesheet entries", "fields": [
            {"key": "project", "type": "link", "table": "projects"}, {"key": "hours", "type": "number"}, {"key": "billable", "type": "checkbox"}]},
        {"op": "create_table", "table": "lines", "label": "Invoice lines", "fields": [
            {"key": "project", "type": "link", "table": "projects"}, {"key": "hours", "type": "number"}]},
        {"op": "create_table", "table": "variations", "label": "Variations", "fields": [
            {"key": "project", "type": "link", "table": "projects"}, {"key": "amount", "type": "money"},
            {"key": "status", "type": "select", "options": ["Raised", "Signed"]}]},
        # as written: the timing in the label, a per-line condition as the step's only_if, a value of the project set on
        # the variation, and a list column for the project's name
        {"op": "add_automation", "automation": {"name": "Friday reminder", "trigger": {"type": "schedule", "table": "consultants", "label": "Friday 3pm"},
                                                "conditions": [{"field": "status", "op": "not_equals", "value": "Off"}],
                                                "actions": [{"type": "notify", "message": "Log your hours, {{record.name}}"}]}},
        {"op": "add_automation", "automation": {"name": "Bill hours", "trigger": {"type": "button", "table": "projects", "label": "Bill"},
                                                "actions": [{"type": "create_for_each", "source": {"table": "entries", "via": "project"}, "table": "lines",
                                                             "values": {"project": "{{record}}", "hours": "{{item.hours}}"},
                                                             "only_if": [{"field": "item.billable", "op": "equals", "value": True}]}]}},
        {"op": "add_automation", "automation": {"name": "Variation signed", "trigger": {"type": "record_updated", "table": "variations"},
                                                "conditions": [{"field": "status", "op": "changed_to", "value": "Signed"}],
                                                "actions": [{"type": "update_record", "values": {"contract_value": "{{record.project.contract_value + record.amount}}"}}]}},
        {"op": "add_page", "page": {"label": "Overview", "widgets": [{"type": "list", "table": "variations", "label": "Pending",
                                                                      "columns": ["project_name", "amount", "nonsense"]}]}},
    ])
    friday, billing, signed = ws.blueprint["automations"]
    assert friday["trigger"] == {"type": "schedule", "table": "consultants", "every": "week", "at": "15:00", "weekday": "fri"}
    assert billing["actions"][0]["where"] == [{"field": "billable", "op": "equals", "value": True}] and "only_if" not in billing["actions"][0]
    assert signed["actions"][0]["target"] == "project"
    assert ws.blueprint["pages"][0]["widgets"][0]["columns"] == ["project", "amount"]
    job = e.create("projects", {"name": "Survey", "contract_value": 1000})
    e.create("entries", {"project": job.id, "hours": 5, "billable": True})
    e.create("entries", {"project": job.id, "hours": 3, "billable": False})
    automations.press(e, billing, Engine(ws).get(job.id), "Ethan")
    assert [r.data["hours"] for r in Engine(ws).rows("lines")] == [5]
    variation = e.create("variations", {"project": job.id, "amount": 250, "status": "Raised"})
    e.update(e.get(variation.id), {"status": "Signed"})
    assert Engine(ws).get(job.id).data["contract_value"] == 1250


def test_the_closing_reply_isnt_told_about_things_not_built_when_there_are_none(app, owner):
    from app import ai
    from tests.conftest import ScriptedAI
    scripted = ScriptedAI({"reply": "Done."}, {"reply": "Mostly done."})
    ai.set_backend(scripted)
    chat._closing_reply("Patients and notes", ["Built."], ["Created the table Treatment notes"], [], [])
    chat._closing_reply("Patients and notes", ["Built."], ["Created the table Patients"], ["the treatment notes"], [])
    assert "Carry on building" not in scripted.asked[0][0]["content"] and "Carry on building" in scripted.asked[1][0]["content"]


def test_an_invoice_made_in_a_run_gets_journal_lines_from_its_own_lines(app, owner):
    _, ws = owner
    e = _built(ws, [
        {"op": "create_table", "table": "orders", "label": "Orders", "fields": [
            {"key": "ref", "type": "text"}, {"key": "status", "type": "select", "options": ["Open", "Invoiced"]}]},
        {"op": "create_table", "table": "order_lines", "label": "Order lines", "embedded": True, "fields": [
            {"key": "order", "type": "link", "table": "orders"}, {"key": "amount", "type": "money"}]},
        {"op": "create_table", "table": "invoices", "label": "Invoices", "fields": [{"key": "order", "type": "link", "table": "orders"},
                                                                                   {"key": "status", "type": "text"}]},
        {"op": "create_table", "table": "invoice_lines", "label": "Invoice lines", "embedded": True, "fields": [
            {"key": "invoice", "type": "link", "table": "invoices"}, {"key": "amount", "type": "money"}]},
        {"op": "create_table", "table": "entries", "label": "Journal entries", "fields": [{"key": "memo", "type": "text"}]},
        {"op": "create_table", "table": "journal_lines", "label": "Journal lines", "embedded": True, "fields": [
            {"key": "entry", "type": "link", "table": "entries"}, {"key": "debit", "type": "money"}]},
        {"op": "add_automation", "automation": {"name": "Order to invoice", "trigger": {"type": "record_updated", "table": "orders"},
                                                "conditions": [{"field": "status", "op": "changed_to", "value": "Invoiced"}], "actions": [
            {"type": "create_record", "table": "invoices", "values": {"order": "{{record}}"}},
            {"type": "create_for_each", "source": {"table": "order_lines", "via": "order"}, "table": "invoice_lines",
             "values": {"invoice": "{{created}}", "amount": "{{item.amount}}"}},
            {"type": "update_record", "values": {"status": "Sent", "target": "created"}},  # "target" among the values, as written
            {"type": "create_record", "table": "entries", "values": {"memo": "Invoice"}},
            {"type": "create_for_each", "source": {"table": "invoice_lines", "via": "invoice", "of": "created"}, "table": "journal_lines",
             "values": {"entry": "{{created}}", "debit": "{{item.amount}}"}}]}},
    ])
    order = e.create("orders", {"ref": "A1", "status": "Open"})
    for amount in (100, 50):
        e.create("order_lines", {"order": order.id, "amount": amount})
    e.update(e.get(order.id), {"status": "Invoiced"})
    assert e.started[-1].status == "done", e.started[-1].error
    e = Engine(ws)
    assert e.rows("invoices")[0].data["status"] == "Sent"
    assert sorted(r.data["debit"] for r in e.rows("journal_lines")) == [50, 100]
    assert automations.describe(e, ws.blueprint["automations"][0])["then"][4].startswith("For each of the new invoice's invoice lines")


def test_clients_and_subcontractors_each_have_a_portal_showing_only_theirs(app, owner, client):
    import re
    from .conftest import login
    _, ws = owner
    e = _built(ws, [  # as the fifth run wrote it: tables shown in the portal, but set_portal only for the clients
        {"op": "create_table", "table": "clients", "label": "Clients", "fields": [{"key": "name", "type": "text"}, {"key": "email", "type": "email"}]},
        {"op": "create_table", "table": "subcontractors", "label": "Subcontractors", "singular": "Subcontractor", "fields": [
            {"key": "name", "type": "text"}, {"key": "email", "type": "email"}]},
        {"op": "create_table", "table": "projects", "label": "Projects", "fields": [
            {"key": "title", "type": "text"}, {"key": "client", "type": "link", "table": "clients"}], "portal": {"via": "client"}},
        {"op": "create_table", "table": "subcontractor_jobs", "label": "Subcontractor jobs", "fields": [
            {"key": "title", "type": "text"}, {"key": "subcontractor", "type": "link", "table": "subcontractors"},
            {"key": "project", "type": "link", "table": "projects"}], "portal": {"via": "subcontractor"}},
        {"op": "create_table", "table": "subcontractor_invoices", "label": "Subcontractor invoices", "fields": [
            {"key": "reference", "type": "text"}, {"key": "amount", "type": "money"},
            {"key": "subcontractor", "type": "link", "table": "subcontractors"}], "portal": {"via": "subcontractor", "can_add": True}},
        {"op": "set_portal", "portal": {"table": "clients", "email_field": "email", "title": "Client portal"}},
    ])
    assert [p["title"] for p in schema.portals(ws.blueprint)] == ["Client portal", "Subcontractor portal"]
    from app.models import Version
    said = Version.query.filter_by(workspace_id=ws.id).order_by(Version.number.desc()).first().changes
    assert "Set up the Subcontractor portal for Subcontractors, where they see their subcontractor jobs" in said
    kim = e.create("clients", {"name": "Kim", "email": "kim@example.com"})
    sparky, chippy = (e.create("subcontractors", {"name": n, "email": f"{n.lower()}@example.com"}) for n in ("Sparky", "Chippy"))
    house = e.create("projects", {"title": "12 Hill Rd", "client": kim.id})
    wiring = e.create("subcontractor_jobs", {"title": "Rough-in wiring", "subcontractor": sparky.id, "project": house.id})
    e.create("subcontractor_jobs", {"title": "Frame", "subcontractor": chippy.id, "project": house.id})

    def join(table, person, email):
        login(client)
        page = client.post(f"/w/{ws.id}/app/t/{table}/{person.id}/portal-invite", follow_redirects=True).get_data(as_text=True)
        client.post("/logout")
        client.get(f"/invite/{re.search(r'/invite/([\w-]+)', page).group(1)}")
        client.post("/register", data={"name": person.data["name"], "email": email, "password": "password1234"})
        return client.get(f"/portal/{ws.id}/").get_data(as_text=True)

    home = join("subcontractors", sparky, "sparky@example.com")
    assert "Subcontractor portal" in home and "Rough-in wiring" in home and "Frame" not in home and "Projects" not in home
    assert client.get(f"/portal/{ws.id}/t/subcontractor_jobs/{wiring.id}").status_code == 200
    assert client.get(f"/portal/{ws.id}/t/projects/{house.id}").status_code == 404
    assert client.post(f"/portal/{ws.id}/t/subcontractor_invoices/new", data={"reference": "INV-77", "amount": "1200"}).status_code == 302
    sent = next(r for r in Engine(ws).rows("subcontractor_invoices"))
    assert sent.data["subcontractor"] == sparky.id
    client.post("/logout")
    home = join("clients", kim, "kim@example.com")
    assert "Client portal" in home and "12 Hill Rd" in home and "Rough-in wiring" not in home and "INV-77" not in home


def test_a_new_purchase_order_takes_its_suppliers_currency_and_says_what_it_keeps_private(app, owner):
    _, ws = owner
    result = ops.apply(ws.blueprint, [  # as the sixth run wrote it: the supplier has a currency, the order doesn't
        {"op": "create_table", "table": "suppliers", "label": "Suppliers", "fields": [
            {"key": "name", "type": "text"}, {"key": "currency", "type": "select", "options": ["AUD", "USD"]}]},
        {"op": "create_table", "table": "warehouses", "label": "Warehouses", "fields": [{"key": "name", "type": "text"}]},
        {"op": "create_table", "table": "purchase_orders", "label": "Purchase orders", "fields": [
            {"key": "supplier", "type": "link", "table": "suppliers"}, {"key": "warehouse", "type": "link", "table": "warehouses"},
            {"key": "total", "type": "rollup", "table": "po_lines", "via": "po", "aggregate": "sum", "field": "amount"}]},
        {"op": "create_table", "table": "po_lines", "label": "PO lines", "embedded": True, "fields": [
            {"key": "po", "type": "link", "table": "purchase_orders"}, {"key": "amount", "type": "money"}]},
        {"op": "create_table", "table": "staff", "label": "Staff", "access": {"view": "admin", "edit": "admin"}, "fields": [
            {"key": "name", "type": "text"}, {"key": "pay_rate", "type": "money", "access": "owner"}]},
        {"op": "create_table", "table": "invoices", "label": "Invoices", "fields": [{"key": "status", "type": "text"}],
         "lock_if": [{"field": "status", "op": "equals", "value": "Sent"}]},
    ])
    assert not result.problems, result.problems
    po_t = schema.table(result.blueprint, "purchase_orders")
    assert schema.field(po_t, "currency") == {**schema.field(po_t, "currency"), "type": "lookup", "link": "supplier", "field": "currency"}
    assert schema.field(po_t, "total")["currency_field"] == "currency"
    assert schema.field(schema.table(result.blueprint, "po_lines"), "amount")["currency_field"] == "po.currency"
    assert "Added Currency to Purchase orders (its supplier's), so its amounts are in it" in result.changes
    said = " | ".join(result.changes)
    assert "Staff (2 fields: Name, Pay rate): seen only by admins; Pay rate kept to owners" in said
    assert "Invoices (1 field: Status): locked when Status equals Sent" in said
    again = ops.apply(result.blueprint, [{"op": "remove_field", "table": "purchase_orders", "key": "currency"}])
    assert not schema.field(schema.table(again.blueprint, "purchase_orders"), "currency")  # only new orders get one


def test_an_email_step_without_a_mailbox_asks_a_person_to_send_it(app, owner):
    from app.models import Run
    _, ws = owner
    e = _built(ws, [
        {"op": "create_table", "table": "jobs", "label": "Jobs", "fields": [{"key": "title", "type": "text"}, {"key": "email", "type": "email"}]},
        {"op": "add_automation", "automation": {"key": "booked", "name": "Tell the customer", "trigger": {"type": "record_created", "table": "jobs"},
                                                "actions": [{"type": "send_email", "to": "{{record.email}}", "subject": "Booked", "body": "See you soon."}]}},
    ])
    job = e.create("jobs", {"title": "Burst pipe", "email": "kim@example.com"})
    assert Engine(ws).get(job.id) is not None
    run = Run.query.filter_by(automation="booked").one()
    from app.models import Approval
    task = Approval.query.filter_by(run_id=run.id).one()  # nothing pretends it went: a person sends it, then it carries on
    assert run.status == "waiting_approval" and task.kind == "task" and "Email isn't set up, so please send this yourself" in task.message
    assert "“Booked” to kim@example.com" in task.message
