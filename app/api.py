from flask import Blueprint, jsonify, request, send_file, abort
import os
import re
import uuid
from datetime import date, datetime, timedelta
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from .extensions import db
from .models import resource_tags, Category, Resource, RecordingSession, Project, Tag, Location, JobRun, FileEvent, Clip, ResourcePhoto, Export, Setting, TrackPoint, RecorderProfile
from .settings import get_config, set_config, settings_snapshot, SECRET_KEYS
from config import Config
from .timeutil import parse_to_utc_naive, to_utc_iso
from . import categories as cats

bp = Blueprint("api", __name__)


# --- Resources (the review queue is just a filtered view over this) ---

def _filtered_resources(args, default_statuses=None):
    """The query behind the Library and the map: every filter the list endpoint accepts, in one place
    (status, role, category, session, project, tag, q). `default_statuses` applies when `status` is absent."""
    query = Resource.query

    statuses = [s for s in (args.get("status") or "").split(",") if s] if args.get("status") is not None else (default_statuses or [])
    if statuses:
        query = query.filter(Resource.status.in_(statuses))

    roles = args.get("role")
    if roles == "all":
        pass
    elif roles:
        query = query.filter(Resource.role.in_([x for x in roles.split(",") if x]))
    else:
        # Sidecars and project files ride along with their audio; they are not listed as recordings.
        query = query.filter(Resource.role.notin_(("sidecar", "project-file")))
    category = args.get("category")
    if category:
        query = query.filter(Resource.category == category)

    session_id = args.get("session_id")
    if session_id == "none":
        query = query.filter(Resource.session_id.is_(None))
    elif session_id:
        query = query.filter(Resource.session_id == session_id)
    project_id = args.get("project_id")
    if project_id == "none":
        query = query.filter(Resource.project_id.is_(None))
    elif project_id:
        query = query.filter(Resource.project_id == project_id)

    for tag_name in args.getlist("tag"):
        query = query.filter(Resource.tags.any(Tag.name == tag_name))

    q = (args.get("q") or "").strip()
    if q:
        # Search what people actually remember: the name, their notes, the session or project it belongs to, a tag.
        query = query.filter(db.or_(
            Resource.filename.icontains(q, autoescape=True),
            Resource.notes.icontains(q, autoescape=True),
            Resource.session.has(RecordingSession.name.icontains(q, autoescape=True)),
            Resource.project.has(Project.name.icontains(q, autoescape=True)),
            Resource.tags.any(Tag.name.icontains(q, autoescape=True)),
            Resource.location.has(Location.place_name.icontains(q, autoescape=True)),
        ))

    return query


@bp.get("/resources")
def list_resources():
    """
    Paginated — this is explicitly a "growing collection" app, so an
    unbounded `SELECT *` here would eventually mean a multi-MB JSON
    response and a slow query. limit caps at 200/defaults to 50;
    offset for simple pagination (fine at this scale — a cursor would
    only matter at a size this app isn't going to reach).

    Filters (all optional, combined with AND): `status` (comma-separated
    for several), `category`, `project_id` (or `none` for no project),
    `tag` (repeatable -- must carry every one named), `q` (filename
    substring, case-insensitive). `sort=captured` orders by captured_at,
    newest first, recordings with no timestamp last; the default is
    newest-added first.
    """
    limit = min(int(request.args.get("limit", 50)), 200)
    offset = int(request.args.get("offset", 0))

    query = _filtered_resources(request.args)

    total = query.count()

    if request.args.get("sort") == "captured":
        order = (Resource.captured_at.desc().nullslast(), Resource.created_at.desc(), Resource.id)
    else:
        order = (Resource.created_at.desc(), Resource.id)  # id: stable paging on ties

    # Eager-load what _resource_to_dict touches so a page of results is a
    # handful of queries, not several per row.
    resources = (
        query.options(
            selectinload(Resource.tags),
            selectinload(Resource.location),
            selectinload(Resource.photos),
        )
        .order_by(*order).offset(offset).limit(limit).all()
    )
    return jsonify({
        "total": total,
        "limit": limit,
        "offset": offset,
        "resources": [_resource_to_dict(r) for r in resources],
    })


@bp.get("/resources/<resource_id>")
def get_resource(resource_id):
    r = Resource.query.get_or_404(resource_id)
    return jsonify(_resource_to_dict(r))


@bp.patch("/resources/<resource_id>")
def update_resource(resource_id):
    r = Resource.query.get_or_404(resource_id)
    body, status = _update_resource(r, request.get_json() or {})
    if status >= 400:
        db.session.rollback()
    return jsonify(body), status


def _update_resource(r, data):
    """
    Apply a PATCH to one resource. Returns (body_dict, http_status). Shared by the single-resource
    PATCH and the batch endpoint (the review queue's multi-select) so both obey the same rules.
    On an error status nothing has been committed; callers roll back the session.
    """

    if "category" in data and data["category"] is not None and data["category"] != r.category:
        # An archived category keeps its existing files but can't be given to new ones.
        allowed = cats.active_slugs()
        if data["category"] not in allowed:
            return {"error": f"category must be one of {sorted(allowed)}"}, 400

    if "status" in data and data["status"] not in ("pending-review", "filed", "archived"):
        # 'filing' and 'failed' are set by the system (jobs/filing.py), never by a client.
        return {"error": "status must be 'pending-review', 'filed' or 'archived'"}, 400
    filing_now = data.get("status") == "filed" and r.status not in ("filed", "filing")
    need_filing_job = False
    if filing_now:
        # Refuse before changing anything, so a dropped NAS mount can't leave a
        # half-applied edit behind.
        from jobs.nas import nas_status
        nas_ok, nas_reason = nas_status()
        if not nas_ok:
            return {"error": "The NAS is not available, so nothing was changed.",
                            "detail": nas_reason}, 503

    if data.get("use_suggested_date"):
        # Confirm the date a "suggest" recorder profile read from the filename.
        if not r.suggested_captured_at:
            return {"error": "this recording has no suggested date"}, 400
        data["captured_at"] = r.suggested_captured_at
        data["captured_at_source"] = "filename"
        # A date-only filename can only ever be an approximate date.
        data.setdefault("captured_at_precision",
                        "exact" if (r.filename_info or {}).get("time_known", True) else "approximate")

    if "captured_at_precision" in data and data["captured_at_precision"] not in ("exact", "approximate"):
        return {"error": "captured_at_precision must be 'exact' or 'approximate' "
                                 "(clear captured_at instead to make the date unknown)"}, 400

    if "captured_at" in data:
        # Time contract (app/timeutil.py): Z/offset strings become UTC; a string
        # with no offset is taken as UTC; null clears it.
        if data["captured_at"] is None:
            data["captured_at"] = None
        else:
            try:
                data["captured_at"] = parse_to_utc_naive(data["captured_at"])
            except ValueError:
                return {"error": "captured_at must be an ISO 8601 date-time, e.g. 2026-09-17T08:11:24Z"}, 400

    tags_to_set = None
    if "tags" in data:
        if not isinstance(data["tags"], list) or not all(isinstance(t, str) for t in data["tags"]):
            return {"error": "tags must be a list of tag id strings"}, 400
        wanted = list(dict.fromkeys(data["tags"]))  # de-duplicated, order kept
        found = {t.id: t for t in Tag.query.filter(Tag.id.in_(wanted)).all()} if wanted else {}
        unknown = [t for t in wanted if t not in found]
        if unknown:
            # A stale id used to put None into r.tags and crash on commit.
            return {"error": f"unknown tag id(s): {unknown}"}, 400
        tags_to_set = [found[t] for t in wanted]

    structure = _resolve_structure(r, data)
    if isinstance(structure, tuple):
        return structure  # (error body, status)

    old_date, old_precision = r.captured_at, r.captured_at_precision
    for field in ("category", "captured_at_source"):
        if field in data:
            setattr(r, field, data[field])
    if structure:
        r.session_id, r.project_id = structure["session_id"], structure["project_id"]
        for field in ("role", "derived_from_id", "notes", "track_label"):
            if field in structure:
                setattr(r, field, structure[field])

    if "captured_at" in data:
        r.captured_at = data["captured_at"]
        # A date implies exact (typed by hand) unless the caller says approximate; no date is unknown.
        r.captured_at_precision = (data.get("captured_at_precision", "exact")
                                   if r.captured_at is not None else "unknown")
        r.suggested_captured_at = None
    elif "captured_at_precision" in data:
        if r.captured_at is None:
            return {"error": "there is no date to mark as exact or approximate"}, 400
        r.captured_at_precision = data["captured_at_precision"]

    if (r.captured_at, r.captured_at_precision) != (old_date, old_precision):
        # Anything looked up from the old time (track, auto location, companion photos) is
        # now stale, and an approximate/unknown time must not be looked up at all. Derived
        # data only: a location the user set by hand is kept. Enrichment re-queues itself.
        _clear_derived_by_time(r)

    if tags_to_set is not None:
        r.tags = tags_to_set

    need_place_job = False
    if "location" in data:
        from jobs.geocode import place_moved, reset_place
        loc = r.location or Location(resource_id=r.id)
        new_lat, new_lon = data["location"].get("lat"), data["location"].get("lon")
        if place_moved(loc.lat, loc.lon, new_lat, new_lon):
            reset_place(loc)               # moved: the old name no longer applies, and a new one is looked up
            need_place_job = True
        loc.lat, loc.lon = new_lat, new_lon
        loc.source = data["location"].get("source", "manual")
        db.session.add(loc)

    if "place_name" in data:
        from jobs.geocode import set_manual_place
        if data["place_name"] is not None and not isinstance(data["place_name"], str):
            return {"error": "place_name must be text"}, 400
        if r.location is None:
            return {"error": "there is no location to name yet; set one first"}, 400
        need_place_job = set_manual_place(r.location, data["place_name"]) or need_place_job

    if filing_now:
        if not r.category:
            return {"error": "category is required before filing"}, 400
        if not r.staging_path or not os.path.exists(r.staging_path):
            return {"error": "the file is no longer in staging, so it can't be filed"}, 409
        # The copy runs in the background (jobs/filing.py): it can take longer than a web request.
        r.status = "filing"
        r.failure_stage = r.failure_detail = None
        need_filing_job = True
    elif "status" in data:
        r.status = data["status"]

    db.session.commit()
    if need_filing_job:
        from jobs.queue import enqueue
        enqueue("file-resources", triggered_by="filing")
    if need_place_job:
        from jobs.geocode import enqueue_geocode
        enqueue_geocode()
    return _resource_to_dict(r), 200


