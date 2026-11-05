"""Emails and text messages the rules send: an outbox the worker delivers from.

A rule's "send an email" step works the message out as it always has (who to, the subject, the words, the PDFs) and
writes it here, in the same save as the step itself. The step is then done: the rest of the rule, and the record's
stage, carry on at once instead of waiting on the mailbox. The worker (`work()`) sends it; if the mailbox can't be
reached it tries again a little later each time (BACKOFF seconds, doubled each time, up to TRIES tries), and what
happened is written on the run's step, so the rule's history says "Emailed kim@…" or why it couldn't be.

Under TESTING, TRYING_OUT or SIMULATION it's sent there and then, through the same code, so tests and try-outs see what
they always did (a copy being tried reaches nothing outside: it says what it would have sent). Steps whose answer later
steps use (a webhook's) don't come here: they stay part of the run.

Delivery is at least once: a worker stopped between sending and saving that it sent leaves the message to be sent again
once its claim runs out (leases.py), never lost.
"""
import base64
import logging
from datetime import timedelta
from zoneinfo import ZoneInfo

from flask import current_app
from sqlalchemy import select

from . import leases
from .extensions import db
from .models import File, Outbox, Run, Workspace, utcnow

log = logging.getLogger("outbox")
TRIES = 6  # then it's given up on, and the people who run the app are told
BACKOFF = 60  # seconds before the second try; doubled for each one after
LONGEST = 3600  # the longest wait between tries
VERB = {"email": "email", "sms": "text"}


class OutboxProblem(Exception):
    """Why a message couldn't go, in words for the run's history."""


def at_once():
    """Whether messages are sent there and then (tests, try-outs, the simulator) instead of by the worker."""
    cfg = current_app.config
    return bool(cfg.get("TESTING") or cfg.get("TRYING_OUT") or cfg.get("SIMULATION"))


def _reaches_outside():
    cfg = current_app.config
    return not (cfg.get("TRYING_OUT") or cfg.get("SIMULATION"))


def add(workspace_id, run_id, channel, to, subject="", body="", cc="", attachments=()):
    """Put a message in the outbox (it's saved with whatever the caller saves next)."""
    item = Outbox(workspace_id=workspace_id, run_id=run_id, channel=channel, to=to, cc=cc or None, subject=subject, body=body,
                  attachments=list(attachments), status="queued", attempts=0, next_try=utcnow(), reported=False)
    db.session.add(item)
    db.session.flush()
    return item


def step(item):
    """What the rule's step says about its message: sent (or not) there and then, or on its way."""
    if at_once():
        deliver(item, now=True)
        return {"result": item.result, "outbox": item.id}
    return {"result": _words(item, "queued"), "outbox": item.id}


def _names(item):
    names = [a.get("name") for a in item.attachments or [] if a.get("name")]
    return f" with {', '.join(names)}" if names else ""


def _short(text, n=60):
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def _words(item, how, problem=None, when=None):
    """The step's result, in words: on its way, sent, would be sent (a try-out), or why it couldn't be."""
    verb = VERB.get(item.channel, "send")
    what = f"“{item.subject}”{_names(item)}" if item.channel == "email" else f"“{_short(item.body)}”"
    if how == "queued":
        return f"{'Email' if item.channel == 'email' else 'Text message'} to {item.to} on its way: {what}"
    if how == "sent":
        return f"{'Emailed' if item.channel == 'email' else 'Texted'} {item.to}: {what}"
    if how == "trying_out":
        return f"Would {verb} {item.to}: {what} (not sent while trying the app out)"
    if how == "retry":
        return f"Couldn't {verb} {item.to} yet ({problem}); trying again at {when}"
    return f"Couldn't {verb} {item.to}: {problem} (tried {item.attempts} times)"


def _files(item):
    """The attachments as (name, bytes, type): the record's own files, read now, or a report made when the rule ran."""
    from .storage import path_of
    out = []
    for a in item.attachments or []:
        if a.get("file"):
            file = db.session.get(File, a["file"])
            if file is None or not path_of(file).exists():
                raise OutboxProblem(f"the attachment “{a.get('name') or 'file'}” is no longer there")
            out.append((file.name, path_of(file).read_bytes(), file.mime))
        elif a.get("data") is not None:
            out.append((a.get("name") or "attachment", base64.b64decode(a["data"]), a.get("mime") or "application/octet-stream"))
    return out


def _send_email(ws, item):
    if not _reaches_outside():
        return _words(item, "trying_out")
    from .connectors import microsoft
    microsoft.send_mail(ws, item.to, item.subject or "", item.body or "", cc=item.cc or "", attachments=_files(item))
    return _words(item, "sent")


def _send_sms(ws, item):
    """A text message, through a connected texting service (a connected app that can send texts)."""
    if not _reaches_outside():
        return _words(item, "trying_out")
    from .connectors import apps
    for app in apps.all_apps():
        if callable(getattr(app, "send_sms", None)) and apps.connected(ws, app.key):
            app.send_sms(apps.creds(apps.connection(ws, app.key)), item.to, item.body or "")
            return _words(item, "sent")
    raise OutboxProblem("no texting service is connected (Settings › Connections)")


