# ecovacs-bridge — a 100% LAN replacement for the Ecovacs cloud + live map in Home Assistant

Self-hosted service that removes the dependency on Ecovacs' backend for
Deebot robots — the OZMO 930 (XMPP, 2018) and eco-ng T8+ models (MQTT) —
by replacing the official cloud with a single service of your own:
bootstrap (lookup), XMPP, MQTT broker, legacy-app API and web panel, plus
a Home Assistant integration with a live map. **The Ecovacs cloud is no
longer involved in anything** (its official XMPP-era OTA is retired — see
`docs/firmware.md`).

## Architecture (single container)

```
930 robot ────XMPP :5223───►┐
eco-ng robot ──MQTT :8883───►┤  bridge = MONOLITH (lookup :8007, XMPP,
                             │  TLS MQTT broker, panel :8181, conf :443*)
Legacy "Ecovacs" app ───────►┘  *off by default (to pair newer robots:
                                 `servers.conf = true` + 443)

bridge ──► map decoder → PNG renderer → MQTT (HA discovery) ──► mosquitto :1883
```

There is no bumper container any more (kept as `--profile legacy` for
rollback). TLS certificates are auto-generated on clean installs; existing
ones are never touched. Full rollback: branch `backup/pre-absorcion` +
legacy.

## Project status — VALIDATED WITH A REAL ROBOT

| Phase | Scope | Status |
|---|---|---|
| 0-5 | Environment, pairing, corpus, reverse engineering, HA bridge | **✅ done** |
| 6 | MQTT transport for T8+ robots (eco-ng) | **✅ done** (spec mqtt-json-spec.md §10) |
| 7 | Bumper absorption into the bridge (monolith) | **✅ done** (FA–FD, `docs/absorcion-bumper.md`) |
| 8 | Regression with the real 930 in production | **✅ done** — connects, fw 1.9.9, battery, consumables, map |
| — | Real eco-ng (T8+), legacy app against FD | pending hardware/phone |
| — | Custom firmware | **separate project** (roadmap in `docs/firmware.md`) |

Highlights from the real-robot regression: the 930's **eco dialect** (it
replies by ctl-id without td/pid — everything was dropped until this was
caught on the wire) and the **de.ecorobot.net broadcast** (the battery
report Bumper never routed correctly).

The bridge supports **48 XMPP models** (detected by JID) and **eco-ng
MQTT** robots (T8+; known class ids + generic fallback). A single `serve`
handles both protocols simultaneously: same panel, same HA entities.

## Bridge (Python ≥3.10, see `bridge/`)

```bash
cd bridge
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m ecovacs_bridge serve -c ../configs/bridge-dev.toml --mqtt
# panel: http://localhost:8282 | HA: discovery on mosquitto :1883

.venv/bin/python -m ecovacs_bridge probe-mqtt --ca-file ../data/certs/ca.crt   # eco-ng diagnostics
python tools/mock_mqtt_robot.py    # synthetic T9 AIVI robot (eco-ng/MQTT), no hardware needed
python tools/extract_930_corpus.py && python tools/mock_930_robot.py  # XMPP clone of the REAL 930
.venv/bin/python -m pytest         # suite (decoders, assembler, state, routing)
```

The **930 mock** reproduces the real robot's exact behavior from the
corpus extracted from Bumper's log (`data/mock930-corpus.json`): the same
map spec with its CRCs, real LZMA pieces of the floor, the real cleaning
trajectory (replayed while cleaning), real lifespans and battery, and
frame formats verified attribute by attribute. Add its JID to `robots`
in the config so that `serve` adopts it (when docked it emits nothing,
like the real one).

Config: the `[bumper_mqtt]` section of the TOML (host/port/CA of the
Bumper broker, `enabled`, known robots to adopt without waiting for a
report). The map decoder detects **LZMA/zstd by magic number** and the
eco-ng trace uses the 5-bytes-per-point format (spec §6).

### hbmqtt note (Bumper's broker)

hbmqtt's `matches()` compiles `+` as a character class without `-`, so
filters using `+` never match eco-ng dids (UUIDs with hyphens). The
bridge works around it by subscribing to `iot/#` and filtering by topic
structure (robust against any broker); `bumper/bumper/mqttserver.py` also
carries a monkey-patch with correct MQTT-4.7 matching (applies when the
Bumper image is rebuilt).

## Decision made: the future of Bumper (abandoned upstream)

