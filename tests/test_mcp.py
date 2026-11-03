"""AI assistants (Claude, ChatGPT, Cursor…) working on the app's records over MCP, and the API described in OpenAPI."""
import json

from app.engine.records import Engine
from app.web.api import make_key

from .test_wave2 import _built

DESIGN = [
    {"op": "create_table", "table": "customers", "label": "Customers", "fields": [
        {"key": "name", "type": "text", "required": True}, {"key": "email", "type": "email"},
        {"key": "tier", "type": "select", "options": ["Gold", "Silver"]}]},
    {"op": "create_table", "table": "invoices", "label": "Invoices", "fields": [
        {"key": "customer", "type": "link", "table": "customers", "required": True}, {"key": "total", "type": "money"}],
     "rules": [{"check": "total >= 0", "message": "A total can't be below zero"}]},
]


def _rpc(client, key, method, params=None, mid=1):
    body = {"jsonrpc": "2.0", "method": method, **({"id": mid} if mid is not None else {}), **({"params": params} if params else {})}
    return client.post("/api/v1/mcp", data=json.dumps(body), content_type="application/json", headers={"Authorization": f"Bearer {key}"})


def _tool(client, key, name, args):
    answer = _rpc(client, key, "tools/call", {"name": name, "arguments": args}).get_json()["result"]
    text = answer["content"][0]["text"]
    return (json.loads(text) if not answer["isError"] else text), answer["isError"]


def test_an_assistant_finds_reads_and_adds_records_with_the_apps_rules(app, client, owner):
    user, ws = owner
    _built(ws, DESIGN)
    e = Engine(ws)
    acme = e.create("customers", {"name": "Acme", "tier": "Gold"})
    e.create("customers", {"name": "Bolt", "tier": "Silver"})
    _, key = make_key(ws, "Claude", "editor", user.id)
    hello = _rpc(client, key, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test"}}).get_json()
    assert hello["result"]["protocolVersion"] == "2025-06-18" and "tools" in hello["result"]["capabilities"]
    assert _rpc(client, key, "notifications/initialized", mid=None).status_code == 202
    names = [t["name"] for t in _rpc(client, key, "tools/list").get_json()["result"]["tools"]]
    assert names == ["list_tables", "find_records", "get_record", "create_record", "update_record"]
    tables, _ = _tool(client, key, "list_tables", {})
    assert [t["key"] for t in tables] == ["customers", "invoices"] and tables[0]["fields"][2]["options"] == ["Gold", "Silver"]
    found, _ = _tool(client, key, "find_records", {"table": "customers", "filters": {"tier": "Gold"}})
    assert found["total"] == 1 and found["records"][0]["Name"] == "Acme"
    made, error = _tool(client, key, "create_record", {"table": "invoices", "values": {"customer": acme.id, "total": 120}})
    assert not error and made["values"]["total"] == 120
    said, error = _tool(client, key, "create_record", {"table": "invoices", "values": {"customer": acme.id, "total": -5}})
    assert error and "A total can't be below zero" in said  # the app's own rules, as for a person
    said, error = _tool(client, key, "find_records", {"table": "payroll"})
    assert error and "list_tables" in said
    assert _rpc(client, "keel_x_wrong", "tools/list").status_code == 401


def test_a_read_only_key_gets_only_the_reading_tools_and_the_api_is_described(app, client, owner):
    user, ws = owner
    _built(ws, DESIGN)
    _, key = make_key(ws, "BI", "viewer", user.id)
    names = [t["name"] for t in _rpc(client, key, "tools/list").get_json()["result"]["tools"]]
    assert "create_record" not in names
    said, error = _tool(client, key, "create_record", {"table": "customers", "values": {"name": "X"}})
    assert error and "only reads" in said
    spec = client.get("/api/v1/openapi.json", headers={"Authorization": f"Bearer {key}"}).get_json()
    assert spec["openapi"] == "3.1.0"
    customer = spec["components"]["schemas"]["Customers"]
    assert customer["properties"]["tier"]["enum"] == ["Gold", "Silver"] and customer["required"] == ["name"]
    assert "post" not in spec["paths"]["/tables/customers/records"]  # a viewer key can't add
