"""
Filing runs in the background.

Copying, checksumming and reading back a 700 MB recording takes far longer than a web request
can wait (nginx gives up after about a minute), so `PATCH status=filed` only validates and marks
the resource `filing`; this sweeper job does the copy. Same shape as `process-exports`: one queued
job drains every resource that is `filing`.

States: pending-review -> filing -> filed, or -> failed (failure_stage "move", with the reason;
`retry-failed` puts it back to `filing`). A NAS that is not mounted is not a failure of the
recording: the resource stays `filing` and the whole run stops, to be retried later.

Sidecars (`.reapeaks`, `.pkf`) attached to a file (role "sidecar", derived_from = the audio) are
copied into the same NAS folder right after their audio, and never overwrite anything.
"""
import logging
import os
from datetime import datetime

from config import Config
from app.extensions import db
from app.models import Resource, FileEvent, JobRun
from .nas import NasUnavailable, file_to_nas, nas_status
from .path_template import render_path

log = logging.getLogger(__name__)


def _record_run(status, detail):
    run = JobRun.query.get("file-resources") or JobRun(job_name="file-resources")
    run.last_run_at = datetime.utcnow()
    run.status = status
    run.log_tail = detail[-4000:]
    db.session.merge(run)
    db.session.commit()


def tidy_staging_dir(path):
    """Inbox files are staged one per subdirectory (so equal filenames from different folders
    can't collide); remove the emptied directory. Never touches STAGING_DIR itself."""
    parent = os.path.dirname(path)
    inbox_root = os.path.join(Config.STAGING_DIR, "inbox")
    if os.path.dirname(parent) == inbox_root:
        try:
            os.rmdir(parent)
        except OSError:
            pass


def file_resource(resource):
    """Copy one resource (and its attached sidecars) into the library. Raises NasUnavailable,
    FileExistsError or OSError; on any of those nothing was changed for this resource."""
    if not resource.staging_path or not os.path.exists(resource.staging_path):
        raise FileNotFoundError(f"staging file missing for resource {resource.id}: {resource.staging_path}")

    relative_path = render_path(resource, project=resource.project, session=resource.session)
    destination = os.path.join(Config.NAS_LIBRARY_ROOT, relative_path)
    source = resource.staging_path

    # Staging and the NAS are different filesystems, so this is copy -> verify -> delete,
    # not a rename (see jobs/nas.py).
    file_to_nas(source, destination, expected_sha256=resource.checksum)
    tidy_staging_dir(source)

    resource.nas_path = destination
    resource.staging_path = None
    resource.status = "filed"
    resource.failure_stage = None
    resource.failure_detail = None
    db.session.add(FileEvent(resource_id=resource.id, event_type="moved", detail=destination))
    db.session.commit()

    file_attached(resource)


def file_attached(parent):
    """Copy any sidecars attached to an already-filed parent next to it."""
    children = Resource.query.filter(
        Resource.derived_from_id == parent.id, Resource.role == "sidecar", Resource.status == "attached",
    ).all()
    for child in children:
        dest = os.path.join(os.path.dirname(parent.nas_path), child.filename)
        try:
            file_to_nas(child.staging_path, dest, expected_sha256=child.checksum)
        except NasUnavailable:
            raise
        except (FileExistsError, OSError, FileNotFoundError) as e:
            # A sidecar is regenerable and never worth failing the recording for: note it, move on.
            child.failure_detail = f"could not copy next to {parent.filename}: {e}"
            db.session.add(FileEvent(resource_id=child.id, event_type="failed", detail=child.failure_detail))
            db.session.commit()
            continue
        tidy_staging_dir(child.staging_path or "")
        child.nas_path, child.staging_path, child.status = dest, None, "filed"
        child.failure_detail = None
        db.session.add(FileEvent(resource_id=child.id, event_type="moved", detail=dest))
        db.session.commit()


def file_resources():
    """Drain every resource that is `filing`. Returns a small summary dict."""
    ok, reason = nas_status()
    if not ok:
        _record_run("error", f"NAS unavailable, nothing filed (they stay 'filing'): {reason}")
        return {"status": "error", "detail": f"NAS unavailable: {reason}"}

    filed, failed, skipped = [], [], []
    while True:
        ids = [rid for (rid,) in db.session.query(Resource.id).filter_by(status="filing").order_by(Resource.created_at).all()
               if rid not in filed and rid not in failed and rid not in skipped]
        if not ids:
            break
        for rid in ids:
            resource = db.session.get(Resource, rid)   # re-read each one: it may have changed or vanished meanwhile
            if resource is None or resource.status != "filing":
                skipped.append(rid)                     # deleted, or already dealt with: nothing to do
                continue
            try:
                file_resource(resource)
                filed.append(rid)
            except NasUnavailable as e:
                db.session.rollback()
                _record_run("error", f"NAS became unavailable part-way: {e}. filed={len(filed)}")
                return {"status": "error", "detail": str(e), "filed": filed, "failed": failed}
            except Exception as e:  # noqa: BLE001 -- one bad file must not stop the rest
                db.session.rollback()
                failed.append(rid)
                log.warning("filing %s failed: %s", rid, e)
                resource = db.session.get(Resource, rid)
                if resource is None:
                    continue
                resource.status = "failed"
                resource.failure_stage = "move"
                resource.failure_detail = str(e)
                db.session.add(FileEvent(resource_id=rid, event_type="failed", detail=f"filing failed: {e}"))
                try:
                    db.session.commit()
                except Exception:  # noqa: BLE001
                    db.session.rollback()

    # Sidecars that arrived while their audio was mid-filing (or that failed once): copy them now.
    for parent in Resource.query.filter(Resource.status == "filed", Resource.role.in_(("original", "edit")),
                                        Resource.id.in_(db.session.query(Resource.derived_from_id).filter(
                                            Resource.role == "sidecar", Resource.status == "attached"))).all():
        try:
            file_attached(parent)
        except NasUnavailable:
            break

    status = "partial" if failed else "success"
    _record_run(status, f"filed: {len(filed)}, failed: {len(failed)}")
    return {"status": status, "filed": filed, "failed": failed}
