"""Servidores embebidos del bridge — absorción de Bumper (Fase F).

Cada componente vive en su módulo y se activa por su flag en
``[servers]`` (config.py). El ciclo de vida lo gestiona serve.run().

- lookup.py  :8007  bootstrap del robot (POST /lookup.do)   [FA]
- xmpp.py    :5223  servidor XMPP (era 930)                 [FB]
- mqtt.py    :8883  broker MQTT eco-ng                      [FC]
- conf.py    :443   API de la app Ecovacs                   [FD]

Con todos off, el bridge se comporta como hasta ahora: cliente puro contra
un Bumper externo.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# fases pendientes de implementar — el serve avisa si se activan pronto
NOT_YET = {
    "conf": "FD",
}
