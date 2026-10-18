"""Phase FD tests: embedded conf (:443) — the Ecovacs app API.

Shapes copied from Bumper's confserver plugins and verified against the
corpus (pairing-session.log, old app 1.4.9): login → getAuthCode →
user.do loginByItToken → user.do GetDeviceList.

- login accepts any credentials (use_auth=False, like Bumper)
- GetDeviceList lists the DeviceRegistry's bots (XMPP and MQTT)
- loginByItToken exchanges getAuthCode's authCode
- TLS with ensure_certs certs (FE) and full verification
- serve wiring: conf on/off, certs/port failures take nothing down
"""

from __future__ import annotations

import re

import pytest
from aiohttp import ClientSession, TCPConnector

from ecovacs_bridge.config import Config
from ecovacs_bridge.serve import Service
from ecovacs_bridge.servers import NOT_YET
from ecovacs_bridge.servers.certs import ensure_certs
from ecovacs_bridge.servers.conf import ConfAuth, ConfServer, login_data
from ecovacs_bridge.servers.registry import DeviceRegistry

# old app's private path exactly as it appears in the corpus (apptype i_eco_e)
APP_LOGIN = ("/v1/private/es/en/5d8a10c413d47b4194c29ef1580cbb04/i_eco_e"
             "/1.4.9/c_googleplay/1/user/login")
APP_GET_AUTHCODE = APP_LOGIN.replace("/user/login", "/user/getAuthCode")

DID = "b11fceaf-1234-5678-9abc-def012345678"


def free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _registry_with_bot() -> DeviceRegistry:
    reg = DeviceRegistry()
    reg.bot_add("E00010571234567890ab", DID, "yna5xh", "axm", "eco-legacy")
    reg.bot_set_xmpp(DID, True)
    return reg


# ---------- sessions (tokens/authcodes) ----------


class TestConfAuth:
    def test_login_mints_hex32_token_and_rotates(self):
        auth = ConfAuth()
        s1 = auth.login("dev1")
        assert re.fullmatch(r"[0-9a-f]{32}", s1.token)
        assert auth.login("dev1").token != s1.token  # re-login = new token

    def test_authcode_stable_per_session(self):
        auth = ConfAuth()
        auth.login("dev1")
        ac1 = auth.authcode("dev1", "es")
        assert re.fullmatch(r"es_[0-9a-f]{32}", ac1)
        assert auth.authcode("dev1", "es") == ac1  # cached like Bumper
        auth.login("dev1")  # re-login → new authcode
        assert auth.authcode("dev1", "es") != ac1

    def test_authcode_without_previous_login_creates_session(self):
        """Bridge restart mid-session: the app just redoes the flow."""
        auth = ConfAuth()
        assert re.fullmatch(r"us_[0-9a-f]{32}", auth.authcode("dev9", "us"))

    def test_check_authcode(self):
        auth = ConfAuth()
        auth.login("dev1")
        ac = auth.authcode("dev1", "es")
        assert auth.check_authcode("fuid_tmpuser", ac)
        assert auth.check_authcode("tmpuser", ac)  # with/without fuid_ (like Bumper)
        assert not auth.check_authcode("fuid_other", ac)
        assert not auth.check_authcode("fuid_tmpuser", "es_never_issued")

    def test_logout_invalidates_the_authcode(self):
        auth = ConfAuth()
        auth.login("dev1")
        ac = auth.authcode("dev1", "es")
        auth.logout("dev1")
        assert not auth.check_authcode("fuid_tmpuser", ac)


# ---------- login shapes ----------


class TestLoginData:
    def test_legacy_app(self):
        d = login_data("i_eco_e", "es", "tok123")
        assert list(d) == ["accessToken", "uid", "username", "country", "email"]
        assert d == {"accessToken": "tok123", "uid": "fuid_tmpuser",
                     "username": "fusername_tmpuser", "country": "es",
                     "email": "null@null.com"}

    def test_ecovacs_home_extras_first(self):
        d = login_data("global_a", "de", "tok123")
        assert list(d)[:3] == ["ucUid", "loginName", "mobile"]
        assert d["mobile"] is None and d["uid"] == "fuid_tmpuser"


# ---------- server (plain HTTP, like the FA/FC tests) ----------


