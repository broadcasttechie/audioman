"""
rclone-backed jobs. Every subprocess call carries an explicit timeout
— rclone hanging on a flaky connection must never hang this job
indefinitely. Per-file failures in drive_inbox_pull are isolated: one
stuck/failed file logs and gets skipped (retried next run), it doesn't
abort the rest of the batch.
"""
import json
import os
import subprocess
from datetime import datetime, timedelta

from config import Config
from app.extensions import db
from app.models import PendingUpload, FileEvent, JobRun
from .ingest import ingest_staged_file, _fail


def _record_run(job_name, status, log_tail=""):
    run = JobRun.query.get(job_name) or JobRun(job_name=job_name)
    run.last_run_at = datetime.utcnow()
    run.status = status
    run.log_tail = log_tail[-4000:]
    db.session.merge(run)
    db.session.commit()


def _rclone_lsjson(remote_path):
    result = subprocess.run(
        ["rclone", "lsjson", remote_path],
        capture_output=True, text=True, check=True,
        timeout=Config.RCLONE_LIST_TIMEOUT_SECONDS,
    )
    return json.loads(result.stdout)


def drive_inbox_pull():
    """
    Two-poll stability check before anything is pulled: a file must be
    unchanged in size+modtime across two consecutive calls of this job,
    AND older than MIN_AGE_MINUTES, before it's eligible for
    `rclone move`. Prevents pulling (and later deleting from Drive) a
    file that's still mid-upload.
    """
    remote_path = f"{Config.RCLONE_DRIVE_REMOTE}:{Config.DRIVE_INBOX_PATH}"
    try:
        entries = _rclone_lsjson(remote_path)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        _record_run("drive-inbox-pull", "error", str(e))
        return {"status": "error", "detail": str(e)}

    pulled, failed = [], []
    now = datetime.utcnow()

    for entry in entries:
        path = entry["Path"]
        size = entry["Size"]
        modtime = entry["ModTime"]

        previous = PendingUpload.query.get(path)
        unchanged = previous and previous.size == size and previous.modtime == modtime

        if unchanged:
            age = now - previous.first_seen_at
            if age >= timedelta(minutes=Config.MIN_AGE_MINUTES):
                try:
                    subprocess.run(
                        ["rclone", "move", f"{remote_path}/{path}", Config.STAGING_DIR],
                        check=True, capture_output=True, text=True,
                        timeout=Config.RCLONE_TRANSFER_TIMEOUT_SECONDS,
                    )
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
                    # Leave `previous` in place — retried next run.
                    # This one file's trouble doesn't stop the batch.
                    failed.append(path)
                    db.session.add(FileEvent(
                        event_type="failed",
                        detail=f"rclone move failed for {path}, will retry next run: {e}",
                    ))
                    db.session.commit()
                    continue

                pulled.append(path)
                db.session.delete(previous)
                db.session.commit()

                local_path = os.path.join(Config.STAGING_DIR, os.path.basename(path))
                try:
                    ingest_staged_file(local_path, drive_inbox_path=path)
                except Exception as e:
                    # Catch-all so a bug in ingest can never leave a
                    # file sitting in STAGING_DIR with no Resource row
                    # and no record of what happened to it.
                    _fail(None, local_path, "metadata-extraction", f"unexpected ingest error: {e}")
                continue

        # Either still within MIN_AGE_MINUTES (unchanged, keep counting
        # from the original first_seen_at) or new/changed since the
        # last poll (restart the stability clock at now).
        db.session.merge(PendingUpload(
            path=path, size=size, modtime=modtime,
            first_seen_at=previous.first_seen_at if unchanged else now,
        ))

    db.session.commit()
    status = "partial" if failed else "success"
    _record_run("drive-inbox-pull", status, f"pulled: {pulled}, failed: {failed}")
    return {"status": status, "pulled": pulled, "failed": failed}


def nas_to_drive_library():
    """
    NAS is source of truth — this is a true mirror (rclone sync, not
    copy), so re-filing/renaming on the NAS side removes/moves the
    corresponding file in Drive `/Library` too.
    """
    remote_path = f"{Config.RCLONE_DRIVE_REMOTE}:{Config.DRIVE_LIBRARY_PATH}"
    try:
        subprocess.run(
            ["rclone", "sync", Config.NAS_LIBRARY_ROOT, remote_path],
            check=True, capture_output=True, text=True,
            timeout=Config.RCLONE_TRANSFER_TIMEOUT_SECONDS,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        _record_run("nas-to-drive-library", "error", str(e))
        return {"status": "error", "detail": str(e)}

    _record_run("nas-to-drive-library", "success")
    return {"status": "success"}


def library_verify():
    """Drift check only, no transfer — feeds jobs.maintenance.find_orphans."""
    remote_path = f"{Config.RCLONE_DRIVE_REMOTE}:{Config.DRIVE_LIBRARY_PATH}"
    try:
        result = subprocess.run(
            ["rclone", "check", Config.NAS_LIBRARY_ROOT, remote_path],
            capture_output=True, text=True,
            timeout=Config.RCLONE_TRANSFER_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as e:
        _record_run("library-verify", "error", str(e))
        return {"status": "error", "detail": str(e)}

    status = "success" if result.returncode == 0 else "partial"
    _record_run("library-verify", status, result.stdout + result.stderr)
    return {"status": status, "detail": result.stdout}
