"""Publicador MQTT con discovery nativo de Home Assistant (integracion mqtt core).

Por robot (slug):
  homeassistant/vacuum/<slug>/config        → discovery vacuum
  homeassistant/camera/<slug>/config        → discovery camara del mapa
  <base>/<slug>/command                     → comandos (start/pause/dock/locate)
  <base>/<slug>/state                       → estado HA (docked/cleaning/...)
  <base>/<slug>/attributes                  → atributos (bateria, consumibles, pos)
  <base>/<slug>/map                         → PNG base64 (topic de la camara)
"""

from __future__ import annotations

import json
import logging

import aiomqtt

log = logging.getLogger(__name__)

HA_VACUUM_COMMANDS = {
    "start": "start",
    "pause": "pause",
    "stop": "pause",
    "return_to_base": "dock",
    "dock": "dock",
    "locate": "locate",
    "clean_spot": "spot",  # pendiente de vocabulario exacto (barrido app)
    "fan_speed": "fan_speed",
}


class MqttPublisher:
    def __init__(self, host: str, port: int = 1883, user: str = "", password: str = "",
                 base_topic: str = "deebot"):
        self.host, self.port = host, port
        self.user, self.password = user, password
        self.base = base_topic
        self.client: aiomqtt.Client | None = None
        self.on_command = None  # async cb(slug, accion, valor)

    async def connect(self) -> None:
        kwargs = {"hostname": self.host, "port": self.port}
        if self.user:
            kwargs["username"] = self.user
        if self.password:
            kwargs["password"] = self.password
        self.client = aiomqtt.Client(**kwargs)
        await self.client.__aenter__()
        await self.client.subscribe(f"{self.base}/+/command")
        log.info("MQTT conectado a %s:%s", self.host, self.port)

    async def listen_commands(self) -> None:
        """Bucle de comandos entrantes → self.on_command(slug, accion)."""
        if not self.client:
            return
        async for msg in self.client.messages:
            topic = str(msg.topic)
            parts = topic.split("/")
            if len(parts) >= 3 and parts[-1] == "command":
                slug = parts[-2]
                action = msg.payload.decode()
                if self.on_command:
                    await self.on_command(slug, action, None)

    # ---------- discovery ----------

    async def publish_discovery(self, slug: str, name: str = "Deebot") -> None:
        assert self.client
        device = {
            "identifiers": [slug],
            "name": name,
            "manufacturer": "Ecovacs (cloud-free)",
            "model": "OZMO 930 via deebot930-bridge",
        }
        # schema 'state' de HA: claves payload_turn_on/off/etc (NO payload_start)
        vac = {
            "schema": "state",
            "name": name,
            "unique_id": slug,
            "device": device,
            "command_topic": f"{self.base}/{slug}/command",
            "state_topic": f"{self.base}/{slug}/state",
            "json_attributes_topic": f"{self.base}/{slug}/attributes",
            "payload_turn_on": "start",
            "payload_turn_off": "pause",
            "payload_pause": "pause",
            "payload_stop": "pause",
            "payload_return_to_base": "dock",
            "payload_locate": "locate",
            "payload_clean_spot": "spot",
        }
        await self.client.publish(
            f"homeassistant/vacuum/{slug}/config", json.dumps(vac), retain=True
        )
        cam = {
            "name": f"{name} map",
            "unique_id": f"{slug}_map",
            "device": device,
            "topic": f"{self.base}/{slug}/map",
            "content_type": "image/png",
        }
        await self.client.publish(
            f"homeassistant/camera/{slug}_map/config", json.dumps(cam), retain=True
        )
        log.info("discovery publicado para %s", slug)

    # ---------- estado ----------

    async def publish_state(self, slug: str, status: str) -> None:
        if self.client:
            await self.client.publish(f"{self.base}/{slug}/state", status, retain=True)

    async def publish_attributes(self, slug: str, attrs: dict) -> None:
        if self.client:
            await self.client.publish(
                f"{self.base}/{slug}/attributes", json.dumps(attrs), retain=True
            )

    async def publish_map(self, slug: str, png: bytes) -> None:
        if self.client:
            # el MQTT camera de HA espera los bytes crudos de la imagen
            await self.client.publish(
                f"{self.base}/{slug}/map", png, retain=True, qos=0
            )
