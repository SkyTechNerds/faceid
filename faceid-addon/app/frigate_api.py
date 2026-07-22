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
            log.warning("Snapshot %s fehlgeschlagen: %s", event_id, e)
            return None

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
            log.warning("sub_label %s fehlgeschlagen: %s", event_id, e)
