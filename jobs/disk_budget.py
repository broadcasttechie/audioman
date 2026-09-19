"""
Disk-space admission control for the Inbox pull.

Why this exists: a pulled file sits in STAGING_DIR until a human reviews
and files it, and that staging area shares a small disk with the app. A
bulk import (the ~340-file archive is going through the Inbox) would
otherwise fill the disk long before anyone reviews it. Files that don't
fit simply stay in the Drive Inbox and are pulled on a later run once
space frees up -- nothing is lost or re-ordered by waiting.

Kept as pure functions (no DB, no rclone) so the rules can be tested
without touching a disk.
"""
import os

GB = 1024 ** 3

OK = "ok"
WAIT = "wait"    # doesn't fit right now, will once staging drains
NEVER = "never"  # bigger than the whole staging budget: needs a config change


def staged_bytes(staging_dir):
    """Bytes currently held under staging_dir, measured from disk (so it
    also counts stray files with no database row)."""
    total = 0
    for root, _dirs, files in os.walk(staging_dir):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass  # vanished mid-walk (filed/moved by another process)
    return total


def admit(size, free_bytes, staged, reserve_bytes, budget_bytes):
    """
    Decide whether a file of `size` bytes may be pulled now.

    Two independent limits, both must hold:
      * free space on the disk must stay above `reserve_bytes` afterwards
        (protects the database, logs and everything else on the disk);
      * bytes staged awaiting review must stay within `budget_bytes`.
    Returns (verdict, reason).
    """
    if size > budget_bytes:
        return NEVER, (
            f"{size / GB:.2f} GB is larger than the whole staging budget "
            f"({budget_bytes / GB:.2f} GB); raise STAGING_BUDGET_GB"
        )
    if free_bytes - size < reserve_bytes:
        return WAIT, (
            f"would leave {(free_bytes - size) / GB:.2f} GB free, below the "
            f"{reserve_bytes / GB:.2f} GB reserve"
        )
    if staged + size > budget_bytes:
        return WAIT, (
            f"staging holds {staged / GB:.2f} GB of a {budget_bytes / GB:.2f} GB "
            f"budget; waiting for files to be reviewed and filed"
        )
    return OK, ""
