"""The two studios: the data model (every list, its fields and how the lists link, drawn as a diagram and changed by
hand) and workflows (each list's stages, each rule and each step-by-step flow, drawn and changed by hand).

Neither touches the design directly. An edit made here becomes the same changes the builder makes (blueprint ops),
checked the same way and saved as a version that can be gone back to; what can't be changed by hand is asked of the
builder in words. A few edits reach the records too, and say so: renaming a choice (or a stage) renames it on every
record that has it, so nothing is left behind under the old name.
"""
import copy
import re

from .blueprint import schema
from .blueprint.schema import SchemaError, slug

# What a new field can be, as people say it (the calculated kinds need more than a name: the builder makes those).
NEW_TYPES = ["text", "long_text", "number", "money", "percent", "date", "datetime", "checkbox", "select", "multi_select", "email",
             "phone", "url", "address", "link", "user", "file", "image", "signature", "barcode", "rating", "duration", "autonumber",
             "formula"]
# Kinds a field can change between while it has values: they're kept the same way, so nothing is lost or misread.
SAFE_CHANGES = [{"text", "long_text", "rich_text"}, {"number", "money"}, {"date", "datetime"}]
FIELD_SETTINGS = ("label", "help", "required", "unique", "default", "placeholder", "options", "multiple", "expression", "format",
                  "sensitive", "decimals", "unit", "currency", "prefix", "digits", "max", "min", "access", "readonly", "option_colors",
                  "before", "after", "yes_label", "no_label")
SIGNED = {"number", "money", "percent", "formula", "rollup", "duration"}  # what can be coloured by its sign
STEP_TYPES = ["notify", "send_email", "task", "request_approval", "update_record", "create_record", "wait", "assign", "branch",
              "generate_document", "ai_fill", "ai_check", "ai_extract", "ai_create_record", "webhook"]
STEP_WORDS = {"notify": "Tell someone", "send_email": "Send an email", "task": "Ask someone to do something",
              "request_approval": "Ask for approval", "update_record": "Change this record", "create_record": "Add a record",
              "wait": "Wait", "assign": "Give it to someone", "branch": "Go one of several ways", "generate_document": "Make a document",
              "ai_fill": "Have the AI fill in a field", "ai_check": "Have the AI check it", "ai_extract": "Have the AI read a document",
              "ai_create_record": "Have the AI make a record from it", "webhook": "Send to another system"}
TRIGGER_WORDS = {"record_created": "is added", "record_updated": "is changed", "record_created_or_updated": "is added or changed",
                 "record_deleted": "is deleted", "button": "a button is pressed on one", "schedule": "on a schedule"}


def _name(text, what="name", most=80):
    text = re.sub(r"\s+", " ", str(text or "")).strip()[:most]
    if not text:
        raise SchemaError(f"Give it a {what}")
    return text


def _fresh_key(taken, label, fallback="field"):
    base = slug(label) or fallback
    key, n = base, 2
    while key in taken or key in schema.RESERVED:
        key, n = f"{base}_{n}", n + 1
    return key


def _table(bp, key):
    t = schema.table(bp, slug(key or ""))
    if t is None:
        raise SchemaError("That list isn't there any more: reload the page")
    return t


def _field(t, key):
    f = schema.field(t, slug(key or ""))
    if f is None:
        raise SchemaError(f"{t['label']} has no field “{key}” any more: reload the page")
    return f


def _yes(value):
    return value in (True, "true", "yes", "on", 1, "1")


# --- the data model ----------------------------------------------------------------------------------------------------

def data_model(engine, about, counts, positions):
    """The design as the studio draws it, for this person (only the lists and fields they may see). `about(t, f)` is
    (what kind a field is, one line on it), as the setup page says it."""
    shown = engine.visible_blueprint()
    tables = shown.get("tables", [])
    keys = {t["key"] for t in tables}
    out, links = [], []
    for t in tables:
        fields = []
        for f in t["fields"]:
            kind, detail = about(t, f)
            target = f.get("table") if f["type"] == "link" else None
            fields.append({"key": f["key"], "label": f["label"], "type": f["type"], "kind": kind, "detail": detail,
                           "required": bool(f.get("required")), "unique": bool(f.get("unique")), "sensitive": bool(f.get("sensitive")),
                           "ai": bool(f.get("ai")), "help": f.get("help") or "", "link": target if target in keys else None,
                           "multiple": bool(f.get("multiple")), "calculated": f["type"] in schema.CALCULATED,
                           "options": list(f.get("options") or []), "default": f.get("default"), "expression": f.get("expression", ""),
                           "decimals": f.get("decimals"), "unit": f.get("unit", ""), "currency": f.get("currency", ""),
                           "access": f.get("access", ""), "title": f["key"] == t.get("title_field"),
                           "stage": f["key"] == (t.get("workflow") or {}).get("field"), "before": f.get("before", ""),
                           "after": f.get("after", ""), "yes_label": f.get("yes_label", ""), "no_label": f.get("no_label", ""),
                           "option_colors": f.get("option_colors") or {},
                           "ai_instructions": (f.get("ai") or {}).get("instructions", ""), "ai_fillable": f["type"] in schema.AI_FILLABLE,
                           "signs": signs_of(t, f["key"]) if f["type"] in SIGNED else None})
            if target in keys:
                links.append({"from": t["key"], "field": f["key"], "to": target, "many": bool(f.get("multiple")), "label": f["label"]})
        combines = list(dict.fromkeys(a["table"] for a in t.get("combines") or [] if a["table"] in keys))
        out.append({"key": t["key"], "label": t["label"], "singular": t["singular"], "icon": t.get("icon") or "table",
                    "embedded": bool(t.get("embedded")), "module": t.get("module") or "", "count": counts.get(t["key"], 0),
                    "fields": fields, "title_field": t.get("title_field"), "combines": combines})
    combined = [{"from": source, "to": t["key"]} for t in out for source in t["combines"]]
    return {"tables": out, "links": links, "combined": combined, "positions": {k: v for k, v in (positions or {}).items() if k in keys},
            "sections": [{"key": m["key"], "label": m["label"]} for m in shown.get("modules", [])]}


def _sign_rule(rule, key):
    when = rule.get("when") or []
    return (rule.get("field") == key and len(when) == 1 and when[0].get("field") == key and when[0].get("op") in ("greater_than", "less_than")
            and str(when[0].get("value")) in ("0", "0.0"))


def signs_of(t, key):
    """How a number is coloured by its sign: "normal" (above nothing green, below red), "inverted", or ""."""
    rules = [r for r in t.get("colors", []) if _sign_rule(r, key)]
    up = next((r["color"] for r in rules if r["when"][0]["op"] == "greater_than"), None)
    return "normal" if up == "green" else "inverted" if up == "red" else ""


