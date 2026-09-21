"""Edit candidates and library search (real DB). ZZTEST-free names that look like a Zoom recorder's, removed after."""
import os
import subprocess

from app import create_app
from app.extensions import db
from app.models import Resource, Project, RecordingSession, Tag, FileEvent
from config import Config
from jobs.ingest import ingest_staged_file

app = create_app()
ids = []


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  [{extra}]" if extra else ""))
    if not cond:
        raise SystemExit(1)


def stage(name, freq, inbox_path=None):
    os.makedirs(Config.STAGING_DIR, exist_ok=True)
    d = os.path.join(Config.STAGING_DIR, "zztest-edits")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, name)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration=1", "-ac", "1", path], check=True)
    r = ingest_staged_file(path, drive_inbox_path=inbox_path or name)
    ids.append(r.id)
    return r


try:
    with app.app_context():
        c = app.test_client()
        orig = stage("250424-121237-ZZ Parkridge Nature Reserve.WAV", 810, "2025-04/Parkridge/250424-121237-ZZ Parkridge Nature Reserve.WAV")
        edit = stage("250424-121237-ZZ Parkridge Nature Reserve-EDIT.WAV.wav", 811, "2025-04/Parkridge/250424-121237-ZZ Parkridge Nature Reserve-EDIT.WAV.wav")
        decoy = stage("250424-121237-ZZ other place.WAV", 812, "Somewhere else/250424-121237-ZZ other place.WAV")
        other = stage("250424-130000-ZZ later take.WAV", 813, "2025-04/Parkridge/250424-130000-ZZ later take.WAV")

        got = c.get(f"/api/resources/{edit.id}/edit-candidates").get_json()
        check("an EDIT file is offered its original (same folder, same timestamp)", [x["id"] for x in got] == [orig.id], str([x["filename"][:30] for x in got]))
        check("a different folder with the same timestamp is not offered", decoy.id not in [x["id"] for x in got])
        check("an ordinary file has no candidates", c.get(f"/api/resources/{orig.id}/edit-candidates").get_json() == [])
        r = c.patch(f"/api/resources/{edit.id}", json={"derived_from_id": orig.id}).get_json()
        check("linking makes it an edit of the original", r["role"] == "edit" and r["derived_from_id"] == orig.id)
        check("once linked, nothing more is suggested", c.get(f"/api/resources/{edit.id}/edit-candidates").get_json() == [])

        # ---- search covers notes, session, project and tags, not just the filename ----------------------------
        p = c.post("/api/projects", json={"name": "ZZUnicorn Festival", "slug": "zzunicorn-festival"}).get_json()
        s = c.post("/api/sessions", json={"name": "ZZ Midsummer night", "project_id": p["id"]}).get_json()
        tag = c.post("/api/tags", json={"name": "zzcuckoo"}).get_json()
        c.patch(f"/api/resources/{other.id}", json={"session_id": s["id"], "notes": "a distant zzwoodpecker drumming", "tags": [tag["id"]]})
        search = lambda q: {x["id"] for x in c.get("/api/resources?status=&limit=50&q=" + q).get_json()["resources"]}
        check("search finds by note text", other.id in search("zzwoodpecker"))
        check("search finds by session name", other.id in search("Midsummer"))
        check("search finds by project name", other.id in search("ZZUnicorn"))
        check("search finds by tag", other.id in search("zzcuckoo"))
        check("search still finds by filename, case-insensitively", other.id in search("later%20TAKE"))
        check("a term that matches nothing finds nothing", search("zzqqxxnothing") == set())
        check("wildcard characters are literal, not patterns", search("%25") == set() and search("zz_cuckoo") == set() and search("zz%25cuckoo") == set())
        print("\nALL EDIT/SEARCH CHECKS PASSED")
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
                r.tags = []
                if r.staging_path and os.path.exists(r.staging_path):
                    os.remove(r.staging_path)
                db.session.delete(r)
        db.session.commit()
        RecordingSession.query.filter(RecordingSession.name.like("ZZ%")).delete(synchronize_session=False)
        Project.query.filter(Project.slug == "zzunicorn-festival").delete(synchronize_session=False)
        Tag.query.filter(Tag.name.like("zz%")).delete(synchronize_session=False)
        db.session.commit()
        import shutil
        shutil.rmtree(os.path.join(Config.STAGING_DIR, "zztest-edits"), ignore_errors=True)
        print("cleanup: real resources =", Resource.query.count())
