"""
Recorder profiles and filename patterns (PLAN 17.6 / 18.5b / 18.5c / 18.5f).

The user's recorders write no embedded tags, so the date usually comes from
the FILENAME. Each recorder profile lists grok-style patterns, a timezone, an
optional clock offset, and a trust level:

  trusted  the filename date is applied at ingest (source "filename", precision
           exact), so location lookup runs straight away;
  suggest  the filename date is only stored as a suggestion for the user to
           confirm at intake.

This module is pure (no database, no Flask) so every rule can be tested against
the real filenames in tests/fixtures/sample_filenames.txt.

Pattern syntax: literal text plus {tokens}. Literal text is matched exactly;
each token may appear once.

    {YYYY} {YY} {MM} {DD} {hh} {mm} {ss} {ms}   date/time parts
    {seq}   digits (a file counter)             {bits}  digits
    {rest}  anything to the end of the name     {any}   anything (non-greedy)

The name is matched WITHOUT its extension(s) (`take.WAV.wav` -> `take`) and
without a leading "Copy of " (Google Drive's duplicate prefix, seen in the real
data). Whatever `{rest}` captures becomes the suggested title: leading/trailing
separators are stripped and a trailing "-EDIT" is recognised as an edited
version of the original (the file becomes a related "edit" later, PLAN 18.5g).

Date rules:
  * two-digit years are 20YY; a year before 2010 means the recorder's clock was
    never set (the Insta360's `audio_000101_...` files) -> no date, with a reason;
  * a date more than a day in the future -> no date, with a reason;
  * a name with a date but no time gets 12:00 local and time_known=False (it can
    only ever be an approximate date);
  * the parsed time is the RECORDER's wall clock. `clock_offset_seconds` is
    (recorder clock - true time), so a recorder running 3 minutes fast is +180;
    the true time is parsed - offset, then converted from the profile's IANA
    timezone to UTC (app/timeutil.naive_local_to_utc).
"""
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.timeutil import naive_local_to_utc

AUDIO_EXTENSIONS = (".wav", ".mp3", ".m4a", ".flac", ".aac", ".ogg", ".opus", ".aif", ".aiff", ".mp4")
MIN_PLAUSIBLE_YEAR = 2010

TOKENS = {
    "YYYY": r"\d{4}", "YY": r"\d{2}", "MM": r"\d{2}", "DD": r"\d{2}",
    "hh": r"\d{2}", "mm": r"\d{2}", "ss": r"\d{2}", "ms": r"\d{3}",
    "seq": r"\d+", "bits": r"\d+", "rest": r".*", "any": r".+?",
}
_TOKEN_RE = re.compile(r"\{(\w+)\}")


@dataclass
class FilenameMatch:
    profile: str
    pattern: str
    trusted: bool
    local: datetime | None = None      # recorder wall-clock time as parsed
    utc: datetime | None = None        # naive UTC after offset + timezone
    time_known: bool = False           # False when the name has a date but no time
    title: str | None = None
    is_edit: bool = False
    seq: str | None = None
    unknown_reason: str | None = None
    fields: dict = field(default_factory=dict)


def compile_pattern(pattern):
    """Compile a profile pattern to a regex. Raises ValueError for an unknown or
    repeated token, so bad patterns are rejected when saved, not at ingest."""
    seen, out, pos = set(), [], 0
    for m in _TOKEN_RE.finditer(pattern):
        name = m.group(1)
        if name not in TOKENS:
            raise ValueError(f"unknown token {{{name}}}; use one of {sorted(TOKENS)}")
        if name in seen:
            raise ValueError(f"token {{{name}}} appears more than once")
        seen.add(name)
        out.append(re.escape(pattern[pos:m.start()]))
        out.append(f"(?P<{name}>{TOKENS[name]})")
        pos = m.end()
    out.append(re.escape(pattern[pos:]))
    return re.compile("^" + "".join(out) + "$", re.IGNORECASE)


def strip_name(filename):
    """Filename -> the part patterns are matched against."""
    name = filename
    while True:
        lower = name.lower()
        ext = next((e for e in AUDIO_EXTENSIONS if lower.endswith(e)), None)
        if not ext:
            break
        name = name[: -len(ext)]
    return re.sub(r"^Copy of ", "", name)


def parse_rest(rest):
    """Free text after the timestamp -> (title or None, is_edit)."""
    if not rest:
        return None, False
    text = rest.strip(" .-_")
    is_edit = False
    # "EDIT" only counts as a whole word at the end ("...-EDIT"), never inside one ("Credit").
    m = re.search(r"(?:^|[\s._-])edit\s*$", text, re.IGNORECASE)
    if m:
        is_edit, text = True, text[: m.start()]
    text = text.strip(" .-_")
    return (text or None), is_edit