def _signs(t, key, mode):
    """The table's colour rules with this field's sign colours set to `mode` ("", "normal" or "inverted")."""
    rules = [r for r in t.get("colors", []) if not _sign_rule(r, key)]
    if mode in ("normal", "inverted"):
        up, down = ("green", "red") if mode == "normal" else ("red", "green")
        rules += [{"field": key, "when": [{"field": key, "op": "greater_than", "value": 0}], "color": up},
                  {"field": key, "when": [{"field": key, "op": "less_than", "value": 0}], "color": down}]
    return rules


def data_ops(bp, change, filled):
    """An edit made in the data model studio, as blueprint ops. `filled(table, field, value=None)` says how many records
    have a value in a field (or that value): what's in use is never thrown away by a click here.
    Returns (ops, renames): renames are [(table, field, old, new)] to carry over to the records once it's saved."""
    do = change.get("do")
    if do == "add_table":
        label = _name(change.get("label"), "name for the list")
        if any(t["label"].lower() == label.lower() for t in bp.get("tables", [])):
            raise SchemaError(f"There's already a list called {label}")
        key = _fresh_key({t["key"] for t in bp.get("tables", [])}, label, "list")
        first = _name(change.get("first") or "Name", "name for its first field")
        spec = {"table": key, "label": label, "fields": [{"key": _fresh_key(set(), first), "label": first, "type": "text", "required": True}]}
        if change.get("section"):
            spec["module"] = slug(change["section"])
        return [{"op": "create_table", **spec}], []
    if do == "update_table":
        t = _table(bp, change.get("table"))
        updates = {}
        if change.get("label") is not None:
            label = _name(change["label"], "name for the list")
            if label != t["label"] and any(x is not t and x["label"].lower() == label.lower() for x in bp["tables"]):
                raise SchemaError(f"There's already a list called {label}")
            updates["label"] = label
        if change.get("singular"):
            updates["singular"] = _name(change["singular"], "name for one record", 60)
        if change.get("section"):
            updates["module"] = slug(change["section"])
        if change.get("title_field"):
            updates["title_field"] = slug(change["title_field"])
        if not updates:
            raise SchemaError("Nothing to change")
        return [{"op": "update_table", "table": t["key"], **updates}], []
    if do == "delete_table":
        t = _table(bp, change.get("table"))
        return [{"op": "delete_table", "table": t["key"]}], []
    if do in ("add_field", "link"):
        t = _table(bp, change.get("table") or change.get("from"))
        ftype = schema.field_type(change.get("type") or ("link" if do == "link" else "text"))
        target = _table(bp, change.get("to") or change.get("link_to")) if ftype == "link" else None
        label = _name(change.get("label") or (target["singular"] if target else ""), "name for the field")
        if any(f["label"].lower() == label.lower() for f in t["fields"]):
            raise SchemaError(f"{t['label']} already has a field called {label}")
        spec = {"key": _fresh_key({f["key"] for f in t["fields"]}, label), "label": label, "type": ftype}
        if target:
            spec["table"] = target["key"]
            if _yes(change.get("multiple")):
                spec["multiple"] = True
        if ftype in ("select", "multi_select"):
            spec["options"] = _options(change.get("options")) or ["Yes", "No"]
        if ftype == "formula":
            spec["expression"] = str(change.get("expression") or "").strip()
            if not spec["expression"]:
                raise SchemaError("A worked-out field needs its sum, like quantity * price")
        if _yes(change.get("required")):
            spec["required"] = True
        return [{"op": "add_field", "table": t["key"], "field": spec}], []
    if do == "update_field":
        t = _table(bp, change.get("table"))
        f = _field(t, change.get("key"))
        updates = {k: change[k] for k in FIELD_SETTINGS if k in change}
        if "label" in updates:
            updates["label"] = _name(updates["label"], "name for the field")
            if any(x is not f and x["label"].lower() == updates["label"].lower() for x in t["fields"]):
                raise SchemaError(f"{t['label']} already has a field called {updates['label']}")
        for k in ("required", "unique", "multiple", "sensitive", "readonly"):
            if k in updates:
                updates[k] = _yes(updates[k])
        if "options" in updates:
            updates["options"] = _options(updates["options"])
            gone = [o for o in f.get("options") or [] if o not in updates["options"]]
            used = [(o, filled(t["key"], f["key"], o)) for o in gone]
            used = [(o, n) for o, n in used if n]
            if used:
                o, n = used[0]
                raise SchemaError(f"{n} {t['label'].lower() if n != 1 else t['singular'].lower()} {'are' if n != 1 else 'is'} “{o}”: "
                                  f"rename the choice instead, or move {'them' if n != 1 else 'it'} to another first")
            for o in gone:
                _not_used(bp, t, f, o, "choice")
            if not updates["options"]:
                raise SchemaError("Keep at least one choice")
        new_type = schema.field_type(change["type"]) if change.get("type") else f["type"]
        if new_type != f["type"]:
            if (f["type"] in schema.FILES) != (new_type in schema.FILES):
                raise SchemaError("Files are kept apart from the rest: add a new field instead")
            if f["type"] in schema.CALCULATED or new_type in schema.CALCULATED - {"autonumber"} and new_type != "formula":
                raise SchemaError("A field worked out for you can't become one people fill in (or the other way): add a new field instead")
            n = filled(t["key"], f["key"])
            if n and not any({f["type"], new_type} <= family for family in SAFE_CHANGES):
                raise SchemaError(f"{n} {t['label'].lower()} have a {f['label']} already, kept as {f['type'].replace('_', ' ')}: "
                                  f"add a new field instead, so none of them are misread")
            updates["type"] = new_type
            if new_type in ("select", "multi_select") and "options" not in updates:
                updates["options"] = list(f.get("options") or []) or ["Yes", "No"]
            if new_type == "link" and change.get("link_to"):
                updates["table"] = _table(bp, change["link_to"])["key"]
        for k in ("default", "unit", "currency", "help", "placeholder", "access", "expression"):
            if k in updates and updates[k] in (None, ""):
                updates[k] = None
        for k in ("before", "after", "yes_label", "no_label"):
            if k in updates and not str(updates[k] or "").strip():
                updates[k] = None
        if "ai" in change:  # the AI fills it in (with these instructions), or it stops: people fill it in
            said = str(change.get("ai") or "").strip()
            if said and f["type"] not in schema.AI_FILLABLE:
                raise SchemaError(f"The AI can't fill in {f['label']}: it fills in text, numbers, dates, choices, yes-or-no and links to other records")
            updates["ai"] = {**(f.get("ai") or {}), "instructions": said[:1000]} if said else None
        if "option_colors" in updates:
            updates["option_colors"] = {o: c for o, c in (updates["option_colors"] or {}).items() if c in schema.CHOICE_COLOURS} or None
        out = []
        if "signs" in change and f["type"] in SIGNED and change["signs"] != signs_of(t, f["key"]):
            out.append({"op": "update_table", "table": t["key"], "colors": _signs(t, f["key"], change["signs"]) or None})
        if updates:
            out.insert(0, {"op": "update_field", "table": t["key"], "key": f["key"], "changes": _without_empty(f, updates)})
        if not out:
            raise SchemaError("Nothing to change")
        return out, []
    if do == "rename_option":
        t = _table(bp, change.get("table"))
        f = _field(t, change.get("key"))
        old, new = str(change.get("old") or ""), _name(change.get("new"), "new name for the choice")
        if old not in (f.get("options") or []):
            raise SchemaError(f"{f['label']} has no choice “{old}” any more: reload the page")
        if new in f["options"] and new != old:
            raise SchemaError(f"{f['label']} already has a choice “{new}”")
        return [{"op": "rename_choice", "table": t["key"], "key": f["key"], "old": old, "new": new}], []
    if do == "remove_field":
        t = _table(bp, change.get("table"))
        f = _field(t, change.get("key"))
        if f["key"] == (t.get("workflow") or {}).get("field"):
            raise SchemaError(f"{f['label']} holds the stages of {t['label']}: take the stages off in Processes first")
        if len(t["fields"]) == 1:
            raise SchemaError(f"{t['label']} needs at least one field")
        return [{"op": "remove_field", "table": t["key"], "key": f["key"]}], []
    if do == "order_fields":
        t = _table(bp, change.get("table"))
        keys = [slug(k) for k in change.get("keys") or []]
        if sorted(keys) != sorted(f["key"] for f in t["fields"]):
            raise SchemaError("The fields changed while you were moving them: reload the page")
        return [{"op": "order_fields", "table": t["key"], "keys": keys}], []
    raise SchemaError("That can't be changed here")


