# Audio Recording Manager

See `PLAN.md` for the full design spec — schema, pipeline, jobs, and
open decisions. This is a backend-first scaffold: models + internal API
+ job stubs are in place; ingest pipeline wiring (steps 3-5 in PLAN.md
§4), the scheduler, and the UI are not yet built.

## Layout

- `app/` — Flask app factory, models, internal REST API
- `jobs/` — rclone jobs, maintenance tasks, path templating, Dawarich client
- `config.py` — all paths/remotes/templates/categories in one place
- `PLAN.md` — the spec this scaffold is built from

## Not yet wired up (see PLAN.md for the full list, section noted per item)

- `deploy/systemd/*.service`/`*.timer` need to actually be copied to
  `/etc/systemd/system/` and enabled on the LXC — nothing installs
  them automatically yet. This now includes `audio-manager-worker@.service`
  (§12/§14) — every job depends on at least one instance of this
  running; enable `@1` at minimum.
- Review-queue UI, including the map + waveform player + companion
  photos strip + export/format controls (backend for all of it is
  done — frontend with Leaflet + wavesurfer.js is the remaining piece).
- Duplicate-file policy (quarantine vs. discard) is a quarantine-and-log
  default in `jobs/ingest.py` — a real decision, not confirmed final.
- `ffprobe`, `exiftool`, and `ffmpeg` must be installed as system
  packages on the LXC — not in `requirements.txt`, they're not Python
  packages.
- Immich's `/api/search/metadata` and `/api/assets/<id>/thumbnail`
  `|original` paths are unconfirmed against your actual instance
  version (§13) — verify before relying on companion photos.
- `MAX_CONTENT_LENGTH` and reverse-proxy body-size/timeout limits
  aren't set for the direct-upload endpoint (§16) — fine for testing,
  needs deciding before real mobile uploads depend on it.
- The Android app itself (§16) is a proposal only — the server-side
  endpoint it would need is built; the app is a separate project.

## Local dev

```
pip install -r requirements.txt
export DATABASE_URL=postgresql://audio:audio@localhost/audio_manager
python run.py
```
