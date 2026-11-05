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

    from .web import account, api, auth, builder, connect, connect_apps, erp, filters, forms, mcp, portal, settings, workspaces  # noqa: F401 (mcp adds to api.bp)
    for module in (auth, workspaces, builder, erp, settings, forms, connect, connect_apps, api, portal, account):
        app.register_blueprint(module.bp)
    app.register_blueprint(api.hooks)
    app.register_blueprint(portal.join)
    filters.register(app)
    from . import health
    app.before_request(health.started)
    app.after_request(health.finished)
    app.after_request(security.headers)
    if not app.config.get("TESTING") and not app.debug:  # every page's layout read now, with the code it goes with: a layout
        for name in app.jinja_env.list_templates():     # changed on disk while it runs waits for the next start, never mixes
            if name.endswith(".html"):
                app.jinja_env.get_template(name)

    @app.errorhandler(403)
    def forbidden(error):
        said = getattr(error, "description", "") or ""
        why = said if said and not said.startswith("You don't have the permission") else "Ask the person who runs your app if you need it."
        return render_template("error.html", code=403, title="This isn't open to you", text=why), 403

    @app.errorhandler(404)
    def not_found(_):
        return render_template("error.html", code=404, title="We can't find this page", text="It may have been moved or deleted, or it may not be open to you."), 404

    @app.errorhandler(413)
    def too_large(_):
        return render_template("error.html", code=413, title="That file is too large",
                               text=f"Files can be up to {app.config['MAX_UPLOAD_MB']} MB."), 413

    @app.errorhandler(500)
    def broken(exc):
        app.logger.error("Error on %s: %s", request.path, exc)
        return render_template("error.html", code=500, title="Sorry, something went wrong",
                               text="It's not your fault, and nothing you entered was lost. Go back and try again."), 500

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
    app.asgi = front(app)  # the v2 API (FastAPI) in front, the pages behind it

    if app.config["WORKER"] and not app.config.get("TESTING") and not app.config.get("SIMULATION"):
        from . import worker
        worker.start(app)
    return app
