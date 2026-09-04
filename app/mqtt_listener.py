"""Frigate-Events per MQTT verarbeiten und Ergebnisse für HA publizieren.

Pipeline: frigate/events (person) -> Snapshot-Crop -> ArcFace -> Galerie-Match
  - Match  >= match_threshold   -> Person publizieren + Frigate sub_label
  - Match  <  unknown_threshold -> als Unbekannter in die Review-Queue
  - dazwischen                  -> unsicher; nur Review-Queue, keine Meldung

Gibt der Snapshot ueberhaupt kein Gesicht her, wird am Ereignisende die Aufnahme
abgetastet (clip_fallback) — das ist der haeufigste Fall, nicht die Ausnahme.
"""
import json
import logging
import queue
import threading
import time
from collections import deque

import paho.mqtt.client as mqtt
import requests

from .engine import FaceEngine, crop_face, reject_reason
from .hires import find_face_in_clip, upgrade_face

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
        # Frigate waehlt seinen Snapshot nach dem hoechsten Personen-Score, nicht danach,
        # ob ein Gesicht zu sehen ist — das ist oft der Moment, in dem jemand weggeht.
        # Ueber sieben Tage echter Ereignisse hatten nur 21 % der Snapshots ein
        # verwertbares Gesicht; im Clip fanden sich 9 von 12 doch noch. Deshalb: erst
        # Snapshot (sofort da, kostet nichts), und nur wenn der leer bleibt, die Aufnahme.
        self.clip_fallback = bool(f.get("clip_fallback", True))
        # Der Nutzen haengt am Blickwinkel: eine Kamera auf Kopfhoehe rettet fast jedes
        # Ereignis, eine hoch montierte keins — dort enthaelt auch die Aufnahme kein
        # Gesicht und der Scan kostet nur Rechenzeit. Leer = alle Kameras (wie cameras).
        self.clip_fallback_cameras = set(f.get("clip_fallback_cameras") or [])
        # Der Clip-Rueckgriff greift erst am Ereignisende — fuer Automationen, die beim
        # Betreten reagieren sollen, zu spaet (Issue #8). go2rtc liefert denselben
        # Haupt-Stream sofort (~1 s gemessen), die Aufnahme gaebe einen Zeitpunkt erst
        # nach rund 45 s her. Standardmaessig aus, weil Port 1984 nicht ueberall
        # erreichbar ist; der Startcheck sagt, ob es sich lohnt.
        self.live_hires = bool(f.get("live_hires_fallback", False))
        self.live_hires_cameras = set(f.get("live_hires_fallback_cameras") or [])
        # "always": Frigate ist nur noch der Ausloeser ("da ist wer"), FaceID sieht im
        # Vollbild selbst nach, wie viele Gesichter da sind. Gedacht fuer Gruppen, bei
        # denen Frigate eine verdeckte Person gar nicht erst trackt (Issue #8) — auf
        # sieben Tagen hiesiger Daten kam das nie vor (0 von 15 Gruppen), auf anderen
        # Kameras kann es anders aussehen. Deshalb Option, nicht Standard.
        mode = str(f.get("live_hires_mode", "fallback")).strip().lower()
        self.live_hires_mode = mode if mode in ("fallback", "always") else "fallback"
        # Eine Gruppe erzeugt mehrere Ereignisse fast gleichzeitig — ohne Sperre liefe
        # der teure Scan mehrfach fuer dasselbe Bild.
        self.live_cooldown = float(f.get("live_hires_cooldown", 2))
        self._live_last: dict[str, float] = {}    # Kamera -> letzter Vollbild-Scan
        self.clip_frames = int(f.get("clip_fallback_frames", 12))
        # strenger als beim Snapshot (0.55): aus zwoelf Frames darf man waehlerisch sein
        self.clip_min_det = float(f.get("clip_fallback_min_det", 0.65))
        # Frigate braucht nach dem Ereignis einen Moment, bis der Clip abrufbar ist
        self.clip_retries_max = int(f.get("clip_fallback_retries", 3))
        self.clip_retry_secs = float(f.get("clip_fallback_retry_seconds", 10))
        # klein gehalten: bei einem Ereignisschwall lieber welche auslassen als eine
        # Warteschlange aufbauen, die Minuten hinter der Gegenwart herlaeuft
        self.clip_queue: "queue.Queue[str]" = queue.Queue(maxsize=20)
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
        # letzte Meldung je Kamera, damit sie im Attribut stehen bleibt: der Finalizer
        # schreibt den Anwesenheitsstand alle paar Sekunden neu, und ohne diesen Merker
        # verschwand die letzte Erkennung, sobald das Anwesenheitsfenster ablief.
        self._last_event: dict[str, dict] = {}
        # Verlauf der Meldungen; wird vom Dienst gesetzt (None = nicht mitschreiben)
        self.history = None

    # ---------- MQTT ----------

    def start(self):
        m = self.cfg["mqtt"]
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.prefix)
        self.client = c
        if m.get("user"):
            c.username_pw_set(m["user"], m.get("password", ""))
        c.will_set(f"{self.prefix}/status", "offline", retain=True)
        c.on_connect = self._on_connect
        c.on_message = self._on_message
        c.connect(m["host"], int(m.get("port", 1883)), keepalive=60)
        c.loop_start()
        self._check_frigate()
        threading.Thread(target=self._worker, daemon=True, name="faceid-worker").start()
        threading.Thread(target=self._finalizer, daemon=True, name="faceid-finalizer").start()
        # Eigener Thread: ein Clip-Scan dauert Sekunden, im Finalizer wuerde er die
        # Anwesenheits-Aktualisierung blockieren, im Worker die naechsten Snapshots.
        # Laeuft unabhaengig von clip_fallback, damit die Option in den Einstellungen
        # sofort greift statt erst nach einem Neustart — er wartet dann nur an der Queue.
        threading.Thread(target=self._clip_worker, daemon=True, name="faceid-clip").start()
        # Einschraenkungen sichtbar machen: sonst sucht man spaeter im Log vergeblich
        # nach Aufnahme-Scans, die per Konfiguration gar nicht stattfinden sollen.
        if not self.clip_fallback:
            log.info("Recording fallback is off — events whose snapshot has no face are dropped")
        elif self.clip_fallback_cameras:
            log.info("Recording fallback limited to: %s",
                     ", ".join(sorted(self.clip_fallback_cameras)))
        self._check_go2rtc()

    def _check_go2rtc(self):
        """Einmal beim Start pruefen, ob der Live-Rueckgriff moeglich waere.

        Ohne diese Zeile bliebe die Option ein Ratespiel: go2rtc laeuft auf einem
        eigenen Port, den nicht jedes Setup freigibt — und ob er erreichbar ist, merkt
        man sonst erst am naechsten Ereignis, das nichts findet.
        """
        cams = sorted(self.live_hires_cameras) or sorted(self._frigate_cameras())
        if not cams:
            return
        img = self.frigate.live_frame(cams[0], timeout=4.0)
        if img is not None:
            h, w = img.shape[:2]
            if self.live_hires:
                log.info("Live hi-res fallback active via go2rtc (%dx%d)", w, h)
            else:
                log.info("go2rtc reachable (%dx%d) — 'live_hires_fallback' would work here", w, h)
        elif self.live_hires:
            log.warning("Live hi-res fallback is on, but go2rtc at %s did not answer — "
                        "recognition falls back to the recording at the end of an event",
                        self.frigate.go2rtc)
        if self.poll_interval > 0:
            threading.Thread(target=self._poller, daemon=True, name="faceid-poller").start()

    def _check_frigate(self):
        """Beim Start einmal nachsehen, ob Frigate ueberhaupt antwortet.

        Ohne diese Zeile im Log ist "es erkennt nichts" kaum von "es kommt nichts an"
        zu unterscheiden."""
        url = self.cfg["frigate"]["url"].rstrip("/")
        conf = self.frigate.config()
        try:
            if conf is None:
                log.error("Frigate at %s did not answer — without snapshots nothing can "
                          "be recognised", url)
                return
            cams = list((conf.get("cameras") or {}).keys())
            log.info("Frigate reachable (%s), cameras: %s", url, ", ".join(cams) or "keine")
            if self.cameras:
                unknown = self.cameras - set(cams)
                if unknown:
                    log.warning("Configured camera(s) %s do not exist in Frigate — nothing from these will ever be processed", ", ".join(sorted(unknown)))
        except (requests.RequestException, ValueError) as e:
            log.error("Frigate at %s unreachable: %s — snapshots, and therefore recognition, will fail", url, e)

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        # Eine Exception in einem paho-Callback beendet dessen Netzwerk-Thread. Der Dienst
        # laeuft dann weiter, die Web-UI antwortet, Gesichter werden erkannt — nur MQTT ist
        # still, ohne weitere Meldung. Genau so trat Issue #11 auf. Deshalb faengt jeder
        # Callback hier ab und protokolliert, statt die Verbindung mitzureissen.
        try:
            log.info("MQTT connected (%s), subscribing to %s/events", reason_code, self.frigate_topic)
            client.subscribe(f"{self.frigate_topic}/events")
            client.publish(f"{self.prefix}/status", "online", retain=True)
            self._publish_discovery()
        except Exception:
            log.exception("error in the MQTT connect callback — connection kept alive")

    def _on_message(self, client, userdata, msg):
        try:
            self._handle_message(msg)
        except Exception:
            log.exception("error while handling an MQTT message — connection kept alive")

    def _handle_message(self, msg):
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
             "created": time.time(), "zones": [],
             "start_time": after.get("start_time") or time.time(), "end_time": None},
        )
        # Zonen aus jedem Update mitschreiben: beim Veroeffentlichen liegt der
        # Frigate-Payload laengst nicht mehr vor. `entered_zones` ist kumulativ und
        # beantwortet genau die Frage, die Automationen stellen — war die Person in
        # Zone X? Nur uebernehmen, wenn gefuellt, damit ein spaeteres Update die
        # Angabe nicht wieder ausloescht.
        if after.get("entered_zones"):
            st["zones"] = list(after["entered_zones"])
        if etype == "end":
            st["ended"] = True
            st["end_time"] = after.get("end_time") or time.time()
        if st["done"] or st["attempts"] >= self.max_attempts:
            return
        # Ohne Snapshot gibt es fuer den Normalweg nichts zu holen. Ist der Vollbild-Weg
        # fuer diese Kamera aber eingeschaltet, ist genau das der Fall, fuer den er gebaut
        # wurde: Frigate verweigert den Snapshot an einer Zonenbedingung, waehrend die
        # Person klar im Bild steht — und FaceID tat bisher gar nichts (Issue #14).
        if ((after.get("has_snapshot") or self._live_possible(st["camera"]))
                and time.time() - st["last_try"] >= self.retry_secs):
            st["last_try"] = time.time()
            try:
                self.queue.put_nowait({"eid": eid})
            except queue.Full:
                log.warning("queue full, skipping event %s", eid)

    # ---------- Verarbeitung ----------

    def _worker(self):
        while True:
            item = self.queue.get()
            try:
                self._process(item["eid"])
            except Exception:
                log.exception("error while handling event %s", item["eid"])

    def _process(self, eid: str):
        st = self.events.get(eid)
        if st is None or st["done"]:
            return
        st["attempts"] += 1
        if self.live_hires_mode == "always":
            # Zuerst das Vollbild — der Snapshot laeuft danach trotzdem, denn nur er
            # sagt, WER zu diesem Ereignis gehoert.
            self._try_live_hires(eid, st, always=True)
        img = self.frigate.snapshot(eid, crop=True)
        if img is None:
            log.info("event %s (%s): no snapshot from Frigate", eid, st["camera"])
            # always=True, weil ohne Snapshot niemand sagt, WER zu diesem Ereignis
            # gehoert: melden und zur Anwesenheit zaehlen ja, sub_label nein. Genau
            # dieselbe Begruendung wie im always-Modus.
            self._try_live_hires(eid, st, always=True)
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
            log.info("event %s (%s): attempt %d, %s (snapshot %dx%d)",
                     eid, st["camera"], st["attempts"],
                     reject_reason(found, self.min_face_px), w, h)
            self._try_live_hires(eid, st)
            return
        self._handle_face(eid, st, img, face)

    def _live_possible(self, camera: str) -> bool:
        """Koennte der Vollbild-Weg fuer diese Kamera ueberhaupt greifen?"""
        if not self.live_hires:
            return False
        return not self.live_hires_cameras or camera in self.live_hires_cameras

    def _try_live_hires(self, eid: str, st: dict, always: bool = False):
        """Der Snapshot gab nichts her — sofort einen Haupt-Stream-Frame nachschieben.

        Bewusst nur EINMAL je Ereignis: der Abruf kostet rund eine Sekunde, und bei
        ``max_attempts`` Versuchen waere das sonst das Vielfache, ohne mehr zu finden —
        die Kamera liefert in dieser Zeit kaum ein anderes Bild.
        """
        if not self.live_hires or st.get("live_tried") or st["done"]:
            return
        if self.live_hires_cameras and st["camera"] not in self.live_hires_cameras:
            return
        if always:
            last = self._live_last.get(st["camera"], 0.0)
            if time.time() - last < self.live_cooldown:
                return          # dieselbe Gruppe, das Bild waere praktisch dasselbe
            self._live_last[st["camera"]] = time.time()
        st["live_tried"] = True
        t0 = time.time()
        frame = self.frigate.live_frame(st["camera"])
        if frame is None:
            log.info("event %s (%s): no live frame from go2rtc — is it reachable?",
                     eid, st["camera"])
            return
        h, w = frame.shape[:2]
        # ALLE Gesichter, nicht nur das groesste: ein Vollbild zeigt oft mehrere Personen
        # (gemessen in 5 von 12 Tuer-Ereignissen). Frigate legt zwar je Person ein eigenes
        # Ereignis an, aber wessen Snapshot nichts hergibt, faellt sonst hinten runter —
        # obwohl das Gesicht hier klar zu sehen ist. Groesstes zuerst: das bindet ans
        # Ereignis, alle weiteren werden nur gemeldet.
        faces = sorted((f for f in self.engine.faces(frame)
                        if (f.bbox[2] - f.bbox[0]) >= self.min_face_px
                        and (f.bbox[3] - f.bbox[1]) >= self.min_face_px
                        and float(f.det_score) >= 0.55),
                       key=lambda f: -(f.bbox[2] - f.bbox[0]))
        if not faces:
            log.info("event %s (%s): live frame %dx%d has no usable face either (%.1fs)",
                     eid, st["camera"], w, h, time.time() - t0)
            return
        log.info("event %s (%s): live frame %dx%d has %d face(s) the snapshot missed "
                 "(largest %dpx, det %.2f, %.1fs)", eid, st["camera"], w, h, len(faces),
                 int(faces[0].bbox[2] - faces[0].bbox[0]), float(faces[0].det_score),
                 time.time() - t0)
        for i, face in enumerate(faces):
            # Im always-Modus bindet NICHTS ans Ereignis: der Scan laeuft, bevor der
            # Snapshot geklaert hat, wer gemeint ist — das groesste Gesicht im Vollbild
            # kann jemand ganz anderes sein. Melden und Anwesenheit ja, sub_label nein.
            self._handle_face(eid, st, frame, face, source="live frame",
                              primary=(i == 0 and not always))

    def _handle_face(self, eid: str, st: dict, img, face, source: str = "snapshot",
                     primary: bool = True):
        """Gefundenes Gesicht zuordnen, melden, ablegen.

        Gemeinsam fuer Snapshot und Aufnahme — beide Wege muessen dieselben Schwellen,
        dieselbe Ignore-Logik und dieselbe Meldung verwenden, sonst haengt das Ergebnis
        davon ab, welcher Weg zufaellig gegriffen hat.

        ``primary=False`` ist eine WEITERE Person im selben Bild: sie wird gemeldet und
        zaehlt zur Anwesenheit, bindet aber nichts ans Ereignis. Frigates ``sub_label``
        nimmt nur einen Namen, und ``best_score`` gehoert der Person, um die es in diesem
        Ereignis geht — Frigate legt je Person ein eigenes an.
        """
        via = "" if source == "snapshot" else f" (from the {source})"
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
                    log.info("event %s: new auto ignore anchor %s (sim %.3f)", eid, iid, ig)
            log.info("event %s (%s): ignored face (sim %.3f)", eid, st["camera"], ig)
            return
        crop = crop_face(img, face.bbox)
        # Ausdruecklich dazuschreiben, ob der Treffer die Schwelle nimmt. Ohne das liest
        # sich "match X (0.251)" wie eine Zuordnung — und ein zu schwacher Kandidat sieht
        # aus wie eine falsche Erkennung, obwohl nichts publiziert wird (aus der Community).
        verdict = ("published" if (slug and score >= self.match_thr)
                   else f"below threshold {self.match_thr:.2f}, NOT published — review queue")
        log.info("event %s (%s): attempt %d, best match %s (%.3f)%s — %s", eid, st["camera"],
                 st["attempts"], name, score, via, verdict)

        if slug and score >= self.match_thr:
            if not primary:
                # Nur melden und zur Anwesenheit zaehlen; kein sub_label, kein best_score.
                self._publish_recognition(eid, st, name, score, crop=crop, emb=emb)
                return
            if score > st["best_score"]:
                st["best_score"], st["best_person"] = score, name
                self._publish_recognition(eid, st, name, score, crop=crop, emb=emb)
                if self.set_sub_label:
                    self.frigate.set_sub_label(eid, name, score)
            if score >= self.match_thr + 0.1:
                st["done"] = True  # sehr sicherer Treffer -> keine weiteren Versuche
        else:
            if not primary:
                # Unsichere Zweitgesichter nicht in die Review-Queue: die Queue soll die
                # Person des Ereignisses zeigen, nicht jeden Passanten im Hintergrund.
                return
            # bestes unsicheres/unbekanntes Gesicht des Events merken, Ablage erst beim Event-Ende
            prev = st.get("best_unknown")
            if prev is None or face.det_score > prev["det_score"]:
                st["best_unknown"] = {"crop": crop, "emb": emb, "det_score": float(face.det_score),
                                      "guess": name, "guess_score": float(score), "full": img,
                                      # aus der Aufnahme ist bereits das schaerfste Bild —
                                      # ein zweiter Durchgang durch hires waere derselbe Clip
                                      "from_clip": source != "snapshot"}

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
                # Hier bleibt der Snapshot Pflicht, anders als im MQTT-Weg: der Poll
                # holt Nachzuegler von vor bis zu fuenf Minuten, und ein Vollbild von
                # JETZT zeigt die Person von damals nicht mehr. Ohne Snapshot gaebe es
                # dort also nichts zu gewinnen.
                batch = self.frigate.events(label="person", has_snapshot=1,
                                            limit=50, after=since - 30)
            except (requests.RequestException, ValueError) as e:
                log.debug("poll failed: %s", e)
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
                    # hier sogar vollstaendig: das Ereignis ist abgeschlossen
                    "zones": list(ev.get("zones") or []),
                    "start_time": ev.get("start_time") or time.time(),
                    "end_time": ev.get("end_time"),
                }
                log.info("poll: picked up event %s (%s) — never announced over MQTT",
                         eid, cam)
                try:
                    self.queue.put_nowait({"eid": eid})
                except queue.Full:
                    log.warning("queue full — dropped polled event %s", eid)

    def _clip_wanted(self, camera: str) -> bool:
        """Lohnt der Aufnahme-Rueckgriff bei dieser Kamera?

        Leere Liste = alle, wie bei ``cameras``. Sonst nur die genannten: an hoch
        montierten Kameras schaut niemand ins Objektiv, dort enthaelt auch die Aufnahme
        kein Gesicht — der Scan kostet dann Sekunden je Ereignis fuer nichts.
        """
        return not self.clip_fallback_cameras or camera in self.clip_fallback_cameras

    def _clip_worker(self):
        """Ereignisse nachbearbeiten, deren Snapshot kein Gesicht hergab."""
        while True:
            eid = self.clip_queue.get()
            try:
                self._process_clip(eid)
            except Exception:
                log.exception("error while scanning the recording of event %s", eid)
            finally:
                st = self.events.get(eid)
                if st is not None:
                    st["clip_pending"] = False

    def _process_clip(self, eid: str):
        st = self.events.get(eid)
        if st is None or st["done"]:
            return
        t0 = time.time()
        stats: dict = {}
        hit = find_face_in_clip(self.engine, self.frigate, eid,
                                max_frames=self.clip_frames,
                                min_px=self.min_face_px,
                                min_det=self.clip_min_det, stats=stats)
        took = time.time() - t0
        if hit is None and not stats.get("frames"):
            # Kein einziger Frame gelesen: Frigate stellt den Clip erst nach dem
            # Ereignis fertig, der Finalizer greift aber direkt danach zu. Das als
            # "kein Gesicht" zu verbuchen verwirft ein Ereignis wegen einer Datei,
            # die es Sekunden spaeter gibt — gemessen rund jeder vierte Scan.
            tries = st.get("clip_retries", 0) + 1
            st["clip_retries"] = tries
            if tries <= self.clip_retries_max:
                st["clip_tried"] = False               # erneut einreihen erlauben
                st["clip_retry_after"] = time.time() + self.clip_retry_secs
                log.info("event %s (%s): recording not ready yet, retry %d/%d in %ds",
                         eid, st["camera"], tries, self.clip_retries_max,
                         int(self.clip_retry_secs))
            else:
                log.info("event %s (%s): recording still not available after %d tries",
                         eid, st["camera"], tries)
            return
        if hit is None:
            log.info("event %s (%s): no face in the recording either (%d frames, %.1fs)",
                     eid, st["camera"], stats.get("frames", 0), took)
            return
        face, frame = hit
        log.info("event %s (%s): the recording has a face the snapshot missed "
                 "(%dpx, det %.2f, %.1fs)", eid, st["camera"],
                 int(face.bbox[2] - face.bbox[0]), float(face.det_score), took)
        self._handle_face(eid, st, frame, face, source="recording")

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
                # Snapshot hat nichts gefunden -> in der Aufnahme nachsehen, bevor das
                # Ereignis verworfen wird. Erst hier, weil der Clip erst am Ende steht.
                if (self.clip_fallback and st["best_person"] is None
                        and st["best_unknown"] is None and not st.get("clip_tried")
                        and now >= st.get("clip_retry_after", 0)
                        and self._clip_wanted(st["camera"])):
                    st["clip_tried"] = True
                    try:
                        self.clip_queue.put_nowait(eid)
                        st["clip_pending"] = True
                        st["clip_since"] = now
                        continue
                    except queue.Full:
                        log.info("event %s: clip queue full, skipping the recording scan", eid)
                if st.get("clip_pending"):
                    if now - st.get("clip_since", now) < 300:
                        continue      # laeuft noch
                    log.warning("event %s: recording scan did not finish, closing anyway", eid)
                    st["clip_pending"] = False
                if st["best_person"] is None and st["best_unknown"] is not None:
                    u = st["best_unknown"]
                    crop, emb, full = u["crop"], u["emb"], u.get("full")
                    if self.hires_enroll and not u.get("from_clip"):
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
                                log.info("event %s: sharper reference from the recording (%dpx instead of %dpx)",
                                         eid, new_w, old_w)
                    uid = self.gallery.save_unknown(
                        crop, emb,
                        {"camera": st["camera"], "event_id": eid,
                         "event_ts": st.get("start_time"),
                         "guess": u["guess"], "guess_score": round(u["guess_score"], 3)},
                        full_bgr=full,
                    )
                    # crop/emb statt u[...]: kommt der schaerfere Ausschnitt aus der
                    # Aufnahme, ist genau der auch der gemeldete.
                    self._publish_recognition(eid, st, "unknown", u["guess_score"],
                                              crop=crop, emb=emb)
                    log.info("event %s: unknown face stored (%s)", eid, uid)
                self.events.pop(eid, None)

    # ---------- Publish ----------

    def _publish_recognition(self, eid: str, st: dict, name: str, score: float,
                             crop=None, emb=None):
        payload = {
            "person": name, "score": round(float(score), 3), "camera": st["camera"],
            "event_id": eid, "ts": time.time(),
            # Frigate-Zonen, die die Person in diesem Ereignis betreten hat. Leer, wenn
            # die Kamera keine Zonen hat ODER Frigate die Person keiner zugeordnet hat —
            # beides sieht gleich aus, deshalb in Automationen nie auf "leer heisst
            # ausserhalb" bauen.
            "zones": list(st.get("zones") or []),
        }
        self.recent.appendleft(payload)
        # faceid/event genau einmal pro (Event, Person) — Score-Verbesserungen lösen keine
        # erneute Meldung aus (sonst mehrere Notifications für dieselbe Sichtung).
        # Eine MENGE, kein einzelner Name: in einem Vollbild koennen mehrere Bekannte
        # stehen, die sich sonst gegenseitig ueberschreiben und abwechselnd neu gemeldet
        # wuerden — genau die Doppelmeldung, die diese Stelle verhindern soll.
        # Erst vermerken, wenn die Meldung den Client wirklich verlassen hat. Ein
        # vorhandener Client heisst nicht, dass die Verbindung steht: bei getrenntem
        # Broker liefert paho MQTT_ERR_NO_CONN. Wuerde der Name schon davor als gemeldet
        # gelten, bliebe er es fuer dieses Ereignis auch nach dem Wiederverbinden, und
        # kein spaeterer Treffer koennte die Meldung nachholen. Der Verlauf fuehrt seine
        # eigene Merkliste (hids, s. unten).
        announced = st.setdefault("announced", set())
        if self.client and name not in announced:
            info = self.client.publish(f"{self.prefix}/event",
                                       json.dumps(payload, ensure_ascii=False))
            if getattr(info, "rc", mqtt.MQTT_ERR_SUCCESS) == mqtt.MQTT_ERR_SUCCESS:
                announced.add(name)
        # "unknown" gehoert NICHT in die Anwesenheitsliste. Der Sensor-State ist eine
        # Aufzaehlung von Namen ("Christian, Juli"), und ein hineingemischtes "unknown"
        # liest sich wie ein weiterer Name — auf dem Handy stand "Christian unknown ist
        # da". Fremde meldet ausschliesslich das Event-Topic; der Sensor sagt, WER da ist.
        if name != "unknown":
            self.present.setdefault(st["camera"], {})[name] = time.time()
        self._publish_presence(st["camera"], last=payload)
        # Den TATSAECHLICH benutzten Ausschnitt festhalten. Der Frigate-Snapshot wird
        # waehrend des Ereignisses fortlaufend ersetzt und zeigt spaeter oft einen anderen
        # Moment — eine Nachpruefung an ihm fuehrt in die Irre. Siehe app/history.py.
        # EINE Zeile je (Ereignis, Person). Ein spaeterer Treffer derselben Person meldet
        # nichts mehr (s. oben) — eine zweite Zeile behauptete also eine Meldung, die es
        # nie gab, und verdraengte dabei aeltere echte Eintraege: gemessen 27 solcher
        # Zeilen bei 200 Plaetzen, also 13 % weniger Reichweite. Der spaetere Ausschnitt
        # ist aber oft der bessere Beleg, deshalb ersetzt er das Bild, statt verworfen zu
        # werden.
        if self.history is not None and crop is not None:
            # Die vorhandene Zeile ist die Merkliste: gibt es noch keine — auch weil der
            # erste Treffer ohne Ausschnitt kam oder das Anlegen scheiterte —, wird
            # angelegt statt verbessert. Sonst fiele die Erkennung stillschweigend aus
            # dem Verlauf, obwohl sie stattgefunden hat.
            hids = st.setdefault("hids", {})
            hid = hids.get(name)
            if hid is None:
                hid = self.history.add(
                    crop, emb, {k: v for k, v in payload.items() if k != "ts"}
                    | {"ts": payload["ts"], "attempt": st.get("attempts")})
                if hid:
                    hids[name] = hid
            else:
                self.history.improve(hid, crop, emb, score,
                                     attempt=st.get("attempts"))

    def _publish_presence(self, cam: str, last: dict | None = None):
        """Sensor-State = alle im Fenster gesehenen Personen ('Christian, Juli' / 'niemand')."""
        now = time.time()
        pres = self.present.setdefault(cam, {})
        for n, ts in list(pres.items()):
            if now - ts > self.presence_window:
                pres.pop(n)
        names = [n for n, _ in sorted(pres.items(), key=lambda kv: -kv[1])]
        if last:
            self._last_event[cam] = last
        if names == self._last_presence.get(cam) and last is None:
            return  # nichts geändert -> retained Topic nicht neu beschreiben
        self._last_presence[cam] = names
        if self.client:
            attrs = {"persons": names, "window_s": self.presence_window, "ts": now}
            # 'last' bleibt erhalten, auch wenn niemand mehr anwesend ist — es beantwortet
            # "wer wurde hier zuletzt erkannt?", und das wird nicht falsch, nur weil die
            # Person gegangen ist. Nach einem Neustart erst wieder ab der ersten Erkennung.
            seen = self._last_event.get(cam)
            if seen:
                attrs["last"] = seen
            self.client.publish(f"{self.prefix}/{cam}/person", ", ".join(names) or "nobody", retain=True)
            self.client.publish(f"{self.prefix}/{cam}/attributes", json.dumps(attrs, ensure_ascii=False), retain=True)

    def _frigate_cameras(self) -> set:
        """Kameranamen von Frigate holen — fuer den Fall, dass keine konfiguriert sind."""
        try:
            conf = self.frigate.config()
            if conf is not None:
                return set((conf.get("cameras") or {}).keys())
        except (requests.RequestException, ValueError) as e:
            log.warning("could not fetch the camera list from Frigate (%s) — sensors will appear once the first person is seen", e)
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
            log.info("MQTT discovery: announced %d sensor(s)%s", len(cams),
                     "" if cams else " — cameras unknown, they follow on first recognition")
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
            if self.client:
                self.client.publish(f"homeassistant/sensor/{self.prefix}_{cam}/config",
                                    json.dumps(conf, ensure_ascii=False), retain=True)
            # frischen Anwesenheits-Stand publizieren (räumt auch stale retained States nach Neustart auf)
            self._last_presence.pop(cam, None)
            self.present.setdefault(cam, {})
            self._publish_presence(cam)