class TestConfServer:
    async def test_full_app_flow(self):
        """login → getAuthCode → loginByItToken → GetDeviceList (corpus)."""
        srv = ConfServer(_registry_with_bot(), announce_ip="192.168.1.50",
                         xmpp_port=5223, listen="127.0.0.1", port=free_port())
        await srv.start()
        try:
            async with ClientSession() as s:
                base = f"http://127.0.0.1:{srv.port}"
                # 1. login (the app sends encrypted credentials: ignored)
                async with s.get(f"{base}{APP_LOGIN}",
                                 params={"account": "x", "password": "y"}) as r:
                    assert r.status == 200
                    login = await r.json()
                assert login["code"] == "0000"
                assert login["msg"] == "操作成功"
                assert re.fullmatch(r"[0-9a-f]{32}",
                                    login["data"]["accessToken"])
                assert login["data"]["uid"] == "fuid_tmpuser"
                assert login["data"]["country"] == "es"

                # 2. getAuthCode (uid/accessToken in the query, like the app)
                async with s.get(f"{base}{APP_GET_AUTHCODE}", params={
                        "uid": "fuid_tmpuser",
                        "accessToken": login["data"]["accessToken"]}) as r:
                    ac = (await r.json())["data"]
                assert re.fullmatch(r"es_[0-9a-f]{32}", ac["authCode"])
                assert ac["ecovacsUid"] == "fuid_tmpuser"

                # 3. loginByItToken (form-urlencoded with the authCode)
                async with s.post(f"{base}/api/users/user.do", data={
                        "todo": "loginByItToken", "country": "ES",
                        "resource": "be9baaf0", "org": "ECOWW",
                        "realm": "ecouser.net", "userId": "fuid_tmpuser",
                        "token": ac["authCode"]}) as r:
                    body = await r.json()
                assert body == {"resource": "be9baaf0", "result": "ok",
                                "todo": "result", "token": ac["authCode"],
                                "userId": "fuid_tmpuser"}

                # 4. GetDeviceList (json) — the registry's bots
                async with s.post(
                        f"{base}/api/users/user.do?td=GetDeviceList",
                        json={"todo": "GetDeviceList", "userid": "fuid_tmpuser",
                              "auth": {"with": "users", "userid": "fuid_tmpuser",
                                       "realm": "ecouser.net",
                                       "token": ac["authCode"],
                                       "resource": "be9baaf0"}}) as r:
                    body = await r.json()
                assert body["result"] == "ok" and body["todo"] == "result"
                assert body["devices"] == [{
                    "class": "yna5xh", "company": "eco-legacy", "did": DID,
                    "name": "E00010571234567890ab", "nick": "",
                    "resource": "axm", "mqtt_connection": False,
                    "xmpp_connection": True}]
        finally:
            await srv.stop()

    async def test_login_by_it_token_rejected_with_use_auth(self):
        """use_auth=True: unknown authcode → empty body (like Bumper)."""
        reg = DeviceRegistry(use_auth=True)
        srv = ConfServer(reg, listen="127.0.0.1", port=free_port())
        await srv.start()
        try:
            async with ClientSession() as s:
                async with s.post(
                        f"http://127.0.0.1:{srv.port}/api/users/user.do",
                        data={"todo": "loginByItToken",
                              "userId": "fuid_tmpuser",
                              "token": "es_made_up"}) as r:
                    assert await r.json() == {}
        finally:
            await srv.stop()

    async def test_bot_management_by_the_app(self):
        reg = _registry_with_bot()
        srv = ConfServer(reg, listen="127.0.0.1", port=free_port())
        await srv.start()
        try:
            async with ClientSession() as s:
                base = f"http://127.0.0.1:{srv.port}"
                async with s.post(f"{base}/api/users/user.do",
                                  json={"todo": "SetDeviceNick", "did": DID,
                                        "nick": "Lounge"}) as r:
                    assert (await r.json())["result"] == "ok"
                assert reg.bot_get(DID)["nick"] == "Lounge"
                async with s.post(f"{base}/api/users/user.do",
                                  json={"todo": "DeleteOneDevice",
                                        "did": DID}) as r:
                    assert (await r.json())["result"] == "ok"
                assert reg.bot_get(DID) is None
        finally:
            await srv.stop()

    async def test_courtesy_statics(self):
        """Endpoints the app probes and Bumper answered with statics."""
        srv = ConfServer(_registry_with_bot(), listen="127.0.0.1",
                         port=free_port())
        await srv.start()
        try:
            async with ClientSession() as s:
                base = f"http://127.0.0.1:{srv.port}"
                async with s.get(f"{base}{APP_LOGIN.replace('/user/login', '/user/checkAgreement')}") as r:
                    body = await r.json()
                assert body["code"] == "0000" and body["data"] == []
                async with s.get(f"{base}{APP_LOGIN.replace('/user/login', '/common/checkVersion')}") as r:
                    body = await r.json()
                assert body["data"]["r"] == 0 and body["data"]["v"] is None
                async with s.get(f"{base}{APP_LOGIN.replace('/user/login', '/campaign/homePageAlert')}") as r:
                    assert (await r.json())["data"]["hasCampaign"] == "N"
                async with s.get(f"{base}{APP_LOGIN.replace('/user/login', '/user/logout')}") as r:
                    body = await r.json()
                assert body["code"] == "0000" and body["data"] is None
        finally:
            await srv.stop()

    async def test_confserver_base(self):
        """lookup.do/newauth.do also live on :443 (like Bumper)."""
        srv = ConfServer(DeviceRegistry(), announce_ip="10.1.2.3",
                         xmpp_port=5223, listen="127.0.0.1", port=free_port())
        await srv.start()
        try:
            async with ClientSession() as s:
                base = f"http://127.0.0.1:{srv.port}"
                async with s.post(f"{base}/lookup.do", json={
                        "todo": "FindBest", "service": "EcoMsgNew"}) as r:
                    text = await r.text()
                assert " " not in text  # compact JSON for the firmware
                import json

                assert json.loads(text) == {"ip": "10.1.2.3", "port": 5223,
                                            "result": "ok"}
                async with s.post(f"{base}/newauth.do", data={
                        "itToken": "es_abc", "todo": "newAuth"}) as r:
                    assert await r.json() == {"authCode": "es_abc",
                                              "result": "ok",
                                              "todo": "result"}
                # portal GET → fail (like Bumper)
                async with s.get(f"{base}/api/users/user.do") as r:
                    assert (await r.json())["result"] == "fail"
        finally:
            await srv.stop()

    async def test_get_authcode_global(self):
        """/v1/global/auth/getAuthCode (deviceId in the query, no path)."""
        srv = ConfServer(DeviceRegistry(), listen="127.0.0.1", port=free_port())
        await srv.start()
        try:
            async with ClientSession() as s:
                async with s.get(
                        f"http://127.0.0.1:{srv.port}/v1/global/auth/getAuthCode",
                        params={"deviceId": "dev-global",
                                "uid": "fuid_tmpuser"}) as r:
                    body = await r.json()
                assert body["code"] == "0000"
                assert re.fullmatch(r"us_[0-9a-f]{32}",
                                    body["data"]["authCode"])
        finally:
            await srv.stop()

    async def test_post_on_login_also_validates(self):
        """web.route('*') on v1: the app sometimes uses POST."""
        srv = ConfServer(DeviceRegistry(), listen="127.0.0.1", port=free_port())
        await srv.start()
        try:
            async with ClientSession() as s:
                async with s.post(f"http://127.0.0.1:{srv.port}{APP_LOGIN}",
                                  data={"account": "x"}) as r:
                    assert (await r.json())["code"] == "0000"
        finally:
            await srv.stop()


