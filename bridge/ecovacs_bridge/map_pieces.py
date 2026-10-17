"""MapP pieces: LZMA/zstd decoding, per-map assembly and PNG rendering.

Wire format (validated against the 930 corpus + ecovacs-privacy-control recipe):
  p= base64 → props(1B 0x5d) + dict(u32 LE) + size(u32 LE) + lzma data[9:]
  rebuild: props + dict + size(u64) + data → LZMA-alone → 10,000 B
  = 100x100 cells/piece. Values: 0=none 1=floor 2=wall 3=carpet.
  MapSet frame geometry: w/h=100 (cells per piece), c/r=8 (pieces per
  axis) → 800x800 grid, 64 pieces; p=50 (~mm per cell).

MQTT robots (T8+, spec mqtt-json-spec.md §6): same LZMA piece (or zstd on
gen-3) with variable geometry — pieceWidth/pieceHeight/cellWidth/cellHeight/
pixel come in the getMajorMap JSON → per-map MapGeometry.
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


def log_geometry_warning(map_id: str, geo: "MapGeometry") -> None:
    """Out-of-range geometry (corrupt payload or unknown format)."""
    import logging

    logging.getLogger(__name__).warning(
        "map %s: implausible geometry %dx%d (%d cells) — chunked discarded",
        map_id, geo.total_cols, geo.total_rows, geo.total_rows * geo.total_cols)

# app-like palette: flat grey = pending, white trace = cleaned
COLORS = {
    0: (247, 247, 245),    # no data (light background)
    1: (203, 206, 210),    # floor pending cleaning
    2: (85, 91, 98),       # wall
    3: (174, 214, 241),    # carpet
}
TRACE_COLOR = (255, 255, 255)   # cleaned
ROBOT_COLOR = (76, 175, 80)
DOCK_COLOR = (255, 152, 0)

# Grid origin relative to the robot's mm frame (930; found by exhaustive
# search: 86.8% trace-over-floor — tools/buscar_transformacion.py).
# Natural mapping: row = y_mm/50, col = x_mm/50 (x→columns, y→rows).
ORIGIN_ROW = 401
ORIGIN_COL = 401


# Empirical piece placement (validated by the user against their floor):
# the robot indexes pieces in its own TILTED internal frame, which does
# not map to axis-aligned rows/columns. pid → (row, column) table;
# pids outside the table fall back to row-major.
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
    """Decompress a map payload, picking the algorithm by magic number.

    - zstd (gen-3, T80/X8 OMNI — ecovacs-deebot.js #592): magic 28 B5 2F FD
    - LZMA-alone (930 and T8+): props 5D 00 00 04 00 (the size u64 is
      re-injected from size_hint because the original can come as 0)
    """
    if raw[:4] == ZSTD_MAGIC:
        import zstandard  # dependency declared in pyproject

        return zstandard.ZstdDecompressor().decompress(raw, max_output_size=size_hint * 8)
    hdr = raw[0:5] + struct.pack("<Q", size_hint)
    return lzma.decompress(hdr + raw[9:], format=lzma.FORMAT_ALONE)


def decode_piece_auto(p_b64: str, size_hint: int = PIECE_CELLS * PIECE_CELLS) -> bytes:
    """Piece with automatic LZMA/zstd detection (spec §6.1b)."""
    return decompress_auto(base64.b64decode(p_b64), size_hint)


def _lzma_stream(raw: bytes) -> bytes:
    """Standard LZMA-alone (13B header: props+dict+u64 size).

    Robots send traces and chunked maps as standard LZMA-alone (unlike
    pieces, which truncate the size to u32). Fallback: 9-byte header
    with the size re-injected as unknown (EOS).
    """
    try:
        return lzma.LZMADecompressor(format=lzma.FORMAT_ALONE).decompress(raw)
    except lzma.LZMAError:
        if len(raw) <= 9:
            return b""
        return lzma.LZMADecompressor(format=lzma.FORMAT_ALONE).decompress(
            raw[:5] + b"\xff" * 8 + raw[9:]
        )


def decompress_stream_auto(raw: bytes, max_out: int = 1 << 26) -> bytes:
    """Decompress a standard stream (getMapTrace trace, chunked getMapInfo
    map) detecting zstd by magic — LZMA header left untouched."""
    if raw[:4] == ZSTD_MAGIC:
        import zstandard

        return zstandard.ZstdDecompressor().decompress(raw, max_output_size=max_out)
    return _lzma_stream(raw)


def decode_trace_mqtt(trace_b64: str) -> list[tuple[float, float, bool]]:
    """MQTT robots trace (spec §6.3): 5 bytes/point.

    base64 → standard LZMA → records: int16_be y+2000, int16_be x+2000,
    flags (bit7 = discontinuity). Returns [(x_mm, y_mm, discontinuous)].
    """
    data = decompress_stream_auto(base64.b64decode(trace_b64))
    pts: list[tuple[float, float, bool]] = []
    for i in range(0, len(data) - 4, 5):
        y = struct.unpack_from(">h", data, i)[0] - 2000
        x = struct.unpack_from(">h", data, i + 2)[0] - 2000
        pts.append((float(x), float(y), bool(data[i + 4] & 0x80)))
    return pts


class MapGeometry:
    """Geometry of one map — per map, not global.

    930 (XML GetMapM): w/h cells per piece, c/r pieces per axis, p mm/cell.
    MQTT (getMajorMap JSON): cellWidth/cellHeight, pieceWidth/pieceHeight, pixel.

    y_down transform: 930 convention — y+ mm towards row+ (empirically
    validated). For JSON maps we use a centered origin with inverted y
    (y+ mm towards row-), the usual convention of the T9+ family;
    PROVISIONAL until validated on a real robot (mock and bridge agree).
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
        """getMajorMap JSON → geometry with centered origin and y-up."""
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

    @classmethod
    def from_chunked_json(cls, data: dict) -> "MapGeometry":
        """Chunked getMapInfo (T8/N8, spec §4.5): the joined blob IS the
        full grid — totalWidth/totalHeight in cells, no pieces.

        Chunks carry width/height (chunk cells) and totalWidth/totalHeight
        (full grid); using from_major_map_json here would build a giant
        grid (width=100 → 100 pieces per axis → 100M cells).
        """
        try:
            total_cols = int(data.get("totalWidth") or data.get("width") or PIECE_CELLS)
            total_rows = int(data.get("totalHeight") or data.get("height") or PIECE_CELLS)
            cell_mm = int(data.get("pixel", 50))
        except (TypeError, ValueError):
            return cls()
        geo = cls(cells_x=total_cols, cells_y=total_rows,
                  piece_cols=1, piece_rows=1, cell_mm=cell_mm)
        geo.set_centered_origin()
        return geo

    def set_centered_origin(self) -> None:
        """mm origin at the center of the total grid (eco-ng convention)."""
        self.origin_col = (self.piece_cols * self.cells_x) // 2
        self.origin_row = (self.piece_rows * self.cells_y) // 2
        self.y_down = False  # y+ mm → row-

    @property
    def total_rows(self) -> int:
        return self.piece_rows * self.cells_y

    @property
    def total_cols(self) -> int:
        return self.piece_cols * self.cells_x

    def mm_to_cell(self, x_mm: float, y_mm: float) -> tuple[int, int]:
        """Robot-frame mm → (row, column) in the total grid."""
        col = int(x_mm // self.cell_mm) + self.origin_col
        drow = int(y_mm // self.cell_mm)
        row = self.origin_row + drow if self.y_down else self.origin_row - drow
        return row, col


class MapAssembler:
    """Collects pieces per map_id and assembles the full grid."""

    def __init__(self) -> None:
        self.maps: dict[str, dict[int, bytes]] = {}
        self.geometry: dict[str, MapGeometry] = {}
        self.crcs: dict[str, list[int]] = {}  # per map_id: expected crc per pid
        self.version = 0
        self.spec_changes = 0
        # chunked maps (T8/N8 getMapInfo): mid → {index: b64} until complete
        self._chunks: dict[str, dict[int, str]] = {}

    def set_geometry(self, map_id: str, attrs: dict) -> None:
        self.geometry[map_id] = MapGeometry.from_xml_attrs(attrs)
        crcs_new = [int(x) for x in attrs.get("m", "").split(",") if x]
        old = self.crcs.get(map_id)
        self.crcs[map_id] = crcs_new
        if old is not None and old != crcs_new:
            self.spec_changes += 1

    def set_geometry_json(self, map_id: str, data: dict) -> None:
        """Geometry+CRCs from getMajorMap JSON (MQTT robots, spec §4.5).

        Unlike the XML path (pieces arrive via push), here the first
        spec also counts as a change: without an initial walk there are
        no pieces (getMinorMap is the only source).
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
        """Accepts the piece only if its crc32 matches the current spec."""
        pieces = self.maps.setdefault(map_id, {})
        decoded = decode_piece(p_b64)
        crcs = self.crcs.get(map_id)
        if crcs and pid < len(crcs) and zlib.crc32(decoded) != crcs[pid]:
            return False  # stale piece (rebuilt map): discarded
        changed = pieces.get(pid) != decoded
        if changed:
            pieces[pid] = decoded
            self.version += 1
        return True

    def add_piece_json(self, map_id: str, pid: int, piece_value_b64: str) -> bool:
        """Piece from getMinorMap JSON (MQTT robots) — auto LZMA/zstd decoder."""
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
        """getMapInfo chunk (T8/N8): concatenate and decode once complete.

        Returns True when this chunk completed the map.
        """
        chunks = self._chunks.setdefault(map_id, {})
        if value_b64:
            chunks[index] = value_b64
        if total_count < 1 or len(chunks) < total_count:
            return False
        joined = "".join(chunks[i] for i in range(total_count) if i in chunks)
        if len(chunks) != total_count:
            return False  # missing intermediate chunks
        if data:
            self.geometry[map_id] = MapGeometry.from_chunked_json(data)
        geo = self.geo(map_id)
        # safeguard (B9): a payload with absurd geometry must not build a
        # 100M-cell grid — cap at 2000x2000
        if geo.total_rows * geo.total_cols > 2000 * 2000:
            log_geometry_warning(map_id, geo)
            self._chunks.pop(map_id, None)
            return False
        # the chunked blob is a STANDARD LZMA/zstd stream (not piece format)
        grid = decompress_stream_auto(base64.b64decode(joined),
                                      max_out=geo.total_rows * geo.total_cols * 4)
        if len(grid) < geo.total_rows * geo.total_cols:
            self._chunks.pop(map_id, None)
            return False
        # chunked does NOT come in pieces: a single "piece" = full grid,
        # indexed as pid -1 (B15: drops old pieces of the same mid)
        pieces = self.maps.setdefault(map_id, {})
        pieces.clear()
        pieces[-1] = grid
        if self.crcs.get(map_id) is None:
            self.crcs[map_id] = []
        self._chunks.pop(map_id, None)
        self.version += 1
        return True

    def piece_count(self, map_id: str) -> int:
        return len(self.maps.get(map_id, {}))

    def assemble(self, map_id: str) -> list[list[int]] | None:
        """Row-major placement (privacy-control): r0=pid//8, c0=pid%8.
        Validated by trace/floor correlation 86.8% (tools/buscar_transformacion.py).
        With per-map geometry (MQTT): piece N at row=N//pieceW, col=N%pieceW.
        """
        pieces = self.maps.get(map_id)
        if not pieces:
            return None
        geo = self.geo(map_id)
        grid = [[0] * geo.total_cols for _ in range(geo.total_rows)]
        expected = geo.cells_x * geo.cells_y
        for pid, piece in pieces.items():
            if pid < 0:  # chunked map: the grid arrives pre-assembled
                for r in range(geo.total_rows):
                    row = grid[r]
                    base = r * geo.total_cols
                    for c in range(geo.total_cols):
                        row[c] = piece[base + c]
                continue
            if len(piece) < expected:  # (B10) truncated/corrupt piece: skipped
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
    """Render a PNG (1 cell = 1px). trace/position in robot-frame mm.

    geo: map geometry (mm→cell transform). If None, the 930's is used
    (cell_mm, ORIGIN_ROW/COL with y pointing down).
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

    # overlay: mm → full-grid cell, then subtract the crop offset
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
                canvas[cell[0]][cell[1]] = -1  # trace marker
    if position:
        cell = to_cell(*position)
        if cell:
            canvas[cell[0]][cell[1]] = -2  # robot marker
    if dock:
        dcell = to_cell(*dock)
        if dcell:
            canvas[dcell[0]][dcell[1]] = -3  # dock marker

    lut = {-1: trace_color, -2: ROBOT_COLOR, -3: DOCK_COLOR, **COLORS}
    bg = COLORS[0]
    px = b"".join(
        b"\x00" + b"".join(bytes(lut.get(v, bg)) for v in row) for row in canvas
    )

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(px, 9))
        + chunk(b"IEND", b"")
    )
