"""Modo servicio: XMPP + MQTT (Bumper) ⇄ MQTT (Home Assistant), por robot.

- Login en Bumper como cliente XMPP (robots era 930) y, si está activado,
  como cliente MQTT del broker (robots eco-ng T8+, spec mqtt-json-spec.md)
- Poll suave read-only cada POLL_SECONDS (no despierta al robot en base)
- MapP pushes / getMinorMap → MapAssembler → PNG → topic de la cámara
- Comandos MQTT → vocabulario XMPP o JSON según el protocolo del robot
"""

from __future__ import annotations

import asyncio
import logging
import time

from .config import Config
from .map_pieces import MapAssembler, decode_trace_mqtt, render_png
from .models import DeviceId
from .mqtt_pub import MqttPublisher
from .robot import SPEED_JSON, RobotCommander, RobotState
from .webpanel import WebPanel
from .xmpp_client import BumperXMPPClient

log = logging.getLogger(__name__)

POLL_SECONDS = 30
POLL_MQTT_SECONDS = 60
TRACE_FETCH_SECONDS = 30
# robots se detectan automáticamente al recibir su primer frame

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

# respuestas/reportes que disparan fetch del mapa completo
MAJOR_MAP_NAMES = ("getMajorMap", "onMajorMap")
MINOR_MAP_NAMES = ("getMinorMap", "onMinorMap")
MAP_INFO_NAMES = ("getMapInfo", "onMapInfo")
TRACE_NAMES = ("getMapTrace", "onMapTrace")


