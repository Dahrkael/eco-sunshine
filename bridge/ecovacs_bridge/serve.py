"""Service mode: XMPP + MQTT (Bumper) ⇄ MQTT (Home Assistant), per robot.

- Logs in to Bumper as an XMPP client (930-era robots) and, if enabled,
  as an MQTT client of its broker (eco-ng T8+ robots, spec mqtt-json-spec.md)
- Gentle read-only polling every POLL_SECONDS (does not wake a docked robot)
- MapP pushes / getMinorMap → MapAssembler → PNG → camera topic
- MQTT commands → XMPP or JSON vocabulary depending on the robot's protocol
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
# robots are detected automatically on their first frame

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

# replies/reports that trigger a full map fetch
MAJOR_MAP_NAMES = ("getMajorMap", "onMajorMap")
MINOR_MAP_NAMES = ("getMinorMap", "onMinorMap")
MAP_INFO_NAMES = ("getMapInfo", "onMapInfo")
TRACE_NAMES = ("getMapTrace", "onMapTrace")


class Service:
    def __init__(self, cfg: Config, jid_hint: str | None):
        self.cfg = cfg
        self.jid_hint = jid_hint  # None = auto-detect via on_event
        self.robots: dict[str, RobotState] = {}
        self.commanders: dict[str, object] = {}
        self.asm = MapAssembler()
        self.fan_speeds: dict[str, str] = {}
        self.pub = MqttPublisher(
            cfg.mqtt.host, cfg.mqtt.port, cfg.mqtt.user, cfg.mqtt.password,
            cfg.mqtt.base_topic,
        )
        self.client: BumperXMPPClient | None = None
        self.mqtt_transport = None  # MqttTransport (eco-ng), when enabled
        # active embedded servers (Bumper absorption, Phase F)
        self.embedded: dict[str, object] = {}
        # unified bots/clients registry (when embedded servers exist)
        self.registry = None
        if cfg.servers.panel:
            self.panel = WebPanel(self, port=cfg.panel_port, host=cfg.panel_host)
        else:
            from .webpanel import NullPanel

            self.panel = NullPanel()  # type: ignore[assignment]
        self._last_publish: dict[str, float] = {}
        self._last_status: dict[str, str] = {}
        self._last_trace_fetch: dict[str, float] = {}
        self._last_poll: dict[str, float] = {}  # (C3) kept separate from the state throttle
        self._walking = False
        self._pending_walks: set[str] = set()
        # map_id → robot key: each robot renders ITS own map (avoids mid
        # collisions across protocols — the 930 and an eco-ng may both use "0")
        self._map_owner: dict[str, str] = {}
        # persisted positions by slug (seeded on adoption, for immediate
        # overlays after a restart without waiting for the robot)
        self._seed_positions: dict[str, dict] = {}

    # ---------- adoption ----------

    def _adopt_xmpp(self, jid: str) -> None:
        r = RobotState.from_jid(jid)
        self._seed_robot(r)
        self.robots[jid] = r
        self.commanders[jid] = RobotCommander(self.client, r)  # type: ignore[arg-type]
        log.info("XMPP robot adopted: %s (%s)", jid, r.slug)

    def _adopt_mqtt(self, key: str) -> RobotState:
        did = DeviceId.from_key(key, protocol="mqtt")
        r = RobotState.from_device_id(did)
        self._seed_robot(r)
        self.robots[key] = r
        from .mqtt_commander import MqttCommander

        self.commanders[key] = MqttCommander(self.mqtt_transport, r)  # type: ignore[arg-type]
        log.info("eco-ng robot adopted: %s (%s)", key, r.slug)
        return r

    def _seed_robot(self, r: RobotState) -> None:
        """Applies the last persisted position/dock (immediate overlays after
        a restart, without waiting for the robot to speak)."""
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
            # initial sync (the rest arrives via reports)
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
            # initial XMPP probe: forces replies addressed to this session
            self._poll_xmpp_robot(self.commanders[key], deep=True)
            await self._publish_state(key, force=True)

    # ---------- XMPP events (930-era robots) ----------

    async def on_event(self, jid: str, attrs: dict, inner: str) -> None:
        if "ecorobot.net" not in jid:
            return
        if jid not in self.robots:
            await self._adopt_and_discover(jid, mqtt=False)
        r = self.robots[jid]
        r.update(attrs, inner)

        # charging dock: while charging/docked, the robot's position IS the dock's
        if r.status in ("charging", "docked") and r.charger is None and r.position:
            r.charger = r.position
            self.asm.version += 1  # force re-render with the dock marker
            log.info("charging dock pinned at %s", (r.charger.x, r.charger.y))

        # map spec (GetMapM reply: geometry + crcs per piece)
        if attrs.get("m") and attrs.get("i") and attrs.get("w"):
            map_id = attrs["i"]
            self._map_owner.setdefault(map_id, jid)
            before = self.asm.spec_changes
            self.asm.set_geometry(map_id, attrs)
            # walk if the spec changed OR there are no pieces yet for that
            # map (a docked robot pushes no MapP: without an initial walk, deadlock)
            if self.asm.spec_changes > before or not self.asm.maps.get(map_id):
                log.info(
                    "map spec %s: %d crcs — starting pull walk",
                    map_id, len(self.asm.crcs[map_id]),
                )
                asyncio.create_task(self._pull_walk(map_id))
            return

        # map pieces (real pid; the assembler validates CRC against the spec)
        p = attrs.get("p", "")
        if p.startswith("XQ") and attrs.get("i") and attrs.get("pid"):
            map_id, pid = attrs["i"], int(attrs["pid"])
            try:
                if self.asm.add_piece(map_id, pid, p):
                    log.info("map piece %s#%d accepted", map_id, pid)
            except Exception:  # noqa: BLE001  (B13: a corrupt piece must not kill the handler)
                log.exception("map piece %s#%d unreadable", map_id, pid)

        await self._publish_state(jid)

    # ---------- MQTT events (eco-ng T8+ robots) ----------

    async def on_report(self, key: str, name: str, data: dict) -> None:
        """iot/atr reports and p2p responses — same payload shape."""
        if key not in self.robots:
            await self._adopt_and_discover(key, mqtt=True)
        r = self.robots.get(key)
        if r is None:
            return

        # ---- map: spec (geometry + CRCs) → walk of changed pieces
        if name in MAJOR_MAP_NAMES:
            if data.get("ret") == "fail":
                # the T8/N8 family has no getMajorMap: chunked format
                c = self.commanders.get(key)
                if c is not None and hasattr(c, "get_map_info"):
                    c.get_map_info()
                return
            if "value" not in data:
                return
            # per-robot namespace: mids are not unique across protocols
            map_id = f"{r.did.sn}:{data.get('mid', '0')}" if r.did else str(data.get("mid", "0"))
            self._map_owner.setdefault(map_id, key)
            before = self.asm.spec_changes
            self.asm.set_geometry_json(map_id, data)
            if self.asm.spec_changes > before:
                log.info("JSON map spec %s: %d crcs — starting minor walk",
                         map_id, len(self.asm.crcs.get(map_id, [])))
                asyncio.create_task(self._minor_walk(map_id))
            return

        # ---- map: single piece
        if name in MINOR_MAP_NAMES and "pieceValue" in data:
            raw_mid = str(data.get("mid", "0"))
            map_id = f"{r.did.sn}:{raw_mid}" if r.did else raw_mid
            self._map_owner.setdefault(map_id, key)
            pid = int(data.get("pieceIndex", -1))
            if pid >= 0 and self.asm.add_piece_json(map_id, pid, data["pieceValue"]):
                log.debug("JSON map piece %s#%d accepted", map_id, pid)
            return

        # ---- map: chunked format (T8/N8)
        if name in MAP_INFO_NAMES and "value" in data and "totalCount" in data:
            raw_mid = str(data.get("mid", "0"))
            map_id = f"{r.did.sn}:{raw_mid}" if r.did else raw_mid
            self._map_owner.setdefault(map_id, key)
            if self.asm.add_chunk_json(map_id, int(data.get("index", 0)),
                                       data["value"], int(data["totalCount"]), data):
                log.info("chunked map %s assembled", map_id)
            return

        # ---- real trace (5 bytes/point) + pagination (B14)
        if name in TRACE_NAMES and "traceValue" in data:
            try:
                pts = decode_trace_mqtt(data["traceValue"])
                total = int(data.get("totalCount", 0) or 0)
                start = int(data.get("traceStart", 0) or 0)
                if pts:
                    page = [(x, y) for x, y, _ in pts]
                    # first page replaces; later pages append
                    r.trace_points = page if start == 0 else r.trace_points + page
                    log.debug("trace %s: %d points in [%d..%d) of %d",
                              key, len(pts), start, start + len(pts), total)
                # more pages left? request the next one
                if total and start + len(pts) < total:
                    c = self.commanders.get(key)
                    if c is not None:
                        c.get_map_trace(start + len(pts), 1500)
            except Exception:  # noqa: BLE001
                log.exception("MQTT trace failed to decode")
            return

        r.update_report(name, data)

        # outlines of freshly listed walls/zones (getMapSubSet per entry)
        if RobotState._canon_name(name) == "mapSet" and "subsets" in data:
            ms = r.map_sets.get(str(data.get("type", "")))
            if ms and ms.tp in ("vw", "mw"):
                c = self.commanders.get(key)
                if c is not None:
                    for e in list(ms.entries):
                        if isinstance(e, dict) and e.get("_pending_outline"):
                            c.get_map_subset(e["mssid"], ms.tp, msid=ms.msid or "0")

        # live trace while cleaning (the full trace arrives via getMapTrace)
        if r.status == "cleaning" and r.did and r.did.protocol == "mqtt":
            now = time.monotonic()
            if now - self._last_trace_fetch.get(key, 0) > TRACE_FETCH_SECONDS:
                self._last_trace_fetch[key] = now
                self.commanders[key].get_map_trace(0, 1500)  # type: ignore[attr-defined]

        await self._publish_state(key)

    # ---------- state → HA + panel ----------

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

    # ---------- MQTT commands (HA/panel) ----------

    async def on_command(self, slug: str, action: str, val) -> None:
        for key, r in self.robots.items():
            if r.slug != slug:
                continue
            c = self.commanders[key]
            log.info("command %s → %s %s", slug, action, val or "")
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
                # val = "mssid" or "mssid1,mssid2" (eco-ng) / "idx,idx" (930)
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
        """Re-requests mapsets after a change (wall/zone added or deleted)."""
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

    MAPSET_POLL_CYCLES = 5  # mapsets+stats every N poll cycles

    async def poll_loop(self) -> None:
        cycle = 0
        while True:
            cycle += 1
            deep = cycle % self.MAPSET_POLL_CYCLES == 0
            for key, c in list(self.commanders.items()):  # snapshot: concurrent adoptions
                r = self.robots.get(key)
                if r is None:
                    continue
                try:
                    if r.did and r.did.protocol == "mqtt":
                        await self._poll_mqtt_robot(key, c, deep)
                    else:
                        self._poll_xmpp_robot(c, deep)
                except Exception:  # noqa: BLE001
                    log.exception("poll failed for %s", key)
            # positions/mapsets change without touching the map: save anyway
            # (the fingerprint avoids redundant writes)
            self._save_state()
            await asyncio.sleep(POLL_SECONDS)

    def _poll_xmpp_robot(self, c, deep: bool = False) -> None:
        c.set_map_autoreport(True)  # the app keeps re-arming it
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
        if deep:  # walls/zones + stats (less frequent)
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
        if deep:  # rooms + walls/zones + stats (less frequent)
            c.get_map_set("ar")
            c.get_map_set("vw")
            c.get_map_set("mw")
            c.get_stats()

    # ---------- map walks ----------

    def _commander_for_map(self, map_id: str):
        """Commander of the robot owning the map (B6): multi-robot walks
        must not go out through whichever protocol's commander is first."""
        owner = self._map_owner.get(map_id)
        c = self.commanders.get(owner) if owner else None
        if c is not None:
            return c
        # no known owner → first commander of the matching protocol
        is_mqtt = ":" in map_id  # eco-ng mids carry the sn:mid namespace
        for key, cmd in self.commanders.items():
            r = self.robots.get(key)
            if r and r.did and (r.did.protocol == "mqtt") == is_mqtt:
                return cmd
        return None

    async def _minor_walk(self, map_id: str) -> None:
        """Requests getMinorMap for the pieces whose CRC changed (eco-ng robots).

        One queue per map (B7): if a walk arrives while another is running,
        it stays pending and runs afterwards — nothing is dropped.
        The wire mid is the RAW one: the sn: namespace is internal only (B2).
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
                log.info("minor walk map %s: %d pieces to request", map_id, len(missing))
                for pid in missing:
                    try:
                        c.get_minor_map(pid, mid=raw_mid)
                    except Exception:  # noqa: BLE001
                        log.exception("getMinorMap %d failed", pid)
                    await asyncio.sleep(0.25)
        finally:
            self._walking = False

    async def _pull_walk(self, map_id: str) -> None:
        """Re-fetches pieces emitted before startup: PullMP 0..63 (like the app)."""
        c = self._commander_for_map(map_id)
        if not c:
            return
        log.info("pull walk map %s", map_id)
        for pid in range(64):
            try:
                c.pull_map_piece(pid)
            except Exception:  # noqa: BLE001
                return
            await asyncio.sleep(0.25)

    # ---------- embedded servers (Bumper absorption, Phase F) ----------

    async def _start_embedded_servers(self) -> None:
        """Starts ONLY the servers enabled in [servers].

        Each absorption phase adds its own; the not-yet-implemented ones
        warn instead of failing silently.
        """
        srv = self.cfg.servers
        from .servers import NOT_YET

        for name, phase in NOT_YET.items():
            if getattr(srv, name):
                log.warning("embedded server '%s' arrives in phase %s — ignored",
                            name, phase)

        # TLS material for the embedded servers (FE): on a clean install
        # CA+cert are generated on first boot; existing ones are NEVER
        # touched (robots already trust the production CA)
        if srv.xmpp or srv.mqtt or srv.conf:
            from .servers.certs import ensure_certs

            if not ensure_certs(srv.ca_file, srv.server_cert, srv.server_key,
                                cn=srv.announce_ip or "bumper"):
                log.error("no usable TLS certs — embedded XMPP/MQTT/conf "
                          "servers disabled for this boot (clients keep "
                          "their external config)")
                srv.xmpp = srv.mqtt = srv.conf = False
                if srv.lookup:
                    # do not announce a dead XMPP: without certs there is
                    # no server for lookup to send the robot to
                    log.error("embedded lookup disabled too "
                              "(it would announce a nonexistent XMPP)")
                    srv.lookup = False

        # unified bots/clients registry: shared by XMPP (FB), the MQTT
        # broker (FC) and the app conf (FD, GetDeviceList) — persisted
        # next to state_file (if any)
        if srv.xmpp or srv.mqtt or srv.conf:
            from .servers.registry import DeviceRegistry

            reg_path = None
            if self.cfg.state_file:
                from pathlib import Path as _P

                reg_path = str(_P(self.cfg.state_file).parent / "registry.json")
            self.registry = DeviceRegistry(persist_path=reg_path)
            self.registry.load()

        if srv.lookup:
            from .servers.lookup import LookupServer

            try:
                lookup = LookupServer(announce_ip=srv.announce_ip,
                                      xmpp_port=srv.xmpp_port,
                                      listen=srv.listen, port=srv.lookup_port)
                await lookup.start()
            except Exception:  # noqa: BLE001  (empty announce, busy port…)
                log.exception("embedded lookup failed to start — the robot "
                              "will not find the XMPP until fixed "
                              "(announce_ip=%r port=%d)",
                              srv.announce_ip, srv.lookup_port)
            else:
                self.embedded["lookup"] = lookup

        if srv.xmpp:
            from .servers.xmpp import XMPPServer

            xmpp = XMPPServer((srv.listen, srv.xmpp_port), self.registry,
                              server_cert=srv.server_cert, server_key=srv.server_key,
                              ca_cert=srv.ca_file)
            await xmpp.start_async_server()
            if xmpp.started:
                self.embedded["xmpp"] = xmpp
                # the bridge's XMPP client talks to OUR server (loopback)
                b = self.cfg.bumper
                if b.host not in ("127.0.0.1", "localhost"):
                    log.info("embedded xmpp active: XMPP client → 127.0.0.1:%d "
                             "([bumper] host=%s config ignored)", srv.xmpp_port, b.host)
                # the cert the server presents is ours (same rule as the
                # MQTT branch: keep the ca consistent even if the config
                # points elsewhere)
                b.host, b.port, b.ca_file = "127.0.0.1", srv.xmpp_port, srv.ca_file
            else:
                log.error("embedded XMPP did not start — the client keeps "
                          "its external [bumper] config")

        if srv.mqtt:
            from .servers.mqtt import EmbeddedMqttBroker

            try:
                broker = EmbeddedMqttBroker(
                    (srv.listen, srv.mqtt_port), self.registry,
                    server_cert=srv.server_cert, server_key=srv.server_key)
                await broker.start()
            except Exception:  # noqa: BLE001  (missing certs, busy port…)
                # degrade WITHOUT returning: conf (or whatever comes next)
                # must still start (FA-FE per-server degradation pattern)
                broker = None
                log.exception("embedded MQTT broker failed to start")
            if broker is not None:
                self.embedded["mqtt"] = broker
                # the bridge's eco-ng client talks to OUR broker (loopback)
                bm = self.cfg.bumper_mqtt
                if bm.enabled:
                    if (bm.host, bm.port) != ("127.0.0.1", srv.mqtt_port):
                        log.info("embedded eco-ng mqtt active: [bumper_mqtt] client → "
                                 "127.0.0.1:%d (%s:%d config ignored)",
                                 srv.mqtt_port, bm.host, bm.port)
                    bm.host, bm.port = "127.0.0.1", srv.mqtt_port
                    # the cert the broker presents is ours (ServersConfig)
                    bm.ca_file = srv.ca_file
                    # explicit TLS: the port heuristic (8883) would break if
                    # mqtt_port is changed to coexist with an external Bumper
                    bm.tls = True
                else:
                    log.warning("eco-ng broker on :%d but [bumper_mqtt] enabled=false "
                                "— no T8+ client", srv.mqtt_port)

        if srv.conf:
            from .servers.conf import ConfServer

            try:
                conf = ConfServer(self.registry,
                                  announce_ip=srv.announce_ip,
                                  xmpp_port=srv.xmpp_port,
                                  listen=srv.listen, port=srv.conf_port,
                                  server_cert=srv.server_cert,
                                  server_key=srv.server_key)
                await conf.start()
            except Exception:  # noqa: BLE001  (unreadable certs, 443 busy…)
                log.exception("embedded conf (:443, app API) failed to "
                              "start — the app cannot log in against "
                              "the bridge (the other servers continue)")
            else:
                self.embedded["conf"] = conf

    # ---------- main loop ----------

    async def run(self) -> None:
        b = self.cfg.bumper
        try:
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

            # MQTT transport (eco-ng T8+ robots) — optional. If the broker
            # does not answer at startup we do NOT take down the serve (B5):
            # listen() retries and send_command degrades to {"ret":"fail"}.
            bm = self.cfg.bumper_mqtt
            if bm.enabled:
                from .mqtt_transport import MqttTransport

                self.mqtt_transport = MqttTransport(
                    bm.host, bm.port, ca_file=bm.ca_file, on_data=self.on_report,
                    tls=bm.tls)
                try:
                    await self.mqtt_transport.connect()
                except Exception:  # noqa: BLE001
                    log.exception("eco-ng broker did not connect at startup — retrying in the background")
                tasks.append(self.mqtt_transport.listen())
                # static known robots (asleep ones emit no reports)
                for key in bm.robots:
                    if "ecorobot.net" in key or "/" not in key.split("@")[-1]:
                        log.error("bumper_mqtt.robots espera did@class/res, no JIDs: %r", key)
                        continue
                    if key not in self.robots:
                        try:
                            await self._adopt_and_discover(key, mqtt=True)
                        except Exception:  # noqa: BLE001
                            log.exception("static adoption failed for %s", key)

            # load map from Bumper's log (survives serve restarts)
            self._load_map_from_log()
            # restore persisted state (pieces + positions): the map is
            # published without depending on the robot
            self._load_state()

            # initial adoption of known robots: a docked robot emits no
            # frames on its own — the initial probe (with OUR jid as from)
            # forces replies that reach this session
            known = [j for j in [self.jid_hint, *self.cfg.robots] if j]
            for jid in dict.fromkeys(known):  # no duplicates, stable order
                if jid in self.robots:
                    continue
                try:
                    await self._adopt_and_discover(jid, mqtt=False)
                except Exception:  # noqa: BLE001
                    log.exception("initial adoption of %s failed", jid)

            await self.panel.start()
            log.info("serve up (poll every %ds, panel %s, eco-ng mqtt %s, embedded: %s)",
                     POLL_SECONDS,
                     f":{self.panel.port}" if self.cfg.servers.panel else "off",
                     "on" if bm.enabled else "off",
                     ",".join(self.embedded) or "none")
            await asyncio.gather(*tasks)
        finally:
            # graceful shutdown even when startup fails halfway (e.g.
            # external XMPP down with the embedded broker already up)
            await self._stop_embedded_servers()

    async def _stop_embedded_servers(self) -> None:
        """Graceful shutdown of the embedded servers (reverse order)."""
        for name, srv in reversed(list(self.embedded.items())):
            try:
                if name == "xmpp":
                    srv.disconnect()  # type: ignore[attr-defined]
                elif hasattr(srv, "stop"):
                    await srv.stop()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                log.exception("stopping embedded %s failed", name)
        self.embedded.clear()

    def _load_map_from_log(self) -> None:
        """Loads the latest MapP pieces from Bumper's log at startup.
        No CRC validation (that happens live) — last version per pid."""
        import re
        from pathlib import Path

        log_path = Path(__file__).parent.parent.parent / "data" / "captures" / "pairing-session.log"
        if not log_path.exists():
            log.debug("no Bumper log to preload the map from")
            return

        # pass 1: most recent spec (to know the current map)
        latest_mapid = None
        for line in open(log_path, errors="replace"):
            m = re.search(r'<ctl c="\d+" h="\d+" i="(\d+)" id="[0-9a-f]+" m="[^"]+" p="\d+" r="\d+" w="\d+"', line)
            if m:
                latest_mapid = m.group(1)
        if not latest_mapid:
            return

        # pass 2: last piece per pid of the current map
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
                decode_piece(m.group(2))  # validate it decodes
                best[pid] = m.group(2)  # keep the last one
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
            log.info("preloaded %d pieces of map %s", loaded, latest_mapid)
            self.asm.version += 1
        else:
            log.warning("preload: 0 valid pieces for map %s", latest_mapid)

    # ---------- persistence ----------

    def _save_state(self) -> None:
        """Pieces per map_id + geometry + positions → atomic JSON.

        The map survives bridge restarts without depending on the robot
        (a docked robot pushes no pieces) and the panel/HA paint it right
        after boot. Only writes when something changed (fingerprint).
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
            log.debug("state persisted to %s (%d maps)", path, len(doc["maps"]))
        except Exception:  # noqa: BLE001
            log.exception("state persistence failed")

    def _load_state(self) -> None:
        """Restores persisted state: the map is published on the publisher's
        first cycle (version != -1) without waiting for the robot."""
        if not self.cfg.state_file or not Path(self.cfg.state_file).exists():
            return
        try:
            doc = json.loads(Path(self.cfg.state_file).read_text())
        except (json.JSONDecodeError, OSError):
            log.exception("persisted state unreadable — ignored")
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
        self.asm.version = int(doc.get("version", 0)) + 1  # force publication
        self._map_owner.update(doc.get("map_owner") or {})
        self._seed_positions = doc.get("positions") or {}
        if restored:
            log.info("state restored: %d pieces in %d maps (from %s)",
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
        """The robot's own map; fallback with protocol affinity.

        eco-ng mids carry the namespace ("sn:mid", contain ':'), XMPP ones
        stay as-is — the 930's map preloaded from the log (no known owner)
        thus never gets confused with an eco-ng's.
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
        """Publishes each robot's map whenever its content changes."""
        last_version, last_pub, last_walk = -1, 0.0, 0.0
        walking = False
        while True:
            await asyncio.sleep(5)
            now = time.monotonic()

            # XMPP re-walk: refill pieces every 60s. Knowing the spec
            # (crcs) is enough even with no pieces yet — without this,
            # deadlock: no pieces → no walk, and no walk → no pieces ever.
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
                            last_version = -1  # force republication after each walk

                    asyncio.create_task(walk())

            # publish when the map content changed (not just when it grows)
            if self.asm.version != last_version and now - last_pub > 10 and self.asm.maps:
                last_version, last_pub = self.asm.version, now
                try:
                    # (O1) assemble each map ONCE and reuse it for the panel
                    # and the PNG renders (avoids 2 grids of 640k cells/robot)
                    grids = {mid: self.asm.assemble(mid)
                             for mid in self.asm.maps if self.asm.piece_count(mid)}
                    # map to the panel (WebSocket canvas, one per robot)
                    self.panel.build_and_broadcast_map(grids)
                    # map to HA (PNG via MQTT) — each robot with ITS map
                    for key, r in list(self.robots.items()):
                        map_id = self._robot_map_id(key)
                        grid = grids.get(map_id) if map_id else None
                        if grid:
                            await self.pub.publish_map(r.slug,
                                                       self._render_map_png(map_id, r, grid))
                    log.debug("maps published (v%d, %d pieces in %d maps)",
                              self.asm.version,
                              sum(self.asm.piece_count(m) for m in self.asm.maps),
                              len(self.asm.maps))
                    self._save_state()  # persist current pieces + positions
                except Exception:  # noqa: BLE001
                    log.exception("map publication failed")
