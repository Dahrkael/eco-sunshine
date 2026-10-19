"""Device registry — replaces bumper.db (bots/clients).

The surface mirrors what Bumper's xmppserver.py consumed
(bot_add/bot_get/client_add/check_authcode/use_auth…), but the model is
ours: each bot is a DeviceId with XMPP presence. Optional JSON
persistence next to state_file — no external DB.

The embedded MQTT broker plugin (FC) also registers the eco-ng bots that
connect here: a single registry for both protocols (the DeviceId seam
prepared during the MQTT phase). The embedded conf (FD) lists these same
bots to the app (user.do GetDeviceList) and applies their renames
(SetDeviceNick/DeleteOneDevice).
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class RegBot:
    did: str
    sn: str
    device_class: str
    resource: str
    company: str
    # nick set by the app (user.do SetDeviceNick); empty = not renamed
    nick: str = ""
    # per-protocol presence (not persisted: recovered on reconnect)
    xmpp: bool = False
    mqtt: bool = False


@dataclass
class RegClient:
    user_id: str
    realm: str
    resource: str
    xmpp: bool = False


@dataclass
class DeviceRegistry:
    """Known bots (robots) + clients (apps/bridge), with presence.

    use_auth=False mirrors Bumper's default behaviour: any client is
    accepted (there are no accounts to validate on a LAN).
    """

    use_auth: bool = False
    persist_path: str | None = None
    bots: dict[str, RegBot] = field(default_factory=dict)   # did → bot
    clients: dict[str, RegClient] = field(default_factory=dict)  # resource → client

    # ---------- bots ----------

    def bot_add(self, sn: str, did: str, devclass: str, resource: str,
                company: str) -> None:
        bot = RegBot(did=did or sn, sn=sn, device_class=devclass,
                     resource=resource, company=company)
        existed = self.bots.get(bot.did)
        self.bots[bot.did] = bot
        if existed is None:
            log.info("bot registered: %s (did=%s class=%s %s)",
                     sn, bot.did, devclass, company)
        # persist only if something changed (reconnects do not rewrite)
        if existed is None or asdict(existed) != asdict(bot):
            self._save()

    def bot_get(self, did: str) -> dict | None:
        bot = self.bots.get(did)
        return asdict(bot) if bot else None

    def bot_set_xmpp(self, did: str, on: bool) -> None:
        bot = self.bots.get(did)
        if bot:
            bot.xmpp = on
            self._save()

    def bot_set_mqtt(self, did: str, on: bool) -> None:
        bot = self.bots.get(did)
        if bot:
            bot.mqtt = on
            self._save()

    def bot_set_nick(self, did: str, nick: str) -> None:
        """Renames a bot (the app asks via user.do SetDeviceNick)."""
        bot = self.bots.get(did)
        if bot:
            bot.nick = nick
            self._save()

    def bot_remove(self, did: str) -> None:
        """Forgets a bot (user.do DeleteOneDevice): it reappears if it reconnects."""
        if self.bots.pop(did, None) is not None:
            log.info("bot removed from registry: %s", did)
            self._save()

    # ---------- clients ----------

    def client_add(self, user_id: str, realm: str, resource: str) -> None:
        self.clients[resource] = RegClient(user_id=user_id, realm=realm,
                                           resource=resource)
        self._save()

    def client_get(self, resource: str) -> dict | None:
        cl = self.clients.get(resource)
        return asdict(cl) if cl else None

    def client_set_xmpp(self, resource: str, on: bool) -> None:
        cl = self.clients.get(resource)
        if cl:
            cl.xmpp = on
            self._save()

    # ---------- auth ----------

    def check_authcode(self, uid: str, authcode: str) -> bool:
        # no local accounts: always falls to the use_auth=False path
        # (identical to Bumper without auth enabled)
        return False

    # ---------- persistence ----------

    def _save(self) -> None:
        if not self.persist_path:
            return
        try:
            path = Path(self.persist_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            doc = {
                "bots": {d: asdict(b) for d, b in self.bots.items()},
                "clients": {r: asdict(c) for r, c in self.clients.items()},
            }
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(doc))
            tmp.replace(path)
        except Exception:  # noqa: BLE001
            log.exception("registry persistence failed")

    def load(self) -> None:
        if not self.persist_path or not Path(self.persist_path).exists():
            return
        try:
            doc = json.loads(Path(self.persist_path).read_text())
            for d, b in (doc.get("bots") or {}).items():
                b.pop("xmpp", None)  # presence is not persisted
                b.pop("mqtt", None)
                self.bots[d] = RegBot(**b)
            for r, c in (doc.get("clients") or {}).items():
                c.pop("xmpp", None)
                self.clients[r] = RegClient(**c)
            log.info("registry restored: %d bots, %d clients (from %s)",
                     len(self.bots), len(self.clients), self.persist_path)
        except (json.JSONDecodeError, OSError, TypeError):
            log.exception("persisted registry unreadable — ignored")
