"""The AI: models on servers the platform runs itself or a cloud provider's, reached through a gateway.

Where the calls go (app/ai_providers.py has the catalog, and which one each purpose uses):
- Ollama on this computer, for development and one business on one machine (/api/chat, /api/embed).
- Your own OpenAI-compatible server (vLLM, SGLang, llama.cpp's server, LM Studio, LiteLLM). Production runs vLLM or
  SGLang on its own GPU servers: they batch many customers' requests together, keep shared prompt prefixes cached and
  constrain JSON quickly.
- A cloud provider: OpenAI, Anthropic (Claude), Google (Gemini), Azure OpenAI, Mistral, Groq, DeepSeek, xAI, OpenRouter,
  Together, Fireworks, Cerebras, Cohere. Nothing goes to one unless a platform admin chooses it (Settings › AI) or the
  .env file names it, and the platform says so wherever the AI is shown.
Three ways of asking cover them all: Ollama's own, OpenAI's chat completions and Anthropic's messages. What one provider
or model doesn't take (a temperature, a way of switching thinking, a JSON schema too big for it) it says when it refuses
the request; the part it named is sent another way, or left out, and that's remembered for that model (_negotiate).

Each purpose can have its own model, and its own server: the builder a big model (AI_MODEL_BUILDER, AI_BASE_URL_BUILDER),
quick jobs a small one (AI_MODEL_FAST), the in-app assistant and background work AI_MODEL.

`chat` streams the model's answer, calling `on_progress(thinking, content)` as it goes so the builder can show what the
model is doing while it works. A model that can't be reached raises AIUnavailable: the work waits and tries again
later instead of failing.

The gateway. Every call waits its turn in this process's queue for the server it goes to (one queue per server):
- At most AI_CONCURRENCY calls go at once (2 for Ollama, 8 for any other server, unless set). Sending more
  only makes every answer slower, and a call still waiting here can be put ahead of others; one already sent can't.
- Strict priority: an owner waiting on a screen or a question ("interactive") goes before the builder's work ("build"),
  which goes before work nobody is watching ("background").
- Within a priority, workspaces take turns, the one with fewest calls already running first, so one customer's eight
  parallel builder calls can't keep another customer's single call waiting behind all of them.
- A deadline for the whole call (AI_DEADLINE: its wait for a turn, its retries and the answer), as well as the read
  timeout (AI_READ_TIMEOUT: that long with nothing from the server and it's taken as gone).
- Retries with a growing wait when the server can't be reached or says it's busy or broken (429, 5xx), never when it
  refused the request itself (another 4xx: the same request gets the same answer). Meanwhile a server that can't be
  reached is skipped by every other call for DOWN_FOR seconds, so they don't each wait out the same timeouts.

The queue is per process, so it can't be the limit across processes: several web and worker processes, on several
machines, each have their own, and together they may send more than AI_CONCURRENCY at once. The model server's own queue
is that limit (vLLM's --max-num-seqs, SGLang's --max-running-requests, OLLAMA_NUM_PARALLEL): it holds what it can't start
yet, or answers 429 or 503 when it's full, which the retries here wait out. Set AI_CONCURRENCY to about the server's
limit divided by the processes that use it. (vLLM with --scheduling-policy priority can also order every process's
requests by how soon they're needed: AI_SERVER_PRIORITY=true sends each call's priority along.)

`stats()` counts what each purpose asked of each provider since the process started; Settings › Health shows them.
"""
import contextlib
import contextvars
import hashlib
import json
import logging
import os
import random
import re
import threading
import time
from collections import deque, namedtuple

import requests

from . import ai_providers as providers
from .core.settings import settings

log = logging.getLogger("ai")


class AIError(Exception):
    """The model answered, but not with something usable (or the model server refused the request)."""


class AIUnavailable(AIError):
    """The model can't be reached right now (its server not running, the computer asleep, too busy). The work waits and
    retries."""


class Late(AIUnavailable):
    """The call took longer than AI_DEADLINE, waiting its turn or answering: the AI is busy, or it was a lot to ask at
    once. Like any AIUnavailable the work waits and tries again; the server isn't taken as down, since it is answering."""


class Stuck(AIError):
    """The model kept writing the same passage over and over (asking again, smaller, usually gets past it)."""


class CutOff(AIError):
    """The answer reached the length limit before it was finished: `content`, what of the answer was written by then
    (empty when the model was still thinking), and `thinking`."""

    def __init__(self, message, content="", thinking=""):
        super().__init__(message)
        self.content, self.thinking = content, thinking


class _Named(AIError):
    """The provider has the model asked for, under its own name ("claude-sonnet-5-5" for a typed "claude-sonnet-5.5"):
    the call is sent again with that name, and the name is remembered (`_named`)."""

    def __init__(self, model):
        super().__init__(model)
        self.model = model


_named = {}  # (provider, address, the model's name as chosen) -> the provider's own name for it


DOWN_FOR = 20  # after a failed connection, skip the model for this many seconds instead of waiting out timeouts again
_down = {}
_backend = None  # tests plug in a function here
_embedder = None
# A builder turn's conversations with the model and what it answered, kept (with how they turned out) to train a model
# that builds better: a list while a turn records, None otherwise.
recording = contextvars.ContextVar("recording", default=None)
# The builder at work (a turn, or writing an owner's check): it asks with less chance and a fixed seed, so the same
# request gets the same design, not a different one each time it's asked (AI_BUILDER_TEMPERATURE, AI_BUILDER_SEED).
building = contextvars.ContextVar("building", default=False)
# how many times the builder has asked again after the model went round in a loop: each new go a little less fixed
# (warmer, another seed), so it doesn't take the same path into the same loop
again = contextvars.ContextVar("again", default=0)

PURPOSES = ("builder", "fast", "assistant", "background")
PRIORITIES = ("interactive", "build", "background")  # the order they're served in
PRIORITY_OF = {"builder": "build", "fast": "interactive", "assistant": "interactive", "background": "background"}
# The worker's threads (app/worker.py names them) say what a call on them is for, and how soon it's needed, when the
# call doesn't say itself: the assistant answers someone who's waiting; jobs, rules and reading are background work.
THREADS = {"builder": ("builder", "build"), "assistant": ("assistant", "interactive"), "jobs": ("background", "background"),
           "automations": ("background", "background"), "knowledge": ("background", "background")}
_asking = contextvars.ContextVar("asking", default=None)
WAIT_NOTE_EVERY = 2.0  # seconds between telling on_progress a call is still waiting its turn


def set_backend(fn):
    """Tests: replace the model with fn(messages, schema, on_progress) -> (content, thinking)."""
    global _backend
    _backend = fn


def set_embedder(fn):
    """Tests: replace the embedding model with fn(texts) -> [[float]]."""
    global _embedder
    _embedder = fn


@contextlib.contextmanager
def asking(*, purpose=None, priority=None, workspace_id=None):
    """For every call to the AI inside the block, say what it's for, how soon it's needed and whose it is, where the calls
    themselves don't (a job: `with ai.asking(priority="background", workspace_id=job.workspace_id): ...`). What a call
    says itself comes first, and the builder at work always asks as the builder."""
    _valid(purpose, priority)
    said = {k: v for k, v in (("purpose", purpose), ("priority", priority), ("workspace_id", workspace_id)) if v is not None}
    token = _asking.set({**(_asking.get() or {}), **said})
    try:
        yield
    finally:
        _asking.reset(token)


def _valid(purpose, priority):
    if purpose is not None and purpose not in PURPOSES:
        raise ValueError(f"purpose is one of {', '.join(PURPOSES)}, not {purpose!r}")
    if priority is not None and priority not in PRIORITIES:
        raise ValueError(f"priority is one of {', '.join(PRIORITIES)}, not {priority!r}")


def _who(purpose=None, priority=None, workspace_id=None):
    """(purpose, priority, workspace) of a call: what the caller said, or else what the work around it says (the builder
    at work, `asking`, the worker thread it's on), or else the assistant's. The workspace is the request's (g.ws) when a
    person's request has one; None (one shared turn) when nothing says."""
    _valid(purpose, priority)
    said = _asking.get() or {}
    thread = THREADS.get(threading.current_thread().name)
    purpose = purpose or ("builder" if building.get() else None) or said.get("purpose") or (thread[0] if thread else "assistant")
    priority = priority or said.get("priority") or (thread[1] if thread else None) or PRIORITY_OF[purpose]
    if workspace_id is None:
        workspace_id = said.get("workspace_id")
    if workspace_id is None:
        from .core import actor
        workspace_id = actor.workspace_id()
    return purpose, priority, workspace_id


