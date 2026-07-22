"""Personen-Galerie: pro Person ein Ordner mit Gesichts-Crops + Embedding-Matrix.

Matching per Cosine-Similarity (Embeddings sind L2-normiert -> Dot-Product).
Kein Training, kein Overfitting: jedes Bild ist ein eigener Vergleichspunkt.
"""
import json
import re
import threading
import time
from pathlib import Path

import cv2
import numpy as np


def slugify(name: str) -> str:
    s = name.strip().lower()
    for a, b in [("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")]:
        s = s.replace(a, b)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "person"


class Gallery:
    def __init__(self, data_dir: Path, top_k: int = 3, max_per_person: int = 40):
        self.persons_dir = data_dir / "persons"
        self.unknown_dir = data_dir / "unknowns"
        self.ignored_dir = data_dir / "ignored"
        self.persons_dir.mkdir(parents=True, exist_ok=True)
        self.unknown_dir.mkdir(parents=True, exist_ok=True)
        self.ignored_dir.mkdir(parents=True, exist_ok=True)
        self.top_k = max(1, int(top_k))
        self.max_per_person = int(max_per_person)  # 0 = unbegrenzt
        self._lock = threading.Lock()
        self._cache = {}  # slug -> {"name":..., "emb": np.ndarray, "files": [...]}
        self._ign_emb = np.zeros((0, 512), dtype=np.float32)
        self._ign_ids: list[str] = []
        self.reload()

    # ---------- Laden / Speichern ----------

    def reload(self):
        with self._lock:
            self._cache = {}
            for pdir in sorted(self.persons_dir.iterdir()):
                if not pdir.is_dir():
                    continue
                meta_f = pdir / "meta.json"
                emb_f = pdir / "embeddings.npy"
                if not meta_f.exists() or not emb_f.exists():
                    continue
                meta = json.loads(meta_f.read_text())
                emb = np.load(emb_f)
                self._cache[pdir.name] = {
                    "name": meta.get("name", pdir.name),
                    "emb": emb,
                    "files": meta.get("files", []),
                }
            embs, ids = [], []
            for jf in sorted(self.ignored_dir.glob("*.json")):
                try:
                    m = json.loads(jf.read_text())
                except (json.JSONDecodeError, OSError):
                    continue
                embs.append(m["embedding"])
                ids.append(jf.stem)
            self._ign_emb = np.array(embs, dtype=np.float32) if embs else np.zeros((0, 512), dtype=np.float32)
            self._ign_ids = ids

    def _persist(self, slug: str):
        pdir = self.persons_dir / slug
        entry = self._cache[slug]
        np.save(pdir / "embeddings.npy", entry["emb"])
        (pdir / "meta.json").write_text(
            json.dumps({"name": entry["name"], "files": entry["files"]}, ensure_ascii=False, indent=1)
        )

    # ---------- Personen ----------

    def persons(self):
        with self._lock:
            return {
                slug: {"name": e["name"], "count": len(e["files"]), "files": list(e["files"])}
                for slug, e in self._cache.items()
            }

    def create_person(self, name: str) -> str:
        slug = slugify(name)
        with self._lock:
            pdir = self.persons_dir / slug
            pdir.mkdir(exist_ok=True)
            if slug not in self._cache:
                self._cache[slug] = {"name": name, "emb": np.zeros((0, 512), dtype=np.float32), "files": []}
                self._persist(slug)
        return slug

    def add_face(self, slug: str, crop_bgr: np.ndarray, embedding: np.ndarray) -> str:
        """Gesichts-Crop + Embedding einer Person hinzufügen."""
        with self._lock:
            if slug not in self._cache:
                raise KeyError(slug)
            entry = self._cache[slug]
            fname = f"{int(time.time() * 1000)}_{len(entry['files'])}.jpg"  # Suffix gegen ms-Kollisionen
            cv2.imwrite(str(self.persons_dir / slug / fname), crop_bgr, [cv2.IMWRITE_JPEG_QUALITY, 92])
            entry["emb"] = np.vstack([entry["emb"], embedding.astype(np.float32)[None, :]])
            entry["files"].append(fname)
            if self.max_per_person and len(entry["files"]) > self.max_per_person:
                # redundanteste Referenz entfernen (höchste mittlere Ähnlichkeit zu den übrigen)
                sims = entry["emb"] @ entry["emb"].T
                np.fill_diagonal(sims, 0.0)
                drop = int(np.argmax(sims.mean(axis=1)))
                (self.persons_dir / slug / entry["files"][drop]).unlink(missing_ok=True)
                entry["files"].pop(drop)
                entry["emb"] = np.delete(entry["emb"], drop, axis=0)
            self._persist(slug)
            return fname

    def delete_face(self, slug: str, fname: str):
        with self._lock:
            entry = self._cache[slug]
            if fname not in entry["files"]:
                return
            idx = entry["files"].index(fname)
            entry["files"].pop(idx)
            entry["emb"] = np.delete(entry["emb"], idx, axis=0)
            (self.persons_dir / slug / fname).unlink(missing_ok=True)
            self._persist(slug)

    def unassign_face(self, slug: str, fname: str) -> bool:
        """Gesicht aus einer Person entfernen und zurück in die Unknown-Queue legen."""
        with self._lock:
            entry = self._cache.get(slug)
            if entry is None or fname not in entry["files"]:
                return False
            idx = entry["files"].index(fname)
            emb = entry["emb"][idx]
            uid = f"u{int(time.time() * 1000)}"
            (self.persons_dir / slug / fname).rename(self.unknown_dir / f"{uid}.jpg")
            (self.unknown_dir / f"{uid}.json").write_text(json.dumps(
                {"camera": "", "event_id": "", "removed_from": entry["name"], "ts": time.time(),
                 "embedding": [round(float(v), 6) for v in emb]}, ensure_ascii=False))
            entry["files"].pop(idx)
            entry["emb"] = np.delete(entry["emb"], idx, axis=0)
            self._persist(slug)
            return True

    def delete_person(self, slug: str):
        with self._lock:
            entry = self._cache.pop(slug, None)
            if entry is None:
                return
            pdir = self.persons_dir / slug
            for f in pdir.iterdir():
                f.unlink()
            pdir.rmdir()

    # ---------- Matching ----------

    def match(self, embedding: np.ndarray):
        """-> (slug, name, score) der besten Person oder (None, None, best_score).
        Score = Mittel der Top-k Ähnlichkeiten pro Person (statt Max) — eine Person
        mit vielen Referenzbildern gewinnt Grenzfälle nicht mehr per Einzel-Ausreißer."""
        with self._lock:
            best = (None, None, 0.0)
            for slug, e in self._cache.items():
                if len(e["files"]) == 0:
                    continue
                sims = e["emb"] @ embedding
                k = min(self.top_k, len(sims))
                score = float(np.sort(sims)[-k:].mean())
                if score > best[2]:
                    best = (slug, e["name"], score)
            return best

    # ---------- Ignore-Liste (Negativ-Anker) ----------

    def match_ignored(self, embedding: np.ndarray) -> float:
        """Höchste Ähnlichkeit zu einem ignorierten Gesicht (0.0 wenn Liste leer)."""
        with self._lock:
            if len(self._ign_ids) == 0:
                return 0.0
            return float(np.max(self._ign_emb @ embedding))

    def ignore_unknown(self, uid: str) -> bool:
        """Unknown in die Ignore-Liste verschieben: nie mehr melden/zuordnen/vorlegen."""
        with self._lock:
            jf = self.unknown_dir / f"{uid}.json"
            img = self.unknown_dir / f"{uid}.jpg"
            if not jf.exists() or not img.exists():
                return False
            meta = json.loads(jf.read_text())
            iid = f"i{uid.lstrip('ui')}"
            img.rename(self.ignored_dir / f"{iid}.jpg")
            (self.ignored_dir / f"{iid}.json").write_text(json.dumps(
                {k: v for k, v in meta.items() if k in ("camera", "ts", "embedding")},
                ensure_ascii=False))
            jf.unlink()
            (self.unknown_dir / f"{uid}_full.jpg").unlink(missing_ok=True)
            self._ign_emb = np.vstack([self._ign_emb, np.array(meta["embedding"], dtype=np.float32)[None, :]])
            self._ign_ids.append(iid)
            return True

    def ignore_person(self, slug: str) -> int:
        """Ganze Person in die Ignore-Liste überführen (alle Bilder werden Negativ-Anker)."""
        with self._lock:
            entry = self._cache.pop(slug, None)
            if entry is None:
                return 0
            n = 0
            for fname, emb in zip(list(entry["files"]), entry["emb"]):
                iid = f"i{int(time.time() * 1000)}_{n}"
                src = self.persons_dir / slug / fname
                if not src.exists():
                    continue
                src.rename(self.ignored_dir / f"{iid}.jpg")
                (self.ignored_dir / f"{iid}.json").write_text(json.dumps(
                    {"camera": "", "ts": time.time(), "from_person": entry["name"],
                     "embedding": [round(float(v), 6) for v in emb]}, ensure_ascii=False))
                self._ign_emb = np.vstack([self._ign_emb, np.array(emb, dtype=np.float32)[None, :]])
                self._ign_ids.append(iid)
                n += 1
            for f in (self.persons_dir / slug).iterdir():
                f.unlink()
            (self.persons_dir / slug).rmdir()
            return n

    def ignored(self):
        out = []
        for jf in sorted(self.ignored_dir.glob("*.json"), reverse=True):
            try:
                m = json.loads(jf.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            out.append({"id": jf.stem, "camera": m.get("camera", ""), "ts": m.get("ts", 0)})
        return out

    def restore_ignored(self, iid: str) -> bool:
        """Ignoriertes Gesicht zurück in die Review-Queue."""
        with self._lock:
            jf = self.ignored_dir / f"{iid}.json"
            img = self.ignored_dir / f"{iid}.jpg"
            if not jf.exists() or not img.exists():
                return False
            meta = json.loads(jf.read_text())
            uid = f"u{int(time.time() * 1000)}"
            img.rename(self.unknown_dir / f"{uid}.jpg")
            meta.update(ts=time.time(), event_id="", restored=True)
            (self.unknown_dir / f"{uid}.json").write_text(json.dumps(meta, ensure_ascii=False))
            jf.unlink()
            self._drop_ignored(iid)
            return True

    def delete_ignored(self, iid: str):
        with self._lock:
            (self.ignored_dir / f"{iid}.json").unlink(missing_ok=True)
            (self.ignored_dir / f"{iid}.jpg").unlink(missing_ok=True)
            self._drop_ignored(iid)

    def _drop_ignored(self, iid: str):
        if iid in self._ign_ids:
            idx = self._ign_ids.index(iid)
            self._ign_ids.pop(idx)
            self._ign_emb = np.delete(self._ign_emb, idx, axis=0)

    # ---------- Unbekannte ----------

    def save_unknown(self, crop_bgr: np.ndarray, embedding: np.ndarray, meta: dict,
                     dedupe_sim: float = 0.75, full_bgr: np.ndarray | None = None):
        """Unbekanntes Gesicht ablegen; sehr ähnliche jüngste Unknowns werden übersprungen."""
        with self._lock:
            now = time.time()
            for jf in self.unknown_dir.glob("*.json"):
                try:
                    m = json.loads(jf.read_text())
                except (json.JSONDecodeError, OSError):
                    continue
                if now - m.get("ts", 0) < 3600:
                    sim = float(np.dot(np.array(m["embedding"], dtype=np.float32), embedding))
                    if sim > dedupe_sim:
                        return None
            uid = f"u{int(now * 1000)}"
            cv2.imwrite(str(self.unknown_dir / f"{uid}.jpg"), crop_bgr, [cv2.IMWRITE_JPEG_QUALITY, 92])
            if full_bgr is not None:
                cv2.imwrite(str(self.unknown_dir / f"{uid}_full.jpg"), full_bgr, [cv2.IMWRITE_JPEG_QUALITY, 85])
            meta = dict(meta, ts=now, embedding=[round(float(v), 6) for v in embedding])
            (self.unknown_dir / f"{uid}.json").write_text(json.dumps(meta, ensure_ascii=False))
            return uid

    def unknowns(self):
        out = []
        for jf in sorted(self.unknown_dir.glob("*.json"), reverse=True):
            try:
                m = json.loads(jf.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            out.append({"id": jf.stem, **{k: v for k, v in m.items() if k != "embedding"},
                        "has_full": (self.unknown_dir / f"{jf.stem}_full.jpg").exists(),
                        "embedding": np.array(m["embedding"], dtype=np.float32)})
        return out

    def unknown_clusters(self, eps: float = 0.45):
        """Unknowns per DBSCAN über Cosine-Distanz gruppieren (der Immich-Trick)."""
        items = self.unknowns()
        if not items:
            return []
        from sklearn.cluster import DBSCAN

        X = np.stack([it["embedding"] for it in items])
        labels = DBSCAN(eps=eps, min_samples=1, metric="cosine").fit(X).labels_
        clusters = {}
        for it, lb in zip(items, labels):
            it.pop("embedding")
            clusters.setdefault(int(lb), []).append(it)
        return sorted(clusters.values(), key=len, reverse=True)

    def assign_unknown(self, uid: str, slug: str):
        jf = self.unknown_dir / f"{uid}.json"
        img_f = self.unknown_dir / f"{uid}.jpg"
        if not jf.exists() or not img_f.exists():
            return False
        meta = json.loads(jf.read_text())
        crop = cv2.imread(str(img_f))
        self.add_face(slug, crop, np.array(meta["embedding"], dtype=np.float32))
        jf.unlink()
        img_f.unlink()
        (self.unknown_dir / f"{uid}_full.jpg").unlink(missing_ok=True)
        return True

    def refresh_guesses(self):
        """Verbleibende Unknowns gegen die aktuelle Galerie neu bewerten (nach Zuordnungen)."""
        for jf in self.unknown_dir.glob("*.json"):
            try:
                m = json.loads(jf.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            _, name, score = self.match(np.array(m["embedding"], dtype=np.float32))
            m["guess"], m["guess_score"] = name, round(float(score), 3)
            jf.write_text(json.dumps(m, ensure_ascii=False))

    def discard_unknown(self, uid: str):
        (self.unknown_dir / f"{uid}.json").unlink(missing_ok=True)
        (self.unknown_dir / f"{uid}.jpg").unlink(missing_ok=True)
        (self.unknown_dir / f"{uid}_full.jpg").unlink(missing_ok=True)
