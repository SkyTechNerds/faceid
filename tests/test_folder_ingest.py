import json
import tempfile
import time
import unittest
from pathlib import Path

import cv2
import numpy as np

from app.folder_ingest import (FolderIngest, MediaReadError, _sample_frames,
                                scan_media)


class FakeFace:
    def __init__(self, embedding, bbox=(10, 10, 90, 90), score=0.9):
        emb = np.asarray(embedding, dtype=np.float32)
        self.normed_embedding = emb / np.linalg.norm(emb)
        self.bbox = np.asarray(bbox, dtype=np.float32)
        self.det_score = score


class FakeEngine:
    def __init__(self, per_frame):
        self.per_frame = per_frame
        self.calls = 0

    def faces(self, frame):
        value = self.per_frame[min(self.calls, len(self.per_frame) - 1)]
        self.calls += 1
        return value


class FakeProcessor:
    def __init__(self):
        self.calls = []

    def process_local_face(self, eid, camera, event_ts, frame, face, media_path):
        self.calls.append((eid, camera, event_ts, media_path, face))
        return {"event_id": eid, "person": "unknown", "score": 0.0}


def write_image(path: Path, value=120):
    image = np.full((120, 120, 3), value, dtype=np.uint8)
    assert cv2.imwrite(str(path), image)


class ScanMediaTests(unittest.TestCase):
    def test_repeated_face_is_collapsed_and_distinct_face_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "frame.jpg"
            write_image(path)
            first = FakeFace([1.0, 0.0])
            same = FakeFace([0.98, 0.02])
            other = FakeFace([0.0, 1.0], bbox=(15, 15, 105, 105))
            engine = FakeEngine([[first, same, other]])
            faces, stats = scan_media(engine, path, same_person_similarity=0.8)
            self.assertEqual(len(faces), 2)
            self.assertEqual(stats["detections"], 3)
            self.assertEqual(stats["distinct_faces"], 2)

    def test_small_and_low_confidence_faces_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "frame.jpg"
            write_image(path)
            small = FakeFace([1.0, 0.0], bbox=(0, 0, 20, 20))
            weak = FakeFace([0.0, 1.0], score=0.2)
            faces, stats = scan_media(FakeEngine([[small, weak]]), path,
                                      min_face_px=48, min_det=0.65)
            self.assertEqual(faces, [])
            self.assertEqual(stats["detections"], 0)

    def test_invalid_media_raises_instead_of_being_marked_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.mp4"
            path.write_bytes(b"not a video")
            with self.assertRaises(MediaReadError):
                scan_media(FakeEngine([[]]), path)

    def test_video_sampling_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "visit.avi"
            writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"),
                                     10.0, (120, 120))
            self.assertTrue(writer.isOpened())
            for value in range(30):
                writer.write(np.full((120, 120, 3), value, dtype=np.uint8))
            writer.release()
            engine = FakeEngine([[]])
            faces, stats = scan_media(engine, path, max_frames=5)
            self.assertEqual(faces, [])
            self.assertEqual(stats["frames"], 5)
            self.assertEqual(engine.calls, 5)


