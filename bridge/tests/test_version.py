"""Firmware version ingestion tests (<ver> element, 930 corpus)."""

from __future__ import annotations

from eco_sunshine.robot import RobotState


class TestVersion:
    def _r(self) -> RobotState:
        return RobotState.from_jid("E000105701860607AB7@115.ecorobot.net/atom")

    def test_ver_element_from_corpus(self):
        """The 930's REAL format (corpus 2026-10-13): <ver name="FW">1.9.9</ver>."""
        r = self._r()
        r.update({"td": "GetVersion", "ret": "ok"},
                 '<ver name="FW">1.9.9</ver>')
        assert r.version == "1.9.9"

    def test_element_with_namespace(self):
        r = self._r()
        r.update({"td": "GetVersion", "ret": "ok"},
                 '<ns0:ver xmlns:ns0="com:ctl" name="FW">2.0.5</ns0:ver>')
        assert r.version == "2.0.5"

    def test_attribute_variant_tolerated(self):
        r = self._r()
        r.update({"td": "GetVersion", "ret": "ok"},
                 '<FW ver="0.25.16" />')
        assert r.version == "0.25.16"

    def test_no_version_untouched(self):
        r = self._r()
        r.version = "previous"
        r.update({"td": "GetVersion", "ret": "ok"}, "")
        assert r.version == "previous"
