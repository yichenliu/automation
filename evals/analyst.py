"""Evaluate the AI analyst on real businesses with the real model (slow: minutes per turn on a local model).

    .venv/bin/python -m evals.analyst --out /tmp/analyst-eval                 every scenario
    .venv/bin/python -m evals.analyst --out /tmp/analyst-eval plumbing        some of them
    .venv/bin/python -m evals.analyst --out /tmp/analyst-eval --repeat 3      each three times (the model varies)
    .venv/bin/python -m evals.analyst --regrade /tmp/analyst-eval             score saved designs again, without the model
    .venv/bin/python -m evals.analyst --out /tmp/fresh --fresh 8              8 businesses never seen before (generic scores only)

Each scenario gets a fresh platform. The owner's messages are sent one turn at a time through the real builder
(chat.ask + chat.work, exactly as the app does), which tries each change out itself and fixes what that finds. After each
turn the design is scored (what it has) and the business's stories are acted out (evals/stories.py: does it do what they
asked, and does a later change break what already worked); after the last, the finished app is tried out in a copy (every
list, stage, button, rule, document and screen: what's wrong with it). Results go to <out>/results.jsonl and are printed
compactly, with totals at the end. The number that matters is right when handed over (handed_over).
"""
import argparse
import io
import json
import re
import shutil
import sys
import time
import traceback
from collections import Counter
from datetime import datetime
from pathlib import Path

from . import fresh, mapux, stories, updates, ux
from .scenarios import SCENARIOS


def _portal(bp):
    from app.blueprint import schema
    return bool(schema.portals(bp))


def _has_table(bp, words):
    for t in bp.get("tables", []):
        text = f"{t['key']} {t['label']} {t['singular']}".lower()
        if any(w in text for w in words):
            return True
    return False


def _fields(bp):
    return [(t, f) for t in bp.get("tables", []) for f in t["fields"]]


def _actions(bp):
    from app.blueprint import schema
    return [act for a in bp.get("automations", []) for act in schema.steps_of(a["actions"])]


def _steps_of(a):
    from app.blueprint import schema
    return list(schema.steps_of(a["actions"]))


def _field_of(bp, automation, act):
    from app.blueprint import schema
    t = schema.table(bp, automation["trigger"].get("table") or "")
    return schema.field(t, act.get("field")) if t else None


def _checked_reply(bp):
    """A reply the AI drafts is checked by a person before it goes: an ai_fill checked every time, in an automation
    that sends email (or one the person sends from the record)."""
    for a in bp.get("automations", []):
        steps = _steps_of(a)
        if any(x["type"] == "ai_fill" and x.get("review") == "always" for x in steps):
            return True
    return False


def _routes_on_ai(bp):
    """The process goes on from what an AI check found: a branch, an approval or another automation on its verdict."""
    verdicts = {act["field"] for act in _actions(bp) if act["type"] == "ai_check"}
    if not verdicts:
        return False
    from app.blueprint import schema
    for a in bp.get("automations", []):
        if any(c.get("field") in verdicts for c in schema.condition_leaves(a["conditions"])):
            return True
        for act in schema.steps_of(a["actions"]):
            conds = list(act.get("only_if") or []) + [c for path in act.get("paths") or [] for c in path["when"]]
            if any(c.get("field") in verdicts for c in schema.condition_leaves(conds)):
                return True
    return False


def _threshold_approval(bp):
    """An approval asked only when a condition holds: on a branch's way, or with its own "only_if"."""
    from app.blueprint import schema
    for t in bp.get("tables", []):
        if any(m.get("only_if") for m in (t.get("workflow") or {}).get("transitions", [])):
            return True
    for a in bp.get("automations", []):
        for act in schema.steps_of(a["actions"]):
            if act["type"] == "request_approval" and act.get("only_if"):
                return True
            if act["type"] == "branch" and any(x["type"] == "request_approval" for path in act["paths"] for x in schema.steps_of(path["steps"])):
                return True
    return False


def _rejection_path(bp):
    """A "no" sends the record somewhere: an approval's "if rejected" steps, or a workflow stage it can be moved on from."""
    if any(act["type"] == "request_approval" and act.get("if_rejected") for act in _actions(bp)):
        return True
    for t in bp.get("tables", []):
        moves = (t.get("workflow") or {}).get("transitions", [])
        rejected = {m["to"] for m in moves if "reject" in m["to"].lower() or "return" in m["to"].lower()}
        if rejected and any(set(m["from"]) & rejected for m in moves):
            return True
    return False


def _approver_from_limits(bp):
    """Approvals routed by a table of limits: a lookup that finds the approver, used by an approval or a workflow move."""
    finders = {f["key"] for _, f in _fields(bp) if f["type"] == "lookup" and "where" in f}
    return any(act.get("approver") in finders or set(act.get("approvers") or []) & finders for act in _actions(bp)) \
        or any(set(m.get("who") or []) & finders for t in bp.get("tables", []) for m in (t.get("workflow") or {}).get("transitions", []))


def _invoice_match(bp):
    """Invoices checked against their order (and receipt): a lookup or formula on the invoice (or its lines) reaching the
    order's price or quantity, or a rule comparing them."""
    for t in bp.get("tables", []):
        if "invoice" not in t["key"]:
            continue
        reaches = any(f["type"] == "lookup" and any(w in (f.get("link") or f.get("table") or "") for w in ("po", "order", "receipt"))
                      for f in t["fields"])
        compares = any(f["type"] == "formula" and any(w in f.get("expression", "") for w in ("po", "order", "receipt", "variance", "tolerance"))
                       for f in t["fields"]) or any(any(w in r["check"] for w in ("po", "order", "receipt", "tolerance")) for r in t.get("rules", []))
        if reaches or compares:
            return True
    return False

