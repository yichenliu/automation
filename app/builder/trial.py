"""Trying an app out before it's handed over, the way its people would use it.

`copy_of` makes a throwaway copy of the app: its design, settings and people, none of its records, and nothing that
reaches outside (no mailbox is connected, the AI isn't asked, web calls aren't made). In a copy, `walk` adds a record to
every list, takes each along its stages, presses every button, runs every scheduled rule, makes every document and opens
every screen, noting what went wrong; `play` acts out one of the builder's worked examples ("a quote for 80 guests at
$25 with 15% off") and compares what the app then shows with what the owner would expect. The real app is never touched.

What's found is an issue: {"level": "fix" (it's wrong) or "check" (it may be meant), "text": for the AI, with keys,
"plain": for people}. A fault of the platform's own (a screen that crashes) is a crash: logged, never the AI's to fix.
"""
import copy
import json
import re
import shutil
import tempfile
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

from flask import current_app

from .. import database
from ..blueprint import expr, schema
from ..engine import automations
from ..engine.records import Engine, FieldErrors, NoRate, RecordContext, mask, masked, to_number
from ..extensions import db
from ..models import Approval, Membership, Record, Run, User, Workspace

STAND_IN = "Alex Stand-in"  # someone other than whoever raised a thing, for the approvals kept apart from them
FIELD_TRIES = 3  # other values tried for a field a sample was refused over


# --- a throwaway copy -----------------------------------------------------------------------------------------------

class Trial:
    def __init__(self, app, ws_id, owner_id):
        self.app, self.ws_id, self.owner_id = app, ws_id, owner_id

    @property
    def ws(self):
        return db.session.get(Workspace, self.ws_id)

    def member(self, user_id=None):
        return Membership.query.filter_by(workspace_id=self.ws_id, user_id=user_id or self.owner_id).first()

    def engine(self, user_id=None):
        user = db.session.get(User, user_id or self.owner_id)
        engine = Engine(self.ws, by=user.name if user else "someone", member=self.member(user_id))
        engine.stepped_up = True  # whoever's trying it has just confirmed it's them
        return engine

    def client(self, user_id=None):
        """Signed in as the owner (or someone else in the copy) without the sign-in form (whose limits count real
        people's attempts)."""
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["_user_id"], session["_fresh"] = str(user_id or self.owner_id), True
        return client


@contextmanager
def copy_of(workspace):
    """A throwaway copy of the app (see the module's notes), gone when the block ends."""
    from .. import create_app
    people = [(m.user_id, m.role, list(m.teams or []), u.name, u.email) for m, u in
              db.session.query(Membership, User).filter(Membership.workspace_id == workspace.id, Membership.user_id == User.id,
                                                        Membership.role != "portal")]
    named = _looked_up_by_name(workspace.blueprint or {})
    kept = [(r.id, r.table, copy.deepcopy(r.data)) for r in Record.query.filter(Record.workspace_id == workspace.id, Record.deleted_at.is_(None),
                                                                                  Record.table.in_(named)).limit(2000)] if named else []
    design = {"name": workspace.name, "about": workspace.about, "blueprint": copy.deepcopy(workspace.blueprint or schema.empty(workspace.name)),
              "settings": copy.deepcopy(workspace.settings or {}), "timezone": workspace.timezone, "currency": workspace.currency,
              "version": workspace.version}
    owner_id = workspace.owner_id
    root = Path(current_app.config["DATA_DIR"]) / "trials"
    root.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(dir=root))
    config = {k: v for k, v in current_app.config.items() if k.isupper()}
    config.update({"TESTING": True, "WORKER": False, "TRYING_OUT": True, "DATA_DIR": folder, "SECRET_KEY": "try-out",
                   "SQLALCHEMY_BINDS": {}, "WTF_CSRF_ENABLED": False, "SQLALCHEMY_ENGINE_OPTIONS": {"pool_size": 2, "max_overflow": 4},
                   "MICROSOFT_CLIENT_ID": "", "MICROSOFT_CLIENT_SECRET": "", "OLLAMA_EMBED_MODEL": "", "SERVER_NAME": None})
    try:
        place = database.throwaway(current_app.config["SQLALCHEMY_DATABASE_URI"], "tryout")  # an empty database of its own
        config["SQLALCHEMY_DATABASE_URI"] = place.__enter__()
        app = create_app(config)
        with app.app_context():
            if not any(p[0] == owner_id for p in people):
                people.insert(0, (owner_id, "owner", [], "Owner", "owner@example.com"))
            teams = [tm["key"] for tm in schema.teams(design["blueprint"])]
            stand_in = max(p[0] for p in people) + 1
            for user_id, role, member_teams, name, email in people + [(stand_in, "admin", teams, STAND_IN, "stand-in@example.com")]:
                user = User(id=user_id, email=email, name=name)
                user.password_hash = "!"  # nobody signs in as them
                db.session.add(user)
            db.session.flush()
            ws = Workspace(owner_id=owner_id, state={}, **design)
            db.session.add(ws)
            db.session.flush()
            for user_id, role, member_teams, *_ in people + [(stand_in, "admin", teams)]:
                db.session.add(Membership(workspace_id=ws.id, user_id=user_id, role=role, teams=member_teams or None))
            for record_id, table, data in kept:  # what rules find by name (the "Wages" account) is there as it is in the app
                db.session.add(Record(id=record_id, workspace_id=ws.id, table=table, data=data))
            db.session.flush()
            database.fix_sequences(db.session)  # people and records came in with their ids: new ones carry on after them
            db.session.commit()
            try:
                yield Trial(app, ws.id, owner_id)
            finally:
                db.session.remove()
                db.engine.dispose()  # nothing stays open on a copy that's about to be thrown away
    finally:
        if "place" in locals():
            place.__exit__(None, None, None)
        shutil.rmtree(folder, ignore_errors=True)


def _looked_up_by_name(bp):
    """The tables whose records automations name in their steps (an account "Wages" a journal line goes to)."""
    out = set()
    for a in bp.get("automations", []):
        for act in schema.steps_of(a["actions"]):
            t = schema.table(bp, act.get("table") or a["trigger"].get("table") or "")
            for key, value in (act.get("values") or {}).items() if t else []:
                f = schema.field(t, key)
                if f is not None and f["type"] == "link" and isinstance(value, str) and "{{" not in value:
                    out.add(f["table"])
    return out


# --- sample values ----------------------------------------------------------------------------------------------------

def matching(pattern):
    """A value that fits a field's pattern (an 11-digit ABN, a SKU like AB-1234), walked from the parsed expression."""
    from re import _constants as c, _parser as parser

    def walk(items):
        out = ""
        for op, arg in items:
            if op == c.LITERAL:
                out += chr(arg)
            elif op == c.NOT_LITERAL:
                out += "a" if arg != ord("a") else "b"
            elif op == c.ANY:
                out += "a"
            elif op == c.IN:
                out += first(arg)
            elif op in (c.MAX_REPEAT, c.MIN_REPEAT, getattr(c, "POSSESSIVE_REPEAT", None)):
                low, _, inner = arg
                out += walk(inner) * low
            elif op == c.SUBPATTERN:
                out += walk(arg[-1])
            elif op == c.BRANCH:
                out += walk(arg[1][0])
            elif op == c.CATEGORY:
                out += first([(op, arg)])
        return out

    def first(options):
        if options and options[0][0] == c.NEGATE:
            return "a"
        for op, arg in options:
            if op == c.LITERAL:
                return chr(arg)
            if op == c.RANGE:
                return chr(arg[0])
            if op == c.CATEGORY:
                return {c.CATEGORY_DIGIT: "1", c.CATEGORY_SPACE: " ", c.CATEGORY_WORD: "a"}.get(arg, "a")
        return "a"

    try:
        value = walk(parser.parse(pattern))
        return value if re.fullmatch(pattern, value) else None
    except Exception:  # noqa: BLE001 - a pattern too odd to walk: the field is left empty
        return None


