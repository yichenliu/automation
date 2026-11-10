"""The AI gateway (app/ai.py): OpenAI-compatible servers (vLLM, SGLang) as well as Ollama; a queue in front of each server
with a cap, priorities, turns between workspaces and a deadline; retries when the server is busy; and counters.

The model server is a fake one on a local port speaking the same HTTP (server-sent events for a streamed answer), so
what's sent and how the answer is read are what's tested."""
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app import ai

SCHEMA = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}


def last(body):
    return ((body.get("messages") or [{}])[-1]).get("content")


def _pieces(text, size=6):
    return [text[i:i + size] for i in range(0, len(text), size)]


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):  # /v1/models (or Ollama's /api/tags): what the health check asks
        self.send_json(200, {"object": "list", "data": [{"id": "big"}], "models": []})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        fake = self.server.fake
        with fake.lock:
            fake.seen.append({"path": self.path, "body": body, "headers": dict(self.headers)})
            fake.now += 1
            fake.most = max(fake.most, fake.now)
        try:
            if last(body) == "hold":
                fake.hold.wait(10)
            fake.reply(self, body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the gateway hung up (its deadline)
        finally:
            with fake.lock:
                fake.now -= 1

    def send_json(self, status, said, headers=()):
        data = json.dumps(said).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def events(self, chunks, pause=0.0):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for chunk in chunks:
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.flush()
            if pause:
                time.sleep(pause)
        self.wfile.write(b"data: [DONE]\n\n")

    def stream(self, text, thinking="", usage=(11, 7), reason="stop"):
        """An answer as vLLM streams it: the role, the reasoning, the content, why it stopped, then what it cost."""
        chunks = [{"choices": [{"index": 0, "delta": {"role": "assistant"}}]}]
        chunks += [{"choices": [{"index": 0, "delta": {"reasoning_content": t}}]} for t in _pieces(thinking)]
        chunks += [{"choices": [{"index": 0, "delta": {"content": t}}]} for t in _pieces(text)]
        chunks.append({"choices": [{"index": 0, "delta": {}, "finish_reason": reason}]})
        chunks.append({"choices": [], "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1]}})
        self.events(chunks)


class Fake:
    """A model server on a local port. `reply(handler, body)` answers each request (by default it streams "answer to"
    the last message); every request is kept in `seen` in the order it came; a request saying "hold" waits for `hold`."""

    def __init__(self, reply=None):
        self.reply = reply or (lambda h, body: h.stream(f"answer to {last(body)}"))
        self.seen, self.lock, self.now, self.most = [], threading.Lock(), 0, 0
        self.hold = threading.Event()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.daemon_threads = True
        self.server.fake = self
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def root(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    @property
    def url(self):
        return self.root + "/v1"

    def asked(self):
        return [last(x["body"]) for x in self.seen]

    def close(self):
        self.hold.set()
        self.server.shutdown()
        self.server.server_close()


def _fresh():
    ai._down.clear()
    ai._counts.clear()
    with ai._gates_lock:
        ai._gates.clear()


@pytest.fixture
def fake(app, monkeypatch):
    server = Fake()
    settings = {"AI_PROVIDER": "openai", "AI_BASE_URL": server.url, "AI_API_KEY": "", "AI_MODEL": "big", "AI_CONCURRENCY": 4,
                "AI_DEADLINE": 30, "AI_READ_TIMEOUT": 600, "AI_RETRIES": 3, "AI_BACKOFF": 0.01, "AI_THINK": False,
                "AI_SERVER_PRIORITY": False, "AI_BUILDER_SEED": 7, "AI_BUILDER_TEMPERATURE": 0.3, "AI_TEMPERATURE": 0.6}
    for purpose in ("_BUILDER", "_FAST", "_EMBED"):
        settings.update({f"AI_PROVIDER{purpose}": "", f"AI_BASE_URL{purpose}": "", f"AI_API_KEY{purpose}": ""})
    settings.update({"AI_MODEL_BUILDER": "", "AI_MODEL_FAST": ""})
    for key, value in settings.items():
        monkeypatch.setitem(app.config, key, value)
    _fresh()
    yield server
    server.close()
    _fresh()


def _until(test, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if test():
            return
        time.sleep(0.01)
    raise AssertionError("it never happened")


def _waiting(n):
    """Until n calls wait in the gateway's queues."""
    _until(lambda: sum(sum(q["waiting"].values()) for q in ai.stats()["queues"]) == n)


def _ask_in_thread(app, results, said, **kw):
    def run():
        with app.app_context():
            try:
                results[said] = ai.chat([{"role": "user", "content": said}], **kw)
            except Exception as exc:  # noqa: BLE001 - kept for the test to look at
                results[said] = exc
    thread = threading.Thread(target=run)
    thread.start()
    return thread


def _calls(purpose):
    return next(c for c in ai.stats()["calls"] if c["purpose"] == purpose)


def test_an_openai_compatible_server_streams_the_answer_and_its_thinking(app, fake, monkeypatch):
    monkeypatch.setitem(app.config, "AI_API_KEY", "sekrit")
    fake.reply = lambda h, body: h.stream('{"name": "Clients"}', thinking="The owner keeps clients. So a list.", usage=(120, 9))
    shown = []
    content, thinking = ai.chat([{"role": "user", "content": "clients"}], schema=SCHEMA, think=True,
                                on_progress=lambda t, c: shown.append((t, c)))
    assert content == '{"name": "Clients"}' and thinking == "The owner keeps clients. So a list."
    assert shown[-1] == (thinking, content)  # the screen ends showing the whole answer
    sent = fake.seen[0]
    assert sent["path"] == "/v1/chat/completions" and sent["headers"]["Authorization"] == "Bearer sekrit"
    body = sent["body"]
    assert body["model"] == "big" and body["stream"] is True and body["stream_options"] == {"include_usage": True}
    assert body["response_format"] == {"type": "json_schema", "json_schema": {"name": "answer", "schema": SCHEMA, "strict": True}}
    assert body["chat_template_kwargs"] == {"enable_thinking": True}
    assert "max_tokens" not in body and "max_completion_tokens" not in body  # thinking on our own server: no budget
    assert body["temperature"] == 0.6 and body["top_p"] == 0.9 and "seed" not in body and "priority" not in body
    calls = _calls("assistant")
    assert (calls["provider"], calls["model"], calls["calls"], calls["answered"]) == ("compatible", "big", 1, 1)
    assert (calls["tokens_in"], calls["tokens_out"], calls["failures"]) == (120, 9, 0)


def test_thinking_written_into_the_answer_is_taken_out_of_it(app, fake):
    """A server started without a reasoning parser sends the thinking inside the content, between <think> tags (or only
    the closing one, when the chat template opened it)."""
    fake.reply = lambda h, body: h.stream("<think>Clients first.</think>\n{}")
    assert ai.chat([{"role": "user", "content": "a"}]) == ("{}", "Clients first.")
    fake.reply = lambda h, body: h.stream("Clients first.\n</think>\n\n{}")
    assert ai.chat([{"role": "user", "content": "b"}]) == ("{}", "Clients first.")
    fake.reply = lambda h, body: h.stream('{"name": "Cli', reason="length")
    with pytest.raises(ai.CutOff):
        ai.chat([{"role": "user", "content": "c"}])


def test_each_purpose_goes_to_its_own_model_and_server(app, fake, monkeypatch):
    small = Fake()
    try:
        monkeypatch.setitem(app.config, "AI_MODEL_BUILDER", "huge")
        monkeypatch.setitem(app.config, "AI_MODEL_FAST", "small")
        monkeypatch.setitem(app.config, "AI_BASE_URL_FAST", small.root)  # given without /v1: it's added
        steady = ai.building.set(True)
        try:
            ai.chat([{"role": "user", "content": "design"}], schema=SCHEMA)
        finally:
            ai.building.reset(steady)
        ai.chat([{"role": "user", "content": "quick"}], purpose="fast", think=False)
        monkeypatch.setitem(app.config, "AI_SERVER_PRIORITY", True)
        ai.chat([{"role": "user", "content": "question"}])
    finally:
        small.close()
    designed, asked = fake.seen[0]["body"], fake.seen[1]["body"]
    assert designed["model"] == "huge" and designed["seed"] == 7 and designed["temperature"] == 0.3  # the builder is steady
    assert designed["chat_template_kwargs"] == {"enable_thinking": False}
    assert small.seen[0]["path"] == "/v1/chat/completions" and small.seen[0]["body"]["model"] == "small"
    assert asked["model"] == "big" and asked["priority"] == 0  # interactive: first in vLLM's priority scheduling
    assert {c["purpose"] for c in ai.stats()["calls"]} == {"builder", "fast", "assistant"}
    assert {q["server"] for q in ai.stats()["queues"]} == {f"compatible at {fake.url}", f"compatible at {small.url}"}


def test_embeddings_come_from_v1_embeddings_in_the_order_asked(app, fake, monkeypatch):
    """vLLM serves one model per server, so the embedding model has its own (AI_BASE_URL_EMBED)."""
    monkeypatch.setitem(app.config, "AI_BASE_URL", "http://127.0.0.1:9/v1")  # the chat model's server: not asked
    monkeypatch.setitem(app.config, "AI_BASE_URL_EMBED", fake.url)
    monkeypatch.setitem(app.config, "OLLAMA_EMBED_MODEL", "bge-m3")
    fake.reply = lambda h, body: h.send_json(200, {"object": "list", "usage": {"prompt_tokens": 6, "total_tokens": 6},
                                                   "data": [{"index": 1, "embedding": [0.0, 1.0]}, {"index": 0, "embedding": [1.0, 0.0]}]})
    assert ai.embed(["cat", "dog"]) == [[1.0, 0.0], [0.0, 1.0]]
    assert fake.seen[0]["path"] == "/v1/embeddings" and fake.seen[0]["body"] == {"model": "bge-m3", "input": ["cat", "dog"]}
    embedded = _calls("embed")
    assert (embedded["answered"], embedded["tokens_in"], embedded["model"]) == (1, 6, "bge-m3")


def test_a_busy_server_is_asked_again(app, fake):
    statuses = iter([503, 429, None])

    def reply(h, body):
        status = next(statuses)
        if status:
            h.send_json(status, {"error": {"message": "busy"}}, headers=[("Retry-After", "0")] if status == 429 else [])
        else:
            h.stream("{}")
    fake.reply = reply
    assert ai.chat([{"role": "user", "content": "hi"}])[0] == "{}"
    assert len(fake.seen) == 3 and not ai._down
    assert _calls("assistant")["retries"] == 2 and _calls("assistant")["failures"] == 0


def test_a_refused_request_is_not_asked_again(app, fake):
    """Refused, naming no part of the request that could be sent another way: asking again gets the same answer."""
    fake.reply = lambda h, body: h.send_json(400, {"error": {"message": "the conversation isn't valid"}})
    with pytest.raises(ai.AIError) as refused:
        ai.chat([{"role": "user", "content": "hi"}], schema=SCHEMA)
    assert not isinstance(refused.value, ai.AIUnavailable) and "the conversation isn't valid" in str(refused.value)
    assert len(fake.seen) == 1 and not ai._down
    assert _calls("assistant")["failed"] == {"AIError": 1}


def test_a_server_that_stays_down_is_skipped_for_a_while(app, fake, monkeypatch):
    monkeypatch.setitem(app.config, "AI_RETRIES", 2)
    fake.reply = lambda h, body: h.send_json(503, {"error": "the model is loading"})
    with pytest.raises(ai.AIUnavailable):
        ai.chat([{"role": "user", "content": "hi"}])
    assert len(fake.seen) == 3 and fake.url in ai._down  # asked three times, then taken as down
    with pytest.raises(ai.AIUnavailable):
        ai.chat([{"role": "user", "content": "again"}])
    assert len(fake.seen) == 3  # skipped while it's down, not waited out again
    with socket.socket() as s:  # a server that isn't there at all
        s.bind(("127.0.0.1", 0))
        gone = f"http://127.0.0.1:{s.getsockname()[1]}/v1"
    monkeypatch.setitem(app.config, "AI_BASE_URL", gone)
    with pytest.raises(ai.AIUnavailable, match="isn't answering"):
        ai.chat([{"role": "user", "content": "hi"}])
    assert gone in ai._down and _calls("assistant")["failures"] == 3


def test_the_deadline_stops_an_answer_that_takes_too_long(app, fake, monkeypatch):
    monkeypatch.setitem(app.config, "AI_DEADLINE", 0.8)
    fake.reply = lambda h, body: h.events(({"choices": [{"index": 0, "delta": {"content": f"word{n} "}}]} for n in range(100)), pause=0.05)
    began = time.monotonic()
    with pytest.raises(ai.Late, match="longer than allowed to answer"):
        ai.chat([{"role": "user", "content": "a lot"}])
    assert time.monotonic() - began < 3 and not ai._down  # busy, not gone: other calls still go
    fake.reply = lambda h, body: time.sleep(3)  # and a server that says nothing at all
    began = time.monotonic()
    with pytest.raises(ai.Late):
        ai.chat([{"role": "user", "content": "silence"}])
    assert time.monotonic() - began < 2.5 and not ai._down
    assert isinstance(ai.Late("x"), ai.AIUnavailable)  # the work waits and tries again, as when the AI is off


def test_a_call_that_waits_past_its_deadline_gives_up_without_reaching_the_server(app, fake, monkeypatch):
    monkeypatch.setitem(app.config, "AI_CONCURRENCY", 1)
    results = {}
    holding = _ask_in_thread(app, results, "hold")
    _until(lambda: fake.now == 1)
    monkeypatch.setitem(app.config, "AI_DEADLINE", 0.5)
    with pytest.raises(ai.Late, match="seconds for its turn"):
        ai.chat([{"role": "user", "content": "late"}])
    fake.hold.set()
    holding.join(10)
    assert fake.asked() == ["hold"] and isinstance(results["hold"], tuple)
    assert ai.stats()["queues"][0]["waiting"] == {"interactive": 0, "build": 0, "background": 0}  # it left the queue
    assert ai.stats()["queues"][0]["running"] == 0


def test_the_owner_waiting_goes_before_the_builder_and_the_builder_before_background_work(app, fake, monkeypatch):
    monkeypatch.setitem(app.config, "AI_CONCURRENCY", 1)
    results = {}
    threads = [_ask_in_thread(app, results, "hold", priority="background")]
    _until(lambda: fake.now == 1)
    for n, (said, priority) in enumerate((("later", "background"), ("build", "build"), ("now", "interactive"))):
        threads.append(_ask_in_thread(app, results, said, priority=priority))
        _waiting(n + 1)
    fake.hold.set()
    for thread in threads:
        thread.join(10)
    assert fake.asked() == ["hold", "now", "build", "later"]
    assert all(isinstance(r, tuple) for r in results.values())


def test_workspaces_take_turns_so_one_customers_many_calls_dont_starve_anothers(app, fake, monkeypatch):
    monkeypatch.setitem(app.config, "AI_CONCURRENCY", 1)
    results = {}
    threads = [_ask_in_thread(app, results, "hold", priority="build", workspace_id=1)]
    _until(lambda: fake.now == 1)
    for n, (said, workspace) in enumerate((("a1", 1), ("a2", 1), ("a3", 1), ("a4", 1), ("b1", 2))):
        threads.append(_ask_in_thread(app, results, said, priority="build", workspace_id=workspace))
        _waiting(n + 1)
    fake.hold.set()
    for thread in threads:
        thread.join(10)
    assert fake.asked() == ["hold", "a1", "b1", "a2", "a3", "a4"]  # first come, first served would leave b1 till last


def test_the_workspace_with_fewest_calls_running_has_the_next_turn():
    """With a customer already holding every place, another customer's call goes next, ahead of the first's next one."""
    gate, deadline, order = ai.Gate("test", 2), ai.Deadline(10), []
    gate.wait("build", "a", deadline)
    gate.wait("build", "a", deadline)

    def wait(name, workspace):
        gate.wait("build", workspace, deadline)
        order.append(name)
    threads = []
    for n, (name, workspace) in enumerate((("a3", "a"), ("b1", "b"))):
        threads.append(threading.Thread(target=wait, args=(name, workspace)))
        threads[-1].start()
        _until(lambda: sum(len(q) for q in gate.waiting["build"].values()) == n + 1)
    gate.done("a")
    _until(lambda: order == ["b1"])
    gate.done("a")
    _until(lambda: order == ["b1", "a3"])
    for thread in threads:
        thread.join(5)
    assert gate.running == 2 and gate.running_for == {"a": 1, "b": 1}


def test_no_more_than_the_cap_go_at_once(app, fake, monkeypatch):
    monkeypatch.setitem(app.config, "AI_CONCURRENCY", 2)
    fake.reply = lambda h, body: (time.sleep(0.2), h.stream("{}"))
    results = {}
    threads = [_ask_in_thread(app, results, f"q{n}", workspace_id=n) for n in range(6)]
    for thread in threads:
        thread.join(15)
    assert len(fake.seen) == 6 and all(r == ("{}", "") for r in results.values())
    assert fake.most == 2
    calls = _calls("assistant")
    assert calls["answered"] == 6 and calls["longest_wait"] > 0.1 and ai.stats()["queues"][0]["limit"] == 2
    monkeypatch.setitem(app.config, "AI_CONCURRENCY", 0)  # the provider's own default
    assert ai._gate(ai._route("assistant")).limit == 8
    monkeypatch.setitem(app.config, "AI_PROVIDER", "ollama")
    assert ai._gate(ai._route("assistant")).limit == 2


def test_a_call_waiting_its_turn_says_so_on_its_progress(app, fake, monkeypatch):
    monkeypatch.setitem(app.config, "AI_CONCURRENCY", 1)
    results, shown = {}, []
    holding = _ask_in_thread(app, results, "hold")
    _until(lambda: fake.now == 1)
    waiting = _ask_in_thread(app, results, "next", on_progress=lambda t, c: shown.append((t, c)))
    _waiting(1)
    fake.hold.set()
    holding.join(10)
    waiting.join(10)
    assert shown[0] == ("Waiting for a turn with the AI: it's busy with 1 other request.", "")
    assert results["next"] == ("answer to next", "") and shown[-1] == ("", "answer to next")  # the note isn't the thinking


def test_ollama_still_answers_as_it_did_and_says_what_it_cost(app, fake, monkeypatch):
    monkeypatch.setitem(app.config, "AI_PROVIDER", "ollama")
    monkeypatch.setitem(app.config, "AI_BASE_URL", fake.root)

    def reply(h, body):
        h.send_response(200)
        h.send_header("Content-Type", "application/x-ndjson")
        h.end_headers()
        for line in ({"message": {"thinking": "Hmm."}}, {"message": {"content": "{}"}},
                     {"message": {"content": ""}, "done": True, "done_reason": "stop", "prompt_eval_count": 50, "eval_count": 3}):
            h.wfile.write((json.dumps(line) + "\n").encode())
    fake.reply = reply
    assert ai.chat([{"role": "user", "content": "hi"}], schema=SCHEMA, think=True) == ("{}", "Hmm.")
    chats = lambda: [s for s in fake.seen if s["path"] == "/api/chat"]  # noqa: E731 - besides asking what levels it has
    sent = chats()[0]
    assert sent["body"]["format"] == SCHEMA and sent["body"]["think"] is True  # an Ollama that names no levels: on
    assert sent["body"]["options"]["num_predict"] == -1  # thinking: as long as the model takes, its effort level says how long
    calls = _calls("assistant")
    assert (calls["provider"], calls["tokens_in"], calls["tokens_out"]) == ("ollama", 50, 3)
    fake.reply = lambda h, body: h.send_json(400, {"error": "invalid format"})
    with pytest.raises(ai.AIError, match="Ollama refused the request"):
        ai.chat([{"role": "user", "content": "hi"}], schema=SCHEMA)
    assert len(chats()) == 2


def test_a_local_model_with_thinking_levels_thinks_at_the_effort_asked(app, fake, monkeypatch):
    """Qwen 3.8 through Ollama has levels (false, low, medium, xhigh): "think": true is its default, medium, so the
    builder thought at medium while AI_EFFORT said max. It's told the level now, or the nearest below that it has."""
    monkeypatch.setitem(app.config, "AI_PROVIDER", "ollama")
    monkeypatch.setitem(app.config, "AI_BASE_URL", fake.root)
    levels = {"values": [False, "low", "medium", "xhigh"], "default": "medium"}

    def reply(h, body):
        if h.path == "/api/show":
            return h.send_json(200, {"capabilities": ["completion", "thinking"], "thinking": levels})
        h.send_response(200)
        h.send_header("Content-Type", "application/x-ndjson")
        h.end_headers()
        h.wfile.write((json.dumps({"message": {"content": "ok"}, "done": True, "done_reason": "stop"}) + "\n").encode())
    fake.reply = reply
    sent = lambda: [s["body"]["think"] for s in fake.seen if s["path"] == "/api/chat"][-1]  # noqa: E731
    for effort, think, told in (("xhigh", True, "xhigh"), ("max", True, "xhigh"), ("high", True, "medium"),
                                ("low", True, "low"), ("xhigh", False, False)):
        monkeypatch.setitem(app.config, "AI_EFFORT", effort)
        ai.chat([{"role": "user", "content": "hi"}], think=think)
        assert sent() == told, (effort, think)
    assert sum(1 for s in fake.seen if s["path"] == "/api/show") == 1  # asked once a model
    ai._levels.clear()
    levels = {"values": ["low", "medium", "high"], "default": "medium"}  # one that always thinks: the least it does
    ai.chat([{"role": "user", "content": "hi"}], think=False)
    assert sent() == "low"


def test_what_a_call_is_for_comes_from_where_its_asked(app, owner):
    from app.web.testing import a_request
    _, ws = owner
    assert ai._who() == ("assistant", "interactive", None)
    assert ai._who("fast", "build", 9) == ("fast", "build", 9)
    steady = ai.building.set(True)
    try:
        assert ai._who() == ("builder", "build", None)
    finally:
        ai.building.reset(steady)
    with ai.asking(priority="background", workspace_id=ws.id):
        assert ai._who() == ("assistant", "background", ws.id)
    with a_request() as request:  # a person's request in a workspace
        request.state.ws = ws
        assert ai._who() == ("assistant", "interactive", ws.id)
    seen = {}

    def job():
        with app.app_context():
            seen["who"] = ai._who()
    worker = threading.Thread(target=job, name="jobs")  # the worker's thread for jobs (app/worker.py)
    worker.start()
    worker.join(5)
    assert seen["who"] == ("background", "background", None)
    with pytest.raises(ValueError):
        ai.chat([{"role": "user", "content": "hi"}], priority="soon")


def test_health_shows_what_the_ai_was_asked(app, fake, owner):
    from app import health
    _, ws = owner
    ai.chat([{"role": "user", "content": "hi"}])
    parts = {p["name"]: p for p in health.report(ws)}
    assert parts["The AI"]["ok"] and parts["The AI"]["said"] == "big is answering on this computer"
    assert parts["Asking the AI"]["ok"] and parts["Asking the AI"]["said"].startswith("1 asked since the app started, 0 went wrong")
    assert "11 tokens read, 7 written" in parts["Asking the AI"]["said"]


def test_an_answer_that_stalls_part_way_is_stuck_not_the_server_gone(app, fake, monkeypatch):
    """Round 45: Ollama kept writing for 11 minutes but sent nothing for ten of them, the read timed out, and the
    gateway took the server for gone: every call for the next 20 seconds failed at once, and the round ended. A server
    that answered and then went quiet is a stalled answer (Stuck, which the builder answers again without thinking);
    the next call is asked as usual."""
    monkeypatch.setitem(app.config, "AI_READ_TIMEOUT", 1)

    def reply(h, body):
        if last(body) == "stall":
            h.events([{"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
                      {"choices": [{"index": 0, "delta": {"content": "a start"}}]}], pause=2.5)
        else:
            h.stream("fine")
    fake.reply = reply
    with pytest.raises(ai.Stuck, match="went quiet part way"):
        ai.chat([{"role": "user", "content": "stall"}])
    assert not ai._down
    assert ai.chat([{"role": "user", "content": "next"}])[0] == "fine"
