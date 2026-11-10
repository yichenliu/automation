"""Changing a blueprint: a list of operations (from the builder AI) applied all together or not at all.

`apply(blueprint, ops)` returns a Result: the new blueprint, one plain sentence per change, the work on records that
should follow (sample data to add, a question to answer), where the person should look next, and any problems. With
problems the blueprint is unchanged and the problems go back to the AI to fix.
"""
import copy
import json
import re
from dataclasses import dataclass, field as dfield

from . import expr, schema
from .schema import SchemaError, _first, _list, slug

OPS = ["set_portal", "set_team", "create_table", "update_table", "delete_table", "add_field", "update_field", "remove_field", "add_view",
       "update_view", "remove_view", "add_page", "update_page", "remove_page", "add_automation", "update_automation",
       "remove_automation", "set_module", "rename_app", "set_theme", "add_document", "update_document", "remove_document",
       "set_public_form", "add_records", "import_file", "find", "add_flow", "update_flow", "remove_flow", "set_space", "remove_space", "set_sync", "remove_sync", "order_fields", "rename_choice"]
OP_ALIASES = {"add_table": "create_table", "new_table": "create_table", "create_tables": "create_table",
              "edit_table": "update_table", "rename_table": "update_table", "drop_table": "delete_table", "remove_table": "delete_table",
              "create_field": "add_field", "new_field": "add_field", "add_column": "add_field", "add_fields": "add_field",
              "edit_field": "update_field", "change_field": "update_field", "rename_field": "update_field", "modify_field": "update_field",
              "delete_field": "remove_field", "drop_field": "remove_field", "remove_column": "remove_field",
              "create_view": "add_view", "edit_view": "update_view", "delete_view": "remove_view",
              "create_page": "add_page", "add_dashboard": "add_page", "create_dashboard": "add_page", "edit_page": "update_page",
              "delete_page": "remove_page", "create_automation": "add_automation", "add_workflow": "add_automation",
              "edit_automation": "update_automation", "delete_automation": "remove_automation",
              "add_module": "set_module", "create_module": "set_module", "update_module": "set_module",
              "rename": "rename_app", "set_name": "rename_app", "add_record": "add_records", "create_records": "add_records",
              "theme": "set_theme", "set_colors": "set_theme", "set_colour": "set_theme", "set_color": "set_theme",
              "brand": "set_theme", "add_template": "add_document", "add_pdf": "add_document", "create_document": "add_document",
              "edit_document": "update_document", "delete_document": "remove_document", "add_form": "set_public_form",
              "public_form": "set_public_form", "create_form": "set_public_form", "set_form": "set_public_form",
              "add_team": "set_team", "create_team": "set_team", "update_team": "set_team", "team": "set_team",
              "add_role": "set_team", "create_role": "set_team", "add_group": "set_team",
              "insert_records": "add_records", "add_data": "add_records", "query": "find", "search": "find", "list_records": "find",
              "import": "import_file", "import_spreadsheet": "import_file", "import_records": "import_file", "load_file": "import_file",
              "create_flow": "add_flow", "add_guided_flow": "add_flow", "add_process": "add_flow", "edit_flow": "update_flow",
              "delete_flow": "remove_flow", "add_workflow_screens": "add_flow", "add_station": "add_flow",
              "add_space": "set_space", "create_space": "set_space", "update_space": "set_space", "team_menu": "set_space",
              "delete_space": "remove_space", "add_sync": "set_sync", "create_sync": "set_sync", "update_sync": "set_sync",
              "sync": "set_sync", "import_from_app": "set_sync", "delete_sync": "remove_sync",
              "reorder_fields": "order_fields", "move_fields": "order_fields", "arrange_fields": "order_fields",
              "rename_option": "rename_choice", "rename_stage": "rename_choice", "rename_status": "rename_choice", "rename_value": "rename_choice"}


@dataclass
class Result:
    blueprint: dict
    changes: list = dfield(default_factory=list)
    data: list = dfield(default_factory=list)  # [{"op": "add_records"|"find", ...}] for after the design is saved
    navigate: str = None  # "table:clients" | "page:overview" | "automations"
    problems: list = dfield(default_factory=list)
    left_out: list = dfield(default_factory=list)  # changes salvage() had to leave out, with why (for the AI)
    missing: list = dfield(default_factory=list)  # the same in the owner's words: "the Orders table", "the page Overview"
    cut: list = dfield(default_factory=list)  # [(the owner's words, what to look for to tell whether it's there later)]
    corrected: list = dfield(default_factory=list)  # mistakes the platform put right (_resolve_sense): not right first time

    @property
    def changed(self):
        return bool(self.changes)


def _table(bp, key, problems, where):
    t = schema.table(bp, slug(key or ""))
    if t is None:
        problems.append(f"{where}: there's no table “{key}” (tables: {', '.join(x['key'] for x in bp['tables']) or 'none yet'})")
    return t


def _ensure_module(bp, key, changes):
    if key and key not in {m["key"] for m in bp["modules"]}:
        bp["modules"].append({"key": key, "label": schema.label_of(key)})
        changes.append(f"Added the {schema.label_of(key)} section to the menu")


def _default_views(t):
    """A list of everything, and a board when the table has a status-like select field."""
    if t.get("embedded"):
        return
    if not any(v["type"] == "list" for v in t["views"]):
        columns = [f["key"] for f in t["fields"] if f["type"] != "long_text"][:7]
        t["views"].insert(0, {"key": "all", "label": f"All {schema.lc(t['label'])}", "type": "list", "columns": columns})
    status = next((f for f in t["fields"] if f["type"] == "select" and f["key"] in ("status", "stage", "state", "phase")), None)
    if status and not any(v["type"] == "board" for v in t["views"]):
        t["views"].append({"key": "board", "label": f"By {schema.lc(status['label'])}", "type": "board", "group_by": status["key"]})


def _merge_field(old, changes):
    merged = {**old, **{k: v for k, v in changes.items() if k != "key"}}
    if "type" in changes or "kind" in changes:
        merged["type"] = changes.get("type") or changes.get("kind")
        if schema.field_type(merged["type"]) != old["type"]:  # drop settings of the old type
            for k in ("options", "table", "expression", "format", "via", "aggregate", "field", "prefix", "digits", "filters"):
                if k not in changes:
                    merged.pop(k, None)
    merged["key"] = old["key"]
    return schema.normalize_field(merged)


def wrong_already(blueprint):
    """What's wrong in a design already, so a change isn't blamed for it: what it fails now, and what saving it as it is
    would (an app made before a newer way of saying something, brought up to date on any save)."""
    if not (blueprint or {}).get("tables"):
        return []
    return schema.check(blueprint) + apply(blueprint, [], always_check=True).problems


def apply(blueprint, ops, always_check=False, already=(), strict=False):
    """`already`: problems the design had before these changes (an old app checked by newer rules). They don't stop a
    change that leaves them as they were; only a problem the changes bring does. `strict`: the ops are exact (the Keel
    language compiles to them): nothing is read into them, so a problem is said, never quietly put right."""
    bp = copy.deepcopy(blueprint) if blueprint else schema.empty()
    for k in ("modules", "tables", "pages", "automations"):
        bp.setdefault(k, [])
    result = Result(blueprint=bp)
    changes, problems = result.changes, result.problems
    for i, raw in enumerate(_list(ops)):
        if not isinstance(raw, dict):
            problems.append(f"change {i + 1} isn't an object")
            continue
        name = slug(_first(raw, "op", "action", "operation", "type", default=""))
        op = OP_ALIASES.get(name, name)
        where = f"change {i + 1} ({op or 'no op'})"
        if op not in OPS:
            problems.append(f"{where}: “{name}” isn't something I can do; use one of: {', '.join(OPS)}")
            continue
        restating, said = op.startswith(("update_", "set_")) or op == "rename_app", len(changes)
        before = json.dumps(bp, sort_keys=True, default=str) if restating else None
        token = schema._TEAM_KEYS.set(frozenset(tm["key"] for tm in schema.teams(bp)))  # a team named like a who-word stays the team
        tables_ = schema._BP_TABLES.set({t["key"]: t for t in bp.get("tables", [])})  # and a list's own person field, its field
        try:
            _apply_one(bp, op, raw, result, where)
        except SchemaError as exc:
            problems.append(f"{where}: {exc}")
        finally:
            schema._TEAM_KEYS.reset(token)
            schema._BP_TABLES.reset(tables_)
        if restating and json.dumps(bp, sort_keys=True, default=str) == before:  # saying again what's so isn't a change
            del changes[said:]
    result.corrected = [] if strict else _resolve(bp, changes, {t["key"] for t in bp["tables"]} - {t["key"] for t in (blueprint or {}).get("tables", [])})
    for a in bp["automations"]:  # the same message twice in a row, from before that was caught: sent once
        a["actions"] = [x for n, x in enumerate(a.get("actions") or []) if not (n and x.get("type") in schema.REPEATS_ARE_SLIPS and x == a["actions"][n - 1])]
    for t in bp["tables"]:
        _default_views(t)
        if not t.get("title_field") or not schema.field(t, t["title_field"]):
            t["title_field"] = schema.title_field(t)
        _ensure_module(bp, t.get("module"), changes)
    for p in bp["pages"]:
        _ensure_module(bp, p.get("module"), changes)
    if not problems or always_check:
        problems.extend(schema.check(bp))
    if already:
        problems[:] = [p for p in problems if p not in set(already)]
    if problems:
        result.blueprint = blueprint
    return result


POLICIES = ("access", "permissions", "row_access", "own_records", "only_own", "rules", "validations", "constraints", "checks",
            "lock_if", "locked_when", "lock_when", "lock_except", "still_editable", "portal", "workflow", "stages", "status_flow",
            "transitions", "stage_flow", "unique_together", "unique", "approve_changes", "changes_need_approval", "sealed", "sealed_until",
            "audit_views", "journey", "limits", "capacity", "no_overlap", "keep_for", "retention")


POLICY_PARTS = [(("access", "permissions"), "who may see or change {}"),
                (("row_access", "own_records", "only_own"), "which {} each person sees"),
                (("rules", "validations", "constraints", "checks"), "the rules on {}"),
                (("lock_if", "locked_when", "lock_when", "lock_except", "still_editable"), "when {} locks"),
                (("portal",), "what the portal shows of {}"),
                (("workflow", "stages", "status_flow", "transitions", "stage_flow"), "how {} move between stages"),
                (("unique_together", "unique"), "what no two {} may share"),
                (("limits", "capacity", "no_overlap"), "how many {} may share a day or a time"),
                (("approve_changes", "changes_need_approval"), "which changes to {} need approval"),
                (("sealed", "sealed_until"), "when {} open"), (("journey",), "the journey shown on {}"),
                (("keep_for", "retention"), "how long {} are kept")]


def _without_policies(raw):
    """The same change without its permissions, rules and locks (the part most often written wrong), or None."""
    return _without(raw, POLICIES)


def _named(raw):
    """A whole change, in the owner's words: "the Orders table", "the page Overview", "the automation “Chase rent”"."""
    op, table = _op(raw), _table_words(raw)
    automation = raw.get("automation") if isinstance(raw.get("automation"), dict) else raw
    thing = {"create_table": f"the {table} table", "update_table": f"the changes to {table}", "delete_table": f"removing {table}",
             "add_field": f"the new fields of {table}", "update_field": f"the change to a field of {table}",
             "add_view": f"the new view of {table}", "update_view": f"the change to a view of {table}",
             "add_page": _page_words(raw), "update_page": f"the changes to {_page_words(raw)}",
             "add_automation": f"the automation “{_first(automation, 'name', 'label', 'key', default='')}”",
             "update_automation": f"the change to the automation “{_first(automation, 'name', 'label', 'key', default='')}”",
             "add_document": f"the document for {table}", "update_document": f"the change to the document for {table}",
             "set_public_form": f"the public form for {table}", "set_portal": "the customer portal",
             "add_records": f"the sample {table.lower()}",
             "add_flow": f"the steps for {_first(raw.get('flow') if isinstance(raw.get('flow'), dict) else raw, 'label', 'name', 'key', default='the new work')}",
             "update_flow": f"the change to the steps for {_first(raw.get('flow') if isinstance(raw.get('flow'), dict) else raw, 'label', 'name', 'key', default='it')}",
             "import_file": f"bringing the rows into {table}" if _first(raw, "table") else "bringing in the spreadsheet's rows"}
    return thing.get(op) or (op.replace("_", " ") if op else "a change")


def salvage(blueprint, raw_ops, rounds=40):
    """After the AI's repairs, an answer that still has problems: keep everything that fits. Each round makes the cut
    that clears the most problems for what it loses: one of a table's permissions, rules, locks or portal; one field,
    view or block of a page; a table's views and colours; and only then a whole change. When no cut clears anything but
    one moves a problem on (a field cut out, and now the formula that used it complains), that's followed a few steps.
    Returns a Result whose `left_out` says what was left out and why (for the AI), and `missing` the same in the owner's
    words."""
    current = list(_list(raw_ops))
    result = apply(blueprint, current, always_check=True)
    left_out, missing, follows = [], [], 16
    for _ in range(rounds):
        if not result.problems or not current:
            break
        best, now = None, {_unnumbered(p) for p in result.problems}
        cuts = [(cost, current[:i] + ([smaller] if smaller is not None else []) + current[i + 1:], lost, ref)
                for i, raw in enumerate(current) for cost, smaller, lost, ref in _smaller(raw, result.problems)]
        named = {}
        for i, raw in enumerate(current):  # the same thing changed twice (a document added twice): both go, or neither helps
            named.setdefault(_named(raw) if isinstance(raw, dict) else None, []).append(i)
        cuts += [(3, [raw for i, raw in enumerate(current) if i not in group], name, _whole(current[group[0]]))
                 for name, group in named.items() if name and len(group) > 1]
        for cost, trial_ops, lost, ref in cuts:
            trial = apply(blueprint, trial_ops, always_check=True)
            gain = len(result.problems) - len(trial.problems)
            after = {_unnumbered(p) for p in trial.problems}
            if gain > 0:
                score = (1, gain / cost, -cost)
            elif follows and now - after:  # clears one but others follow from it: cut those next
                score = (0, gain, -cost)
            else:
                continue
            if best is None or score > best[0]:
                best = (score, trial, trial_ops, lost, ref)
        if best is None:
            break
        if best[0][0] == 0:
            follows -= 1
        after = {_unnumbered(p) for p in best[1].problems}  # "change 12 (…)" is "change 11 (…)" once one before it goes
        cleared = [p for p in result.problems if _unnumbered(p) not in after] or result.problems
        current, result = best[2], best[1]
        left_out.append(f"{best[3]} (because {cleared[0]})")
        missing.append((best[3], best[4]))
    if result.problems:
        return result
    final = apply(blueprint, current)
    final.left_out, final.missing, final.cut = left_out, [name for name, _ in missing], missing
    return final


def _unnumbered(problem):
    """A problem without what shifts when something else is cut: its change number, its list of the fields there are."""
    return re.sub(r"\s*\((fields|tables|that's|links|and)?[^()]*\)$", "", re.sub(r"^change \d+ ", "change ", problem))


PRESENTATION = ("views", "colors", "colours", "color_rules", "colour_rules", "sections", "layout", "tabs", "wizard", "steps",
                "public_form", "documents")


