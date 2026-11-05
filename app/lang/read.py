"""An app as the builder reads it: its lists, fields, stages, features and rules, in the Keel language's own terms and
as few words as will do, so the AI sees exactly what it may name and change (and much less than the design itself)."""
import json

from ..blueprint import schema
from .design import TYPE_BACK, Design


def _field(d, f):
    ftype = f["type"]
    kind = TYPE_BACK.get(ftype, ftype)
    bits = [f["key"], kind]
    if ftype in ("select", "multi_select"):
        bits.append("[" + ", ".join(f.get("options") or []) + "]")
    elif ftype == "link":
        bits[-1] = f"link→{f.get('table')}" + ("(many)" if f.get("multiple") else "")
    elif ftype == "formula":
        bits[-1] = f"formula({f.get('format') or 'number'}) = {f.get('expression')}"
    elif ftype == "rollup":
        bits[-1] = f"total {f.get('aggregate')} {f.get('table')}.{f.get('field') or ''} via {f.get('via')}".replace(". via", " via")
        if f.get("filters"):
            bits.append("only " + _conds(f["filters"]))
    elif ftype == "lookup":
        bits[-1] = f"lookup {f.get('link')}.{f.get('field')}" if f.get("link") else f"lookup in {f.get('table')}"
    elif ftype == "autonumber":
        bits[-1] = f"auto_number {f.get('prefix') or ''}".strip()
    if f.get("label") and schema.slug(f["label"]) != f["key"]:
        bits.insert(1, f"“{f['label']}”")
    if f.get("required"):
        bits[0] += "*"
    if f.get("feature"):
        bits.append(f"(by {f['feature']})")
    return " ".join(bits)


def _conds(conditions, joined=" and "):
    out = []
    for c in conditions or []:
        if "any" in c or "all" in c:
            word = "any" if "any" in c else "all"
            out.append(f"({_conds(c[word], ' or ' if word == 'any' else ' and ')})")
            continue
        value = c.get("value")
        out.append(f"{c.get('field')} {c.get('op')}" + ("" if value is None else f" {json.dumps(value, ensure_ascii=False)}"))
    return joined.join(out)


def _rule(bp, t, rule):
    """A rule the list's records must keep: "must keep quote.status == 'Approved' “A job can't be…”", or one a gate made
    to refuse a choice ("refused when …"), with the feature that made it."""
    by = next((e["key"] for e in bp.get("features") or [] if rule in ((e.get("made") or {}).get("rules") or {}).get(t["key"], [])), None)
    when = f" while {_conds(rule['when'])}" if rule.get("when") else ""
    said = f"refused when {_conds(rule.get('when'))}" if str(rule.get("check")).strip().lower() == "false" else f"must keep {rule.get('check')}{when}"
    return f"{said} “{rule.get('message')}”" + (f" (by {by})" if by else " (add_validation; remove_validation takes it away)")


def _moves(flow):
    out = []
    for mv in flow.get("transitions") or []:
        s = f"{'/'.join(mv.get('from') or ['any'])}→{mv['to']} “{mv.get('label') or mv['to']}”"
        if mv.get("who"):
            s += f" by {','.join(mv['who'])}"
        if mv.get("requires"):
            s += f" needs {','.join(mv['requires'])}"
        if mv.get("only_if"):
            s += f" if {_conds(mv['only_if'])}"
        out.append(s)
    return "; ".join(out)


def _when(a):
    t = a["trigger"]
    kind = t["type"]
    table = t.get("table") or ""
    if kind == "record_created":
        return f"added {table}"
    if kind == "record_updated":
        return f"changed {table}"
    if kind == "record_created_or_updated":
        return f"added_or_changed {table}"
    if kind == "record_deleted":
        return f"deleted {table}"
    if kind == "button":
        return f"button “{t.get('label')}” on {table}"
    if kind == "schedule":
        return f"every {t.get('every')} {t.get('weekday') or ''} at {t.get('at')} on {table}".replace("  ", " ")
    if kind == "email_received":
        narrow = [f"{k.replace('_contains', '')} has “{t[k]}”" for k in ("from_contains", "subject_contains") if t.get(k)]
        return "email arrives" + (f" ({', '.join(narrow)})" if narrow else "") + (" with attachment" if t.get("has_attachment") else "")
    if kind == "file_added":
        return f"file arrives in {t.get('folder')}"
    return f"{kind} {table}".strip()


