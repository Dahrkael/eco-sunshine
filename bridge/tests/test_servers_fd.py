"""Tests de Fase FD: conf embebido (:443) — la API de la app Ecovacs.

Shapes copiadas de los plugins del confserver de Bumper y verificadas
contra el corpus (pairing-session.log, app antigua 1.4.9): login →
getAuthCode → user.do loginByItToken → user.do GetDeviceList.

- login acepta cualquier credencial (use_auth=False, como Bumper)
- GetDeviceList lista los bots del DeviceRegistry (XMPP y MQTT)
- loginByItToken canjea el authCode del getAuthCode
- TLS con los certs de ensure_certs (FE) y verificación completa
- wiring de serve: conf on/off, fallo de certs/puerto no tumba nada
"""

from __future__ import annotations

import re

import pytest
from aiohttp import ClientSession, TCPConnector

from deebot930_bridge.config import Config
from deebot930_bridge.serve import Service
from deebot930_bridge.servers import NOT_YET
from deebot930_bridge.servers.certs import ensure_certs
from deebot930_bridge.servers.conf import ConfAuth, ConfServer, login_data
from deebot930_bridge.servers.registry import DeviceRegistry

# path privado de la app antigua tal como sale en el corpus (apptype i_eco_e)
APP_LOGIN = ("/v1/private/es/en/5d8a10c413d47b4194c29ef1580cbb04/i_eco_e"
             "/1.4.9/c_googleplay/1/user/login")
APP_GET_AUTHCODE = APP_LOGIN.replace("/user/login", "/user/getAuthCode")

DID = "b11fceaf-1234-5678-9abc-def012345678"


def free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _registry_con_bot() -> DeviceRegistry:
    reg = DeviceRegistry()
    reg.bot_add("E00010571234567890ab", DID, "yna5xh", "axm", "eco-legacy")
    reg.bot_set_xmpp(DID, True)
    return reg


# ---------- sesiones (tokens/authcodes) ----------


class TestConfAuth:
    def test_login_minta_token_hex32_y_rota(self):
        auth = ConfAuth()
        s1 = auth.login("dev1")
        assert re.fullmatch(r"[0-9a-f]{32}", s1.token)
        assert auth.login("dev1").token != s1.token  # re-login = token nuevo

    def test_authcode_estable_por_sesion(self):
        auth = ConfAuth()
        auth.login("dev1")
        ac1 = auth.authcode("dev1", "es")
        assert re.fullmatch(r"es_[0-9a-f]{32}", ac1)
        assert auth.authcode("dev1", "es") == ac1  # cacheado como Bumper
        auth.login("dev1")  # re-login → authcode nuevo
        assert auth.authcode("dev1", "es") != ac1

    def test_authcode_sin_login_previo_crea_sesion(self):
        """Reinicio del bridge a media sesión: la app rehace el flujo igual."""
        auth = ConfAuth()
        assert re.fullmatch(r"us_[0-9a-f]{32}", auth.authcode("dev9", "us"))

    def test_check_authcode(self):
        auth = ConfAuth()
        auth.login("dev1")
        ac = auth.authcode("dev1", "es")
        assert auth.check_authcode("fuid_tmpuser", ac)
        assert auth.check_authcode("tmpuser", ac)  # con/sin fuid_ (como Bumper)
        assert not auth.check_authcode("fuid_otro", ac)
        assert not auth.check_authcode("fuid_tmpuser", "es_nunca_emitido")

    def test_logout_invalida_el_authcode(self):
        auth = ConfAuth()
        auth.login("dev1")
        ac = auth.authcode("dev1", "es")
        auth.logout("dev1")
        assert not auth.check_authcode("fuid_tmpuser", ac)


# ---------- shapes de login ----------


class TestLoginData:
    def test_app_antigua(self):
        d = login_data("i_eco_e", "es", "tok123")
        assert list(d) == ["accessToken", "uid", "username", "country", "email"]
        assert d == {"accessToken": "tok123", "uid": "fuid_tmpuser",
                     "username": "fusername_tmpuser", "country": "es",
                     "email": "null@null.com"}

    def test_ecovacs_home_los_extras_primero(self):
        d = login_data("global_a", "de", "tok123")
        assert list(d)[:3] == ["ucUid", "loginName", "mobile"]
        assert d["mobile"] is None and d["uid"] == "fuid_tmpuser"


# ---------- servidor (HTTP plano, como los tests FA/FC) ----------