def _smaller(raw, problems):
    """Ways to cut one change down, as (cost, the smaller change or None to drop it, what's lost in plain words, and
    what to look for later to tell whether it's been put back)."""
    if not isinstance(raw, dict):
        return [(3, None, "a change that wasn't readable", None)]
    out, table, op, key = [], _table_words(raw), _op(raw), _table_key(raw)
    if op in ("create_table", "update_table"):
        for keys, lost in POLICY_PARTS:  # one policy at a time: a broken lock mustn't cost payroll its privacy
            smaller = _without(raw, keys)
            if smaller:
                out.append((0.5, smaller, lost.format(table), ("policy", key, keys)))
        soft = _without_policies(raw)
        if soft and sum(1 for keys, _ in POLICY_PARTS if _without(raw, keys)) > 1:
            out.append((0.75, soft, f"who may see or change {table}, its rules and its locks", ("policy", key, POLICIES)))
    said = " ".join(problems)
    for spot, kind in _lists_in(raw):
        items = spot.get(kind)
        for n, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            name = str(_first(item, "label", "title", "name", "key", default="")).strip()
            if kind == "widgets" or (name and name in said) or str(item.get("key") or "~") in said:
                smaller = copy.deepcopy(raw)
                del _same_spot(smaller, spot, raw, kind)[n]
                if kind == "widgets":
                    page = raw.get("page") if isinstance(raw.get("page"), dict) else raw
                    out.append((1, smaller, f"the {name or 'untitled'} block on {_page_words(raw)}", ("widget", slug(str(_first(page, "key", default=""))), name)))
                elif kind == "views":
                    out.append((1, smaller, f"the view {name} of {table}", ("view", key, name)))
                else:
                    _forget(smaller, slug(item.get("key") or name))
                    out.append((1, smaller, f"the field {name or item.get('key')} of {table}", ("field", key, slug(item.get("key") or name))))
    plain = _without(raw, PRESENTATION) if op in ("create_table", "update_table") else None
    if plain:
        out.append((2, plain, f"the views, colours and sections of {table}", ("presented", key)))
    out.append((3, None, _named(raw), _whole(raw)))
    return out


def _table_key(raw):
    spec = raw["table"] if isinstance(raw.get("table"), dict) else raw
    return slug(str(_first(spec, "table", "key", default="")))


def _whole(raw):
    """What to look for to tell whether a whole change that was cut has been put back since."""
    op, table = _op(raw), _table_key(raw)
    automation = raw.get("automation") if isinstance(raw.get("automation"), dict) else raw
    page = raw.get("page") if isinstance(raw.get("page"), dict) else raw
    return {"create_table": ("table", table), "add_page": ("page", slug(str(_first(page, "key", "label", default="")))),
            "add_automation": ("automation", slug(str(_first(automation, "name", "key", default="")))),
            "add_document": ("document", table), "set_public_form": ("public_form", table), "set_portal": ("portal",),
            "import_file": ("import", schema.expr.to_number(_first(raw, "file_id", "file", "id"))),
            "add_field": ("fields", table, [slug(str(_first(f, "key", "label", default=""))) for f in _list(_first(raw, "fields", default=None))
                                            or [_first(raw, "field", default={})] if isinstance(f, dict)])}.get(op)


def still_missing(bp, cut, imported=()):
    """Of the things salvage cut, the ones still not in the design (after the AI's second try at them), in the owner's
    words; `imported` are the files brought in since. What can't be checked counts as still missing: better to say so
    than to claim it."""
    tables = {t["key"]: t for t in bp.get("tables", [])}
    pages = {p["key"]: p for p in bp.get("pages", [])}
    autos = {slug(a["name"]) for a in bp.get("automations", [])} | {a["key"] for a in bp.get("automations", [])}

    def there(ref):
        kind, t = ref[0], tables.get(ref[1]) if len(ref) > 1 else None
        if kind == "table":
            return t is not None
        if kind == "page":
            return ref[1] in pages or any(slug(p["label"]) == ref[1] for p in pages.values())
        if kind == "automation":
            return ref[1] in autos
        if kind == "portal":
            return bool(schema.portals(bp))
        if kind == "import":
            return ref[1] in imported
        if t is None:
            return False
        if kind == "document":
            return bool(t.get("documents"))
        if kind == "public_form":
            return bool(t.get("public_form"))
        if kind == "policy":
            return any(t.get(k) for k in ref[2])
        if kind == "field":
            return schema.field(t, ref[2]) is not None
        if kind == "fields":
            return all(schema.field(t, k) for k in ref[2])
        if kind == "view":
            return any(v["label"] == ref[2] or v["key"] == slug(ref[2]) for v in t["views"])
        if kind == "presented":
            return bool(t.get("colors") or t.get("sections") or any(v["key"] not in ("all", "board") for v in t["views"]))
        if kind == "widget":
            return any(w.get("label") == ref[2] for w in (pages.get(ref[1]) or {}).get("widgets", []))
        return True

    return list(dict.fromkeys(name for name, ref in cut if not ref or not there(ref)))


def _lists_in(raw):
    """The lists of fields (a table) and blocks (a page) inside a change, as (the dict holding it, its key)."""
    holders = [raw] + [raw[k] for k in ("table", "changes", "updates", "page") if isinstance(raw.get(k), dict)]
    return [(h, k) for h in holders for k in ("fields", "columns", "widgets", "views") if isinstance(h.get(k), list)]


def _same_spot(copied, spot, raw, key):
    """The list in the deep copy that is `spot[key]` in the original."""
    if spot is raw:
        return copied[key]
    holder = next(k for k in ("table", "changes", "updates", "page") if raw.get(k) is spot)
    return copied[holder][key]


def _forget(raw, key):
    """Take a field that's been cut out of the sections and view columns that list it."""
    for holder in [raw] + [raw[k] for k in ("table", "changes", "updates") if isinstance(raw.get(k), dict)]:
        for section in holder.get("sections") or []:
            if isinstance(section, dict) and isinstance(section.get("fields"), list):
                section["fields"] = [f for f in section["fields"] if slug(str(f)) != key]
        for view in holder.get("views") or []:
            if isinstance(view, dict) and isinstance(view.get("columns"), list):
                view["columns"] = [c for c in view["columns"] if slug(str(c)) != key]


def _without(raw, keys):
    inner = {k: raw[k] for k in ("table", "changes", "updates") if isinstance(raw.get(k), dict)}
    if not any(k in raw for k in keys) and not any(k in d for d in inner.values() for k in keys):
        return None
    smaller = {k: v for k, v in raw.items() if k not in keys}
    for k, d in inner.items():
        smaller[k] = {x: v for x, v in d.items() if x not in keys}
    return smaller


def _table_words(raw):
    spec = raw["table"] if isinstance(raw.get("table"), dict) else raw
    name = _first(spec, "label", "name", default="") if spec is not raw or _op(raw) == "create_table" else ""
    return str(name or schema.label_of(slug(str(_first(spec, "table", "key", default="it")))))


def _page_words(raw):
    page = raw.get("page") if isinstance(raw.get("page"), dict) else raw
    return f"the page {_first(page, 'label', 'title', 'key', default='')}".strip()


def _op(raw):
    name = slug(_first(raw, "op", "action", "operation", "type", default=""))
    return OP_ALIASES.get(name, name)


def _infer_table(bp, automation):
    """A schedule written without its table, or with one that doesn't fit: use the one table that has every field the
    automation mentions (its conditions, the values it sets, the {{record.x}} in its messages), or failing that the one
    table that has every field its conditions check and more of the rest than any other."""
    trigger = automation["trigger"]
    conditions = {c["field"].split(".")[0] for c in schema.condition_leaves(automation["conditions"])}
    used = set(conditions)
    for act in schema.steps_of(automation["actions"]):
        for path in act.get("paths") or []:
            used |= {c["field"].split(".")[0] for c in schema.condition_leaves(path["when"])}
        if act["type"] == "update_record" and not act.get("target"):
            used |= set(act["values"])
        if act["type"] == "ai_fill":
            used.add(act["field"])
        for text in [act.get("message")] + [v for v in (act.get("values") or {}).values() if isinstance(v, str)]:
            used |= set(re.findall(r"record\.(\w+)", text or ""))
    if not used:
        return
    current = schema.table(bp, trigger.get("table")) if trigger.get("table") else None
    if current and used <= {f["key"] for f in current["fields"]}:
        return
    fits = [t for t in bp["tables"] if used <= {f["key"] for f in t["fields"]}]
    if len(fits) == 1:
        trigger["table"] = fits[0]["key"]
    elif not fits and current is None and trigger["type"] == "schedule" and conditions:
        near = sorted(((len(used & {f["key"] for f in t["fields"]}), t["key"]) for t in bp["tables"]
                       if conditions <= {f["key"] for f in t["fields"]}), reverse=True)
        if near and (len(near) == 1 or near[0][0] > near[1][0]):
            trigger["table"] = near[0][1]  # what doesn't fit it is then pointed out field by field


END_WORDS = re.compile(r"end|finish|due|until|complet|deadline|^to_|_to$")


def _resolve(bp, changes, new=()):
    """Shorthand the AI often writes, filled in once every table of the answer exists, where only one reading fits:
    a lookup's link or field, a timeline's end date, the table a "for each line" step fills, a schedule's table.
    `new` are the tables made by this change."""
    tables = {t["key"]: t for t in bp["tables"]}
    _resolve_portals(bp, tables, changes)
    _resolve_currency_lookups(bp, tables, changes, new)
    for t in bp["tables"]:
        _resolve_percents(t)
        for f in t["fields"]:
            if f["type"] == "lookup":
                _resolve_lookup(t, f, tables)
            if f["type"] == "formula" and f.get("format") in ("number", "money", "percent") and _gives_words(f.get("expression", "")):
                f["format"] = "text"  # “Low” or “OK” shown as a number is nothing at all
                changes.append(f"{t['label']} › {f['label']} is shown as words: it gives words, not a number")
        keys = {f["key"] for f in t["fields"]} | {"created_at", "updated_at"}
        if t["fields"]:  # sections and columns only arrange fields: one naming no field (often the lines) is skipped
            if t.get("sections"):
                for section in t["sections"]:
                    section["fields"] = [k for k in section["fields"] if k in keys]
                t["sections"] = [x for x in t["sections"] if x["fields"]]
            for v in t["views"]:
                if v.get("columns"):
                    v["columns"] = [c for c in v["columns"] if c in keys or "." in c] or v["columns"]
            form = t.get("public_form")
            if form and form.get("fields"):  # a question about nothing is left off the form
                form["fields"] = [k for k in form["fields"] if k in keys] or \
                    [f["key"] for f in t["fields"] if f["type"] in schema.STORED and f["type"] not in ("autonumber", "link", "user")][:8]
        for doc in t.get("documents", []):
            _resolve_document(t, doc, tables, keys)
        if t.get("workflow"):
            _resolve_workflow(t, changes)
        _resolve_currency(t, tables)
        for v in t["views"]:
            if v["type"] == "timeline" and not v.get("end_field"):
                ends = [f["key"] for f in t["fields"] if f["key"] != v["start_field"] and END_WORDS.search(f["key"])
                        and (f["type"] in schema.DATES or f.get("format") in schema.DATES)]
                if len(ends) == 1:
                    v["end_field"] = ends[0]
    for p in bp.get("pages", []):
        for w in p["widgets"]:
            t = tables.get(w.get("table"))
            if t and w.get("columns"):  # "project_name" for the project link, which shows its name; others left off
                keys = {f["key"] for f in t["fields"]} | {"created_at", "updated_at"}
                links = {f["key"] for f in t["fields"] if f["type"] == "link"}
                named = [c if c in keys else next((x for x in links if c.startswith(x + "_")), None) for c in w["columns"]]
                w["columns"] = list(dict.fromkeys(c for c in named if c)) or w["columns"]
    names = {schema.slug(a["name"]): a["key"] for a in bp["automations"]}
    for a in bp["automations"]:
        _infer_table(bp, a)
        made = None
        docs = [d["key"] for d in (tables.get(a["trigger"].get("table")) or {}).get("documents", [])]
        files = {f["key"] for f in (tables.get(a["trigger"].get("table")) or {}).get("fields", []) if f["type"] in schema.FILES}
        trigger_table = tables.get(a["trigger"].get("table"))
        for act in schema.steps_of(a["actions"]):
            if act["type"] == "run_automation" and act["automation"] not in names.values() and act["automation"] in names:
                act["automation"] = names[act["automation"]]  # named by its name, not its key
            if not made:  # {{created}} before anything is created: the record the automation is about
                for k, v in list((act.get("values") or {}).items()):
                    act["values"][k] = _as_record(v)
                for k in ("message", "subject", "body", "to"):
                    if isinstance(act.get(k), str):
                        act[k] = _as_record(act[k])
            if act["type"] == "create_for_each" and act.get("only_if"):  # a condition on "item.x" picks which lines
                each = [dict(c, field=c["field"][5:]) for c in act["only_if"] if c.get("field", "").startswith("item.")]
                if each:
                    act["where"] = act.get("where", []) + each
                    act["only_if"] = [c for c in act["only_if"] if not c.get("field", "").startswith("item.")]
                    if not act["only_if"]:
                        del act["only_if"]
            if act["type"] == "update_record" and not act.get("target") and isinstance((act.get("values") or {}).get("target"), str) \
                    and not schema.field(trigger_table, "target"):  # "target" written among the values
                act["target"] = schema.slug(act["values"].pop("target"))
            if act["type"] == "update_record" and not act.get("target") and trigger_table and act["values"]:
                # values the record hasn't got but the one record it links to (or the one just made) has: set them there
                own = {f["key"] for f in trigger_table["fields"]}
                wanted = set(act["values"])
                if not wanted <= own:
                    fits = [f["key"] for f in trigger_table["fields"] if f["type"] == "link" and not f.get("multiple")
                            and wanted <= {g["key"] for g in (tables.get(f["table"]) or {}).get("fields", [])}]
                    if made and wanted <= {g["key"] for g in (tables.get(made) or {}).get("fields", [])}:
                        fits.append("created")
                    if len(fits) == 1:
                        act["target"] = fits[0]
            source = act.get("source") if isinstance(act.get("source"), dict) else None
            if source and source.get("table") and source.get("via") and not source.get("of") and trigger_table and source["table"] in tables:
                via = schema.field(tables[source["table"]], source["via"])
                if via and via["type"] == "link" and via["table"] != trigger_table["key"]:
                    # the work order's product's bill of materials: through the one link that reaches it
                    links = [f["key"] for f in trigger_table["fields"] if f["type"] == "link" and f["table"] == via["table"]]
                    if len(links) == 1:
                        source["of"] = links[0]
            if act["type"] == "ai_create_record" and act.get("attach_to"):  # keeping what was read needs a file field
                if (schema.field(tables.get(act.get("table")), act["attach_to"]) or {}).get("type") not in schema.FILES:
                    del act["attach_to"]
            new_docs = [d["key"] for d in (tables.get(made) or {}).get("documents", [])] if made else []
            if act["type"] == "generate_document" and not act.get("target") and act.get("document") not in docs and new_docs:
                # after making an invoice, "produce the tax invoice" means the new invoice's
                if act["document"] in new_docs or len(new_docs) == 1:
                    act.update(target="created", document=act["document"] if act["document"] in new_docs else new_docs[0])
                    if act.get("field") and (schema.field(tables[made], act["field"]) or {}).get("type") not in schema.FILES:
                        files_of_new = [f["key"] for f in tables[made]["fields"] if f["type"] in schema.FILES]
                        if len(files_of_new) == 1:
                            act["field"] = files_of_new[0]
                        else:
                            del act["field"]
                    continue
            if len(docs) == 1:  # "invoice_pdf" for the one document the table has
                if act["type"] == "generate_document" and act.get("document") not in docs and not act.get("target"):
                    act["document"] = docs[0]
                if act.get("attach"):
                    act["attach"] = [k if k.startswith("page:") or k in docs or k in files or k in new_docs else docs[0] for k in act["attach"]]
            elif act.get("attach") and len(new_docs) == 1:
                act["attach"] = [k if k.startswith("page:") or k in docs or k in files or k in new_docs else new_docs[0] for k in act["attach"]]
            if act["type"] == "create_for_each" and not act.get("table") and made:
                # after making an invoice, "for each job line" means: make the invoice's lines
                lines = [x["key"] for x in bp["tables"] if x.get("embedded") and any(f["type"] == "link" and f["table"] == made for f in x["fields"])]
                if len(lines) == 1:
                    act["table"] = lines[0]
            if act["type"] == "update_record" and act.get("target") != "created" and trigger_table and act.get("values"):
                # "set status Paid" on a payment: Paid is the choice of the record made just before, or of the one record
                # it links to whose field has it (its invoice), not its own; "Deposit paid" on its invoice: its invoice's couple's
                links = schema.link_path(trigger_table, act["target"], tables) if act.get("target") else []
                here = tables.get(links[-1]["table"]) if links else None if act.get("target") else trigger_table
                if here is not None and _not_choices(here, act["values"]):
                    fit = lambda t: t is not None and set(act["values"]) <= {f["key"] for f in t["fields"]} and not _not_choices(t, act["values"])  # noqa: E731
                    places = ["created"] if made and not act.get("target") and fit(tables.get(made)) else []
                    for longest in (1, 2):
                        if places:
                            break
                        places = [path for path, t in _link_paths(trigger_table, tables, longest) if path.count(".") == longest - 1
                                  and (not act.get("target") or path.startswith(act["target"] + ".")) and fit(t)]
                    if len(places) == 1:
                        act["target"] = places[0]
            if act["type"] in ("update_record", "create_record", "create_for_each") and isinstance(act.get("values"), dict):
                into = trigger_table if act["type"] == "update_record" and not act.get("target") else \
                    tables.get(made) if act.get("target") == "created" else \
                    (lambda ls: tables.get(ls[-1]["table"]) if ls else None)(schema.link_path(trigger_table, act["target"], tables)) \
                    if act["type"] == "update_record" else tables.get(act.get("table"))
                for key, value in act["values"].items():
                    if _worked_out_bare(schema.field(into, key) if into else None, value):
                        act["values"][key] = "{{" + value.strip() + "}}"  # “round(hire_cost * rate, 2)” is worked out, not saved as words
            if act["type"] in ("create_record", "ai_create_record"):
                made = act.get("table")
    corrected = []
    _resolve_sense(bp, tables, corrected)
    changes.extend(corrected)
    return corrected


