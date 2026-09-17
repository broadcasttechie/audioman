"""
Export/format-conversion workflow. Creating an Export (app/api.py) is
a fast DB insert; process_exports() below — a named job, run by the
queue worker, never inline in a request — does the actual ffmpeg work.
This matters for exactly the reason everything else in this app is
async: a format conversion (re-encoding to MP3, say) can take real
time, and nothing HTTP-facing should block on that regardless of file
size.

Formats:
- "original": stream-copy (fast, lossless), REQUIRES a clip_id — a
  whole-resource "export unchanged" is just the file itself, so the
  API rejects that combination rather than doing pointless work.
- "wav" / "flac": re-encoded losslessly.
- "mp3": re-encoded with `quality` (e.g. "192k"), defaulting to
  Config.EXPORT_MP3_DEFAULT_QUALITY.

Metadata embedding (embed_metadata=True, the default) writes
date/comment/title tags into the output via ffmpeg's -metadata flag —
works well for FLAC/MP3 (proper tag containers); WAV's tag support via
ffmpeg is limited to the INFO chunk and less universally read by other
software, worth knowing if you're relying on it for WAV exports.
"""
import os
import subprocess
from datetime import datetime

from config import Config
from app.extensions import db
from app.models import Export, FileEvent

FORMAT_CODEC_ARGS = {
    "wav": ["-c:a", "pcm_s16le"],
    "flac": ["-c:a", "flac"],
    "mp3": ["-c:a", "libmp3lame"],
}


def _metadata_args(resource):
    args = ["-metadata", f"title={resource.filename}"]
    if resource.captured_at:
        args += ["-metadata", f"date={resource.captured_at.date().isoformat()}"]

    comment_parts = []
    if resource.category:
        comment_parts.append(resource.category)
    if resource.location:
        comment_parts.append(f"{resource.location.lat},{resource.location.lon}")
    if resource.tags:
        comment_parts.append(",".join(t.name for t in resource.tags))
    if comment_parts:
        args += ["-metadata", f"comment={'; '.join(comment_parts)}"]

    return args


def _run_one_export(export):
    resource = export.resource
    source_path = resource.nas_path or resource.staging_path
    if not source_path or not os.path.exists(source_path):
        raise FileNotFoundError(f"source audio missing for resource {resource.id}")

    os.makedirs(Config.EXPORTS_DIR, exist_ok=True)
    base = os.path.splitext(os.path.basename(source_path))[0]

    cmd = ["ffmpeg", "-y", "-i", source_path]
    window_suffix = ""
    if export.clip:
        cmd += ["-ss", str(export.clip.start_seconds), "-to", str(export.clip.end_seconds)]
        window_suffix = f"__{export.clip.start_seconds:.2f}-{export.clip.end_seconds:.2f}"

    if export.format == "original":
        cmd += ["-c", "copy"]
        ext = os.path.splitext(source_path)[1].lstrip(".")
    else:
        codec_args = FORMAT_CODEC_ARGS.get(export.format)
        if not codec_args:
            raise ValueError(f"unsupported export format: {export.format}")
        cmd += codec_args
        if export.format == "mp3":
            cmd += ["-b:a", export.quality or Config.EXPORT_MP3_DEFAULT_QUALITY]
        if export.embed_metadata:
            cmd += _metadata_args(resource)
        ext = export.format

    out_path = os.path.join(Config.EXPORTS_DIR, f"{base}{window_suffix}.{ext}")
    cmd.append(out_path)

    subprocess.run(
        cmd, check=True, capture_output=True, text=True,
        timeout=Config.FFMPEG_TIMEOUT_SECONDS,
    )
    return out_path


def process_exports():
    """
    Named job, registered like any other — the worker runs this on a
    schedule (frequent — see deploy/systemd — since a person is often
    actively waiting to download the result). One export's failure
    doesn't stop the rest of the batch.
    """
    processed, failed = [], []

    for export in Export.query.filter_by(status="queued").all():
        export.status = "running"
        db.session.commit()

        try:
            out_path = _run_one_export(export)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
                FileNotFoundError, ValueError) as e:
            export.status = "error"
            export.error_detail = str(e)
            db.session.add(FileEvent(
                resource_id=export.resource_id, event_type="failed",
                detail=f"export {export.id} failed: {e}",
            ))
            failed.append(export.id)
        else:
            export.status = "success"
            export.output_path = out_path
            export.completed_at = datetime.utcnow()
            db.session.add(FileEvent(
                resource_id=export.resource_id, event_type="exported", detail=out_path,
            ))
            processed.append(export.id)

        db.session.commit()

    return {"status": "success", "processed": processed, "failed": failed}
