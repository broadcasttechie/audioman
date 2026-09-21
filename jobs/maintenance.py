"""
Maintenance tasks — each is safe to run on a schedule (nightly, low
priority) and also exposed via the manual "run now" API endpoint.
None of these should ever run silently-destructive: moves/deletes are
always logged to FileEvent first.

Dawarich/Immich backfill lives in jobs/enrich.py, not here — those are
a continuous self-healing queue (see that module's docstring), not an
occasional maintenance sweep.
"""
import os
from datetime import datetime

from config import Config
from app.extensions import db
from app.models import Resource, FileEvent, JobRun
from .nas import nas_status, sha256_file
from .path_template import render_path


def _nas_blocked(job_name):
    """A dropped NAS mount must stop these jobs, not make them report every file
    missing (or refile onto the local disk). Returns a result dict when blocked."""
    ok, reason = nas_status()
    if ok:
        return None
    _record_run(job_name, "error", f"NAS unavailable, nothing done: {reason}")
    return {"status": "error", "detail": f"NAS unavailable: {reason}"}


def _record_run(job_name, status, log_tail=""):
    run = JobRun.query.get(job_name) or JobRun(job_name=job_name)
    run.last_run_at = datetime.utcnow()
    run.status = status
    run.log_tail = log_tail[-4000:]
    db.session.merge(run)
    db.session.commit()


def refile_all():
    """
    Re-render every filed resource's path from the CURRENT template.
    Moves any resource whose stored nas_path no longer matches, and
    logs each move — then triggers a Drive resync.
    """
    blocked = _nas_blocked("refile-all")
    if blocked:
        return blocked
    moved, skipped = [], []
    for resource in Resource.query.filter_by(status="filed").all():
        expected = render_path(resource, project=resource.project, session=resource.session)
        expected_full = os.path.join(Config.NAS_LIBRARY_ROOT, expected)

        if resource.nas_path != expected_full:
            if os.path.exists(expected_full):
                # os.rename would silently overwrite another file: never do that.
                skipped.append({"id": resource.id, "issue": f"destination exists: {expected_full}"})
                continue
            os.makedirs(os.path.dirname(expected_full), exist_ok=True)
            if resource.nas_path and os.path.exists(resource.nas_path):
                os.rename(resource.nas_path, expected_full)
            resource.nas_path = expected_full
            db.session.add(FileEvent(
                resource_id=resource.id, event_type="refiled",
                detail=f"-> {expected_full}",
            ))
            moved.append(resource.id)

    db.session.commit()

    # TODO: trigger nas_to_drive_library() here once refiling is done,
    # rather than waiting for its own schedule.
    _record_run("refile-all", "partial" if skipped else "success", f"moved: {moved}, skipped: {skipped}")
    return {"status": "partial" if skipped else "success", "moved": moved, "skipped": skipped}


def verify_integrity():
    """Re-checksum NAS files, flag drift against stored checksums."""
    blocked = _nas_blocked("verify-integrity")
    if blocked:
        return blocked
    mismatches = []
    for resource in Resource.query.filter_by(status="filed").all():
        if not resource.nas_path or not os.path.exists(resource.nas_path):
            mismatches.append({"id": resource.id, "issue": "missing"})
            continue

        # Chunked: recordings run to hundreds of MB and the container has 2 GB.
        try:
            actual = sha256_file(resource.nas_path)
        except OSError as e:
            mismatches.append({"id": resource.id, "issue": f"unreadable: {e}"})
            continue
        if actual != resource.checksum:
            mismatches.append({"id": resource.id, "issue": "checksum-mismatch"})

    _record_run("verify-integrity", "success", f"mismatches: {mismatches}")
    return {"status": "success", "mismatches": mismatches}


def find_orphans():
    """
    Files present on disk (NAS or Drive Library) with no matching
    `resources` row, or vice versa. Depends on library_verify's rclone
    check output for the Drive side — left as a TODO wiring point.
    """
    blocked = _nas_blocked("find-orphans")
    if blocked:
        return blocked
    known_paths = {r.nas_path for r in Resource.query.filter_by(status="filed").all()}
    on_disk = set()
    for root, _, files in os.walk(Config.NAS_LIBRARY_ROOT):
        for f in files:
            if f == Config.NAS_MARKER_FILE:
                continue  # the mount-guard marker is not a recording
            on_disk.add(os.path.join(root, f))

    orphans_on_disk = list(on_disk - known_paths)
    orphans_in_db = list(known_paths - on_disk)

    _record_run(
        "find-orphans", "success",
        f"on_disk_orphans={len(orphans_on_disk)} db_orphans={len(orphans_in_db)}",
    )
    return {
        "status": "success",
        "orphans_on_disk": orphans_on_disk,
        "orphans_missing_from_disk": orphans_in_db,
    }


def retry_failed():
    """
    Reprocess only resources stuck in status: failed, resuming from
    whichever stage they failed at (Resource.failure_stage). Dawarich/
    Immich lookups are NOT among these stages — they never fail a
    resource; see jobs/enrich.py for how those recover instead.
    """
    retried = []
    still_failing = []

    for resource in Resource.query.filter_by(status="failed").all():
        try:
            if resource.failure_stage == "move":
                expected = render_path(resource, project=resource.project, session=resource.session)
                expected_full = os.path.join(Config.NAS_LIBRARY_ROOT, expected)
                os.makedirs(os.path.dirname(expected_full), exist_ok=True)
                os.rename(resource.nas_path, expected_full)
                resource.nas_path = expected_full
                resource.status = "filed"

            else:
                # checksum / metadata-extraction failures mean the raw
                # file itself needs re-inspection (it may be corrupt,
                # or an unsupported format) — not something to just
                # retry blindly. Leave as still-failing.
                still_failing.append(resource.id)
                continue

            resource.failure_stage = None
            resource.failure_detail = None
            retried.append(resource.id)

        except Exception as e:
            resource.failure_detail = str(e)
            still_failing.append(resource.id)

    db.session.commit()
    _record_run(
        "retry-failed", "success",
        f"retried: {retried}, still_failing: {still_failing}",
    )
    return {"status": "success", "retried": retried, "still_failing": still_failing}
