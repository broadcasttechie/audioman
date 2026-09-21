# NEXT.md — build-ready summary (read this first, then PLAN.md only as needed)

Written 2026-09-19 to make the next coding session cheap: everything decided
in the design discussion, in one place, with an ordered work list. PLAN.md
§17/§18 hold the reasoning; this file holds the conclusions. If they
disagree, PLAN.md §18 wins and this file needs fixing.

## State of the code (commit a0261f5 and later docs)
Flask/Postgres app on LXC 132 (pmx2), Postgres job queue + worker, systemd
timers. Built: settings page, Drive connection (service account, folder
picker), Inbox pull with **disk-space admission**, ingest, Review Queue,
Resource Detail, Library. Deploy: tar → scp pmx2 → `pct push 132` → extract in
`/opt/audio-manager` → `systemctl restart audio-manager-web.service` and
`audio-manager-worker@1.service`. No pytest locally (no Flask); run
`python3 -m unittest discover -s tests` **on the container** with
`/etc/audio-manager/audio-manager.env` loaded and `PYTHONPATH=/opt/audio-manager`.
`audiowaveform` 1.10.2 is installed on the container. No git remote yet.

## Decisions (all stated or agreed by the user)
- **Model:** Project → Session → File. A session is one night (shows) or one
  outing (field); loose recordings hide the scaffolding. **No project nesting.**
- **File roles:** `original | edit | export | sidecar | project-file`, plus
  `derived_from`. `-EDIT` files are versions of the same recording; edits
  inherit date/place/category from the original. Segments belong to one file.
- **Dates:** `captured_at_source` = filename > embedded > mtime > manual; only
  manual/embedded are confirmed. `captured_at_precision` = exact | approximate
  | unknown. Only *exact* gets a Dawarich lookup / map pin / outing grouping.
  Recorder profiles select filename patterns; a filename year of 2000/1970 =
  unknown date. `clock_reliable=false` recorders never offer mtime. User must
  confirm suggested dates at intake. **Timestamp contract still unfixed**
  (naive-UTC in DB, serialise with Z, recorder IANA tz + offset seconds).
- **Verified facts:** filename time is local time: `audio_260917_091124…` =
  09:11:24 BST = 08:11:24 UTC, 22 s from a Dawarich point 62 m from Stoke
  station. The one real resource's date has since been corrected (2026-09-17
  08:11Z).
- **Real filename patterns:** see `tests/fixtures/sample_filenames.txt`.
  Insta360 mic `audio_YYMMDD_HHMMSS_{24|32}bit_orig[_stereo].wav` = the
  30-minute splitter (parts exactly 1800 s; next part's name is ~2 s after
  start+30:00, so tolerance is seconds) and also the no-clock recorder
  (`audio_000101_…`). Zoom `YYMMDD-HHMMSS[<sep>description].WAV`, `ZOOMNNNN`,
  `STE-NNN` (no date; folder name is the only context). Phone memo styles
  incl. no-year. Ignore sidecars (`.reapeaks`, `.pkf`) as audio.
- **Multitrack:** one mono file per track; best-effort detection (start
  together, equal duration/rate, shared prefix + varying label); suggestions
  only. No sample files yet.
- **Categories become data**; one `voice` category proposed (needs a yes);
  "ambient" → "Field recordings". Map is a lens on anything with an *exact*
  location, not a category flag. Global map view of located recordings
  (clustered pins; OSM tiles fine, tile URL a setting).
- **Grouping suggestion** of takes close in time/place into an outing session;
  never automatic; thresholds are settings.
- **Place names:** wanted; Dawarich returns none (geocoding off) → this app
  looks each recording up once and caches on the location row, editable.
  Source (public Nominatim vs self-hosted) still open.
- **NAS paths:** project files `{project}/…`; loose files
  `misc/{category}/{year}/{month}/`. Config change, not retroactive.
- **Notes:** free-text notes on project, session, file and segment; searchable.
- **Voice:** section-level *segments* (start/end, tags, notes, transcript,
  speaker). One tag pool; people are tags. Local-only transcription
  (Whisper-family, VAD first, English) on a **separate worker** (Mac or a
  GPU box) over an authenticated HTTP API; no DB exposure. Segments export as
  WAV markers. Manual segment tagging first. Private flag **parked**.
