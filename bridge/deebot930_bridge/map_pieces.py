"""Piezas MapP: decodificacion LZMA, ensamblado por mapa y render PNG.

Formato (validado contra corpus 930 + receta de ecovacs-privacy-control):
  p= base64 → props(1B 0x5d) + dict(u32 LE) + size(u32 LE) + lzma data[9:]
  reconstruccion: props + dict + size(u64) + data → LZMA-alone → 10.000 B
  = 100x100 celdas/pieza. Valores: 0=none 1=floor 2=wall 3=carpet.
  Geometria del frame MapSet: w/h=100 (celdas por pieza), c/r=8 (piezas por
  eje) → grid 800x800, 64 piezas; p=50 (~mm por celda).
"""

from __future__ import annotations

import base64
import lzma
import struct
import zlib

PIECE_CELLS = 100
PIECES_PER_ROW = 8
GRID_CELLS = PIECE_CELLS * PIECES_PER_ROW  # 800

COLORS = {
    0: (10, 10, 14),       # sin datos (fondo casi negro)
    1: (96, 125, 139),     # suelo
    2: (236, 239, 241),    # pared
    3: (129, 199, 132),    # alfombra
}
TRACE_COLOR = (244, 67, 54)
ROBOT_COLOR = (76, 175, 80)


def decode_piece(p_b64: str) -> bytes:
    raw = base64.b64decode(p_b64)
    hdr = raw[0:5] + struct.pack("<Q", PIECE_CELLS * PIECE_CELLS)
    return lzma.decompress(hdr + raw[9:], format=lzma.FORMAT_ALONE)


class MapAssembler:
    """Colecciona piezas por map_id y ensambla el grid completo."""

    def __init__(self) -> None:
        self.maps: dict[str, dict[int, bytes]] = {}
        self.geometry: dict[str, dict] = {}

    def set_geometry(self, map_id: str, attrs: dict) -> None:
        self.geometry[map_id] = {
            k: attrs.get(k) for k in ("w", "h", "c", "r", "p", "m")
        }

    def add_piece(self, map_id: str, pid: int, p_b64: str) -> None:
        self.maps.setdefault(map_id, {})[pid] = decode_piece(p_b64)

    def piece_count(self, map_id: str) -> int:
        return len(self.maps.get(map_id, {}))

    def assemble(self, map_id: str) -> list[list[int]] | None:
        pieces = self.maps.get(map_id)
        if not pieces:
            return None
        grid = [[0] * GRID_CELLS for _ in range(GRID_CELLS)]
        for pid, piece in pieces.items():
            r0 = (pid // PIECES_PER_ROW) * PIECE_CELLS
            c0 = (pid % PIECES_PER_ROW) * PIECE_CELLS
            for r in range(PIECE_CELLS):
                base = r * PIECE_CELLS
                row = grid[r0 + r]
                for c in range(PIECE_CELLS):
                    row[c0 + c] = piece[base + c]
        return grid


def render_png(
    grid: list[list[int]],
    trace: list[tuple[float, float]] | None = None,
    position: tuple[float, float] | None = None,
    cell_mm: int = 50,
    trace_color: tuple[int, int, int] = TRACE_COLOR,
) -> bytes:
    """Render PNG (1 celda = 1px). trace/position en mm del frame del robot."""
    size = len(grid)
    rows = [r for r in range(size) if any(grid[r])]
    cols = [c for c in range(size) if any(grid[rr][c] for rr in range(size))]
    if not rows:
        raise ValueError("grid vacío")
    w = max(cols) - min(cols) + 1
    h = max(rows) - min(rows) + 1
    rmin, cmin = min(rows), min(cols)
    canvas = [list(grid[r][cmin : cmin + w]) for r in range(rmin, rmin + h)]

    # overlay de traza: mm → celdas (origen del grid = esquina NO del bounding)
    def to_cell(x_mm: float, y_mm: float) -> tuple[int, int] | None:
        c = int(x_mm // cell_mm) - cmin
        r = int(y_mm // cell_mm) - rmin
        if 0 <= r < h and 0 <= c < w:
            return r, c
        return None

    if trace:
        for x, y in trace:
            cell = to_cell(x, y)
            if cell:
                canvas[cell[0]][cell[1]] = -1  # marcador traza
    if position:
        cell = to_cell(*position)
        if cell:
            canvas[cell[0]][cell[1]] = -2  # marcador robot

    lut = {-1: trace_color, -2: ROBOT_COLOR, **COLORS}
    px = b"".join(
        b"\x00" + b"".join(bytes(lut.get(v, COLORS[0])) for v in row) for row in canvas
    )

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(px, 9))
        + chunk(b"IEND", b"")
    )
