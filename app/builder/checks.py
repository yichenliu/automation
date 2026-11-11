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

from ..blueprint import expr, schema
from ..engine.records import automations_words

CASES = 400  # combinations of values tried for one stage's moves


def run(engine):
    return (choices(engine) + impossible(engine) + unreachable(engine) + gaps(engine) + asked_on_no(engine) + left_to_others(engine)
            + out_of_reach(engine) + needs_rates(engine) + left_unfilled(engine) + made_only_sometimes(engine) + ai_doing_sums(engine)
            + approval_without_stages(engine) + skips_approvals(engine) + runs_while_true(engine) + wrong_kind(engine)
            + nobody_may_answer(engine) + nobody_asked(engine) + never_late(engine) + two_statuses(engine) + restamped(engine)
            + days_for_hours(engine))


STAGE_WORDS = re.compile(r"^(status|stage|state)$", re.I)
ONCE = ("create_record", "create_for_each", "copy_record", "request_approval", "task", "send_email", "send_sms", "webhook")


STAMP = re.compile(r"\b(today|now)\s*\(", re.I)
BARE_STAMP = re.compile(r"\{\{\s*(today|now)\s*\(\s*\)\s*\}\}")


def restamped(engine):
    """A rule run at every save (on a change, none of its conditions saying what changed) that sets a date from today:
    the date moves on each time the record is saved. A due date of today() + 7 comes out a week from the last change,
    never from when the invoice was sent; a "paid on" of today() while it's Paid, the day it was last touched. A plain
    today() with no condition at all is a "last changed" date, and meant."""
    out = []
    for a in engine.bp.get("automations", []):
        trig = a["trigger"]
        t = engine.tables.get(trig.get("table") or "")
        if trig["type"] not in ("record_updated", "record_created_or_updated") or t is None:
            continue
        said, found = list(schema.condition_leaves(a.get("conditions"))), {}
        for act in schema.steps_of(a["actions"]):
            if act.get("type") != "update_record" or act.get("target"):
                continue
            conds = said + list(schema.condition_leaves(act.get("only_if")))
            formulas = list(schema.formulas(a.get("conditions"))) + list(schema.formulas(act.get("only_if")))
            if any(str(c.get("op", "")).startswith("changed") for c in conds):
                continue
            for key, v in (act.get("values") or {}).items():
                if schema.field(t, key) is None or not isinstance(v, str) or not STAMP.search(v):
                    continue
                if any(c.get("field") == key for c in conds) or any(re.search(rf"\b{re.escape(key)}\b", x) for x in formulas):
                    continue  # only while it's empty, or otherwise minded of what it is
                bare = BARE_STAMP.fullmatch(v.strip())
                if not (bare and not conds and not formulas):
                    found.setdefault(key, []).append((v, bool(bare)))
        one = schema.lc(t["singular"])
        for key, sets in found.items():
            f = schema.field(t, key)
            out.append(_issue("check" if all(bare for _, bare in sets) else "fix",
                              f"Rule {a['key']} runs at every save of {_a(one)} (none of its conditions says what changed) and sets "
                              f"{key} to {sets[0][0]}: so {f['label']} moves on each time one is saved, and never stays at when it "
                              f"was set. Set it when the {one} is added (\"on\": \"added\"), when what it dates happens (on "
                              f"\"reaches\" a stage, or a field \"changed_to\" a value), or only while {key} is empty",
                              f"“{a['name']}” moves {f['label']} on every time {_a(one)} is saved", t["key"]))
    return out


HOURS_SAID = re.compile(r"\b(\d+|an?|one|two|three|four|six|twelve|24|48|72)[\s-]+(hours?|hrs?|minutes?|mins?)\b", re.I)
PLAIN_OFFSET = re.compile(r"(\b[a-z_][\w.]*|\bnow\(\s*\))\s*[+-]\s*(\d+(?:\.\d+)?)(?![\w.(])", re.I)


