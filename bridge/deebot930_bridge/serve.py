"""Modo servicio: XMPP + MQTT (Bumper) ⇄ MQTT (Home Assistant), por robot.

- Login en Bumper como cliente XMPP (robots era 930) y, si está activado,
  como cliente MQTT del broker (robots eco-ng T8+, spec mqtt-json-spec.md)
- Poll suave read-only cada POLL_SECONDS (no despierta al robot en base)
- MapP pushes / getMinorMap → MapAssembler → PNG → topic de la cámara
- Comandos MQTT → vocabulario XMPP o JSON según el protocolo del robot
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from pathlib import Path

from .config import Config
from .map_pieces import MapAssembler, MapGeometry, decode_trace_mqtt, render_png
from .models import DeviceId
from .mqtt_pub import MqttPublisher
from .robot import SPEED_JSON, Position, RobotCommander, RobotState
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
        # servidores embebidos activos (absorción de Bumper, Fase F)
        self.embedded: dict[str, object] = {}
        # registro unificado de bots/clients (cuando hay servidores embebidos)
        self.registry = None
        if cfg.servers.panel:
            self.panel = WebPanel(self, port=cfg.panel_port, host=cfg.panel_host)
        else:
            from .webpanel import NullPanel

            self.panel = NullPanel()  # type: ignore[assignment]
        self._last_publish: dict[str, float] = {}
        self._last_status: dict[str, str] = {}
        self._last_trace_fetch: dict[str, float] = {}
        self._last_poll: dict[str, float] = {}  # (C3) separado del throttle de estado
        self._walking = False
        self._pending_walks: set[str] = set()
        # map_id → robot key: cada robot renderiza SU mapa (evita colisión
        # de mids entre protocolos — el 930 y un eco-ng pueden usar ambos "0")
        self._map_owner: dict[str, str] = {}
        # posiciones persistidas por slug (semilla al adoptar, para overlays
        # inmediatos tras reinicio sin esperar al robot)
        self._seed_positions: dict[str, dict] = {}

    # ---------- adopción ----------

    def _adopt_xmpp(self, jid: str) -> None:
        r = RobotState.from_jid(jid)
        self._seed_robot(r)
        self.robots[jid] = r
        self.commanders[jid] = RobotCommander(self.client, r)  # type: ignore[arg-type]
        log.info("robot XMPP adoptado: %s (%s)", jid, r.slug)

    def _adopt_mqtt(self, key: str) -> RobotState:
        did = DeviceId.from_key(key, protocol="mqtt")
        r = RobotState.from_device_id(did)
        self._seed_robot(r)
        self.robots[key] = r
        from .mqtt_commander import MqttCommander

        self.commanders[key] = MqttCommander(self.mqtt_transport, r)  # type: ignore[arg-type]
        log.info("robot eco-ng adoptado: %s (%s)", key, r.slug)
        return r

    def _seed_robot(self, r: RobotState) -> None:
        """Aplica la última posición/base persistida (overlays inmediatos
        tras reinicio, sin esperar a que el robot hable)."""
        seed = self._seed_positions.get(r.slug)
        if not seed:
            return
        pos = seed.get("position")
        if pos and r.position is None:
            r.position = Position(*pos)
        ch = seed.get("charger")
        if ch and r.charger is None:
            r.charger = Position(*ch)

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
            c.get_map_set("ar")
            c.get_map_set("vw")
            c.get_map_set("mw")
            c.get_stats()
            await self._publish_state(key, force=True)
        else:
            # sondeo inicial XMPP: fuerza respuestas dirigidas a esta sesión
            self._poll_xmpp_robot(self.commanders[key], deep=True)
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
            # walk si la spec cambió O si aún no hay piezas de ese mapa
            # (robot en base no empuja MapP: sin walk inicial, deadlock)
            if self.asm.spec_changes > before or not self.asm.maps.get(map_id):
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
            try:
                if self.asm.add_piece(map_id, pid, p):
                    log.info("pieza mapa %s#%d aceptada", map_id, pid)
            except Exception:  # noqa: BLE001  (B13: pieza corrupta no tira el handler)
                log.exception("pieza mapa %s#%d ilegible", map_id, pid)

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
        if name in MAJOR_MAP_NAMES:
            if data.get("ret") == "fail":
                # la familia T8/N8 no tiene getMajorMap: formato chunked
                c = self.commanders.get(key)
                if c is not None and hasattr(c, "get_map_info"):
                    c.get_map_info()
                return
            if "value" not in data:
                return
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
            self._map_owner.setdefault(map_id, key)
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

        # ---- traza real (5 bytes/punto) + paginación (B14)
        if name in TRACE_NAMES and "traceValue" in data:
            try:
                pts = decode_trace_mqtt(data["traceValue"])
                total = int(data.get("totalCount", 0) or 0)
                start = int(data.get("traceStart", 0) or 0)
                if pts:
                    page = [(x, y) for x, y, _ in pts]
                    # primera página reemplaza; las siguientes se añaden
                    r.trace_points = page if start == 0 else r.trace_points + page
                    log.debug("traza %s: %d puntos en [%d..%d) de %d",
                              key, len(pts), start, start + len(pts), total)
                # ¿quedan más páginas? pedir la siguiente
                if total and start + len(pts) < total:
                    c = self.commanders.get(key)
                    if c is not None:
                        c.get_map_trace(start + len(pts), 1500)
            except Exception:  # noqa: BLE001
                log.exception("traza MQTT no decodificó")
            return

        r.update_report(name, data)

        # contornos de muros/zonas recién listados (getMapSubSet por entrada)
        if RobotState._canon_name(name) == "mapSet" and "subsets" in data:
            ms = r.map_sets.get(str(data.get("type", "")))
            if ms and ms.tp in ("vw", "mw"):
                c = self.commanders.get(key)
                if c is not None:
                    for e in list(ms.entries):
                        if isinstance(e, dict) and e.get("_pending_outline"):
                            c.get_map_subset(e["mssid"], ms.tp, msid=ms.msid or "0")

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
            "map_sets": {tp: ms.to_doc() for tp, ms in r.map_sets.items()},
            "stats": r.stats,
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
            elif action == "spot":
                c.start_clean(clean_type="spot")
            elif action == "fan_speed":
                self.fan_speeds[slug] = val
                c.set_clean_speed(val)
                await self.pub.publish_fan_speed(slug, val)
            elif action == "clean_room":
                # val = "mssid" o "mssid1,mssid2" (eco-ng) / "idx,idx" (930)
                rooms = str(val or "").strip()
                mid = self._robot_map_id(key) or "0"
                c.clean_room(rooms, mid=mid.split(":")[-1])
            elif action == "add_wall":
                c.add_wall(val)
                self._refresh_mapsets(key)
            elif action == "del_wall":
                c.del_wall(str(val))
                self._refresh_mapsets(key)
            elif action == "add_zone":
                c.add_zone(val)
                self._refresh_mapsets(key)
            elif action == "del_zone":
                c.del_zone(str(val))
                self._refresh_mapsets(key)
            elif action == "reset_life_span":
                c.reset_life_span(val or "Brush")
            elif action == "dnd":
                await self.pub.publish_switch_state(slug, "dnd", val == "ON")
                c.set_block_time_state(val == "ON")
            elif action == "silent":
                await self.pub.publish_switch_state(slug, "silent", val == "ON")
                c.set_silent_mode_state(val == "ON")
            return

    def _refresh_mapsets(self, key: str) -> None:
        """Re-pide mapsets tras un cambio (muro/zona añadido o borrado)."""
        c = self.commanders.get(key)
        r = self.robots.get(key)
        if c is None or r is None:
            return
        if r.did and r.did.protocol == "mqtt":
            c.get_map_set("vw")
            c.get_map_set("mw")
            c.get_map_set("ar")
        else:
            c.get_map_set("vw")
            c.get_map_set("sa")

    # ---------- polls ----------

    MAPSET_POLL_CYCLES = 5  # mapsets+stats cada N ciclos de poll

    async def poll_loop(self) -> None:
        cycle = 0
        while True:
            cycle += 1
            deep = cycle % self.MAPSET_POLL_CYCLES == 0
            for key, c in list(self.commanders.items()):  # snapshot: adopciones concurrentes
                r = self.robots.get(key)
                if r is None:
                    continue
                try:
                    if r.did and r.did.protocol == "mqtt":
                        await self._poll_mqtt_robot(key, c, deep)
                    else:
                        self._poll_xmpp_robot(c, deep)
                except Exception:  # noqa: BLE001
                    log.exception("poll falló para %s", key)
            # posiciones/mapsets cambian sin tocar el mapa: guardar igual
            # (la huella evita escrituras redundantes)
            self._save_state()
            await asyncio.sleep(POLL_SECONDS)

    def _poll_xmpp_robot(self, c, deep: bool = False) -> None:
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
        if deep:  # muros/zonas + stats (menos frecuentes)
            c.get_map_set("vw")
            c.get_map_set("sa")
            c.get_clean_statistics()
            c.get_clean_sum()

    async def _poll_mqtt_robot(self, key: str, c, deep: bool = False) -> None:
        now = time.monotonic()
        if now - self._last_poll.get(key, 0) < POLL_MQTT_SECONDS:
            return
        self._last_poll[key] = now
        c.get_battery_info()
        c.get_pos()
        c.get_clean_state()
        c.get_charge_state()
        c.get_life_span()
        c.get_error()
        c.get_major_map()
        if deep:  # habitaciones + muros/zonas + stats (menos frecuentes)
            c.get_map_set("ar")
            c.get_map_set("vw")
            c.get_map_set("mw")
            c.get_stats()

    # ---------- walks de mapa ----------

    def _commander_for_map(self, map_id: str):
        """Commander del robot dueño del mapa (B6): los walks multi-robot
        no deben salir por el primer commander del protocolo que haya."""
        owner = self._map_owner.get(map_id)
        c = self.commanders.get(owner) if owner else None
        if c is not None:
            return c
        # sin owner conocido → primer commander del protocolo afín
        is_mqtt = ":" in map_id  # los mids eco-ng van con namespace sn:mid
        for key, cmd in self.commanders.items():
            r = self.robots.get(key)
            if r and r.did and (r.did.protocol == "mqtt") == is_mqtt:
                return cmd
        return None

    async def _minor_walk(self, map_id: str) -> None:
        """Pide getMinorMap de las piezas cuyo CRC cambió (robots eco-ng).

        Cola por mapa (B7): si llega un walk mientras otro está en curso,
        queda pendiente y se procesa al terminar — nada se descarta.
        El mid del wire es el CRUDO: el namespace sn: es solo interno (B2).
        """
        self._pending_walks.add(map_id)
        if self._walking:
            return
        self._walking = True
        try:
            while self._pending_walks:
                map_id = self._pending_walks.pop()
                c = self._commander_for_map(map_id)
                if c is None:
                    continue
                raw_mid = map_id.split(":", 1)[1] if ":" in map_id else map_id
                crcs = self.asm.crcs.get(map_id, [])
                missing = [pid for pid, crc in enumerate(crcs)
                           if crc and self.asm.piece_crc(map_id, pid) != crc]
                log.info("minor walk mapa %s: %d piezas a pedir", map_id, len(missing))
                for pid in missing:
                    try:
                        c.get_minor_map(pid, mid=raw_mid)
                    except Exception:  # noqa: BLE001
                        log.exception("getMinorMap %d falló", pid)
                    await asyncio.sleep(0.25)
        finally:
            self._walking = False

    async def _pull_walk(self, map_id: str) -> None:
        """Repone las piezas emitidas antes de arrancar: PullMP 0..63 (como la app)."""
        c = self._commander_for_map(map_id)
        if not c:
            return
        log.info("pull walk mapa %s", map_id)
        for pid in range(64):
            try:
                c.pull_map_piece(pid)
            except Exception:  # noqa: BLE001
                return
            await asyncio.sleep(0.25)

    # ---------- servidores embebidos (absorción de Bumper, Fase F) ----------

    async def _start_embedded_servers(self) -> None:
        """Arranca SOLO los servidores activos en [servers].

        Cada fase de la absorción añade los suyos; los aún no implementados
        avisan en vez de fallar en silencio.
        """
        srv = self.cfg.servers
        from .servers import NOT_YET

        for name, phase in NOT_YET.items():
            if getattr(srv, name):
                log.warning("servidor embebido '%s' llega en la fase %s — ignorado",
                            name, phase)

        # registro unificado de bots/clients: lo comparten el XMPP (FB) y el
        # broker MQTT (FC) — persistente junto al state_file (si hay)
        if srv.xmpp or srv.mqtt:
            from .servers.registry import DeviceRegistry

            reg_path = None
            if self.cfg.state_file:
                from pathlib import Path as _P

                reg_path = str(_P(self.cfg.state_file).parent / "registry.json")
            self.registry = DeviceRegistry(persist_path=reg_path)
            self.registry.load()

        if srv.lookup:
            from .servers.lookup import LookupServer

            lookup = LookupServer(announce_ip=srv.announce_ip,
                                  xmpp_port=srv.xmpp_port,
                                  listen=srv.listen, port=srv.lookup_port)
            await lookup.start()
            self.embedded["lookup"] = lookup

        if srv.xmpp:
            from .servers.xmpp import XMPPServer

            xmpp = XMPPServer((srv.listen, srv.xmpp_port), self.registry,
                              server_cert=srv.server_cert, server_key=srv.server_key,
                              ca_cert=srv.ca_file)
            await xmpp.start_async_server()
            if xmpp.started:
                self.embedded["xmpp"] = xmpp
                # el cliente XMPP del bridge habla con NUESTRO servidor (loopback)
                b = self.cfg.bumper
                if b.host not in ("127.0.0.1", "localhost"):
                    log.info("xmpp embebido activo: cliente XMPP → 127.0.0.1:%d "
                             "(config [bumper] host=%s ignorado)", srv.xmpp_port, b.host)
                b.host, b.port = "127.0.0.1", srv.xmpp_port
            else:
                log.error("XMPP embebido no arrancó — el cliente sigue con "
                          "su config [bumper] externa")

        if srv.mqtt:
            from .servers.mqtt import EmbeddedMqttBroker

            try:
                broker = EmbeddedMqttBroker(
                    (srv.listen, srv.mqtt_port), self.registry,
                    server_cert=srv.server_cert, server_key=srv.server_key)
                await broker.start()
            except Exception:  # noqa: BLE001  (certs ausentes, puerto ocupado…)
                log.exception("broker MQTT embebido no pudo arrancar")
                return
            self.embedded["mqtt"] = broker
            # el cliente eco-ng del bridge habla con NUESTRO broker (loopback)
            bm = self.cfg.bumper_mqtt
            if bm.enabled:
                if (bm.host, bm.port) != ("127.0.0.1", srv.mqtt_port):
                    log.info("mqtt eco-ng embebido activo: cliente [bumper_mqtt] → "
                             "127.0.0.1:%d (config %s:%d ignorada)",
                             srv.mqtt_port, bm.host, bm.port)
                bm.host, bm.port = "127.0.0.1", srv.mqtt_port
                # el cert que presenta el broker es el nuestro (ServersConfig)
                bm.ca_file = srv.ca_file
                # TLS explícito: el heurístico por puerto (8883) rompería si
                # mqtt_port se cambia para convivir con un Bumper externo
                bm.tls = True
            else:
                log.warning("broker eco-ng en :%d pero [bumper_mqtt] enabled=false "
                            "— sin cliente T8+", srv.mqtt_port)

    # ---------- bucle principal ----------

    async def run(self) -> None:
        b = self.cfg.bumper
        await self._start_embedded_servers()
        self.client = BumperXMPPClient(
            host=b.host, port=b.port, uid=b.uid, resource=b.resource,
            ca_file=b.ca_file, on_event=self.on_event,
        )
        await self.client.connect()
        await self.pub.connect()
        self.pub.on_command = self.on_command

        tasks = [self.client.listen(), self.pub.listen_commands(),
                 self.poll_loop(), self._map_publisher()]

        # transporte MQTT (robots eco-ng T8+) — opcional. Si el broker no
        # responde al arranque NO tumbamos el serve (B5): listen() reintenta
        # y send_command degrada con {"ret":"fail"}.
        bm = self.cfg.bumper_mqtt
        if bm.enabled:
            from .mqtt_transport import MqttTransport

            self.mqtt_transport = MqttTransport(
                bm.host, bm.port, ca_file=bm.ca_file, on_data=self.on_report,
                tls=bm.tls)
            try:
                await self.mqtt_transport.connect()
            except Exception:  # noqa: BLE001
                log.exception("broker eco-ng no conectó al arranque — reintentando en background")
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
        # restaurar estado persistido (piezas + posiciones): el mapa se
        # publica sin depender del robot
        self._load_state()

        # adopción inicial de robots conocidos: un robot en base no emite
        # frames por sí solo — el sondeo inicial (con NUESTRO jid como from)
        # fuerza respuestas que llegan a esta sesión
        known = [j for j in [self.jid_hint, *self.cfg.robots] if j]
        for jid in dict.fromkeys(known):  # sin duplicados, orden estable
            if jid in self.robots:
                continue
            try:
                await self._adopt_and_discover(jid, mqtt=False)
            except Exception:  # noqa: BLE001
                log.exception("adopción inicial de %s falló", jid)

        await self.panel.start()
        log.info("serve operativo (poll cada %ds, panel %s, mqtt eco-ng %s, embebidos: %s)",
                 POLL_SECONDS,
                 f":{self.panel.port}" if self.cfg.servers.panel else "off",
                 "on" if bm.enabled else "off",
                 ",".join(self.embedded) or "ninguno")
        try:
            await asyncio.gather(*tasks)
        finally:
            await self._stop_embedded_servers()

    async def _stop_embedded_servers(self) -> None:
        """Apagado graceful de los servidores embebidos (orden inverso)."""
        for name, srv in reversed(list(self.embedded.items())):
            try:
                if name == "xmpp":
                    srv.disconnect()  # type: ignore[attr-defined]
                elif hasattr(srv, "stop"):
                    await srv.stop()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                log.exception("parando %s embebido falló", name)
        self.embedded.clear()

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

    # ---------- persistencia ----------

    def _save_state(self) -> None:
        """Piezas por map_id + geometría + posiciones → JSON atómico.

        El mapa sobrevive a reinicios del bridge sin depender del robot
        (un robot en base no empuja piezas) y el panel/HA lo pintan nada
        más arrancar. Sólo escribe si algo cambió (huella).
        """
        if not self.cfg.state_file:
            return
        fp = (self.asm.version,
              tuple((r.slug,
                     (r.position.x, r.position.y) if r.position else None,
                     (r.charger.x, r.charger.y) if r.charger else None)
                    for r in self.robots.values()),
              len(self._map_owner))
        if fp == getattr(self, "_state_fp", None):
            return
        self._state_fp = fp
        try:
            doc = {
                "maps": {
                    mid: {str(pid): base64.b64encode(piece).decode()
                          for pid, piece in pieces.items()}
                    for mid, pieces in self.asm.maps.items()
                },
                "geometry": {mid: vars(g) for mid, g in self.asm.geometry.items()},
                "crcs": self.asm.crcs,
                "version": self.asm.version,
                "map_owner": self._map_owner,
                "positions": {
                    r.slug: {
                        "position": [r.position.x, r.position.y, r.position.angle]
                        if r.position else None,
                        "charger": [r.charger.x, r.charger.y, r.charger.angle]
                        if r.charger else None,
                    } for r in self.robots.values()
                },
            }
            path = Path(self.cfg.state_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(doc))
            tmp.replace(path)
            log.debug("estado persistido en %s (%d mapas)", path, len(doc["maps"]))
        except Exception:  # noqa: BLE001
            log.exception("persistencia de estado falló")

    def _load_state(self) -> None:
        """Restaura el estado persistido: el mapa se publica al primer ciclo
        del publisher (version != -1) sin esperar al robot."""
        if not self.cfg.state_file or not Path(self.cfg.state_file).exists():
            return
        try:
            doc = json.loads(Path(self.cfg.state_file).read_text())
        except (json.JSONDecodeError, OSError):
            log.exception("estado persistido ilegible — ignorado")
            return
        restored = 0
        for mid, geo_d in (doc.get("geometry") or {}).items():
            try:
                self.asm.geometry[mid] = MapGeometry(**geo_d)
            except TypeError:
                continue
        for mid, crcs in (doc.get("crcs") or {}).items():
            self.asm.crcs[mid] = [int(c) for c in crcs]
        for mid, pieces in (doc.get("maps") or {}).items():
            for pid_s, p_b64 in pieces.items():
                try:
                    self.asm.maps.setdefault(mid, {})[int(pid_s)] = base64.b64decode(p_b64)
                    restored += 1
                except (ValueError, base64.binascii.Error):
                    continue
        self.asm.version = int(doc.get("version", 0)) + 1  # fuerza publicación
        self._map_owner.update(doc.get("map_owner") or {})
        self._seed_positions = doc.get("positions") or {}
        if restored:
            log.info("estado restaurado: %d piezas en %d mapas (de %s)",
                     restored, len(self.asm.maps), self.cfg.state_file)

    def _render_map_png(self, map_id: str, robot: RobotState | None = None,
                        grid: list[list[int]] | None = None) -> bytes:
        grid = grid if grid is not None else self.asm.assemble(map_id)
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

    def _robot_map_id_by_slug(self, slug: str) -> str | None:
        for key, r in self.robots.items():
            if r.slug == slug:
                return self._robot_map_id(key)
        return None

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

            # re-walk XMPP: reponer piezas cada 60s. Basta conocer la spec
            # (crcs) aunque aún no haya piezas — sin esto, deadlock: sin
            # piezas no hay walk y sin walk nunca llegan piezas.
            if not walking and now - last_walk > 60 and (self.asm.maps or self.asm.crcs):
                has_xmpp = any(r.did and r.did.protocol == "xmpp"
                               for r in self.robots.values()) or not self.robots
                if has_xmpp:
                    walking = True
                    last_walk = now

                    async def walk() -> None:
                        nonlocal walking, last_version
                        try:
                            await self._pull_walk(
                                max(set(self.asm.maps) | set(self.asm.crcs),
                                    key=lambda m: self.asm.piece_count(m)
                                    + len(self.asm.crcs.get(m, []))))
                        finally:
                            walking = False
                            last_version = -1  # fuerza republicación tras cada walk

                    asyncio.create_task(walk())

            # publicar si el contenido del mapa cambió (no solo si crece)
            if self.asm.version != last_version and now - last_pub > 10 and self.asm.maps:
                last_version, last_pub = self.asm.version, now
                try:
                    # (O1) ensamblar cada mapa UNA vez y reutilizar para
                    # panel y renders PNG (evita 2 grids de 640k celdas/robot)
                    grids = {mid: self.asm.assemble(mid)
                             for mid in self.asm.maps if self.asm.piece_count(mid)}
                    # mapa al panel (canvas por WebSocket, uno por robot)
                    self.panel.build_and_broadcast_map(grids)
                    # mapa a HA (PNG via MQTT) — cada robot con SU mapa
                    for key, r in list(self.robots.items()):
                        map_id = self._robot_map_id(key)
                        grid = grids.get(map_id) if map_id else None
                        if grid:
                            await self.pub.publish_map(r.slug,
                                                       self._render_map_png(map_id, r, grid))
                    log.debug("mapas publicados (v%d, %d piezas en %d mapas)",
                              self.asm.version,
                              sum(self.asm.piece_count(m) for m in self.asm.maps),
                              len(self.asm.maps))
                    self._save_state()  # persiste piezas + posiciones actuales
                except Exception:  # noqa: BLE001
                    log.exception("publicación de mapa falló")