FEATURES = {
    "line_items": lambda bp: any(t.get("embedded") for t in bp.get("tables", [])),
    "money_totals": lambda bp: any(f["type"] == "rollup" and f.get("aggregate") == "sum" for _, f in _fields(bp))
                               and any(f["type"] == "formula" and f.get("format") == "money" for _, f in _fields(bp)),
    "rollup": lambda bp: any(f["type"] == "rollup" for _, f in _fields(bp)),
    "document": lambda bp: any(t.get("documents") for t in bp.get("tables", [])),
    "public_form": lambda bp: any((t.get("public_form") or {}).get("enabled", True) and t.get("public_form") for t in bp.get("tables", [])),
    "signature": lambda bp: any(f["type"] == "signature" for _, f in _fields(bp)),
    "location": lambda bp: any(f["type"] == "location" for _, f in _fields(bp)) or any(v["type"] == "map" for t in bp.get("tables", []) for v in t["views"]),
    "schedule_view": lambda bp: any(v["type"] in ("calendar", "timeline") for t in bp.get("tables", []) for v in t["views"]),
    "datetime": lambda bp: any(f["type"] in ("datetime", "time") for _, f in _fields(bp)),
    "automation": lambda bp: bool(bp.get("automations")),
    "schedule": lambda bp: any(a["trigger"]["type"] == "schedule" for a in bp.get("automations", [])),
    "board": lambda bp: any(v["type"] == "board" for t in bp.get("tables", []) for v in t["views"]),
    "page": lambda bp: bool(bp.get("pages")),
    "colour": lambda bp: any(t.get("colors") for t in bp.get("tables", [])),
    "auto_create": lambda bp: any(act["type"] in ("create_record", "create_for_each", "ai_create_record") for act in _actions(bp)),
    "row_access": lambda bp: any(t.get("row_access") for t in bp.get("tables", [])),
    "lock": lambda bp: any(t.get("lock_if") for t in bp.get("tables", [])),
    "rule": lambda bp: any(t.get("rules") for t in bp.get("tables", [])),
    "restricted": lambda bp: any(t.get("access") for t in bp.get("tables", [])) or any(f.get("access") for _, f in _fields(bp)),
    "currency": lambda bp: any(f.get("currency") or f.get("currency_field") for _, f in _fields(bp))
                           or any(f["type"] == "formula" and "convert(" in f.get("expression", "") for _, f in _fields(bp)),
    "pivot": lambda bp: any(w["type"] == "pivot" for p in bp.get("pages", []) for w in p["widgets"]),
    "chart_or_pivot": lambda bp: any(w["type"] in ("pivot", "chart") for p in bp.get("pages", []) for w in p["widgets"]),
    "stock_ledger": lambda bp: any(("movement" in t["key"] or "transaction" in t["key"] or "ledger" in t["key"]) for t in bp.get("tables", []))
                               and any(f["type"] == "rollup" and ("stock" in f["key"] or "hand" in f["key"] or "qty" in f["key"] or "quantity" in f["key"]) for _, f in _fields(bp)),
    "stock_ledger_or_rollup": lambda bp: FEATURES["stock_ledger"](bp) or any(f["type"] == "rollup" and any(w in f["key"] for w in ("stock", "hand", "qty", "quantity")) for _, f in _fields(bp)),
    "webhook_trigger": lambda bp: any(a["trigger"]["type"] == "webhook_received" for a in bp.get("automations", [])),
    "dependencies": lambda bp: any(v.get("depends_field") for t in bp.get("tables", []) for v in t["views"]),
    "portal_or_restricted": lambda bp: _portal(bp) or FEATURES["row_access"](bp),
    "portal_or_public_form": lambda bp: _portal(bp) or FEATURES["public_form"](bp),
    "signature_or_approval": lambda bp: FEATURES["signature"](bp) or any(act["type"] == "request_approval" for act in _actions(bp)),
    "schedule_view_or_datetime": lambda bp: FEATURES["schedule_view"](bp) or FEATURES["datetime"](bp),
    "workflow": lambda bp: any(t.get("workflow") for t in bp.get("tables", [])),
    "approval_threshold": _threshold_approval,
    "if_rejected": _rejection_path,
    "escalation": lambda bp: any(act.get("remind_after_days") or act.get("escalate_after_days") for act in _actions(bp))
                             or any(a["trigger"]["type"] == "schedule" and any(x["type"] in ("notify", "send_email") for x in _steps_of(a))
                                    for a in bp.get("automations", [])),
    "task_or_required_move": lambda bp: any(act["type"] == "task" for act in _actions(bp))
                                        or any(m.get("requires") for t in bp.get("tables", []) for m in (t.get("workflow") or {}).get("transitions", [])),
    "ai_intake": lambda bp: any(act["type"] == "ai_create_record" for act in _actions(bp)),
    "ai_checks_set": lambda bp: any(act.get("review") in ("always", "when_unsure") for act in _actions(bp)),
    "ai_reply_checked": _checked_reply,
    "ai_match": lambda bp: any(act["type"] == "ai_fill" and (_field_of(bp, a, act) or {}).get("type") == "link"
                               for a in bp.get("automations", []) for act in _steps_of(a)),
    "ai_knowledge": lambda bp: any(act.get("use_knowledge") for act in _actions(bp)),
    "ai_draft_checked": lambda bp: any(act["type"] == "ai_fill" and act.get("review") == "always" for act in _actions(bp)),
    "ai_reads_documents": lambda bp: any(act["type"] in ("ai_extract", "ai_create_record") for act in _actions(bp)),
    "ai_check_reference": lambda bp: any(act["type"] == "ai_check" and act.get("reads") for act in _actions(bp)),
    "routes_on_ai": _routes_on_ai,
    "conditional_field": lambda bp: any(f.get(k) for _, f in _fields(bp) for k in ("show_if", "hide_if", "readonly_if", "required_if", "options_by"))
                                    or any(s.get("show_if") for t in bp.get("tables", []) for s in t.get("sections", [])),
    # procurement-grade controls (a source-to-pay system)
    "teams": lambda bp: bool(bp.get("teams")),
    "limits_lookup": lambda bp: any(f["type"] == "lookup" and "where" in f for _, f in _fields(bp))
                                and any(act["type"] == "request_approval" for act in _actions(bp)) or _approver_from_limits(bp),
    "separation_of_duties": lambda bp: any(m.get("not_by") for t in bp.get("tables", []) for m in (t.get("workflow") or {}).get("transitions", []))
                                       or any(act.get("not_by") for act in _actions(bp)),
    "step_up": lambda bp: any(m.get("step_up") for t in bp.get("tables", []) for m in (t.get("workflow") or {}).get("transitions", []))
                          or any(act.get("step_up") for act in _actions(bp)) or any((t.get("approve_changes") or {}).get("step_up") for t in bp.get("tables", [])),
    "unique_together": lambda bp: any(t.get("unique_together") for t in bp.get("tables", [])),
    "bank_file": lambda bp: any(d.get("format") == "text" for t in bp.get("tables", []) for d in t.get("documents", [])),
    "supplier_portal": lambda bp: _portal(bp) and any((t.get("portal") or {}).get("can_add") or "edit" in (t.get("portal") or {})
                                                      or any("portal" in (m.get("who") or []) for m in (t.get("workflow") or {}).get("transitions", []))
                                                      for t in bp.get("tables", [])),
    "sealed_bids": lambda bp: any(t.get("sealed") for t in bp.get("tables", [])),
    "sensitive": lambda bp: any(f.get("sensitive") for _, f in _fields(bp)),
    "change_approval": lambda bp: any(t.get("approve_changes") for t in bp.get("tables", [])),
    "cooling_off": lambda bp: any((t.get("approve_changes") or {}).get("wait_days") for t in bp.get("tables", [])),
    "sign_up": lambda bp: any(p.get("sign_up") is not None for p in bp.get("portals") or []),
    "invoice_match": _invoice_match,
}



