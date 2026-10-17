# SPEC: Ecovacs MQTT/JSON protocol — for implementation in ecovacs-bridge

> Sources: Ecovacs Home v3.16.1 APK decompile, Bumper source, ecovacs-deebot.js,
> ecovacs-stack, own corpus (sessions 1-3 with an OZMO 930 over XMPP).
> This document is the implementation spec, not an analysis.

## 0. Executive summary

Modern Ecovacs robots (T8+, 950+, N8, X1) communicate over MQTT with:
- **Reports** (robot → client): `iot/atr/` topics
- **Commands** (client → robot): `iot/p2p/` topics with a JSON envelope
- **Maps**: the same LZMA as the XMPP era, but in JSON fields instead of XML

**Bumper already has the entire transport**: MQTT broker :8883, a helperbot
that publishes to p2p topics, and report relaying. Only the JSON vocabulary
is missing.

---

## 1. Addresses and identity

### 1.1 Robot connection to the broker

The robot connects to the MQTT broker (Bumper :8883 with DNS redirect) with:
- **Client ID**: `{sn}@{class}/{resource}` (e.g. `b11fceaf-1234@yna5xh/axm`)
  - `sn` = device UUID (assigned by the Ecovacs server)
  - `class` = 6-character class ID (e.g. `yna5xh` = T9 AIVI)
  - `resource` = short string (e.g. `axm`, `atom`)
- **Authentication**: no authentication when connecting to Bumper (allow_anonymous)

### 1.2 Bridge connection (us)

Our bridge connects like Bumper's "helperbot":
- **Client ID**: `bridge@bumper/bridge`
- Subscribes to: `iot/p2p/+/+/+/+/bridge/bumper/bridge/+/+/+` (responses)
- Subscribes to: `iot/atr/+/+/+/+` (reports from all robots)

---

## 2. MQTT topics

### 2.1 General structure

```
iot/<domain>/<name>/<from-id>/<from-class>/<from-res>/<to-id>/<to-class>/<to-res>/<type>/<rand>/<fmt>
```

| Level | Description | Example |
|---|---|---|
| `domain` | `p2p` (commands), `atr` (reports), `cfg` (config) | `p2p` |
| `name` | command or report name | `clean_V2`, `onBattery` |
| `from-*` | sender identity | `bridge/bumper/bridge` |
| `to-*` | recipient identity | `{sn}/{cls}/{res}` |
| `type` | `q`=request, `p`=response, `e`=error, `m`=message | `q` |
| `rand` | random correlation token | `a1b2` |
| `fmt` | `j`=JSON, `x`=XML, `b`=binary | `j` |

### 2.2 Topics the bridge PUBLISHES (commands to the robot)

```
iot/p2p/{command}/bridge/bumper/bridge/{robot-sn}/{robot-cls}/{robot-res}/q/{request-id}/j
```

Payload: the JSON envelope (see §3).

### 2.3 Topics the bridge LISTENS to

**Robot reports:**
```
iot/atr/{report-name}/{robot-sn}/{robot-cls}/{robot-res}/j
```

**Command responses:**
```
iot/p2p/{command}/{robot-sn}/{robot-cls}/{robot-res}/bridge/bumper/bridge/p/{request-id}/j
```

---

## 3. JSON message format

### 3.1 Command envelope (bridge → robot)

```json
{
  "header": {
    "pri": 2,
    "ts": "1697312345678",
    "tzm": 480,
    "ver": "0.0.22"
  },
  "body": {
    "data": {
      "...command parameters...": "...",
      "bdTaskID": "1697312345678421"
    }
  }
}
```

| Field | Value | Description |
|---|---|---|
| `pri` | `2` | fixed priority |
| `ts` | epoch ms as a string | timestamp |
| `tzm` | timezone minutes | Spain = `480` (UTC+8... no, UTC+1 = 60) |
| `ver` | `"0.0.22"` | protocol version |
| `bdTaskID` | `{ts}{3 random digits}` | telemetry correlation (optional but recommended) |

### 3.2 Response envelope (robot → bridge)

```json
{
  "ret": "ok",
  "...result fields...": "..."
}
```

Error:
```json
{
  "ret": "fail",
  "errno": 3,
  "error": "reason"
}
```

### 3.3 Async reports (robot → all subscribers)

