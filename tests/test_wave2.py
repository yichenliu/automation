"""What the second wave of beginner testers found (a food wholesaler with two warehouses, a furniture maker and a property
manager building their own apps), each kept fixed. Every fix is the platform's, for any app: the designs here are shaped
like the ones the builder made for them."""
import json

from app.blueprint import ops, schema
from app.builder import chat, trial
from app.engine import automations
from app.engine.records import Engine, FieldErrors
from app.extensions import db
from app.models import Approval, Membership, Record, User

import pytest

from .conftest import login


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


# The wholesaler's orders: delivering one takes its lines off the stock of the warehouse it was picked from, each line
# naming the stock level by what it's for (this product at that warehouse). "Delivered" needs the delivery date, so it
# was hidden, leaving Cancel as the only button after Start picking; the try-out took Cancel too, and never ran the rule.
STOCK = [
    {"op": "create_table", "table": "warehouses", "label": "Warehouses", "fields": [{"key": "name", "type": "text", "required": True}]},
    {"op": "create_table", "table": "products", "label": "Products", "fields": [{"key": "name", "type": "text", "required": True}]},
    {"op": "create_table", "table": "stock_levels", "label": "Stock levels", "fields": [
        {"key": "product", "type": "link", "table": "products", "required": True},
        {"key": "warehouse", "type": "link", "table": "warehouses", "required": True},
        {"key": "on_hand", "label": "On hand", "type": "rollup", "aggregate": "sum", "table": "stock_movements", "field": "quantity",
         "via": "stock_level"}]},
    {"op": "create_table", "table": "stock_movements", "label": "Stock movements", "fields": [
        {"key": "stock_level", "label": "Stock level", "type": "link", "table": "stock_levels", "required": True},
        {"key": "quantity", "type": "number", "required": True}, {"key": "reason", "type": "text"}]},
    {"op": "create_table", "table": "sales_orders", "label": "Sales orders", "fields": [
        {"key": "reference", "type": "text", "required": True},
        {"key": "warehouse", "type": "link", "table": "warehouses", "required": True},
        {"key": "delivery_date", "label": "Delivery date", "type": "date"},
        {"key": "status", "type": "select", "options": ["New", "Picking", "Delivered", "Cancelled"], "default": "New"}],
     "workflow": {"field": "status", "transitions": [
         {"from": ["New"], "to": "Picking", "label": "Start picking"},
         {"from": ["Picking"], "to": "Delivered", "label": "Delivered", "requires": ["delivery_date"]},
         {"from": ["New", "Picking"], "to": "Cancelled", "label": "Cancel"}]}},
    {"op": "create_table", "table": "order_lines", "label": "Order lines", "embedded": True, "fields": [
        {"key": "order", "type": "link", "table": "sales_orders", "required": True},
        {"key": "product", "type": "link", "table": "products", "required": True},
        {"key": "quantity", "type": "number", "required": True}]},
    {"op": "add_automation", "automation": {
        "key": "take_stock", "name": "Delivered orders come off the stock", "trigger": {"type": "record_updated", "table": "sales_orders"},
        "conditions": [{"field": "status", "op": "changed_to", "value": "Delivered"}],
        "actions": [{"type": "create_for_each", "source": {"table": "order_lines", "via": "order"}, "table": "stock_movements",
                     "values": {"stock_level": {"product": "{{item.product}}", "warehouse": "{{record.warehouse}}"},
                                "quantity": "{{0 - item.quantity}}", "reason": "Order {{record.reference}}"}}]}},
]


def _order(engine, warehouse, product, quantity=10):
    order = engine.create("sales_orders", {"reference": "Bar Milano", "warehouse": warehouse.id})
    engine.create("order_lines", {"order": order.id, "product": product.id, "quantity": quantity})
    return engine.get(order.id)


def test_a_rule_names_a_record_by_what_its_for_and_one_thats_only_a_pairing_is_added(app, owner):
    user, ws = owner
    _built(ws, STOCK)
    e = Engine(ws, by="Marco")
    osborne, welshpool = e.create("warehouses", {"name": "Osborne Park"}), e.create("warehouses", {"name": "Welshpool"})
    oil, basil = e.create("products", {"name": "Olive oil"}), e.create("products", {"name": "Basil"})
    for w in (osborne, welshpool):
        level = e.create("stock_levels", {"product": oil.id, "warehouse": w.id})
        e.create("stock_movements", {"stock_level": level.id, "quantity": 50})
    order = _order(e, welshpool, oil, 10)
    e.move(order, 0)
    e.update(e.get(order.id), {"delivery_date": "2026-11-03"})
    e.move(e.get(order.id), 1)
    e.reset()
    on_hand = {e.title(e.get(r.data["warehouse"])): e.value(r, "on_hand") for r in e.rows("stock_levels")}
    assert on_hand == {"Osborne Park": 50, "Welshpool": 40}  # off the warehouse it went from, not the other

    order = _order(e, welshpool, basil, 2)  # no basil at Welshpool yet: its stock level is added, going below nothing
    e.move(order, 0)
    e.update(e.get(order.id), {"delivery_date": "2026-11-03"})
    e.move(e.get(order.id), 1)
    e.reset()
    added = next(r for r in e.rows("stock_levels") if r.data["product"] == basil.id)
    assert added.data["warehouse"] == welshpool.id and e.value(added, "on_hand") == -2

    with pytest.raises(FieldErrors, match="there's no Stock level with Product “Saffron” and Warehouse “Welshpool”"):
        e.create("stock_movements", {"stock_level": {"product": "Saffron", "warehouse": welshpool.id}, "quantity": 1})
    assert len(e.rows("products")) == 2  # a name that isn't there is never made up


