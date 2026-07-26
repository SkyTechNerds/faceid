"""Frigate-Events per MQTT verarbeiten und Ergebnisse für HA publizieren.

Pipeline: frigate/events (person) -> Snapshot-Crop -> ArcFace -> Galerie-Match
  - Match  >= match_threshold   -> Person publizieren + Frigate sub_label
  - Match  <  unknown_threshold -> als Unbekannter in die Review-Queue
  - dazwischen                  -> unsicher; nur Review-Queue, keine Meldung
"""
import json
import logging
import queue
import threading
import time
from collections import deque

import paho.mqtt.client as mqtt
import requests

from .engine import FaceEngine, crop_face
from .hires import upgrade_face

log = logging.getLogger("faceid.mqtt")


class EventProcessor:
    def __init__(self, cfg: dict, engine, gallery, frigate):
        self.cfg = cfg
        self.engine = engine
        self.gallery = gallery
        self.frigate = frigate
        self.queue: "queue.Queue[dict]" = queue.Queue(maxsize=200)
        self.events: dict[str, dict] = {}  # event_id -> Zustand
        self.recent = deque(maxlen=100)  # Ringpuffer für die UI
        self.client: mqtt.Client | None = None
        f = cfg["faceid"]
        self.match_thr = float(f.get("match_threshold", 0.5))
        self.unknown_thr = float(f.get("unknown_threshold", 0.35))
        self.min_face_px = int(f.get("min_face_px", 48))
        self.max_attempts = int(f.get("max_attempts", 6))
        self.retry_secs = float(f.get("retry_seconds", 2.5))
        self.cameras = set(f.get("cameras") or [])
        self.set_sub_label = bool(f.get("set_sub_label", True))
        self.presence_window = float(f.get("presence_window", 120))
        self.ignore_thr = float(f.get("ignore_threshold", f.get("match_threshold", 0.5)))
        self.ignore_learning = bool(f.get("ignore_learning", True))
        self.hires_enroll = bool(f.get("hires_enroll", True))
        # Ereignisse, die Frigate nicht per MQTT meldet (z. B. per API angelegte
        # Kamera-Meldungen als Zuverlaessigkeits-Bruecke), per Abfrage nachziehen.
        self.poll_interval = float(f.get("poll_interval", 0))
        # Frigate darf sein MQTT-Topic umbenennen (topic_prefix in dessen config.yml).
        self.frigate_topic = str(f.get("frigate_topic_prefix", "frigate")).strip("/") or "frigate"
        self._polled: deque = deque(maxlen=500)   # schon gesehene IDs
        self._announced: set = set()              # Kameras mit angemeldetem Sensor
        # Der Finalizer raeumt self.events nach der Verarbeitung ab — ohne dieses
        # Gedaechtnis haelt der Poller ein fertig verarbeitetes Ereignis fuer neu.
        self._handled: deque = deque(maxlen=1000)
        self.prefix = str(f.get("mqtt_prefix", "faceid")).strip("/") or "faceid"
        self.present: dict[str, dict[str, float]] = {}  # camera -> {person: zuletzt gesehen}
        self._last_presence: dict[str, list] = {}  # zuletzt publizierter Stand je Kamera

    # ---------- MQTT ----------

    def start(self):
        m = self.cfg["mqtt"]
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.prefix)
        if m.get("user"):
            c.username_pw_set(m["user"], m.get("password", ""))
        c.will_set(f"{self.prefix}/status", "offline", retain=True)
        c.on_connect = self._on_connect
        c.on_message = self._on_message
        c.connect(m["host"], int(m.get("port", 1883)), keepalive=60)
        c.loop_start()
        self.client = c
        self._check_frigate()
        threading.Thread(target=self._worker, daemon=True, name="faceid-worker").start()
        threading.Thread(target=self._finalizer, daemon=True, name="faceid-finalizer").start()
        if self.poll_interval > 0:
            threading.Thread(target=self._poller, daemon=True, name="faceid-poller").start()

    def _check_frigate(self):
        """Beim Start einmal nachsehen, ob Frigate ueberhaupt antwortet.

        Ohne diese Zeile im Log ist "es erkennt nichts" kaum von "es kommt nichts an"
        zu unterscheiden."""
        url = self.cfg["frigate"]["url"].rstrip("/")
        try:
            r = requests.get(f"{url}/api/config", timeout=8)
            if r.status_code != 200:
                log.error("Frigate unter %s antwortet mit HTTP %s — ohne Snapshots kann "
                          "nicht erkannt werden", url, r.status_code)
                return
            cams = list((r.json().get("cameras") or {}).keys())
            log.info("Frigate erreichbar (%s), Kameras: %s", url, ", ".join(cams) or "keine")
            if self.cameras:
                unknown = self.cameras - set(cams)
                if unknown:
                    log.warning("Konfigurierte Kamera(s) %s gibt es in Frigate nicht — "
                                "von diesen wird nie etwas verarbeitet", ", ".join(sorted(unknown)))
        except (requests.RequestException, ValueError) as e:
            log.error("Frigate unter %s nicht erreichbar: %s — Snapshots und damit die "
                      "Erkennung werden fehlschlagen", url, e)

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        log.info("MQTT verbunden (%s), abonniere %s/events", reason_code, self.frigate_topic)
        client.subscribe(f"{self.frigate_topic}/events")
        client.publish(f"{self.prefix}/status", "online", retain=True)
        self._publish_discovery()

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload)
        except json.JSONDecodeError:
            return
        after = payload.get("after") or {}
        etype = payload.get("type")
        if after.get("label") != "person":
            return
        cam = after.get("camera", "")
        if self.cameras and cam not in self.cameras:
            return
        eid = after.get("id")
        if not eid:
            return
        if eid not in self._handled:
            self._handled.append(eid)
        self._ensure_discovery(cam)
        st = self.events.setdefault(
            eid,
            {"camera": cam, "attempts": 0, "best_score": 0.0, "best_person": None,
             "best_unknown": None, "last_try": 0.0, "done": False, "ended": False,
             "created": time.time(),
             "start_time": after.get("start_time") or time.time(), "end_time": None},
        )
        if etype == "end":
            st["ended"] = True
            st["end_time"] = after.get("end_time") or time.time()
        if st["done"] or st["attempts"] >= self.max_attempts:
            return
        if after.get("has_snapshot") and time.time() - st["last_try"] >= self.retry_secs:
            st["last_try"] = time.time()
            try:
                self.queue.put_nowait({"eid": eid})
            except queue.Full:
                log.warning("Queue voll, Event %s übersprungen", eid)

    # ---------- Verarbeitung ----------

    def _worker(self):
        while True:
            item = self.queue.get()
            try:
                self._process(item["eid"])
            except Exception:
                log.exception("Fehler bei Event %s", item["eid"])

    def _process(self, eid: str):
        st = self.events.get(eid)
        if st is None or st["done"]:
            return
        st["attempts"] += 1
        img = self.frigate.snapshot(eid, crop=True)
        if img is None:
            log.info("Event %s (%s): kein Snapshot von Frigate", eid, st["camera"])
            return
        found = self.engine.faces(img)
        face = FaceEngine.best_face(found, min_px=self.min_face_px)
        if face is None:
            # Haeufigster Normalfall (Ruecken zur Kamera, zu weit weg) — trotzdem
            # protokollieren, sonst sieht ein stiller Log wie ein Defekt aus. Die
            # Unterscheidung "zu klein" vs. "gar keins" entscheidet, wo man sucht:
            # zu klein deutet auf Frigates snapshots.height oder Kameraabstand,
            # gar keins eher auf Blickwinkel oder Licht.
            h, w = img.shape[:2]
            if found:
                big = max(int(f.bbox[2] - f.bbox[0]) for f in found)
                log.info("Event %s (%s): Versuch %d, groesstes Gesicht %dpx < min_face_px "
                         "%d (Snapshot %dx%d)", eid, st["camera"], st["attempts"], big,
                         self.min_face_px, w, h)
            else:
                log.info("Event %s (%s): Versuch %d, kein Gesicht im Snapshot %dx%d",
                         eid, st["camera"], st["attempts"], w, h)
            return
        emb = face.normed_embedding
        slug, name, score = self.gallery.match(emb)
        ig = self.gallery.match_ignored(emb)
        if ig >= self.ignore_thr and ig >= score:
            # Gesicht steht auf der Ignore-Liste: nicht melden, nicht taggen, nicht vorlegen
            st["best_unknown"] = None
            st["done"] = True
            # Anker-Lernen nur bei eindeutigen Fällen: klarer Ignore-Match UND deutlicher
            # Abstand zum besten Personen-Match — so wird nie ein Familienmitglied still
            # zum Negativ-Anker. Nur neue Erscheinungsformen werden gespeichert.
            if self.ignore_learning and ig >= self.ignore_thr + 0.1 and ig - score >= 0.1:
                iid = self.gallery.add_ignore_anchor(crop_face(img, face.bbox), emb)
                if iid:
                    log.info("Event %s: neuer Auto-Ignore-Anker %s (sim %.3f)", eid, iid, ig)
            log.info("Event %s (%s): ignoriertes Gesicht (sim %.3f)", eid, st["camera"], ig)
            return
        crop = crop_face(img, face.bbox)
        log.info("Event %s (%s): Versuch %d, Match %s (%.3f)", eid, st["camera"], st["attempts"], name, score)

        if slug and score >= self.match_thr:
            if score > st["best_score"]:
                st["best_score"], st["best_person"] = score, name
                self._publish_recognition(eid, st, name, score)
                if self.set_sub_label:
                    self.frigate.set_sub_label(eid, name, score)
            if score >= self.match_thr + 0.1:
                st["done"] = True  # sehr sicherer Treffer -> keine weiteren Versuche
        else:
            # bestes unsicheres/unbekanntes Gesicht des Events merken, Ablage erst beim Event-Ende
            prev = st.get("best_unknown")
            if prev is None or face.det_score > prev["det_score"]:
                st["best_unknown"] = {"crop": crop, "emb": emb, "det_score": float(face.det_score),
                                      "guess": name, "guess_score": float(score), "full": img}

    def _poller(self):
        """Frigate-Ereignisse abfragen, die per MQTT nie ankommen.

        Manuell ueber die API angelegte Ereignisse sind fuer Frigate keine getrackten
        Objekte und loesen ``frigate/events`` nicht aus — eine Kamera-eigene
        Personenmeldung, die als Bruecke ein Ereignis anlegt, bliebe sonst ungenutzt.
        Sie haben keine Bounding-Box, der Snapshot ist also das Vollbild; die Erkennung
        laeuft ansonsten durch dieselbe Pipeline.
        """
        url = self.cfg["frigate"]["url"].rstrip("/")
        # Beim Start nicht die halbe Historie aufrollen.
        since = time.time() - min(self.poll_interval * 4, 300)
        while True:
            time.sleep(self.poll_interval)
            try:
                r = requests.get(f"{url}/api/events",
                                 params={"label": "person", "has_snapshot": 1,
                                         "limit": 50, "after": since - 30},
                                 timeout=10)
                if r.status_code != 200:
                    continue
                batch = r.json()
            except (requests.RequestException, ValueError) as e:
                log.debug("Poll fehlgeschlagen: %s", e)
                continue
            since = time.time()
            for ev in batch:
                eid = ev.get("id")
                if (not eid or eid in self._polled or eid in self.events
                        or eid in self._handled):
                    continue
                cam = ev.get("camera", "")
                if self.cameras and cam not in self.cameras:
                    continue
                self._polled.append(eid)
                self._ensure_discovery(cam)
                # Nur abgeschlossene Ereignisse — laufende meldet MQTT ohnehin.
                if not ev.get("end_time"):
                    continue
                self.events[eid] = {
                    "camera": cam, "attempts": 0, "best_score": 0.0, "best_person": None,
                    "best_unknown": None, "last_try": 0.0, "done": False, "ended": True,
                    "created": time.time(), "polled": True,
                    "start_time": ev.get("start_time") or time.time(),
                    "end_time": ev.get("end_time"),
                }
                log.info("Poll: Ereignis %s (%s) nachgezogen — von MQTT nie gemeldet",
                         eid, cam)
                try:
                    self.queue.put_nowait({"eid": eid})
                except queue.Full:
                    log.warning("Queue voll — Poll-Ereignis %s verworfen", eid)

    def _finalizer(self):
        """Beendete Events abschließen: Unknown ablegen, 'unbekannt' melden, aufräumen."""
        while True:
            time.sleep(5)
            now = time.time()
            for cam in list(self.present.keys()):
                self._publish_presence(cam)  # abgelaufene Personen austragen -> ggf. 'niemand'
            for eid in list(self.events.keys()):
                st = self.events[eid]
                expired = now - st["created"] > 600
                if not (st["ended"] or expired):
                    continue
                if now - st["last_try"] < self.retry_secs + 1 and not expired:
                    continue  # letzter Versuch evtl. noch in der Queue
                if st["best_person"] is None and st["best_unknown"] is not None:
                    u = st["best_unknown"]
                    crop, emb, full = u["crop"], u["emb"], u.get("full")
                    if self.hires_enroll:
                        # schärferes Gesicht aus der Aufnahme holen (bessere Referenz)
                        try:
                            hi = upgrade_face(self.engine, self.frigate, st["camera"],
                                              st.get("start_time"), st.get("end_time"), emb,
                                              event_id=eid)
                        except Exception:
                            hi = None
                        if hi is not None:
                            face, img = hi
                            old_w = int(u["crop"].shape[1])
                            new_w = int(face.bbox[2] - face.bbox[0])
                            if new_w > old_w:
                                crop, emb, full = crop_face(img, face.bbox), face.normed_embedding, img
                                log.info("Event %s: hi-res Referenz aus Aufnahme (%dpx statt %dpx)",
                                         eid, new_w, old_w)
                    uid = self.gallery.save_unknown(
                        crop, emb,
                        {"camera": st["camera"], "event_id": eid,
                         "event_ts": st.get("start_time"),
                         "guess": u["guess"], "guess_score": round(u["guess_score"], 3)},
                        full_bgr=full,
                    )
                    self._publish_recognition(eid, st, "unknown", u["guess_score"])
                    log.info("Event %s: unbekanntes Gesicht abgelegt (%s)", eid, uid)
                self.events.pop(eid, None)

    # ---------- Publish ----------

    def _publish_recognition(self, eid: str, st: dict, name: str, score: float):
        payload = {
            "person": name, "score": round(float(score), 3), "camera": st["camera"],
            "event_id": eid, "ts": time.time(),
        }
        self.recent.appendleft(payload)
        # faceid/event genau einmal pro (Event, Person) — Score-Verbesserungen lösen keine
        # erneute Meldung aus (sonst mehrere Notifications für dieselbe Sichtung)
        if self.client and st.get("announced") != name:
            st["announced"] = name
            self.client.publish(f"{self.prefix}/event", json.dumps(payload, ensure_ascii=False))
        self.present.setdefault(st["camera"], {})[name] = time.time()
        self._publish_presence(st["camera"], last=payload)

    def _publish_presence(self, cam: str, last: dict | None = None):
        """Sensor-State = alle im Fenster gesehenen Personen ('Christian, Juli' / 'niemand')."""
        now = time.time()
        pres = self.present.setdefault(cam, {})
        for n, ts in list(pres.items()):
            if now - ts > self.presence_window:
                pres.pop(n)
        names = [n for n, _ in sorted(pres.items(), key=lambda kv: -kv[1])]
        if names == self._last_presence.get(cam) and last is None:
            return  # nichts geändert -> retained Topic nicht neu beschreiben
        self._last_presence[cam] = names
        if self.client:
            attrs = {"persons": names, "window_s": self.presence_window, "ts": now}
            if last:
                attrs["last"] = last
            self.client.publish(f"{self.prefix}/{cam}/person", ", ".join(names) or "nobody", retain=True)
            self.client.publish(f"{self.prefix}/{cam}/attributes", json.dumps(attrs, ensure_ascii=False), retain=True)

    def _frigate_cameras(self) -> set:
        """Kameranamen von Frigate holen — fuer den Fall, dass keine konfiguriert sind."""
        try:
            r = requests.get(f"{self.cfg['frigate']['url'].rstrip('/')}/api/config", timeout=8)
            if r.status_code == 200:
                return set((r.json().get("cameras") or {}).keys())
        except (requests.RequestException, ValueError) as e:
            log.warning("Kameraliste von Frigate nicht abrufbar (%s) — Sensoren entstehen "
                        "dann erst, sobald die erste Person erkannt wird", e)
        return set()

    def _ensure_discovery(self, cam: str):
        """Sensor fuer eine Kamera anlegen, falls noch nicht geschehen."""
        if not cam or cam in self._announced:
            return
        self._announced.add(cam)
        self._publish_discovery([cam])

    def _publish_discovery(self, only: list | None = None):
        """HA MQTT-Discovery: ein Sensor je Kamera (zuletzt erkannte Person).

        Eine leere ``cameras``-Liste bedeutet "alle Kameras verarbeiten" — frueher
        entstanden dann gar keine Sensoren, weil hier ueber eine leere Menge gelaufen
        wurde. Ohne Konfiguration fragen wir deshalb Frigate; klappt auch das nicht,
        legt ``_ensure_discovery`` den Sensor an, sobald die Kamera das erste Mal
        auftaucht."""
        if only is not None:
            cams = set(only)
        else:
            cams = (self.cameras
                    or set(self.cfg["faceid"].get("discovery_cameras") or [])
                    or self._frigate_cameras())
            self._announced |= cams
            log.info("MQTT-Discovery: %d Sensor(en) angemeldet%s", len(cams),
                     "" if cams else " — Kameras unbekannt, folgen bei der ersten Erkennung")
        device = {"identifiers": [self.prefix], "name": self.prefix.replace("-", " ").title() if self.prefix != "faceid" else "FaceID",
                  "manufacturer": "Eigenbau", "model": "InsightFace/ArcFace"}
        for cam in cams:
            conf = {
                "name": cam,  # HA stellt den Gerätenamen "FaceID" voran
                "unique_id": f"{self.prefix}_{cam}",
                "object_id": f"{self.prefix}_{cam}",
                "state_topic": f"{self.prefix}/{cam}/person",
                "json_attributes_topic": f"{self.prefix}/{cam}/attributes",
                "availability_topic": f"{self.prefix}/status",
                "icon": "mdi:face-recognition",
                "device": device,
            }
            self.client.publish(f"homeassistant/sensor/{self.prefix}_{cam}/config",
                                json.dumps(conf, ensure_ascii=False), retain=True)
            # frischen Anwesenheits-Stand publizieren (räumt auch stale retained States nach Neustart auf)
            self._last_presence.pop(cam, None)
            self.present.setdefault(cam, {})
            self._publish_presence(cam)