class Service:
    def __init__(self, cfg: Config, jid_hint: str | None):
        self.cfg = cfg
        self.jid_hint = jid_hint  # None = auto-detección por on_event
        self.robots: dict[str, RobotState] = {}
        self.commanders: dict[str, object] = {}
        self.asm = MapAssembler()
        self.fan_speeds: dict[str, str] = {}
        self.pub = MqttPublisher(
            cfg.mqtt.host, cfg.mqtt.port, cfg.mqtt.user, cfg.mqtt.password,
            cfg.mqtt.base_topic,
        )
        self.client: BumperXMPPClient | None = None
        self.mqtt_transport = None  # MqttTransport (eco-ng), si está activado
        self.panel = WebPanel(self, port=cfg.panel_port, host=cfg.panel_host)
        self._last_publish: dict[str, float] = {}
        self._last_status: dict[str, str] = {}
        self._last_trace_fetch: dict[str, float] = {}
        self._walking = False
        # map_id → robot key: cada robot renderiza SU mapa (evita colisión
        # de mids entre protocolos — el 930 y un eco-ng pueden usar ambos "0")
        self._map_owner: dict[str, str] = {}

    # ---------- adopción ----------

    def _adopt_xmpp(self, jid: str) -> None:
        r = RobotState.from_jid(jid)
        self.robots[jid] = r
        self.commanders[jid] = RobotCommander(self.client, r)  # type: ignore[arg-type]
        log.info("robot XMPP adoptado: %s (%s)", jid, r.slug)

    def _adopt_mqtt(self, key: str) -> RobotState:
        did = DeviceId.from_key(key, protocol="mqtt")
        r = RobotState.from_device_id(did)
        self.robots[key] = r
        from .mqtt_commander import MqttCommander

        self.commanders[key] = MqttCommander(self.mqtt_transport, r)  # type: ignore[arg-type]
        log.info("robot eco-ng adoptado: %s (%s)", key, r.slug)
        return r

    async def _adopt_and_discover(self, key: str, mqtt: bool) -> None:
        r = self._adopt_mqtt(key) if mqtt else (self._adopt_xmpp(key) or self.robots[key])
        await self.pub.publish_discovery(r.slug, r.model.display, r.model.ha_model)
        if mqtt:
            c = self.commanders[key]
            # sincronización inicial (el resto llega por reports)
            c.get_battery_info()
            c.get_pos()
            c.get_clean_state()
            c.get_charge_state()
            c.get_life_span()
            c.get_error()
            c.get_version()
            c.get_major_map()
            await self._publish_state(key, force=True)
        else:
            # sondeo inicial XMPP: fuerza respuestas dirigidas a esta sesión
            self._poll_xmpp_robot(self.commanders[key])
            await self._publish_state(key, force=True)

    # ---------- eventos XMPP (robots era 930) ----------

    async def on_event(self, jid: str, attrs: dict, inner: str) -> None:
        if "ecorobot.net" not in jid:
            return
        if jid not in self.robots:
            await self._adopt_and_discover(jid, mqtt=False)
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
            self._map_owner.setdefault(map_id, jid)
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

    # ---------- eventos MQTT (robots eco-ng T8+) ----------

    async def on_report(self, key: str, name: str, data: dict) -> None:
        """Reports iot/atr y respuestas p2p — misma forma de payload."""
        if key not in self.robots:
            await self._adopt_and_discover(key, mqtt=True)
        r = self.robots.get(key)
        if r is None:
            return

        # ---- mapa: spec (geometría + CRCs) → walk de piezas cambiadas
        if name in MAJOR_MAP_NAMES and "value" in data:
            # namespace por robot: los mids no son únicos entre protocolos
            map_id = f"{r.did.sn}:{data.get('mid', '0')}" if r.did else str(data.get("mid", "0"))
            self._map_owner.setdefault(map_id, key)
            before = self.asm.spec_changes
            self.asm.set_geometry_json(map_id, data)
            if self.asm.spec_changes > before:
                log.info("spec mapa JSON %s: %d crcs — lanzando minor walk",
                         map_id, len(self.asm.crcs.get(map_id, [])))
                asyncio.create_task(self._minor_walk(map_id))
            return

        # ---- mapa: pieza individual
        if name in MINOR_MAP_NAMES and "pieceValue" in data:
            raw_mid = str(data.get("mid", "0"))
            map_id = f"{r.did.sn}:{raw_mid}" if r.did else raw_mid
            pid = int(data.get("pieceIndex", -1))
            if pid >= 0 and self.asm.add_piece_json(map_id, pid, data["pieceValue"]):
                log.debug("pieza JSON mapa %s#%d aceptada", map_id, pid)
            return

        # ---- mapa: formato chunked (T8/N8)
        if name in MAP_INFO_NAMES and "value" in data and "totalCount" in data:
            raw_mid = str(data.get("mid", "0"))
            map_id = f"{r.did.sn}:{raw_mid}" if r.did else raw_mid
            self._map_owner.setdefault(map_id, key)
            if self.asm.add_chunk_json(map_id, int(data.get("index", 0)),
                                       data["value"], int(data["totalCount"]), data):
                log.info("mapa chunked %s montado", map_id)
            return

        # ---- traza real (5 bytes/punto)
        if name in TRACE_NAMES and "traceValue" in data:
            try:
                pts = decode_trace_mqtt(data["traceValue"])
                if pts:
                    r.trace_points = [(x, y) for x, y, _ in pts]
                    log.debug("traza %s: %d puntos", key, len(pts))
            except Exception:  # noqa: BLE001
                log.exception("traza MQTT no decodificó")
            return

        r.update_report(name, data)

        # traza en vivo mientras limpia (la traza completa llega por getMapTrace)
        if r.status == "cleaning" and r.did and r.did.protocol == "mqtt":
            now = time.monotonic()
            if now - self._last_trace_fetch.get(key, 0) > TRACE_FETCH_SECONDS:
                self._last_trace_fetch[key] = now
                self.commanders[key].get_map_trace(0, 1500)  # type: ignore[attr-defined]

        await self._publish_state(key)

    # ---------- estado → HA + panel ----------

    async def _publish_state(self, key: str, force: bool = False) -> None:
        r = self.robots[key]
        now = time.monotonic()
        changed = self._last_status.get(key) != r.status
        if not force and not changed and now - self._last_publish.get(key, 0) < 2:
            return
        self._last_publish[key] = now
        self._last_status[key] = r.status
        if r.fan_speed_json is not None:
            self.fan_speeds[r.slug] = SPEED_JSON.get(r.fan_speed_json, "standard")
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
        for key2, comp in {"brush": "Brush", "sidebrush": "SideBrush",
                           "dustcase": "DustCaseHeap", "airfilter": "AirFilter"}.items():
            life = r.lifespans.get(comp)
            if life and life.get("total"):
                await self.pub.publish_consumable(r.slug, key2, round(life["left"] / life["total"] * 100))
        await self.pub.publish_attributes(r.slug, doc)
        await self.panel.broadcast(doc, slug=r.slug)

    # ---------- comandos MQTT (HA/panel) ----------

    async def on_command(self, slug: str, action: str, val) -> None:
        for key, r in self.robots.items():
            if r.slug != slug:
                continue
            c = self.commanders[key]
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
                c.set_clean_speed(val)
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

    # ---------- polls ----------

    async def poll_loop(self) -> None:
        while True:
            for key, c in self.commanders.items():
                r = self.robots[key]
                try:
                    if r.did and r.did.protocol == "mqtt":
                        await self._poll_mqtt_robot(key, c)
                    else:
                        self._poll_xmpp_robot(c)
                except Exception:  # noqa: BLE001
                    log.exception("poll falló para %s", key)
            await asyncio.sleep(POLL_SECONDS)

    def _poll_xmpp_robot(self, c) -> None:
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

    async def _poll_mqtt_robot(self, key: str, c) -> None:
        now = time.monotonic()
        last = self._last_publish.get(f"{key}::poll", 0)
        if now - last < POLL_MQTT_SECONDS:
            return
        self._last_publish[f"{key}::poll"] = now
        c.get_battery_info()
        c.get_pos()
        c.get_clean_state()
        c.get_charge_state()
        c.get_life_span()
        c.get_error()
        c.get_major_map()

    # ---------- walks de mapa ----------

    async def _pull_walk(self, map_id: str) -> None:
        """Repone las piezas emitidas antes de arrancar: PullMP 0..63 (como la app)."""
        c = next((c for j, c in self.commanders.items()
                  if not (self.robots[j].did and self.robots[j].did.protocol == "mqtt")), None)
        if not c:
            return
        log.info("pull walk mapa %s", map_id)
        for pid in range(64):
            try:
                c.pull_map_piece(pid)
            except Exception:  # noqa: BLE001
                return
            await asyncio.sleep(0.25)

    async def _minor_walk(self, map_id: str) -> None:
        """Pide getMinorMap de las piezas cuyo CRC cambió (robots eco-ng)."""
        if self._walking:
            return
        c = next((c for j, c in self.commanders.items()
                  if self.robots[j].did and self.robots[j].did.protocol == "mqtt"), None)
        if not c:
            return
        self._walking = True
        try:
            crcs = self.asm.crcs.get(map_id, [])
            missing = [pid for pid, crc in enumerate(crcs)
                       if crc and self.asm.piece_crc(map_id, pid) != crc]
            log.info("minor walk mapa %s: %d piezas a pedir", map_id, len(missing))
            for pid in missing:
                try:
                    c.get_minor_map(pid, mid=map_id)
                except Exception:  # noqa: BLE001
                    log.exception("getMinorMap %d falló", pid)
                await asyncio.sleep(0.25)
        finally:
            self._walking = False

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

        tasks = [self.client.listen(), self.pub.listen_commands(),
                 self.poll_loop(), self._map_publisher()]

        # transporte MQTT (robots eco-ng T8+) — opcional
        bm = self.cfg.bumper_mqtt
        if bm.enabled:
            from .mqtt_transport import MqttTransport

            self.mqtt_transport = MqttTransport(
                bm.host, bm.port, ca_file=bm.ca_file, on_data=self.on_report)
            await self.mqtt_transport.connect()
            tasks.append(self.mqtt_transport.listen())
            # robots conocidos estáticos (dormidos no emiten reports)
            for key in bm.robots:
                if "ecorobot.net" in key or "/" not in key.split("@")[-1]:
                    log.error("bumper_mqtt.robots espera did@class/res, no JIDs: %r", key)
                    continue
                if key not in self.robots:
                    try:
                        await self._adopt_and_discover(key, mqtt=True)
                    except Exception:  # noqa: BLE001
                        log.exception("adopción estática falló para %s", key)

        # cargar mapa del log de Bumper (sobrevive a reinicios del serve)
        self._load_map_from_log()

        # adopción inicial del robot conocido: un robot en base no emite
        # frames por sí solo — el sondeo inicial (con NUESTRO jid como from)
        # fuerza respuestas que llegan a esta sesión
        if self.jid_hint and self.jid_hint not in self.robots:
            try:
                await self._adopt_and_discover(self.jid_hint, mqtt=False)
            except Exception:  # noqa: BLE001
                log.exception("adopción inicial de %s falló", self.jid_hint)

        await self.panel.start()
        log.info("serve operativo (poll cada %ds, panel en :8080, mqtt eco-ng %s)",
                 POLL_SECONDS, "on" if bm.enabled else "off")
        await asyncio.gather(*tasks)

    def _load_map_from_log(self) -> None:
        """Carga las últimas piezas MapP del log de Bumper al arrancar.
        Sin validación CRC (eso se hace en vivo) — última versión por pid."""
        import re
        from pathlib import Path

        log_path = Path(__file__).parent.parent.parent / "data" / "captures" / "pairing-session.log"
        if not log_path.exists():
            log.debug("sin log de Bumper para precargar mapa")
            return

        # pasada 1: spec más reciente (para saber qué mapa es el actual)
        latest_mapid = None
        for line in open(log_path, errors="replace"):
            m = re.search(r'<ctl c="\d+" h="\d+" i="(\d+)" id="[0-9a-f]+" m="[^"]+" p="\d+" r="\d+" w="\d+"', line)
            if m:
                latest_mapid = m.group(1)
        if not latest_mapid:
            return

        # pasada 2: última pieza por pid del mapa actual
        best: dict[int, str] = {}
        for line in open(log_path, errors="replace"):
            if 'td="MapP"' not in line:
                continue
            m = re.search(r'i="(\d+)"[^>]*p="([A-Za-z0-9+/=]{20,})"[^>]*pid="(\d+)"', line)
            if not m or m.group(1) != latest_mapid:
                continue
            pid = int(m.group(3))
            try:
                from .map_pieces import decode_piece
                decode_piece(m.group(2))  # validar que decodifica
                best[pid] = m.group(2)  # nos quedamos con la última
            except Exception:
                pass

        loaded = 0
        for pid, p_b64 in best.items():
            try:
                if self.asm.add_piece(latest_mapid, pid, p_b64):
                    loaded += 1
            except Exception:
                pass
        if loaded:
            log.info("precargadas %d piezas del mapa %s", loaded, latest_mapid)
            self.asm.version += 1
        else:
            log.warning("precarga: 0 piezas validas para mapa %s", latest_mapid)

    def _render_map_png(self, map_id: str, robot: RobotState | None = None) -> bytes:
        """Render PNG del mapa para MQTT/HA (overlay del robot indicado)."""
        grid = self.asm.assemble(map_id)
        if not grid:
            return b""
        r = robot or next(iter(self.robots.values()), None)
        if r is None:
            return render_png(grid, geo=self.asm.geo(map_id))
        return render_png(
            grid,
            trace=r.trace_points[-2000:] or None,
            position=(r.position.x, r.position.y) if r.position else None,
            dock=(r.charger.x, r.charger.y) if r.charger else None,
            geo=self.asm.geo(map_id),
        )

    def _robot_map_id(self, key: str) -> str | None:
        """Mapa propio del robot; fallback con afinidad de protocolo.

        Los mids eco-ng van con namespace ("sn:mid", contienen ':'), los
        XMPP quedan tal cual — el mapa precargado del log del 930 (sin
        owner conocido) así no se confunde con el de un eco-ng.
        """
        r = self.robots.get(key)
        is_mqtt = bool(r and r.did and r.did.protocol == "mqtt")
        owned = [m for m, owner in self._map_owner.items()
                 if owner == key and self.asm.maps.get(m)]
        if owned:
            return max(owned, key=lambda m: self.asm.piece_count(m))
        cands = [m for m in self.asm.maps if (":" in m) == is_mqtt]
        if not cands:
            cands = list(self.asm.maps)
        return max(cands, key=lambda m: self.asm.piece_count(m)) if cands else None

    async def _map_publisher(self) -> None:
        """Publica cada mapa de cada robot cuando cambia el contenido."""
        last_version, last_pub, last_walk = -1, 0.0, 0.0
        walking = False
        while True:
            await asyncio.sleep(5)
            now = time.monotonic()

            # re-walk XMPP: reponer piezas cada 60s (la app hace lo mismo al abrir el mapa)
            if not walking and now - last_walk > 60 and self.asm.maps:
                has_xmpp = any(r.did and r.did.protocol == "xmpp"
                               for r in self.robots.values()) or not self.robots
                if has_xmpp:
                    walking = True
                    last_walk = now

                    async def walk() -> None:
                        nonlocal walking, last_version
                        try:
                            await self._pull_walk(max(self.asm.maps,
                                                      key=lambda m: self.asm.piece_count(m)))
                        finally:
                            walking = False
                            last_version = -1  # fuerza republicación tras cada walk

                    asyncio.create_task(walk())

            # publicar si el contenido del mapa cambió (no solo si crece)
            if self.asm.version != last_version and now - last_pub > 10 and self.asm.maps:
                last_version, last_pub = self.asm.version, now
                try:
                    # mapa al panel (canvas por WebSocket, uno por robot)
                    self.panel.build_and_broadcast_map()
                    # mapa a HA (PNG via MQTT) — cada robot con SU mapa
                    for key, r in self.robots.items():
                        map_id = self._robot_map_id(key)
                        if map_id:
                            await self.pub.publish_map(r.slug,
                                                       self._render_map_png(map_id, r))
                    log.info("mapas publicados (v%d, %d piezas en %d mapas)",
                             self.asm.version,
                             sum(self.asm.piece_count(m) for m in self.asm.maps),
                             len(self.asm.maps))
                except Exception:  # noqa: BLE001
                    log.exception("publicación de mapa falló")
