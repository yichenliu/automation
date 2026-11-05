"""The builder, Keel v2: a turn is a search for an app that does what the owner needs and proves it.

    needs      what the app must do, one plain sentence each, from the owner's own words
    data       the lists, fields and stages those needs require (Keel language patches)
    behaviour  for each need, a feature (made right every time, with its own checks) or a rule
    checks     every feature's own; worked examples for needs met some other way; the owner's own, always
    simulate   all of them acted out in the simulator (the same engine, in memory, a second or so)
    search     what fails is put right by several candidate fixes made at once; the one that passes most, breaking
               nothing that passed, is kept; again, while that helps
    hand over  what was made, need by need, and honestly what still isn't right

Each step gets only what it needs: the language's catalog (generated from the registry, so the AI always knows every
feature the platform has), the app as the language describes it, and the needs. A patch the platform can't apply
comes back with exactly what's wrong and is sent again corrected, alone.
"""
import logging
import concurrent.futures
import contextvars
import copy
import json
import re
from datetime import timedelta
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field

from .. import ai, lang
from ..blueprint import schema
from ..engine.records import Engine
from ..extensions import db
from ..lang import model as m, registry
from ..models import Membership, Message, Workspace, utcnow
from . import checking, trial
from ..core.settings import settings
from ..core.context import current_app

_log = logging.getLogger(__name__)

CANDIDATES = 3  # candidate fixes made at once for what didn't come out right
SEARCH_ROUNDS = 2  # rounds of that, while each round helps
REPAIRS = 2  # rounds of sending patches the platform couldn't apply again, corrected

BASE = """You build and change business apps with Keel, for owners who know nothing about software. You write the Keel
language: patches, which the platform checks name by name before anything is saved (and tells you exactly what's
wrong if anything is).
- Use the owner's own words for lists, fields, stages and choices. Never invent what they didn't say: no extra choices,
  stages, tiers or rules. Where something needs a number or name they didn't give, use a sensible one.
- Keys are snake_case (invoice_lines). A list's key is plural; its singular names one record.
- A column, field, box or detail the owner asks for is a field on the list it belongs to (add_field), never a list.
- In a rule, every name is read from the record the rule is about; reach linked records through dots (order.status).
- Whenever a FEATURE does what's needed, use it: it is made right every time and checks itself. Write a rule only for
  what no feature does."""

UNDERSTAND = BASE + """

Read what the owner says and list what the app must do: their needs. Each need says what the app will do ("does": under
12 plain words, starting with a verb, as "Tells you when an invoice is paid"), with the owner's own words it comes
from ("said"). Only what they asked for. For an app made from
nothing, when they've said little, add what a business like theirs needs at its core (a few needs, "suggested": true).
For a change to an app in use, only what this message asks for.
Ask a question only when nothing at all can be built without its answer; otherwise choose sensibly and build.
"reply": one or two plain sentences to the owner about what you'll make (no jargon, no field names)."""

DATA = BASE + """

Now make the lists and fields these needs require: data only (lists, fields, stages and the moves between them).
What the app does by itself comes next: leave rules and features out. If the app already has what's needed, send no
patches.
- A list whose records go through steps gets stages, in order, with a move (a button) for each way people move one on.
- Lines that belong to a record (invoice lines) are a list "inside" it, with a link field to it.
- First name, in "lists", every list the needs require (most apps need several: the people, the things, the work, the
  money); then send an add_list for each one the app doesn't have yet, all in this answer.
- Something the owner wants added to a list the app has (a note, a date, a box, a detail: "add a team note to jobs") is
  one field on that list (add_field). A new list only for a new kind of thing kept many times over, with details of
  its own, that the owner asks for.
- Every link points at a list that exists or that you add here. Put numbers people type as number or money fields.

{catalog}"""

BEHAVIOUR = BASE + """

Now make what the app does by itself, need by need: a feature whenever one fits, a rule only for what none does. For
each need, send the patches that make it happen in "work", or put its number in "already" if the app as it is (its
lists, fields, features and rules) already does it. Every need is one or the other. Add a field only when a rule
needs one.
- What must be stopped ("can't", "prevent", "unless", "only once", "never more than") is refused by the app, not
  only told: a gate holds a record back from a stage or a choice; add_validation refuses adding or saving a record
  that breaks it (a job whose quote isn't approved: quote.status = 'Approved'); a limit caps how many share something.

{catalog}"""

CHECKS = """You check business apps before they're handed over. For each need below, write one worked example: what
people do in the app, step by step, and what they should see then. Use only the lists, fields, stages and buttons the
app has (below).
Steps:
- {{"do": "add", "table": list key, "as": a short name for the record (q1), "values": {{field: value}}}}: fill in every
  field marked *; a link's value is the short name of a record added before; dates as "today" or "today+3".
- {{"do": "change", "record": q1, "values": {{...}}}}, {{"do": "move", "record": q1, "to": a stage}} (by its button),
  {{"do": "press", "record": q1, "button": its label}}, {{"do": "approve", "record": q1}}.
- Something that happens on a schedule: {{"do": "wait", "days": n}} then {{"do": "run", "rule": the rule's key}}.
- "by": "staff" on a step someone other than the owner does; "refused": true on a step the app must stop.
Expect:
- {{"record": q1, "field": key, "equals": value}}: a number written as its arithmetic ("=2*100+50"), never your own sum;
  a date as "today" or "today+N" on the example's calendar (after a wait, today has moved on by those days);
- {{"table": key, "count": n}}; {{"told": true, "record": q1}} when someone must be told or emailed.
{when_none}"""

