"""Passwords, API keys, session tokens and credential encryption."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

API_KEY_PREFIX = "urm_"
SESSION_COOKIE = "urumi_session"

# --- Passwords ----------------------------------------------------------------
# People choose passwords, so they get a deliberately slow, salted hash (scrypt).

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    return "scrypt${}${}${}${}${}".format(
        _SCRYPT_N, _SCRYPT_R, _SCRYPT_P, base64.b64encode(salt).decode(), base64.b64encode(digest).decode()
    )


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(
            password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=len(expected)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# --- API keys and invite tokens -----------------------------------------------
# These are 256 bits of randomness, not something a person chose, so a plain
# SHA-256 is the right hash: nothing to brute-force, and lookups stay fast.


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class NewApiKey:
    plaintext: str  # shown to the user exactly once
    prefix: str  # stored, for display
    key_hash: str  # stored, for lookup


def generate_api_key() -> NewApiKey:
    plaintext = API_KEY_PREFIX + secrets.token_urlsafe(32)
    return NewApiKey(plaintext=plaintext, prefix=plaintext[:12], key_hash=hash_token(plaintext))


def generate_invite_token() -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    return token, hash_token(token)


# --- Dashboard sessions -------------------------------------------------------


def issue_session(user_id: str, secret: str, ttl_hours: int) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode({"sub": user_id, "iat": now, "exp": now + timedelta(hours=ttl_hours)}, secret, algorithm="HS256")


def read_session(token: str, secret: str) -> str | None:
    """Return the user id in a session token, or None if it is invalid or expired."""
    try:
        return jwt.decode(token, secret, algorithms=["HS256"]).get("sub")
    except jwt.PyJWTError:
        return None


# --- Upstream credentials at rest ---------------------------------------------


class CredentialCipher:
    """Encrypts upstream credentials before they reach the database.

    The key comes from a Kubernetes Secret, so a copy of the database alone
    does not reveal the credentials.
    """

    def __init__(self, secret: str) -> None:
        derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"urumi-upstream-credentials").derive(
            secret.encode()
        )
        self._fernet = Fernet(base64.urlsafe_b64encode(derived))

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        """Raises ValueError if the ciphertext was not produced with this key."""
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken as exc:
            raise ValueError("stored credential cannot be decrypted with the current encryption key") from exc
