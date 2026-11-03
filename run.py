"""Start the platform: `python run.py` (or ./start.sh). One process serves the web app (with waitress, a production
server) and runs the background worker threads (AI turns, jobs, automations, schedules, Microsoft 365, backups)."""
import logging

from waitress import serve

from app import create_app

app = create_app()

if __name__ == "__main__":
    port = app.config["PORT"]
    logging.getLogger("waitress").setLevel(logging.WARNING)
    print(f"{app.config['PLATFORM_NAME']} is running: open {app.config['BASE_URL']}", flush=True)
    serve(app, host="127.0.0.1", port=port, threads=16, channel_timeout=120, ident=app.config["PLATFORM_NAME"])
