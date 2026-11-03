"""The AI: a model on this computer, reached through Ollama.

`chat` streams the model's answer, calling `on_progress(thinking, content)` as it goes so the builder can show what the
model is doing while it works. A model that can't be reached raises AIUnavailable: the work waits and tries again
later instead of failing.
"""
import contextvars
import json
import logging
import re
import time

import requests
from flask import current_app

log = logging.getLogger("ai")


class AIError(Exception):
    """The model answered, but not with something usable (or Ollama refused the request)."""


class AIUnavailable(AIError):
    """The model can't be reached right now (Ollama not running, the computer asleep). The work waits and retries."""


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


def set_backend(fn):
    """Tests: replace the model with fn(messages, schema, on_progress) -> (content, thinking)."""
    global _backend
    _backend = fn


def set_embedder(fn):
    """Tests: replace the embedding model with fn(texts) -> [[float]]."""
    global _embedder
    _embedder = fn


def describe():
    cfg = current_app.config
    return {"model": cfg["OLLAMA_MODEL"], "url": cfg["OLLAMA_URL"], "think": cfg["AI_THINK"]}


def _unavailable(url, why):
    reason = f"the AI at {url} isn't answering ({why})"
    _down[url] = (time.monotonic() + DOWN_FOR, reason)
    return AIUnavailable(reason)


def check():
    """Raise AIUnavailable unless Ollama answers."""
    if _backend:
        return
    url = current_app.config["OLLAMA_URL"]
    until, reason = _down.get(url, (0, ""))
    if time.monotonic() < until:
        raise AIUnavailable(reason)
    try:
        requests.get(f"{url}/api/tags", timeout=(5, 15)).raise_for_status()
    except requests.RequestException as exc:
        raise _unavailable(url, type(exc).__name__) from exc
    _down.pop(url, None)


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
    if current_app.config.get("TESTING"):
        raise AssertionError("the AI was asked while this thread held unsaved writes: commit first")
    log.warning("The AI was asked while unsaved writes held the database", stack_info=True)
    if not db.session().in_nested_transaction():
        db.session.commit()


def chat(messages, *, schema=None, think=None, on_progress=None):
    """Send a conversation, return (content, thinking). With a JSON schema, the answer is constrained to it."""
    if current_app.config.get("TRYING_OUT"):  # a throwaway copy of an app being tried: its AI steps wait, as if it were off
        raise AIUnavailable("the AI isn't asked while an app is being tried out")
    _let_go()
    content, thinking = _backend(messages, schema, on_progress) if _backend else _chat(messages, schema, think, on_progress)
    calls = recording.get()
    if calls is not None:
        calls.append({"messages": list(messages), "content": content})
    return content, thinking


