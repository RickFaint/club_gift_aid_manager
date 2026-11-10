"""Runtime text configuration for the volunteer trip declaration."""

from __future__ import annotations

# Central place for the declaration text displayed on mileage forms. The list
# is an in-memory history for this process; submitted trips keep their own text
# snapshot in the database so later wording changes do not rewrite old claims.
DECLARATION_TEXT = (
    "I declare that I have taken other parents' children on this journey."
)

AUDIT_LOG = []


def get_declaration_text() -> str:
    """Return the wording currently shown to users when they submit a trip."""
    return DECLARATION_TEXT


def update_declaration_text(new_text: str) -> str:
    """Replace the current wording and retain the prior/current pair in memory."""
    global DECLARATION_TEXT
    previous = DECLARATION_TEXT
    DECLARATION_TEXT = new_text
    AUDIT_LOG.append({"previous": previous, "current": new_text})
    return DECLARATION_TEXT
