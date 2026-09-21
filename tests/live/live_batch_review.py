"""Batch review live check (real DB, NAS and worker). All ZZTEST; removed at the end."""
import os
import shutil
import subprocess
import time
import uuid

from app import create_app
from app.extensions import db
from app.models import Resource, RecordingSession, Project, FileEvent, Tag
from config import Config
from jobs.ingest import ingest_staged_file

app = create_app()
ids = []


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  [{extra}]" if extra else ""))
    if not cond:
        raise SystemExit(1)


def stage(name, freq):
    os.makedirs(Config.STAGING_DIR, exist_ok=True)
    path = os.path.join(Config.STAGING_DIR, name)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration=1", "-ac", "1", path], check=True)
    r = ingest_staged_file(path)
    ids.append(r.id)
    return r


def wait_until(rid, states, secs=90):
    end = time.time() + secs
    while time.time() < end:
        db.session.expire_all()
        if db.session.get(Resource, rid).status in states:
            return True
        time.sleep(2)
    return False


try:
    with app.app_context():
        c = app.test_client()
        page = c.get("/review")
        check("review page serves with the batch bar", page.status_code == 200 and b'id="batch-bar"' in page.data and b"selectall" in page.data)

        p = c.post("/api/projects", json={"name": "ZZTEST batch", "slug": "zztest-batch"}).get_json()
        s = c.post("/api/sessions", json={"name": "2026-09-15 Night 2", "project_id": p["id"]}).get_json()
        a, b, d = stage("ZZTEST_b1.wav", 601), stage("ZZTEST_b2.wav", 602), stage("ZZTEST_b3.wav", 603)
        e = stage("2026-07-08 ZZTEST batch title.wav", 604)      # suggest-only profile: has a suggestion, no date
        tag = c.post("/api/tags", json={"name": "zztest-batch-tag"}).get_json()
        B = lambda **kw: c.post("/api/resources/batch", json=kw)

        # ---- validation ---------------------------------------------------------
        check("empty ids refused", B(ids=[], patch={"category": "ambient"}).status_code == 400)
        check("501 ids refused", B(ids=[str(uuid.uuid4()) for _ in range(501)], patch={"category": "ambient"}).status_code == 400)
        check("nothing to apply refused", B(ids=[a.id]).status_code == 400)
        check("per-file fields refused in a batch", B(ids=[a.id], patch={"notes": "x"}).status_code == 400)
        check("tags_add must be a list of strings", B(ids=[a.id], tags_add=[1]).status_code == 400)

        # ---- one bad item doesn't stop the rest; a failure leaves NO partial change ------
        out = B(ids=[a.id, str(uuid.uuid4()), b.id], patch={"category": "ambient"}).get_json()
        check("unknown id reported per item, others applied", out["ok_count"] == 2 and out["error_count"] == 1 and any(r["status"] == 404 for r in out["results"]))
        out = B(ids=[a.id, b.id], patch={"category": "event", "session_id": str(uuid.uuid4())}).get_json()
        db.session.expire_all()
        check("a rejected change is fully rolled back (category not applied)", out["error_count"] == 2 and db.session.get(Resource, a.id).category == "ambient")

        # ---- the real thing: category + project + session + tags ---------------------------
        out = B(ids=[a.id, b.id, d.id], patch={"category": "event", "project_id": p["id"], "session_id": s["id"]}, tags_add=[tag["id"]]).get_json()
        db.session.expire_all()
        ra, rb, rd = (db.session.get(Resource, x) for x in (a.id, b.id, d.id))
        check("category, project and session applied to all three", out["ok_count"] == 3 and all(r.category == "event" and r.project_id == p["id"] and r.session_id == s["id"] for r in (ra, rb, rd)))
        check("tag added to all three", all([t.name for t in r.tags] == ["zztest-batch-tag"] for r in (ra, rb, rd)))
        again = B(ids=[a.id], patch={"category": "event"}, tags_add=[tag["id"]]).get_json()
        db.session.expire_all()
        check("adding a tag again doesn't duplicate it; existing tags are kept", again["ok_count"] == 1 and [t.name for t in db.session.get(Resource, a.id).tags] == ["zztest-batch-tag"])

        # ---- suggested dates in a batch ------------------------------------------------------
        out = B(ids=[e.id], patch={"use_suggested_date": True}).get_json()
        db.session.expire_all()
        re_ = db.session.get(Resource, e.id)
        check("use_suggested_date confirms each file's own suggestion", out["ok_count"] == 1 and re_.captured_at is not None and re_.captured_at_precision == "approximate")
        out = B(ids=[b.id], patch={"use_suggested_date": True}).get_json()
        check("a file with no suggestion fails on its own, with a reason", out["error_count"] == 1 and "no suggested date" in out["results"][0]["error"])

        # ---- filing through the batch, via the real worker ---------------------------------------
        out = B(ids=[a.id, b.id, d.id], patch={"status": "filed"}).get_json()
        check("batch filing queues all three", out["ok_count"] == 3 and all(r["resource_status"] == "filing" for r in out["results"]))
        check("the worker files all three", all(wait_until(x, ("filed", "failed")) for x in (a.id, b.id, d.id)))
        db.session.expire_all()
        for x in (a.id, b.id, d.id):
            r = db.session.get(Resource, x)
            want = os.path.join(Config.NAS_LIBRARY_ROOT, "zztest-batch", "2026-09-15 Night 2", r.filename)
            check(f"{r.filename} is in {{project}}/{{session}}/ on the NAS", r.status == "filed" and r.nas_path == want and os.path.exists(want), r.failure_detail or r.status)
        check("uncategorised file can't be filed", B(ids=[e.id], patch={"status": "filed"}).get_json()["results"][0]["error"] == "category is required before filing")
        print("\nALL BATCH CHECKS PASSED")
finally:
    with app.app_context():
        for rid in ids:
            r = db.session.get(Resource, rid)
            if r:
                for x in (r.nas_path, r.staging_path):
                    if x and os.path.exists(x) and os.path.basename(x).startswith(("ZZTEST", "2026-07-08 ZZTEST")):
                        os.remove(x)
        for rid in ids:
            FileEvent.query.filter_by(resource_id=rid).delete()
            r = db.session.get(Resource, rid)
            if r:
                r.tags = []
                db.session.delete(r)
        db.session.commit()
        RecordingSession.query.filter(RecordingSession.name == "2026-09-15 Night 2").delete(synchronize_session=False)
        Project.query.filter(Project.slug == "zztest-batch").delete(synchronize_session=False)
        Tag.query.filter(Tag.name.like("zztest-%")).delete(synchronize_session=False)
        db.session.commit()
        shutil.rmtree(os.path.join(Config.NAS_LIBRARY_ROOT, "zztest-batch"), ignore_errors=True)
        for f in os.listdir(Config.STAGING_DIR):
            if f.startswith(("ZZTEST", "2026-07-08 ZZTEST")):
                os.remove(os.path.join(Config.STAGING_DIR, f))
        print("cleanup: real resources =", Resource.query.count(), "| NAS zztest dir exists =", os.path.isdir(os.path.join(Config.NAS_LIBRARY_ROOT, "zztest-batch")))
