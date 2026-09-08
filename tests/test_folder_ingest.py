import json
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from app.folder_ingest import FolderIngest, MediaReadError, scan_media


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
