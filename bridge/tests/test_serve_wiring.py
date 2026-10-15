"""Tests de integración del wiring de serve.on_report con transporte falso.

Estos tests existen porque el E2E con el mock no cazó el bug B2 (mid con
namespace en el wire): el mock ignoraba el mid. Ahora el mock valida, y
estos tests fijan el contrato desde el lado del bridge.
"""

from __future__ import annotations

import asyncio
import base64
import lzma
import zlib

import pytest

from deebot930_bridge.config import Config
from deebot930_bridge.map_pieces import MapAssembler  # noqa: F401 (referencia)
from deebot930_bridge.models import DeviceId
from deebot930_bridge.robot import RobotState
from deebot930_bridge.serve import Service


def make_piece_b64(fill: int, size: int = 10000) -> str:
    comp = lzma.compress(bytes([fill]) * size, format=lzma.FORMAT_ALONE,
                         filters=[{"id": lzma.FILTER_LZMA1, "preset": 6}])
    return base64.b64encode(comp[:9] + comp[13:]).decode()


class FakeTransport:
    """Captura los comandos fire() del MqttCommander sin red."""

    def __init__(self):
        self.sent: list[tuple[str, str, object]] = []

    def fire(self, did, command, params=None):
        self.sent.append((command, did.key, params))


def make_service() -> tuple[Service, FakeTransport]:
    svc = Service(Config(), None)
    svc.pub = None  # tipo: ignore — no publicamos en estos tests
    t = FakeTransport()
    svc.mqtt_transport = t
    did = DeviceId.from_key("mock-t9-0001@yna5xh/axm")
    r = RobotState.from_device_id(did)
    key = did.key
    svc.robots[key] = r
    from deebot930_bridge.mqtt_commander import MqttCommander

    svc.commanders[key] = MqttCommander(t, r)
    return svc, t


async def _run(coro):
    return await coro


class TestMinorWalkWiring:
    async def test_major_map_dispara_walk_con_mid_crudo(self):
        """B2: el walk debe pedir getMinorMap con el mid CRUDO ("0"),
        no con el namespace interno ("sn:0")."""
        svc, t = make_service()
        piece_crc = zlib.crc32(bytes([1]) * 10000)
        await svc.on_report("mock-t9-0001@yna5xh/axm", "getMajorMap", {
            "ret": "ok", "mid": "0", "type": "ol", "pieceWidth": 4,
            "pieceHeight": 5, "cellWidth": 100, "cellHeight": 100, "pixel": 50,
            "value": str(piece_crc)})
        # el walk es async con sleeps — darle un instante
        await asyncio.sleep(0.05)
        minor = [c for c in t.sent if c[0] == "getMinorMap"]
        assert minor, "el walk no se lanzó"
        cmd, _, params = minor[0]
        assert params["mid"] == "0", f"mid del wire debe ser crudo, no {params['mid']!r}"
        assert params["type"] == "ol"
        assert params["pieceIndex"] == 0

    async def test_minor_map_responsealimenta_assembler_con_namespace(self):
        svc, t = make_service()
        await svc.on_report("mock-t9-0001@yna5xh/axm", "getMinorMap", {
            "ret": "ok", "mid": "0", "type": "ol", "pieceIndex": 0,
            "pieceValue": make_piece_b64(1)})
        assert svc.asm.piece_count("mock-t9-0001:0") == 1
        assert svc._map_owner.get("mock-t9-0001:0") == "mock-t9-0001@yna5xh/axm"

    async def test_major_map_fail_dispara_chunked(self):
        """T8/N8: getMajorMap fail → getMapInfo (formato chunked)."""
        svc, t = make_service()
        await svc.on_report("mock-t9-0001@yna5xh/axm", "getMajorMap",
                            {"ret": "fail", "errno": 1})
        assert any(c[0] == "getMapInfo" for c in t.sent)

    async def test_map_owner_define_commander_del_walk(self):
        """B6: con dos robots eco-ng, el walk del mapa de B sale por B."""
        svc, t = make_service()
        # segundo robot
        did2 = DeviceId.from_key("otro-robot@vsc5ia/axm")
        r2 = RobotState.from_device_id(did2)
        svc.robots[did2.key] = r2
        from deebot930_bridge.mqtt_commander import MqttCommander

        t2 = FakeTransport()
        svc.commanders[did2.key] = MqttCommander(t2, r2)

        piece_crc = zlib.crc32(bytes([1]) * 10000)
        await svc.on_report(did2.key, "onMajorMap", {
            "ret": "ok", "mid": "0", "type": "ol", "pieceWidth": 4,
            "pieceHeight": 5, "cellWidth": 100, "cellHeight": 100, "pixel": 50,
            "value": str(piece_crc)})
        await asyncio.sleep(0.05)
        assert any(c[0] == "getMinorMap" for c in t2.sent), "walk debe salir por el dueño"
        assert not any(c[0] == "getMinorMap" for c in t.sent)


class TestTracePagination:
    async def test_total_mayor_pide_siguiente_pagina(self):
        """B14: totalCount > página → el bridge pide la siguiente."""
        svc, t = make_service()
        # 2 puntos de 5 bytes, totalCount 10 → debe pedir getMapTrace(2, …)
        buf = bytes([0x07, 0xD0, 0x07, 0xD0, 0x00]) * 2
        comp = lzma.compress(buf, format=lzma.FORMAT_ALONE,
                             filters=[{"id": lzma.FILTER_LZMA1, "preset": 6}])
        await svc.on_report("mock-t9-0001@yna5xh/axm", "getMapTrace", {
            "ret": "ok", "tid": "1", "traceStart": 0, "pointCount": 2,
            "totalCount": 10,
            "traceValue": base64.b64encode(comp).decode()})
        traza_cmds = [c for c in t.sent if c[0] == "getMapTrace"]
        assert traza_cmds, "debe pedir la siguiente página"
        assert traza_cmds[0][2]["traceStart"] == 2
