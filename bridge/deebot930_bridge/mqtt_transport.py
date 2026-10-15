"""Transporte MQTT hacia el broker de Bumper (robots eco-ng / T8+).

Topics (spec docs/mqtt-json-spec.md §2):
  comandos:   iot/p2p/{cmd}/bridge/bumper/bridge/{sn}/{cls}/{res}/q/{reqid}/j
  respuesta:  iot/p2p/{cmd}/{sn}/{cls}/{res}/bridge/bumper/bridge/p/{reqid}/j
  reports:    iot/atr/{name}/{sn}/{cls}/{res}/j

El broker de Bumper (hbmqtt :8883, TLS con CA propia) acepta nuestro
client-id `bridge@bumper/bridge` sin más auth (su plugin autentica a
cualquier cliente con "bumper" en el realm — bumper/mqttserver.py:266).

Este módulo es deliberadamente independiente de serve.py: es la costura
("seam") para la futura absorción de Bumper — hoy es un cliente de red,
mañana puede sustituirse por un adaptador in-process enganchado al broker
embebido sin tocar ni serve.py ni MqttCommander (docs/absorcion-bumper.md).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import ssl
import time
import uuid
from typing import Any, Awaitable, Callable

import aiomqtt

from .models import DeviceId

log = logging.getLogger(__name__)

PROTOCOL_VER = "0.0.22"
TZM_MINUTES = 60  # España UTC+1

# callback(robot_key, name, data) — reports atr Y respuestas p2p (misma forma)
DataHandler = Callable[[str, str, dict], Awaitable[None]]

RECONNECT_DELAY = 3.0
RESPONSE_TIMEOUT = 10.0


def build_envelope(params: dict | list | None) -> dict:
    """Envelope de comando (spec §3.1): header + body.data + bdTaskID."""
    ts = str(int(time.time() * 1000))
    data: Any = dict(params) if isinstance(params, dict) else params
    if isinstance(data, dict):
        data["bdTaskID"] = f"{ts}{random.randint(100, 999)}"
    return {
        "header": {"pri": 2, "ts": ts, "tzm": TZM_MINUTES, "ver": PROTOCOL_VER},
        "body": {"data": data if data is not None else {}},
    }


def route_topic(topic: str) -> tuple[str, str, str, str] | None:
    """Clasifica un topic MQTT del broker.

    Devuelve (kind, robot_key, name, request_id) o None si no interesa.
    kind: "report" (iot/atr) | "response" (p2p type=p) | "request" (p2p q)
    robot_key: "{sn}@{cls}/{res}" — la clave canónica de DeviceId.
    """
    parts = topic.split("/")
    if len(parts) < 6 or parts[0] != "iot":
        return None
    if parts[1] == "atr":
        # iot/atr/{name}/{sn}/{cls}/{res}/{fmt}
        return ("report", f"{parts[3]}@{parts[4]}/{parts[5]}", parts[2], "")
    if parts[1] == "p2p" and len(parts) == 12:
        cmd, fid, fcls, fres, _tid, _tcls, _tres, typ, rand, _fmt = parts[2:]
        if typ == "p":
            # respuesta: el emisor (from) es el robot
            return ("response", f"{fid}@{fcls}/{fres}", cmd, rand)
        if typ == "q":
            # comando dirigido a {to} (p.ej. si alguien más manda comandos)
            return ("request", f"{parts[6]}@{parts[7]}/{parts[8]}", cmd, rand)
    return None


def command_topic(did: DeviceId, command: str, request_id: str) -> str:
    return (
        f"iot/p2p/{command}/bridge/bumper/bridge/"
        f"{did.sn}/{did.device_class}/{did.resource}/q/{request_id}/j"
    )


def _tls_context(ca_file: str | None) -> ssl.SSLContext:
    """Misma política que el cliente XMPP: CA propia, sin check de hostname,
    sin VERIFY_X509_STRICT (el CA generado por Bumper no lleva AKI)."""
    if ca_file:
        ctx = ssl.create_default_context(cafile=ca_file)
        ctx.check_hostname = False
        if hasattr(ssl, "VERIFY_X509_STRICT"):
            ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
        return ctx
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class MqttTransport:
    """Cliente MQTT del broker de Bumper para robots eco-ng.

    Uso:
        t = MqttTransport(host, port, ca_file, on_data=handler)
        await t.connect()
        asyncio.create_task(t.listen())          # bucle con reconexión
        resp = await t.send_command(did, "getBattery", {})
    """

    def __init__(
        self,
        host: str,
        port: int = 8883,
        ca_file: str | None = None,
        on_data: DataHandler | None = None,
        identifier: str = "bridge@bumper/bridge",
    ) -> None:
        self.host, self.port, self.ca_file = host, port, ca_file
        self.on_data = on_data
        self.identifier = identifier
        self.client: aiomqtt.Client | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self.connected = asyncio.Event()

    async def connect(self) -> None:
        kwargs: dict[str, Any] = {
            "hostname": self.host,
            "port": self.port,
            "identifier": self.identifier,
        }
        if self.port == 8883:
            kwargs["tls_context"] = _tls_context(self.ca_file)
        self.client = aiomqtt.Client(**kwargs)
        await self.client.__aenter__()
        # reports de TODOS los robots + respuestas a NUESTROS comandos
        # (qos 0, como el helperbot de Bumper)
        await self.client.subscribe("iot/atr/+/+/+/+/+", qos=0)
        await self.client.subscribe("iot/p2p/+/+/+/+/bridge/bumper/bridge/+/+/+", qos=0)
        self.connected.set()
        log.info("MQTT bumper conectado: %s:%d como %s", self.host, self.port, self.identifier)

    async def listen(self) -> None:
        """Bucle de mensajes con reconexión exponencial-máx."""
        delay = RECONNECT_DELAY
        while True:
            try:
                if self.client is None:
                    await self.connect()
                assert self.client is not None
                async for msg in self.client.messages:
                    try:
                        await self._on_message(msg)
                    except Exception:  # noqa: BLE001
                        log.exception("error procesando mensaje MQTT")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.connected.clear()
                log.warning("MQTT bumper desconectado (%s) — reintento en %.0fs", e, delay)
                try:
                    if self.client is not None:
                        await self.client.__aexit__(type(e), e, None)
                except Exception:  # noqa: BLE001
                    pass
                self.client = None
                await asyncio.sleep(delay)
                delay = min(delay * 1.5, 30)
            else:
                delay = RECONNECT_DELAY

    async def _on_message(self, msg: Any) -> None:
        routed = route_topic(str(msg.topic))
        if routed is None:
            return
        kind, key, name, reqid = routed
        try:
            payload = json.loads(msg.payload)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            log.debug("payload no-JSON en %s", msg.topic)
            return
        if not isinstance(payload, dict):
            return

        if kind == "response" and reqid:
            fut = self._pending.get(reqid)
            if fut is not None and not fut.done():
                fut.set_result(payload)

        if self.on_data is not None:
            try:
                await self.on_data(key, name, payload)
            except Exception:  # noqa: BLE001
                log.exception("on_data falló para %s/%s", name, key)

    # ---------- comandos ----------

    async def send_command(
        self,
        did: DeviceId,
        command: str,
        params: dict | list | None = None,
        timeout: float = RESPONSE_TIMEOUT,
    ) -> dict:
        """Publica un comando y espera su respuesta p2p (correlación por rand)."""
        if self.client is None:
            return {"ret": "fail", "errno": 503, "debug": "transporte no conectado"}
        request_id = uuid.uuid4().hex[:16]
        topic = command_topic(did, command, request_id)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = fut
        try:
            await self.client.publish(topic, json.dumps(build_envelope(params)), qos=0)
            log.debug(">> %s %s", command, params or "")
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            log.debug("timeout esperando respuesta de %s (%s)", command, did.key)
            return {"ret": "fail", "errno": 500, "debug": "timeout respuesta p2p"}
        finally:
            self._pending.pop(request_id, None)

    def fire(self, did: DeviceId, command: str, params: dict | list | None = None) -> None:
        """Comando sin bloqueo (estilo XMPP commander): la respuesta llegará
        por on_data y actualizará el estado igual que un report."""

        async def _run() -> None:
            try:
                await self.send_command(did, command, params)
            except Exception:  # noqa: BLE001
                log.exception("fire %s falló", command)

        try:
            asyncio.get_running_loop().create_task(_run())
        except RuntimeError:
            log.error("fire fuera de event loop: %s", command)
