"""Tests del panel: broadcast_raw (bug B1) y serialización del mapa."""

from __future__ import annotations

import asyncio
import json

from ecovacs_bridge.config import Config
from ecovacs_bridge.serve import Service
from ecovacs_bridge.webpanel import WebPanel


class FakeWS:
    """WebSocket falso: captura send_str sin red."""

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
        """B1: asyncio sin importar mataba el broadcast y EXPULSABA a los
        clientes — un mapa por WS debe llegar a todos y no tocar la lista."""
        panel, _ = make_panel()
        ws1, ws2 = FakeWS(), FakeWS()
        panel._ws_clients = [ws1, ws2]
        panel.broadcast_raw(json.dumps({"type": "map", "rows": []}))
        await asyncio.sleep(0.05)  # deja fluir las tasks de envío
        assert ws1.sent and ws2.sent
        assert len(panel._ws_clients) == 2  # nadie expulsado

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
