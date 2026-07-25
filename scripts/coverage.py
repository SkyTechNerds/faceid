"""Wie gut ist jede Person abgedeckt — und was fehlt ihr?

Mehr Fotos heißen nicht bessere Erkennung; entscheidend ist die Bandbreite. Dieses
Skript vermisst pro Person vier Dimensionen und leitet daraus ab, welche Aufnahme
konkret fehlt:

* **Vielfalt** — mittlere paarweise Ähnlichkeit der Referenzen. Hoch = die Fotos
  ähneln sich stark, jedes weitere bringt wenig.
* **Blickwinkel** — aus den Landmarks geschätzt: frontal / halb / Profil.
* **Licht** — Farbaufnahme (Tag) oder Graustufen (Nacht-/IR-Aufnahme).
* **Selbst-Test** — Leave-one-out: wird ein Foto von den übrigen Fotos derselben
  Person wiedergefunden? Achtung, das ist bewusst hart und bei kleinen Sammlungen
  systematisch pessimistisch: bei fünf vielfältigen Fotos muss eines gegen vier ganz
  andere Situationen bestehen. Eine niedrige Rate heißt "die Fotos stützen einander
  wenig", NICHT "die Person wird im Betrieb nicht erkannt" — dafür ist die
  Praxisprobe in measure-recognition.py zuständig.

    python scripts/coverage.py
"""
import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np
import yaml

BASE = Path(__file__).resolve().parent.parent
TOP_K = 3
FRONTAL, HALF = 0.55, 0.25   # Symmetrie-Grenzen frontal / halb / Profil
NIGHT_SAT = 12               # mittlere HSV-Sättigung darunter = Graustufen/IR


def yaw_symmetry(kps):
    """1.0 = frontal, gegen 0 = Profil. Nase relativ zu den beiden Augen."""
    k = np.asarray(kps, dtype=np.float32)
    if k.shape[0] < 5:
        return None
    axis = k[1] - k[0]
    d = float(np.linalg.norm(axis))
    if d < 1e-3:
        return None
    axis = axis / d
    a = abs(float(np.dot(k[2] - k[0], axis)))
    b = abs(float(np.dot(k[2] - k[1], axis)))
    return min(a, b) / max(a, b) if max(a, b) > 1e-3 else 0.0


def match(gal, e, drop_slug, drop_idx):
    best_slug, best = None, 0.0
    for slug, g in gal.items():
        sims = g["emb"] @ e
        if slug == drop_slug:
            sims = np.delete(sims, drop_idx)
        if not len(sims):
            continue
        k = min(TOP_K, len(sims))
        s = float(np.mean(np.sort(sims)[-k:]))
        if s > best:
            best_slug, best = slug, s
    return best_slug, best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(BASE / "data"))
    args = ap.parse_args()

    sys.path.insert(0, str(BASE))
    from app.engine import FaceEngine

    cfg = yaml.safe_load((BASE / "config.yaml").read_text())
    thr = float(cfg["faceid"].get("match_threshold", 0.5))
    eng = FaceEngine(det_size=int(cfg["faceid"].get("det_size", 640)))

    persons = Path(args.data) / "persons"
    gal = {}
    for d in sorted(p for p in persons.glob("*") if p.is_dir()):
        try:
            emb = np.load(d / "embeddings.npy").astype(np.float32)
            meta = json.loads((d / "meta.json").read_text())
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if emb.ndim != 2 or not len(emb):
            continue
        gal[d.name] = {"name": meta.get("name", d.name), "emb": emb,
                       "files": meta.get("files", []), "dir": d}

    if not gal:
        print("keine Galerie gefunden", file=sys.stderr)
        return 1

    print(f"{'Person':24s} {'Fotos':>5s} {'Vielfalt':>8s} {'frontal':>7s} {'halb':>5s} "
          f"{'Profil':>6s} {'Nacht':>5s} {'Selbst':>7s}")
    print("-" * 78)

    advice = []
    for slug, g in gal.items():
        emb, n = g["emb"], len(g["emb"])
        sims = emb @ emb.T
        np.fill_diagonal(sims, np.nan)
        diversity = float(np.nanmean(sims)) if n > 1 else float("nan")

        angles, night = [], 0
        for fn in g["files"]:
            img = cv2.imread(str(g["dir"] / fn))
            if img is None:
                continue
            if float(np.mean(cv2.cvtColor(img, cv2.COLOR_BGR2HSV)[:, :, 1])) < NIGHT_SAT:
                night += 1
            f = FaceEngine.best_face(eng.faces(img), min_px=20, min_det=0.4)
            if f is not None:
                s = yaw_symmetry(f.kps)
                if s is not None:
                    angles.append(s)

        front = sum(1 for a in angles if a >= FRONTAL)
        half = sum(1 for a in angles if HALF <= a < FRONTAL)
        prof = sum(1 for a in angles if a < HALF)

        hit = 0
        for i in range(n):
            got, sc = match(gal, emb[i], slug, i)   # einmal rechnen, nicht zweimal
            if got == slug and sc >= thr:
                hit += 1
        rate = 100 * hit // n if n else 0

        div_s = "—" if n < 2 else f"{diversity:.2f}"
        print(f"{g['name'][:24]:24s} {n:5d} {div_s:>8s} {front:>7d} {half:>5d} "
              f"{prof:>6d} {night:>5d} {rate:>6d}%")

        missing = []
        if n < 5:
            missing.append("zu wenige Fotos")
        if not front:
            missing.append("keine frontale Aufnahme")
        if not night:
            missing.append("keine Nacht-/IR-Aufnahme")
        if n > 1 and diversity > 0.55:
            missing.append(f"Fotos ähneln sich stark ({diversity:.2f})")
        # Der Selbst-Test ist bei kleinen Sammlungen systematisch pessimistisch —
        # als Mangel nur melden, wenn genug Fotos da sind, dass er aussagt.
        if n >= 8 and rate < 40:
            missing.append(f"Fotos stuetzen einander kaum (Selbst-Test {rate}%)")
        if missing:
            advice.append((rate, g["name"], missing))

    if advice:
        print("\nWas konkret fehlt (schwächste zuerst):")
        for rate, name, missing in sorted(advice):
            print(f"  {name}: {', '.join(missing)}")
    else:
        print("\nAlle Personen sind breit abgedeckt.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
