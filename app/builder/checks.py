"""Mistakes a design can have whatever the business: found by reading it, not by asking the AI.

- a condition or a step naming a choice its field doesn't have ("Complete" when the choices are Draft, Done): the rule
  never fires, or the step fails every time;
- a stage nothing can ever get a record to (no move leads there and no rule sets it);
- a stage left by two or more moves whose conditions miss a case: a quote with exactly 10% off, or none, when one move
  is for "more than 10%" and the other for "less than 10%", can't move on at all.

Each is an issue as trial.py describes it ({"level", "text" for the AI, "plain" for people, "where"}).
"""
import itertools
import re
from types import SimpleNamespace

from ..blueprint import schema
from ..engine.records import automations_words

CASES = 400  # combinations of values tried for one stage's moves


def run(engine):
    return (choices(engine) + impossible(engine) + unreachable(engine) + gaps(engine) + asked_on_no(engine) + left_to_others(engine)
            + out_of_reach(engine) + needs_rates(engine) + left_unfilled(engine) + made_only_sometimes(engine))


MAKES = ("create_record", "create_for_each", "copy_record")


def made_only_sometimes(engine):
    """A rule that makes a record only on some of a branch's paths, with nothing for the rest (the invoice for a finished
    job made only when the job has a quote): the others get none, though "when a job is done, draft its invoice" meant
    every one. What's only sometimes there (the quote's lines) belongs in a branch after the making, not around it."""
    out = []
    for a in engine.bp.get("automations", []):
        source = engine.tables.get(a["trigger"].get("table"))
        for act in schema.steps_of(a["actions"]):
            if act.get("type") != "branch" or act.get("otherwise") or not act.get("paths"):
                continue
            made = [{s.get("table") for s in schema.steps_of(p["steps"]) if s.get("type") in MAKES} for p in act["paths"]]
            for key in sorted(set().union(*made)):  # what no otherwise makes: nothing
                t = engine.tables.get(key)
                if t is None or t.get("embedded"):
                    continue
                when = "; or ".join(automations_words(engine, p["when"], source["key"]) for p, m in zip(act["paths"], made) if key in m) if source else ""
                thing = schema.lc(source["singular"]) if source else "record"
                out.append(_issue("check", f"Rule {a['key']} makes a {key} record only when {when}; any other {thing} gets none, and the "
                                           f"branch has no otherwise. If every one should get it, make it before the branch and keep "
                                           f"only what depends on that (lines, amounts) on the path",
                                  f"“{a['name']}” makes {_a(schema.lc(t['singular']))} only when {when}: any other "
                                  f"{thing} gets none", key))
    return out


def _a(word):
    return f"{'an' if word[:1] in 'aeiou' else 'a'} {word}"


def left_unfilled(engine):
    """A rule that adds a record without something it must have (a child added on enrolment with no last name): it
    stops every time. Or it copies the must-have from something people may leave empty: it stops whenever they do."""
    out = []
    for a in engine.bp.get("automations", []):
        source = engine.tables.get(a["trigger"].get("table"))
        for act in schema.steps_of(a["actions"]):
            t = engine.tables.get(act.get("table")) if act.get("type") == "create_record" else None
            if t is None or act.get("each") or act.get("from"):  # one per line of another record: its values come from there
                continue
            values = act.get("values") or {}
            stage = (t.get("workflow") or {}).get("field")
            for f in t["fields"]:
                if not f.get("required") or f["type"] not in schema.STORED or f["type"] in ("autonumber", "user") \
                        or f.get("default") not in (None, "") or f["key"] == stage:
                    continue
                said = values.get(f["key"])
                copies = act.get("copy_matching") and source and (schema.field(source, f["key"]) or f.get("table") == source["key"])
                if said is None and not copies:  # what copy_matching fills: the same field, or a link back to the record itself
                    out.append(_issue("fix", f"Rule {a['key']} adds {t['key']} records without {f['key']}, which must be filled in, "
                                             f"so it stops every time: set \"{f['key']}\" in its values (from the record that "
                                             f"starts it, or a fixed value), or make {f['key']} optional",
                                      f"“{a['name']}” can't add a {schema.lc(t['singular'])}: it doesn't fill in "
                                      f"{schema.lc(f['label'])}, which must be filled in", t["key"]))
                    continue
                copied = re.fullmatch(r"\{\{\s*record\.(\w+)\s*\}\}", str(said or ""))
                origin = schema.field(source, copied.group(1)) if copied and source else None
                if origin is not None and not origin.get("required") and origin["type"] in schema.STORED:
                    out.append(_issue("check", f"Rule {a['key']} fills {t['key']}.{f['key']} (which must be filled in) from "
                                               f"{source['key']}.{origin['key']}, which people may leave empty: then the rule stops. "
                                               f"Make {source['key']}.{origin['key']} required, or a move that starts the rule require it",
                                      f"“{a['name']}” stops when a {schema.lc(source['singular'])}'s {schema.lc(origin['label'])} "
                                      f"is left empty", t["key"]))
    return out