JARGON = re.compile(r"\b(fields?|tables?|automations?|workflows?|schema|json|rollups?|formulas?|lookups?|webhooks?|enum|boolean|"
                    r"modules?|ops|embedded|title_field|blueprint|crud|api)\b", re.I)
REMOVING = {"delete_table", "remove_field", "remove_automation", "remove_page", "remove_view", "remove_document"}


def score(bp, expect, message, imported, tables_before=0):
    """What a turn got right and wrong, as {check: True/False}, plus notes on smells. Vague requests are judged on what a
    good analyst does with them: asks a few plain questions when it can't tell, builds something small and sensible when
    it can, and never deletes what's there because a request was vague."""
    out = {}
    text = message.text or ""
    asked = "?" in text or bool(re.search(r"\b(tell me|let me know|what do you|which)\b", text, re.I))
    tables = len([t for t in bp.get("tables", []) if not t.get("embedded")])
    sent = {str(o.get("op")) for o in message.ops or [] if isinstance(o, dict)}
    for words in expect.get("tables", []):
        out[f"table:{words[0]}"] = _has_table(bp, words)
    for name in expect.get("features", []):
        out[f"feature:{name}"] = bool(FEATURES[name](bp))
    if "max_tables" in expect:
        n = len([t for t in bp.get("tables", []) if not t.get("embedded")])
        out[f"small:<= {expect['max_tables']} tables"] = n <= expect["max_tables"]
    if expect.get("asks"):
        out["asks instead of building"] = not message.ops and asked  # "Tell me what you sell…" asks too
    if "asks_or_small" in expect:  # can't tell much: ask, or build a small start and ask about the rest
        out[f"asks, or builds small (<= {expect['asks_or_small']}) and asks"] = (not message.ops and asked) or \
            (bool(message.changes) and tables <= expect["asks_or_small"] and asked)
    if expect.get("builds"):
        out["builds"] = bool(message.changes)
    if expect.get("plain"):
        found = sorted({m.group(0).lower() for m in JARGON.finditer(text)})
        out["plain words" + (f" (said {', '.join(found)})" if found else "")] = not found
    if "few_questions" in expect:
        out[f"at most {expect['few_questions']} questions"] = text.count("?") <= expect["few_questions"]
    if expect.get("keeps"):  # a vague request never costs the owner what's there
        out["keeps what's there"] = not (sent & REMOVING) and tables >= tables_before
    if expect.get("question"):
        out["answers without changing"] = not message.version_after or message.version_after == message.version_before
    if expect.get("imports"):
        out["imports the rows"] = imported > 0
    return out


def smells(bp):
    """Things a good analyst wouldn't do."""
    notes = []
    labels = [t["label"].lower().rstrip("s") for t in bp.get("tables", [])]
    for name in set(labels):
        if labels.count(name) > 1:
            notes.append(f"two tables for {name}")
    for t, f in _fields(bp):
        if f["type"] == "text" and any(w in f["key"] for w in ("amount", "price", "total", "cost", "rate")) and not f["key"].endswith("_text"):
            notes.append(f"{t['key']}.{f['key']} is text but looks like money")
        if f["type"] == "text" and any(w in f["key"] for w in ("date", "_on", "due")) and f["key"] not in ("update", "notes"):
            notes.append(f"{t['key']}.{f['key']} is text but looks like a date")
        if f.get("access") and f["type"] == "link":
            notes.append(f"{t['key']}.{f['key']}: access on a link")
    linked = {f["table"] for _, f in _fields(bp) if f["type"] == "link"} | {t["key"] for t, f in _fields(bp) if f["type"] == "link"}
    for t in bp.get("tables", []):
        if len(bp.get("tables", [])) > 2 and t["key"] not in linked:
            notes.append(f"{t['label']} isn't linked to anything")
    return notes


