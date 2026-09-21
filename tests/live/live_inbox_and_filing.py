"""WP5 live check: recursive Inbox pull, classification, per-file staging, sidecars, background filing
via the REAL worker, collision handling. rclone is faked; DB and NAS are real. Everything is removed after."""
import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timedelta

from app import create_app
from app.extensions import db
from app.models import Resource, PendingUpload, RecordingSession, FileEvent
from config import Config
import jobs.rclone_jobs as rj

app = create_app()
tmp_stage = tempfile.mkdtemp(prefix="zztest-stage-", dir="/var/lib/audio-manager")
tmp_src = tempfile.mkdtemp(prefix="zztest-src-")
made = set()
PATHS_AUDIO = ["2024 France/STE-000.wav", "storiesandforrest/STE-000.wav",
               "250424-121237-Parkridge Nature Reserve.WAV", "250424-121237-Parkridge Nature Reserve-EDIT.WAV.wav"]
SIDECAR = "2024 France/STE-000.wav.reapeaks"
ORPHAN_SIDECAR = "Archive/HATS/hats carrots raw.pkf"
OTHER = ["H1n tests/1/1.RPP", ".DS_Store", "notes.pdf", "_processed/old.wav"]
SRC = {}
moved = []


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  [{extra}]" if extra else ""))
    if not cond:
        raise SystemExit(1)


def make_sources():
    for i, p in enumerate(PATHS_AUDIO):
        f = os.path.join(tmp_src, f"a{i}.wav")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency={510 + 37 * i}:duration=1", "-ac", "1", f], check=True)
        SRC[p] = f
    for p in (SIDECAR, ORPHAN_SIDECAR, "H1n tests/1/1.RPP", ".DS_Store", "notes.pdf", "_processed/old.wav"):
        f = os.path.join(tmp_src, "x" + str(abs(hash(p))))
        open(f, "wb").write(os.urandom(2048))
        SRC[p] = f


class Done:
    returncode = 0
    stdout = stderr = ""


def fake_run(cmd, **kw):
    if cmd[0] == "rclone" and cmd[1] == "copy":
        path = cmd[2].split("gdrive:Inbox/", 1)[1]
        os.makedirs(cmd[3], exist_ok=True)
        shutil.copy(SRC[path], os.path.join(cmd[3], os.path.basename(path)))
    elif cmd[0] == "rclone" and cmd[1] == "moveto":
        moved.append(cmd[2].split("gdrive:Inbox/", 1)[1])
    else:
        return real_run(cmd, **kw)
    return Done()


