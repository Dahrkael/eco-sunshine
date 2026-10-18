"""MQTT publisher with native Home Assistant discovery (mqtt core integration).

Each robot = ONE HA device with all its entities grouped:
  vacuum.<slug>            (state, commands, battery, fan_speed)
  camera.<slug>_map        (map PNG)
  sensor.<slug>_*          (4 consumables, error, firmware, stats x3)
  button.<slug>_reset_*    (consumable resets)
  button.<slug>_room_*     (clean a single room — published when rooms are
                            learned, see publish_room_buttons)
  switch.<slug>_dnd        (do not disturb, optimistic)
  switch.<slug>_silent     (silent mode, optimistic)
  select.<slug>_water      (water flow level, optimistic)

Topics:
  <base>/<slug>/state            consolidated JSON (retained)
  <base>/<slug>/battery          int % (retained)
  <base>/<slug>/fan_speed        current speed (retained)
  <base>/<slug>/consumable/<k>   remaining % per consumable (retained)
  <base>/<slug>/stats/<k>        total_time/total_area/total_count (retained)
  <base>/<slug>/fw               firmware version (retained)
  <base>/<slug>/command          vacuum commands (start/pause/stop/dock/locate/spot)
  <base>/<slug>/room             payload = room mssid(s) "id1,id2"
  <base>/<slug>/fan_speed/set    set fan speed
  <base>/<slug>/water/set        water level (low/medium/high)
  <base>/<slug>/reset_life_span  payload = component (Brush/SideBrush/...)
  <base>/<slug>/dnd/set          ON/OFF
  <base>/<slug>/silent/set       ON/OFF
"""

from __future__ import annotations

