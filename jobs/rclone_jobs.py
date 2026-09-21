"""
rclone-backed jobs. Every subprocess call carries an explicit timeout
— rclone hanging on a flaky connection must never hang this job
indefinitely. Per-file failures in drive_inbox_pull are isolated: one
stuck/failed file logs and gets skipped (retried next run), it doesn't
abort the rest of the batch.
"""
import hashlib
import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta

from config import Config
from app.extensions import db
from app.models import PendingUpload, FileEvent, JobRun, Resource
from .ingest import ingest_staged_file, ingest_sidecar, _fail
from .inbox_rules import classify, is_processed_path, sidecar_target, sidecar_matches
from . import disk_budget
from .nas import nas_status


def _cmd_error_detail(e):
    """
    str(CalledProcessError) is just "Command '[...]' returned non-zero
    exit status N" -- no stderr, which is the part that actually says
    why (e.g. Google's "insufficientFilePermissions"). First noticed
    this the hard way debugging a real ingest failure with nothing
    useful in file_events/JobRun to go on.
    """
    stderr = getattr(e, "stderr", None)
    return f"{e}: {stderr.strip()}" if stderr else str(e)


def _record_run(job_name, status, log_tail=""):
    run = JobRun.query.get(job_name) or JobRun(job_name=job_name)
    run.last_run_at = datetime.utcnow()
    run.status = status
    run.log_tail = log_tail[-4000:]
    db.session.merge(run)
    db.session.commit()


def _rclone_lsjson(remote_path, files_only=False, recursive=False):
    cmd = ["rclone", "lsjson", remote_path]
    if files_only:
        cmd.append("--files-only")
    if recursive:
        cmd.append("-R")
    result = subprocess.run(
        cmd, capture_output=True, text=True, check=True,
        timeout=Config.RCLONE_LIST_TIMEOUT_SECONDS,
    )
    return json.loads(result.stdout)


def _stage_dir_for(path):
    """One staging directory per Inbox file: `STE-000.wav` exists in several Inbox folders, and a shared
    flat staging directory would let one overwrite the other."""
    return os.path.join(Config.STAGING_DIR, "inbox", hashlib.sha1(path.encode("utf-8")).hexdigest()[:12])


def _find_sidecar_parent(path):
    """The audio Resource a sidecar's Inbox path points at, or None (its audio hasn't arrived yet)."""
    target = sidecar_target(path)
    key = target[1] or target[2]
    if not key:
        return None
    like = key.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    candidates = Resource.query.filter(
        Resource.drive_inbox_path.isnot(None), Resource.role.in_(("original", "edit")),
        db.func.lower(Resource.filename).like(like, escape="\\"),
    ).all()
    return next((c for c in candidates if sidecar_matches(c.drive_inbox_path, target)), None)


