"""Trying an app out before it's handed over, the way its people would use it.

`copy_of` makes a throwaway copy of the app: its design, settings and people, none of its records, and nothing that
reaches outside (no mailbox is connected, the AI isn't asked, web calls aren't made). In a copy, `walk` adds a record to
every list, takes each along its stages, presses every button, runs every scheduled rule, makes every document and opens
every screen, noting what went wrong; `play` acts out one of the builder's worked examples ("a quote for 80 guests at
$25 with 15% off") and compares what the app then shows with what the owner would expect. The real app is never touched.

What's found is an issue: {"level": "fix" (it's wrong) or "check" (it may be meant), "text": for the AI, with keys,
"plain": for people}. A fault of the platform's own (a screen that crashes) is a crash: logged, never the AI's to fix.
"""
import logging
import copy
import json
import re
import shutil
import tempfile
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path


from .. import database
from ..blueprint import expr, schema
from ..engine import automations, outside
from ..engine.records import Engine, FieldErrors, NoRate, RecordContext, automations_words, mask, masked, parse_duration, to_number
from ..extensions import db
from ..models import Approval, Membership, Record, Run, User, Workspace, utcnow
from ..core.settings import settings

_log = logging.getLogger(__name__)

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
def copy_of(workspace, blueprint=None):
    """A throwaway copy of the app (see the module's notes), gone when the block ends. `blueprint`: a design to try in
    its place (a candidate the builder hasn't saved)."""
    from .. import create_app
    people = [(m.user_id, m.role, list(m.teams or []), u.name, u.email) for m, u in
              db.session.query(Membership, User).filter(Membership.workspace_id == workspace.id, Membership.user_id == User.id,
                                                        Membership.role != "portal", User.email != "stand-in@example.com")]  # (a copy of a copy has one already)
    named = _looked_up_by_name(blueprint or workspace.blueprint or {})
    kept = [(r.id, r.table, copy.deepcopy(r.data)) for r in Record.query.filter(Record.workspace_id == workspace.id, Record.deleted_at.is_(None),
                                                                                  Record.table.in_(named)).limit(2000)] if named else []
    design = {"name": workspace.name, "about": workspace.about, "blueprint": copy.deepcopy(blueprint or workspace.blueprint or schema.empty(workspace.name)),
              "settings": copy.deepcopy(workspace.settings or {}), "timezone": workspace.timezone, "currency": workspace.currency,
              "version": workspace.version}
    owner_id = workspace.owner_id
    root = Path(settings()["DATA_DIR"]) / "trials"
    root.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(dir=root))
    config = {k: v for k, v in settings().items() if k.isupper()}
    config.update({"TESTING": True, "WORKER": False, "TRYING_OUT": True, "DATA_DIR": folder, "SECRET_KEY": "try-out",
                   "SQLALCHEMY_BINDS": {}, "WTF_CSRF_ENABLED": False, "SQLALCHEMY_ENGINE_OPTIONS": {"pool_size": 2, "max_overflow": 4},
                   "MICROSOFT_CLIENT_ID": "", "MICROSOFT_CLIENT_SECRET": "", "OLLAMA_EMBED_MODEL": "", "SERVER_NAME": None})
    try:
        if settings().get("TRY_IN_POSTGRES"):  # the old way: an empty PostgreSQL database of its own
            place = database.throwaway(settings()["SQLALCHEMY_DATABASE_URI"], "tryout")
            config["SQLALCHEMY_DATABASE_URI"] = place.__enter__()
        else:  # the simulator: the same engine on an in-memory database, made in a fraction of a second
            from .. import sim
            config.update(sim.overrides())
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


def _read_by_rule(t, exc):
    """The fields a table's rule that refused a sample reads, in its check and its "when": the ones a sample may set."""
    table, check, _ = getattr(exc, "rule", None) or (None, None, None)
    rule = next((r for r in t.get("rules", []) if r.get("check") == check), None) if table == t["key"] else None
    if rule is None:
        return []
    keys = list(expr.outer_names(check)) + [c.get("field") for c in schema.condition_leaves(list(rule.get("when") or []) + list(rule.get("holds") or []))]
    stage = (t.get("workflow") or {}).get("field")  # a new one starts at the first stage, whatever the rule says
    return [f for f in (schema.field(t, k) for k in dict.fromkeys(k for k in keys if k))
            if f is not None and f["type"] in schema.STORED and f["type"] != "autonumber" and not f.get("readonly") and f["key"] != stage]


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
                if f is None:  # a rule across fields ("_"): what it reads first left empty where it may be (no pickup
                    # date yet, no cost yet), then other values for it (an Active coach needs a signed waiver: one On
                    # leave), each field a different one (a margin of revenue less cost is never 0 for want of it); its
                    # dates each a little later than the one before, in the order of the form, then the other way round
                    dated = lambda g: engine.kind(g) in ("date", "datetime")  # noqa: E731
                    for i, g in enumerate(_read_by_rule(t, exc)):
                        if dated(g) and attempt:
                            continue
                        if not g.get("required") and attempt == 0 and values.get(g["key"]) not in (None, ""):
                            values.pop(g["key"], None)
                            changed = True
                            continue
                        if dated(g):
                            continue
                        others = _others(engine, g, attempt)
                        n = attempt - (0 if g.get("required") else 1)
                        pick = others[(n + i) % len(others)] if others and n < len(others) else None
                        if pick is not None and values.get(g["key"]) != pick:
                            values[g["key"]] = pick
                            changed = True
                        elif not g.get("required") and values.pop(g["key"], None) is not None:
                            changed = True
                    dates = [g for g in t["fields"] if dated(g) and values.get(g["key"])]
                    for i, g in enumerate(dates if attempt < 2 else dates[::-1]):
                        if attempt:
                            values[g["key"]] = (engine.today + timedelta(days=7 + i)).isoformat()
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
                if not automations.button_held(engine, a, engine.get(record.id)):  # one not offered yet isn't pressed
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
            expr.evaluate(schema.percents_fixed(t, f["expression"], engine.tables), RecordContext(engine, record))
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
                          f"{schema.a_one(t['singular'], start=True)}’s {off_f['label']} came out bigger than its subtotal ({facts})", t["key"]))
    if tax_f and tax is not None and tax > 0.35 * max(subtotal - off, 0.01):
        out.append(_issue("check", f"On a sample {t['key']} record ({facts}) {tax_f['key']} is more than a third of the price",
                          f"{schema.a_one(t['singular'], start=True)}’s {tax_f['label']} looks too high ({facts})", t["key"]))
    near = lambda a, b: abs(a - b) <= 0.02  # noqa: E731
    if tax_f and total_f and tax is not None and total is not None and not (near(total, subtotal - off + tax) or near(total, subtotal - off)):
        # the total is neither the price plus its tax nor a price that includes it
        why = ("leaves out the discount" if off and near(total, subtotal + tax) else
               "counts the tax twice" if near(total, subtotal - off + 2 * tax) else "doesn't add up")
        out.append(_issue("check", f"On a sample {t['key']} record ({facts}) {total_f['key']} {why}: it should be {sub_f['key']}"
                                   f"{' - ' + off_f['key'] if off_f else ''} + {tax_f['key']} (or the price itself, if prices include tax)",
                          f"{schema.a_one(t['singular'], start=True)}’s total {why} ({facts})", t["key"]))
    return out


def _flow_moves_on(engine, t, record, field, later):
    """Whether a flow done on the record here (Receive the delivery, while it's Sent) can set one of the stages after it."""
    return any(f["table"] == t["key"] and f["start"] == "record" and (not f.get("when") or engine.matches(record, f["when"]))
               and any(s["kind"] == "decide" and s.get("field") == field and any(o["value"] in later for o in s["options"]) for s in f["steps"])
               for f in schema.flows(engine.bp))


def _rule_moves_on(engine, t, field, at, later):
    """A rule (not a button's) moves records of this list on from this stage by itself: a payment marking it paid, a
    day passing marking it overdue, a chase escalating it. Waiting at it isn't being stuck."""
    for a in engine.bp.get("automations", []):
        if a["trigger"]["type"] == "button":
            continue
        for x in schema.steps_of(a["actions"]):
            if x.get("type") != "update_record" or str((x.get("values") or {}).get(field, "")) not in later:
                continue
            own = a["trigger"].get("table") == t["key"] and not x.get("target")
            linked = x.get("target") and (engine.tables.get(a["trigger"].get("table")) or {}) and \
                any(f.get("type") == "link" and f.get("table") == t["key"] and f["key"] == str(x["target"]).split(".")[0]
                    for f in engine.tables[a["trigger"]["table"]]["fields"])
            if own or linked:
                return True
    return False


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
                    and not _flow_moves_on(engine, t, record, flow["field"], later) \
                    and not _rule_moves_on(engine, t, flow["field"], at, later):
                stops = [m.get("label") or m["to"] for _, m in moves]
                pressed = next((a for a in engine.bp.get("automations", []) if a["trigger"]["type"] == "button"
                                and a["trigger"].get("table") == t["key"] and any(str((x.get("values") or {}).get(flow["field"], "")) in later
                                                                                  for x in schema.steps_of(a["actions"]) if x.get("type") == "update_record")), None)
                instead = (f"; the button rule {pressed['key']} sets the stage instead, which skips the stage's own move and what it "
                           f"checks (who may, what must be filled in): make it a workflow move from {at}, and the rule's other steps a "
                           f"rule on that move" if pressed else "")
                out.append(_issue("fix", f"A {t['key']} record at {at} can't move on to {', '.join(later)}: the workflow has no move from "
                                         f"{at}" + (f" but {', '.join(stops)}" if stops else "") + instead,
                                  f"{schema.a_one(t['singular'], start=True)} at {at} can't go on to {' or '.join(later[:2])}: "
                                  + (f"the only way out is {' or '.join(stops)}" if stops else "nothing moves it on")
                                  + (f" (“{pressed['name']}” sets it with a rule, skipping the stage's own checks)" if pressed else ""), t["key"]))
            if not moves:
                break
            i, m = moves[0]
            try:
                _attach(engine, record, t, m.get("requires") or [])  # a signature or a file it needs, as the person would
                record = engine.get(record.id)
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
    # the App map draws the whole design: the map, what each list keeps, and each diagram in it too
    paths += [f"/w/{wid}/app/studio", f"/w/{wid}/app/studio?view=data", f"/w/{wid}/app/studio/map.json"]
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