def tried(ws):
    """The finished design tried out in a copy (app/builder/trial.py) and its business's stories acted out (stories.py)."""
    from app.builder import trial
    walked, _ = trial.try_out(ws)
    return {"made": len(walked["made"]), "tables": len((ws.blueprint or {}).get("tables", [])),
            "fix": [i["text"] for i in walked["issues"] if i["level"] == "fix"],
            "check": [i["text"] for i in walked["issues"] if i["level"] == "check"], "crashes": walked["crashes"]}


def spreadsheet(path):
    from openpyxl import Workbook
    book = Workbook()
    sheet = book.active
    sheet.title = "Orders"
    sheet.append(["Order #", "Date", "Customer", "Customer email", "Product", "Qty", "Unit price", "Status"])
    rows = [("1001", "2026-09-02", "Bean There Cafe", "orders@beanthere.com.au", "House blend 1kg", 4, 32.5, "Delivered"),
            ("1002", "2026-09-03", "Little Fox", "hello@littlefox.com.au", "Decaf 500g", 6, 18.0, "Delivered"),
            ("1003", "2026-09-10", "Bean There Cafe", "orders@beanthere.com.au", "Single origin 1kg", 2, 41.0, "Invoiced"),
            ("1004", "2026-09-15", "Grind House", "accounts@grindhouse.net", "House blend 1kg", 10, 32.5, "Packed"),
            ("1005", "2026-09-21", "Little Fox", "hello@littlefox.com.au", "House blend 1kg", 3, 32.5, "New")]
    for r in rows:
        sheet.append(list(r))
    book.save(path)


def _database(out, key):
    """The scenario's own PostgreSQL database, made again empty each run and kept after (for a look): named by the run's
    folder and the scenario, so runs side by side never share one."""
    import hashlib
    from app import database
    from app.config import Config
    run_id = hashlib.sha1(str(Path(out).resolve()).encode()).hexdigest()[:8]
    return database.fresh(Config.SQLALCHEMY_DATABASE_URI, f"eval_{run_id}_{re.sub(r'[^a-z0-9_]+', '_', key.lower())}")


def run(scenario, out):
    from app import create_app
    data = out / scenario["key"]
    shutil.rmtree(data, ignore_errors=True)
    data.mkdir(parents=True)
    app = create_app({"DATA_DIR": data, "SQLALCHEMY_DATABASE_URI": _database(out, scenario["key"]), "WORKER": False,
                      "WTF_CSRF_ENABLED": False, "SECRET_KEY": "eval",
                      "TRAINING_LOG": bool(scenario.get("fresh"))})  # the fixed scenarios are the exam: never lessons
    result = {"key": scenario["key"], "title": scenario["title"], "turns": []}
    with app.app_context():
        if scenario.get("fresh") and not scenario["turns"]:
            opener, real = fresh.owner(scenario["fresh"], scenario.get("style", "plain"))
            scenario = dict(scenario, turns=[{"say": opener, "expect": {}}], real=real)
            result["said"], result["style"] = opener, scenario.get("style", "plain")
            if opener != real:
                result["real"] = real
        from app.blueprint import schema
        from app.builder import chat
        from app.extensions import db
        from app.models import Membership, Message, Record, User, Workspace
        owner = User(email="owner@example.com", name="Sam")
        owner.set_password("eval-password-1")
        db.session.add(owner)
        db.session.flush()
        ws = Workspace(owner_id=owner.id, name=scenario["title"].split(" (")[0], about="", blueprint=schema.empty(scenario["title"].split(" (")[0]),
                       settings={}, state={})
        db.session.add(ws)
        db.session.flush()
        db.session.add(Membership(workspace_id=ws.id, user_id=owner.id, role="owner"))
        db.session.commit()
        files = []
        if scenario.get("file"):
            from app.storage import save
            path = data / "orders.xlsx"
            spreadsheet(path)
            files = [save(ws.id, path.read_bytes(), "Orders 2026.xlsx", source="chat", by="Sam").id]
        for i, turn in enumerate(scenario["turns"]):
            tables_before = len([t for t in (ws.blueprint or {}).get("tables", []) if not t.get("embedded")])
            reply = chat.ask(ws, turn["say"], user=owner, files=files if i == 0 else None)
            started = time.time()
            try:
                chat.work(reply.id)
            except Exception as exc:
                traceback.print_exc()
                result["turns"].append({"say": turn["say"], "crash": f"{type(exc).__name__}: {exc}"})
                continue
            from app import jobs
            from app.models import Job
            for job in Job.query.filter_by(workspace_id=ws.id, kind="import", status="queued").all():  # as the worker would
                jobs.run(job)
            db.session.expire_all()
            reply = db.session.get(Message, reply.id)
            ws = db.session.get(Workspace, ws.id)
            imported = Record.query.filter_by(workspace_id=ws.id, deleted_at=None).count()
            checks = score(ws.blueprint, turn.get("expect", {}), reply, imported, tables_before)
            result["turns"].append({
                "say": turn["say"], "seconds": round(time.time() - started), "status": reply.status, "error": reply.error,
                "reply": reply.text, "parts": sum(1 for p in reply.progress or [] if p.startswith("Built part")) + (1 if reply.status == "done" else 0),
                "repairs": sum(1 for p in reply.progress or [] if p.startswith("Fixing")), "changes": len(reply.changes or []),
                "checks": checks, "passed": sum(checks.values()), "of": len(checks), "tried": reply.checks,
                "left_out": [p for p in reply.progress or [] if p.startswith("Left out") or p.startswith("Didn't fit")],
                "stories": {st["story"]: st["result"] for st in stories.run(ws, scenario["key"].split("-")[0])}
                if (ws.blueprint or {}).get("tables") else {}})
            if scenario.get("real") and reply.status == "done" and not reply.changes and "?" in (reply.text or "") \
                    and len(scenario["turns"]) < 3:  # it asked: the owner answers from how their business really works
                scenario["turns"].append({"say": fresh.answer(scenario["fresh"], scenario["real"], reply.text), "expect": {}})
        bp = ws.blueprint
        result["design"] = {"tables": [f"{t['label']}{' (lines)' if t.get('embedded') else ''}: {len(t['fields'])} fields" for t in bp.get("tables", [])],
                            "pages": [p["label"] for p in bp.get("pages", [])], "automations": [a["name"] for a in bp.get("automations", [])],
                            "problems": schema.check(bp)}
        result["smells"] = smells(bp)
        result["tryout"] = tried(ws) if bp.get("tables") else None
        result["stories"] = stories.run(ws, scenario["key"].split("-")[0]) if bp.get("tables") else []
        result["ux"] = ux.review(ws, result) if bp.get("tables") else []  # what a beginner would trip over
        if scenario.get("fresh") and bp.get("tables"):
            result["owner_checks"] = owner_checks(ws, scenario, data / "training")
        (data / "blueprint.json").write_text(json.dumps(bp, indent=1, ensure_ascii=False))  # as handed over
        if bp.get("tables"):
            theirs = ux.theirs_of(ws, result)
            result["map_ux"] = mapux.review(ws, theirs)  # the App map as the owner sees it, in a browser
            result["updates"] = updated(ws, owner, scenario, result, theirs)
            ws = db.session.get(Workspace, ws.id)
            (data / "blueprint_updated.json").write_text(json.dumps(ws.blueprint, indent=1, ensure_ascii=False))
    return result


