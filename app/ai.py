"""The AI: models on servers the platform runs itself, reached through a gateway.

Two kinds of model server (AI_PROVIDER):
- ollama: Ollama on this computer, for development and one business on one machine (/api/chat, /api/embed).
- openai: any OpenAI-compatible server (vLLM, SGLang, llama.cpp's server, LM Studio) at AI_BASE_URL. Production runs vLLM
  or SGLang on its own GPU servers: they batch many customers' requests together, keep shared prompt prefixes cached and
  constrain JSON quickly.
No cloud model is wired in: a conversation only goes somewhere else if someone puts that address in AI_BASE_URL.

Each purpose can have its own model, and its own server: the builder a big model (AI_MODEL_BUILDER, AI_BASE_URL_BUILDER),
quick jobs a small one (AI_MODEL_FAST), the in-app assistant and background work AI_MODEL.

`chat` streams the model's answer, calling `on_progress(thinking, content)` as it goes so the builder can show what the
model is doing while it works. A model that can't be reached raises AIUnavailable: the work waits and tries again
later instead of failing.

The gateway. Every call waits its turn in this process's queue for the server it goes to (one queue per server):
- At most AI_CONCURRENCY calls go at once (2 for Ollama, 8 for an OpenAI-compatible server, unless set). Sending more
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
import json
import logging
import os
import random
import re
import threading
import time
from collections import deque, namedtuple

import requests
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
    """The answer reached the length limit before it was finished (asked for too much at once)."""


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
    at work, `asking`, the worker thread it's on), or else the assistant's. The workspace is flask.g's when a person's
    request has one; None (one shared turn) when nothing says."""
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
PROVIDERS = ("ollama", "openai")
_TAGS = {"builder": "_BUILDER", "fast": "_FAST", "embed": "_EMBED"}


def _route(purpose):
    """Where a purpose's calls go: its provider, server, key and model. AI_MODEL_BUILDER, AI_BASE_URL_FAST and the like
    where they're set; the plain AI_* settings otherwise."""
    cfg = settings()
    tag = _TAGS.get(purpose, "")

    def setting(name):
        return (cfg.get(name + tag) if tag else None) or cfg.get(name)
    provider = (setting("AI_PROVIDER") or "ollama").strip().lower()
    url = (setting("AI_BASE_URL") or cfg.get("OLLAMA_URL") or "http://127.0.0.1:11434").rstrip("/")
    if provider == "openai" and not re.search(r"/v\d+$", url):
        url += "/v1"  # the address may be given with or without the /v1 the API lives under
    model = cfg.get("OLLAMA_EMBED_MODEL") if purpose == "embed" else (setting("AI_MODEL") or cfg.get("OLLAMA_MODEL"))
    return Route(provider, url, setting("AI_API_KEY") or "", model or "")


def _headers(route):
    return {"Authorization": f"Bearer {route.key}"} if route.key else {}


def describe():
    cfg = settings()
    route = _route("assistant")
    host = re.sub(r"^\w+://", "", route.url).split("/")[0]
    out = {"model": route.model, "url": route.url, "think": cfg["AI_THINK"], "provider": route.provider,
           "where": "on this computer" if host.startswith(("127.", "localhost", "[::1]")) else f"at {host}"}
    for purpose in ("builder", "fast"):  # a purpose sent to a model or server of its own
        other = _route(purpose)
        if (other.model, other.url) != (route.model, route.url):
            out[purpose] = {"model": other.model, "url": other.url, "provider": other.provider}
    return out


def _unavailable(url, why):
    reason = f"the AI at {url} isn't answering ({why})"
    _down[url] = (time.monotonic() + DOWN_FOR, reason)
    return AIUnavailable(reason)


def _skip_if_down(url):
    until, reason = _down.get(url, (0, ""))
    if time.monotonic() < until:
        raise AIUnavailable(reason)


