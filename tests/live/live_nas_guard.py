"""End-to-end check of NAS guard + safe filing against the REAL mount.
Everything is named ZZTEST_ and removed at the end (rows, staging and NAS files)."""
import glob
import os
import subprocess
import time

from app import create_app
from app.extensions import db
from app.models import Resource, FileEvent, JobRun
from config import Config
from jobs.ingest import ingest_staged_file

app = create_app()
GUARDED_JOBS = ("refile-all", "verify-integrity", "find-orphans")
NAME = "ZZTEST_nas_guard.wav"
created = []


def make_wav(freq):
    os.makedirs(Config.STAGING_DIR, exist_ok=True)
    path = os.path.join(Config.STAGING_DIR, NAME)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration=2",
                    "-ac", "1", path], check=True)
    return path


def wait_done(rid, secs=90):
    end = time.time() + secs
    while time.time() < end:
        db.session.expire_all()
        if db.session.get(Resource, rid).status in ("filed", "failed"):
            return
        time.sleep(2)


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  [{extra}]" if extra else ""))
    if not cond:
        raise SystemExit(1)


try:
    with app.app_context():
        client = app.test_client()
        # This test makes the guarded jobs refuse (recording an error). Remember their state so it can be put back.
        saved_runs = {n: (lambda r: (r.status, r.log_tail, r.last_run_at) if r else None)(db.session.get(JobRun, n)) for n in GUARDED_JOBS}
        check("nas status endpoint healthy", client.get("/api/nas/status").status_code == 200)

        # --- 1. guard: NAS 'unavailable' -> 503, nothing changed --------------
        r1 = ingest_staged_file(make_wav(440))
        created.append(r1.id)
        rid, staged = r1.id, r1.staging_path
        check("ingested", r1.status == "pending-review")
        real_marker = Config.NAS_MARKER_FILE
        Config.NAS_MARKER_FILE = ".marker-that-does-not-exist"
        try:
            check("nas status endpoint reports down", client.get("/api/nas/status").status_code == 503)
            resp = client.patch(f"/api/resources/{rid}", json={"category": "ambient", "status": "filed"})
            check("filing refused with 503 when NAS is down", resp.status_code == 503, resp.get_json().get("detail", "")[:80])
        finally:
            Config.NAS_MARKER_FILE = real_marker
        db.session.expire_all()
        r1 = db.session.get(Resource, rid)
        check("resource untouched by the refused filing", r1.status == "pending-review" and r1.category is None and r1.nas_path is None)
        check("staging file still present", os.path.exists(staged))

        # --- 2. real filing to the NAS (cross-filesystem) ------------------
        resp = client.patch(f"/api/resources/{rid}", json={"category": "ambient", "status": "filed"})
        check("filing is accepted (runs in the background)", resp.status_code == 200 and resp.get_json()["status"] == "filing", str(resp.get_json())[:120])
        wait_done(rid)
        db.session.expire_all()
        r1 = db.session.get(Resource, rid)
        check("status filed, staging cleared", r1.status == "filed" and r1.staging_path is None)
        check("file is on the NAS mount", os.path.exists(r1.nas_path) and r1.nas_path.startswith(Config.NAS_LIBRARY_ROOT + "/"), r1.nas_path)
        from jobs.nas import sha256_file
        check("NAS copy checksum equals stored checksum", sha256_file(r1.nas_path) == r1.checksum)
        check("staging file removed", not os.path.exists(staged))
        check("no .part left behind", not glob.glob(os.path.join(os.path.dirname(r1.nas_path), ".*.part")))
        check("moved event logged", FileEvent.query.filter_by(resource_id=rid, event_type="moved").count() == 1)
        a = client.get(f"/api/resources/{rid}/audio", headers={"Range": "bytes=0-99"})
        check("audio served from the NAS with Range", a.status_code == 206 and len(a.data) == 100)

        # --- 3. same destination name, different content: refused, not overwritten
        r2 = ingest_staged_file(make_wav(880))
        created.append(r2.id)
        staged2 = r2.staging_path
        client.patch(f"/api/resources/{r2.id}", json={"category": "ambient"})
        before = sha256_file(r1.nas_path)
        resp = client.patch(f"/api/resources/{r2.id}", json={"status": "filed"})
        wait_done(r2.id)
        db.session.expire_all()
        r2x = db.session.get(Resource, r2.id)
        check("name collision fails safely", r2x.status == "failed" and "refusing to overwrite" in (r2x.failure_detail or ""), (r2x.failure_detail or "")[:60])
        check("first file untouched", sha256_file(r1.nas_path) == before)
        db.session.expire_all()
        r2 = db.session.get(Resource, r2.id)
        check("second resource keeps its file in staging", r2.status == "failed" and os.path.exists(staged2))

        # --- 4. maintenance jobs refuse while the NAS is 'down' ---------------
        from jobs import maintenance
        Config.NAS_MARKER_FILE = ".marker-that-does-not-exist"
        try:
            for job in (maintenance.verify_integrity, maintenance.find_orphans, maintenance.refile_all):
                out = job()
                check(f"{job.__name__} refuses when NAS is down", out["status"] == "error" and "NAS unavailable" in out["detail"])
        finally:
            Config.NAS_MARKER_FILE = real_marker
        out = maintenance.verify_integrity()
        check("verify_integrity healthy with NAS up", out["status"] == "success" and not out["mismatches"], str(out["mismatches"]))
        orphans = maintenance.find_orphans()
        check("marker file is not reported as an orphan", not any(p.endswith(real_marker) for p in orphans["orphans_on_disk"]))
        print("\nALL E2E CHECKS PASSED")
finally:
    with app.app_context():
        for name, was in globals().get("saved_runs", {}).items():
            row = db.session.get(JobRun, name)
            if was is None and row is not None:
                db.session.delete(row)
            elif was is not None:
                row = row or JobRun(job_name=name)
                row.status, row.log_tail, row.last_run_at = was
                db.session.merge(row)
        db.session.commit()
        for rid in created:
            r = db.session.get(Resource, rid)
            if not r:
                continue
            for p in (r.nas_path, r.staging_path):
                if p and os.path.basename(p) == NAME and os.path.exists(p):
                    os.remove(p)
            FileEvent.query.filter_by(resource_id=rid).delete()
            r.tags = []
            db.session.delete(r)
        db.session.commit()
        for p in glob.glob(os.path.join(Config.STAGING_DIR, "ZZTEST_*")):
            os.remove(p)
        leftovers = glob.glob(os.path.join(Config.NAS_LIBRARY_ROOT, "**", "ZZTEST_*"), recursive=True)
        for p in leftovers:
            os.remove(p)
        print("cleanup: test rows left =", Resource.query.filter(Resource.filename.like("ZZTEST%")).count(),
              "| NAS test files removed =", len(leftovers), "| real resources =", Resource.query.count())
