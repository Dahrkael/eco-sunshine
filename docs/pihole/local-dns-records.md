# Pi-hole — Local DNS Records (sin wildcard)

Alternativa a `99-deebot-bumper.conf` (dnsmasq) para quien prefiera el GUI:
Local DNS → DNS Records → añadir cada dominio apuntando a **192.0.2.10**
(en producción: IP del minipc).

Instanciado para España: `{countrycode}` = es, `{region}` = eu, más ww.

## CRÍTICOS (robot 930 XMPP + app antigua "Ecovacs")

| Dominio | Para qué |
|---|---|
| `lbo.ecovacs.net` | **Lookup de arranque del robot** (HTTP :8007 → le dice dónde está el XMPP) |
| `msg-es.ecouser.net` | Servidor XMPP (robot 930) |
| `msg-eu.ecouser.net` | Ídem, variante región |
| `msg-ww.ecouser.net` | Ídem, worldwide |
| `portal.ecouser.net` | Login + API REST |
| `portal-es.ecouser.net` | Ídem |
| `portal-eu.ecouser.net` | Ídem |
| `portal-ww.ecouser.net` | Ídem |
| `eco-es-api.ecovacs.com` | Login app antigua |
| `eco-eu-api.ecovacs.com` | Ídem |

## Load balancers

| Dominio |
|---|
| `lb-es.ecovacs.net` |
| `lb-eu.ecovacs.net` |
| `lb-ww.ecovacs.net` |
| `lb-es.ecouser.net` |
| `lb-eu.ecouser.net` |
| `lb-ww.ecouser.net` |
| `lbus.ecouser.net` |
| `lbes.ecouser.net` |
| `lbeu.ecouser.net` |
| `lbww.ecouser.net` |

## App "Ecovacs Home" (contingencia)

| Dominio |
|---|
| `gl-es-api.ecovacs.com` |
| `gl-eu-api.ecovacs.com` |
| `gl-es-openapi.ecovacs.com` |
| `gl-eu-openapi.ecovacs.com` |

## MQTT (contingencia si el firmware hablase MQTT)

| Dominio |
|---|
| `mq-es.ecouser.net` |
| `mq-eu.ecouser.net` |
| `mq-ww.ecouser.net` |
| `mq-es.ecovacs.com` |
| `mq-eu.ecovacs.com` |

## Telemetría (que ni intente salir)

| Dominio |
|---|
| `bigdata-europe.ecovacs.com` |
| `bigdata-international.ecovacs.com` |
| `recommender.ecovacs.com` |

## App v2+ (poco probable con la app antigua; baratos de añadir)

| Dominio |
|---|
| `api-app.ww.ecouser.net` |
| `api-app.dc-eu.ww.ecouser.net` |
| `users-base.dc-eu.ww.ecouser.net` |
| `jmq-ngiot-eu.dc.ww.ecouser.net` |
| `api-rop.dc-eu.ww.ecouser.net` |
| `jmq-ngiot-eu.area.ww.ecouser.net` |

## Red de seguridad anti-fuga (recomendado)

Los Local DNS Records son exactos: cualquier subdominio NO listado
resolvería a la nube real. Para tapar eso, añadir en
Local DNS → DNS Records NO; en **Group Management → Domains / Blocklist**
(domains: add regex):

```
(^|\.)ecovacs\.(com|net)$
(^|\.)ecouser\.net$
```

Los Local DNS Records (dnsmasq hosts) tienen prioridad sobre las
blocklists, así que los dominios sobreescritos siguen funcionando; el
resto del ecosistema Ecovacs queda bloqueado en vez de escapar.

## Detección de fugas durante el emparejamiento

Query Log de Pi-hole filtrando `ecovacs|ecouser`: si aparece algún
dominio del ecosistema **con IP pública** (o bloqueado por la regex que
no esté ya en Local DNS), añadirlo a la lista y reintentar.
