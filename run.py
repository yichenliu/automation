"""Start the platform's web: `python run.py` (or ./start.sh). It serves the platform app with uvicorn, a production
server: the pages, the v1 API, MCP and the v2 API, in one process (`uvicorn asgi:app --workers 4` for several).
With WORKER=1 (the default, for one process while developing) the background work runs inside it as threads (AI turns,
jobs, rules, mail, schedules, Microsoft 365, backups); ./start.sh runs it with WORKER=0 and the worker as a process of its
own beside it (python -m app.worker), and more of either can run at once. Its database is PostgreSQL, started first when
it's the platform's own (data/postgres)."""
import uvicorn

from app import create_app, database

if (database.folder() / "PG_VERSION").exists():  # the platform's own PostgreSQL (started at login too, before the app)
    database.start()
app = create_app()

if __name__ == "__main__":
    port = app.config["PORT"]
    print(f"{app.config['PLATFORM_NAME']} is running: open {app.config['BASE_URL']}"
          + ("" if app.config["WORKER"] else " (its background work is done by the worker: python -m app.worker)"), flush=True)
    # proxy_headers off: a request's address is the connection's, and X-Forwarded-For is believed only with TRUST_PROXY=1
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning", access_log=False, timeout_keep_alive=120, proxy_headers=False,
                server_header=False, headers=[("Server", app.config["PLATFORM_NAME"])])
