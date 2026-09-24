"""Unit tests for the device-auth pure functions and the upload filename safety check (no database)."""
import unittest

from app.device_auth import hash_token, generate_token
from app.device_api import _safe_leaf_filename


class TokenTests(unittest.TestCase):
    def test_hash_is_deterministic_and_sha256_hex(self):
        h1, h2 = hash_token("abc"), hash_token("abc")
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 64)
        self.assertTrue(all(c in "0123456789abcdef" for c in h1))

    def test_different_tokens_hash_differently(self):
        self.assertNotEqual(hash_token("abc"), hash_token("abd"))

    def test_generate_token_returns_a_matching_raw_hash_pair(self):
        raw, digest = generate_token()
        self.assertEqual(hash_token(raw), digest)
        self.assertGreaterEqual(len(raw), 32)   # url-safe, plenty of entropy (32 random bytes)

    def test_generate_token_is_unique_each_time(self):
        raws = {generate_token()[0] for _ in range(20)}
        self.assertEqual(len(raws), 20)


class SafeLeafFilenameTests(unittest.TestCase):
    def test_a_normal_filename_is_kept_unchanged(self):
        self.assertEqual(_safe_leaf_filename("audio_260101_100000.wav"), "audio_260101_100000.wav")

    def test_a_directory_prefix_including_traversal_is_reduced_to_its_basename(self):
        self.assertEqual(_safe_leaf_filename("DCIM/Recordings/take1.wav"), "take1.wav")
        self.assertEqual(_safe_leaf_filename("../../etc/passwd"), "passwd")

    def test_rejects_a_name_that_is_only_a_traversal_marker(self):
        self.assertIsNone(_safe_leaf_filename(".."))
        self.assertIsNone(_safe_leaf_filename("."))

    def test_rejects_empty_or_missing(self):
        self.assertIsNone(_safe_leaf_filename(""))
        self.assertIsNone(_safe_leaf_filename(None))

    def test_rejects_a_null_byte(self):
        self.assertIsNone(_safe_leaf_filename("ok\x00.wav"))


if __name__ == "__main__":
    unittest.main()
