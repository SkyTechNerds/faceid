"""Frigate-Historie in die Unknown-Queue laden: python -m app.backfill [--days 28]

Zieht vergangene Person-Events mit Snapshot, extrahiert das beste Gesicht pro Event
und legt es als Unknown ab. Die Review-UI clustert dann automatisch — pro Cluster
nur noch Namen vergeben. Für Training gelten strengere Qualitätsfilter als live.
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=28)
    ap.add_argument("--min-px", type=int, default=64, help="Mindest-Gesichtsgröße (Training: strenger als live)")
    ap.add_argument("--min-det", type=float, default=0.65)
    ap.add_argument("--dedupe", type=float, default=0.82, help="Cosine-Sim, ab der ein Gesicht als Dublette gilt")
    ap.add_argument("--no-tag", action="store_true", help="erkannte Events NICHT in Frigate sub_labeln")
    args = ap.parse_args()

    cfg = yaml.safe_load((BASE / "config.yaml").read_text())
    frigate = FrigateAPI(cfg["frigate"]["url"])
    gallery = Gallery(BASE / "data")
    engine = FaceEngine(det_size=int(cfg["faceid"].get("det_size", 640)))

    after = time.time() - args.days * 86400
    events, before = [], None
    while True:  # Frigate paginiert über before=start_time
        params = {"label": "person", "has_snapshot": 1, "limit": 100, "after": after}
        if before:
            params["before"] = before
        batch = requests.get(f"{cfg['frigate']['url']}/api/events", params=params, timeout=10).json()
        if not batch:
            break
        events.extend(batch)
        before = batch[-1]["start_time"]
        if len(batch) < 100:
            break
    print(f"{len(events)} Person-Events der letzten {args.days} Tage mit Snapshot")

    stats = {"faces": 0, "no_face": 0, "dupe": 0, "known": 0}
    match_thr = float(cfg["faceid"].get("match_threshold", 0.5))
    for i, ev in enumerate(events):
        img = frigate.snapshot(ev["id"], crop=True)
        if img is None:
            stats["no_face"] += 1
            continue
        face = FaceEngine.best_face(engine.faces(img), min_px=args.min_px, min_det=args.min_det)
        if face is None:
            stats["no_face"] += 1
            continue
        emb = face.normed_embedding
        slug, name, score = gallery.match(emb)
        if slug and score >= match_thr:
            stats["known"] += 1  # schon eingelernte Person -> kein Review nötig
            if not args.no_tag:
                frigate.set_sub_label(ev["id"], name, score)  # Clip rückwirkend taggen
            continue
        uid = gallery.save_unknown(
            crop_face(img, face.bbox), emb,
            {"camera": ev["camera"], "event_id": ev["id"], "backfill": True,
             "guess": name, "guess_score": round(float(score), 3)},
            dedupe_sim=args.dedupe, full_bgr=img,
        )
        stats["dupe" if uid is None else "faces"] += 1
        if (i + 1) % 50 == 0:
            print(f"  {i + 1}/{len(events)} verarbeitet … ({stats})")

    print(f"Fertig: {stats['faces']} Gesichter in der Review-Queue, "
          f"{stats['dupe']} Dubletten übersprungen, {stats['no_face']} ohne brauchbares Gesicht, "
          f"{stats['known']} bereits bekannt.")


if __name__ == "__main__":
    main()
