"""
What to do with each file found in the Drive Inbox (pure functions, no I/O).

  audio         ingested and reviewed like any recording
  sidecar       a generated companion of an audio file (REAPER peaks `.reapeaks`, `.pkf`): regenerable,
                but worth keeping, so it travels with its audio into the same NAS folder
  project-file  a DAW project (`.RPP`, `.sesx`, ...): NOT imported by the Inbox. A project refers to
                audio by relative path, so where it belongs depends on where its audio ends up; they
                are left in the Inbox and reported (adoption from the NAS folder is a later feature,
                PLAN 18.5i)
  ignore        junk that must never become a recording (hidden/temp files, OS clutter)
  unknown       anything else (documents, images...): left alone and counted

The `_processed` folder holds files this system has already ingested and is never scanned.
"""
import posixpath

from .filename_patterns import AUDIO_EXTENSIONS

SIDECAR_EXTENSIONS = (".reapeaks", ".pkf")
PROJECT_FILE_EXTENSIONS = (".rpp", ".rpp-bak", ".sesx", ".cpr", ".npr", ".ptx", ".als", ".flp")
IGNORED_NAMES = ("thumbs.db", "desktop.ini")
IGNORED_EXTENSIONS = (".tmp", ".part", ".partial", ".crdownload", ".download", ".swp")
PROCESSED_DIR = "_processed"


def is_processed_path(path):
    return path.split("/", 1)[0] == PROCESSED_DIR


def classify(path):
    name = posixpath.basename(path)
    lower = name.lower()
    if name.startswith(".") or name.startswith("~$") or lower in IGNORED_NAMES or lower.endswith(IGNORED_EXTENSIONS):
        return "ignore"
    if lower.endswith(SIDECAR_EXTENSIONS):
        return "sidecar"
    if lower.endswith(PROJECT_FILE_EXTENSIONS):
        return "project-file"
    if lower.endswith(AUDIO_EXTENSIONS):
        return "audio"
    return "unknown"


def sidecar_target(path):
    """
    Which audio file does this sidecar belong to? Returns (directory, exact_name, stem), lower-cased,
    where exactly one of exact_name / stem is set:
      take.wav.reapeaks -> ("dir", "take.wav", None)   the audio's full name plus the sidecar extension
      take.pkf          -> ("dir", None, "take")       same stem, any audio extension
    """
    directory, name = posixpath.split(path)
    lower = name.lower()
    for ext in SIDECAR_EXTENSIONS:
        if lower.endswith(ext):
            base = lower[: -len(ext)]
            if base.endswith(AUDIO_EXTENSIONS):
                return directory.lower(), base, None
            return directory.lower(), None, base
    return directory.lower(), None, None


def sidecar_matches(candidate_inbox_path, target):
    """Is an audio file (given by its Inbox path) the one a sidecar's `target` points at?"""
    directory, exact, stem = target
    cand_dir, cand_name = posixpath.split(candidate_inbox_path)
    cand_dir, cand_name = cand_dir.lower(), cand_name.lower()
    if cand_dir != directory:
        return False
    if exact:
        return cand_name == exact
    if stem:
        return posixpath.splitext(cand_name)[0] == stem
    return False
