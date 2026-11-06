"""A person's own account: their name, password, two-step sign-in, and whether notifications are also emailed."""
from fastapi import APIRouter, Depends, Request

from .. import audit, totp
from ..core.context import current_app
from ..extensions import db
from ..models import Membership, Workspace
from ..security import decrypt, encrypt
from .http import Form, PageRoute, check_csrf, flash, form_of, redirect, render, signed_in, url_for

router = APIRouter(route_class=PageRoute, dependencies=[Depends(check_csrf)])


@router.api_route("/account/", methods=["GET", "POST"], name="account.page")
def page(request: Request, user=Depends(signed_in), form: Form = Depends(form_of)):
    session = request.state.session
    if request.method == "POST":
        action = form.get("action")
        if action == "profile":
            user.name = (form.get("name") or "").strip()[:120]
            user.notify_email = bool(form.get("notify_email"))
            db.session.commit()
            flash(request, "Saved.", "success")
        elif action == "password":
            if not user.check_password(form.get("current") or ""):
                flash(request, "Your current password isn't right.", "error")
            elif len(form.get("new") or "") < 10:
                flash(request, "Use at least 10 characters for the new password.", "error")
            else:
                user.set_password(form["new"])
                db.session.commit()
                audit.log(None, "password_reset", "changed their password")
                flash(request, "Password changed.", "success")
        elif action == "2fa_start":
            session["totp_setup"] = totp.new_secret()
        elif action == "2fa_confirm":
            secret = session.get("totp_setup")
            if secret and totp.verify(secret, form.get("code")):
                user.totp_secret = encrypt(secret)
                db.session.commit()
                session.pop("totp_setup", None)
                audit.log(None, "two_step", "turned two-step sign-in on")
                flash(request, "Two-step sign-in is on: you'll be asked for a code from your app each time you sign in.", "success")
            else:
                flash(request, "That code isn't right. Check the app shows this account, and use the newest code.", "error")
        elif action == "ms_link":
            from ..connectors import microsoft
            try:
                url = microsoft.login_url(session)
            except microsoft.MicrosoftProblem as exc:
                flash(request, str(exc), "error")
                return redirect(url_for("account.page"))
            session["ms_link"] = True
            return redirect(url)
        elif action == "sso_link":
            from ..connectors import oidc
            try:
                return redirect(oidc.login_url(session, linking=True))
            except oidc.SSOProblem as exc:
                flash(request, str(exc), "error")
                return redirect(url_for("account.page"))
        elif action == "sso_unlink":
            user.sso_identity = None
            db.session.commit()
            audit.log(None, "sign_in_method", "unlinked single sign-on")
            flash(request, "Single sign-on is unlinked. Sign in with your password.", "success")
        elif action == "ms_unlink":
            user.ms_identity = None
            db.session.commit()
            audit.log(None, "sign_in_method", "unlinked Microsoft sign-in")
            flash(request, "Microsoft sign-in is unlinked. Sign in with your password.", "success")
        elif action == "2fa_off":
            secret = decrypt(user.totp_secret) if user.totp_secret else None
            if isinstance(secret, str) and totp.verify(secret, form.get("code")):
                user.totp_secret = None
                db.session.commit()
                audit.log(None, "two_step", "turned two-step sign-in off")
                flash(request, "Two-step sign-in is off.", "success")
            else:
                flash(request, "Enter a current code from your app to turn it off.", "error")
        return redirect(url_for("account.page"))
    setup = session.get("totp_setup")
    spaces = [(m, db.session.get(Workspace, m.workspace_id)) for m in Membership.query.filter_by(user_id=user.id)]
    return render(request, "account.html", setup=setup, spaces=spaces,
                  setup_uri=totp.setup_uri(setup, user.email, current_app.config["PLATFORM_NAME"]) if setup else None)
