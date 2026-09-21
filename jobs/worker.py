"""
Standalone worker process — run as its own systemd service
(deploy/systemd/audio-manager-worker@.service), NOT inside the Flask
web process. Safe to run more than one instance concurrently: claiming
uses Postgres's FOR UPDATE SKIP LOCKED (jobs/queue.py), so two workers
can never claim the same row. Each worker identifies itself as
"<hostname>:<pid>" (jobs/queue.py:_worker_id) — running N instances on
one host just means N distinct pids, no configuration needed.

The `while True` loop here is a normal, correct shape for a queue
consumer daemon — it's meant to run forever. "No runaway loops" refers
to retry/backoff logic never spinning unboundedly, which is enforced
in jobs/queue.py (bounded attempts, backoff delay stored in the DB)
and jobs/retry.py (bounded external-call retries, circuit breaker) —
not to this outer loop, which is supposed to keep running.

Usage: `python3 worker.py`, or via the systemd unit.
"""
import logging
import signal
import time

from config import Config
from app import create_app
from app.extensions import db
from app.models import JobRun
from jobs.queue import claim_next, mark_success, mark_failure, reap_stale_jobs, _worker_id

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("worker")

_shutdown = False


def _handle_signal(signum, frame):
    global _shutdown
    log.info("received shutdown signal, will stop after current job (if any)")
    _shutdown = True


def _record_crash_in_job_run(job_name, detail):
    """
    Last-resort visibility: if a job function raises an unhandled
    exception (a bug, as opposed to the errors it's expected to catch
    itself), job_queue's mark_failure() already handles the retry
    bookkeeping — but JobRun (what /api/jobs/<name>/status calls
    "last_result") is normally only updated by the job function's own
    _record_run() call, which never ran in this case. Without this,
    /status would keep showing a stale success from some earlier run
    while silently retrying a crashing job in the background.
    """
    run = JobRun.query.get(job_name) or JobRun(job_name=job_name)
    run.status = "error"
    run.log_tail = f"unhandled exception: {detail}"
    db.session.merge(run)
    db.session.commit()


def run_forever():
    from jobs import JOB_REGISTRY  # populated at import time

    app = create_app()
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    worker_id = _worker_id()
    log.info("worker %s starting, jobs available: %s", worker_id, sorted(JOB_REGISTRY.keys()))

    with app.app_context():
        from app.schema import ensure_schema
        ensure_schema(db)  # the web service does this too; harmless if it already has
        while not _shutdown:
            try:
                reaped = reap_stale_jobs()
                for item in reaped:
                    log.warning("reaped stale job %s (%s) - worker likely crashed",
                                item.id, item.job_name)

                item = claim_next()
                if not item:
                    time.sleep(Config.JOB_QUEUE_POLL_INTERVAL_SECONDS)
                    continue

                log.info("worker %s claimed %s (%s, attempt %d/%d)",
                          worker_id, item.id, item.job_name, item.attempts + 1, item.max_attempts)

                try:
                    JOB_REGISTRY[item.job_name]()
                    mark_success(item)
                    log.info("job %s (%s) succeeded", item.id, item.job_name)
                except Exception as e:
                    log.exception("job %s (%s) failed", item.id, item.job_name)
                    db.session.rollback()  # discard any half-committed work from the failed job
                    mark_failure(item, str(e))
                    _record_crash_in_job_run(item.job_name, str(e))

            except Exception as e:
                # Something went wrong OUTSIDE the job itself — e.g. a
                # transient DB error on claim_next() or reap_stale_jobs().
                # Log and back off briefly rather than letting the whole
                # process die (which would just bounce through systemd's
                # Restart=on-failure anyway, but that's a slower, noisier
                # recovery than just retrying the loop).
                log.exception("worker loop error (will retry): %s", e)
                db.session.rollback()
                time.sleep(Config.JOB_QUEUE_POLL_INTERVAL_SECONDS)

    log.info("worker %s shut down cleanly", worker_id)


if __name__ == "__main__":
    run_forever()