- **Storage:** not a mirror. Project `placement` = nas | drive | both
  (default **nas**, user wants to cut Drive cost) and `home` (where edited;
  switchable by explicit action). Per-file `file_copies` records, per-file
  `rclone copyto`, never `sync`, never delete. Non-home copy changed →
  conflict flag, never overwrite. Audio immutable (drift = alarm); project
  files / caches / sidecars mutable. DAW project files are adopted by scanning
  the project folder. Reaper paths are relative bare filenames (measured), so
  **never rename/move an original; refile whole project folders only.**
- **Drive:** originals stay in `Inbox/_processed` (app can't delete on a
  personal account) → build a **reclaimable-space view** (NAS-verified
  originals, sizes, total). **`nas-to-drive-library` has never worked**
  (service accounts have no quota, 403); fix choice pending (see questions).
- **Waveforms:** job `generate-waveform`, cache keyed by sha256 + params on
  local disk, `audiowaveform` native `.dat`, one dense tier (~100 peaks/s,
  spp = rate/100) passed unchanged to Peaks.js; vendored pinned JS; record
  engine, log any fallback, test the primary path.
- **Archive import** (~340 files) goes through the Inbox — no import feature.
  Prereqs: NAS mounted, recursion into subfolders, sidecar rules, batch review.

## Built 2026-09-21: time contract + location fixes (work package 1, mostly done)
- **Contract** (`app/timeutil.py`): DB = naive UTC; API **always sends a trailing Z**, accepts
  Z / offset / naive (= UTC), 400 on garbage. This fixes the reported 09:00-reads-back-as-08:00
  bug at its cause (a naive ISO string is parsed by browsers as local time); reproduced and
  confirmed in Node in Europe/London.
- **Ingest:** embedded timestamps keep a UTC offset if present; a bare wall-clock time is the
  recorder's local time, converted with `DEFAULT_RECORDER_TIMEZONE` (Europe/London) so BST is
  right (a BWF 09:11:24 -> 08:11:24 UTC, verified through exiftool). Ambiguous/nonexistent DST
  times resolve deterministically (documented in the module) — recorder profiles can flag them later.
- **Dawarich:** range queries use epoch seconds; point timestamps are integer epochs (the old
  `fromisoformat` would have crashed on the first real response); coordinates arrive as strings
  and are now numbers. Verified live: the test file's moment returns 14 points with the pin
  **62 m from Stoke station** (point density is low while stationary; the client returns the
  same set at any page size).
- **Location refresh:** replaces the track instead of appending (it duplicated points), and never
  overwrites a **manual** location (the scheduled enrichment did too). Shared helper
  `jobs/enrich.apply_location_result`.
- **API:** stale/malformed tag ids now 400 (used to crash on commit); all API datetimes carry Z.
- Verified: 35 unit tests + a 30-check live run (`tests/`, run on the container).
- **Real-file test (requested by the user):** the one real resource (captured 2026-09-17
  08:11Z = 09:11 BST, date since corrected by the user) was refreshed against Dawarich and now
  has location 53.00816, -2.18121 (`dawarich-auto`, Stoke station) and a 13-point track.
- **Not done from WP1:** the "Refresh location" button on the detail screen (the API works).

## Built 2026-09-21: filename patterns + recorder profiles + date precision (work package 2)
- **Recorder profiles** (`recorder_profiles` table, seeded from `jobs/filename_patterns.DEFAULT_PROFILES`,
  editable via `GET/POST/PATCH /api/recorder-profiles`; patterns are grok-style `{YY}{MM}{DD}-{hh}{mm}{ss}{rest}`,
  validated on save; each has an IANA timezone, a `clock_offset_seconds` (recorder clock minus true time),
  a trust level and a priority). Eight defaults cover every real filename in `tests/fixtures/`:
  Insta360 mic, Zoom, Zoom ZOOMnnnn, Zoom STE-nnn, two phone-recorder formats, "Date and title", phone memo
  with no year. Matching ignores extensions (`.WAV.wav`) and Drive's "Copy of " prefix.
- **The user's decision, implemented:** **Zoom and Insta360 are `trusted`** -> the filename date is applied at
  ingest (`captured_at_source = filename`, precision exact) so location lookup runs at once; **every other
  profile is `suggest`** -> the date is only stored as `suggested_captured_at` for the user to confirm.
