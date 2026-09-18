"""Personen-Galerie: pro Person ein Ordner mit Gesichts-Crops + Embedding-Matrix.

Matching per Cosine-Similarity (Embeddings sind L2-normiert -> Dot-Product).
Kein Training, kein Overfitting: jedes Bild ist ein eigener Vergleichspunkt.
"""
import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
import unicodedata
import uuid
from pathlib import Path

import cv2
import numpy as np
from .nas_io import (
    atomic_write_bytes_pinned, atomic_write_text_pinned, unlink_pinned,
    rename_pinned, copy_file_pinned, remove_tree_pinned, copy_tree_pinned,
    mkdir_pinned,
)

log = logging.getLogger("faceid.gallery")


def slugify(name: str) -> str:
    normalized = unicodedata.normalize("NFKC", name).strip().casefold()
    s = normalized
    for a, b in [("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")]:
        s = s.replace(a, b)
    # Strip accents from Latin characters while keeping the on-disk identifier ASCII.
    # Scripts without an ASCII representation get a stable,
    # name-derived suffix instead of all collapsing to the literal directory "person".
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    if s:
        return s
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:10]
    return f"person-{digest}"


def _same_name(a: str, b: str) -> bool:
    """Compare display names without treating Unicode composition or case as distinct."""
    return (
        unicodedata.normalize("NFKC", a).strip().casefold()
        == unicodedata.normalize("NFKC", b).strip().casefold()
    )