def _followed(engine, names, alias):
    """A record named through another's links, as a formula reads it: j1.invoices (the latest invoice of job j1, made by a
    rule), i1.customer (that invoice's customer). None when the path doesn't lead to one."""
    from ..engine.records import RecordProxy
    head, *rest = alias.split(".")
    found = engine.get(names[head]) if head in names else None
    for part in rest:
        if found is None:
            return None
        got = RecordProxy(engine, found).field(part.strip())
        if isinstance(got, (expr.Many, expr.Lazy, list)):
            items = [x for x in got if isinstance(x, RecordProxy)]
            found = max(items, key=lambda x: x.record.id).record if items else None
        elif isinstance(got, RecordProxy):
            found = got.record
        else:
            return None
    return found


def _record(engine, names, alias):
    if isinstance(alias, str) and "." in alias and alias.split(".")[0] in names:  # through its links: j1.invoices
        engine.reset()
        found = _followed(engine, names, alias)
        if found is None:
            raise ExampleWrong(f"“{alias}” leads to no record (no {alias.split('.')[-1]} of {alias.split('.')[0]} yet)")
        names[alias] = found.id
        return found
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
    named = _titled(engine, names, alias, table)
    if named is not None:  # a record's own name or number (the "Wages" account, ORD-1): that record, not a guess
        names[alias] = named
        return alias
    added = names.get("_added", [])
    head = alias.strip().lower().split("_")[0].rstrip("0123456789")
    like = lambda key: key.startswith(head) or "".join(w[:1] for w in key.split("_")) == head  # noqa: E731  ("il" for invoice_lines)
    fits = [(key, rid) for key, rid in reversed(added) if key == table] if table else \
        sorted([(key, rid) for key, rid in reversed(added) if head and like(key)],
               key=lambda x: bool(engine.tables.get(x[0], {}).get("embedded")))
    counted = re.fullmatch(r"[a-z]+?_?(\d+)", alias.strip().lower())
    if fits and counted:  # "cr2": the second crew member the example added, not the latest (of the table it's read as
        # naming, as before: the latest added of those its letters fit, "d1" the delivery just added, not the driver)
        n, best = int(counted.group(1)), table or fits[0][0]
        mine = [rid for key, rid in added if key == best]
        if 0 < n <= len(mine):
            fits = [(best, mine[n - 1])]
    if not fits and head:  # one the app made itself (the job an accepted quote became)
        engine.reset()
        made = [(not engine.tables[key].get("embedded"), r.id, key) for key in engine.tables
                if (table and key == table) or (not table and like(key)) for r in engine.rows(key)]
        fits = [(key, rid) for _, rid, key in sorted(made, reverse=True)]  # a main record before a line, the latest first
    if fits and not any(alias.strip().lower() == engine.title(engine.get(rid)).lower() for _, rid in added if engine.get(rid)):
        names[alias] = fits[0][1]
    return alias


def _number(text):
    """("ord", 1) for "ORD-1", "ord-0001" or "ORD 1": a number as people write it, whatever its zeros."""
    found = re.fullmatch(r"([a-z]*)[\s#_-]*0*(\d+)", str(text or "").strip().lower())
    return (found.group(1), int(found.group(2))) if found else None


def _titled(engine, names, alias, table=None):
    """The record an example names by what it's called or its number ("Ethiopia Yirgacheffe", "ORD-1" for ORD-0001),
    as a person points at the one in front of them: the latest the example added, else the latest the app has."""
    wanted, number = alias.strip().lower(), _number(alias)
    keys = [table] if table else list(engine.tables)

    def called(r):
        if engine.title(r).strip().lower() == wanted:
            return True
        t = engine.tables.get(r.table) or {}
        return number is not None and any(f["type"] == "autonumber" and _number((r.data or {}).get(f["key"])) == number
                                          for f in t.get("fields", []))

    for key, rid in reversed(names.get("_added", [])):
        r = engine.get(rid)
        if r is not None and key in keys and called(r):
            return rid
    found = [r for key in keys if key in engine.tables for r in engine.rows(key) if called(r)]
    return found[-1].id if found else None


def _the_one_meant(engine, step, names):
    """A step that doesn't say which record (“press confirm_booking”): the latest one the example added of the table it's
    about (the button's, the stage's, the one with the fields it changes), as a person presses it on the record in front
    of them; a main record before a line."""
    do, tables = step.get("do"), []
    if do == "press":
        wanted = str(step.get("button") or "").strip().lower()
        tables = [a["trigger"].get("table") for a in engine.bp.get("automations", []) if a["trigger"]["type"] == "button"
                  and wanted in (a["key"].lower(), a["name"].lower(), str(a["trigger"].get("label") or "").lower())]
    elif do == "move":
        wanted = str(step.get("to") or "").strip().lower()
        tables = [t["key"] for t in engine.tables.values() if any(x.lower() == wanted for x in schema.stages(t))]
    elif do == "change":
        keys = set(step.get("values") or {})
        tables = [t["key"] for t in engine.tables.values() if keys and keys <= {f["key"] for f in t["fields"]}]
    added = [(key, rid) for key, rid in reversed(names.get("_added", [])) if not tables or key in tables]
    if not added and tables:  # none the example added: the one the app made itself (the invoice a finished job became)
        engine.reset()
        added = [(key, r.id) for _, r, key in sorted(((r.id, r, key) for key in dict.fromkeys(tables) if key in engine.tables
                                                      for r in engine.rows(key)), key=lambda x: x[0], reverse=True)]
    main = [x for x in added if not engine.tables.get(x[0], {}).get("embedded")] or added
    return main[0][1] if main else None


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
                if isinstance(v, str) and v not in names and "." in v and v.split(".")[0] in names:  # s1.bids: the bid the app made for s1
                    engine.reset()
                    found = _followed(engine, names, v)
                    if found is None:
                        raise ExampleWrong(f"“{v}” leads to no record (no {v.split('.')[-1]} of {v.split('.')[0]} yet)")
                    names[v] = found.id
                elif isinstance(v, str) and v not in names:
                    _guess(engine, names, v, f["table"])
            ids = [names.get(v, v) if isinstance(v, str) else v for v in many]
            value = ids if f.get("multiple") else ids[0]
        elif engine.kind(f) in ("date", "datetime") and isinstance(value, str) and RELATIVE.match(value.strip()):
            # "today+3": a date counted from the day the example is acted out (a check kept for later stays true)
            n = int((RELATIVE.match(value.strip()).group(1) or "0").replace(" ", ""))
            value = (date.today() + timedelta(days=n)).isoformat()
            if engine.kind(f) == "datetime":
                value += " 10:00"
        elif engine.kind(f) in ("date", "datetime") and (names.get("_waited") or names.get("_moved")):  # a date on the
            # calendar, after days passed (and on the day the example was written, moved on to today's)
            value = _shifted(engine, f, value, names.get("_moved", 0) - names.get("_waited", 0))
        elif f["type"] == "user":  # a record's name ("s1", the staff member Sam) means that person
            many = [engine.title(engine.get(names[v])) if isinstance(v, str) and isinstance(names.get(v), int) and engine.get(names[v])
                    else v for v in (value if isinstance(value, list) else [value])]
            value = [_person(engine, v, make=True) for v in many] if isinstance(value, list) else _person(engine, many[0], make=True)
        out[f["key"]] = value
    return out


RELATIVE = re.compile(r"^today\s*([+-]\s*\d+)?$", re.I)
ANOTHER = re.compile(r"^(?:another|a second|second|the other|other)\s+(.+)$|^(.+?)\s+(\d+)$", re.I)
STAFF = re.compile(r"^(an? )?((ordinary|regular) )?(staff( member)?|member of staff|employee|worker|team member|someone else)$", re.I)
ROLES_BY = {"admin": "admin", "an admin": "admin", "editor": "editor", "viewer": "viewer"}


def _doer(trial, engine, by, names=None):
    """Who does a step done "by" someone other than the owner: an ordinary staff member, someone in a team (its key or
    name), an admin, a person by name (a stand-in joins, as the owner would invite them), or an outside person (the
    example's name for a seller, or one of its people: an Outsider). None: the owner."""
    text = str(by or "").strip()
    if not text or text.lower() in ("me", "owner", "the owner", "i", "admin@example.com"):
        return None
    if names is not None and text in names:
        found = _outsider(trial, engine, text, names)
        if found is not None:
            return found
    nth = ANOTHER.match(text)  # four eyes: "another Verifier", "a second verifier", "Verifier 2" is someone else in the team
    named, n = (nth.group(1) or nth.group(2), 2 if nth.group(1) else int(nth.group(3))) if nth else (text, 1)
    key = schema.slug(named)
    team = next((tm for tm in schema.teams(engine.bp) if key in (tm["key"], schema.slug(tm.get("label") or ""), schema.slug(tm.get("name") or ""))), None)
    if team is None and nth:
        key, n = schema.slug(text), 1
    if team is not None:
        uid = _person(engine, f"{team.get('label') or team.get('name') or team['key']} member" + (f" {n}" if n > 1 else ""), make=True)
        member = trial.member(uid)
        member.teams = sorted(set(member.teams or []) | {team["key"]})
        db.session.commit()
    elif text.lower() in ROLES_BY:
        uid = _person(engine, ROLES_BY[text.lower()].capitalize(), make=True)
        trial.member(uid).role = ROLES_BY[text.lower()]
        db.session.commit()
    else:
        found = _outsider(trial, engine, text, names) if names is not None and not STAFF.match(text) else None
        if found is not None:
            return found
        uid = _person(engine, "Staff member" if STAFF.match(text) else text, make=True)
    return None if uid == engine.user_id else trial.engine(uid)


class Outsider:
    """An outside person a step is done by (a seller's person submitting their bid, a client accepting a quote): they act
    through what the portal lets them do (app/engine/outside.py), never as staff."""

    def __init__(self, trial, member, name):
        self.engine, self.portal, self.contact = outside.enter(trial.ws, member, name)
        self.user_id, self.by = member.user_id, f"{name} (through the portal)"


def _portal_kin(engine):
    """{table: (the portal's people table, their link to it, its link back to them)}: a portal's people, and the records
    they're people of (the seller a seller's person signs in for)."""
    out = {}
    for p in schema.portals(engine.bp):
        people = engine.tables.get(p["table"])
        if people is None or not p.get("enabled", True):
            continue
        out[people["key"]] = (people["key"], None, None)
        for f in people["fields"]:
            if f["type"] == "link" and f["table"] in engine.tables and f["table"] not in out:
                back = next((g for g in engine.tables[f["table"]]["fields"] if g["type"] == "link" and g["table"] == people["key"]), None)
                out[f["table"]] = (people["key"], f, back)
    return out


def _ids(value):
    return [int(x) for x in (value if isinstance(value, list) else [value]) if str(x).isdigit()]


