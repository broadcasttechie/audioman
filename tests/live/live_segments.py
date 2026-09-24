"""
Segments live check (PLAN 18.4): a clip carrying tags/transcript/speaker, segment search across
the library, and the tag pool being genuinely shared between files and clips (merge/delete now
account for clip usage too). Real DB and worker. ZZTEST rows removed in `finally`.
"""
import os
import subprocess

from app import create_app
from app.extensions import db
from app.models import Resource, Clip, Tag, FileEvent
from config import Config
from jobs.ingest import ingest_staged_file

app = create_app()
ids, tag_ids = [], []


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  [{extra}]" if extra else ""))
    if not cond:
        raise SystemExit(1)


def make_wav(name, secs=3, freq=440):
    os.makedirs(Config.STAGING_DIR, exist_ok=True)
    path = os.path.join(Config.STAGING_DIR, name)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={secs}", "-ac", "1", path], check=True)
    return path


try:
    with app.app_context():
        c = app.test_client()

        r = ingest_staged_file(make_wav("ZZTEST_segments.wav"))
        ids.append(r.id)

        tag_jacob = c.post("/api/tags", json={"name": "ZZTEST-Jacob"}).get_json()
        tag_ids.append(tag_jacob["id"])
        tag_birds = c.post("/api/tags", json={"name": "ZZTEST-birds"}).get_json()
        tag_ids.append(tag_birds["id"])

        # ================= create a segment with the full metadata block =================
        made = c.post(f"/api/resources/{r.id}/clips", json={
            "start_seconds": 0.5, "end_seconds": 2.0, "label": "Jacob spots a heron",
            "tags": [tag_jacob["id"], tag_birds["id"]], "transcript": "look, a heron by the water",
            "speaker": "Jacob",
        })
        check("creating a segment with tags/transcript/speaker is accepted", made.status_code == 201, str(made.get_json())[:200])
        clip = made.get_json()
        clip_id = clip["id"]
        check("tags come back by name, in the shared pool", set(clip["tags"]) == {"ZZTEST-Jacob", "ZZTEST-birds"}, str(clip["tags"]))
        check("transcript and speaker come back too", clip["transcript"] == "look, a heron by the water" and clip["speaker"] == "Jacob")

        check("an unknown tag id on create is refused", c.post(f"/api/resources/{r.id}/clips", json={
            "start_seconds": 0, "end_seconds": 1, "tags": ["not-a-real-id"],
        }).status_code == 400)

        # ================= update: swap a tag, change transcript/speaker =================
        tag_water = c.post("/api/tags", json={"name": "ZZTEST-water"}).get_json()
        tag_ids.append(tag_water["id"])
        upd = c.patch(f"/api/clips/{clip_id}", json={"tags": [tag_jacob["id"], tag_water["id"]], "speaker": "Jacob R."})
        check("updating tags/speaker is accepted", upd.status_code == 200, str(upd.get_json())[:200])
        check("the tag set actually changed (birds dropped, water added)", set(upd.get_json()["tags"]) == {"ZZTEST-Jacob", "ZZTEST-water"}, str(upd.get_json()["tags"]))
        check("speaker updated", upd.get_json()["speaker"] == "Jacob R.")
        check("an unknown tag id on update is refused, and changes nothing", c.patch(f"/api/clips/{clip_id}", json={"tags": ["nope"]}).status_code == 400)
        db.session.expire_all()
        check("...confirmed: the tag set from the successful update is still there", set(t.name for t in db.session.get(Clip, clip_id).tags) == {"ZZTEST-Jacob", "ZZTEST-water"})

        # ================= segment search: "the unit of search is a segment" =================
        def search(q):
            return c.get("/api/segments/search?q=" + q).get_json()

        by_transcript = search("heron")
        check("finds it by transcript text", any(s["id"] == clip_id for s in by_transcript), str(by_transcript)[:200])
        by_speaker = search("Jacob+R.")
        check("finds it by speaker", any(s["id"] == clip_id for s in by_speaker))
        by_tag = search("ZZTEST-water")
        check("finds it by tag name", any(s["id"] == clip_id for s in by_tag))
        by_label = search("spots+a+heron")
        check("finds it by label", any(s["id"] == clip_id for s in by_label))
        none_found = search("ZZTEST-completely-unrelated-xyz")
        check("an unrelated query finds nothing", none_found == [])
        hit = next(s for s in by_transcript if s["id"] == clip_id)
        check("a hit carries its resource's context, enough to link straight to the moment",
             hit["resource"]["id"] == r.id and hit["resource"]["filename"] == r.filename, str(hit.get("resource")))
        check("a literal '%' in the query is not treated as a SQL wildcard (would otherwise match everything)", search("%25") == [])

        # ================= the editor page accepts a deep link to this segment =================
        check("the editor page loads with ?clip= for 'land on that moment'",
             c.get(f"/edit/{r.id}?clip={clip_id}").status_code == 200)

        # ================= the shared tag pool: list/merge/delete account for clip usage too ====
        listed = {t["id"]: t for t in c.get("/api/tags").get_json()}
        check("list_tags reports clip_count for a tag used only by a clip",
             listed[tag_jacob["id"]]["clip_count"] == 1 and listed[tag_jacob["id"]]["file_count"] == 0, str(listed[tag_jacob["id"]]))

        refused = c.delete(f"/api/tags/{tag_water['id']}")
        check("deleting a tag used only by a clip is refused without force=1 (not just resource usage)",
             refused.status_code == 409 and refused.get_json().get("clip_count") == 1, str(refused.get_json()))

        merged = c.post(f"/api/tags/{tag_water['id']}/merge", json={"into": tag_birds["id"]})
        check("merging a tag moves its clip uses to the target too", merged.status_code == 200 and merged.get_json().get("clips_merged") == 1, str(merged.get_json()))
        db.session.expire_all()
        check("the clip now carries the merge target's tag instead of the deleted one",
             set(t.name for t in db.session.get(Clip, clip_id).tags) == {"ZZTEST-Jacob", "ZZTEST-birds"})
        check("the merged-away tag is actually gone", db.session.get(Tag, tag_water["id"]) is None)
        tag_ids.remove(tag_water["id"])   # already gone, don't try to delete it again in cleanup

        forced = c.delete(f"/api/tags/{tag_jacob['id']}?force=1")
        check("force-deleting a tag used by a clip succeeds", forced.status_code == 204)
        db.session.expire_all()
        check("the clip lost that tag but is otherwise untouched",
             set(t.name for t in db.session.get(Clip, clip_id).tags) == {"ZZTEST-birds"} and db.session.get(Clip, clip_id) is not None)
        tag_ids.remove(tag_jacob["id"])

        # ================= deleting a tagged clip works cleanly (M2M cleanup, no FK error) =======
        deleted = c.delete(f"/api/clips/{clip_id}")
        check("deleting a clip that still has a tag succeeds", deleted.status_code == 204)
        db.session.expire_all()
        check("the clip is really gone", db.session.get(Clip, clip_id) is None)
        listed_after = {t["id"]: t for t in c.get("/api/tags").get_json()}
        check("the surviving tag's clip_count dropped back to 0 (the join row was cleaned up)",
             listed_after[tag_birds["id"]]["clip_count"] == 0, str(listed_after[tag_birds["id"]]))

        print("\nALL SEGMENT CHECKS PASSED")
finally:
    with app.app_context():
        # The clip is already deleted by the happy path above; this also covers a clip left
        # behind by an early failure, without depending on clip_id having been assigned.
        for rid in ids:
            r = db.session.get(Resource, rid)
            if r:
                for clip in list(r.clips):
                    db.session.delete(clip)
                for p in (r.staging_path, r.nas_path):
                    if p and os.path.exists(p) and os.path.basename(p).startswith("ZZTEST"):
                        os.remove(p)
                FileEvent.query.filter_by(resource_id=rid).delete()
                r.tags = []
                db.session.delete(r)
        db.session.commit()
        for tid in tag_ids:
            t = db.session.get(Tag, tid)
            if t:
                db.session.delete(t)
        db.session.commit()
        print("cleanup done; real resources =", Resource.query.count())
