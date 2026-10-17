# Bumper absorption into the bridge — merge map

> Status: **FA–FD implemented** (branch `feat/absorcion-bumper`). FD (the
> `:443` confserver, the app API) is **optional and off by default**. This
> document started as a plan and now records what was actually done — see
> the final checklist.

## Principle

The bridge already talks to "Bumper" only through stable interfaces:

| Seam | Today | After the merge |
|---|---|---|
| `MqttTransport` | TLS client to `bumper_mqtt.host:8883` | points at `127.0.0.1` of the embedded hbmqtt broker (or an in-process adapter) — zero changes in serve/commanders |
| `BumperXMPPClient` | TLS client to `bumper.host:5223` | points at `127.0.0.1` of the embedded xmppserver — zero changes |
| `DeviceId` + `robots` dict | the bridge's own registry | becomes THE device registry (replaces `bumper.bot_add()`/SQLite) |

**The golden rule: embed first, refactor later.** Bumper's servers are
already asyncio libraries inside its process (`xmppserver.py`,
`mqttserver.py`); the merge is about moving them to the bridge's
entrypoint, not rewriting them.

## What moves

| Bumper module | Destination | Notes |
|---|---|---|
| `mqttserver.py` (MQTTServer + auth plugin) | `ecovacs_bridge/servers/mqtt.py` | **done (FC)**: embedded `amqtt` broker; the helperbot was REMOVED (MqttTransport replaces it) |
| `xmppserver.py` | `ecovacs_bridge/servers/xmpp.py` | **done (FB)**: vendored almost verbatim, registry injected |
| `confserver.py` (:443/:8007) | `servers/lookup.py` (:8007) + `servers/conf.py` (:443) | lookup **done (FA)**; conf **done (FD)**: the legacy app's login/getAuthCode/user.do against the DeviceRegistry — detail below |
| `db.py` (bots/tokens/users) | `ecovacs_bridge/servers/registry.py` | **done (FB)**: JSON DeviceRegistry next to the state_file (bumper.db dies) |
| `create_certs` + data/certs | `ecovacs_bridge/servers/certs.py` | **done (FE)**: CA+cert auto-generated on first boot if missing; existing ones are never touched |

## What gets removed

- `MQTTHelperBot` — the bridge already publishes p2p commands directly.
- The double definition of "known robot" (bumper's DB vs the bridge's
  config).
- The bumper container from docker-compose (one container less).

## The real work it would take (in order of pain)

> Note: section from the original plan — how it actually went: (1) amqtt
> without friction, (2) the TLS clients already worked around it and FE
> generates CAs with AKI/SKI, (3) solved in FA (only what is active) with
> graceful shutdown in FC, (4) `[servers]` in FA + `DEEBOT_ANNOUNCE_IP`
> in FE (the toml doesn't read env).

1. **Dependencies py3.7 → 3.12+**: `hbmqtt` is archived → the `amqtt`
   fork; `pkg_resources` (removed in 3.12) → `importlib`; `passlib` →
   optional.
2. **Certificates**: Bumper's generator produces CAs without AKI that
   py≥3.12's strict mode rejects (both TLS clients already work around
   it; the embedded broker inherits the problem → improve create_certs).
3. **Lifecycle**: a single `asyncio.run()` with graceful shutdown of
   xmpp + mqtt-broker + conf + transports + panel.
4. **Unified config**: `[bumper]` (client) merges with the server flags
   (`listen`, `announce_ip`…).

## Merge checklist

Status by phase (one saga of commits each, `git log --oneline`):

- [x] **FA** — `[servers]` config (toggles/ports/certs per component),
  embedded lookup `:8007` (POST /lookup.do, compact JSON) and a
  "serve starts ONLY what is active" lifecycle
- [x] **FB** — `DeviceRegistry` (JSON persistence next to the
  state_file, replaces bumper.db) + embedded, vendored XMPP server
  `:5223`; the bridge's XMPP client moves to `127.0.0.1`. E2E: the 930
  mock authenticates against the embedded one, corpus accepted,
  state/map/dock OK
- [x] **FC** — embedded MQTT broker `:8883` (amqtt; Bumper-style auth,
  '+' = one exact level, hyphens included, explicit TLS without
  downgrade); the eco-ng client moves to `127.0.0.1`. E2E: p2p roundtrip
  and TLS
- [x] **FE** — certs auto-generated on first boot (`servers/certs.py`:
  CA with AKI/SKI — py≥3.13's strict ssl rejected the CAs from Bumper's
  create_certs — and existing ones are NEVER touched), the
  `DEEBOT_ANNOUNCE_IP` fallback for `servers.announce_ip`, and
  single-container deployment: bridge without a profile
  (5223/8007/8883/8181, no 443), Bumper moved to the `legacy` profile,
  `./data/certs` certs mounted RW
- [x] **FD** — `:443` confserver (`servers/conf.py`), **off by default**
  (`servers.conf`). The minimum the **legacy "Ecovacs" app** (XMPP,
  apptype `i_eco_e`) needs, verified against the corpus
  (`pairing-session.log`): `user/login` (use_auth=False, fresh token),
  `user/getAuthCode` (authCode `{country}_{uuid}` — the one the XMPP
  SASL presents), `user.do loginByItToken` (redeems that authCode) and
  `user.do GetDeviceList` (the DeviceRegistry's bots in Bumper's
  `VacBotDevice` shape) + `SetDeviceNick`/`DeleteOneDevice` and the
  statics the app polls (`checkAgreement`, `checkVersion`,
  `homePageAlert`). `lookup.do`/`newauth.do` also on :443 (like Bumper's
  confserver). The **modern app (Ecovacs Home)** is out: its flow signs
  requests with HMAC (`X-ECO-SIGN`) — only its `global_`-apptype login
  answers the `EcoVacsHome_Login` shape. Dropped because they never
  appear in the legacy app's flow (Bumper returned 404 for them and the
  app moved on): the pim catalog, `devmanager.do`, OTA `latest.json`,
  `notify_engine.do`, `getPurchaseUrl`. E2E with curl in the phase
  report; to use it: enable `conf`, publish 443 (commented out in the
  compose) and the Pi-hole DNS already redirects
  `*.ecovacs.com`/`*.ecouser.net`

Compatibility: with everything in `[servers]` off, the bridge remains a
pure client against an external Bumper (the pre-existing suite keeps
passing before each phase; FD adds 19 tests without touching the
existing ones).

Pending after the merge: ~~production regression with the real 930~~
**DONE (2026-10-17)**: the real 930 connects to the monolith, negotiates
lookup→XMPP→TLS, reports fw 1.9.9 / battery 100 / consumables / position,
and the map flows (it required the eco dialect fix: the robot replies by
ctl-id without td/pid — `fix(xmpp): correlación por ctl-id`). The real
eco-ng is pending T8+ hardware. The legacy app against the conf (FD) is
pending a test with a phone.

Rollback if ever needed: `backup/pre-absorcion` + `--profile legacy`.