- **Unknown dates:** an Insta360 `audio_000101_...` name (year 2000, clock never set) gets no date, precision
  `unknown`, and a stated reason. A year before 2010, a future date, or an impossible date is never applied.
- **Precision** `captured_at_precision` = exact | approximate | unknown (unknown <=> no date). **Only exact is
  looked up in Dawarich/Immich** (job queries and both manual refreshes enforce it). Changing a date, or
  marking it approximate, clears the derived track/auto-location/photos and re-queues; a manual location is kept.
- **Filename info** stored per resource: profile, description text (title suggestion), `is_edit` (a trailing
  "-EDIT"), file counter (`seq`), the Inbox subfolder (the only context an `STE-000` has), unknown-date reason.
- **API:** `PATCH {use_suggested_date: true}` confirms a suggestion (date-only names become approximate);
  `POST /api/filename-preview {filename}` shows what ingest would do (nothing stored).
- **UI:** the detail screen shows source + precision badges, an exact/approximate switch, the suggestion with a
  "Use this date" button, the unknown-date reason, and the filename description; the queue labels
  "(from filename)", "(approx.)" and pending suggestions. (JS syntax-checked and pages return 200; **not yet
  clicked through in a browser.**)
- **Schema:** no migration tool exists, so `app/schema.py::ensure_schema` adds columns idempotently at web and
  worker start (and backfilled the existing dated resource to `exact`). **Deploy order: restart web first.**
- Verified: 52 unit tests (every fixture filename has an expected result) + a 34-check live run (real
  ingest, real Dawarich enrichment gating). Weak spot: "same-value PATCH doesn't reset" only asserts HTTP 200.
- **Not built yet:** no UI for editing profiles (API only); no mtime suggestion (deliberately, see PLAN 18.5b);
  multitrack/split grouping and using `is_edit`/`seq` (work packages 3-5); DST ambiguity flag.

## Built 2026-09-21: Project -> Session -> File (work package 3)
No migration was needed (the user confirmed only test data exists), so the change is additive.
- **Sessions** (`sessions` table, model `RecordingSession`): a night of a show or an outing. `project_id` is
  nullable (a loose outing has none), `name` is the NAS folder name, optional `session_date`, `notes`. Unique per
  project by folder name **compared case-insensitively after sanitising** ("a/b" and "a\\b" clash; SMB is
  case-insensitive). `GET/POST /api/sessions`, `GET/PATCH/DELETE /api/sessions/<id>` (delete refused while it has
  files); moving a session to another project moves its files' project with it; `GET /api/resources?session_id=`.
- **Files** gained `session_id`, `role` (original|edit|export|sidecar|project-file), `derived_from_id` (edits point
  at their original; self and loops refused; pointing at one makes the file an edit), `track_label`, `notes`.
  **Rule: a file's project is its session's project.** Giving a session sets the project from it; a contradicting
  project is refused; moving a file to another project takes it out of its old session.
- **Projects:** `notes`, `placement` (nas|drive|both) and `home` (nas|drive) stored and validated (home must be
  possible for the placement) but **not yet acted on** (package 9). Slug validated (`[a-z0-9-]`, it becomes a folder)
  and **immutable**. **Client-supplied UUID `id` makes project and session creation idempotent** (the retry-safe
  create the phone app will need).
- **NAS paths:** `{project}/{session}/{filename}` and loose `misc/{category}/{year}/{month}/{session}/{filename}`
  (the session folder is dropped when there is none; "unknown" for a missing date). Every folder part goes through
  `safe_component` (no separators/reserved characters, no leading/trailing dots or spaces, never `..`); the
  **original filename is never altered**. A filename clash in one folder is refused (409), never overwritten.
- **UI:** the detail screen has a Session picker (sessions of the file's project), "+ New" (pre-filled with the
  captured date, client-generated id), session notes, and file notes (saved on blur). (JS syntax-checked, page
  serves 200; **not clicked through in a browser.**)
- **Decision made by me, please confirm:** a loose recording in a session (an outing) gets that session's folder
  under the month folder; a loose take with no session sits directly in the month folder.