@bp.post("/resources/batch")
def batch_update_resources():
    """
    Apply one change to many resources at once (the review queue's multi-select).
    Body: {"ids": [...], "patch": {...same fields as PATCH...}, "tags_add": [tag ids]}.
    `tags_add` adds to each resource's existing tags instead of replacing them; `use_suggested_date`
    applies each resource's own filename suggestion. Each resource is applied on its own, so one
    that can't take the change (no suggestion, unknown session, NAS down...) doesn't stop the rest;
    the response lists every outcome. `status: "filed"` queues the background filing job.
    Fields that only make sense per file (notes, track_label, derived_from_id, role) are refused.
    """
    data = request.get_json() or {}
    ids, patch, tags_add = data.get("ids"), data.get("patch") or {}, data.get("tags_add")
    if not isinstance(ids, list) or not ids or len(ids) > 500 or not all(isinstance(i, str) for i in ids):
        return jsonify({"error": "ids must be a list of 1 to 500 resource ids"}), 400
    if not isinstance(patch, dict):
        return jsonify({"error": "patch must be an object"}), 400
    forbidden = [k for k in ("notes", "track_label", "derived_from_id", "role") if k in patch]
    if forbidden:
        return jsonify({"error": f"{forbidden} can only be set on one file at a time"}), 400
    if tags_add is not None and (not isinstance(tags_add, list) or not all(isinstance(t, str) for t in tags_add)):
        return jsonify({"error": "tags_add must be a list of tag id strings"}), 400
    if not patch and not tags_add:
        return jsonify({"error": "nothing to apply"}), 400

    results = []
    for rid in dict.fromkeys(ids):
        r = db.session.get(Resource, rid)
        if r is None:
            results.append({"id": rid, "ok": False, "status": 404, "error": "not found"})
            continue
        item = dict(patch)
        if tags_add:
            item["tags"] = list(dict.fromkeys([t.id for t in r.tags] + tags_add))
        body, status = _update_resource(r, item)
        if status >= 400:
            db.session.rollback()
            results.append({"id": rid, "ok": False, "status": status, "error": body.get("error"), "detail": body.get("detail")})
        else:
            results.append({"id": rid, "ok": True, "status": status, "resource_status": body.get("status")})
    ok = sum(1 for x in results if x["ok"])
    return jsonify({"results": results, "ok_count": ok, "error_count": len(results) - ok})


ROLES = ("original", "edit", "export", "sidecar", "project-file")


def _resolve_structure(r, data):
    """
    Validate the project / session / role / notes part of a PATCH. Returns None if the request
    doesn't touch them, a dict of values to apply, or (error_body, status).

    Rules: a file's project is always its session's project; giving a session sets the project
    from it (and a project that contradicts the session is refused); moving a file to another
    project takes it out of its old session; an edit points at an existing, different file and
    the chain can't loop.
    """
    keys = ("session_id", "project_id", "role", "derived_from_id", "notes", "track_label")
    if not any(k in data for k in keys):
        return None
    out = {}

    session_id, project_id = r.session_id, r.project_id
    if "project_id" in data and data["project_id"] is not None and db.session.get(Project, data["project_id"]) is None:
        return {"error": "unknown project"}, 400

    if data.get("session_id") is not None:
        session = db.session.get(RecordingSession, data["session_id"])
        if session is None:
            return {"error": "unknown session"}, 400
        if "project_id" in data and data["project_id"] != session.project_id:
            return {"error": "that session belongs to a different project; change the session "
                                     "or clear it when moving the file to another project"}, 400
        session_id, project_id = session.id, session.project_id
    else:
        if "session_id" in data:              # explicitly cleared
            session_id = None
        if "project_id" in data:
            project_id = data["project_id"]
            if session_id is not None and r.session.project_id != project_id:
                session_id = None             # moved to another project: leave the old session
    out["session_id"], out["project_id"] = session_id, project_id

    if "role" in data:
        if data["role"] not in ROLES:
            return {"error": f"role must be one of {list(ROLES)}"}, 400
        out["role"] = data["role"]
    if "derived_from_id" in data:
        target = data["derived_from_id"]
        if target is not None:
            if target == r.id:
                return {"error": "a file can't be an edit of itself"}, 400
            seen, cursor = {r.id}, db.session.get(Resource, target)
            if cursor is None:
                return {"error": "unknown file for derived_from_id"}, 400
            while cursor is not None:  # no loops: walk up the chain from the proposed original
                if cursor.id in seen:
                    return {"error": "that would make the edits form a loop"}, 400
                seen.add(cursor.id)
                cursor = cursor.derived_from
            if data.get("role", r.role) == "original":
                out["role"] = "edit"  # pointing at an original makes this an edit of it
        out["derived_from_id"] = target
    for field in ("notes", "track_label"):
        if field in data:
            if data[field] is not None and not isinstance(data[field], str):
                return {"error": f"{field} must be text"}, 400
            out[field] = data[field]
    return out


def _clear_derived_by_time(resource):
    TrackPoint.query.filter_by(resource_id=resource.id).delete(synchronize_session=False)
    ResourcePhoto.query.filter_by(resource_id=resource.id).delete(synchronize_session=False)
    if resource.location is not None and resource.location.source != "manual":
        db.session.delete(resource.location)
    resource.dawarich_checked_at = None
    resource.immich_checked_at = None


def _resource_to_dict(r: Resource):
    d = {
        "id": r.id,
        "filename": r.filename,
        "category": r.category,
        "project_id": r.project_id,
        "session_id": r.session_id,
        "session": {"id": r.session.id, "name": r.session.name} if r.session else None,
        "role": r.role or "original",
        "derived_from_id": r.derived_from_id,
        "track_label": r.track_label,
        "notes": r.notes,
        "size_bytes": r.size_bytes,
        "has_waveform": r.waveform_at is not None,
        "has_preview": r.preview_at is not None,
        "waveform_error": r.waveform_error,
        "preview_error": r.preview_error,
        "status": r.status,
        "captured_at": to_utc_iso(r.captured_at),
        "captured_at_source": r.captured_at_source,
        "captured_at_precision": r.captured_at_precision or ("exact" if r.captured_at else "unknown"),
        "suggested_captured_at": to_utc_iso(r.suggested_captured_at),
        "filename_info": r.filename_info,
        "duration_seconds": r.duration_seconds,
        "nas_path": r.nas_path,
        "tags": [t.name for t in r.tags],
        "location": (
            {"lat": r.location.lat, "lon": r.location.lon, "source": r.location.source,
             "place_name": r.location.place_name, "place_source": r.location.place_source}
            if r.location else None
        ),
        # An existence check, not len(r.track_points): that would load every
        # GPS point of every row just to answer yes/no, which matters once
        # this is called for a page of results.
        "has_track": TrackPoint.query.filter_by(resource_id=r.id).first() is not None,
        "has_photos": len(r.photos) > 0,
    }
    if r.status == "failed":
        # Only surfaced when relevant - a UI showing every resource in
        # a list doesn't need this on every row, just the failed ones.
        d["failure_stage"] = r.failure_stage
        d["failure_detail"] = r.failure_detail
    return d


# --- Map: GPS path for a resource ---

@bp.get("/resources/<resource_id>/track")
def get_track(resource_id):
    """
    Returns the cached GPS path for a resource, each point with an
    `offset_seconds` already computed relative to captured_at so the
    frontend can seek the audio player directly without doing its own
    time math. Empty list if there's no track (no captured_at at
    ingest time, or Dawarich had nothing for that window) — the
    frontend should fall back to showing just the single pin (if any)
    with no path/markers.
    """
    resource = Resource.query.get_or_404(resource_id)
    if not resource.captured_at:
        return jsonify([])

    return jsonify([
        {
            "lat": p.lat,
            "lon": p.lon,
            "recorded_at": to_utc_iso(p.recorded_at),
            "offset_seconds": (p.recorded_at - resource.captured_at).total_seconds(),
        }
        for p in resource.track_points
    ])


# --- Audio streaming for the waveform player ---

@bp.get("/resources/<resource_id>/audio")
def stream_audio(resource_id):
    """
    Serves the raw audio file for the waveform player (e.g.
    wavesurfer.js). conditional=True makes Flask handle Range
    requests, which browsers/players use for seeking without
    downloading the whole file up front.
    """
    resource = Resource.query.get_or_404(resource_id)
    path = resource.nas_path or resource.staging_path
    if not path or not os.path.exists(path):
        if path:
            # A missing file under the NAS root is far more likely an unmounted
            # NAS than a deleted recording: say so instead of a misleading 404.
            from jobs.nas import is_nas_path, nas_status
            nas_ok, nas_reason = nas_status()
            if is_nas_path(path) and not nas_ok:
                abort(503, description=f"NAS unavailable: {nas_reason}")
        abort(404, description="audio file not found on disk")
    return send_file(path, conditional=True)


def _provider_or_error(kind):
    """(function, None) for the configured provider, or (None, (body, status)) explaining why there isn't one."""
    from jobs import providers
    try:
        fn = providers.get(kind)
    except providers.UnknownProvider as e:
        return None, ({"error": str(e)}, 400)
    if fn is None:
        return None, ({"error": f"This lookup is switched off ({providers.CONFIG_KEYS[kind]} is 'none')."}, 400)
    return fn, None


def _derived_response(r, kind):
    """Serve a generated waveform/preview, or say it is on its way (202) and make sure the sweeper runs."""
    from jobs import previews
    if kind == "waveform":
        ready, err = r.waveform_at, r.waveform_error
        path, mimetype = previews.waveform_path(r.checksum), "application/octet-stream"
    else:
        ready, err = r.preview_at, r.preview_error
        path, mimetype = previews.preview_path(r.checksum), "audio/mp4"
    if ready and os.path.exists(path):
        # Content-addressed by checksum, so a cached copy can never be stale. send_file handles Range.
        return send_file(path, mimetype=mimetype, conditional=True, max_age=3600)
    if ready:  # marked generated but the cache file is gone: heal by regenerating
        if kind == "waveform":
            r.waveform_at = None
        else:
            r.preview_at = None
        db.session.commit()
    elif err:
        return jsonify({"status": "failed", "error": err}), 500
    from jobs.queue import enqueue
    enqueue("generate-previews", triggered_by="requested")
    return jsonify({"status": "generating"}), 202


