"""Path validation and POSIX permission helpers for protected application data."""

from __future__ import annotations

import os
from pathlib import Path


PROJECT_DIRECTORY = Path(__file__).resolve().parent.parent
KNOWN_SYNC_COMPONENTS = {"onedrive", "dropbox"}
KNOWN_STATIC_COMPONENTS = {"static", "staticfiles"}


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def validate_private_path(path: Path, *, purpose: str, allow_file: bool = True) -> Path:
    """Reject application data paths in source, static, web, or sync locations."""
    resolved = path.expanduser().resolve()
    target = resolved if allow_file else resolved
    forbidden_roots = [PROJECT_DIRECTORY, PROJECT_DIRECTORY / "static"]
    configured_web_roots = os.environ.get("GIFT_AID_WEB_ROOTS", "")
    forbidden_roots.extend(
        Path(value.strip()).expanduser().resolve()
        for value in configured_web_roots.split(",")
        if value.strip()
    )
    for root in forbidden_roots:
        if _is_within(target, root):
            raise RuntimeError(f"{purpose} must be outside the repository and configured web roots.")
    normalized_parts = [part.casefold() for part in resolved.parts]
    if any(
        any(part == sync or part.startswith(f"{sync} ") for sync in KNOWN_SYNC_COMPONENTS)
        for part in normalized_parts
    ):
        raise RuntimeError(f"{purpose} must not be stored in OneDrive or Dropbox.")
    if any(part in KNOWN_STATIC_COMPONENTS for part in normalized_parts):
        raise RuntimeError(f"{purpose} must not be stored in a static-files directory.")
    return resolved


def ensure_private_directory(path: Path) -> Path:
    """Create a directory and enforce owner-only POSIX access."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        os.chmod(path, 0o700)
    return path


def ensure_private_file(path: Path) -> None:
    """Enforce owner read/write access for a POSIX data file."""
    if os.name == "posix":
        os.chmod(path, 0o600)
