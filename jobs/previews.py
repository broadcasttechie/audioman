"""
Derived files for playback: the waveform (peaks) and a compressed listening copy ("preview").

Both are generated in the background by the `generate-previews` sweeper job and cached on LOCAL disk
(never the NAS: no network latency on every check, no competing with the audio itself), keyed by the
file's sha256. Content-keyed means: refiling or renaming never invalidates them, an edited file is a new
file with a new checksum so it can't go stale, and they are deleted with their resource.

Waveform
  ffmpeg (mono mix, 80 Hz high-pass so sub-bass rumble and DC don't dominate the peaks, NO resampling)
    | audiowaveform  -> native 8-bit `.dat`, one dense tier of about PEAKS_PER_SECOND peaks per second.
  The header inside the `.dat` carries the sample rate and samples-per-pixel the data was built with, so
  the browser never recomputes them (recomputing a rounded samples-per-pixel is what makes a playhead
  drift, PLAN 18.5i). There is deliberately NO silent fallback engine: if audiowaveform is missing or
  fails, the job records the error against the resource and says so, instead of quietly producing a
  different-looking waveform. The engine and versions are recorded in a `.json` next to each `.dat`.

Preview
  AAC in .m4a (plays everywhere, seeks with HTTP Range): a 30-minute stereo file is about 35 MB instead of
  the ~700 MB of a 32-bit float WAV, which matters on a phone. Original files are never touched.

Every subprocess has a timeout, is niced, and a failed step removes its partial output.
"""
import json
import logging
import os
import struct
import subprocess
import tempfile
import time
from datetime import datetime

from config import Config
from app.extensions import db
from app.models import Resource, JobRun, FileEvent
from .nas import nas_status, is_nas_path

log = logging.getLogger(__name__)

GENERATOR_VERSION = 1
_HEADER_V1 = struct.Struct("<iIiiI")          # version, flags, sample_rate, samples_per_pixel, length
AUDIO_ROLES = ("original", "edit", "export")
LIVE_STATUSES = ("pending-review", "filing", "filed")


class GenerationError(Exception):
    pass


# ---------------------------------------------------------------- cache locations

def cache_file(kind, checksum, ext):
    base = Config.WAVEFORM_DIR if kind == "waveform" else Config.PREVIEW_DIR
    return os.path.join(base, checksum[:2], f"{checksum}.{ext}")


def waveform_path(checksum):
    return cache_file("waveform", checksum, "dat")


def preview_path(checksum):
    return cache_file("preview", checksum, "m4a")


def delete_cache(checksum):
    for p in (waveform_path(checksum), waveform_path(checksum)[:-4] + ".json", preview_path(checksum)):
        try:
            os.unlink(p)
        except OSError:
            pass


# ---------------------------------------------------------------- waveform

