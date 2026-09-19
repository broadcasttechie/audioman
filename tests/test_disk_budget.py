"""Run with: python3 -m unittest discover -s tests"""
import os
import tempfile
import unittest

from jobs import disk_budget as d

GB = d.GB


class AdmitTests(unittest.TestCase):
    def test_fits(self):
        self.assertEqual(d.admit(1 * GB, 10 * GB, 0, 3 * GB, 6 * GB)[0], d.OK)

    def test_reserve_protects_free_space(self):
        verdict, reason = d.admit(1 * GB, 3.5 * GB, 0, 3 * GB, 6 * GB)
        self.assertEqual(verdict, d.WAIT)
        self.assertIn("reserve", reason)

    def test_exactly_at_reserve_is_allowed(self):
        self.assertEqual(d.admit(1 * GB, 4 * GB, 0, 3 * GB, 6 * GB)[0], d.OK)

    def test_budget_counts_what_is_already_staged(self):
        verdict, reason = d.admit(1 * GB, 50 * GB, 5.5 * GB, 3 * GB, 6 * GB)
        self.assertEqual(verdict, d.WAIT)
        self.assertIn("budget", reason)

    def test_file_bigger_than_whole_budget_never_fits(self):
        verdict, reason = d.admit(7 * GB, 50 * GB, 0, 3 * GB, 6 * GB)
        self.assertEqual(verdict, d.NEVER)
        self.assertIn("STAGING_BUDGET_GB", reason)

    def test_zero_byte_file_still_checks_reserve(self):
        self.assertEqual(d.admit(0, 2 * GB, 0, 3 * GB, 6 * GB)[0], d.WAIT)


class StagedBytesTests(unittest.TestCase):
    def test_counts_nested_files_and_missing_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "duplicates"))
            for rel, n in (("a.wav", 100), ("duplicates/b.wav", 50)):
                with open(os.path.join(tmp, rel), "wb") as f:
                    f.write(b"x" * n)
            self.assertEqual(d.staged_bytes(tmp), 150)
        self.assertEqual(d.staged_bytes("/nonexistent/dir"), 0)


if __name__ == "__main__":
    unittest.main()
