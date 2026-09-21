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

> **Being reversed in part — see §17.** Categories are to become
> configurable at the user's request; the "predictable path" concern
> above is what §17's design has to preserve.

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

## 17. Planned (noted, not built): configurable categories + a manage page

Two related requests from the UI work. Nothing here is implemented; this
records what was asked and what it touches so it isn't re-derived later.

### 17.1 Configurable categories

**Asked for**: categories editable by the user, not a fixed list.
This reverses part of §3 ("fixed enum"), whose reason was path
predictability — `misc/<category>/<year>/` is rendered from the category
string, so a careless rename or delete silently changes where files
belong.

**User's stated intent**: the right categories aren't known yet, and there
is real overlap with tags — but categories must stay a separate concept,
because they're the *high-level sort* (the folder tree) while tags are
descriptive detail. So: start with the four defaults, expect early
renaming/merging as real use shows what's wanted, and keep category and tag
distinct. A working test for which is which: if you'd ever want to *browse*
it as a folder, it's a category (one value, mutually exclusive); if you'd
want to *filter across* it, it's a tag. Because experimentation will
produce overlaps, **merging two categories** (reassign resources, then
refile) belongs on the manage page alongside tag merge, not just
rename/archive.

**Everything that currently assumes the fixed list**
- `config.py` `CATEGORIES` — the source of truth today.
- `app/api.py` `update_resource` — validates against `Config.CATEGORIES`.
- `app/templates/resource_detail.html` — a *second, hardcoded copy* of the
  same list in JS (it should fetch from the API instead; this duplicate
  would drift the moment categories become editable).
- `jobs/path_template.py` — category string becomes the folder name.
- `jobs/export.py` — category goes into exported metadata.
- `Resource.category` — plain string, no FK.

**Suggested shape (open — not decided)**: a `categories` table
(`slug`, `label`, `sort_order`, `archived`) with `Resource.category`
continuing to store the slug, so the path template and existing rows need
no migration. `Config.CATEGORIES` becomes seed data only. The label is
freely editable; the slug is the path segment, so changing it is a
*refile* operation (re-render paths via `refile-all`, log to
`file_events`), not a plain edit. "Delete" should be **archive** — hidden
from the picker but still valid on resources that already use it — with a
hard delete only when zero resources reference it, so a category can
never dangle.

### 17.2 Manage page (tags / projects / categories)

**Asked for**: an admin page for tag and project management — merge,
correction (rename), removal, "etc." Reachable from Settings rather than
a fourth bottom tab, to keep mobile nav at three items (open — say if you
want it as its own tab). Categories from 17.1 would live on the same page.

**Missing API** — only `GET`/`POST` exist today for tags and projects:
- Tags: `PATCH` (rename), `DELETE`, `POST .../merge` (repoint
  `resource_tags` from the source tag(s) to a target, skipping resources
  that already carry the target, then delete the sources). A rename that
  collides with an existing name (`Tag.name` is unique) should offer a
  merge rather than just 400.
- Projects: `PATCH` (name/slug/notes), `DELETE`, merge. **A project's slug
  is a NAS folder name** (`{project}/{filename}`), so a slug change or
  merge moves files — same refile-and-log treatment as a category slug
  change, not a metadata-only edit. Deleting a project with resources
  needs an explicit reassign-or-unassign choice.
- List endpoints should return usage counts (resources per tag/project/
  category) — every destructive action wants to show "affects N
  recordings" first, and unused/near-duplicate tags are what make
  cleanup findable.

