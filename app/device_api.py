"""
The Android app's API: everything under /api/device/v1/, gated by a per-device token
(X-Device-Token, app/device_auth.py) instead of the web UI's plain VPN trust — this is the one
surface most likely to leave the VPN one day (PLAN 16/19). Kept as its own versioned prefix so the
app and server can move independently of the browser-facing /api/*.

Browse, playback and export routes are deliberately thin wrappers that call straight into the
existing app.api view functions (list_resources, get_waveform, create_export, ...) rather than
reimplementing them — one behaviour, two doors. Only what's genuinely new to the app lives here:
the chunked/resumable upload protocol and the metadata-at-upload resolution (PLAN 19.3/19.5).

Upload protocol:
  POST   /uploads                  declare a file (name, size, sha256, metadata) -> upload_id,
                                    or an immediate {"status":"duplicate"} if that checksum is
                                    already a resource, or 503 if there isn't room right now.
  PUT    /uploads/<id>/chunk?offset=N   raw bytes, must start exactly at bytes_received so far;
                                    a 409 names the correct offset to resume from.
  GET    /uploads/<id>             current bytes_received (for resuming after a reconnect).
  POST   /uploads/<id>/complete    verifies the assembled file's checksum, then ingests it.
"""
import os
import shutil

from flask import Blueprint, jsonify, request, g, abort

from config import Config
from app.extensions import db
from app.models import Resource, Project, RecordingSession, Tag, UploadSession
from app.timeutil import to_utc_iso, parse_to_utc_naive
from app.device_auth import require_device_token
from . import api as web_api
from jobs import disk_budget

device_bp = Blueprint("device", __name__)


# --- discovery -----------------------------------------------------------------------------

@device_bp.get("/ping")
@require_device_token
def ping():
    from datetime import datetime
    return jsonify({"ok": True, "device": g.device.label, "server_time": to_utc_iso(datetime.utcnow())})


# --- browse: thin delegation to the same handlers the web UI uses --------------------------

@device_bp.get("/projects")
@require_device_token
def projects():
    return web_api.list_projects()


@device_bp.post("/projects")
@require_device_token
def create_project():
    return web_api.create_project()


@device_bp.get("/sessions")
@require_device_token
def sessions():
    return web_api.list_sessions()


@device_bp.post("/sessions")
@require_device_token
def create_session():
    return web_api.create_session()


@device_bp.get("/tags")
@require_device_token
def tags():
    return web_api.list_tags()


@device_bp.get("/categories")
@require_device_token
def categories():
    return web_api.list_categories()


@device_bp.get("/library")
@require_device_token
def library():
    return web_api.list_resources()


@device_bp.get("/resources/<resource_id>")
@require_device_token
def resource_detail(resource_id):
    return web_api.get_resource(resource_id)


# --- playback: same files the web player streams --------------------------------------------

@device_bp.get("/resources/<resource_id>/waveform")
@require_device_token
def waveform(resource_id):
    return web_api.get_waveform(resource_id)


@device_bp.get("/resources/<resource_id>/preview")
@require_device_token
def preview(resource_id):
    return web_api.get_preview(resource_id)


@device_bp.get("/resources/<resource_id>/audio")
@require_device_token
def audio(resource_id):
    return web_api.stream_audio(resource_id)


# --- download with conversion: the existing export workflow ---------------------------------

@device_bp.post("/resources/<resource_id>/export")
@require_device_token
def create_export(resource_id):
    return web_api.create_export(resource_id)


@device_bp.get("/exports/<export_id>")
@require_device_token
def export_status(export_id):
    return web_api.get_export(export_id)


@device_bp.get("/exports/<export_id>/download")
@require_device_token
def export_download(export_id):
    return web_api.download_export(export_id)


# --- fast dedupe: "have you already got this file?" -----------------------------------------

@device_bp.post("/checksums/query")
@require_device_token
def checksums_query():
    data = request.get_json() or {}
    checksums = data.get("checksums")
    if not isinstance(checksums, list) or not checksums or len(checksums) > 500 or not all(isinstance(c, str) for c in checksums):
        return jsonify({"error": "checksums must be a list of 1 to 500 sha256 strings"}), 400
    found = {r.checksum: r.id for r in Resource.query.filter(Resource.checksum.in_(checksums)).all()}
    return jsonify({c: ({"exists": True, "resource_id": found[c]} if c in found else {"exists": False}) for c in checksums})


# --- chunked/resumable upload -----------------------------------------------------------------