class Gallery:
    def __init__(self, data_dir: Path, top_k: int = 3, max_per_person: int = 40,
                 storage_guard=None):
        self.data_dir = Path(data_dir)
        if storage_guard is not None:
            storage_guard(data=data_dir)
        self.persons_dir = self.data_dir / "persons"
        self.unknown_dir = self.data_dir / "unknowns"
        self.ignored_dir = self.data_dir / "ignored"
        self.anonymous_groups_path = self.data_dir / "anonymous-groups.json"
        self.storage_guard = storage_guard
        self.last_save_status = "none"
        self._mkdir_nas(self.persons_dir)
        self._mkdir_nas(self.unknown_dir)
        self._mkdir_nas(self.ignored_dir)
        self.top_k = max(1, int(top_k))
        # 0 = unbegrenzt; begrenzt nur automatisch gelernte Anker je Gruppe
        self.max_ignore_anchors = 0
        self.max_per_person = int(max_per_person)  # 0 = unbegrenzt
        self.trimmed_keep = 10  # wie viele beiseitegelegte Fotos je Person aufgehoben werden
        # Ab welcher Aehnlichkeit zu einer ANDEREN Person gilt eine Referenz als riskant?
        # Wird vom Dienst auf match_threshold - cross_risk_margin gesetzt; 0 = aus.
        self.cross_risk_threshold = 0.0
        # Zweites Kriterium: Wie weit darf eine Referenz hinter dem zurueckbleiben, was
        # bei DIESER Person ueblich ist? Anteil am Median; 0 = aus. Siehe _self_outlier.
        self.self_outlier_ratio = 0.0
        self.self_outlier_min_photos = 5  # darunter ist der Median nicht belastbar
        self.dedupe_threshold = 0.65  # ab hier gilt ein Foto als Duplikat (Hover-Highlight + Dedup)
        # Cross-person rollback reloads the cache while the operation lock is held.
        self._lock = threading.RLock()
        self._cache = {}  # slug -> {"name":..., "emb": np.ndarray, "files": [...]}
        self._ign_emb = np.zeros((0, 512), dtype=np.float32)
        self._ign_ids: list[str] = []
        self._ign_groups: list[str] = []
        self._recover_person_transactions()
        self._recover_pending_commits()
        self._clean_pending_unknowns()
        self.reload()

    def _ensure_storage(self):
        """Run the production mount guard immediately before durable writes."""
        if self.storage_guard is not None:
            self.storage_guard()

    def _mkdir_nas(self, path: Path) -> None:
        mkdir_pinned(path, self.storage_guard)

    def _write_nas_bytes(self, path: Path, data: bytes) -> None:
        """Commit a file through a mount-pinned parent directory."""
        atomic_write_bytes_pinned(path, data, self.storage_guard)

    def _write_nas_text(self, path: Path, text: str) -> None:
        atomic_write_text_pinned(path, text, self.storage_guard)

    def _unlink_nas(self, path: Path) -> None:
        unlink_pinned(path, self.storage_guard)

    def _recover_pending_commits(self):
        """Roll back an interrupted multi-file person commit before loading it.

        Embeddings and metadata are a single logical record but two files.  The
        journal is deliberately tiny and local to the person directory: if a
        process dies between the two replacements, restoring both old files is
        safer than loading a mismatched pair and silently changing recognition.
        """
        if not self.persons_dir.exists():
            return
        for pdir in self.persons_dir.iterdir():
            journal = pdir / ".faceid-commit.json"
            if not journal.is_file():
                continue
            try:
                record = json.loads(journal.read_text(encoding="utf-8"))
                for current, backup in (("embeddings.npy", record.get("embedding_backup")),
                                        ("meta.json", record.get("metadata_backup"))):
                    target = pdir / current
                    old = pdir / str(backup) if backup else None
                    if old is not None and old.exists():
                        rename_pinned(old, target, self.storage_guard)
                    elif record.get("embeddings.npy_existed" if current == "embeddings.npy"
                                    else "meta.json_existed") is False:
                        unlink_pinned(target, self.storage_guard)
                unlink_pinned(journal, self.storage_guard)
                for backup in (record.get("embedding_backup"), record.get("metadata_backup")):
                    if backup:
                        unlink_pinned(pdir / str(backup), self.storage_guard)
            except (OSError, ValueError, TypeError) as exc:
                log.error("could not recover interrupted person commit %s: %s", journal, exc)

    def _person_transaction_path(self, token: str) -> Path:
        return self.data_dir / ".faceid-transactions" / token

    def _write_transaction_journal(self, path: Path, payload: dict) -> None:
        journal = path / "journal.json"
        temporary = journal.with_name(f".{journal.name}.{os.getpid()}.{time.time_ns()}.partial")
        self._write_nas_text(temporary, json.dumps(payload, ensure_ascii=False, indent=1))
        rename_pinned(temporary, journal, self.storage_guard)

    def _begin_person_transaction(self, slugs: list[str], operation: str,
                                  absent_slugs: set[str] | None = None) -> Path:
        """Snapshot all affected person directories before a cross-person move.

        ``absent_slugs`` marks directories created as part of the operation.  They
        must be removed during crash recovery when the process dies after creation
        but before the move is committed; an empty newly-created person is not a
        valid recovery result.
        """
        self._ensure_storage()
        token = f"{operation}-{os.getpid()}-{time.time_ns()}"
        tx = self._person_transaction_path(token)
        backup_root = tx / "persons"
        self._mkdir_nas(backup_root)
        records = []
        absent_slugs = absent_slugs or set()
        for slug in dict.fromkeys(slugs):
            source = self.persons_dir / slug
            existed = source.is_dir()
            backup = backup_root / slug
            if existed:
                copy_tree_pinned(source, backup, self.storage_guard)
            records.append({"slug": slug, "existed": existed,
                            "created_by_operation": slug in absent_slugs})
        self._write_transaction_journal(tx, {"version": 1, "operation": operation,
                                              "state": "prepared", "records": records})
        return tx

    def _finish_person_transaction(self, tx: Path) -> None:
        journal = tx / "journal.json"
        payload = json.loads(journal.read_text(encoding="utf-8"))
        payload["state"] = "committed"
        self._write_transaction_journal(tx, payload)
        remove_tree_pinned(tx, self.storage_guard)

    def _rollback_person_transaction(self, tx: Path) -> None:
        """Restore a failed cross-person operation immediately and remove staging."""
        payload = json.loads((tx / "journal.json").read_text(encoding="utf-8"))
        backup_root = tx / "persons"
        for record in payload.get("records", []):
            slug = str(record["slug"])
            current = self.persons_dir / slug
            remove_tree_pinned(current, self.storage_guard)
            if record.get("created_by_operation"):
                continue
            backup = backup_root / slug
            if record.get("existed") and backup.is_dir():
                copy_tree_pinned(backup, current, self.storage_guard)
        remove_tree_pinned(tx, self.storage_guard)

    def _recover_person_transactions(self):
        root = self.data_dir / ".faceid-transactions"
        if not root.is_dir():
            return
        for tx in sorted(root.iterdir()):
            journal = tx / "journal.json"
            try:
                if not journal.exists():
                    remove_tree_pinned(tx, self.storage_guard)
                    continue
                payload = json.loads(journal.read_text(encoding="utf-8"))
                if payload.get("state") == "committed":
                    remove_tree_pinned(tx, self.storage_guard)
                    continue
                self._ensure_storage()
                backup_root = tx / "persons"
                for record in payload.get("records", []):
                    slug = str(record["slug"])
                    current = self.persons_dir / slug
                    remove_tree_pinned(current, self.storage_guard)
                    if record.get("created_by_operation"):
                        continue
                    backup = backup_root / slug
                    if record.get("existed") and backup.is_dir():
                        copy_tree_pinned(backup, current, self.storage_guard)
                remove_tree_pinned(tx, self.storage_guard)
            except (OSError, ValueError, TypeError, KeyError) as exc:
                log.error("could not recover gallery transaction %s: %s", tx, exc)

    def _clean_pending_unknowns(self):
        """Remove only abandoned temporary unknown transactions, never live crops."""
        if not self.unknown_dir.exists():
            return
        for pending in self.unknown_dir.glob(".*.pending.json"):
            uid = pending.name[1:-len(".pending.json")]
            unlink_pinned(pending, self.storage_guard)
            if (self.unknown_dir / f"{uid}.json").exists():
                # The durable metadata replacement won; only the final marker
                # cleanup was interrupted.
                continue
            unlink_pinned(self.unknown_dir / f"{uid}.jpg", self.storage_guard)
            unlink_pinned(self.unknown_dir / f"{uid}.json", self.storage_guard)
        for partial in self.unknown_dir.glob(".*.partial*"):
            unlink_pinned(partial, self.storage_guard)

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
                meta = json.loads(meta_f.read_text(encoding="utf-8"))
                emb = np.load(emb_f)
                files, changed = self._repair_person(pdir, meta, emb)
                person_uuid = meta.get("person_uuid") or str(uuid.uuid4())
                changed = changed or not meta.get("person_uuid")
                self._cache[pdir.name] = {
                    "name": meta.get("name", pdir.name),
                    "emb": emb,
                    "files": files,
                    "favorite": bool(meta.get("favorite", False)),
                    # Herkunft je Foto (Kamera, Ereigniszeit) — fehlt bei Altbestand
                    "sources": dict(meta.get("sources", {})),
                    "last_seen": self._last_seen_from_meta(meta, pdir),
                    "person_uuid": person_uuid,
                }
                if changed:
                    self._write_nas_text(meta_f, json.dumps(
                        {"name": meta.get("name", pdir.name), "files": files,
                         "favorite": bool(meta.get("favorite", False)),
                         "sources": dict(meta.get("sources", {})),
                         "last_seen": self._last_seen_from_meta(meta, pdir),
                         "person_uuid": person_uuid},
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

    @staticmethod
    def _last_seen_from_meta(meta: dict, pdir: Path) -> float:
        """Return the durable last-observed timestamp, with a safe legacy fallback."""
        try:
            if meta.get("last_seen") is not None:
                return float(meta["last_seen"])
        except (TypeError, ValueError):
            pass
        timestamps = []
        for source in (meta.get("sources") or {}).values():
            try:
                if source.get("event_ts") is not None:
                    timestamps.append(float(source["event_ts"]))
            except (AttributeError, TypeError, ValueError):
                continue
        if timestamps:
            return max(timestamps)
        try:
            return pdir.joinpath("meta.json").stat().st_mtime
        except OSError:
            return 0.0

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
                    copy_file_pinned(real, pdir / new, self.storage_guard)
                fn = new
                changed = True
            seen.add(fn)
            out.append(fn)
        return out, changed

    def _trimmed_dir(self, slug: str) -> Path:
        d = self.persons_dir / slug / "_trimmed"
        if not d.exists():
            self._mkdir_nas(d)
        return d

    def _trim_face(self, slug: str, fname: str, emb, mean_sim: float, reason: str = "over the per-person photo limit — most similar to your other photos", partner: str = ""):
        td = self._trimmed_dir(slug)
        src = self.persons_dir / slug / fname
        if src.exists():
            rename_pinned(src, td / fname, self.storage_guard)
        log_f = td / "log.json"
        try:
            log = json.loads(log_f.read_text()) if log_f.exists() else []
        except (json.JSONDecodeError, OSError):
            log = []
        log.insert(0, {"file": fname, "ts": time.time(), "mean_sim": round(mean_sim, 3),
                       "reason": reason, "partner": partner,
                       "embedding": [round(float(v), 6) for v in emb]})
        # Getrimmt-Ordner begrenzen: nur die neuesten trimmed_keep aufheben, Rest löschen
        keep = self.trimmed_keep if self.trimmed_keep and self.trimmed_keep > 0 else len(log)
        for old in log[keep:]:
            unlink_pinned(td / old["file"], self.storage_guard)
        log = log[:keep]
        self._write_nas_text(log_f, json.dumps(log, ensure_ascii=False))

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
            partner = e.get("partner") or ""
            emb = e.get("embedding")
            sims = act_emb @ np.array(emb, dtype=np.float32) if (emb and len(act_files)) else None
            seen = set()
            if partner and partner in act_files:
                sc = float(sims[act_files.index(partner)]) if sims is not None else 0.0
                similar.append({"file": partner, "score": round(sc, 2), "partner": True})
                seen.add(partner)
            if sims is not None:
                # die ähnlichsten aktiven Fotos — mit Wert, damit sichtbar ist, ob es sich
                # um echte Dubletten (hoch) oder nur dieselbe Person (mittel) handelt
                for i in np.argsort(-sims)[:3]:
                    f = act_files[i]
                    if f not in seen:
                        similar.append({"file": f, "score": round(float(sims[i]), 2), "partner": False})
                        seen.add(f)
            out.append({"file": e["file"], "ts": e.get("ts", 0), "mean_sim": e.get("mean_sim"),
                        "reason": e.get("reason", ""), "similar": similar,
                        "partner": partner,
                        "kind": ("same_image" if "same image" in e.get("reason", "")
                                 else "near_dup" if "near-duplicate" in e.get("reason", "")
                                 # eigener Fall: nicht wegen Menge aussortiert, sondern
                                 # weil die Referenz zwei Personen verwechselbar macht
                                 else "cross" if "too close to" in e.get("reason", "")
                                 # ... oder weil sie zur eigenen Person nicht passt
                                 else "outlier" if "barely resembles" in e.get("reason", "")
                                 else "limit")})
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
            rename_pinned(td / fname, self.persons_dir / slug / fname, self.storage_guard)
            entry["emb"] = np.vstack([entry["emb"], np.array(rec["embedding"], dtype=np.float32)[None, :]])
            entry["files"].append(fname)
            log = [e for e in log if e["file"] != fname]
            self._write_nas_text(log_f, json.dumps(log, ensure_ascii=False))
            self._persist(slug)
            return True

    def delete_trimmed(self, slug: str, fname: str):
        td = self.persons_dir / slug / "_trimmed"
        log_f = td / "log.json"
        unlink_pinned(td / fname, self.storage_guard)
        if log_f.exists():
            try:
                log = [e for e in json.loads(log_f.read_text()) if e["file"] != fname]
                self._write_nas_text(log_f, json.dumps(log, ensure_ascii=False))
            except (json.JSONDecodeError, OSError):
                pass

    def clear_trimmed(self, slug: str) -> int:
        td = self.persons_dir / slug / "_trimmed"
        if not td.exists():
            return 0
        n = 0
        for f in td.glob("*.jpg"):
            unlink_pinned(f, self.storage_guard); n += 1
        unlink_pinned(td / "log.json", self.storage_guard)
        return n

    def _persist(self, slug: str):
        self._ensure_storage()
        pdir = self.persons_dir / slug
        entry = self._cache[slug]
        embeddings_path = pdir / "embeddings.npy"
        token = f"{os.getpid()}.{time.time_ns()}"
        journal_path = pdir / ".faceid-commit.json"
        emb_backup = pdir / f".embeddings.npy.{token}.bak"
        meta_backup = pdir / f".meta.json.{token}.bak"
        journal = {
            "embedding_backup": emb_backup.name if embeddings_path.exists() else None,
            "metadata_backup": meta_backup.name if (pdir / "meta.json").exists() else None,
            "embeddings.npy_existed": embeddings_path.exists(),
            "meta.json_existed": (pdir / "meta.json").exists(),
        }
        if embeddings_path.exists():
            copy_file_pinned(embeddings_path, emb_backup, self.storage_guard)
        if (pdir / "meta.json").exists():
            copy_file_pinned(pdir / "meta.json", meta_backup, self.storage_guard)
        self._write_nas_text(journal_path, json.dumps(journal))
        embeddings_tmp = pdir / f".embeddings.npy.{token}.partial"
        metadata_path = pdir / "meta.json"
        metadata_tmp = pdir / f".meta.json.{token}.partial"
        try:
            buf = __import__("io").BytesIO()
            np.save(buf, entry["emb"])
            self._write_nas_bytes(embeddings_tmp, buf.getvalue())
            rename_pinned(embeddings_tmp, embeddings_path, self.storage_guard)
            self._write_nas_text(
                metadata_tmp,
                json.dumps({"name": entry["name"], "files": entry["files"],
                            "favorite": bool(entry.get("favorite", False)),
                            "sources": entry.get("sources", {}),
                            "last_seen": float(entry.get("last_seen", 0.0)),
                            "person_uuid": entry.get("person_uuid") or str(uuid.uuid4())},
                           ensure_ascii=False, indent=1),
            )
            rename_pinned(metadata_tmp, metadata_path, self.storage_guard)
        except Exception:
            # Leave the journal for the next process to restore both files.
            self._unlink_nas(embeddings_tmp)
            self._unlink_nas(metadata_tmp)
            raise
        self._unlink_nas(journal_path)
        self._unlink_nas(emb_backup)
        self._unlink_nas(meta_backup)

    # ---------- Personen ----------

    def persons(self):
        with self._lock:
            return {
                slug: {"name": e["name"], "count": len(e["files"]), "files": list(e["files"]),
                       "favorite": bool(e.get("favorite", False)),
                       "person_uuid": e.get("person_uuid"),
                       "trimmed": self.trimmed(slug)}
                for slug, e in self._cache.items()
            }

    def person_uuid(self, slug: str) -> str | None:
        """Return the durable opaque UUID for a labelled gallery person."""
        with self._lock:
            entry = self._cache.get(slug)
            return entry.get("person_uuid") if entry else None

    def _cross_max(self, slug: str, emb) -> tuple[float, str]:
        """Wie nah kommt dieses Embedding der Galerie einer ANDEREN Person?

        -> (hoechste Aehnlichkeit, deren Name). Aufrufer haelt bereits das Lock.
        """
        best, who = 0.0, ""
        for other, e in self._cache.items():
            if other == slug or not len(e["files"]):
                continue
            sim = float(np.max(e["emb"] @ emb))
            if sim > best:
                best, who = sim, e["name"]
        return best, who

    def _self_outlier(self, entry) -> tuple | None:
        """Das Foto einer Person, das am wenigsten zu ihren UEBRIGEN Fotos passt —
        sofern es deutlich aus der Reihe faellt. -> (index, mittelwert, median) oder None.

        Hintergrund: Eine Referenz mit wenig echter Gesichtsinformation — Hinterkopf,
        ueberstrahltes IR-Bild, Gesicht zum Boden — erzeugt ein generisches Embedding.
        Sie aehnelt der eigenen Person kaum, zieht aber fremde Gesichter an, weil
        generische Embeddings zu jedem ein bisschen passen.

        Das ist das schaerfere Kriterium: es braucht keine zweite Person zum Vergleich
        und sieht einer Referenz schon an, dass sie nichts taugt. Die Kreuzpruefung kann
        denselben Fall verfehlen, weil sie Galerie gegen Galerie misst — ein LEBENDES
        Gesicht kommt deutlich naeher als jedes gespeicherte Foto derselben Person.

        Gemessen wird relativ zum Median der Person, nicht absolut: wie aehnlich sich die
        Fotos einer Person normalerweise sind, haengt stark davon ab, wie verschieden die
        Aufnahmen sind (hier je nach Person zwischen 0.23 und 0.47).
        """
        n = len(entry["files"])
        if self.self_outlier_ratio <= 0 or n < self.self_outlier_min_photos:
            return None
        sims = entry["emb"] @ entry["emb"].T
        np.fill_diagonal(sims, np.nan)
        mean = np.nanmean(sims, axis=1)
        median = float(np.median(mean))
        worst = int(np.argmin(mean))
        if median <= 0 or float(mean[worst]) >= median * self.self_outlier_ratio:
            return None
        return worst, float(mean[worst]), median

    def _self_outlier_reason(self, name: str, mean_sim: float, median: float) -> str:
        return (f"barely resembles the other photos of {name} ({mean_sim:.3f} where "
                f"{median:.3f} is normal for this person) — a reference with this little "
                f"face information attracts strangers instead of its own person")

    def _drop(self, slug: str, entry, idx: int, mean_sim: float, reason: str, partner: str = ""):
        """Foto aus der aktiven Galerie nehmen und nach _trimmed/ legen (Lock beim Aufrufer)."""
        fname = entry["files"][idx]
        self._trim_face(slug, fname, entry["emb"][idx], mean_sim, reason=reason, partner=partner)
        entry.get("sources", {}).pop(fname, None)
        entry["files"].pop(idx)
        entry["emb"] = np.delete(entry["emb"], idx, axis=0)
        return fname

    def _self_outlier_sweep(self) -> list:
        """Alle Personen auf Selbst-Ausreisser pruefen (Lock beim Aufrufer).

        Iterativ, weil sich der Median mit jedem entfernten Foto verschiebt: erst wenn der
        schlimmste Fall weg ist, zeigt sich, ob der naechste noch aus der Reihe faellt.
        """
        removed = []
        for slug in list(self._cache):
            changed = False
            while True:
                entry = self._cache[slug]
                hit = self._self_outlier(entry)
                if hit is None:
                    break
                idx, mean_sim, median = hit
                reason = self._self_outlier_reason(entry["name"], mean_sim, median)
                fname = self._drop(slug, entry, idx, mean_sim, reason)
                removed.append({"person": entry["name"], "file": fname, "kind": "outlier",
                                "mean_sim": round(mean_sim, 3), "median": round(median, 3)})
                changed = True
                log.info("%s: %s set aside — barely resembles its own person (%.3f vs %.3f)",
                         entry["name"], fname, mean_sim, median)
            if changed:
                self._persist(slug)
        return removed

    def _cross_risk_sweep(self, threshold: float) -> list:
        """Alle Personen auf Referenzen pruefen, die einer FREMDEN Person zu nahe kommen
        (Lock beim Aufrufer)."""
        removed = []
        for slug in list(self._cache):
            changed = False
            while True:
                entry = self._cache[slug]
                if not len(entry["files"]):
                    break
                risks = [self._cross_max(slug, entry["emb"][i])
                         for i in range(len(entry["files"]))]
                worst = max(range(len(risks)), key=lambda i: risks[i][0]) if risks else None
                if worst is None or risks[worst][0] < threshold:
                    break
                sim, who = risks[worst]
                fname = self._drop(slug, entry, worst, sim,
                                   reason=f"too close to {who} ({sim:.3f}) — a reference "
                                          f"this ambiguous makes the two confusable",
                                   partner=who)
                removed.append({"person": entry["name"], "file": fname, "kind": "cross",
                                "similarity": round(sim, 3), "partner": who})
                changed = True
            if changed:
                self._persist(slug)
        return removed

    def cross_risk_scan(self, threshold: float) -> list:
        """Bestehende Galerie durchgehen und Fotos aussortieren, die einer FREMDEN
        Person zu nahe kommen.

        Nicht geloescht, sondern nach _trimmed/ verschoben — mit Begruendung, und
        jederzeit zurueckholbar.
        """
        with self._lock:
            return self._cross_risk_sweep(threshold)

    def quality_scan(self, cross_threshold: float) -> list:
        """Beide Pruefungen in einem Durchgang — der Anwender muss nicht wissen, welche
        von beiden seinen Fall findet.

        Reihenfolge mit Absicht: erst die Selbst-Ausreisser. Referenzen ohne verwertbares
        Gesicht verzerren auch den Kreuzvergleich, also fliegen sie zuerst raus, und die
        Kreuzpruefung laeuft danach auf einer sauberen Galerie.
        """
        with self._lock:
            out = self._self_outlier_sweep()
            if cross_threshold > 0:
                out += self._cross_risk_sweep(cross_threshold)
        return out

    def set_aside(self, slug: str, fname: str, reason: str) -> bool:
        """Ein Referenzfoto mit Begruendung beiseitelegen (nicht loeschen).

        Gedacht fuer den Fall, dass eine Fehlerkennung auf genau dieses Foto
        zurueckgefuehrt wurde — dann soll es mit dem Grund in der Ablage landen und
        jederzeit zurueckholbar sein, wie bei den automatischen Pruefungen auch.
        """
        with self._lock:
            entry = self._cache.get(slug)
            if entry is None or fname not in entry["files"]:
                return False
            idx = entry["files"].index(fname)
            self._drop(slug, entry, idx, 0.0, reason=reason)
            self._persist(slug)
            return True

    def rename(self, slug: str, new_name: str) -> str:
        """Anzeigenamen einer Person aendern.

        Der Ordner (slug) bleibt absichtlich, wie er ist: er steckt in Datei-Referenzen
        und Bild-URLs, und ein Tippfehler im Namen ist kein Grund, Daten zu verschieben.
        Sichtbar ist ohnehin nur der Name — in der UI, im Anwesenheitssensor und im
        Frigate-sub_label.
        """
        name = (new_name or "").strip()
        if not name:
            raise ValueError("empty name")
        with self._lock:
            if slug not in self._cache:
                raise KeyError(slug)
            for other, e in self._cache.items():
                # dieselbe Gleichheit wie in create_person: sonst wird ein Name
                # abgelehnt/akzeptiert, je nachdem ueber welchen Weg er entsteht
                if other != slug and _same_name(e["name"], name):
                    raise ValueError(f"'{name}' already exists")
            self._cache[slug]["name"] = name
            self._persist(slug)
            return name

    def embeddings(self, slug: str):
        """Referenz-Embeddings einer Person (NxD) oder None.

        Wird gebraucht, wenn ein Bild mehrere Gesichter enthaelt und entschieden werden
        muss, welches gemeint ist — das groesste ist es nicht zwangslaeufig.
        """
        with self._lock:
            e = self._cache.get(slug)
            if not e or not len(e["files"]):
                return None
            return e["emb"].copy()

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
            self._ensure_storage()
            # Creating the same display name again is idempotent, even if an older
            # version used a different slugging scheme for it.
            for existing_slug, entry in self._cache.items():
                if _same_name(entry["name"], name):
                    return existing_slug

            # Different names can legitimately have the same ASCII projection
            # ("Alex" / "Alex!", or names from different non-Latin scripts). Never
            # silently turn such a request into the already-existing person.
            if slug in self._cache or (self.persons_dir / slug).exists():
                digest = hashlib.sha256(
                    unicodedata.normalize("NFKC", name).strip().casefold().encode("utf-8")
                ).hexdigest()[:10]
                base = f"{slug}-{digest}"
                slug = base
                suffix = 2
                while slug in self._cache or (self.persons_dir / slug).exists():
                    slug = f"{base}-{suffix}"
                    suffix += 1
            pdir = self.persons_dir / slug
            self._mkdir_nas(pdir)
            self._cache[slug] = {"name": name, "emb": np.zeros((0, 512), dtype=np.float32),
                                 "files": [], "favorite": False, "sources": {},
                                 "last_seen": 0.0, "person_uuid": str(uuid.uuid4())}
            self._persist(slug)
        return slug

    def add_face(self, slug: str, crop_bgr: np.ndarray, embedding: np.ndarray,
                 source: dict | None = None) -> str:
        """Gesichts-Crop + Embedding einer Person hinzufügen.

        ``source`` haelt fest, woher das Foto stammt (Kamera, Ereigniszeit). Ohne diese
        Angabe laesst sich spaeter nicht sagen, ob eine Person nur an einer Kamera
        vertreten ist — siehe scripts/coverage.py."""
        with self._lock:
            self._ensure_storage()
            if slug not in self._cache:
                raise KeyError(slug)
            entry = self._cache[slug]
            observation_key = (source or {}).get("observation_key")
            if observation_key:
                existing = next(
                    (fname for fname, provenance in entry.get("sources", {}).items()
                     if provenance.get("observation_key") == observation_key),
                    None,
                )
                if existing is not None:
                    return existing
            fname = f"{int(time.time() * 1000)}_{len(entry['files'])}.jpg"  # Suffix gegen ms-Kollisionen
            target = self.persons_dir / slug / fname
            temporary = target.with_name(f".{target.name}.{os.getpid()}.{time.time_ns()}.partial.jpg")
            ok, encoded = cv2.imencode(".jpg", crop_bgr, [cv2.IMWRITE_JPEG_QUALITY, 92])
            if not ok:
                raise OSError("failed to encode person face crop")
            try:
                self._write_nas_bytes(temporary, encoded.tobytes())
                rename_pinned(temporary, target, self.storage_guard)
                entry["emb"] = np.vstack([entry["emb"], embedding.astype(np.float32)[None, :]])
                entry["files"].append(fname)
                if source:
                    entry.setdefault("sources", {})[fname] = {
                        k: v for k, v in source.items() if v is not None}
            except Exception:
                self._unlink_nas(temporary)
                self._unlink_nas(target)
                raise
            seen_ts = (source or {}).get("event_ts") if source else None
            try:
                seen_ts = float(seen_ts) if seen_ts is not None else time.time()
            except (TypeError, ValueError):
                seen_ts = time.time()
            entry["last_seen"] = max(float(entry.get("last_seen", 0.0)), seen_ts)
            # Neues Foto, das einer FREMDEN Person zu nahe kommt, gar nicht erst behalten:
            # es wuerde die beiden verwechselbar machen, statt diese Person zu staerken.
            # Aussortiert, nicht geloescht — mit Begruendung und zurueckholbar.
            if self.cross_risk_threshold > 0:
                sim, who = self._cross_max(slug, embedding.astype(np.float32))
                if sim >= self.cross_risk_threshold:
                    self._drop(slug, entry, len(entry["files"]) - 1, sim,
                               reason=f"too close to {who} ({sim:.3f}) — a reference "
                                      f"this ambiguous makes the two confusable",
                               partner=who)
                    self._persist(slug)
                    log.info("%s: new photo set aside — too close to %s (%.3f)",
                             entry["name"], who, sim)
                    return fname
            # Zweites Kriterium: passt das neue Foto ueberhaupt zu dieser Person? Nur das
            # NEUE wird hier geprueft — der Bestand bleibt dem Durchlauf ueberlassen,
            # damit ein einzelner Upload nicht die halbe Galerie umraeumt.
            hit = self._self_outlier(entry)
            if hit is not None and hit[0] == len(entry["files"]) - 1:
                _, mean_sim, median = hit
                self._drop(slug, entry, len(entry["files"]) - 1, mean_sim,
                           reason=self._self_outlier_reason(entry["name"], mean_sim, median))
                self._persist(slug)
                log.info("%s: new photo set aside — barely resembles its own person "
                         "(%.3f vs %.3f)", entry["name"], mean_sim, median)
                return fname
            if self.max_per_person and len(entry["files"]) > self.max_per_person:
                # redundanteste Referenz aussortieren (höchste mittlere Ähnlichkeit zu den
                # übrigen = Dublette). Nicht löschen, sondern nach _trimmed/ verschieben,
                # damit der User in der UI sieht WAS ging und es zurückholen kann.
                sims = entry["emb"] @ entry["emb"].T
                np.fill_diagonal(sims, 0.0)
                mean_sim = sims.mean(axis=1)
                drop = int(np.argmax(mean_sim))
                self._trim_face(slug, entry["files"][drop], entry["emb"][drop], float(mean_sim[drop]))
                entry.get("sources", {}).pop(entry["files"][drop], None)
                entry["files"].pop(drop)
                entry["emb"] = np.delete(entry["emb"], drop, axis=0)
            try:
                self._persist(slug)
            except Exception:
                entry["files"].pop()
                entry["emb"] = np.delete(entry["emb"], -1, axis=0)
                entry.get("sources", {}).pop(fname, None)
                self._unlink_nas(target)
                raise
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

    @staticmethod
    def _dhash(path, size: int = 8):
        """Perceptual hash — erkennt visuell identische Bilddateien unabhaengig vom
        Erkennungsmodell (z.B. zweimal hochgeladen, oder Alt-Artefakte)."""
        img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if img is None:
            return None
        img = cv2.resize(img, (size + 1, size))
        return (img[:, 1:] > img[:, :-1]).flatten()

    def deduplicate_pixels(self, slug: str, max_hamming: int = 2, dry_run: bool = False) -> int:
        """Bild-Dubletten entfernen: Fotos, deren BILD praktisch identisch zu einem
        anderen ist. Das kommt vor, wenn dasselbe Foto zweimal landet — dann zeigen zwei
        Kacheln dasselbe Gesicht, obwohl die hinterlegten Merkmale verschieden sind.
        Solche Referenzen sind nicht beurteilbar und fliegen raus."""
        with self._lock:
            entry = self._cache.get(slug)
            if entry is None:
                return 0
            pdir = self.persons_dir / slug
            hashes = {}
            for f in entry["files"]:
                h = self._dhash(pdir / f)
                if h is not None:
                    hashes[f] = h
            drop = set()
            names = list(hashes)
            for a in range(len(names)):
                if names[a] in drop:
                    continue
                for b in range(a + 1, len(names)):
                    if names[b] in drop:
                        continue
                    if int((hashes[names[a]] != hashes[names[b]]).sum()) <= max_hamming:
                        # das spaeter hinzugefuegte (bzw. _dup-Artefakt) weicht
                        loser = names[b] if "_dup" not in names[a] else names[a]
                        drop.add(loser)
            if dry_run:
                return len(drop)
            moved = 0
            for fname in list(drop):
                if fname not in entry["files"]:
                    continue
                i = entry["files"].index(fname)
                self._trim_face(slug, fname, entry["emb"][i], 1.0,
                                reason="same image as another photo — cannot be judged as its own reference",
                                partner=next((n for n in names if n not in drop and
                                              int((hashes[n] != hashes[fname]).sum()) <= max_hamming), ""))
                entry["files"].pop(i)
                entry["emb"] = np.delete(entry["emb"], i, axis=0)
                moved += 1
            if moved:
                self._persist(slug)
            return moved

    def deduplicate_pixels_all(self, max_hamming: int = 2, dry_run: bool = False) -> int:
        return sum(self.deduplicate_pixels(s, max_hamming, dry_run) for s in list(self._cache.keys()))

    def deduplicate_person(self, slug: str, threshold: float = None, dry_run: bool = False) -> int:
        """Nahezu identische Fotos (Cosine >= threshold) beiseitelegen — sie bringen der
        Erkennung nichts. Von jedem zu ähnlichen Paar geht das redundantere. dry_run zählt
        nur (verschiebt nichts). Läuft, bis kein Paar mehr über der Schwelle liegt."""
        if threshold is None:
            threshold = self.dedupe_threshold
        with self._lock:
            entry = self._cache.get(slug)
            if entry is None:
                return 0
            if dry_run:
                # Simulation auf Kopie: aktive-Maske, ohne Dateien anzufassen
                emb = entry["emb"]
                active = np.ones(emb.shape[0], dtype=bool)
                removed = 0
                while active.sum() > 1:
                    ai = np.where(active)[0]
                    sub = emb[ai]
                    sims = sub @ sub.T
                    np.fill_diagonal(sims, -1.0)
                    p = int(np.argmax(sims))
                    r, c = np.unravel_index(p, sims.shape)
                    if sims[r, c] < threshold:
                        break
                    mean = sims.mean(axis=1)  # sub-mean
                    drop_local = r if mean[r] >= mean[c] else c
                    active[ai[drop_local]] = False
                    removed += 1
                return removed
            moved = 0
            while len(entry["files"]) > 1:
                sims = entry["emb"] @ entry["emb"].T
                np.fill_diagonal(sims, 0.0)
                i, jx = np.unravel_index(int(np.argmax(sims)), sims.shape)
                if sims[i, jx] < threshold:
                    break
                mean = sims.mean(axis=1)
                drop = i if mean[i] >= mean[jx] else jx
                keeper = jx if drop == i else i
                self._trim_face(slug, entry["files"][drop], entry["emb"][drop], float(mean[drop]),
                                reason="near-duplicate of another photo — no added value for recognition",
                                partner=entry["files"][keeper])
                entry["files"].pop(drop)
                entry["emb"] = np.delete(entry["emb"], drop, axis=0)
                moved += 1
            if moved:
                self._persist(slug)
            return moved

    def deduplicate_all(self, threshold: float = None, dry_run: bool = False) -> int:
        return sum(self.deduplicate_person(s, threshold, dry_run) for s in list(self._cache.keys()))

    def delete_face(self, slug: str, fname: str):
        with self._lock:
            self._ensure_storage()
            entry = self._cache[slug]
            if fname not in entry["files"]:
                return
            idx = entry["files"].index(fname)
            entry["files"].pop(idx)
            entry["emb"] = np.delete(entry["emb"], idx, axis=0)
            self._unlink_nas(self.persons_dir / slug / fname)
            self._persist(slug)

    def unassign_face(self, slug: str, fname: str) -> bool:
        """Gesicht aus einer Person entfernen und zurück in die Unknown-Queue legen."""
        with self._lock:
            self._ensure_storage()
            entry = self._cache.get(slug)
            if entry is None or fname not in entry["files"]:
                return False
            idx = entry["files"].index(fname)
            emb = entry["emb"][idx]
            uid = f"u{int(time.time() * 1000)}"
            rename_pinned(self.persons_dir / slug / fname,
                          self.unknown_dir / f"{uid}.jpg", self.storage_guard)
            self._write_nas_text(self.unknown_dir / f"{uid}.json", json.dumps(
                {"camera": "", "event_id": "", "removed_from": entry["name"], "ts": time.time(),
                 "embedding": [round(float(v), 6) for v in emb]}, ensure_ascii=False))
            entry["files"].pop(idx)
            entry["emb"] = np.delete(entry["emb"], idx, axis=0)
            self._persist(slug)
            return True

    def delete_person(self, slug: str):
        with self._lock:
            self._ensure_storage()
            entry = self._cache.pop(slug, None)
            if entry is None:
                return
            pdir = self.persons_dir / slug
            # rmtree statt unlink-Schleife: sobald eine Person ausgemusterte Fotos hat,
            # liegt unter ihr der Ordner _trimmed — und unlink() auf einem Verzeichnis
            # wirft IsADirectoryError. Loeschen war damit fuer praktisch jede Person
            # kaputt, die eine der automatischen Pruefungen durchlaufen hat.
            remove_tree_pinned(pdir, self.storage_guard)

    def touch_person(self, slug: str, seen_ts: float | None = None) -> bool:
        """Persist the last time a person was actually observed by the engine."""
        with self._lock:
            self._ensure_storage()
            entry = self._cache.get(slug)
            if entry is None:
                return False
            ts = time.time() if seen_ts is None else float(seen_ts)
            if ts <= float(entry.get("last_seen", 0.0)):
                return True
            entry["last_seen"] = ts
            self._persist(slug)
            return True

    def merge_persons(self, target_slug: str, source_slug: str) -> int:
        """Merge source into target while preserving embeddings and provenance."""
        with self._lock:
            if target_slug == source_slug:
                return 0
            target = self._cache.get(target_slug)
            source = self._cache.get(source_slug)
            if target is None or source is None:
                raise KeyError("unknown person")
            target_dir = self.persons_dir / target_slug
            source_dir = self.persons_dir / source_slug
            tx = self._begin_person_transaction([target_slug, source_slug], "merge")
            try:
                moved = 0
                for fname, emb in zip(list(source["files"]), source["emb"]):
                    dest_name = fname
                    if dest_name in target["files"] or (target_dir / dest_name).exists():
                        dest_name = f"{source_slug}-{fname}"
                    rename_pinned(source_dir / fname, target_dir / dest_name, self.storage_guard)
                    target["files"].append(dest_name)
                    target["emb"] = np.vstack([target["emb"], emb[None, :]])
                    if fname in source.get("sources", {}):
                        target.setdefault("sources", {})[dest_name] = source["sources"][fname]
                    moved += 1
                target["last_seen"] = max(float(target.get("last_seen", 0.0)),
                                           float(source.get("last_seen", 0.0)))
                self._persist(target_slug)
                self._cache.pop(source_slug, None)
                remove_tree_pinned(source_dir, self.storage_guard)
                self._finish_person_transaction(tx)
                return moved
            except Exception:
                # Restore immediately for callers that continue using this instance;
                # the same prepared journal also protects a process crash.
                self._rollback_person_transaction(tx)
                self.reload()
                raise

    def split_person(self, slug: str, files: list[str], new_name: str) -> tuple[str, int]:
        """Move selected references into a new durable person (the split operation)."""
        with self._lock:
            source = self._cache.get(slug)
            if source is None:
                raise KeyError(slug)
            selected = [fname for fname in files if fname in source["files"]]
            self._ensure_storage()
        # create_person takes the same non-reentrant lock; create it between the
        # validation and the move, then reacquire the lock for the transaction.
        new_slug = self.create_person(new_name)
        with self._lock:
            source = self._cache.get(slug)
            if source is None:
                raise KeyError(slug)
            destination = self._cache[new_slug]
            tx = self._begin_person_transaction([slug, new_slug], "split",
                                                absent_slugs={new_slug})
            try:
                for fname in selected:
                    idx = source["files"].index(fname)
                    destination["emb"] = np.vstack([destination["emb"], source["emb"][idx][None, :]])
                    destination["files"].append(fname)
                    if fname in source.get("sources", {}):
                        destination.setdefault("sources", {})[fname] = source["sources"].pop(fname)
                    destination["last_seen"] = max(float(destination.get("last_seen", 0.0)),
                                                     float(source.get("last_seen", 0.0)))
                    rename_pinned(self.persons_dir / slug / fname,
                                  self.persons_dir / new_slug / fname, self.storage_guard)
                    source["files"].pop(idx)
                    source["emb"] = np.delete(source["emb"], idx, axis=0)
                self._persist(slug)
                self._persist(new_slug)
                self._finish_person_transaction(tx)
                return new_slug, len(destination["files"])
            except Exception:
                self._rollback_person_transaction(tx)
                self.reload()
                raise

    def expire_persons(self, ttl_seconds: float = 90 * 86400,
                       now: float | None = None) -> list[str]:
        """Delete derived person galleries not observed within the retention window."""
        cutoff = (time.time() if now is None else now) - max(0.0, ttl_seconds)
        stale = []
        with self._lock:
            for slug, entry in self._cache.items():
                timestamps = []
                timestamps.append(float(entry.get("last_seen", 0.0)))
                if max(timestamps) < cutoff:
                    stale.append(slug)
        for slug in stale:
            self.delete_person(slug)
        return stale

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
        self._write_nas_text(jf, json.dumps(m, ensure_ascii=False))

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
            self.add_face(slug, crop, np.array(meta["embedding"], dtype=np.float32),
                          source={"camera": meta.get("camera"),
                                  "event_ts": meta.get("event_ts") or meta.get("ts"),
                                  "observation_key": meta.get("observation_key") or iid})
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
            self._ensure_storage()
            jf = self.unknown_dir / f"{uid}.json"
            img = self.unknown_dir / f"{uid}.jpg"
            if not jf.exists() or not img.exists():
                return False
            meta = json.loads(jf.read_text())
            iid = f"i{uid.lstrip('ui')}"
            rename_pinned(img, self.ignored_dir / f"{iid}.jpg", self.storage_guard)
            grp = group or f"g{iid}"
            payload = {k: v for k, v in meta.items() if k in ("camera", "ts", "embedding")}
            payload["group"] = grp
            self._write_nas_text(self.ignored_dir / f"{iid}.json", json.dumps(payload, ensure_ascii=False))
            unlink_pinned(jf, self.storage_guard)
            unlink_pinned(self.unknown_dir / f"{uid}_full.jpg", self.storage_guard)
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
                rename_pinned(src, self.ignored_dir / f"{iid}.jpg", self.storage_guard)
                self._write_nas_text(self.ignored_dir / f"{iid}.json", json.dumps(
                    {"camera": "", "ts": time.time(), "from_person": entry["name"], "group": grp,
                     "embedding": [round(float(v), 6) for v in emb]}, ensure_ascii=False))
                self._ign_emb = np.vstack([self._ign_emb, np.array(emb, dtype=np.float32)[None, :]])
                self._ign_ids.append(iid)
                self._ign_groups.append(grp)
                n += 1
            remove_tree_pinned(self.persons_dir / slug, self.storage_guard)   # s. delete_person
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
            ok, encoded = cv2.imencode(".jpg", crop_bgr, [cv2.IMWRITE_JPEG_QUALITY, 92])
            if not ok:
                raise OSError("failed to encode ignore anchor")
            self._write_nas_bytes(self.ignored_dir / f"{iid}.jpg", encoded.tobytes())
            self._write_nas_text(self.ignored_dir / f"{iid}.json", json.dumps(
                {"camera": "", "ts": time.time(), "auto": True, "group": grp,
                 "embedding": [round(float(v), 6) for v in embedding]}, ensure_ascii=False))
            self._ign_emb = np.vstack([self._ign_emb, embedding.astype(np.float32)[None, :]])
            self._ign_ids.append(iid)
            self._ign_groups.append(grp)
            self._enforce_anchor_cap(grp)
            return iid

    def _enforce_anchor_cap(self, group: str):
        """Auto-gelernte Anker einer Gruppe begrenzen (wie das Foto-Limit bei Personen).

        Ohne Deckel waechst die Ignore-Liste bei viel Publikumsverkehr unbegrenzt. Es
        fliegt der REDUNDANTESTE Anker (hoechste mittlere Aehnlichkeit zu den uebrigen),
        nicht der aelteste — Alter sagt nichts darueber, wie gut ein Anker die Person
        abdeckt. Von Hand angelegte Anker bleiben unangetastet; nur automatisch gelernte
        werden entfernt, sonst koennte die Ignore-Wirkung ganz verschwinden."""
        if not self.max_ignore_anchors:
            return
        idx = [i for i, g in enumerate(self._ign_groups) if g == group]
        if len(idx) <= self.max_ignore_anchors:
            return
        auto = []
        for i in idx:
            jf = self.ignored_dir / f"{self._ign_ids[i]}.json"
            try:
                if json.loads(jf.read_text()).get("auto"):
                    auto.append(i)
            except (OSError, json.JSONDecodeError):
                continue
        if not auto:
            return          # nur manuelle Anker — dann lieber wachsen lassen
        sub = self._ign_emb[idx]
        sims = sub @ sub.T
        np.fill_diagonal(sims, 0.0)
        mean_sim = sims.mean(axis=1)
        pos = {v: k for k, v in enumerate(idx)}
        drop = max(auto, key=lambda i: mean_sim[pos[i]])
        log.info("ignore anchor %s dropped: group '%s' over the cap of %d",
                 self._ign_ids[drop], group, self.max_ignore_anchors)
        self.delete_ignored(self._ign_ids[drop])

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
            rename_pinned(img, self.unknown_dir / f"{uid}.jpg", self.storage_guard)
            meta.update(ts=time.time(), event_id="", restored=True)
            self._write_nas_text(self.unknown_dir / f"{uid}.json", json.dumps(meta, ensure_ascii=False))
            unlink_pinned(jf, self.storage_guard)
            self._drop_ignored(iid)
            return True

    def delete_ignored(self, iid: str):
        with self._lock:
            self._ensure_storage()
            unlink_pinned(self.ignored_dir / f"{iid}.json", self.storage_guard)
            unlink_pinned(self.ignored_dir / f"{iid}.jpg", self.storage_guard)
            self._drop_ignored(iid)

    def _drop_ignored(self, iid: str):
        if iid in self._ign_ids:
            idx = self._ign_ids.index(iid)
            self._ign_ids.pop(idx)
            self._ign_groups.pop(idx)
            self._ign_emb = np.delete(self._ign_emb, idx, axis=0)

    # ---------- Unbekannte ----------

    # The runtime must never persist full camera frames; only bounded face crops
    # are allowed to reach the gallery.  The upstream API keeps this parameter
    # optional for callers that explicitly need a review frame, but this service
    # has no such mode.
    def save_unknown(self, crop_bgr: np.ndarray, embedding: np.ndarray, meta: dict,
                     dedupe_sim: float = 0.75, full_bgr: np.ndarray | None = None):
        """Store a face crop and embedding; full frames are intentionally ignored."""
        with self._lock:
            self._ensure_storage()
            now = time.time()
            observation_key = meta.get("observation_key")
            for jf in self.unknown_dir.glob("*.json"):
                try:
                    m = json.loads(jf.read_text())
                except (json.JSONDecodeError, OSError):
                    continue
                if observation_key and m.get("observation_key") == observation_key:
                    self.last_save_status = "replayed"
                    return jf.stem
                # A distinct event is a distinct observation, even when its
                # embedding is nearly identical.  Similarity dedupe is only a
                # fallback for callers that do not provide an observation key;
                # otherwise the second clear observation could never promote an
                # anonymous person or refresh its last_seen.
                if not observation_key and now - m.get("ts", 0) < 3600:
                    sim = float(np.dot(np.array(m["embedding"], dtype=np.float32), embedding))
                    if sim > dedupe_sim:
                        self.last_save_status = "deduped"
                        return None
            uid = f"u{int(now * 1000)}"
            while (self.unknown_dir / f"{uid}.json").exists():
                uid = f"u{int(time.time_ns())}"
            crop_path = self.unknown_dir / f"{uid}.jpg"
            transaction = self.unknown_dir / f".{uid}.pending.json"
            temporary_crop = crop_path.with_name(
                f".{crop_path.name}.{os.getpid()}.{time.time_ns()}.partial.jpg")
            ok, encoded = cv2.imencode(".jpg", crop_bgr, [cv2.IMWRITE_JPEG_QUALITY, 92])
            if not ok:
                raise OSError("failed to encode face crop")
            metadata_path = self.unknown_dir / f"{uid}.json"
            temporary = metadata_path.with_name(
                f".{metadata_path.name}.{os.getpid()}.{time.time_ns()}.partial")
            try:
                self._ensure_storage()
                self._write_nas_text(transaction, json.dumps({"uid": uid}))
                self._write_nas_bytes(crop_path, encoded.tobytes())
                meta = dict(meta, ts=float(meta.get("ts", now)),
                            ingested_at=float(meta.get("ingested_at", now)),
                            embedding=[round(float(v), 6) for v in embedding])
                self._write_nas_text(metadata_path, json.dumps(meta, ensure_ascii=False))
                self._unlink_nas(transaction)
                self.last_save_status = "created"
                return uid
            except Exception:
                try:
                    self._unlink_nas(transaction)
                except Exception:
                    unlink_pinned(transaction, self.storage_guard)
                unlink_pinned(temporary_crop, self.storage_guard)
                unlink_pinned(temporary, self.storage_guard)
                try:
                    self._unlink_nas(crop_path)
                    self._unlink_nas(metadata_path)
                except Exception:
                    pass
                raise

    def expire_unknowns(self, ttl_seconds: float = 7 * 86400,
                        promoted_ttl_seconds: float = 90 * 86400,
                        now: float | None = None) -> int:
        """Delete provisional data after 7d and promoted data after 90d.

        Promoted status is read from the durable anonymous manifest rather than
        inferred from a filename, so a restart cannot silently shorten a
        promoted observation's retention.
        """
        current = time.time() if now is None else now
        self._ensure_storage()
        try:
            manifest = json.loads(self.anonymous_groups_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            manifest = {}
        promoted = {
            str(oid)
            for group in manifest.get("groups", [])
            if group.get("status") == "promoted"
            for oid in group.get("observation_ids", [])
        }
        removed = 0
        with self._lock:
            self._ensure_storage()
            expired = []
            for jf in self.unknown_dir.glob("*.json"):
                try:
                    meta = json.loads(jf.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                uid = jf.stem
                ttl = promoted_ttl_seconds if uid in promoted else ttl_seconds
                # ``last_seen`` is a group-level ingestion timestamp.  A historical
                # event_ts must never make a newly ingested crop expire immediately,
                # and a remaining crop in a promoted group keeps its 90-day policy.
                group_last_seen = next(
                    (float(group.get("last_seen", 0.0))
                     for group in manifest.get("groups", [])
                     if uid in group.get("observation_ids", [])),
                    None,
                )
                seen_at = group_last_seen if group_last_seen is not None else float(
                    meta.get("ingested_at", meta.get("ts", 0)))
                cutoff = current - max(0.0, ttl)
                if seen_at >= cutoff:
                    continue
                self._unlink_nas(jf)
                self._unlink_nas(self.unknown_dir / f"{uid}.jpg")
                self._unlink_nas(self.unknown_dir / f"{uid}_full.jpg")
                expired.append(uid)
                removed += 1
            if expired:
                self._remove_anonymous_observations(expired)
        return removed

    def unknowns(self):
        out = []
        for jf in sorted(self.unknown_dir.glob("*.json"), reverse=True):
            try:
                m = json.loads(jf.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            out.append({"id": jf.stem, **{k: v for k, v in m.items() if k != "embedding"},
                        "has_full": False,
                        "embedding": np.array(m["embedding"], dtype=np.float32)})
        return out

    def anonymous_person_uuid(self, observation_id: str) -> str | None:
        """Return the stable anonymous UUID currently assigned to an observation."""
        try:
            manifest = json.loads(self.anonymous_groups_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        for group in manifest.get("groups", []):
            if observation_id in group.get("observation_ids", []):
                return group.get("anonymous_person_uuid")
        return None

    def unknown_clusters(self, eps: float = 0.45):
        """Unknowns per DBSCAN über Cosine-Distanz gruppieren (der Immich-Trick)."""
        items = self.unknowns()
        if not items:
            self._sync_anonymous_manifest([])
            return []
        from sklearn.cluster import DBSCAN

        X = np.stack([it["embedding"] for it in items])
        labels = DBSCAN(eps=eps, min_samples=1, metric="cosine").fit(X).labels_
        clusters = {}
        for it, lb in zip(items, labels):
            it.pop("embedding")
            clusters.setdefault(int(lb), []).append(it)
        result = sorted(clusters.values(), key=len, reverse=True)
        self._sync_anonymous_manifest(result)
        return result

    def _sync_anonymous_manifest(self, clusters: list[list[dict]]) -> None:
        """Persist stable anonymous UUIDs for current provisional observations."""
        self._ensure_storage()
        try:
            existing = json.loads(self.anonymous_groups_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = {}
        by_observation = {
            oid: group
            for group in existing.get("groups", [])
            for oid in group.get("observation_ids", [])
            if group.get("anonymous_person_uuid")
        }
        groups = []
        for cluster in clusters:
            ids = sorted(str(item["id"]) for item in cluster)
            old_group = next((by_observation[item] for item in ids if item in by_observation), None)
            group_id = old_group.get("anonymous_person_uuid") if old_group else None
            group_id = group_id or str(uuid.uuid5(
                uuid.NAMESPACE_URL, "camera-face-service:" + "|".join(ids)))
            # Promotion is a durable identity decision.  Once promoted, retaining
            # one observation after TTL cleanup must not downgrade it to provisional.
            status = (old_group.get("status") if old_group and old_group.get("status") == "promoted"
                      else "promoted" if len(ids) >= 2 else "provisional")
            current_seen = max((float(item.get("ingested_at", item.get("ts", 0)))
                                for item in cluster), default=0.0)
            previous_seen = float(old_group.get("last_seen", 0.0)) if old_group else 0.0
            groups.append({"anonymous_person_uuid": group_id, "observation_ids": ids,
                           "status": status, "observation_count": len(ids),
                           "last_seen": max(previous_seen, current_seen),
                           "observations": [dict(item) for item in cluster]})
        self._write_nas_text(
            self.anonymous_groups_path,
            json.dumps({"model": "local-arcface", "groups": groups},
                       ensure_ascii=False, indent=2) + "\n")

    def _remove_anonymous_observations(self, ids: list[str]) -> None:
        try:
            manifest = json.loads(self.anonymous_groups_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        removed = set(ids)
        groups = []
        for group in manifest.get("groups", []):
            keep = [oid for oid in group.get("observation_ids", []) if oid not in removed]
            if keep:
                group["observation_ids"] = keep
                group["observations"] = [item for item in group.get("observations", [])
                                           if item.get("id") in keep]
                groups.append(group)
        manifest["groups"] = groups
        self._write_nas_text(
            self.anonymous_groups_path,
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

    def remove_anonymous_observation(self, uid: str) -> None:
        """Remove one provisional observation from the durable anonymous manifest."""
        with self._lock:
            self._ensure_storage()
            self._remove_anonymous_observations([uid])

    def assign_unknown(self, uid: str, slug: str):
        self._ensure_storage()
        jf = self.unknown_dir / f"{uid}.json"
        img_f = self.unknown_dir / f"{uid}.jpg"
        if not jf.exists() or not img_f.exists():
            return False
        meta = json.loads(jf.read_text())
        crop = cv2.imread(str(img_f))
        self.add_face(slug, crop, np.array(meta["embedding"], dtype=np.float32),
                      source={"camera": meta.get("camera"),
                              "event_ts": meta.get("event_ts") or meta.get("ts"),
                              "observation_key": meta.get("observation_key") or uid})
        try:
            self._unlink_nas(jf)
            self._unlink_nas(img_f)
            self._unlink_nas(self.unknown_dir / f"{uid}_full.jpg")
            self._remove_anonymous_observations([uid])
        except Exception:
            # The durable person record is already committed; leave the unknown
            # for an explicit retry rather than silently losing its provenance.
            log.exception("could not remove assigned unknown %s", uid)
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
            self._write_nas_text(jf, json.dumps(m, ensure_ascii=False))

    def discard_unknown(self, uid: str):
        self._ensure_storage()
        self._unlink_nas(self.unknown_dir / f"{uid}.json")
        self._unlink_nas(self.unknown_dir / f"{uid}.jpg")
        # Remove legacy full-frame artifacts if an older runtime created one.
        self._unlink_nas(self.unknown_dir / f"{uid}_full.jpg")
        self._remove_anonymous_observations([uid])
