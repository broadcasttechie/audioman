"""
rclone-backed jobs. Every subprocess call carries an explicit timeout
— rclone hanging on a flaky connection must never hang this job
indefinitely. Per-file failures in drive_inbox_pull are isolated: one
stuck/failed file logs and gets skipped (retried next run), it doesn't
abort the rest of the batch.
"""
import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta

from config import Config
from app.extensions import db
from app.models import PendingUpload, FileEvent, JobRun
from .ingest import ingest_staged_file, _fail
from . import disk_budget


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


def _rclone_lsjson(remote_path, files_only=False):
    cmd = ["rclone", "lsjson", remote_path]
    if files_only:
        cmd.append("--files-only")
    result = subprocess.run(
        cmd, capture_output=True, text=True, check=True,
        timeout=Config.RCLONE_LIST_TIMEOUT_SECONDS,
    )
    return json.loads(result.stdout)


def drive_inbox_pull():
    """
    Two-poll stability check before anything is pulled: a file must be
    unchanged in size+modtime across two consecutive calls of this job,
    AND older than MIN_AGE_MINUTES, before it's eligible to be pulled.
    Prevents pulling a file that's still mid-upload.

    Pulled files are copied out, then moved (re-parented) into
    Inbox/_processed rather than deleted from Drive. This isn't a
    style choice: confirmed live against a real 403
    insufficientFilePermissions error that on a personal (non-
    Workspace) Google account, Editor sharing lets a non-owner
    read/write a file but not delete it -- Shared Drives (where a
    Content Manager genuinely can delete) are a Workspace-only
    feature. Re-parenting a file you don't own works fine under
    Editor; deleting it generally doesn't.
    """
    remote_path = f"{Config.RCLONE_DRIVE_REMOTE}:{Config.DRIVE_INBOX_PATH}"
    try:
        # files_only: the _processed subfolder this job moves ingested
        # files into (see docstring) shows up as a directory entry in
        # this same listing otherwise, and gets treated like a file
        # with a bogus negative size if not filtered out.
        entries = _rclone_lsjson(remote_path, files_only=True)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        detail = _cmd_error_detail(e)
        _record_run("drive-inbox-pull", "error", detail)
        return {"status": "error", "detail": detail}

    pulled, failed, deferred = [], [], []
    now = datetime.utcnow()

    # Oldest-seen first, so a bulk drop is drained in arrival order and a
    # big file can't be starved by smaller ones that arrive after it.
    pending = {p.path: p for p in PendingUpload.query.all()}
    entries.sort(key=lambda e: (pending[e["Path"]].first_seen_at if e["Path"] in pending else now, e["Path"]))

    os.makedirs(Config.STAGING_DIR, exist_ok=True)
    reserve = Config.DISK_RESERVE_GB * disk_budget.GB
    budget = Config.STAGING_BUDGET_GB * disk_budget.GB
    staged = disk_budget.staged_bytes(Config.STAGING_DIR)
    held_reason = None  # once one file has to wait, everything behind it waits too

    for entry in entries:
        path = entry["Path"]
        size = entry["Size"]
        modtime = entry["ModTime"]

        previous = pending.get(path)
        unchanged = previous and previous.size == size and previous.modtime == modtime

        if unchanged:
            age = now - previous.first_seen_at
            if age >= timedelta(minutes=Config.MIN_AGE_MINUTES):
                # Disk admission. A file that doesn't fit is left alone
                # in the Inbox (its PendingUpload row is kept, so its
                # stability clock and its place in the queue survive).
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

                try:
                    subprocess.run(
                        ["rclone", "copy", f"{remote_path}/{path}", Config.STAGING_DIR],
                        check=True, capture_output=True, text=True,
                        timeout=Config.RCLONE_TRANSFER_TIMEOUT_SECONDS,
                    )
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
                    # Leave `previous` in place — retried next run.
                    # This one file's trouble doesn't stop the batch.
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

                local_path = os.path.join(Config.STAGING_DIR, os.path.basename(path))
                try:
                    ingest_staged_file(local_path, drive_inbox_path=path)
                except Exception as e:
                    # Catch-all so a bug in ingest can never leave a
                    # file sitting in STAGING_DIR with no Resource row
                    # and no record of what happened to it.
                    _fail(None, local_path, "metadata-extraction", f"unexpected ingest error: {e}")

                # Best-effort tidy-up, not a correctness requirement:
                # the file is already safely ingested locally at this
                # point regardless of what happens below. A failure
                # here just means this file gets re-copied and
                # re-detected-as-duplicate (quarantined, not
                # duplicated — see jobs/ingest.py) on every future
                # poll until someone notices and fixes it by hand.
                # NEEDS_ATTENTION: this is exactly the kind of
                # silently-stuck state the cleanup-visibility
                # mechanism noted in DEPLOYMENT.md is meant to surface
                # — not built yet, so for now it only shows up here in
                # file_events.
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

        # Either still within MIN_AGE_MINUTES (unchanged, keep counting
        # from the original first_seen_at) or new/changed since the
        # last poll (restart the stability clock at now).
        db.session.merge(PendingUpload(
            path=path, size=size, modtime=modtime,
            first_seen_at=previous.first_seen_at if unchanged else now,
        ))

    db.session.commit()
    status = "partial" if failed else "success"
    log = f"pulled: {pulled}, failed: {failed}"
    if deferred:
        log += f", held back in Inbox ({len(deferred)}): {deferred[:5]}"
    _record_run("drive-inbox-pull", status, log)
    return {"status": status, "pulled": pulled, "failed": failed, "deferred": deferred}


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
        detail = _cmd_error_detail(e)
        _record_run("nas-to-drive-library", "error", detail)
        return {"status": "error", "detail": detail}

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
        detail = _cmd_error_detail(e)
        _record_run("library-verify", "error", detail)
        return {"status": "error", "detail": detail}

    status = "success" if result.returncode == 0 else "partial"
    _record_run("library-verify", status, result.stdout + result.stderr)
    return {"status": status, "detail": result.stdout}
