"""
Per-device authentication for the Android app's API (PLAN 19 / NEXT.md package 4).

A device token is a long random string, shown to the user exactly once at creation (in the
Settings "Add a device" flow) and never stored anywhere -- only its sha256 lives in the database
(DeviceToken.token_hash), the same reasoning as a password hash: a database dump alone can't be
used to impersonate a device. Revoking a device keeps its row (never deleted, see app/models.py)
so past uploads still show which device they came from.
"""
import functools
import hashlib
import secrets
from datetime import datetime

from flask import request, jsonify, g

from app.extensions import db
from app.models import DeviceToken

TOKEN_HEADER = "X-Device-Token"


def hash_token(raw):
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def generate_token():
    """Returns (raw_token, token_hash). The raw token must be shown to the caller now -- it can
    never be recovered later, only re-issued as a new token."""
    raw = secrets.token_urlsafe(32)
    return raw, hash_token(raw)


def authenticate(raw_token):
    """The DeviceToken for a valid, non-revoked token, or None."""
    if not raw_token:
        return None
    token = DeviceToken.query.filter_by(token_hash=hash_token(raw_token)).first()
    if token is None or token.revoked_at is not None:
        return None
    return token


def require_device_token(view):
    """Route decorator for the device-facing API: 401s unless X-Device-Token names a live,
    unrevoked device, else sets flask.g.device and records last_used_at."""
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        device = authenticate(request.headers.get(TOKEN_HEADER))
        if device is None:
            return jsonify({"error": f"missing or invalid {TOKEN_HEADER} header"}), 401
        g.device = device
        device.last_used_at = datetime.utcnow()
        db.session.commit()
        return view(*args, **kwargs)
    return wrapped
