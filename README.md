# deebot930 — OZMO 930 cloud-free + mapa en vivo en Home Assistant

Proyecto: eliminar la dependencia del backend de Ecovacs para un Deebot
OZMO 930 (XMPP, 2018) y darle control 100% LAN + **mapa en vivo** en Home
Assistant, escribiendo el código que falta en el ecosistema.

## Arquitectura

```
Robot 930 ──XMPP:5223──► Bumper (vendorizado, instrumentable) ──► docker (WSL dev → minipc prod)
App "Ecovacs" antigua ──► (oraculo de emparejamiento/captura; movil con DNS del Pi-hole)
bridge (nuevo, Python) ──► decoder mapa → renderer PNG → MQTT (formato Valetudo) ──► HA
```

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
| 7 | Absorción de Bumper en el bridge | plan: `docs/absorcion-bumper.md` |

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
python tools/mock_mqtt_robot.py    # robot T9 AIVI sintético para pruebas sin hardware
.venv/bin/python -m pytest         # suite (decoders, assembler, estado, routing)
```

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

## Decision pendiente: futuro de Bumper (upstream abandonado)

El robot habla XMPP (lookup.do pide `EcoMsgNew`) → el protocolo no es
negociable. Vias evaluadas:
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

```bash
cd /mnt/t/ecovacs
docker compose up -d --build
docker logs -f deebot930-bumper     # ver conexiones del robot/app
```

Puertos publicados en el host Windows (**192.0.2.10**): 443, 5223, 8007, 8883.

- `BUMPER_ANNOUNCE_IP` (`.env`) = IP que Bumper comunica al robot como
  servidor XMPP. **DEV**: IP LAN de Windows. **PROD**: IP del minipc.
- Certs/db persistentes en `./data/` (no versionar).

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
inbound 443/tcp, 5223/tcp, 8007/tcp, 8883/tcp para `com.docker.backend`
(Perfil privado de la red Wi-Fi).

## Checklist de emparejamiento (fase 1)

1. [ ] Reserva DHCP con IP fija para el robot (router).
2. [ ] Pi-hole respondiendo por el wildcard (probar desde el móvil:
       `nslookup lbo.ecovacs.net` → 192.0.2.10).
3. [ ] Bumper arriba (`docker compose ps`).
4. [ ] Robot en modo emparejamiento (botón WiFi hasta voz/parpadeo).
5. [ ] App antigua "Ecovacs" → añadir dispositivo → red del robot →
       credenciales Wi-Fi de casa.
6. [ ] Verificar en `docker logs deebot930-bumper`: login del bot XMPP
       (~30 s tras reiniciar el robot).
7. [ ] Probar comando de limpieza desde la app (todo local).

## Migración a producción (minipc)

1. Copiar el proyecto (incluido `./data/` para conservar certs y estado).
2. `.env`: `BUMPER_ANNOUNCE_IP=<IP-minipc>`.
3. `docker compose up -d --build`.
4. Pi-hole: actualizar las 3 líneas `address=` a la IP del minipc y
   recargar FTL.
5. Reiniciar el robot y el móvil (re-resolución DNS).

## Referencias

- Bumper: https://github.com/bmartin5692/bumper (docs: bumper.readthedocs.io)
- sucks + fork bmartin5692 (cliente histórico XMPP, base de ha_ecovacs_bumper)
- ecovacs-deebot.js (mrbungle64) — issue #10: mapa en vivo sin resolver
- DEF CON 32: "Reverse engineering and hacking Ecovacs robots" (Giese/Luedtke)
- valetudo-map-card (formato MQTT objetivo del bridge)
