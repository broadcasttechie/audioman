"""Place names live check (real Photon, DB, worker, settings). ZZTEST rows only; settings are restored."""
import os
import subprocess
import time

from app import create_app
from app.extensions import db
from app.models import Resource, Location, FileEvent, Setting, JobRun
from config import Config
from jobs import geocode
from jobs.ingest import ingest_staged_file

app = create_app()
REAL = "66482729-aa93-4704-b3b9-13cdad3a2e7e"
ids = []
touched = {}


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


def setting(key, value):
    """Set a setting for the test, remembering how to put it back."""
    if key not in touched:
        row = db.session.get(Setting, key)
        touched[key] = None if row is None else row.value
    row = db.session.get(Setting, key) or Setting(key=key)
    row.value = value
    db.session.merge(row)
    db.session.commit()


def wait_named(loc_id, secs=90):
    end = time.time() + secs
    while time.time() < end:
        db.session.expire_all()
        loc = db.session.get(Location, loc_id)
        if loc.place_checked_at is not None:
            return loc
        time.sleep(2)
    return db.session.get(Location, loc_id)


try:
    with app.app_context():
        c = app.test_client()
        check("the Settings page has the Photon section and the test endpoint exists", b'id="photon_url"' in c.get("/settings").data)
        snap = c.get("/api/settings").get_json()
        check("settings expose the Photon URL and the active providers", snap["PHOTON_API_URL"]["value"].startswith("http") and snap["providers"]["geocoder"]["active"] == "photon" and snap["providers"]["location"]["active"] == "dawarich")
        t = c.post("/api/settings/place-names/test").get_json()
        check("the real Photon answers the settings test", t["ok"] and "London" in (t["example"] or ""), str(t))

        # ---- the real recording ---------------------------------------------------------------------
        r = c.post(f"/api/resources/{REAL}/place/lookup")
        check("looking up the real recording names it", r.status_code == 200 and r.get_json()["found"], str(r.get_json()))
        name = r.get_json()["place_name"]
        check("the name is Stoke station area, from Photon", "Stoke-on-Trent" in name and r.get_json()["place_source"] == "photon", name)
        d = c.get(f"/api/resources/{REAL}").get_json()
        check("the resource carries the place name", d["location"]["place_name"] == name and d["location"]["place_source"] == "photon")
        check("the Library finds it by place", REAL in {x["id"] for x in c.get("/api/resources?status=&q=Shelton").get_json()["resources"]})
        check("the map pin carries it", next(p for p in c.get("/api/map/pins").get_json()["pins"] if p["id"] == REAL)["place_name"] == name)

        # ---- the queue: a new location is named by the worker ----------------------------------------------
        a, b, far = stage("ZZTEST_pl_a.wav", 1001), stage("ZZTEST_pl_b.wav", 1002), stage("ZZTEST_pl_c.wav", 1003)
        la = Location(resource_id=a.id, lat=53.0082, lon=-2.1812, source="dawarich-auto")           # Stoke station
        lb = Location(resource_id=b.id, lat=52.3855, lon=-2.4030, source="dawarich-auto")           # Wyre Forest: nothing named within ~1 km
        lc = Location(resource_id=far.id, lat=30.0, lon=-40.0, source="manual")                      # mid-Atlantic: nothing at all
        db.session.add_all([la, lb, lc]); db.session.commit()
        la_id, lb_id, lc_id = la.id, lb.id, lc.id
        c.post("/api/jobs/geocode-locations/run")
        la, lb, lc = wait_named(la_id), wait_named(lb_id), wait_named(lc_id)
        check("a queued location is named by the worker", la.place_name and "Stoke-on-Trent" in la.place_name and la.place_source == "photon", la.place_name)
        check("a woodland point is named by area, never by a postcode", lb.place_name and "DY14" not in lb.place_name, lb.place_name)
        check("nothing found is remembered, not asked again", lc.place_checked_at is not None and lc.place_name is None)

        # ---- typed names, moving, clearing -------------------------------------------------------------------
        out = c.patch(f"/api/resources/{a.id}", json={"place_name": "  Jacob's garden  "}).get_json()
        check("a typed name is stored and marked typed", out["location"]["place_name"] == "Jacob's garden" and out["location"]["place_source"] == "manual")
        c.post("/api/jobs/geocode-locations/run"); time.sleep(6)
        db.session.expire_all()
        check("the sweeper never overwrites a typed name", db.session.get(Location, la_id).place_name == "Jacob's garden")
        out = c.patch(f"/api/resources/{a.id}", json={"location": {"lat": 53.00822, "lon": -2.18121, "source": "manual"}}).get_json()
        check("a tiny move (GPS jitter) keeps the name", out["location"]["place_name"] == "Jacob's garden")
        out = c.patch(f"/api/resources/{a.id}", json={"location": {"lat": 52.3855, "lon": -2.4030, "source": "manual"}}).get_json()
        check("a real move drops the old name and queues a new lookup", out["location"]["place_name"] is None)
        moved = wait_named(la_id)
        check("...and the worker names the new place", moved.place_name and "Stoke" not in moved.place_name and moved.place_source == "photon", moved.place_name)
        out = c.patch(f"/api/resources/{a.id}", json={"place_name": ""}).get_json()
        check("clearing a name queues a fresh lookup", out["location"]["place_name"] is None)
        check("...which brings it back", wait_named(la_id).place_name is not None)
        check("a name needs a location", c.patch(f"/api/resources/{stage('ZZTEST_pl_d.wav', 1004).id}", json={"place_name": "x"}).status_code == 400)
        check("place_name must be text", c.patch(f"/api/resources/{a.id}", json={"place_name": 5}).status_code == 400)

        # ---- Photon down: nothing fails, nothing is lost --------------------------------------------------------
        setting("PHOTON_API_URL", "http://127.0.0.1:9")
        resp = c.post(f"/api/resources/{b.id}/place/lookup")
        check("with Photon unreachable the lookup says so (503)", resp.status_code == 503 and "not available" in resp.get_json()["error"], resp.get_json()["error"][:70])
        t = c.post("/api/settings/place-names/test").get_json()
        check("...and so does the settings test", t["ok"] is False and t["error"])
        db.session.execute(db.text("UPDATE locations SET place_checked_at = NULL, place_name = NULL WHERE id = :i"), {"i": lb_id}); db.session.commit()
        out = geocode.geocode_locations()
        db.session.expire_all()
        check("the sweeper reports Photon as down (even for one location) and leaves it queued", out["status"] == "error" and db.session.get(Location, lb_id).place_checked_at is None, str(out.get("detail"))[:60])
        setting("PHOTON_API_URL", "http://photon.home.zamia.co.uk:2322")
        out = geocode.geocode_locations()
        check("once Photon is back the queue drains by itself", out["status"] == "success" and db.session.get(Location, lb_id).place_checked_at is not None)

        # ---- swapping providers by config ---------------------------------------------------------------------------
        setting("GEOCODER_PROVIDER", "none")
        check("provider 'none' switches place names off", c.post(f"/api/resources/{b.id}/place/lookup").status_code == 400 and geocode.geocode_locations()["detail"] == "provider disabled")
        setting("GEOCODER_PROVIDER", "nominatim")
        resp = c.post(f"/api/resources/{b.id}/place/lookup")
        check("an unknown provider is named in the error, with the choices", resp.status_code == 400 and "nominatim" in resp.get_json()["error"] and "photon" in resp.get_json()["error"])
        setting("LOCATION_PROVIDER", "none")
        check("the location provider can be switched off too", c.post(f"/api/resources/{REAL}/location/refresh").status_code == 400)
        print("\nALL PLACE CHECKS PASSED")
finally:
    with app.app_context():
        for key, old in touched.items():
            row = db.session.get(Setting, key)
            if old is None and row is not None:
                db.session.delete(row)
            elif row is not None:
                row.value = old
        db.session.commit()
        for rid in ids:
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
        run = db.session.get(JobRun, "geocode-locations")
        if run and run.status == "error":
            db.session.delete(run)     # the error was provoked on purpose above; don't leave it on Home
            db.session.commit()
        print("cleanup: real resources =", Resource.query.count(), "| settings restored:", sorted(touched))
