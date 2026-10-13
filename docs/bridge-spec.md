# deebot930-bridge — especificación (todo-en-uno, multi-robot)

Un solo servicio que sustituye a la app **y** al componente custom de HA:
habla XMPP con Bumper como cliente, controla y escucha al robot, decodifica
el mapa y publica todo por MQTT con discovery nativo de Home Assistant.

## Arquitectura

```
                      ┌─────────────────────────────────────────────┐
                      │  deebot930-bridge (asyncio)                 |
 Bumper (XMPP 5223) ←──┤  ├─ sesion XMPP por robot (auto-login fake) │
                      │  ├─ comando ←→ MQTT                          │
   robot 930 ⇄ bumper │  ├─ colector: Pos/trace/MapP/CleanSt/...    │
                      │  ├─ decodificador piezas (MapP) + traza      │
                      │  └─ renderer PNG                             │
                      └──────────────────┬──────────────────────────┘
                                         │ MQTT (discovery)
                    Home Assistant ══════╧══ vacuum.* + camera.* + sensors
                                          (integracion mqtt CORE, sin custom)
```

## Multi-robot

- Config: lista de robots; auto-detección por defecto (cualquier bot que se
  registre en Bumper se adopta: `discover: true`).
- Cada robot = una tarea asyncio con su propio estado, su colector y su render.
- Namespacing MQTT por robot: `deebot/<slug>/#` (slug = serie o friendly name),
  discovery independiente por robot (vacuum, camera, sensors).
- Sin límite práctico; robots de distintos modelos de la era XMPP deberían
  funcionar si comparten dialecto (930/900/960...).

## Vocabulario XMPP (validado contra el 930 real, corpus sesiones 1-2)

### Comandos (app→robot, envolver en <iq type=set><query><ctl ...>)
- Iniciar limpieza:  `<ctl td="Clean"><clean act="s" speed="standard" type="auto"/></ctl>`
- Pausa:            `<ctl td="Clean"><clean act="p" speed="standard" type="auto"/></ctl>`
- A base:           `<ctl td="Charge"><charge type="go"/></ctl>`
- Mapa por piezas:  `<ctl td="PullMP" pid="N"/>` (respuestas: `<ctl td="MapP" i="<mapid>" p="<b64>"/>`)
- Traza paginada:   `<ctl td="GetTr" tf="0" tt="N"/>`
- Autoreporte mapa: `<ctl td="SetMapAutoReport" on="1"/>`
- Sondeo: GetPos, GetCleanState, GetChargeState, GetLifeSpan (type Brush/DustCaseHeap...),
  GetOnOff (t=g/b/s), GetError, GetVersion (name=FW), GetSched, GetTime/SetTime,
  GetChargerPos, GetSleepStatus, GetWaterPermeability, GetMapSet (tp=vw → muros)

### Eventos (robot→cliente)
- `Pos` (a=ángulo, p="x,y" mm, valid) — posición en vivo
- `trace` (tr=base64 varint ~2cm, trid, tf/tt fragmento)
- `TrM` (c=contador de traza), `CleanSt`, `CleanReport` (<clean st= type= speed=>),
  `ChargeState`, `SleepStatus`, `GetLifeSpan` resp, `MapP` (piezas), MapSet/MapSt

## Salida MQTT por robot (`deebot/<slug>/`)

| Qué | Topic | Nota |
|---|---|---|
| Discovery vacuum | `homeassistant/vacuum/<slug>/config` | entidad vacuum.mqtt core |
| Comandos | `deebot/<slug>/command` | start/pause/return/dock/fan |
| Estado | `deebot/<slug>/state`, `/attributes` | battery, status, consumibles |
| Mapa | `deebot/<slug>/map` (PNG b64) + `/map_data` | cámara mqtt + valetudo-map-card compatible |
| Posición/path | `deebot/<slug>/state` | incl. en payload del mapa |

## Módulos (bridge/deebot930_bridge/)

- `xmpp_client.py` — sesión TLS contra Bumper (CA propio), login usuario fake,
  envío iq/ctl, parse de eventos (a partir del formato de Bumper xmppserver).
- `robot.py` — estado por robot + máquina de estados de HA (docked/cleaning/…).
- `map_pieces.py` — PullMP walker + reassembly + decodificador (fase 3).
- `trace.py` — decoder varint (hecho, falta el par constante).
- `renderer.py` — Pillow → PNG (mapa + path + posición).
- `mqtt_pub.py` — paho-mqtt/aiomqtt + discovery.
- `cli.py` — `serve` (modo servicio) / `decode` / `tail`.

## Dependencias previstas

slixmpp (o implementación mínima propia — Bumper espera un dialecto simple),
aiomqtt, Pillow. Python >= 3.10. Contenedor propio en docker-compose.
