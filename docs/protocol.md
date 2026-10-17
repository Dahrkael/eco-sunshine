# OZMO 930 XMPP protocol — reverse-engineering notes

Source: pairing session + first cleaning run (2026-10-13).
Robot JID: `E000105701860607AB7@115.ecorobot.net/atom`.
Captures: `data/captures/pairing-session.log` (text), `*.pcap` (binary).

## Robot bootstrapping

1. Robot boots → plain HTTP `POST http://lbo.ecouser.net:8007/lookup.do`
   body `{"todo":"FindBest","service":"EcoMsgNew"}` (UA: `Wget/1.19 (linux-gnueabi)`)
2. Bumper answers `{"ip":"<announce-ip>","port":5223,"result":"ok"}`
3. Robot connects XMPP (TLS 5223) and logs in with its JID.

## Catalog of observed `td=` stanzas (during a cleaning run)

| td | Dir | Content | Example |
|---|---|---|---|
| `Pos` | bot→app | plaintext pose: `a`=angle, `p`="x,y", `t`, `valid` | `<ctl a="141" p="-211,-6" t="p" td="Pos" valid="1"/>` |
| `trace` | bot→app | trace in chunks: `tr` base64 binary, `trid` seq, `tf`/`tt` | `tr="XQAABAAFAAAAAHW/8AAAAAAA" trid="605959" tf="10" tt="10"` |
| `GetMapM` | bot→app (resp) | map chunks (?) — 88 in one cleaning run | pending decode |
| `MapSet` / `GetMapSet` / `MapSt` | both | saved-map metadata | pending |
| `TrM` | bot→app | "trace map" (?) | pending |
| `CleanSt` | bot→app | cleaning state: `a` mode, `l` ?, `s` trigger | `a="1" l="26" s="0" t=""` |
| `ChargeState` | bot→app | charge state | |
| `SleepStatus` | bot→app | | |
| `GetLifeSpan` | app→bot | consumables (brushes/filters) | |
| `DeviceInfo` | bot→app | robot info | |
| `SetMapAutoReport` | app→bot | enables continuous map push | |

## Working hypotheses (phase 3)

- `Pos.p` is mm in the robot's frame (origin ~ the dock). ecovacs-deebot.js's
  "impossible transform" was for the JSON era; here it travels in plaintext.
- `trace.tr` base64: trace compressed as pairs (probably int16/delta).
- `GetMapM`: occupancy-map chunks; rebuild via trid/reassembly.

## session-01 findings (partial cleaning, 2026-10-13)

- **The map bitmap did NOT travel over XMPP in this session**: the bot's
  largest frames = 1081 chars (MapSet catalog: 65 slots with the same
  map-id 1295764014, attrs h/w/p/r/c). No td="MapM", no blobs, no HTTP
  uploads (only the consumables getPurchaseUrl).
- Inventory of the bot's elements: iq/query/ctl/ping/charge/clean/wd/
  session/class/chn/MID/root/battery/time/m. `CleanReport` nests `<clean>`
  (st=h=home, type=auto, speed=standard...). `TrM`/`GetTrM` = counters only.
- `trace.tr` constant header: `5d 00 00 04 00 05 00 00 00 00` + int16-pairs
  payload + padding. tf/tt = fragment number (1,3,5...). trid fixed per
  session (605959).
- `Pos` positions already extracted to jsonl (112). Corpus in
  data/captures/session-01/ (log.txt, traffic.pcap, extracted/).

## session-02 plan

1. Cleaning run with the app's map screen OPEN from the start
   (SetMapAutoReport already on → see if it streams GetMapM chunks that way).
2. Widen the sniffer filter to ALL tcp (except 8883) to hunt for a possible
   direct robot↔app channel outside Bumper.
3. Offline: decode the trace (int16 LE) and correlate it with Pos.

## session-02 (cleaning + re-entry, 2026-10-13)

- **The map travels 100% over XMPP** (pcap conversations: only
  5223/8007/443; no direct robot↔app channel).
- Map protocol: the app sends `<ctl td="PullMP" pid="27"/>` (incrementing
  pid, 27,28...) and the robot answers with pieces
  `<ctl i="<mapid>" p="<base64>">`.
- Pieces: constant header `5d 00 00 04 00 10 27 00 00` + variable payload
  (53–749 B, average 335). 17 unique pieces for the partial cleaning run.
- The robot DOES emit pieces during cleaning (t=20:02:12) — the app doesn't
  render them live (only when re-entering the map). For the bridge it
  doesn't matter: we'll do our own PullMP + render.
- SetMapAutoReport x127-288: the app keeps re-arming it constantly.
- Corpus: data/captures/session-02/ (log, wide-filter pcap, extracted/).

## Pending decode (phase 3 active)

~~1. Piece payload p=~~ **SOLVED 2026-10-13** (recipe from
   ecovacs-privacy-control/api_client/responses.go validated against our
   corpus): pieces = LZMA-alone with a header of props(1)+dict(u32)+size(u32);
   rebuild props+dict+size(u64)+data[9:] → 10,000 bytes (100x100 cells).
   Values: 0=none, 1=floor, 2=wall, 3=carpet. See `tools/decode_map.py`
   and `data/captures/session-02/map-decoded.png`.
   Geometry (attrs of the MapSet frame we mistook for a "catalog"):
   `i`=mapid, `w`/`h`=100 cells per piece, `c`/`r`=8 pieces per axis,
   `p`=50 (scale), `m`=CRCs per piece. Total grid 800x800, 64 pieces.
2. trace: LEB128 varints, ~2 cm unit; constant pair (48,64) unidentified.
3. pid ↔ piece relationship and how the app knows the range (MapSt?).
   (Note: in the corpus the response pids don't travel explicitly — the
   bridge will PullMP by pid and place each piece by its known pid.)