def sample_value(engine, t, f, owner_id, made, n, nth_date=0):
    """An ordinary value for a field (dates a day apart in the order they come, so a finish is after its start)."""
    kind = f["type"]
    if f.get("pattern"):
        return matching(f["pattern"])
    if kind == "link":
        linked = made.get(f["table"])
        return ([linked] if f.get("multiple") else linked) if linked else None
    if kind == "user":
        return [owner_id] if f.get("multiple") else owner_id
    day = engine.today + timedelta(days=nth_date)
    return {"text": f"Test {f['label']} {n}", "long_text": "Some notes.", "rich_text": "Some **notes**.", "email": f"test{n}@example.com",
            "phone": "0400 000 000", "url": "example.com", "number": 3, "money": 120.5, "percent": 0.1, "duration": 1.5, "rating": 4,
            "date": day.isoformat(), "datetime": f"{day.isoformat()} 09:30", "time": "09:30", "checkbox": True,
            "select": (f.get("options") or [None])[0], "multi_select": (f.get("options") or [])[:1], "tags": ["test"],
            "color": "#336699", "json": {"test": n}, "address": "1 Test St\nPerth WA 6000", "location": "-31.95, 115.86",
            "barcode": f"T{n:06d}"}.get(kind)


def _others(engine, f, n):
    """Other ordinary values for a field a sample was refused over."""
    kind = engine.kind(f)
    if kind in ("date", "datetime"):
        return [(engine.today + timedelta(days=d)).isoformat() for d in (7, 30, -7)]
    if kind in ("number", "money", "duration"):
        return [1, 100, 0]
    if kind == "percent":
        return [0.05, 0.5, 0]
    if kind == "select":
        return (f.get("options") or [])[1:FIELD_TRIES + 1]
    if kind == "checkbox":
        return [False]
    if kind in ("text", "long_text"):
        return [f"Sample {n}", "A"]
    return []


def in_order(engine):
    """The tables, each after the ones it links to (so a quote's customer exists first)."""
    tables, order, placed = list(engine.tables.values()), [], set()
    for _ in range(len(tables) + 1):
        for t in tables:
            needs = {f["table"] for f in t["fields"] if f["type"] == "link" and f["table"] != t["key"] and f["table"] in engine.tables}
            if t["key"] not in placed and needs <= placed:
                order.append(t)
                placed.add(t["key"])
    return order + [t for t in tables if t["key"] not in placed]


def sample_values(engine, t, owner_id, made, n):
    values, nth_date = {}, 0
    for f in t["fields"]:
        if f["type"] in schema.STORED and f["type"] != "autonumber" and not f.get("readonly"):
            v = sample_value(engine, t, f, owner_id, made, n, nth_date)
            nth_date += engine.kind(f) in ("date", "datetime")
            if v is not None:
                values[f["key"]] = v
    return values


def _add(engine, t, values):
    """Add a sample, trying other ordinary values for what it was refused over. Returns (record, errors)."""
    errors = {}
    for attempt in range(FIELD_TRIES + 1):
        try:
            return engine.create(t["key"], values, ai=False), {}
        except FieldErrors as exc:
            errors = exc.errors
            if attempt == FIELD_TRIES:
                break
            changed = False
            for key in exc.errors:
                f = schema.field(t, key)
                if f is None:  # a rule across fields ("_"): later dates a little later
                    for g in t["fields"]:
                        if engine.kind(g) in ("date", "datetime") and values.get(g["key"]):
                            values[g["key"]] = (engine.today + timedelta(days=7 * (attempt + 1) + t["fields"].index(g))).isoformat()
                            changed = True
                    continue
                others = _others(engine, f, attempt)
                if attempt < len(others):
                    values[key] = others[attempt]
                    changed = True
                elif not f.get("required"):
                    values.pop(key, None)
                    changed = True
            if not changed:
                break
    return None, errors


# --- trying everything ----------------------------------------------------------------------------------------------

def _issue(level, text, plain, where=""):
    return {"level": level, "text": text, "plain": plain, "where": where}


def walk(trial, screens=True):
    """Try everything once in a copy. Returns {"issues", "crashes", "made"}."""
    from . import checks
    engine = trial.engine()
    issues, crashes, made = list(checks.run(engine)), [], {}
    for n, t in enumerate(in_order(engine), start=1):
        if t.get("combines"):  # it fills itself from the lists it's put together from
            continue
        try:
            record, errors = _add(engine, t, sample_values(engine, t, trial.owner_id, made, n))
        except Exception as exc:  # noqa: BLE001 - a fault of ours: noted, and the rest is still tried
            crashes.append(f"adding a {t['singular']}: {type(exc).__name__}: {exc}")
            db.session.rollback()
            continue
        if record is not None:
            made[t["key"]] = record.id
        elif not any(str(k) in {f["key"] for f in t["fields"] if f["type"] == "link" and f["table"] not in made} for k in errors):
            said = "; ".join(f"{k}: {v}" for k, v in errors.items())
            issues.append(_issue("check", f"Adding a {t['key']} record with ordinary values was refused, even after trying other "
                                          f"values: {said}", f"A new {schema.lc(t['singular'])} with ordinary details couldn't be saved: "
                                          f"{'; '.join(errors.values())}", t["key"]))
    engine.reset()
    for t in engine.tables.values():  # what a combined list got from the samples, to open like the rest
        rows = engine.rows(t["key"]) if t.get("combines") else []
        if rows:
            made[t["key"]] = rows[0].id
    for t in engine.tables.values():
        record = engine.get(made[t["key"]]) if made.get(t["key"]) else None
        if record is None:
            continue
        issues += _formulas(engine, t, record) + _money_sense(engine, t, record)
    issues += _stages(trial, engine, made, crashes)
    for a in engine.bp.get("automations", []):
        table = a["trigger"].get("table")
        record = engine.get(made[table]) if table and made.get(table) else None
        try:
            if a["trigger"]["type"] == "button" and record is not None:
                automations.press(engine, a, engine.get(record.id), engine.by)
            elif a["trigger"]["type"] == "schedule":
                automations.run_schedule(engine, a, "Tried out")
        except Exception as exc:  # noqa: BLE001
            crashes.append(f"running {a['name']}: {type(exc).__name__}: {exc}")
            db.session.rollback()
        engine.reset()
    issues += _documents(engine, made, crashes)
    issues += _flows(trial, engine, made, crashes)
    issues += failed_runs(engine)
    issues += _bare(trial, engine, made, crashes)
    if screens:
        crashes += _screens(trial, engine, made)
    return {"issues": _unique(issues), "crashes": crashes, "made": made}


def _flows(trial, engine, made, crashes):
    """Each flow done once, as someone on the floor would: the form filled in, the first thing scanned, every line
    counted to its target, a photo and a signature, the first button. One that can't be finished is found here."""
    from ..engine import flows
    from ..storage import save
    out = []
    for flow in schema.flows(engine.bp):
        t = engine.tables.get(flow["table"])
        record = engine.get(made[flow["table"]]) if flow["start"] == "record" and made.get(flow["table"]) else None
        if t is None or (flow["start"] == "record" and record is None):
            continue
        try:
            # the flow's steps are what's tried, whatever stage the sample record is at (its button shows at one)
            run = flows.begin(engine.system() if flow.get("who") else engine, {**flow, "when": None}, trial.owner_id, record)
            sample = sample_values(engine, t, trial.owner_id, made, 50)
            for step in flow["steps"]:
                kind, values, files = step["kind"], {}, []
                if kind == "form":
                    values = {k: sample.get(k) for k in step["fields"]}
                elif kind == "scan":
                    item = schema.field(engine.tables[step["into"]["table"]], step["into"]["item"])
                    if item and made.get(item["table"]):
                        values = {"scanned": [{"id": made[item["table"]], "qty": 2}]}
                elif kind == "pick":
                    values = {"counts": {str(x["id"]): x["target"] or 1 for x in flows.pick_lines(engine, flow, step, flows.record_of(engine, run))}}
                elif kind in ("photo", "signature"):
                    files = [save(engine.ws.id, _PIXEL, f"{step['label']}.png", by="Tried out", mime="image/png").id]
                elif kind == "decide":
                    values = {"choice": 0}
                elif kind in ("info", "print"):
                    continue
                errors = flows.answer(engine, run, step, values, files)
                if errors:
                    out.append(_issue("fix", f"The flow {flow['key']} can't get past its step {step['key']} with ordinary answers: "
                                             f"{'; '.join(errors.values())}",
                                      f"“{flow['label']}” can't get past “{step['label']}”: {'; '.join(errors.values())}", flow["table"]))
                    break
                if kind == "decide" and step["options"][0].get("finish"):
                    break
            else:
                flows.finish(engine, run, "Tried out")
        except FieldErrors as exc:
            db.session.rollback()
            said = "; ".join(exc.errors.values())
            out.append(_issue("fix", f"Finishing the flow {flow['key']} was refused: {said}",
                              f"“{flow['label']}” can't be finished: {said}", flow["table"]))
        except Exception as exc:  # noqa: BLE001 - a fault of ours, noted
            db.session.rollback()
            crashes.append(f"doing the flow {flow['key']}: {type(exc).__name__}: {exc}")
        engine.reset()
    return out