Same format as a response, on the `iot/atr/onXxx/...` topic:
```json
{"ret":"ok", "value": 85, "isLow": false}
```

---

## 4. Full command set

### 4.1 Cleaning control

#### `clean` / `clean_V2`
```json
// v1
{"act": "start", "mode": "auto", "type": "auto", "count": 1, "donotClean": 0}
// v2 (recommended)
{"act": "start", "mode": "auto", "router": "", "bdTaskID": "...",
 "content": {"type": "auto", "value": "", "count": 1, "donotClean": 0}}
```

| `act` | Meaning |
|---|---|
| `start` | start |
| `pause` | pause |
| `resume` | resume |
| `stop` | stop |

| `type` | Meaning |
|---|---|
| `auto` | full cleaning |
| `spot` | spot cleaning |
| `customArea` | specific area (coords in `content.value` = `"x1,y1,x2,y2"`) |
| `spotArea` | specific rooms (`content.value` = `"mid1,mid2"` ) |
| `singlePoint` | single point |
| `aiclean` | selective AI |
| `mapPoint` | a point on the map |

#### `charge`
```json
{"act": "go"}
```

#### `move` (remote control)
```json
{"act": "turn", "a": 30}   // turn 30°
{"act": "forward"}           // move forward
```

#### `stop`
```json
{"act": "stop"}
```

### 4.2 State and properties

#### `getBattery`
```json
// request: {}
// response:
{"ret":"ok", "value": 85, "isLow": false}
```

#### `getChargeState`
```json
{"ret":"ok", "mode": "slot_charging"}  // slot_charging | idle | going | wire_charging
```

#### `getCleanInfo` / `getCleanInfo_V2`
```json
{"ret":"ok", "state":"idle", "trigger":"none",
 "cleanState": {"mode":"auto", "motionState":"idle", "router":"", "cid":"",
   "cmode":0, "borderState":0,
   "content":{"type":"auto","value":"","count":0,"donotClean":0,
              "areaSts":0,"countValue":"","start":"","total":0,
              "subContent":{...}}}}
```

#### `getSpeed`
```json
{"ret":"ok", "speed": 2}  // 0=silent, 1=standard, 2=strong (varies by model)
```

#### `setSpeed`
```json
{"speed": 1}
```

#### `getWaterInfo`
```json
{"ret":"ok", "amount": 2, "type": 0, "sweepType": 1, "enable": true}
```

#### `setWaterInfo`
```json
{"amount": 2, "type": 0, "sweepType": 1, "enable": true}
```

#### `getLifeSpan`
```json
// request:
["brush", "sideBrush", "heap", "unitCare", "dModule"]
// response:
{"ret":"ok", "brush": 1800, "sideBrush": 120, "heap": 6200, "unitCare": 0}
```
Values are **minutes remaining**.

#### `resetLifeSpan`
```json
{"component": "brush"}
```

#### `getVolume` / `setVolume`
```json
{"ret":"ok", "volume": 5, "total": 10, "type": "system"}
// set:
{"volume": 3, "type": "system"}
```

#### `getBlock` / `setBlock` (Do not disturb / DND)
```json
{"ret":"ok", "enable": false, "start": "22:00", "end": "08:00"}
// set:
{"enable": true, "start": "22:00", "end": "08:00"}
```

#### `getNetInfo`
```json
{"ret":"ok", "ip": "192.168.1.50", "mac": "AA:BB:CC:DD:EE:FF", "ssid": "MyWiFi", "rssi": -52, "sn": "...", "wkVer": "0.25.16"}
```

#### `getStats` / `getTotalStats`
```json
{"ret":"ok", "cid": 123, "type": "auto", "start": 1697000000, "end": 1697003600,
 "time": 3600, "area": 45000000, "mapCount": 1, "aiopen": 1,
 "aitypes": [5,3,6,4,9], "avoidCount": 24, "enablePowerMop": false, "powerMopType": 2}
```

#### `getError`
```json
{"ret":"ok", "error": []}  // empty array = no errors
```

#### `getPos` / `getPos_V2`
```json
// request v1: ["chargePos", "deebotPos"]
// response:
{"ret":"ok", "mid":"0",
 "deebotPos": {"x": 500, "y": 1200, "a": 90},
 "chargePos": [{"x": 100, "y": 100, "a": 0}]}
```
Positions in **mm** from the map origin. `a` = angle in degrees.

