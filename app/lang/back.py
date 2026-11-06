"""The design read back into the Keel language: any rule the design has, however it was made (the App map, a ready-made
module, the analyst, an older builder), said as a Keel rule. So every editor changes rules in one language, and the AI
reads and changes a rule a person made on the App map in the words it writes itself.

    rule_in_keel(bp, automation)   -> a Keel rule (a dict that m.Rule accepts), or raises Unsaid saying what Keel can't
                                      say yet

Compiling what comes back gives the same automation (tests/test_back.py proves it on every rule of the sample apps).
"""
import re

from ..blueprint import schema

WHOLE = re.compile(r"^\s*\{\{\s*(.+?)\s*\}\}\s*$", re.S)


class Unsaid(Exception):
    """A part of a rule the language has no words for yet."""


def condition_in_keel(c):
    if "formula" in c:
        return c["formula"]
    for word in ("any", "all"):
        if word in c:
            return {word: [condition_in_keel(x) for x in c[word]]}
    out = {"field": c["field"], "op": c["op"]}
    if "value" in c:
        out["value"] = c["value"]
    return out


def conditions_in_keel(cs):
    return [condition_in_keel(c) for c in cs or []]


def values_in_keel(values):
    out = []
    for key, v in (values or {}).items():
        if isinstance(v, dict):
            raise Unsaid("a value that makes a linked record of its own ({\"name\": …})")
        whole = WHOLE.match(v) if isinstance(v, str) else None
        if whole and "{{" not in whole.group(1):
            out.append({"field": key, "formula": whole.group(1).strip()})
        else:
            out.append({"field": key, "to": v})
    return out


def _with_only_if(out, action):
    if action.get("only_if"):
        out["only_if"] = conditions_in_keel(action["only_if"])
    return out


def _follow_up(out, a):
    for key in ("remind_after_days", "escalate_after_days", "escalate_to"):
        if a.get(key) not in (None, ""):
            out[key] = int(a[key]) if key != "escalate_to" else a[key]
    return out


def _ai(out, a):
    for key in ("read_files", "use_knowledge", "manual"):
        if a.get(key):
            out[key] = True
    if a.get("learn") is False:
        out["learn"] = False
    if a.get("review") and a["review"] != "never":
        out["review"] = a["review"]
        if a.get("reviewer") and a["reviewer"] != "editor":
            out["reviewer"] = a["reviewer"]
    if a.get("reads"):
        out["reads"] = [{"list": r["table"], **({"where": conditions_in_keel(r["where"])} if r.get("where") else {})} for r in a["reads"]]
    return out


def step_in_keel(bp, t, a):
    """One step of a design automation, said in Keel."""
    kind = a.get("type")
    if kind == "update_record":
        out = {"do": "set", "values": values_in_keel(a.get("values"))}
        if a.get("target"):
            out["on"] = a["target"]
    elif kind == "create_record":
        out = {"do": "add", "list": a["table"], "values": values_in_keel(a.get("values")), "copy_same": bool(a.get("copy_matching"))}
    elif kind == "create_for_each":
        source = a.get("source") or {}
        if source.get("field"):  # one for each one chosen in a field
            out = {"do": "add_each", "list": a["table"], "for_each": source["field"], "values": values_in_keel(a.get("values")),
                   "copy_same": bool(a.get("copy_matching"))}
            if a.get("where"):
                out["lines_where"] = conditions_in_keel(a["where"])
            return _with_only_if(out, a)
        if not source.get("via"):
            raise Unsaid("one record for each of something with no link to say which")
        out = {"do": "add_each", "list": a["table"], "lines": source["table"], "link": source["via"],
               "values": values_in_keel(a.get("values")), "copy_same": bool(a.get("copy_matching"))}
        if source.get("of"):
            out["of"] = source["of"]
        if a.get("where"):
            out["lines_where"] = conditions_in_keel(a["where"])
    elif kind == "update_each":
        source = a.get("source") or {}
        if any(isinstance(v, (dict, list)) for v in (a.get("values") or {}).values()):
            raise Unsaid("changing the records each line links to (values on a line's link)")
        out = {"do": "update_each", "list": source.get("table"), "link": source.get("via"), "values": values_in_keel(a.get("values"))}
        if source.get("of"):
            out["of"] = source["of"]
        if a.get("where"):
            out["lines_where"] = conditions_in_keel(a["where"])
    elif kind == "notify":
        out = {"do": "notify", "who": a.get("to") or "owner", "message": a.get("message") or ""}
    elif kind == "send_email":
        to = str(a.get("to") or "")
        out = {"do": "email", "to": to, "subject": a.get("subject") or "", "body": a.get("body") or ""}  # a template is kept as written
        if a.get("attach"):
            out["attach"] = list(a["attach"])
    elif kind == "send_sms":
        to = str(a.get("to") or "")
        out = {"do": "text", "to": to, "message": a.get("message") or ""}
    elif kind == "generate_document":
        out = {"do": "document", "document": a.get("document")}
        if a.get("field"):
            out["save_to"] = a["field"]
        if a.get("target"):
            out["on"] = a["target"]
    elif kind == "task":
        out = {"do": "task", "who": a.get("to") or "editor", "message": a.get("message") or ""}
        for key in ("fields", "not_by"):
            if a.get(key):
                out[key] = list(a[key])
        if a.get("priority"):
            out["priority"] = a["priority"]
        _follow_up(out, a)
        if a.get("flow"):
            raise Unsaid("a task done by a flow")
    elif kind == "request_approval":
        out = {"do": "approval", "who": a.get("approver") or "owner", "message": a.get("message") or "Please approve this."}
        if a.get("approvers"):
            out["approvers"], out["mode"] = list(a["approvers"]), a.get("mode") or "any"
        if a.get("not_by"):
            out["not_by"] = list(a["not_by"])
        if a.get("step_up"):
            out["step_up"] = True
        _follow_up(out, a)
        if a.get("if_rejected"):
            out["if_rejected"] = [step_in_keel(bp, t, x) for x in a["if_rejected"]]
    elif kind == "wait":
        out = {"do": "wait", "days": int(a.get("days") or 1)}
    elif kind == "payment_link":
        out = {"do": "payment_link", "amount": a.get("amount"), "save_to": a.get("save_to")}
        if a.get("paid"):
            out["when_paid"] = values_in_keel(a["paid"])
        for key in ("currency", "description"):
            if a.get(key):
                out[key] = a[key]
    elif kind == "assign":
        out = {"do": "assign", "field": a.get("field"), "among": a.get("from") or "editor"}
    elif kind == "copy_record":
        out = {"do": "copy", "values": values_in_keel(a.get("values"))}
        if a.get("lines") is False:
            out["lines"] = False
    elif kind == "delete_record":
        out = {"do": "delete"}
    elif kind == "run_automation":
        out = {"do": "run", "rule": a.get("automation")}
    elif kind == "webhook":
        out = {"do": "webhook", "url": a.get("url"), "method": a.get("method") or "POST"}
    elif kind == "branch":
        out = {"do": "branch", "ways": [{"label": p.get("label") or "", "when": conditions_in_keel(p.get("when")),
                                         "steps": [step_in_keel(bp, t, x) for x in p.get("steps") or []]} for p in a.get("paths") or []]}
        if a.get("otherwise"):
            out["otherwise"] = [step_in_keel(bp, t, x) for x in a["otherwise"]]
    elif kind == "ai_fill":
        out = _ai({"do": "ai_fill", "field": a.get("field"), "instructions": a.get("instructions") or ""}, a)
    elif kind == "ai_check":
        out = _ai({"do": "ai_check", "field": a.get("field"), "instructions": a.get("instructions") or ""}, a)
        if a.get("notes_field"):
            out["notes_field"] = a["notes_field"]
    elif kind == "ai_extract":
        out = _ai({"do": "ai_extract", "source": a.get("from"), "fields": list(a.get("fields") or [])}, a)
        if a.get("instructions"):
            out["instructions"] = a["instructions"]
    elif kind == "ai_create_record":
        out = _ai({"do": "ai_create", "list": a.get("table"), "instructions": a.get("instructions") or ""}, a)
        if a.get("attach_to"):
            out["attach_to"] = a["attach_to"]
    else:
        raise Unsaid(f"a “{kind}” step")
    return _with_only_if(out, a)


