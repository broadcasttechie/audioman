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
from . import providers


def _record_run(job_name, status, log_tail=""):
    run = JobRun.query.get(job_name) or JobRun(job_name=job_name)
    run.last_run_at = datetime.utcnow()
    run.status = status
    run.log_tail = log_tail[-4000:]
    db.session.merge(run)
    db.session.commit()


def apply_location_result(resource, track_points, pin):
    """
    Store a Dawarich result for one resource. Shared by the scheduled queue and the
    manual refresh so both behave the same:
      * the fetched track REPLACES any earlier one (a re-run must not duplicate points);
      * a location the user set by hand is NEVER overwritten by an automatic lookup.
    Returns {"pin_stored": bool, "kept_manual": bool}.
    """
    TrackPoint.query.filter_by(resource_id=resource.id).delete(synchronize_session=False)
    for point in track_points:
        db.session.add(TrackPoint(
            resource_id=resource.id, recorded_at=point["timestamp"],
            lat=point["lat"], lon=point["lon"],
        ))

    if not pin:
        return {"pin_stored": False, "kept_manual": False}
    loc = resource.location
    if loc is not None and loc.source == "manual":
        return {"pin_stored": False, "kept_manual": True}
    loc = loc or Location(resource_id=resource.id)
    from .geocode import place_moved, reset_place
    if place_moved(loc.lat, loc.lon, pin["lat"], pin["lon"]):
        reset_place(loc)          # a different place now: its old name no longer applies
    loc.lat, loc.lon, loc.source = pin["lat"], pin["lon"], f"{providers.provider_name('location')}-auto"
    db.session.add(loc)
    return {"pin_stored": True, "kept_manual": False}


def _provider_or_report(kind, job_name):
    """The configured provider, or (None, result) after recording why the job did nothing."""
    try:
        fn = providers.get(kind)
    except providers.UnknownProvider as e:
        _record_run(job_name, "error", str(e))
        return None, {"status": "error", "detail": str(e)}
    if fn is None:
        _record_run(job_name, "success", f"{providers.CONFIG_KEYS[kind]} is 'none': this lookup is switched off")
        return None, {"status": "success", "checked": [], "found": [], "still_queued": 0, "detail": "provider disabled"}
    return fn, None


def enrich_locations():
    fetch_track_and_pin, skipped = _provider_or_report("location", "enrich-locations")
    if skipped:
        return skipped
    breaker = CircuitBreaker()
    checked, found = [], []

    candidates = Resource.query.filter(
        Resource.captured_at.isnot(None),
        Resource.captured_at_precision == "exact",   # a fuzzy time must not be looked up (PLAN 18.5c)
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

        if apply_location_result(resource, track_points, pin)["pin_stored"]:
            found.append(resource.id)

        db.session.commit()

    if found:
        from .geocode import enqueue_geocode
        enqueue_geocode("enrich-locations")       # name the places just found
    remaining = len(candidates) - len(checked)
    status = "partial" if breaker.tripped else "success"
    _record_run(
        "enrich-locations", status,
        f"checked: {len(checked)}, found: {len(found)}, still_queued: {remaining}",
    )
    return {"status": status, "checked": checked, "found": found, "still_queued": remaining}


def enrich_photos():
    fetch_photos_for_recording, skipped = _provider_or_report("photos", "enrich-photos")
    if skipped:
        return skipped
    breaker = CircuitBreaker()
    checked, found = [], []

    candidates = Resource.query.filter(
        Resource.captured_at.isnot(None),
        Resource.captured_at_precision == "exact",
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
