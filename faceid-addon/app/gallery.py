"""Personen-Galerie: pro Person ein Ordner mit Gesichts-Crops + Embedding-Matrix.

Matching per Cosine-Similarity (Embeddings sind L2-normiert -> Dot-Product).
Kein Training, kein Overfitting: jedes Bild ist ein eigener Vergleichspunkt.
"""
import json
import re
import shutil
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
        self.trimmed_keep = 10  # wie viele beiseitegelegte Fotos je Person aufgehoben werden
        self._lock = threading.Lock()
        self._cache = {}  # slug -> {"name":..., "emb": np.ndarray, "files": [...]}
        self._ign_emb = np.zeros((0, 512), dtype=np.float32)
        self._ign_ids: list[str] = []
        self._ign_groups: list[str] = []
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
                files, changed = self._repair_person(pdir, meta, emb)
                self._cache[pdir.name] = {
                    "name": meta.get("name", pdir.name),
                    "emb": emb,
                    "files": files,
                    "favorite": bool(meta.get("favorite", False)),
                }
                if changed:
                    meta_f.write_text(json.dumps(
                        {"name": meta.get("name", pdir.name), "files": files,
                         "favorite": bool(meta.get("favorite", False))},
                        ensure_ascii=False, indent=1))
            embs, ids, groups = [], [], []
            for jf in sorted(self.ignored_dir.glob("*.json")):
                try:
                    m = json.loads(jf.read_text())
                except (json.JSONDecodeError, OSError):
                    continue
                embs.append(m["embedding"])
                ids.append(jf.stem)
                groups.append(m.get("group"))
            self._ign_emb = np.array(embs, dtype=np.float32) if embs else np.zeros((0, 512), dtype=np.float32)
            self._ign_ids = ids
            # Migration: Anker ohne Gruppe greedy zuordnen (ähnlich -> selbe Gruppe)
            for i, grp in enumerate(groups):
                if grp:
                    continue
                sims = self._ign_emb @ self._ign_emb[i]
                cand = [(float(sims[j]), groups[j]) for j in range(len(ids)) if j != i and groups[j]]
                best = max(cand, default=(0.0, None))
                groups[i] = best[1] if best[0] >= 0.5 else f"g{ids[i]}"
                self._rewrite_ignored_meta(ids[i], {"group": groups[i]})
            self._ign_groups = groups

    def _repair_person(self, pdir: Path, meta: dict, emb: "np.ndarray"):
        """Alt-Daten (vor v0.2.1) heilen: doppelte/fehlende Dateinamen 1:1 zu Embeddings
        machen. Embeddings bleiben unangetastet (Erkennung), nur die JPG/Namen werden
        konsistent. Gibt (files, changed) zurück."""
        files = list(meta.get("files", []))
        n = int(emb.shape[0])
        changed = False
        # Länge an Embeddings angleichen (Guard; sollte selten nötig sein)
        if len(files) < n:
            files += [f"missing_{i}.jpg" for i in range(len(files), n)]
            changed = True
        elif len(files) > n:
            files = files[:n]
            changed = True
        # Dateinamen eindeutig machen; für Kollisionen vorhandenes JPG kopieren
        seen, out = set(), []
        for i, fn in enumerate(files):
            src = pdir / fn
            if fn in seen or not src.exists():
                stem = Path(fn).stem.split("_dup")[0]
                new = f"{stem}_dup{i}.jpg"
                while new in seen or (pdir / new).exists():
                    new = f"{stem}_dup{i}_{len(seen)}.jpg"
                # Bildquelle: das (überlebende) JPG dieses Namens, sonst irgendein vorhandenes
                real = src if src.exists() else next((pdir / o for o in files if (pdir / o).exists()), None)
                if real is not None and real.exists():
                    shutil.copyfile(real, pdir / new)
                fn = new
                changed = True
            seen.add(fn)
            out.append(fn)
        return out, changed

    def _trimmed_dir(self, slug: str) -> Path:
        d = self.persons_dir / slug / "_trimmed"
        d.mkdir(exist_ok=True)
        return d

    def _trim_face(self, slug: str, fname: str, emb, mean_sim: float, reason: str = "over the per-person photo limit — most similar to your other photos"):
        td = self._trimmed_dir(slug)
        src = self.persons_dir / slug / fname
        if src.exists():
            src.rename(td / fname)
        log_f = td / "log.json"
        try:
            log = json.loads(log_f.read_text()) if log_f.exists() else []
        except (json.JSONDecodeError, OSError):
            log = []
        log.insert(0, {"file": fname, "ts": time.time(), "mean_sim": round(mean_sim, 3),
                       "reason": reason,
                       "embedding": [round(float(v), 6) for v in emb]})
        # Getrimmt-Ordner begrenzen: nur die neuesten trimmed_keep aufheben, Rest löschen
        keep = self.trimmed_keep if self.trimmed_keep and self.trimmed_keep > 0 else len(log)
        for old in log[keep:]:
            (td / old["file"]).unlink(missing_ok=True)
        log = log[:keep]
        log_f.write_text(json.dumps(log, ensure_ascii=False))

    def trimmed(self, slug: str):
        td = self.persons_dir / slug / "_trimmed"
        log_f = td / "log.json"
        if not log_f.exists():
            return []
        try:
            log = json.loads(log_f.read_text())
        except (json.JSONDecodeError, OSError):
            return []
        entry = self._cache.get(slug)
        act_emb = entry["emb"] if entry else np.zeros((0, 512), dtype=np.float32)
        act_files = entry["files"] if entry else []
        out = []
        for e in log:
            if not (td / e["file"]).exists():
                continue
            similar = []
            emb = e.get("embedding")
            if emb and len(act_files):
                sims = act_emb @ np.array(emb, dtype=np.float32)
                # aktive Fotos, die diesem getrimmten ähneln (Cosine >= 0.45), stärkste zuerst
                idx = [i for i in np.argsort(-sims) if sims[i] >= 0.45][:12]
                similar = [act_files[i] for i in idx]
            out.append({"file": e["file"], "ts": e.get("ts", 0), "mean_sim": e.get("mean_sim"),
                        "reason": e.get("reason", ""), "similar": similar})
        return out

    def restore_trimmed(self, slug: str, fname: str) -> bool:
        """Getrimmtes Foto zurück in die Galerie holen (Cap wird dabei NICHT erzwungen)."""
        with self._lock:
            entry = self._cache.get(slug)
            td = self.persons_dir / slug / "_trimmed"
            log_f = td / "log.json"
            if entry is None or not log_f.exists():
                return False
            log = json.loads(log_f.read_text())
            rec = next((e for e in log if e["file"] == fname), None)
            if rec is None or not (td / fname).exists():
                return False
            (td / fname).rename(self.persons_dir / slug / fname)
            entry["emb"] = np.vstack([entry["emb"], np.array(rec["embedding"], dtype=np.float32)[None, :]])
            entry["files"].append(fname)
            log = [e for e in log if e["file"] != fname]
            log_f.write_text(json.dumps(log, ensure_ascii=False))
            self._persist(slug)
            return True

    def delete_trimmed(self, slug: str, fname: str):
        td = self.persons_dir / slug / "_trimmed"
        log_f = td / "log.json"
        (td / fname).unlink(missing_ok=True)
        if log_f.exists():
            try:
                log = [e for e in json.loads(log_f.read_text()) if e["file"] != fname]
                log_f.write_text(json.dumps(log, ensure_ascii=False))
            except (json.JSONDecodeError, OSError):
                pass

    def clear_trimmed(self, slug: str) -> int:
        td = self.persons_dir / slug / "_trimmed"
        if not td.exists():
            return 0
        n = 0
        for f in td.glob("*.jpg"):
            f.unlink(missing_ok=True); n += 1
        (td / "log.json").unlink(missing_ok=True)
        return n

    def _persist(self, slug: str):
        pdir = self.persons_dir / slug
        entry = self._cache[slug]
        np.save(pdir / "embeddings.npy", entry["emb"])
        (pdir / "meta.json").write_text(
            json.dumps({"name": entry["name"], "files": entry["files"],
                        "favorite": bool(entry.get("favorite", False))},
                       ensure_ascii=False, indent=1)
        )

    # ---------- Personen ----------

    def persons(self):
        with self._lock:
            return {
                slug: {"name": e["name"], "count": len(e["files"]), "files": list(e["files"]),
                       "favorite": bool(e.get("favorite", False)),
                       "trimmed": self.trimmed(slug)}
                for slug, e in self._cache.items()
            }

    def set_favorite(self, slug: str, fav: bool) -> bool:
        with self._lock:
            entry = self._cache.get(slug)
            if entry is None:
                return False
            entry["favorite"] = bool(fav)
            self._persist(slug)
            return True

    def create_person(self, name: str) -> str:
        slug = slugify(name)
        with self._lock:
            pdir = self.persons_dir / slug
            pdir.mkdir(exist_ok=True)
            if slug not in self._cache:
                self._cache[slug] = {"name": name, "emb": np.zeros((0, 512), dtype=np.float32), "files": [], "favorite": False}
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
                # redundanteste Referenz aussortieren (höchste mittlere Ähnlichkeit zu den
                # übrigen = Dublette). Nicht löschen, sondern nach _trimmed/ verschieben,
                # damit der User in der UI sieht WAS ging und es zurückholen kann.
                sims = entry["emb"] @ entry["emb"].T
                np.fill_diagonal(sims, 0.0)
                mean_sim = sims.mean(axis=1)
                drop = int(np.argmax(mean_sim))
                self._trim_face(slug, entry["files"][drop], entry["emb"][drop], float(mean_sim[drop]))
                entry["files"].pop(drop)
                entry["emb"] = np.delete(entry["emb"], drop, axis=0)
            self._persist(slug)
            return fname

    def enforce_cap_all(self) -> int:
        """Alle Personen sofort auf max_per_person trimmen (z. B. nach Cap-Senkung im
        Settings-Tab). Ausgemusterte Fotos wandern nach _trimmed. Gibt Anzahl zurück."""
        if not self.max_per_person:
            return 0
        total = 0
        with self._lock:
            for slug, entry in self._cache.items():
                while len(entry["files"]) > self.max_per_person:
                    sims = entry["emb"] @ entry["emb"].T
                    np.fill_diagonal(sims, 0.0)
                    ms = sims.mean(axis=1)
                    drop = int(np.argmax(ms))
                    self._trim_face(slug, entry["files"][drop], entry["emb"][drop], float(ms[drop]))
                    entry["files"].pop(drop)
                    entry["emb"] = np.delete(entry["emb"], drop, axis=0)
                    total += 1
                self._persist(slug)
        return total

    def deduplicate_person(self, slug: str, threshold: float = 0.90) -> int:
        """Nahezu identische Fotos (Cosine >= threshold) beiseitelegen — sie bringen der
        Erkennung nichts. Von jedem zu ähnlichen Paar geht das redundantere (höhere
        mittlere Ähnlichkeit zum Rest). Läuft, bis kein Paar mehr über der Schwelle liegt."""
        moved = 0
        with self._lock:
            entry = self._cache.get(slug)
            if entry is None:
                return 0
            while len(entry["files"]) > 1:
                sims = entry["emb"] @ entry["emb"].T
                np.fill_diagonal(sims, 0.0)
                i, jx = np.unravel_index(int(np.argmax(sims)), sims.shape)
                if sims[i, jx] < threshold:
                    break
                mean = sims.mean(axis=1)
                drop = i if mean[i] >= mean[jx] else jx
                self._trim_face(slug, entry["files"][drop], entry["emb"][drop], float(mean[drop]),
                                reason="near-duplicate of another photo — no added value for recognition")
                entry["files"].pop(drop)
                entry["emb"] = np.delete(entry["emb"], drop, axis=0)
                moved += 1
            if moved:
                self._persist(slug)
        return moved

    def deduplicate_all(self, threshold: float = 0.90) -> int:
        return sum(self.deduplicate_person(s, threshold) for s in list(self._cache.keys()))

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

    def _rewrite_ignored_meta(self, iid: str, updates: dict):
        jf = self.ignored_dir / f"{iid}.json"
        try:
            m = json.loads(jf.read_text())
        except (json.JSONDecodeError, OSError):
            return
        m.update(updates)
        jf.write_text(json.dumps(m, ensure_ascii=False))

    def set_ignored_group(self, ids: list, group: str) -> int:
        """Anker in eine andere Gruppe verschieben / Gruppen zusammenlegen."""
        with self._lock:
            n = 0
            for iid in ids:
                if iid in self._ign_ids:
                    self._ign_groups[self._ign_ids.index(iid)] = group
                    self._rewrite_ignored_meta(iid, {"group": group})
                    n += 1
            return n

    def assign_ignored(self, ids: list, slug: str) -> int:
        """Anker als Referenzbilder einer echten Person übernehmen (z. B. falsch Ignorierte)."""
        n = 0
        for iid in ids:
            jf = self.ignored_dir / f"{iid}.json"
            img_f = self.ignored_dir / f"{iid}.jpg"
            if not jf.exists() or not img_f.exists():
                continue
            meta = json.loads(jf.read_text())
            crop = cv2.imread(str(img_f))
            if crop is None:
                continue
            self.add_face(slug, crop, np.array(meta["embedding"], dtype=np.float32))
            self.delete_ignored(iid)
            n += 1
        return n

    def match_ignored(self, embedding: np.ndarray) -> float:
        """Höchste Ähnlichkeit zu einem ignorierten Gesicht (0.0 wenn Liste leer)."""
        with self._lock:
            if len(self._ign_ids) == 0:
                return 0.0
            return float(np.max(self._ign_emb @ embedding))

    def ignore_unknown(self, uid: str, group: str | None = None) -> bool:
        """Unknown in die Ignore-Liste verschieben: nie mehr melden/zuordnen/vorlegen."""
        with self._lock:
            jf = self.unknown_dir / f"{uid}.json"
            img = self.unknown_dir / f"{uid}.jpg"
            if not jf.exists() or not img.exists():
                return False
            meta = json.loads(jf.read_text())
            iid = f"i{uid.lstrip('ui')}"
            img.rename(self.ignored_dir / f"{iid}.jpg")
            grp = group or f"g{iid}"
            payload = {k: v for k, v in meta.items() if k in ("camera", "ts", "embedding")}
            payload["group"] = grp
            (self.ignored_dir / f"{iid}.json").write_text(json.dumps(payload, ensure_ascii=False))
            jf.unlink()
            (self.unknown_dir / f"{uid}_full.jpg").unlink(missing_ok=True)
            self._ign_emb = np.vstack([self._ign_emb, np.array(meta["embedding"], dtype=np.float32)[None, :]])
            self._ign_ids.append(iid)
            self._ign_groups.append(grp)
            return True

    def ignore_person(self, slug: str) -> int:
        """Ganze Person in die Ignore-Liste überführen (alle Bilder werden Negativ-Anker)."""
        with self._lock:
            entry = self._cache.pop(slug, None)
            if entry is None:
                return 0
            n = 0
            grp = f"g{int(time.time() * 1000)}"
            for fname, emb in zip(list(entry["files"]), entry["emb"]):
                iid = f"i{int(time.time() * 1000)}_{n}"
                src = self.persons_dir / slug / fname
                if not src.exists():
                    continue
                src.rename(self.ignored_dir / f"{iid}.jpg")
                (self.ignored_dir / f"{iid}.json").write_text(json.dumps(
                    {"camera": "", "ts": time.time(), "from_person": entry["name"], "group": grp,
                     "embedding": [round(float(v), 6) for v in emb]}, ensure_ascii=False))
                self._ign_emb = np.vstack([self._ign_emb, np.array(emb, dtype=np.float32)[None, :]])
                self._ign_ids.append(iid)
                self._ign_groups.append(grp)
                n += 1
            for f in (self.persons_dir / slug).iterdir():
                f.unlink()
            (self.persons_dir / slug).rmdir()
            return n

    def add_ignore_anchor(self, crop_bgr: np.ndarray, embedding: np.ndarray, novelty_max: float = 0.8):
        """Bestätigten Ignore-Match als zusätzlichen Anker lernen — aber nur, wenn er eine
        neue Erscheinungsform abdeckt (nicht fast identisch zu einem bestehenden Anker)."""
        with self._lock:
            if len(self._ign_ids) == 0:
                return None
            sims = self._ign_emb @ embedding
            if float(np.max(sims)) >= novelty_max:
                return None
            grp = self._ign_groups[int(np.argmax(sims))]  # lernt in die Gruppe des besten Ankers
            iid = f"i{int(time.time() * 1000)}_{len(self._ign_ids)}"
            cv2.imwrite(str(self.ignored_dir / f"{iid}.jpg"), crop_bgr, [cv2.IMWRITE_JPEG_QUALITY, 92])
            (self.ignored_dir / f"{iid}.json").write_text(json.dumps(
                {"camera": "", "ts": time.time(), "auto": True, "group": grp,
                 "embedding": [round(float(v), 6) for v in embedding]}, ensure_ascii=False))
            self._ign_emb = np.vstack([self._ign_emb, embedding.astype(np.float32)[None, :]])
            self._ign_ids.append(iid)
            self._ign_groups.append(grp)
            return iid

    def ignored(self):
        out = []
        for jf in sorted(self.ignored_dir.glob("*.json"), reverse=True):
            try:
                m = json.loads(jf.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            out.append({"id": jf.stem, "camera": m.get("camera", ""), "ts": m.get("ts", 0),
                        "auto": bool(m.get("auto"))})
        return out

    def ignored_clusters(self, eps: float = 0.45):
        """Ignore-Anker nach ihrer persistenten Gruppe bündeln (vom User kuratierbar;
        Auto-Anker lernen in die Gruppe ihres besten Matches)."""
        clusters: dict[str, list] = {}
        for jf in sorted(self.ignored_dir.glob("*.json"), reverse=True):
            try:
                m = json.loads(jf.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            grp = m.get("group") or f"g{jf.stem}"
            clusters.setdefault(grp, []).append(
                {"id": jf.stem, "ts": m.get("ts", 0), "auto": bool(m.get("auto")),
                 "from_person": m.get("from_person", ""), "group": grp})
        return sorted(clusters.values(), key=len, reverse=True)

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
            self._ign_groups.pop(idx)
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
