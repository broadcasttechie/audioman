"""
The time contract (PLAN §17.5 / NEXT.md work package 1).

  * The database stores `captured_at` (and every other datetime) as a NAIVE
    datetime that is UTC.
  * The API always SERIALISES with a trailing "Z" (`to_utc_iso`), so a browser
    parses it as an instant and shows it in the viewer's own timezone. Emitting
    a naive ISO string was the cause of the "09:00 reads back as 08:00" bug:
    JavaScript treats a naive date-time as *local* time.
  * The API ACCEPTS an ISO string with "Z" or an offset (converted to UTC); a
    string with no offset is taken to be UTC, per the contract.
  * A wall-clock time read from a file with no offset (BWF, EXIF) is in the
    RECORDER's local time. Until per-recorder profiles exist (work package 2) it
    is interpreted in Config.DEFAULT_RECORDER_TIMEZONE (`naive_local_to_utc`),
    so British summer time is handled correctly.
  * Dawarich sends integer epoch seconds (UTC): `utc_from_epoch`.

DST edge cases in `naive_local_to_utc`: an ambiguous wall time (clocks going
back, e.g. 01:30 on the last Sunday of October) resolves to the first
occurrence (summer time); a wall time that never existed (clocks going
forward) is shifted forward by zoneinfo. Both are a guess about one hour of one
night a year; recorder profiles can flag them later.
"""
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


def to_utc_iso(dt):
    """Naive-UTC (or aware) datetime -> 'YYYY-MM-DDTHH:MM:SS[.ffffff]Z', None stays None."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.isoformat() + "Z"


def parse_to_utc_naive(value):
    """API input -> naive UTC datetime. Accepts 'Z', an offset, or no offset (= UTC)."""
    if isinstance(value, datetime):
        dt = value
    else:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("expected an ISO 8601 date-time string")
        s = value.strip()
        if s.endswith(("Z", "z")):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)  # ValueError on garbage
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def utc_from_epoch(seconds):
    return datetime.fromtimestamp(float(seconds), timezone.utc).replace(tzinfo=None)


def to_epoch(dt):
    """Naive-UTC datetime -> integer epoch seconds."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def naive_local_to_utc(naive, tzname):
    """A wall-clock time in `tzname` -> naive UTC."""
    return naive.replace(tzinfo=ZoneInfo(tzname)).astimezone(timezone.utc).replace(tzinfo=None)


def parse_point_timestamp(raw):
    """A location-service timestamp (epoch number, digit string, or ISO) -> naive UTC."""
    if isinstance(raw, (int, float)) or (isinstance(raw, str) and raw.strip().lstrip("-").isdigit()):
        return utc_from_epoch(raw)
    return parse_to_utc_naive(raw)


_EXIF_RE = re.compile(
    r"^\s*(\d{4})[:\-](\d{2})[:\-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})(?:\.\d+)?\s*(Z|[+-]\d{2}:?\d{2})?\s*$"
)


def parse_exif_datetime(raw, default_tz):
    """
    An exiftool date-time ('2026:09:17 09:11:24', optionally with '.fff' and a
    'Z'/'+01:00' suffix) -> naive UTC, or None if it isn't a date-time.
    With an offset it is exact; without one it is recorder wall-clock time in
    `default_tz`.
    """
    m = _EXIF_RE.match(raw or "")
    if not m:
        return None
    y, mo, d, h, mi, s, off = m.groups()
    try:
        naive = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s))
    except ValueError:
        return None
    if off is None:
        return naive_local_to_utc(naive, default_tz)
    if off == "Z":
        return naive
    sign = 1 if off[0] == "+" else -1
    digits = off[1:].replace(":", "")
    delta = timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
    return naive - sign * delta