def _step(s):
    kind = s.get("type")
    if kind == "update_record":
        return f"set {s.get('target') or 'record'} {json.dumps(s.get('values'), ensure_ascii=False)}" + (f" if {_conds(s['only_if'])}" if s.get("only_if") else "")
    if kind == "create_record":
        return f"add {s.get('table')} {json.dumps(s.get('values'), ensure_ascii=False)}"
    if kind == "create_for_each":
        src = s.get("source") or {}
        return f"add_each {s.get('table')} for each {src.get('table')} via {src.get('via')} {json.dumps(s.get('values'), ensure_ascii=False)}"
    if kind == "notify":
        return f"notify {s.get('to')}: “{s.get('message')}”"
    if kind == "send_email":
        return f"email {s.get('to')}: “{s.get('subject')}”" + (f" attaching {','.join(s['attach'])}" if s.get("attach") else "")
    if kind == "send_sms":
        return f"text {s.get('to')}: “{s.get('message')}”"
    if kind == "generate_document":
        return f"document {s.get('document')}"
    if kind == "request_approval":
        return f"approval by {s.get('approver')}: “{s.get('message')}”"
    if kind == "task":
        return f"task for {s.get('to')}: “{s.get('message')}”"
    if kind == "wait":
        return f"wait {s.get('days') or ''} days".strip()
    review = f" (checked by a person: {s['review']})" if s.get("review") else ""
    if kind == "ai_fill":
        return f"ai_fill {s.get('field')}: “{s.get('instructions')}”{review}"
    if kind == "ai_extract":
        return f"ai_extract {', '.join(s.get('fields') or [])} from {s.get('from')}{review}"
    if kind == "ai_create_record":
        return f"ai_create {s.get('table')}: “{s.get('instructions')}”{review}"
    if kind == "ai_check":
        return f"ai_check into {s.get('field')}: “{s.get('instructions')}”{review}"
    if kind == "assign":
        return f"assign {s.get('field')} among {s.get('from')}"
    if kind == "update_each":
        src = s.get("source") or {}
        return f"update_each {src.get('table')} via {src.get('via')} {json.dumps(s.get('values'), ensure_ascii=False)}"
    if kind == "copy_record":
        return "copy" + (" without lines" if s.get("lines") is False else "")
    if kind == "delete_record":
        return "delete"
    if kind == "run_automation":
        return f"run {s.get('automation')}"
    if kind == "webhook":
        return f"webhook {s.get('method', 'POST')} {s.get('url')}"
    return kind


def describe(bp, with_rules=True):
    """The app in the builder's terms, for its instructions."""
    d = Design(bp or {})
    lines = []
    if not d.lists:
        return "The app is empty: no lists yet."
    lines.append("LISTS (key “label” (singular): fields; * = must be filled in):")
    for t in d.lists:
        head = f"- {t['key']} “{t['label']}” ({t.get('singular')})"
        if t.get("embedded"):
            parent = next((f.get("table") for f in t["fields"] if f["type"] == "link" and f.get("required")), None)
            head += f", lines inside {parent}" if parent else ", lines"
        lines.append(head + ": " + ", ".join(_field(d, f) for f in t["fields"]))
        if t.get("workflow"):
            flow = t["workflow"]
            lines.append(f"    stages ({flow.get('field')}): {' → '.join(d.stages(t))}; moves: {_moves(flow)}")
        if t.get("limits"):
            lines.append(f"    limits: " + "; ".join(f"max {x.get('max')} per {','.join(x.get('per') or [])}" for x in t["limits"]))
        if t.get("lock_if"):
            lines.append(f"    locked when {_conds(t['lock_if'])}")
        for rule in t.get("rules") or []:  # what's refused on saving: every example and change must keep to it
            lines.append("    " + _rule(bp, t, rule))
        if t.get("documents"):
            lines.append(f"    documents: {', '.join(x['key'] for x in t['documents'])}")
    features = bp.get("features") or []
    if features:
        lines.append("FEATURES (change one by sending it again with its key; remove_feature takes it away):")
        for e in features:
            lines.append(f"- {e['key']}: {json.dumps({'do': e['do'], **e.get('params', {})}, ensure_ascii=False)}")
    rules = [a for a in bp.get("automations", []) if not a.get("feature")]
    if with_rules and rules:
        lines.append("RULES (made by hand or by add_rule; change_rule changes one):")
        for a in rules:
            cond = _conds(a.get("conditions"))
            lines.append(f"- {a['key']} “{a['name']}”: {_when(a)}" + (f" if {cond}" if cond else "") + " → " + "; ".join(_step(s) for s in a.get("actions", [])))
    people = schema.teams(bp)
    if people:
        lines.append("TEAMS: " + ", ".join(tm["key"] for tm in people))
    return "\n".join(lines)