def _person_of(engine, record, people_key, to_it, back):
    """One of a record's outside people (a seller's person, who signs in for them): one it has, or a stand-in who joins
    its people, as the business would invite one."""
    system = engine.system()
    ids = _ids((record.data or {}).get(back["key"])) if back is not None else []
    if to_it is not None:
        ids += [r.id for r in system.rows(people_key) if record.id in _ids((r.data or {}).get(to_it["key"]))]
    found = next((x for x in (system.get(i, people_key) for i in ids) if x is not None), None)
    if found is not None:
        return found
    portal, t, title = schema.portal_of(engine.bp, people_key), engine.tables[people_key], engine.title(record)
    values = {f["key"]: f"{title} contact" for f in t["fields"] if f.get("required") and f["type"] == "text"}
    if portal.get("email_field"):
        values[portal["email_field"]] = re.sub(r"\W+", ".", title.lower()).strip(".") + ".contact@example.com"
    if to_it is not None:
        values[to_it["key"]] = [record.id] if to_it.get("multiple") else record.id
    person = system.create(people_key, values, automations=False, ai=False)
    if back is not None:
        had = _ids((record.data or {}).get(back["key"]))
        system.update(system.get(record.id), {back["key"]: had + [person.id] if back.get("multiple") else (had[0] if had else person.id)},
                      automations=False)
    return person


def _outsider(trial, engine, text, names):
    """The outside person a step's "by" names (the example's name for their record, or its title), signed in to the
    portal as if invited from their record; None when it names no one outside."""
    kin = _portal_kin(engine)
    if not kin:
        return None
    record = None
    if isinstance(names.get(text), int):
        record = engine.get(names[text])
        record = record if record is not None and record.table in kin else None
    elif text not in names:
        record = next((r for key in kin for r in engine.rows(key) if str(engine.title(r)).strip().lower() == text.lower()), None)
    if record is None:
        return None
    people_key, to_it, back = kin[record.table]
    contact = record if record.table == people_key else _person_of(engine, record, people_key, to_it, back)
    member = _portal_account(trial, engine, contact)
    for colleague in _colleagues(engine, contact, kin):  # the rest of their organisation's people are signed up too (a
        _portal_account(trial, engine, colleague)       # change one makes waits for another of them)
    user = db.session.get(User, member.user_id)
    return Outsider(trial, member, user.name or str(engine.title(contact)))


def _colleagues(engine, contact, kin):
    """The other outside people of the record theirs hang off (the seller's other people), those with an email."""
    portal = schema.portal_of(engine.bp, contact.table)
    out = []
    for table, (people_key, to_it, back) in kin.items():
        if people_key != contact.table or to_it is None:
            continue
        mine = set(_ids((contact.data or {}).get(to_it["key"])))
        out += [r for r in engine.rows(people_key) if r.id != contact.id and mine & set(_ids((r.data or {}).get(to_it["key"])))
                and (r.data or {}).get(portal.get("email_field") or "")]
    return out


def _portal_account(trial, engine, contact):
    """Their portal sign-in, as if invited from their record: made the first time it's needed."""
    member = Membership.query.filter_by(workspace_id=trial.ws_id, role="portal", contact_id=contact.id).first()
    if member is None:
        portal = schema.portal_of(engine.bp, contact.table)
        email = str((contact.data or {}).get(portal.get("email_field") or "") or "").strip().lower()
        user = User.query.filter_by(email=email).first() if email else None
        if user is not None and Membership.query.filter_by(workspace_id=trial.ws_id, user_id=user.id).first() is not None:
            user, email = None, f"portal.{contact.id}.{email}"  # (one of the business's own people: a sign-in of its own)
        if user is None:
            user = User(email=email or f"portal.{contact.id}@example.com", name=str(engine.title(contact)))
            user.password_hash = "!"  # nobody signs in as them
            db.session.add(user)
            db.session.flush()
        member = Membership(workspace_id=trial.ws_id, user_id=user.id, role="portal", contact_id=contact.id)
        db.session.add(member)
        db.session.commit()
        engine._members = None
    return member


def _act_outside(trial, doer, step, names, hold=False):
    """A step done by an outside person: what the portal lets them do, or refused as it would be there."""
    engine, contact, do = doer.engine, doer.contact, step.get("do")
    try:
        if do == "add":
            t = _table(engine, step.get("table"))
            names["_adding"] = step.get("as")
            try:
                given = _values(engine, t, step.get("values"), names)
            finally:
                names.pop("_adding", None)
            record = outside.add_line(engine, contact, t["key"], given) if t.get("embedded") else \
                outside.add(engine, doer.portal, contact, t["key"], given, automations=not hold)
            if step.get("as"):
                names[step["as"]] = record.id
            names.setdefault("_added", []).append((t["key"], record.id))
            names.setdefault("_given", {}).setdefault(record.id, set()).update(given)
            return
        alias = step.get("record") or (step.get("as") if step.get("as") in names else None)
        if alias in (None, ""):
            found = _the_one_meant(engine, step, names)
            if found is not None:
                alias, names["_this"] = "_this", found
        record = _record(engine, names, alias)
        t = engine.tables[record.table]
        if not t.get("embedded"):
            outside.find(engine, contact, t["key"], record.id)
        if do == "change":
            values = _values(engine, t, step.get("values"), names)
            outside.change(engine, contact, t, record, values)
            names.setdefault("_given", {}).setdefault(record.id, set()).update(values)
            return
        if do == "press":  # a stage's button (Submit the bid): the move it makes; nothing else is pressed in the portal
            wanted = str(step.get("button") or "").strip().lower()
            move = next((m for m in (t.get("workflow") or {}).get("transitions") or [] if str(m.get("label") or "").strip().lower() == wanted), None)
            if move is None:
                raise FieldErrors({"_": f"there's no “{step.get('button')}” button for {doer.by}"})
            step, do = {**step, "do": "move", "to": move["to"]}, "move"
        if do == "move":
            if step.get("values"):
                outside.change(engine, contact, t, record, _values(engine, t, step["values"], names))
                engine.reset()
                record = engine.get(record.id)
            wanted = str(step.get("to") or "").strip().lower()
            offered = [(i, problem) for i, m, problem in engine.moves(record) if m["to"].lower() == wanted]
            index = next((i for i, problem in offered if not problem), None)
            if index is None:
                at = (record.data or {}).get((t.get("workflow") or {}).get("field"))
                why = next((problem for _, problem in offered if problem), None)
                raise FieldErrors({"_": why or f"{doer.by} can't move a {schema.lc(t['singular'])} at {at} to {step.get('to')}"})
            outside.move(engine, contact, t, record, index, by=doer.by)
            return
        if do in ("approve", "reject"):  # what was sent to them (sign the contract), or a colleague's change of theirs
            asks = Approval.query.filter_by(workspace_id=trial.ws_id, record_id=record.id, status="pending",
                                            assignee_id=doer.user_id).order_by(Approval.id).all()
            if not asks:
                raise FieldErrors({"_": f"nothing on this {schema.lc(t['singular'])} waits for {doer.by}'s answer"})
            said = step.get("comment") or step.get("reason")
            if asks[0].kind == "task":  # what it asks them for (who signed), as they'd type it
                given = {k: v for k, v in (step.get("values") or {}).items() if k in (asks[0].fields or [])}
                automations.complete(engine, asks[0], doer.by, given, comment=said)
            elif asks[0].kind == "change":
                automations.decide_change(engine, asks[0], do == "approve", doer.by, said)
            else:
                automations.decide(engine, asks[0], do == "approve", doer.by, said)
            return
        raise ExampleWrong(f"“{do}” isn't a step an outside person takes (add, change, move, press, approve or reject)")
    except outside.NotTheirs:
        raise FieldErrors({"_": f"that isn't open to {doer.by} in the portal"}) from None


