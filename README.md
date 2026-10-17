# deebot930 — OZMO 930 cloud-free + mapa en vivo en Home Assistant

Proyecto: eliminar la dependencia del backend de Ecovacs para un Deebot
OZMO 930 (XMPP, 2018) y darle control 100% LAN + **mapa en vivo** en Home
Assistant, escribiendo el código que falta en el ecosistema.

## Arquitectura

```
Robot 930 ────XMPP :5223───►┐
Robot eco-ng ──MQTT :8883───►┤  bridge (servidores embebidos: lookup :8007,
                             │  XMPP, broker MQTT TLS, panel :8181)
App "Ecovacs" antigua ──────►┘  (oráculo de emparejamiento; DNS del Pi-hole)

bridge ──► decoder mapa → renderer PNG → MQTT (formato Valetudo) ──► HA (mosquitto :1883)
```

Desde la absorción de Bumper (fase 7) el bridge YA NO necesita el
contenedor `bumper`: los servidores (lookup, XMPP, broker MQTT eco-ng)
viven en el proceso del bridge. Bumper queda como `legacy` opcional.

## Estado del proyecto

| Fase | Contenido | Estado |
|---|---|---|
| 0 | Entorno: compose, DNS, IP fija, app antigua | **✅ hecho** |
| 1 | Emparejamiento controlado contra Bumper | **✅ hecho** |
| 2 | Captura de trazas XMPP (oraculo: app) | **✅ hecho** (corpus sesiones 1-3) |
| 3 | Ingeniería inversa: posición + mapa | **✅ hecho** (origen 86.8% traza/suelo) |
| 4 | Bridge → MQTT + discovery HA | **✅ hecho** (14 entidades, panel web, multi-robot) |
| 5 | Aislamiento (firewall) + dashboard HA | **✅ hecho** |
| 6 | Transporte MQTT robots T8+ (eco-ng) | **✅ hecho** (spec mqtt-json-spec.md §10) |
| 7 | Absorción de Bumper en el bridge | **en marcha**: FA–FE hechos (`docs/absorcion-bumper.md`), FD opcional |

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
Bumper (lookup, XMPP, broker MQTT) se absorbieron DENTRO del bridge
(fases FA–FC) y el despliegue es un solo contenedor (FE). El código de
Bumper queda vendorizado bajo `bumper/` y su contenedor pasa al profile
`legacy` para rollback durante la transición. La API de la app (`:443`,
confserver) no se absorbió todavía (FD, opcional).

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
8007 (lookup), 8883 (MQTT eco-ng), 8181 (panel). El 443 ya no se publica
(API de la app = FD, opcional).

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
inbound 5223/tcp, 8007/tcp, 8883/tcp para `com.docker.backend`
(Perfil privado de la red Wi-Fi). El 443 ya no se usa (FD pendiente).

## Checklist de emparejamiento (fase 1)

> ⚠️ El emparejamiento usa la app Ecovacs antigua, que necesita la API
> `:443` — hoy solo existe en el **bumper legacy** (`--profile legacy`,
> FD pendiente). Para emparejar un robot nuevo: levanta legacy, empareja,
> y vuelve al stack normal (los robots ya emparejados no necesitan nada).

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
