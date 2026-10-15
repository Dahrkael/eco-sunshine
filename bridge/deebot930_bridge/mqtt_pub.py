"""Publicador MQTT con discovery nativo de Home Assistant (integracion mqtt core).

Cada robot = UN dispositivo HA con todas sus entidades agrupadas:
  vacuum.<slug>          (estado, comandos, bateria, fan_speed)
  camera.<slug>_map      (PNG del mapa)
  sensor.<slug>_*        (consumibles x4, error, firmware, tiempo de limpieza)
  button.<slug>_reset_*  (reset de consumibles)
  switch.<slug>_dnd      (no molestar, optimista)
  switch.<slug>_silent   (modo silencio, optimista)

Topics:
  <base>/<slug>/state            JSON consolidado (retained)
  <base>/<slug>/battery          int % (retained)
  <base>/<slug>/fan_speed        velocidad actual (retained)
  <base>/<slug>/consumable/<k>   % restante por consumible (retained)
  <base>/<slug>/command          comandos del vacuum (start/pause/stop/dock/locate)
  <base>/<slug>/fan_speed/set    set fan speed
  <base>/<slug>/reset_life_span  payload = componente (Brush/SideBrush/...)
  <base>/<slug>/dnd/set          ON/OFF
  <base>/<slug>/silent/set       ON/OFF
"""

from __future__ import annotations

import json
import logging

import aiomqtt

log = logging.getLogger(__name__)

CONSUMABLES = {"brush": "Brush", "sidebrush": "SideBrush",
               "dustcase": "DustCaseHeap", "airfilter": "AirFilter"}