def when_in_keel(bp, a):
    """(the Keel “when”, the conditions left for only_if) for a design automation's trigger and conditions."""
    trig, conditions = a.get("trigger") or {}, list(a.get("conditions") or [])
    kind, table = trig.get("type"), trig.get("table")
    if kind == "email_received":
        out = {"on": "email"}
        for key in ("from_contains", "subject_contains"):
            if trig.get(key):
                out[key] = trig[key]
        if trig.get("has_attachment"):
            out["with_attachment"] = True
        return out, conditions
    if kind == "file_added":
        return {"on": "file", "folder": trig.get("folder") or ""}, conditions
    if kind == "webhook_received":
        return {"on": "webhook", **({"list": table} if table else {})}, conditions
    if kind == "schedule":
        out = {"on": "every", "period": trig.get("every") or "day", "at": trig.get("at") or "08:00"}
        if table:
            out["list"] = table
        if trig.get("weekday") and out["period"] == "week":
            out["weekday"] = trig["weekday"]
        return out, conditions
    if not table:
        raise Unsaid(f"a “{kind}” rule about no list")
    if kind == "record_created":
        return {"on": "added", "list": table}, conditions
    if kind == "record_deleted":
        return {"on": "deleted", "list": table}, conditions
    if kind == "button":
        return {"on": "button", "list": table, "label": trig.get("label") or a.get("name") or "Run"}, conditions
    changes = [c for c in conditions if c.get("op") in ("changed", "changed_to") and "field" in c]
    t = schema.table(bp, table) or {}
    stage = (t.get("workflow") or {}).get("field")
    if kind == "record_created_or_updated":
        first = changes[0] if changes else None
        if first and first["field"] == stage and first["op"] == "changed_to" and not isinstance(first.get("value"), list):
            return {"on": "reaches", "list": table, "stage": first["value"]}, [c for c in conditions if c is not first]
        return {"on": "added_or_changed", "list": table}, conditions
    if kind == "record_updated":
        if changes:
            first = changes[0]
            out = {"on": "changed", "list": table, "field": first["field"]}
            if first["op"] == "changed_to":
                out["to"] = first.get("value")
            return out, [c for c in conditions if c is not first]
        return {"on": "changed", "list": table}, conditions
    raise Unsaid(f"a “{kind}” trigger")


def rule_in_keel(bp, a):
    """A design automation said as a Keel rule (a dict for m.Rule)."""
    when, rest = when_in_keel(bp, a)
    t = schema.table(bp, (a.get("trigger") or {}).get("table") or "")
    out = {"key": a["key"], "name": a.get("name") or a["key"], "when": when, "only_if": conditions_in_keel(rest),
           "steps": [step_in_keel(bp, t, x) for x in a.get("actions") or []]}
    if a.get("enabled") is False:
        out["enabled"] = False
    return out
