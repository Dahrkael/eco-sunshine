"""Broker MQTT embebido para robots eco-ng (T8+) — absorbe mqttserver.py de Bumper (FC).

Sustituye al hbmqtt de Bumper por ``amqtt`` (fork mantenido, misma función,
py3.12+): el broker vive en el proceso del bridge con listener TLS en
:servers.mqtt_port y la política de autenticación de Bumper — los robots se
identifican por su client-id ``{did}@{cls}/{res}`` y quedan registrados en el
DeviceRegistry compartido con el XMPP embebido.

Del servidor de Bumper NO se porta el helperbot: MqttTransport ya manda los
comandos p2p directamente (docs/absorcion-bumper.md).

Dos bugs de hbmqtt desaparecen gratis con amqtt:

- CONNECT con username sin password era rechazado a nivel de protocolo
  (CONNACK 0x4) — por eso los bots conectaban con password dummy
- ``Broker.matches()`` compilaba '+' como la clase ``[/\\$\\s\\w\\d]+`` (sin
  '-') y los dids eco-ng (UUIDs con guión) no matcheaban filtros con '+'.
  amqtt hace el matching por niveles (MQTT-4.7.1): '+' = un nivel exacto,
  '#' = el resto — guiones incluidos.
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
    """Autenticación fiel al modelo de Bumper (mqttserver.py::authenticate).

    - bot eco-ng: client-id ``{did}@{cls}/{res}`` con realm SIN
      "ecouser"/"bumper" → ``registry.bot_add(sn, did, cls, res, "eco-ng")``
      (el SN es el username del CONNECT) y acepta.
    - client: realm con "ecouser"/"bumper" (p.ej. ``bridge@bumper/bridge``)
      → acepta sin cuentas (``use_auth=False``, LAN sin passwords) y queda
      registrado como client.
    - helperbot (el cliente interno de Bumper) → acepta sin registrar.
    - client-id sin "@" → rechaza (Bumper arrancaba con allow-anonymous=False).

    El DeviceRegistry llega colgado del broker (``EmbeddedMqttBroker.registry``):
    el PluginManager COPIA el contexto al cargar cada plugin, así que el
    plugin no ve atributos añadidos al contexto original después — pero la
    copia mantiene la referencia al broker, que sí es estable.
    """

    async def authenticate(self, *, session: Session) -> bool:
        registry: DeviceRegistry | None = getattr(
            getattr(self.context, "_broker_instance", None), "registry", None)
        client_id = str(session.client_id or "")
        if "@" not in client_id:
            log.warning("MQTT auth rechazada (client-id sin '@'): %r", client_id)
            return False

        did, _, realm = client_id.partition("@")
        if client_id.count("@") != 1:
            # fidelidad Bumper: split('@') con 2+ arrobas truncaba el realm
            # y rechazaba; aquí explícito
            log.warning("MQTT auth rechazada (client-id con varias '@'): %r",
                        client_id)
            return False
        if "/" not in realm:
            # como Bumper ({cls}/{res} o {realm}/{res} sin '/' → IndexError
            # → no autenticado): el realm siempre trae resource
            log.warning("MQTT auth rechazada (realm sin '/'): %r", client_id)
            return False

        if "ecouser" in realm or "bumper" in realm:
            # client de la familia Bumper: userid@{realm}/{resource}
            realm_name, _, resource = realm.partition("/")
            if did == "helperbot":
                return True  # como Bumper: acepta sin registrar
            if registry is None or not registry.use_auth or \
                    registry.check_authcode(did, session.password or ""):
                if registry is not None:
                    registry.client_add(did, realm_name, resource)
                log.info("client MQTT autenticado: %s@%s/%s",
                         did, realm_name, resource)
                return True
            return False

        # bot eco-ng: did@{cls}/{res} — realm ajeno a ecouser/bumper
        devclass, _, resource = realm.partition("/")
        sn = session.username or did  # el SN viaja en el username del CONNECT
        if registry is not None:
            registry.bot_add(sn, did, devclass, resource, "eco-ng")
            registry.bot_set_mqtt(did, True)
        log.info("bot eco-ng autenticado: did=%s sn=%s clase=%s/%s",
                 did, sn, devclass, resource)
        return True


class EmbeddedMqttBroker(Broker):  # type: ignore[misc]
    """Broker MQTT :8883 — el papel del hbmqtt de Bumper, dentro del bridge.

    - listener TLS con los certs de ServersConfig (TCP plano si no se pasan,
      solo para tests)
    - auth delegada a BridgeAuthPlugin (config ``plugins`` de amqtt)
    - sin plugin $SYS (topics de sistema) ni topic-check, como Bumper
    - QoS0/retained/ping los cubre amqtt; este sistema solo usa QoS0
    """

    def __init__(self, address: tuple[str, int], registry: DeviceRegistry,
                 server_cert: str | None = None, server_key: str | None = None) -> None:
        # antes de super(): los plugins se cargan DURANTE Broker.__init__ y
        # leen el registro del broker (la copia del contexto apunta aquí)
        self.address = address
        self.registry = registry
        # certs a medio configurar = error, NO downgrade silencioso a plano
        if bool(server_cert) != bool(server_key):
            raise ValueError(
                f"certs MQTT incompletos: server_cert={server_cert!r} "
                f"server_key={server_key!r} — se necesitan ambos o ninguno")
        listener: dict[str, Any] = {
            "type": "tcp",
            "bind": f"{address[0]}:{address[1]}",
        }
        if server_cert and server_key:
            listener.update(ssl=True, certfile=server_cert, keyfile=server_key)
        elif address[0] not in ("127.0.0.1", "localhost"):
            # TCP plano solo para tests locales — nunca silencioso en LAN
            log.warning("broker MQTT embebido SIN TLS escuchando en %s "
                        "(no 127.0.0.1) — solo aceptable en pruebas",
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
        log.info("broker MQTT embebido en %s:%d%s", *self.address, tls)

    async def _presence_loop(self) -> None:
        """Sincroniza mqtt_connection del registro con las sesiones vivas
        (amqtt no expone hook de desconexión a plugins; _sessions sí)."""
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
                log.exception("sync de presencia MQTT falló")

    async def stop(self) -> None:
        log.info("broker MQTT embebido: parando")
        if getattr(self, "_presence_task", None):
            self._presence_task.cancel()
        await self.shutdown()
        # nadie conectado tras el apagado
        for did, bot in list(self.registry.bots.items()):
            if bot.mqtt:
                self.registry.bot_set_mqtt(did, False)
