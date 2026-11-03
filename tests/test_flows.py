"""Flows: work done a screen at a time (receiving a delivery, a stock count), saved together at the end."""
import json

from app.blueprint import schema
from app.engine import flows
from app.engine.records import Engine
from app.models import File, FlowRun, Run

from .conftest import login
from .test_wave2 import _as, _built, _person

PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="

WAREHOUSE = [
    {"op": "create_table", "table": "products", "label": "Products", "fields": [
        {"key": "name", "type": "text", "required": True}, {"key": "sku", "label": "SKU", "type": "barcode"}]},
    {"op": "create_table", "table": "purchase_orders", "label": "Purchase orders", "fields": [
        {"key": "supplier", "type": "text", "required": True}, {"key": "delivery_photo", "type": "image"},
        {"key": "status", "type": "select", "options": ["Sent", "Received", "Rejected"], "default": "Sent"},
        {"key": "short_note", "type": "text"}]},
    {"op": "create_table", "table": "po_lines", "label": "Order lines", "embedded": True, "fields": [
        {"key": "order", "type": "link", "table": "purchase_orders"}, {"key": "product", "type": "link", "table": "products"},
        {"key": "quantity", "type": "number"}, {"key": "received", "type": "number"}],
     "documents": [{"label": "Box label", "format": "label", "fields": ["quantity"]}]},
    {"op": "create_table", "table": "stock_counts", "label": "Stock counts", "fields": [
        {"key": "area", "type": "text", "required": True}, {"key": "counted_by", "type": "signature"}]},
    {"op": "create_table", "table": "count_lines", "label": "Counted", "embedded": True, "fields": [
        {"key": "count", "type": "link", "table": "stock_counts"}, {"key": "product", "type": "link", "table": "products"},
        {"key": "qty", "type": "number"}]},
    {"op": "add_flow", "flow": {
        "label": "Receive goods", "table": "purchase_orders", "when": [{"field": "status", "op": "equals", "value": "Sent"}],
        "steps": [
            {"kind": "info", "label": "Check the delivery", "text": "Delivery from **{{record.supplier}}**."},
            {"kind": "pick", "label": "Count it in", "lines": {"table": "po_lines"}, "target": "quantity", "count_into": "received",
             "code": "product.sku"},
            {"kind": "photo", "label": "Photo of the pallet", "field": "delivery_photo", "optional": True},
            {"kind": "decide", "label": "Accept it?", "field": "status",
             "options": [{"label": "Accept", "value": "Received"}, {"label": "Reject", "value": "Rejected", "finish": True}]},
            {"kind": "print", "label": "Box labels", "document": "box_label", "of": "po_lines"}],
        "then": [{"type": "update_record", "values": {"short_note": "{{flow.count_it_in.reason}}"}}]}},
    {"op": "add_flow", "flow": {
        "label": "Stock count", "table": "stock_counts", "start": "new", "again": True,
        "steps": [{"kind": "form", "label": "Where", "fields": ["area"]},
                  {"kind": "scan", "label": "Scan everything on the shelf", "into": {"table": "count_lines", "quantity": "qty"}},
                  {"kind": "signature", "label": "Sign it off", "field": "counted_by"}]}},
]


def test_a_flow_is_understood_and_checked(app, owner):
    _, ws = owner
    _built(ws, WAREHOUSE)
    receive, count = schema.flows(ws.blueprint)
    assert receive["start"] == "record" and [s["kind"] for s in receive["steps"]] == ["info", "pick", "photo", "decide", "print"]
    assert count["start"] == "new" and count["again"] and count["steps"][1]["duplicates"] == "count"
    wrong = schema.check({**ws.blueprint, "flows": [schema.normalize_flow({
        "label": "Broken", "table": "purchase_orders", "steps": [
            {"kind": "pick", "lines": {"table": "po_lines"}, "count_into": "product"},
            {"kind": "photo", "field": "supplier"}, {"kind": "decide", "field": "status", "options": ["Fine", "Not fine"]}]})]})
    assert any("must be a number field of Order lines" in p for p in wrong)
    assert any("must be an image or file field" in p for p in wrong)
    assert any("“Fine” isn't a choice of Status" in p for p in wrong)


