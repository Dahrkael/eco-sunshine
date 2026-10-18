"""H1 tests: new HA entities (stats/fw/water select/room buttons) + dispatch."""

from __future__ import annotations

import json

from ecovacs_bridge.mqtt_pub import MqttPublisher


class FakeClient:
    def __init__(self):
        self.published: list[tuple[str, str, bool]] = []

    async def publish(self, topic, payload, retain=False, qos=0):
        self.published.append((topic, payload if isinstance(payload, str)
                               else payload.decode(), retain))


def make_pub() -> tuple[MqttPublisher, FakeClient]:
    pub = MqttPublisher("h", 1)
    fc = FakeClient()
    pub.client = fc
    return pub, fc


def topics(pub: FakeClient) -> dict[str, str]:
    return {t: p for t, p, _ in pub.published}


class TestDiscoveryH1:
    async def test_stats_fw_and_water(self):
        pub, fc = make_pub()
        await pub.publish_discovery("bot", "Deebot", "model", has_water=True)
        ts = topics(fc)
        assert "homeassistant/sensor/bot_fw/config" in ts
        for k in ("total_time", "total_area", "total_count"):
            cfg = json.loads(ts[f"homeassistant/sensor/bot_{k}/config"])
            assert cfg["state_topic"] == f"deebot/bot/stats/{k}"
        sel = json.loads(ts["homeassistant/select/bot_water/config"])
        assert sel["options"] == ["low", "medium", "high"]
        assert sel["command_topic"] == "deebot/bot/water/set"
        assert sel["optimistic"] is True

    async def test_no_water_if_model_does_not_mop(self):
        pub, fc = make_pub()
        await pub.publish_discovery("bot", "Deebot", has_water=False)
        assert all("/select/" not in t for t in topics(fc))

    async def test_fan_speeds_per_model(self):
        pub, fc = make_pub()
        await pub.publish_discovery("bot", fan_speeds=["silent", "standard"])
        vac = json.loads(topics(fc)["homeassistant/vacuum/bot/config"])
        assert vac["fan_speed_list"] == ["silent", "standard"]

    async def test_room_buttons_idempotent(self):
        pub, fc = make_pub()
        rooms = [{"mssid": "1", "name": "Living"}, {"mssid": "2", "name": "Kitchen"}]
        await pub.publish_room_buttons("bot", "Deebot", "Deebot OZMO 930", rooms)
        ts = topics(fc)
        b1 = json.loads(ts["homeassistant/button/bot_room_1/config"])
        assert b1["payload_press"] == "1"
        assert b1["command_topic"] == "deebot/bot/room"
        assert "homeassistant/button/bot_room_2/config" in ts
        # empty list publishes nothing
        n = len(fc.published)
        await pub.publish_room_buttons("bot", "Deebot", "Deebot OZMO 930", [])
        assert len(fc.published) == n


class TestPublishersH1:
    async def test_stats_and_fw_topics(self):
        pub, fc = make_pub()
        await pub.publish_fw("bot", "1.9.9")
        await pub.publish_stats("bot", {"total_time": 3600,
                                        "total_area": 45_000_000,
                                        "total_count": 12})
        ts = topics(fc)
        assert ts["deebot/bot/fw"] == "1.9.9"
        assert ts["deebot/bot/stats/total_time"] == "3600"
        assert ts["deebot/bot/stats/total_area"] == "45000000"
        assert ts["deebot/bot/stats/total_count"] == "12"


class TestDispatchH1:
    async def test_water_and_room_route(self):
        pub, _ = make_pub()
        seen = []

        async def on_cmd(slug, action, val):
            seen.append((slug, action, val))

        pub.on_command = on_cmd

        class M:
            topic = "deebot/bot/water/set"
            payload = b"high"

        await pub._dispatch(M())
        class M2:
            topic = "deebot/bot/room"
            payload = b"2"

        await pub._dispatch(M2())
        assert ("bot", "set_water_level", "high") in seen
        assert ("bot", "clean_room", "2") in seen


