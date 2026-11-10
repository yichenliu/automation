"""One scheduler, however many processes run.

The work that comes round by the clock (schedules, connected apps' syncs, watching Microsoft 365, the analyst's daily
look, emailing notifications, the daily backup, tidying up) is done only by the process holding a PostgreSQL advisory
lock, on a connection kept open for nothing else (`Lock`). Every worker process tries for it each time round: one gets
it, the others skip that work. If the holder stops, or its connection drops, PostgreSQL lets the lock go with it, and
the next process to try takes over.

Each kind of work reads only what is due: workspaces whose schedules' next time has come (`Workspace.next_due_at`, set
by automations.due and cleared when the design changes), and for syncs and mail only workspaces connected to an app or
to Microsoft 365.
"""
import hashlib
import logging
import time
from datetime import timedelta

from sqlalchemy import create_engine, or_, text
from sqlalchemy.pool import NullPool

from .extensions import db
from .models import Connection, Job, ScheduleRun, Workspace, utcnow

log = logging.getLogger("scheduler")
EVERY = {"schedules": 30, "notifications": 60, "apps": 60, "watch": 120, "learn": 3600, "backup": 3600, "tidy": 3600, "norms": 20,
         "retention": 3600}  # seconds
DUE_AT_ONCE = 500  # workspaces looked at in one go (the rest are still due next time round)
KEEP_SLOTS = timedelta(days=60)  # how long a schedule's taken times are kept


def lock_key(name):
    """The advisory lock's number for a name: the same in every process."""
    return int.from_bytes(hashlib.sha256(f"keel:{name}".encode()).digest()[:8], "big") & 0x7FFF_FFFF_FFFF_FFFF


