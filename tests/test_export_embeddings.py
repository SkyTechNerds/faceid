"""Export der Galerie-Fingerabdruecke fuer WAYFARE (14.11.2026): nur Zahlen, keine Bilder, keine Quellen."""
import base64
import tempfile
import unittest
from pathlib import Path

import numpy as np

from tests.test_assign_guards import _app, _route
from tests.test_gallery_cover import _face


def _vectors(person):
    raw = base64.b64decode(person["embeddings"])
    return np.frombuffer(raw, dtype="<f2").astype(np.float32).reshape(person["count"], 512)


class ExportEmbeddingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.gallery = _app(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_export_has_all_persons_with_their_vectors(self):
        a = self.gallery.create_person("Eli")
        b = self.gallery.create_person("Juli")
        for seed in (1, 2):
            _face(self.gallery, a, seed)
        for seed in (3, 4, 5):
            _face(self.gallery, b, seed)
        out = self.gallery.export_embeddings()
        by = {p["slug"]: p for p in out["persons"]}
        self.assertEqual(2, by[a]["count"])
        self.assertEqual(3, by[b]["count"])
        orig = self.gallery.embeddings(b)
        back = _vectors(by[b])
        for o, r in zip(orig, back):
            self.assertGreaterEqual(float(o @ r / np.linalg.norm(r)), 0.999)

    def test_export_leaves_out_files_sources_and_ignored(self):
        s = self.gallery.create_person("Eli")
        _face(self.gallery, s, 1)
        out = self.gallery.export_embeddings()
        self.assertEqual({"slug", "name", "favorite", "count", "updated", "embeddings"}, set(out["persons"][0].keys()))
        self.assertEqual("buffalo_l/w600k_r50", out["model"])
        self.assertEqual(512, out["dim"])

    def test_export_skips_empty_person(self):
        self.gallery.create_person("Leer")
        s = self.gallery.create_person("Eli")
        _face(self.gallery, s, 1)
        self.assertEqual([s], [p["slug"] for p in self.gallery.export_embeddings()["persons"]])

    def test_route_returns_json_with_effective_thresholds(self):
        s = self.gallery.create_person("Eli")
        _face(self.gallery, s, 1)
        out = _route(self.app, "/api/export/embeddings")()
        self.assertEqual(512, out["dim"])
        self.assertEqual(0.5, out["threshold"])
        self.assertEqual(0.35, out["unknown"])
        self.assertEqual(1, len(out["persons"]))


if __name__ == "__main__":
    unittest.main()
