"""The background worker: its own process (`python -m app.worker`, which ./start.sh starts beside the web), or threads
inside the web process (WORKER=1, the default, for one process while developing).

- builder: the builder chat's turns, BUILDER_CONCURRENCY at once (an app's own turns one after another);
- assistant: questions to the in-ERP assistant, so they never queue behind a long design turn;
- jobs: imports, reading a business's Microsoft 365, AI-filled fields, reading documents (JOB_CONCURRENCY at once);
- automations: rule runs left to the worker (AI steps, reminders of approvals, waits), an app's runs one after another;
- outbox: the emails and text messages rules send, with tries again when the mailbox can't be reached (outbox.py);
- knowledge: reading changed records, new documents and mail into what the AI can look up (one process at a time);
- scheduler: schedules, connected apps, watching Microsoft 365, the daily look and backups (one process at a time,
  scheduler.py), and putting back work a stopped process left.
WORKER_KINDS says which of these a process does. Every item is claimed before it's worked on (leases.py), so however
many workers run, nothing is done twice; a process that stops leaves its work to the others once its claims run out.
Everything is in the database, so a restart picks up where it left off.
"""
import logging
import signal
import sys
import threading
import time
from datetime import timedelta

from sqlalchemy import and_, or_

from . import leases
from .extensions import db
from .models import Job, Message, Outbox, Run, Workspace, utcnow

log = logging.getLogger("worker")
KINDS = ("builder", "assistant", "jobs", "automations", "outbox", "knowledge", "scheduler")
PAUSE = {"builder": 1.0, "assistant": 0.7, "jobs": 1.0, "automations": 2.0, "outbox": 2.0, "knowledge": 3.0, "scheduler": 5.0}
_working = {}  # what each thread is busy with right now: {thread id: message id, ("job", id) or ("run", id)}
GIVE_UP_AFTER = 1800  # seconds: a turn or question that keeps stopping part-way is given up on, and says so
UNSTICK_EVERY = 60  # seconds between looks for work a stopped process left
RUNS_AT_ONCE = 5  # rule runs one automations tick carries out before it pauses
_stop = threading.Event()
_last = {"unstick": 0.0}
_scheduler = None
_knowledge = None


def _put_back(busy=(), give_up=False):
    """Work nobody holds any more goes back in the queue. With `give_up`, work that keeps stopping part-way is given up
    on instead, and says so. `busy`: what this process is working on itself (left alone)."""
    messages = [b for b in busy if isinstance(b, int)]
    jobs_busy = [b[1] for b in busy if isinstance(b, tuple) and b[0] == "job"]
    runs_busy = [b[1] for b in busy if isinstance(b, tuple) and b[0] == "run"]
    if give_up:
        leases.requeue(Message, "thinking", "failed", Message.started_at.isnot(None),
                       Message.started_at < utcnow() - timedelta(seconds=GIVE_UP_AFTER), busy=messages,
                       values={"finished_at": utcnow(), "error": "This stopped part-way through and couldn't be finished. Please ask again."})
        leases.requeue(Job, "running", "failed", Job.attempts >= 3, busy=jobs_busy,
                       values={"finished_at": utcnow(), "error": "This stopped part-way through three times and was given up on."})
    leases.requeue(Message, "thinking", "queued", busy=messages, values={"next_try": None})
    leases.requeue(Job, "running", "queued", busy=jobs_busy)
    leases.requeue(Run, "running", "queued", busy=runs_busy)
    leases.requeue(Outbox, "sending", "queued")


def recover():
    """When a worker starts: work that was mid-way when a process stopped, and that nobody holds now (its claim ran out),
    starts again. Work another running process holds is left to it."""
    _put_back()
    from . import jobs
    for workspace in Workspace.query.all():  # workspaces from before the AI could look things up: read them once
        if not (workspace.settings or {}).get("knowledge_built"):
            jobs.enqueue("knowledge_rebuild", workspace_id=workspace.id, dedupe=f"knowledge_rebuild:{workspace.id}")
            workspace.settings = {**(workspace.settings or {}), "knowledge_built": True}
    db.session.commit()


