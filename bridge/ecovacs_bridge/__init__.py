"""deebot930-bridge: XMPP + MQTT (eco-ng) → decoder → panel → MQTT (HA).

Fases:
  [x] F0 skeleton
  [x] F1 emparejamiento y captura (corpus sesiones 1-3)
  [x] F2 tail de tráfico de Bumper (frames crudos)
  [x] F3 decoder XML del 930 (posición, mapa LZMA, traza, estado)
  [x] F4 bridge XMPP → MQTT con discovery HA + panel web
  [x] F5 transporte MQTT robots T8+ (eco-ng: mapa, traza 5B, zstd)
  [x] F6 muros/zonas/habitaciones/stats en ambos protocolos
  [ ] F7 absorción de Bumper en el bridge (docs/absorcion-bumper.md)
"""

__version__ = "0.2.0"
