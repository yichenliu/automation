"""Ready-made connectors: a shop's orders brought in with their lines, invoices sent to the books with theirs, card
payments marking invoices paid, shipping labels bought. Each app's API is stood in for; what's sent to it is checked."""
import hashlib
import hmac
import json
import time

from app.blueprint import schema
from app.connectors import apps
from app.engine import automations
from app.engine.records import Engine
from app.models import Connection

from .conftest import login
from .test_wave2 import _built

SHOP = [
    {"op": "create_table", "table": "customers", "label": "Customers", "fields": [
        {"key": "name", "type": "text", "required": True}, {"key": "email", "type": "email"}]},
    {"op": "create_table", "table": "orders", "label": "Orders", "fields": [
        {"key": "number", "type": "text", "required": True}, {"key": "shopify_id", "type": "text"},
        {"key": "customer_email", "type": "email"}, {"key": "total", "type": "money"}, {"key": "paid", "type": "text"},
        {"key": "tracking", "type": "text"}, {"key": "label", "type": "url"}, {"key": "xero_id", "type": "text"},
        {"key": "pay_link", "type": "url"},
        {"key": "status", "type": "select", "options": ["New", "Paid", "Sent"], "default": "New"}]},
    {"op": "create_table", "table": "order_lines", "label": "Order lines", "embedded": True, "fields": [
        {"key": "order", "type": "link", "table": "orders"}, {"key": "sku", "type": "text"}, {"key": "description", "type": "text"},
        {"key": "quantity", "type": "number"}, {"key": "price", "type": "money"}]},
]


class Answer:
    def __init__(self, body, status=200):
        self.body, self.status_code, self.text, self.headers = body, status, json.dumps(body), {"content-type": "application/json"}

    def json(self):
        return self.body


def _connect(ws, key, details):
    from app.security import encrypt
    from app.extensions import db
    db.session.add(Connection(workspace_id=ws.id, kind=f"app:{key}", status="connected", account="Test", secret=encrypt(json.dumps(details)), state={}))
    db.session.commit()


def test_a_shops_orders_come_in_with_their_lines_and_update_next_time(app, owner, monkeypatch):
    _, ws = owner
    _built(ws, SHOP + [{"op": "set_sync", "sync": {
        "app": "shopify", "pull": "orders", "into": "orders", "match": "shopify_id",
        "fields": {"number": "number", "customer_email": "customer.email", "total": "total", "paid": "payment_status"},
        "lines": {"into": "order_lines", "fields": {"sku": "sku", "description": "title", "quantity": "quantity", "price": "price"}}}}])
    sync = schema.syncs(ws.blueprint)[0]
    assert sync["lines"]["via"] == "order" and not schema.check(ws.blueprint)
    _connect(ws, "shopify", {"store": "acme.myshopify.com", "token": "shpat_x"})
    asked = []
    status = {"value": "PENDING"}

    def fake(method, url, **kw):
        asked.append((url, kw["headers"].get("X-Shopify-Access-Token"), kw["json"]["variables"]))
        return Answer({"data": {"orders": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [{
            "id": "gid://shopify/Order/5001", "name": "#1001", "createdAt": "2026-11-03T01:00:00Z", "updatedAt": "2026-11-03T02:00:00Z",
            "email": "cafe@example.com", "displayFinancialStatus": status["value"], "displayFulfillmentStatus": "UNFULFILLED",
            "currentTotalPriceSet": {"shopMoney": {"amount": "84.50", "currencyCode": "AUD"}},
            "customer": {"displayName": "Cafe Uno", "email": "cafe@example.com"}, "shippingAddress": None,
            "lineItems": {"nodes": [{"sku": "FL25", "title": "Flour 25kg", "quantity": 2, "originalUnitPriceSet": {"shopMoney": {"amount": "30.00"}}},
                                    {"sku": "SU10", "title": "Sugar 10kg", "quantity": 1, "originalUnitPriceSet": {"shopMoney": {"amount": "24.50"}}}]}}]}}})
    monkeypatch.setattr(apps.requests, "request", fake)
    assert apps.run_sync(ws, sync) == {"added": 1, "updated": 0, "refused": []}
    url, token, variables = asked[0]
    assert url == "https://acme.myshopify.com/admin/api/2025-01/graphql.json" and token == "shpat_x" and variables["q"] is None
    e = Engine(ws)
    order = e.rows("orders")[0]
    assert (order.data["number"], order.data["total"], order.data["paid"], order.data["shopify_id"]) == ("#1001", 84.5, "PENDING", "5001")
    assert sorted((r.data["sku"], r.data["quantity"]) for r in e.rows("order_lines")) == [("FL25", 2), ("SU10", 1)]
    status["value"] = "PAID"
    assert apps.run_sync(ws, sync) == {"added": 0, "updated": 1, "refused": []}  # the same order: updated, not added again
    assert asked[-1][2]["q"] == "updated_at:>'2026-11-03T02:00:00Z'"  # from where it got to
    e.reset()
    assert e.rows("orders")[0].data["paid"] == "PAID" and len(e.rows("order_lines")) == 2


