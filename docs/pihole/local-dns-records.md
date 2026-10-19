# Pi-hole — Local DNS Records (no wildcard)

Alternative to `99-deebot-bumper.conf` (dnsmasq) for those who prefer the
GUI: Local DNS → DNS Records → add each domain pointing to
**192.0.2.10** (replace with your host's IP).

The list is instantiated for one country/region (`es`/`eu`, plus the
worldwide `ww` variants) — keep the rows matching yours.

## CRITICAL (930 XMPP robot + legacy "Ecovacs" app)

| Domain | Purpose |
|---|---|
| `lbo.ecovacs.net` | **Robot boot lookup** (HTTP :8007 → tells it where the XMPP server is) |
| `msg-es.ecouser.net` | XMPP server (930 robot) |
| `msg-eu.ecouser.net` | Ditto, region variant |
| `msg-ww.ecouser.net` | Ditto, worldwide |
| `portal.ecouser.net` | Login + REST API |
| `portal-es.ecouser.net` | Ditto |
| `portal-eu.ecouser.net` | Ditto |
| `portal-ww.ecouser.net` | Ditto |
| `eco-es-api.ecovacs.com` | Legacy app login |
| `eco-eu-api.ecovacs.com` | Ditto |

## Load balancers

| Domain |
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

## "Ecovacs Home" app (contingency)

| Domain |
|---|
| `gl-es-api.ecovacs.com` |
| `gl-eu-api.ecovacs.com` |
| `gl-es-openapi.ecovacs.com` |
| `gl-eu-openapi.ecovacs.com` |

## MQTT (contingency if the firmware spoke MQTT)

| Domain |
|---|
| `mq-es.ecouser.net` |
| `mq-eu.ecouser.net` |
| `mq-ww.ecouser.net` |
| `mq-es.ecovacs.com` |
| `mq-eu.ecovacs.com` |

## Telemetry (so it doesn't even try to get out)

| Domain |
|---|
| `bigdata-europe.ecovacs.com` |
| `bigdata-international.ecovacs.com` |
| `recommender.ecovacs.com` |

## v2+ app (unlikely with the legacy app; cheap to add)

| Domain |
|---|
| `api-app.ww.ecouser.net` |
| `api-app.dc-eu.ww.ecouser.net` |
| `users-base.dc-eu.ww.ecouser.net` |
| `jmq-ngiot-eu.dc.ww.ecouser.net` |
| `api-rop.dc-eu.ww.ecouser.net` |
| `jmq-ngiot-eu.area.ww.ecouser.net` |

## Anti-leak safety net (recommended)

Local DNS Records are exact: any subdomain NOT listed would resolve to
the real cloud. To close that gap, add — not under
Local DNS → DNS Records, but under **Group Management → Domains /
Blocklist** (domains: add regex):

```
(^|\.)ecovacs\.(com|net)$
(^|\.)ecouser\.net$
```

Local DNS Records (dnsmasq hosts) take priority over blocklists, so the
overridden domains keep working; the rest of the Ecovacs ecosystem gets
blocked instead of leaking out.

## Leak detection during pairing

Pi-hole's Query Log filtered by `ecovacs|ecouser`: if any domain of the
ecosystem shows up **with a public IP** (or is blocked by the regex while
not already being in Local DNS), add it to the list and retry.
