"""serve.on_report wiring tests with a fake transport.

These pin the bridge-side contract for the map walks (raw vs namespaced
mids, per-owner commanders, trace pagination).
"""

from __future__ import annotations

import asyncio
import base64
import lzma
import zlib

import pytest

from ecovacs_bridge.config import Config
from ecovacs_bridge.map_pieces import MapAssembler  # noqa: F401 (reference)
from ecovacs_bridge.models import DeviceId
from ecovacs_bridge.robot import RobotState
from ecovacs_bridge.serve import Service


def make_piece_b64(fill: int, size: int = 10000) -> str:
    comp = lzma.compress(bytes([fill]) * size, format=lzma.FORMAT_ALONE,
                         filters=[{"id": lzma.FILTER_LZMA1, "preset": 6}])
    return base64.b64encode(comp[:9] + comp[13:]).decode()


class FakeTransport:
    """Captures MqttCommander fire() commands without a network."""

    def __init__(self):
        self.sent: list[tuple[str, str, object]] = []

    def fire(self, did, command, params=None):
        self.sent.append((command, did.key, params))


def make_service() -> tuple[Service, FakeTransport]:
    svc = Service(Config(), None)
    svc.pub = None  # no publishing in these tests
    t = FakeTransport()
    svc.mqtt_transport = t
    did = DeviceId.from_key("mock-t9-0001@yna5xh/axm")
    r = RobotState.from_device_id(did)
    key = did.key
    svc.robots[key] = r
    from ecovacs_bridge.mqtt_commander import MqttCommander

    svc.commanders[key] = MqttCommander(t, r)
    return svc, t


async def _run(coro):
    return await coro


class TestMinorWalkWiring:
    async def test_major_map_triggers_walk_with_raw_mid(self):
        """B2: the walk must request getMinorMap with the RAW mid ("0"),
        not the internal namespace ("sn:0")."""
        svc, t = make_service()
        piece_crc = zlib.crc32(bytes([1]) * 10000)
        await svc.on_report("mock-t9-0001@yna5xh/axm", "getMajorMap", {
            "ret": "ok", "mid": "0", "type": "ol", "pieceWidth": 4,
            "pieceHeight": 5, "cellWidth": 100, "cellHeight": 100, "pixel": 50,
            "value": str(piece_crc)})
        # the walk is async with sleeps — give it a moment
        await asyncio.sleep(0.05)
        minor = [c for c in t.sent if c[0] == "getMinorMap"]
        assert minor, "the walk did not start"
        cmd, _, params = minor[0]
        assert params["mid"] == "0", f"wire mid must be raw, not {params['mid']!r}"
        assert params["type"] == "ol"
        assert params["pieceIndex"] == 0

    async def test_minor_map_response_feeds_assembler_with_namespace(self):
        svc, t = make_service()
        await svc.on_report("mock-t9-0001@yna5xh/axm", "getMinorMap", {
            "ret": "ok", "mid": "0", "type": "ol", "pieceIndex": 0,
            "pieceValue": make_piece_b64(1)})
        assert svc.asm.piece_count("mock-t9-0001:0") == 1
        assert svc._map_owner.get("mock-t9-0001:0") == "mock-t9-0001@yna5xh/axm"

    async def test_major_map_fail_triggers_chunked(self):
        """T8/N8: getMajorMap fail → getMapInfo (chunked format)."""
        svc, t = make_service()
        await svc.on_report("mock-t9-0001@yna5xh/axm", "getMajorMap",
                            {"ret": "fail", "errno": 1})
        assert any(c[0] == "getMapInfo" for c in t.sent)

    async def test_map_owner_defines_walks_commander(self):
        """B6: with two eco-ng robots, B's map walk goes out through B."""
        svc, t = make_service()
        # second robot
        did2 = DeviceId.from_key("other-robot@vsc5ia/axm")
        r2 = RobotState.from_device_id(did2)
        svc.robots[did2.key] = r2
        from ecovacs_bridge.mqtt_commander import MqttCommander

        t2 = FakeTransport()
        svc.commanders[did2.key] = MqttCommander(t2, r2)

        piece_crc = zlib.crc32(bytes([1]) * 10000)
        await svc.on_report(did2.key, "onMajorMap", {
            "ret": "ok", "mid": "0", "type": "ol", "pieceWidth": 4,
            "pieceHeight": 5, "cellWidth": 100, "cellHeight": 100, "pixel": 50,
            "value": str(piece_crc)})
        await asyncio.sleep(0.05)
        assert any(c[0] == "getMinorMap" for c in t2.sent), "walk must go out through the owner"
        assert not any(c[0] == "getMinorMap" for c in t.sent)


class TestTracePagination:
    async def test_total_greater_requests_next_page(self):
        """B14: totalCount > page → the bridge requests the next one."""
        svc, t = make_service()
        # 2 5-byte points, totalCount 10 → must request getMapTrace(2, …)
        buf = bytes([0x07, 0xD0, 0x07, 0xD0, 0x00]) * 2
        comp = lzma.compress(buf, format=lzma.FORMAT_ALONE,
                             filters=[{"id": lzma.FILTER_LZMA1, "preset": 6}])
        await svc.on_report("mock-t9-0001@yna5xh/axm", "getMapTrace", {
            "ret": "ok", "tid": "1", "traceStart": 0, "pointCount": 2,
            "totalCount": 10,
            "traceValue": base64.b64encode(comp).decode()})
        trace_cmds = [c for c in t.sent if c[0] == "getMapTrace"]
        assert trace_cmds, "next page must be requested"
        assert trace_cmds[0][2]["traceStart"] == 2