FIX = BASE + """

The app was tried out in a copy and the checks below didn't come out right, and some needs aren't made yet. Send the
patches that make them come out right and make what's missing, each for its need (need 0: a fix that isn't about one
need). The checks are what the owner needs: change the app, never the checks. Change only what's needed; leave what
works as it is. A feature's parts are changed by sending the feature again with its key.

{catalog}"""

REPAIR = BASE + """

These patches couldn't be applied, for the reasons given. Send each of them again, corrected, for the same need (what
else you sent is saved). Leave one out only if Keel truly can't do it, and say why in "reply".

{catalog}"""


# --- the answers' shapes --------------------------------------------------------------------------------------------

class Need(BaseModel):
    said: str
    does: str
    suggested: bool = False


class Understood(BaseModel):
    needs: list[Need]
    question: str = ""
    reply: str = ""


def _patches(group):
    if group == "data":
        allowed = registry.items("patch", "data")
    elif group == "behaviour":
        allowed = registry.items("feature") + registry.items("patch", "behaviour") + (m.AddList, m.AddField, m.ChangeField)
    else:
        allowed = lang.kinds()
    return Annotated[Union[allowed], Field(discriminator="do")]


def _answer_schema(group, by_need=False):
    patch = _patches(group)
    if by_need:
        class Work(BaseModel):
            need: int
            patches: list[patch]

        class Answer(BaseModel):
            work: list[Work]
            already: list[int] = Field(default_factory=list, description="the needs the app already meets as it is (no patches)")
            reply: str = ""
    elif group == "data":
        class Answer(BaseModel):  # noqa: F811
            lists: list[str] = Field(description="the key of every list the needs require, made here or there already")
            patches: list[patch]
            reply: str = ""
    else:
        class Answer(BaseModel):  # noqa: F811
            patches: list[patch]
            reply: str = ""
    return Answer.model_json_schema()


EXAMPLE_SCHEMA = {"type": "object", "required": ["examples"], "properties": {"examples": {"type": "array", "items": {
    "type": "object", "required": ["need", "title", "steps", "expect"], "properties": {
        "need": {"type": "integer"}, "title": {"type": "string"},
        "steps": {"type": "array", "items": {"type": "object", "required": ["do"], "properties": {
            "do": {"type": "string", "enum": ["add", "change", "move", "press", "approve", "reject", "wait", "run"]},
            "as": {"type": "string"}, "table": {"type": "string"}, "record": {"type": "string"}, "values": {"type": "object"},
            "to": {"type": "string"}, "button": {"type": "string"}, "rule": {"type": "string"}, "days": {"type": "integer"},
            "by": {"type": "string"}, "refused": {"type": "boolean"}}}},
        "expect": {"type": "array", "items": {"type": "object", "properties": {
            "record": {"type": "string"}, "field": {"type": "string"}, "equals": {}, "table": {"type": "string"},
            "count": {"type": "integer"}, "told": {"type": "boolean"}}}}}}}}}


def catalog(group):
    if group == "data":
        return registry.catalog(("field",)) + "\n\n" + _patch_lines(registry.items("patch", "data"))
    if group == "behaviour":
        return (registry.catalog(("feature", "when", "step")) + "\n\n"
                + _patch_lines(registry.items("patch", "behaviour") + (m.AddField, m.ChangeField)) + "\n\n" + registry.catalog(("field",)))
    return registry.catalog()


def _patch_lines(classes):
    lines = [registry.TITLES["patch"] + ":"]
    for cls in classes:
        lines.append(f"- {registry._names(cls, 'patch')} {{{registry._fields_said(cls, 'patch')}}}: {registry._doc(cls)} e.g. "
                     + json.dumps(cls.__keel_example__, ensure_ascii=False))
    return "\n".join(lines)


# --- asking ---------------------------------------------------------------------------------------------------------

def _ask(system, user, schema_, progress=None, tries=0, kind=None):
    """One call to the model: the answer as a dict. Each `tries` asks a little differently (another seed, warmer). The
    call is kept tagged with its step and the language it was written in (training.py keeps the turns that came out
    right, so the builder's own verified work is what it learns from)."""
    token = ai.again.set(tries)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    try:
        content, _ = ai.chat(messages, schema=schema_, on_progress=progress, think=settings().get("AI_THINK_BUILDER"), purpose="builder")
    finally:
        ai.again.reset(token)
    calls = ai.recording.get()
    if calls is not None and kind:
        mine = next((c for c in reversed(calls) if c.get("messages") == messages and c.get("content") == content), None)
        if mine is not None:
            mine.update(kind=f"v2_{kind}", language=registry.fingerprint())
    answer = ai.parse_json(content) or {}
    if isinstance(answer, dict):
        answer["_content"] = content
    return answer


