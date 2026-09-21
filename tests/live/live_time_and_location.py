"""WP1 end-to-end check on the container: time contract, tag validation, Dawarich
epoch handling and location refresh. All rows are ZZTEST and removed at the end."""
import math
import os
import subprocess
from datetime import datetime

from app import create_app
from app.extensions import db
from app.models import Resource, Tag, Location, TrackPoint, FileEvent
from config import Config

app = create_app()
STATION = (53.0079, -2.1804)


def km(a, b):
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(b[1] - a[1]) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  [{extra}]" if extra else ""))
    if not cond:
        raise SystemExit(1)


rid = None
try:
    with app.app_context():
        c = app.test_client()
        r = Resource(checksum="zztest-wp1", filename="ZZTEST_wp1.wav", format="wav", status="pending-review", duration_seconds=901.8)
        db.session.add(r)
        db.session.commit()
        rid = r.id

        # --- time contract over the API -------------------------------------
        def patch(body):
            return c.patch(f"/api/resources/{rid}", json=body)

        for sent in ("2026-09-17T08:11:24Z", "2026-09-17T09:11:24+01:00", "2026-09-17T08:11:24", "2026-09-17T08:11:24.000Z"):
            resp = patch({"captured_at": sent, "captured_at_source": "manual"})
            got = resp.get_json()["captured_at"]
            check(f"PATCH {sent} -> {got}", resp.status_code == 200 and got == "2026-09-17T08:11:24Z")
        check("GET returns the same instant with Z", c.get(f"/api/resources/{rid}").get_json()["captured_at"] == "2026-09-17T08:11:24Z")
        db.session.expire_all()
        check("stored as naive UTC", db.session.get(Resource, rid).captured_at == datetime(2026, 9, 17, 8, 11, 24))
        # the reported bug: type 09:11 BST in the browser -> JS sends 08:11Z -> must read back as the same instant
        check("bug case 09:11 BST round-trips", patch({"captured_at": "2026-09-17T08:11:00.000Z"}).get_json()["captured_at"] == "2026-09-17T08:11:00Z")
        bad = patch({"captured_at": "not a date"})
        check("garbage captured_at -> 400", bad.status_code == 400)
        check("null clears captured_at", patch({"captured_at": None}).get_json()["captured_at"] is None)

        # --- tags: stale / malformed ids ---------------------------------------
        tag = Tag(name="zztest-wp1-tag")
        db.session.add(tag)
        db.session.commit()
        ok = patch({"tags": [tag.id]})
        check("valid tag id accepted", ok.status_code == 200 and ok.get_json()["tags"] == ["zztest-wp1-tag"])
        stale = patch({"tags": [tag.id, "no-such-tag-id"]})
        check("stale tag id -> 400, not a crash", stale.status_code == 400, stale.get_json()["error"][:60])
        check("tags unchanged after the rejected update", c.get(f"/api/resources/{rid}").get_json()["tags"] == ["zztest-wp1-tag"])
        check("non-string tag ids -> 400", patch({"tags": [1, 2]}).status_code == 400)
        check("empty list clears tags", patch({"tags": []}).get_json()["tags"] == [])

        # --- Dawarich: the real file's real moment ----------------------------
        from jobs.dawarich import fetch_track_and_pin
        patch({"captured_at": "2026-09-17T09:11:24+01:00"})
        pts, pin = fetch_track_and_pin(datetime(2026, 9, 17, 8, 11, 24), 901.8)
        check("dawarich returns points (epoch handling; density is low while stationary)", len(pts) >= 5, f"{len(pts)} points")
        check("point timestamps are naive UTC datetimes", isinstance(pts[0]["timestamp"], datetime) and pts[0]["timestamp"].tzinfo is None)
        check("all points inside the requested window", all(datetime(2026, 9, 17, 8, 9, 24) <= p["timestamp"] <= datetime(2026, 9, 17, 8, 28, 27) for p in pts),
              f"{pts[0]['timestamp']} .. {pts[-1]['timestamp']}")
        d = km((pin["lat"], pin["lon"]), STATION) * 1000
        check("pin is at Stoke station", d < 300, f"{d:.0f} m")

        # --- refresh endpoint: replace-not-append, manual location kept -------------
        r1 = c.post(f"/api/resources/{rid}/location/refresh").get_json()
        n1 = TrackPoint.query.filter_by(resource_id=rid).count()
        check("refresh stores track and pin", r1["found"] and n1 == r1["track_points"] and n1 >= 5, f"{n1} points")
        r2 = c.post(f"/api/resources/{rid}/location/refresh").get_json()
        n2 = TrackPoint.query.filter_by(resource_id=rid).count()
        check("second refresh does not duplicate points", n2 == n1, f"{n1} -> {n2}")
        db.session.expire_all()
        loc = db.session.get(Resource, rid).location
        check("location source is dawarich-auto", loc.source == "dawarich-auto")

        patch({"location": {"lat": 51.5, "lon": -0.12, "source": "manual"}})
        r3 = c.post(f"/api/resources/{rid}/location/refresh").get_json()
        db.session.expire_all()
        loc = db.session.get(Resource, rid).location
        check("manual location survives a refresh", r3["kept_manual_location"] and (loc.lat, loc.lon, loc.source) == (51.5, -0.12, "manual"))
        check("track still refreshed alongside", TrackPoint.query.filter_by(resource_id=rid).count() == n1)

        tr = c.get(f"/api/resources/{rid}/track").get_json()
        check("track times end in Z", all(p["recorded_at"].endswith("Z") for p in tr))
        check("offsets are relative to captured_at (starts ~-120 s)", -130 <= tr[0]["offset_seconds"] <= 0 and tr[-1]["offset_seconds"] > 400,
              f"{tr[0]['offset_seconds']} .. {tr[-1]['offset_seconds']}")

        # --- ingest: BWF timestamp with a recorder wall clock in summer time --------
        os.makedirs(Config.STAGING_DIR, exist_ok=True)
        wav = os.path.join(Config.STAGING_DIR, "ZZTEST_bwf.wav")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-write_bext", "1",
                        "-metadata", "origination_date=2026-09-17", "-metadata", "origination_time=09:11:24", wav], check=True)
        from jobs.ingest import _extract_timestamp
        got, source = _extract_timestamp(wav)
        check("BWF 09:11:24 wall clock -> 08:11:24 UTC (BST handled)", got == datetime(2026, 9, 17, 8, 11, 24) and source == "embedded", f"{got} {source}")
        os.remove(wav)
        print("\nALL WP1 CHECKS PASSED")
finally:
    with app.app_context():
        if rid:
            TrackPoint.query.filter_by(resource_id=rid).delete()
            Location.query.filter_by(resource_id=rid).delete()
            FileEvent.query.filter_by(resource_id=rid).delete()
            r = db.session.get(Resource, rid)
            if r:
                r.tags = []
                db.session.delete(r)
        Tag.query.filter(Tag.name.like("zztest-%")).delete(synchronize_session=False)
        db.session.commit()
        for f in ("ZZTEST_bwf.wav",):
            p = os.path.join(Config.STAGING_DIR, f)
            if os.path.exists(p):
                os.remove(p)
        print("cleanup: ZZTEST rows left =", Resource.query.filter(Resource.filename.like("ZZTEST%")).count(), "| real resources =", Resource.query.count())
