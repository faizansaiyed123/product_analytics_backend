"""Password hashing, opaque tokens, API-key hashing, CSRF tokens."""

from __future__ import annotations

import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.config import get_settings

_hasher = PasswordHasher()  # Argon2id by default

API_KEY_PREFIX = "phc_"


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


# Burn comparable time when the user does not exist (mitigates user enumeration by timing).
DUMMY_PASSWORD_HASH = _hasher.hash("dummy-password-for-timing")


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def hash_api_key(key: str) -> str:
    """HMAC-SHA256 with a server secret so a DB leak alone cannot verify guesses."""
    secret = get_settings().api_key_secret.encode()
    return hmac.new(secret, key.encode(), hashlib.sha256).hexdigest()


def generate_api_key() -> tuple[str, str, str]:
    """Return (raw_key, prefix, key_hash). The raw key is shown once and never stored."""
    raw = API_KEY_PREFIX + secrets.token_urlsafe(30)
    return raw, raw[:10], hash_api_key(raw)


def csrf_token_for_session(session_id: str) -> str:
    secret = get_settings().jwt_secret.encode()
    return hmac.new(secret, f"csrf:{session_id}".encode(), hashlib.sha256).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())