def _ask_many(system, user, schema_, n):
    """n answers to the same question at once, each asked a little differently: for the search."""
    app = current_app._get_current_object()

    def one(k):
        def run():
            with app.app_context():
                return _ask(system, user, schema_, tries=k + 1, kind="fix")
        return contextvars.copy_context().run(run)

    out = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
        for future in [pool.submit(one, k) for k in range(n)]:
            try:
                out.append(future.result())
            except ai.AIError as exc:
                _log.info("A candidate fix wasn't made: %s", exc)
    return out


# --- checking -------------------------------------------------------------------------------------------------------

def _kept(workspace):
    return [x for x in (workspace.state or {}).get("examples") or [] if isinstance(x, dict)]


def _theirs(workspace):
    """The owner's own checks: never changed, never dropped, always acted out."""
    return [x for x in _kept(workspace) if checking.theirs(x)]


def _suite(workspace, bp, custom):
    """Every check this design must pass: its features' own, the builder's worked examples, and the owner's."""
    return lang.checks(bp) + custom + _theirs(workspace)


def _run(workspace, bp, examples, screens=False, asked=""):
    walked, results = trial.try_out(workspace, examples, screens=screens, blueprint=bp)
    walked["issues"] = list(walked.get("issues", [])) + _kept_as_asked(asked, bp)  # what the owner said is kept from some people
    hard = [i for i in walked.get("issues", []) if i.get("level") == "fix"]
    return {"walked": walked, "results": results, "examples": examples, "hard": hard}


def _score(run, required):
    """How right a design came out: the checks required of it that pass (one it no longer has fails), then fewer
    problems found walking it."""
    passed = {x.get("title") for x, r in zip(run["examples"], run["results"]) if r["ok"]}
    return (sum(1 for title in required if title in passed), -len(run["hard"]))


def _failing(run):
    return [(x, r) for x, r in zip(run["examples"], run["results"]) if not r["ok"] and r.get("kind") in ("app", "crash")]


def _broken_examples(run):
    return [(x, r) for x, r in zip(run["examples"], run["results"]) if r.get("kind") == "example" and x.get("by") == "builder"]


def _said_failing(run):
    lines = []
    for x, r in _failing(run)[:8]:
        steps = "; ".join(json.dumps(s, ensure_ascii=False) for s in x.get("steps", [])[:8])
        by = str(x.get("by") or "builder")
        whose = (f"the feature {by[8:]}'s own check: change that feature (send it again with its key)" if by.startswith("feature:")
                 else "your own worked example" if by == "builder" else "the owner's own check: the app must do what it says")
        lines.append(f"- “{x.get('title')}” ({whose}; steps: {steps}; expects {json.dumps(x.get('expect'), ensure_ascii=False)}): {r.get('problem')}")
    lines += [f"- {i['text']}" for i in run["hard"][:6]]
    return "\n".join(lines)


# --- the turn -------------------------------------------------------------------------------------------------------

