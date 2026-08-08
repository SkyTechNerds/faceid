"""Why do so few events yield a usable face?

The question behind most "it barely recognises anyone" reports. It is tempting to answer
it by tuning the gallery — but on this setup that turned out to be the wrong end entirely:
of 39 person events over seven days only 6 ever produced a face, so no gallery could have
made a difference.

This counts the reasons instead of guessing them, and separates the two cases that call
for completely different fixes:

  hopeless    the person is too far away — there are simply not enough pixels
  recoverable the person is large enough in frame, only that one snapshot moment is bad
              (turned away, motion blur, half occluded)

Frigate picks its snapshot by highest *person* score, which is not the same criterion as
"a face is visible". So the second case is common, and it is what ``clip_fallback`` fixes.
Pass --clip to check how many of your own events the recording would rescue.

    python scripts/why-no-face.py --days 7 --clip 12
"""
import argparse
import datetime
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import cv2
import numpy as np
import yaml

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

NIGHT_SAT = 25.0    # mean HSV saturation below this = greyscale, i.e. IR night mode

NO_SNAPSHOT = "no snapshot"
NO_DETECTION = "no face detected"
TOO_SMALL = "face too small"
UNCERTAIN = "detection uncertain"
OK = "usable"
ORDER = [NO_SNAPSHOT, NO_DETECTION, TOO_SMALL, UNCERTAIN, OK]


def load_config(base: Path) -> dict:
    """config.yaml plus die in der UI gesetzten Werte aus data/settings.json.

    Ohne den Overlay misst das Skript andere Schwellen als die, die laufen.
    """
    cfg = yaml.safe_load((base / "config.yaml").read_text())
    sf = base / "data" / "settings.json"
    if sf.exists():
        try:
            cfg.setdefault("faceid", {}).update(json.loads(sf.read_text()))
        except (OSError, json.JSONDecodeError):
            pass
    return cfg