def read_waveform_header(path):
    """Parse an audiowaveform `.dat` header. Raises GenerationError if it isn't a sane one."""
    with open(path, "rb") as f:
        raw = f.read(24)
    if len(raw) < _HEADER_V1.size:
        raise GenerationError("waveform file is shorter than its header")
    version, flags, rate, spp, length = _HEADER_V1.unpack(raw[: _HEADER_V1.size])
    channels, header_size = 1, _HEADER_V1.size
    if version == 2:
        if len(raw) < 24:
            raise GenerationError("truncated version 2 waveform header")
        channels, header_size = struct.unpack("<i", raw[20:24])[0], 24
    elif version != 1:
        raise GenerationError(f"unknown waveform format version {version}")
    bits = 8 if flags & 1 else 16
    expected = header_size + length * channels * 2 * (bits // 8)
    size = os.path.getsize(path)
    if rate <= 0 or spp <= 0 or size != expected:
        raise GenerationError(f"waveform header inconsistent with file size (rate={rate} spp={spp} "
                              f"length={length} channels={channels} bits={bits}: expected {expected} bytes, got {size})")
    return {"version": version, "sample_rate": rate, "samples_per_pixel": spp, "length": length,
            "channels": channels, "bits": bits, "header_size": header_size, "duration": length * spp / rate}


def probe_audio(path):
    """(sample_rate, channels, duration_seconds) of the first audio stream, from one ffprobe call."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
             "stream=sample_rate,channels,duration:format=duration", "-of", "json", path],
            capture_output=True, text=True, timeout=Config.PROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        raise GenerationError("timed out reading the audio file (is the NAS slow or unreachable?)")
    if out.returncode != 0:
        raise GenerationError("the audio file could not be read: " + (out.stderr.strip()[-200:] or "ffprobe failed"))
    data = json.loads(out.stdout)
    stream = (data.get("streams") or [{}])[0]
    duration = stream.get("duration") or (data.get("format") or {}).get("duration")
    if not stream.get("sample_rate"):
        raise GenerationError("no audio stream found")
    return int(stream["sample_rate"]), int(stream.get("channels") or 1), float(duration) if duration else None


def audiowaveform_version():
    try:
        out = subprocess.run(["audiowaveform", "--version"], capture_output=True, text=True, timeout=10)
        return (out.stdout + out.stderr).strip().split()[-1].lstrip("v")
    except (OSError, subprocess.SubprocessError):
        return None


def _nice():
    os.nice(10)


def generate_waveform(src, checksum):
    """Build the `.dat` for `src` and cache it. Raises GenerationError with the reason on any failure."""
    rate, _channels, duration = probe_audio(src)
    spp = max(1, round(rate / Config.PEAKS_PER_SECOND))
    dest = waveform_path(checksum)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    version = audiowaveform_version()
    if version is None:
        raise GenerationError("the audiowaveform program is not installed or not runnable")

    errs = [tempfile.TemporaryFile(), tempfile.TemporaryFile()]
    ff = aw = None
    try:
        ff = subprocess.Popen(
            ["ffmpeg", "-v", "error", "-i", src, "-ac", "1", "-af", "highpass=f=80", "-f", "wav", "-"],
            stdout=subprocess.PIPE, stderr=errs[0], preexec_fn=_nice)
        aw = subprocess.Popen(
            ["audiowaveform", "--input-format", "wav", "-i", "-", "--output-format", "dat",
             "-z", str(spp), "--bits", "8", "-o", tmp],
            stdin=ff.stdout, stdout=subprocess.DEVNULL, stderr=errs[1], preexec_fn=_nice)
        ff.stdout.close()  # so ffmpeg gets SIGPIPE if audiowaveform dies early
        try:
            aw.wait(timeout=Config.WAVEFORM_TIMEOUT_SECONDS)
            ff.wait(timeout=60)
        except subprocess.TimeoutExpired:
            raise GenerationError(f"waveform generation timed out after {Config.WAVEFORM_TIMEOUT_SECONDS}s")
        finally:
            for p in (ff, aw):
                if p and p.poll() is None:
                    p.kill()
        for f in errs:
            f.seek(0)
        ff_err, aw_err = (f.read().decode("utf-8", "replace").strip() for f in errs)
        if aw.returncode != 0:
            raise GenerationError(f"audiowaveform exited {aw.returncode}: {aw_err[-300:] or ff_err[-300:]}")
        if ff.returncode != 0 and not os.path.exists(tmp):
            raise GenerationError(f"ffmpeg exited {ff.returncode}: {ff_err[-300:]}")
        if ff_err:
            # e.g. "Invalid PCM packet" at the end of a file cut mid-sample: real audio was still decoded.
            log.warning("waveform for %s: ffmpeg said: %s", src, ff_err[-200:])

        header = read_waveform_header(tmp)
        if duration and abs(header["duration"] - duration) > max(2.0, duration * 0.01):
            raise GenerationError(f"waveform covers {header['duration']:.1f}s but the file is {duration:.1f}s long")
        meta = {"engine": "audiowaveform", "engine_version": version, "generator_version": GENERATOR_VERSION,
                "peaks_per_second": Config.PEAKS_PER_SECOND, "source_sha256": checksum,
                "high_pass_hz": 80, "mix": "mono", "duration": header["duration"],
                "sample_rate": header["sample_rate"], "samples_per_pixel": header["samples_per_pixel"],
                "length": header["length"], "generated_at": datetime.utcnow().isoformat() + "Z"}
        os.replace(tmp, dest)
        with open(dest[:-4] + ".json", "w") as f:
            json.dump(meta, f)
        return meta
    except BaseException:
        for p in (tmp,):
            try:
                os.unlink(p)
            except OSError:
                pass
        raise
    finally:
        for f in errs:
            f.close()


# ---------------------------------------------------------------- preview

def generate_preview(src, checksum):
    """Compressed AAC listening copy. Returns the file size."""
    rate, channels, _ = probe_audio(src)
    kbps = Config.PREVIEW_BITRATE_KBPS if channels >= 2 else max(64, Config.PREVIEW_BITRATE_KBPS * 3 // 5)
    dest = preview_path(checksum)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part.m4a"
    cmd = ["ffmpeg", "-v", "error", "-y", "-i", src, "-vn", "-c:a", "aac", "-b:a", f"{kbps}k"]
    if channels > 2:
        cmd += ["-ac", "2"]
    cmd += ["-movflags", "+faststart", tmp]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=Config.PREVIEW_TIMEOUT_SECONDS, preexec_fn=_nice)
        if out.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) < 1024:
            raise GenerationError(f"ffmpeg exited {out.returncode}: {out.stderr.strip()[-300:]}")
        if out.stderr.strip():
            log.warning("preview for %s: ffmpeg said: %s", src, out.stderr.strip()[-200:])
        os.replace(tmp, dest)
        return os.path.getsize(dest)
    except subprocess.TimeoutExpired:
        raise GenerationError(f"preview generation timed out after {Config.PREVIEW_TIMEOUT_SECONDS}s")
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


# ---------------------------------------------------------------- the sweeper job

def _record_run(status, detail):
    run = JobRun.query.get("generate-previews") or JobRun(job_name="generate-previews")
    run.last_run_at = datetime.utcnow()
    run.status = status
    run.log_tail = detail[-4000:]
    db.session.merge(run)
    db.session.commit()


def _needs_work():
    return Resource.query.filter(
        Resource.role.in_(AUDIO_ROLES), Resource.status.in_(LIVE_STATUSES),
        db.or_(
            db.and_(Resource.waveform_at.is_(None), Resource.waveform_error.is_(None)),
            db.and_(Resource.preview_at.is_(None), Resource.preview_error.is_(None)),
        ),
    ).order_by(Resource.created_at.desc())


def generate_previews():
    """
    Make the waveform and preview for every recording that lacks them, newest first, until the run's
    time budget is spent (the next run continues). A failure is stored on the resource (with the
    reason) and not retried until someone asks (`POST /api/resources/<id>/previews/regenerate`).
    A missing source is not a failure while the NAS is unmounted: those are simply skipped this run.
    """
    started = time.time()
    nas_ok, _ = nas_status()
    made, failed, skipped = [], [], 0
    for r in _needs_work().all():
        if time.time() - started > Config.PREVIEW_RUN_SECONDS:
            break
        src = r.nas_path or r.staging_path
        if not src or (is_nas_path(src) and not nas_ok):
            skipped += 1
            continue
        if not os.path.exists(src):
            r.waveform_error = r.waveform_error or "the audio file is missing"
            r.preview_error = r.preview_error or "the audio file is missing"
            db.session.commit()
            failed.append(r.id)
            continue

        if r.size_bytes is None:
            r.size_bytes = os.path.getsize(src)
        for kind in ("waveform", "preview"):
            done_at = r.waveform_at if kind == "waveform" else r.preview_at
            err = r.waveform_error if kind == "waveform" else r.preview_error
            if done_at or err:
                continue
            try:
                if kind == "waveform":
                    generate_waveform(src, r.checksum)
                    r.waveform_at, r.waveform_error = datetime.utcnow(), None
                else:
                    generate_preview(src, r.checksum)
                    r.preview_at, r.preview_error = datetime.utcnow(), None
                made.append(f"{kind}:{r.id}")
            except Exception as e:  # noqa: BLE001 -- one bad file must not stop the rest
                message = str(e)[:500] or e.__class__.__name__
                if kind == "waveform":
                    r.waveform_error = message
                else:
                    r.preview_error = message
                db.session.add(FileEvent(resource_id=r.id, event_type="failed", detail=f"{kind} generation failed: {message}"))
                failed.append(r.id)
                log.warning("%s for %s failed: %s", kind, r.id, message)
            db.session.commit()

    remaining = _needs_work().count()
    status = "partial" if failed else "success"
    _record_run(status, f"generated: {len(made)}, failed: {len(failed)}, skipped (NAS down): {skipped}, still to do: {remaining}")
    return {"status": status, "generated": made, "failed": failed, "remaining": remaining}
