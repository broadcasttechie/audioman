"""
Android app backend live check: device tokens, the chunked/resumable upload protocol, metadata-
at-upload, and browse/playback/export delegation under device auth (real DB, NAS, worker, ffmpeg).
ZZTEST rows/devices/files are removed in `finally`. Never modifies the one real resource, only
reads it (checksum-query dedupe, a delegated export/download).
"""
import glob
import hashlib
import os
import shutil
import subprocess
import time

from app import create_app
from app.extensions import db
from app.models import Resource, DeviceToken, UploadSession, Project, RecordingSession, Tag, FileEvent
from config import Config
from jobs import device_uploads

app = create_app()
REAL = "66482729-aa93-4704-b3b9-13cdad3a2e7e"
HDR = "X-Device-Token"
device_ids, resource_ids, project_ids, session_ids, tag_ids = [], [], [], [], []


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  [{extra}]" if extra else ""))
    if not cond:
        raise SystemExit(1)


def make_wav(name, secs, freq=440, rate=48000, channels=1):
    os.makedirs(Config.STAGING_DIR, exist_ok=True)
    path = os.path.join(Config.STAGING_DIR, name)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={secs}",
                    "-ar", str(rate), "-ac", str(channels), path], check=True)
    return path


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


try:
    with app.app_context():
        c = app.test_client()

        # ================= device management (web UI auth) =================
        r = c.post("/api/devices", json={"label": "ZZTEST phone"})
        check("creating a device is accepted", r.status_code == 201)
        d = r.get_json()
        device_ids.append(d["id"])
        token = d["token"]
        check("the raw token is a real secret, not echoed back on later reads", len(token) >= 32)
        headers = {HDR: token}

        listed = c.get("/api/devices").get_json()
        check("the device shows up in the list, without its token", any(x["id"] == d["id"] and "token" not in x for x in listed))

        # ================= auth =================
        check("no token is refused", c.get("/api/device/v1/ping").status_code == 401)
        check("a wrong token is refused", c.get("/api/device/v1/ping", headers={HDR: "not-a-real-token"}).status_code == 401)
        r = c.get("/api/device/v1/ping", headers=headers)
        check("a valid token pings ok and reports the device label", r.status_code == 200 and r.get_json()["device"] == "ZZTEST phone")

        c.post(f"/api/devices/{d['id']}/revoke")
        check("a revoked token stops working", c.get("/api/device/v1/ping", headers=headers).status_code == 401)
        c.post(f"/api/devices/{d['id']}/unrevoke")
        check("unrevoking brings the SAME token back to life", c.get("/api/device/v1/ping", headers=headers).status_code == 200)

        # ================= checksum dedupe query =================
        real_checksum = db.session.get(Resource, REAL).checksum
        out = c.post("/api/device/v1/checksums/query", json={"checksums": [real_checksum, "0" * 64]}, headers=headers).get_json()
        check("the real resource's checksum is reported as already present", out[real_checksum] == {"exists": True, "resource_id": REAL})
        check("an unknown checksum is reported absent", out["0" * 64] == {"exists": False})

        # ================= idempotent project/session creation, delegated =================
        import uuid as _uuid
        pid = str(_uuid.uuid4())
        body = {"id": pid, "name": "ZZTEST Device Project", "slug": "zztest-device-project", "placement": "nas", "home": "nas"}
        r1 = c.post("/api/device/v1/projects", json=body, headers=headers)
        r2 = c.post("/api/device/v1/projects", json=body, headers=headers)
        check("creating a project via the device API works and is idempotent on retry",
             r1.status_code == 201 and r2.status_code == 200 and r1.get_json()["id"] == r2.get_json()["id"] == pid)
        project_ids.append(pid)

        sid = str(_uuid.uuid4())
        sbody = {"id": sid, "project_id": pid, "name": "ZZTEST Night"}
        r = c.post("/api/device/v1/sessions", json=sbody, headers=headers)
        check("creating a session via the device API works", r.status_code == 201 and r.get_json()["id"] == sid)
        session_ids.append(sid)

        # ================= full chunked upload, in several pieces, with a rich metadata block ====
        p1 = make_wav("zztest_device_upload1.wav", 4, freq=555)
        size1, checksum1 = os.path.getsize(p1), sha256_of(p1)
        meta = {
            "project_id": pid, "session_id": sid, "category": "ambient", "tags": ["zztest-gig", "ZZTest-Gig"],
            "title": "A device upload", "notes": "from the live check", "recorder_hint": "ZZTEST recorder",
            "source_path": "DCIM/Recordings/zztest_device_upload1.wav",
            "captured_at": "2026-05-01T09:00:00Z", "captured_at_precision": "approximate",
        }
        r = c.post("/api/device/v1/uploads", json={"filename": os.path.basename(p1), "size_bytes": size1, "checksum": checksum1, "metadata": meta}, headers=headers)
        check("initiate is accepted", r.status_code == 201, str(r.get_json())[:200])
        up = r.get_json()
        upload_id = up["upload_id"]
        check("starts at 0 bytes received", up["bytes_received"] == 0)

        with open(p1, "rb") as f:
            raw = f.read()
        third = len(raw) // 3
        chunks = [raw[:third], raw[third:2 * third], raw[2 * third:]]

        r = c.put(f"/api/device/v1/uploads/{upload_id}/chunk?offset=0", data=chunks[0], headers={**headers, "Content-Type": "application/octet-stream"})
        check("first chunk accepted", r.status_code == 200 and r.get_json()["bytes_received"] == len(chunks[0]))

        # Wrong offset (simulating a resume after a dropped connection that guessed wrong): refused,
        # and told the correct offset to resume from.
        r = c.put(f"/api/device/v1/uploads/{upload_id}/chunk?offset=0", data=chunks[1], headers={**headers, "Content-Type": "application/octet-stream"})
        check("a stale/wrong offset is refused with the correct one to resume from",
             r.status_code == 409 and r.get_json()["bytes_received"] == len(chunks[0]))

        # "Reconnect": GET status instead of trusting local state, then resume from exactly that offset.
        status = c.get(f"/api/device/v1/uploads/{upload_id}", headers=headers).get_json()
        check("status reports the true bytes_received after a 'reconnect'", status["bytes_received"] == len(chunks[0]))
        r = c.put(f"/api/device/v1/uploads/{upload_id}/chunk?offset={status['bytes_received']}", data=chunks[1], headers={**headers, "Content-Type": "application/octet-stream"})
        check("resuming from the reported offset works", r.status_code == 200)
        r = c.put(f"/api/device/v1/uploads/{upload_id}/chunk?offset={len(chunks[0]) + len(chunks[1])}", data=chunks[2], headers={**headers, "Content-Type": "application/octet-stream"})
        check("final chunk completes the byte count", r.status_code == 200 and r.get_json()["bytes_received"] == size1)

        r = c.post(f"/api/device/v1/uploads/{upload_id}/complete", headers=headers)
        check("complete succeeds and returns the new resource", r.status_code == 201, str(r.get_json())[:200])
        res = r.get_json()
        resource_ids.append(res["id"])
        check("category/project/session applied from metadata", res["category"] == "ambient" and res["project_id"] == pid and res["session_id"] == sid)
        check("title+notes combined into notes", res["notes"] == "A device upload\n\nfrom the live check", res["notes"])
        check("captured_at applied as a manual, approximate override", res["captured_at"] == "2026-05-01T09:00:00Z" and res["captured_at_precision"] == "approximate")
        tag_names = {t for t in res["tags"]}
        check("tags resolved (case-insensitive dedupe: two spellings of the same tag -> one tag)", tag_names == {"zztest-gig"}, str(tag_names))
        db.session.expire_all()
        real_tag = Tag.query.filter(db.func.lower(Tag.name) == "zztest-gig").first()
        if real_tag:
            tag_ids.append(real_tag.id)
        check("filename_info records the device source folder as a hint", db.session.get(Resource, res["id"]).filename_info.get("folder") == "DCIM/Recordings")
        check("re-completing an already-completed upload is idempotent (200, same resource)",
             c.post(f"/api/device/v1/uploads/{upload_id}/complete", headers=headers).get_json()["id"] == res["id"])

        # ================= duplicate checksum: short-circuited, nothing new made =================
        r = c.post("/api/device/v1/uploads", json={"filename": "again.wav", "size_bytes": size1, "checksum": checksum1}, headers=headers)
        check("initiating an upload of an already-known checksum short-circuits as a duplicate",
             r.status_code == 200 and r.get_json() == {"status": "duplicate", "resource_id": res["id"]})

        # ================= checksum mismatch at complete: refused, cleaned up, session failed =====
        p2 = make_wav("zztest_device_upload2.wav", 2, freq=777)
        size2 = os.path.getsize(p2)
        r = c.post("/api/device/v1/uploads", json={"filename": "zztest_mismatch.wav", "size_bytes": size2, "checksum": "1" * 64}, headers=headers)
        up2 = r.get_json()
        with open(p2, "rb") as f:
            raw2 = f.read()
        c.put(f"/api/device/v1/uploads/{up2['upload_id']}/chunk?offset=0", data=raw2, headers={**headers, "Content-Type": "application/octet-stream"})
        r = c.post(f"/api/device/v1/uploads/{up2['upload_id']}/complete", headers=headers)
        check("a checksum mismatch at complete is refused (422)", r.status_code == 422, str(r.get_json())[:200])
        db.session.expire_all()
        failed_session = db.session.get(UploadSession, up2["upload_id"])
        check("the session is marked failed and its staging file is gone",
             failed_session.status == "failed" and failed_session.staging_path is None)
        check("chunking to an already-failed session is refused (410), never silently resumed",
             c.put(f"/api/device/v1/uploads/{up2['upload_id']}/chunk?offset=0", data=b"x", headers={**headers, "Content-Type": "application/octet-stream"}).status_code == 410)

        # ================= oversized chunk refused =================
        real_max = Config.DEVICE_UPLOAD_CHUNK_MAX_BYTES
        Config.DEVICE_UPLOAD_CHUNK_MAX_BYTES = 100
        try:
            p3 = make_wav("zztest_device_upload3.wav", 1, freq=999)
            r = c.post("/api/device/v1/uploads", json={"filename": os.path.basename(p3), "size_bytes": os.path.getsize(p3), "checksum": sha256_of(p3)}, headers=headers)
            up3 = r.get_json()
            with open(p3, "rb") as f:
                big_chunk = f.read()
            check("a chunk bigger than the configured max is refused (413)",
                 len(big_chunk) > 100 and c.put(f"/api/device/v1/uploads/{up3['upload_id']}/chunk?offset=0", data=big_chunk, headers={**headers, "Content-Type": "application/octet-stream"}).status_code == 413)
        finally:
            Config.DEVICE_UPLOAD_CHUNK_MAX_BYTES = real_max

        # ================= disk admission: no room right now =================
        real_budget = Config.STAGING_BUDGET_GB
        Config.STAGING_BUDGET_GB = 0.0000001
        try:
            r = c.post("/api/device/v1/uploads", json={"filename": "zztest_no_room.wav", "size_bytes": 5_000_000, "checksum": "2" * 64,
                                                        "metadata": {"tags": ["zztest-should-not-be-created"]}}, headers=headers)
            check("initiate refuses with 'try later' when there is no room", r.status_code in (413, 503), str(r.get_json())[:200])
        finally:
            Config.STAGING_BUDGET_GB = real_budget
        db.session.expire_all()
        check("a tag from the rejected request's metadata was never actually created (rolled back)",
             Tag.query.filter_by(name="zztest-should-not-be-created").first() is None)

        # ================= metadata validation errors, none of them create anything ================
        def initiate_expect_400(meta, why):
            r = c.post("/api/device/v1/uploads", json={"filename": "zztest_bad.wav", "size_bytes": 10, "checksum": "3" * 64, "metadata": meta}, headers=headers)
            check(why, r.status_code == 400, str(r.get_json())[:150])

        initiate_expect_400({"project_id": "not-a-real-id"}, "unknown project_id is refused")
        initiate_expect_400({"session_id": "not-a-real-id"}, "unknown session_id is refused")
        initiate_expect_400({"session_id": sid, "project_id": "00000000-0000-4000-8000-000000000000"}, "a project_id that contradicts session_id's project is refused")
        initiate_expect_400({"category": "not-a-real-category"}, "an unknown category is refused")
        initiate_expect_400({"captured_at": "not a date"}, "a malformed captured_at is refused")
        initiate_expect_400({"tags": "not-a-list"}, "tags must be a list")
        check("none of the rejected initiates created an UploadSession",
             UploadSession.query.filter_by(declared_checksum="3" * 64).count() == 0)

        # ================= browse/playback/export delegation under device auth =====================
        lib = c.get("/api/device/v1/library?limit=1", headers=headers).get_json()
        check("library browse delegates correctly (same shape as the web API)", "resources" in lib and "total" in lib)
        detail = c.get(f"/api/device/v1/resources/{REAL}", headers=headers).get_json()
        check("resource detail delegates correctly", detail["id"] == REAL)
        wf = c.get(f"/api/device/v1/resources/{REAL}/waveform", headers=headers)
        check("waveform streaming delegates correctly", wf.status_code == 200, str(wf.status_code))
        check("playback endpoints are refused without a device token", c.get(f"/api/device/v1/resources/{REAL}/waveform").status_code == 401)

        r = c.post(f"/api/device/v1/resources/{res['id']}/export", json={"format": "wav"}, headers=headers)
        check("export delegates correctly", r.status_code == 202, str(r.get_json())[:200])
        export_id = r.get_json()["id"]
        end = time.time() + 60
        while time.time() < end:
            exp = c.get(f"/api/device/v1/exports/{export_id}", headers=headers).get_json()
            if exp["status"] in ("success", "error"):
                break
            time.sleep(2)
        check("the delegated export finished through the real worker", exp["status"] == "success", str(exp)[:200])
        dl = c.get(f"/api/device/v1/exports/{export_id}/download", headers=headers)
        check("the delegated download is real audio", dl.status_code == 200 and len(dl.data) > 1000, str(dl.status_code))

        # ================= abandoned-upload cleanup sweeper ==========================================
        p4 = make_wav("zztest_device_abandoned.wav", 1, freq=111)
        r = c.post("/api/device/v1/uploads", json={"filename": os.path.basename(p4), "size_bytes": os.path.getsize(p4), "checksum": sha256_of(p4)}, headers=headers)
        abandoned_id = r.get_json()["upload_id"]
        s = db.session.get(UploadSession, abandoned_id)
        s.updated_at = s.created_at = __import__("datetime").datetime.utcnow() - __import__("datetime").timedelta(hours=Config.DEVICE_UPLOAD_ABANDONED_HOURS + 1)
        staged_path = s.staging_path
        db.session.commit()
        out = device_uploads.cleanup_stale_uploads()
        check("the sweeper ran cleanly and found the abandoned session", out["status"] == "success" and out["abandoned"] >= 1, str(out))
        db.session.expire_all()
        s = db.session.get(UploadSession, abandoned_id)
        check("the abandoned session is failed and its partial file removed", s.status == "failed" and not os.path.exists(staged_path))

        print("\nALL DEVICE API CHECKS PASSED")
