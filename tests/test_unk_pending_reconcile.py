"""_unk_pending must not leak once a worker has moved a face off disk.

Regression guard: hide_unk_ui() adds ids, only unhide_unk_ui() (rollback) removes
them, so a finished assign/discard used to leave the id pending forever —
review_queue_len() then subtracted it a second time (count drifting low, clamped to 0
while faces still waited) and assign_pending_len() grew without bound.
"""
import tempfile
import threading
import unittest
from pathlib import Path

from app.gallery import Gallery


class UnkPendingReconcileTests(unittest.TestCase):
    def _gallery(self, tmp):
        g = Gallery.__new__(Gallery)  # skip model load; exercise the bookkeeping only
        g.unknown_dir = Path(tmp)
        g._unk_pending = set()
        g._unk_ui = None
        g._unk_ui_lock = threading.Lock()
        return g

    def test_finished_ids_leave_pending_and_the_count_stays_right(self):
        with tempfile.TemporaryDirectory() as tmp:
            g = self._gallery(tmp)
            for uid in ("a", "b", "c"):
                (Path(tmp) / f"{uid}.json").write_text("{}")

            g.hide_unk_ui(["a", "b"])            # two faces in flight
            self.assertEqual(g.assign_pending_len(), 2)
            self.assertEqual(g.review_queue_len(), 1)   # 3 on disk - 2 pending

            (Path(tmp) / "a.json").unlink()             # worker finished "a"
            self.assertEqual(g.assign_pending_len(), 1)  # "a" reconciled out, not stuck
            self.assertEqual(g.review_queue_len(), 1)    # 2 on disk - 1 pending, not 0

            (Path(tmp) / "b.json").unlink()             # worker finished "b"
            self.assertEqual(g.assign_pending_len(), 0)  # no unbounded growth
            self.assertEqual(g.review_queue_len(), 1)    # only "c" remains

    def test_in_flight_id_still_counts_as_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            g = self._gallery(tmp)
            (Path(tmp) / "x.json").write_text("{}")      # still on disk = worker not done
            g.hide_unk_ui(["x"])
            self.assertEqual(g.assign_pending_len(), 1)  # kept, not pruned
            self.assertEqual(g.review_queue_len(), 0)    # hidden from the queue


if __name__ == "__main__":
    unittest.main()
