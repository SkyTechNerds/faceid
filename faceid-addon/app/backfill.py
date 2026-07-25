"""Frigate-Historie scannen: Gesichter in die Review-Queue laden, Bekannte taggen.

Als Bibliothek (run_backfill, genutzt vom UI-Button) und als CLI:
    python -m app.backfill [--days 14] [--no-tag]
"""
import argparse
import time
from pathlib import Path

import requests
import yaml

from .engine import FaceEngine, crop_face
from .hires import upgrade_face
from .frigate_api import FrigateAPI
from .gallery import Gallery

BASE = Path(__file__).resolve().parent.parent


def run_backfill(engine, gallery, frigate, frigate_url: str, days: int = 14,
                 min_px: int = 64, min_det: float = 0.65, dedupe: float = 0.82,
                 tag: bool = True, match_thr: float = 0.5, progress=None,
                 hires: bool = True, rescue: bool = False,
                 rescue_min_px: int = 90, rescue_min_det: float = 0.85) -> dict:
    """Person-Events der letzten `days` Tage verarbeiten. Threadsafe zur Live-Pipeline
    (Engine und Galerie sind intern gelockt). progress(i, total) wird pro Event gerufen.

    ``rescue`` holt Ereignisse zurück, in deren Snapshot gar kein Gesicht steckte: der
    Detect-Stream ist zu grob, in der Aufnahme ist dieselbe Person oft doppelt so groß und
    wird dann erkannt. Kostet einen Clip-Download pro betroffenem Ereignis — deshalb nur
    für diesen manuell angestoßenen Lauf, nicht für die Live-Pipeline. Erwartungswert aus
    einer Messung über 180 Ereignisse: gut jedes fünfte liefert einen Fund, der die
    Güteschwelle übersteht.
    """
    after = time.time() - days * 86400
    events, before = [], None
    while True:  # Frigate paginiert über before=start_time
        params = {"label": "person", "has_snapshot": 1, "limit": 100, "after": after}
        if before:
            params["before"] = before
        batch = requests.get(f"{frigate_url}/api/events", params=params, timeout=10).json()
        if not batch:
            break
        events.extend(batch)
        before = batch[-1]["start_time"]
        if len(batch) < 100:
            break

    stats = {"events": len(events), "faces": 0, "no_face": 0, "dupe": 0, "known": 0, "ignored": 0}
    for i, ev in enumerate(events):
        if progress:
            progress(i + 1, len(events))
        img = frigate.snapshot(ev["id"], crop=True)
        if img is None:
            stats["no_face"] += 1
            continue
        face = FaceEngine.best_face(engine.faces(img), min_px=min_px, min_det=min_det)
        upgraded = False
        if face is None:
            if not (hires and rescue):
                stats["no_face"] += 1
                continue
            # Kein Gesicht im Detect-Snapshot heißt nicht "kein Gesicht da" — in der
            # Aufnahme ist es doppelt so groß. Ohne Referenzgesicht fehlt hier die
            # Identitätsprüfung, deshalb entscheidet die Detektionsgüte: an 74 echten
            # Funden gemessen liefert alles unter ~0.8 Hinterköpfe, Bewegungsunschärfe
            # und schlicht Fehldetektionen (ein Kirchturm bei 0.57). Nicht zusätzlich auf
            # den Galerie-Match filtern — Fremde sollen ja gerade in die Queue.
            try:
                hit = upgrade_face(engine, frigate, ev["camera"], ev.get("start_time"),
                                   ev.get("end_time"), None, event_id=ev["id"],
                                   min_px=rescue_min_px, min_det=rescue_min_det)
            except Exception:
                hit = None
            if hit is None:
                stats["no_face"] += 1
                continue
            face, img = hit
            upgraded = True
            stats["rescued"] = stats.get("rescued", 0) + 1
        emb = face.normed_embedding
        slug, name, score = gallery.match(emb)
        if gallery.match_ignored(emb) >= max(match_thr, score):
            stats["ignored"] += 1
            continue
        if slug and score >= match_thr:
            stats["known"] += 1  # schon eingelernte Person -> kein Review nötig
            if tag:
                frigate.set_sub_label(ev["id"], name, score)  # Clip rückwirkend taggen
            continue
        crop, save_emb, full = crop_face(img, face.bbox), emb, img
        if hires and not upgraded:
            try:
                hi = upgrade_face(engine, frigate, ev["camera"], ev.get("start_time"),
                                  ev.get("end_time"), emb, event_id=ev["id"])
            except Exception:
                hi = None
            if hi is not None:
                hface, himg = hi
                if int(hface.bbox[2] - hface.bbox[0]) > int(face.bbox[2] - face.bbox[0]):
                    crop, save_emb, full = crop_face(himg, hface.bbox), hface.normed_embedding, himg
                    stats["hires"] = stats.get("hires", 0) + 1
        uid = gallery.save_unknown(
            crop, save_emb,
            {"camera": ev["camera"], "event_id": ev["id"], "backfill": True,
             "event_ts": ev.get("start_time"),  # wann es passierte, nicht wann wir es fanden
             "guess": name, "guess_score": round(float(score), 3)},
            dedupe_sim=dedupe, full_bgr=full,
        )
        stats["dupe" if uid is None else "faces"] += 1
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--min-px", type=int, default=64, help="Mindest-Gesichtsgröße (Training: strenger als live)")
    ap.add_argument("--min-det", type=float, default=0.65)
    ap.add_argument("--dedupe", type=float, default=0.82, help="Cosine-Sim, ab der ein Gesicht als Dublette gilt")
    ap.add_argument("--no-tag", action="store_true", help="erkannte Events NICHT in Frigate sub_labeln")
    ap.add_argument("--rescue", action="store_true",
                    help="Events ohne Gesicht im Snapshot per Clip-Scan nachholen (langsam)")
    ap.add_argument("--rescue-min-det", type=float, default=0.85,
                    help="Detektionsgüte für Rescue-Funde; niedriger = mehr Funde, mehr Ausschuss")
    args = ap.parse_args()

    cfg = yaml.safe_load((BASE / "config.yaml").read_text())
    frigate = FrigateAPI(cfg["frigate"]["url"])
    gallery = Gallery(BASE / "data")
    engine = FaceEngine(det_size=int(cfg["faceid"].get("det_size", 640)))

    def progress(i, total):
        if i % 50 == 0:
            print(f"  {i}/{total} verarbeitet …")

    stats = run_backfill(engine, gallery, frigate, cfg["frigate"]["url"], days=args.days,
                         min_px=args.min_px, min_det=args.min_det, dedupe=args.dedupe,
                         tag=not args.no_tag,
                         match_thr=float(cfg["faceid"].get("match_threshold", 0.5)),
                         progress=progress, rescue=args.rescue,
                         rescue_min_det=args.rescue_min_det)
    print(f"Fertig: {stats['faces']} Gesichter in der Review-Queue, {stats['dupe']} Dubletten, "
          f"{stats['no_face']} ohne brauchbares Gesicht, {stats['known']} bereits bekannt "
          f"(von {stats['events']} Events).")
    if stats.get("rescued"):
        print(f"  davon {stats['rescued']} erst über die Aufnahme gefunden.")


if __name__ == "__main__":
    main()
