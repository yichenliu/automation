"""Cloud and local AI providers (app/ai_providers.py, app/ai.py): which one each purpose uses (the platform's choice
under Settings › AI before the .env settings), each provider asked its own way, and what a model refuses learned
from its refusal and sent another way.

The providers are fake servers on local ports speaking their HTTP: OpenAI's chat completions and Anthropic's
messages, streamed as server-sent events."""
import json

import pytest

from app import ai
from app import ai_providers as providers
from app.security import encrypt

from .test_ai_gateway import SCHEMA, Fake, _fresh, _pieces


def anthropic_stream(h, text, thinking="", stop="end_turn", usage=(40, 12)):
    """An answer as Anthropic streams it: message_start, a thinking block, a text block, message_delta, message_stop."""
    events = [{"type": "message_start", "message": {"id": "msg_1", "role": "assistant", "content": [],
                                                    "usage": {"input_tokens": usage[0], "cache_read_input_tokens": 100, "output_tokens": 1}}}]
    index = 0
    if thinking:
        events.append({"type": "content_block_start", "index": index, "content_block": {"type": "thinking", "thinking": ""}})
        events += [{"type": "content_block_delta", "index": index, "delta": {"type": "thinking_delta", "thinking": t}} for t in _pieces(thinking)]
        events.append({"type": "content_block_delta", "index": index, "delta": {"type": "signature_delta", "signature": "sig"}})
        events.append({"type": "content_block_stop", "index": index})
        index += 1
    events.append({"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}})
    events += [{"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": t}} for t in _pieces(text)]
    events.append({"type": "content_block_stop", "index": index})
    events.append({"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": usage[1]}})
    events.append({"type": "message_stop"})
    h.send_response(200)
    h.send_header("Content-Type", "text/event-stream")
    h.end_headers()
    for event in events:
        h.wfile.write(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode())
        h.wfile.flush()


def refuse(h, message, status=400, anthropic=False):
    said = {"type": "error", "error": {"type": "invalid_request_error", "message": message}} if anthropic else \
        {"error": {"message": message, "type": "invalid_request_error"}}
    h.send_json(status, said)


