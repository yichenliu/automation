"""The simulator: the same engine, the same rules, on an in-memory database instead of PostgreSQL.

A try-out, a check or a candidate design runs in a copy made here in milliseconds rather than a throwaway PostgreSQL
database made in seconds, so the builder can afford to try many designs and keep the one that works. The engine works on
records in Python and keeps its SQL portable (SQLAlchemy's JSON accessors, not PostgreSQL's operators), so what passes
here passes on the live app. What only PostgreSQL has (the AI's passage index) isn't in a copy; nothing a check runs
needs it.
"""
from sqlalchemy.pool import StaticPool


def overrides():
    """The app settings that make an app instance a simulator."""
    return {"SIMULATION": True, "SQLALCHEMY_DATABASE_URI": "sqlite://", "SQLALCHEMY_BINDS": {},
            "SQLALCHEMY_ENGINE_OPTIONS": {"poolclass": StaticPool, "connect_args": {"check_same_thread": False}}}
