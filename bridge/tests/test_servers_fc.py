"""Phase FC tests: embedded MQTT broker — Bumper-style auth + routing.

The broker is amqtt (per-level filter matching, without hbmqtt's '+'
bug), so these tests pin the contract the system relies on:

- auth: bots {did}@{cls}/{res} → DeviceRegistry; bumper/ecouser clients
- '+' wildcard = exactly ONE level, WITH dashes (eco-ng UUID dids)
- standard pub/sub routing between MqttTransport (iot/#) and a mock robot
- TLS listener with ServersConfig certs
- serve wiring: servers.mqtt=true → [bumper_mqtt] client to loopback
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

from ecovacs_bridge.config import Config
from ecovacs_bridge.models import DeviceId
from ecovacs_bridge.mqtt_transport import MqttTransport
from ecovacs_bridge.servers.mqtt import BridgeAuthPlugin, EmbeddedMqttBroker
from ecovacs_bridge.servers.registry import DeviceRegistry
from ecovacs_bridge.serve import Service

# real eco-ng did: dash-carrying UUID — the case hbmqtt's '+' broke
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
    """Context like the one amqtt gives: copy with _broker_instance → broker."""
    broker = SimpleNamespace(registry=registry)
    ctx = BaseContext()
    ctx._broker_instance = broker  # noqa: SLF001 (amqtt's copy points at the broker)
    return BridgeAuthPlugin(ctx)


# ---------- auth (plugin, unit) ----------

class TestAuthPlugin:
    async def test_eco_ng_bot_registers(self):
        reg = DeviceRegistry()
        ok = await make_plugin(reg).authenticate(
            session=sess(BOT_KEY, username=f"SN-{DID}", password="mock"))
        assert ok is True
        bot = reg.bot_get(DID)
        assert bot is not None
        assert bot["company"] == "eco-ng"
        assert bot["sn"] == f"SN-{DID}"  # the SN travels in the username
        assert bot["device_class"] == CLS and bot["resource"] == RES

    async def test_bot_without_username_uses_did_as_sn(self):
        reg = DeviceRegistry()
        assert await make_plugin(reg).authenticate(session=sess(BOT_KEY)) is True
        assert reg.bot_get(DID)["sn"] == DID

    async def test_bumper_client_accepted_and_registered(self):
        """bridge@bumper/bridge — the client-id of the bridge's MqttTransport."""
        reg = DeviceRegistry()
        assert await make_plugin(reg).authenticate(session=sess("bridge@bumper/bridge")) is True
        cl = reg.client_get("bridge")
        assert cl == {"user_id": "bridge", "realm": "bumper",
                      "resource": "bridge", "xmpp": False}

    async def test_ecouser_client_accepted_without_accounts(self):
        """use_auth=False (default): passwordless LAN, like Bumper."""
        reg = DeviceRegistry()
        ok = await make_plugin(reg).authenticate(session=sess("fuid_x@ecouser/ios"))
        assert ok is True
        assert reg.client_get("ios")["user_id"] == "fuid_x"

    async def test_client_use_auth_true_without_authcode_rejects(self):
        reg = DeviceRegistry(use_auth=True)
        ok = await make_plugin(reg).authenticate(session=sess("app@bumper/ios"))
        assert ok is False
        assert reg.client_get("ios") is None

    async def test_helperbot_accepted_without_registering(self):
        reg = DeviceRegistry()
        ok = await make_plugin(reg).authenticate(session=sess("helperbot@bumper/helperbot"))
        assert ok is True
        assert reg.client_get("helperbot") is None

    @pytest.mark.parametrize("client_id", ["no-at", "a@b", ""])
    async def test_malformed_client_id_rejected(self, client_id):
        reg = DeviceRegistry()
        assert await make_plugin(reg).authenticate(session=sess(client_id)) is False
        assert not reg.bots and not reg.clients


# ---------- live broker (plain TCP) ----------

