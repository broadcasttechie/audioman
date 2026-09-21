"""WP3 live check: projects/sessions API, structure rules, roles, notes and real NAS filing paths.
Everything is ZZTEST-named and removed at the end (rows, NAS files and the empty folders it made)."""
import os
import shutil
import subprocess
import time
import uuid

from app import create_app
from app.extensions import db
from app.models import Resource, RecordingSession, Project, FileEvent
from config import Config
from jobs.ingest import ingest_staged_file
from jobs.path_template import render_path

app = create_app()
ids, made_dirs = [], []
REAL = "66482729-aa93-4704-b3b9-13cdad3a2e7e"


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  [{extra}]" if extra else ""))
    if not cond:
        raise SystemExit(1)


def wait_done(rid, secs=90):
    """Filing is a background job (jobs/filing.py): wait for the worker."""
    end = time.time() + secs
    while time.time() < end:
        db.session.expire_all()
        if db.session.get(Resource, rid).status in ("filed", "failed"):
            return
        time.sleep(2)


def stage(name, freq):
    os.makedirs(Config.STAGING_DIR, exist_ok=True)
    path = os.path.join(Config.STAGING_DIR, name)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration=1", "-ac", "1", path], check=True)
    r = ingest_staged_file(path)
    ids.append(r.id)
    return r