class Turn:
    def __init__(self, message, workspace, question):
        self.message, self.workspace, self.question = message, workspace, question
        self.notes, self.changes, self.log = [], [], []  # log: each patch set sent, and what the platform said to it
        self.by = question.user_id if question else None
        self.tried, self.made = set(), set()  # the needs (by number) patches were sent for, and made for
        self.features = {}  # feature key -> the need it was made for
        self.navigate = None  # where to show the owner afterwards: the first list the turn made or changed
        self.ruled = set()  # the needs made by rules of their own (checked by worked examples, not a feature's checks)

    def note(self, text):
        self.notes.append(text)
        self.message.progress = list(self.notes)
        db.session.commit()

    def progress(self, thinking, content):
        self.message.thinking = (thinking or "")[-6000:]
        db.session.commit()

    @property
    def bp(self):
        return self.workspace.blueprint or schema.empty(self.workspace.name)

    def about(self):
        from .chat import describe_files
        engine = Engine(self.workspace)
        people = ", ".join(f"{engine.members().get(m_.user_id, 'someone')} ({m_.role})"
                           for m_ in Membership.query.filter_by(workspace_id=self.workspace.id) if m_.role != "portal")
        files = describe_files(self.question.files, self.workspace.id) if self.question is not None and self.question.files else ""
        from .prompts import looking_at
        where = looking_at(self.message.page)
        asker = Membership.query.filter_by(workspace_id=self.workspace.id, user_id=self.by).first() if self.by else None
        me = "owner" if asker is None or asker.role == "owner" else "admin"
        return (f"About the business: {self.workspace.about or self.workspace.name}\nToday: {schema.today_words(engine.today)}"
                + (f"\nPeople in the app: {people}" if people else "")
                + f"\n“Me” in what they say is the person asking: tell them as \"{me}\"."
                + (f"\n{where}" if where else "")
                + (f"\n\nFiles the owner attached (to design from; bring their rows in with import_rows when the owner wants their "
                   f"data in the app):\n{files}" if files else ""))

    def history(self):
        from .chat import _history
        said = []
        for turn in _history(self.workspace, self.question.id if self.question else self.message.id)[-6:]:
            said.append(f"{'Owner' if turn['role'] == 'user' else 'You'}: {str(turn['content'])[:800]}")
        return "\n".join(said)

    def save(self, out):
        from .chat import _data, save_version
        if out.changes:
            self.message.version_after = save_version(self.workspace, out.blueprint, out.changes, "chat", self.message.id, by=self.by)
            self.changes += out.changes
            self.navigate = self.navigate or out.navigate
            db.session.commit()
        if out.data:  # the records the owner gave, the rows of a file they attached: once the lists they go into are saved
            said, shown = _data(self.workspace, out.data, by=self.by)
            self.changes += said
            if shown is not None:
                self.message.results = shown
            db.session.commit()

    def credit(self, patches, out, owners):
        """Which needs the applied patches were for (and which features they made)."""
        made = credited(patches, out, owners)
        for i, p in enumerate(patches):
            if owners.get(i):
                self.tried.add(owners[i])
        self.made |= made["needs"]
        self.features.update(made["features"])

    def apply(self, patches, group, needs_text, owners=None):
        """Apply patches; send the refused ones back to be corrected, twice at most. The outcome as finally saved."""
        owners = owners or {}
        out = lang.apply(self.bp, patches)
        self.log.append({"step": group, "patches": patches, "problems": out.problems})
        self.save(out)
        self.credit(patches, out, owners)
        sent = list(patches)
        for _ in range(REPAIRS):
            if not out.problems:
                break
            refused = [(sent[n - 1], owners.get(n - 1)) for n in out.refused if 0 < n <= len(sent)]
            if not refused:
                break
            self.note(f"Correcting {len(refused)} change{'s' if len(refused) != 1 else ''} the platform couldn't make as sent")
            user = (f"{self.about()}\n\nThe needs:\n{needs_text}\n\nThe app now:\n{lang.describe(self.bp)}\n\nRefused patches:\n"
                    + "\n".join((f"(need {need}) " if need else "") + json.dumps(p, ensure_ascii=False)[:1500] for p, need in refused)
                    + "\n\nWhy:\n- " + "\n- ".join(out.problems[:12]))
            by_need = group == "behaviour"
            answer = _ask(REPAIR.format(catalog=catalog(group)), user, _answer_schema(group, by_need=by_need), self.progress, kind="repair")
            sent, owners = flatten(answer) if by_need else (answer.get("patches") or [], {})
            if not sent:
                break
            out = lang.apply(self.bp, sent)
            self.log.append({"step": f"{group} corrected", "patches": sent, "problems": out.problems})
            self.save(out)
            self.credit(sent, out, owners)
        return out


def flatten(answer):
    """(patches, {index: need}) from an answer that groups its patches by need."""
    sent, owners = [], {}
    for w in answer.get("work") or []:
        for p in w.get("patches") or []:
            if w.get("need"):
                owners[len(sent)] = w.get("need")
            sent.append(p)
    return sent, owners


def credited(patches, out, owners):
    """The needs whose patches applied, and the features they made: {"needs": {n}, "features": {key: n}}."""
    from ..lang import compile as lc
    needs, features = set(), {}
    refused = set(out.refused)
    for i, p in enumerate(patches):
        if i + 1 in refused:
            continue
        need = owners.get(i)
        if need:
            needs.add(need)
        parsed, _ = lc.parse([p])
        if parsed and hasattr(parsed[0][1], "feature_key"):
            features[parsed[0][1].feature_key()] = need
    return {"needs": needs, "features": features}


def work(message_id):
    """Do one queued builder turn the v2 way (chat.work calls this)."""
    message = db.session.get(Message, message_id)
    if message is None or message.status not in ("queued", "waiting_ai"):
        return
    workspace = db.session.get(Workspace, message.workspace_id)
    message.status, message.started_at, message.error = "thinking", message.started_at or utcnow(), None
    message.progress, message.thinking = [], ""
    message.version_before = workspace.version or 0
    db.session.commit()
    question = Message.query.filter(Message.workspace_id == workspace.id, Message.channel == "builder", Message.id < message.id,
                                    Message.role == "user").order_by(Message.id.desc()).first()
    turn = Turn(message, workspace, question)
    try:
        _turn(turn)
    except ai.AIUnavailable as exc:
        if turn.changes:  # what's built is saved: hand that over rather than wait
            _hand_over(turn, [], None, stopped=str(exc))
            return
        message = db.session.get(Message, message_id)
        message.status, message.error = "waiting_ai", str(exc)
        message.next_try = utcnow() + timedelta(seconds=20)
        db.session.commit()
    except ai.AIError as exc:
        message = db.session.get(Message, message_id)
        message.status, message.error, message.finished_at = "failed", str(exc), utcnow()
        db.session.commit()
    except Exception as exc:  # a fault of ours: said, never left "thinking"
        _log.exception("Builder turn %s failed", message_id)
        db.session.rollback()
        message = db.session.get(Message, message_id)
        message.status, message.finished_at = "failed", utcnow()
        message.error = f"Something went wrong on our side ({type(exc).__name__}). It's been logged; try again, or ask in different words."
        db.session.commit()