class TestConfServer:
    async def test_flujo_completo_de_la_app(self):
        """login → getAuthCode → loginByItToken → GetDeviceList (corpus)."""
        srv = ConfServer(_registry_con_bot(), announce_ip="192.168.1.50",
                         xmpp_port=5223, listen="127.0.0.1", port=free_port())
        await srv.start()
        try:
            async with ClientSession() as s:
                base = f"http://127.0.0.1:{srv.port}"
                # 1. login (la app manda credenciales cifradas: ignoradas)
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

                # 2. getAuthCode (uid/accessToken en el query, como la app)
                async with s.get(f"{base}{APP_GET_AUTHCODE}", params={
                        "uid": "fuid_tmpuser",
                        "accessToken": login["data"]["accessToken"]}) as r:
                    ac = (await r.json())["data"]
                assert re.fullmatch(r"es_[0-9a-f]{32}", ac["authCode"])
                assert ac["ecovacsUid"] == "fuid_tmpuser"

                # 3. loginByItToken (form-urlencoded con el authCode)
                async with s.post(f"{base}/api/users/user.do", data={
                        "todo": "loginByItToken", "country": "ES",
                        "resource": "be9baaf0", "org": "ECOWW",
                        "realm": "ecouser.net", "userId": "fuid_tmpuser",
                        "token": ac["authCode"]}) as r:
                    body = await r.json()
                assert body == {"resource": "be9baaf0", "result": "ok",
                                "todo": "result", "token": ac["authCode"],
                                "userId": "fuid_tmpuser"}

                # 4. GetDeviceList (json) — los bots del registry
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

    async def test_login_by_it_token_rechazado_con_use_auth(self):
        """use_auth=True: authcode desconocido → body vacío (como Bumper)."""
        reg = DeviceRegistry(use_auth=True)
        srv = ConfServer(reg, listen="127.0.0.1", port=free_port())
        await srv.start()
        try:
            async with ClientSession() as s:
                async with s.post(
                        f"http://127.0.0.1:{srv.port}/api/users/user.do",
                        data={"todo": "loginByItToken",
                              "userId": "fuid_tmpuser",
                              "token": "es_inventado"}) as r:
                    assert await r.json() == {}
        finally:
            await srv.stop()

    async def test_gestion_de_bots_por_la_app(self):
        reg = _registry_con_bot()
        srv = ConfServer(reg, listen="127.0.0.1", port=free_port())
        await srv.start()
        try:
            async with ClientSession() as s:
                base = f"http://127.0.0.1:{srv.port}"
                async with s.post(f"{base}/api/users/user.do",
                                  json={"todo": "SetDeviceNick", "did": DID,
                                        "nick": "Salo"}) as r:
                    assert (await r.json())["result"] == "ok"
                assert reg.bot_get(DID)["nick"] == "Salo"
                async with s.post(f"{base}/api/users/user.do",
                                  json={"todo": "DeleteOneDevice",
                                        "did": DID}) as r:
                    assert (await r.json())["result"] == "ok"
                assert reg.bot_get(DID) is None
        finally:
            await srv.stop()

    async def test_estaticos_de_cortesia(self):
        """Endpoints que la app sondea y Bumper contestaba con estáticos."""
        srv = ConfServer(_registry_con_bot(), listen="127.0.0.1",
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

    async def test_base_del_confserver(self):
        """lookup.do/newauth.do también viven en el :443 (como Bumper)."""
        srv = ConfServer(DeviceRegistry(), announce_ip="10.1.2.3",
                         xmpp_port=5223, listen="127.0.0.1", port=free_port())
        await srv.start()
        try:
            async with ClientSession() as s:
                base = f"http://127.0.0.1:{srv.port}"
                async with s.post(f"{base}/lookup.do", json={
                        "todo": "FindBest", "service": "EcoMsgNew"}) as r:
                    text = await r.text()
                assert " " not in text  # JSON compacto para el firmware
                import json

                assert json.loads(text) == {"ip": "10.1.2.3", "port": 5223,
                                            "result": "ok"}
                async with s.post(f"{base}/newauth.do", data={
                        "itToken": "es_abc", "todo": "newAuth"}) as r:
                    assert await r.json() == {"authCode": "es_abc",
                                              "result": "ok",
                                              "todo": "result"}
                # GET del portal → fail (como Bumper)
                async with s.get(f"{base}/api/users/user.do") as r:
                    assert (await r.json())["result"] == "fail"
        finally:
            await srv.stop()

    async def test_get_authcode_global(self):
        """/v1/global/auth/getAuthCode (deviceId en el query, sin path)."""
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

    async def test_post_en_login_tambien_valida(self):
        """web.route('*') en la v1: la app a veces usa POST."""
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
    async def test_login_por_tls_con_ca_de_ensure_certs(self, tmp_path):
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


# ---------- wiring de serve ----------


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
            blocker = tmp_path / "bloqueo"
            blocker.write_text("no soy un directorio")
            cfg.servers.server_cert = str(blocker / "srv.crt")
            cfg.servers.server_key = str(blocker / "srv.key")
        return cfg

    async def test_conf_solo_arranca_y_crea_el_registry(self, tmp_path):
        """conf=true (sin xmpp/mqtt): el :443 sube y el registry existe
        (GetDeviceList lo necesita aunque no haya otros servidores)."""
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
            # el registry persiste junto al state_file (misma regla que FB)
            from deebot930_bridge.servers.registry import DeviceRegistry

            assert isinstance(svc.registry, DeviceRegistry)
        finally:
            await svc._stop_embedded_servers()

    async def test_certs_irrecuperables_desactivan_conf_sin_tirar_serve(self, tmp_path):
        cfg = self._cfg(tmp_path, certs=False)
        svc = Service(cfg, None)
        await svc._start_embedded_servers()  # no lanza
        assert "conf" not in svc.embedded
        assert cfg.servers.conf is False

    async def test_puerto_ocupado_no_tumba_serve(self, tmp_path):
        cfg = self._cfg(tmp_path)
        assert ensure_certs(cfg.servers.ca_file, cfg.servers.server_cert,
                            cfg.servers.server_key)
        ocupante = ConfServer(DeviceRegistry(), listen="127.0.0.1",
                              port=cfg.servers.conf_port,
                              server_cert=cfg.servers.server_cert,
                              server_key=cfg.servers.server_key)
        await ocupante.start()
        try:
            svc = Service(cfg, None)
            await svc._start_embedded_servers()  # OSError capturada
            assert "conf" not in svc.embedded
        finally:
            await ocupante.stop()
            await svc._stop_embedded_servers()

    def test_not_yet_vacio(self):
        """FA–FD completas: ningún flag avisa 'llega en la fase X'."""
        assert NOT_YET == {}
