"""Estado por robot + vocabulario de comandos (validado contra corpus del 930)."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# ---- comandos (contenido interno del <ctl>) ----
CMD_CLEAN_START = '<clean act="s" speed="standard" type="auto"/>'
CMD_CLEAN_PAUSE = '<clean act="p" speed="standard" type="auto"/>'
CMD_CHARGE_GO = '<charge type="go"/>'
CMD_GET_POS = None  # sin hijos: usar send_ctl_td
CMD_SET_MAP_AUTOREPORT = None


@dataclass
class Position:
    x: float  # mm
    y: float
    angle: float


@dataclass
class RobotState:
    jid: str
    slug: str
    status: str = "unknown"  # docked/charging/cleaning/paused/returning/error/idle
    battery: int | None = None
    position: Position | None = None
    trace_points: list[tuple[float, float]] = field(default_factory=list)
    map_pieces: list[bytes] = field(default_factory=list)
    lifespans: dict[str, dict] = field(default_factory=dict)

    @staticmethod
    def from_jid(jid: str) -> "RobotState":
        serial = jid.split("@")[0]
        return RobotState(jid=jid, slug=f"deebot-{serial[-6:].lower()}")

    # ---- ingesta de eventos (pushes td=... y respuestas correladas) ----

    def update(self, attrs: dict, inner: str) -> None:
        td = attrs.get("td", "")
        # posicion: push td=Pos o respuesta a GetPos (mismos attrs a/p/valid)
        if "p" in attrs and "valid" in attrs and "," in attrs["p"]:
            try:
                x, y = (float(v) for v in attrs["p"].split(","))
                self.position = Position(x, y, float(attrs.get("a", 0)))
                if len(self.trace_points) < 100_000:
                    self.trace_points.append((x, y))
            except ValueError:
                pass
        # consumibles: respuesta GetLifeSpan
        if "left" in attrs and "total" in attrs:
            self.lifespans[attrs.get("type", "?")] = {
                "left": int(attrs["left"]), "total": int(attrs["total"])
            }
        if td in ("CleanSt", "CleanReport", "GetCleanState"):
            st = attrs.get("st")
            if not st:
                m = re.search(r'st="([a-z])"', inner)
                st = m.group(1) if m else None
            if st:
                self.status = self._map_clean_status({"st": st}, inner)
        elif td in ("ChargeState", "GetChargeState") or "SlotCharging" in inner:
            if "SlotCharging" in inner or "WireCharging" in inner:
                self.status = "charging"
            elif "Going" in inner:
                self.status = "returning"
            elif "Idle" in inner and self.status not in ("cleaning", "paused"):
                self.status = "idle"
        elif td == "BatteryInfo" or any("battery" in k.lower() for k in attrs):
            for k, v in attrs.items():
                if "battery" in k.lower() or k == "power":
                    try:
                        self.battery = int(str(v).rstrip("%"))
                    except ValueError:
                        pass

    @staticmethod
    def _map_clean_status(attrs: dict, inner: str) -> str:
        st = attrs.get("st", inner)
        # 'st': h=home/dock, i=idle, p=paused, c/g cleaning variants (930 empirico)
        low = st if isinstance(st, str) else ""
        if "charg" in inner or "charg" in low:
            return "charging"
        return {
            "h": "docked",
            "i": "idle",
            "p": "paused",
        }.get(low[:1], "cleaning" if low else "idle")


class RobotCommander:
    """Envuelve el cliente XMPP con el vocabulario del 930."""

    def __init__(self, client, robot: RobotState):
        self.client = client
        self.robot = robot

    def start_clean(self) -> str:
        return self.client.send_ctl(self.robot.jid, CMD_CLEAN_START, td="Clean")

    def pause(self) -> str:
        return self.client.send_ctl(self.robot.jid, CMD_CLEAN_PAUSE, td="Clean")

    def dock(self) -> str:
        return self.client.send_ctl(self.robot.jid, CMD_CHARGE_GO, td="Charge")

    def get_pos(self) -> str:
        return self.client.send_ctl_td(self.robot.jid, "GetPos")

    def get_clean_state(self) -> str:
        return self.client.send_ctl_td(self.robot.jid, "GetCleanState")

    def get_charge_state(self) -> str:
        return self.client.send_ctl_td(self.robot.jid, "GetChargeState")

    def get_lifespan(self, kind: str = "Brush") -> str:
        return self.client.send_ctl_td(self.robot.jid, "GetLifeSpan", f' type="{kind}"')

    def get_version(self) -> str:
        return self.client.send_ctl_td(self.robot.jid, "GetVersion", ' name="FW"')

    def set_map_autoreport(self, on: bool = True) -> str:
        return self.client.send_ctl_td(self.robot.jid, "SetMapAutoReport", f' on="{int(on)}"')

    def pull_map_piece(self, pid: int) -> str:
        return self.client.send_ctl_td(self.robot.jid, "PullMP", f' pid="{pid}"')

    def get_trace_page(self, page: int) -> str:
        return self.client.send_ctl_td(self.robot.jid, "GetTr", f' tf="0" tt="{page}"')

