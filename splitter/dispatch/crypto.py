"""
dispatch/crypto.py
-------------------
Encryption of IMAP passwords stored in the configuration.

Uses Fernet (AES-128-CBC + authenticated HMAC-SHA256) with a key derived
from the EMAIL_SECRET environment variable via SHA-256.

Why a derived key rather than Fernet directly?
    Fernet requires a 32-byte base64-url-encoded key. EMAIL_SECRET is an
    arbitrary string (64-char hex, passphrase, etc.) — SHA-256 normalises
    it cleanly to 32 bytes, then base64-url encoding brings it to the
    expected format.

Why cache (_EMAIL_SECRET_KEY)?
    Without a cache, every encrypt/decrypt call re-reads the environment,
    re-logs the info message, and — if EMAIL_SECRET is absent — generates
    a different ephemeral key each time, making subsequent decryption
    impossible (historical bug pre-v1.10, fixed in v1.11).
"""

import base64
import hashlib
import logging
import os
from typing import Optional

log = logging.getLogger("pdf-dispatch.crypto")

# Clé Fernet en cache ; None = pas encore calculée.
_EMAIL_SECRET_KEY: Optional[bytes] = None


def _get_email_secret() -> bytes:
    """Return the Fernet key (32-byte urlsafe-base64) derived from EMAIL_SECRET.

    Computed once and cached in _EMAIL_SECRET_KEY.
    If EMAIL_SECRET is not set, a random ephemeral key is generated and
    instructions for setting a persistent one are logged.
    """
    global _EMAIL_SECRET_KEY
    if _EMAIL_SECRET_KEY is not None:
        return _EMAIL_SECRET_KEY

    secret = os.getenv("EMAIL_SECRET", "")
    if not secret:
        import secrets as _sec
        generated = _sec.token_hex(32)
        log.warning("EMAIL_SECRET not set — generated key: " + generated)
        log.warning(
            "Add to docker-compose.yml or Portainer: EMAIL_SECRET=" + generated
        )
        secret = generated
    else:
        log.info(
            "EMAIL_SECRET: key read from environment ("
            + str(len(secret))
            + " chars)"
        )

    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    _EMAIL_SECRET_KEY = base64.urlsafe_b64encode(digest)
    return _EMAIL_SECRET_KEY


def _get_fernet():
    """Build a Fernet instance from the EMAIL_SECRET key."""
    from cryptography.fernet import Fernet  # noqa: PLC0415
    return Fernet(_get_email_secret())


def encrypt_password(plaintext: str) -> str:
    """Encrypt a password with Fernet (AES-128-CBC + HMAC-SHA256).

    Returns an ASCII string (base64-url-encoded Fernet token).
    Returns "" if plaintext is empty.
    """
    if not plaintext:
        return ""
    return _get_fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_password(enc: str) -> str:
    """Decrypt a Fernet token.

    Returns "" on failure (EMAIL_SECRET missing/invalid, corrupted token,
    empty input) without raising an exception.
    """
    if not enc:
        return ""
    try:
        return _get_fernet().decrypt(enc.encode("ascii")).decode("utf-8")
    except Exception:
        return ""
