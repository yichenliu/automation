"""The platform's database: PostgreSQL with pgvector, on this computer.

- `./start.sh` starts a PostgreSQL of the platform's own, kept in data/postgres and listening only on this computer
  (port 5433). The first time (`setup`) it's made, with a database and a login of the platform's own whose password goes
  in .env as DATABASE_URL. pgvector is in every database made on it (it's in template1), for the AI's search by meaning;
  full-text search is PostgreSQL's own.
- `throwaway()` makes an empty database beside the platform's for a while (the builder's try-out of each change, the
  tests, the evals) and drops it after.
- `fix_sequences()`: rows put in with their own ids (copied from elsewhere) leave each table's id counter where it was;
  this moves it past them, so the next row gets a new id.

Run as `python -m app.database setup|start|stop|status|url`.
"""
import logging
import os
import secrets
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parent.parent
BIN = Path(os.getenv("PG_BIN") or "/opt/homebrew/opt/postgresql@18/bin")
PORT = int(os.getenv("PG_PORT") or 5433)
NAME = "keel"
SETTINGS = """
# --- set by the platform (app/database.py) ---
port = {port}
listen_addresses = 'localhost'
unix_socket_directories = '{socket}'
shared_buffers = 512MB
effective_cache_size = 2GB
work_mem = 16MB
maintenance_work_mem = 256MB
max_connections = 200
wal_compression = on
random_page_cost = 1.1
"""


class DatabaseProblem(Exception):
    """PostgreSQL couldn't be set up or reached; the message says what to do."""


def folder():
    return Path(os.getenv("PG_DATA") or ROOT / "data" / "postgres")


def _run(tool, *args, check=True):
    path = BIN / tool
    if not path.exists():
        raise DatabaseProblem(f"PostgreSQL isn't installed here ({path}): run `brew install postgresql@18 pgvector`")
    done = subprocess.run([str(path), *args], capture_output=True, text=True)
    if check and done.returncode != 0:
        raise DatabaseProblem(f"{tool} {' '.join(args[:2])}: {(done.stderr or done.stdout).strip()[:500]}")
    return done


def _psql(sql, db="postgres"):
    return _run("psql", "-h", str(folder()), "-p", str(PORT), "-U", "postgres", "-d", db, "-v", "ON_ERROR_STOP=1", "-tAc", sql).stdout.strip()


def running():
    return (folder() / "PG_VERSION").exists() and _run("pg_ctl", "-D", str(folder()), "status", check=False).returncode == 0


def start():
    if not (folder() / "PG_VERSION").exists():
        raise DatabaseProblem("There's no database yet: run ./start.sh setup")
    if not running():
        (ROOT / "data" / "logs").mkdir(parents=True, exist_ok=True)
        _run("pg_ctl", "-D", str(folder()), "-l", str(ROOT / "data" / "logs" / "postgres.log"), "-w", "-t", "60", "start")


def stop():
    if running():
        _run("pg_ctl", "-D", str(folder()), "-m", "fast", "-w", "stop")


