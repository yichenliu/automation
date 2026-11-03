"""Keeping an existing database in step with the code. New tables are created automatically (create_all); changes to
existing tables are listed here in order and applied once each, schema_step recording how far a database has got. A
new database is made complete by create_all, so it starts at the latest step.

The platform ran on SQLite until step 15; every PostgreSQL database starts at 15 or later, so the steps from 16 on are
PostgreSQL's. Append new steps; never edit or reorder old ones.
"""
from sqlalchemy import inspect, text

from .extensions import db

SQLITE_STEPS = 15  # the steps taken on SQLite, before PostgreSQL (each database made since has them already)
STEPS = [
    # 16: PostgreSQL: the AI's passages found by meaning (pgvector) and by words (a full-text index)
    ["CREATE EXTENSION IF NOT EXISTS vector"],
]
# Kept up whatever the step: the indexes PostgreSQL finds things by.
ALWAYS = [
    "CREATE EXTENSION IF NOT EXISTS vector",
    "CREATE INDEX IF NOT EXISTS ix_chunk_words ON chunk USING gin (words)",
    "CREATE INDEX IF NOT EXISTS ix_record_data ON record USING gin (data jsonb_path_ops)",
    "CREATE INDEX IF NOT EXISTS ix_record_ws_table_live ON record (workspace_id, \"table\") WHERE deleted_at IS NULL",
]


def latest():
    return SQLITE_STEPS + len(STEPS)


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
