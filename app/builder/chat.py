"""The builder chat: what the owner says is queued as a turn (ask), and the builder (build.py, Keel v2) does it in the
background (work). Here too is what a turn and the pages around it share: a design saved as a new version and going
back to an earlier one, the conversation so far, what an attached file holds, records the owner gave being added, a
reply put in plain words, and telling when the owner is asking for what isn't right to be fixed.
"""
import logging
import re
from datetime import datetime

from .. import ai
from ..blueprint import schema
from ..engine.records import Engine, FieldErrors
from ..extensions import db
from ..models import Message, Version, utcnow
from . import checking, training


_log = logging.getLogger(__name__)


AI_RETRY_SECONDS = 20


HISTORY = 8


def ask(workspace, text, page=None, user=None, files=None):
    """Queue a turn. Returns the assistant message the chat will watch."""
    db.session.add(Message(workspace_id=workspace.id, channel="builder", role="user", text=text.strip()[:8000], page=page,
                           user_id=user.id if user else None, files=list(files or [])))
    reply = Message(workspace_id=workspace.id, channel="builder", role="assistant", status="queued", page=page, progress=[],
                    user_id=user.id if user else None)
    db.session.add(reply)
    db.session.commit()
    return reply


def save_version(workspace, blueprint, changes, source, message_id=None, by=None):
    """A new version of the design. What people entered is carried across it (engine/carry.py), and what that did is
    added to `changes` (the list given, so the caller's list says it too)."""
    number = (workspace.version or 0) + 1
    from ..engine import carry
    from ..models import User
    who = db.session.get(User, by).display if isinstance(by, int) and db.session.get(User, by) else (by if isinstance(by, str) else None)
    before = workspace.blueprint or {}
    workspace.blueprint, workspace.version = blueprint, number
    db.session.flush()
    try:
        kept = carry.keep(workspace, before, blueprint) + carry.repriced(workspace, before, blueprint) if before.get("tables") else []
    except Exception:  # keeping values is a help: it never stops a design being saved (an undo brings them back)
        _log.exception("Keeping records' values through version %s of workspace %s failed", number, workspace.id)
        db.session.rollback()
        workspace.blueprint, workspace.version = blueprint, number
        kept = []
    from .. import studio  # a choice renamed in place: the records that had it follow (and back again, on going back)
    renames = studio.renamed_choices(before, blueprint)
    for table, key, old, new in renames:
        n = studio.rename_records(workspace, table, key, old, new)
        if n:
            kept.append(f"{n} record{'s' if n != 1 else ''} now say “{new}”")
    from ..engine import combined
    for t in combined.changed(before, blueprint):  # a combined list new or put together differently: made again
        try:
            n = combined.rebuild(Engine(workspace, by="builder"), t)
            kept.append(f"Put together {t['label']} from {', '.join(combined.sources(blueprint, t))}: {n} record{'s' if n != 1 else ''}")
        except Exception:  # its records come with the next change to the lists it's made from
            _log.exception("Putting together %s in workspace %s failed", t["key"], workspace.id)
            db.session.rollback()
            workspace.blueprint, workspace.version = blueprint, number
    try:  # what the AI has read of the records, in the design's words as they are now
        from .. import knowledge
        knowledge.redesigned(workspace, before, blueprint)
    except Exception:  # never a reason a design isn't saved: the records are read again as they change
        _log.exception("Marking records to read again after version %s of workspace %s failed", number, workspace.id)
        db.session.rollback()
        workspace.blueprint, workspace.version = blueprint, number
    if source != "restore":
        studio.remember_renames(workspace, renames)
    if isinstance(changes, list):
        changes.extend(kept)
    db.session.add(Version(workspace_id=workspace.id, number=number, blueprint=blueprint, changes=list(changes or []) + ([] if isinstance(changes, list) else kept),
                           source=source, message_id=message_id, by=who))
    db.session.commit()
    from .. import audit
    said = list(changes or [])
    audit.log(workspace.id, "design", f"version {number} ({source}): " + "; ".join(str(c) for c in said[:6]) + (f"; and {len(said) - 6} more" if len(said) > 6 else ""),
              by=who or ("the builder" if source == "chat" else None))
    if source != "chat":  # the builder tries its own changes out; any other change (an undo, an edit by hand) is checked after
        checking.soon(workspace, f"the change “{(changes or ['a change'])[0]}”")
    engine = Engine(workspace, by="builder")
    for t in blueprint.get("tables", []):
        engine.number_missing(t["key"])
    db.session.commit()
    return number