- **Safety change:** the nightly `audio-manager-refile-all.timer` is now **disabled** (unit file kept in
  `deploy/systemd/`). Renaming a session or project changes the rendered path, and an unattended nightly move would
  silently break Reaper projects, which reference audio by relative path. `refile-all` is now a deliberate manual run
  (`POST /api/jobs/refile-all/run`); it refuses to overwrite and reports skips. Re-enable with
  `systemctl enable --now audio-manager-refile-all.timer`.
- Verified: 64 unit tests + a 46-check live run on the real NAS (filing paths, collisions, idempotent creates,
  structure rules, edit chains). Test rows and NAS folders were removed afterwards.
- **Not built:** a manage screen for projects/sessions (package 8); adopting DAW project files and moving a project
  folder as a unit (package 5/9); split/multitrack grouping suggestions; the placement behaviour.

## Built 2026-09-21: Inbox hardening + background filing (work package 5, first half)
- **Filing is now a background job** (`jobs/filing.py`, job `file-resources`). Copying + verifying + reading back
  a 700 MB file takes longer than nginx will wait, so `PATCH status=filed` only validates and sets **`filing`**;
  the worker drains every `filing` resource. States: pending-review -> filing -> filed | failed (stage `move`,
  with the reason and the file still in staging; `retry-failed` or filing again re-queues it). A dropped NAS
  leaves resources in `filing` and stops the run (not a recording failure). A client can no longer set
  `filing`/`failed` itself. The detail screen polls while filing. A **5-minute timer**
  (`audio-manager-file-resources.timer`, installed and enabled) sweeps anything stranded.
- **Inbox pull** (`drive_inbox_pull`, `jobs/inbox_rules.py`): now **recursive** (`rclone lsjson -R`), never scans
  `_processed`, and **classifies** each file: audio -> ingested; sidecar (`.reapeaks`, `.pkf`) -> attached to its
  audio (matched by Inbox folder + name, case-insensitive; held in the Inbox until the audio exists);
  **project files (`.RPP`, `.sesx`, ...) are left in the Inbox and reported, not imported** (where one belongs
  depends on where its audio ends up; adoption from the NAS is a later feature); junk (`.DS_Store`, `~$`, temp
  files) and unknown types are ignored and counted.
- **Each Inbox file is staged in its own directory** (`staging/inbox/<hash of its Inbox path>/`): `STE-000.wav`
  exists in several folders and one shared directory would let them overwrite each other. Quarantined
  duplicates are prefixed with a checksum so two duplicates with one name can't overwrite either. The Inbox folder is
  kept on the resource (`filename_info.folder`).
- **Sidecars** are `role=sidecar` resources (`derived_from` = the audio, status `attached` -> `filed`), copied into
  the audio's NAS folder right after it and never overwriting; hidden from the review queue and the library
  (`GET /api/resources` excludes sidecar/project-file unless `role=` is given). A failed sidecar never fails
  the recording.
- Verified: 74 unit tests + a 23-check live run (fake rclone; real DB, NAS and **worker**): two `STE-000.wav`
  from different folders, sidecar attach + copy, orphan sidecar held, project file held, junk ignored,
  background filing, name clash fails safely and re-files after a session is chosen.
- **Still to build for the archive import:** batch review actions (next), and the NAS backup on the user's side.

## Built 2026-09-21: batch review (work package 5, second half)
- **`POST /api/resources/batch`** `{ids, patch, tags_add}`: one change applied to up to 500 recordings; each item
  is applied and committed on its own (a failure rolls that item back completely and is reported with its reason;
  the rest continue). `tags_add` adds to existing tags; `use_suggested_date` uses each file's own suggestion;
  `status: filed` queues the background filing job. Per-file fields (notes, track_label, role, derived_from_id)
  are refused. The single-file PATCH and the batch share one function (`_update_resource`), so the rules can't drift.
- **Review queue UI:** a checkbox on every card, "Select all", and a bar above the tab bar to set category, project,
  session (existing, or "+ New session" created with a client id), tags, "Use suggested dates" (only sent for files
  that have one) and "Apply & file" (only files with a category). Shows how many are being copied and links to the
  failed ones. JS syntax-checked and the page serves; **not clicked through in a browser.**
- **Regression suite:** the live checks are kept in `tests/live/` (README explains how to run them on the
  container). Unit tests: 74. All live checks pass after the refactor.

