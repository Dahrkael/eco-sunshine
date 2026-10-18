"""Chunked format tests (getMapInfo, T8/N8) — bug B9."""

from __future__ import annotations

import base64
import lzma

from ecovacs_bridge.map_pieces import MapAssembler, MapGeometry


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
    """Pseudo-random data: does not collapse under compression (realistic chunks)."""
    import random

    rng = random.Random(seed)
    return bytes(rng.randrange(4) for _ in range(n))


class TestChunked:
    def test_roundtrip_two_chunks(self):
        grid = noisy_grid(400 * 500)
        chunks, meta = make_chunked(grid)
        assert len(chunks) >= 2
        asm = MapAssembler()
        for i, ch in enumerate(chunks):
            done = asm.add_chunk_json("0", i, ch, len(chunks), meta)
            if i < len(chunks) - 1:
                assert not done
        assert done, "the last chunk must assemble the map"
        g = asm.assemble("0")
        assert g is not None and len(g) == 500 and len(g[0]) == 400
        assert b"".join(bytes(row) for row in g) == grid

    def test_chunked_geometry_not_giant(self):
        """B9: from_chunked_json uses totalWidth/Height as CELLS, not as
        pieces — width=100 must not build a 100M-cell grid."""
        geo = MapGeometry.from_chunked_json(
            {"width": 100, "height": 100, "totalWidth": 100, "totalHeight": 100,
             "pixel": 5})
        assert geo.total_cols == 100 and geo.total_rows == 100
        assert geo.total_rows * geo.total_cols == 10_000

    def test_absurd_geometry_rejected(self):
        grid = bytes([1]) * 100
        # corrupt metadata: huge totalWidth
        comp = lzma.compress(grid, format=lzma.FORMAT_ALONE,
                             filters=[{"id": lzma.FILTER_LZMA1, "preset": 6}])
        b64 = base64.b64encode(comp).decode()
        asm = MapAssembler()
        done = asm.add_chunk_json("0", 0, b64, 1,
                                  {"totalWidth": 5000, "totalHeight": 5000,
                                   "pixel": 5, "width": 100, "height": 100})
        assert not done  # 25M cells > 2000x2000 cap → discarded

    def test_incomplete_chunks_do_not_assemble(self):
        grid = noisy_grid(5000)
        chunks, meta = make_chunked(grid, chunk_chars=64)
        asm = MapAssembler()
        assert len(chunks) >= 3
        assert not asm.add_chunk_json("0", 0, chunks[0], len(chunks), meta)
        assert not asm.add_chunk_json("0", 2, chunks[2], len(chunks), meta)
        assert asm.piece_count("0") == 0

    def test_chunked_replaces_old_pieces(self):
        """B15: the same mid migrating from pieces to chunked mixes no formats."""
        grid = noisy_grid(3000)
        chunks, meta = make_chunked(grid, chunk_chars=100)
        assert len(chunks) >= 2
        asm = MapAssembler()
        asm.maps.setdefault("0", {})[0] = bytes([9]) * 10000  # old piece
        for i, ch in enumerate(chunks):
            done = asm.add_chunk_json("0", i, ch, len(chunks),
                                      {**meta, "totalWidth": len(grid), "width": len(grid),
                                       "totalHeight": 1, "height": 1})
        assert done
        assert set(asm.maps["0"].keys()) == {-1}  # only the full grid
