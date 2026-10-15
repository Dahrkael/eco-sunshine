"""Estado + comandos del 930 — vocabulario 1:1 del driver de la app
(DR930.java + EcoRobotProtocol.java decompilados, corpus sesiones 1-3).

Formatos de comando (todos dentro de <iq type=set><query><ctl .../></query></iq>):
  self-closing: <ctl id=".." [attrs] td="CMD" [attrs]/>
  con hijos:     <ctl id=".." td="Clean"><clean .../></ctl>

Robots MQTT (T8+): la ingesta va por update_report() (spec §5) — reports
iot/atr y respuestas p2p comparten forma de payload, así que un solo camino.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from .models import DeviceId, ModelInfo, detect_model, detect_model_for
from .map_sets import (MapSetData, parse_mapset_json, parse_mapset_xml,
                       parse_stats_json, parse_stats_xml, parse_subset_json)

log = logging.getLogger(__name__)

# ---------- enums de la app (valores de wire) ----------

CLEAN_TYPES = {"auto": "auto", "spot_area": "SpotArea", "custom_area": "CustomArea",
               "stop": "stop", "spot": "spot"}
CLEAN_SPEEDS = {"standard": "standard", "silent": "silent", "intelligent": "intelligent",
                "strong": "strong"}
CLEAN_ACTIONS = {"s": "s", "p": "p", "r": "r", "h": "h"}
COMPONENTS = {"brush": "Brush", "sidebrush": "SideBrush", "dustcase": "DustCaseHeap",
              "airfilter": "AirFilter", "ion": "IonSterilize"}
SOUNDS = {"boot": 0, "wheels": 4, "dustbin": 6, "stuck": 5, "ding": 17, "battery_low": 18,
          "here": 30, "brush_tangled": 31, "returning": 63, "paused": 65, "relocating": 55}

# MQTT: nombre JSON del componente (getLifeSpan array / respuesta)
LIFESPAN_JSON = {"brush": "brush", "sidebrush": "sideBrush",
                 "dustcase": "unitCare", "airfilter": "heap", "ion": "dModule"}
# respuesta JSON → clave interna 930 (la que consumen HA y el panel)
LIFESPAN_JSON_INTERNAL = {"brush": "Brush", "sideBrush": "SideBrush",
                          "heap": "AirFilter", "unitCare": "DustCaseHeap",
                          "dModule": "IonSterilize"}
# minutos de vida nominal por componente (para el % de HA)
LIFESPAN_TOTALS = {"brush": 10800, "sideBrush": 7200, "heap": 9000,
                   "unitCare": 10800, "dModule": 10800}
# velocidad numérica eco-ng ↔ nombre HA
SPEED_JSON = {0: "silent", 1: "standard", 2: "strong", 3: "strong"}
# inverso EXPLÍCITO: sin él, el dict-comp haría last-wins y strong→3 (fuera de spec)
SPEED_JSON_REV = {"silent": 0, "standard": 1, "strong": 2}


@dataclass
class Position:
    x: float
    y: float
    angle: float = 0.0


@dataclass
class RobotState:
    jid: str
    slug: str
    model: ModelInfo = field(default_factory=lambda: detect_model(""))
    status: str = "unknown"  # docked/charging/cleaning/paused/returning/error/idle
    battery: int | None = None
    position: Position | None = None
    charger: Position | None = None
    error: str | None = None
    version: str | None = None
    trace_points: list[tuple[float, float]] = field(default_factory=list)
    lifespans: dict[str, dict] = field(default_factory=dict)
    cleaned_map_pieces: dict[str, str] = field(default_factory=dict)
    did: DeviceId | None = None
    clean_seconds: int = 0
    # última velocidad numérica reportada por robots MQTT (onSpeed/getSpeed)
    fan_speed_json: int | None = None
    # muros/zonas/habitaciones por tipo: {"vw": MapSetData, "ar": MapSetData…}
    map_sets: dict[str, MapSetData] = field(default_factory=dict)
    # {total_time: s, total_area: mm², total_count: n}
    stats: dict = field(default_factory=dict)

    @staticmethod
    def from_jid(jid: str) -> "RobotState":
        did = DeviceId.from_jid(jid)
        serial = jid.split("@")[0]
        model = detect_model(jid)
        return RobotState(jid=jid, slug=f"{model.slug}-{serial[-6:].lower()}",
                          model=model, did=did)

    @staticmethod
    def from_device_id(did: DeviceId) -> "RobotState":
        model = detect_model_for(did)
        serial = did.sn
        return RobotState(jid=did.key, slug=f"{model.slug}-{serial[-6:].lower()}",
                          model=model, did=did)

    # ---- ingesta de eventos y respuestas ----

    def update(self, attrs: dict, inner: str) -> None:
        td = attrs.get("td", "")

        # posición: push td=Pos o respuesta GetPos (attrs a/p/t/valid)
        if "p" in attrs and "valid" in attrs and "," in attrs["p"]:
            try:
                x, y = (float(v) for v in attrs["p"].split(","))
                self.position = Position(x, y, float(attrs.get("a", 0)))
                if len(self.trace_points) < 100_000:
                    self.trace_points.append((x, y))
            except ValueError:
                pass

        # base de carga (GetChargerPos con p= en attrs)
        if td == "GetChargerPos" and "p" in attrs and "," in attrs["p"]:
            try:
                x, y = (float(v) for v in attrs["p"].split(","))
                self.charger = Position(x, y, float(attrs.get("a", 0)))
            except ValueError:
                pass

        # consumibles: respuesta GetLifeSpan
        if "left" in attrs and "total" in attrs:
            self.lifespans[attrs.get("type", "?")] = {
                "left": int(attrs["left"]), "total": int(attrs["total"])
            }

        # batería: respuesta GetBatteryInfo (<battery power="N"/>) o push
        if td in ("BatteryInfo", "GetBatteryInfo") or "power" in attrs:
            m = re.search(r'power="(\d+)"', inner) or (
                re.search(r'power="(\d+)"', str(attrs)) if "power" in attrs else None)
            if m:
                self.battery = int(m.group(1))

        # error: respuesta GetError (<error errno=.../> o attrs)
        if td == "GetError" or "errno" in attrs:
            m = re.search(r'errno="(\d+)"', inner) or re.search(r'errno="(\d+)"', str(attrs))
            if m and m.group(1) not in ("0", ""):
                self.error = m.group(1)
                if self.status not in ("cleaning",):
                    self.status = "error"

        # versión firmware: respuesta GetVersion (<FW ver>)
        if td == "GetVersion":
            m = re.search(r'ver="([^"]+)"', inner)
            if m:
                self.version = m.group(1)

        # estado de limpieza
        if td in ("CleanSt", "CleanReport", "GetCleanState"):
            st = attrs.get("st")
            if not st:
                m = re.search(r'st="([a-z])"', inner)
                st = m.group(1) if m else None
            if st:
                self.status = self._map_clean_status({"st": st}, inner)
            # stats: t = tiempo (s), a = area?
            m = re.search(r't="(\d+)"', inner)
            if m:
                self.clean_seconds = int(m.group(1))
        elif td == "ChargeState" or "SlotCharging" in inner or "Going" in inner:
            if "Going" in inner:
                self.status = "returning"
            elif "SlotCharging" in inner or "WireCharging" in inner:
                self.status = "charging"
            elif "Idle" in inner and self.status not in ("cleaning", "paused"):
                self.status = "idle"

        # muros/zonas/habitaciones (respuesta GetMapSet o push)
        if td == "MapSet":
            ms = parse_mapset_xml(attrs, inner)
            if ms is not None:
                self.map_sets[ms.tp] = ms

        # estadísticas (GetCleanStatistics/GetCleanSum — formato tolerante)
        if td in ("GetCleanStatistics", "CleanStatistics", "GetCleanSum", "CleanSum"):
            stats = parse_stats_xml(attrs, inner)
            if stats:
                self.stats.update(stats)

    @staticmethod
    def _map_clean_status(attrs: dict, inner: str) -> str:
        st = attrs.get("st", "")
        if not st:
            m = re.search(r'st="([a-z])"', inner)
            st = m.group(1) if m else ""
        return {
            "h": "docked", "i": "idle", "p": "paused",
        }.get(st[:1], "cleaning" if st else "idle")

    # ---- ingesta de reports/respuestas JSON (robots MQTT, spec §3-§5) ----

    @staticmethod
    def _canon_name(name: str) -> str:
        """onBattery / getBattery / getBattery_V2 / onCleanInfo_V2 → battery / cleanInfo."""
        n = name.removeprefix("on")
        if n[:3].lower() == "get":
            n = n[3:]
        n = n.replace("_V2", "")
        return n[0].lower() + n[1:] if n else name

    def update_report(self, name: str, data: dict) -> None:
        """Aplica un report iot/atr o respuesta p2p JSON al estado.

        Reports (onXxx) y respuestas (getXxx) comparten forma de payload,
        así que se tratan por igual: onBattery≈getBattery, onPos≈getPos_V2…
        """
        if not isinstance(data, dict):
            return
        if data.get("ret") == "fail":
            log.debug("report %s fail: errno=%s", name, data.get("errno"))
            return
        n = self._canon_name(name)

        # batería: {value, isLow}
        if "value" in data and isinstance(data["value"], (int, float)) and (
                n in ("battery",) or "isLow" in data):
            self.battery = int(data["value"])

        # posición: {mid, deebotPos:{x,y,a}, chargePos:[{x,y,a}]}
        pos = data.get("deebotPos")
        if isinstance(pos, dict) and "x" in pos:
            self.position = Position(float(pos["x"]), float(pos["y"]),
                                     float(pos.get("a", 0)))
            if len(self.trace_points) < 100_000:
                self.trace_points.append((self.position.x, self.position.y))
        charge = data.get("chargePos")
        if isinstance(charge, list) and charge and isinstance(charge[0], dict):
            c0 = charge[0]
            self.charger = Position(float(c0.get("x", 0)), float(c0.get("y", 0)),
                                    float(c0.get("a", 0)))
        elif isinstance(charge, dict) and "x" in charge:
            self.charger = Position(float(charge["x"]), float(charge["y"]),
                                    float(charge.get("a", 0)))

        # estado de carga: {mode: slot_charging|idle|going|wire_charging}
        mode = data.get("mode")
        if isinstance(mode, str) and n in ("chargeState",):
            if "going" in mode:
                self.status = "returning"
            elif "charging" in mode:
                self.status = "charging"
            elif mode == "idle" and self.status not in ("cleaning", "paused"):
                self.status = "idle"

        # limpieza: {state, cleanState:{motionState, content{...}}}
        if n in ("cleanInfo",):
            cs = data.get("cleanState") or {}
            motion = cs.get("motionState") or data.get("state")
            if isinstance(motion, str):
                mapped = {"working": "cleaning", "pause": "paused", "paused": "paused",
                          "going": "returning", "returning": "returning",
                          "idle": "idle"}.get(motion)
                if mapped:
                    self.status = mapped
            if isinstance(cs.get("content"), dict):
                total = cs["content"].get("total")
                if isinstance(total, (int, float)):
                    self.clean_seconds = int(total)

        # error: {error: [] | [{errno...}]}
        err = data.get("error")
        if n == "error" and isinstance(err, list):
            if err:
                self.error = ",".join(str(e.get("errno", e) if isinstance(e, dict) else e)
                                      for e in err)
                if self.status not in ("cleaning",):
                    self.status = "error"
            else:
                self.error = None

        # consumibles: {brush: N, sideBrush: N, heap: N, unitCare: N} (min restantes)
        # se guardan con las claves internas 930 (Brush/SideBrush/…) para que
        # HA y el panel funcionen igual con ambos protocolos
        for comp, total in LIFESPAN_TOTALS.items():
            if isinstance(data.get(comp), (int, float)):
                internal = LIFESPAN_JSON_INTERNAL.get(comp, comp)
                self.lifespans[internal] = {"left": int(data[comp]), "total": total}

        # velocidad: {speed: 0-3}
        if isinstance(data.get("speed"), (int, float)) and n == "speed":
            self.fan_speed_json = int(data["speed"])

        # firmware: getNetInfo → wkVer
        if isinstance(data.get("wkVer"), str):
            self.version = data["wkVer"]

        # stats: {time, area, count}
        if n in ("stats", "totalStats"):
            st = parse_stats_json(data)
            if st:
                self.stats.update(st)
                if "total_time" in st:
                    self.clean_seconds = int(st["total_time"])

        # muros/zonas/habitaciones (getMapSet/onMapSet → subsets)
        if n in ("mapSet",) and "subsets" in data:
            ms = parse_mapset_json(data)
            if ms is not None:
                self.map_sets[ms.tp] = ms

        # contorno de un subconjunto (getMapSubSet → value "x,y;x,y;…")
        if n in ("mapSubSet",) and "value" in data:
            parse_subset_json(data, self.map_sets)


class RobotCommander:
    """Comandos 1:1 con los stanzas de la app (corpus + EcoRobotProtocol)."""

    def __init__(self, client, robot: RobotState):
        self.client = client
        self.robot = robot

    def _cmd(self, ctl: str, td: str = "") -> str:
        return self.client.send_command(self.robot.jid, ctl, td)

    # ---- limpieza ----

    def start_clean(self, speed: str = "standard", clean_type: str = "auto") -> str:
        return self._cmd(
            f'<ctl id="{{ctl_id}}" td="Clean"><clean act="s" speed="{speed}" type="{clean_type}" /></ctl>',
            "Clean")

    def pause(self) -> str:
        return self._cmd('<ctl id="{ctl_id}" td="Clean"><clean act="p" speed="standard" type="auto" /></ctl>', "Clean")

    def resume(self) -> str:
        return self._cmd('<ctl id="{ctl_id}" td="Clean"><clean act="r" speed="standard" type="auto" /></ctl>', "Clean")

    def stop(self) -> str:
        return self._cmd('<ctl id="{ctl_id}" td="Clean"><clean act="h" speed="standard" type="auto" /></ctl>', "Clean")

    def clean_area(self, mid: str, positions: str, speed: str = "standard") -> str:
        return self._cmd(
            f'<ctl id="{{ctl_id}}" td="Clean"><clean act="s" speed="{speed}" type="SpotArea" mid="{mid}" p="{positions}" /></ctl>',
            "Clean")

    def clean_custom(self, positions: str, speed: str = "standard") -> str:
        return self._cmd(
            f'<ctl id="{{ctl_id}}" td="Clean"><clean act="s" speed="{speed}" type="CustomArea" p="{positions}" /></ctl>',
            "Clean")

    def clean_room(self, rooms: str | list, mid: str = "0",
                   speed: str = "standard") -> str:
        """Limpieza por habitaciones: SpotArea con p="idx,idx" (driver DR930)."""
        if isinstance(rooms, (list, tuple)):
            rooms = ",".join(str(r) for r in rooms)
        return self._cmd(
            f'<ctl id="{{ctl_id}}" td="Clean"><clean act="s" speed="{speed}" '
            f'type="SpotArea" mid="{mid}" p="{rooms}" /></ctl>',
            "Clean")

    # ---- movimiento ----

    def dock(self) -> str:
        return self._cmd('<ctl id="{ctl_id}" td="Charge"><charge type="go" /></ctl>', "Charge")

    def move(self, action: str) -> str:
        return self._cmd(f'<ctl id="{{ctl_id}}" td="Move"><move act="{action}" /></ctl>', "Move")

    # ---- sonido ----

    def play_sound(self, sid: int = 30) -> str:
        return self._cmd(f'<ctl id="{{ctl_id}}" td="PlaySound" sid="{sid}" />', "PlaySound")

    # ---- consultas ----

    def get_pos(self) -> str:
        return self._cmd('<ctl id="{ctl_id}" td="GetPos" />', "GetPos")

    def get_clean_state(self) -> str:
        return self._cmd('<ctl id="{ctl_id}" td="GetCleanState" />', "GetCleanState")

    def get_charge_state(self) -> str:
        return self._cmd('<ctl id="{ctl_id}" td="GetChargeState" />', "GetChargeState")

    def get_battery_info(self) -> str:
        return self._cmd('<ctl id="{ctl_id}" td="GetBatteryInfo" />', "GetBatteryInfo")

    def get_charger_pos(self) -> str:
        return self._cmd('<ctl id="{ctl_id}" td="GetChargerPos" />', "GetChargerPos")

    def get_error(self) -> str:
        return self._cmd('<ctl id="{ctl_id}" td="GetError" />', "GetError")

    def get_version(self) -> str:
        return self._cmd('<ctl id="{ctl_id}" name="FW" td="GetVersion" />', "GetVersion")

    def get_life_span(self, kind: str = "Brush") -> str:
        return self._cmd(f'<ctl id="{{ctl_id}}" td="GetLifeSpan" type="{kind}" />', "GetLifeSpan")

    def get_more_life_span(self, kind: str) -> str:
        return self._cmd(f'<ctl id="{{ctl_id}}" td="GetMoreLifeSpan" type="{kind}" />', "GetMoreLifeSpan")

    def reset_life_span(self, kind: str = "Brush") -> str:
        return self._cmd(f'<ctl id="{{ctl_id}}" td="ResetLifeSpan" type="{kind}" />', "ResetLifeSpan")

    def get_device_position(self) -> str:
        return self._cmd('<ctl id="{ctl_id}" td="GetDevicePosition" />', "GetDevicePosition")

    # ---- mapa ----

    def get_map_m(self) -> str:
        return self._cmd('<ctl id="{ctl_id}" td="GetMapM" />', "GetMapM")

    def pull_map_piece(self, pid: int) -> str:
        return self._cmd(f'<ctl id="{{ctl_id}}" pid="{pid}" td="PullMP" />', "PullMP")

    def get_map_st(self) -> str:
        return self._cmd('<ctl id="{ctl_id}" td="GetMapSt" />', "GetMapSt")

    def set_map_autoreport(self, on: bool = True) -> str:
        return self._cmd(f'<ctl id="{{ctl_id}}" on="{int(on)}" td="SetMapAutoReport" />', "SetMapAutoReport")

    def get_trace_page(self, tt: int, trid: int = 605959, tf: int = 0) -> str:
        return self._cmd(f'<ctl id="{{ctl_id}}" td="GetTr" tf="{tf}" trid="{trid}" tt="{tt}" />', "GetTr")

    def get_map_set(self, tp: str = "vw") -> str:
        """tp: vw=muros virtuales, sa=zonas, mw=zonas de fregado"""
        return self._cmd(f'<ctl id="{{ctl_id}}" td="GetMapSet" tp="{tp}" />', "GetMapSet")

    # ---- idioma / hora / silent ----

    def get_languages(self) -> str:
        return self._cmd('<ctl id="{ctl_id}" td="GetLanguages" />', "GetLanguages")

    def get_active_lang(self) -> str:
        return self._cmd('<ctl id="{ctl_id}" td="GetActiveLang" />', "GetActiveLang")

    def set_active_lang(self, lang: str) -> str:
        return self._cmd(f'<ctl id="{{ctl_id}}" td="SetActiveLang" lang="{lang}" />', "SetActiveLang")

    def set_time(self, ts: int, tz: int = 60) -> str:
        return self._cmd(f'<ctl id="{{ctl_id}}" td="SetTime" t="{ts}" tz="{tz}" />', "SetTime")

    # ---- DND (block time) / silent ----

    def get_block_time(self) -> str:
        return self._cmd('<ctl id="{ctl_id}" td="GetBlockTime" />', "GetBlockTime")

    def set_block_time_state(self, on: bool) -> str:
        return self._cmd(f'<ctl id="{{ctl_id}}" td="SetBlockTimeState" on="{int(on)}" />', "SetBlockTimeState")

    def get_silent_mode_state(self) -> str:
        return self._cmd('<ctl id="{ctl_id}" td="GetSilentModeState" />', "GetSilentModeState")

    def set_silent_mode_state(self, on: bool) -> str:
        return self._cmd(f'<ctl id="{{ctl_id}}" td="SetSilentModeState" on="{int(on)}" />', "SetSilentModeState")

    # ---- schedules / logs / stats ----

    def get_sched(self) -> str:
        return self._cmd('<ctl id="{ctl_id}" td="GetSched" />', "GetSched")

    def get_clean_logs(self) -> str:
        return self._cmd('<ctl id="{ctl_id}" td="GetCleanLogs" />', "GetCleanLogs")

    def get_clean_statistics(self, typ: str = "all") -> str:
        return self._cmd(f'<ctl id="{{ctl_id}}" td="GetCleanStatistics" type="{typ}" />', "GetCleanStatistics")

    def get_clean_sum(self) -> str:
        return self._cmd('<ctl id="{ctl_id}" td="GetCleanSum" />', "GetCleanSum")

    def get_progress(self) -> str:
        return self._cmd('<ctl id="{ctl_id}" td="GetProgress" />', "GetProgress")

    # ---- mapa wifi / net ----

    def get_map_wifi(self) -> str:
        """Mapa de cobertura WiFi (MapType wa): mismo GetMapM pero tipo wa."""
        return self._cmd('<ctl id="{ctl_id}" tp="wa" td="GetMapM" />', "GetMapM")

    def get_net_info(self) -> str:
        return self._cmd('<ctl id="{ctl_id}" td="GetNetInfo" />', "GetNetInfo")

    # ---- gestión de mapa / multi-piso ----

    def if_have_map(self) -> str:
        return self._cmd('<ctl id="{ctl_id}" td="IfHaveMap" />', "IfHaveMap")

    def clear_map(self) -> str:
        return self._cmd('<ctl id="{ctl_id}" td="ClearMap" all="1" />', "ClearMap")

    def recover_map(self) -> str:
        return self._cmd('<ctl id="{ctl_id}" td="RecoverMap" />', "RecoverMap")

    def build_map(self, action: str = "pp") -> str:
        """pp=preparar, r=reanudar, h=parar"""
        return self._cmd(f'<ctl id="{{ctl_id}}" td="BuildMap" act="{action}" />', "BuildMap")

    def get_build_map_state(self) -> str:
        return self._cmd('<ctl id="{ctl_id}" td="GetBuildMapState" />', "GetBuildMapState")

    def rename_map(self, mid: str, name: str) -> str:
        return self._cmd(f'<ctl id="{{ctl_id}}" td="RenameM" mid="{mid}" n="{name}" />', "RenameM")

    # ---- muros virtuales / zonas ----

    def pull_map_set(self, tp: str, msid: str, mid: str, seq: int = 0) -> str:
        return self._cmd(
            f'<ctl id="{{ctl_id}}" td="PullM" tp="{tp}" msid="{msid}" mid="{mid}" seq="{seq}" />',
            "PullM")

    def add_map_set(self, tp: str, msid: str, name: str, data: str) -> str:
        return self._cmd(
            f'<ctl id="{{ctl_id}}" td="AddM" tp="{tp}" msid="{msid}" n="{name}" m="{data}" />',
            "AddM")

    def del_map_set(self, tp: str, msid: str, mid: str) -> str:
        return self._cmd(
            f'<ctl id="{{ctl_id}}" td="DelM" tp="{tp}" msid="{msid}" mid="{mid}" />',
            "DelM")

    # ---- muros/zonas con nombres unificados (duck typing con MqttCommander) ----

    @staticmethod
    def _fmt_coords(coords) -> str:
        if isinstance(coords, (list, tuple)):
            return ",".join(str(int(round(v))) for v in coords)
        return str(coords)

    def add_wall(self, coords) -> str:
        """Muro virtual: coords [x1,y1,x2,y2] mm (AddM tp=vw)."""
        return self.add_map_set("vw", "0", "", self._fmt_coords(coords))

    def del_wall(self, mid: str) -> str:
        return self.del_map_set("vw", "0", mid)

    def add_zone(self, coords) -> str:
        """Zona no-fregar: coords [x1,y1,x2,y2] mm (AddM tp=mw)."""
        return self.add_map_set("mw", "0", "", self._fmt_coords(coords))

    def del_zone(self, mid: str) -> str:
        return self.del_map_set("mw", "0", mid)

    # ---- volumen / sonido ----

    def get_volume(self) -> str:
        return self._cmd('<ctl id="{ctl_id}" td="GetVolume" />', "GetVolume")

    def set_volume(self, vol: int) -> str:
        return self._cmd(f'<ctl id="{{ctl_id}}" td="SetVolume" vol="{vol}" />', "SetVolume")

    def set_mute(self, on: bool) -> str:
        return self._cmd(f'<ctl id="{{ctl_id}}" td="SetMute" mute="{int(on)}" />', "SetMute")

    # ---- switches (t: g=continuo, b=DND, l=LED, cl=bloqueo infantil) ----

    def set_on_off(self, t: str, on: bool) -> str:
        return self._cmd(f'<ctl id="{{ctl_id}}" td="SetOnOff" t="{t}" on="{int(on)}" />', "SetOnOff")

    def get_on_off(self, t: str) -> str:
        return self._cmd(f'<ctl id="{{ctl_id}}" td="GetOnOff" t="{t}" />', "GetOnOff")

    # ---- velocidad de limpieza directa ----

    def get_clean_speed(self) -> str:
        return self._cmd('<ctl id="{ctl_id}" td="GetCleanSpeed" />', "GetCleanSpeed")

    def set_clean_speed(self, speed: str) -> str:
        return self._cmd(f'<ctl id="{{ctl_id}}" td="SetCleanSpeed" speed="{speed}" />', "SetCleanSpeed")

    # ---- fregado (nivel de agua — el 930 lo soporta via protocolo compartido) ----

    def get_water_permeability(self) -> str:
        return self._cmd('<ctl id="{ctl_id}" td="GetWaterPermeability" />', "GetWaterPermeability")

    def set_water_permeability(self, level: int) -> str:
        """level: 1-4 (bajo/alto)"""
        return self._cmd(f'<ctl id="{{ctl_id}}" td="SetWaterPermeability" v="{level}" />', "SetWaterPermeability")

    def get_water_box_info(self) -> str:
        return self._cmd('<ctl id="{ctl_id}" td="GetWaterBoxInfo" />', "GetWaterBoxInfo")

    # ---- schedules ----

    def add_sched(self, sched_xml: str) -> str:
        return self._cmd(f'<ctl id="{{ctl_id}}" td="AddSched">{sched_xml}</ctl>', "AddSched")

    def del_sched(self, sid: str) -> str:
        return self._cmd(f'<ctl id="{{ctl_id}}" td="DelSched" sid="{sid}" />', "DelSched")

    # ---- misc ----

    def get_time(self) -> str:
        return self._cmd('<ctl id="{ctl_id}" td="GetTime" />', "GetTime")

    def get_sys_info(self) -> str:
        return self._cmd('<ctl id="{ctl_id}" td="GetSysInfo" />', "GetSysInfo")

    def get_sleep_status(self) -> str:
        return self._cmd('<ctl id="{ctl_id}" td="GetSleepStatus" />', "GetSleepStatus")

    def factory_reset(self) -> str:
        return self._cmd('<ctl id="{ctl_id}" td="FactoryReset" />', "FactoryReset")
