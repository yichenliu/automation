"""Files on disk: uploads, generated documents, attachments from email and OneDrive. Each is described by a File row
and kept under the data folder; names are cleaned and contents hashed."""
import hashlib
import mimetypes
import re
import uuid
from pathlib import Path

from flask import current_app

from .extensions import db
from .models import File, utcnow

BLOCKED = {".exe", ".bat", ".cmd", ".com", ".scr", ".js", ".vbs", ".ps1", ".sh", ".app", ".dmg", ".msi", ".jar", ".html", ".htm", ".svg"}


class FileRejected(ValueError):
    pass


def clean_name(name):
    name = Path(str(name or "file")).name
    name = re.sub(r"[^\w.\- ()&+,]", "_", name).strip(" .") or "file"
    return name[:180]


def save(workspace_id, data, name, *, source="upload", record_id=None, field="", by=None, mime=None):
    """Keep a file. `data` is bytes or a file-like object (an upload)."""
    name = clean_name(name)
    if Path(name).suffix.lower() in BLOCKED:
        raise FileRejected(f"“{name}” is a kind of file that can't be stored here.")
    content = data if isinstance(data, (bytes, bytearray)) else data.read()
    limit = current_app.config["MAX_UPLOAD_MB"] * 1024 * 1024
    if len(content) > limit:
        raise FileRejected(f"“{name}” is larger than {current_app.config['MAX_UPLOAD_MB']} MB.")
    if not content:
        raise FileRejected(f"“{name}” is empty.")
    now = utcnow()
    relative = Path("files") / str(workspace_id) / f"{now:%Y}" / f"{now:%m}" / f"{uuid.uuid4().hex[:12]}_{name}"
    target = Path(current_app.config["DATA_DIR"]) / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    uploader = None
    try:
        from flask_login import current_user
        uploader = current_user.id if current_user and current_user.is_authenticated else None
    except Exception:  # outside a request (the worker, an email arriving)
        uploader = None
    record = File(workspace_id=workspace_id, record_id=record_id, field=field or "", name=name, size=len(content), uploaded_by_id=uploader,
                  mime=mimetypes.guess_type(name)[0] or ("application/pdf" if mime == "application/pdf" else "application/octet-stream"),
                  sha256=hashlib.sha256(content).hexdigest(), path=str(relative), source=source, by=by)
    db.session.add(record)
    db.session.commit()
    if source != "generated":  # what the platform makes from records is known from the records themselves
        from . import knowledge
        knowledge.touch(workspace_id, "file", record.id)
    return record


def path_of(file):
    return Path(current_app.config["DATA_DIR"]) / file.path


def files_for(record_id, field=None):
    q = File.query.filter_by(record_id=record_id, deleted_at=None)
    if field is not None:
        q = q.filter_by(field=field)
    return q.order_by(File.id).all()


def human_size(n):
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