def test_an_invoice_goes_to_the_books_with_its_lines_and_a_label_is_bought(app, owner, monkeypatch):
    _, ws = owner
    _built(ws, SHOP + [
        {"op": "add_automation", "automation": {"key": "to_xero", "name": "Send to Xero", "trigger": {"type": "button", "table": "orders", "label": "Send to Xero"},
                                                "actions": [{"type": "webhook", "app": "xero", "url": "/Invoices", "payload": {"Invoices": [{
                                                    "Type": "ACCREC", "Contact": {"Name": "{{record.customer_email}}"}, "Reference": "{{record.number}}",
                                                    "LineItems": {"each": "order_lines", "item": {"Description": "{{line.description}}",
                                                                                                  "Quantity": "{{line.quantity}}", "UnitAmount": "{{line.price}}"}}}]},
                                                "save": {"xero_id": "Invoices.0.InvoiceID"}}]}},
        {"op": "add_automation", "automation": {"key": "ship", "name": "Buy a label", "trigger": {"type": "button", "table": "orders", "label": "Ship it"},
                                                "actions": [{"type": "webhook", "app": "easypost", "url": "/shipments", "payload": {"shipment": {
                                                    "to_address": {"name": "{{record.customer_email}}"}, "service": "Express"}},
                                                "save": {"tracking": "tracking_code", "label": "postage_label.label_url"}}]}}])
    assert not schema.check(ws.blueprint)
    _connect(ws, "xero", {"access_token": "tok", "refresh_token": "r", "expires_at": "2999-01-01T00:00:00", "tenant_id": "T1"})
    _connect(ws, "easypost", {"api_key": "EZAK1"})
    e = Engine(ws)
    order = e.create("orders", {"number": "#1001", "customer_email": "cafe@example.com"})
    e.create("order_lines", {"order": order.id, "description": "Flour 25kg", "quantity": 2, "price": 30})
    e.create("order_lines", {"order": order.id, "description": "Sugar 10kg", "quantity": 1, "price": 24.5})
    sent = []

    def fake(method, url, **kw):
        sent.append((method, url, kw.get("headers"), kw.get("auth"), kw.get("json")))
        if "xero" in url:
            return Answer({"Invoices": [{"InvoiceID": "inv-77"}]})
        return Answer({"tracking_code": "TRK123", "postage_label": {"label_url": "https://labels.example.com/1.pdf"}})
    monkeypatch.setattr(apps.requests, "request", fake)
    for key in ("to_xero", "ship"):
        automations.press(e, next(a for a in e.bp["automations"] if a["key"] == key), e.get(order.id), "Ethan")
    method, url, headers, _, body = sent[0]
    assert (method, url, headers["Xero-tenant-id"], headers["Authorization"]) == ("POST", "https://api.xero.com/api.xro/2.0/Invoices", "T1", "Bearer tok")
    assert body["Invoices"][0]["LineItems"] == [{"Description": "Flour 25kg", "Quantity": 2, "UnitAmount": 30},
                                                {"Description": "Sugar 10kg", "Quantity": 1, "UnitAmount": 24.5}]
    assert sent[1][1] == "https://api.easypost.com/v2/shipments" and sent[1][3] == ("EZAK1", "")
    e.reset()
    saved = e.get(order.id).data
    assert (saved["xero_id"], saved["tracking"], saved["label"]) == ("inv-77", "TRK123", "https://labels.example.com/1.pdf")


