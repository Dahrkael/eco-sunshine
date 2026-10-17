"""Tests de Fase FC: broker MQTT embebido — auth estilo Bumper + routing.

El broker es amqtt (matching de filtros por niveles, sin el bug de '+'
de hbmqtt), así que estos tests fijan el contrato que el sistema usa:

- auth: bots {did}@{cls}/{res} → DeviceRegistry; clients bumper/ecouser
- wildcard '+' = UN nivel exacto, CON guiones (dids UUID eco-ng)
- routing pub/sub estándar entre el MqttTransport (iot/#) y un robot mock
- listener TLS con certs de ServersConfig
- wiring de serve: servers.mqtt=true → cliente [bumper_mqtt] a loopback
"""

from __future__ import annotations

import asyncio
import json
import socket
import ssl
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import aiomqtt
import pytest
from amqtt.plugins.base import BaseContext

from deebot930_bridge.config import Config
from deebot930_bridge.models import DeviceId
from deebot930_bridge.mqtt_transport import MqttTransport
from deebot930_bridge.servers.mqtt import BridgeAuthPlugin, EmbeddedMqttBroker
from deebot930_bridge.servers.registry import DeviceRegistry
from deebot930_bridge.serve import Service

# did eco-ng real: UUID con guiones — el caso que el '+' de hbmqtt rompía
DID = "b11fceaf-1234-5678-9abc-def012345678"
CLS, RES = "yna5xh", "axm"
BOT_KEY = f"{DID}@{CLS}/{RES}"


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def sess(client_id: str, username: str | None = None,
         password: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(client_id=client_id, username=username,
                           password=password)


def make_plugin(registry: DeviceRegistry) -> BridgeAuthPlugin:
    """Contexto como el que le da amqtt: copia con _broker_instance → broker."""
    broker = SimpleNamespace(registry=registry)
    ctx = BaseContext()
    ctx._broker_instance = broker  # noqa: SLF001 (la copia de amqtt apunta al broker)
    return BridgeAuthPlugin(ctx)


# ---------- auth (plugin, unit) ----------

class TestAuthPlugin:
    async def test_bot_eco_ng_se_registra(self):
        reg = DeviceRegistry()
        ok = await make_plugin(reg).authenticate(
            session=sess(BOT_KEY, username=f"SN-{DID}", password="mock"))
        assert ok is True
        bot = reg.bot_get(DID)
        assert bot is not None
        assert bot["company"] == "eco-ng"
        assert bot["sn"] == f"SN-{DID}"  # el SN viaja en el username
        assert bot["device_class"] == CLS and bot["resource"] == RES

    async def test_bot_sin_username_usa_did_como_sn(self):
        reg = DeviceRegistry()
        assert await make_plugin(reg).authenticate(session=sess(BOT_KEY)) is True
        assert reg.bot_get(DID)["sn"] == DID

    async def test_client_bumper_aceptado_y_registrado(self):
        """bridge@bumper/bridge — el client-id del MqttTransport del bridge."""
        reg = DeviceRegistry()
        assert await make_plugin(reg).authenticate(session=sess("bridge@bumper/bridge")) is True
        cl = reg.client_get("bridge")
        assert cl == {"user_id": "bridge", "realm": "bumper",
                      "resource": "bridge", "xmpp": False}

    async def test_client_ecouser_aceptado_sin_cuentas(self):
        """use_auth=False (default): LAN sin passwords, como Bumper."""
        reg = DeviceRegistry()
        ok = await make_plugin(reg).authenticate(session=sess("fuid_x@ecouser/ios"))
        assert ok is True
        assert reg.client_get("ios")["user_id"] == "fuid_x"

    async def test_client_use_auth_true_sin_authcode_rechaza(self):
        reg = DeviceRegistry(use_auth=True)
        ok = await make_plugin(reg).authenticate(session=sess("app@bumper/ios"))
        assert ok is False
        assert reg.client_get("ios") is None

    async def test_helperbot_aceptado_sin_registrar(self):
        reg = DeviceRegistry()
        ok = await make_plugin(reg).authenticate(session=sess("helperbot@bumper/helperbot"))
        assert ok is True
        assert reg.client_get("helperbot") is None

    @pytest.mark.parametrize("client_id", ["sin-arroba", "a@b", ""])
    async def test_client_id_malformado_rechazado(self, client_id):
        reg = DeviceRegistry()
        assert await make_plugin(reg).authenticate(session=sess(client_id)) is False
        assert not reg.bots and not reg.clients


# ---------- broker en vivo (TCP plano) ----------

class TestBrokerRouting:
    async def test_report_atr_llega_al_bridge_y_bot_registrado(self):
        """El robot publica iot/atr/…, el MqttTransport (suscrito a iot/#)
        lo recibe — y el CONNECT del bot lo deja en el DeviceRegistry."""
        reg = DeviceRegistry()
        broker = EmbeddedMqttBroker(("127.0.0.1", free_port()), reg)
        await broker.start()
        try:
            got: list[tuple[str, str, dict]] = []

            async def on_data(key, name, data):
                got.append((key, name, data))

            t = MqttTransport("127.0.0.1", broker.port, ca_file=None,
                              on_data=on_data)
            await t.connect()
            listen = asyncio.create_task(t.listen())
            try:
                async with aiomqtt.Client(
                        hostname="127.0.0.1", port=broker.port,
                        identifier=BOT_KEY, username=f"SN-{DID}") as robot:
                    await robot.publish(
                        f"iot/atr/onPos/{DID}/{CLS}/{RES}/j",
                        json.dumps({"ret": "ok",
                                    "deebotPos": {"x": 10, "y": 20, "a": 0}}),
                        qos=0)
                await _wait_for(lambda: got, 3)
            finally:
                listen.cancel()
            assert got[0][0] == BOT_KEY and got[0][1] == "onPos"
            assert reg.bot_get(DID)["company"] == "eco-ng"
        finally:
            await broker.stop()

    async def test_comando_p2p_llega_al_robot_y_responde(self):
        """Roundtrip completo con filtros '+' y did con guiones:

        send_command publica iot/p2p/{cmd}/bridge/bumper/bridge/{did}/…/q/…
        → el robot (filtro 12 niveles con '+') lo recibe y responde p/…
        → el transport correlaciona por reqid.
        """
        reg = DeviceRegistry()
        broker = EmbeddedMqttBroker(("127.0.0.1", free_port()), reg)
        await broker.start()
        try:
            t = MqttTransport("127.0.0.1", broker.port, ca_file=None)
            await t.connect()
            listen = asyncio.create_task(t.listen())
            try:
                async with aiomqtt.Client(
                        hostname="127.0.0.1", port=broker.port,
                        identifier=BOT_KEY, username=f"SN-{DID}") as robot:
                    await robot.subscribe(
                        f"iot/p2p/+/+/+/+/{DID}/{CLS}/{RES}/q/+/+", qos=0)

                    async def responder() -> None:
                        async for msg in robot.messages:
                            p = str(msg.topic).split("/")
                            if len(p) == 12 and p[9] == "q":
                                reply = (f"iot/p2p/{p[2]}/{DID}/{CLS}/{RES}/"
                                         f"{p[3]}/{p[4]}/{p[5]}/p/{p[10]}/j")
                                await robot.publish(
                                    reply, json.dumps({"ret": "ok", "value": 87}),
                                    qos=0)

                    task = asyncio.create_task(responder())
                    try:
                        resp = await t.send_command(
                            DeviceId.from_key(BOT_KEY), "getBattery", {}, timeout=5)
                    finally:
                        task.cancel()
                    assert resp == {"ret": "ok", "value": 87}
            finally:
                listen.cancel()
        finally:
            await broker.stop()

    async def test_plus_es_un_nivel_exacto(self):
        """'+' NO matchea varios niveles (MQTT-4.7.1) — con hbmqtt sí lo
        hacía (clase regex con '/') y por eso 'funcionaban' filtros rotos."""
        broker = EmbeddedMqttBroker(("127.0.0.1", free_port()), DeviceRegistry())
        await broker.start()
        try:
            async with aiomqtt.Client(hostname="127.0.0.1", port=broker.port,
                                      identifier="sub@bumper/test") as sub, \
                    aiomqtt.Client(hostname="127.0.0.1", port=broker.port,
                                   identifier="pub@bumper/test") as pub:
                await sub.subscribe("iot/atr/+/fin", qos=0)
                await asyncio.sleep(0.1)
                await pub.publish("iot/atr/a/b/fin", b"{}", qos=0)  # 2 niveles
                await pub.publish("iot/atr/a/fin", b"{}", qos=0)     # 1 nivel
                msgs = [str(m.topic) async for m in _take(sub.messages, 1, 2)]
                assert msgs == ["iot/atr/a/fin"]
        finally:
            await broker.stop()

    async def test_username_sin_password_conecta(self):
        """hbmqtt rechazaba username sin password a nivel de protocolo
        (CONNACK 0x4) — amqtt no; el bot conecta igual."""
        reg = DeviceRegistry()
        broker = EmbeddedMqttBroker(("127.0.0.1", free_port()), reg)
        await broker.start()
        try:
            async with aiomqtt.Client(hostname="127.0.0.1", port=broker.port,
                                      identifier=BOT_KEY,
                                      username=f"SN-{DID}") as robot:
                await robot.publish(f"iot/atr/onPos/{DID}/{CLS}/{RES}/j",
                                    b"{}", qos=0)
            assert reg.bot_get(DID)["sn"] == f"SN-{DID}"
        finally:
            await broker.stop()


# ---------- listener TLS ----------

def _generate_certs(tmp_path: Path) -> tuple[Path, Path, Path]:
    """CA + cert de servidor autofirmados (cryptography, solo tests)."""
    cryptography = pytest.importorskip("cryptography")
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    def name(cn: str) -> x509.Name:
        return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])

    now = datetime.now(timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_cert = (x509.CertificateBuilder().subject_name(name("bridge-test-ca"))
               .issuer_name(name("bridge-test-ca"))
               .public_key(ca_key.public_key())
               .serial_number(x509.random_serial_number())
               .not_valid_before(now - timedelta(days=1))
               .not_valid_after(now + timedelta(days=1))
               .add_extension(x509.BasicConstraints(ca=True, path_length=None),
                              critical=True)
               .add_extension(x509.SubjectKeyIdentifier.from_public_key(
                   ca_key.public_key()), critical=False).sign(ca_key, hashes.SHA256()))
    srv_key = ec.generate_private_key(ec.SECP256R1())
    srv_cert = (x509.CertificateBuilder().subject_name(name("localhost"))
                .issuer_name(ca_cert.subject)
                .public_key(srv_key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(days=1))
                .not_valid_after(now + timedelta(days=1))
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(
                    ca_key.public_key()), critical=False).sign(ca_key, hashes.SHA256()))

    ca, cert, key = tmp_path / "ca.crt", tmp_path / "srv.crt", tmp_path / "srv.key"
    ca.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    cert.write_bytes(srv_cert.public_bytes(serialization.Encoding.PEM))
    key.write_bytes(srv_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()))
    return ca, cert, key


