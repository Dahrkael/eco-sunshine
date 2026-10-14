# ECOVACS HOME (v3.16.1) — Robot Model Registry (MQTT Era)

> Extracted from decompiled APK by subagent (ses_ec38adc6cffeNtzTCF7berdLcQ)

## 1. Architecture

| Vendor | Transport | Library |
|---|---|---|
| `eco-legacy` | XMPP | `IOTXmppDevice` (nuestro 930) |
| `eco-ng` | **MQTT** (NG-IoT) | `IOTMqDevice` |
| `ali-living` | Alibaba LinkLiving | `IOTAliDevice` |
| `winbot-bt` | Bluetooth | BLE presenters |

## 2. appLogicId Registry (MQTT-era models)

### T8 family
- `t8_cn_n_nobel`, `t8_ww_n_nobel`, `t8_ww_n_nobelplus` (+costco/black variants)
- `DT_8`, `DT_8A` (no water), `DT_8G`, `DT_8SE`, `DT_8SE_G`, `N8_PLUS`
- Water yield (mopping): all except DT_8A
- Auto-empty v2: only t8_ww_n_nobel* variants

### T9 family
- `T9_PRO`, `T9_PRO_A`, `T9_PRO_G`
- `T9_AF`, `T9_AF_G` (AIVI camera + video patrol)

### T10 / X1 family (all H5-driven)
- OMNI: `T10_OMNI`, `t10_*_omni*` (hot-water mop wash + dry + auto-empty)
- Curie/Turing/Darwin/Newton: `t10_*_curie*`, `t10_*_turing*`, `t10_*_darwin*`, `t10_*_newton*`
- ACS (auto-clean station): `t10_*_*acs`
- AES (auto-empty station): `t10_*_*aes`
- X1: `x1pro_cn_n_omni`, `x1pro_cn_n_omni_plus`, `x1proplus_*_x1proh5`

### Other vacuum families
- U2: `U2_HIGH/MEDIUM/LOW` (+ `_G` variants)
- DV3 SE: `DV_33_P`, `DV_33_PS`, `DV_33_P_G`
- DK850: `DK_850`, `DK_850_CN_ALI`
- Bruce: `bruce_cn_n_bruce`, `bruce_ww_n_bruce` (temp/humidity sensor)

### Non-vacuum
- GOAT mowers: G1 + GOAT2 family (`goatr/goatl_ww_h_goat2*`, `goatbz*`)
- AIRBOT: `ATMOBOT_ANDY/AVA/AVA_IN`, `ANDY_PRO`, air detectors `AT80/AT_01/AT_01G/AT90`
- WINBOT: `winbot`, `winbot_g` (BLE, `product_category=WINBOT`)

### NOT in this version (too old APK)
No T20, T30, N20, U10, X2/X2PRO — only forward-compat camera hooks (`x2`, `z2`, `kepler`, `faraday`).

## 3. MQTT Class IDs (mid)

Server-driven; hardcoded exceptions:

| classid | Model |
|---|---|
| `ewfvki`, `6cibhb`, `0v1rox` | GOAT camera mowers |
| `6n9pcz` | goatbzse |
| `qhq6i0` | goatbz |
| `gnqx18`, `7cswgv` | DV3 SE (no carpet-pressure) |
| `ylgz95`, `sdp1y1` | Bruce (has temp/humidity) |
| `qqy0di` | AA30 (AIRBOT) |
| `m7lqzi`, `2q15j8` | AVA China |
| `89kr00`, `4f0c4e`, `q1v5dn`, `kckkso` | Air detectors |

## 4. MQTT Command Vocabulary (complete, from Names.java)

getBattery, clean, charge, move, playSound, getSched/onSched/setSched,
getSpeed/setSpeed, getWaterInfo/setWaterInfo, getCleanInfo, getError,
getStats/getTotalStats, getLifeSpan/resetLifeSpan,
getMajorMap/getMinorMap/getMapTrace/getPos_V2,
getMapSet/setMapSet, getMapSubSet/setMapSubSet, getMapState,
getMapInfo/onMapInfo, getCachedMapInfo/setCachedMapInfo,
getMultiMapState/setMultiMapState, getBlock/setBlock,
getBreakPoint/setBreakPoint, getAdvancedMode/setAdvancedMode,
getCarpertPressure/setCarpertPressure, getDModule/setDModule,
getNetInfo, ota/getOta/setOta, getVoice/setVoice,
getVolume/setVolume, avoidObject/onAvoidObject,
recognize/getRecognization/setRecognization (AIVI),
getLiveState/setLiveState/livePing (video),
getRelocationState/setRelocationState, getChargeState,
onEvt, getConfig, clearMap, getAIMap

## 5. Capability flags (server-driven via functionConfig)

- `auto_empty`, `slop_tank` (mop washing)
- `mop_mode`, `mop_drying`
- `supportMultiMap`, `supportSmartVW`, `supportAdvance`
- `water_yield`, `vwall_mop_forbid`
- `yiko_type` (YIKO voice)
- `supportBorderSpin`, `support_audio`
- `acs_box` (auto-clean station)
