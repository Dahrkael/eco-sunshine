#!/usr/bin/env python3
"""Extracts the real 930's corpus (Bumper log) into a compact JSON for the mock.

One streaming pass over the log (681 MB): keeps only the last GetMapM
spec, the last piece per pid, the Pos trajectory, the varint trace and
samples of BatteryInfo/ChargeState/CleanSt. The result feeds
tools/mock_930_robot.py so it behaves EXACTLY like the real robot.

Usage:
    python tools/extract_930_corpus.py [log] [-o corpus.json]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

BOT_FRAME = re.compile(
    r"from \(\d+\.\d+\.\d+\.\d+:\d+ \| E0001057\d+@115\.ecorobot\.net/atom\) - "
    r"<iq xmlns:ns0=\"com:ctl\" [^>]*to=\"([^\"]+)\"[^>]*>(.*)</iq>"
)
ATTR = re.compile(r'(\w+)="([^"]*)"')

def parse_ctl(inner: str) -> dict:
    m = re.search(r"<ctl ([^>]*)/?>", inner)
    return dict(ATTR.findall(m.group(1))) if m else {}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("log", nargs="?", default="data/captures/pairing-session.log")
    ap.add_argument("-o", "--out", default="data/mock930-corpus.json")
    args = ap.parse_args()

    log_path = Path(args.log)
    if not log_path.exists():
        print(f"log not found: {log_path}", file=sys.stderr)
        return 1

    spec: dict | None = None          # last GetMapM reply (ctl attrs)
    pieces: dict[str, str] = {}       # pid → last piece b64
    positions: list[tuple[str, str]] = []  # (p, a) in order
    trace: str | None = None          # last varint tr
    battery: dict | None = None
    charge: dict | None = None
    clean_st: dict | None = None
    lifespan: dict[str, dict] = {}
    charge_pos: dict | None = None

    n_frames = 0
    with open(log_path, errors="replace") as fh:
        for line in fh:
            m = BOT_FRAME.search(line)
            if not m:
                continue
            n_frames += 1
            attrs = parse_ctl(m.group(2))
            td = attrs.get("td", "")
            if "m" in attrs and "i" in attrs and "w" in attrs and "c" in attrs:
                spec = attrs
            elif td == "MapP":
                pieces[attrs.get("pid", "?")] = attrs.get("p", "")
            elif td == "Pos":
                if attrs.get("valid") == "1" and "," in attrs.get("p", ""):
                    positions.append((attrs["p"], attrs.get("a", "0")))
            elif td == "trace" and attrs.get("tr"):
                trace = attrs["tr"]
            elif td == "BatteryInfo":
                bat = re.search(r'power="(\d+)"', m.group(2))
                if bat:
                    battery = {"power": bat.group(1)}
            elif td == "ChargeState":
                ch = re.search(r'type="([^"]*)"', m.group(2))
                if ch:
                    charge = {"type": ch.group(1)}
            elif td == "CleanSt":
                clean_st = attrs
            elif "left" in attrs and "total" in attrs:
                lifespan[attrs.get("type", "?")] = {
                    "left": attrs["left"], "total": attrs["total"]}
            elif td == "GetChargerPos" and "p" in attrs:
                charge_pos = attrs

    # trajectory compression: drop consecutive duplicate positions
    dedup: list[tuple[str, str]] = []
    for pos in positions:
        if not dedup or dedup[-1] != pos:
            dedup.append(pos)

    corpus = {
        "source": str(log_path),
        "frames_robot": n_frames,
        "spec_mapm": spec,
        "pieces": pieces,
        "trajectory": dedup,
        "trace_varint": trace,
        "battery": battery,
        "charge_state": charge,
        "clean_st": clean_st,
        "lifespans": lifespan,
        "charger_pos": charge_pos,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(corpus))
    print(f"corpus → {out} ({out.stat().st_size // 1024} KB)")
    print(f"  robot frames: {n_frames}")
    print(f"  GetMapM spec: {'yes' if spec else 'NO'} — map_id={spec.get('i') if spec else '-'}"
          f", {len(spec.get('m', '').split(',')) if spec else 0} crcs")
    print(f"  pieces: {len(pieces)} unique pids")
    print(f"  trajectory: {len(dedup)} positions (of {len(positions)})")
    print(f"  varint trace: {'yes' if trace else 'NO'}")
    print(f"  battery={battery} charge={charge}")
    print(f"  lifespans={lifespan}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
