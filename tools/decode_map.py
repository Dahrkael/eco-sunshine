#!/usr/bin/env python3
"""Decodifica las piezas MapP de un log de Bumper y renderiza un PNG.

Pipeline (receta validada contra privacy-control + nuestro corpus 930):
  MapP p= base64
    → bytes con cabecera: props(1B=0x5d) + dict_size(u32 LE) + size(u32 LE)
    → reconstruir cabecera LZMA-alone: props + dict + size(u64) + data[9:]
    → descomprimir → 10.000 bytes (100x100 celdas por pieza)
  Valores de celda: 0=sin datos, 1=suelo, 2=pared, 3=alfombra
  Grid total: (h*w) celdas montado en piezas de (r*c) — h=w=100, c=r=8,
  p=50 → 800x800 celdas de mapa, 64 piezas max.

Uso: python3 tools/decode_map.py <log> <salida.png> [--sequential]
  (por defecto orden de llegada; con --sequential asume pids 0..N)
"""

import base64
import lzma
import re
import struct
import sys
import zlib

PIECE_CELLS = 100      # 100x100 celdas por pieza (rowGrid=columnGrid=100)
PIECES_PER_ROW = 8     # columnPiece=rowPiece=8
COLORS = {0: (0, 0, 0), 1: (15, 10, 222), 2: (255, 255, 255), 3: (100, 200, 200)}


def decode_piece(raw: bytes) -> bytes:
    hdr = raw[0:5] + struct.pack("<Q", PIECE_CELLS * PIECE_CELLS)
    return lzma.decompress(hdr + raw[9:], format=lzma.FORMAT_ALONE)


def extract_pieces(log_path: str) -> list[bytes]:
    vals, seen = [], set()
    for line in open(log_path, errors="replace"):
        for m in re.finditer(r'p="([A-Za-z0-9+/=]{20,})"', line):
            v = m.group(1)
            if v not in seen:
                seen.add(v)
                vals.append(v)
    return [decode_piece(base64.b64decode(v)) for v in vals]


def assemble(pieces: list[bytes]) -> list[list[int]]:
    size = PIECE_CELLS * PIECES_PER_ROW
    grid = [[0] * size for _ in range(size)]
    for pid, piece in enumerate(pieces):
        r0 = (pid // PIECES_PER_ROW) * PIECE_CELLS
        c0 = (pid % PIECES_PER_ROW) * PIECE_CELLS
        for r in range(PIECE_CELLS):
            for c in range(PIECE_CELLS):
                grid[r0 + r][c0 + c] = piece[r * PIECE_CELLS + c]
    return grid


def render_png(grid: list[list[int]], out: str) -> None:
    size = len(grid)
    rows = [r for r in range(size) if any(grid[r])]
    cols = [c for c in range(size) if any(grid[rr][c] for rr in range(size))]
    w, h = max(cols) - min(cols) + 1, max(rows) - min(rows) + 1
    px = b"".join(
        b"\x00" + b"".join(bytes(COLORS[grid[min(rows) + r][min(cols) + c]]) for c in range(w))
        for r in range(h)
    )

    def chunk(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(px, 9))
        + chunk(b"IEND", b"")
    )
    with open(out, "wb") as f:
        f.write(png)
    print(f"{out}: {w}x{h}px")


def main() -> None:
    log, out = sys.argv[1], sys.argv[2]
    pieces = extract_pieces(log)
    print(f"{len(pieces)} piezas")
    render_png(assemble(pieces), out)


if __name__ == "__main__":
    main()