def _worked_out_bare(f, value):
    """A value for a number or date field written as a formula without {{ }} (“round(hire_cost * bin.penalty_rate, 2)”,
    “add_days(record.start, 14)”): one that would be saved as its words and refused."""
    if f is None or not isinstance(value, str) or "{{" in value or f["type"] not in schema.NUMERIC | {"duration", "date", "datetime"}:
        return False
    if expr.to_number(value) is not None or re.fullmatch(r"\s*(today|now|tomorrow|yesterday|\d{4}-\d\d-\d\d.*)\s*", value, re.I):
        return False
    import ast
    try:
        tree = expr.parse(value)
    except expr.ExprError:
        return False
    return any(isinstance(n, (ast.BinOp, ast.Call, ast.Attribute)) for n in ast.walk(tree))


def _link_paths(t, tables, longest=2):
    """Every path of single links from t, `longest` links long at most, shortest first (a payment's “invoice”, then
    “invoice.couple”), with the table each reaches; never back to t."""
    out, level = [], [("", t)]
    for _ in range(longest):
        level = [(f"{path}.{f['key']}" if path else f["key"], tables[f["table"]]) for path, here in level for f in here["fields"]
                 if f["type"] == "link" and not f.get("multiple") and f["table"] in tables and f["table"] != t["key"]]
        out += level
    return out


def _not_choices(t, values):
    """The values that aren't one of their (choice) field's options on this table."""
    out = []
    for key, value in (values or {}).items():
        f = schema.field(t, key) if t else None
        if f is not None and f["type"] == "select" and isinstance(value, str) and "{{" not in value \
                and value.strip().lower() not in {o.lower() for o in f.get("options", [])}:
            out.append(key)
    return out


FINAL = re.compile(r"done|complete|closed|paid|cancel|reject|declin|lost|won|archiv|void|finish|deliver|signed|accepted|approved|ordered|invoiced", re.I)
CANCEL = re.compile(r"cancel|void|withdrawn|abandon", re.I)
NO = re.compile(r"\bnot\b|reject|declin|refus|lost|fail|unsuccess", re.I)


def _resolve_sense(bp, tables, changes):
    """Mistakes with one sensible reading, put right before anything is saved (the try-out would only find them later):
    a choice spelled close to one of its options; a field required to equal two things at once (either was meant); moves
    out of a stage on "more than X" and "less than X" that leave X itself stuck; a stage no button leads to."""
    for a in bp["automations"]:
        t = tables.get(a["trigger"].get("table") or "")
        _item_outside_a_loop(a, changes)
        _then_the_same(a, changes)
        _said_twice(a, a["actions"], changes)
        if t is not None:
            _set_when_added(a, t, changes)
        if t is not None and t.get("workflow") and schema.stages(t):
            _starts_when_added(a, t, changes)
            _approval_moves_on(a, t, changes)
            _one_way_to_do_it(a, t, changes)
        lifted = []
        a["actions"] = _in_turn(a["actions"], lifted)
        if lifted:
            changes.append(f"Put the approvals of “{a['name']}” one after another: {_and_words(lifted)} "
                           f"{'was' if len(lifted) == 1 else 'were'} only asked when the one before said no")
        a["conditions"] = _either(a.get("conditions") or [])
        _spell(t, a.get("conditions"), tables)
        for act in schema.steps_of(a["actions"]):
            if act.get("only_if"):
                act["only_if"] = _either(act["only_if"])
                _on_target(t, act, tables, changes, a)
                _spell(t, act["only_if"], tables)
            for path in act.get("paths") or []:
                _spell(t, path.get("when"), tables)
    lists = {t["label"].strip().lower() for t in bp["tables"]}
    for page in bp.get("pages", []):  # a page named like a list showed in the menu twice ("Inspections", "Inspections")
        if page.get("label", "").strip().lower() in lists:
            changes.append(f"Called the page {page['label']} “{page['label']} overview”, so it isn't mistaken for the list of {page['label'].lower()}")
            page["label"] = f"{page['label']} overview"
    for t in bp["tables"]:
        _whole_name(t, changes)
        flow = t.get("workflow")
        for m in (flow or {}).get("transitions", []):
            if m.get("only_if"):
                m["only_if"] = _either(m["only_if"])
                _spell(t, m["only_if"], tables)
        if flow and schema.stages(t):
            _cover_both_sides(t, changes)
            _lead_to_every_stage(bp, t, changes)


APPROVED = re.compile(r"approv|accept|authori[sz]|\bok\b|agree|confirm|sign", re.I)
APPROVED_BY = re.compile(r"approv\w*[ _]?by|^approver|signed[ _]off[ _]by|authori[sz]ed[ _]by", re.I)
APPROVED_ON = re.compile(r"(approv|authori[sz]|signed[ _]off)\w*[ _](date|on|at)\b|date[ _](approved|authori[sz]ed)", re.I)


WORK = {"update_record", "create_record", "create_for_each", "generate_document", "send_email", "update_each", "run_automation",
        "webhook", "assign", "task"}


LOOPS = ("create_for_each", "update_each")  # the steps done once for each line (or person), where {{item}} is that one


def _item_outside_a_loop(a, changes):
    """“{{item.product}}” in a step done once (a stock movement made when a PO line is received): there's no item there, so
    it always came out empty and the step stopped. The rule's own record was meant."""
    def swap(value):
        if isinstance(value, str):
            return expr.TEMPLATE.sub(lambda m: m.group(0).replace(m.group(1), re.sub(r"(?<![.\w])item\.", "record.", m.group(1))), value)
        if isinstance(value, list):
            return [swap(v) for v in value]
        if isinstance(value, dict):
            return {k: swap(v) for k, v in value.items()}
        return value
    fixed = False
    for act in schema.steps_of(a["actions"]):
        if act.get("type") in LOOPS:
            continue
        for k in [k for k in act if k not in ("paths", "otherwise", "if_rejected", "steps")]:
            if swap(act[k]) != act[k]:
                act[k], fixed = swap(act[k]), True
    if fixed:
        changes.append(f"“{a['name']}” now reads its own record where it said “item” (only a step done for each line has one)")


def _then_the_same(a, changes):
    """"Over $20,000 asks the owner; $20,000 or under is made into a PDF, emailed and marked Sent": the approved way
    stopped at the approval, so an approved quote was never sent. It now goes on to do what the other way does."""
    for act in schema.steps_of(a["actions"]):
        ways = [p.get("steps") or [] for p in act.get("paths") or []] + ([act["otherwise"]] if isinstance(act.get("otherwise"), list) else [])
        if act.get("type") != "branch" or len(ways) < 2:
            continue
        asking = [w for w in ways if any(s.get("type") == "request_approval" for s in w)]
        doing = [w for w in ways if w not in asking and any(s.get("type") in WORK for s in w)]
        if len(doing) != 1:
            continue
        for w in asking:
            last = max(i for i, s in enumerate(w) if s.get("type") == "request_approval")
            if any(s.get("type") in WORK for s in w[last + 1:]):
                continue
            w[last + 1:last + 1] = copy.deepcopy(doing[0])
            label = next((p.get("label") for p in act.get("paths") or [] if (p.get("steps") or []) is w), None)
            changes.append(f"“{a['name']}” now carries on once it's approved{' (' + label + ')' if label else ''}, doing what the "
                           f"other way does (it stopped at the approval)")


def _said_twice(a, steps, changes):
    """A step done at the end of one way of a branch and again, word for word, straight after the branch (“on hold: they
    owe $X” told for a customer who owes, then again for every job): it was meant for that way only, and the copy after
    did it for every record, twice on that way. The copy after goes."""
    i = 0
    while i < len(steps or []):
        act = steps[i]
        for path in act.get("paths") or []:
            _said_twice(a, path.get("steps") or [], changes)
        for key in ("otherwise", "if_rejected"):
            if isinstance(act.get(key), list):
                _said_twice(a, act[key], changes)
        ways = [(p.get("label"), p.get("steps")) for p in act.get("paths") or []] + [("otherwise", act.get("otherwise"))]
        ends = [(label, way[-1]) for label, way in ways if isinstance(way, list) and way]
        if act.get("type") == "branch" and i + 1 < len(steps) and any(last == steps[i + 1] for _, last in ends):
            what = steps.pop(i + 1)
            said = str(what.get("message") or what.get("subject") or what.get("type")).strip()
            label = next(label for label, last in ends if last == what)
            changes.append(f"“{a['name']}” now does “{said[:60]}{'…' if len(said) > 60 else ''}” only "
                           f"{'otherwise' if label == 'otherwise' else 'when ' + str(label or 'its way applies')}: it was done again "
                           f"straight after, so for every record, and twice then")
            continue
        i += 1


def _one_way_to_do_it(a, t, changes):
    """A stage button and a rule's button with the same name ("Send to customer"): the rule's asks for approval over
    $20,000 and sends the PDF, the plain one went straight to Sent, and the New form pressed the plain one. The rule's
    stays; the plain one goes."""
    if a["trigger"]["type"] != "button":
        return
    key, flow = t["workflow"]["field"], t["workflow"]
    if not any(key in (s.get("values") or {}) for s in schema.steps_of(a["actions"]) if s.get("type") == "update_record"):
        return
    names = {str(a["trigger"].get("label") or "").strip().lower(), a["name"].strip().lower()} - {""}
    same = [m for m in flow["transitions"] if str(m.get("label") or "").strip().lower() in names]
    for m in same:
        flow["transitions"].remove(m)
        changes.append(f"{t['label']} now have one “{m.get('label')}” button: the rule's, which does all it should (a second one "
                       f"went straight to {m['to']} without it)")


FIRST_NAME = re.compile(r"^(first|given|preferred)(_name)?$")
LAST_NAME = re.compile(r"^(last|family|sur)(_?name)?$")


def _whole_name(t, changes):
    """People known by their first name alone ("Emma" in every list, record title and dropdown): two Emmas get mixed up,
    and with 180 members there are several. They go by their whole name."""
    key = schema.title_field(t)
    first = schema.field(t, key) if key else None
    last = next((f for f in t["fields"] if LAST_NAME.match(f["key"]) and f["type"] == "text"), None)
    if not first or not FIRST_NAME.match(first["key"]) or last is None or schema.field(t, "full_name"):
        return
    t["fields"].insert(t["fields"].index(first), {"key": "full_name", "label": "Name", "type": "formula",
                                                  "expression": f"concat({first['key']}, ' ', coalesce({last['key']}, ''))"})
    t["title_field"] = "full_name"
    for s in t.get("sections") or []:
        if first["key"] in s.get("fields", []):
            s["fields"].insert(s["fields"].index(first["key"]), "full_name")
            break
    changes.append(f"{t['label']} go by their whole name (first and last), so two with the same first name aren't mixed up")


def _starts_at(a, t):
    """The stage a rule picks a record up at: the one its conditions say it's moved to, or where a new one starts."""
    key = t["workflow"]["field"]
    said = next((c.get("value") for c in a.get("conditions") or [] if c.get("field") == key and c.get("op") in ("changed_to", "equals")), None)
    if said not in (None, ""):
        return next((s for s in schema.stages(t) if s.lower() == str(said).lower()), None)
    return schema.start_stage(t) if a["trigger"]["type"] in ("record_created", "record_created_or_updated") else None


def _set_when_added(a, t, changes):
    """A rule on a field "changed to" a value people choose as they add the record (a quality check saved as Fail): a new
    one never "changes" to it, so the rule never started for the usual way it happens. It starts when one is added with
    that value too. Not a status, which starts somewhere and moves on (a workflow's stage is the next correction's)."""
    flow = (t.get("workflow") or {}).get("field")
    if a["trigger"]["type"] != "record_updated":
        return
    said = [(f, c.get("value")) for c in a.get("conditions") or [] if isinstance(c, dict) and c.get("op") == "changed_to"
            for f in [schema.field(t, c.get("field") or "")] if f and f["key"] != flow and f["type"] in schema.STORED and not f.get("readonly")
            and f.get("default") in (None, "") and not re.search(r"status|stage|state", f"{f['key']} {f['label']}", re.I)]  # a status starts somewhere and moves
    if not said:
        return
    a["trigger"]["type"] = "record_created_or_updated"
    f, value = said[0]
    changes.append(f"“{a['name']}” now also starts when a {t['singular'].lower()} is added with {f['label']} already "
                   f"{value} (it only started on a change, so one entered that way never counted)")


