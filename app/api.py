from flask import Blueprint, jsonify, request, send_file, abort
import os
from sqlalchemy.exc import IntegrityError

from .extensions import db
from .models import Resource, Project, Tag, Location, JobRun, FileEvent, Clip, ResourcePhoto, Export, Setting
from .settings import get_config, set_config, settings_snapshot, SECRET_KEYS
from config import Config

bp = Blueprint("api", __name__)


# --- Resources (the review queue is just a filtered view over this) ---

@bp.get("/resources")
def list_resources():
    """
    Paginated — this is explicitly a "growing collection" app, so an
    unbounded `SELECT *` here would eventually mean a multi-MB JSON
    response and a slow query. limit caps at 200/defaults to 50;
    offset for simple pagination (fine at this scale — a cursor would
    only matter at a size this app isn't going to reach).
    """
    status = request.args.get("status")
    limit = min(int(request.args.get("limit", 50)), 200)
    offset = int(request.args.get("offset", 0))

    query = Resource.query
    if status:
        query = query.filter_by(status=status)

    total = query.count()
    resources = query.order_by(Resource.created_at.desc()).offset(offset).limit(limit).all()
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
    data = request.get_json() or {}

    if "category" in data and data["category"] not in (None, *Config.CATEGORIES):
        return jsonify({"error": f"category must be one of {Config.CATEGORIES}"}), 400

    filing_now = data.get("status") == "filed" and r.status != "filed"

    for field in ("category", "project_id", "captured_at", "captured_at_source"):
        if field in data:
            setattr(r, field, data[field])

    if "tags" in data:
        r.tags = [Tag.query.get(tag_id) for tag_id in data["tags"]]

    if "location" in data:
        loc = r.location or Location(resource_id=r.id)
        loc.lat = data["location"].get("lat")
        loc.lon = data["location"].get("lon")
        loc.source = data["location"].get("source", "manual")
        db.session.add(loc)

    if filing_now:
        if not r.category:
            return jsonify({"error": "category is required before filing"}), 400
        _file_resource(r)
    elif "status" in data:
        r.status = data["status"]

    db.session.commit()
    return jsonify(_resource_to_dict(r))


def _file_resource(resource):
    """
    Moves a reviewed resource's file from staging into its NAS
    canonical path (rendered from the current template), and updates
    status/paths accordingly. Import is local to avoid a circular
    import between app.api and jobs.path_template.
    """
    from jobs.path_template import render_path

    if not resource.staging_path or not os.path.exists(resource.staging_path):
        raise FileNotFoundError(
            f"staging file missing for resource {resource.id}: {resource.staging_path}"
        )

    relative_path = render_path(resource, project=resource.project)
    destination = os.path.join(Config.NAS_LIBRARY_ROOT, relative_path)

    os.makedirs(os.path.dirname(destination), exist_ok=True)
    os.rename(resource.staging_path, destination)

    resource.nas_path = destination
    resource.staging_path = None
    resource.status = "filed"
    db.session.add(FileEvent(resource_id=resource.id, event_type="moved", detail=destination))


def _resource_to_dict(r: Resource):
    d = {
        "id": r.id,
        "filename": r.filename,
        "category": r.category,
        "project_id": r.project_id,
        "status": r.status,
        "captured_at": r.captured_at.isoformat() if r.captured_at else None,
        "captured_at_source": r.captured_at_source,
        "duration_seconds": r.duration_seconds,
        "nas_path": r.nas_path,
        "tags": [t.name for t in r.tags],
        "location": (
            {"lat": r.location.lat, "lon": r.location.lon, "source": r.location.source}
            if r.location else None
        ),
        "has_track": len(r.track_points) > 0,
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
            "recorded_at": p.recorded_at.isoformat(),
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
        abort(404, description="audio file not found on disk")
    return send_file(path, conditional=True)


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
        "created_at": c.created_at.isoformat() if c.created_at else None,
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
        "requested_at": e.requested_at.isoformat() if e.requested_at else None,
        "completed_at": e.completed_at.isoformat() if e.completed_at else None,
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
            "taken_at": p.taken_at.isoformat() if p.taken_at else None,
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
    from jobs.immich import fetch_photos_for_recording
    from jobs.retry import ServiceUnavailable

    resource = Resource.query.get_or_404(resource_id)
    if not resource.captured_at:
        return jsonify({"error": "resource has no captured_at to search around"}), 400

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
    from jobs.dawarich import fetch_track_and_pin
    from jobs.retry import ServiceUnavailable

    resource = Resource.query.get_or_404(resource_id)
    if not resource.captured_at:
        return jsonify({"error": "resource has no captured_at to search around"}), 400

    try:
        track_points, pin = fetch_track_and_pin(
            resource.captured_at, resource.duration_seconds, max_attempts=1,
        )
    except ServiceUnavailable as e:
        return jsonify({"error": f"Dawarich unavailable, try again later: {e}"}), 503

    from app.models import TrackPoint
    for point in track_points:
        db.session.add(TrackPoint(
            resource_id=resource.id, recorded_at=point["timestamp"],
            lat=point["lat"], lon=point["lon"],
        ))
    found = False
    if pin:
        loc = resource.location or Location(resource_id=resource.id)
        loc.lat, loc.lon, loc.source = pin["lat"], pin["lon"], "dawarich-auto"
        db.session.add(loc)
        found = True

    resource.dawarich_checked_at = db.func.now()
    db.session.commit()
    return jsonify({"found": found, "track_points": len(track_points)})

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

@bp.get("/projects")
def list_projects():
    return jsonify([{"id": p.id, "name": p.name, "slug": p.slug} for p in Project.query.all()])


@bp.post("/projects")
def create_project():
    data = request.get_json()
    p = Project(name=data["name"], slug=data["slug"], notes=data.get("notes"))
    db.session.add(p)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({"error": f"slug '{data['slug']}' is already in use"}), 400
    return jsonify({"id": p.id}), 201


@bp.get("/tags")
def list_tags():
    return jsonify([{"id": t.id, "name": t.name} for t in Tag.query.all()])


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
            "last_run_at": run.last_run_at.isoformat() if run.last_run_at else None,
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
            "next_attempt_at": current.next_attempt_at.isoformat() if current.next_attempt_at else None,
        }

    return jsonify({"job_name": job_name, "last_result": last_result, "queue": queue_info})


# --- Settings ---
# DB-backed overrides for the credentials/URLs it makes sense to edit
# from a settings page rather than only via env var + restart (see
# app/settings.py for exactly which keys). Everything else in
# config.py is still env-var only.

@bp.get("/settings")
def get_settings():
    snapshot = settings_snapshot()
    snapshot["rclone_drive"] = _rclone_drive_status()
    return jsonify(snapshot)


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