def days_for_hours(engine):
    """A rule its own name says is about hours ("over two hours late") whose formula adds a plain number to a date and
    time: a plain number with a date is days, so returned_at > due_back_at + 2 is two days late, not two hours."""
    out = []
    for a in engine.bp.get("automations", []):
        t = engine.tables.get(a["trigger"].get("table") or "")
        if t is None or not HOURS_SAID.search(a.get("name") or ""):
            continue
        said = list(schema.formulas(a.get("conditions")))
        for act in schema.steps_of(a["actions"]):
            said += list(schema.formulas(act.get("only_if")))
            said += [v for v in (act.get("values") or {}).values() if isinstance(v, str)]
        for text in filter(None, said):
            if any(i["text"].startswith(f"Rule {a['key']} ") for i in out):
                break  # once a rule
            for m in PLAIN_OFFSET.finditer(expr.normalise(text)):
                name = m.group(1)
                f = schema.field(t, name) if "(" not in name else {"type": "datetime"}
                if f is None or f["type"] not in ("datetime", "created_time", "modified_time"):
                    continue
                n = m.group(2)
                out.append(_issue("fix", f"Rule {a['key']} (“{a['name']}”) says hours, but its formula {text.strip('{} ')} adds {n} to "
                                         f"{name}, a date and time, which is {n} days on: for hours write {name} + {n}h "
                                         f"(lengths of time: 2h, 30m, 48 hours, 3 days)",
                                  f"“{a['name']}” counts {n} days where it means {n} hours", t["key"]))
                break
    return out


def two_statuses(engine):
    """Two choice fields of a list with the same choices, one of them its status (a Payment status and a Status, both
    Unpaid, Partially paid, Paid): people don't know which to set, and rules, stages and changes land on different ones."""
    out = []
    for t in engine.tables.values():
        flow = (t.get("workflow") or {}).get("field")
        picks = [f for f in t["fields"] if f["type"] == "select" and len(f.get("options") or []) >= 2]
        for f, g in itertools.combinations(picks, 2):
            same = {o.lower() for o in f["options"]} == {o.lower() for o in g["options"]}
            status = [x for x in (f, g) if x["key"] == flow or STAGE_WORDS.match(x["key"]) or re.search(r"\b(status|stage)\b", x["label"], re.I)]
            if not same or len(status) < 1 or f.get("feature") or g.get("feature"):
                continue
            keep = next((x for x in (f, g) if x["key"] == flow), None) or status[0]
            drop = g if keep is f else f
            out.append(_issue("fix", f"{t['key']} has two fields with the same choices, {f['key']} and {g['key']}: keep {keep['key']} "
                                     f"and remove_field {drop['key']}, pointing whatever used {drop['key']} (rules, features, views, "
                                     f"formulas) at {keep['key']}",
                              f"{t['label']} has both {f['label']} and {g['label']}, with the same choices: people won't know which "
                              f"to set", t["key"]))
    return out


def approval_without_stages(engine):
    """Records a rule asks someone to approve, whose stage is a plain choice anyone can change: the approval can be skipped
    by setting it on (a quote over 10% off set to Sent by hand). Stages with moves hold it: the move waits for the OK."""
    out = []
    for t in engine.tables.values():
        stage = next((f for f in t["fields"] if f["type"] == "select" and (STAGE_WORDS.match(f["key"]) or STAGE_WORDS.match(f["label"]))), None)
        if t.get("workflow") or stage is None:
            continue
        asks = next((a for a in engine.bp.get("automations", []) if a["trigger"].get("table") == t["key"]
                     and any(x.get("type") == "request_approval" for x in schema.steps_of(a["actions"]))), None)
        if asks is None:
            continue
        out.append(_issue("fix", f"{t['key']} records are approved by rule {asks['key']}, but their {stage['key']} is a plain choice "
                                 f"anyone can change, so the approval can be skipped by setting it on: make {stage['key']} the "
                                 f"table's workflow (stages with moves), so the move waits for the OK",
                          f"{schema.a_one(t['singular'], start=True)} can be set on by hand without the approval “{asks['name']}” asks for",
                          t["key"]))
    return out


