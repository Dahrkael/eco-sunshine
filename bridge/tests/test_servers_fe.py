"""Tests de Fase FE: auto-generación de certs + announce_ip por entorno.

ensure_certs cubre la instalación limpia (generar CA+cert si falta todo)
sin pisar JAMÁS lo existente (los robots de producción ya confían en la
CA legada de Bumper). Estos tests fijan ese contrato:

- genera el trío completo (+ clave de la CA) cuando no hay nada
- los certs existentes no se tocan (ni a medias, ni con CA ausente)
- par ausente + CA existente → firma con ESA CA (re-emisión sin re-pareo)
- destino inservible → False (serve desactiva los TLS embebidos, no muere)
- los certs generados sirven para TLS con verificación ESTRICTA (py≥3.13)
"""

from __future__ import annotations

import socket
import ssl

import aiomqtt
import pytest
from cryptography import x509

from deebot930_bridge.config import Config
from deebot930_bridge.servers.certs import ensure_certs
from deebot930_bridge.servers.mqtt import EmbeddedMqttBroker
from deebot930_bridge.servers.registry import DeviceRegistry
from deebot930_bridge.serve import Service

DID = "b11fceaf-1234-5678-9abc-def012345678"
BOT_KEY = f"{DID}@yna5xh/axm"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _strict_ctx(ca_file: str) -> ssl.SSLContext:
    """Cliente TLS exigente: CA explícita, hostname off (conectamos por IP),
    VERIFY_X509_STRICT on (el modo que rechazaba las CAs sin AKI)."""
    ctx = ssl.create_default_context(cafile=ca_file)
    ctx.check_hostname = False
    if hasattr(ssl, "VERIFY_X509_STRICT"):
        ctx.verify_flags |= ssl.VERIFY_X509_STRICT
    return ctx


# ---------- generación ----------

class TestEnsureCerts:
    def test_instalacion_limpia_genera_trio_y_clave_de_ca(self, tmp_path):
        ca, crt, key = tmp_path / "ca.crt", tmp_path / "srv.crt", tmp_path / "srv.key"
        assert ensure_certs(str(ca), str(crt), str(key), cn="bumper") is True
        for p in (ca, crt, key, tmp_path / "ca.key"):
            assert p.is_file(), f"falta {p}"
        ca_cert = x509.load_pem_x509_certificate(ca.read_bytes())
        srv_cert = x509.load_pem_x509_certificate(crt.read_bytes())
        # cadena: el leaf lo firma la CA generada
        assert srv_cert.issuer == ca_cert.subject
        assert ca_cert.subject == ca_cert.issuer  # autofirmada
        bc = ca_cert.extensions.get_extension_for_class(x509.BasicConstraints)
        assert bc.value.ca is True and bc.critical
        # AKI en el leaf (lo que el ssl estricto de py≥3.13 exige y el
        # generador original de Bumper no ponía)
        aki = srv_cert.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier)
        assert aki.value.key_identifier is not None

    def test_existentes_no_se_tocan(self, tmp_path):
        ca, crt, key = tmp_path / "ca.crt", tmp_path / "srv.crt", tmp_path / "srv.key"
        for p, payload in ((ca, b"CA-VIEJA"), (crt, b"CRT-VIEJO"),
                           (key, b"KEY-VIEJA")):
            p.write_bytes(payload)
        assert ensure_certs(str(ca), str(crt), str(key)) is True
        assert ca.read_bytes() == b"CA-VIEJA"
        assert crt.read_bytes() == b"CRT-VIEJO"
        assert key.read_bytes() == b"KEY-VIEJA"

    def test_par_a_medias_no_genera_nada(self, tmp_path):
        ca, crt, key = tmp_path / "ca.crt", tmp_path / "srv.crt", tmp_path / "srv.key"
        crt.write_bytes(b"huerfano")
        assert ensure_certs(str(ca), str(crt), str(key)) is False
        assert not ca.exists() and not key.exists()

    def test_ca_ausente_con_par_presente_no_genera_ca(self, tmp_path):
        """Una CA nueva no firmaría el cert existente: solo avisar."""
        ca, crt, key = tmp_path / "ca.crt", tmp_path / "srv.crt", tmp_path / "srv.key"
        crt.write_bytes(b"crt"), key.write_bytes(b"key")
        assert ensure_certs(str(ca), str(crt), str(key)) is True
        assert not ca.exists()

    def test_ca_existente_firma_el_par_nuevo(self, tmp_path):
        """bumper.crt borrado + CA intacta → re-emisión firmada por ESA CA
        (los robots que ya confían en ella no se enteran)."""
        ca, crt, key = tmp_path / "ca.crt", tmp_path / "srv.crt", tmp_path / "srv.key"
        assert ensure_certs(str(ca), str(crt), str(key))
        first = x509.load_pem_x509_certificate(crt.read_bytes())
        crt.unlink(), key.unlink()
        assert ensure_certs(str(ca), str(crt), str(key)) is True
        second = x509.load_pem_x509_certificate(crt.read_bytes())
        ca_cert = x509.load_pem_x509_certificate(ca.read_bytes())
        assert second.issuer == ca_cert.subject
        assert second.serial_number != first.serial_number

    def test_cn_ip_acaba_como_san_de_ip(self, tmp_path):
        ca, crt, key = tmp_path / "ca.crt", tmp_path / "srv.crt", tmp_path / "srv.key"
        import ipaddress

        assert ensure_certs(str(ca), str(crt), str(key), cn="192.168.1.50")
        san = (x509.load_pem_x509_certificate(crt.read_bytes())
               .extensions.get_extension_for_class(x509.SubjectAlternativeName))
        assert ipaddress.ip_address("192.168.1.50") in san.value.get_values_for_type(
            x509.IPAddress)

    def test_destino_inservible_devuelve_false_sin_medidas(self, tmp_path):
        bloqueo = tmp_path / "bloqueo"
        bloqueo.write_text("no soy un directorio")
        ca = tmp_path / "d" / "ca.crt"  # destino VÁLIDO: no debe llegar a crearse
        assert ensure_certs(str(ca), str(bloqueo / "srv.crt"),
                            str(bloqueo / "srv.key")) is False
        assert not ca.exists() and list(tmp_path.glob("**/*.crt")) == []


