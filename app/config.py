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
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL") or f"sqlite:///{DATA_DIR / 'erp.db'}"
    PLATFORM_NAME = os.getenv("PLATFORM_NAME") or "Keel"
    PORT = int(os.getenv("PORT") or 5050)
    BASE_URL = (os.getenv("BASE_URL") or f"http://localhost:{os.getenv('PORT') or 5050}").rstrip("/")
    OLLAMA_URL = (os.getenv("OLLAMA_URL") or "http://127.0.0.1:11434").rstrip("/")
    OLLAMA_MODEL = os.getenv("OLLAMA_MODEL") or "qwen3.8:27b-dflash2-nvfp4"
    OLLAMA_EMBED_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "bge-m3")  # finds documents and records by meaning; "" for words only
    AI_THINK = (os.getenv("AI_THINK") or "false").lower() == "true"
    # The builder's own reasoning-heavy work (designing, fixing what the try-out found, writing worked examples) thinks
    # first when this is on, whatever AI_THINK says for the quick jobs; AI_THINK_TOKENS is the room for that thinking.
    AI_THINK_BUILDER = (os.getenv("AI_THINK_BUILDER") or os.getenv("AI_THINK") or "false").lower() == "true"
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
    MICROSOFT_CLIENT_ID = os.getenv("MICROSOFT_CLIENT_ID") or ""
    MICROSOFT_CLIENT_SECRET = os.getenv("MICROSOFT_CLIENT_SECRET") or ""
    MICROSOFT_TENANT = os.getenv("MICROSOFT_TENANT") or "organizations"
    BACKUP_KEEP = int(os.getenv("BACKUP_KEEP") or 14)  # daily backups kept in data/backups
    WORKER = (os.getenv("WORKER") or "1") == "1"  # the background threads for AI, automations, schedules and mail
    TRUST_PROXY = (os.getenv("TRUST_PROXY") or "0") == "1"  # only when a proxy in front sets X-Forwarded-For
    WTF_CSRF_TIME_LIMIT = None
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SECURE = BASE_URL.startswith("https://")
    REMEMBER_COOKIE_SECURE = BASE_URL.startswith("https://")
    REMEMBER_COOKIE_HTTPONLY = True
