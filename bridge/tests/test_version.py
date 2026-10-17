"""Firmware version ingestion tests (<ver> element, 930 corpus)."""

from __future__ import annotations

from ecovacs_bridge.robot import RobotState


class TestVersion:
    def _r(self) -> RobotState:
        return RobotState.from_jid("E000105701860607AB7@115.ecorobot.net/atom")

    def test_elemento_ver_del_corpus(self):
        """The 930's REAL format (corpus 2026-10-13): <ver name="FW">1.9.9</ver>."""
        r = self._r()
        r.update({"td": "GetVersion", "ret": "ok"},
                 '<ver name="FW">1.9.9</ver>')
        assert r.version == "1.9.9"

    def test_elemento_con_namespace(self):
        r = self._r()
        r.update({"td": "GetVersion", "ret": "ok"},
                 '<ns0:ver xmlns:ns0="com:ctl" name="FW">2.0.5</ns0:ver>')
        assert r.version == "2.0.5"

    def test_variante_atributo_tolerada(self):
        r = self._r()
        r.update({"td": "GetVersion", "ret": "ok"},
                 '<FW ver="0.25.16" />')
        assert r.version == "0.25.16"

    def test_sin_version_no_toca(self):
        r = self._r()
        r.version = "previa"
        r.update({"td": "GetVersion", "ret": "ok"}, "")
        assert r.version == "previa"
