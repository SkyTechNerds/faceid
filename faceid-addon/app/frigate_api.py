"""Minimaler Frigate-HTTP-Client: Snapshot-Crops holen, sub_label setzen."""
import logging

import cv2
import numpy as np
import requests

log = logging.getLogger("faceid.frigate")


class FrigateAPI:
    def __init__(self, base_url: str, timeout: float = 6.0):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    def snapshot(self, event_id: str, crop: bool = True) -> np.ndarray | None:
        """Aktuellen Person-Snapshot eines Events als BGR-Bild (crop=Person-Box)."""
        url = f"{self.base}/api/events/{event_id}/snapshot.jpg"
        try:
            r = self.session.get(url, params={"crop": int(crop), "quality": 100}, timeout=self.timeout)
            if r.status_code != 200 or not r.content:
                return None
            img = cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_COLOR)
            return img
        except requests.RequestException as e:
            log.warning("snapshot %s failed: %s", event_id, e)
            return None

    def recording_frame(self, camera: str, ts: float) -> np.ndarray | None:
        """Frame aus der AUFNAHME holen (volle Kamera-Auflösung statt Detect-Stream).
        Deutlich schärfere Gesichter, dafür langsamer — nur fürs Enrollment gedacht."""
        url = f"{self.base}/api/{camera}/recordings/{ts}/snapshot.jpg"
        try:
            r = self.session.get(url, timeout=self.timeout * 4)
            if r.status_code != 200 or not r.content:
                return None
            return cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_COLOR)
        except requests.RequestException as e:
            log.debug("recording frame %s@%s failed: %s", camera, ts, e)
            return None

    def download_clip(self, event_id: str, dest: str, max_bytes: int = 80_000_000) -> bool:
        """Ereignis-Clip (volle Aufnahme-Auflösung) nach ``dest`` streamen.

        Nur fürs Enrollment: ein Download deckt das ganze Ereignis ab, statt einzelne
        Zeitpunkte zu raten. ``max_bytes`` bricht überlange Clips ab.
        """
        url = f"{self.base}/api/events/{event_id}/clip.mp4"
        try:
            with self.session.get(url, timeout=self.timeout * 6, stream=True) as r:
                if r.status_code != 200:
                    return False
                written = 0
                with open(dest, "wb") as fh:
                    for chunk in r.iter_content(chunk_size=1 << 18):
                        if not chunk:
                            continue
                        written += len(chunk)
                        if written > max_bytes:
                            log.debug("clip %s aborted (> %d bytes)", event_id, max_bytes)
                            return False
                        fh.write(chunk)
                return written > 1000
        except (requests.RequestException, OSError) as e:
            log.debug("clip %s failed: %s", event_id, e)
            return False

    def set_sub_label(self, event_id: str, label: str, score: float):
        try:
            r = self.session.post(
                f"{self.base}/api/events/{event_id}/sub_label",
                json={"subLabel": label[:100], "subLabelScore": round(score, 3)},
                timeout=self.timeout,
            )
            if r.status_code not in (200, 202):
                log.warning("sub_label %s -> %s: HTTP %s %s", event_id, label, r.status_code, r.text[:200])
        except requests.RequestException as e:
            log.warning("sub_label %s failed: %s", event_id, e)
