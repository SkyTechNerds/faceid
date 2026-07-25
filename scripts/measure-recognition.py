"""Hat das Anlernen die Erkennung verbessert?

Zwei Messungen, beide ohne manuelles Nachprüfen:

1. Leave-one-out über die Galerie — harter Test mit echtem Ground Truth: jedes Foto
   muss sich gegen die übrigen behaupten, aus denen es selbst entfernt wurde.
2. Praxisprobe an frischen Frigate-Ereignissen — zeigt die Abdeckung im Alltag.

Mit ``--baseline`` wird gegen eine ältere Galerie verglichen (z. B. ein entpacktes
Backup aus data/backups), sonst wird nur der Ist-Zustand vermessen.

    python scripts/measure-recognition.py --baseline /tmp/alt --days 2
"""
import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

import numpy as np
import requests
import yaml

BASE = Path(__file__).resolve().parent.parent
TOP_K = 3
FAVORITES = set()   # wird aus meta.json gefuellt
SELF_HIT = 0.99     # darueber ist es dasselbe Bild, kein echter Test


def load_gallery(persons_dir: Path):
    """{slug: (name, embeddings NxD)}"""
    out = {}
    for d in sorted(p for p in persons_dir.glob("*") if p.is_dir()):
        emb_f, meta_f = d / "embeddings.npy", d / "meta.json"
        if not (emb_f.exists() and meta_f.exists()):
            continue
        try:
            emb = np.load(emb_f).astype(np.float32)
            name = json.loads(meta_f.read_text()).get("name", d.name)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if emb.ndim == 2 and len(emb):
            out[d.name] = (name, emb)
            try:
                if json.loads(meta_f.read_text()).get("favorite"):
                    FAVORITES.add(name)
            except (OSError, json.JSONDecodeError):
                pass
    return out


def match(gal, e, drop_slug=None, drop_idx=None):
    """Beste Person per top-k-Mittel; optional ein Embedding ausschließen."""
    best_slug, best_name, best = None, None, 0.0
    for slug, (name, emb) in gal.items():
        sims = emb @ e
        if slug == drop_slug and drop_idx is not None:
            sims = np.delete(sims, drop_idx)
        if not len(sims):
            continue
        k = min(TOP_K, len(sims))
        s = float(np.mean(np.sort(sims)[-k:]))
        if s > best:
            best_slug, best_name, best = slug, name, s
    return best_slug, best_name, best


def find_same(gal, slug, e):
    """Index desselben Fotos in einer anderen Galerie (Embedding ~identisch)."""
    if slug not in gal:
        return None
    d = np.abs(gal[slug][1] @ e - 1.0)
    return int(d.argmin()) if len(d) and d.min() < 1e-4 else None


def leave_one_out(test_gal, gal, thr):
    hit = wrong = 0
    scores = []
    per_person = Counter()
    n = 0
    for slug, (_, emb) in test_gal.items():
        for i in range(len(emb)):
            e = emb[i]
            drop = i if gal is test_gal else find_same(gal, slug, e)
            got, _, sc = match(gal, e, slug, drop)
            n += 1
            scores.append(sc)
            if sc >= thr:
                if got == slug:
                    hit += 1
                    per_person[slug] += 1
                else:
                    wrong += 1
    return {"n": n, "hit": hit, "wrong": wrong, "miss": n - hit - wrong,
            "score": float(np.mean(scores)) if scores else 0.0, "per_person": per_person}


