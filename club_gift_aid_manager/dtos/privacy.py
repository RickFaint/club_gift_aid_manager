"""Privacy-notice DTOs that preserve immutable text exactly."""

from pydantic import ConfigDict

from .base import DTO


class PrivacyNoticeVersionDTO(DTO):
    """Represent an immutable published privacy notice and its integrity digest."""

    model_config = ConfigDict(from_attributes=True, extra="forbid", str_strip_whitespace=False)

    id: int
    effective_date: str
    published_date: str
    text_sha256: str
    notice_text: str
