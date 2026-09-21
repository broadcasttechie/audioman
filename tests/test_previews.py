"""Run on the container: python3 -m unittest discover -s tests

Generation tests need ffmpeg and audiowaveform and skip themselves where they are absent, but they DO run
on the container: the primary generator path must be exercised (a fallback that hides a broken primary is
the mistake the media-curator handover warns about)."""
import json
import os
import shutil
import struct
import subprocess
import tempfile
import unittest

from config import Config
from jobs import previews as pv


def have(binary):
    return shutil.which(binary) is not None


def make_dat(path, rate=48000, spp=480, pairs=200, channels=1, bits8=True, version=1, truncate=0):
    body = b"\x00" * (pairs * channels * 2 * (1 if bits8 else 2))
    header = struct.pack("<iIiiI", version, 1 if bits8 else 0, rate, spp, pairs)
    if version == 2:
        header += struct.pack("<i", channels)
    with open(path, "wb") as f:
        f.write((header + body)[: len(header + body) - truncate])


class HeaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.p = os.path.join(self.tmp, "w.dat")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_valid_v1(self):
        make_dat(self.p)
        h = pv.read_waveform_header(self.p)
        self.assertEqual((h["sample_rate"], h["samples_per_pixel"], h["length"], h["channels"]), (48000, 480, 200, 1))
        self.assertAlmostEqual(h["duration"], 2.0)

    def test_valid_v2_with_channels_and_16_bit(self):
        make_dat(self.p, version=2, channels=2, bits8=False, pairs=10)
        h = pv.read_waveform_header(self.p)
        self.assertEqual((h["channels"], h["bits"], h["header_size"]), (2, 16, 24))

    def test_truncated_file_is_rejected(self):
        make_dat(self.p, truncate=7)
        with self.assertRaises(pv.GenerationError):
            pv.read_waveform_header(self.p)

    def test_unknown_version_and_tiny_file_rejected(self):
        with open(self.p, "wb") as f:
            f.write(struct.pack("<iIiiI", 9, 1, 48000, 480, 0))
        with self.assertRaises(pv.GenerationError):
            pv.read_waveform_header(self.p)
        with open(self.p, "wb") as f:
            f.write(b"abc")
        with self.assertRaises(pv.GenerationError):
            pv.read_waveform_header(self.p)

    def test_zero_rate_rejected(self):
        make_dat(self.p, rate=0)
        with self.assertRaises(pv.GenerationError):
            pv.read_waveform_header(self.p)


class CachePathTests(unittest.TestCase):
    def test_content_addressed_and_sharded(self):
        sha = "ab" + "c" * 62
        self.assertTrue(pv.waveform_path(sha).endswith("/ab/" + sha + ".dat"))
        self.assertTrue(pv.preview_path(sha).endswith("/ab/" + sha + ".m4a"))
        self.assertNotEqual(os.path.dirname(pv.waveform_path(sha)), os.path.dirname(pv.preview_path(sha)))


