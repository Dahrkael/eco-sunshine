"""Regresión de la dialecta ECO del 930 real (hallada en el monolito).

El robot responde los comandos REPITIENDO el ctl-id, SIN td ni pid:
    petición:  <ctl id="8467e033" pid="20" td="PullMP"/>
    eco:       <ctl id="8467e033" ret="ok" i="409549211" p="XQA…"/>
Sin correlación por ctl-id + reinyección de td/pid, TODO se descartaba
(batería None, posición congelada, mapa sin piezas tras los primeros 2 min).
"""

from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET

from ecovacs_bridge.xmpp_client import BumperXMPPClient


class FakeClient(BumperXMPPClient):
    """Cliente sin red: procesamos stanzas a mano."""

    def __init__(self):
        super().__init__("h", 1, "u", "r", on_event=self._evt)
        self.events: list[dict] = []

    def _send(self, data: str) -> None:  # sin writer: capturar en su lugar
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
        # recuperar el ctl_id real que generó send_command (está en _pending)
        ctl_key = next(k for k in c._pending if k.startswith("c:"))
        ctl_id = ctl_key[2:]
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            self._feed(c, eco.replace("PLACEHOLDER", ctl_id)))
        assert c.events, "el eco no generó evento"
        attrs = c.events[-1]
        assert attrs["td"] == "PullMP"      # inyectado
        assert attrs["pid"] == "20"          # reinyectado de la petición
        assert attrs["i"] == "409549211" and attrs["p"] == "XQAABAAQ"

    @staticmethod
    async def _feed(c, stanza):
        c.feed(stanza)
        await asyncio.sleep(0.05)  # deja correr el create_task del emit

    def test_respuesta_con_td_no_se_toca(self):
        """Frames con td propio (pushes MapP/Pos del robot y del mock) intactos."""
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
        """Un eco cuyo ctl-id no conocemos no explota ni inyecta nada."""
        c = FakeClient()
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            self._feed(c,
                       '<iq to="u@ecouser.net/r" type="set" id="X" from="b@115.ecorobot.net/atom">'
                       '<query xmlns="com:ctl"><ctl id="desconocido" ret="ok" /></query></iq>'))
        assert c.events[-1].get("td") is None
