"""Trusted client-address resolution for explicitly configured hosting proxies."""

from ipaddress import ip_address
from typing import Any


class ProxyAddressError(ValueError):
    """Report a missing or malformed provider-owned client-address header."""


def validate_proxy_mode(mode: str) -> str:
    """Accept only documented proxy profiles; arbitrary header names are unsafe."""
    normalized = mode.strip().lower()
    if normalized not in {"none", "pythonanywhere"}:
        raise RuntimeError("CLUB_GIFT_AID_PROXY_MODE must be 'none' or 'pythonanywhere'.")
    return normalized


def resolve_client_address(request: Any, proxy_mode: str) -> str:
    """Return the rate-limit address from the direct peer or trusted PA header.

    PythonAnywhere documents that its load balancer replaces ``X-Real-IP`` with
    the address it received. ``X-Forwarded-For`` is deliberately ignored because
    PythonAnywhere warns that earlier entries may be supplied by untrusted peers.
    """
    if proxy_mode == "pythonanywhere":
        supplied = request.headers.get("X-Real-IP", "").strip()
        if not supplied:
            raise ProxyAddressError("The hosting proxy did not supply a client address.")
        try:
            return ip_address(supplied).compressed
        except ValueError as exc:
            raise ProxyAddressError(
                "The hosting proxy supplied an invalid client address."
            ) from exc
    supplied = (request.remote_addr or "").strip()
    if not supplied:
        raise ProxyAddressError("The request has no client address.")
    try:
        return ip_address(supplied).compressed
    except ValueError as exc:
        raise ProxyAddressError("The request has an invalid client address.") from exc