@unittest.skipUnless(have("ffmpeg") and have("audiowaveform"), "needs ffmpeg and audiowaveform")
class GenerationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old = (Config.WAVEFORM_DIR, Config.PREVIEW_DIR)
        Config.WAVEFORM_DIR, Config.PREVIEW_DIR = os.path.join(self.tmp, "wf"), os.path.join(self.tmp, "pv")

    def tearDown(self):
        Config.WAVEFORM_DIR, Config.PREVIEW_DIR = self._old
        shutil.rmtree(self.tmp)

    def wav(self, name, lavfi, channels=1, rate=48000):
        path = os.path.join(self.tmp, name)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", lavfi, "-ac", str(channels), "-ar", str(rate), path], check=True)
        return path

    def peaks(self, dat):
        h = pv.read_waveform_header(dat)
        with open(dat, "rb") as f:
            f.seek(h["header_size"])
            raw = f.read()
        vals = struct.unpack(f"{len(raw)}b", raw)
        return [max(abs(vals[i]), abs(vals[i + 1])) for i in range(0, len(vals), 2)]

    def test_primary_engine_makes_a_correct_waveform(self):
        # 1 s of silence then 1 s of a 440 Hz tone: the peaks must show exactly that.
        src = self.wav("sil_tone.wav", "aevalsrc='if(lt(t,1),0,0.5*sin(2*PI*440*t))':d=2:s=48000")
        meta = pv.generate_waveform(src, "cd" * 32)
        self.assertEqual(meta["engine"], "audiowaveform")     # the primary path really ran
        self.assertIsNotNone(meta["engine_version"])
        dat = pv.waveform_path("cd" * 32)
        h = pv.read_waveform_header(dat)
        self.assertEqual((h["sample_rate"], h["samples_per_pixel"]), (48000, 480))
        self.assertAlmostEqual(h["duration"], 2.0, delta=0.02)
        p = self.peaks(dat)
        self.assertLessEqual(max(p[:90]), 2, "the silent half must be flat")
        self.assertGreaterEqual(min(p[110:190]), 40, "the tone half must show a clear level")
        with open(dat[:-4] + ".json") as f:
            self.assertEqual(json.load(f)["source_sha256"], "cd" * 32)

    def test_stereo_source_is_mixed_to_mono(self):
        src = self.wav("st.wav", "sine=frequency=330:duration=1", channels=2)
        pv.generate_waveform(src, "ef" * 32)
        self.assertEqual(pv.read_waveform_header(pv.waveform_path("ef" * 32))["channels"], 1)

    def test_sample_rate_drives_samples_per_pixel(self):
        src = self.wav("r44.wav", "sine=frequency=330:duration=1", rate=44100)
        pv.generate_waveform(src, "12" * 32)
        h = pv.read_waveform_header(pv.waveform_path("12" * 32))
        self.assertEqual((h["sample_rate"], h["samples_per_pixel"]), (44100, 441))

    def test_garbage_input_fails_loudly_and_leaves_no_partial_file(self):
        bad = os.path.join(self.tmp, "bad.wav")
        with open(bad, "wb") as f:
            f.write(b"this is not audio at all" * 100)
        with self.assertRaises(Exception):
            pv.generate_waveform(bad, "34" * 32)
        leftovers = [f for _, _, fs in os.walk(self.tmp) for f in fs if f.endswith((".part", ".dat"))]
        self.assertEqual(leftovers, [])

    def test_preview_is_aac_and_the_right_length(self):
        src = self.wav("p.wav", "sine=frequency=330:duration=3", channels=2)
        size = pv.generate_preview(src, "56" * 32)
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,channels:format=duration,bit_rate",
                              "-of", "json", pv.preview_path("56" * 32)], capture_output=True, text=True, check=True)
        info = json.loads(out.stdout)
        self.assertEqual(info["streams"][0]["codec_name"], "aac")
        self.assertAlmostEqual(float(info["format"]["duration"]), 3.0, delta=0.15)
        self.assertGreater(size, 1024)
        self.assertEqual([f for _, _, fs in os.walk(self.tmp) for f in fs if ".part" in f], [])

    def test_mono_preview_is_smaller_than_stereo_at_the_same_setting(self):
        mono = self.wav("m.wav", "sine=frequency=330:duration=3", channels=1)
        st = self.wav("s.wav", "sine=frequency=330:duration=3", channels=2)
        a, b = pv.generate_preview(mono, "78" * 32), pv.generate_preview(st, "9a" * 32)
        self.assertLess(a, b)

    def test_delete_cache_removes_everything_for_a_checksum(self):
        src = self.wav("d.wav", "sine=frequency=330:duration=1")
        pv.generate_waveform(src, "bc" * 32)
        pv.generate_preview(src, "bc" * 32)
        pv.delete_cache("bc" * 32)
        self.assertEqual([f for _, _, fs in os.walk(self.tmp) for f in fs if f.startswith("bc" * 4)], [])


if __name__ == "__main__":
    unittest.main()
