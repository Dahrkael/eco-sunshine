# deebot930 — reemplazo 100% LAN de la nube Ecovacs + mapa en vivo en HA

Proyecto: eliminar la dependencia del backend de Ecovacs para robots
Deebot — el OZMO 930 (XMPP, 2018) y los eco-ng T8+ (MQTT) — con un solo
servicio propio que sustituye a la nube oficial: bootstrap (lookup), XMPP,
broker MQTT, API de la app antigua y panel web, más integración Home
Assistant con mapa en vivo. **La nube de Ecovacs ya no interviene en
nada** (su OTA oficial de la era XMPP está retirada — ver
`docs/firmware.md`).

## Arquitectura (contenedor único)

```
Robot 930 ────XMPP :5223───►┐
Robot eco-ng ──MQTT :8883───►┤  bridge = MONOLITO (lookup :8007, XMPP,
                             │  broker MQTT TLS, panel :8181, conf :443*)
App "Ecovacs" antigua ──────►┘  *off por defecto (para emparejar robots
                                nuevos: `servers.conf = true` + 443)

bridge ──► decoder mapa → renderer PNG → MQTT (discovery HA) ──► mosquitto :1883
```

Ya NO hay contenedor bumper (queda como `--profile legacy` para rollback).
Certs TLS auto-generados en instalaciones limpias; los existentes nunca
se tocan. Rollback completo: rama `backup/pre-absorcion` + legacy.

## Estado del proyecto — VALIDADO CON ROBOT REAL

| Fase | Contenido | Estado |
|---|---|---|
| 0-5 | Entorno, emparejamiento, corpus, ingeniería inversa, bridge HA | **✅ hecho** |
| 6 | Transporte MQTT robots T8+ (eco-ng) | **✅ hecho** (spec mqtt-json-spec.md §10) |
| 7 | Absorción de Bumper en el bridge (monolito) | **✅ hecho** (FA–FD, `docs/absorcion-bumper.md`) |
| 8 | Regresión con el 930 real en producción | **✅ hecha** — conecta, fw 1.9.9, batería, consumibles, mapa |
| — | eco-ng real (T8+), app antigua contra FD | pendientes de hardware/móvil |
| — | Custom firmware | **proyecto aparte** (mapa en `docs/firmware.md`) |

Hitos de la regresión real: la **dialecta eco** del 930 (responde por
ctl-id sin td/pid — todo se descartaba hasta que se cazó en el wire) y el
**broadcast de.ecorobot.net** (la batería que Bumper nunca enrutó bien).

El bridge soporta **48 modelos XMPP** (detección por JID) y robots **eco-ng
MQTT** (T8+; class ids conocidos + fallback genérico). Un solo `serve`
maneja ambos protocolos simultáneamente: mismo panel, mismas entidades HA.

## Bridge (Python ≥3.10, ver `bridge/`)

```bash
cd bridge
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m deebot930_bridge serve -c ../configs/bridge-dev.toml --mqtt
# panel: http://localhost:8282 | HA: discovery en mosquitto :1883

.venv/bin/python -m deebot930_bridge probe-mqtt --ca-file ../data/certs/ca.crt   # diagnóstico eco-ng
python tools/mock_mqtt_robot.py    # robot T9 AIVI sintético (eco-ng/MQTT) sin hardware
python tools/extract_930_corpus.py && python tools/mock_930_robot.py  # clon XMPP del 930 REAL
.venv/bin/python -m pytest         # suite (decoders, assembler, estado, routing)
```

El **mock 930** reproduce el comportamiento exacto del robot real a partir
del corpus extraído del log de Bumper (`data/mock930-corpus.json`): misma
spec de mapa con sus CRCs, piezas LZMA reales del piso, trayectoria real de
limpieza (replay al limpiar), lifespans y batería reales, y los formatos de
frame verificados atributo a atributo. Añade su JID a `robots` del config
para que el serve lo adopte (en base no emite nada, como el real).

Config: sección `[bumper_mqtt]` del TOML (host/puerto/CA del broker de
Bumper, `enabled`, robots conocidos para adoptar sin esperar report). El
decoder de mapas detecta **LZMA/zstd por magic number** y la traza eco-ng
usa el formato de 5 bytes/punto (spec §6).

