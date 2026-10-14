# ECOVACS HOME v3.16.1 — Command Vocabulary & Map Protocol (MQTT/JSON)

> Extracted from decompiled APK by subagent (ses_ec38a5c50ffeLrnwhiLSSfVxMJ)

## 1. Transport

Commands via HTTPS POST to `/api/iot/endpoint/control?si={id}&ct=q|m&eid={sn}&et={mid}&er={res}&apn={cmd}&fmt=j`
Reports via MQTT subscribe `iot/atr/{name}/{sn}/{mid}/{res}/+`

Request envelope:
```json
{"header":{"pri":2,"ts":"<ms>","tzm":480,"ver":"0.0.22"},"body":{"data":{...}}}
```

Response: `{"ret":"ok",...}` or `{"ret":"fail","errno":N,"error":"..."}`

## 2. Command Vocabulary (complete)

### Control
- `clean` / `clean_V2`: `{act:start|pause|resume|stop, mode, type:auto|spot|customArea|spotArea, content:{type,value,count}}`
- `charge`: `{act:"go"}`
- `move`: `{act, a}` (remote control)
- `playSound`: `{sid, count}`
- `autoEmpty` / `stationAction`

### Getters (params `{}` unless noted)
- `getBattery` → `{value:%, isLow}`
- `getChargeState` → `{mode}`
- `getSpeed` → `{speed}`
- `getWaterInfo` → `{amount, type, sweepType, enable}`
- `getCleanInfo` / `_V2` → estado completo
- `getLifeSpan` (params `["brush","sideBrush","heap","unitCare","dModule"]`)
- `getBlock` → DND `{enable, start:"HH:MM", end:"HH:MM"}`
- `getSched` / `_V2` → schedules
- `getVolume` → `{volume, total}`
- `getNetInfo` → `{ip, mac, ssid, rssi}`
- `getStats` / `getTotalStats` → `{time, area, ...}`
- `getPos` (params `["chargePos","deebotPos"]`) → `{deebotPos:{x,y,a}, chargePos:[...]}`
- `getPos_V2` (params `[{"type":[...],"mid":"0"}]`)
- `getMapState` → `{mode, state, progress(0-100), times}`
- `getMultiMapState`, `getRelocationState`
- `getAudioCallState`, `getRecognization`, `getTrueDetect`

### Setters
- `setSpeed{speed}`, `setWaterInfo{amount,...}`, `setVolume{volume}`
- `setBlock{enable,start,end}` (DND)
- `setSched`/`_V2`, `setSleep{enable}`, `setBreakPoint{enable}`
- `setRelocationState{mode:"manu"}`, `setCarpertPressure`
- `resetLifeSpan{component}`, `clearMap{mid}`

### Async reports (MQTT `iot/atr/onXxx/...`)
`onBattery, onChargeState, onCleanInfo(_V2), onWaterInfo, onSpeed, onPos, onMajorMap, onMinorMap, onMapTrace, onMapSet(_V2), onMapSubSet, onMapInfo(_V2), onMapState, onError, onEvt, onOta, onSched(_V2), onSleep, onRelocationState, onBlock, onVolume, onVoice, onStats, onAvoidObject, onRecognize, onLiveState, onAIMap`

## 3. Map Protocol — three coexisting systems

### (A) Piece/diff grid — T9/T20/X1 (same concept as our 930 MapP)
1. `getMajorMap {}` → `{pieceWidth, pieceHeight, cellWidth, cellHeight, pixel, value:"crc1,crc2,..."}`
2. For each changed CRC: `getMinorMap {mid, pieceIndex}` → `{pieceValue:"<base64(LZMA)>"}`
3. Decompress → `cellWidth×cellHeight` bytes → blit at grid position

**Same LZMA encoding as our 930** (`5D 00 00 04 00` header, raw LZMA1).

### (B) Chunked whole-map — N8/T8 family
- `getMapInfo {type:"ol"}` → chunks `{index, totalCount, value:"<base64>"}`
- Concat by index → base64 → LZMA → `width×height` byte grid

### (C) Outline/"3D" — X2/T30 family
- `getMapInfo_V2 {type:"0", mid}` → `{index, info, infoSize, serial}` chunks

### Virtual walls / zones
- `getMapSet {type:"ar"|"vw"|"mw"|"svw", mid}` → subsets with `mssid, name, center, cleanset`
- `getMapSubSet {mid, msid, mssid, seq, count:50, seqIndex}` → `{value:"x,y;x,y;..."}`
- `setMapSubSet` with act: add|mod|del|merge|divide

### Trace
- `getMapTrace {traceStart:0, pointCount:400}` → `{tid, traceValue:"<base64(LZMA)>"}`
- 5 bytes/point: int16BE y+2000, int16BE x+2000, flags byte
- `tid` change = new cleaning run

### Positions
- `getPos ["chargePos","deebotPos"]` → `{deebotPos:{x,y,a}, chargePos:[{x,y,a}]}`

## 4. LZMA decode (same as our 930)

`DataParseUtil.decode7zData(String)`:
1. Base64 decode
2. Validate header: `5D 00 00 04 00` + 4-byte LE uncompressed size
3. Raw LZMA1 decode

**Our bridge already has this decoder** (`map_pieces.decode_piece`).

## 5. Key differences from XMPP

| Aspect | XMPP (our 930) | MQTT/JSON |
|---|---|---|
| Envelope | XML `<ctl td="...">` | JSON `{header, body:{data}}` + `apn` param |
| Map | GetMapM + MapP pushes | getMajorMap/onMajorMap + getMinorMap (diff) |
| LZMA | Same encoding | Same encoding |
| Commands | XML via XMPP :5223 | JSON via HTTPS or MQTT p2p |
| Reports | IQ stanzas | MQTT `iot/atr/` topics |
