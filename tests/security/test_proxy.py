"""Trusted proxy address handling for login throttling."""

from types import SimpleNamespace

import pytest

from club_gift_aid_manager.security.proxy import (
    ProxyAddressError,
    resolve_client_address,
    validate_proxy_mode,
)


def request_stub(*, remote_addr="203.0.113.20", headers=None):
    """Build the small request interface consumed by the resolver."""
    return SimpleNamespace(remote_addr=remote_addr, headers=headers or {})


def test_direct_mode_ignores_spoofed_forwarding_headers():
    """Clients cannot select a rate-limit bucket when no proxy is trusted."""
    request = request_stub(
        headers={
            "X-Real-IP": "198.51.100.9",
            "X-Forwarded-For": "192.0.2.1",
        }
    )

    assert resolve_client_address(request, "none") == "203.0.113.20"


def test_pythonanywhere_mode_uses_only_validated_x_real_ip():
    """PythonAnywhere's provider-owned header replaces the load-balancer peer."""
    request = request_stub(
        remote_addr="10.0.0.5",
        headers={"X-Real-IP": "2001:0db8::1", "X-Forwarded-For": "192.0.2.1"},
    )

    assert resolve_client_address(request, "pythonanywhere") == "2001:db8::1"


@pytest.mark.parametrize(
    "headers", [{}, {"X-Real-IP": "not-an-ip"}, {"X-Real-IP": "1.2.3.4, 5.6.7.8"}]
)
def test_pythonanywhere_mode_fails_closed_on_invalid_proxy_contract(headers):
    """Missing or ambiguous proxy data cannot collapse or bypass throttling buckets."""
    with pytest.raises(ProxyAddressError):
        resolve_client_address(
            request_stub(remote_addr="10.0.0.5", headers=headers), "pythonanywhere"
        )


def test_proxy_mode_rejects_arbitrary_header_profiles():
    """Operators cannot accidentally configure trust in an attacker-chosen header."""
    with pytest.raises(RuntimeError, match="PROXY_MODE"):
        validate_proxy_mode("trust-everything")
