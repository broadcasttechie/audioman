"""
Split-file joining and multitrack grouping live check (real DB, NAS, worker, ffmpeg).

Uses a throwaway recorder profile ("ZZTEST splitter", split_seconds=10) so this never touches
the real Insta360 mic profile or its 1800s split_seconds. All ZZTEST rows, the throwaway
profile, and any files under staging/ are removed in `finally`.
"""
import glob
import os
import shutil
import subprocess
import time

from app import create_app
from app.extensions import db
from app.models import Resource, RecorderProfile, FileEvent
from config import Config
from jobs.ingest import ingest_staged_file
from jobs import grouping

app = create_app()
PROFILE_NAME = "ZZTEST splitter"
ids = []
profile_id = None


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  [{extra}]" if extra else ""))
    if not cond:
        raise SystemExit(1)


def make_wav(name, secs, rate=48000, channels=2, freq=440):
    os.makedirs(Config.STAGING_DIR, exist_ok=True)
    path = os.path.join(Config.STAGING_DIR, name)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={secs}",
                    "-ar", str(rate), "-ac", str(channels), path], check=True)
    return path


def poll_group_status(rid, statuses, secs=90):
    end = time.time() + secs
    r = None
    while time.time() < end:
        db.session.expire_all()
        r = db.session.get(Resource, rid)
        if r and r.group_status in statuses:
            return r
        time.sleep(2)
    return r


try:
    with app.app_context():
        c = app.test_client()

        profile = RecorderProfile(
            name=PROFILE_NAME, patterns=["zztestsplit_{YY}{MM}{DD}_{hh}{mm}{ss}"], timezone="UTC",
            clock_offset_seconds=0, date_trust="trusted", priority=1, active=True, split_seconds=10,
        )
        db.session.add(profile)
        db.session.commit()
        profile_id = profile.id

        # ================= split chain: 3 parts, the last one short (partial) =================
        # Distinct frequencies so each file has distinct content/checksum (ffmpeg's sine source is
        # deterministic from t=0, so same freq+duration+rate+channels would otherwise mean a
        # byte-identical file, and ingest treats a repeat checksum as a duplicate, not a new part).
        p1 = make_wav("zztestsplit_260101_100000.wav", 10, freq=440)
        p2 = make_wav("zztestsplit_260101_100012.wav", 10, freq=441)   # +2s gap after part 1's computed end, still "full"
        p3 = make_wav("zztestsplit_260101_100022.wav", 2, freq=442)    # +0s gap, short: ends the chain
        r1 = ingest_staged_file(p1); ids.append(r1.id)
        r2 = ingest_staged_file(p2); ids.append(r2.id)
        r3 = ingest_staged_file(p3); ids.append(r3.id)
        db.session.expire_all()
        r1, r2, r3 = (db.session.get(Resource, x.id) for x in (r1, r2, r3))
        check("part 1 got an exact trusted date from the throwaway profile",
             r1.captured_at_precision == "exact" and r1.filename_info.get("profile") == PROFILE_NAME)
        check("all three parts have a real duration from ffprobe", all(x.duration_seconds for x in (r1, r2, r3)))

        out = grouping.suggest_groupings()
        check("suggest-groupings ran cleanly", out["status"] == "success")
        db.session.expire_all()
        r1, r2, r3 = (db.session.get(Resource, x.id) for x in (r1, r2, r3))
        gid = r1.group_id
        check("all three parts were grouped together as a split chain",
             bool(gid) and r1.group_type == "split" and r2.group_id == gid and r3.group_id == gid,
             str((r1.group_id, r2.group_id, r3.group_id)))
        check("all three start out 'suggested'", r1.group_status == r2.group_status == r3.group_status == "suggested")
        check("the reason mentions 3 parts", "3 parts" in (r1.group_reason or ""), r1.group_reason)

        listed = c.get("/api/groups").get_json()
        found = next((g for g in listed if g["group_id"] == gid), None)
        check("the group appears in the default (needs-attention) listing",
             found is not None and {m["id"] for m in found["members"]} == {r1.id, r2.id, r3.id})

        # dismiss, then re-confirm: exercises the reversible path
        resp = c.post(f"/api/groups/{gid}/dismiss")
        check("dismiss succeeds", resp.status_code == 200 and resp.get_json()["status"] == "dismissed")
        db.session.expire_all()
        check("dismissing marks every member", db.session.get(Resource, r1.id).group_status == "dismissed")
        resp = c.post(f"/api/groups/{gid}/confirm")
        check("re-confirming a dismissed group is accepted (202, the join is queued)", resp.status_code == 202)

        joined = poll_group_status(r1.id, ("joined", "join-failed"))
        check("the join completed", joined is not None and joined.group_status == "joined", (joined.group_error if joined else "timed out") or "")
        db.session.expire_all()
        r1, r2, r3 = (db.session.get(Resource, x.id) for x in (r1, r2, r3))
        new_id = r1.joined_into_id
        check("all three parts point at the same new joined file",
             bool(new_id) and r2.joined_into_id == new_id and r3.joined_into_id == new_id)
        ids.append(new_id)
        new = db.session.get(Resource, new_id)
        check("the joined file is pending-review, role edit, derived from part 1",
             new.status == "pending-review" and new.role == "edit" and new.derived_from_id == r1.id)
        check("joined_from_ids records all three parts in order",
             new.joined_from_ids == [r1.id, r2.id, r3.id], str(new.joined_from_ids))
        check("duration is about 22s (10 + 10 + 2)", abs(new.duration_seconds - 22.0) < 1.0, new.duration_seconds)
        check("captured_at matches part 1's", new.captured_at == r1.captured_at)
        check("a FileEvent audit trail exists on both sides",
             FileEvent.query.filter_by(resource_id=new_id, event_type="joined").count() == 1
             and FileEvent.query.filter_by(resource_id=r1.id, event_type="joined").count() == 1)
        check("the source parts are untouched, not deleted",
             os.path.exists(r1.staging_path) and os.path.exists(r2.staging_path) and os.path.exists(r3.staging_path))
        a = c.get(f"/api/resources/{new_id}/audio")
        check("the joined audio is actually servable", a.status_code in (200, 206))

        listed = c.get("/api/groups").get_json()
        check("a joined group drops out of the default listing", all(g["group_id"] != gid for g in listed))
        listed_all = c.get("/api/groups?status=all").get_json()
        found = next((g for g in listed_all if g["group_id"] == gid), None)
        check("...but is still visible with status=all", found is not None and found["status"] == "joined")

        # ================= format mismatch is refused, not silently re-encoded =================
        m1 = make_wav("zztestsplit_260102_100000.wav", 10, rate=48000, channels=2, freq=443)
        m2 = make_wav("zztestsplit_260102_100012.wav", 10, rate=44100, channels=1, freq=444)
        rm1 = ingest_staged_file(m1); ids.append(rm1.id)
        rm2 = ingest_staged_file(m2); ids.append(rm2.id)
        grouping.suggest_groupings()
        db.session.expire_all()
        rm1 = db.session.get(Resource, rm1.id)
        gid2 = rm1.group_id
        check("a mismatched-format pair is still suggested (detection is metadata-only)", gid2 is not None)
        c.post(f"/api/groups/{gid2}/confirm")
        mismatched = poll_group_status(rm1.id, ("joined", "join-failed"))
        check("joining refuses when sample rate/channels differ, instead of silently re-encoding",
             mismatched is not None and mismatched.group_status == "join-failed" and mismatched.group_error,
             (mismatched.group_error if mismatched else "timed out") or "")

        # ================= multitrack: no timestamp at all, folder + batch fallback =================
        t1 = make_wav("ZZTEST_Tr1.wav", 6, freq=300)
        t2 = make_wav("ZZTEST_Tr2.wav", 6, freq=301)
        rt1 = ingest_staged_file(t1, drive_inbox_path="ZZTEST Gig/ZZTEST_Tr1.wav"); ids.append(rt1.id)
        rt2 = ingest_staged_file(t2, drive_inbox_path="ZZTEST Gig/ZZTEST_Tr2.wav"); ids.append(rt2.id)
        check("multitrack candidates have no date at all (nothing in the name)",
             rt1.captured_at is None and rt1.filename_info.get("folder") == "ZZTEST Gig")
        grouping.suggest_groupings()
        db.session.expire_all()
        rt1, rt2 = db.session.get(Resource, rt1.id), db.session.get(Resource, rt2.id)
        check("both tracks grouped as multitrack via the folder fallback",
             bool(rt1.group_id) and rt1.group_id == rt2.group_id and rt1.group_type == "multitrack")
        check("track labels were assigned from the varying part of the name",
             {rt1.track_label, rt2.track_label} == {"1", "2"}, str((rt1.track_label, rt2.track_label)))
        resp = c.post(f"/api/groups/{rt1.group_id}/confirm")
        check("confirming a multitrack group is immediate (200), no file work", resp.status_code == 200)
        db.session.expire_all()
        rt1 = db.session.get(Resource, rt1.id)
        check("multitrack confirm never touches status/staging_path",
             rt1.group_status == "confirmed" and rt1.status == "pending-review" and rt1.staging_path == t1)

        print("\nALL GROUPING CHECKS PASSED")
