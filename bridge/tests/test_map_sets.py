"""Tests de map_sets: parsers XML (930) y JSON (eco-ng), stats, comandos."""

from __future__ import annotations

from deebot930_bridge.map_sets import (
    MapSetData,
    Room,
    VirtualWall,
    Zone,
    parse_mapset_json,
    parse_mapset_xml,
    parse_stats_json,
    parse_stats_xml,
)
from deebot930_bridge.models import DeviceId
from deebot930_bridge.robot import RobotState


class TestParseMapSetXml:
    def test_set_vacio_del_corpus(self):
        """El corpus manda <m/> vacíos cuando no hay nada configurado."""
        ms = parse_mapset_xml(
            {"td": "MapSet", "tp": "vw", "msid": "", "ts": "123"},
            '<m mid="" n="" p="" />')
        assert ms is not None and ms.tp == "vw" and ms.entries == []

    def test_muro_con_datos(self):
        ms = parse_mapset_xml(
            {"td": "MapSet", "tp": "vw", "msid": "0"},
            '<m mid="17" n="" p="100,200,800,200" />')
        assert ms is not None and len(ms.entries) == 1
        w = ms.entries[0]
        assert isinstance(w, VirtualWall)
        assert (w.x1, w.y1, w.x2, w.y2) == (100.0, 200.0, 800.0, 200.0)
        assert w.mid == "17"

    def test_zona_no_fregar_por_tipo(self):
        ms = parse_mapset_xml(
            {"td": "MapSet", "tp": "vw", "msid": "0"},
            '<m mid="3" n="" p="0,0,400,400" t="mw" />')
        assert ms is not None
        assert isinstance(ms.entries[0], Zone) and ms.entries[0].kind == "mw"

    def test_habitaciones(self):
        ms = parse_mapset_xml(
            {"td": "MapSet", "tp": "ar", "msid": "1"},
            '<m mid="0" n="Salón" center="250,300" index="0" />'
            '<m mid="1" n="Cocina" center="600,150" index="1" />')
        assert ms is not None and len(ms.entries) == 2
        r0 = ms.entries[0]
        assert isinstance(r0, Room) and r0.name == "Salón" and r0.center == (250.0, 300.0)

    def test_sin_tp_devuelve_none(self):
        assert parse_mapset_xml({"td": "MapSet"}, "") is None


class TestParseStatsXml:
    def test_attrs_planos(self):
        # convención CleanSum (sucks): a=área t=tiempo c=cuenta
        st = parse_stats_xml({"td": "CleanSum", "a": "1234", "t": "567", "c": "8"}, "")
        assert st == {"total_area": 1234.0, "total_time": 567.0, "total_count": 8}

    def test_hijo_statistics(self):
        st = parse_stats_xml(
            {"td": "GetCleanStatistics"},
            '<statistics area="999" time="100" count="3" />')
        assert st["total_area"] == 999.0
        assert st["total_time"] == 100.0
        assert st["total_count"] == 3

    def test_vacio(self):
        assert parse_stats_xml({"td": "GetCleanStatistics"}, "") == {}


class TestParseJson:
    def test_mapset_ar(self):
        ms = parse_mapset_json({
            "ret": "ok", "mid": "0", "msid": "0", "type": "ar", "count": 2,
            "subsets": [
                {"mssid": "1", "name": "Salón", "center": "250,300", "index": 0},
                {"mssid": "2", "name": "Cocina", "center": "600,150", "index": 1},
            ]})
        assert ms is not None and ms.tp == "ar" and len(ms.entries) == 2
        assert ms.entries[0].name == "Salón" and ms.entries[0].center == (250.0, 300.0)

    def test_mapset_vw_pendiente_de_contorno(self):
        ms = parse_mapset_json({
            "ret": "ok", "mid": "0", "msid": "0", "type": "vw", "count": 1,
            "subsets": [{"mssid": "7", "name": "muro pasillo", "index": 0}]})
        assert ms is not None and len(ms.entries) == 1
        assert ms.entries[0]["_pending_outline"] is True

    def test_subset_resuelve_muro(self):
        sets = {"vw": MapSetData(tp="vw", msid="0", entries=[
            {"mssid": "7", "name": "muro", "_pending_outline": True}])}
        from deebot930_bridge.map_sets import parse_subset_json

        parse_subset_json({"ret": "ok", "type": "vw", "mssid": "7",
                           "value": "100,200;800,200"}, sets)
        w = sets["vw"].entries[0]
        assert isinstance(w, VirtualWall)
        assert (w.x1, w.y1, w.x2, w.y2) == (100.0, 200.0, 800.0, 200.0)

    def test_stats_json(self):
        st = parse_stats_json({"ret": "ok", "time": 3600, "area": 45000000,
                               "count": 12})
        assert st == {"total_time": 3600.0, "total_area": 45000000.0,
                      "total_count": 12}


