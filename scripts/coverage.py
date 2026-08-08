"""Wie gut ist jede Person abgedeckt — und was fehlt ihr?

Mehr Fotos heißen nicht bessere Erkennung; entscheidend ist die Bandbreite. Dieses
Skript vermisst pro Person vier Dimensionen und leitet daraus ab, welche Aufnahme
konkret fehlt:

* **Vielfalt** — mittlere paarweise Ähnlichkeit der Referenzen. Hoch = die Fotos
  ähneln sich stark, jedes weitere bringt wenig.
* **Blickwinkel** — aus den Landmarks geschätzt: frontal / halb / Profil.
* **Kameras** — an welchen Kameras die Person überhaupt vertreten ist. Wer nur an
  einer Kamera eingelernt ist, wird an den anderen schlechter erkannt (anderer
  Winkel, andere Höhe, anderes Licht).
* **Graustufen** — Anteil an S/W-Aufnahmen. Nur dort ein Mangel, wo eine Kamera
  tatsächlich in den IR-Nachtmodus schaltet; wo bei Bewegung Licht angeht, nimmt sie
  auch nachts in Farbe auf.
* **Selbst-Test** — Leave-one-out: wird ein Foto von den übrigen Fotos derselben
  Person wiedergefunden? Achtung, das ist bewusst hart und bei kleinen Sammlungen
  systematisch pessimistisch: bei fünf vielfältigen Fotos muss eines gegen vier ganz
  andere Situationen bestehen. Eine niedrige Rate heißt "die Fotos stützen einander
  wenig", NICHT "die Person wird im Betrieb nicht erkannt" — dafür ist die
  Praxisprobe in measure-recognition.py zuständig.

    python scripts/coverage.py
"""
import argparse
import datetime
import json
import time
from collections import Counter
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


def ir_cameras(cfg, days: int = 7, sample: int = 8) -> set:
    """Kameras, deren Nachtaufnahmen in Graustufen kommen (echte IR-Umschaltung).

    Wo bei Bewegung Licht angeht, nimmt die Kamera auch nachts in Farbe auf — dort ist
    eine fehlende IR-Referenz kein Mangel. Ermittelt aus echten Ereignissen statt aus
    der Galerie, sonst wuerde man genau die Luecke uebersehen, die man sucht.
    """
    try:
        from zoneinfo import ZoneInfo
        import requests
        from app.frigate_api import FrigateAPI
    except ImportError:
        return set()
    url = cfg.get("frigate", {}).get("url")
    if not url:
        return set()
    api = FrigateAPI(url)
    tz = ZoneInfo(cfg.get("faceid", {}).get("timezone", "Europe/Berlin"))
    out, seen = set(), {}
    try:
        evs = requests.get(f"{url}/api/events",
                           params={"label": "person", "has_snapshot": 1, "limit": 300,
                                   "after": time.time() - days * 86400},
                           timeout=15).json()
    except Exception:
        return set()
    for ev in evs:
        cam = ev.get("camera")
        if not cam or seen.get(cam, 0) >= sample:
            continue
        hour = datetime.datetime.fromtimestamp(ev["start_time"], tz).hour
        if not (hour >= 21 or hour <= 5):
            continue
        img = api.snapshot(ev["id"], crop=True)
        if img is None:
            continue
        seen[cam] = seen.get(cam, 0) + 1
        if float(np.mean(cv2.cvtColor(img, cv2.COLOR_BGR2HSV)[:, :, 1])) < NIGHT_SAT:
            out.add(cam)
    return out


def load_config(base: Path) -> dict:
    """config.yaml plus die in der UI gesetzten Werte aus data/settings.json.

    Der Dienst legt settings.json ueber die config — ohne das misst ein Skript
    andere Schwellen als die, die tatsaechlich laufen.
    """
    cfg = yaml.safe_load((base / "config.yaml").read_text())
    sf = base / "data" / "settings.json"
    if sf.exists():
        try:
            cfg.setdefault("faceid", {}).update(json.loads(sf.read_text()))
        except (OSError, json.JSONDecodeError):
            pass
    return cfg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(BASE / "data"))
    args = ap.parse_args()

    sys.path.insert(0, str(BASE))
    from app.engine import FaceEngine

    cfg = load_config(BASE)
    thr = float(cfg["faceid"].get("match_threshold", 0.5))
    global TOP_K
    TOP_K = max(1, int(cfg["faceid"].get("match_top_k", 3)))
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
                       "files": meta.get("files", []), "dir": d,
                       "sources": meta.get("sources", {})}

    if not gal:
        print("no gallery found", file=sys.stderr)
        return 1

    print(f"{'person':22s} {'photos':>6s} {'divers':>6s} {'front':>6s} {'half':>5s} "
          f"{'profile':>7s} {'grey':>5s} {'self':>5s}  cameras")
    print("-" * 96)

    # Welche Kameras nachts ueberhaupt in Graustufen schalten? Das aus der Galerie
    # abzuleiten waere zirkulaer — fehlt die IR-Aufnahme, faende man die Kamera nie.
    # Also bei Frigate nachsehen, wie die Naechte dort tatsaechlich aussehen.
    ir_cams = ir_cameras(cfg)
    if ir_cams:
        print(f"cameras that switch to IR at night: {', '.join(sorted(ir_cams))}\n")

    advice = []
    for slug, g in gal.items():
        emb, n = g["emb"], len(g["emb"])
        sims = emb @ emb.T
        np.fill_diagonal(sims, np.nan)
        diversity = float(np.nanmean(sims)) if n > 1 else float("nan")

        cams = Counter()
        angles, night = [], 0
        for fn in g["files"]:
            src = g["sources"].get(fn) or {}
            cams[src.get("camera") or "?"] += 1
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
        known = {c: k for c, k in cams.items() if c != "?"}
        cam_s = ", ".join(f"{c} {k}" for c, k in sorted(known.items(), key=lambda x: -x[1]))
        if cams.get("?"):
            cam_s = (cam_s + ", " if cam_s else "") + f"{cams['?']}x unknown"
        print(f"{g['name'][:22]:22s} {n:6d} {div_s:>6s} {front:>6d} {half:>5d} "
              f"{prof:>7d} {night:>5d} {rate:>4d}%  {cam_s}")

        missing = []
        if n < 5:
            missing.append("too few photos")
        if not front:
            missing.append("no frontal shot")
        # S/W nur anmahnen, wo ueberhaupt eine Kamera in den IR-Modus schaltet.
        if ir_cams and not night and (known.keys() & ir_cams):
            missing.append("no IR night shot")
        if len(known) == 1 and n >= 3:
            missing.append(f"only one camera ({next(iter(known))})")
        if n > 1 and diversity > 0.55:
            missing.append(f"photos look very alike ({diversity:.2f})")
        # Der Selbst-Test ist bei kleinen Sammlungen systematisch pessimistisch —
        # als Mangel nur melden, wenn genug Fotos da sind, dass er aussagt.
        if n >= 8 and rate < 40:
            missing.append(f"photos barely support each other (self test {rate}%)")
        if missing:
            advice.append((rate, g["name"], missing))

    if advice:
        print("\nWhat is concretely missing (weakest first):")
        for rate, name, missing in sorted(advice):
            print(f"  {name}: {', '.join(missing)}")
    else:
        print("\nEvery person is broadly covered.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