def test_a_move_that_needs_something_filled_in_asks_for_it_beside_its_button(app, owner, client):
    user, ws = owner
    _built(ws, STOCK)
    e = Engine(ws, by="Marco")
    welshpool, oil = e.create("warehouses", {"name": "Welshpool"}), e.create("products", {"name": "Olive oil"})
    order = _order(e, welshpool, oil)
    e.move(order, 0)
    login(client)
    page = client.get(f"/w/{ws.id}/app/t/sales_orders/{order.id}").get_data(as_text=True)
    assert "Next: delivered" in page and 'name="fill_delivery_date"' in page  # not "Next: cancel", with Delivered hidden
    assert page.index("Delivered</button>") < page.index("Cancel</button>")
    client.post(f"/w/{ws.id}/app/t/sales_orders/{order.id}/move/1", data={"fill_delivery_date": "2026-11-04"})
    e.reset()
    done = e.get(order.id)
    assert done.data["status"] == "Delivered" and done.data["delivery_date"] == "2026-11-04"
    assert Record.query.filter_by(table="stock_movements").count() == 1  # and its rule ran


def test_the_try_out_goes_through_a_move_that_needs_filling_in_and_finds_a_stage_with_no_way_on(app, owner):
    _, ws = owner
    broken = [dict(op) for op in STOCK]
    broken[-1] = {"op": "add_automation", "automation": {**STOCK[-1]["automation"], "actions": [
        {"type": "create_for_each", "source": {"table": "order_lines", "via": "order"}, "table": "stock_movements",
         "values": {"stock_level": "{{item.nothing_here}}", "quantity": "{{item.quantity}}"}}]}}
    _built(ws, broken)
    walked, _ = trial.try_out(ws, screens=False)
    assert any("Delivered orders come off the stock" in i["text"] and i["level"] == "fix" for i in walked["issues"]), walked["issues"]

    flow = ws.blueprint["tables"][4]["workflow"]
    stuck = {**ws.blueprint}
    stuck["tables"] = [dict(t) for t in ws.blueprint["tables"]]
    stuck["tables"][4] = {**stuck["tables"][4], "workflow": {**flow, "transitions": [m for m in flow["transitions"] if m["to"] != "Delivered"]}}
    chat.save_version(ws, stuck, ["Delivered taken out by hand"], "chat")
    walked, _ = trial.try_out(ws, screens=False)
    dead = [i for i in walked["issues"] if "can't go on to Delivered" in i["plain"]]
    assert dead and dead[0]["level"] == "fix" and "the only way out is Cancel" in dead[0]["plain"], walked["issues"]


# Accounts approved an order over the customer's credit limit, then couldn't open it: reps see only their own orders,
# and so did everyone else on the team.
ORDERS = [
    {"op": "set_team", "team": {"key": "finance", "name": "Finance"}},
    {"op": "create_table", "table": "orders", "label": "Orders", "row_access": {"field": "rep", "roles": ["editor"]}, "fields": [
        {"key": "customer", "type": "text", "required": True}, {"key": "rep", "type": "user"}, {"key": "total", "type": "money"},
        {"key": "status", "type": "select", "options": ["New", "Waiting for approval", "Approved", "Sent"], "default": "New"}],
     "workflow": {"field": "status", "transitions": [
         {"from": ["New"], "to": "Waiting for approval", "label": "Send for approval"},
         {"from": ["Approved"], "to": "Sent", "label": "Send"}]}},
    {"op": "add_automation", "automation": {
        "key": "credit", "name": "Over-limit orders need Finance", "trigger": {"type": "record_updated", "table": "orders"},
        "conditions": [{"field": "status", "op": "changed_to", "value": "Waiting for approval"}],
        "actions": [{"type": "request_approval", "approver": "finance", "message": "Over the credit limit."},
                    {"type": "update_record", "values": {"status": "Approved"}}]}},
]


def test_whoever_is_asked_to_approve_a_record_can_open_it(app, owner):
    user, ws = owner
    _built(ws, ORDERS)
    luca = _person(ws, "luca@example.com", "Luca", "editor")
    sofia = _person(ws, "sofia@example.com", "Sofia", "editor", teams=["finance"])
    giulia = _person(ws, "giulia@example.com", "Giulia", "editor")
    rep = _as(ws, luca)
    order = rep.create("orders", {"customer": "Bar Milano", "rep": luca.id, "total": 2145})
    rep.move(rep.get(order.id), 0)
    accounts = _as(ws, sofia)
    assert accounts.allowed(accounts.get(order.id)) and [r.id for r in accounts.visible_rows("orders")] == [order.id]
    assert not _as(ws, giulia).allowed(Engine(ws).get(order.id))  # another rep still can't
    approval = Approval.query.one()
    automations.decide(accounts.system(), approval, True, "Sofia", by_id=sofia.id)  # her Approve, in To do
    assert db.session.get(Approval, approval.id).status == "approved"
    after = _as(ws, sofia)
    assert after.get(order.id).data["status"] == "Approved" and after.allowed(after.get(order.id))  # still hers to look at


