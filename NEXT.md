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

## Built 2026-09-21: Manage, Home, configurable categories, reclaimable space (work packages 8 + 6 second half)
- **Categories are data now** (`categories` table, seeded from `Config.CATEGORIES`; `ambient` is labelled **Field
  recordings**). `slug` = the NAS folder name, fixed once created; `label` renameable; archive (hidden from pickers,
  kept on existing files, can't be given to new ones) and **merge** (moves the files' category, archives the source;
  files already on the NAS stay put until a deliberate `refile-all`). `GET/POST/PATCH /api/categories`, `POST
  .../merge`. The detail screen no longer has its own hard-coded list.
- **Tags:** usage counts, rename (a clash with an existing name, any case, is refused and names the tag to merge
  into), merge (no duplicates for files that already have both), delete (refused while in use unless `?force=1`).
- **Manage screen** (`/manage`, new tab): Projects (name, notes, placement/home, sessions with name + notes),
  Tags, Categories. **Home** (`/`, new first tab, `GET /api/overview`): counts (to review / being copied / failed /
  library size), categories, recent projects, recently filed, and a health strip (NAS down, background-job errors
  from the last two days). Nav is now five tabs. Shared helpers in `app/static/app.js`.
- **Reclaimable Drive space** (`/reclaim`, linked from Settings and Home; `GET /api/reclaimable`): lists the originals
  in `Inbox/_processed` whose NAS copy exists (checked file by file), largest first, with a total and one-click copy
  of the Drive paths; `?check_drive=1` asks Drive which are still there. **The list stays hidden until the user ticks
  "I back up the NAS share myself"** (the backup is theirs, outside the app); refuses (503) when the NAS is down.
  Verified against the real Drive: the test file is still there.
- **Not clicked through in a browser:** Home, Manage, Reclaim, review-queue batch bar, detail waveform. JS syntax-checked;
  all pages serve; every API behind them is covered by the live checks.

## Built 2026-09-21: maps (per-recording route + all-recordings map)
- **A map component of our own** (`app/static/map.js`, no library, no CDN): OpenStreetMap raster tiles on a canvas
  (tile URL and attribution configurable: `MAP_TILE_URL`, `MAP_ATTRIBUTION`; the browser fetches the tiles, which is the
  only outside request; OSM answered 200 for a test tile). Pan, pinch / ctrl+wheel / +- zoom (fractional, keeps the
  point under the finger fixed), double-click zoom, crisp tiles on high-density screens, blurry-parent fallback while
  tiles load, a bounded shared tile cache, attribution shown.
- **Detail screen, Location section:** the recording's GPS **route** on the map (start green, end dark, fixes as dots), a
  **blue marker that follows the audio** along the route (interpolated between fixes by time; "follow" keeps it in
  view), and **click the route to jump the audio to that moment** (hover shows the time under the pointer; a click
  seeks; it is not clamped past the audio's length). "Look up from Dawarich" button (the refresh the user asked for
  earlier; needs an exact date; keeps a manual location; reports what it found), "Choose on map" to set a manual
  location by clicking (works for recordings with no GPS data), and "Show on the big map".
- **Map tab** (`/map`, new sixth tab): every recording that has a location as clustered pins, coloured by category, click a
  pin for a card (name, category, date, duration, link), click a cluster to zoom into it (or list them if they are in
  one place); filters for category, text search (notes/sessions/projects/tags too) and "include not yet filed";
  says how many matching recordings have no location. `GET /api/map/pins` shares the Library's filters via one helper
  (`_filtered_resources`); `GET /api/map/config`.
- **Verified without a browser:** 16 Node tests of the map maths and behaviour (`tests/js/test_map.js`: projection agrees
  with the standard tile formulas, tile selection, zoom/pan invariants, interpolation, nearest-point-on-route, clustering,
  pointer handling with a fake canvas), plus a run of the same code against the **real 13-point route** (`tests/live/`
  README): the whole route fits the map, clicking each fix jumps to that fix's time, halfway between two fixes gives halfway
  between their times, and the marker moves smoothly with no jumps. The API is covered by `live_map.py` (19 checks).
  **Not verified:** how it looks and feels in a real browser (touch gestures, tile loading, the popup placement).
- **The real test recording is a train journey:** about 8 minutes at Stoke station, then about 7 km in 4.5 minutes.
- **Not built:** place names (still needs the geocoder decision); clicking the waveform region ranges on the map; a
  heat/density mode. A recording whose date is approximate has no automatic location, so it appears only under
  "without a location" until one is chosen on the map.

