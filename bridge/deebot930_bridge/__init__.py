"""deebot930-bridge: XMPP -> decoder -> renderer -> MQTT (formato Valetudo).

Fases:
  [x] F0 skeleton
  [ ] F2 tail de trafico de Bumper para capturar frames crudos
  [ ] F3 decoder de mensajes XML del 930 (posicion, mapa, estado)
  [ ] F4 renderer (PNG) + publicador MQTT compatible con valetudo-map-card
"""

__version__ = "0.1.0"