## Built 2026-09-21: waveforms, listening copies and the player (work package 6, first half)
- **`generate-previews` sweeper job** (`jobs/previews.py`) makes, per recording, a **waveform** and a compressed
  **listening copy**, cached on **local disk by the file's sha256** (`/var/lib/audio-manager/{waveforms,previews}/<aa>/<sha>`):
  refiling/renaming can't invalidate them, an edit is a new file so it can't go stale. Triggered when the Inbox pull
  brings files in, when a client asks for one that's missing, and by a 10-minute timer (installed) for backlogs; each
  run stops after `PREVIEW_RUN_SECONDS`. NAS-hosted files are skipped (not failed) while the NAS is down.
- **Waveform:** `ffmpeg` (mono, 80 Hz high-pass, no resample) -> `audiowaveform` -> native 8-bit `.dat`, one dense
  tier at 100 peaks/s (about 180 KB per 15 minutes). The header inside carries the sample rate and samples-per-pixel,
  so the browser maps time exactly and there is no playhead drift. **No fallback engine:** a missing/failed
  `audiowaveform` is recorded on the resource (`waveform_error`) and logged, never masked. A `.json` beside each records engine,
  version, source checksum and parameters. Timeouts, `nice`, both processes killed on timeout, partial files removed.
- **Listening copy:** AAC in `.m4a`, 160 kbps stereo / 96 kbps mono (`PREVIEW_BITRATE_KBPS`; **quality is the user's
  call, not yet asked**). Real 15-minute file: 18 MB vs 346 MB, 16 s to make; waveform 2.5 s.
- **API:** `GET /api/resources/<id>/waveform` (binary) and `/preview` (audio, Range) return 202 `generating` while
  pending and start the job, 500 `failed` + reason after a failure, self-heal if the cache file vanished;
  `POST .../previews/regenerate` retries. Resources report `has_waveform`, `has_preview`, `*_error`, `size_bytes`
  (backfilled for existing rows by the sweeper).
- **Detail screen:** a waveform on a canvas (**my own renderer, not Peaks.js**: no downloaded library, no LGPL, no CDN, and
  with one dense tier zooming is just choosing which slice to draw). Tap to seek, drag to pan, two-finger pinch or
  ctrl+wheel or +/- to zoom, Fit; follows the playhead when zoomed; auto-gain for quiet files; dark-mode aware; the
  player uses the listening copy when it exists. **Not run in a browser.** What was verified without one: the page's own
  parser against the real `.dat` in Node, and the peaks against an independent ffmpeg measurement of the same audio
  (log-level correlation **0.987**, 0.972 linear; the 5 loudest moments align to +/-0.2 s; agreement falls steadily as the
  series are shifted apart). The recording is 32-bit float with overs (up to 3.4x full scale) that clip at full height in
  the 16-bit pipeline; that only affects display.
- Verified: 87 unit tests (incl. a silence-then-tone file proving the primary engine ran and the shape is right) and a
  16-check live run through the real worker (`tests/live/live_previews.py`).
- **Not built yet from package 6:** the reclaimable-space view.

## Work packages (proposed order; each ends with something checkable)
Items marked **[app]** are backend work the Android app (PLAN §19) needs;
they are scheduled here on purpose, early, and each also benefits the web UI.
The app itself is still not to be built until explicitly asked.

1. **Time contract + location fixes** (small). `jobs/ingest.py`
   `_extract_timestamp` (drops offsets), `jobs/dawarich.py` ~line 73
   (`fromisoformat` fails on integer epochs), `app/api.py` `_resource_to_dict`
   (serialise with Z) and `update_resource` (accept Z, store naive UTC; stale
   tag id puts None in tags), `refresh_location` (appends TrackPoints; must
   replace, and must not overwrite a manual Location), detail-screen round trip
   (`resource_detail.html`: 10:00 BST reads back 09:00). *Check:* enter
   09:11 BST -> stored 08:11Z -> reads back 09:11; a Dawarich refresh returns
   the Stoke pin, twice, without duplicating points.
2. **Filename patterns + recorder profiles.** Tables + a small matcher using
   the fixtures file as test cases (unit tests, no DB). Produces suggested
   date/precision/title-from-remainder/track label. **[app]** the same matcher
   serves the app's "recorder profile per card". *Check:* every fixture line
   parses to the expected result or to "unknown".