_PIXEL = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                       "1f15c4890000000d49444154789c6360f8cfc0f01f0005000201e2f0bb9e0000000049454e44ae426082")


def _bare(trial, engine, made, crashes):
    """The same again with only what each form insists on filled in, as people do: a plan's line with just its procedure
    picked, then the plan accepted. A rule that copies what's usually left empty into what can't be (an invoice line's
    description) only stops this way."""
    since = db.session.query(db.func.max(Run.id)).filter(Run.workspace_id == engine.ws.id).scalar() or 0
    bare = {}
    for n, t in enumerate(in_order(engine), start=1):
        if t.get("combines"):
            continue
        values = {k: v for k, v in sample_values(engine, t, trial.owner_id, {**made, **bare}, 100 + n).items()
                  if (schema.field(t, k) or {}).get("required") or (schema.field(t, k) or {}).get("type") == "link"}
        if len(values) == len(sample_values(engine, t, trial.owner_id, made, 100 + n)):
            continue  # nothing it can be left without: tried already
        try:
            record, _ = _add(engine, t, values)
        except Exception as exc:  # noqa: BLE001
            crashes.append(f"adding a bare {t['singular']}: {type(exc).__name__}: {exc}")
            db.session.rollback()
            continue
        if record is not None:
            bare[t["key"]] = record.id
    engine.reset()
    _stages(trial, engine, bare, crashes)
    out = []
    for i in failed_runs(engine, since):
        i["text"] = "With only the required fields filled in (as people often leave the rest): " + i["text"]
        i["plain"] = "When only what's required is filled in, " + i["plain"][:1].lower() + i["plain"][1:]
        out.append(i)
    return out


def _unique(issues):
    seen, out = set(), []
    for i in issues:
        if i["text"] not in seen:
            seen.add(i["text"])
            out.append(i)
    return out


def _formulas(engine, t, record):
    out = []
    for f in t["fields"]:
        if f["type"] != "formula":
            continue
        try:
            expr.evaluate(schema.percents_fixed(t, f["expression"]), RecordContext(engine, record))
        except NoRate:
            continue  # the owner's to add: said once, as a check (checks.py), not a fault of the design
        except expr.ExprError as exc:
            out.append(_issue("fix", f"{t['key']}.{f['key']} = {f['expression']} can't be worked out on a {t['key']} record with every "
                                     f"field filled in: {exc}", f"{t['singular']}’s {f['label']} can't be worked out ({exc})", t["key"]))
    return out


TAX = re.compile(r"\b(gst|vat|tax|sales_tax)\b")


def _money(engine, t, record, *patterns, exclude=None):
    """The value of a money field whose key or label matches the first pattern that one does."""
    for pattern in patterns:
        for f in t["fields"]:
            name = f"{f['key']} {schema.lc(f['label'])}".replace("_", " ")
            if engine.kind(f) == "money" and re.search(pattern, name) and not (exclude and re.search(exclude, name)):
                value = to_number(engine.value(record, f["key"]))
                if value is not None:
                    return f, value
    return None, None


def _money_sense(engine, t, record):
    """Sums that can't be right whatever the business: a discount bigger than what it's off, tax that's most of the price,
    a total that isn't the subtotal less the discount plus the tax (tax counted twice, a discount left out)."""
    sub_f, subtotal = _money(engine, t, record, r"\bsub ?total\b")
    if sub_f is None or not subtotal:
        return []
    off_f, off = _money(engine, t, record, r"\bdiscount\b", exclude=r"percent|rate|%")
    tax_f, tax = _money(engine, t, record, r"\b(gst|vat|tax)\b", exclude=r"rate|percent|%|inc|code")
    total_f, total = _money(engine, t, record, r"^(grand )?total\b", r"\btotal (inc|incl|including)\b", exclude=r"sub ?total|paid|deposit")
    off, out, shown = off or 0, [], lambda v: engine.display({"type": "money"}, v)
    facts = f"Subtotal {shown(subtotal)}" + (f", {off_f['label']} {shown(off)}" if off_f else "") + \
        (f", {tax_f['label']} {shown(tax)}" if tax_f else "") + (f", {total_f['label']} {shown(total)}" if total_f else "")
    if off_f and off > subtotal + 0.01:
        out.append(_issue("fix", f"On a sample {t['key']} record ({facts}) {off_f['key']} is more than {sub_f['key']}: a discount "
                                 f"can't be bigger than what it's off (a percentage used as a whole number?)",
                          f"A {schema.lc(t['singular'])}’s {off_f['label']} came out bigger than its subtotal ({facts})", t["key"]))
    if tax_f and tax is not None and tax > 0.35 * max(subtotal - off, 0.01):
        out.append(_issue("check", f"On a sample {t['key']} record ({facts}) {tax_f['key']} is more than a third of the price",
                          f"A {schema.lc(t['singular'])}’s {tax_f['label']} looks too high ({facts})", t["key"]))
    near = lambda a, b: abs(a - b) <= 0.02  # noqa: E731
    if tax_f and total_f and tax is not None and total is not None and not (near(total, subtotal - off + tax) or near(total, subtotal - off)):
        # the total is neither the price plus its tax nor a price that includes it
        why = ("leaves out the discount" if off and near(total, subtotal + tax) else
               "counts the tax twice" if near(total, subtotal - off + 2 * tax) else "doesn't add up")
        out.append(_issue("check", f"On a sample {t['key']} record ({facts}) {total_f['key']} {why}: it should be {sub_f['key']}"
                                   f"{' - ' + off_f['key'] if off_f else ''} + {tax_f['key']} (or the price itself, if prices include tax)",
                          f"A {schema.lc(t['singular'])}’s total {why} ({facts})", t["key"]))
    return out


def _flow_moves_on(engine, t, record, field, later):
    """Whether a flow done on the record here (Receive the delivery, while it's Sent) can set one of the stages after it."""
    return any(f["table"] == t["key"] and f["start"] == "record" and (not f.get("when") or engine.matches(record, f["when"]))
               and any(s["kind"] == "decide" and s.get("field") == field and any(o["value"] in later for o in s["options"]) for s in f["steps"])
               for f in schema.flows(engine.bp))