class Lock:
    """A PostgreSQL session advisory lock, on a connection of its own that stays open while it's held. Anything else
    than PostgreSQL has a single process, which always holds it."""

    def __init__(self, name):
        self.name, self.key = name, lock_key(name)
        self._engine = self._conn = None

    def held(self):
        """Whether this process holds the lock now, trying for it if it doesn't."""
        if db.engine.dialect.name != "postgresql":
            return True
        if self._conn is not None:
            try:
                self._conn.execute(text("SELECT 1"))
                return True
            except Exception:  # noqa: BLE001 - the connection went, and the lock with it: another process may have it now
                log.warning("Lost the %s lock (its connection closed)", self.name)
                self._drop()
        try:
            if self._engine is None:
                self._engine = create_engine(db.engine.url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
            conn = self._engine.connect()
            if conn.execute(text("SELECT pg_try_advisory_lock(CAST(:k AS bigint))"), {"k": self.key}).scalar():
                self._conn = conn
                log.info("This process now does the %s work", self.name)
                return True
            conn.close()
        except Exception as exc:  # noqa: BLE001 - the database is away: nobody does it until it's back
            log.warning("Couldn't try for the %s lock: %s", self.name, exc)
        return False

    def release(self):
        self._drop()
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None

    def _drop(self):
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()  # closing the connection lets the lock go
            except Exception:  # noqa: BLE001
                pass


# --- the work -----------------------------------------------------------------------------------------------------------

def schedules():
    """Start the schedules whose time has come, in the workspaces that are due."""
    from .engine import automations
    from .engine.records import Engine
    now = utcnow()
    due = [i for (i,) in db.session.query(Workspace.id).filter(or_(Workspace.next_due_at.is_(None), Workspace.next_due_at <= now))
           .order_by(Workspace.next_due_at.nullsfirst(), Workspace.id).limit(DUE_AT_ONCE)]
    for workspace_id in due:
        try:
            automations.due(Engine(db.session.get(Workspace, workspace_id), by="schedule"))
        except Exception:  # noqa: BLE001 - one workspace's trouble doesn't hold up the others; it's looked at again soon
            log.exception("Schedules of workspace %s failed", workspace_id)
            db.session.rollback()
            ws = db.session.get(Workspace, workspace_id)
            if ws is not None:
                ws.next_due_at = utcnow() + timedelta(minutes=5)
                db.session.commit()
    return len(due)


def _connected(kind_like):
    return [i for (i,) in db.session.query(Connection.workspace_id).filter(Connection.kind.like(kind_like),
                                                                             Connection.status == "connected").distinct()]


def apps():
    """What's due from connected apps (orders from the shop, payments from the books), in workspaces connected to one."""
    from .connectors import apps as connected_apps
    for workspace_id in _connected("app:%"):
        workspace = db.session.get(Workspace, workspace_id)
        for sync in connected_apps.due(workspace) if workspace is not None else []:
            try:
                connected_apps.run_sync(workspace, sync)
            except Exception as exc:  # noqa: BLE001 - one app's trouble doesn't stop the others; it's kept and shown in Connections
                log.warning("Bringing in %s %s for workspace %s failed: %s", sync["app"], sync["pull"], workspace_id, exc)
                db.session.rollback()


def watch():
    """New mail and files in Microsoft 365, in workspaces connected to it."""
    from .connectors import watch as m365
    for workspace_id in _connected("microsoft365"):
        workspace = db.session.get(Workspace, workspace_id)
        try:
            if workspace is not None:
                m365.check(workspace)
        except Exception as exc:  # noqa: BLE001 - one workspace's mailbox trouble doesn't stop the others
            log.warning("Watching Microsoft 365 for workspace %s failed: %s", workspace_id, exc)
            db.session.rollback()


def learn():
    """Once a day, the analyst looks at how each app is being used (those it hasn't looked at today)."""
    from . import jobs
    today = utcnow().strftime("%Y-%m-%d")
    done = {d for (d,) in db.session.query(Job.dedupe).filter(Job.dedupe.like(f"learn:%:{today}"))}
    for (workspace_id,) in db.session.query(Workspace.id).order_by(Workspace.id):
        key = f"learn:{workspace_id}:{today}"
        if key in done:
            continue
        if (db.session.get(Workspace, workspace_id).blueprint or {}).get("tables"):
            jobs.enqueue("learn", {"ai": True}, workspace_id=workspace_id, dedupe=key)


def notifications():
    from .engine import automations
    automations.email_notifications()


def backup():
    """A backup a day."""
    from . import backup as backups
    latest = backups.latest()
    if latest is None or latest.stat().st_mtime < time.time() - 86400:
        log.info("Backup made: %s", backups.make())


def tidy():
    """Old counts of rate limits and schedules' times long past are let go."""
    from . import security
    security.forget_old_hits()
    ScheduleRun.query.filter(ScheduleRun.created_at < utcnow() - KEEP_SLOTS).delete(synchronize_session=False)
    db.session.commit()


def retention():
    """Each app's lists that say how long their records are kept: what's past its time deleted (unless held), what was
    deleted long enough ago erased (app/engine/retention.py)."""
    from .engine import retention as kept
    for ws in Workspace.query.order_by(Workspace.id).all():
        if not any(t.get("keep_for") for t in (ws.blueprint or {}).get("tables", [])):
            continue
        try:
            kept.run(ws)
        except Exception:  # noqa: BLE001 - one app's trouble doesn't stop the others'
            log.exception("Retention failed for workspace %s", ws.id)
            db.session.rollback()


def norms(batch=2000):
    """Records saved before their values were kept as compared (models.normalised) get them, a batch at a time: until
    then they're read along with what the index finds, so nothing is ever missed."""
    from .models import Record, normalised
    rows = Record.query.filter(Record.norm.is_(None)).order_by(Record.id).limit(batch).all()
    for r in rows:
        r.norm = normalised(r.data)
    if rows:
        db.session.commit()
    return len(rows)


WORK = {"schedules": schedules, "notifications": notifications, "apps": apps, "watch": watch, "learn": learn, "backup": backup,
        "tidy": tidy, "norms": norms, "retention": retention}


class Scheduler:
    """The clock's work, done while this process holds the scheduler lock."""

    def __init__(self, name="scheduler"):
        self.lock = Lock(name)
        self.leading = False
        self.last = {}

    def tick(self):
        """Do whatever kind of work is due, if this process is the scheduler. Returns whether it is."""
        if not self.lock.held():
            self.leading = False
            return False
        if not self.leading:  # just took over: everything is looked at now, and the apps' checks after a platform update
            self.leading, self.last = True, {}
            try:
                from .builder import checking
                checking.after_platform_update()
            except Exception:  # noqa: BLE001
                log.exception("Couldn't queue the apps' checks after an update")
                db.session.rollback()
        now = time.monotonic()
        for name, seconds in EVERY.items():
            if now - self.last.get(name, -10 ** 9) < seconds:
                continue
            self.last[name] = now
            try:
                WORK[name]()
            except Exception:  # noqa: BLE001 - one kind of work failing doesn't stop the rest
                log.exception("The scheduler's %s work failed", name)
                db.session.rollback()
        return True

    def stop(self):
        self.lock.release()
        self.leading = False