3. **Project/Session/File restructure + migration** of the existing rows;
   roles, `derived_from`, notes columns, placement/home fields. **[app]** give
   projects and sessions a **client-generated UUID** so creates are idempotent
   (retry-safe, works offline), and add create/list **project + session API**.
   One migration, reversible. *Check:* the existing resource survives; the
   library still loads; POSTing the same project twice with one client id
   yields one project.
4. **[app] API v1 + device authentication.** Put the API under a version
   prefix (keep old paths working), add a `devices` table with per-device
   tokens (create/revoke on the settings page), an auth decorator applied
   per route (PLAN §16: addable per route, no IP trust), rate-limit-free
   because VPN-only. Replaces the single shared `UPLOAD_API_KEY` for new
   clients. *Check:* a revoked token is refused; the web UI still works.
5. **Inbox hardening + [app] Upload API v2** (shared ingest path).
   Inbox: recursion, sidecar rules, extension ignore list, batch review
   actions. Upload: **chunked/resumable** upload, a **metadata block**
   (project/session by client id, category, tags, title/notes, recorder
   profile, source path, optional batch date override stored as approximate),
   a **"have this checksum?"** query, **disk admission** on uploads
   (`jobs/disk_budget.py`, answer 503 + Retry-After when full), Flask
   `MAX_CONTENT_LENGTH` and nginx `client_max_body_size` / timeouts.
   Depends on 3 and 4. *Check:* a fake tree with an `.RPP`, `.reapeaks`, a
   nested folder and an `-EDIT` file ingests correctly; a 700 MB upload
   interrupted and resumed arrives with the right sha256; a duplicate is
   skipped by the checksum query; a full staging disk returns 503.
6. **Waveform job + [app] proxy audio + reclaimable-space view.** One job
   pattern, one cache keyed by sha256: the `.dat` peaks and a compressed
   **playback proxy** (AAC/Opus ~96-128 kbps, format/quality to decide),
   served with HTTP Range; the web player switches to the proxy too (it
   currently streams the raw original). *Check:* peaks match a full-band
   envelope (~0.98 correlation); proxy plays and seeks (206) through nginx;
   run time and proxy size per hour measured.
7. **[app] Export API for a device.** The export/convert workflow (PLAN §15)
   is already API-first; make it callable with a device token, add progress
   and resumable download, exclude sidecars/project files. **Decide the
   parked private flag first.** *Check:* request an mp3 of a clip with a
   token, poll, download, checksum matches a local ffmpeg run.
8. **Categories as data, manage page (tags/projects/categories), Home page,
   global map, place names.**
9. **Placement + per-file copies + Drive remote decision** (only after the
   Drive question below is answered).
10. **Segments UI (Peaks.js), then the transcription worker.**

Cheap wins any time (each under an hour): pause/disable the failing hourly
`nas-to-drive-library` timer; put the disk-budget limits on the settings page;
set Flask `MAX_CONTENT_LENGTH` and nginx `client_max_body_size`; update
DEPLOYMENT.md for the Library/disk-queue/audiowaveform work.

## Storage gaps found 2026-09-21 (decide/do before the packages noted)
- **Backup: the user manages it externally to the app (decided 2026-09-21).**
  The app builds no backup feature and does not verify one. Consequences:
  the app cannot know whether a file is backed up, so the reclaimable-space
  view (WP6) offers originals whose NAS copy is checksum-verified and shows a
  plain warning that deleting the Drive original is safe only if the user's
  own backup covers the NAS; consider a one-time "I have an external backup"
  acknowledgement in settings before the view offers anything. The audio
  mount is outside vzdump/replication (`backup=0,replicate=0`) and ds124 also
  holds the PBS datastore, so the user's backup must target the NAS share
  itself. Still worth doing before the archive import, but it is the user's
  task, not a build item.
- **NAS mount guard (before the mount goes live).** `/mnt/nas/audio` is now a
  plain directory on the 16 GB root disk. Once NFS is mounted, a dropped mount
  would let filing write to the local disk. Filing, copying and verify jobs
  must first confirm the path is a live mount (e.g. `os.path.ismount` plus a
  marker file that only exists on the NAS) and refuse otherwise. Also confirm
  filing copies, verifies the checksum, and only then removes the staging file
  (not checked in the code yet).
