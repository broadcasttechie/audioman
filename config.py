import os

class Config:
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", "postgresql://audio:audio@localhost/audio_manager"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Signs the session cookie used only for the OAuth CSRF state nonce
    # (app/api.py rclone_drive_oauth_start/callback) -- nothing else in
    # this app uses sessions. Browser only ever reaches this over HTTPS
    # (nginx terminates TLS in front), so SESSION_COOKIE_SECURE is safe
    # even though the nginx->Flask hop itself is plain HTTP.
    SECRET_KEY = os.environ.get("SECRET_KEY", "")
    SESSION_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True

    # --- Storage paths ---
    NAS_LIBRARY_ROOT = os.environ.get("NAS_LIBRARY_ROOT", "/mnt/nas/audio")
    # Guard against an unmounted NAS (jobs/nas.py): the library root must be a real
    # mount point AND contain this marker file, which exists only on the NAS share.
    # Set NAS_REQUIRE_MOUNT=0 only for local development without a NAS.
    NAS_MARKER_FILE = os.environ.get("NAS_MARKER_FILE", ".audio-manager-nas")
    NAS_REQUIRE_MOUNT = os.environ.get("NAS_REQUIRE_MOUNT", "1") not in ("0", "false", "False", "")
    STAGING_DIR = os.environ.get("STAGING_DIR", "/var/lib/audio-manager/staging")

    # --- rclone remotes (names as configured in rclone.conf) ---
    RCLONE_DRIVE_REMOTE = os.environ.get("RCLONE_DRIVE_REMOTE", "gdrive")
    DRIVE_INBOX_PATH = os.environ.get("DRIVE_INBOX_PATH", "Inbox")
    DRIVE_LIBRARY_PATH = os.environ.get("DRIVE_LIBRARY_PATH", "Library")

    # --- Google OAuth client (for the settings-page "Connect with Google"
    # redirect flow, app/api.py rclone_drive_oauth_*). A registered client
    # is only needed for that flow -- the settings-page paste-token
    # alternative uses rclone's own bundled client and needs neither of
    # these. ---
    GOOGLE_OAUTH_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
    GOOGLE_OAUTH_CLIENT_SECRET = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
    GOOGLE_OAUTH_REDIRECT_URI = os.environ.get(
        "GOOGLE_OAUTH_REDIRECT_URI",
        "https://audioman.home.zamia.co.uk/api/settings/rclone/drive/oauth/callback",
    )

    # --- Time contract (app/timeutil.py): the DB holds naive UTC. A wall-clock time read from a
    # file with no offset is the recorder's local time; until per-recorder profiles exist it is
    # interpreted in this IANA zone so British summer time is converted correctly. ---
    DEFAULT_RECORDER_TIMEZONE = os.environ.get("DEFAULT_RECORDER_TIMEZONE", "Europe/London")

    # --- Inbox stability check ---
    MIN_AGE_MINUTES = int(os.environ.get("MIN_AGE_MINUTES", 5))

    # --- Inbox pull: disk-space admission (jobs/disk_budget.py) ---
    # Pulled files wait in STAGING_DIR for review, and staging shares a
    # small disk with the app, so a bulk import must not fill it. A file
    # that doesn't fit stays in the Drive Inbox and is pulled later.
    DISK_RESERVE_GB = float(os.environ.get("DISK_RESERVE_GB", 3))          # always left free on the disk
    STAGING_BUDGET_GB = float(os.environ.get("STAGING_BUDGET_GB", 6))      # max bytes awaiting review/filing
    INBOX_MAX_FILES_PER_RUN = int(os.environ.get("INBOX_MAX_FILES_PER_RUN", 10))

    # --- Folder pattern (Pattern C) ---
    # Rendered against a dict of: project, category, year, filename
    # Falls back to the second template when `project` is None/empty.
    # Fields: project (slug), category, year, month (01-12), session, filename. {year}/{month} are
    # "unknown" when there is no date. A missing {session} drops its folder, empty path parts are
    # removed, and every part is made filesystem-safe (jobs/path_template.py).
    PATH_TEMPLATE_WITH_PROJECT = "{project}/{session}/{filename}"
    PATH_TEMPLATE_NO_PROJECT = "misc/{category}/{year}/{month}/{session}/{filename}"

    # --- Categories (fixed enum) ---
    # Seeds for the `categories` table (app/categories.py); after that the table is the source of truth
    # and these are only used to fill it the first time. Slugs are folder names and never change; the
    # labels are what the user sees ("ambient" is a field recording).
    CATEGORIES = ["ambient", "event", "voice-personal", "voice-project"]
    CATEGORY_LABELS = {"ambient": "Field recordings", "event": "Event",
                       "voice-personal": "Voice (personal)", "voice-project": "Voice (project)"}

    # --- Dawarich integration ---
    DAWARICH_API_URL = os.environ.get("DAWARICH_API_URL", "")
    DAWARICH_API_KEY = os.environ.get("DAWARICH_API_KEY", "")

    # --- Waveforms and listening copies (jobs/previews.py): derived files cached on LOCAL disk, keyed by the
    # file's sha256 (never the NAS) ---
    WAVEFORM_DIR = os.environ.get("WAVEFORM_DIR", "/var/lib/audio-manager/waveforms")
    PREVIEW_DIR = os.environ.get("PREVIEW_DIR", "/var/lib/audio-manager/previews")
    PEAKS_PER_SECOND = 100            # one dense tier; measured ~180 KB per 15 minutes
    PREVIEW_BITRATE_KBPS = 160        # stereo AAC; mono files get 60% of this. Quality/size trade-off is the user's call.
    WAVEFORM_TIMEOUT_SECONDS = 1200
    PREVIEW_TIMEOUT_SECONDS = 1800
    PREVIEW_RUN_SECONDS = 600         # one sweeper run stops starting new files after this long

    # --- Maps (app/static/map.js): OpenStreetMap raster tiles fetched by the browser. Swap the URL for a
    # self-hosted or another provider's tile server if you want; {z}/{x}/{y} are filled in. Keep the attribution. ---
    MAP_TILE_URL = os.environ.get("MAP_TILE_URL", "https://tile.openstreetmap.org/{z}/{x}/{y}.png")
    MAP_ATTRIBUTION = os.environ.get("MAP_ATTRIBUTION", "\u00a9 OpenStreetMap contributors")
    MAP_MAX_ZOOM = int(os.environ.get("MAP_MAX_ZOOM", 19))
    MAP_MAX_PINS = 5000

    # --- Swappable services (jobs/providers.py; PLAN 20): which implementation serves each role. A registered
    # name, or "none" to switch the feature off. Only the current setup is implemented. ---
    LOCATION_PROVIDER = os.environ.get("LOCATION_PROVIDER", "dawarich")
    GEOCODER_PROVIDER = os.environ.get("GEOCODER_PROVIDER", "photon")
    PHOTO_PROVIDER = os.environ.get("PHOTO_PROVIDER", "immich")

    # --- Place names (jobs/photon.py): a self-hosted Photon geocoder (no key needed, nothing leaves the LAN).
    # Editable on the Settings page. Photon answers on plain HTTP port 2322 here. ---
    PHOTON_API_URL = os.environ.get("PHOTON_API_URL", "http://photon.home.zamia.co.uk:2322")
    PLACE_LANG = "en"
    PLACE_HOME_COUNTRY = "GB"              # a country name is added to the label only when it is elsewhere
    PLACE_FALLBACK_RADIUS_KM = 5           # nothing within Photon's default ~1 km -> "near <nearest thing within this>"
    PLACE_BATCH = 100                      # locations named per sweeper run
    PLACE_MOVED_METRES = 50                # a location that moves less than this keeps its place name

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