class FolderWatcherTests(unittest.TestCase):
    def make_watcher(self, root, process_existing=True, max_retries=3):
        cfg = {
            "faceid": {"min_face_px": 48},
            "folder": {
                "enabled": True, "path": str(root / "input"), "camera": "front_door",
                "extensions": [".jpg", ".mp4"], "settle_seconds": 0,
                "poll_interval": 1, "process_existing": process_existing,
                "max_retries": max_retries, "retry_seconds": 1,
            },
        }
        processor = FakeProcessor()
        watcher = FolderIngest(cfg, root / "data", FakeEngine([[FakeFace([1.0, 0.0])]]),
                               processor)
        return watcher, processor

    def test_file_must_be_unchanged_across_two_scans_and_runs_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "input").mkdir()
            path = root / "input" / "visit.jpg"
            write_image(path)
            watcher, processor = self.make_watcher(root)

            self.assertEqual(watcher.scan_once(now=2_000_000_000)["processed"], 0)
            self.assertEqual(watcher.scan_once(now=2_000_000_001)["processed"], 1)
            self.assertEqual(len(processor.calls), 1)
            self.assertEqual(watcher.scan_once(now=2_000_000_002)["processed"], 0)
            self.assertEqual(len(processor.calls), 1)

            reloaded, reloaded_processor = self.make_watcher(root)
            reloaded.scan_once(now=2_000_000_003)
            self.assertEqual(reloaded.scan_once(now=2_000_000_004)["processed"], 0)
            self.assertEqual(reloaded_processor.calls, [])

    def test_changed_file_is_processed_again_with_a_new_event_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "input").mkdir()
            path = root / "input" / "visit.jpg"
            write_image(path, 100)
            watcher, processor = self.make_watcher(root)
            watcher.scan_once(now=2_000_000_000)
            watcher.scan_once(now=2_000_000_001)
            first_id = processor.calls[0][0]
            write_image(path, 200)
            watcher.scan_once(now=2_000_000_002)
            watcher.scan_once(now=2_000_000_003)
            self.assertEqual(len(processor.calls), 2)
            self.assertNotEqual(first_id, processor.calls[1][0])

    def test_existing_files_can_be_baselined_without_processing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "input").mkdir()
            write_image(root / "input" / "old.jpg")
            watcher, processor = self.make_watcher(root, process_existing=False)
            result = watcher.scan_once(now=2_000_000_000)
            self.assertEqual(result["skipped"], 1)
            self.assertEqual(processor.calls, [])

            # Baseline mode only skips startup contents. A later arrival is processed.
            write_image(root / "input" / "new.jpg", 200)
            watcher.scan_once(now=2_000_000_001)
            result = watcher.scan_once(now=2_000_000_002)
            self.assertEqual(result["processed"], 1)
            self.assertEqual(len(processor.calls), 1)

    def test_decode_failure_is_persisted_and_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "input").mkdir()
            (root / "input" / "bad.mp4").write_bytes(b"bad")
            watcher, _ = self.make_watcher(root, max_retries=2)
            watcher.scan_once(now=2_000_000_000)
            self.assertEqual(watcher.scan_once(now=2_000_000_001)["failed"], 1)
            self.assertEqual(watcher.scan_once(now=2_000_000_001.5)["skipped"], 1)
            self.assertEqual(watcher.scan_once(now=2_000_000_002)["failed"], 1)
            self.assertEqual(watcher.scan_once(now=2_000_000_004)["skipped"], 1)
            state = json.loads((root / "data" / "folder_ingest.json").read_text())
            item = next(iter(state["files"].values()))
            self.assertEqual(item["status"], "failed")
            self.assertEqual(item["attempts"], 2)

    def test_temporary_recording_suffix_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "input").mkdir()
            (root / "input" / "visit.mp4.recording").write_bytes(b"partial")
            watcher, processor = self.make_watcher(root)
            first = watcher.scan_once(now=2_000_000_000)
            second = watcher.scan_once(now=2_000_000_001)
            self.assertEqual(first["found"], 0)
            self.assertEqual(second["found"], 0)
            self.assertEqual(processor.calls, [])


if __name__ == "__main__":
    unittest.main()


class ScanLockTests(unittest.TestCase):
    """Die Sperre muss den ganzen Lauf halten und einen zweiten Anlauf ausfallen lassen."""

    def _ingest(self, tmp):
        cfg = {"folder": {"enabled": True, "path": tmp, "process_existing": True},
               "faceid": {}}
        return FolderIngest(cfg, Path(tmp), FakeEngine([]), None)

    def test_second_scan_returns_immediately_instead_of_repeating_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            ing = self._ingest(tmp)
            ing._lock.acquire()
            try:
                result = ing.scan_once()
            finally:
                ing._lock.release()
            self.assertTrue(result["busy"])
            self.assertEqual(result["processed"], 0)
            # Nicht gewartet und danach doch gelaufen: der Zustand ist unberuehrt.
            self.assertEqual(ing._status["last_scan"], 0.0)

    def test_lock_is_released_after_a_failing_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            ing = self._ingest(tmp)
            ing.path = Path(tmp) / "weg"          # existiert nicht -> FileNotFoundError
            with self.assertRaises(FileNotFoundError):
                ing.scan_once()
            self.assertTrue(ing._lock.acquire(blocking=False), "Sperre haengt nach Fehler")
            ing._lock.release()

    def test_progress_reports_during_the_scan_not_only_at_the_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            for n in ("a.mp4", "b.mp4", "c.mp4"):
                (Path(tmp) / n).write_bytes(b"x")
            ing = self._ingest(tmp)
            seen = []
            ing.scan_once(progress=lambda i, total: seen.append((i, total)))
            self.assertEqual(seen[0], (0, 3), "Gesamtzahl muss sofort gemeldet werden")
            self.assertEqual([i for i, _ in seen], [0, 1, 2, 3])