## Mapping solved (2026-10-14, 82% correlation over 23k positions)

- Piece placement: **row-major** (rowStart=pid//8, colStart=pid%8) —
   like privacy-control. The column-major experiment was a visual illusion.
- **Swapped axes**: cell_row = x_mm/50 + ORIGIN_ROW(400);
   cell_column = y_mm/50 + ORIGIN_COL(390). The robot's mm frame has X/Y
   permuted with respect to the grid, plus a shifted origin (constants in
   map_pieces.py).
- Validation: 82% of 23k positions fall on cells with data (the rest:
   LDS rays over still-unknown cells — normal).
- Mirrors: they confuse the LDS (ghost rays on the map) — a known
   artifact, also visible in the app.

## MAP FORMAT (solved and validated — definitive summary)

### MapP piece (the map travels in 100x100-cell pieces)
- Robot pushes: `<iq type="set"><query><ctl i="<mapid>" pid="<N>" td="MapP" p="<base64>"/></query></iq>`
- `p` = base64 → bytes: `[0]` LZMA props (0x5d), `[1:5]` dict u32 LE (256 KB),
  `[5:9]` size u32 LE (10000), `[9:]` LZMA1 stream
- Decompresses to 10,000 bytes = 100x100 cells: 0=unknown, 1=floor,
  2=wall, 3=carpet
- Piece N occupies block (piece-row N//8, column N%8) of the 800x800 grid
- Validity: crc32(decompressed) == crcs[pid] of the current spec (m=)

### GetMapM spec (geometry + CRCs)
`<ctl c="8" h="100" i="<mapid>" m="<crc0,crc1,...>" p="50" r="8" w="100"/>`
- w/h = cells per piece; c/r = pieces per axis (8x8=64); p = mm per cell
- m = 64 CRC32s, one per piece (crc 1295764014 = empty piece)
- The app/bridge requests pieces with `<ctl pid="<N>" td="PullMP"/>`

### Position (Pos push / GetPos)
- `p="x,y"` in **mm** in the robot's frame; `a` = angle
- grid conversion: row = x/50 + 400, col = y/50 + 390 (axes permuted with
  respect to the grid + empirical offset; 82% correlation over 23k points)

### Trace
- base64 → LEB128 varints, ~2 cm unit (x20 = mm)

### State
CleanSt/CleanReport (st=h/i/p + <clean st type speed>), ChargeState
(SlotCharging/WireCharging/Going/Idle), BatteryInfo (<battery power>),
GetLifeSpan (consumables by type), SleepStatus, GetError

### Commands
Clean (<clean act=s/p type=auto speed=standard>), Charge (<charge type=go>),
SetMapAutoReport (on=1 — the app keeps re-arming it), PullMP (pid),
GetTr (trace pages), PlaySound (sid), SetOnOff/GetOnOff, GetMapSet tp=vw

## DEFINITIVE VERIFICATION via APK decompile (jadx, 2026-10-14)

Source: com/ecovacs/lib_iot_client/robot/MapInfo.java (Ecovacs app 1.4.9).

- init(row_grid=100, column_grid=100, row_piece=8, column_piece=8)
  → 800x800-cell grid, piece = 100x100, p=50mm/cell
- UpdateMapBuffer(pid, piece): blockRow = pid/8; colOffset = (pid%8)*100;
  rowOffset = blockRow*100; buffer[rowOffset+r][colOffset+c] = piece[r*100+c]
  → **row-major, no transpose** (confirms the original row-major)
- MapP: decode7z (LZMA) + CRC32(decompressed); it only updates if the CRC
  differs from the stored one (0 = first time)
- **CleanedMap**: the robot pushes the cleaned area per piece:
  <ctl td="CleanedMap" sts=<ts> ts=<ts> p="<base64>" pid="<pid>" total="<N>"/>
  → a "cleaned" overlay with the robot's own data source (to implement in
  the bridge)
- Position → map: mm / pixelWidth(50)

The current partial map is EXPECTED: the robot rebuilt the map today
(MapSt st=building) and the 4-min cleaning only covered part of the house.
It grows with every cleaning (it persists on the robot except on
reset/relocate).

## Intel imported from external repos (reviewed 2026-10-13)

- **kushagharahi/ecovacs-privacy-control**: the LZMA map pipeline (above),
  the PlaySound sid→phrase sound table ("30"="I am here"...), app-less
  WiFi provisioning: `POST http://192.168.0.1:8888/rcp.do {"td":"SetApConfig",
  "s":SSID,"p":PASS,...}`, the `PullM tp=sa msid mid` command (room
  subsets), MQTT topics `iot/atr/<evt>/...` + `iot/p2p/<cmd>/...`
  (transport plan C: robot→mosquitto directly), FIQ debugger serial on the
  900/901, OTA `portal.ecouser.net/api/ota/...`.
- **m8schmit/ecovacs-stack** (T9, JSON-era): architectural reference only.


## Trace decode (offline, 2026-10-13)

- The payload is NOT plain int16: it's protobuf-style **varint (LEB128)**.
- Empirical correlation with Pos: varint1 × 20 ≈ Pos.x_mm (chunks 1 and 3:
  34→700mm, 115→2312mm; ratio 20.6/20.1). Trace unit ≈ 2 cm.
- Constant pair (varints 48,64) after the first one: unidentified.
- Next: port the `getMapTrace` decoder from ecovacs-deebot.js (the 950
  series uses the same style) and validate it against the full path.



## Visual oracle

Screenshots of the app during cleaning, correlated with the capture by
timestamp (store them next to the corpus).
