"""Run on the container: python3 -m unittest discover -s tests"""
import hashlib
import os
import tempfile
import unittest
from unittest import mock

from jobs import nas


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


class NasStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "audio")
        os.makedirs(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_guard_can_be_disabled_for_development(self):
        self.assertEqual(nas.nas_status(self.root, ".m", require_mount=False), (True, ""))

    def test_missing_root(self):
        ok, reason = nas.nas_status(os.path.join(self.tmp.name, "nope"), ".m", True)
        self.assertFalse(ok)
        self.assertIn("does not exist", reason)

    def test_plain_directory_is_not_a_mount(self):
        _write(os.path.join(self.root, ".m"), b"")  # marker present, but not a mount
        ok, reason = nas.nas_status(self.root, ".m", True)
        self.assertFalse(ok)
        self.assertIn("not a mount point", reason)

    def test_mount_without_marker_is_rejected(self):
        with mock.patch("os.path.ismount", return_value=True):
            ok, reason = nas.nas_status(self.root, ".m", True)
        self.assertFalse(ok)
        self.assertIn("marker", reason)

    def test_mount_with_marker_is_ok(self):
        _write(os.path.join(self.root, ".m"), b"")
        with mock.patch("os.path.ismount", return_value=True):
            self.assertEqual(nas.nas_status(self.root, ".m", True), (True, ""))

    def test_is_nas_path(self):
        self.assertTrue(nas.is_nas_path(os.path.join(self.root, "a", "b.wav"), self.root))
        self.assertFalse(nas.is_nas_path(os.path.join(self.tmp.name, "other.wav"), self.root))
        self.assertFalse(nas.is_nas_path(self.root + "-sibling/x.wav", self.root))


class FileToNasTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = os.path.join(self.tmp.name, "staging", "take.wav")
        self.dest = os.path.join(self.tmp.name, "nas", "misc", "ambient", "2026", "take.wav")
        self.data = os.urandom(3 * 1024 * 1024 + 17)  # > one chunk boundary is irrelevant; odd size on purpose
        _write(self.src, self.data)
        self.sha = hashlib.sha256(self.data).hexdigest()
        self.guard = mock.patch("jobs.nas.nas_status", return_value=(True, ""))
        self.guard.start()

    def tearDown(self):
        self.guard.stop()
        self.tmp.cleanup()

    def leftovers(self):
        d = os.path.dirname(self.dest)
        return [f for f in os.listdir(d) if f.endswith(".part")] if os.path.isdir(d) else []

    def test_success_moves_verifies_and_removes_source(self):
        got = nas.file_to_nas(self.src, self.dest, expected_sha256=self.sha)
        self.assertEqual(got, self.sha)
        with open(self.dest, "rb") as f:
            self.assertEqual(f.read(), self.data)
        self.assertFalse(os.path.exists(self.src))
        self.assertEqual(self.leftovers(), [])

    def test_checksum_mismatch_keeps_source_and_leaves_nothing(self):
        with self.assertRaises(OSError):
            nas.file_to_nas(self.src, self.dest, expected_sha256="0" * 64)
        self.assertTrue(os.path.exists(self.src))
        self.assertFalse(os.path.exists(self.dest))
        self.assertEqual(self.leftovers(), [])

    def test_never_overwrites_existing_destination(self):
        _write(self.dest, b"someone else's recording")
        with self.assertRaises(FileExistsError):
            nas.file_to_nas(self.src, self.dest, expected_sha256=self.sha)
        with open(self.dest, "rb") as f:
            self.assertEqual(f.read(), b"someone else's recording")
        self.assertTrue(os.path.exists(self.src))

    def test_unavailable_nas_touches_nothing(self):
        self.guard.stop()
        with mock.patch("jobs.nas.nas_status", return_value=(False, "not mounted")):
            with self.assertRaises(nas.NasUnavailable):
                nas.file_to_nas(self.src, self.dest)
        self.guard.start()
        self.assertTrue(os.path.exists(self.src))
        self.assertFalse(os.path.exists(os.path.dirname(self.dest)))  # not even the folders

    def test_failure_during_copy_cleans_partial_file(self):
        with mock.patch("jobs.nas.os.fsync", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                nas.file_to_nas(self.src, self.dest, expected_sha256=self.sha)
        self.assertTrue(os.path.exists(self.src))
        self.assertFalse(os.path.exists(self.dest))
        self.assertEqual(self.leftovers(), [])

    def test_readback_mismatch_is_caught(self):
        real = nas.sha256_file
        calls = {"n": 0}

        def flaky(path, drop_cache=False):
            calls["n"] += 1
            return "f" * 64 if drop_cache else real(path)

        with mock.patch("jobs.nas.sha256_file", side_effect=flaky):
            with self.assertRaises(OSError):
                nas.file_to_nas(self.src, self.dest, expected_sha256=self.sha)
        self.assertTrue(os.path.exists(self.src))
        self.assertFalse(os.path.exists(self.dest))


class Sha256Tests(unittest.TestCase):
    def test_matches_hashlib_and_is_chunked(self):
        data = os.urandom(nas.CHUNK * 2 + 5)
        with tempfile.NamedTemporaryFile() as f:
            f.write(data)
            f.flush()
            self.assertEqual(nas.sha256_file(f.name), hashlib.sha256(data).hexdigest())


if __name__ == "__main__":
    unittest.main()
