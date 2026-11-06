"""The API other systems use (a web shop sending orders, a bank feed, payroll, a BI tool reading totals), and the webhook
addresses that start automations.

    GET    /api/v1/tables                                  the tables and fields this key may see
    GET    /api/v1/tables/<table>/records?q=&sort=-total&limit=&offset=&<field>=<value>
    POST   /api/v1/tables/<table>/records                  {"field": value, ..., "lines": {"invoice_lines": [{...}]}}
    GET    /api/v1/tables/<table>/records/<id>
    PATCH  /api/v1/tables/<table>/records/<id>             {"field": value}; send If-Match: <version> to refuse
                                                           overwriting a change made since you read it
    DELETE /api/v1/tables/<table>/records/<id>
    POST   /hooks/<workspace>/<automation>/<secret>        starts a webhook automation with the JSON sent

Keys are made in Settings › API and act with the role they were given (viewer keys only read); everything goes through
the same engine as the screens, so rules, locks, permissions, history and automations all apply.
"""
import hashlib
import hmac
import secrets
from types import SimpleNamespace

from fastapi import APIRouter, Depends, Request
from starlette.responses import Response

from ..blueprint import schema
from ..blueprint.expr import to_number
from ..engine import automations
from ..engine.records import Conflict, Engine, FieldErrors, RecordProxy
from ..extensions import db
from ..models import ApiKey, Run, Workspace, utcnow
from ..security import Throttle, client_ip
from .http import Answer, Form, PageRoute, body_of, form_of, json_of, json_response

# other systems call these with a key (or, a webhook, a secret address), never from a page's form: no CSRF token
router = APIRouter(prefix="/api/v1", route_class=PageRoute)  # with the MCP and OpenAPI addresses mcp.py adds
hooks = APIRouter(route_class=PageRoute)
calls = Throttle(1200, 60)  # per key
hook_calls = Throttle(300, 60)  # per address
MAX_LIMIT = 500


# --- keys ----------------------------------------------------------------------------------------------------------------

def make_key(workspace, name, role, by):
    """A new key: (ApiKey, the key itself). The key is shown once; only its hash is kept."""
    prefix, secret = secrets.token_hex(4), secrets.token_urlsafe(30)
    plain = f"keel_{prefix}_{secret}"
    key = ApiKey(workspace_id=workspace.id, name=name[:120] or "API key", prefix=prefix, role=role if role in ("viewer", "editor", "admin") else "editor",
                 hash=hashlib.sha256(plain.encode()).hexdigest(), created_by=by)
    db.session.add(key)
    db.session.commit()
    return key, plain


def _error(status, message, **extra):
    return json_response({"error": message, **extra}, status)


def _key(request):
    header = request.headers.get("Authorization", "")
    plain = header[7:].strip() if header.lower().startswith("bearer ") else request.headers.get("X-Api-Key", "").strip()
    parts = plain.split("_")
    if len(parts) < 3 or parts[0] != "keel":
        raise Answer(_error(401, "Send your API key as: Authorization: Bearer keel_..."))
    key = ApiKey.query.filter_by(prefix=parts[1], revoked_at=None).first()
    if key is None or not hmac.compare_digest(key.hash, hashlib.sha256(plain.encode()).hexdigest()):
        raise Answer(_error(401, "That API key isn't valid (it may have been revoked)."))
    if not calls.allow(key.id):
        raise Answer(_error(429, "Too many requests; slow down and try again in a minute."))
    if key.last_used_at is None or (utcnow() - key.last_used_at).total_seconds() > 60:
        key.last_used_at = utcnow()
        db.session.commit()
    return key


def _engine(request):
    key = _key(request)
    ws = db.session.get(Workspace, key.workspace_id)
    return Engine(ws, by=f"API key “{key.name}”", member=SimpleNamespace(role=key.role, user_id=None)), key


def _args(request):
    """The query's values by name, each name's first value (as they were always read)."""
    return {k: request.query_params.getlist(k)[0] for k in request.query_params.keys()}