def test_receiving_a_delivery_step_by_step(app, client, owner):
    user, ws = owner
    _built(ws, WAREHOUSE)
    e = Engine(ws)
    flour = e.create("products", {"name": "Flour 25kg", "sku": "FL25"})
    sugar = e.create("products", {"name": "Sugar 10kg", "sku": "SU10"})
    po = e.create("purchase_orders", {"supplier": "Mill Co"})
    a = e.create("po_lines", {"order": po.id, "product": flour.id, "quantity": 3})
    b = e.create("po_lines", {"order": po.id, "product": sugar.id, "quantity": 2})
    login(client)
    base = f"/w/{ws.id}/app"
    page = client.get(f"{base}/t/purchase_orders/{po.id}").get_data(as_text=True)
    assert "flows/receive_goods/start" in page  # a button on the order, while it's Sent
    go = client.post(f"{base}/flows/receive_goods/start", data={"record": po.id})
    run_url = go.headers["Location"]
    assert "Delivery from <strong>Mill Co</strong>" in client.get(run_url).get_data(as_text=True)
    client.post(run_url, data={"go": "next"})
    counting = client.get(run_url).get_data(as_text=True)
    assert "Count it in" in counting and "su10" in counting  # the lines, with the codes a scan finds them by
    short = client.post(run_url, data={"go": "next", "answer": json.dumps({"counts": {str(a.id): 3, str(b.id): 1}})})
    assert "say why before going on" in short.get_data(as_text=True)  # one line short: why, before going on
    client.post(run_url, data={"go": "next", "answer": json.dumps({"counts": {str(a.id): 3, str(b.id): 1}, "reason": "One sack split"})})
    client.post(run_url, data={"go": "next"})  # no photo: it's optional
    assert e.get(a.id).data.get("received") is None  # nothing saved yet
    done = client.post(run_url, data={"choice": "0"})
    assert "/done" in done.headers["Location"]
    e.reset()
    assert e.get(po.id).data["status"] == "Received"
    assert (e.get(a.id).data["received"], e.get(b.id).data["received"]) == (3, 1)
    assert e.get(po.id).data["short_note"] == "One sack split"  # the "then" steps read the flow's answers
    finished = client.get(done.headers["Location"]).get_data(as_text=True)
    assert "Box labels (2)" in finished  # each line's label, printed once it's saved
    assert Run.query.filter_by(automation="flow:receive_goods").one().status == "done"
    assert "flows/receive_goods/start" not in client.get(f"{base}/t/purchase_orders/{po.id}").get_data(as_text=True)  # it's Received now


def test_rejecting_ends_the_flow_there_and_stopping_saves_nothing(app, client, owner):
    user, ws = owner
    _built(ws, WAREHOUSE)
    e = Engine(ws)
    po = e.create("purchase_orders", {"supplier": "Mill Co"})
    login(client)
    base = f"/w/{ws.id}/app"
    run_url = client.post(f"{base}/flows/receive_goods/start", data={"record": po.id}).headers["Location"]
    for _ in range(3):
        client.post(run_url, data={"go": "next", "answer": json.dumps({"counts": {}})})
    client.post(run_url, data={"choice": "1"})  # Reject
    e.reset()
    assert e.get(po.id).data["status"] == "Rejected"
    po2 = e.create("purchase_orders", {"supplier": "Bakers"})
    run2 = client.post(f"{base}/flows/receive_goods/start", data={"record": po2.id}).headers["Location"]
    client.post(run2, data={"go": "next"})
    client.post(run2, data={"go": "cancel"})
    assert FlowRun.query.order_by(FlowRun.id.desc()).first().status == "cancelled"
    e.reset()
    assert e.get(po2.id).data["status"] == "Sent"


