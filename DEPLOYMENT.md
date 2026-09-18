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
- **Google Drive OAuth** — run `rclone authorize "drive"` on a machine with a
  browser, log into Google there, paste the resulting token into the new
  Settings page's Google Drive section. Requires your consent in a real
  Google auth flow; I won't drive this myself.
- **Dawarich API key** (CT 128, pmx2, guessed at `http://192.168.1.143:3000`
  — port not confirmed) and **Immich API key** (CT 131, pmx1, guessed at
  `http://192.168.1.186:2283`) — I have infra access to both containers but
  didn't generate keys inside someone else's already-running service without
  checking first. Say the word and I will, or generate them yourself via each
  app's UI and paste them into the Settings page.
- **PBS backup + ZFS replication**: deliberately deferred — the NAS bind
  mount (once added) makes this guest ineligible for replication, same as
  Immich already is. Worth folding into `/etc/pve/jobs.cfg` once the NAS
  mount and rclone remote are both in place, not before.
- **TLS cert** via the cluster's certbot + Cloudflare DNS-01 convention —
  optional given PLAN.md's VPN-only access decision; say if you want it
  anyway for browser trust.
- **Review-queue UI** — not started, the other major piece of this session's
  original task.