def _upload_session_dict(s):
    return {
        "upload_id": s.id, "status": s.status, "filename": s.filename,
        "bytes_received": s.bytes_received, "declared_size": s.declared_size,
        "chunk_max_bytes": Config.DEVICE_UPLOAD_CHUNK_MAX_BYTES,
        "resource_id": s.resource_id, "error": s.error_detail,
    }


def _safe_leaf_filename(filename):
    """The basename only, rejecting anything that isn't a real file name on its own -- bounds
    where the upload is written on disk. The Resource's own `filename` is set from this same
    value (never altered afterwards), consistent with jobs/path_template.py's "original filename
    is never changed": a name containing a path separator was never a legitimate one to begin with."""
    base = os.path.basename(str(filename or ""))
    if not base or base in (".", "..") or "\x00" in base:
        return None
    return base


def _resolve_upload_metadata(raw):
    """Validates the app's metadata block and resolves it to stored ids/values. Returns
    (resolved_dict, None) or (None, error_message). Tag lookup/creation is done last, once
    nothing else can fail, so a rejected request never leaves a stray tag behind."""
    meta = raw if isinstance(raw, dict) else {}
    out = {}

    project_id, session_id = meta.get("project_id"), meta.get("session_id")
    if session_id:
        rsession = db.session.get(RecordingSession, session_id)
        if rsession is None:
            return None, "unknown session_id"
        if project_id and project_id != rsession.project_id:
            return None, "metadata.project_id does not match metadata.session_id's project"
        project_id = rsession.project_id
    elif project_id and db.session.get(Project, project_id) is None:
        return None, "unknown project_id"
    out["project_id"], out["session_id"] = project_id, session_id

    category = meta.get("category")
    if category is not None:
        from app import categories as cats
        allowed = cats.active_slugs()
        if category not in allowed:
            return None, f"category must be one of {sorted(allowed)}"
    out["category"] = category

    for field in ("title", "notes", "source_path", "recorder_hint"):
        if meta.get(field) is not None:
            if not isinstance(meta[field], str):
                return None, f"{field} must be text"
            out[field] = meta[field]

    if meta.get("captured_at"):
        try:
            parse_to_utc_naive(meta["captured_at"])
        except ValueError:
            return None, "captured_at must be an ISO 8601 date-time, e.g. 2026-09-17T08:11:24Z"
        out["captured_at"] = meta["captured_at"]
        precision = meta.get("captured_at_precision", "approximate")
        if precision not in ("exact", "approximate"):
            return None, "captured_at_precision must be 'exact' or 'approximate'"
        out["captured_at_precision"] = precision

    tag_names = meta.get("tags")
    if tag_names:
        if not isinstance(tag_names, list) or not all(isinstance(t, str) and t.strip() for t in tag_names):
            return None, "tags must be a list of non-empty tag names"
        tag_ids = []
        for name in dict.fromkeys(t.strip() for t in tag_names):
            tag = Tag.query.filter(db.func.lower(Tag.name) == name.lower()).first()
            if tag is None:
                tag = Tag(name=name)
                db.session.add(tag)
                db.session.flush()
            tag_ids.append(tag.id)
        out["tag_ids"] = tag_ids

    return out, None


@device_bp.post("/uploads")
@require_device_token
def initiate_upload():
    data = request.get_json() or {}
    filename, size_bytes, checksum = data.get("filename"), data.get("size_bytes"), data.get("checksum")

    leaf = _safe_leaf_filename(filename)
    if leaf is None:
        return jsonify({"error": "filename is required and must be a real file name"}), 400
    if not isinstance(size_bytes, int) or isinstance(size_bytes, bool) or size_bytes <= 0:
        return jsonify({"error": "size_bytes must be a positive integer"}), 400
    if not isinstance(checksum, str) or len(checksum) != 64 or not all(c in "0123456789abcdefABCDEF" for c in checksum):
        return jsonify({"error": "checksum must be a sha256 hex string (compute it while reading the file)"}), 400
    checksum = checksum.lower()

    new_id = data.get("id")
    if new_id is not None:
        if not web_api._valid_uuid(new_id):
            return jsonify({"error": "id must be a UUID"}), 400
        existing_session = db.session.get(UploadSession, new_id)
        if existing_session and existing_session.device_id == g.device.id:
            return jsonify(_upload_session_dict(existing_session)), 200

    existing_resource = Resource.query.filter_by(checksum=checksum).first()
    if existing_resource:
        return jsonify({"status": "duplicate", "resource_id": existing_resource.id}), 200

    meta, error = _resolve_upload_metadata(data.get("metadata"))
    if error:
        db.session.rollback()
        return jsonify({"error": error}), 400

    verdict, reason = disk_budget.admit(
        size_bytes, shutil.disk_usage(Config.STAGING_DIR).free, disk_budget.staged_bytes(Config.STAGING_DIR),
        Config.DISK_RESERVE_GB * disk_budget.GB, Config.STAGING_BUDGET_GB * disk_budget.GB,
    )
    if verdict == disk_budget.NEVER:
        db.session.rollback()
        return jsonify({"error": reason}), 413
    if verdict == disk_budget.WAIT:
        db.session.rollback()
        return jsonify({"error": reason, "retry": True}), 503

    session = UploadSession(
        device_id=g.device.id, filename=leaf, declared_size=size_bytes, declared_checksum=checksum,
        metadata_json=meta, bytes_received=0, status="uploading",
    )
    if new_id is not None:
        session.id = new_id
    db.session.add(session)
    db.session.flush()   # need session.id before building its staging path

    out_dir = os.path.join(Config.DEVICE_UPLOAD_DIR, session.id)
    os.makedirs(out_dir, exist_ok=True)
    session.staging_path = os.path.join(out_dir, leaf)
    open(session.staging_path, "wb").close()
    db.session.commit()
    return jsonify(_upload_session_dict(session)), 201


