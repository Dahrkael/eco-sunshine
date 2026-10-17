"""Tests de MapAssembler: geometría por mapa (XML y JSON), transform mm→celda."""

from __future__ import annotations

import base64
import lzma
import zlib

from ecovacs_bridge.map_pieces import MapAssembler, MapGeometry


def make_piece(fill: int, size: int = 10000) -> str:
    comp = lzma.compress(bytes([fill]) * size, format=lzma.FORMAT_ALONE,
                         filters=[{"id": lzma.FILTER_LZMA1, "preset": 6}])
    return base64.b64encode(comp[:9] + comp[13:]).decode()


class TestGeometry:
    def test_default_930(self):
        g = MapGeometry()
        assert g.total_rows == 800 and g.total_cols == 800
        assert g.cell_mm == 50 and g.y_down
        row, col = g.mm_to_cell(0, 0)
        assert (row, col) == (401, 401)
        # x+100mm → 2 columnas; y+50mm → 1 fila (y-down)
        assert g.mm_to_cell(100, 50) == (402, 403)

    def test_json_centered_y_up(self):
        g = MapGeometry.from_major_map_json(
            {"pieceWidth": 4, "pieceHeight": 5, "cellWidth": 100,
             "cellHeight": 100, "pixel": 50})
        assert g.total_cols == 400 and g.total_rows == 500
        assert (g.origin_col, g.origin_row) == (200, 250)
        assert not g.y_down
        # x+100mm → 2 cols; y+100mm → 2 filas MENOS (y-up)
        assert g.mm_to_cell(100, 100) == (248, 202)

    def test_from_xml_attrs(self):
        g = MapGeometry.from_xml_attrs({"w": 100, "h": 100, "c": 8, "r": 8, "p": 50})
        assert g.total_rows == 800 and g.cell_mm == 50 and g.y_down


class TestAssembler:
    def test_xml_geometry_compatible_930(self):
        asm = MapAssembler()
        asm.set_geometry("0", {"w": 100, "h": 100, "c": 8, "r": 8, "p": 50,
                               "m": str(zlib.crc32(bytes([1]) * 10000))})
        assert asm.add_piece("0", 0, make_piece(1))
        grid = asm.assemble("0")
        assert grid is not None
        assert len(grid) == 800 and len(grid[0]) == 800
        assert grid[0][0] == 1 and grid[0][99] == 1
        assert grid[0][100] == 0  # pieza 1 no cargada

    def test_json_geometry_and_crc(self):
        asm = MapAssembler()
        piece = bytes([1]) * 10000  # 100x100
        asm.set_geometry_json("0", {"mid": "0", "type": "ol", "pieceWidth": 4,
                                    "pieceHeight": 5, "cellWidth": 100,
                                    "cellHeight": 100, "pixel": 50,
                                    "value": str(zlib.crc32(piece))})
        geo = asm.geo("0")
        assert (geo.piece_cols, geo.piece_rows) == (4, 5)
        assert asm.add_piece_json("0", 0, make_piece(1))
        # CRC incorrecto → rechazada (pid 0, dentro del rango de la spec)
        asm.set_geometry_json("0", {"value": "12345", "pieceWidth": 4, "pieceHeight": 5,
                                    "cellWidth": 100, "cellHeight": 100, "pixel": 50})
        assert not asm.add_piece_json("0", 0, make_piece(2))
        grid = asm.assemble("0")
        assert grid is not None
        assert len(grid) == 500 and len(grid[0]) == 400  # 5x100 filas, 4x100 cols

    def test_piece_crc_accessor(self):
        asm = MapAssembler()
        p = bytes([1]) * 10000
        asm.set_geometry_json("0", {"value": str(zlib.crc32(p)), "pieceWidth": 4,
                                    "pieceHeight": 5, "cellWidth": 100,
                                    "cellHeight": 100, "pixel": 50})
        assert asm.piece_crc("0", 0) is None
        asm.add_piece_json("0", 0, make_piece(1))
        assert asm.piece_crc("0", 0) == zlib.crc32(p)

    def test_spec_change_counter(self):
        asm = MapAssembler()
        p = bytes([1]) * 10000
        v1 = str(zlib.crc32(p))
        # primera spec con crcs → cuenta (dispara el minor walk inicial)
        asm.set_geometry_json("0", {"value": v1, "pieceWidth": 4, "pieceHeight": 5,
                                    "cellWidth": 100, "cellHeight": 100, "pixel": 50})
        assert asm.spec_changes == 1
        # idéntica → no cuenta
        asm.set_geometry_json("0", {"value": v1, "pieceWidth": 4, "pieceHeight": 5,
                                    "cellWidth": 100, "cellHeight": 100, "pixel": 50})
        assert asm.spec_changes == 1
        # cambia → cuenta
        asm.set_geometry_json("0", {"value": v1 + "," + str(zlib.crc32(bytes([2]) * 10000)),
                                    "pieceWidth": 4, "pieceHeight": 5,
                                    "cellWidth": 100, "cellHeight": 100, "pixel": 50})
        assert asm.spec_changes == 2
