"""External geocoding and road-routing integration for trip distance lookups."""

from __future__ import annotations

from collections import OrderedDict
import json
import ipaddress
import math
import os
import socket
import time
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ..database import current_database_path
from ..db import get_destination, save_destination_coordinates

# These public endpoint defaults need no application key. They can be replaced
# with compatible services through environment variables for a production host.
GEOCODER_URL = os.environ.get(
    "CLUB_GIFT_AID_GEOCODER_URL",
    "https://nominatim.openstreetmap.org/search",
)
ROUTER_URL = os.environ.get(
    "CLUB_GIFT_AID_ROUTER_URL",
    "https://router.project-osrm.org/route/v1/driving",
)
REQUEST_TIMEOUT_SECONDS = 12
METRES_PER_MILE = 1609.344
_geocoder_lock = Lock()
_last_geocoder_request = 0.0
ROUTE_CACHE_SIZE = 2048
ROUTE_CACHE_TTL_SECONDS = 3600
_route_cache_lock = Lock()
_route_cache: OrderedDict[tuple[str, ...], tuple[float, float]] = OrderedDict()


class _RejectRedirects(HTTPRedirectHandler):
    """Prevent a public service URL redirecting a request into a private network."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """Return `None` so urllib raises instead of following a redirect target."""
        return None


_url_opener = build_opener(_RejectRedirects)


class RouteLookupError(ValueError):
    """A user-facing route lookup failure with the HTTP status to return."""

    def __init__(self, message: str, status_code: int = 422):
        """Store both the explanation and suitable Flask response status."""
        super().__init__(message)
        self.status_code = status_code


def _validate_public_service_url(url: str) -> None:
    """Require an HTTP(S) target whose every resolved address is public."""
    try:
        parsed = urlsplit(url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise RouteLookupError("The route service URL is invalid.", 502) from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RouteLookupError("The route service URL must use HTTP or HTTPS.", 502)
    if parsed.username is not None or parsed.password is not None:
        raise RouteLookupError("The route service URL must not contain credentials.", 502)
    try:
        addresses = {
            result[4][0]
            for result in socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
        }
    except (OSError, UnicodeError) as exc:
        raise RouteLookupError("The route service hostname could not be resolved.", 502) from exc
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise RouteLookupError(
            "The route service must resolve only to public network addresses.", 502
        )


def _request_json(url: str) -> object:
    """Fetch JSON from a validated public target without following redirects."""
    _validate_public_service_url(url)
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "ClubGiftAidManager/1.0",
        },
    )
    try:
        with _url_opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:  # nosec B310
            return json.loads(response.read().decode("utf-8"))
    except (HTTPError, OSError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RouteLookupError(
            "The route service is unavailable. Please try again later.", 502
        ) from exc


def _geocode_location(location: dict) -> tuple[float, float]:
    """Return cached coordinates, or geocode and persist the location once.

    A venue-plus-address search is attempted first. When a postcode exists, a
    postcode-only retry handles venue names that the geocoder does not know.
    Requests are spaced out to respect the public geocoder's usage policy.
    """
    if location.get("latitude") is not None and location.get("longitude") is not None:
        return float(location["latitude"]), float(location["longitude"])

    address_parts = [
        location.get("address"),
        location.get("town_city"),
        location.get("postcode"),
    ]
    address = ", ".join(str(part).strip() for part in address_parts if part and str(part).strip())
    if not address:
        raise RouteLookupError(
            f"Add an address, town or postcode for {location['name']} before calculating mileage."
        )

    queries = [
        ", ".join((location["name"], address, "United Kingdom")),
    ]
    postcode = str(location.get("postcode") or "").strip()
    if postcode:
        queries.append(f"{postcode}, United Kingdom")

    global _last_geocoder_request
    results = []
    for query in queries:
        params = urlencode(
            {
                "format": "jsonv2",
                "limit": 1,
                "countrycodes": "gb",
                "q": query,
            }
        )
        with _geocoder_lock:
            elapsed = time.monotonic() - _last_geocoder_request
            if _last_geocoder_request and elapsed < 1:
                time.sleep(1 - elapsed)
            response = _request_json(f"{GEOCODER_URL}?{params}")
            _last_geocoder_request = time.monotonic()
        if isinstance(response, list) and response:
            results = response
            break

    if not results:
        raise RouteLookupError(
            f"Could not find {location['name']} using its address or postcode. Check the location details."
        )
    try:
        latitude = float(results[0]["lat"])
        longitude = float(results[0]["lon"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RouteLookupError("The geocoding service returned an invalid location.", 502) from exc
    if (
        not math.isfinite(latitude)
        or not math.isfinite(longitude)
        or not -90 <= latitude <= 90
        or not -180 <= longitude <= 180
    ):
        raise RouteLookupError("The geocoding service returned invalid coordinates.", 502)

    save_destination_coordinates(location["id"], latitude, longitude)
    return latitude, longitude


def _route_distance_metres(coordinates: str) -> float:
    """Cache successful directional routes, bounded by time and entry count."""
    key = (str(current_database_path()), GEOCODER_URL, ROUTER_URL, coordinates)
    with _route_cache_lock:
        cached = _route_cache.get(key)
        if cached is not None:
            expires, distance = cached
            if expires > time.monotonic():
                _route_cache.move_to_end(key)
                return distance
            del _route_cache[key]

    params = urlencode({"overview": "false", "steps": "false"})
    result = _request_json(f"{ROUTER_URL.rstrip('/')}/{coordinates}?{params}")
    try:
        if not isinstance(result, dict) or result.get("code") != "Ok":
            raise RouteLookupError("No driving route was found between these locations.")
        distance_metres = float(result["routes"][0]["distance"])
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        if isinstance(exc, RouteLookupError):
            raise
        raise RouteLookupError("The routing service returned an invalid distance.", 502) from exc
    if not math.isfinite(distance_metres) or distance_metres < 0:
        raise RouteLookupError("The routing service returned an invalid distance.", 502)
    with _route_cache_lock:
        _route_cache[key] = (time.monotonic() + ROUTE_CACHE_TTL_SECONDS, distance_metres)
        _route_cache.move_to_end(key)
        while len(_route_cache) > ROUTE_CACHE_SIZE:
            _route_cache.popitem(last=False)
    return distance_metres


def calculate_route_distance(origin_id: int, destination_id: int) -> dict[str, object]:
    """Route between active saved locations and return one-way miles."""
    origin = get_destination(origin_id)
    destination = get_destination(destination_id)
    if origin is None or not origin["active"]:
        raise RouteLookupError("Origin location was not found or is inactive.")
    if destination is None or not destination["active"]:
        raise RouteLookupError("Destination was not found or is inactive.")

    distance_metres = 0.0
    if origin_id != destination_id:
        origin_latitude, origin_longitude = _geocode_location(origin)
        destination_latitude, destination_longitude = _geocode_location(destination)
        # Location edits clear coordinates; different coordinates select a fresh route.
        coordinates = (
            f"{origin_longitude},{origin_latitude};{destination_longitude},{destination_latitude}"
        )
        distance_metres = _route_distance_metres(coordinates)

    return {
        "origin_id": origin_id,
        "destination_id": destination_id,
        "distance_meters": distance_metres,
        "distance_miles": round(distance_metres / METRES_PER_MILE, 1),
        "provider": "OpenStreetMap / OSRM",
        "attribution": "© OpenStreetMap contributors",
    }
