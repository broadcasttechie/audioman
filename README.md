# Audio Recording Manager

A self-hosted web app for filing, browsing and working with field, show and
voice recordings. Files dropped into a Google Drive **Inbox** are pulled in,
dated and located, reviewed (in bulk if needed) and filed onto a NAS as
`{project}/{session}/` or `misc/{category}/{year}/{month}/`. From there you can
search the library, listen through a waveform player, see where a recording was
made on a map, cut and export clips, and join split files.

It runs as a Flask + Postgres app with a Postgres-backed job queue, one or more
worker processes, and systemd timers for the scheduled jobs.

## Docs in this repo

| file | what it holds |
|---|---|
| `NEXT.md` | **Start here.** Current state, decisions made, what's built, what's next, open questions |
| `PLAN.md` | The full design spec and its reasoning (schema, pipeline, jobs, providers, Android app plan) |
| `DEPLOYMENT.md` | How the production container, NAS mount, Drive remote and timers are set up |
| `tests/live/README.md` | How to run the live end-to-end checks and the JavaScript tests |

## Features

- **Inbox pull** from Google Drive via rclone. It recurses into subfolders,
  attaches sidecars (`.reapeaks`, `.pkf`), holds DAW project files, ignores junk
  and checks free disk space before each copy. Processed originals are moved to
  `Inbox/_processed` because a personal Drive account can't delete them.
- **Dates from filenames** using recorder profiles (grok-style patterns, per
  recorder timezone and clock offset). Each date is marked as *exact*,
  *approximate* or *unknown*, and only exact dates get automatic location and
  photo lookups.
- **Location and context** from swappable providers: Dawarich (GPS track),
  Photon (place names) and Immich (companion photos).
- **Review queue** with batch actions (category, project, session, tags,
  suggested dates, file) and **background filing** to the NAS. Filing copies,
  checks the checksum and reads the file back before anything is removed. It
  never overwrites, and it refuses to run if the NAS isn't mounted.
- **Project → Session → File** model with file roles (original, edit, export,
  sidecar, project-file), edit chains and notes everywhere.
- **Waveforms and listening copies** (`audiowaveform` + ffmpeg AAC), cached by
  checksum. A canvas waveform player sits at the bottom of every page, and
  `/edit/<id>` has a full-screen editor for clips and exports (original, WAV,
  FLAC, MP3).
- **Maps** without third-party JS. Each recording shows its route with a
  playhead you can click to jump to that point, and an all-recordings map shows
  clustered pins.
- **Split-file joining** (e.g. the Insta360 mic's 30-minute parts) and
  **multitrack grouping**. Both are suggestions only; joining keeps the parts.
- **Manage / Home / Reclaim** screens: projects, sessions, tags, categories
  (stored as data), an overview with health status, and the Drive space you can
  free by deleting originals already verified on the NAS.
- **Android app backend** under `/api/device/v1/...`: per-device tokens,
  resumable chunked uploads with metadata, and browse, playback and export. The
  app itself is not built (see PLAN.md §19).

## Pages

`/` Home · `/review` Review queue · `/library` Library · `/map` Map ·
`/manage` Manage · `/groups` Split/multitrack suggestions · `/reclaim`
Reclaimable Drive space · `/settings` Settings (Drive connection, provider
URLs/keys, device tokens) · `/edit/<id>` Waveform editor

## Layout

- `app/`: Flask app factory, models, schema top-ups (`schema.py`), REST API
  (`api.py`), device API and auth, UI views, templates and the page scripts in
  `static/` (waveform, map, shared helpers)
- `jobs/`: the job queue and worker, ingest, Inbox rules, filing, NAS guard,
  previews, grouping, geocoding, enrichment, exports, device uploads, providers
  and maintenance jobs
- `config.py`: every path, remote, template and tunable, each overridable by
  an environment variable
- `deploy/systemd/`: the worker unit plus a `.service`/`.timer` pair per
  scheduled job
- `tests/`: unit tests (`test_*.py`), JS tests (`tests/js/`) and live
  end-to-end checks (`tests/live/`)

## Requirements

- Python 3.11+ and PostgreSQL 15
- System packages (not in `requirements.txt`): `ffmpeg`/`ffprobe`,
  `exiftool`, `audiowaveform`, `rclone`
- A mounted library root containing the marker file
  `.audio-manager-nas`, or set `NAS_REQUIRE_MOUNT=0` for local development

## Configuration

Everything lives in `config.py` and can be overridden from the environment. In
production the environment comes from `/etc/audio-manager/audio-manager.env`,
which is not in git. The ones you'll most likely need:

| variable | purpose |
|---|---|
| `DATABASE_URL` | Postgres connection string |
| `SECRET_KEY` | Signs the OAuth-state cookie |
| `NAS_LIBRARY_ROOT`, `NAS_REQUIRE_MOUNT` | Library location and the mount guard |
| `STAGING_DIR`, `STAGING_BUDGET_GB`, `DISK_RESERVE_GB` | Where pulled files wait for review, and how much disk they may use |
| `RCLONE_DRIVE_REMOTE`, `DRIVE_INBOX_PATH` | The rclone remote and Inbox folder |
| `LOCATION_PROVIDER`, `GEOCODER_PROVIDER`, `PHOTO_PROVIDER` | Choose the provider for each, or `none` to turn it off |
| `DEFAULT_RECORDER_TIMEZONE` | The timezone assumed for wall-clock times with no offset (default `Europe/London`) |

Service URLs and API keys (Dawarich, Photon, Immich), the Drive connection and
device tokens can also be set at runtime on the Settings page. They're stored
in the database, and saved secrets are never shown again.

## Local development

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL=postgresql://audio:audio@localhost/audio_manager
export NAS_REQUIRE_MOUNT=0 NAS_LIBRARY_ROOT=$PWD/.dev/nas STAGING_DIR=$PWD/.dev/staging
python run.py                 # web app on :5000
python -m jobs.worker         # job worker, in a second shell
```

`run.py` creates missing tables and adds new columns at start-up. There's no
migration tool yet (see DEPLOYMENT.md, "Schema changes").

Run the worker as a module (`python -m jobs.worker`), not as
`python jobs/worker.py`. Running it as a script lets `jobs/queue.py` shadow the
standard library's `queue` module.

## Tests

```sh
python3 -m unittest discover -s tests                                         # unit tests
for f in test_map test_waveform test_detail_page test_editor_page; do node tests/js/$f.js; done   # JS, no dependencies
```

The unit tests need Flask and a database, so in practice they run on the
container with the env file loaded. The live checks in `tests/live/` run against
the real Postgres, NAS, worker and services. See `tests/live/README.md`.

## Deployment

Production runs on a Proxmox LXC with the NAS bind-mounted at `/mnt/nas/audio`.
You need the web service, at least one `audio-manager-worker@N` instance, and
the job timers from `deploy/systemd/`. Two timers are deliberately left
disabled: `refile-all` (it must never move audio unattended) and
`nas-to-drive-library` (it can't work with a service account). Full details,
including the Drive service-account setup and the nginx upload limits, are in
`DEPLOYMENT.md`.

## Status and open items

The MVP is built and deployed. Still open (see NEXT.md for the full list):

- Segments/tagging within recordings, then local transcription
- Non-destructive processing in the editor (PLAN.md §21)
- Placement and per-file Drive copies, which is waiting on the Drive remote
  decision
- The Android app itself
- `MAX_CONTENT_LENGTH` and the reverse-proxy body-size limits for large uploads
