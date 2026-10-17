"""Configuración del bridge (TOML)."""

from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib
except ImportError:  # py<3.11
    tomllib = None


@dataclass
class BumperConfig:
    host: str = "localhost"
    port: int = 5223
    uid: str = "fuid_tmpuser"  # mismo uid que la app → routing del bot
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
    """Conexión al broker MQTT de Bumper (robots eco-ng / T8+).

    Hoy apunta al contenedor Bumper; cuando Bumper se absorba en el bridge,
    apuntará a 127.0.0.1 del broker embebido (docs/absorcion-bumper.md).
    """
    host: str = "localhost"
    port: int = 8883
    ca_file: str = "data/certs/ca.crt"
    enabled: bool = False
    # robots conocidos (did@class/res) para adoptar sin esperar su primer report
    robots: list[str] = field(default_factory=list)


@dataclass
class ServersConfig:
    """Servidores embebidos (absorción de Bumper, docs/absorcion-bumper.md).

    Cada componente se enciende por separado — el bridge arranca SOLO lo
    que necesita. Mientras un servidor esté off, su cliente del bridge
    sigue apuntando al host externo ([bumper]/[bumper_mqtt]).
    """
    # :8007 — bootstrap del robot (POST lookup.do → IP del XMPP). Diminuto
    # e imprescindible: el firmware lo pide en cada arranque.
    lookup: bool = False
    # :5223 — servidor XMPP (robots era 930) — FA/FB
    xmpp: bool = False
    # :8883 — broker MQTT eco-ng — FC
    mqtt: bool = False
    # :443 — API de la app Ecovacs (login/dispositivos) — FD, OFF por defecto
    conf: bool = False
    # panel web del bridge (nativo, no absorbido)
    panel: bool = True

    listen: str = "0.0.0.0"
    lookup_port: int = 8007
    xmpp_port: int = 5223
    mqtt_port: int = 8883
    conf_port: int = 443
    # IP que se anuncia a robots/app (como BUMPER_ANNOUNCE_IP de Bumper)
    announce_ip: str = ""
    # TLS de los servidores que lo usan (xmpp/mqtt/conf)
    ca_file: str = "data/certs/ca.crt"
    server_cert: str = "data/certs/server.crt"
    server_key: str = "data/certs/server.key"


@dataclass
class Config:
    bumper: BumperConfig = field(default_factory=BumperConfig)
    mqtt: MQTTConfig = field(default_factory=MQTTConfig)
    bumper_mqtt: BumperMQTTConfig = field(default_factory=BumperMQTTConfig)
    servers: ServersConfig = field(default_factory=ServersConfig)
    # vacío = auto-descubrir cualquier bot que envíe frames
    robots: list[str] = field(default_factory=list)
    panel_port: int = 8181
    panel_host: str = "0.0.0.0"
    # persistencia de piezas de mapa + posiciones (sobrevive reinicios sin
    # robot); vacío = sin persistencia
    state_file: str = ""


def load_config(path: str | None) -> Config:
    cfg = Config()
    if not path or tomllib is None or not Path(path).exists():
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
    return cfg
