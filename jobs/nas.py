"""
NAS access guard and safe file placement.

Two separate problems live here:

1. Guard. NAS_LIBRARY_ROOT is a bind mount of an NFS share. If that mount
   drops, the path silently becomes an ordinary empty directory on the
   container's small root disk, and anything that writes there "works" --
   files land on the wrong disk, filing/refiling/verify jobs report every
   file missing, and `rclone sync` from an empty directory would delete the
   Drive copy. So everything that touches the library first checks that the
   path is a real mount AND that a marker file (which only exists on the NAS)
   is visible, and refuses otherwise.

2. Placement. Staging and the NAS are different filesystems, so os.rename()
   fails with EXDEV. Filing is therefore copy -> fsync -> verify -> atomic
   rename into place -> only then delete the staging file. At every failure
   point the source is left untouched and any partial file is removed, so a
   recording is never lost or half-written into the library. An existing
   destination is never overwritten.
"""
import hashlib
import logging
import os

from config import Config

log = logging.getLogger(__name__)

CHUNK = 8 * 1024 * 1024


class NasUnavailable(Exception):
    """The NAS is not mounted/visible; nothing may be written to the local fallback directory."""


def nas_status(root=None, marker=None, require_mount=None):
    """Returns (ok, reason). Arguments default to Config; they exist so the
    checks can be tested without touching the real mount."""
    root = Config.NAS_LIBRARY_ROOT if root is None else root
    marker = Config.NAS_MARKER_FILE if marker is None else marker
    require = Config.NAS_REQUIRE_MOUNT if require_mount is None else require_mount
    if not require:
        return True, ""
    try:
        if not os.path.isdir(root):
            return False, f"{root} does not exist"
        if not os.path.ismount(root):
            return False, f"{root} is not a mount point; the NAS is probably not mounted"
        if not os.path.isfile(os.path.join(root, marker)):
            return False, (
                f"marker file '{marker}' is not visible in {root}; the NAS may be "
                f"unmounted or the wrong share is mounted"
            )
    except OSError as e:
        return False, f"cannot check {root}: {e}"
    return True, ""


def require_nas():
    ok, reason = nas_status()
    if not ok:
        raise NasUnavailable(reason)


def is_nas_path(path, root=None):
    root = os.path.realpath(Config.NAS_LIBRARY_ROOT if root is None else root)
    return os.path.realpath(path).startswith(root + os.sep)


def sha256_file(path, drop_cache=False):
    """Chunked, so a multi-GB recording never has to fit in memory. With
    drop_cache the kernel is asked to forget the file's cached pages first so a
    read-back after a write is served by the NAS, not from local memory
    (best effort; NFS may still cache)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        if drop_cache and hasattr(os, "posix_fadvise"):
            os.posix_fadvise(f.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def file_to_nas(src, dest, expected_sha256=None, verify_readback=True):
    """
    Move `src` (local staging) to `dest` (under the NAS root) safely.
    Returns the sha256 of the data written. Raises NasUnavailable,
    FileExistsError (destination taken: never overwritten), or OSError
    (copy/verify failed). On any exception `src` is untouched and no partial
    file is left behind.
    """
    require_nas()
    if os.path.exists(dest):
        raise FileExistsError(f"refusing to overwrite existing NAS file: {dest}")

    dest_dir = os.path.dirname(dest)
    os.makedirs(dest_dir, exist_ok=True)
    tmp = os.path.join(dest_dir, f".{os.path.basename(dest)}.part")

    h = hashlib.sha256()
    try:
        with open(src, "rb") as fin, open(tmp, "wb") as fout:
            for chunk in iter(lambda: fin.read(CHUNK), b""):
                h.update(chunk)
                fout.write(chunk)
            fout.flush()
            os.fsync(fout.fileno())
        copied = h.hexdigest()

        if expected_sha256 and copied != expected_sha256:
            raise OSError(
                f"source {src} no longer matches its stored checksum "
                f"(read {copied[:12]}..., expected {expected_sha256[:12]}...): "
                f"the file changed or is corrupt, so it was not filed"
            )
        if os.path.getsize(tmp) != os.path.getsize(src):
            raise OSError(f"size differs after copying {src} to the NAS")
        if verify_readback and sha256_file(tmp, drop_cache=True) != copied:
            raise OSError(f"read-back checksum from the NAS differs for {dest}")

        require_nas()  # still mounted after a possibly long copy?
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    try:
        os.unlink(src)
    except OSError as e:
        # Filed and verified; only the staging leftover remains. Loud, not fatal.
        log.warning("filed %s but could not remove staging file %s: %s", dest, src, e)
    return copied
