"""
Split-file joining and multitrack grouping (PLAN 22, formerly 17.7 / 18.5f / 18.5g).

Two different things that both need "these files belong together", never applied
automatically:

  split       A recorder that starts a new file every N seconds (the Insta360 mic,
              every 30 minutes) -> offer to join the chain into one real file, a
              non-destructive ffmpeg concat. The parts are kept, audit-logged via
              FileEvent, and linked to the result (Resource.joined_into_id).
  multitrack  Several files recorded at once (one per input) -> offer to mark them
              as tracks of one take (Resource.group_id/group_type + track_label).
              Nothing is merged; this is "these are one unit", not a DAW.

Detection is metadata-only and lives in pure functions (find_split_chains,
find_multitrack_by_time, find_multitrack_by_batch) so it can be tested against
tests/fixtures/sample_filenames.txt without a database, the same shape as
jobs/filename_patterns.py and jobs/disk_budget.py. A GroupCandidate that's already
carrying a group_id is never reconsidered, so a dismissed suggestion stays dismissed.

Two sweeper jobs, same "one queued job drains everything due" shape as the rest of
this app:
  suggest-groupings  finds new candidates and stamps group_id/group_type/group_status
                     = "suggested" + a human-readable group_reason.
  join-groups        does the actual ffmpeg concat for every split group the user
                     has confirmed (group_status = "joining"), and creates the new
                     joined Resource. Multitrack has no file work: confirming one
                     just flips group_status to "confirmed" (see app/api.py).
"""
import json
import logging
import os
import shutil
import subprocess
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from config import Config
from app.extensions import db
from app.models import Resource, RecorderProfile, RecordingSession, JobRun, FileEvent
from . import disk_budget
from .filename_patterns import strip_name
from .nas import nas_status, is_nas_path, sha256_file, NasUnavailable
from .previews import probe_audio, GenerationError

log = logging.getLogger(__name__)

CANDIDATE_STATUSES = ("pending-review", "filed")


class GroupingError(Exception):
    """The join failed for a reason a human needs to look at; the group is marked join-failed."""


class GroupWaiting(Exception):
    """Not a failure: there isn't room to do this right now. Left as 'joining', retried next sweep."""


# ---------------------------------------------------------------- pure detection

@dataclass
class GroupCandidate:
    id: str
    filename: str
    captured_at: "datetime | None"
    captured_at_precision: str
    duration_seconds: "float | None"
    created_at: datetime
    profile: "str | None"          # filename_info['profile']
    split_seconds: "int | None"    # that profile's RecorderProfile.split_seconds
    folder: "str | None"           # filename_info['folder']


def _stripped(filename):
    return strip_name(filename)


def find_split_chains(candidates, gap_min=Config.SPLIT_GAP_MIN_SECONDS, gap_max=Config.SPLIT_GAP_MAX_SECONDS,
                      duration_tolerance_fraction=Config.SPLIT_DURATION_TOLERANCE_FRACTION):
    """
    candidates: GroupCandidates with captured_at and split_seconds both set (filter before calling).
    A chain is 2+ files, same profile, in time order, where every part but the last has a duration
    within tolerance of its profile's split_seconds, and each next part starts gap_min..gap_max
    seconds after the previous part's computed end. Returns a list of
    {"ids": [...], "type": "split", "reason": "..."}, oldest chain member first.
    """
    by_profile = defaultdict(list)
    for c in candidates:
        by_profile[c.profile].append(c)

    chains = []
    for profile, items in by_profile.items():
        items = sorted(items, key=lambda c: c.captured_at)
        split_seconds = items[0].split_seconds
        tol = max(5.0, split_seconds * duration_tolerance_fraction)

        def is_full(c):
            return c.duration_seconds is not None and abs(c.duration_seconds - split_seconds) <= tol

        i = 0
        while i < len(items) - 1:
            if not is_full(items[i]):
                i += 1
                continue
            chain = [items[i]]
            j = i
            while j < len(items) - 1:
                nxt = items[j + 1]
                expected_end = chain[-1].captured_at + timedelta(seconds=chain[-1].duration_seconds)
                gap = (nxt.captured_at - expected_end).total_seconds()
                if not (gap_min <= gap <= gap_max):
                    break
                chain.append(nxt)
                if not is_full(nxt):
                    break   # a short/partial part ends the chain (it's the last one)
                j += 1
            if len(chain) >= 2:
                chains.append(_describe_split_chain(chain, profile, split_seconds))
            i = j + 1
    return chains


