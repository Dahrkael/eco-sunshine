"""Registro de modelos soportados por el bridge — extraído del decompile del APK.

Fuente: IOTDeviceType.java (enum de dispositivos) + EcoRobotFactory.java
(mapping dispositivo→driver) + análisis de cada driver (capabilities).

Detección: el JID del robot lleva el dominio de la clase
(SERIAL@CLASE.ecorobot.net/atom). La clase identifica el modelo.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ModelInfo:
    slug: str
    display: str
    jid_class: str           # dominio del JID (ej: "115.ecorobot.net")
    driver: str = ""         # clase Java del driver (ej: "DR930")
    has_mopping: bool = False
    has_water_level: bool = False
    has_carpet_detect: bool = False
    has_volume: bool = True
    has_on_off: bool = True
    fan_speeds: list[str] = field(default_factory=lambda: ["silent", "standard", "strong"])
    consumables: list[str] = field(default_factory=lambda: ["Brush", "SideBrush", "DustCaseHeap", "AirFilter"])
    map_types: list[str] = field(default_factory=lambda: ["st", "wa"])
    notes: str = ""

    @property
    def ha_model(self) -> str:
        return f"{self.display} via deebot-bridge"


def _m(jid: str, slug: str, display: str, driver: str = "",
       mop: bool = False, water: bool = False, carpet: bool = True,
       speeds: list[str] | None = None, notes: str = "") -> tuple[str, ModelInfo]:
    return jid, ModelInfo(
        slug=slug, display=display, jid_class=jid, driver=driver,
        has_mopping=mop, has_water_level=water, has_carpet_detect=carpet,
        fan_speeds=speeds or ["silent", "standard", "strong"],
        notes=notes,
    )


# Registro completo: dominio JID → ModelInfo
# (dominios de IOTDeviceType.java que usan XMPP/EcoAllRobot)
MODELS: dict[str, ModelInfo] = dict([
    # --- OZMO 930 family (driver DR930) ---
    _m("115", "ozmo930", "Deebot OZMO 930", "DR930", mop=True, water=True,
       notes="LDS, fregado mecánico con nivel de agua, SpotArea"),
    _m("139", "ozmo930cn", "Deebot OZMO 930 (CN)", "DR930", mop=True, water=True),
    _m("lbw54s", "de5g", "Deebot DE5G", "DR930", notes="variante del 930"),

    # --- OZMO 920 (driver DR920) ---
    _m("116", "ozmo920", "Deebot OZMO 920", "DR920", mop=True, water=True),

    # --- DR95/96/98 (driver DR95a) ---
    _m("113", "dr95", "Deebot 950/955", "DR95a"),
    _m("118", "dr96", "Deebot 960/965", "DR95a"),
    _m("117", "dr98", "Deebot 980", "DR95a"),

    # --- DG720/DD37 (driver DG720) ---
    _m("119", "dg720", "Deebot DG720", "DG720", mop=True, water=True),
    _m("156", "dd37", "Deebot DD37", "DG720"),

    # --- DG726/DG622/DN622 (driver DG726/DG622) ---
    _m("130", "dg726", "Deebot DG726", "DG726", mop=True, water=True),
    _m("132", "dg622", "Deebot DG622", "DG622", mop=True, water=True),
    _m("126", "dn622", "Deebot DN622", "DN622", mop=True, water=True),

    # --- DN720 (driver DN720) ---
    _m("129", "dn720", "Deebot N78", "DN720"),

    # --- DH43/DH45 (driver DH43/DH45) ---
    _m("133", "dh43", "Deebot DH43", "DH43", mop=True, water=True),
    _m("144", "dh45", "Deebot DH45", "DH45"),

    # --- DF35 (driver DF35 — purificador de aire, no aspiradora) ---
    # _m("146", "df35", "AtmoBot AE", "DF35"),  # no es aspiradora

    # --- DM80i/DM81 (driver DM81/DM81_Pro) ---
    _m("125", "dm80i", "Deebot M80i", "DM81"),
    _m("141", "dm81pro", "Deebot M81 Pro", "DM81"),

    # --- Mini2 (driver Mini2) ---
    _m("142", "mini2", "Deebot Mini2", "Mini2"),

    # --- Slim (driver Slim) ---
    _m("123", "slim", "Deebot Slim", "Slim"),

    # --- ChaoBao3 (driver ChaoBao3 — Conga rebrand) ---
    _m("136", "chaobao3", "Conga 3090", "ChaoBao3"),
    _m("160", "d36s", "Conga D36S", "ChaoBao3"),
    _m("162", "db30", "Conga DB30", "ChaoBao3"),

    # --- Slim/DD30 (driver Slim) ---
    _m("134", "dd30", "Deebot DD30", "Slim"),
    _m("135", "dh36", "Deebot DH36", "Slim"),

    # --- Otros con EcoAllRobot ---
    _m("107", "dm88", "Deebot M88", "Slim"),
    _m("122", "dr97", "Deebot R97", "Slim"),
    _m("124", "dg718", "Deebot DG718", "Slim"),
    _m("114", "dn630", "Deebot N63", "Slim"),
    _m("120", "ds620", "Deebot S20", "Slim"),
    _m("131", "de33", "Deebot 710", "Slim"),
    _m("143", "dd53", "Deebot DD53", "Slim"),
    _m("145", "dd56", "Deebot DD56", "Slim"),
    _m("153", "dt87g", "Deebot T87", "Slim"),
    _m("157", "df43", "Deebot F43", "Slim"),
    _m("158", "df45", "Deebot F45", "Slim"),
    _m("148", "db53", "Deebot DB53", "Slim"),
    _m("150", "dd36", "Deebot DD36", "Slim"),
    _m("151", "dd35e", "Deebot 35E", "Slim"),
    _m("112", "d112", "Deebot 112", "Slim"),
    _m("137", "d36e", "Deebot 36E", "Slim"),
    _m("138", "dw701", "Deebot W70", "Slim"),
])

DEFAULT_MODEL = ModelInfo(
    slug="deebot",
    display="Deebot",
    jid_class="unknown",
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
    checks = [
        ("930", "115"), ("920", "116"), ("950", "113"), ("960", "118"),
        ("980", "117"), ("720", "119"), ("726", "130"), ("622", "126"),
    ]
    for pattern, cls in checks:
        if pattern in v:
            return MODELS.get(cls)
    return None