def _table(engine, key, action="view"):
    t = engine.tables.get(key)
    if t is None or not engine.can(t) or t.get("embedded") and action != "lines":
        raise Answer(_error(404, f"There's no table “{key}” (GET /api/v1/tables lists them)."))
    if action == "edit" and not engine.can(t, "edit"):
        raise Answer(_error(403, f"This key can't change {schema.lc(t['label'])}."))
    return engine.visible(t)


def _plain(value):
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, RecordProxy):
        return value.record.id
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _out(engine, t, record, lines=False):
    fields = [f for f in t["fields"]]
    out = {"id": record.id, "table": t["key"], "version": record.version,
           "created_at": record.created_at.isoformat() + "Z", "updated_at": (record.updated_at or record.created_at).isoformat() + "Z",
           "values": {f["key"]: _plain(engine.value(record, f["key"])) for f in fields},
           "display": {f["key"]: engine.display(f, engine.value(record, f["key"]), record) for f in fields}}
    if lines:
        for child in engine.bp.get("tables", []):
            link = next((f for f in child["fields"] if f["type"] == "link" and f["table"] == t["key"]), None)
            if child.get("embedded") and link and engine.can(child):
                child_t = engine.visible(child)
                out.setdefault("lines", {})[child["key"]] = [
                    {"id": r.id, "values": {f["key"]: _plain(engine.value(r, f["key"])) for f in child_t["fields"] if f["key"] != link["key"]}}
                    for r in engine.children(child["key"], link["key"], record.id)]
    return out


def _record(engine, t, rid):
    r = engine.get(rid, t["key"])
    if r is None or not engine.allowed(r):
        raise Answer(_error(404, f"There's no {schema.lc(t['singular'])} {rid}."))
    return r


def _body(data):
    """The JSON object sent (`data`, from json_of), or the API's 400 when it isn't one."""
    if not isinstance(data, dict):
        raise Answer(_error(400, "Send a JSON object of field values, e.g. {\"name\": \"Acme\", \"status\": \"Active\"}."))
    return data


def _values(t, data):
    unknown = [k for k in data if k != "lines" and not schema.field(t, k)]
    if unknown:
        raise Answer(_error(400, f"{t['label']} has no field {', '.join(unknown)}.", fields=[f["key"] for f in t["fields"]]))
    return {k: v for k, v in data.items() if k != "lines"}


def _failed(exc):
    status = 409 if isinstance(exc, Conflict) else 400
    return _error(status, "; ".join(exc.errors.values()), fields=exc.errors)


# --- routes --------------------------------------------------------------------------------------------------------------

@router.api_route("/tables", methods=["GET"], name="api.tables")
def tables(request: Request):
    engine, _ = _engine(request)
    out = []
    for t in engine.visible_blueprint().get("tables", []):
        fields = [{k: f[k] for k in ("key", "label", "type", "options", "table", "required") if k in f} for f in t["fields"]]
        out.append({"key": t["key"], "label": t["label"], "singular": t["singular"], "lines_of": None if not t.get("embedded") else
                    next((f["table"] for f in t["fields"] if f["type"] == "link"), None), "fields": fields,
                    "editable": engine.can(t, "edit")})
    return json_response({"tables": out})


@router.api_route("/tables/{table}/records", methods=["GET"], name="api.list_records")
def list_records(request: Request, table: str):
    engine, _ = _engine(request)
    t = _table(engine, table)
    args = _args(request)
    filters = []
    for name, value in args.items():
        if name in ("q", "sort", "limit", "offset"):
            continue
        f = schema.field(t, name)
        if f is None and name not in ("created_at", "updated_at"):
            return _error(400, f"{t['label']} has no field “{name}” to filter by.")
        filters.append({"field": name, "op": "equals", "value": value})
    sort = None
    if args.get("sort"):
        key = args["sort"].lstrip("-")
        if not schema.field(t, key) and key not in ("created_at", "updated_at"):
            return _error(400, f"{t['label']} has no field “{key}” to sort by.")
        sort = {"field": key, "dir": "desc" if args["sort"].startswith("-") else "asc"}
    limit = int(min(max(to_number(args.get("limit")) or 50, 1), MAX_LIMIT))
    offset = int(max(to_number(args.get("offset")) or 0, 0))
    rows, total = engine.page(t["key"], filters, q=args.get("q"), sort=sort, offset=offset, limit=limit)  # only this page read
    return json_response({"total": total, "limit": limit, "offset": offset, "records": [_out(engine, t, r) for r in rows]})


