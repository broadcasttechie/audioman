"""
Place names from a self-hosted Photon geocoder (https://github.com/komoot/photon).

Photon's `reverse` endpoint returns the nearest OpenStreetMap feature to a coordinate, which is often not the
most useful name for a recording: in woodland it can be a postcode point, on a platform it is "Railway Station
Platform 1". `format_place` turns the feature into a short label people would recognise:

  * a postcode is never used as the name;
  * the parts are (name or street) + district + city, plus the county/region only when that leaves fewer than
    three parts, at most four parts, no repeats;
  * a country is added only when it isn't the home country (PLACE_HOME_COUNTRY);
  * when nothing is found within Photon's default radius the search is widened (PLACE_FALLBACK_RADIUS_KM) and the
    label is prefixed "near ".

Nothing is sent outside the LAN, and an unreachable Photon never fails anything: the lookup just stays queued.
"""
import requests

from config import Config
from app.settings import get_config
from .retry import call_with_retry

KEEP = ("name", "street", "housenumber", "district", "locality", "city", "county", "state", "country", "countrycode",
        "osm_key", "osm_value")


def format_place(props, home_country="GB", near=False):
    """A Photon feature's `properties` -> a short label, or None if there is nothing usable."""
    is_postcode = props.get("osm_key") == "place" and props.get("osm_value") == "postcode"
    name = None if is_postcode else (props.get("name") or "").strip() or None
    if not name and props.get("housenumber") and props.get("street"):
        name = f"{props['housenumber']} {props['street']}"

    parts = []

    def add(value):
        value = (value or "").strip()
        if value and value.lower() not in (p.lower() for p in parts):
            parts.append(value)

    add(name)
    if props.get("osm_key") != "highway":          # a street feature's own name already is the street
        add(props.get("street"))
    add(props.get("district"))
    add(props.get("locality"))
    add(props.get("city"))
    if len(parts) < 3:
        add(props.get("county") or props.get("state"))
    label = ", ".join(parts[:4])
    if not label:
        return None
    code = (props.get("countrycode") or "").upper()
    if code and code != (home_country or "").upper() and props.get("country"):
        label += ", " + props["country"]
    return ("near " + label) if near else label


def _query(base, lat, lon, radius_km=None, max_attempts=None):
    params = {"lat": lat, "lon": lon, "lang": Config.PLACE_LANG, "limit": 1}
    if radius_km:
        params["radius"] = radius_km

    def _fetch():
        response = requests.get(f"{base}/reverse", params=params, timeout=Config.HTTP_TIMEOUT_SECONDS)
        response.raise_for_status()
        return response

    features = call_with_retry(_fetch, max_attempts=max_attempts).json().get("features") or []
    return features[0]["properties"] if features else None


def reverse(lat, lon, max_attempts=None):
    """
    Name a coordinate. Returns {"label", "info", "near"} (label may be None if the feature had nothing usable),
    or None when Photon has nothing at all nearby. Returns "unconfigured" if no Photon URL is set. Raises
    ServiceUnavailable (see jobs/retry.py) if Photon can't be reached or refuses the request.
    """
    base = (get_config("PHOTON_API_URL") or "").rstrip("/")
    if not base:
        return "unconfigured"
    props, near = _query(base, lat, lon, max_attempts=max_attempts), False
    if props is None and Config.PLACE_FALLBACK_RADIUS_KM:
        props, near = _query(base, lat, lon, radius_km=Config.PLACE_FALLBACK_RADIUS_KM, max_attempts=max_attempts), True
    if props is None:
        return None
    info = {k: props[k] for k in KEEP if props.get(k)}
    info["near"] = near
    return {"label": format_place(props, Config.PLACE_HOME_COUNTRY, near), "info": info, "near": near}
