# eco-sunshine — specification (all-in-one, multi-robot)

A single service that replaces the app **and** the custom HA component:
it talks XMPP to Bumper as a client, controls the robot and listens to it,
decodes the map and publishes everything over MQTT with native Home
Assistant discovery.

## Architecture

```
                      ┌─────────────────────────────────────────────┐
                      │  eco-sunshine (asyncio)                 |
 Bumper (XMPP 5223) ←──┤  ├─ XMPP session per robot (fake auto-login) │
                      │  ├─ command ←→ MQTT                          │
   930 robot ⇄ bumper │  ├─ collector: Pos/trace/MapP/CleanSt/...    │
                      │  ├─ piece decoder (MapP) + trace              │
                      │  └─ PNG renderer                              │
                      └──────────────────┬──────────────────────────┘
                                         │ MQTT (discovery)
                    Home Assistant ══════╧══ vacuum.* + camera.* + sensors
                                          (CORE mqtt integration, no custom)
```

## Multi-robot

- Config: list of robots; auto-detection by default (any bot that
  registers with Bumper is adopted: `discover: true`).
- Each robot = one asyncio task with its own state, its collector and its
  render.
- Per-robot MQTT namespacing: `deebot/<slug>/#` (slug = serial or friendly
  name), independent discovery per robot (vacuum, camera, sensors).
- No practical limit; XMPP-era robots of different models should work if
  they share the dialect (930/900/960...).

## XMPP vocabulary (validated against the real 930, corpus sessions 1-2)

### Commands (app→robot, wrap in <iq type=set><query><ctl ...>)
- Start cleaning:  `<ctl td="Clean"><clean act="s" speed="standard" type="auto"/></ctl>`
- Pause:            `<ctl td="Clean"><clean act="p" speed="standard" type="auto"/></ctl>`
- To dock:          `<ctl td="Charge"><charge type="go"/></ctl>`
- Map by pieces:    `<ctl td="PullMP" pid="N"/>` (responses: `<ctl td="MapP" i="<mapid>" p="<b64>"/>`)
- Paginated trace:  `<ctl td="GetTr" tf="0" tt="N"/>`
- Map auto-report:  `<ctl td="SetMapAutoReport" on="1"/>`
- Polling: GetPos, GetCleanState, GetChargeState, GetLifeSpan (type Brush/DustCaseHeap...),
  GetOnOff (t=g/b/s), GetError, GetVersion (name=FW), GetSched, GetTime/SetTime,
  GetChargerPos, GetSleepStatus, GetWaterPermeability, GetMapSet (tp=vw → walls)

### Events (robot→client)
- `Pos` (a=angle, p="x,y" mm, valid) — live position
- `trace` (tr=base64 varint ~2cm, trid, tf/tt fragment)
- `TrM` (c=trace counter), `CleanSt`, `CleanReport` (<clean st= type= speed=>),
  `ChargeState`, `SleepStatus`, `GetLifeSpan` responses, `MapP` (pieces), MapSet/MapSt

## MQTT output per robot (`deebot/<slug>/`)

| What | Topic | Note |
|---|---|---|
| Vacuum discovery | `homeassistant/vacuum/<slug>/config` | core vacuum.mqtt entity |
| Commands | `deebot/<slug>/command` | start/pause/return/dock/fan |
| State | `deebot/<slug>/state`, `/attributes` | battery, status, consumables |
| Map | `deebot/<slug>/map` (PNG b64) + `/map_data` | mqtt camera + valetudo-map-card compatible |
| Position/path | `deebot/<slug>/state` | incl. in the map payload |

## Modules (bridge/eco_sunshine/)

- `xmpp_client.py` — TLS session against Bumper (own CA), fake-user login,
  iq/ctl sending, event parsing (from Bumper's xmppserver format).
- `robot.py` — per-robot state + HA state machine (docked/cleaning/…).
- `map_pieces.py` — PullMP walker + reassembly + decoder (phase 3).
- `trace.py` — varint decoder (done, the constant pair is still missing).
- `renderer.py` — Pillow → PNG (map + path + position).
- `mqtt_pub.py` — paho-mqtt/aiomqtt + discovery.
- `cli.py` — `serve` (service mode) / `decode` / `tail`.

## Planned dependencies

slixmpp (or a minimal implementation of our own — Bumper expects a simple
dialect), aiomqtt, Pillow. Python >= 3.10. Own container in docker-compose.
