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
import re
import shutil
import tempfile
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

from flask import current_app

from ..blueprint import expr, schema
from ..engine import automations
from ..engine.records import Engine, FieldErrors, RecordContext, to_number
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
                   "SQLALCHEMY_DATABASE_URI": f"sqlite:///{folder / 'try-out.db'}", "SQLALCHEMY_BINDS": {}, "WTF_CSRF_ENABLED": False,
                   "MICROSOFT_CLIENT_ID": "", "MICROSOFT_CLIENT_SECRET": "", "OLLAMA_EMBED_MODEL": "", "SERVER_NAME": None})
    try:
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
            db.session.commit()
            try:
                yield Trial(app, ws.id, owner_id)
            finally:
                db.session.remove()
                db.engine.dispose()  # nothing stays open on a copy that's about to be thrown away
    finally:
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
    issues += failed_runs(engine)
    issues += _bare(trial, engine, made, crashes)
    if screens:
        crashes += _screens(trial, engine, made)
    return {"issues": _unique(issues), "crashes": crashes, "made": made}


def _bare(trial, engine, made, crashes):
    """The same again with only what each form insists on filled in, as people do: a plan's line with just its procedure
    picked, then the plan accepted. A rule that copies what's usually left empty into what can't be (an invoice line's
    description) only stops this way."""
    since = db.session.query(db.func.max(Run.id)).filter(Run.workspace_id == engine.ws.id).scalar() or 0
    bare = {}
    for n, t in enumerate(in_order(engine), start=1):
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
                    and not any(not schema.says_no(m["to"]) for _, m, _ in engine.moves(record)):
                stops = [m.get("label") or m["to"] for _, m in moves]
                out.append(_issue("fix", f"A {t['key']} record at {at} can't move on to {', '.join(later)}: the workflow has no move from "
                                         f"{at} but {', '.join(stops) or 'none'}", f"A {schema.lc(t['singular'])} at {at} can't go on to "
                                         f"{' or '.join(later[:2])}: the only way out is {' or '.join(stops) or 'none'}", t["key"]))
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
    client, wid, out = trial.client(), trial.ws_id, []
    paths = [f"/w/{wid}/app/"] + [f"/w/{wid}/app/p/{p['key']}" for p in engine.bp.get("pages", [])]
    for t in engine.tables.values():
        if t.get("embedded"):
            continue
        paths += [f"/w/{wid}/app/t/{t['key']}?view={v['key']}" for v in t.get("views", [])] or [f"/w/{wid}/app/t/{t['key']}"]
        paths.append(f"/w/{wid}/app/t/{t['key']}/new")
        if made.get(t["key"]):
            paths += [f"/w/{wid}/app/t/{t['key']}/{made[t['key']]}", f"/w/{wid}/app/t/{t['key']}/{made[t['key']]}/edit"]
    for path in paths:
        try:
            status = client.get(path).status_code
        except Exception as exc:  # noqa: BLE001
            status = f"{type(exc).__name__}: {exc}"
        if not isinstance(status, int) or status >= 500:
            out.append(f"GET {path}: {status}")
    return out


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
        record = engine.create(t["key"], _values(engine, t, step.get("values"), names), ai=False)
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
        engine.update(record, _values(engine, t, step.get("values"), names))
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
            at = (record.data or {}).get(t["workflow"]["field"])
            open_ = [m["to"] for _, m, _ in engine.moves(record)]
            raise FieldErrors({"_": f"from {at} there's no way to move it to {step.get('to')} (it can go to: {', '.join(open_) or 'nowhere'})"})
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
        else:  # what they'd write answering it ("the quote is too old"): the rule's {{reason}}
            automations.decide(decider, approval, do == "approve", decider.by, comment=said, by_id=decider.user_id)
    else:
        raise ExampleWrong(f"“{do}” isn't a step (add, change, move, press, approve, reject, wait or run)")


APPROVING = automations.APPROVES
REJECTING = re.compile(r"\b(reject|declin|refus|turn(s|ed)? down)", re.I)


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


def _same(engine, f, actual, wanted, names):
    if wanted in (None, "") or (isinstance(wanted, list) and not wanted):
        return actual in (None, "", [])
    kind = engine.kind(f) if f else None
    if kind == "percent":
        wanted = schema.as_fraction(wanted)
    if kind in ("number", "money", "percent", "duration", "rating") or (isinstance(wanted, (int, float)) and not isinstance(wanted, bool)):
        a, w = to_number(actual), to_number(str(wanted).replace("$", "").replace(",", "")) if not isinstance(wanted, (int, float)) else wanted
        return a is not None and w is not None and abs(a - w) <= max(0.011, abs(w) * 0.001)
    if f is not None and f["type"] == "link":
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
        if wanted not in (None, "") and f["type"] not in ("link", "user") else str(wanted or "empty")
    plain = f"{t['singular']}’s {label}: {shown}" if ok else f"{t['singular']}’s {label} came out {shown}, expected {expected}"
    return ok, plain, f"{c.get('record')} ({t['key']}).{key} is {actual!r}; expected {wanted!r}"


def play(trial, example):
    """Act out one example in this copy. Returns {"title", "ok", "lines": [(ok, plain)], "problem", "kind", "detail"}:
    kind "app" when the app refused a step, a rule stopped or something came out other than expected; "example" when the
    example itself names what isn't there."""
    engine, names = trial.engine(), {}
    out = {"title": str(example.get("title") or "An example")[:160], "ok": False, "lines": [], "problem": None, "kind": None, "detail": []}
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
            left_out = all(OMITTED.match(str(e)) for e in exc.errors.values())
            out.update(problem=f"step {n} ({said}) was refused: {reason}" + (" (correct the example's values)" if left_out else ""),
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
