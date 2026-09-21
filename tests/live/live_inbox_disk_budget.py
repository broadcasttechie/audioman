"""Integration check of drive_inbox_pull's disk admission against the real
Postgres, with rclone/ingest faked and a temp staging dir. Rows use a ZZTEST
prefix and are removed at the end."""
import os
import shutil
import tempfile
from datetime import datetime, timedelta

from app import create_app
from app.extensions import db
from app.models import PendingUpload
from config import Config
import jobs.rclone_jobs as rj

app = create_app()
tmp = tempfile.mkdtemp(prefix="zztest-staging-")
copied, moved = [], []


class Done:
    returncode = 0
    stdout = stderr = ""


def fake_run(cmd, **kw):
    if cmd[1] == "copy":
        name = os.path.basename(cmd[2])
        os.makedirs(cmd[3], exist_ok=True)          # each Inbox file is staged in its own directory
        with open(os.path.join(cmd[3], name), "wb") as f:
            f.write(b"x" * SIZES[name])
        copied.append(name)
    elif cmd[1] == "moveto":
        moved.append(os.path.basename(cmd[2]))
    return Done()


SIZES = {}


def run_case(title, sizes, budget_bytes, reserve_gb=0, max_files=10):
    SIZES.clear(); SIZES.update(sizes)
    copied.clear(); moved.clear()
    Config.STAGING_BUDGET_GB = budget_bytes / 1024 ** 3
    Config.DISK_RESERVE_GB = reserve_gb
    Config.INBOX_MAX_FILES_PER_RUN = max_files
    Config.MIN_AGE_MINUTES = 0
    base = datetime.utcnow() - timedelta(hours=5)
    for i, (name, size) in enumerate(sizes.items()):
        db.session.merge(PendingUpload(path=name, size=size, modtime="2026-01-01T00:00:00Z",
                                       first_seen_at=base + timedelta(minutes=i)))
    db.session.commit()
    rj._rclone_lsjson = lambda remote, files_only=False, recursive=False: [
        {"Path": n, "Size": s, "ModTime": "2026-01-01T00:00:00Z"} for n, s in reversed(list(sizes.items()))]
    result = rj.drive_inbox_pull()
    print(f"\n[{title}] pulled={result['pulled']}")
    for d in result["deferred"]:
        print("   deferred:", d)
    return result


try:
    Config.STAGING_DIR = tmp
    rj.subprocess.run = fake_run
    rj.ingest_staged_file = lambda *a, **k: None
    with app.app_context():
        # 1. FIFO + budget: 3000-byte budget, three 1500-byte files -> two pulled, one waits.
        r = run_case("budget", {"ZZTEST_a.wav": 1500, "ZZTEST_b.wav": 1500, "ZZTEST_c.wav": 1500}, 3000)
        assert r["pulled"] == ["ZZTEST_a.wav", "ZZTEST_b.wav"], r["pulled"]
        assert len(r["deferred"]) == 1 and "ZZTEST_c.wav" in r["deferred"][0]
        assert PendingUpload.query.get("ZZTEST_c.wav") is not None, "deferred file must stay queued"
        assert PendingUpload.query.get("ZZTEST_a.wav") is None, "pulled file's row should be gone"

        # 2. Staging drains (files reviewed/filed) -> the waiting file is pulled next run.
        shutil.rmtree(os.path.join(tmp, "inbox"), ignore_errors=True)
        r = run_case("after drain", {"ZZTEST_c.wav": 1500}, 3000)
        assert r["pulled"] == ["ZZTEST_c.wav"] and not r["deferred"]

        # 3. Head-of-line: once one waits, later (smaller) files don't jump the queue.
        shutil.rmtree(os.path.join(tmp, "inbox"), ignore_errors=True)
        r = run_case("head of line", {"ZZTEST_d.wav": 2500, "ZZTEST_e.wav": 2500, "ZZTEST_f.wav": 10}, 3000)
        assert r["pulled"] == ["ZZTEST_d.wav"], r["pulled"]
        assert len(r["deferred"]) == 2

        # 4. A file bigger than the whole budget is skipped loudly but doesn't block others.
        shutil.rmtree(os.path.join(tmp, "inbox"), ignore_errors=True)
        r = run_case("oversize", {"ZZTEST_g.wav": 9000, "ZZTEST_h.wav": 100}, 3000)
        assert r["pulled"] == ["ZZTEST_h.wav"], r["pulled"]
        assert any("STAGING_BUDGET_GB" in d for d in r["deferred"])

        # 5. Per-run cap.
        shutil.rmtree(os.path.join(tmp, "inbox"), ignore_errors=True)
        r = run_case("per-run cap", {"ZZTEST_i.wav": 10, "ZZTEST_j.wav": 10, "ZZTEST_k.wav": 10}, 3000, max_files=2)
        assert len(r["pulled"]) == 2 and len(r["deferred"]) == 1

        # 6. Reserve larger than the real free space -> nothing is pulled.
        shutil.rmtree(os.path.join(tmp, "inbox"), ignore_errors=True)
        r = run_case("reserve", {"ZZTEST_l.wav": 10}, 3000, reserve_gb=10 ** 6)
        assert r["pulled"] == [] and "reserve" in r["deferred"][0]
        print("\nALL CASES PASSED")
finally:
    with app.app_context():
        PendingUpload.query.filter(PendingUpload.path.like("ZZTEST%")).delete(synchronize_session=False)
        db.session.commit()
        print("cleanup: leftover ZZTEST rows =", PendingUpload.query.filter(PendingUpload.path.like("ZZTEST%")).count())
    shutil.rmtree(tmp, ignore_errors=True)