def _starts_when_added(a, t, changes):
    """A rule on "moved to Pending" for records that start at Pending: a new one is never moved there, so its approval
    (or the work it hands out) was never asked for. It starts when one is added there too."""
    start = schema.start_stage(t)
    key = t["workflow"]["field"]
    if a["trigger"]["type"] != "record_updated" or not start or not any(
            c.get("field") == key and c.get("op") == "changed_to" and str(c.get("value", "")).lower() == start.lower()
            for c in a.get("conditions") or []):
        return
    if not any(s.get("type") in ("request_approval", "task") or key in (s.get("values") or {}) for s in schema.steps_of(a["actions"])):
        return
    a["trigger"]["type"] = "record_created_or_updated"
    one = t["singular"].lower()
    changes.append(f"“{a['name']}” now also starts when a new {one} is added (new ones start at {start}, so it never started)")


def _approval_moves_on(a, t, changes):
    """Approvals that leave the record where it was once everyone has said yes (a request still "Submitted", so no
    purchase order is made, and its Approve button still offered): the stage its own buttons approve it into is set
    after them, with who approved and when; and a no sets the "no" stage when nothing else does."""
    key, flow = t["workflow"]["field"], t["workflow"]
    stage = _starts_at(a, t)
    if not stage:
        return
    said = []

    def onward(at):
        out = [m for m in flow["transitions"] if (not m["from"] or at in m["from"]) and m["to"] != at]
        yes = [m for m in out if not schema.says_no(m["to"])]
        named = [m for m in yes if APPROVED.search(m.get("label") or "") or APPROVED.search(m["to"])]
        no = [m for m in out if schema.says_no(m["to"]) and not CANCEL.search(m["to"])]
        return (named or (yes if len(yes) == 1 else [{}]))[0].get("to"), (no[0]["to"] if len(no) == 1 else None)

    def sets_stage(steps):
        return any(s.get("type") == "update_record" and key in (s.get("values") or {}) and s.get("target") in (None, "", "record")
                   for s in schema.steps_of(steps))

    def fix(steps, at, following):
        for s in steps:
            for path in s.get("paths") or []:
                fix(path.get("steps") or [], at, following + [x for x in steps[steps.index(s) + 1:]])
            if isinstance(s.get("otherwise"), list):
                fix(s["otherwise"], at, following + steps[steps.index(s) + 1:])
            if s.get("type") == "update_record" and s.get("target") in (None, "", "record") and (s.get("values") or {}).get(key):
                at = next((x for x in schema.stages(t) if x.lower() == str(s["values"][key]).lower()), at)  # "Waiting for approval" first
            if s.get("type") == "request_approval":
                yes, no = onward(at)
                if no and not sets_stage(s.get("if_rejected")):
                    s["if_rejected"] = [{"type": "update_record", "values": {key: no}}] + list(s.get("if_rejected") or [])
                    said.append(f"sets it to {no} when someone says no")
        last = max((i for i, s in enumerate(steps) if s.get("type") == "request_approval"), default=None)
        if last is None or sets_stage(steps[last + 1:]) or sets_stage(following):
            return
        before = next((x for x in schema.stages(t) for s in steps[:last] if s.get("type") == "update_record"
                       and s.get("target") in (None, "", "record") and str((s.get("values") or {}).get(key, "")).lower() == x.lower()), at)
        yes, _ = onward(before)
        if not yes:
            return
        values = {key: yes}
        for f in t["fields"]:
            words = f"{f['key']} {f['label']}"
            if f["type"] == "user" and not f.get("multiple") and APPROVED_BY.search(words):
                values[f["key"]], f["readonly"] = "{{approver}}", True
            elif f["type"] in ("date", "datetime") and APPROVED_ON.search(words):
                values[f["key"]], f["readonly"] = "{{today()}}" if f["type"] == "date" else "{{now()}}", True
        steps.insert(last + 1, {"type": "update_record", "values": values})
        said.insert(0, f"moves {'an' if t['singular'][:1].lower() in 'aeiou' else 'a'} {schema.lc(t['singular'])} to {yes} once it's approved"
                       + (f", filling in {_and_words([schema.field(t, k)['label'] for k in values if k != key])}" if len(values) > 1 else ""))

    fix(a["actions"], stage, [])
    if said:
        changes.append(f"“{a['name']}” now {_and_words(list(dict.fromkeys(said)))} (it left it at {stage})")


def _in_turn(steps, lifted):
    """Approvals one after another written inside the first one's "if they say no" steps, after the record is already
    set back (status Rejected): the next approver would only be asked when the first said no. They're lifted out, in turn."""
    out = []
    for act in steps or []:
        out.append(act)
        if act.get("type") == "branch":
            for path in act.get("paths") or []:
                path["steps"] = _in_turn(path.get("steps"), lifted)
            if isinstance(act.get("otherwise"), list):
                act["otherwise"] = _in_turn(act["otherwise"], lifted)
        if act.get("type") == "request_approval" and act.get("if_rejected"):
            rejected = act["if_rejected"]
            i = next((n for n, x in enumerate(rejected) if x.get("type") == "request_approval"), None)
            if i is not None and any(x.get("type") == "update_record" for x in rejected[:i]):
                act["if_rejected"] = rejected[:i]
                lifted.append(str(rejected[i].get("approver") or rejected[i].get("message") or "the next approver").replace("_", " "))
                out += _in_turn(rejected[i:], lifted)
    return out


def _either(conditions):
    """[status = Fail, status = Needs rework] can never hold together: it means either."""
    plain = [c for c in conditions if "any" not in c and "all" not in c]
    same = {}
    for c in plain:
        if c.get("op") in ("equals", "changed_to") and isinstance(c.get("value"), (str, int, float)) and "{{" not in str(c["value"]):
            same.setdefault((c["field"], c["op"]), []).append(c)
    for (field, op), group in same.items():
        if len({str(c["value"]).strip().lower() for c in group}) > 1:
            conditions = [c for c in conditions if c not in group] + [{"any": group}]
    return conditions


CHOOSING = ("equals", "not_equals", "changed_to", "one_of")


def _on_target(t, act, tables, changes, a):
    """A step on a linked record (its order) "only if status is In fitting": it's about the order, so it means the order's
    Status, but it's read on the record that started the rule (an alteration), where it never holds. A field only the
    linked record has, or a choice only its field has, is written as that record's (order.status)."""
    target = act.get("target")
    if t is None or act.get("type") not in ("update_record", "generate_document", "run_automation") or not target or target == "created":
        return
    path = schema.link_path(t, target, tables)
    other = tables.get(path[-1]["table"]) if path else None
    if other is None or other is t:
        return
    moved = []
    for c in schema.condition_leaves(act["only_if"]):
        name = str(c.get("field") or "")
        theirs = schema.field(other, name) if name and "." not in name else None
        if theirs is None:
            continue
        mine = schema.field(t, name)
        values = c.get("value") if isinstance(c.get("value"), list) else [c.get("value")]
        if mine is not None:
            choosing = {mine["type"], theirs["type"]} <= {"select", "multi_select"} and c.get("op") in CHOOSING \
                and all(isinstance(v, str) and "{{" not in v for v in values)
            if not choosing:
                continue
            ours, its = ({str(o).lower() for o in x.get("options") or []} for x in (mine, theirs))
            if not (any(v.lower() not in ours for v in values) and all(v.lower() in its for v in values)):
                continue
        c["field"] = f"{target}.{name}"
        moved.append(theirs.get("label") or name)
    if moved:
        changes.append(f"In “{a['name']}”, the step on its {schema.lc(other.get('singular') or other['label'])} now checks that "
                       f"{schema.lc(other.get('singular') or other['label'])}'s {_and_words(moved)}, as the step is about it")


def _spell(t, conditions, tables):
    """A choice in a condition spelled as one of the field's options: "completed" or "Complete" for Completed."""
    import difflib
    for c in schema.condition_leaves(conditions):
        owner, f = t, None
        for part in str(c.get("field", "")).split(".")[:-1]:
            link = schema.field(owner, part) if owner else None
            owner = tables.get(link["table"]) if link and link["type"] == "link" else None
        f = schema.field(owner, str(c.get("field", "")).split(".")[-1]) if owner else None
        if f is None or f["type"] not in ("select", "multi_select") or c.get("op") not in ("equals", "not_equals", "changed_to", "one_of"):
            continue
        options = f.get("options") or []
        lowered = {o.lower(): o for o in options}

        def spelled(v):
            if not isinstance(v, str) or "{{" in v or v in options:
                return v
            if v.strip().lower() in lowered:
                return lowered[v.strip().lower()]
            close = difflib.get_close_matches(v.strip().lower(), list(lowered), n=2, cutoff=0.8)
            return lowered[close[0]] if len(close) == 1 else v
        c["value"] = [spelled(v) for v in c["value"]] if isinstance(c.get("value"), list) else spelled(c.get("value"))


def _cover_both_sides(t, changes):
    """Moves out of a stage for "more than 10%" and "less than 10%": exactly 10% (or none) would be stuck, so the
    lower side takes it in ("10% or less"), and a discount nobody filled in counts as 0."""
    moves = t["workflow"]["transitions"]
    for stage in schema.stages(t):
        out = [m for m in moves if (not m["from"] or stage in m["from"]) and m["to"] != stage]
        single = [m for m in out if len(m.get("only_if") or []) == 1 and "field" in m["only_if"][0]]
        if len(single) < 2 or len(single) != len(out):
            continue
        fields = {m["only_if"][0]["field"] for m in single}
        values = {str(m["only_if"][0].get("value")) for m in single}
        ops_ = {m["only_if"][0]["op"]: m for m in single}
        if len(fields) != 1 or len(values) != 1 or not {"greater_than", "less_than"} <= set(ops_):
            continue
        ops_["less_than"]["only_if"][0]["op"] = "at_most"
        f = schema.field(t, next(iter(fields)))
        if f is not None and f.get("default") in (None, "") and not f.get("required") and \
                (f["type"] == "percent" or "discount" in f["key"]):
            f["default"] = 0
        changes.append(f"{t['label']} at {stage} can always move on, whatever its {f['label'] if f else 'value'} (“"
                       f"{ops_['less_than'].get('label') or ops_['less_than']['to']}” now includes exactly {next(iter(values))})")


def _lead_to_every_stage(bp, t, changes):
    """A stage no button leads to and no automation sets: Cancelled gets a "Cancel" button wherever there's work still
    going; another (Signed off, Rejected after "Sent to client") a button from the stage before it that leads nowhere."""
    flow, names = t["workflow"], schema.stages(t)
    start = schema.start_stage(t) or names[0]
    set_by_rules = {str((act.get("values") or {}).get(flow["field"], "")).lower() for a in bp["automations"]
                    for act in schema.steps_of(a["actions"])}
    reached, grew = {start} | {n for n in names if n.lower() in set_by_rules}, True
    while grew:
        grew = False
        for m in flow["transitions"]:
            if (not m["from"] or set(m["from"]) & reached) and m["to"] not in reached:
                reached.add(m["to"])
                grew = True
    missing = [n for n in names if n not in reached]
    if not missing or len(missing) > 6:
        return
    leaving = {x for m in flow["transitions"] for x in (m["from"] or names) if m["to"] != x}
    going = [n for n in names if n in reached and n in leaving and not FINAL.search(n)]
    stuck = [n for n in names if n in reached and n not in leaving and not FINAL.search(n) and n != start]
    last = stuck[0] if len(stuck) == 1 else None  # where the chain of stages nothing leads to picks up
    yes_from = None
    for stage in missing:
        if CANCEL.search(stage):
            sources, label = going, "Cancel"
        elif last is not None and names.index(last) < names.index(stage):
            # Scored, Recommended, Awarded after Submitted: each from the one before; a "no" (Not awarded, Rejected)
            # from the same place as the "yes" beside it
            label = f"Mark as {stage[:1].lower() + stage[1:]}"
            if NO.search(stage):
                sources = [yes_from or last]
            else:
                sources, yes_from, last = [last], last, stage
        else:
            continue
        if sources:
            flow["transitions"].append({"from": list(sources), "to": stage, "label": label})
            one = t["singular"].lower()
            changes.append(f"Added a “{label}” button to {t['label']} (from {_and_words(sources)}), so {'an' if one[:1] in 'aeiou' else 'a'} "
                           f"{one} can get to {stage}")


def _resolve_workflow(t, changes):
    """A workflow's stages as its field's options are spelled ("approved" is "Approved"); a stage it moves records to
    that the field hasn't got is added to it (a Rejected the AI forgot to list)."""
    flow = t["workflow"]
    f = schema.field(t, flow["field"])
    if not f or f["type"] != "select":  # "stage" for the one choice field holding every stage it names
        named = {m["to"].lower() for m in flow["transitions"]} | {x.lower() for m in flow["transitions"] for x in m["from"]}
        fits = [x for x in t["fields"] if x["type"] == "select" and named <= {o.lower() for o in x.get("options", [])}]
        if len(fits) != 1:
            return
        f = fits[0]
        flow["field"] = f["key"]
    spelled = {o.lower(): o for o in f.get("options", [])}
    added = []
    for m in flow["transitions"]:
        m["from"] = [spelled.get(x.lower(), x) for x in m["from"]]
        if m["to"].lower() not in spelled and len(f.get("options", [])) < 40:
            f["options"] = _placed(list(f.get("options", [])), m["to"], m["from"])
            spelled[m["to"].lower()] = m["to"]
            added.append(m["to"])
        m["to"] = spelled.get(m["to"].lower(), m["to"])
    if flow.get("start"):
        flow["start"] = spelled.get(flow["start"].lower(), flow["start"])
    if added:
        changes.append(f"Added {_and_words(added)} to the stages of {t['label']} ({f['label']})")


def _placed(stages, stage, sources):
    """The stages with a new one where its moves put it: straight after the stage it's reached from (On hold after
    Applied, not after Adopted). One that stops or says no (Withdrawn, Rejected), or that's reached from anywhere, goes last."""
    at = [stages.index(x) for x in sources if x in stages]
    if schema.says_no(stage) or schema.STOPS.search(stage) or not at or len(at) > 1:
        return stages + [stage]
    return stages[:at[0] + 1] + [stage] + stages[at[0] + 1:]


SEEN_BY = {"viewer": "everyone in the app", "editor": "everyone who can do the work", "admin": "only people who can change the app",
           "owner": "only the owner"}


def _seen_by(bp, access, rows=None):
    """Who sees something, in words: "everyone who can do the work", "only Practice manager (and people who can change the app)"."""
    access = access if isinstance(access, dict) else {"view": access} if access else {}
    names = {tm["key"]: tm["name"] for tm in schema.teams(bp)}
    if access.get("teams"):
        said = f"only {_and_words([names.get(k, k) for k in access['teams']])} (and people who can change the app)"
    elif access.get("view") in names:
        said = f"only {names[access['view']]} (and people who can change the app)"
    else:
        said = SEEN_BY.get(access.get("view") or "viewer", "everyone in the app")
    if rows:
        said += ", each only their own" + (f" ({_and_words([names.get(k, k) for k in rows['teams']])} all of them)" if rows.get("teams") else "")
    return said