def runs_while_true(engine):
    """A rule on a change whose conditions only say how the record is (over 10% off, a Draft), not what changed: it runs
    again at every save while that holds (asking again, sending again) and never for a record added that way."""
    out = []
    for a in engine.bp.get("automations", []):
        if a["trigger"]["type"] not in ("record_updated", "record_created_or_updated") or not a.get("conditions"):
            continue
        flat = [c for c in a["conditions"] for c in (c.get("any") or c.get("all") or [c])]
        if any(str(c.get("op", "")).startswith("changed") for c in flat):
            continue
        does = next((x["type"] for x in schema.steps_of(a["actions"]) if x.get("type") in ONCE), None)
        if does is None:
            continue
        t = engine.tables.get(a["trigger"].get("table")) or {"singular": "record"}
        out.append(_issue("check", f"Rule {a['key']} runs at every change while its conditions hold (none says what changed), so it "
                                   f"does its {does} again at each save, and never for one added that way: start it when something "
                                   f"changes to that (\"changed_to\"), or on the move that sends it on",
                          f"“{a['name']}” runs again every time a {schema.lc(t['singular'])} like that is saved", a["trigger"].get("table", "")))
    return out


SUMS = re.compile(r"\b(compare|multipl\w*|subtract\w*|add (it )?up|sum of|on[ _]hand|how many|how much|short(age|fall)?|"
                  r"(less|more|greater|fewer) than|exceeds?)\b", re.I)


def ai_doing_sums(engine):
    """An AI step asked to work something out from the records (a shortage from what's needed and what's on hand): the AI
    can get sums wrong, and the try-out can't check it (the AI isn't asked in a copy). A formula or rollup always can."""
    out = []
    for a in engine.bp.get("automations", []):
        t = engine.tables.get(a["trigger"].get("table"))
        for act in schema.steps_of(a["actions"]):
            f = schema.field(t, act.get("field") or "") if t and act.get("type") == "ai_fill" else None
            said = SUMS.search(str(act.get("instructions") or ""))
            if f is None or not said or f["type"] not in ("number", "money", "percent", "select", "checkbox"):
                continue
            out.append(_issue("check", f"Rule {a['key']} has the AI work out {t['key']}.{f['key']} (“…{said.group(0)}…”): that's "
                                       f"arithmetic on the records, which the AI can get wrong and the try-out can't check. Work it out "
                                       f"with a formula or rollup (a rule can still alert on it)",
                              f"“{a['name']}” has the AI work out {schema.lc(f['label'])}, a sum the app should do itself", t["key"]))
    return out


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
        made = [engine.tables[x["table"]] for x in schema.steps_of(a["actions"])  # what it adds: a line links to its new invoice
                if x.get("type") == "create_record" and x.get("table") in engine.tables]
        for act in schema.steps_of(a["actions"]):
            kind = act.get("type")
            t = engine.tables.get(act.get("table")) if kind in ("create_record", "create_for_each") else None
            # one per line (create_for_each): {{item.…}} is the line, and copying takes from the line first
            item = engine.tables.get((act.get("source") or {}).get("table")) if kind == "create_for_each" else None
            if t is None or (kind == "create_for_each" and (item is None or (act.get("source") or {}).get("field"))):
                continue
            values = act.get("values") or {}
            stage = (t.get("workflow") or {}).get("field")
            near = [x for x in (item, source) if x] + [x for x in made if x is not t]
            for f in t["fields"]:
                if not f.get("required") or f["type"] not in schema.STORED or f["type"] == "autonumber" \
                        or f.get("default") not in (None, "") or f["key"] == stage:
                    continue
                said = values.get(f["key"])
                copies = act.get("copy_matching") and any(schema.field(x, f["key"]) or f.get("table") == x["key"] for x in near)
                if said is None and not copies:  # what copy_matching fills: the same field, or a link back to the record itself
                    # it's added all the same, for people to finish (they're told what to fill in): wrong only when what it
                    # starts from plainly has it (a field of that name, a link to the same list)
                    known = next((g for x in near for g in x["fields"] if g["key"] == f["key"] or g["label"].lower() == f["label"].lower()
                                  or (f["type"] == "link" and g["type"] == "link" and g.get("table") == f.get("table"))), None)
                    if known is not None:
                        out.append(_issue("fix", f"Rule {a['key']} adds {t['key']} records without {f['key']}, which must be filled in, "
                                                 f"though {known['key']} of the record that starts it has it: set \"{f['key']}\" in its "
                                                 f"values from it (people are otherwise asked to fill it in every time)",
                                          f"“{a['name']}” adds {_a(schema.lc(t['singular']))} without its {schema.lc(f['label'])}, "
                                          f"though it could take it from the {schema.lc(known['label'])}", t["key"]))
                    else:
                        out.append(_issue("check", f"Rule {a['key']} adds {t['key']} records without {f['key']}, which must be filled "
                                                   f"in: each is added for people to finish (they're told to fill it in). If what starts "
                                                   f"it knows it, set \"{f['key']}\" in its values" + (" (a person: {{approver}} after an "
                                                   "approval, or a field there that holds one)" if f["type"] == "user" else ""),
                                          f"“{a['name']}” adds {_a(schema.lc(t['singular']))} without its {schema.lc(f['label'])}: "
                                          f"you'll be asked to fill it in each time", t["key"]))
                    continue
                copied = re.fullmatch(r"\{\{\s*(record|item)\.(\w+)\s*\}\}", str(said or ""))
                whose = (item if copied.group(1) == "item" else source) if copied else None
                origin = schema.field(whose, copied.group(2)) if whose else None
                if whose is not None and origin is None and copied.group(2) not in ("id", "title", "created_at", "updated_at") \
                        and schema.backlink(engine.tables, whose, copied.group(2)) is None:
                    out.append(_issue("fix", f"Rule {a['key']} (“{a['name']}”) fills {t['key']}.{f['key']} from {whose['key']}."
                                             f"{copied.group(2)}, which isn't a field of {whose['label']} (its fields: "
                                             f"{', '.join(x['key'] for x in whose['fields'])}), so it's always left empty",
                                      f"“{a['name']}” fills in {_a(schema.lc(t['singular']))}'s {schema.lc(f['label'])} from "
                                      f"something {_a(schema.lc(whose['singular']))} doesn't have, so it's never filled in", t["key"]))
                    continue
                skipped = whose is item and any(c.get("field") == origin["key"] and c.get("op") not in ("is_empty", "not_equals")
                                                for c in act.get("where") or [] if origin is not None)  # only lines that have it
                if origin is not None and not origin.get("required") and origin["type"] in schema.STORED and not skipped and kind == "create_record":
                    # added all the same, for people to finish
                    out.append(_issue("check", f"Rule {a['key']} fills {t['key']}.{f['key']} (which must be filled in) from "
                                               f"{whose['key']}.{origin['key']}, which people may leave empty: then it's added for "
                                               f"people to finish (they're told to fill it in). Make {whose['key']}.{origin['key']} "
                                               f"required if it must always be known",
                                      f"“{a['name']}” adds {_a(schema.lc(t['singular']))} without its {schema.lc(f['label'])} when "
                                      f"{_a(schema.lc(whose['singular']))}'s {schema.lc(origin['label'])} is left empty: you'll be "
                                      f"asked to fill it in then", t["key"]))
                elif origin is not None and not origin.get("required") and origin["type"] in schema.STORED and not skipped:
                    out.append(_issue("check", f"Rule {a['key']} fills {t['key']}.{f['key']} (which must be filled in) from "
                                               f"{whose['key']}.{origin['key']}, which people may leave empty: then each is added "
                                               f"for people to finish (they're told to fill it in)",
                                      f"“{a['name']}” adds {schema.lc(t['label'])} without their {schema.lc(f['label'])} when "
                                      f"{_a(schema.lc(whose['singular']))}'s {schema.lc(origin['label'])} is left empty: you'll be "
                                      f"asked to fill it in then", t["key"]))
    return out


