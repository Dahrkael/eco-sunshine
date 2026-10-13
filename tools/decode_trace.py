#!/usr/bin/env python3
"""Decodifica los chunks 'trace' y los correlaciona con las 'Pos' cercanas.

Uso: python3 tools/decode_trace.py <extracted_dir>
"""

import json
import struct
import sys
from datetime import datetime, timezone
from pathlib import Path

HEADER = bytes.fromhex("5d000004000500000000")


def ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def load(p: Path) -> list:
    with open(p) as f:
        return [json.loads(l) for l in f]


def main(d: str) -> None:
    d = Path(d)
    traces = sorted(load(d / "traces.jsonl"), key=lambda r: r["ts"])
    poss = sorted(load(d / "positions.jsonl"), key=lambda r: r["ts"])

    print(f"{len(traces)} chunks de trace, {len(poss)} Pos\n")
    print("chunk | tf/tt | bytes tras header | int16s (LE)        | Pos mas cercana (Δt s)")

    for t in traces:
        raw = bytes.fromhex(t["tr_hex"])
        body = raw[len(HEADER):] if raw.startswith(HEADER) else raw
        # consumir pares int16 hasta que todo lo que quede sean ceros
        vals = []
        for i in range(0, len(body) - 1, 2):
            v = struct.unpack_from("<h", body, i)[0]
            if v == 0 and all(b == 0 for b in body[i:]):
                break
            vals.append(v)
        # pos mas cercana en tiempo
        tt = ts(t["ts"])
        near = min(poss, key=lambda p: abs((ts(p["ts"]) - tt).total_seconds()))
        dt = (ts(near["ts"]) - tt).total_seconds()
        pairs = [f"({vals[i]},{vals[i+1]})" for i in range(0, len(vals) - 1, 2)]
        print(
            f"trid {t['trid']} {t['tf']}/{t['tt']} | {len(body):2d}B | "
            f"{' '.join(pairs):34s} | p={near['p']} a={near['a']} (dt={dt:+.1f})"
        )


if __name__ == "__main__":
    main(sys.argv[1])
