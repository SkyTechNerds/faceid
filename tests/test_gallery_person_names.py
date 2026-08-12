import tempfile
import unittest
from pathlib import Path

from app.gallery import Gallery, slugify


class PersonNameTests(unittest.TestCase):
    def test_non_latin_names_get_distinct_stable_slugs(self):
        self.assertEqual(slugify("Σοφία"), slugify("σοφία"))
        self.assertNotEqual(slugify("Σοφία"), slugify("さくら"))
        self.assertRegex(slugify("Σοφία"), r"^person-[0-9a-f]{10}$")

    def test_people_with_names_from_different_scripts_are_both_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            gallery = Gallery(Path(tmp))
            first = gallery.create_person("Σοφία")
            second = gallery.create_person("さくら")

            self.assertNotEqual(first, second)
            self.assertEqual(
                {person["name"] for person in gallery.persons().values()},
                {"Σοφία", "さくら"},
            )

    def test_ascii_slug_collisions_do_not_reuse_another_person(self):
        with tempfile.TemporaryDirectory() as tmp:
            gallery = Gallery(Path(tmp))
            first = gallery.create_person("Alex")
            second = gallery.create_person("Alex!")

            self.assertEqual(first, "alex")
            self.assertNotEqual(first, second)
            self.assertEqual(len(gallery.persons()), 2)

    def test_same_unicode_name_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            gallery = Gallery(Path(tmp))
            first = gallery.create_person("Σοφία")
            second = gallery.create_person("σοφία")

            self.assertEqual(first, second)
            self.assertEqual(len(gallery.persons()), 1)


if __name__ == "__main__":
    unittest.main()
