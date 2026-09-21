"""Run on the container: python3 -m unittest discover -s tests"""
import unittest

from jobs.inbox_rules import classify, is_processed_path, sidecar_matches, sidecar_target


class ClassifyTests(unittest.TestCase):
    def test_audio(self):
        for p in ("a.wav", "2024 France/STE-000.wav", "x.WAV", "Wed at 11-30.m4a", "y.mp3", "z.FLAC"):
            self.assertEqual(classify(p), "audio", p)

    def test_sidecars_are_not_audio(self):
        for p in ("STE-002.wav.reapeaks", "hats carrots raw.pkf", "H1n tests/x.WAV.reapeaks"):
            self.assertEqual(classify(p), "sidecar", p)

    def test_project_files_held(self):
        for p in ("H1n tests/1/1.RPP", "show.sesx", "a.rpp-bak"):
            self.assertEqual(classify(p), "project-file", p)

    def test_junk_is_ignored(self):
        for p in (".DS_Store", "dir/._take.wav", "~$notes.wav", "Thumbs.db", "take.wav.part", "x.wav.crdownload", "y.tmp"):
            self.assertEqual(classify(p), "ignore", p)

    def test_everything_else_is_unknown(self):
        for p in ("notes.pdf", "photo.jpg", "Untitled", "song.gdoc"):
            self.assertEqual(classify(p), "unknown", p)

    def test_processed_folder(self):
        self.assertTrue(is_processed_path("_processed/a.wav"))
        self.assertTrue(is_processed_path("_processed/sub/a.wav"))
        self.assertFalse(is_processed_path("sub/_processed/a.wav"))
        self.assertFalse(is_processed_path("_processed_by_me/a.wav"))


class SidecarTests(unittest.TestCase):
    def test_full_name_sidecar(self):
        t = sidecar_target("2024 France/STE-002.wav.reapeaks")
        self.assertEqual(t, ("2024 france", "ste-002.wav", None))
        self.assertTrue(sidecar_matches("2024 France/STE-002.wav", t))
        self.assertTrue(sidecar_matches("2024 FRANCE/ste-002.WAV", t))
        self.assertFalse(sidecar_matches("2024 France/STE-003.wav", t))

    def test_same_name_in_another_folder_is_a_different_file(self):
        t = sidecar_target("2024 France/STE-000.wav.reapeaks")
        self.assertFalse(sidecar_matches("storiesandforrest/STE-000.wav", t))
        self.assertFalse(sidecar_matches("STE-000.wav", t))

    def test_stem_sidecar_matches_any_audio_extension(self):
        t = sidecar_target("Archive/HATS/hats carrots raw.pkf")
        self.assertTrue(sidecar_matches("Archive/HATS/hats carrots raw.wav", t))
        self.assertTrue(sidecar_matches("Archive/HATS/hats carrots raw.mp3", t))
        self.assertFalse(sidecar_matches("Archive/HATS/hats carrots.wav", t))

    def test_top_level(self):
        t = sidecar_target("take.wav.reapeaks")
        self.assertTrue(sidecar_matches("take.wav", t))
        self.assertFalse(sidecar_matches("sub/take.wav", t))


if __name__ == "__main__":
    unittest.main()
