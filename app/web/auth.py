"""Signing in (with a password, a two-step code when it's turned on, or Microsoft), creating an account, joining a
workspace from an invitation, and resetting a forgotten password with a link."""
import time

from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .. import audit, totp
from ..extensions import db
from ..models import Invite, Membership, User, Workspace, utcnow
from ..security import client_ip, decrypt, sign_ins

bp = Blueprint("auth", __name__)


def _safe_next(default):
    target = request.args.get("next") or request.form.get("next") or ""
    return target if target.startswith("/") and not target.startswith("//") else default


def _join(invite, user):
    if not Membership.query.filter_by(workspace_id=invite.workspace_id, user_id=user.id).first():
        db.session.add(Membership(workspace_id=invite.workspace_id, user_id=user.id, role=invite.role, contact_id=invite.contact_id,
                                  teams=invite.teams or None))
    invite.accepted_at = utcnow()
    db.session.commit()


def landing(workspace_id, user):
    """Where someone goes in a workspace: the ERP, or the portal for outside people."""
    member = Membership.query.filter_by(workspace_id=workspace_id, user_id=user.id).first()
    if member is not None and member.role == "portal":
        return url_for("portal.home", wid=workspace_id)
    return url_for("app.home", wid=workspace_id)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(_safe_next(url_for("workspaces.home")))
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        key = f"{client_ip()}:{email}"
        if sign_ins.blocked(key):  # only wrong passwords count
            flash("Too many wrong passwords. Wait 15 minutes and try again.", "error")
            return render_template("auth/login.html", email=email), 429
        user = User.query.filter_by(email=email).first()
        if user and user.check_password(request.form.get("password") or ""):
            if user.totp_secret:  # the password was right; now the code from their authenticator app
                session["pending_2fa"] = {"user": user.id, "at": time.time(), "next": _safe_next("")}
                return redirect(url_for("auth.code"))
            return _signed_in(user)
        sign_ins.hit(key)
        audit.log(None, "sign_in_failed", f"a wrong password for {email}", user=user, by=email or "someone")
        flash("That email and password don't match.", "error")
        return render_template("auth/login.html", email=email), 401
    return render_template("auth/login.html", email="")


def _signed_in(user, next_url=""):
    login_user(user, remember=True)
    audit.log(None, "signed_in", "", user=user)
    user.last_seen_at = utcnow()
    db.session.commit()
    invite = Invite.query.filter_by(token=session.pop("invite", None) or "", accepted_at=None).first()
    if invite:
        _join(invite, user)
        return redirect(landing(invite.workspace_id, user))
    target = next_url or _safe_next("")
    return redirect(target if target.startswith("/") and not target.startswith("//") else url_for("workspaces.home"))


@bp.route("/login/code", methods=["GET", "POST"])
def code():
    """The second step: the six-digit code from their authenticator app."""
    pending = session.get("pending_2fa")
    if not pending or time.time() - pending["at"] > 600:
        session.pop("pending_2fa", None)
        flash("Sign in again.", "info")
        return redirect(url_for("auth.login"))
    user = db.session.get(User, pending["user"])
    key = f"{client_ip()}:2fa:{pending['user']}"
    if request.method == "POST":
        if sign_ins.blocked(key):
            flash("Too many wrong codes. Wait 15 minutes and try again.", "error")
            return render_template("auth/code.html"), 429
        secret = decrypt(user.totp_secret) if user and user.totp_secret else None
        if isinstance(secret, str) and totp.verify(secret, request.form.get("code")):
            session.pop("pending_2fa", None)
            return _signed_in(user, pending.get("next") or "")
        sign_ins.hit(key)
        flash("That code isn't right. Use the newest one in your authenticator app.", "error")
        return render_template("auth/code.html"), 401
    return render_template("auth/code.html")


def _resets():
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="password-reset")


def reset_link(user):
    """A link that lets someone choose a new password: it works for 24 hours, and only until the password changes."""
    token = _resets().dumps({"user": user.id, "was": user.password_hash[-16:]})
    return f"{current_app.config['BASE_URL']}/reset/{token}"


