"""Erkennungsqualität messen — dieselbe Rechnung für CLI und Weboberfläche.

Bis hierher lagen diese Auswertungen nur in ``scripts/`` und brauchten eine Shell.
Wer FaceID als Home-Assistant-App betreibt, hat keine — konnte also Schwelle und
``match_top_k`` verstellen, ohne die Wirkung je zu sehen. Genau der Rat, den das
Projekt allen anderen gibt (messen statt raten), war für diese Gruppe nicht befolgbar.

Zwei Auswertungen:

* **Leave-one-out** über die Galerie. Jedes Referenzfoto wird gegen die Sammlung
  geprüft, aus der es selbst entfernt wurde — dadurch echter Ground Truth ohne
  manuelles Etikettieren.
* **Praxisprobe** an echten Frigate-Ereignissen: wie viel Luft jede Erkennung über
  der Schwelle hat, und wie hoch der beste FALSCHE Treffer kommt. Letzteres ist die
  Zahl, die entscheidet, wie weit man die Schwelle senken darf.
"""
import json
import logging
import time
from pathlib import Path

import numpy as np

log = logging.getLogger("faceid.analysis")

SELF_HIT = 0.99   # darüber ist es dasselbe Bild — misst nichts, siehe unten


def _gallery(persons_dir: Path):
    """{slug: {"name", "emb", "fav"}} direkt von der Platte, ohne den Live-Cache."""
    out = {}
    for d in sorted(p for p in persons_dir.glob("*") if p.is_dir()):
        try:
            emb = np.load(d / "embeddings.npy").astype(np.float32)
            meta = json.loads((d / "meta.json").read_text())
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if emb.ndim == 2 and len(emb):
            out[d.name] = {"name": meta.get("name", d.name), "emb": emb,
                           "fav": bool(meta.get("favorite"))}
    return out


def _match(gal, e, top_k, drop_slug=None, drop_idx=None):
    """Beste Person per top-k-Mittel; optional ein Embedding ausschließen."""
    best_slug, best_name, best = None, None, 0.0
    for slug, g in gal.items():
        sims = g["emb"] @ e
        if slug == drop_slug and drop_idx is not None:
            sims = np.delete(sims, drop_idx)
        if not len(sims):
            continue
        k = min(top_k, len(sims))
        s = float(np.mean(np.sort(sims)[-k:]))
        if s > best:
            best_slug, best_name, best = slug, g["name"], s
    return best_slug, best_name, best


def leave_one_out(gal, thr: float, top_k: int) -> dict:
    """Harter Selbsttest mit Ground Truth: erkennt die Galerie ihre eigenen Fotos?

    Bewusst streng — bei wenigen, sehr verschiedenen Fotos muss eines gegen lauter
    fremde Situationen bestehen. Eine niedrige Rate heißt "die Fotos stützen einander
    wenig", nicht "diese Person wird im Betrieb nicht erkannt".
    """
    hit = wrong = 0
    scores, per_person = [], {}
    n = 0
    for slug, g in gal.items():
        for i in range(len(g["emb"])):
            got, _, sc = _match(gal, g["emb"][i], top_k, slug, i)
            n += 1
            scores.append(sc)
            ok = got == slug and sc >= thr
            p = per_person.setdefault(g["name"], {"n": 0, "hit": 0, "fav": g["fav"]})
            p["n"] += 1
            if sc >= thr:
                if got == slug:
                    hit += 1
                    p["hit"] += 1
                else:
                    wrong += 1
    return {"n": n, "hit": hit, "wrong": wrong, "miss": n - hit - wrong,
            "score": round(float(np.mean(scores)), 3) if scores else 0.0,
            "per_person": per_person}