def needs_rates(engine):
    """Amounts turned into the business's own currency, with no exchange rates set: every one of them stays empty."""
    if not schema.converts(engine.bp) or engine.rates:
        return []
    return [_issue("check", "convert() needs exchange rates, and none are set: tell the owner to add them in Settings › General "
                            "(the builder can't), or the converted amounts stay empty",
                   "Amounts in other currencies stay empty until you add your exchange rates in Settings › General")]


def out_of_reach(engine):
    """A team that adds records which must link to records they can't see (a payment's lease, for the bookkeeper who
    can't see leases): the box is empty for them, so they can't add one at all. Or they see only their own of them,
    which may be meant (a manager's own leases) or not (the bookkeeper's)."""
    from ..engine.records import Engine
    out = []
    for tm in schema.teams(engine.bp):
        them = Engine(engine.ws, member=SimpleNamespace(role="editor", user_id=-1, teams=[tm["key"]]))
        for t in engine.tables.values():
            if t.get("embedded") or not them.can(t, "edit"):
                continue
            for f in t["fields"]:
                target = engine.tables.get(f.get("table")) if f["type"] == "link" and f.get("required") else None
                if target is None:
                    continue
                if not them.can(target):
                    out.append(_issue("fix", f"Team {tm['key']} can add {t['key']} records, whose {f['key']} must link to {target['key']}, which "
                                             f"they can't see: the box is empty for them. Let them see {target['key']} (give them view access, "
                                             f"keeping edit to others) or don't let them add {t['key']}",
                                      f"{tm['name']} can add {schema.lc(t['label'])}, but can't see any {schema.lc(target['label'])}, so "
                                      f"“{f['label']}” is empty for them", t["key"]))
                elif them._owner_rule(target):
                    owner = schema._path_field(target, target["row_access"]["field"], engine.tables) if target["row_access"]["field"] != "created_by" else None
                    out.append(_issue("check", f"Team {tm['key']} can add {t['key']} records, but sees only the {target['key']} records where "
                                               f"{target['row_access']['field']} is them, so that's all {f['key']} offers them. If they should "
                                               f"pick from all of them, add the team to {target['key']}'s row_access \"teams\"",
                                      f"{tm['name']} can add {schema.lc(t['label'])}, but see only the {schema.lc(target['label'])} where "
                                      f"they're the {schema.lc((owner or {'label': 'person who added it'})['label'])}", t["key"]))
    return out


def left_to_others(engine):
    """A stage whose way on, for whoever raised the record, is only under a condition (Send for approval over $5,000),
    with the other cases left to someone else's button (the owner's Approve under it): the person who raised a small
    one has nothing to press, though "under the limit goes straight through" was meant."""
    out = []
    for t in engine.tables.values():
        flow = t.get("workflow")
        for stage in schema.stages(t) if flow else []:
            onward = [m for m in flow["transitions"] if (not m["from"] or stage in m["from"]) and m["to"] != stage and not schema.says_no(m["to"])]
            anyone = [m for m in onward if not m.get("who") or {"editor", "creator"} & set(m["who"])]
            others = [m for m in onward if m not in anyone and m.get("only_if")]
            if not anyone or not others or not all(m.get("only_if") for m in anyone):
                continue
            when = "; or ".join(automations_words(engine, m["only_if"], t["key"]) for m in anyone)
            theirs = ", ".join(f"“{m.get('label') or m['to']}” ({', '.join(m['who'])})" for m in others)
            out.append(_issue("check", f"At “{stage}”, whoever raised a {t['key']} record can move it on only when {when}; otherwise only "
                                       f"{theirs} can. If those cases should go straight through, the raiser's move should be open "
                                       f"whatever the amount, with a rule's branch setting the stage, not someone else's button",
                              f"Whoever raises a {schema.lc(t['singular'])} can move it on from “{stage}” only when {when}; otherwise "
                              f"it waits for {theirs}", t["key"]))
    return out