real_run = subprocess.run
try:
    make_sources()
    with app.app_context():
        c = app.test_client()
        Config.STAGING_DIR = tmp_stage
        Config.MIN_AGE_MINUTES = 0
        Config.STAGING_BUDGET_GB = 5
        Config.DISK_RESERVE_GB = 0
        Config.INBOX_MAX_FILES_PER_RUN = 50
        listing = [{"Path": p, "Size": os.path.getsize(SRC[p]), "ModTime": "2026-01-01T00:00:00Z"} for p in PATHS_AUDIO + [SIDECAR, ORPHAN_SIDECAR] + OTHER]
        rj._rclone_lsjson = lambda remote, files_only=False, recursive=False: listing if recursive else []
        rj.subprocess.run = fake_run
        old = datetime.utcnow() - timedelta(hours=3)
        for e in [x for x in listing if x["Path"] in PATHS_AUDIO + [SIDECAR, ORPHAN_SIDECAR]]:
            db.session.merge(PendingUpload(path=e["Path"], size=e["Size"], modtime=e["ModTime"], first_seen_at=old))
        db.session.commit()

        out = rj.drive_inbox_pull()
        made.update(r.id for r in Resource.query.filter(Resource.drive_inbox_path.in_(PATHS_AUDIO + [SIDECAR])).all())
        check("all four audio files and the sidecar were pulled", sorted(out["pulled"]) == sorted(PATHS_AUDIO + [SIDECAR]), str(len(out["pulled"])))
        check("orphan sidecar (no audio) is held back, not failed", any(ORPHAN_SIDECAR in d and "waiting for its audio" in d for d in out["deferred"]) and not out["failed"])
        check("the project file is left in the Inbox and reported", out["held_project_files"] == ["H1n tests/1/1.RPP"])
        check("junk and unknown types are ignored, never staged", out["ignored"] == 2 and "notes.pdf" not in moved and ".DS_Store" not in moved)
        check("_processed is never scanned", "_processed/old.wav" not in out["pulled"] and "_processed/old.wav" not in moved)
        check("each pulled file is moved to _processed afterwards", sorted(moved) == sorted(PATHS_AUDIO + [SIDECAR]))
        check("held-back files keep their queue row", PendingUpload.query.get(ORPHAN_SIDECAR) is not None and PendingUpload.query.get("H1n tests/1/1.RPP") is None)

        stes = Resource.query.filter(Resource.filename == "STE-000.wav").all()
        check("two different STE-000.wav files both ingested", len(stes) == 2 and len({r.staging_path for r in stes}) == 2 and all(os.path.exists(r.staging_path) for r in stes))
        france = next(r for r in stes if r.drive_inbox_path.startswith("2024 France"))
        other = next(r for r in stes if r.drive_inbox_path.startswith("storiesandforrest"))
        check("Inbox folder kept as a hint on each", france.filename_info["folder"] == "2024 France" and other.filename_info["folder"] == "storiesandforrest")
        side = Resource.query.filter_by(role="sidecar").filter(Resource.filename == "STE-000.wav.reapeaks").first()
        check("sidecar attached to the right audio, awaiting it", side and side.derived_from_id == france.id and side.status == "attached" and side.format == "reapeaks")
        zoom = Resource.query.filter(Resource.filename == "250424-121237-Parkridge Nature Reserve.WAV").first()
        edit = Resource.query.filter(Resource.filename.like("%-EDIT.WAV.wav")).first()
        check("Zoom filenames dated at ingest", zoom.captured_at == datetime(2025, 4, 24, 11, 12, 37) and edit.filename_info["is_edit"] is True)

        lst = c.get("/api/resources?status=pending-review&limit=50").get_json()
        ids = [r["id"] for r in lst["resources"]]
        check("review queue lists recordings, not the sidecar", side.id not in ids and france.id in ids and lst["total"] >= 4)
        check("role=sidecar can list them", [r["id"] for r in c.get("/api/resources?role=sidecar&status=attached").get_json()["resources"]] == [side.id])

        # ---- background filing through the REAL worker ----------------------------
        c.patch(f"/api/resources/{france.id}", json={"category": "ambient", "captured_at": "2019-03-04T10:00:00Z"})
        resp = c.patch(f"/api/resources/{france.id}", json={"status": "filed"})
        check("PATCH filed returns immediately as 'filing'", resp.status_code == 200 and resp.get_json()["status"] == "filing")
        check("a client can't set the 'filing' status itself", c.patch(f"/api/resources/{other.id}", json={"status": "filing"}).status_code == 400)
        deadline = time.time() + 90
        while time.time() < deadline:
            db.session.expire_all()
            if db.session.get(Resource, france.id).status in ("filed", "failed"):
                break
            time.sleep(2)
        db.session.expire_all()
        f = db.session.get(Resource, france.id)
        want = os.path.join(Config.NAS_LIBRARY_ROOT, "misc", "ambient", "2019", "03", "STE-000.wav")
        check("the worker filed it to the NAS", f.status == "filed" and f.nas_path == want and os.path.exists(want), f"{f.status} {f.failure_detail}")
        db.session.expire_all()
        s = db.session.get(Resource, side.id)
        check("its sidecar was copied next to it", s.status == "filed" and s.nas_path == want + ".reapeaks" and os.path.exists(s.nas_path), f"{s.status} {s.failure_detail}")
        from jobs.filing import tidy_staging_dir
        d1 = os.path.join(tmp_stage, "inbox", "zzdir"); os.makedirs(d1); open(os.path.join(d1, "x"), "w").close(); os.remove(os.path.join(d1, "x"))
        tidy_staging_dir(os.path.join(d1, "x"))
        f2 = os.path.join(tmp_stage, "keep.wav"); open(f2, "w").close(); os.remove(f2)
        tidy_staging_dir(f2)
        check("emptied per-file staging dir is removed; the staging root never is", not os.path.exists(d1) and os.path.isdir(tmp_stage))
        check("filed sidecar still hidden from the library", side.id not in [r["id"] for r in c.get("/api/resources?status=filed&limit=50").get_json()["resources"]])

        # ---- name clash: the other STE-000.wav has the same loose destination ----------
        c.patch(f"/api/resources/{other.id}", json={"category": "ambient", "captured_at": "2019-03-04T11:00:00Z"})
        c.patch(f"/api/resources/{other.id}", json={"status": "filed"})
        from jobs.filing import file_resources
        deadline = time.time() + 60
        while time.time() < deadline:            # the worker may or may not already have taken it
            db.session.expire_all()
            if db.session.get(Resource, other.id).status in ("filed", "failed"):
                break
            time.sleep(2)
        db.session.expire_all()
        o = db.session.get(Resource, other.id)
        check("same name into the same folder fails safely, file kept in staging", o.status == "failed" and o.failure_stage == "move" and "refusing to overwrite" in o.failure_detail and os.path.exists(o.staging_path), (o.failure_detail or "")[:70])
        check("the first file was not overwritten", os.path.exists(want))
        sess = c.post("/api/sessions", json={"name": "ZZTEST second folder"}).get_json()
        c.patch(f"/api/resources/{other.id}", json={"session_id": sess["id"]})
        r = c.patch(f"/api/resources/{other.id}", json={"status": "filed"})
        check("a failed file can be re-filed after fixing the cause", r.status_code == 200 and r.get_json()["status"] == "filing")
        deadline = time.time() + 60
        while time.time() < deadline:
            db.session.expire_all()
            if db.session.get(Resource, other.id).status in ("filed", "failed"):
                break
            time.sleep(2)
        db.session.expire_all()
        o = db.session.get(Resource, other.id)
        check("re-filed into its own session folder", o.status == "filed" and o.nas_path.endswith("/ZZTEST second folder/STE-000.wav") and os.path.exists(o.nas_path), o.nas_path or o.failure_detail)
        print("\nALL WP5 CHECKS PASSED")
