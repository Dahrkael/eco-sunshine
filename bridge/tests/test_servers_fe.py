"""Phase FE tests: cert auto-generation + announce_ip from the environment.

ensure_certs covers the clean install (generate CA+cert when everything
is missing) without EVER touching what exists (production robots
already trust Bumper's legacy CA). These tests pin that contract:

- generates the full trio (+ the CA key) when nothing exists
- existing certs are never touched (nor half-written, nor with a missing CA)
- missing pair + existing CA → signs with THAT CA (re-issue without re-pairing)
- unusable destination → False (serve disables the embedded TLS servers, does not die)
- the generated certs work for TLS with STRICT verification (py≥3.13)
"""

from __future__ import annotations

import socket
import ssl

import aiomqtt
import pytest
from cryptography import x509

from eco_sunshine.config import Config, load_config
from eco_sunshine.servers.certs import ensure_certs
from eco_sunshine.servers.mqtt import EmbeddedMqttBroker
from eco_sunshine.servers.registry import DeviceRegistry
from eco_sunshine.serve import Service

DID = "b11fceaf-1234-5678-9abc-def012345678"
BOT_KEY = f"{DID}@yna5xh/axm"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _strict_ctx(ca_file: str) -> ssl.SSLContext:
    """Demanding TLS client: explicit CA, hostname off (we connect by IP),
    VERIFY_X509_STRICT on (the mode that rejected CAs without AKI)."""
    ctx = ssl.create_default_context(cafile=ca_file)
    ctx.check_hostname = False
    if hasattr(ssl, "VERIFY_X509_STRICT"):
        ctx.verify_flags |= ssl.VERIFY_X509_STRICT
    return ctx


# ---------- generation ----------

class TestEnsureCerts:
    def test_clean_install_generates_trio_and_ca_key(self, tmp_path):
        ca, crt, key = tmp_path / "ca.crt", tmp_path / "srv.crt", tmp_path / "srv.key"
        assert ensure_certs(str(ca), str(crt), str(key), cn="bumper") is True
        for p in (ca, crt, key, tmp_path / "ca.key"):
            assert p.is_file(), f"missing {p}"
        # permission contract: private keys 0600, certs readable
        import stat

        for secret in (key, tmp_path / "ca.key"):
            mode = stat.S_IMODE(secret.stat().st_mode)
            assert mode == 0o600, f"{secret} with permissions {oct(mode)}"
        ca_cert = x509.load_pem_x509_certificate(ca.read_bytes())
        srv_cert = x509.load_pem_x509_certificate(crt.read_bytes())
        # chain: the leaf is signed by the generated CA
        assert srv_cert.issuer == ca_cert.subject
        assert ca_cert.subject == ca_cert.issuer  # self-signed
        bc = ca_cert.extensions.get_extension_for_class(x509.BasicConstraints)
        assert bc.value.ca is True and bc.critical
        # AKI on the leaf (what py≥3.13's strict ssl requires and
        # Bumper's original generator never set)
        aki = srv_cert.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier)
        assert aki.value.key_identifier is not None

    def test_ca_without_its_key_signs_nothing(self, tmp_path):
        ca, crt, key = tmp_path / "ca.crt", tmp_path / "srv.crt", tmp_path / "srv.key"
        ca.write_bytes(b"garbage-ca")  # CA present, ca.key missing
        assert ensure_certs(str(ca), str(crt), str(key)) is False
        assert not crt.exists() and not key.exists()

    def test_existing_untouched(self, tmp_path):
        ca, crt, key = tmp_path / "ca.crt", tmp_path / "srv.crt", tmp_path / "srv.key"
        for p, payload in ((ca, b"OLD-CA"), (crt, b"OLD-CRT"),
                           (key, b"OLD-KEY")):
            p.write_bytes(payload)
        assert ensure_certs(str(ca), str(crt), str(key)) is True
        assert ca.read_bytes() == b"OLD-CA"
        assert crt.read_bytes() == b"OLD-CRT"
        assert key.read_bytes() == b"OLD-KEY"

    def test_half_pair_generates_nothing(self, tmp_path):
        ca, crt, key = tmp_path / "ca.crt", tmp_path / "srv.crt", tmp_path / "srv.key"
        crt.write_bytes(b"orphan")
        assert ensure_certs(str(ca), str(crt), str(key)) is False
        assert not ca.exists() and not key.exists()

    def test_missing_ca_with_pair_present_generates_no_ca(self, tmp_path):
        """A new CA would not sign the existing cert: just warn."""
        ca, crt, key = tmp_path / "ca.crt", tmp_path / "srv.crt", tmp_path / "srv.key"
        crt.write_bytes(b"crt"), key.write_bytes(b"key")
        assert ensure_certs(str(ca), str(crt), str(key)) is True
        assert not ca.exists()

    def test_existing_ca_signs_the_new_pair(self, tmp_path):
        """bumper.crt deleted + intact CA → re-issue signed by THAT CA
        (robots already trusting it notice nothing)."""
        ca, crt, key = tmp_path / "ca.crt", tmp_path / "srv.crt", tmp_path / "srv.key"
        assert ensure_certs(str(ca), str(crt), str(key))
        first = x509.load_pem_x509_certificate(crt.read_bytes())
        crt.unlink(), key.unlink()
        assert ensure_certs(str(ca), str(crt), str(key)) is True
        second = x509.load_pem_x509_certificate(crt.read_bytes())
        ca_cert = x509.load_pem_x509_certificate(ca.read_bytes())
        assert second.issuer == ca_cert.subject
        assert second.serial_number != first.serial_number

    def test_ip_cn_ends_up_as_ip_san(self, tmp_path):
        ca, crt, key = tmp_path / "ca.crt", tmp_path / "srv.crt", tmp_path / "srv.key"
        import ipaddress

        assert ensure_certs(str(ca), str(crt), str(key), cn="192.168.1.50")
        san = (x509.load_pem_x509_certificate(crt.read_bytes())
               .extensions.get_extension_for_class(x509.SubjectAlternativeName))
        assert ipaddress.ip_address("192.168.1.50") in san.value.get_values_for_type(
            x509.IPAddress)

    def test_unusable_destination_returns_false_without_side_effects(self, tmp_path):
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory")
        ca = tmp_path / "d" / "ca.crt"  # VALID destination: must never get created
        assert ensure_certs(str(ca), str(blocker / "srv.crt"),
                            str(blocker / "srv.key")) is False
        assert not ca.exists() and list(tmp_path.glob("**/*.crt")) == []