def setup(env_file=ROOT / ".env"):
    """Make the cluster the first time, start it, and see the platform's login, database and pgvector are there.
    Returns the DATABASE_URL (written to .env when it's new)."""
    d = folder()
    if not (d / "PG_VERSION").exists():
        d.parent.mkdir(parents=True, exist_ok=True)
        _run("initdb", "-D", str(d), "-E", "UTF8", "--locale=en_US.UTF-8", "-U", "postgres", "--auth-local=trust", "--auth-host=scram-sha-256")
        os.chmod(d, 0o700)  # the socket's in here: only this computer's account reaches it without a password
        with open(d / "postgresql.conf", "a") as conf:
            conf.write(SETTINGS.format(port=PORT, socket=d))
    start()
    url = os.getenv("DATABASE_URL") or _from_env_file(env_file)
    for db in ("template1",):  # every database made from now on has pgvector
        _psql("CREATE EXTENSION IF NOT EXISTS vector", db)
    if not url:
        password = secrets.token_urlsafe(24)
        if _psql(f"SELECT 1 FROM pg_roles WHERE rolname = '{NAME}'"):
            _psql(f"ALTER ROLE {NAME} WITH LOGIN CREATEDB PASSWORD '{password}'")
        else:
            _psql(f"CREATE ROLE {NAME} WITH LOGIN CREATEDB PASSWORD '{password}'")
        url = f"postgresql+psycopg://{NAME}:{password}@localhost:{PORT}/{NAME}"
        _write_env(env_file, "DATABASE_URL", url)
    name = urlsplit(url).path.lstrip("/") or NAME
    if _psql("SELECT 1 FROM pg_roles WHERE rolname = 'pg_signal_autovacuum_worker'"):  # PostgreSQL 18: so a throwaway copy
        _psql(f"GRANT pg_signal_autovacuum_worker TO {urlsplit(url).username or NAME}")  # being vacuumed can still be dropped
    if not _psql(f"SELECT 1 FROM pg_database WHERE datname = '{name}'"):
        _psql(f'CREATE DATABASE "{name}" OWNER {urlsplit(url).username or NAME}')
    _psql("CREATE EXTENSION IF NOT EXISTS vector", name)
    return url


def _from_env_file(path):
    if not Path(path).exists():
        return None
    for line in Path(path).read_text().splitlines():
        if line.startswith("DATABASE_URL=") and line.split("=", 1)[1].strip():
            return line.split("=", 1)[1].strip()
    return None


def _write_env(path, key, value):
    lines = Path(path).read_text().splitlines() if Path(path).exists() else []
    lines = [line for line in lines if not line.startswith(f"{key}=")] + [f"{key}={value}"]
    Path(path).write_text("\n".join(lines) + "\n")
    os.chmod(path, 0o600)


def with_database(url, name):
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=f"/{name}"))


@contextmanager
def throwaway(url, prefix="tmp"):
    """An empty database beside `url`'s for the length of the block (pgvector in it), dropped after."""
    from sqlalchemy import create_engine, text
    name = f"{prefix}_{uuid.uuid4().hex[:16]}"
    admin = create_engine(with_database(url, "postgres"), isolation_level="AUTOCOMMIT", poolclass=_null_pool())
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
        yield with_database(url, name)
    finally:
        _drop(admin, name)
        admin.dispose()


def _drop(admin, name, tries=20):
    """Drop a throwaway database. The server's own vacuum may be busy in it for a moment (a login that may not stop it
    can't force it out), so a refused drop is tried again shortly, and one still refused is left, logged, for later:
    what it was for has already worked."""
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError
    for n in range(tries):
        try:
            with admin.connect() as conn:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            return True
        except DBAPIError as exc:
            if n == tries - 1:
                logging.getLogger(__name__).warning("Couldn't drop the throwaway database %s: %s", name, exc)
                return False
            time.sleep(0.25)


def fresh(url, name):
    """A database of this name beside `url`'s, made again empty (an eval's scenario: kept after, for a look). Returns
    its address."""
    from sqlalchemy import create_engine, text
    name = name[:63]
    admin = create_engine(with_database(url, "postgres"), isolation_level="AUTOCOMMIT", poolclass=_null_pool())
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    admin.dispose()
    return with_database(url, name)


def _null_pool():
    from sqlalchemy.pool import NullPool
    return NullPool


def fix_sequences(session):
    """Each table's id counter moved past the ids already in it (after rows were copied in with their own)."""
    from sqlalchemy import text
    if session.get_bind().dialect.name != "postgresql":
        return  # SQLite carries on after the highest id by itself
    rows = session.execute(text("""SELECT c.table_name, pg_get_serial_sequence(quote_ident(c.table_name), c.column_name)
                                   FROM information_schema.columns c WHERE c.table_schema = 'public' AND c.column_name = 'id'
                                   AND c.column_default LIKE 'nextval%'""")).fetchall()
    for table, sequence in rows:
        if sequence:
            session.execute(text(f'SELECT setval(:s, COALESCE((SELECT MAX(id) FROM "{table}"), 0) + 1, false)'), {"s": sequence})


