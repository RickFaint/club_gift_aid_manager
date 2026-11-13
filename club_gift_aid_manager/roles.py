"""Canonical application roles.

``Role`` inherits from :class:`str`, so its members remain convenient wherever
Flask or SQLite works with ordinary strings.  Database writes still use
``.value`` explicitly to make the persisted representation clear.
"""

from enum import StrEnum


class Role(StrEnum):
    """The three authorization levels supported by the application."""

    ADMIN = "admin"
    TREASURER = "treasurer"
    VOLUNTEER = "volunteer"

