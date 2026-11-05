"""Keeping an existing database in step with the code. New tables are created automatically (create_all); changes to
existing tables are listed here in order and applied once each, schema_step recording how far a database has got. A
new database is made complete by create_all, so it starts at the latest step.

The platform ran on SQLite until step 15; every PostgreSQL database starts at 15 or later, so the steps from 16 on are
PostgreSQL's. Append new steps; never edit or reorder old ones.

The web and the worker start side by side (./start.sh), so `one_at_a_time()` keeps two processes from making the same
tables at the same moment: the second waits, then finds them made.
"""
from contextlib import contextmanager

from sqlalchemy import inspect, text

from .core import tenancy
from .extensions import db

SQLITE_STEPS = 15  # the steps taken on SQLite, before PostgreSQL (each database made since has them already)
STEPS = [
    # 16: PostgreSQL: the AI's passages found by meaning (pgvector) and by words (a full-text index)
    ["CREATE EXTENSION IF NOT EXISTS vector"],
    # 17: several web and worker processes at once: queued work says who is doing it and until when (leases.py), and
    # each workspace when its schedules are next due; the queues are found by their status
    ["ALTER TABLE job ADD COLUMN IF NOT EXISTS claimed_by VARCHAR(120)",
     "ALTER TABLE job ADD COLUMN IF NOT EXISTS lease_until TIMESTAMP WITHOUT TIME ZONE",
     "ALTER TABLE message ADD COLUMN IF NOT EXISTS claimed_by VARCHAR(120)",
     "ALTER TABLE message ADD COLUMN IF NOT EXISTS lease_until TIMESTAMP WITHOUT TIME ZONE",
     "ALTER TABLE run ADD COLUMN IF NOT EXISTS claimed_by VARCHAR(120)",
     "ALTER TABLE run ADD COLUMN IF NOT EXISTS lease_until TIMESTAMP WITHOUT TIME ZONE",
     "ALTER TABLE workspace ADD COLUMN IF NOT EXISTS next_due_at TIMESTAMP WITHOUT TIME ZONE",
     "CREATE INDEX IF NOT EXISTS ix_workspace_next_due_at ON workspace (next_due_at)",
     "CREATE INDEX IF NOT EXISTS ix_message_status ON message (status)",
     "CREATE INDEX IF NOT EXISTS ix_run_status ON run (status)"],
]
# Kept up whatever the step: the indexes PostgreSQL finds things by.
ALWAYS = [
    "CREATE EXTENSION IF NOT EXISTS vector",
    "CREATE INDEX IF NOT EXISTS ix_chunk_words ON chunk USING gin (words)",
    "CREATE INDEX IF NOT EXISTS ix_record_data ON record USING gin (data jsonb_path_ops)",
    "CREATE INDEX IF NOT EXISTS ix_record_ws_table_live ON record (workspace_id, \"table\") WHERE deleted_at IS NULL",
]
ALWAYS += tenancy.policies()  # row-level security on the tenant tables (app/core/tenancy.py)


UPGRADING = 7_301_221  # the advisory lock held while a process brings the database up to date


def latest():
    return SQLITE_STEPS + len(STEPS)


@contextmanager
def one_at_a_time():
    """Only one process makes tables and takes steps at a time (PostgreSQL; anything else has one process)."""
    if db.engine.dialect.name != "postgresql":
        yield
        return
    with db.engine.connect() as conn:
        conn.execute(text("SELECT pg_advisory_lock(CAST(:k AS bigint))"), {"k": UPGRADING})
        conn.commit()
        try:
            yield
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(CAST(:k AS bigint))"), {"k": UPGRADING})
            conn.commit()


def is_new():
    """Whether the database has nothing in it yet (asked before create_all)."""
    return not inspect(db.engine).has_table("workspace")


def upgrade(new=False):
    with db.engine.begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS schema_step (number INTEGER NOT NULL)"))
        current = conn.execute(text("SELECT MAX(number) FROM schema_step")).scalar()
        if current is None:
            current = latest() if new else SQLITE_STEPS  # a database brought over from SQLite has every SQLite step
            conn.execute(text("INSERT INTO schema_step (number) VALUES (:n)"), {"n": current})
        for number, statements in enumerate(STEPS, start=SQLITE_STEPS + 1):
            if number <= current:
                continue
            for statement in statements:
                conn.execute(text(statement))
        conn.execute(text("UPDATE schema_step SET number = :n"), {"n": latest()})
        for statement in ALWAYS:
            conn.execute(text(statement))