def _options(raw):
    if isinstance(raw, str):
        raw = raw.split("\n")
    out = []
    for o in raw or []:
        o = re.sub(r"\s+", " ", str(o or "")).strip()[:80]
        if o and o not in out:
            out.append(o)
    return out


def _without_empty(f, updates):
    """Settings cleared in the studio are taken off the field (ops merges what's given over what's there)."""
    return {k: v for k, v in updates.items() if not (v is None and k not in f)}


# --- renaming a choice everywhere it's used ------------------------------------------------------------------------------

def rename_choice(bp, table, key, old, new):
    """A choice of a field renamed in the design: its options, the stages that use it, and every condition, value and
    move that names it for this list. Returns how many places said it. (The records are renamed by the caller.)"""
    t = schema.table(bp, table)
    f = schema.field(t, key)
    said = 0
    f["options"] = [new if o == old else o for o in f["options"]]
    if old in (f.get("option_colors") or {}):
        f["option_colors"][new] = f["option_colors"].pop(old)
    if f.get("default") == old:
        f["default"], said = new, said + 1
    if isinstance(f.get("default"), list):
        f["default"] = [new if o == old else o for o in f["default"]]
    for by in (f.get("options_by") or {}).get("options", {}).values():
        by[:] = [new if o == old else o for o in by]
    flow = t.get("workflow")
    if flow and flow["field"] == key:
        if flow.get("start") == old:
            flow["start"], said = new, said + 1
        for m in flow["transitions"]:
            if old in m["from"] or m["to"] == old or old in m.get("not_by", []):
                said += 1
            m["from"] = [new if s == old else s for s in m["from"]]
            m["to"] = new if m["to"] == old else m["to"]
            if m.get("not_by"):
                m["not_by"] = [new if s == old else s for s in m["not_by"]]
            said += _rename_in_conditions(m.get("only_if"), key, old, new)
    for name in ("lock_if", "colors", "rules", "views", "row_access", "approve_changes", "sealed", "journey"):
        said += _rename_in_conditions(t.get(name), key, old, new)
    for a in bp.get("automations", []):
        if a["trigger"].get("table") != table:
            continue
        said += _rename_in_conditions(a["conditions"], key, old, new)
        for step in schema.steps_of(a["actions"]):
            said += _rename_in_conditions({k: v for k, v in step.items() if k in ("only_if", "paths", "until_met")}, key, old, new)
            for values in (step.get("values") if step["type"] == "update_record" and not step.get("target") else None, step.get("paid")):
                if isinstance(values, dict) and values.get(key) == old:
                    values[key], said = new, said + 1
    for a in bp.get("automations", []):  # records made in this list by other lists' rules
        for step in schema.steps_of(a["actions"]):
            if step["type"] in ("create_record", "create_for_each") and step.get("table") == table and (step.get("values") or {}).get(key) == old:
                step["values"][key], said = new, said + 1
    for flow_ in bp.get("flows") or []:
        if flow_["table"] != table:
            continue
        said += _rename_in_conditions(flow_.get("when"), key, old, new)
        for step in flow_["steps"]:
            if step["kind"] == "decide" and step.get("field") == key:
                for o in step["options"]:
                    if o["value"] == old:
                        o["value"], said = new, said + 1
    for p in bp.get("pages", []):
        for w in p.get("widgets", []):
            if w.get("table") == table:
                said += _rename_in_conditions(w, key, old, new)
    return said


def renamed_choices(before, after):
    """Choices renamed in place between two versions of a design: [(table, field, old, new)]. A choice counts as renamed
    when its field kept the same number of choices and it alone changed at its place (Shipped → Dispatched, everything
    else as it was), whoever made the change: a studio, the builder, a published test copy, a suggestion."""
    was = {t["key"]: t for t in (before or {}).get("tables", [])}
    out = []
    for t in (after or {}).get("tables", []):
        for f in t["fields"]:
            if f["type"] not in ("select", "multi_select") or t["key"] not in was:
                continue
            old = schema.field(was[t["key"]], f["key"])
            if not old or old["type"] != f["type"] or len(old.get("options") or []) != len(f.get("options") or []):
                continue
            gone = [o for o in old["options"] if o not in f["options"]]
            came = [o for o in f["options"] if o not in old["options"]]
            moved = [(a, b) for a, b in zip(old["options"], f["options"]) if a != b]
            if gone and len(gone) == len(came) == len(moved) and all(a in gone and b in came for a, b in moved):
                out += [(t["key"], f["key"], a, b) for a, b in moved]
    return out


def rename_records(ws, table, key, old, new):
    """The records (the bin's too, so they come back right) that have the old choice given the new name. Returns how many."""
    from .extensions import db
    from .models import Record
    n = 0
    for r in Record.query.filter_by(workspace_id=ws.id, table=table).all():
        v = (r.data or {}).get(key)
        if v == old:
            r.data = {**r.data, key: new}
        elif isinstance(v, list) and old in v:
            r.data = {**r.data, key: [new if x == old else x for x in v]}
        else:
            continue
        n += 1
    db.session.flush()
    return n


def remember_renames(ws, renames):
    """Choices renamed by this version (the workspace's latest), kept so that going back to before it, by any way (a
    studio's Take it back, Versions' Go back, the builder's undo), names the records as they were."""
    if not renames:
        return
    from .extensions import db
    kept = dict(((ws.settings or {}).get("studio") or {}).get("renamed") or {})
    kept[str(ws.version)] = [list(r) for r in renames]
    kept = dict(sorted(kept.items(), key=lambda kv: int(kv[0]))[-40:])
    ws.settings = {**(ws.settings or {}), "studio": {**((ws.settings or {}).get("studio") or {}), "renamed": kept}}
    db.session.commit()


