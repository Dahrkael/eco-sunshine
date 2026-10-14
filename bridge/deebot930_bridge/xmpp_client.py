"""Cliente XMPP minimal contra el servidor de Bumper (puerto 5223).

Protocolo (validado contra bumper/xmppserver.py + corpus de la app):
  1. TCP plano → <stream:stream> → features
  2. <starttls/> → upgrade TLS (CA propio de Bumper)
  3. nuevo stream → features (SASL PLAIN)
  4. auth = base64("\\x00<uid>\\x00<pass>") + "/<resource>/<authcode>"
     (con use_auth=False el servidor acepta cualquier authcode)
  5. bind resource → session → presence → READY
  6. responder pings del servidor (iq get ping → result)

Los frames del bot llegan como <iq type=set><query><ctl .../></query></iq>
con from=<bot jid>: el servidor enruta a TODO cliente cuyo uid esté en to=.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import ssl
import uuid
import xml.etree.ElementTree as ET
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

STREAM_OPENER = (
    '<stream:stream xmlns:stream="http://etherx.jabber.org/streams" '
    'xmlns="jabber:client" xmlns:com="com:ctl" version="1.0" to="ecouser.net">'
)

# callback(jid_emisor, ctl_attrs, children_xml)
EventHandler = Callable[[str, dict, str], Awaitable[None]]


def extract_stanzas(buf: str) -> tuple[list[str], str]:
    """Corta stanzas completas <x>...</x> o <x/> del buffer; devuelve (stanzas, resto)."""
    out: list[str] = []
    while True:
        start = min(
            (i for i in (buf.find("<iq"), buf.find("<presence"), buf.find("<message")) if i != -1),
            default=-1,
        )
        if start == -1:
            break
        # stanza auto-cerrada
        self_close = buf.find("/>", start)
        close = buf.find(">", start)
        if self_close != -1 and self_close + 1 == close:
            end = close + 1
        else:
            tag = buf[start + 1 : buf.find(" ", start)]
            if tag.endswith(">"):  # sin atributos
                tag = tag[:-1]
            end = buf.find(f"</{tag}>")
            if end == -1:
                break
            end += len(tag) + 3
        out.append(buf[start:end])
        buf = buf[end:]
    return out, buf


class BumperXMPPClient:
    def __init__(
        self,
        host: str,
        port: int,
        uid: str,
        resource: str = "bridge",
        ca_file: str | None = None,
        on_event: EventHandler | None = None,
    ):
        self.host, self.port, self.uid, self.resource = host, port, uid, resource
        self.ca_file = ca_file
        self.on_event = on_event
        self.jid = f"{uid}@ecouser.net/{resource}"
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self._buf = ""
        self.ready = asyncio.Event()
        self._pending: dict[str, str] = {}
        self._iq_counter: int = 2600  # iq_id -> td esperado (correlacion)

    # ---------- transporte ----------

    async def connect(self) -> None:
        log.info("conectando a %s:%s", self.host, self.port)
        self.reader, self.writer = await asyncio.open_connection(self.host, self.port)
        self._send(STREAM_OPENER)
        await self._expect("starttls")
        self._send('<starttls xmlns="urn:ietf:params:xml:ns:xmpp-tls"/>')
        await self._expect("proceed")
        await self._starttls()
        log.debug("TLS arriba")

        self._buf = ""
        self._send(STREAM_OPENER)
        await self._expect("mechanism")
        token = base64.b64encode(f"\x00{self.uid}\x00{self.resource}".encode()).decode()
        self._send(
            f'<auth xmlns="urn:ietf:params:xml:ns:xmpp-sasl">{token}/{self.resource}/0</auth>'
        )
        await self._expect("success")
        log.info("autenticado como %s", self.uid)

        self._buf = ""
        self._send(STREAM_OPENER)
        await self._expect("bind")
        bid = uuid.uuid4().hex[:8]
        self._send(
            f'<iq type="set" id="{bid}"><bind xmlns="urn:ietf:params:xml:ns:xmpp-bind">'
            f"<resource>{self.resource}</resource></bind></iq>"
        )
        await self._expect('type="result"')
        self._send(f'<iq type="set" id="{uuid.uuid4().hex[:8]}"><session xmlns="urn:ietf:params:xml:ns:xmpp-session"/></iq>')
        await self._expect('type="result"')
        self._send("<presence/>")
        self.ready.set()
        log.info("sesión XMPP lista: %s", self.jid)

    async def _starttls(self) -> None:
        ctx = ssl.create_default_context(cafile=self.ca_file)
        ctx.check_hostname = False
        # el CA de Bumper (generado con Go) no lleva AKI; el modo estricto de
        # py>=3.14 lo rechaza — mantenemos verificación de cadena sin strict
        if hasattr(ssl, "VERIFY_X509_STRICT"):
            ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
        # el servidor es py3.7: con TLS1.3 pierde datos post-handshake (bug
        # conocido de start_tls en 3.7); la app original negocia 1.2 — igualamos
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        loop = asyncio.get_running_loop()
        transport = await loop.start_tls(
            self.writer.transport, self.writer._protocol, ctx, server_hostname=None
        )
        # mismo writer, nuevo transporte (si se crea otro StreamWriter, el
        # viejo muere por GC y su __del__ aborta la conexión TLS)
        self.writer._transport = transport
        await asyncio.sleep(0.1)  # margen contra la carrera post-handshake

    def _send(self, data: str) -> None:
        assert self.writer is not None
        self.writer.write(data.encode())
        log.debug(">> %s", data[:160])

    async def _expect(self, needle: str, timeout: float = 8.0) -> str:
        buf = ""
        while needle not in buf:
            data = await asyncio.wait_for(self.reader.read(65536), timeout)
            if not data:
                raise ConnectionError("conexión cerrada esperando " + needle)
            text = data.decode(errors="replace")
            log.debug("<< %s", text[:160])
            buf += text
        return buf

    # ---------- bucle de eventos ----------

    async def listen(self) -> None:
        assert self.reader is not None
        while True:
            data = await self.reader.read(65536)
            if not data:
                raise ConnectionError("conexión cerrada por Bumper")
            self._buf += data.decode(errors="replace")
            stanzas, self._buf = extract_stanzas(self._buf)
            if stanzas:
                log.debug("stanzas recibidas: %d (buf %d)", len(stanzas), len(self._buf))
                for st in stanzas:
                    log.debug("<< %s", st[:140])
            for s in stanzas:
                await self._handle_stanza(s)

    async def _handle_stanza(self, s: str) -> None:
        try:
            xml = ET.fromstring(s)
        except ET.ParseError as e:
            log.warning("stanza no parseable (%s): %.180s", e, s)
            return
        if xml.tag.endswith("iq"):
            if xml.get("type") == "get":  # ping del servidor
                iq_id = xml.get("id", "")
                self._send(f'<iq type="result" id="{iq_id}" to="ecouser.net"/>')
                return
            self._handle_ctl(xml)

    def _handle_ctl(self, xml: ET.Element) -> None:
        frm = xml.get("from", "")
        if "m=" in ET.tostring(xml, encoding="unicode")[:400]:
            log.debug("frame con m=: %s...", ET.tostring(xml, encoding="unicode")[:300])
        iq_id = xml.get("id", "")
        # correlacion respuesta→peticion: inyectar el td esperado
        expected_td = self._pending.pop(iq_id, None)
        for child in xml:
            if not child.tag.endswith("query"):
                continue
            for ctl in child:
                attrs = dict(ctl.attrib)
                if expected_td and "td" not in attrs:
                    attrs["td"] = expected_td
                inner = "".join(ET.tostring(c, encoding="unicode") for c in ctl)
                if self.on_event:
                    asyncio.create_task(self._emit(frm, attrs, inner))

    async def _emit(self, frm: str, attrs: dict, inner: str) -> None:
        try:
            assert self.on_event is not None
            await self.on_event(frm, attrs, inner)
        except Exception:  # noqa: BLE001
            log.exception("error en handler de evento")

    # ---------- comandos ----------

    def send_command(self, bot_jid: str, ctl_template: str, td: str = "") -> str:
        """Envuelve un ctl plantilla (con {ctl_id}) en el iq EXACTO de la app.

        Formato corpus: <iq xmlns:ns0="com:ctl" from=... id=<decimal> to=...
        type="set"><query>{ctl}</query></iq>
        """
        iq_id = str(self._iq_counter)
        self._iq_counter += 1
        ctl_id = uuid.uuid4().hex[:8]
        if td:
            self._pending[iq_id] = td
        stanza = (
            f'<iq xmlns:ns0="com:ctl" from="{self.jid}" id="{iq_id}" to="{bot_jid}" type="set">'
            f"<query>{ctl_template.format(ctl_id=ctl_id)}</query></iq>"
        )
        self._send(stanza)
        return iq_id

    def reply_ping(self, iq_id: str) -> None:
        self._send(f'<iq type="result" id="{iq_id}" to="ecouser.net"/>')