finally:
    rj.subprocess.run = real_run
    with app.app_context():
        for r in Resource.query.filter(Resource.id.in_(made)).all():
            for x in (r.nas_path, r.staging_path):
                if x and os.path.exists(x) and ("zztest" in x.lower() or "/2019/" in x):
                    os.remove(x)
        for r in Resource.query.filter(Resource.id.in_(made)).all():
            r.derived_from_id = None
        db.session.commit()
        for rid in made:
            FileEvent.query.filter_by(resource_id=rid).delete()
        for r in Resource.query.filter(Resource.id.in_(made)).all():
            r.tags = []
            db.session.delete(r)
        RecordingSession.query.filter(RecordingSession.name.like("ZZTEST%")).delete(synchronize_session=False)
        PendingUpload.query.filter(PendingUpload.path.in_(PATHS_AUDIO + [SIDECAR, ORPHAN_SIDECAR] + OTHER)).delete(synchronize_session=False)
        db.session.commit()
        for top in (os.path.join(Config.NAS_LIBRARY_ROOT, "misc", "ambient", "2019"),):
            if os.path.isdir(top):
                shutil.rmtree(top)
        shutil.rmtree(tmp_stage, ignore_errors=True)
        shutil.rmtree(tmp_src, ignore_errors=True)
        print("cleanup: test resources left =", Resource.query.filter(Resource.id.in_(made)).count(), "| real resources =", Resource.query.count(),
              "| NAS 2019 dir exists =", os.path.isdir(os.path.join(Config.NAS_LIBRARY_ROOT, "misc", "ambient", "2019")))