finally:
    with app.app_context():
        # Upload sessions reference a Resource (resource_id) once completed and a DeviceToken
        # always, so they must go first, or deleting either trips a foreign key. Matched by
        # device_id, not a filename pattern, so a session named without the zztest prefix (a
        # deliberately-odd name for a test case) can't be missed.
        for did in device_ids:
            for s in UploadSession.query.filter_by(device_id=did).all():
                db.session.delete(s)
        db.session.commit()
        for rid in resource_ids:
            r = db.session.get(Resource, rid)
            if r:
                from jobs import previews as pv
                pv.delete_cache(r.checksum)
                for p in (r.staging_path, r.nas_path):
                    if p and os.path.exists(p) and os.path.basename(p).startswith("zztest"):
                        os.remove(p)
                FileEvent.query.filter_by(resource_id=rid).delete()
                r.tags = []
                db.session.delete(r)
        db.session.commit()
        for sid in session_ids:
            s = db.session.get(RecordingSession, sid)
            if s:
                db.session.delete(s)
        db.session.commit()
        for pid in project_ids:
            p = db.session.get(Project, pid)
            if p:
                db.session.delete(p)
        db.session.commit()
        for tid in tag_ids:
            t = db.session.get(Tag, tid)
            if t:
                db.session.delete(t)
        db.session.commit()
        for did in device_ids:
            dev = db.session.get(DeviceToken, did)
            if dev:
                db.session.delete(dev)
        db.session.commit()
        for f in glob.glob(os.path.join(Config.STAGING_DIR, "zztest*")):
            os.remove(f)
        for d in glob.glob(os.path.join(Config.DEVICE_UPLOAD_DIR, "*")):
            shutil.rmtree(d, ignore_errors=True)
        print("cleanup done; real resources =", Resource.query.count())