def wrong_kind(engine):
    """A rule that puts one kind of record where another belongs (a pay line's staff member into a payment's customer, a
    list kept for something else): it never finds one, and stops every time."""
    out = []
    for a in engine.bp.get("automations", []):
        source = engine.tables.get(a["trigger"].get("table"))
        for act in schema.steps_of(a["actions"]):
            t = engine.tables.get(act.get("table")) if act.get("type") in ("create_record", "create_for_each") else None
            item = engine.tables.get((act.get("source") or {}).get("table")) if act.get("type") == "create_for_each" else None
            for key, said in (act.get("values") or {}).items() if t else ():
                f = schema.field(t, key)
                said = next(iter(said.values()), None) if isinstance(said, dict) and len(said) == 1 else said  # {"name": …}
                ref = re.fullmatch(r"\{\{\s*(record|item)\.(\w+)(?:\.(\w+))?\s*\}\}", str(said or ""))
                whose = (item if ref.group(1) == "item" else source) if ref and f and f["type"] == "link" else None
                origin = schema.field(whose, ref.group(2)) if whose else None
                if origin is not None and origin["type"] == "link" and ref.group(3) and origin.get("table") in engine.tables:
                    onward = schema.field(engine.tables[origin["table"]], ref.group(3))  # the bid's supplier is a supplier
                    whose, origin = (engine.tables[origin["table"]], onward) if onward and onward["type"] == "link" else (whose, origin)
                if origin is None or origin["type"] != "link" or origin.get("table") in (f["table"], None) \
                        or origin["table"] not in engine.tables or f["table"] not in engine.tables:
                    continue
                theirs, wanted = engine.tables[origin["table"]], engine.tables[f["table"]]
                out.append(_issue("fix", f"Rule {a['key']} puts {whose['key']}.{origin['key']} (a {origin['table']} record) into "
                                         f"{t['key']}.{key}, which links to {f['table']}: it finds none, and stops every time. "
                                         f"Link it to the right list ({t['key']} may be the wrong place for these: add a list of "
                                         f"their own), or fill {key} from a field that links to {f['table']}",
                                  f"“{a['name']}” puts {_a(schema.lc(theirs['singular']))} where {_a(schema.lc(t['singular']))} "
                                  f"needs {_a(schema.lc(wanted['singular']))}", t["key"]))
    return out


