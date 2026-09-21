"""Run on the container: python3 -m unittest discover -s tests"""
import unittest
from datetime import datetime
from types import SimpleNamespace as NS

from jobs.path_template import render_path, safe_component


def res(filename="take.wav", category="ambient", captured=datetime(2026, 9, 15, 8, 11)):
    return NS(filename=filename, category=category, captured_at=captured)


PROJECT = NS(slug="the-show")


class SafeComponentTests(unittest.TestCase):
    def test_separators_and_reserved_characters(self):
        self.assertEqual(safe_component("Night 2 / Act 1"), "Night 2 - Act 1")
        self.assertEqual(safe_component("a\\b:c*d?e"), "a-b-c-d-e")

    def test_dots_and_spaces_cannot_escape_or_hide(self):
        for bad in ("..", ".", "  ", "", "...", ". ."):
            self.assertEqual(safe_component(bad), "unnamed", repr(bad))
        self.assertEqual(safe_component("../../etc"), "-..-etc".strip(" ."))  # separators neutralised
        self.assertNotIn("/", safe_component("../../etc"))

    def test_trailing_dot_and_space_dropped_and_whitespace_collapsed(self):
        self.assertEqual(safe_component("  Night   2. "), "Night 2")

    def test_control_characters(self):
        self.assertEqual(safe_component("a\x00b\nc"), "a-b-c")


class RenderTests(unittest.TestCase):
    def test_project_with_session(self):
        self.assertEqual(render_path(res(), PROJECT, NS(name="2026-09-15 Night 2")), "the-show/2026-09-15 Night 2/take.wav")

    def test_project_without_session(self):
        self.assertEqual(render_path(res(), PROJECT), "the-show/take.wav")

    def test_loose_is_filed_by_year_and_month(self):
        self.assertEqual(render_path(res()), "misc/ambient/2026/09/take.wav")
        self.assertEqual(render_path(res(captured=datetime(2025, 1, 3))), "misc/ambient/2025/01/take.wav")

    def test_loose_without_a_date(self):
        self.assertEqual(render_path(res(captured=None)), "misc/ambient/unknown/unknown/take.wav")

    def test_loose_outing_session_gets_its_own_folder(self):
        self.assertEqual(render_path(res(), None, NS(name="Parkridge walk")), "misc/ambient/2026/09/Parkridge walk/take.wav")

    def test_uncategorised(self):
        self.assertEqual(render_path(res(category=None)), "misc/uncategorised/2026/09/take.wav")

    def test_hostile_session_name_stays_inside_its_folder(self):
        path = render_path(res(), PROJECT, NS(name="../../outside"))
        self.assertNotIn("..", path.split("/"))
        self.assertTrue(path.startswith("the-show/"))
        self.assertEqual(len(path.split("/")), 3)

    def test_original_filename_is_never_altered(self):
        odd = "Copy of 250424-121237-Parkridge: Nature Reserve?-EDIT.WAV.wav"
        self.assertTrue(render_path(res(filename=odd), PROJECT).endswith("/" + odd))


if __name__ == "__main__":
    unittest.main()
