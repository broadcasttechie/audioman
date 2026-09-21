"""Run on the container: python3 -m unittest discover -s tests

Every filename in tests/fixtures/sample_filenames.txt (real names from the user's
archive) must have an expectation here, so adding a fixture without saying what
it should do fails the suite."""
import os
import unittest
from datetime import datetime

from jobs import filename_patterns as fp

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "sample_filenames.txt")
PROFILES = sorted(fp.DEFAULT_PROFILES, key=lambda p: p["priority"])


def m(name, **kw):
    return fp.match_filename(name, PROFILES, **kw)


def dt(*a):
    return datetime(*a)


# filename -> (profile, utc or None, time_known, title, is_edit, reason-substring or None)
EXPECT = {
    "audio_260917_091124_32bit_orig_stereo.wav": ("Insta360 mic", dt(2026, 9, 17, 8, 11, 24), True, None, False, None),
    "audio_260917_100746_32bit_orig_stereo.wav": ("Insta360 mic", dt(2026, 9, 17, 9, 7, 46), True, None, False, None),
    "audio_260917_103748_32bit_orig_stereo.wav": ("Insta360 mic", dt(2026, 9, 17, 9, 37, 48), True, None, False, None),
    "audio_260917_110748_32bit_orig_stereo.wav": ("Insta360 mic", dt(2026, 9, 17, 10, 7, 48), True, None, False, None),
    "audio_260914_145838_32bit_orig_stereo.wav": ("Insta360 mic", dt(2026, 9, 14, 13, 58, 38), True, None, False, None),
    "audio_260914_152840_32bit_orig_stereo.wav": ("Insta360 mic", dt(2026, 9, 14, 14, 28, 40), True, None, False, None),
    "audio_260914_191744_32bit_orig_stereo.wav": ("Insta360 mic", dt(2026, 9, 14, 18, 17, 44), True, None, False, None),
    "audio_260914_192731_32bit_orig_stereo.wav": ("Insta360 mic", dt(2026, 9, 14, 18, 27, 31), True, None, False, None),
    "audio_260913_191831_32bit_orig_stereo.wav": ("Insta360 mic", dt(2026, 9, 13, 18, 18, 31), True, None, False, None),
    "audio_260913_184515_24bit_orig.wav": ("Insta360 mic", dt(2026, 9, 13, 17, 45, 15), True, None, False, None),
    # clock never set: the filename says 1 Jan 2000 -> date unknown, with a reason
    "audio_000101_000228_24bit_orig.wav": ("Insta360 mic", None, False, None, False, "clock was not set"),
    "audio_000101_000433_24bit_orig.wav": ("Insta360 mic", None, False, None, False, "clock was not set"),
    "audio_000101_000707_24bit_orig.wav": ("Insta360 mic", None, False, None, False, "clock was not set"),
    "audio_000101_003709_24bit_orig.wav": ("Insta360 mic", None, False, None, False, "clock was not set"),
    "240818-210658.WAV": ("Zoom recorder", dt(2024, 8, 18, 20, 6, 58), True, None, False, None),
    "240821-101757.WAV": ("Zoom recorder", dt(2024, 8, 21, 9, 17, 57), True, None, False, None),
    "240821-105248.WAV": ("Zoom recorder", dt(2024, 8, 21, 9, 52, 48), True, None, False, None),
    "250424-114713.WAV": ("Zoom recorder", dt(2025, 4, 24, 10, 47, 13), True, None, False, None),
    "250424-121237.-woods-includes-voices.WAV": ("Zoom recorder", dt(2025, 4, 24, 11, 12, 37), True, "woods-includes-voices", False, None),
    "250424-115151- Parkridge NR Birds and Jacob.WAV": ("Zoom recorder", dt(2025, 4, 24, 10, 51, 51), True, "Parkridge NR Birds and Jacob", False, None),
    "250424-115254-Jacob at Parkridge NR.WAV": ("Zoom recorder", dt(2025, 4, 24, 10, 52, 54), True, "Jacob at Parkridge NR", False, None),
    "250424-121237-Parkridge Nature Reserve-EDIT.WAV.wav": ("Zoom recorder", dt(2025, 4, 24, 11, 12, 37), True, "Parkridge Nature Reserve", True, None),
    "260822-144229 - waking from nap Shuttleworth airplanes.WAV": ("Zoom recorder", dt(2026, 8, 22, 13, 42, 29), True, "waking from nap Shuttleworth airplanes", False, None),
    "ZOOM0001.WAV": ("Zoom recorder (ZOOMnnnn)", None, False, None, False, "does not put a date"),
    "ZOOM0009.WAV": ("Zoom recorder (ZOOMnnnn)", None, False, None, False, "does not put a date"),
    "STE-000.wav": ("Zoom stereo (STE-nnn)", None, False, None, False, "does not put a date"),
    "STE-005.wav": ("Zoom stereo (STE-nnn)", None, False, None, False, "does not put a date"),
    "STE-001.mp3": ("Zoom stereo (STE-nnn)", None, False, None, False, "does not put a date"),
    "2025-08-27_184647372.wav": ("Phone recorder (date_time)", dt(2025, 8, 27, 17, 46, 47), True, None, False, None),
    "27-04-2025, 15-42.wav": ("Phone recorder (dd-mm-yyyy)", dt(2025, 4, 27, 14, 42), True, None, False, None),
    "29-04-2025, 19-21.wav": ("Phone recorder (dd-mm-yyyy)", dt(2025, 4, 29, 18, 21), True, None, False, None),
    "Wednesday at 11-30.m4a": ("Phone memo (no year)", None, False, None, False, "no year"),
    "5 Sept at 10-04.m4a": ("Phone memo (no year)", None, False, None, False, "no year"),
    "2026-07-08 Down at the station.wav": ("Date and title", dt(2026, 7, 8, 11, 0), False, "Down at the station", False, None),
    # nothing to read: not matched by any profile
    "yellow.wav": None,
    "sing a song of sixpence .wav": None,
    "STE-002.wav.reapeaks": None,
    "hats carrots raw.pkf": None,
}