def _act(trial, engine, step, names, hold=False):
    """Do one step of an example as whoever would. Raises ExampleWrong or FieldErrors (the app refused)."""
    do = step.get("do")
    doer = _doer(trial, engine, step.get("by"), names) if do not in ("wait", "run") else None
    if isinstance(doer, Outsider):
        return _act_outside(trial, doer, step, names, hold)
    if doer is not None and do == "add":
        engine = doer
    if do == "add":
        t = _table(engine, step.get("table"))
        names["_adding"] = step.get("as")
        try:
            given = _values(engine, t, step.get("values"), names)
            record = engine.create(t["key"], given, ai=False, automations=not hold)
        finally:
            names.pop("_adding", None)
        if step.get("as"):
            names[step["as"]] = record.id
        names.setdefault("_added", []).append((t["key"], record.id))
        names.setdefault("_given", {}).setdefault(record.id, set()).update(given)  # what the example says, never filled over
        return
    if do == "wait" and step.get("until"):  # until a record's date has gone by: one worked out (due 14 days after it's issued) can't be written in
        _wait_until(engine, names, step)
        return
    if do == "wait":  # days pass, one by one: the app's daily rules run each day, reminders fall due, things get overdue
        said = to_number(step.get("days"))
        days = 1 if said is None else int(said)
        if days <= 0:  # no time passes: today's scheduled rules run, as they will today
            _days_rules(engine, names)
            return
        for _ in range(days):
            pass_time(engine, 1)
            names["_waited"] = names.get("_waited", 0) + 1
            _days_rules(engine, names)
        return
    if do == "run":  # a scheduled rule, as if its time had come (it already ran today if a wait brought today round)
        wanted = str(step.get("rule") or step.get("button") or "").strip().lower()
        a = next((a for a in engine.bp.get("automations", []) if wanted in (a["key"].lower(), a["name"].lower())), None)
        if a is None:
            raise ExampleWrong(f"there's no automation “{wanted}” (automations: {', '.join(a['key'] for a in engine.bp.get('automations', []))})")
        if a["trigger"]["type"] == "button":
            raise ExampleWrong(f"{a['key']} is a button: “press” it on the record (button: {a['trigger'].get('label') or a['name']})")
        if a["trigger"]["type"] != "schedule":  # it ran by itself when its moment came: nothing more to do
            return
        if (a["key"], names.get("_waited", 0)) not in names.setdefault("_ran", set()):
            names["_ran"].add((a["key"], names.get("_waited", 0)))
            automations.run_schedule(engine, a, "Tried out")
        return
    alias = step.get("record") or (step.get("as") if step.get("as") in names else None)
    if alias in (None, ""):  # it doesn't say which: the one in front of them
        found = _the_one_meant(engine, step, names)
        if found is not None:
            alias, names["_this"] = "_this", found
    record = _record(engine, names, alias)
    t = engine.tables[record.table]
    if doer is not None:  # done by someone else, as they'd see it
        theirs = doer.get(record.id)
        if theirs is None:
            raise FieldErrors({"_": f"{doer.by} can't see this {schema.lc(t['singular'])}"})
        engine, record = doer, theirs
    if do == "change":
        values = _values(engine, t, step.get("values"), names)
        if step.get("refused") and values and all(_unchanged(engine, t, record, k, v) for k, v in values.items()):
            raise ExampleWrong(f"the change it expects to be refused sets {', '.join(values)} to what {record.table} already has, so "
                               f"nothing would change: give it a different value")
        if step.get("refused") and engine.locked(record) is None:  # the owner may change what's locked: it's kept from staff
            staff = trial.engine(_person(engine, "Staff member", make=True))
            theirs = staff.get(record.id) or record  # kept from their view (another rep's customer), it's as locked to them
            if staff.locked(theirs):
                staff.update(theirs, values)
                return
        engine.update(record, values)
        names.setdefault("_given", {}).setdefault(record.id, set()).update(values)
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
        given = names.setdefault("_given", {}).setdefault(record.id, set())
        if step.get("values"):  # filled in on the button's form
            filled = _values(engine, t, step["values"], names)
            engine.update(record, filled)
            given.update(filled)
            engine.reset()
            record = engine.get(record.id)
        if not step.get("refused"):  # what its button asks for, filled in as the person would (a stop for it left empty is
            # shown with "refused")
            _fill_asked(trial, engine, record, t, lambda m: m["to"].lower() == wanted, given, names["_given"])
            record = engine.get(record.id)
        if not _press_move(engine, record, t, lambda m: m["to"].lower() == wanted, attach=not step.get("refused")):
            _take_along(engine, record, t, step.get("to"), trial, last=not step.get("refused"), given=given, others=names["_given"])
    elif do == "press":
        wanted = str(step.get("button") or "").strip().lower()
        a = next((a for a in engine.bp.get("automations", []) if a["trigger"]["type"] == "button"
                  and wanted in (a["key"].lower(), a["name"].lower(), str(a["trigger"].get("label") or "").lower())), None)
        doc = next((d for d in t.get("documents", []) if wanted in (d["key"].lower(), str(d.get("label") or "").lower())), None)
        move = next((m for m in (t.get("workflow") or {}).get("transitions") or [] if str(m.get("label") or "").strip().lower() == wanted),
                    None) if a is None and doc is None else None
        if move is not None:  # a stage's button (“Send delivery note”), pressed as the person would: the move it makes
            shown = [m for _, m, _ in engine.moves(record) if str(m.get("label") or "").strip().lower() == wanted]
            if step.get("refused") and not shown:  # not on the record at its stage: the person can't press it, so it's
                # stopped as it stands (Send on a Draft quote, before its Approve), not taken along the stages between
                at = (record.data or {}).get(t["workflow"]["field"])
                now = [m.get("label") or m["to"] for _, m, _ in engine.moves(record)]
                raise FieldErrors({"_": f"there's no “{step.get('button')}” button on a {schema.lc(t['singular'])} at {at}"
                                        + (f" (its buttons now: {', '.join(now)})" if now else "")})
            _act(trial, engine, {**step, "do": "move", "to": move["to"]}, names, hold)
            return
        if a is None and doc is not None:  # its document's button (Make the invoice PDF), as the person would press it
            from ..documents import produce
            produce(engine, record, doc["key"], by=engine.by)
            return
        if a is None:
            buttons = [a["key"] for a in engine.bp.get("automations", []) if a["trigger"]["type"] == "button"]
            raise ExampleWrong(f"there's no button “{step.get('button')}” (buttons: {', '.join(buttons) or 'none'}"
                               + (f"; documents: {', '.join(d['key'] for d in t.get('documents', []))}" if t.get("documents") else "") + ")")
        automations.press(engine, a, record, engine.by)
    elif do in ("approve", "reject"):
        waiting = Approval.query.filter(Approval.workspace_id == trial.ws_id, Approval.record_id == record.id,
                                        Approval.status == "pending").order_by(Approval.id).all()
        if not waiting:  # its own Approve button (a move to Approved, the client's Sign off), as the person would press it
            words = APPROVING if do == "approve" else REJECTING
            if t.get("workflow") and _press_move(engine, record, t, lambda m: words.search(f"{m.get('label') or ''} {m['to']}")):
                return
            if do == "approve" and t.get("workflow"):  # the approver lets it through: the move only they may make (an
                # approval kept as who may make a move, not as a request to answer)
                theirs = [m for _, m, problem in engine.moves(record) if m.get("who") and not problem]
                if theirs and _press_move(engine, record, t, lambda m: m in theirs):
                    return
                # the approver's own move is there at this stage, but not for this person: the app stops them (“only I can
                # sign off”, tried by a member of staff), it isn't an example asking for an OK nobody asks for
                at = (record.data or {}).get(t["workflow"]["field"])
                kept = [m for m in t["workflow"].get("transitions") or [] if m.get("who") and at in (m.get("from") or [at])
                        and not schema.says_no(m["to"])]
                if kept:
                    who = " or ".join(dict.fromkeys(w for m in kept for w in m["who"]))
                    raise FieldErrors({"_": f"only the {who} may {(kept[0].get('label') or 'move it to ' + kept[0]['to']).lower()} a "
                                            f"{schema.lc(t['singular'])} at {at}"})
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
        if doer is not None:  # they answer it, or the app says they can't
            if not automations.may_decide(approval, trial.member(doer.user_id), doer.user_id, record, doer):
                raise FieldErrors({"_": f"{doer.by} can't answer “{approval.message}”"})
            people = [doer.user_id]
        who = next((uid for uid in people if uid and automations.may_decide(approval, trial.member(uid), uid, record, trial.engine(uid))), None)
        if who is None:
            why = automations.kept_from(trial.engine(trial.owner_id), approval, record)
            raise FieldErrors({"_": f"nobody could answer “{approval.message}”" + (f"; not the owner either: {why}" if why else "")})
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


def _unchanged(engine, t, record, key, value):
    f = schema.field(t, key)
    try:
        value = engine.coerce(f, value) if f else value
    except ValueError:
        return False
    return str(value if value is not None else "") == str((record.data or {}).get(key) if (record.data or {}).get(key) is not None else "")


def _take_along(engine, record, t, target, trial=None, last=True, given=(), others=None):
    """“Move it to Invoiced” from New: taken along the stages between by their buttons, as the person would (a rule may
    carry it on by itself), filling in what each button asks (with `trial`): on the way, always; at the target too
    unless not `last` (a move meant to be refused is tried as it stands). `given`: the fields the example set itself,
    never filled over (`others`: those of every record, by id). Raises FieldErrors where it can't go on."""
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
            if trial is not None and (last or way[0].lower() != str(target).lower()):
                _fill_asked(trial, engine, record, t, lambda m: m["to"].lower() == way[0].lower(), given, others)
                record = engine.get(record.id)
            pick = lambda m: m["to"].lower() == way[0].lower()  # noqa: E731
            try:
                pressed = _press_move(engine, record, t, pick, attach=last or way[0].lower() != str(target).lower())
            except FieldErrors:
                # kept from whoever's doing it (someone else approves what they recommended): a colleague who may, does
                other = _colleague(trial, engine, record, pick)
                if other is None:
                    raise
                pressed = _press_move(other, other.get(record.id), t, pick, attach=last or way[0].lower() != str(target).lower())
        except FieldErrors as exc:
            raise FieldErrors({"_": f"on its way from {began} to {target} (through {', '.join(way[:-1])}), it couldn't move from "
                                    f"{at} to {way[0]}: {'; '.join(exc.errors.values())}"}) from exc
        if not pressed:
            raise FieldErrors({"_": f"on its way from {began} to {target}, there was no button to move it from {at} to {way[0]}"})
    raise FieldErrors({"_": f"it went round its stages without reaching {target}"})


def _fill_asked(trial, engine, record, t, pick, given=(), others=None):
    """Fill in what the button of the first move `pick` likes asks for (the Check out button's Checked out by), as the
    person pressing it would, with ordinary values; and what it waits for (“once the deposit is Received”), unless the
    example set that field itself (`given`): what it says stands, and the move then says if it's stopped. What it waits
    for of a linked record (“once the contract is Approved”) is done to that record first, as the person would (the
    contract taken to Approved by its own buttons), unless the example set it itself (`others`: by record id)."""
    move = next((m for _, m, _ in engine.moves(record) if pick(m)), None)
    if move is None:
        return
    asked = [f for f in engine.to_fill(t, record, move) if engine.kind(f) not in schema.FILES]
    # a link it asks for (a category, a cost centre) is one that's there, or an ordinary one added first
    linked = {f["table"]: got for f in asked if f["type"] == "link" for got in [_one_of(engine, f["table"], trial.owner_id)] if got}
    needs = {f["key"]: v for f in asked for v in [sample_value(engine, t, f, trial.owner_id, linked, 300, 0)] if v is not None}
    if move.get("only_if") and not engine.matches(record, move["only_if"]):  # “once the clearance date is filled in”
        linked = []
        meet = _meeting(trial, engine, record, t, move["only_if"], given, others or {}, linked)
        needs = {**needs, **meet}  # what it waits for wins over an ordinary value
        for other, ot, values in linked:
            try:
                stage = (ot.get("workflow") or {}).get("field")
                if stage in values:
                    _take_along(engine, other, ot, values.pop(stage), trial, given=(others or {}).get(other.id, ()), others=others)
                if values:
                    engine.update(engine.get(other.id), values)
                engine.reset()
            except FieldErrors:
                pass  # it can't be done: the move then says what it waits for
    dates = [f for f in asked if engine.kind(f) in ("date", "datetime") and f["key"] in needs]
    own = [v for g in t["fields"] if engine.kind(g) in ("date", "datetime") and g["key"] not in needs
           for v in [(record.data or {}).get(g["key"])] if v]  # a pickup date like the hire's own dates, not just today
    for attempt in range(FIELD_TRIES + 1):
        if not needs:
            return
        try:
            engine.update(record, needs)
            engine.reset()
            return
        except FieldErrors:
            if not dates or attempt == FIELD_TRIES:
                return  # left as it is: the move then says what it needs
            for f in dates:
                tries = own + _others(engine, f, 0)
                needs[f["key"]] = tries[attempt % len(tries)] if tries else needs[f["key"]]


