"""Claiming queued work, so each item is done once however many web and worker processes are running.

- A worker takes its next item with `claim()`. On PostgreSQL the item is picked with SELECT … FOR UPDATE SKIP LOCKED
  (two workers looking at the same moment each get a different one, without waiting on each other) and marked as
  theirs in the same transaction, with a lease: `claimed_by` (which computer, process and thread) and `lease_until`.
  Nobody else takes it until the lease runs out.
- While the work goes on, the process's heartbeat (`renew()`, every RENEW_EVERY seconds) moves the lease on, so a builder
  turn that takes twenty minutes keeps it. A process that stops, or is killed, stops renewing: after LEASE_SECONDS its
  work is free again and the worker's clean-up (`requeue()`) puts it back in the queue.
- A save that carries out a rule there and then (automations.start) holds the run the same way for as long as a request
  can take (`hold()`), so a worker never starts the same run beside it.

On SQLite (the simulator: one process, no row locks) the same statements run without FOR UPDATE: the UPDATE that only
changes a row nobody holds is what makes a claim stick.
"""
import logging
import os
import socket
import threading
import time
from datetime import timedelta

from sqlalchemy import exists, or_, select, text, update
from sqlalchemy.orm import aliased

from .extensions import db
from .models import utcnow

log = logging.getLogger("leases")
LEASE_SECONDS = 120  # how long a claim lasts without being renewed
RENEW_EVERY = 30  # how often a working process renews what it holds
HOLD_SECONDS = 300  # a run carried out inside a save: nothing renews it, and no request takes this long
NAMESPACE = {"message": 1, "run": 2, "job": 3, "outbox": 4}  # advisory locks: "one per workspace" claims, by kind
_HOST = socket.gethostname()[:60]
_held = {}  # {(model, id): token}: what this process is working on, for the heartbeat
_lock = threading.Lock()


def me():
    """Who is doing it: this computer, process and thread. Two threads of one process are two claimers, so a request
    and a worker thread of the same process never both carry out one run."""
    return f"{_HOST}:{os.getpid()}:{threading.get_ident()}"[:120]


def postgres():
    return db.engine.dialect.name == "postgresql"


def free(model, now=None):
    """Nobody holds it: never claimed, let go, or its lease ran out."""
    now = now or utcnow()
    return or_(model.lease_until.is_(None), model.lease_until < now)


def _held_now(model, now):
    return model.lease_until.isnot(None) & (model.lease_until >= now)


def claim(model, *where, order_by=None, values=None, one_per_workspace=None, seconds=LEASE_SECONDS):
    """Take the first item matching `where` that nobody holds, mark it with `values` and a lease, and return it (None when
    there's nothing to take). `one_per_workspace`, a function of the other item (an alias of `model`) giving the
    conditions that make it the same kind of work, skips items whose workspace already has such work held: an app's
    builder turns are done one after another, never two at once."""
    now, token = utcnow(), me()
    pg = postgres()
    conditions = [*where, free(model, now)]
    if one_per_workspace is not None:
        other = aliased(model)
        conditions.append(~exists().where(other.workspace_id == model.workspace_id, other.id != model.id,
                                          _held_now(other, now), *one_per_workspace(other)))
    query = select(model.id, model.workspace_id).where(*conditions).order_by(*(order_by or [model.id])).limit(10)
    if pg:
        query = query.with_for_update(skip_locked=True, of=model)
    try:
        for item_id, workspace_id in db.session.execute(query).all():
            if one_per_workspace is not None and pg:
                # two claimers may each have picked a different turn of the same app: whoever takes the app's lock goes
                # on, and looks again (now that earlier claims are visible) that no turn of it was taken meanwhile
                if not db.session.execute(text("SELECT pg_try_advisory_xact_lock(CAST(:ns AS integer), CAST(:ws AS integer))"),
                                          {"ns": NAMESPACE.get(model.__tablename__, 9), "ws": int(workspace_id or 0)}).scalar():
                    continue
                other = aliased(model)
                if db.session.execute(select(other.id).where(other.workspace_id == workspace_id, other.id != item_id,
                                                             _held_now(other, utcnow()), *one_per_workspace(other))).first():
                    continue
            taken = db.session.execute(update(model).where(model.id == item_id, free(model, now))
                                       .values(claimed_by=token, lease_until=now + timedelta(seconds=seconds), **(values or {}))
                                       .execution_options(synchronize_session=False))
            if taken.rowcount == 1:
                db.session.commit()
                track(model, item_id, token)
                return db.session.get(model, item_id)
        db.session.rollback()  # lets go of the rows looked at
        return None
    except Exception:
        db.session.rollback()
        raise


