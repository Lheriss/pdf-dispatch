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
    The key is derived once per process; every encrypt/decrypt reuses it.

No EMAIL_SECRET: fail-closed (since v1.8.2)
    Earlier versions generated a random key at each start and wrote it to
    the log. Stored IMAP passwords then became unreadable after the next
    restart, and the key leaked to anyone reading the log. Now no key is
    generated: storing a password raises EmailSecretMissing (the API turns
    it into an explicit error), reading one raises PasswordUnreadable, and
    the key itself is never logged.
"""

import base64
import hashlib
import logging
import os
from typing import Optional

log = logging.getLogger("pdf-dispatch.crypto")

# Clé Fernet en cache ; None = pas encore calculée.
_EMAIL_SECRET_KEY: Optional[bytes] = None


class EmailSecretMissing(RuntimeError):
    """EMAIL_SECRET is not set: IMAP passwords can be neither stored nor read."""


class PasswordUnreadable(RuntimeError):
    """A stored password cannot be decrypted (missing or different EMAIL_SECRET,
    corrupted value). The message says which, for the activity log."""


EMAIL_SECRET_HELP = (
    "EMAIL_SECRET is not set: IMAP passwords cannot be stored or read. "
    "Generate a key with `openssl rand -hex 32`, set it as EMAIL_SECRET in the "
    "container environment and restart."
)


def email_secret_configured() -> bool:
    """True when EMAIL_SECRET is set (non-empty)."""
    return bool(os.getenv("EMAIL_SECRET", ""))


def warn_if_email_secret_missing() -> None:
    """Log one startup warning when EMAIL_SECRET is absent (never a key)."""
    if not email_secret_configured():
        log.warning(EMAIL_SECRET_HELP)


def _get_email_secret() -> bytes:
    """Return the Fernet key (32-byte urlsafe-base64) derived from EMAIL_SECRET.

    Computed once and cached in _EMAIL_SECRET_KEY. Raises EmailSecretMissing
    when EMAIL_SECRET is not set — no ephemeral key is ever generated, and
    no key material is ever written to the log.
    """
    global _EMAIL_SECRET_KEY
    if _EMAIL_SECRET_KEY is not None:
        return _EMAIL_SECRET_KEY

    secret = os.getenv("EMAIL_SECRET", "")
    if not secret:
        raise EmailSecretMissing(EMAIL_SECRET_HELP)
    log.info("EMAIL_SECRET: key read from environment (" + str(len(secret)) + " chars)")

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
    Returns "" if plaintext is empty. Raises EmailSecretMissing when
    EMAIL_SECRET is not set (the password is NOT stored).
    """
    if not plaintext:
        return ""
    return _get_fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_password_checked(enc: str) -> str:
    """Decrypt a Fernet token, explaining any failure.

    Returns "" for an empty input (no password stored). Raises
    PasswordUnreadable with a message suitable for the activity log when the
    key is missing or does not match, or the value is corrupted.
    """
    if not enc:
        return ""
    from cryptography.fernet import InvalidToken  # noqa: PLC0415
    try:
        fernet = _get_fernet()
    except EmailSecretMissing as exc:
        raise PasswordUnreadable(str(exc)) from None
    try:
        return fernet.decrypt(enc.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeError):
        raise PasswordUnreadable(
            "the stored IMAP password cannot be decrypted: EMAIL_SECRET is not "
            "the key it was saved with (changed or lost), or the value is "
            "corrupted. Set the original EMAIL_SECRET, or enter the password again."
        ) from None


def decrypt_password(enc: str) -> str:
    """Decrypt a Fernet token; "" on any failure (never raises).

    Kept for callers that only need a best-effort value. Code that acts on
    the password should use decrypt_password_checked() to report why it is
    unreadable instead of treating it as empty.
    """
    try:
        return decrypt_password_checked(enc)
    except PasswordUnreadable:
        return ""
