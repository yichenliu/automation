"""Start the platform's web: `python run.py` (or ./start.sh). It serves the web app with waitress, a production server.
With WORKER=1 (the default, for one process while developing) the background work runs inside it as threads (AI turns,
jobs, rules, mail, schedules, Microsoft 365, backups); ./start.sh runs it with WORKER=0 and the worker as a process of its
own beside it (python -m app.worker), and more of either can run at once. Its database is PostgreSQL, started first when
it's the platform's own (data/postgres)."""
import logging

from waitress import serve

from app import create_app, database

if (database.folder() / "PG_VERSION").exists():  # the platform's own PostgreSQL (started at login too, before the app)
    database.start()
app = create_app()

if __name__ == "__main__":
    port = app.config["PORT"]
    logging.getLogger("waitress").setLevel(logging.WARNING)
    print(f"{app.config['PLATFORM_NAME']} is running: open {app.config['BASE_URL']}"
          + ("" if app.config["WORKER"] else " (its background work is done by the worker: python -m app.worker)"), flush=True)
    serve(app, host="127.0.0.1", port=port, threads=16, channel_timeout=120, ident=app.config["PLATFORM_NAME"])