def needs_rates(engine):
    """Amounts turned into the business's own currency, with no exchange rates set: every one of them stays empty."""
    if not schema.converts(engine.bp) or engine.rates:
        return []
    return [_issue("check", "convert() needs exchange rates, and none are set: tell the owner to add them in Settings › Business details "
                            "(the builder can't), or the converted amounts stay empty",
                   "Amounts in other currencies stay empty until you add your exchange rates in Settings › Business details")]


def out_of_reach(engine):
    """A team that adds records which must link to records they can't see (a payment's lease, for the bookkeeper who
    can't see leases): the box is empty for them, so they can't add one at all. Or they see only their own of them,
    which may be meant (a manager's own leases) or not (the bookkeeper's)."""
    from ..engine.records import Engine
    out, own_only = [], {}

    def kept_by(t):  # the person fields a list's record rules end at (an organisation's members), as (list, field)
        ends = set()
        for rule in schema.row_rules(t) if t else []:
            path, here = str(rule.get("field") or "").split("."), t
            for part in path[:-1]:
                f = schema.field(here, part)
                here = engine.tables.get(f["table"]) if f and f["type"] == "link" else None
                if here is None:
                    break
            if here is not None:
                ends.add((here["key"], path[-1]))
        return ends
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
                elif them._owner_rule(target) and not (kept_by(target) & kept_by(t)):  # (both kept per organisation: as meant)
                    own_only.setdefault((t["key"], f["key"]), []).append(tm)
    for (tk, fk), teams_ in own_only.items():  # said once for the list, whichever teams it's about
        t, f = engine.tables[tk], schema.field(engine.tables[tk], fk)
        target = engine.tables[f["table"]]
        owner = schema._path_field(target, target["row_access"]["field"], engine.tables) if target["row_access"]["field"] != "created_by" else None
        names = [tm["name"] for tm in teams_]
        who = names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}" if len(names) <= 3 else f"{len(names)} teams"
        out.append(_issue("check", f"Teams {', '.join(tm['key'] for tm in teams_)} can add {tk} records, but see only the {target['key']} records where "
                                   f"{target['row_access']['field']} is them, so that's all {fk} offers them. If they should "
                                   f"pick from all of them, add the teams to {target['key']}'s row_access \"teams\"",
                          f"{who} can add {schema.lc(t['label'])}, but see only the {schema.lc(target['label'])} where "
                          f"they're the {schema.lc((owner or {'label': 'person who added it'})['label'])}", tk))
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
            if all(m.get("feature") for m in others):  # an approval feature's own split: over the amount it waits, as asked
                continue
            when = "; or ".join(automations_words(engine, m["only_if"], t["key"]) for m in anyone)
            theirs = ", ".join(f"“{m.get('label') or m['to']}” ({', '.join(m['who'])})" for m in others)
            out.append(_issue("check", f"At “{stage}”, whoever raised a {t['key']} record can move it on only when {when}; otherwise only "
                                       f"{theirs} can. If those cases should go straight through, the raiser's move should be open "
                                       f"whatever the amount, with a rule's branch setting the stage, not someone else's button",
                              f"Whoever raises a {schema.lc(t['singular'])} can move it on from “{stage}” only when {when}; otherwise "
                              f"it waits for {theirs}", t["key"]))
    return out


