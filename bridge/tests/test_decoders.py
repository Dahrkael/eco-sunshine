"""Tests de decoders: piezas LZMA/zstd (auto), traza 5B MQTT, envelope JSON."""

from __future__ import annotations

import base64
import lzma
import struct

import pytest

from ecovacs_bridge.map_pieces import (
    ZSTD_MAGIC,
    decode_piece,
    decode_piece_auto,
    decode_trace_mqtt,
    decompress_auto,
)
from ecovacs_bridge.mqtt_transport import build_envelope


def make_piece_bytes(size: int = 10000, fill: int = 1) -> bytes:
    """Grid sintético de `size` celdas → payload LZMA-alone como el robot."""
    grid = bytes([fill]) * size
    comp = lzma.compress(
        grid,
        format=lzma.FORMAT_ALONE,
        filters=[{"id": lzma.FILTER_LZMA1, "preset": 6}],
    )
    # LZMA-alone de stdlib incluye props(1)+dict(4)+size(8); el robot manda
    # props(1)+dict(4)+size(4) → recortar a la cabecera de 9 bytes del wire
    return comp[:9] + comp[13:]


def make_zstd_bytes(size: int = 10000, fill: int = 2) -> bytes:
    zstandard = pytest.importorskip("zstandard")
    return zstandard.ZstdCompressor().compress(bytes([fill]) * size)


class TestPieceAuto:
    def test_lzma_wire_format_decode(self):
        raw = make_piece_bytes(10000, fill=1)
        assert decompress_auto(raw) == bytes([1]) * 10000

    def test_decode_piece_legacy_930(self):
        raw = make_piece_bytes(10000, fill=3)
        b64 = base64.b64encode(raw).decode()
        assert decode_piece(b64) == bytes([3]) * 10000

    def test_zstd_magic_detected(self):
        raw = make_zstd_bytes(10000, fill=2)
        assert raw[:4] == ZSTD_MAGIC
        assert decompress_auto(raw) == bytes([2]) * 10000

    def test_decode_piece_auto_route_zstd(self):
        b64 = base64.b64encode(make_zstd_bytes(10000, fill=1)).decode()
        assert decode_piece_auto(b64) == bytes([1]) * 10000

    def test_decode_piece_auto_size_hint_variable(self):
        # pieza MQTT con geometría distinta (80x80 = 6400 celdas)
        raw = make_piece_bytes(6400, fill=0)
        assert decompress_auto(raw, size_hint=6400) == bytes(6400)

    def test_lzma_corrupto_raises(self):
        with pytest.raises(lzma.LZMAError):
            decompress_auto(b"\x5d\x00\x00\x04\x00" + b"\x00" * 8 + b"garbage!")


class TestTraceMqtt:
    def _encode(self, buf: bytes) -> str:
        # la traza viaja como LZMA-alone ESTÁNDAR (cabecera de 13 bytes)
        comp = lzma.compress(buf, format=lzma.FORMAT_ALONE,
                             filters=[{"id": lzma.FILTER_LZMA1, "preset": 6}])
        return base64.b64encode(comp).decode()

    def test_roundtrip_5bytes(self):
        # codificar puntos conocidos al formato wire: y+2000, x+2000, flags
        pts = [(0.0, 0.0, False), (100.0, -50.0, False), (250.0, 250.0, True)]
        buf = b"".join(
            struct.pack(">hhB", int(y) + 2000, int(x) + 2000, 0x80 if d else 0)
            for x, y, d in pts
        )
        out = decode_trace_mqtt(self._encode(buf))
        assert out == pts

    def test_empty(self):
        assert decode_trace_mqtt(self._encode(b"")) == []


class TestEnvelope:
    def test_envelope_campos(self):
        env = build_envelope({"act": "go"})
        h = env["header"]
        assert h["pri"] == 2
        assert h["ver"] == "0.0.22"
        assert h["tzm"] == 60
        assert h["ts"].isdigit() and len(h["ts"]) >= 13
        data = env["body"]["data"]
        assert data["act"] == "go"
        assert "bdTaskID" in data and data["bdTaskID"].isdigit()

    def test_envelope_no_mutates_params(self):
        params = {"act": "go"}
        build_envelope(params)
        assert params == {"act": "go"}