@bp.route("/forgot")
def forgot():
    return render_template("auth/forgot.html")


@bp.route("/reset/<token>", methods=["GET", "POST"])
def reset(token):
    try:
        data = _resets().loads(token, max_age=86400)
    except SignatureExpired:
        return render_template("error.html", code=410, title="This link has expired", text="Ask for a new one."), 410
    except BadSignature:
        return render_template("error.html", code=404, title="This link isn't valid", text="Ask for a new one."), 404
    user = db.session.get(User, data.get("user"))
    if user is None or user.password_hash[-16:] != data.get("was"):
        return render_template("error.html", code=410, title="This link was already used", text="Ask for a new one if you need it."), 410
    if request.method == "POST":
        password = request.form.get("password") or ""
        if len(password) < 10:
            flash("Use at least 10 characters.", "error")
        else:
            user.set_password(password)
            db.session.commit()
            flash("Your password is changed. Sign in with it now.", "success")
            return redirect(url_for("auth.login"))
    return render_template("auth/reset.html", user=user)


@bp.route("/login/sso")
def sso_login():
    from ..connectors import oidc
    try:
        return redirect(oidc.login_url(session))
    except oidc.SSOProblem as exc:
        flash(str(exc), "error")
        return redirect(url_for("auth.login"))


@bp.route("/login/sso/callback")
def sso_callback():
    """Back from the company's identity provider (Google, Okta…): linking it to the signed-in account, accepting an
    invitation with it, or signing in with an account it's linked to. Never matched by the email it claims."""
    from ..connectors import oidc
    try:
        claims, linking = oidc.finish(session, request.args)
    except oidc.SSOProblem as exc:
        flash(str(exc), "error")
        return redirect(url_for("auth.login"))
    identity = oidc.identity(claims)
    owner = User.query.filter_by(sso_identity=identity).first()
    if linking and current_user.is_authenticated:
        if owner is not None and owner.id != current_user.id:
            flash(f"That {oidc.name()} account is already linked to another account here.", "error")
        else:
            current_user.sso_identity = identity
            db.session.commit()
            audit.log(None, "sign_in_method", f"linked {oidc.name()} sign-in ({claims.get('email') or 'their account'})")
            flash(f"You can now sign in with {oidc.name()}.", "success")
        return redirect(url_for("account.page"))
    if owner is None:
        invite = Invite.query.filter_by(token=session.get("invite") or "", accepted_at=None).first()
        if invite is None:
            flash(f"That {oidc.name()} account isn't linked to an account here yet. Sign in with your password once, then "
                  "link it from Your account; or open your invitation link first.", "error")
            return redirect(url_for("auth.login"))
        owner = User.query.filter(db.func.lower(User.email) == invite.email.lower()).first()
        if owner is None:  # the invitation (sent to this email) is the proof; the new account takes its address
            import secrets
            owner = User(email=invite.email.lower(), name=claims.get("name") or "")
            owner.set_password(secrets.token_urlsafe(32))
            db.session.add(owner)
        elif owner.sso_identity:
            flash("Sign in with your password to accept the invitation.", "error")
            return redirect(url_for("auth.login"))
        owner.sso_identity = identity
        db.session.commit()
    if owner.totp_secret:
        session["pending_2fa"] = {"user": owner.id, "at": time.time(), "next": ""}
        return redirect(url_for("auth.code"))
    return _signed_in(owner)


@bp.route("/login/microsoft")
def microsoft_login():
    from ..connectors import microsoft
    try:
        return redirect(microsoft.login_url(session))
    except microsoft.MicrosoftProblem as exc:
        flash(str(exc), "error")
        return redirect(url_for("auth.login"))


