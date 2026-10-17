"""Tests del wiring FB/FE del XMPP embebido en serve (ca_file incluido)."""

from __future__ import annotations

from ecovacs_bridge.config import Config
from ecovacs_bridge.serve import Service
from tests.test_servers_fc import _free_port


class TestXmppWiring:
    def _cfg(self, tmp_path, unwritable: bool = False) -> Config:
        cfg = Config()
        cfg.servers.xmpp = True
        cfg.servers.listen = "127.0.0.1"
        cfg.servers.xmpp_port = _free_port()
        cfg.state_file = str(tmp_path / "state.json")
        cfg.servers.ca_file = str(tmp_path / "ca-mia.crt")
        if unwritable:
            # destino inservible: un fichero regular donde iría el directorio
            blocker = tmp_path / "bloqueo"
            blocker.write_text("no soy un directorio")
            cfg.servers.server_cert = str(blocker / "srv.crt")
            cfg.servers.server_key = str(blocker / "srv.key")
        else:
            cfg.servers.server_cert = str(tmp_path / "no.crt")
            cfg.servers.server_key = str(tmp_path / "no.key")
        return cfg

    async def test_xmpp_falla_cliente_con_config_externa(self, tmp_path):
        """Certs irrecuperables → el cliente [bumper] queda INTACTO (ca incluida)."""
        cfg = self._cfg(tmp_path, unwritable=True)
        cfg.bumper.host = "bumper-externo"
        cfg.bumper.ca_file = "/otra/ca.crt"
        svc = Service(cfg, None)
        await svc._start_embedded_servers()
        assert "xmpp" not in svc.embedded  # ensure_certs falló → desactivado
        assert cfg.bumper.host == "bumper-externo"
        assert cfg.bumper.ca_file == "/otra/ca.crt"

    async def test_xmpp_arranca_cliente_a_loopback_con_ca_del_server(self, tmp_path):
        """XMPP arriba → host/port/ca_file del cliente apuntan al embebido."""
        from pathlib import Path

        # generar certs reales para que arranque (usa el generador de FE)
        from ecovacs_bridge.servers.certs import ensure_certs

        cfg = self._cfg(tmp_path)
        ca = str(tmp_path / "ca-mia.crt")
        assert ensure_certs(ca, str(tmp_path / "srv.crt"),
                            str(tmp_path / "srv.key"), cn="127.0.0.1")
        cfg.servers.server_cert = str(tmp_path / "srv.crt")
        cfg.servers.server_key = str(tmp_path / "srv.key")
        svc = Service(cfg, None)
        await svc._start_embedded_servers()
        try:
            assert "xmpp" in svc.embedded
            assert cfg.bumper.host == "127.0.0.1"
            assert cfg.bumper.port == cfg.servers.xmpp_port
            # la CA del cliente es la del servidor embebido (fix del review
            # final: antes quedaba la de [bumper], potencialmente otra)
            assert cfg.bumper.ca_file == cfg.servers.ca_file
        finally:
            await svc._stop_embedded_servers()
