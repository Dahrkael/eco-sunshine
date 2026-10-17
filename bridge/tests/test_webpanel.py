"""Panel tests: broadcast_raw (bug B1) and map serialization."""

from __future__ import annotations

import asyncio
import json

from ecovacs_bridge.config import Config
from ecovacs_bridge.serve import Service
from ecovacs_bridge.webpanel import WebPanel


class FakeWS:
    """Fake WebSocket: captures send_str without a network."""

    def __init__(self):
        self.sent: list[str] = []

    async def send_str(self, s: str) -> None:
        self.sent.append(s)


def make_panel() -> tuple[WebPanel, Service]:
    svc = Service(Config(), None)
    panel = WebPanel(svc, port=0)
    return panel, svc


class TestBroadcastRaw:
    async def test_envia_a_todos_sin_excepciones(self, caplog):
        """B1: asyncio without the import was killing the broadcast and
        KICKING clients out — one map per WS must reach everyone and the
        list must stay intact."""
        panel, _ = make_panel()
        ws1, ws2 = FakeWS(), FakeWS()
        panel._ws_clients = [ws1, ws2]
        panel.broadcast_raw(json.dumps({"type": "map", "rows": []}))
        await asyncio.sleep(0.05)  # let the send tasks flow
        assert ws1.sent and ws2.sent
        assert len(panel._ws_clients) == 2  # nobody kicked out

    async def test_ws_roto_se_expulsa_limpiamente(self):
        panel, _ = make_panel()

        class Roto(FakeWS):
            async def send_str(self, s: str) -> None:
                raise ConnectionResetError()

        roto, sano = Roto(), FakeWS()
        panel._ws_clients = [roto, sano]
        panel.broadcast_raw(json.dumps({"type": "map"}))
        await asyncio.sleep(0.05)
        assert sano.sent
        assert panel._ws_clients == [sano]
