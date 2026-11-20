"""Custom SQLAlchemy column types shared by several model modules."""

from typing import Any

from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

from ..security.crypto import decrypt_value, encrypt_value


class EncryptedText(TypeDecorator[str | None]):
    """Encrypt values before storage and decrypt them after database reads."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect) -> str | None:
        """Encrypt a Python value immediately before SQLAlchemy binds it to SQL."""
        return encrypt_value(value)

    def process_result_value(self, value: str | None, dialect) -> str | None:
        """Decrypt ciphertext immediately after SQLAlchemy reads the column."""
        return decrypt_value(value)
