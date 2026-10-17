"""Embedded lookup server — robot bootstrap (Phase FA).

On boot, each robot's firmware asks over plain HTTP where the message
server is::

    POST http://lbo.ecouser.net:8007/lookup.do
    {"todo": "FindBest", "service": "EcoMsgNew"}        (UA: Wget/1.19)

Response (the robot is picky about spaces — Bumper learned this the hard
way; we reproduce its compact-json trick)::

    {"ip":"<announce_ip>","port":5223,"result":"ok"}

It also answers ``EcoUpdate`` (OTA) pointing at Ecovacs' real server,
like Bumper does — the firmware checks it and we do not want to handle
OTA.

Default port :8007 (configurable, ``servers.lookup_port``). It is mounted
on :443 only when ``servers.conf`` is active (FD) — for now 443 stays
free.
"""

from __future__ import annotations

import json
import logging

from aiohttp import web

log = logging.getLogger(__name__)

# Ecovacs' real OTA server (as Bumper answers)
ECOUPDATE_IP = "47.88.66.164"
ECOUPDATE_PORT = 8005


def lookup_response(service: str, announce_ip: str, xmpp_port: int) -> web.Response:
    """Faithful reply to bumper/confserver.handle_lookup (both formats)."""
    if service == "EcoMsgNew":
        # no spaces: the robot's parser is strict
        body = json.dumps({"ip": announce_ip, "port": xmpp_port, "result": "ok"},
                          separators=(",", ":"))
        return web.Response(text=body, content_type="application/json")
    if service == "EcoUpdate":
        return web.json_response({"result": "ok", "ip": ECOUPDATE_IP,
                                  "port": ECOUPDATE_PORT})
    return web.json_response({})


async def parse_lookup_request(request: web.Request) -> dict:
    """Body of lookup.do: JSON (robot's wget) or form-urlencoded (app).

    Shared by the :8007 lookup (FA) and the :443 conf (FD), which mounts
    the same endpoint — like Bumper's confserver.
    """
    try:
        if request.content_type == "application/x-www-form-urlencoded":
            return {k: v for k, v in (await request.post()).items()}
        return await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


class LookupServer:
    """POST /lookup.do over plain HTTP — all the robot needs."""

    def __init__(self, announce_ip: str, xmpp_port: int = 5223,
                 listen: str = "0.0.0.0", port: int = 8007):
        if not announce_ip:
            raise ValueError("servers.announce_ip es obligatorio para el lookup embebido")
        self.announce_ip = announce_ip
        self.xmpp_port = xmpp_port
        self.listen = listen
        self.port = port
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_post("/lookup.do", self.handle_lookup)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.listen, self.port)
        await site.start()
        log.info("embedded lookup at http://%s:%d (announce %s:%d)",
                 self.listen, self.port, self.announce_ip, self.xmpp_port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def handle_lookup(self, request: web.Request) -> web.Response:
        body = await parse_lookup_request(request)
        service = str(body.get("service", ""))
        log.info("lookup.do todo=%s service=%s from %s",
                 body.get("todo"), service, request.remote)
        return lookup_response(service, self.announce_ip, self.xmpp_port)