class MqttPublisher:
    def __init__(self, host: str, port: int = 1883, user: str = "", password: str = "",
                 base_topic: str = "deebot"):
        self.host, self.port = host, port
        self.user, self.password = user, password
        self.base = base_topic
        self.client: aiomqtt.Client | None = None
        self.on_command = None  # async cb(slug, action, value)

    def _device(self, slug: str, name: str, model_name: str = "Deebot") -> dict:
        return {
            "identifiers": [f"deebot-{slug}"],
            "name": name,
            "manufacturer": "Ecovacs (cloud-free)",
            "model": model_name,
        }

    async def connect(self) -> None:
        kwargs = {"hostname": self.host, "port": self.port}
        if self.user:
            kwargs["username"] = self.user
        if self.password:
            kwargs["password"] = self.password
        self.client = aiomqtt.Client(**kwargs)
        await self.client.__aenter__()
        for t in (f"{self.base}/+/command", f"{self.base}/+/fan_speed/set",
                  f"{self.base}/+/reset_life_span", f"{self.base}/+/dnd/set",
                  f"{self.base}/+/silent/set"):
            await self.client.subscribe(t)
        log.info("MQTT conectado a %s:%s", self.host, self.port)

    async def _pub(self, topic: str, payload, retain: bool = True) -> None:
        if self.client:
            if not isinstance(payload, (bytes, bytearray)):
                payload = str(payload)
            await self.client.publish(topic, payload, retain=retain, qos=0)

    # ---------- discovery ----------

    async def publish_discovery(self, slug: str, name: str = "Deebot", model_name: str = "Deebot") -> None:
        if not self.client:
            return
        dev = self._device(slug, name, model_name)
        b = f"{self.base}/{slug}"

        vac = {
            "schema": "state",
            "name": name,
            "unique_id": f"{slug}_vacuum",
            "device": dev,
            "command_topic": f"{b}/command",
            "state_topic": f"{b}/state",
            "state_value_template": "{{ value_json.status }}",
            "battery_level_topic": f"{b}/battery",
            "battery_level_template": "{{ value | int }}",
            "fan_speed_topic": f"{b}/fan_speed",
            "fan_speed_command_topic": f"{b}/fan_speed/set",
            "fan_speed_list": list(CLEAN_SPEEDS.keys()) if False else ["silent", "standard", "strong"],
            "json_attributes_topic": f"{b}/attributes",
            "payload_turn_on": "start",
            "payload_turn_off": "pause",
            "payload_pause": "pause",
            "payload_stop": "pause",
            "payload_return_to_base": "dock",
            "payload_locate": "locate",
            "payload_clean_spot": "spot",
        }
        await self._pub(f"homeassistant/vacuum/{slug}/config", json.dumps(vac))

        cam = {
            "name": f"{name} map",
            "unique_id": f"{slug}_map",
            "device": dev,
            "topic": f"{b}/map",
            "content_type": "image/png",
            "image_refresh_interval": 5,
        }
        await self._pub(f"homeassistant/camera/{slug}_map/config", json.dumps(cam))

        # sensores de consumibles (% restante por tipo)
        for key, comp in CONSUMABLES.items():
            sensor = {
                "name": f"{name} {key.replace('_', ' ')}",
                "unique_id": f"{slug}_{key}",
                "device": dev,
                "state_topic": f"{b}/consumable/{key}",
                "unit_of_measurement": "%",
                "state_class": "measurement",
                "icon": "mdi:brush" if key in ("brush", "sidebrush") else
                        "mdi:air-filter" if key == "airfilter" else "mdi:delete-variant",
                "entity_category": "diagnostic",
            }
            await self._pub(f"homeassistant/sensor/{slug}_{key}/config", json.dumps(sensor))

        # sensor de error
        err = {
            "name": f"{name} error",
            "unique_id": f"{slug}_error",
            "device": dev,
            "state_topic": f"{b}/error",
            "icon": "mdi:alert-circle",
            "entity_category": "diagnostic",
        }
        await self._pub(f"homeassistant/sensor/{slug}_error/config", json.dumps(err))

        # botones de reset de consumibles
        for key, comp in CONSUMABLES.items():
            btn = {
                "name": f"{name} reset {key.replace('_', ' ')}",
                "unique_id": f"{slug}_reset_{key}",
                "device": dev,
                "command_topic": f"{b}/reset_life_span",
                "payload_press": comp,
                "entity_category": "config",
                "icon": "mdi:restart",
            }
            await self._pub(f"homeassistant/button/{slug}_reset_{key}/config", json.dumps(btn))

        # switches DND / silent (optimisticos)
        for key, label, icon in (("dnd", "Do not disturb", "mdi:moon-waning-crescent"),
                                 ("silent", "Silent mode", "mdi:volume-off")):
            sw = {
                "name": f"{name} {label}",
                "unique_id": f"{slug}_{key}",
                "device": dev,
                "command_topic": f"{b}/{key}/set",
                "state_topic": f"{b}/{key}/state",
                "payload_on": "ON",
                "payload_off": "OFF",
                "state_on": "ON",
                "state_off": "OFF",
                "optimistic": True,
                "icon": icon,
            }
            await self._pub(f"homeassistant/switch/{slug}_{key}/config", json.dumps(sw))

        log.info("discovery publicado para %s (vacuum+camera+7 sensores+4 botones+2 switches)", slug)

    # ---------- estado ----------

    async def publish_state_doc(self, slug: str, doc: dict) -> None:
        await self._pub(f"{self.base}/{slug}/state", json.dumps(doc))

    async def publish_state(self, slug: str, status: str) -> None:
        """Estado optimista tras un comando (el doc completo lo sobreescribe en <2s)."""
        await self._pub(f"{self.base}/{slug}/state", json.dumps({"status": status}))

    async def publish_battery(self, slug: str, battery: int | None) -> None:
        if battery is not None:
            await self._pub(f"{self.base}/{slug}/battery", battery)

    async def publish_fan_speed(self, slug: str, speed: str) -> None:
        await self._pub(f"{self.base}/{slug}/fan_speed", speed)

    async def publish_consumable(self, slug: str, key: str, pct: int) -> None:
        await self._pub(f"{self.base}/{slug}/consumable/{key}", pct)

    async def publish_error(self, slug: str, error: str | None) -> None:
        await self._pub(f"{self.base}/{slug}/error", error or "none")

    async def publish_switch_state(self, slug: str, key: str, on: bool) -> None:
        await self._pub(f"{self.base}/{slug}/{key}/state", "ON" if on else "OFF")

    async def publish_attributes(self, slug: str, attrs: dict) -> None:
        await self._pub(f"{self.base}/{slug}/attributes", json.dumps(attrs))

    async def publish_map(self, slug: str, png: bytes) -> None:
        # el MQTT camera de HA espera los bytes crudos de la imagen
        if self.client:
            await self.client.publish(f"{self.base}/{slug}/map", png, retain=True, qos=0)

    # ---------- bucle de comandos ----------

    async def listen_commands(self) -> None:
        if not self.client:
            return
        async for msg in self.client.messages:
            try:
                await self._dispatch(msg)
            except Exception:  # noqa: BLE001
                log.exception("error despachando comando MQTT (mensaje ignorado)")

    async def _dispatch(self, msg) -> None:
        topic = str(msg.topic)
        parts = topic.split("/")
        if len(parts) < 3 or parts[0] != self.base:
            return
        last = parts[-1]
        slug = parts[-3] if last == "set" else parts[-2]
        payload = msg.payload.decode(errors="replace").strip()
        if not self.on_command:
            return
        if last == "command":
            await self.on_command(slug, payload, None)
        elif last == "set" and parts[-2] == "fan_speed":
            await self.on_command(slug, "fan_speed", payload)
        elif last == "reset_life_span":
            await self.on_command(slug, "reset_life_span", payload)
        elif last == "set" and parts[-2] == "dnd":
            await self.on_command(slug, "dnd", payload)
        elif last == "set" and parts[-2] == "silent":
            await self.on_command(slug, "silent", payload)
