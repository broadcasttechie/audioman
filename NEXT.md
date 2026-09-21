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
  station. The stored `captured_at` on the one real resource (2026-09-15
  08:11) is a mis-entered date; unchanged pending the user.
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

## Work packages (proposed order; each ends with something checkable)
1. **Time contract + location fixes** (small). `jobs/ingest.py`
   `_extract_timestamp` (drops offsets), `jobs/dawarich.py` ~line 73
   (`fromisoformat` fails on integer epochs), `app/api.py` `_resource_to_dict`
   (serialise with Z) and `update_resource` (accept Z, store naive UTC; stale
   tag id puts None in tags), `refresh_location` (appends TrackPoints; must
   replace, and must not overwrite a manual Location), detail-screen round trip
   (`resource_detail.html`: 10:00 BST reads back 09:00). *Check:* enter
   09:11 BST → stored 08:11Z → reads back 09:11; a Dawarich refresh returns
   the Stoke pin, twice, without duplicating points.
2. **Filename patterns + recorder profiles.** Tables + a small matcher using
   the fixtures file as test cases (unit tests, no DB). Produces suggested
   date/precision/title-from-remainder/track label. *Check:* every fixture
   line parses to the expected result or to "unknown".
3. **Project/Session/File restructure + migration** of the existing rows;
   roles, `derived_from`, notes columns, placement/home fields. One migration,
   reversible. *Check:* the existing resource survives; library still loads.
4. **Inbox: recursion, sidecar rules, extension ignore list, batch review
   actions.** Depends on 3. *Check:* a fake tree with an `.RPP`, `.reapeaks`,
   a nested folder and an `-EDIT` file ingests correctly.
5. **Waveform job + reclaimable-space view.** Needs 3 for file identity.
   *Check:* peaks match a full-band envelope (~0.98 correlation), 206 Range
   through nginx, run time measured.
6. **Categories as data, manage page (tags/projects/categories), Home page,
   global map, place names.**
7. **Placement + per-file copies + Drive remote decision** (only after the
   Drive question below is answered).
8. **Segments UI (Peaks.js), then the transcription worker.**

Cheap wins any time (each under an hour): pause/disable the failing hourly
`nas-to-drive-library` timer; put the disk-budget limits on the settings page;
update DEPLOYMENT.md for the Library/disk-queue/audiowaveform work.

## Questions the user can answer offline (each unblocks a package)
1. Correct the wrong date on the existing resource (15th → 17th)? (WP1)
2. Confirm one `voice` category replacing voice-personal/voice-project. (WP6)
3. Place-name source: public Nominatim with caching, or self-hosted. (WP6)
4. Drive copies: (a) second OAuth remote with `drive.file` scope [recommended],
   (b) full owner OAuth, or (c) skip Drive copies for now. (WP7)
5. Pause the failing hourly Drive sync timer now? (cheap win)
6. Where do multitrack files come from / a sample of names when available. (WP2)
7. OK for filename/mtime dates as *suggestions* confirmed at intake — assumed
   yes from "filenames often have times"; say if not. (WP2)
8. Synology NFS export + mount (`nfs: audio-library`, `mp0`), UniFi reservation
   and DNS, PBS/replication. Needed before the archive import. (WP4 gate)

## Android app — PLAN ONLY
v1 is an **uploader for files on attached storage** (USB card reader /
recorder), not a recorder; recording is a later option. **Do not build the app
or server work for it until the user explicitly asks.** Plan and server gap
list: PLAN.md §19.

## Not decided / parked
Private flag; encryption at rest; soundcheck-vs-show sessions; tag "kind";
semantic search; git remote; nginx `client_max_body_size` and Flask
`MAX_CONTENT_LENGTH` (uploads of large files).
