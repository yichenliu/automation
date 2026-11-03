"""Combined lists: one list put together from several (a stock outlook from what's moved, what's on order and what's
promised), kept by the platform as they change, read like any list and never changed by hand."""
from app.blueprint import schema
from app.engine.records import Engine, FieldErrors
from app.models import Record

from .conftest import login
from .test_wave2 import _built, _person

OUTLOOK = [
    {"op": "create_table", "table": "items", "label": "Items", "fields": [{"key": "name", "type": "text", "required": True}]},
    {"op": "create_table", "table": "movements", "label": "Stock movements", "fields": [
        {"key": "item", "type": "link", "table": "items", "required": True}, {"key": "quantity", "type": "number"}, {"key": "on", "type": "date"}]},
    {"op": "create_table", "table": "po_lines", "label": "Purchase lines", "fields": [
        {"key": "item", "type": "link", "table": "items", "required": True}, {"key": "outstanding", "type": "number"},
        {"key": "expected", "type": "date"}, {"key": "confirmed", "type": "checkbox"}]},
    {"op": "create_table", "table": "so_lines", "label": "Order lines", "fields": [
        {"key": "item", "type": "link", "table": "items", "required": True}, {"key": "quantity", "type": "number"},
        {"key": "ship_by", "type": "date"}, {"key": "status", "type": "select", "options": ["Open", "Closed"], "default": "Open"}]},
    {"op": "create_table", "table": "outlook", "label": "Stock outlook", "fields": [
        {"key": "kind", "type": "select", "options": ["Committed", "Planned in", "Planned out"], "required": True},
        {"key": "item", "type": "link", "table": "items", "required": True}, {"key": "on", "type": "date"}, {"key": "quantity", "type": "number"}],
     "combines": [
        {"table": "movements", "values": {"kind": "Committed", "item": "{{record.item}}", "on": "{{record.on}}", "quantity": "{{record.quantity}}"}},
        {"table": "po_lines", "where": [{"field": "confirmed", "op": "equals", "value": True}],
         "values": {"kind": "Planned in", "item": "{{record.item}}", "on": "{{record.expected}}", "quantity": "{{record.outstanding}}"}},
        {"table": "so_lines", "where": [{"field": "status", "op": "equals", "value": "Open"}],
         "values": {"kind": "Planned out", "item": "{{record.item}}", "on": "{{record.ship_by}}", "quantity": "{{0 - record.quantity}}"}}]},
]


def _outlook(e):
    e.reset()
    return sorted((r.data["kind"], r.data.get("quantity")) for r in e.rows("outlook"))


def test_a_combined_list_follows_the_lists_it_is_put_together_from(app, owner):
    user, ws = owner
    _built(ws, OUTLOOK[:4])
    e = Engine(ws)
    flour = e.create("items", {"name": "Flour"})
    e.create("movements", {"item": flour.id, "quantity": 40, "on": "2026-11-01"})  # there before the combined list was made
    result = _built(ws, OUTLOOK[4:])
    assert "Put together Stock outlook from Stock movements, Purchase lines, Order lines: 1 record" in result.changes
    e = Engine(ws)  # as each request has: one that knows the combined list
    assert _outlook(e) == [("Committed", 40)]
    po = e.create("po_lines", {"item": flour.id, "outstanding": 100, "expected": "2026-11-10"})
    assert _outlook(e) == [("Committed", 40)]  # not confirmed yet: left out
    e.update(e.get(po.id), {"confirmed": True})
    so = e.create("so_lines", {"item": flour.id, "quantity": 30, "ship_by": "2026-11-05"})
    assert _outlook(e) == [("Committed", 40), ("Planned in", 100), ("Planned out", -30)]
    e.update(e.get(po.id), {"outstanding": 60})
    e.update(e.get(so.id), {"status": "Closed"})  # shipped: no longer promised
    assert _outlook(e) == [("Committed", 40), ("Planned in", 60)]
    e.delete(e.get(po.id))
    assert _outlook(e) == [("Committed", 40)]
    e.restore(Record.query.get(po.id))
    assert _outlook(e) == [("Committed", 40), ("Planned in", 60)]


def test_nobody_changes_a_combined_list_by_hand(app, client, owner):
    user, ws = owner
    _built(ws, OUTLOOK)
    e = Engine(ws)
    flour = e.create("items", {"name": "Flour"})
    e.create("movements", {"item": flour.id, "quantity": 5})
    try:
        e.create("outlook", {"kind": "Committed", "item": flour.id})
        raise AssertionError("a combined list took a record by hand")
    except FieldErrors as exc:
        assert "put together from Stock movements, Purchase lines, Order lines" in exc.errors["_"]
    row = e.rows("outlook")[0]
    try:
        e.update(row, {"quantity": 99})
        raise AssertionError("a combined record was changed by hand")
    except FieldErrors:
        pass
    login(client)
    page = client.get(f"/w/{ws.id}/app/t/outlook").get_data(as_text=True)
    assert "Put together from stock movements, purchase lines and order lines" in page and "New stock outlook" not in page
    assert client.get(f"/w/{ws.id}/app/t/outlook/new").status_code in (302, 403, 404)
    model = client.get(f"/w/{ws.id}/app/studio/data.json").get_json()
    assert {(c["from"], c["to"]) for c in model["combined"]} == {("movements", "outlook"), ("po_lines", "outlook"), ("so_lines", "outlook")}


def test_a_combined_list_must_fill_in_what_it_needs_and_name_real_lists(app, owner):
    user, ws = owner
    bad = [dict(op) for op in OUTLOOK]
    bad[4] = {**bad[4], "combines": [{"table": "nowhere", "values": {"kind": "Committed"}},
                                     {"table": "movements", "values": {"quantity": "{{record.nope}}"}}]}
    from app.blueprint import ops
    problems = ops.apply(ws.blueprint, bad).problems
    assert any("“nowhere” isn't another table" in p for p in problems)
    assert any("it must fill in Kind, Item" in p for p in problems)
    assert any("nope" in p for p in problems)


def test_a_design_with_a_combined_list_is_tried_out_without_complaint(app, owner):
    from app.builder import trial
    user, ws = owner
    _built(ws, OUTLOOK)
    walked, _ = trial.try_out(ws, [], screens=True)
    assert not walked["crashes"], walked["crashes"]
    assert not walked["issues"], [i["plain"] for i in walked["issues"]]
    assert walked["made"].get("outlook")  # it filled itself from the samples, and its page was opened