def asked_on_no(engine):
    """An approval asked only when another approver says no (inside their "if_rejected"): approvals in turn belong in
    the main steps."""
    out = []
    for a in engine.bp.get("automations", []):
        for act in schema.steps_of(a["actions"]):
            if act["type"] == "request_approval" and any(x["type"] == "request_approval" for x in act.get("if_rejected") or []):
                out.append(_issue("fix", f"In the automation {a['key']}, an approval is inside another approval's if_rejected steps, so it's "
                                         f"only asked when the first approver says no. Approvals one after another go in the main steps, "
                                         f"in turn (each with only_if for the amounts it's for)",
                                  f"In “{a['name']}”, a second approval is only asked when the first person says no",
                                  a["trigger"].get("table") or ""))
    return out


def _issue(level, text, plain, where=""):
    return {"level": level, "text": text, "plain": plain, "where": where}


def _conditions(engine):
    """Every plain condition of the design with the table it's checked on and where it is: (table, condition, where)."""
    for t in engine.tables.values():
        for m in (t.get("workflow") or {}).get("transitions", []):
            for c in schema.condition_leaves(m.get("only_if")):
                yield t, c, f"the move “{m.get('label') or m['to']}” on {t['label']}"
        for colour in t.get("colors") or []:
            for c in schema.condition_leaves(colour.get("when")):
                yield t, c, f"a colour on {t['label']}"
        for v in t.get("views") or []:
            for c in schema.condition_leaves(v.get("filters")):
                yield t, c, f"the view “{v.get('label') or v['key']}” of {t['label']}"
        for f in t["fields"]:
            for key in ("show_if", "hide_if", "readonly_if"):
                for c in schema.condition_leaves(f.get(key)):
                    yield t, c, f"{t['label']}’s {f['label']} ({key})"
    for a in engine.bp.get("automations", []):
        t = engine.tables.get(a["trigger"].get("table") or "")
        if t is None:
            continue
        for c in schema.condition_leaves(a.get("conditions")):
            yield t, c, f"the automation “{a['name']}”"
        for act in schema.steps_of(a["actions"]):
            for c in schema.condition_leaves(act.get("only_if")):
                yield t, c, f"a step of “{a['name']}”"
            for path in act.get("paths") or []:
                for c in schema.condition_leaves(path.get("when")):
                    yield t, c, f"a way of “{a['name']}”"
    for p in engine.bp.get("pages", []):
        for w in p.get("widgets", []):
            t = engine.tables.get(w.get("table") or "")
            for c in schema.condition_leaves(w.get("filters")) if t else []:
                yield t, c, f"“{w.get('label') or w['type']}” on the page {p['label']}"


def _field_at(engine, t, path):
    for part in path.split(".")[:-1]:
        link = schema.field(t, part)
        t = engine.tables.get(link["table"]) if link and link["type"] == "link" else None
        if t is None:
            return None, None
    return t, schema.field(t, path.split(".")[-1])


def _not_a_choice(f, value):
    names = [o.lower() for o in f.get("options") or []]
    values = value if isinstance(value, list) else [value]
    return [v for v in values if isinstance(v, str) and "{{" not in v and v.strip() and v.strip().lower() not in names]


