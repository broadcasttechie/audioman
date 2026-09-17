"""
The actual job queue. Claiming uses Postgres's `SELECT ... FOR UPDATE
SKIP LOCKED` — the standard pattern for a DB-backed queue: it lets a
worker atomically grab one queued row without blocking on (or racing)
any other worker trying to do the same thing, and without needing a
separate lock table or external broker.

⚠️ SKIP LOCKED is Postgres-specific. Config already defaults to
Postgres in production; if you ever run this against SQLite (e.g. a
quick local test), claim_next() falls back to a plain SELECT+UPDATE
that is NOT safe under concurrent workers — fine for single-process
local dev, not for anything else. Don't run two worker processes
against a SQLite-backed instance.
"""
from datetime import datetime, timedelta
import os
import socket

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from config import Config
from app.extensions import db
from app.models import JobQueueItem


def _worker_id():
    return f"{socket.gethostname()}:{os.getpid()}"


def enqueue(job_name, triggered_by="manual", max_attempts=None):
    """
    Adds a job to the queue, UNLESS one is already queued or running
    for this job_name — prevents pile-up if e.g. a systemd timer fires
    again while the worker is still backed up on the previous run, or
    someone mashes "run now" repeatedly in the UI.

    The pre-check + insert below is TOCTOU-racy on its own (two
    near-simultaneous calls could both pass the check) — the DB-level
    partial unique index on JobQueueItem (see app/models.py) is what
    actually closes that race; the except IntegrityError branch just
    handles losing that race gracefully instead of erroring.
    Returns the JobQueueItem (existing or newly created).
    """
    existing = JobQueueItem.query.filter(
        JobQueueItem.job_name == job_name,
        JobQueueItem.status.in_(["queued", "running"]),
    ).first()
    if existing:
        return existing

    item = JobQueueItem(
        job_name=job_name,
        triggered_by=triggered_by,
        max_attempts=max_attempts or Config.JOB_QUEUE_MAX_ATTEMPTS,
    )
    db.session.add(item)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        # Lost the race - someone else's enqueue landed first between
        # our check and our insert. Their row is the queue entry now.
        return JobQueueItem.query.filter(
            JobQueueItem.job_name == job_name,
            JobQueueItem.status.in_(["queued", "running"]),
        ).first()
    return item


def claim_next():
    """
    Atomically claims the oldest eligible queued item (status='queued',
    next_attempt_at <= now) and marks it 'running'. Returns the
    JobQueueItem, or None if nothing's eligible right now.
    """
    is_postgres = db.engine.dialect.name == "postgresql"
    now = datetime.utcnow()

    if is_postgres:
        row = db.session.execute(
            text("""
                SELECT id FROM job_queue
                WHERE status = 'queued' AND next_attempt_at <= :now
                ORDER BY enqueued_at
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            """),
            {"now": now},
        ).first()
        if not row:
            return None
        item = JobQueueItem.query.get(row[0])
    else:
        # Non-Postgres fallback — see module docstring caveat.
        item = JobQueueItem.query.filter(
            JobQueueItem.status == "queued",
            JobQueueItem.next_attempt_at <= now,
        ).order_by(JobQueueItem.enqueued_at).first()
        if not item:
            return None

    item.status = "running"
    item.started_at = now
    item.locked_by = _worker_id()
    db.session.commit()
    return item


def mark_success(item):
    item.status = "success"
    item.finished_at = datetime.utcnow()
    db.session.commit()


def mark_failure(item, detail):
    """
    Increments attempts. If under max_attempts, requeues with
    exponential backoff (next_attempt_at pushed out — the delay lives
    in the DB row, so it survives a worker restart, not just a
    sleep() in memory). Otherwise marks permanently failed — bounded,
    never retried again automatically, but visible for a human to
    look at via /api/jobs/<name>/status or the job_queue table itself.
    """
    item.attempts += 1
    item.error_detail = detail

    if item.attempts < item.max_attempts:
        delay = Config.JOB_QUEUE_RETRY_BASE_DELAY_SECONDS * (2 ** (item.attempts - 1))
        item.status = "queued"
        item.next_attempt_at = datetime.utcnow() + timedelta(seconds=delay)
        item.locked_by = None
    else:
        item.status = "failed-permanently"
        item.finished_at = datetime.utcnow()

    db.session.commit()


def reap_stale_jobs():
    """
    If a worker is killed (OOM, crash, `systemctl kill`) mid-job, its
    claimed row stays 'running' forever otherwise — claim_next() only
    ever looks at status='queued', so nothing else would notice.
    Finds any 'running' row whose started_at is older than
    JOB_QUEUE_STALE_RUNNING_TIMEOUT_SECONDS and routes it through the
    same bounded requeue-or-give-up path as an ordinary failure
    (mark_failure), rather than leaving it stuck. Cheap indexed query
    — safe to call every worker loop iteration, and safe to call from
    multiple concurrent workers (each row is only ever touched once,
    since the first reaper to update a row moves it out of 'running').
    """
    cutoff = datetime.utcnow() - timedelta(seconds=Config.JOB_QUEUE_STALE_RUNNING_TIMEOUT_SECONDS)
    stale = JobQueueItem.query.filter(
        JobQueueItem.status == "running",
        JobQueueItem.started_at < cutoff,
    ).all()

    for item in stale:
        mark_failure(
            item,
            f"reaped: still 'running' after "
            f"{Config.JOB_QUEUE_STALE_RUNNING_TIMEOUT_SECONDS}s — "
            f"worker likely crashed (was locked by {item.locked_by})",
        )

    return stale
