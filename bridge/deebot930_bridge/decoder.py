"""Decoder de mensajes XML del OZMO 930 (fase 3 - nucleo del proyecto).

Objetivos de ingenieria inversa, por orden:
  1. Mensajes de estado/bateria/consumibles (td="..." conocidos en sucks/xmppserver)
  2. PushRobotPos / posicion en vivo (transform de coordenadas)
  3. Payload de mapa (formato por determinar: probablemente troceado y
     comprimido; la issue #10 de ecovacs-deebot.js documenta el fallo previo
     en la traduccion a coordenadas - nuestra referencia principal)

Toda traza capturada se archivara en traces/ con su screenshot de la app
como ground truth visual.
"""


def decode(raw_frame: str) -> dict:
    """Decodifica un frame XMPP crudo a un evento estructurado.

    TODO(fase 3).
    """
    raise NotImplementedError