Resolved in phase 7 with route **(b) own backend**: Bumper's servers
(lookup, XMPP, MQTT broker and the app's `:443` API) were absorbed INTO
the bridge (phases FA–FD) and the deployment is a single container (FE).
Bumper's code stays vendored under `bumper/` and its container moves to
the `legacy` profile for rollback during the transition. The app API
(confserver, FD) is implemented but **off by default** — see "Using the
Ecovacs app against the bridge".

The robot speaks XMPP (lookup.do asks for `EcoMsgNew`) → the protocol was
non-negotiable. Routes evaluated at the time:
- (a) Own FORK (current): already vendored + patched; an xmppserver of
  ~800 lines we know inside out, plus a corpus for regression. Low
  maintenance cost.
- (b) Minimal own backend (lookup.do + XMPP + the app's HTTPS): the corpus
  IS the specification of the dialect. Removes the abandoned component
  without touching the robot. The elegant mid-term route.
- (c) Direct MQTT (ecovacs-privacy-control style, local mosquitto): ONLY
  if the robot's firmware can request the MQTT service (the 900/901
  migrated over OTA; some 930s according to privacy-control). An uncertain
  experiment that loses the app as an oracle. It is not "dropping XMPP":
  it would require the robot itself to change channels.

## Operation (DEV, WSL + Docker Desktop)

Since the Bumper absorption (phase 7) the deployment is **two
containers**: the bridge (everything embedded: lookup+XMPP+MQTT
broker+panel) and mosquitto.

```bash
cd /mnt/t/ecovacs
docker compose up -d --build          # bridge + mosquitto
docker logs -f ecovacs-bridge       # embedded servers + robots
```

Ports published on the Windows host (**192.0.2.10**): 5223 (XMPP),
8007 (lookup), 8883 (eco-ng MQTT), 8181 (panel). Port 443 (the app API)
is **commented out** in the compose file: publish it only if you enable
`servers.conf` (FD) — see "Using the Ecovacs app against the bridge".

- `BUMPER_ANNOUNCE_IP` (`.env`) = the IP the bridge announces to the robot
  as its server (via `DEEBOT_ANNOUNCE_IP` → `servers.announce_ip`; the
  toml can pin it and wins). **DEV**: the Windows LAN IP. **PROD**: the
  minipc's IP.
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

## Windows firewall

Docker Desktop publishes the ports; if the robot/app can't connect, allow
inbound 5223/tcp, 8007/tcp, 8883/tcp (and 443/tcp if you enable the conf)
for `com.docker.backend` (Private profile of the Wi-Fi network).

## Using the Ecovacs app against the bridge (FD, optional)

The **legacy "Ecovacs" app** (XMPP robots) can log in, list the robots
and send commands against the bridge, without Bumper legacy. Supported:
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

## Pairing checklist (phase 1)

> The legacy app needs the `:443` API: with FD it is enough to enable
> `servers.conf` + publish 443 (above). The **bumper legacy** alternative
> (`--profile legacy`) remains valid for rollback — never alongside the
> bridge's conf (same port).

1. [ ] DHCP reservation with a fixed IP for the robot (router).
2. [ ] Pi-hole answering the wildcard (test from the phone:
        `nslookup lbo.ecovacs.net` → 192.0.2.10).
3. [ ] Bridge up (`docker compose ps` — embedded servers on).
4. [ ] Robot in pairing mode (WiFi button until the voice/blinking).
5. [ ] Legacy "Ecovacs" app → add device → the robot's network → home
        Wi-Fi credentials.
6. [ ] Verify in `docker logs ecovacs-bridge`: the XMPP bot's login
        (~30 s after restarting the robot).
7. [ ] Try a cleaning command from the app (all local).

## Production migration (minipc)

1. Copy the project (including `./data/` to keep certs and state).
2. `.env`: `BUMPER_ANNOUNCE_IP=<minipc-IP>` (the bridge consumes it).
3. `docker compose up -d --build` (bridge + mosquitto; bumper only with
   `--profile legacy`).
4. Pi-hole: update the 3 `address=` lines to the minipc's IP and reload
   FTL.
5. Restart the robot and the phone (DNS re-resolution).

The certs in `./data/certs` travel with the copy: the robots do NOT have
to re-pair (the CA they already trust is the same one the bridge's
embedded servers present).

## References

- Bumper: https://github.com/bmartin5692/bumper (docs: bumper.readthedocs.io)
- sucks + bmartin5692's fork (the historical XMPP client, basis of ha_ecovacs_bumper)
- ecovacs-deebot.js (mrbungle64) — issue #10: live map, unsolved
- DEF CON 32: "Reverse engineering and hacking Ecovacs robots" (Giese/Luedtke)
- valetudo-map-card (the bridge's target MQTT format)
