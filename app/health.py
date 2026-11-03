"""How the app is running, for whoever looks after it (Settings › Health): the local AI, the background work, rules that
stopped, the screens' speed and errors, storage and backups. Each comes with what to do about it, in plain words.

Every request's time is kept in memory (the last few thousand since the app started), grouped by screen."""
import shutil
import time
from collections import deque
from datetime import timedelta
from pathlib import Path

from flask import current_app, g, request

from .extensions import db

KEPT = 3000
_seen = deque(maxlen=KEPT)  # (when, screen, status, milliseconds)


def started():
    g.started_at = time.perf_counter()


def finished(response):
    began = g.get("started_at")
    rule = request.url_rule.rule if request.url_rule is not None else None
    if began is not None and rule and not rule.startswith("/static"):
        _seen.append((time.time(), rule, response.status_code, (time.perf_counter() - began) * 1000))
    return response


def screens(since_seconds=24 * 3600):
    """Requests since the app started (up to the last few thousand): how many, how many failed, how fast, the slowest."""
    now = time.time()
    recent = [x for x in list(_seen) if now - x[0] <= since_seconds]
    if not recent:
        return {"count": 0}
    times = sorted(x[3] for x in recent)
    by = {}
    for _, rule, status, ms in recent:
        by.setdefault(rule, []).append(ms)
    slowest = sorted(((rule, sorted(v)[len(v) // 2], len(v)) for rule, v in by.items()), key=lambda x: -x[1])[:5]
    return {"count": len(recent), "errors": sum(1 for x in recent if x[2] >= 500), "typical": times[len(times) // 2],
            "slow": times[int(len(times) * 0.95)] if len(times) > 1 else times[0], "slowest": slowest}


def report(ws):
    """Each part of the app's health: {"name", "ok": True | False | None (just so), "said", "do"}."""
    from . import ai, backup
    from .models import Job, Run, utcnow
    out = []
    try:
        ai.check()
        out.append({"name": "The AI", "ok": True, "said": f"{ai.describe()['model']} is answering on this computer"})
    except ai.AIUnavailable as exc:
        out.append({"name": "The AI", "ok": False, "said": str(exc),
                    "do": "Start Ollama (or restart the computer it runs on). Work that needs the AI waits and carries on by itself."})
    day = utcnow() - timedelta(days=1)
    waiting = Job.query.filter(Job.workspace_id == ws.id, Job.status.in_(["queued", "running", "waiting_ai"])).all()
    failed_jobs = Job.query.filter(Job.workspace_id == ws.id, Job.status == "failed", Job.created_at >= day).count()
    oldest = min((j.created_at for j in waiting), default=None)
    stuck = oldest is not None and utcnow() - oldest > timedelta(minutes=30)
    out.append({"name": "Background work", "ok": not stuck and not failed_jobs,
                "said": f"{len(waiting)} waiting" + (f", the oldest for {_ago(utcnow() - oldest)}" if oldest else "")
                        + f"; {failed_jobs} failed today",
                "do": "Background work isn't moving: the app's worker may have stopped (./start.sh status, then restart)." if stuck else
                      "Some background work failed today: the log in data/logs says why." if failed_jobs else ""})
    stopped = Run.query.filter(Run.workspace_id == ws.id, Run.status == "failed", Run.created_at >= day).all()
    out.append({"name": "Rules", "ok": not stopped,
                "said": f"{len(stopped)} stopped in the last day" + (f": {', '.join(sorted({r.name for r in stopped})[:3])}" if stopped else ""),
                "do": "Open Rules to see where each stopped, and ask the builder to fix it." if stopped else ""})
    seen = screens()
    if seen["count"]:
        bad = seen["errors"] / seen["count"]
        out.append({"name": "Screens", "ok": bad < 0.01 and seen["slow"] < 2000,
                    "said": f"{seen['count']} opened since the app started; usually {seen['typical']:.0f} ms, 1 in 20 over {seen['slow']:.0f} ms; "
                            f"{seen['errors']} went wrong",
                    "do": "Some screens went wrong: the log in data/logs says which and why." if seen["errors"] else
                          "Screens are slow: a big list may need a filtered view, or the computer may be busy." if seen["slow"] >= 2000 else "",
                    "slowest": seen["slowest"]})
    data = Path(current_app.config["DATA_DIR"])
    try:  # PostgreSQL, with pgvector for the AI's search by meaning
        from sqlalchemy import text as sql
        row = db.session.execute(sql("SELECT current_setting('server_version'), pg_database_size(current_database()), "
                                     "(SELECT extversion FROM pg_extension WHERE extname = 'vector'), "
                                     "(SELECT count(*) FROM pg_stat_activity WHERE datname = current_database())")).one()
        out.append({"name": "Database", "ok": bool(row[2]), "said": f"PostgreSQL {row[0].split()[0]} ({_size(row[1])}, {row[3]} connections)"
                    + (f" with pgvector {row[2]} for search by meaning" if row[2] else ""),
                    "do": "" if row[2] else "pgvector is missing: run ./start.sh setup, so the AI can find things by meaning."})
    except Exception as exc:  # noqa: BLE001 - said, never a reason the page can't show
        out.append({"name": "Database", "ok": False, "said": f"Couldn't be asked: {type(exc).__name__}", "do": "Run ./start.sh to start it."})
    free = shutil.disk_usage(data).free
    size = sum(f.stat().st_size for f in data.rglob("*") if f.is_file())
    out.append({"name": "Storage", "ok": free > 5 * 1024 ** 3, "said": f"the app's data is {_size(size)}; {_size(free)} free on this disk",
                "do": "The disk is nearly full: make room, or the app will stop saving." if free <= 5 * 1024 ** 3 else ""})
    last = backup.latest()
    age = timedelta(seconds=time.time() - last.stat().st_mtime) if last else None
    out.append({"name": "Backups", "ok": age is not None and age < timedelta(days=2),
                "said": f"the last was {_ago(age)} ago ({last.name})" if last else "none yet",
                "do": "" if age is not None and age < timedelta(days=2) else
                      "Backups are made every night while the app runs; make one now under Data & AI, and keep a copy off this computer."})
    return out


def _ago(delta):
    minutes = int(delta.total_seconds() // 60)
    return f"{minutes} min" if minutes < 120 else f"{minutes // 60} hours" if minutes < 48 * 60 else f"{minutes // 1440} days"


def _size(n):
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
