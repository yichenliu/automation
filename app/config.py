import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _secret_key(data_dir):
    """A random key made on first start and kept in the data folder, so sign-ins (and the encryption of stored
    Microsoft 365 tokens) survive restarts. Back it up with the data folder."""
    if os.getenv("SECRET_KEY"):
        return os.getenv("SECRET_KEY")
    path = Path(data_dir) / "secret_key"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(secrets.token_hex(32))
        path.chmod(0o600)
    return path.read_text().strip()


class Config:
    DATA_DIR = Path(os.getenv("DATA_DIR") or ROOT / "data")
    # PostgreSQL only (with pgvector): made and started by ./start.sh (app/database.py), its address kept in .env
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL")
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True, "pool_size": 10, "max_overflow": 20, "pool_recycle": 1800}
    PLATFORM_NAME = os.getenv("PLATFORM_NAME") or "Keel"
    PORT = int(os.getenv("PORT") or 5050)
    BASE_URL = (os.getenv("BASE_URL") or f"http://localhost:{os.getenv('PORT') or 5050}").rstrip("/")
    # The AI (app/ai.py) runs on model servers the platform runs itself; nothing reaches a cloud model unless its address
    # is put here. AI_PROVIDER=ollama: Ollama on this computer (development, one business on one machine).
    # AI_PROVIDER=openai: any OpenAI-compatible server (vLLM, SGLang, llama.cpp's server, LM Studio) at AI_BASE_URL, with
    # AI_API_KEY if it asks for one: the GPU servers production serves many customers from.
    AI_PROVIDER = (os.getenv("AI_PROVIDER") or "ollama").strip().lower()
    OLLAMA_URL = (os.getenv("OLLAMA_URL") or "http://127.0.0.1:11434").rstrip("/")
    AI_BASE_URL = (os.getenv("AI_BASE_URL") or (OLLAMA_URL if AI_PROVIDER == "ollama" else "http://127.0.0.1:8000/v1")).rstrip("/")
    AI_API_KEY = os.getenv("AI_API_KEY") or ""
    AI_MODEL = os.getenv("AI_MODEL") or os.getenv("OLLAMA_MODEL") or "qwen3.8:27b-dflash2-nvfp4"
    OLLAMA_MODEL = AI_MODEL  # the older name, still read by some
    # One deployment can send each purpose to its own model, and its own server: the builder to a big one, quick jobs to
    # a small one. Each falls back to the plain setting above when it isn't set.
    AI_MODEL_BUILDER = os.getenv("AI_MODEL_BUILDER") or ""
    AI_MODEL_FAST = os.getenv("AI_MODEL_FAST") or ""
    AI_PROVIDER_BUILDER = (os.getenv("AI_PROVIDER_BUILDER") or "").strip().lower()
    AI_PROVIDER_FAST = (os.getenv("AI_PROVIDER_FAST") or "").strip().lower()
    AI_BASE_URL_BUILDER = (os.getenv("AI_BASE_URL_BUILDER") or "").rstrip("/")
    AI_BASE_URL_FAST = (os.getenv("AI_BASE_URL_FAST") or "").rstrip("/")
    AI_API_KEY_BUILDER = os.getenv("AI_API_KEY_BUILDER") or ""
    AI_API_KEY_FAST = os.getenv("AI_API_KEY_FAST") or ""
    # The embedding model finds documents and records by meaning ("" for words only). vLLM serves one model per server,
    # so an embedding model there has a server of its own (AI_BASE_URL_EMBED); Ollama serves both from one.
    OLLAMA_EMBED_MODEL = os.getenv("AI_MODEL_EMBED") or os.getenv("OLLAMA_EMBED_MODEL", "bge-m3")
    AI_PROVIDER_EMBED = (os.getenv("AI_PROVIDER_EMBED") or "").strip().lower()
    AI_BASE_URL_EMBED = (os.getenv("AI_BASE_URL_EMBED") or "").rstrip("/")
    AI_API_KEY_EMBED = os.getenv("AI_API_KEY_EMBED") or ""
    # The gateway in front of each model server (one queue per process; see app/ai.py). AI_CONCURRENCY: calls at once to
    # one server, 0 for the provider's own default (2 for Ollama, which answers one or two at a time well; 8 for vLLM or
    # SGLang, which batch them). AI_DEADLINE: seconds a whole call may take, its wait for a turn included.
    # AI_READ_TIMEOUT: seconds with nothing from the server before it's taken as gone. AI_RETRIES and AI_BACKOFF: how many
    # times a call is asked again when the server can't be reached or is busy, and the first wait (doubling each time).
    AI_CONCURRENCY = int(os.getenv("AI_CONCURRENCY") or 0)
    AI_DEADLINE = float(os.getenv("AI_DEADLINE") or 900)
    AI_READ_TIMEOUT = float(os.getenv("AI_READ_TIMEOUT") or 600)
    AI_RETRIES = int(os.getenv("AI_RETRIES") or 3)
    AI_BACKOFF = float(os.getenv("AI_BACKOFF") or 1.0)
    # vLLM started with --scheduling-policy priority (or SGLang with priority scheduling) can order requests from every
    # process by how soon they're needed: true sends each call's priority along. Off by default: a vLLM server
    # without that policy refuses requests that carry one.
    AI_SERVER_PRIORITY = (os.getenv("AI_SERVER_PRIORITY") or "false").lower() == "true"
    AI_THINK = (os.getenv("AI_THINK") or "false").lower() == "true"
    # The builder's own reasoning-heavy work (designing, fixing what the try-out found, writing worked examples) thinks
    # first when this is on, whatever AI_THINK says for the quick jobs; AI_THINK_TOKENS is the room for that thinking.
    TENANCY_RLS = (os.getenv("TENANCY_RLS") or "true").lower() != "false"  # PostgreSQL row-level security behind the workspace filters
    FILES_BACKEND = (os.getenv("FILES_BACKEND") or "local").lower()  # local (the data folder) or s3 (S3-compatible object storage)
    S3_BUCKET = os.getenv("S3_BUCKET") or ""
    S3_PREFIX = os.getenv("S3_PREFIX") or ""
    S3_ENDPOINT = os.getenv("S3_ENDPOINT") or ""  # MinIO, R2 and the like; empty for AWS
    S3_REGION = os.getenv("S3_REGION") or ""
    S3_ACCESS_KEY = os.getenv("S3_ACCESS_KEY") or ""
    S3_SECRET_KEY = os.getenv("S3_SECRET_KEY") or ""
    AI_THINK_BUILDER = (os.getenv("AI_THINK_BUILDER") or os.getenv("AI_THINK") or "false").lower() == "true"
    # Which of the builder's steps think first: "all", or some of understand (the owner's words into needs), data,
    # behaviour, checks (worked examples), fix, repair, owner_check (an owner's check made an example). Left empty, all
    # of them when AI_THINK_BUILDER is on, none when it's off.
    AI_THINK_STEPS = os.getenv("AI_THINK_STEPS") or ""
    AI_THINK_TOKENS = int(os.getenv("AI_THINK_TOKENS") or 8000)
    AI_NUM_CTX = int(os.getenv("AI_NUM_CTX") or 0)  # 0: use the context size Ollama already has loaded
    AI_MAX_TOKENS = int(os.getenv("AI_MAX_TOKENS") or 12000)  # a cap, so a model stuck in a loop can't run forever
    AI_TEMPERATURE = float(os.getenv("AI_TEMPERATURE") or 0.6)  # lower than most models' 1.0: designs and records need care, not flair
    # The builder's own work is steadier: less chance, and a fixed seed, so the same request gets the same design
    # (AI_BUILDER_SEED=none for a different one each time)
    AI_BUILDER_TEMPERATURE = float(os.getenv("AI_BUILDER_TEMPERATURE") or 0.3)
    AI_BUILDER_SEED = None if (os.getenv("AI_BUILDER_SEED") or "").strip().lower() == "none" else int(os.getenv("AI_BUILDER_SEED") or 7)
    DEFAULT_TIMEZONE = os.getenv("DEFAULT_TIMEZONE") or "Australia/Perth"
    CURRENCY_SYMBOL = os.getenv("CURRENCY_SYMBOL") or "$"
    MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB") or 25)
    MAX_CONTENT_LENGTH = (int(os.getenv("MAX_UPLOAD_MB") or 25) + 5) * 1024 * 1024
    OIDC_ISSUER = os.getenv("OIDC_ISSUER") or ""  # single sign-on: https://accounts.google.com, an Okta or Auth0 domain…
    OIDC_CLIENT_ID = os.getenv("OIDC_CLIENT_ID") or ""
    OIDC_CLIENT_SECRET = os.getenv("OIDC_CLIENT_SECRET") or ""
    OIDC_NAME = os.getenv("OIDC_NAME") or ""  # what the button says ("Google", "Okta"); worked out from the issuer otherwise
    XERO_CLIENT_ID = os.getenv("XERO_CLIENT_ID") or ""  # the platform's Xero app (developer.xero.com): connecting Xero
    XERO_CLIENT_SECRET = os.getenv("XERO_CLIENT_SECRET") or ""
    QBO_CLIENT_ID = os.getenv("QBO_CLIENT_ID") or ""  # the platform's QuickBooks app (developer.intuit.com)
    QBO_CLIENT_SECRET = os.getenv("QBO_CLIENT_SECRET") or ""
    QBO_SANDBOX = (os.getenv("QBO_SANDBOX") or "").lower() in ("1", "true", "yes")
    MICROSOFT_CLIENT_ID = os.getenv("MICROSOFT_CLIENT_ID") or ""
    MICROSOFT_CLIENT_SECRET = os.getenv("MICROSOFT_CLIENT_SECRET") or ""
    MICROSOFT_TENANT = os.getenv("MICROSOFT_TENANT") or "organizations"
    BACKUP_KEEP = int(os.getenv("BACKUP_KEEP") or 14)  # daily backups kept in data/backups
    # The background work (AI turns, jobs, rules, mail, schedules) as threads of the web process: on (1) for one process
    # while developing; ./start.sh runs the web with WORKER=0 and the worker as its own process (python -m app.worker)
    WORKER = (os.getenv("WORKER") or "1") == "1"
    # which kinds of work a worker process does (any of builder, assistant, jobs, automations, outbox, knowledge,
    # scheduler; all of them by default), and how many builder turns and jobs it works on at once
    WORKER_KINDS = [k.strip() for k in (os.getenv("WORKER_KINDS") or "builder,assistant,jobs,automations,outbox,knowledge,scheduler")
                    .split(",") if k.strip()]
    BUILDER_CONCURRENCY = int(os.getenv("BUILDER_CONCURRENCY") or 2)
    JOB_CONCURRENCY = int(os.getenv("JOB_CONCURRENCY") or 2)
    TRUST_PROXY = (os.getenv("TRUST_PROXY") or "0") == "1"  # only when a proxy in front sets X-Forwarded-For
    WTF_CSRF_TIME_LIMIT = None
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SECURE = BASE_URL.startswith("https://")
    REMEMBER_COOKIE_SECURE = BASE_URL.startswith("https://")
    REMEMBER_COOKIE_HTTPONLY = True
