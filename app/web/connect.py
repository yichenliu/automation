"""Connecting Microsoft 365: sending the person to Microsoft to sign in, and handling the way back."""

from ..connectors import microsoft
from ..security import workspace
from ..webkit import Blueprint, current_user, flash, login_required, redirect, request, session, url_for

bp = Blueprint("connect", __name__)


@bp.route("/w/<int:wid>/connect/microsoft", methods=["POST"])
@login_required
def start(wid):
    ws = workspace(wid, "admin")
    session["ms_next"] = request.form.get("next") if (request.form.get("next") or "").startswith("/") else url_for("settings.connections", wid=wid)
    try:
        return redirect(microsoft.sign_in_url(session, ws.id))
    except microsoft.MicrosoftProblem as exc:
        flash(str(exc), "error")
        return redirect(session.pop("ms_next"))


@bp.route("/connect/callback")
def callback():
    """Microsoft sends people back here both after connecting Microsoft 365 and after signing in with Microsoft."""
    if session.get("ms_login"):
        from .auth import finish_microsoft_login
        from .http import current_request
        return finish_microsoft_login(current_request(), request.args)
    if not current_user.is_authenticated:
        return redirect(url_for("auth.login"))
    target = session.pop("ms_next", None)
    try:
        wid = microsoft.finish_sign_in(session, request.args, current_user)
        workspace(wid, "admin")
        flash("Microsoft 365 is connected.", "success")
    except microsoft.MicrosoftProblem as exc:
        flash(str(exc), "error")
    return redirect(target or url_for("workspaces.home"))