@bp.get("/resources/<resource_id>/edit-candidates")
def edit_candidates(resource_id):
    """
    For a file whose name says it is an edited version ("...-EDIT"), which originals might it be an edit of?
    Same Inbox folder and the same leading timestamp in the name (e.g. 250424-121237), or failing that the same
    exact capture time. Only ever a suggestion: linking is the user's decision (PATCH derived_from_id).
    """
    from jobs.filename_patterns import strip_name
    r = Resource.query.get_or_404(resource_id)
    info = r.filename_info or {}
    if not info.get("is_edit") or r.derived_from_id:
        return jsonify([])
    m = re.match(r"^(\d{6}-\d{6})", strip_name(r.filename))
    query = Resource.query.filter(Resource.id != r.id, Resource.role == "original",
                                  Resource.status.in_(("pending-review", "filing", "filed")))
    if m:
        query = query.filter(Resource.filename.ilike(m.group(1).replace("%", "") + "%"))
    elif r.captured_at is not None and r.captured_at_precision == "exact":
        query = query.filter(Resource.captured_at == r.captured_at)
    else:
        return jsonify([])
    out = []
    for c in query.order_by(Resource.filename).limit(20).all():
        cinfo = c.filename_info or {}
        if cinfo.get("is_edit") or (cinfo.get("folder") or None) != (info.get("folder") or None):
            continue
        out.append({"id": c.id, "filename": c.filename, "status": c.status})
    return jsonify(out[:5])


@bp.get("/resources/<resource_id>/waveform")
def get_waveform(resource_id):
    """The peaks as audiowaveform's native binary `.dat` (its header carries sample rate and samples per pixel)."""
    return _derived_response(Resource.query.get_or_404(resource_id), "waveform")


@bp.get("/resources/<resource_id>/preview")
def get_preview(resource_id):
    """A compressed AAC listening copy, cheap to stream to a phone. Supports HTTP Range."""
    return _derived_response(Resource.query.get_or_404(resource_id), "preview")


@bp.post("/resources/<resource_id>/previews/regenerate")
def regenerate_previews(resource_id):
    """Throw away the waveform and preview and rebuild them (also the retry after a failure)."""
    from jobs import previews
    from jobs.queue import enqueue
    r = Resource.query.get_or_404(resource_id)
    previews.delete_cache(r.checksum)
    r.waveform_at = r.preview_at = r.waveform_error = r.preview_error = None
    db.session.commit()
    enqueue("generate-previews", triggered_by="regenerate")
    return jsonify({"status": "queued"}), 202


# --- Home: one call for the overview page ---

@bp.get("/overview")
def overview():
    from jobs.nas import nas_status
    recordings = Resource.role.notin_(("sidecar", "project-file"))
    by_status = dict(db.session.query(Resource.status, db.func.count(Resource.id)).filter(recordings).group_by(Resource.status).all())
    filed = db.session.query(db.func.count(Resource.id), db.func.coalesce(db.func.sum(Resource.duration_seconds), 0),
                             db.func.coalesce(db.func.sum(Resource.size_bytes), 0)).filter(recordings, Resource.status == "filed").one()

    recent_projects = (
        db.session.query(Project, db.func.max(Resource.created_at), db.func.count(Resource.id))
        .outerjoin(Resource, db.and_(Resource.project_id == Project.id, recordings))
        .group_by(Project.id).order_by(db.func.max(Resource.created_at).desc().nullslast(), Project.created_at.desc())
        .limit(6).all())
    recent_files = (Resource.query.filter(recordings, Resource.status == "filed")
                    .order_by(Resource.created_at.desc()).limit(6).all())
    counts = _category_counts()
    cats.ensure_default_categories()
    categories = [{"slug": c.slug, "label": c.label, "file_count": counts.get(c.slug, 0)}
                  for c in Category.query.filter(Category.archived.is_(False)).order_by(Category.sort_order).all()]

    nas_ok, nas_reason = nas_status()
    # Only errors from the last two days are "current"; an old last result of a job that no longer runs shouldn't nag forever.
    recent = datetime.utcnow() - timedelta(days=2)
    job_errors = [{"job": r.job_name, "at": to_utc_iso(r.last_run_at), "detail": (r.log_tail or "")[:160]}
                  for r in JobRun.query.filter(JobRun.status == "error", JobRun.last_run_at > recent).order_by(JobRun.job_name).all()]
    return jsonify({
        "counts": {"pending_review": by_status.get("pending-review", 0), "filing": by_status.get("filing", 0),
                   "failed": by_status.get("failed", 0), "filed": by_status.get("filed", 0),
                   "archived": by_status.get("archived", 0)},
        "library": {"files": filed[0], "hours": round(float(filed[1]) / 3600, 1), "bytes": int(filed[2])},
        "categories": categories,
        "recent_projects": [{"id": p.id, "name": p.name, "slug": p.slug, "file_count": n, "last_activity": to_utc_iso(last)}
                            for p, last, n in recent_projects],
        "recent_files": [{"id": r.id, "filename": r.filename, "category": r.category,
                          "captured_at": to_utc_iso(r.captured_at), "duration_seconds": r.duration_seconds} for r in recent_files],
        "health": {"nas": {"ok": nas_ok, "reason": nas_reason}, "job_errors": job_errors},
    })


# --- Reclaimable Drive space: ingested originals that are safely on the NAS ---

BACKUP_ACK_KEY = "BACKUP_ACKNOWLEDGED"


def _reclaimable_rows():
    """Filed files that came from the Drive Inbox and whose NAS copy exists. Their original is still in
    Inbox/_processed (the app can't delete it on a personal Drive), taking Drive space until the user does."""
    rows = []
    for r in (Resource.query.filter(Resource.status == "filed", Resource.drive_inbox_path.isnot(None), Resource.nas_path.isnot(None))
              .order_by(Resource.created_at).all()):
        if not os.path.exists(r.nas_path):
            continue   # not on the NAS: never offer it (this is also what makes the view safe)
        size = r.size_bytes if r.size_bytes is not None else os.path.getsize(r.nas_path)
        rows.append({"id": r.id, "filename": r.filename, "size_bytes": size, "role": r.role or "original",
                     "drive_path": f"{Config.DRIVE_INBOX_PATH}/_processed/{r.drive_inbox_path}"})
    return rows


@bp.get("/reclaimable")
def reclaimable():
    """
    The originals in Drive `Inbox/_processed` that can be deleted to free Drive space, with sizes and a total.
    The app has no backup of its own, so it lists them only after the user has confirmed they back up the NAS
    themselves (PUT /reclaimable/ack); until then only the count and total are shown. `?check_drive=1` also
    asks Drive which ones are still there, hiding those already deleted.
    """
    from jobs.nas import nas_status
    nas_ok, nas_reason = nas_status()
    if not nas_ok:
        return jsonify({"error": "The NAS is not available, so nothing can be confirmed as safely copied.",
                        "detail": nas_reason}), 503
    rows = _reclaimable_rows()
    drive_checked = False
    if request.args.get("check_drive"):
        try:
            import json as _json
            import subprocess
            out = subprocess.run(["rclone", "lsjson", "-R", "--files-only", f"{Config.RCLONE_DRIVE_REMOTE}:{Config.DRIVE_INBOX_PATH}/_processed"],
                                 capture_output=True, text=True, check=True, timeout=Config.RCLONE_LIST_TIMEOUT_SECONDS)
            present = {e["Path"] for e in _json.loads(out.stdout)}
            rows = [x for x in rows if x["drive_path"].split("/_processed/", 1)[1] in present]
            drive_checked = True
        except Exception:  # noqa: BLE001 -- Drive being unreachable must not break the page
            drive_checked = False
    ack = db.session.get(Setting, BACKUP_ACK_KEY)
    total = sum(x["size_bytes"] for x in rows)
    body = {"acknowledged": bool(ack and ack.value), "acknowledged_at": ack.value if ack else None,
            "count": len(rows), "total_bytes": total, "drive_checked": drive_checked}
    if body["acknowledged"]:
        body["items"] = sorted(rows, key=lambda x: -x["size_bytes"])
    return jsonify(body)


@bp.put("/reclaimable/ack")
def reclaimable_ack():
    """Record (or withdraw) 'I back up the NAS share myself'. The backup is the user's, outside this app."""
    yes = (request.get_json() or {}).get("acknowledged")
    if not isinstance(yes, bool):
        return jsonify({"error": "acknowledged must be true or false"}), 400
    row = db.session.get(Setting, BACKUP_ACK_KEY) or Setting(key=BACKUP_ACK_KEY)
    row.value = (datetime.utcnow().isoformat() + "Z") if yes else ""
    db.session.merge(row)
    db.session.commit()
    return jsonify({"acknowledged": yes})


# --- Maps ---

@bp.get("/map/config")
def map_config():
    """Where the browser fetches map tiles from (the map itself is drawn client-side, app/static/map.js)."""
    return jsonify({"tile_url": Config.MAP_TILE_URL, "attribution": Config.MAP_ATTRIBUTION, "max_zoom": Config.MAP_MAX_ZOOM})


@bp.get("/map/pins")
def map_pins():
    """
    Every recording that has a location, for the all-recordings map. Takes the same filters as the Library
    (`category`, `project_id`, `session_id`, `tag`, `q`, `status`); with no `status` it covers filed, being-filed
    and still-to-review recordings. Also reports how many matching recordings have NO location, so the map can
    say what it is leaving out (only recordings with an exact time are looked up automatically).
    """
    base = _filtered_resources(request.args, default_statuses=["filed", "filing", "pending-review"])
    located = base.join(Location, Location.resource_id == Resource.id).filter(Location.lat.isnot(None), Location.lon.isnot(None))
    total = located.count()
    rows = (located.with_entities(Resource.id, Resource.filename, Resource.category, Resource.captured_at, Resource.duration_seconds,
                                  Location.lat, Location.lon, Location.source, Location.place_name)
            .order_by(Resource.captured_at.desc().nullslast(), Resource.id).limit(Config.MAP_MAX_PINS).all())
    unlocated = base.filter(~Resource.location.has()).count()
    return jsonify({
        "pins": [{"id": r.id, "filename": r.filename, "category": r.category, "captured_at": to_utc_iso(r.captured_at),
                  "duration_seconds": r.duration_seconds, "lat": r.lat, "lon": r.lon, "source": r.source,
                  "place_name": r.place_name} for r in rows],
        "total": total, "truncated": total > len(rows), "unlocated": unlocated,
    })


