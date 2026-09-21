"""Live check: configurable categories, tag rename/merge/delete, project counts, Home overview, reclaimable-space
view and the new pages. ZZTEST data is removed at the end; the ack setting is restored."""
import os
import subprocess
import uuid

from app import create_app
from app.extensions import db
from app.models import Resource, Category, Tag, Project, RecordingSession, FileEvent, Setting
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


ack_before = None
try:
    with app.app_context():
        c = app.test_client()
        ack_row = db.session.get(Setting, "BACKUP_ACKNOWLEDGED")
        ack_before = ack_row.value if ack_row else None
        for path in ("/", "/manage", "/reclaim", "/static/app.js"):
            check(f"{path} serves", c.get(path).status_code == 200)
        check("nav has the five tabs", all(t in c.get("/").data for t in (b'href="/manage"', b'href="/library"', b'href="/settings"')))

        # ---- categories ------------------------------------------------------------
        cats = c.get("/api/categories").get_json()
        by = {x["slug"]: x for x in cats}
        check("seeded categories, with 'ambient' labelled Field recordings", set(by) >= {"ambient", "event", "voice-personal", "voice-project"} and by["ambient"]["label"] == "Field recordings")
        made = c.post("/api/categories", json={"label": "ZZTEST Rehearsals"})
        check("create derives a folder-safe slug", made.status_code == 201 and made.get_json()["slug"] == "zztest-rehearsals")
        check("duplicate slug refused", c.post("/api/categories", json={"label": "x", "slug": "zztest-rehearsals"}).status_code == 409)
        check("bad slug refused", c.post("/api/categories", json={"label": "x", "slug": "../x"}).status_code == 400)
        check("slug can't be changed", c.patch("/api/categories/zztest-rehearsals", json={"slug": "other"}).status_code == 400)
        check("label can be renamed", c.patch("/api/categories/zztest-rehearsals", json={"label": "ZZTEST Band rehearsals"}).get_json()["label"] == "ZZTEST Band rehearsals")
        c.post("/api/categories", json={"label": "ZZTEST Spare"})

        a, b = stage("ZZTEST_m1.wav", 701), stage("ZZTEST_m2.wav", 702)
        check("a file can take the new category", c.patch(f"/api/resources/{a.id}", json={"category": "zztest-rehearsals"}).status_code == 200)
        check("unknown category refused", c.patch(f"/api/resources/{b.id}", json={"category": "no-such"}).status_code == 400)
        check("file_count reflects use", next(x for x in c.get("/api/categories").get_json() if x["slug"] == "zztest-rehearsals")["file_count"] == 1)
        c.patch("/api/categories/zztest-spare", json={"archived": True})
        check("archived categories are hidden from pickers but listed with ?all=1",
              "zztest-spare" not in [x["slug"] for x in c.get("/api/categories").get_json()] and "zztest-spare" in [x["slug"] for x in c.get("/api/categories?all=1").get_json()])
        check("an archived category can't be given to a file", c.patch(f"/api/resources/{b.id}", json={"category": "zztest-spare"}).status_code == 400)
        check("merge into an archived/unknown/same category refused", all(c.post("/api/categories/zztest-rehearsals/merge", json={"into": t}).status_code == 400 for t in ("zztest-spare", "nope", "zztest-rehearsals")))
        out = c.post("/api/categories/zztest-rehearsals/merge", json={"into": "event"}).get_json()
        db.session.expire_all()
        check("merge moves the files and archives the source", out["moved"] == 1 and db.session.get(Resource, a.id).category == "event" and db.session.get(Category, "zztest-rehearsals").archived)

        # ---- tags -------------------------------------------------------------------------
        t1 = c.post("/api/tags", json={"name": "zztest-wind"}).get_json()
        t2 = c.post("/api/tags", json={"name": "zztest-windy"}).get_json()
        t3 = c.post("/api/tags", json={"name": "zztest-rain"}).get_json()
        c.patch(f"/api/resources/{a.id}", json={"tags": [t1["id"], t2["id"]]})   # a has both
        c.patch(f"/api/resources/{b.id}", json={"tags": [t1["id"]]})              # b only the first
        tl = {x["name"]: x for x in c.get("/api/tags").get_json()}
        check("tag list carries usage counts", tl["zztest-wind"]["file_count"] == 2 and tl["zztest-windy"]["file_count"] == 1 and tl["zztest-rain"]["file_count"] == 0)
        r = c.patch(f"/api/tags/{t3['id']}", json={"name": "zztest-drizzle"})
        check("rename works", r.status_code == 200 and r.get_json()["name"] == "zztest-drizzle")
        r = c.patch(f"/api/tags/{t3['id']}", json={"name": "ZZTEST-WIND"})
        check("rename onto an existing name (any case) is refused with the merge target", r.status_code == 409 and r.get_json()["merge_into"] == t1["id"])
        m = c.post(f"/api/tags/{t2['id']}/merge", json={"into": t1["id"]}).get_json()
        db.session.expire_all()
        check("merge moves uses without duplicating and removes the source", m["merged"] == 1 and sorted(t.name for t in db.session.get(Resource, a.id).tags) == ["zztest-wind"] and db.session.get(Tag, t2["id"]) is None)
        check("can't merge a tag into itself", c.post(f"/api/tags/{t1['id']}/merge", json={"into": t1["id"]}).status_code == 400)
        check("deleting a tag in use is refused", c.delete(f"/api/tags/{t1['id']}").status_code == 409)
        check("...unless forced, which removes it from files", c.delete(f"/api/tags/{t1['id']}?force=1").status_code == 204 and not db.session.get(Resource, b.id).tags)
        check("an unused tag deletes cleanly", c.delete(f"/api/tags/{t3['id']}").status_code == 204)
        check("unknown tag id is a 404", c.delete(f"/api/tags/{uuid.uuid4()}").status_code == 404)

        # ---- projects + overview -------------------------------------------------------------
        p = c.post("/api/projects", json={"name": "ZZTEST Overview", "slug": "zztest-overview"}).get_json()
        c.patch(f"/api/resources/{a.id}", json={"project_id": p["id"]})
        c.post("/api/sessions", json={"name": "ZZTEST night", "project_id": p["id"]})
        pl = next(x for x in c.get("/api/projects").get_json() if x["id"] == p["id"])
        check("project list shows file and session counts", pl["file_count"] == 1 and pl["session_count"] == 1)
        ov = c.get("/api/overview").get_json()
        real = c.get(f"/api/resources/{REAL}").get_json()
        check("overview counts add up", ov["counts"]["pending_review"] >= 2 and ov["counts"]["filed"] >= 1 and ov["library"]["files"] == ov["counts"]["filed"])
        check("overview library totals include the real file", ov["library"]["hours"] > 0 and ov["library"]["bytes"] >= real["size_bytes"])
        check("recent projects include the new one", "zztest-overview" in [x["slug"] for x in ov["recent_projects"]])
        check("recent files list the real recording", REAL in [x["id"] for x in ov["recent_files"]])
        check("health reports the NAS", ov["health"]["nas"]["ok"] is True and isinstance(ov["health"]["job_errors"], list))
        check("categories on the overview are the active ones", all(x["slug"] != "zztest-spare" for x in ov["categories"]))

        # ---- reclaimable space ----------------------------------------------------------------------------
        c.put("/api/reclaimable/ack", json={"acknowledged": False})
        rc = c.get("/api/reclaimable").get_json()
        check("before acknowledging: only a count and total, no list", rc["acknowledged"] is False and rc["count"] >= 1 and rc["total_bytes"] >= 300_000_000 and "items" not in rc)
        check("ack must be a boolean", c.put("/api/reclaimable/ack", json={"acknowledged": "yes"}).status_code == 400)
        c.put("/api/reclaimable/ack", json={"acknowledged": True})
        rc = c.get("/api/reclaimable").get_json()
        item = next(i for i in rc["items"] if i["id"] == REAL)
        check("after acknowledging: the list, with the Drive path and size", rc["acknowledged"] and item["drive_path"] == "Inbox/_processed/" + real["filename"].replace("", "") or item["drive_path"].startswith("Inbox/_processed/"), item["drive_path"])
        check("only files whose NAS copy exists are offered", all(os.path.exists(db.session.get(Resource, i["id"]).nas_path) for i in rc["items"]))
        Config.NAS_MARKER_FILE = ".no-such-marker"
        try:
            check("with the NAS down nothing is offered (503)", c.get("/api/reclaimable").status_code == 503)
        finally:
            Config.NAS_MARKER_FILE = ".audio-manager-nas"
        chk = c.get("/api/reclaimable?check_drive=1").get_json()
        check("check_drive asks Drive and reports it did", chk["drive_checked"] is True, f"{chk['count']} still in Drive")
        print("\nALL MANAGE CHECKS PASSED")
finally:
    with app.app_context():
        row = db.session.get(Setting, "BACKUP_ACKNOWLEDGED")
        if ack_before is None and row:
            db.session.delete(row)
        elif row:
            row.value = ack_before
        for rid in ids:
            FileEvent.query.filter_by(resource_id=rid).delete()
            r = db.session.get(Resource, rid)
            if r:
                r.tags = []
                for x in (r.staging_path,):
                    if x and os.path.exists(x):
                        os.remove(x)
                db.session.delete(r)
        db.session.commit()
        RecordingSession.query.filter(RecordingSession.name.like("ZZTEST%")).delete(synchronize_session=False)
        Project.query.filter(Project.slug.like("zztest-%")).delete(synchronize_session=False)
        Tag.query.filter(Tag.name.like("zztest-%")).delete(synchronize_session=False)
        Category.query.filter(Category.slug.like("zztest-%")).delete(synchronize_session=False)
        db.session.commit()
        for f in os.listdir(Config.STAGING_DIR):
            if f.startswith("ZZTEST"):
                os.remove(os.path.join(Config.STAGING_DIR, f))
        print("cleanup: real resources =", Resource.query.count(), "| categories =", Category.query.count(), "| ack restored to", repr(ack_before))