# The buyer raised a $6,000 purchase order, the owner approved it, and she then made it $18,000: still "approved".
PURCHASES = [
    {"op": "create_table", "table": "purchase_orders", "label": "Purchase orders", "fields": [
        {"key": "supplier", "type": "text", "required": True},
        {"key": "total", "type": "rollup", "aggregate": "sum", "table": "po_lines", "field": "amount", "via": "po"},
        {"key": "paid", "label": "Amount paid", "type": "money"},
        {"key": "status", "type": "select", "options": ["Draft", "Waiting for approval", "Approved", "Sent to supplier"], "default": "Draft"}],
     "workflow": {"field": "status", "transitions": [
         {"from": ["Draft"], "to": "Waiting for approval", "label": "Send for approval"},
         {"from": ["Approved"], "to": "Sent to supplier", "label": "Send to supplier"}]}},
    {"op": "create_table", "table": "po_lines", "label": "PO lines", "embedded": True, "fields": [
        {"key": "po", "label": "PO", "type": "link", "table": "purchase_orders", "required": True},
        {"key": "item", "type": "text"}, {"key": "quantity", "type": "number"}, {"key": "unit_cost", "type": "money"},
        {"key": "amount", "type": "formula", "format": "money", "expression": "quantity * unit_cost"}]},
    {"op": "add_automation", "automation": {
        "key": "big_pos", "name": "POs over $5,000 need the owner", "trigger": {"type": "record_updated", "table": "purchase_orders"},
        "conditions": [{"field": "status", "op": "changed_to", "value": "Waiting for approval"}],
        "actions": [{"type": "branch", "paths": [
            {"label": "Over $5,000", "when": [{"field": "total", "op": "greater_than", "value": 5000}],
             "steps": [{"type": "request_approval", "approver": "owner", "message": "Please approve this.", "not_by": ["creator"]},
                       {"type": "update_record", "values": {"status": "Approved"}}]},
            {"label": "Under", "when": [{"field": "total", "op": "at_most", "value": 5000}],
             "steps": [{"type": "update_record", "values": {"status": "Approved"}}]}]}]}},
]


def test_what_was_approved_cant_quietly_grow_afterwards(app, owner):
    user, ws = owner
    _built(ws, PURCHASES)
    anna = _person(ws, "anna@example.com", "Anna", "editor")
    buyer, marco = _as(ws, anna), _as(ws, user)
    po = buyer.create("purchase_orders", {"supplier": "Olio Co"})
    line = buyer.create("po_lines", {"po": po.id, "item": "Olive oil", "quantity": 100, "unit_cost": 60})
    buyer.move(buyer.get(po.id), 0)
    automations.decide(Engine(ws, by="Ethan"), Approval.query.one(), True, "Ethan", by_id=user.id)  # in To do
    buyer.reset()
    assert buyer.get(po.id).data["status"] == "Approved"
    buyer.move(buyer.get(po.id), 1)  # sent to the supplier: still what was approved
    with pytest.raises(FieldErrors, match=r"approved at \$6,000.00 by Ethan, so its total can't go above that now \(\$18,000.00\)"):
        buyer.update(buyer.get(line.id), {"quantity": 300})
    with pytest.raises(FieldErrors, match="approved at"):
        buyer.create("po_lines", {"po": po.id, "item": "More oil", "quantity": 1, "unit_cost": 60})
    buyer.update(buyer.get(line.id), {"quantity": 90})  # less is fine
    buyer.update(buyer.get(po.id), {"paid": 5400})  # and money that's meant to change later
    marco.reset()
    marco.update(marco.get(line.id), {"quantity": 120})  # whoever approved it may
    marco.reset()
    assert marco.value(marco.get(po.id), "total") == 7200

    small = buyer.create("purchase_orders", {"supplier": "Farm"})  # under the limit it goes straight through, and is held too
    small_line = buyer.create("po_lines", {"po": small.id, "item": "Tomatoes", "quantity": 500, "unit_cost": 3.2})
    buyer.move(buyer.get(small.id), 0)
    buyer.reset()
    assert buyer.get(small.id).data["status"] == "Approved"
    with pytest.raises(FieldErrors, match=r"approved at \$1,600.00, so its total"):
        buyer.update(buyer.get(small_line.id), {"quantity": 5000})
    system = Engine(ws, by="a rule")  # sent back for approval, it starts over
    system.update(system.get(small.id), {"status": "Draft"})
    buyer.reset()
    buyer.update(buyer.get(small_line.id), {"quantity": 600})
    assert "_approved" not in buyer.get(small.id).data


