"""State and routing tests: DeviceId, update_report (JSON), route_topic."""

from __future__ import annotations

from ecovacs_bridge.models import DeviceId, detect_model_for
from ecovacs_bridge.mqtt_transport import build_envelope, command_topic, route_topic
from ecovacs_bridge.robot import SPEED_JSON_REV, RobotState


class TestDeviceId:
    def test_from_jid(self):
        did = DeviceId.from_jid("E000105701860607AB7@115.ecorobot.net/atom")
        assert did.sn == "E000105701860607AB7"
        assert did.device_class == "115"
        assert did.resource == "atom"
        assert did.protocol == "xmpp"
        assert did.key == "E000105701860607AB7@115/atom"

    def test_from_key_mqtt(self):
        did = DeviceId.from_key("b11fceaf-1234@yna5xh/axm")
        assert did.sn == "b11fceaf-1234"
        assert did.device_class == "yna5xh"
        assert did.protocol == "mqtt"

    def test_model_detection(self):
        assert detect_model_for(DeviceId.from_key("x@yna5xh/axm")).slug == "t9aivi"
        assert detect_model_for(DeviceId.from_key("x@unknown/axm")).protocol == "mqtt"
        assert detect_model_for(
            DeviceId.from_jid("S@115.ecorobot.net/atom")).slug == "ozmo930"


class TestUpdateReport:
    def _robot(self) -> RobotState:
        return RobotState.from_device_id(DeviceId.from_key("sn123456@yna5xh/axm"))

    def test_battery(self):
        r = self._robot()
        r.update_report("onBattery", {"ret": "ok", "value": 85, "isLow": False})
        assert r.battery == 85

    def test_battery_response_get(self):
        """getBattery (p2p response) must update like onBattery does."""
        r = self._robot()
        r.update_report("getBattery", {"ret": "ok", "value": 42, "isLow": True})
        assert r.battery == 42

    def test_pos_and_charger(self):
        r = self._robot()
        r.update_report("onPos", {"ret": "ok", "mid": "0",
                                  "deebotPos": {"x": 500, "y": 1200, "a": 90},
                                  "chargePos": [{"x": 100, "y": 100, "a": 0}]})
        assert r.position is not None
        assert (r.position.x, r.position.y, r.position.angle) == (500, 1200, 90)
        assert r.charger is not None and (r.charger.x, r.charger.y) == (100, 100)
        assert r.trace_points == [(500, 1200)]

    def test_charge_state(self):
        r = self._robot()
        r.update_report("onChargeState", {"ret": "ok", "mode": "slot_charging"})
        assert r.status == "charging"
        r.update_report("onChargeState", {"ret": "ok", "mode": "going"})
        assert r.status == "returning"

    def test_clean_info_v2(self):
        r = self._robot()
        r.update_report("onCleanInfo_V2", {
            "ret": "ok", "state": "clean",
            "cleanState": {"mode": "auto", "motionState": "working",
                           "content": {"type": "auto", "total": 1234}}})
        assert r.status == "cleaning"
        assert r.clean_seconds == 1234

    def test_clean_info_response_get(self):
        """getCleanInfo_V2 (poll response) — bug B3: the get prefix must be
        stripped when normalizing or 4 polls update no state."""
        r = self._robot()
        r.update_report("getCleanInfo_V2", {
            "ret": "ok",
            "cleanState": {"motionState": "working"}})
        assert r.status == "cleaning"

    def test_charge_state_response_get(self):
        r = self._robot()
        r.update_report("getChargeState", {"ret": "ok", "mode": "slot_charging"})
        assert r.status == "charging"

    def test_error_response_get(self):
        r = self._robot()
        r.update_report("getError", {"ret": "ok", "error": [{"errno": 4}]})
        assert r.error == "4" and r.status == "error"

    def test_error_list(self):
        r = self._robot()
        r.update_report("onError", {"ret": "ok", "error": [{"errno": 4}]})
        assert r.error == "4" and r.status == "error"
        r.update_report("onError", {"ret": "ok", "error": []})
        assert r.error is None

    def test_lifespan_json(self):
        r = self._robot()
        r.update_report("getLifeSpan", {"ret": "ok", "brush": 1800,
                                        "sideBrush": 120, "heap": 6200})
        assert r.lifespans["Brush"] == {"left": 1800, "total": 10800}
        assert r.lifespans["AirFilter"]["left"] == 6200
        assert r.lifespans["SideBrush"]["left"] == 120

    def test_netinfo_version(self):
        r = self._robot()
        r.update_report("getNetInfo", {"ret": "ok", "ip": "1.2.3.4",
                                       "wkVer": "0.25.16"})
        assert r.version == "0.25.16"

    def test_fail_response_ignored(self):
        r = self._robot()
        r.battery = 50
        r.update_report("getBattery", {"ret": "fail", "errno": 3})
        assert r.battery == 50  # unchanged

    def test_speed(self):
        r = self._robot()
        r.update_report("onSpeed", {"ret": "ok", "speed": 2})
        assert r.fan_speed_json == 2

    def test_speed_rev_in_spec(self):
        """B4: the inverse must send strong=2 (3 does not exist in the spec)."""
        assert SPEED_JSON_REV == {"silent": 0, "standard": 1, "strong": 2}


class TestRouteTopic:
    def test_report(self):
        # iot/atr/{name}/{sn}/{cls}/{res}/{fmt} — separate levels
        r = route_topic("iot/atr/onBattery/abc123/yna5xh/axm/j")
        assert r == ("report", "abc123@yna5xh/axm", "onBattery", "")

    def test_response(self):
        t = "iot/p2p/getBattery/abc123/yna5xh/axm/bridge/bumper/bridge/p/deadbeef/j"
        r = route_topic(t)
        assert r == ("response", "abc123@yna5xh/axm", "getBattery", "deadbeef")

    def test_request(self):
        t = "iot/p2p/clean_V2/bridge/bumper/bridge/abc/yna5xh/axm/q/ff00/j"
        r = route_topic(t)
        assert r == ("request", "abc@yna5xh/axm", "clean_V2", "ff00")

    def test_ignores_others(self):
        assert route_topic("iot/cfg/x/y/z/j") is None
        assert route_topic("other/topic") is None
        assert route_topic("iot/atr/onPos") is None  # too short


class TestCommandTopic:
    def test_format(self):
        did = DeviceId.from_key("abc@yna5xh/axm")
        t = command_topic(did, "getBattery", "req1")
        assert t == "iot/p2p/getBattery/bridge/bumper/bridge/abc/yna5xh/axm/q/req1/j"