def _meeting(trial, engine, record, t, conditions, given=(), others=None, linked=None):
    """Values for the record's own fields that a move's conditions ask for (“once the deposit is Received”, “once the
    clearance date is filled in”), as the person told so would fill them in. A field the example set itself (`given`)
    is the example's to say, and is never changed. What they ask of a record it links to (“once the contract is
    Approved”) is put in `linked` as (that record, its list, values), unless the example set those itself (`others`)."""
    out = {}
    for c in conditions or []:
        if not isinstance(c, dict):
            continue
        if "." in str(c.get("field") or ""):
            head, rest = str(c["field"]).split(".", 1)
            lf = schema.field(t, head)
            other = engine.get((record.data or {}).get(lf["key"])) if lf and lf["type"] == "link" and not lf.get("multiple") \
                and isinstance((record.data or {}).get(lf["key"]), int) else None
            ot = engine.tables.get(lf["table"]) if other is not None else None
            if ot is not None and linked is not None and "." not in rest and not engine.matches(record, [c]):
                values = _meeting(trial, engine, other, ot, [{**c, "field": rest}], (others or {}).get(other.id, ()))
                if values:
                    linked.append((other, ot, values))
            continue
        f = schema.field(t, c.get("field") or "")
        if f is None or f["type"] not in schema.STORED or f["type"] in schema.FILES or f["key"] in given:
            continue
        if engine.matches(record, [c]):  # this one holds already
            continue
        op, v = c.get("op"), c.get("value")
        kind = engine.kind(f)
        number = to_number(v)
        today = engine.today
        value = None
        if op == "equals" or (op == "one_of" and isinstance(v, list) and v):
            value = v[0] if isinstance(v, list) else v
        elif op == "not_equals" and f["type"] == "select":
            value = next((o for o in f.get("options") or [] if str(o).lower() != str(v).lower()), None)
        elif op == "not_equals" and f["type"] == "checkbox":
            value = not (v in (True, "true", "yes", 1))
        elif op == "not_empty":
            value = sample_value(engine, t, f, trial.owner_id, {}, 300, 0)
        elif kind == "number" and number is not None and op in ("greater_than", "at_least", "less_than", "at_most"):
            value = {"greater_than": number + 1, "at_least": number, "less_than": number - 1, "at_most": number}[op]
        elif kind == "date" and op in ("before_today", "after_today", "more_than_days_ago"):
            gone = {"before_today": 1, "after_today": -1, "more_than_days_ago": int(number or 0) + 1}[op]
            value = (today - timedelta(days=gone)).isoformat()
        if value is not None:
            out[f["key"]] = value
    return out


def _colleague(trial, engine, record, pick):
    """Someone else who may make the move `pick` likes, when it's kept from this person by separation of duties (they
    raised it, or moved it to an earlier stage): a member of the team the move is for, or another staff member. None
    when it isn't kept from them, or there's no trial to bring someone in."""
    if trial is None:
        return None
    found = next(((m, problem) for _, m, problem in engine.moves(record) if pick(m)), None)
    if found is None or not engine.kept_from(record, found[0].get("not_by")):
        return None
    teams = {tm["key"] for tm in schema.teams(engine.bp)}
    who = next((w for w in found[0].get("who") or [] if w in teams or w in ROLES_BY), None) or "staff member"
    other = _doer(trial, engine, who)
    if other is not None and other.user_id is not None:  # a stand-in who sees what the owner sees (not kept from records
        trial.member(other.user_id).role = "admin"      # by row rules), still held to the move's who and not_by
        db.session.commit()
        other = trial.engine(other.user_id)
    return other


def _one_of(engine, table, owner_id, depth=0):
    """A record of a list for a sample to link to: one that's there, or an ordinary one added (with what it must link to
    in turn), so a button that needs a category or a cost centre can be pressed in a copy that has none yet."""
    rows = engine.rows(table)
    if rows:
        return rows[0].id
    t = engine.tables.get(table)
    if t is None or depth > 3:
        return None
    made = {}
    for f in t["fields"]:
        if f["type"] == "link" and f.get("required") and f["table"] != table:
            got = _one_of(engine, f["table"], owner_id, depth + 1)
            if got:
                made[f["table"]] = got
    record, _ = _add(engine, t, sample_values(engine, t, owner_id, made, 900 + depth))
    return record.id if record is not None else None


def _press_move(engine, record, t, pick, attach=True):
    """Press the record's button for the first move `pick` likes, as its person would (a file it needs attached first,
    unless not `attach`: a move meant to be refused is tried as it stands, its signature not yet given). False when
    there's no such button now; raises FieldErrors when the app says not yet."""
    for m in t["workflow"]["transitions"] if attach else []:  # a file the move needs is attached first, as the person would (examples can't)
        if pick(m):
            _attach(engine, record, t, m.get("requires") or [])
    engine.reset()
    record = engine.get(record.id)
    found = [(i, m, problem) for i, m, problem in engine.moves(record) if pick(m)]
    if not found:
        return False
    move = next((x for x in found if not x[2]), found[0])  # two buttons to one stage (the owner's, and short ones'): the open one
    if move[2]:
        raise FieldErrors({"_": move[2]})
    engine.move(record, move[0])
    return True


def _days_rules(engine, names):
    """The app's rules that run on a schedule, on the day the example has got to, as the scheduler would run them: the
    daily (and hourly) ones each day, the weekly ones on their weekday, the monthly ones on the 1st."""
    day = engine.today + timedelta(days=names.get("_waited", 0))
    for a in engine.bp.get("automations", []):
        trigger = a.get("trigger") or {}
        if trigger.get("type") != "schedule" or not a.get("enabled", True):
            continue
        every = trigger.get("every", "day")
        if every == "week" and day.weekday() != automations.WEEKDAYS.index(trigger.get("weekday", "mon")):
            continue
        if every == "month" and day.day != 1:
            continue
        if (a["key"], names.get("_waited", 0)) in names.setdefault("_ran", set()):
            continue
        names["_ran"].add((a["key"], names.get("_waited", 0)))
        automations.run_schedule(engine, a, "Tried out")
    engine.reset()


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
        r.created_at, r.updated_at = (r.created_at or utcnow()) - gone, (r.updated_at or utcnow()) - gone
    for row in RecordChange.query.filter_by(workspace_id=engine.ws.id).all():
        row.created_at = (row.created_at or utcnow()) - gone
    for a in Approval.query.filter_by(workspace_id=engine.ws.id).all():
        a.created_at = (a.created_at or utcnow()) - gone
        if (a.proposal or {}).get("ready_at"):  # a change's cooling-off ends that much sooner too
            a.proposal = {**a.proposal, "ready_at": (datetime.fromisoformat(a.proposal["ready_at"]) - gone).isoformat()}
    for run in Run.query.filter_by(workspace_id=engine.ws.id).all():
        run.created_at = (run.created_at or utcnow()) - gone
        if run.next_try:
            run.next_try -= gone
    db.session.commit()
    for _ in range(3):  # what falls due, and what that sets off
        due = Run.query.filter(Run.workspace_id == engine.ws.id, Run.status.in_(["waiting", "waiting_approval"]),
                               Run.next_try <= utcnow()).all()
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
            name = f["label"]  # named as people would see it (“Supplier's quote.pdf”), never by its key
            if f["type"] in ("image", "signature"):  # a picture, as a signature pad or a camera gives
                save(engine.ws.id, _PIXEL, f"{name}.png", record_id=record.id, field=key, by=engine.by, mime="image/png")
            else:
                save(engine.ws.id, b"%PDF-1.4 sample", f"{name}.pdf", record_id=record.id, field=key, by=engine.by)
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


def _named(engine, w, names):
    """A record an example named ("f1") by the name people see (“Mike Foreman”); anything else as it was written."""
    record = engine.get(names[w]) if isinstance(w, str) and isinstance(names.get(w), int) else None
    return engine.title(record) if record is not None else str(w)


def _same(engine, f, actual, wanted, names):
    if isinstance(wanted, str) and FILLED.match(wanted):
        return actual not in (None, "", [])
    if f is not None and f.get("sensitive") and wanted not in (None, ""):  # kept private: its last four, as anyone sees it
        return masked(actual) == mask(wanted)
    if wanted in (None, "") or (isinstance(wanted, list) and not wanted):
        return actual in (None, "", [])
    kind = engine.kind(f) if f else None
    if kind == "percent":
        a = to_number(actual)
        if a is not None and abs(a - 0.01) < 1e-9 and to_number(str(wanted).strip()) == 1:  # “1” is as likely 1% as 100%
            return True
        wanted = schema.as_fraction(wanted)
    if kind == "duration" and isinstance(wanted, str):  # "8:00", "1.5h", "90m", as it's typed in
        wanted = parse_duration(wanted) if parse_duration(wanted) is not None else wanted
    if kind in ("number", "money", "percent", "duration", "rating") or (isinstance(wanted, (int, float)) and not isinstance(wanted, bool)):
        a, w = to_number(actual), to_number(str(wanted).replace("$", "").replace(",", "")) if not isinstance(wanted, (int, float)) else wanted
        if a is None and w is None and actual not in (None, ""):  # words where a number was meant ("Low"): said as they are
            return str(actual).strip().lower() == str(wanted).strip().lower()
        return a is not None and w is not None and abs(a - w) <= max(0.011, abs(w) * 0.001)
    if f is not None and (f["type"] == "link" or _shows_a_link(engine, f)):  # a lookup of a link (a note's patient, by its visit) too
        ids = actual if isinstance(actual, list) else [actual]

        def among(w):  # one the example named ("f1"), or by its name ("Mike Foreman"), is one of those linked
            wanted_id = names.get(w, w) if isinstance(w, str) else w
            return any(str(i) == str(wanted_id) for i in ids) or any(engine.title(engine.get(i)).lower() == str(w).strip().lower()
                                                                     for i in ids if i and engine.get(i))
        return all(among(w) for w in (wanted if isinstance(wanted, list) else [wanted]))  # a crew of five: each of them
    if kind == "checkbox":
        return bool(actual) == (str(wanted).strip().lower() in ("true", "yes", "1", "ticked", "on"))
    if f is not None and kind in ("date", "datetime"):
        return str(actual or "")[:10] == str(wanted)[:10]
    if f is not None and kind == "user":  # a person, or a lookup of one (a request's approver, from its cost centre)
        people = {str(x) for x in (actual if isinstance(actual, list) else [actual])}
        return all(str(_person(engine, w)) in people for w in (wanted if isinstance(wanted, list) else [wanted]))
    if isinstance(actual, list):
        return [str(x).lower() for x in actual] == [str(x).lower() for x in (wanted if isinstance(wanted, list) else [wanted])]
    return str(actual if actual is not None else "").strip().lower() == str(wanted).strip().lower()


