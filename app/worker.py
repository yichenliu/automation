"""The background worker: threads in the web process.

- builder: the builder chat's turns, one at a time (as a local model works);
- assistant: questions to the in-ERP assistant, so they never queue behind a long design turn;
- jobs: imports, reading a business's Microsoft 365, AI-filled fields, reading documents;
- automations: runs waiting for the AI or an approval, schedules, watching Microsoft 365, and the daily backup;
- knowledge: reading changed records, new documents and mail into what the AI can look up.
Everything is in the database, so a restart picks up where it left off.
"""
import logging
import threading
import time

from sqlalchemy import and_, or_

from .extensions import db
from .models import Job, Message, Run, Workspace, utcnow

log = logging.getLogger("worker")
_working = {}  # what each thread is busy with right now: {"builder": message id, "assistant": message id, "jobs": job id}
GIVE_UP_AFTER = 1800  # seconds: a turn or question that keeps stopping part-way is given up on, and says so
SCHEDULE_EVERY = 30  # seconds between checks for due schedules
WATCH_EVERY = 120  # seconds between looks at Microsoft 365 for new mail and files
_last = {"schedule": 0.0, "watch": 0.0, "backup": 0.0}


def recover():
    """After a restart, work that was mid-way starts again."""
    Message.query.filter(Message.status == "thinking").update({"status": "queued"})
    Run.query.filter(Run.status == "running").update({"status": "queued"})
    Job.query.filter(Job.status == "running").update({"status": "queued"})
    db.session.commit()
    from . import jobs
    for workspace in Workspace.query.all():  # workspaces from before the AI could look things up: read them once
        if not (workspace.settings or {}).get("knowledge_built"):
            jobs.enqueue("knowledge_rebuild", workspace_id=workspace.id, dedupe=f"knowledge_rebuild:{workspace.id}")
            workspace.settings = {**(workspace.settings or {}), "knowledge_built": True}
    db.session.commit()


def unstick():
    """Work a failure left half done (the database busy too long, a fault of ours) goes back in the queue, as it does after
    a restart, instead of showing "working…" for ever; work that keeps stopping part-way is given up on and says so."""
    busy = set(_working.values())
    for message in Message.query.filter(Message.status == "thinking").all():
        if message.id in busy:
            continue
        if message.started_at and (utcnow() - message.started_at).total_seconds() > GIVE_UP_AFTER:
            message.status, message.finished_at = "failed", utcnow()
            message.error = "This stopped part-way through and couldn't be finished. Please ask again."
        else:
            message.status, message.next_try = "queued", None
    for job in Job.query.filter(Job.status == "running").all():
        if ("job", job.id) in busy:
            continue
        if (job.attempts or 0) >= 3:
            job.status, job.finished_at = "failed", utcnow()
            job.error = "This stopped part-way through three times and was given up on."
        else:
            job.status = "queued"
    db.session.commit()


def _busy(kind, value, fn, *args):
    _working[kind] = value
    try:
        return fn(*args)
    finally:
        _working.pop(kind, None)


def _next_message(channel):
    return (Message.query.filter(Message.channel == channel, Message.role == "assistant", or_(
        Message.status == "queued", and_(Message.status == "waiting_ai", Message.next_try <= utcnow())))
        .order_by(Message.id).first())


def builder_tick():
    from .builder import chat
    message = _next_message("builder")
    if message is None:
        return False
    _busy("builder", message.id, chat.work, message.id)
    return True


def assistant_tick():
    from . import assistant
    message = _next_message("assistant")
    if message is None:
        return False
    _busy("assistant", message.id, assistant.work, message.id)
    return True


def jobs_tick():
    from . import discovery, importer, intelligence, jobs  # noqa: F401 (registers the job handlers)
    job = jobs.next_job()
    if job is None:
        return False
    _busy("jobs", ("job", job.id), jobs.run, job)
    return True


def knowledge_tick():
    from . import knowledge
    return knowledge.work()


def automation_tick(app):
    from . import backup
    from .connectors import watch
    from .engine import automations
    from .engine.records import Engine
    runs = (Run.query.filter(or_(Run.status == "queued", and_(Run.status.in_(["waiting_ai", "waiting", "waiting_approval"]), Run.next_try <= utcnow())))
            .order_by(Run.id).limit(5).all())
    for run in runs:
        workspace = db.session.get(Workspace, run.workspace_id)
        automations.execute(Engine(workspace, by="automation"), run)
    now = time.monotonic()
    if now - _last.get("unstick", 0) > 60:
        _last["unstick"] = now
        unstick()
    if now - _last["schedule"] > SCHEDULE_EVERY:
        _last["schedule"] = now
        for workspace in Workspace.query.all():
            if any(a["trigger"]["type"] == "schedule" for a in (workspace.blueprint or {}).get("automations", [])):
                automations.due(Engine(workspace, by="schedule"))
    if now - _last.get("mail", 0) > 60:
        _last["mail"] = now
        automations.email_notifications()
    if now - _last["watch"] > WATCH_EVERY:
        _last["watch"] = now
        for workspace in Workspace.query.all():
            try:
                watch.check(workspace)
            except Exception as exc:  # one workspace's mailbox trouble doesn't stop the others
                log.warning("Watching Microsoft 365 for workspace %s failed: %s", workspace.id, exc)
                db.session.rollback()
    if now - _last.get("learn", 0) > 3600:  # once a day, the analyst looks at how each ERP is being used
        _last["learn"] = now
        from . import jobs
        today = utcnow().strftime("%Y-%m-%d")
        for workspace in Workspace.query.all():
            key = f"learn:{workspace.id}:{today}"
            if (workspace.blueprint or {}).get("tables") and not Job.query.filter_by(dedupe=key).first():
                jobs.enqueue("learn", {"ai": True}, workspace_id=workspace.id, dedupe=key)
    if now - _last["backup"] > 3600:
        _last["backup"] = now
        latest = backup.latest()
        if latest is None or latest.stat().st_mtime < time.time() - 86400:
            path = backup.make()
            log.info("Backup made: %s", path)
    return bool(runs)


def _loop(app, tick, pause, name):
    while True:
        busy = False
        try:
            with app.app_context():
                try:
                    busy = tick(app) if tick is automation_tick else tick()
                finally:
                    db.session.remove()
        except Exception:
            log.exception("Background work (%s) failed", name)
            time.sleep(5)
        if not busy:
            time.sleep(pause)


def start(app):
    if app.extensions.get("worker_started"):
        return
    app.extensions["worker_started"] = True
    with app.app_context():
        recover()
        try:  # the platform may have been updated since each app's checks last ran: run them again in the background
            from .builder import checking
            checking.after_platform_update()
        except Exception:
            log.exception("Couldn't queue the apps' checks after an update")
    for name, tick, pause in (("builder", builder_tick, 1.0), ("assistant", assistant_tick, 0.7), ("jobs", jobs_tick, 1.0),
                              ("automations", automation_tick, 2.0), ("knowledge", knowledge_tick, 3.0)):
        threading.Thread(target=_loop, args=(app, tick, pause, name), daemon=True, name=name).start()
