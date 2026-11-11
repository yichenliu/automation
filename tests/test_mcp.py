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


def test_the_owners_own_agent_changes_the_app_in_the_keel_language_with_the_builders_tools(app, client, owner):
    from app import lang
    from app.blueprint import schema as design
    user, ws = owner
    ws.blueprint = lang.apply(design.empty("Shop"), [
        {"do": "add_list", "key": "invoices", "label": "Invoices", "singular": "Invoice", "fields": [{"type": "text", "key": "name", "label": "Name"}],
         "stages": {"stages": ["Draft", "Sent", "Paid"]}}]).blueprint
    from app.extensions import db
    db.session.commit()
    _, staff = make_key(ws, "Staff's agent", "editor", user.id)
    assert not [t for t in _rpc(client, staff, "tools/list").get_json()["result"]["tools"] if t["name"].startswith("keel_")]
    said, wrong = _tool(client, staff, "keel_describe", {})
    assert wrong and "owner's or an admin's key" in said
    _, key = make_key(ws, "Owner's agent", "admin", user.id)
    names = {t["name"] for t in _rpc(client, key, "tools/list").get_json()["result"]["tools"]}
    assert {"keel_language", "keel_describe", "keel_what_if", "keel_apply", "keel_test", "keel_undo"} <= names
    language, _ = _tool(client, key, "keel_language", {})
    assert "stage_alert" in language["catalog"]
    now, _ = _tool(client, key, "keel_describe", {})
    patch = {"do": "stage_alert", "list": "invoices", "stage": "Paid"}
    tried, _ = _tool(client, key, "keel_what_if", {"patches": [patch, {"do": "stage_alert", "list": "invoices", "stage": "Payed"}]})
    assert tried["refused"] == [2] and "isn't a stage" in tried["problems"][0]
    assert tried["checks"] and all(c["ok"] for c in tried["checks"])
    stale, _ = _tool(client, key, "keel_apply", {"patches": [patch], "version": now["version"] + 5})
    assert stale["saved"] is False and "describe it again" in stale["why"]
    done, _ = _tool(client, key, "keel_apply", {"patches": [patch], "version": now["version"]})
    assert done["saved"] and done["version"] == now["version"] + 1 and all(c["ok"] for c in done["checks"])


def test_the_builders_apply_tool_adds_the_records_its_patches_bring(app, client, owner):
    """The usual units, the owner's staff: add_records through keel_apply saved the list but not its records (only the
    builder's chat added them)."""
    from app import lang
    from app.blueprint import schema as design
    user, ws = owner
    ws.blueprint = lang.apply(design.empty("Shop"), [
        {"do": "add_list", "key": "units", "label": "Units", "singular": "Unit", "fields": [{"type": "text", "key": "name", "label": "Name"}]}]).blueprint
    from app.extensions import db
    db.session.commit()
    _, key = make_key(ws, "Owner's agent", "admin", user.id)
    now, _ = _tool(client, key, "keel_describe", {})
    done, _ = _tool(client, key, "keel_apply", {"patches": [{"do": "add_records", "list": "units", "records": [{"name": "Each"}, {"name": "Pair"}]}],
                                               "version": now["version"]})
    assert done["saved"], done
    assert sorted(r.data["name"] for r in Engine(ws).rows("units")) == ["Each", "Pair"]


def test_a_builder_keeps_its_own_worked_examples_as_the_apps_checks(app):
    """add_check acts an example out and keeps it, run with every change from then on; one naming what isn't there isn't
    kept; remove_check lets it go."""
    from app.lang import tools
    from app.models import User, Workspace, Membership
    from app.extensions import db
    owner = User(email="o@example.com", name="Olive")
    owner.set_password("x" * 12)
    db.session.add(owner)
    db.session.flush()
    ws = Workspace(owner_id=owner.id, name="Shop", blueprint={}, state={}, settings={}, version=0)
    db.session.add(ws)
    db.session.flush()
    db.session.add(Membership(workspace_id=ws.id, user_id=owner.id, role="owner"))
    db.session.commit()
    out = tools.apply(ws, [{"do": "add_list", "key": "orders", "label": "Orders", "singular": "Order", "fields": [
        {"type": "number", "key": "quantity", "label": "Quantity"}, {"type": "money", "key": "price", "label": "Price"},
        {"type": "formula", "key": "total", "label": "Total", "formula": "quantity * price", "shows": "money"}]}], 0)
    assert out["saved"]
    kept = tools.call(ws, "keel_add_check", {"title": "Two at $5 come to $10", "steps": [{"do": "add", "table": "orders", "as": "o1",
                                                                                          "values": {"quantity": 2, "price": 5}}],
                                             "expect": [{"record": "o1", "field": "total", "equals": 10}]}, by=owner.id)
    assert kept["kept"] and kept["ok"], kept
    assert any(c["title"] == "Two at $5 come to $10" and c["ok"] for c in tools.test(ws)["checks"])
    wrong = tools.add_check(ws, "Names a list that isn't there", [{"do": "add", "table": "invoices", "as": "i1", "values": {}}], [])
    assert not wrong["kept"]
    assert tools.remove_check(ws, "Two at $5 come to $10")["removed"]
    assert not any(c["title"] == "Two at $5 come to $10" for c in tools.test(ws)["checks"])
