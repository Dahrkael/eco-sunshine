#!/usr/bin/env python3
"""Seeds the bridge's state.json with the REAL map from the 930 corpus.

Use case: the robot lost its map (spec with empty piece CRCs) or the
bridge state was wiped — this script rebuilds the bridge's persisted
copy from data/mock930-corpus.json (extracted from Bumper's log with
tools/extract_930_corpus.py), decoding pieces to the internal format.

Usage:
    python tools/seed_state_from_corpus.py [--corpus data/mock930-corpus.json]
        [--out data/bridge/state.json] [--jid E0001057…@115.ecorobot.net/atom]
        [--pos -37,-595,0]
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "bridge"))

from ecovacs_bridge.map_pieces import decode_piece  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--corpus", default="data/mock930-corpus.json")
    ap.add_argument("--out", default="data/bridge/state.json")
    ap.add_argument("--jid", default="E000105701860607AB7@115.ecorobot.net/atom")
    ap.add_argument("--pos", default="-37,-595,0",
                    help="last known position/dock 'x,y,angle'")
    args = ap.parse_args()

    corpus = json.loads(Path(args.corpus).read_text())
    spec = corpus.get("spec_mapm")
    if not spec or not corpus.get("pieces"):
        print("corpus has no spec or no pieces — extract first with extract_930_corpus.py",
              file=sys.stderr)
        return 1

    # the state persists DECODED pieces (the assembler's internal format)
    pieces = {str(pid): base64.b64encode(decode_piece(p)).decode()
              for pid, p in corpus["pieces"].items()}
    x, y, a = (float(v) for v in args.pos.split(","))

    # slug = model detected from the JID + last 6 of the serial (like RobotState)
    from ecovacs_bridge.models import detect_model

    model = detect_model(args.jid)
    slug = f"{model.slug}-{args.jid.split('@')[0][-6:].lower()}"

    state = {
        "maps": {spec["i"]: pieces},
        "geometry": {spec["i"]: {
            "cells_x": int(spec.get("w", 100)), "cells_y": int(spec.get("h", 100)),
            "piece_cols": int(spec.get("c", 8)), "piece_rows": int(spec.get("r", 8)),
            "cell_mm": int(spec.get("p", 50)),
            "origin_row": 401, "origin_col": 401, "y_down": True}},
        # no CRCs: the seeded pieces are the truth by default
        "crcs": {spec["i"]: []},
        "version": 1,
        "map_owner": {spec["i"]: args.jid},
        "positions": {slug: {"position": [x, y, a], "charger": [x, y, a]}},
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(state))
    print(f"state seeded: {len(pieces)} decoded pieces "
          f"(map {spec['i']}) → {out} ({out.stat().st_size // 1024} KB)")
    print("restart the bridge to restore the map")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
