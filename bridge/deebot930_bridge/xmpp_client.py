"""Cliente XMPP del 930 (fase 3/4).

El robot y la app se comunican a traves del servidor XMPP de Bumper
(puerto 5223, TLS con el CA autofirmado generado por Bumper).

Este modulo se hara pasar por la "app" para:
  - subscribirse a eventos del robot (estado, bateria, consumibles)
  - recibir pushes de posicion y datos de mapa
  - enviar comandos (clean, charge, stop...)

Referencias:
  - bumper/xmppserver.py  (como parsea los mensajes el servidor)
  - sucks (wpietri) y fork bmartin5692 (como hablaba el cliente historico)
  - ecovacs-deebot.js de mrbungle64 (mapa: donde se atascaron las coords)
"""


class XMPPClient:
    """Stub. Se implementara con slixmpp contra el broker de Bumper."""

    def __init__(self, host: str, port: int = 5223):
        self.host = host
        self.port = port
