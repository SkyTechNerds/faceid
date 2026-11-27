"""refresh_guesses must persist the cues it already wrote, even if a later file
raises mid-pass.

Regression guard: the post-loop bookkeeping (_fsync_unknown_dir, invalidate_unk_ui,
epoch bump) used to sit after the for-loop, so an OSError on the second unknown
skipped it entirely — the first unknown's freshly written guess stayed invisible
(stale epoch, no UI invalidation) until the next mutation happened to commit.
"""
import json
import tempfile
import threading
import unittest
from pathlib import Path

from app.gallery import Gallery


class RefreshCommitOnErrorTests(unittest.TestCase):
    def _gallery(self, tmp):
        g = Gallery.__new__(Gallery)  # skip model load; exercise the write path only
        g.unknown_dir = Path(tmp)
        g._refresh_sched_lock = threading.Lock()
        g._guesses_epoch = 0
        g._guesses_updated_ts = 0.0
        g.invalidated = 0
        g.fsynced = 0
        g.invalidate_unk_ui = lambda: setattr(g, "invalidated", g.invalidated + 1)
        g._fsync_unknown_dir = lambda: setattr(g, "fsynced", g.fsynced + 1)
        return g

    def test_bookkeeping_runs_when_a_later_file_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            g = self._gallery(tmp)
            for uid in ("a", "b"):
                (Path(tmp) / f"{uid}.json").write_text(
                    json.dumps({"embedding": [0.0]}))

            calls = {"n": 0}

            def fake_match(_emb):
                calls["n"] += 1
                if calls["n"] == 1:
                    return ("ann", "Ann", 0.9, "ann/1.jpg")  # first file: real change
                raise OSError("disk fell over")              # second file: blow up

            g.match = fake_match

            with self.assertRaises(OSError):
                g._refresh_guesses_once()

            # finally ran despite the raise: first file's cue is committed + visible.
            self.assertEqual(g._guesses_epoch, 1)
            self.assertEqual(g.invalidated, 1)
            self.assertEqual(g.fsynced, 1)
            # and the first file really was rewritten with the new guess.
            files = sorted(Path(tmp).glob("*.json"))
            wrote = [f for f in files
                     if json.loads(f.read_text()).get("guess") == "Ann"]
            self.assertEqual(len(wrote), 1)


if __name__ == "__main__":
    unittest.main()
