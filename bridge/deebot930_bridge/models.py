"""Registro de modelos soportados por el bridge — extraído del decompile del APK.

Fuente: IOTDeviceType.java (enum de dispositivos) + EcoRobotFactory.java
(mapping dispositivo→driver) + análisis de cada driver (capabilities).

Detección: el JID del robot lleva el dominio de la clase
(SERIAL@CLASE.ecorobot.net/atom). La clase identifica el modelo.

Protocolos:
  - EcoRobotProtocol (v1): XML sobre XMPP — generación 2016-2018
  - EcoRobotProtocol2: JSON sobre XMPP — transición, algunos modelos
  (ambos funcionan a través de Bumper sin cambios en el bridge;
  el vocabulario de tds es mayormente compartido)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from collections import Counter


@dataclass
class ModelInfo:
    slug: str
    display: str
    jid_class: str
    driver: str = ""
    has_mopping: bool = False
    has_water_level: bool = False
    has_carpet_detect: bool = True
    has_volume: bool = True
    has_on_off: bool = True
    fan_speeds: list[str] = field(default_factory=lambda: ["silent", "standard", "strong"])
    consumables: list[str] = field(default_factory=lambda: ["Brush", "SideBrush", "DustCaseHeap", "AirFilter"])
    map_types: list[str] = field(default_factory=lambda: ["st", "wa"])
    notes: str = ""
    # "xmpp" (era 930, XML) | "mqtt" (eco-ng T8+, JSON — spec mqtt-json-spec.md)
    protocol: str = "xmpp"

    @property
    def ha_model(self) -> str:
        return f"{self.display} via deebot-bridge"


@dataclass(frozen=True)
class DeviceId:
    """Identidad unificada de un robot — la semilla del registro de
    dispositivos que sustituirá a bumper.bot_add()/SQLite cuando Bumper
    se absorba en el bridge (ver docs/absorcion-bumper.md).

    XMPP: sn=serial, class del JID (ej "115"), resource="atom"
    MQTT: sn=did (UUID del servidor), class 6 chars (ej "yna5xh"), resource="axm"
    """

    sn: str
    device_class: str
    resource: str
    protocol: str = "xmpp"

    @property
    def key(self) -> str:
        """Clave canónica para los dicts de robots/commanders del serve."""
        return f"{self.sn}@{self.device_class}/{self.resource}"

    @property
    def jid(self) -> str:
        """JID XMPP completo (solo tiene sentido con protocol='xmpp')."""
        return f"{self.sn}@{self.device_class}.ecorobot.net/{self.resource}"

    @staticmethod
    def from_jid(jid: str) -> "DeviceId":
        """E000105701860607AB7@115.ecorobot.net/atom → DeviceId xmpp."""
        local, _, rest = jid.partition("@")
        cls = rest.split(".")[0] if rest else ""
        resource = rest.split("/", 1)[1] if "/" in rest else "atom"
        return DeviceId(sn=local, device_class=cls, resource=resource, protocol="xmpp")

    @staticmethod
    def from_key(key: str, protocol: str = "mqtt") -> "DeviceId":
        """{sn}@{cls}/{res} → DeviceId (por defecto mqtt: clave de topic MQTT)."""
        local, _, rest = key.partition("@")
        cls, _, resource = rest.partition("/")
        return DeviceId(sn=local, device_class=cls, resource=resource or "axm",
                        protocol=protocol)


def _m(jid: str, slug: str, display: str, driver: str = "",
       mop: bool = False, water: bool = False, carpet: bool = True,
       speeds: list[str] | None = None, notes: str = "") -> tuple[str, ModelInfo]:
    return jid, ModelInfo(
        slug=slug, display=display, jid_class=jid, driver=driver,
        has_mopping=mop, has_water_level=water, has_carpet_detect=carpet,
        fan_speeds=speeds or ["silent", "standard", "strong"],
        notes=notes,
    )


MODELS: dict[str, ModelInfo] = dict([
    # ═══ DR930 (Protocol v1, LDS, fregado con nivel de agua) ═══
    _m("115", "ozmo930", "Deebot OZMO 930", "DR930", mop=True, water=True,
       notes="LDS, fregado mecánico con nivel de agua, SpotArea"),
    _m("139", "ozmo930cn", "Deebot OZMO 930 (CN)", "DR930", mop=True, water=True),
    _m("lbw54s", "de5g", "Deebot DE5G", "DE5G"),

    # ═══ DR920 (Protocol v1, LDS, fregado con nivel de agua) ═══
    _m("116", "ozmo920", "Deebot OZMO 920", "DR920", mop=True, water=True),

    # ═══ DR95a (Protocol v1, LDS, sin fregado) ═══
    _m("113", "dr95", "Deebot 950/955", "DR95a"),
    _m("118", "dr96", "Deebot 960/965", "DR95a"),
    _m("117", "dr98", "Deebot 980", "DR95a"),

    # ═══ DG720 (Protocol v1, LDS, fregado con nivel de agua) ═══
    _m("119", "dg720", "Deebot DG720", "DG720", mop=True, water=True),
    _m("156", "dd37", "Deebot DD37", "DG720"),

    # ═══ DG726 (Protocol v1, fregado con nivel de agua) ═══
    _m("130", "dg726", "Deebot DG726", "DG726", mop=True, water=True),

    # ═══ DG622 / DN622 (Protocol2, fregado con nivel de agua) ═══
    _m("132", "dg622", "Deebot DG622", "DG622", mop=True, water=True),
    _m("126", "dn622", "Deebot DN622", "DN622", mop=True, water=True),

    # ═══ DN720 (Protocol v1) ═══
    _m("129", "dn720", "Deebot N78", "DN720"),

    # ═══ DN79S (Protocol v1) ═══
    _m("155", "dn79s", "Deebot N79S", "DN79S"),

    # ═══ DH43 (Protocol2, fregado con nivel de agua) ═══
    _m("133", "dh43", "Deebot DH43", "DH43", mop=True, water=True),

    # ═══ DH45 ═══
    _m("144", "dh45", "Deebot DH45", "DH45"),

    # ═══ DM81 / DM80i / DM81 Pro ═══
    _m("125", "dm80i", "Deebot M80i", "DM81"),
    _m("141", "dm81pro", "Deebot M81 Pro", "DM81"),

    # ═══ Mini2 (Protocol2) ═══
    _m("142", "mini2", "Deebot Mini2", "Mini2"),

    # ═══ DT87G (híbrido: usa AMBOS Protocol y Protocol2) ═══
    _m("153", "dt87g", "Deebot T87", "DT87G"),

    # ═══ Slim family (Protocol2 mayormente) ═══
    _m("123", "slim", "Deebot Slim", "Slim"),
    _m("134", "dd30", "Deebot DD30", "Slim"),
    _m("135", "dh36", "Deebot DH36", "Slim"),
    _m("107", "dm88", "Deebot M88", "Slim"),
    _m("122", "dr97", "Deebot R97", "Slim"),
    _m("124", "dg718", "Deebot DG718", "Slim"),
    _m("114", "dn630", "Deebot N63", "Slim"),
    _m("120", "ds620", "Deebot S20", "Slim"),
    _m("131", "de33", "Deebot 710", "Slim"),
    _m("143", "dd53", "Deebot DD53", "Slim"),
    _m("145", "dd56", "Deebot DD56", "Slim"),
    _m("148", "db53", "Deebot DB53", "Slim"),
    _m("150", "dd36", "Deebot DD36", "Slim"),
    _m("151", "dd35e", "Deebot 35E", "Slim"),
    _m("112", "d112", "Deebot 112", "Slim"),
    _m("137", "d36e", "Deebot 36E", "Slim"),
    _m("138", "dw701", "Deebot W70", "Slim"),
    _m("140", "slimneo", "Deebot Slim Neo", "Slim"),
    _m("wbueya", "slim4", "Deebot Slim 4", "Slim4"),
    _m("02uwxm", "slim4intl", "Deebot Slim 4 Intl", "Slim4_intl"),
    _m("d2i688", "dk39", "Deebot DK39", "Slim"),
    _m("eifibd", "slim11", "Deebot Slim 11", "Slim"),
    _m("l5j1rh", "ds35", "Deebot S35", "Slim"),
    _m("qdbjuh", "ds36", "Deebot S36", "Slim"),
    _m("byahsh", "ds37", "Deebot S37", "Slim"),

    # ═══ ChaoBao3 (Conga rebrand, Protocol v1) ═══
    _m("136", "chaobao3", "Conga 3090", "ChaoBao3"),
    _m("160", "d36s", "Conga D36S", "ChaoBao3"),
    _m("162", "db30", "Conga DB30", "ChaoBao3"),
])

# ═══ Robots eco-ng (MQTT/JSON, T8+) — class IDs del spec §7 y home-apk-models.md ═══
# La mayoría se asignan dinámicamente en el servidor; el robot anuncia su class
# en el client-id al conectar al broker ({did}@{class}/{res}).
MQTT_MODELS: dict[str, ModelInfo] = dict([
    _m("yna5xh", "t9aivi", "Deebot OZMO T9 AIVI", "dt9ai_main"),
    _m("x7p4y4", "t9pro", "Deebot OZMO T9 PRO", "dt9_main"),
    _m("vsc5ia", "t8n8", "Deebot T8/N8 family", "dt8_main"),
    _m("qqy0di", "aa30", "Deebot AA30 (AIRBOT)", "lib_iot_client"),
    _m("oxandq", "a680", "Deebot A680 (AIRBOT)", ""),
])
for _m_info in MQTT_MODELS.values():
    _m_info.protocol = "mqtt"

DEFAULT_MODEL = ModelInfo(
    slug="deebot",
    display="Deebot",
    jid_class="unknown",
    map_types=["st"],
)

DEFAULT_MQTT_MODEL = ModelInfo(
    slug="deebot-mqtt",
    display="Deebot (eco-ng)",
    jid_class="unknown",
    map_types=["ol"],
    protocol="mqtt",
)


def detect_model(jid: str) -> ModelInfo:
    """Detecta el modelo desde el JID (SERIAL@CLASE.ecorobot.net/atom)."""
    parts = jid.split("@")
    if len(parts) >= 2:
        sub = parts[1].split("/")[0].split(".")[0]
        if sub in MODELS:
            return MODELS[sub]
    return DEFAULT_MODEL


def detect_model_mqtt(device_class: str) -> ModelInfo:
    """Detecta el modelo desde el class del client-id MQTT ({did}@{cls}/{res})."""
    return MQTT_MODELS.get(device_class, DEFAULT_MQTT_MODEL)


def detect_model_for(did: DeviceId) -> ModelInfo:
    """Detecta el modelo desde un DeviceId, según su protocolo."""
    if did.protocol == "mqtt":
        return detect_model_mqtt(did.device_class)
    return detect_model(did.jid)


def detect_from_version(version_str: str) -> ModelInfo | None:
    """Intenta detectar el modelo desde la string de firmware."""
    v = version_str.lower()
    for pattern, cls in [("930", "115"), ("920", "116"), ("950", "113"),
                         ("960", "118"), ("980", "117"), ("720", "119"),
                         ("726", "130"), ("622", "126")]:
        if pattern in v:
            return MODELS.get(cls)
    return None


def model_summary() -> str:
    c = Counter(m.driver for m in MODELS.values())
    lines = [f"  {d:14s}: {n} modelos" for d, n in c.most_common()]
    lines.append(f"  MQTT (eco-ng) : {len(MQTT_MODELS)} class ids registrados")
    return "\n".join(lines)