def restore(workspace, number, by=None, why=None):
    """Go back to an earlier design, as a new version (so going back can itself be undone)."""
    old = Version.query.filter_by(workspace_id=workspace.id, number=number).first()
    if old is None:
        raise ValueError("That version doesn't exist.")
    from .. import studio
    said = [why or f"Went back to the design as it was at version {number}"]
    renamed = studio.rename_back(workspace, number)  # choices renamed since: the records named as they were then
    if renamed:
        said.append(f"{renamed} record{'s' if renamed != 1 else ''} named as {'they were' if renamed != 1 else 'it was'} then")
    return save_version(workspace, old.blueprint, said, "restore", by=by)


def _history(workspace, before_id):
    """The conversation so far, with what was changed outside it in between (in a studio, an undo, ready-made parts,
    a test copy published), so the AI knows those were meant and builds on them rather than putting them back."""
    turns = list(reversed(Message.query.filter(Message.workspace_id == workspace.id, Message.channel == "builder", Message.id < before_id)
                          .order_by(Message.id.desc()).limit(HISTORY).all()))
    since = turns[0].created_at if turns else None
    elsewhere = (Version.query.filter(Version.workspace_id == workspace.id, Version.source.notin_(["chat", "start"]),
                                      *([Version.created_at >= since] if since else []))
                 .order_by(Version.number).limit(40).all())
    said = {"studio": "changed by hand on the App map", "manual": "changed by hand", "restore": "undid a change",
            "parts": "added ready-made parts", "published": "published from the test copy", "import": "brought in from a file"}
    items = [(m.created_at, 0, m) for m in turns] + [(v.created_at, 1, v) for v in elsewhere]
    out = []
    for _, kind, x in sorted(items, key=lambda item: (item[0] or datetime.min, item[1])):
        if kind == 1:
            out.append({"role": "user", "content": f"(Meanwhile the owner {said.get(x.source, 'changed the design')} (version {x.number}): "
                                                   + "; ".join(str(c) for c in (x.changes or [])[:8]) + ". Keep it.)"})
        elif x.role == "user":
            out.append({"role": "user", "content": x.text})
        elif x.status == "done":
            summary = x.text or ""
            if x.changes:
                summary += "\n(Changes made: " + "; ".join(x.changes) + ")"
            out.append({"role": "assistant", "content": summary})
    return out


GUESS = {True: (15, 45), False: (1, 6)}  # minutes, before this computer has done any: a whole business, a change


