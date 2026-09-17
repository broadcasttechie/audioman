# Audio Recording Manager — Build Plan

Single-user, self-hosted server for managing a growing audio recording
collection (ambient, theatre/event, personal/project voice). Runs in a
Proxmox LXC. Google Drive is the field-upload inbox *and* a permanent
mirror of the organized library; the NAS is the source of truth.

## 1. Storage model

- **Drive `/Inbox`** — one-way, Drive → server. Field uploads land here.
  Files are *moved* out once safely pulled (never left duplicated).
- **NAS canonical tree** — source of truth. All metadata edits, moves,
  and re-filing happen here first.
- **Drive `/Library`** — one-way mirror, NAS → Drive, kept in sync on a
  schedule. Read-only in practice; never edited directly in Drive.

Nothing is ever deleted from Drive by this system — Drive is explicitly
the safety-net copy. NAS is where re-tagging/re-filing actually happens.

## 2. Folder pattern (Pattern C, configurable)

Rendered from DB fields via a template, never the source of truth itself:

- `{project}/{filename}` — when a project is set (matches how theatre
  work is actually organized: one folder per show).
- `misc/{category}/{year}/{filename}` — fallback when no project is set
  (ambient / personal voice recordings).

Template lives in config, not hardcoded, so the pattern can change later.
Changing it does **not** retroactively move files — use the `refile-all`
maintenance task for that (see §5).

## 3. Data model

```
resources
  id, checksum, filename, format, duration_seconds
  captured_at, captured_at_source        # embedded | dawarich-inferred | manual
  category                               # ambient | event | voice-personal | voice-project | NULL until reviewed
  project_id        -> projects.id       # nullable
  status                                 # pending-review | filed | archived | failed
  failure_stage, failure_detail          # only set when status == failed
  nas_path                               # set once filed
  staging_path                           # set on ingest, cleared once filed
  drive_inbox_path                       # nullable once moved out of Inbox

projects
  id, name, slug, notes

locations
  id, resource_id -> resources.id
  lat, lon
  source                                 # dawarich-auto | manual | none

tags
  id, name

resource_tags                            # many-to-many
  resource_id, tag_id

file_events                              # audit log
  id, resource_id (nullable), event_type, detail, occurred_at
  # event_type: ingested | moved | refiled | drive-synced | failed | verified

pending_uploads                          # scratch table for stability check
  path, size, modtime, first_seen_at
```

Category is a **fixed enum**, not a tag — it drives the `misc/<category>/`
fallback path and needs to stay predictable. Everything else (venue,
mic used, weather, etc.) is a free-form tag.

## 4. Ingest pipeline — implemented in `jobs/ingest.py`

1. `drive-inbox-pull` job lists `/Inbox` via `rclone lsjson`, compares
   size+modtime against the previous poll (`pending_uploads`). A file is
   only eligible once it's been **unchanged across two consecutive
   polls** *and* older than `MIN_AGE_MINUTES` (protects against Drive
   showing a "final" size before the upload has actually settled).
2. Eligible files: `rclone move` from Drive `/Inbox` → local staging dir.
3. `jobs.ingest.ingest_staged_file()` takes over: sha256 checksum
   (dedupe check — a match quarantines the incoming file under
   `STAGING_DIR/duplicates` rather than guessing whether to discard
   it), then `ffprobe` for format/duration.
4. Timestamp: only a fixed set of embedded metadata tags count as
   trustworthy (`DateTimeOriginal`, `CreationDate`, `MediaCreateDate`,
   `EncodedDate`, `OriginationDate` — the last being WAV's
   broadcast-wave field, likely what your recorders write). Filesystem
   mtime is deliberately never used as a fallback, since
   uploads/moves reset it unreliably — a wrong timestamp would
   silently mis-locate a recording via Dawarich, worse than no
   timestamp. No trusted tag found → `captured_at` stays `None`,
   `captured_at_source: manual`, and neither enrichment job (§12
   below) has anything to search a window around.
5. Resource lands as `status: pending-review`, `category: None`
   (not inferable from the file — always a human review step),
   `dawarich_checked_at`/`immich_checked_at` both `NULL` (queued —
   see §12), file itself still sitting at `staging_path`. Ingest
   calls NEITHER Dawarich NOR Immich — see §12 for why that's
   deliberate.