def test_a_rep_who_leaves_themselves_off_their_order_still_sees_it(app, owner):
    user, ws = owner
    _built(ws, ORDERS + [{"op": "add_field", "table": "orders", "field": {"key": "raised_by", "label": "Raised by", "type": "user"}}])
    luca = _person(ws, "luca@example.com", "Luca", "editor")
    rep = _as(ws, luca)
    order = rep.create("orders", {"customer": "Bar Milano", "total": 300})  # Sales rep and Raised by left empty
    assert order.data["rep"] == luca.id and order.data["raised_by"] == luca.id and rep.allowed(rep.get(order.id))
    marco = _as(ws, user)
    theirs = marco.create("orders", {"customer": "Nonna's", "rep": luca.id})
    assert theirs.data["raised_by"] == user.id and theirs.data["rep"] == luca.id  # what's filled in is kept
    assert marco.create("orders", {"customer": "Cafe"}).data.get("rep") is None  # the owner isn't who the list is limited for


def test_a_change_that_reprices_what_was_already_sent_says_so_and_keeps_it_in_the_history(app, owner):
    user, ws = owner
    invoices = [
        {"op": "create_table", "table": "invoices", "label": "Invoices", "fields": [
            {"key": "number", "type": "autonumber", "prefix": "INV-"},
            {"key": "subtotal", "type": "rollup", "aggregate": "sum", "table": "invoice_lines", "field": "amount", "via": "invoice"},
            {"key": "gst", "label": "GST", "type": "formula", "format": "money", "expression": "round(subtotal * 0.1, 2)"},
            {"key": "total", "type": "formula", "format": "money", "expression": "subtotal + gst"},
            {"key": "status", "type": "select", "options": ["Draft", "Sent", "Paid"], "default": "Draft"}],
         "workflow": {"field": "status", "transitions": [{"from": ["Draft"], "to": "Sent", "label": "Send"}]}},
        {"op": "create_table", "table": "invoice_lines", "label": "Invoice lines", "embedded": True, "fields": [
            {"key": "invoice", "type": "link", "table": "invoices", "required": True}, {"key": "item", "type": "text"},
            {"key": "gst_free", "label": "GST free", "type": "checkbox"}, {"key": "amount", "type": "money"}]}]
    _built(ws, invoices)
    e = Engine(ws, by="Sofia")
    sent, draft = e.create("invoices", {}), e.create("invoices", {})
    for inv in (sent, draft):
        e.create("invoice_lines", {"invoice": inv.id, "item": "Olive oil", "amount": 650})
        e.create("invoice_lines", {"invoice": inv.id, "item": "Tomatoes", "amount": 90, "gst_free": True})
    e.move(e.get(sent.id), 0)
    changes = []
    fix = ops.apply(ws.blueprint, [
        {"op": "add_field", "table": "invoices", "field": {"key": "taxed", "type": "rollup", "aggregate": "sum", "table": "invoice_lines",
                                                           "field": "amount", "via": "invoice", "where": [{"field": "gst_free", "op": "not_equals", "value": True}]}},
        {"op": "update_field", "table": "invoices", "field": "gst", "changes": {"expression": "round(taxed * 0.1, 2)"}}])
    assert not fix.problems, fix.problems
    chat.save_version(ws, fix.blueprint, changes, "chat")
    said = next(c for c in changes if c.startswith("Amounts changed on"))
    assert "2 invoices you already have" in said and said.index("INV-0001") < said.index("INV-0002")  # the sent one first
    assert "INV-0001's GST $74.00 → $65.00, Total $814.00 → $805.00" in said and "it is past Draft, so check any copy already sent" in said
    from app.models import RecordChange
    kept = RecordChange.query.filter_by(record_id=sent.id, by="the builder (worked out differently since the app changed)").one()
    assert kept.changes == {"gst": [74.0, 65.0], "total": [814.0, 805.0]}


def test_a_stage_left_to_someone_elses_button_under_the_limit_is_found(app, owner):
    from app.builder import checks
    _, ws = owner
    _built(ws, [{"op": "create_table", "table": "purchase_orders", "label": "Purchase orders", "fields": [
        {"key": "supplier", "type": "text", "required": True}, {"key": "total", "type": "money"},
        {"key": "status", "type": "select", "options": ["Draft", "Waiting for approval", "Approved"], "default": "Draft"}],
        "workflow": {"field": "status", "transitions": [
            {"from": ["Draft"], "to": "Waiting for approval", "label": "Send for approval", "only_if": [{"field": "total", "op": "greater_than", "value": 5000}]},
            {"from": ["Draft"], "to": "Approved", "label": "Approve", "who": ["owner"], "only_if": [{"field": "total", "op": "at_most", "value": 5000}]},
            {"from": ["Waiting for approval"], "to": "Approved", "label": "Approve", "who": ["owner"]}]}}])
    found = [i for i in checks.run(Engine(ws)) if "only when" in i["plain"]]
    assert len(found) == 1 and "from “Draft” only when Total is more than $5,000" in found[0]["plain"] and "“Approve” (owner)" in found[0]["plain"], found


