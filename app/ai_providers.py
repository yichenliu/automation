"""Where the AI's calls can go, local or cloud, and which one each purpose uses.

The catalog: Ollama on this computer; your own OpenAI-compatible model server (vLLM, SGLang, LM Studio, llama.cpp, or a
gateway such as LiteLLM); and the cloud providers OpenAI, Anthropic (Claude), Google (Gemini), Microsoft Azure OpenAI,
Mistral, Groq, DeepSeek, xAI (Grok), OpenRouter, Together AI, Fireworks AI, Cerebras and Cohere, with Voyage AI for
search by meaning only. Three ways of asking cover all of them, and app/ai.py speaks each: Ollama's own API, OpenAI's
chat completions (which most providers speak too) and Anthropic's messages API.

What each purpose uses (the assistant and background work, the builder, quick jobs, search by meaning) comes from:
1. the platform's stored choice: Settings › AI, made by a platform admin, in effect in every web and worker process
   within a few seconds, no restart; the keys saved there are encrypted;
2. else the .env settings: AI_PROVIDER, AI_MODEL, AI_BASE_URL, AI_API_KEY (and _BUILDER, _FAST, _EMBED of each), and
   each provider's key under the name its own tools use (OPENAI_API_KEY, ANTHROPIC_API_KEY, GEMINI_API_KEY, ...).

A cloud provider given no model uses the best one its key can use (the newest of its leading family). Search by meaning
stays on this computer unless it's sent somewhere that can do it: Anthropic, for one, has no embedding model.
"""
import hashlib
import re
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import requests

from .core.settings import settings

ANTHROPIC_VERSION = "2023-06-01"


@dataclass(frozen=True)
class Provider:
    key: str
    name: str
    protocol: str  # how it's asked: "ollama", "openai" (chat completions) or "anthropic" (messages)
    url: str  # where its API lives, unless a platform admin (or AI_BASE_URL) says somewhere else
    cloud: bool = True  # whether what's asked leaves the platform's own machines
    keys: tuple = ()  # the settings its key may be in, besides AI_API_KEY and one saved under Settings › AI
    auth: str = "bearer"  # how the key is sent: bearer, api-key (Azure) or x-api-key (Anthropic)
    think: str = "effort"  # how thinking is switched: ollama, template (chat_template_kwargs), effort (reasoning_effort), reasoning (OpenRouter), anthropic, none
    tokens: str = "max_tokens"  # the name of the answer's length cap it takes first
    strict: bool = False  # the JSON schema sent as strict: the server constrains what's written to it as it decodes
    sampling: bool = True  # temperature, top_p and seed sent (one a model refuses is left off from then on)
    chats: bool = True
    embeds: bool = False
    needs_key: bool = True
    prefer: tuple = ()  # the models to use when none is chosen, best first: patterns over its list, the highest version wins
    site: str = ""  # where to get a key
    about: str = ""


