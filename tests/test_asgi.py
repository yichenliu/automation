"""The FastAPI front: the Keel language's tools as REST v2, a builder turn's progress streamed, and the pages
still served through it."""
from starlette.testclient import TestClient

from app import lang
from app.api import create
from app.blueprint import schema
from app.extensions import db
from app.models import Message
from app.web.api import make_key


def _shop(ws):
    ws.blueprint = lang.apply(schema.empty("Shop"), [
        {"do": "add_list", "key": "invoices", "label": "Invoices", "singular": "Invoice", "fields": [{"type": "text", "key": "name", "label": "Name"}],
         "stages": {"stages": ["Draft", "Sent", "Paid"]}}]).blueprint
    db.session.commit()


def test_an_admins_key_changes_the_app_through_rest_v2_and_the_pages_still_work(app, owner):
    user, ws = owner
    _shop(ws)
    _, admin = make_key(ws, "Owner's script", "admin", user.id)
    _, staff = make_key(ws, "Staff's script", "editor", user.id)
    client = TestClient(create(app))
    assert client.get("/api/v2/app").status_code == 401
    assert "stage_alert" in client.get("/api/v2/language").json()["catalog"]
    now = client.get("/api/v2/app", headers={"Authorization": f"Bearer {admin}"}).json()
    assert "invoices “Invoices”" in now["app"]
    patch = {"do": "stage_alert", "list": "invoices", "stage": "Paid"}
    assert client.post("/api/v2/app/what-if", json={"patches": [patch]}, headers={"Authorization": f"Bearer {staff}"}).status_code == 403
    tried = client.post("/api/v2/app/what-if", json={"patches": [patch]}, headers={"Authorization": f"Bearer {admin}"}).json()
    assert tried["refused"] == [] and tried["checks"] and all(c["ok"] for c in tried["checks"])
    done = client.post("/api/v2/app/apply", json={"patches": [patch], "version": now["version"]}, headers={"Authorization": f"Bearer {admin}"}).json()
    assert done["saved"] and done["version"] == now["version"] + 1
    assert client.get("/login").status_code == 200  # a page, through the mount


def test_a_builder_turns_progress_streams_until_it_is_done(app, owner):
    user, ws = owner
    _, key = make_key(ws, "Watcher", "viewer", user.id)
    m = Message(workspace_id=ws.id, channel="builder", role="assistant", status="done", progress=["Working out what you need", "Making the lists"],
                text="Your app is ready to try.")
    db.session.add(m)
    db.session.commit()
    client = TestClient(create(app))
    with client.stream("GET", f"/api/v2/builder/{m.id}/events", headers={"Authorization": f"Bearer {key}"}) as response:
        body = "".join(response.iter_text())
    assert body.count("event: progress") == 2 and "event: done" in body and "ready to try" in body
