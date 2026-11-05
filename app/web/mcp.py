"""The app for AI assistants (Claude, ChatGPT, Cursor, Copilot…) over the Model Context Protocol, and the API described
in OpenAPI for code and BI tools.

    POST /api/v1/mcp            MCP (streamable HTTP, JSON answers): tools to list the lists, find, read, add and change
                                records; with the API key's role, permissions, rules and locks, as on the screens
    GET  /api/v1/openapi.json   this app's API, its own tables and fields as types (OpenAPI 3.1)

An assistant is added with the app's address and an API key from Settings › API and AI assistants, e.g.

    claude mcp add --transport http keel https://<this app>/api/v1/mcp --header "Authorization: Bearer keel_…"
"""
import json

from flask import current_app, jsonify, request

from ..blueprint import schema
from ..engine.records import FieldErrors
from . import api
from .api import bp

PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
TOOLS = [
    {"name": "list_tables", "description": "The business's lists (tables) this key may see, with their fields, their types and choices. "
                                           "Start here: the other tools name a table by its key.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "find_records", "description": "Records of a table: searched by words, filtered by fields' values, sorted. Answers with "
                                            "each record's values as people see them.",
     "inputSchema": {"type": "object", "required": ["table"], "properties": {
         "table": {"type": "string", "description": "the table's key"}, "search": {"type": "string"},
         "filters": {"type": "object", "description": "field key: value it equals", "additionalProperties": True},
         "sort": {"type": "string", "description": "a field key; -key for newest or largest first"},
         "limit": {"type": "integer", "minimum": 1, "maximum": 200}}}},
    {"name": "get_record", "description": "One record with all its values and its lines (an invoice's lines).",
     "inputSchema": {"type": "object", "required": ["table", "id"], "properties": {"table": {"type": "string"}, "id": {"type": "integer"}}}},
    {"name": "create_record", "description": "Add a record (and its lines), checked by the app's rules as if a person added it.",
     "inputSchema": {"type": "object", "required": ["table", "values"], "properties": {
         "table": {"type": "string"}, "values": {"type": "object", "additionalProperties": True},
         "lines": {"type": "object", "description": "line table key: [values of each line]", "additionalProperties": True}}}},
    {"name": "update_record", "description": "Change some of a record's values, checked by the app's rules and locks.",
     "inputSchema": {"type": "object", "required": ["table", "id", "values"], "properties": {
         "table": {"type": "string"}, "id": {"type": "integer"}, "values": {"type": "object", "additionalProperties": True}}}},
]


@bp.route("/mcp", methods=["POST", "GET", "DELETE"])
def mcp():
    if request.method != "POST":  # no server-sent stream: every answer comes back on its request
        return "", 405, {"Allow": "POST"}
    engine, key = api._engine()
    message = request.get_json(silent=True)
    if isinstance(message, list):
        answers = [a for a in (_answer(engine, m) for m in message) if a is not None]
        return (jsonify(answers), 200) if answers else ("", 202)
    answer = _answer(engine, message)
    return (jsonify(answer), 200) if answer is not None else ("", 202)


def _answer(engine, message):
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _fail(None, -32600, "Send JSON-RPC 2.0 messages")
    method, mid, params = message.get("method"), message.get("id"), message.get("params") or {}
    if mid is None:  # a notification (initialized, cancelled): nothing to answer
        return None
    if method == "initialize":
        asked = params.get("protocolVersion")
        return _ok(mid, {"protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0], "capabilities": {"tools": {"listChanged": False}},
                         "serverInfo": {"name": current_app.config.get("PLATFORM_NAME") or "Keel", "version": "1"},
                         "instructions": f"The business app “{engine.bp.get('name') or engine.ws.name}”. Call list_tables first. "
                                         "Values you add or change are checked by the app's rules, as a person's would be."})
    if method == "ping":
        return _ok(mid, {})
    if method == "tools/list":
        tools = TOOLS if engine.member.role != "viewer" else [t for t in TOOLS if t["name"] not in ("create_record", "update_record")]
        if _designs(engine):  # the owner's and admins' keys may also change the app itself, in the Keel language
            from ..lang import tools as keel
            tools = tools + keel.schemas()
        return _ok(mid, {"tools": tools})
    if method == "tools/call":
        try:
            if str(params.get("name") or "").startswith("keel_"):
                result = _design(engine, params.get("name"), params.get("arguments") or {})
            else:
                result = _call(engine, params.get("name"), params.get("arguments") or {})
        except _Wrong as exc:
            return _ok(mid, {"content": [{"type": "text", "text": str(exc)}], "isError": True})
        return _ok(mid, {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, default=str)}], "isError": False})
    return _fail(mid, -32601, f"“{method}” isn't something this server does")