**Things this exposes**
- `update_resource` does `Tag.query.get(tag_id)` per id and would put
  `None` into `r.tags` for an ID that no longer exists. Harmless today
  (tags can't be deleted); once merge/delete exist, an open review page
  holding a stale tag ID hits it. Should 400 on an unknown ID.
- Merges/renames/removals that change what's on disk or on many
  resources at once should write `file_events` rows, same as filing does.
- Everything above is single-user reference data, so no auth beyond the
  existing VPN-only stance (§16) — but destructive actions still want a
  confirm step, and merge/delete are hard to undo without a backup.

### 17.3 Location: refresh button after the timestamp is fixed

**Asked for**: on the resource detail screen, a button to re-run the
location lookup once the timestamp has been corrected. UI-only in the
simple case — `POST /api/resources/<id>/location/refresh` already exists
(§14) and returns `{found, track_points}`, enough for "found a location" /
"Dawarich had nothing for that window" feedback. It 400s without
`captured_at`, so the button should be disabled with a hint until one is
set. Photos depend on `captured_at` the same way (`/photos/refresh`), so
one "refresh from Dawarich/Immich" action probably covers both.

**Why a button is needed (checked in `jobs/enrich.py`)**
- *Timestamp was missing, then set*: `enrich_locations` only picks up
  `captured_at IS NOT NULL AND dawarich_checked_at IS NULL`, and a
  resource with no timestamp is never checked — so it would be picked up
  automatically on the next 15-minute run. The button here is just
  immediacy and feedback.
- *Timestamp was wrong, then corrected*: `dawarich_checked_at` is already
  set from the first lookup, so the automatic queue **never** retries it.
  This is the case the button is really for. Cleaner alternative or
  complement: changing `captured_at` via `PATCH` should reset
  `dawarich_checked_at`/`immich_checked_at` to NULL so the normal queue
  redoes it without anyone remembering to press anything.

**Two problems in the refresh path to fix alongside** (both apply to the
endpoint in `app/api.py`, and it's the one the button would call):
- It **appends** `TrackPoint` rows without deleting existing ones, so
  refreshing after correcting a timestamp leaves the old (wrong-time)
  track in place next to the new one, and repeated presses duplicate
  points. It should replace the resource's track, not add to it.
- It overwrites `Location` unconditionally, including a location the user
  set by hand (`source: manual`) — a refresh would silently discard a
  manual override. It should leave `manual` alone (or ask).

### 17.4 Workflow order: when and where first, the rest inferred

**Asked for**: captured date and location come first in the review
workflow; from those, the rest can be worked out.

**Done**: `resource_detail.html` now orders sections Captured at →
Location → companion photos → Category → Project → Tags → Clips
(previously category/project/tags led). Presentation only.

**Not built — "worked out" means suggestions, not just ordering.** Possible
inputs, cheapest first:
- *History*: nearest already-filed recordings by place and time → suggest
  their project/category/tags. Single-user, so the user's own filing
  history is the whole model; a plain nearest-neighbour lookup, nothing
  fancier. Suggested values pre-fill for one-tap confirm, never auto-apply.
- *Immich photos* taken at that time/place as context (photo metadata may
  carry place names — unverified; §13's caveat about Immich's API shape
  applies).
- *Filename timestamp as a suggestion for `captured_at`.* The first real
  ingest (`audio_260917_091124_32bit_orig_stereo.wav`) has the time in its
  name and no trusted embedded tag, so it landed with no timestamp and
  therefore no location. §4 deliberately never uses filenames/mtime as a
  silent fallback (a wrong time mislocates a recording quietly). A *visible
  suggestion the user confirms* would respect the reason for that rule but
  bends its letter, so it needs an explicit OK. Timezone is also unknown
  (recorder-local vs UTC).

**Prerequisite, not optional**: setting `captured_at` doesn't currently
trigger a location lookup. Today you set the date and see "No location
yet" for up to 15 minutes (next `enrich-locations` run), and if the date
was *corrected* the automatic queue never re-runs at all. The §17.3 pieces
(reset `dawarich_checked_at`/`immich_checked_at` when `captured_at`
changes, replace rather than append the track, don't overwrite a manual
location, plus an immediate refresh with visible "looking up…" state) are
what make timestamp-first actually feel like a workflow.

**Open**: category is still required to file. If suggestions pre-fill it,
that's fine (confirm rather than choose); the earlier question of making it
optional when a project is set is unchanged.

### 17.5 Time correctness: timezone/DST and per-recorder clock offset

**Asked for**: read timestamps with the right summer-time/timezone offset,
and allow a precise per-recorder offset (e.g. a recorder known to run a
few minutes off).

**Checked against the running system (2026-09-18) — these are facts, not
guesses:**
- Dawarich's `timestamp` is an **integer epoch (UTC)**. `jobs/dawarich.py`
  calls `datetime.fromisoformat()` on it, which raises `TypeError` on the
  first real lookup. Dormant only because no resource has yet had a
  `captured_at`. **Not fixed** — the right fix depends on the contract
  below.
- Dawarich's `start_at`/`end_at` filters **work** (ISO with offset or epoch;
  13 of 13 returned points inside the window). This resolves the "best
  guess" caveat in §8/§12. The code sends *naive* `isoformat()` strings —
  they should be aware UTC. Immich queries in `jobs/immich.py` have the same
  naive-datetime habit.
- Ingest (`jobs/ingest.py` `_extract_timestamp`) keeps `raw[:19]`, which
  **drops any UTC offset** in the tag, and stores the recorder's wall-clock
  time as if it were UTC. So today a summer recording is an hour out
  against Dawarich's UTC epoch.
- The detail screen has a **round-trip bug**: it sends UTC, but the API
  returns a naive `isoformat()` with no `Z`, which the browser reads as
  local. Entering 10:00 (BST) reads back as 09:00 — reproduced in the
  browser (Europe/London). Not fixed yet, same reason.
- Also, `DAWARICH_API_URL` had been set to a guessed `http://…:3000`, which
  answers 400 (that port serves HTTPS). Corrected to
  `https://dawarich.home.zamia.co.uk` (certificate verifies). Recorded so
  the guess isn't reintroduced. Dawarich points also carry `city`,
  `country` and `geodata` — place names for §17.4's inference, free.

**Proposed contract**
- `captured_at` is **naive UTC in the DB, serialised with `Z`** — one
  meaning everywhere (API, UI, Dawarich/Immich queries).
- **Keep the original.** Store `captured_at_raw` (what the file said,
  unconverted), the recorder profile used, and the correction applied, so a
  changed offset or timezone can be re-applied without re-ingesting and
  nothing is ever lost to a bad correction.
- **Recorder profile** (new table): name; how to recognise it (a `device`
  capture from §17.6, or embedded `Model`/`Originator`/serial via
  exiftool); `timezone` as an **IANA name** (`Europe/London`) — the tz
  database is the actual answer to summer time, never a stored `+1`;
  `clock_offset_seconds` (signed, seconds precision); optional
  `effective_from`, since drift changes after battery swaps or resets;
  notes. Conversion: `utc = localise(wall_clock − offset, tz)`.
- **An offset in the tag wins**: when the embedded value carries one
  (`+01:00`), use it instead of the profile timezone — stop truncating.
- **DST edges**: the clock-back hour happens twice and the clock-forward
  hour never happens. Pick a value, set an "ambiguous" flag, and surface it
  for review rather than silently choosing.
- The UI edits in the browser's local time, stores UTC, and shows the raw
  value plus the applied correction so it's obvious why a time is what it is.
- The phone app (§16) can embed true UTC at source, which sidesteps all of
  this for recordings made with it.

### 17.6 Filename patterns (user-defined, grok-style)

**Asked for**: customisable patterns to extract data from filenames,
grok-style. **Recommended**: yes — a small grok layer that compiles to a
plain Python regex, ~30 lines and no new dependency. Raw named-group regex
stays allowed inside a pattern, so it's a superset of "just write a regex".
A bare `strptime` format was considered and rejected: it only handles the
date, and the value here is also sequence numbers, track numbers and device
names.

- **Syntax**: `%{NAME}` or `%{NAME:field}`, expanded recursively from a
  dictionary of named building blocks (`YY`, `MM`, `DD`, `HH`, `MI`, `SS`,
  `INT`, `WORD`, `DATE_YYMMDD`, `TIME_HHMMSS`, `DATA`, …). Users can add
  their own. Unanchored by default — the first real file arrived as
  `Copy of audio_260917_091124_…wav`, so patterns can't assume the name
  starts cleanly.
- **Reserved field names** (anything else captured is ignored):
  `year month day hour minute second` (2-digit year → 20xx), `seq`,
  `track`, `prefix` (the part siblings share), `device`, and `project` /
  `category` / `tag` as *hints* matched against existing slugs.
- **Storage**: a `filename_patterns` table (name, pattern, priority,
  enabled, sample filename), first enabled match wins. Extracted values go
  in a `filename_meta` JSON on the resource, so editing a pattern can be
  re-run over pending resources without re-ingesting.
- **Never silent** (consistent with §4 and §17.4): extracted values appear as
  suggestions to confirm, not as facts. A pattern can propose `captured_at`
  but that still needs the explicit OK noted in §17.4, and goes through the
  recorder-profile conversion in §17.5.
- **Editor + tester** on the manage page (§17.2): type or paste a name, see
  every extracted field and the resulting UTC time live, and check a
  pattern against the filenames already in the database to see the match
  rate. Compile errors caught at save; cap pattern length (Python's `re`
  has no timeout, but filenames are short).
- **Seed** one default pattern for the known recorder
  (`audio_%{YY}%{MM}%{DD}_%{HH}%{MI}%{SS}_…`). The other recorders' patterns
  come from real examples — which is what unblocks §17.7.

### 17.7 Split files and multitrack (recorded from discussion)

Two different things that both need "these files belong together":
- **Split** (a recorder starting a new file every ~30 minutes) should end up
  as **one file** — a real ffmpeg concat, non-destructive (originals kept and
  audit-logged, consistent with never silently discarding).
- **Multitrack** (simultaneous files, one per input) stays as **separate
  files** reviewed, categorised and filed as one unit. Not a DAW: no
  synced multi-track playback, just awareness.

Shared plumbing: a nullable `group_id` plus `group_type` (`split` |
`multitrack`) on `Resource`. Detection is automatic-but-confirmable — a
"these look like one recording, join?" suggestion, never an automatic merge:
contiguous `captured_at` + `seq` for splits (start of N+1 ≈ start of N +
duration), identical start + duration + shared `prefix` for multitrack.
Both are read off the §17.6 fields, so they're per-recorder patterns rather
than hardcoded heuristics — and the sample filenames still owed for both
recorders become the test cases for those patterns. Timestamps used for
adjacency must be corrected via §17.5 first, or clock offset makes
contiguous files look gapped.

### 17.8 Two pages, and type-dependent UI

**Stated**: the initial review page is essentially basic intake; the full
map / timeline browser belongs on a separate detailed page; and the map is
only relevant for certain kinds of recording, e.g. ambient.

**As built today** the queue is a flat list (`pending-review` = anything not
yet filed) and one screen does everything — date, place, cataloguing, clips,
export. Nothing distinguishes "needs its when/where sorted" from "ready to
catalogue", and there is no detailed page yet (the map and waveform from
§11 are still unbuilt).

