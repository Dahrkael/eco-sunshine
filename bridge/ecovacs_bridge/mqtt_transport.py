"""MQTT transport to Bumper's broker (eco-ng / T8+ robots).

Topics (spec docs/mqtt-json-spec.md §2):
  commands:   iot/p2p/{cmd}/bridge/bumper/bridge/{sn}/{cls}/{res}/q/{reqid}/j
  responses:  iot/p2p/{cmd}/{sn}/{cls}/{res}/bridge/bumper/bridge/p/{reqid}/j
  reports:    iot/atr/{name}/{sn}/{cls}/{res}/j

Bumper's broker (hbmqtt :8883, TLS with its own CA) accepts our client-id
`bridge@bumper/bridge` with no further auth (its plugin authenticates any
client with "bumper" in the realm — bumper/mqttserver.py:266).

This module is deliberately independent of serve.py: it is the seam for
the future absorption of Bumper — today a network client, tomorrow it
can be swapped for an in-process adapter hooked to the embedded broker
without touching serve.py or MqttCommander (docs/absorcion-bumper.md).
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
TZM_MINUTES = 60  # Spain, UTC+1

# callback(robot_key, name, data) — atr reports AND p2p responses (same shape)
DataHandler = Callable[[str, str, dict], Awaitable[None]]

RECONNECT_DELAY = 3.0
RESPONSE_TIMEOUT = 10.0


def build_envelope(params: dict | list | None) -> dict:
    """Command envelope (spec §3.1): header + body.data + bdTaskID."""
    ts = str(int(time.time() * 1000))
    data: Any = dict(params) if isinstance(params, dict) else params
    if isinstance(data, dict):
        data["bdTaskID"] = f"{ts}{random.randint(100, 999)}"
    return {
        "header": {"pri": 2, "ts": ts, "tzm": TZM_MINUTES, "ver": PROTOCOL_VER},
        "body": {"data": data if data is not None else {}},
    }


def route_topic(topic: str) -> tuple[str, str, str, str] | None:
    """Classify an MQTT topic from the broker.

    Returns (kind, robot_key, name, request_id) or None if uninteresting.
    kind: "report" (iot/atr) | "response" (p2p type=p) | "request" (p2p q)
    robot_key: "{sn}@{cls}/{res}" — DeviceId's canonical key.
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
            # response: the sender (from) is the robot
            return ("response", f"{fid}@{fcls}/{fres}", cmd, rand)
        if typ == "q":
            # command addressed to {to} (e.g. someone else sending commands)
            return ("request", f"{parts[6]}@{parts[7]}/{parts[8]}", cmd, rand)
    return None


def command_topic(did: DeviceId, command: str, request_id: str) -> str:
    return (
        f"iot/p2p/{command}/bridge/bumper/bridge/"
        f"{did.sn}/{did.device_class}/{did.resource}/q/{request_id}/j"
    )


def _tls_context(ca_file: str | None) -> ssl.SSLContext:
    """Same policy as the XMPP client: own CA, no hostname check, no
    VERIFY_X509_STRICT (Bumper's generated CA carries no AKI)."""
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
    """MQTT client of Bumper's broker for eco-ng robots.

    Usage:
        t = MqttTransport(host, port, ca_file, on_data=handler)
        await t.connect()
        asyncio.create_task(t.listen())          # loop with reconnection
        resp = await t.send_command(did, "getBattery", {})
    """

    def __init__(
        self,
        host: str,
        port: int = 8883,
        ca_file: str | None = None,
        on_data: DataHandler | None = None,
        identifier: str = "bridge@bumper/bridge",
        tls: bool | None = None,
    ) -> None:
        self.host, self.port, self.ca_file = host, port, ca_file
        self.on_data = on_data
        self.identifier = identifier
        # None = legacy heuristic (TLS on 8883) for existing configs;
        # serve pins it explicitly when it starts the embedded broker
        # (the port may not be 8883 during the Bumper migration)
        self.tls = tls
        self.client: aiomqtt.Client | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._fire_tasks: set[asyncio.Task] = set()  # anti-GC refs (S7)

    async def connect(self) -> None:
        kwargs: dict[str, Any] = {
            "hostname": self.host,
            "port": self.port,
            "identifier": self.identifier,
        }
        use_tls = self.tls if self.tls is not None else self.port == 8883
        if use_tls:
            kwargs["tls_context"] = _tls_context(self.ca_file)
        self.client = aiomqtt.Client(**kwargs)
        await self.client.__aenter__()
        # Broad subscription + structural filtering in route_topic.
        #
        # NO '+' filters: Bumper's hbmqtt compiles '+' as the class
        # [/\$\s\w\d]+ (no '-'), so it never matches dids with a dash
        # (all eco-ng ones are UUID-like). '#' works fine and the exact
        # filtering happens here — robust against any broker.
        await self.client.subscribe("iot/#", qos=0)
        log.info("bumper MQTT connected: %s:%d as %s", self.host, self.port, self.identifier)

    async def listen(self) -> None:
        """Message loop with capped exponential reconnection."""
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
                        log.exception("error processing MQTT message")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("bumper MQTT disconnected (%s) — retrying in %.0fs", e, delay)
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
        # (O2) p2p requests seen on iot/# = commands in transit (ours or
        # another app's): not reports nor responses — processing them only
        # pollutes state with the envelope.
        if kind == "request":
            return
        try:
            payload = json.loads(msg.payload)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            log.debug("non-JSON payload on %s", msg.topic)
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
                log.exception("on_data failed for %s/%s", name, key)

    # ---------- commands ----------

    async def send_command(
        self,
        did: DeviceId,
        command: str,
        params: dict | list | None = None,
        timeout: float = RESPONSE_TIMEOUT,
    ) -> dict:
        """Publishes a command and awaits its p2p response (correlated by rand)."""
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
            log.debug("timeout waiting for response from %s (%s)", command, did.key)
            return {"ret": "fail", "errno": 500, "debug": "p2p response timeout"}
        finally:
            self._pending.pop(request_id, None)

    def fire(self, did: DeviceId, command: str, params: dict | list | None = None) -> None:
        """Non-blocking command (XMPP commander style): the response arrives
        via on_data and updates state like a report."""

        async def _run() -> None:
            try:
                await self.send_command(did, command, params)
            except Exception:  # noqa: BLE001
                log.exception("fire %s failed", command)

        try:
            task = asyncio.get_running_loop().create_task(_run())
            self._fire_tasks.add(task)  # (S7) anti-GC reference
            task.add_done_callback(self._fire_tasks.discard)
        except RuntimeError:
            log.error("fire outside event loop: %s", command)
