"""Background jobs: work that takes a while (importing a spreadsheet, reading a business's files, the AI filling a
field or reading a document). A job is queued in the database and done by the worker; if the AI can't be reached it
waits and tries again instead of failing. Handlers update `job.stage` and `job.progress` so screens can show progress."""
import logging
import traceback
from datetime import timedelta

from .extensions import db
from .models import Job, utcnow

log = logging.getLogger("jobs")
HANDLERS = {}
AI_RETRY_SECONDS = 30


def handler(kind):
    def register(fn):
        HANDLERS[kind] = fn
        return fn
    return register


def enqueue(kind, payload=None, *, workspace_id=None, dedupe=None, delay=0, by=None):
    if dedupe:
        existing = Job.query.filter(Job.dedupe == dedupe, Job.status.in_(["queued", "running", "waiting_ai"])).first()
        if existing:
            return existing
    job = Job(kind=kind, payload=payload or {}, workspace_id=workspace_id, dedupe=dedupe, created_by=by,
              run_after=utcnow() + timedelta(seconds=delay), stage="Waiting to start")
    db.session.add(job)
    db.session.commit()
    return job


def stage(job, text, progress=None):
    job.stage = text[:300]
    if progress is not None:
        job.progress = max(0, min(100, int(progress)))
    db.session.commit()


def run(job):
    from . import ai
    fn = HANDLERS.get(job.kind)
    job.status, job.attempts = "running", (job.attempts or 0) + 1
    db.session.commit()
    try:
        if fn is None:
            raise RuntimeError(f"Nothing knows how to do “{job.kind}”.")
        job.result = fn(job)
        job.status, job.progress, job.finished_at, job.error = "done", 100, utcnow(), None
        db.session.commit()
    except ai.AIUnavailable as exc:
        db.session.rollback()
        job = db.session.get(Job, job.id)
        job.status, job.error = "waiting_ai", f"Waiting for the AI: {exc}"
        job.run_after = utcnow() + timedelta(seconds=AI_RETRY_SECONDS)
        db.session.commit()
    except Exception as exc:  # a job that fails says why, and the rest of the platform carries on
        db.session.rollback()
        job = db.session.get(Job, job.id)
        log.error("Job %s (%s) failed: %s\n%s", job.id, job.kind, exc, traceback.format_exc())
        job.status, job.error, job.finished_at = "failed", str(exc)[:2000] or type(exc).__name__, utcnow()
        db.session.commit()
    return job


def next_job():
    return (Job.query.filter(Job.status.in_(["queued", "waiting_ai"]), Job.run_after <= utcnow())
            .order_by(Job.id).first())
