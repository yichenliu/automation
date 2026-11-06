"""The platform: what create_app() makes, and the context everything in it runs in.

    app = create_app()
    with app.app_context():     # a worker, a script, a test: the platform's settings, its database, nobody acting yet
        ...
    app.asgi                    # the platform as an ASGI app: FastAPI serving the pages, the APIs and MCP (app/api)
    app.test_client()           # requests through app.asgi, as a browser makes them (app/web/testing.py)

It holds the config, the database engine (app/core/database.py puts it here), the log, and the Jinja environment the
pages are drawn with (app/templates, with the filters of app/web/filters.py and the pages' @template_global). Serving a
request is FastAPI's and app/web/http.py's: nothing here knows about one.
"""
import contextlib
import logging
import sys
from pathlib import Path

from .core import actor, context as core_context, settings as core_settings

DEFAULTS = {  # what app.config.Config and the overrides don't say
    "DEBUG": False, "TESTING": False, "PROPAGATE_EXCEPTIONS": None, "SECRET_KEY": None, "TEMPLATES_AUTO_RELOAD": None,
    "MAX_CONTENT_LENGTH": None, "MAX_FORM_MEMORY_SIZE": 500_000, "MAX_FORM_PARTS": 1_000, "SEND_FILE_MAX_AGE_DEFAULT": None,
}


class Config(dict):
    """The platform's settings: a dict, filled from a class's UPPERCASE attributes (app.config.Config), then overrides."""

    def from_object(self, obj):
        for key in dir(obj):
            if key.isupper():
                self[key] = getattr(obj, key)


class Context:
    """`app.app_context()`: the platform (core.context), its settings (core.settings), a database session of the
    context's own (core.database, given back when it ends), and nobody acting until a page, the API or a job says who
    (core.actor). `with app.app_context():`, or push() and pop() for a script that stays in it."""

    def __init__(self, platform):
        self.platform = platform
        self._stacks = []

    def push(self):
        stack = contextlib.ExitStack()
        stack.enter_context(core_context.entered(self.platform))
        stack.enter_context(core_settings.using(self.platform.config))
        database = self.platform.extensions.get("sqlalchemy")
        if database is not None and self.platform.engine is not None:
            stack.enter_context(database.using(self.platform.engine))
        stack.enter_context(actor.acting())
        self._stacks.append(stack)
        return self

    def pop(self):
        self._stacks.pop().close()

    def __enter__(self):
        return self.push()

    def __exit__(self, *exc):
        self.pop()


class Platform:
    """The platform app (create_app()). Called as an ASGI app it hands the call to `asgi`."""

    name = "app"

    def __init__(self, root_path=None):
        self.root_path = str(root_path or Path(__file__).resolve().parent)
        self.template_folder = str(Path(self.root_path) / "templates")
        self.static_folder = str(Path(self.root_path) / "static")
        self.config = Config(DEFAULTS)
        self.extensions = {}  # "sqlalchemy": the database; "pages": the pages by name, for url_for (app/web/http.py)
        self.engine = None  # the database's, set by db.init_app
        self.asgi = None  # the FastAPI app, set by create_app (app/api: front)
        self._jinja_env = self._logger = None

    @property
    def debug(self):
        return bool(self.config.get("DEBUG"))

    @property
    def testing(self):
        return bool(self.config.get("TESTING"))

    @property
    def secret_key(self):
        return self.config.get("SECRET_KEY")

    @property
    def logger(self):
        """The platform's log (errors on pages, with what went wrong), made the first time it's used: to the log files
        create_app set up by then (through the root logger), else to the terminal, where nothing else would show it."""
        if self._logger is None:
            logger = logging.getLogger(self.name)
            level, current = logger.getEffectiveLevel(), logger
            while current:
                if any(h.level <= level for h in current.handlers):
                    break
                if not current.propagate:
                    current = None
                    break
                current = current.parent
            if current is None:
                handler = logging.StreamHandler(sys.stderr)
                handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s in %(module)s: %(message)s"))
                logger.addHandler(handler)
            self._logger = logger
        return self._logger

    @property
    def jinja_env(self):
        """The templates (app/templates), autoescaped; reread when they change on disk only while DEBUG (or
        TEMPLATES_AUTO_RELOAD). create_app adds the filters (app/web/filters.py), app/web/http.py the pages' globals."""
        if self._jinja_env is None:
            import jinja2

            from .web.http import dumps
            reload = self.config.get("TEMPLATES_AUTO_RELOAD")
            self._jinja_env = jinja2.Environment(loader=jinja2.FileSystemLoader(self.template_folder), autoescape=_autoescape,
                                                 auto_reload=self.debug if reload is None else bool(reload))
            self._jinja_env.policies["json.dumps_function"] = dumps  # |tojson writes JSON as the pages' answers do
        return self._jinja_env

    def app_context(self):
        return Context(self)

    def test_client(self):
        from .web.testing import Client
        return Client(self)

    async def __call__(self, scope, receive, send):
        await self.asgi(scope, receive, send)


def _autoescape(name):
    return True if name is None else name.endswith((".html", ".htm", ".xml", ".xhtml", ".svg"))