def _wait_until(engine, names, step):
    """Days pass, one by one, until the record's date is `days` days gone (1 if it doesn't say: the day after; 0: the
    day itself; -2: two days before it), the app's daily rules running each day as in any wait. For a date worked out
    from others (due 14 days after it's issued), which an example can't write in, and which moves on as the dates it's
    worked out from do."""
    alias, key = step.get("record"), str(step["until"]).strip()
    if "." in key and not alias:  # "q1.due_on"
        alias, key = key.split(".", 1)
    record = _record(engine, names, alias)
    f, _ = engine._at_path(record, key)
    if f is None or engine.kind(f) not in ("date", "datetime"):
        raise ExampleWrong(f"a wait “until” names a date of the record: {key} isn't one of {record.table}'s dates")
    said = to_number(step.get("days"))
    gone = 1 if said is None else int(said)
    for _ in range(732):
        record = _record(engine, names, alias)
        day = expr.to_date(engine._at_path(record, key)[1])
        if day is None:
            raise ExampleWrong(f"{alias or 'the record'}'s {key} is empty, so the day it goes by never comes: give it what it's worked out from")
        if (engine.today - day).days >= gone:
            return
        pass_time(engine, 1)
        names["_waited"] = names.get("_waited", 0) + 1
        _days_rules(engine, names)
    raise ExampleWrong(f"{alias or 'the record'}'s {key} doesn't go by within two years of waiting")


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


TOLD = re.compile(r"^(Notified|Would email|Emailed|Would text|Texted)\b")


def _told(engine, c, names):
    """Someone was told (in the app, by email or text) since the example began, about its record if it names one:
    {"told": true, "record": "i1", "about": "words the message has"} (or "told": false: nobody was). "by_rule": [keys]
    counts only what those rules said (a feature's own check: another rule's message neither passes nor fails it)."""
    record_id = _record(engine, names, c["record"]).id if c.get("record") else None
    runs = Run.query.filter(Run.workspace_id == engine.ws.id, Run.id > names.get("_first_run", 0)).order_by(Run.id).all()
    if c.get("by_rule"):
        runs = [r for r in runs if r.automation in set(c["by_rule"])]
    about = str(c.get("about") or "").strip().lower()
    said = [str(step.get("result") or "") for run in runs if record_id is None or run.record_id == record_id
            for step in run.steps or [] if TOLD.match(str(step.get("result") or ""))]
    said = [x for x in said if not about or about in x.lower()]
    wanted = c.get("told") not in (False, "false", 0, "no")
    what = "about it" if record_id else "at all"
    if wanted:
        return bool(said), (f"Told: {said[0]}" if said else f"Nobody was told {what}"), \
            (f"told: {said[0]}" if said else f"nobody was told {what}" + (f" with “{about}” in it" if about else "")
             + f" (rules that ran: {', '.join(dict.fromkeys(r.name or r.automation for r in runs)) or 'none'})")
    return not said, ("Nobody was told" if not said else f"Told when nobody should be: {said[0]}"), \
        ("nobody told" if not said else f"expected nobody to be told, but: {said[0]}")


def _worked_out(wanted):
    """An expected number written as its arithmetic ("=2*100+50", "=round(250*1.1, 2)"): the platform works it out, so
    a check never carries the model's own sum."""
    if isinstance(wanted, str) and wanted.strip().startswith("="):
        try:
            value = expr.evaluate(wanted.strip()[1:], expr.Context())
        except Exception:  # noqa: BLE001 - not arithmetic: compared as written
            return wanted
        return round(value, 6) if isinstance(value, float) else value
    return wanted


def _expect(engine, c, names):
    """One thing an example expects: (ok, what it shows in plain words, the same with keys for the AI)."""
    if c.get("told") is not None:
        return _told(engine, c, names)
    if "not_equals" in c:  # anything but this ("not Approved" after a move that's refused part of the way)
        same, plain, said = _expect(engine, {k: v for k, v in c.items() if k != "not_equals"} | {"equals": c["not_equals"]}, names)
        if same:
            return False, f"{plain}, expected anything but that", said.replace("; expected", "; expected anything but")
        return True, plain.replace(" came out ", " is ").replace(", expected ", ", not "), said
    written_out = False  # a date written out in full: on the calendar of the day the example was written
    if "equals" in c:
        c = {**c, "equals": _worked_out(c["equals"])}
        written_out = isinstance(c["equals"], str) and not RELATIVE.match(c["equals"].strip())
        if isinstance(c["equals"], str) and RELATIVE.match(c["equals"].strip()):  # "today+3" on the example's own calendar
            n = int((RELATIVE.match(c["equals"].strip()).group(1) or "0").replace(" ", ""))
            c = {**c, "equals": (date.today() + timedelta(days=n + int(names.get("_waited") or 0))).isoformat()}
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
    if engine.kind(f) in ("date", "datetime") and written_out and names.get("_moved"):
        wanted = _shifted(engine, f, wanted, names["_moved"])
    ok = _same(engine, f, actual, wanted, names)
    label = automations._path_label(engine, t["key"], path)
    shown = engine.display(f, actual) if actual not in (None, "", []) else "empty"
    as_kept = schema.as_fraction(wanted) if engine.kind(f) == "percent" else \
        parse_duration(wanted) if engine.kind(f) == "duration" and isinstance(wanted, str) and parse_duration(wanted) is not None else wanted
    expected = "filled in" if isinstance(wanted, str) and FILLED.match(wanted) else engine.display(f, as_kept) \
        if wanted not in (None, "") and f["type"] not in ("link", "user") and not _shows_a_link(engine, f) else \
        ", ".join(_named(engine, w, names) for w in (wanted if isinstance(wanted, list) else [wanted])) if wanted not in (None, "", []) else "empty"
    plain = f"{t['singular']}’s {label}: {shown}" if ok else f"{t['singular']}’s {label} came out {shown}, expected {expected}"
    said = f"{c.get('record')} ({t['key']}).{key} is {actual!r}; expected {wanted!r}"
    if not ok and f.get("expression") and len(path) == 1:  # how it was worked out, to tell a wrong sum from a wrong expectation
        read = _formula_reads(engine, record, f["expression"], names)
        said += f" (worked out as `{f['expression']}`" + (f" from {read}" if read else "") + ")"
    elif not ok and f["type"] == "rollup" and len(path) == 1:
        said += f" (worked out as {_rollup_reads(engine, record, f, names)})"
    outside = _filled_from_outside(engine, record.table, key) if not ok and actual in (None, "", []) and len(path) == 1 else []
    if outside:
        said += (f" (only {', '.join(f'“{a}”' for a in outside)} fills it in, and that starts when something comes in from outside (an "
                 f"email, another system, a file), which an example can't send: a {schema.lc(t['singular'])} added by hand doesn't "
                 f"start it, so leave this out of the example)")
    idle = _never_ran(engine, record, key) if not ok and len(path) == 1 else []
    for a, step in (_sets_from_elsewhere(engine, record, key) if not ok and len(path) == 1 else []):
        said += (f" (the rule “{a['name']}” on {a['trigger'].get('table')} sets it on its {step['target']}, but didn't on {c.get('record')}: "
                 f"it runs when {automations_words(engine, a.get('conditions') or [], a['trigger'].get('table')) or 'one changes'}"
                 + (f", and that step only if {automations_words(engine, step['only_if'], a['trigger'].get('table'))}" if step.get("only_if") else "") + ")")
    for a in idle:
        one = schema.lc(t["singular"])
        said += (f" (the rule “{a['name']}” sets it, but never ran on {c.get('record')}: "
                 + (f"it starts when {'an' if one[:1] in 'aeiou' else 'a'} {one} is changed, and nothing changed it after it was added (adding its lines doesn't): "
                    f"start it when one is added or changed (record_created_or_updated), keeping its conditions)"
                    if a["trigger"]["type"] == "record_updated" else
                    f"its conditions weren't met: {automations_words(engine, a.get('conditions') or [], record.table) or 'none'})"))
    if not ok and actual in (None, "", []) and len(path) == 1 and _checking(engine, record, key):
        plain += " (the AI's work is waiting for someone to check it)"
        said += (f" (the AI's work for it is waiting for someone to check it, as its step's review says: to try it as kept, "
                 f"add an “approve” step for {c.get('record')} before this, as whoever checks it would)")
    return ok, plain, said


def _formula_reads(engine, record, expression, names, deeper=True):
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
        how = ""  # what's itself worked out says how: total = 770.0 (`subtotal + gst`)
        if deeper and g is not None and "." not in name:
            if g.get("expression"):
                how = f" (`{g['expression']}`)"
            elif g["type"] == "rollup":
                how = f" ({_rollup_reads(engine, record, g, names, deeper=False)})"
        out.append(f"{name} = {value!r}{how}")
    if re.search(r"\b(today|now)\s*\(", plain):  # after a wait, today is that many days on in the example's calendar
        out.append(f"today = '{(engine.today + timedelta(days=names.get('_waited') or 0)).isoformat()}'"
                   + (f" (after waiting {names['_waited']} days)" if names.get("_waited") else ""))
    return ", ".join(out[:9])


def _rollup_reads(engine, record, f, names, deeper=True):
    """What a total was added up from, line by line: “the sum of quote_lines.amount over its 2 quote_lines: 1500.0 (from
    quantity = 10, unit_price = 150.0), 1000.0 (…)”, to tell a wrong sum in the example from a wrong total."""
    linked = []
    try:
        rows = linked = engine.related(record, f["table"], f["via"]) if f.get("via") else []
        if f.get("filters"):
            rows = [r for r in rows if engine.matches(r, f["filters"], about=record)]
    except Exception:  # noqa: BLE001 - a path it can't follow: said without the lines
        rows = []
    g = engine.field(f["table"], f["field"]) if f.get("field") and f["table"] in engine.tables else None
    what = f"the {f.get('aggregate', 'sum')} of {f['table']}" + (f".{f['field']}" if f.get("field") else "")
    if not rows and linked and f.get("filters"):  # why none count: they're linked, but not the ones it adds up
        theirs = []
        for c in list(schema.condition_leaves(f["filters"]))[:2]:
            h = engine.field(f["table"], str(c.get("field") or "").split(".")[0]) if f["table"] in engine.tables else None
            if h is not None:
                theirs.append(f"{h['key']}: " + ", ".join(sorted({engine.display(h, engine.value(r, h['key'])) or "empty" for r in linked})[:4]))
        return (what + f": {len(linked)} {f['table']} link to it, but only those where "
                + automations_words(engine, f["filters"], f["table"]) + " count" + (f" (theirs: {'; '.join(theirs)})" if theirs else ""))
    if not rows:
        return what + f": no {f['table']} records count towards it"
    parts = []
    for r in rows[:6]:
        value = engine.value(r, g["key"]) if g is not None else None
        read = _formula_reads(engine, r, g["expression"], names, deeper=False) if deeper and g is not None and g.get("expression") else ""
        parts.append(f"{value!r}" + (f" (from {read})" if read else ""))
    return what + f" over its {len(rows)} {f['table']}: " + ", ".join(parts) + (", …" if len(rows) > 6 else "")


