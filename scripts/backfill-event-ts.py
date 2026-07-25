"""Ereigniszeit in bestehende Review-Karten nachtragen.

Karten aus einem Verlaufs-Scan von vor Version 0.6.3 tragen als Zeit den Moment der
Verarbeitung statt den des Ereignisses — bei einem Scan über vier Wochen steht dann
überall derselbe Tag. Die Ereignis-ID ist gespeichert, die echte Zeit lässt sich also
aus Frigate nachziehen.

    python scripts/backfill-event-ts.py [--dry-run]
"""
import argparse
import json
import sys
from pathlib import Path

import requests
import yaml

BASE = Path(__file__).resolve().parent.parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = yaml.safe_load((BASE / "config.yaml").read_text())
    url = cfg["frigate"]["url"].rstrip("/")
    d = BASE / "data" / "unknowns"
    if not d.is_dir():
        print("keine Review-Queue gefunden")
        return 1

    fixed = skipped = failed = 0
    session = requests.Session()
    for jf in sorted(d.glob("*.json")):
        try:
            m = json.loads(jf.read_text())
        except (json.JSONDecodeError, OSError):
            failed += 1
            continue
        if m.get("event_ts") or not m.get("event_id"):
            skipped += 1
            continue
        try:
            r = session.get(f"{url}/api/events/{m['event_id']}", timeout=10)
            ts = r.json().get("start_time") if r.status_code == 200 else None
        except (requests.RequestException, ValueError):
            ts = None
        if not ts:
            failed += 1  # Ereignis aus Frigate gelaufen — Karte bleibt wie sie ist
            continue
        fixed += 1
        if not args.dry_run:
            m["event_ts"] = ts
            jf.write_text(json.dumps(m, ensure_ascii=False))

    verb = "waere korrigiert" if args.dry_run else "korrigiert"
    print(f"{fixed} {verb}, {skipped} schon in Ordnung, {failed} ohne Frigate-Ereignis")
    return 0


if __name__ == "__main__":
    sys.exit(main())