class _Wrong(Exception):
    """A tool call that can't be done: said to the assistant as the tool's answer, so it can put it right."""


def _designs(engine):
    return engine.member.role in ("owner", "admin")


def _design(engine, name, args):
    """A Keel language tool (app/lang/tools.py): the same ones the builder uses, for the owner's own agent."""
    from pydantic import ValidationError

    from ..lang import tools as keel
    if not _designs(engine):
        raise _Wrong("Only the owner's or an admin's key may change the app itself")
    try:
        return keel.call(engine.ws, name, args, by=getattr(engine, "user_id", None))
    except (KeyError, ValidationError) as exc:
        raise _Wrong(str(exc)) from None


def _table(engine, key, action="view"):
    t = engine.tables.get(str(key or ""))
    if t is None or not engine.can(t) or t.get("embedded"):
        raise _Wrong(f"There's no table “{key}”: list_tables says which there are")
    if action == "edit" and not engine.can(t, "edit"):
        raise _Wrong(f"This key can't change {schema.lc(t['label'])}")
    return engine.visible(t)


def _call(engine, name, args):
    if name == "list_tables":
        return [{"key": t["key"], "label": t["label"], "one": t["singular"], "editable": engine.can(t, "edit"),
                 "fields": [{k: f[k] for k in ("key", "label", "type", "options", "table", "required") if k in f} for f in t["fields"]],
                 **({"lines_of": next((f["table"] for f in t["fields"] if f["type"] == "link"), None)} if t.get("embedded") else {})}
                for t in engine.visible_blueprint().get("tables", [])]
    if name == "find_records":
        t = _table(engine, args.get("table"))
        filters = []
        for k, v in (args.get("filters") or {}).items():
            if not schema.field(t, k):
                raise _Wrong(f"{t['label']} has no field “{k}”")
            filters.append({"field": k, "op": "equals", "value": v})
        sort = None
        if args.get("sort"):
            k = str(args["sort"]).lstrip("-")
            if not schema.field(t, k) and k not in ("created_at", "updated_at"):
                raise _Wrong(f"{t['label']} has no field “{k}” to sort by")
            sort = {"field": k, "dir": "desc" if str(args["sort"]).startswith("-") else "asc"}
        rows = engine.query(t["key"], filters, q=args.get("search"), sort=sort)
        limit = max(1, min(int(args.get("limit") or 25), 200))
        return {"total": len(rows), "shown": min(limit, len(rows)),
                "records": [{"id": r.id, **{f["label"]: engine.display(f, engine.value(r, f["key"]), r) for f in t["fields"]}} for r in rows[:limit]]}
    if name == "get_record":
        t = _table(engine, args.get("table"))
        r = engine.get(int(args.get("id") or 0), t["key"])
        if r is None or not engine.allowed(r):
            raise _Wrong(f"There's no {schema.lc(t['singular'])} {args.get('id')}")
        return api._out(engine, t, r, lines=True)
    if name in ("create_record", "update_record"):
        if engine.member.role == "viewer":
            raise _Wrong("This key only reads")
        t = _table(engine, args.get("table"), "edit")
        values = args.get("values") if isinstance(args.get("values"), dict) else {}
        unknown = [k for k in values if not schema.field(t, k)]
        if unknown:
            raise _Wrong(f"{t['label']} has no field {', '.join(unknown)} (its fields: {', '.join(f['key'] for f in t['fields'])})")
        try:
            with engine.atomic():
                if name == "create_record":
                    record = engine.create(t["key"], values)
                    for child_key, rows in (args.get("lines") or {}).items():
                        child = engine.tables.get(child_key)
                        link = next((f for f in (child or {"fields": []})["fields"] if f["type"] == "link" and f["table"] == t["key"]), None)
                        if child is None or link is None or not child.get("embedded"):
                            raise FieldErrors({"lines": f"“{child_key}” aren't lines of {t['label']}"})
                        for row in rows if isinstance(rows, list) else []:
                            engine.create(child_key, {**{k: v for k, v in row.items() if schema.field(child, k)}, link["key"]: record.id})
                else:
                    record = engine.get(int(args.get("id") or 0), t["key"])
                    if record is None or not engine.allowed(record):
                        raise FieldErrors({"_": f"There's no {schema.lc(t['singular'])} {args.get('id')}"})
                    engine.update(record, values)
        except FieldErrors as exc:
            raise _Wrong("The app refused it: " + "; ".join(exc.errors.values())) from exc
        engine.reset()
        return api._out(engine, t, engine.get(record.id), lines=True)
    raise _Wrong(f"There's no tool “{name}”")


