"""Regression test for the real 930's ECO dialect (found on the monolith).

The robot answers commands by ECHOING the ctl-id, WITHOUT td or pid:
    request:  <ctl id="8467e033" pid="20" td="PullMP"/>
    echo:     <ctl id="8467e033" ret="ok" i="409549211" p="XQA…"/>
Without ctl-id correlation + td/pid re-injection, EVERYTHING was dropped
(battery None, frozen position, no map pieces after the first 2 min).
"""

from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET

from ecovacs_bridge.xmpp_client import BumperXMPPClient


class FakeClient(BumperXMPPClient):
    """Network-less client: we feed stanzas by hand."""

    def __init__(self):
        super().__init__("h", 1, "u", "r", on_event=self._evt)
        self.events: list[dict] = []

    def _send(self, data: str) -> None:  # no writer: capture instead
        self.sent = data

    async def _evt(self, jid, attrs, inner):
        self.events.append(attrs)

    def feed(self, stanza: str) -> None:
        self._handle_ctl(ET.fromstring(stanza))


def make_client() -> tuple[FakeClient, str]:
    c = FakeClient()
    iq_id = c.send_command(
        "bot@115.ecorobot.net/atom",
        '<ctl id="{ctl_id}" pid="20" td="PullMP" />', "PullMP",
        extra_attrs={"pid": "20"})
    return c, iq_id


class TestDialectaEco:
    def test_eco_con_ctl_id_recibe_td_y_pid(self):
        """El eco repite el CTL id con un iq id propio — debe procesarse."""
        c, iq_id = make_client()
        eco = ('<iq to="u@ecouser.net/r" type="set" id="021C6E5B2D10" '
               'from="bot@115.ecorobot.net/atom">'
               '<query xmlns="com:ctl">'
               '<ctl id="' + "PLACEHOLDER" + '" ret="ok" i="409549211" p="XQAABAAQ" />'
               "</query></iq>")
        # recover the real ctl_id send_command generated (it is in _pending)
        ctl_key = next(k for k in c._pending if k.startswith("c:"))
        ctl_id = ctl_key[2:]
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            self._feed(c, eco.replace("PLACEHOLDER", ctl_id)))
        assert c.events, "el eco no generó evento"
        attrs = c.events[-1]
        assert attrs["td"] == "PullMP"      # injected
        assert attrs["pid"] == "20"          # re-injected from the request
        assert attrs["i"] == "409549211" and attrs["p"] == "XQAABAAQ"

    @staticmethod
    async def _feed(c, stanza):
        c.feed(stanza)
        await asyncio.sleep(0.05)  # let the emit's create_task run

    def test_respuesta_con_td_no_se_toca(self):
        """Frames with their own td (robot and mock MapP/Pos pushes) untouched."""
        c = FakeClient()
        c.send_command("b@115.ecorobot.net/atom", '<ctl id="{ctl_id}" td="GetPos" />',
                       "GetPos")
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            self._feed(c,
                       '<iq to="u@ecouser.net/r" type="set" id="X" from="b@115.ecorobot.net/atom">'
                       '<query xmlns="com:ctl"><ctl a="0" p="1,2" t="p" td="Pos" valid="1" />'
                       "</query></iq>"))
        assert c.events[-1]["td"] == "Pos"

    def test_sin_correlacion_pasa_limpio(self):
        """An echo whose ctl-id we do not know neither blows up nor injects anything."""
        c = FakeClient()
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            self._feed(c,
                       '<iq to="u@ecouser.net/r" type="set" id="X" from="b@115.ecorobot.net/atom">'
                       '<query xmlns="com:ctl"><ctl id="desconocido" ret="ok" /></query></iq>'))
        assert c.events[-1].get("td") is None
