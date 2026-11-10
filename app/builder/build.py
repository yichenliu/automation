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
import time
from datetime import timedelta
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field

from .. import ai, lang
from ..blueprint import schema
from ..engine.records import Engine
from ..extensions import db
from ..lang import model as m, registry
from ..models import Membership, Message, Workspace, utcnow
from . import checking, recipes, trial
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
For a new app, when the owner has said almost nothing about how their business works (one line: "need a system for
my shop"), don't guess: ask up to 3 short questions in "questions" and send no needs. Each has 2 to 5 short answers
they can click, the usual ones for a business like theirs (and "None of these" where it fits), about what decides the
app most: what they keep track of, what needs their OK, what gets chased or reminded. Ask only once: when
they've answered, build from what they said and choose sensibly for the rest.
Otherwise ask a question ("question") only when nothing at all can be built without its answer; choose sensibly and build.
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
needs one, and a list (add_list, first in that need's patches) only when a need is about records the app has no list
for yet (stock to reorder, invoices to chase).
- What must be stopped ("can't", "prevent", "unless", "only once", "never more than", "not while") is refused by the
  app, whoever tries (the owner, staff, the app's own rules), not only told: a gate holds a record back from a stage or
  a choice; add_validation refuses adding or saving a record that breaks it (a job whose quote isn't approved:
  quote.status == 'Approved'); a button's only_if holds the button back until it's met (no specs sent until the design
  is approved).
- In an app in use, what it does already that this message isn't about stays as it is: take a rule or a feature away
  only when the owner asks for it gone, or a patch here makes what replaces it; change one rule with change_rule
  (a feature's rule too: it's then yours), rather than taking rules away to start again.
- A need about seeing or keeping records ("lists…", "shows who owes", "keeps customer details") is met by the lists and
  fields themselves: put its number in "already", or add a view of a list (add_view: filtered, grouped by customer, a
  board by stage, a calendar), never a page.
- A rule about other records is one formula: the records linking to this one (client.invoices, job.payments) or a
  whole list (bookings), with count, sum, any, all, none, min, max and where. "No booking while the client owes over
  30 days": add_validation check count(client.invoices where status = "Unpaid" and days_since(due_date) > 30) == 0.
  "A stylist never booked twice at once": not any(bookings where stylist == this.stylist and id != this.id and
  overlaps(starts_at, ends_at, this.starts_at, this.ends_at)). "At most 8 children a class": count(enrolments where
  class == this.class and id != this.id) < 8. Inside count(...), names are the counted record's fields; this.x is the
  record's own.

{catalog}"""

CHECKS = """You check business apps before they're handed over. For each need below, write one worked example: what
people do in the app, step by step, and what they should see then. Use only the lists, fields, stages and buttons the
app has (below).
Steps:
- {{"do": "add", "table": list key, "as": a short name for the record (q1), "values": {{field: value}}}}: fill in every
  field marked *; a link's value is the short name of a record added before; dates as "today" or "today+3".
- {{"do": "change", "record": q1, "values": {{...}}}}, {{"do": "move", "record": q1, "to": a stage}} (by its button),
  a record the app made itself is named through its links: "j1.invoices" (the invoice made for job j1), "i1.customer";
  {{"do": "press", "record": q1, "button": its label}}, {{"do": "approve", "record": q1}}.
- Time passing: {{"do": "wait", "days": n}}: each day of it, the app's rules that run on a schedule run as they would
  (reminders, chasing, marking overdue), so what's due by then has happened. A date the app works out itself (due 14
  days after it's issued) can't be written in: {{"do": "wait", "record": q1, "until": that date's key, "days": n}}
  waits until it's n days gone (left out: the day after; 0: the day itself; -2: two days before it).
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


class Question(BaseModel):
    ask: str = Field(description="one short plain question")
    choices: list[str] = Field(description="2 to 5 short answers they can click, the usual ones for a business like theirs")
    many: bool = Field(True, description="whether they may click more than one")


class Understood(BaseModel):
    needs: list[Need]
    questions: list[Question] = Field(default_factory=list, description="only for a new app when they've said almost nothing")
    question: str = ""
    reply: str = ""
    about: list[int] = Field(default_factory=list, description="the numbers of what isn't right (if listed) that this "
                                                                "message is about: explains, or asks to be put right")


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
                + _patch_lines(registry.items("patch", "behaviour") + (m.AddList, m.AddField, m.ChangeField)) + "\n\n" + registry.catalog(("field",)))
    return registry.catalog()


def _patch_lines(classes):
    lines = [registry.TITLES["patch"] + ":"]
    for cls in classes:
        lines.append(f"- {registry._names(cls, 'patch')} {{{registry._fields_said(cls, 'patch')}}}: {registry._doc(cls)} e.g. "
                     + json.dumps(cls.__keel_example__, ensure_ascii=False))
    return "\n".join(lines)


# --- asking ---------------------------------------------------------------------------------------------------------

# How much a step may think (characters) before it's taken to have run away and is answered straight away instead: what
# it needed when it came out right, with room to spare. Round 44: the understand step never thought more than 15,000
# characters when it came out right, but ran away in 5 of 56 calls (mostly an owner's follow-up), each about 12 minutes
# before the length limit stopped it. Steps not named here think as long as their effort level takes them.
THINK_LIMITS = {"understand": 30000}


def thinks(kind):
    """Whether this step of the builder thinks before it answers (AI_THINK_STEPS, or AI_THINK_BUILDER for all of them)."""
    cfg = settings()
    steps = {x.strip().lower() for x in str(cfg.get("AI_THINK_STEPS") or "").split(",") if x.strip()}
    if not steps:
        return bool(cfg.get("AI_THINK_BUILDER"))
    return "all" in steps or (kind or "") in steps


def _ask(system, user, schema_, progress=None, tries=0, kind=None, unreadable=False):
    """One call to the model: the answer as a dict. Each `tries` asks a little differently (another seed, warmer). The
    call is kept tagged with its step and the language it was written in (training.py keeps the turns that came out
    right, so the builder's own verified work is what it learns from). An answer that can't be read at all is asked for
    once more (`unreadable`), rather than failing the owner's turn on one bad answer."""
    token = ai.again.set(tries)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    think, lost = thinks(kind), None
    ceiling = ai.think_limit.set(THINK_LIMITS.get(kind or ""))
    try:
        # thinking that went round in circles, or used up the room for the answer: answered straight away instead, not
        # asked again to think the same way (round 43: three such tries took the owner's turn 38 minutes)
        content, _, lost = ai.chat_thinking(messages, schema=schema_, on_progress=progress, think=think, purpose="builder")
        if lost is not None:
            _log.info("The %s step's thinking ran away; answering without it", kind or "builder")
            think = False
    finally:
        ai.again.reset(token)
        ai.think_limit.reset(ceiling)
    calls = ai.recording.get()
    if calls is not None and kind:
        mine = next((c for c in reversed(calls) if c.get("messages") == messages and c.get("content") == content), None)
        if mine is not None:
            mine.update(kind=f"v2_{kind}", language=registry.fingerprint(), think=think, **({"ran_away": lost} if lost else {}))
    try:
        answer = ai.parse_json(content, schema_) or {}
    except ai.AIError:
        if unreadable:
            raise
        _log.info("The %s step's answer couldn't be read; asking once more", kind or "builder")
        return _ask(system, user, schema_, progress, tries + 1, kind, unreadable=True)
    if isinstance(answer, dict):
        answer["_content"] = content
    return answer


def _ask_many(system, user, schema_, n):
    """n answers to the same question at once, each asked a little differently: for the search. Each runs in a copy of
    this turn's context, taken here (in the worker it'd be the worker's own, empty one): kept with the turn's other
    calls (training, and how long each took), and counted as this workspace's turn at the model."""
    app = current_app._get_current_object()

    def one(k, context):
        def run():
            with app.app_context():
                return _ask(system, user, schema_, tries=k + 1, kind="fix")
        return context.run(run)

    out = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
        for future in [pool.submit(one, k, contextvars.copy_context()) for k in range(n)]:
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


def _still_asked(suite, before):
    """A fix's checks, with those it took away: a ready-made feature's own go with it, but what they checked is still
    asked of the app. Acted out on the fixed app, they pass when what replaced the feature does the same job, and fail
    when it was dropped. Counted as failed unseen, a fix that swapped a feature for rules (a deposit reminder that skips
    quotes already paid) scored below the app it put right, and the owner was told no way was found."""
    have = {x.get("title") for x in suite}
    return suite + [x for x in before if x.get("title") not in have]


def _score(run, required):
    """How right a design came out: the checks required of it that pass (one it no longer has fails), then fewer
    problems found walking it."""
    passed = {x.get("title") for x, r in zip(run["examples"], run["results"]) if r["ok"]}
    return (sum(1 for title in required if title in passed), -len(run["hard"]))


def _failing(run):
    return [(x, r) for x, r in zip(run["examples"], run["results"]) if not r["ok"] and r.get("kind") in ("app", "crash")]


def _broken_examples(run):
    return [(x, r) for x, r in zip(run["examples"], run["results"]) if r.get("kind") == "example" and x.get("by") == "builder"]


def _said_failing(run, failing=None, hard=None):
    lines = []
    failing = _failing(run) if failing is None else failing
    for x, r in failing[:8]:
        steps = "; ".join(json.dumps(s, ensure_ascii=False) for s in x.get("steps", [])[:8])
        by = str(x.get("by") or "builder")
        whose = (f"the feature {by[8:]}'s own check: change that feature (send it again with its key)" if by.startswith("feature:")
                 else "your own worked example" if by == "builder" else "the owner's own check: the app must do what it says")
        lines.append(f"- “{x.get('title')}” ({whose}; steps: {steps}; expects {json.dumps(x.get('expect'), ensure_ascii=False)}): {r.get('problem')}"
                     + "".join(f"\n    why: {w}" for w in (r.get("why") or [])[:6]))
    lines += [f"- {i['text']}" for i in (run["hard"] if hard is None else hard)[:6]]
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
        self.already = set()  # the needs it said the app already met: one that does more than keep records is tried too

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

    def theirs(self):
        """The owner's own words: what they said, and the app's names (a fire alarm firm's “Trigger point” stays)."""
        bp = self.bp or {}
        return " ".join([self.question.text or "" if self.question is not None else ""]
                        + [t["label"] for t in bp.get("tables", [])]
                        + [str(x) for t in bp.get("tables", []) for f in t["fields"] for x in [f["label"], *(f.get("options") or [])]])

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

    def apply(self, patches, group, needs_text, owners=None, answers=()):
        """Apply patches (from `answers`); send the refused ones back to be corrected, twice at most. The outcome as finally saved.
        Make before break: what a need's patches take away (a rule, a feature, a field) waits while what they make for it
        is refused, and is taken away only once that's made; so a fix that would replace what works never leaves the
        app with neither."""
        owners = owners or {}
        patches = worded(patches, self.theirs())
        out = lang.apply(self.bp, patches)
        patches, owners, out, held = self._held_back(patches, owners, out)
        self.log.append({"step": group, "patches": patches, "problems": out.problems})
        self.save(out)
        self.credit(patches, out, owners)
        _mark(answers, corrected=bool(out.problems))
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
            try:
                answer = _ask(REPAIR.format(catalog=catalog(group)), user, _answer_schema(group, by_need=by_need), self.progress, kind="repair")
            except TOO_MUCH:  # what was made stays made; what was refused is said as not made yet
                self.note("Couldn't correct them this time")
                break
            sent, owners = flatten(answer) if by_need else (answer.get("patches") or [], {})
            if not sent:
                break
            sent = worded(sent, self.theirs())
            out = lang.apply(self.bp, sent)
            _mark([answer], corrected=bool(out.problems))
            self.log.append({"step": f"{group} corrected", "patches": sent, "problems": out.problems})
            self.save(out)
            self.credit(sent, out, owners)
        if held and not out.problems:  # what replaces them is made: now they go
            gone = lang.apply(self.bp, held)
            self.log.append({"step": f"{group} taken away once replaced", "patches": held, "problems": gone.problems})
            self.save(gone)
        elif held:
            self.note(f"Kept {len(held)} part{'s' if len(held) != 1 else ''} of the app that what replaces "
                      f"{'them' if len(held) != 1 else 'it'} couldn't be made yet")
        return out

    def _held_back(self, patches, owners, out):
        """(patches, owners, outcome, held): the patches as applied, with the taking-away of any need whose making was
        refused held back, when holding it back refuses nothing more."""
        def making_refused(o, sent):
            return [n for n in o.refused if 0 < n <= len(sent) and str(sent[n - 1].get("do", "")) not in REMOVING]
        failing = {owners.get(n - 1, 0) for n in making_refused(out, patches)}
        held = [i for i, p in enumerate(patches) if str(p.get("do", "")) in REMOVING and owners.get(i, 0) in failing
                and i + 1 not in out.refused]
        if not held:
            return patches, owners, out, []
        kept = [i for i in range(len(patches)) if i not in held]
        sent = [patches[i] for i in kept]
        again = lang.apply(self.bp, sent)
        if len(making_refused(again, sent)) > len(making_refused(out, patches)):  # taking away came first for a reason
            return patches, owners, out, []
        self.note(f"Keeping {len(held)} part{'s' if len(held) != 1 else ''} of the app until what replaces "
                  f"{'them' if len(held) != 1 else 'it'} is made")
        return sent, {j: owners[i] for j, i in enumerate(kept) if i in owners}, again, [patches[i] for i in held]


REMOVING = ("remove_rule", "remove_feature", "remove_validation", "remove_list", "remove_field", "remove_stage", "remove_stages",
            "remove_move", "remove_step", "remove_view")


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
    if changing:
        _refit(turn)
    mending = _mending(workspace, question) if changing else None
    before = checking.not_right(workspace) if changing else {"failing": [], "issues": [], "missing": []}
    needs, custom = [], [x for x in _kept(workspace) if x.get("by") == "builder"]
    reply = ""
    if not (mending and only_fixing(asked)):  # anything more than "fix it" is read: a change, or what should happen
        turn.note("Working out what you need")
        listed = _listed(mending["found"]) if mending and mending.get("maybe") else []
        understood = _understand(turn, asked, [said for _, _, said in listed])
        needs, reply = understood.needs, understood.reply
        if listed:  # after "Not everything works yet": only what this message is about is taken on
            picked = [listed[i - 1] for i in dict.fromkeys(understood.about) if 0 < i <= len(listed)]
            mending = {**mending, "found": {k: [x for kind, x, _ in picked if kind == k] for k in ("failing", "issues", "missing")}} if picked else None
        # three at most: four at once reads as a form to fill in (rounds 44 and 45, a beginner's eyes)
        asking = [q for q in understood.questions if q.ask.strip() and len([c for c in q.choices if c.strip()]) >= 2][:3]
        if asking and not changing and not _asked_before(workspace, turn.message):  # a new app from almost nothing: asked, once
            turn.message.text = "A few quick questions, so I build what your business needs:\n" + "\n".join(
                f"{i}. {q.ask.strip()} ({' / '.join(c.strip() for c in q.choices if c.strip())})" for i, q in enumerate(asking, start=1))
            turn.message.proposals = {"questions": [{"ask": q.ask.strip(), "choices": [c.strip() for c in q.choices if c.strip()][:5],
                                                     "many": q.many} for q in asking]}
            turn.message.status, turn.message.finished_at = "done", utcnow()
            db.session.commit()
            return
        if understood.question and not needs:
            turn.message.text, turn.message.status, turn.message.finished_at = understood.question, "done", utcnow()
            db.session.commit()
            return
        if needs:
            turn.note("What I'll make it do: " + "; ".join(n.does.rstrip(".") for n in needs[:6]))
    # what wasn't right before this turn, and isn't what it's asked to put right, stays for the owner's Fix: a change
    # does what was asked, and never more (an unasked change to who sees what, to put an old check right)
    if mending:
        before = {"failing": [x for x in before["failing"] if x not in mending["found"]["failing"]],
                  "issues": [i for i in before["issues"] if i not in mending["found"]["issues"]]}
    old_titles, old_issues = {x["title"] for x in before["failing"]}, set(before["issues"])
    asked_for = [x for x in ((mending or {}).get("found") or {}).get("missing") or [] if x.get("said")]
    if asked_for:  # the owner's checks the app doesn't do yet: what they asked for, made now as needs of their own
        known = {n.said.strip().lower() for n in needs}
        needs = list(needs) + [Need(said=x["said"], does=x["said"].strip().rstrip(".")[:160]) for x in asked_for
                               if x["said"].strip().lower() not in known]
        turn.note("Making what your checks ask for: " + "; ".join(x["said"].rstrip(".")[:80] for x in asked_for[:3]))
    needs_text = "\n".join(f"{i}. {n.does} (from: “{n.said}”)" for i, n in enumerate(needs, start=1))
    if needs:
        _make_lists(turn, needs_text)
        # how each need that's more than keeping records will be tried, written before what makes it: the builder then
        # makes what those examples need (refused when it's added, not only when it's moved on), not its own idea of it
        doing = [i for i, n in enumerate(needs, start=1) if not KEEPS_RECORDS.match(n.does.strip())]
        early = _examples_for(turn, needs, doing, "Writing how I'll check what it does") if doing else []
        turn.note("Making what it does")
        sent, owners = _make_behaviour(turn, needs, needs_text, early)
        # worked examples for the needs met by rules (a feature writes its own checks)
        by_rule = sorted({owners[i] for i, p in enumerate(sent) if isinstance(p, dict) and owners.get(i) in turn.made
                          and (p.get("do") in ("add_rule", "change_rule", "add_validation") or _formula_kept(p))}
                         # said to be met already, but more than keeping records (“needs my approval”, “pauses bookings”):
                         # tried out as well, never taken on its word
                         | {i for i in turn.already if 0 < i <= len(needs) and not KEEPS_RECORDS.match(needs[i - 1].does.strip())})
        turn.ruled |= set(by_rule)
        # the examples written first, for what a rule makes (a feature proves itself); written now for any not covered
        found = [x for x in early if x.get("need") in by_rule]
        later = [i for i in by_rule if i not in {x.get("need") for x in found}]
        if later:
            found += _examples_for(turn, needs, later, "Writing checks for what isn't a ready-made feature")
        titles = {x.get("title") for x in custom}
        custom += [{**{k: v for k, v in x.items() if k != "need"}, "by": "builder",
                    "need": needs[x["need"] - 1].does if 0 < (x.get("need") or 0) <= len(needs) else ""}
                   for x in found if x.get("title") not in titles][:6]
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
        _mark_broken({x.get("title") for x, _ in broken})
        try:
            answer = _ask(CHECKS.format(when_none=""), f"{turn.about()}\n\nThe app:\n{lang.describe(turn.bp)}\n\nThese examples couldn't be acted out. "
                          f"Send each corrected, with the same title (need 0):\n{said}", EXAMPLE_SCHEMA, turn.progress, kind="checks")
            found = answer.get("examples") or []
            _mark([answer])
        except TOO_MUCH:  # they stay as they were, said as not acted out
            found = []
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
        failing = [(x, r) for x, r in _failing(best) if x.get("title") not in old_titles]
        hard = [i for i in best["hard"] if i.get("plain") not in old_issues]
        unmade = sorted(turn.tried - turn.made)
        if not failing and not hard and not unmade:
            rounds -= 1
            break
        n = len(failing) + len(hard) + len(unmade)
        turn.note(f"Putting right {n} thing{'s' if n != 1 else ''} that didn't come out right or aren't made yet (trying {CANDIDATES} ways)")
        about = [needs[i - 1].does for i in unmade if 0 < i <= len(needs)] + [str(x.get("need") or x.get("title") or "") for x, _ in failing]
        shown = recipes.said(recipes.for_needs(about))
        user = (f"{turn.about()}\n\nThe owner's latest message: {asked}\n\nThe needs:\n{needs_text or '(as the app is)'}\n\n"
                f"The app now:\n{lang.describe(turn.bp)}\n\n"
                + (f"Didn't come out right:\n{_said_failing(best, failing, hard)}\n\n" if failing or hard else "")
                + (f"Not made yet: {', '.join(f'need {i}' for i in unmade)} (what was sent for them was refused)\n" if unmade else "")
                + (f"\n{shown}\n" if shown else ""))
        answers = _ask_many(FIX.format(catalog=catalog("all")), user, _answer_schema("all", by_need=True), CANDIDATES)
        tried = []
        for answer in answers:
            patches, owners = flatten(answer)
            if not patches:
                continue
            patches = worded(patches, turn.theirs())
            out = lang.apply(turn.bp, patches)
            turn.log.append({"step": f"fix {rounds}", "patches": patches, "problems": out.problems})
            if not out.changes:
                continue
            made = credited(patches, out, owners)
            run = _run(workspace, out.blueprint, _still_asked(_suite(workspace, out.blueprint, custom), examples), asked=asked)
            passing, issues = _score(run, required)
            tried.append(((passing + len(made["needs"] & set(unmade)), issues, answer.get("_content")), out, run, patches, owners))
        if not tried:
            break
        score, out, run, patches, owners = max(tried, key=lambda x: x[0][:2])
        if score[:2] <= _score(best, required):  # nothing tried did better: what there is stands
            break
        _chosen(score[2], corrected=bool(out.problems))
        turn.save(out)
        turn.credit(patches, out, owners)
        best = run
    if turn.changes and _refit(turn, [(x, r) for x, r in zip(best["examples"], best["results"])
                                      if r.get("kind") == "example" and checking.theirs(x)]):
        best = _run(workspace, turn.bp, _suite(workspace, turn.bp, custom), asked=asked)  # this turn's changes left them behind
    if asked_for and turn.changes:  # the owner's checks for them are tried again, as the owner would
        checking.try_again_missing(workspace)
    _hand_over(turn, needs, best, reply=reply, first=first, rounds=rounds, custom=custom)


TOO_MUCH = (ai.CutOff, ai.Stuck)


def _asked_before(workspace, message):
    """Whether the builder has already asked this owner its quick questions (they're asked once)."""
    return db.session.query(Message.id).filter(Message.workspace_id == workspace.id, Message.channel == "builder",
                                               Message.role == "assistant", Message.id != message.id,
                                               Message.proposals.isnot(None)).first() is not None


def _formula_kept(patch):
    """Whether a feature holds to a formula (a gate blocked while the client owes): its own checks can't be written for
    sure, so the need gets a worked example like a rule's."""
    return any(isinstance(c, str) for k in ("only_if", "blocked_if") for c in (patch.get(k) or []))


def _refit(turn, found=None):
    """The owner's checks that no longer fit the app (a field they named renamed, something now needed not given),
    written again from their own words for the app as it is: the turn then puts right what they find, rather than
    nothing being tried at all. `found`: [(check, result)]; left out, those that didn't fit when last tried (and the
    app is checked again with them). Returns whether any were."""
    last = found is None
    found = checking.stale(turn.workspace) if last else found
    if not found:
        return False
    turn.note(f"Rewriting {len(found)} of your checks for your app as it is now (it changed since they were written)")
    done = checking.refit(turn.workspace, found)
    if done and last:
        checking.check(turn.workspace, "your checks were rewritten for your app as it is now")
    return bool(done)


def _listed(found):
    """What isn't right, one by one, as the understand step is shown it: [(kind, item, words)]."""
    return ([("failing", x, f"the check “{x['title']}” doesn't come out right: {x['why']}") for x in found.get("failing") or []]
            + [("issues", i, f"trying the app out found: {i}") for i in found.get("issues") or []]
            + [("missing", x, f"the owner's check “{x['said']}” isn't something the app does yet") for x in found.get("missing") or []])[:12]


def _understand(turn, asked, open_items=()):
    """The owner's needs: asked once, then again a little differently, then for fewer, if the answer runs away (a model
    repeating itself, or writing more than fits). `open_items`: what isn't right now, for "about" to say which this
    message is about."""
    shown = ("\n\nWhat isn't right in the app now:\n" + "\n".join(f"{i}. {x}" for i, x in enumerate(open_items, start=1))
             + "\nIn \"about\", put the numbers of those this message is about (it says what should happen there, or asks for "
               "it to be put right); none when it asks for something else. Its needs are only what the message asks.") if open_items else ""
    user = (f"{turn.about()}\n\nThe app now:\n{lang.describe(turn.bp)}\n\nEarlier:\n{turn.history() or '(nothing yet)'}{shown}\n\n"
            f"The owner says: {asked}")
    for tries, extra in ((0, ""), (1, ""), (2, "\n\n(Keep it short: at most six needs, one sentence each.)")):
        try:
            answer = _ask(UNDERSTAND, user + extra, Understood.model_json_schema(), turn.progress, tries, kind="understand")
            understood = Understood.model_validate(answer)
            _mark([answer])
            return understood
        except TOO_MUCH:
            turn.note("That came out too long; asking again more briefly")
    raise ai.Stuck("I got stuck working out what you need. Press Try again, or say it again in fewer words.")
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
    data = {}
    for tries, extra in ((0, ""), (1, "\n\nName every list in \"lists\", but send at most four add_list patches in this answer (the first "
                                     "four not made yet); the rest are asked for next."),
                         (2, "\n\nThis time only name, in \"lists\", every list the needs require (the key each would have), and "
                             "send no patches: they're made a few at a time next.")):
        try:
            data = ask(extra, tries=tries)
            break
        except TOO_MUCH:  # too much to write at once: a few at a time, then the names alone
            turn.note("That was too much in one go; making the lists a few at a time")
    if data.get("patches"):
        turn.apply(data["patches"], "data", needs_text, answers=[data])
    named = list(data.get("lists") or [])
    size = 4
    for n in range(8):
        missing = [name for name in named if Design(turn.bp).list(name) is None]
        if not missing:
            return
        turn.note(f"Making the list{'s' if len(missing) != 1 else ''} still to make: {', '.join(missing)}")
        try:
            more = ask(f"\n\nThese lists are named as needed but aren't made yet: {', '.join(missing)}. Send an add_list for at most "
                       f"{size} of them now (only those).", tries=min(n + 1, 2))
        except TOO_MUCH:
            size = max(1, size // 2)  # still too much: fewer at a time
            continue
        if not more.get("patches"):
            return
        turn.apply(more["patches"], "data", needs_text, answers=[more])


# what people read in the app that the builder writes (a field's label and help, a message, an email), in the app's
# words: a restock level that "sets off" an alert, never one that "triggers" it, unless that's the owner's own word
WORDED = {"label", "singular", "help", "message", "subject", "body", "name", "description"}


def worded(patches, theirs=""):
    from .chat import OURS
    words = {k: v for k, v in OURS.items() if k not in ("table", "tables", "field", "fields")}  # "Field visit" is a word too
    kept = {w for w in words if re.search(rf"\b{re.sub(r'(s|ed|ing)$', '', w)}", theirs or "", re.I)}  # any form of their word
    found = re.compile(r"\b(" + "|".join(words) + r")\b", re.I)

    def said(m):
        word = m.group(0)
        if word.lower() in kept:
            return word
        new = words[word.lower()]
        return new[:1].upper() + new[1:] if word[:1].isupper() else new

    def walk(x, key=None, when=False):
        if isinstance(x, dict):
            return {k: walk(v, k, when or k == "when") for k, v in x.items()}
        if isinstance(x, list):
            return [walk(v, key, when) for v in x]
        if isinstance(x, str) and key in WORDED and not when:  # the words around a {{ }}, never inside it
            return "".join(part if part.startswith("{{") else found.sub(said, part) for part in re.split(r"(\{\{.*?\}\})", x))
        return x
    return [walk(p) for p in patches]


# a need that's met by keeping records in lists ("Records bookings", "Shows which invoices are unpaid"): the lists are it
KEEPS_RECORDS = re.compile(r"(records?|tracks?|lists?|stores?|shows?|keeps?|logs?|holds?|manages?|lets?|views?|sees?|organi[sz]es?|"
                           r"captures?|saves?|notes?|links?)\b", re.I)


def _examples_for(turn, needs, which, note):
    """Worked examples for these needs (their numbers), on the app as it is now: [{title, steps, expect, need}]."""
    turn.note(note)
    wanted = "\n".join(f"{i}. {needs[i - 1].does}" for i in which if 0 < i <= len(needs))
    try:
        answer = _ask(CHECKS.format(when_none=""), f"{turn.about()}\n\nThe needs to check:\n{wanted}\n\nThe app:\n{lang.describe(turn.bp)}",
                      EXAMPLE_SCHEMA, turn.progress, kind="checks")
    except (ai.CutOff, ai.Stuck):  # no worked examples this time: those needs are said as not tried out
        return []
    found = answer.get("examples") or []
    _mark([answer])
    return [x for x in found if isinstance(x, dict) and x.get("steps") and x.get("expect") and isinstance(x.get("need"), int)]


def _targets(examples):
    """The examples written before the behaviour, as the behaviour step is shown them."""
    if not examples:
        return ""
    lines = [f"- need {x['need']}: “{x.get('title')}”: " + "; ".join(json.dumps(st, ensure_ascii=False) for st in x["steps"][:8])
             + f" → expects {json.dumps(x['expect'], ensure_ascii=False)}" for x in examples[:8]]
    return ("\n\nOnce it's made, each need will be tried like this, so make what these need (a step with \"refused\": true "
            "must be stopped by the app, however it's done):\n" + "\n".join(lines))


def _make_behaviour(turn, needs, needs_text, early=()):
    """(patches, {index: need}) for what the app does: all needs at once, or one need at a time if that's too much."""
    shown = recipes.said(recipes.for_needs([n.does for n in needs]))  # how needs like these are made, known to work
    shown = (f"\n\n{shown}" if shown else "") + _targets(list(early))

    def ask(extra="", by_need=True, tries=0):
        return _ask(BEHAVIOUR.format(catalog=catalog("behaviour")), f"{turn.about()}\n\nThe needs:\n{needs_text}\n\nThe app now:\n"
                    f"{lang.describe(turn.bp)}{shown}{extra}", _answer_schema("behaviour", by_need=by_need), turn.progress, tries, kind="behaviour")
    used = []
    try:
        answer = ask()
        used.append(answer)
        sent, owners = flatten(answer)
        already = {int(i) for i in answer.get("already") or [] if str(i).isdigit()}
        silent = [i for i in range(1, len(needs) + 1) if i not in set(owners.values()) | already]
        if silent:  # a need neither made nor said to be met already: asked for once more, those alone
            turn.note(f"Making what's still missing: {'; '.join(needs[i - 1].does.rstrip('.') for i in silent[:3])}")
            try:
                more = ask(f"\n\nYou sent nothing for need{'s' if len(silent) != 1 else ''} {', '.join(map(str, silent))}. Send the patches that "
                           f"make each happen (a feature whenever one fits), or put its number in \"already\" if the app as it is "
                           f"already does it.", tries=1)
            except TOO_MUCH:  # what the first answer made stays; these are said as not made yet
                more = {}
            used.append(more)
            more_sent, more_owners = flatten(more)
            already |= {int(i) for i in more.get("already") or [] if str(i).isdigit()}
            owners.update({len(sent) + k: v for k, v in more_owners.items()})
            sent = sent + more_sent
        turn.tried |= {i for i in range(1, len(needs) + 1) if i not in already}  # what's neither made nor already met is said
        turn.already |= already
    except TOO_MUCH:
        turn.note("That was too much in one go; making it one need at a time")
        sent, owners = [], {}
        for i, n in enumerate(needs, start=1):
            try:
                one = ask(f"\n\nNow only need {i}: {n.does}. Send just the patches for it (none if the lists already meet it).",
                          by_need=False, tries=1)
            except TOO_MUCH:
                continue
            used.append(one)
            for p in one.get("patches") or []:
                owners[len(sent)] = i
                sent.append(p)
    if sent:
        turn.apply(sent, "behaviour", needs_text, owners, answers=used)
    else:  # every need met already: the answer said so, and that's what was done
        _mark(used)
    return sent, owners


def _not_yet_said(wrong, issues, unmade, again=False):
    """What isn't right yet, in plain words: what came out wrong when tried, then what couldn't be made at all. `again`:
    a later turn that changed nothing, so another go the same way won't help (round 43: three Fix presses, 25 minutes
    each, nothing changed): it asks the owner to say what should happen instead of offering Fix again."""
    parts = []
    said = [f"“{r['title'].rstrip('.')}”" for r in wrong[:3]] + [i.rstrip(".") for i in issues[:max(0, 3 - len(wrong))]]
    more = len(wrong) + len(issues) - len(said)
    if said:
        parts.append(f"When I tried it out, {'this' if len(said) + more == 1 else 'these'} didn't come out right: "
                     + "; ".join(said) + (f", and {more} more" if more > 0 else "") + ".")
    if unmade:
        parts.append(f"I couldn't make {'this' if len(unmade) == 1 else 'these'} yet: " + "; ".join(unmade) + ".")
    if again:
        return ("Not everything works yet. " + " ".join(parts) + " I tried to put this right but couldn't find a way that "
                "works, so trying again the same way won't help. Tell me in your own words what should happen (who does what, "
                "and what should stop it) and I'll build that.")
    return ("Not everything works yet. " + " ".join(parts) + " Press “Fix these” and I'll have another go, or tell me what "
            "should happen.")


def _chosen(content, corrected=False):
    """The candidate fix that was kept, marked among the turn's calls (the others weren't the builder's best)."""
    for c in ai.recording.get() or []:
        if c.get("kind") == "v2_fix" and c.get("content") == content:
            c.update(chosen=True, applied=True, **({"corrected": True} if corrected else {}))


def _mark(answers, corrected=False):
    """What became of answers (each as _ask gave it), kept on its call among the turn's for training.py to grade the
    lessons by: it went into the app (applied), and the platform refused some of it, or it was put right after
    (corrected). An answer never marked wasn't used: it isn't a lesson."""
    contents = {a.get("_content") for a in answers if isinstance(a, dict) and a.get("_content")}
    for c in ai.recording.get() or []:
        if c.get("content") in contents:
            c["applied"] = True
            if corrected:
                c["corrected"] = True


def _mark_broken(titles):
    """The worked examples that couldn't be acted out: the answers that wrote them needed putting right."""
    for c in ai.recording.get() or []:
        if c.get("kind") == "v2_checks":
            said = ai.parse_json(c.get("content") or "") or {}
            if isinstance(said, dict) and any(isinstance(x, dict) and x.get("title") in titles for x in said.get("examples") or []):
                c["corrected"] = True


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
            # keeping or showing records is what the lists do: what was refused for it was extra (a page), not the need
            states.append((n, "ok" if KEEPS_RECORDS.match(n.does.strip()) and (turn.bp.get("tables") or []) else "unmade"))
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


def _gaps(turn, states, run):
    """What the builder couldn't make, or made wrong, with what it tried and what the platform said: kept where whoever
    runs the platform reads it (DATA_DIR/training/gaps.jsonl, `python -m app.builder.training gaps`), so what the
    language can't say yet is a list, never a guess. Nothing here stops a turn."""
    left = [(n, state) for n, state in states if state in ("unmade", "wrong")]
    if not left:
        return
    try:
        from pathlib import Path
        folder = Path(settings()["DATA_DIR"]) / "training"
        folder.mkdir(parents=True, exist_ok=True)
        refused = [p for entry in turn.log for p in entry.get("problems") or []][-8:]
        failing = [f"{x.get('title')}: {r.get('problem')}" for x, r in _failing(run)][:6] if run else []
        with open(folder / "gaps.jsonl", "a") as f:
            for need, state in left:
                f.write(json.dumps({"at": utcnow().isoformat(timespec="seconds"), "workspace": turn.workspace.id, "need": need.does,
                                    "said": need.said, "state": state, "refused": refused, "failing": failing,
                                    "language": registry.fingerprint()}, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 - keeping the list is a help, never a reason a turn fails
        _log.warning("Keeping what the builder couldn't make failed", exc_info=True)


def _hand_over(turn, needs, run, reply="", first=None, rounds=0, custom=(), stopped=None):
    from .chat import _not_yet, navigate_url, plain
    workspace, message = turn.workspace, turn.message
    shown, issues, wrong = [], [], []
    if run is not None:
        results = list(zip(run["examples"], run["results"]))
        playable = [(x, r) for x, r in results if r.get("kind") not in ("example", "crash") or x.get("by") not in ("builder",)]
        wrong = [r for _, r in playable if not r["ok"]]
        issues = [i["plain"] for i in run["hard"]]
        shown = [{"title": r["title"], "ok": r["ok"], "lines": [[ok, line] for ok, line in r["lines"]], "problem": r.get("plain") or r.get("problem") or "", "example": x}
                 for x, r in playable]
        state = dict(workspace.state or {})
        state["examples"] = [trial.dated(x) for x, r in playable if x.get("by") != "builder" or r["ok"] or x in custom][:checking.KEEP]
        workspace.state = state
        db.session.commit()
        checking.remember(workspace, run["walked"], [r for _, r in playable], "this change", [x for x, _ in playable], tell=False)
    states = _need_states(turn, needs, run)
    _gaps(turn, states, run)
    unmade = [n.does.rstrip(".") for n, s in states if s == "unmade"]
    lines = [f"- {n.does.rstrip('.')}" + {"ok": "", "wrong": " (not right yet)", "unmade": " (not made yet)",
                                           "unchecked": " (made, but I couldn't try it out: check it yourself)"}[s] for n, s in states]
    if wrong or issues or unmade:
        text = _not_yet_said(wrong, issues, unmade, again=bool(message.version_before) and not turn.changes)
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
