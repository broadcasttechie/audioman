import uuid
from datetime import datetime

from .extensions import db


def gen_uuid():
    return str(uuid.uuid4())


class Project(db.Model):
    __tablename__ = "projects"

    id = db.Column(db.String, primary_key=True, default=gen_uuid)
    name = db.Column(db.String, nullable=False)
    slug = db.Column(db.String, unique=True, nullable=False)
    notes = db.Column(db.Text)

    resources = db.relationship("Resource", back_populates="project")


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

    captured_at = db.Column(db.DateTime)
    # embedded | dawarich-inferred | manual
    captured_at_source = db.Column(db.String, default="embedded")

    # ambient | event | voice-personal | voice-project
    # Nullable: not inferable from the file itself — set during review.
    category = db.Column(db.String, nullable=True)

    project_id = db.Column(db.String, db.ForeignKey("projects.id"), nullable=True)
    project = db.relationship("Project", back_populates="resources")

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


class Location(db.Model):
    __tablename__ = "locations"

    id = db.Column(db.String, primary_key=True, default=gen_uuid)
    resource_id = db.Column(db.String, db.ForeignKey("resources.id"), unique=True)
    resource = db.relationship("Resource", back_populates="location")

    lat = db.Column(db.Float)
    lon = db.Column(db.Float)
    # dawarich-auto | manual | none
    source = db.Column(db.String, default="none")


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


class Clip(db.Model):
    """
    A sub-clip marker against a resource — start/end offsets in
    seconds, metadata only. Export state lives entirely in the Export
    table (a clip can be exported more than once, in different
    formats), not here.
    """
    __tablename__ = "clips"

    id = db.Column(db.String, primary_key=True, default=gen_uuid)
    resource_id = db.Column(db.String, db.ForeignKey("resources.id"), index=True)
    resource = db.relationship("Resource", back_populates="clips")

    start_seconds = db.Column(db.Float, nullable=False)
    end_seconds = db.Column(db.Float, nullable=False)
    label = db.Column(db.String)
    notes = db.Column(db.Text)
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
    # ingested | moved | refiled | drive-synced | failed | verified
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
