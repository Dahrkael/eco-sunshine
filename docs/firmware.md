# Firmware Ecovacs — investigación completa (OZMO 930 y familia)

> Worktree `research/firmware` (2026-10-17). Fuentes: corpus propio (log
> Bumper 681 MB + pcaps), decompile de ambas apps (Ecovacs 1.4.9 y Home
> 3.16.1), fuente de `ecovacs-firmware-tools` (denysvitali, 2025), charlas
> DEF CON 32 / HITCON CMT 2024 (Giese & Luedtke), probes en vivo contra la
> API OTA oficial.

## 1. Qué firmware lleva nuestro robot

**OZMO 930 (class `115`): firmware `1.9.9`** (módulo `fw0`), leído del
corpus (2026-10-13):

```xml
<!-- app → robot (XMPP) -->
<ctl id="34196105" name="FW" td="GetVersion"/>
<!-- robot → app (¡multi-línea en el wire!) -->
<ctl id="34196105" ret="ok">
  <ver name="FW">1.9.9</ver>
</ctl>
```

- La versión viaja en un **elemento hijo** `<ver name="FW">TEXTO</ver>`,
  no en un atributo (nuestro parser estaba equivocado → `fw: None`; fix
  en este branch con tests).
- El módulo `FW` es el principal (`fw0` en la API OTA). La app antigua
  también pedía versiones de otros módulos cambiando `name=`.
- `DeviceInfo` (push al conectar) NO lleva versión: solo MID/class/chn/wd.

## 2. Cómo flashea la app antigua (era XMPP) — verificado en decompile

Flujo completo de `FirmwareVersionActivity` + `DR930.java` +
`EcoRobotProtocol.java` (app Ecovacs 1.4.9):

1. **Versión actual**: `GetVersion name=FW` por XMPP (arriba).
2. **Comprobar novedades** (HTTP, no XMPP):
   ```
   GET https://{portal}/api/ota/products/wukong/class/{cls}/firmware/latest.json
       ?did={did}&sn={sn}&ver={actual}&module=fw0&ts={ms}&lang={lang}
   → {"version":"x.y.z","fw0":{"changeLog":"<b64>","url":"...","size":N,
       "checkSum":"<md5|sha256>"},"force":false}
   ```
   - `{portal}` = `portal-ww.ecouser.net` (WW) / `portal.ecouser.net` (CN)
     — resuelto vía `DataParseUtil.getDlUrl()` del país de la cuenta.
3. **Requisitos** (la app los exige antes de mandar nada): batería ≥ 40%
   Y cargando en base.
4. **Disparar la actualización** (XMPP):
   ```xml
   <ctl id="..." td="fw" act="QuickUpgrade"/>
   ```
   **EL ROBOT SE DESCARGA EL FIRMWARE ÉL SOLO.** La app no le pasa ni URL
   ni binario: el robot pide el servidor OTA al arranque con
   `lookup.do {"todo":"FindBest","service":"EcoUpdate"}` y descarga de ahí.
5. **Progreso**:
   - Push del robot: frames `td="fw"` con attrs `status`/`upgrade_stattus`
     (sic) / `progress`.
   - Sondeo: `<ctl td="fw" act="GetStatus"/>`; y `GetProgress` con
     `act={download|upgrade}` para los pases de voz.
6. UA HTTP de la app: `Dalvik/2.1.0 (Linux; U; Android 5.1.1; …)`.

### Estado HOY de la infra OTA oficial (medido 2026-10-17)

| Endpoint | Estado |
|---|---|
| `portal-ww.ecouser.net` / `portal-eu.ecouser.net` (`slb-oldweb-iot-*`) | DNS vivo (CNAME → 8.211.16.120) pero `/api/ota/...` → **"Not Found"** para todos los class probados (115, 659yh8/T9AIVI, yna5xh, vsc5ia) |
| `EcoUpdate` `47.88.66.164:8005` (a donde apunta el lookup.do) | **timeout** — muerto |

Conclusión: **la OTA oficial de la era XMPP está retirada**. Un robot en
firmware antiguo ya no puede actualizarse solo por la nube de Ecovacs.
(`ecovacs-firmware-tools` documentaba descargas funcionando en 2025 —
lo han apagado entre medias; quedan firmwares modernos en la API nueva
que usa la app Home, fuera de alcance de los XMPP.)

⚠️ Ojo: nuestro **Pi-hole wildcard** redirige `*.ecouser.net` al bridge —
para probar endpoints reales hay que resolver con `8.8.8.8` y usar
`curl --resolve`.

## 3. Formato del firmware (era T9+, `ecovacs-firmware-tools`)

Contenedor de secciones cifradas (firmware `fw0` de T9AF_px30 v1.4.9 como
ejemplo del tool):