def rename_back(ws, to):
    """Going back to version `to`: the choices renamed by later versions get their old names back on the records, the
    latest first. Returns how many records."""
    from .extensions import db
    kept = dict(((ws.settings or {}).get("studio") or {}).get("renamed") or {})
    undone = sorted((v for v in kept if int(v) > to), key=int, reverse=True)
    n = 0
    for v in undone:
        for table, key, old, new in reversed(kept.pop(v)):
            n += rename_records(ws, table, key, new, old)
    if undone:
        ws.settings = {**(ws.settings or {}), "studio": {**((ws.settings or {}).get("studio") or {}), "renamed": kept}}
        db.session.commit()
    return n


def _rename_in_conditions(node, key, old, new):
    """Every condition (a {"field", "op", "value"}) on this field naming the old choice, anywhere inside `node`."""
    n = 0
    if isinstance(node, list):
        for x in node:
            n += _rename_in_conditions(x, key, old, new)
    elif isinstance(node, dict):
        if node.get("field") == key and "op" in node:
            value = node.get("value")
            if value == old:
                node["value"], n = new, n + 1
            elif isinstance(value, list) and old in value:
                node["value"], n = [new if v == old else v for v in value], n + 1
        for k, v in node.items():
            if isinstance(v, (list, dict)) and k != "value":
                n += _rename_in_conditions(v, key, old, new)
    return n


# --- workflows: what there is ------------------------------------------------------------------------------------------

def workflow_items(engine):
    """Everything with a flow to it, grouped by the list it's about: its stages, its rules, its step-by-step flows."""
    shown = {t["key"]: t for t in engine.visible_blueprint().get("tables", [])}
    groups = {}

    def group(table):
        key = table if table in shown else ""
        return groups.setdefault(key, {"table": key, "label": shown[key]["label"] if key else "Across the app",
                                       "icon": shown[key].get("icon") or "table" if key else "globe", "items": []})
    for t in shown.values():
        if t.get("workflow"):
            group(t["key"])["items"].append({"id": f"stages:{t['key']}", "kind": "stages", "label": f"{t['singular']} stages",
                                             "about": " → ".join(schema.stages(t)[:5]) + ("…" if len(schema.stages(t)) > 5 else "")})
    for a in engine.bp.get("automations", []):
        table = a["trigger"].get("table")
        if table and table not in shown:
            continue
        group(table)["items"].append({"id": f"rule:{a['key']}", "kind": "rule", "label": a["name"], "on": a.get("enabled", True)})
    for f in engine.bp.get("flows") or []:
        if f["table"] in shown:
            group(f["table"])["items"].append({"id": f"flow:{f['key']}", "kind": "flow", "label": f["label"],
                                               "about": f"{len(f['steps'])} screens"})
    order = [k for k in shown if k in groups] + ([""] if "" in groups else [])
    return [groups[k] for k in order]


def stage_diagram(engine, t, counts_by_stage):
    from .engine import automations
    flow = t["workflow"]
    names = schema.stages(t)
    start = schema.start_stage(t) or (names[0] if names else None)
    rules = {}
    for a in engine.bp.get("automations", []):
        if a["trigger"].get("table") != t["key"]:
            continue
        for c in a["conditions"]:
            if c.get("field") == flow["field"] and c.get("op") in ("changed_to", "equals") and c.get("value") in names:
                rules.setdefault(c["value"], []).append({"key": a["key"], "name": a["name"], "on": a.get("enabled", True)})
    set_by = {}
    for a in engine.bp.get("automations", []):
        for step in schema.steps_of(a["actions"]):
            value = (step.get("values") or {}).get(flow["field"]) if step["type"] == "update_record" and a["trigger"].get("table") == t["key"] else None
            if value in names:
                set_by.setdefault(value, []).append(a["name"])
    moves = []
    for i, m in enumerate(flow["transitions"]):
        moves.append({"i": i, "from": list(m["from"]), "to": m["to"], "label": m.get("label") or f"Move to {m['to']}",
                      "who": [automations._who(engine, t["key"], w) for w in m.get("who", [])],
                      "who_keys": list(m.get("who", [])), "requires": [(schema.field(t, k) or {"label": k})["label"] for k in m.get("requires", [])],
                      "requires_keys": list(m.get("requires", [])),
                      "only_if": automations.automations_words(engine, m["only_if"], t["key"]) if m.get("only_if") else "",
                      "step_up": bool(m.get("step_up"))})
    f = schema.field(t, flow["field"])
    return {"kind": "stages", "table": t["key"], "label": f"{t['singular']} stages", "field": flow["field"], "field_label": f["label"],
            "singular": t["singular"], "plural": t["label"],
            "stages": [{"name": n, "start": n == start, "count": counts_by_stage.get(n, 0), "ends": bool(schema.ENDED.search(n) or schema.STOPS.search(n)),
                        "no": schema.says_no(n), "rules": rules.get(n, []), "set_by": sorted(set(set_by.get(n, [])))} for n in names],
            "moves": moves, "who": who_choices(engine, t), "fields": [{"key": x["key"], "label": x["label"]} for x in t["fields"]
                                                                       if x["type"] in schema.STORED and x["key"] != flow["field"]]}


def who_choices(engine, t):
    """Who a move or a step can be for, in the words people use."""
    out = [{"value": "editor", "label": "Anyone on the team"}, {"value": "admin", "label": "Admins"},
           {"value": "owner", "label": "The owner"}, {"value": "creator", "label": f"Whoever added the {t['singular'].lower()}" if t else "Whoever added it"}]
    out += [{"value": tm["key"], "label": f"Someone in {tm['name']}"} for tm in schema.teams(engine.bp)]
    if t:
        out += [{"value": f["key"], "label": f"The {t['singular'].lower()}'s {f['label']}"} for f in t["fields"] if f["type"] == "user"]
    return out


def counts_by_stage(engine, t):
    flow = t.get("workflow")
    out = {}
    if flow:
        for r in engine.rows(t["key"]):
            v = (r.data or {}).get(flow["field"])
            if v not in (None, ""):
                out[v] = out.get(v, 0) + 1
    return out