# Where calls go ---------------------------------------------------------------------------------------------------

Route = namedtuple("Route", "provider url key model")
PROVIDERS = tuple(providers.CATALOG)


def _route(purpose):
    """Where a purpose's calls go: its provider, server, key and model (app/ai_providers.py: the platform's choice under
    Settings › AI, else AI_MODEL_BUILDER, AI_BASE_URL_FAST and the like where they're set, else the plain AI_* settings).
    A cloud provider's model may be "": its best is found when it's asked (_model)."""
    return Route(*providers.resolve(purpose))


def _provider(route):
    return providers.CATALOG.get(route.provider)


def _headers(route):
    return providers.headers(_provider(route), route.key)


def describe():
    """What the AI is, for the screens that show it: {"model", "url", "provider", "name" (the provider's, as people know
    it), "cloud" (whether what's asked leaves the platform's own machines), "where", "think"}, with "builder" and "fast"
    where they go somewhere else, and "embed" (search by meaning: its model, or None for words only)."""
    cfg = settings()
    route = _route("assistant")
    out = {**_described(route), "think": cfg["AI_THINK"]}
    for purpose in ("builder", "fast"):  # a purpose sent to a model or server of its own
        other = _route(purpose)
        if (other.provider, other.model, other.url) != (route.provider, route.model, route.url):
            out[purpose] = _described(other)
    embedding = _route("embed")
    out["embed"] = _described(embedding) if embedding.model and embedding.provider != providers.WORDS_ONLY else None
    out["any_cloud"] = any(x.get("cloud") for x in [out, out.get("builder") or {}, out.get("fast") or {}, out.get("embed") or {}])
    return out


def _described(route):
    p = _provider(route)
    host = re.sub(r"^\w+://", "", route.url or "").split("/")[0]
    local = host.startswith(("127.", "localhost", "[::1]")) or not host
    return {"model": route.model or ("its best model" if p and p.cloud else ""), "url": route.url, "provider": route.provider,
            "name": p.name if p else route.provider, "cloud": bool(p and p.cloud),
            "where": "on this computer" if local and not (p and p.cloud) else (f"at {p.name}" if p and p.cloud else f"at {host}")}


def _unavailable(url, why):
    reason = f"the AI at {url} isn't answering ({why})"
    _down[url] = (time.monotonic() + DOWN_FOR, reason)
    return AIUnavailable(reason)


def _skip_if_down(url):
    until, reason = _down.get(url, (0, ""))
    if time.monotonic() < until:
        raise AIUnavailable(reason)


def _no_key(route):
    p = _provider(route)
    return AIUnavailable(f"there's no key for {p.name} yet: a platform admin adds one under Settings › AI"
                         + (f" (or {p.keys[0]} in the .env file)" if p.keys else ""))


def check():
    """Raise AIUnavailable unless every model server the AI uses answers: the default one, and the builder's, quick jobs'
    and the embedding model's where they're set apart. A cloud provider is asked which models it has, which says the
    key works too."""
    if _backend:
        return
    routes = {}
    for purpose in ("assistant", "builder", "fast", "embed"):
        route = _route(purpose)
        if purpose == "embed" and (not route.model or route.provider == providers.WORDS_ONLY):
            continue
        routes.setdefault((route.provider, route.url, route.key), route)
    for route in routes.values():
        p = _provider(route)
        if p is None:
            raise AIUnavailable(f"AI_PROVIDER is {route.provider!r}: it can be one of {', '.join(PROVIDERS)}")
        if p.needs_key and not route.key:
            raise _no_key(route)
        _skip_if_down(route.url)
        try:
            providers._list(p, route.url, route.key)
        except providers.KeyRefused as exc:  # it's there, but turned us away (a wrong key): not down
            raise AIUnavailable(f"{exc}: check the key under Settings › AI") from exc
        except (requests.RequestException, ValueError) as exc:
            raise _unavailable(route.url, type(exc).__name__) from exc
        _down.pop(route.url, None)


# Saving before asking ---------------------------------------------------------------------------------------------

def _holding_writes():
    """Whether this thread has written to the database without committing. Asking the AI now (minutes, on a busy
    computer) would hold what it wrote locked all that while: anyone saving the same records waits, and a failure later
    would undo work that was done. (Each connection notes when it writes, below.)"""
    from .extensions import db
    try:
        session = db.session()
        if not session.in_transaction():
            return False
        return bool(session.connection().info.get("wrote"))
    except Exception:
        return False