def fixture_filenames():
    names = []
    for line in open(FIXTURES):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        names.append(os.path.basename(parts[1]))
    return names


class FixtureTests(unittest.TestCase):
    def test_every_fixture_has_an_expectation(self):
        missing = [n for n in fixture_filenames() if n not in EXPECT]
        self.assertEqual(missing, [], "add these real filenames to EXPECT")

    def test_expectations(self):
        for name, want in EXPECT.items():
            got = m(name)
            if want is None:
                self.assertIsNone(got, name)
                continue
            profile, utc, time_known, title, is_edit, reason = want
            self.assertIsNotNone(got, name)
            self.assertEqual(got.profile, profile, name)
            self.assertEqual(got.utc, utc, name)
            self.assertEqual(got.time_known, time_known, name)
            self.assertEqual(got.title, title, name)
            self.assertEqual(got.is_edit, is_edit, name)
            if reason:
                self.assertIn(reason, got.unknown_reason or "", name)
            else:
                self.assertIsNone(got.unknown_reason, name)

    def test_trust_follows_the_profile(self):
        for name in ("240818-210658.WAV", "audio_260917_091124_32bit_orig_stereo.wav"):
            self.assertTrue(m(name).trusted, name)
        for name in ("2025-08-27_184647372.wav", "27-04-2025, 15-42.wav", "2026-07-08 Down at the station.wav", "STE-000.wav"):
            self.assertFalse(m(name).trusted, name)

    def test_track_number_captured(self):
        self.assertEqual(m("STE-005.wav").seq, "005")
        self.assertEqual(m("ZOOM0009.WAV").seq, "0009")


class RuleTests(unittest.TestCase):
    def test_drive_copy_prefix_is_ignored(self):
        got = m("Copy of audio_260917_091124_32bit_orig_stereo.wav")
        self.assertEqual(got.utc, dt(2026, 9, 17, 8, 11, 24))

    def test_extension_case_and_doubling(self):
        self.assertEqual(m("240818-210658.wav").utc, m("240818-210658.WAV.wav").utc)

    def test_winter_uses_gmt(self):
        self.assertEqual(m("250115-101500.WAV").utc, dt(2025, 1, 15, 10, 15))

    def test_clock_offset_shifts_the_true_time(self):
        fast = [dict(PROFILES[1], clock_offset_seconds=180)]  # recorder runs 3 min fast
        got = fp.match_filename("250115-101500.WAV", fast)
        self.assertEqual(got.utc, dt(2025, 1, 15, 10, 12))
        slow = [dict(PROFILES[1], clock_offset_seconds=-300)]
        self.assertEqual(fp.match_filename("250115-101500.WAV", slow).utc, dt(2025, 1, 15, 10, 20))

    def test_profile_timezone_is_used(self):
        cet = [dict(PROFILES[1], timezone="Europe/Paris")]
        self.assertEqual(fp.match_filename("250115-101500.WAV", cet).utc, dt(2025, 1, 15, 9, 15))

    def test_impossible_date(self):
        got = m("251340-101500.WAV")  # month 13
        self.assertIsNone(got.utc)
        self.assertIn("not a real date", got.unknown_reason)

    def test_future_date_is_rejected(self):
        got = m("990101-101500.WAV", now=dt(2026, 9, 21))
        self.assertIsNone(got.utc)
        self.assertIn("future", got.unknown_reason)

    def test_first_matching_profile_wins(self):
        ordered = [
            {"name": "A", "patterns": ["{YY}{MM}{DD}-{hh}{mm}{ss}{rest}"], "date_trust": "trusted", "timezone": "UTC"},
            {"name": "B", "patterns": ["{YY}{MM}{DD}-{hh}{mm}{ss}{rest}"], "date_trust": "suggest", "timezone": "UTC"},
        ]
        self.assertEqual(fp.match_filename("250115-101500.WAV", ordered).profile, "A")

    def test_title_and_edit_parsing(self):
        cases = {
            "": (None, False), "-EDIT": (None, True), " - cows-EDIT": ("cows", True),
            "- Credit": ("Credit", False),      # 'edit' inside a word is not an edit marker
            ".-wind in trees": ("wind in trees", False), "_take 2 edit": ("take 2", True),
        }
        for rest, want in cases.items():
            self.assertEqual(fp.parse_rest(rest), want, repr(rest))


class CompileTests(unittest.TestCase):
    def test_unknown_token_rejected(self):
        with self.assertRaises(ValueError):
            fp.compile_pattern("{YY}{nonsense}")

    def test_repeated_token_rejected(self):
        with self.assertRaises(ValueError):
            fp.compile_pattern("{YY}-{YY}")

    def test_literals_are_escaped(self):
        rx = fp.compile_pattern("a.b({YY})")
        self.assertTrue(rx.match("a.b(26)"))
        self.assertFalse(rx.match("aXb(26)"))

    def test_all_default_patterns_compile(self):
        for profile in fp.DEFAULT_PROFILES:
            for pattern in profile["patterns"]:
                fp.compile_pattern(pattern)


if __name__ == "__main__":
    unittest.main()