def _describe_split_chain(chain, profile, split_seconds):
    times = " → ".join(c.captured_at.strftime("%H:%M:%S") for c in chain)
    total_min = sum(c.duration_seconds for c in chain) / 60
    return {
        "ids": [c.id for c in chain], "type": "split",
        "reason": f"{profile}: {len(chain)} parts every {split_seconds // 60} min, {times} "
                  f"(about {total_min:.0f} min total)",
    }


def _shared_label(names, max_varying_fraction=0.4, max_varying_len=6):
    """Longest common prefix across every name in `names` (via the lexicographic min/max trick),
    accepted only if what's left of each name is short and actually varies -- i.e. looks like real
    track labels ('Tr1'/'Tr2', 'CH01'/'CH02'), not two unrelated names that happen to start the same.
    Returns (prefix, {name: suffix}) or None."""
    if len(names) < 2:
        return None
    shortest = min(len(n) for n in names)
    lo, hi = min(names), max(names)
    i = 0
    while i < len(lo) and i < len(hi) and lo[i] == hi[i]:
        i += 1
    prefix = lo[:i]
    if len(prefix) < shortest * (1 - max_varying_fraction):
        return None
    suffixes = {n: n[len(prefix):] for n in names}
    if any(not (1 <= len(s) <= max_varying_len) for s in suffixes.values()):
        return None
    if len(set(suffixes.values())) != len(names):
        return None   # not actually varying -> these aren't distinct track labels
    return prefix, suffixes


