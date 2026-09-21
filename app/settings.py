"""
Runtime overrides for a handful of Config values, editable from the
settings page without a restart. Everything else in config.py stays
env-var only — this only covers the keys actually surfaced on the
settings page (external API credentials + URLs).
"""
from config import Config
from .extensions import db
from .models import Setting

OVERRIDABLE = {
    "DAWARICH_API_URL",
    "DAWARICH_API_KEY",
    "IMMICH_API_URL",
    "IMMICH_API_KEY",
    "PHOTON_API_URL",
    "LOCATION_PROVIDER",
    "GEOCODER_PROVIDER",
    "PHOTO_PROVIDER",
    "UPLOAD_API_KEY",
    "GOOGLE_OAUTH_CLIENT_ID",
    "GOOGLE_OAUTH_CLIENT_SECRET",
}

SECRET_KEYS = {
    "DAWARICH_API_KEY", "IMMICH_API_KEY", "UPLOAD_API_KEY",
    "GOOGLE_OAUTH_CLIENT_SECRET",
}


def get_config(key):
    """DB override if one has been set, otherwise the config.py/env default."""
    if key in OVERRIDABLE:
        row = Setting.query.get(key)
        if row is not None and row.value != "":
            return row.value
    return getattr(Config, key)


def set_config(key, value):
    if key not in OVERRIDABLE:
        raise ValueError(f"{key} is not a settable key")
    row = Setting.query.get(key)
    if row is None:
        row = Setting(key=key, value=value)
        db.session.add(row)
    else:
        row.value = value
    db.session.commit()


def settings_snapshot():
    """
    Current values for the settings page. Secrets are reported as
    set/unset only -- never echoed back once saved.
    """
    out = {}
    for key in OVERRIDABLE:
        value = get_config(key)
        if key in SECRET_KEYS:
            out[key] = {"is_set": bool(value)}
        else:
            out[key] = {"value": value}
    return out
