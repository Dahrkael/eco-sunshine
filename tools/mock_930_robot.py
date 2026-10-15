#!/usr/bin/env python3
"""Mock del Deebot OZMO 930 (XMPP) — clon del comportamiento del robot real.

Se conecta al XMPP de Bumper :5223 como bot (SERIAL@115.ecorobot.net/atom)
y responde a los comandos del bridge con los frames EXACTOS que el robot
real mandó, extraídos del corpus (tools/extract_930_corpus.py a partir del
log de Bumper): misma spec de mapa (mismos CRCs), mismas piezas LZMA, la
trayectoria real de limpieza, lifespans y batería reales.

Flujo de conexión (igual que el robot): stream to=115.ecorobot.net →
STARTTLS (CA de Bumper) → SASL PLAIN (uid=SN) → bind "atom" → session →
responde pings de ecouser.net.

Uso:
    .venv/bin/python tools/mock_930_robot.py [--host localhost] [--port 5223]
        [--sn E00010570MOCK9301] [--corpus data/mock930-corpus.json]
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import random
import ssl
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "bridge"))

from deebot930_bridge.xmpp_client import extract_stanzas  # noqa: E402

log = logging.getLogger("mock-930")

STREAM_OPENER = (
    '<stream:stream xmlns:stream="http://etherx.jabber.org/streams" '
    'xmlns="jabber:client" xmlns:com="com:ctl" version="1.0" to="{cls}.ecorobot.net">'
)


def iq_id() -> str:
    """El real genera ids hex de 12 (ej: 069F9910AEA0)."""
    return f"{random.randrange(0x100000000000):012X}"


def ctl_id() -> str:
    """El real genera ids decimales de 8 (ej: 23259737)."""
    return f"{random.randrange(10000000, 99999999)}"


class Mock930:
    def __init__(self, host: str, port: int, sn: str, cls: str, corpus: dict):
        self.host, self.port = host, port
        self.sn, self.cls = sn, cls
        self.jid = f"{sn}@{cls}.ecorobot.net/atom"
        self.corpus = corpus
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self._buf = ""
        # estado del robot (arranca como el real al final del corpus: en base)
        self.mode = "charging"          # charging|idle|cleaning|paused|returning
        self.battery = int((corpus.get("battery") or {}).get("power", 100))
        self.speed = "standard"
        self.map_id = (corpus.get("spec_mapm") or {}).get("i", "0")
        # trayectoria real: replay cíclico mientras limpia
        self.path = corpus.get("trayectoria") or [["0,0", "0"]]
        self.path_i = 0
        self.pos = self.path[0] if self.path else ["0,0", "0"]
        # último cliente que nos habló → destino de los pushes
        self.client_jid: str | None = None
        # muros/zonas dinámicos (AddM/DelM mutan; el corpus no tenía ninguno)
        self.walls = [{"mid": "17", "p": "-1000,2000,1000,2000"}]
        self.zones = [{"mid": "18", "p": "-3500,-4500,-2500,-3500"}]
        self.stats = {"area": 12345678, "time": 4200, "count": 14}

    # ---------- conexión (dialecto bot del 930) ----------

    async def run(self) -> None:
        while True:
            try:
                await self._session()
                await self._loop()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("desconectado (%s) — reintento en 3s", e)
                await asyncio.sleep(3)

    async def _send(self, data: str) -> None:
        assert self.writer is not None
        self.writer.write(data.encode())
        log.debug(">> %s", data[:160])

    async def _expect(self, needle: str, timeout: float = 8.0) -> str:
        assert self.reader is not None
        buf = ""
        while needle not in buf:
            data = await asyncio.wait_for(self.reader.read(65536), timeout)
            if not data:
                raise ConnectionError("conexión cerrada esperando " + needle)
            buf += data.decode(errors="replace")
        return buf

    async def _session(self) -> None:
        log.info("conectando a %s:%d como %s", self.host, self.port, self.jid)
        self.reader, self.writer = await asyncio.open_connection(self.host, self.port)
        self._buf = ""
        await self._send(STREAM_OPENER.format(cls=self.cls))
        await self._expect("starttls")
        await self._send('<starttls xmlns="urn:ietf:params:xml:ns:xmpp-tls"/>')
        await self._expect("proceed")
        await self._starttls()

        self._buf = ""
        await self._send(STREAM_OPENER.format(cls=self.cls))
        await self._expect("mechanism")
        # SASL del bot: uid = SN (bumper registra SN@cls/atom como bot eco-legacy)
        token = base64.b64encode(f"\x00{self.sn}\x00atom".encode()).decode()
        await self._send(
            f'<auth xmlns="urn:ietf:params:xml:ns:xmpp-sasl">{token}/atom/0</auth>')
        await self._expect("success")
        log.info("autenticado como bot %s", self.sn)

        self._buf = ""
        await self._send(STREAM_OPENER.format(cls=self.cls))
        await self._expect("bind")
        await self._send(
            f'<iq type="set" id="{iq_id()}"><bind xmlns="urn:ietf:params:xml:ns:xmpp-bind">'
            f"<resource>atom</resource></bind></iq>")
        await self._expect('type="result"')
        await self._send(
            f'<iq type="set" id="{iq_id()}"><session xmlns="urn:ietf:params:xml:ns:xmpp-session"/></iq>')
        await self._expect('type="result"')
        await self._send("<presence/>")
        # presentación, como el real al arrancar
        await self.push_ctl(
            f'<ctl td="DeviceInfo"><MID>{self.jid}</MID><class>{self.cls}</class>'
            f'<chn v="xb" /><wd>0</wd></ctl>')
        log.info("sesión bot lista: %s", self.jid)

    async def _starttls(self) -> None:
        ctx = ssl.create_default_context(cafile=self._ca_file())
        ctx.check_hostname = False
        if hasattr(ssl, "VERIFY_X509_STRICT"):
            ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
        # bumper es py3.7: TLS1.3 pierde datos post-handshake (mismo workaround
        # que el xmpp_client del bridge)
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        loop = asyncio.get_running_loop()
        transport = await loop.start_tls(
            self.writer.transport, self.writer._protocol, ctx, server_hostname=None)
        self.writer._transport = transport
        await asyncio.sleep(0.1)

    def _ca_file(self) -> str:
        for cand in ("/mnt/t/ecovacs/data/certs/ca.crt", "data/certs/ca.crt"):
            if Path(cand).exists():
                return cand
        return "/mnt/t/ecovacs/data/certs/ca.crt"

    # ---------- frames (formatos exactos del corpus) ----------

    async def send_iq(self, ctl: str, to: str | None = None) -> None:
        """iq como los del robot: xmlns ns0, id hex12, type=set."""
        dst = to or self.client_jid or "ecouser.net"
        await self._send(
            f'<iq xmlns:ns0="com:ctl" id="{iq_id()}" to="{dst}" type="set">'
            f"<query>{ctl}</query></iq>")

    async def push_ctl(self, ctl: str) -> None:
        await self.send_iq(ctl)

    async def ack(self, cid: str, errs: str = "") -> None:
        """ACK del real: <ctl id=.. ret='ok' errs=''/> (sin td)."""
        await self.send_iq(f"<ctl id='{cid}' ret='ok' errs='{errs}'/>")

    def _ts(self) -> str:
        return str(int(time.time()))

    # ---------- bucle principal ----------

    async def _loop(self) -> None:
        assert self.reader is not None
        pusher = asyncio.create_task(self._push_loop())
        try:
            while True:
                data = await self.reader.read(65536)
                if not data:
                    raise ConnectionError("conexión cerrada por Bumper")
                self._buf += data.decode(errors="replace")
                stanzas, self._buf = extract_stanzas(self._buf)
                for st in stanzas:
                    await self._handle(stanza=st)
        finally:
            pusher.cancel()

    async def _handle(self, stanza: str) -> None:
        if "type=\"get\"" in stanza or "type='get'" in stanza:
            # ping de ecouser.net → result (como el cliente del bridge)
            m_id = _attr(stanza, "id")
            await self._send(f'<iq type="result" id="{m_id}" to="ecouser.net"/>')
            return
        if "com:ctl" not in stanza and "<ctl" not in stanza:
            return
        import re

        m = re.search(r"<ctl ([^>]*)/?>", stanza)
        if not m:
            return
        attrs = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
        frm = _attr(stanza, "from")
        if frm and "ecouser.net" in frm:
            self.client_jid = frm  # pushes dirigidos a quien pregunta
        await self._on_ctl(attrs)

    async def _on_ctl(self, a: dict) -> None:
        td = a.get("td", "")
        log.info("<< td=%s %s", td, {k: v for k, v in a.items() if k != "td"})

        # ---- control de limpieza (cambia el modo + pushes como el real)
        if td == "Clean":
            act = a.get("act", "s")
            self.speed = a.get("speed", self.speed)
            await self.ack(a.get("id", ctl_id()))
            if act == "s":
                self.mode = "cleaning"
                await self.push_ctl(
                    f'<ctl td="CleanReport" ts="{self._ts()}"><clean a="" l="" rsn="" '
                    f'speed="{self.speed}" st="s" sts="" type="{a.get("type", "auto")}" /></ctl>')
            elif act == "p":
                self.mode = "paused"
                await self._push_clean_st("p")
            elif act == "r":
                self.mode = "cleaning"
                await self._push_clean_st("s")
            elif act == "h":
                self.mode = "idle"
                await self._push_clean_st("h")
            return
        if td == "Charge":
            self.mode = "returning"
            await self.ack(a.get("id", ctl_id()))
            await self.push_ctl(
                f'<ctl td="ChargeState" ts="{self._ts()}">'
                f'<charge h="" r="" s="" type="Going" /></ctl>')
            return

        # ---- consultas: respuestas con datos REALES del corpus
        if td == "GetPos":
            await self.send_iq(
                f'<ctl a="{self.pos[1]}" p="{self.pos[0]}" t="p" td="Pos" valid="1" />')
        elif td == "GetCleanState":
            await self._push_clean_st({"charging": "h", "idle": "i", "paused": "p",
                                       "cleaning": "s", "returning": "s"}[self.mode])
        elif td == "GetChargeState":
            typ = {"charging": "SlotCharging", "returning": "Going"}.get(self.mode, "Idle")
            await self.push_ctl(
                f'<ctl td="ChargeState" ts="{self._ts()}">'
                f'<charge h="" r="" s="" type="{typ}" /></ctl>')
        elif td == "GetBatteryInfo":
            await self.push_ctl(
                f'<ctl td="BatteryInfo" ts="{self._ts()}">'
                f"<battery power=\"{self.battery}\" /></ctl>")
        elif td == "GetMapM":
            spec = self.corpus.get("spec_mapm") or {}
            await self.send_iq(
                f'<ctl c="{spec.get("c", 8)}" h="{spec.get("h", 100)}" i="{self.map_id}" '
                f'id="{a.get("id", ctl_id())}" m="{spec.get("m", "")}" '
                f'p="{spec.get("p", 50)}" r="{spec.get("r", 8)}" ret="ok" '
                f'w="{spec.get("w", 100)}" />')
        elif td == "PullMP":
            pid = a.get("pid", "")
            piece = (self.corpus.get("piezas") or {}).get(pid)
            if piece:
                await self.push_ctl(
                    f'<ctl i="{self.map_id}" p="{piece}" pid="{pid}" td="MapP" />')
            else:
                await self.ack(a.get("id", ctl_id()))
        elif td == "GetMapSet":
            # vw = muros + zonas no-fregar; sa/ar = vacío (como el corpus)
            await self._push_mapset(a.get("tp", "vw"))
        elif td == "AddM":
            tp, m_val = a.get("tp", "vw"), a.get("m", "")
            if m_val:
                entry = {"mid": str(random.randrange(100, 999)), "p": m_val}
                (self.walls if tp == "vw" else self.zones).append(entry)
                await self.ack(a.get("id", ctl_id()))
                await self._push_mapset(tp)
            else:
                await self.ack(a.get("id", ctl_id()), errs="1")
        elif td == "DelM":
            tp, mid = a.get("tp", "vw"), a.get("mid", "")
            lst = self.walls if tp == "vw" else self.zones
            lst[:] = [e for e in lst if e["mid"] != mid]
            await self.ack(a.get("id", ctl_id()))
            await self._push_mapset(tp)
        elif td in ("GetCleanStatistics", "GetCleanSum"):
            s = self.stats
            await self.send_iq(
                f'<ctl id="{a.get("id", ctl_id())}" ret="ok" td="{td}" '
                f'a="{s["area"]}" t="{s["time"]}" c="{s["count"]}" />')
        elif td == "GetTr":
            tr = self.corpus.get("trace_varint") or ""
            await self.push_ctl(
                f'<ctl td="trace" tf="0" tr="{tr}" trid="605959" tt="0" />')
        elif td == "GetLifeSpan":
            comp = a.get("type", "Brush")
            life = (self.corpus.get("lifespans") or {}).get(comp, {"left": 100, "total": 100})
            await self.send_iq(
                f'<ctl id="{a.get("id", ctl_id())}" left="{life["left"]}" ret="ok" '
                f'total="{life["total"]}" type="{comp}" />')
        elif td == "GetError":
            await self.ack(a.get("id", ctl_id()))
        elif td == "GetVersion":
            await self.send_iq(
                f'<ctl id="{a.get("id", ctl_id())}" name="FW" ret="ok" td="GetVersion">'
                f'<FW ver="2.1.19-mock930" /></ctl>')
        elif td == "GetChargerPos":
            ch = self.corpus.get("charger_pos") or {}
            await self.send_iq(
                f'<ctl id="{a.get("id", ctl_id())}" ret="ok" td="GetChargerPos" '
                f'p="{ch.get("p", "-37,-595")}" a="{ch.get("a", 0)}" valid="1" />')
        elif td == "GetSleepStatus":
            await self.push_ctl(f'<ctl st="0" td="SleepStatus" ts="{self._ts()}" />')
        elif td == "SetMapAutoReport":
            await self.ack(a.get("id", ctl_id()))
            # el real re-envía piezas al armarlo: replay de las piezas del corpus
            for pid, piece in (self.corpus.get("piezas") or {}).items():
                await self.push_ctl(
                    f'<ctl i="{self.map_id}" p="{piece}" pid="{pid}" td="MapP" />')
                await asyncio.sleep(0.15)
        elif td in ("PlaySound", "SetTime", "GetTime", "SetVolume", "GetVolume",
                    "GetLanguages", "GetActiveLang", "SetActiveLang", "GetSched",
                    "SetOnOff", "GetOnOff", "GetNetInfo", "SetWaterPermeability",
                    "GetWaterPermeability", "GetCleanSpeed", "SetCleanSpeed",
                    "GetBlockTime", "SetBlockTimeState", "GetSilentModeState",
                    "SetSilentModeState", "ResetLifeSpan", "GetMapSet", "GetMapSt"):
            await self.ack(a.get("id", ctl_id()))
        else:
            await self.ack(a.get("id", ctl_id()))

    async def _push_clean_st(self, st: str) -> None:
        await self.push_ctl(
            f'<ctl a="0" l="1" s="0" t="" td="CleanSt" st="{st}" />')

    async def _push_mapset(self, tp: str) -> None:
        if tp == "vw":
            children = "".join(
                f'<m mid="{w["mid"]}" n="" p="{w["p"]}" />' for w in self.walls)
            children += "".join(
                f'<m mid="{z["mid"]}" n="" p="{z["p"]}" t="mw" />' for z in self.zones)
        else:
            children = '<m mid="" n="" p="" />'
        await self.push_ctl(
            f'<ctl msid="0" td="MapSet" tp="{tp}" ts="{self._ts()}">{children}</ctl>')

    # ---------- simulación ----------

    async def _push_loop(self) -> None:
        tick = 0
        while True:
            await asyncio.sleep(1.2)
            tick += 1
            if self.mode == "cleaning":
                # replay de la TRAYECTORIA REAL (cíclico)
                self.path_i = (self.path_i + 1) % len(self.path)
                self.pos = self.path[self.path_i]
                if tick % 60 == 0:
                    self.battery = max(5, self.battery - 1)
                if tick % 30 == 0:
                    await self.push_ctl(
                        f'<ctl td="CleanReport" ts="{self._ts()}"><clean a="" l="" rsn="" '
                        f'speed="{self.speed}" st="s" sts="" type="auto" /></ctl>')
            elif self.mode == "returning":
                if tick % 10 == 0:
                    await self.push_ctl(
                        f'<ctl td="ChargeState" ts="{self._ts()}">'
                        f'<charge h="" r="" s="" type="SlotCharging" /></ctl>')
                    self.mode = "charging"
            elif self.mode == "charging":
                if tick % 20 == 0 and self.battery < 100:
                    self.battery = min(100, self.battery + 1)
            # Pos en vivo (el real lo empuja constantemente mientras despierto)
            if self.mode in ("cleaning", "returning"):
                await self.push_ctl(
                    f'<ctl a="{self.pos[1]}" p="{self.pos[0]}" t="p" td="Pos" valid="1" />')


def _attr(stanza: str, name: str) -> str:
    import re

    m = re.search(rf'{name}="([^"]*)"', stanza)
    return m.group(1) if m else ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=5223)
    ap.add_argument("--sn", default="E00010570MOCK9301",
                    help="serial del mock (NO el del robot real, para no chocar)")
    ap.add_argument("--cls", default="115")
    ap.add_argument("--corpus", default="data/mock930-corpus.json")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    corpus_path = Path(args.corpus)
    if not corpus_path.exists():
        alt = Path("/mnt/t/ecovacs") / args.corpus
        if alt.exists():
            corpus_path = alt
    corpus = json.loads(corpus_path.read_text())
    log.info("corpus: %s — %d piezas, %d posiciones, spec %s",
             corpus_path, len(corpus.get("piezas") or {}),
             len(corpus.get("trayectoria") or []),
             (corpus.get("spec_mapm") or {}).get("i", "?"))
    robot = Mock930(args.host, args.port, args.sn, args.cls, corpus)
    try:
        asyncio.run(robot.run())
    except KeyboardInterrupt:
        print("mock 930 fuera")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