```
[8 B cabecera de sección][datos AES-128-CBC][64 B checksum sha256hex ASCII]
 cabecera: u8 unkn1(=1), u8 type(=1), u16 unkn2 (type real = unkn2>>12),
           u32 size (LE)
 secciones: manifest (JSON), pre_upgrade_script (sh), normal_boot (kernel),
            normal_fs (SquashFS), mcu, post_upgrade_script (sh)
 manifest: {"fw_ver","hw_ver","product","release_date",
            "sections":[{name,type,size,skip,flash_cmd}]}
```

**Cifrado por sección** (AES-128-CBC, derivación determinista — sin secretos
por dispositivo, todo está en el formato):

```go
snprintf("ZWNvX2Z3X3RhcmdldCAECO-PT1jdSAtpx30byBtYW4%dy5iaW4%s825xxjeff-hk@126.com",
         typeDigit, sizeHex)
b64 := base64(...)          // estándar
seed := b64[4:][:len(snprintf_result)]   // trunca al largo original
h := sha256hex(seed)
iv  := h[0:16]  (ASCII)     key := h[35:51] (ASCII, 16 bytes)
```

- El checksum es SHA256 hex de `cabecera+datos` (64 chars ASCII).
- Padding PKCS7 al desencriptar.

**AVISO**: esto es el formato de la era T9+/N8 (2020+). El 930 (2018,
plataforma anterior) puede usar otro contenedor — se sabrá cuando haya un
dump del 930 en la mano. La derivación de clave (con el email
`jeff-hk@126.com` del desarrollador dentro) probablemente se mantiene de
la era antigua; verificar contra un dump real.

## 4. Vías de root conocidas (público, DEF CON 32 / HITCON 2024)

Giese & Luedtke (dontvacuum.me), sobre robots Ecovacs en general:

- **UART**: shell de login accesible por el puerto serie (lo han rooteado
  así en T9/X1 con firmware actual; en los rootfs modernos hay dropbear
  parcheable). Para el 930: abrir el robot, localizar el UART del SoC
  principal (típico 115200 8N1), ver si hay getty/login.
- **Interfaz multicast local sin auth**: comandos tipo `FPsetNetPin…`
  (el payload de ejemplo de HITCON empieza `0x60 'F' 'P' setNetPin\0…`).
  Es el protocolo de provisioning/enlace local — varios RCEs reportados.
- **Protocolo de provisioning HTTP** en el AP del robot:
  `POST http://192.168.0.1:8888/rcp.do` (documentado en protocol.md desde
  ecovacs-privacy-control: `SetApConfig` para el WiFi sin app). El catálogo
  completo de `rcp.do` está sin mapear — candidato a exploración.
- Broken crypto / TLS sin verificación / ACL por honor — el contexto de
  por qué todo esto funciona.

Para el 930 en concreto no hay writeup público de root que encontremos:
seríamos primeros — y con ventaja: ya controlamos el servidor XMPP del
robot (QuickUpgrade cuando queramos) y tenemos corpus completo de su
dialecta.

## 5. Plan de dissección (cuando haya imagen)

1. **Conseguir una imagen del 930**:
   a. Comunidad: buscar dumps (GitHub issues de sucks/bumper, foros HA,
      archive.org del portal OTA si alguien archivó latest.json/bin).
   b. **Extraerla de nuestro robot** (la más fiable): vía UART → shell →
      `dd` de las particiones MTD (`/proc/mtd`, `/dev/mtd*`) → montar el
      SquashFS offline.
   c. La ruta QuickUpgrade-sobre-nuestro-XMPP requeriría un servidor
      EcoUpdate propio + imagen válida para SU plataforma: brickeable,
      solo si a+b fallan y con dumps de respaldo.
2. **Análisis**: `binwalk -e` sobre cada sección; el rootfs SquashFS
   (`unsquashfs`) dará el sistema completo: busybox, el binario del robot
   (el que habla nuestro XMPP), configs, claves.
3. **Parcheo** (una vez entendido el contenedor del 930): modificar el
   rootfs (p.ej. añadir dropbear + clave), re-empaquetar con el mismo
   formato/cifrado, servirlo como EcoUpdate propio, y `QuickUpgrade`.
4. **Riesgos**: brick irreversible si el bootloader no recupera; hacer
   dump completo ANTES de cualquier flasheo experimental; idealmente
   practicar con un 930 de segunda mano barato.

## 6. Qué hay ya en este repo para todo esto

- El monolito controla el canal de comandos del robot (`td="fw"
  act="QuickUpgrade"` es un comando más del commander) y el lookup.do
  (a dónde apunta EcoUpdate — configurable ya en `lookup.py`).
- `docs/protocol.md`: provisioning `rcp.do` (192.168.0.1:8888).
- Corpus 100% de la dialecta del 930 (bonus: con el fix de `<ver>`, el
  bridge ahora reporta el firmware 1.9.9 en panel/HA).
