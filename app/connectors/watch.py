"""Watching Microsoft 365: new emails in the inbox and new files in OneDrive folders start automations, and (when the
owner has turned it on) new mail is read into what the AI can look up.

Called by the worker every couple of minutes for each workspace with a connected mailbox. Only what arrives after a
trigger exists starts it: on first look everything already there is marked as seen.
"""
import logging

from .. import knowledge
from ..engine import automations
from ..engine.records import Engine
from ..extensions import db
from ..models import utcnow
from . import microsoft

log = logging.getLogger("watch")


def _matches(trigger, email):
    def has(needle, haystack):
        return not needle or needle.lower() in (haystack or "").lower()
    return (has(trigger.get("from_contains"), f"{email['from']} {email['from_name']}")
            and has(trigger.get("subject_contains"), email["subject"])
            and has(trigger.get("body_contains"), email["body"])
            and (not trigger.get("has_attachment") or email["has_attachments"]))


def check(workspace):
    """Look for new mail and files and start the automations they trigger. Returns how many runs started."""
    wanted = [a for a in (workspace.blueprint or {}).get("automations", [])
              if a.get("enabled", True) and a["trigger"]["type"] in ("email_received", "file_added")]
    learn = bool(((workspace.settings or {}).get("knowledge") or {}).get("mail"))
    conn = microsoft.connection(workspace)
    if not (wanted or learn) or conn is None or conn.status != "connected":
        return 0
    engine = Engine(workspace, by="Microsoft 365")
    state = dict(conn.state or {})
    started = 0
    mail_rules = [a for a in wanted if a["trigger"]["type"] == "email_received"]
    if mail_rules or learn:
        since = state.get("mail_since") or utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        seen = set(state.get("mail_seen") or [])
        for email in microsoft.new_messages(workspace, since):
            if email["id"] in seen:
                continue
            seen.add(email["id"])
            since = max(since, email["received"] or since)
            if learn:
                knowledge.touch(workspace.id, "email", email["id"], data=knowledge.mail_item(email))
            rules = [a for a in mail_rules if _matches(a["trigger"], email)]
            if not rules:
                continue
            files = microsoft.save_attachments(workspace, email["id"]) if email["has_attachments"] else []
            item = {**email, "file_ids": [f.id for f in files], "attachments": ", ".join(f.name for f in files)}
            for a in rules:
                automations.start_from(engine, a, "email", item, f"Email from {email['from']}: “{email['subject'][:120]}”")
                started += 1
        state["mail_since"], state["mail_seen"] = since, sorted(seen)[-300:]
    folders = dict(state.get("folders") or {})
    for a in (x for x in wanted if x["trigger"]["type"] == "file_added"):
        folder = a["trigger"]["folder"]
        try:
            items = [i for i in microsoft.list_folder(workspace, folder) if not i["folder"]]
        except microsoft.MicrosoftProblem as exc:
            log.warning("Watching %s failed: %s", folder, exc)
            continue
        known = folders.get(folder)
        if known is None:  # first look: what's there already doesn't count
            folders[folder] = [i["id"] for i in items]
            continue
        for item in items:
            if item["id"] in known:
                continue
            known.append(item["id"])
            file = microsoft.download(workspace, item["id"], item["name"])
            automations.start_from(engine, a, "file", {**item, "file_ids": [file.id], "folder": folder},
                                   f"New file in {folder}: {item['name']}")
            started += 1
        folders[folder] = known[-1000:]
    state["folders"] = folders
    conn.state, conn.checked_at = state, utcnow()
    db.session.commit()
    return started
