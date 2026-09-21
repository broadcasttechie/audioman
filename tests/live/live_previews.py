"""Waveform/preview live check (real DB, NAS, worker, ffmpeg, audiowaveform). ZZTEST rows are removed after;
the real resource's derived files are deliberately kept (they are wanted)."""
import os
import subprocess
import tempfile
import time

from app import create_app
from app.extensions import db
from app.models import Resource, FileEvent
from config import Config
from jobs import previews as pv
from jobs.ingest import ingest_staged_file

app = create_app()
REAL = "66482729-aa93-4704-b3b9-13cdad3a2e7e"
ids = []


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  [{extra}]" if extra else ""))
    if not cond:
        raise SystemExit(1)


def stage(name, freq, secs):
    os.makedirs(Config.STAGING_DIR, exist_ok=True)
    path = os.path.join(Config.STAGING_DIR, name)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={secs}", "-ac", "2", path], check=True)
    r = ingest_staged_file(path)
    ids.append(r.id)
    return r, path


def poll(c, url, want=200, secs=180, **kw):
    end = time.time() + secs
    resp = None
    while time.time() < end:
        resp = c.get(url, **kw)
        if resp.status_code == want:
            return resp
        if resp.status_code == 500 and want != 500:
            return resp
        time.sleep(3)
    return resp


try:
    with app.app_context():
        c = app.test_client()
        # ---- a new file: asking for the waveform starts generation, the worker delivers it ----------
        r, path = stage("ZZTEST_pv.wav", 350, 5)
        first = c.get(f"/api/resources/{r.id}/waveform")
        check("waveform not ready yet is 202 (or already done), never an error", first.status_code in (200, 202))
        w = poll(c, f"/api/resources/{r.id}/waveform")
        check("the worker generated the waveform", w.status_code == 200 and w.mimetype == "application/octet-stream", str(w.status_code))
        tmp = tempfile.NamedTemporaryFile(suffix=".dat", delete=False); tmp.write(w.data); tmp.close()
        h = pv.read_waveform_header(tmp.name); os.unlink(tmp.name)
        check("served waveform is valid, 5 s at 100 peaks/s", abs(h["duration"] - 5.0) < 0.05 and h["length"] in range(499, 502), f"{h['length']} peaks, spp {h['samples_per_pixel']}")
        p = poll(c, f"/api/resources/{r.id}/preview")
        check("the worker generated the listening copy", p.status_code == 200 and p.mimetype == "audio/mp4", str(p.status_code))
        rng = c.get(f"/api/resources/{r.id}/preview", headers={"Range": "bytes=0-99"})
        check("preview supports Range (206) with the right length", rng.status_code == 206 and len(rng.data) == 100 and "bytes 0-99/" in rng.headers.get("Content-Range", ""))
        db.session.expire_all()
        d = c.get(f"/api/resources/{r.id}").get_json()
        check("resource reports has_waveform/has_preview and its size", d["has_waveform"] and d["has_preview"] and d["size_bytes"] and not d["waveform_error"])

        # ---- a broken source fails loudly, is remembered, and can be retried ---------------------------
        r2, path2 = stage("ZZTEST_pv2.wav", 360, 3)
        good = open(path2, "rb").read()
        open(path2, "wb").write(b"not audio " * 500)
        c.get(f"/api/resources/{r2.id}/waveform")
        bad = poll(c, f"/api/resources/{r2.id}/waveform", want=500, secs=120)
        check("a broken file gives a clear failure, not silence", bad.status_code == 500 and bad.get_json()["status"] == "failed" and bad.get_json()["error"], (bad.get_json() or {}).get("error", "")[:70])
        db.session.expire_all()
        check("the failure is stored on the resource and logged", db.session.get(Resource, r2.id).waveform_error and FileEvent.query.filter_by(resource_id=r2.id, event_type="failed").count() >= 1)
        open(path2, "wb").write(good)
        check("regenerate is accepted", c.post(f"/api/resources/{r2.id}/previews/regenerate").status_code == 202)
        ok = poll(c, f"/api/resources/{r2.id}/waveform")
        check("after the file is fixed, regenerate succeeds", ok.status_code == 200)

        # ---- the real recording (on the NAS) ---------------------------------------------------------------
        t0 = time.time()
        wr = poll(c, f"/api/resources/{REAL}/waveform", secs=240)
        pr = poll(c, f"/api/resources/{REAL}/preview", secs=240)
        check("the real 15-minute recording has both", wr.status_code == 200 and pr.status_code == 200, f"{time.time() - t0:.0f}s")
        meta_path = pv.waveform_path(db.session.get(Resource, REAL).checksum)[:-4] + ".json"
        import json
        meta = json.load(open(meta_path))
        check("real waveform covers the real duration", abs(meta["duration"] - 901.8) < 1.5 and meta["engine"] == "audiowaveform", f"{meta['duration']:.2f}s, {meta['engine']} {meta['engine_version']}")
        check("real preview is much smaller than the original", 0 < len(pr.data) < 30_000_000 if False else os.path.getsize(pv.preview_path(db.session.get(Resource, REAL).checksum)) < 30_000_000)
        d = c.get(f"/api/resources/{REAL}").get_json()
        check("size backfilled for the existing resource", d["size_bytes"] and d["size_bytes"] > 300_000_000, str(d["size_bytes"]))

        # ---- NAS unavailable: resources on the NAS are skipped, not marked failed --------------------------------
        c.post(f"/api/resources/{REAL}/previews/regenerate")
        real_marker = Config.NAS_MARKER_FILE
        Config.NAS_MARKER_FILE = ".no-such-marker"
        try:
            out = pv.generate_previews()
        finally:
            Config.NAS_MARKER_FILE = real_marker
        db.session.expire_all()
        rr = db.session.get(Resource, REAL)
        check("with the NAS down, NAS-hosted files are skipped, not failed", out["status"] == "success" and rr.waveform_error is None and rr.preview_error is None)
        wr2 = poll(c, f"/api/resources/{REAL}/waveform", secs=120)
        check("and once it is back they are generated", wr2.status_code == 200)
        # ---- a resource that vanishes mid-run must not break the sweeper (found by a regression run) ----------
        import uuid as _uuid
        real_ids = pv._candidate_ids
        pv._candidate_ids = lambda: [str(_uuid.uuid4())] + real_ids()
        try:
            out = pv.generate_previews()
        finally:
            pv._candidate_ids = real_ids
        check("the previews sweeper skips an id that no longer exists", out["status"] in ("success", "partial"))
        import jobs.filing as filing
        out = filing.file_resources()
        check("the filing sweeper runs cleanly when nothing is waiting", out["status"] == "success")
        from app.extensions import db as _db
        _db.session.execute(_db.text("SELECT 1"))
        check("the session is still healthy afterwards", True)
        print("\nALL PREVIEW CHECKS PASSED")
finally:
    with app.app_context():
        for rid in ids:
            r = db.session.get(Resource, rid)
            if r:
                pv.delete_cache(r.checksum)
                for x in (r.staging_path, r.nas_path):
                    if x and os.path.basename(x).startswith("ZZTEST") and os.path.exists(x):
                        os.remove(x)
                FileEvent.query.filter_by(resource_id=rid).delete()
                r.tags = []
                db.session.delete(r)
        db.session.commit()
        for f in os.listdir(Config.STAGING_DIR):
            if f.startswith("ZZTEST"):
                os.remove(os.path.join(Config.STAGING_DIR, f))
        print("cleanup: real resources =", Resource.query.count())