CATALOG = {p.key: p for p in [
    Provider("ollama", "Ollama on this computer", "ollama", "http://127.0.0.1:11434", cloud=False, auth="", think="ollama",
             embeds=True, needs_key=False, site="https://ollama.com/download",
             about="Free and private: the model runs on this computer. Slower, and only as good as the model it can hold."),
    Provider("compatible", "Your own model server", "openai", "http://127.0.0.1:8000/v1", cloud=False, think="template",
             strict=True, embeds=True, needs_key=False,
             about="vLLM, SGLang, LM Studio, llama.cpp, or a gateway such as LiteLLM, at an address you give."),
    Provider("openai", "OpenAI", "openai", "https://api.openai.com/v1", keys=("OPENAI_API_KEY",), tokens="max_completion_tokens",
             embeds=True, prefer=(r"gpt-(\d+)(?:\.(\d+))?",), site="https://platform.openai.com/api-keys"),
    Provider("anthropic", "Anthropic (Claude)", "anthropic", "https://api.anthropic.com/v1", keys=("ANTHROPIC_API_KEY",),
             auth="x-api-key", think="anthropic", sampling=False,
             prefer=(r"claude-opus-(\d+)(?:-(\d))?", r"claude-sonnet-(\d+)(?:-(\d))?"), site="https://console.anthropic.com/settings/keys"),
    Provider("gemini", "Google (Gemini)", "openai", "https://generativelanguage.googleapis.com/v1beta/openai",
             keys=("GEMINI_API_KEY",), embeds=True,
             prefer=(r"gemini-(\d+)(?:\.(\d+))?-pro", r"gemini-(\d+)(?:\.(\d+))?-pro-preview(?:-[\w-]+)?", r"gemini-(\d+)(?:\.(\d+))?-flash"),
             site="https://aistudio.google.com/apikey"),
    Provider("azure", "Microsoft Azure OpenAI", "openai", "", keys=("AZURE_OPENAI_API_KEY",), auth="api-key",
             tokens="max_completion_tokens", embeds=True, site="https://portal.azure.com",
             about="Your company's own Azure OpenAI resource: its address (https://NAME.openai.azure.com), and a deployment's name as the model."),
    Provider("mistral", "Mistral", "openai", "https://api.mistral.ai/v1", keys=("MISTRAL_API_KEY",), think="none", embeds=True,
             prefer=(r"mistral-large-latest", r"mistral-medium-latest"), site="https://console.mistral.ai/api-keys"),
    Provider("groq", "Groq", "openai", "https://api.groq.com/openai/v1", keys=("GROQ_API_KEY",),
             prefer=(r"openai/gpt-oss-(\d+)b", r"llama-(\d+)\.(\d+)-70b-versatile"), site="https://console.groq.com/keys"),
    Provider("deepseek", "DeepSeek", "openai", "https://api.deepseek.com/v1", keys=("DEEPSEEK_API_KEY",), think="none",
             prefer=(r"deepseek-chat",), site="https://platform.deepseek.com/api_keys"),
    Provider("xai", "xAI (Grok)", "openai", "https://api.x.ai/v1", keys=("XAI_API_KEY",), prefer=(r"grok-(\d+)(?:\.(\d+))?",),
             site="https://console.x.ai"),
    Provider("openrouter", "OpenRouter (many models, one key)", "openai", "https://openrouter.ai/api/v1", keys=("OPENROUTER_API_KEY",),
             think="reasoning", site="https://openrouter.ai/keys"),
    Provider("together", "Together AI", "openai", "https://api.together.xyz/v1", keys=("TOGETHER_API_KEY",), embeds=True,
             site="https://api.together.ai/settings/api-keys"),
    Provider("fireworks", "Fireworks AI", "openai", "https://api.fireworks.ai/inference/v1", keys=("FIREWORKS_API_KEY",), embeds=True,
             site="https://fireworks.ai/account/api-keys"),
    Provider("cerebras", "Cerebras", "openai", "https://api.cerebras.ai/v1", keys=("CEREBRAS_API_KEY",), site="https://cloud.cerebras.ai"),
    Provider("cohere", "Cohere", "openai", "https://api.cohere.ai/compatibility/v1", keys=("COHERE_API_KEY",), think="none",
             embeds=True, site="https://dashboard.cohere.com/api-keys"),
    Provider("voyage", "Voyage AI", "openai", "https://api.voyageai.com/v1", keys=("VOYAGE_API_KEY",), chats=False, embeds=True,
             prefer=(r"voyage-(\d+)(?:\.(\d+))?",), site="https://dash.voyageai.com/api-keys",
             about="Search by meaning only (the embeddings Anthropic suggests)."),
]}
ALIASES = {"vllm": "compatible", "sglang": "compatible", "lmstudio": "compatible", "lm-studio": "compatible", "llamacpp": "compatible",
           "llama.cpp": "compatible", "litellm": "compatible", "local": "compatible", "openai-compatible": "compatible",
           "claude": "anthropic", "google": "gemini", "azure-openai": "azure", "azure_openai": "azure", "grok": "xai", "x.ai": "xai"}
SLOTS = ("assistant", "builder", "fast", "embed")  # what can be sent somewhere of its own (background work goes with the assistant)
TAGS = {"builder": "_BUILDER", "fast": "_FAST", "embed": "_EMBED"}
WORDS_ONLY = "none"  # search by meaning chosen off: found by words only