def unstick():
    """Work a failure left half done (the database busy too long, a fault of ours, a process that stopped) goes back in
    the queue, as it does after a restart, instead of showing "working…" for ever; work that keeps stopping part-way is
    given up on and says so. What this process is working on, and what other processes hold, is left alone."""
    _put_back(busy=set(_working.values()), give_up=True)


def _work_on(item, value, fn, *args):
    """Do `fn` on an item this thread claimed, then let it go, whatever happened (work a failure left half done is put
    back in the queue by unstick, as before). `value` is what `_working` says this thread is busy with."""
    model, item_id, me = type(item), item.id, threading.get_ident()
    _working[me] = value
    try:
        return fn(*args)
    finally:
        _working.pop(me, None)
        try:
            db.session.rollback()  # anything a failure left half written
            leases.release(model, item_id)
        except Exception:  # noqa: BLE001 - it's free again once its claim runs out
            log.exception("Couldn't let go of %s %s", model.__tablename__, item_id)
            db.session.rollback()
            leases.untrack(model, item_id)


def _next_message(channel):
    """The next turn or question to work on, claimed. Its status stays "queued" (the turn's own code marks it thinking as
    it starts, and takes only a queued one): the claim is what keeps every other worker off it."""
    now = utcnow()
    return leases.claim(Message, Message.channel == channel, Message.role == "assistant",
                        or_(Message.status == "queued", and_(Message.status == "waiting_ai", Message.next_try <= now)),
                        one_per_workspace=(lambda other: [other.channel == "builder", other.role == "assistant"]) if channel == "builder" else None)


def builder_tick():
    from .builder import chat
    message = _next_message("builder")
    if message is None:
        return False
    _work_on(message, message.id, chat.work, message.id)
    return True


def assistant_tick():
    from . import assistant
    message = _next_message("assistant")
    if message is None:
        return False
    _work_on(message, message.id, assistant.work, message.id)
    return True


def jobs_tick():
    from . import discovery, importer, intelligence, jobs  # noqa: F401 (registers the job handlers)
    job = jobs.next_job()
    if job is None:
        return False
    _work_on(job, ("job", job.id), jobs.run, job)
    return True


def knowledge_tick():
    """One process at a time reads into the AI's index (two would read the same things)."""
    global _knowledge
    from . import knowledge, scheduler
    if _knowledge is None:
        _knowledge = scheduler.Lock("knowledge")
    if not _knowledge.held():
        return False
    return knowledge.work()


def _carry_out(run):
    from .engine import automations
    from .engine.records import Engine
    run_id = run.id
    try:
        automations.execute(Engine(db.session.get(Workspace, run.workspace_id), by="automation"), run)
    except Exception:  # noqa: BLE001 - a fault of ours: the run says so instead of being tried for ever
        log.exception("Run %s failed", run_id)
        db.session.rollback()
        run = db.session.get(Run, run_id)
        run.status, run.finished_at = "failed", utcnow()
        run.error = "Something went wrong on our side while this rule ran; it's been logged."
        db.session.commit()


def automation_tick(app=None):
    """Carry out the rule runs that are due: queued for the worker, or waiting (for the AI, an approval's reminder, a
    time) until now. Each is claimed and marked running in one go; an app's runs are done one after another."""
    done = 0
    while done < RUNS_AT_ONCE and not _stop.is_set():
        now = utcnow()
        run = leases.claim(Run, or_(Run.status == "queued", and_(Run.status.in_(["waiting_ai", "waiting", "waiting_approval"]),
                                                                   Run.next_try <= now)),
                           values={"status": "running"}, one_per_workspace=lambda other: [])
        if run is None:
            break
        done += 1
        _work_on(run, ("run", run.id), _carry_out, run)
    return bool(done)


def outbox_tick():
    from . import outbox
    return outbox.work()


def scheduler_tick(app=None):
    """The clock's work, if this process is the scheduler; and in every process, a look for work a stopped one left."""
    global _scheduler
    from . import scheduler
    now = time.monotonic()
    if now - _last.get("unstick", 0.0) > UNSTICK_EVERY:
        _last["unstick"] = now
        unstick()
    if _scheduler is None:
        _scheduler = scheduler.Scheduler()
    _scheduler.tick()
    return False