def _ok(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _fail(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


# --- OpenAPI ---------------------------------------------------------------------------------------------------------------

JSON_TYPES = {"number": "number", "money": "number", "percent": "number", "duration": "number", "rating": "integer", "checkbox": "boolean",
              "link": "integer", "user": "integer", "autonumber": "string", "multi_select": "array", "tags": "array", "json": "object",
              "location": "object", "file": "array", "image": "array", "signature": "array"}


@bp.route("/openapi.json")
def openapi():
    """This app's API in OpenAPI 3.1: each table this key sees, with its fields as types, for code generators and BI tools."""
    engine, key = api._engine()
    schemas, paths = {}, {"/tables": {"get": {"summary": "The tables and fields this key may see", "responses": {"200": {"description": "OK"}}}}}
    for t in engine.visible_blueprint().get("tables", []):
        props = {}
        for f in t["fields"]:
            kind = JSON_TYPES.get(f["type"], "string")
            prop = {"type": kind, "title": f["label"]}
            if f["type"] in ("select",) and f.get("options"):
                prop["enum"] = list(f["options"])
            if kind == "array":
                prop["items"] = {"type": "string"}
            if f["type"] in ("date",):
                prop["format"] = "date"
            if f["type"] in ("datetime", "created_time", "modified_time"):
                prop["format"] = "date-time"
            if f["type"] == "email":
                prop["format"] = "email"
            if f["type"] in schema.CALCULATED or f["type"] in ("autonumber", "created_time", "modified_time", "created_by", "modified_by"):
                prop["readOnly"] = True
            props[f["key"]] = prop
        name = "".join(p.title() for p in t["key"].split("_"))
        schemas[name] = {"type": "object", "title": t["singular"], "properties": props,
                         "required": [f["key"] for f in t["fields"] if f.get("required")]}
        if t.get("embedded"):
            continue
        ref = {"$ref": f"#/components/schemas/{name}"}
        record = {"type": "object", "properties": {"id": {"type": "integer"}, "version": {"type": "integer"}, "values": ref}}
        paths[f"/tables/{t['key']}/records"] = {
            "get": {"summary": f"List {schema.lc(t['label'])}", "parameters": [
                {"name": "q", "in": "query", "schema": {"type": "string"}}, {"name": "sort", "in": "query", "schema": {"type": "string"}},
                {"name": "limit", "in": "query", "schema": {"type": "integer", "maximum": api.MAX_LIMIT}},
                {"name": "offset", "in": "query", "schema": {"type": "integer"}}],
                    "responses": {"200": {"description": "OK", "content": {"application/json": {"schema": {
                        "type": "object", "properties": {"total": {"type": "integer"}, "records": {"type": "array", "items": record}}}}}}}},
            **({"post": {"summary": f"Add a {schema.lc(t['singular'])}", "requestBody": {"content": {"application/json": {"schema": ref}}},
                         "responses": {"201": {"description": "Added"}, "400": {"description": "Refused by the app's rules"}}}}
               if engine.can(t, "edit") else {})}
        paths[f"/tables/{t['key']}/records/{{id}}"] = {
            "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "get": {"summary": f"One {schema.lc(t['singular'])}, with its lines", "responses": {"200": {"description": "OK"}}},
            **({"patch": {"summary": f"Change a {schema.lc(t['singular'])}", "requestBody": {"content": {"application/json": {"schema": ref}}},
                          "parameters": [{"name": "If-Match", "in": "header", "schema": {"type": "string"},
                                          "description": "the version you read: refused if it changed since"}],
                          "responses": {"200": {"description": "Changed"}, "409": {"description": "Changed by someone else since"}}},
                "delete": {"summary": f"Delete a {schema.lc(t['singular'])} (to the recycle bin)", "responses": {"204": {"description": "Deleted"}}}}
               if engine.can(t, "edit") else {})}
    return jsonify({"openapi": "3.1.0", "info": {"title": f"{engine.bp.get('name') or engine.ws.name} API", "version": str(engine.ws.version or 1)},
                    "servers": [{"url": f"{current_app.config['BASE_URL']}/api/v1"}],
                    "components": {"securitySchemes": {"key": {"type": "http", "scheme": "bearer"}}, "schemas": schemas},
                    "security": [{"key": []}], "paths": paths})