SENDERS = {"email": _send_email, "sms": _send_sms}


def _when(ws, at):
    try:
        tz = ZoneInfo((ws.timezone if ws is not None else None) or current_app.config["DEFAULT_TIMEZONE"])
    except Exception:  # noqa: BLE001 - an unknown time zone
        tz = ZoneInfo("UTC")
    local = at.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
    return local.strftime("%-I:%M %p").lower() if local.date() == utcnow().replace(tzinfo=ZoneInfo("UTC")).astimezone(tz).date() \
        else local.strftime("%-d %b, %-I:%M %p")


def deliver(item, now=False):
    """Send one message. `now`: sent there and then (a test or a try-out): a failure is the step's, as it always was.
    Otherwise a failure is tried again later, and after TRIES tries given up on, with the app's admins told."""
    ws = db.session.get(Workspace, item.workspace_id)
    sender = SENDERS.get(item.channel)
    try:
        if ws is None or sender is None:
            raise OutboxProblem("the app it was for is no longer here" if ws is None else f"there's no way to send “{item.channel}”")
        said = sender(ws, item)
    except Exception as exc:  # the mailbox not connected or not answering, an attachment gone, a fault of ours
        item.attempts = (item.attempts or 0) + 1
        item.error = (str(exc) or type(exc).__name__)[:2000]
        item.reported = now
        if now:
            item.status, item.result = "failed", _words(item, "failed", item.error)
            raise
        if item.attempts >= TRIES:
            item.status, item.result = "failed", _words(item, "failed", item.error)
            _tell_admins(ws, item)
        else:
            wait = min(LONGEST, BACKOFF * 2 ** (item.attempts - 1))
            item.status, item.next_try = "queued", utcnow() + timedelta(seconds=wait)
            item.result = _words(item, "retry", item.error, _when(ws, item.next_try))
        return item
    item.attempts = (item.attempts or 0) + 1
    item.status, item.result, item.sent_at, item.error = "sent", said, utcnow(), None
    item.attachments = [{k: v for k, v in a.items() if k != "data"} for a in item.attachments or []]  # sent: the copy isn't needed
    item.reported = now  # sent there and then: the step says so itself
    return item


def _tell_admins(ws, item):
    if ws is None:
        return
    from .engine import automations
    from .engine.records import Engine
    run = db.session.get(Run, item.run_id) if item.run_id else None
    rule = f"The rule “{run.name}”" if run is not None and run.name else "A rule"
    automations.notify(Engine(ws, by="the outbox"), f"{rule} couldn't {VERB.get(item.channel, 'send')} {item.to}: {item.error}",
                       "automations", "admin", kind="error")


def report(item):
    """Write what happened on the run's step ("Emailed kim@…"), so the rule's history shows it. A run being carried out
    right now is left alone (its own saves would write over this): it's done next time round. Returns whether it was."""
    if item.run_id:
        pick = select(Run).where(Run.id == item.run_id, leases.free(Run)).execution_options(populate_existing=True)
        if leases.postgres():
            pick = pick.with_for_update(skip_locked=True)
        run = db.session.execute(pick).scalars().first()
        if run is None and db.session.get(Run, item.run_id) is not None:
            db.session.rollback()
            return False
        if run is not None:
            steps = [dict(s) for s in run.steps or []]
            for s in steps:
                if s.get("outbox") == item.id:
                    s["result"] = item.result
            run.steps = steps
    item.reported = True
    db.session.commit()
    return True


def work(limit=20):
    """The worker's turn: send what's due (each claimed, so two workers never send the same one), then write what
    happened on the runs' steps. Returns whether anything was sent or tried."""
    done = 0
    while done < limit:
        item = leases.claim(Outbox, Outbox.status == "queued", Outbox.next_try <= utcnow(), values={"status": "sending"})
        if item is None:
            break
        done += 1
        item_id = item.id
        try:
            deliver(item)
        except Exception:  # noqa: BLE001 - a fault of ours: tried again later, like any other failure
            log.exception("Delivering message %s failed", item_id)
            db.session.rollback()
            item = db.session.get(Outbox, item_id)
            item.status, item.next_try = "queued", utcnow() + timedelta(seconds=BACKOFF)
        item.claimed_by = item.lease_until = None
        leases.untrack(Outbox, item_id)
        db.session.commit()
    for item in (Outbox.query.filter(Outbox.reported.is_(False), Outbox.attempts > 0, Outbox.status != "sending")
                 .order_by(Outbox.id).limit(50).all()):
        try:
            report(item)
        except Exception:  # noqa: BLE001 - the run's history waits; the message itself went (or didn't) as it says
            log.exception("Couldn't write message %s's result on its run", item.id)
            db.session.rollback()
    return bool(done)
