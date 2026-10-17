"""Tests de Fase FA: config [servers], lookup embebido, ciclo de vida."""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import ClientSession

from deebot930_bridge.config import Config, ServersConfig, load_config
from deebot930_bridge.servers.lookup import LookupServer, lookup_response


class TestServersConfig:
    def test_defaults_todo_off_excepto_panel(self):
        cfg = ServersConfig()
        assert cfg.lookup is False and cfg.xmpp is False
        assert cfg.mqtt is False and cfg.conf is False
        assert cfg.panel is True  # nativo, sigue on por defecto
        assert cfg.lookup_port == 8007 and cfg.conf_port == 443

    def test_parse_toml(self, tmp_path):
        p = tmp_path / "c.toml"
        p.write_text(
            '[servers]\nlookup = true\nannounce_ip = "192.168.1.50"\n'
            'lookup_port = 18007\npanel = false\n')
        cfg = load_config(str(p))
        assert cfg.servers.lookup and cfg.servers.announce_ip == "192.168.1.50"
        assert cfg.servers.lookup_port == 18007
        assert cfg.servers.panel is False

    def test_sin_seccion(self):
        assert load_config(None).servers == ServersConfig()


class TestLookupResponse:
    def test_ecomsgnew_sin_espacios(self):
        """El parser del robot es estricto: JSON compacto (truco de Bumper)."""
        r = lookup_response("EcoMsgNew", "192.168.1.50", 5223)
        body = r.text
        assert " " not in body
        assert json.loads(body) == {"ip": "192.168.1.50", "port": 5223, "result": "ok"}

    def test_ecoupdate_al_real(self):
        r = lookup_response("EcoUpdate", "1.2.3.4", 5223)
        d = json.loads(r.text)
        assert d == {"result": "ok", "ip": "47.88.66.164", "port": 8005}

    def test_desconocido_vacio(self):
        assert json.loads(lookup_response("Nope", "1.2.3.4", 5223).text) == {}


class TestLookupServer:
    async def test_post_lookup_do(self):
        srv = LookupServer("192.168.1.50", xmpp_port=5223, listen="127.0.0.1",
                           port=18007)
        await srv.start()
        try:
            async with ClientSession() as s:
                # JSON (como el robot con wget)
                async with s.post("http://127.0.0.1:18007/lookup.do",
                                  json={"todo": "FindBest",
                                        "service": "EcoMsgNew"}) as r:
                    assert r.status == 200
                    body = await r.text()
                    assert " " not in body
                    assert json.loads(body)["port"] == 5223
                # form-urlencoded (variante de la app)
                async with s.post("http://127.0.0.1:18007/lookup.do",
                                  data={"todo": "FindBest",
                                        "service": "EcoUpdate"}) as r:
                    assert json.loads(await r.text())["ip"] == "47.88.66.164"
        finally:
            await srv.stop()

    def test_announce_ip_obligatorio(self):
        with pytest.raises(ValueError):
            LookupServer("", 5223)


class TestNullPanel:
    async def test_noop(self):
        from deebot930_bridge.webpanel import NullPanel

        p = NullPanel()
        await p.start()
        p.build_and_broadcast_map({})
        p.broadcast_raw("x")
        await p.broadcast({"a": 1}, slug="s")  # no lanza nada
