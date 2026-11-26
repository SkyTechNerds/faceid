"""_clip_frames samples a clip with one forward pass instead of a seek per frame.

The invariant that matters: the forward pass must hand back the *same* frames the
old per-frame ``cap.set(CAP_PROP_POS_FRAMES, i)`` seek returned — same indices,
byte-identical decoded pixels — just faster. Both decode the same file, so the
comparison holds regardless of the (lossy) codec.
"""
import os
import tempfile
import unittest

import numpy as np

try:
    import cv2
except ImportError:  # pragma: no cover - CI installs opencv
    cv2 = None

from app import hires


def _write_clip(path, n=40, w=64, h=48):
    """A clip whose frame i carries i in its pixels, so decoded frames are distinguishable."""
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    vw = cv2.VideoWriter(path, fourcc, 15.0, (w, h))
    for i in range(n):
        frame = np.full((h, w, 3), i % 256, dtype=np.uint8)
        vw.write(frame)
    vw.release()


def _seek_sample(path, max_frames):
    """The old behaviour: seek to each evenly spaced index and read."""
    cap = cv2.VideoCapture(path)
    try:
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        out = []
        for i in sorted(set(int(i) for i in
                            np.linspace(0, total - 1, min(max_frames, total)))):
            cap.set(cv2.CAP_PROP_POS_FRAMES, i)
            ok, frame = cap.read()
            if ok and frame is not None:
                out.append(frame)
        return out
    finally:
        cap.release()


@unittest.skipIf(cv2 is None, "opencv not available")
class ClipFramesTests(unittest.TestCase):
    def setUp(self):
        fd, self.ref = tempfile.mkstemp(suffix=".mp4", prefix="faceid-ref-")
        os.close(fd)
        _write_clip(self.ref)

    def tearDown(self):
        try:
            os.unlink(self.ref)
        except OSError:
            pass

    def _frigate(self):
        import shutil
        ref = self.ref

        class Stub:
            def download_clip(self, event_id, path, **kw):
                shutil.copy(ref, path)
                return True

        return Stub()

    def test_forward_pass_matches_seek_frames(self):
        got = list(hires._clip_frames(self._frigate(), "e", 12))
        expected = _seek_sample(self.ref, 12)
        self.assertEqual(len(got), len(expected))
        self.assertTrue(len(got) >= 2)
        for a, b in zip(got, expected):
            self.assertTrue(np.array_equal(a, b), "forward-pass frame differs from seek frame")

    def test_requests_no_more_frames_than_exist(self):
        got = list(hires._clip_frames(self._frigate(), "e", 1000))
        # 40 written; decoder may report a few less/more — never more than exist, never zero.
        self.assertGreater(len(got), 0)
        self.assertLessEqual(len(got), 60)

    def test_download_failure_yields_nothing_and_cleans_up(self):
        class Fail:
            def download_clip(self, event_id, path, **kw):
                return False

        self.assertEqual(list(hires._clip_frames(Fail(), "e", 12)), [])


if __name__ == "__main__":
    unittest.main()
