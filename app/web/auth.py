"""Signing in (with a password, a two-step code when it's turned on, or Microsoft), creating an account, joining a
workspace from an invitation, and resetting a forgotten password with a link."""
import time

from fastapi import APIRouter, Depends, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .. import audit, totp
from ..core.context import current_app
from ..extensions import db
from ..models import Invite, Membership, User, Workspace, utcnow
from ..security import client_ip, decrypt, sign_ins
from .http import (Form, PageRoute, check_csrf, current_user, flash, form_of, login_user, logout_user, redirect, render, signed_in,
                   url_for)

router = APIRouter(route_class=PageRoute, dependencies=[Depends(check_csrf)])


def _safe_next(request, form, default):
    target = request.query_params.get("next") or form.get("next") or ""
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


@router.api_route("/login", methods=["GET", "POST"], name="auth.login")
def login(request: Request, form: Form = Depends(form_of)):
    if current_user(request).is_authenticated:
        return redirect(_safe_next(request, form, url_for("workspaces.home")))
    if request.method == "POST":
        email = (form.get("email") or "").strip().lower()
        key = f"{client_ip(request)}:{email}"
        if sign_ins.blocked(key):  # only wrong passwords count
            flash(request, "Too many wrong passwords. Wait 15 minutes and try again.", "error")
            return render(request, "auth/login.html", status_code=429, email=email)
        user = User.query.filter_by(email=email).first()
        if user and user.check_password(form.get("password") or ""):
            if user.totp_secret:  # the password was right; now the code from their authenticator app
                request.state.session["pending_2fa"] = {"user": user.id, "at": time.time(), "next": _safe_next(request, form, "")}
                return redirect(url_for("auth.code"))
            return _signed_in(request, user, form=form)
        sign_ins.hit(key)
        audit.log(None, "sign_in_failed", f"a wrong password for {email}", user=user, by=email or "someone")
        flash(request, "That email and password don't match.", "error")
        return render(request, "auth/login.html", status_code=401, email=email)
    return render(request, "auth/login.html", email="")


def _signed_in(request, user, next_url="", form=None):
    login_user(request, user, remember=True)
    audit.log(None, "signed_in", "", user=user)
    user.last_seen_at = utcnow()
    db.session.commit()
    invite = Invite.query.filter_by(token=request.state.session.pop("invite", None) or "", accepted_at=None).first()
    if invite:
        _join(invite, user)
        return redirect(landing(invite.workspace_id, user))
    target = next_url or _safe_next(request, form if form is not None else Form(), "")
    return redirect(target if target.startswith("/") and not target.startswith("//") else url_for("workspaces.home"))


@router.api_route("/login/code", methods=["GET", "POST"], name="auth.code")
def code(request: Request, form: Form = Depends(form_of)):
    """The second step: the six-digit code from their authenticator app."""
    session = request.state.session
    pending = session.get("pending_2fa")
    if not pending or time.time() - pending["at"] > 600:
        session.pop("pending_2fa", None)
        flash(request, "Sign in again.", "info")
        return redirect(url_for("auth.login"))
    user = db.session.get(User, pending["user"])
    key = f"{client_ip(request)}:2fa:{pending['user']}"
    if request.method == "POST":
        if sign_ins.blocked(key):
            flash(request, "Too many wrong codes. Wait 15 minutes and try again.", "error")
            return render(request, "auth/code.html", status_code=429)
        secret = decrypt(user.totp_secret) if user and user.totp_secret else None
        if isinstance(secret, str) and totp.verify(secret, form.get("code")):
            session.pop("pending_2fa", None)
            return _signed_in(request, user, pending.get("next") or "", form)
        sign_ins.hit(key)
        flash(request, "That code isn't right. Use the newest one in your authenticator app.", "error")
        return render(request, "auth/code.html", status_code=401)
    return render(request, "auth/code.html")


def _resets():
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="password-reset")


def reset_link(user):
    """A link that lets someone choose a new password: it works for 24 hours, and only until the password changes."""
    token = _resets().dumps({"user": user.id, "was": user.password_hash[-16:]})
    return f"{current_app.config['BASE_URL']}/reset/{token}"


@router.api_route("/forgot", methods=["GET"], name="auth.forgot")
def forgot(request: Request):
    return render(request, "auth/forgot.html")


