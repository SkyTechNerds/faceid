"""Auswertungen, die es bisher nur als Skript in ``scripts/`` gab.

Das ist keine Bequemlichkeitskopie. Wer FaceID als Home-Assistant-App betreibt, hat
``scripts/`` überhaupt nicht — das Dockerfile kopiert nur ``app/`` und ``static/`` —
und ``measure-delay.py`` liest ``journalctl``, das es in einem Container ohne systemd
gar nicht gibt. Für genau die Leute ohne Shell waren diese Auswertungen also nie
erreichbar. Hier laufen sie serverseitig und brauchen weder das eine noch das andere.

Die Verzögerungsmessung kommt deshalb auch aus einer anderen Quelle als das Skript:
nicht aus dem Log, sondern aus dem Verlauf. Der Ereignisbeginn steckt ohnehin in der
Frigate-Ereignis-ID (``1788331891.926818-xyuhgt``), der Meldezeitpunkt im Eintrag.
"""
from __future__ import annotations

import datetime
import json
import time
from collections import Counter
from pathlib import Path

import cv2
import numpy as np

FRONTAL, HALF = 0.55, 0.25   # Symmetrie-Grenzen frontal / halb / Profil
NIGHT_SAT = 12               # mittlere HSV-Sättigung darunter = Graustufen/IR

# Erkennungen aus dem Verlaufs-Scan liegen Wochen hinter ihrem Ereignis. Als
# Verzögerung gezählt würden sie jeden Median zerlegen, also fliegen sie raus —
# aber sichtbar, nicht stillschweigend.
LIVE_MAX_DELAY = 300.0


def _stats(vals: list) -> dict:
    """Median und Ränder. Der Mittelwert wäre hier irreführend: ein einzelnes
    Ereignis, das erst nach zwei Minuten ein Gesicht hergibt, verschiebt ihn weit."""
    if not vals:
        return {"n": 0}
    s = sorted(vals)
    return {"n": len(s), "median": round(float(np.median(s)), 1),
            "p10": round(float(np.percentile(s, 10)), 1),
            "p90": round(float(np.percentile(s, 90)), 1),
            "min": round(s[0], 1), "max": round(s[-1], 1)}