def _sets_from_elsewhere(engine, record, key):
    """The rules on other lists that set this field on a record of this one, through a link (an alteration done moving
    its order on): [(rule, step)], to say what they wait for when it didn't come out."""
    out = []
    for a in engine.bp.get("automations", []):
        source = engine.tables.get(a["trigger"].get("table") or "")
        if not a.get("enabled", True) or source is None or source["key"] == record.table:
            continue
        for act in schema.steps_of(a["actions"]):
            if act.get("type") != "update_record" or not act.get("target") or key not in (act.get("values") or {}):
                continue
            path = schema.link_path(source, act["target"], engine.tables)
            if path and path[-1].get("table") == record.table:
                out.append((a, act))
    return out[:2]


def _never_ran(engine, record, key):
    """The rules of a record's own list that set this field on it, started by a change to one, that never ran on it."""
    ran = {r.automation for r in Run.query.filter_by(workspace_id=engine.ws.id, record_id=record.id)}
    return [a for a in engine.bp.get("automations", []) if a.get("enabled", True) and a["trigger"].get("table") == record.table
            and a["trigger"]["type"] in ("record_updated", "record_created", "record_created_or_updated") and a["key"] not in ran
            and any(act.get("type") == "update_record" and not act.get("target") and key in (act.get("values") or {})
                    for act in schema.steps_of(a["actions"]))]


OUTSIDE = ("email_received", "webhook_received", "file_added")


def _filled_from_outside(engine, table, key):
    """The rules that fill a field in, when every one of them starts from outside (a bank feed sent by another system):
    their names, else []."""
    filling = []
    for a in engine.bp.get("automations", []):
        own = a["trigger"].get("table") == table
        for act in schema.steps_of(a["actions"]):
            sets = (own and act.get("type") in ("ai_fill", "ai_check") and act.get("field") == key) \
                or (own and act.get("type") == "ai_extract" and key in (act.get("fields") or [])) \
                or (own and act.get("type") == "update_record" and not act.get("target") and key in (act.get("values") or {})) \
                or (act.get("type") in ("create_record", "ai_create_record") and act.get("table") == table and key in (act.get("values") or {}))
            if sets:
                filling.append(a)
                break
    return [a["name"] for a in filling] if filling and all(a["trigger"]["type"] in OUTSIDE for a in filling) else []


def _checking(engine, record, key):
    """Whether the AI's work for this field of this record is waiting for a person to check it."""
    return any(key in (a.fields or []) for a in Approval.query.filter_by(workspace_id=engine.ws.id, record_id=record.id,
                                                                         status="pending", kind="review"))


def walkthrough(workspace, example):
    """Act out one example in a throwaway copy, as play does, and what it went through: each stage a record got to, in
    order, and the rules that ran (the App map lights them up). Nothing in the real app changes."""
    from .. import appmap
    from ..models import RecordChange
    with copy_of(workspace) as trial:
        result = play(trial, example)
        engine = trial.engine()
        engine.reset()
        tables = {t["key"]: t for t in engine.bp.get("tables", [])}
        went = []
        rows = db.session.query(RecordChange, Record.table).join(Record, Record.id == RecordChange.record_id) \
            .filter(RecordChange.workspace_id == trial.ws_id).order_by(RecordChange.id).all()
        for change, table in rows:
            f = appmap._status(tables[table]) if table in tables else None
            moved = (change.changes or {}).get(f["key"]) if f else None
            if isinstance(moved, list) and len(moved) == 2 and moved[1] not in (None, ""):
                record = engine.get(change.record_id)
                went.append({"table": table, "stage": moved[1], "record": engine.title(record) if record else "",
                             "by": "a rule" if str(change.by or "").startswith(("automation", "the rule")) else "a person"})
        ran = list(dict.fromkeys(r.name or r.automation for r in Run.query.filter_by(workspace_id=trial.ws_id).order_by(Run.id)))
    return {**result, "went": went, "ran": ran}


def dated(example):
    """An example kept to be acted out again later, marked with the day it was written: a date it writes out in full
    (sold on 2026-10-28, chased on 2026-11-08) is read on that day's calendar, moved on by the days since, so a check
    kept for later stays true (round 43: an owner's check written the day before failed with nothing wrong)."""
    if not isinstance(example, dict) or example.get("written"):
        return example
    return {**example, "written": date.today().isoformat()}


def _moved(example):
    """The days since the example was written (0 for one without the day, or written today)."""
    try:
        return max(0, (date.today() - date.fromisoformat(str(example.get("written") or ""))).days)
    except ValueError:
        return 0


def play(trial, example):
    """Act out one example in this copy. Returns {"title", "ok", "lines": [(ok, plain)], "problem", "kind", "detail"}:
    kind "app" when the app refused a step, a rule stopped or something came out other than expected; "example" when the
    example itself names what isn't there."""
    engine, names = trial.engine(), {"_moved": _moved(example)}
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


STOPPED = re.compile(r"\b(block|stop|refus|prevent|can'?t|cannot|not allowed|isn'?t allowed|disallow|reject|must(n'?t| not)? |"
                     r"needs? (a|an|the|to)|without|requires?|kept from|held)", re.I)


def _rule_saw(exc, title="", n=None):
    """What the rule that refused a record saw, for the builder: a check written backwards (“is_empty(asset_tag)” for “capital
    items need an asset tag”) refuses records that have just what it asks for. An example about the refusal itself (“A coach
    without a waiver is blocked”) is told to expect it, so a rule that's right isn't turned round to let it through."""
    if not getattr(exc, "rule", None):
        return ""
    table, check, values = exc.rule
    saw = ", ".join(f"{k} = {'empty' if v in (None, '', []) else json.dumps(v, default=str)}" for k, v in values.items())
    refused = (f"If this example is meant to show it being stopped, the rule is right: mark step {n} \"refused\": true "
               f"(nothing is saved by it) and expect only what's there after it")
    said = f" (the rule on {table} checks `{check}`, which must come out true for a record to be saved; it came out false" + (f" with {saw}" if saw else "")
    kept = ("If the rule is what the owner asked for and this example isn't about it, its values break the rule (a margin "
            "of 0 where at least 10% is needed): send the example corrected, with values that keep to it")
    if STOPPED.search(title or ""):
        return said + f". {refused}; only if those values should be allowed is the check written backwards)"
    return said + f". {kept}. If those values should be allowed, the check is written backwards: fix the rule. {refused})"


def _line_of(engine, step, alias):
    """Whether a step adds a line of the record the example calls `alias` (a sale's line, right after the sale)."""
    if not (isinstance(step, dict) and step.get("do") == "add" and alias):
        return False
    t = engine.tables.get(str(step.get("table") or "")) or next((x for x in engine.tables.values() if str(step.get("table") or "").lower()
                                                                in (x["label"].lower(), x["singular"].lower())), None)
    return bool(t and t.get("embedded") and alias in [v for v in (step.get("values") or {}).values() if isinstance(v, str)])


def _both(example):
    """What an example expects is looked at once, after its last step: two values for one field can't both come out."""
    seen = {}
    for c in example.get("expect") or []:
        if isinstance(c, dict) and c.get("record") and c.get("field") and "equals" in c:
            was = seen.setdefault((c["record"], c["field"]), c["equals"])
            if str(was).strip().lower() != str(c["equals"]).strip().lower():
                return (f"it expects {c['record']}'s {c['field']} to be both “{was}” and “{c['equals']}”, but what an example "
                        f"expects is looked at once, after its last step: expect where it ends up, and make where it is part "
                        f"way its own example that stops there")
    return None


def _meant_refused(engine, example, step, names):
    """Someone else tries a move (“by”: "staff") that the example shows them being stopped at, though it doesn't say
    "refused": it expects the record to stay at the stage it's at, or the same move is made again later by someone else
    (staff can't send it; the owner then does)."""
    alias = step.get("record") or (step.get("as") if step.get("as") in names else None)
    if step.get("do") != "move" or not step.get("by") or not alias or alias not in names:
        return False
    steps = [s for s in example.get("steps") or [] if isinstance(s, dict)]
    later = steps[steps.index(step) + 1:] if step in steps else []
    if any(((s.get("do") == "move" and str(s.get("to") or "").lower() == str(step.get("to") or "").lower()) or s.get("do") == "approve")
           and (s.get("record") or s.get("as")) == alias and str(s.get("by") or "owner").lower() != str(step.get("by")).lower() for s in later):
        return True  # the same move made later by someone else, or someone else approving it: this one is stopped
    record = engine.get(names[alias])
    flow = (engine.tables.get(record.table) or {}).get("workflow") if record is not None else None
    if not flow:
        return False
    now = str((record.data or {}).get(flow["field"]) or "").strip().lower()
    return any(isinstance(c, dict) and c.get("record") == alias and c.get("field") == flow["field"]
               and str(c.get("equals") or "").strip().lower() == now for c in example.get("expect") or [])


def _play(trial, engine, example, names, out):
    both = _both(example)
    if both:
        out.update(problem=both, kind="example")
        return out
    last_run = db.session.query(db.func.max(Run.id)).filter(Run.workspace_id == trial.ws_id).scalar() or 0
    steps, by_hand = _not_by_hand(engine, example)
    if by_hand:
        out["by_hand"] = by_hand
    # a record added with its lines straight after (a sale, then its two lines) is saved as its form saves it: the lines
    # first, then its rules (the stock taken out for each line), never its rules on a record with no lines yet
    held = {i for i, s in enumerate(steps) if isinstance(s, dict) and s.get("do") == "add" and i + 1 < len(steps)
            and _line_of(engine, steps[i + 1], s.get("as"))}
    pending = None  # (alias, record id) whose rules wait for its lines

    def flush():
        nonlocal pending
        if pending:
            engine.reset()
            automations.on_change(engine, engine.get(pending[1]), None)
            engine.reset()
            pending = None
    for n, step in enumerate(steps, start=1):
        said = _step_words(step)
        try:
            if pending and not _line_of(engine, step, pending[0]):
                flush()
            _act(trial, engine, step, names, hold=n - 1 in held)
            if n - 1 in held:
                pending = (step["as"], names[step["as"]])
        except ExampleWrong as exc:
            out.update(problem=f"step {n} ({said}): {exc}", kind="example")
            return out
        except FieldErrors as exc:
            if step.get("refused") or _meant_refused(engine, example, step, names):
                engine.reset()
                continue
            reason = "; ".join(exc.errors.values())
            shapes = {f.get("pattern_message") for x in engine.tables.values() for f in x["fields"] if f.get("pattern_message")}
            left_out = all(OMITTED.match(str(e)) or BAD_VALUE.search(str(e)) or str(e) in shapes for e in exc.errors.values())
            out.update(problem=f"step {n} ({said}) was refused: {reason}" + (" (correct the example's values)" if left_out else "")
                       + _rule_saw(exc, example.get("title") or "", n),
                       kind="example" if left_out else "app", plain=f"{_plain_step(engine, step, names)} wasn't allowed: {reason}")
            return out
        except Exception as exc:  # noqa: BLE001 - a fault of ours: not the app's design, not the example's
            _log.warning("Example %r crashed at step %s: %s", out["title"], n, exc, exc_info=True)
            db.session.rollback()
            out.update(problem=f"step {n} ({said}): {type(exc).__name__}", kind="crash")
            return out
        engine.reset()
        if step.get("refused"):
            owner = "" if step.get("by") else (" (the owner did it, who may do nearly anything: if only some people should, such as "
                                                "the owner, have \"by\": \"staff\" try it)")
            out.update(problem=f"step {n} ({said}) should have been refused, but the app allowed it{owner}{_scheduled(engine, example)}", kind="app",
                       plain=f"{_plain_step(engine, step, names)} should have been stopped, but wasn't",
                       why=_safely(_why_allowed, engine, step, names))
            return out
    flush()
    stopped = failed_runs(engine, last_run)
    names["_first_run"] = last_run
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
        out["why"] = [line for c, (ok, _) in zip(example.get("expect") or [], out["lines"]) if not ok
                      for line in _safely(_why_value, engine, c, names, last_run)][:10]
        out["problem"] = ("; ".join(out["detail"] + [i["text"] for i in stopped]) or "it expected nothing") + _scheduled(engine, example)
        out["plain"] = "; ".join([p for ok, p in out["lines"] if not ok] + [i["plain"] for i in stopped])
        if by_hand:
            out["problem"] += (f" (the example's own steps added {', '.join(by_hand)} by hand after it happened; they were left out, "
                               f"since the app has to do that itself: a rule that adds them when it happens (create_record, or "
                               f"create_for_each over its lines, so what's used is lines, not words in a note), or, if people "
                               f"record them themselves, an example that says so in its title)")
            out["plain"] += f" (the app didn't add the {', '.join(schema.lc(x) for x in by_hand)} itself)"
    return out