6. On review (`PATCH /api/resources/<id>`, later the UI): set/edit
   category, project, tags, datetime, location. Setting `status:
   filed` (category required first) moves the file from
   `staging_path` to the rendered NAS canonical path, sets `nas_path`,
   clears `staging_path`, logs a `file_events` row.
7. `nas-to-drive-library` job mirrors the NAS tree → Drive `/Library`
   on its own schedule (`rclone sync`, source of truth = NAS).
8. Independently of all the above, `enrich-locations`/`enrich-photos`
   (§12) run every 15 minutes and fill in location/track/photo data
   for anything not yet successfully checked — this happens whether
   the resource is still `pending-review` or already `filed`.

## 5. Maintenance tasks

All available as both a scheduled (nightly, low-priority) job **and** a
manual "run now" API call. Dawarich/Immich backfill is NOT here — see
§12, since those are a continuous queue, not an occasional sweep:

- `refile-all` — re-render every resource's path from the current
  template, move any mismatches, log to `file_events`, trigger a
  Drive resync.
- `verify-integrity` — re-checksum NAS files, flag drift against stored
  checksums.
- `find-orphans` — files on NAS/Drive-Library with no matching
  `resources` row, or vice versa.
- `retry-failed` — reprocess only resources in `status: failed`
  (checksum/metadata-extraction/move failures — never Dawarich/Immich,
  see §12).
- `library-verify` — `rclone check` NAS vs Drive `/Library`, no
  transfer, just drift detection feeding `find-orphans`.

## 6. rclone jobs

| Job | Direction | Mode | Frequency |
|---|---|---|---|
| `drive-inbox-pull` | Drive `/Inbox` → local staging | move | frequent (15–60 min) |
| `nas-to-drive-library` | NAS → Drive `/Library` | sync | less frequent (hourly / after filing batches) |
| `library-verify` | NAS ↔ Drive `/Library` | check only | nightly |

Each job tracks: last-run timestamp, last-run status, log tail — surfaced
via API (and later UI) so a missed run is visible at a glance.

## 7. API-first, UI-last

Backend is a set of internal REST endpoints from the start:

- `/api/resources` (list/filter/get/update) — the review queue is just a
  filtered list view over this.
- `/api/jobs/<name>/run` — manual trigger for any rclone or maintenance
  job (same code path the scheduler calls).
- `/api/jobs/<name>/status` — last run info.
- `/api/projects`, `/api/tags` — simple CRUD.

UI (Flask + Jinja + htmx) is built entirely on top of this API, once the
backend is solid — no UI-specific business logic.

## 8. Open decisions

Settled:

- **Scheduler: systemd timers**, not APScheduler/cron. Each job is a
  oneshot `.service` that curls its own `/api/jobs/<name>/run`
  endpoint; a matching `.timer` schedules it. Templates in
  `deploy/systemd/`. Rationale: job liveness isn't tied to the Flask
  process staying up, and `journalctl` gives free logging/history
  per job. Manual "run now" hits the exact same endpoint, so a
  scheduled and a manual run are indistinguishable to the app.
- **Dawarich integration**: `GET /api/v1/points`, paginated,
  filterable by date range, auth via `api_key` query param (NOT a
  Bearer header — confirmed from Dawarich's docs, corrected from the
  original stub). No single "nearest point to a timestamp" endpoint
  exists, so `jobs/dawarich.py` fetches a ±30 min window around the
  audio's `captured_at` and picks the closest point client-side.
  ⚠️ Exact date-range param names (`start_at`/`end_at`) are the
  documented convention but weren't confirmed from a static fetch of
  Dawarich's docs (their param table renders via JS) — verify against
  your instance's own Swagger/OpenAPI page once it's running.
- **`failure_stage` added to `Resource`** (`checksum` |
  `metadata-extraction` | `move` — Dawarich/Immich lookups are never
  among these, see §12), so `retry_failed` resumes from the right
  step instead of restarting ingest.
