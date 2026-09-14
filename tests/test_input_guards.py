"""Die zwei Abstuerze aus dem Review zu #21 — beide waren nachgestellt, beide hier fixiert."""
import unittest

from app.frigate_api import DisabledFrigateAPI, FrigateAPI, frigate_client
from app.webui import _frigate_url


class FrigateUrlTests(unittest.TestCase):
    def test_empty_url_key_does_not_raise(self):
        # "url:" ohne Wert ist in YAML None, nicht der fehlende Schluessel — genau das
        # hat frueher /api/unknowns mit AttributeError zerlegt.
        self.assertEqual(_frigate_url({"frigate": {"url": None}}), "")

    def test_missing_key_and_missing_section(self):
        self.assertEqual(_frigate_url({"frigate": {}}), "")
        self.assertEqual(_frigate_url({}), "")

    def test_normal_url_keeps_working_without_trailing_slash(self):
        self.assertEqual(_frigate_url({"frigate": {"url": "http://f:5000/"}}), "http://f:5000")


class DisabledClientTests(unittest.TestCase):
    def test_disabled_client_is_a_frigate_api(self):
        # Die Annotation frigate_client(...) -> FrigateAPI muss stimmen, sonst faellt
        # eine spaeter ergaenzte Methode erst im Ordnerbetrieb zur Laufzeit auf.
        client = frigate_client({"frigate": {"enabled": False}})
        self.assertIsInstance(client, DisabledFrigateAPI)
        self.assertIsInstance(client, FrigateAPI)
        self.assertFalse(client.enabled)

    def test_disabled_client_answers_without_network(self):
        c = DisabledFrigateAPI()
        self.assertIsNone(c.snapshot("e1"))
        self.assertIsNone(c.live_frame("cam"))
        self.assertEqual(c.events(), [])
        self.assertEqual(c.config(), {"cameras": {}})

    def test_an_unoverridden_network_path_says_what_is_wrong(self):
        with self.assertRaises(RuntimeError) as ctx:
            DisabledFrigateAPI().session.get("http://example.invalid")
        self.assertIn("Frigate is disabled", str(ctx.exception))

    def test_a_missing_frigate_section_is_the_same_as_off(self):
        # Wer den Ordner nutzt, loescht den Block oft ganz. Mit fester Vorgabe True
        # stuerzte der Start hier mit KeyError: 'url' ab, bevor irgendetwas lief.
        for cfg in ({}, {"frigate": {}}, {"frigate": None}, {"frigate": {"url": ""}}):
            with self.subTest(cfg=cfg):
                self.assertIsInstance(frigate_client(cfg), DisabledFrigateAPI)

    def test_enabled_without_a_url_says_what_is_wrong(self):
        with self.assertRaises(ValueError) as ctx:
            frigate_client({"frigate": {"enabled": True}})
        self.assertIn("frigate.url is empty", str(ctx.exception))

    def test_enabled_by_default_so_existing_configs_are_untouched(self):
        client = frigate_client({"frigate": {"url": "http://f:5000"}})
        self.assertIsInstance(client, FrigateAPI)
        self.assertNotIsInstance(client, DisabledFrigateAPI)
        self.assertTrue(client.enabled)


if __name__ == "__main__":
    unittest.main()
