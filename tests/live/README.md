# Live checks (run on the container, not on a dev machine)

These are end-to-end checks against the **real** Postgres, NAS mount, worker and (for two of them) Dawarich.
They are not unit tests and are deliberately not discovered by `python3 -m unittest discover -s tests`
(which only picks up `test_*.py`); importing one runs it.

Everything they create is named `ZZTEST...` (or a name shaped like a recorder's, for the filename tests) and is
removed in a `finally` block, including NAS files and the empty folders they made. They print `PASS`/`FAIL` per
check and `ALL ... PASSED` at the end. They never modify the one real resource, and several assert that.

Run one (from the dev machine; needs `ssh pmx2`):

    scp tests/live/live_sessions.py pmx2:/tmp/ && ssh pmx2 'pct push 132 /tmp/live_sessions.py /root/x.py &&
      pct exec 132 -- bash -c "cd /opt/audio-manager; set -a; . /etc/audio-manager/audio-manager.env; set +a;
      PYTHONPATH=/opt/audio-manager .venv/bin/python3 /root/x.py; rm /root/x.py"; rm /tmp/live_sessions.py'

| file | covers |
|---|---|
| `live_nas_guard.py` | mount guard, safe filing, refusing to overwrite, maintenance jobs refuse when the NAS is "down" |
| `live_time_and_location.py` | UTC contract over the API, tag validation, real Dawarich lookup and refresh (needs Dawarich up) |
| `live_filename_profiles.py` | recorder profiles API, ingest from filenames, suggestions, date precision, enrichment gating |
| `live_sessions.py` | projects/sessions API, structure rules, edits, NAS paths |
| `live_inbox_disk_budget.py` | the Inbox pull's disk admission (fake rclone) |
| `live_inbox_and_filing.py` | recursive Inbox pull, sidecars, held project files, background filing through the real worker |
| `live_batch_review.py` | the batch endpoint, incl. filing through the worker |
| `live_editor.py` | the calls the waveform editor makes: clips with the page's rounding and the end-of-recording edge, every export format through the worker, deleting an exported clip |
| `live_places.py` | place names from the real Photon: the queue, typed names, moving, Photon down, provider switching |
| `live_map.py` | map config/pins with the Library's filters, unlocated counts, the track data the route map draws, choosing a location on the map |
| `live_previews.py` | waveform + listening-copy generation through the worker, failures, NAS-down skipping, the real 15-minute file |

They assume the real resource `66482729-...` exists (the first test recording); update the constant if it is removed.
Running them while a real Inbox pull or filing is in progress could interleave with it; do it when the queue is idle.

## JavaScript tests (run on the dev machine, no dependencies)

    for f in test_map test_waveform test_detail_page test_editor_page; do node tests/js/$f.js; done

`test_map.js` / `test_waveform.js` test the map and waveform components with a fake canvas. `test_detail_page.js` and `test_editor_page.js`
run the real page scripts (`resource_detail.html`, `edit.html`) under a small fake DOM (`minidom.js`) and check what a user would see
happen: the bottom player, click-the-route-to-play, selecting and saving clips, keyboard shortcuts, exporting. They exist because a
shipped bug (a waveform stuck on "Generating") was invisible to tests that only covered the parser.
