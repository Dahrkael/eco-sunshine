"""Registro de modelos soportados por el bridge.

Detección: el JID del robot lleva la clase (SERIAL@CLASE.ecorobot.net).
La clase identifica el modelo. Las capacidades se ajustan por familia.

Familias soportadas (XMPP era):
  - OZMO 930 (clase 115): mapeo LZMA, fregado mecánico, SpotArea
  - Deebot 900/901 (clase 125): similar al 930 sin fregado
  - OZMO 920/950 (XMPP): transición a JSON, parcialmente compatible
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ModelInfo:
    slug: str            # nombre corto para MQTT/panel
    display: str         # nombre legible
    model_class: str     # clase del JID (ej: "115")
    manufacturer: str = "Ecovacs"
    has_mopping: bool = False
    has_water_level: bool = False
    has_carpet_detect: bool = False
    fan_speeds: list[str] = field(default_factory=lambda: ["silent", "standard", "strong"])
    consumables: list[str] = field(default_factory=lambda: ["Brush", "SideBrush", "DustCaseHeap", "AirFilter"])
    map_types: list[str] = field(default_factory=lambda: ["st"])  # st=estándar, wa=wifi
    sounds: dict[int, str] = field(default_factory=lambda: {})

    @property
    def ha_model(self) -> str:
        return f"{self.display} via deebot930-bridge"


# Registro por clase del JID
MODELS: dict[str, ModelInfo] = {
    "115": ModelInfo(
        slug="ozmo930",
        display="Deebot OZMO 930",
        model_class="115",
        has_mopping=True,
        has_water_level=True,
        has_carpet_detect=True,
        map_types=["st", "wa"],
        sounds={0: "boot", 4: "wheels", 5: "stuck", 6: "dustbin", 17: "ding",
                18: "battery_low", 30: "here", 31: "brush_tangled", 55: "relocating",
                63: "returning", 65: "paused"},
    ),
    "125": ModelInfo(
        slug="deebot900",
        display="Deebot 900/901",
        model_class="125",
        has_mopping=False,
        has_water_level=False,
        has_carpet_detect=True,
        map_types=["st", "wa"],
    ),
    "126": ModelInfo(
        slug="ozmo920",
        display="Deebot OZMO 920",
        model_class="126",
        has_mopping=True,
        has_water_level=True,
        map_types=["st", "wa"],
    ),
}

DEFAULT_MODEL = ModelInfo(
    slug="deebot",
    display="Deebot",
    model_class="unknown",
    map_types=["st"],
)


def detect_model(jid: str) -> ModelInfo:
    """Detecta el modelo desde el JID (SERIAL@CLASE.ecorobot.net/atom)."""
    parts = jid.split("@")
    if len(parts) >= 2:
        domain = parts[1]          # e.g. "115.ecorobot.net"
        sub = domain.split(".")[0]  # e.g. "115"
        if sub in MODELS:
            return MODELS[sub]
    return DEFAULT_MODEL


def detect_from_version(version_str: str) -> ModelInfo | None:
    """Intenta detectar el modelo desde la string de firmware."""
    v = version_str.lower()
    if "930" in v: return MODELS["115"]
    if "900" in v or "901" in v: return MODELS["125"]
    if "920" in v: return MODELS["126"]
    return None