def canonical(raw, url=""):
    """A provider's name as the catalog has it. "openai" at an address of your own is your own OpenAI-compatible server
    (what AI_PROVIDER=openai always meant with an AI_BASE_URL), not OpenAI."""
    name = (raw or "").strip().lower()
    name = ALIASES.get(name, name)
    if name == "openai" and url and urlparse(url if "://" in url else "http://" + url).hostname not in (None, "api.openai.com"):
        return "compatible"
    return name


def address(provider, url):
    """Where a provider's API is, given as its people write it: the version path its calls go under added when it's
    left off (https://gpu1:8000 → https://gpu1:8000/v1; https://NAME.openai.azure.com → …/openai/v1)."""
    url = (url or "").strip().rstrip("/")
    if not url:
        return url
    if "://" not in url:
        url = "https://" + url
    if provider.key == "azure" and "/openai" not in url:
        return url + "/openai/v1"
    if provider.key in ("compatible", "openai", "anthropic") and not re.search(r"/v\d+(beta)?$", url):
        return url + "/v1"
    return url


def default_url(provider, cfg):
    if provider.key == "ollama":
        return cfg.get("OLLAMA_URL") or provider.url
    if provider.key == "azure":
        return address(provider, cfg.get("AZURE_OPENAI_ENDPOINT") or "")
    return provider.url


# The platform's stored choice ------------------------------------------------------------------------------------

_cache = {}  # database -> (read at, value)
_cache_lock = threading.Lock()
FRESH_FOR = 5.0  # seconds a process trusts what it read: another process's change is in effect this soon


def stored():
    """The platform's stored choice (Settings › AI): {"use": {slot: {"provider", "model"}}, "keys": {provider:
    encrypted}, "urls": {provider: address}}, or {} when none is made (or there's no database here)."""
    from .extensions import db
    try:
        engine = db.engine
    except RuntimeError:
        return {}
    where = str(engine.url)
    with _cache_lock:
        at, value = _cache.get(where, (0.0, None))
    if value is not None and time.monotonic() - at < FRESH_FOR:
        return value
    try:  # its own connection: never the caller's transaction, held open while the AI answers
        from sqlalchemy import text
        with engine.connect() as conn:
            row = conn.execute(text("SELECT value FROM platform_setting WHERE key = 'ai'")).first()
        value = (row[0] if row else None) or {}
    except Exception:  # noqa: BLE001 - no table yet (a database being made), or none reachable: the .env settings
        value = {}
    with _cache_lock:
        _cache[where] = (time.monotonic(), value)
    return value


def forget():
    with _cache_lock:
        _cache.clear()


def save(value, by=""):
    """Keep the platform's choice (what `stored` returns), in effect at once in this process and within FRESH_FOR
    seconds in the others."""
    from .extensions import db
    from .models import PlatformSetting
    row = db.session.get(PlatformSetting, "ai")
    if row is None:
        row = PlatformSetting(key="ai", value={})
        db.session.add(row)
    row.value, row.updated_by = value, by
    db.session.commit()
    forget()


def saved_key(name, value=None):
    """The key saved for a provider under Settings › AI ("" if none)."""
    from .security import decrypt
    token = ((value if value is not None else stored()).get("keys") or {}).get(name)
    said = decrypt(token) if token else ""
    return said if isinstance(said, str) else ""


# Which one each purpose uses -------------------------------------------------------------------------------------

def slot_of(purpose):
    return purpose if purpose in ("builder", "fast", "embed") else "assistant"


