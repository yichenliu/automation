"""A person's own account: their name, password, two-step sign-in, and whether notifications are also emailed."""
from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required

from .. import totp
from ..extensions import db
from ..models import Membership, Workspace
from ..security import decrypt, encrypt

bp = Blueprint("account", __name__, url_prefix="/account")


@bp.route("/", methods=["GET", "POST"])
@login_required
def page():
    user = current_user
    if request.method == "POST":
        action = request.form.get("action")
        if action == "profile":
            user.name = (request.form.get("name") or "").strip()[:120]
            user.notify_email = bool(request.form.get("notify_email"))
            db.session.commit()
            flash("Saved.", "success")
        elif action == "password":
            if not user.check_password(request.form.get("current") or ""):
                flash("Your current password isn't right.", "error")
            elif len(request.form.get("new") or "") < 10:
                flash("Use at least 10 characters for the new password.", "error")
            else:
                user.set_password(request.form["new"])
                db.session.commit()
                flash("Password changed.", "success")
        elif action == "2fa_start":
            session["totp_setup"] = totp.new_secret()
        elif action == "2fa_confirm":
            secret = session.get("totp_setup")
            if secret and totp.verify(secret, request.form.get("code")):
                user.totp_secret = encrypt(secret)
                db.session.commit()
                session.pop("totp_setup", None)
                flash("Two-step sign-in is on: you'll be asked for a code from your app each time you sign in.", "success")
            else:
                flash("That code isn't right. Check the app shows this account, and use the newest code.", "error")
        elif action == "ms_link":
            from ..connectors import microsoft
            try:
                url = microsoft.login_url(session)
            except microsoft.MicrosoftProblem as exc:
                flash(str(exc), "error")
                return redirect(url_for("account.page"))
            session["ms_link"] = True
            return redirect(url)
        elif action == "ms_unlink":
            user.ms_identity = None
            db.session.commit()
            flash("Microsoft sign-in is unlinked. Sign in with your password.", "success")
        elif action == "2fa_off":
            secret = decrypt(user.totp_secret) if user.totp_secret else None
            if isinstance(secret, str) and totp.verify(secret, request.form.get("code")):
                user.totp_secret = None
                db.session.commit()
                flash("Two-step sign-in is off.", "success")
            else:
                flash("Enter a current code from your app to turn it off.", "error")
        return redirect(url_for("account.page"))
    setup = session.get("totp_setup")
    spaces = [(m, db.session.get(Workspace, m.workspace_id)) for m in Membership.query.filter_by(user_id=user.id)]
    return render_template("account.html", setup=setup, spaces=spaces,
                           setup_uri=totp.setup_uri(setup, user.email, current_app.config["PLATFORM_NAME"]) if setup else None)
