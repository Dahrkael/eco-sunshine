"""MqttCommander — JSON commands for eco-ng robots (T8+).

Mirrors RobotCommander's (XMPP) method names so serve.py, the web panel
and HA work with both protocols via duck typing. Commands are fired
without blocking; responses arrive through the transport's on_data and
update RobotState.update_report just like reports.

Vocabulary: docs/mqtt-json-spec.md §4 (Home APK v3.16.1 decompile).
"""

from __future__ import annotations

import logging

from .models import DeviceId
from .mqtt_transport import MqttTransport
from .robot import LIFESPAN_JSON, RobotState, SPEED_JSON_REV

log = logging.getLogger(__name__)

# HA clean type → eco-ng content.type
CLEAN_TYPE_JSON = {"auto": "auto", "spot": "spotArea", "spot_area": "spotArea",
                   "custom_area": "customArea", "single_room": "spotArea"}


# reset/getLifeSpan: HA/930 name → eco-ng component (spec §4.2)
LIFESPAN_CMD_JSON = {"Brush": "brush", "SideBrush": "sideBrush",
                     "DustCaseHeap": "unitCare", "AirFilter": "heap",
                     "IonSterilize": "dModule",
                     # tolerant of lowercase from HA/panel
                     **{k.lower(): v for k, v in {
                         "Brush": "brush", "SideBrush": "sideBrush",
                         "DustCaseHeap": "unitCare", "AirFilter": "heap",
                         "IonSterilize": "dModule"}.items()}}