def import_sqlite(path, app):
    """Copy everything from the platform's old SQLite database into this (empty) PostgreSQL one, ids and all: JSON,
    dates and yes/no turned into PostgreSQL's own, the AI's vectors into pgvector's. Returns {table: (rows in the old, rows
    copied)}; rows that no longer belong anywhere (a record of a workspace that's gone) are left behind and counted."""
    import json
    import sqlite3
    from datetime import date, datetime

    import numpy as np
    from pgvector.sqlalchemy import Vector
    from sqlalchemy import Boolean, Date, DateTime, text
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.types import JSON

    from .extensions import db
    source = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    there = {r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    out = {}

    def convert(column, value):
        if value is None:
            return None
        kind = column.type
        if isinstance(kind, (JSON, JSONB)):
            return json.loads(value) if isinstance(value, (str, bytes)) else value
        if isinstance(kind, Vector):
            return [float(x) for x in np.frombuffer(value, dtype=np.float32)] if isinstance(value, (bytes, memoryview)) else value
        if isinstance(kind, DateTime):
            return datetime.fromisoformat(value) if isinstance(value, str) else value
        if isinstance(kind, Date):
            return date.fromisoformat(value[:10]) if isinstance(value, str) else value
        if isinstance(kind, Boolean):
            return bool(value)
        return value

    with app.app_context():
        for table in db.metadata.sorted_tables:  # what others point at first
            if table.name not in there:
                continue
            names = {r[1] for r in source.execute(f'PRAGMA table_info("{table.name}")')}
            columns = [c for c in table.columns if c.name in names and c.computed is None]
            rows = []
            for row in source.execute(f'SELECT * FROM "{table.name}"'):
                values = {c.name: convert(c, row[c.name]) for c in columns}
                if table.name == "chunk" and values.get("embedding") is not None:
                    values["dims"] = len(values["embedding"])
                rows.append(values)
            copied = 0
            for start in range(0, len(rows), 500):
                batch = rows[start:start + 500]
                try:
                    with db.session.begin_nested():
                        db.session.execute(table.insert(), batch)
                    copied += len(batch)
                except IntegrityError:  # one that no longer belongs anywhere: the rest of the batch one by one
                    for one in batch:
                        try:
                            with db.session.begin_nested():
                                db.session.execute(table.insert(), [one])
                            copied += 1
                        except IntegrityError:
                            pass
            db.session.commit()
            out[table.name] = (len(rows), copied)
        fix_sequences(db.session)
        for (dims,) in db.session.execute(text("SELECT DISTINCT dims FROM chunk WHERE dims IS NOT NULL")):
            from . import knowledge
            knowledge._vector_index(dims)
        db.session.commit()
    source.close()
    return out


def main(argv):
    what = argv[1] if len(argv) > 1 else "status"
    try:
        if what == "setup":
            url = setup()
            print("PostgreSQL is ready:", urlsplit(url)._replace(netloc=f"{urlsplit(url).username}:•••@{urlsplit(url).hostname}:{urlsplit(url).port}").geturl())
        elif what == "start":
            start()
            print("PostgreSQL is running.")
        elif what == "stop":
            stop()
            print("PostgreSQL stopped.")
        elif what == "import-sqlite":
            from . import create_app
            path = Path(argv[2]) if len(argv) > 2 else ROOT / "data" / "erp.db"
            app = create_app({"WORKER": False})
            with app.app_context():
                from .extensions import db
                from .models import Workspace
                if db.session.query(Workspace.id).first() is not None:
                    raise DatabaseProblem("PostgreSQL has workspaces in it already: it's only brought over into an empty one")
            moved = import_sqlite(path, app)
            for name, (was, now) in moved.items():
                print(f"  {name}: {now} of {was}" + ("" if now == was else f"  ({was - now} left behind: they belonged to nothing)"))
            print(f"Brought over from {path}.")
        else:
            print("PostgreSQL is running." if running() else "PostgreSQL isn't running.")
    except DatabaseProblem as exc:
        print(f"Database: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
