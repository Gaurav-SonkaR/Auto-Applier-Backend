"""Single-user authentication for a local-only tool.

Design constraints, and why:

* **Credentials live in `.env`, never in SQLite.** SEC-001 requires "no password
  fields in database schema", so `AUTH_USERNAME` / `AUTH_PASSWORD_HASH` are
  settings, not a table. It also means a stolen `autoapply.db` carries no
  credential material at all.

* **There is deliberately no signup endpoint.** The only way to create or change
  credentials is running `scripts/set_password.py` on the machine hosting the
  app. Nobody can provision an account over the network, which is exactly the
  "no account without my verification" requirement — it isn't a permission
  check that could be bypassed, the code path simply does not exist.

* **Stdlib only.** `hashlib.scrypt` for hashing, `hmac` for token signing. No
  new dependency (keeps the ARM/Oracle-Cloud image lean, NFR-004), and a
  bespoke HMAC-only token format sidesteps JWT algorithm-confusion attacks
  entirely — there is no `alg` field here to lie about.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from typing import Optional

logger = logging.getLogger("auth")

# scrypt cost parameters. n=2**14 lands around 50-100ms per hash on typical
# hardware — slow enough to make offline brute force expensive, fast enough
# that a login doesn't feel laggy.
_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SALT_BYTES = 16

_HASH_PREFIX = "scrypt"


# ── Password hashing ─────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    """Hash a password for storage in AUTH_PASSWORD_HASH.

    Returns `scrypt$n$r$p$<salt-b64>$<hash-b64>` — self-describing, so the
    cost parameters can be raised later without invalidating existing hashes.
    """
    if not password:
        raise ValueError("password must not be empty")
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_DKLEN,
    )
    return "$".join([
        _HASH_PREFIX, str(_SCRYPT_N), str(_SCRYPT_R), str(_SCRYPT_P),
        base64.b64encode(salt).decode(), base64.b64encode(digest).decode(),
    ])


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check of `password` against a stored hash.

    Returns False rather than raising on a malformed/blank stored hash — an
    unconfigured deployment must fail closed, not crash into a 500 that leaks
    which half of the credential pair was wrong.
    """
    if not password or not stored:
        return False
    try:
        prefix, n_s, r_s, p_s, salt_b64, hash_b64 = stored.split("$")
        if prefix != _HASH_PREFIX:
            return False
        expected = base64.b64decode(hash_b64)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=base64.b64decode(salt_b64),
            n=int(n_s), r=int(r_s), p=int(p_s), dklen=len(expected),
        )
    except (ValueError, TypeError, KeyError) as exc:
        logger.warning("AUTH_PASSWORD_HASH is malformed (%s) — refusing login", exc)
        return False
    return hmac.compare_digest(expected, actual)


# ── Signed tokens ────────────────────────────────────────────────────────────

def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _sign(payload_b64: str, secret: str) -> str:
    mac = hmac.new(secret.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256)
    return _b64url_encode(mac.digest())


def create_token(username: str, secret: str, ttl_hours: int) -> str:
    """Issue `<payload-b64url>.<hmac-b64url>` carrying the user and expiry."""
    if not secret:
        raise ValueError("AUTH_SECRET_KEY must be set to issue tokens")
    payload = {"sub": username, "exp": int(time.time()) + ttl_hours * 3600}
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    return f"{payload_b64}.{_sign(payload_b64, secret)}"


def decode_token(token: str, secret: str) -> Optional[str]:
    """Return the username from a valid, unexpired token — else None.

    Every failure mode (malformed, tampered, expired) returns None rather than
    distinguishing itself, so a caller can't use the error to probe the token
    format or the secret.
    """
    if not token or not secret:
        return None
    try:
        payload_b64, signature = token.split(".", 1)
    except ValueError:
        return None

    # Compare first, parse second — never trust the payload of an unsigned token.
    if not hmac.compare_digest(_sign(payload_b64, secret), signature):
        return None

    try:
        payload = json.loads(_b64url_decode(payload_b64))
        username = payload["sub"]
        expires_at = int(payload["exp"])
    except (ValueError, KeyError, TypeError):
        return None

    if time.time() >= expires_at:
        return None
    return username


def token_expiry(token: str) -> Optional[int]:
    """Unix expiry of a token, without verifying it. For display only."""
    try:
        payload = json.loads(_b64url_decode(token.split(".", 1)[0]))
        return int(payload["exp"])
    except (ValueError, KeyError, TypeError, IndexError):
        return None
