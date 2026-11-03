"""The audit log: what matters is written down as it happens, chained so that a change afterwards shows."""
from app import audit
from app.extensions import db
from app.models import AuditEvent

from .conftest import login
from .test_wave2 import _built


def test_what_matters_is_logged_and_tampering_shows(app, client, owner):
    user, ws = owner
    _built(ws, [{"op": "create_table", "table": "staff", "label": "Staff", "fields": [
        {"key": "name", "type": "text", "required": True}, {"key": "bank", "type": "text", "sensitive": True}]}])
    client.post("/login", data={"email": "ethan@example.com", "password": "wrong-password"})
    login(client)
    base, settings = f"/w/{ws.id}/app", f"/w/{ws.id}/settings"
    client.post(f"{settings}/members", data={"action": "invite", "email": "sam@example.com", "role": "editor"})
    client.get(f"{base}/t/staff/export.csv")
    client.post(f"{settings}/api", data={"name": "Shop", "role": "editor"})
    actions = [e.action for e in AuditEvent.query.order_by(AuditEvent.id)]
    assert actions[:3] == ["design", "sign_in_failed", "signed_in"]  # the design built before, then the sign-ins
    assert {"design", "invited", "exported", "api_key"} <= set(actions)
    page = client.get(f"{settings}/audit").get_data(as_text=True)
    assert "The log is whole" in page and "sam@example.com as editor" in page and "a wrong password for ethan@example.com" in page
    assert "Staff as CSV" in client.get(f"{settings}/audit?csv=1").get_data(as_text=True)
    assert audit.verify(ws.id)[0]
    entry = AuditEvent.query.filter_by(workspace_id=ws.id, action="exported").first()
    entry.detail = "nothing to see"  # changed afterwards
    db.session.commit()
    whole, at = audit.verify(ws.id)
    assert not whole and at.id == entry.id
    assert "has been tampered with" in client.get(f"{settings}/audit").get_data(as_text=True)


def test_an_entry_taken_out_shows_too(app, owner):
    _, ws = owner
    for n in range(3):
        audit.log(ws.id, "settings", f"change {n}", by="Ethan")
    middle = AuditEvent.query.filter_by(workspace_id=ws.id).order_by(AuditEvent.id).all()[1]
    db.session.delete(middle)
    db.session.commit()
    assert audit.verify(ws.id)[0] is False


def test_people_are_signed_out_after_the_time_chosen_without_use(app, client, owner, monkeypatch):
    import time
    user, ws = owner
    login(client)
    client.post(f"/w/{ws.id}/settings/", data={"name": ws.name, "idle_minutes": "15", "currency": "$"})
    assert ws.settings["idle_minutes"] == 15
    assert client.get(f"/w/{ws.id}/app/").status_code == 200
    later = time.time() + 16 * 60
    monkeypatch.setattr(time, "time", lambda: later)
    gone = client.get(f"/w/{ws.id}/app/")
    assert gone.status_code == 302 and "/login" in gone.headers["Location"]
    assert AuditEvent.query.filter_by(action="signed_out").first().detail == "after 15 minutes without use"


def test_single_sign_on_signs_in_only_an_account_its_linked_to(app, client, owner, monkeypatch):
    import base64
    import json
    from urllib.parse import parse_qs, urlparse
    from app.connectors import oidc
    user, ws = owner
    app.config.update(OIDC_ISSUER="https://login.example.com", OIDC_CLIENT_ID="keel", OIDC_CLIENT_SECRET="s3cret", OIDC_NAME="Okta")
    oidc._DISCOVERED.clear()
    said = {"aud": "keel", "sub": "u-77", "email": "ethan@example.com"}

    class Answer:
        def __init__(self, body):
            self.body = body

        def json(self):
            return self.body

    def token(nonce):
        claims = {"iss": "https://login.example.com", "nonce": nonce, "exp": 4102444800, **said}
        return "x." + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=") + ".sig"

    monkeypatch.setattr(oidc.requests, "get", lambda url, **kw: Answer(
        {"authorization_endpoint": "https://login.example.com/authorize", "token_endpoint": "https://login.example.com/token"}))
    nonces = {}
    monkeypatch.setattr(oidc.requests, "post", lambda url, **kw: Answer({"id_token": token(nonces["n"])}))

    def go(start):
        to = urlparse(client.get(start).headers["Location"]) if start.startswith("/login") else urlparse(client.post(start, data={"action": "sso_link"}).headers["Location"])
        q = parse_qs(to.query)
        nonces["n"] = q["nonce"][0]
        return client.get(f"/login/sso/callback?code=abc&state={q['state'][0]}", follow_redirects=True).get_data(as_text=True)

    assert "Sign in with Okta" in client.get("/login").get_data(as_text=True)
    assert "linked to an account here yet" in go("/login/sso")  # never matched by the email it claims
    login(client)
    assert "You can now sign in with Okta" in go("/account/")
    assert user.sso_identity == "https://login.example.com|u-77"
    client.post("/logout")
    go("/login/sso")
    assert client.get(f"/w/{ws.id}/app/").status_code == 200  # signed in by it
    client.post("/logout")
    said["aud"] = "someone-else"
    assert "for this platform" in go("/login/sso")
    said["aud"] = "keel"
    assert "has expired or wasn" in client.get("/login/sso/callback?code=abc&state=forged", follow_redirects=True).get_data(as_text=True)


def test_the_health_page_says_whats_wrong_and_what_to_do(app, client, owner):
    from app import ai, health
    user, ws = owner
    login(client)
    client.get(f"/w/{ws.id}/app/")
    page = client.get(f"/w/{ws.id}/settings/health").get_data(as_text=True)
    for part in ("The AI", "Background work", "Rules", "Screens", "Storage", "Backups"):
        assert part in page, part
    assert "make one now under Data &amp; AI" in page  # no backup yet
    assert health.screens()["count"] >= 1