def _note_writes():
    """Every database connection notes when it writes, and forgets on commit or rollback (_holding_writes reads it)."""
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    @event.listens_for(Engine, "before_cursor_execute")
    def _wrote(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip()[:6].upper() in ("INSERT", "UPDATE", "DELETE") or statement.lstrip()[:8].upper() == "TRUNCATE":
            conn.info["wrote"] = True

    for name in ("commit", "rollback"):
        event.listen(Engine, name, lambda conn: conn.info.pop("wrote", None))


_note_writes()


def _let_go():
    """Save what this thread has written before the AI is asked (see _holding_writes); never inside a savepoint, whose
    work must still be able to be undone as one."""
    from .extensions import db
    if not _holding_writes():
        return
    if settings().get("TESTING"):
        raise AssertionError("the AI was asked while this thread held unsaved writes: commit first")
    log.warning("The AI was asked while unsaved writes held the database", stack_info=True)
    if not db.session().in_nested_transaction():
        db.session.commit()


# The gateway ------------------------------------------------------------------------------------------------------

class Deadline:
    """When a call must be done by (AI_DEADLINE seconds from when it was asked), and how to say it wasn't."""

    def __init__(self, seconds):
        self.seconds, self.ends = seconds, time.monotonic() + seconds

    def left(self):
        return self.ends - time.monotonic()

    def waiting(self, waited):
        return Late(f"The AI is busy: this waited {waited:.0f} seconds for its turn, longer than allowed "
                    f"(AI_DEADLINE, {self.seconds:.0f} seconds).")

    def answering(self):
        return Late(f"The AI took longer than allowed to answer (AI_DEADLINE, {self.seconds:.0f} seconds): it's busy, "
                    "or this was a lot to ask at once.")


class _Ticket:
    __slots__ = ("priority", "workspace", "turn", "granted")

    def __init__(self, priority, workspace):
        self.priority, self.workspace = priority, workspace
        self.turn, self.granted = threading.Event(), False


class Gate:
    """One process's queue for one model server (see the top of this module): at most `limit` calls at once; the highest
    priority first; within a priority, the workspace with fewest calls running, then the one whose last turn was
    longest ago."""

    def __init__(self, name, limit):
        self.name, self.limit = name, limit
        self.lock = threading.Lock()
        self.running = 0
        self.running_for = {}  # workspace -> its calls running now
        # priority -> {workspace: its waiting calls, oldest first}. A dict keeps the order workspaces take turns in: one
        # that's just had a turn goes to the back, one that starts waiting joins at the back.
        self.waiting = {p: {} for p in PRIORITIES}

    def wait(self, priority, workspace, deadline, on_wait=None):
        """Wait for a turn and return the seconds waited (call done() after). Raises Late when the deadline comes first.
        on_wait(running, waiting) is told how busy the server is, now and every few seconds while this waits."""
        ticket = _Ticket(priority, workspace)
        with self.lock:
            self.waiting[priority].setdefault(workspace, deque()).append(ticket)
            self._hand_out()
        if ticket.granted:
            return 0.0
        began = time.monotonic()
        try:
            while True:
                if on_wait:
                    on_wait(*self._busy())
                if ticket.turn.wait(max(0.0, min(WAIT_NOTE_EVERY, deadline.left()))):
                    return time.monotonic() - began
                if deadline.left() <= 0:
                    raise deadline.waiting(time.monotonic() - began)
        except BaseException:
            with self.lock:
                if ticket.granted:  # its turn came just as it gave up: pass the turn on
                    self._release(workspace)
                else:
                    self._forget(ticket)
            raise

    def done(self, workspace):
        with self.lock:
            self._release(workspace)

    def resize(self, limit):
        with self.lock:
            self.limit = limit
            self._hand_out()

    def _busy(self):
        with self.lock:
            waiting = sum(len(q) for queues in self.waiting.values() for q in queues.values())
            return self.running, max(0, waiting - 1)  # the others: not counting the one asking

    def _release(self, workspace):
        self.running -= 1
        left = self.running_for.get(workspace, 1) - 1
        if left > 0:
            self.running_for[workspace] = left
        else:
            self.running_for.pop(workspace, None)
        self._hand_out()

    def _forget(self, ticket):
        queues = self.waiting[ticket.priority]
        queue = queues.get(ticket.workspace)
        if queue and ticket in queue:
            queue.remove(ticket)
            if not queue:
                del queues[ticket.workspace]

    def _hand_out(self):
        while self.running < self.limit:
            queues = next((self.waiting[p] for p in PRIORITIES if self.waiting[p]), None)
            if queues is None:
                return
            workspace = min(queues, key=lambda w: self.running_for.get(w, 0))  # the first of the fewest: turn order breaks ties
            queue = queues.pop(workspace)
            ticket = queue.popleft()
            if queue:
                queues[workspace] = queue  # to the back, for its next turn
            ticket.granted = True
            self.running += 1
            self.running_for[workspace] = self.running_for.get(workspace, 0) + 1
            ticket.turn.set()


_gates = {}  # (provider, url) -> Gate
_gates_lock = threading.Lock()


def _gate(route):
    limit = max(1, int(settings().get("AI_CONCURRENCY") or 0) or (2 if route.provider == "ollama" else 8))
    with _gates_lock:
        gate = _gates.get((route.provider, route.url))
        if gate is None:
            gate = _gates[(route.provider, route.url)] = Gate(f"{route.provider} at {route.url}", limit)
    if gate.limit != limit:
        gate.resize(limit)
    return gate


# Counters ---------------------------------------------------------------------------------------------------------

_counts = {}  # (purpose, provider) -> what it's done since the process started
_counts_lock = threading.Lock()
_since = time.time()


def _count(purpose, route, *, waited=0.0, took=None, usage=None, failed=None):
    usage = usage or {}
    with _counts_lock:
        c = _counts.setdefault((purpose, route.provider), {
            "model": "", "calls": 0, "answered": 0, "failures": 0, "failed": {}, "retries": 0, "seconds": 0.0, "slowest": 0.0,
            "waited": 0.0, "longest_wait": 0.0, "tokens_in": 0, "tokens_out": 0})
        c["model"] = route.model
        c["calls"] += 1
        c["retries"] += usage.get("retries", 0)
        c["waited"] += waited
        c["longest_wait"] = max(c["longest_wait"], waited)
        c["tokens_in"] += usage.get("in") or 0
        c["tokens_out"] += usage.get("out") or 0
        if failed:
            c["failures"] += 1
            c["failed"][failed] = c["failed"].get(failed, 0) + 1
        else:
            c["answered"] += 1
            c["seconds"] += took or 0.0
            c["slowest"] = max(c["slowest"], took or 0.0)


def stats():
    """What the AI has been asked since this process started, per purpose ("embed" for embeddings) and provider: calls,
    those answered and those that failed (by kind), retries, seconds answering (in all, on average, the slowest), seconds
    waiting for a turn, and tokens in and out where the server reports them. With each server's queue as it is now."""
    with _counts_lock:
        calls = [{"purpose": purpose, "provider": provider, **c, "failed": dict(c["failed"]),
                  "average": c["seconds"] / c["answered"] if c["answered"] else 0.0,
                  "average_wait": c["waited"] / c["calls"] if c["calls"] else 0.0}
                 for (purpose, provider), c in sorted(_counts.items())]
    with _gates_lock:
        gates = list(_gates.values())
    queues = []
    for gate in gates:
        with gate.lock:
            queues.append({"server": gate.name, "limit": gate.limit, "running": gate.running,
                           "waiting": {p: sum(len(q) for q in gate.waiting[p].values()) for p in PRIORITIES}})
    return {"since": _since, "calls": calls, "queues": queues}


def _after_fork():
    """A process forked from this one (a preforking web server) starts with empty queues and fresh locks: the threads
    that held a turn or a lock here weren't copied over."""
    global _gates_lock, _counts_lock
    _gates.clear()
    _gates_lock, _counts_lock = threading.Lock(), threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


# Asking -----------------------------------------------------------------------------------------------------------

def chat(messages, *, schema=None, think=None, on_progress=None, purpose=None, priority=None, workspace_id=None):
    """Send a conversation, return (content, thinking). With a JSON schema, the answer is constrained to it.

    purpose picks the model ("builder", "fast", "assistant", "background"), priority the place in the queue
    ("interactive", "build", "background"), workspace_id whose turn it is; each, when not given, comes from where it's
    asked (see _who)."""
    if settings().get("TRYING_OUT"):  # a throwaway copy of an app being tried: its AI steps wait, as if it were off
        raise AIUnavailable("the AI isn't asked while an app is being tried out")
    purpose, priority, workspace_id = _who(purpose, priority, workspace_id)
    _let_go()
    began, route = time.monotonic(), None
    if _backend:
        content, thinking = _backend(messages, schema, on_progress)
    else:
        content, thinking, route = _finished(messages, schema, think, on_progress, purpose, priority, workspace_id)
    calls = recording.get()
    if calls is not None:  # with how long it took and how much it thought: which steps thinking is worth its wait in
        calls.append({"messages": list(messages), "content": content, "seconds": round(time.monotonic() - began, 1),
                      "thought": len(thinking or ""),
                      # which model wrote it: a cloud model's answers are told apart from the platform's own model's
                      **({"provider": route.provider, "model": route.model} if route else {})})
    return content, thinking


GO_ON = ("Your answer above was cut off at the length limit before it ended. Continue it from exactly where it stops: "
         "write only the rest, starting with the very next character. Don't repeat anything already written, don't start "
         "over, and add nothing else.")
CONTINUATIONS = 2  # more goes at an answer cut off part way before it's given up as too long to finish


def _finished(messages, schema, think, on_progress, purpose, priority, workspace_id):
    """_ask, with an answer that reached the length limit part way finished, whichever model wrote it: it's shown what
    it wrote and asked for the rest (without thinking again, and not held to the schema: the rest isn't a whole
    answer), up to CONTINUATIONS more goes, and the pieces joined. One cut off before any answer (its thinking took all
    the room) has nothing to finish: CutOff, and chat_thinking answers it again without thinking."""
    try:
        return _ask(messages, schema, think, on_progress, purpose, priority, workspace_id)
    except CutOff as cut:
        if not cut.content.strip():
            raise
        written, thinking = cut.content, cut.thinking
    for n in range(1, CONTINUATIONS + 1):
        log.info("The AI's answer was cut off at the length limit after %s characters; asking for the rest (%s of %s)",
                 f"{len(written):,}", n, CONTINUATIONS)
        more = list(messages) + [{"role": "assistant", "content": written}, {"role": "user", "content": GO_ON}]
        try:
            rest, _, route = _ask(more, None, False, on_progress, purpose, priority, workspace_id)
            return written + _after(written, rest), thinking, route
        except CutOff as cut:
            if not cut.content.strip():
                raise
            written += _after(written, cut.content)
    raise CutOff(f"The AI's answer was still not finished after {CONTINUATIONS} more goes ({len(written):,} characters).",
                 content=written, thinking=thinking)


def _after(written, rest):
    """What the rest of an answer adds to what was written: a code fence it opened again, and what it wrote again
    from the end of the part before (a model asked to go on often starts a little back), taken off."""
    rest = re.sub(r"^\s*```[a-zA-Z]*\n", "", rest) if "```" not in written[-200:] else rest
    for k in range(min(len(written), len(rest), 2000), 7, -1):  # the longest overlap first; a few characters is chance
        if written.endswith(rest[:k]):
            return rest[k:]
    return rest


def chat_thinking(messages, *, think, **kw):
    """`chat` for work that may think first: thinking that runs away (round in circles, or past the room it has) is let
    go and the work answered without it, never failed. Round 43's builder learned it; the owner's checks crashed on it
    when a cloud model at max effort ran out of room. Returns (content, thinking, seconds lost to the runaway or None)."""
    began = time.monotonic()
    try:
        content, thinking = chat(messages, think=think, **kw)
        return content, thinking, None
    except (Stuck, CutOff):
        if not think:
            raise
    lost = round(time.monotonic() - began)
    log.info("Thinking ran away after %ss; answering without it", lost)
    content, thinking = chat(messages, think=False, **kw)
    return content, thinking, lost


def _ask(messages, schema, think, on_progress, purpose, priority, workspace_id, *, route=None, seconds=None):
    """One call through the gateway: wait for a turn at its server, ask, count it. Returns (content, thinking, route),
    the route with the model that answered."""
    route = route or _route(purpose)
    deadline = Deadline(float(seconds or settings().get("AI_DEADLINE") or 900))
    asked, usage, waited = time.monotonic(), {}, None
    try:
        p = _provider(route)
        ask = {"ollama": _ollama_chat, "openai": _openai_chat, "anthropic": _anthropic_chat}.get(p.protocol) if p else None
        if ask is None or not p.chats:
            raise AIError(f"AI_PROVIDER is {route.provider!r}: it can be one of "
                          f"{', '.join(k for k, x in providers.CATALOG.items() if x.chats)}")
        if p.needs_key and not route.key:
            raise _no_key(route)
        _skip_if_down(route.url)  # not worth waiting for a turn to find the server gone
        route = _model(route)
        gate = _gate(route)
        waited = gate.wait(priority, workspace_id, deadline, _waiting_note(on_progress) if on_progress else None)
        try:
            began = time.monotonic()
            _skip_if_down(route.url)  # it went while this call waited
            asking = _settings(think, purpose, priority)
            try:
                content, thinking = ask(route, messages, schema, asking, on_progress, deadline, usage)
            except _Named as named:  # the model chosen as people write it: asked again by the provider's own name for it
                _named[(route.provider, route.url, route.model)] = named.model
                route = route._replace(model=named.model)
                content, thinking = ask(route, messages, schema, asking, on_progress, deadline, usage)
            took = time.monotonic() - began
        finally:
            gate.done(workspace_id)
    except Exception as exc:
        _count(purpose, route, waited=time.monotonic() - asked if waited is None else waited, usage=usage, failed=type(exc).__name__)
        raise
    _count(purpose, route, waited=waited, took=took, usage=usage)
    return content, thinking, route


def _model(route, kind="chat"):
    """The route with its model: the one chosen, or else the provider's best for this key (providers.best_model)."""
    if route.model:
        return route._replace(model=_named.get((route.provider, route.url, route.model), route.model))
    p = _provider(route)
    try:
        found = providers.best_model(route.provider, providers.models(route.provider, route.url, route.key, kind=kind))
    except providers.KeyRefused as exc:
        raise AIUnavailable(f"{exc}: check the key under Settings › AI") from exc
    except (requests.RequestException, ValueError) as exc:
        raise _unavailable(route.url, type(exc).__name__) from exc
    if not found:
        raise AIUnavailable(f"no model is chosen for {p.name if p else route.provider}: a platform admin chooses one under Settings › AI"
                            + (" (or AI_MODEL in the .env file)" if kind == "chat" else " (or AI_MODEL_EMBED in the .env file)"))
    return route._replace(model=found)


def try_out(route, *, embed_too=False, seconds=90):
    """Ask a model one small question the way the platform's calls go, JSON schema and all, to see that it works (Settings
    › AI's "Try it"): {"ok", "said" (its answer, or what went wrong), "seconds", "model"}."""
    began = time.monotonic()
    p = _provider(route)
    try:
        if embed_too:
            vectors = _embed_with(route, ["Is this working?"], Deadline(seconds))
            return {"ok": True, "said": f"it read a sentence into {len(vectors[0])} numbers", "seconds": round(time.monotonic() - began, 1),
                    "model": route.model}
        schema = {"type": "object", "properties": {"greeting": {"type": "string"}}, "required": ["greeting"], "additionalProperties": False}
        content, _, used = _ask([{"role": "system", "content": "You are the AI inside a business app, checking that you're connected."},
                                 {"role": "user", "content": "Greet the business owner in eight words or fewer."}],
                                schema, False, None, "assistant", "interactive", None, route=route, seconds=seconds)
        said = parse_json(content).get("greeting") or content
        return {"ok": True, "said": str(said)[:200], "seconds": round(time.monotonic() - began, 1), "model": used.model}
    except (AIError, requests.RequestException, ValueError) as exc:
        return {"ok": False, "said": str(exc)[:400] or type(exc).__name__, "seconds": round(time.monotonic() - began, 1),
                "model": _named.get((route.provider, route.url, route.model), route.model) or (f"{p.name}'s best" if p else "")}


def _waiting_note(on_progress):
    """While a call waits its turn, what's shown of its thinking says so, instead of nothing at all."""
    def note(running, waiting):
        more = f", and {waiting} more {'is' if waiting == 1 else 'are'} waiting" if waiting else ""
        on_progress(f"Waiting for a turn with the AI: it's busy with {running} other request{'' if running == 1 else 's'}{more}.", "")
    return note


def _settings(think, purpose, priority):
    """What's asked of the model besides the conversation, the same whichever server answers: whether it thinks first,
    how long it may write, and how much chance it takes.

    No budget: how much a model thinks is its effort level (AI_EFFORT). Every model is asked for the same length,
    AI_MAX_TOKENS (128,000 unless set), thinking and answer together: far more than any step needs, so nothing is cut
    off part way (a step that ran out answered again without any thinking, all of it wasted), and said outright, so no
    server's own smaller default (4,096 at some) applies. A model that allows less says so, and its own limit is learned
    (_step); it's charged only for what it writes. A model that runs away is still stopped: going round in circles or
    repeating itself, and at the deadline."""
    cfg = settings()
    thinks = cfg["AI_THINK"] if think is None else think
    steady, tries = building.get() or purpose == "builder", again.get()
    seed = cfg.get("AI_BUILDER_SEED")
    return {"think": thinks, "priority": priority, "max_tokens": int(cfg.get("AI_MAX_TOKENS") or 128000),
            "temperature": min(0.9, (cfg.get("AI_BUILDER_TEMPERATURE", 0.3) if steady else cfg.get("AI_TEMPERATURE", 0.6)) + 0.2 * tries),
            "top_p": 0.9, "seed": seed + tries if steady and seed is not None else None, "num_ctx": cfg.get("AI_NUM_CTX") or 0}


def _post(route, path, body, deadline, usage, *, stream=True, read_timeout=None):
    """POST to a model server, asking again with a growing wait when it can't be reached or says it's busy or broken (429,
    5xx), and never when it refused the request itself (another 4xx). Returns the last response, whatever its status;
    raises AIUnavailable when the server couldn't be reached at all, Late when the deadline came first."""
    cfg = settings()
    tries, backoff = int(cfg.get("AI_RETRIES", 3)), float(cfg.get("AI_BACKOFF", 1.0))
    read_timeout = read_timeout or float(cfg.get("AI_READ_TIMEOUT") or 600)
    response = failure = None
    for attempt in range(tries + 1):
        left = deadline.left()
        if left <= 0:
            raise deadline.answering()
        try:
            # The read timeout cut to the time left, so a server that says nothing is given up on at the deadline. (One
            # that stops part way through is caught by the deadline between chunks, or by this timeout.)
            response = requests.post(route.url + path, json=body, headers=_headers(route), stream=stream,
                                     timeout=(min(10.0, left), min(read_timeout, left)))
        except (requests.ConnectionError, requests.Timeout) as exc:
            if deadline.left() <= 0.05:
                raise deadline.answering() from exc
            # Skipped by every other call at once (they'd only wait out the same timeouts); this one keeps trying, and
            # clears it if the server comes back.
            response, failure = None, _unavailable(route.url, type(exc).__name__)
            failure.__cause__ = exc
        else:
            if response.status_code < 400:
                _down.pop(route.url, None)
                return response
            if response.status_code != 429 and response.status_code < 500:
                return response  # refused: asking again gets the same answer
            if response.status_code == 429 and _out_of_credit(response):
                return response  # not busy: the account has run out, and waiting won't change that
        wait = backoff * 2 ** attempt * random.uniform(0.75, 1.25)  # the jitter keeps calls that failed together apart
        after = _retry_after(response)
        if after is not None:
            wait = max(wait, min(after, 30.0))
        if attempt == tries or wait >= deadline.left():
            break
        _close(response)
        usage["retries"] = usage.get("retries", 0) + 1
        log.info("The AI at %s: %s; asking again in %.1f s", route.url,
                 f"HTTP {response.status_code}" if response is not None else type(failure.__cause__).__name__, wait)
        time.sleep(wait)
    if response is None:
        raise failure
    return response


def _retry_after(response):
    """The seconds a busy server asked to be left alone for (Retry-After), if it said."""
    try:
        return float((getattr(response, "headers", None) or {}).get("Retry-After"))
    except (TypeError, ValueError):
        return None


def _close(response):
    close = getattr(response, "close", None)
    if close:
        close()


def _out_of_credit(response):
    text = (getattr(response, "text", "") or "").lower()
    return any(w in text for w in ("insufficient_quota", "exceeded your current quota", "credit balance", "billing", "out of credit",
                                   "payment required"))


def _refused(response, route, what):
    """Raise what a model server's error status means (after _post has asked again where that's worth it). A key turned
    away, or an account out of credit, waits like a server that's down (AIUnavailable): the work carries on by itself
    once a platform admin puts it right."""
    status = response.status_code
    if status < 400:
        return
    p = _provider(route)
    who = p.name if p and p.cloud else "Ollama" if route.provider == "ollama" else "The model server"
    if status in (401, 403):
        raise AIUnavailable(f"{who} turned the key away (HTTP {status}): a platform admin checks it under Settings › AI")
    if status == 402 or (status == 429 and _out_of_credit(response)):
        raise AIUnavailable(f"{who} says the account has run out of credit (HTTP {status}): top it up with {who}, then the work carries on")
    if status == 429:
        raise AIUnavailable(f"the AI at {route.url} is too busy right now (HTTP 429)")
    if status in (502, 503, 504, 529):
        raise _unavailable(route.url, f"HTTP {status}")
    if status == 404 and p and p.cloud:
        try:
            ids = providers.models(route.provider, route.url, route.key)
        except Exception:  # noqa: BLE001 - it can't say what it has: said as it is
            ids = []
        own = providers.own_name(route.model, ids)
        if own and own != route.model:
            raise _Named(own)
        near = providers.near_names(route.model, ids)
        raise AIError(f"{who} has no model called {route.model!r} for this key (HTTP 404)"
                      + (f": did you mean {' or '.join(near)}? Choose it under Settings › AI" if near else ": choose another under Settings › AI"))
    raise AIError(f"{who} refused {what} ({status}): {_said(response)[:300]}")


def _said(response):
    """The message in a server's error, else its text."""
    try:
        said = response.json()
        error = said.get("error") if isinstance(said, dict) else None
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if isinstance(error, str):
            return error
        if isinstance(said, dict) and said.get("message"):
            return str(said["message"])
    except ValueError:
        pass
    return response.text or ""


class _Answer:
    """An answer as it streams in, from either kind of server: its thinking and its text so far, watched for a model gone
    round in a loop (stopped now, not at the length limit), shown to on_progress at most every 0.6 seconds, and stopped
    at the deadline."""

    def __init__(self, response, on_progress, deadline, url):
        self.response, self.on_progress, self.deadline, self.url = response, on_progress, deadline, url
        self.thinking, self.content, self.reason, self.last = [], [], None, 0.0
        self.thought = self.pondered = self.size = self.looked = self.blank = 0

    def think(self, text):
        if not text:
            return
        self.thinking.append(text)
        self.thought += len(text)
        if self.thought - self.pondered >= 3000:  # thinking that goes round in circles: stop it now, not at the length limit
            self.pondered = self.thought
            if _repeating("".join(self.thinking)):
                _close(self.response)
                raise Stuck("The AI got stuck thinking (it kept going over the same ground).")

    def say(self, text):
        if not text:
            return
        self.content.append(text)
        self.blank = self.blank + len(text) if not text.strip() else 0
        if self.blank > 400:  # constrained JSON output sometimes degenerates into endless whitespace
            _close(self.response)
            raise AIError("The AI got stuck writing its answer (it was only writing blank space).")
        self.size += len(text)
        if self.size - self.looked >= 1500:  # or into the same passage over and over: stop now, not at the length limit
            self.looked = self.size
            if _repeating("".join(self.content)):
                _close(self.response)
                raise Stuck("The AI got stuck writing its answer (it kept repeating itself).")

    def show(self, done=False):
        if self.on_progress and (time.monotonic() - self.last > 0.6 or done):
            self.last = time.monotonic()
            self.on_progress("".join(self.thinking), "".join(self.content))

    def on_time(self):
        if self.deadline.left() <= 0:
            _close(self.response)
            raise self.deadline.answering()

    def broke(self, exc):
        """What a connection that broke while the answer streamed in means: the deadline, if it's passed (the read
        timeout was cut to it); the answer stalled, if the server stopped sending part way (it had answered, so it isn't
        gone: a model held up writing what the server holds back, and every other call is asked as usual); or else the
        server gone. Round 45: a stall read as the server gone skipped every call for 20 seconds, and ended the round."""
        if self.deadline.left() <= 0.05:
            return self.deadline.answering()
        if isinstance(exc, requests.Timeout) or "timed out" in str(exc).lower():
            return Stuck(f"The AI went quiet part way through its answer ({len(''.join(self.thinking)):,} characters of "
                         f"thinking and {self.size:,} of answer so far).")
        return _unavailable(self.url, type(exc).__name__)

    def finish(self):
        content, thinking = "".join(self.content), "".join(self.thinking)
        # A server not told to keep the thinking apart writes it in the answer, between <think> tags (or, when the chat
        # template opened the tag itself, only the closing one): it's taken out of the answer either way.
        inline = re.findall(r"<think>(.*?)</think>", content, flags=re.S)
        content = re.sub(r"<think>.*?</think>", "", content, flags=re.S)
        if "</think>" in content and "<think>" not in content:
            before, content = content.split("</think>", 1)
            inline.insert(0, before)
        text = content.strip()
        if self.reason == "length":
            said = content.split("<think>")[0]  # cut off in thinking written inline: none of it is the answer
            raise CutOff(f"The AI's answer was cut off at the length limit ({len(text):,} characters written).",
                         content=said if said.strip() else "", thinking=thinking or "\n".join(inline))
        return text, thinking or "\n".join(x.strip() for x in inline if x.strip())


def _chunk(line, server):
    try:
        return json.loads(line)
    except ValueError as exc:
        raise AIError(f"{server} sent something that couldn't be read: {str(line)[:200]!r}") from exc


_levels = {}  # (Ollama's address, model) -> the thinking levels Ollama says the model takes, None when it names none


def _ollama_levels(route):
    """The thinking levels a model takes, as Ollama says (/api/show's "thinking" values: Qwen 3.8's are false, low, medium
    and xhigh, its default medium); None from an Ollama or a model that names none. Asked once a model."""
    key = (route.url, route.model)
    if key not in _levels:
        try:
            response = requests.post(route.url.rstrip("/") + "/api/show", json={"model": route.model}, timeout=(5, 15))
        except requests.RequestException:
            return None  # asked again next time: the chat itself says what's wrong
        try:
            values = ((response.json() or {}).get("thinking") or {}).get("values") if response.ok else None
        except ValueError:
            values = None
        _levels[key] = list(values) if isinstance(values, list) and values else None
    return _levels[key]


def _ollama_think(route, think, effort):
    """What Ollama is told about thinking. A model with levels thinks at the effort asked for, or the nearest level below
    it that the model has (the least it has when none is below): "true" alone thinks at its default, which for Qwen 3.8 is
    medium, whatever AI_EFFORT says. Off is off where the model can stop thinking, else its lowest level."""
    levels = _ollama_levels(route)
    named = sorted((v for v in levels or () if v in EFFORTS), key=EFFORTS.index)
    if not named:
        return bool(think)
    if not think:
        return False if False in levels else named[0]
    rank = EFFORTS.index(effort) if effort in EFFORTS else EFFORTS.index("high")
    below = [v for v in named if EFFORTS.index(v) <= rank]
    return below[-1] if below else named[0]


def _ollama_chat(route, messages, schema, ask, on_progress, deadline, usage):
    think = _ollama_think(route, ask["think"], settings().get("AI_EFFORT") or "xhigh")
    body = {"model": route.model, "messages": messages, "stream": True, "think": think}
    if schema:
        body["format"] = schema
    body["options"] = {"num_predict": ask["max_tokens"], "top_p": ask["top_p"], "temperature": ask["temperature"],
                       **({"seed": ask["seed"]} if ask["seed"] is not None else {}),
                       **({"num_ctx": ask["num_ctx"]} if ask["num_ctx"] else {})}
    response = _post(route, "/api/chat", body, deadline, usage)
    _refused(response, route, "the request")
    answer = _Answer(response, on_progress, deadline, route.url)
    try:
        for line in response.iter_lines():
            answer.on_time()
            if not line:
                continue
            chunk = _chunk(line, "Ollama")
            if chunk.get("error"):
                raise AIError(f"Ollama: {chunk['error']}")
            message = chunk.get("message") or {}
            answer.think(message.get("thinking"))
            answer.say(message.get("content"))
            if chunk.get("done"):
                answer.reason = chunk.get("done_reason")
                usage["in"], usage["out"] = chunk.get("prompt_eval_count"), chunk.get("eval_count")
            answer.show(done=bool(chunk.get("done")))
    except requests.RequestException as exc:
        raise answer.broke(exc) from exc
    return answer.finish()


# Asking in the way a server takes ----------------------------------------------------------------------------------

_learned = {}  # (server, model) -> {a part of the request: the rung it's on now, "cap": the longest answer it allows}
_learned_lock = threading.Lock()


class _Part(namedtuple("_Part", "name words rungs")):
    """An optional part of a request, as a ladder of ways to send it, best first (a rung is what it adds to the request;
    {} leaves it out). A server that refuses a request naming the part (`words`, in its error) is asked again a rung
    down."""


def _negotiate(route, path, base, parts, deadline, usage, finish=None):
    """POST `base` with each part on the rung this server and model haven't refused yet. When the server refuses the
    request (400, 422) naming one of the parts, that part goes down a rung, remembered for this server and model, and
    it's asked again; a refusal naming none of them (or a part already left out) is returned as it is. `finish(body,
    rungs)` makes what a rung asks beyond adding to the body (the JSON schema written into the prompt)."""
    known = (route.url, route.model)
    response = None
    for _ in range(sum(len(part.rungs) for part in parts) + 2):
        with _learned_lock:
            learned = dict(_learned.get(known, {}))
        body, rungs = dict(base), {}
        for part in parts:
            rung = part.rungs[min(learned.get(part.name, 0), len(part.rungs) - 1)]
            rungs[part.name] = rung
            for k, v in rung.items():
                if not k.startswith("_"):
                    body[k] = {**body[k], **v} if isinstance(v, dict) and isinstance(body.get(k), dict) else v
        if learned.get("cap"):
            for k in ("max_tokens", "max_completion_tokens"):
                if body.get(k):
                    body[k] = min(body[k], learned["cap"])
        if finish:
            body = finish(body, rungs)
        response = _post(route, path, body, deadline, usage)
        if response.status_code not in (400, 422):
            return response
        step = _step(parts, learned, _said(response).lower(), body)
        if not step:
            return response
        with _learned_lock:
            _learned.setdefault(known, {}).update(step)
        log.info("The AI at %s (%s) refused %s; asking again with %s", route.url, route.model, _said(response)[:200], step)
        _close(response)
    return response


def _step(parts, learned, said, body):
    """What to change after a refusal that says `said`: {"cap": n} when it names the longest answer it allows, or
    {part: its next rung} for the first part it names that isn't left out already; None when it names none."""
    sent = body.get("max_completion_tokens") or body.get("max_tokens")
    if sent and any(w in said for w in ("max_tokens", "max_completion_tokens", "output tokens", "completion tokens")) \
            and "context length" not in said and "budget" not in said:
        smaller = [n for n in (int(x) for x in re.findall(r"\d{3,}", said)) if 256 <= n < sent]
        if smaller:
            return {"cap": max(smaller)}
    for part in parts:
        at = learned.get(part.name, 0)
        if at < len(part.rungs) - 1 and any(w in said for w in part.words):
            return {part.name: at + 1}
    return None


def _schema_note(schema):
    return ("Answer with one JSON object and nothing else: no words before or after it, no code fence. It must match this "
            "JSON schema:\n" + json.dumps(schema, ensure_ascii=False, separators=(",", ":")))


def _schema_key(schema):
    return "format:" + hashlib.sha1(json.dumps(schema, sort_keys=True, default=str).encode()).hexdigest()[:12]


def _image_type(data):
    head = data[:16]
    return ("image/jpeg" if head.startswith("/9j/") else "image/gif" if head.startswith("R0lGOD") else
            "image/webp" if head.startswith("UklGR") else "image/png")


def _images(message):
    """A message's pictures (Ollama's way: base64 in "images") as (media type, base64) pairs."""
    out = []
    for data in message.get("images") or []:
        data = str(data)
        if data.startswith("data:") and ";base64," in data:
            kind, data = data[5:].split(";base64,", 1)
            out.append((kind, data))
        else:
            out.append((_image_type(data), data))
    return out


def _openai_messages(messages):
    """The conversation as chat completions takes it: role and content only, pictures as image parts."""
    out = []
    for m in messages:
        pictures = _images(m)
        content = m.get("content") or ""
        if pictures:
            content = [{"type": "text", "text": content}] + [{"type": "image_url", "image_url": {"url": f"data:{kind};base64,{data}"}}
                                                            for kind, data in pictures]
        out.append({"role": m.get("role") or "user", "content": content})
    return out


EFFORTS = ("low", "medium", "high", "xhigh", "max")


def _efforts(effort):
    """The effort to ask for, then the ones to step to when a model hasn't it: each level below, down to high (a model
    without max is asked xhigh, not dropped to high); one below high is raised to high; one a provider names its own
    way (OpenAI's minimal) is tried as said."""
    effort = effort or "high"
    below = reversed(EFFORTS[2:EFFORTS.index(effort)]) if effort in EFFORTS else ()
    return list(dict.fromkeys([effort, *below, "high"]))


def _openai_parts(p, ask, schema):
    """The optional parts of a chat completions request, most likely to be what a refusal names first."""
    cfg, n = settings(), ask["max_tokens"]
    parts = []
    if cfg.get("AI_SERVER_PRIORITY") and p.key == "compatible":
        parts.append(_Part("priority", ("priority",), [{"priority": PRIORITIES.index(ask["priority"])}, {}]))  # vLLM's: lower first
    if p.think == "template":
        # Qwen-style models think first or not by a switch in their chat template, which vLLM, SGLang and llama.cpp pass on
        # (a template without the switch ignores it). Ollama's own /v1 ignores it: Ollama is AI_PROVIDER=ollama.
        parts.append(_Part("template", ("chat_template_kwargs",), [{"chat_template_kwargs": {"enable_thinking": bool(ask["think"])}}, {}]))
    parts.append(_Part("stream_options", ("stream_options", "include_usage"), [{"stream_options": {"include_usage": True}}, {}]))
    if p.sampling:
        if ask["seed"] is not None:
            parts.append(_Part("seed", ("seed",), [{"seed": ask["seed"]}, {}]))
        parts.append(_Part("top_p", ("top_p",), [{"top_p": ask["top_p"]}, {}]))
        parts.append(_Part("temperature", ("temperature",), [{"temperature": ask["temperature"]}, {}]))
    effort = cfg.get("AI_EFFORT") or "xhigh"
    if p.think == "effort":  # OpenAI's reasoning models and those that copy them: low to high (none, minimal, xhigh on some)
        on = [{"reasoning_effort": e} for e in _efforts(effort)]
        off = [{"reasoning_effort": e} for e in ("none", "minimal", "low")]
        parts.append(_Part("think:on" if ask["think"] else "think:off", ("reasoning_effort", "reasoning effort", "reasoning.effort", "reasoning"),
                           (on if ask["think"] else off) + [{}]))
    elif p.think == "reasoning":  # OpenRouter's, one way for all its models
        parts.append(_Part("think:on" if ask["think"] else "think:off", ("reasoning",),
                           [{"reasoning": {"effort": effort if effort in ("low", "medium", "high") else "high"}} if ask["think"] else
                            {"reasoning": {"enabled": False}}, {}]))
    # a server that refuses the length as more than its context (vLLM names no limit of its own) is sent none
    first, other = p.tokens, "max_tokens" if p.tokens == "max_completion_tokens" else "max_completion_tokens"
    parts.append(_Part("tokens", ("max_tokens", "max_completion_tokens"), [{first: n}, {other: n}, {}]))
    if schema:
        # The answer constrained to the schema as it's decoded: vLLM, SGLang, llama.cpp and LM Studio take this form (vLLM
        # also its older `"guided_json"`). A cloud model is given it to follow (not strict: a schema built for constrained
        # decoding breaks the rules OpenAI's strict mode has), else told it in the prompt, in JSON mode or without.
        parts.append(_Part(_schema_key(schema), ("response_format", "json_schema", "json_object", "schema", "json mode"), [
            {"response_format": {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema, "strict": p.strict}}},
            {"response_format": {"type": "json_object"}, "_prompt": True}, {"_prompt": True}]))
    return parts


