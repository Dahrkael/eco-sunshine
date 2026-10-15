# Absorción de Bumper en el bridge — mapa de fusión

> Estado: plan. Este documento describe cómo llevar los servidores de Bumper
> DENTRO del proceso del bridge para tener un solo programa gestionando el
> flow completo (DNS-spoof → auth → XMPP/MQTT → HA).

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
| `mqttserver.py` (MQTTServer + plugin auth) | `deebot930_bridge/bumper_server/mqtt.py` | broker hbmqtt embebido; el helperbot se ELIMINA (MqttTransport lo sustituye) |
| `xmppserver.py` | `deebot930_bridge/bumper_server/xmpp.py` | sin cambios funcionales |
| `confserver.py` (:443/:8007) | `deebot930_bridge/bumper_server/conf.py` | auth de app + lbo lookup; el robot lo necesita para encontrar el servidor |
| `db.py` (bots/tokens/users) | migrar a `DeviceId` + store propio | la tabla `bots` pasa a ser el registro del bridge |
| `create_certs` + data/certs | gestión de certs del bridge | generar al primer arranque si no existen |

## Qué se elimina

- `MQTTHelperBot` — el bridge ya publica comandos p2p directamente.
- Doble definición de "robot conocido" (DB de bumper vs config del bridge).
- Contenedor bumper del docker-compose (uno menos).

## Trabajo real que supondrá (por orden de dolor)

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

- [ ] `bridge serve` arranca xmppserver+broker+confserver embebidos
- [ ] 930 real conecta y funciona (regresión XMPP: corpus + panel)
- [ ] mock eco-ng conecta al broker embebido y funciona (regresión MQTT)
- [ ] helperbot eliminado, `docker-compose` con un solo servicio bridge
- [ ] tests: suite existente + smoke test de arranque de servidores
