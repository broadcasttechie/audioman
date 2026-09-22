"""
Unit tests for the pure detection functions in jobs/grouping.py (no database).
The split-chain cases use the real timestamps/sizes documented in
tests/fixtures/sample_filenames.txt and PLAN 18.5f (confirmed chains: 2026-09-17
10:07:46 -> 10:37:48 -> 11:07:48, and 2026-09-14 14:58:38 -> 15:28:40).
"""
import unittest
from datetime import datetime

from jobs.grouping import (
    GroupCandidate, find_split_chains, find_multitrack_by_time, find_multitrack_by_batch, _shared_label,
)

INSTA = "Insta360 mic"


def cand(id, filename, captured_at=None, duration=None, profile=None, split_seconds=None,
        precision="exact", created_at=None, folder=None):
    return GroupCandidate(
        id=id, filename=filename, captured_at=captured_at, captured_at_precision=precision,
        duration_seconds=duration, created_at=created_at or datetime(2026, 1, 1), profile=profile,
        split_seconds=split_seconds, folder=folder,
    )


class SplitChainTests(unittest.TestCase):
    def test_three_part_chain_2026_09_17(self):
        # audio_260917_100746 (691MB=1800s) -> audio_260917_103748 (691MB=1800s, +2s gap)
        # -> audio_260917_110748 (408MB, partial, +0s gap)
        items = [
            cand("p1", "audio_260917_100746_32bit_orig_stereo.wav", datetime(2026, 9, 17, 10, 7, 46), 1800.0, INSTA, 1800),
            cand("p2", "audio_260917_103748_32bit_orig_stereo.wav", datetime(2026, 9, 17, 10, 37, 48), 1800.0, INSTA, 1800),
            cand("p3", "audio_260917_110748_32bit_orig_stereo.wav", datetime(2026, 9, 17, 11, 7, 48), 1060.0, INSTA, 1800),
        ]
        chains = find_split_chains(items)
        self.assertEqual(len(chains), 1)
        self.assertEqual(chains[0]["ids"], ["p1", "p2", "p3"])
        self.assertEqual(chains[0]["type"], "split")
        self.assertIn("3 parts", chains[0]["reason"])

    def test_two_part_chain_2026_09_14(self):
        items = [
            cand("q1", "audio_260914_145838_32bit_orig_stereo.wav", datetime(2026, 9, 14, 14, 58, 38), 1800.0, INSTA, 1800),
            cand("q2", "audio_260914_152840_32bit_orig_stereo.wav", datetime(2026, 9, 14, 15, 28, 40), 900.0, INSTA, 1800),
        ]
        chains = find_split_chains(items)
        self.assertEqual([c["ids"] for c in chains], [["q1", "q2"]])

    def test_standalone_recording_is_not_a_chain(self):
        # audio_260917_091124 (346MB ~ 900s): far short of a full 1800s part, so it can't start a chain.
        items = [cand("solo", "audio_260917_091124_32bit_orig_stereo.wav", datetime(2026, 9, 17, 9, 11, 24), 900.0, INSTA, 1800)]
        self.assertEqual(find_split_chains(items), [])

    def test_two_short_recordings_close_together_is_not_a_chain(self):
        # audio_260914_191744 (224MB) then audio_260914_192731 (257MB): close in time (~9m47s) but
        # neither is anywhere near a full 1800s part, and the gap itself is also way over tolerance.
        items = [
            cand("r1", "audio_260914_191744_32bit_orig_stereo.wav", datetime(2026, 9, 14, 19, 17, 44), 590.0, INSTA, 1800),
            cand("r2", "audio_260914_192731_32bit_orig_stereo.wav", datetime(2026, 9, 14, 19, 27, 31), 670.0, INSTA, 1800),
        ]
        self.assertEqual(find_split_chains(items), [])

    def test_full_length_parts_too_far_apart_is_not_a_chain(self):
        # Two genuinely full-length (1800s) parts, but the second starts 10 minutes after the first
        # would have ended -- not the recorder's own ~2s splice gap, so these are two separate takes.
        items = [
            cand("a", "audio_260101_100000_32bit_orig_stereo.wav", datetime(2026, 1, 1, 10, 0, 0), 1800.0, INSTA, 1800),
            cand("b", "audio_260101_105000_32bit_orig_stereo.wav", datetime(2026, 1, 1, 10, 50, 0), 1800.0, INSTA, 1800),
        ]
        self.assertEqual(find_split_chains(items), [])

    def test_different_profiles_are_never_chained_together(self):
        items = [
            cand("x1", "audio_260101_100000_32bit_orig_stereo.wav", datetime(2026, 1, 1, 10, 0, 0), 1800.0, "Insta360 mic", 1800),
            cand("x2", "other_260101_103002.wav", datetime(2026, 1, 1, 10, 30, 2), 1800.0, "Some other splitter", 1800),
        ]
        self.assertEqual(find_split_chains(items), [])

    def test_small_negative_gap_within_tolerance_still_chains(self):
        # A next part timestamped a couple of seconds "before" the mathematical end (clock rounding).
        items = [
            cand("a", "audio_260101_100000_32bit_orig_stereo.wav", datetime(2026, 1, 1, 10, 0, 0), 1800.0, INSTA, 1800),
            cand("b", "audio_260101_102958_32bit_orig_stereo.wav", datetime(2026, 1, 1, 10, 29, 58), 600.0, INSTA, 1800),
        ]
        chains = find_split_chains(items, gap_min=-3, gap_max=30)
        self.assertEqual([c["ids"] for c in chains], [["a", "b"]])


