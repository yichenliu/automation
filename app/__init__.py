"""An ERP you build by chatting: describe your business (or let the platform read your Microsoft 365), and the AI shapes
tables, screens, documents and automations that a fixed engine runs. See README.md.

create_app() makes the platform (app/platform.py): its config, database and templates, the context workers and scripts
run in (`with app.app_context():`), and `app.asgi`, the FastAPI app serving the pages, the v1 and v2 APIs and MCP
(app/api)."""
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import Config, _secret_key
from .extensions import db
from .platform import Platform


def _logging(app):
    folder = Path(app.config["DATA_DIR"]) / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(folder / "app.log", maxBytes=5_000_000, backupCount=5)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.setLevel(logging.INFO)
    root = logging.getLogger()
    if not any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        root.addHandler(handler)
    root.setLevel(logging.INFO)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)  # not a line for every request


def create_app(overrides=None):
    app = Platform()
    app.config.from_object(Config)
    if overrides:
        app.config.update(overrides)
    Path(app.config["DATA_DIR"]).mkdir(parents=True, exist_ok=True)
    if not app.config.get("SECRET_KEY"):  # the defaults include SECRET_KEY = None
        app.config["SECRET_KEY"] = _secret_key(app.config["DATA_DIR"])
    if not app.config.get("TESTING"):
        _logging(app)
    if not app.config.get("SQLALCHEMY_DATABASE_URI"):
        raise RuntimeError("There's no database set: run ./start.sh setup (it makes the platform's PostgreSQL and puts DATABASE_URL in .env)")
    db.init_app(app)
    from . import models  # noqa: F401 (every table, before create_all)
    from .web import filters
    filters.register(app)

    with app.app_context():
        if db.engine.dialect.name != "postgresql" and not app.config.get("SIMULATION"):
            raise RuntimeError("The platform runs on PostgreSQL only: run ./start.sh setup (it sets DATABASE_URL in .env)")
        if app.config.get("SIMULATION"):  # the simulator (app/sim): the same engine on an in-memory database
            from .sim import store
            store.create(db)
        else:
            from . import migrate
            with migrate.one_at_a_time():  # the web and the worker may start together
                new = migrate.is_new()
                db.create_all()
                migrate.upgrade(new)
        from . import discovery, importer, intelligence, knowledge, learning  # noqa: F401 (register their background jobs)

    from .api import front
    app.asgi = front(app)  # FastAPI: the pages (app/web), the v1 and v2 APIs and MCP
    if not app.config.get("TESTING") and not app.debug:  # every page's layout read now, with the code it goes with: a layout
        for name in app.jinja_env.list_templates():     # changed on disk while it runs waits for the next start, never mixes
            if name.endswith(".html"):
                app.jinja_env.get_template(name)

    if app.config["WORKER"] and not app.config.get("TESTING") and not app.config.get("SIMULATION"):
        from . import worker
        worker.start(app)
    return app