def check():
    """Raise AIUnavailable unless every model server the AI uses answers: the default one, and the builder's, quick jobs'
    and the embedding model's where they're set apart."""
    if _backend:
        return
    routes = {}
    for purpose in ("assistant", "builder", "fast") + (("embed",) if settings().get("OLLAMA_EMBED_MODEL") else ()):
        route = _route(purpose)
        routes.setdefault((route.provider, route.url), route)
    for route in routes.values():
        if route.provider not in PROVIDERS:
            raise AIUnavailable(f"AI_PROVIDER is {route.provider!r}: it can be ollama or openai (vLLM, SGLang and other OpenAI-compatible servers)")
        _skip_if_down(route.url)
        try:
            response = requests.get(route.url + ("/api/tags" if route.provider == "ollama" else "/models"),
                                    headers=_headers(route), timeout=(5, 15))
        except requests.RequestException as exc:
            raise _unavailable(route.url, type(exc).__name__) from exc
        if response.status_code >= 500:
            raise _unavailable(route.url, f"HTTP {response.status_code}")
        if response.status_code >= 400:  # it's there, but turned us away (a wrong AI_API_KEY): not down
            raise AIUnavailable(f"the AI at {route.url} refused to say what it serves (HTTP {response.status_code}); check AI_API_KEY")
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
    if _backend:
        content, thinking = _backend(messages, schema, on_progress)
    else:
        content, thinking = _ask(messages, schema, think, on_progress, purpose, priority, workspace_id)
    calls = recording.get()
    if calls is not None:
        calls.append({"messages": list(messages), "content": content})
    return content, thinking


def _ask(messages, schema, think, on_progress, purpose, priority, workspace_id):
    """One call through the gateway: wait for a turn at its server, ask, count it."""
    route = _route(purpose)
    deadline = Deadline(float(settings().get("AI_DEADLINE") or 900))
    asked, usage, waited = time.monotonic(), {}, None
    try:
        ask = {"ollama": _ollama_chat, "openai": _openai_chat}.get(route.provider)
        if ask is None:
            raise AIError(f"AI_PROVIDER is {route.provider!r}: it can be ollama or openai (vLLM, SGLang and other OpenAI-compatible servers)")
        _skip_if_down(route.url)  # not worth waiting for a turn to find the server gone
        gate = _gate(route)
        waited = gate.wait(priority, workspace_id, deadline, _waiting_note(on_progress) if on_progress else None)
        try:
            began = time.monotonic()
            _skip_if_down(route.url)  # it went while this call waited
            content, thinking = ask(route, messages, schema, _settings(think, purpose, priority), on_progress, deadline, usage)
            took = time.monotonic() - began
        finally:
            gate.done(workspace_id)
    except Exception as exc:
        _count(purpose, route, waited=time.monotonic() - asked if waited is None else waited, usage=usage, failed=type(exc).__name__)
        raise
    _count(purpose, route, waited=waited, took=took, usage=usage)
    return content, thinking


def _waiting_note(on_progress):
    """While a call waits its turn, what's shown of its thinking says so, instead of nothing at all."""
    def note(running, waiting):
        more = f", and {waiting} more {'is' if waiting == 1 else 'are'} waiting" if waiting else ""
        on_progress(f"Waiting for a turn with the AI: it's busy with {running} other request{'' if running == 1 else 's'}{more}.", "")
    return note