def nobody_may_answer(engine):
    """An approval or task kept from the only people it's for: the owner's OK, kept from whoever added it (a quote the
    owner adds would wait for ever: nobody else may answer it), whoever added it kept from their own, a team kept from
    itself."""
    out = []
    for a in engine.bp.get("automations", []):
        t = engine.tables.get(a["trigger"].get("table") or "")
        for act in schema.steps_of(a["actions"]) if t else []:
            if act["type"] not in ("request_approval", "task"):
                continue
            who, apart = act.get("approver") or act.get("to"), [str(x) for x in act.get("not_by") or []]
            if who not in apart and not (who == "owner" and "creator" in apart):
                continue
            plain = schema.a_one(t["singular"], start=True) + (" the owner adds" if who == "owner" else "")
            out.append(_issue("fix", f"A step of the automation {a['key']} asks {who} but keeps it from {', '.join(apart)} (not_by), "
                                     f"so {'a ' + t['key'] + ' record the owner adds' if who == 'owner' else 'every one'} waits for ever: "
                                     f"nobody else may answer it. Leave {who if who != 'owner' else 'creator'} out of not_by, or ask "
                                     f"someone it isn't kept from (a team, or admin)",
                              f"{plain} could never be {'approved' if act['type'] == 'request_approval' else 'done'} by “{a['name']}”: "
                              f"the only {'person' if who in ('owner', 'creator') else 'people'} it's for may not answer it",
                              t["key"]))
    return out


APPROVER = re.compile(r"\{\{\s*approver\b")
NESTED = ("paths", "otherwise", "if_rejected", "steps")


