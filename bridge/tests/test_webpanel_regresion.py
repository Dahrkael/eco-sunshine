"""Regresiones del panel: rutas registradas con binding correcto.

Caza el bug del deploy real: un @staticmethod huérfano dejó api_ws sin
binding (TypeError: missing 'request') y la definición de _call desapareció
— ningún test lo cubría porque la suite ejerce serve.on_command, no el
dispatch del panel.
"""

from __future__ import annotations

import inspect

from deebot930_bridge.config import Config
from deebot930_bridge.serve import Service
from deebot930_bridge.webpanel import WebPanel


class FakeCommander:
    def __init__(self):
        self.calls = []

    def set_clean_speed(self, v):
        self.calls.append(("set_clean_speed", v))


def make_panel() -> WebPanel:
    return WebPanel(Service(Config(), None), port=0)


class TestRutasPanel:
    def test_handlers_son_metodos_de_instancia(self):
        """Cada handler registrado debe ser un bound method con (self, request).

        Un @staticmethod accidental pasa el registro pero pierde el request
        en runtime (el bug del monolito en el primer deploy).
        """
        panel = make_panel()
        for route in panel.app.router.routes():
            handler = route.handler
            assert not inspect.isfunction(handler) or handler.__self__ is not None, \
                f"handler {handler} sin binding"
            sig = inspect.signature(handler)
            params = list(sig.parameters)
            assert params[:1] != ["request"] or len(params) == 1, \
                f"handler {handler} con firma sospechosa {params}"

    def test_api_ws_recibe_request(self):
        panel = make_panel()
        sig = inspect.signature(panel.api_ws)
        assert "request" in sig.parameters  # bound: self implícito

    def test_call_existe_y_es_tolerante(self):
        panel = make_panel()
        c = FakeCommander()
        panel._call(c, "set_clean_speed", "strong")
        assert c.calls == [("set_clean_speed", "strong")]
        panel._call(c, "metodo_inexistente")  # no lanza, solo loguea
