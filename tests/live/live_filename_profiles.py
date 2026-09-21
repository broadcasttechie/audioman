"""WP2 live check: recorder profiles, filename ingest, suggestions, precision, enrichment gating.
Rows are removed by id at the end; profile rows by name (ZZTEST prefix)."""
import os
import subprocess
from datetime import datetime

from app import create_app
from app.extensions import db
from app.models import Resource, RecorderProfile, Location, TrackPoint, FileEvent
from config import Config
from jobs.ingest import ingest_staged_file

app = create_app()
made = []
REAL = "66482729-aa93-4704-b3b9-13cdad3a2e7e"


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  [{extra}]" if extra else ""))
    if not cond:
        raise SystemExit(1)


def stage(name, freq, folder=None):
    os.makedirs(Config.STAGING_DIR, exist_ok=True)
    path = os.path.join(Config.STAGING_DIR, name)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration=1", "-ac", "1", path], check=True)
    r = ingest_staged_file(path, drive_inbox_path=(f"{folder}/{name}" if folder else name))
    made.append(r.id)
    return r


try:
    with app.app_context():
        c = app.test_client()

        # ---- schema step on the existing real row ------------------------------
        real = c.get(f"/api/resources/{REAL}").get_json()
        check("existing dated resource was backfilled to exact", real["captured_at_precision"] == "exact", real["captured_at"])

        # ---- profiles API -----------------------------------------------------------
        profs = c.get("/api/recorder-profiles").get_json()
        names = [p["name"] for p in profs]
        check("8 default profiles seeded, in priority order", len(profs) >= 8 and names[0] == "Insta360 mic" and names[1] == "Zoom recorder", str(names[:3]))
        trusted = {p["name"] for p in profs if p["date_trust"] == "trusted"}
        check("only Zoom and Insta360 are trusted", trusted == {"Insta360 mic", "Zoom recorder"}, str(sorted(trusted)))
        check("bad pattern rejected", c.post("/api/recorder-profiles", json={"name": "ZZTEST bad", "patterns": ["{nope}"]}).status_code == 400)
        check("bad timezone rejected", c.post("/api/recorder-profiles", json={"name": "ZZTEST bad", "patterns": ["a{seq}"], "timezone": "Mars/Base"}).status_code == 400)
        created = c.post("/api/recorder-profiles", json={"name": "ZZTEST profile", "patterns": ["zz{seq}"], "priority": 5})
        check("valid profile created", created.status_code == 201)
        pid = created.get_json()["id"]
        check("duplicate name -> 409", c.post("/api/recorder-profiles", json={"name": "ZZTEST profile", "patterns": ["zz{seq}"]}).status_code == 409)
        upd = c.patch(f"/api/recorder-profiles/{pid}", json={"clock_offset_seconds": 120, "date_trust": "trusted"})
        check("profile edited", upd.status_code == 200 and upd.get_json()["clock_offset_seconds"] == 120)
        check("offset must be an integer", c.patch(f"/api/recorder-profiles/{pid}", json={"clock_offset_seconds": "x"}).status_code == 400)
        c.patch(f"/api/recorder-profiles/{pid}", json={"active": False})

        # ---- preview ----------------------------------------------------------------------
        pv = c.post("/api/filename-preview", json={"filename": "Copy of audio_260917_091124_32bit_orig_stereo.wav"}).get_json()
        check("preview: the real file", pv["matched"] and pv["utc"] == "2026-09-17T08:11:24Z" and pv["would_apply"] == "captured_at", pv["utc"])
        pv = c.post("/api/filename-preview", json={"filename": "27-04-2025, 15-42.wav"}).get_json()
        check("preview: phone name is only a suggestion", pv["would_apply"] == "suggestion" and pv["utc"] == "2025-04-27T14:42:00Z")
        check("preview: unmatched name", c.post("/api/filename-preview", json={"filename": "yellow.wav"}).get_json()["matched"] is False)

        # ---- ingest ------------------------------------------------------------------------
        a = stage("250424-121237.-woods-includes-voices.WAV", 301)
        check("trusted Zoom name sets captured_at at ingest", a.captured_at == datetime(2025, 4, 24, 11, 12, 37) and a.captured_at_source == "filename" and a.captured_at_precision == "exact")
        check("title kept from the filename remainder", a.filename_info["title"] == "woods-includes-voices" and a.filename_info["profile"] == "Zoom recorder")
        check("no suggestion when the date was applied", a.suggested_captured_at is None)

        b = stage("audio_000101_000707_24bit_orig.wav", 302)
        check("unset-clock Insta360 file: no date, marked unknown, with a reason",
              b.captured_at is None and b.captured_at_precision == "unknown" and "clock was not set" in b.filename_info["unknown_reason"])

        d = stage("2026-07-08 Down at the station.wav", 303)
        check("suggest-only profile: date stored as a suggestion, not applied",
              d.captured_at is None and d.suggested_captured_at == datetime(2026, 7, 8, 11, 0) and d.filename_info["time_known"] is False)

        e = stage("STE-000.wav", 304, folder="2024 France")
        check("STE file: no date, folder kept as a hint", e.captured_at is None and e.filename_info["folder"] == "2024 France" and e.filename_info["seq"] == "000")

        f = stage("yellow.wav", 305)
        check("unmatched name: nothing guessed", f.captured_at is None and f.suggested_captured_at is None and f.filename_info["profile"] is None)

        g = stage("250424-121237-Parkridge Nature Reserve-EDIT.WAV.wav", 306)
        check("EDIT version recognised", g.filename_info["is_edit"] is True and g.filename_info["title"] == "Parkridge Nature Reserve")

        j = c.get(f"/api/resources/{d.id}").get_json()
        check("API exposes suggestion and info", j["suggested_captured_at"] == "2026-07-08T11:00:00Z" and j["captured_at_precision"] == "unknown" and j["filename_info"]["profile"] == "Date and title")

        # ---- confirming a suggestion, precision rules -----------------------------------------------
        r = c.patch(f"/api/resources/{d.id}", json={"use_suggested_date": True})
        jr = r.get_json()
        check("use_suggested_date: date-only -> approximate", r.status_code == 200 and jr["captured_at"] == "2026-07-08T11:00:00Z" and jr["captured_at_precision"] == "approximate" and jr["captured_at_source"] == "filename" and jr["suggested_captured_at"] is None)
        check("no suggestion left to use", c.patch(f"/api/resources/{d.id}", json={"use_suggested_date": True}).status_code == 400)
        check("approximate date refuses location refresh", c.post(f"/api/resources/{d.id}/location/refresh").status_code == 400)
        check("approximate date refuses photo refresh", c.post(f"/api/resources/{d.id}/photos/refresh").status_code == 400)
        check("cannot set precision 'unknown' directly", c.patch(f"/api/resources/{d.id}", json={"captured_at_precision": "unknown"}).status_code == 400)
        check("cannot qualify a missing date", c.patch(f"/api/resources/{b.id}", json={"captured_at_precision": "exact"}).status_code == 400)
        manual = c.patch(f"/api/resources/{b.id}", json={"captured_at": "2026-09-13T17:45:00Z"}).get_json()
        check("typing a date by hand -> exact", manual["captured_at_precision"] == "exact" and manual["captured_at_source"] == "manual")

        # ---- enrichment gating (runs the real job against the real Dawarich) --------------------------
        from jobs.enrich import enrich_locations
        db.session.expire_all()
        out = enrich_locations()
        db.session.expire_all()
        check("exact-dated resource was enriched", db.session.get(Resource, a.id).dawarich_checked_at is not None and a.id in out["checked"])
        check("approximate-dated resource was NOT looked up", db.session.get(Resource, d.id).dawarich_checked_at is None and d.id not in out["checked"])

        # ---- changing the time clears derived data, keeps manual location ------------------------------
        Location.query.filter_by(resource_id=a.id).delete()  # the real enrichment above may have stored one
        TrackPoint.query.filter_by(resource_id=a.id).delete()
        db.session.commit()
        db.session.add(Location(resource_id=a.id, lat=1.0, lon=2.0, source="dawarich-auto"))
        db.session.add(TrackPoint(resource_id=a.id, recorded_at=datetime(2025, 4, 24, 11, 13), lat=1.0, lon=2.0))
        db.session.commit()
        c.patch(f"/api/resources/{a.id}", json={"captured_at": "2025-04-24T12:00:00Z"})
        db.session.expire_all()
        ra = db.session.get(Resource, a.id)
        check("date change removes stale auto location + track and re-queues", ra.location is None and TrackPoint.query.filter_by(resource_id=a.id).count() == 0 and ra.dawarich_checked_at is None)
        c.patch(f"/api/resources/{a.id}", json={"location": {"lat": 51.5, "lon": -0.1, "source": "manual"}})
        c.patch(f"/api/resources/{a.id}", json={"captured_at": "2025-04-24T13:00:00Z"})
        db.session.expire_all()
        check("a manual location survives a date change", db.session.get(Resource, a.id).location.source == "manual")
        c.patch(f"/api/resources/{a.id}", json={"captured_at_precision": "approximate"})
        check("same-value PATCH does not reset anything", c.patch(f"/api/resources/{a.id}", json={"captured_at": "2025-04-24T13:00:00Z", "captured_at_precision": "approximate"}).status_code == 200)
        check("clearing the date makes it unknown", c.patch(f"/api/resources/{a.id}", json={"captured_at": None}).get_json()["captured_at_precision"] == "unknown")
        check("the real resource is untouched by all this", c.get(f"/api/resources/{REAL}").get_json()["captured_at"] == real["captured_at"])
        print("\nALL WP2 CHECKS PASSED")
finally:
    with app.app_context():
        for rid in made:
            TrackPoint.query.filter_by(resource_id=rid).delete()
            Location.query.filter_by(resource_id=rid).delete()
            FileEvent.query.filter_by(resource_id=rid).delete()
            r = db.session.get(Resource, rid)
            if r:
                r.tags = []
                for p in (r.staging_path,):
                    if p and os.path.exists(p):
                        os.remove(p)
                db.session.delete(r)
        RecorderProfile.query.filter(RecorderProfile.name.like("ZZTEST%")).delete(synchronize_session=False)
        db.session.commit()
        print("cleanup: created rows removed =", len(made), "| real resources =", Resource.query.count(), "| profiles =", RecorderProfile.query.count())