@bp.post("/resources/<resource_id>/place/lookup")
def lookup_place(resource_id):
    """Look up the place name for this recording's location now (the button next to the name). Replaces a typed
    name too, because it was asked for explicitly. 503 with the reason if Photon can't be reached."""
    from jobs.geocode import apply_lookup
    from jobs.retry import ServiceUnavailable
    r = Resource.query.get_or_404(resource_id)
    reverse, problem = _provider_or_error("geocoder")
    if problem:
        return jsonify(problem[0]), problem[1]
    if r.location is None or r.location.lat is None:
        return jsonify({"error": "this recording has no location to name"}), 400
    try:
        result = reverse(r.location.lat, r.location.lon, max_attempts=1)
    except ServiceUnavailable as e:
        return jsonify({"error": f"The place-name service is not available: {e}"}), 503
    if result == "unconfigured":
        return jsonify({"error": "No place-name service URL is set. Add one on the Settings page."}), 400
    apply_lookup(r.location, result)
    db.session.commit()
    return jsonify({"place_name": r.location.place_name, "place_source": r.location.place_source, "found": bool(r.location.place_name)})


@bp.get("/nas/status")
def nas_health():
    """Whether the library mount is live (see jobs/nas.py)."""
    from jobs.nas import nas_status
    ok, reason = nas_status()
    return jsonify({"ok": ok, "reason": reason, "root": Config.NAS_LIBRARY_ROOT}), (200 if ok else 503)


# --- Recorder profiles: how each recorder's filenames are read (jobs/filename_patterns.py) ---

def _profile_to_dict(p):
    return {"id": p.id, "name": p.name, "patterns": p.patterns, "timezone": p.timezone,
            "clock_offset_seconds": p.clock_offset_seconds, "date_trust": p.date_trust,
            "priority": p.priority, "active": p.active}


