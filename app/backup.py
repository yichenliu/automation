"""Backups: the database (pg_dump: a consistent copy, even while the platform runs), every stored file, and the key that
unlocks stored sign-ins, in one zip under data/backups. The worker makes one a day and keeps the most recent BACKUP_KEEP.
To restore: stop the platform, unzip into an empty data folder, start PostgreSQL (./start.sh setup), then load the dump:
pg_restore --clean --if-exists --no-owner -d "$DATABASE_URL" database.dump (without the "+psycopg" in the address)."""
import os
import subprocess
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import urlsplit


from .database import BIN
from .models import utcnow
from .core.settings import settings


def _dump(uri, target):
    """The whole database in pg_dump's own format (compressed, restorable table by table)."""
    parts = urlsplit(uri)
    env = {**os.environ, "PGPASSWORD": parts.password or ""}
    subprocess.run([str(BIN / "pg_dump"), "-h", parts.hostname or "localhost", "-p", str(parts.port or 5432), "-U", parts.username or "",
                    "-d", parts.path.lstrip("/"), "-F", "c", "-f", str(target)], env=env, check=True, capture_output=True, timeout=3600)


def make():
    data = Path(settings()["DATA_DIR"])
    folder = data / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"backup-{utcnow():%Y%m%d-%H%M%S}.zip"
    with tempfile.TemporaryDirectory() as tmp, zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as out:
        dump = Path(tmp) / "database.dump"
        _dump(settings()["SQLALCHEMY_DATABASE_URI"], dump)
        out.write(dump, "database.dump")
        if (data / "secret_key").exists():
            out.write(data / "secret_key", "secret_key")
        files = data / "files"
        if files.exists():
            for path in files.rglob("*"):
                if path.is_file():
                    out.write(path, str(path.relative_to(data)))
    keep = settings()["BACKUP_KEEP"]
    for old in sorted(folder.glob("backup-*.zip"))[:-keep]:
        old.unlink()
    return target


def latest():
    folder = Path(settings()["DATA_DIR"]) / "backups"
    found = sorted(folder.glob("backup-*.zip")) if folder.exists() else []
    return found[-1] if found else None