def updated(ws, owner, scenario, result, theirs, ask=True):
    """The finished app changed the two ways an owner can (evals/updates.py): by hand on the App map, in a copy, then by
    asking the AI, on the app itself. Each change must work and keep what worked; what each says is looked at too."""
    key = scenario["key"].split("-")[0]
    before = {st["story"]: st["result"] for st in result.get("stories") or []}
    try:
        changes, after = updates.by_hand(ws, key)
    except Exception as exc:  # noqa: BLE001 - a fault of ours: said, and the AI's turn is still tried
        traceback.print_exc()
        changes, after = [{"how": "map", "change": "changing it on the App map", "ok": False, "why": f"it failed: {type(exc).__name__}: {exc}"[:200]}], {}
    broke = sorted(name for name, r in before.items() if r == "pass" and after and after.get(name) != "pass")
    if broke:
        changes.append({"how": "map", "change": "everything that worked still works", "ok": False, "why": f"broke: {', '.join(broke)}"})
    if ask:
        try:
            changes += updates.by_asking(ws, owner, key, before)
        except Exception as exc:  # noqa: BLE001 - a fault of ours: said, and the run goes on
            traceback.print_exc()
            changes.append({"how": "ai", "change": "asking the AI for changes", "ok": False, "why": f"it failed: {type(exc).__name__}: {exc}"[:200]})
    return {"changes": changes, "ux": updates.findings(changes, theirs)}


def owner_checks(ws, scenario, folder):
    """The owner's own checks, written from what they said before the build, tried on what was built. Those the app could
    be checked on decide whether its build turns are verified lessons (training.verdict); none, and nobody can say."""
    from app.builder import checking, training
    out = []
    try:
        said = fresh.checks(scenario["fresh"], scenario["real"])
    except Exception as exc:  # noqa: BLE001 - no checks: the turns stay unverified
        print(f"  owner checks couldn't be written: {exc}", flush=True)
        return out
    for text in said:
        try:
            r = checking.add(ws, text, by="owner")
        except Exception as exc:  # noqa: BLE001
            out.append({"check": text, "result": f"crash: {type(exc).__name__}"})
            continue
        out.append({"check": text, "result": "couldn't check" if r is None or r["kind"] in ("example", "crash") else "pass" if r["ok"] else "fail",
                    "why": (r or {}).get("plain") or (r or {}).get("problem") or ""})
    tried = [c for c in out if c["result"] in ("pass", "fail")]
    if tried:
        failed = [c for c in tried if c["result"] == "fail"]
        training.verdict(folder, not failed, failed[0]["check"] if failed else "", "owner checks", workspace=ws.id, checks=out)
    return out


def handed_over(result):
    """What stops the finished app being right when handed over: none when it does what was asked (its stories and the
    owner's checks), trying it out finds nothing wrong, nothing errors and nothing was left out. None when nothing was
    built (a vague request answered with a question is scored by its design checks)."""
    if not result.get("tryout"):
        return None
    why = []
    for t in result["turns"]:
        if t.get("crash"):
            why.append("a turn crashed")
        elif t["status"] != "done":
            why.append(f"a turn {t['status']}")
        elif t.get("left_out"):
            why.append(f"left out {len(t['left_out'])} part(s)")
    last = next((t for t in reversed(result["turns"]) if not t.get("crash")), {})
    failed = [e["title"] for e in (last.get("tried") or {}).get("examples") or [] if not e["ok"]]
    if failed:
        why.append(f"its own examples failed: {', '.join(failed[:3])}")
    x = result["tryout"]
    if x["fix"]:
        why.append(f"trying it out found {len(x['fix'])} thing(s) wrong")
    if x["crashes"]:
        why.append(f"{len(x['crashes'])} platform fault(s)")
    why += [f"story {st['result']}: {st['story']}" for st in result.get("stories") or [] if st["result"] != "pass"]
    why += [f"owner's check failed: {c['check'][:80]}" for c in result.get("owner_checks") or [] if c["result"] == "fail"]
    why += [f"a beginner would be stuck: {f['what'][:80]} ({f['who']}, {f['where']})" for f in result.get("ux") or [] if f["level"] == "blocker"]
    why += [f"a beginner would be stuck on the App map: {f['what'][:80]} ({f['where']})" for f in result.get("map_ux") or [] if f["level"] == "blocker"]
    return why


def kept_working(result):
    """(stories that worked before a later change, {story: the turn that broke it}): a change mustn't break what was right."""
    worked, broke = set(), {}
    for n, t in enumerate(result["turns"], 1):
        now = t.get("stories")
        if now is None:  # crashed, or a run saved before stories were acted out after each turn
            continue
        for name in worked:
            if now.get(name) != "pass":
                broke.setdefault(name, n)
        worked |= {name for name, r in now.items() if r == "pass" and n < len(result["turns"])}
    return worked, broke