class MultitrackByTimeTests(unittest.TestCase):
    def test_two_tracks_starting_together(self):
        items = [
            cand("t1", "Desk_Tr1.wav", datetime(2026, 1, 1, 20, 0, 0), 2700.0, precision="exact"),
            cand("t2", "Desk_Tr2.wav", datetime(2026, 1, 1, 20, 0, 2), 2701.0, precision="exact"),
        ]
        groups = find_multitrack_by_time(items)
        self.assertEqual(len(groups), 1)
        self.assertEqual(set(groups[0]["ids"]), {"t1", "t2"})
        self.assertEqual(groups[0]["type"], "multitrack")
        # "Desk_Tr" is common literal text, so only the digit actually varies.
        self.assertEqual(groups[0]["labels"], {"t1": "1", "t2": "2"})

    def test_four_channel_recorder(self):
        items = [cand(f"c{i}", f"Gig_CH0{i}.wav", datetime(2026, 1, 1, 20, 0, i), 1200.0, precision="exact") for i in range(1, 5)]
        groups = find_multitrack_by_time(items)
        self.assertEqual(len(groups), 1)
        self.assertEqual(set(groups[0]["ids"]), {"c1", "c2", "c3", "c4"})
        self.assertEqual(groups[0]["labels"]["c1"], "1")

    def test_rejects_when_durations_disagree(self):
        items = [
            cand("t1", "Desk_Tr1.wav", datetime(2026, 1, 1, 20, 0, 0), 2700.0, precision="exact"),
            cand("t2", "Desk_Tr2.wav", datetime(2026, 1, 1, 20, 0, 2), 900.0, precision="exact"),  # a third as long
        ]
        self.assertEqual(find_multitrack_by_time(items), [])

    def test_rejects_when_names_are_unrelated(self):
        items = [
            cand("t1", "yellow.wav", datetime(2026, 1, 1, 20, 0, 0), 300.0, precision="exact"),
            cand("t2", "STE-000.wav", datetime(2026, 1, 1, 20, 0, 1), 300.0, precision="exact"),
        ]
        self.assertEqual(find_multitrack_by_time(items), [])

    def test_lone_file_is_never_a_group(self):
        items = [cand("t1", "Desk_Tr1.wav", datetime(2026, 1, 1, 20, 0, 0), 2700.0, precision="exact")]
        self.assertEqual(find_multitrack_by_time(items), [])

    def test_span_cap_rejects_a_cluster_that_drifts_too_wide(self):
        # Consecutive gaps are all within start_tolerance, but the whole cluster spans more than
        # max_span -- a chain of coincidental near-misses, not one simultaneous take.
        items = [
            cand("a", "X_1.wav", datetime(2026, 1, 1, 20, 0, 0), 100.0, precision="exact"),
            cand("b", "X_2.wav", datetime(2026, 1, 1, 20, 0, 8), 100.0, precision="exact"),
        ]
        self.assertEqual(find_multitrack_by_time(items, start_tolerance=100, max_span=5), [])
        groups = find_multitrack_by_time(items, start_tolerance=100, max_span=10)
        self.assertEqual(len(groups), 1)