def live_probe(gal_old, gal_new, days, thr, cfg):
    sys.path.insert(0, str(BASE))   # app/ liegt eine Ebene ueber scripts/
    from app.engine import FaceEngine
    from app.frigate_api import FrigateAPI

    url = cfg["frigate"]["url"]
    eng = FaceEngine(det_size=int(cfg["faceid"].get("det_size", 640)))
    api = FrigateAPI(url)
    after = time.time() - days * 86400
    events, before = [], None
    while True:
        p = {"label": "person", "has_snapshot": 1, "limit": 100, "after": after}
        if before:
            p["before"] = before
        b = requests.get(f"{url}/api/events", params=p, timeout=15).json()
        if not b:
            break
        events.extend(b)
        before = b[-1]["start_time"]
        if len(b) < 100:
            break

    faces = hit_old = hit_new = disagree = selfhits = 0
    gained = Counter()
    per_person = {}   # Name -> Liste der Scores, zeigt den Abstand zur Schwelle
    for ev in events:
        img = api.snapshot(ev["id"], crop=True)
        if img is None:
            continue
        f = FaceEngine.best_face(eng.faces(img), min_px=64, min_det=0.65)
        if f is None:
            continue
        e = f.normed_embedding
        # k-unabhaengig: steht exakt dieses Bild schon in der Galerie?
        if max((float(np.max(emb @ e)) for _, emb in gal_new.values()), default=0.0) >= SELF_HIT:
            selfhits += 1
            continue
        faces += 1
        _, n_new, s_new = match(gal_new, e)
        ok_new = s_new >= thr
        hit_new += ok_new
        # Score ~1.0 heisst: exakt dieses Bild steht schon in der Galerie. Solche
        # Selbsttreffer messen nichts — sie wuerden vor allem kleine k schoenrechnen.
        if ok_new:
            per_person.setdefault(n_new, []).append(s_new)
        if gal_old:
            _, n_old, s_old = match(gal_old, e)
            ok_old = s_old >= thr
            hit_old += ok_old
            if ok_old and ok_new and n_old != n_new:
                disagree += 1
            if ok_new and not ok_old:
                gained[n_new] += 1
    return {"events": len(events), "faces": faces, "old": hit_old, "new": hit_new,
            "disagree": disagree, "gained": gained, "per_person": per_person,
            "selfhits": selfhits}


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
    ap.add_argument("--baseline", help="Pfad zu einer aelteren Galerie (entpacktes Backup)")
    ap.add_argument("--days", type=float, default=2, help="Zeitraum der Praxisprobe (0 = aus)")
    ap.add_argument("--data", default=str(BASE / "data"))
    ap.add_argument("--top-k", type=int, help="match_top_k zum Vergleich uebersteuern")
    args = ap.parse_args()

    global TOP_K
    if args.top_k:
        TOP_K = max(1, args.top_k)

    cfg = load_config(BASE)
    thr = float(cfg["faceid"].get("match_threshold", 0.5))
    if not args.top_k:
        TOP_K = max(1, int(cfg["faceid"].get("match_top_k", 3)))
    new = load_gallery(Path(args.data) / "persons")
    if not new:
        print("keine Galerie gefunden", file=sys.stderr)
        return 1
    old = None
    if args.baseline:
        p = Path(args.baseline)
        old = load_gallery(p / "persons" if (p / "persons").is_dir() else p)

    def size(g):
        return f"{len(g)} Personen / {sum(len(e) for _, e in g.values())} Fotos"

    print(f"jetzt:   {size(new)}")
    if old:
        print(f"vorher:  {size(old)}")
    print(f"Schwelle {thr}, gemittelt ueber {TOP_K} Foto(s)\n")

    # 1) Leave-one-out. Testmenge ist die aeltere Galerie, damit keine Seite ihre
    #    eigenen Neuzugaenge benotet.
    test = old or new
    r_new = leave_one_out(test, new, thr)
    print(f"Leave-one-out ({r_new['n']} Testgesichter)")
    print(f"{'':22s} {'vorher':>10s} {'jetzt':>10s}" if old else f"{'':22s} {'jetzt':>10s}")
    if old:
        r_old = leave_one_out(test, old, thr)
        for lbl, k in (("korrekt erkannt", "hit"), ("falsche Person", "wrong"),
                       ("nicht erkannt", "miss")):
            print(f"  {lbl:20s} {r_old[k]:>10d} {r_new[k]:>10d}")
        print(f"  {'mittlerer Score':20s} {r_old['score']:>10.3f} {r_new['score']:>10.3f}")
    else:
        for lbl, k in (("korrekt erkannt", "hit"), ("falsche Person", "wrong"),
                       ("nicht erkannt", "miss")):
            print(f"  {lbl:20s} {r_new[k]:>10d}")
        print(f"  {'mittlerer Score':20s} {r_new['score']:>10.3f}")

    if args.days > 0:
        print()
        lp = live_probe(old, new, args.days, thr, cfg)
        print(f"Praxisprobe ({args.days:g} Tage): {lp['events']} Ereignisse, "
              f"{lp['faces']} mit verwertbarem Gesicht"
              + (f" ({lp['selfhits']} Selbsttreffer ausgeschlossen)" if lp["selfhits"] else ""))
        f = max(lp["faces"], 1)
        if old:
            print(f"  erkannt vorher: {lp['old']:3d} ({100*lp['old']//f:3d}%)   "
                  f"jetzt: {lp['new']:3d} ({100*lp['new']//f:3d}%)")
            print(f"  beide erkannt, aber anderer Name: {lp['disagree']}")
            if lp["gained"]:
                print(f"  neu erkannt: {dict(lp['gained'])}")
        else:
            print(f"  erkannt: {lp['new']} ({100*lp['new']//f}%)")

        if lp["per_person"]:
            # Ein Treffer knapp ueber der Schwelle faellt bei schlechterem Licht weg —
            # deshalb zaehlt der Abstand, nicht nur das Ja/Nein.
            print("\n  Sicherheitsabstand je erkannter Person:")
            print(f"    {'Person':24s} {'Treffer':>7s} {'Median':>7s} {'min':>6s} {'knapp':>6s}")
            for name, scores in sorted(lp["per_person"].items(),
                                       key=lambda kv: -len(kv[1])):
                sc = sorted(scores)
                knapp = sum(1 for v in sc if v < thr + 0.05)
                mark = " *" if name in FAVORITES else ""
                print(f"    {(name + mark)[:24]:24s} {len(sc):>7d} "
                      f"{sc[len(sc)//2]:>7.2f} {sc[0]:>6.2f} {knapp:>6d}")
            if FAVORITES:
                print("    * = Favorit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