def _openai_chat(route, messages, schema, ask, on_progress, deadline, usage):
    """/v1/chat/completions, streamed as server-sent events: the answer arrives as `content` deltas, the thinking (from a
    server started with a reasoning parser, e.g. vLLM's --reasoning-parser qwen3, or a provider that shows it) as
    `reasoning_content` deltas (`reasoning` in newer vLLM, Groq and OpenRouter), and what it cost in a last chunk of
    `usage`. (AI_NUM_CTX isn't sent: an OpenAI-compatible server's context length is fixed when it starts.)"""
    p = _provider(route)
    base = {"model": route.model, "messages": _openai_messages(messages), "stream": True}

    def finish(body, rungs):
        if schema and any(r.get("_prompt") for r in rungs.values()):
            said = list(body["messages"])
            if said and said[0]["role"] == "system" and isinstance(said[0]["content"], str):
                said[0] = {**said[0], "content": said[0]["content"] + "\n\n" + _schema_note(schema)}
            else:
                said.insert(0, {"role": "system", "content": _schema_note(schema)})
            body["messages"] = said
        return body

    response = _negotiate(route, "/chat/completions", base, _openai_parts(p, ask, schema), deadline, usage, finish)
    _refused(response, route, "the request")
    answer = _Answer(response, on_progress, deadline, route.url)
    try:
        for line in response.iter_lines():
            answer.on_time()
            line = line.decode("utf-8", "replace") if isinstance(line, bytes) else line
            if not line.startswith("data:"):
                continue  # the blank line between events, a comment (":"), an "event:" line
            data = line[5:].strip()
            if data == "[DONE]":
                break
            chunk = _chunk(data, "The model server")
            if chunk.get("error"):
                error = chunk["error"]
                raise AIError(f"The model server: {error.get('message') if isinstance(error, dict) else error}")
            if chunk.get("usage"):
                usage["in"], usage["out"] = chunk["usage"].get("prompt_tokens"), chunk["usage"].get("completion_tokens")
            for choice in (chunk.get("choices") or [])[:1]:
                delta = choice.get("delta") or {}
                answer.think(delta.get("reasoning_content") or delta.get("reasoning"))
                answer.say(delta.get("content"))
                if choice.get("finish_reason"):
                    answer.reason = choice["finish_reason"]
            answer.show()
    except requests.RequestException as exc:
        raise answer.broke(exc) from exc
    answer.show(done=True)
    if answer.reason == "content_filter":
        raise AIError(f"{p.name} held the answer back (its content filter)")
    return answer.finish()


