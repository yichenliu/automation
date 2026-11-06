"""An ERP you build by chatting: describe your business (or let the platform read your Microsoft 365), and the AI shapes
tables, screens, documents and automations that a fixed engine runs. See README.md.

create_app() makes the platform app (app.webkit.App): an ASGI app serving the pages, the v1 API, MCP and the v2 API
(FastAPI, app/api), and the context workers and scripts run in (`with app.app_context():`)."""
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


from .config import Config, _secret_key
from .extensions import csrf, db, login_manager
from .webkit import App, render_template, request


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


def _old_layer_hooks(app):
    """What the old web kit's pages are finished with (transition: the native pages' PageRoute does the same, and these
    go with app/webkit): the time each took (Settings › Health), the security headers, and the errors' pages."""
    import time

    from . import health, security
    from .web.http import error_words
    from .webkit import g

    @app.before_request
    def started():
        g.started_at = time.perf_counter()

    @app.after_request
    def finished(response):
        health.record(request.url_rule.rule if request.url_rule is not None else None, response.status_code, g.get("started_at"))
        return response

    @app.after_request
    def headers(response):
        for key, value in security.page_headers(request.path, response.mimetype):
            response.headers.setdefault(key, value)
        return response

    def page(code):
        def handler(error):
            if code == 500:
                app.logger.error("Error on %s: %s", request.path, error)
            return render_template("error.html", **error_words(code, getattr(error, "description", None))), code
        return handler

    for code in (403, 404, 413, 500):
        app.register_error_handler(code, page(code))


def create_app(overrides=None):
    app = App(__name__)
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
    login_manager.init_app(app)
    csrf.init_app(app)

    from . import security
    from .models import User

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    # the pages the old web kit still serves, one a line (the native ones, app/web/http.py, are served by the FastAPI
    # front: app/api; docs/WEB.md says how a module moves from here to there)
    from .web import filters
    from .web import erp  # noqa: F401 (with erp_ai, erp_flows, erp_learn, erp_pages, erp_records, erp_studio on its bp)
    from .web import mcp  # noqa: F401 (adds to api.bp)
    from .web import api, builder, connect, connect_apps, forms, portal, settings
    for blueprint in (
        builder.bp,
        erp.bp,
        settings.bp,
        forms.bp,
        connect.bp,
        connect_apps.bp,
        api.bp,
        api.hooks,
        portal.bp,
        portal.join,
    ):
        app.register_blueprint(blueprint)
    filters.register(app)
    _old_layer_hooks(app)

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
    app.asgi = front(app)  # the v2 API (FastAPI) and the native pages in front, the old web kit's pages behind them
    if not app.config.get("TESTING") and not app.debug:  # every page's layout read now, with the code it goes with: a layout
        for name in app.jinja_env.list_templates():     # changed on disk while it runs waits for the next start, never mixes
            if name.endswith(".html"):
                app.jinja_env.get_template(name)

    if app.config["WORKER"] and not app.config.get("TESTING") and not app.config.get("SIMULATION"):
        from . import worker
        worker.start(app)
    return app
