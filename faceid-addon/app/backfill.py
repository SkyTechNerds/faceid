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
from .frigate_api import FrigateAPI
from .gallery import Gallery

BASE = Path(__file__).resolve().parent.parent


def run_backfill(engine, gallery, frigate, frigate_url: str, days: int = 14,
                 min_px: int = 64, min_det: float = 0.65, dedupe: float = 0.82,
                 tag: bool = True, match_thr: float = 0.5, progress=None) -> dict:
    """Person-Events der letzten `days` Tage verarbeiten. Threadsafe zur Live-Pipeline
    (Engine und Galerie sind intern gelockt). progress(i, total) wird pro Event gerufen."""
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
        if face is None:
            stats["no_face"] += 1
            continue
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
        uid = gallery.save_unknown(
            crop_face(img, face.bbox), emb,
            {"camera": ev["camera"], "event_id": ev["id"], "backfill": True,
             "guess": name, "guess_score": round(float(score), 3)},
            dedupe_sim=dedupe, full_bgr=img,
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
                         progress=progress)
    print(f"Fertig: {stats['faces']} Gesichter in der Review-Queue, {stats['dupe']} Dubletten, "
          f"{stats['no_face']} ohne brauchbares Gesicht, {stats['known']} bereits bekannt "
          f"(von {stats['events']} Events).")


if __name__ == "__main__":
    main()