def _settings(think, purpose, priority):
    """What's asked of the model besides the conversation, the same whichever server answers: whether it thinks first,
    how long the answer may be, and how much chance it takes."""
    cfg = settings()
    thinks = cfg["AI_THINK"] if think is None else think
    steady, tries = building.get() or purpose == "builder", again.get()
    seed = cfg.get("AI_BUILDER_SEED")
    return {"think": thinks, "priority": priority,
            "max_tokens": cfg["AI_MAX_TOKENS"] + (cfg.get("AI_THINK_TOKENS", 8000) if thinks else 0),  # the thinking counts towards the cap
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


def _refused(response, route, what):
    """Raise what a model server's error status means (after _post has asked again where that's worth it)."""
    status = response.status_code
    if status < 400:
        return
    if status == 429:
        raise AIUnavailable(f"the AI at {route.url} is too busy right now (HTTP 429)")
    if status in (502, 503, 504):
        raise _unavailable(route.url, f"HTTP {status}")
    raise AIError(f"{'Ollama' if route.provider == 'ollama' else 'The model server'} refused {what} ({status}): {response.text[:300]}")


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
        timeout was cut to it), or else the server gone."""
        return self.deadline.answering() if self.deadline.left() <= 0.05 else _unavailable(self.url, type(exc).__name__)

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
            raise CutOff(f"The AI's answer was cut off at the length limit ({len(text):,} characters written).")
        return text, thinking or "\n".join(x.strip() for x in inline if x.strip())


def _chunk(line, server):
    try:
        return json.loads(line)
    except ValueError as exc:
        raise AIError(f"{server} sent something that couldn't be read: {str(line)[:200]!r}") from exc


def _ollama_chat(route, messages, schema, ask, on_progress, deadline, usage):
    body = {"model": route.model, "messages": messages, "stream": True, "think": ask["think"]}
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


def _openai_chat(route, messages, schema, ask, on_progress, deadline, usage):
    """/v1/chat/completions, streamed as server-sent events: the answer arrives as `content` deltas, the thinking (from a
    server started with a reasoning parser, e.g. vLLM's --reasoning-parser qwen3) as `reasoning_content` deltas
    (`reasoning` in newer vLLM), and what it cost in a last chunk of `usage`."""
    body = {"model": route.model, "messages": messages, "stream": True, "stream_options": {"include_usage": True},
            "max_tokens": ask["max_tokens"], "temperature": ask["temperature"], "top_p": ask["top_p"],
            # Qwen-style models think first or not by a switch in their chat template, which vLLM, SGLang and llama.cpp
            # pass on (a template without the switch ignores it). Ollama's own /v1 ignores it: Ollama is AI_PROVIDER=ollama.
            "chat_template_kwargs": {"enable_thinking": bool(ask["think"])}}
    # (AI_NUM_CTX isn't sent: an OpenAI-compatible server's context length is fixed when it starts, vLLM's --max-model-len)
    if ask["seed"] is not None:
        body["seed"] = ask["seed"]
    if schema:
        # The answer constrained to the schema as it's decoded. vLLM, SGLang, llama.cpp and LM Studio all take this form;
        # vLLM also takes its older `"guided_json": <schema>` in the body.
        body["response_format"] = {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema, "strict": True}}
    if settings().get("AI_SERVER_PRIORITY"):
        body["priority"] = PRIORITIES.index(ask["priority"])  # vLLM's priority scheduling: lower goes first
    response = _post(route, "/chat/completions", body, deadline, usage)
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
    return answer.finish()


def _repeating(text, probe=300, times=4, window=8000):
    """Whether the end of an answer is the same passage written over and over (a model stuck in a loop)."""
    tail = text[-window:]
    end = tail[-probe:]
    return len(end.strip()) >= probe // 3 and len(text) >= probe * times and tail.count(end) >= times


def embed(texts):
    """Vectors for passages of text, from the embedding model (AI_MODEL_EMBED or OLLAMA_EMBED_MODEL, bge-m3 by default):
    passages that mean the same thing get vectors that point the same way, whatever words they use or language they're in.

    Embeddings don't wait in the chat queue: they take a moment, not minutes, and a search shouldn't wait behind the
    builder. They're retried, given the deadline and counted (as "embed") like any call."""
    if _embedder:
        return _embedder(list(texts))
    model = settings().get("OLLAMA_EMBED_MODEL")
    if not model:
        raise AIUnavailable("no embedding model is set (AI_MODEL_EMBED)")
    route, texts = _route("embed"), list(texts)
    deadline, usage, began = Deadline(float(settings().get("AI_DEADLINE") or 900)), {}, time.monotonic()
    try:
        if route.provider not in PROVIDERS:
            raise AIError(f"AI_PROVIDER_EMBED is {route.provider!r}: it can be ollama or openai")
        _skip_if_down(route.url)
        if route.provider == "ollama":
            response = _post(route, "/api/embed", {"model": model, "input": texts, "truncate": True}, deadline, usage,
                             stream=False, read_timeout=300)
        else:
            response = _post(route, "/embeddings", {"model": model, "input": texts}, deadline, usage, stream=False, read_timeout=300)
        if response.status_code == 404:
            raise AIUnavailable(f"the embedding model {model} isn't in Ollama yet (run: ollama pull {model})" if route.provider == "ollama"
                                else f"the server at {route.url} doesn't serve the embedding model {model} (AI_BASE_URL_EMBED)")
        _refused(response, route, "to embed")
        said = response.json()
        if route.provider == "ollama":
            vectors, usage["in"] = said.get("embeddings") or [], said.get("prompt_eval_count")
        else:
            vectors = [x.get("embedding") for x in sorted(said.get("data") or [], key=lambda x: x.get("index", 0))]
            usage["in"] = (said.get("usage") or {}).get("prompt_tokens")
        if len(vectors) != len(texts):
            raise AIError("The embedding model didn't return a vector for every passage.")
    except Exception as exc:
        _count("embed", route, usage=usage, failed=type(exc).__name__)
        raise
    _count("embed", route, took=time.monotonic() - began, usage=usage)
    return vectors


def parse_json(text):
    """The model's answer as a dict (tolerating stray text around the JSON object)."""
    try:
        return json.loads(text)
    except json.JSONDecodeError as first:
        match = re.search(r"\{.*\}", text or "", re.S)
        try:
            if not match:
                raise first
            return json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            log.warning("Unreadable AI answer (%s); it began: %r … and ended: %r", exc, (text or "")[:600], (text or "")[-300:])
            raise AIError(f"The AI's answer wasn't valid JSON: {exc}") from exc
