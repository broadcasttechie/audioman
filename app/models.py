import uuid
from datetime import datetime

from .extensions import db


def gen_uuid():
    return str(uuid.uuid4())


class Project(db.Model):
    __tablename__ = "projects"

    id = db.Column(db.String, primary_key=True, default=gen_uuid)
    name = db.Column(db.String, nullable=False)
    slug = db.Column(db.String, unique=True, nullable=False)   # the NAS folder name; never changed after filing
    notes = db.Column(db.Text)
    # Where copies live and where the project is edited (PLAN 18.5j/k). Stored now, acted on by the
    # placement work (NEXT.md package 9): nas | drive | both, and nas | drive.
    placement = db.Column(db.String, nullable=False, default="nas")
    home = db.Column(db.String, nullable=False, default="nas")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    resources = db.relationship("Resource", back_populates="project")
    sessions = db.relationship("RecordingSession", back_populates="project")


class RecordingSession(db.Model):
    """
    One night of a show, or one outing: what is reviewed, dated, located and filed as a unit
    (PLAN 18.1). A session normally belongs to a project; it may have none (an outing of loose
    field takes). `name` is the NAS folder name, so it is sanitised when a path is built.
    Named RecordingSession so it can't be confused with db.session or Flask's session.
    """
    __tablename__ = "sessions"
    __table_args__ = (db.UniqueConstraint("project_id", "name", name="uq_session_project_name"),)

    id = db.Column(db.String, primary_key=True, default=gen_uuid)   # may be chosen by a client (idempotent create)
    project_id = db.Column(db.String, db.ForeignKey("projects.id"), nullable=True)
    name = db.Column(db.String, nullable=False)
    session_date = db.Column(db.Date, nullable=True)   # the local calendar date, for sorting/display
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    project = db.relationship("Project", back_populates="sessions")
    resources = db.relationship("Resource", back_populates="session")


class Tag(db.Model):
    __tablename__ = "tags"

    id = db.Column(db.String, primary_key=True, default=gen_uuid)
    name = db.Column(db.String, unique=True, nullable=False)


resource_tags = db.Table(
    "resource_tags",
    db.Column("resource_id", db.String, db.ForeignKey("resources.id"), primary_key=True),
    db.Column("tag_id", db.String, db.ForeignKey("tags.id"), primary_key=True),
)


