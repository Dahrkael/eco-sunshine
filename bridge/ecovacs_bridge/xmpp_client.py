"""Minimal XMPP client against Bumper's server (port 5223).

Protocol (validated against bumper/xmppserver.py + the app corpus):
  1. plain TCP → <stream:stream> → features
  2. <starttls/> → TLS upgrade (Bumper's own CA)
  3. new stream → features (SASL PLAIN)
  4. auth = base64("\\x00<uid>\\x00<pass>") + "/<resource>/<authcode>"
     (with use_auth=False the server accepts any authcode)
  5. bind resource → session → presence → READY
  6. answer the server's pings (iq get ping → result)

Bot frames arrive as <iq type=set><query><ctl .../></query></iq> with
from=<bot jid>: the server routes to EVERY client whose uid is in to=.
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

# callback(sender jid, ctl_attrs, children_xml)
EventHandler = Callable[[str, dict, str], Awaitable[None]]


def extract_stanzas(buf: str) -> tuple[list[str], str]:
    """Cuts complete stanzas <x>...</x> or <x/> from the buffer; returns (stanzas, remainder)."""
    out: list[str] = []
    while True:
        start = min(
            (i for i in (buf.find("<iq"), buf.find("<presence"), buf.find("<message")) if i != -1),
            default=-1,
        )
        if start == -1:
            break
        # self-closing stanza
        self_close = buf.find("/>", start)
        close = buf.find(">", start)
        if self_close != -1 and self_close + 1 == close:
            end = close + 1
        else:
            tag = buf[start + 1 : buf.find(" ", start)]
            if tag.endswith(">"):  # no attributes
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
        self._pending: dict[str, dict] = {}
        self._iq_counter: int = 2600  # iq_id/ctl_id → expected td+attrs (correlation)

    # ---------- transport ----------

    async def connect(self) -> None:
        log.info("connecting to %s:%s", self.host, self.port)
        self.reader, self.writer = await asyncio.open_connection(self.host, self.port)
        self._send(STREAM_OPENER)
        await self._expect("starttls")
        self._send('<starttls xmlns="urn:ietf:params:xml:ns:xmpp-tls"/>')
        await self._expect("proceed")
        await self._starttls()
        log.debug("TLS up")

        self._buf = ""
        self._send(STREAM_OPENER)
        await self._expect("mechanism")
        token = base64.b64encode(f"\x00{self.uid}\x00{self.resource}".encode()).decode()
        self._send(
            f'<auth xmlns="urn:ietf:params:xml:ns:xmpp-sasl">{token}/{self.resource}/0</auth>'
        )
        await self._expect("success")
        log.info("authenticated as %s", self.uid)

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
        log.info("XMPP session ready: %s", self.jid)

    async def _starttls(self) -> None:
        ctx = ssl.create_default_context(cafile=self.ca_file)
        ctx.check_hostname = False
        # Bumper's CA (Go-generated) carries no AKI; py>=3.14's strict mode
        # rejects it — keep chain verification without strict
        if hasattr(ssl, "VERIFY_X509_STRICT"):
            ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
        # the server is py3.7: with TLS1.3 it loses post-handshake data
        # (known 3.7 start_tls bug); the original app negotiates 1.2 — match it
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        loop = asyncio.get_running_loop()
        transport = await loop.start_tls(
            self.writer.transport, self.writer._protocol, ctx, server_hostname=None
        )
        # same writer, new transport (creating another StreamWriter would let
        # the old one die by GC and its __del__ abort the TLS connection)
        self.writer._transport = transport
        await asyncio.sleep(0.1)  # margin against the post-handshake race

    def _send(self, data: str) -> None:
        assert self.writer is not None
        self.writer.write(data.encode())
        log.debug(">> %s", data[:160])

    async def _expect(self, needle: str, timeout: float = 8.0) -> str:
        buf = ""
        while needle not in buf:
            data = await asyncio.wait_for(self.reader.read(65536), timeout)
            if not data:
                raise ConnectionError("connection closed while waiting for " + needle)
            text = data.decode(errors="replace")
            log.debug("<< %s", text[:160])
            buf += text
        return buf

    # ---------- event loop ----------

    async def listen(self) -> None:
        assert self.reader is not None
        while True:
            data = await self.reader.read(65536)
            if not data:
                raise ConnectionError("connection closed by Bumper")
            self._buf += data.decode(errors="replace")
            stanzas, self._buf = extract_stanzas(self._buf)
            if stanzas:
                log.debug("stanzas received: %d (buf %d)", len(stanzas), len(self._buf))
                for st in stanzas:
                    log.debug("<< %s", st[:140])
            for s in stanzas:
                await self._handle_stanza(s)

    async def _handle_stanza(self, s: str) -> None:
        try:
            xml = ET.fromstring(s)
        except ET.ParseError as e:
            log.warning("unparseable stanza (%s): %.180s", e, s)
            return
        if xml.tag.endswith("iq"):
            if xml.get("type") == "get":  # server ping
                iq_id = xml.get("id", "")
                self._send(f'<iq type="result" id="{iq_id}" to="ecouser.net"/>')
                return
            self._handle_ctl(xml)

    def _handle_ctl(self, xml: ET.Element) -> None:
        frm = xml.get("from", "")
        if "m=" in ET.tostring(xml, encoding="unicode")[:400]:
            log.debug("frame with m=: %s...", ET.tostring(xml, encoding="unicode")[:300])
        iq_id = xml.get("id", "")
        for child in xml:
            if not child.tag.endswith("query"):
                continue
            for ctl in child:
                attrs = dict(ctl.attrib)
                # 930 ECO dialect correlation: the robot replies without td
                # (or pid) but echoes back the CTL id we sent. Inject the
                # expected td plus the request's attrs (e.g. a PullMP's
                # pid — the handler then processes it as a MapP with it).
                pending = self._pending.pop(f"c:{attrs.get('id')}", None) \
                    or self._pending.pop(iq_id, None)
                if pending:
                    attrs.setdefault("td", pending["td"])
                    for k, v in pending.get("attrs", {}).items():
                        attrs.setdefault(k, v)
                    # clear the sibling key (avoids a _pending leak)
                    self._pending.pop(str(pending.get("_iq", "")), None)
                inner = "".join(ET.tostring(c, encoding="unicode") for c in ctl)
                if self.on_event:
                    asyncio.create_task(self._emit(frm, attrs, inner))

    async def _emit(self, frm: str, attrs: dict, inner: str) -> None:
        try:
            assert self.on_event is not None
            await self.on_event(frm, attrs, inner)
        except Exception:  # noqa: BLE001
            log.exception("error in event handler")

    # ---------- commands ----------

    def send_command(self, bot_jid: str, ctl_template: str, td: str = "",
                     extra_attrs: dict | None = None) -> str:
        """Wraps a ctl template (with {ctl_id}) in the app's EXACT iq.

        Corpus format: <iq xmlns:ns0="com:ctl" from=... id=<decimal> to=...
        type="set"><query>{ctl}</query></iq>

        Reply correlation is stored by CTL id (the robot echoes that id
        in its replies, not the iq id) and by iq id (just in case).
        extra_attrs: request attrs to re-inject into the reply (e.g.
        {"pid": N} on a PullMP — the echo carries no pid).
        """
        iq_id = str(self._iq_counter)
        self._iq_counter += 1
        ctl_id = uuid.uuid4().hex[:8]
        if td:
            pending = {"td": td, "attrs": extra_attrs or {}, "_iq": iq_id}
            self._pending[f"c:{ctl_id}"] = pending
            self._pending[iq_id] = pending
        stanza = (
            f'<iq xmlns:ns0="com:ctl" from="{self.jid}" id="{iq_id}" to="{bot_jid}" type="set">'
            f"<query>{ctl_template.format(ctl_id=ctl_id)}</query></iq>"
        )
        self._send(stanza)
        return iq_id