class TestBrokerRouting:
    async def test_atr_report_reaches_bridge_and_bot_registered(self):
        """The robot publishes iot/atr/…, the MqttTransport (subscribed to
        iot/#) receives it — and the bot's CONNECT lands it in the
        DeviceRegistry."""
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

    async def test_p2p_command_reaches_robot_and_replies(self):
        """Full roundtrip with '+' filters and a dash-carrying did:

        send_command publishes iot/p2p/{cmd}/bridge/bumper/bridge/{did}/…/q/…
        → the robot (12-level filter with '+') gets it and replies p/…
        → the transport correlates by reqid.
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

    async def test_plus_is_exactly_one_level(self):
        """'+' does NOT match several levels (MQTT-4.7.1) — hbmqtt did
        (regex class with '/'), which is why broken filters 'worked'."""
        broker = EmbeddedMqttBroker(("127.0.0.1", free_port()), DeviceRegistry())
        await broker.start()
        try:
            async with aiomqtt.Client(hostname="127.0.0.1", port=broker.port,
                                      identifier="sub@bumper/test") as sub, \
                    aiomqtt.Client(hostname="127.0.0.1", port=broker.port,
                                   identifier="pub@bumper/test") as pub:
                await sub.subscribe("iot/atr/+/fin", qos=0)
                await asyncio.sleep(0.1)
                await pub.publish("iot/atr/a/b/fin", b"{}", qos=0)  # 2 levels
                await pub.publish("iot/atr/a/fin", b"{}", qos=0)     # 1 level
                msgs = [str(m.topic) async for m in _take(sub.messages, 1, 2)]
                assert msgs == ["iot/atr/a/fin"]
        finally:
            await broker.stop()

    async def test_username_without_password_connects(self):
        """hbmqtt rejected username without password at protocol level
        (CONNACK 0x4) — amqtt does not; the bot connects anyway."""
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
    """Self-signed CA + server cert (cryptography, tests only)."""
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
    async def test_pubsub_over_tls_with_certs(self, tmp_path):
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


# ---------- serve wiring ----------

class TestServeWiringFC:
    async def test_servers_mqtt_starts_broker_and_client_to_loopback(self, tmp_path):
        ca, cert, key = _generate_certs(tmp_path)
        cfg = Config()
        cfg.servers.listen = "127.0.0.1"
        cfg.servers.mqtt = True
        cfg.servers.mqtt_port = free_port()
        cfg.servers.ca_file = str(ca)
        cfg.servers.server_cert = str(cert)
        cfg.servers.server_key = str(key)
        cfg.bumper_mqtt.enabled = True
        cfg.bumper_mqtt.host = "external-bumper"
        cfg.bumper_mqtt.port = 8883
        svc = Service(cfg, None)
        await svc._start_embedded_servers()
        try:
            assert "mqtt" in svc.embedded
            assert svc.registry is not None
            # the client switches to OUR broker (same pattern as FB/xmpp)
            assert cfg.bumper_mqtt.host == "127.0.0.1"
            assert cfg.bumper_mqtt.port == cfg.servers.mqtt_port
            assert cfg.bumper_mqtt.ca_file == cfg.servers.ca_file
            # the broker is alive: connect a bot over TLS
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

    async def test_servers_mqtt_off_client_untouched(self):
        """servers.mqtt=false = everything as today: nothing embedded,
        the client keeps pointing at the config's external broker."""
        cfg = Config()
        cfg.bumper_mqtt.enabled = True
        cfg.bumper_mqtt.host = "10.0.0.9"
        svc = Service(cfg, None)
        await svc._start_embedded_servers()
        assert "mqtt" not in svc.embedded
        assert cfg.bumper_mqtt.host == "10.0.0.9"

    async def test_broker_without_client_by_config_does_not_break(self, tmp_path):
        """Embedded broker but [bumper_mqtt] off: it still starts (only
        warns) and does not touch the client config."""
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
            assert cfg.bumper_mqtt.host == "localhost"  # untouched
        finally:
            await svc.embedded["mqtt"].stop()

    async def test_unrecoverable_certs_do_not_kill_serve(self, tmp_path):
        """Unrecoverable certs (unusable destination) → no embedded broker,
        but serve stays alive — same criterion as the embedded XMPP (FA/FB).

        (FE changed the original "missing certs" premise: if they are
        simply MISSING they now self-generate — that case is covered by
        test_servers_fe.py. Here the cert directory is blocked by a
        regular file.)
        """
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory")
        cfg = Config()
        cfg.servers.listen = "127.0.0.1"
        cfg.servers.mqtt = True
        # deterministic: impossible destination in tmp and a free port
        # (not 8883, which the environment's docker bumper may have taken)
        cfg.servers.server_cert = str(blocker / "no.crt")
        cfg.servers.server_key = str(blocker / "no.key")
        cfg.servers.mqtt_port = _free_port()
        svc = Service(cfg, None)
        await svc._start_embedded_servers()
        assert "mqtt" not in svc.embedded  # did not start…
        # inverse promise: with [bumper_mqtt] enabled, the client config
        # is NOT touched when the broker fails (stays on the external one)
        cfg2 = Config()
        cfg2.servers.listen = "127.0.0.1"
        cfg2.servers.mqtt = True
        cfg2.servers.server_cert = str(blocker / "no2.crt")
        cfg2.servers.server_key = str(blocker / "no2.key")
        cfg2.servers.mqtt_port = _free_port()
        cfg2.bumper_mqtt.enabled = True
        cfg2.bumper_mqtt.host = "external-bumper"
        cfg2.bumper_mqtt.port = 8883
        svc2 = Service(cfg2, None)
        await svc2._start_embedded_servers()
        assert "mqtt" not in svc2.embedded
        assert cfg2.bumper_mqtt.host == "external-bumper"  # untouched
        assert cfg2.bumper_mqtt.tls is None  # untouched


# ---------- helpers ----------

def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _wait_for(pred, timeout: float) -> None:
    """Waits (fails on timeout) for pred() to return truthy."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail("condition not reached within the timeout")
        await asyncio.sleep(0.02)


async def _take(aiter, n: int, timeout: float):
    """Async generator: up to n items from the iterator with a GLOBAL timeout."""
    loop = asyncio.get_running_loop()
    it = aiter.__aiter__()
    deadline = loop.time() + timeout
    while n > 0:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return
        try:
            item = await asyncio.wait_for(it.__anext__(), remaining)
        except (StopAsyncIteration, asyncio.TimeoutError):
            return
        yield item
        n -= 1