**Proposed split**
- *Intake/review page* (the current `/review/<id>`, trimmed): when and where
  first (§17.4/§17.5), then category/project/tags, then File. Location here
  is a pin and place name, not a map browser.
- *Detail/explore page* (new): the §11 features — waveform and GPS
  map/timeline synced to each other, clips, export, companion photos.
  Reached from the intake page and from the Library.
- Whether clips/export move off the intake page is **open**; they need the
  waveform, which argues for the detail page.
- Stage could be *derived* rather than a new DB status (e.g. "needs
  when/where" = no `captured_at` or unconfirmed location; "ready to
  catalogue" = has both), shown as a badge/filter on the queue. A literal
  second queue is the alternative. **Open.**

**Type-dependent UI — the first place behaviour depends on category.**
Until now nothing branches on a category's value (§17.1). "Show the
map/timeline" should not be keyed on the *name* `ambient`, since names are
user-editable — it should be a **flag on the category record** (e.g.
`show_map`, or a small set of traits), so a renamed or new category still
behaves correctly. Category is then chosen at intake and decides whether the
detail page's map/timeline is offered prominently. Same flag could decide
whether the full GPS *track* is fetched at all versus just the pin (a voice
memo doesn't need a track; wasted Dawarich calls otherwise). **Open:** what
the traits are, and whether a category with no map flag still gets a pin.


## 18. Workflow and model redesign (agreed in discussion; nothing built yet)

Supersedes §17.8's "map flag on the category" and refines §3's flat
Resource-with-optional-Project shape. Decisions marked **Agreed** were
answered by the user; **Proposed** are still to be confirmed.

### 18.1 Model: Project → Session → File

- **Agreed — vocabulary:** the middle level is called a **session**
  (rather than "recording", which is ambiguous between a take and a file).
- **Agreed — multitrack is one track per file** (mono files, one per input),
  so a multitrack session is N files with the same start time and duration.
  No polyphonic-file handling needed.
- **Agreed — a show over several nights is one Project with one Session per
  night.** That covers the "nested project" example, so **project nesting is
  not needed** and is dropped; revisit only if a case appears that sessions
  can't express.
- A **session** is what gets reviewed, dated, located and filed as a unit; a
  **file** is a physical audio file (a split part, or one track of a
  multitrack). Split files (§17.7) still join into one file *within* a
  session; multitrack files stay separate files in the session.
- A single loose recording is a project of one session of one file; the UI
  hides that scaffolding rather than making the user create it.
- **Agreed — one session per night, for now.** Two recorders on the same
  night (e.g. a multitrack desk plus a stereo pair) are therefore one session
  containing several file groups. A soundcheck/show split within a night is
  not modelled; revisit if it becomes a real need.

### 18.2 Free-text notes everywhere

**Agreed (new):** every level needs a free-text description/notes field —
**project, session, file, and segment/clip**. Distinct from the title and
from tags: long-form, unstructured, searchable in the same full-text index as
transcripts (§18.5). Check what `Project`, `Resource` and `Clip` already
carry before adding columns; the manage page (§17.2) edits project notes.

### 18.3 Navigation

- **Agreed — a Home page** as a separate overview (library counts, recent
  projects), as a fourth tab. (The user's answer was tentative — "yes???" —
  so treat as provisional until seen.)
- **Proposed:** browse by category in the UI; the project is the physical
  container on the NAS. Category is a *filter/lens* over projects, not a
  folder level, so a project spanning several categories is still one
  folder. Needs the user's confirmation — the question was not understood as
  first phrased.
- **Agreed direction:** "ambient" becomes **Field recordings** (label only —
  slug can stay until §17.1 categories are configurable).
- **Agreed direction:** the map/track view is a **lens available on any
  session or file that has location data**, not tied to one category. A
  category may still set a *default* (e.g. field recordings open on the map).

### 18.4 Voice recordings: section-level tagging (SFX-library style)

Voice material is organised like an SFX library: the unit of search is a
**segment** — a time range within a file — not just the file. Example:
find Jacob saying a particular phrase and land on that moment.

- **Agreed — one tag pool.** Segment tags come from the same pool as file
  and project tags; the UI shows which level a tag was applied at.
- A segment carries: start/end, tags, free-text notes (§18.2), optional
  transcript text, optional speaker. The existing `Clip` becomes one use of a
  segment (a segment can be exported; not every segment is).
- **Agreed — segments export as WAV markers/regions** (BWF/iXML-style) so
  they appear in a DAW. Format details to design at build time.
- Manual segment tagging works with no ML and ships first; transcription
  (§18.5) pre-populates segments rather than replacing manual ones.

### 18.5 Transcription and speaker identification (local only)

**Hard constraint (stated): local only — no cloud services.** Not
necessarily on the app server.

- **Engine:** Whisper-family model (faster-whisper / whisper.cpp) with
  word-level timestamps; voice-activity detection first so only speech is
  transcribed; known names fed as a vocabulary hint. English.
- **Volume (stated):** under an hour of voice so far, mostly a small set of
  ~3 known people. So transcribe-everything-on-ingest is affordable and
  speaker identification is a realistic small closed-set problem. Diarise
  (who spoke when) then **suggest** names from a few user-labelled examples;
  never apply a name silently. Voice profiles of real people are biometric
  data — stored locally only, deletable per person.
- **Where it runs — leaning:** the user would like to keep it on Proxmox but
  accepts the Mac is far stronger and a small worker app there is
  reasonable. Design for **a transcription worker that is not the web
  container**: it claims only `transcribe` jobs and reports through an HTTP
  API with a token, rather than opening the database to the LAN. If that
  worker is off, jobs wait (same self-healing pattern as enrichment, §12).
  **Open:** Mac worker vs a Proxmox VM/CT with a GPU — depends on hardware
  available on the cluster (not yet checked).
- **Corrections** are stored as verified and never overwritten by a
  re-transcription. Split files are joined before transcribing so a phrase
  spanning a boundary isn't cut. In a multitrack session with one mic per
  person the *track* already identifies the speaker; no ML needed.
- **Search:** Postgres full-text search plus trigram over transcripts,
  notes, tags, filenames and project titles; results for segments show the
  snippet and a play-from-here.
- **Behaviour follows content, not category:** anything containing speech can
  be transcribed; a category only sets the default policy.

### 18.5b Assume no embedded metadata (stated)

**Agreed:** design as if recordings carry **no embedded tags at all**; any
that exist are a bonus. The user's kit is not believed to write any. So:

- **Track names come from filenames only** (e.g. `TR01`, channel numbers), via
  the §17.6 patterns. No reliance on iXML/BWF track names.
- **`captured_at` and grouping** (§17.7 split/multitrack detection) cannot
  rely on trusted tags either. Sources, in order: a filename pattern
  timestamp, then embedded tag if present, then file mtime, then manual
  entry. Every value records *which source* produced it, and only a manual
  or embedded value counts as confirmed; filename/mtime values are
  suggestions the user confirms at intake.
- **This bends §4**, which today takes a timestamp only from trusted embedded
  tags and otherwise leaves it null. **Needs the user's explicit OK** before
  ingest changes. Note the one real file so far
  (`audio_260917_091124_32bit_orig_stereo.wav`) has a timestamp in its name
  that does not match the date the user entered by hand (2026-09-15), so
  filename timestamps can't be trusted blindly either — hence "suggest, then
  confirm".
- ffprobe still supplies duration, channels and sample rate, which are the
  main signals for grouping tracks of one multitrack take (same duration,
  adjacent or identical start).
- Sample filenames from each recorder (split-file and multitrack) are still
  owed and are now the *only* input to grouping, so they matter more.

### 18.5c Date certainty: exact / approximate / unknown (stated)

**Agreed:** filenames often carry a timestamp, so the source order in §18.5b
stands (filename → embedded → mtime → manual, each recorded and only
manual/embedded counting as confirmed). Added on top:

- **Unknown date flag at import.** One of the user's recorders does not set
  file times correctly at all. Files from such a recorder must arrive flagged
  "date unknown — needs manual correction" rather than carrying a wrong guess.
  This is a property of the **recorder profile** (§17.5), e.g.
  `clock_reliable = false`: for that recorder mtime is never offered, and a
  filename timestamp is only a suggestion if the pattern matches.
- **Approximate date/time.** Some recordings only have an approximate
  date/time — mainly show recordings, and some early ambient tests. These
  must **not** be location-tagged: a fuzzy time would pull the wrong GPS
  point. Proposed: `captured_at_precision` = `exact | approximate | unknown`
  alongside `captured_at_source`. Location enrichment (Dawarich lookup) and
  the map pin run only for `exact`; `approximate`/`unknown` recordings appear
  under an "unlocated" filter and can still take a manual location.
- The intake queue gets a derived "needs date" state for `unknown`.
- Note: the round-trip bug that shifted 09:00 to 08:00 in the detail screen
  (§17.5, naive vs Z serialisation) is a code issue, not user error, and
  stays open until the timestamp contract is implemented.

### 18.5d Field recordings journey (agreed points)

- Recordings are mostly **short takes**, some up to about an hour, so both
  shapes must work: a short take shows a **pin**; a long take can also show
  its **track**.
- **Dawarich is logging constantly**, so location for an `exact` date is
  normally available; a gap in the log is the exception and is shown as
  "no location found", not an error.
- **Global map view (wanted):** a map of all located recordings with a pin
  (clustered) per recording, like the Places view in photo managers;
  filterable by category, tag and date; a pin opens the recording. Distinct
  from the per-recording map lens (§18.3).

**Field journey decisions (answered):**
- **Grouping: yes.** Short takes close in time and place are *suggested* as one
  session (an outing/walk), never auto-merged; the user confirms or splits.
  Thresholds are settings, not constants. Grouping needs `exact` dates
  (§18.5c), so approximate/unknown recordings are never suggested into a
  walk.
- **Map tiles: OpenStreetMap is acceptable** (external tile fetch from the
  browser). Keep attribution and light usage per OSM's tile policy; the tile
  URL is a setting so it can be swapped for self-hosted tiles later.
- **Place names: yes**, a readable name on each located recording.
  **Open:** the source. Preferred order: whatever reverse-geocoding Dawarich
  already does (no new outbound calls), else a geocoder. A public Nominatim
  sends coordinates outside the LAN and has rate/usage limits; self-hosted
  Photon/Nominatim is the local option. The name is cached on the location
  row and editable by hand, so a geocoder outage never blocks anything (§12).
- **Open:** where a loose (no-project) field take is filed on the NAS —
  `misc/field-recordings/{year}/` (today's fallback) versus a folder per
  outing. Not yet answered.

### 18.5e Voice journey decisions (answered)

- **Categories:** the user could not say what separates `voice-personal` from
  `voice-project`. **Proposed:** one `voice` category; whether it belongs to a
  project (or which) carries the difference. Cheap now: one real resource
  exists and categories become data anyway (§17.1). Needs a yes.
- **Location** matters little for voice; no full browsable map for it. The
  §18.3 rule already covers this: the map lens appears only when a recording
  has an exact location, so voice simply never asks for one.
- **People are tags** (one pool, §18.4) — no separate people table. A speaker
  label on a segment is just a tag on that segment. Later speaker
  identification (§18.5) maps a voice profile to an existing tag rather than
  creating a second identity system. Optional later refinement: a tag "kind"
  (person / place / topic) purely for filtering and display.
- **Private flag: build it (requested).** Recordings of identifiable people
  need a way to keep them contained. Scope is **proposed, not yet confirmed**:
  a `private` boolean on project, session and file, where a private parent
  makes its children private. A private item is:
  1. **excluded from the Drive `/Library` mirror** — Drive is a cloud service
     and the user's stated constraint for voice/transcription is local only;
  2. never sent to any external service (transcription is local regardless);
  3. excluded from export/download links and any future sharing;
  4. hidden from Library and search by default, shown with an explicit
     "include private" toggle.
  The Inbox path stays as-is (files arrive via Drive by necessity); once
  filed, a private file's Drive copy in `_processed` is a separate question
  (cleanup, §DEPLOYMENT) to raise with the user. **Open:** confirm scope, and
  whether private files should be encrypted at rest or only excluded.

### 18.5f Real filename inventory (from the user's Drive archive, 2026-09-19)

Surveyed by listing filenames only (no file contents) in the Drive
"Audio recording" folder on the user's Mac; samples saved in
`tests/fixtures/sample_filenames.txt`. Findings that shape the design:

- **Several recorders, several patterns; the app needs per-recorder
  profiles selected by filename pattern** (§17.6): Zoom `YYMMDD-HHMMSS.WAV`
  (largest set, ~125 files; also older `ZOOMNNNN.WAV`), Zoom stereo
  `STE-NNN.wav` (no date at all), Insta360 mic
  `audio_YYMMDD_HHMMSS_{24|32}bit_orig[_stereo].wav`, phone voice-memo apps
  (`2025-08-27_184647372.wav`, `27-04-2025, 15-42.wav`, and `5 Sept at 10-04.m4a`
  with no year), and hand-named files with no date.
- **The 30-minute splitter is the Insta360 mic.** 32-bit float stereo parts
  are exactly 1800 s (691.27 MB); the next part's filename time is ~2 s
  after start+30:00, so adjacency needs a tolerance of a few seconds, not
  exact equality. Confirmed chains: 2026-09-17 10:07:46 → 10:37:48 → 11:07:48
  (3 parts) and 2026-09-14 14:58:38 → 15:28:40 (2 parts). The last part is
  short. A shorter file followed by a non-30:00 gap is a separate take, not a
  split.
- **The unreliable-clock recorder is also the Insta360 mic in 24-bit mode**:
  its earlier files are named `audio_000101_HHMMSS_…` (1 Jan 2000, HHMMSS
  since power-on). A filename year of 2000 (or 1970) is therefore a **rule to
  mark the date unknown** (§18.5c), not something to parse. The time-of-day
  in those names still orders takes within one power-on. Later files carry
  real dates, so the clock was set at some point (13 Sep 2026).
- **User-written descriptions follow the timestamp** with inconsistent
  separators (` - `, `-`, `- `, `.-`, e.g.
  `250424-121237.-woods-includes-voices.WAV`). The pattern should split
  timestamp from a free-text remainder and offer the remainder as the
  **title/notes suggestion**, not throw it away.
- **Existing outing structure confirms the grouping idea (§18.5d):**
  Zoom takes cluster into obvious outings (e.g. 2025-04-24 Parkridge: seven
  takes within about 1h20m; 2025-05-25 and 2024-08-21/22 similar).
- **Edits and derivatives live beside the originals:** `-EDIT` renders,
  `.mp3` copies, REAPER `.reapeaks` / `.pkf` sidecars. Ingest should ignore
  sidecar extensions and treat an "EDIT" sibling as a *related* file, not a
  duplicate original (open question for the user).
- **The archive already has ~340 files, so a bulk import path is needed,**
  not just the Inbox trickle. **Open:** whether the archive should be
  imported (and where the user's REAPER project folders sit).
- **No multitrack-recorder files were found** in this folder. Still owed.

### 18.5g Archive import, file roles, multitrack, NAS paths (answered)

- **Archive import goes through the Inbox routine — no separate import
  feature (stated).** Consequences found in the code, to be handled *before*
  the archive is dropped in (~340 files, tens of GB):
  - `drive_inbox_pull` lists only the top level of `/Inbox` (`rclone lsjson`
    without `-R`), so files in subfolders are not seen. Needs recursion; the
    relative path is already kept in `drive_inbox_path` and re-created under
    `_processed`, so folder names survive as hints (matters for `STE-` files,
    §18.5f, whose only context is the folder).
  - There is no extension filter, so `.reapeaks`/`.pkf` sidecars would be
    ingested and land as `failed`. Needs an ignore/allow list (setting).
  - Throughput and disk: cap files per run; check staging free space.
  - 340 items in the review queue need **batch actions** (select many, apply
    category/project/tags), otherwise the review journey doesn't scale.
- **Edited versions are the same recording (stated).** `-EDIT` files
  ("cleaned up / trimmed") are versions of an original, and this is exactly
  why a project/session is a *bundle of files with different purposes*.
  **Proposed:** a file `role` (`original | edit | export | reference`) and a
  nullable `derived_from` link inside the session. Suggest the link when an
  edit's filename shares the original's timestamp/prefix (never automatic).
  The original stays the primary for date, place and category; edits inherit
  them. **Segments/transcripts belong to one file**: a trimmed edit shifts
  time offsets, so segments are not copied between versions. Existing clip
  exports (§15) are the same idea and become `role = export`.
- **Multitrack: best-effort default, no sample files yet (stated: user will
  import those later as new items).** Detect from signals, not names:
  simultaneous files (start within a few seconds), equal duration and sample
  rate, same shared filename prefix with a varying number/label
  (`TR1…TRn`, `CH01`, `_1`). With no timestamps in the names, arrival in the
  same Inbox batch/folder plus equal duration is the fallback. The varying
  part of the name becomes the **track label**; the user can rename. Always
  a suggestion. Revisit with real files.
- **Loose files are filed by year and month (stated):** default template
  `misc/{category}/{year}/{month}/{filename}` (month zero-padded), a config
  change to the existing template (§2), so it is not retroactive. Project
  files stay `{project}/…` (session sub-folder per §18.1 to be decided at
  restructure time).

### 18.5h Dawarich check against the test file (read-only, 2026-09-19)

- **The filename time is correct local time** (`YYMMDD_HHMMSS`, BST):
  `audio_260917_091124…` = 2026-09-17 09:11:24 BST = 08:11:24 UTC. Nearest
  Dawarich point was 22 s away and **62 m from Stoke-on-Trent station**,
  matching the user's expectation. So both the pattern reading and the
  BST-as-UTC+1 handling are confirmed on real data.
- The value stored on that resource (2026-09-15 08:11) is a different day and
  ~12 km from the station under either UTC or BST reading, so the *date* was
  mis-entered; the time-of-day was right. Not corrected — the user's data.
- **Dawarich returns no place names:** `city`, `country`, `geodata` are empty
  and `reverse_geocoded_at` is null on these points (reverse geocoding is
  evidently not enabled on the instance). So place names need a source we
  control. Options: enable geocoding in Dawarich; call a geocoder from this
  app once per recording, cached on the location row (public Nominatim is
  fine at that volume but sends coordinates to OSM, consistent with the
  user's OK for OSM tiles); or self-host Photon/Nominatim. **Open.**
- Point data is plentiful: 632 points in 90 minutes around the target, so
  short takes and hour-long tracks are both well served.

### 18.5i Sidecars, DAW project files, disk queue, waveforms (answered)

**Disk-space queue — BUILT** (`jobs/disk_budget.py`, wired into
`drive_inbox_pull`). A pulled file waits in `STAGING_DIR` for review, and
staging shares a 16 GB disk (about 15 GB free when measured) with the app, so a
bulk import must not fill it. Before each pull a file must satisfy two limits:
free space stays above `DISK_RESERVE_GB` (3) and bytes awaiting review stay
within `STAGING_BUDGET_GB` (6); plus `INBOX_MAX_FILES_PER_RUN` (10). A file
that doesn't fit **stays in the Drive Inbox** with its queue position and
stability clock intact and is pulled on a later run. Oldest-seen first;
head-of-line (once one waits, later ones wait behind it); a file larger than
the whole budget is skipped loudly without blocking others. Held-back files
are listed in the job's log tail / result (`deferred`). Verified: 7 unit tests
and a 6-case run against the real Postgres with rclone/ingest faked, plus a
real pull afterwards. **Not verified / limits:** the limits are env vars, not
yet on the settings page; the budget only counts staging, so until the NFS
export is mounted (`NAS_LIBRARY_ROOT` is on the same root disk) filed files
also consume it — the reserve still protects the disk, but **do not drop the
archive into the Inbox before the NAS is mounted.**

**Sidecars follow their audio (stated).** `.reapeaks` / `.pkf` are
regenerable but should be kept. Proposed: a file `role = sidecar`, attached to
its audio by name (`X.wav.reapeaks`, `X.pkf` → `X.wav`), never ffprobed,
never shown in review, moved with the audio to the same NAS folder and
mirrored to Drive, and not indexed for search. A sidecar whose audio hasn't
arrived is held in the Inbox rather than ingested as a failure. They count
against the disk budget. Extension list is a setting.

**DAW project files live in the project folder and are edited in place
(stated).** Reaper/Audition projects sit beside the audio on the NAS. This
changes the storage rules:
- Two kinds of file: **immutable sources** (audio; checksum-guarded, any drift
  is an alarm — `verify-integrity` §5) and **mutable working files** (project
  files, peak caches, sidecars; edited by other programs, no drift alarm).
  Editing is non-destructive, so sources never change.
- They are created *after* filing and never pass through the Inbox, so they
  must be **adopted by a scan** of the project folder (today `find-orphans`
  would just flag them as unknown). Proposed: adopt as `role = project-file`
  with the DAW type inferred from extension; the mirror job already carries
  them to Drive.
- **Refiling can break a DAW project** (moved audio leaves its references
  dangling). A project containing project files must move as a whole folder
  or refuse/warn; the template-change-isn't-retroactive rule (§2) already
  helps. **Open:** how the user opens them (SMB from the Mac?) and whether
  Reaper references are relative or absolute.

**Waveforms — plan (not built), adapted from the media-curator handover.**
Fits our architecture better than theirs: the queue is durable Postgres, so
none of their in-process queue/generation-counter machinery or the
"exactly one server process" restriction applies.
- A `generate-waveform` job in the existing queue, enqueued after ingest
  (the file is on local staging then, so it's fast). Opening a file that has
  none enqueues it at high priority; needs priority support in
  `jobs/queue.py` if absent (not checked).
- **Cache keyed by content checksum** (already stored) plus generator
  version/params, not path+mtime: it survives refiling, and an edit is a new
  file, so invalidation is automatic. On local disk, not the NAS, e.g.
  `/var/lib/audio-manager/waveforms/<sha[:2]>/<sha>.dat`; deleted with its
  resource. Each entry records engine, generator version and params.
- Generator: `audiowaveform`, fed through `ffmpeg -ac 1 -af highpass=f=80
  -f wav -` (mono display mix, hum removed, no resample), `-z <spp>` with
  `spp` from the real sample rate, `--bits 8`, `--output-format dat`. Their
  rules kept: the flag is `-z`; run the tool once and check the exit code;
  every subprocess has a timeout and `nice`; ffmpeg's stdout closed in the
  parent; on timeout kill both; **log rc + stderr and record `engine` on any
  fallback, and test the primary path** (2 s sine fixture asserting
  `engine == audiowaveform`).
- **One dense tier, not five**: about 100 peaks/s (spp = rate/100), roughly
  0.7 MB per hour as native binary `.dat`, handed to Peaks.js **unchanged**
  (its header carries sample rate and spp), which removes the handover's
  playhead-drift bug (their §5.2 "cleaner, untested" route — untested here
  too). Extra tiers only if zoom proves insufficient. Normalisation is a
  display choice, done client-side per file (untested).
- **`audiowaveform`** was not in apt; it has since been installed from BBC's
  release package with the user's OK (see §18.5j).
- Playback: `/api/resources/<id>/audio` uses `send_file(conditional=True)`
  and **measured 206 with correct Content-Range** direct from Flask (through
  nginx not yet measured — their lesson: check Range before blaming the
  waveform).
- Front end: Peaks.js v3 + Konva, **vendored at pinned versions** in
  `app/static` (no runtime CDN), with their custom player adapter and init
  order (fetch duration first; double-rAF init; resize handling). Verify with
  numbers (peaks vs full-band envelope correlation, playhead drift, click
  accuracy) and remember hidden tabs pause `requestAnimationFrame`, which
  looks exactly like "waveform won't load" in my automated browser.

### 18.5j audiowaveform installed; Reaper paths measured; storage is not a mirror

**audiowaveform — INSTALLED on the container (with the user's OK).** v1.10.2
from BBC's GitHub release, the Debian 12 build
`audiowaveform_1.10.2-1-12_amd64.deb` (the `-13` build is Debian 13; a web
summary I fetched first misread this, so the package was chosen from the raw
GitHub API listing). GitHub publishes no checksum for that build; the sha256
I computed on download is `63ef3226097e7dc7f17104b353f540f3d423191d675fdde4d00665fca231c5cf`
(169,730 bytes, matching the API's size). Note this is 1.10.2, not the 1.11.1
the media-curator handover mentions (1.11.x has no GitHub release that I
could see). **Measured:** a 2 s sine fixture through
`ffmpeg | audiowaveform … --output-format dat -z 441 --bits 8` exits 0 and
gives 200 points; the real 346 MB, 48 kHz stereo 32-bit-float file (901.8 s)
gives 90,181 points at spp 480 = 901.81 s, sample_rate and spp carried in the
header, i.e. a 180 KB `.dat` for 15 minutes. Not yet measured: run time,
correlation against a full-band envelope, the generator wired into a job.
That file's data chunk ends mid-frame (ffmpeg warns "Invalid PCM packet");
harmless but the job must not treat an ffmpeg warning as failure.

**Reaper paths — measured on the user's real project** (`H1n tests/1/1.RPP`,
only its `FILE` lines read): all 12 media references are **bare relative
filenames** (audio sits in the same folder as the `.RPP`), none absolute.
REAPER stores a relative path when the media is under the project folder,
absolute otherwise (web sources conflict; this file is the evidence). So: a
project folder moved *as a whole* keeps working; **renaming or moving an
audio file inside it breaks the reference.** Rules that follow: never rename
an original once filed (suggested titles from filename suffixes must go to
title/notes, not the filename), refiling moves a whole project folder or
nothing, and the audio + `.RPP` layout inside a project folder is preserved
as found.

**Storage is not a mirror (stated).** Items may need to exist on the NAS, on
Google Drive, or both — decided per item — and the user opens project files
from either place. §1's one-way "NAS → Drive `/Library` sync" no longer fits:
`rclone sync` of a whole tree is all-or-nothing and deletes at the destination
what isn't at the source. **Proposed replacement:** a per-file record of
where copies exist (`file_copies`: file, location `nas|drive`, remote path/id,
checksum, verified-at, state) driven by a **placement policy** on
project/session/file (`nas`, `drive`, `both`; project default, file override),
with copy jobs per file (`rclone copyto`, never `sync`, never delete) and the
UI showing badges for where each thing lives. `library-verify` /
`find-orphans` become per-file checks against those records.

**The hard part is mutable files.** Audio is immutable, so a second copy is
easy to keep correct. Reaper/Audition projects are edited in place, and if
both a NAS copy and a Drive copy can be edited they diverge. **Proposed rule:**
each project has one **home** (where it is edited); the other location is a
copy, refreshed one way from home. If the non-home copy is found changed
(checksum differs from what the app last wrote), **flag a conflict and do not
overwrite** — consistent with "never silently discard". Bidirectional sync is
avoided. **Open:** whether the user switches a project's home over time, and
how the change of home is done (an explicit "make Drive the home" action).
Drive storage quota also matters for `both` (not yet checked).

### 18.5k Placement decisions, Drive cost, and a broken Drive upload

**Answered:** (1) placement is **a setting on the project** (`nas | drive |
both`, with `home` = where it is edited); (2) a project's home **can be
switched**, via an explicit action, for now; (3) the user wants to **reduce
Drive use overall for cost** — about 300 GB free of 1 TB.

**Consequences.**
- **Default placement is `nas`.** Drive is the field Inbox plus opt-in
  projects, not a second full library. The current hourly `nas-to-drive-library`
  (whole-tree `rclone sync`) must not run as-is once placement exists; it
  is replaced by per-file copies for projects flagged `both`/`drive`.
- **Drive space is mostly held by originals in `Inbox/_processed`,** which the
  app cannot delete (personal account, Editor rights, §DEPLOYMENT), so they
  linger until the user deletes them. The biggest saving is therefore a
  **reclaimable-space view**: list ingested originals whose NAS copy is
  checksum-verified, with sizes and a total, so the user can delete them in
  Drive knowing it is safe. That is the "outstanding cleanup" mechanism the
  user asked for earlier, now with a cost reason. The archive import will
  create ~340 such files.

**FOUND — the NAS→Drive upload has been failing.** Checked live 2026-09-19:
`nas-to-drive-library` errors on every run with Google 403
`storageQuotaExceeded: Service Accounts do not have storage quota`. A service
account can make folders (they exist under `Library/`) but cannot own files on
a personal Drive, so **no file has ever been copied to Drive `/Library`**. It
is harmless (rclone refuses to delete when there are IO errors) but the
Drive-copy feature cannot work over the current folder-restricted
service-account connection. Options, a security trade-off for the user:
 a. *Least privilege (recommended):* keep the service account for the Inbox
    (read + move within folders), and add a **second rclone remote using
    OAuth with the `drive.file` scope**, which can only see files it created,
    used solely for Library copies. Cannot touch the user's originals; cannot
    reclaim `_processed`, which stays a manual delete guided by the
    reclaimable list.
 b. *Full OAuth as the owner:* one remote can upload and also delete
    `_processed` (owner), but the token on the LXC can reach the whole Drive
    (folder limits would then be client-side only). Simplest, broadest.
 c. *Shared Drive:* not available on a personal account.
**Open:** which option; not building Drive copies until decided. Because the
default placement is `nas` this is not urgent, but the failing hourly job is
noise and the `_processed` cleanup is the cost lever.

### 18.6 Build order (proposed)

1. Timestamp/timezone contract and its dependent fixes (§17.5).
2. Project → Session → File restructure, migrating the existing rows.
3. Notes fields (§18.2), Home page, categories-as-data (§17.1).
4. Manual segment tagging with waveform and transcript-free UI.
5. Transcription worker, then speaker identification.

Before step 2, walk the remaining journeys (event first: show across nights,
one or several recorders, multitrack) so the session boundary is right.

## 19. Android companion app — PLAN ONLY (do not build until explicitly asked)

**Instruction (2026-09-21): this is planning only, for a while. No app code,
no server work done *for* the app, until the user explicitly asks.**

### 19.1 Scope decided
- **v1 = an uploader for files on attached storage** (an SD card in a USB
  reader, or a recorder in mass-storage mode plugged into the phone). Not a
  recorder, not a library browser.
- **Recording in the app is a good idea, kept as a later, separate option**
  (§16 notes: USB audio input, foreground service, exact timestamp at the
  source). Not part of v1.

### 19.2 Proposed behaviour of v1 (not decided in detail)
- Attach storage → the user picks the folder once → the app lists audio files
  it hasn't uploaded before → uploads → the server confirms → the app marks
  them done. **The card is treated as read-only by default: nothing is ever
  deleted from field media.**
- **Recorder profile chosen per card** (e.g. "Zoom H1n", "Insta360") so the
  server applies the right filename pattern and clock rules (§18.5c/f) — the
  phone's clock is the *upload* time and says nothing about when a file was
  recorded, so the app must not claim a capture time for recorded-earlier
  files.
- **Batch assignment at upload:** choose project/session (or "leave for
  review") for everything selected, so files arrive already grouped, e.g.
  "Show X, night 2".
- Send each file's relative folder path as a **source-path hint** (the same
  idea as `drive_inbox_path` for the Inbox, and it matters for `STE-` files
  whose only context is the folder name).
- Filter by extension on the phone: skip sidecars/peak files unless the
  sidecar rules (§18.5i) say to send them alongside their audio.
- Android access to attached storage goes through the Storage Access
  Framework (user grants a folder, files are read via document URIs, not
  paths) — from general knowledge, **not verified here**; confirm before
  designing further.
- Work runs in a foreground service with WorkManager-style retry; prefer
  Wi-Fi/VPN, tolerate the phone not being on the VPN (§16); hash the file
  while streaming it; never mark done until the server returns a matching
  checksum.

### 19.3 What the server would need (only when asked)
The upload endpoint exists (`POST /api/ingest/upload`, `X-Upload-Key`, runs
the shared ingest synchronously) but is not sized for this. Gaps, all listed
so nothing is forgotten, none scheduled:
- **Large files:** 30-minute Insta360 parts are ~690 MB. Needs a
  chunked/resumable upload, Flask `MAX_CONTENT_LENGTH` and nginx
  `client_max_body_size`/timeouts set.
- **Disk admission:** uploads bypass the Inbox disk-budget queue
  (`jobs/disk_budget.py`), so they must be refused with a "try later"
  response when staging is full.
- **Idempotent + confirmable:** dedupe by sha256 already exists; needs a
  "have you got this checksum?" query so the app can skip and confirm.
- **Metadata block** with the upload (recorder profile, project/session,
  source path, title, notes); confirmed-date fields only if the app truly
  knows the capture time.
- **Auth:** shared key is enough on the VPN; per-device tokens if external
  access is ever revisited (§16).

### 19.4 Later, plan-only options
Recording in the app (exact time and timezone stamped at the source, optional
GPS, project/session picked before recording); browsing/playing/tagging from
the phone; notifications when transcripts finish. Native Kotlin is the likely
tool, since recording, USB and background work are platform-specific — not
decided.
