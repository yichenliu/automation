"""Microsoft 365: signing in, then mail and OneDrive through Microsoft Graph.

Signing in is OAuth 2.0 with PKCE against the Entra app named in the settings (MICROSOFT_CLIENT_ID/SECRET/TENANT); the
tokens are kept encrypted with the workspace's connection and refreshed when they run out. Graph calls retry when
Microsoft asks us to slow down, and a refused token is refreshed once before giving up. If the refresh itself is
refused, the connection is marked "needs sign-in" and the person is told, rather than failing quietly.
"""
import base64
import hashlib
import json
import re
import secrets
import threading
import time
from datetime import timedelta
from urllib.parse import quote

import markdown
import nh3
import requests

from ..extensions import db
from ..models import Connection, utcnow
from ..security import decrypt, encrypt
from ..core.settings import settings

GRAPH = "https://graph.microsoft.com/v1.0"
AUTHORITY = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0"
SCOPES = "offline_access openid profile email User.Read Mail.ReadWrite Mail.Send Files.ReadWrite"
MESSAGE_FIELDS = "id,subject,from,toRecipients,ccRecipients,receivedDateTime,hasAttachments,bodyPreview,body,internetMessageId,webLink"
_locks = {}


class MicrosoftProblem(Exception):
    """Something a person can act on: not connected, sign-in expired, a refused request."""


def configured():
    cfg = settings()
    return bool(cfg["MICROSOFT_CLIENT_ID"] and cfg["MICROSOFT_CLIENT_SECRET"])


def connection(workspace, create=False):
    found = Connection.query.filter_by(workspace_id=workspace.id, kind="microsoft365").first()
    if found is None and create:
        found = Connection(workspace_id=workspace.id, kind="microsoft365", state={})
        db.session.add(found)
        db.session.commit()
    return found


def redirect_uri():
    return f"{settings()['BASE_URL']}/connect/callback"


def _authority():
    return AUTHORITY.format(tenant=settings()["MICROSOFT_TENANT"] or "organizations")


def sign_in_url(session, workspace_id):
    if not configured():
        raise MicrosoftProblem("Microsoft 365 isn't set up on this platform yet: add MICROSOFT_CLIENT_ID and MICROSOFT_CLIENT_SECRET to .env.")
    state, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    session["ms_oauth"] = {"state": state, "verifier": verifier, "workspace": workspace_id, "at": time.time()}
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    params = {"client_id": settings()["MICROSOFT_CLIENT_ID"], "response_type": "code", "redirect_uri": redirect_uri(),
              "response_mode": "query", "scope": SCOPES, "state": state, "code_challenge": challenge,
              "code_challenge_method": "S256", "prompt": "select_account"}
    return requests.Request("GET", f"{_authority()}/authorize", params=params).prepare().url


def login_url(session):
    """Signing in to the platform with a Microsoft work account (OpenID Connect, with PKCE)."""
    if not configured():
        raise MicrosoftProblem("Signing in with Microsoft isn't set up on this platform: add MICROSOFT_CLIENT_ID and MICROSOFT_CLIENT_SECRET to .env.")
    state, verifier, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(48), secrets.token_urlsafe(16)
    session["ms_login"] = {"state": state, "verifier": verifier, "nonce": nonce, "at": time.time()}
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    params = {"client_id": settings()["MICROSOFT_CLIENT_ID"], "response_type": "code", "redirect_uri": redirect_uri(),
              "response_mode": "query", "scope": "openid email profile", "state": state, "nonce": nonce,
              "code_challenge": challenge, "code_challenge_method": "S256", "prompt": "select_account"}
    return requests.Request("GET", f"{_authority()}/authorize", params=params).prepare().url


def finish_login(session, args):
    """The claims of the account that signed in (name, email), from the ID token Microsoft returned to this server."""
    pending = session.pop("ms_login", None)
    if not pending or args.get("state") != pending["state"] or time.time() - pending["at"] > 900:
        raise MicrosoftProblem("That sign-in has expired or was already used. Try again.")
    if args.get("error"):
        raise MicrosoftProblem(f"Microsoft said: {args.get('error_description') or args.get('error')}")
    body = _token({"grant_type": "authorization_code", "code": args.get("code", ""), "redirect_uri": redirect_uri(),
                   "code_verifier": pending["verifier"], "scope": "openid email profile"})
    token = body.get("id_token") or ""
    try:
        claims = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "==="))
    except (IndexError, ValueError) as exc:
        raise MicrosoftProblem("Microsoft didn't say who signed in.") from exc
    if claims.get("nonce") != pending["nonce"] or claims.get("aud") != settings()["MICROSOFT_CLIENT_ID"] or claims.get("exp", 0) < time.time():
        raise MicrosoftProblem("That sign-in couldn't be checked. Try again.")
    return claims


