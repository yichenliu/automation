"""Keeping an existing database in step with the code. New tables are created automatically; changes to existing
tables are listed here in order and applied once each (SQLite's user_version records how far a database has got). A new
database is made complete by create_all, so it starts at the latest step."""
import re

from sqlalchemy import inspect, text

from .extensions import db

# Each step: a list of SQL statements. Append new steps; never edit or reorder old ones.
STEPS = [
    [],  # 1: the first release
    ["ALTER TABLE record ADD COLUMN version INTEGER NOT NULL DEFAULT 1"],  # 2: records refuse out-of-date saves
    ["ALTER TABLE membership ADD COLUMN contact_id INTEGER", "ALTER TABLE invite ADD COLUMN contact_id INTEGER",
     "ALTER TABLE comment ADD COLUMN shared BOOLEAN DEFAULT 0"],  # 3: the portal for customers and suppliers
    ["ALTER TABLE record ADD COLUMN updated_by VARCHAR(200)"],  # 4: who last changed a record (for "modified by" fields)
    ["ALTER TABLE user ADD COLUMN totp_secret TEXT", "ALTER TABLE user ADD COLUMN notify_email BOOLEAN DEFAULT 0"],  # 5: two-step sign-in
    ["ALTER TABLE notification ADD COLUMN emailed_at DATETIME", "UPDATE notification SET emailed_at = created_at"],  # 6: notification emails
    ["ALTER TABLE record ADD COLUMN created_by_id INTEGER", "ALTER TABLE file ADD COLUMN uploaded_by_id INTEGER",
     "ALTER TABLE user ADD COLUMN ms_identity VARCHAR(120)",
     "CREATE UNIQUE INDEX IF NOT EXISTS ix_user_ms_identity ON user (ms_identity)"],  # 7: ownership by id, linked Microsoft sign-in
    ["ALTER TABLE approval ADD COLUMN kind VARCHAR(16) DEFAULT 'approval'", "ALTER TABLE approval ADD COLUMN step VARCHAR(40)",
     "ALTER TABLE approval ADD COLUMN fields JSON", "ALTER TABLE approval ADD COLUMN history JSON"],  # 8: approval flows and tasks
    ["ALTER TABLE approval ADD COLUMN proposal JSON"],  # 9: people checking the AI's work
    ["ALTER TABLE membership ADD COLUMN teams JSON", "ALTER TABLE membership ADD COLUMN away_from DATE",
     "ALTER TABLE membership ADD COLUMN away_until DATE", "ALTER TABLE membership ADD COLUMN cover_id INTEGER",
     "ALTER TABLE record_change ADD COLUMN by_id INTEGER"],  # 10: teams, cover while away, separation of duties
    ["ALTER TABLE message ADD COLUMN checks JSON"],  # 11: the builder tries each change out (the feedback table is new: created whole)
    ["ALTER TABLE approval ADD COLUMN decided_by_id INTEGER"],  # 12: who approved, for the record's "Approved by"
    ["ALTER TABLE invite ADD COLUMN teams JSON"],  # 13: an invitation says which teams the person joins
    ["ALTER TABLE approval ADD COLUMN priority INTEGER DEFAULT 1", "ALTER TABLE approval ADD COLUMN deferred_until DATETIME",
     "ALTER TABLE approval ADD COLUMN taken_by_id INTEGER", "ALTER TABLE approval ADD COLUMN flow VARCHAR(64)"],  # 14: work queues
]
# Kept up whatever the step: the full-text index of the AI's knowledge (rowid = chunk.id).
ALWAYS = ["CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(title, body, ws UNINDEXED, tokenize='porter unicode61 remove_diacritics 2')"]


def is_new():
    """Whether the database has nothing in it yet (asked before create_all)."""
    return not inspect(db.engine).has_table("workspace")


def _has_column_already(conn, statement):
    """An ADD COLUMN whose column is there already (a step half-applied before, or a database made by a newer build)."""
    match = re.match(r"\s*ALTER TABLE (\w+) ADD COLUMN (\w+)", statement, re.I)
    if not match:
        return False
    return match.group(2) in [row[1] for row in conn.execute(text(f"PRAGMA table_info({match.group(1)})"))]


def upgrade(new=False):
    if db.engine.dialect.name != "sqlite":
        return
    with db.engine.begin() as conn:
        current = len(STEPS) if new else conn.execute(text("PRAGMA user_version")).scalar() or 0
        for number, statements in enumerate(STEPS, start=1):
            if number <= current:
                continue
            for statement in statements:
                if _has_column_already(conn, statement):
                    continue
                conn.execute(text(statement))
        conn.execute(text(f"PRAGMA user_version = {len(STEPS)}"))
        for statement in ALWAYS:
            conn.execute(text(statement))