class TestTLSListener:
    async def test_pubsub_sobre_tls_con_certs(self, tmp_path):
        ca, cert, key = _generate_certs(tmp_path)
        broker = EmbeddedMqttBroker(("127.0.0.1", free_port()), DeviceRegistry(),
                                    server_cert=str(cert), server_key=str(key))
        await broker.start()
        try:
            ctx = ssl.create_default_context(cafile=str(ca))
            ctx.check_hostname = False
            if hasattr(ssl, "VERIFY_X509_STRICT"):
                ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
            async with aiomqtt.Client(hostname="127.0.0.1", port=broker.port,
                                      identifier="sub@bumper/test",
                                      tls_context=ctx) as sub:
                await sub.subscribe("iot/#", qos=0)
                async with aiomqtt.Client(hostname="127.0.0.1", port=broker.port,
                                          identifier=BOT_KEY,
                                          username=f"SN-{DID}",
                                          tls_context=ctx) as bot:
                    await bot.publish(f"iot/atr/onPos/{DID}/{CLS}/{RES}/j",
                                      b'{"x":1}', qos=0)
                msgs = [str(m.topic) async for m in _take(sub.messages, 1, 3)]
                assert msgs == [f"iot/atr/onPos/{DID}/{CLS}/{RES}/j"]
        finally:
            await broker.stop()