def _stages(trial, engine, made, crashes):
    """Each record taken along its stages, onwards before any "no" or stop, by the first move to a stage it hasn't been
    at; what a move asks to have filled in first is filled in, as its button asks for it. A stage it can only leave by
    a stop (Cancel), with stages still to come, is a dead end."""
    out = []
    for t in engine.tables.values():
        flow = t.get("workflow")
        if not flow or not made.get(t["key"]) or not schema.stages(t):
            continue
        record, been, names = engine.get(made[t["key"]]), set(), schema.stages(t)
        for n in range(len(names) + 1):
            at = (record.data or {}).get(flow["field"])
            been.add(at)
            moves = sorted(((i, m) for i, m, problem in engine.moves(record) if m["to"] not in been
                            and (not problem or not engine.move_problem(t, record, m, filled=True))), key=lambda x: schema.says_no(x[1]["to"]))
            later = [s for s in names[names.index(at) + 1:] if not schema.says_no(s)] if at in names else []
            if later and not schema.says_no(at) and not schema.ENDED.search(str(at)) and not engine.waiting_for(record) \
                    and not any(not schema.says_no(m["to"]) for _, m, _ in engine.moves(record)) \
                    and not _flow_moves_on(engine, t, record, flow["field"], later):
                stops = [m.get("label") or m["to"] for _, m in moves]
                pressed = next((a for a in engine.bp.get("automations", []) if a["trigger"]["type"] == "button"
                                and a["trigger"].get("table") == t["key"] and any(str((x.get("values") or {}).get(flow["field"], "")) in later
                                                                                  for x in schema.steps_of(a["actions"]) if x.get("type") == "update_record")), None)
                instead = (f"; the button rule {pressed['key']} sets the stage instead, which skips the stage's own move and what it "
                           f"checks (who may, what must be filled in): make it a workflow move from {at}, and the rule's other steps a "
                           f"rule on that move" if pressed else "")
                out.append(_issue("fix", f"A {t['key']} record at {at} can't move on to {', '.join(later)}: the workflow has no move from "
                                         f"{at}" + (f" but {', '.join(stops)}" if stops else "") + instead,
                                  f"A {schema.lc(t['singular'])} at {at} can't go on to {' or '.join(later[:2])}: "
                                  + (f"the only way out is {' or '.join(stops)}" if stops else "nothing moves it on")
                                  + (f" (“{pressed['name']}” sets it with a rule, skipping the stage's own checks)" if pressed else ""), t["key"]))
            if not moves:
                break
            i, m = moves[0]
            try:
                needs = {f["key"]: v for f in engine.to_fill(t, record, m)
                         for v in [sample_value(engine, t, f, trial.owner_id, made, 200 + n, 0)] if v is not None}
                if needs:
                    engine.update(record, needs)
                    engine.reset()
                    record = engine.get(record.id)
                engine.move(record, i)
            except FieldErrors as exc:
                out.append(_issue("check", f"Moving a {t['key']} record from {at} to {m['to']} (“{m.get('label') or m['to']}”) was "
                                           f"refused although the move showed as open: {'; '.join(exc.errors.values())}",
                                  f"“{m.get('label') or m['to']}” on a {schema.lc(t['singular'])} was offered, then refused: "
                                  f"{'; '.join(exc.errors.values())}", t["key"]))
                break
            except Exception as exc:  # noqa: BLE001
                crashes.append(f"moving a {t['key']} to {m['to']}: {type(exc).__name__}: {exc}")
                db.session.rollback()
                break
            engine.reset()
            record = engine.get(record.id)
    return out


def _documents(engine, made, crashes):
    from ..documents import produce, text_file
    out = []
    for t in engine.tables.values():
        record = engine.get(made[t["key"]]) if made.get(t["key"]) else None
        for d in t.get("documents", []) if record is not None else []:
            try:
                if d.get("format") == "text":  # a file in another system's layout: made when it's downloaded (never kept if private)
                    text_file(engine, record, d["key"])
                elif d.get("format") == "label":  # printed, never kept
                    from ..documents import labels
                    labels.render(labels.for_records(engine, [record], d), d.get("size"))
                else:
                    produce(engine, record, d["key"], by=engine.by)
            except (ValueError, expr.ExprError, KeyError) as exc:
                out.append(_issue("fix", f"The document {t['key']}.{d['key']} couldn't be made for a {t['key']} record: {exc}",
                                  f"The {d['label']} couldn't be made ({exc})", t["key"]))
            except Exception as exc:  # noqa: BLE001
                crashes.append(f"making {t['key']}.{d['key']}: {type(exc).__name__}: {exc}")
                db.session.rollback()
    return out


def failed_runs(engine, since_id=0):
    """The automations that stopped with a problem while things were tried."""
    out = []
    for run in Run.query.filter(Run.workspace_id == engine.ws.id, Run.status == "failed", Run.id > since_id).all():
        step = next((s for s in reversed(run.steps or []) if s.get("result")), {})
        error = run.error or step.get("result") or "it stopped"
        # the app's own records may name it differently: worth a look, not certain (a name that's still a {{template}}
        # or a set of fields is wrong whatever the records are)
        named = re.search(r"there's no .+ called “[^{]*”", error)
        out.append(_issue("check" if named else "fix", f"The automation {run.automation} (“{run.name}”) stopped at its step {step.get('at', '?')} "
                                 f"({step.get('action', '')}): {error}",
                          f"The rule “{run.name}” stopped: {error}", run.table or ""))
    return out


def _screens(trial, engine, made):
    """Every screen opened by the owner, and again by someone in each team: a team's own page or list is checked the
    way the team gets it, not only the way the owner does."""
    wid, out = trial.ws_id, []
    paths = [f"/w/{wid}/app/"] + [f"/w/{wid}/app/p/{p['key']}" for p in engine.bp.get("pages", [])]
    for t in engine.tables.values():
        if t.get("embedded"):
            continue
        paths += [f"/w/{wid}/app/t/{t['key']}?view={v['key']}" for v in t.get("views", [])] or [f"/w/{wid}/app/t/{t['key']}"]
        paths.append(f"/w/{wid}/app/t/{t['key']}/new")
        if made.get(t["key"]):
            paths += [f"/w/{wid}/app/t/{t['key']}/{made[t['key']]}", f"/w/{wid}/app/t/{t['key']}/{made[t['key']]}/edit"]
    # the studios draw the whole design: each diagram in them too
    paths += [f"/w/{wid}/app/studio/data", f"/w/{wid}/app/studio/workflows"]
    paths += [f"/w/{wid}/app/studio/workflows/item?id=rule:{a['key']}" for a in engine.bp.get("automations", [])]
    paths += [f"/w/{wid}/app/studio/workflows/item?id=stages:{t['key']}" for t in engine.tables.values() if t.get("workflow")]
    paths += [f"/w/{wid}/app/studio/workflows/item?id=flow:{f['key']}" for f in engine.bp.get("flows") or []]
    people = [(None, None)] + [(tm["key"], _in_team(engine, tm)) for tm in schema.teams(engine.bp)]
    for team, user_id in people:
        client = trial.client(user_id)
        for path in paths:
            try:
                status = client.get(path).status_code
            except Exception as exc:  # noqa: BLE001
                status = f"{type(exc).__name__}: {exc}"
            if not isinstance(status, int) or status >= 500:
                out.append(f"GET {path}{f' (as someone in {team})' if team else ''}: {status}")
    return out


def _in_team(engine, team):
    """A stand-in staff member in a team, for opening screens as that team does."""
    user = User(email=f"in.{re.sub(r'[^a-z0-9]+', '.', team['key'].lower())}@example.com", name=f"Someone in {team.get('name') or team['key']}")
    user.password_hash = "!"  # nobody signs in as them
    db.session.add(user)
    db.session.flush()
    db.session.add(Membership(workspace_id=engine.ws.id, user_id=user.id, role="editor", teams=[team["key"]]))
    db.session.commit()
    engine._members = None
    return user.id


# --- worked examples --------------------------------------------------------------------------------------------------

class ExampleWrong(Exception):
    """The example asks for something the app doesn't have (a table, field, stage or button by another name)."""


def _table(engine, key):
    t = engine.tables.get(key) or next((x for x in engine.tables.values() if key and key.lower() in (x["label"].lower(), x["singular"].lower())), None)
    if t is None:
        raise ExampleWrong(f"there's no table “{key}” (tables: {', '.join(engine.tables)})")
    return t


def _record(engine, names, alias):
    if alias not in names:
        alias = _guess(engine, names, alias)
    if alias not in names:
        raise ExampleWrong(f"no record was added as “{alias}” earlier in the example")
    record = engine.get(names[alias])
    if record is None:
        raise ExampleWrong(f"“{alias}” was deleted")
    return record


def _person(engine, value, make=False):
    """A person by name or email ("me" and "owner" are whoever's trying it): their id. With `make`, someone the example
    names who isn't in the copy (the team lead "Sam") joins it as a stand-in, as the owner would invite them: otherwise
    whoever's trying it would be everyone, and approving what they raised themselves rightly isn't allowed."""
    members = engine.members()
    if isinstance(value, int) or str(value).isdigit():
        return int(value)
    text = str(value or "").strip().lower()
    if text in ("me", "owner", "the owner", "i", ""):
        return engine.user_id
    found = next((uid for uid, name in members.items() if text in (str(name).lower(), str(name).lower().split()[0])
                  or text == engine._emails.get(uid, "").lower()), None)
    if found is not None or not make:
        return found if found is not None else engine.user_id
    name = str(value).strip()
    user = User(email=name.lower() if "@" in name else re.sub(r"\W+", ".", name.lower()).strip(".") + "@example.com",
                name=name.split("@")[0] if "@" in name else name)
    user.password_hash = "!"  # nobody signs in as them
    db.session.add(user)
    db.session.flush()
    db.session.add(Membership(workspace_id=engine.ws.id, user_id=user.id, role="editor"))
    db.session.commit()
    engine._members = None
    return user.id


