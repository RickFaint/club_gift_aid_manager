"""Field encryption and blind indexes for sensitive database values."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from functools import lru_cache
from typing import Any

from cryptography.fernet import Fernet, InvalidToken, MultiFernet


ENCRYPTED_PREFIX = "enc:v1:"


class EncryptionConfigurationError(RuntimeError):
    """Raised when encryption keys are absent or malformed."""


class EncryptedValueError(RuntimeError):
    """Raised when encrypted storage contains plaintext or invalid ciphertext."""


def _encryption_keys(configured: str | None = None) -> list[bytes]:
    configured = os.environ.get("GIFT_AID_ENC_KEY", "") if configured is None else configured
    keys = [value.strip().encode("ascii") for value in configured.split(",") if value.strip()]
    if not keys:
        raise EncryptionConfigurationError("GIFT_AID_ENC_KEY must contain at least one Fernet key.")
    try:
        for key in keys:
            Fernet(key)
    except (TypeError, ValueError) as exc:
        raise EncryptionConfigurationError("GIFT_AID_ENC_KEY contains an invalid Fernet key.") from exc
    return keys


@lru_cache(maxsize=8)
def _fernet_for_config(configured: str) -> MultiFernet:
    return MultiFernet([Fernet(key) for key in _encryption_keys(configured)])


def _fernet() -> MultiFernet:
    return _fernet_for_config(os.environ.get("GIFT_AID_ENC_KEY", ""))


@lru_cache(maxsize=8)
def _blind_index_key_for_config(configured: str) -> bytes:
    configured = configured.strip()
    if not configured:
        raise EncryptionConfigurationError(
            "GIFT_AID_BLIND_INDEX_KEY must contain a separate base64-encoded key."
        )
    try:
        key = base64.urlsafe_b64decode(configured.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise EncryptionConfigurationError(
            "GIFT_AID_BLIND_INDEX_KEY is not valid URL-safe base64."
        ) from exc
    if len(key) < 32:
        raise EncryptionConfigurationError(
            "GIFT_AID_BLIND_INDEX_KEY must decode to at least 32 bytes."
        )
    return key


def _blind_index_key() -> bytes:
    return _blind_index_key_for_config(os.environ.get("GIFT_AID_BLIND_INDEX_KEY", ""))


def validate_encryption_config() -> None:
    """Validate both independent key settings without exposing their contents."""
    _encryption_keys()
    _blind_index_key()


def encrypt_value(value: Any | None) -> str | None:
    """Encrypt a scalar value; NULL stays NULL and empty strings are encrypted."""
    if value is None:
        return None
    plaintext = str(value).encode("utf-8")
    return ENCRYPTED_PREFIX + _fernet().encrypt(plaintext).decode("ascii")


def decrypt_value(value: str | None) -> str | None:
    """Decrypt a marked value and reject plaintext or corrupted ciphertext."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.startswith(ENCRYPTED_PREFIX):
        raise EncryptedValueError("An encrypted database column contains unmarked plaintext.")
    token = value[len(ENCRYPTED_PREFIX):]
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError, UnicodeEncodeError) as exc:
        raise EncryptedValueError("An encrypted database value is invalid or cannot be decrypted.") from exc


def rotate_value(value: str | None) -> str | None:
    """Re-encrypt an existing token with the first configured Fernet key."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.startswith(ENCRYPTED_PREFIX):
        raise EncryptedValueError("An encrypted database column contains unmarked plaintext.")
    token = value[len(ENCRYPTED_PREFIX):]
    try:
        return ENCRYPTED_PREFIX + _fernet().rotate(token.encode("ascii")).decode("ascii")
    except (InvalidToken, UnicodeEncodeError) as exc:
        raise EncryptedValueError("An encrypted database value is invalid or cannot be rotated.") from exc


def blind_index(value: str) -> str:
    """Return a keyed exact-match index over normalized text."""
    normalized = value.strip().casefold().encode("utf-8")
    return hmac.new(_blind_index_key(), normalized, hashlib.sha256).hexdigest()
