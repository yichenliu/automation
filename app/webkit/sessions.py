"""The browser's session: a small dict kept in a cookie the browser can read but can't change (it's signed with the
app's secret key). It holds who is signed in, the two-step sign-in in progress, the CSRF secret and the messages
waiting to be shown on the next page.

The cookie is the native pages' own (app/web/http.py: the same signer, salt and format the platform has always used),
so a session is one session whichever layer serves the page. It's sent back only when the session was changed."""
from ..web.http import Session, dump_session, load_session, save_session, session_cookie_name

__all__ = ["Session", "SessionInterface"]


class SessionInterface:
    salt = "cookie-session"

    def load(self, app, value):
        """The session in a cookie's value (an empty one if it's missing, tampered with or too old)."""
        return load_session(app.config, value)

    def dump(self, app, session):
        return dump_session(app.config, session)

    def open_session(self, app, request):
        return self.load(app, request.cookies.get(self.cookie_name(app)))

    def cookie_name(self, app):
        return session_cookie_name(app.config)

    def save_session(self, app, session, response):
        save_session(app.config, session, response)