def _turn(turn):
    from .chat import _mending
    workspace, question = turn.workspace, turn.question
    asked = (question.text if question else "") or ""
    changing = bool(turn.bp.get("tables"))
    mending = _mending(workspace, question) if changing else None
    needs, custom = [], [x for x in _kept(workspace) if x.get("by") == "builder"]
    reply = ""
    if not (mending and only_fixing(asked)):  # anything more than "fix it" is read: a change, or what should happen
        turn.note("Working out what you need")
        understood = _understand(turn, asked)
        needs, reply = understood.needs, understood.reply
        if understood.question and not needs:
            turn.message.text, turn.message.status, turn.message.finished_at = understood.question, "done", utcnow()
            db.session.commit()
            return
        if needs:
            turn.note("What I'll make it do: " + "; ".join(n.does.rstrip(".") for n in needs[:6]))
    asked_for = [x for x in ((mending or {}).get("found") or {}).get("missing") or [] if x.get("said")]
    if asked_for:  # the owner's checks the app doesn't do yet: what they asked for, made now as needs of their own
        known = {n.said.strip().lower() for n in needs}
        needs = list(needs) + [Need(said=x["said"], does=x["said"].strip().rstrip(".")[:160]) for x in asked_for
                               if x["said"].strip().lower() not in known]
        turn.note("Making what your checks ask for: " + "; ".join(x["said"].rstrip(".")[:80] for x in asked_for[:3]))
    needs_text = "\n".join(f"{i}. {n.does} (from: “{n.said}”)" for i, n in enumerate(needs, start=1))
    if needs:
        _make_lists(turn, needs_text)
        turn.note("Making what it does")
        sent, owners = _make_behaviour(turn, needs, needs_text)
        # worked examples for the needs met by rules (a feature writes its own checks)
        by_rule = sorted({owners[i] for i, p in enumerate(sent) if isinstance(p, dict) and p.get("do") in ("add_rule", "change_rule")
                          and owners.get(i) in turn.made})
        turn.ruled |= set(by_rule)
        if by_rule:
            turn.note("Writing checks for what isn't a ready-made feature")
            wanted = "\n".join(f"{i}. {needs[i - 1].does}" for i in by_rule if 0 < i <= len(needs))
            try:
                found = _ask(CHECKS.format(when_none=""), f"{turn.about()}\n\nThe needs to check:\n{wanted}\n\nThe app:\n{lang.describe(turn.bp)}",
                             EXAMPLE_SCHEMA, turn.progress, kind="checks").get("examples") or []
            except (ai.CutOff, ai.Stuck):  # no worked examples this time: those needs are said as not tried out
                found = []
            titles = {x.get("title") for x in custom}
            custom += [{**{k: v for k, v in x.items() if k != "need"}, "by": "builder",
                        "need": needs[x["need"] - 1].does if 0 < (x.get("need") or 0) <= len(needs) else ""}
                       for x in found if isinstance(x, dict) and x.get("steps") and x.get("expect") and x.get("title") not in titles][:6]
    if not turn.changes and not mending and not needs:
        turn.message.text, turn.message.status, turn.message.finished_at = reply or "Tell me what you'd like your app to do.", "done", utcnow()
        db.session.commit()
        return
    # simulate, then search
    examples = _suite(workspace, turn.bp, custom)
    turn.note(f"Trying your app out: {len(examples)} check{'s' if len(examples) != 1 else ''}")
    best = _run(workspace, turn.bp, examples, asked=asked)
    broken = _broken_examples(best)
    if broken:  # a worked example of the builder's that names what isn't there: corrected once, never quietly dropped
        turn.note(f"Correcting {len(broken)} check{'s' if len(broken) != 1 else ''} that couldn't be acted out")
        said = "\n".join(f"- “{x.get('title')}”: {json.dumps({k: x.get(k) for k in ('steps', 'expect')}, ensure_ascii=False)[:1500]}\n  why: {r.get('problem')}"
                         for x, r in broken)
        found = _ask(CHECKS.format(when_none=""), f"{turn.about()}\n\nThe app:\n{lang.describe(turn.bp)}\n\nThese examples couldn't be acted out. "
                     f"Send each corrected, with the same title (need 0):\n{said}", EXAMPLE_SCHEMA, turn.progress, kind="checks").get("examples") or []
        fixed = {x.get("title"): x for x in found if isinstance(x, dict) and x.get("steps") and x.get("expect")}
        custom = [{**{k: v for k, v in fixed[x.get("title")].items() if k != "need"}, "by": "builder", "need": x.get("need", "")}
                  if x.get("title") in fixed else x for x in custom]
        examples = _suite(workspace, turn.bp, custom)
        best = _run(workspace, turn.bp, examples, asked=asked)
    first = {"wrong": len(best["hard"]), "examples": [sum(1 for r in best["results"] if r["ok"]), len(best["results"])],
             "found": [i["text"] for i in best["hard"]][:8] + [f"{x['title']}: {r['problem']}" for x, r in _failing(best)][:8]}
    required = [x.get("title") for x in examples]
    rounds = 0
    for rounds in range(1, SEARCH_ROUNDS + 1 + (1 if mending else 0)):
        failing, unmade = _failing(best), sorted(turn.tried - turn.made)
        if not failing and not best["hard"] and not unmade:
            rounds -= 1
            break
        n = len(failing) + len(best["hard"]) + len(unmade)
        turn.note(f"Putting right {n} thing{'s' if n != 1 else ''} that didn't come out right or aren't made yet (trying {CANDIDATES} ways)")
        user = (f"{turn.about()}\n\nThe owner's latest message: {asked}\n\nThe needs:\n{needs_text or '(as the app is)'}\n\n"
                f"The app now:\n{lang.describe(turn.bp)}\n\n"
                + (f"Didn't come out right:\n{_said_failing(best)}\n\n" if failing or best["hard"] else "")
                + (f"Not made yet: {', '.join(f'need {i}' for i in unmade)} (what was sent for them was refused)\n" if unmade else ""))
        answers = _ask_many(FIX.format(catalog=catalog("all")), user, _answer_schema("all", by_need=True), CANDIDATES)
        tried = []
        for answer in answers:
            patches, owners = flatten(answer)
            if not patches:
                continue
            out = lang.apply(turn.bp, patches)
            turn.log.append({"step": f"fix {rounds}", "patches": patches, "problems": out.problems})
            if not out.changes:
                continue
            made = credited(patches, out, owners)
            run = _run(workspace, out.blueprint, _suite(workspace, out.blueprint, custom), asked=asked)
            passing, issues = _score(run, required)
            tried.append(((passing + len(made["needs"] & set(unmade)), issues, answer.get("_content")), out, run, patches, owners))
        if not tried:
            break
        score, out, run, patches, owners = max(tried, key=lambda x: x[0][:2])
        if score[:2] <= _score(best, required):  # nothing tried did better: what there is stands
            break
        _chosen(score[2])
        turn.save(out)
        turn.credit(patches, out, owners)
        best = run
    if asked_for and turn.changes:  # the owner's checks for them are tried again, as the owner would
        checking.try_again_missing(workspace)
    _hand_over(turn, needs, best, reply=reply, first=first, rounds=rounds, custom=custom)