def finish_microsoft_login(args):
    """Back from Microsoft. The account is found by the Microsoft account's own identity (its tenant and object id),
    never by the email it claims, which its organisation can set to anything. That identity is linked to an account here
    either by its owner (Your account › Link Microsoft sign-in, while signed in) or when accepting an invitation."""
    from ..connectors import microsoft
    linking = session.pop("ms_link", False)
    try:
        claims = microsoft.finish_login(session, args)
    except microsoft.MicrosoftProblem as exc:
        flash(str(exc), "error")
        return redirect(url_for("account.page") if linking else url_for("auth.login"))
    identity = f"{claims.get('tid', '')}:{claims.get('oid', '')}"
    if not claims.get("tid") or not claims.get("oid"):
        flash("Microsoft didn't say which account signed in. Try again.", "error")
        return redirect(url_for("auth.login"))
    owner = User.query.filter_by(ms_identity=identity).first()
    if linking and current_user.is_authenticated:
        if owner is not None and owner.id != current_user.id:
            flash("That Microsoft account is already linked to another account here.", "error")
        else:
            current_user.ms_identity = identity
            db.session.commit()
            flash(f"You can now sign in with Microsoft ({claims.get('preferred_username') or claims.get('email') or 'your work account'}).", "success")
        return redirect(url_for("account.page"))
    if owner is None:
        invite = Invite.query.filter_by(token=session.get("invite") or "", accepted_at=None).first()
        if invite is None:
            flash("That Microsoft account isn't linked to an account here yet. Sign in with your password once, then link it "
                  "from Your account; or open your invitation link first.", "error")
            return redirect(url_for("auth.login"))
        owner = User.query.filter(db.func.lower(User.email) == invite.email.lower()).first()
        if owner is None:  # the invitation (sent to this email) is the proof; the new account takes its address
            import secrets
            owner = User(email=invite.email.lower(), name=claims.get("name") or "")
            owner.set_password(secrets.token_urlsafe(32))
            db.session.add(owner)
        elif owner.ms_identity:
            flash("Sign in with your password to accept the invitation.", "error")
            return redirect(url_for("auth.login"))
        owner.ms_identity = identity
        db.session.commit()
    if owner.totp_secret:  # Microsoft sign-in still asks for the code, when it's on
        session["pending_2fa"] = {"user": owner.id, "at": time.time(), "next": ""}
        return redirect(url_for("auth.code"))
    return _signed_in(owner)


@bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("workspaces.home"))
    invite = Invite.query.filter_by(token=session.get("invite") or "", accepted_at=None).first()
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        name = (request.form.get("name") or "").strip()[:120]
        problem = None
        if "@" not in email or "." not in email.split("@")[-1]:
            problem = "Enter a valid email address."
        elif len(password) < 10:
            problem = "Use a password of at least 10 characters."
        elif User.query.filter_by(email=email).first():
            problem = "There's already an account with that email. Sign in instead."
        if problem:
            flash(problem, "error")
            return render_template("auth/register.html", form=request.form, invite=invite), 400
        user = User(email=email, name=name or email.split("@")[0])
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        login_user(user, remember=True)
        if invite:
            session.pop("invite", None)
            _join(invite, user)
            return redirect(landing(invite.workspace_id, user))
        return redirect(url_for("workspaces.new"))
    return render_template("auth/register.html", form={"email": invite.email if invite else ""}, invite=invite)


@bp.route("/invite/<token>")
def invite(token):
    found = Invite.query.filter_by(token=token, accepted_at=None).first()
    if found is None:
        return render_template("error.html", code=404, title="This invitation isn't valid",
                               text="It may have been used already or withdrawn. Ask for a new one."), 404
    if current_user.is_authenticated:
        _join(found, current_user)
        return redirect(landing(found.workspace_id, current_user))
    session["invite"] = token
    ws = db.session.get(Workspace, found.workspace_id)
    flash(f"You've been invited to {ws.blueprint.get('name') or ws.name}. Create an account, or sign in if you have one.", "info")
    return redirect(url_for("auth.register") if not User.query.filter_by(email=found.email).first() else url_for("auth.login"))


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    audit.log(None, "signed_out", "")
    logout_user()
    return redirect(url_for("auth.login"))