@pytest.fixture
def server(app, monkeypatch):
    fake = Fake()
    for key, value in {"AI_PROVIDER": "ollama", "AI_BASE_URL": "", "AI_API_KEY": "", "AI_MODEL": "", "AI_CONCURRENCY": 4,
                       "AI_DEADLINE": 30, "AI_RETRIES": 3, "AI_BACKOFF": 0.01, "AI_THINK": False, "AI_SERVER_PRIORITY": False,
                       "AI_BUILDER_SEED": 7, "AI_EFFORT": "high", "OLLAMA_EMBED_MODEL": "bge-m3"}.items():
        monkeypatch.setitem(app.config, key, value)
    for tag in ("_BUILDER", "_FAST", "_EMBED"):
        for name in ("AI_PROVIDER", "AI_BASE_URL", "AI_API_KEY"):
            monkeypatch.setitem(app.config, name + tag, "")
    for name in ("AI_MODEL_BUILDER", "AI_MODEL_FAST", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.setitem(app.config, name, "")
    _fresh()
    ai._learned.clear()
    providers._listed.clear()
    yield fake
    fake.close()
    _fresh()
    ai._learned.clear()


def choose(provider, model, url, key="k-123", **more):
    providers.save({"use": {"assistant": {"provider": provider, "model": model}, **more},
                    "keys": {provider: encrypt(key)}, "urls": {provider: url}}, by="test")


# Which one each purpose uses ---------------------------------------------------------------------------------------

def test_the_platforms_choice_comes_before_the_env_and_each_purpose_can_go_its_own_way(app, server, monkeypatch):
    monkeypatch.setitem(app.config, "AI_MODEL", "qwen-local")
    assert ai._route("assistant")[0::3] == ("ollama", "qwen-local")
    monkeypatch.setitem(app.config, "ANTHROPIC_API_KEY", "from-env")
    providers.save({"use": {"assistant": {"provider": "anthropic", "model": "claude-opus-5-5"},
                            "builder": {"provider": "openai", "model": "gpt-9"}},
                    "keys": {"openai": encrypt("sk-saved")}}, by="test")
    assistant, builder, background = ai._route("assistant"), ai._route("builder"), ai._route("background")
    assert assistant == ai.Route("anthropic", "https://api.anthropic.com/v1", "from-env", "claude-opus-5-5")
    assert builder == ai.Route("openai", "https://api.openai.com/v1", "sk-saved", "gpt-9")
    assert background == assistant and ai._route("fast") == assistant  # quick jobs go with the assistant unless chosen apart
    embed = ai._route("embed")  # Anthropic has no embedding model: search by meaning stays on this computer
    assert embed.provider == "ollama" and embed.model == "bge-m3" and ai.embed_model() == "bge-m3"
    described = ai.describe()
    assert described["cloud"] and described["name"] == "Anthropic (Claude)" and described["builder"]["name"] == "OpenAI"
    assert described["any_cloud"] and described["where"] == "at Anthropic (Claude)"


def test_the_env_names_any_provider_and_openai_at_an_address_of_your_own_is_your_own_server(app, server, monkeypatch):
    monkeypatch.setitem(app.config, "AI_PROVIDER", "openai")
    monkeypatch.setitem(app.config, "AI_BASE_URL", "http://gpu1:8000")
    assert ai._route("assistant")[:2] == ("compatible", "http://gpu1:8000/v1")
    monkeypatch.setitem(app.config, "AI_BASE_URL", "")
    monkeypatch.setitem(app.config, "OPENAI_API_KEY", "sk-env")
    assert ai._route("assistant")[:3] == ("openai", "https://api.openai.com/v1", "sk-env")
    assert ai._route("embed").provider == "ollama"  # a cloud provider for the chat doesn't take search with it unasked
    monkeypatch.setitem(app.config, "AI_PROVIDER", "claude")  # what people call it works too
    monkeypatch.setitem(app.config, "AI_PROVIDER_BUILDER", "gemini")
    monkeypatch.setitem(app.config, "GEMINI_API_KEY", "g-key")
    assert ai._route("assistant").provider == "anthropic"
    builder = ai._route("builder")  # its own provider: not the plain one's address or key
    assert builder[:3] == ("gemini", "https://generativelanguage.googleapis.com/v1beta/openai", "g-key")
    monkeypatch.setitem(app.config, "AI_PROVIDER", "azure")
    monkeypatch.setitem(app.config, "AZURE_OPENAI_ENDPOINT", "https://acme.openai.azure.com")
    assert ai._route("assistant").url == "https://acme.openai.azure.com/openai/v1"


def test_the_best_model_is_the_newest_of_the_leading_family():
    assert providers.best_model("openai", ["gpt-4o", "gpt-5", "gpt-5.1", "gpt-5.2", "gpt-5.2-mini", "gpt-5.2-2025-12-11"]) == "gpt-5.2"
    assert providers.best_model("anthropic", ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-4-8", "claude-opus-5-5",
                                              "claude-opus-4-5-20251101"]) == "claude-opus-5-5"
    assert providers.best_model("gemini", ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-3-pro-preview"]) == "gemini-2.5-pro"
    assert providers.best_model("openrouter", ["anything"]) == ""


# Asking each provider its way --------------------------------------------------------------------------------------

def test_openai_is_asked_its_way_and_what_a_model_refuses_is_learned_once(app, server):
    def reply(h, body):
        if "temperature" in body:
            return refuse(h, "Unsupported value: 'temperature' does not support 0.6 with this model. Only the default (1) value is supported.")
        if "top_p" in body:
            return refuse(h, "Unsupported parameter: 'top_p' is not supported with this model.")
        if body.get("reasoning_effort") == "none":
            return refuse(h, "Unsupported value: 'reasoning_effort' does not support 'none' with this model. Supported values are: "
                             "'minimal', 'low', 'medium', and 'high'.")
        h.stream('{"name": "Clients"}')
    server.reply = reply
    choose("openai", "gpt-9", server.url, key="sk-test")
    content, _ = ai.chat([{"role": "system", "content": "You design."}, {"role": "user", "content": "clients"}], schema=SCHEMA)
    assert content == '{"name": "Clients"}'
    sent = server.seen[-1]
    assert sent["headers"]["Authorization"] == "Bearer sk-test"
    body = sent["body"]
    assert body["max_completion_tokens"] == app.config["AI_MAX_TOKENS"] and "max_tokens" not in body
    assert body["reasoning_effort"] == "minimal" and "temperature" not in body and "top_p" not in body
    assert "chat_template_kwargs" not in body  # a vLLM switch OpenAI doesn't know
    assert body["response_format"]["json_schema"]["strict"] is False  # followed, not constrained: any schema is taken
    asked = len(server.seen)
    assert asked == 4  # refused three times, each naming what it wouldn't take
    ai.chat([{"role": "user", "content": "again"}], schema=SCHEMA)
    assert len(server.seen) == asked + 1  # learned: asked the way it takes straight away
    ai.chat([{"role": "user", "content": "think"}], schema=SCHEMA, think=True)
    assert server.seen[-1]["body"]["reasoning_effort"] == "high"


def test_a_schema_a_provider_wont_take_is_written_into_the_prompt(app, server):
    def reply(h, body):
        kind = (body.get("response_format") or {}).get("type")
        if kind == "json_schema":
            return refuse(h, "Invalid schema for response_format 'answer': In context=(), 'additionalProperties' is required")
        if kind == "json_object":
            return refuse(h, "response_format of type json_object is not supported with this model")
        h.stream('{"name": "Clients"}')
    server.reply = reply
    choose("deepseek", "deepseek-chat", server.url)
    content, _ = ai.chat([{"role": "system", "content": "You design."}, {"role": "user", "content": "clients"}], schema=SCHEMA)
    assert content == '{"name": "Clients"}'
    body = server.seen[-1]["body"]
    assert "response_format" not in body
    system = body["messages"][0]["content"]
    assert system.startswith("You design.") and "JSON schema" in system and '"required":["name"]' in system


def test_a_cap_on_the_answers_length_is_learned_from_the_refusal(app, server):
    def reply(h, body):
        if body.get("max_tokens", 0) > 8192:
            return refuse(h, f"max_tokens is too large: {body['max_tokens']}. This model supports at most 8192 completion tokens, "
                             f"whereas you provided {body['max_tokens']}.")
        h.stream("fine")
    server.reply = reply
    choose("groq", "llama-3.3-70b-versatile", server.url)
    assert ai.chat([{"role": "user", "content": "hi"}], think=True)[0] == "fine"
    assert server.seen[-1]["body"]["max_tokens"] == 8192


def test_anthropic_is_asked_its_way_and_its_stream_read(app, server):
    server.reply = lambda h, body: anthropic_stream(h, '{"name": "Clients"}', thinking="The owner keeps clients.")
    choose("anthropic", "claude-opus-5-5", server.url, key="sk-ant")
    shown = []
    content, thinking = ai.chat([{"role": "system", "content": "You design apps."}, {"role": "user", "content": "clients"}],
                                schema=SCHEMA, think=True, on_progress=lambda t, c: shown.append((t, c)))
    assert content == '{"name": "Clients"}' and thinking == "The owner keeps clients."
    assert shown[-1] == (thinking, content)
    sent = server.seen[0]
    assert sent["path"] == "/v1/messages"
    assert sent["headers"]["x-api-key"] == "sk-ant" and sent["headers"]["anthropic-version"] == providers.ANTHROPIC_VERSION
    body = sent["body"]
    assert body["system"] == [{"type": "text", "text": "You design apps.", "cache_control": {"type": "ephemeral"}}]
    assert body["messages"] == [{"role": "user", "content": "clients"}]
    assert body["thinking"] == {"type": "adaptive", "display": "summarized"} and body["output_config"]["effort"] == "high"
    assert body["output_config"]["format"] == {"type": "json_schema", "schema": {**SCHEMA, "additionalProperties": False}}
    assert "temperature" not in body and "top_p" not in body
    assert body["max_tokens"] == app.config["AI_MAX_TOKENS"] + app.config["AI_THINK_TOKENS"]
    calls = next(c for c in ai.stats()["calls"] if c["purpose"] == "assistant")
    assert (calls["provider"], calls["model"], calls["tokens_in"], calls["tokens_out"]) == ("anthropic", "claude-opus-5-5", 140, 12)


def test_a_claude_model_that_always_thinks_is_asked_the_least_it_does(app, server):
    def reply(h, body):
        kind = (body.get("thinking") or {}).get("type")
        if kind in ("disabled", "between_tools"):
            return refuse(h, f"thinking.type: '{kind}' is not supported for this model", anthropic=True)
        anthropic_stream(h, "hello")
    server.reply = reply
    choose("anthropic", "claude-opus-5-5", server.url)
    assert ai.chat([{"role": "user", "content": "hi"}], think=False)[0] == "hello"
    body = server.seen[-1]["body"]
    assert "thinking" not in body and body["output_config"] == {"effort": "low"}
    assert len(server.seen) == 3
    ai.chat([{"role": "user", "content": "again"}], think=False)
    assert len(server.seen) == 4


def test_a_schema_claude_cant_take_goes_in_the_prompt_and_its_stops_mean_what_they_say(app, server):
    server.reply = lambda h, body: anthropic_stream(h, '{"values": {}}')
    choose("anthropic", "claude-sonnet-5-5", server.url)
    open_map = {"type": "object", "properties": {"values": {"type": "object", "additionalProperties": {"type": "string"}}}}
    ai.chat([{"role": "user", "content": "hi"}], schema=open_map)
    body = server.seen[-1]["body"]
    assert "format" not in (body.get("output_config") or {}) and "JSON schema" in body["system"][0]["text"]
    server.reply = lambda h, body: anthropic_stream(h, '{"name": "Cli', stop="max_tokens")
    with pytest.raises(ai.CutOff):
        ai.chat([{"role": "user", "content": "long"}])
    server.reply = lambda h, body: anthropic_stream(h, "", stop="refusal")
    with pytest.raises(ai.AIError, match="declined"):
        ai.chat([{"role": "user", "content": "bad"}])


def test_pictures_go_the_way_each_provider_takes_them(app, server):
    png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
    server.reply = lambda h, body: h.stream("a receipt")
    choose("openai", "gpt-9", server.url)
    ai.chat([{"role": "user", "content": "read this", "images": [png]}])
    content = server.seen[-1]["body"]["messages"][0]["content"]
    assert content == [{"type": "text", "text": "read this"}, {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{png}"}}]
    server.reply = lambda h, body: anthropic_stream(h, "a receipt")
    choose("anthropic", "claude-opus-5-5", server.url)
    ai.chat([{"role": "user", "content": "read this", "images": [png]}])
    content = server.seen[-1]["body"]["messages"][0]["content"]
    assert content == [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": png}}, {"type": "text", "text": "read this"}]


# When it can't answer -----------------------------------------------------------------------------------------------

def test_no_key_a_key_turned_away_or_no_credit_waits_for_an_admin_to_put_it_right(app, server):
    providers.save({"use": {"assistant": {"provider": "openai", "model": "gpt-9"}}, "urls": {"openai": server.url}}, by="test")
    with pytest.raises(ai.AIUnavailable, match="no key for OpenAI"):
        ai.chat([{"role": "user", "content": "hi"}])
    assert not server.seen
    choose("openai", "gpt-9", server.url)
    server.reply = lambda h, body: refuse(h, "Incorrect API key provided", status=401)
    with pytest.raises(ai.AIUnavailable, match="turned the key away"):
        ai.chat([{"role": "user", "content": "hi"}])
    server.seen.clear()
    server.reply = lambda h, body: h.send_json(429, {"error": {"message": "You exceeded your current quota", "code": "insufficient_quota"}})
    with pytest.raises(ai.AIUnavailable, match="run out of credit"):
        ai.chat([{"role": "user", "content": "hi"}])
    assert len(server.seen) == 1  # not asked again and again: waiting won't bring credit


def test_a_provider_given_no_model_uses_its_best_and_the_try_out_says_what_answered(app, server):
    server.reply = lambda h, body: h.stream('{"greeting": "Hello, ready to help."}')
    original = server.server.RequestHandlerClass.do_GET

    def models(h):
        h.send_json(200, {"object": "list", "data": [{"id": i} for i in ("gpt-5", "gpt-5.1", "gpt-5.1-mini", "text-embedding-3-small")]})
    server.server.RequestHandlerClass.do_GET = models
    try:
        choose("openai", "", server.url)
        content, _ = ai.chat([{"role": "user", "content": "hi"}])
        assert server.seen[-1]["body"]["model"] == "gpt-5.1"
        result = ai.try_out(ai._route("assistant"))
        assert result["ok"] and result["said"] == "Hello, ready to help." and result["model"] == "gpt-5.1"
        assert providers.models("openai", kind="embed") == ["text-embedding-3-small"]
    finally:
        server.server.RequestHandlerClass.do_GET = original


def test_search_by_meaning_on_a_cloud_provider(app, server):
    def reply(h, body):
        h.send_json(200, {"data": [{"index": i, "embedding": [0.1 * (i + 1), 0.2]} for i in range(len(body["input"]))],
                          "usage": {"prompt_tokens": 9}})
    server.reply = reply
    choose("anthropic", "claude-opus-5-5", "https://unused.example", embed={"provider": "voyage", "model": "voyage-3.5"})
    providers.save({**providers.stored(), "keys": {**providers.stored()["keys"], "voyage": encrypt("v-key")},
                    "urls": {"voyage": server.url}}, by="test")
    assert ai.embed_model() == "voyage-3.5"
    assert ai.embed(["one", "two"]) == [[0.1, 0.2], [0.2, 0.2]]
    sent = server.seen[-1]
    assert sent["path"] == "/v1/embeddings" and sent["body"] == {"model": "voyage-3.5", "input": ["one", "two"]}
    providers.save({**providers.stored(), "use": {**providers.stored()["use"], "embed": {"provider": "none"}}}, by="test")
    assert ai.embed_model() is None  # by words only


def test_a_recorded_call_says_which_model_wrote_it(app, server):
    server.reply = lambda h, body: anthropic_stream(h, "hi")
    choose("anthropic", "claude-opus-5-5", server.url)
    calls = []
    token = ai.recording.set(calls)
    try:
        ai.chat([{"role": "user", "content": "hi"}])
    finally:
        ai.recording.reset(token)
    assert calls[0]["provider"] == "anthropic" and calls[0]["model"] == "claude-opus-5-5"