def drive_inbox_pull():
    """
    Pull everything under Drive /Inbox (subfolders included) into staging and ingest it.

    Two-poll stability check before anything is pulled: a file must be unchanged in size+modtime
    across two consecutive calls of this job, AND older than MIN_AGE_MINUTES, before it's eligible.
    Prevents pulling a file that's still mid-upload.

    What is done with a file depends on jobs/inbox_rules.classify: audio is ingested; sidecars
    (`.reapeaks`, `.pkf`) are attached to their audio (and held in the Inbox until it has arrived);
    project files, junk and unknown types are left alone and reported. `_processed` is never scanned.

    Pulled files are copied out, then moved (re-parented) into Inbox/_processed rather than deleted
    from Drive. This isn't a style choice: confirmed live against a real 403
    insufficientFilePermissions error that on a personal (non-Workspace) Google account, Editor
    sharing lets a non-owner read/write a file but not delete it -- Shared Drives (where a Content
    Manager genuinely can delete) are a Workspace-only feature. Re-parenting a file you don't own
    works fine under Editor; deleting it generally doesn't.
    """
    remote_path = f"{Config.RCLONE_DRIVE_REMOTE}:{Config.DRIVE_INBOX_PATH}"
    try:
        entries = _rclone_lsjson(remote_path, files_only=True, recursive=True)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        detail = _cmd_error_detail(e)
        _record_run("drive-inbox-pull", "error", detail)
        return {"status": "error", "detail": detail}

    pulled, failed, deferred = [], [], []
    held_project_files, ignored = [], 0
    now = datetime.utcnow()
    pending = {p.path: p for p in PendingUpload.query.all()}

    work = []
    for entry in entries:
        path = entry["Path"]
        if is_processed_path(path):
            continue
        kind = classify(path)
        if kind == "project-file":
            held_project_files.append(path)
        elif kind in ("ignore", "unknown"):
            ignored += 1
        else:
            work.append((kind, entry))
    # Audio before sidecars (a sidecar needs its audio to exist), then oldest-seen first, so a bulk
    # drop drains in arrival order and a big file can't be starved by smaller ones behind it.
    work.sort(key=lambda w: (w[0] != "audio",
                             pending[w[1]["Path"]].first_seen_at if w[1]["Path"] in pending else now,
                             w[1]["Path"]))

    os.makedirs(Config.STAGING_DIR, exist_ok=True)
    reserve = Config.DISK_RESERVE_GB * disk_budget.GB
    budget = Config.STAGING_BUDGET_GB * disk_budget.GB
    staged = disk_budget.staged_bytes(Config.STAGING_DIR)
    held_reason = None  # once one file has to wait, everything behind it waits too

    for kind, entry in work:
        path = entry["Path"]
        size = entry["Size"]
        modtime = entry["ModTime"]

        previous = pending.get(path)
        unchanged = previous and previous.size == size and previous.modtime == modtime

        if unchanged and now - previous.first_seen_at >= timedelta(minutes=Config.MIN_AGE_MINUTES):
            parent = None
            if kind == "sidecar":
                parent = _find_sidecar_parent(path)
                if parent is None:
                    deferred.append(f"{path} (waiting for its audio file to arrive)")
                    continue

            # Disk admission. A file that doesn't fit is left alone in the Inbox (its PendingUpload
            # row is kept, so its stability clock and its place in the queue survive).
            if held_reason is None and len(pulled) >= Config.INBOX_MAX_FILES_PER_RUN:
                held_reason = f"per-run cap of {Config.INBOX_MAX_FILES_PER_RUN} files reached"
            if held_reason is None:
                verdict, reason = disk_budget.admit(
                    size, shutil.disk_usage(Config.STAGING_DIR).free, staged, reserve, budget)
                if verdict == disk_budget.NEVER:
                    # Would block the queue forever; skip it loudly, keep going.
                    deferred.append(f"{path} ({reason})")
                    continue
                if verdict == disk_budget.WAIT:
                    held_reason = reason
            if held_reason is not None:
                deferred.append(f"{path} ({held_reason})")
                continue

            stage_dir = _stage_dir_for(path)
            local_path = os.path.join(stage_dir, os.path.basename(path))
            try:
                os.makedirs(stage_dir, exist_ok=True)
                subprocess.run(
                    ["rclone", "copy", f"{remote_path}/{path}", stage_dir],
                    check=True, capture_output=True, text=True,
                    timeout=Config.RCLONE_TRANSFER_TIMEOUT_SECONDS,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
                # Leave `previous` in place — retried next run. This one file's trouble doesn't stop the batch.
                failed.append(path)
                db.session.add(FileEvent(
                    event_type="failed",
                    detail=f"rclone copy failed for {path}, will retry next run: {_cmd_error_detail(e)}",
                ))
                db.session.commit()
                continue

            pulled.append(path)
            staged += size
            db.session.delete(previous)
            db.session.commit()

            try:
                if kind == "sidecar":
                    ingest_sidecar(local_path, path, parent)
                else:
                    ingest_staged_file(local_path, drive_inbox_path=path)
            except Exception as e:
                # Catch-all so a bug in ingest can never leave a file sitting in staging with no
                # Resource row and no record of what happened to it.
                db.session.rollback()
                _fail(None, local_path, "metadata-extraction", f"unexpected ingest error: {e}")

            # Best-effort tidy-up, not a correctness requirement: the file is already safely ingested
            # locally at this point regardless of what happens below. A failure here just means this
            # file gets re-copied and re-detected-as-duplicate (quarantined, not duplicated — see
            # jobs/ingest.py) on every future poll until someone notices and fixes it by hand.
            # NEEDS_ATTENTION: exactly the kind of silently-stuck state the cleanup-visibility
            # mechanism noted in DEPLOYMENT.md is meant to surface — not built yet, so for now it
            # only shows up here in file_events.
            try:
                subprocess.run(
                    ["rclone", "moveto", f"{remote_path}/{path}", f"{remote_path}/_processed/{path}"],
                    check=True, capture_output=True, text=True,
                    timeout=Config.RCLONE_TRANSFER_TIMEOUT_SECONDS,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
                db.session.add(FileEvent(
                    event_type="failed",
                    detail=(
                        f"ingested {path} successfully, but couldn't move it to "
                        f"Inbox/_processed afterwards (will keep re-copying and "
                        f"quarantining-as-duplicate until this is fixed): {_cmd_error_detail(e)}"
                    ),
                ))
                db.session.commit()
            continue

        # Either still within MIN_AGE_MINUTES (unchanged, keep counting from the original
        # first_seen_at) or new/changed since the last poll (restart the stability clock at now).
        db.session.merge(PendingUpload(
            path=path, size=size, modtime=modtime,
            first_seen_at=previous.first_seen_at if unchanged else now,
        ))

    db.session.commit()
    status = "partial" if failed else "success"
    log = f"pulled: {pulled}, failed: {failed}"
    if deferred:
        log += f", held back in Inbox ({len(deferred)}): {deferred[:5]}"
    if held_project_files:
        log += f", project files left in Inbox ({len(held_project_files)}): {held_project_files[:5]}"
    if ignored:
        log += f", ignored {ignored} non-audio file(s)"
    _record_run("drive-inbox-pull", status, log)
    return {"status": status, "pulled": pulled, "failed": failed, "deferred": deferred,
            "held_project_files": held_project_files, "ignored": ignored}


def nas_to_drive_library():
    """
    NAS is source of truth — this is a true mirror (rclone sync, not
    copy), so re-filing/renaming on the NAS side removes/moves the
    corresponding file in Drive `/Library` too.
    """
    remote_path = f"{Config.RCLONE_DRIVE_REMOTE}:{Config.DRIVE_LIBRARY_PATH}"
    nas_ok, nas_reason = nas_status()
    if not nas_ok:
        # Critical: `rclone sync` from an empty unmounted directory would DELETE the Drive copy.
        detail = f"NAS unavailable, sync skipped: {nas_reason}"
        _record_run("nas-to-drive-library", "error", detail)
        return {"status": "error", "detail": detail}
    try:
        subprocess.run(
            ["rclone", "sync", Config.NAS_LIBRARY_ROOT, remote_path],
            check=True, capture_output=True, text=True,
            timeout=Config.RCLONE_TRANSFER_TIMEOUT_SECONDS,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        detail = _cmd_error_detail(e)
        _record_run("nas-to-drive-library", "error", detail)
        return {"status": "error", "detail": detail}

    _record_run("nas-to-drive-library", "success")
    return {"status": "success"}


def library_verify():
    """Drift check only, no transfer — feeds jobs.maintenance.find_orphans."""
    remote_path = f"{Config.RCLONE_DRIVE_REMOTE}:{Config.DRIVE_LIBRARY_PATH}"
    nas_ok, nas_reason = nas_status()
    if not nas_ok:
        detail = f"NAS unavailable, check skipped: {nas_reason}"
        _record_run("library-verify", "error", detail)
        return {"status": "error", "detail": detail}
    try:
        result = subprocess.run(
            ["rclone", "check", Config.NAS_LIBRARY_ROOT, remote_path],
            capture_output=True, text=True,
            timeout=Config.RCLONE_TRANSFER_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as e:
        detail = _cmd_error_detail(e)
        _record_run("library-verify", "error", detail)
        return {"status": "error", "detail": detail}

    status = "success" if result.returncode == 0 else "partial"
    _record_run("library-verify", status, result.stdout + result.stderr)
    return {"status": status, "detail": result.stdout}