# ---------- the generated certs work for TLS ----------

class TestGeneratedCertsWorkForTLS:
    async def test_pubsub_tls_with_strict_verification(self, tmp_path):
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


# ---------- DEEBOT_ANNOUNCE_IP (environment fallback) ----------

class TestAnnounceIpEnvironment:
    """The toml reads no env: the deploy injects announce_ip through a
    variable (compose: DEEBOT_ANNOUNCE_IP ← ${BUMPER_ANNOUNCE_IP} from
    .env)."""

    def test_env_fills_empty_announce_ip(self, tmp_path, monkeypatch):
        p = tmp_path / "c.toml"
        p.write_text("[servers]\nlookup = true\n")
        monkeypatch.setenv("DEEBOT_ANNOUNCE_IP", "192.0.2.10")
        cfg = load_config(str(p))
        assert cfg.servers.announce_ip == "192.0.2.10"
        assert cfg.servers.lookup is True  # the rest of the toml intact

    def test_explicit_toml_wins_over_environment(self, tmp_path, monkeypatch):
        p = tmp_path / "c.toml"
        p.write_text('[servers]\nannounce_ip = "10.0.0.7"\n')
        monkeypatch.setenv("DEEBOT_ANNOUNCE_IP", "192.0.2.10")
        assert load_config(str(p)).servers.announce_ip == "10.0.0.7"

    def test_no_env_stays_empty(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DEEBOT_ANNOUNCE_IP", raising=False)
        p = tmp_path / "c.toml"
        p.write_text("[servers]\n")
        assert load_config(str(p)).servers.announce_ip == ""

    def test_blank_env_ignored(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DEEBOT_ANNOUNCE_IP", "   ")
        p = tmp_path / "c.toml"
        p.write_text("[servers]\n")
        assert load_config(str(p)).servers.announce_ip == ""


# ---------- serve wiring (FE) ----------

class TestServeWiringFE:
    async def test_missing_certs_generated_and_broker_starts(self, tmp_path):
        """Clean install: serve generates the certs and starts the embedded
        broker — the [bumper_mqtt] client moves to loopback as in FC."""
        cfg = Config()
        cfg.servers.listen = "127.0.0.1"
        cfg.servers.mqtt = True
        cfg.servers.mqtt_port = free_port()
        cfg.servers.ca_file = str(tmp_path / "ca.crt")
        cfg.servers.server_cert = str(tmp_path / "srv.crt")
        cfg.servers.server_key = str(tmp_path / "srv.key")
        cfg.bumper_mqtt.enabled = True
        cfg.bumper_mqtt.host = "external-bumper"
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

    async def test_unrecoverable_certs_disable_embedded_tls(self, tmp_path):
        """Unusable destination → no embedded XMPP/MQTT and the client
        untouched (serve does not die, stays compatible with an external
        bumper)."""
        blocker = tmp_path / "blocker"
        blocker.write_text("x")
        cfg = Config()
        cfg.servers.listen = "127.0.0.1"
        cfg.servers.mqtt = True
        cfg.servers.server_cert = str(blocker / "no.crt")
        cfg.servers.server_key = str(blocker / "no.key")
        cfg.servers.mqtt_port = free_port()
        cfg.bumper_mqtt.enabled = True
        cfg.bumper_mqtt.host = "external-bumper"
        svc = Service(cfg, None)
        await svc._start_embedded_servers()
        assert "mqtt" not in svc.embedded
        assert cfg.bumper_mqtt.host == "external-bumper"
        assert cfg.bumper_mqtt.tls is None  # untouched
