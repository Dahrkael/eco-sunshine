# SPEC: Protocolo Ecovacs MQTT/JSON — Para implementación en deebot-bridge

> Fuentes: decompile APK Ecovacs Home v3.16.1, Bumper source, ecovacs-deebot.js,
> ecovacs-stack, corpus propio (sesiones 1-3 con OZMO 930 XMPP).
> Este documento es la especificación de implementación, no un análisis.

## 0. Resumen ejecutivo

Los robots Ecovacs modernos (T8+, 950+, N8, X1) se comunican por MQTT con:
- **Reports** (robot → cliente): topics `iot/atr/`
- **Comandos** (cliente → robot): topics `iot/p2p/` con JSON en envelope
- **Mapas**: mismo LZMA que la era XMPP, pero en campos JSON en vez de XML

**Bumper ya tiene todo el transporte**: broker MQTT :8883, helperbot que
publica a topics p2p, y relay de reports. Solo falta el vocabulario JSON.

---

## 1. Direcciones e identidad

### 1.1 Conexión del robot al broker

El robot se conecta al broker MQTT (Bumper :8883 con DNS redirect) con:
- **Client ID**: `{sn}@{class}/{resource}` (ej: `b11fceaf-1234@yna5xh/axm`)
  - `sn` = device UUID (asignado por el servidor Ecovacs)
  - `class` = class ID de 6 caracteres (ej: `yna5xh` = T9 AIVI)
  - `resource` = string corto (ej: `axm`, `atom`)
- **Autenticación**: sin autenticación cuando conecta a Bumper (allow_anonymous)

### 1.2 Conexión del bridge (nosotros)

Nuestro bridge se conecta como el "helperbot" de Bumper:
- **Client ID**: `bridge@bumper/bridge`
- Se suscribe a: `iot/p2p/+/+/+/+/bridge/bumper/bridge/+/+/+` (respuestas)
- Se suscribe a: `iot/atr/+/+/+/+` (reports de todos los robots)

---

## 2. Topics MQTT

### 2.1 Estructura general

```
iot/<domain>/<name>/<from-id>/<from-class>/<from-res>/<to-id>/<to-class>/<to-res>/<type>/<rand>/<fmt>
```

| Nivel | Descripción | Ejemplo |
|---|---|---|
| `domain` | `p2p` (comandos), `atr` (reports), `cfg` (config) | `p2p` |
| `name` | nombre del comando o report | `clean_V2`, `onBattery` |
| `from-*` | identidad del emisor | `bridge/bumper/bridge` |
| `to-*` | identidad del destinatario | `{sn}/{cls}/{res}` |
| `type` | `q`=request, `p`=response, `e`=error, `m`=message | `q` |
| `rand` | token de correlación aleatorio | `a1b2` |
| `fmt` | `j`=JSON, `x`=XML, `b`=binario | `j` |

### 2.2 Topics que el bridge PUBLICA (comandos al robot)

```
iot/p2p/{comando}/bridge/bumper/bridge/{robot-sn}/{robot-cls}/{robot-res}/q/{request-id}/j
```

Payload: JSON del envelope (ver §3).

### 2.3 Topics que el bridge ESCUCHA

**Reports del robot:**
```
iot/atr/{report-name}/{robot-sn}/{robot-cls}/{robot-res}/j
```

**Respuestas a comandos:**
```
iot/p2p/{comando}/{robot-sn}/{robot-cls}/{robot-res}/bridge/bumper/bridge/p/{request-id}/j
```

---

## 3. Formato de mensajes JSON