def typical_minutes(first):
    """How long a builder turn takes on this computer, from the last ones it did (a whole business, or a change): the
    middle half of them, in minutes, so what the screen promises is what people get."""
    turns = (Message.query.filter(Message.channel == "builder", Message.role == "assistant", Message.status == "done",
                                  Message.started_at.isnot(None), Message.finished_at.isnot(None))
             .order_by(Message.id.desc()).limit(60).all())
    took = sorted((m.finished_at - m.started_at).total_seconds() / 60 for m in turns
                  if bool((m.version_before or 0) == 0) == first and m.changes)[:30]
    if len(took) < 3:
        return GUESS[first]
    low, high = took[len(took) // 4], took[(len(took) * 3) // 4]
    return max(1, round(low)), max(round(low) + 1, round(high + 0.49))


def navigate_url(workspace, target):
    """'table:clients', 'table:clients:open', 'table:clients:record:7', 'page:overview', 'automations' -> a path."""
    if not target:
        return None
    if target.startswith("/") and not target.startswith("//"):  # a page of this site, as it is ("/w/3/app/checks")
        return target
    parts, base = target.split(":"), f"/w/{workspace.id}/app"
    if parts[0] == "table":
        if len(parts) >= 4 and parts[2] == "record":
            return f"{base}/t/{parts[1]}/{parts[3]}"
        return f"{base}/t/{parts[1]}" + (f"?view={parts[2]}" if len(parts) > 2 else "")
    if parts[0] == "page":
        return f"{base}/p/{parts[1]}"
    if parts[0] in ("automations", "improvements", "approvals", "checks"):
        return f"{base}/{parts[0]}"
    return None


BUILDER_BY = "builder chat"  # who the records the builder adds (starting and sample records) are by


def unmade(workspace, message):
    """When a builder turn is undone, the records it added go in the bin too (its recipe lines doubled a price; the
    design going back left them), unless someone has changed them or linked to them since. Returns (binned, kept)."""
    from ..models import Record, RecordChange
    if message.started_at is None:
        return [], []
    rows = Record.query.filter(Record.workspace_id == workspace.id, Record.created_by == BUILDER_BY, Record.deleted_at.is_(None),
                               Record.created_at >= message.started_at, Record.created_at <= (message.finished_at or utcnow())).all()
    engine, binned, kept = Engine(workspace, by="Undo"), [], []
    made = {r.id for r in rows}
    for r in rows:
        t = engine.tables.get(r.table)
        changed = RecordChange.query.filter(RecordChange.record_id == r.id, RecordChange.kind == "updated",
                                            RecordChange.by != BUILDER_BY).first() is not None
        if t is None or changed or _linked_to(engine, r, made):
            kept.append(engine.title(r))
            continue
        binned.append(f"{t['singular'].lower()} “{engine.title(r)}”")
        engine.delete(r, automations=False)
    return binned, kept


def _linked_to(engine, record, made):
    """Whether a record other than those in `made` links to this one (an order for a product the builder added)."""
    for other in engine.tables.values():
        for f in other["fields"]:
            if f["type"] != "link" or f["table"] != record.table:
                continue
            for x in engine.rows(other["key"]):
                value = (x.data or {}).get(f["key"])
                if x.id not in made and record.id in (value if isinstance(value, list) else [value]):
                    return True
    return False


def _already(engine, t, values):
    """The record the AI asks to add when it's there already: the same links (a dining table's legs in its recipe) with
    nothing else different, or, with no links, the same name. Notes and other words aside, it's the same thing."""
    links, rest = {}, {}
    for k, v in values.items():
        f = schema.field(t, k)
        if f is None or v in (None, "", []):
            continue
        if f["type"] == "link":
            found = engine._find_linked(f["table"], v)
            if found is None:
                return None
            links[k] = found.id
        elif f["type"] not in ("text", "long_text", "rich_text") or k == schema.title_field(t):
            rest[k] = (f, v)

    def same(r, f, v):
        have = (r.data or {}).get(f["key"])
        if have in (None, "", []):
            return True
        try:
            return engine.coerce(f, v) == have
        except ValueError:
            return False
    if links:
        return next((r for r in engine.rows(t["key"]) if all(str((r.data or {}).get(k)) == str(i) for k, i in links.items())
                     and all(same(r, f, v) for f, v in rest.values())), None)
    key = schema.title_field(t)
    name = str(values.get(key) or "").strip().lower() if key else ""
    return next((r for r in engine.rows(t["key"]) if str((r.data or {}).get(key) or "").strip().lower() == name), None) if name else None


def _data(workspace, items, by=None):
    """Records the AI was asked to add, files to import, and searches it ran. Returns (sentences, results to show)."""
    engine = Engine(workspace, by=BUILDER_BY)
    said, shown = [], None
    for item in items:
        t = engine.tables[item["table"]]
        if item["op"] == "import_file":
            from .. import importer
            job = importer.start(workspace, item["file_id"], item["table"], item.get("mapping"), item.get("sheet"), by=by)
            said.append(f"Importing {job.payload.get('rows', 'the')} rows into {t['label']} in the background"
                        + (f" (not brought in: {', '.join(item['skipped'])})" if item.get("skipped") else ""))
            continue
        if item["op"] == "add_records":
            added, failed = 0, []
            dropped, there = [], []
            for row in item["records"]:
                values = {schema.slug(k): v for k, v in row.items() if schema.slug(k) != "id"}
                same = _already(engine, t, values)
                if same is not None:  # asked again for what's there (a recipe's legs a second time would double its price)
                    there.append(engine.title(same))
                    continue
                try:
                    engine.create(item["table"], values, automations=False)
                    added += 1
                except FieldErrors as exc:
                    # keep the record without the values that don't fit, unless one of them is required
                    required = {f["key"] for f in t["fields"] if f.get("required")}
                    if set(exc.errors) & required or "_" in exc.errors:
                        failed.append("; ".join(exc.errors.values()))
                        continue
                    try:
                        engine.create(item["table"], {k: v for k, v in values.items() if k not in exc.errors}, automations=False)
                        added += 1
                        dropped += [f"{schema.field(t, k)['label']} ({v})" for k, v in exc.errors.items()]
                    except FieldErrors as again:
                        failed.append("; ".join(again.errors.values()))
            said.append(f"Added {added} {schema.lc(t['label']) if added != 1 else schema.lc(t['singular'])}"
                        + (f" ({len(failed)} couldn't be added: {failed[0]})" if failed else "")
                        + (f"; {len(there)} {'was' if len(there) == 1 else 'were'} already there, so not added again "
                           f"({', '.join(there[:3])})" if there else "")
                        + (f"; left out values that didn't fit: {'; '.join(dropped[:3])}" if dropped else ""))
        elif item["op"] == "find":
            rows = engine.query(item["table"], item["filters"], sort=item.get("sort"))
            from ..engine.automations import describe_condition
            where = " and ".join(describe_condition(engine, c, t["key"]) for c in item["filters"])
            noun = t["label"].lower() if len(rows) != 1 else t["singular"].lower()
            said.append(f"Found {len(rows)} {noun}" + (f" where {where}" if where else ""))
            columns = [c for c in item.get("columns") or [] if schema.field(t, c)] or \
                      next((v.get("columns") for v in t["views"] if v["type"] == "list" and v.get("columns")), [f["key"] for f in t["fields"]])[:5]
            shown = {"table": t["key"], "label": t["label"], "total": len(rows),
                     "columns": [schema.field(t, c)["label"] for c in columns],
                     "rows": [{"id": r.id, "cells": [engine.display(schema.field(t, c), engine.value(r, c)) for c in columns]}
                              for r in rows[:item["limit"]]]}
    return said, shown


# the app's words for what builders call tables, fields, automations and workflows (the menu says Rules, lists, details)
OURS = {"table": "list", "tables": "lists", "field": "detail", "fields": "details", "automation": "rule", "automations": "rules",
        "workflow": "process", "workflows": "processes", "erp": "app", "trigger": "set off", "triggers": "sets off",
        "triggered": "set off", "triggering": "setting off"}


JARGON = re.compile(r"\b(" + "|".join(OURS) + r")\b", re.I)


def plain(text, bp=None, said=""):
    """A reply with no code in it: {{record.title}} → [title], ['Confirmed', 'Prepping'] → Confirmed or Prepping, a list's
    or detail's code name (bid_lines, approved_by) → its name (bid lines, approved by), and builders' words in the app's
    (tables → lists, automations → rules), unless the owner used the word (a furniture maker's tables) or the app does."""
    text = re.sub(r"\{\{\s*(?:record\.)?([\w.]+)\s*\}\}", lambda m: "[" + m.group(1).replace("_", " ").replace(".", "’s ") + "]", text or "")
    text = re.sub(r"\[((?:'[^'\]]*'(?:,\s*)?)+)\]", lambda m: " or ".join(re.findall(r"'([^']*)'", m.group(1))), text)
    if bp:
        text = schema.named(bp, text)
        theirs = " ".join([said or ""] + [t["label"] for t in bp.get("tables", [])]
                          + [str(x) for t in bp.get("tables", []) for f in t["fields"] for x in [f["label"], *(f.get("options") or [])]]).lower()
        kept = {w for w in OURS if re.search(rf"\b{w}s?\b", theirs)}

        def ours(m):
            word = m.group(0)
            if word.lower() in kept or word.lower().rstrip("s") in kept:
                return word
            new = OURS[word.lower()]
            starts = m.start() == 0 or re.search(r"[.!?:]\s+$|\n\s*$", m.string[:m.start()])  # a sentence's first word
            return new.capitalize() if starts else new
        text = JARGON.sub(ours, text)
    return re.sub(r"\$\s*\$", "$", text)


def describe_files(file_ids, workspace_id):
    """What the AI is told about files attached to a message: spreadsheets' columns and first rows, documents' text."""
    from ..files import readers
    from ..models import File
    from ..storage import path_of
    parts = []
    for f in File.query.filter(File.id.in_(file_ids or []), File.workspace_id == workspace_id).all():
        content = readers.read(path_of(f), name=f.name, max_rows=15, max_chars=6000)
        head = f"File id {f.id}: {f.name}"
        if content.error:
            parts.append(f"{head} (couldn't be read: {content.error})")
        elif content.sheets:
            for sheet in content.sheets[:5]:
                rows = "\n".join(" | ".join("" if v is None else str(v) for v in row) for row in sheet.rows[:15])
                worked = "".join(f"\n  {k} = {v}" for k, v in sheet.formulas.items())
                parts.append(f"{head}, sheet “{sheet.name}” ({sheet.row_count} rows). Columns: {' | '.join(sheet.headers)}\n{rows}"
                             + (f"\nWorked out by the sheet's own formulas (calculated in the app, never imported or typed):{worked}"
                                if worked else ""))
        else:
            parts.append(f"{head} ({content.kind}):\n{content.text[:6000]}")
    return "\n\n".join(parts)


def _not_yet(wrong, issues):
    """What still doesn't work, in the owner's words: the everyday cases that didn't come out right, by their names (the
    numbers behind each are shown under the reply), then anything else trying it out found."""
    said = [f"“{x['title'].rstrip('.')}”" for x in wrong[:3]] + [i.rstrip(".") for i in issues[:max(0, 3 - len(wrong))]]
    more = len(wrong) + len(issues) - len(said)
    return (f"Not everything works yet. When I tried it out, {'this' if len(said) + more == 1 else 'these'} didn't come out right: "
            + "; ".join(said) + (f", and {more} more" if more > 0 else "") + ". The details are below. Press “Fix these” and "
            "I'll have another go, or tell me what should happen.")


FIX_IT = re.compile(r"\b(fix|sort out|sort|correct|repair|mend)\b\s*(it|them|these|those|this|that|everything|all\b|what\b|the "
                    r"(problems?|errors?|issues?|checks?|things?|rest)\b|up\b|please\b|now\b|for me\b|[.!?]|$)"
                    r"|\bmake (it|them|these|this|everything) work\b|\b(isn'?t|aren'?t|not) (right|working)\b", re.I)


YES = re.compile(r"\b(yes|yeah|yep|ok|okay|sure|go ahead|do it|do that|please|fix|sort|thanks)\b", re.I)


def _mending(workspace, question):
    """When the owner asks for what isn't right to be fixed ("fix it", "Ask the builder to fix them" on the Checks page,
    "yes please" to "Not everything works yet"), or names a check that fails: {"all": every failing check is theirs to
    raise, "titles": the checks named, "found": checking.not_right, "text": it said for the builder}. None otherwise."""
    if question is None or not (workspace.blueprint or {}).get("tables"):
        return None
    found = checking.not_right(workspace)
    if not (found["failing"] or found["issues"] or found["missing"]):
        return None
    text = question.text or ""
    named = {x["title"] for x in found["failing"] if x["title"] in text} | {x["said"] for x in found["missing"] if x["said"] in text}
    before = Message.query.filter(Message.workspace_id == workspace.id, Message.channel == "builder", Message.id < question.id,
                                  Message.role == "assistant").order_by(Message.id.desc()).first()
    answering = before is not None and (before.text or "").startswith("Not everything works yet")  # what it said doesn't work, answered
    asked = bool((FIX_IT.search(text) and len(text.split()) <= 25) or text.startswith("Please fix what isn't right"))
    every = asked or answering
    if not (every or named):
        return None
    if not every:
        found = {"failing": [x for x in found["failing"] if x["title"] in named], "issues": [],
                 "missing": [x for x in found["missing"] if x["said"] in named]}
    lines = [f"- the check “{x['title']}” doesn't come out right: {x['why']}" for x in found["failing"][:8]] + \
            [f"- trying the app out found: {i}" for i in found["issues"][:6]] + \
            [f"- the owner's check “{x['said']}” isn't something the app does yet" + (f" ({x['why']})" if x["why"] else "") for x in found["missing"][:5]]
    said = ("\n\nWhat isn't right in the app now (from trying it out in a copy with its checks), for you to put right:\n" + "\n".join(lines)
            + "\nThe owner pressed “Fix” to have these put right, so make the changes now: add what each needs (a stage, a "
              "move, a rule, a field) or correct what's wrong. Don't ask whether to, and never change a check of the owner's to "
              "expect less. Where a number or a name only the owner knows is needed, use a sensible one and say which in the "
              "reply, so they can change it.")
    # only answering (no “fix it”, no check named): it may be about what isn't right, or something new (“tell me when
    # one is paid”); the builder reads which before taking any of it on
    return {"all": every, "titles": [x["title"] for x in found["failing"]], "found": found, "text": said,
            "maybe": answering and not asked and not named}


def work(message_id):
    """Do one queued builder turn (the background worker calls this): the Keel v2 builder (build.py), its calls kept
    for training when the turn is done."""
    from . import build
    calls = []
    token, steady, tries = ai.recording.set(calls), ai.building.set(True), ai.again.set(0)
    try:
        build.work(message_id)
    finally:
        ai.recording.reset(token)
        ai.building.reset(steady)
        ai.again.reset(tries)
    message = db.session.get(Message, message_id)
    if message is not None and message.status == "done":
        training.keep(message, calls)
