"""Piezas MapP: decodificacion LZMA/zstd, ensamblado por mapa y render PNG.

Formato (validado contra corpus 930 + receta de ecovacs-privacy-control):
  p= base64 → props(1B 0x5d) + dict(u32 LE) + size(u32 LE) + lzma data[9:]
  reconstruccion: props + dict + size(u64) + data → LZMA-alone → 10.000 B
  = 100x100 celdas/pieza. Valores: 0=none 1=floor 2=wall 3=carpet.
  Geometria del frame MapSet: w/h=100 (celdas por pieza), c/r=8 (piezas por
  eje) → grid 800x800, 64 piezas; p=50 (~mm por celda).

Robots MQTT (T8+, spec mqtt-json-spec.md §6): misma pieza LZMA (o zstd en
gen-3) con geometría variable — pieceWidth/pieceHeight/cellWidth/cellHeight/
pixel vienen en el getMajorMap JSON → MapGeometry por mapa.
"""

from __future__ import annotations

import base64
import lzma
import struct
import zlib

PIECE_CELLS = 100
PIECE_CELLS_Y = 100
PIECES_PER_ROW = 8
GRID_CELLS = PIECE_CELLS * PIECES_PER_ROW  # 800

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"

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

# Origen del grid respecto al marco mm del robot (930; resuelto por busqueda
# exhaustiva: 86.8% traza-sobre-suelo — tools/buscar_transformacion.py).
# Mapeo natural: fila = y_mm/50, col = x_mm/50 (x→columnas, y→filas).
ORIGIN_ROW = 401
ORIGIN_COL = 401


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


def decompress_auto(raw: bytes, size_hint: int = PIECE_CELLS * PIECE_CELLS) -> bytes:
    """Descomprime un payload de mapa detectando el algoritmo por magic number.

    - zstd (gen-3, T80/X8 OMNI — ecovacs-deebot.js #592): magic 28 B5 2F FD
    - LZMA-alone (930 y T8+): props 5D 00 00 04 00 (el u64 de tamaño se
      reinyecta desde size_hint porque el original puede venir a 0)
    """
    if raw[:4] == ZSTD_MAGIC:
        import zstandard  # dependencia declarada en pyproject

        return zstandard.ZstdDecompressor().decompress(raw, max_output_size=size_hint * 8)
    hdr = raw[0:5] + struct.pack("<Q", size_hint)
    return lzma.decompress(hdr + raw[9:], format=lzma.FORMAT_ALONE)


def decode_piece_auto(p_b64: str, size_hint: int = PIECE_CELLS * PIECE_CELLS) -> bytes:
    """Pieza con detección automática LZMA/zstd (spec §6.1b)."""
    return decompress_auto(base64.b64decode(p_b64), size_hint)


def _lzma_stream(raw: bytes) -> bytes:
    """LZMA-alone estándar (13B de cabecera: props+dict+size u64).

    Los robots mandan la traza como LZMA-alone estándar (a diferencia de las
    piezas, que recortan el size a u32). Fallback: cabecera de 9 bytes con
    size reinyectado como desconocido (modo marcador EOS).
    """
    try:
        return lzma.LZMADecompressor(format=lzma.FORMAT_ALONE).decompress(raw)
    except lzma.LZMAError:
        if len(raw) <= 9:
            return b""
        return lzma.LZMADecompressor(format=lzma.FORMAT_ALONE).decompress(
            raw[:5] + b"\xff" * 8 + raw[9:]
        )


def decode_trace_mqtt(trace_b64: str) -> list[tuple[float, float, bool]]:
    """Traza de robots MQTT (spec §6.3): 5 bytes/punto.

    base64 → LZMA → registros: int16_be y+2000, int16_be x+2000, flags
    (bit7 = discontinuidad). Devuelve [(x_mm, y_mm, discontinuo)].
    """
    raw = base64.b64decode(trace_b64)
    if raw[:4] == ZSTD_MAGIC:
        import zstandard

        data = zstandard.ZstdDecompressor().decompress(raw, max_output_size=1 << 22)
    else:
        data = _lzma_stream(raw)
    pts: list[tuple[float, float, bool]] = []
    for i in range(0, len(data) - 4, 5):
        y = struct.unpack_from(">h", data, i)[0] - 2000
        x = struct.unpack_from(">h", data, i + 2)[0] - 2000
        pts.append((float(x), float(y), bool(data[i + 4] & 0x80)))
    return pts