# ---------- los certs generados sirven para TLS ----------

class TestCertsGeneradosSirvenParaTLS:
    async def test_pubsub_tls_con_verificacion_estrita(self, tmp_path):
        ensure_certs(str(tmp_path / "ca.crt"), str(tmp_path / "srv.crt"),
                     str(tmp_path / "srv.key"))
        broker = EmbeddedMqttBroker(("127.0.0.1", free_port()), DeviceRegistry(),
                                     server_cert=str(tmp_path / "srv.crt"),
                                     server_key=str(tmp_path / "srv.key"))
        await broker.start()
        try:
            ctx = _strict_ctx(str(tmp_path / "ca.crt"))
            async with aiomqtt.Client(hostname="127.0.0.1", port=broker.port,
                                      identifier="sub@bumper/test",
                                      tls_context=ctx) as sub:
                await sub.subscribe("iot/#", qos=0)
                async with aiomqtt.Client(hostname="127.0.0.1", port=broker.port,
                                          identifier=BOT_KEY,
                                          username=f"SN-{DID}",
                                          tls_context=ctx) as bot:
                    await bot.publish(f"iot/atr/onPos/{DID}/yna5xh/axm/j",
                                      b'{"ret":"ok"}', qos=0)
                import asyncio

                msg = await asyncio.wait_for(anext(sub.messages), 3)
                assert str(msg.topic) == f"iot/atr/onPos/{DID}/yna5xh/axm/j"
        finally:
            await broker.stop()


# ---------- wiring de serve (FE) ----------

class TestServeWiringFE:
    async def test_certs_ausentes_se_generan_y_el_broker_arranca(self, tmp_path):
        """Instalación limpia: serve genera los certs y arranca el broker
        embebido — el cliente [bumper_mqtt] pasa al loopback como en FC."""
        cfg = Config()
        cfg.servers.listen = "127.0.0.1"
        cfg.servers.mqtt = True
        cfg.servers.mqtt_port = free_port()
        cfg.servers.ca_file = str(tmp_path / "ca.crt")
        cfg.servers.server_cert = str(tmp_path / "srv.crt")
        cfg.servers.server_key = str(tmp_path / "srv.key")
        cfg.bumper_mqtt.enabled = True
        cfg.bumper_mqtt.host = "bumper-externo"
        svc = Service(cfg, None)
        await svc._start_embedded_servers()
        try:
            assert "mqtt" in svc.embedded
            assert (tmp_path / "ca.crt").is_file()
            assert (tmp_path / "srv.crt").is_file()
            assert (tmp_path / "srv.key").is_file()
            assert cfg.bumper_mqtt.host == "127.0.0.1"
            assert cfg.bumper_mqtt.port == cfg.servers.mqtt_port
            assert cfg.bumper_mqtt.tls is True
        finally:
            if "mqtt" in svc.embedded:
                await svc.embedded["mqtt"].stop()

    async def test_certs_irrecuperables_desactivan_los_tls_embebidos(self, tmp_path):
        """Destino inservible → sin XMPP/MQTT embebido y el cliente intacto
        (serve no muere, compat con bumper externo)."""
        bloqueo = tmp_path / "bloqueo"
        bloqueo.write_text("x")
        cfg = Config()
        cfg.servers.listen = "127.0.0.1"
        cfg.servers.mqtt = True
        cfg.servers.server_cert = str(bloqueo / "no.crt")
        cfg.servers.server_key = str(bloqueo / "no.key")
        cfg.servers.mqtt_port = free_port()
        cfg.bumper_mqtt.enabled = True
        cfg.bumper_mqtt.host = "bumper-externo"
        svc = Service(cfg, None)
        await svc._start_embedded_servers()
        assert "mqtt" not in svc.embedded
        assert cfg.bumper_mqtt.host == "bumper-externo"
        assert cfg.bumper_mqtt.tls is None  # sin tocar
