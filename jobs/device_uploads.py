"""
Turning a finished chunked upload (app/device_api.py) into a Resource, and cleaning up abandoned
or old upload sessions (PLAN 19.3).

Ingest here is deliberately NOT jobs.ingest.ingest_staged_file: that function is built for a file
arriving with no context except its filename (the Inbox trickle), so it works hard to guess a
capture time and leaves everything else for the user to fill in at review. A device upload always
arrives WITH a metadata block the app already collected (project/session/category/tags/notes, and
optionally a capture-time override for files with no date in the name) -- so this applies that
directly, still using the same probe/checksum/date-resolution building blocks as every other
ingest path, and still landing in pending-review like everything else (nothing is auto-filed
without a category and a human's OK).
"""
import logging
import os
from datetime import datetime, timedelta

from config import Config
from app.extensions import db
from app.models import Resource, Tag, UploadSession, FileEvent, JobRun
from app.timeutil import parse_to_utc_naive
from .ingest import _ffprobe, _resolve_capture as _resolve_capture_from_file

log = logging.getLogger(__name__)


def _record_run(job_name, status, detail):
    run = JobRun.query.get(job_name) or JobRun(job_name=job_name)
    run.last_run_at = datetime.utcnow()
    run.status = status
    run.log_tail = detail[-4000:]
    db.session.merge(run)
    db.session.commit()


def _cleanup_file(path):
    """Removes the staged file and, if it was the only thing in its per-upload directory, the
    directory too (each upload gets its own dir under DEVICE_UPLOAD_DIR, see app/device_api.py)."""
    if not path:
        return
    try:
        if os.path.exists(path):
            os.remove(path)
        parent = os.path.dirname(path)
        if os.path.isdir(parent) and not os.listdir(parent):
            os.rmdir(parent)
    except OSError:
        pass


def resolve_capture(local_path, meta):
    """Same precedence as jobs.ingest._resolve_capture (trusted filename > embedded > suggestion),
    except an explicit captured_at from the app's metadata block wins over all of that -- but only
    ever lands as 'approximate' unless the app explicitly says 'exact' (PLAN 19.5: never a
    confirmed exact time from a batch guess unless the app/user actually knows it)."""
    if meta.get("captured_at"):
        precision = meta.get("captured_at_precision")
        if precision not in ("exact", "approximate"):
            precision = "approximate"
        info = {"profile": None, "device_override": True}
        if meta.get("source_path"):
            info["folder"] = os.path.dirname(meta["source_path"])
        return {
            "captured_at": parse_to_utc_naive(meta["captured_at"]), "source": "manual",
            "precision": precision, "suggested": None, "info": info,
        }
    return _resolve_capture_from_file(local_path, drive_inbox_path=meta.get("source_path"))


def _combine_title_notes(title, notes):
    parts = [p.strip() for p in (title, notes) if p and p.strip()]
    return "\n\n".join(parts) or None


def ingest_device_upload(session):
    """
    Turns a finished UploadSession into a Resource, or -- if the checksum turns out to already
    exist (a race with another upload; the cheap check at initiate time can't fully rule this out
    over a long transfer) -- returns the existing one instead and discards the redundant file.
    Raises on any real failure; the caller (POST .../complete) marks the session failed.
    project_id/session_id/category/tag_ids in metadata_json are assumed already validated at
    initiate time (app/device_api.py), so this does no further checking of them.
    """
    path = session.staging_path
    meta = session.metadata_json or {}

    existing = Resource.query.filter_by(checksum=session.declared_checksum).first()
    if existing:
        _cleanup_file(path)
        return existing

    probe = _ffprobe(path)
    capture = resolve_capture(path, meta)

    resource = Resource(
        checksum=session.declared_checksum, filename=session.filename,
        size_bytes=os.path.getsize(path), format=probe["format"], duration_seconds=probe["duration_seconds"],
        captured_at=capture["captured_at"], captured_at_source=capture["source"],
        captured_at_precision=capture["precision"], suggested_captured_at=capture["suggested"],
        filename_info=capture["info"], category=meta.get("category"),
        project_id=meta.get("project_id"), session_id=meta.get("session_id"),
        notes=_combine_title_notes(meta.get("title"), meta.get("notes")),
        drive_inbox_path=meta.get("source_path"),   # doubles as "wherever this came from", Drive or device
        status="pending-review", staging_path=path,
    )
    db.session.add(resource)
    db.session.flush()

    tag_ids = meta.get("tag_ids") or []
    if tag_ids:
        resource.tags = Tag.query.filter(Tag.id.in_(tag_ids)).all()

    db.session.add(FileEvent(resource_id=resource.id, event_type="ingested",
                             detail=f"uploaded from device {session.device_id}: {session.filename}"))
    db.session.commit()
    return resource


def cleanup_stale_uploads():
    """Sweeper job: fails any 'uploading' session with no chunk activity for
    DEVICE_UPLOAD_ABANDONED_HOURS (removing its partial file -- it can never be resumed sanely once
    the app has given up on it), and drops old completed/failed session ROWS after
    DEVICE_UPLOAD_SESSION_RETENTION_DAYS purely for tidiness (the Resource a completed one made, if
    any, is never touched by this)."""
    abandoned_cutoff = datetime.utcnow() - timedelta(hours=Config.DEVICE_UPLOAD_ABANDONED_HOURS)
    abandoned = UploadSession.query.filter(
        UploadSession.status == "uploading", UploadSession.updated_at < abandoned_cutoff,
    ).all()
    for s in abandoned:
        _cleanup_file(s.staging_path)
        s.status = "failed"
        s.error_detail = f"abandoned: no activity for over {Config.DEVICE_UPLOAD_ABANDONED_HOURS}h"
    db.session.commit()

    retention_cutoff = datetime.utcnow() - timedelta(days=Config.DEVICE_UPLOAD_SESSION_RETENTION_DAYS)
    removed = UploadSession.query.filter(
        UploadSession.status.in_(("completed", "failed")), UploadSession.updated_at < retention_cutoff,
    ).delete(synchronize_session=False)
    db.session.commit()

    _record_run("cleanup-device-uploads", "success", f"abandoned: {len(abandoned)}, old rows removed: {removed}")
    return {"status": "success", "abandoned": len(abandoned), "removed": removed}