def rule_diagram(engine, a, runs):
    """A rule as a flowchart: when it starts, what it checks, then each step (a branch's ways side by side), each with
    how often it ran lately. Each step carries its place ("at"), as the runs record it."""
    from .engine import automations
    words = automations.describe(engine, a)
    table = a["trigger"].get("table")
    counts = step_counts(runs)

    def nodes(actions, prefix):
        out = []
        for i, step in enumerate(actions or []):
            at = f"{prefix}{i}"
            plain = {k: v for k, v in step.items() if k not in ("if_rejected", "review", "reviewer")}
            node = {"at": at, "type": step["type"], "kind": _step_kind(engine, step, table),
                    "text": automations.describe_action(engine, a, plain) if step["type"] != "branch" else "Go one of these ways",
                    "counts": counts.get(at, {}), "editable": step["type"] in STEP_TYPES, "step": step,
                    "only_if": automations.automations_words(engine, step["only_if"], table) if step.get("only_if") else ""}
            if step["type"] == "task" and len(step.get("fields") or []) == 1:  # a job by hand the AI could take over
                f = schema.field(engine.tables.get(table) or {"fields": []}, step["fields"][0])
                node["ai_could"] = {"field": (f or {"label": step["fields"][0]})["label"]}
            if step["type"] in schema.AI_STEPS:  # the AI at work: who checks it, and how it's done when they have
                mode = step.get("review") or "never"
                record = automations.track_record(engine, a["key"], at)
                node["ai"] = {"review": mode, "words": automations.REVIEW_WORDS[mode], "manual": bool(step.get("manual")),
                              "checked": record["checked"], "right": record["right"], "unchecked": record["unchecked"],
                              "share": record["share"], "could_trust": mode == "always" and record["checked"] >= 20 and record["lower"] >= 0.9}
            if step["type"] == "branch":
                node["ways"] = [{"label": (p.get("label") or "") or "If " + automations._path_words(engine, p, table), "when": p["when"], "n": n,
                                 "into": f"{at}.{n}.", "steps": nodes(p["steps"], f"{at}.{n}.")} for n, p in enumerate(step["paths"])]
                node["ways"].append({"label": "Anything else", "into": f"{at}.o.", "n": "o", "steps": nodes(step.get("otherwise"), f"{at}.o.")})
            elif step["type"] == "request_approval":
                node["ways"] = [{"label": "If they say no (then it stops)", "into": f"{at}.r.", "n": "r", "steps": nodes(step.get("if_rejected"), f"{at}.r.")}]
            out.append(node)
        return out
    t = engine.tables.get(table) if table else None
    last = runs[0] if runs else None
    return {"kind": "rule", "key": a["key"], "name": a["name"], "on": a.get("enabled", True), "table": table or "",
            "when": words["when"], "trigger": a["trigger"], "conditions": a["conditions"], "only_if": words["only_if"],
            "steps": nodes(a["actions"], ""), "runs": {"total": len(runs), "failed": sum(1 for r in runs if r.status == "failed"),
                                                       "last": last.created_at.isoformat() if last else None},
            "who": who_choices(engine, t), "tables": [{"key": x["key"], "label": x["label"], "singular": x["singular"]} for x in engine.bp["tables"]],
            "documents": [{"key": d["key"], "label": d["label"]} for d in (t or {}).get("documents", [])]}


def _step_kind(engine, step, table):
    """What a step does, named for what it makes or changes (“Add a job”, “Change its enquiry”), else its kind."""
    from .engine.automations import _article
    made, here = engine.tables.get(step.get("table") or ""), engine.tables.get(table or "")
    if step["type"] in ("create_record", "ai_create_record", "copy_record") and made:
        return f"{'Have the AI add' if step['type'] == 'ai_create_record' else 'Add'} {_article(made['singular'])} {made['singular'].lower()}"
    if step["type"] == "create_for_each" and made:
        return f"Add {made['label'].lower()}, one for each"
    if step["type"] == "update_record" and here:
        link = schema.field(here, step["target"]) if step.get("target") else None
        return f"Change its {link['label'].lower()}" if link else f"Change the {here['singular'].lower()}"
    return STEP_WORDS.get(step["type"]) or MORE_STEP_WORDS.get(step["type"]) or step["type"].replace("_", " ").capitalize()


MORE_STEP_WORDS = {"update_each": "Change each of them", "delete_record": "Delete it", "run_automation": "Run another rule",
                   "payment_link": "Make a payment link", "create_for_each": "Add one for each", "copy_record": "Copy it"}


def step_counts(runs):
    """{at: {"ran": n, "skipped": n, "stopped": n}} over these runs: how each step of a rule has gone lately."""
    out = {}
    for run in runs:
        log = run.steps or []
        for n, entry in enumerate(log):
            at = str(entry.get("at", ""))
            c = out.setdefault(at, {"ran": 0, "skipped": 0, "stopped": 0})
            if entry.get("skipped"):
                c["skipped"] += 1
            elif entry.get("done"):
                c["ran"] += 1
            elif run.status == "failed" and n == len(log) - 1:
                c["stopped"] += 1
    return out


def flow_diagram(engine, flow):
    from .engine import automations
    t = engine.tables.get(flow["table"]) or {"label": flow["table"], "singular": flow["table"], "fields": []}
    label = lambda k: (schema.field(t, k) or {"label": k})["label"]  # noqa: E731
    steps = []
    for s in flow["steps"]:
        detail = {"form": lambda: "Fill in " + ", ".join(label(k) for k in s.get("fields", [])),
                  "photo": lambda: f"Kept in {label(s['field'])}", "signature": lambda: f"Kept in {label(s['field'])}",
                  "scan": lambda: f"Each code becomes a line of {(engine.tables.get(s['into']['table']) or {'label': s['into']['table']})['label']}",
                  "pick": lambda: f"Count off its {(engine.tables.get(s['lines']['table']) or {'label': s['lines']['table']})['label'].lower()}",
                  "decide": lambda: " / ".join(o["label"] for o in s["options"]),
                  "print": lambda: f"Prints {s.get('document') or 'its label'}"}.get(s["kind"], lambda: "")()
        steps.append({"key": s["key"], "kind": s["kind"], "label": s["label"], "text": s.get("text", ""), "detail": detail,
                      "fields": s.get("fields", []), "options": s.get("options", [])})
    return {"kind": "flow", "key": flow["key"], "label": flow["label"], "table": flow["table"],
            "starts": f"From a {t['singular'].lower()}'s page" if flow["start"] == "record" else "From the menu: it makes a new " + t["singular"].lower(),
            "steps": steps, "then": [automations.describe_action(engine, {"trigger": {"table": flow["table"]}, "key": flow["key"], "name": flow["label"]}, x)
                                     for x in flow.get("then", [])],
            "fields": [{"key": x["key"], "label": x["label"]} for x in t["fields"] if x["type"] in schema.STORED]}


# --- workflows: changing them ------------------------------------------------------------------------------------------