#### `getMapState`
```json
{"ret":"ok", "mode": "training", "state": "built", "progress": 100, "times": 1}
```
States: `building`, `built`. Progress 0-100 while the map is being built.

#### `getRelocationState` / `setRelocationState`
```json
{"ret":"ok", "mode": "", "state": "", "isHasMap": true}
// set (re-localize after moving to another floor):
{"mode": "manu"}
```

#### `getSleep` / `setSleep`
```json
{"ret":"ok", "enable": false}
```

#### `getBreakPoint` / `setBreakPoint` (breakpoint resume)
```json
{"ret":"ok", "enable": true}
```

#### `getCarpertPressure` / `setCarpertPressure`
Carpet pressure (compatible models).

### 4.3 Sound

#### `playSound`
```json
{"sid": 30, "count": 1}
```
`sid=30` = "I am here" (locate the robot).

### 4.4 Schedules

#### `getSched` / `getSched_V2`
```json
{"ret":"ok", "scheds": [
  {"id":"sched_0", "enable":true, "h":8, "m":0, "repeat":"1,1,1,1,1,0,0",
   "content":{"type":"auto","value":"","count":1,"donotClean":0}}
]}
```

#### `setSched` / `setSched_V2`
```json
{"id":"sched_0", "enable":true, "h":8, "m":0, "repeat":"1,1,1,1,1,0,0",
 "content":{"type":"auto","value":"","count":1,"donotClean":0}}
```

### 4.5 Map

#### `getMajorMap` (pieces with CRCs — same as the 930's GetMapM)
```json
// request: {}
// response:
{"ret":"ok", "mid":"0", "type":"ol",
 "pieceWidth": 4, "pieceHeight": 5,
 "cellWidth": 100, "cellHeight": 100,
 "pixel": 100,
 "value": "crc1,crc2,crc3,..."}
```
`value` = one CRC32 per piece, comma-separated, row-major order.