# The furniture maker: starting a work order was to take its product's parts out of stock. The AI asked for "the bill
# of materials of the work order's product", a lookup from its order, about 25 times; only a link was taken, so the rule
# was never built. Her quality checks saved as Fail never sent the work back either: the rule waited for a change to Fail.
WORKSHOP = [
    {"op": "create_table", "table": "products", "label": "Products", "fields": [{"key": "name", "type": "text", "required": True}]},
    {"op": "create_table", "table": "components", "label": "Parts", "fields": [{"key": "name", "type": "text", "required": True},
        {"key": "on_hand", "label": "On hand", "type": "rollup", "aggregate": "sum", "table": "stock_movements", "field": "quantity", "via": "component"}]},
    {"op": "create_table", "table": "bom_lines", "label": "Bill of materials", "fields": [
        {"key": "product", "type": "link", "table": "products", "required": True},
        {"key": "component", "type": "link", "table": "components", "required": True}, {"key": "quantity", "type": "number"}]},
    {"op": "create_table", "table": "stock_movements", "label": "Stock movements", "fields": [
        {"key": "component", "type": "link", "table": "components", "required": True}, {"key": "quantity", "type": "number"},
        {"key": "reason", "type": "text"}]},
    {"op": "create_table", "table": "orders", "label": "Orders", "fields": [
        {"key": "customer", "type": "text", "required": True}, {"key": "product", "type": "link", "table": "products"}]},
    {"op": "create_table", "table": "work_orders", "label": "Work orders", "fields": [
        {"key": "order", "type": "link", "table": "orders", "required": True},
        {"key": "product", "type": "lookup", "link": "order", "field": "product"},
        {"key": "status", "type": "select", "options": ["Planned", "In progress", "Rework", "Done"], "default": "Planned"}],
     "workflow": {"field": "status", "transitions": [{"from": ["Planned", "Rework"], "to": "In progress", "label": "Start build"},
                                                     {"from": ["In progress"], "to": "Done", "label": "Done"}]}},
    {"op": "create_table", "table": "qc_checks", "label": "Quality checks", "fields": [
        {"key": "work_order", "type": "link", "table": "work_orders", "required": True},
        {"key": "result", "type": "select", "options": ["Pass", "Fail"]}]},
    {"op": "add_automation", "automation": {
        "key": "issue", "name": "Start build takes the parts out", "trigger": {"type": "record_updated", "table": "work_orders"},
        "conditions": [{"field": "status", "op": "changed_to", "value": "In progress"}],
        "actions": [{"type": "create_for_each", "source": {"table": "bom_lines", "via": "product", "of": "product"}, "table": "stock_movements",
                     "values": {"component": "{{item.component}}", "quantity": "{{0 - item.quantity}}", "reason": "Work order"}}]}},
    {"op": "add_automation", "automation": {
        "key": "rework", "name": "A failed check sends it back", "trigger": {"type": "record_updated", "table": "qc_checks"},
        "conditions": [{"field": "result", "op": "changed_to", "value": "Fail"}],
        "actions": [{"type": "update_record", "target": "work_order", "values": {"status": "Rework"}}]}},
]


def test_a_rule_goes_through_the_parts_of_a_product_shown_from_another_record_and_a_check_entered_as_failed_counts(app, owner):
    _, ws = owner
    result = _built(ws, WORKSHOP)
    assert "“A failed check sends it back” now also starts when a quality check is added with Result already Fail" in " ".join(result.changes)
    by_path = json.loads(json.dumps(WORKSHOP).replace('"of": "product"', '"of": "order.product"'))
    path = ops.apply(schema.empty("Joinery"), by_path)
    assert not path.problems, path.problems  # or the path through its links, said either way
    assert path.blueprint["automations"][0]["actions"][0]["source"]["of"] == "order.product"
    e = Engine(ws, by="Minh")
    table, legs = e.create("products", {"name": "Dining table"}), e.create("components", {"name": "Leg"})
    board = e.create("components", {"name": "Timber board"})
    e.create("bom_lines", {"product": table.id, "component": legs.id, "quantity": 4})
    e.create("bom_lines", {"product": table.id, "component": board.id, "quantity": 3})
    for c in (legs, board):
        e.create("stock_movements", {"component": c.id, "quantity": 40})
    wo = e.create("work_orders", {"order": e.create("orders", {"customer": "Sarah", "product": table.id}).id})
    e.move(e.get(wo.id), 0)
    e.reset()
    assert (e.value(e.get(legs.id), "on_hand"), e.value(e.get(board.id), "on_hand")) == (36, 37)
    e.create("qc_checks", {"work_order": wo.id, "result": "Fail"})
    e.reset()
    assert e.get(wo.id).data["status"] == "Rework"


