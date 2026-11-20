"""SSRF controls for configurable geocoding and routing services."""

import socket
from urllib.error import HTTPError

import pytest

import club_gift_aid_manager.services.routing as routing


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.8",
        "172.16.0.8",
        "192.168.1.8",
        "169.254.169.254",
        "::1",
        "fe80::1",
    ],
)
def test_route_service_rejects_non_public_resolved_addresses(monkeypatch, address):
    """Loopback, private, link-local, and metadata targets never reach the network."""
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    monkeypatch.setattr(
        routing.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(family, socket.SOCK_STREAM, 6, "", (address, 443))],
    )

    with pytest.raises(routing.RouteLookupError, match="public network"):
        routing._request_json("https://routing.example.test/route")


def test_route_service_rejects_mixed_public_and_private_dns(monkeypatch):
    """A hostname is rejected if even one DNS answer could reach a private target."""
    monkeypatch.setattr(
        routing.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
        ],
    )

    with pytest.raises(routing.RouteLookupError, match="public network"):
        routing._request_json("https://routing.example.test/route")


def test_route_service_rejects_credentials_before_dns(monkeypatch):
    """User-info syntax cannot obscure the actual outbound destination."""
    monkeypatch.setattr(
        routing.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: pytest.fail("DNS should not be queried"),
    )

    with pytest.raises(routing.RouteLookupError, match="credentials"):
        routing._request_json("https://public.example@127.0.0.1/path")


def test_route_service_allows_public_dns_but_does_not_follow_redirects(monkeypatch):
    """The opener may contact a public host but treats redirects as failures."""
    monkeypatch.setattr(
        routing.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
        ],
    )

    def redirect(_request, timeout):
        """Simulate a service redirect that the hardened HTTP client must refuse."""
        assert timeout == routing.REQUEST_TIMEOUT_SECONDS
        raise HTTPError("https://routing.example.test/route", 302, "Found", {}, None)

    monkeypatch.setattr(routing._url_opener, "open", redirect)

    with pytest.raises(routing.RouteLookupError, match="unavailable"):
        routing._request_json("https://routing.example.test/route")