class MqttCommander:
    """Mirror API of RobotCommander on top of Bumper's MQTT transport."""

    def __init__(self, transport: MqttTransport, robot: RobotState):
        self.transport = transport
        self.robot = robot
        self.did: DeviceId = robot.did  # type: ignore[assignment]
        self.mid = "0"  # active map (updated by getMajorMap)

    # ---------- sending ----------

    def _fire(self, cmd: str, params: dict | list | None = None) -> None:
        self.transport.fire(self.did, cmd, params)

    async def cmd(self, name: str, params: dict | list | None = None,
                  timeout: float = 10.0) -> dict:
        """Sends and awaits the response (for flows that need it)."""
        return await self.transport.send_command(self.did, name, params, timeout)

    # ---- cleaning ----

    def start_clean(self, speed: str = "standard", clean_type: str = "auto",
                    value: str = "") -> None:
        if speed in SPEED_JSON_REV:
            self._fire("setSpeed", {"speed": SPEED_JSON_REV[speed]})
        self._fire("clean_V2", {
            "act": "start", "mode": "auto", "router": "",
            "content": {"type": CLEAN_TYPE_JSON.get(clean_type, "auto"),
                        "value": value, "count": 1, "donotClean": 0},
        })

    def pause(self) -> None:
        self._fire("clean_V2", {"act": "pause", "mode": "auto"})

    def resume(self) -> None:
        self._fire("clean_V2", {"act": "resume", "mode": "auto"})

    def stop(self) -> None:
        self._fire("clean_V2", {"act": "stop", "mode": "auto"})

    def clean_area(self, value: str, speed: str = "standard") -> None:
        self.start_clean(speed=speed, clean_type="custom_area", value=value)

    # ---- movement ----

    def dock(self) -> None:
        self._fire("charge", {"act": "go"})

    def move(self, action: str) -> None:
        # remote control: {"act": "turn", "a": 30} | {"act": "forward"}
        if action in ("forward", "backward", "left", "right"):
            self._fire("move", {"act": action})
        else:
            self._fire("move", {"act": "turn", "a": int(action)})

    # ---- sound ----

    def play_sound(self, sid: int = 30) -> None:
        self._fire("playSound", {"sid": sid, "count": 1})

    # ---- queries ----

    def get_pos(self) -> None:
        self._fire("getPos_V2", ["chargePos", "deebotPos"])

    def get_clean_state(self) -> None:
        self._fire("getCleanInfo_V2")

    def get_charge_state(self) -> None:
        self._fire("getChargeState")

    def get_battery_info(self) -> None:
        self._fire("getBattery")

    def get_charger_pos(self) -> None:
        self._fire("getPos_V2", ["chargePos", "deebotPos"])

    def get_error(self) -> None:
        self._fire("getError")

    def get_version(self) -> None:
        self._fire("getNetInfo")

    def get_life_span(self, kind: str = "Brush") -> None:
        # a single query with all components (single response)
        self._fire("getLifeSpan", list(LIFESPAN_JSON.values()))

    def reset_life_span(self, kind: str = "Brush") -> None:
        self._fire("resetLifeSpan", {"component": LIFESPAN_CMD_JSON.get(kind, "brush")})

    # ---- map ----

    def get_major_map(self) -> None:
        self._fire("getMajorMap")

    def get_map_info(self, mtype: str = "ol") -> None:
        """Chunked map (T8/N8): the robot replies with N getMapInfo chunks."""
        self._fire("getMapInfo", {"type": mtype})

    def get_minor_map(self, piece_index: int, mid: str = "0", mtype: str = "ol") -> None:
        self._fire("getMinorMap", {"type": mtype, "mid": mid, "pieceIndex": piece_index})

    def get_map_trace(self, start: int = 0, count: int = 400) -> None:
        self._fire("getMapTrace", {"traceStart": start, "pointCount": count})

    def get_map_set(self, tp: str = "vw") -> None:
        # ar=rooms, vw=walls, mw=no-mop zones
        self._fire("getMapSet", {"type": tp, "mid": self.mid})

    def get_map_subset(self, mssid: str, tp: str = "vw", msid: str = "0") -> None:
        """Outline of a subset (wall/zone/room), paginated."""
        self._fire("getMapSubSet", {"mid": self.mid, "msid": msid, "mssid": mssid,
                                    "type": tp, "seq": 0, "count": 50, "seqIndex": 0})

    @staticmethod
    def _fmt_coords(coords) -> str:
        """[x1,y1,x2,y2] → "x1,y1;x2,y2" (point pairs, spec §4.5)."""
        if isinstance(coords, (list, tuple)):
            pts = [f"{int(round(coords[i]))},{int(round(coords[i + 1]))}"
                   for i in range(0, len(coords) - 1, 2)]
            return ";".join(pts)
        return str(coords)

    def clean_room(self, rooms: str | list, mid: str = "0",
                   speed: str = "standard") -> None:
        """Per-room cleaning: clean_V2 spotArea value="mssid1,mssid2"."""
        if isinstance(rooms, (list, tuple)):
            rooms = ",".join(str(r) for r in rooms)
        if speed in SPEED_JSON_REV:
            self._fire("setSpeed", {"speed": SPEED_JSON_REV[speed]})
        self._fire("clean_V2", {
            "act": "start", "mode": "auto", "router": "",
            "content": {"type": "spotArea", "value": str(rooms),
                        "count": 1, "donotClean": 0},
        })

    def add_wall(self, coords) -> None:
        """Virtual wall: coords [x1,y1,x2,y2] mm → setMapSubSet (provisional)."""
        self._fire("setMapSubSet", {"mid": self.mid, "msid": "0", "type": "vw",
                                    "value": self._fmt_coords(coords)})

    def del_wall(self, mssid: str) -> None:
        self._fire("setMapSubSet", {"mid": self.mid, "msid": "0", "mssid": mssid,
                                    "type": "vw", "value": ""})

    def add_zone(self, coords) -> None:
        """No-mop zone: coords [x1,y1,x2,y2] mm."""
        self._fire("setMapSubSet", {"mid": self.mid, "msid": "0", "type": "mw",
                                    "value": self._fmt_coords(coords)})

    def del_zone(self, mssid: str) -> None:
        self._fire("setMapSubSet", {"mid": self.mid, "msid": "0", "mssid": mssid,
                                    "type": "mw", "value": ""})

    def clear_map(self) -> None:
        self._fire("clearMap", {"mid": self.mid})

    def set_relocation(self) -> None:
        self._fire("setRelocationState", {"mode": "manu"})

    # ---- speed / water / volume ----

    def set_clean_speed(self, speed: str) -> None:
        self._fire("setSpeed", {"speed": SPEED_JSON_REV.get(speed, 1)})

    def get_clean_speed(self) -> None:
        self._fire("getSpeed")

    def set_water_permeability(self, level: int) -> None:
        self._fire("setWaterInfo", {"amount": int(level)})

    def get_water_permeability(self) -> None:
        self._fire("getWaterInfo")

    def get_volume(self) -> None:
        self._fire("getVolume")

    def set_volume(self, vol: int) -> None:
        self._fire("setVolume", {"volume": int(vol), "type": "system"})

    # ---- DND / schedules / misc (parity with HA/panel) ----

    def get_block_time(self) -> None:
        self._fire("getBlock")

    def set_block_time_state(self, on: bool,
                             start: str = "22:00", end: str = "08:00") -> None:
        self._fire("setBlock", {"enable": bool(on), "start": start, "end": end})

    def get_sched(self) -> None:
        self._fire("getSched_V2")

    def get_stats(self) -> None:
        self._fire("getTotalStats")

    def get_net_info(self) -> None:
        self._fire("getNetInfo")

    def get_sleep_status(self) -> None:
        self._fire("getSleep")

    def set_on_off(self, t: str, on: bool) -> None:
        # t=g continuous (breakpoint), b=DND → eco-ng uses setBreakPoint/setBlock
        if t == "g":
            self._fire("setBreakPoint", {"enable": bool(on)})
        elif t == "b":
            self.set_block_time_state(on)

    def set_silent_mode_state(self, on: bool) -> None:
        self.set_clean_speed("silent" if on else "standard")

    def factory_reset(self) -> None:
        log.warning("factory_reset not supported on eco-ng — ignored")
