#!/usr/bin/env python3
"""Decodifica las piezas MapP de un log de Bumper y renderiza PNG por mapa.

Las piezas se agrupan por el atributo i= (map id) del mismo frame — mezclar
mapas de sesiones distintas produce renders corruptos (aprendido a pulidas).

Uso: python3 tools/decode_map.py <log> <salida_prefijo>
Genera <prefijo>-<mapid>.png por cada map id encontrado.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bridge"))

from deebot930_bridge.map_pieces import MapAssembler, render_png  # noqa: E402

CTL_RE = re.compile(r"<ctl\s+([^>]*)>")
ATTR_RE = re.compile(r'(\w+)="([^"]*)"')
P_RE = re.compile(r"^[A-Za-z0-9+/]{20,}={0,2}$")


def extract(log_path: str) -> MapAssembler:
    asm = MapAssembler()
    seen: set[str] = set()
    counters: dict[str, int] = {}
    for line in open(log_path, errors="replace"):
        for tag in CTL_RE.findall(line):
            attrs = dict(ATTR_RE.findall(tag))
            p = attrs.get("p", "")
            i = attrs.get("i", "")
            if not (i and p and P_RE.match(p)):
                continue
            if p in seen:  # el log duplica frames
                continue
            seen.add(p)
            pid = counters.get(i, 0)
            counters[i] = pid + 1
            asm.add_piece(i, pid, p)
            if attrs.get("w") and attrs.get("c"):
                asm.set_geometry(i, attrs)
    return asm


def main() -> None:
    log, prefix = sys.argv[1], sys.argv[2]
    asm = extract(log)
    print(f"mapas: {list(asm.maps)} piezas: {[asm.piece_count(m) for m in asm.maps]}")
    for map_id in asm.maps:
        grid = asm.assemble(map_id)
        if grid is None:
            continue
        out = f"{prefix}-{map_id}.png"
        Path(out).write_bytes(render_png(grid))
        print(f"{out}")


if __name__ == "__main__":
    main()