def _validate_profile(data, partial):
    """Returns an error string or None."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    from jobs.filename_patterns import compile_pattern
    if not partial or "name" in data:
        if not isinstance(data.get("name"), str) or not data["name"].strip():
            return "name is required"
    if not partial or "patterns" in data:
        pats = data.get("patterns")
        if not isinstance(pats, list) or not all(isinstance(x, str) and x for x in pats):
            return "patterns must be a list of non-empty strings"
        for pattern in pats:
            try:
                compile_pattern(pattern)
            except ValueError as e:
                return f"pattern {pattern!r}: {e}"
    if "timezone" in data:
        try:
            ZoneInfo(data["timezone"])
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            return "timezone must be an IANA name such as Europe/London"
    if "clock_offset_seconds" in data:
        v = data["clock_offset_seconds"]
        if isinstance(v, bool) or not isinstance(v, int) or abs(v) > 86400:
            return "clock_offset_seconds must be an integer within +/-86400 (recorder clock minus true time)"
    if "date_trust" in data and data["date_trust"] not in ("trusted", "suggest"):
        return "date_trust must be 'trusted' or 'suggest'"
    if "priority" in data and (isinstance(data["priority"], bool) or not isinstance(data["priority"], int)):
        return "priority must be an integer"
    if "active" in data and not isinstance(data["active"], bool):
        return "active must be true or false"
    return None


@bp.get("/recorder-profiles")
def list_recorder_profiles():
    from jobs.profiles import ensure_default_profiles
    ensure_default_profiles()
    rows = RecorderProfile.query.order_by(RecorderProfile.priority, RecorderProfile.name).all()
    return jsonify([_profile_to_dict(p) for p in rows])


@bp.post("/recorder-profiles")
def create_recorder_profile():
    data = request.get_json() or {}
    error = _validate_profile(data, partial=False)
    if error:
        return jsonify({"error": error}), 400
    if RecorderProfile.query.filter_by(name=data["name"].strip()).first():
        return jsonify({"error": "a profile with that name already exists"}), 409
    p = RecorderProfile(
        name=data["name"].strip(), patterns=data["patterns"],
        timezone=data.get("timezone", Config.DEFAULT_RECORDER_TIMEZONE),
        clock_offset_seconds=data.get("clock_offset_seconds", 0),
        date_trust=data.get("date_trust", "suggest"), priority=data.get("priority", 100),
        active=data.get("active", True),
    )
    db.session.add(p)
    db.session.commit()
    return jsonify(_profile_to_dict(p)), 201


@bp.patch("/recorder-profiles/<profile_id>")
def update_recorder_profile(profile_id):
    p = RecorderProfile.query.get_or_404(profile_id)
    data = request.get_json() or {}
    error = _validate_profile(data, partial=True)
    if error:
        return jsonify({"error": error}), 400
    if "name" in data:
        clash = RecorderProfile.query.filter(RecorderProfile.name == data["name"].strip(), RecorderProfile.id != p.id).first()
        if clash:
            return jsonify({"error": "a profile with that name already exists"}), 409
        p.name = data["name"].strip()
    for field in ("patterns", "timezone", "clock_offset_seconds", "date_trust", "priority", "active"):
        if field in data:
            setattr(p, field, data[field])
    db.session.commit()
    return jsonify(_profile_to_dict(p))


@bp.post("/filename-preview")
def filename_preview():
    """What would ingest make of this filename? Nothing is stored. For trying patterns."""
    from jobs.filename_patterns import match_filename
    from jobs.profiles import active_profiles
    name = (request.get_json() or {}).get("filename")
    if not isinstance(name, str) or not name.strip():
        return jsonify({"error": "filename is required"}), 400
    fm = match_filename(os.path.basename(name), active_profiles())
    if fm is None:
        return jsonify({"matched": False, "would_apply": "none"})
    if fm.utc and fm.trusted and fm.time_known:
        would = "captured_at"
    elif fm.utc:
        would = "suggestion"
    else:
        would = "none"
    return jsonify({
        "matched": True, "profile": fm.profile, "pattern": fm.pattern, "trusted": fm.trusted,
        "utc": to_utc_iso(fm.utc), "time_known": fm.time_known, "title": fm.title,
        "is_edit": fm.is_edit, "seq": fm.seq, "unknown_reason": fm.unknown_reason, "would_apply": would,
    })


# --- Sub-clips ---

@bp.get("/resources/<resource_id>/clips")
def list_clips(resource_id):
    Resource.query.get_or_404(resource_id)
    clips = Clip.query.filter_by(resource_id=resource_id).order_by(Clip.start_seconds).all()
    return jsonify([_clip_to_dict(c) for c in clips])


@bp.post("/resources/<resource_id>/clips")
def create_clip(resource_id):
    resource = Resource.query.get_or_404(resource_id)
    data = request.get_json() or {}

    if "start_seconds" not in data or "end_seconds" not in data:
        return jsonify({"error": "start_seconds and end_seconds are required"}), 400
    if data["end_seconds"] <= data["start_seconds"]:
        return jsonify({"error": "end_seconds must be after start_seconds"}), 400
    if resource.duration_seconds and data["end_seconds"] > resource.duration_seconds:
        return jsonify({
            "error": f"end_seconds ({data['end_seconds']}) exceeds the "
                     f"resource's duration ({resource.duration_seconds})",
        }), 400

    clip = Clip(
        resource_id=resource_id,
        start_seconds=data["start_seconds"],
        end_seconds=data["end_seconds"],
        label=data.get("label"),
        notes=data.get("notes"),
    )
    db.session.add(clip)
    db.session.commit()
    return jsonify(_clip_to_dict(clip)), 201


@bp.patch("/clips/<clip_id>")
def update_clip(clip_id):
    clip = Clip.query.get_or_404(clip_id)
    data = request.get_json() or {}
    for field in ("start_seconds", "end_seconds", "label", "notes"):
        if field in data:
            setattr(clip, field, data[field])
    db.session.commit()
    return jsonify(_clip_to_dict(clip))


@bp.delete("/clips/<clip_id>")
def delete_clip(clip_id):
    clip = Clip.query.get_or_404(clip_id)
    db.session.delete(clip)
    db.session.commit()
    return "", 204


@bp.post("/clips/<clip_id>/export")
def export_clip_route(clip_id):
    """
    Queues a stream-copy export of this clip's exact time window in
    its original format — the "just cut this bit out" case. For a
    format conversion, use POST /api/resources/<id>/export with
    clip_id in the body instead. Kept as a separate shorthand route
    since it's the most common single action from a waveform-browser
    "export this clip" button.
    """
    from jobs.queue import enqueue

    clip = Clip.query.get_or_404(clip_id)
    export = Export(resource_id=clip.resource_id, clip_id=clip.id, format="original")
    db.session.add(export)
    db.session.commit()

    enqueue("process-exports", triggered_by="manual")
    return jsonify(_export_to_dict(export)), 202


def _clip_to_dict(c: Clip):
    return {
        "id": c.id,
        "resource_id": c.resource_id,
        "start_seconds": c.start_seconds,
        "end_seconds": c.end_seconds,
        "label": c.label,
        "notes": c.notes,
        "created_at": to_utc_iso(c.created_at),
    }


# --- Export / format-conversion workflow ---

@bp.post("/resources/<resource_id>/export")
def create_export(resource_id):
    """
    Queues an export — actual ffmpeg work happens asynchronously in
    the process-exports job (jobs/export.py), run by the queue worker.
    This endpoint only ever does a fast DB insert + enqueue, so
    starting an export never hangs the request regardless of file
    size or how long the target format takes to encode.
    """
    from jobs.queue import enqueue

    resource = Resource.query.get_or_404(resource_id)
    data = request.get_json() or {}

    fmt = data.get("format", "original")
    if fmt not in Config.EXPORT_FORMATS:
        return jsonify({"error": f"format must be one of {Config.EXPORT_FORMATS}"}), 400

    clip_id = data.get("clip_id")
    if clip_id:
        clip = Clip.query.get_or_404(clip_id)
        if clip.resource_id != resource_id:
            return jsonify({"error": "clip does not belong to this resource"}), 400
    elif fmt == "original":
        return jsonify({
            "error": "format 'original' requires clip_id — exporting a whole "
                     "resource unchanged is just the file itself",
        }), 400

    export = Export(
        resource_id=resource_id,
        clip_id=clip_id,
        format=fmt,
        quality=data.get("quality"),
        embed_metadata=data.get("embed_metadata", True),
    )
    db.session.add(export)
    db.session.commit()

    enqueue("process-exports", triggered_by="manual")
    return jsonify(_export_to_dict(export)), 202


@bp.get("/resources/<resource_id>/exports")
def list_exports(resource_id):
    Resource.query.get_or_404(resource_id)
    exports = Export.query.filter_by(resource_id=resource_id).order_by(Export.requested_at.desc()).all()
    return jsonify([_export_to_dict(e) for e in exports])


@bp.get("/exports/<export_id>")
def get_export(export_id):
    export = Export.query.get_or_404(export_id)
    return jsonify(_export_to_dict(export))


@bp.get("/exports/<export_id>/download")
def download_export(export_id):
    export = Export.query.get_or_404(export_id)
    if export.status != "success" or not export.output_path or not os.path.exists(export.output_path):
        abort(404, description="export not ready or file missing")
    return send_file(export.output_path, as_attachment=True, conditional=True)


def _export_to_dict(e: Export):
    return {
        "id": e.id,
        "resource_id": e.resource_id,
        "clip_id": e.clip_id,
        "format": e.format,
        "quality": e.quality,
        "embed_metadata": e.embed_metadata,
        "status": e.status,
        "error_detail": e.error_detail,
        "requested_at": to_utc_iso(e.requested_at),
        "completed_at": to_utc_iso(e.completed_at),
        "download_url": f"/api/exports/{e.id}/download" if e.status == "success" else None,
    }


# --- Companion photos (Immich) ---

@bp.get("/resources/<resource_id>/photos")
def list_photos(resource_id):
    Resource.query.get_or_404(resource_id)
    photos = ResourcePhoto.query.filter_by(resource_id=resource_id).all()
    return jsonify([
        {
            "id": p.id,
            "immich_asset_id": p.immich_asset_id,
            "taken_at": to_utc_iso(p.taken_at),
            # Frontend loads the actual image from these, not from Immich
            # directly — keeps the Immich API key server-side only.
            "thumbnail_url": f"/api/photos/{p.immich_asset_id}/thumbnail",
            "original_url": f"/api/photos/{p.immich_asset_id}/original",
        }
        for p in photos
    ])


@bp.post("/resources/<resource_id>/photos/refresh")
def refresh_photos(resource_id):
    """
    Manual re-run of the Immich lookup for one resource (ad-hoc, not
    scheduled). max_attempts=1: this runs synchronously inside a user's
    HTTP request, so it must fail fast rather than working through the
    background jobs' full retry+backoff (which could add up to ~15s of
    waiting before even reporting failure) — a bounded quick failure
    beats a slow one for something the UI is waiting on.
    """
    from jobs.retry import ServiceUnavailable

    resource = Resource.query.get_or_404(resource_id)
    fetch_photos_for_recording, problem = _provider_or_error("photos")
    if problem:
        return jsonify(problem[0]), problem[1]
    if not resource.captured_at:
        return jsonify({"error": "resource has no captured_at to search around"}), 400
    if resource.captured_at_precision != "exact":
        return jsonify({"error": "photo lookup needs an exact date and time; this one is "
                                 f"{resource.captured_at_precision}"}), 400

    try:
        photos = fetch_photos_for_recording(
            resource.captured_at, resource.duration_seconds, max_attempts=1,
        )
    except ServiceUnavailable as e:
        return jsonify({"error": f"Immich unavailable, try again later: {e}"}), 503

    added = []
    for photo in photos:
        exists = ResourcePhoto.query.filter_by(
            resource_id=resource.id, immich_asset_id=photo["immich_asset_id"],
        ).first()
        if exists:
            continue
        rp = ResourcePhoto(
            resource_id=resource.id,
            immich_asset_id=photo["immich_asset_id"],
            taken_at=photo["taken_at"],
        )
        db.session.add(rp)
        added.append(photo["immich_asset_id"])

    resource.immich_checked_at = db.func.now()
    db.session.commit()
    return jsonify({"added": added})


@bp.get("/photos/<immich_asset_id>/thumbnail")
def photo_thumbnail(immich_asset_id):
    return _proxy_immich_asset(immich_asset_id, endpoint="thumbnail")


@bp.get("/photos/<immich_asset_id>/original")
def photo_original(immich_asset_id):
    return _proxy_immich_asset(immich_asset_id, endpoint="original")


def _proxy_immich_asset(immich_asset_id, endpoint):
    """
    Streams a thumbnail/original through from Immich rather than
    exposing the Immich API key to the browser. ⚠️ Path assumed as
    /api/assets/<id>/thumbnail|original — confirm against your
    instance's API docs if this 404s; Immich's asset-serving routes
    have shifted between versions (see jobs/immich.py docstring).

    Single attempt, short timeout, no retry-with-backoff: this is a
    synchronous request the browser is waiting on to render an <img>,
    so a bounded quick failure (browser shows a broken-image icon) is
    correct — retrying here would just make a slow page slower.
    """
    import requests
    from flask import Response

    immich_url = get_config("IMMICH_API_URL")
    if not immich_url:
        abort(404, description="Immich not configured")

    try:
        upstream = requests.get(
            f"{immich_url}/api/assets/{immich_asset_id}/{endpoint}",
            headers={"x-api-key": get_config("IMMICH_API_KEY")},
            stream=True, timeout=Config.HTTP_TIMEOUT_SECONDS,
        )
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
        abort(502, description="Immich unreachable")

    if upstream.status_code != 200:
        abort(upstream.status_code)

    return Response(
        upstream.iter_content(chunk_size=8192),
        content_type=upstream.headers.get("Content-Type", "application/octet-stream"),
    )


@bp.post("/resources/<resource_id>/location/refresh")
def refresh_location(resource_id):
    """
    Manual re-run of the Dawarich lookup for one resource. Mirrors
    refresh_photos below. Exists for the case enrich_locations doesn't
    cover: a resource already has dawarich_checked_at set (Dawarich
    was reachable and genuinely had nothing for that window at the
    time), but you've since backfilled older location history into
    Dawarich and want this specific resource re-checked — the
    automatic queue only ever looks at checked_at IS NULL, so it will
    never retry this on its own.
    """
    from jobs.retry import ServiceUnavailable

    resource = Resource.query.get_or_404(resource_id)
    fetch_track_and_pin, problem = _provider_or_error("location")
    if problem:
        return jsonify(problem[0]), problem[1]
    if not resource.captured_at:
        return jsonify({"error": "resource has no captured_at to search around"}), 400
    if resource.captured_at_precision != "exact":
        return jsonify({"error": "location lookup needs an exact date and time; this one is "
                                 f"{resource.captured_at_precision}"}), 400

    try:
        track_points, pin = fetch_track_and_pin(
            resource.captured_at, resource.duration_seconds, max_attempts=1,
        )
    except ServiceUnavailable as e:
        return jsonify({"error": f"Dawarich unavailable, try again later: {e}"}), 503

    from jobs.enrich import apply_location_result
    result = apply_location_result(resource, track_points, pin)

    resource.dawarich_checked_at = db.func.now()
    db.session.commit()
    if result["pin_stored"]:
        from jobs.geocode import enqueue_geocode
        enqueue_geocode("location-refresh")
    return jsonify({
        "found": result["pin_stored"], "track_points": len(track_points),
        # A location entered by hand is never replaced by an automatic lookup.
        "kept_manual_location": result["kept_manual"],
    })

# --- Direct upload (e.g. a future companion mobile app) ---

@bp.post("/ingest/upload")
def upload_audio():
    """
    Direct-upload path — for a phone app (or manual testing) that
    wants to send a recording straight to the server instead of going
    via the Google Drive inbox. Reuses the exact same
    ingest_staged_file() pipeline drive_inbox_pull uses, so a file
    uploaded this way gets identical checksum/dedupe/metadata/
    enrichment-queueing behavior regardless of which path it came in
    through — no separate code path to keep in sync.

    Runs synchronously in the request, unlike Drive's async poll-based
    pull: an HTTP multipart upload is already fully received by the
    time this view function runs, so there's no "is it still
    uploading?" ambiguity to wait out — that stability check exists
    specifically for polling a Drive folder where "the file appeared"
    and "the file finished uploading" are different moments; here they
    coincide by construction. Ingest itself is fast (checksum + probe,
    all local), so this doesn't meaningfully block.

    Gated by UPLOAD_API_KEY since — unlike the rest of this API — this
    is the endpoint most likely to need internet exposure (a phone on
    mobile data), not just LAN/VPN access.
    """
    upload_key = get_config("UPLOAD_API_KEY")
    if not upload_key:
        abort(404, description="upload endpoint not configured")
    if request.headers.get("X-Upload-Key") != upload_key:
        return jsonify({"error": "invalid or missing X-Upload-Key header"}), 401

    if "file" not in request.files:
        return jsonify({"error": "no file part in request"}), 400
    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "no file selected"}), 400

    os.makedirs(Config.STAGING_DIR, exist_ok=True)
    local_path = os.path.join(Config.STAGING_DIR, file.filename)
    file.save(local_path)

    from jobs.ingest import ingest_staged_file
    try:
        resource = ingest_staged_file(local_path, drive_inbox_path=None)
    except Exception as e:
        return jsonify({"error": f"ingest failed: {e}"}), 500

    if resource is None:
        return jsonify({
            "status": "duplicate",
            "message": "file matched an existing resource, quarantined",
        }), 200

    return jsonify(_resource_to_dict(resource)), 201


# --- Projects / Tags (simple CRUD) ---

PLACEMENTS = ("nas", "drive", "both")
HOMES = ("nas", "drive")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def _valid_uuid(value):
    try:
        return str(uuid.UUID(str(value))) == str(value).lower()
    except (ValueError, AttributeError):
        return False


def _project_to_dict(p):
    return {"id": p.id, "name": p.name, "slug": p.slug, "notes": p.notes,
            "placement": p.placement, "home": p.home, "created_at": to_utc_iso(p.created_at)}


def _placement_error(placement, home):
    if placement not in PLACEMENTS:
        return f"placement must be one of {list(PLACEMENTS)}"
    if home not in HOMES:
        return f"home must be one of {list(HOMES)}"
    if (placement == "nas" and home != "nas") or (placement == "drive" and home != "drive"):
        return f"home '{home}' isn't possible with placement '{placement}'"
    return None


@bp.get("/projects")
def list_projects():
    files = dict(db.session.query(Resource.project_id, db.func.count(Resource.id))
                 .filter(Resource.project_id.isnot(None), Resource.role.notin_(("sidecar", "project-file")))
                 .group_by(Resource.project_id).all())
    sessions = dict(db.session.query(RecordingSession.project_id, db.func.count(RecordingSession.id))
                    .filter(RecordingSession.project_id.isnot(None)).group_by(RecordingSession.project_id).all())
    out = []
    for p in Project.query.order_by(Project.name).all():
        d = _project_to_dict(p)
        d["file_count"], d["session_count"] = files.get(p.id, 0), sessions.get(p.id, 0)
        out.append(d)
    return jsonify(out)


@bp.post("/projects")
def create_project():
    """
    Idempotent when the client supplies its own UUID `id` (an offline app retrying a create):
    posting the same id again returns the existing project instead of making another.
    """
    data = request.get_json() or {}
    name = data.get("name")
    slug = data.get("slug")
    if not isinstance(name, str) or not name.strip():
        return jsonify({"error": "name is required"}), 400
    if not isinstance(slug, str) or not SLUG_RE.match(slug):
        return jsonify({"error": "slug must be lowercase letters, digits and hyphens (it becomes the NAS folder name)"}), 400
    placement, home = data.get("placement", "nas"), data.get("home", "nas")
    error = _placement_error(placement, home)
    if error:
        return jsonify({"error": error}), 400

    new_id = data.get("id")
    if new_id is not None:
        if not _valid_uuid(new_id):
            return jsonify({"error": "id must be a UUID"}), 400
        existing = db.session.get(Project, new_id)
        if existing:
            return jsonify(_project_to_dict(existing)), 200

    p = Project(name=name.strip(), slug=slug, notes=data.get("notes"), placement=placement, home=home)
    if new_id is not None:
        p.id = new_id
    db.session.add(p)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({"error": f"slug '{slug}' is already in use"}), 400
    return jsonify(_project_to_dict(p)), 201


@bp.get("/projects/<project_id>")
def get_project(project_id):
    p = Project.query.get_or_404(project_id)
    d = _project_to_dict(p)
    d["sessions"] = [_session_to_dict(s) for s in
                     RecordingSession.query.filter_by(project_id=p.id).order_by(RecordingSession.session_date, RecordingSession.name)]
    return jsonify(d)


@bp.patch("/projects/<project_id>")
def update_project(project_id):
    p = Project.query.get_or_404(project_id)
    data = request.get_json() or {}
    if "slug" in data and data["slug"] != p.slug:
        return jsonify({"error": "the slug is the NAS folder name and can't be changed"}), 400
    if "name" in data:
        if not isinstance(data["name"], str) or not data["name"].strip():
            return jsonify({"error": "name can't be empty"}), 400
        p.name = data["name"].strip()
    if "notes" in data:
        if data["notes"] is not None and not isinstance(data["notes"], str):
            return jsonify({"error": "notes must be text"}), 400
        p.notes = data["notes"]
    if "placement" in data or "home" in data:
        placement, home = data.get("placement", p.placement), data.get("home", p.home)
        error = _placement_error(placement, home)
        if error:
            return jsonify({"error": error}), 400
        p.placement, p.home = placement, home
    db.session.commit()
    return jsonify(_project_to_dict(p))


# --- Sessions: one night of a show / one outing (PLAN 18.1) ---

def _session_to_dict(s, file_count=None):
    if file_count is None:
        file_count = Resource.query.filter_by(session_id=s.id).count()
    return {"id": s.id, "project_id": s.project_id, "name": s.name,
            "session_date": s.session_date.isoformat() if s.session_date else None,
            "notes": s.notes, "file_count": file_count, "created_at": to_utc_iso(s.created_at)}


def _session_folder_taken(project_id, name, exclude_id=None):
    """Two names that end up as the same NAS folder (case-insensitively, as SMB does) clash."""
    from jobs.path_template import safe_component
    wanted = safe_component(name).lower()
    query = RecordingSession.query.filter(RecordingSession.project_id.is_(None) if project_id is None
                                          else RecordingSession.project_id == project_id)
    return any(safe_component(s.name).lower() == wanted for s in query if s.id != exclude_id)


def _parse_session_date(value):
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError("session_date must be YYYY-MM-DD")


@bp.get("/sessions")
def list_sessions():
    query = RecordingSession.query
    project_id = request.args.get("project_id")
    if project_id == "none":
        query = query.filter(RecordingSession.project_id.is_(None))
    elif project_id:
        query = query.filter(RecordingSession.project_id == project_id)
    counts = dict(db.session.query(Resource.session_id, db.func.count(Resource.id))
                  .filter(Resource.session_id.isnot(None)).group_by(Resource.session_id).all())
    rows = query.order_by(RecordingSession.session_date.desc().nullslast(), RecordingSession.name).all()
    return jsonify([_session_to_dict(s, counts.get(s.id, 0)) for s in rows])


@bp.post("/sessions")
def create_session():
    """Idempotent by client-supplied UUID `id`, like projects. Without an id, a name that would
    collide with a sibling folder is refused (409)."""
    data = request.get_json() or {}
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        return jsonify({"error": "name is required"}), 400
    project_id = data.get("project_id")
    if project_id is not None and db.session.get(Project, project_id) is None:
        return jsonify({"error": "unknown project"}), 400
    try:
        session_date = _parse_session_date(data.get("session_date"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if data.get("notes") is not None and not isinstance(data["notes"], str):
        return jsonify({"error": "notes must be text"}), 400

    new_id = data.get("id")
    if new_id is not None:
        if not _valid_uuid(new_id):
            return jsonify({"error": "id must be a UUID"}), 400
        existing = db.session.get(RecordingSession, new_id)
        if existing:
            return jsonify(_session_to_dict(existing)), 200
    if _session_folder_taken(project_id, name):
        return jsonify({"error": "a session with that folder name already exists in this project"}), 409

    s = RecordingSession(name=name.strip(), project_id=project_id, session_date=session_date, notes=data.get("notes"))
    if new_id is not None:
        s.id = new_id
    db.session.add(s)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({"error": "a session with that name already exists in this project"}), 409
    return jsonify(_session_to_dict(s, 0)), 201


@bp.get("/sessions/<session_id>")
def get_session(session_id):
    s = RecordingSession.query.get_or_404(session_id)
    d = _session_to_dict(s)
    files = Resource.query.filter_by(session_id=s.id).order_by(Resource.captured_at, Resource.filename).all()
    d["files"] = [{"id": f.id, "filename": f.filename, "role": f.role or "original", "track_label": f.track_label,
                   "status": f.status, "duration_seconds": f.duration_seconds,
                   "captured_at": to_utc_iso(f.captured_at)} for f in files]
    return jsonify(d)


@bp.patch("/sessions/<session_id>")
def update_session(session_id):
    s = RecordingSession.query.get_or_404(session_id)
    data = request.get_json() or {}
    new_project = data["project_id"] if "project_id" in data else s.project_id
    new_name = data["name"].strip() if isinstance(data.get("name"), str) else s.name
    if "name" in data and (not isinstance(data["name"], str) or not data["name"].strip()):
        return jsonify({"error": "name can't be empty"}), 400
    if "project_id" in data and new_project is not None and db.session.get(Project, new_project) is None:
        return jsonify({"error": "unknown project"}), 400
    if (("name" in data or "project_id" in data)
            and _session_folder_taken(new_project, new_name, exclude_id=s.id)):
        return jsonify({"error": "a session with that folder name already exists in the target project"}), 409
    if "session_date" in data:
        try:
            s.session_date = _parse_session_date(data["session_date"])
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
    if "notes" in data:
        if data["notes"] is not None and not isinstance(data["notes"], str):
            return jsonify({"error": "notes must be text"}), 400
        s.notes = data["notes"]
    s.name = new_name
    if "project_id" in data and new_project != s.project_id:
        s.project_id = new_project
        # A file's project is its session's project: move the files along with it.
        Resource.query.filter_by(session_id=s.id).update({"project_id": new_project}, synchronize_session=False)
    db.session.commit()
    return jsonify(_session_to_dict(s))


@bp.delete("/sessions/<session_id>")
def delete_session(session_id):
    s = RecordingSession.query.get_or_404(session_id)
    if Resource.query.filter_by(session_id=s.id).count():
        return jsonify({"error": "this session still has files; move them out first"}), 409
    db.session.delete(s)
    db.session.commit()
    return "", 204


def _category_to_dict(c, count):
    return {"slug": c.slug, "label": c.label, "archived": c.archived, "sort_order": c.sort_order, "file_count": count}


def _category_counts():
    return dict(db.session.query(Resource.category, db.func.count(Resource.id))
                .filter(Resource.category.isnot(None), Resource.role.notin_(("sidecar", "project-file")))
                .group_by(Resource.category).all())


@bp.get("/categories")
def list_categories():
    """Active categories (what pickers offer). ?all=1 includes archived ones, for the manage screen."""
    cats.ensure_default_categories()
    query = Category.query.order_by(Category.sort_order, Category.label)
    if not request.args.get("all"):
        query = query.filter(Category.archived.is_(False))
    counts = _category_counts()
    return jsonify([_category_to_dict(c, counts.get(c.slug, 0)) for c in query.all()])


@bp.post("/categories")
def create_category():
    data = request.get_json() or {}
    label = data.get("label")
    if not isinstance(label, str) or not label.strip():
        return jsonify({"error": "label is required"}), 400
    slug = data.get("slug") or cats.slugify(label)
    if not SLUG_RE.match(slug or ""):
        return jsonify({"error": "slug must be lowercase letters, digits and hyphens (it becomes a folder name)"}), 400
    if db.session.get(Category, slug):
        return jsonify({"error": f"a category with slug '{slug}' already exists"}), 409
    top = db.session.query(db.func.max(Category.sort_order)).scalar() or 0
    c = Category(slug=slug, label=label.strip(), sort_order=top + 10)
    db.session.add(c)
    db.session.commit()
    return jsonify(_category_to_dict(c, 0)), 201


@bp.patch("/categories/<slug>")
def update_category(slug):
    c = Category.query.get_or_404(slug)
    data = request.get_json() or {}
    if "slug" in data and data["slug"] != c.slug:
        return jsonify({"error": "the slug is a folder name and can't be changed; rename the label instead"}), 400
    if "label" in data:
        if not isinstance(data["label"], str) or not data["label"].strip():
            return jsonify({"error": "label can't be empty"}), 400
        c.label = data["label"].strip()
    if "archived" in data:
        if not isinstance(data["archived"], bool):
            return jsonify({"error": "archived must be true or false"}), 400
        c.archived = data["archived"]
    if "sort_order" in data:
        if isinstance(data["sort_order"], bool) or not isinstance(data["sort_order"], int):
            return jsonify({"error": "sort_order must be an integer"}), 400
        c.sort_order = data["sort_order"]
    db.session.commit()
    return jsonify(_category_to_dict(c, _category_counts().get(c.slug, 0)))


@bp.post("/categories/<slug>/merge")
def merge_category(slug):
    """Give every file in this category to another one, then archive this one. Only the database changes:
    files already on the NAS stay where they are until `refile-all` is run on purpose."""
    src = Category.query.get_or_404(slug)
    target = db.session.get(Category, (request.get_json() or {}).get("into"))
    if target is None or target.slug == src.slug or target.archived:
        return jsonify({"error": "'into' must be another, active category"}), 400
    moved = Resource.query.filter_by(category=src.slug).update({"category": target.slug}, synchronize_session=False)
    src.archived = True
    db.session.commit()
    return jsonify({"moved": moved, "into": target.slug})


@bp.get("/tags")
def list_tags():
    counts = dict(db.session.query(resource_tags.c.tag_id, db.func.count(resource_tags.c.resource_id))
                  .group_by(resource_tags.c.tag_id).all())
    tags = Tag.query.order_by(db.func.lower(Tag.name)).all()
    return jsonify([{"id": t.id, "name": t.name, "file_count": counts.get(t.id, 0)} for t in tags])


@bp.patch("/tags/<tag_id>")
def rename_tag(tag_id):
    tag = Tag.query.get_or_404(tag_id)
    name = ((request.get_json() or {}).get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    clash = Tag.query.filter(db.func.lower(Tag.name) == name.lower(), Tag.id != tag.id).first()
    if clash:
        return jsonify({"error": f"a tag called '{clash.name}' already exists; merge into it instead",
                        "merge_into": clash.id}), 409
    tag.name = name
    db.session.commit()
    return jsonify({"id": tag.id, "name": tag.name})


@bp.post("/tags/<tag_id>/merge")
def merge_tag(tag_id):
    """Move every use of this tag to another tag (skipping files that already have it), then remove this one."""
    src = Tag.query.get_or_404(tag_id)
    target = db.session.get(Tag, (request.get_json() or {}).get("into"))
    if target is None or target.id == src.id:
        return jsonify({"error": "'into' must be another existing tag"}), 400
    with_target = {rid for (rid,) in db.session.query(resource_tags.c.resource_id).filter(resource_tags.c.tag_id == target.id)}
    on_src = [rid for (rid,) in db.session.query(resource_tags.c.resource_id).filter(resource_tags.c.tag_id == src.id)]
    for rid in on_src:
        if rid not in with_target:
            db.session.execute(resource_tags.insert().values(resource_id=rid, tag_id=target.id))
    db.session.execute(resource_tags.delete().where(resource_tags.c.tag_id == src.id))
    db.session.delete(src)
    db.session.commit()
    return jsonify({"merged": len(on_src), "into": target.id})


@bp.delete("/tags/<tag_id>")
def delete_tag(tag_id):
    """Remove a tag from everything and delete it. Refuses if it is in use unless ?force=1."""
    tag = Tag.query.get_or_404(tag_id)
    used = db.session.query(db.func.count()).select_from(resource_tags).filter(resource_tags.c.tag_id == tag.id).scalar()
    if used and not request.args.get("force"):
        return jsonify({"error": f"this tag is on {used} file(s); merge it into another tag, or delete anyway", "file_count": used}), 409
    db.session.execute(resource_tags.delete().where(resource_tags.c.tag_id == tag.id))
    db.session.delete(tag)
    db.session.commit()
    return "", 204


@bp.post("/tags")
def create_tag():
    """
    Get-or-create by name -- the review screen's tag picker calls this
    for a typed name with no matching existing tag, and wants back
    whichever tag now has that name rather than a 400 on a race with
    itself (e.g. adding the same new tag on two resources back to back).
    """
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400

    existing = Tag.query.filter_by(name=name).first()
    if existing:
        return jsonify({"id": existing.id, "name": existing.name})

    tag = Tag(name=name)
    db.session.add(tag)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        existing = Tag.query.filter_by(name=name).first()
        return jsonify({"id": existing.id, "name": existing.name})
    return jsonify({"id": tag.id, "name": tag.name}), 201


# --- Jobs: same trigger for scheduled runs and manual "run now" ---

JOB_REGISTRY = {}  # populated by jobs/__init__.py at import time


@bp.post("/jobs/<job_name>/run")
def run_job(job_name):
    """
    Enqueues the job and returns immediately — actual execution
    happens in the separate worker process (jobs/worker.py), not here.
    This is what makes "run now" non-blocking regardless of how long
    the job takes: the HTTP request only ever does a fast DB insert.
    """
    from jobs.queue import enqueue

    if job_name not in JOB_REGISTRY:
        return jsonify({"error": f"unknown job '{job_name}'"}), 404

    item = enqueue(job_name, triggered_by=request.args.get("triggered_by", "manual"))
    if item.status in ("queued", "running"):
        status_code = 202
    else:
        status_code = 200  # shouldn't happen immediately after enqueue, but be safe
    return jsonify({"status": item.status, "job_name": job_name, "queue_item_id": item.id}), status_code


@bp.get("/jobs/<job_name>/status")
def job_status(job_name):
    """
    last_result: the most recent completed run's outcome (from
    JobRun, updated by the job function itself — success/error/partial
    with its own detail message).
    queue: the current queue item for this job_name, if any is
    queued/running right now — lets the UI show "queued, attempt 2 of
    3, retrying in ~40s" rather than just the last historical result.
    """
    from app.models import JobQueueItem

    run = JobRun.query.get(job_name)
    last_result = None
    if run:
        last_result = {
            "last_run_at": to_utc_iso(run.last_run_at),
            "status": run.status,
            "log_tail": run.log_tail,
        }

    current = JobQueueItem.query.filter(
        JobQueueItem.job_name == job_name,
        JobQueueItem.status.in_(["queued", "running"]),
    ).order_by(JobQueueItem.enqueued_at.desc()).first()
    queue_info = None
    if current:
        queue_info = {
            "status": current.status,
            "attempts": current.attempts,
            "max_attempts": current.max_attempts,
            "next_attempt_at": to_utc_iso(current.next_attempt_at),
        }

    return jsonify({"job_name": job_name, "last_result": last_result, "queue": queue_info})


# --- Settings ---
# DB-backed overrides for the credentials/URLs it makes sense to edit
# from a settings page rather than only via env var + restart (see
# app/settings.py for exactly which keys). Everything else in
# config.py is still env-var only.

@bp.get("/settings")
def get_settings():
    from jobs import providers
    snapshot = settings_snapshot()
    snapshot["rclone_drive"] = _rclone_drive_status()
    snapshot["providers"] = providers.describe()      # which service fills each role, and what else is registered
    return jsonify(snapshot)


@bp.post("/settings/place-names/test")
def test_place_names():
    """Ask the configured geocoder to name a known place, so a wrong URL is found on the Settings page rather than
    later. Uses the URL currently saved (save first if you just changed it)."""
    from jobs.retry import ServiceUnavailable
    reverse, problem = _provider_or_error("geocoder")
    if problem:
        return jsonify(problem[0]), problem[1]
    try:
        result = reverse(51.5074, -0.1278, max_attempts=1)     # Charing Cross, London
    except ServiceUnavailable as e:
        return jsonify({"ok": False, "error": str(e)}), 200
    if result == "unconfigured":
        return jsonify({"ok": False, "error": "No URL is set."}), 200
    return jsonify({"ok": True, "example": result["label"] if result else None})


@bp.put("/settings")
def put_settings():
    """
    Accepts a partial update -- any subset of the overridable keys.
    A secret field left out (or sent as "") leaves the existing value
    untouched, since the settings page never has the real value to
    redisplay/resubmit -- only PATCH-style "set to this new value" is
    supported for secrets, never "confirm the current value".
    """
    data = request.get_json() or {}
    from .settings import OVERRIDABLE
    unknown = set(data) - OVERRIDABLE
    if unknown:
        return jsonify({"error": f"not settable: {sorted(unknown)}"}), 400

    for key, value in data.items():
        if key in SECRET_KEYS and value == "":
            continue  # blank means "leave unchanged", not "clear it"
        set_config(key, value)

    return jsonify(settings_snapshot())


def _rclone_drive_status():
    """
    Whether the `gdrive` remote (Config.RCLONE_DRIVE_REMOTE) has a
    token configured -- never returns the token itself. Local rclone
    config check only, no network call, so this is safe to include in
    every GET /settings.
    """
    import subprocess
    try:
        result = subprocess.run(
            ["rclone", "config", "show", Config.RCLONE_DRIVE_REMOTE],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return {"configured": False}
    has_token = result.returncode == 0 and "token = " in result.stdout
    has_service_account = result.returncode == 0 and "service_account_file = " in result.stdout
    auth_method = "service_account" if has_service_account else ("oauth" if has_token else None)
    status = {
        "configured": has_token or has_service_account,
        "auth_method": auth_method,
        "remote": Config.RCLONE_DRIVE_REMOTE,
        "oauth_client_configured": bool(get_config("GOOGLE_OAUTH_CLIENT_ID")),
    }
    if has_service_account:
        status["service_account_email"] = _service_account_email()
    if status["configured"]:
        for line in result.stdout.splitlines():
            if line.startswith("root_folder_id"):
                value = line.split("=", 1)[1].strip()
                if value:
                    status["root_folder_id"] = value
            elif line.startswith("shared_with_me"):
                status["shared_with_me"] = line.split("=", 1)[1].strip() == "true"
        if "root_folder_id" in status:
            # Display-only cache set alongside root_folder_id itself
            # (see set_rclone_drive_root below) -- not re-fetched from
            # Drive on every /api/settings call, just kept in sync.
            cached = Setting.query.get("_ROOT_FOLDER_NAME")
            if cached:
                status["root_folder_name"] = cached.value
    return status


def _service_account_email():
    import json as json_mod
    try:
        with open(SERVICE_ACCOUNT_KEY_PATH) as f:
            return json_mod.load(f).get("client_email")
    except (OSError, ValueError):
        return None


@bp.get("/settings/rclone/drive/folders")
def list_rclone_drive_folders():
    """
    Folder picker backend. With `parent_id`, lists that folder's
    children (--drive-root-folder-id, a one-off flag -- never touches
    the persisted remote config; only POST .../root-folder below does
    that). With no `parent_id` (top level), what "top level" means
    depends on the auth method: a service account has no Drive of its
    own, so everything it can see is under --drive-shared-with-me;
    an OAuth-as-you connection has a real My Drive root, and
    --drive-shared-with-me there would show items shared BY OTHERS
    with the user instead of their own Inbox/Library.
    """
    import json as json_mod
    import subprocess

    parent_id = request.args.get("parent_id")
    remote = Config.RCLONE_DRIVE_REMOTE
    cmd = ["rclone", "lsjson", f"{remote}:", "--dirs-only"]
    if parent_id:
        cmd += [f"--drive-root-folder-id={parent_id}"]
    elif _rclone_drive_status().get("auth_method") == "service_account":
        cmd += ["--drive-shared-with-me"]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except (subprocess.SubprocessError, OSError) as e:
        return jsonify({"error": f"rclone invocation failed: {e}"}), 500
    if result.returncode != 0:
        return jsonify({"error": result.stderr.strip()}), 400

    try:
        entries = json_mod.loads(result.stdout)
    except ValueError:
        return jsonify({"error": "unexpected rclone output"}), 500

    return jsonify([{"id": e["ID"], "name": e["Name"]} for e in entries])


@bp.post("/settings/rclone/drive/root-folder")
def set_rclone_drive_root():
    """
    Points the `gdrive` remote's root at a folder picked via the
    endpoint above, instead of the connection's own (for a service
    account: empty) Drive root -- this is what makes DRIVE_INBOX_PATH
    ("Inbox") and DRIVE_LIBRARY_PATH ("Library") in config.py resolve
    against the actual shared folder rather than nothing.
    """
    import subprocess

    data = request.get_json() or {}
    folder_id = (data.get("folder_id") or "").strip()
    folder_name = (data.get("folder_name") or "").strip()
    if not folder_id:
        return jsonify({"error": "folder_id is required"}), 400

    try:
        _update_rclone_drive_field("root_folder_id", folder_id)
    except (subprocess.SubprocessError, OSError) as e:
        return jsonify({"error": f"rclone invocation failed: {e}"}), 500
    except RcloneConfigError as e:
        return jsonify({"error": f"rclone config update failed: {e}"}), 400

    if folder_name:
        row = Setting.query.get("_ROOT_FOLDER_NAME") or Setting(key="_ROOT_FOLDER_NAME")
        row.value = folder_name
        db.session.merge(row)
        db.session.commit()

    return jsonify(_rclone_drive_status())


class RcloneConfigError(Exception):
    pass


# Even with --non-interactive and a token supplied up front, rclone's
# `drive` backend still walks a short post-config wizard (confirmed by
# hand against this rclone version -- not documented anywhere as a
# fixed sequence, so this is deliberately a lookup by field name, not
# by position, and unknown questions abort rather than guess):
# "already have a token, refresh it now?" -> no, it's fresh from the
# OAuth exchange we just did; "configure as a Shared/Team Drive?" -> no.
_DRIVE_WIZARD_ANSWERS = {
    "config_refresh_token": "false",
    "config_change_team_drive": "false",
}


def _drive_rclone_wizard(remote, result):
    """
    Drives rclone's --non-interactive post-config wizard (see
    _DRIVE_WIZARD_ANSWERS) to completion, starting from the JSON
    result of an initial `config create`/`config update` call.

    Raises RcloneConfigError on failure or an unrecognized wizard
    question (never silently answers something we don't have a
    considered default for).
    """
    import json as json_mod
    import subprocess

    for _ in range(10):  # hard ceiling -- never loop indefinitely
        try:
            reply = json_mod.loads(result.stdout)
        except ValueError:
            raise RcloneConfigError(f"unexpected rclone output: {result.stdout[:200]}")

        state = reply.get("State") or ""
        if not state:
            return

        option_name = (reply.get("Option") or {}).get("Name")
        if option_name not in _DRIVE_WIZARD_ANSWERS:
            raise RcloneConfigError(f"unhandled rclone config question: {option_name!r}")

        result = subprocess.run(
            ["rclone", "config", "update", remote, "--non-interactive", "--continue",
             "--state", state, "--result", _DRIVE_WIZARD_ANSWERS[option_name]],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            raise RcloneConfigError(result.stderr.strip())

    raise RcloneConfigError("rclone config wizard did not terminate")


def _write_rclone_drive_config(**params):
    """
    (Re)creates the `gdrive` remote with the given rclone `drive`
    backend params (token+client_id/secret for OAuth, or
    service_account_file for a service account). `rclone config
    create` on an existing remote fully replaces it -- confirmed by
    hand, not just assumed -- so switching auth methods never leaves
    stale fields (e.g. an old token) mixed in with a new
    service_account_file. Use _update_rclone_drive_field instead for a
    single-field change (e.g. root_folder_id) that should leave
    existing auth fields alone.
    """
    import subprocess

    remote = Config.RCLONE_DRIVE_REMOTE
    cmd = ["rclone", "config", "create", remote, "drive", "--non-interactive"]
    for key, value in params.items():
        if value:
            cmd += [key, value]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RcloneConfigError(result.stderr.strip())
    _drive_rclone_wizard(remote, result)


def _update_rclone_drive_field(key, value):
    """
    Updates a single field on the existing `gdrive` remote (e.g.
    root_folder_id) -- `config update`, unlike `config create`, merges
    rather than replaces, confirmed by hand (auth fields survive).
    """
    import subprocess

    remote = Config.RCLONE_DRIVE_REMOTE
    result = subprocess.run(
        ["rclone", "config", "update", remote, "--non-interactive", key, value],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RcloneConfigError(result.stderr.strip())
    _drive_rclone_wizard(remote, result)


@bp.post("/settings/rclone/drive")
def connect_rclone_drive():
    """
    Finishes a headless rclone OAuth setup: the user runs
    `rclone authorize "drive"` on a machine with a browser (this
    server has none), logs into Google there, and pastes the resulting
    JSON token blob here. We never perform the OAuth grant ourselves --
    only write the token rclone already obtained into this remote's
    config, the same as `rclone config create gdrive drive token '<pasted>'`
    would do interactively.
    """
    data = request.get_json() or {}
    token = (data.get("token") or "").strip()
    if not token:
        return jsonify({"error": "token is required (paste the output of 'rclone authorize \"drive\"')"}), 400

    import subprocess
    try:
        _write_rclone_drive_config(token=token)
    except (subprocess.SubprocessError, OSError) as e:
        return jsonify({"error": f"rclone invocation failed: {e}"}), 500
    except RcloneConfigError as e:
        return jsonify({"error": f"rclone config create failed: {e}"}), 400

    return jsonify(_rclone_drive_status())


SERVICE_ACCOUNT_KEY_PATH = "/etc/audio-manager/gdrive-service-account.json"


@bp.post("/settings/rclone/drive/service-account")
def connect_rclone_drive_service_account():
    """
    Switches the `gdrive` remote to a service account instead of
    OAuth-as-you. Unlike OAuth (whole-Drive access, no per-folder
    scope), a service account only ever sees folders explicitly
    shared with its own email address -- genuine folder-level
    restriction, not just rclone-side convention. The key is a
    long-lived credential (doesn't expire the way an OAuth token
    does), so it's stored as its own file, not inline in rclone.conf.
    """
    import json as json_mod
    import subprocess

    data = request.get_json() or {}
    raw_key = (data.get("key") or "").strip()
    if not raw_key:
        return jsonify({"error": "key is required (the full JSON key file content)"}), 400

    try:
        parsed = json_mod.loads(raw_key)
    except ValueError:
        return jsonify({"error": "not valid JSON"}), 400
    if parsed.get("type") != "service_account" or "client_email" not in parsed:
        return jsonify({"error": "doesn't look like a Google service account key (expected type=service_account, client_email)"}), 400

    os.makedirs(os.path.dirname(SERVICE_ACCOUNT_KEY_PATH), exist_ok=True)
    with open(SERVICE_ACCOUNT_KEY_PATH, "w") as f:
        f.write(raw_key)
    os.chmod(SERVICE_ACCOUNT_KEY_PATH, 0o600)

    try:
        _write_rclone_drive_config(service_account_file=SERVICE_ACCOUNT_KEY_PATH)
    except (subprocess.SubprocessError, OSError) as e:
        return jsonify({"error": f"rclone invocation failed: {e}"}), 500
    except RcloneConfigError as e:
        return jsonify({"error": f"rclone config create failed: {e}"}), 400

    return jsonify(_rclone_drive_status())


GOOGLE_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"


@bp.get("/settings/rclone/drive/oauth/start")
def rclone_drive_oauth_start():
    """
    Redirects the browser to Google's consent screen using our own
    registered OAuth client (settings page "Connect with Google Drive"
    button) -- the alternative to the paste-token flow above. The
    consent happens in the user's own browser/Google session; this
    server never sees their Google credentials, only the resulting
    authorization code (exchanged for a token in the callback below).
    """
    from flask import redirect, session
    from urllib.parse import urlencode
    import secrets as secrets_mod

    client_id = get_config("GOOGLE_OAUTH_CLIENT_ID")
    if not client_id:
        return jsonify({"error": "set a Google OAuth Client ID/Secret first"}), 400

    state = secrets_mod.token_urlsafe(24)
    session["drive_oauth_state"] = state

    params = {
        "client_id": client_id,
        "redirect_uri": Config.GOOGLE_OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": GOOGLE_DRIVE_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return redirect(f"{GOOGLE_AUTH_ENDPOINT}?{urlencode(params)}")


@bp.get("/settings/rclone/drive/oauth/callback")
def rclone_drive_oauth_callback():
    """
    Google redirects the user's browser here with an authorization
    code after they approve. Exchanges it for a token server-side
    (this is the one step that needs the client secret) and writes it
    into rclone's config the same way the paste-token flow does.
    """
    import requests
    from flask import redirect, session

    error = request.args.get("error")
    if error:
        return redirect(f"/settings?drive_error={error}")

    state = request.args.get("state")
    if not state or state != session.pop("drive_oauth_state", None):
        return redirect("/settings?drive_error=state_mismatch")

    code = request.args.get("code")
    client_id = get_config("GOOGLE_OAUTH_CLIENT_ID")
    client_secret = get_config("GOOGLE_OAUTH_CLIENT_SECRET")

    try:
        resp = requests.post(GOOGLE_TOKEN_ENDPOINT, data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": Config.GOOGLE_OAUTH_REDIRECT_URI,
            "grant_type": "authorization_code",
        }, timeout=Config.HTTP_TIMEOUT_SECONDS)
        resp.raise_for_status()
        tok = resp.json()
    except requests.RequestException:
        return redirect("/settings?drive_error=token_exchange_failed")

    if "access_token" not in tok:
        return redirect("/settings?drive_error=token_exchange_failed")

    from datetime import datetime, timedelta
    import json as json_mod
    expiry = (datetime.utcnow() + timedelta(seconds=tok.get("expires_in", 3600))).isoformat() + "Z"
    token_json = json_mod.dumps({
        "access_token": tok["access_token"],
        "token_type": tok.get("token_type", "Bearer"),
        "refresh_token": tok.get("refresh_token"),
        "expiry": expiry,
    })

    import subprocess
    try:
        _write_rclone_drive_config(token=token_json, client_id=client_id, client_secret=client_secret)
    except (subprocess.SubprocessError, OSError, RcloneConfigError):
        return redirect("/settings?drive_error=rclone_config_failed")

    return redirect("/settings?drive_connected=1")