# Anthropic's messages API ----------------------------------------------------------------------------------------

ANTHROPIC_FORMATS = {"date-time", "time", "date", "duration", "email", "hostname", "uri", "ipv4", "ipv6", "uuid"}
ANTHROPIC_DROPS = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "minLength", "maxLength", "maxItems",
                   "uniqueItems", "minProperties", "maxProperties", "patternProperties", "propertyNames", "contains"}


def _anthropic_schema(schema):
    """The schema as Anthropic's JSON output takes it (every object closed, no number or length limits), or None when it
    can't be said that way: an open object (a map), a schema that refers to itself, or more optional fields or choices
    than it allows (24, 16), when the prompt carries it instead."""
    counts, clean = {"optional": 0, "unions": 0}, set()
    maps = ("properties", "$defs", "definitions")

    def follow(ref, seen):  # what a reference points to: walked once, to find a schema that refers to itself
        if not ref.startswith("#/") or ref in seen:
            raise ValueError("refers to itself")
        if ref in clean:
            return
        target = schema
        for piece in ref[2:].split("/"):
            target = target.get(piece, {}) if isinstance(target, dict) else {}
        walk(target, seen | {ref}, count=False)
        clean.add(ref)

    def walk(node, seen, count=True):
        if isinstance(node, list):
            return [walk(x, seen, count) for x in node]
        if not isinstance(node, dict):
            return node
        if not node:  # any value ({}), which Anthropic refuses: any it can say (an open object it can't)
            counts["unions"] += 1 if count else 0
            return {"anyOf": [{"type": "string"}, {"type": "number"}, {"type": "boolean"}, {"type": "null"},
                              {"type": "array", "items": {"anyOf": [{"type": "string"}, {"type": "number"}, {"type": "boolean"}]}}]}
        if node.get("type") == "object" and not node.get("properties") and node.get("additionalProperties") is not False:
            raise ValueError("an open object")  # a map ({"type": "object"} alone): closed, it could only ever be {}
        if isinstance(node.get("$ref"), str):
            follow(node["$ref"], seen)
        out = {}
        for k, v in node.items():
            if k in maps and isinstance(v, dict):
                out[k] = {name: walk(sub, seen, count) for name, sub in v.items()}
            elif k in ANTHROPIC_DROPS or (k == "format" and isinstance(v, str) and v not in ANTHROPIC_FORMATS) or (k == "minItems" and v not in (0, 1)):
                continue
            else:
                out[k] = v if k in ("enum", "const", "default", "required", "$ref") else walk(v, seen, count)
        if "oneOf" in out:
            out["anyOf"] = out.pop("oneOf")
        if count and ("anyOf" in out or isinstance(out.get("type"), list)):
            counts["unions"] += 1
        if out.get("type") == "object" or "properties" in out:
            if out.get("additionalProperties") not in (None, False):
                raise ValueError("an open object")
            out["additionalProperties"] = False
            if count:
                counts["optional"] += len(set(out.get("properties") or {}) - set(out.get("required") or []))
        return out
    try:
        made = walk(schema, frozenset())
    except (ValueError, RecursionError):
        return None
    return made if counts["optional"] <= 24 and counts["unions"] <= 16 else None


