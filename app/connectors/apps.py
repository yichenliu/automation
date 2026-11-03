"""Ready-made connectors to the systems a business already runs on: its web shop (Shopify, WooCommerce), its books (Xero,
QuickBooks), card payments (Stripe), shipping labels (EasyPost: Australia Post, Royal Mail, UPS, FedEx, DHL and more).

Each app here says what it needs to connect (an address and a key, or signing in), what it can bring in ("pulls":
orders, customers, products…, each item a flat set of details with its lines), and where its API is, so a rule's
webhook step can send to it ("app": "xero") with the sign-in added.

- Connecting is in Settings › Connections. What's given is checked with the app at once and kept encrypted.
- A sync (the design's "syncs", set by the builder) brings an app's items into a list on a schedule: matched by the
  app's own id kept in a field, new ones added with their lines, changed ones updated, by the same engine as people.
"""
import json
from datetime import datetime, timedelta

import requests

from ..extensions import db
from ..models import Connection, utcnow
from ..security import decrypt, encrypt

REGISTRY = {}
TIMEOUT = 30


class AppProblem(Exception):
    """The app couldn't be reached or refused; the message says why, plainly."""


class App:
    key = name = about = icon = ""
    fields = []  # what connecting needs: [{"key", "label", "placeholder", "secret": bool, "help"}]
    pulls = {}  # what it can bring in: {"orders": "Orders, with their lines", ...}
    item_details = {}  # each pull's details, for the builder to map: {"orders": "id, number, ..., lines: [sku, ...]"}
    oauth = False

    def base(self, creds):
        raise NotImplementedError

    def headers(self, creds):
        return {}

    def auth(self, creds):
        return None

    def check(self, creds):
        """The account's name if the details work; AppProblem otherwise."""
        raise NotImplementedError

    def pull(self, creds, what, since):
        """(items, the cursor to start from next time). `since` is the cursor kept from last time (None: from the start)."""
        raise AppProblem(f"{self.name} can't bring in {what}")

    def request(self, creds, method, path, **kw):
        url = path if path.startswith("https://") else f"{self.base(creds).rstrip('/')}/{path.lstrip('/')}"
        try:
            answer = requests.request(method, url, headers={**self.headers(creds), **kw.pop("headers", {})}, auth=self.auth(creds),
                                      timeout=TIMEOUT, **kw)
        except requests.RequestException as exc:
            raise AppProblem(f"{self.name} couldn't be reached ({type(exc).__name__})") from exc
        if answer.status_code in (401, 403):
            raise AppProblem(f"{self.name} refused the connection's details: connect it again under Settings › Connections")
        if answer.status_code >= 400:
            raise AppProblem(f"{self.name} said {answer.status_code}: {answer.text[:300]}")
        try:
            return answer.json()
        except ValueError:
            return {"text": answer.text}


def register(cls):
    REGISTRY[cls.key] = cls()
    return cls


def get(key):
    if key not in REGISTRY:
        from . import catalog  # noqa: F401 (registers the apps)
    return REGISTRY.get(key)


def all_apps():
    from . import catalog  # noqa: F401
    return list(REGISTRY.values())


# --- connecting ------------------------------------------------------------------------------------------------------------

def connection(ws, key, create=False):
    conn = Connection.query.filter_by(workspace_id=ws.id, kind=f"app:{key}").first()
    if conn is None and create:
        conn = Connection(workspace_id=ws.id, kind=f"app:{key}", status="not connected", state={})
        db.session.add(conn)
    return conn


def creds(conn):
    return json.loads(decrypt(conn.secret)) if conn is not None and conn.secret else {}


def connect(ws, key, values, user_id):
    """Check what's given with the app, then keep it (encrypted). Returns the account's name."""
    app = get(key)
    given = {f["key"]: str(values.get(f["key"]) or "").strip() for f in app.fields}
    missing = [f["label"] for f in app.fields if not given[f["key"]]]
    if missing:
        raise AppProblem(f"Fill in {', '.join(missing)}")
    account = app.check(given)
    conn = connection(ws, key, create=True)
    conn.secret, conn.account, conn.status, conn.problem = encrypt(json.dumps(given)), str(account)[:255], "connected", None
    conn.connected_by, conn.checked_at = user_id, utcnow()
    db.session.commit()
    return account


def store_tokens(ws, key, given, account, user_id):
    """An app signed in to (OAuth): its tokens kept like any other details."""
    conn = connection(ws, key, create=True)
    conn.secret, conn.account, conn.status, conn.problem = encrypt(json.dumps(given)), str(account)[:255], "connected", None
    conn.connected_by, conn.checked_at = user_id, utcnow()
    db.session.commit()


