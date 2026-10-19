"""Bridge configuration (TOML)."""

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib
except ImportError:  # python<3.11 has no tomllib: tomli ships as a dependency
    import tomli as tomllib


@dataclass
class BumperConfig:
    host: str = "localhost"
    port: int = 5223
    uid: str = "fuid_tmpuser"  # same uid as the app → bot routing
    resource: str = "bridge"
    ca_file: str = "data/certs/ca.crt"


@dataclass
class MQTTConfig:
    host: str = "localhost"
    port: int = 1883
    user: str = ""
    password: str = ""
    base_topic: str = "deebot"


@dataclass
class BumperMQTTConfig:
    """Connection to Bumper's MQTT broker (eco-ng / T8+ robots).

    Currently points at the external Bumper container; once Bumper is
    absorbed into the bridge it will point at the embedded broker on
    127.0.0.1 (docs/bumper-absorption.md).
    """
    host: str = "localhost"
    port: int = 8883
    ca_file: str = "data/certs/ca.crt"
    enabled: bool = False
    # None = TLS iff port 8883 (legacy heuristic); serve pins it to True
    # when it starts the embedded broker (which may use another port)
    tls: bool | None = None
    # known robots (did@class/res) to adopt without waiting for their first report
    robots: list[str] = field(default_factory=list)


@dataclass
class ServersConfig:
    """Embedded servers (Bumper absorption, docs/bumper-absorption.md).

    Each component is enabled separately — the bridge starts ONLY what
    it needs. While a server is off, its bridge client keeps pointing
    at the external host ([bumper]/[bumper_mqtt]).
    """
    # :8007 — robot bootstrap (POST lookup.do → XMPP IP). Tiny but
    # essential: the firmware requests it on every boot.
    lookup: bool = False
    # :5223 — XMPP server (930-era robots) — FA/FB
    xmpp: bool = False
    # :8883 — eco-ng MQTT broker — FC
    mqtt: bool = False
    # :443 — Ecovacs app API (login/devices) — FD, OFF by default
    conf: bool = False
    # bridge web panel (native, not absorbed)
    panel: bool = True

    listen: str = "0.0.0.0"
    lookup_port: int = 8007
    xmpp_port: int = 5223
    mqtt_port: int = 8883
    conf_port: int = 443
    # IP announced to robots/app (like Bumper's BUMPER_ANNOUNCE_IP)
    announce_ip: str = ""
    # TLS material for the servers that use it (xmpp/mqtt/conf)
    ca_file: str = "data/certs/ca.crt"
    server_cert: str = "data/certs/server.crt"
    server_key: str = "data/certs/server.key"


@dataclass
class Config:
    bumper: BumperConfig = field(default_factory=BumperConfig)
    mqtt: MQTTConfig = field(default_factory=MQTTConfig)
    bumper_mqtt: BumperMQTTConfig = field(default_factory=BumperMQTTConfig)
    servers: ServersConfig = field(default_factory=ServersConfig)
    # empty = auto-discover any bot that sends frames
    robots: list[str] = field(default_factory=list)
    panel_port: int = 8181
    panel_host: str = "0.0.0.0"
    # map pieces + positions persistence (survives restarts without the
    # robot); empty = no persistence
    state_file: str = ""


def load_config(path: str | None) -> Config:
    cfg = Config()
    if not path or tomllib is None or not Path(path).exists():
        _apply_env(cfg)  # env fallback applies even without a toml
        return cfg
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    for k, v in raw.get("bumper", {}).items():
        setattr(cfg.bumper, k, v)
    for k, v in raw.get("mqtt", {}).items():
        setattr(cfg.mqtt, k, v)
    for k, v in raw.get("bumper_mqtt", {}).items():
        setattr(cfg.bumper_mqtt, k, v)
    for k, v in raw.get("servers", {}).items():
        setattr(cfg.servers, k, v)
    for k in ("robots", "panel_port", "panel_host", "state_file"):
        if k in raw:
            setattr(cfg, k, raw[k])
    _apply_env(cfg)
    return cfg


def _apply_env(cfg: Config) -> None:
    """Environment variable fallbacks.

    TOML cannot read the environment, and some values are only known
    when the container starts (docker-compose):

    - ``DEEBOT_ANNOUNCE_IP`` → ``servers.announce_ip`` when empty
      (Bumper's BUMPER_ANNOUNCE_IP equivalent; compose injects
      ``DEEBOT_ANNOUNCE_IP: ${BUMPER_ANNOUNCE_IP}`` from ``.env``)
    """
    if not cfg.servers.announce_ip:
        env_ip = os.environ.get("DEEBOT_ANNOUNCE_IP", "").strip()
        if env_ip:
            cfg.servers.announce_ip = env_ip