### 3.1 Envelope de comando (bridge → robot)

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
      "...parámetros del comando...": "...",
      "bdTaskID": "1697312345678421"
    }
  }
}
```

| Campo | Valor | Descripción |
|---|---|---|
| `pri` | `2` | prioridad fija |
| `ts` | epoch ms como string | timestamp |
| `tzm` | minutos de timezone | España = `480` (UTC+8... no, UTC+1 = 60) |
| `ver` | `"0.0.22"` | versión del protocolo |
| `bdTaskID` | `{ts}{3-dígitos random}` | correlación para telemetría (opcional pero recomendado) |

### 3.2 Envelope de respuesta (robot → bridge)

```json
{
  "ret": "ok",
  "...campos del resultado...": "..."
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

### 3.3 Reports asíncronos (robot → todos los suscritos)

Mismo formato que la respuesta, en el topic `iot/atr/onXxx/...`:
```json
{"ret":"ok", "value": 85, "isLow": false}
```

---

## 4. Comandos completos

### 4.1 Control de limpieza

#### `clean` / `clean_V2`
```json
// v1
{"act": "start", "mode": "auto", "type": "auto", "count": 1, "donotClean": 0}
// v2 (recomendado)
{"act": "start", "mode": "auto", "router": "", "bdTaskID": "...",
 "content": {"type": "auto", "value": "", "count": 1, "donotClean": 0}}
```

| `act` | Significado |
|---|---|
| `start` | iniciar |
| `pause` | pausar |
| `resume` | reanudar |
| `stop` | detener |

| `type` | Significado |
|---|---|
| `auto` | limpieza completa |
| `spot` | limpieza puntual |
| `customArea` | área específica (coords en `content.value` = `"x1,y1,x2,y2"`) |
| `spotArea` | habitaciones específicas (`content.value` = `"mid1,mid2"` ) |
| `singlePoint` | punto único |
| `aiclean` | IA selectiva |
| `mapPoint` | punto en el mapa |

#### `charge`
```json
{"act": "go"}
```

#### `move` (control remoto)
```json
{"act": "turn", "a": 30}   // girar 30°
{"act": "forward"}           // avanzar
```

#### `stop`
```json
{"act": "stop"}
```

### 4.2 Estado y propiedades

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
{"ret":"ok", "speed": 2}  // 0=silent, 1=standard, 2=strong (varía por modelo)
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
Valores en **minutos restantes**.

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

#### `getBlock` / `setBlock` (No molestar / DND)
```json
{"ret":"ok", "enable": false, "start": "22:00", "end": "08:00"}
// set:
{"enable": true, "start": "22:00", "end": "08:00"}
```

#### `getNetInfo`
```json
{"ret":"ok", "ip": "192.168.1.50", "mac": "AA:BB:CC:DD:EE:FF", "ssid": "MiWiFi", "rssi": -52, "sn": "...", "wkVer": "0.25.16"}
```

#### `getStats` / `getTotalStats`
```json
{"ret":"ok", "cid": 123, "type": "auto", "start": 1697000000, "end": 1697003600,
 "time": 3600, "area": 45000000, "mapCount": 1, "aiopen": 1,
 "aitypes": [5,3,6,4,9], "avoidCount": 24, "enablePowerMop": false, "powerMopType": 2}
```

#### `getError`
```json
{"ret":"ok", "error": []}  // array vacío = sin errores
```

#### `getPos` / `getPos_V2`
```json
// request v1: ["chargePos", "deebotPos"]
// response:
{"ret":"ok", "mid":"0",
 "deebotPos": {"x": 500, "y": 1200, "a": 90},
 "chargePos": [{"x": 100, "y": 100, "a": 0}]}
```
Posiciones en **mm** desde el origen del mapa. `a` = ángulo en grados.

#### `getMapState`
```json
{"ret":"ok", "mode": "training", "state": "built", "progress": 100, "times": 1}
```
Estados: `building`, `built`. Progreso 0-100 durante construcción del mapa.

#### `getRelocationState` / `setRelocationState`
```json
{"ret":"ok", "mode": "", "state": "", "isHasMap": true}
// set (re-localizar tras mover de piso):
{"mode": "manu"}
```

#### `getSleep` / `setSleep`
```json
{"ret":"ok", "enable": false}
```

#### `getBreakPoint` / `setBreakPoint` (limpieza continua)
```json
{"ret":"ok", "enable": true}
```

#### `getCarpertPressure` / `setCarpertPressure`
Presión sobre alfombra (modelos compatibles).

### 4.3 Sonido

#### `playSound`
```json
{"sid": 30, "count": 1}
```
`sid=30` = "I am here" (localizar).

### 4.4 Programaciones

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

### 4.5 Mapa

#### `getMajorMap` (piezas con CRCs — igual que GetMapM del 930)
```json
// request: {}
// response:
{"ret":"ok", "mid":"0", "type":"ol",
 "pieceWidth": 4, "pieceHeight": 5,
 "cellWidth": 100, "cellHeight": 100,
 "pixel": 100,
 "value": "crc1,crc2,crc3,..."}
```
`value` = CRC32 por pieza, separados por comas, en orden row-major.

#### `getMinorMap` (pieza individual)
```json
// request:
{"type":"ol", "mid":"0", "pieceIndex": 7}
// response:
{"ret":"ok", "mid":"0", "type":"ol", "pieceIndex":7,
 "pieceValue":"<base64(LZMA)>"}
```
Decodifica a `cellWidth × cellHeight` bytes (igual que MapP del 930).

#### `getMapInfo` (mapa completo chunked — N8/T8 family)
```json
// request: {"type":"ol"}
// response (múltiples chunks):
{"mid":"0", "type":"ol", "index":0, "totalCount":3,
 "value":"<base64 chunk>", "crc":"...", "pixel":5,
 "width":100, "height":100, "totalWidth":500, "totalHeight":500,
 "startX":0, "startY":0}
```
Concatenar `value` por `index` → base64 → LZMA → grid `width×height`.

#### `getMapSet` / `getMapSubSet` (muros virtuales, zonas, habitaciones)
```json
// getMapSet:
{"type":"ar", "mid":"0"}  // ar=habitaciones, vw=muros, mw=zonas no-fregar
// response:
{"ret":"ok", "mid":"0", "msid":"0", "type":"ar", "count":3,
 "subsets":[
   {"mssid":"1", "totalCount":204, "name":"Salón", "subtype":"0",
    "connections":"...", "index":0, "center":"250,300", "cleanset":"1,0,1"}]}

// getMapSubSet (coordenadas paginadas):
{"mid":"0", "msid":"0", "mssid":"1", "type":"ar", "seq":0, "count":50, "seqIndex":0}
// response:
{"ret":"ok", "mssid":"1", "value":"x1,y1;x2,y2;..."}
```

#### `getMapTrace` (traza de limpieza)
```json
// request:
{"traceStart": 0, "pointCount": 400}
// response:
{"ret":"ok", "tid":"12345", "traceStart":0, "pointCount":400, "totalCount":1500,
 "traceValue":"<base64(LZMA)>"}
```
Decodifica a registros de 5 bytes/punto:
- bytes 0-1: int16 BE `y + 2000`
- bytes 2-3: int16 BE `x + 2000`
- byte 4: bit7 = discontinuidad, bit0 = tipo de punto

#### `getCachedMapInfo` / `setCachedMapInfo` (multi-mapa)
```json
// get:
{"ret":"ok", "maps":[
  {"mid":"0", "name":"Piso 1", "using":true, "index":0, "status":1,
   "reMid":"", "backupId":"", "built":true}]}
// set (seleccionar mapa activo):
{"mid":"1"}
```

#### `getMultiMapState` / `setMultiMapState`
Habilita/deshabilita el modo multi-mapa del robot.

#### `clearMap`
```json
{"mid":"0"}
```

### 4.6 Station / Auto-empty / ACS

#### `getAutoEmpty` / `setAutoEmpty` / `autoEmpty`
```json
{"act": "open"}  // abrir/cerrar bolsa
{"enable": true} // habilitar auto-empty
```

#### `getStationInfo` / `getStationState`
Estado de la base (niveles de agua, bolsa, etc.)

### 4.7 IA / AIVI

#### `getRecognization` / `setRecognization`
Tipos de objetos a reconocer/evitar.

#### `avoidObject` / `onAvoidObject`
Evitar objetos detectados.

---

## 5. Reports asíncronos (robot → bridge)

El robot publica a `iot/atr/{nombre}/...` sin que nadie lo pida:

| Report | Datos | Cuándo |
|---|---|---|
| `onBattery` | `{value: 85, isLow: false}` | cambio de batería |
| `onChargeState` | `{mode: "slot_charging"}` | cambio de carga |
| `onCleanInfo(_V2)` | estado de limpieza completo | cambio de estado |
| `onSpeed` | `{speed: 2}` | cambio de potencia |
| `onWaterInfo` | `{amount: 2, ...}` | cambio de agua |
| `onPos` | `{mid, deebotPos, chargePos}` | movimiento (¡en vivo!) |
| `onMajorMap` | CRCs de piezas | cambio del mapa |
| `onMapTrace` | traza | durante limpieza |
| `onMapState` | `{mode, state, progress}` | construcción mapa |
| `onError` | errores | error |
| `onEvt` | eventos varios | ver tabla de códigos |
| `onOta` | `{status, progress}` | actualización firmware |
| `onSleep` | `{enable}` | modo sueño |
| `onBlock` | DND | no molestar |
| `onVolume` | `{volume}` | volumen |
| `onRelocationState` | re-localización | tras mover |
| `onStats` | estadísticas | fin limpieza |
| `onLiveState` | video en vivo (AIVI) | cámara |
| `onRecognize` | reconocimiento IA | detección |

### Códigos `onEvt` (parciales)
| code | Significado |
|---|---|
| 1007 | mop pro conectada |
| 1015 | ? |
| 1052 | recordatorio mop |
| 1068 | no localiza, volviendo a base |
| 1071 | re-localización exitosa |
| 1088 | fallo de localización |
| 20003 | tipo de tarea no soportado |
| 20011 | fallo setSched |

---

## 6. Formato de mapa — detalle técnico

### 6.1 Codificación LZMA (era XMPP + MQTT gen 1-2)

```
base64(pieceValue) → bytes:
  [0:5]   props LZMA: 5D 00 00 04 00
  [5:9]   uncompressed size (int32 LE) = cellWidth * cellHeight
  [9:]    stream LZMA1
→ LZMA decompress → grid bytes (1 byte/celda)
```

Nuestro `map_pieces.decode_piece()` ya implementa esto.

### 6.1b Codificación zstd (T80/T80S/X8 OMNI — gen 3)

Los modelos más recientes (2024+) usan **zstd** en vez de LZMA:
```
base64(pieceValue) → bytes:
  [0:4]   zstd magic: 28 B5 2F FD
  [4:]    stream zstd
→ zstd decompress → mismo grid bytes (1 byte/celda)
```

Detección automática por magic number:
```python
def decode_piece_auto(data: bytes) -> bytes:
    if data[:4] == b'\x28\xB5\x2F\xFD':  # zstd magic
        import zstandard
        return zstandard.ZstdDecompressor().decompress(data)
    else:  # LZMA (header 5D 00 00 04 00)
        return lzma.decompress(data[0:5] + struct.pack('<Q', 10000) + data[9:],
                              format=lzma.FORMAT_ALONE)
```

Referencia: ecovacs-deebot.js issue #592 (T80S OMNI, zstd map data).
La APK v3.16.1 NO contiene zstd — es posterior. El formato del grid
descomprimido es idéntico, solo cambia el algoritmo.

### 6.2 Ensamblado de piezas (igual que 930)

```
piece grid: pieceWidth × pieceHeight pieces
cada pieza: cellWidth × cellHeight celdas
grid total: (pieceWidth*cellWidth) × (pieceHeight*cellHeight)
pieza N en: row = N / pieceWidth, col = N % pieceWidth (row-major)
```

Valores de celda: **dependen del modelo** — consultar temas del mapa en la app.

### 6.3 Traza (5 bytes/punto)

```
struct TracePoint {
    int16_be y;      // offset +2000 (mm desde origen del mapa)
    int16_be x;      // offset +2000
    uint8  flags;    // bit7=discontinuo, bit0=tipo
}
```

### 6.4 Diferencias con la era XMPP

| Aspecto | XMPP (930) | MQTT (T9+) |
|---|---|---|
| Transporte del comando | XML `<ctl td="GetMapM">` | JSON `{header, body}` en topic p2p |
| Piezas | MapP push o PullMP | getMinorMap por pieceIndex |
| Geometría | attrs `w/h/c/r/p` en XML | `pieceWidth/Height, cellWidth/Height, pixel` en JSON |
| CRCs | attr `m` en GetMapM response | `value` en getMajorMap response |
| Contenido pieza | attr `p` base64 en MapP | `pieceValue` base64 en getMinorMap |
| LZMA decode | IDÉNTICO | IDÉNTICO |
| Posición | attr `p="x,y"` en Pos | `deebotPos:{x,y,a}` en onPos |
| Unidades posición | mm | mm (idéntico) |
| Traza | varint LEB128 (930) | 5 bytes/punto (T9+) — ¡DIFERENTE! |

---

## 7. Modelos MQTT — registro

### Class IDs conocidos (server-driven, algunos hardcodeados)

| classid | Modelo | Driver app |
|---|---|---|
| `yna5xh` | T9 AIVI | dt9ai_main |
| `x7p4y4` | T9 PRO | dt9_main |
| `vsc5ia` | T8/N8 family | dt8_main |
| `qqy0di` | AA30 (AIRBOT) | lib_iot_client |
| `oxandq` | A680 (AIRBOT) | — |
| `lbw54s` | DE5G | — |

Nota: la mayoría se asignan dinámicamente. El robot anuncia su `class`
en el client ID al conectar al broker.

### Capacidades por familia

| Familía | Mapa | Fregado | Agua | AIVI | Auto-empty | Multi-mapa |
|---|---|---|---|---|---|---|
| T8 base | chunked | ✓ | ✓ | — | — | — |
| T8+/N8+ | chunked | ✓ | ✓ | — | ✓ | ✓ |
| T9 | piece/diff | ✓ | ✓ | — | — | ✓ |
| T9 AIVI | piece/diff | ✓ | ✓ | ✓ | — | ✓ |
| X1 OMNI | piece/diff | ✓ | ✓ | ✓ | ✓ | ✓ |
| T10 OMNI | piece/diff | ✓ | ✓ | ✓ | ✓ | ✓ |

---

## 8. Arquitectura de implementación en el bridge

### 8.1 Transporte MQTT del bridge (nuevo módulo)

```python
class MqttTransport:
    """Publica comandos JSON a robots MQTT vía Bumper :8883."""

    async def connect(self):
        # conectar al broker MQTT de Bumper
        # suscribirse a: iot/atr/+/+/+/+ (todos los reports)
        # suscribirse a: iot/p2p/+/+/+/+/bridge/bumper/bridge/+/+/+ (respuestas)

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
        # enrutamiento por estructura del topic
        parts = topic.split("/")
        if parts[1] == "atr":  # report
            report_name = parts[2]
            data = json.loads(payload)
            await self.handle_report(report_name, data)
        elif parts[1] == "p2p" and parts[9] == "p":  # response
            request_id = parts[10]
            self.resolve_pending(request_id, json.loads(payload))
```

### 8.2 Reutilización del MapAssembler

El decoder LZMA es idéntico. Solo cambia:
- La fuente de piezas (JSON `getMinorMap` en vez de XML `MapP`)
- Los nombres de campos (`pieceValue` en vez de `p`)
- La geometría viene en JSON (`pieceWidth` en vez de attr `w`)

```python
# En MapAssembler, añadir:
def add_piece_json(self, map_id, piece_index, piece_value_b64):
    """Añade pieza desde getMinorMap JSON (robots MQTT)."""
    decoded = decode_piece(piece_value_b64)  # mismo decoder LZMA
    self.maps.setdefault(map_id, {})[piece_index] = decoded
    self.version += 1
```

### 8.3 RobotCommander JSON

```python
class MqttCommander:
    """Comandos JSON para robots MQTT (T8+)."""

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

## 9. Referencias cruzadas con otros proyectos

### ecovacs-deebot.js (mrbungle64)
- Dropeó XML, se centra en MQTT/JSON
- Sus modelos soportados: OZMO 920/950, T5, T8, T9, N8, U2, X1
- Sus `getMajorMap`/`getMinorMap` → confirma nuestro análisis del APK
- Su `getMapTrace` → confirma formato 5 bytes/punto
- Su `getPos` → confirma formato `{x, y, a}` en mm

### Bumper
- El helperbot YA publica a `iot/p2p/` con JSON (payloadType "j")
- Solo necesita que el bridge construya los payloads correctos
- El broker YA corre en :8883

### ecovacs-stack (m8schmit)
- Su T9 funciona con topics `iot/p2p` y `iot/atr`
- Confirma el formato de commands y events
- Su mapa usa getMajorMap/getMinorMap

---

## 10. Plan de implementación (fases)

### Fase A: Transporte (2-3h)
1. Nuevo módulo `mqtt_transport.py` — cliente MQTT al broker de Bumper
2. Publicar comandos a `iot/p2p/` con envelope JSON
3. Escuchar reports en `iot/atr/`
4. Correlación request/response por request-id

### Fase B: Comandos básicos (2-3h)
1. MqttCommander con comandos core: clean_V2, charge, getBattery, getPos
2. RobotState se actualiza desde reports (onBattery, onCleanInfo, onPos)
3. Serve enruta a MqttCommander o XmppCommander según el tipo de robot

### Fase C: Mapa (3-4h)
1. getMajorMap → parsear CRCs y geometría
2. getMinorMap por cada pieza cambiada → decode_piece (ya existe)
3. MapAssembler añadir add_piece_json
4. Renderer: mismo (reutilizado)

### Fase D: Integración multi-protocolo (2h)
1. Detección automática: XMPP (XML frames) vs MQTT (JSON en broker)
2. El bridge arranca ambos transportes (XMPP :5223 + MQTT :8883)
3. Un solo serve.py maneja ambos tipos de robot simultáneamente
4. Panel web y MQTT a HA funcionan igual para ambos
