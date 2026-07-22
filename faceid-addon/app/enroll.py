"""Enrollment aus Foto-Ordnern: python -m app.enroll "Christian" /pfad/zu/fotos [--max 40]"""
import argparse
from pathlib import Path

import cv2

from .engine import FaceEngine, crop_face
from .gallery import Gallery

BASE = Path(__file__).resolve().parent.parent
EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("person")
    ap.add_argument("folder")
    ap.add_argument("--max", type=int, default=40, help="max. Bilder (Diversität > Menge)")
    args = ap.parse_args()

    engine = FaceEngine()
    gallery = Gallery(BASE / "data")
    slug = gallery.create_person(args.person)

    files = sorted(p for p in Path(args.folder).rglob("*") if p.suffix.lower() in EXT)[: args.max]
    added = 0
    for p in files:
        img = cv2.imread(str(p))
        if img is None:
            print(f"  übersprungen (kein Bild): {p.name}")
            continue
        if max(img.shape[:2]) > 2000:
            s = 2000 / max(img.shape[:2])
            img = cv2.resize(img, None, fx=s, fy=s)
        face = FaceEngine.best_face(engine.faces(img), min_px=60)
        if face is None:
            print(f"  übersprungen (kein Gesicht): {p.name}")
            continue
        gallery.add_face(slug, crop_face(img, face.bbox), face.normed_embedding)
        added += 1
        print(f"  eingelernt: {p.name}")
    print(f"Fertig: {added} Gesichter für '{args.person}' ({slug}).")


if __name__ == "__main__":
    main()
