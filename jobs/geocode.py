"""
Name the places recordings were made (see jobs/photon.py for the lookup and the label rules).

Like the Dawarich/Immich enrichment, this is a self-healing queue rather than a call made while a request waits:
a location whose `place_checked_at` is NULL is queued work. If Photon is down, locations simply stay queued and the
next run (or the timer) names them; nothing fails and nothing needs retrying by hand. A circuit breaker stops a run
early when Photon is clearly unreachable.

  * A location that MOVES (a fresh Dawarich lookup, or a place picked on the map) loses its old name and is queued
    again, but only if it moved more than PLACE_MOVED_METRES: GPS jitter between two lookups must not rename it.
  * A name TYPED by the user is never overwritten by a lookup (place_source "manual"); clearing it queues a lookup.
  * "Nothing found" is an answer too: place_checked_at is set with no name so it isn't asked again.
"""
import logging
from datetime import datetime

from config import Config
from app.extensions import db
from app.geo import distance_m
from app.models import Location, JobRun
from . import providers
from .retry import CircuitBreaker, ServiceUnavailable

log = logging.getLogger(__name__)


def place_moved(old_lat, old_lon, new_lat, new_lon):
    """Has a location moved far enough that its place name no longer applies?"""
    if old_lat is None or old_lon is None or new_lat is None or new_lon is None:
        return True
    return distance_m(old_lat, old_lon, new_lat, new_lon) > Config.PLACE_MOVED_METRES


def reset_place(loc):
    """Forget the place name and queue a fresh lookup."""
    loc.place_name = loc.place_source = loc.place_info = loc.place_checked_at = None


def set_manual_place(loc, name):
    """A name typed by the user (empty clears it and queues a lookup). Returns True if a lookup was queued."""
    name = (name or "").strip()
    if not name:
        reset_place(loc)
        return True
    loc.place_name, loc.place_source, loc.place_info, loc.place_checked_at = name[:200], "manual", None, datetime.utcnow()
    return False


def apply_lookup(loc, result):
    """Store a Photon answer (None = nothing found nearby)."""
    loc.place_checked_at = datetime.utcnow()
    if result:
        loc.place_name, loc.place_source, loc.place_info = result["label"], "photon", result["info"]
    else:
        loc.place_name = loc.place_source = loc.place_info = None


def enqueue_geocode(trigger="location-changed"):
    """Ask the worker to run the sweeper soon. Never raises: naming a place must not break what triggered it."""
    try:
        from .queue import enqueue
        enqueue("geocode-locations", triggered_by=trigger)
    except Exception:  # noqa: BLE001
        db.session.rollback()


def _record_run(status, detail):
    run = JobRun.query.get("geocode-locations") or JobRun(job_name="geocode-locations")
    run.last_run_at = datetime.utcnow()
    run.status = status
    run.log_tail = detail[-4000:]
    db.session.merge(run)
    db.session.commit()


def geocode_locations():
    """Name up to PLACE_BATCH queued locations. Returns a small summary dict."""
    try:
        reverse = providers.get("geocoder")
    except providers.UnknownProvider as e:
        _record_run("error", str(e))
        return {"status": "error", "detail": str(e)}
    if reverse is None:
        _record_run("success", "GEOCODER_PROVIDER is 'none': place names are switched off")
        return {"status": "success", "named": 0, "detail": "provider disabled"}
    breaker = CircuitBreaker()
    named, empty, last_error = 0, 0, None
    ids = [lid for (lid,) in db.session.query(Location.id)
           .filter(Location.place_checked_at.is_(None), Location.lat.isnot(None), Location.lon.isnot(None))
           .order_by(Location.id).limit(Config.PLACE_BATCH).all()]
    for lid in ids:
        if breaker.tripped:
            break
        loc = db.session.get(Location, lid)
        if loc is None or loc.place_checked_at is not None or loc.place_source == "manual":
            continue                                    # changed or removed while we worked
        try:
            result = reverse(loc.lat, loc.lon)
        except ServiceUnavailable as e:
            breaker.record_failure()
            last_error = str(e)
            log.warning("place lookup failed for %s: %s", lid, e)
            continue
        if result == "unconfigured":
            _record_run("success", "no Photon URL is set (Settings page); nothing to do")
            return {"status": "success", "named": 0, "detail": "unconfigured"}
        breaker.record_success()
        db.session.refresh(loc)
        if loc.place_source == "manual" or loc.place_checked_at is not None:
            continue                                    # the user typed a name while we were asking
        apply_lookup(loc, result)
        db.session.commit()
        if result and result["label"]:
            named += 1
        else:
            empty += 1

    remaining = db.session.query(db.func.count(Location.id)).filter(Location.place_checked_at.is_(None), Location.lat.isnot(None)).scalar()
    if last_error and (breaker.tripped or named + empty == 0):
        # Photon is not answering and nothing was achieved: say so (Home shows it), and leave everything queued.
        _record_run("error", f"Photon unreachable{', stopped early' if breaker.tripped else ''}: {last_error}. named={named}, still queued={remaining}")
        return {"status": "error", "named": named, "still_queued": remaining, "detail": last_error}
    if last_error:
        _record_run("partial", f"some lookups failed ({last_error}); named: {named}, still queued: {remaining}")
        return {"status": "partial", "named": named, "still_queued": remaining, "detail": last_error}
    _record_run("success", f"named: {named}, nothing found: {empty}, still queued: {remaining}")
    return {"status": "success", "named": named, "nothing_found": empty, "still_queued": remaining}
