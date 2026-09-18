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
  in. Not yet switched over — needs a service account created in Cloud
  Console and the `Inbox`/`Library` folders shared with its email; the
  OAuth connection still works in the meantime.

## NAS

- Decision: **Synology (ds124)**, new share `/volume1/Audio` — created.
- `/mnt/nas/audio` inside the container is currently just an **empty local
  directory**, not yet NFS-mounted — filing a resource (`status: filed`) will
  write there but it won't reach the Synology until the mount is live.
- **Blocked on DSM UI**, not automatable from here: the scoped `claude`
  account on ds124 only has sudo for `synoshare`/`synonfs` (deliberately, it
  seems) — no CLI path to grant NFS host permissions. Needs, in DSM:
  Control Panel → Shared Folder → **Audio** → Edit → NFS Permissions → Create
  → hosts `192.168.1.231`, `192.168.1.232`, `192.168.1.233` (matches the
  existing Plex/backups export convention — Proxmox hosts mount NFS, then
  bind-mount into the LXC), read/write.
- Once that's live: add `nfs: audio-library` to Proxmox storage (export
  `/volume1/Audio`, server `ds124`), then `pct set 132 -mp0
  /mnt/pve/audio-library,mp=/mnt/nas/audio` on pmx2. I can do both once the
  export exists.

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
