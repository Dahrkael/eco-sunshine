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

# estilo tipo app: plano gris = pendiente, traza blanca = limpiado
COLORS = {
    0: (247, 247, 245),    # sin datos (fondo claro)
    1: (203, 206, 210),    # suelo pendiente de limpiar
    2: (85, 91, 98),       # pared
    3: (174, 214, 241),    # alfombra
}
TRACE_COLOR = (255, 255, 255)   # limpiado
ROBOT_COLOR = (76, 175, 80)
DOCK_COLOR = (255, 152, 0)

# Origen del grid respecto al marco mm del robot (resuelto empiricamente:
# correlacion traza/suelo 82% — ver tools/corr_offset.py)
ORIGIN_ROW = 400
ORIGIN_COL = 390


# Colocacion empirica de piezas (validada por el usuario contra su piso):
# el robot indexa las piezas en su marco INTERNO (inclinado), que no
# corresponde a filas/columnas axis-aligned. Tabla pid → (fila, columna)
# de pieza. Los pids fuera de la tabla caen en fallback row-major.
PIECE_PLACEMENT = {
    18: (0, 0), 19: (0, 1), 20: (0, 2), 28: (0, 3),
    26: (1, 0), 27: (1, 2), 36: (1, 3),
    34: (2, 0), 35: (2, 1),
}


def piece_slot(pid: int) -> tuple[int, int]:
    if pid in PIECE_PLACEMENT:
        return PIECE_PLACEMENT[pid]
    return (pid // PIECES_PER_ROW, pid % PIECES_PER_ROW)


def decode_piece(p_b64: str) -> bytes:
    raw = base64.b64decode(p_b64)
    hdr = raw[0:5] + struct.pack("<Q", PIECE_CELLS * PIECE_CELLS)
    return lzma.decompress(hdr + raw[9:], format=lzma.FORMAT_ALONE)


class MapAssembler:
    """Colecciona piezas por map_id y ensambla el grid completo."""

    def __init__(self) -> None:
        self.maps: dict[str, dict[int, bytes]] = {}
        self.geometry: dict[str, dict] = {}
        self.crcs: dict[str, list[int]] = {}  # por map_id: crc esperado por pid
        self.version = 0
        self.spec_changes = 0

    def set_geometry(self, map_id: str, attrs: dict) -> None:
        self.geometry[map_id] = {k: attrs.get(k) for k in ("w", "h", "c", "r", "p")}
        crcs_new = [int(x) for x in attrs.get("m", "").split(",") if x]
        old = self.crcs.get(map_id)
        self.crcs[map_id] = crcs_new
        if old is not None and old != crcs_new:
            self.spec_changes += 1

    def add_piece(self, map_id: str, pid: int, p_b64: str) -> bool:
        """Acepta la pieza solo si su crc32 coincide con la spec vigente."""
        pieces = self.maps.setdefault(map_id, {})
        decoded = decode_piece(p_b64)
        crcs = self.crcs.get(map_id)
        if crcs and pid < len(crcs) and zlib.crc32(decoded) != crcs[pid]:
            return False  # pieza obsoleta (mapa reconstruido): descartada
        changed = pieces.get(pid) != decoded
        if changed:
            pieces[pid] = decoded
            self.version += 1
        return True

    def piece_count(self, map_id: str) -> int:
        return len(self.maps.get(map_id, {}))

    def assemble(self, map_id: str) -> list[list[int]] | None:
        """Colocacion column-major — LA VALIDADA (map-con-traza.png).
        NO cambiar sin validacion visual contra la app."""
        pieces = self.maps.get(map_id)
        if not pieces:
            return None
        grid = [[0] * GRID_CELLS for _ in range(GRID_CELLS)]
        for pid, piece in pieces.items():
            r0 = (pid % PIECES_PER_ROW) * PIECE_CELLS
            c0 = (pid // PIECES_PER_ROW) * PIECE_CELLS
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
    dock: tuple[float, float] | None = None,
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

    # overlay: mm → celda del grid completo (ejes intercambiados + origen),
    # luego restar el recorte
    def to_cell(x_mm: float, y_mm: float) -> tuple[int, int] | None:
        row = int(x_mm // cell_mm) + ORIGIN_ROW - rmin
        col = int(y_mm // cell_mm) + ORIGIN_COL - cmin
        if 0 <= row < h and 0 <= col < w:
            return row, col
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
    if dock:
        dcell = to_cell(*dock)
        if dcell:
            canvas[dcell[0]][dcell[1]] = -3  # marcador base

    lut = {-1: trace_color, -2: ROBOT_COLOR, -3: DOCK_COLOR, **COLORS}
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