try:
    with app.app_context():
        c = app.test_client()
        real_before = c.get(f"/api/resources/{REAL}").get_json()
        check("schema step added the new columns; existing file has role original", real_before["role"] == "original" and real_before["session_id"] is None and real_before["notes"] is None)

        # ---- projects -------------------------------------------------------------------
        pid_a = str(uuid.uuid4())
        r1 = c.post("/api/projects", json={"id": pid_a, "name": "ZZTEST Show", "slug": "zztest-show"})
        r2 = c.post("/api/projects", json={"id": pid_a, "name": "ZZTEST Show", "slug": "zztest-show"})
        check("client-id create is idempotent (201 then 200, one row)", (r1.status_code, r2.status_code) == (201, 200) and Project.query.filter_by(slug="zztest-show").count() == 1)
        check("bad slug rejected (it becomes a folder name)", c.post("/api/projects", json={"name": "x", "slug": "../x"}).status_code == 400)
        check("non-UUID client id rejected", c.post("/api/projects", json={"id": "abc", "name": "x", "slug": "zztest-x"}).status_code == 400)
        check("duplicate slug rejected", c.post("/api/projects", json={"name": "y", "slug": "zztest-show"}).status_code == 400)
        check("home 'drive' impossible with placement 'nas'", c.post("/api/projects", json={"name": "y", "slug": "zztest-y", "placement": "nas", "home": "drive"}).status_code == 400)
        pid_b = c.post("/api/projects", json={"name": "ZZTEST Other", "slug": "zztest-other"}).get_json()["id"]
        up = c.patch(f"/api/projects/{pid_a}", json={"notes": "Two nights at the Playhouse", "placement": "both", "home": "drive"})
        check("project notes/placement/home editable", up.status_code == 200 and up.get_json()["home"] == "drive" and up.get_json()["notes"].startswith("Two nights"))
        check("slug can't be changed", c.patch(f"/api/projects/{pid_a}", json={"slug": "other"}).status_code == 400)

        # ---- sessions -----------------------------------------------------------------------
        sid = str(uuid.uuid4())
        s1 = c.post("/api/sessions", json={"id": sid, "name": "2026-09-15 Night 2", "project_id": pid_a, "session_date": "2026-09-15"})
        s1b = c.post("/api/sessions", json={"id": sid, "name": "2026-09-15 Night 2", "project_id": pid_a})
        check("session create is idempotent by client id", (s1.status_code, s1b.status_code) == (201, 200) and RecordingSession.query.filter_by(project_id=pid_a).count() == 1)
        check("same folder name (any case) in one project -> 409", c.post("/api/sessions", json={"name": "2026-09-15 NIGHT 2", "project_id": pid_a}).status_code == 409)
        check("names that sanitise to the same folder -> 409", c.post("/api/sessions", json={"name": "a/b", "project_id": pid_a}).status_code == 201 and c.post("/api/sessions", json={"name": "a\\b", "project_id": pid_a}).status_code == 409)
        check("same name in another project is fine", c.post("/api/sessions", json={"name": "2026-09-15 Night 2", "project_id": pid_b}).status_code == 201)
        check("unknown project rejected", c.post("/api/sessions", json={"name": "x", "project_id": str(uuid.uuid4())}).status_code == 400)
        check("bad date rejected", c.post("/api/sessions", json={"name": "x2", "session_date": "15/09/2026"}).status_code == 400)
        loose_sid = c.post("/api/sessions", json={"name": "ZZTEST Parkridge walk", "session_date": "2020-05-01"}).get_json()["id"]

        # ---- structure rules on a file --------------------------------------------------------------
        a = stage("ZZTEST_a.wav", 401)
        b = stage("ZZTEST_b.wav", 402)
        d = stage("ZZTEST_c.wav", 403)
        A = lambda **kw: c.patch(f"/api/resources/{a.id}", json=kw)
        r = A(session_id=sid).get_json()
        check("giving a session sets the project from it", r["session_id"] == sid and r["project_id"] == pid_a and r["session"]["name"] == "2026-09-15 Night 2")
        check("a project that contradicts the session is refused", A(session_id=sid, project_id=pid_b).status_code == 400)
        r = A(project_id=pid_b).get_json()
        check("moving to another project takes the file out of its session", r["project_id"] == pid_b and r["session_id"] is None)
        r = A(session_id=sid).get_json()
        r = A(session_id=None).get_json()
        check("clearing the session keeps the project", r["session_id"] is None and r["project_id"] == pid_a)
        check("unknown session refused", A(session_id=str(uuid.uuid4())).status_code == 400)
        check("unknown project refused", A(project_id=str(uuid.uuid4())).status_code == 400)
        A(session_id=sid)

        # roles / edits / notes
        check("invalid role refused", A(role="bootleg").status_code == 400)
        check("a file can't be an edit of itself", A(derived_from_id=a.id).status_code == 400)
        check("unknown original refused", A(derived_from_id=str(uuid.uuid4())).status_code == 400)
        r = c.patch(f"/api/resources/{b.id}", json={"derived_from_id": a.id}).get_json()
        check("pointing at an original makes it an edit", r["role"] == "edit" and r["derived_from_id"] == a.id)
        check("an edit loop is refused", A(derived_from_id=b.id).status_code == 400)
        n = c.patch(f"/api/resources/{b.id}", json={"notes": "Trimmed, noise-reduced", "track_label": "TR1"}).get_json()
        check("notes and track label saved", n["notes"] == "Trimmed, noise-reduced" and n["track_label"] == "TR1")
        check("notes must be text", A(notes=5).status_code == 400)

        # ---- listing ----------------------------------------------------------------------------------
        lst = c.get(f"/api/resources?session_id={sid}").get_json()
        check("library can filter by session", [x["id"] for x in lst["resources"]] == [a.id])
        sess = c.get(f"/api/sessions/{sid}").get_json()
        check("session detail lists its files and count", sess["file_count"] == 1 and sess["files"][0]["id"] == a.id)
        check("delete a session with files -> 409", c.delete(f"/api/sessions/{sid}").status_code == 409)

        # ---- moving a session between projects moves its files' project -----------------------------------
        mv = c.post("/api/sessions", json={"name": "ZZTEST mover", "project_id": pid_b}).get_json()["id"]
        c.patch(f"/api/resources/{d.id}", json={"session_id": mv})
        c.patch(f"/api/sessions/{mv}", json={"project_id": pid_a})
        db.session.expire_all()
        check("moving a session moves its files' project too", db.session.get(Resource, d.id).project_id == pid_a)
        check("moving into a project with that folder name is refused", c.post("/api/sessions", json={"name": "ZZTEST mover2", "project_id": pid_b}).status_code == 201 and c.patch(f"/api/sessions/{mv}", json={"name": "2026-09-15 Night 2"}).status_code == 409)
        c.patch(f"/api/resources/{d.id}", json={"session_id": None})

        # ---- real filing paths on the NAS --------------------------------------------------------------------
        def file_it(res_id, **fields):
            c.patch(f"/api/resources/{res_id}", json={"category": "ambient", **fields})
            resp = c.patch(f"/api/resources/{res_id}", json={"status": "filed"})
            wait_done(res_id)
            return resp

        resp = file_it(a.id)
        check("filing into project+session is accepted", resp.status_code == 200, str(resp.get_json())[:100])
        db.session.expire_all()
        ra = db.session.get(Resource, a.id)
        want = os.path.join(Config.NAS_LIBRARY_ROOT, "zztest-show", "2026-09-15 Night 2", "ZZTEST_a.wav")
        check("file is at {project}/{session}/{filename} on the NAS", ra.nas_path == want and os.path.exists(want), ra.nas_path)
        check("path equals what refile-all would render (no phantom move)", os.path.join(Config.NAS_LIBRARY_ROOT, render_path(ra, ra.project, ra.session)) == ra.nas_path)
        made_dirs.append(os.path.join(Config.NAS_LIBRARY_ROOT, "zztest-show"))

        # same name into the same session: refused, first file untouched
        dup = stage("ZZTEST_a.wav", 404)   # same filename, different content
        c.patch(f"/api/resources/{dup.id}", json={"session_id": sid, "category": "ambient"})
        resp = c.patch(f"/api/resources/{dup.id}", json={"status": "filed"})
        wait_done(dup.id)
        db.session.expire_all()
        dd = db.session.get(Resource, dup.id)
        check("same filename in the same session fails safely, nothing overwritten", dd.status == "failed" and "refusing to overwrite" in (dd.failure_detail or "") and os.path.exists(want))

        # loose recording with a date -> year/month
        c.patch(f"/api/resources/{d.id}", json={"captured_at": "2020-05-01T10:00:00Z", "project_id": None})  # make it loose
        resp = file_it(d.id)
        db.session.expire_all()
        rd = db.session.get(Resource, d.id)
        check("loose file goes under misc/{category}/{year}/{month}/", resp.status_code == 200 and rd.nas_path == os.path.join(Config.NAS_LIBRARY_ROOT, "misc", "ambient", "2020", "05", "ZZTEST_c.wav"), rd.nas_path)
        made_dirs.append(os.path.join(Config.NAS_LIBRARY_ROOT, "misc", "ambient", "2020"))

        # loose recording in an outing session
        e = stage("ZZTEST_d.wav", 405)
        c.patch(f"/api/resources/{e.id}", json={"captured_at": "2020-05-01T10:00:00Z", "session_id": loose_sid})
        resp = file_it(e.id)
        db.session.expire_all()
        re_ = db.session.get(Resource, e.id)
        check("a loose outing session adds its own folder", resp.status_code == 200 and re_.nas_path == os.path.join(Config.NAS_LIBRARY_ROOT, "misc", "ambient", "2020", "05", "ZZTEST Parkridge walk", "ZZTEST_d.wav"), re_.nas_path)

        real_after = c.get(f"/api/resources/{REAL}").get_json()
        check("the real resource is untouched", real_after["nas_path"] == real_before["nas_path"] and real_after["captured_at"] == real_before["captured_at"])
        print("\nALL WP3 CHECKS PASSED")