def choices(engine):
    out = []
    for t, c, where in _conditions(engine):
        owner, f = _field_at(engine, t, c["field"])
        if f is None or f["type"] not in ("select", "multi_select") or c["op"] not in ("equals", "not_equals", "changed_to", "one_of"):
            continue
        for wrong in _not_a_choice(f, c.get("value")):
            out.append(_issue("fix", f"{where} checks {c['field']} {c['op']} “{wrong}”, but {owner['key']}.{f['key']} has no such choice "
                                     f"(its choices: {', '.join(f['options'])}): use one of them, or add it to the field's options",
                              f"{where[:1].upper() + where[1:]} waits for {f['label']} “{wrong}”, which isn't one of its choices, "
                              f"so it would never happen", t["key"]))
    for a in engine.bp.get("automations", []):
        trigger = engine.tables.get(a["trigger"].get("table") or "")
        for act in schema.steps_of(a["actions"]):
            linked = schema.field(trigger, act.get("target") or "") if trigger and act["type"] == "update_record" else None
            target = engine.tables.get(act.get("table") or "") if act["type"] in ("create_record", "create_for_each") else \
                trigger if act["type"] == "update_record" and not act.get("target") else \
                engine.tables.get(linked["table"]) if linked and linked["type"] == "link" else None  # the linked record it updates
            for key, value in (act.get("values") or {}).items() if target else []:
                f = schema.field(target, key)
                if f is not None and f["type"] == "select":
                    for wrong in _not_a_choice(f, value):
                        worked = " (a value worked out goes inside {{ }})" if re.search(r"\w\(.*\)", wrong) else ""
                        out.append(_issue("fix", f"A step of the automation {a['key']} sets {target['key']}.{key} to “{wrong}”, which isn't "
                                                 f"one of its choices ({', '.join(f['options'])}){worked}: the step would fail every time",
                                          f"The rule “{a['name']}” sets {f['label']} to “{wrong}”, which isn't one of its choices",
                                          target["key"]))
    return out


def _never(conditions):
    """Two conditions of one list that can't both hold: a field equal to two different things (Fail and Needs rework)."""
    wanted = {}
    for c in conditions or []:
        if "any" in c:
            continue
        if "all" in c:
            found = _never(c["all"])
            if found:
                return found
            continue
        if c.get("op") in ("equals", "changed_to") and isinstance(c.get("value"), (str, int, float)) and "{{" not in str(c["value"]):
            before = wanted.setdefault(c["field"], c["value"])
            if str(before).strip().lower() != str(c["value"]).strip().lower():
                return c["field"], before, c["value"]
    return None


def impossible(engine):
    out = []
    lists = [(a["trigger"].get("table"), a.get("conditions"), f"the automation “{a['name']}”", a["key"]) for a in engine.bp.get("automations", [])]
    lists += [(a["trigger"].get("table"), act.get("only_if"), f"a step of “{a['name']}”", a["key"])
              for a in engine.bp.get("automations", []) for act in schema.steps_of(a["actions"])]
    lists += [(t["key"], m.get("only_if"), f"the move “{m.get('label') or m['to']}” on {t['label']}", t["key"])
              for t in engine.tables.values() for m in (t.get("workflow") or {}).get("transitions", [])]
    for table, conditions, where, key in lists:
        found = _never(conditions)
        if found:
            field, first, second = found
            out.append(_issue("fix", f"{where} ({key}) needs {field} to equal both “{first}” and “{second}”, which can never be: put "
                                     f"them in an \"any\" group (either one) or keep the one meant",
                              f"{where[:1].upper() + where[1:]} waits for {field.replace('_', ' ')} to be both “{first}” and “{second}” at "
                              f"once, so it would never happen", table or ""))
    return out


def _set_by_rules(engine, t, key):
    """The stages automations put a table's records at."""
    out = set()
    for a in engine.bp.get("automations", []):
        for act in schema.steps_of(a["actions"]):
            mine = (act["type"] == "update_record" and a["trigger"].get("table") == t["key"] and not act.get("target")) or \
                (act["type"] in ("create_record", "create_for_each", "update_each") and act.get("table") == t["key"]) or \
                (act["type"] == "update_record" and act.get("target"))  # a linked record's stage: counted, to be safe
            value = (act.get("values") or {}).get(key)
            if mine and isinstance(value, str):
                out.add(value.strip().lower())
    return out