def nobody_asked(engine):
    """{{approver}} (who approved it) in a step that no approval comes before: nobody was asked, so it's always left empty
    (an order approved on its own within its credit limit, "Approved by" set to {{approver}} there)."""
    out = []
    for a in engine.bp.get("automations", []):
        t = engine.tables.get(a["trigger"].get("table") or "")

        def walk(steps, asked, path):
            for s in steps or []:
                if not isinstance(s, dict):
                    continue
                own = {k: v for k, v in s.items() if k not in NESTED}
                if not asked and APPROVER.search(str(own)):
                    keys = [k for k, v in (s.get("values") or {}).items() if APPROVER.search(str(v))]
                    named = [((schema.field(t, k) or {}).get("label") if t and s.get("target") in (None, "", "record") else None) or k
                             for k in keys]
                    on = f" on the path “{path}”" if path else ""
                    out.append(_issue("fix", f"In the automation {a['key']}, a {s.get('type')} step{on} uses {{{{approver}}}}"
                                             f"{' for ' + ', '.join(keys) if keys else ''}, but no request_approval comes before it, so "
                                             f"nobody was asked and it's always left empty: leave it out there (nobody approved it), or "
                                             f"ask for the OK first",
                                      f"In “{a['name']}”{on}, " + (f"{schema.lc(' and '.join(named))} is always left empty"
                                                                    if named else "the approver is never named")
                                      + ": nobody is asked for their OK there", a["trigger"].get("table") or ""))
                maybe = False
                for p in s.get("paths") or []:
                    maybe |= walk(p.get("steps"), asked, p.get("label") or path)
                if isinstance(s.get("otherwise"), list):
                    maybe |= walk(s["otherwise"], asked, "otherwise" if not path else path)
                walk(s.get("if_rejected"), asked, path)  # a no doesn't say who approved
                asked = asked or maybe or s.get("type") == "request_approval" or \
                    (str(s.get("type", "")).startswith("ai_") and s.get("review") == "always")  # who checked the AI's work
            return asked
        walk(a.get("actions"), False, "")
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
            for key, when in (("show_if", "when it shows"), ("hide_if", "when it's hidden"), ("readonly_if", "when it can't be changed")):
                for c in schema.condition_leaves(f.get(key)):
                    yield t, c, f"{t['label']}’s {f['label']} ({when})"
    for a in engine.bp.get("automations", []):
        t = engine.tables.get(a["trigger"].get("table") or "")
        if t is None:
            continue
        for c in schema.condition_leaves(a.get("conditions")):
            yield t, c, f"the rule “{a['name']}”"
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
                                     f"(its choices: {', '.join(f['options'])}): use one of them, add it to the field's options, or, if it means a linked "
                                     f"record's field, write its path (as order.status)",
                              f"{where[:1].upper() + where[1:]} waits for {f['label']} “{wrong}”, which isn't one of its choices, "
                              f"so it would never happen", t["key"]))
    for a in engine.bp.get("automations", []):
        trigger = engine.tables.get(a["trigger"].get("table") or "")
        for act in schema.steps_of(a["actions"]):
            linked = schema.link_path(trigger, act.get("target"), engine.tables) if trigger and act["type"] == "update_record" \
                and act.get("target") != "created" else None
            target = engine.tables.get(act.get("table") or "") if act["type"] in ("create_record", "create_for_each") else \
                trigger if act["type"] == "update_record" and not act.get("target") else \
                engine.tables.get(linked[-1]["table"]) if linked else None  # the linked record it updates
            for key, value in (act.get("values") or {}).items() if target else []:
                f = schema.field(target, key)
                if f is not None and f["type"] == "select":
                    for wrong in _not_a_choice(f, value):
                        worked = " (a value worked out goes inside {{ }})" if re.search(r"\w\(.*\)", wrong) else ""
                        out.append(_issue("fix", f"A step of the automation {a['key']} sets {target['key']}.{key} to “{wrong}”, which isn't "
                                                 f"one of its choices ({', '.join(f['options'])}){worked}: the step would fail every time",
                                          f"The rule “{a['name']}” sets {f['label']} to “{wrong}”, which isn't one of its choices",
                                          target["key"]))
                    copied = re.fullmatch(r"\{\{\s*record\.([\w.]+)\s*\}\}", value.strip()) if isinstance(value, str) and trigger else None
                    source = schema._path_field(trigger, copied.group(1), engine.tables) if copied else None
                    left = [o for o in (source or {}).get("options") or [] if o.strip().lower() not in {x.strip().lower() for x in f.get("options") or []}] \
                        if source is not None and source is not f and source["type"] in ("select", "multi_select") else []
                    if left:  # a return's condition “Good” copied into a costume's, which has Ready instead
                        words = " and ".join(f"“{o}”" for o in left[:4])
                        out.append(_issue("fix", f"A step of the automation {a['key']} copies {trigger['key']}.{copied.group(1)} into "
                                                 f"{target['key']}.{key}, but its choices {words} aren't among {key}'s ({', '.join(f['options'])}): "
                                                 f"the step fails whenever it's one of those. Give both the same choices, or set {key} with a "
                                                 f"branch for each",
                                          f"The rule “{a['name']}” copies {source['label']} into {f['label']}, but {words} "
                                          f"{'isn' if len(left) == 1 else 'aren'}'t one of {f['label']}'s choices, so it stops then",
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


def _stage_runs(actions, key, cap=64):
    """The stages one run of a rule puts its own record at, in order, along each way it can go (one of a branch's paths,
    or none of them)."""
    runs = [[]]
    for act in actions or []:
        if act.get("type") == "branch":
            ways = [w for p in act.get("paths") or [] for w in _stage_runs(p["steps"], key, cap)] + _stage_runs(act.get("otherwise"), key, cap)
            runs = [r + w for r in runs for w in ways][:cap]
        elif act.get("type") == "update_record" and not act.get("target") and isinstance((act.get("values") or {}).get(key), str):
            runs = [r + [act["values"][key]] for r in runs]
    return runs


def skips_approvals(engine):
    """A rule that puts a record at a stage where someone gives the OK and takes it straight on in the same run, so nobody
    ever gets to (a request routed to its team lead, and on to Approved). A rule that asks for the OK first is fine."""
    from ..engine.automations import APPROVES
    out = []
    for a in engine.bp.get("automations", []):
        t = engine.tables.get(a["trigger"].get("table") or "")
        if not t or not t.get("workflow") or any(x.get("type") == "request_approval" for x in schema.steps_of(a["actions"])):
            continue
        key, moves, seen = t["workflow"]["field"], t["workflow"]["transitions"], set()
        # a rule that starts when a record gets to a stage starts there (one firing at “Waiting for team lead” that moves
        # it on is as much past the OK as one that put it there itself)
        at = next((c.get("value") for c in a.get("conditions") or [] if isinstance(c, dict) and c.get("field") == key
                   and c.get("op") in ("changed_to", "equals") and isinstance(c.get("value"), str)), None)
        for run in ([at] + r if at else r for r in _stage_runs(a["actions"], key)):
            for here, there in zip(run, run[1:]):
                ok = next((m for m in moves if here.lower() in [x.lower() for x in m.get("from") or []]
                           and m["to"].lower() == there.lower() and APPROVES.search(str(m.get("label") or ""))), None)
                if ok is None or (here, there) in seen:
                    continue
                seen.add((here, there))
                start = "starts when" if here == at and run[0] == at else "puts"
                out.append(_issue("fix", f"Rule {a['key']} {'starts when ' + t['key'] + ' records get to' if start == 'starts when' else 'puts ' + t['key'] + ' records at'} "
                                         f"“{here}” and takes them straight on to “{there}”, so nobody ever gets to give the OK there "
                                         f"(“{ok.get('label') or ok['to']}”): leave the move on from “{here}” to whoever approves"
                                         + (" (a rule can ask them first with request_approval)" if start == "starts when" else ""),
                                  f"The rule “{a['name']}” takes a {schema.lc(t['singular'])} past “{here}” by itself, so the "
                                  f"approval there (“{ok.get('label') or ok['to']}”) never happens", t["key"]))
    return out


def _set_by_rules(engine, t, key):
    """The stages automations, and flows' buttons, put a table's records at."""
    return schema.stages_set_by_rules(engine.bp, t)


LATE = re.compile(r"\b(overdue|late|expired|lapsed|past due|stale)\b", re.I)


def never_late(engine):
    """A stage that means time ran out (Overdue, Expired) that no rule ever sets, while a scheduled rule acts only on
    records already at it (chase the overdue invoices): someone would have to remember to move each one there by hand,
    so in practice nothing is ever chased."""
    out = []
    for t in engine.tables.values():
        key = (t.get("workflow") or {}).get("field") or next((f["key"] for f in t["fields"] if f["type"] == "select"
                                                              and (STAGE_WORDS.match(f["key"]) or STAGE_WORDS.match(f["label"]))), None)
        f = schema.field(t, key) if key else None
        if f is None:
            continue
        set_by = _set_by_rules(engine, t, key)
        for stage in f.get("options") or []:
            if not LATE.search(str(stage)) or str(stage).strip().lower() in set_by:
                continue
            waiting = next((a for a in engine.bp.get("automations", []) if a["trigger"]["type"] == "schedule"
                            and a["trigger"].get("table") == t["key"]
                            and any(c.get("field") == key and c.get("op") in ("equals", "one_of")
                                    and str(stage).lower() in [str(v).lower() for v in (c["value"] if isinstance(c.get("value"), list) else [c.get("value")])]
                                    for c in schema.condition_leaves(a.get("conditions")))), None)
            if waiting is None:
                continue
            due = next((x for x in t["fields"] if x["type"] in ("date", "datetime") and re.search(r"\b(due|expir|end|until)", f"{x['key']} {x['label']}", re.I)), None)
            out.append(_issue("fix", f"Nothing moves {_a(schema.lc(t['singular']))} to “{stage}”: rule {waiting['key']} only acts on ones already "
                                     f"“{stage}”, and no rule sets it, so someone must remember to move each by hand. Add a daily rule "
                                     f"on {t['key']} (schedule) that sets {key} to “{stage}” when "
                                     + (f"{due['key']} is before_today" if due else "its date has passed")
                                     + " and it isn't finished yet (its earlier stages)",
                              f"no {schema.lc(t['singular'])} ever becomes “{stage}” by itself, so “{waiting['name']}” never has any to act on",
                              t["key"]))
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
                                      f"{schema.a_one(t['singular'], start=True)} at “{stage}” with {words} would be stuck: none of its next steps "
                                      f"allow it", t["key"]))
                    break
    return out


def _shown(engine, f, value, plain=False):
    if value in (None, "", []):
        return "left empty" if plain else "empty"
    if plain:
        return f"at {engine.display(f, value)}"
    return f"= {value!r}"
