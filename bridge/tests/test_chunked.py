"""Tests del formato chunked (getMapInfo, T8/N8) — bug B9."""

from __future__ import annotations

import base64
import lzma

from deebot930_bridge.map_pieces import MapAssembler, MapGeometry


def make_chunked(grid: bytes, chunk_chars: int = 4000):
    comp = lzma.compress(grid, format=lzma.FORMAT_ALONE,
                         filters=[{"id": lzma.FILTER_LZMA1, "preset": 6}])
    b64 = base64.b64encode(comp).decode()
    n = (len(b64) + chunk_chars - 1) // chunk_chars
    chunks = [b64[i * chunk_chars:(i + 1) * chunk_chars] for i in range(n)]
    meta = {"pixel": 50, "width": 400, "height": 500,
            "totalWidth": 400, "totalHeight": 500}
    return chunks, meta


def noisy_grid(n: int, seed: int = 7) -> bytes:
    """Datos pseudo-aleatorios: no colapsan al comprimir (chunks reales)."""
    import random

    rng = random.Random(seed)
    return bytes(rng.randrange(4) for _ in range(n))


class TestChunked:
    def test_roundtrip_dos_chunks(self):
        grid = noisy_grid(400 * 500)
        chunks, meta = make_chunked(grid)
        assert len(chunks) >= 2
        asm = MapAssembler()
        for i, ch in enumerate(chunks):
            done = asm.add_chunk_json("0", i, ch, len(chunks), meta)
            if i < len(chunks) - 1:
                assert not done
        assert done, "el último chunk debe montar el mapa"
        g = asm.assemble("0")
        assert g is not None and len(g) == 500 and len(g[0]) == 400
        assert b"".join(bytes(row) for row in g) == grid

    def test_geometria_chunked_no_gigante(self):
        """B9: from_chunked_json usa totalWidth/Height como CELDAS, no como
        piezas — un width=100 no debe montar un grid de 100M celdas."""
        geo = MapGeometry.from_chunked_json(
            {"width": 100, "height": 100, "totalWidth": 100, "totalHeight": 100,
             "pixel": 5})
        assert geo.total_cols == 100 and geo.total_rows == 100
        assert geo.total_rows * geo.total_cols == 10_000

    def test_geometria_absurda_rechazada(self):
        grid = bytes([1]) * 100
        # metadata corrupta: totalWidth enorme
        comp = lzma.compress(grid, format=lzma.FORMAT_ALONE,
                             filters=[{"id": lzma.FILTER_LZMA1, "preset": 6}])
        b64 = base64.b64encode(comp).decode()
        asm = MapAssembler()
        done = asm.add_chunk_json("0", 0, b64, 1,
                                  {"totalWidth": 5000, "totalHeight": 5000,
                                   "pixel": 5, "width": 100, "height": 100})
        assert not done  # 25M celdas > límite 2000x2000 → descartado

    def test_chunks_incompletos_no_montan(self):
        grid = noisy_grid(5000)
        chunks, meta = make_chunked(grid, chunk_chars=64)
        asm = MapAssembler()
        assert len(chunks) >= 3
        assert not asm.add_chunk_json("0", 0, chunks[0], len(chunks), meta)
        assert not asm.add_chunk_json("0", 2, chunks[2], len(chunks), meta)
        assert asm.piece_count("0") == 0

    def test_chunked_sustituye_piezas_viejas(self):
        """B15: mismo mid migrando de piezas a chunked no mezcla formatos."""
        grid = noisy_grid(3000)
        chunks, meta = make_chunked(grid, chunk_chars=100)
        assert len(chunks) >= 2
        asm = MapAssembler()
        asm.maps.setdefault("0", {})[0] = bytes([9]) * 10000  # pieza vieja
        for i, ch in enumerate(chunks):
            done = asm.add_chunk_json("0", i, ch, len(chunks),
                                      {**meta, "totalWidth": len(grid), "width": len(grid),
                                       "totalHeight": 1, "height": 1})
        assert done
        assert set(asm.maps["0"].keys()) == {-1}  # solo el grid completo
