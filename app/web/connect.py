"""Connecting Microsoft 365: sending the person to Microsoft to sign in, and handling the way back."""
from fastapi import APIRouter, Depends, Request

from ..connectors import microsoft
from ..security import member_of, workspace
from .http import Form, PageRoute, check_csrf, current_user, flash, form_of, redirect, url_for

router = APIRouter(route_class=PageRoute, dependencies=[Depends(check_csrf)])


@router.api_route("/w/{wid:int}/connect/microsoft", methods=["POST"], name="connect.start")
def start(request: Request, wid: int, ws=Depends(member_of("admin")), form: Form = Depends(form_of)):
    session = request.state.session
    session["ms_next"] = form.get("next") if (form.get("next") or "").startswith("/") else url_for("settings.connections", wid=wid)
    try:
        return redirect(microsoft.sign_in_url(session, ws.id))
    except microsoft.MicrosoftProblem as exc:
        flash(request, str(exc), "error")
        return redirect(session.pop("ms_next"))


@router.api_route("/connect/callback", methods=["GET"], name="connect.callback")
def callback(request: Request):
    """Microsoft sends people back here both after connecting Microsoft 365 and after signing in with Microsoft."""
    session = request.state.session
    if session.get("ms_login"):
        from .auth import finish_microsoft_login
        return finish_microsoft_login(request, request.query_params)
    who = current_user(request)
    if not who.is_authenticated:
        return redirect(url_for("auth.login"))
    target = session.pop("ms_next", None)
    try:
        wid = microsoft.finish_sign_in(session, request.query_params, who)
        workspace(wid, "admin", request)
        flash(request, "Microsoft 365 is connected.", "success")
    except microsoft.MicrosoftProblem as exc:
        flash(request, str(exc), "error")
    return redirect(target or url_for("workspaces.home"))
