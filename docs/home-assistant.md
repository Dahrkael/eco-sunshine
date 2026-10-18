# Home Assistant integration (pure MQTT discovery)

The bridge publishes everything through the **mqtt core integration** — no
custom component required. Each robot appears as one HA device with all
its entities; the optional Lovelace cards (below) add the rich UI on top.

## Entities per robot

| Entity | Type | Notes |
|---|---|---|
| `<name>` | `vacuum` | state schema, commands, battery, fan_speed (list per model) |
| `<name> map` | `camera` | map PNG (retained) |
| `<name> brush/side brush/dust case/air filter` | `sensor` % | consumables |
| `<name> error` | `sensor` | error code |
| `<name> firmware` | `sensor` | fw version (e.g. 1.9.9) |
| `<name> cleaning time / cleaned area / clean count` | `sensor` | totals, `state_class: total_increasing` |
| `<name> reset <consumable>` ×4 | `button` | lifespan reset |
| `<name> room <room>` ×N | `button` | clean a single room (auto-published when rooms are learned) |
| `<name> do not disturb` / `silent mode` | `switch` | optimistic |
| `<name> water flow` | `select` | low/medium/high — only for mopping models |

Entity ids are derived from the device **name** by HA (e.g. a robot
named "Deebot OZMO 930" → `vacuum.deebot_ozmo_930`).

## Topics (stable contract)

```
deebot/<slug>/state            consolidated JSON doc (retained)
deebot/<slug>/map              PNG bytes (camera)
deebot/<slug>/map_data         canvas JSON for custom cards (retained)
deebot/<slug>/command          start|pause|dock|locate|spot
deebot/<slug>/room             room mssid(s), "1" or "1,2"
deebot/<slug>/fan_speed/set    silent|standard|strong
deebot/<slug>/water/set        low|medium|high
deebot/<slug>/reset_life_span  Brush|SideBrush|DustCaseHeap|AirFilter
deebot/<slug>/dnd/set, /silent/set   ON|OFF
```

The `state` doc carries everything the cards need: `slug`, `has_water`,
`position`, `charger`, `map_sets` (rooms/walls/zones), `stats`,
`consumables`, `fw`.

## Custom cards (optional)

See `hass/` — live map (canvas, animated robot, rooms, walls), control
(fan/water/rooms/consumables) and status cards. Copy the four `.js` files
to `/config/www/ecovacs/`, add them as Lovelace resources (common first),
and use `hass/dashboard-example.yaml` as a starting view.

> The map card subscribes to MQTT over the HA websocket — it needs an
> **admin** user (HA gates `mqtt/subscribe` to admins).

## Setup

1. Mosquitto (or your broker) running and `mqtt:` configured in the
   bridge TOML.
2. Discovery flows automatically on first adoption; entities appear
   without restarts.
3. Rooms publish their clean buttons as soon as the robot reports a
   mapped-room set (first deep poll or after a clean).
