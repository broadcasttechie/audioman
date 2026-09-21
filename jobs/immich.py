"""
Thin client for Immich's search API.

- Auth: x-api-key HEADER (not a query param — different from Dawarich,
  worth not mixing the two up).
- POST /api/search/metadata with takenAfter/takenBefore filters the
  library by capture time — this is the documented shape as of
  Immich's older stable API. Immich has since introduced a "search v2"
  API and deprecated some endpoints in past releases, so ⚠️ confirm
  /api/search/metadata is still current on your instance's own API
  docs (usually at https://<your-immich-host>/api/docs, or
  https://api.immich.app for the hosted reference) before relying on
  this — the response shape in particular (a bare list vs. a nested
  {"assets": {"items": [...]}}") has changed between versions, so
  _extract_items below defensively handles both.
"""
from datetime import timedelta

import requests

from config import Config
from app.settings import get_config
from app.timeutil import parse_to_utc_naive, to_utc_iso
from .retry import call_with_retry

PHOTO_PADDING = timedelta(minutes=5)


def fetch_photos_in_range(start, end, max_attempts=None):
    """
    Returns a list of {"immich_asset_id": str, "taken_at": datetime}
    for IMAGE assets taken in [start, end]. Returns [] if Immich isn't
    configured or nothing matches. Raises ServiceUnavailable if the
    request fails after retries.
    """
    immich_url = get_config("IMMICH_API_URL")
    if not immich_url:
        return []

    def _fetch():
        response = requests.post(
            f"{immich_url}/api/search/metadata",
            headers={"x-api-key": get_config("IMMICH_API_KEY")},
            json={
                "takenAfter": to_utc_iso(start),
                "takenBefore": to_utc_iso(end),
                "type": "IMAGE",
            },
            timeout=Config.HTTP_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return response

    response = call_with_retry(_fetch, max_attempts=max_attempts)

    photos = []
    for item in _extract_items(response.json()):
        taken_raw = item.get("fileCreatedAt") or item.get("takenAt") or item.get("localDateTime")
        photos.append({
            "immich_asset_id": item["id"],
            "taken_at": _parse_dt(taken_raw) if taken_raw else None,
        })
    return photos


def _extract_items(payload):
    """Handles both a bare list and the nested search-response shape."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and "assets" in payload:
        return payload["assets"].get("items", [])
    return []


def _parse_dt(raw):
    # Naive UTC, like every other datetime we store (app/timeutil.py).
    try:
        return parse_to_utc_naive(raw)
    except (ValueError, AttributeError):
        return None


def fetch_photos_for_recording(captured_at, duration_seconds, max_attempts=None):
    """Convenience wrapper matching the ingest pipeline's call shape."""
    end_time = captured_at + timedelta(seconds=duration_seconds or 0)
    return fetch_photos_in_range(
        captured_at - PHOTO_PADDING, end_time + PHOTO_PADDING, max_attempts=max_attempts,
    )
