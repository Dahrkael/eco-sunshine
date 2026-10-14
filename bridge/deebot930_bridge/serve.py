"""Modo servicio: XMPP (Bumper) ⇄ MQTT (Home Assistant), por robot.

- Login en Bumper como cliente (mismo uid que la app → recibe los pushes)
- Poll suave read-only cada POLL_SECONDS (no despierta al robot en base)
- MapP pushes → MapAssembler → PNG → topic de la cámara
- Comandos MQTT → vocabulario XMPP del robot
"""

from __future__ import annotations

import asyncio
import logging

from .config import Config
from .map_pieces import MapAssembler, render_png
from .mqtt_pub import MqttPublisher
from .robot import RobotCommander, RobotState
from .webpanel import WebPanel
from .xmpp_client import BumperXMPPClient

log = logging.getLogger(__name__)

POLL_SECONDS = 30
KNOWN_JID = "E000105701860607AB7@115.ecorobot.net/atom"

HA_STATUS = {
    "docked": "docked",
    "charging": "docked",
    "cleaning": "cleaning",
    "paused": "paused",
    "returning": "returning",
    "idle": "idle",
    "unknown": "idle",
    "error": "error",
}


class Service:
    def __init__(self, cfg: Config, jid_hint: str | None):
        self.cfg = cfg
        self.jid_hint = jid_hint or KNOWN_JID
        self.robots: dict[str, RobotState] = {}
        self.commanders: dict[str, RobotCommander] = {}
        self.asm = MapAssembler()
        self.fan_speeds: dict[str, str] = {}
        self.pub = MqttPublisher(
            cfg.mqtt.host, cfg.mqtt.port, cfg.mqtt.user, cfg.mqtt.password,
            cfg.mqtt.base_topic,
        )
        self.client: BumperXMPPClient | None = None
        self.panel = WebPanel(self, port=8080)
        self._last_publish: dict[str, float] = {}
        self._last_status: dict[str, str] = {}

    # ---------- eventos XMPP ----------

    def _adopt(self, jid: str) -> None:
        r = RobotState.from_jid(jid)
        self.robots[jid] = r
        self.commanders[jid] = RobotCommander(self.client, r)  # type: ignore[arg-type]
        log.info("robot adoptado: %s (%s)", jid, r.slug)

    async def on_event(self, jid: str, attrs: dict, inner: str) -> None:
        if "ecorobot.net" not in jid:
            return
        if jid not in self.robots:
            self._adopt(jid)
            await self.pub.publish_discovery(self.robots[jid].slug)
        r = self.robots[jid]
        r.update(attrs, inner)

        # base de carga: cuando esta cargando/en base, su posicion ES la de la base
        if r.status in ("charging", "docked") and r.charger is None and r.position:
            r.charger = r.position
            self.asm.version += 1  # fuerza re-render con el marcador de la base
            log.info("base de carga fijada en %s", (r.charger.x, r.charger.y))

        # spec de mapa (respuesta GetMapM: geometria + crcs por pieza)
        if attrs.get("m") and attrs.get("i") and attrs.get("w"):
            map_id = attrs["i"]
            before = self.asm.spec_changes
            self.asm.set_geometry(map_id, attrs)
            if self.asm.spec_changes > before:
                log.info(
                    "spec mapa %s: %d crcs — lanzando pull walk",
                    map_id, len(self.asm.crcs[map_id]),
                )
                asyncio.create_task(self._pull_walk(map_id))
            return

        # piezas de mapa (pid real; el assembler valida CRC contra la spec)
        p = attrs.get("p", "")
        if p.startswith("XQ") and attrs.get("i") and attrs.get("pid"):
            map_id, pid = attrs["i"], int(attrs["pid"])
            if self.asm.add_piece(map_id, pid, p):
                log.info("pieza mapa %s#%d aceptada", map_id, pid)

        await self._publish_state(jid)

    async def _publish_state(self, jid: str, force: bool = False) -> None:
        import time

        r = self.robots[jid]
        now = time.monotonic()
        changed = self._last_status.get(jid) != r.status
        if not force and not changed and now - self._last_publish.get(jid, 0) < 2:
            return
        self._last_publish[jid] = now
        self._last_status[jid] = r.status
        doc = {
            "status": HA_STATUS.get(r.status, "idle"),
            "status_raw": r.status,
            "battery": r.battery,
            "fan_speed": self.fan_speeds.get(r.slug, "standard"),
            "error": r.error,
            "fw": r.version,
            "clean_seconds": r.clean_seconds,
            "position": {"x": r.position.x, "y": r.position.y, "angle": r.position.angle}
            if r.position else None,
            "charger": {"x": r.charger.x, "y": r.charger.y, "angle": r.charger.angle}
            if r.charger else None,
            "consumables": r.lifespans,
        }
        await self.pub.publish_state_doc(r.slug, doc)
        await self.pub.publish_battery(r.slug, r.battery)
        await self.pub.publish_fan_speed(r.slug, doc["fan_speed"])
        await self.pub.publish_error(r.slug, r.error)
        for key, comp in {"brush": "Brush", "sidebrush": "SideBrush",
                          "dustcase": "DustCaseHeap", "airfilter": "AirFilter"}.items():
            life = r.lifespans.get(comp)
            if life and life.get("total"):
                await self.pub.publish_consumable(r.slug, key, round(life["left"] / life["total"] * 100))
        await self.pub.publish_attributes(r.slug, doc)
        await self.panel.broadcast(doc)
        self.panel._last_png = None  # invalidar cache del mapa

    # ---------- comandos MQTT ----------

    async def on_command(self, slug: str, action: str, val) -> None:
        for jid, r in self.robots.items():
            if r.slug != slug:
                continue
            c = self.commanders[jid]
            log.info("comando %s → %s %s", slug, action, val or "")
            if action == "start":
                speed = self.fan_speeds.get(slug, "standard")
                c.start_clean(speed=speed)
                await self.pub.publish_state(slug, "cleaning")
            elif action in ("pause", "stop"):
                (c.pause() if action == "pause" else c.stop())
            elif action == "dock":
                c.dock()
                await self.pub.publish_state(slug, "returning")
            elif action == "locate":
                c.play_sound(30)
            elif action == "fan_speed":
                self.fan_speeds[slug] = val
                await self.pub.publish_fan_speed(slug, val)
            elif action == "reset_life_span":
                c.reset_life_span(val or "Brush")
            elif action == "dnd":
                await self.pub.publish_switch_state(slug, "dnd", val == "ON")
                c.set_block_time_state(val == "ON")
            elif action == "silent":
                await self.pub.publish_switch_state(slug, "silent", val == "ON")
                c.set_silent_mode_state(val == "ON")
            return

    # ---------- poll read-only ----------

    async def poll_loop(self) -> None:
        while True:
            for jid, c in self.commanders.items():
                try:
                    c.set_map_autoreport(True)  # la app lo re-arma constantemente
                    c.get_pos()
                    c.get_clean_state()
                    c.get_charge_state()
                    c.get_charger_pos()
                    c.get_battery_info()
                    c.get_map_m()
                    c.get_life_span("Brush")
                    c.get_life_span("SideBrush")
                    c.get_life_span("DustCaseHeap")
                    c.get_life_span("AirFilter")
                    c.get_error()
                    c.get_version()
                except Exception:  # noqa: BLE001
                    log.exception("poll falló para %s", jid)
            await asyncio.sleep(POLL_SECONDS)

    # ---------- bucle principal ----------

    async def run(self) -> None:
        b = self.cfg.bumper
        self.client = BumperXMPPClient(
            host=b.host, port=b.port, uid=b.uid, resource=b.resource,
            ca_file=b.ca_file, on_event=self.on_event,
        )
        await self.client.connect()
        await self.pub.connect()
        self.pub.on_command = self.on_command

        # adopcion inicial del robot conocido + discovery inmediato
        if self.jid_hint and self.jid_hint not in self.robots:
            self._adopt(self.jid_hint)
            await self.pub.publish_discovery(self.robots[self.jid_hint].slug)
            c = self.commanders[self.jid_hint]
            c.set_map_autoreport(True)
            c.get_pos(); c.get_clean_state(); c.get_charge_state()

        await self.panel.start()
        log.info("serve operativo (poll cada %ds, panel en :8080)", POLL_SECONDS)
        await asyncio.gather(
            self.client.listen(),
            self.pub.listen_commands(),
            self.poll_loop(),
            self._map_publisher(),
        )

    async def _pull_walk(self, map_id: str) -> None:
        """Repone las piezas emitidas antes de arrancar: PullMP 0..63 (como la app)."""
        c = next((c for j, c in self.commanders.items()), None)
        if not c:
            return
        log.info("pull walk mapa %s", map_id)
        for pid in range(64):
            try:
                c.pull_map_piece(pid)
            except Exception:  # noqa: BLE001
                return
            await asyncio.sleep(0.25)

    async def _map_publisher(self) -> None:
        """Publica el mapa cuando cambie contenido; re-walk periódico de piezas."""
        import time

        last_version, last_pub, last_walk = -1, 0.0, 0.0
        walking = False
        while True:
            await asyncio.sleep(5)
            now = time.monotonic()

            # re-walk: reponer piezas cada 60s (la app hace lo mismo al abrir el mapa)
            if not walking and now - last_walk > 60 and self.asm.maps:
                walking = True
                last_walk = now
                map_id = max(self.asm.maps, key=lambda m: self.asm.piece_count(m))

                async def walk() -> None:
                    nonlocal walking, last_version
                    try:
                        await self._pull_walk(map_id)
                    finally:
                        walking = False
                        last_version = -1  # fuerza republicación tras cada walk

                asyncio.create_task(walk())

            # publicar si el contenido del mapa cambió (no solo si crece)
            total = sum(self.asm.piece_count(m) for m in self.asm.maps)
            if self.asm.version != last_version and now - last_pub > 10 and self.asm.maps:
                last_version, last_pub = self.asm.version, now
                map_id = max(self.asm.maps, key=lambda m: self.asm.piece_count(m))
                try:
                    grid = self.asm.assemble(map_id)
                    for jid, r in self.robots.items():
                        trace = [(p.x, p.y) for p in ([r.position] if r.position else [])]
                        png = render_png(
                            grid,
                            trace=trace,
                            position=(r.position.x, r.position.y) if r.position else None,
                            dock=(r.charger.x, r.charger.y) if r.charger else None,
                        )
                        await self.pub.publish_map(r.slug, png)
                    log.info(
                        "mapa %s publicado (v%d, %d piezas)",
                        map_id, self.asm.version, total,
                    )
                except Exception:  # noqa: BLE001
                    log.exception("render/publicación de mapa falló")
