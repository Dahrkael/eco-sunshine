"""Muros virtuales, zonas y habitaciones — modelos + parsers XML/JSON.

Fuentes:
- XML (era 930): corpus propio (frames MapSet: <ctl msid td="MapSet" tp><m
  mid n p/></ctl>) + vocabulario del driver DR930.java (decompile). El
  corpus no contiene muros/zonas configurados (entradas vacías) → los
  formatos con datos reales quedan A VALIDAR con robot; parsers tolerantes.
- JSON (eco-ng, T8+): docs/mqtt-json-spec.md §4.5 (getMapSet/getMapSubSet,
  verificado contra APK Home v3.16.1 y ecovacs-deebot.js).

Tipos (tp):
- vw: muros virtuales (línea de 2 puntos, en mm)
- mw: zonas no-fregar (rectángulo, mm) — eco-ng; en 930 viaja en vw con
  atributo de tipo cuando aplica
- sa: zonas de limpieza custom (rectángulo)
- ar: habitaciones (eco-ng; mssid + nombre + centro)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

ATTR_RE = re.compile(r'(\w+)="([^"]*)"')


# ---------- modelos ----------

@dataclass
class VirtualWall:
    mid: str
    x1: float
    y1: float
    x2: float
    y2: float

    def to_doc(self) -> dict:
        return {"mid": self.mid, "x1": self.x1, "y1": self.y1,
                "x2": self.x2, "y2": self.y2}


@dataclass
class Zone:
    """Rectángulo (esquinas opuestas) — zona no-fregar (mw) o custom (sa)."""
    mid: str
    x1: float
    y1: float
    x2: float
    y2: float
    kind: str = "mw"  # mw=no-fregar, sa=custom

    def to_doc(self) -> dict:
        return {"mid": self.mid, "x1": self.x1, "y1": self.y1,
                "x2": self.x2, "y2": self.y2, "kind": self.kind}


@dataclass
class Room:
    mssid: str
    name: str
    center: tuple[float, float] | None = None
    index: int = 0
    # coordenadas del contorno ("x,y;x,y;…") si se conocen (getMapSubSet)
    outline: list[tuple[float, float]] = field(default_factory=list)

    def to_doc(self) -> dict:
        return {"mssid": self.mssid, "name": self.name, "index": self.index,
                "center": list(self.center) if self.center else None}


@dataclass
class MapSetData:
    tp: str  # vw|mw|sa|ar
    msid: str = ""
    entries: list = field(default_factory=list)  # VirtualWall|Zone|Room

    def to_doc(self) -> dict:
        return {"tp": self.tp, "msid": self.msid,
                "entries": [e.to_doc() if hasattr(e, "to_doc") else dict(e)
                            for e in self.entries]}


# ---------- helpers ----------

def _coords(s: str) -> list[float]:
    out = []
    for part in (s or "").split(","):
        part = part.strip()
        try:
            out.append(float(part))
        except ValueError:
            pass
    return out


# ---------- parsers XML (era 930) ----------

def parse_mapset_xml(attrs: dict, inner: str) -> MapSetData | None:
    """Frame MapSet (respuesta GetMapSet o push) → MapSetData.

    <ctl msid="0" td="MapSet" tp="vw" ts="…"><m mid="0" n="" p="x1,y1,x2,y2"/></ctl>
    Entradas con mid vacío = set vacío (patrón del corpus).
    """
    tp = attrs.get("tp", "")
    if not tp:
        return None
    data = MapSetData(tp=tp, msid=attrs.get("msid", ""))
    # ns0:-prefijado: Bumper re-serializa los frames y los hijos pueden
    # llegar con namespace (<ns0:m …/>) — regex agnóstica de prefijo
    for m in re.finditer(r"<(?:[\w.-]+:)?m\s+([^>]*?)/?>", inner):
        a = dict(ATTR_RE.findall(m.group(1)))
        mid, p = a.get("mid", ""), a.get("p", "")
        if not mid and not p:
            continue  # entrada vacía (sin muros/zonas configurados)
        c = _coords(p)
        if tp == "vw":
            # atributo t (si existe) distingue muro de zona no-fregar
            if len(c) >= 4:
                entry: object
                if a.get("t") in ("mw", "zone", "1"):
                    entry = Zone(mid, *c[:4], kind="mw")
                else:
                    entry = VirtualWall(mid, *c[:4])
                data.entries.append(entry)
        elif tp in ("sa", "mw") and len(c) >= 4:
            data.entries.append(Zone(mid, *c[:4], kind=tp))
        elif tp == "ar":
            room = Room(mssid=mid, name=a.get("n", ""), index=int(a.get("index", 0) or 0))
            center = _coords(a.get("center", ""))
            if len(center) >= 2:
                room.center = (center[0], center[1])
            data.entries.append(room)
    return data


def parse_stats_xml(attrs: dict, inner: str) -> dict:
    """GetCleanStatistics/GetCleanSum → {total_time, total_area, total_count}.

    Formato exacto no presente en el corpus (no se pidieron) → tolerante:
    - attrs planos (CleanSum: a=área t=tiempo c=cuenta, convención sucks)
    - hijos <statistics .../> o <clean .../> con area/time/count
    """
    out: dict = {}
    # attrs directos
    if attrs.get("a") and attrs.get("t"):
        out.setdefault("total_area", float(attrs["a"]))
        out.setdefault("total_time", float(attrs["t"]))
    if attrs.get("c"):
        out.setdefault("total_count", int(attrs["c"]))
    # hijos (posible prefijo de namespace por la re-serialización de Bumper)
    for tag in re.finditer(r"<(?:[\w.-]+:)?\w+ ([^>]*?)/?>", inner):
        a = dict(ATTR_RE.findall(tag.group(1)))
        for src_area in ("area", "a"):
            if a.get(src_area) and "total_area" not in out:
                try:
                    out["total_area"] = float(a[src_area])
                except ValueError:
                    pass
        for src_time in ("time", "t"):
            if a.get(src_time) and "total_time" not in out:
                try:
                    out["total_time"] = float(a[src_time])
                except ValueError:
                    pass
        for src_cnt in ("count", "c"):
            if a.get(src_cnt) and "total_count" not in out:
                try:
                    out["total_count"] = int(a[src_cnt])
                except ValueError:
                    pass
    return out


# ---------- parsers JSON (eco-ng, spec §4.5) ----------

def parse_mapset_json(data: dict) -> MapSetData | None:
    """Respuesta getMapSet → MapSetData (habitaciones ar / muros vw / mw)."""
    tp = str(data.get("type", ""))
    if not tp:
        return None
    ms = MapSetData(tp=tp, msid=str(data.get("msid", "")))
    for sub in data.get("subsets") or []:
        if not isinstance(sub, dict):
            continue
        if tp == "ar":
            center = None
            c = _coords(str(sub.get("center", "")))
            if len(c) >= 2:
                center = (c[0], c[1])
            ms.entries.append(Room(mssid=str(sub.get("mssid", "")),
                                   name=str(sub.get("name", "")),
                                   center=center,
                                   index=int(sub.get("index", 0) or 0)))
        else:  # vw / mw: las coordenadas llegan por getMapSubSet (value)
            ms.entries.append({"mssid": str(sub.get("mssid", "")),
                               "name": str(sub.get("name", "")),
                               "_pending_outline": True})
    return ms


def _entry_mssid(e) -> str | None:
    """Id de una entrada sea dict pendiente, Room (.mssid) o Wall/Zone (.mid)."""
    if isinstance(e, dict):
        return e.get("mssid")
    return getattr(e, "mssid", None) or getattr(e, "mid", None)


def parse_subset_json(data: dict, sets: dict[str, MapSetData]) -> None:
    """Respuesta getMapSubSet: coordina el contorno de un mssId y lo resuelve
    a VirtualWall/Zone dentro del MapSetData correspondiente (muta sets)."""
    tp = str(data.get("type", ""))
    mssid = str(data.get("mssid", ""))
    pts: list[tuple[float, float]] = []
    for pair in str(data.get("value", "")).split(";"):
        c = _coords(pair)
        if len(c) >= 4:  # formato plano defensivo: "x1,y1,x2,y2"
            pts.append((c[0], c[1]))
            pts.append((c[2], c[3]))
        elif len(c) >= 2:
            pts.append((c[0], c[1]))
    ms = sets.get(tp)
    if ms is None:
        return
    for i, e in enumerate(ms.entries):
        if _entry_mssid(e) != mssid:
            continue
        if tp == "ar" and hasattr(e, "outline"):
            e.outline = pts
            if not e.center and pts:
                e.center = pts[0]
        elif len(pts) >= 2:
            (x1, y1), (x2, y2) = pts[0], pts[-1]
            ms.entries[i] = (VirtualWall(mssid, x1, y1, x2, y2) if tp == "vw"
                             else Zone(mssid, x1, y1, x2, y2, kind=tp))
        return


def parse_stats_json(data: dict) -> dict:
    """getTotalStats/getStats/onStats → {total_time, total_area, total_count}.

    OJO unidades: time en s, area en mm² (spec §4.2: area=45000000 ≈ 45 m²).
    """
    out: dict = {}
    if isinstance(data.get("time"), (int, float)):
        out["total_time"] = float(data["time"])
    elif isinstance(data.get("total"), (int, float)):
        out["total_time"] = float(data["total"])
    if isinstance(data.get("area"), (int, float)):
        out["total_area"] = float(data["area"])
    if isinstance(data.get("count"), (int, float)):
        out["total_count"] = int(data["count"])
    elif isinstance(data.get("mapCount"), (int, float)):
        out["total_count"] = int(data["mapCount"])
    return out
