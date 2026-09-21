"""
Swappable services (PLAN 20).

The app talks to three kinds of outside service, each behind one small function so that another service can be
swapped in by configuration alone, without touching the code that uses it:

  kind       function contract                                                       today
  ---------  ----------------------------------------------------------------------  ---------
  location   fetch_track_and_pin(captured_at, duration_seconds, max_attempts=None)   dawarich
               -> (track_points, pin)   track_points: [{"timestamp": naive-UTC datetime, "lat": float, "lon": float}]
                                        pin: {"lat", "lon"} or None
  geocoder   reverse(lat, lon, max_attempts=None)                                    photon
               -> None (nothing nearby) | {"label": str|None, "info": dict, "near": bool} | "unconfigured"
  photos     fetch_photos_for_recording(captured_at, duration_seconds, max_attempts=None)   immich
               -> [{"immich_asset_id": str, "taken_at": naive-UTC datetime}]   (see the note below)

Every provider raises `jobs.retry.ServiceUnavailable` (or its subclass ServiceRejected) when its service can't be
reached or refuses the request, and never raises for "nothing found". All datetimes in and out are naive UTC
(app/timeutil.py).

Choosing one: set LOCATION_PROVIDER / GEOCODER_PROVIDER / PHOTO_PROVIDER (environment, or the settings table) to a
registered name, or to "none" to switch that feature off. An unknown name is reported clearly instead of being
guessed at. Providers are imported only when used, so an unused one costs nothing and may have extra dependencies.

To add a provider: write the function to the contract above in a module under jobs/, add one line to REGISTRY,
give it a test, and set the config key. Not done yet, on purpose (only the current setup is built): see PLAN 20 for
candidates. One known gap: the photos contract still returns Immich-style asset ids and the photo thumbnail/original
routes proxy to Immich, so a different photo service also needs an equivalent of those two routes.
"""
import importlib

from config import Config
from app.settings import get_config

# kind -> name -> "module:function"
REGISTRY = {
    "location": {"dawarich": "jobs.dawarich:fetch_track_and_pin"},
    "geocoder": {"photon": "jobs.photon:reverse"},
    "photos": {"immich": "jobs.immich:fetch_photos_for_recording"},
}

CONFIG_KEYS = {"location": "LOCATION_PROVIDER", "geocoder": "GEOCODER_PROVIDER", "photos": "PHOTO_PROVIDER"}
DISABLED = ("", "none", "off", "disabled")


class UnknownProvider(Exception):
    """The configured provider name isn't registered."""


def provider_name(kind):
    return (get_config(CONFIG_KEYS[kind]) or "").strip().lower()


def get(kind):
    """The function for the configured provider of this kind, or None if that feature is switched off."""
    name = provider_name(kind)
    if name in DISABLED:
        return None
    spec = REGISTRY[kind].get(name)
    if spec is None:
        raise UnknownProvider(
            f"{CONFIG_KEYS[kind]} is '{name}', which isn't a known {kind} provider. "
            f"Available: {', '.join(sorted(REGISTRY[kind]))}, or 'none' to switch it off."
        )
    module, func = spec.split(":")
    return getattr(importlib.import_module(module), func)


def describe():
    """For the settings page: what is active for each kind and what else could be chosen."""
    return {kind: {"active": provider_name(kind) or "none", "available": sorted(REGISTRY[kind]), "setting": CONFIG_KEYS[kind]}
            for kind in REGISTRY}