def _guess(engine, names, alias, table=None):
    """A name used but never given ("q_jane" for the quote added just before): the latest record added of its table (the
    link's table, or the one the name starts like), named so from now on."""
    if not isinstance(alias, str) or not alias.strip():
        return alias
    engine.reset()
    if any(engine.title(r).strip().lower() == alias.strip().lower() for key in ([table] if table else engine.tables)
           for r in engine.rows(key)):
        return alias  # a record's own name (the "Wages" account): it means that record, not a guess
    added = names.get("_added", [])
    head = alias.strip().lower().split("_")[0].rstrip("0123456789")
    like = lambda key: key.startswith(head) or "".join(w[:1] for w in key.split("_")) == head  # noqa: E731  ("il" for invoice_lines)
    fits = [(key, rid) for key, rid in reversed(added) if key == table] if table else \
        sorted([(key, rid) for key, rid in reversed(added) if head and like(key)],
               key=lambda x: bool(engine.tables.get(x[0], {}).get("embedded")))
    if not fits and head:  # one the app made itself (the job an accepted quote became)
        engine.reset()
        made = [(not engine.tables[key].get("embedded"), r.id, key) for key in engine.tables
                if (table and key == table) or (not table and like(key)) for r in engine.rows(key)]
        fits = [(key, rid) for _, rid, key in sorted(made, reverse=True)]  # a main record before a line, the latest first
    if fits and not any(alias.strip().lower() == engine.title(engine.get(rid)).lower() for _, rid in added if engine.get(rid)):
        names[alias] = fits[0][1]
    return alias


def _values(engine, t, values, names):
    out = {}
    for key, value in (values or {}).items():
        f = schema.field(t, key) or next((x for x in t["fields"] if x["label"].lower() == str(key).lower()), None)
        if f is None:
            raise ExampleWrong(f"{t['key']} has no field “{key}” (fields: {', '.join(x['key'] for x in t['fields'])})")
        if f["type"] in schema.CALCULATED:
            raise ExampleWrong(f"{t['key']}.{f['key']} is worked out by the app, so it can't be typed in: expect it instead")
        if f["type"] == "link":
            many = value if isinstance(value, list) else [value]
            for v in many:
                if isinstance(v, str) and v not in names:
                    _guess(engine, names, v, f["table"])
            ids = [names.get(v, v) if isinstance(v, str) else v for v in many]
            value = ids if f.get("multiple") else ids[0]
        elif engine.kind(f) in ("date", "datetime") and names.get("_waited"):  # a date on the calendar, after days passed
            value = _shifted(engine, f, value, -names["_waited"])
        elif f["type"] == "user":  # a record's name ("s1", the staff member Sam) means that person
            many = [engine.title(engine.get(names[v])) if isinstance(v, str) and isinstance(names.get(v), int) and engine.get(names[v])
                    else v for v in (value if isinstance(value, list) else [value])]
            value = [_person(engine, v, make=True) for v in many] if isinstance(value, list) else _person(engine, many[0], make=True)
        out[f["key"]] = value
    return out


def _act(trial, engine, step, names):
    """Do one step of an example as whoever would. Raises ExampleWrong or FieldErrors (the app refused)."""
    do = step.get("do")
    if do == "add":
        t = _table(engine, step.get("table"))
        names["_adding"] = step.get("as")
        try:
            record = engine.create(t["key"], _values(engine, t, step.get("values"), names), ai=False)
        finally:
            names.pop("_adding", None)
        if step.get("as"):
            names[step["as"]] = record.id
        names.setdefault("_added", []).append((t["key"], record.id))
        return
    if do == "wait":  # days pass: reminders fall due, approvals get chased, things get overdue
        days = int(to_number(step.get("days")) or 1)
        pass_time(engine, days)
        names["_waited"] = names.get("_waited", 0) + days
        return
    if do == "run":  # a scheduled rule, as if its time had come: about no one record
        wanted = str(step.get("rule") or step.get("button") or "").strip().lower()
        a = next((a for a in engine.bp.get("automations", []) if wanted in (a["key"].lower(), a["name"].lower())), None)
        if a is None:
            raise ExampleWrong(f"there's no automation “{wanted}” (automations: {', '.join(a['key'] for a in engine.bp.get('automations', []))})")
        if a["trigger"]["type"] != "schedule":  # it runs by itself when its moment comes: running it as well does it twice
            raise ExampleWrong(f"“run” is for scheduled automations; {a['key']} starts by itself "
                               f"({automations.describe_trigger(engine, a['trigger']).lower()}): leave the “run” step out")
        automations.run_schedule(engine, a, "Tried out")
        return
    record = _record(engine, names, step.get("record"))
    t = engine.tables[record.table]
    if do == "change":
        values = _values(engine, t, step.get("values"), names)
        if step.get("refused") and engine.locked(record) is None:  # the owner may change what's locked: it's kept from staff
            staff = trial.engine(_person(engine, "Staff member", make=True))
            theirs = staff.get(record.id) or record  # kept from their view (another rep's customer), it's as locked to them
            if staff.locked(theirs):
                staff.update(theirs, values)
                return
        engine.update(record, values)
    elif do == "move":
        wanted = str(step.get("to") or "").strip().lower()
        if not t.get("workflow"):  # no buttons: people set its Status themselves
            f = next((f for f in t["fields"] if f["type"] == "select" and re.search(r"status|stage|state", f["key"])
                      and wanted in [o.lower() for o in f.get("options") or []]), None)
            if f is None:
                raise ExampleWrong(f"{t['key']} has no stage “{step.get('to')}”")
            engine.update(record, {f["key"]: next(o for o in f["options"] if o.lower() == wanted)})
            return
        if wanted not in [s.lower() for s in schema.stages(t)]:
            raise ExampleWrong(f"{t['key']} has no stage “{step.get('to')}” (stages: {', '.join(schema.stages(t))})")
        if not _press_move(engine, record, t, lambda m: m["to"].lower() == wanted):
            _take_along(engine, record, t, step.get("to"))
    elif do == "press":
        wanted = str(step.get("button") or "").strip().lower()
        a = next((a for a in engine.bp.get("automations", []) if a["trigger"]["type"] == "button"
                  and wanted in (a["key"].lower(), a["name"].lower(), str(a["trigger"].get("label") or "").lower())), None)
        if a is None:
            buttons = [a["key"] for a in engine.bp.get("automations", []) if a["trigger"]["type"] == "button"]
            raise ExampleWrong(f"there's no button “{step.get('button')}” (buttons: {', '.join(buttons) or 'none'})")
        automations.press(engine, a, record, engine.by)
    elif do in ("approve", "reject"):
        waiting = Approval.query.filter(Approval.workspace_id == trial.ws_id, Approval.record_id == record.id,
                                        Approval.status == "pending").order_by(Approval.id).all()
        if not waiting:  # its own Approve button (a move to Approved, the client's Sign off), as the person would press it
            words = APPROVING if do == "approve" else REJECTING
            if t.get("workflow") and _press_move(engine, record, t, lambda m: words.search(f"{m.get('label') or ''} {m['to']}")):
                return
            asking = {a["key"] for a in engine.bp.get("automations", []) if a["trigger"].get("table") == t["key"]
                      and any(act["type"] == "request_approval" for act in schema.steps_of(a["actions"]))}
            if not asking:  # nothing ever asks: the example meant a move
                open_ = [m["to"] for _, m, _ in engine.moves(record)] if t.get("workflow") else []
                raise ExampleWrong(f"nothing asks for someone's OK on a {schema.lc(t['singular'])}: “{do}” answers an approval a rule "
                                   f"asks for; to move it on, “move” it" + (f" (it can go to: {', '.join(open_)})" if open_ else ""))
            done = Run.query.filter(Run.workspace_id == trial.ws_id, Run.record_id == record.id, Run.automation.in_(asking),
                                    Run.status == "done").first()
            if done is not None and not Approval.query.filter_by(workspace_id=trial.ws_id, record_id=record.id).count():
                took = next((s["result"] for s in done.steps or [] if str(s.get("result", "")).startswith("It's “")), "")
                raise ExampleWrong(f"the rule “{done.name}” didn't need anyone's OK this time{' (' + took + ')' if took else ''}: "
                                   f"leave out the “{do}” step, or make the example one that needs it")
            raise FieldErrors({"_": f"nothing was waiting for someone's OK on this {schema.lc(t['singular'])}"})
        approval = waiting[0]
        people = [approval.assignee_id, trial.owner_id] + [m.user_id for m in Membership.query.filter_by(workspace_id=trial.ws_id)]
        who = next((uid for uid in people if uid and automations.may_decide(approval, trial.member(uid), uid, record, trial.engine(uid))), None)
        if who is None:
            raise FieldErrors({"_": f"nobody could answer “{approval.message}”"})
        decider = trial.engine(who)
        said = step.get("comment") or step.get("reason") or next((v for v in (step.get("values") or {}).values() if isinstance(v, str)), None)
        if approval.kind == "task":
            automations.complete(decider, approval, decider.by, comment=said)
        elif approval.kind == "review" and do == "approve":  # a person checks the AI's draft and keeps it as it is
            automations.complete_review(decider, approval, decider.by, comment=said)
        else:  # what they'd write answering it ("the quote is too old"): the rule's {{reason}}
            automations.decide(decider, approval, do == "approve", decider.by, comment=said, by_id=decider.user_id)
    else:
        raise ExampleWrong(f"“{do}” isn't a step (add, change, move, press, approve, reject, wait or run)")


