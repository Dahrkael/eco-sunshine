#!/usr/bin/env python3
"""Decodes MapP pieces from a Bumper log and renders one PNG per map.

Pieces are grouped by the same frame's i= attribute (map id) — mixing
maps from different sessions produces corrupt renders.

Usage: python3 tools/decode_map.py <log> <output_prefix>
Generates <prefix>-<mapid>.png for every map id found.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bridge"))

from eco_sunshine.map_pieces import MapAssembler, render_png  # noqa: E402

CTL_RE = re.compile(r"<ctl\s+([^>]*)>")
ATTR_RE = re.compile(r'(\w+)="([^"]*)"')
P_RE = re.compile(r"^[A-Za-z0-9+/]{20,}={0,2}$")


def extract(log_path: str) -> MapAssembler:
    asm = MapAssembler()
    seen: set[tuple[str, int]] = set()
    for line in open(log_path, errors="replace"):
        for tag in CTL_RE.findall(line):
            attrs = dict(ATTR_RE.findall(tag))
            p = attrs.get("p", "")
            i = attrs.get("i", "")
            pid = attrs.get("pid", "")
            if not (i and p and pid.isdigit() and P_RE.match(p)):
                continue
            key = (i, int(pid))
            if key in seen:  # the log duplicates frames; only take each once
                continue
            seen.add(key)
            asm.add_piece(i, int(pid), p)
            if attrs.get("w") and attrs.get("c"):
                asm.set_geometry(i, attrs)
    return asm


def main() -> None:
    log, prefix = sys.argv[1], sys.argv[2]
    asm = extract(log)
    print(f"maps: {list(asm.maps)} pieces: {[asm.piece_count(m) for m in asm.maps]}")
    for map_id in asm.maps:
        grid = asm.assemble(map_id)
        if grid is None:
            continue
        out = f"{prefix}-{map_id}.png"
        try:
            Path(out).write_bytes(render_png(grid))
        except ValueError:
            print(f"{out}: empty grid, skipped")
            continue
        print(f"{out}")


if __name__ == "__main__":
    main()