@device_bp.put("/uploads/<upload_id>/chunk")
@require_device_token
def upload_chunk(upload_id):
    session = db.session.get(UploadSession, upload_id)
    if session is None or session.device_id != g.device.id:
        abort(404)
    if session.status != "uploading":
        return jsonify({"error": f"this upload is '{session.status}', not accepting more data"}), 410

    try:
        offset = int(request.args.get("offset", ""))
    except ValueError:
        return jsonify({"error": "an integer ?offset= query parameter is required"}), 400
    if offset != session.bytes_received:
        return jsonify({"error": "offset does not match what has been received so far",
                        "bytes_received": session.bytes_received}), 409

    content_length = request.content_length
    if content_length is None or content_length == 0:
        return jsonify({"error": "a non-empty request body is required"}), 400
    if content_length > Config.DEVICE_UPLOAD_CHUNK_MAX_BYTES:
        return jsonify({"error": f"a chunk must be at most {Config.DEVICE_UPLOAD_CHUNK_MAX_BYTES} bytes; send more, smaller chunks"}), 413
    if session.bytes_received + content_length > session.declared_size:
        return jsonify({"error": "this chunk would exceed the size_bytes declared when the upload was started"}), 400

    body = request.get_data(cache=False)
    with open(session.staging_path, "ab") as f:
        f.write(body)
    session.bytes_received += len(body)
    db.session.commit()
    return jsonify({"bytes_received": session.bytes_received, "declared_size": session.declared_size})


@device_bp.get("/uploads/<upload_id>")
@require_device_token
def upload_status(upload_id):
    session = db.session.get(UploadSession, upload_id)
    if session is None or session.device_id != g.device.id:
        abort(404)
    return jsonify(_upload_session_dict(session))


@device_bp.post("/uploads/<upload_id>/complete")
@require_device_token
def complete_upload(upload_id):
    from jobs.ingest import _sha256
    from jobs.device_uploads import ingest_device_upload, _cleanup_file

    session = db.session.get(UploadSession, upload_id)
    if session is None or session.device_id != g.device.id:
        abort(404)
    if session.status == "completed":
        return jsonify(web_api._resource_to_dict(db.session.get(Resource, session.resource_id))), 200
    if session.status != "uploading":
        return jsonify({"error": f"this upload is '{session.status}'"}), 410
    if session.bytes_received != session.declared_size:
        return jsonify({"error": "not all bytes have been received yet",
                        "bytes_received": session.bytes_received, "declared_size": session.declared_size}), 409

    actual = _sha256(session.staging_path)
    if actual != session.declared_checksum:
        _cleanup_file(session.staging_path)
        session.staging_path, session.status = None, "failed"
        session.error_detail = "the assembled file's checksum does not match what was declared; re-initiate the upload"
        db.session.commit()
        return jsonify({"error": session.error_detail}), 422

    try:
        resource = ingest_device_upload(session)
    except Exception as e:  # noqa: BLE001 -- a bad upload must fail cleanly, not crash the request
        db.session.rollback()
        s = db.session.get(UploadSession, upload_id)
        if s:
            s.status, s.error_detail = "failed", str(e)[:2000]
            db.session.commit()
        return jsonify({"error": f"could not finish this upload: {e}"}), 500

    session = db.session.get(UploadSession, upload_id)
    session.status, session.resource_id, session.error_detail = "completed", resource.id, None
    db.session.commit()
    return jsonify(web_api._resource_to_dict(resource)), 201