def delay(history_dir: Path, days: float = 0.0) -> dict:
    """Wie lange dauert es vom Ereignisbeginn bis zum gemeldeten Namen?

    Untergrenze ist nicht FaceID, sondern Frigate: vor dem ersten Schnappschuss gibt
    es nichts zu erkennen. Die Aufschlüsselung nach Versuch zeigt genau das — Versuch 1
    ist die Zeit bis zum ersten brauchbaren Bild, jeder weitere kostet den Abstand.
    """
    if not history_dir.is_dir():
        return {"error": "no history yet — recognitions are recorded from now on"}

    after = time.time() - days * 86400 if days else 0.0
    rows, late = [], 0
    for jf in history_dir.glob("*.json"):
        try:
            h = json.loads(jf.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        ev, ts = h.get("event_id"), h.get("ts")
        if not ev or not ts or float(ts) < after:
            continue
        try:
            start = float(str(ev).split("-")[0])
        except ValueError:
            continue
        d = float(ts) - start
        if d < 0:
            continue
        if d > LIVE_MAX_DELAY:
            late += 1
            continue
        rows.append({"d": d, "ts": float(ts), "cam": h.get("camera") or "?",
                     "attempt": h.get("attempt"), "person": h.get("person")})

    if not rows:
        return {"n": 0, "skipped_late": late,
                "note": "no recognitions in this window"}

    by_attempt, by_cam, by_day = {}, {}, {}
    for r in rows:
        if r["attempt"] is not None:
            by_attempt.setdefault(int(r["attempt"]), []).append(r["d"])
        by_cam.setdefault(r["cam"], []).append(r["d"])
        day = datetime.date.fromtimestamp(r["ts"]).isoformat()
        by_day.setdefault(day, []).append(r["d"])

    vals = [r["d"] for r in rows]
    return {
        **_stats(vals),
        "days": days,
        "skipped_late": late,
        "first": min(r["ts"] for r in rows),
        "last": max(r["ts"] for r in rows),
        "by_attempt": [{"attempt": k, **_stats(v)} for k, v in sorted(by_attempt.items())],
        "by_camera": [{"camera": k, **_stats(v)}
                      for k, v in sorted(by_cam.items(), key=lambda kv: -len(kv[1]))],
        "by_day": [{"day": k, **_stats(v)} for k, v in sorted(by_day.items())],
    }


def _yaw_symmetry(kps):
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


def _match(gal, e, top_k, drop_slug, drop_idx):
    best_slug, best = None, 0.0
    for slug, g in gal.items():
        sims = g["emb"] @ e
        if slug == drop_slug:
            sims = np.delete(sims, drop_idx)
        if not len(sims):
            continue
        k = min(top_k, len(sims))
        s = float(np.mean(np.sort(sims)[-k:]))
        if s > best:
            best_slug, best = slug, s
    return best_slug, best


def _ir_cameras(frigate, cfg, days: int = 7, sample: int = 8) -> set:
    """Kameras, deren Nachtaufnahmen in Graustufen kommen (echte IR-Umschaltung).

    Wo bei Bewegung Licht angeht, nimmt die Kamera auch nachts in Farbe auf — dort ist
    eine fehlende IR-Referenz kein Mangel. Aus echten Ereignissen ermittelt statt aus
    der Galerie, sonst übersähe man genau die Lücke, die man sucht.
    """
    if frigate is None:
        return set()
    # Nur Kameras, die FaceID ueberhaupt verarbeitet. Sonst raet die Auswertung zu einer
    # IR-Aufnahme an einer Kamera, die gar nicht mehr in Betrieb ist — genau das ist hier
    # passiert, nachdem eine Kamera aus der Liste geflogen war.
    wanted = set(cfg.get("faceid", {}).get("cameras") or [])
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(cfg.get("faceid", {}).get("timezone", "Europe/Berlin"))
        evs = frigate.events(label="person", has_snapshot=1, limit=300,
                             after=time.time() - days * 86400) or []
    except Exception:
        return set()
    out, seen = set(), Counter()
    for ev in evs:
        cam = ev.get("camera")
        if not cam or seen[cam] >= sample or (wanted and cam not in wanted):
            continue
        hour = datetime.datetime.fromtimestamp(ev["start_time"], tz).hour
        if not (hour >= 21 or hour <= 5):
            continue
        img = frigate.snapshot(ev["id"], crop=True)
        if img is None:
            continue
        seen[cam] += 1
        if float(np.mean(cv2.cvtColor(img, cv2.COLOR_BGR2HSV)[:, :, 1])) < NIGHT_SAT:
            out.add(cam)
    return out


def coverage(data_dir: Path, engine, frigate, cfg, progress=None) -> dict:
    """Wie gut ist jede Person abgedeckt — und welche Aufnahme fehlt ihr konkret?

    Mehr Fotos heißen nicht bessere Erkennung, entscheidend ist die Bandbreite:
    Blickwinkel, Kameras, Tag und Nacht. Der Selbst-Test am Ende ist bewusst hart und
    bei kleinen Sammlungen systematisch pessimistisch — bei fünf vielfältigen Fotos
    muss eines gegen vier ganz andere Situationen bestehen. Eine niedrige Rate heißt
    „die Fotos stützen einander wenig", nicht „die Person wird nicht erkannt".
    """
    from .engine import FaceEngine

    thr = float(cfg["faceid"].get("match_threshold", 0.5))
    top_k = max(1, int(cfg["faceid"].get("match_top_k", 3)))

    gal = {}
    for d in sorted(p for p in (data_dir / "persons").glob("*") if p.is_dir()):
        try:
            emb = np.load(d / "embeddings.npy").astype(np.float32)
            meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if emb.ndim != 2 or not len(emb):
            continue
        gal[d.name] = {"name": meta.get("name", d.name), "emb": emb,
                       "files": meta.get("files", []), "dir": d,
                       "sources": meta.get("sources", {})}
    if not gal:
        return {"error": "no gallery yet — assign a few faces first"}

    ir_cams = _ir_cameras(frigate, cfg)
    total = sum(len(g["files"]) for g in gal.values())
    done = 0
    people, advice = [], []

    for slug, g in gal.items():
        emb, n = g["emb"], len(g["emb"])
        sims = emb @ emb.T
        np.fill_diagonal(sims, np.nan)
        diversity = float(np.nanmean(sims)) if n > 1 else None

        cams, angles, night = Counter(), [], 0
        for fn in g["files"]:
            done += 1
            if progress:
                progress(done, total)
            src = g["sources"].get(fn) or {}
            cams[src.get("camera") or "?"] += 1
            img = cv2.imread(str(g["dir"] / fn))
            if img is None:
                continue
            if float(np.mean(cv2.cvtColor(img, cv2.COLOR_BGR2HSV)[:, :, 1])) < NIGHT_SAT:
                night += 1
            f = FaceEngine.best_face(engine.faces(img), min_px=20, min_det=0.4)
            if f is not None:
                s = _yaw_symmetry(f.kps)
                if s is not None:
                    angles.append(s)

        front = sum(1 for a in angles if a >= FRONTAL)
        half = sum(1 for a in angles if HALF <= a < FRONTAL)
        prof = sum(1 for a in angles if a < HALF)

        hit = 0
        for i in range(n):
            got, sc = _match(gal, emb[i], top_k, slug, i)
            if got == slug and sc >= thr:
                hit += 1
        rate = 100 * hit // n if n else 0

        known = {c: k for c, k in cams.items() if c != "?"}
        people.append({"name": g["name"], "photos": n,
                       "diversity": None if diversity is None else round(diversity, 2),
                       "frontal": front, "half": half, "profile": prof,
                       "greyscale": night, "self_test": rate,
                       "cameras": [{"camera": c, "n": k}
                                   for c, k in sorted(known.items(), key=lambda x: -x[1])],
                       "unknown_camera": cams.get("?", 0)})

        missing = []
        if n < 5:
            missing.append("too few photos")
        if not front:
            missing.append("no frontal shot")
        if ir_cams and not night and (known.keys() & ir_cams):
            missing.append("no IR night shot")
        if len(known) == 1 and n >= 3:
            missing.append(f"only one camera ({next(iter(known))})")
        if diversity is not None and diversity > 0.55:
            missing.append(f"photos look very alike ({diversity:.2f})")
        if n >= 8 and rate < 40:
            missing.append(f"photos barely support each other (self test {rate}%)")
        if missing:
            advice.append({"name": g["name"], "self_test": rate, "missing": missing})

    people.sort(key=lambda p: p["name"].lower())
    advice.sort(key=lambda a: a["self_test"])
    return {"threshold": thr, "top_k": top_k, "people": people, "advice": advice,
            "ir_cameras": sorted(ir_cams)}


NO_DETECTION, TOO_SMALL, UNCERTAIN, OK = "no face", "too small", "uncertain", "ok"


def why_no_face(engine, frigate, cfg, days: float = 3.0, progress=None) -> dict:
    """Warum liefern so viele Ereignisse kein brauchbares Gesicht?

    Trennt die drei Gründe, die sich sonst zu einem „wird nicht erkannt" vermischen:
    gar kein Gesicht im Bild, eines das zu klein ist, oder eines das die Erkennung
    selbst nicht sicher genug findet. Erst die Aufschlüsselung nach Kamera zeigt, ob
    ein Standort etwas taugt — oder nur Rücken und Hinterköpfe liefert.
    """
    from .engine import FaceEngine

    if frigate is None:
        return {"error": "no Frigate connection configured"}
    min_px = int(cfg["faceid"].get("min_face_px", 48))
    min_det = 0.65

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

    reasons, per_cam, widths = Counter(), {}, []
    for i, ev in enumerate(events):
        if progress:
            progress(i + 1, len(events))
        img = frigate.snapshot(ev["id"], crop=True)
        if img is None:
            reasons["no snapshot"] += 1
            continue
        faces = engine.faces(img)
        if not len(faces):
            reason, w, det = NO_DETECTION, 0.0, 0.0
        else:
            ws = [float(f.bbox[2] - f.bbox[0]) for f in faces]
            hs = [float(f.bbox[3] - f.bbox[1]) for f in faces]
            big = [f for f, w, h in zip(faces, ws, hs) if w >= min_px and h >= min_px]
            if not big:
                reason, w, det = TOO_SMALL, max(ws), max(float(f.det_score) for f in faces)
            else:
                det = max(float(f.det_score) for f in big)
                w = max(ws)
                reason = UNCERTAIN if det < min_det else OK
        reasons[reason] += 1
        if w:
            widths.append(w)
        cam = ev.get("camera") or "?"
        per_cam.setdefault(cam, Counter())[reason] += 1

    # Bewusst ueber ALLE Kameras, nicht nur die verarbeiteten: die Frage lautet ja
    # gerade, ob sich eine Kamera lohnt. Welche in Betrieb sind, steht daneben.
    wanted = set(cfg["faceid"].get("cameras") or [])
    cams = [{"camera": c, "events": sum(r.values()), "ok": r[OK],
             "no_face": r[NO_DETECTION], "too_small": r[TOO_SMALL],
             "uncertain": r[UNCERTAIN], "used": not wanted or c in wanted}
            for c, r in sorted(per_cam.items(), key=lambda kv: -sum(kv[1].values()))]
    return {"days": days, "events": len(events), "min_face_px": min_px,
            "reasons": dict(reasons), "cameras": cams,
            "face_width": _stats(widths) if widths else {"n": 0}}
