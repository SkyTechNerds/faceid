#!/usr/bin/env python3
"""How long does a recognition take — and where does the time go?

Reads the service log and measures, for every published recognition, the delay between
the start of the Frigate event and the moment FaceID published a name.

The answer is rarely "FaceID is slow". The floor is Frigate's own time-to-first-snapshot;
everything above it is waiting between attempts (``retry_seconds``), which shows up as a
clear staircase in the per-attempt medians. Run this before changing ``retry_seconds`` and
again afterwards.

    python3 scripts/measure-delay.py                # last 7 days
    python3 scripts/measure-delay.py --days 3
    python3 scripts/measure-delay.py --since "2026-08-27 10:15"   # after a config change

``--since`` is what you want after changing a setting: it ignores everything recorded
before, so the two runs do not blend into each other.
"""
import argparse
import datetime
import re
import statistics as st
import subprocess
import sys

LINE = re.compile(
    r"^(?P<date>\S+)T(?P<time>\S+?)[+.].*"
    r"event (?P<start>\d+)\.\d+-\w+ \((?P<cam>\w+)\): "
    r"attempt (?P<attempt>\d+), best match .*?\((?P<score>[\d.]+)\)(?P<via>.*?)— published"
)


def rows(days: int, since: str | None):
    cmd = ["journalctl", "-u", "faceid", "--no-pager", "-o", "short-iso",
           "--since", since or f"{days} days ago"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=False).stdout
    except FileNotFoundError:
        sys.exit("journalctl not found — run this on the machine where FaceID runs.")
    for line in out.splitlines():
        m = LINE.search(line)
        if not m:
            continue
        when = datetime.datetime.fromisoformat(f"{m['date']}T{m['time']}")
        delay = when.timestamp() - int(m["start"])
        if delay < 0 or delay > 600:      # Uhr-/Parsing-Ausreisser nicht mitrechnen
            continue
        via = m["via"]
        path = ("recording" if "recording" in via
                else "live frame" if "live frame" in via else "snapshot")
        yield delay, int(m["attempt"]), path, m["cam"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--since", help='e.g. "2026-08-27 10:15" — overrides --days')
    a = ap.parse_args()

    data = list(rows(a.days, a.since))
    if not data:
        sys.exit("No published recognitions found in that window.")
    ds = sorted(r[0] for r in data)
    print(f"{len(data)} published recognitions "
          f"({a.since or f'last {a.days} days'})\n")
    print(f"  median {st.median(ds):.1f}s   fastest {min(ds):.1f}s   slowest {max(ds):.1f}s")
    for label, lo, hi in (("under 3s", 0, 3), ("3-10s", 3, 10),
                          ("10-30s", 10, 30), ("over 30s", 30, 1e9)):
        n = sum(1 for d in ds if lo <= d < hi)
        print(f"    {label:9} {n:4}  ({n / len(ds) * 100:3.0f}%)")

    print("\nBy attempt — each failed attempt costs retry_seconds:")
    for att in sorted({r[1] for r in data}):
        sel = [r[0] for r in data if r[1] == att]
        print(f"  attempt {att}: {len(sel):4}   median {st.median(sel):5.1f}s   "
              f"fastest {min(sel):5.1f}s")

    print("\nBy path:")
    for path in ("snapshot", "live frame", "recording"):
        sel = [r[0] for r in data if r[2] == path]
        if sel:
            print(f"  {path:11} {len(sel):4} ({len(sel) / len(data) * 100:3.0f}%)   "
                  f"median {st.median(sel):5.1f}s")

    first = [r[0] for r in data if r[1] == 1 and r[2] == "snapshot"]
    if first:
        print(f"\nFloor: {len(first)} hits came from the first snapshot with no detour, "
              f"median {st.median(first):.1f}s.")
        print("That is Frigate's time-to-first-snapshot. No FaceID setting goes below it.")


if __name__ == "__main__":
    main()
