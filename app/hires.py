"""Schärfere Referenzbilder: Gesicht aus der Aufnahme statt aus dem Detect-Snapshot.

Frigate erkennt auf einem heruntergerechneten Stream (z. B. 1280x960), zeichnet aber in
voller Kameraauflösung auf (z. B. 2560x1920). Für die Galerie lohnt sich deshalb der
Umweg über die Aufnahme: dort sind Gesichter typischerweise doppelt so groß.

Die Live-Erkennung arbeitet zuerst auf dem Snapshot — der ist sofort da und kostet
nichts. Erst wenn der gar kein Gesicht hergibt, lohnt der Blick in die Aufnahme
(``find_face_in_clip``): gemessen an sieben Tagen echter Ereignisse hatten nur 21 %
der Snapshots ein verwertbares Gesicht, der Clip lieferte in 9 von 12 Fällen doch
noch eines. Frigate wählt seinen Snapshot nach dem höchsten Personen-Score aus, und
das ist ein anderes Kriterium als "Gesicht sichtbar".
"""
import logging
import os
import tempfile

import cv2
import numpy as np

from .engine import FaceEngine

log = logging.getLogger("faceid.hires")


def _pick(engine, frame, ref_embedding, min_px, identity_min, min_det=0.55):
    """Aus allen Gesichtern eines Frames das passendste zurückgeben.

    Wichtig bei mehreren Personen im Bild: das GRÖSSTE Gesicht ist nicht zwingend das
    gesuchte. Ohne diesen Vergleich würde der Frame komplett verworfen, sobald jemand
    anderes näher an der Kamera steht.
    """
    best = None
    for f in engine.faces(frame):
        w = float(f.bbox[2] - f.bbox[0])
        if w < min_px or float(f.det_score) < min_det:
            continue
        sim = 1.0 if ref_embedding is None else float(f.normed_embedding @ ref_embedding)
        if sim < identity_min:
            continue
        if best is None or sim > best[0]:
            best = (sim, w, f)
    return best


def _clip_frames(frigate, event_id, max_frames):
    """Gleichmäßig über den Clip verteilte Frames — der Generator raeumt selbst auf.

    Wird bewusst immer vollstaendig durchlaufen (kein ``break`` beim Aufrufer), damit
    das ``finally`` die heruntergeladene Datei sicher wieder loescht.
    """
    fd, path = tempfile.mkstemp(suffix=".mp4", prefix="faceid-clip-")
    os.close(fd)
    try:
        if not frigate.download_clip(event_id, path):
            return
        cap = cv2.VideoCapture(path)
        try:
            total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            if total <= 0:
                return
            # Frigate waehlt fuer den Snapshot den besten Moment nach seinem Kriterium,
            # den kennen wir nicht — also gleichmaessig abtasten.
            for i in np.linspace(0, total - 1, min(max_frames, total)).astype(int):
                cap.set(cv2.CAP_PROP_POS_FRAMES, int(i))
                ok, frame = cap.read()
                if ok and frame is not None:
                    yield frame
        finally:
            cap.release()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _scan_clip(engine, frigate, event_id, ref_embedding, max_frames, min_px, identity_min,
               min_det):
    best = None
    for frame in _clip_frames(frigate, event_id, max_frames):
        cand = _pick(engine, frame, ref_embedding, min_px, identity_min, min_det)
        if cand is None:
            continue
        if best is None or cand[1] > best[0][1]:
            best = (cand, frame)
    return (best[0][2], best[1]) if best else None


def find_face_in_clip(engine, frigate, event_id: str, max_frames: int = 12,
                      min_px: int = 48, min_det: float = 0.65):
    """Bestes Gesicht im Clip — fuer Ereignisse, deren Snapshot gar keines hergab.

    Ausgewaehlt wird nach det_score, ausdruecklich NICHT nach Galerie-Aehnlichkeit:
    sonst sucht man sich aus zwoelf Frames denjenigen heraus, der zufaellig am ehesten
    wie jemand Bekanntes aussieht, und rechnet sich die Erkennung schoen. Kriterium
    bleibt die Bildqualitaet, die Zuordnung kommt danach — genau wie beim Snapshot.

    -> (face, frame) oder None
    """
    best = None
    for frame in _clip_frames(frigate, event_id, max_frames):
        for f in engine.faces(frame):
            w = float(f.bbox[2] - f.bbox[0])
            h = float(f.bbox[3] - f.bbox[1])
            if w < min_px or h < min_px or float(f.det_score) < min_det:
                continue
            key = (float(f.det_score), w)
            if best is None or key > best[0]:
                best = (key, f, frame)
    return (best[1], best[2]) if best else None


def _scan_recordings(engine, frigate, camera, start_time, end_time, ref_embedding,
                     attempts, min_px, identity_min, min_det):
    """Fallback ohne Clip: einzelne Aufnahme-Frames per HTTP abklopfen."""
    span = max(0.0, (end_time or start_time) - start_time)
    offsets = [1.0, 0.5, 2.0] if span <= 2 else [span * 0.25, span * 0.5, span * 0.75, 1.0]
    best = None
    for off in offsets[:max(1, attempts)]:
        img = frigate.recording_frame(camera, round(start_time + off, 3))
        if img is None:
            continue
        cand = _pick(engine, img, ref_embedding, min_px, identity_min, min_det)
        if cand is None:
            continue
        if best is None or cand[1] > best[0][1]:
            best = (cand, img)
    return (best[0][2], best[1]) if best else None


def upgrade_face(engine, frigate, camera: str, start_time: float, end_time: float,
                 ref_embedding, event_id: str | None = None, max_frames: int = 12,
                 attempts: int = 3, min_px: int = 60, identity_min: float = 0.5,
                 min_det: float = 0.55):
    """Sucht in der Aufnahme ein größeres Gesicht DERSELBEN Person.

    ``ref_embedding`` ist das Gesicht aus dem Snapshot — jeder Kandidat muss dazu passen
    (Cosine >= ``identity_min``), sonst würde bei mehreren Personen im Bild das falsche
    Gesicht eingelernt.

    Rückgabe: ``(face, image)`` des größten passenden Treffers oder ``None``.
    """
    if event_id:
        hit = _scan_clip(engine, frigate, event_id, ref_embedding, max_frames, min_px,
                         identity_min, min_det)
        if hit is not None:
            return hit
    if not camera or not start_time:
        return None
    return _scan_recordings(engine, frigate, camera, start_time, end_time, ref_embedding,
                            attempts, min_px, identity_min, min_det)
