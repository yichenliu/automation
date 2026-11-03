"""An ERP you build by chatting: describe your business (or let the platform read your Microsoft 365), and the AI shapes
tables, screens, documents and automations that a fixed engine runs. See README.md."""
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from flask import Flask, render_template, request
from sqlalchemy import event

from .config import Config, _secret_key
from .extensions import csrf, db, login_manager


def _sqlite_pragmas(connection, _):
    cursor = connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")  # the web app and the background worker read and write side by side
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


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
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


def create_app(overrides=None):
    app = Flask(__name__)
    app.config.from_object(Config)
    if overrides:
        app.config.update(overrides)
    Path(app.config["DATA_DIR"]).mkdir(parents=True, exist_ok=True)
    if not app.config.get("SECRET_KEY"):  # Flask's defaults include SECRET_KEY = None
        app.config["SECRET_KEY"] = _secret_key(app.config["DATA_DIR"])
    if not app.config.get("TESTING"):
        _logging(app)
    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)

    from . import security
    from .models import User

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    from .web import account, api, auth, builder, connect, erp, filters, forms, mcp, portal, settings, workspaces  # noqa: F401 (mcp adds to api.bp)
    for module in (auth, workspaces, builder, erp, settings, forms, connect, api, portal, account):
        app.register_blueprint(module.bp)
    app.register_blueprint(api.hooks)
    app.register_blueprint(portal.join)
    filters.register(app)
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
        if db.engine.dialect.name == "sqlite":
            event.listen(db.engine, "connect", _sqlite_pragmas)
            db.engine.dispose()
        from . import migrate
        new = migrate.is_new()
        db.create_all()
        migrate.upgrade(new)
        from . import discovery, importer, intelligence, knowledge, learning  # noqa: F401 (register their background jobs)

    if app.config["WORKER"] and not app.config.get("TESTING"):
        from . import worker
        worker.start(app)
    return app