class TestRobotStateIngest:
    def _r(self) -> RobotState:
        return RobotState.from_device_id(DeviceId.from_key("x@yna5xh/axm"))

    def test_update_xml_mapset(self):
        r = RobotState.from_jid("S@115.ecorobot.net/atom")
        r.update({"td": "MapSet", "tp": "vw", "msid": "0"},
                 '<m mid="17" n="" p="1,2,3,4" />')
        assert "vw" in r.map_sets and r.map_sets["vw"].entries[0].mid == "17"

    def test_update_report_mapset_y_stats(self):
        r = self._r()
        r.update_report("getMapSet", {
            "ret": "ok", "type": "ar", "msid": "0",
            "subsets": [{"mssid": "1", "name": "Salón", "center": "0,0"}]})
        assert r.map_sets["ar"].entries[0].name == "Salón"
        r.update_report("getTotalStats", {"ret": "ok", "time": 100, "area": 50, "count": 2})
        assert r.stats["total_time"] == 100.0
        assert r.clean_seconds == 100


class TestCommanders:
    def test_mqtt_clean_room_payload(self):
        from deebot930_bridge.mqtt_commander import MqttCommander
        from tests.test_serve_wiring import FakeTransport

        t = FakeTransport()
        r = RobotState.from_device_id(DeviceId.from_key("x@yna5xh/axm"))
        c = MqttCommander(t, r)
        c.clean_room(["1", "3"])
        cmd = [x for x in t.sent if x[0] == "clean_V2"][-1]
        assert cmd[2]["content"]["type"] == "spotArea"
        assert cmd[2]["content"]["value"] == "1,3"

    def test_mqtt_add_wall_payload(self):
        from deebot930_bridge.mqtt_commander import MqttCommander
        from tests.test_serve_wiring import FakeTransport

        t = FakeTransport()
        r = RobotState.from_device_id(DeviceId.from_key("x@yna5xh/axm"))
        c = MqttCommander(t, r)
        c.add_wall([100, 200, 800, 200])
        cmd = [x for x in t.sent if x[0] == "setMapSubSet"][-1]
        assert cmd[2]["type"] == "vw"
        assert cmd[2]["value"] == "100,200,800,200"

    def test_xmpp_clean_room_xml(self):
        from deebot930_bridge.robot import RobotCommander

        class FakeClient:
            def send_command(self, jid, ctl, td=""):
                self.last = ctl
                return "1"

        r = RobotState.from_jid("S@115.ecorobot.net/atom")
        c = RobotCommander(FakeClient(), r)
        c.clean_room("0,1", mid="832374654")
        assert 'type="SpotArea"' in c.client.last
        assert 'mid="832374654"' in c.client.last
        assert 'p="0,1"' in c.client.last

    def test_xmpp_add_wall_xml(self):
        from deebot930_bridge.robot import RobotCommander

        class FakeClient:
            def send_command(self, jid, ctl, td=""):
                self.last = ctl
                return "1"

        r = RobotState.from_jid("S@115.ecorobot.net/atom")
        c = RobotCommander(FakeClient(), r)
        c.add_wall([10, 20, 30, 40])
        assert 'td="AddM"' in c.client.last
        assert 'tp="vw"' in c.client.last
        assert 'm="10,20,30,40"' in c.client.last