def consistency(results):
    """With each business built several times (--repeat): how often it came out the same. A builder that's right on one
    run and wrong on the next is one an owner can't rely on, so "sometimes" counts as much as "never". None without repeats."""
    groups = {}
    for r in results:
        base = re.sub(r"-\d+$", "", str(r.get("key") or ""))
        groups.setdefault(base, []).append(r)
    groups = {k: v for k, v in groups.items() if len(v) > 1}
    if not groups:
        return None
    out = {"businesses": len(groups), "always": [], "never": [], "sometimes": [], "flipping": []}
    for base, runs in sorted(groups.items()):
        verdicts = [handed_over(r) for r in runs]
        built = [not w for w in verdicts if w is not None]
        if built:
            right = sum(built)
            out["always" if right == len(built) else "never" if right == 0 else "sometimes"].append(f"{base} {right}/{len(built)}")
        passed = {}
        for r in runs:
            for st in r.get("stories") or []:
                passed.setdefault(st["story"], []).append(st["result"] == "pass")
        out["flipping"] += [f"{base}: {name} ({sum(v)}/{len(v)})" for name, v in passed.items() if 0 < sum(v) < len(v)]
    return out


def report(result):
    lines = [f"\n== {result['title']}"] + ([f"  owner said ({result.get('style', 'plain')}): {result['said'][:400]}"] if result.get("said") else [])
    for t in result["turns"]:
        if t.get("crash"):
            lines.append(f"  CRASH: {t['crash']}")
            continue
        missed = [k for k, v in t["checks"].items() if not v]
        lines.append(f"  [{t['status']}] {t['seconds']}s, {t['parts']} part(s), {t['repairs']} repair(s), {t['changes']} changes, "
                     f"{t['passed']}/{t['of']} checks" + (f"  MISSED: {', '.join(missed)}" if missed else "")
                     + (f"\n    error: {t['error'][:300]}" if t.get("error") else ""))
        lines.append(f"    reply: {(t['reply'] or '')[:600]}")
        if t.get("left_out"):
            lines.append(f"    left out ({len(t['left_out'])}): {' | '.join(x[:140] for x in t['left_out'][:6])}")
    d = result.get("design") or {}
    lines.append(f"  design: {len(d.get('tables', []))} tables, {len(d.get('pages', []))} pages, {len(d.get('automations', []))} automations"
                 + (f"; PROBLEMS {d['problems'][:3]}" if d.get("problems") else ""))
    if result.get("smells"):
        lines.append(f"  smells: {'; '.join(result['smells'][:8])}")
    x = result.get("tryout")
    if x:
        lines.append(f"  tried out: records in {x['made']}/{x['tables']} tables; {len(x['fix'])} wrong, {len(x['check'])} to check, "
                     f"{len(x['crashes'])} platform faults" + "".join(f"\n    WRONG: {i[:220]}" for i in x["fix"][:8])
                     + "".join(f"\n    check: {i[:200]}" for i in x["check"][:4]) + "".join(f"\n    FAULT: {c[:200]}" for c in x["crashes"][:4]))
    for st in result.get("stories") or []:
        lines.append(f"  story [{st['result']}] {st['story']}" + (f": {st['why'][:240]}" if st["why"] else ""))
    found = result.get("ux") or []
    if found:
        lines.append(f"  beginner's eyes: {sum(f['level'] == 'blocker' for f in found)} stuck, {sum(f['level'] == 'confusing' for f in found)} "
                     f"confusing, {sum(f['level'] == 'idea' for f in found)} ideas"
                     + "".join(f"\n    {f['level'].upper()}: {f['what'][:170]} ({f['who']}, {f['where']})"
                               for f in sorted(found, key=lambda f: ("blocker", "confusing", "idea").index(f["level"]))[:10]))
    for name, n in kept_working(result)[1].items():
        lines.append(f"  BROKEN BY A LATER CHANGE: {name} (worked before turn {n})")
    found = result.get("map_ux")
    if found is not None:
        lines.append(f"  App map, beginner's eyes: {sum(f['level'] == 'blocker' for f in found)} stuck, {sum(f['level'] == 'confusing' for f in found)} "
                     f"confusing, {sum(f['level'] == 'idea' for f in found)} ideas"
                     + "".join(f"\n    {f['level'].upper()}: {f['what'][:170]} ({f['who']}, {f['where']})"
                               for f in sorted(found, key=lambda f: ("blocker", "confusing", "idea").index(f["level"]))[:10]))
    up = result.get("updates")
    if up:
        for how, said in (("map", "changed by hand on the App map"), ("ai", "changed by asking the AI")):
            done = [c for c in up["changes"] if c["how"] == how and c["ok"] is not None]
            if done:
                lines.append(f"  {said}: {sum(1 for c in done if c['ok'])}/{len(done)} worked"
                             + (f" ({round(sum(c.get('seconds', 0) for c in done) / len(done))}s each)" if how == "ai" else "")
                             + "".join(f"\n    x {c['change']}: {c['why'][:200]}" for c in done if not c["ok"]))
        if up["ux"]:
            lines.append(f"  what the changes said, beginner's eyes: " + "; ".join(f"{f['level']}: {f['what'][:120]} ({f['where'][:60]})" for f in up["ux"][:6]))
    for c in result.get("owner_checks") or []:
        lines.append(f"  owner's check [{c['result']}] {c['check'][:200]}" + (f": {c['why'][:200]}" if c.get("why") and c["result"] == "fail" else ""))
    why = handed_over(result)
    if why is not None:
        lines.append(f"  HANDED OVER: {'RIGHT' if not why else 'not right: ' + '; '.join(why[:6])}")
    for t in result["turns"]:
        examples = ((t.get("tried") or {}).get("examples") or [])
        first = (t.get("tried") or {}).get("first")
        if first:
            right = not first["wrong"] and first["examples"][0] == first["examples"][1]
            lines.append(f"  first draft: {'right' if right else 'not right'} ({first['wrong']} wrong, examples {first['examples'][0]}/{first['examples'][1]})"
                         + "".join(f"\n    found: {x[:200]}" for x in first.get("found", [])[:6]))
        if examples:
            lines.append(f"  builder's own examples: {sum(1 for e in examples if e['ok'])}/{len(examples)} worked"
                         + "".join(f"\n    x {e['title']}: {e.get('problem', '')[:200]}" for e in examples if not e["ok"]))
    return "\n".join(lines)