TOO_MUCH = (ai.CutOff, ai.Stuck)


def _understand(turn, asked):
    """The owner's needs: asked once, then again a little differently, then for fewer, if the answer runs away (a model
    repeating itself, or writing more than fits)."""
    user = (f"{turn.about()}\n\nThe app now:\n{lang.describe(turn.bp)}\n\nEarlier:\n{turn.history() or '(nothing yet)'}\n\n"
            f"The owner says: {asked}")
    for tries, extra in ((0, ""), (1, ""), (2, "\n\n(Keep it short: at most six needs, one sentence each.)")):
        try:
            return Understood.model_validate(_ask(UNDERSTAND, user + extra, Understood.model_json_schema(), turn.progress, tries,
                                                  kind="understand"))
        except TOO_MUCH:
            turn.note("That came out too long; asking again more briefly")
    raise ai.Stuck("The AI kept repeating itself working out what's needed")
FIX_ONLY = re.compile(r"^\W*(please\W+)?(fix|sort out|sort|correct|repair|mend)\b.{0,40}$|^\W*(yes|yeah|yep|ok|okay|sure|go ahead|do it|"
                      r"please)\W*(please|thanks|do it)?\W*$", re.I | re.S)


def only_fixing(text):
    """Whether the owner only asked for what isn't right to be put right (the Fix button, the Checks page's, "fix it",
    "yes please"), with nothing else to read: then the turn goes straight to putting it right."""
    text = str(text or "").strip()
    return text.startswith("Please fix what isn't right") or text == "Fix what didn't come out right" or bool(FIX_ONLY.match(text))