APPROVING = automations.APPROVES
REJECTING = re.compile(r"\b(reject|declin|refus|turn(s|ed)? down)", re.I)


def _way(t, start, target):
    """The stages a record goes through, move by move, from one stage to another (the shortest way), or None."""
    moves = t["workflow"]["transitions"]
    ways, seen = [[str(start or "")]], {str(start or "").lower()}
    while ways:
        way = ways.pop(0)
        if way[-1].lower() == target.lower():
            return way[1:]
        for m in moves:
            if (not m.get("from") or way[-1].lower() in [x.lower() for x in m["from"]]) and m["to"].lower() not in seen:
                seen.add(m["to"].lower())
                ways.append(way + [m["to"]])
    return None


def _take_along(engine, record, t, target):
    """“Move it to Invoiced” from New: taken along the stages between by their buttons, as the person would (a rule may
    carry it on by itself). Raises FieldErrors where it can't go on."""
    field, began = t["workflow"]["field"], None
    for _ in range(len(schema.stages(t)) + 1):
        engine.reset()
        record = engine.get(record.id)
        at = (record.data or {}).get(field)
        began = began or at
        if str(at or "").lower() == str(target).lower():
            return
        way = _way(t, at, str(target))
        if not way:
            open_ = [m["to"] for _, m, _ in engine.moves(record)]
            raise FieldErrors({"_": f"from {at} there's no way to move it to {target} (it can go to: {', '.join(open_) or 'nowhere'})"})
        try:
            pressed = _press_move(engine, record, t, lambda m: m["to"].lower() == way[0].lower())
        except FieldErrors as exc:
            raise FieldErrors({"_": f"on its way from {began} to {target} (through {', '.join(way[:-1])}), it couldn't move from "
                                    f"{at} to {way[0]}: {'; '.join(exc.errors.values())}"}) from exc
        if not pressed:
            raise FieldErrors({"_": f"on its way from {began} to {target}, there was no button to move it from {at} to {way[0]}"})
    raise FieldErrors({"_": f"it went round its stages without reaching {target}"})


def _press_move(engine, record, t, pick):
    """Press the record's button for the first move `pick` likes, as its person would (a file it needs attached first).
    False when there's no such button now; raises FieldErrors when the app says not yet."""
    for m in t["workflow"]["transitions"]:  # a file the move needs is attached first, as the person would (examples can't)
        if pick(m):
            _attach(engine, record, t, m.get("requires") or [])
    engine.reset()
    record = engine.get(record.id)
    move = next(((i, m, problem) for i, m, problem in engine.moves(record) if pick(m)), None)
    if move is None:
        return False
    if move[2]:
        raise FieldErrors({"_": move[2]})
    engine.move(record, move[0])
    return True


def pass_time(engine, days):
    """As if `days` had passed in this copy: every date and time in it moves that much earlier (records' own dates, when
    they were added and changed, when approvals were asked), and what then falls due is done, as the worker would."""
    from ..models import RecordChange
    gone = timedelta(days=days)
    for r in Record.query.filter_by(workspace_id=engine.ws.id).all():
        t = engine.tables.get(r.table) or {"fields": []}
        data = dict(r.data or {})
        for f in t["fields"]:
            value = data.get(f["key"])
            if engine.kind(f) in ("date", "datetime") and f["type"] in schema.STORED and isinstance(value, str) and value:
                try:
                    if len(value) == 10:
                        data[f["key"]] = (date.fromisoformat(value) - gone).isoformat()
                    else:
                        moved = datetime.fromisoformat(value.replace(" ", "T")) - gone
                        data[f["key"]] = moved.isoformat(sep=" " if " " in value else "T", timespec="minutes")
                except ValueError:
                    pass
        r.data = data
        r.created_at, r.updated_at = (r.created_at or datetime.utcnow()) - gone, (r.updated_at or datetime.utcnow()) - gone
    for row in RecordChange.query.filter_by(workspace_id=engine.ws.id).all():
        row.created_at = (row.created_at or datetime.utcnow()) - gone
    for a in Approval.query.filter_by(workspace_id=engine.ws.id).all():
        a.created_at = (a.created_at or datetime.utcnow()) - gone
        if (a.proposal or {}).get("ready_at"):  # a change's cooling-off ends that much sooner too
            a.proposal = {**a.proposal, "ready_at": (datetime.fromisoformat(a.proposal["ready_at"]) - gone).isoformat()}
    for run in Run.query.filter_by(workspace_id=engine.ws.id).all():
        run.created_at = (run.created_at or datetime.utcnow()) - gone
        if run.next_try:
            run.next_try -= gone
    db.session.commit()
    for _ in range(3):  # what falls due, and what that sets off
        due = Run.query.filter(Run.workspace_id == engine.ws.id, Run.status.in_(["waiting", "waiting_approval"]),
                               Run.next_try <= datetime.utcnow()).all()
        if not due:
            break
        for run in due:
            automations.execute(engine.system(), run)
    engine.reset()


def _attach(engine, record, t, keys):
    from ..storage import save
    for key in keys:
        f = schema.field(t, key)
        if f is not None and f["type"] in schema.FILES and not engine.files(record, key):
            save(engine.ws.id, b"%PDF-1.4 sample", f"{key}.pdf", record_id=record.id, field=key, by=engine.by)
    engine.reset()


# the example left out what the app rightly asks for, named a record that isn't there ("no Patient called 'emma'"), or
# added a record at a later stage than new ones start at: its own mistake, not the app's
OMITTED = re.compile(r"^(Fill in .+ first|.+ is required( here)?|.*there's no .+ called|A new .+ starts at )")
# a value the example typed that isn't one (an ABN of 9 digits, a date that isn't a date, a choice that isn't offered): the
# app rightly refused it, and it's the example that's corrected, so it can go on to try what it was written for
BAD_VALUE = re.compile(r"isn't an? (number|date|time|duration|colour|location|email address|date and time|time of day)\b|"
                       r"isn't one of: |isn't a member of this workspace|^a rating is a number|isn't valid JSON")


FILLED = re.compile(r"^\s*(non[- ]?empty|not empty|filled( in)?|any(thing)?|<any>|\*)\s*$", re.I)  # "anything, as long as it's there"


def _shows_a_link(engine, f):
    """A lookup whose value is a linked record (a treatment note's patient, through its appointment)."""
    if f is None or f["type"] != "lookup":
        return False
    try:
        source = engine.source(f)
    except Exception:  # noqa: BLE001 - a lookup that can't be followed isn't a link
        return False
    return bool(source) and source["type"] == "link"


