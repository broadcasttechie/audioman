# Deployment status

## Container

- Proxmox LXC **132** on **pmx2** (`pct config 132`), Debian 12, unprivileged
- Hostname: `audioman.home.zamia.co.uk`
- Current address: `192.168.1.34` (DHCP — **not yet reserved**, see below)
- MAC: `BC:24:11:D3:66:A7`
- 2 cores / 2048MB / 16GB rootfs (`local-zfs`)
- App code: `/opt/audio-manager` (deployed from this repo's working tree)
- Python: venv at `/opt/audio-manager/.venv`
- Config/secrets: `/etc/audio-manager/audio-manager.env` (root:root, 640 — not in git)

## Running and verified

- `audio-manager-web.service` — Flask app on `:5000`
- `audio-manager-worker@1.service` — job queue worker
- All 9 job timers enabled and firing on schedule
- Postgres 15, db `audio_manager` (UTF8/en_GB.UTF-8 — the auto-created cluster
  defaulted to SQL_ASCII/C before locale was generated; recreated properly)
- End-to-end smoke test: uploaded a synthetic WAV via `POST /api/ingest/upload`
  → checksum + ffprobe + timestamp check ran, landed as `pending-review` with
  correct duration and no fabricated timestamp. Test resource then deleted.
- Verified job-level resilience live: `drive-inbox-pull` / `nas-to-drive-library`
  fail cleanly (rclone remote not configured yet — see below) and report
  `status: error` via `/api/jobs/<name>/status` without crashing the worker,
  exactly as PLAN.md §12 describes.

## Settings page

`http://192.168.1.34:5000/` now redirects to `/settings` — a page for the
config items that make sense to edit at runtime rather than only via
`audio-manager.env` + restart: Dawarich URL/key, Immich URL/key, the upload
API key, and a Google Drive connect flow (paste the JSON token from
`rclone authorize "drive"`, run on a machine with a browser since this
server has none — the app never performs the OAuth grant itself). Backed by
a new `settings` DB table; secrets are never echoed back once saved, only
reported as set/unset. See `app/settings.py`.

## nginx / HTTPS (being set up separately)

Root now returns something (redirect to `/settings`) instead of 404ing, so
a reverse proxy has a home page to hit. Two things whoever configures nginx
needs to know, both from PLAN.md:
- **Body size + timeout**: nginx's defaults (~1MB, 60s) will silently reject
  real audio uploads through `/api/ingest/upload` before Flask even sees
  them — needs `client_max_body_size` raised generously and a longer
  `proxy_read_timeout`.
- Flask's own `MAX_CONTENT_LENGTH` (config.py) is still unset — independent
  of nginx's limit, deliberately left as a real decision for whoever sets
  a realistic max recording size, not a guessed default.
- This also revisits PLAN.md's explicit "VPN-only, no reverse proxy/TLS
  assumed" access decision — worth updating that section once nginx/HTTPS
  is actually in place, so PLAN.md doesn't go stale on this point.

## Bugs found and fixed in the deploy artifacts (not application logic)

- `jobs/worker.py` was invoked as `python3 jobs/worker.py`, which puts `jobs/`
  at the front of `sys.path` — `jobs/queue.py` then shadows the **stdlib**
  `queue` module, breaking `requests`/`urllib3` on import. Fixed by running it
  as `python3 -m jobs.worker` (with `WorkingDirectory=/opt/audio-manager`)
  instead of a direct script path, in `audio-manager-worker@.service`.
- No systemd unit existed for the Flask web process itself, even though every
  job `.service` curls `127.0.0.1:5000`. Added `audio-manager-web.service`
  (not in the original zip).
- Neither unit had `EnvironmentFile=` wired up — added
  `/etc/audio-manager/audio-manager.env` to both.
- `rclone config create ... --non-interactive` with a token supplied still
  walks a short post-config wizard for the `drive` backend (confirmed by
  hand: "already have a token, refresh it now?" then "configure as a
  Shared/Team Drive?") rather than completing in one call — the first real
  OAuth callback hung on this for 30s and 500'd. `_write_rclone_token` in
  `app/api.py` now drives rclone's `--continue --state --result` protocol
  properly (answering "no" to both, with a hard iteration ceiling and no
  guessing on an unrecognized question).

## Google Drive

- **OAuth client created** (Client ID/Secret in the `settings` DB table, not
  git) — redirect URI `https://audioman.home.zamia.co.uk/api/settings/rclone/drive/oauth/callback`.
  Connected successfully once the rclone-wizard bug above was fixed.
- **Folder-level restriction**: OAuth scopes are all-or-nothing (no
  per-folder scope), so a service-account option was added to the settings
  page as the recommended path — `POST /api/settings/rclone/drive/service-account`
  (paste the downloaded JSON key; validated for `type: service_account` +
  `client_email` before writing). The key is stored as its own file
  (`/etc/audio-manager/gdrive-service-account.json`, 600 root:root), not
  inline in rclone.conf, since unlike an OAuth token it doesn't expire.
  `rclone config create` on an existing remote fully replaces it (confirmed
  by hand), so switching auth methods never leaves stale OAuth fields mixed
  in. **Done** — connected as `audio-manager@hass-355519.iam.gserviceaccount.com`.
- **Root folder**: sharing a parent folder ("Audio recording", containing
  `Inbox`/`Library`) does grant access to its contents, but nested items
  don't surface at the connection's own top level (a service account has no
  Drive of its own; an OAuth user's top level is their My Drive root, not
  what's shared with them) — needs `root_folder_id` pointed at the shared
  parent, OR `shared_with_me = true` if `Inbox`/`Library` are shared
  individually instead of via a common parent (which is what actually
  happened — see below). Added a proper browsable picker to the settings
  page instead of requiring a pasted folder ID: `GET .../drive/folders[?parent_id=]`
  lists folders using one-off `--drive-root-folder-id`/`--drive-shared-with-me`
  flags that never touch the persisted config; `POST .../drive/root-folder`
  is the only thing that does, via `rclone config update` (merges, unlike
  `create` — confirmed by hand the service account field survives).
- **Personal Google accounts can't delete files they don't own, even with
  Editor sharing** — hit this live as a real ingest failure (403
  `insufficientFilePermissions`), not a sharing misconfiguration. Editor
  grants read/write but not delete on a non-owned file; only Shared Drives
  (a Workspace-only feature, not available on personal Gmail) let a
  non-owner genuinely delete. Re-parenting a file you don't own works fine
  under Editor, though, so `drive_inbox_pull` (`jobs/rclone_jobs.py`) now
  copies out + `rclone moveto`s the source into `Inbox/_processed` instead
  of deleting it. Verified end-to-end with a real 330MB file: copy, ingest
  (landed correctly in the Review Queue), re-parent into `_processed`, and
  confirmed `_processed` itself gets filtered out of future listings
  (`--files-only`) rather than being mistaken for a pending upload.
  Non-fatal failure mode if the re-parent step ever fails: the file just
  gets re-copied and quarantined-as-duplicate every poll until someone
  notices (logged to `file_events`, not silent data loss) — see the
  cleanup-visibility note below.
- Also mid-saga: you unshared/re-shared partway through, which pointed
  `root_folder_id` at a now-inaccessible folder — fixed by switching to
  `shared_with_me` (see above). **Overall: done** — Inbox pull is fully
  working with a service account under a personal Google account's real
  constraints.

## Cleanup visibility (not built yet — noted, not implemented)

Several things in this app can end up in a "needs a human to notice and fix"
state without failing loudly: the `_processed` re-parent above, a resource
stuck in `failed` status, a job reporting `error`/`partial`. Right now these
are only visible if you go looking (`file_events`, `/api/jobs/*/status`,
`/api/resources?status=failed`). Worth a small aggregating mechanism —
e.g. a `GET /api/needs-attention` that rolls these up, surfaced as a badge
somewhere in the UI nav — rather than each one being its own silent trail.
Not built; flagging so it doesn't get lost.

## refile-all is manual-only

`audio-manager-refile-all.timer` is **deliberately disabled** (2026-09-21). Renaming a session or project changes
the path a file should have, and re-filing unattended overnight would move audio that a Reaper/Audition project
refers to by relative path. Run it on purpose with `POST /api/jobs/refile-all/run`. The unit files remain in
`deploy/systemd/`; re-enable with `systemctl enable --now audio-manager-refile-all.timer`.

## Schema changes (no migration tool yet)

`db.create_all()` (in `run.py`) creates missing tables but never adds columns to an existing one, so
`app/schema.py::ensure_schema` adds new columns with `ADD COLUMN IF NOT EXISTS` at web and worker start.
**Restart the web service first** when deploying a change that adds columns, then the worker.
A proper reversible migration arrives with the Project/Session/File restructure (NEXT.md package 3).

## NAS

**Mounted and live (2026-09-21).** Synology ds124 (192.168.1.2), share
`/volume1/Audio`, NFSv3 exported to the three Proxmox hosts
(192.168.1.231/.232/.233, read/write, squash to admin).

- Proxmox: cluster-wide storage `audio-library` (`pvesm add nfs audio-library
  --server ds124 --export /volume1/Audio --content snippets --options
  soft,timeo=100,retrans=2`), mounted on the hosts at `/mnt/pve/audio-library`.
  `content snippets` was only to satisfy Proxmox; it created an unused
  `snippets/` folder at the share root.
- The library tree lives in the **`library/` subfolder** of the share, which is
  bind-mounted into the LXC: `pct set 132 -mp0
  /mnt/pve/audio-library/library,mp=/mnt/nas/audio,backup=0,replicate=0`.
  **`replicate=0` was required**: LXC 132 has a storage-replication job
  (132-0 -> pmx1), and Proxmox refuses a non-replicatable volume without it.
  The mount is therefore not replicated and not in vzdump backups — the audio
  is protected only by the user's own backup of the NAS share, managed
  outside the app (decided 2026-09-21).
- `/mnt/nas/audio/.audio-manager-nas` is a marker file that only exists on the
  NAS; the planned **mount guard** (not built) should require it before any
  filing/copy/verify.
- Migration: the one existing filed file was copied from the container's root
  disk to the NAS, checksums matched, then the local copy was removed.
  Verified after the reboot: mountpoint present, container writes land as the
  squashed admin user, the audio endpoint returns 206 from the NAS file, the
  library lists the file with its unchanged path, and `verify-integrity`
  reports no mismatches.
- **Mount guard and safe filing are live** (`jobs/nas.py`): mounting the NAS had
  broken filing (cross-device rename), now fixed as copy -> verify -> delete; see
  NEXT.md for the full list of what refuses to run when the NAS is not mounted.
  Check it with `GET /api/nas/status`.
- Mount options follow the existing Plex/backups storages (`soft`). A soft mount
  can return an I/O error to a write rather than hang; filing should copy,
  verify the checksum, then delete the staging file (not yet confirmed in the
  code).

## Still needed (not things I can/should do unilaterally)

- **UniFi**: DHCP reservation for the MAC above, plus local DNS
  `audioman.home.zamia.co.uk` → reserved IP (per cluster convention: LAN-only
  DNS, no public record). I don't have UniFi access from here.
- **Google Drive OAuth** — client is registered, just needs re-approval at
  `/settings` → Connect with Google Drive (see "Google Drive" section
  above for why the first attempt didn't stick). Requires your consent in a
  real Google auth flow; I won't drive this myself.
- **Dawarich API key** (CT 128, pmx2, guessed at `http://192.168.1.143:3000`
  — port not confirmed) — still needed; **Immich's is now set** (entered via
  the settings page). Same offer as before: I have infra access to Dawarich
  and could generate its key myself, but haven't without checking first.
- **PBS backup + ZFS replication**: deliberately deferred — the NAS bind
  mount (once added) makes this guest ineligible for replication, same as
  Immich already is. Worth folding into `/etc/pve/jobs.cfg` once the NAS
  mount and rclone remote are both in place, not before.
- **TLS cert** via the cluster's certbot + Cloudflare DNS-01 convention —
  optional given PLAN.md's VPN-only access decision; say if you want it
  anyway for browser trust.
- **Review-queue UI** — not started, the other major piece of this session's
  original task.
