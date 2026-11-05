"""Passwords, kept as salted hashes in the form they have always been stored in (`scrypt:32768:8:1$<salt>$<hash>`, or
`pbkdf2:sha256:<rounds>$<salt>$<hash>` for older ones), so every existing password still works."""
import hashlib
import hmac
import secrets
import string

_SALT_CHARS = string.ascii_letters + string.digits
PBKDF2_ROUNDS = 1_000_000


def _hashed(method, salt, password):
    """(the hash in hex, the method as written in front of it)."""
    name, *args = method.split(":")
    if name == "scrypt":
        n, r, p = map(int, args) if args else (2 ** 15, 8, 1)
        return (hashlib.scrypt(password.encode(), salt=salt.encode(), n=n, r=r, p=p, maxmem=132 * n * r * p).hex(),
                f"scrypt:{n}:{r}:{p}")
    if name == "pbkdf2":
        digest = args[0] if args else "sha256"
        rounds = int(args[1]) if len(args) > 1 else PBKDF2_ROUNDS
        return hashlib.pbkdf2_hmac(digest, password.encode(), salt.encode(), rounds).hex(), f"pbkdf2:{digest}:{rounds}"
    raise ValueError(f"Invalid hash method '{name}'.")


def hash_password(password, method="scrypt", salt_length=16):
    salt = "".join(secrets.choice(_SALT_CHARS) for _ in range(salt_length))
    value, written = _hashed(method, salt, password)
    return f"{written}${salt}${value}"


def check_password(stored, password):
    """Whether the password is the one this hash was made from (False for anything that isn't a hash, like "!")."""
    try:
        method, salt, value = stored.split("$", 2)
    except ValueError:
        return False
    return hmac.compare_digest(_hashed(method, salt, password)[0], value)
