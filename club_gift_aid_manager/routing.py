"""External geocoding and road-routing integration for trip distance lookups."""

from __future__ import annotations

import json
import math
import os
import time
from threading import Lock
from urllib.error import URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from .db import get_destination, save_destination_coordinates

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


class RouteLookupError(ValueError):
    """A user-facing route lookup failure with the HTTP status to return."""

    def __init__(self, message: str, status_code: int = 422):
        """Store both the explanation and suitable Flask response status."""
        super().__init__(message)
        self.status_code = status_code


def _request_json(url: str) -> object:
    """Fetch JSON with a short timeout and identify service/network failures."""
    # The service URL is configurable, but local-file and custom URL schemes
    # must never be allowed to turn routing into a file-read or SSRF primitive.
    if urlsplit(url).scheme not in {"http", "https"}:
        raise RouteLookupError("The route service URL must use HTTP or HTTPS.", 502)
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "ClubGiftAidManager/1.0",
        },
    )
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:  # nosec B310
            return json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RouteLookupError("The route service is unavailable. Please try again later.", 502) from exc


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
        params = urlencode({
            "format": "jsonv2",
            "limit": 1,
            "countrycodes": "gb",
            "q": query,
        })
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
    if not math.isfinite(latitude) or not math.isfinite(longitude) or not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise RouteLookupError("The geocoding service returned invalid coordinates.", 502)

    save_destination_coordinates(location["id"], latitude, longitude)
    return latitude, longitude


def calculate_route_distance(origin_id: int, destination_id: int) -> dict[str, object]:
    """Route between active saved locations and return one-way miles.

    Coordinates are supplied to OSRM in longitude,latitude order. The returned
    response includes provider attribution for display alongside the mileage.
    """
    origin = get_destination(origin_id)
    destination = get_destination(destination_id)
    if origin is None or not origin["active"]:
        raise RouteLookupError("Origin location was not found or is inactive.")
    if destination is None or not destination["active"]:
        raise RouteLookupError("Destination was not found or is inactive.")

    origin_latitude, origin_longitude = _geocode_location(origin)
    destination_latitude, destination_longitude = _geocode_location(destination)
    coordinates = (
        f"{origin_longitude},{origin_latitude};"
        f"{destination_longitude},{destination_latitude}"
    )
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

    return {
        "origin_id": origin_id,
        "destination_id": destination_id,
        "distance_meters": distance_metres,
        "distance_miles": round(distance_metres / METRES_PER_MILE, 1),
        "provider": "OpenStreetMap / OSRM",
        "attribution": "© OpenStreetMap contributors",
    }