def disconnect(ws, key):
    conn = connection(ws, key)
    if conn is not None:
        db.session.delete(conn)
        db.session.commit()


def connected(ws, key):
    conn = connection(ws, key)
    return conn is not None and conn.status == "connected"


def call(ws, key, method, path, payload=None, params=None):
    """A rule's webhook step sent to a connected app: its address and sign-in added. Returns what it answered."""
    app, conn = get(key), connection(ws, key)
    if app is None:
        raise AppProblem(f"There's no app “{key}”")
    if conn is None or conn.status != "connected":
        raise AppProblem(f"{app.name} isn't connected: connect it under Settings › Connections")
    details = _fresh(app, conn)
    return app.request(details, method, path, json=payload if method != "GET" else None, params=params)


def _fresh(app, conn):
    """The connection's details, its sign-in renewed first where it's about to run out (OAuth apps)."""
    details = creds(conn)
    if app.oauth and details.get("expires_at") and datetime.fromisoformat(details["expires_at"]) < datetime.utcnow() + timedelta(minutes=2):
        details = app.refresh(details)
        conn.secret = encrypt(json.dumps(details))
        db.session.commit()
    return details


# --- syncs ---------------------------------------------------------------------------------------------------------------

def due(ws):
    """The syncs of a workspace whose time has come (each runs every so many minutes)."""
    out = []
    for sync in (ws.blueprint or {}).get("syncs") or []:
        conn = connection(ws, sync["app"])
        if conn is None or conn.status != "connected":
            continue
        last = ((conn.state or {}).get("syncs") or {}).get(sync["key"], {}).get("at")
        if not last or datetime.fromisoformat(last) < datetime.utcnow() - timedelta(minutes=sync.get("every", 15)):
            out.append(sync)
    return out


def run_sync(ws, sync, engine=None):
    """Bring an app's items into the list: added, or updated where one with the app's id is there already. Returns
    {"added", "updated", "refused": [why…]} and keeps where it got to."""
    from ..engine.records import Engine, FieldErrors
    from ..models import Record
    app, conn = get(sync["app"]), connection(ws, sync["app"])
    engine = engine or Engine(ws, by=f"{app.name} sync")
    state = dict((conn.state or {}).get("syncs") or {})
    mine = dict(state.get(sync["key"]) or {})
    out = {"added": 0, "updated": 0, "refused": []}
    try:
        items, cursor = app.pull(_fresh(app, conn), sync["pull"], mine.get("cursor"))
    except AppProblem as exc:
        mine.update(at=datetime.utcnow().isoformat(), problem=str(exc))
        conn.state = {**(conn.state or {}), "syncs": {**state, sync["key"]: mine}}
        db.session.commit()
        raise
    for item in items:
        values = {field: _at(item, path) for field, path in sync["fields"].items()}
        values[sync["match"]] = str(item.get("id"))
        existing = next((r for r in Record.query.filter_by(workspace_id=ws.id, table=sync["into"], deleted_at=None)
                         if str((r.data or {}).get(sync["match"])) == str(item.get("id"))), None)
        try:
            with engine.atomic():
                if existing is not None:
                    engine.update(existing, {k: v for k, v in values.items() if v is not None})
                    out["updated"] += 1
                else:
                    record = engine.create(sync["into"], {k: v for k, v in values.items() if v is not None})
                    lines = dict(sync["lines"]) if sync.get("lines") else None
                    if lines and not lines.get("via"):  # the line table's one link to the list
                        from ..blueprint import schema
                        lines["via"] = schema._line_link(engine.tables[lines["into"]], sync["into"]) or ""
                    for line in (item.get("lines") or []) if lines else []:
                        engine.create(lines["into"], {**{f: _at(line, p) for f, p in lines["fields"].items() if _at(line, p) is not None},
                                                      lines["via"]: record.id})
                    out["added"] += 1
        except FieldErrors as exc:
            out["refused"].append(f"{app.name} {sync['pull']} {item.get('number') or item.get('id')}: {'; '.join(exc.errors.values())}")
    mine.update(at=datetime.utcnow().isoformat(), cursor=cursor or mine.get("cursor"), problem="; ".join(out["refused"][:3]) or None,
                added=mine.get("added", 0) + out["added"], updated=mine.get("updated", 0) + out["updated"])
    conn.state = {**(conn.state or {}), "syncs": {**state, sync["key"]: mine}}
    db.session.commit()
    return out


def _at(item, path):
    """A detail of an item by its path ("customer.email"); lists joined."""
    value = item
    for part in str(path).split("."):
        value = value.get(part) if isinstance(value, dict) else None
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return value
