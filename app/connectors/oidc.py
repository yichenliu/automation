"""Signing in with the business's own identity provider: Google Workspace, Okta, Auth0, Keycloak, JumpCloud or any
OpenID Connect one, set in .env (OIDC_ISSUER, OIDC_CLIENT_ID, OIDC_CLIENT_SECRET, OIDC_NAME).

The account here is found by the provider's own identity for the person (its issuer and subject), never by the email it
claims; that identity is linked to an account by its owner (Your account › Link, while signed in) or by accepting an
invitation, as with Microsoft sign-in. The code is exchanged straight with the provider over TLS, with PKCE, and the ID
token it returns is checked for its issuer, audience, nonce and expiry."""
import base64
import hashlib
import json
import secrets
import time
from urllib.parse import urlencode

import requests
from ..core.settings import settings
from ..web.http import url_for

_DISCOVERED = {}  # issuer: (when, its configuration)


class SSOProblem(Exception):
    """Signing in didn't work; the message says why, plainly."""


def configured():
    c = settings()
    return bool(c.get("OIDC_ISSUER") and c.get("OIDC_CLIENT_ID") and c.get("OIDC_CLIENT_SECRET"))


def name():
    """What the button says: "Sign in with Google", "… with Okta"."""
    issuer = str(settings().get("OIDC_ISSUER") or "")
    return settings().get("OIDC_NAME") or ("Google" if "accounts.google.com" in issuer else "your company account")


def _config():
    issuer = str(settings()["OIDC_ISSUER"]).rstrip("/")
    cached = _DISCOVERED.get(issuer)
    if cached and time.time() - cached[0] < 3600:
        return cached[1]
    try:
        found = requests.get(f"{issuer}/.well-known/openid-configuration", timeout=10).json()
    except (requests.RequestException, ValueError) as exc:
        raise SSOProblem(f"{name()} couldn't be reached to sign in ({exc}). Try again, or sign in with your password.") from exc
    if not found.get("authorization_endpoint") or not found.get("token_endpoint"):
        raise SSOProblem("The sign-in provider's settings couldn't be read: check OIDC_ISSUER in .env")
    _DISCOVERED[issuer] = (time.time(), found)
    return found


def redirect_uri():
    return f"{settings()['BASE_URL']}{url_for('auth.sso_callback')}"


def login_url(session, linking=False):
    """Where to send the person to sign in; what's needed to check their return is kept in their session."""
    if not configured():
        raise SSOProblem("Single sign-on isn't set up on this platform")
    verifier = secrets.token_urlsafe(48)
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    session["sso"] = {"state": state, "nonce": nonce, "verifier": verifier, "at": time.time(), "linking": bool(linking)}
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    query = {"response_type": "code", "client_id": settings()["OIDC_CLIENT_ID"], "redirect_uri": redirect_uri(),
             "scope": "openid email profile", "state": state, "nonce": nonce, "code_challenge": challenge,
             "code_challenge_method": "S256", "prompt": "select_account"}
    return f"{_config()['authorization_endpoint']}?{urlencode(query)}"


def finish(session, args):
    """Back from the provider: the claims of who signed in ({"iss", "sub", "email", "name", ...}) and whether this was
    linking an account. Raises SSOProblem when anything doesn't check out."""
    pending = session.pop("sso", None) or {}
    if args.get("error"):
        raise SSOProblem(f"Signing in was stopped: {args.get('error_description') or args.get('error')}")
    if not pending or args.get("state") != pending.get("state") or time.time() - pending.get("at", 0) > 600:
        raise SSOProblem("That sign-in link has expired or wasn't started here. Try again.")
    found = _config()
    try:
        answer = requests.post(found["token_endpoint"], timeout=15, data={
            "grant_type": "authorization_code", "code": args.get("code", ""), "redirect_uri": redirect_uri(),
            "client_id": settings()["OIDC_CLIENT_ID"], "client_secret": settings()["OIDC_CLIENT_SECRET"],
            "code_verifier": pending["verifier"]}).json()
    except (requests.RequestException, ValueError) as exc:
        raise SSOProblem(f"{name()} didn't answer ({exc}). Try again.") from exc
    claims = _id_claims(answer.get("id_token") or "")
    issuer = str(settings()["OIDC_ISSUER"]).rstrip("/")
    audience = claims.get("aud") if isinstance(claims.get("aud"), list) else [claims.get("aud")]
    if str(claims.get("iss", "")).rstrip("/") != issuer or settings()["OIDC_CLIENT_ID"] not in audience:
        raise SSOProblem("The sign-in answer wasn't for this platform. Try again.")
    if claims.get("nonce") != pending.get("nonce") or float(claims.get("exp") or 0) < time.time() - 60:
        raise SSOProblem("The sign-in answer has expired. Try again.")
    if not claims.get("sub"):
        raise SSOProblem(f"{name()} didn't say which account signed in. Try again.")
    if found.get("userinfo_endpoint") and answer.get("access_token") and not claims.get("email"):
        try:
            claims = {**requests.get(found["userinfo_endpoint"], timeout=10,
                                     headers={"Authorization": f"Bearer {answer['access_token']}"}).json(), **claims}
        except (requests.RequestException, ValueError):
            pass
    return claims, pending.get("linking", False)


def identity(claims):
    return f"{str(claims.get('iss', '')).rstrip('/')}|{claims.get('sub')}"[:200]


def _id_claims(token):
    """The ID token's claims. It came straight from the provider's token endpoint over TLS, so its signature needn't be
    checked again (OpenID Connect Core 3.1.3.7); its issuer, audience, nonce and expiry are."""
    try:
        payload = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError) as exc:
        raise SSOProblem("The sign-in answer couldn't be read. Try again.") from exc
