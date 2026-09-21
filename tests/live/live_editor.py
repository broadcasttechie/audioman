"""The calls the waveform editor makes, against the real API and worker: clip create/edit/delete with the exact rounding the
page uses, the end-of-recording edge, and an export through the worker. ZZTEST rows and export files are removed after."""
import math
import os
import subprocess
import time

from app import create_app
from app.extensions import db
from app.models import Resource, Clip, Export, FileEvent
from config import Config
from jobs.ingest import ingest_staged_file

app = create_app()
ids = []


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  [{extra}]" if extra else ""))
    if not cond:
        raise SystemExit(1)


try:
    with app.app_context():
        c = app.test_client()
        for p in ("/static/waveform.js",):
            check(f"{p} serves", c.get(p).status_code == 200)
        os.makedirs(Config.STAGING_DIR, exist_ok=True)
        path = os.path.join(Config.STAGING_DIR, "ZZTEST_editor.wav")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=500:duration=3.2371", "-ac", "1", path], check=True)
        r = ingest_staged_file(path)
        ids.append(r.id)
        dur = r.duration_seconds
        check("a test recording with an awkward length", 3.2 < dur < 3.3, str(dur))
        ms = lambda x: math.floor(x * 1000) / 1000              # the page's rounding

        made = c.post(f"/api/resources/{r.id}/clips", json={"start_seconds": ms(0.5), "end_seconds": ms(dur), "label": "To the very end"})
        check("a clip to the very end of the recording is accepted with the page's rounding", made.status_code == 201, str(made.get_json()))
        naive = round(dur, 3) if round(dur, 3) > dur else dur + 0.0005
        check("(and rounding to nearest could have been refused, which is why the page rounds down)", c.post(f"/api/resources/{r.id}/clips", json={"start_seconds": 0.5, "end_seconds": naive}).status_code in (201, 400))
        check("past the end is refused", c.post(f"/api/resources/{r.id}/clips", json={"start_seconds": 0.5, "end_seconds": dur + 0.5}).status_code == 400)
        check("end before start is refused", c.post(f"/api/resources/{r.id}/clips", json={"start_seconds": 2, "end_seconds": 1}).status_code == 400)
        cid = made.get_json()["id"]
        check("rename", c.patch(f"/api/clips/{cid}", json={"label": "Blackbird"}).get_json()["label"] == "Blackbird")
        upd = c.patch(f"/api/clips/{cid}", json={"start_seconds": 1.0, "end_seconds": 2.0}).get_json()
        check("update the range from a new selection", (upd["start_seconds"], upd["end_seconds"]) == (1.0, 2.0))
        check("the clip list carries the label and range the editor draws", [(x["label"], x["start_seconds"]) for x in c.get(f"/api/resources/{r.id}/clips").get_json() if x["id"] == cid] == [("Blackbird", 1.0)])

        # ---- export through the real worker ---------------------------------------------------------------------
        results = {}
        for fmt in ("original", "wav", "flac", "mp3"):
            e = c.post(f"/api/resources/{r.id}/export", json={"clip_id": cid, "format": fmt})
            check(f"export as {fmt} is accepted", e.status_code == 202, str(e.get_json()))
            eid = e.get_json()["id"]
            end = time.time() + 90
            status = None
            while time.time() < end:
                status = c.get(f"/api/exports/{eid}").get_json()
                if status["status"] in ("success", "error"):
                    break
                time.sleep(2)
            check(f"the worker finished the {fmt} export", status["status"] == "success", str(status.get("error_detail")))
            dl = c.get(f"/api/exports/{eid}/download")
            results[fmt] = (dl.status_code, len(dl.data), eid)
            check(f"the {fmt} download is real audio", dl.status_code == 200 and len(dl.data) > 1000, str(results[fmt][:2]))
        check("a WAV of a 1.0 s mono clip at 44.1 kHz is about 88 KB", 80_000 < results["wav"][1] < 100_000, str(results["wav"][1]))
        check("deleting a clip that has been exported works, and the exports are kept", c.delete(f"/api/clips/{cid}").status_code == 204
              and Export.query.filter_by(resource_id=r.id).count() == 4 and Export.query.filter_by(resource_id=r.id, clip_id=None).count() == 4)
        db.session.expire_all()
        check("the kept exports still download", all(c.get(f"/api/exports/{res[2]}/download").status_code == 200 for res in results.values()))
        print("\nALL EDITOR CHECKS PASSED")
finally:
    with app.app_context():
        for rid in ids:
            for e in Export.query.filter_by(resource_id=rid).all():
                for f in (getattr(e, "output_path", None),):
                    if f and os.path.exists(f):
                        os.remove(f)
                db.session.delete(e)
            Clip.query.filter_by(resource_id=rid).delete()
            FileEvent.query.filter_by(resource_id=rid).delete()
            r = db.session.get(Resource, rid)
            if r:
                r.tags = []
                if r.staging_path and os.path.exists(r.staging_path):
                    os.remove(r.staging_path)
                db.session.delete(r)
        db.session.commit()
        for d in (Config.EXPORTS_DIR,):
            if os.path.isdir(d):
                for f in os.listdir(d):
                    if f.startswith("ZZTEST_editor"):
                        os.remove(os.path.join(d, f))
        print("cleanup: real resources =", Resource.query.count())
