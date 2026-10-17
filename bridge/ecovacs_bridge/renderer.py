"""Renderer del mapa + publicador MQTT (fase 4).

Estrategia: publicar en el formato de topics MQTT de Valetudo para
reutilizar el ecosystem (valetudo-map-card en Lovelace, MQTT discovery):
  - mapa renderizado (PNG base64)
  - posicion del robot + dock
  - path recorrido
  - estado del vacuum (entidad vacuum.mqtt de HA)

Fallback si el payload del mapa no es decodificable: reconstruir el mapa
por acumulacion de posiciones del robot (trayectoria LDS).
"""


def render(events: list[dict]) -> bytes:
    """Renderiza eventos decodificados a un PNG del mapa. TODO(fase 4)."""
    raise NotImplementedError
