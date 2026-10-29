"""Sort board must not rebuild the whole queue on Select all."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class SortSelUiTests(unittest.TestCase):
    def test_select_all_patches_selection_in_place(self):
        html = (ROOT / "static" / "index.html").read_text()
        addon = (ROOT / "faceid-addon" / "static" / "index.html").read_text()
        self.assertEqual(html, addon)
        self.assertIn("function syncSelUI()", html)
        # Select all / click / clear must not innerHTML the board.
        self.assertIn("t.dataset.selcard", html)
        block = html.split("if(t.dataset.selcard)")[1].split("if(t.dataset.expand)")[0]
        self.assertIn("syncSelUI()", block)
        self.assertNotIn("renderSort()", block)
        self.assertIn("OPEN_MAX=24", html)

    def test_sort_cards_show_the_gallery_reference_face(self):
        html = (ROOT / "static" / "index.html").read_text()
        addon = (ROOT / "faceid-addon" / "static" / "index.html").read_text()
        self.assertEqual(html, addon)
        self.assertIn("function guessPhoto(", html)
        self.assertIn("function cardRefHtml(", html)
        self.assertIn("guess_top_photo", html)
        self.assertIn("class=\"who-ref\"", html)
        self.assertIn("class=\"guess-thumb\"", html)
        self.assertIn("class=\"guess-pop\"", html)
        # Suggested / Maybe headers must actually paint the cue, not only define helpers.
        header = html.split("function renderSort()")[1].split("function pileHtml(")[0]
        self.assertIn("cardRefHtml(c)", header)

    def test_sort_board_mints_unnamed_and_labeled_track_as_new(self):
        html = (ROOT / "static" / "index.html").read_text()
        self.assertIn("id=\"newPile\"", html)
        self.assertIn("id=\"unnamedPile\"", html)
        self.assertIn("function mintName(label)", html)
        self.assertIn("id=\"newTrack\"", html)
        self.assertIn("assign(mintName(v))", html)
        self.assertIn("assign(mintName(''))", html)

    def test_selection_bar_stays_visible_when_the_people_rail_is_on_screen(self):
        # Desktop used to hide #sel while the rail intersected the viewport, so
        # Discard only appeared after scrolling the first pile off-screen.
        html = (ROOT / "static" / "index.html").read_text()
        addon = (ROOT / "faceid-addon" / "static" / "index.html").read_text()
        self.assertEqual(html, addon)
        self.assertNotRegex(
            html,
            r"rail-in-view[^{]*\.sel\s*\{[^}]*display:\s*none",
            "selection bar must stay on screen when a crop is selected",
        )
        self.assertIn('id="selDisc"', html)


if __name__ == "__main__":
    unittest.main()