def hold(item, seconds=HOLD_SECONDS, wait=0.0, **values):
    """Hold this one item (a run about to be carried out in a save), with `values` set in the same statement. Returns
    False if someone else holds it; `wait` seconds are given for them to finish first (a worker's quick look at a run
    waiting for approval, as the person approves it)."""
    model, token = type(item), me()
    deadline = time.monotonic() + wait
    while True:
        now = utcnow()
        taken = db.session.execute(update(model).where(model.id == item.id, or_(free(model, now), model.claimed_by == token))
                                   .values(claimed_by=token, lease_until=now + timedelta(seconds=seconds), **values)
                                   .execution_options(synchronize_session=False))
        db.session.commit()  # (and the object is read again, with its new lease)
        if taken.rowcount == 1:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.2)


def when_free(item, wait=5.0, **values):
    """Set `values` on an item nobody else holds, without holding it; waits up to `wait` seconds for whoever holds it
    now. Returns whether it was set."""
    model = type(item)
    deadline = time.monotonic() + wait
    while True:
        done = db.session.execute(update(model).where(model.id == item.id, or_(free(model), model.claimed_by == me())).values(**values)
                                  .execution_options(synchronize_session=False))
        db.session.commit()
        if done.rowcount == 1 or time.monotonic() >= deadline:
            return done.rowcount == 1
        time.sleep(0.2)


def mine(item):
    """Whether this thread holds the item now."""
    return item.claimed_by == me() and item.lease_until is not None and item.lease_until >= utcnow()


def let_go(item, commit=True):
    """Done with it (for now): it's free for whoever comes next. Only a claim of this thread's is undone."""
    release(type(item), item.id, commit=commit)


def release(model, item_id, commit=True):
    db.session.execute(update(model).where(model.id == item_id, model.claimed_by == me()).values(claimed_by=None, lease_until=None)
                       .execution_options(synchronize_session=False))
    untrack(model, item_id)
    if commit:
        db.session.commit()


def track(model, item_id, token=None):
    with _lock:
        _held[(model, item_id)] = token or me()


def untrack(model, item_id):
    with _lock:
        _held.pop((model, item_id), None)


def holding():
    with _lock:
        return dict(_held)


def renew():
    """The heartbeat: every claim this process holds lasts another LEASE_SECONDS. Each is renewed on its own, waiting at
    most two seconds for a row its own work has locked (it's tried again next time). Returns the claims found lost
    (someone else had taken them after this process went quiet for too long)."""
    lost = []
    for (model, item_id), token in holding().items():
        table = model.__table__
        try:
            with db.engine.begin() as conn:
                if conn.dialect.name == "postgresql":
                    conn.execute(text("SET LOCAL lock_timeout = '2s'"))
                done = conn.execute(update(table).where(table.c.id == item_id, table.c.claimed_by == token)
                                    .values(lease_until=utcnow() + timedelta(seconds=LEASE_SECONDS)))
            if done.rowcount == 0:
                lost.append((model.__tablename__, item_id))
                untrack(model, item_id)
        except Exception as exc:  # busy for a moment: the lease has time left, and the next beat tries again
            log.info("Couldn't renew the claim on %s %s yet: %s", model.__tablename__, item_id, exc)
    for name, item_id in lost:
        log.warning("The claim on %s %s was lost: another process took it after this one went quiet", name, item_id)
    return lost


def requeue(model, status, back, *where, busy=(), values=None):
    """Work left `status` ("running", "thinking") that nobody holds any more goes back to `back`, as after a restart. Each
    row changes only if it's still unheld when the change is made (a row being worked on is skipped, never waited for).
    `busy`: ids this process is working on (left alone whatever their lease says). Returns how many went back."""
    now = utcnow()
    pick = select(model.id).where(model.status == status, free(model, now), *where)
    if busy:
        pick = pick.where(model.id.notin_(list(busy)))
    if postgres():
        pick = pick.with_for_update(skip_locked=True)
    ids = [row[0] for row in db.session.execute(pick).all()]
    if not ids:
        db.session.rollback()
        return 0
    done = db.session.execute(update(model).where(model.id.in_(ids), model.status == status, free(model, now))
                              .values(status=back, claimed_by=None, lease_until=None, **(values or {}))
                              .execution_options(synchronize_session=False))
    db.session.commit()
    return done.rowcount
