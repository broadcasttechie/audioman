"""
Ingest pipeline: a file freshly pulled into Config.STAGING_DIR becomes
a `pending-review` Resource row. Plugged in from
jobs.rclone_jobs.drive_inbox_pull right after `rclone move` lands a
file locally.

Deliberately calls NO external services (Dawarich, Immich) — ingest
must never depend on anything outside this server. Location and photo
enrichment happen entirely separately, in jobs/enrich.py, which is
what makes an external outage self-healing: ingest keeps working
regardless, and enrichment just catches up whenever the service comes
back (see dawarich_checked_at / immich_checked_at on Resource).

Two deliberate behavioral decisions, not defaults to revisit lightly:

1. Duplicate (checksum already exists as a Resource): the incoming
   file is quarantined under STAGING_DIR/duplicates and logged, NOT
   auto-discarded. Whether a genuine duplicate should just be deleted
   is a real policy decision this doesn't make for you.
2. The recording time comes from (see _resolve_capture): a filename date read by
   a TRUSTED recorder profile (source "filename"); else a fixed set of embedded
   metadata tags (TRUSTED_TIMESTAMP_TAGS below; source "embedded"). Both are
   `exact`. A date from a merely "suggest" profile is stored as a suggestion for
   the user to confirm, not applied. Filesystem mtime is deliberately never
   used — it survives uploads/moves unreliably, and one of the user's recorders
   doesn't set it at all, so a wrong-but-plausible timestamp would be worse than
   none (silently mis-locating a recording via Dawarich). No date -> captured_at
   stays None (precision "unknown") and enrichment is skipped entirely for that
   resource (no captured_at means nothing to search a time window around).
"""
import hashlib
import json
import os
import shutil
import subprocess
from datetime import datetime

from config import Config
from app.extensions import db
from app.timeutil import parse_exif_datetime
from app.models import Resource, FileEvent
from .filename_patterns import match_filename
from .profiles import active_profiles

TRUSTED_TIMESTAMP_TAGS = [
    "DateTimeOriginal",
    "CreationDate",
    "MediaCreateDate",
    "EncodedDate",
    "OriginationDate",  # WAV broadcast-wave chunk - likely tag for field recorders
]

QUARANTINE_DIR = os.path.join(Config.STAGING_DIR, "duplicates")


def ingest_staged_file(local_path, drive_inbox_path=None):
    """
    local_path: current location of the file inside Config.STAGING_DIR
    drive_inbox_path: its original relative path under Drive /Inbox,
        kept for audit purposes even after the file has moved on.
    Returns the Resource row (status pending-review or failed), or
    None if the file was a quarantined duplicate.
    """
    filename = os.path.basename(local_path)

    try:
        checksum = _sha256(local_path)
    except Exception as e:
        return _fail(None, local_path, "checksum", str(e))

    existing = Resource.query.filter_by(checksum=checksum).first()
    if existing:
        os.makedirs(QUARANTINE_DIR, exist_ok=True)
        dest = os.path.join(QUARANTINE_DIR, filename)
        shutil.move(local_path, dest)
        db.session.add(FileEvent(
            resource_id=existing.id, event_type="failed",
            detail=f"duplicate of existing resource {existing.id}, quarantined at {dest}",
        ))
        db.session.commit()
        return None

    try:
        probe = _ffprobe(local_path)
    except Exception as e:
        return _fail(checksum, local_path, "metadata-extraction", str(e))

    capture = _resolve_capture(local_path, drive_inbox_path)

    resource = Resource(
        checksum=checksum,
        filename=filename,
        format=probe["format"],
        duration_seconds=probe["duration_seconds"],
        captured_at=capture["captured_at"],
        captured_at_source=capture["source"],
        captured_at_precision=capture["precision"],
        suggested_captured_at=capture["suggested"],
        filename_info=capture["info"],
        category=None,  # set during review, not inferable here
        status="pending-review",
        drive_inbox_path=drive_inbox_path,
        staging_path=local_path,
        # dawarich_checked_at / immich_checked_at default to NULL —
        # jobs/enrich.py picks these up on its next scheduled run.
        # If captured_at is None there's no time window to search, so
        # they'll never be queried, which is correct (nothing to find).
    )
    db.session.add(resource)
    db.session.flush()

    db.session.add(FileEvent(resource_id=resource.id, event_type="ingested", detail=local_path))
    db.session.commit()
    return resource


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _ffprobe(path):
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", path],
        capture_output=True, text=True, check=True, timeout=Config.PROBE_TIMEOUT_SECONDS,
    )
    data = json.loads(result.stdout)
    fmt = data.get("format", {})
    duration = fmt.get("duration")
    return {
        "format": (fmt.get("format_name") or "").split(",")[0] or None,
        "duration_seconds": float(duration) if duration else None,
    }