class ImageExtensionTests(unittest.TestCase):
    """Bilder laufen durch dieselbe Kette — aber nur, wenn ihre Endung konfiguriert ist.

    Gemeldet im HA-Forum am 14.09.2026: ein Ordner voller .jpg wurde stillschweigend
    uebergangen. Kein Fehler im Code, sondern die Vorgabe `extensions` ist video-only.
    Beide Richtungen sind hier festgehalten, damit die Vorgabe nicht unbemerkt kippt.
    """

    def _ingest(self, tmp, extensions=None):
        fc = {"enabled": True, "path": tmp, "settle_seconds": 0, "process_existing": True}
        if extensions is not None:
            fc["extensions"] = extensions
        return FolderIngest({"folder": fc, "faceid": {}},
                            Path(tmp), FakeEngine([[FakeFace([1, 0, 0])]]), FakeProcessor())

    def test_image_is_skipped_with_the_default_extensions(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_image(Path(tmp) / "visitor.jpg")
            ing = self._ingest(tmp)
            self.assertNotIn(".jpg", ing.extensions)
            # ``found`` zaehlt direkt nach dem Auflisten und damit vor jeder
            # Settle-Logik — es ist schon im ERSTEN Lauf besetzt (nachgemessen).
            # Der zweite Lauf steht hier trotzdem, damit die Zusicherung auch dann
            # noch traegt, wenn die Reihenfolge im Scan einmal umgebaut wird.
            now = time.time()
            self.assertEqual(ing.scan_once(now=now)["found"], 0)
            self.assertEqual(ing.scan_once(now=now + 60)["found"], 0)

    def test_image_is_processed_once_its_extension_is_listed(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_image(Path(tmp) / "visitor.jpg")
            ing = self._ingest(tmp, [".mp4", ".jpg"])
            now = time.time()
            ing.scan_once(now=now)                  # erster Lauf: beobachten
            result = ing.scan_once(now=now + 60)    # zweiter Lauf: stabil -> verarbeiten
            self.assertEqual(result["found"], 1)
            self.assertEqual(result["processed"], 1)
            self.assertEqual(result["faces"], 1)
            self.assertEqual(len(ing.processor.calls), 1)

    def test_extensions_are_normalised_with_or_without_a_leading_dot(self):
        with tempfile.TemporaryDirectory() as tmp:
            ing = self._ingest(tmp, ["jpg", ".PNG"])
            self.assertEqual(ing.extensions, {".jpg", ".png"})

    def test_a_still_image_decodes_to_exactly_one_frame(self):
        """Der echte Dekodierpfad, ohne Modell: ein Bild ist EIN Bild.

        ``_sample_frames`` entscheidet an der Endung zwischen ``cv2.imread`` und
        ``cv2.VideoCapture``. Ginge ein Bild versehentlich in den Videozweig, meldete
        ``VideoCapture`` null Bilder und die Datei waere still unbrauchbar — genau die
        Regression, die man der Doku-Zusage „dieselbe Kette" nicht ansieht.
        """
        for name in ("visitor.jpg", "visitor.jpeg", "visitor.png", "visitor.webp"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / name
                write_image(path)
                frames = list(_sample_frames(path, max_frames=24))
                self.assertEqual(len(frames), 1, "ein Bild muss genau ein Bild liefern")
                self.assertEqual(frames[0].shape, (120, 120, 3))

    def test_the_decoder_rejects_a_file_that_only_looks_like_an_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "kaputt.jpg"
            path.write_bytes(b"not an image")
            with self.assertRaises(MediaReadError):
                list(_sample_frames(path, max_frames=24))

    def test_an_unreadable_image_is_marked_failed_and_retried_not_done(self):
        """Der Ingest-Weg, nicht nur der Dekoder.

        Eine unlesbare Datei darf nicht als erledigt im Index landen — sonst wird sie
        nie wieder angefasst, obwohl sie beim naechsten Mal vollstaendig sein koennte
        (ein Rekorder, der noch schreibt, sieht genauso aus).
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "kaputt.jpg"
            path.write_bytes(b"not an image")
            ing = self._ingest(tmp, [".jpg"])
            now = time.time()
            ing.scan_once(now=now)
            failed_at = now + 60          # DIESE Uhr zaehlt, nicht die des ersten Laufs
            result = ing.scan_once(now=failed_at)
            self.assertEqual(result["processed"], 0)
            self.assertEqual(result["failed"], 1)
            entry = ing._state["files"][str(path.resolve())]
            self.assertEqual(entry["status"], "failed")
            # Gegen den Zeitpunkt des Fehlschlags pruefen: gegen ``now`` waere die
            # Zusicherung trivial wahr und wuerde auch eine Dauerschleife ohne
            # Wartezeit durchgehen lassen.
            self.assertGreaterEqual(entry["next_retry"], failed_at + ing.retry_seconds)
            self.assertEqual(ing.status()["processed"], 0)


class IndexCapTests(unittest.TestCase):
    """Der Fingerabdruck-Index darf nicht unbegrenzt wachsen.

    Gemeldet im HA-Forum am 15.09.2026: nichts raeumte je auf, und weil der Index bei
    jeder Datei komplett neu geschrieben wird, wachsen die Schreibkosten mit allem, was
    vorher da war — gemessen 320 ms je Datei bei 50 000 Eintraegen.
    """

    def _ingest(self, tmp, cap=None, faceid=None):
        fc = {"enabled": True, "path": tmp, "settle_seconds": 0, "extensions": [".jpg"]}
        if cap is not None:
            fc["max_indexed_files"] = cap
        return FolderIngest({"folder": fc, "faceid": faceid or {}},
                            Path(tmp), FakeEngine([[FakeFace([1, 0, 0])]]), FakeProcessor())

    def _fill(self, ing, n, start=1000.0):
        ing._state["files"] = {
            f"/rec/{i:06d}.jpg": {"signature": [1, i], "status": "processed",
                                  "processed_at": start + i}
            for i in range(n)}

    def test_oldest_entries_are_dropped_down_to_the_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            ing = self._ingest(tmp, cap=100)
            self._fill(ing, 250)
            self.assertEqual(ing._enforce_index_cap(), 150)
            keys = ing._state["files"]
            self.assertEqual(len(keys), 100)
            # Die juengsten 100 muessen ueberlebt haben, die aeltesten weg sein.
            self.assertIn("/rec/000249.jpg", keys)
            self.assertNotIn("/rec/000000.jpg", keys)
            self.assertNotIn("/rec/000149.jpg", keys)

    def test_zero_means_no_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            ing = self._ingest(tmp, cap=0)
            self._fill(ing, 500)
            self.assertEqual(ing._enforce_index_cap(), 0)
            self.assertEqual(len(ing._state["files"]), 500)

    def test_a_file_being_processed_is_never_evicted(self):
        # Sonst liefe dieselbe Datei doppelt durch die Erkennung.
        with tempfile.TemporaryDirectory() as tmp:
            ing = self._ingest(tmp, cap=2)
            self._fill(ing, 5)
            ing._state["files"]["/rec/000000.jpg"] = {"signature": [1, 0],
                                                      "status": "processing", "attempts": 1}
            ing._enforce_index_cap()
            self.assertIn("/rec/000000.jpg", ing._state["files"],
                          "laufender Eintrag darf nicht verdraengt werden")

    def test_saving_alone_does_not_trim(self):
        # Waehrend eines Laufs wird nach jeder Datei gespeichert. Wuerde dabei verdraengt,
        # flogen Eintraege raus, die derselbe Lauf gerade erst geschrieben hat.
        with tempfile.TemporaryDirectory() as tmp:
            ing = self._ingest(tmp, cap=10)
            self._fill(ing, 40)
            ing._save_state()
            self.assertEqual(len(json.loads(ing.state_file.read_text())["files"]), 40)

    def test_the_cap_is_applied_when_a_scan_finishes(self):
        with tempfile.TemporaryDirectory() as tmp:
            ing = self._ingest(tmp, cap=10)
            self._fill(ing, 40)
            ing._finish_scan({"found": 0})
            self.assertEqual(len(json.loads(ing.state_file.read_text())["files"]), 10)

    def test_a_limit_set_in_the_settings_tab_survives_a_restart(self):
        # Der Einstellungen-Tab schreibt nach cfg["faceid"], FolderIngest liest aus
        # cfg["folder"] — ohne den Rueckgriff waere die Einstellung nach einem Neustart weg.
        with tempfile.TemporaryDirectory() as tmp:
            ing = self._ingest(tmp, faceid={"folder_max_indexed_files": 250})
            self.assertEqual(ing.max_indexed_files, 250)

    def test_an_explicit_folder_value_wins_over_the_settings_overlay(self):
        with tempfile.TemporaryDirectory() as tmp:
            ing = self._ingest(tmp, cap=42, faceid={"folder_max_indexed_files": 250})
            self.assertEqual(ing.max_indexed_files, 42)

    def test_the_status_reports_the_cap_alongside_the_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            ing = self._ingest(tmp, cap=100)
            self._fill(ing, 7)
            st = ing.status()
            self.assertEqual(st["indexed"], 7)
            self.assertEqual(st["index_cap"], 100)


class IndexCapRegressionTests(unittest.TestCase):
    """Fuenf Befunde aus dem Review zum Deckel — jeder hier festgenagelt."""

    def _ingest(self, tmp, cap=None, faceid=None):
        fc = {"enabled": True, "path": tmp, "settle_seconds": 0, "extensions": [".jpg"]}
        if cap is not None:
            fc["max_indexed_files"] = cap
        return FolderIngest({"folder": fc, "faceid": faceid or {}},
                            Path(tmp), FakeEngine([[FakeFace([1, 0, 0])]]), FakeProcessor())

    def test_a_scan_processes_every_file_once_even_below_the_cap(self):
        """Verdraengung darf nicht MITTEN im Lauf zuschlagen.

        Sonst wirft der Lauf Eintraege weg, die er selbst gerade geschrieben hat, und
        verarbeitet dieselben Dateien im selben Durchgang erneut.
        """
        with tempfile.TemporaryDirectory() as tmp:
            for i in range(6):
                write_image(Path(tmp) / f"r{i}.jpg")
            ing = self._ingest(tmp, cap=3)
            now = time.time()
            ing.scan_once(now=now)
            first = ing.scan_once(now=now + 60)
            self.assertEqual(first["processed"], 6, "jede Datei genau einmal")
            self.assertEqual(len(ing.processor.calls), 6, "keine Doppelverarbeitung")

    def test_nothing_is_re_processed_when_the_folder_fits_under_the_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            for i in range(4):
                write_image(Path(tmp) / f"r{i}.jpg")
            ing = self._ingest(tmp, cap=100)
            now = time.time()
            ing.scan_once(now=now)
            self.assertEqual(ing.scan_once(now=now + 60)["processed"], 4)
            self.assertEqual(ing.scan_once(now=now + 120)["processed"], 0,
                             "zweiter Durchgang darf nichts wiederholen")

    def test_a_folder_larger_than_the_cap_is_warned_about(self):
        """Ordner groesser als der Deckel heisst zwangslaeufig Wiederholung — das ist
        keine Panne, aber es muss sichtbar sein statt still zu passieren."""
        with tempfile.TemporaryDirectory() as tmp:
            ing = self._ingest(tmp, cap=3)
            with self.assertLogs("faceid.folder", level="WARNING") as logs:
                ing._finish_scan({"found": 10})
            self.assertTrue(any("re-processed on every scan" in m for m in logs.output))

    def test_an_interrupted_entry_is_reset_for_retry_on_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            ing = self._ingest(tmp, cap=10)
            ing._state["files"]["/rec/haengt.jpg"] = {"signature": [1, 2],
                                                      "status": "processing", "attempts": 1}
            ing._save_state()
            again = self._ingest(tmp, cap=10)
            entry = again._state["files"]["/rec/haengt.jpg"]
            self.assertEqual(entry["status"], "failed")
            self.assertEqual(entry["attempts"], 1, "Versuchszaehler muss erhalten bleiben")
            self.assertIn("interrupted", entry["error"])

    def test_interrupted_entries_cannot_make_the_cap_unreachable(self):
        with tempfile.TemporaryDirectory() as tmp:
            ing = self._ingest(tmp, cap=5)
            ing._state["files"] = {f"/rec/{i}.jpg": {"signature": [1, i],
                                                     "status": "processing", "attempts": 1}
                                   for i in range(20)}
            ing._save_state()
            again = self._ingest(tmp, cap=5)
            again._enforce_index_cap()
            self.assertEqual(len(again._state["files"]), 5,
                             "nach dem Zuruecksetzen muessen sie verdraengbar sein")

    def test_set_index_cap_returns_false_while_a_scan_holds_the_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            ing = self._ingest(tmp, cap=10)
            ing._lock.acquire()
            try:
                applied = ing.set_index_cap(3)
            finally:
                ing._lock.release()
            self.assertFalse(applied, "darf nicht auf den laufenden Scan warten")
            # Der Wert wird VORGEMERKT statt sofort geschrieben: der laufende Scan liest
            # das Feld und wuerde die neue Grenze sonst mitten im Lauf anwenden, obwohl
            # hier gerade aufgeschoben wird. Uebernommen wird sie in _finish_scan().
            self.assertEqual(ing.max_indexed_files, 10, "noch die alte Grenze")
            self.assertEqual(ing._pending_cap, 3, "die neue ist vorgemerkt")

    def test_set_index_cap_trims_when_the_lock_is_free(self):
        with tempfile.TemporaryDirectory() as tmp:
            ing = self._ingest(tmp, cap=100)
            ing._state["files"] = {f"/rec/{i:04d}.jpg": {"signature": [1, i],
                                                         "status": "processed",
                                                         "processed_at": 1000.0 + i}
                                   for i in range(30)}
            self.assertTrue(ing.set_index_cap(10))
            self.assertEqual(len(ing._state["files"]), 10)
            self.assertEqual(len(json.loads(ing.state_file.read_text())["files"]), 10)


class IndexCapSecondRoundTests(unittest.TestCase):
    """Vier Befunde aus dem Review zum Fix des Deckels."""

    def _ingest(self, tmp, cap=None):
        fc = {"enabled": True, "path": tmp, "settle_seconds": 0, "extensions": [".jpg"]}
        if cap is not None:
            fc["max_indexed_files"] = cap
        return FolderIngest({"folder": fc, "faceid": {}},
                            Path(tmp), FakeEngine([[FakeFace([1, 0, 0])]]), FakeProcessor())

    def test_the_cap_is_enforced_even_when_the_scan_raises(self):
        """Sonst waechst der Index gerade dort unbegrenzt, wo Scans zuverlaessig scheitern."""
        with tempfile.TemporaryDirectory() as tmp:
            ing = self._ingest(tmp, cap=5)
            ing._state["files"] = {f"/rec/{i:04d}.jpg": {"signature": [1, i],
                                                         "status": "processed",
                                                         "processed_at": 1000.0 + i}
                                   for i in range(30)}
            ing.path = Path(tmp) / "verschwunden"        # loest FileNotFoundError aus
            with self.assertRaises(FileNotFoundError):
                ing.scan_once(now=time.time())
            self.assertEqual(len(ing._state["files"]), 5,
                             "auch der gescheiterte Lauf muss aufraeumen")

    def test_attempts_is_already_incremented_before_processing_starts(self):
        """Gegen die Sorge, ein Absturz waehrend der Verarbeitung erzeuge eine Endlosschleife.

        Der Zaehler wird erhoeht und gespeichert, BEVOR _process laeuft — ein Absturz
        hinterlaesst also den bereits erhoehten Stand, und max_retries greift.
        """
        with tempfile.TemporaryDirectory() as tmp:
            write_image(Path(tmp) / "boom.jpg")
            ing = self._ingest(tmp)
            ing._process = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("kaputt"))
            now = time.time()
            ing.scan_once(now=now)
            ing.scan_once(now=now + 60)
            entry = ing._state["files"][str((Path(tmp) / "boom.jpg").resolve())]
            self.assertEqual(entry["attempts"], 1)
            self.assertEqual(entry["status"], "failed")

    def test_a_process_crash_cannot_loop_forever(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "boom.jpg"
            write_image(path)
            ing = self._ingest(tmp)
            ing.max_retries = 2
            ing.retry_seconds = 1.0
            ing._process = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("kaputt"))
            now = time.time()
            ing.scan_once(now=now)
            # Jeder Durchgang simuliert einen Neustart: Eintrag steht auf processing,
            # wird beim Laden zurueckgesetzt, naechster Versuch.
            for i in range(6):
                ing._state = ing._load_state() if i else ing._state
                ing.scan_once(now=now + 60 * (i + 1))
            entry = ing._state["files"][str(path.resolve())]
            self.assertLessEqual(entry["attempts"], ing.max_retries,
                                 "max_retries muss die Schleife brechen")

    def test_a_deferred_cap_is_applied_when_the_scan_finishes(self):
        with tempfile.TemporaryDirectory() as tmp:
            ing = self._ingest(tmp, cap=100)
            ing._state["files"] = {f"/rec/{i:04d}.jpg": {"signature": [1, i],
                                                         "status": "processed",
                                                         "processed_at": 1000.0 + i}
                                   for i in range(30)}
            ing._lock.acquire()
            try:
                self.assertFalse(ing.set_index_cap(10))
                self.assertEqual(ing.max_indexed_files, 100,
                                 "darf nicht am laufenden Scan vorbei geschrieben werden")
            finally:
                ing._lock.release()
            ing._finish_scan({"found": 0})
            self.assertEqual(ing.max_indexed_files, 10)
            self.assertEqual(len(ing._state["files"]), 10)


class IndexCapThirdRoundTests(unittest.TestCase):
    """Folgefehler der Fixes selbst — alle drei aus dem Review."""

    def _ingest(self, tmp, cap=None):
        fc = {"enabled": True, "path": tmp, "settle_seconds": 0, "extensions": [".jpg"]}
        if cap is not None:
            fc["max_indexed_files"] = cap
        return FolderIngest({"folder": fc, "faceid": {}},
                            Path(tmp), FakeEngine([[FakeFace([1, 0, 0])]]), FakeProcessor())

    def test_cleanup_failure_does_not_replace_the_scans_own_error(self):
        """Eine Ausnahme aus dem finally wuerde die aus dem Rumpf ersetzen."""
        with tempfile.TemporaryDirectory() as tmp:
            ing = self._ingest(tmp, cap=5)
            ing.path = Path(tmp) / "verschwunden"
            ing._save_state = lambda: (_ for _ in ()).throw(OSError("disk full"))
            with self.assertRaises(FileNotFoundError):     # nicht OSError
                ing.scan_once(now=time.time())

    def test_an_aborted_scan_is_marked_in_the_published_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            ing = self._ingest(tmp)
            ing.path = Path(tmp) / "verschwunden"
            with self.assertRaises(FileNotFoundError):
                ing.scan_once(now=time.time())
            self.assertTrue(ing._status["last_result"]["aborted"],
                            "ein abgebrochener Lauf darf nicht wie ein fertiger aussehen")
            self.assertTrue(ing.status()["last_error"])

    def test_a_completed_scan_is_not_marked_aborted(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_image(Path(tmp) / "a.jpg")
            ing = self._ingest(tmp)
            now = time.time()
            ing.scan_once(now=now)
            ing.scan_once(now=now + 60)
            self.assertNotIn("aborted", ing._status["last_result"])

    def test_a_cap_queued_during_cleanup_is_not_lost(self):
        """Getrenntes Lesen und Loeschen wuerde einen dazwischen eingereihten Wert verwerfen."""
        with tempfile.TemporaryDirectory() as tmp:
            ing = self._ingest(tmp, cap=100)
            ing._pending_cap = 50
            # Simuliert den HTTP-Thread, der GENAU zwischen Lesen und Loeschen schreibt.
            original = ing._enforce_index_cap
            def racing():
                ing._pending_cap = 7        # neuer Wunsch, waehrend wir aufraeumen
                return original()
            ing._enforce_index_cap = racing
            ing._finish_scan({"found": 0})
            self.assertEqual(ing.max_indexed_files, 50, "der gelesene Wert gilt")
            self.assertEqual(ing._pending_cap, 7,
                             "der neu eingereihte darf nicht verworfen werden")
            ing._enforce_index_cap = original
            ing._finish_scan({"found": 0})
            self.assertEqual(ing.max_indexed_files, 7, "und wird beim naechsten Ende angewandt")
