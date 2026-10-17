"""Servidor XMPP embebido — vendorizado de bumper/xmppserver.py (Fase FB).

El código de wire (handshake, SASL, bind, routing de iq/ctl, ping) es el
de Bumper PROBADO contra robots reales — se copia casi literal. Cambios
quirúrgicos respecto al original:

- sin `import bumper`: el registro de bots/clients es DeviceRegistry
  (servers/registry.py), inyectado por el constructor
- py3.10+: xml.getchildren() → list(xml); asyncio.Task() → create_task;
  get_event_loop() → get_running_loop()
- un error del listener ya no tumba el proceso (bumper.shutdown() fuera)
- certs por parámetro (ServersConfig) en vez de globales de bumper

Todo lo demás (incluidos los raros replace de namespaces de ET, necesarios
para que los frames lleguen idénticos a como los genera la app) queda
INTACTO a propósito: es el dialecto exacto que hablan los robots.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import ssl
import uuid
import xml.etree.ElementTree as ET

from .registry import DeviceRegistry

log = logging.getLogger(__name__)


class XMPPServer:
    server_id = "ecouser.net"
    clients: list = []
    exit_flag = False
    server = None

    def __init__(self, address, registry: DeviceRegistry,
                 server_cert: str, server_key: str, ca_cert: str):
        self.address = address
        self.registry = registry
        self.server_cert = server_cert
        self.server_key = server_key
        self.ca_cert = ca_cert
        self.xmpp_protocol = lambda: XMPPServerProtocol(self)

    started = False

    async def start_async_server(self) -> None:
        log.info("XMPP embebido en %s:%s", *self.address)
        try:
            loop = asyncio.get_running_loop()
            self.server = await loop.create_server(
                self.xmpp_protocol, host=self.address[0], port=self.address[1]
            )
            self.server_coro = loop.create_task(self.server.serve_forever())
            self.started = True
        except PermissionError as e:
            log.error("XMPP embebido sin permiso: %s", e.strerror)
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            log.exception("XMPP embebido no pudo arrancar")

    def disconnect(self) -> None:
        log.debug("XMPP embebido: cerrando %d clientes", len(self.clients))
        for client in self.clients:
            client._disconnect()
        self.exit_flag = True
        self.server_coro.cancel()


class XMPPServerProtocol(asyncio.Protocol):
    client_id = None
    exit_flag = False
    aclient = None

    def __init__(self, server: "XMPPServer"):
        self.server = server

    def connection_made(self, transport):
        if self.aclient:  # upgrade a TLS: reutiliza el cliente existente
            log.debug("Upgraded connection for %s", self.aclient.address)
            self.aclient.transport = transport
        else:
            aclient = XMPPAsyncClient(transport, self.server)
            self.aclient = aclient
            XMPPServer.clients.append(aclient)
            self.aclient.state = getattr(aclient, "CONNECT")
            log.debug("New Connection from %s", aclient.address)

    def connection_lost(self, error):
        if self.aclient in XMPPServer.clients:
            XMPPServer.clients.remove(self.aclient)
        import time as _t

        self.aclient.disconnected_at = _t.time()
        log.info("conexión cerrada (%s:%s | %s | bot=%s tras %.0fs): %s",
                 self.aclient.address[0], self.aclient.address[1],
                 self.aclient.bumper_jid or "?",
                 self.aclient.type == self.aclient.BOT,
                 _t.time() - getattr(self.aclient, "authenticated_at", _t.time()),
                 error or "EOF/cierre del peer")
        self.aclient._set_state("DISCONNECT")
        log.debug("End Connection for (%s:%s | %s)", self.aclient.address[0],
                  self.aclient.address[1], self.aclient.bumper_jid)

    def data_received(self, data):
        try:
            self.aclient._parse_data(data)
        except UnicodeDecodeError:
            # carrera del handshake TLS: bytes del ClientHello llegando al
            # transporte plano mientras start_tls hace el relevo — se pierden
            # (el handshake los re-procesa el nuevo transporte)
            log.debug("bytes no-UTF8 pre-TLS ignorados (%d)", len(data))


class XMPPAsyncClient:
    IDLE = 0
    CONNECT = 1
    INIT = 2
    BIND = 3
    READY = 4
    DISCONNECT = 5
    UNKNOWN = 0
    BOT = 1
    CONTROLLER = 2
    TLSUpgraded = False

    def __init__(self, transport, server: XMPPServer):
        self.server = server
        self.type = self.UNKNOWN
        self.state = self.IDLE
        self.address = transport.get_extra_info("peername")
        self.transport = transport
        self.clientresource = ""
        self.devclass = ""
        self.bumper_jid = ""
        self.uid = ""
        self.name = ""
        self.log_sent_message = True
        self.log_incoming_data = True

    # ---------- envío ----------

    def send(self, command: str) -> None:
        try:
            if self.log_sent_message:
                log.debug("send to (%s:%s | %s) - %s", self.address[0],
                          self.address[1], self.bumper_jid, command[:200])
            self.transport.write(command.encode())
        except Exception:  # noqa: BLE001
            log.exception("send falló")

    def _disconnect(self) -> None:
        try:
            bot = self.server.registry.bot_get(self.uid)
            if bot:
                self.server.registry.bot_set_xmpp(bot["did"], False)
            client = self.server.registry.client_get(self.clientresource)
            if client:
                self.server.registry.client_set_xmpp(client["resource"], False)
            self.transport.close()
        except Exception:  # noqa: BLE001
            log.exception("disconnect falló")

    # ---------- estado ----------

    def _tag_strip_uri(self, tag: str) -> str:
        if tag and tag[0] == "{":
            _, _, tag = tag[1:].partition("}")
        return tag

    def _set_state(self, state: str) -> None:
        try:
            new_state = getattr(XMPPAsyncClient, state)
            if self.state > new_state:
                raise Exception(f"{self.address} illegal state change "
                                f"{self.state}->{new_state}")
            self.state = new_state
            if new_state == 5:
                self._disconnect()
        except Exception:  # noqa: BLE001
            log.exception("state change falló")

    # ---------- iq/ctl ----------

    def _handle_ctl(self, xml, data: str) -> None:
        try:
            if "roster" in data or "disco#items" in data or "disco#info" in data:
                self.send(
                    '<iq type="error" id="{}"><error type="cancel" code="501">'
                    '<feature-not-implemented xmlns="urn:ietf:params:xml:ns:xmpp-stanzas"/>'
                    "</error></iq>".format(xml.get("id"))
                )
                return

            if xml.get("type") == "set":
                if "com:sf" in data and xml.get("to") == "rl.ecorobot.net":
                    self.send(
                        '<iq id="{}" to="{}@{}/{}" from="rl.ecorobot.net" type="result"/>'.format(
                            xml.get("id"), self.uid, XMPPServer.server_id,
                            self.clientresource,
                        )
                    )

            if len(xml) > 0 and len(xml[0]) > 0:
                ctl = xml[0][0]
                if ctl.get("admin") and self.type == self.BOT:
                    XMPPServerProtocol.client_id = ctl.get("admin")
                    return

            # forward a su destinatario
            for client in XMPPServer.clients:
                if (client.bumper_jid != self.bumper_jid
                        and client.state == client.READY):
                    ctl_to = xml.get("to")
                    if "from" not in xml.attrib:
                        xml.attrib["from"] = f"{self.bumper_jid}"
                    rxmlstring = ET.tostring(xml).decode("utf-8")
                    # limpiar namespaces que añade ET — necesario para que el
                    # frame llegue idéntico al que genera la app original
                    rxmlstring = rxmlstring.replace("xmlns:ns0=", "xmlns=")
                    rxmlstring = rxmlstring.replace("ns0:", "")
                    rxmlstring = rxmlstring.replace('iq xmlns="com:ctl"', "iq")
                    rxmlstring = rxmlstring.replace("<query", '<query xmlns="com:ctl"')

                    if client.type == self.BOT:
                        if client.uid.lower() in ctl_to.lower():
                            log.debug("Sending ctl to bot: %s", rxmlstring[:200])
                            client.send(rxmlstring)
        except Exception:  # noqa: BLE001
            log.exception("handle_ctl falló")

    def _handle_ping(self, xml, data: str) -> None:
        try:
            if xml.get("to").find("@") == -1:  # ping al servidor
                self.send('<iq type="result" id="{}" from="{}" />'.format(
                    xml.get("id"), xml.get("to")))
                return

            pingto = xml.get("to")
            if "from" not in xml.attrib:
                xml.attrib["from"] = f"{self.bumper_jid}"
            pingstring = ET.tostring(xml).decode("utf-8")
            pingstring = pingstring.replace("xmlns:ns0=", "xmlns=")
            pingstring = pingstring.replace("ns0:", "")
            pingstring = pingstring.replace('iq xmlns="urn:xmpp:ping"', "iq")
            pingstring = pingstring.replace("<ping", '<ping xmlns="urn:xmpp:ping"')

            for client in XMPPServer.clients:
                if (client.bumper_jid != self.bumper_jid
                        and client.state == client.READY):
                    if client.uid.lower() in pingto.lower():
                        client.send(pingstring)
        except Exception:  # noqa: BLE001
            log.exception("handle_ping falló")

    async def schedule_ping(self, seconds: float) -> None:
        if not self.state == 5:
            self.send(
                "<iq from='{}' to='{}' id='s2c1' type='get'>"
                "<ping xmlns='urn:xmpp:ping'/></iq>".format(
                    XMPPServer.server_id, self.bumper_jid))
            await asyncio.sleep(seconds)
            asyncio.get_running_loop().create_task(self.schedule_ping(seconds))

    def _handle_result(self, xml, data: str) -> None:
        try:
            ctl_to = xml.get("to")
            if "from" not in xml.attrib:
                xml.attrib["from"] = f"{self.bumper_jid}"

            if ("errno='103'" in data
                    and self.type == self.BOT):
                # robot estaba en la nube real: dar permisos al usuario local
                # (misma receta que Bumper)
                log.info("bot sin permisos para el usuario — aplicando receta AddUser/SetAC")
                try:
                    xquery = list(xml)
                    ctl = list(xquery[0])
                    if "error" in ctl[0].attrib:
                        adminuser = ctl[0].attrib["error"].replace(
                            "permission denied, please contact ", "").replace(" ", "")
                    else:
                        adminuser = ctl[0].attrib.get("admin", "")
                    if not (adminuser.startswith(("fuid_", "fusername_"))
                            or self.server.registry.use_auth):
                        newuser = ctl_to.split("/")[0]
                        self.send(
                            '<iq type="set" id="{}" from="{}" to="{}">'
                            '<query xmlns="com:ctl"><ctl td="AddUser" id="0000" jid="{}" />'
                            "</query></iq>".format(uuid.uuid4().hex[:8], adminuser,
                                                   self.bumper_jid, newuser))
                        self.send(
                            '<iq type="set" id="{}" from="{}" to="{}">'
                            '<query xmlns="com:ctl"><ctl td="SetAC" id="1111" jid="{}">'
                            '<acs><ac name="userman" allow="1"/><ac name="setting" allow="1"/>'
                            '<ac name="clean" allow="1"/></acs></ctl></query></iq>'.format(
                                uuid.uuid4().hex[:8], adminuser, self.bumper_jid, newuser))
                except Exception:  # noqa: BLE001
                    log.exception("receta AddUser falló")
                return

            rxmlstring = ET.tostring(xml).decode("utf-8")
            rxmlstring = rxmlstring.replace("xmlns:ns0=", "xmlns=")
            rxmlstring = rxmlstring.replace("ns0:", "")
            rxmlstring = rxmlstring.replace('iq xmlns="com:ctl"', "iq")
            rxmlstring = rxmlstring.replace("<query", '<query xmlns="com:ctl"')

            if self.type == self.BOT and ctl_to == "de.ecorobot.net":
                # broadcast del robot (p.ej. BatteryInfo) → todos los clients
                log.debug("broadcast de.ecorobot.net → todos")
                for client in XMPPServer.clients:
                    client.send(rxmlstring)

            if xml.get("to").find("@") == -1:
                ctl_to = xml.get("to")
            else:
                ctl_to = "{}@ecouser.net".format(ctl_to.split("@")[0])

            for client in XMPPServer.clients:
                if (client.bumper_jid != self.bumper_jid
                        and client.state == client.READY):
                    if "@" not in ctl_to:
                        client.send(rxmlstring)
                    elif client.uid.lower() in ctl_to.lower():
                        log.debug("Sending from %s to client %s", self.uid, client.uid)
                        client.send(rxmlstring)
        except Exception:  # noqa: BLE001
            log.exception("handle_result falló")

    # ---------- handshake ----------

    def _handle_connect(self, data, xml=None) -> None:
        try:
            if self.state == self.CONNECT:
                if xml is None:
                    if data.decode("utf-8").find("jabber:client") > -1:
                        text = data.decode("utf-8")
                        sc = text.find("to=")
                        ec = text.find(".ecorobot.net")
                        if ec > -1:
                            # class del bot en el to= del stream (SERIAL@CLS…)
                            self.devclass = text[sc + 4: ec]
                        self.send(
                            '<stream:stream xmlns:stream="http://etherx.jabber.org/streams" '
                            'xmlns="jabber:client" version="1.0" id="1" from="{}">'.format(
                                XMPPServer.server_id))
                        if not self.TLSUpgraded:
                            self.send(
                                '<stream:features><starttls xmlns="urn:ietf:params:xml:ns:xmpp-tls">'
                                '<required/></starttls><mechanisms '
                                'xmlns="urn:ietf:params:xml:ns:xmpp-sasl">'
                                "<mechanism>PLAIN</mechanism></mechanisms></stream:features>")
                        else:
                            self.send(
                                '<stream:features><mechanisms '
                                'xmlns="urn:ietf:params:xml:ns:xmpp-sasl">'
                                "<mechanism>PLAIN</mechanism></mechanisms></stream:features>")
                    else:
                        self.send("</stream>")
                else:
                    if "urn:ietf:params:xml:ns:xmpp-sasl" in xml.tag:
                        self._handle_sasl_auth(xml)
                    else:
                        log.error("Couldn't handle: %s", xml)

            elif self.state == self.INIT:
                if xml is None:
                    if data.decode("utf-8").find("jabber:client") > -1:
                        self.send(
                            '<stream:stream xmlns:stream="http://etherx.jabber.org/streams" '
                            'xmlns="jabber:client" version="1.0" id="1" from="{}">'.format(
                                XMPPServer.server_id))
                        self.send(
                            '<stream:features><bind xmlns="urn:ietf:params:xml:ns:xmpp-bind"/>'
                            '<session xmlns="urn:ietf:params:xml:ns:xmpp-session"/>'
                            "</stream:features>")
                else:
                    child = self._tag_strip_uri(xml[0].tag) if len(xml) else None
                    if xml.tag == "iq" and child == "bind":
                        self._handle_bind(xml)
        except Exception:  # noqa: BLE001
            log.exception("handle_connect falló")

    async def _handle_starttls(self, data) -> None:
        try:
            if not self.TLSUpgraded:
                self.TLSUpgraded = True
                self.send("<proceed xmlns='urn:ietf:params:xml:ns:xmpp-tls'/>")
                loop = asyncio.get_running_loop()
                transport = self.transport
                protocol = self.transport.get_protocol()
                ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
                ssl_ctx.load_cert_chain(self.server.server_cert, self.server.server_key)
                ssl_ctx.load_verify_locations(cafile=self.server.ca_cert)
                new_transport = await loop.start_tls(
                    transport, protocol, ssl_ctx, server_side=True)
                protocol.connection_made(new_transport)
        except Exception:  # noqa: BLE001
            log.exception("starttls falló")

    def _handle_sasl_auth(self, xml) -> None:
        try:
            # auth = b64("\x00uid\x00resource") + "/resource/authcode"
            # El token b64 termina en su padding '=' (o es múltiplo de 4 sin
            # él): separar token y tail ANTES de decodificar. py3.7 tragaba
            # el conjunto con basura; py3.14 es estricto ("Incorrect padding")
            text = xml.text or ""
            eq = text.find("=")
            if eq != -1:
                token, tail = text[:eq + 1], text[eq + 1:]
            else:
                n = (len(text) // 4) * 4
                token, tail = text[:n], text[n:]
            decoded = base64.b64decode(token)
            fields = decoded.split(b"\x00")
            username = fields[1].decode("utf-8", "ignore") if len(fields) > 1 else ""
            # resource: dentro del token (2º campo \x00) o en el tail /res/ac
            if len(fields) > 2:
                resource = fields[2].decode("utf-8", "ignore")
            else:
                tail_parts = tail.split("/")
                resource = tail_parts[1] if len(tail_parts) > 1 else ""
            authcode = tail.split("/")[2] if len(tail.split("/")) > 2 else ""
            self.uid = username
            self.clientresource = resource

            if self.devclass:  # es un bot (class en el to= del stream)
                self.server.registry.bot_add(self.uid, self.uid, self.devclass,
                                             "atom", "eco-legacy")
                self.type = self.BOT
                import time as _t

                self.authenticated_at = _t.time()
                log.info("bot autenticado SN=%s clase=%s", self.uid, self.devclass)
                self.send('<success xmlns="urn:ietf:params:xml:ns:xmpp-sasl"/>')
                self._set_state("INIT")
            else:
                reg = self.server.registry
                if reg.check_authcode(self.uid, authcode) or not reg.use_auth:
                    self.type = self.CONTROLLER
                    reg.client_add(self.uid, "bumper", self.clientresource)
                    log.info("client autenticado %s/%s", self.uid, self.clientresource)
                    self._set_state("INIT")
                    self.send('<success xmlns="urn:ietf:params:xml:ns:xmpp-sasl"/>')
                else:
                    self.send('<response xmlns="urn:ietf:params:xml:ns:xmpp-sasl"/>')
        except Exception:  # noqa: BLE001
            log.exception("sasl_auth falló")

    def _handle_bind(self, xml) -> None:
        try:
            bot = self.server.registry.bot_get(self.uid)
            if bot:
                self.server.registry.bot_set_xmpp(bot["did"], True)
            client = self.server.registry.client_get(self.clientresource)
            if client:
                self.server.registry.client_set_xmpp(client["resource"], True)

            if self.devclass:  # bot: jid SERIAL@CLS.ecorobot.net/atom
                self.name = f"XMPP_Client_{self.uid}_{self.devclass}"
                self.bumper_jid = f"{self.uid}@{self.devclass}.ecorobot.net/atom"
                res = ('<iq type="result" id="{}"><bind xmlns="urn:ietf:params:xml:ns:xmpp-bind">'
                       "<jid>{}</jid></bind></iq>").format(xml.get("id"), self.bumper_jid)
            else:
                children = list(xml)
                if children and len(children[0]):
                    self.clientresource = list(children[0])[0].text
                self.name = f"XMPP_Client_{self.clientresource}"
                self.bumper_jid = f"{self.uid}@{XMPPServer.server_id}/{self.clientresource}"
                res = ('<iq type="result" id="{}"><bind xmlns="urn:ietf:params:xml:ns:xmpp-bind">'
                       "<jid>{}</jid></bind></iq>").format(xml.get("id"), self.bumper_jid)
            self._set_state("BIND")
            self.send(res)
        except Exception:  # noqa: BLE001
            log.exception("bind falló")

    def _handle_session(self, xml) -> None:
        res = '<iq type="result" id="{}" />'.format(xml.get("id"))
        self._set_state("READY")
        self.send(res)
        asyncio.get_running_loop().create_task(self.schedule_ping(30))

    def _handle_presence(self, xml) -> None:
        if len(xml) and self._tag_strip_uri(xml[0].tag) == "status":
            # presencia de bot (hello world) → dummy + GetDeviceInfo
            self.send(f'<presence to="{self.bumper_jid}"> dummy </presence>')
            if self.type == self.BOT:
                self.send(
                    '<iq type="set" id="14" to="{}" from="{}">'
                    '<query xmlns="com:ctl"><ctl td="GetDeviceInfo"/></query></iq>'.format(
                        self.bumper_jid, XMPPServer.server_id))
        else:
            ptype = xml.get("type")
            if ptype == "unavailable":
                self._set_state("DISCONNECT")
            else:
                self.send(f'<presence to="{self.bumper_jid}"> dummy </presence>')

    # ---------- parser ----------

    def _parse_data(self, data) -> None:
        text = data.decode("utf-8")
        if text.startswith("<?xml"):
            newdata = re.sub(r"(<\?xml[^>]+\?>)", r"<root>", text) + "</root>"
        else:
            newdata = f"<root>{text}</root>"

        try:
            root = ET.fromstring(newdata)
            for item in root.iter():
                if item.tag == "root":
                    continue
                if item.tag == "iq":
                    self._handle_iq(item, newdata)
                    item.clear()
                elif "auth" in item.tag:
                    if "urn:ietf:params:xml:ns:xmpp-sasl" in item.tag:
                        self._handle_sasl_auth(item)
                        item.clear()
                elif "-tls" in item.tag:
                    if not self.TLSUpgraded:
                        asyncio.get_running_loop().create_task(
                            self._handle_starttls(newdata.encode("utf-8")))
                elif "presence" in item.tag:
                    self._handle_presence(item)
                    item.clear()
        except ET.ParseError as e:
            if "no element found" in e.msg:
                # stream sin cerrar (handshake): normal en XMPP
                if "<stream:stream " in newdata:
                    if self.state in (self.CONNECT, self.INIT):
                        self._handle_connect(newdata.encode("utf-8"))
            elif "not well-formed (invalid token)" in e.msg:
                if "</stream:stream>" in newdata:
                    self.send("</stream:stream>")
                else:
                    log.error("xml parse error - %s - %s", newdata[:200], e)
            else:
                if "<stream:stream" in newdata:
                    if self.state in (self.CONNECT, self.INIT):
                        self._handle_connect(newdata.encode("utf-8"))
                elif "</stream:stream>" in newdata:
                    self.send("</stream:stream>")
                    self._set_state("DISCONNECT")
                else:
                    log.error("xml parse error - %s - %s", newdata[:200], e)
        except Exception:  # noqa: BLE001
            log.exception("parse_data falló")

    def _handle_iq(self, xml, data: str) -> None:
        child = self._tag_strip_uri(xml[0].tag) if len(xml) else None

        if xml.tag != "iq":
            return
        if child == "bind":
            self._handle_bind(xml)
        elif child == "session":
            self._handle_session(xml)
        elif child == "ping":
            self._handle_ping(xml, data)
        elif child == "query":
            if self.type == self.BOT:
                self._handle_result(xml, data)
            else:
                self._handle_ctl(xml, data)
        elif xml.get("type") in ("result", "set"):
            self._handle_result(xml, data)