def stage_ops(bp, change, filled):
    """An edit to a list's stages, as ops. Returns (ops, renames) like data_ops."""
    t = _table(bp, change.get("table"))
    do = change.get("do")
    if do == "add_stages":  # a list without stages gets them: a choice field's options, one after the other
        if t.get("workflow"):
            raise SchemaError(f"{t['label']} has stages already")
        key = slug(change.get("field") or "")
        f = schema.field(t, key) if key else None
        ops = []
        if f is None or f["type"] != "select":
            names = _options(change.get("stages")) or ["New", "In progress", "Done"]
            key = _fresh_key({x["key"] for x in t["fields"]}, "Stage" if schema.field(t, "status") else "Status")
            ops.append({"op": "add_field", "table": t["key"], "field": {"key": key, "label": schema.label_of(key), "type": "select",
                                                                         "options": names, "default": names[0]}})
        else:
            names = list(f["options"])
        moves = [{"from": [a], "to": b, "label": f"Move to {b}"} for a, b in zip(names, names[1:])]
        ops.append({"op": "update_table", "table": t["key"], "workflow": {"field": key, "start": names[0], "transitions": moves}})
        return ops, []
    flow = copy.deepcopy(t.get("workflow"))
    if not flow:
        plain = schema.field(t, slug(change.get("field") or ""))
        if plain is None or plain["type"] != "select" or do not in ("add_stage", "rename_stage", "remove_stage", "order_stages"):
            raise SchemaError(f"{t['label']} has no stages yet")
        return _plain_stage_ops(bp, t, plain, change, filled), []
    f = _field(t, flow["field"])
    names = list(f["options"])
    if do == "remove_stages":
        return [{"op": "update_table", "table": t["key"], "workflow": None}], []
    if do == "add_stage":
        name = _name(change.get("name"), "name for the stage", 60)
        if name in names:
            raise SchemaError(f"There's a stage called {name} already")
        after = change.get("after")
        at = names.index(after) + 1 if after in names else len(names)
        names.insert(at, name)
        if at > 0 and _yes(change.get("move", True)):
            flow["transitions"].append({"from": [names[at - 1]], "to": name, "label": f"Move to {name}"})
            if at + 1 < len(names):  # put between two: from it, on to the next, as from the one before
                flow["transitions"].append({"from": [name], "to": names[at + 1], "label": f"Move to {names[at + 1]}"})
        return _stage_change(t, f, names, flow), []
    if do == "rename_stage":
        old = change.get("old")
        if old not in names:
            raise SchemaError("That stage isn't there any more: reload the page")
        new = _name(change.get("new"), "new name for the stage", 60)
        if new in names and new != old:
            raise SchemaError(f"There's a stage called {new} already")
        return [{"op": "rename_choice", "table": t["key"], "key": f["key"], "old": old, "new": new}], []
    if do == "remove_stage":
        name = change.get("name")
        if name not in names:
            raise SchemaError("That stage isn't there any more: reload the page")
        n = filled(t["key"], f["key"], name)
        if n:
            raise SchemaError(f"{n} {t['label'].lower() if n != 1 else t['singular'].lower()} {'are' if n != 1 else 'is'} at {name}: "
                              f"move {'them' if n != 1 else 'it'} on first")
        _not_used(bp, t, f, name)
        if len(names) <= 2:
            raise SchemaError("Stages need at least two: take the stages off instead")
        names.remove(name)
        moves = []
        for m in flow["transitions"]:
            if m["to"] == name:
                continue
            if name in m["from"]:
                m["from"] = [s for s in m["from"] if s != name]
                if not m["from"]:
                    continue
            moves.append(m)
        flow["transitions"] = moves
        if flow.get("start") == name:
            flow["start"] = names[0]
        return _stage_change(t, f, names, flow), []
    if do == "order_stages":
        order = [str(x) for x in change.get("names") or []]
        if sorted(order) != sorted(names):
            raise SchemaError("The stages changed while you were moving them: reload the page")
        return _stage_change(t, f, order, flow), []
    if do == "set_start":
        if change.get("name") not in names:
            raise SchemaError("That stage isn't there any more: reload the page")
        flow["start"] = change["name"]
        return [{"op": "update_table", "table": t["key"], "workflow": flow}], []
    if do in ("add_move", "update_move"):
        move = _move(t, names, change)
        if do == "add_move":
            flow["transitions"].append(move)
        else:
            i = int(change.get("i", -1))
            if not 0 <= i < len(flow["transitions"]):
                raise SchemaError("That move isn't there any more: reload the page")
            flow["transitions"][i] = {**{k: v for k, v in flow["transitions"][i].items() if k in ("not_by", "step_up", "only_if")}, **move}
        return [{"op": "update_table", "table": t["key"], "workflow": flow}], []
    if do == "remove_move":
        i = int(change.get("i", -1))
        if not 0 <= i < len(flow["transitions"]):
            raise SchemaError("That move isn't there any more: reload the page")
        if len(flow["transitions"]) == 1:
            raise SchemaError("Stages need at least one way to move on: add another first, or take the stages off")
        flow["transitions"].pop(i)
        return [{"op": "update_table", "table": t["key"], "workflow": flow}], []
    raise SchemaError("That can't be changed here")


def _not_used(bp, t, f, value, what="stage"):
    """Refuse to take away a stage (or choice) a rule waits for, checks or sets: taking it away would quietly stop the
    rule (a job's invoice never made). The rule is named, to change or delete first."""
    def conds(cs, table):
        return any(isinstance(c, dict) and table == t["key"] and c.get("field") == f["key"]
                   and (c.get("value") == value or (isinstance(c.get("value"), list) and value in c["value"])) for c in cs or [])

    def steps(actions, table):
        for s in actions or []:
            if not isinstance(s, dict):
                continue
            to = s.get("table") if s.get("type") in ("create_record", "create_for_each", "update_each") else table
            if s.get("type") == "update_record" and s.get("target"):
                link = schema.field(next((x for x in bp.get("tables", []) if x["key"] == table), {"fields": []}), s["target"])
                to = (link or {}).get("table")
            if (to == t["key"] and (s.get("values") or {}).get(f["key"]) == value) or conds(s.get("only_if"), table):
                return True
            if steps(s.get("otherwise"), table) or steps(s.get("if_rejected"), table) or any(
                    conds(p.get("when"), table) or steps(p.get("steps"), table) for p in s.get("paths") or [] if isinstance(p, dict)):
                return True
        return False
    for a in bp.get("automations", []):
        table = a["trigger"].get("table")
        if conds(a.get("conditions"), table) or steps(a.get("actions"), table):
            raise SchemaError(f"The rule “{a['name']}” uses the {what} {value}: change or delete that rule first, so nothing stops working "
                              f"without you knowing")


def _plain_stage_ops(bp, t, f, change, filled):
    """The stages of a list whose Status is a plain choice (anyone picks any): changed as its choices, in order."""
    do, names = change.get("do"), list(f["options"])
    if do == "add_stage":
        name = _name(change.get("name"), "name for the stage", 60)
        if name in names:
            raise SchemaError(f"There's a stage called {name} already")
        after = change.get("after")
        names.insert(names.index(after) + 1 if after in names else len(names), name)
    elif do == "rename_stage":
        old, new = change.get("old"), _name(change.get("new"), "new name for the stage", 60)
        if old not in names:
            raise SchemaError("That stage isn't there any more: reload the page")
        if new in names and new != old:
            raise SchemaError(f"There's a stage called {new} already")
        return [{"op": "rename_choice", "table": t["key"], "key": f["key"], "old": old, "new": new}]
    elif do == "remove_stage":
        name = change.get("name")
        if name not in names:
            raise SchemaError("That stage isn't there any more: reload the page")
        n = filled(t["key"], f["key"], name)
        if n:
            raise SchemaError(f"{n} {t['label'].lower() if n != 1 else t['singular'].lower()} {'are' if n != 1 else 'is'} at {name}: "
                              f"move {'them' if n != 1 else 'it'} on first")
        _not_used(bp, t, f, name)
        names.remove(name)
    else:
        order = [str(x) for x in change.get("names") or []]
        if sorted(order) != sorted(names):
            raise SchemaError("The stages changed while you were moving them: reload the page")
        names = order
    return [{"op": "update_field", "table": t["key"], "key": f["key"], "changes": {"options": names}}]


