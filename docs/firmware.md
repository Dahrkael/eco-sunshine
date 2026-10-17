# Ecovacs firmware — complete research (OZMO 930 and family)

> **Scope of THIS repo**: replacing Ecovacs' official cloud (lookup,
> XMPP/MQTT, app API) — already achieved with the monolith. The future
> **custom firmware** line (dump/UART/patching/flashing) is being
> investigated in a separate project; this document records everything
> learned so it can be reused there (§3-§5).
>
> Worktree `research/firmware` (2026-10-17, merged). Sources: own corpus
> (681 MB Bumper log + pcaps), decompiles of both apps (Ecovacs 1.4.9 and
> Home 3.16.1), `ecovacs-firmware-tools` source (denysvitali, 2025), the
> DEF CON 32 / HITCON CMT 2024 talks (Giese & Luedtke), live probes
> against the official OTA API.

## 1. What firmware our robot runs

**OZMO 930 (class `115`): firmware `1.9.9`** (module `fw0`), read from the
corpus (2026-10-13):

```xml
<!-- app → robot (XMPP) -->
<ctl id="34196105" name="FW" td="GetVersion"/>
<!-- robot → app (multi-line on the wire!) -->
<ctl id="34196105" ret="ok">
  <ver name="FW">1.9.9</ver>
</ctl>
```

- The version travels in a **child element** `<ver name="FW">TEXT</ver>`,
  not in an attribute (our parser was wrong → `fw: None`; fixed on this
  branch with tests).
- The `FW` module is the main one (`fw0` in the OTA API). The legacy app
  also asked for other modules' versions by changing `name=`.
- `DeviceInfo` (pushed on connect) does NOT carry a version: only
  MID/class/chn/wd.

## 2. How the legacy app flashes (XMPP era) — verified in the decompile

Full flow from `FirmwareVersionActivity` + `DR930.java` +
`EcoRobotProtocol.java` (Ecovacs app 1.4.9):

1. **Current version**: `GetVersion name=FW` over XMPP (above).
2. **Check for updates** (HTTP, not XMPP):
   ```
   GET https://{portal}/api/ota/products/wukong/class/{cls}/firmware/latest.json
       ?did={did}&sn={sn}&ver={current}&module=fw0&ts={ms}&lang={lang}
   → {"version":"x.y.z","fw0":{"changeLog":"<b64>","url":"...","size":N,
       "checkSum":"<md5|sha256>"},"force":false}
   ```
   - `{portal}` = `portal-ww.ecouser.net` (WW) / `portal.ecouser.net` (CN)
     — resolved via `DataParseUtil.getDlUrl()` from the account's country.
3. **Requirements** (the app enforces them before sending anything):
   battery ≥ 40% AND charging on the dock.
4. **Trigger the update** (XMPP):
   ```xml
   <ctl id="..." td="fw" act="QuickUpgrade"/>
   ```
   **THE ROBOT DOWNLOADS THE FIRMWARE BY ITSELF.** The app passes neither
   URL nor binary: the robot asks for the OTA server at boot with
   `lookup.do {"todo":"FindBest","service":"EcoUpdate"}` and downloads
   from there.
5. **Progress**:
   - Robot push: `td="fw"` frames with attrs `status`/`upgrade_stattus`
     (sic) / `progress`.
   - Polling: `<ctl td="fw" act="GetStatus"/>`; and `GetProgress` with
     `act={download|upgrade}` for the voice packs.
6. App HTTP UA: `Dalvik/2.1.0 (Linux; U; Android 5.1.1; …)`.

### State TODAY of the official OTA infrastructure (measured 2026-10-17)

| Endpoint | Status |
|---|---|
| `portal-ww.ecouser.net` / `portal-eu.ecouser.net` (`slb-oldweb-iot-*`) | DNS alive (CNAME → 8.211.16.120) but `/api/ota/...` → **"Not Found"** for every class tested (115, 659yh8/T9AIVI, yna5xh, vsc5ia) |
| `EcoUpdate` `47.88.66.164:8005` (where lookup.do points) | **timeout** — dead |

Conclusion: **the official XMPP-era OTA is retired**. A robot on old
firmware can no longer update itself through the Ecovacs cloud.
(`ecovacs-firmware-tools` documented working downloads in 2025 —
they switched it off in the meantime; some modern firmwares remain in
the new API used by the Home app, out of reach for XMPP robots.)

⚠️ Careful: our **Pi-hole wildcard** redirects `*.ecouser.net` to the
bridge — to test real endpoints you must resolve with `8.8.8.8` and use
`curl --resolve`.

## 3. Firmware format (T9+ era, `ecovacs-firmware-tools`)

