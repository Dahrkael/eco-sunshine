# eco-sunshine — 100% LAN replacement for the Ecovacs cloud with Home Assistant integration

[![CI](https://github.com/Dahrkael/eco-sunshine/actions/workflows/ci.yml/badge.svg)](https://github.com/Dahrkael/eco-sunshine/actions/workflows/ci.yml)

Self-hosted service that removes the dependency on Ecovacs' backend for
Deebot robots (both old ones using XMPP and new ones using MQTT)
by replacing the official cloud with a single service of your own:
bootstrap (lookup), XMPP, MQTT broker, legacy-app API and web panel, plus
a Home Assistant integration with separate entities for each added robot and a live map card.

The bridge supports **48 XMPP models** (detected by JID) and **eco-ng
MQTT** robots (T8+; known class ids + generic fallback). A single `serve`
handles both protocols simultaneously: same panel, same HA entities.

## Architecture

```
legacy robot ────XMPP :5223───►┐
eco-ng robot ──MQTT :8883───►┤  bridge = MONOLITH (lookup :8007, XMPP,
                             │  TLS MQTT broker, panel :8181, conf :443*)
Legacy "Ecovacs" app ───────►┘  *off by default (to pair newer robots:
                                 `servers.conf = true` + 443)

bridge ──► map decoder → MQTT (HA discovery) ──► mosquitto :1883
```
TLS certificates are auto-generated on clean installs; existing
ones are never touched.

## Quickstart (fresh clone)

```bash
git clone <repo> && cd eco-sunshine
cp .env.example .env          # edit: BUMPER_ANNOUNCE_IP = this host's LAN IP
docker compose up -d --build  # bridge + mosquitto
```

The panel is at `http://<host>:8181`. Before a robot can find the bridge
you also need the **DNS setup** (Pi-hole section below) — robots resolve
Ecovacs domains and must land on your host.

Requirements: Docker + docker compose, a DNS you control (Pi-hole
recommended), Python ≥3.10 only for development/CLI use without Docker.

## Bridge (Python ≥3.10, see `bridge/`)

```bash
cd bridge
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m eco_sunshine serve -c ../configs/bridge-dev.toml --mqtt
# panel: http://localhost:8282 | HA: discovery on mosquitto :1883

.venv/bin/python -m eco_sunshine probe-mqtt --ca-file ../data/certs/ca.crt   # eco-ng diagnostics
python tools/mock_mqtt_robot.py    # synthetic T9 AIVI robot (eco-ng/MQTT), no hardware needed
.venv/bin/python -m pytest         # suite (decoders, assembler, state, routing)
```

## Operation (dev/prod notes)

Deployment is **two containers**: the bridge (everything embedded:
lookup+XMPP+MQTT broker+panel) and mosquitto — see Quickstart above for
the fresh-clone flow.

```bash
cd eco-sunshine
docker compose up -d --build          # bridge + mosquitto
docker logs -f eco-sunshine       # embedded servers + robots
```

Ports published: 5223 (XMPP), 8007 (lookup), 8883 (eco-ng MQTT), 8181 (panel).
Port 443 (the app API) is **commented out** in the compose file:
publish it only if you enable `servers.conf` — see "Using the Ecovacs app against the bridge".

- `BUMPER_ANNOUNCE_IP` (`.env`) = the IP the bridge announces to the robot
  as its server (via `DEEBOT_ANNOUNCE_IP` → `servers.announce_ip`; the
  toml can pin it and wins).
- Certs: `./data/certs` mounted RW — on a clean install the bridge
  auto-generates CA+cert on first boot (robots must then pair against
  that CA). If they already exist (migration from Bumper) they are NOT
  touched: the robots keep trusting the legacy CA.
- Persistent state in `./data/bridge/` (map, positions, registry) — do
  not commit it (`data/` is in `.gitignore`).
- **Bumper legacy** (rollback/comparison during the transition):
  `docker compose --profile legacy up -d bumper` — never alongside the
  bridge (same published ports). With `[servers]` off in the toml, the
  bridge goes back to being a pure client of that Bumper.

## DNS (Pi-hole) — do this BEFORE pairing the robot

Recommended option (wildcard, proof against future subdomains — the app
gets its domains dynamically from the backend, no exact list can be
trusted):

**1. Custom dnsmasq (wildcard)**: `docs/pihole/99-deebot-bumper.conf`.
   It redirects:

```
ecouser.net / ecovacs.com / ecovacs.net  →  192.0.2.10
```

   ⚠️ **Pi-hole v6 does not read `/etc/dnsmasq.d/` by default**: apply it
   via `Settings → All settings (expert) → Miscellaneous → dnsmasq_lines`
   (paste the 3 lines; it persists in pihole.toml) or by enabling
   `etc_dnsmasq_d` + mounting the file as a volume.

2. Local DNS Records alternative (GUI, ~40 exact domains):
   `docs/pihole/local-dns-records.md` + an anti-leak blocklist regex.

The Android phone (legacy "Ecovacs" app) must resolve through the
Pi-hole: Wi-Fi → advanced options → private DNS/IP = the Pi-hole's IP.

## Adding a robot

With the DNS already pointing at the bridge (see above):

- **Robot already configured** (it was paired with the Ecovacs cloud at
  some point): just restart it. It resolves the Ecovacs domains to your
  host and connects to the bridge — no app involved. Watch it appear in
  `docker logs -f eco-sunshine`, the panel and Home Assistant.
- **New robot**: it needs the legacy "Ecovacs" app ONCE, to receive your
  WiFi credentials — enable the app API as described in the next section,
  put the robot in pairing mode (WiFi button until it blinks/speaks) and
  add it from the app. The app is not needed after that. There is also an
  app-less alternative — connect to the robot's own AP and POST the
  credentials via `rcp.do` (`SetApConfig`) — documented in
  `docs/protocol.md`.

## Home Assistant integration

Pure **MQTT discovery** — no custom component needed. Each robot shows up
as one device with ~18 entities (vacuum, map camera, consumables, stats,
firmware, error, per-room clean buttons, water-flow select for mopping
models, DND/silent switches). Entity ids are derived from the robot's
**name** (e.g. `vacuum.deebot_ozmo_930`). Full entity table and topic
contract: `docs/home-assistant.md`.

### Optional Lovelace cards (recommended)

Three custom cards on top of the entities: a **live map** (canvas with
animated robot, dock, rooms, virtual walls, no-mop zones), a **control
card** (start/pause/dock/locate + suction/water selectors + room buttons)
and a **status card** (battery, totals, firmware, errors). Spanish/English
following the HA language.

Install (manual copy):

1. Copy the four files from `hass/` in this repo to your HA config:
   ```
   cp hass/ecovacs-card-common.js  <ha-config>/www/ecovacs/
   cp hass/ecovacs-map-card.js     <ha-config>/www/ecovacs/
   cp hass/ecovacs-control-card.js <ha-config>/www/ecovacs/
   cp hass/ecovacs-status-card.js  <ha-config>/www/ecovacs/
   ```
   (`<ha-config>` = the folder containing `configuration.yaml`; create
   `www/ecovacs/` if it doesn't exist. `ecovacs-card-common.js` MUST be
   listed first in the resources.)

2. Add the Lovelace resources (**Settings → Dashboards → ⋮ → Resources**,
   or in YAML mode):
   ```yaml
   resources:
     - url: /local/ecovacs/ecovacs-card-common.js
       type: module
     - url: /local/ecovacs/ecovacs-map-card.js
       type: module
     - url: /local/ecovacs/ecovacs-control-card.js
       type: module
     - url: /local/ecovacs/ecovacs-status-card.js
       type: module
   ```

3. Add the cards to a dashboard — the entity is your `vacuum.*`:
   ```yaml
   type: custom:ecovacs-map-card
   entity: vacuum.deebot_ozmo_930
   height: 420
   ```
   A complete ready-to-paste view lives in `hass/dashboard-example.yaml`.

Notes:
- The map card subscribes to MQTT over the HA websocket — it needs an
  **admin user** (HA gates `mqtt/subscribe`).
- Cards derive the MQTT base topic from the `slug` the bridge reports in
  the vacuum attributes; if you changed `base_topic` in the bridge config,
  set `base_topic: <yours>` on the card too.
- The map appears after the first `map_data` publication (retained) —
  start a clean once if the map is empty.

## Using the Ecovacs app against the bridge (optional)

The **legacy "Ecovacs" app** (XMPP robots) can log in, list the robots
and send commands against the bridge. Supported:
**only the legacy app** — the modern one (Ecovacs Home) signs its
requests with HMAC (`X-ECO-SIGN`) and is not implemented.

1. `configs/bridge.toml`: uncomment `conf = true` under `[servers]`.
2. `docker-compose.yml`: uncomment the bridge's `- "443:443"` mapping.
3. `docker compose up -d --build` and reinstall/open the legacy app on
   the phone (the Pi-hole DNS already redirects `*.ecovacs.com`/
   `*.ecouser.net` to the bridge; the app will install the certificate's
   CA if needed — the same CA the pairing flow already uses).
4. Log in with any account (use_auth=False, like Bumper): the app does
   `user/login` → `user/getAuthCode` → `user.do loginByItToken` →
   `user.do GetDeviceList` (the bots registered via XMPP/MQTT) and
   connects over XMPP to the embedded :5223.

## References

- Bumper: https://github.com/bmartin5692/bumper (docs: bumper.readthedocs.io)
- sucks + bmartin5692's fork (the historical XMPP client, basis of ha_ecovacs_bumper)

## License

**GPL-3.0** — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

This project vendors [bumper](https://github.com/bmartin5692/bumper)
(GPL-3.0) and its embedded XMPP server, legacy-app API and MQTT auth
derive from it, so the copyleft applies to the whole distribution.
