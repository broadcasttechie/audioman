"""
Thin client for Dawarich's location-history API (confirmed against
https://dawarich.app/docs/api/dawarich-api/):

- Auth: api_key passed as a QUERY PARAMETER, not a Bearer header.
- GET /api/v1/points — paginated, filterable by date range. There's no
  single "nearest point to this timestamp" endpoint, so we fetch a
  window and pick/derive what we need ourselves.

NOTE: the exact date-range query param names (start_at/end_at below)
come from Dawarich's documented convention but weren't confirmed from
a static fetch of their docs page (it renders the param table via JS).
Verify against your instance's own /api-docs (Swagger/OpenAPI) once
Dawarich is running, and adjust PARAM_START/PARAM_END below if needed
— everything else in this client is unaffected by that detail.
"""
from datetime import datetime, timedelta

import requests

from config import Config
from app.settings import get_config
from .retry import call_with_retry

PARAM_START = "start_at"
PARAM_END = "end_at"

SEARCH_WINDOW = timedelta(minutes=30)
TRACK_PADDING = timedelta(minutes=2)


def fetch_points_in_range(start, end, max_attempts=None):
    """
    Returns a list of {"timestamp": datetime, "lat": float, "lon": float}
    sorted by timestamp, for all Dawarich points in [start, end].
    Handles pagination. Returns [] if Dawarich isn't configured or no
    points fall in range. Raises ServiceUnavailable (see jobs/retry.py)
    if the request itself fails after retries — callers decide what
    "unavailable" means for them (skip vs. abort the batch).
    """
    dawarich_url = get_config("DAWARICH_API_URL")
    if not dawarich_url:
        return []

    def _fetch_page(page):
        response = requests.get(
            f"{dawarich_url}/api/v1/points",
            params={
                "api_key": get_config("DAWARICH_API_KEY"),
                PARAM_START: start.isoformat(),
                PARAM_END: end.isoformat(),
                "per_page": 200,
                "page": page,
            },
            timeout=Config.HTTP_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return response

    points = []
    page = 1
    while True:
        response = call_with_retry(lambda: _fetch_page(page), max_attempts=max_attempts)
        batch = response.json()
        if not batch:
            break

        for p in batch:
            ts_raw = p.get("timestamp") or p.get("recorded_at")
            if not ts_raw:
                continue
            points.append({
                "timestamp": datetime.fromisoformat(ts_raw),
                "lat": p["latitude"],
                "lon": p["longitude"],
            })

        total_pages = int(response.headers.get("X-Total-Pages", 1))
        if page >= total_pages:
            break
        page += 1

    return sorted(points, key=lambda p: p["timestamp"])


def lookup_location(captured_at, max_attempts=None):
    """Single nearest-point lookup. Returns {"lat", "lon"} or None."""
    points = fetch_points_in_range(
        captured_at - SEARCH_WINDOW, captured_at + SEARCH_WINDOW, max_attempts=max_attempts,
    )
    if not points:
        return None
    closest = min(points, key=lambda p: abs(p["timestamp"] - captured_at))
    return {"lat": closest["lat"], "lon": closest["lon"]}


def fetch_track_and_pin(captured_at, duration_seconds, max_attempts=None):
    """
    One Dawarich call covering the full recording duration (+padding).
    Returns (track_points, pin). Raises ServiceUnavailable if the
    request fails after retries — this propagates up to the caller
    (jobs/enrich.py) which treats it as "try again next scheduled run",
    never as a reason to fail the resource itself.
    """
    end_time = captured_at + timedelta(seconds=duration_seconds or 0)
    points = fetch_points_in_range(
        captured_at - TRACK_PADDING, end_time + TRACK_PADDING, max_attempts=max_attempts,
    )
    if not points:
        return [], None

    pin_point = min(points, key=lambda p: abs(p["timestamp"] - captured_at))
    return points, {"lat": pin_point["lat"], "lon": pin_point["lon"]}