# ---------- TLS ----------


class TestConfServerTLS:
    async def test_login_over_tls_with_ensure_certs_ca(self, tmp_path):
        ensure_certs(str(tmp_path / "ca.crt"), str(tmp_path / "srv.crt"),
                     str(tmp_path / "srv.key"), cn="127.0.0.1")
        srv = ConfServer(DeviceRegistry(), listen="127.0.0.1", port=free_port(),
                         server_cert=str(tmp_path / "srv.crt"),
                         server_key=str(tmp_path / "srv.key"))
        await srv.start()
        try:
            import ssl

            ctx = ssl.create_default_context(cafile=str(tmp_path / "ca.crt"))
            async with ClientSession(
                    connector=TCPConnector(ssl=ctx)) as s:
                async with s.get(f"https://127.0.0.1:{srv.port}{APP_LOGIN}") as r:
                    assert (await r.json())["code"] == "0000"
        finally:
            await srv.stop()


# ---------- serve wiring ----------


class TestServeWiringFD:
    def _cfg(self, tmp_path, certs: bool = True) -> Config:
        cfg = Config()
        cfg.servers.conf = True
        cfg.servers.listen = "127.0.0.1"
        cfg.servers.conf_port = free_port()
        cfg.servers.announce_ip = "192.168.1.50"
        cfg.state_file = str(tmp_path / "state.json")
        cfg.servers.ca_file = str(tmp_path / "ca.crt")
        if certs:
            cfg.servers.server_cert = str(tmp_path / "srv.crt")
            cfg.servers.server_key = str(tmp_path / "srv.key")
        else:
            blocker = tmp_path / "blocker"
            blocker.write_text("not a directory")
            cfg.servers.server_cert = str(blocker / "srv.crt")
            cfg.servers.server_key = str(blocker / "srv.key")
        return cfg

    async def test_conf_only_starts_and_creates_the_registry(self, tmp_path):
        """conf=true (no xmpp/mqtt): :443 comes up and the registry exists
        (GetDeviceList needs it even without other servers)."""
        cfg = self._cfg(tmp_path)
        assert ensure_certs(cfg.servers.ca_file, cfg.servers.server_cert,
                            cfg.servers.server_key)
        svc = Service(cfg, None)
        await svc._start_embedded_servers()
        try:
            assert "conf" in svc.embedded
            assert svc.registry is not None
            conf = svc.embedded["conf"]
            assert conf.port == cfg.servers.conf_port
            # the registry persists next to state_file (same rule as FB)
            from ecovacs_bridge.servers.registry import DeviceRegistry

            assert isinstance(svc.registry, DeviceRegistry)
        finally:
            await svc._stop_embedded_servers()

    async def test_unrecoverable_certs_disable_conf_without_killing_serve(self, tmp_path):
        cfg = self._cfg(tmp_path, certs=False)
        svc = Service(cfg, None)
        await svc._start_embedded_servers()  # does not raise
        assert "conf" not in svc.embedded
        assert cfg.servers.conf is False

    async def test_busy_port_does_not_kill_serve(self, tmp_path):
        cfg = self._cfg(tmp_path)
        assert ensure_certs(cfg.servers.ca_file, cfg.servers.server_cert,
                            cfg.servers.server_key)
        squatter = ConfServer(DeviceRegistry(), listen="127.0.0.1",
                              port=cfg.servers.conf_port,
                              server_cert=cfg.servers.server_cert,
                              server_key=cfg.servers.server_key)
        await squatter.start()
        try:
            svc = Service(cfg, None)
            await svc._start_embedded_servers()  # OSError caught
            assert "conf" not in svc.embedded
        finally:
            await squatter.stop()
            await svc._stop_embedded_servers()

    def test_not_yet_empty(self):
        """FA–FD complete: no flag warns 'arrives in phase X'."""
        assert NOT_YET == {}


