"""The platform's PostgreSQL: throwaway copies (a try-out's) are dropped even when the server is busy in them."""
from contextlib import contextmanager

from sqlalchemy.exc import DBAPIError

from app import database


class _Busy:
    """An admin connection whose first drops are refused, as when the server's vacuum is working in the database."""

    def __init__(self, refusals):
        self.refusals, self.dropped = refusals, []

    @contextmanager
    def connect(self):
        yield self

    def execute(self, statement):
        if self.refusals:
            self.refusals -= 1
            raise DBAPIError("DROP DATABASE", {}, Exception("permission denied to terminate process"))
        self.dropped.append(str(statement))


def test_a_throwaway_database_the_server_is_busy_in_is_dropped_once_its_free_and_never_fails_the_work(monkeypatch):
    monkeypatch.setattr(database.time, "sleep", lambda s: None)
    busy = _Busy(refusals=3)
    assert database._drop(busy, "tryout_x") is True and busy.dropped == ['DROP DATABASE IF EXISTS "tryout_x" WITH (FORCE)']
    stuck = _Busy(refusals=99)
    assert database._drop(stuck, "tryout_y", tries=4) is False and not stuck.dropped  # left for later, logged: no error raised
