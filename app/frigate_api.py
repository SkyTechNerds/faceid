"""Minimaler Frigate-HTTP-Client: Snapshot-Crops holen, sub_label setzen.

Optional gegen Frigates authentifizierten Port (8971) statt der offenen API auf 5000:
dafuer ``user`` und ``password`` setzen. Achtung, gemessen an Frigate 0.17: ein
``viewer``-Konto darf lesen, aber KEIN sub_label schreiben ("Role viewer not authorized.
Required: admin") — wer die Namen zurueck nach Frigate schreiben will, braucht ein
Admin-Konto oder muss ``set_sub_label`` abschalten.
"""
import logging
import time

import cv2
import numpy as np
import requests

log = logging.getLogger("faceid.frigate")


class FrigateAPI:
    def __init__(self, base_url: str, timeout: float = 6.0, user: str | None = None,
                 password: str | None = None, verify_tls: bool = False,
                 go2rtc_url: str | None = None):
        self.base = base_url.rstrip("/")
        # go2rtc laeuft neben Frigate auf einem eigenen Port und ist nicht hinter dessen
        # API erreichbar (/api/go2rtc/... antwortet 404) — daher gleicher Host, Port 1984,
        # ueberschreibbar fuer Setups, die es woanders betreiben.
        if go2rtc_url:
            self.go2rtc = go2rtc_url.rstrip("/")
        else:
            from urllib.parse import urlparse
            u = urlparse(self.base)
            self.go2rtc = f"http://{u.hostname}:1984"
        self.timeout = timeout
        self.session = requests.Session()
        self.user = user or None
        self.password = password or ""
        # Frigate liefert im Standard ein selbstsigniertes Zertifikat; eine Pruefung
        # schlaegt dann immer fehl. Wer eine eigene CA hat, setzt verify_tls: true.
        self.session.verify = bool(verify_tls)
        self._logged_in_at = 0.0
        if self.user:
            self._login()

    # ---------- Anmeldung ----------

    def _login(self) -> bool:
        """Session-Cookie holen. Fehlschlag ist kein Beinbruch: gegen den offenen Port
        (5000) braucht es keine Anmeldung, dort laeuft danach alles wie bisher."""
        try:
            r = self.session.post(f"{self.base}/api/login",
                                  json={"user": self.user, "password": self.password},
                                  timeout=self.timeout)
        except requests.RequestException as e:
            log.warning("Frigate login failed (%s) — continuing unauthenticated", e)
            return False
        if r.status_code == 200:
            self._logged_in_at = time.time()
            log.info("Frigate login as '%s' succeeded", self.user)
            return True
        log.warning("Frigate login as '%s': HTTP %s — continuing unauthenticated",
                    self.user, r.status_code)
        return False

    def _request(self, method: str, url: str, **kw):
        """Aufruf mit einmaligem Neuanmelden, falls die Session abgelaufen ist."""
        kw.setdefault("timeout", self.timeout)
        r = self.session.request(method, url, **kw)
        if r.status_code in (401, 403) and self.user and time.time() - self._logged_in_at > 5:
            # 403 kann auch fehlende Rechte bedeuten (viewer + sub_label) — dann hilft
            # das erneute Anmelden nicht, kostet aber nur einen Versuch.
            if self._login():
                r = self.session.request(method, url, **kw)
        return r

    def snapshot(self, event_id: str, crop: bool = True) -> np.ndarray | None:
        """Aktuellen Person-Snapshot eines Events als BGR-Bild (crop=Person-Box)."""
        url = f"{self.base}/api/events/{event_id}/snapshot.jpg"
        try:
            r = self._request("GET", url, params={"crop": int(crop), "quality": 100})
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
            r = self._request("GET", url, timeout=self.timeout * 4)
            if r.status_code != 200 or not r.content:
                return None
            return cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_COLOR)
        except requests.RequestException as e:
            log.debug("recording frame %s@%s failed: %s", camera, ts, e)
            return None

    def live_frame(self, camera: str, timeout: float = 3.0) -> np.ndarray | None:
        """Aktueller Frame in voller Aufloesung — ueber go2rtc, nicht ueber Frigate.

        Warum nicht die Aufnahme: die gibt einen Zeitpunkt erst nach rund 45 s her
        (gemessen), taugt fuer eine Live-Erkennung also nicht. Frigates ``latest.jpg``
        wiederum kommt vom Detect-Stream und ist damit genauso klein wie der Snapshot.
        go2rtc liefert den Haupt-Stream und braucht dafuer ~1 s.

        go2rtc gehoert zu Frigate, laeuft aber auf einem eigenen Port (1984) und ist
        nicht in jedem Setup erreichbar — der Aufrufer muss damit rechnen, dass hier
        dauerhaft nichts kommt.
        """
        # Ein zweiter Versuch: der Abruf scheitert gelegentlich einzeln, waehrend die
        # Aufrufe davor und danach durchgehen (in zwei unabhaengigen Setups beobachtet).
        # go2rtc muss den Frame aus einem laufenden Stream greifen — kommt die Anfrage
        # zwischen zwei Keyframes, geht sie leer aus. Ein Ereignis deswegen zu verwerfen
        # waere schade, der zweite Anlauf kostet einen Sekundenbruchteil.
        for attempt in (1, 2):
            try:
                r = self.session.get(f"{self.go2rtc}/api/frame.jpeg",
                                     params={"src": camera}, timeout=timeout)
                if r.status_code == 200 and r.content:
                    return cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_COLOR)
                log.debug("go2rtc frame %s: HTTP %s (attempt %d)", camera, r.status_code, attempt)
            except requests.RequestException as e:
                log.debug("go2rtc frame %s failed: %s (attempt %d)", camera, e, attempt)
            if attempt == 1:
                time.sleep(0.4)
        return None

    def download_clip(self, event_id: str, dest: str, max_bytes: int = 80_000_000) -> bool:
        """Ereignis-Clip (volle Aufnahme-Auflösung) nach ``dest`` streamen.

        Nur fürs Enrollment: ein Download deckt das ganze Ereignis ab, statt einzelne
        Zeitpunkte zu raten. ``max_bytes`` bricht überlange Clips ab.
        """
        url = f"{self.base}/api/events/{event_id}/clip.mp4"
        try:
            with self._request("GET", url, timeout=self.timeout * 6, stream=True) as r:
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

    def config(self) -> dict | None:
        """Frigates Konfiguration (u. a. die Kameraliste)."""
        try:
            r = self._request("GET", f"{self.base}/api/config", timeout=8)
            return r.json() if r.status_code == 200 else None
        except (requests.RequestException, ValueError) as e:
            log.debug("config failed: %s", e)
            return None

    def events(self, **params) -> list:
        """Ereignisliste; Parameter wie in Frigates API (label, limit, after, before …)."""
        try:
            r = self._request("GET", f"{self.base}/api/events", params=params, timeout=15)
            return r.json() if r.status_code == 200 else []
        except (requests.RequestException, ValueError) as e:
            log.debug("events failed: %s", e)
            return []

    def set_sub_label(self, event_id: str, label: str, score: float):
        try:
            r = self._request(
                "POST", f"{self.base}/api/events/{event_id}/sub_label",
                json={"subLabel": label[:100], "subLabelScore": round(score, 3)},
            )
            if r.status_code == 403 and self.user:
                log.warning("sub_label %s rejected (403): the Frigate account '%s' lacks "
                            "admin rights. Use an admin account or set set_sub_label: false",
                            event_id, self.user)
            elif r.status_code not in (200, 202):
                log.warning("sub_label %s -> %s: HTTP %s %s", event_id, label, r.status_code, r.text[:200])
        except requests.RequestException as e:
            log.warning("sub_label %s failed: %s", event_id, e)


def frigate_client(cfg: dict, timeout: float = 6.0) -> FrigateAPI:
    """FrigateAPI aus der Konfiguration — mit Anmeldung, falls Zugangsdaten gesetzt sind."""
    f = cfg["frigate"]
    return FrigateAPI(f["url"], timeout=timeout, user=f.get("user"),
                      password=f.get("password"), verify_tls=bool(f.get("verify_tls", False)),
                      go2rtc_url=f.get("go2rtc_url"))