finally:
    with app.app_context():
        # Break the derived_from_id <-> joined_into_id cycle between a joined file and its parts
        # before deleting anything, or Postgres refuses whichever row is deleted first.
        for rid in ids:
            r = db.session.get(Resource, rid)
            if r:
                r.joined_into_id = None
                r.derived_from_id = None
        db.session.commit()
        for rid in ids:
            r = db.session.get(Resource, rid)
            if r is None:
                continue
            for p in (r.staging_path, r.nas_path):
                if p and os.path.basename(p).startswith(("zztestsplit", "ZZTEST")) and os.path.exists(p):
                    os.remove(p)
            FileEvent.query.filter_by(resource_id=rid).delete()
            r.tags = []
            db.session.delete(r)
        db.session.commit()
        if profile_id:
            p = db.session.get(RecorderProfile, profile_id)
            if p:
                db.session.delete(p)
                db.session.commit()
        for f in glob.glob(os.path.join(Config.STAGING_DIR, "zztestsplit_*")) + glob.glob(os.path.join(Config.STAGING_DIR, "ZZTEST_*")):
            os.remove(f)
        for d in glob.glob(os.path.join(Config.STAGING_DIR, "joins", "*")):
            shutil.rmtree(d, ignore_errors=True)
        print("cleanup: leftover ZZTEST/zztestsplit resources =",
             Resource.query.filter(db.or_(Resource.filename.like("ZZTEST%"), Resource.filename.like("zztestsplit%"))).count(),
             "| real resources =", Resource.query.count())