def _finalize_multitrack_cluster(cluster, why, duration_tolerance_fraction):
    durations = sorted(c.duration_seconds for c in cluster)
    median = durations[len(durations) // 2]
    tol = max(2.0, median * duration_tolerance_fraction)
    if any(abs(d - median) > tol for d in durations):
        return []
    shared = _shared_label([_stripped(c.filename) for c in cluster])
    if not shared:
        return []
    prefix, suffixes = shared
    mins, secs = int(median // 60), int(median % 60)
    return [{
        "ids": [c.id for c in cluster], "type": "multitrack",
        "reason": f"{len(cluster)} files {why}, about {mins}:{secs:02d} long, sharing the name \"{prefix}\"",
        "labels": {c.id: suffixes[_stripped(c.filename)] for c in cluster},
    }]


def find_multitrack_by_time(candidates, start_tolerance=Config.MULTITRACK_START_TOLERANCE_SECONDS,
                            max_span=Config.MULTITRACK_MAX_SPAN_SECONDS,
                            duration_tolerance_fraction=Config.MULTITRACK_DURATION_TOLERANCE_FRACTION):
    """Files with a trusted timestamp that start within a few seconds of each other: single-linkage
    clustering by captured_at, then filtered to clusters that also agree on duration and share a
    filename prefix with a short varying label. candidates should already be limited to
    captured_at_precision == 'exact' with both captured_at and duration_seconds set."""
    items = sorted(candidates, key=lambda c: c.captured_at)
    clusters, current = [], []
    for c in items:
        if current and (c.captured_at - current[-1].captured_at).total_seconds() > start_tolerance:
            if len(current) >= 2:
                clusters.append(current)
            current = []
        current.append(c)
    if len(current) >= 2:
        clusters.append(current)

    out = []
    for cluster in clusters:
        if (cluster[-1].captured_at - cluster[0].captured_at).total_seconds() > max_span:
            continue
        out += _finalize_multitrack_cluster(cluster, f"starting within {start_tolerance:.0f}s of each other",
                                            duration_tolerance_fraction)
    return out


def find_multitrack_by_batch(candidates, batch_window=Config.MULTITRACK_BATCH_WINDOW_SECONDS,
                             duration_tolerance_fraction=Config.MULTITRACK_DURATION_TOLERANCE_FRACTION):
    """Fallback for recorders with no timestamp in the filename at all: same Inbox folder, arrived
    (created_at) within the same short window, equal duration, shared name. candidates should
    already be limited to a non-exact captured_at_precision with a folder and duration known."""
    by_folder = defaultdict(list)
    for c in candidates:
        if c.folder:
            by_folder[c.folder].append(c)

    out = []
    for folder, items in by_folder.items():
        items = sorted(items, key=lambda c: c.created_at)
        clusters, current = [], []
        for c in items:
            if current and (c.created_at - current[-1].created_at).total_seconds() > batch_window:
                if len(current) >= 2:
                    clusters.append(current)
                current = []
            current.append(c)
        if len(current) >= 2:
            clusters.append(current)
        for cluster in clusters:
            out += _finalize_multitrack_cluster(cluster, f'ingested together from "{folder}"',
                                                duration_tolerance_fraction)
    return out


# ---------------------------------------------------------------- suggest-groupings sweeper

def _record_run(job_name, status, detail):
    run = JobRun.query.get(job_name) or JobRun(job_name=job_name)
    run.last_run_at = datetime.utcnow()
    run.status = status
    run.log_tail = detail[-4000:]
    db.session.merge(run)
    db.session.commit()


def _load_candidates():
    profiles = {p.name: p for p in RecorderProfile.query.all()}
    rows = (Resource.query.filter(
        Resource.role == "original", Resource.status.in_(CANDIDATE_STATUSES),
        Resource.group_id.is_(None), Resource.duration_seconds.isnot(None),
    ).order_by(Resource.captured_at).all())
    out = []
    for r in rows:
        info = r.filename_info or {}
        profile = profiles.get(info.get("profile"))
        out.append(GroupCandidate(
            id=r.id, filename=r.filename, captured_at=r.captured_at,
            captured_at_precision=r.captured_at_precision or "unknown",
            duration_seconds=r.duration_seconds, created_at=r.created_at or datetime.utcnow(),
            profile=info.get("profile"), split_seconds=profile.split_seconds if profile else None,
            folder=info.get("folder"),
        ))
    return out


def _commit():
    try:
        db.session.commit()
        return True
    except Exception as e:  # noqa: BLE001
        db.session.rollback()
        log.warning("could not save a grouping suggestion: %s", e)
        return False


def suggest_groupings():
    """Finds new split/multitrack candidates among ungrouped resources and stamps group_id/
    group_type/group_status='suggested'/group_reason. Split is tried first (the more specific,
    more certain pattern), so a file can't be claimed as a fuzzy multitrack match if it's really
    part of a split chain. Never touches a resource that already has a group_id."""
    candidates = _load_candidates()

    split_pool = [c for c in candidates if c.captured_at and c.split_seconds]
    groups = find_split_chains(split_pool)
    claimed = {cid for g in groups for cid in g["ids"]}

    remaining = [c for c in candidates if c.id not in claimed]
    timed = [c for c in remaining if c.captured_at and c.captured_at_precision == "exact" and c.duration_seconds]
    groups += find_multitrack_by_time(timed)
    claimed |= {cid for g in groups for cid in g["ids"]}

    remaining = [c for c in remaining if c.id not in claimed]
    untimed = [c for c in remaining if c.captured_at_precision != "exact" and c.duration_seconds and c.folder]
    groups += find_multitrack_by_batch(untimed)

    created = []
    for g in groups:
        gid = str(uuid.uuid4())
        touched = False
        for cid in g["ids"]:
            r = db.session.get(Resource, cid)
            if r is None or r.group_id is not None:
                continue   # vanished, or already claimed earlier in this same run
            r.group_id, r.group_type, r.group_status, r.group_reason = gid, g["type"], "suggested", g["reason"]
            if g["type"] == "multitrack" and not r.track_label:
                r.track_label = g["labels"][cid]
            touched = True
        if touched and _commit():
            created.append(gid)
    _record_run("suggest-groupings", "success", f"new suggestions: {len(created)}")
    return {"status": "success", "groups": created}


# ---------------------------------------------------------------- join-groups sweeper

JOIN_FORMAT_FIELDS = ("codec_name", "sample_fmt", "sample_rate", "channels")


def _nice():
    os.nice(10)


def _probe_stream_format(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
         "stream=codec_name,sample_fmt,sample_rate,channels", "-of", "json", path],
        capture_output=True, text=True, timeout=Config.PROBE_TIMEOUT_SECONDS,
    )
    if out.returncode != 0:
        raise GroupingError(f"{os.path.basename(path)} could not be read: {out.stderr.strip()[-200:] or 'ffprobe failed'}")
    stream = (json.loads(out.stdout).get("streams") or [{}])[0]
    if not stream.get("codec_name"):
        raise GroupingError(f"{os.path.basename(path)}: no audio stream found")
    return {k: stream.get(k) for k in JOIN_FORMAT_FIELDS}


def _cleanup(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _common_structure(members):
    """(session_id, project_id) shared by every member, respecting 'a file's project is always
    its session's project' -- or (None, None) if they don't all agree."""
    session_ids = {m.session_id for m in members}
    if len(session_ids) == 1 and None not in session_ids:
        session = db.session.get(RecordingSession, session_ids.pop())
        if session is not None:
            return session.id, session.project_id
    project_ids = {m.project_id for m in members}
    if len(project_ids) == 1 and None not in project_ids:
        return None, project_ids.pop()
    return None, None


def _common_value(values):
    s = set(values)
    return s.pop() if len(s) == 1 and None not in s else None


def _join_group(members):
    """members: Resource rows for one split group, in time order. Returns a new, unsaved Resource.
    Raises NasUnavailable (stop the whole sweep run), GroupWaiting (skip, retry next sweep) or
    GroupingError (mark this group join-failed)."""
    nas_ok, nas_reason = nas_status()
    paths = []
    for m in members:
        src = m.nas_path or m.staging_path
        if not src:
            raise GroupingError(f"{m.filename} has no file recorded on disk")
        if is_nas_path(src) and not nas_ok:
            raise NasUnavailable(nas_reason)
        if not os.path.exists(src):
            raise GroupingError(f"{m.filename} is missing from disk ({src})")
        paths.append(src)

    formats = [_probe_stream_format(p) for p in paths]
    if len({tuple(f[k] for k in JOIN_FORMAT_FIELDS) for f in formats}) > 1:
        detail = "; ".join(f"{m.filename}: {f}" for m, f in zip(members, formats))
        raise GroupingError(f"the parts do not all share the same audio format, so they can't be "
                            f"joined safely without re-encoding ({detail})")

    total_size = sum((m.size_bytes or os.path.getsize(p)) for m, p in zip(members, paths))
    verdict, reason = disk_budget.admit(
        total_size, shutil.disk_usage(Config.STAGING_DIR).free, disk_budget.staged_bytes(Config.STAGING_DIR),
        Config.DISK_RESERVE_GB * disk_budget.GB, Config.STAGING_BUDGET_GB * disk_budget.GB,
    )
    if verdict == disk_budget.WAIT:
        raise GroupWaiting(reason)
    if verdict == disk_budget.NEVER:
        raise GroupingError(f"not enough staging budget to join these files ({reason})")

    base, ext = os.path.splitext(members[0].filename)
    out_dir = os.path.join(Config.STAGING_DIR, "joins", members[0].group_id)
    os.makedirs(out_dir, exist_ok=True)
    dest = os.path.join(out_dir, f"{base}__joined{ext}")
    # ffmpeg picks the output container from the filename's extension, so the extension has to be
    # last (".wav.part" would leave it guessing) -- same trick as previews.py's preview ".part.m4a".
    tmp = os.path.join(out_dir, f"{base}__joined.part{ext}")
    listfile = os.path.join(out_dir, "concat_list.txt")
    with open(listfile, "w") as f:
        for p in paths:
            f.write("file '%s'\n" % os.path.abspath(p).replace("'", "'\\''"))
    try:
        out = subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "concat", "-safe", "0", "-i", listfile, "-c", "copy", "-y", tmp],
            capture_output=True, text=True, timeout=Config.GROUP_JOIN_TIMEOUT_SECONDS, preexec_fn=_nice,
        )
    except subprocess.TimeoutExpired:
        _cleanup(tmp)
        raise GroupingError(f"joining timed out after {Config.GROUP_JOIN_TIMEOUT_SECONDS}s")
    finally:
        _cleanup(listfile)
    if out.returncode != 0 or not os.path.exists(tmp):
        _cleanup(tmp)
        raise GroupingError(f"ffmpeg exited {out.returncode}: {out.stderr.strip()[-300:]}")

    try:
        _rate, _channels, out_duration = probe_audio(tmp)
    except GenerationError as e:
        _cleanup(tmp)
        raise GroupingError(f"the joined file could not be read back: {e}")
    expected = sum(m.duration_seconds for m in members)
    if out_duration and abs(out_duration - expected) > max(5.0, expected * 0.02):
        _cleanup(tmp)
        raise GroupingError(f"the joined file is {out_duration:.1f}s but the parts add up to "
                            f"{expected:.1f}s, so it was not saved")

    os.replace(tmp, dest)
    checksum = sha256_file(dest)
    session_id, project_id = _common_structure(members)

    return Resource(
        checksum=checksum, filename=os.path.basename(dest), format=members[0].format,
        duration_seconds=out_duration or expected, size_bytes=os.path.getsize(dest),
        captured_at=members[0].captured_at, captured_at_source=members[0].captured_at_source,
        captured_at_precision=members[0].captured_at_precision,
        filename_info={"joined_from": [m.filename for m in members]},
        category=_common_value(m.category for m in members), project_id=project_id, session_id=session_id,
        role="edit", derived_from_id=members[0].id, joined_from_ids=[m.id for m in members],
        notes=f"Joined from {len(members)} parts: {', '.join(m.filename for m in members)}.",
        status="pending-review", staging_path=dest,
    )