class MultitrackByBatchTests(unittest.TestCase):
    def test_same_folder_arriving_together(self):
        items = [
            cand("s1", "STE-010.wav", duration=600.0, precision="unknown", created_at=datetime(2026, 1, 1, 12, 0, 0), folder="Gig 12 March"),
            cand("s2", "STE-011.wav", duration=601.0, precision="unknown", created_at=datetime(2026, 1, 1, 12, 0, 5), folder="Gig 12 March"),
        ]
        groups = find_multitrack_by_batch(items)
        self.assertEqual(len(groups), 1)
        self.assertEqual(set(groups[0]["ids"]), {"s1", "s2"})

    def test_different_folders_never_grouped(self):
        items = [
            cand("s1", "STE-010.wav", duration=600.0, precision="unknown", created_at=datetime(2026, 1, 1, 12, 0, 0), folder="Gig 12 March"),
            cand("s2", "STE-011.wav", duration=600.0, precision="unknown", created_at=datetime(2026, 1, 1, 12, 0, 5), folder="A different gig"),
        ]
        self.assertEqual(find_multitrack_by_batch(items), [])

    def test_arriving_far_apart_in_time_never_grouped(self):
        items = [
            cand("s1", "STE-010.wav", duration=600.0, precision="unknown", created_at=datetime(2026, 1, 1, 12, 0, 0), folder="Gig 12 March"),
            cand("s2", "STE-011.wav", duration=600.0, precision="unknown", created_at=datetime(2026, 1, 2, 12, 0, 0), folder="Gig 12 March"),
        ]
        self.assertEqual(find_multitrack_by_batch(items), [])

    def test_no_folder_never_grouped(self):
        items = [
            cand("s1", "STE-010.wav", duration=600.0, precision="unknown", created_at=datetime(2026, 1, 1, 12, 0, 0), folder=None),
            cand("s2", "STE-011.wav", duration=600.0, precision="unknown", created_at=datetime(2026, 1, 1, 12, 0, 1), folder=None),
        ]
        self.assertEqual(find_multitrack_by_batch(items), [])


class SharedLabelTests(unittest.TestCase):
    def test_finds_short_varying_suffix(self):
        result = _shared_label(["Desk_Tr1", "Desk_Tr2", "Desk_Tr3"])
        self.assertEqual(result[0], "Desk_Tr")
        self.assertEqual(result[1], {"Desk_Tr1": "1", "Desk_Tr2": "2", "Desk_Tr3": "3"})

    def test_rejects_unrelated_names(self):
        self.assertIsNone(_shared_label(["yellow", "STE-000"]))

    def test_rejects_a_single_name(self):
        self.assertIsNone(_shared_label(["only_one"]))

    def test_rejects_identical_names(self):
        # Two STE-000.wav from different folders share the same stripped name: no varying part at
        # all to use as a track label (this is a real duplicate/collision, not a multitrack take).
        self.assertIsNone(_shared_label(["STE-000", "STE-000"]))

    def test_rejects_overly_long_suffix(self):
        # A long free-text remainder isn't a track label -- these are two differently-described
        # recordings, not two channels of one take.
        self.assertIsNone(_shared_label([
            "Recording of the desk mix from the mixing desk",
            "Recording of the audience from row twelve",
        ]))


if __name__ == "__main__":
    unittest.main()