def _make_lists(turn, needs_text):
    """The lists the needs require: asked for all at once, then a few at a time for any named but not made (and a few
    at a time from the start if all at once is too much to write)."""
    from ..lang.design import Design

    def ask(extra="", tries=0):
        return _ask(DATA.format(catalog=catalog("data")), f"{turn.about()}\n\nThe needs:\n{needs_text}\n\nThe app now:\n"
                    f"{lang.describe(turn.bp, with_rules=False)}{extra}", _answer_schema("data"), turn.progress, tries, kind="data")
    turn.note("Making the lists")
    try:
        data = ask()
    except TOO_MUCH:
        turn.note("That was too much in one go; making the lists a few at a time")
        try:
            data = ask("\n\nName every list in \"lists\", but send at most four add_list patches in this answer (the first four "
                       "not made yet); the rest are asked for next.", tries=1)
        except TOO_MUCH:
            data = {}
    if data.get("patches"):
        turn.apply(data["patches"], "data", needs_text)
    named = list(data.get("lists") or [])
    for n in range(3):
        missing = [name for name in named if Design(turn.bp).list(name) is None]
        if not missing:
            return
        turn.note(f"Making the list{'s' if len(missing) != 1 else ''} still to make: {', '.join(missing)}")
        try:
            more = ask(f"\n\nThese lists are named as needed but aren't made yet: {', '.join(missing)}. Send an add_list for at most "
                       f"four of them now (only those).", tries=n + 1)
        except TOO_MUCH:
            continue
        if not more.get("patches"):
            return
        turn.apply(more["patches"], "data", needs_text)


def _make_behaviour(turn, needs, needs_text):
    """(patches, {index: need}) for what the app does: all needs at once, or one need at a time if that's too much."""
    def ask(extra="", by_need=True, tries=0):
        return _ask(BEHAVIOUR.format(catalog=catalog("behaviour")), f"{turn.about()}\n\nThe needs:\n{needs_text}\n\nThe app now:\n"
                    f"{lang.describe(turn.bp)}{extra}", _answer_schema("behaviour", by_need=by_need), turn.progress, tries, kind="behaviour")
    try:
        answer = ask()
        sent, owners = flatten(answer)
        already = {int(i) for i in answer.get("already") or [] if str(i).isdigit()}
        silent = [i for i in range(1, len(needs) + 1) if i not in set(owners.values()) | already]
        if silent:  # a need neither made nor said to be met already: asked for once more, those alone
            turn.note(f"Making what's still missing: {'; '.join(needs[i - 1].does.rstrip('.') for i in silent[:3])}")
            more = ask(f"\n\nYou sent nothing for need{'s' if len(silent) != 1 else ''} {', '.join(map(str, silent))}. Send the patches that "
                       f"make each happen (a feature whenever one fits), or put its number in \"already\" if the app as it is "
                       f"already does it.", tries=1)
            more_sent, more_owners = flatten(more)
            already |= {int(i) for i in more.get("already") or [] if str(i).isdigit()}
            owners.update({len(sent) + k: v for k, v in more_owners.items()})
            sent = sent + more_sent
        turn.tried |= {i for i in range(1, len(needs) + 1) if i not in already}  # what's neither made nor already met is said
    except TOO_MUCH:
        turn.note("That was too much in one go; making it one need at a time")
        sent, owners = [], {}
        for i, n in enumerate(needs, start=1):
            try:
                one = ask(f"\n\nNow only need {i}: {n.does}. Send just the patches for it (none if the lists already meet it).",
                          by_need=False, tries=1)
            except TOO_MUCH:
                continue
            for p in one.get("patches") or []:
                owners[len(sent)] = i
                sent.append(p)
    if sent:
        turn.apply(sent, "behaviour", needs_text, owners)
    return sent, owners


def _not_yet_said(wrong, issues, unmade):
    """What isn't right yet, in plain words: what came out wrong when tried, then what couldn't be made at all."""
    parts = []
    said = [f"“{r['title'].rstrip('.')}”" for r in wrong[:3]] + [i.rstrip(".") for i in issues[:max(0, 3 - len(wrong))]]
    more = len(wrong) + len(issues) - len(said)
    if said:
        parts.append(f"When I tried it out, {'this' if len(said) + more == 1 else 'these'} didn't come out right: "
                     + "; ".join(said) + (f", and {more} more" if more > 0 else "") + ".")
    if unmade:
        parts.append(f"I couldn't make {'this' if len(unmade) == 1 else 'these'} yet: " + "; ".join(unmade) + ".")
    return ("Not everything works yet. " + " ".join(parts) + " Press “Fix these” and I'll have another go, or tell me what "
            "should happen.")


def _chosen(content):
    """The candidate fix that was kept, marked among the turn's calls (the others weren't the builder's best)."""
    for c in ai.recording.get() or []:
        if c.get("kind") == "v2_fix" and c.get("content") == content:
            c["chosen"] = True


KEEP_FROM = re.compile(r"\b(shouldn'?t|should not|mustn'?t|must not|can'?t|cannot|don'?t|do not|never) (be able to )?(see|know|access)\b"
                       r"|\bonly see\b|\bhidden from\b|\bkept? .{0,40}\b(private|from)\b|\bnot (be )?visible\b", re.I)


