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

from .engine import FaceEngine, crop_face

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
        self.present: dict[str, dict[str, float]] = {}  # camera -> {person: zuletzt gesehen}
        self._last_presence: dict[str, list] = {}  # zuletzt publizierter Stand je Kamera

    # ---------- MQTT ----------

    def start(self):
        m = self.cfg["mqtt"]
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="faceid")
        if m.get("user"):
            c.username_pw_set(m["user"], m.get("password", ""))
        c.will_set("faceid/status", "offline", retain=True)
        c.on_connect = self._on_connect
        c.on_message = self._on_message
        c.connect(m["host"], int(m.get("port", 1883)), keepalive=60)
        c.loop_start()
        self.client = c
        threading.Thread(target=self._worker, daemon=True, name="faceid-worker").start()
        threading.Thread(target=self._finalizer, daemon=True, name="faceid-finalizer").start()

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        log.info("MQTT verbunden (%s)", reason_code)
        client.subscribe("frigate/events")
        client.publish("faceid/status", "online", retain=True)
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
        st = self.events.setdefault(
            eid,
            {"camera": cam, "attempts": 0, "best_score": 0.0, "best_person": None,
             "best_unknown": None, "last_try": 0.0, "done": False, "ended": False,
             "created": time.time()},
        )
        if etype == "end":
            st["ended"] = True
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
            return
        face = FaceEngine.best_face(self.engine.faces(img), min_px=self.min_face_px)
        if face is None:
            return
        emb = face.normed_embedding
        slug, name, score = self.gallery.match(emb)
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
                    uid = self.gallery.save_unknown(
                        u["crop"], u["emb"],
                        {"camera": st["camera"], "event_id": eid,
                         "guess": u["guess"], "guess_score": round(u["guess_score"], 3)},
                        full_bgr=u.get("full"),
                    )
                    self._publish_recognition(eid, st, "unbekannt", u["guess_score"])
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
            self.client.publish("faceid/event", json.dumps(payload, ensure_ascii=False))
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
            self.client.publish(f"faceid/{cam}/person", ", ".join(names) or "niemand", retain=True)
            self.client.publish(f"faceid/{cam}/attributes", json.dumps(attrs, ensure_ascii=False), retain=True)

    def _publish_discovery(self):
        """HA MQTT-Discovery: ein Sensor je Kamera (zuletzt erkannte Person)."""
        cams = self.cameras or set(self.cfg["faceid"].get("discovery_cameras") or [])
        device = {"identifiers": ["faceid"], "name": "FaceID",
                  "manufacturer": "Eigenbau", "model": "InsightFace/ArcFace"}
        for cam in cams:
            conf = {
                "name": cam,  # HA stellt den Gerätenamen "FaceID" voran
                "unique_id": f"faceid_{cam}",
                "object_id": f"faceid_{cam}",
                "state_topic": f"faceid/{cam}/person",
                "json_attributes_topic": f"faceid/{cam}/attributes",
                "availability_topic": "faceid/status",
                "icon": "mdi:face-recognition",
                "device": device,
            }
            self.client.publish(f"homeassistant/sensor/faceid_{cam}/config",
                                json.dumps(conf, ensure_ascii=False), retain=True)
            # frischen Anwesenheits-Stand publizieren (räumt auch stale retained States nach Neustart auf)
            self._last_presence.pop(cam, None)
            self.present.setdefault(cam, {})
            self._publish_presence(cam)
