"""Was bei einer Zuordnung schiefgehen darf, ohne Spuren zu hinterlassen.

Gemessen an der laufenden Instanz: POST /api/unknowns/assign mit veralteten IDs gab
HTTP 200 und {"assigned": 0, "slug": "testperson"} zurueck — und die Person war
angelegt. Ueber "Track as new" (PR #32) heisst das: jeder Fehlversuch laesst ein
leeres Unnamed-xxxxxx in der Galerie zurueck.
"""
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.gallery import Gallery
from app.webui import build_app


def _app(tmp: Path):
    data = tmp / "data"
    (data / "persons").mkdir(parents=True)
    (data / "ignored").mkdir()
    gallery = Gallery(data, top_k=3, max_per_person=40)
    cfg = {"faceid": {"match_threshold": 0.5, "unknown_threshold": 0.35,
                      "suggest_threshold": 0.4, "cluster_eps": 0.55,
                      "ignore_threshold": 0.5, "dedupe_threshold": 0.65},
           "folder": {"enabled": False}, "frigate": {"url": ""}, "mqtt": {}}
    proc = SimpleNamespace(_announced=set(), match_thr=0.5, unknown_thr=0.35,
                           ignore_thr=0.5, history=None, logbuffer=None,
                           frigate=SimpleNamespace(enabled=False),
                           folder_ingest=SimpleNamespace(status=lambda: {},
                                                         max_indexed_files=5000,
                                                         enabled=False))
    app = build_app(cfg, None, gallery, proc, data, Path("static"))
    return app, gallery


def _route(app, path: str):
    for r in app.routes:
        if getattr(r, "path", None) == path:
            return r.endpoint
    raise AssertionError(f"route {path} not found")


class AssignGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.gallery = _app(Path(self.tmp.name))
        self.assign = _route(self.app, "/api/unknowns/assign")
        from app.webui import AssignBody
        self.Body = AssignBody

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_failed_assign_does_not_leave_an_empty_person(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as cm:
            self.assign(self.Body(ids=["does-not-exist"], person="Unnamed-ab12cd"))
        self.assertEqual(cm.exception.status_code, 409)
        self.assertEqual(self.gallery.persons(), {},
                         "die fuer diesen Aufruf angelegte Person muss weg sein")
        self.assertFalse((Path(self.tmp.name) / "data" / "persons" / "unnamed-ab12cd").exists())

    def test_an_existing_person_survives_a_failed_assign(self):
        from fastapi import HTTPException
        slug = self.gallery.create_person("Juli")
        with self.assertRaises(HTTPException) as cm:
            self.assign(self.Body(ids=["does-not-exist"], person=slug))
        self.assertEqual(cm.exception.status_code, 409)
        self.assertIn(slug, self.gallery.persons(),
                      "eine vorhandene Person darf ein Fehlversuch nie loeschen")

    def test_a_display_name_for_an_existing_person_never_deletes_it(self):
        # create_person ist idempotent: "Juli" liefert den vorhandenen Slug "juli"
        # zurueck. Wer am Eingabewert statt am aufgeloesten Slug entscheidet, loescht
        # hier eine bestehende Person samt Gesichtern.
        from fastapi import HTTPException
        slug = self.gallery.create_person("Juli")
        self.assertEqual(slug, "juli")
        for typed in ("Juli", " juli ", "JULI"):
            with self.assertRaises(HTTPException) as cm:
                self.assign(self.Body(ids=["does-not-exist"], person=typed))
            self.assertEqual(cm.exception.status_code, 409)
            self.assertIn(slug, self.gallery.persons(),
                          f"Eingabe {typed!r} darf die vorhandene Person nicht loeschen")

    def test_a_successful_assign_still_returns_the_count(self):
        # Gegenprobe: der 409 darf nur den Nullfall treffen.
        import numpy as np
        slug = self.gallery.create_person("Juli")
        emb = np.ones(512, dtype=np.float32) / np.sqrt(512)
        crop = np.full((80, 80, 3), 127, dtype=np.uint8)
        uid = self.gallery.save_unknown(crop, emb, {"camera": "entrance"})
        self.assertIsNotNone(uid)
        r = self.assign(self.Body(ids=[uid], person=slug))
        self.assertEqual(r["assigned"], 1)
        self.assertEqual(r["slug"], slug)
        self.assertTrue(r.get("queued"))
        deadline = time.time() + 2.0
        while time.time() < deadline and (self.gallery.unknown_dir / f"{uid}.json").exists():
            time.sleep(0.03)