finally:
    with app.app_context():
        for rid in ids:
            r = db.session.get(Resource, rid)
            if r:
                r.derived_from_id = None
        db.session.commit()
        for rid in ids:
            FileEvent.query.filter_by(resource_id=rid).delete()
            r = db.session.get(Resource, rid)
            if r:
                for p in (r.nas_path, r.staging_path):
                    if p and os.path.basename(p).startswith("ZZTEST_") and os.path.exists(p):
                        os.remove(p)
                r.tags = []
                db.session.delete(r)
        db.session.commit()
        RecordingSession.query.filter(RecordingSession.name.like("ZZTEST%")).delete(synchronize_session=False)
        for s in RecordingSession.query.filter(RecordingSession.project_id.in_(
                [p.id for p in Project.query.filter(Project.slug.like("zztest-%")).all()])).all():
            db.session.delete(s)
        db.session.commit()
        Project.query.filter(Project.slug.like("zztest-%")).delete(synchronize_session=False)
        db.session.commit()
        # remove the folders this test created (only if now empty of anything else)
        for top in made_dirs:
            if os.path.isdir(top):
                shutil.rmtree(top) if os.path.basename(top).startswith("zztest") else None
        yr = os.path.join(Config.NAS_LIBRARY_ROOT, "misc", "ambient", "2020")
        if os.path.isdir(yr):
            for root, dirs, files in os.walk(yr, topdown=False):
                if not files and not os.listdir(root):
                    os.rmdir(root)
        leftover = []
        for root, _, files in os.walk(Config.NAS_LIBRARY_ROOT):
            leftover += [f for f in files if f.startswith("ZZTEST")]
        for f in os.listdir(Config.STAGING_DIR):
            if f.startswith("ZZTEST"):
                os.remove(os.path.join(Config.STAGING_DIR, f))
        print("cleanup: test files left on NAS =", leftover, "| projects/sessions left =",
              Project.query.filter(Project.slug.like("zztest-%")).count(), RecordingSession.query.filter(RecordingSession.name.like("ZZTEST%")).count(),
              "| real resources =", Resource.query.count())