class TestServeWiringH1:
    async def test_room_buttons_published_on_learning_rooms(self):
        from ecovacs_bridge.config import Config
        from ecovacs_bridge.serve import Service
        from tests.test_serve_wiring import FakeTransport

        svc = Service(Config(), None)
        pub, fc = make_pub()
        svc.pub = pub  # type: ignore[assignment]
        t = FakeTransport()
        svc.mqtt_transport = t
        from ecovacs_bridge.models import DeviceId
        from ecovacs_bridge.robot import RobotState
        from ecovacs_bridge.mqtt_commander import MqttCommander

        r = RobotState.from_device_id(DeviceId.from_key("x@yna5xh/axm"))
        key = r.did.key
        svc.robots[key] = r
        svc.commanders[key] = MqttCommander(t, r)

        # first mapset with rooms → buttons published
        await svc.on_report(key, "getMapSet", {
            "ret": "ok", "type": "ar", "msid": "0",
            "subsets": [{"mssid": "1", "name": "Living", "center": "0,0"}]})
        ts = topics(fc)
        assert f"homeassistant/button/{r.slug}_room_1/config" in ts
        # the device keeps the real model (not degraded to "Deebot")
        cfg = json.loads(ts[f"homeassistant/button/{r.slug}_room_1/config"])
        assert cfg["device"]["model"] == "Deebot OZMO T9 AIVI via ecovacs-bridge"
        n = len(fc.published)
        # same rooms again → no republication (signature unchanged)
        await svc.on_report(key, "getMapSet", {
            "ret": "ok", "type": "ar", "msid": "0",
            "subsets": [{"mssid": "1", "name": "Living", "center": "0,0"}]})
        assert len(fc.published) == n


class TestMapDataTopic:
    async def test_publish_map_data_retained(self):
        pub, fc = make_pub()
        payload = {"type": "map", "w": 4, "h": 3, "rows": ["0000", "0110", "0000"],
                   "cell_mm": 50, "origin_r": 250, "origin_c": 200,
                   "crop_r": 0, "crop_c": 0, "y_down": False}
        await pub.publish_map_data("bot", payload)
        topic, body, retain = fc.published[-1]
        assert topic == "deebot/bot/map_data"
        assert retain is True
        import json as j
        assert j.loads(body)["w"] == 4
        # None publishes nothing
        n = len(fc.published)
        await pub.publish_map_data("bot", None)
        assert len(fc.published) == n


class TestReviewRegressions:
    async def test_set_water_level_maps_levels(self):
        from ecovacs_bridge.config import Config
        from ecovacs_bridge.serve import Service
        from tests.test_serve_wiring import FakeTransport
        from ecovacs_bridge.models import DeviceId
        from ecovacs_bridge.robot import RobotState
        from ecovacs_bridge.mqtt_commander import MqttCommander

        svc = Service(Config(), None)
        t = FakeTransport()
        svc.mqtt_transport = t
        r = RobotState.from_device_id(DeviceId.from_key("x@yna5xh/axm"))
        svc.robots[r.did.key] = r
        svc.commanders[r.did.key] = MqttCommander(t, r)
        svc.pub = None  # on_command publishes: without a client nothing is published
        pub_calls = []

        class _NoPub:
            async def publish_state(self, *a, **k): pass
        svc.pub = _NoPub()
        await svc.on_command(r.slug, "set_water_level", "high")
        cmd = [x for x in t.sent if x[0] == "setWaterInfo"][-1]
        assert cmd[2]["amount"] == 3
        await svc.on_command(r.slug, "set_water_level", "low")
        cmd = [x for x in t.sent if x[0] == "setWaterInfo"][-1]
        assert cmd[2]["amount"] == 1

    def test_null_panel_build_map_data_noop(self):
        from ecovacs_bridge.webpanel import NullPanel

        assert NullPanel()._build_map_data("slug", "0", None) is None

    async def test_rename_room_republishes_button(self):
        from ecovacs_bridge.config import Config
        from ecovacs_bridge.serve import Service
        from tests.test_ha_entities import make_pub, topics
        from tests.test_serve_wiring import FakeTransport
        from ecovacs_bridge.models import DeviceId
        from ecovacs_bridge.robot import RobotState
        from ecovacs_bridge.mqtt_commander import MqttCommander

        svc = Service(Config(), None)
        pub, fc = make_pub()
        svc.pub = pub
        t = FakeTransport()
        svc.mqtt_transport = t
        r = RobotState.from_device_id(DeviceId.from_key("x@yna5xh/axm"))
        key = r.did.key
        svc.robots[key] = r
        svc.commanders[key] = MqttCommander(t, r)

        async def send(name):
            svc._last_publish[key] = 0  # beat the 2s state throttle
            await svc.on_report(key, "getMapSet", {
                "ret": "ok", "type": "ar", "msid": "0",
                "subsets": [{"mssid": "1", "name": name, "center": "0,0"}]})

        def n_room_pubs():
            return sum(1 for t, _, _ in fc.published if "_room_" in t)

        await send("Living")
        assert n_room_pubs() == 1
        await send("Living")   # same → no republication
        assert n_room_pubs() == 1
        await send("Hall")     # renamed → republished with new label
        assert n_room_pubs() == 2
        cfg = json.loads(topics(fc)[f"homeassistant/button/{r.slug}_room_1/config"])
        assert "Hall" in cfg["name"]
