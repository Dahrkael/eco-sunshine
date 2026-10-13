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
| 0 | Entorno: compose, DNS, IP fija, app antigua | **en curso** |
| 1 | Emparejamiento controlado contra Bumper | pendiente |
| 2 | Captura de trazas XMPP (oráculo: app) | pendiente |
| 3 | Ingeniería inversa: posición + mapa | pendiente |
| 4 | Bridge → MQTT Valetudo-compatible | pendiente |
| 5 | Aislamiento (firewall) + dashboard HA | pendiente |

## Operación (DEV, WSL + Docker Desktop)

```bash
cd /tmp/opencode/deebot930
docker compose up -d --build
docker logs -f deebot930-bumper     # ver conexiones del robot/app
```

Puertos publicados en el host Windows (**192.0.2.10**): 443, 5223, 8007, 8883.

- `BUMPER_ANNOUNCE_IP` (`.env`) = IP que Bumper comunica al robot como
  servidor XMPP. **DEV**: IP LAN de Windows. **PROD**: IP del minipc.
- Certs/db persistentes en `./data/` (no versionar).

## DNS (Pi-hole) — hacer ANTES de emparejar el robot

Copiar `docs/pihole/99-deebot-bumper.conf` a `/etc/dnsmasq.d/` del host de
Pi-hole (o montarlo en el contenedor) y recargar FTL. Redirige wildcard:

```
ecouser.net / ecovacs.com / ecovacs.net  →  192.0.2.10
```

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
