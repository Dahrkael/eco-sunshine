"""Embedded bridge servers — Bumper absorption (Phase F).

Each component lives in its own module and is enabled by its flag in
``[servers]`` (config.py). serve.run() manages the lifecycle.

- lookup.py  :8007  robot bootstrap (POST /lookup.do)   [FA]
- xmpp.py    :5223  XMPP server (930 era)               [FB]
- mqtt.py    :8883  eco-ng MQTT broker                  [FC]
- conf.py    :443   Ecovacs app API                     [FD]

With all off, the bridge behaves as before: a pure client against an
external Bumper.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# phases still pending — serve warns if they are enabled early
# (FA–FD are done: none left)
NOT_YET: dict[str, str] = {}