def test_a_check_that_takes_the_stock_out_by_hand_proves_nothing_and_a_payment_someone_records_still_counts(app, owner):
    _, ws = owner
    design = [op for op in WORKSHOP if op.get("automation", {}).get("key") != "issue"]
    _built(ws, design + [{"op": "create_table", "table": "invoices", "label": "Invoices", "fields": [
        {"key": "customer", "type": "text", "required": True}, {"key": "total", "type": "money"},
        {"key": "paid", "type": "rollup", "aggregate": "sum", "table": "payments", "field": "amount", "via": "invoice"},
        {"key": "owing", "type": "formula", "format": "money", "expression": "total - paid"},
        {"key": "status", "type": "select", "options": ["Draft", "Sent"], "default": "Draft"}],
        "workflow": {"field": "status", "transitions": [{"from": ["Draft"], "to": "Sent", "label": "Send"}]}},
        {"op": "create_table", "table": "payments", "label": "Payments", "fields": [
            {"key": "invoice", "type": "link", "table": "invoices", "required": True}, {"key": "amount", "type": "money"}]}])
    stock = {"title": "Starting a work order for a dining table takes 4 legs out of stock", "steps": [
        {"do": "add", "as": "p", "table": "products", "values": {"name": "Dining table"}},
        {"do": "add", "as": "leg", "table": "components", "values": {"name": "Leg"}},
        {"do": "add", "as": "b", "table": "bom_lines", "values": {"product": "p", "component": "leg", "quantity": 4}},
        {"do": "add", "as": "in", "table": "stock_movements", "values": {"component": "leg", "quantity": 10}},
        {"do": "add", "as": "o", "table": "orders", "values": {"customer": "Sarah", "product": "p"}},
        {"do": "add", "as": "w", "table": "work_orders", "values": {"order": "o"}},
        {"do": "move", "as": "", "record": "w", "to": "In progress"},
        {"do": "add", "as": "out", "table": "stock_movements", "values": {"component": "leg", "quantity": -4}}],
        "expect": [{"record": "leg", "field": "on_hand", "equals": 6}]}
    paid = {"title": "Recording a $500 payment on a $869 invoice leaves $369 owing", "steps": [
        {"do": "add", "as": "i", "table": "invoices", "values": {"customer": "Bar Milano", "total": 869}},
        {"do": "move", "as": "", "record": "i", "to": "Sent"},
        {"do": "add", "as": "p1", "table": "payments", "values": {"invoice": "i", "amount": 500}}],
        "expect": [{"record": "i", "field": "owing", "equals": 369}]}
    _, (by_hand, payment) = trial.try_out(ws, [stock, paid], screens=False)
    assert not by_hand["ok"] and by_hand["by_hand"] == ["Stock movements"] and "didn't add the stock movements itself" in by_hand["plain"]
    assert payment["ok"] and "by_hand" not in payment


def test_the_builder_doesnt_add_a_recipes_parts_twice_and_undo_takes_back_what_it_added(app, owner):
    from app.models import Message, utcnow
    _, ws = owner
    _built(ws, WORKSHOP)
    first = [{"op": "add_records", "table": "products", "records": [{"name": "Dining table"}]},
             {"op": "add_records", "table": "components", "records": [{"name": "Leg"}, {"name": "Tabletop"}]},
             {"op": "add_records", "table": "bom_lines", "records": [{"product": "Dining table", "component": "Leg", "quantity": 4}]}]
    chat._data(ws, first)
    turn = Message(workspace_id=ws.id, channel="builder", role="assistant", status="done", started_at=utcnow())
    db.session.add(turn)
    db.session.commit()
    said, _ = chat._data(ws, [{"op": "add_records", "table": "bom_lines", "records": [
        {"product": "Dining table", "component": "Leg", "quantity": 4, "note": "Four legs"},
        {"product": "Dining table", "component": "Tabletop", "quantity": 1}]}])
    turn.finished_at = utcnow()
    db.session.commit()
    assert "1 was already there, so not added again" in said[0]
    e = Engine(ws)
    assert len(e.rows("bom_lines")) == 2  # the legs once, the new tabletop line
    binned, kept = chat.unmade(ws, turn)
    assert binned == ["bill of material “Dining table · Tabletop”"] and kept == []
    e.reset()
    assert [e.title(r) for r in e.rows("bom_lines")] == ["Dining table · Leg"]  # what the earlier turn added stays


def test_what_people_mustnt_see_is_kept_from_them_or_said_and_a_list_never_hides_its_records(app, owner, client):
    _, ws = owner
    asked = "The makers shouldn't see prices, margins or customer details - they just see their work orders and log their hours."
    _built(ws, WORKSHOP)
    found = chat._kept_as_asked(asked, ws.blueprint)
    assert found and found[0]["level"] == "fix" and "everyone you invite to do the work can still see all of it" in found[0]["plain"]
    assert "The makers shouldn't see prices" in found[0]["plain"]
    kept = _built(ws, [{"op": "set_team", "team": {"key": "office", "name": "Office"}},
                       {"op": "update_table", "table": "orders", "changes": {"access": {"teams": ["office"]}}}])
    assert chat._kept_as_asked(asked, ws.blueprint) == [] and chat._kept_as_asked("Track our jobs.", ws.blueprint) == []

    _built(ws, [{"op": "add_view", "table": "stock_movements", "view": {"key": "issued", "label": "Issued to jobs", "type": "list",
                                                                        "filters": [{"field": "reason", "op": "equals", "value": "Work order"}]}}])
    bp = json.loads(json.dumps(ws.blueprint))
    stock = next(t for t in bp["tables"] if t["key"] == "stock_movements")
    stock["views"] = [v for v in stock["views"] if v.get("filters")]
    assert [v["label"] for v in stock["views"]] == ["Issued to jobs"]
    chat.save_version(ws, bp, ["only the filtered tab"], "chat")
    e = Engine(ws)
    leg = e.create("components", {"name": "Leg"})
    e.create("stock_movements", {"component": leg.id, "quantity": 40, "reason": "Purchase"})
    login(client)
    page = client.get(f"/w/{ws.id}/app/t/stock_movements").get_data(as_text=True)
    assert "Issued to jobs" in page and "view=_all" in page
    assert "Purchase" in client.get(f"/w/{ws.id}/app/t/stock_movements?view=_all").get_data(as_text=True)


