"""Connecting the ready-made apps (connectors/catalog.py): Settings › Connections uses these, and two addresses the apps
call back on: signing in to Xero or QuickBooks, and Stripe telling the app a payment was made."""
import hashlib
import hmac
import json
import time

from .. import audit
from ..connectors import apps
from ..extensions import csrf, db
from ..models import Workspace
from ..security import workspace
from ..webkit import Blueprint, abort, current_user, flash, login_required, redirect, request, session, url_for

bp = Blueprint("apps", __name__)
csrf.exempt("app.web.connect_apps.stripe_events")


@bp.route("/w/<int:wid>/settings/apps/<key>", methods=["POST"])
@login_required
def connect(wid, key):
    """Connect (with the details asked for, or by signing in), disconnect, or bring a sync in now."""
    ws = workspace(wid, "admin")
    app = apps.get(key)
    if app is None:
        abort(404)
    action = request.form.get("action", "connect")
    back = url_for("settings.connections", wid=wid) + f"#app-{key}"
    if action == "disconnect":
        apps.disconnect(ws, key)
        audit.log(ws.id, "connected", f"{app.name} disconnected")
        flash(f"{app.name} disconnected.", "success")
        return redirect(back)
    if action == "sync":
        sync = next((s for s in (ws.blueprint or {}).get("syncs") or [] if s["key"] == request.form.get("sync")), None)
        if sync is None:
            abort(404)
        try:
            done = apps.run_sync(ws, sync)
            flash(f"From {app.name}: {done['added']} added, {done['updated']} updated" + (f"; {len(done['refused'])} refused (see below)" if done["refused"] else "") + ".",
                  "success" if not done["refused"] else "error")
        except apps.AppProblem as exc:
            flash(str(exc), "error")
        return redirect(back)
    if app.oauth:
        try:
            state = f"{wid}:{key}:{apps_state()}"
            session["app_oauth"] = state
            return redirect(app.sign_in_url(redirect_uri(), state))
        except apps.AppProblem as exc:
            flash(str(exc), "error")
            return redirect(back)
    try:
        account = apps.connect(ws, key, request.form, current_user.id)
    except apps.AppProblem as exc:
        flash(str(exc), "error")
        return redirect(back)
    audit.log(ws.id, "connected", f"{app.name} connected ({account})")
    flash(f"{app.name} is connected: {account}.", "success")
    return redirect(back)


def apps_state():
    import secrets
    return secrets.token_urlsafe(18)


def redirect_uri():
    from ..webkit import current_app
    return f"{current_app.config['BASE_URL']}{url_for('apps.oauth_callback')}"


@bp.route("/apps/oauth/callback")
@login_required
def oauth_callback():
    """Back from signing in to Xero or QuickBooks: the tokens kept for the workspace it was started from."""
    expected = session.pop("app_oauth", None)
    if not expected or request.args.get("state") != expected:
        flash("That sign-in wasn't started here or has expired. Try connecting again.", "error")
        return redirect(url_for("workspaces.home"))
    wid, key, _ = expected.split(":", 2)
    ws = workspace(int(wid), "admin")
    app = apps.get(key)
    if request.args.get("error"):
        flash(f"{app.name} wasn't connected: {request.args.get('error_description') or request.args['error']}", "error")
        return redirect(url_for("settings.connections", wid=ws.id))
    try:
        details = app.finish(request.args.get("code", ""), redirect_uri(), request.args)
        account = details.pop("account", None) or app.check(details)
    except apps.AppProblem as exc:
        flash(str(exc), "error")
        return redirect(url_for("settings.connections", wid=ws.id))
    apps.store_tokens(ws, key, details, account, current_user.id)
    audit.log(ws.id, "connected", f"{app.name} connected ({account})")
    flash(f"{app.name} is connected: {account}.", "success")
    return redirect(url_for("settings.connections", wid=ws.id) + f"#app-{key}")


@bp.route("/paid")
def paid():
    """Where Stripe sends a customer back after paying."""
    from ..webkit import current_app
    return (f"<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Paid</title>"
            f"<body style='font-family:system-ui;max-width:32rem;margin:15vh auto;padding:0 16px;text-align:center'><h1>Thank you</h1>"
            f"<p>Your payment was received. You can close this page.</p><p style='color:#888'>{current_app.config.get('PLATFORM_NAME') or ''}</p></body>")


@bp.route("/apps/stripe/<int:wid>/events", methods=["POST"])
def stripe_events(wid):
    """Stripe saying a payment was made: checked by its signature, then the record it was for gets what "paid" sets."""
    from ..engine.records import Engine, FieldErrors
    ws = db.session.get(Workspace, wid)
    conn = apps.connection(ws, "stripe") if ws else None
    secret = apps.creds(conn).get("webhook_secret") if conn else None
    if not secret or not _signed(request.get_data(), request.headers.get("Stripe-Signature", ""), secret):
        abort(400)
    event = request.get_json(silent=True) or {}
    paid = (event.get("data") or {}).get("object") or {}
    if event.get("type") != "checkout.session.completed" or paid.get("payment_status") != "paid":
        return {"received": True}
    meta = paid.get("metadata") or {}
    if str(meta.get("workspace")) != str(ws.id):
        return {"received": True}
    engine = Engine(ws, by="Stripe")
    record = engine.get(int(meta.get("record") or 0))
    if record is None:
        return {"received": True}
    try:
        values = json.loads(meta.get("paid") or "{}")
        if values:
            engine.update(record, values)
    except (ValueError, FieldErrors) as exc:
        from ..webkit import current_app
        current_app.logger.warning("Stripe paid record %s, but it couldn't be marked: %s", record.id, exc)
    return {"received": True}


def _signed(body, header, secret, tolerance=300):
    """Stripe's signature: t=<time>,v1=<HMAC-SHA256 of "<time>.<body>"> with the endpoint's signing secret."""
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    stamp = parts.get("t", "")
    if not stamp.isdigit() or abs(time.time() - int(stamp)) > tolerance:
        return False
    expected = hmac.new(secret.encode(), f"{stamp}.".encode() + body, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, v) for k, v in (p.split("=", 1) for p in header.split(",") if "=" in p) if k == "v1")
