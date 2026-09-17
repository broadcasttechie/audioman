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
2. Only a fixed set of embedded metadata tags count as a trustworthy
   recording timestamp (TRUSTED_TIMESTAMP_TAGS below). Filesystem
   mtime is deliberately never used as a fallback — it survives
   uploads/moves unreliably, so a wrong-but-plausible timestamp would
   be worse than none (silently mis-locating a recording via Dawarich).
   No trusted tag found -> captured_at stays None, source "manual",
   and enrichment is skipped entirely for that resource (no
   captured_at means nothing to search a time window around).
"""
import hashlib
import json
import os
import shutil
import subprocess
from datetime import datetime

from config import Config
from app.extensions import db
from app.models import Resource, FileEvent

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

    captured_at, captured_at_source = _extract_timestamp(local_path)

    resource = Resource(
        checksum=checksum,
        filename=filename,
        format=probe["format"],
        duration_seconds=probe["duration_seconds"],
        captured_at=captured_at,
        captured_at_source=captured_at_source,
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
        try:
            return datetime.strptime(raw[:19], "%Y:%m:%d %H:%M:%S"), "embedded"
        except ValueError:
            continue

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