@router.api_route("/tables/{table}/records/{rid:int}", methods=["GET"], name="api.get_record")
def get_record(request: Request, table: str, rid: int):
    engine, _ = _engine(request)
    t = _table(engine, table)
    return json_response(_out(engine, t, _record(engine, t, rid), lines=True))


@router.api_route("/tables/{table}/records", methods=["POST"], name="api.create_record")
def create_record(request: Request, table: str, sent=Depends(json_of)):
    engine, _ = _engine(request)
    t = _table(engine, table, "edit")
    data = _body(sent)
    values = _values(t, data)
    try:
        with engine.atomic():
            record = engine.create(t["key"], values)
            lines = data.get("lines") or {}
            if not isinstance(lines, dict):
                raise FieldErrors({"lines": "send lines as {\"line_table\": [{...}, ...]}"})
            for child_key, rows in lines.items():
                child = _table(engine, child_key, "lines")
                link = next((f for f in engine.tables[child_key]["fields"] if f["type"] == "link" and f["table"] == t["key"]), None)
                if link is None or not engine.tables[child_key].get("embedded"):
                    raise FieldErrors({"lines": f"“{child_key}” aren't lines of {t['label']}"})
                for row in rows if isinstance(rows, list) else []:
                    engine.create(child_key, {**_values(child, row), link["key"]: record.id})
    except FieldErrors as exc:
        return _failed(exc)
    engine.reset()
    return json_response(_out(engine, t, engine.get(record.id), lines=True), 201)


@router.api_route("/tables/{table}/records/{rid:int}", methods=["PATCH", "PUT"], name="api.update_record")
def update_record(request: Request, table: str, rid: int, sent=Depends(json_of)):
    engine, _ = _engine(request)
    t = _table(engine, table, "edit")
    record = _record(engine, t, rid)
    expected = request.headers.get("If-Match", "").strip('" ') or None
    try:
        engine.update(record, _values(t, _body(sent)), expected_version=expected)
    except FieldErrors as exc:
        return _failed(exc)
    engine.reset()
    return json_response(_out(engine, t, engine.get(rid), lines=True))


@router.api_route("/tables/{table}/records/{rid:int}", methods=["DELETE"], name="api.delete_record")
def delete_record(request: Request, table: str, rid: int):
    engine, _ = _engine(request)
    t = _table(engine, table, "edit")
    try:
        engine.delete(_record(engine, t, rid))
    except FieldErrors as exc:
        return _failed(exc)
    return Response(status_code=204)


@hooks.api_route("/hooks/{wid:int}/{key}/{token}", methods=["POST"], name="hooks.hook")
def hook(request: Request, wid: int, key: str, token: str, sent=Depends(json_of), form: Form = Depends(form_of),
         body: bytes = Depends(body_of)):
    """Another system calling an automation's webhook address with JSON (or a form): the automation runs with what was
    sent as {{payload}}."""
    if not hook_calls.allow(f"{wid}:{key}:{client_ip(request)}"):
        return _error(429, "Too many calls; try again in a minute.")
    ws = db.session.get(Workspace, wid)
    automation = next((a for a in (ws.blueprint or {}).get("automations", []) if a["key"] == key), None) if ws else None
    if automation is None or automation["trigger"]["type"] != "webhook_received" or \
            not hmac.compare_digest(token, automations.hook_token(wid, key)):
        return _error(404, "There's no webhook at this address.")
    if not automation.get("enabled", True):
        return _error(409, "This automation is turned off.")
    payload = sent
    if payload is None:
        payload = form.to_dict() or {"body": body.decode("utf-8", "replace")[:20000]}
    engine = Engine(ws, by="webhook")
    run = automations.start_from(engine, automation, "payload", payload if isinstance(payload, dict) else {"items": payload},
                                 f"Webhook call from {client_ip(request)}")
    run = db.session.get(Run, run.id)
    return json_response({"run": run.id, "status": run.status, "steps": [s.get("result") for s in run.steps or []]}, 202)