def finish_sign_in(session, args, user):
    """Handle Microsoft's redirect back: swap the code for tokens and remember who signed in. Returns the workspace id."""
    pending = session.pop("ms_oauth", None)
    if not pending or args.get("state") != pending["state"] or time.time() - pending["at"] > 1800:
        raise MicrosoftProblem("That sign-in has expired or was already used. Start it again.")
    if args.get("error"):
        raise MicrosoftProblem(f"Microsoft said: {args.get('error_description') or args.get('error')}")
    body = _token({"grant_type": "authorization_code", "code": args.get("code", ""), "redirect_uri": redirect_uri(),
                   "code_verifier": pending["verifier"]})
    from ..models import Workspace
    ws = db.session.get(Workspace, pending["workspace"])
    conn = connection(ws, create=True)
    _store(conn, body)
    me = graph(ws, "GET", "/me", params={"$select": "displayName,mail,userPrincipalName"})
    conn.account = me.get("mail") or me.get("userPrincipalName")
    conn.status, conn.problem, conn.connected_by, conn.checked_at = "connected", None, user.id, utcnow()
    conn.state = {**(conn.state or {}), "mail_since": utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")}
    db.session.commit()
    return ws.id


def _token(form):
    cfg = settings()
    response = requests.post(f"{_authority()}/token", timeout=30, data={  # a sign-in asks only for its own scopes
        "scope": SCOPES, **form, "client_id": cfg["MICROSOFT_CLIENT_ID"], "client_secret": cfg["MICROSOFT_CLIENT_SECRET"]})
    body = response.json() if response.content else {}
    if response.status_code >= 400 or ("access_token" not in body and "id_token" not in body):
        reason = str(body.get("error_description") or body.get("error") or response.status_code).splitlines()[0][:300]
        raise MicrosoftProblem(f"Microsoft refused the sign-in: {reason}")
    return body


def _store(conn, body):
    secret = decrypt(conn.secret)
    secret["access_token"] = body["access_token"]
    if body.get("refresh_token"):
        secret["refresh_token"] = body["refresh_token"]
    secret["expires_at"] = time.time() + int(body.get("expires_in", 3600)) - 120
    conn.secret = encrypt(secret)
    db.session.commit()


def _access_token(conn, force=False):
    with _locks.setdefault(conn.id, threading.Lock()):
        db.session.refresh(conn)
        secret = decrypt(conn.secret)
        if not force and secret.get("access_token") and secret.get("expires_at", 0) > time.time():
            return secret["access_token"]
        if not secret.get("refresh_token"):
            raise MicrosoftProblem("Microsoft 365 isn't signed in. Connect it in Settings.")
        try:
            body = _token({"grant_type": "refresh_token", "refresh_token": secret["refresh_token"]})
        except MicrosoftProblem as exc:
            conn.status, conn.problem = "needs sign-in", str(exc)
            db.session.commit()
            raise MicrosoftProblem("The Microsoft 365 sign-in has expired. Sign in again in Settings → Connections.") from exc
        _store(conn, body)
        return body["access_token"]


def graph(workspace, method, path, *, params=None, json=None, raw=False, headers=None, stream=False):
    """One Graph call for this workspace's connection. raw=True returns the response (for downloads)."""
    conn = connection(workspace)
    if conn is None or conn.status == "not connected" or not conn.secret:
        raise MicrosoftProblem("Microsoft 365 isn't connected. Connect it in Settings → Connections.")
    url = path if path.startswith("https://") else f"{GRAPH}{path}"
    refreshed = False
    for attempt in range(5):
        token = _access_token(conn)
        response = requests.request(method, url, params=params, json=json, timeout=60, stream=stream,
                                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json", **(headers or {})})
        if response.status_code == 401 and not refreshed:
            _access_token(conn, force=True)
            refreshed = True
            continue
        if response.status_code in (429, 503, 504):
            time.sleep(min(int(response.headers.get("Retry-After", 2 ** attempt)), 30))
            continue
        if response.status_code >= 400:
            try:
                message = response.json().get("error", {}).get("message")
            except ValueError:
                message = response.text[:200]
            raise MicrosoftProblem(f"Microsoft 365 refused {method} {path.split('?')[0]}: {message or response.status_code}")
        if raw:
            return response
        return response.json() if response.content else {}
    raise MicrosoftProblem("Microsoft 365 is busy; it will be tried again shortly.")


def pages(workspace, path, params=None, limit=1000):
    """Every item of a Graph list, following its next links, up to `limit`."""
    out, body = [], graph(workspace, "GET", path, params=params)
    while True:
        out += body.get("value", [])
        nxt = body.get("@odata.nextLink")
        if not nxt or len(out) >= limit:
            return out[:limit]
        body = graph(workspace, "GET", nxt)


# --- mail ------------------------------------------------------------------------------------------------------------

def _address(entry):
    return ((entry or {}).get("emailAddress") or {}).get("address", "")


def html_to_text(html):
    text = re.sub(r"(?is)<(script|style).*?</\1>", "", html or "")
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def simplify(message):
    body = message.get("body") or {}
    text = body.get("content", "") if body.get("contentType") == "text" else html_to_text(body.get("content", ""))
    return {"id": message["id"], "subject": message.get("subject") or "", "from": _address(message.get("from")),
            "from_name": ((message.get("from") or {}).get("emailAddress") or {}).get("name", ""),
            "to": ", ".join(_address(x) for x in message.get("toRecipients") or []),
            "received": message.get("receivedDateTime"), "has_attachments": bool(message.get("hasAttachments")),
            "preview": message.get("bodyPreview", ""), "body": text[:20000], "link": message.get("webLink")}


def recent_messages(workspace, days=180, limit=400, folder="inbox"):
    since = (utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return [simplify(m) for m in pages(workspace, f"/me/mailFolders/{folder}/messages", limit=limit, params={
        "$select": MESSAGE_FIELDS, "$filter": f"receivedDateTime ge {since}", "$orderby": "receivedDateTime desc", "$top": 50})]


def new_messages(workspace, since, limit=50):
    return [simplify(m) for m in pages(workspace, "/me/mailFolders/inbox/messages", limit=limit, params={
        "$select": MESSAGE_FIELDS, "$filter": f"receivedDateTime gt {since}", "$orderby": "receivedDateTime asc", "$top": 25})]


def save_attachments(workspace, message_id, by="email"):
    """Keep a message's file attachments (not inline images). Returns File rows."""
    from ..storage import FileRejected, save
    files = []
    for a in pages(workspace, f"/me/messages/{message_id}/attachments", limit=20, params={"$select": "id,name,contentType,size,isInline"}):
        if a.get("isInline") or a.get("@odata.type") != "#microsoft.graph.fileAttachment":
            continue
        content = graph(workspace, "GET", f"/me/messages/{message_id}/attachments/{a['id']}/$value", raw=True).content
        try:
            files.append(save(workspace.id, content, a.get("name") or "attachment", source="email", by=by, mime=a.get("contentType")))
        except FileRejected:
            continue
    return files


def send_mail(workspace, to, subject, body, cc="", attachments=()):
    """Send from the connected mailbox. body is plain text or Markdown; attachments are (name, bytes, mime)."""
    html = nh3.clean(markdown.markdown(body, extensions=["nl2br", "sane_lists"]))
    message = {"subject": subject, "body": {"contentType": "HTML", "content": html},
               "toRecipients": [{"emailAddress": {"address": a}} for a in re.split(r"\s*[,;]\s*", to) if a],
               "ccRecipients": [{"emailAddress": {"address": a}} for a in re.split(r"\s*[,;]\s*", cc or "") if a]}
    if attachments:
        if sum(len(content) for _, content, _ in attachments) > 3 * 1024 * 1024:
            raise MicrosoftProblem("The attachments are over 3 MB, too large to send this way.")
        message["attachments"] = [{"@odata.type": "#microsoft.graph.fileAttachment", "name": name, "contentType": mime or "application/octet-stream",
                                   "contentBytes": base64.b64encode(content).decode()} for name, content, mime in attachments]
    graph(workspace, "POST", "/me/sendMail", json={"message": message, "saveToSentItems": True})


# --- OneDrive --------------------------------------------------------------------------------------------------------

def _item_path(folder):
    folder = (folder or "").strip("/")
    return "/me/drive/root" if not folder else f"/me/drive/root:/{quote(folder)}:"


def list_folder(workspace, folder=""):
    items = pages(workspace, f"{_item_path(folder)}/children", limit=500,
                  params={"$select": "id,name,size,file,folder,lastModifiedDateTime,parentReference,webUrl", "$top": 200})
    return [{"id": i["id"], "name": i["name"], "folder": "folder" in i, "size": i.get("size", 0),
             "modified": i.get("lastModifiedDateTime"), "path": ((folder.strip("/") + "/") if folder.strip("/") else "") + i["name"],
             "mime": (i.get("file") or {}).get("mimeType"), "children": (i.get("folder") or {}).get("childCount", 0)} for i in items]


def walk(workspace, folder="", depth=4, limit=500):
    """Files under a folder (breadth first), skipping obvious system folders."""
    out, queue = [], [(folder, 0)]
    while queue and len(out) < limit:
        current, level = queue.pop(0)
        try:
            items = list_folder(workspace, current)
        except MicrosoftProblem:
            continue
        for item in items:
            if item["folder"]:
                if level + 1 < depth and not item["name"].startswith((".", "~")) and item["name"] not in ("Apps", "Attachments", "Recordings"):
                    queue.append((item["path"], level + 1))
            else:
                out.append(item)
    return out[:limit]


def download(workspace, item_id, name, by="OneDrive"):
    from ..storage import save
    response = graph(workspace, "GET", f"/me/drive/items/{item_id}/content", raw=True)
    return save(workspace.id, response.content, name, source="onedrive", by=by)


def status(workspace):
    conn = connection(workspace)
    if conn is None:
        return {"connected": False, "configured": configured()}
    return {"connected": conn.status == "connected", "status": conn.status, "account": conn.account, "problem": conn.problem,
            "configured": configured(), "checked_at": conn.checked_at}


def disconnect(workspace):
    conn = connection(workspace)
    if conn is not None:
        conn.secret, conn.status, conn.account, conn.problem = None, "not connected", None, None
        db.session.commit()
