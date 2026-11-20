"""Pseudonymous login-throttling counter model."""

from sqlalchemy import Integer, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class LoginAttempt(Base):
    """Map a pseudonymous failure counter used for persistent login throttling."""

    __tablename__ = "login_attempts"

    attempt_key: Mapped[str] = mapped_column(Text, primary_key=True)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False)
    window_started_at: Mapped[int] = mapped_column(Integer, nullable=False)
    locked_until: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