def stranger_ceiling(gal, ignored_dir: Path, top_k: int) -> dict:
    """Wie hoch kommen Fremde? Die Zahl entscheidet über die Schwelle.

    Zwei Quellen: die Ignoriert-Anker (echte Nicht-Familienmitglieder) und, als
    Ersatz falls es keine gibt, der beste Treffer jedes Galeriefotos bei einer
    FALSCHEN Person.
    """
    best_anchor = 0.0
    n_anchor = 0
    for jf in sorted(ignored_dir.glob("*.json")) if ignored_dir.is_dir() else []:
        try:
            e = np.array(json.loads(jf.read_text())["embedding"], dtype=np.float32)
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            continue
        n_anchor += 1
        _, _, sc = _match(gal, e, top_k)
        best_anchor = max(best_anchor, sc)

    worst_cross = 0.0
    for slug, g in gal.items():
        for i in range(len(g["emb"])):
            e = g["emb"][i]
            for s2, g2 in gal.items():
                if s2 == slug:
                    continue
                sims = g2["emb"] @ e
                if len(sims):
                    k = min(top_k, len(sims))
                    worst_cross = max(worst_cross, float(np.mean(np.sort(sims)[-k:])))
    return {"anchors": n_anchor, "anchor_max": round(best_anchor, 3),
            "cross_max": round(worst_cross, 3)}


def live_probe(gal, engine, frigate, cfg, days: float, thr: float, top_k: int,
               progress=None) -> dict:
    """Praxisprobe an echten Ereignissen — wie viel Luft hat jede Erkennung?

    Ereignisse, deren Gesicht bereits in der Galerie steht, fliegen raus: die
    erreichen ~1.0 und messen nichts, würden aber kleine ``top_k`` schöner aussehen
    lassen als sie sind.
    """
    from .engine import FaceEngine

    after = time.time() - days * 86400
    events, before = [], None
    while True:
        params = {"label": "person", "has_snapshot": 1, "limit": 100, "after": after}
        if before:
            params["before"] = before
        batch = frigate.events(**params)
        if not batch:
            break
        events.extend(batch)
        before = batch[-1]["start_time"]
        if len(batch) < 100:
            break

    faces = hits = selfhits = 0
    per_person = {}
    for i, ev in enumerate(events):
        if progress:
            progress(i + 1, len(events))
        img = frigate.snapshot(ev["id"], crop=True)
        if img is None:
            continue
        f = FaceEngine.best_face(engine.faces(img),
                                 min_px=int(cfg["faceid"].get("min_face_px", 48)),
                                 min_det=0.65)
        if f is None:
            continue
        e = f.normed_embedding
        if max((float(np.max(g["emb"] @ e)) for g in gal.values()), default=0.0) >= SELF_HIT:
            selfhits += 1
            continue
        faces += 1
        _, name, sc = _match(gal, e, top_k)
        if sc >= thr:
            hits += 1
            per_person.setdefault(name, []).append(round(sc, 3))
    out = []
    for name, scores in sorted(per_person.items(), key=lambda kv: -len(kv[1])):
        s = sorted(scores)
        out.append({"name": name, "hits": len(s), "median": s[len(s) // 2],
                    "min": s[0], "tight": sum(1 for v in s if v < thr + 0.05)})
    return {"events": len(events), "faces": faces, "hits": hits,
            "selfhits": selfhits, "per_person": out}


def run(data_dir: Path, engine, frigate, cfg, days: float = 3.0,
        progress=None) -> dict:
    """Beide Auswertungen mit den Werten, die gerade tatsächlich laufen."""
    f = cfg["faceid"]
    thr = float(f.get("match_threshold", 0.5))
    top_k = max(1, int(f.get("match_top_k", 3)))
    gal = _gallery(data_dir / "persons")
    if not gal:
        return {"error": "no gallery yet — assign a few faces first"}

    loo = leave_one_out(gal, thr, top_k)
    strangers = stranger_ceiling(gal, data_dir / "ignored", top_k)
    probe = live_probe(gal, engine, frigate, cfg, days, thr, top_k, progress) \
        if days > 0 else None

    # Wie weit ließe sich die Schwelle senken, ohne den höchsten je gemessenen
    # Fremdtreffer zu berühren? Bewusst mit Sicherheitsabstand statt auf Kante.
    ceiling = max(strangers["anchor_max"], strangers["cross_max"])
    headroom = round(max(0.0, thr - ceiling), 3)
    return {"threshold": thr, "top_k": top_k, "days": days,
            "persons": len(gal), "photos": sum(len(g["emb"]) for g in gal.values()),
            "leave_one_out": loo, "strangers": strangers,
            "stranger_ceiling": round(ceiling, 3), "headroom": headroom,
            "probe": probe, "ts": time.time()}