def access_changes(old_bp, new_bp):
    """Who can see what, where a change made it different: said first in the builder's reply, since a list or a field
    seen by people it shouldn't be (clinical notes, pay rates) is the worst thing a change can do quietly."""
    out = []
    old_tables = {t["key"]: t for t in (old_bp or {}).get("tables", [])}
    for t in (new_bp or {}).get("tables", []):
        was = old_tables.get(t["key"])
        if was is None:
            continue
        before = _seen_by(old_bp, was.get("access"), was.get("row_access"))
        after = _seen_by(new_bp, t.get("access"), t.get("row_access"))
        if before != after:
            out.append(f"{t['label']} can now be seen by {after} (before: {before})")
        for f in t["fields"]:
            g = schema.field(was, f["key"])
            if g is not None and (g.get("access") or None) != (f.get("access") or None) and (g.get("access") or f.get("access")):
                out.append(f"{t['label']}{'’' if t['label'].endswith('s') else '’s'} {f['label']} can now be seen by {_seen_by(new_bp, f.get('access'))} "
                           f"(before: {_seen_by(old_bp, g.get('access'))})")
    return out


def _and_words(items):
    items = list(items)
    if not items:
        return ""
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _as_bank_file(doc, tables):
    """A payment file from a PDF-shaped document: a CSV with a header of the lines' columns and a line per record listed,
    each payee's BSB and account number (fields of the record a column links to) next to its name."""
    source = tables.get(doc["lines"]["table"])
    if source is None:
        return
    columns = [schema.field(source, c) for c in doc["lines"].get("columns") or []] or \
        [f for f in source["fields"] if f["type"] not in schema.FILES and f["key"] != doc["lines"].get("via")][:6]
    heads, cells = [], []
    for c in [c for c in columns if c]:
        heads.append(c["label"])
        cells.append(f"{{{{line.{c['key']}}}}}")
        target = tables.get(c.get("table")) if c["type"] == "link" else None
        for g in (target or {}).get("fields", []):
            if g.get("sensitive") or re.search(r"bsb|bank|account_(number|no)|iban|swift|sort_code|routing", g["key"]):
                heads.append(g["label"])
                cells.append(f"{{{{line.{c['key']}.{g['key']}}}}}")
    doc.update({"format": "text", "extension": "csv", "header": ",".join(heads), "line": ",".join(cells), "footer": ""})
    for part in ("fields", "details", "totals", "to", "sections", "net", "notes"):  # the PDF's parts (its number stays: the file's name)
        doc.pop(part, None)


def _resolve_document(t, doc, tables, keys):
    """A document's lists reach the record by the one path there is; "to" the table's own kind is the record itself (a
    statement to the owner it's about); header fields and totals the table hasn't got (an invoice's subtotal on a
    statement) are left off."""
    words = f"{doc['key']} {doc['label']} {doc.get('title', '')}".lower().replace("_", " ")
    if doc.get("format") != "text" and doc.get("lines") and re.search(r"\b(bank|payment|aba|eft|abn amro)\s+file\b|\baba\b|\bcsv\b|\beft\b", words):
        _as_bank_file(doc, tables)  # a bank's file asked for as a PDF: a CSV the bank can read, the payees' bank details in it
    if doc.get("format") == "text":
        if doc.get("lines") and not doc["lines"].get("via"):
            source = tables.get(doc["lines"]["table"])
            links = [f["key"] for f in (source or {}).get("fields", []) if f["type"] == "link" and f["table"] == t["key"]]
            if len(links) == 1:
                doc["lines"]["via"] = links[0]
        return
    for part in ("fields", "details", "totals"):
        doc[part] = [k for k in doc.get(part, []) if k in keys]
    if doc.get("number_field") and doc["number_field"] not in keys:
        del doc["number_field"]
    to = doc.get("to")
    if to and to["field"] not in keys and to["field"] != "record":
        own = {t["key"], t["key"].rstrip("s"), schema.slug(t.get("singular", "")), "self", "this", "record"}
        if to["field"] in own:
            to["field"] = "record"
            to["details"] = [k for k in to.get("details", []) if k in keys]
        else:
            del doc["to"]
    for lines in ([doc["lines"]] if doc.get("lines") else []) + doc.get("sections", []):
        source = tables.get(lines["table"])
        if not source or schema.reach(t, source, lines["via"], tables):
            continue
        links = [f for f in source["fields"] if f["type"] == "link"]
        paths = [f["key"] for f in links] + [f"{f['key']}.{g['key']}" for f in links if f["table"] in tables
                                              for g in tables[f["table"]]["fields"] if g["type"] == "link"]
        reached = {p: schema.reach(t, source, p, tables) for p in paths}
        for anchored in (False, True):  # to the record itself, nearest first; else to the one it's about (its owner)
            fits = [p for p, r in reached.items() if r and bool(r[1]) == anchored]
            fits = [p for p in fits if "." not in p] or fits
            if fits:
                if len(fits) == 1:
                    lines["via"] = fits[0]
                break


def _resolve_portals(bp, tables, changes):
    """Records shown in the portal to people who have no portal yet (a subcontractor's jobs) get them theirs: nobody
    sees anything until they're invited from their record."""
    current = schema.portals(bp)
    for t in bp["tables"]:
        portal = t.get("portal")
        own = next((p for p in current if p["table"] == t["key"]), None)
        if portal and own is not None and not portal.get("everyone") and (portal["via"] in ("record", "self", "id", "this", "own", "contact", t["key"], schema.slug(t["singular"]))
                                           or not schema.field(t, portal["via"].partition(".")[0])):
            # the portal's people seeing their own record: that's the portal's "profile" (their details, kept up to date)
            keys = {f["key"]: f for f in t["fields"]}
            stage = (t.get("workflow") or {}).get("field")
            said = portal.get("profile") if isinstance(portal.get("profile"), list) else None
            fields = [k for k in (said or portal.get("edit") or portal["fields"]) if k in keys and keys[k]["type"] in schema.STORED - {"autonumber"}
                      and k != stage and k != "status" and not keys[k].get("access")]
            own["profile"] = fields or [f["key"] for f in t["fields"] if f["type"] in schema.STORED - {"autonumber", "link", "user"}
                                        and not f.get("access") and f["key"] not in (stage, "status")][:10]
            if portal.get("sign_up") and "sign_up" not in own:
                join = portal["sign_up"]
                own["sign_up"] = {"fields": [schema.slug(k) for k in (join.get("fields") or []) if schema.slug(k) in keys]
                                  if isinstance(join, dict) else []}
            bp["portals"] = current
            bp.pop("portal", None)
            t.pop("portal")
            changes.append(f"In the {own['title']}, {schema.lc(t['label'])} keep their own details up to date "
                           f"({', '.join(keys[k]['label'] for k in own['profile'] if k in keys)})"
                           + (" and can join by themselves" if own.get("sign_up") is not None else ""))
            continue
        if portal:  # lines are shown with their record anyway, and what's worked out can't be typed: dropped, not refused
            keys = {f["key"]: f for f in t["fields"]}
            portal["fields"] = [k for k in portal["fields"] if k in keys or k not in tables]
            if portal.get("edit"):  # (an empty list means everything they see, so a list emptied here goes)
                kept = [k for k in portal["edit"] if k not in keys or keys[k]["type"] not in schema.CALCULATED]
                if kept:
                    portal["edit"] = kept
                else:
                    portal.pop("edit")
        via = schema.field(t, (t.get("portal") or {}).get("via", "").partition(".")[0])
        people = tables.get(via["table"]) if via is not None and via["type"] == "link" else None
        emails = [f["key"] for f in (people or {}).get("fields", []) if f["type"] == "email"]
        if people is None or len(emails) != 1 or any(schema.portal_shows(tables, t, p["table"]) for p in current):
            continue
        portal = schema.normalize_portal({"table": people["key"], "email_field": emails[0], "title": f"{people['singular']} portal"})
        current.append(portal)
        bp["portals"] = current
        bp.pop("portal", None)
        changes.append(f"Set up the {portal['title']} for {people['label']}, where they see their {schema.lc(t['label'])}")


WHEN_SAID = {"record_created": "when {} is added", "record_updated": "when {} changes", "record_created_or_updated": "when {} is added or changed",
             "record_deleted": "when {} is deleted", "email_received": "when an email arrives", "file_added": "when a file arrives",
             "webhook_received": "when another system sends data"}


def _a(thing):
    """“an order”, “a quote”."""
    return f"{'an' if str(thing)[:1].lower() in 'aeiou' else 'a'} {thing}"


# A field's settings as the change list says them (never their keys: "Changed Orders › Status (its colours)")
SETTING_WORDS = {"label": "its name", "type": "what it keeps", "options": "its choices", "option_colors": "its colours", "required": "whether it must be filled in",
                 "unique": "whether two can be the same", "help": "its help", "default": "what it starts as", "placeholder": "its hint",
                 "sensitive": "whether it's kept private", "access": "who can see it", "decimals": "its decimal places", "unit": "its unit",
                 "currency": "its currency", "before": "what's shown before it", "after": "what's shown after it", "yes_label": "what yes says",
                 "no_label": "what no says", "ai": "what the AI fills in", "expression": "how it's worked out", "multiple": "whether it takes several",
                 "table": "the list it links to", "readonly": "whether it can be changed", "show_if": "when it's shown", "hide_if": "when it's hidden",
                 "required_if": "when it must be filled in", "readonly_if": "when it's locked", "min": "its lowest", "max": "its highest",
                 "pattern": "its format", "pattern_message": "its format message", "format": "its format", "template": "the link it makes",
                 "display": "how it's shown", "prefix": "its prefix", "digits": "its digits", "options_by": "which choices it offers"}


TOLD_SAID = {"owner": "the owner", "creator": "whoever added it", "admin": "the admins"}
COND_SAID = {"equals": "is {}", "not_equals": "is not {}", "contains": "contains {}", "is_empty": "is empty", "not_empty": "is filled in",
             "greater_than": "is more than {}", "less_than": "is less than {}", "at_least": "is at least {}", "at_most": "is at most {}",
             "one_of": "is one of {}", "before_today": "is before today", "after_today": "is after today", "today": "is today",
             "within_next_days": "is within the next {} days", "within_last_days": "is within the last {} days",
             "more_than_days_ago": "is more than {} days ago", "changed": "changes", "changed_to": "changes to {}"}


def _cond_said(label, c):
    """A condition as people say it: “Due is more than 14 days ago”, never “Due more than days ago 14”."""
    value = c.get("value")
    value = ", ".join(map(str, value)) if isinstance(value, list) else "" if value is None else str(value)
    words = COND_SAID.get(c.get("op"), str(c.get("op", "")).replace("_", " ") + " {}")
    return f"{label} {words.format(value)}".strip()


def _whose_path(tables, t, path):
    """{{record.customer.email}} as people say it: “its customer's email”, each step through a link by its label."""
    words, at = [], t
    for part in path.split("."):
        f = schema.field(at, part) if at else None
        words.append(((f or {}).get("label") or part.replace("_", " ")).lower())
        at = tables.get(f["table"]) if f and f["type"] == "link" else None
    return "its " + "'s ".join(words)


NOTIFY_KEYS = ("message", "subject")


def _kept_steps(bp, sent):
    """A rule sent again whole to change it (a notify added): the model often drops what a step already had (a step that
    changes a linked record losing its "target", its "values" or its "only_if") or folds the new message into it. What a
    step at the same place of the same kind had and the new one leaves out is kept, and a message folded into a step
    that changes a record becomes its own notify step after it."""
    key = slug(str(sent.get("key") or sent.get("name") or ""))
    existing = next((x for x in bp.get("automations", []) if x["key"] == key or slug(x.get("name") or "") == key), None)
    steps = sent.get("actions") if isinstance(sent.get("actions"), list) else sent.get("steps") if isinstance(sent.get("steps"), list) else None
    if existing is None or steps is None:
        return sent
    out = []
    for i, step in enumerate(steps):
        old = existing["actions"][i] if i < len(existing["actions"]) else None
        if not isinstance(step, dict):
            out.append(step)
            continue
        step = dict(step)
        if old and str(step.get("type") or "") == old.get("type"):
            for k in ("values", "target", "only_if", "table"):
                if k not in step and k in old:
                    step[k] = copy.deepcopy(old[k])
        told = None
        if step.get("type") in ("update_record", "create_record") and any(k in step for k in NOTIFY_KEYS):
            told = {"type": "notify", **{k: step.pop(k) for k in NOTIFY_KEYS if k in step}}
            to = step.pop("to", None)
            table = schema.table(bp, (existing.get("trigger") or {}).get("table") or "")
            link = schema.field(table, slug(str(to))) if table and to is not None and step["type"] == "update_record" else None
            if link is not None and link["type"] == "link":  # the record it changes ("to": "invoice"), not who hears
                step.setdefault("target", slug(str(to)))
            elif to is not None:
                told["to"] = to
        out.append(step)
        if told and not any(isinstance(x, dict) and x.get("type") == "notify" for x in steps):
            out.append(told)
    return {**sent, "actions": out} if isinstance(sent.get("actions"), list) else {**sent, "steps": out}


