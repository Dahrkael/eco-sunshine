# eco-sunshine Lovelace cards

Three custom cards for the eco-sunshine MQTT integration.

## Install (manual)

Copy the four `.js` files to `<ha-config>/www/ecovacs/` and add to Lovelace
resources (one per file):

```yaml
resources:
  - url: /local/ecovacs/ecovacs-card-common.js     # type: module
    type: module
  - url: /local/ecovacs/ecovacs-map-card.js
    type: module
  - url: /local/ecovacs/ecovacs-control-card.js
    type: module
  - url: /local/ecovacs/ecovacs-status-card.js
    type: module
```

Note: `ecovacs-card-common.js` must load first (it is an ES module imported
by the other three).

## Cards

| Card | Purpose |
|---|---|
| `custom:ecovacs-map-card` | Live canvas map: robot (animated), dock, room labels, virtual walls, no-mop zones. Subscribes to `<base>/<slug>/map_data` over the HA websocket (**admin user required** — HA gates `mqtt/subscribe`). |
| `custom:ecovacs-control-card` | Start/pause/dock/locate (native vacuum services) + suction & water selectors + one button per room + consumable bars. |
| `custom:ecovacs-status-card` | Compact glance: state, battery, totals (time/area/count), firmware, errors. |

All three auto-detect the HA language (Spanish/English). Cards derive the
MQTT base topic from the `slug`/`has_water` attributes the bridge reports
in the vacuum state — no entity-id guessing. Override with `base_topic:
<your-base>` if you changed it in the bridge config.

## Quick config

```yaml
type: custom:ecovacs-map-card
entity: vacuum.deebot_ozmo_930   # YOUR vacuum entity id
height: 420
```

```yaml
type: custom:ecovacs-control-card
entity: vacuum.deebot_ozmo_930   # YOUR vacuum entity id
```

```yaml
type: custom:ecovacs-status-card
entity: vacuum.deebot_ozmo_930   # YOUR vacuum entity id
```

See `dashboard-example.yaml` for a full view combining the three.
