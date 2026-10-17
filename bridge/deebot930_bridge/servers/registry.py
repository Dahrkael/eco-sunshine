"""Registro de dispositivos — sustituye a bumper.db (bots/clients).

La superficie replica la que xmppserver.py de Bumper consumía
(bot_add/bot_get/client_add/check_authcode/use_auth…), pero el modelo es
nuestro: cada bot es un DeviceId con presencia XMPP. Persistencia JSON
opcional junto al state_file — sin DB externa.

El plugin del broker MQTT embebido (FC) también registra aquí los bots
eco-ng que conectan: un único registro para ambos protocolos (la costura
DeviceId preparada en la fase de MQTT).
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
    xmpp: bool = False


@dataclass
class RegClient:
    user_id: str
    realm: str
    resource: str
    xmpp: bool = False


@dataclass
class DeviceRegistry:
    """Bots (robots) + clients (apps/bridge) conocidos, con presencia.

    use_auth=False replica el comportamiento por defecto de Bumper: se
    acepta cualquier cliente (no hay cuentas que validar en LAN).
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
            log.info("bot registrado: %s (did=%s clase=%s %s)",
                     sn, bot.did, devclass, company)
        self._save()

    def bot_get(self, did: str) -> dict | None:
        bot = self.bots.get(did)
        return asdict(bot) if bot else None

    def bot_set_xmpp(self, did: str, on: bool) -> None:
        bot = self.bots.get(did)
        if bot:
            bot.xmpp = on
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
        # sin cuentas locales: siempre cae al camino use_auth=False
        # (idéntico a Bumper sin auth habilitada)
        return False

    # ---------- persistencia ----------

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
            log.exception("persistencia del registro falló")

    def load(self) -> None:
        if not self.persist_path or not Path(self.persist_path).exists():
            return
        try:
            doc = json.loads(Path(self.persist_path).read_text())
            for d, b in (doc.get("bots") or {}).items():
                b.pop("xmpp", None)  # presencia no persiste
                self.bots[d] = RegBot(**b)
            for r, c in (doc.get("clients") or {}).items():
                c.pop("xmpp", None)
                self.clients[r] = RegClient(**c)
            log.info("registro restaurado: %d bots, %d clients (de %s)",
                     len(self.bots), len(self.clients), self.persist_path)
        except (json.JSONDecodeError, OSError, TypeError):
            log.exception("registro persistido ilegible — ignorado")
