# Protocolo XMPP OZMO 930 — notas de ingeniería inversa

Fuente: sesion de emparejamiento + primera limpieza (2026-10-13).
Robot JID: `E000105701860607AB7@115.ecorobot.net/atom`.
Capturas: `data/captures/pairing-session.log` (texto), `*.pcap` (binario).

## Bootstrapping del robot

1. Robot arranca → HTTP plano `POST http://lbo.ecouser.net:8007/lookup.do`
   body `{"todo":"FindBest","service":"EcoMsgNew"}` (UA: `Wget/1.19 (linux-gnueabi)`)
2. Bumper responde `{"ip":"<announce-ip>","port":5223,"result":"ok"}`
3. Robot conecta XMPP (TLS 5223) y hace login con su JID.

## Catálogo de stanzas `td=` observadas (limpieza en curso)

| td | Dir | Contenido | Ejemplo |
|---|---|---|---|
| `Pos` | bot→app | pose en claro: `a`=ángulo, `p`="x,y", `t`, `valid` | `<ctl a="141" p="-211,-6" t="p" td="Pos" valid="1"/>` |
| `trace` | bot→app | traza por trozos: `tr` base64 binario, `trid` seq, `tf`/`tt` | `tr="XQAABAAFAAAAAHW/8AAAAAAA" trid="605959" tf="10" tt="10"` |
| `GetMapM` | bot→app (resp) | chunks de mapa (?) — 88 en una limpieza | pendiente decodificar |
| `MapSet` / `GetMapSet` / `MapSt` | ambos | metadatos de mapas guardados | pendiente |
| `TrM` | bot→app | "trace map" (?) | pendiente |
| `CleanSt` | bot→app | estado limpieza: `a` modo, `l` ?, `s` trigger | `a="1" l="26" s="0" t=""` |
| `ChargeState` | bot→app | estado carga | |
| `SleepStatus` | bot→app | | |
| `GetLifeSpan` | app→bot | consumibles (cepillos/filtros) | |
| `DeviceInfo` | bot→app | info del robot | |
| `SetMapAutoReport` | app→bot | activa el push continuo de mapa | |

## Hipótesis de trabajo (fase 3)

- `Pos.p` son mm en el frame del robot (origen ~ dock). El "transform imposible"
  de ecovacs-deebot.js era para JSON-era; aqui va en claro.
- `trace.tr` base64: traza comprimida por pares (probable int16/delta).
- `GetMapM`: chunks del mapa de ocupacion; reconstruir via trid/reassembly.

## Hallazgos sesion-01 (limpieza parcial, 2026-10-13)

- **El bitmap del mapa NO viajo por XMPP en esta sesion**: frames mas grandes
  del bot = 1081 chars (catalogo MapSet: 65 slots con el mismo map-id
  1295764014, attrs h/w/p/r/c). Sin td="MapM", sin blobs, sin uploads HTTP
  (solo getPurchaseUrl de consumibles).
- Inventario de elementos del bot: iq/query/ctl/ping/charge/clean/wd/
  session/class/chn/MID/root/battery/time/m. `CleanReport` anida `<clean>`
  (st=h=home, type=auto, speed=standard...). `TrM`/`GetTrM` = solo contadores.
- `trace.tr` header constante: `5d 00 00 04 00 05 00 00 00 00` + payload
  int16-pairs + padding. tf/tt = numero de fragmento (1,3,5...). trid fijo
  por sesion (605959).
- Posiciones `Pos` ya extraidas a jsonl (112). Corpus en
  data/captures/session-01/ (log.txt, traffic.pcap, extracted/).

## Plan sesion-02

1. Limpieza con la pantalla de mapa de la app ABIERTA desde el inicio
   (SetMapAutoReport ya activo → ver si asi streamea chunks GetMapM).
2. Ampliar filtro del sniffer a TODO el tcp (excepto 8883) para cazar un
   posible canal directo robot↔app fuera de Bumper.
3. Offline: decodificar trace (int16 LE) y correlacionar con Pos.


## Oraculo visual

Screenshots de la app durante limpieza correlacionados por timestamp con
la captura (guardar junto al corpus).
