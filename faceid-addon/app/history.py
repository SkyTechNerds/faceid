"""Verlauf der veroeffentlichten Erkennungen — mit dem Bild, das WIRKLICH benutzt wurde.

Warum es das gibt: Fragt man einen Tag spaeter "wer war da eigentlich zu sehen?", ist der
Frigate-Snapshot laengst ein anderer. Frigate ersetzt ihn waehrend des Ereignisses
fortlaufend durch den mit dem hoechsten Personen-Score, und das ist selten der Moment, in
dem das Gesicht erkannt wurde. Eine Nachpruefung am Snapshot zeigt deshalb regelmaessig
eine andere Person als die, ueber die entschieden wurde — hier einmal live passiert:
gemeldet wurde korrekt Person A, der Snapshot zeigte zwei Sekunden spaeter Person B.

Deshalb legt FaceID beim Veroeffentlichen den benutzten Gesichtsausschnitt selbst ab,
zusammen mit dem Embedding. Das Embedding ist der eigentliche Gewinn: Damit laesst sich
nachtraeglich ausrechnen, WELCHES Referenzfoto einen falschen Treffer verursacht hat —
und was passiert waere, haette es dieses Foto nicht gegeben.
"""
import json
import logging
import os
import threading
import time
from pathlib import Path

import cv2
import numpy as np

from .analysis import SELF_HIT

log = logging.getLogger("faceid.history")