Container of encrypted sections (the `fw0` firmware of T9AF_px30 v1.4.9
as the tool's example):

```
[8 B section header][AES-128-CBC data][64 B ASCII sha256hex checksum]
 header: u8 unkn1(=1), u8 type(=1), u16 unkn2 (real type = unkn2>>12),
         u32 size (LE)
 sections: manifest (JSON), pre_upgrade_script (sh), normal_boot (kernel),
           normal_fs (SquashFS), mcu, post_upgrade_script (sh)
 manifest: {"fw_ver","hw_ver","product","release_date",
            "sections":[{name,type,size,skip,flash_cmd}]}
```

**Per-section encryption** (AES-128-CBC, deterministic derivation — no
per-device secrets, everything is in the format):

```go
snprintf("ZWNvX2Z3X3RhcmdldCAECO-PT1jdSAtpx30byBtYW4%dy5iaW4%s825xxjeff-hk@126.com",
         typeDigit, sizeHex)
b64 := base64(...)          // standard
seed := b64[4:][:len(snprintf_result)]   // truncates to the original length
h := sha256hex(seed)
iv  := h[0:16]  (ASCII)     key := h[35:51] (ASCII, 16 bytes)
```

- The checksum is SHA256 hex of `header+data` (64 ASCII chars).
- PKCS7 padding when decrypting.

**WARNING**: this is the T9+/N8-era format (2020+). The 930 (2018,
earlier platform) may use a different container — we'll know once a 930
dump is in hand. The key derivation (with the developer's
`jeff-hk@126.com` email inside) probably carries over from the old era;
verify against a real dump.

## 4. Known root avenues (public, DEF CON 32 / HITCON 2024)

Giese & Luedtke (dontvacuum.me), about Ecovacs robots in general:

- **UART**: a login shell accessible over the serial port (they have
  rooted T9/X1 this way on current firmware; modern rootfs images ship a
  patchable dropbear). For the 930: open the robot, locate the main SoC's
  UART (typically 115200 8N1), see whether there's a getty/login.
- **Unauthenticated local multicast interface**: commands like
  `FPsetNetPin…` (HITCON's example payload starts
  `0x60 'F' 'P' setNetPin\0…`). It's the local provisioning/linking
  protocol — several RCEs reported.
- **HTTP provisioning protocol** on the robot's AP:
  `POST http://192.168.0.1:8888/rcp.do` (documented in protocol.md, from
  ecovacs-privacy-control: `SetApConfig` for app-less WiFi). The full
  `rcp.do` catalog is unmapped — a candidate for exploration.
- Broken crypto / TLS without verification / honor-system ACLs — the
  context for why all of this works.

For the 930 specifically we find no public root write-up: we'd be first —
and with an edge: we already control the robot's XMPP server (QuickUpgrade
whenever we want) and hold a complete corpus of its dialect.

## 5. Dissection plan (once we have an image) — SEPARATE PROJECT

> Firmware dump/patching/flashing will be investigated in a dedicated
> project; this stays as the starting map.

1. **Get a 930 image**:
   a. Community: look for dumps (sucks/bumper GitHub issues, HA forums,
      archive.org of the OTA portal if anyone archived latest.json/bin).
   b. **Extract it from our robot** (the most reliable): via UART →
      shell → `dd` of the MTD partitions (`/proc/mtd`, `/dev/mtd*`) →
      mount the SquashFS offline.
   c. The QuickUpgrade-over-our-XMPP route would need our own EcoUpdate
      server + a valid image for ITS platform: brickable, only if a+b
      fail and with backup dumps.
2. **Analysis**: `binwalk -e` on each section; the SquashFS rootfs
   (`unsquashfs`) will yield the whole system: busybox, the robot binary
   (the one that speaks our XMPP), configs, keys.
3. **Patching** (once the 930 container is understood): modify the rootfs
   (e.g. add dropbear + a key), repack with the same format/encryption,
   serve it as our own EcoUpdate, and `QuickUpgrade`.
4. **Risks**: irreversible brick if the bootloader can't recover; take a
   full dump BEFORE any experimental flash; ideally practice on a cheap
   second-hand 930.

## 6. What this repo already has for all of this

- The monolith controls the robot's command channel (`td="fw"
  act="QuickUpgrade"` is just another commander command) and lookup.do
  (where EcoUpdate points — already configurable in `lookup.py`).
- `docs/protocol.md`: `rcp.do` provisioning (192.168.0.1:8888).
- 100% corpus of the 930's dialect (bonus: with the `<ver>` fix, the
  bridge now reports firmware 1.9.9 on the panel/HA).