def classify(faces, min_px: int, min_det: float):
    """-> (reason, widest face px, best det_score among the big enough ones)"""
    if not len(faces):
        return NO_DETECTION, 0.0, 0.0
    widths = [float(f.bbox[2] - f.bbox[0]) for f in faces]
    heights = [float(f.bbox[3] - f.bbox[1]) for f in faces]
    big = [f for f, w, h in zip(faces, widths, heights) if w >= min_px and h >= min_px]
    if not big:
        return TOO_SMALL, max(widths), max(float(f.det_score) for f in faces)
    best = max(float(f.det_score) for f in big)
    if best < min_det:
        return UNCERTAIN, max(widths), best
    return OK, max(widths), best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=7, help="how far back to look")
    ap.add_argument("--min-px", type=int, help="override min_face_px")
    # Betriebswert: _process ruft best_face() ohne min_det auf, dessen Default ist 0.55.
    # Mit einem strengeren Wert zu messen zaehlt Ereignisse als verloren, die live
    # durchgehen — genau der Fehler, den diese Datei vermeiden soll.
    ap.add_argument("--min-det", type=float, default=0.55,
                    help="detection score the live path requires (default matches it)")
    ap.add_argument("--clip", type=int, default=0,
                    help="also re-check this many discarded events against the recording")
    ap.add_argument("--frames", type=int, default=12)
    ap.add_argument("--clip-min-crop", type=int, default=120,
                    help="only re-check events whose crop is at least this wide")
    args = ap.parse_args()

    from zoneinfo import ZoneInfo

    from app.engine import FaceEngine
    from app.frigate_api import frigate_client
    from app.hires import find_face_in_clip

    cfg = load_config(BASE)
    f = cfg["faceid"]
    min_px = args.min_px if args.min_px else int(f.get("min_face_px", 48))
    tz = ZoneInfo(f.get("timezone", "Europe/Berlin"))
    eng = FaceEngine(det_size=int(f.get("det_size", 640)))
    api = frigate_client(cfg)

    after = time.time() - args.days * 86400
    events, before = [], None
    while True:
        p = {"label": "person", "has_snapshot": 1, "limit": 100, "after": after}
        if before:
            p["before"] = before
        batch = api.events(**p)
        if not batch:
            break
        events.extend(batch)
        before = batch[-1]["start_time"]
        if len(batch) < 100:
            break

    if not events:
        print("no person events in that window", file=sys.stderr)
        return 1

    print(f"window: {args.days:g} days, {len(events)} person events")
    print(f"same criteria as the live path: min_face_px={min_px}, min_det={args.min_det}\n")

    reasons = Counter()
    per_camera = defaultdict(Counter)
    per_light = defaultdict(Counter)
    crop_widths = defaultdict(list)
    face_widths = defaultdict(list)
    discarded = []

    for i, ev in enumerate(events, 1):
        print(f"\r  checking {i}/{len(events)} …", end="", file=sys.stderr, flush=True)
        cam = ev.get("camera", "?")
        img = api.snapshot(ev["id"], crop=True)
        if img is None:
            reason, cw, light = NO_SNAPSHOT, 0, "?"
        else:
            cw = img.shape[1]
            sat = float(np.mean(cv2.cvtColor(img, cv2.COLOR_BGR2HSV)[:, :, 1]))
            light = "IR/grey" if sat < NIGHT_SAT else "colour"
            reason, fw, _ = classify(eng.faces(img), min_px, args.min_det)
            if fw:
                face_widths[reason].append(fw)
        reasons[reason] += 1
        per_camera[cam][reason] += 1
        per_light[light][reason] += 1
        crop_widths[reason].append(cw)
        if reason != OK:
            discarded.append((ev, reason, cw, light))
    print("\r" + " " * 44 + "\r", end="", file=sys.stderr)

    n = max(1, len(events))
    print("===== where it fails =====")
    for r in ORDER:
        if not reasons[r]:
            continue
        cw = crop_widths[r]
        extra = ""
        if face_widths[r]:
            extra = f", face median {int(np.median(face_widths[r]))}px wide"
        print(f"  {r:<21} {reasons[r]:>3}  ({reasons[r] / n * 100:4.0f}%)"
              f"   crop median {int(np.median(cw)) if cw else 0}px wide{extra}")

    head = "  " + "camera".ljust(16) + "".join(r[:11].rjust(13) for r in ORDER)
    print("\n===== by camera =====")
    print(head)
    for cam, c in sorted(per_camera.items(), key=lambda kv: -sum(kv[1].values())):
        print("  " + cam.ljust(16) + "".join(str(c[r] or "").rjust(13) for r in ORDER))

    print("\n===== by light =====")
    print(head.replace("camera".ljust(16), "light".ljust(16)))
    for light, c in sorted(per_light.items(), key=lambda kv: -sum(kv[1].values())):
        print("  " + light.ljust(16) + "".join(str(c[r] or "").rjust(13) for r in ORDER))

    # Ein Ausschnitt, der schmaler ist als das geforderte Gesicht, kann auch im Clip
    # keines enthalten — diese Ereignisse sind nicht zu retten, egal wie oft man sucht.
    recoverable = [d for d in discarded if d[2] >= args.clip_min_crop]
    print("\n===== outlook =====")
    print(f"  discarded in total:            {len(discarded)}")
    print(f"  crop narrower than {args.clip_min_crop}px:      "
          f"{len(discarded) - len(recoverable)}  (too far away — nothing to find)")
    print(f"  wide enough:                   {len(recoverable)}  (bad snapshot moment — "
          f"the recording may hold a face)")

    if args.clip and recoverable:
        check = recoverable[:args.clip]
        print(f"\n===== recording cross-check ({len(check)} events, {args.frames} frames) =====")
        found = 0
        for i, (ev, reason, cw, light) in enumerate(check, 1):
            print(f"\r  scanning {i}/{len(check)} …", end="", file=sys.stderr, flush=True)
            hit = find_face_in_clip(eng, api, ev["id"], max_frames=args.frames,
                                    min_px=min_px, min_det=0.65)
            ts = datetime.datetime.fromtimestamp(ev["start_time"], tz).strftime("%d.%m. %H:%M")
            if hit is None:
                print(f"  {ts}  {ev.get('camera', '?'):<14} {light:<8} {reason:<21} no")
                continue
            found += 1
            face, _ = hit
            print(f"  {ts}  {ev.get('camera', '?'):<14} {light:<8} {reason:<21}"
                  f" YES  {int(face.bbox[2] - face.bbox[0])}px det {float(face.det_score):.2f}")
        print(f"\n  snapshot empty, recording has a face: {found}/{len(check)}")
        if found:
            print(f"  scaled to all {len(recoverable)} recoverable events: "
                  f"{reasons[OK]} -> ~{reasons[OK] + round(found / len(check) * len(recoverable))}"
                  f" usable events")
        print("\n  Enable it with Settings -> Search the recording (clip_fallback).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
