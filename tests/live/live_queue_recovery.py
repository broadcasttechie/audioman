"""A worker that was restarted must not leave its job blocking new runs for an hour. ZZTEST rows only."""
import os
import socket
from datetime import datetime

from app import create_app
from app.extensions import db
from app.models import JobQueueItem
from jobs.queue import requeue_dead_local_jobs

app = create_app()
host = socket.gethostname()


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  [{extra}]" if extra else ""))
    if not cond:
        raise SystemExit(1)


def row(name, locked_by):
    r = JobQueueItem(job_name=name, status="running", locked_by=locked_by, started_at=datetime.utcnow(), triggered_by="test")
    db.session.add(r)
    db.session.commit()
    return r.id


try:
    with app.app_context():
        dead = row("zztest-dead", f"{host}:999999")
        live = row("zztest-live", f"{host}:{os.getpid()}")
        other_host = row("zztest-other", "some-other-host:999999")
        garbage = row("zztest-garbage", "no-pid-here")
        out = requeue_dead_local_jobs()
        names = {i.job_name for i in out}
        db.session.expire_all()
        check("a row locked by a dead process on this host is re-queued", "zztest-dead" in names and db.session.get(JobQueueItem, dead).status == "queued")
        check("it does not count as a failed attempt", db.session.get(JobQueueItem, dead).attempts == 0 and db.session.get(JobQueueItem, dead).locked_by is None)
        check("a row locked by a live process is left alone", db.session.get(JobQueueItem, live).status == "running")
        check("a row from another host is left alone", db.session.get(JobQueueItem, other_host).status == "running")
        check("an unparseable locked_by is left alone", db.session.get(JobQueueItem, garbage).status == "running")
        print("\nALL QUEUE RECOVERY CHECKS PASSED")
finally:
    with app.app_context():
        JobQueueItem.query.filter(JobQueueItem.job_name.like("zztest-%")).delete(synchronize_session=False)
        db.session.commit()
