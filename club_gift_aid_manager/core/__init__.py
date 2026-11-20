"""Application configuration and transport-independent input validation."""

from .config import DECLARATION_TEXT, get_declaration_text, update_declaration_text
from .validation import ValidationError, parse_dto

__all__ = [
    "DECLARATION_TEXT",
    "ValidationError",
    "get_declaration_text",
    "parse_dto",
    "update_declaration_text",
]
