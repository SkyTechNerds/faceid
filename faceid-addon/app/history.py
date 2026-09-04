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
        # RLock, nicht Lock: delete() nimmt die Sperre selbst, und ein Aufrufer, der sie
        # schon haelt, wuerde sich sonst selbst blockieren.
        self._lock = threading.RLock()

    # ---------- Schreiben ----------

    def _img_name(self, payload: dict, hid: str) -> str:
        """Dateiname des Bildes einer Zeile. Aeltere Zeilen kennen das Feld nicht.

        Nur der Basisname wird uebernommen: der Wert stammt aus einer Datei, und ein
        Pfad darin duerfte nie mit ``self.dir`` zusammengesetzt werden.
        """
        name = Path(str(payload.get("img") or "")).name
        return name or f"{hid}.jpg"

    @staticmethod
    def _row_of(img: Path) -> str:
        """Zu welcher Zeile gehoert dieses Bild? ``<id>.jpg`` oder ``<id>.vN.jpg``.

        Die einzige Stelle, die den Namensaufbau kennt — vorher fragten Verdraengung
        und Waisensuche dasselbe auf zwei Arten.
        """
        name = img.name[:-len(".jpg")]
        head, sep, tail = name.rpartition(".v")
        return head if sep and tail.isdigit() else name

    def _images_of(self, stem: str):
        """Alle Bildfassungen einer Zeile.

        Bewusst ohne Muster aus der Kennung: die ist Nutzdatum, und ein ``*`` oder
        ``[`` darin traefe als Glob fremde Zeilen.
        """
        for f in self.dir.glob("*.jpg"):
            if f.stem == stem or self._row_of(f) == stem:
                yield f

    def _write_json(self, js: Path, payload: dict) -> bool:
        """JSON atomar ersetzen — der einzige Moment, in dem eine Zeile umschaltet."""
        tmpdir = self.dir / ".tmp"
        tmpdir.mkdir(exist_ok=True)
        tmp = tmpdir / f"{js.stem}.json"
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, js)
            return True
        except (OSError, TypeError, ValueError):
            # json.dumps scheitert an nicht serialisierbaren Werten mit TypeError —
            # faengt man nur OSError, reisst es die ganze Erkennung mit.
            log.exception("could not write the history entry %s", js.stem)
            return False
        finally:
            tmp.unlink(missing_ok=True)

    def _write_image(self, name: str, crop_bgr) -> bool:
        """Bild unter seinem endgueltigen Namen schreiben.

        ``cv2.imwrite`` wirft bei einem Fehler nicht immer, es gibt auch ``False``
        zurueck (volle Platte, unbeschreibbarer Pfad) — beides muss abgefangen werden,
        sonst beschriebe die JSON danach ein Bild, das es nicht gibt.
        """
        try:
            return bool(cv2.imwrite(str(self.dir / name), crop_bgr,
                                    [cv2.IMWRITE_JPEG_QUALITY, 88]))
        except cv2.error:
            log.warning("could not encode the history image %s", name)
            return False

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
                payload["img"] = f"{hid}.jpg"
                # Reihenfolge ist der ganze Trick: Erst das Bild, dann die JSON. Gelistet
                # wird ueber *.json, die Zeile entsteht also genau in dem Moment, in dem
                # die JSON da ist — und dann liegt das Bild schon bereit.
                if not self._write_image(payload["img"], crop_bgr):
                    return None
                if not self._write_json(self.dir / f"{hid}.json", payload):
                    (self.dir / payload["img"]).unlink(missing_ok=True)
                    return None
                self._enforce_cap()
                return hid
        except Exception:
            log.exception("could not record a recognition in the history")
            return None

    def improve(self, hid: str, crop_bgr, embedding, score: float,
                attempt=None) -> bool | None:
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

        Rueckgabe: ``True`` uebernommen; ``False`` nicht uebernommen, die Zeile steht
        unveraendert (nicht besser, oder das Schreiben schlug fehl); ``None`` die Zeile
        gibt es nicht mehr oder sie ist unlesbar — dann muss der Aufrufer neu anlegen,
        sonst faellt die Erkennung stillschweigend aus dem Verlauf.

        Ein unerwarteter Fehler ergibt bewusst ``False`` und nicht ``None``: die Zeile
        existiert ja noch, und ein Neuanlegen brauechte genau das Duplikat, das diese
        Funktion vermeiden soll.
        """
        if self.keep <= 0 or crop_bgr is None or embedding is None or not hid:
            return False
        try:
            with self._lock:
                jf = self.dir / f"{hid}.json"
                if not jf.exists():
                    return None
                try:
                    payload = json.loads(jf.read_text(encoding="utf-8"))
                except (ValueError, OSError):
                    # Unlesbare Zeile: wie eine fehlende behandeln, damit der Aufrufer
                    # neu anlegt statt sie fuer den Rest des Ereignisses einzufrieren.
                    log.warning("history entry %s is unreadable — will be replaced", hid)
                    return None
                # Ein unlesbarer gespeicherter Wert darf nicht dazu fuehren, dass jede
                # weitere Verbesserung still unterbleibt — dann lieber ersetzen.
                try:
                    best = round(float(payload.get("best_score",
                                                   payload.get("score", 0))), 3)
                except (TypeError, ValueError):
                    best = 0.0
                if round(float(score), 3) <= best:
                    return False
                # Das neue Bild bekommt einen EIGENEN Namen, das alte bleibt liegen.
                # Damit aendert sich die Zeile ausschliesslich beim Ersetzen der JSON —
                # scheitert irgendetwas davor oder dabei, zeigt sie unveraendert auf ihr
                # altes, stimmiges Bild. Ein Zurueckspielen von Sicherungskopien braucht
                # es dafuer nicht.
                old_img = self._img_name(payload, hid)
                n = 1
                while (self.dir / f"{hid}.v{n}.jpg").exists():
                    n += 1
                new_img = f"{hid}.v{n}.jpg"
                if not self._write_image(new_img, crop_bgr):
                    # Ein halb geschriebenes Bild wuerde bis zum naechsten Aufraeumen
                    # als Waise herumliegen.
                    (self.dir / new_img).unlink(missing_ok=True)
                    return False
                payload["embedding"] = [round(float(v), 6) for v in embedding]
                payload["best_score"] = round(float(score), 3)
                payload["img"] = new_img
                if attempt is not None:
                    payload["best_attempt"] = attempt
                if not self._write_json(jf, payload):
                    (self.dir / new_img).unlink(missing_ok=True)
                    return False
                if old_img != new_img:
                    # Nur noch Aufraeumen — die Zeile steht bereits richtig. Ein Fehler
                    # hier darf nicht als Fehlschlag zurueckgemeldet werden; das Bild
                    # holt spaetestens der naechste Durchlauf von _enforce_cap.
                    try:
                        (self.dir / old_img).unlink(missing_ok=True)
                    except OSError:
                        log.warning("could not remove the superseded image %s", old_img)
                return True
        except Exception:
            log.exception("could not update history entry %s", hid)
            return False

    def _enforce_cap(self):
        """Aelteste ueber dem Limit entfernen (Aufrufer haelt das Lock)."""
        for old in sorted(self.dir.glob("*.json"), reverse=True)[self.keep:]:
            old.unlink(missing_ok=True)
        # Danach EIN Durchlauf ueber die Bilder: das raeumt die eben verdraengten Zeilen
        # mit weg und ebenso Bilder, die auf anderem Weg ihre Zeile verloren haben — die
        # wuerden sonst nie wieder gezaehlt, weil ueber *.json gelistet wird.
        # Vorher lief _images_of je Zeile einmal ueber das ganze Verzeichnis.
        # Gefahrlos, weil beide Schreibwege das Lock halten und ein Bild nur innerhalb
        # dieses Abschnitts kurz ohne seine Zeile existiert.
        stems = {j.stem for j in self.dir.glob("*.json")}
        for img in self.dir.glob("*.jpg"):
            # Erst der volle Name: gaebe es je eine Kennung, die selbst auf ".v<Ziffern>"
            # endet, wuerde _row_of sie einer fremden Zeile zuschlagen und ihr lebendes
            # Bild loeschen. Die Kennungen entstehen zwar aus time.time() und koennen
            # keinen Punkt enthalten — aber das hier ist ein Aufraeumpfad, und der darf
            # sich auf keine Annahme stuetzen, die er selbst nicht prueft.
            if img.stem not in stems and self._row_of(img) not in stems:
                img.unlink(missing_ok=True)

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
            if not (self.dir / self._img_name(m, jf.stem)).exists():
                continue
            item = {"id": jf.stem, **{k: v for k, v in m.items() if k != "embedding"}}
            # Den geprueften Namen auch ausliefern: sonst pruefte die Zeile oben den
            # bereinigten Wert, waehrend die Oberflaeche den rohen aus der Datei bekaeme.
            item["img"] = self._img_name(m, jf.stem)
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
        # Unter derselben Sperre wie die Schreibwege: sonst koennte zwischen dem
        # Entfernen der Zeile und ihrer Bilder ein laufendes improve() ein neues Bild
        # anlegen, das anschliessend niemandem mehr gehoert.
        with self._lock:
            (self.dir / f"{hid}.json").unlink(missing_ok=True)
            for img in self._images_of(hid):
                img.unlink(missing_ok=True)

    def clear(self) -> int:
        n = 0
        for jf in list(self.dir.glob("*.json")):
            self.delete(jf.stem)
            n += 1
        return n
