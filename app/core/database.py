"""The platform's database access, on plain SQLAlchemy 2: no web framework needed (Keel v2, D9).

It offers what the code has always used: `db.Model` (with `Model.query`), `db.session`, `db.engine`, `db.Column` and
the other SQLAlchemy names, and `db.create_all()`. So models and queries stay as they are while the web framework
underneath them is changed.

Which database a session talks to belongs to the context the code runs in:
- the live app's PostgreSQL;
- a simulator copy's in-memory SQLite, inside it;
- a worker's job.
`using(engine)` enters a context; the platform app's own context (`app.app_context()`) enters its engine this way. Each
context has its own session, removed when the context ends, so a simulator copy opened inside a request never shares
the request's session.
"""
import contextlib
import contextvars
import itertools
import re

import sqlalchemy as sa
from sqlalchemy import orm

_context = contextvars.ContextVar("keel_db_context", default=None)  # (engine, scope id) entered with using()
_scopes = itertools.count(1)


def _snake(name):
    """Flask-SQLAlchemy's table names, kept: ApiKey → api_key, RecordChange → record_change."""
    return re.sub(r"((?<=[a-z0-9])[A-Z]|(?!^)[A-Z](?=[a-z]))", r"_\1", name).lower().lstrip("_")


class _Query:
    """`Model.query`: a query of the model in the session of the context it's asked in."""

    def __get__(self, obj, cls):
        return cls._db.session.query(cls)


class Database:
    def __init__(self):
        db = self
        self.metadata = sa.MetaData()

        class Model(orm.DeclarativeBase):
            metadata = db.metadata
            query = _Query()
            _db = db

            @orm.declared_attr.directive
            def __tablename__(cls):  # noqa: N805
                return _snake(cls.__name__)

        self.Model = Model
        # a context's session is made bound to that context's engine, the first time it's asked for
        self.session = orm.scoped_session(lambda: orm.Session(bind=self.engine), scopefunc=self._scope)

    # the context: which engine, which session ----------------------------------------------------------------------
    def _current(self):
        entered = _context.get()
        if entered is None:
            raise RuntimeError("No database here: run inside the platform's context (db.using(engine), or an app context)")
        return entered

    def _scope(self):
        return self._current()[1]

    @property
    def engine(self):
        return self._current()[0]

    def _session(self):
        """The session of this context, bound to its engine (made the first time it's asked for)."""
        return self.session.registry()

    @contextlib.contextmanager
    def using(self, engine):
        """Run the block against this engine, with a session of its own (removed when the block ends)."""
        token = _context.set((engine, ("using", next(_scopes))))
        try:
            yield self
        finally:
            try:
                self.session.remove()
            finally:
                _context.reset(token)

    # setting up --------------------------------------------------------------------------------------------------------
    @staticmethod
    def make_engine(url, options=None):
        return sa.create_engine(url, **(options or {}))

    def init_app(self, app):
        """The app's engine, made from its config; its context runs against it (app.app_context() enters `using`)."""
        app.engine = self.make_engine(app.config["SQLALCHEMY_DATABASE_URI"], app.config.get("SQLALCHEMY_ENGINE_OPTIONS") or {})
        app.extensions["sqlalchemy"] = self
        return app.engine

    def create_all(self):
        self.metadata.create_all(self.engine)

    def drop_all(self):
        self.metadata.drop_all(self.engine)

    # the SQLAlchemy names models are written with (db.Column, db.Integer, db.func, db.Index…) -----------------------------
    def __getattr__(self, name):
        for module in (sa, orm):
            if hasattr(module, name):
                return getattr(module, name)
        raise AttributeError(name)

