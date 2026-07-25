"""Schärfere Referenzbilder: Gesicht aus dem Aufnahme-Frame statt aus dem Detect-Snapshot.

Frigate erkennt auf einem heruntergerechneten Stream (z. B. 1280x960), zeichnet aber in
voller Kameraauflösung auf (z. B. 2560x1920). Für die Galerie lohnt sich deshalb der
Umweg über die Aufnahme: dort sind Gesichter typischerweise doppelt so groß.

Bewusst nur fürs Enrollment (Review-Queue, Verlaufs-Scan) — die Live-Erkennung bleibt
schnell auf dem Snapshot.
"""
import logging

import numpy as np

from .engine import FaceEngine

log = logging.getLogger("faceid.hires")


def upgrade_face(engine, frigate, camera: str, start_time: float, end_time: float,
                 ref_embedding, attempts: int = 3, min_px: int = 60,
                 identity_min: float = 0.5):
    """Sucht in Aufnahme-Frames ein größeres Gesicht DERSELBEN Person.

    ``ref_embedding`` ist das Gesicht aus dem Snapshot — jeder Kandidat muss dazu passen
    (Cosine >= ``identity_min``), sonst würde bei mehreren Personen im Bild das falsche
    Gesicht eingelernt.

    Rückgabe: ``(face, image)`` des größten passenden Treffers oder ``None``.
    """
    if not camera or not start_time:
        return None
    span = max(0.0, (end_time or start_time) - start_time)
    # Zeitpunkte über das Ereignis verteilen; Frigate wählt für den Snapshot den besten
    # Moment, den kennen wir nicht — also mehrere Stellen abklopfen.
    if span <= 2:
        offsets = [1.0, 0.5, 2.0]
    else:
        offsets = [span * 0.25, span * 0.5, span * 0.75, 1.0]
    best = None
    for off in offsets[:max(1, attempts)]:
        img = frigate.recording_frame(camera, round(start_time + off, 3))
        if img is None:
            continue
        face = FaceEngine.best_face(engine.faces(img), min_px=min_px, min_det=0.55)
        if face is None:
            continue
        if ref_embedding is not None:
            sim = float(face.normed_embedding @ ref_embedding)
            if sim < identity_min:
                log.debug("hires: Treffer verworfen (andere Person, sim %.2f)", sim)
                continue
        width = float(face.bbox[2] - face.bbox[0])
        if best is None or width > best[0]:
            best = (width, face, img)
    if best is None:
        return None
    return best[1], best[2]