def _automation_said(bp, a):
    """What an automation does, in brief, for the change list: the closing reply knows only what that list says, so an
    automation that asks the procurement manager to approve must say so."""
    tables = {t["key"]: t for t in bp.get("tables", [])}
    t = tables.get(a["trigger"].get("table"))
    label = lambda k, of=None: ((schema.field(of or t, k) or {}).get("label") or str(k).replace("_", " "))  # noqa: E731
    thing = lambda key: (tables.get(key) or {}).get("singular", str(key).replace("_", " ")).lower()  # noqa: E731
    cond = lambda cs: " and ".join(_cond_said(label(c["field"]), c) for c in list(schema.condition_leaves(cs))[:2])  # noqa: E731
    trig = a["trigger"]
    when = WHEN_SAID.get(trig["type"], "").format(_a(t["singular"].lower() if t else "record")) if trig["type"] in WHEN_SAID else \
        f"every {trig.get('every', 'day')}" if trig["type"] == "schedule" else f"from its “{trig.get('label') or a['name']}” button"
    leaves = list(schema.condition_leaves(a["conditions"])) if a["conditions"] else []
    moved = next((c for c in leaves if c.get("op") in ("changed", "changed_to")), None) if t and trig["type"] in ("record_updated", "record_created_or_updated") else None
    if moved:  # “when an order's Status changes to Delivered”, not “when an order changes if Status changes to Delivered”
        when = f"when {_a(t['singular'].lower())}'s {_cond_said(label(moved['field']), moved)}"
        leaves = [c for c in leaves if c is not moved]
    if leaves:
        when += " if " + " and ".join(_cond_said(label(c["field"]), c) for c in leaves[:2])
    said = []
    for x in schema.steps_of(a["actions"]):
        kind = x["type"]
        how = " (switched off: a person does it)" if x.get("manual") else {"always": " (a person checks it)", "when_unsure": " (a person checks it when unsure)"}.get(x.get("review"), "")
        if kind == "ai_fill":
            said.append(f"the AI fills {label(x['field'])}{how}")
        elif kind == "ai_check":
            against = ", ".join((tables.get(r["table"]) or {}).get("label", r["table"]) for r in x.get("reads", []))
            said.append(f"the AI checks it into {label(x['field'])}" + (f" against {against}" if against else "") + how)
        elif kind == "ai_extract":
            said.append(f"the AI reads the attached files{how}")
        elif kind == "ai_create_record":
            said.append(f"the AI makes {_a(thing(x['table']))}{how}")
        elif kind == "request_approval":
            who = " and ".join(label(w) if w not in schema.APPROVER_ROLES + ["creator"] else w for w in x.get("approvers") or [x["approver"]])
            said.append(f"asks {who} to approve" + (f" if {cond(x['only_if'])}" if x.get("only_if") else "")
                        + (" (never whoever raised it)" if "creator" in x.get("not_by", []) else " (kept apart from some people)" if x.get("not_by") else "")
                        + (" after confirming their sign-in" if x.get("step_up") else "")
                        + (" (if rejected, it says what happens)" if x.get("if_rejected") else ""))
        elif kind == "task":
            said.append(f"asks {x['to'] if x['to'] in schema.APPROVER_ROLES + ['creator'] else label(x['to'])} to {x['message'][:60].rstrip('.')}")
        elif kind == "branch":
            said.append("goes one of " + str(len(x["paths"]) + bool(x.get("otherwise"))) + " ways by " + ", ".join(
                sorted({label(c["field"]) for p in x["paths"] for c in schema.condition_leaves(p["when"])})))
        elif kind == "create_record":
            said.append(f"adds {_a(thing(x['table']))}")
        elif kind == "create_for_each":
            said.append(f"adds {(tables.get(x['table']) or {}).get('label', x['table'] or 'records').lower()} for each "
                        + (label(x["source"]["field"]).lower() if x["source"].get("field") else
                           (tables.get(x["source"]["table"]) or {}).get("singular", "one").lower() if x["source"].get("all") else "line"))
        elif kind == "copy_record":
            said.append("copies it with its lines" + (f", linked back by {label(x['link_back'])}" if x.get("link_back") else ""))
        elif kind == "update_record":
            said.append("sets " + ", ".join(label(k) for k in list(x["values"])[:3]))
        elif kind in ("notify", "send_email"):
            to = str(x.get("to") or "everyone")
            said.append(("emails " if kind == "send_email" else "tells ") + TOLD_SAID.get(to, re.sub(r"\{\{\s*record\.([\w.]+)\s*\}\}", lambda m: _whose_path(tables, t, m.group(1)), to)))
        elif kind == "generate_document":
            said.append("produces the PDF")
        elif kind == "wait":
            said.append("waits")
        elif kind == "run_automation":
            said.append(f"runs “{x['automation']}”")
        else:
            said.append(kind.replace("_", " "))
    return when + ": " + "; ".join(said[:10]) + ("; …" if len(said) > 10 else "")


def _condition_words(t, c):
    """A condition in words: a field's test (Status equals Sent), a formula as written (tender.bids_close_at <= now()), or a
    group of either."""
    if c.get("formula"):
        return str(c["formula"])
    for word in ("any", "all"):
        if c.get(word):
            joined = (" or " if word == "any" else " and ").join(_condition_words(t, x) for x in c[word])
            return f"({joined})"
    label = (schema.field(t, c.get("field", "")) or {"label": c.get("field", "")})["label"]
    return f"{label} {str(c.get('op', '')).replace('_', ' ')} {c.get('value', '')}".strip()


def _policies_said(t):
    """A table's permissions, locks and rules in the owner's words, for the change list (and so the reply knows)."""
    said, access = [], t.get("access") or {}
    if access.get("view") in ("admin", "owner"):
        said.append(f"seen only by {access['view']}s")
    elif access.get("edit") in ("admin", "owner"):
        said.append(f"changed only by {access['edit']}s")
    if access.get("teams"):
        said.append(f"kept to the {_and_words(access['teams'])} team{'s' if len(access['teams']) > 1 else ''} (and admins)")
    kept = {}
    for f in t["fields"]:
        if f.get("access"):
            kept.setdefault(f["access"], []).append(f["label"])
    said += [f"{', '.join(labels)} kept to {role + 's' if role in schema.ROLE_NAMES else 'the ' + role + ' team'}" for role, labels in kept.items()]
    secret = [f["label"] for f in t["fields"] if f.get("sensitive")]
    if secret:
        said.append(f"{', '.join(secret)} kept encrypted and shown masked")
    if t.get("row_access"):
        by = [schema.field(t, r.get("field", "")) or {"label": r.get("field", "")} for r in schema.row_rules(t)]
        said.append(f"each person seeing only their own (by {' and by '.join(b['label'] for b in by)})")
    if t.get("lock_if"):
        said.append("locked when " + " and ".join(_condition_words(t, c) for c in t["lock_if"]))
    if t.get("rules"):
        said.append(f"{len(t['rules'])} rule{'s' if len(t['rules']) != 1 else ''} every save must keep")
    if t.get("workflow") and schema.stages(t):
        buttons = [m.get("label") or f"to {m['to']}" for m in t["workflow"]["transitions"]]
        said.append(f"moving through {' → '.join(schema.stages(t))} by its buttons ({', '.join(buttons[:6])})")
        for m in t["workflow"]["transitions"]:
            if m.get("not_by"):
                said.append(f"“{m.get('label') or m['to']}” never by {_and_words([_apart_said(x, t) for x in m['not_by']])}")
            if m.get("step_up"):
                said.append(f"“{m.get('label') or m['to']}” after confirming their sign-in")
    for group in t.get("unique_together", []):
        said.append(f"no two alike in {_and_words([(schema.field(t, k) or {'label': k})['label'] for k in group])}")
    for limit in t.get("limits", []):
        per = _and_words([(schema.field(t, k) or {"label": k})["label"].lower() for k in limit["per"]])
        if limit.get("overlap"):
            said.append(f"no two at overlapping times" + (f" for the same {per}" if per else ""))
        else:
            most = f"{limit['max']:g}" if not isinstance(limit["max"], str) else f"its {limit['max'].replace('_', ' ').replace('.', '’s ')}"
            said.append(f"at most {most}" + (f" for the same {per}" if per else " at a time"))
    held = t.get("approve_changes")
    if held:
        what = _and_words([(schema.field(t, k) or {"label": k})["label"] for k in held["fields"]]) or "any change"
        said.append(f"changes to {what} waiting for {held['approver']}'s approval (never the person who made them)"
                    + (f", no sooner than {held['wait_days']:g} days later" if held.get("wait_days") else "")
                    + (f", with {held['tell']} told and able to cancel" if held.get("tell") else ""))
    if t.get("sealed"):
        said.append(f"sealed until {t['sealed']['until'].replace('_', ' ').replace('.', '’s ')}: nobody but whoever made each one sees what's "
                    "in it, admins included, and nobody can change it after")
    elif t.get("audit_views"):
        said.append("every opening kept in its history")
    if t.get("journey"):
        said.append(f"its journey shown as {' → '.join(s['label'] for s in t['journey'])}")
    return said


def _apart_said(who, t):
    if who == "creator":
        return "whoever raised it"
    if str(who).lower() in [x.lower() for x in schema.stages(t)]:
        return f"whoever moved it to {who}"
    f = schema.field(t, who)
    return f["label"] if f else who


HOME_MONEY = re.compile(r"(^|_)(base|home|local|converted|aud|usd|nzd|gbp|eur|cad|sgd)(_|$)")


def _money(f):
    return f["type"] == "money" or (f["type"] in ("formula", "lookup") and f.get("format") == "money")


def _resolve_currency_lookups(bp, tables, changes, new):
    """A new purchase order (with priced lines) to a supplier who invoices in a currency of their own takes that
    currency, shown on it, so it and its lines are in it."""
    for t in bp["tables"]:
        if t["key"] not in new or t.get("embedded") or schema.field(t, "currency"):
            continue
        priced = any(c.get("embedded") and any(_money(f) for f in c["fields"]) and any(f["type"] == "link" and f["table"] == t["key"] for f in c["fields"])
                     for c in bp["tables"])
        parties = [f for f in t["fields"] if f["type"] == "link" and not f.get("multiple")
                   and (schema.field(tables.get(f["table"]), "currency") or {}).get("type") in ("select", "text")]
        if priced and len(parties) == 1:
            t["fields"].append(schema.normalize_field({"key": "currency", "label": "Currency", "type": "lookup", "link": parties[0]["key"], "field": "currency"}))
            changes.append(f"Added Currency to {t['label']} (its {schema.lc(parties[0]['label'])}'s), so its amounts are in it")


def _resolve_currency(t, tables):
    """A record with a currency (a supplier's USD purchase order) has its amounts in it, and so do its lines, unless
    they're converted into the business's own (convert(), or named so: "total_aud", "base_amount")."""
    def codes(table):
        f = schema.field(table, "currency") if table else None
        return f is not None and f["type"] in ("select", "text", "lookup", "formula")
    for f in t["fields"]:  # one whose currency field has gone (removed by the owner) is in the business's own again
        head, _, rest = (f.get("currency_field") or "").partition(".")
        link = schema.field(t, head) if rest else None
        if head and not (codes(tables.get(link["table"])) if link and link["type"] == "link" else not rest and schema.field(t, head)):
            del f["currency_field"]
    if codes(t):
        source = "currency"
    else:
        parents = [f["key"] for f in t["fields"] if f["type"] == "link" and not f.get("multiple") and codes(tables.get(f["table"]))]
        if not t.get("embedded") or len(parents) != 1:
            return
        source = f"{parents[0]}.currency"
    for f in t["fields"]:
        if f["type"] == "rollup":
            totalled = schema.field(tables.get(f["table"]), f.get("field") or "") if tables.get(f["table"]) else None
            money = f["aggregate"] != "count" and totalled is not None and (totalled["type"] == "money" or totalled.get("format") == "money")
        else:
            money = f["type"] == "money" or (f["type"] == "formula" and f.get("format") == "money")
        if money and not f.get("currency") and not f.get("currency_field") and "convert(" not in f.get("expression", "") \
                and not HOME_MONEY.search(f["key"]):
            f["currency_field"] = source


def _as_record(value):
    if not isinstance(value, str) or "created" not in value:
        return value
    return expr.TEMPLATE.sub(lambda m: "{{" + re.sub(r"\bcreated\b", "record", m.group(1)) + "}}", value)


def _gives_words(expression):
    """Whether a formula can only ever give words (“iif(on_hand <= reorder_level, 'Low', 'OK')”), never a number."""
    import ast
    try:
        tree = expr.parse(expression)
    except expr.ExprError:
        return False

    def words(node):
        if isinstance(node, ast.Constant):
            return isinstance(node.value, str)
        if isinstance(node, ast.IfExp):
            return words(node.body) and words(node.orelse)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            name = expr.ALIASES.get(node.func.id.lower(), node.func.id.lower())
            if name == "iif":
                return len(node.args) >= 3 and words(node.args[1]) and words(node.args[2])
            return name in ("concat", "upper", "lower", "text", "format_date", "pad", "lpad")
        return False
    return words(tree.body)


def _resolve_percents(t):
    """Formulas and rules written with percentages as people say them ("discount_percent / 100", "> 10") saved as they
    work: percentages are kept as fractions."""
    for f in t["fields"]:
        if f["type"] == "formula":
            f["expression"] = schema.percents_fixed(t, f["expression"])
            if f.get("format") == "percent":
                f["expression"] = _as_fraction(f["expression"])
    for rule in t.get("rules", []):
        rule["check"] = schema.percents_fixed(t, rule["check"])


def _as_fraction(expression):
    """A percentage formula written to give a whole number ("(revenue - cost) / revenue * 100", which shows as 9320%) as
    percentages are kept, a fraction: the "* 100" taken off, and a round() around it keeping two more places (93.2% stays
    93.2%). One that also divides by 100 (rounding a fraction) is left as it is."""
    import ast
    if not isinstance(expression, str) or not re.search(r"\*\s*100\b|\b100(?:\.0*)?\s*\*", expression) or re.search(r"/\s*100\b", expression):
        return expression
    try:
        tree = copy.deepcopy(expr.parse(expression))  # (parse keeps what it read: never changed in place)
    except expr.ExprError:
        return expression

    class Hundreds(ast.NodeTransformer):
        taken = 0

        def visit_BinOp(self, node):
            self.generic_visit(node)
            if isinstance(node.op, ast.Mult):
                for kept, other in ((node.left, node.right), (node.right, node.left)):
                    if isinstance(other, ast.Constant) and not isinstance(other.value, bool) and other.value == 100:
                        self.taken += 1
                        return kept
            return node

        def visit_Call(self, node):
            before = self.taken
            self.generic_visit(node)
            if self.taken > before and isinstance(node.func, ast.Name) and node.func.id.lower() in ("iif", "if"):
                node.args = [ast.Constant(1) if i and isinstance(a, ast.Constant) and not isinstance(a.value, bool) and a.value == 100
                             else a for i, a in enumerate(node.args)]  # the other way's 100 (per cent) is 1 too
            if self.taken > before and isinstance(node.func, ast.Name) and node.func.id.lower() == "round":
                if len(node.args) == 1:
                    node.args.append(ast.Constant(2))
                elif isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, int):
                    node.args[1] = ast.Constant(node.args[1].value + 2)
            return node

    fixed = Hundreds()
    tree = fixed.visit(tree)
    return ast.unparse(tree) if fixed.taken else expression


def _resolve_lookup(t, f, tables):
    if "where" in f:  # found by its conditions, not through a link: a field left out is the found record's namesake
        source = tables.get(f.get("table"))
        if f["field"] or source is None:
            return
        named = [x["key"] for x in source["fields"] if x["key"] == f["key"] or x["label"].lower() == f["label"].lower()]
        people = [x["key"] for x in source["fields"] if x["type"] == "user"]
        if named:
            f["field"] = named[0]
        elif len(people) == 1 and any(w in f["key"] for w in ("approver", "manager", "owner", "person", "who", "signatory", "delegate")):
            f["field"] = people[0]  # the approver: the one person on an approval limit
        return
    links = [x for x in t["fields"] if x["type"] == "link"]
    link = schema.field(t, f["link"]) if f["link"] else None
    if f["link"] and (not link or link["type"] != "link"):  # named by the table it reaches: "material" for the link to Materials
        to = [x for x in links if x["table"].rstrip("s") == f["link"].rstrip("s")]
        link = to[0] if len(to) == 1 else link
    if not f["link"]:
        wanted = f["field"] or f["key"]
        to = [x for x in links if schema.field(tables.get(x["table"]), wanted)]
        link = to[0] if len(to) == 1 else None
    if not link or link["type"] != "link":
        return
    f["link"] = link["key"]
    source = tables.get(link["table"])
    if f["field"] or not source:
        return
    for key in (f["key"], f["key"].removeprefix(link["key"] + "_"), f["key"].removeprefix(link["table"].rstrip("s") + "_")):
        if key != link["key"] and schema.field(source, key):
            f["field"] = key
            return
    named = [x["key"] for x in source["fields"] if x["label"].lower() == f["label"].lower()]
    if len(named) == 1:
        f["field"] = named[0]


