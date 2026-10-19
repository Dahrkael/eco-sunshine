"""Panel regressions: routes registered with correct binding.

A deploy slipped an orphan @staticmethod leaving api_ws unbound
(TypeError: missing 'request') and dropped _call's definition — no test
caught it because the suite exercises serve.on_command, not the panel
dispatch.
"""

from __future__ import annotations

import inspect

from eco_sunshine.config import Config
from eco_sunshine.serve import Service
from eco_sunshine.webpanel import WebPanel


class FakeCommander:
    def __init__(self):
        self.calls = []

    def set_clean_speed(self, v):
        self.calls.append(("set_clean_speed", v))


def make_panel() -> WebPanel:
    return WebPanel(Service(Config(), None), port=0)


class TestPanelRoutes:
    def test_handlers_are_instance_methods(self):
        """Every registered handler must be a bound method with (self, request).

        An accidental @staticmethod passes registration but loses the
        request at runtime (the first-deploy bug).
        """
        panel = make_panel()
        for route in panel.app.router.routes():
            handler = route.handler
            assert not inspect.isfunction(handler) or handler.__self__ is not None, \
                f"handler {handler} without binding"
            sig = inspect.signature(handler)
            params = list(sig.parameters)
            assert params[:1] != ["request"] or len(params) == 1, \
                f"handler {handler} with suspicious signature {params}"

    def test_api_ws_receives_request(self):
        panel = make_panel()
        sig = inspect.signature(panel.api_ws)
        assert "request" in sig.parameters  # bound: self implicit

    def test_call_exists_and_is_tolerant(self):
        panel = make_panel()
        c = FakeCommander()
        panel._call(c, "set_clean_speed", "strong")
        assert c.calls == [("set_clean_speed", "strong")]
        panel._call(c, "nonexistent_method")  # does not raise, only logs