def regrade(folder, only=(), resolve=False):
    """Try out the designs a run saved (blueprint.json in each scenario's folder) and act out their stories again."""
    from app import create_app
    results = []
    log = Path(folder) / "results.jsonl"  # what the run said, so its replies are looked at again too
    saved = {r["key"]: r for r in (json.loads(line) for line in log.read_text().splitlines() if line.strip())} if log.exists() else {}
    for path in sorted(Path(folder).glob("*/blueprint.json")):
        key = path.parent.name
        if only and key.split("-")[0] not in only:
            continue
        scenario = next((x for x in SCENARIOS if x["key"] == key.split("-")[0]), {"key": key, "title": key})
        data = path.parent / "regrade"
        shutil.rmtree(data, ignore_errors=True)
        data.mkdir()
        app = create_app({"DATA_DIR": data, "SQLALCHEMY_DATABASE_URI": _database(path.parent.parent, f"{key}_regrade"), "WORKER": False,
                          "WTF_CSRF_ENABLED": False, "SECRET_KEY": "eval", "TESTING": True})
        with app.app_context():
            from app.extensions import db
            from app.models import Membership, User, Workspace
            owner = User(email="owner@example.com", name="Sam")
            owner.password_hash = "!"
            db.session.add(owner)
            db.session.flush()
            bp = json.loads(path.read_text())
            if resolve:  # as the builder would save it now, with today's corrections
                from app.blueprint import ops
                ops._resolve(bp, [])
            ws = Workspace(owner_id=owner.id, name=bp.get("name") or key, about="", blueprint=bp, settings={}, state={})
            db.session.add(ws)
            db.session.flush()
            db.session.add(Membership(workspace_id=ws.id, user_id=owner.id, role="owner"))
            db.session.commit()
            result = {"key": key, "title": scenario["title"], "turns": _replayed(ws, saved.get(key, {}).get("turns") or []), "design": {},
                      "tryout": tried(ws) if bp.get("tables") else None, "stories": stories.run(ws, scenario["key"]) if bp.get("tables") else []}
            result["ux"] = ux.review(ws, result) if bp.get("tables") else []
            if bp.get("tables"):
                theirs = ux.theirs_of(ws, result)
                result["map_ux"] = mapux.review(ws, theirs)
                result["updates"] = updated(ws, owner, scenario, result, theirs, ask=False)
                asked = [c for c in ((saved.get(key) or {}).get("updates") or {}).get("changes") or [] if c["how"] == "ai"]
                result["updates"]["changes"] += asked  # (the AI isn't asked again: what it did then)
                result["updates"]["ux"] = updates.findings(result["updates"]["changes"], theirs)
        shutil.rmtree(data, ignore_errors=True)
        print(report(result), flush=True)
        results.append(result)
    print(totals(results))
    return results


def _replayed(ws, turns):
    """The turns, with the last one's own examples acted out again on the saved design by today's code (a run saved
    before the examples were kept with their results keeps what it found then)."""
    from app.builder import trial
    last = next((t for t in reversed(turns) if not t.get("crash")), None)
    shown = ((last or {}).get("tried") or {}).get("examples") or []
    if not shown or not all(e.get("example") for e in shown):
        return turns
    _, results = trial.try_out(ws, [e["example"] for e in shown], screens=False)
    again = [{**e, "ok": r["ok"], "lines": [[ok, line] for ok, line in r["lines"]], "problem": r.get("plain") or ""}
             for e, r in zip(shown, results)]
    return [{**t, "tried": {**t["tried"], "examples": again}} if t is last else t for t in turns]


