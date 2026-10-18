"""Embedded MQTT broker for eco-ng robots (T8+) — absorbs Bumper's mqttserver.py (FC).

Replaces Bumper's hbmqtt with ``amqtt`` (maintained fork, same role,
py3.12+): the broker lives inside the bridge process with a TLS listener
on :servers.mqtt_port and Bumper's authentication policy — robots
identify by their client-id ``{did}@{cls}/{res}`` and get registered in
the DeviceRegistry shared with the embedded XMPP.

Bumper's helperbot is NOT ported: MqttTransport already sends p2p
commands directly (docs/absorcion-bumper.md).

Two hbmqtt bugs disappear for free with amqtt:

- CONNECT with username but no password was rejected at protocol level
  (CONNACK 0x4) — hence bots connected with a dummy password
- ``Broker.matches()`` compiled '+' as the class ``[/\\$\\s\\w\\d]+`` (no
  '-') so eco-ng dids (dash-carrying UUIDs) never matched '+' filters.
  amqtt matches per level (MQTT-4.7.1): '+' = exactly one level,
  '#' = the rest — dashes included.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from amqtt.broker import Broker
from amqtt.plugins.base import BaseAuthPlugin
from amqtt.session import Session

from .registry import DeviceRegistry

log = logging.getLogger(__name__)


class BridgeAuthPlugin(BaseAuthPlugin):
    """Authentication faithful to Bumper's model (mqttserver.py::authenticate).

    - eco-ng bot: client-id ``{did}@{cls}/{res}`` with a realm WITHOUT
      "ecouser"/"bumper" → ``registry.bot_add(sn, did, cls, res, "eco-ng")``
      (the SN is the CONNECT username) and accepted.
    - client: realm with "ecouser"/"bumper" (e.g. ``bridge@bumper/bridge``)
      → accepted without accounts (``use_auth=False``, passwordless LAN)
      and registered as a client.
    - helperbot (Bumper's internal client) → accepted, not registered.
    - client-id without "@" → rejected (Bumper ran with allow-anonymous=False).

    The DeviceRegistry hangs off the broker (``EmbeddedMqttBroker.registry``):
    the PluginManager COPIES the context when loading each plugin, so the
    plugin does not see attributes added to the original context afterwards —
    but the copy keeps the reference to the broker, which is stable.
    """

    async def authenticate(self, *, session: Session) -> bool:
        registry: DeviceRegistry | None = getattr(
            getattr(self.context, "_broker_instance", None), "registry", None)
        client_id = str(session.client_id or "")
        if "@" not in client_id:
            log.warning("MQTT auth rejected (client-id without '@'): %r", client_id)
            return False

        did, _, realm = client_id.partition("@")
        if client_id.count("@") != 1:
            # Bumper fidelity: split('@') with 2+ @s truncated the realm
            # and rejected; explicit here
            log.warning("MQTT auth rejected (client-id with several '@'): %r",
                        client_id)
            return False
        if "/" not in realm:
            # like Bumper ({cls}/{res} or {realm}/{res} without '/' →
            # IndexError → unauthenticated): the realm always carries a resource
            log.warning("MQTT auth rejected (realm without '/'): %r", client_id)
            return False

        if "ecouser" in realm or "bumper" in realm:
            # Bumper-family client: userid@{realm}/{resource}
            realm_name, _, resource = realm.partition("/")
            if did == "helperbot":
                return True  # like Bumper: accepted without registering
            if registry is None or not registry.use_auth or \
                    registry.check_authcode(did, session.password or ""):
                if registry is not None:
                    registry.client_add(did, realm_name, resource)
                log.info("MQTT client authenticated: %s@%s/%s",
                         did, realm_name, resource)
                return True
            return False

        # eco-ng bot: did@{cls}/{res} — realm outside ecouser/bumper
        devclass, _, resource = realm.partition("/")
        sn = session.username or did  # the SN travels in the CONNECT username
        if registry is not None:
            registry.bot_add(sn, did, devclass, resource, "eco-ng")
            registry.bot_set_mqtt(did, True)
        log.info("eco-ng bot authenticated: did=%s sn=%s class=%s/%s",
                 did, sn, devclass, resource)
        return True


class EmbeddedMqttBroker(Broker):  # type: ignore[misc]
    """MQTT broker :8883 — the role of Bumper's hbmqtt, inside the bridge.

    - TLS listener with ServersConfig certs (plain TCP if none passed,
      tests only)
    - auth delegated to BridgeAuthPlugin (amqtt ``plugins`` config)
    - no $SYS plugin (system topics) nor topic-check, like Bumper
    - QoS0/retained/ping covered by amqtt; this system only uses QoS0
    """

    def __init__(self, address: tuple[str, int], registry: DeviceRegistry,
                 server_cert: str | None = None, server_key: str | None = None) -> None:
        # before super(): plugins load DURING Broker.__init__ and read the
        # broker's registry (the context copy points here)
        self.address = address
        self.registry = registry
        # half-configured certs = error, NOT a silent downgrade to plain
        if bool(server_cert) != bool(server_key):
            raise ValueError(
                f"incomplete MQTT certs: server_cert={server_cert!r} "
                f"server_key={server_key!r} — both or neither are required")
        listener: dict[str, Any] = {
            "type": "tcp",
            "bind": f"{address[0]}:{address[1]}",
        }
        if server_cert and server_key:
            listener.update(ssl=True, certfile=server_cert, keyfile=server_key)
        elif address[0] not in ("127.0.0.1", "localhost"):
            # plain TCP only for local tests — never silent on a LAN
            log.warning("embedded MQTT broker WITHOUT TLS listening on %s "
                        "(not 127.0.0.1) — acceptable only in tests",
                        address[0])
        config: dict[str, Any] = {
            "listeners": {"default": listener},
            "timeout_disconnect_delay": 0,
            "plugins": {f"{__name__}.BridgeAuthPlugin": {}},
        }
        super().__init__(config)

    @property
    def port(self) -> int:
        return self.address[1]

    async def start(self) -> None:
        tls = " (TLS)" if self.config.listeners["default"].ssl else ""
        await super().start()
        self._presence_task = asyncio.get_running_loop().create_task(
            self._presence_loop())
        log.info("embedded MQTT broker on %s:%d%s", *self.address, tls)

    async def _presence_loop(self) -> None:
        """Syncs the registry's bot.mqtt presence with live sessions
        (amqtt exposes no disconnect hook to plugins; _sessions does)."""
        while True:
            await asyncio.sleep(15)
            try:
                live = set(self._sessions)
                for did, bot in list(self.registry.bots.items()):
                    online = (f"{did}@{bot.device_class}/{bot.resource}" in live)
                    if bot.mqtt != online:
                        self.registry.bot_set_mqtt(did, online)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("MQTT presence sync failed")

    async def stop(self) -> None:
        log.info("embedded MQTT broker: stopping")
        if getattr(self, "_presence_task", None):
            self._presence_task.cancel()
        await self.shutdown()
        # nobody connected after shutdown
        for did, bot in list(self.registry.bots.items()):
            if bot.mqtt:
                self.registry.bot_set_mqtt(did, False)