def test_the_same_document_made_twice_is_kept_once_and_checks_say_nothing_is_checked_when_nothing_is(app, owner, client):
    from app.documents import produce
    from app.models import File
    _, ws = owner
    _built(ws, [{"op": "create_table", "table": "quotes", "label": "Quotes", "fields": [
        {"key": "customer", "type": "text", "required": True}, {"key": "total", "type": "money"}]},
        {"op": "add_document", "table": "quotes", "document": {"key": "quote", "label": "Quote", "title": "Quote", "totals": ["total"]}}])
    e = Engine(ws, by="Kate")
    q = e.create("quotes", {"customer": "Sarah", "total": 1319.40})
    first, again = produce(e, q, "quote"), produce(e, e.get(q.id), "quote")
    assert first.id == again.id and File.query.filter_by(record_id=q.id).count() == 1
    e.update(e.get(q.id), {"total": 1400})
    assert produce(e, e.get(q.id), "quote").id != first.id  # changed: a new copy

    login(client)
    page = client.get(f"/w/{ws.id}/app/checks").get_data(as_text=True)
    assert "Everything checked works" not in page and "No checks yet" in page
    ws.state = {**(ws.state or {}), "refused_checks": [{"said": "Starting a build takes the boards out", "why": "it names no button", "at": "x"}]}
    db.session.commit()
    page = client.get(f"/w/{ws.id}/app/checks").get_data(as_text=True)
    assert "Couldn't turn “Starting a build takes the boards out” into a check: it names no button" in page


# The property manager: "Mel does the books, she shouldn't change leases; Jess and Raj only their own houses". Mel then
# saw no leases at all (each manager's own, and she manages none), so she couldn't record rent; the commission's
# default of 10 showed as 1000% on every new owner.
RENTALS = [
    {"op": "set_team", "team": {"key": "finance", "name": "Finance"}},
    {"op": "set_team", "team": {"key": "managers", "name": "Property managers"}},
    {"op": "create_table", "table": "owners", "label": "Owners", "fields": [
        {"key": "name", "type": "text", "required": True}, {"key": "commission", "type": "percent", "default": 10, "max": 100}]},
    {"op": "create_table", "table": "leases", "label": "Leases", "row_access": {"field": "manager", "roles": ["editor"]},
     "access": {"view": "editor", "edit": "admin"}, "fields": [
        {"key": "number", "type": "autonumber", "prefix": "L-"}, {"key": "tenant", "type": "text", "required": True},
        {"key": "manager", "type": "user"}]},
    {"op": "create_table", "table": "payments", "label": "Payments", "access": {"teams": ["finance", "managers"]}, "fields": [
        {"key": "lease", "type": "link", "table": "leases", "required": True}, {"key": "amount", "type": "money"}]},
]


def test_whoever_records_payments_can_pick_the_lease_and_a_percent_default_is_a_percent(app, owner):
    from app.builder import checks
    _, ws = owner
    _built(ws, RENTALS)
    commission = schema.field(ws.blueprint["tables"][0], "commission")
    assert commission["default"] == 0.1 and commission["max"] == 1
    found = [i for i in checks.run(Engine(ws)) if i["where"] == "payments"]
    assert any(i["level"] == "check" and i["plain"].startswith("Finance can add payments, but see only the leases where they're the manager")
               for i in found), found
    _built(ws, [{"op": "update_table", "table": "leases", "changes": {"row_access": {"field": "manager", "roles": ["editor"], "teams": ["finance"]}}}])
    assert not [i for i in checks.run(Engine(ws)) if i["where"] == "payments" and "Finance" in i["plain"]]
    jess = _person(ws, "jess@example.com", "Jess", "editor", teams=["managers"])
    mel = _person(ws, "mel@example.com", "Mel", "editor", teams=["finance"])
    e = Engine(ws)
    mine, raj = e.create("leases", {"tenant": "Danny", "manager": jess.id}), e.create("leases", {"tenant": "Tom"})
    assert [r.id for r in _as(ws, mel).visible_rows("leases")] == [mine.id, raj.id]  # the books: all of them
    assert [r.id for r in _as(ws, jess).visible_rows("leases")] == [mine.id]  # a manager: her own
    _built(ws, [{"op": "update_table", "table": "leases", "changes": {"access": {"view": "admin", "edit": "admin"}}}])
    assert any(i["level"] == "fix" and "can't see any leases" in i["plain"] for i in checks.run(Engine(ws)))