def join_pending_groups():
    """Drains every split group the user has confirmed (group_status='joining'). One bad group
    doesn't stop the rest, except a NAS outage, which stops the whole run (like file_resources)."""
    group_ids = [gid for (gid,) in db.session.query(Resource.group_id).filter(
        Resource.group_type == "split", Resource.group_status == "joining",
    ).distinct().order_by(Resource.group_id).all()]

    joined, failed, waiting = [], [], []
    for gid in group_ids:
        members = [m for m in Resource.query.filter_by(group_id=gid, group_type="split")
                  .order_by(Resource.captured_at).all() if m.group_status == "joining"]
        if len(members) < 2:
            continue   # raced with a dismiss, or a member vanished: nothing sane to join
        try:
            new_resource = _join_group(members)
            db.session.add(new_resource)
            db.session.flush()
            for m in members:
                m.group_status, m.joined_into_id, m.group_error = "joined", new_resource.id, None
            db.session.add(FileEvent(resource_id=new_resource.id, event_type="joined",
                                     detail=f"joined from: {', '.join(m.filename for m in members)}"))
            for m in members:
                db.session.add(FileEvent(resource_id=m.id, event_type="joined",
                                         detail=f"joined into {new_resource.filename} ({new_resource.id})"))
            db.session.commit()
            joined.append(gid)
        except NasUnavailable as e:
            db.session.rollback()
            _record_run("join-groups", "error", f"NAS unavailable part-way: {e}. joined={len(joined)}")
            return {"status": "error", "detail": str(e), "joined": joined, "failed": failed}
        except GroupWaiting as e:
            db.session.rollback()
            waiting.append(gid)
            log.info("join %s waiting: %s", gid, e)
        except Exception as e:  # noqa: BLE001 -- one bad group must not stop the rest
            db.session.rollback()
            failed.append(gid)
            log.warning("join %s failed: %s", gid, e)
            for m in Resource.query.filter_by(group_id=gid, group_type="split").all():
                m.group_status, m.group_error = "join-failed", str(e)[:2000]
            _commit()

    status = "partial" if failed else "success"
    _record_run("join-groups", status, f"joined: {len(joined)}, failed: {len(failed)}, waiting: {len(waiting)}")
    return {"status": status, "joined": joined, "failed": failed, "waiting": waiting}
