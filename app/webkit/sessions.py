"""The browser's session: a small dict kept in a cookie the browser can read but can't change (it's signed with the
app's secret key). It holds who is signed in, the two-step sign-in in progress, the CSRF secret and the messages
waiting to be shown on the next page.

The cookie is written the way it always has been (signed with itsdangerous, the same salt and signer), so people
signed in before the platform moved to this kit stay signed in. It's sent back only when the session was changed."""
import hashlib
import json
from datetime import timedelta

from itsdangerous import BadSignature, URLSafeTimedSerializer
from markupsafe import Markup


class _JSON:
    """Plain JSON, compact; a tuple, Markup or bytes written by the old cookie format reads back as it was."""

    @staticmethod
    def dumps(value):
        return json.dumps(value, separators=(",", ":"), default=_tagged)

    @staticmethod
    def loads(value):
        return json.loads(value, object_hook=_untag)


def _tagged(value):
    if isinstance(value, bytes):
        import base64
        return {" b": base64.b64encode(value).decode("ascii")}
    raise TypeError(f"Object of type {type(value).__name__} can't be kept in the session")


def _untag(obj):
    if len(obj) == 1:
        key, value = next(iter(obj.items()))
        if key == " t":
            return tuple(value)
        if key == " m":
            return Markup(value)
        if key == " b":
            import base64
            return base64.b64decode(value)
        if key == " di":
            return value
    return obj


class Session(dict):
    """The session's values. Setting, removing or adding one marks it changed, so the cookie is written again; reading
    it marks it read, so caches are told the page depends on the cookie."""

    def __init__(self, initial=None):
        super().__init__(initial or {})
        self.modified = False
        self.accessed = False
        self.new = initial is None
        self.permanent = bool(self.get("_permanent"))

    def _changed(self):
        self.modified = True
        self.accessed = True

    def __getitem__(self, key):
        self.accessed = True
        return super().__getitem__(key)

    def get(self, key, default=None):
        self.accessed = True
        return super().get(key, default)

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self._changed()

    def __delitem__(self, key):
        super().__delitem__(key)
        self._changed()

    def setdefault(self, key, default=None):
        self.accessed = True
        if key not in self:
            super().__setitem__(key, default)
            self._changed()
        return super().__getitem__(key)

    def pop(self, key, *default):
        self.accessed = True
        if key in self:
            self._changed()
        return super().pop(key, *default)

    def popitem(self):
        self._changed()
        return super().popitem()

    def update(self, *args, **kwargs):
        super().update(*args, **kwargs)
        self._changed()

    def clear(self):
        super().clear()
        self._changed()


class SessionInterface:
    salt = "cookie-session"

    def serializer(self, app):
        secret = app.config.get("SECRET_KEY")
        if not secret:
            return None
        return URLSafeTimedSerializer(secret, salt=self.salt, serializer=_JSON,
                                      signer_kwargs={"key_derivation": "hmac", "digest_method": hashlib.sha1})

    def lifetime(self, app):
        value = app.config.get("PERMANENT_SESSION_LIFETIME") or timedelta(days=31)
        return value if isinstance(value, timedelta) else timedelta(seconds=int(value))

    def load(self, app, value):
        """The session in a cookie's value (an empty one if it's missing, tampered with or too old)."""
        signer = self.serializer(app)
        if signer is None or not value:
            return Session()
        try:
            return Session(signer.loads(value, max_age=int(self.lifetime(app).total_seconds())))
        except (BadSignature, ValueError, TypeError):
            return Session()

    def dump(self, app, session):
        return self.serializer(app).dumps(dict(session))

    def open_session(self, app, request):
        return self.load(app, request.cookies.get(self.cookie_name(app)))

    def cookie_name(self, app):
        return app.config.get("SESSION_COOKIE_NAME") or "session"

    def save_session(self, app, session, response):
        name = self.cookie_name(app)
        domain = app.config.get("SESSION_COOKIE_DOMAIN") or None
        path = app.config.get("SESSION_COOKIE_PATH") or app.config.get("APPLICATION_ROOT") or "/"
        secure = bool(app.config.get("SESSION_COOKIE_SECURE"))
        httponly = bool(app.config.get("SESSION_COOKIE_HTTPONLY", True))
        samesite = app.config.get("SESSION_COOKIE_SAMESITE")
        if session.accessed:
            _vary(response, "Cookie")
        if not session:
            if session.modified:
                response.delete_cookie(name, domain=domain, path=path, secure=secure, samesite=samesite, httponly=httponly)
                _vary(response, "Cookie")
            return
        if not (session.modified or (session.permanent and app.config.get("SESSION_REFRESH_EACH_REQUEST", True))):
            return
        if self.serializer(app) is None:
            raise RuntimeError("The session can't be kept without a SECRET_KEY")
        from datetime import datetime, timezone
        expires = datetime.now(timezone.utc) + self.lifetime(app) if session.permanent else None
        response.set_cookie(name, self.dump(app, session), expires=expires, httponly=httponly, domain=domain, path=path,
                            secure=secure, samesite=samesite)
        _vary(response, "Cookie")


def _vary(response, value):
    found = [v.strip() for v in (response.headers.get("Vary") or "").split(",") if v.strip()]
    if value.lower() not in {v.lower() for v in found}:
        response.headers["Vary"] = ", ".join(found + [value])