class History:
    def __init__(self, data_dir: Path, keep: int = 200):
        self.dir = data_dir / "history"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.keep = int(keep)
        self._lock = threading.Lock()

    # ---------- Schreiben ----------

    def _write_pair(self, hid: str, crop_bgr, payload: dict) -> bool:
        """Bild und JSON als Paar schreiben — beide oder keins.

        ``cv2.imwrite`` wirft bei einem Fehler nicht, es gibt ``False`` zurueck (volle
        Platte, unbeschreibbarer Pfad). Wird das uebergangen, beschreibt die JSON danach
        ein Gesicht, das im .jpg gar nicht steht — und genau darauf laeuft spaeter die
        Fehleranalyse.

        Beide Dateien gehen zuerst nach ``.tmp`` und werden dann per ``os.replace``
        eingehaengt. Ein Absturz zwischen den beiden ``replace`` bleibt theoretisch
        moeglich; das Fenster schrumpft damit aber von zwei Schreibvorgaengen auf zwei
        Metadaten-Operationen.
        """
        jpg, js = self.dir / f"{hid}.jpg", self.dir / f"{hid}.json"
        # Eigenes Unterverzeichnis, und die Zwischendatei behaelt .jpg: OpenCV waehlt das
        # Format ueber die Endung und schreibt nach ".jpg.tmp" gar nichts. Im selben
        # Verzeichnis wuerde sie ausserdem vom *.json-Glob mitgezaehlt und taeuchte
        # kurzzeitig als halbe Zeile im Verlauf auf.
        tmpdir = self.dir / ".tmp"
        tmpdir.mkdir(exist_ok=True)
        tmp_jpg, tmp_js = tmpdir / f"{hid}.jpg", tmpdir / f"{hid}.json"

        backup = tmpdir / f"{hid}.prev.jpg"
        try:
            if not cv2.imwrite(str(tmp_jpg), crop_bgr, [cv2.IMWRITE_JPEG_QUALITY, 88]):
                log.warning("could not write the history image for %s", hid)
                return False
            tmp_js.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            # Das Bild wandert zuerst. Scheitert danach die JSON, waere das Bild neu und
            # die Beschreibung alt — genau der Widerspruch, den diese Funktion verhindern
            # soll. Deshalb vorher eine Kopie, die in dem Fall zurueckgespielt wird.
            if jpg.exists():
                os.replace(jpg, backup)
            os.replace(tmp_jpg, jpg)
            try:
                os.replace(tmp_js, js)
            except OSError:
                if backup.exists():
                    os.replace(backup, jpg)
                raise
            return True
        finally:
            for f in (tmp_jpg, tmp_js, backup):
                f.unlink(missing_ok=True)

    def add(self, crop_bgr, embedding, meta: dict) -> str | None:
        """Einen veroeffentlichten Treffer ablegen. Fehler hier duerfen die Erkennung
        nie stoeren — im Zweifel wird nichts gespeichert und weitergearbeitet."""
        if self.keep <= 0 or crop_bgr is None or embedding is None:
            return None
        try:
            with self._lock:
                # Suffix gegen Millisekunden-Kollisionen: bei einer Gruppe entstehen
                # mehrere Meldungen im selben Augenblick, und ohne das ueberschreiben
                # sie einander stillschweigend.
                base = f"h{int(time.time() * 1000)}"
                hid, n = base, 0
                while (self.dir / f"{hid}.json").exists():
                    n += 1
                    hid = f"{base}_{n}"
                payload = dict(meta)
                payload["embedding"] = [round(float(v), 6) for v in embedding]
                if not self._write_pair(hid, crop_bgr, payload):
                    return None
                self._enforce_cap()
                return hid
        except Exception:
            log.exception("could not record a recognition in the history")
            return None

    def improve(self, hid: str, crop_bgr, embedding, score: float, attempt=None) -> bool:
        """Einen spaeteren, besseren Treffer derselben Person im selben Ereignis
        einarbeiten — ohne eine zweite Zeile anzulegen.

        Gemeldet wurde nur der erste Treffer (das ``announced``-Set unterdrueckt
        weitere), eine zweite Zeile behauptete also eine Meldung, die es nie gab. Der
        spaetere Ausschnitt ist aber haeufig der weit bessere Beleg — gemessen bis zu
        94 KB gegen 8 KB derselben Person —, und genau der macht eine Fehlerkennung
        nachpruefbar. Also: eine Zeile, aber mit dem besten Bild.

        Bild UND Embedding werden zusammen ersetzt. Sie getrennt zu behandeln waere der
        eigentliche Fehler: Die Analyse liefe sonst auf einem anderen Gesicht, als die
        Zeile zeigt. ``score``/``ts`` bleiben die der Meldung, damit der Verlauf weiter
        beantwortet, womit sie ausgeloest wurde.

        Rueckgabe: ``True`` uebernommen, ``False`` nicht besser, ``None`` die Zeile gibt
        es nicht mehr (vom Limit verdraengt) — dann muss der Aufrufer neu anlegen, sonst
        faellt die Erkennung stillschweigend aus dem Verlauf.
        """
        if self.keep <= 0 or crop_bgr is None or embedding is None or not hid:
            return False
        try:
            with self._lock:
                jf = self.dir / f"{hid}.json"
                if not jf.exists():
                    return None
                payload = json.loads(jf.read_text(encoding="utf-8"))
                # Ein unlesbarer gespeicherter Wert darf nicht dazu fuehren, dass jede
                # weitere Verbesserung still unterbleibt — dann lieber ersetzen.
                try:
                    best = round(float(payload.get("best_score",
                                                   payload.get("score", 0))), 3)
                except (TypeError, ValueError):
                    best = 0.0
                if round(float(score), 3) <= best:
                    return False
                payload["embedding"] = [round(float(v), 6) for v in embedding]
                payload["best_score"] = round(float(score), 3)
                if attempt is not None:
                    payload["best_attempt"] = attempt
                # Scheitert das Bild, bleibt die alte Zeile unveraendert stehen — lieber
                # der aeltere, stimmige Stand als eine Zeile, die auf ein anderes
                # Gesicht zeigt.
                return self._write_pair(hid, crop_bgr, payload)
        except Exception:
            log.exception("could not update history entry %s", hid)
            return False

    def _enforce_cap(self):
        """Aelteste ueber dem Limit entfernen (Aufrufer haelt das Lock)."""
        files = sorted(self.dir.glob("*.json"), reverse=True)
        for old in files[self.keep:]:
            old.unlink(missing_ok=True)
            (self.dir / f"{old.stem}.jpg").unlink(missing_ok=True)

    # ---------- Lesen ----------

    def items(self, limit: int = 100, gallery=None, threshold: float = 0.5) -> list:
        """Der Verlauf, optional mit dem HEUTIGEN Urteil je Eintrag.

        Der Eintrag selbst bleibt unangetastet — er protokolliert, was gemeldet wurde, und
        das aendert sich nachtraeglich nicht. Wird die Galerie mitgegeben, kommt zusaetzlich
        dazu, wie dasselbe Gesicht jetzt eingeordnet wuerde: Ein damals unbekanntes Gesicht,
        das inzwischen einer Person zugewiesen wurde, traegt hier ihren Namen. Genau die
        Rueckmeldung, ob das Zuordnen etwas gebracht hat.
        """
        out = []
        for jf in sorted(self.dir.glob("*.json"), reverse=True)[:limit]:
            try:
                m = json.loads(jf.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if not (self.dir / f"{jf.stem}.jpg").exists():
                continue
            item = {"id": jf.stem, **{k: v for k, v in m.items() if k != "embedding"}}
            if gallery is not None and m.get("embedding"):
                try:
                    emb = np.array(m["embedding"], dtype=np.float32)
                    _, name, score = gallery.match(emb)
                    # Nur einen Treffer zeigen, der auch veroeffentlicht wuerde. match()
                    # liefert immer den besten Kandidaten, auch bei 0.13 — das als
                    # "heute waere das X" anzuzeigen waere genau die irrefuehrende Angabe,
                    # gegen die dieser Verlauf gebaut wurde.
                    if name and name != item.get("person") and score >= threshold:
                        # Ein Wert von 1.0 heisst nicht "wird sicher erkannt", sondern
                        # dass genau dieser Ausschnitt inzwischen selbst ein Referenzfoto
                        # ist: das Bild vergleicht sich mit sich selbst. Als Guetemass
                        # waere das eine Scheinaussage, also wird es getrennt ausgewiesen.
                        item["now"] = {"person": name, "score": round(float(score), 3),
                                       "self": float(score) >= SELF_HIT}
                except Exception:
                    pass
            out.append(item)
        return out

    def _embedding(self, hid: str):
        jf = self.dir / f"{hid}.json"
        if not jf.exists():
            return None, {}
        try:
            m = json.loads(jf.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None, {}
        emb = m.get("embedding")
        if not emb:
            return None, m
        return np.array(emb, dtype=np.float32), m

    # ---------- Ursachensuche ----------

    def blame(self, hid: str, gallery) -> dict:
        """Welches Referenzfoto hat diesen Treffer getragen — und was waere ohne es passiert?

        Genau die Rechnung, die eine Fehlerkennung aufklaert: Ein einzelnes Foto mit wenig
        Gesichtsinformation kann eine fremde Person anziehen, waehrend alle uebrigen Fotos
        derselben Person weit zurueckliegen. Ist der Abstand zwischen bestem und
        zweitbestem Foto gross, war es genau dieses eine Foto.
        """
        emb, meta = self._embedding(hid)
        if emb is None:
            raise KeyError(hid)
        name = meta.get("person", "")
        slug = next((s for s, e in gallery._cache.items() if e["name"] == name), None)
        if slug is None:
            return {"person": name, "found": False,
                    "note": "this person no longer exists in the gallery"}

        entry = gallery._cache[slug]
        sims = entry["emb"] @ emb
        order = np.argsort(-sims)
        k = gallery.top_k
        photos = [{"file": entry["files"][i], "sim": round(float(sims[i]), 3)}
                  for i in order[:6]]

        def score_of(vals):
            if not len(vals):
                return 0.0
            kk = min(k, len(vals))
            return float(np.sort(vals)[-kk:].mean())

        with_all = score_of(sims)
        without_top = score_of(np.delete(sims, order[0]))

        # Wer haette stattdessen gewonnen?
        others = []
        for s2, e2 in gallery._cache.items():
            if s2 == slug or not len(e2["files"]):
                continue
            others.append((score_of(e2["emb"] @ emb), e2["name"]))
        others.sort(reverse=True)

        return {"person": name, "found": True, "slug": slug,
                "score": round(with_all, 3),
                "without_top": round(without_top, 3),
                "top_photo": photos[0]["file"] if photos else "",
                "photos": photos,
                "runner_up": ({"name": others[0][1], "score": round(others[0][0], 3)}
                              if others else None),
                # Der aussagekraeftigste Wert: Traegt EIN Foto den Treffer allein?
                "carried_by_one": bool(photos and len(photos) > 1
                                       and photos[0]["sim"] - photos[1]["sim"] >= 0.15)}

    # ---------- Aufraeumen ----------

    def delete(self, hid: str):
        (self.dir / f"{hid}.json").unlink(missing_ok=True)
        (self.dir / f"{hid}.jpg").unlink(missing_ok=True)

    def clear(self) -> int:
        n = 0
        for jf in list(self.dir.glob("*.json")):
            self.delete(jf.stem)
            n += 1
        return n
