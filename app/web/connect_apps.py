"""Connecting the ready-made apps (connectors/catalog.py): Settings › Connections uses these, and two addresses the apps
call back on: signing in to Xero or QuickBooks, and Stripe telling the app a payment was made."""
import hashlib
import hmac
import json
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.responses import HTMLResponse

from .. import audit
from ..connectors import apps
from ..core.context import current_app
from ..extensions import db
from ..models import Workspace
from ..security import member_of, workspace
from .http import Form, PageRoute, body_of, check_csrf, current_user, flash, form_of, json_of, json_response, redirect, signed_in, url_for

router = APIRouter(route_class=PageRoute, dependencies=[Depends(check_csrf)])
stripe = APIRouter(route_class=PageRoute)  # Stripe's calls: checked by their signature, not a form's token


@router.api_route("/w/{wid:int}/settings/apps/{key}", methods=["POST"], name="apps.connect")
def connect(request: Request, wid: int, key: str, ws=Depends(member_of("admin")), form: Form = Depends(form_of)):
    """Connect (with the details asked for, or by signing in), disconnect, or bring a sync in now."""
    app = apps.get(key)
    if app is None:
        raise HTTPException(404)
    action = form.get("action", "connect")
    back = url_for("settings.connections", wid=wid) + f"#app-{key}"
    if action == "disconnect":
        apps.disconnect(ws, key)
        audit.log(ws.id, "connected", f"{app.name} disconnected")
        flash(request, f"{app.name} disconnected.", "success")
        return redirect(back)
    if action == "sync":
        sync = next((s for s in (ws.blueprint or {}).get("syncs") or [] if s["key"] == form.get("sync")), None)
        if sync is None:
            raise HTTPException(404)
        try:
            done = apps.run_sync(ws, sync)
            flash(request, f"From {app.name}: {done['added']} added, {done['updated']} updated" + (f"; {len(done['refused'])} refused (see below)" if done["refused"] else "") + ".",
                  "success" if not done["refused"] else "error")
        except apps.AppProblem as exc:
            flash(request, str(exc), "error")
        return redirect(back)
    if app.oauth:
        try:
            state = f"{wid}:{key}:{apps_state()}"
            request.state.session["app_oauth"] = state
            return redirect(app.sign_in_url(redirect_uri(), state))
        except apps.AppProblem as exc:
            flash(request, str(exc), "error")
            return redirect(back)
    try:
        account = apps.connect(ws, key, form, current_user(request).id)
    except apps.AppProblem as exc:
        flash(request, str(exc), "error")
        return redirect(back)
    audit.log(ws.id, "connected", f"{app.name} connected ({account})")
    flash(request, f"{app.name} is connected: {account}.", "success")
    return redirect(back)


def apps_state():
    import secrets
    return secrets.token_urlsafe(18)


def redirect_uri():
    return f"{current_app.config['BASE_URL']}{url_for('apps.oauth_callback')}"


@router.api_route("/apps/oauth/callback", methods=["GET"], name="apps.oauth_callback")
def oauth_callback(request: Request, user=Depends(signed_in)):
    """Back from signing in to Xero or QuickBooks: the tokens kept for the workspace it was started from."""
    args = request.query_params
    expected = request.state.session.pop("app_oauth", None)
    if not expected or args.get("state") != expected:
        flash(request, "That sign-in wasn't started here or has expired. Try connecting again.", "error")
        return redirect(url_for("workspaces.home"))
    wid, key, _ = expected.split(":", 2)
    ws = workspace(int(wid), "admin", request)
    app = apps.get(key)
    if args.get("error"):
        flash(request, f"{app.name} wasn't connected: {args.get('error_description') or args['error']}", "error")
        return redirect(url_for("settings.connections", wid=ws.id))
    try:
        details = app.finish(args.get("code", ""), redirect_uri(), args)
        account = details.pop("account", None) or app.check(details)
    except apps.AppProblem as exc:
        flash(request, str(exc), "error")
        return redirect(url_for("settings.connections", wid=ws.id))
    apps.store_tokens(ws, key, details, account, user.id)
    audit.log(ws.id, "connected", f"{app.name} connected ({account})")
    flash(request, f"{app.name} is connected: {account}.", "success")
    return redirect(url_for("settings.connections", wid=ws.id) + f"#app-{key}")


@router.api_route("/paid", methods=["GET"], name="apps.paid")
def paid(request: Request):
    """Where Stripe sends a customer back after paying."""
    return HTMLResponse(f"<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Paid</title>"
                        f"<body style='font-family:system-ui;max-width:32rem;margin:15vh auto;padding:0 16px;text-align:center'><h1>Thank you</h1>"
                        f"<p>Your payment was received. You can close this page.</p><p style='color:#888'>{current_app.config.get('PLATFORM_NAME') or ''}</p></body>")


@stripe.api_route("/apps/stripe/{wid:int}/events", methods=["POST"], name="apps.stripe_events")
def stripe_events(request: Request, wid: int, body: bytes = Depends(body_of), event=Depends(json_of)):
    """Stripe saying a payment was made: checked by its signature, then the record it was for gets what "paid" sets."""
    from ..engine.records import Engine, FieldErrors
    ws = db.session.get(Workspace, wid)
    conn = apps.connection(ws, "stripe") if ws else None
    secret = apps.creds(conn).get("webhook_secret") if conn else None
    if not secret or not _signed(body, request.headers.get("Stripe-Signature", ""), secret):
        raise HTTPException(400)
    event = event or {}
    paid = (event.get("data") or {}).get("object") or {}
    if event.get("type") != "checkout.session.completed" or paid.get("payment_status") != "paid":
        return json_response({"received": True})
    meta = paid.get("metadata") or {}
    if str(meta.get("workspace")) != str(ws.id):
        return json_response({"received": True})
    engine = Engine(ws, by="Stripe")
    record = engine.get(int(meta.get("record") or 0))
    if record is None:
        return json_response({"received": True})
    try:
        values = json.loads(meta.get("paid") or "{}")
        if values:
            engine.update(record, values)
    except (ValueError, FieldErrors) as exc:
        current_app.logger.warning("Stripe paid record %s, but it couldn't be marked: %s", record.id, exc)
    return json_response({"received": True})


def _signed(body, header, secret, tolerance=300):
    """Stripe's signature: t=<time>,v1=<HMAC-SHA256 of "<time>.<body>"> with the endpoint's signing secret."""
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    stamp = parts.get("t", "")
    if not stamp.isdigit() or abs(time.time() - int(stamp)) > tolerance:
        return False
    expected = hmac.new(secret.encode(), f"{stamp}.".encode() + body, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, v) for k, v in (p.split("=", 1) for p in header.split(",") if "=" in p) if k == "v1")
