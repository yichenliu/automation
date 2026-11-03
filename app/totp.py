"""Two-step sign-in codes (TOTP, RFC 6238): the six-digit codes authenticator apps (Microsoft Authenticator, Google
Authenticator, 1Password) show, changing every 30 seconds."""
import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote


def new_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def code(secret, at=None):
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    counter = int((at if at is not None else time.time()) // 30)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    return f"{(struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}"


def verify(secret, given, at=None):
    """Whether a code is right, allowing for a clock that's half a minute out either way."""
    given = "".join(ch for ch in str(given or "") if ch.isdigit())
    now = at if at is not None else time.time()
    return len(given) == 6 and any(hmac.compare_digest(code(secret, now + 30 * step), given) for step in (-1, 0, 1))


def setup_uri(secret, account, issuer):
    """What an authenticator app scans (or is given by hand): otpauth://totp/..."""
    return f"otpauth://totp/{quote(issuer)}:{quote(account)}?secret={secret}&issuer={quote(issuer)}&digits=6&period=30"
