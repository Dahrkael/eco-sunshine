"""Ecovacs Deebot bridge: robot protocols in, MQTT + web panel out.

Speaks the old XMPP protocol (OZMO 930 era) and the MQTT-based eco-ng
protocol (T8+ robots), decodes maps/traces/state, and republishes
everything to MQTT with Home Assistant discovery plus a live web panel.
"""

__version__ = "0.2.0"