@router.api_route("/reset/{token}", methods=["GET", "POST"], name="auth.reset")
def reset(request: Request, token: str, form: Form = Depends(form_of)):
    try:
        data = _resets().loads(token, max_age=86400)
    except SignatureExpired:
        return render(request, "error.html", status_code=410, code=410, title="This link has expired", text="Ask for a new one.")
    except BadSignature:
        return render(request, "error.html", status_code=404, code=404, title="This link isn't valid", text="Ask for a new one.")
    user = db.session.get(User, data.get("user"))
    if user is None or user.password_hash[-16:] != data.get("was"):
        return render(request, "error.html", status_code=410, code=410, title="This link was already used", text="Ask for a new one if you need it.")
    if request.method == "POST":
        password = form.get("password") or ""
        if len(password) < 10:
            flash(request, "Use at least 10 characters.", "error")
        else:
            user.set_password(password)
            db.session.commit()
            flash(request, "Your password is changed. Sign in with it now.", "success")
            return redirect(url_for("auth.login"))
    return render(request, "auth/reset.html", user=user)


@router.api_route("/login/sso", methods=["GET"], name="auth.sso_login")
def sso_login(request: Request):
    from ..connectors import oidc
    try:
        return redirect(oidc.login_url(request.state.session))
    except oidc.SSOProblem as exc:
        flash(request, str(exc), "error")
        return redirect(url_for("auth.login"))


@router.api_route("/login/sso/callback", methods=["GET"], name="auth.sso_callback")
def sso_callback(request: Request):
    """Back from the company's identity provider (Google, Okta…): linking it to the signed-in account, accepting an
    invitation with it, or signing in with an account it's linked to. Never matched by the email it claims."""
    from ..connectors import oidc
    session, who = request.state.session, current_user(request)
    try:
        claims, linking = oidc.finish(session, request.query_params)
    except oidc.SSOProblem as exc:
        flash(request, str(exc), "error")
        return redirect(url_for("auth.login"))
    identity = oidc.identity(claims)
    owner = User.query.filter_by(sso_identity=identity).first()
    if linking and who.is_authenticated:
        if owner is not None and owner.id != who.id:
            flash(request, f"That {oidc.name()} account is already linked to another account here.", "error")
        else:
            who.sso_identity = identity
            db.session.commit()
            audit.log(None, "sign_in_method", f"linked {oidc.name()} sign-in ({claims.get('email') or 'their account'})")
            flash(request, f"You can now sign in with {oidc.name()}.", "success")
        return redirect(url_for("account.page"))
    if owner is None:
        invite = Invite.query.filter_by(token=session.get("invite") or "", accepted_at=None).first()
        if invite is None:
            flash(request, f"That {oidc.name()} account isn't linked to an account here yet. Sign in with your password once, then "
                  "link it from Your account; or open your invitation link first.", "error")
            return redirect(url_for("auth.login"))
        owner = User.query.filter(db.func.lower(User.email) == invite.email.lower()).first()
        if owner is None:  # the invitation (sent to this email) is the proof; the new account takes its address
            import secrets
            owner = User(email=invite.email.lower(), name=claims.get("name") or "")
            owner.set_password(secrets.token_urlsafe(32))
            db.session.add(owner)
        elif owner.sso_identity:
            flash(request, "Sign in with your password to accept the invitation.", "error")
            return redirect(url_for("auth.login"))
        owner.sso_identity = identity
        db.session.commit()
    if owner.totp_secret:
        session["pending_2fa"] = {"user": owner.id, "at": time.time(), "next": ""}
        return redirect(url_for("auth.code"))
    return _signed_in(request, owner)


@router.api_route("/login/microsoft", methods=["GET"], name="auth.microsoft_login")
def microsoft_login(request: Request):
    from ..connectors import microsoft
    try:
        return redirect(microsoft.login_url(request.state.session))
    except microsoft.MicrosoftProblem as exc:
        flash(request, str(exc), "error")
        return redirect(url_for("auth.login"))