- **Filename collisions (before WP3).** Originals are never renamed, but names
  like `STE-000.wav` repeat. Proposed: `{project}/{session}/{filename}` with
  the session folder as the namespace (e.g. `2026-09-15 Night 2`); a collision
  inside one session is refused and shown to the user, never overwritten.
- **Drive write access only matters for NAS-home projects copied to Drive.**
  Drive-home projects flow Drive -> NAS over the existing read-only service
  account. So the Drive remote choice can wait until WP9.
- **NAS is MOUNTED (2026-09-21).** ds124 = 192.168.1.2, ~3.2 TB free of 11 TB,
  shared with Plex, Photos, Proxmox backups and the PBS datastore. Proxmox
  storage `audio-library`; the library is the `library/` subfolder, bind-mounted
  at `/mnt/nas/audio` (`replicate=0,backup=0` — LXC 132 is replicated to pmx1,
  which otherwise refuses the mount). Existing file migrated and verified.
  Details in DEPLOYMENT.md. **Mount guard BUILT** (`jobs/nas.py`, see below). Backup is the user's,
  outside the app.

## Built 2026-09-21: NAS mount guard + safe filing (`jobs/nas.py`)
Mounting the NAS **broke filing**: `os.rename` from staging to the NAS fails with
EXDEV (verified). Fixed together with the guard.
- **Guard:** the library root must be a real mount point AND contain the marker
  `.audio-manager-nas`; otherwise filing (PATCH -> 503, nothing changed), the audio
  endpoint (503, not a misleading 404), `refile-all`, `verify-integrity`,
  `find-orphans`, `nas-to-drive-library` (an rclone sync from an empty unmounted
  dir would delete the Drive copy), `library-verify` and exports refuse to run.
  `GET /api/nas/status` reports it. `NAS_REQUIRE_MOUNT=0` disables it for local dev.
- **Safe filing:** copy -> fsync -> checksum vs stored -> read-back from the NAS ->
  atomic rename -> only then delete staging. Any failure leaves the source intact and
  no partial file. **Never overwrites** an existing destination (409); `refile-all`
  now skips (and reports) a destination that exists instead of silently overwriting.
- **Also fixed:** `verify-integrity` read each whole file into memory (a 690 MB
  recording on a 2 GB container) and never closed it; it is now chunked.
- Verified: 20 unit tests (`tests/`, run on the container) and a 21-check end-to-end
  run against the real mount (guard down -> 503 and unchanged; real filing; checksum
  on the NAS; Range playback from the NAS; name collision -> 409; jobs refuse when
  down). Not covered: an actual NFS outage/unmount (simulated by hiding the marker).
- Left as is: `retry_failed`'s "move" branch (nothing ever sets that failure stage).

## Questions the user can answer offline (each unblocks a package)
1. ~~Correct the wrong date on the existing resource~~ done by the user; location refreshed. (WP1)
2. Confirm one `voice` category replacing voice-personal/voice-project. (WP8)
3. Place-name source: public Nominatim with caching, or self-hosted. (WP8)
4. Drive copies: (a) second OAuth remote with `drive.file` scope [recommended],
   (b) full owner OAuth, or (c) skip Drive copies for now. (WP9)
5. Pause the failing hourly Drive sync timer now? (cheap win)
6. Where do multitrack files come from / a sample of names when available. (WP2)
7. OK for filename/mtime dates as *suggestions* confirmed at intake — assumed
   yes from "filenames often have times"; say if not. (WP2)
8. ~~Synology NFS export + mount~~ **done 2026-09-21**. Still open: UniFi
   reservation and DNS. Backup: user-managed, outside the app.

## Android app — PLAN ONLY
v1 is an **uploader for files on attached storage** (USB card reader /
recorder) that attaches key metadata (project/session, tags, date override) and
must be quick, **plus browse, playback (via a server-made compressed proxy) and
download with conversion**; recording is a later option. **Do not build the app
until the user explicitly asks.** The backend work it needs is scheduled in the
work packages above (marked [app], updated 2026-09-21 at the user's request:
"sooner rather than later"). Plan and server gap list: PLAN.md §19.

## Not decided / parked
Private flag; encryption at rest; soundcheck-vs-show sessions; tag "kind";
semantic search; git remote; nginx `client_max_body_size` and Flask
`MAX_CONTENT_LENGTH` (uploads of large files).