def _env(cfg, slot):
    """What the .env settings say for a slot: (provider, address, key, model). A purpose's own setting first; the plain
    one where it isn't set, and only the plain address, key and model where it's the plain provider too."""
    tag = TAGS.get(slot, "")
    raw_plain = (cfg.get("AI_PROVIDER") or "ollama").strip().lower()
    raw = ((cfg.get("AI_PROVIDER" + tag) if tag else "") or raw_plain).strip().lower()
    same = raw == raw_plain
    url = (cfg.get("AI_BASE_URL" + tag) if tag else "") or (cfg.get("AI_BASE_URL") if same else "") or ""
    key = (cfg.get("AI_API_KEY" + tag) if tag else "") or (cfg.get("AI_API_KEY") if same else "") or ""
    if slot == "embed":
        model = cfg.get("OLLAMA_EMBED_MODEL") or ""
        plain = CATALOG.get(canonical(raw_plain, cfg.get("AI_BASE_URL") or ""))
        if not cfg.get("AI_PROVIDER_EMBED") and plain is not None and plain.cloud:
            # a cloud provider for the chat doesn't take search by meaning with it unasked (its model here is Ollama's)
            return "ollama", cfg.get("OLLAMA_URL") or CATALOG["ollama"].url, "", model
    else:
        model = (cfg.get("AI_MODEL" + tag) if tag else "") or (cfg.get("AI_MODEL") if same else "") or ""
    return canonical(raw, url), url, key, model


def resolve(purpose, saved=None):
    """(provider, address, key, model) for a purpose's calls. The model may be "" for a cloud provider (its best is
    found when it's asked: `best_model`), and for search by meaning "" means by words only. `saved`: a choice to resolve
    instead of the stored one (Settings › AI trying what's on its form before it's saved)."""
    cfg = settings()
    slot = slot_of(purpose)
    saved = stored() if saved is None else saved
    use = saved.get("use") or {}
    picked = use.get(slot) or (use.get("assistant") if slot != "embed" else None)
    env_name, env_url, env_key, env_model = _env(cfg, slot)
    if picked:
        name, model, url, key = canonical(picked.get("provider")), picked.get("model") or "", "", ""
        if name == WORDS_ONLY:
            return WORDS_ONLY, "", "", ""
    else:
        name, model, url, key = env_name, env_model, env_url, env_key
    provider = CATALOG.get(name)
    if provider is None:
        return name, url, key, model
    if slot == "embed" and not provider.embeds:  # chats with a provider that has no embedding model: search stays here
        provider, model, url, key = CATALOG["ollama"], cfg.get("OLLAMA_EMBED_MODEL") or "", "", ""
        name = "ollama"
    url = (saved.get("urls") or {}).get(name) or url or (env_url if name == env_name else "") or default_url(provider, cfg)
    url = address(provider, url) if provider.protocol != "ollama" else url.rstrip("/")
    key = saved_key(name, saved) or key or (env_key if name == env_name else "") or next((cfg.get(k) for k in provider.keys if cfg.get(k)), "")
    if not model and provider.key in ("ollama", "compatible") and slot != "embed":
        model = cfg.get("OLLAMA_MODEL") or ""
    elif not model and provider.key == "ollama" and picked:  # search by meaning chosen on this computer: its usual model
        model = cfg.get("OLLAMA_EMBED_MODEL") or "bge-m3"
    return name, url, key, model


def headers(provider, key):
    if provider is None or not key:
        out = {}
    elif provider.auth == "api-key":
        out = {"api-key": key}
    elif provider.auth == "x-api-key":
        out = {"x-api-key": key}
    else:
        out = {"Authorization": f"Bearer {key}"}
    if provider is not None and provider.protocol == "anthropic":
        out["anthropic-version"] = ANTHROPIC_VERSION
    if provider is not None and provider.key == "openrouter":
        out["X-Title"] = settings().get("PLATFORM_NAME") or "Keel"
    return out


# The models a provider has ---------------------------------------------------------------------------------------

class KeyRefused(Exception):
    """The provider turned the key away (HTTP 401 or 403)."""


_listed = {}  # (provider, address, key's hash) -> (listed at, ids)
LISTED_FOR = 600.0
NOT_CHAT = re.compile(r"embed|tts|whisper|dall-e|davinci|babbage|moderation|audio|realtime|transcri|image|sora|rerank|guard|"
                      r"computer-use|search|codex|ocr|speech", re.I)


