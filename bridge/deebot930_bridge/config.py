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
class Config:
    bumper: BumperConfig = field(default_factory=BumperConfig)
    mqtt: MQTTConfig = field(default_factory=MQTTConfig)
    # vacío = auto-descubrir cualquier bot que envíe frames
    robots: list[str] = field(default_factory=list)


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
    cfg.robots = raw.get("robots", [])
    return cfg