def test_a_stock_count_on_a_phone_scans_items_in_and_offers_another(app, client, owner):
    user, ws = owner
    _built(ws, WAREHOUSE)
    e = Engine(ws)
    flour = e.create("products", {"name": "Flour 25kg", "sku": "FL25"})
    sugar = e.create("products", {"name": "Sugar 10kg", "sku": "SU10"})
    login(client)
    base = f"/w/{ws.id}/app"
    assert "Step by step" in client.get(f"{base}/").get_data(as_text=True)  # in the menu
    assert "Stock count" in client.get(f"{base}/flows").get_data(as_text=True)
    run_url = client.post(f"{base}/flows/stock_count/start").headers["Location"]
    assert "is needed" in client.post(run_url, data={"go": "next", "area": ""}).get_data(as_text=True)
    client.post(run_url, data={"go": "next", "area": "Aisle 3"})
    run = FlowRun.query.one()
    assert client.get(f"{base}/flows/run/{run.id}/code?code=fl25").get_json() == {"id": flour.id, "title": "Flour 25kg"}
    assert client.get(f"{base}/flows/run/{run.id}/code?code=nope").status_code == 404
    client.post(run_url, data={"go": "next", "answer": json.dumps({"scanned": [
        {"id": flour.id, "qty": 1}, {"id": flour.id, "qty": 1}, {"id": sugar.id, "qty": 4}]})})  # the same twice: two of it
    assert "Sign in the box" in client.post(run_url, data={"go": "next"}).get_data(as_text=True)
    done = client.post(run_url, data={"go": "next", "signature": PNG})
    e.reset()
    count = e.rows("stock_counts")[0]
    assert count.data["area"] == "Aisle 3"
    assert sorted((e.title(e.get(int(r.data["product"]))), r.data["qty"]) for r in e.rows("count_lines")) == [("Flour 25kg", 2), ("Sugar 10kg", 4)]
    assert File.query.filter_by(record_id=count.id, field="counted_by").count() == 1
    assert "Do another" in client.get(done.headers["Location"]).get_data(as_text=True)


def test_a_flow_is_only_for_who_its_for(app, owner):
    _, ws = owner
    _built(ws, WAREHOUSE + [{"op": "set_team", "team": {"key": "warehouse", "name": "Warehouse"}},
                            {"op": "update_flow", "flow": {"label": "Stock count", "table": "stock_counts", "start": "new",
                                                           "who": ["warehouse"], "steps": [{"kind": "form", "fields": ["area"]}]}}])
    sam = _person(ws, "sam@example.com", "Sam", "editor")
    wes = _person(ws, "wes@example.com", "Wes", "editor", teams=["warehouse"])
    assert [f["key"] for f in flows.in_menu(_as(ws, wes))] == ["stock_count"]
    assert flows.in_menu(_as(ws, sam)) == []


def test_the_try_out_does_every_flow_once(app, owner):
    from app.builder import trial
    _, ws = owner
    _built(ws, WAREHOUSE)
    walked, _ = trial.try_out(ws, [], screens=False)
    assert not walked["crashes"], walked["crashes"]
    assert not [i for i in walked["issues"] if "flow" in i["text"].lower()], walked["issues"]
    _built(ws, [{"op": "add_field", "table": "stock_counts", "field": {"key": "approved_by", "type": "text", "required": True}}])
    walked, _ = trial.try_out(ws, [], screens=False)  # the count can't be saved without a field no step asks for
    assert any("“Stock count” can't be finished" in i["plain"] for i in walked["issues"]), walked["issues"]


def test_scanning_what_a_record_already_has_a_line_for_adds_to_that_line(app, owner):
    """A delivery scanned in against its purchase order counts into the order's own line for that product; a product the
    order hasn't got gets a line of its own."""
    user, ws = owner
    _built(ws, WAREHOUSE + [{"op": "add_flow", "flow": {"label": "Scan in", "table": "purchase_orders", "steps": [
        {"kind": "scan", "label": "Scan the delivery", "into": {"table": "po_lines", "via": "order", "item": "product", "quantity": "received"}}]}}])
    e = Engine(ws)
    flour, sugar = e.create("products", {"name": "Flour 25kg", "sku": "FL25"}), e.create("products", {"name": "Sugar 10kg", "sku": "SU10"})
    po = e.create("purchase_orders", {"supplier": "Mill Co"})
    line = e.create("po_lines", {"order": po.id, "product": flour.id, "quantity": 3, "received": 1})
    flow = next(f for f in schema.flows(ws.blueprint) if f["label"] == "Scan in")
    run = flows.begin(e, flow, user.id, po)
    assert not flows.answer(e, run, flow["steps"][0], {"scanned": [{"id": flour.id, "qty": 2}, {"id": sugar.id, "qty": 1}]}, [])
    flows.finish(e, run, "Sam")
    e.reset()
    lines = sorted(((r.data["product"], r.data.get("quantity"), r.data["received"]) for r in e.children("po_lines", "order", po.id)))
    assert lines == sorted([(flour.id, 3, 3), (sugar.id, None, 1)]) and e.get(line.id).data["received"] == 3
