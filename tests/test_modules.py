"""Ready-made parts: Inventory, Orders, Procurement and Production, alone and together, tried out, and stock moving
through them as a business would move it."""
import json

import pytest

from app.blueprint import modules, schema
from app.builder import trial
from app.engine.records import Engine
from app.models import FlowRun, Workspace
from app.web.workspaces import with_parts

from .conftest import login
from .test_wave2 import _as, _person


@pytest.mark.parametrize("keys", [["inventory"], ["orders"], ["procurement"], ["production"], list(modules.MODULES)])
def test_each_part_alone_and_together_is_sound_and_tried_out(app, owner, keys):
    _, ws = owner
    assert all("do" in p for p in modules.ops_for(keys))  # written in the Keel language
    result = with_parts(ws.blueprint, keys)
    assert not result.problems, result.problems
    ws.blueprint = result.blueprint
    walked, _ = trial.try_out(ws, [], screens=True)
    assert not walked["crashes"], walked["crashes"]
    assert not walked["issues"], [i["plain"] for i in walked["issues"]]


def test_stock_moves_through_buying_selling_making_and_counting(app, client, owner):
    user, ws = owner
    login(client)
    client.post("/workspaces/new", data={"name": "Harbour Foods", "start": "parts", "parts": list(modules.MODULES)})
    ws = Workspace.query.filter_by(name="Harbour Foods").one()
    e = Engine(ws)
    shelf = e.create("locations", {"name": "Main warehouse", "code": "WH1"})
    flour = e.create("products", {"name": "Flour 25kg", "sku": "FL25", "reorder_point": 5})
    loaf = e.create("products", {"name": "Bread loaf", "sku": "BR1"})
    mill = e.create("suppliers", {"name": "Mill Co"})
    po = e.create("purchase_orders", {"supplier": mill.id})
    line = e.create("po_lines", {"purchase_order": po.id, "product": flour.id, "quantity": 10, "unit_cost": 30})
    e.move(e.get(po.id), next(i for i, m, _ in e.moves(e.get(po.id)) if m["to"] == "Sent"))
    base = f"/w/{ws.id}/app"
    run_url = client.post(f"{base}/flows/receive_po/start", data={"record": po.id}).headers["Location"]
    client.post(run_url, data={"go": "next"})
    client.post(run_url, data={"go": "next", "answer": json.dumps({"counts": {str(line.id): 8}, "reason": "Two sacks short"})})
    client.post(run_url, data={"choice": "0"})  # Received
    e.reset()
    assert e.get(po.id).data["status"] == "Received" and e.get(po.id).data["short_note"] == "Two sacks short"
    assert e.value(e.get(flour.id), "on_hand") == 8  # what was counted in, not what was ordered

    cafe = e.create("customers", {"name": "Cafe Uno"})
    order = e.create("sales_orders", {"customer": cafe.id, "carrier": "AusPost"})
    e.create("order_lines", {"order": order.id, "product": flour.id, "quantity": 3, "unit_price": 45})
    for stage in ("Picking", "Packed", "Shipped"):
        e.move(e.get(order.id), next(i for i, m, _ in e.moves(e.get(order.id)) if m["to"] == stage))
    e.reset()
    assert e.value(e.get(flour.id), "on_hand") == 5 and e.value(e.get(flour.id), "low")  # down to its reorder point

    wo = e.create("work_orders", {"product": loaf.id, "quantity": 20})
    e.create("wo_materials", {"work_order": wo.id, "material": flour.id, "quantity": 2})
    for stage in ("In progress", "Done"):
        e.move(e.get(wo.id), next(i for i, m, _ in e.moves(e.get(wo.id)) if m["to"] == stage))
    e.reset()
    assert (e.value(e.get(flour.id), "on_hand"), e.value(e.get(loaf.id), "on_hand")) == (3, 20)

    run_url = client.post(f"{base}/flows/stock_count/start").headers["Location"]
    client.post(run_url, data={"go": "next", "location": str(shelf.id)})
    client.post(run_url, data={"go": "next", "answer": json.dumps({"scanned": [{"id": flour.id, "qty": 1}, {"id": flour.id, "qty": 1},
                                                                                {"id": loaf.id, "qty": 20}]})})
    client.post(run_url, data={"choice": "0"})  # finished: correct the stock
    e.reset()
    assert FlowRun.query.order_by(FlowRun.id.desc()).first().status == "done"
    assert (e.value(e.get(flour.id), "on_hand"), e.value(e.get(loaf.id), "on_hand")) == (2, 20)  # one sack found missing


def test_parts_are_added_to_an_app_that_has_some_already(app, client, owner):
    user, ws = owner
    result = with_parts(ws.blueprint, ["orders"])
    from app.builder import chat
    chat.save_version(ws, result.blueprint, result.changes, "parts", by=user.id)
    login(client)
    page = client.get(f"/w/{ws.id}/app/design").get_data(as_text=True)
    assert "Add ready-made parts" in page and "Inventory &amp; warehouse" in page and "Orders &amp; fulfilment" not in page
    client.post(f"/w/{ws.id}/app/parts", data={"parts": ["inventory"]})
    keys = {a["key"] for a in ws.blueprint["automations"]}
    assert {"count_done", "shipped_takes_stock", "return_received"} <= keys  # what joins them comes too
    assert "on_hand" in {f["key"] for f in schema.table(ws.blueprint, "products")["fields"]}
    assert not schema.check(ws.blueprint)


def test_a_big_purchase_order_is_approved_by_someone_else_and_goes_back_to_draft_if_turned_down(app, owner):
    _, ws = owner
    bp = with_parts(ws.blueprint, ["procurement"]).blueprint
    ask = next(a for a in bp["automations"] if a["key"] == "approve_po")["actions"][0]
    assert ask["type"] == "request_approval" and ask["not_by"] == ["creator"]
    assert ask["if_rejected"] == [{"type": "update_record", "values": {"status": "Draft"}}]
    moves = schema.table(bp, "purchase_orders")["workflow"]["transitions"]
    assert not any(mv["to"] == "Approved" for mv in moves)  # only the approval puts it there, never a button
    assert schema.table(bp, "po_lines")["title_field"] == "purchase_order" and schema.table(bp, "purchase_orders")["views"][0]["columns"][:2] == ["number", "supplier"]