def _stage_change(t, f, names, flow):
    return [{"op": "update_field", "table": t["key"], "key": f["key"], "changes": {"options": names}},
            {"op": "update_table", "table": t["key"], "workflow": flow}]


def _move(t, names, change):
    to = change.get("to")
    if to not in names:
        raise SchemaError("Choose the stage it moves to")
    start = [s for s in change.get("from") or [] if s in names and s != to]
    move = {"from": start, "to": to, "label": re.sub(r"\s+", " ", str(change.get("label") or "")).strip()[:60] or f"Move to {to}"}
    who = [w for w in change.get("who") or [] if w]
    if who:
        move["who"] = who
    needs = [slug(k) for k in change.get("requires") or [] if schema.field(t, slug(k))]
    if needs:
        move["requires"] = needs
    return move


def rule_ops(bp, change):
    """An edit to a rule (an automation), as ops: the rule is changed as a whole and checked again."""
    do = change.get("do")
    if do == "add_rule":
        name = _name(change.get("name"), "name for the rule")
        key = _fresh_key({a["key"] for a in bp.get("automations", [])}, name, "rule")
        trigger = change.get("trigger") or {}
        step = change.get("step") or {"type": "notify", "message": f"{name}: {{{{record}}}}", "to": "admin"}
        made = schema.normalize_automation({"key": key, "name": name, "trigger": trigger, "conditions": change.get("conditions") or [], "actions": [_step(step)]})
        same = next((a for a in bp.get("automations", []) if (a["trigger"], a["conditions"], a["actions"]) == (made["trigger"], made["conditions"], made["actions"])), None)
        if same:  # pressed twice, or made before: one is enough
            raise SchemaError(f"There's a rule that does exactly this already: “{same['name']}”")
        return [{"op": "add_automation", "automation": {"key": key, "name": name, "trigger": trigger,
                                                         "conditions": change.get("conditions") or [], "actions": [_step(step)]}}], key
    a = copy.deepcopy(next((x for x in bp.get("automations", []) if x["key"] == change.get("key")), None))
    if a is None:
        raise SchemaError("That rule isn't there any more: reload the page")
    if do == "remove_rule":
        return [{"op": "remove_automation", "key": a["key"]}], None
    if do == "rename":
        a["name"] = _name(change.get("name"), "name for the rule")
    elif do == "set_enabled":
        a["enabled"] = _yes(change.get("on"))
    elif do == "set_trigger":
        raw = dict(change.get("trigger") or {})
        field, to = raw.pop("field", None), raw.pop("to", None)
        a["trigger"] = schema.normalize_trigger(raw)
        if field or a["trigger"]["type"] not in ("record_updated", "record_created_or_updated"):  # what it waits for, said again
            a["conditions"] = [c for c in a["conditions"] if c.get("op") not in ("changed", "changed_to")]
        if field and to:
            a["conditions"].insert(0, schema.normalize_condition({"field": field, "op": "changed_to", "value": to}))
    elif do == "set_conditions":
        a["conditions"] = [schema.normalize_condition(c) for c in change.get("conditions") or []]
    elif do == "add_step":
        steps = _steps_at(a["actions"], change.get("into") or "")
        at = int(change.get("index", len(steps)))
        steps.insert(max(0, min(at, len(steps))), _step(change.get("step")))
    elif do == "update_step":
        steps, i = _step_at(a["actions"], change.get("at"))
        old = steps[i]
        new = _step(change.get("step"))
        for kept in ("paths", "otherwise", "if_rejected", "review", "reviewer", "only_if"):  # what the form doesn't show stays
            if kept in old and kept not in (change.get("step") or {}):
                new[kept] = old[kept]
        steps[i] = new
    elif do == "set_ai_on":  # the AI switched off (a person does its job instead, as a task) or back on
        steps, i = _step_at(a["actions"], change.get("at"))
        if steps[i]["type"] not in schema.AI_STEPS:
            raise SchemaError("Only an AI step can be switched off or on")
        if _yes(change.get("on")):
            steps[i].pop("manual", None)
        else:
            steps[i]["manual"] = True
    elif do == "ai_take_over":  # a job people do by hand (a task with one field to fill) done by the AI, checked by them
        steps, i = _step_at(a["actions"], change.get("at"))
        step = steps[i]
        if step["type"] != "task" or len(step.get("fields") or []) != 1:
            raise SchemaError("The AI can take over a task that asks someone to fill in one field")
        steps[i] = {"type": "ai_fill", "field": step["fields"][0], "review": "always", "reviewer": step.get("to") or "editor",
                    "instructions": str(change.get("instructions") or "").strip()[:2000] or step.get("message") or "Fill this in."}
    elif do == "set_review":  # who checks an AI step's work: every time, when it isn't sure, or nobody
        from .engine import automations
        changed = automations.with_review(a, str(change.get("at") or ""), change.get("mode"))
        if changed is None:
            raise SchemaError("Only an AI step's checking can be set, to every time, when it isn't sure, or nobody")
        a = changed
    elif do == "remove_step":
        steps, i = _step_at(a["actions"], change.get("at"))
        if steps is a["actions"] and len(steps) == 1:
            raise SchemaError("A rule needs at least one step: remove the rule instead")
        steps.pop(i)
    elif do == "move_step":
        steps, i = _step_at(a["actions"], change.get("at"))
        j = i + (1 if int(change.get("by", 1)) > 0 else -1)
        if not 0 <= j < len(steps):
            raise SchemaError("It can't go any further that way")
        steps[i], steps[j] = steps[j], steps[i]
    elif do in ("add_way", "update_way", "remove_way"):
        steps, i = _step_at(a["actions"], change.get("at"))
        branch = steps[i]
        if branch["type"] != "branch":
            raise SchemaError("Only a step that goes one of several ways has ways")
        if do == "add_way":
            branch["paths"].append({"label": str(change.get("label") or "")[:80], "when": [schema.normalize_condition(c) for c in change.get("when") or []],
                                    "steps": []})
            if not branch["paths"][-1]["when"]:
                raise SchemaError("Say when a record goes this way")
        else:
            n = int(change.get("n", -1))
            if not 0 <= n < len(branch["paths"]):
                raise SchemaError("That way isn't there any more: reload the page")
            if do == "remove_way":
                if len(branch["paths"]) == 1:
                    raise SchemaError("It needs at least one way besides “anything else”: remove the step instead")
                branch["paths"].pop(n)
            else:
                when = [schema.normalize_condition(c) for c in change.get("when") or []]
                if not when:
                    raise SchemaError("Say when a record goes this way")
                branch["paths"][n].update(label=str(change.get("label") or "")[:80], when=when)
    else:
        raise SchemaError("That can't be changed here")
    return [{"op": "update_automation", "automation": a}], a["key"]