TICKS = {"builder": builder_tick, "assistant": assistant_tick, "jobs": jobs_tick, "automations": automation_tick,
         "outbox": outbox_tick, "knowledge": knowledge_tick, "scheduler": scheduler_tick}


BACKGROUND = {"outbox", "scheduler"}  # kinds the AI gateway doesn't know by their thread's name: background work too


def _loop(app, kind):
    from . import ai
    tick, pause = TICKS[kind], PAUSE[kind]
    while not _stop.is_set():
        busy = False
        try:
            with app.app_context():
                try:
                    if kind in BACKGROUND:
                        with ai.asking(purpose="background", priority="background"):
                            busy = tick()
                    else:
                        busy = tick()
                finally:
                    db.session.remove()
        except Exception:
            log.exception("Background work (%s) failed", kind)
            _stop.wait(5)
        if not busy:
            _stop.wait(pause)


def _heartbeat(app):
    """Every claim this process holds is renewed while its work goes on (leases.renew)."""
    while not _stop.wait(leases.RENEW_EVERY):
        try:
            with app.app_context():
                leases.renew()
        except Exception:  # noqa: BLE001 - the next beat tries again; the leases have time left
            log.exception("Renewing this process's claims failed")


def kinds_of(app):
    """The kinds of work this process does (WORKER_KINDS), each known."""
    asked = app.config.get("WORKER_KINDS") or KINDS
    unknown = [k for k in asked if k not in TICKS]
    if unknown:
        raise ValueError(f"WORKER_KINDS has {', '.join(unknown)}, which isn't work the worker knows (it knows {', '.join(KINDS)})")
    return list(dict.fromkeys(asked))


def run_threads(app, kinds):
    """Start a thread for each kind of work (BUILDER_CONCURRENCY builder threads, JOB_CONCURRENCY job threads), and the
    heartbeat. Returns the threads. Each thread is named by its kind of work, however many there are of it: the AI
    gateway (app/ai.py) knows from the name what a call is for and how soon it's needed."""
    _stop.clear()
    threads = []
    for kind in kinds:
        count = {"builder": app.config.get("BUILDER_CONCURRENCY", 2), "jobs": app.config.get("JOB_CONCURRENCY", 2)}.get(kind, 1)
        for _ in range(max(1, int(count))):
            threads.append(threading.Thread(target=_loop, args=(app, kind), daemon=True, name=kind))
    threads.append(threading.Thread(target=_heartbeat, args=(app,), daemon=True, name="heartbeat"))
    for thread in threads:
        thread.start()
    return threads


def start(app):
    """The worker as threads of the web process (WORKER=1)."""
    if app.extensions.get("worker_started"):
        return
    app.extensions["worker_started"] = True
    kinds = kinds_of(app)
    with app.app_context():
        recover()
    run_threads(app, kinds)


def stop(threads=(), wait=25):
    """Ask every loop to stop after what it's doing, wait up to `wait` seconds for them, and give up the scheduler."""
    _stop.set()
    deadline = time.monotonic() + wait
    for thread in threads:
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
    for lock in (_scheduler.lock if _scheduler is not None else None, _knowledge):
        if lock is not None:
            lock.release()


def main(argv=None):
    """`python -m app.worker`: the worker as its own process, until it's stopped (Ctrl-C, or ./start.sh stop)."""
    from . import create_app, database
    if (database.folder() / "PG_VERSION").exists():  # the platform's own PostgreSQL (./start.sh starts it first)
        database.start()
    app = create_app({"WORKER": False})  # (WORKER=1 would start the threads inside create_app too)
    try:
        kinds = kinds_of(app)
    except ValueError as exc:
        print(f"The worker didn't start: {exc}", file=sys.stderr)
        return 2
    with app.app_context():
        recover()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: _stop.set())
    threads = run_threads(app, kinds)
    print(f"{app.config['PLATFORM_NAME']}'s worker is running: {', '.join(kinds)}", flush=True)
    while not _stop.is_set():
        _stop.wait(1)
    print("The worker is stopping: finishing what it's doing (work left half done is picked up when it starts again).", flush=True)
    stop(threads)
    return 0


if __name__ == "__main__":  # run through the module's own name, so there's one copy of its state
    from app import worker
    sys.exit(worker.main(sys.argv[1:]))