def _build_local(groups):
    """Date/time groups -> (naive datetime, time_known) or (None, False) if no date parts."""
    if "YYYY" in groups:
        year = int(groups["YYYY"])
    elif "YY" in groups:
        year = 2000 + int(groups["YY"])
    else:
        return None, False
    if "MM" not in groups or "DD" not in groups:
        return None, False
    has_time = "hh" in groups and "mm" in groups
    local = datetime(
        year, int(groups["MM"]), int(groups["DD"]),
        int(groups["hh"]) if has_time else 12,
        int(groups["mm"]) if has_time else 0,
        int(groups["ss"]) if has_time and "ss" in groups else 0,
    )  # raises ValueError for an impossible date such as month 13
    return local, has_time


def match_filename(filename, profiles, now=None):
    """
    Try each profile (already ordered by priority) and return the first
    FilenameMatch, or None if nothing matches. `profiles` are objects/dicts with
    name, patterns, timezone, clock_offset_seconds and date_trust.
    """
    now = now or datetime.utcnow()
    name = strip_name(filename)
    for profile in profiles:
        get = profile.get if isinstance(profile, dict) else lambda k, d=None: getattr(profile, k, d)
        for pattern in get("patterns") or []:
            m = compile_pattern(pattern).match(name)
            if not m:
                continue
            groups = {k: v for k, v in m.groupdict().items() if v is not None}
            result = FilenameMatch(
                profile=get("name"), pattern=pattern,
                trusted=get("date_trust") == "trusted", fields=groups, seq=groups.get("seq"),
            )
            result.title, result.is_edit = parse_rest(groups.get("rest"))

            try:
                local, time_known = _build_local(groups)
            except ValueError:
                result.unknown_reason = "the date in the filename is not a real date"
                return result
            if local is None:
                result.unknown_reason = _no_date_reason(pattern, groups)
                return result
            if local.year < MIN_PLAUSIBLE_YEAR:
                result.unknown_reason = (
                    f"the filename says {local.year}: the recorder's clock was not set, "
                    f"so the date is unknown"
                )
                return result

            offset = timedelta(seconds=get("clock_offset_seconds") or 0)
            utc = naive_local_to_utc(local - offset, get("timezone") or "Europe/London")
            if utc > now + timedelta(days=1):
                result.unknown_reason = "the date in the filename is in the future"
                return result
            result.local, result.utc, result.time_known = local, utc, time_known
            return result
    return None


def _no_date_reason(pattern, groups):
    if "YYYY" not in pattern and "YY" not in pattern:
        if "MM" in pattern or "hh" in pattern:
            return "the filename has no year, so the date can't be worked out"
        return "this recorder does not put a date in the filename"
    return "the filename does not contain a full date"


# Seeded into the recorder_profiles table on first use; the user can edit them
# afterwards (patterns, timezone, clock offset, trust) via the API. Order = priority.
DEFAULT_PROFILES = [
    {   # 30-minute splitter; also the recorder whose clock was unset (`audio_000101_...`)
        "name": "Insta360 mic", "date_trust": "trusted", "priority": 10, "split_seconds": 1800,
        "patterns": [
            "audio_{YY}{MM}{DD}_{hh}{mm}{ss}_{bits}bit_orig_stereo",
            "audio_{YY}{MM}{DD}_{hh}{mm}{ss}_{bits}bit_orig",
        ],
    },
    {"name": "Zoom recorder", "date_trust": "trusted", "priority": 20,
     "patterns": ["{YY}{MM}{DD}-{hh}{mm}{ss}{rest}"]},
    {"name": "Zoom recorder (ZOOMnnnn)", "date_trust": "suggest", "priority": 30,
     "patterns": ["ZOOM{seq}"]},
    {"name": "Zoom stereo (STE-nnn)", "date_trust": "suggest", "priority": 31,
     "patterns": ["STE-{seq}"]},
    {"name": "Phone recorder (date_time)", "date_trust": "suggest", "priority": 40,
     "patterns": ["{YYYY}-{MM}-{DD}_{hh}{mm}{ss}{ms}"]},
    {"name": "Phone recorder (dd-mm-yyyy)", "date_trust": "suggest", "priority": 41,
     "patterns": ["{DD}-{MM}-{YYYY}, {hh}-{mm}"]},
    {"name": "Date and title", "date_trust": "suggest", "priority": 50,
     "patterns": ["{YYYY}-{MM}-{DD} {rest}"]},
    {"name": "Phone memo (no year)", "date_trust": "suggest", "priority": 60,
     "patterns": ["{any} at {hh}-{mm}{rest}"]},
]
