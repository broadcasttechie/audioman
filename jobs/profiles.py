"""Recorder profiles in the database: seeding and loading (see jobs/filename_patterns.py)."""
from config import Config
from app.extensions import db
from app.models import RecorderProfile
from .filename_patterns import DEFAULT_PROFILES


def ensure_default_profiles():
    """Insert any default profile whose NAME is missing. Never overwrites or resurrects
    anything: a profile the user edited or deactivated keeps its row, so it is left alone."""
    existing = {name for (name,) in db.session.query(RecorderProfile.name).all()}
    added = []
    for spec in DEFAULT_PROFILES:
        if spec["name"] in existing:
            continue
        db.session.add(RecorderProfile(
            name=spec["name"], patterns=list(spec["patterns"]), date_trust=spec["date_trust"],
            priority=spec["priority"], timezone=Config.DEFAULT_RECORDER_TIMEZONE,
            clock_offset_seconds=0, active=True,
        ))
        added.append(spec["name"])
    if added:
        db.session.commit()
    return added


def active_profiles():
    """Active profiles in the order they should be tried."""
    ensure_default_profiles()
    return (RecorderProfile.query.filter_by(active=True)
            .order_by(RecorderProfile.priority, RecorderProfile.name).all())
