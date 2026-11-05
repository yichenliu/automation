"""The simulator's database: every table the engine uses, in memory, with snapshots to go back to."""
import sqlite3

PG_ONLY = {"chunk"}  # the AI's passages: vectors and PostgreSQL's full-text index


def create(db):
    db.metadata.create_all(bind=db.engine, tables=[t for t in db.metadata.sorted_tables if t.name not in PG_ONLY])


def _raw(db):
    return db.engine.raw_connection().driver_connection


def snapshot(db):
    """Everything in the simulator now, to restore later (a check starts from the same records every time)."""
    db.session.commit()
    copy = sqlite3.connect(":memory:", check_same_thread=False)
    _raw(db).backup(copy)
    return copy


def restore(db, snap):
    db.session.remove()
    snap.backup(_raw(db))