class MapGeometry:
    """Geometría de un mapa — por mapa, no global.

    930 (XML GetMapM): w/h celdas por pieza, c/r piezas por eje, p mm/celda.
    MQTT (getMajorMap JSON): cellWidth/cellHeight, pieceWidth/pieceHeight, pixel.

    transform y_down: convención 930 — y+ mm hacia fila+ (validada empíricamente).
    Para mapas JSON usamos origen centrado con y invertida (y+ mm hacia fila-),
    convención habitual de la familia T9+; PROVISIONAL hasta validar con robot
    real (mock y bridge son consistentes entre sí).
    """

    def __init__(
        self,
        cells_x: int = PIECE_CELLS,
        cells_y: int = PIECE_CELLS,
        piece_cols: int = PIECES_PER_ROW,
        piece_rows: int = PIECES_PER_ROW,
        cell_mm: int = 50,
        origin_row: int = ORIGIN_ROW,
        origin_col: int = ORIGIN_COL,
        y_down: bool = True,
    ) -> None:
        self.cells_x = cells_x
        self.cells_y = cells_y
        self.piece_cols = piece_cols
        self.piece_rows = piece_rows
        self.cell_mm = max(1, cell_mm)
        self.origin_row = origin_row
        self.origin_col = origin_col
        self.y_down = y_down

    @classmethod
    def from_xml_attrs(cls, attrs: dict) -> "MapGeometry":
        try:
            return cls(
                cells_x=int(attrs.get("w", PIECE_CELLS)),
                cells_y=int(attrs.get("h", PIECE_CELLS)),
                piece_cols=int(attrs.get("c", PIECES_PER_ROW)),
                piece_rows=int(attrs.get("r", PIECES_PER_ROW)),
                cell_mm=int(attrs.get("p", 50)),
            )
        except (TypeError, ValueError):
            return cls()

    @classmethod
    def from_major_map_json(cls, data: dict) -> "MapGeometry":
        """getMajorMap/getMapInfo JSON → geometría con origen centrado y y-up."""
        try:
            cells_x = int(data.get("cellWidth", PIECE_CELLS))
            cells_y = int(data.get("cellHeight", PIECE_CELLS))
            piece_cols = int(data.get("pieceWidth") or data.get("width") or PIECES_PER_ROW)
            piece_rows = int(data.get("pieceHeight") or data.get("height") or PIECES_PER_ROW)
            cell_mm = int(data.get("pixel", 50))
        except (TypeError, ValueError):
            return cls()
        geo = cls(cells_x=cells_x, cells_y=cells_y, piece_cols=piece_cols,
                  piece_rows=piece_rows, cell_mm=cell_mm)
        geo.set_centered_origin()
        return geo

    def set_centered_origin(self) -> None:
        """Origen mm en el centro del grid total (convención eco-ng)."""
        self.origin_col = (self.piece_cols * self.cells_x) // 2
        self.origin_row = (self.piece_rows * self.cells_y) // 2
        self.y_down = False  # y+ mm → fila-

    @property
    def total_rows(self) -> int:
        return self.piece_rows * self.cells_y

    @property
    def total_cols(self) -> int:
        return self.piece_cols * self.cells_x

    def mm_to_cell(self, x_mm: float, y_mm: float) -> tuple[int, int]:
        """mm del frame del robot → (fila, columna) del grid total."""
        col = int(x_mm // self.cell_mm) + self.origin_col
        drow = int(y_mm // self.cell_mm)
        row = self.origin_row + drow if self.y_down else self.origin_row - drow
        return row, col


class MapAssembler:
    """Colecciona piezas por map_id y ensambla el grid completo."""

    def __init__(self) -> None:
        self.maps: dict[str, dict[int, bytes]] = {}
        self.geometry: dict[str, MapGeometry] = {}
        self.crcs: dict[str, list[int]] = {}  # por map_id: crc esperado por pid
        self.version = 0
        self.spec_changes = 0
        # mapas chunked (T8/N8 getMapInfo): mid → {index: b64} hasta completar
        self._chunks: dict[str, dict[int, str]] = {}

    def set_geometry(self, map_id: str, attrs: dict) -> None:
        self.geometry[map_id] = MapGeometry.from_xml_attrs(attrs)
        crcs_new = [int(x) for x in attrs.get("m", "").split(",") if x]
        old = self.crcs.get(map_id)
        self.crcs[map_id] = crcs_new
        if old is not None and old != crcs_new:
            self.spec_changes += 1

    def set_geometry_json(self, map_id: str, data: dict) -> None:
        """Geometría+CRCs desde getMajorMap JSON (robots MQTT, spec §4.5).

        A diferencia del camino XML (las piezas llegan por push), aquí la
        primera spec también cuenta como cambio: sin walk inicial no hay
        piezas (getMinorMap es la única fuente).
        """
        old = self.crcs.get(map_id)
        self.geometry[map_id] = MapGeometry.from_major_map_json(data)
        crcs_new = []
        for part in str(data.get("value", "")).split(","):
            part = part.strip()
            if part:
                try:
                    crcs_new.append(int(part))
                except ValueError:
                    pass
        self.crcs[map_id] = crcs_new
        if crcs_new and old != crcs_new:
            self.spec_changes += 1

    def geo(self, map_id: str) -> MapGeometry:
        return self.geometry.get(map_id, MapGeometry())

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

    def add_piece_json(self, map_id: str, pid: int, piece_value_b64: str) -> bool:
        """Pieza desde getMinorMap JSON (robots MQTT) — decoder auto LZMA/zstd."""
        geo = self.geo(map_id)
        pieces = self.maps.setdefault(map_id, {})
        decoded = decode_piece_auto(piece_value_b64, geo.cells_x * geo.cells_y)
        crcs = self.crcs.get(map_id)
        if crcs and pid < len(crcs) and zlib.crc32(decoded) != crcs[pid]:
            return False
        changed = pieces.get(pid) != decoded
        if changed:
            pieces[pid] = decoded
            self.version += 1
        return True

    def piece_crc(self, map_id: str, pid: int) -> int | None:
        piece = self.maps.get(map_id, {}).get(pid)
        return zlib.crc32(piece) if piece is not None else None

    def add_chunk_json(self, map_id: str, index: int, value_b64: str,
                       total_count: int, data: dict | None = None) -> bool:
        """Chunk de getMapInfo (T8/N8): concatena y decodifica al completar.

        Devuelve True cuando el mapa quedó montado con este chunk.
        """
        chunks = self._chunks.setdefault(map_id, {})
        if value_b64:
            chunks[index] = value_b64
        if len(chunks) < total_count:
            return False
        joined = "".join(chunks[i] for i in range(total_count) if i in chunks)
        if len(chunks) != total_count:
            return False  # faltan chunks intermedios
        if data:
            self.geometry[map_id] = MapGeometry.from_major_map_json(data)
        geo = self.geo(map_id)
        grid = decompress_auto(base64.b64decode(joined), geo.total_rows * geo.total_cols)
        # el chunked NO viene por piezas: una sola "pieza" = grid completo,
        # indexada como pid -1 para no chocar con el ensamblado por piezas
        pieces = self.maps.setdefault(map_id, {})
        pieces[-1] = grid
        if self.crcs.get(map_id) is None:
            self.crcs[map_id] = []
        self._chunks.pop(map_id, None)
        self.version += 1
        return True

    def piece_count(self, map_id: str) -> int:
        return len(self.maps.get(map_id, {}))

    def assemble(self, map_id: str) -> list[list[int]] | None:
        """Colocacion row-major (privacy-control): r0=pid//8, c0=pid%8.
        Validada por correlacion traza/suelo 86.8% (tools/buscar_transformacion.py).
        Con geometría por mapa (MQTT): pieza N en row=N//pieceW, col=N%pieceW.
        """
        pieces = self.maps.get(map_id)
        if not pieces:
            return None
        geo = self.geo(map_id)
        grid = [[0] * geo.total_cols for _ in range(geo.total_rows)]
        for pid, piece in pieces.items():
            if pid < 0:  # mapa chunked: el grid ya viene montado
                for r in range(geo.total_rows):
                    row = grid[r]
                    base = r * geo.total_cols
                    for c in range(geo.total_cols):
                        row[c] = piece[base + c]
                continue
            r0 = (pid // geo.piece_cols) * geo.cells_y
            c0 = (pid % geo.piece_cols) * geo.cells_x
            for r in range(geo.cells_y):
                base = r * geo.cells_x
                row = grid[r0 + r]
                for c in range(geo.cells_x):
                    row[c0 + c] = piece[base + c]
        return grid


def render_png(
    grid: list[list[int]],
    trace: list[tuple[float, float]] | None = None,
    position: tuple[float, float] | None = None,
    dock: tuple[float, float] | None = None,
    cell_mm: int = 50,
    trace_color: tuple[int, int, int] = TRACE_COLOR,
    geo: MapGeometry | None = None,
) -> bytes:
    """Render PNG (1 celda = 1px). trace/position en mm del frame del robot.

    geo: geometría del mapa (transform mm→celda). Si es None se usa la del
    930 (cell_mm, ORIGIN_ROW/COL con y hacia abajo).
    """
    size_r = len(grid)
    size_c = len(grid[0]) if grid else 0
    rows = [r for r in range(size_r) if any(grid[r])]
    cols = [c for c in range(size_c) if any(grid[rr][c] for rr in range(size_r))]
    if not rows:
        raise ValueError("grid vacío")
    w = max(cols) - min(cols) + 1
    h = max(rows) - min(rows) + 1
    rmin, cmin = min(rows), min(cols)
    canvas = [list(grid[r][cmin : cmin + w]) for r in range(rmin, rmin + h)]

    g = geo or MapGeometry(cell_mm=cell_mm)

    # overlay: mm → celda del grid completo, luego restar el recorte
    def to_cell(x_mm: float, y_mm: float) -> tuple[int, int] | None:
        row, col = g.mm_to_cell(x_mm, y_mm)
        row, col = row - rmin, col - cmin
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
