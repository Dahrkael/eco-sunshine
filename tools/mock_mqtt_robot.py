#!/usr/bin/env python3
"""Mock de robot Ecovacs eco-ng (MQTT/JSON) — banco de pruebas del bridge.

Se conecta al broker MQTT de Bumper :8883 como un robot T9 AIVI
({did}@yna5xh/axm), responde a los comandos p2p del puente y emite reports
iot/atr. El mapa usa piezas LZMA sintéticas (mismo formato wire que un robot
real: base64(props+dict+size_u32+lzma)) y la traza usa el formato de 5
bytes/punto — todo según docs/mqtt-json-spec.md.

Uso:
    .venv/bin/python tools/mock_mqtt_robot.py [--host localhost] [--port 8883]
        [--ca-file data/certs/ca.crt] [--did mock-t9-0001] [--cls yna5xh]
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import lzma
import math
import struct
import time
import zlib

import aiomqtt

log = logging.getLogger("mock-robot")

# ---------- geometría del mapa sintético ----------
PIECE_W, PIECE_H = 4, 5          # piezas por eje
CELL_W, CELL_H = 100, 100        # celdas por pieza
PIXEL = 50                       # mm por celda
TOTAL_COLS = PIECE_W * CELL_W    # 400
TOTAL_ROWS = PIECE_H * CELL_H    # 500
MAP_ID = "0"

PATROL = [(-6000, -8000), (6000, -8000), (6000, 8000), (-6000, 8000)]
DOCK_MM = (-6500.0, -9000.0)
SPEED_MMS = 300.0


def build_grid() -> bytearray:
    """Apartamento sintético 400x500: paredes, 3 habitaciones y alfombra."""
    g = bytearray(TOTAL_ROWS * TOTAL_COLS)  # 0 = sin datos

    def cell(r: int, c: int, v: int) -> None:
        if 0 <= r < TOTAL_ROWS and 0 <= c < TOTAL_COLS:
            g[r * TOTAL_COLS + c] = v

    # suelo interior
    for r in range(60, 440):
        for c in range(60, 340):
            g[r * TOTAL_COLS + c] = 1
    # paredes exteriores (grosor 3)
    for c in range(50, 350):
        for t in range(3):
            cell(50 + t, c, 2)
            cell(447 + t, c, 2)
    for r in range(50, 450):
        for t in range(3):
            cell(r, 50 + t, 2)
            cell(r, 347 + t, 2)
    # pared interior vertical (col 200) con hueco-puerta en filas 250-270
    for r in range(50, 450):
        if 250 <= r <= 270:
            continue
        for t in range(3):
            cell(r, 200 + t, 2)
    # pared interior horizontal (fila 300, mitad derecha) con puerta
    for c in range(203, 347):
        if 330 <= c <= 345:
            continue
        for t in range(3):
            cell(300 + t, c, 2)
    # alfombra en la habitación superior derecha
    for r in range(120, 180):
        for c in range(250, 320):
            cell(r, c, 3)
    return g


def encode_piece(data: bytes) -> str:
    """Grid → LZMA wire del robot: base64(props+dict(4)+size_u32(4)+stream)."""
    comp = lzma.compress(data, format=lzma.FORMAT_ALONE,
                         filters=[{"id": lzma.FILTER_LZMA1, "preset": 6}])
    return base64.b64encode(comp[:9] + comp[13:]).decode()


def split_pieces(grid: bytearray) -> tuple[list[str], list[int]]:
    """Grid completo → piezas row-major (pid = row*PIECE_W + col)."""
    pieces_b64: list[str] = []
    crcs: list[int] = []
    for pr in range(PIECE_H):
        for pc in range(PIECE_W):
            piece = bytearray(CELL_W * CELL_H)
            for r in range(CELL_H):
                src = (pr * CELL_H + r) * TOTAL_COLS + pc * CELL_W
                piece[r * CELL_W:(r + 1) * CELL_W] = grid[src:src + CELL_W]
            pieces_b64.append(encode_piece(bytes(piece)))
            crcs.append(zlib.crc32(bytes(piece)))
    return pieces_b64, crcs


class MockRobot:
    def __init__(self, host: str, port: int, ca_file: str, did: str, cls: str, res: str):
        self.host, self.port, self.ca_file = host, port, ca_file
        self.did, self.cls, self.res = did, cls, res
        self.client: aiomqtt.Client | None = None
        self.mode = "idle"            # idle|cleaning|paused|returning|charging
        self.battery = 87
        self.fan = 1                  # 0 silent, 1 standard, 2 strong
        self.water = 2
        self.pos = {"x": DOCK_MM[0], "y": DOCK_MM[1], "a": 0.0}
        self.trace: list[tuple[float, float]] = [DOCK_MM]
        self.grid = build_grid()
        self.pieces, self.crcs = split_pieces(self.grid)
        # mapa chunked (getMapInfo): b64 del stream LZMA estándar del grid
        comp = lzma.compress(bytes(self.grid), format=lzma.FORMAT_ALONE,
                             filters=[{"id": lzma.FILTER_LZMA1, "preset": 6}])
        self._chunked_b64 = base64.b64encode(comp).decode()
        # mapsets: 3 habitaciones + 1 muro + 1 zona no-fregar (spec §4.5)
        self.map_msid = "0"
        self.rooms = [
            {"mssid": "1", "name": "Salón", "center": "-1500,2000", "index": 0},
            {"mssid": "2", "name": "Cocina", "center": "2500,1500", "index": 1},
            {"mssid": "3", "name": "Dormitorio", "center": "500,-3500", "index": 2},
        ]
        self.walls = [{"mssid": "7", "name": "muro pasillo",
                       "value": "0,-500;0,4500"}]
        self.zones = [{"mssid": "8", "name": "alfombra", "kind": "mw",
                       "value": "-2500,1000;-1000,2500"}]
        self.stats = {"time": 3600 * 42, "area": 45_000_000, "count": 17}
        log.info("mapa sintético: %d piezas, grid %dx%d", len(self.pieces), TOTAL_COLS, TOTAL_ROWS)

    # ---------- conexión ----------

    async def run(self) -> None:
        import ssl

        ctx = ssl.create_default_context(cafile=self.ca_file)
        ctx.check_hostname = False
        if hasattr(ssl, "VERIFY_X509_STRICT"):
            ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
        while True:
            try:
                self.client = aiomqtt.Client(
                    hostname=self.host, port=self.port,
                    identifier=f"{self.did}@{self.cls}/{self.res}",
                    username=f"SN-{self.did}",   # el plugin lo registra como SN del bot
                    password="mock",  # compat con Bumper/hbmqtt externo (exigía
                                      # password con username; amqtt ya no)
                    tls_context=ctx,
                )
                async with self.client:
                    # 12 niveles exactos (spec §2): '+' = UN nivel. El filtro
                    # viejo tenía 11 y solo matcheaba por el bug de hbmqtt
                    # ('+' se tragaba niveles con '/')
                    await self.client.subscribe(
                        f"iot/p2p/+/+/+/+/{self.did}/{self.cls}/{self.res}/q/+/+", qos=0)
                    log.info("conectado como %s@%s/%s", self.did, self.cls, self.res)
                    await self._announce()
                    await asyncio.gather(self._cmd_loop(), self._tick_loop())
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("desconectado (%s) — reintento en 3s", e)
                await asyncio.sleep(3)

    async def _announce(self) -> None:
        """Primer chorro de reports para que el bridge adopte el robot."""
        await self.publish_report("onBattery", {"value": self.battery, "isLow": False})
        await self.publish_report("onMajorMap", self._major_map())
        await self.publish_report("onPos", self._pos_payload())
        await self.publish_report("onCleanInfo_V2", self._clean_info())
        await self.publish_report("onChargeState", {"mode": "idle"})

    # ---------- topics ----------

    async def publish_report(self, name: str, data: dict) -> None:
        if self.client:
            await self.client.publish(
                f"iot/atr/{name}/{self.did}/{self.cls}/{self.res}/j",
                json.dumps({"ret": "ok", **data}), qos=0)

    async def _cmd_loop(self) -> None:
        assert self.client is not None
        async for msg in self.client.messages:
            parts = str(msg.topic).split("/")
            if len(parts) != 12 or parts[9] != "q":
                continue
            cmd, fid, fcls, fres = parts[2], parts[3], parts[4], parts[5]
            reqid, fmt = parts[10], parts[11]
            try:
                payload = json.loads(msg.payload)
            except json.JSONDecodeError:
                payload = {}
            log.info("<< %s %s", cmd, payload.get("body", {}).get("data", {}))
            resp = self.handle(cmd, payload.get("body", {}).get("data", {}) or {})
            topic = (f"iot/p2p/{cmd}/{self.did}/{self.cls}/{self.res}/"
                     f"{fid}/{fcls}/{fres}/p/{reqid}/{fmt}")
            await self.client.publish(topic, json.dumps(resp), qos=0)
            log.debug(">> %s → %.120s", cmd, resp)

    # ---------- lógica del robot ----------

    def _pos_payload(self) -> dict:
        return {"mid": MAP_ID, "deebotPos": self.pos,
                "chargePos": [{"x": DOCK_MM[0], "y": DOCK_MM[1], "a": 0}]}

    def _clean_info(self) -> dict:
        motion = {"idle": "idle", "cleaning": "working", "paused": "paused",
                  "returning": "going", "charging": "idle"}[self.mode]
        return {"state": "clean" if self.mode == "cleaning" else "idle",
                "trigger": "none",
                "cleanState": {"mode": "auto", "motionState": motion, "router": "",
                               "content": {"type": "auto", "value": "", "count": 1,
                                           "donotClean": 0, "total": int(time.time() % 3600)}}}

    def _major_map(self) -> dict:
        return {"mid": MAP_ID, "type": "ol", "pieceWidth": PIECE_W,
                "pieceHeight": PIECE_H, "cellWidth": CELL_W, "cellHeight": CELL_H,
                "pixel": PIXEL, "value": ",".join(str(c) for c in self.crcs)}

    def _trace_payload(self, start: int, count: int) -> dict:
        pts = self.trace
        page = pts[start:start + count]
        buf = b"".join(
            struct.pack(">hhB", int(y) + 2000, int(x) + 2000, 0)
            for x, y in page
        )
        comp = lzma.compress(buf, format=lzma.FORMAT_ALONE,
                             filters=[{"id": lzma.FILTER_LZMA1, "preset": 6}])
        return {"tid": "42", "traceStart": start, "pointCount": len(page),
                "totalCount": len(pts),
                "traceValue": base64.b64encode(comp).decode()}

    # ---- formato chunked (T8/N8): getMapInfo divide el b64 en N partes ----

    def _chunked_response(self, data) -> dict:
        """Responde UN chunk por llamada (index), como un T8/N8 real.

        Spec §4.5: concatenar `value` por index → base64 → LZMA estándar →
        grid totalWidth×totalHeight. Los chunks son TROZOS del b64.
        """
        mtype = data.get("type", "ol") if isinstance(data, dict) else "ol"
        b64 = self._chunked_b64
        chunk_chars = 4000
        total_chunks = (len(b64) + chunk_chars - 1) // chunk_chars
        idx = int(data.get("index", 0)) if isinstance(data, dict) else 0
        if idx >= total_chunks:
            return {"ret": "fail", "errno": 2, "error": "index fuera de rango"}
        return {"ret": "ok", "mid": MAP_ID, "type": mtype, "index": idx,
                "totalCount": total_chunks,
                "value": b64[idx * chunk_chars:(idx + 1) * chunk_chars],
                "pixel": PIXEL, "width": TOTAL_COLS, "height": TOTAL_ROWS,
                "totalWidth": TOTAL_COLS, "totalHeight": TOTAL_ROWS,
                "startX": 0, "startY": 0}

    def _mapset_payload(self, tp: str) -> dict:
        if tp == "ar":
            return {"ret": "ok", "mid": MAP_ID, "msid": self.map_msid, "type": "ar",
                    "count": len(self.rooms), "subsets": [dict(r) for r in self.rooms]}
        entries = self.walls if tp == "vw" else self.zones
        subs = [{"mssid": e["mssid"], "name": e["name"], "index": i}
                for i, e in enumerate(entries)]
        return {"ret": "ok", "mid": MAP_ID, "msid": self.map_msid, "type": tp,
                "count": len(subs), "subsets": subs}

    def _set_map_subset(self, data: dict) -> dict:
        """setMapSubSet: value vacío+mssid = borrar; value con coords = crear."""
        tp = str(data.get("type", "vw"))
        mssid = str(data.get("mssid", ""))
        value = str(data.get("value", ""))
        entries = self.walls if tp == "vw" else self.zones
        if mssid and not value:  # borrar
            before = len(entries)
            entries[:] = [e for e in entries if e["mssid"] != mssid]
            if len(entries) == before:
                return {"ret": "fail", "errno": 2, "error": "mssid desconocido"}
        elif value:  # crear
            new_id = str(int(max((int(e["mssid"]) for e in entries), default=100)) + 1)
            entries.append({"mssid": new_id, "name": str(data.get("name", "")),
                            "kind": tp, "value": value})
            mssid = new_id
        else:
            return {"ret": "fail", "errno": 3, "error": "value o mssid requerido"}
        return {"ret": "ok", "mid": MAP_ID, "msid": self.map_msid,
                "mssid": mssid, "type": tp}

    def handle(self, cmd: str, data) -> dict:
        if cmd in ("getMapSet",):
            tp = data.get("type", "ar") if isinstance(data, dict) else "ar"
            return self._mapset_payload(tp)
        if cmd in ("getMapSubSet",):
            tp = data.get("type", "vw") if isinstance(data, dict) else "vw"
            mssid = str(data.get("mssid", "")) if isinstance(data, dict) else ""
            entries = self.walls if tp == "vw" else self.zones
            for e in entries:
                if e["mssid"] == mssid:
                    return {"ret": "ok", "mid": MAP_ID, "msid": self.map_msid,
                            "mssid": mssid, "type": tp, "value": e["value"]}
            return {"ret": "fail", "errno": 2, "error": "mssid desconocido"}
        if cmd in ("setMapSubSet",):
            return self._set_map_subset(data if isinstance(data, dict) else {})
        if cmd in ("getTotalStats", "getStats"):
            return {"ret": "ok", "tid": "1", "type": "auto",
                    "time": self.stats["time"], "area": self.stats["area"],
                    "count": self.stats["count"], "mapCount": self.stats["count"]}
        if cmd in ("getBattery",):
            return {"ret": "ok", "value": self.battery, "isLow": self.battery < 15}
        if cmd in ("getPos", "getPos_V2"):
            return {"ret": "ok", **self._pos_payload()}
        if cmd in ("getMajorMap",):
            return {"ret": "ok", **self._major_map()}
        if cmd in ("getMinorMap",):
            # endurecido: valida type/mid como haría un robot real — el
            # bridge debe mandar el mid CRUDO (sin namespace interno)
            if not isinstance(data, dict):
                return {"ret": "fail", "errno": 3, "error": "payload inválido"}
            if data.get("type") not in ("ol", "st", "wa"):
                return {"ret": "fail", "errno": 3, "error": f"type inválido: {data.get('type')}"}
            if str(data.get("mid", "")) != MAP_ID:
                return {"ret": "fail", "errno": 3,
                        "error": f"mid desconocido: {data.get('mid')} (esperado {MAP_ID})"}
            idx = data.get("pieceIndex", -1)
            if not isinstance(idx, int) or not (0 <= idx < len(self.pieces)):
                return {"ret": "fail", "errno": 2, "error": f"pieceIndex fuera de rango: {idx}"}
            return {"ret": "ok", "mid": MAP_ID, "type": "ol", "pieceIndex": idx,
                    "pieceValue": self.pieces[idx]}
        if cmd in ("getMapInfo",):
            return self._chunked_response(data)
        if cmd in ("getMapTrace",):
            start = int(data.get("traceStart", 0)) if isinstance(data, dict) else 0
            count = int(data.get("pointCount", 400)) if isinstance(data, dict) else 400
            return {"ret": "ok", **self._trace_payload(start, count)}
        if cmd in ("getCleanInfo", "getCleanInfo_V2"):
            return {"ret": "ok", **self._clean_info()}
        if cmd in ("getChargeState",):
            mode = {"charging": "slot_charging", "returning": "going"}.get(
                self.mode, "idle")
            return {"ret": "ok", "mode": mode, "isCharging": mode == "slot_charging"}
        if cmd in ("getLifeSpan",):
            return {"ret": "ok", "brush": 5400, "sideBrush": 2100,
                    "heap": 7800, "unitCare": 9000}
        if cmd in ("getSpeed",):
            return {"ret": "ok", "speed": self.fan}
        if cmd in ("setSpeed",):
            self.fan = int(data.get("speed", 1)) if isinstance(data, dict) else 1
            return {"ret": "ok", "speed": self.fan}
        if cmd in ("getWaterInfo",):
            return {"ret": "ok", "amount": self.water, "type": 0,
                    "sweepType": 1, "enable": True}
        if cmd in ("setWaterInfo",):
            self.water = int(data.get("amount", 2)) if isinstance(data, dict) else 2
            return {"ret": "ok", "amount": self.water}
        if cmd in ("getError",):
            return {"ret": "ok", "error": []}
        if cmd in ("getNetInfo",):
            return {"ret": "ok", "ip": "192.168.1.50", "mac": "AA:BB:CC:DD:EE:FF",
                    "ssid": "CasaMock", "rssi": -52, "sn": f"SN-{self.did}",
                    "wkVer": "0.25.16-mock"}
        if cmd in ("getVolume",):
            return {"ret": "ok", "volume": 5, "total": 10, "type": "system"}
        if cmd in ("getBlock",):
            return {"ret": "ok", "enable": False, "start": "22:00", "end": "08:00"}
        if cmd in ("getSched", "getSched_V2"):
            return {"ret": "ok", "scheds": []}
        if cmd in ("clean", "clean_V2"):
            act = data.get("act", "start") if isinstance(data, dict) else "start"
            self.mode = {"start": "cleaning", "resume": "cleaning",
                         "pause": "paused", "stop": "idle"}.get(act, "idle")
            return {"ret": "ok"}
        if cmd in ("charge",):
            self.mode = "returning"
            return {"ret": "ok"}
        if cmd in ("playSound", "resetLifeSpan", "setVolume", "setBlock",
                   "setSched", "setBlockTimeState", "setRelocationState"):
            return {"ret": "ok"}
        return {"ret": "ok", "mock": True, "cmd": cmd}

    # ---------- simulación ----------

    async def _tick_loop(self) -> None:
        seg = 0
        while True:
            await asyncio.sleep(1.0)
            seg += 1
            self._advance(SPEED_MMS)
            if self.mode == "cleaning":
                self.battery = max(5, self.battery - 1)
            elif self.mode == "charging":
                self.battery = min(100, self.battery + 2)
            await self.publish_report("onPos", self._pos_payload())
            if seg % 10 == 0:
                await self.publish_report("onBattery",
                                          {"value": self.battery, "isLow": self.battery < 15})
                await self.publish_report("onCleanInfo_V2", self._clean_info())
            if self.mode == "returning" and self._near_dock():
                self.mode = "charging"
                await self.publish_report("onChargeState", {"mode": "slot_charging"})
                await self.publish_report("onCleanInfo_V2", self._clean_info())

    def _near_dock(self) -> bool:
        return math.hypot(self.pos["x"] - DOCK_MM[0], self.pos["y"] - DOCK_MM[1]) < 150

    def _advance(self, dist_mm: float) -> None:
        """Mueve el robot según su modo (patrulla rectangular o vuelta a base)."""
        x, y = self.pos["x"], self.pos["y"]

        if self.mode in ("charging", "paused", "idle"):
            return

        if self.mode == "returning":
            tx, ty = DOCK_MM
        else:  # cleaning → siguiente vértice de la patrulla
            tx, ty = min(PATROL, key=lambda p: math.hypot(p[0] - x, p[1] - y))
            # si ya está encima, avanza al siguiente en el ciclo
            if math.hypot(tx - x, ty - y) < dist_mm:
                i = (PATROL.index((tx, ty)) + 1) % len(PATROL)
                tx, ty = PATROL[i]

        dx, dy = tx - x, ty - y
        d = math.hypot(dx, dy) or 1.0
        step = min(dist_mm, d)
        self.pos = {"x": x + dx / d * step, "y": y + dy / d * step,
                    "a": math.degrees(math.atan2(dy, dx))}
        if self.mode == "cleaning":
            self.trace.append((self.pos["x"], self.pos["y"]))
            if len(self.trace) > 4000:
                self.trace = self.trace[-3000:]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=8883)
    ap.add_argument("--ca-file", default="/mnt/t/ecovacs/data/certs/ca.crt")
    ap.add_argument("--did", default="mock-t9-0001")
    ap.add_argument("--cls", default="yna5xh")
    ap.add_argument("--res", default="axm")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    robot = MockRobot(args.host, args.port, args.ca_file, args.did, args.cls, args.res)
    try:
        asyncio.run(robot.run())
    except KeyboardInterrupt:
        print("mock robot fuera")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