### Nota hbmqtt (broker de Bumper)

El `matches()` de hbmqtt compila `+` como una clase de caracteres sin `-`,
con lo que filtros con `+` no matchean dids eco-ng (UUIDs con guión). El
bridge lo esquiva suscribiendo `iot/#` y filtrando por estructura de topic
(robusto ante cualquier broker); además `bumper/bumper/mqttserver.py` lleva
un monkey-patch con matching MQTT-4.7 correcto (aplica al reconstruir la
imagen de Bumper).

## Decisión tomada: futuro de Bumper (upstream abandonado)

Resuelta en la fase 7 con la vía **(b) backend propio**: los servidores de
Bumper (lookup, XMPP, broker MQTT y la API `:443` de la app) se absorbieron
DENTRO del bridge (fases FA–FD) y el despliegue es un solo contenedor (FE).
El código de Bumper queda vendorizado bajo `bumper/` y su contenedor pasa al
profile `legacy` para rollback durante la transición. La API de la app
(confserver, FD) está implementada pero **off por defecto** — véase
"Usar la app Ecovacs contra el bridge".

El robot habla XMPP (lookup.do pide `EcoMsgNew`) → el protocolo no era
negociable. Vias evaluadas en su día:
- (a) FORK propio (actual): ya vendorizado + parcheado; xmppserver ~800 lineas
  que conocemos a fondo y corpus para regresion. Coste de mantenimiento bajo.
- (b) Backend minimo propio (lookup.do + XMPP + HTTPS de la app): el corpus
  ES la especificacion del dialecto. Elimina el componente abandonado sin
  tocar el robot. Via elegante a medio plazo.
- (c) MQTT-directo (estilo ecovacs-privacy-control, mosquitto local):
  SOLO si el firmware del robot soporta pedir el servicio MQTT (los 900/901
  migraron por OTA; algunos 930 segun privacy-control). Experimento incierto,
  pierde la app como oraculo. No es "dropear XMPP": requeriria que el propio
  robot cambie de canal.

## Operación (DEV, WSL + Docker Desktop)

Desde la absorción de Bumper (fase 7) el despliegue son **dos
contenedores**: el bridge (todo embebido: lookup+XMPP+broker MQTT+panel)
y mosquitto.

```bash
cd /mnt/t/ecovacs
docker compose up -d --build          # bridge + mosquitto
docker logs -f deebot930-bridge       # servidores embebidos + robots
```

Puertos publicados en el host Windows (**192.0.2.10**): 5223 (XMPP),
8007 (lookup), 8883 (MQTT eco-ng), 8181 (panel). El 443 (API de la app)
está **comentado** en el compose: publicarlo solo si activas
`servers.conf` (FD) — véase "Usar la app Ecovacs contra el bridge".

- `BUMPER_ANNOUNCE_IP` (`.env`) = IP que el bridge comunica al robot como
  servidor (vía `DEEBOT_ANNOUNCE_IP` → `servers.announce_ip`; el toml
  puede fijarla y gana). **DEV**: IP LAN de Windows. **PROD**: IP del minipc.
- Certs: `./data/certs` montado RW — en una instalación limpia el bridge
  auto-genera CA+cert al primer arranque (los robots deberán emparejar
  contra esa CA). Si ya existen (migración desde Bumper) NO se tocan: los
  robots siguen confiando en la CA legada.
- Estado persistente en `./data/bridge/` (mapa, posiciones, registry) —
  no versionar (`data/` está en `.gitignore`).
- **Bumper legacy** (rollback/comparación durante la transición):
  `docker compose --profile legacy up -d bumper` — nunca a la vez que el
  bridge (mismos puertos publicados). Con `[servers]` off en el toml, el
  bridge vuelve a ser cliente puro de ese Bumper.

## DNS (Pi-hole) — hacer ANTES de emparejar el robot

Opción recomendada (wildcard, a prueba de futuros subdominios — la app
obtiene sus dominios dinámicamente del backend, ninguna lista exacta
es fiable):

**1. dnsmasq custom (wildcard)**: `docs/pihole/99-deebot-bumper.conf`.
   Redirige:

```
ecouser.net / ecovacs.com / ecovacs.net  →  192.0.2.10
```

   ⚠️ **Pi-hole v6 no lee `/etc/dnsmasq.d/` por defecto**: aplicar via
   `Settings → All settings (expert) → Miscellaneous → dnsmasq_lines`
   (pega las 3 líneas; persiste en pihole.toml) o activando
   `etc_dnsmasq_d` + montando el fichero como volumen.

2. Alternativa Local DNS Records (GUI, ~40 dominios exactos):
   `docs/pihole/local-dns-records.md` + regex de bloqueo anti-fuga.

El móvil Android (app "Ecovacs" antigua) debe resolver por el Pi-hole:
Wi-Fi → opciones avanzadas → DNS privado/IP = IP del Pi-hole.

## Firewall Windows

Docker Desktop publica los puertos; si el robot/app no conectan, permitir
inbound 5223/tcp, 8007/tcp, 8883/tcp (y 443/tcp si activas el conf) para
`com.docker.backend` (Perfil privado de la red Wi-Fi).

## Usar la app Ecovacs contra el bridge (FD, opcional)

La **app antigua "Ecovacs"** (robots XMPP) puede loguearse, listar los
robots y mandar comandos contra el bridge, sin Bumper legacy. Soportada:
**solo la app antigua** — la moderna (Ecovacs Home) firma sus requests
con HMAC (`X-ECO-SIGN`) y no se implementa.

1. `configs/bridge.toml`: descomenta `conf = true` en `[servers]`.
2. `docker-compose.yml`: descomenta el mapeo `- "443:443"` del bridge.
3. `docker compose up -d --build` y reinstala/abre la app antigua en el
   móvil (el DNS del Pi-hole ya redirige `*.ecovacs.com`/`*.ecouser.net`
   al bridge; la app instalará la CA del cert si hace falta — misma CA
   que ya usa el flujo de emparejamiento).
4. Login con cualquier cuenta (use_auth=False, como Bumper): la app hace
   `user/login` → `user/getAuthCode` → `user.do loginByItToken` →
   `user.do GetDeviceList` (los bots registrados por XMPP/MQTT) y conecta
   por XMPP al :5223 embebido.

## Checklist de emparejamiento (fase 1)

> La app antigua necesita la API `:443`: con FD basta activar
> `servers.conf` + publicar el 443 (arriba). La alternativa con
> **bumper legacy** (`--profile legacy`) sigue válida para rollback —
> nunca a la vez que el conf del bridge (mismo puerto).

1. [ ] Reserva DHCP con IP fija para el robot (router).
2. [ ] Pi-hole respondiendo por el wildcard (probar desde el móvil:
       `nslookup lbo.ecovacs.net` → 192.0.2.10).
3. [ ] Bridge arriba (`docker compose ps` — servidores embebidos on).
4. [ ] Robot en modo emparejamiento (botón WiFi hasta voz/parpadeo).
5. [ ] App antigua "Ecovacs" → añadir dispositivo → red del robot →
       credenciales Wi-Fi de casa.
6. [ ] Verificar en `docker logs deebot930-bridge`: login del bot XMPP
       (~30 s tras reiniciar el robot).
7. [ ] Probar comando de limpieza desde la app (todo local).

## Migración a producción (minipc)

1. Copiar el proyecto (incluido `./data/` para conservar certs y estado).
2. `.env`: `BUMPER_ANNOUNCE_IP=<IP-minipc>` (la consume el bridge).
3. `docker compose up -d --build` (bridge + mosquitto; bumper solo con
   `--profile legacy`).
4. Pi-hole: actualizar las 3 líneas `address=` a la IP del minipc y
   recargar FTL.
5. Reiniciar el robot y el móvil (re-resolución DNS).

Los certs de `./data/certs` viajan con la copia: los robots NO tienen que
re-emparejar (la CA que ya confían es la misma que presentan los
servidores embebidos del bridge).

## Referencias

- Bumper: https://github.com/bmartin5692/bumper (docs: bumper.readthedocs.io)
- sucks + fork bmartin5692 (cliente histórico XMPP, base de ha_ecovacs_bumper)
- ecovacs-deebot.js (mrbungle64) — issue #10: mapa en vivo sin resolver
- DEF CON 32: "Reverse engineering and hacking Ecovacs robots" (Giese/Luedtke)
- valetudo-map-card (formato MQTT objetivo del bridge)