EMBEDDED_DISAGREE_SECONDS = 120


def _resolve_capture(local_path, drive_inbox_path=None):
    """
    Decide captured_at for a new file. Returns a dict: captured_at, source, precision,
    suggested (a date awaiting confirmation) and info (what the filename told us).
    """
    filename = os.path.basename(local_path)
    embedded, _ = _extract_timestamp(local_path)
    fm = match_filename(filename, active_profiles())

    info = {"profile": fm.profile if fm else None}
    if fm:
        info.update({"title": fm.title, "is_edit": fm.is_edit, "seq": fm.seq,
                     "unknown_reason": fm.unknown_reason, "time_known": fm.time_known})
    folder = os.path.dirname(drive_inbox_path) if drive_inbox_path else ""
    if folder:
        info["folder"] = folder  # the only context some files have (e.g. STE-000 in "2024 France")

    captured_at, source, precision, suggested = None, "manual", "unknown", None
    if fm and fm.utc and fm.trusted and fm.time_known:
        captured_at, source, precision = fm.utc, "filename", "exact"
        if embedded and abs((embedded - fm.utc).total_seconds()) > EMBEDDED_DISAGREE_SECONDS:
            info["embedded_disagrees"] = embedded.isoformat() + "Z"
    elif embedded:
        captured_at, source, precision = embedded, "embedded", "exact"
    elif fm and fm.utc:
        suggested = fm.utc
    return {"captured_at": captured_at, "source": source, "precision": precision,
            "suggested": suggested, "info": info}


def _extract_timestamp(path):
    """Returns (datetime | None, source). See module docstring for policy."""
    try:
        result = subprocess.run(
            ["exiftool", "-j"] + [f"-{tag}" for tag in TRUSTED_TIMESTAMP_TAGS] + [path],
            capture_output=True, text=True, check=True, timeout=Config.PROBE_TIMEOUT_SECONDS,
        )
        data = json.loads(result.stdout)[0]
    except Exception:
        return None, "manual"

    for tag in TRUSTED_TIMESTAMP_TAGS:
        raw = data.get(tag)
        if not raw:
            continue
        # Keeps a UTC offset if the tag has one; a bare wall-clock time is the
        # recorder's local time (app/timeutil.py), stored as UTC.
        parsed = parse_exif_datetime(raw, Config.DEFAULT_RECORDER_TIMEZONE)
        if parsed:
            return parsed, "embedded"

    return None, "manual"


def _fail(checksum, local_path, stage, detail):
    resource = Resource(
        checksum=checksum or f"unresolved-{os.path.basename(local_path)}-{datetime.utcnow().timestamp()}",
        filename=os.path.basename(local_path),
        status="failed",
        failure_stage=stage,
        failure_detail=detail,
        staging_path=local_path,
    )
    db.session.add(resource)
    db.session.flush()
    db.session.add(FileEvent(resource_id=resource.id, event_type="failed", detail=detail))
    db.session.commit()
    return resource
