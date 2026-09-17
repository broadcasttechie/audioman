"""
Decoupled from ingest entirely (see jobs/ingest.py docstring). These
two jobs are the actual "queue" for external enrichment: any resource
with captured_at set and *_checked_at still NULL is queued work.
Nothing needs to track outages explicitly — if Dawarich is offline for
a day, resources just accumulate with dawarich_checked_at IS NULL, and
whichever run of this job first finds Dawarich reachable again quietly
catches up on all of them.

Each job runs its own CircuitBreaker: after a few consecutive
failures, it stops trying the rest of the batch (the service is almost
certainly down, not failing per-resource) and lets the next scheduled
run pick up where it left off. Progress commits per-resource, not once
at the end, so a run that gets interrupted partway still keeps what it
found.
"""
from datetime import datetime

from app.extensions import db
from app.models import Resource, FileEvent, Location, TrackPoint, ResourcePhoto, JobRun
from .retry import CircuitBreaker, ServiceUnavailable
from .dawarich import fetch_track_and_pin
from .immich import fetch_photos_for_recording


def _record_run(job_name, status, log_tail=""):
    run = JobRun.query.get(job_name) or JobRun(job_name=job_name)
    run.last_run_at = datetime.utcnow()
    run.status = status
    run.log_tail = log_tail[-4000:]
    db.session.merge(run)
    db.session.commit()


def enrich_locations():
    breaker = CircuitBreaker()
    checked, found = [], []

    candidates = Resource.query.filter(
        Resource.captured_at.isnot(None),
        Resource.dawarich_checked_at.is_(None),
        Resource.status.in_(["pending-review", "filed"]),
    ).all()

    for resource in candidates:
        if breaker.tripped:
            break

        try:
            track_points, pin = fetch_track_and_pin(resource.captured_at, resource.duration_seconds)
            breaker.record_success()
        except ServiceUnavailable as e:
            breaker.record_failure()
            db.session.add(FileEvent(
                resource_id=resource.id, event_type="failed",
                detail=f"dawarich unreachable, will retry next run: {e}",
            ))
            db.session.commit()
            continue  # dawarich_checked_at stays NULL - retried next run

        # Reached Dawarich successfully, even if it had nothing for this
        # window — mark checked so we stop querying it forever.
        resource.dawarich_checked_at = datetime.utcnow()
        checked.append(resource.id)

        for point in track_points:
            db.session.add(TrackPoint(
                resource_id=resource.id, recorded_at=point["timestamp"],
                lat=point["lat"], lon=point["lon"],
            ))
        if pin:
            loc = resource.location or Location(resource_id=resource.id)
            loc.lat, loc.lon, loc.source = pin["lat"], pin["lon"], "dawarich-auto"
            db.session.add(loc)
            found.append(resource.id)

        db.session.commit()

    remaining = len(candidates) - len(checked)
    status = "partial" if breaker.tripped else "success"
    _record_run(
        "enrich-locations", status,
        f"checked: {len(checked)}, found: {len(found)}, still_queued: {remaining}",
    )
    return {"status": status, "checked": checked, "found": found, "still_queued": remaining}


def enrich_photos():
    breaker = CircuitBreaker()
    checked, found = [], []

    candidates = Resource.query.filter(
        Resource.captured_at.isnot(None),
        Resource.immich_checked_at.is_(None),
        Resource.status.in_(["pending-review", "filed"]),
    ).all()

    for resource in candidates:
        if breaker.tripped:
            break

        try:
            photos = fetch_photos_for_recording(resource.captured_at, resource.duration_seconds)
            breaker.record_success()
        except ServiceUnavailable as e:
            breaker.record_failure()
            db.session.add(FileEvent(
                resource_id=resource.id, event_type="failed",
                detail=f"immich unreachable, will retry next run: {e}",
            ))
            db.session.commit()
            continue

        resource.immich_checked_at = datetime.utcnow()
        checked.append(resource.id)

        for photo in photos:
            exists = ResourcePhoto.query.filter_by(
                resource_id=resource.id, immich_asset_id=photo["immich_asset_id"],
            ).first()
            if exists:
                continue
            db.session.add(ResourcePhoto(
                resource_id=resource.id,
                immich_asset_id=photo["immich_asset_id"],
                taken_at=photo["taken_at"],
            ))
        if photos:
            found.append(resource.id)

        db.session.commit()

    remaining = len(candidates) - len(checked)
    status = "partial" if breaker.tripped else "success"
    _record_run(
        "enrich-photos", status,
        f"checked: {len(checked)}, found: {len(found)}, still_queued: {remaining}",
    )
    return {"status": status, "checked": checked, "found": found, "still_queued": remaining}
