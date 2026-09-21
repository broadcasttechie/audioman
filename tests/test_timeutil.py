"""Run on the container: python3 -m unittest discover -s tests"""
import unittest
from datetime import datetime, timezone

from app import timeutil as t

LONDON = "Europe/London"


class SerialiseTests(unittest.TestCase):
    def test_naive_utc_gets_z(self):
        self.assertEqual(t.to_utc_iso(datetime(2026, 9, 17, 8, 11, 24)), "2026-09-17T08:11:24Z")

    def test_none_stays_none(self):
        self.assertIsNone(t.to_utc_iso(None))

    def test_aware_is_converted(self):
        from datetime import timedelta
        aware = datetime(2026, 9, 17, 9, 11, 24, tzinfo=timezone(timedelta(hours=1)))
        self.assertEqual(t.to_utc_iso(aware), "2026-09-17T08:11:24Z")


class ParseInputTests(unittest.TestCase):
    def test_z_offset_and_naive_forms(self):
        expected = datetime(2026, 9, 17, 8, 11, 24)
        for s in ("2026-09-17T08:11:24Z", "2026-09-17T09:11:24+01:00", "2026-09-17T08:11:24",
                  "2026-09-17T08:11:24.000Z", "2026-09-17T08:11:24z"):
            self.assertEqual(t.parse_to_utc_naive(s), expected, s)

    def test_browser_toISOString_with_millis(self):
        self.assertEqual(t.parse_to_utc_naive("2026-09-17T08:11:00.000Z"), datetime(2026, 9, 17, 8, 11))

    def test_rejects_garbage(self):
        for bad in ("", "yesterday", None, 12):
            with self.assertRaises(ValueError):
                t.parse_to_utc_naive(bad)

    def test_round_trip_is_stable(self):
        original = "2026-09-17T08:11:24Z"
        self.assertEqual(t.to_utc_iso(t.parse_to_utc_naive(original)), original)


class EpochTests(unittest.TestCase):
    def test_epoch_round_trip(self):
        dt = datetime(2026, 9, 17, 8, 11, 24)
        self.assertEqual(t.utc_from_epoch(t.to_epoch(dt)), dt)
        self.assertEqual(t.to_epoch(datetime(1970, 1, 1, 0, 0, 1)), 1)

    def test_point_timestamp_forms(self):
        dt = datetime(2026, 9, 17, 8, 11, 24)
        e = t.to_epoch(dt)
        self.assertEqual(t.parse_point_timestamp(e), dt)
        self.assertEqual(t.parse_point_timestamp(str(e)), dt)
        self.assertEqual(t.parse_point_timestamp("2026-09-17T08:11:24Z"), dt)


class ExifTests(unittest.TestCase):
    def test_summer_wall_clock_is_bst(self):
        # The user's real file: 09:11:24 on 17 Sep 2026 is 08:11:24 UTC.
        self.assertEqual(t.parse_exif_datetime("2026:09:17 09:11:24", LONDON), datetime(2026, 9, 17, 8, 11, 24))

    def test_winter_wall_clock_is_gmt(self):
        self.assertEqual(t.parse_exif_datetime("2026:01:15 09:11:24", LONDON), datetime(2026, 1, 15, 9, 11, 24))

    def test_explicit_offset_wins_over_default_zone(self):
        self.assertEqual(t.parse_exif_datetime("2026:09:17 09:11:24+02:00", LONDON), datetime(2026, 9, 17, 7, 11, 24))
        self.assertEqual(t.parse_exif_datetime("2026:09:17 09:11:24-0500", LONDON), datetime(2026, 9, 17, 14, 11, 24))
        self.assertEqual(t.parse_exif_datetime("2026:09:17 09:11:24Z", LONDON), datetime(2026, 9, 17, 9, 11, 24))

    def test_fractional_seconds_tolerated(self):
        self.assertEqual(t.parse_exif_datetime("2026:09:17 09:11:24.50", LONDON), datetime(2026, 9, 17, 8, 11, 24))

    def test_not_a_date(self):
        for bad in (None, "", "0000:00:00 00:00:00", "hello", "2026:13:01 00:00:00"):
            self.assertIsNone(t.parse_exif_datetime(bad, LONDON), bad)

    def test_dst_boundaries_are_deterministic(self):
        # Clocks go back 25 Oct 2026 at 02:00 BST: 01:30 happens twice -> first (BST) occurrence.
        self.assertEqual(t.parse_exif_datetime("2026:10:25 01:30:00", LONDON), datetime(2026, 10, 25, 0, 30))
        # Clocks go forward 29 Mar 2026 at 01:00: 01:30 never existed -> shifted, not an error.
        self.assertIsNotNone(t.parse_exif_datetime("2026:03:29 01:30:00", LONDON))


if __name__ == "__main__":
    unittest.main()
