"""InsightFace-Wrapper: SCRFD-Detection + ArcFace-Embeddings (buffalo_l — dieselben Modelle wie Immich)."""
import threading

import numpy as np


class FaceEngine:
    def __init__(self, det_size: int = 640, providers=None):
        from insightface.app import FaceAnalysis

        self.app = FaceAnalysis(
            name="buffalo_l",
            providers=providers or ["CPUExecutionProvider"],
            allowed_modules=["detection", "recognition"],
        )
        self.app.prepare(ctx_id=0, det_size=(det_size, det_size))
        # onnxruntime-Sessions sind nicht garantiert threadsafe bei parallelem run()
        self._lock = threading.Lock()

    def faces(self, bgr: np.ndarray):
        with self._lock:
            return self.app.get(bgr)

    @staticmethod
    def best_face(faces, min_px: int = 48, min_det: float = 0.55):
        """Größtes Gesicht, das Mindestgröße und Detection-Score erfüllt."""
        best, best_area = None, 0
        for f in faces:
            w = f.bbox[2] - f.bbox[0]
            h = f.bbox[3] - f.bbox[1]
            if w < min_px or h < min_px or f.det_score < min_det:
                continue
            if w * h > best_area:
                best, best_area = f, w * h
        return best


def crop_face(bgr: np.ndarray, bbox, margin: float = 0.35) -> np.ndarray:
    x1, y1, x2, y2 = [int(v) for v in bbox]
    mx, my = int((x2 - x1) * margin), int((y2 - y1) * margin)
    h, w = bgr.shape[:2]
    return bgr[max(0, y1 - my) : min(h, y2 + my), max(0, x1 - mx) : min(w, x2 + mx)]
