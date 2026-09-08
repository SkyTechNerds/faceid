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
                return raw
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            log.warning("folder state unreadable; starting with an empty index")
        return {"version": 1, "files": {}}

    def _save_state(self):
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

    def scan_once(self, now: float | None = None) -> dict:
        """Discover and synchronously process stable files; safe to call from tests/UI."""
        now = time.time() if now is None else now
        with self._lock:
            self._status.update(scanning=True, last_scan=now, last_error="")
            result = {"found": 0, "stable": 0, "processed": 0, "failed": 0,
                      "faces": 0, "skipped": 0}
            try:
                if not self.path.is_dir():
                    raise FileNotFoundError(f"watch folder is unavailable: {self.path}")
                files = self._files()
                result["found"] = len(files)
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
                    self._status["last_result"] = result
                    return result
                for path in files:
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
                self._status["last_result"] = result
                return result
            except Exception as exc:
                self._status["last_error"] = str(exc)
                raise
            finally:
                self._status["scanning"] = False

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

    def status(self) -> dict:
        # A scan deliberately holds _lock for its whole run to prevent a UI-triggered
        # second scan. Status must not take that lock: otherwise /api/health would hang
        # for the full inference time precisely while the service is busiest.
        files = dict(self._state.get("files", {}))
        counts = {name: sum(1 for item in files.values() if item.get("status") == name)
                  for name in ("processed", "failed", "processing", "skipped")}
        return dict(self._status, indexed=len(files), **counts)