def test_a_card_payment_marks_the_invoice_paid_when_stripe_says_so(app, client, owner, monkeypatch):
    _, ws = owner
    ws.settings = {**ws.settings, "money": {"base": "AUD", "rates": {}}}
    _built(ws, SHOP + [{"op": "add_automation", "automation": {
        "key": "pay", "name": "Payment link", "trigger": {"type": "record_created", "table": "orders"},
        "actions": [{"type": "payment_link", "amount": "{{record.total}}", "save_to": "pay_link", "paid": {"status": "Paid"}}]}}])
    assert not schema.check(ws.blueprint)
    _connect(ws, "stripe", {"secret_key": "sk_test_1", "webhook_secret": "whsec_abc"})
    made = {}

    def fake(method, url, **kw):
        made.update(kw.get("data") or {})
        return Answer({"url": "https://checkout.stripe.com/c/pay_1", "id": "cs_1"})
    monkeypatch.setattr(apps.requests, "request", fake)
    e = Engine(ws)
    order = e.create("orders", {"number": "#1002", "total": 84.5})
    e.reset()
    assert e.get(order.id).data["pay_link"] == "https://checkout.stripe.com/c/pay_1"
    assert made["line_items[0][price_data][unit_amount]"] == 8450 and made["line_items[0][price_data][currency]"] == "aud"
    event = json.dumps({"type": "checkout.session.completed", "data": {"object": {"payment_status": "paid",
                        "metadata": {"workspace": str(ws.id), "record": str(order.id), "paid": made["metadata[paid]"]}}}}).encode()
    stamp = str(int(time.time()))
    good = hmac.new(b"whsec_abc", f"{stamp}.".encode() + event, hashlib.sha256).hexdigest()
    forged = client.post(f"/apps/stripe/{ws.id}/events", data=event, content_type="application/json",
                         headers={"Stripe-Signature": f"t={stamp},v1={'0' * 64}"})
    assert forged.status_code == 400
    ok = client.post(f"/apps/stripe/{ws.id}/events", data=event, content_type="application/json", headers={"Stripe-Signature": f"t={stamp},v1={good}"})
    assert ok.status_code == 200
    e.reset()
    assert e.get(order.id).data["status"] == "Paid"


def test_connecting_checks_the_details_and_keeps_them_secret(app, client, owner, monkeypatch):
    _, ws = owner
    login(client)
    page = client.get(f"/w/{ws.id}/settings/connections").get_data(as_text=True)
    for name in ("Shopify", "WooCommerce", "Stripe", "EasyPost", "Xero", "QuickBooks Online"):
        assert name in page
    said = client.post(f"/w/{ws.id}/settings/apps/woocommerce", data={"url": "http://shop.example.com", "consumer_key": "ck", "consumer_secret": "cs"},
                       follow_redirects=True).get_data(as_text=True)
    assert "must start with https://" in said  # keys never go over an unsecured connection
    monkeypatch.setattr(apps.requests, "request", lambda method, url, **kw: Answer({"data": {"shop": {"name": "Acme Foods"}}}))
    client.post(f"/w/{ws.id}/settings/apps/shopify", data={"store": "acme.myshopify.com", "token": "shpat_secret"})
    conn = apps.connection(ws, "shopify")
    assert conn.status == "connected" and conn.account == "Acme Foods" and "shpat_secret" not in (conn.secret or "")
    assert apps.creds(conn)["token"] == "shpat_secret"
    problems = schema.check({**ws.blueprint, "tables": [], "syncs": [schema.normalize_sync({"app": "shopify", "pull": "invoices", "into": "x", "fields": {"a": "b"}})]})
    assert any("can bring in orders, customers, products" in p for p in problems)