- **Ingest pipeline built** (`jobs/ingest.py`) — checksum, `ffprobe`,
  trusted-tag timestamp check, all local — no external calls at all
  (moved out to §12's enrichment jobs). `retry_failed` handles `move`
  failures; `checksum`/`metadata-extraction` failures are left
  genuinely still-failing (the file itself needs re-inspection, not
  just a retried API call).
- **Filing wired end-to-end**: `PATCH /api/resources/<id>` with
  `status: filed` now actually renders the path and moves the file
  from `staging_path` to the NAS, rather than just updating the DB row.

Still open:

- [ ] Review-queue UI wireframe — not yet designed.
- [ ] Exact rclone remote config (Drive OAuth client, NAS mount type —
      NFS/SMB into the LXC).
- [ ] Config format: `.env` + `config.py`, or a small YAML — TBD.
- [ ] Duplicate-file policy: `jobs/ingest.py` currently quarantines a
      re-uploaded duplicate under `STAGING_DIR/duplicates` and logs it,
      rather than deleting it — a real decision, not a placeholder to
      treat as settled. Revisit once you've actually hit this case.
- [ ] `ffprobe`/`exiftool` must be installed on the LXC (not Python
      packages — system binaries, add to the Proxmox provisioning
      step, not `requirements.txt`).
- [ ] `deploy/systemd/audio-manager-worker.service` must actually be
      enabled on the LXC (`systemctl enable --now`) for anything to
      get processed at all — every job now depends on this one daemon
      running, so it's worth a startup check/alert if it ever dies
      and doesn't restart cleanly (§12).

## 9. Stack

- Python 3 / Flask (app factory pattern)
- SQLAlchemy + Postgres
- rclone (external process, invoked via subprocess or shelled-out jobs)
- ffprobe / exiftool (external, via subprocess — system binaries, not
  Python packages; must be installed on the LXC itself)
- Proxmox LXC, NFS/SMB mount for NAS

## 10. See also

`README.md` for local dev setup and a live-updated summary of what's
wired up vs. still stubbed.

## 11. Map + waveform player (advanced feature)

For field recordings: an interactive map of the GPS path walked during
the recording, clickable to seek the audio player to that point in
time — plus a full waveform browser with sub-clipping.

**Data model additions:**
- `track_points` — the full GPS path per resource, cached from
  Dawarich at ingest time (one fetch spanning `captured_at` →
  `captured_at + duration_seconds`, padded ±2 min). This is separate
  from `locations`, which stays a single pinned point (map marker /
  manual override) — `locations.lat/lon` is just the track point
  closest to `captured_at`, derived from the same fetch so it's one
  Dawarich call, not two. A resource with no track (stationary
  recording, or Dawarich had nothing for that window) just shows the
  pin with no path.
- `clips` — sub-clip markers (start/end seconds, label, notes) against
  a resource. **Metadata only** until explicitly exported — marking a
  moment and producing a standalone file are different actions.
  Export renders via `ffmpeg` stream-copy (instant, sample-accurate
  for WAV; revisit if you sub-clip compressed formats often, since a
  copy-cut can land slightly off-frame there).

**New API:**
- `GET /api/resources/<id>/track` — path points with `offset_seconds`
  pre-computed relative to `captured_at`, so the frontend just calls
  `player.setTime(offset_seconds)` on a map-point click with no time
  math of its own.
- `GET /api/resources/<id>/audio` — streams the file (Range-request
  aware via Flask's `conditional=True`), the source URL for the
  waveform player.
- `GET/POST /api/resources/<id>/clips`, `PATCH`/`DELETE
  /api/clips/<id>`, `POST /api/clips/<id>/export`.

**Frontend (not yet built — part of the still-pending review UI):**
- **wavesurfer.js** for the waveform browser — its Regions plugin
  covers sub-clip drag-to-select/resize natively; a region's
  create/update/remove events map directly onto the clip API above.
- **Leaflet** for the map (no API key needed, unlike Google Maps) —
  a polyline from `track_points` plus a marker per point; a marker
  click calls the player's seek with that point's `offset_seconds`.
- These two need to share state (current playback position, hover
  sync between map and waveform) — straightforward with plain JS
  event listeners once both libraries are loaded; no framework needed
  given this is one page.

## 12. Reliability: no dependency on external services staying up

The core requirement: ingest must keep working regardless of whether
Dawarich or Immich are reachable, an outage of any length must
self-heal with zero manual intervention, nothing external-facing may
hang the UI, and no loop may run unbounded.

**Ingest is now fully decoupled from external services.** `jobs/ingest.py`
calls neither Dawarich nor Immich — it only ever does local work
(checksum, `ffprobe`, `exiftool`). A resource always reaches
`pending-review` regardless of what's up or down elsewhere.

**Enrichment is a queue, not a step in ingest.** `Resource` carries
`dawarich_checked_at` / `immich_checked_at` — NULL means "not yet
successfully checked", which is what makes the "offline for a day"
case self-healing: nothing is marked done until a check actually
succeeds (even a success that finds no data still counts as checked),
so a resource with `dawarich_checked_at IS NULL` just sits there as
queued work until some future run of `enrich-locations` finds
Dawarich reachable again. Two new jobs (`jobs/enrich.py`) process this
queue every 15 minutes: `enrich-locations`, `enrich-photos`. A DB
column doing queue duty is deliberately the whole "queue" here — a
message broker (Celery+Redis, RabbitMQ) would be real infrastructure
this single-user LXC doesn't need.

**Every external call is bounded.** `jobs/retry.py`:
- `call_with_retry` — up to `EXTERNAL_MAX_ATTEMPTS` (3) with
  exponential backoff + jitter, never retries 4xx (a bad API key
  retrying won't fix itself), raises `ServiceUnavailable` once
  exhausted.
- `CircuitBreaker` — tracks consecutive failures *within one batch
  job run only*. After `EXTERNAL_CIRCUIT_THRESHOLD` (3) in a row, the
  job stops processing the rest of its candidates for this run rather
  than hammering an already-down service once per resource — the next
  scheduled run starts a fresh breaker and just continues the queue.

**Every subprocess call has a timeout** (`rclone`, `ffprobe`,
`exiftool`, `ffmpeg`) — see `Config.RCLONE_*_TIMEOUT_SECONDS`,
`PROBE_TIMEOUT_SECONDS`, `FFMPEG_TIMEOUT_SECONDS`. None of these could
previously hang indefinitely; now all are bounded and a timeout is
caught the same way as any other failure.

**Per-file isolation in `drive_inbox_pull`.** One file's `rclone move`
timing out or failing no longer aborts the rest of the batch — it's
logged, left in place, and retried next run. A catch-all around the
ingest call itself means a bug in ingest can never leave an orphaned
file sitting in `STAGING_DIR` with no `Resource` row and no record of
what happened.

**"Run now" no longer blocks the request that triggers it — and now
via a real persisted queue, not a thread in the web process.**
`POST /api/jobs/<name>/run` (`app/api.py`) enqueues a `JobQueueItem`
row and returns `202` immediately; a separate standalone worker
process (`jobs/worker.py`, its own systemd service, NOT sharing
threads with the Flask process) claims and executes it. Claiming uses
Postgres's `SELECT ... FOR UPDATE SKIP LOCKED` (`jobs/queue.py`) —
the standard atomic-claim pattern for a DB-backed queue, no broker
needed. A web process restart can no longer kill an in-flight job,
since execution happens entirely in the separate worker process.
Job-level failures (an unhandled exception in a job function, as
opposed to the external-call-level retries in `jobs/retry.py`) get
their own bounded retry: `JOB_QUEUE_MAX_ATTEMPTS` (3) with backoff
stored in `next_attempt_at` on the row itself, so the delay survives
a worker restart — after that it's `failed-permanently`, visible via
`/api/jobs/<name>/status`, never retried again automatically.

Why a DB-backed queue over an MQTT-type solution: MQTT is pub/sub for
events, not built for exactly-once task claiming or persisted retry
state — you'd have to build queue semantics on top of it regardless.
Routing job execution through a broker also means the broker being
down affects core job execution, which would be exactly the kind of
external dependency this whole section exists to eliminate. Postgres
is already the app's database; `FOR UPDATE SKIP LOCKED` is a
production-proven pattern (used inside real queue libraries) requiring
zero new infrastructure. ⚠️ SKIP LOCKED is Postgres-specific —
`jobs/queue.py` has a non-concurrent-safe SQLite fallback for local
dev only; never run two worker processes against a SQLite-backed
instance.

Existing scheduled jobs (`deploy/systemd/*.timer`) needed NO changes
for this — they still just curl the same `/run` endpoint on their
existing schedule; that endpoint enqueues instead of running inline
either way. Only one new unit was added:
`deploy/systemd/audio-manager-worker.service` (`Type=simple`,
long-running, `Restart=on-failure` — the actual queue consumer).

**On-demand endpoints fail fast instead of retrying like a batch job.**
`refresh_photos` and the Immich thumbnail/original proxy run inside a
live user request, so they use `max_attempts=1` / no retry and a short
timeout — a quick, clear failure beats a slow one when the UI is
actually waiting on the response.

**What this doesn't cover** (noting rather than solving silently):
- A stale/hung NFS mount for the NAS itself is an OS-level concern,
  not something the app can time-box from inside Python. Worth setting
  `soft,timeo=...` NFS mount options at the Proxmox/LXC level rather
  than relying on app-level handling.
- `retry_failed`'s `checksum`/`metadata-extraction` failures still
  need a human to look at the file — those aren't retryable
  automatically, since the file itself may be the problem.
- The worker is a single process (one job at a time, sequential) —
  correct call for single-user volume, but if the queue ever backs up
  meaningfully, running two worker processes is safe to do (SKIP
  LOCKED guarantees no double-claim) without any code changes.

## 13. Immich companion photos

For a resource with a known `captured_at`, surface photos taken around
the same time from Immich, without copying them into this system.

- **`resource_photos`**: links a resource to an Immich asset id +
  `taken_at`. No image data stored locally — everything is proxied
  through Immich at render time (`GET /api/photos/<id>/thumbnail` and
  `/original` in `app/api.py`), so the browser never needs its own
  Immich API key.
- **Lookup window**: whole recording duration + 5 min padding either
  side (`jobs/immich.py PHOTO_PADDING`) — same idea as Dawarich's
  track padding, since photos might be taken any time during a
  session, not just at the start.
- **Deliberately best-effort**, unlike the Dawarich lookup: a failed
  or empty Immich search does NOT fail the resource. A missing
  companion photo is a non-event; a missing location was treated as
  more consequential earlier in this plan.
- **API confirmed against Immich's documented (older-stable) shape**:
  `POST /api/search/metadata` with `takenAfter`/`takenBefore`/`type`,
  auth via `x-api-key` HEADER (not a query param — don't confuse with
  Dawarich's convention). ⚠️ Immich's search/asset-serving API has
  shifted across versions (a newer search v2, deprecated routes) —
  confirm `/api/search/metadata` and `/api/assets/<id>/thumbnail`
  `|original` are still current on your instance before relying on
  this; `jobs/immich.py` defensively handles two known response
  shapes for the search call, but the asset-serving paths in
  `app/api.py` are not defensively coded — a 404 there is your first
  signal to check.
- `enrich-photos` (§12) backfills photos for resources filed before
  Immich was configured, or before matching photos were imported into
  Immich — running every 15 minutes as part of the self-healing queue,
  not a one-off nightly sweep.
## 14. Multiple workers + end-to-end review

Scoped multi-worker support and a full review pass, both verified with
actual functional tests (not just syntax checks) — each finding below
was reproduced and then confirmed fixed.

**Multi-worker scoping** — running more than one `jobs/worker.py`
instance is now genuinely safe, not just theoretically so:
- `deploy/systemd/audio-manager-worker@.service` is a systemd
  **template** unit — `systemctl enable --now audio-manager-worker@1
  audio-manager-worker@2` runs two independent instances with zero
  code changes; each identifies itself as `<hostname>:<pid>`.
- **Fixed a real race** in `jobs/queue.enqueue()`: the
  check-then-insert dedupe was TOCTOU-racy — two near-simultaneous
  enqueues (concurrent "run now" clicks, or a timer firing while
  already queued) could both pass the check and insert duplicate
  active rows. Closed at the DB level with a partial unique index on
  `job_queue(job_name) WHERE status IN ('queued','running')`
  (`app/models.py`), with `enqueue()` catching the resulting
  `IntegrityError` gracefully rather than erroring.
- **Added `reap_stale_jobs()`** (`jobs/queue.py`): previously, a
  worker killed mid-job (OOM, crash, `systemctl kill`) left its row
  stuck at `status='running'` forever — nothing else ever looked at
  running rows, only queued ones, so it would never be retried. Now
  any row `running` longer than `JOB_QUEUE_STALE_RUNNING_TIMEOUT_SECONDS`
  (1 hour — comfortably above the longest expected job) is treated as
  a failure and routed through the normal bounded retry path. Called
  every worker loop iteration; verified with a functional test
  (simulated stale row → reaped and requeued; simulated fresh row →
  correctly left alone).
- **`SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}`** — the
  worker is a long-lived process (days/weeks); without this, a
  Postgres restart or a firewall dropping an idle connection would
  surface as a confusing `OperationalError` on whatever query
  happened to run next, rather than transparently reconnecting.
- Worker loop now has an outer try/except around claim/reap
  themselves (not just around job execution) — a transient DB hiccup
  logs and backs off for one poll interval rather than crashing the
  whole process (which `Restart=on-failure` would recover from
  anyway, just more slowly and noisily).

**Real bug found and fixed** — `jobs/rclone_jobs.py`
`drive_inbox_pull`'s stability tracking: when a file's size/modtime
*changed* between polls, `first_seen_at` incorrectly carried forward
from the file's very first-ever observation instead of resetting.
Practical effect: a file that grew for two hours before finally
stabilizing could get pulled the moment it stabilized, having
satisfied `MIN_AGE_MINUTES` against a two-hour-old timestamp rather
than the actual moment of stability — directly undermining the
"ensure the upload has completed" requirement this check exists for.
Fixed (`first_seen_at` now only preserved when unchanged, reset to
`now` otherwise) and confirmed with a regression test reproducing the
exact growing-then-stabilizing sequence.

**Regression found and fixed** — the previous rewrite of `worker.py`
(moving from in-process threads to the DB queue) dropped the
last-resort `JobRun` update on an unhandled job exception. Practical
effect: `/api/jobs/<name>/status`'s `last_result` would keep showing a
stale success while a crashing job silently retried in the
background — the failure was recorded in `job_queue` but invisible
anywhere the API surfaces. Restored as `_record_crash_in_job_run()`.

**Other fixes from the review pass:**
- `POST /api/resources/<id>/clips` now rejects `end_seconds` beyond
  the resource's known `duration_seconds`.
- `GET /api/resources` is now paginated (`limit`/`offset`, capped at
  200) — this is explicitly a growing collection; an unbounded list
  endpoint doesn't fit that for long.
- Failed resources now include `failure_stage`/`failure_detail` in
  their API representation (only when `status: failed`, not on every
  row) — needed for a UI to explain *why* something failed.
- `POST /api/projects` returns a clean `400` on a duplicate slug
  instead of an unhandled `IntegrityError` → `500`.
- **Added `POST /api/resources/<id>/location/refresh`**, mirroring
  the existing photos-refresh endpoint: `enrich_locations` only ever
  looks at `dawarich_checked_at IS NULL`, so a resource already
  checked (Dawarich reachable, genuinely had nothing at the time) is
  never automatically re-tried — this covers the case where you
  later backfill older location history into Dawarich and want one
  specific resource re-checked.

## 15. Export / format-conversion workflow

Same "request is a fast DB write, execution happens elsewhere" shape
as the rest of this app — `POST .../export` never runs `ffmpeg`
inline, since a format conversion can take real time regardless of
file size.

- **`Export`** (`app/models.py`) — `format` (`original` | `wav` |
  `mp3` | `flac`), optional `clip_id` (time-window export) or none
  (whole resource), `quality` (mp3 bitrate), `embed_metadata`.
  `original` REQUIRES a `clip_id` — exporting a whole resource
  unchanged is just the file itself, so that combination is rejected
  at the API rather than silently doing nothing useful.
- **`process_exports`** (`jobs/export.py`) — a named job like any
  other, run by the queue worker on a 2-minute timer (frequent, since
  someone's often actively waiting to download the result). Handles
  the `-ss`/`-to` window (if a clip), the codec args per format, and
  metadata embedding (`title`/`date`/`comment` via ffmpeg's
  `-metadata`) — works well for FLAC/MP3's real tag containers; WAV's
  tag support via ffmpeg is the INFO chunk only, less universally read.
- **`Clip.exported_path` removed** — it duplicated state that now
  lives properly in `Export` (a clip can be exported more than once,
  in different formats; the old single field couldn't represent that).
- **`POST /api/clips/<id>/export`** kept as a shorthand for the most
  common case (stream-copy, original format) — the "cut this bit out"
  button. `POST /api/resources/<id>/export` is the general form for
  format conversion, with or without a clip window.
- `GET /api/resources/<id>/exports` (history), `GET
  /api/exports/<id>` (status), `GET /api/exports/<id>/download`.

## 16. Direct upload — groundwork for a companion mobile app

You mentioned a possible Android app to handle uploads straight from a
field mic. Built now, regardless of whether that app happens: **`POST
/api/ingest/upload`** — reuses the exact same `ingest_staged_file()`
pipeline `drive_inbox_pull` uses, so a directly-uploaded file gets
identical checksum/dedupe/metadata/enrichment-queueing behavior to one
that came via Drive. Verified end-to-end: correct-key upload of bad
audio data lands as a proper `failed` resource with
`failure_stage`/`failure_detail` — the existing error handling needed
zero special-casing for this new path.

Runs **synchronously** in the request, unlike Drive's polling pull —
deliberately: an HTTP multipart body is already fully received by the
time the view function runs, so there's no "is it still uploading?"
ambiguity to wait out (that stability check exists specifically for
polling a folder where "appeared" and "finished" are different
moments; here they coincide by construction). Ingest itself is fast
(local checksum + probe only), so this doesn't meaningfully block.

**Gated by `UPLOAD_API_KEY`** (a shared-secret header,
`X-Upload-Key`) — unlike the rest of this API, which assumes LAN/VPN
access, this is the one endpoint a phone on mobile data would need to
reach from the open internet, so it gets its own auth rather than
relying on network trust. Empty key = endpoint disabled (fails closed).

**Access decision (2026-09): VPN-only for now, via the existing UniFi
VPN — not a new dependency, already in place.** The phone joins that
VPN before talking to any endpoint, same as any other device reaching
this server. This applies to the whole API, not just upload: nothing
here is designed to be safe on the open internet as-is (no per-route
auth, no TLS termination assumed, no rate limiting). External access
is explicitly a "maybe later" to revisit, not ruled out — so nothing
above should be built in a way that would need ripping out to get
there. Concretely, that means keeping in mind, going forward:
- Auth stays addable per-route (a decorator/middleware layer), not
  something structurally baked into "requests only ever arrive from a
  trusted network" — `UPLOAD_API_KEY` is already the right *shape* for
  this (a checkable header, not an IP allowlist); the same shape would
  extend to other routes if/when they need it.
- No IP-based trust checks anywhere (there currently are none) — those
  are exactly the kind of thing that quietly breaks when the network
  boundary changes, and are also the first thing to accidentally miss
  cleaning up later.
- If/when external access is revisited: real per-request auth (not a
  single shared secret) and TLS become non-optional at that point,
  not before — VPN-only genuinely doesn't need either. Tailscale was
  suggested earlier in this design as a lighter-weight alternative for
  reaching the API from outside without exposing a public port at
  all; worth reconsidering against the UniFi VPN then, not a decision
  needed now.

**Not yet addressed, worth deciding before relying on this for real
uploads:**
- `MAX_CONTENT_LENGTH` isn't set on the Flask app — an unbounded
  upload size is fine for testing, not for production; set it once
  you know your realistic max recording length/bit depth.
- If this ever sits behind a reverse proxy (nginx, Caddy), that proxy
  needs its own body-size limit and timeout raised to match — a proxy
  default (often 1MB, 60s) would reject a real field recording upload
  before it reaches Flask at all.
- No resumable/chunked upload — a dropped connection on a large
  upload over mobile data means starting over. Fine for short
  recordings; worth revisiting if long-form (multi-hour) recordings
  are a common case for the app.

### What the Android app itself would need (not built — a proposal)

- **Recording**: Android treats a USB Audio Class device (the Rode
  AI-Micro) as a standard input once permission is granted — no
  special driver needed, just `AudioRecord`/`MediaRecorder` pointed at
  it like any mic.
- **Timestamp at the source**: embedding the phone's own accurate
  clock time into the recording the moment it's made would solve the
  "many files have wrong timestamps" problem more directly than the
  post-hoc `exiftool` check in `jobs/ingest.py` — worth having both,
  since not every recording will come from this app.
- **Optional device GPS as a fallback**: the phone has its own GPS;
  sending a coordinate alongside the upload would give an immediate,
  high-confidence location without waiting on Dawarich's enrichment
  queue — most useful as a fallback for whatever gap exists between
  "recorded" and "Dawarich's track for that exact moment is queryable."
- **Background upload with retry**: Android's WorkManager is the
  natural fit — durable, survives app kill/reboot, retries with
  backoff on failure — the same shape as everything built server-side
  in this conversation, just on the client end. A failed upload should
  never delete the local recording until the server confirms receipt.
  With VPN-only access (see above), "server unreachable" needs to be
  one of the ordinary retry cases WorkManager handles — the device
  won't always be connected to the UniFi VPN when a recording
  finishes, and that's expected, not an error state.
- This is a real second project (Kotlin, Android Studio, its own
  release/signing concerns) — say if you want to actually scope and
  start that, rather than just the server-side groundwork done here.