def _anthropic_turns(messages):
    """The conversation as the messages API takes it: the system prompt apart, pictures as image blocks before the words,
    and a user turn first."""
    system = "\n\n".join(m["content"] for m in messages if m.get("role") == "system" and m.get("content"))
    turns = []
    for m in messages:
        if m.get("role") == "system":
            continue
        pictures = _images(m)
        content = m.get("content") or "(nothing)"
        if pictures:
            content = [{"type": "image", "source": {"type": "base64", "media_type": kind, "data": data}} for kind, data in pictures] \
                + [{"type": "text", "text": content}]
        turns.append({"role": "assistant" if m.get("role") == "assistant" else "user", "content": content})
    if not turns or turns[0]["role"] != "user":
        turns.insert(0, {"role": "user", "content": "Go ahead."})
    return system, turns


def _old_claude_budget(most):
    """A Claude model from before adaptive thinking thinks only in a budget (newer ones decide for themselves): three
    quarters of all it may write, the rest the answer's."""
    return max(1024, most * 3 // 4)


def _anthropic_parts(ask, schema):
    cfg, n = settings(), ask["max_tokens"]
    effort = cfg.get("AI_EFFORT") or "xhigh"
    budget = _old_claude_budget(n)
    if ask["think"]:  # Claude decides how much to think (adaptive), shown in summary; older models by a budget
        thinking = [{"thinking": {"type": "adaptive", "display": "summarized"}}, {"thinking": {"type": "adaptive"}},
                    {"thinking": {"type": "enabled", "budget_tokens": budget}}, {}]
        efforts = [{"output_config": {"effort": e}} for e in _efforts(effort)] + [{}]
    else:  # off where it can be (some newest models always think: the least they do, at low effort)
        thinking = [{"thinking": {"type": "disabled"}}, {"thinking": {"type": "between_tools"}}, {}]
        efforts = [{"output_config": {"effort": "low"}}, {}]
    parts = [_Part("think:on" if ask["think"] else "think:off", ("thinking", "budget_tokens", "adaptive", "between_tools", "display"), thinking),
             _Part("effort:on" if ask["think"] else "effort:off", ("effort",), efforts),
             _Part("cache", ("cache_control",), [{"_cache": True}, {}])]
    if schema:
        shaped = _anthropic_schema(schema)
        parts.append(_Part(_schema_key(schema), ("output_config.format", "json_schema", "schema", "too complex", "output format"),
                           ([{"output_config": {"format": {"type": "json_schema", "schema": shaped}}}] if shaped else []) + [{"_prompt": True}]))
    return parts


def _anthropic_chat(route, messages, schema, ask, on_progress, deadline, usage):
    """Anthropic's /v1/messages, streamed: the thinking as thinking_delta events (a summary of it), the answer as
    text_delta, what it cost in message_start and message_delta. The system prompt is cached, so the builder's long
    shared instructions are paid for in full once in a while, not at every call. No temperature: Claude's newest models
    take none."""
    system, turns = _anthropic_turns(messages)
    base = {"model": route.model, "max_tokens": ask["max_tokens"], "messages": turns, "stream": True}

    def finish(body, rungs):
        if (body.get("thinking") or {}).get("type") == "enabled":  # its budget within the limit the model said it has
            body["thinking"] = {**body["thinking"], "budget_tokens": _old_claude_budget(body["max_tokens"])}
        words = system
        if schema and any(r.get("_prompt") for r in rungs.values()):
            words = (words + "\n\n" if words else "") + _schema_note(schema)
        if words:
            block = {"type": "text", "text": words}
            if any(r.get("_cache") for r in rungs.values()):
                block["cache_control"] = {"type": "ephemeral"}
            body["system"] = [block]
        return body

    response = _negotiate(route, "/messages", base, _anthropic_parts(ask, schema), deadline, usage, finish)
    _refused(response, route, "the request")
    answer = _Answer(response, on_progress, deadline, route.url)
    try:
        for line in response.iter_lines():
            answer.on_time()
            line = line.decode("utf-8", "replace") if isinstance(line, bytes) else line
            if not line.startswith("data:"):
                continue  # "event:" lines name what the data line after says too
            event = _chunk(line[5:].strip(), "Anthropic")
            kind = event.get("type")
            if kind == "message_start":
                said = (event.get("message") or {}).get("usage") or {}
                usage["in"] = sum(said.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
            elif kind == "content_block_delta":
                delta = event.get("delta") or {}
                if delta.get("type") == "thinking_delta":
                    answer.think(delta.get("thinking"))
                elif delta.get("type") == "text_delta":
                    answer.say(delta.get("text"))
            elif kind == "message_delta":
                stop = (event.get("delta") or {}).get("stop_reason")
                usage["out"] = (event.get("usage") or {}).get("output_tokens")
                if stop == "refusal":
                    raise AIError("Claude declined to answer this (Anthropic's safety checks stopped it)")
                if stop in ("max_tokens", "model_context_window_exceeded"):
                    answer.reason = "length"
                elif stop:
                    answer.reason = stop
            elif kind == "error":
                error = event.get("error") or {}
                if error.get("type") in ("overloaded_error", "api_error", "rate_limit_error"):
                    _close(response)
                    raise AIUnavailable(f"Anthropic is busy right now ({error.get('message') or error.get('type')})")
                raise AIError(f"Anthropic: {error.get('message') or error}")
            elif kind == "message_stop":
                break
            answer.show()
    except requests.RequestException as exc:
        raise answer.broke(exc) from exc
    answer.show(done=True)
    return answer.finish()


def _repeating(text, probe=300, times=4, window=8000):
    """Whether the end of an answer is the same passage written over and over (a model stuck in a loop)."""
    tail = text[-window:]
    end = tail[-probe:]
    return len(end.strip()) >= probe // 3 and len(text) >= probe * times and tail.count(end) >= times


def embed_model():
    """The model search by meaning uses now (what passages' vectors are kept under), or None for words only."""
    if _embedder:
        return settings().get("OLLAMA_EMBED_MODEL") or None
    route = _route("embed")
    return route.model or None if route.provider != providers.WORDS_ONLY else None


def embed(texts):
    """Vectors for passages of text, from the embedding model (Settings › AI, or AI_MODEL_EMBED: bge-m3 on this computer
    by default): passages that mean the same thing get vectors that point the same way, whatever words they use or
    language they're in.

    Embeddings don't wait in the chat queue: they take a moment, not minutes, and a search shouldn't wait behind the
    builder. They're retried, given the deadline and counted (as "embed") like any call."""
    if _embedder:
        return _embedder(list(texts))
    route = _route("embed")
    if not route.model or route.provider == providers.WORDS_ONLY:
        raise AIUnavailable("no embedding model is set (Settings › AI, or AI_MODEL_EMBED)")
    return _embed_with(route, list(texts), Deadline(float(settings().get("AI_DEADLINE") or 900)))


def _embed_with(route, texts, deadline):
    usage, began = {}, time.monotonic()
    p = _provider(route)
    try:
        if p is None or not p.embeds:
            raise AIError(f"{p.name if p else route.provider} has no model for search by meaning: choose another under Settings › AI")
        if p.needs_key and not route.key:
            raise _no_key(route)
        _skip_if_down(route.url)
        if p.protocol == "ollama":
            response = _post(route, "/api/embed", {"model": route.model, "input": texts, "truncate": True}, deadline, usage,
                             stream=False, read_timeout=300)
        else:
            response = _post(route, "/embeddings", {"model": route.model, "input": texts}, deadline, usage, stream=False, read_timeout=300)
        if response.status_code == 404:
            raise AIUnavailable(f"the embedding model {route.model} isn't in Ollama yet (run: ollama pull {route.model})" if p.protocol == "ollama"
                                else f"{p.name if p.cloud else 'the server at ' + route.url} doesn't have the embedding model {route.model}")
        _refused(response, route, "to embed")
        said = response.json()
        if p.protocol == "ollama":
            vectors, usage["in"] = said.get("embeddings") or [], said.get("prompt_eval_count")
        else:
            vectors = [x.get("embedding") for x in sorted(said.get("data") or [], key=lambda x: x.get("index", 0))]
            usage["in"] = (said.get("usage") or {}).get("prompt_tokens") or (said.get("usage") or {}).get("total_tokens")
        if len(vectors) != len(texts):
            raise AIError("The embedding model didn't return a vector for every passage.")
    except Exception as exc:
        _count("embed", route, usage=usage, failed=type(exc).__name__)
        raise
    _count("embed", route, took=time.monotonic() - began, usage=usage)
    return vectors


def _whole_objects(text):
    """Each JSON object written whole in the text, outermost ones only, in order, and whether the first "{" began one
    (when it didn't, an object found later may be a piece of an answer that was cut off)."""
    decoder, found, i, first = json.JSONDecoder(), [], text.find("{"), None
    while i != -1:
        try:
            value, end = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            first = False if first is None else first
            i = text.find("{", i + 1)
            continue
        first = True if first is None else first
        if isinstance(value, dict):
            found.append(value)
        i = text.find("{", end)
    return found, bool(first)


def parse_json(text, schema=None):
    """The model's answer as a dict. A model told the answer's shape in the prompt only (Anthropic can't be held to the
    builder's: it has maps and more optional fields than it allows) wraps it in stray words, a code fence or a made-up tool
    call, or writes it twice ("Wait — the answer must be only the JSON object. Here it is: {…}"): the cloud round's
    changes failed on it 8 times in 33. Its answer is the last whole object: with a schema, the last with every key it
    requires; without, the last of those after a first "{" that began one."""
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        found, whole = _whole_objects(text or "")
        need = set((schema or {}).get("required") or ())
        found = [o for o in found if need <= o.keys()] if need else found if whole else []
        if found:
            return found[-1]
        match = re.search(r"\{.*\}", text or "", re.S)  # one object, stray words around it
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
        log.warning("Unreadable AI answer (%s); it began: %r … and ended: %r", exc, (text or "")[:600], (text or "")[-300:])
        raise AIError(f"The AI's answer wasn't valid JSON: {exc}") from exc