class TestReviewRegressionsFD:
    """Pin bugs found while reviewing FD."""

    def test_relogin_revokes_the_previous_authcode(self):
        from ecovacs_bridge.servers.conf import ConfAuth

        auth = ConfAuth()
        auth.login("devid-x")
        ac1 = auth.authcode("devid-x", "es")
        assert auth.check_authcode("fuid_tmpuser", ac1) is True
        auth.login("devid-x")  # re-login: the old session dies COMPLETELY
        assert auth.check_authcode("fuid_tmpuser", ac1) is False  # zombie revoked
        assert auth.authcode("devid-x", "es") != ac1

    def test_getauthcode_with_use_auth_requires_valid_token(self):
        from ecovacs_bridge.servers.conf import ConfAuth

        auth = ConfAuth(use_auth=True)
        s = auth.login("devid-x")
        # wrong accessToken → no authcode (fail like Bumper)
        assert auth.authcode("devid-x", "es", access_token="made_up",
                             require_token=True) is None
        # the session's token does
        assert auth.authcode("devid-x", "es", access_token=s.token,
                             require_token=True) is not None

    async def test_mqtt_broker_failure_does_not_skip_conf(self, tmp_path):
        """mqtt with a busy port + conf on → conf STILL STARTS."""
        from ecovacs_bridge.config import Config
        from ecovacs_bridge.serve import Service
        from ecovacs_bridge.servers.certs import ensure_certs
        from tests.test_servers_fc import _free_port

        cfg = Config()
        cfg.servers.listen = "127.0.0.1"
        cfg.servers.mqtt = True
        cfg.servers.conf = True
        cfg.servers.state_file = str(tmp_path / "s.json")
        cfg.state_file = str(tmp_path / "s.json")
        ensure_certs(str(tmp_path / "ca.crt"), str(tmp_path / "s.crt"),
                     str(tmp_path / "s.key"))
        cfg.servers.ca_file = str(tmp_path / "ca.crt")
        cfg.servers.server_cert = str(tmp_path / "s.crt")
        cfg.servers.server_key = str(tmp_path / "s.key")
        # occupy the MQTT broker's port
        import socket

        with socket.socket() as blk:
            blk.bind(("127.0.0.1", 0))
            cfg.servers.mqtt_port = blk.getsockname()[1]
            blk.listen(1)
            cfg.servers.conf_port = _free_port()
            svc = Service(cfg, None)
            await svc._start_embedded_servers()
            try:
                assert "mqtt" not in svc.embedded  # failed (busy port)
                assert "conf" in svc.embedded      # but conf DID start
            finally:
                await svc._stop_embedded_servers()