def _kept_as_asked(asked, bp):
    """The owner said some people mustn't see some things ("the makers shouldn't see prices or customer details"), but
    the design keeps nothing from anyone who can do the work: a try-out problem, said if it isn't put right."""
    if not KEEP_FROM.search(asked or ""):
        return []
    teams = {tm["key"] for tm in schema.teams(bp)}
    kept = any((t.get("access") or {}).get("teams") or (t.get("access") or {}).get("view") in ("admin", "owner") or t.get("row_access")
               or any(f.get("access") for f in t["fields"]) or (t.get("access") or {}).get("view") in teams for t in bp.get("tables", []))
    if kept:
        return []
    said = KEEP_FROM.search(asked)
    sentence = next((x.strip() for x in re.split(r"(?<=[.!?\n])\s+", asked) if said.group(0) in x), said.group(0))[:200]
    return [{"level": "fix", "where": "", "text": f"The owner said “{sentence}”, but the app keeps nothing from anyone who can do the work: "
                                                  f"use set_team, set_access, own_records or a field's visible_to",
             "plain": f"you said “{sentence}”, but everyone you invite to do the work can still see all of it"}]


def _need_states(turn, needs, run):
    """Each need, and whether it's made and comes out right: "ok", "wrong" (a check of it failed) or "unmade"."""
    failing = {x.get("title") for x, r in zip(run["examples"], run["results"]) if not r["ok"]} if run else set()
    states = []
    for i, n in enumerate(needs, start=1):
        if i in turn.tried and i not in turn.made:
            states.append((n, "unmade"))
            continue
        results = dict(zip([id(x) for x in (run or {}).get("examples", [])], (run or {}).get("results", [])))
        mine = [x for x in (run or {}).get("examples", []) if x.get("need") == n.does
                or str(x.get("by", "")).removeprefix("feature:") in {k for k, v in turn.features.items() if v == i}]
        acted = [x for x in mine if results.get(id(x), {}).get("kind") not in ("example", "crash")]
        if any(x.get("title") in failing for x in acted):
            states.append((n, "wrong"))
        elif i in turn.ruled and not acted:  # made by a rule, but no worked example of it could be acted out
            states.append((n, "unchecked"))
        else:
            states.append((n, "ok"))
    return states


def _hand_over(turn, needs, run, reply="", first=None, rounds=0, custom=(), stopped=None):
    from .chat import _not_yet, navigate_url, plain
    workspace, message = turn.workspace, turn.message
    shown, issues, wrong = [], [], []
    if run is not None:
        results = list(zip(run["examples"], run["results"]))
        playable = [(x, r) for x, r in results if r.get("kind") not in ("example", "crash") or x.get("by") not in ("builder",)]
        wrong = [r for _, r in playable if not r["ok"]]
        issues = [i["plain"] for i in run["hard"]]
        shown = [{"title": r["title"], "ok": r["ok"], "lines": [[ok, line] for ok, line in r["lines"]], "problem": r.get("plain") or "", "example": x}
                 for x, r in playable]
        state = dict(workspace.state or {})
        state["examples"] = [x for x, r in playable if x.get("by") != "builder" or r["ok"] or x in custom][:checking.KEEP]
        workspace.state = state
        db.session.commit()
        checking.remember(workspace, run["walked"], [r for _, r in playable], "this change", [x for x, _ in playable], tell=False)
    states = _need_states(turn, needs, run)
    unmade = [n.does.rstrip(".") for n, s in states if s == "unmade"]
    lines = [f"- {n.does.rstrip('.')}" + {"ok": "", "wrong": " (not right yet)", "unmade": " (not made yet)",
                                           "unchecked": " (made, but I couldn't try it out: check it yourself)"}[s] for n, s in states]
    if wrong or issues or unmade:
        text = _not_yet_said(wrong, issues, unmade)
    elif turn.changes:
        text = "Your app is ready to try." if not turn.message.version_before else "Done."
    else:
        text = reply.strip() or "Nothing needed changing."
    if lines and turn.changes:
        text += "\n\nWhat your app does now:\n" + "\n".join(lines)
    if stopped:
        text += f"\n\n(The AI stopped answering before I'd finished: {stopped}. What's built is saved.)"
    if turn.changes and message.version_before:  # said first: who can now see what, and money on what people already have
        from ..blueprint import ops
        from ..engine.carry import REPRICED
        from ..models import Version
        before = Version.query.filter_by(workspace_id=workspace.id, number=message.version_before).first()
        seen = ops.access_changes(before.blueprint if before else {}, workspace.blueprint) if before else []
        repriced = [c for c in turn.changes if c.startswith(REPRICED)]
        if repriced:
            text = ". ".join(repriced[:2]) + ".\n\n" + text
        if seen:
            text = f"Who can see what has changed: {'; '.join(seen[:4])}. Check that's what you want.\n\n" + text
    message.text = plain(text, workspace.blueprint, (turn.question.text if turn.question else ""))
    message.checks = {"examples": shown, "issues": issues, "fixes": rounds, "first": first or {},
                      "needs": [{"does": n.does, "said": n.said, "suggested": n.suggested, "state": s} for n, s in states]}
    message.ops = turn.log  # what was sent and what the platform said to it: for looking into a turn, and training
    message.changes = turn.changes
    message.navigate = navigate_url(workspace, turn.navigate)
    message.status, message.finished_at, message.next_try = "done", utcnow(), None
    db.session.commit()