def models(name, url=None, key=None, *, kind="chat", fresh=False):
    """The models a provider has for this key, newest first where it says ([] if it can't list them): for chatting, or
    with kind="embed" for search by meaning. Raises KeyRefused when the key is turned away, ConnectionError (or
    requests' own) when it can't be reached."""
    provider = CATALOG[name]
    if url is None or key is None:
        _, resolved_url, resolved_key, _ = _resolve_for(name)
        url = resolved_url if url is None else url
        key = resolved_key if key is None else key
    url = address(provider, url) if provider.protocol != "ollama" else url.rstrip("/")
    remembered = (name, url, hashlib.sha256((key or "").encode()).hexdigest()[:16])
    at, ids = _listed.get(remembered, (0.0, None))
    if ids is None or fresh or time.monotonic() - at > LISTED_FOR:
        ids = _list(provider, url, key)
        _listed[remembered] = (time.monotonic(), ids)
    if kind == "embed":
        return [i for i in ids if "embed" in i.lower() or provider.key == "voyage"]
    return [i for i in ids if not NOT_CHAT.search(i)] if provider.chats else []


def _resolve_for(name):
    """(provider, address, key, model) as `resolve` would give them for this provider, whichever purpose uses it."""
    cfg, provider = settings(), CATALOG[name]
    saved = stored()
    for slot in SLOTS:
        got = resolve(slot)
        if got[0] == name:
            return got
    url = (saved.get("urls") or {}).get(name) or default_url(provider, cfg)
    key = saved_key(name, saved) or next((cfg.get(k) for k in provider.keys if cfg.get(k)), "")
    return name, address(provider, url) if provider.protocol != "ollama" else url, key, ""


def _list(provider, url, key):
    if provider.protocol == "ollama":
        response = requests.get(url + "/api/tags", timeout=(5, 15))
    elif provider.protocol == "anthropic":
        response = requests.get(url + "/models", params={"limit": 1000}, headers=headers(provider, key), timeout=(5, 20))
    else:
        response = requests.get(url + "/models", headers=headers(provider, key), timeout=(5, 20))
    if response.status_code in (401, 403):
        raise KeyRefused(f"{provider.name} turned the key away (HTTP {response.status_code})")
    if response.status_code >= 500 or response.status_code == 429:
        raise requests.HTTPError(f"HTTP {response.status_code}", response=response)
    if response.status_code >= 400:  # it can't say what it has (not every server lists them): it's there, though
        return []
    said = response.json()
    if provider.protocol == "ollama":
        return [m.get("name") for m in said.get("models") or [] if m.get("name")]
    items = said.get("data") if isinstance(said, dict) else said
    ids = [str(m.get("id") or m.get("name") or "") for m in (items or []) if isinstance(m, dict)]
    return [re.sub(r"^models/", "", i) for i in ids if i]


def _plain(name):
    """A model's name with what people type differently set aside: case, spaces, dots, underscores, slashes."""
    return re.sub(r"[\s._/-]+", "-", str(name or "").strip().lower()).strip("-")


def own_name(typed, ids):
    """The provider's own name for a model typed another way ("claude-sonnet-5.5" or "Claude Sonnet 5.5" for
    claude-sonnet-5-5, "GPT 5.2" for gpt-5.2): the one model it has that's the same once case, spaces and punctuation are
    set aside; "" when none is (or more than one)."""
    if typed in ids:
        return typed
    found = {i for i in ids if _plain(i) == _plain(typed)}
    return found.pop() if len(found) == 1 else ""


def near_names(typed, ids, n=3):
    """The provider's models nearest what was typed, for "did you mean"."""
    import difflib
    plain = {}
    for i in ids:
        plain.setdefault(_plain(i), i)
    return [plain[x] for x in difflib.get_close_matches(_plain(typed), list(plain), n=n, cutoff=0.6)]


def best_model(name, ids):
    """The model to use when none is chosen: the first of the provider's preferred families it has, the highest version
    of that family (gpt-5.2 over gpt-5.1 over gpt-5; never a -mini or a dated snapshot)."""
    provider = CATALOG.get(name)
    for pattern in (provider.prefer if provider else ()):
        found = []
        for i in ids:
            m = re.fullmatch(pattern, i)
            if m:
                found.append((tuple(int(g) if g and g.isdigit() else 0 for g in m.groups()), i))
        if found:
            return max(found)[1]
    return ""
