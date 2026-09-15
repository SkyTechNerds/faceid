"""Watch completed local media files and feed their faces into FaceID.

The recorder writes temporary files with another suffix and renames them when complete.
The watcher still requires an unchanged size/mtime over two scans and a minimum age, so
it is safe with recorders that write directly to the final filename as well.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger("faceid.folder")

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


class MediaReadError(RuntimeError):
    pass


def _sample_frames(path: Path, max_frames: int):
    """Yield evenly spaced frames and return a decode count via generator exhaustion."""
    if path.suffix.lower() in IMAGE_EXTENSIONS:
        frame = cv2.imread(str(path))
        if frame is None:
            raise MediaReadError(f"could not decode image: {path.name}")
        yield frame
        return

    cap = cv2.VideoCapture(str(path))
    try:
        if not cap.isOpened():
            raise MediaReadError(f"could not open video: {path.name}")
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if total <= 0:
            raise MediaReadError(f"video reports no frames: {path.name}")
        targets = set(np.linspace(0, total - 1,
                                  min(max(1, max_frames), total)).astype(int).tolist())
        decoded = 0
        # Seeking repeatedly in H.264 can land before the reference frame OpenCV needs,
        # producing corrupted samples and noisy decoder errors. Sequential decode is
        # cheap for the short surveillance clips this input targets and deterministic.
        for index in range(total):
            ok, frame = cap.read()
            if index in targets and ok and frame is not None:
                decoded += 1
                yield frame
        if decoded == 0:
            raise MediaReadError(f"no sampled frames decoded: {path.name}")
    finally:
        cap.release()


def scan_media(engine, path: Path, max_frames: int = 24, min_face_px: int = 48,
               min_det: float = 0.65, same_person_similarity: float = 0.55,
               max_people: int = 6) -> tuple[list[dict], dict]:
    """Find distinct faces across a file, retaining the clearest crop per person.

    The within-file clustering is deliberately conservative. It prevents 24 samples of
    one visitor from becoming 24 review entries while keeping two people in one frame
    separate. Cross-visit clustering remains Gallery's job.
    """
    clusters: list[dict] = []
    frames = detections = 0
    for frame in _sample_frames(path, max_frames):
        frames += 1
        for face in engine.faces(frame):
            w = float(face.bbox[2] - face.bbox[0])
            h = float(face.bbox[3] - face.bbox[1])
            det = float(face.det_score)
            if w < min_face_px or h < min_face_px or det < min_det:
                continue
            detections += 1
            emb = np.asarray(face.normed_embedding, dtype=np.float32)
            best_i, best_sim = None, -1.0
            for i, cluster in enumerate(clusters):
                sim = float(cluster["centroid"] @ emb)
                if sim > best_sim:
                    best_i, best_sim = i, sim
            quality = min(w, h) * det
            if best_i is None or best_sim < same_person_similarity:
                clusters.append({"centroid": emb.copy(), "embeddings": [emb],
                                 "face": face, "frame": frame.copy(), "quality": quality,
                                 "detections": 1})
                continue
            cluster = clusters[best_i]
            cluster["embeddings"].append(emb)
            centroid = np.mean(cluster["embeddings"], axis=0)
            norm = float(np.linalg.norm(centroid))
            cluster["centroid"] = centroid / norm if norm else centroid
            cluster["detections"] += 1
            if quality > cluster["quality"]:
                cluster.update(face=face, frame=frame.copy(), quality=quality)

    clusters.sort(key=lambda c: c["quality"], reverse=True)
    selected = [{"face": c["face"], "frame": c["frame"],
                 "detections": c["detections"], "quality": c["quality"]}
                for c in clusters[:max(1, max_people)]]
    return selected, {"frames": frames, "detections": detections,
                      "distinct_faces": len(selected)}


class FolderIngest:
    def __init__(self, cfg: dict, data_dir: Path, engine, processor):
        fc = cfg.get("folder") or {}
        self.enabled = bool(fc.get("enabled", False))
        self.path = Path(str(fc.get("path", ""))).expanduser()
        self.camera = str(fc.get("camera", "front_door")).strip() or "front_door"
        self.recursive = bool(fc.get("recursive", True))
        self.process_existing = bool(fc.get("process_existing", True))
        self.poll_interval = max(1.0, float(fc.get("poll_interval", 10)))
        self.settle_seconds = max(0.0, float(fc.get("settle_seconds", 10)))
        self.max_frames = max(1, int(fc.get("max_frames", 24)))
        self.min_face_px = max(1, int(fc.get("min_face_px",
                                               cfg.get("faceid", {}).get("min_face_px", 48))))
        self.min_det = float(fc.get("min_detection_score", 0.65))
        self.same_person_similarity = float(fc.get("same_person_similarity", 0.55))
        self.max_people = max(1, int(fc.get("max_people_per_file", 6)))
        self.max_retries = max(1, int(fc.get("max_retries", 3)))
        # Obergrenze fuer den Fingerabdruck-Index. Ohne sie waechst er monoton: jede je
        # verarbeitete Datei bleibt fuer immer stehen, auch wenn sie laengst geloescht ist.
        # Der Index wird bei JEDER Datei komplett neu geschrieben, die Kosten wachsen also
        # mit allem, was vorher da war — gemessen 320 ms je Datei bei 50 000 Eintraegen.
        # 0 schaltet die Begrenzung ab. Die Galerie ist davon nicht beruehrt: gelernte
        # Gesichter liegen unter data/persons und altern nie.
        # Wie bei min_face_px: der folder-Block gewinnt, sonst gilt der Wert aus dem
        # faceid-Block. Der zweite ist der, den der Einstellungen-Tab schreibt — ohne
        # ihn ueberlebte eine dort gesetzte Grenze den naechsten Neustart nicht.
        self.max_indexed_files = max(0, int(fc.get(
            "max_indexed_files",
            cfg.get("faceid", {}).get("folder_max_indexed_files", 5000))))
        self.retry_seconds = max(1.0, float(fc.get("retry_seconds", 60)))
        exts = fc.get("extensions") or sorted(VIDEO_EXTENSIONS)
        self.extensions = {str(e).lower() if str(e).startswith(".") else f".{str(e).lower()}"
                           for e in exts}
        self.data_dir = data_dir
        self.state_file = data_dir / "folder_ingest.json"
        self.engine = engine
        self.processor = processor
        self._observed: dict[str, tuple[tuple[int, int], float]] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._state = self._load_state()
        self._status = {"enabled": self.enabled, "path": str(self.path), "camera": self.camera,
                        "running": False, "scanning": False, "last_scan": 0.0,
                        "last_error": "", "last_result": None}

    def _load_state(self) -> dict:
        try:
            raw = json.loads(self.state_file.read_text(encoding="utf-8"))
            if raw.get("version") == 1 and isinstance(raw.get("files"), dict):
                # "processing" heisst: ein Lauf hat begonnen und nie abgeschlossen —
                # der Dienst wurde beendet oder ist abgestuerzt. Solche Eintraege sind
                # von der Verdraengung ausgenommen, also wuerden sie ewig stehen bleiben
                # und irgendwann die Obergrenze unerfuellbar machen. Auf "failed" setzen:
                # die Datei wird erneut versucht, und der Versuchszaehler bleibt erhalten,
                # damit eine dauerhaft kaputte Datei nicht endlos wiederholt wird.
                stale = [k for k, v in raw["files"].items()
                         if v.get("status") == "processing"]
                for key in stale:
                    raw["files"][key].update(status="failed", failed_at=time.time(),
                                             next_retry=0.0,
                                             error="interrupted before it finished")
                if stale:
                    log.info("folder index: %d interrupted entries reset for retry",
                             len(stale))
                return raw
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            log.warning("folder state unreadable; starting with an empty index")
        return {"version": 1, "files": {}}

    def _finish_scan(self, result: dict):
        """Einmal am Ende eines Laufs aufraeumen — nicht zwischendurch."""
        self._status["last_result"] = result
        if self._enforce_index_cap():
            self._save_state()
        # Wenn der Ordner dauerhaft mehr Dateien haelt als der Index merken darf, wird
        # jeder Lauf einen Teil davon erneut verarbeiten. Das ist kein Fehler, aber es
        # ist Arbeit ohne Ertrag — und man sieht es sonst nirgends.
        if self.max_indexed_files and result.get("found", 0) > self.max_indexed_files:
            log.warning("folder holds %d files but the index remembers at most %d — "
                        "files beyond the limit are re-processed on every scan; "
                        "raise folder.max_indexed_files or thin out the folder",
                        result["found"], self.max_indexed_files)

    def _entry_age_key(self, item: dict) -> float:
        """Wann wurde dieser Eintrag zuletzt angefasst? Aeltestes zuerst verdraengen."""
        return float(item.get("processed_at") or item.get("failed_at") or 0.0)

    def _enforce_index_cap(self) -> int:
        """Aelteste Eintraege verwerfen, bis die Obergrenze eingehalten ist.

        Laufende Eintraege (``processing``) bleiben unangetastet — sie gehoeren zum
        gerade aktiven Scan, und sie zu verwerfen hiesse, dieselbe Datei doppelt zu
        verarbeiten. Ein verworfener Eintrag bedeutet nur, dass die Datei bei Bedarf
        noch einmal gelesen wird; verloren geht nichts.
        """
        files = self._state.get("files", {})
        if not self.max_indexed_files or len(files) <= self.max_indexed_files:
            return 0
        removable = [k for k, v in files.items() if v.get("status") != "processing"]
        removable.sort(key=lambda k: self._entry_age_key(files[k]))
        drop = len(files) - self.max_indexed_files
        dropped = 0
        for key in removable[:drop]:
            del files[key]
            dropped += 1
        if dropped:
            log.info("folder index trimmed: %d oldest entries dropped, %d kept (cap %d)",
                     dropped, len(files), self.max_indexed_files)
        return dropped

    def _save_state(self):
        # Bewusst OHNE Verdraengung: waehrend eines Laufs wird nach jeder Datei
        # gespeichert, und wer dabei Eintraege wegwirft, laesst genau die Dateien wieder
        # auflaufen, die er gerade erst verarbeitet hat — beim naechsten Lauf noch einmal,
        # und so fort. Getrimmt wird einmal am Ende, in _finish_scan().
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._state, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.state_file)

    @staticmethod
    def _signature(path: Path) -> tuple[int, int]:
        st = path.stat()
        return st.st_size, st.st_mtime_ns

    def _files(self) -> list[Path]:
        iterator = self.path.rglob("*") if self.recursive else self.path.glob("*")
        out = []
        for path in iterator:
            try:
                if path.is_file() and path.suffix.lower() in self.extensions:
                    out.append((path.stat().st_mtime_ns, str(path), path))
            except OSError:
                continue
        return [row[2] for row in sorted(out)]

    def _event_base(self, path: Path, signature: tuple[int, int]) -> str:
        raw = f"{path.resolve()}\0{signature[0]}\0{signature[1]}".encode()
        return "folder-" + hashlib.sha256(raw).hexdigest()[:20]

    def start(self):
        if not self.enabled:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="faceid-folder")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.poll_interval + 2)

    def _run(self):
        self._status["running"] = True
        log.info("folder input watching %s for %s", self.path,
                 ", ".join(sorted(self.extensions)))
        while not self._stop.is_set():
            try:
                self.scan_once()
            except Exception:
                if not self._status.get("last_error"):
                    self._status["last_error"] = "unexpected scan failure"
                log.exception("folder scan failed")
            self._stop.wait(self.poll_interval)
        self._status["running"] = False

    def scan_once(self, now: float | None = None, progress=None) -> dict:
        """Discover and synchronously process stable files; safe to call from tests/UI.

        Die Sperre haelt den **ganzen** Lauf, nicht nur einen Zaehler: der Poller und ein
        von Hand ausgeloester Scan wuerden sonst dieselbe Datei gleichzeitig verarbeiten
        und sich in der Fingerabdruck-Buchhaltung ins Gehege kommen.

        Haelt sie schon jemand, kehrt der zweite Aufruf **sofort** zurueck, statt zu warten.
        Warten hiesse: der Poller ist fertig, und der Handscan geht anschliessend dieselbe
        Menge noch einmal durch — Arbeit ohne Ergebnis, und in der Oberflaeche ein Balken,
        der ein zweites Mal von vorn beginnt.
        """
        now = time.time() if now is None else now
        if not self._lock.acquire(blocking=False):
            log.info("folder scan already running — skipping this request")
            return {"found": 0, "stable": 0, "processed": 0, "failed": 0,
                    "faces": 0, "skipped": 0, "busy": True}
        try:
            self._status.update(scanning=True, last_scan=now, last_error="")
            result = {"found": 0, "stable": 0, "processed": 0, "failed": 0,
                      "faces": 0, "skipped": 0}
            try:
                if not self.path.is_dir():
                    raise FileNotFoundError(f"watch folder is unavailable: {self.path}")
                files = self._files()
                result["found"] = len(files)
                # Sofort melden, nicht erst am Ende: bei einem grossen Ordner steht der
                # Balken sonst den ganzen Lauf auf 0/0 und liest sich wie ein Haenger.
                if progress:
                    progress(0, len(files))
                # ``process_existing: false`` is a one-time baseline, not a permanent
                # instruction to ignore every file that has never been seen before. Record
                # startup contents now; later arrivals follow the normal settle path.
                if not self.process_existing and not self._state.get("initialized"):
                    for path in files:
                        key = str(path.resolve())
                        try:
                            signature = self._signature(path)
                        except OSError:
                            continue
                        self._observed[key] = (signature, now)
                        self._state["files"][key] = {
                            "signature": [signature[0], signature[1]],
                            "status": "skipped", "processed_at": now,
                            "reason": "present when folder input was first enabled",
                        }
                        result["skipped"] += 1
                    self._state["initialized"] = True
                    self._save_state()
                    self._finish_scan(result)
                    return result
                for index, path in enumerate(files, 1):
                    if progress:
                        progress(index, len(files))
                    key = str(path.resolve())
                    try:
                        signature = self._signature(path)
                    except OSError:
                        continue
                    previous = self._observed.get(key)
                    if previous is None or previous[0] != signature:
                        self._observed[key] = (signature, now)
                        continue
                    if now - (signature[1] / 1_000_000_000) < self.settle_seconds:
                        continue
                    result["stable"] += 1
                    old = self._state["files"].get(key)
                    sig_json = [signature[0], signature[1]]
                    if old and old.get("signature") == sig_json:
                        if old.get("status") in ("processed", "skipped"):
                            result["skipped"] += 1
                            continue
                        if (old.get("status") == "failed" and
                                (old.get("attempts", 0) >= self.max_retries or
                                 now < old.get("next_retry", 0))):
                            result["skipped"] += 1
                            continue
                    attempts = ((old or {}).get("attempts", 0) + 1
                                if old and old.get("signature") == sig_json else 1)
                    self._state["files"][key] = {"signature": sig_json,
                                                      "status": "processing",
                                                      "attempts": attempts}
                    self._save_state()
                    try:
                        details = self._process(path, signature)
                    except Exception as exc:
                        log.exception("could not process folder media %s", path.name)
                        self._state["files"][key].update(
                            status="failed", error=str(exc), failed_at=now,
                            next_retry=now + self.retry_seconds)
                        self._save_state()
                        result["failed"] += 1
                        continue
                    self._state["files"][key].update(
                        status="processed", processed_at=now, result=details)
                    self._state["files"][key].pop("error", None)
                    self._state["files"][key].pop("next_retry", None)
                    self._save_state()
                    result["processed"] += 1
                    result["faces"] += details["distinct_faces"]
                self._finish_scan(result)
                return result
            except Exception as exc:
                self._status["last_error"] = str(exc)
                raise
            finally:
                self._status["scanning"] = False
        finally:
            self._lock.release()

    def _process(self, path: Path, signature: tuple[int, int]) -> dict:
        t0 = time.time()
        candidates, stats = scan_media(
            self.engine, path, max_frames=self.max_frames, min_face_px=self.min_face_px,
            min_det=self.min_det, same_person_similarity=self.same_person_similarity,
            max_people=self.max_people)
        base = self._event_base(path, signature)
        event_ts = signature[1] / 1_000_000_000
        results = []
        for index, candidate in enumerate(candidates, 1):
            results.append(self.processor.process_local_face(
                f"{base}-{index}", self.camera, event_ts, candidate["frame"],
                candidate["face"], str(path)))
        stats.update(seconds=round(time.time() - t0, 3), results=results)
        log.info("folder media %s: %d sampled frames, %d detections, %d distinct face(s), %.1fs",
                 path.name, stats["frames"], stats["detections"],
                 stats["distinct_faces"], stats["seconds"])
        return stats

    def set_index_cap(self, value: int) -> bool:
        """Obergrenze aus dem Einstellungen-Tab uebernehmen.

        Laeuft im HTTP-Thread, waehrend der Poller denselben Zustand schreiben kann.
        Deshalb nur mit der Sperre anfassen — und **nicht blockierend**: ein laufender
        Scan haelt sie ueber seine ganze Dauer, und eine Einstellungsseite, die minutenlang
        haengt, waere die schlechtere Antwort. Klappt es nicht, traegt der naechste
        Laufabschluss die neue Grenze nach.
        """
        self.max_indexed_files = max(0, int(value))
        if not self._lock.acquire(blocking=False):
            log.info("index cap set to %d; a scan is running, it will apply at its end",
                     self.max_indexed_files)
            return False
        try:
            if self._enforce_index_cap():
                self._save_state()
        finally:
            self._lock.release()
        return True

    def status(self) -> dict:
        # A scan deliberately holds _lock for its whole run to prevent a UI-triggered
        # second scan. Status must not take that lock: otherwise /api/health would hang
        # for the full inference time precisely while the service is busiest.
        files = dict(self._state.get("files", {}))
        counts = {name: sum(1 for item in files.values() if item.get("status") == name)
                  for name in ("processed", "failed", "processing", "skipped")}
        return dict(self._status, indexed=len(files),
                    index_cap=self.max_indexed_files, **counts)