def finish_microsoft_login(request, args):
    """Back from Microsoft (connect.callback hands it on). The account is found by the Microsoft account's own identity
    (its tenant and object id), never by the email it claims, which its organisation can set to anything. That identity
    is linked to an account here either by its owner (Your account › Link Microsoft sign-in, while signed in) or when
    accepting an invitation."""
    from ..connectors import microsoft
    session, who = request.state.session, current_user(request)
    linking = session.pop("ms_link", False)
    try:
        claims = microsoft.finish_login(session, args)
    except microsoft.MicrosoftProblem as exc:
        flash(request, str(exc), "error")
        return redirect(url_for("account.page") if linking else url_for("auth.login"))
    identity = f"{claims.get('tid', '')}:{claims.get('oid', '')}"
    if not claims.get("tid") or not claims.get("oid"):
        flash(request, "Microsoft didn't say which account signed in. Try again.", "error")
        return redirect(url_for("auth.login"))
    owner = User.query.filter_by(ms_identity=identity).first()
    if linking and who.is_authenticated:
        if owner is not None and owner.id != who.id:
            flash(request, "That Microsoft account is already linked to another account here.", "error")
        else:
            who.ms_identity = identity
            db.session.commit()
            flash(request, f"You can now sign in with Microsoft ({claims.get('preferred_username') or claims.get('email') or 'your work account'}).", "success")
        return redirect(url_for("account.page"))
    if owner is None:
        invite = Invite.query.filter_by(token=session.get("invite") or "", accepted_at=None).first()
        if invite is None:
            flash(request, "That Microsoft account isn't linked to an account here yet. Sign in with your password once, then link it "
                  "from Your account; or open your invitation link first.", "error")
            return redirect(url_for("auth.login"))
        owner = User.query.filter(db.func.lower(User.email) == invite.email.lower()).first()
        if owner is None:  # the invitation (sent to this email) is the proof; the new account takes its address
            import secrets
            owner = User(email=invite.email.lower(), name=claims.get("name") or "")
            owner.set_password(secrets.token_urlsafe(32))
            db.session.add(owner)
        elif owner.ms_identity:
            flash(request, "Sign in with your password to accept the invitation.", "error")
            return redirect(url_for("auth.login"))
        owner.ms_identity = identity
        db.session.commit()
    if owner.totp_secret:  # Microsoft sign-in still asks for the code, when it's on
        session["pending_2fa"] = {"user": owner.id, "at": time.time(), "next": ""}
        return redirect(url_for("auth.code"))
    return _signed_in(request, owner)


@router.api_route("/register", methods=["GET", "POST"], name="auth.register")
def register(request: Request, form: Form = Depends(form_of)):
    if current_user(request).is_authenticated:
        return redirect(url_for("workspaces.home"))
    session = request.state.session
    invite = Invite.query.filter_by(token=session.get("invite") or "", accepted_at=None).first()
    if request.method == "POST":
        email = (form.get("email") or "").strip().lower()
        password = form.get("password") or ""
        name = (form.get("name") or "").strip()[:120]
        problem = None
        if "@" not in email or "." not in email.split("@")[-1]:
            problem = "Enter a valid email address."
        elif len(password) < 10:
            problem = "Use a password of at least 10 characters."
        elif User.query.filter_by(email=email).first():
            problem = "There's already an account with that email. Sign in instead."
        if problem:
            flash(request, problem, "error")
            return render(request, "auth/register.html", status_code=400, form=form, invite=invite)
        user = User(email=email, name=name or email.split("@")[0])
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        login_user(request, user, remember=True)
        if invite:
            session.pop("invite", None)
            _join(invite, user)
            return redirect(landing(invite.workspace_id, user))
        return redirect(url_for("workspaces.new"))
    return render(request, "auth/register.html", form={"email": invite.email if invite else ""}, invite=invite)


@router.api_route("/invite/{token}", methods=["GET"], name="auth.invite")
def invite(request: Request, token: str):
    found = Invite.query.filter_by(token=token, accepted_at=None).first()
    if found is None:
        return render(request, "error.html", status_code=404, code=404, title="This invitation isn't valid",
                      text="It may have been used already or withdrawn. Ask for a new one.")
    who = current_user(request)
    if who.is_authenticated:
        _join(found, who)
        return redirect(landing(found.workspace_id, who))
    request.state.session["invite"] = token
    ws = db.session.get(Workspace, found.workspace_id)
    flash(request, f"You've been invited to {ws.blueprint.get('name') or ws.name}. Create an account, or sign in if you have one.", "info")
    return redirect(url_for("auth.register") if not User.query.filter_by(email=found.email).first() else url_for("auth.login"))


@router.api_route("/logout", methods=["POST"], name="auth.logout", dependencies=[Depends(signed_in)])
def logout(request: Request):
    audit.log(None, "signed_out", "")
    logout_user(request)
    return redirect(url_for("auth.login"))
