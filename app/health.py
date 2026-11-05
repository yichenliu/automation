"""How the app is running, for whoever looks after it (Settings › Health): the local AI, the background work, rules that
stopped, the screens' speed and errors, storage and backups. Each comes with what to do about it, in plain words.

Every request's time is kept in memory (the last few thousand since the app started), grouped by screen."""
import shutil
import time
from collections import deque
from datetime import timedelta
from pathlib import Path

from .extensions import db
from .core.settings import settings
from .webkit import g, request

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


WORDS = {"t": "a list", "app": "", "studio": "App map", "data": "What each list keeps", "workflows": "Processes", "map.json": "", "design": "Versions", "approvals": "To do",
         "automations": "Rules", "ai": "AI helpers", "knowledge": "What the AI knows", "settings": "Settings", "new": "a new one", "edit": "changing one",
         "search": "Search", "jump": "Jump to", "login": "Signing in", "logout": "Signing out", "flows": "Step by step", "p": "a page", "build": "Change my app", "api": "API"}


def screen_name(rule):
    """A screen as people know it, from its address pattern: "/w/<int:wid>/app/t/<table>/<int:rid>" is "a list › one of them"."""
    parts = [x for x in str(rule).split("/") if x and x not in ("w", "<int:wid>")]
    words = []
    for x in parts:
        if x.startswith("<"):
            words.append("one of them" if "int" in x and words else "")
        else:
            words.append(WORDS.get(x, x.replace("_", " ").replace("-", " ").capitalize()))
    named = " › ".join(w for w in words if w)
    return named[:1].upper() + named[1:] if named else "Home"


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
    slowest = sorted(((screen_name(rule), sorted(v)[len(v) // 2], len(v)) for rule, v in by.items()), key=lambda x: -x[1])[:5]
    return {"count": len(recent), "errors": sum(1 for x in recent if x[2] >= 500), "typical": times[len(times) // 2],
            "slow": times[int(len(times) * 0.95)] if len(times) > 1 else times[0], "slowest": slowest}


def report(ws):
    """Each part of the app's health: {"name", "ok": True | False | None (just so), "said", "do"}."""
    from . import ai, backup
    from .models import Job, Run, utcnow
    out = []
    described = ai.describe()
    try:
        ai.check()
        out.append({"name": "The AI", "ok": True, "said": f"{described['model']} is answering {described['where']}"})
    except ai.AIUnavailable as exc:
        out.append({"name": "The AI", "ok": False, "said": str(exc),
                    "do": ("Start Ollama (or restart the computer it runs on)." if described["provider"] == "ollama" else
                           f"Start the model server at {described['url']} (vLLM or SGLang), or check AI_BASE_URL.")
                          + " Work that needs the AI waits and carries on by itself."})
    out.extend(_asked(ai.stats()))
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
    data = Path(settings()["DATA_DIR"])
    from .blueprint import schema  # every part of the design fits the rest (made before a newer check, it may not)
    problems = schema.check(ws.blueprint or {})
    out.append({"name": "Design", "ok": not problems,
                "said": "every list, rule, flow, page and menu fits the rest" if not problems else
                        f"{len(problems)} thing{'s' if len(problems) != 1 else ''} in how your app is set up don't fit together: {problems[0]}",
                "do": "" if not problems else "Ask the AI to fix it (Change my app), or go back to an earlier version under Versions."})
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
                      "Backups are made every night while the app runs; make one now under Settings › Data and backups, and keep a copy off this computer."})
    return out


def _asked(stats):
    """What this process has asked the AI since it started (app/ai.py counts it): how many, how many went wrong, how long
    an answer takes and how long a question waits for its turn. Nothing until it's been asked."""
    calls = [c for c in stats["calls"] if c["purpose"] != "embed"]
    asked = sum(c["calls"] for c in calls)
    if not asked:
        return []
    failed, answered = sum(c["failures"] for c in calls), sum(c["answered"] for c in calls)
    took = sum(c["seconds"] for c in calls) / answered if answered else 0
    waited = sum(c["waited"] for c in calls) / asked
    tokens_in, tokens_out = sum(c["tokens_in"] for c in calls), sum(c["tokens_out"] for c in calls)
    return [{"name": "Asking the AI", "ok": failed <= asked * 0.1 and waited < 60,
             "said": f"{asked} asked since the app started, {failed} went wrong; an answer takes {took:.0f} s on average, "
                     f"after {waited:.0f} s waiting for a turn" + (f"; {tokens_in:,} tokens read, {tokens_out:,} written" if tokens_in or tokens_out else ""),
             "do": "Many questions to the AI went wrong: the log in data/logs says why." if failed > asked * 0.1 else
                   "Questions wait long for the AI: raise AI_CONCURRENCY if its server has room, or give the builder a server of "
                   "its own (AI_BASE_URL_BUILDER)." if waited >= 60 else ""}]


def _ago(delta):
    minutes = int(delta.total_seconds() // 60)
    return f"{minutes} min" if minutes < 120 else f"{minutes // 60} hours" if minutes < 48 * 60 else f"{minutes // 1440} days"


def _size(n):
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