## Built 2026-09-21: place names (Photon) and swappable providers; play on click; a stuck-waveform bug
- **Place names** from the user's own **Photon** (`http://photon.home.zamia.co.uk:2322`, plain HTTP on 2322, no key; the URL
  is on the **Settings page** with a "Save and test" button). A location's name is looked up by a self-healing sweeper
  (`geocode-locations`; queue = locations with no `place_checked_at`; timer every 15 min, plus a run whenever a location
  appears or moves). Photon returns the *nearest feature*, so `format_place` builds a readable label: a postcode is never the
  name, at most four parts, region only when short, the country only when abroad, and "near ..." when it had to widen the
  search to 5 km. Real results: the test file = "Railway Station Platform 1, Station Road, Shelton, Stoke-on-Trent";
  Wyre Forest = "Rock, Wyre Forest, Worcestershire"; nothing at all (mid-Atlantic) is remembered, not re-asked.
  A **typed name is never overwritten**; clearing it re-queues a lookup; a location that moves **more than 50 m** loses its
  name (GPS jitter doesn't). Shown on the recording page (editable, with a re-lookup button), Library cards, map popups,
  and searchable. An unreachable Photon is reported (Home) and nothing is lost.
- **Providers** (`jobs/providers.py`, PLAN 20): Dawarich, Photon and Immich now sit behind a registry chosen by
  `LOCATION_PROVIDER` / `GEOCODER_PROVIDER` / `PHOTO_PROVIDER` (`none` = off, unknown names are reported). Only the current
  setup is built; PLAN 20 lists candidates. The Settings page shows what is active.
- **Play on click:** clicking the route on the map now seeks *and plays*. (Tapping the waveform still only seeks, so a clip
  start/end can be placed with the position holding still; say if you want that to play too.)
- **Bug fixed (reported by the user): the waveform stayed on "Generating the waveform".** The first load checked the
  section was on the page before it had been attached, so it never fetched or retried. Reproduced with a new fake-DOM
  harness (`tests/js/`), fixed, deployed. It shipped because earlier tests covered the parser but not the page flow.
- Tests: 114 unit, 14 live suites, 20 Node checks (`tests/js/test_map.js`, `test_detail_page.js`).

## Built 2026-09-21: waveform in the bottom player, and a full-screen waveform editor (user request)
- **Bottom player** is now a custom bar: play/pause, time, a **waveform you can press or drag to scrub**, length, and an "open the
  editor" button. **Editor** at `/edit/<id>`: big waveform with ruler and overview strip, drag to select (edges adjustable),
  labelled clip regions, transport with loop and speed, typed start/end, keyboard shortcuts, and a clips list with rename / use
  selection / delete / **export (original, WAV, FLAC, MP3 through the existing worker job)**. One shared component
  (`app/static/waveform.js`) draws all three waveforms. Details and the **processing roadmap (32-bit float conversion etc.,
  non-destructive, new derived files)** are in PLAN 21; the processing itself is not built.
- **Bugs found and fixed on the way:** (1) deleting a clip that had been exported failed with a database error (exports point at the
  clip); delete now keeps the exports and detaches them. (2) The waveform is ~10 ms longer than the audio, so a clip dragged to the
  end was refused by the server; selections now clamp to the recording's real duration and round down.
- **Verification:** 50 Node tests (`tests/js/`: map 16, waveform 14, recording page 8, editor 12) that run the real page scripts under a
  small fake DOM (`tests/js/minidom.js`): scrubbing the bar, click-the-route-plays, dragging a selection, saving/renaming/deleting
  clips, keyboard shortcuts, play-a-range and loop, export flow, waveform 202/500 states. Plus `live_editor.py` (24 checks: the exact
  clip calls with the page's rounding, the end-of-recording edge, all four export formats through the real worker and download).
  **Not verified: the look and feel in a real browser.**

## Built 2026-09-22: split-file joining and multitrack grouping (PLAN 22)
- **Split chains** (the Insta360 mic's 30-minute auto-split): detected from the real confirmed
  chains and negative cases in `tests/fixtures/sample_filenames.txt`, offered at `/groups`, and on
  "Join these into one file" actually joined in the background (`ffmpeg -c copy`, no re-encode) into
  a new ordinary `pending-review` resource. **The parts are always kept**, never deleted, each
  linked to the result (`joined_into_id`); the new file records `joined_from_ids` and is
  `derived_from` the first part. A real audio-format mismatch (checked with ffprobe, not just the
  filenames) refuses the join with a clear reason rather than re-encoding silently.
- **Multitrack** (several files recorded at once): a suggestion only, never a merge — confirming
  just labels the files (`track_label`) as tracks of one take. Detected two ways: files with a real
  timestamp starting within a few seconds of each other, or (no sample files exist yet for this,
  per the user) a same-Inbox-folder-and-arrival-time fallback. Both also require matching duration
  and a shared filename prefix with a short varying remainder ("Tr1"/"Tr2", "CH01"/"CH02").
- **Nothing is ever automatic**: every group is a suggestion (`/groups`, linked from Manage and a
  Home tile) until confirmed or dismissed, and both actions stay reversible on the same group.
- Verified: 22 new unit tests against the real fixture chains (no database) + a live run on the
  real worker (an actual 3-part join with correct duration/provenance/audit trail, dismiss then
  re-confirm, a real format-mismatch refusal, a real multitrack folder-fallback confirm). Full
  regression after: 136 unit tests, all 15 live suites, 50 Node tests.
- **Known gap:** multitrack detection is still unverified against a real multitrack recording (none
  exist yet) — revisit once the user has real filenames from that kind of recorder.

## Built 2026-09-24: the waveform gets a scrollbar; the Android app's backend (PLAN 19.6)
- **Waveform scrollbar:** the recording page's zoomable waveform now shows a second thin strip
  underneath (the same overview-strip component the editor already used) once you zoom in — the
  whole recording, with the visible window outlined, drag or tap it to jump there. Hidden while
  showing the whole file, so it doesn't take up room when there's nowhere to scroll to. A Node
  test drives an actual zoom-then-drag and checks the resulting view window.
- **Android backend** (`app/device_api.py`, `app/device_auth.py`, `jobs/device_uploads.py`; the
  app itself is still plan-only, unbuilt, per standing instruction): per-device tokens managed
  from Settings ("Android app devices"); a chunked/resumable upload protocol (initiate -> PUT
  chunks at a checked offset -> complete, verifying the real assembled checksum) that admits new
  uploads through the same disk-budget check as the Inbox; a bulk "have you got this checksum?"
  query; a metadata block applied directly at ingest (project/session/category/tags/title+notes/
  a capture-time override that's approximate unless the app says otherwise); and browse/playback/
  export/download exposed under `/api/device/v1/...`, device-token-gated, by delegating straight
  into the existing web API handlers rather than duplicating them. An hourly sweeper fails and
  cleans up any upload abandoned mid-transfer.
- Verified: 9 new unit tests + a live run (`tests/live/live_device_api.py`) covering the whole
  protocol including a deliberate wrong-offset resume, a checksum-mismatch refusal, an oversized-
  chunk refusal, disk-admission refusal (with a check that a partially-resolved tag from the
  rejected request was rolled back, not left behind), every metadata validation error, delegated
  browse/waveform/export/download (and refusal without a token), and the abandoned-upload sweeper.
  Full regression after: 145 unit tests, 16 live suites, 51 Node tests, all green.
- Details, exact endpoint list and what's still open (parallel chunk upload, a dedicated `title`
  column, the private flag): PLAN §19.6.

## Fixed 2026-09-21 (found by a regression run and by looking at Home)
- **Sweepers survive a resource vanishing mid-run** (previews and filing now iterate ids and re-read each row).
- **Stranded queue rows:** a worker restarted mid-job left its row `running`, which blocked new runs of that job for
  an hour (one active row per job name). `requeue_dead_local_jobs()` runs at worker start and re-queues rows locked by
  a process on this host that no longer exists (not counted as a failure); live and other-host rows are untouched.
- **Secrets in error messages:** `requests` puts the full URL in its exception text and Dawarich takes its key as a
  query parameter, so an error message could carry the key into `file_events`/the UI. `jobs/retry.scrub()` now strips
  key-like parameters before any message is raised, and 4xx replies become `ServiceRejected("HTTP 400 from
  https://host: <reason>")` (a `ServiceUnavailable` subclass) instead of an unhandled 500. **One stored `file_events` row
  contained a key-like parameter; its value was replaced with `***`** (counted, never printed). It never left the
  server, but if you want to be thorough, rotate the Dawarich API key.
- **Immich was misconfigured:** its URL was `http://192.168.1.186:2283`, which nginx rejects ("plain HTTP request sent to
  HTTPS port"), so every photo lookup since 18 Sept failed. Changed to `https://immich.home.zamia.co.uk` (resolves to the
  same host, answers `/api/server/ping`, and an authenticated search returns 200). Photo refresh now works (none found
  for the test file's time). **This is a setting the user had entered; revert on the Settings page if wrong.**
- **`nas-to-drive-library` timer disabled** (it can never succeed: a service account has no Drive quota; see PLAN 18.5k),
  and the guard marker is now excluded from any future sync/check. Re-enable with
  `systemctl enable --now audio-manager-nas-to-drive-library.timer` once the Drive-copy design (package 9) is done.
- **Search** in the library now matches notes, session name, project name and tags as well as the filename (wildcard
  characters are literal). **Edit linking:** a file named `...-EDIT` gets a hint on its detail screen offering the
  original(s) it may belong to (same Inbox folder + same leading timestamp), one click to link it (`derived_from`,
  role `edit`); never automatic.
- Tests: 93 unit tests; 12 live checks (`tests/live/`, README) all green. The NAS-guard test now restores the job
  status it provokes, so it can't leave false errors on Home.

## Work packages: status as of 2026-09-21 (MVP reached; details in the "Built" sections above)
**MVP = the core loop:** files arrive in the Drive Inbox (subfolders, sidecars, junk handled) -> review in bulk -> filed in
the background onto the NAS in `{project}/{session}/` or `misc/{category}/{year}/{month}/` -> browse and search the
library -> open a recording with its waveform and a compressed listening copy -> manage projects, sessions, tags and
categories -> see how much Drive space can be freed. Everything is deployed, and covered by 93 unit tests and 12 live
checks (`tests/live/`).

| # | package | status |
|---|---|---|
| 1 | Time contract + location fixes | **done** |
| 2 | Filename patterns + recorder profiles + date precision | **done** (no UI to edit profiles; API only) |
| 3 | Project -> Session -> File | **done** |
| 4 | [app] API v1 + per-device tokens | **done** |
| 5 | Inbox hardening, batch review, background filing | **done**; [app] upload API v2 **done** (chunked/resumable, metadata at upload) |
| 6 | Waveform + listening copy + reclaimable space | **done** ([app] device export API **done**: browse/playback/export delegated under device auth) |
| 8 | Categories as data, Manage, Home, maps, place names | **done** (per-recording route map, all-recordings map, Photon place names, swappable providers) |
| 9 | Placement + per-file copies + Drive remote | not started (Drive-copy design unresolved) |
| 10 | Segments UI, then transcription worker | not started |

**Not in the MVP, in the order I would do them:**
1. **Segments and transcription** (waveform region tagging first; needs no ML), then the Mac/GPU transcription worker.
2. **Non-destructive processing** (PLAN §21: gain/normalise, fades, filters, mono/stereo mix, sample-rate and bit-depth
   conversion incl. 32-bit float, FLAC/MP3/Opus export) as a new step on the editor, building on the clip/export plumbing
   that already exists.
3. **Placement/Drive copies** (needs the Drive remote decision: `drive.file` OAuth vs full OAuth vs skip).
4. Smaller: DAW project-file adoption from the NAS, DST-ambiguity flag, a UI for recorder profiles, README refresh.

~~Split-file joining and multitrack/outing grouping~~ **done 2026-09-22**, see the "Built" section above and PLAN §22.
~~Package 4 -> 5 (upload v2) -> 6 (device export): the Android app's backend~~ **done 2026-09-24**, see the "Built"
section below and PLAN §19.6. The app itself is still plan-only (unbuilt), per standing instruction.

**Before the archive import:** back up the NAS share yourself; then drop the archive into the Drive Inbox in batches (the
disk-space queue paces it; nothing is imported until a file has been stable for 5 minutes; 10 files per 15-minute run).
A sensible first run is 5-10 files including one subfolder, one `.reapeaks` and one `-EDIT` file, to see it end to end on
the real Drive (the Inbox logic was tested with a fake rclone; only `lsjson -R` and `copy` of a single path were
exercised against the real remote).

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
3. ~~Place-name source~~ **done 2026-09-21: self-hosted Photon** (`photon.home.zamia.co.uk`), built and live.
4. Drive copies: (a) second OAuth remote with `drive.file` scope [recommended],
   (b) full owner OAuth, or (c) skip Drive copies for now. (WP9)
5. ~~Pause the failing hourly Drive sync timer now?~~ **done 2026-09-21**: `nas-to-drive-library` timer disabled
   (it could never succeed — service accounts have no Drive quota).
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
