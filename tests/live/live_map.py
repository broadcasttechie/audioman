"""Map API live check: config, pins with the Library's filters, unlocated counts, and the track data the map draws.
ZZTEST rows only, removed after; the real resource (which has a location and a track) is only read."""
import os
import subprocess

from app import create_app
from app.extensions import db
from app.models import Resource, Location, TrackPoint, FileEvent, Tag, Project
from config import Config
from jobs.ingest import ingest_staged_file

app = create_app()
REAL = "66482729-aa93-4704-b3b9-13cdad3a2e7e"
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


try:
    with app.app_context():
        c = app.test_client()
        for path in ("/map", "/static/map.js"):
            check(f"{path} serves", c.get(path).status_code == 200)
        check("the nav has the Map tab", b'href="/map"' in c.get("/").data)
        cfg = c.get("/api/map/config").get_json()
        check("map config gives a tile template with z/x/y and an attribution", all(k in cfg["tile_url"] for k in ("{z}", "{x}", "{y}")) and "OpenStreetMap" in cfg["attribution"] and cfg["max_zoom"] >= 15)

        # ---- pins ---------------------------------------------------------------------
        base = c.get("/api/map/pins").get_json()
        real_pin = next((p for p in base["pins"] if p["id"] == REAL), None)
        check("the real recording is on the map at Stoke station", real_pin and abs(real_pin["lat"] - 53.008) < 0.002 and abs(real_pin["lon"] + 2.181) < 0.002, str(real_pin and (round(real_pin["lat"], 4), round(real_pin["lon"], 4))))
        check("pin carries what the popup shows", all(k in real_pin for k in ("filename", "category", "captured_at", "duration_seconds", "source")) and real_pin["captured_at"].endswith("Z"))

        a, b, d = stage("ZZTEST_map_a.wav", 901), stage("ZZTEST_map_b.wav", 902), stage("ZZTEST_map_c.wav", 903)
        db.session.add(Location(resource_id=a.id, lat=52.4, lon=-2.3, source="manual"))
        db.session.add(Location(resource_id=b.id, lat=52.41, lon=-2.31, source="dawarich-auto"))
        db.session.commit()
        c.patch(f"/api/resources/{a.id}", json={"category": "event", "notes": "zzmap-marker"})
        c.patch(f"/api/resources/{b.id}", json={"category": "ambient"})
        after = c.get("/api/map/pins").get_json()
        got = {p["id"] for p in after["pins"]}
        check("located recordings appear, unlocated ones don't", {a.id, b.id} <= got and d.id not in got)
        check("the unlocated count includes the one without a location", after["unlocated"] == base["unlocated"] + 1 and after["total"] == base["total"] + 2, f"{base['unlocated']}->{after['unlocated']}")
        only = {p["id"] for p in c.get("/api/map/pins?category=event").get_json()["pins"]}
        check("the Library's category filter applies", a.id in only and b.id not in only and REAL not in only)
        check("the Library's text search applies (notes)", {p["id"] for p in c.get("/api/map/pins?q=zzmap-marker").get_json()["pins"]} == {a.id})
        filed_only = {p["id"] for p in c.get("/api/map/pins?status=filed").get_json()["pins"]}
        check("status=filed leaves out recordings still to review", REAL in filed_only and a.id not in filed_only)
        check("no match gives an empty list, not an error", c.get("/api/map/pins?q=zzqqnothing").get_json()["pins"] == [])
        check("a recording with a null location isn't a pin", not any(p["lat"] is None for p in after["pins"]))

        # ---- the data behind the per-recording map ----------------------------------------------
        r = c.get(f"/api/resources/{REAL}").get_json()
        tr = c.get(f"/api/resources/{REAL}/track").get_json()
        check("the real recording reports a track and a location", r["has_track"] and r["location"] and len(tr) >= 5, f"{len(tr)} points")
        check("track points have what the map needs, in time order", all({"lat", "lon", "offset_seconds"} <= set(p) for p in tr) and [p["offset_seconds"] for p in tr] == sorted(p["offset_seconds"] for p in tr))
        import math
        def km(p, q):
            p1, p2 = math.radians(p[0]), math.radians(q[0])
            h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(q[1] - p[1]) / 2) ** 2
            return 2 * 6371 * math.asin(math.sqrt(h))
        pin = (r["location"]["lat"], r["location"]["lon"])
        far = max(km(pin, (p["lat"], p["lon"])) for p in tr)
        check("the real track starts at the pin and travels several km (a train journey from Stoke station)",
              km(pin, (tr[0]["lat"], tr[0]["lon"])) < 0.15 and 3 < far < 50, f"furthest point {far:.1f} km from the start")
        check("track times fall inside (or just around) the recording", tr[0]["offset_seconds"] >= -130 and tr[-1]["offset_seconds"] <= r["duration_seconds"] + 130)

        # the manual picker on the map sets a manual location through this same call
        out = c.patch(f"/api/resources/{d.id}", json={"location": {"lat": 51.5, "lon": -0.12, "source": "manual"}}).get_json()
        check("choosing a place on the map stores a manual location", out["location"]["source"] == "manual" and abs(out["location"]["lat"] - 51.5) < 1e-9)
        check("...and the recording then appears on the map", d.id in {p["id"] for p in c.get("/api/map/pins").get_json()["pins"]})
        print("\nALL MAP CHECKS PASSED")
finally:
    with app.app_context():
        for rid in ids:
            TrackPoint.query.filter_by(resource_id=rid).delete()
            Location.query.filter_by(resource_id=rid).delete()
            FileEvent.query.filter_by(resource_id=rid).delete()
            r = db.session.get(Resource, rid)
            if r:
                r.tags = []
                if r.staging_path and os.path.exists(r.staging_path):
                    os.remove(r.staging_path)
                db.session.delete(r)
        db.session.commit()
        for f in os.listdir(Config.STAGING_DIR):
            if f.startswith("ZZTEST"):
                os.remove(os.path.join(Config.STAGING_DIR, f))
        print("cleanup: real resources =", Resource.query.count())
