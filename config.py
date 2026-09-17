import os

class Config:
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", "postgresql://audio:audio@localhost/audio_manager"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # --- Storage paths ---
    NAS_LIBRARY_ROOT = os.environ.get("NAS_LIBRARY_ROOT", "/mnt/nas/audio")
    STAGING_DIR = os.environ.get("STAGING_DIR", "/var/lib/audio-manager/staging")

    # --- rclone remotes (names as configured in rclone.conf) ---
    RCLONE_DRIVE_REMOTE = os.environ.get("RCLONE_DRIVE_REMOTE", "gdrive")
    DRIVE_INBOX_PATH = os.environ.get("DRIVE_INBOX_PATH", "Inbox")
    DRIVE_LIBRARY_PATH = os.environ.get("DRIVE_LIBRARY_PATH", "Library")

    # --- Inbox stability check ---
    MIN_AGE_MINUTES = int(os.environ.get("MIN_AGE_MINUTES", 5))

    # --- Folder pattern (Pattern C) ---
    # Rendered against a dict of: project, category, year, filename
    # Falls back to the second template when `project` is None/empty.
    PATH_TEMPLATE_WITH_PROJECT = "{project}/{filename}"
    PATH_TEMPLATE_NO_PROJECT = "misc/{category}/{year}/{filename}"

    # --- Categories (fixed enum) ---
    CATEGORIES = ["ambient", "event", "voice-personal", "voice-project"]

    # --- Dawarich integration ---
    DAWARICH_API_URL = os.environ.get("DAWARICH_API_URL", "")
    DAWARICH_API_KEY = os.environ.get("DAWARICH_API_KEY", "")

    # --- Clip export (legacy path, superseded by Export workflow below —
    # kept only so an old export from before this change still resolves) ---
    CLIPS_EXPORT_DIR = os.environ.get("CLIPS_EXPORT_DIR", "/var/lib/audio-manager/clips")

    # --- Export / format-conversion workflow ---
    EXPORTS_DIR = os.environ.get("EXPORTS_DIR", "/var/lib/audio-manager/exports")
    EXPORT_FORMATS = ["original", "wav", "mp3", "flac"]
    EXPORT_MP3_DEFAULT_QUALITY = "192k"

    # --- Direct upload (e.g. a companion mobile app) ---
    # Unlike the rest of this API (assumed LAN-only / behind a VPN),
    # this endpoint is the one most likely to need exposing to the
    # internet (a phone on mobile data, not on the home network) — so
    # it's the one endpoint that gets its own shared-secret gate rather
    # than relying on network trust. Set a long random value in
    # production; empty disables the endpoint entirely (fails closed).
    UPLOAD_API_KEY = os.environ.get("UPLOAD_API_KEY", "")

    # --- Immich integration (companion photos) ---
    IMMICH_API_URL = os.environ.get("IMMICH_API_URL", "")
    IMMICH_API_KEY = os.environ.get("IMMICH_API_KEY", "")

    # --- Reliability: timeouts, retries, circuit breaker ---
    HTTP_TIMEOUT_SECONDS = 10
    EXTERNAL_MAX_ATTEMPTS = 3          # per-call retry ceiling (background jobs)
    EXTERNAL_RETRY_BASE_DELAY = 1.0    # seconds, doubles each attempt
    EXTERNAL_CIRCUIT_THRESHOLD = 3     # consecutive failures before a batch job aborts for this run

    RCLONE_LIST_TIMEOUT_SECONDS = 30
    RCLONE_TRANSFER_TIMEOUT_SECONDS = 1800  # 30 min ceiling — raise if you expect huge files
    PROBE_TIMEOUT_SECONDS = 30              # ffprobe / exiftool
    FFMPEG_TIMEOUT_SECONDS = 300            # clip export

    # --- Job queue (jobs/queue.py, jobs/worker.py) ---
    JOB_QUEUE_POLL_INTERVAL_SECONDS = 5     # how often an idle worker checks for new work
    JOB_QUEUE_MAX_ATTEMPTS = 3              # per job-queue-item, separate from EXTERNAL_MAX_ATTEMPTS
    JOB_QUEUE_RETRY_BASE_DELAY_SECONDS = 30 # doubles each attempt, same backoff shape as retry.py
    # If a claimed row is still 'running' after this long, assume the
    # worker that claimed it died (killed, OOM, crashed) rather than
    # that the job is just slow — comfortably above
    # RCLONE_TRANSFER_TIMEOUT_SECONDS so a legitimately long rclone
    # sync is never reaped out from under a still-alive worker.
    JOB_QUEUE_STALE_RUNNING_TIMEOUT_SECONDS = 3600

    # SQLAlchemy connections in a long-lived process (the worker runs
    # for days/weeks) can go stale if Postgres restarts or a firewall
    # drops idle connections. pool_pre_ping tests each connection with
    # a cheap round-trip before use and transparently reconnects if
    # it's dead, rather than surfacing a mysterious OperationalError
    # on whatever query happens to run next.
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    # --- Job schedule (minutes) ---
    DRIVE_INBOX_PULL_INTERVAL_MIN = int(os.environ.get("DRIVE_INBOX_PULL_INTERVAL_MIN", 15))
    NAS_TO_DRIVE_INTERVAL_MIN = int(os.environ.get("NAS_TO_DRIVE_INTERVAL_MIN", 60))
    LIBRARY_VERIFY_INTERVAL_MIN = int(os.environ.get("LIBRARY_VERIFY_INTERVAL_MIN", 24 * 60))