def _apply_one(bp, op, raw, result, where):
    changes, problems = result.changes, result.problems
    if op == "set_theme":
        theme = schema.normalize_theme(_first(raw, "theme", default={k: v for k, v in raw.items() if k != "op"}))
        if not theme:
            raise SchemaError("give an accent colour and/or a density")
        bp["theme"] = {**(bp.get("theme") or {}), **theme}
        changes.append("Changed the look: " + ", ".join(f"{k} {v}" for k, v in theme.items()))
    elif op in ("add_document", "update_document", "remove_document"):
        t = _table(bp, _first(raw, "table"), problems, where)
        if not t:
            return
        docs = t.setdefault("documents", [])
        if op == "remove_document":
            key = slug(_first(raw, "key", "document", default=""))
            found = next((d for d in docs if d["key"] == key), None)
            if not found:
                raise SchemaError(f"{t['label']} has no document “{key}”")
            docs.remove(found)
            changes.append(f"Removed the document {t['label']} › {found['label']}")
            return
        doc = schema.normalize_document(_first(raw, "document", "template", default={k: v for k, v in raw.items() if k not in ("op", "table")}))
        existing = next((d for d in docs if d["key"] == doc["key"]), None)
        if existing:
            docs[docs.index(existing)] = doc
            changes.append(f"Changed the document {t['label']} › {doc['label']}")
        elif op == "update_document":
            raise SchemaError(f"{t['label']} has no document “{doc['key']}”")
        else:
            docs.append(doc)
            changes.append(f"Added the document {t['label']} › {doc['label']} ("
                           + (f"a .{doc['extension']} file in its own layout, made from each {schema.lc(t['singular'])}" if doc.get("format") == "text"
                              else f"a PDF you can produce from each {schema.lc(t['singular'])}") + ")")
        result.navigate = result.navigate or f"table:{t['key']}"
    elif op == "set_public_form":
        t = _table(bp, _first(raw, "table"), problems, where)
        if not t:
            return
        form = schema.normalize_form(_first(raw, "form", "public_form", default={k: v for k, v in raw.items() if k not in ("op", "table")}))
        if not form["fields"]:
            form["fields"] = [f["key"] for f in t["fields"] if f["type"] in schema.STORED and f["type"] not in ("autonumber", "link", "user")][:8]
        t["public_form"] = form
        changes.append(f"{'Set up' if form['enabled'] else 'Turned off'} the public form for {t['label']} ({len(form['fields'])} questions)")
    elif op == "rename_app":
        name = str(_first(raw, "name", "label", "title", default="")).strip()
        if not name:
            raise SchemaError("give the new name")
        bp["name"] = name[:80]
        changes.append(f"Renamed the app to “{bp['name']}”")
    elif op == "set_module":
        module = raw.get("module")
        if isinstance(module, str):
            module = {"key": module, "label": raw.get("label")}
        elif not isinstance(module, dict):
            module = raw
        key = slug(_first(module, "key", "id", "name", "label", default=""))
        if not key:
            raise SchemaError("give the section a name")
        label = str(_first(module, "label", "name", "title", default=schema.label_of(key))).strip()
        existing = next((m for m in bp["modules"] if m["key"] == key), None)
        if existing:
            if existing["label"] != label:
                existing["label"] = label
                changes.append(f"Renamed the {key} section to {label}")
        else:
            bp["modules"].append({"key": key, "label": label})
            changes.append(f"Added the {label} section to the menu")
    elif op == "set_portal":  # one portal for each kind of outside person: customers', subcontractors'
        spec = _first(raw, "portal", default=None) or {k: v for k, v in raw.items() if k != "op"}
        current = schema.portals(bp)
        if spec.get("enabled") in (False, "false", 0) and not _first(spec, "table", "people", "contacts", "for"):
            for p in current:
                p["enabled"] = False
            if current:
                bp["portals"] = current
                bp.pop("portal", None)
                changes.append("Turned the portal off" if len(current) == 1 else "Turned the portals off")
            return
        portal = schema.normalize_portal(spec)
        at = next((i for i, p in enumerate(current) if p["table"] == portal["table"]), len(current))
        current[at:at + 1] = [portal]
        bp["portals"] = current
        bp.pop("portal", None)
        changes.append(f"Set up the {portal['title']} for {schema.label_of(portal['table'])}")
    elif op == "set_team":  # a team of people steps, moves and approvals can be for (Accounts payable)
        spec = _first(raw, "team", default=None) or {k: v for k, v in raw.items() if k != "op"}
        current = schema.teams(bp)
        if isinstance(spec, dict) and spec.get("remove") in (True, "true", "yes"):
            key = slug(_first(spec, "key", "name", default=""))
            if not any(tm["key"] == key for tm in current):
                raise SchemaError(f"there's no team “{key}”")
            bp["teams"] = [tm for tm in current if tm["key"] != key]
            changes.append(f"Removed the team {key}")
            return
        team = schema.normalize_team(spec)
        at = next((i for i, tm in enumerate(current) if tm["key"] == team["key"]), None)
        if at is None:
            current.append(team)
            changes.append(f"Added the team {team['name']}" + (f" ({team['about']})" if team.get("about") else "")
                           + ": put people in it under Settings › People and teams")
        elif current[at] != team:
            current[at] = team
            changes.append(f"Changed the team {team['name']}")
        bp["teams"] = current
    elif op == "create_table":
        spec = _first(raw, "table_spec", "definition", default=None) or raw
        if isinstance(spec.get("table"), dict):
            spec = {**spec, **spec["table"]}
        t = schema.normalize_table(spec)
        existing = schema.table(bp, t["key"])
        if existing:  # creating a table that exists: add what's missing instead of failing
            added = [f for f in t["fields"] if not schema.field(existing, f["key"])]
            existing["fields"].extend(added)
            if added:
                changes.append(f"Added {len(added)} column{'s' if len(added) != 1 else ''} to {existing['label']}: {', '.join(f['label'] for f in added)}")
            for key in ("access", "row_access", "row_access_also", "rules", "lock_if", "lock_except", "colors", "wizard", "portal", "workflow",
                        "unique_together", "limits", "approve_changes", "sealed", "audit_views", "journey"):
                if t.get(key) and t[key] != existing.get(key):
                    existing[key] = t[key]
                    changes.append(f"Set {existing['label']}'s {key.replace('_', ' ')}")
            result.navigate = result.navigate or f"table:{existing['key']}"
            return
        bp["tables"].append(t)
        kind = "line items" if t.get("embedded") else "table"
        views = list(dict.fromkeys(v["type"] for v in t["views"] if v["type"] != "list"))
        extras = ([f"its {', '.join(views)} view{'s' if len(views) != 1 else ''}"] if views else []) + _policies_said(t) \
            + [f"the .{d['extension']} file {d['label']}, made in its own layout" if d.get("format") == "text" else f"the document {d['label']}"
               for d in t.get("documents", [])]  # so the reply knows they're built too
        changes.append(f"Created the {kind} {t['label']} ({len(t['fields'])} field{'s' if len(t['fields']) != 1 else ''}: {', '.join(f['label'] for f in t['fields'][:8])}{'…' if len(t['fields']) > 8 else ''})"
                       + (f": {'; '.join(extras)}" if extras else ""))
        result.navigate = result.navigate or f"table:{t['key']}"
    elif op == "update_table":
        t = _table(bp, _first(raw, "table", "key"), problems, where)
        if not t:
            return
        updates = _first(raw, "changes", "updates", "set", default=None) or raw
        label = str(_first(updates, "new_label", "new_name", "label", default="")).strip()
        if label and label != t["label"]:
            old, t["label"] = t["label"], label
            t["singular"] = str(_first(updates, "singular", default="")).strip() or schema._singular(label)
            changes.append(f"Renamed the table {old} to {label}")
        singular = str(_first(updates, "singular", default="")).strip()
        if singular and singular != t["singular"]:
            t["singular"] = singular
            changes.append(f"One {t['label']} record is now called a {singular}")
        module = slug(_first(updates, "module", "section", default=""))
        if module and module != t.get("module"):
            t["module"] = module
            changes.append(f"Moved {t['label']} to the {schema.label_of(module)} section")
        icon = schema.icons.resolve(_first(updates, "icon", default=""))
        if icon and icon != t.get("icon"):
            t["icon"] = icon
            changes.append(f"Gave {t['label']} a new icon")
        if "embedded" in updates and bool(updates["embedded"]) != bool(t.get("embedded")):
            t["embedded"] = bool(updates["embedded"])
            if not t["embedded"]:
                t.pop("embedded")
            changes.append(f"{t['label']} {'is now edited inside its parent record' if t.get('embedded') else 'now has its own menu entry'}")
        description = _first(updates, "description")
        if description:
            t["description"] = str(description)[:300]
        for raw_field in _list(_first(updates, "fields", "add_fields", "columns", default=[])):
            f = schema.normalize_field(raw_field)
            if f is None:
                continue
            old = schema.field(t, f["key"])
            if old is None:
                t["fields"].append(f)
                changes.append(f"Added {f['label']} to {t['label']}")
            elif old != f:
                t["fields"][t["fields"].index(old)] = _merge_field(old, raw_field if isinstance(raw_field, dict) else {"label": raw_field})
                changes.append(f"Changed {t['label']} › {f['label']}")
        sections = _first(updates, "sections") or (updates.get("layout") if isinstance(updates.get("layout"), list) else None)
        if sections:
            t["sections"] = [schema.normalize_section(x) for x in _list(sections)]
            changes.append(f"Arranged {t['label']}'s fields into {len(t['sections'])} sections")
        policies = {"access": ("access", "permissions"), "row_access": ("row_access", "own_records", "only_own"),
                    "rules": ("rules", "validations", "constraints", "checks"), "lock_if": ("lock_if", "locked_when", "lock_when"),
                    "lock_except": ("lock_except", "still_editable"), "colors": ("colors", "colours", "color_rules", "highlights"),
                    "workflow": ("workflow", "stages", "status_flow", "transitions", "stage_flow"),
                    "wizard": ("wizard", "steps", "step_by_step"), "portal": ("portal", "portal_access"), "layout": ("layout",),
                    "unique_together": ("unique_together", "unique", "no_duplicates"),
                    "limits": ("limits", "capacity", "no_overlap", "max_per"),
                    "approve_changes": ("approve_changes", "changes_need_approval", "change_approval"),
                    "sealed": ("sealed", "sealed_until"), "audit_views": ("audit_views", "log_views"),
                    "journey": ("journey", "timeline", "progress"), "keep_for": ("keep_for", "retention", "retain_for")}
        given = {key: next(updates[a] for a in names if a in updates) for key, names in policies.items() if any(a in updates for a in names)}
        if given.get("keep_for"):  # counted from a date of its own: read against its fields
            given["keep_for"] = schema.normalize_keep_for(given["keep_for"], t)
        if isinstance(given.get("layout"), list):  # a list of sections, handled above
            given.pop("layout")
        if given:
            locked = {"lock_if": t["lock_if"]} if t.get("lock_if") else {}  # what stays editable can be said on its own
            shaped = schema.normalize_table({"label": t["label"], "fields": [], **locked, **{k: v for k, v in given.items() if v}})
            for key, value in given.items():
                if value and key in shaped:
                    t[key] = shaped[key]
                else:
                    t.pop(key, None)
            if t.get("sealed"):  # every opening of a sealed record is kept in its history, however it was sealed
                t["audit_views"] = True
            if given.get("workflow") and t.get("workflow"):  # a person field of its own named like a role word stays that field
                t["workflow"] = schema.normalize_workflow(given["workflow"], t["label"], {f["key"] for f in t["fields"]})
            if "row_access" in given:  # several rules given: the ones after the first must hold too
                if given["row_access"] and shaped.get("row_access_also"):
                    t["row_access_also"] = shaped["row_access_also"]
                else:
                    t.pop("row_access_also", None)
            if "lock_if" not in t:
                t.pop("lock_except", None)
            label = t["label"]
            words = {"access": f"who can see and change {label}", "row_access": f"who sees which {label.lower()}",
                     "rules": f"the rules {label} must keep", "lock_if": f"when {label} lock",
                     "lock_except": f"what stays editable once {label} lock", "colors": f"the colours of {label}",
                     "wizard": f"the step-by-step form for {label}", "portal": f"what the portal shows of {label}",
                     "layout": f"the layout of {label}", "workflow": f"how {label.lower()} move between stages",
                     "unique_together": f"what no two {label.lower()} may share", "approve_changes": f"which changes to {label.lower()} need approval",
                     "limits": f"how many {label.lower()} may share a day or a time",
                     "sealed": f"when {label.lower()} open", "audit_views": f"who opened {label.lower()} (kept in their history)",
                     "journey": f"the journey shown on {label.lower()}", "keep_for": f"how long {label.lower()} are kept"}
            said = _policies_said(t) if given.keys() & {"access", "row_access", "rules", "lock_if", "lock_except", "workflow", "unique_together",
                                                        "limits", "approve_changes", "sealed"} else []
            changes.append("Set " + "; ".join(words[k] for k in given) + (f" ({'; '.join(said)})" if said else ""))
        title = slug(_first(updates, "title_field", "display_field", default=""))
        if title and title != t.get("title_field"):
            if not schema.field(t, title):
                raise SchemaError(f"{t['label']} has no field “{title}” to use as its title")
            t["title_field"] = title
            changes.append(f"{t['label']} records are now named by {schema.field(t, title)['label']}")
        result.navigate = result.navigate or f"table:{t['key']}"
    elif op == "delete_table":
        t = _table(bp, _first(raw, "table", "key"), problems, where)
        if not t:
            return
        users = [f"{o['label']} › {f['label']}" for o in bp["tables"] if o is not t for f in o["fields"]
                 if f["type"] in ("link", "rollup") and f["table"] == t["key"]]
        if users:
            raise SchemaError(f"{t['label']} is still used by {', '.join(users)}; remove those first")
        bp["tables"].remove(t)
        changes.append(f"Removed the table {t['label']} (its records are kept and come back if you undo)")
    elif op == "add_field":
        t = _table(bp, _first(raw, "table", "table_key"), problems, where)
        if not t:
            return
        raws = _list(_first(raw, "fields", default=None)) or [_first(raw, "field", default={k: v for k, v in raw.items() if k not in ("op", "table")})]
        added = []
        for f in raws:
            new = schema.normalize_field(f)
            if new is None:
                continue
            if schema.field(t, new["key"]):
                problems.append(f"{where}: {t['label']} already has a field “{new['key']}”; use update_field to change it")
                continue
            t["fields"].append(new)
            added.append(new)
            for v in t["views"]:
                if v["type"] == "list" and v["key"] == "all" and "columns" in v and len(v["columns"]) < 8 and new["type"] != "long_text":
                    v["columns"].append(new["key"])
        if added:
            changes.append(f"Added the column{'s' if len(added) > 1 else ''} {_and_words([f'“{f['label']}”' for f in added])} to {t['label']}")
        result.navigate = result.navigate or f"table:{t['key']}"
    elif op == "update_field":
        t = _table(bp, _first(raw, "table", "table_key"), problems, where)
        if not t:
            return
        key = slug(_first(raw, "key", "field_key", default=_first(_first(raw, "field", default={}), "key", default="")) or _first(raw, "field", default=""))
        old = schema.field(t, key)
        if not old:
            raise SchemaError(f"{t['label']} has no field “{key}”")
        updates = _first(raw, "changes", "updates", "set", default=None)
        if updates is None:
            updates = _first(raw, "field", default=None) if isinstance(raw.get("field"), dict) else \
                {k: v for k, v in raw.items() if k not in ("op", "table", "key", "field_key", "field")}
        if "new_label" in updates or "new_name" in updates:
            updates["label"] = updates.pop("new_label", None) or updates.pop("new_name")
        new = _merge_field(old, updates)
        if old["type"] in ("select", "multi_select") and new["type"] == old["type"]:
            # a choice renamed in its place (the options rewritten, one changed): renamed everywhere, as rename_choice does
            from .. import studio
            pairs = studio.renamed_choices({"tables": [{"key": t["key"], "fields": [old]}]}, {"tables": [{"key": t["key"], "fields": [new]}]})
            for _, _, was, now in pairs:
                studio.rename_choice(bp, t["key"], key, was, now)
                changes.append(f"Renamed {new['label']} “{was}” to “{now}” in {t['label']}")
            old = schema.field(t, key)
        t["fields"][t["fields"].index(old)] = new
        what = [k for k in new if new.get(k) != old.get(k)] + [k for k in old if k not in new]
        said = list(dict.fromkeys(SETTING_WORDS.get(k, k.replace("_", " ")) for k in sorted(set(what)) if k not in ("options", "option_colors")
                                  or k == "option_colors" and "options" not in what))
        if "options" in what:  # the choices said as what changed in them: “added Waiting on parts”, not “its choices”
            was, now = list(old.get("options") or []), list(new.get("options") or [])
            added, gone = [o for o in now if o not in was], [o for o in was if o not in now]
            said = [*(f"added “{o}”" for o in added), *(f"took away “{o}”" for o in gone),
                    *(["put its choices in a new order"] if not added and not gone and was != now else []), *said]
        changes.append(f"Changed {t['label']} › {new['label']} ({', '.join(said) or 'no difference'})")
        result.navigate = result.navigate or f"table:{t['key']}"
    elif op == "remove_field":
        t = _table(bp, _first(raw, "table", "table_key"), problems, where)
        if not t:
            return
        key = slug(_first(raw, "key", "field_key", "field", default=""))
        f = schema.field(t, key)
        if not f:
            raise SchemaError(f"{t['label']} has no field “{key}”")
        t["fields"].remove(f)
        for v in t["views"]:
            if "columns" in v:
                v["columns"] = [c for c in v["columns"] if c != key]
        t["views"] = [v for v in t["views"] if v.get("group_by") != key]
        changes.append(f"Removed {f['label']} from {t['label']} (the values are kept and come back if you undo)")
        result.navigate = result.navigate or f"table:{t['key']}"
    elif op == "rename_choice":  # a choice (or a stage) renamed everywhere: its field, stages, rules, filters, flows (records follow on save)
        t = _table(bp, _first(raw, "table", "table_key"), problems, where)
        if not t:
            return
        named = _first(raw, "key", "field", "field_key", default="")
        key = slug(_first(named, "key", "name", default="") if isinstance(named, dict) else named)
        f = schema.field(t, key)
        if not f or f["type"] not in ("select", "multi_select"):
            raise SchemaError(f"{t['label']} has no choice field “{key}”")
        old, new = str(_first(raw, "old", "from", default="")), str(_first(raw, "new", "to", default="")).strip()[:80]
        if old not in f["options"]:
            raise SchemaError(f"{f['label']} has no choice “{old}” (its choices: {', '.join(f['options'])})")
        if not new or (new in f["options"] and new != old):
            raise SchemaError(f"give a new name for “{old}” that {f['label']} hasn't got already")
        if new != old:
            from .. import studio
            studio.rename_choice(bp, t["key"], key, old, new)
            changes.append(f"Renamed {f['label']} “{old}” to “{new}” in {t['label']}")
    elif op == "order_fields":  # the order a record's fields are shown and filled in
        t = _table(bp, _first(raw, "table", "table_key"), problems, where)
        if not t:
            return
        keys = [slug(k) for k in _list(_first(raw, "keys", "fields", "order", default=[]))]
        by_key = {f["key"]: f for f in t["fields"]}
        ordered = [by_key[k] for k in dict.fromkeys(keys) if k in by_key]
        ordered += [f for f in t["fields"] if f not in ordered]  # any not named keep their place at the end
        if ordered != t["fields"]:
            t["fields"] = ordered
            changes.append(f"Put the fields of {t['label']} in a new order")
    elif op in ("add_view", "update_view"):
        t = _table(bp, _first(raw, "table"), problems, where)
        if not t:
            return
        view = schema.normalize_view(_first(raw, "view", default={k: v for k, v in raw.items() if k not in ("op", "table")}))
        existing = next((v for v in t["views"] if v["key"] == view["key"]), None)
        if existing:
            t["views"][t["views"].index(existing)] = {**existing, **view}
            changes.append(f"Changed the view {t['label']} › {view['label']}")
        elif op == "update_view":
            raise SchemaError(f"{t['label']} has no view “{view['key']}”")
        else:
            t["views"].append(view)
            changes.append(f"Added the view {t['label']} › {view['label']}")
        result.navigate = result.navigate or f"table:{t['key']}:{view['key']}"
    elif op == "remove_view":
        t = _table(bp, _first(raw, "table"), problems, where)
        if not t:
            return
        key = slug(_first(raw, "key", "view", default=""))
        v = next((v for v in t["views"] if v["key"] == key), None)
        if not v:
            raise SchemaError(f"{t['label']} has no view “{key}”")
        t["views"].remove(v)
        changes.append(f"Removed the view {t['label']} › {v['label']}")
    elif op in ("add_page", "update_page"):
        page = schema.normalize_page(_first(raw, "page", default={k: v for k, v in raw.items() if k != "op"}))
        existing = next((p for p in bp["pages"] if p["key"] == page["key"]), None)
        if existing:
            bp["pages"][bp["pages"].index(existing)] = page
            changes.append(f"Changed the page {page['label']} ({len(page['widgets'])} widgets)")
        elif op == "update_page":
            raise SchemaError(f"there's no page “{page['key']}”")
        else:
            bp["pages"].append(page)
            changes.append(f"Added the page {page['label']} ({len(page['widgets'])} widgets)")
        result.navigate = result.navigate or f"page:{page['key']}"
    elif op in ("add_flow", "update_flow"):
        flow = schema.normalize_flow(_first(raw, "flow", default={k: v for k, v in raw.items() if k != "op"}))
        current = bp.setdefault("flows", [])
        existing = next((f for f in current if f["key"] == flow["key"]), None)
        if existing:
            current[current.index(existing)] = flow
            changes.append(f"Changed the steps for {flow['label']} ({len(flow['steps'])} steps)")
        elif op == "update_flow":
            raise SchemaError(f"there's no flow “{flow['key']}”")
        else:
            current.append(flow)
            changes.append(f"Added the steps for {flow['label']} ({len(flow['steps'])} steps, one screen each)")
    elif op == "set_space":
        space = schema.normalize_space(_first(raw, "space", default={k: v for k, v in raw.items() if k != "op"}))
        current = bp.setdefault("spaces", [])
        at = next((i for i, x in enumerate(current) if x["key"] == space["key"]), None)
        if at is None:
            current.append(space)
            names = {tm["key"]: tm["name"] for tm in schema.teams(bp)}
            changes.append(f"Gave {_and_words([names.get(t, t) for t in space['for']])} their own menu, “{space['label']}”")
        else:
            current[at] = space
            changes.append(f"Changed the menu “{space['label']}”")
    elif op == "remove_space":
        key = slug(_first(raw, "key", "space", default=""))
        x = next((x for x in bp.get("spaces") or [] if x["key"] == key), None)
        if not x:
            raise SchemaError(f"there's no space “{key}”")
        bp["spaces"].remove(x)
        changes.append(f"Removed the menu “{x['label']}”: they see the whole menu again")
    elif op == "set_sync":
        sync = schema.normalize_sync(_first(raw, "sync", default={k: v for k, v in raw.items() if k != "op"}))
        current = bp.setdefault("syncs", [])
        at = next((i for i, x in enumerate(current) if x["key"] == sync["key"]), None)
        table = schema.table(bp, sync["into"]) or {"label": sync["into"]}
        if at is None:
            current.append(sync)
            changes.append(f"Brings {sync['pull']} in from {sync['app'].title()} into {table['label']}, every {sync['every']} minutes")
        else:
            current[at] = sync
            changes.append(f"Changed how {sync['pull']} come in from {sync['app'].title()}")
    elif op == "remove_sync":
        key = slug(_first(raw, "key", "sync", default=""))
        x = next((x for x in bp.get("syncs") or [] if x["key"] == key), None)
        if not x:
            raise SchemaError(f"there's no sync “{key}”")
        bp["syncs"].remove(x)
        changes.append(f"Stopped bringing {x['pull']} in from {x['app'].title()}")
    elif op == "remove_flow":
        key = slug(_first(raw, "key", "flow", default=""))
        f = next((f for f in bp.get("flows") or [] if f["key"] == key), None)
        if not f:
            raise SchemaError(f"there's no flow “{key}”")
        bp["flows"].remove(f)
        changes.append(f"Removed the steps for {f['label']}")
    elif op == "remove_page":
        key = slug(_first(raw, "key", "page", default=""))
        p = next((p for p in bp["pages"] if p["key"] == key), None)
        if not p:
            raise SchemaError(f"there's no page “{key}”")
        bp["pages"].remove(p)
        changes.append(f"Removed the page {p['label']}")
    elif op in ("add_automation", "update_automation"):
        sent = _first(raw, "automation", default={k: v for k, v in raw.items() if k != "op"})
        if op == "update_automation" and isinstance(sent, dict):
            sent = _kept_steps(bp, sent)
        a = schema.normalize_automation(sent)
        _infer_table(bp, a)
        existing = next((x for x in bp["automations"] if x["key"] == a["key"]), None)
        if existing:
            was = existing["trigger"]
            if was.get("type") in ("record_created", "record_created_or_updated") and a["trigger"].get("type") == "record_updated" \
                    and was.get("table") == a["trigger"].get("table"):  # rewritten for a change, it stopped starting when one is added
                a["trigger"]["type"] = "record_created_or_updated"
                table = schema.table(bp, was.get("table") or "")
                changes.append(f"“{a['name']}” still starts when a new {(table or {}).get('singular', 'record').lower()} is added, as it did")
            bp["automations"][bp["automations"].index(existing)] = a
            changes.append(f"Changed the rule “{a['name']}” ({_automation_said(bp, a)})")
        elif op == "update_automation":
            raise SchemaError(f"there's no automation “{a['key']}”")
        else:
            bp["automations"].append(a)
            changes.append(f"Added the rule “{a['name']}” ({_automation_said(bp, a)})")
        result.navigate = result.navigate or "automations"
    elif op == "remove_automation":
        key = slug(_first(raw, "key", "automation", "name", default=""))
        a = next((x for x in bp["automations"] if x["key"] == key), None)
        if not a:
            raise SchemaError(f"there's no automation “{key}”")
        bp["automations"].remove(a)
        changes.append(f"Removed the rule “{a['name']}”")
    elif op == "add_records":
        t = _table(bp, _first(raw, "table"), problems, where)
        if not t:
            return
        records = [r for r in _list(_first(raw, "records", "rows", "data", "items", default=[])) if isinstance(r, dict)]
        if not records:
            raise SchemaError("add_records needs \"records\": a list of {field: value}")
        unknown = sorted({slug(k) for r in records for k in r} - {f["key"] for f in t["fields"]} - {"id"})
        if unknown:
            raise SchemaError(f"{t['label']} has no field {', '.join(unknown)}")
        result.data.append({"op": "add_records", "table": t["key"], "records": records})
        result.navigate = result.navigate or f"table:{t['key']}"
    elif op == "import_file":
        mapping = _first(raw, "mapping", "columns", "map", default={})
        if not isinstance(mapping, dict):
            raise SchemaError("import_file's \"mapping\" is {\"column heading\": \"field key\"}")
        mapping = {str(k): ".".join(slug(p) for p in str(v).split(".")[:2]) for k, v in mapping.items() if v}
        named = _first(raw, "table")
        if not named:  # the one table with the most of the fields the columns go into ("number", or "orders.number")
            def hits(x):
                return sum(1 for v in mapping.values() if schema.field(x, v) or (v.partition(".")[0] == x["key"] and schema.field(x, v.partition(".")[2])))
            scores = {x["key"]: hits(x) for x in bp["tables"]}
            best = [k for k, n in scores.items() if n and n == max(scores.values())]
            # a tie with its own lines (orders and order_lines): the lines come in under their order
            best = [k for k in best if not any(f["type"] == "link" and f["table"] in best and f["table"] != k
                                               for f in schema.table(bp, k)["fields"])] if len(best) > 1 else best
            named = best[0] if len(best) == 1 else None
        if not named:
            raise SchemaError("import_file needs the \"table\" the rows go into")
        t = _table(bp, named, problems, where)
        if not t:
            return
        mapping = {k: v.partition(".")[2] if v.partition(".")[0] == t["key"] and not schema.field(t, t["key"]) else v
                   for k, v in mapping.items()}  # "orders.number" on Orders is its number
        file_id = schema.expr.to_number(_first(raw, "file_id", "file", "id"))
        if not file_id:
            raise SchemaError("import_file needs the \"file_id\" of an attached file")
        tables = {x["key"]: x for x in bp["tables"]}

        def goes_in(v):  # a field; a linked record's field (customer.email); a line's field (order_lines.quantity)
            head, _, rest = v.partition(".")
            if not rest:
                return schema.field(t, v) is not None
            link = schema.field(t, head)
            if link and link["type"] == "link":
                return schema.field(tables.get(link["table"]), rest) is not None
            child = tables.get(head)
            return bool(child and schema.field(child, rest) and any(f["type"] == "link" and f["table"] == t["key"] for f in child["fields"]))
        skipped = [k for k, v in mapping.items() if not goes_in(v)]  # brought in as far as the tables go
        mapping = {k: v for k, v in mapping.items() if k not in skipped}
        if skipped and not mapping:
            raise SchemaError(f"{t['label']} has no field {', '.join(sorted(set(slug(k) for k in skipped)))} to import into")
        result.data.append({"op": "import_file", "table": t["key"], "file_id": int(file_id), "mapping": mapping,
                            "sheet": _first(raw, "sheet", "tab", "worksheet"), "skipped": skipped})
        result.navigate = result.navigate or f"table:{t['key']}"
    elif op == "find":
        t = _table(bp, _first(raw, "table"), problems, where)
        if not t:
            return
        filters = [schema.normalize_condition(c) for c in _list(_first(raw, "filters", "where", "conditions", default=[]))]
        schema._check_conditions(filters, t, where, problems, {x["key"]: x for x in bp["tables"]})
        view = schema.normalize_view({"type": "list", "sort": _first(raw, "sort", "order_by")}) if _first(raw, "sort", "order_by") else {}
        limit = int(min(max(schema.expr.to_number(_first(raw, "limit", default=20)) or 20, 1), 100))
        result.data.append({"op": "find", "table": t["key"], "filters": filters, "sort": view.get("sort"), "limit": limit,
                            "columns": [slug(c) for c in _list(_first(raw, "columns", "fields", default=[]))]})