def unreachable(engine):
    out = []
    for t in engine.tables.values():
        flow, names = t.get("workflow"), schema.stages(t)
        if not flow or not names:
            continue
        reached = {(schema.start_stage(t) or names[0]).lower()} | _set_by_rules(engine, t, flow["field"])
        grew = True
        while grew:
            grew = False
            for m in flow["transitions"]:
                if (not m["from"] or {s.lower() for s in m["from"]} & reached) and m["to"].lower() not in reached:
                    reached.add(m["to"].lower())
                    grew = True
        for stage in names:
            if stage.lower() not in reached:
                out.append(_issue("fix", f"Nothing can ever get a record of {t['key']} to the stage “{stage}”: no move of its workflow "
                                         f"leads there from a stage it can reach, and no automation sets it. Add a move to it (or remove "
                                         f"the stage if it isn't needed)",
                                  f"No {schema.lc(t['singular'])} can ever get to “{stage}”: there's no button that leads there", t["key"]))
    return out


def _tries(engine, f, values):
    """Values worth trying for a field a stage's moves decide on: empty, each value they compare with and either side of it."""
    kind = engine.kind(f)
    if kind in ("number", "money", "percent", "duration", "rating"):
        out = [None, 0]
        for v in values:
            n = schema.as_fraction(v) if kind == "percent" else _number(v)
            if n is not None:
                step = 0.001 if kind == "percent" else 0.01
                out += [n - step, n, n + step]
        return list(dict.fromkeys(out + [max([x for x in out if x is not None] or [0]) * 10 + 1]))
    if kind == "select":
        return [None] + list(f.get("options") or [])
    if kind == "checkbox":
        return [True, False]
    if kind == "multi_select":
        return [[]] + [[o] for o in f.get("options") or []]
    return [None, "something"]


def _number(v):
    try:
        return float(str(v).replace("%", "").replace("$", "").replace(",", ""))
    except (TypeError, ValueError):
        return None


def gaps(engine):
    """A stage whose way out is decided by conditions (two or more moves on the same fields, none open whatever) with a
    case none of them covers."""
    out = []
    for t in engine.tables.values():
        flow = t.get("workflow")
        if not flow or not schema.stages(t):
            continue
        for stage in schema.stages(t):
            moves = [m for m in flow["transitions"] if (not m["from"] or stage in m["from"]) and m["to"] != stage]
            if len(moves) < 2 or not all(m.get("only_if") for m in moves):
                continue
            leaves = [c for m in moves for c in schema.condition_leaves(m["only_if"])]
            fields = {c["field"] for c in leaves}
            if any("." in k or schema.field(t, k) is None or schema.field(t, k)["type"] not in schema.STORED for k in fields):
                continue  # through links, or worked out: not something to try by hand
            shared = set.intersection(*({c["field"] for c in schema.condition_leaves(m["only_if"])} for m in moves))
            if len(fields) > 3 or not shared:
                continue  # moves on different things aren't a decision between them
            keys = sorted(fields)
            options = [_tries(engine, schema.field(t, k), [c.get("value") for c in leaves if c["field"] == k]) for k in keys]
            for combo in itertools.islice(itertools.product(*options), CASES):
                record = SimpleNamespace(id=None, table=t["key"], data={**dict(zip(keys, combo)), flow["field"]: stage},
                                         created_by_id=None, created_at=None, updated_at=None)
                try:
                    stuck = not any(engine.matches(record, m["only_if"]) for m in moves)
                except Exception:  # noqa: BLE001 - a condition this can't try by hand
                    stuck = False
                if stuck:
                    case = ", ".join(f"{k} {_shown(engine, schema.field(t, k), v)}" for k, v in zip(keys, combo))
                    words = ", ".join(f"{schema.field(t, k)['label']} {_shown(engine, schema.field(t, k), v, plain=True)}" for k, v in zip(keys, combo))
                    rules = "; ".join(f"“{m.get('label') or m['to']}” needs {m['only_if']}" for m in moves)
                    out.append(_issue("fix", f"A {t['key']} record at “{stage}” with {case} has no move it can take ({rules}). Make the "
                                             f"conditions cover every case (at_least/at_most rather than greater_than/less_than on both "
                                             f"sides; give a number a default so empty counts as it)",
                                      f"A {schema.lc(t['singular'])} at “{stage}” with {words} would be stuck: none of its next steps "
                                      f"allow it", t["key"]))
                    break
    return out


def _shown(engine, f, value, plain=False):
    if value in (None, "", []):
        return "left empty" if plain else "empty"
    if plain:
        return f"at {engine.display(f, value)}"
    return f"= {value!r}"