def _same(engine, f, actual, wanted, names):
    if isinstance(wanted, str) and FILLED.match(wanted):
        return actual not in (None, "", [])
    if f is not None and f.get("sensitive") and wanted not in (None, ""):  # kept private: its last four, as anyone sees it
        return masked(actual) == mask(wanted)
    if wanted in (None, "") or (isinstance(wanted, list) and not wanted):
        return actual in (None, "", [])
    kind = engine.kind(f) if f else None
    if kind == "percent":
        wanted = schema.as_fraction(wanted)
    if kind in ("number", "money", "percent", "duration", "rating") or (isinstance(wanted, (int, float)) and not isinstance(wanted, bool)):
        a, w = to_number(actual), to_number(str(wanted).replace("$", "").replace(",", "")) if not isinstance(wanted, (int, float)) else wanted
        return a is not None and w is not None and abs(a - w) <= max(0.011, abs(w) * 0.001)
    if f is not None and (f["type"] == "link" or _shows_a_link(engine, f)):  # a lookup of a link (a note's patient, by its visit) too
        ids = actual if isinstance(actual, list) else [actual]
        wanted_id = names.get(wanted) if isinstance(wanted, str) else wanted
        return any(str(i) == str(wanted_id) for i in ids) or any(engine.title(engine.get(i)).lower() == str(wanted).strip().lower()
                                                                    for i in ids if i and engine.get(i))
    if kind == "checkbox":
        return bool(actual) == (str(wanted).strip().lower() in ("true", "yes", "1", "ticked", "on"))
    if f is not None and kind in ("date", "datetime"):
        return str(actual or "")[:10] == str(wanted)[:10]
    if isinstance(actual, list):
        return [str(x).lower() for x in actual] == [str(x).lower() for x in (wanted if isinstance(wanted, list) else [wanted])]
    if f is not None and f["type"] == "user":
        return actual == _person(engine, wanted)
    return str(actual if actual is not None else "").strip().lower() == str(wanted).strip().lower()


def _shifted(engine, f, value, days):
    """A date (or date and time) `days` later, written as the field keeps it. Letting days pass moves every date in the
    copy that much earlier while today stays put (pass_time), so a date an example writes after a wait, and one it expects,
    is the calendar's less the days waited so far: an invoice issued on the 10th and due on the 24th is still due on the
    24th after waiting 15 days."""
    if value in (None, "") or isinstance(value, (list, dict, bool, int, float)):
        return value
    if engine.kind(f) == "date" or len(str(value)) == 10:
        d = expr.to_date(value)
        return (d + timedelta(days=days)).isoformat() if d else value
    moment = expr.to_datetime(value)
    return (moment + timedelta(days=days)).isoformat(sep=" " if " " in str(value) else "T", timespec="minutes") if moment else value


def _expect(engine, c, names):
    """One thing an example expects: (ok, what it shows in plain words, the same with keys for the AI)."""
    if c.get("count") is not None or (c.get("table") and not c.get("record")):
        t = _table(engine, c.get("table"))
        n = len(engine.rows(t["key"]))
        wanted = int(c.get("count") if c.get("count") is not None else c.get("equals") or 0)
        said = f"{n} {schema.lc(t['label']) if n != 1 else schema.lc(t['singular'])}"
        return n == wanted, said + ("" if n == wanted else f", expected {wanted}"), f"{t['key']} has {n} records; expected {wanted}"
    record = _record(engine, names, c.get("record"))
    t = engine.tables[record.table]
    key = c.get("field") or (t["workflow"]["field"] if c.get("stage") and t.get("workflow") else None)
    if not key:
        raise ExampleWrong("each expectation names a record and a field (or a table and a count)")
    wanted = c.get("equals", c.get("stage"))
    path = key.split(".")
    f, actual = engine._at_path(record, key)
    if f is None:
        raise ExampleWrong(f"{t['key']} has no field “{key}” (fields: {', '.join(x['key'] for x in t['fields'])})")
    if engine.kind(f) in ("date", "datetime") and names.get("_waited"):
        actual = _shifted(engine, f, actual, names["_waited"])  # as the calendar shows it, the days waited later
    ok = _same(engine, f, actual, wanted, names)
    label = automations._path_label(engine, t["key"], path)
    shown = engine.display(f, actual) if actual not in (None, "", []) else "empty"
    expected = engine.display(f, schema.as_fraction(wanted) if engine.kind(f) == "percent" else wanted) \
        if wanted not in (None, "") and f["type"] not in ("link", "user") and not _shows_a_link(engine, f) else str(wanted or "empty")
    plain = f"{t['singular']}’s {label}: {shown}" if ok else f"{t['singular']}’s {label} came out {shown}, expected {expected}"
    said = f"{c.get('record')} ({t['key']}).{key} is {actual!r}; expected {wanted!r}"
    if not ok and f.get("expression") and len(path) == 1:  # how it was worked out, to tell a wrong sum from a wrong expectation
        read = _formula_reads(engine, record, f["expression"], names)
        said += f" (worked out as `{f['expression']}`" + (f" from {read}" if read else "") + ")"
    if not ok and actual in (None, "", []) and len(path) == 1 and _checking(engine, record, key):
        plain += " (the AI's work is waiting for someone to check it)"
        said += (f" (the AI's work for it is waiting for someone to check it, as its step's review says: to try it as kept, "
                 f"add an “approve” step for {c.get('record')} before this, as whoever checks it would)")
    return ok, plain, said


def _formula_reads(engine, record, expression, names):
    """The values a formula read on this record, as the example's calendar shows them: “issue_date = '2026-10-01',
    customer.payment_terms = 30”."""
    plain = re.sub(r"'[^']*'|\"[^\"]*\"", "", expression)
    out = []
    for name in dict.fromkeys(re.findall(r"\b[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\b(?!\s*\()", plain)):
        if engine.field(record.table, name.split(".")[0]) is None:
            continue
        try:
            g, value = engine._at_path(record, name)
        except Exception:  # noqa: BLE001 - a path it can't follow: left out of the account
            continue
        if g is not None and engine.kind(g) in ("date", "datetime") and names.get("_waited"):
            value = _shifted(engine, g, value, names["_waited"])
        out.append(f"{name} = {value!r}")
    return ", ".join(out[:8])


def _checking(engine, record, key):
    """Whether the AI's work for this field of this record is waiting for a person to check it."""
    return any(key in (a.fields or []) for a in Approval.query.filter_by(workspace_id=engine.ws.id, record_id=record.id,
                                                                         status="pending", kind="review"))


def play(trial, example):
    """Act out one example in this copy. Returns {"title", "ok", "lines": [(ok, plain)], "problem", "kind", "detail"}:
    kind "app" when the app refused a step, a rule stopped or something came out other than expected; "example" when the
    example itself names what isn't there."""
    engine, names = trial.engine(), {}
    out = {"title": str(example.get("title") or "An example")[:160], "ok": False, "lines": [], "problem": None, "kind": None, "detail": []}
    token = automations.stand_in.set(_ai_answers(trial, engine, example, names))
    try:
        return _play(trial, engine, example, names, out)
    finally:
        automations.stand_in.reset(token)


def _ai_answers(trial, engine, example, names):
    """How the app's AI steps answer while this example is tried: what the example expects of the field the step fills
    for that record (the AI's own judgement can't be tried; what the app does with it can), or an ordinary value."""
    filled = {(a["trigger"].get("table"), k) for a in engine.bp.get("automations", []) for act in schema.steps_of(a["actions"])
              for k in ([act.get("field")] if act.get("type") in ("ai_fill", "ai_check") else act.get("fields") or []
                        if act.get("type") == "ai_extract" else [])}
    wanted = {}
    for c in example.get("expect") or []:
        if c.get("record") and c.get("field") and "." not in str(c["field"]) and "equals" in c:
            wanted.setdefault(c["record"], {})[c["field"]] = c["equals"]

    def answer(record, key):
        f = engine.field(record.table, key)
        if f is None or (record.table, key) not in filled:
            return None
        alias = next((a for a, rid in names.items() if rid == record.id and key in wanted.get(a, {})), None)
        adding = names.get("_adding")  # a rule run as the record is added, before the example has named it
        if alias is None and key in wanted.get(adding, {}) and record.id not in names.values():
            alias = adding
        value = wanted[alias][key] if alias else None
        if value is None or (isinstance(value, str) and FILLED.match(value)):  # "anything": an ordinary value
            value = sample_value(engine, engine.tables[record.table], f, trial.owner_id, {}, 300, 0)
        elif f["type"] == "link":  # the record the example calls inv1, as the AI would name it
            value = [names.get(v, v) if isinstance(v, str) else v for v in value] if isinstance(value, list) \
                else names.get(value, value) if isinstance(value, str) else value
        return value
    return answer


