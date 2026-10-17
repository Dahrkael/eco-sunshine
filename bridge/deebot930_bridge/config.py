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
class Config:
    bumper: BumperConfig = field(default_factory=BumperConfig)
    mqtt: MQTTConfig = field(default_factory=MQTTConfig)
    bumper_mqtt: BumperMQTTConfig = field(default_factory=BumperMQTTConfig)
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
    for k in ("robots", "panel_port", "panel_host", "state_file"):
        if k in raw:
            setattr(cfg, k, raw[k])
    return cfg
