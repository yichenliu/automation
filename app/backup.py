"""Backups: the database (a consistent copy, even while the platform runs), every stored file, and the key that unlocks
stored sign-ins, in one zip under data/backups. The worker makes one a day and keeps the most recent BACKUP_KEEP.
To restore: stop the platform, unzip into an empty data folder, start it."""
import sqlite3
import tempfile
import zipfile
from pathlib import Path

from flask import current_app

from .models import utcnow


def make():
    data = Path(current_app.config["DATA_DIR"])
    folder = data / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"backup-{utcnow():%Y%m%d-%H%M%S}.zip"
    uri = current_app.config["SQLALCHEMY_DATABASE_URI"]
    with tempfile.TemporaryDirectory() as tmp, zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as out:
        if uri.startswith("sqlite:///"):
            copy = Path(tmp) / "erp.db"
            source = sqlite3.connect(uri.removeprefix("sqlite:///"))
            dest = sqlite3.connect(copy)
            with dest:
                source.backup(dest)
            source.close()
            dest.close()
            out.write(copy, "erp.db")
        if (data / "secret_key").exists():
            out.write(data / "secret_key", "secret_key")
        files = data / "files"
        if files.exists():
            for path in files.rglob("*"):
                if path.is_file():
                    out.write(path, str(path.relative_to(data)))
    keep = current_app.config["BACKUP_KEEP"]
    for old in sorted(folder.glob("backup-*.zip"))[:-keep]:
        old.unlink()
    return target


def latest():
    folder = Path(current_app.config["DATA_DIR"]) / "backups"
    found = sorted(folder.glob("backup-*.zip")) if folder.exists() else []
    return found[-1] if found else None