def totals(results):
    """The run in a few numbers. First what matters: was each app right when handed over, and did later changes keep it
    right. Then what's behind it: the stories, the try-out, the builder's own examples and how its first drafts came out."""
    turns = [t for r in results for t in r["turns"] if not t.get("crash")]
    checks = (sum(t["passed"] for t in turns), sum(t["of"] for t in turns))
    told = [st for r in results for st in r.get("stories") or []]
    by = {k: sum(1 for st in told if st["result"] == k) for k in ("pass", "fail", "missing", "error")}
    wrong = sum(len((r.get("tryout") or {}).get("fix") or []) for r in results)
    faults = sum(len((r.get("tryout") or {}).get("crashes") or []) for r in results)
    examples = [e for t in turns for e in ((t.get("tried") or {}).get("examples") or [])]
    clean = sum(1 for r in results if r.get("tryout") and not r["tryout"]["fix"])
    built = [w for w in map(handed_over, results) if w is not None]
    kept = [kept_working(r) for r in results]
    worked, broke = sum(len(k[0]) for k in kept), sum(len(k[1]) for k in kept)
    seen = [f for r in results for f in r.get("ux") or []]
    kinds = {level: Counter(f["kind"] for f in seen if f["level"] == level) for level in ("blocker", "confusing", "idea")}
    tried_turns = [t for t in turns if (t.get("tried") or {}).get("first")]
    first_time = sum(1 for t in tried_turns if not t["tried"]["first"]["wrong"]
                     and t["tried"]["first"]["examples"][0] == t["tried"]["first"]["examples"][1])
    delivered = sum(1 for t in tried_turns if not t["tried"].get("issues") and all(e["ok"] for e in t["tried"].get("examples") or []))
    steady = consistency(results)
    changes = [c for r in results for c in (r.get("updates") or {}).get("changes") or [] if c["ok"] is not None]
    ways = {how: [c for c in changes if c["how"] == how] for how in ("map", "ai")}
    eyes = [f for r in results for f in r.get("map_ux") or []] + [f for r in results for f in (r.get("updates") or {}).get("ux") or []]
    map_kinds = {level: Counter(f["kind"] for f in eyes if f["level"] == level) for level in ("blocker", "confusing", "idea")}
    return (f"\n== TOTALS over {len(results)} run(s)\n"
            + (f"  CONSISTENT: {len(steady['always']) + len(steady['never'])}/{steady['businesses']} businesses came out the same every "
               f"run (right every time: {len(steady['always'])}, wrong every time: {len(steady['never'])}); sometimes right: "
               f"{', '.join(steady['sometimes']) or 'none'}\n"
               + (f"    stories that flip between runs: {'; '.join(steady['flipping'][:12])}\n" if steady["flipping"] else "")
               if steady else "")
            + (f"  RIGHT WHEN HANDED OVER: {sum(1 for w in built if not w)}/{len(built)} apps (did what was asked, nothing wrong "
               f"when tried out, no errors, nothing left out)\n" if built else "")
            + (f"  KEPT WORKING through later changes: {worked - broke}/{worked} stories that worked before a later change\n" if worked else "")
            + (f"  BEGINNER'S EYES: {sum(kinds['blocker'].values())} places they'd be stuck, {sum(kinds['confusing'].values())} confusing, "
               f"{sum(kinds['idea'].values())} ideas"
               + "".join(f"\n    {level}: " + ", ".join(f"{k} {n}" for k, n in kinds[level].most_common(6)) for level in kinds if kinds[level])
               + "\n" if any(r.get("ux") is not None for r in results) else "")
            + (f"  CHANGED AFTER HANDOVER: by hand on the App map {sum(1 for c in ways['map'] if c['ok'])}/{len(ways['map'])} worked; "
               f"by asking the AI {sum(1 for c in ways['ai'] if c['ok'])}/{len(ways['ai'])} worked"
               + (f" ({round(sum(c.get('seconds', 0) for c in ways['ai']) / len(ways['ai']))}s a change)" if ways["ai"] else "")
               + "".join(f"\n    {how} didn't: " + ", ".join(f"{k} {n}" for k, n in Counter(re.sub(r"“[^”]*”|\b[A-Z][\w ]*$", "…", c["change"]) for c in ways[how] if not c["ok"]).most_common(5))
                         for how in ("map", "ai") if any(not c["ok"] for c in ways[how])) + "\n" if changes else "")
            + (f"  APP MAP AND CHANGES, BEGINNER'S EYES: {sum(map_kinds['blocker'].values())} places they'd be stuck, "
               f"{sum(map_kinds['confusing'].values())} confusing, {sum(map_kinds['idea'].values())} ideas"
               + "".join(f"\n    {level}: " + ", ".join(f"{k} {n}" for k, n in map_kinds[level].most_common(6)) for level in map_kinds if map_kinds[level])
               + "\n" if any(r.get("map_ux") is not None for r in results) else "")
            + (f"  design checks {checks[0]}/{checks[1]}\n" if checks[1] else "")
            + f"  stories: {by['pass']} pass, {by['fail']} fail, {by['missing']} missing, {by['error']} error (of {len(told)})\n"
            + f"  tried out: {wrong} things wrong across the designs; {clean}/{sum(1 for r in results if r.get('tryout'))} designs with none; "
              f"{faults} platform faults\n"
            + (f"  builder's own examples: {sum(1 for e in examples if e['ok'])}/{len(examples)} worked\n" if examples else "")
            + (f"  build turns handed over with nothing wrong by the builder's own try-out: {delivered}/{len(tried_turns)}; "
               f"first draft right, before the builder's own fixes: {first_time}/{len(tried_turns)} (speed and training data, not the goal)\n"
               if tried_turns else ""))


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--out")
    parser.add_argument("--repeat", type=int, default=1, help="run each scenario this many times (the model varies)")
    parser.add_argument("--regrade", help="a run's folder: score its saved designs again, without the model")
    parser.add_argument("--fresh", type=int, default=0, help="also build this many businesses never seen before (owners' words written new)")
    parser.add_argument("--seed", type=int, help="with --fresh: which businesses (the same seed picks the same trades)")
    parser.add_argument("--resolve", action="store_true", help="with --regrade: apply today's corrections to the saved designs first")
    parser.add_argument("--resume", action="store_true", help="skip what --out already has a result for (a run that stopped part way)")
    parser.add_argument("only", nargs="*")
    args = parser.parse_args(argv)
    if args.regrade:
        regrade(args.regrade, args.only, args.resolve)
        return 0
    if not args.out:
        parser.error("--out is needed (or --regrade)")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    chosen = [s for s in SCENARIOS if not args.only or s["key"] in args.only] if not (args.fresh and not args.only) else []
    chosen += fresh.scenarios(args.fresh, args.seed)
    results = [json.loads(line) for line in (out / "results.jsonl").open()] if args.resume and (out / "results.jsonl").exists() else []
    done = {r["key"] for r in results}
    with (out / "results.jsonl").open("a") as log:
        for n in range(1, args.repeat + 1):
            for scenario in chosen:
                scenario = dict(scenario, key=f"{scenario['key']}-{n}") if args.repeat > 1 else scenario
                if scenario["key"] in done:
                    continue
                result = run(scenario, out)
                result["at"] = datetime.now().isoformat(timespec="seconds")
                log.write(json.dumps(result, ensure_ascii=False, default=str) + "\n")
                log.flush()
                print(report(result), flush=True)
                results.append(result)
    print(totals(results), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
