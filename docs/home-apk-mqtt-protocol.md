# ECOVACS HOME (v3.16.1) — MQTT/JSON Protocol ("NG-IoT" stack)

**Source:** Decompiled APK analysis by subagent (ses_ec38a9a18ffeyCmgUctsPK5xAl)

## 1. Architecture overview

| Component | Class (decompiled) | Location |
|---|---|---|
| Tech type enum (`CLOUD`, `LOCAL`, `MCAST`, `MATTER`) | `NgTechType` | `com/ecovacs/ngiot/NgTechType.java` |
| Manager singleton | `dk3` (NgIotClient), `a` (NgIotManager) | `defpackage/dk3.java`, `com/ecovacs/ngiot/client/a.java` |
| **TechCloud** (cloud MQTT + REST control) | `ceb` | `defpackage/ceb.java` |
| TechLocal (mDNS + WebSocket + PSK) | `leb` | `defpackage/leb.java` |
| TechHook (dispatch to app listeners) | `p73` | `defpackage/p73.java` |
| MQTT client (Paho fork: tcp/ssl/ws/wss) | `com.ecovacs.mqtt.*` | `com/ecovacs/mqtt/` |
| Endpoint identity `yz1 = (id, class, resource)` | `yz1` | `defpackage/yz1.java` (toString = `id@class/resource`) |
| Message envelope `xw = (name, payload)`, payload `la3 = (PayloadType, bytes)` | `xw`, `la3` | `defpackage/` |
| Payload formats: `j`=JSON, `x`=XML, `b`=raw | `PayloadType` | `com/ecovacs/ngiot/techbase/enums/PayloadType.java` |

**Key architectural fact:** the app does **not** publish commands via MQTT. Commands go over **HTTPS** to the `api-ngiot` control endpoint; the server bridges them to MQTT p2p topics toward the robot. The app's MQTT connection is **receive-only**: robot reports (`iot/atr/...`) and p2p messages (`iot/p2p/...`), plus config pushes.

## 2. MQTT broker addresses and ports

```
DC lookup:   GET https://api-base.{vendor}.ecouser.net/api/basis/dc/get-by-area?area={area}
Control API: https://api-ngiot.dc-{dc}.{vendor}.ecouser.net
Auth API:    https://api-base.dc-{dc}.{vendor}.ecouser.net
MQTT broker: ssl://jmq{cluster}-ngiot-{dc}.dc.robot{vendor}.ecouser.net:443
```

- `dc` ∈ `cn`, `na`, `as`, `eu`; `vendor` ∈ `cn`, `ww`
- Port 443, TLS with pinned self-signed CA

## 3. MQTT CONNECT

- **Client ID**: `{userId}@{class}/{resource}` (e.g., `1234567@USER/a1b2c3d4e5`)
- **Username**: three backtick-separated base64 fields (firmware version, wire version, meta)
- **Password**: account access token
- **TLS**: pinned "ECOVACS CA" self-signed certificate (100-year validity)

## 4. Topic structure

```
iot/atr/{name}/{did}/{cls}/{res}/{fmt}                          ← robot reports
iot/p2p/{name}/{fromId}/{fromCls}/{fromRes}/{toId}/{toCls}/{toRes}/{q|p|e|m}/{rand}/{fmt}  ← p2p
iot/cfg/{did}/{cls}/{res}/{name}/{fmt}                          ← config push
iot/dtgcfg/{cls}/{did}/{res}/{name}/{fmt}                       ← device config
```

Type: `q`=request, `p`=response, `e`=error, `m`=message; `fmt`: `j`=JSON, `x`=XML, `b`=binary

## 5. JSON command/response format

**Request** (sent via HTTPS POST, not MQTT):
```json
{
  "header": {"pri": 2, "ts": "1697312345678", "tzm": 480, "ver": "0.0.22"},
  "body":   {"data": { ...command params..., "bdTaskID": "1697312345678421" }}
}
```

**Response** (synchronous HTTP response):
```json
{
  "header": {"fwVer": "...", "hwVer": "...", "pri": 2, "tmz": 480, "ts": "...", "ver": "..."},
  "body":   {"code": "0", "msg": "ok", "data": { ... }}
}
```

`bdTaskID` = epoch-ms + 3 random digits (telemetry correlation).

## 6. Control endpoint (commands)

```
POST https://api-ngiot.dc-{dc}.{vendor}.ecouser.net
     /api/iot/endpoint/control[/v2]
  ?si={16-char random}&ct=q|m&eid={robot did}&et={robot class}&er={robot res}&apn={command}&fmt=j
```

Headers: `Authorization: Bearer {SST token}`, `X-ECO-REQUEST-ID: {si}`, v2 adds `X-ECO-SIGN` (HMAC-SHA256 with hardcoded key).

## 7. Local (LAN) mode — TechLocal

- mDNS service type `_ecovacs-iot._tcp` on port **8000**
- PSK handshake with headers `X-ECVS-IID`, `X-ECVS-MID`, `X-ECVS-RES`
- WebSocket messages: `{"n":<name>,"i":<iid>,"m":<mid>,"r":<res>,"f":<fmt>,"d":<data>}`

## 8. Differences from XMPP

| Aspect | XMPP (legacy) | NG-IoT MQTT |
|---|---|---|
| Transport | TCP XMPP :5223 | MQTT TLS :443 + HTTPS control |
| Commands | XML `<iq><ctl>` published by phone | JSON POSTed to api-ngiot (server bridges to MQTT) |
| Events | IQ stanzas | MQTT subscribe `iot/atr/` |
| Security | SASL + token | Pinned CA + Bearer token + HMAC signing |
| Local mode | none | mDNS + WebSocket + PSK |

## Implications for eco-sunshine

1. **Bumper already has MQTT broker** (port 8883) — robots can connect
2. **BUT**: commands from the app go via HTTPS, not MQTT — Bumper would need an HTTPS control endpoint
3. **Local mode** (mDNS + PSK + WebSocket on port 8000) is a potential path for LAN-only control
4. The cloud login/token flow is deeply integrated — a self-hosted replacement needs to emulate the auth endpoint