# ---------- wiring de serve ----------

class TestServeWiringFC:
    async def test_servers_mqtt_arranca_broker_y_cliente_a_loopback(self, tmp_path):
        ca, cert, key = _generate_certs(tmp_path)
        cfg = Config()
        cfg.servers.listen = "127.0.0.1"
        cfg.servers.mqtt = True
        cfg.servers.mqtt_port = free_port()
        cfg.servers.ca_file = str(ca)
        cfg.servers.server_cert = str(cert)
        cfg.servers.server_key = str(key)
        cfg.bumper_mqtt.enabled = True
        cfg.bumper_mqtt.host = "bumper-externo"
        cfg.bumper_mqtt.port = 8883
        svc = Service(cfg, None)
        await svc._start_embedded_servers()
        try:
            assert "mqtt" in svc.embedded
            assert svc.registry is not None
            # el cliente pasa a NUESTRO broker (mismo patrón que FB/xmpp)
            assert cfg.bumper_mqtt.host == "127.0.0.1"
            assert cfg.bumper_mqtt.port == cfg.servers.mqtt_port
            assert cfg.bumper_mqtt.ca_file == cfg.servers.ca_file
            # el broker está vivo: conecta un bot por TLS
            ctx = ssl.create_default_context(cafile=str(ca))
            ctx.check_hostname = False
            if hasattr(ssl, "VERIFY_X509_STRICT"):
                ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
            async with aiomqtt.Client(hostname="127.0.0.1",
                                      port=cfg.servers.mqtt_port,
                                      identifier=BOT_KEY, username=f"SN-{DID}",
                                      tls_context=ctx):
                assert svc.registry.bot_get(DID)["company"] == "eco-ng"
        finally:
            if "mqtt" in svc.embedded:
                await svc.embedded["mqtt"].stop()

    async def test_servers_mqtt_off_cliente_intacto(self):
        """servers.mqtt=false = todo como hoy: nada embebido, el cliente
        sigue apuntando al broker externo de la config."""
        cfg = Config()
        cfg.bumper_mqtt.enabled = True
        cfg.bumper_mqtt.host = "10.0.0.9"
        svc = Service(cfg, None)
        await svc._start_embedded_servers()
        assert "mqtt" not in svc.embedded
        assert cfg.bumper_mqtt.host == "10.0.0.9"

    async def test_broker_sin_cliente_por_config_no_rompe(self, tmp_path):
        """Broker embebido pero [bumper_mqtt] off: arranca igual (solo avisa),
        no toca la config del cliente."""
        _ca, cert, key = _generate_certs(tmp_path)
        cfg = Config()
        cfg.servers.listen = "127.0.0.1"
        cfg.servers.mqtt = True
        cfg.servers.mqtt_port = free_port()
        cfg.servers.server_cert = str(cert)
        cfg.servers.server_key = str(key)
        svc = Service(cfg, None)
        await svc._start_embedded_servers()
        try:
            assert "mqtt" in svc.embedded
            assert cfg.bumper_mqtt.host == "localhost"  # intacto
        finally:
            await svc.embedded["mqtt"].stop()

    async def test_certs_ausentes_no_tumban_el_serve(self):
        """Sin certs (o puerto ocupado) el broker no arranca, pero serve
        sigue vivo — mismo criterio que el XMPP embebido (FA/FB)."""
        cfg = Config()
        cfg.servers.listen = "127.0.0.1"
        cfg.servers.mqtt = True
        svc = Service(cfg, None)
        await svc._start_embedded_servers()  # certs por defecto: no existen
        assert "mqtt" not in svc.embedded  # no arrancó…


# ---------- helpers ----------

async def _wait_for(pred, timeout: float) -> None:
    """Espera (fail si vence) a que pred() devuelva algo truthy."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail("condición no alcanzada en el timeout")
        await asyncio.sleep(0.02)


async def _take(aiter, n: int, timeout: float):
    """Generador async: hasta n items del iterator con timeout GLOBAL."""
    loop = asyncio.get_running_loop()
    it = aiter.__aiter__()
    deadline = loop.time() + timeout
    while n > 0:
        restante = deadline - loop.time()
        if restante <= 0:
            return
        try:
            item = await asyncio.wait_for(it.__anext__(), restante)
        except (StopAsyncIteration, asyncio.TimeoutError):
            return
        yield item
        n -= 1
