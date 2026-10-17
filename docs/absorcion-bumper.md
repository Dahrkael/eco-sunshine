# Absorción de Bumper en el bridge — mapa de fusión

> Estado: **FA–FE implementadas** (rama `feat/absorcion-bumper`); queda
> **FD** (confserver `:443`) como opcional. Este documento empezó como plan
> y ahora registra lo realmente hecho — ver el checklist final.

## Principio

El bridge ya habla con "Bumper" solo por interfaces estables:

| Costura | Hoy | Tras la fusión |
|---|---|---|
| `MqttTransport` | cliente TLS a `bumper_mqtt.host:8883` | apunta a `127.0.0.1` del broker hbmqtt embebido (o adaptador in-process) — cero cambios en serve/commanders |
| `BumperXMPPClient` | cliente TLS a `bumper.host:5223` | apunta a `127.0.0.1` del xmppserver embebido — cero cambios |
| `DeviceId` + `robots` dict | registro propio del bridge | se convierte en EL registro de dispositivos (sustituye `bumper.bot_add()`/SQLite) |

**La regla de oro: embeber primero, refactorizar después.** Los servidores de
Bumper ya son librerías asyncio dentro de su proceso (`xmppserver.py`,
`mqttserver.py`); la fusión es moverlos al entrypoint del bridge, no reescribirlos.

## Qué se mueve

| Módulo Bumper | Destino | Notas |
|---|---|---|
| `mqttserver.py` (MQTTServer + plugin auth) | `deebot930_bridge/servers/mqtt.py` | **hecho (FC)**: broker `amqtt` embebido; el helperbot se ELIMINÓ (MqttTransport lo sustituye) |
| `xmppserver.py` | `deebot930_bridge/servers/xmpp.py` | **hecho (FB)**: vendorizado casi literal, registro inyectado |
| `confserver.py` (:443/:8007) | `servers/lookup.py` (:8007) + `servers/conf.py` (:443) | lookup **hecho (FA)**; el resto del confserver (auth de la app) = **FD, pendiente** |
| `db.py` (bots/tokens/users) | `deebot930_bridge/servers/registry.py` | **hecho (FB)**: DeviceRegistry JSON junto al state_file (bumper.db muere) |
| `create_certs` + data/certs | `deebot930_bridge/servers/certs.py` | **hecho (FE)**: CA+cert auto-generados al primer arranque si faltan; los existentes jamás se tocan |

## Qué se elimina

- `MQTTHelperBot` — el bridge ya publica comandos p2p directamente.
- Doble definición de "robot conocido" (DB de bumper vs config del bridge).
- Contenedor bumper del docker-compose (uno menos).

## Trabajo real que supondrá (por orden de dolor)

> Nota: sección del plan original — así salió en la práctica: (1) amqtt
> sin fricción, (2) los clientes TLS ya lo sorteaban y FE genera CAs con
> AKI/SKI, (3) resuelto en FA (solo lo activo) con shutdown graceful en FC,
> (4) `[servers]` en FA + `DEEBOT_ANNOUNCE_IP` en FE (el toml no lee env).

1. **Dependencias py3.7 → 3.12+**: `hbmqtt` está archivado → fork `amqtt`;
   `pkg_resources` (eliminado en 3.12) → `importlib`; `passlib` → opcional.
2. **Certificados**: el generador de Bumper produce CAs sin AKI que el modo
   estricto de py≥3.12 rechaza (ya lo sorteamos en ambos clientes TLS;
   el broker embebido hereda el problema → mejorar create_certs).
3. **Ciclo de vida**: un solo `asyncio.run()` con graceful shutdown de
   xmpp + mqtt-broker + conf + transports + panel.
4. **Config unificada**: `[bumper]` (cliente) se fusiona con los flags de
   servidor (`listen`, `announce_ip`…).

## Checklist de la fusión

Estado por fases (una saga de commits cada una, `git log --oneline`):

- [x] **FA** — config `[servers]` (toggles/puertos/certs por componente),
  lookup embebido `:8007` (POST /lookup.do, JSON compacto) y ciclo de vida
  "serve arranca SOLO lo activo"
- [x] **FB** — `DeviceRegistry` (persistencia JSON junto al state_file,
  sustituye a bumper.db) + servidor XMPP embebido `:5223` vendorizado; el
  cliente XMPP del bridge pasa a `127.0.0.1`. E2E: mock 930 autentica
  contra el embebido, corpus aceptado, estado/mapa/base OK
- [x] **FC** — broker MQTT embebido `:8883` (amqtt; auth estilo Bumper,
  '+' = un nivel exacto con guiones, TLS explícito sin downgrade); el
  cliente eco-ng pasa a `127.0.0.1`. E2E: roundtrip p2p y TLS
- [x] **FE** — certs auto-generados al primer arranque (`servers/certs.py`:
  CA con AKI/SKI — el ssl estricto de py≥3.13 rechazaba las CAs del
  create_certs de Bumper — y los existentes JAMÁS se tocan), fallback
  `DEEBOT_ANNOUNCE_IP` para `servers.announce_ip`, y despliegue de un solo
  contenedor: bridge sin profile (5223/8007/8883/8181, sin 443), Bumper al
  profile `legacy`, certs `./data/certs` montados RW
- [ ] **FD** — confserver `:443` (login/API de la app Ecovacs): opcional,
  solo si se quiere volver a usar la app como cliente/oráculo. Hasta
  entonces el 443 no se publica y `servers.conf` avisa "llega en FD"

Compatibilidad: con `[servers]` todo off el bridge sigue siendo cliente
puro contra un Bumper externo (los 97 tests de antes de FE siguen igual).

Pendiente tras el merge: regresión en producción con el 930 real y un
eco-ng real (los E2E de FB/FC usaron los mocks del corpus).