def test_a_rule_rewritten_for_a_change_still_starts_when_one_is_added(app, owner):
    _, ws = owner
    _built(ws, [{"op": "create_table", "table": "payments", "label": "Payments", "fields": [
        {"key": "amount", "type": "money"}, {"key": "status", "type": "select", "options": ["Received", "Banked"]}]},
        {"op": "add_automation", "automation": {"key": "paid", "name": "Mark the lease paid", "trigger": {"type": "record_created", "table": "payments"},
                                                "actions": [{"type": "notify", "to": "admin", "message": "Paid"}]}}])
    result = _built(ws, [{"op": "update_automation", "automation": {
        "key": "paid", "name": "Mark the lease paid", "trigger": {"type": "record_updated", "table": "payments"},
        "conditions": [{"field": "status", "op": "equals", "value": "Received"}], "actions": [{"type": "notify", "to": "admin", "message": "Paid"}]}}])
    assert ws.blueprint["automations"][0]["trigger"]["type"] == "record_created_or_updated"
    assert "“Mark the lease paid” still starts when a new payment is added, as it did" in result.changes


def test_a_few_loose_words_that_cant_be_built_are_asked_about_and_a_part_that_keeps_failing_isnt_carried_on_for_ever(app, owner):
    from app import ai
    from app.models import Message
    from .conftest import ScriptedAI
    _, ws = owner
    _built(ws, RENTALS)
    bad = {"ops": [{"op": "add_field", "table": "nope", "field": {"key": "x", "type": "text"}}], "reply": "Done"}
    ask = {"question": "What should it do for owners: send each a monthly statement with the fee and repairs taken off, or let them see their houses online?"}
    ai.set_backend(ScriptedAI(bad, bad, bad, ask))
    reply = chat.ask(ws, "can u make it do the usual stuff for owners")
    chat.work(reply.id)
    m = db.session.get(Message, reply.id)
    assert m.status == "done" and m.text.startswith("What should it do for owners") and ws.version == 1  # nothing changed

    assert chat._tries(ws, ["the owner statement"]) == []
    assert chat._tries(ws, ["the owner statement"]) == ["the owner statement"]  # the second time in a row: ask, don't carry on
    ai.set_backend(ScriptedAI(ai.AIError("no closing reply")))  # the plain one, put together without the AI
    said = chat._closing_reply("statements", ["I added the fee."], ["Added Fee"], ["the owner statement"], [], ["the owner statement"])
    assert "Carry on building" not in said and "one small part of it" in said


def test_an_approval_says_why_it_asks_a_rule_emailing_outside_needs_an_admin_to_run_early_and_buttons_read_right(app, owner, client):
    user, ws = owner
    _built(ws, [{"op": "create_table", "table": "jobs", "label": "Maintenance requests", "fields": [
        {"key": "number", "type": "autonumber", "prefix": "M-"}, {"key": "issue", "type": "text", "required": True},
        {"key": "quote", "type": "money"}, {"key": "notes", "type": "long_text"},
        {"key": "status", "type": "select", "options": ["New", "Waiting for approval", "Assigned", "Issues found"], "default": "New"}],
        "workflow": {"field": "status", "transitions": [
            {"from": ["New"], "to": "Waiting for approval", "label": "Assign tradie"},
            {"from": ["Waiting for approval"], "to": "Assigned", "label": "Approve", "who": ["owner"]},
            {"from": ["New"], "to": "Issues found", "label": "Issues found", "requires": ["notes"]}]}},
        {"op": "add_automation", "automation": {"key": "big", "name": "Big jobs need an OK", "trigger": {"type": "record_updated", "table": "jobs"},
         "conditions": [{"field": "status", "op": "changed_to", "value": "Waiting for approval"}],
         "actions": [{"type": "branch", "paths": [{"label": "Over $500", "when": [{"field": "quote", "op": "greater_than", "value": 500}],
                                                   "steps": [{"type": "request_approval", "approver": "owner", "message": "Please approve this."}]}],
                      "otherwise": [{"type": "update_record", "values": {"status": "Assigned"}}]}]}},
        {"op": "add_automation", "automation": {"key": "statements", "name": "Send monthly owner statements",
         "trigger": {"type": "schedule", "table": "jobs", "every": "month", "day": 1},
         "actions": [{"type": "send_email", "to": "someone@example.com", "subject": "Statement", "body": "Hi"}]}}])
    e = Engine(ws, by="Jess")
    job = e.create("jobs", {"issue": "Hot water busted", "quote": 900})
    assert Engine(ws).move_problem(ws.blueprint["tables"][0], job, ws.blueprint["tables"][0]["workflow"]["transitions"][2]) == \
        "Fill in Notes first, then you can press “Issues found”"
    e.move(e.get(job.id), 0)
    assert Approval.query.one().message == "Please approve this. (maintenance request “M-0001”: Over $500)"
    jess = _person(ws, "jess@example.com", "Jess", "editor")
    client.post("/login", data={"email": "jess@example.com", "password": "password1234"})
    assert client.post(f"/w/{ws.id}/app/automations/statements/run").status_code == 403