#### `getMinorMap` (single piece)
```json
// request:
{"type":"ol", "mid":"0", "pieceIndex": 7}
// response:
{"ret":"ok", "mid":"0", "type":"ol", "pieceIndex":7,
 "pieceValue":"<base64(LZMA)>"}
```
Decodes to `cellWidth × cellHeight` bytes (same as the 930's MapP).

#### `getMapInfo` (chunked whole map — N8/T8 family)
```json
// request: {"type":"ol"}
// response (multiple chunks):
{"mid":"0", "type":"ol", "index":0, "totalCount":3,
 "value":"<base64 chunk>", "crc":"...", "pixel":5,
 "width":100, "height":100, "totalWidth":500, "totalHeight":500,
 "startX":0, "startY":0}
```
Concatenate `value` by `index` → base64 → LZMA → `width×height` grid.

#### `getMapSet` / `getMapSubSet` (virtual walls, zones, rooms)
```json
// getMapSet:
{"type":"ar", "mid":"0"}  // ar=rooms, vw=virtual walls, mw=no-mop zones
// response:
{"ret":"ok", "mid":"0", "msid":"0", "type":"ar", "count":3,
 "subsets":[
   {"mssid":"1", "totalCount":204, "name":"Living room", "subtype":"0",
    "connections":"...", "index":0, "center":"250,300", "cleanset":"1,0,1"}]}

// getMapSubSet (paginated coordinates):
{"mid":"0", "msid":"0", "mssid":"1", "type":"ar", "seq":0, "count":50, "seqIndex":0}
// response:
{"ret":"ok", "mssid":"1", "value":"x1,y1;x2,y2;..."}
```

#### `getMapTrace` (cleaning trace)
```json
// request:
{"traceStart": 0, "pointCount": 400}
// response:
{"ret":"ok", "tid":"12345", "traceStart":0, "pointCount":400, "totalCount":1500,
 "traceValue":"<base64(LZMA)>"}
```
Decodes to 5-bytes-per-point records:
- bytes 0-1: int16 BE `y + 2000`
- bytes 2-3: int16 BE `x + 2000`
- byte 4: bit7 = discontinuity, bit0 = point type

#### `getCachedMapInfo` / `setCachedMapInfo` (multi-map)
```json
// get:
{"ret":"ok", "maps":[
  {"mid":"0", "name":"Floor 1", "using":true, "index":0, "status":1,
   "reMid":"", "backupId":"", "built":true}]}
// set (select the active map):
{"mid":"1"}
```

#### `getMultiMapState` / `setMultiMapState`
Enables/disables the robot's multi-map mode.

#### `clearMap`
```json
{"mid":"0"}
```

### 4.6 Station / Auto-empty / ACS

#### `getAutoEmpty` / `setAutoEmpty` / `autoEmpty`
```json
{"act": "open"}  // open/close the bag
{"enable": true} // enable auto-empty
```

#### `getStationInfo` / `getStationState`
Dock state (water levels, bag, etc.)

### 4.7 AI / AIVI

#### `getRecognization` / `setRecognization`
Object types to recognize/avoid.

#### `avoidObject` / `onAvoidObject`
Avoid detected objects.

---

## 5. Async reports (robot → bridge)

The robot publishes to `iot/atr/{name}/...` without anyone asking:

| Report | Data | When |
|---|---|---|
| `onBattery` | `{value: 85, isLow: false}` | battery change |
| `onChargeState` | `{mode: "slot_charging"}` | charge state change |
| `onCleanInfo(_V2)` | full cleaning state | state change |
| `onSpeed` | `{speed: 2}` | power level change |
| `onWaterInfo` | `{amount: 2, ...}` | water level change |
| `onPos` | `{mid, deebotPos, chargePos}` | movement (live!) |
| `onMajorMap` | piece CRCs | map change |
| `onMapTrace` | trace | during cleaning |
| `onMapState` | `{mode, state, progress}` | map building |
| `onError` | errors | error |
| `onEvt` | misc events | see the code table |
| `onOta` | `{status, progress}` | firmware update |
| `onSleep` | `{enable}` | sleep mode |
| `onBlock` | DND | do not disturb |
| `onVolume` | `{volume}` | volume |
| `onRelocationState` | re-localization | after moving |
| `onStats` | stats | cleaning finished |
| `onLiveState` | live video (AIVI) | camera |
| `onRecognize` | AI recognition | detection |

### `onEvt` codes (partial)
| code | Meaning |
|---|---|
| 1007 | mop pro connected |
| 1015 | ? |
| 1052 | mop reminder |
| 1068 | can't localize, returning to dock |
| 1071 | re-localization succeeded |
| 1088 | localization failure |
| 20003 | unsupported task type |
| 20011 | setSched failure |

---

## 6. Map format — technical detail

### 6.1 LZMA encoding (XMPP era + MQTT gen 1-2)

```
base64(pieceValue) → bytes:
  [0:5]   LZMA props: 5D 00 00 04 00
  [5:9]   uncompressed size (int32 LE) = cellWidth * cellHeight
  [9:]    LZMA1 stream
→ LZMA decompress → byte grid (1 byte/cell)
```

Our `map_pieces.decode_piece()` already implements this.

### 6.1b zstd encoding (T80/T80S/X8 OMNI — gen 3)

The most recent models (2024+) use **zstd** instead of LZMA:
```
base64(pieceValue) → bytes:
  [0:4]   zstd magic: 28 B5 2F FD
  [4:]    zstd stream
→ zstd decompress → same byte grid (1 byte/cell)
```

Auto-detection by magic number:
```python
def decode_piece_auto(data: bytes) -> bytes:
    if data[:4] == b'\x28\xB5\x2F\xFD':  # zstd magic
        import zstandard
        return zstandard.ZstdDecompressor().decompress(data)
    else:  # LZMA (header 5D 00 00 04 00)
        return lzma.decompress(data[0:5] + struct.pack('<Q', 10000) + data[9:],
                              format=lzma.FORMAT_ALONE)
```

Reference: ecovacs-deebot.js issue #592 (T80S OMNI, zstd map data).
The v3.16.1 APK does NOT contain zstd — it postdates it. The decompressed
grid format is identical; only the algorithm changes.

### 6.2 Piece assembly (same as the 930)

```
piece grid: pieceWidth × pieceHeight pieces
each piece: cellWidth × cellHeight cells
total grid: (pieceWidth*cellWidth) × (pieceHeight*cellHeight)
piece N at: row = N / pieceWidth, col = N % pieceWidth (row-major)
```

Cell values: **model-dependent** — check the map themes in the app.

### 6.3 Trace (5 bytes/point)

```
struct TracePoint {
    int16_be y;      // offset +2000 (mm from map origin)
    int16_be x;      // offset +2000
    uint8  flags;    // bit7=discontinuous, bit0=type
}
```

### 6.4 Differences from the XMPP era

| Aspect | XMPP (930) | MQTT (T9+) |
|---|---|---|
| Command transport | XML `<ctl td="GetMapM">` | JSON `{header, body}` on a p2p topic |
| Pieces | MapP push or PullMP | getMinorMap by pieceIndex |
| Geometry | attrs `w/h/c/r/p` in XML | `pieceWidth/Height, cellWidth/Height, pixel` in JSON |
| CRCs | attr `m` in the GetMapM response | `value` in the getMajorMap response |
| Piece content | base64 attr `p` in MapP | base64 `pieceValue` in getMinorMap |
| LZMA decode | IDENTICAL | IDENTICAL |
| Position | attr `p="x,y"` in Pos | `deebotPos:{x,y,a}` in onPos |
| Position units | mm | mm (identical) |
| Trace | varint LEB128 (930) | 5 bytes/point (T9+) — DIFFERENT! |

---

## 7. MQTT models — registry

### Known class IDs (server-driven, some hardcoded)

| classid | Model | App driver |
|---|---|---|
| `yna5xh` | T9 AIVI | dt9ai_main |
| `x7p4y4` | T9 PRO | dt9_main |
| `vsc5ia` | T8/N8 family | dt8_main |
| `qqy0di` | AA30 (AIRBOT) | lib_iot_client |
| `oxandq` | A680 (AIRBOT) | — |
| `lbw54s` | DE5G | — |

Note: most are assigned dynamically. The robot announces its `class` in
the client ID when connecting to the broker.

### Capabilities by family

| Family | Map | Mopping | Water | AIVI | Auto-empty | Multi-map |
|---|---|---|---|---|---|---|
| T8 base | chunked | ✓ | ✓ | — | — | — |
| T8+/N8+ | chunked | ✓ | ✓ | — | ✓ | ✓ |
| T9 | piece/diff | ✓ | ✓ | — | — | ✓ |
| T9 AIVI | piece/diff | ✓ | ✓ | ✓ | — | ✓ |
| X1 OMNI | piece/diff | ✓ | ✓ | ✓ | ✓ | ✓ |
| T10 OMNI | piece/diff | ✓ | ✓ | ✓ | ✓ | ✓ |

---

## 8. Implementation architecture in the bridge

### 8.1 Bridge MQTT transport (new module)

```python
class MqttTransport:
    """Publishes JSON commands to MQTT robots via Bumper :8883."""

    async def connect(self):
        # connect to the Bumper MQTT broker
        # subscribe to: iot/atr/+/+/+/+ (all reports)
        # subscribe to: iot/p2p/+/+/+/+/bridge/bumper/bridge/+/+/+ (responses)

    async def send_command(self, robot_sn, robot_cls, robot_res,
                          command, params):
        request_id = uuid4().hex[:8]
        topic = f"iot/p2p/{command}/bridge/bumper/bridge/{robot_sn}/{robot_cls}/{robot_res}/q/{request_id}/j"
        payload = {
            "header": {"pri": 2, "ts": str(int(time.time()*1000)),
                      "tzm": 60, "ver": "0.0.22"},
            "body": {"data": {**params, "bdTaskID": f"{int(time.time()*1000)}{random.randint(100,999)}"}}
        }
        await self.client.publish(topic, json.dumps(payload))
        return request_id

    async def on_message(self, topic, payload):
        # routing by topic structure
        parts = topic.split("/")
        if parts[1] == "atr":  # report
            report_name = parts[2]
            data = json.loads(payload)
            await self.handle_report(report_name, data)
        elif parts[1] == "p2p" and parts[9] == "p":  # response
            request_id = parts[10]
            self.resolve_pending(request_id, json.loads(payload))
```

### 8.2 Reusing the MapAssembler

The LZMA decoder is identical. Only these change:
- The piece source (JSON `getMinorMap` instead of XML `MapP`)
- The field names (`pieceValue` instead of `p`)
- The geometry arrives in JSON (`pieceWidth` instead of attr `w`)

```python
# In MapAssembler, add:
def add_piece_json(self, map_id, piece_index, piece_value_b64):
    """Adds a piece from getMinorMap JSON (MQTT robots)."""
    decoded = decode_piece(piece_value_b64)  # same LZMA decoder
    self.maps.setdefault(map_id, {})[piece_index] = decoded
    self.version += 1
```

### 8.3 JSON RobotCommander

```python
class MqttCommander:
    """JSON commands for MQTT robots (T8+)."""

    def __init__(self, transport, robot_sn, robot_cls, robot_res):
        self.transport = transport
        self.robot_id = (robot_sn, robot_cls, robot_res)

    async def cmd(self, name, params=None):
        return await self.transport.send_command(*self.robot_id, name, params or {})

    async def start_clean(self):
        return await self.cmd("clean_V2", {
            "act": "start", "mode": "auto",
            "content": {"type": "auto", "count": 1, "donotClean": 0}})

    async def dock(self):
        return await self.cmd("charge", {"act": "go"})

    async def get_battery(self):
        return await self.cmd("getBattery")

    async def get_major_map(self):
        return await self.cmd("getMajorMap")

    async def get_minor_map(self, piece_index):
        return await self.cmd("getMinorMap", {"pieceIndex": piece_index})
```

---

## 9. Cross-references with other projects

### ecovacs-deebot.js (mrbungle64)
- Dropped XML, focused on MQTT/JSON
- Their supported models: OZMO 920/950, T5, T8, T9, N8, U2, X1
- Their `getMajorMap`/`getMinorMap` → confirms our APK analysis
- Their `getMapTrace` → confirms the 5-bytes-per-point format
- Their `getPos` → confirms the `{x, y, a}` format in mm

### Bumper
- The helperbot ALREADY publishes to `iot/p2p/` with JSON (payloadType "j")
- It only needs the bridge to build the correct payloads
- The broker ALREADY runs on :8883

### ecovacs-stack (m8schmit)
- Their T9 works with `iot/p2p` and `iot/atr` topics
- Confirms the command and event formats
- Their map uses getMajorMap/getMinorMap

---

## 10. Implementation plan (phases)

### Phase A: Transport (2-3h)
1. New module `mqtt_transport.py` — MQTT client for the Bumper broker
2. Publish commands to `iot/p2p/` with the JSON envelope
3. Listen for reports on `iot/atr/`
4. Request/response correlation by request-id

### Phase B: Basic commands (2-3h)
1. MqttCommander with the core commands: clean_V2, charge, getBattery, getPos
2. RobotState updated from reports (onBattery, onCleanInfo, onPos)
3. Serve routes to MqttCommander or XmppCommander depending on robot type

### Phase C: Map (3-4h)
1. getMajorMap → parse CRCs and geometry
2. getMinorMap for each changed piece → decode_piece (already exists)
3. MapAssembler: add add_piece_json
4. Renderer: unchanged (reused)

### Phase D: Multi-protocol integration (2h)
1. Auto-detection: XMPP (XML frames) vs MQTT (JSON on the broker)
2. The bridge starts both transports (XMPP :5223 + MQTT :8883)
3. A single serve.py handles both robot types simultaneously
4. Web panel and MQTT to HA work the same for both

---

## Status: CLOSED — October 15, 2026

This specification has been verified against the following sources and is
considered complete for implementation:

- ✅ Ecovacs APK decompile (v1.4.9) — XMPP/XML protocol, 100+ tds, LZMA map
- ✅ Ecovacs Home APK decompile (v3.16.1) — MQTT/JSON protocol, 50+ commands, 3 map systems
- ✅ Bumper source code — MQTT helperbot, p2p transport, report relay
- ✅ ecovacs-deebot.js — format confirmation, zstd (issue #592), MQTT-only roadmap
- ✅ ecovacs-stack — topic and command confirmation for the T9
- ✅ ecovacs-privacy-control — LZMA decode, piece structure, CRC
- ✅ Own corpus (3 sessions with an OZMO 930) — empirical validation

Coverage:
- 48 XMPP models registered in the bridge (models.py)
- 30+ MQTT families documented (home-apk-models.md)
- 50+ JSON commands with payloads (this spec)
- 3 map formats + zstd documented
- 4-phase implementation plan (§10)

Pending for gen-3 robots (T80/X8):
- Verify the zstd format with a real device
- The v3.16.1 APK doesn't contain it; reference: ecovacs-deebot.js #592