def _chat(messages, schema, think, on_progress):
    cfg = current_app.config
    url = cfg["OLLAMA_URL"]
    until, reason = _down.get(url, (0, ""))
    if time.monotonic() < until:
        raise AIUnavailable(reason)
    thinks = cfg["AI_THINK"] if think is None else think
    body = {"model": cfg["OLLAMA_MODEL"], "messages": messages, "stream": True, "think": thinks}
    if schema:
        body["format"] = schema
    budget = cfg["AI_MAX_TOKENS"] + (cfg.get("AI_THINK_TOKENS", 8000) if thinks else 0)  # the thinking counts towards the cap
    steady = building.get()
    body["options"] = {"num_predict": budget, "top_p": 0.9,
                       "temperature": cfg.get("AI_BUILDER_TEMPERATURE", 0.3) if steady else cfg.get("AI_TEMPERATURE", 0.6),
                       **({"seed": cfg["AI_BUILDER_SEED"]} if steady and cfg.get("AI_BUILDER_SEED") is not None else {}),
                       **({"num_ctx": cfg["AI_NUM_CTX"]} if cfg["AI_NUM_CTX"] else {})}
    try:
        response = requests.post(f"{url}/api/chat", json=body, stream=True, timeout=(10, 600))
    except (requests.ConnectionError, requests.Timeout) as exc:
        raise _unavailable(url, type(exc).__name__) from exc
    if response.status_code in (502, 503, 504):
        raise _unavailable(url, f"HTTP {response.status_code}")
    if response.status_code >= 400:
        raise AIError(f"Ollama refused the request ({response.status_code}): {response.text[:300]}")
    _down.pop(url, None)
    content, thinking, last, blank, reason, size, looked = [], [], 0.0, 0, None, 0, 0
    thought, pondered = 0, 0
    try:
        for line in response.iter_lines():
            if not line:
                continue
            chunk = json.loads(line)
            if chunk.get("error"):
                raise AIError(f"Ollama: {chunk['error']}")
            message = chunk.get("message") or {}
            if message.get("thinking"):
                thinking.append(message["thinking"])
                thought += len(message["thinking"])
                if thought - pondered >= 3000:  # thinking that goes round in circles: stop it now, not at the length limit
                    pondered = thought
                    if _repeating("".join(thinking)):
                        response.close()
                        raise Stuck("The AI got stuck thinking (it kept going over the same ground).")
            if message.get("content"):
                content.append(message["content"])
                blank = blank + len(message["content"]) if not message["content"].strip() else 0
                if blank > 400:  # constrained JSON output sometimes degenerates into endless whitespace
                    response.close()
                    raise AIError("The AI got stuck writing its answer (it was only writing blank space).")
                size += len(message["content"])
                if size - looked >= 1500:  # or into the same passage over and over: stop now, not at the length limit
                    looked = size
                    if _repeating("".join(content)):
                        response.close()
                        raise Stuck("The AI got stuck writing its answer (it kept repeating itself).")
            if chunk.get("done"):
                reason = chunk.get("done_reason")
            if on_progress and (time.monotonic() - last > 0.6 or chunk.get("done")):
                last = time.monotonic()
                on_progress("".join(thinking), "".join(content))
    except requests.RequestException as exc:
        raise _unavailable(url, type(exc).__name__) from exc
    text = re.sub(r"<think>.*?</think>", "", "".join(content), flags=re.S).strip()
    if reason == "length":
        raise CutOff(f"The AI's answer was cut off at the length limit ({len(text):,} characters written).")
    return text, "".join(thinking)


def _repeating(text, probe=300, times=4, window=8000):
    """Whether the end of an answer is the same passage written over and over (a model stuck in a loop)."""
    tail = text[-window:]
    end = tail[-probe:]
    return len(end.strip()) >= probe // 3 and len(text) >= probe * times and tail.count(end) >= times


def embed(texts):
    """Vectors for passages of text, from the embedding model (OLLAMA_EMBED_MODEL, bge-m3 by default): passages that
    mean the same thing get vectors that point the same way, whatever words they use or language they're in."""
    if _embedder:
        return _embedder(list(texts))
    cfg = current_app.config
    url, model = cfg["OLLAMA_URL"], cfg.get("OLLAMA_EMBED_MODEL")
    if not model:
        raise AIUnavailable("no embedding model is set (OLLAMA_EMBED_MODEL)")
    until, reason = _down.get(url, (0, ""))
    if time.monotonic() < until:
        raise AIUnavailable(reason)
    try:
        response = requests.post(f"{url}/api/embed", json={"model": model, "input": list(texts), "truncate": True}, timeout=(10, 300))
    except (requests.ConnectionError, requests.Timeout) as exc:
        raise _unavailable(url, type(exc).__name__) from exc
    if response.status_code == 404:
        raise AIUnavailable(f"the embedding model {model} isn't in Ollama yet (run: ollama pull {model})")
    if response.status_code >= 500:
        raise _unavailable(url, f"HTTP {response.status_code}")
    if response.status_code >= 400:
        raise AIError(f"Ollama refused to embed ({response.status_code}): {response.text[:300]}")
    vectors = response.json().get("embeddings") or []
    if len(vectors) != len(texts):
        raise AIError("The embedding model didn't return a vector for every passage.")
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