def _step(raw):
    if not isinstance(raw, dict) or not raw.get("type"):
        raise SchemaError("Choose what the step does")
    raw = {k: v for k, v in raw.items() if v not in (None, "", [], {})}
    if raw["type"] == "branch" and not raw.get("paths"):
        raw = {**raw, "paths": [{"label": raw.get("label") or "", "when": raw.get("when") or [], "steps": []}], "otherwise": []}
    if raw["type"] == "branch":
        for p in raw["paths"]:
            if not p.get("when"):
                raise SchemaError("Say when a record goes the first way")
            p.setdefault("steps", [])  # its steps are added once it's there
    return schema.normalize_action(raw)


def _steps_at(actions, into):
    """The list of steps at a place: "" (the rule's own), "2.0." (the first way of step 2), "2.o.", "2.r."."""
    parts = [p for p in str(into or "").split(".") if p != ""]
    steps = actions
    for j in range(0, len(parts), 2):
        try:
            step = steps[int(parts[j])]
            way = parts[j + 1]
        except (ValueError, IndexError) as exc:
            raise SchemaError("That place isn't there any more: reload the page") from exc
        if way == "o":
            steps = step.setdefault("otherwise", [])
        elif way == "r":
            steps = step.setdefault("if_rejected", [])
        else:
            try:
                steps = step["paths"][int(way)]["steps"]
            except (KeyError, ValueError, IndexError) as exc:
                raise SchemaError("That place isn't there any more: reload the page") from exc
    return steps


def _step_at(actions, at):
    at = str(at or "")
    head, _, last = at.rpartition(".")
    steps = _steps_at(actions, head + "." if head else "")
    try:
        i = int(last)
        steps[i]
    except (ValueError, IndexError) as exc:
        raise SchemaError("That step isn't there any more: reload the page") from exc
    return steps, i


def flow_ops(bp, change):
    """An edit to a step-by-step flow's screens, as ops."""
    flow = copy.deepcopy(next((f for f in bp.get("flows") or [] if f["key"] == change.get("key")), None))
    if flow is None:
        raise SchemaError("That flow isn't there any more: reload the page")
    do = change.get("do")
    steps = flow["steps"]
    if do == "remove_flow":
        return [{"op": "remove_flow", "key": flow["key"]}]
    if do == "rename":
        flow["label"] = _name(change.get("label"), "name for the flow")
    elif do in ("update_step", "remove_step", "move_step"):
        i = next((n for n, s in enumerate(steps) if s["key"] == change.get("step")), None)
        if i is None:
            raise SchemaError("That screen isn't there any more: reload the page")
        if do == "update_step":
            steps[i] = {**steps[i], "label": _name(change.get("label") or steps[i]["label"], "title for the screen", 80)}
            if "text" in change:
                steps[i]["text"] = str(change.get("text") or "")[:2000]
                if not steps[i]["text"]:
                    steps[i].pop("text")
            if steps[i]["kind"] == "form" and change.get("fields"):
                steps[i]["fields"] = [slug(k) for k in change["fields"]][:12]
        elif do == "remove_step":
            if len(steps) == 1:
                raise SchemaError("A flow needs at least one screen: remove the flow instead")
            steps.pop(i)
        else:
            j = i + (1 if int(change.get("by", 1)) > 0 else -1)
            if not 0 <= j < len(steps):
                raise SchemaError("It can't go any further that way")
            steps[i], steps[j] = steps[j], steps[i]
    elif do == "add_step":
        kind = change.get("kind")
        if kind not in ("info", "form", "photo", "signature"):
            raise SchemaError("Ask the builder for that kind of screen")
        label = _name(change.get("label") or {"info": "Read this", "form": "Fill in", "photo": "Take a photo", "signature": "Sign"}[kind],
                      "title for the screen", 80)
        step = {"key": _fresh_key({s["key"] for s in steps}, label, kind), "kind": kind, "label": label}
        if change.get("text"):
            step["text"] = str(change["text"])[:2000]
        if kind == "form":
            step["fields"] = [slug(k) for k in change.get("fields") or []][:12]
        if kind in ("photo", "signature"):
            step["field"] = slug(change.get("field") or "")
        steps.insert(int(change.get("index", len(steps))), schema.normalize_flow_step(step))
    else:
        raise SchemaError("That can't be changed here")
    return [{"op": "update_flow", "flow": flow}]


# --- trying a rule on a record, without doing anything ----------------------------------------------------------------

def preview(engine, a, record):
    """What the rule would do for this record, step by step, done nowhere: {"starts", "why", "assumed", "steps": {at:
    {"state": "runs"|"skipped"|"maybe", "why"}}}. A rule that waits for a change ("moved to Approved") is tried as if
    the change had just happened."""
    from .engine import automations
    table = a["trigger"].get("table")
    waits_for = [c for c in a["conditions"] if c.get("op") in ("changed", "changed_to")]
    rest = [c for c in a["conditions"] if c not in waits_for]
    failing = [c for c in rest if not engine.matches(record, [c])]
    out = {"starts": not failing, "why": [automations.describe_condition(engine, c, table) for c in failing],
           "assumed": [automations.describe_condition(engine, c, table) for c in waits_for], "steps": {}}
    if failing:
        return out

    def walk(actions, prefix, state="runs"):
        for i, step in enumerate(actions or []):
            at = f"{prefix}{i}"
            if state != "runs":
                out["steps"][at] = {"state": state}
                continue
            if step.get("only_if") and not engine.matches(record, step["only_if"]):
                out["steps"][at] = {"state": "skipped", "why": automations.automations_words(engine, step["only_if"], table) + " isn't true"}
                continue
            out["steps"][at] = {"state": "runs"}
            if step["type"] == "branch":
                chosen = next((n for n, p in enumerate(step["paths"]) if engine.matches(record, p["when"])), None)
                for n, p in enumerate(step["paths"]):
                    walk(p["steps"], f"{at}.{n}.", "runs" if n == chosen else "not_taken")
                walk(step.get("otherwise"), f"{at}.o.", "runs" if chosen is None else "not_taken")
                out["steps"][at]["why"] = (f"It goes the way “{step['paths'][chosen].get('label') or automations._path_words(engine, step['paths'][chosen], table)}”"
                                           if chosen is not None else "None of the ways fit, so it goes the “anything else” way")
            elif step["type"] == "request_approval":
                walk(step.get("if_rejected"), f"{at}.r.", "maybe")
                out["steps"][at]["why"] = "It waits for their answer: the steps after it happen if they say yes"
            elif step["type"] == "wait":
                out["steps"][at]["why"] = "It waits here, then carries on"
    walk(a["actions"], "")
    return out