# --- why: what of the app was about it, for the builder to put right what's really wrong ---------------------------------

def _safely(why, *args):
    """What explains a check that came out wrong, or nothing: explaining never stops a try-out."""
    try:
        return why(*args)
    except Exception:  # noqa: BLE001
        _log.warning("Explaining a check that came out wrong failed", exc_info=True)
        db.session.rollback()
        return []


def _rule_words(engine, t, rule):
    said = [] if str(rule.get("check")).strip().lower() in ("true", "false") else [f"check {rule['check']}"]
    if rule.get("holds"):
        said.append("must hold: " + automations_words(engine, rule["holds"], t["key"]))
    if rule.get("when"):
        said.append("only when " + automations_words(engine, rule["when"], t["key"]))
    return f"“{rule.get('message')}” (" + "; ".join(said) + ")"


def _why_allowed(engine, step, names):
    """What on the list could have refused a step that went through, and why each didn't: its rules (and what they
    found), limits, a gate's buttons; or that nothing there refuses it."""
    record = None
    try:
        alias = step.get("as") if step.get("do") == "add" else step.get("record")
        record = engine.get(names[alias]) if alias in names else None
    except Exception:  # noqa: BLE001
        record = None
    t = engine.tables.get(record.table) if record is not None else engine.tables.get(str(step.get("table") or ""))
    if t is None:
        return []
    out = []
    for rule in t.get("rules") or []:
        applies = not rule.get("when") or (record is not None and engine.matches(record, rule["when"]))
        try:
            kept = record is not None and engine._keeps(t, record, rule)
        except expr.ExprError as exc:
            out.append(f"{t['key']} rule {_rule_words(engine, t, rule)}: couldn't be worked out ({exc})")
            continue
        out.append(f"{t['key']} rule {_rule_words(engine, t, rule)}: " + ("didn't apply to it" if not applies else
                                                                           "held for it" if kept else "was broken, yet the change was kept"))
    for limit in t.get("limits") or []:
        out.append(f"{t['key']} limit {json.dumps({k: v for k, v in limit.items() if k in ('per', 'max', 'overlap', 'where')}, ensure_ascii=False)}: held")
    if step.get("do") == "move":
        moves = [m for m in (t.get("workflow") or {}).get("transitions") or [] if str(m.get("to", "")).lower() == str(step.get("to", "")).lower()]
        for m in moves:
            if m.get("only_if") and not step.get("by"):
                out.append(f"the move to {m['to']} has only_if {automations_words(engine, m['only_if'], t['key'])}: a button's "
                           f"only_if holds back staff's button, while rules hold back every save (the owner's too)")
    if not out:
        out.append(f"{t['key']} has no rule that refuses this: add_validation refuses adding or saving what breaks it, and a gate "
                   f"holds a record back from a stage, for everyone")
    return out[:8]


def _why_value(engine, c, names, since):
    """For an expectation that came out wrong: what in the app sets that field (rules, features, a formula), whether
    each ran in the example, and if not what it was waiting for, with the record's values."""
    if c.get("told") is not None or c.get("count") is not None or not c.get("record"):
        return []
    try:
        record = engine.get(names[c["record"]]) if c["record"] in names else None
    except Exception:  # noqa: BLE001
        record = None
    if record is None:
        return []
    t = engine.tables[record.table]
    key = str(c.get("field") or (t.get("workflow") or {}).get("field") or "").split(".")[0]
    f = schema.field(t, key)
    if f is None:
        return []
    if f["type"] == "formula":
        return [f"{t['key']}.{key} is worked out: {f['expression']}"]
    ran = {}
    for run in Run.query.filter(Run.workspace_id == engine.ws.id, Run.id > (since or 0)).all():
        ran.setdefault(run.automation, []).append(run.status)
    out = []
    for a in engine.bp.get("automations", []):
        sets = any((act.get("type") == "update_record" and key in (act.get("values") or {}) and a["trigger"].get("table") == t["key"]
                    and not act.get("target")) or (act.get("type") == "create_record" and act.get("table") == t["key"]
                                                    and key in (act.get("values") or {}))
                   for act in schema.steps_of(a["actions"]))
        if not sets:
            continue
        name = f"rule {a['key']}" + (f" (of {a['feature']})" if a.get("feature") else "")
        trigger = automations.describe_trigger(engine, a["trigger"]).lower()
        if a["key"] in ran:
            out.append(f"{name} sets {key}: it ran ({', '.join(sorted(set(ran[a['key']])))}), {trigger}")
            continue
        waiting = []
        for cond in schema.condition_leaves(a.get("conditions")):
            got = engine._at_path(record, cond["field"])[1]
            if not engine.matches(record, [cond]):
                waiting.append(f"{cond['field']} {cond['op']} {cond.get('value', '')}".strip() + f" (it's {got!r})")
        out.append(f"{name} sets {key} but didn't run: {trigger}" + (f"; waiting for {', '.join(waiting[:3])}" if waiting else ""))
    if not out:
        out.append(f"nothing in the app sets {t['key']}.{key} by itself: a rule (or a feature) has to, or people do")
    return out[:6]


def _scheduled(engine, example):
    """What to say of an example that came out wrong without letting any time pass, when the app has rules that run on
    a schedule (marking invoices overdue each day): they act as days go by, and dates in the past don't run them."""
    if any(isinstance(s, dict) and s.get("do") in ("run", "wait") for s in example.get("steps") or []):
        return ""
    timed = [a for a in engine.bp.get("automations", []) if a["trigger"]["type"] == "schedule" and a.get("enabled", True)]
    if not timed:
        return ""
    return (" (the app's rules that run on a schedule act in an example as days go by, {\"do\": \"wait\", \"days\": n}: each "
            "day of a wait they run as they would, even for dates in the past: " +
            ", ".join(f"{a['key']} (every {a['trigger'].get('every') or 'day'})" for a in timed[:6]) + ")")


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
    out, dropped, typed = steps[:first + 1], [], set()  # typed: what the example adds itself after it (a quote and its lines)
    for s in steps[first + 1:]:
        t = table(s.get("table")) if isinstance(s, dict) and s.get("do") == "add" else None
        ours = t is not None and any(v in typed for v in (s.get("values") or {}).values() if isinstance(v, str))
        if t is not None and t["key"] in counted and not said(t) and not ours:
            dropped.append(t["label"])
            continue
        if t is not None and s.get("as"):
            typed.add(s["as"])
        out.append(s)
    return out, list(dict.fromkeys(dropped))


def _plain_step(engine, step, names):
    """A step as the owner would say it: "Adding an invoice", "Moving the refund to Approved" (never its aliases or keys)."""
    do = step.get("do")
    t = engine.tables.get(str(step.get("table") or "")) or next((t for t in engine.tables.values() if str(step.get("table") or "").lower()
                                                                in (t["label"].lower(), t["singular"].lower())), None)
    alias = step.get("record") if step.get("record") in names else "_this" if not step.get("record") and "_this" in names else None
    record = engine.get(names[alias]) if alias else None
    one = (t or engine.tables.get(record.table if record is not None else "") or {"singular": "record"})["singular"].lower()
    if do == "add":
        return f"Adding {'an' if one[:1] in 'aeiou' else 'a'} {one}"
    if do == "move":
        return f"Moving the {one} to {step.get('to')}"
    if do == "press":
        wanted = str(step.get("button") or "").strip().lower()
        a = next((a for a in engine.bp.get("automations", []) if a["trigger"]["type"] == "button"
                  and wanted in (a["key"].lower(), a["name"].lower(), str(a["trigger"].get("label") or "").lower())), None)
        return f"Pressing “{(a and (a['trigger'].get('label') or a['name'])) or step.get('button')}” on the {one}"
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
        return f"move {step.get('record') or 'the one just added'} to {step.get('to')}"
    if do == "press":
        return f"press {step.get('button')} on {step.get('record') or 'the one just added'}"
    if do == "run":
        return f"run {step.get('rule') or step.get('button')}"
    if do == "wait" and step.get("until"):
        return f"wait until {step.get('record') or 'its'} {step['until']} has gone by" + (f" {step['days']} days" if step.get("days") not in (None, 1) else "")
    if do == "wait":
        return f"wait {step.get('days')} days"
    return f"{do} {step.get('record') or 'the one just added'}"


def try_out(workspace, examples=(), screens=True, blueprint=None):
    """Walk the app and act out each example, each in its own throwaway copy. Returns (walked, results). `blueprint`: a
    design to try instead of the app's own (a candidate the builder hasn't saved)."""
    with copy_of(workspace, blueprint) as trial:
        walked = walk(trial, screens=screens)
    results = []
    for example in examples:
        with copy_of(workspace, blueprint) as trial:
            results.append(play(trial, example))
    for crash in walked["crashes"]:
        _log.warning("Trying out workspace %s: %s", workspace.id, crash)
    return walked, results
