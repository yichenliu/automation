"""Files: uploads, generated documents, attachments from email and OneDrive. Each is described by a File row; names
are cleaned and contents hashed. Where the bytes are kept is the store's: the data folder (FILES_BACKEND=local, one
machine), or S3-compatible object storage (FILES_BACKEND=s3: AWS S3, MinIO, R2…, so any number of web and worker
machines share them). Whatever reads a file asks for path_of(file): with object storage it's fetched once into a local
cache named by its contents' hash, so it never goes stale and every reader works unchanged."""
import hashlib
import mimetypes
import re
import uuid
from pathlib import Path


from .extensions import db
from .models import File, utcnow
from .core.settings import settings

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
    limit = settings()["MAX_UPLOAD_MB"] * 1024 * 1024
    if len(content) > limit:
        raise FileRejected(f"“{name}” is larger than {settings()['MAX_UPLOAD_MB']} MB.")
    if not content:
        raise FileRejected(f"“{name}” is empty.")
    now = utcnow()
    relative = Path("files") / str(workspace_id) / f"{now:%Y}" / f"{now:%m}" / f"{uuid.uuid4().hex[:12]}_{name}"
    backend().put(str(relative), content, mimetypes.guess_type(name)[0] or "application/octet-stream")
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
    """A path on this machine with the file's contents (fetched from object storage the first time it's needed)."""
    return backend().path(file.path, file.sha256)


class Local:
    """Files under the data folder."""

    def __init__(self, root):
        self.root = Path(root)

    def put(self, key, content, mime):
        target = self.root / key
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    def path(self, key, sha256=None):
        return self.root / key

    def delete(self, key):
        (self.root / key).unlink(missing_ok=True)


class ObjectStore:
    """Files in an S3-compatible bucket, read through a local cache named by each file's hash."""

    def __init__(self, cfg, client=None):
        self.bucket, self.prefix = cfg["S3_BUCKET"], (cfg.get("S3_PREFIX") or "").strip("/")
        self.cache = Path(cfg["DATA_DIR"]) / "cache" / "files"
        if client is None:
            try:
                import boto3
            except ImportError as exc:  # pragma: no cover - a deployment choice, said plainly
                raise RuntimeError("FILES_BACKEND=s3 needs boto3: pip install boto3") from exc
            client = boto3.client("s3", endpoint_url=cfg.get("S3_ENDPOINT") or None, region_name=cfg.get("S3_REGION") or None,
                                  aws_access_key_id=cfg.get("S3_ACCESS_KEY") or None, aws_secret_access_key=cfg.get("S3_SECRET_KEY") or None)
        self.client = client

    def _key(self, key):
        return f"{self.prefix}/{key}" if self.prefix else key

    def put(self, key, content, mime):
        self.client.put_object(Bucket=self.bucket, Key=self._key(key), Body=content, ContentType=mime)

    def path(self, key, sha256=None):
        name = sha256 or hashlib.sha256(key.encode()).hexdigest()
        target = self.cache / name[:2] / f"{name}{Path(key).suffix}"
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            body = self.client.get_object(Bucket=self.bucket, Key=self._key(key))["Body"].read()
            partial = target.with_suffix(target.suffix + ".part")
            partial.write_bytes(body)
            partial.replace(target)  # whole or not at all, even with several readers at once
        return target

    def delete(self, key):
        self.client.delete_object(Bucket=self.bucket, Key=self._key(key))


_stores = {}


def backend():
    cfg = settings()
    kind = (cfg.get("FILES_BACKEND") or "local").lower()
    if kind == "s3":
        found = _stores.get(("s3", cfg.get("S3_BUCKET"), cfg.get("S3_PREFIX"), cfg.get("DATA_DIR")))
        if found is None:
            found = _stores[("s3", cfg.get("S3_BUCKET"), cfg.get("S3_PREFIX"), cfg.get("DATA_DIR"))] = ObjectStore(cfg)
        return found
    return Local(Path(cfg["DATA_DIR"]))


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