class Resource(db.Model):
    __tablename__ = "resources"

    id = db.Column(db.String, primary_key=True, default=gen_uuid)
    checksum = db.Column(db.String, unique=True, nullable=False, index=True)
    filename = db.Column(db.String, nullable=False)
    format = db.Column(db.String)
    duration_seconds = db.Column(db.Float)

    captured_at = db.Column(db.DateTime)   # naive UTC (app/timeutil.py)
    # filename | embedded | dawarich-inferred | manual
    captured_at_source = db.Column(db.String, default="embedded")
    # exact | approximate | unknown. Invariant: unknown <=> captured_at is NULL. Only
    # `exact` is looked up in Dawarich/Immich and pinned on the map (PLAN 18.5c).
    captured_at_precision = db.Column(db.String, default="unknown")
    # A date read from a filename by a "suggest" recorder profile, awaiting the user's
    # confirmation (a "trusted" profile writes captured_at directly).
    suggested_captured_at = db.Column(db.DateTime, nullable=True)
    # What the filename told us: profile, title, is_edit, seq, unknown_reason, time_known, folder
    filename_info = db.Column(db.JSON, nullable=True)

    # ambient | event | voice-personal | voice-project
    # Nullable: not inferable from the file itself — set during review.
    category = db.Column(db.String, nullable=True)

    project_id = db.Column(db.String, db.ForeignKey("projects.id"), nullable=True)
    project = db.relationship("Project", back_populates="resources")
    # A file's project is always its session's project (kept consistent by the API).
    session_id = db.Column(db.String, db.ForeignKey("sessions.id"), nullable=True)
    session = db.relationship("RecordingSession", back_populates="resources")

    # original | edit | export | sidecar | project-file (PLAN 18.1). An edit is a version of the
    # same recording (cleaned up / trimmed) and points at its original; segments belong to one
    # file, never shared across versions.
    role = db.Column(db.String, nullable=False, default="original")
    derived_from_id = db.Column(db.String, db.ForeignKey("resources.id"), nullable=True)
    track_label = db.Column(db.String, nullable=True)   # e.g. one mono track of a multitrack take
    notes = db.Column(db.Text)
    derived_from = db.relationship("Resource", remote_side="Resource.id", backref="edits", foreign_keys=[derived_from_id])

    # Split-file / multitrack grouping (jobs/grouping.py; PLAN 22). group_id is shared by every
    # member of one candidate/confirmed group; NULL means "not part of any group (yet)". A resource
    # already carrying a group_id is never reconsidered by the detector, even if dismissed.
    group_id = db.Column(db.String, nullable=True, index=True)
    group_type = db.Column(db.String, nullable=True)     # split | multitrack
    # suggested | dismissed | confirmed (multitrack) | joining | joined | join-failed (split)
    group_status = db.Column(db.String, nullable=True)
    group_reason = db.Column(db.Text, nullable=True)     # human-readable "why these belong together"
    group_error = db.Column(db.Text, nullable=True)      # why a join failed, when group_status = join-failed
    # Set on each source part once a split group has been joined into one new file.
    joined_into_id = db.Column(db.String, db.ForeignKey("resources.id"), nullable=True)
    # Set on the new joined file: the ordered source resource ids (derived_from_id is also the
    # first of these, so the existing "edit points at its original" plumbing still works).
    joined_from_ids = db.Column(db.JSON, nullable=True)

    size_bytes = db.Column(db.BigInteger, nullable=True)
    # Derived playback files (jobs/previews.py). *_at set = generated; *_error set = failed, awaiting a manual retry.
    waveform_at = db.Column(db.DateTime, nullable=True)
    waveform_error = db.Column(db.Text, nullable=True)
    preview_at = db.Column(db.DateTime, nullable=True)
    preview_error = db.Column(db.Text, nullable=True)

    # pending-review | filed | archived | failed
    status = db.Column(db.String, default="pending-review", index=True)

    # Only meaningful when status == "failed". Tells retry_failed which
    # pipeline step to resume. External lookups (Dawarich/Immich) are
    # NOT here — they never fail a resource, see dawarich_checked_at
    # / immich_checked_at below and jobs/enrich.py.
    # checksum | metadata-extraction | move
    failure_stage = db.Column(db.String, nullable=True)
    failure_detail = db.Column(db.Text, nullable=True)

    # NULL = not yet successfully checked (queued for jobs/enrich.py).
    # Set the moment a check succeeds, whether or not it found
    # anything — this is what lets a Dawarich/Immich outage of any
    # length self-heal with no manual retry: the enrichment jobs just
    # keep querying "where checked_at IS NULL" every run.
    dawarich_checked_at = db.Column(db.DateTime, nullable=True)
    immich_checked_at = db.Column(db.DateTime, nullable=True)

    nas_path = db.Column(db.String)
    drive_inbox_path = db.Column(db.String, nullable=True)
    # Where the file physically sits after ingest, before it's filed.
    # Cleared once nas_path is set at filing time.
    staging_path = db.Column(db.String, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    location = db.relationship("Location", back_populates="resource", uselist=False)
    tags = db.relationship("Tag", secondary=resource_tags, backref="resources")
    track_points = db.relationship(
        "TrackPoint", back_populates="resource",
        order_by="TrackPoint.recorded_at", cascade="all, delete-orphan",
    )
    clips = db.relationship("Clip", back_populates="resource", cascade="all, delete-orphan")
    photos = db.relationship("ResourcePhoto", back_populates="resource", cascade="all, delete-orphan")
    exports = db.relationship("Export", back_populates="resource", cascade="all, delete-orphan")


class RecorderProfile(db.Model):
    """How to read one recorder's filenames (see jobs/filename_patterns.py). Seeded from
    DEFAULT_PROFILES and editable afterwards; lower priority number is tried first."""
    __tablename__ = "recorder_profiles"

    id = db.Column(db.String, primary_key=True, default=gen_uuid)
    name = db.Column(db.String, unique=True, nullable=False)
    patterns = db.Column(db.JSON, nullable=False, default=list)
    timezone = db.Column(db.String, nullable=False, default="Europe/London")   # IANA
    # recorder clock minus true time, in seconds (a recorder running 3 min fast = 180)
    clock_offset_seconds = db.Column(db.Integer, nullable=False, default=0)
    # trusted: a filename date is applied at ingest; suggest: it is only offered for confirmation
    date_trust = db.Column(db.String, nullable=False, default="suggest")
    priority = db.Column(db.Integer, nullable=False, default=100)
    active = db.Column(db.Boolean, nullable=False, default=True)
    # This recorder splits one continuous recording into consecutive files after this many
    # seconds (the Insta360 mic's 30-minute limit = 1800); NULL for a recorder that doesn't
    # split. Read by jobs/grouping.py to suggest re-joining a chain of parts.
    split_seconds = db.Column(db.Integer, nullable=True)


class Category(db.Model):
    """
    A high-level kind of recording (Field recordings, Event, Voice...). Configurable data, not a fixed list:
    the user isn't yet sure what categories they want. `slug` is the NAS folder name for loose files
    (misc/<slug>/...) and never changes once created; `label` is what people see and can be renamed freely.
    Categories are archived rather than deleted (files keep theirs) and can be merged into another.
    """
    __tablename__ = "categories"

    slug = db.Column(db.String, primary_key=True)
    label = db.Column(db.String, nullable=False)
    archived = db.Column(db.Boolean, nullable=False, default=False)
    sort_order = db.Column(db.Integer, nullable=False, default=100)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Location(db.Model):
    __tablename__ = "locations"

    id = db.Column(db.String, primary_key=True, default=gen_uuid)
    resource_id = db.Column(db.String, db.ForeignKey("resources.id"), unique=True)
    resource = db.relationship("Resource", back_populates="location")

    lat = db.Column(db.Float)
    lon = db.Column(db.Float)
    # dawarich-auto | manual | none
    source = db.Column(db.String, default="none")

    # A readable name for the place ("Rock, Wyre Forest, Worcestershire"), looked up from the coordinates by the
    # geocoder (jobs/photon.py) or typed by hand. place_checked_at NULL = queued for a lookup; it is set even when
    # nothing was found, so an empty answer isn't asked again. Cleared when the location moves.
    place_name = db.Column(db.String, nullable=True)
    place_source = db.Column(db.String, nullable=True)     # photon | manual
    place_info = db.Column(db.JSON, nullable=True)
    place_checked_at = db.Column(db.DateTime, nullable=True)


class TrackPoint(db.Model):
    """
    The full GPS path spanning a recording's duration, cached from
    Dawarich at ingest time (one fetch, see jobs/dawarich.py
    fetch_points_in_range). Read-only from the app's perspective —
    if you need to refresh it, re-run dawarich-requery rather than
    editing these rows directly.
    """
    __tablename__ = "track_points"

    id = db.Column(db.String, primary_key=True, default=gen_uuid)
    resource_id = db.Column(db.String, db.ForeignKey("resources.id"), index=True)
    resource = db.relationship("Resource", back_populates="track_points")

    recorded_at = db.Column(db.DateTime, nullable=False)
    lat = db.Column(db.Float, nullable=False)
    lon = db.Column(db.Float, nullable=False)


clip_tags = db.Table(
    "clip_tags",
    db.Column("clip_id", db.String, db.ForeignKey("clips.id"), primary_key=True),
    db.Column("tag_id", db.String, db.ForeignKey("tags.id"), primary_key=True),
)


class Clip(db.Model):
    """
    A time range within a resource -- PLAN 18.4's "segment": the unit of
    search for voice material is a range, not just the file (find Jacob
    saying a particular phrase and land on that moment). A plain export-only
    marker (the original use) is just a segment with no tags/transcript/
    speaker set; export state lives entirely in the Export table (a clip can
    be exported more than once, in different formats), not here.

    Kept as the `Clip` class/table (not renamed to `Segment`) to avoid
    disturbing the working export/editor code that already depends on it --
    PLAN 18.4 calls this "the existing Clip becomes one use of a segment",
    which this satisfies by extension rather than a rename.
    """
    __tablename__ = "clips"

    id = db.Column(db.String, primary_key=True, default=gen_uuid)
    resource_id = db.Column(db.String, db.ForeignKey("resources.id"), index=True)
    resource = db.relationship("Resource", back_populates="clips")

    start_seconds = db.Column(db.Float, nullable=False)
    end_seconds = db.Column(db.Float, nullable=False)
    label = db.Column(db.String)
    notes = db.Column(db.Text)
    # Tags come from the SAME pool as file/project tags (PLAN 18.4: "one tag pool"), including a
    # person's name where the speaker is identified by tag rather than (or as well as) `speaker`.
    tags = db.relationship("Tag", secondary=clip_tags, backref="clips")
    # Manually typed for now (transcription is a separate, not-yet-built worker, PLAN 18.5); a
    # future ML pass suggests values here rather than overwriting anything already set.
    transcript = db.Column(db.Text, nullable=True)
    speaker = db.Column(db.String, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class ResourcePhoto(db.Model):
    """
    Links a resource to an Immich asset taken around the same time.
    We never copy the photo itself — just the Immich asset id — and
    proxy thumbnail/original requests through Immich's own API at
    render time (see app/api.py), so the browser never needs its own
    Immich credentials.
    """
    __tablename__ = "resource_photos"
    __table_args__ = (
        db.UniqueConstraint("resource_id", "immich_asset_id", name="uq_resource_immich_asset"),
    )

    id = db.Column(db.String, primary_key=True, default=gen_uuid)
    resource_id = db.Column(db.String, db.ForeignKey("resources.id"), index=True)
    resource = db.relationship("Resource", back_populates="photos")

    immich_asset_id = db.Column(db.String, nullable=False)
    taken_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Export(db.Model):
    """
    An export/format-conversion request — either the whole resource
    re-encoded, or (if clip_id is set) just that clip's time window,
    optionally re-encoded. Metadata only until jobs/export.py's
    process_exports() (a queued job, never run inline in a request)
    actually does the ffmpeg work — same "request is a fast DB write,
    execution happens elsewhere" shape as everything else in this app.
    """
    __tablename__ = "exports"

    id = db.Column(db.String, primary_key=True, default=gen_uuid)
    resource_id = db.Column(db.String, db.ForeignKey("resources.id"), index=True)
    resource = db.relationship("Resource", back_populates="exports")
    clip_id = db.Column(db.String, db.ForeignKey("clips.id"), nullable=True)
    clip = db.relationship("Clip")

    # original (stream-copy, requires clip_id) | wav | mp3 | flac
    format = db.Column(db.String, nullable=False)
    quality = db.Column(db.String, nullable=True)  # e.g. "192k", mp3 only
    embed_metadata = db.Column(db.Boolean, default=True)

    # queued | running | success | error
    status = db.Column(db.String, default="queued", index=True)
    output_path = db.Column(db.String, nullable=True)
    error_detail = db.Column(db.Text, nullable=True)

    requested_at = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime, nullable=True)


class FileEvent(db.Model):
    __tablename__ = "file_events"

    id = db.Column(db.String, primary_key=True, default=gen_uuid)
    resource_id = db.Column(db.String, db.ForeignKey("resources.id"), nullable=True)
    # ingested | moved | refiled | drive-synced | failed | verified | joined
    event_type = db.Column(db.String, nullable=False)
    detail = db.Column(db.Text)
    occurred_at = db.Column(db.DateTime, default=datetime.utcnow)


class PendingUpload(db.Model):
    """Scratch table for the Drive inbox stability check."""
    __tablename__ = "pending_uploads"

    path = db.Column(db.String, primary_key=True)
    size = db.Column(db.BigInteger)
    modtime = db.Column(db.String)
    first_seen_at = db.Column(db.DateTime, default=datetime.utcnow)


class JobRun(db.Model):
    """Last-run tracking for rclone + maintenance jobs, surfaced via API."""
    __tablename__ = "job_runs"

    job_name = db.Column(db.String, primary_key=True)
    last_run_at = db.Column(db.DateTime)
    # success | error | partial | running
    status = db.Column(db.String)
    log_tail = db.Column(db.Text)


class DeviceToken(db.Model):
    """
    A per-device credential for the Android app (PLAN 19/22... see 22 for grouping, this is the
    Android backend). The raw token is shown to the user exactly once, at creation, then never
    again or anywhere else -- only its sha256 (`app/device_auth.py`) is stored, so a database
    dump can't be used to impersonate a device. Revoking keeps the row (never deleted) so past
    uploads still show which device they came from.
    """
    __tablename__ = "device_tokens"

    id = db.Column(db.String, primary_key=True, default=gen_uuid)
    label = db.Column(db.String, nullable=False)               # e.g. "Kristan's phone"
    token_hash = db.Column(db.String, unique=True, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_used_at = db.Column(db.DateTime, nullable=True)
    revoked_at = db.Column(db.DateTime, nullable=True)


class UploadSession(db.Model):
    """
    One in-progress (or just-finished) chunked upload from the app (PLAN 19.3): declare the file
    up front (name, size, sha256, and the metadata block it should land with), then PUT raw bytes
    in order. `bytes_received` is the resumability contract -- after a dropped connection the app
    GETs this row to find out where to resume, rather than resending from the start.
    """
    __tablename__ = "upload_sessions"

    id = db.Column(db.String, primary_key=True, default=gen_uuid)   # may be client-chosen (idempotent initiate)
    device_id = db.Column(db.String, db.ForeignKey("device_tokens.id"), nullable=False)

    filename = db.Column(db.String, nullable=False)
    declared_size = db.Column(db.BigInteger, nullable=False)
    declared_checksum = db.Column(db.String, nullable=False)
    # Resolved (not raw) at initiate time: project_id, session_id, category, tag_ids, notes,
    # source_path, recorder_hint, captured_at (ISO, if the app is overriding it), captured_at_precision.
    metadata_json = db.Column(db.JSON, nullable=True)   # never called `metadata`: db.Model reserves that name

    bytes_received = db.Column(db.BigInteger, nullable=False, default=0)
    # uploading -> completed | failed. A failed session (checksum mismatch, expired) must be
    # re-initiated from scratch, never resumed.
    status = db.Column(db.String, nullable=False, default="uploading")
    staging_path = db.Column(db.String, nullable=True)
    resource_id = db.Column(db.String, db.ForeignKey("resources.id"), nullable=True)   # set once completed
    error_detail = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Setting(db.Model):
    """
    DB-backed overrides for a small set of Config values that make
    sense to change at runtime from the settings page (API keys,
    remote URLs) rather than only via env var + restart. See
    app/settings.py for the resolver — anything not in its
    OVERRIDABLE set is env-var/config.py only, unaffected by this
    table.
    """
    __tablename__ = "settings"

    key = db.Column(db.String, primary_key=True)
    value = db.Column(db.Text, nullable=False, default="")
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class JobQueueItem(db.Model):
    """
    The actual queue — see jobs/queue.py. One row per enqueued attempt
    (not per job_name, unlike JobRun above, which is just a "latest
    status" summary). Claimed atomically via Postgres's
    `FOR UPDATE SKIP LOCKED` by a dedicated worker process
    (jobs/worker.py) — safe to run more than one of these; SKIP LOCKED
    guarantees no two workers claim the same row.
    """
    __tablename__ = "job_queue"
    __table_args__ = (
        # Enforces "at most one active row per job_name" at the DB
        # level, not just in application code — closes a TOCTOU race
        # in jobs.queue.enqueue() where two near-simultaneous enqueues
        # (concurrent "run now" clicks, or a timer firing while one is
        # already queued) could otherwise both pass the pre-check and
        # insert duplicate active rows. Only applies while status is
        # queued/running — any number of historical (success/error/
        # failed-permanently) rows are fine.
        db.Index(
            "uq_job_queue_active_job_name", "job_name", unique=True,
            postgresql_where=db.text("status IN ('queued', 'running')"),
        ),
    )

    id = db.Column(db.String, primary_key=True, default=gen_uuid)
    job_name = db.Column(db.String, nullable=False, index=True)
    # queued | running | success | error | failed-permanently
    status = db.Column(db.String, default="queued", index=True)
    # manual (UI "run now") | scheduled (systemd timer)
    triggered_by = db.Column(db.String, default="manual")

    attempts = db.Column(db.Integer, default=0)
    max_attempts = db.Column(db.Integer, default=3)

    enqueued_at = db.Column(db.DateTime, default=datetime.utcnow)
    # Gates re-claiming after a failed attempt - backoff delay lives
    # here, not in a sleep() anywhere, so it survives a worker restart.
    next_attempt_at = db.Column(db.DateTime, default=datetime.utcnow)
    started_at = db.Column(db.DateTime, nullable=True)
    finished_at = db.Column(db.DateTime, nullable=True)

    locked_by = db.Column(db.String, nullable=True)  # "<hostname>:<pid>"
    error_detail = db.Column(db.Text, nullable=True)