import asyncio
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
        # (O4) a single call with all subscriptions
        subs = [(f"{self.base}/+/command", 0),
                (f"{self.base}/+/fan_speed/set", 0),
                (f"{self.base}/+/water/set", 0),
                (f"{self.base}/+/room", 0),
                (f"{self.base}/+/reset_life_span", 0),
                (f"{self.base}/+/dnd/set", 0),
                (f"{self.base}/+/silent/set", 0)]
        await self.client.subscribe(subs)
        log.info("MQTT connected to %s:%s", self.host, self.port)

    async def _pub(self, topic: str, payload, retain: bool = True) -> None:
        if self.client:
            if not isinstance(payload, (bytes, bytearray)):
                payload = str(payload)
            await self.client.publish(topic, payload, retain=retain, qos=0)

    # ---------- discovery ----------

    async def publish_discovery(self, slug: str, name: str = "Deebot",
                                model_name: str = "Deebot",
                                fan_speeds: list[str] | None = None,
                                has_water: bool = False) -> None:
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
            "fan_speed_list": fan_speeds or ["silent", "standard", "strong"],
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

        # consumable sensors (remaining % per type)
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

        # error sensor
        err = {
            "name": f"{name} error",
            "unique_id": f"{slug}_error",
            "device": dev,
            "state_topic": f"{b}/error",
            "icon": "mdi:alert-circle",
            "entity_category": "diagnostic",
        }
        await self._pub(f"homeassistant/sensor/{slug}_error/config", json.dumps(err))

        # consumable reset buttons
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

        # DND / silent switches (optimistic)
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

        # firmware sensor
        fw = {
            "name": f"{name} firmware",
            "unique_id": f"{slug}_fw",
            "device": dev,
            "state_topic": f"{b}/fw",
            "icon": "mdi:chip",
            "entity_category": "diagnostic",
        }
        await self._pub(f"homeassistant/sensor/{slug}_fw/config", json.dumps(fw))

        # stats sensors (totals)
        stats = [
            ("total_time", "cleaning time", "h", "mdi:timer-outline",
             "{{ (value | float / 3600) | round(1) }}"),
            ("total_area", "cleaned area", "m\u00b2", "mdi:texture-box",
             "{{ (value | float / 1000000) | int }}"),
            ("total_count", "clean count", None, "mdi:counter",
             "{{ value | int }}"),
        ]
        for key, label, unit, icon, tpl in stats:
            st = {
                "name": f"{name} {label}",
                "unique_id": f"{slug}_{key}",
                "device": dev,
                "state_topic": f"{b}/stats/{key}",
                "value_template": tpl,
                "state_class": "total_increasing",
                "icon": icon,
                "entity_category": "diagnostic",
            }
            if unit:
                st["unit_of_measurement"] = unit
            await self._pub(f"homeassistant/sensor/{slug}_{key}/config", json.dumps(st))

        # water flow select (optimistic — mopping robots)
        if has_water:
            sel = {
                "name": f"{name} water flow",
                "unique_id": f"{slug}_water",
                "device": dev,
                "command_topic": f"{b}/water/set",
                "options": ["low", "medium", "high"],
                "optimistic": True,
                "icon": "mdi:water",
                "entity_category": "config",
            }
            await self._pub(f"homeassistant/select/{slug}_water/config",
                            json.dumps(sel))

        log.info("discovery published for %s (vacuum+camera+8 sensors+4 buttons"
                 "+2 switches%s)", slug, "+water select" if has_water else "")

    # ---------- state ----------

    async def publish_state_doc(self, slug: str, doc: dict) -> None:
        await self._pub(f"{self.base}/{slug}/state", json.dumps(doc))

    async def publish_state(self, slug: str, status: str) -> None:
        """Optimistic state after a command (the full doc overwrites it in <2s)."""
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

    async def publish_fw(self, slug: str, fw: str | None) -> None:
        if fw:
            await self._pub(f"{self.base}/{slug}/fw", fw)

    async def publish_stats(self, slug: str, stats: dict) -> None:
        """Totals (raw seconds / mm² / count; templates convert for HA)."""
        for key in ("total_time", "total_area", "total_count"):
            v = stats.get(key)
            if v is not None:
                await self._pub(f"{self.base}/{slug}/stats/{key}", v)

    async def publish_room_buttons(self, slug: str, name: str, model_name: str,
                                   rooms: list[dict]) -> None:
        """One clean button per mapped room (re)published when rooms change.

        Discovery messages are idempotent — HA re-reads them fine on every
        publish; rooms that disappear just leave a stale button until the
        next restart (acceptable for a rarely-changing set).
        """
        if not self.client or not rooms:
            return
        dev = self._device(slug, name, model_name)
        b = f"{self.base}/{slug}"
        for room in rooms:
            mssid = str(room.get("mssid", ""))
            label = str(room.get("name") or mssid)
            if not mssid:
                continue
            btn = {
                "name": f"{name} room {label}",
                "unique_id": f"{slug}_room_{mssid}",
                "device": dev,
                "command_topic": f"{b}/room",
                "payload_press": mssid,
                "icon": "mdi:door",
            }
            await self._pub(
                f"homeassistant/button/{slug}_room_{mssid}/config", json.dumps(btn))
        log.info("room buttons published for %s (%d)", slug, len(rooms))

    async def publish_switch_state(self, slug: str, key: str, on: bool) -> None:
        await self._pub(f"{self.base}/{slug}/{key}/state", "ON" if on else "OFF")

    async def publish_attributes(self, slug: str, attrs: dict) -> None:
        await self._pub(f"{self.base}/{slug}/attributes", json.dumps(attrs))

    async def publish_map(self, slug: str, png: bytes) -> None:
        # HA's MQTT camera expects the raw image bytes
        if self.client:
            await self.client.publish(f"{self.base}/{slug}/map", png,
                                      retain=True, qos=0)

    async def publish_map_data(self, slug: str, data: dict | None) -> None:
        """Live map as JSON for custom cards (canvas-rendered).

        Same shape the web panel sends over WebSocket: rows of cell chars +
        crop/origin/cell_mm transform. Retained — a card connecting late
        renders immediately."""
        if data and self.client:
            await self._pub(f"{self.base}/{slug}/map_data", json.dumps(data))

    # ---------- command loop ----------

    async def listen_commands(self) -> None:
        """Command loop with reconnection (B16): if mosquitto drops us the
        serve must not die — retries like MqttTransport.listen."""
        delay = 3.0
        while True:
            try:
                if self.client is None:
                    await self.connect()
                assert self.client is not None
                async for msg in self.client.messages:
                    try:
                        await self._dispatch(msg)
                    except Exception:  # noqa: BLE001
                        log.exception("error dispatching MQTT command (message ignored)")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("HA MQTT disconnected (%s) — retrying in %.0fs", e, delay)
                try:
                    if self.client is not None:
                        await self.client.__aexit__(type(e), e, None)
                except Exception:  # noqa: BLE001
                    pass
                self.client = None
                await asyncio.sleep(delay)
                delay = min(delay * 1.5, 30)
            else:
                delay = 3.0

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
        elif last == "set" and parts[-2] == "water":
            await self.on_command(slug, "set_water_level", payload)
        elif last == "room":
            await self.on_command(slug, "clean_room", payload)
        elif last == "reset_life_span":
            await self.on_command(slug, "reset_life_span", payload)
        elif last == "set" and parts[-2] == "dnd":
            await self.on_command(slug, "dnd", payload)
        elif last == "set" and parts[-2] == "silent":
            await self.on_command(slug, "silent", payload)