def _rule_saw(exc):
    """What the rule that refused a record saw, for the builder: a check written backwards (“is_empty(asset_tag)” for “capital
    items need an asset tag”) refuses records that have just what it asks for."""
    if not getattr(exc, "rule", None):
        return ""
    table, check, values = exc.rule
    saw = ", ".join(f"{k} = {'empty' if v in (None, '', []) else json.dumps(v, default=str)}" for k, v in values.items())
    return (f" (the rule on {table} checks `{check}`, which must come out true for a record to be saved; it came out false"
            + (f" with {saw}" if saw else "") + ". If those values should be allowed, the check is written backwards: fix the rule)")


def _play(trial, engine, example, names, out):
    last_run = db.session.query(db.func.max(Run.id)).filter(Run.workspace_id == trial.ws_id).scalar() or 0
    steps, by_hand = _not_by_hand(engine, example)
    if by_hand:
        out["by_hand"] = by_hand
    for n, step in enumerate(steps, start=1):
        said = _step_words(step)
        try:
            _act(trial, engine, step, names)
        except ExampleWrong as exc:
            out.update(problem=f"step {n} ({said}): {exc}", kind="example")
            return out
        except FieldErrors as exc:
            if step.get("refused"):
                engine.reset()
                continue
            reason = "; ".join(exc.errors.values())
            shapes = {f.get("pattern_message") for x in engine.tables.values() for f in x["fields"] if f.get("pattern_message")}
            left_out = all(OMITTED.match(str(e)) or BAD_VALUE.search(str(e)) or str(e) in shapes for e in exc.errors.values())
            out.update(problem=f"step {n} ({said}) was refused: {reason}" + (" (correct the example's values)" if left_out else "")
                       + _rule_saw(exc),
                       kind="example" if left_out else "app", plain=f"{_plain_step(engine, step, names)} wasn't allowed: {reason}")
            return out
        except Exception as exc:  # noqa: BLE001 - a fault of ours: not the app's design, not the example's
            current_app.logger.warning("Example %r crashed at step %s: %s", out["title"], n, exc, exc_info=True)
            db.session.rollback()
            out.update(problem=f"step {n} ({said}): {type(exc).__name__}", kind="crash")
            return out
        engine.reset()
        if step.get("refused"):
            out.update(problem=f"step {n} ({said}) should have been refused, but the app allowed it", kind="app",
                       plain=f"{_plain_step(engine, step, names)} should have been stopped, but wasn't")
            return out
    stopped = failed_runs(engine, last_run)
    for c in example.get("expect") or []:
        try:
            ok, plain, detail = _expect(engine, c, names)
        except ExampleWrong as exc:
            out.update(problem=f"an expectation: {exc}", kind="example")
            return out
        out["lines"].append((ok, plain))
        if not ok:
            out["detail"].append(detail)
    out["ok"] = bool(out["lines"]) and all(ok for ok, _ in out["lines"]) and not stopped
    if not out["ok"]:
        out["kind"] = "app"
        out["problem"] = "; ".join(out["detail"] + [i["text"] for i in stopped]) or "it expected nothing"
        out["plain"] = "; ".join([p for ok, p in out["lines"] if not ok] + [i["plain"] for i in stopped])
        if by_hand:
            out["problem"] += (f" (the example's own steps added {', '.join(by_hand)} by hand after it happened; they were left out, "
                               f"since the app has to do that itself)")
            out["plain"] += f" (the app didn't add the {', '.join(schema.lc(x) for x in by_hand)} itself)"
    return out


def _not_by_hand(engine, example):
    """An example's steps without those that, after what happens in it (a move, a button), add by hand the records its
    expectations count up, when its title doesn't say a person adds them: stock movements taken out by hand after a
    build starts, then checked as the stock the start "takes out", prove nothing. Played without them, the example shows
    whether the app does it itself; "recording a $500 payment leaves $369 owing" keeps its payment. Returns (steps, the
    labels of what was left out)."""
    steps = list(example.get("steps") or [])
    first = next((i for i, s in enumerate(steps) if isinstance(s, dict) and s.get("do") in ("move", "press")), None)
    if first is None:
        return steps, []
    table = lambda name: engine.tables.get(str(name or "")) or next((t for t in engine.tables.values()  # noqa: E731
                                                                     if str(name or "").lower() in (t["label"].lower(), t["singular"].lower())), None)
    made = {s.get("as"): table(s.get("table")) for s in steps if isinstance(s, dict) and s.get("do") == "add" and s.get("as")}
    counted = set()
    for c in example.get("expect") or []:
        if c.get("table") and c.get("count") is not None and table(c["table"]):
            counted.add(table(c["table"])["key"])
        t, f = made.get(c.get("record")), None
        f = schema.field(t, c.get("field") or "") if t else None
        if f is None:
            continue
        sums = [f] + [g for g in t["fields"] if g["type"] == "rollup" and f["type"] == "formula" and re.search(rf"\b{g['key']}\b", f.get("expression", ""))]
        counted |= {g["table"] for g in sums if g["type"] == "rollup" and g.get("table")}
    title = set(re.findall(r"[a-z]+", str(example.get("title") or "").lower()))
    said = lambda t: all(any(w.startswith(word[:5]) for w in title) for word in re.findall(r"[a-z]+", t["singular"].lower()))  # noqa: E731
    out, dropped = steps[:first + 1], []
    for s in steps[first + 1:]:
        t = table(s.get("table")) if isinstance(s, dict) and s.get("do") == "add" else None
        if t is not None and t["key"] in counted and not said(t):
            dropped.append(t["label"])
            continue
        out.append(s)
    return out, list(dict.fromkeys(dropped))


def _plain_step(engine, step, names):
    """A step as the owner would say it: "Adding an invoice", "Moving the refund to Approved" (never its aliases or keys)."""
    do = step.get("do")
    t = engine.tables.get(str(step.get("table") or "")) or next((t for t in engine.tables.values() if str(step.get("table") or "").lower()
                                                                in (t["label"].lower(), t["singular"].lower())), None)
    record = engine.get(names[step["record"]]) if step.get("record") in names else None
    one = (t or engine.tables.get(record.table if record is not None else "") or {"singular": "record"})["singular"].lower()
    if do == "add":
        return f"Adding {'an' if one[:1] in 'aeiou' else 'a'} {one}"
    if do == "move":
        return f"Moving the {one} to {step.get('to')}"
    if do == "press":
        return f"Pressing “{step.get('button')}” on the {one}"
    if do in ("approve", "reject"):
        return f"{'Approving' if do == 'approve' else 'Saying no to'} the {one}"
    if do == "change":
        return f"Changing the {one}"
    return _step_words(step)[:1].upper() + _step_words(step)[1:]


def _step_words(step):
    do = step.get("do")
    if do == "add":
        return f"add a {step.get('table')}" + (f" as {step['as']}" if step.get("as") else "")
    if do == "move":
        return f"move {step.get('record')} to {step.get('to')}"
    if do == "press":
        return f"press {step.get('button')} on {step.get('record')}"
    if do == "run":
        return f"run {step.get('rule') or step.get('button')}"
    if do == "wait":
        return f"wait {step.get('days')} days"
    return f"{do} {step.get('record')}"


def try_out(workspace, examples=(), screens=True):
    """Walk the app and act out each example, each in its own throwaway copy. Returns (walked, results)."""
    with copy_of(workspace) as trial:
        walked = walk(trial, screens=screens)
    results = []
    for example in examples:
        with copy_of(workspace) as trial:
            results.append(play(trial, example))
    for crash in walked["crashes"]:
        current_app.logger.warning("Trying out workspace %s: %s", workspace.id, crash)
    return walked, results
