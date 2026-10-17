"""Embedded configuration server — the Ecovacs app API (Phase FD).

Bumper's "confserver" (:443, TLS): what the **old "Ecovacs" app** (XMPP
robots, apptype ``i_eco_e``) needs to log in, list its robots and connect
over XMPP. Flow verified against the corpus (``pairing-session.log``,
app 1.4.9 against Bumper):

    1. GET  /v1/private/{country}/{lang}/{devid}/{apptype}/.../user/login
       → code 0000 + accessToken (any credentials: use_auth=False, like
         Bumper by default — no accounts to validate on a LAN)
    2. GET  .../user/getAuthCode?uid=…&accessToken=…
       → authCode "{country}_{uuid}" (what XMPP expects in SASL)
    3. POST /api/users/user.do  (form)  todo=loginByItToken&token={authCode}
       → result ok (the "portal" accepts step 2's authcode)
    4. POST /api/users/user.do  (json)  todo=GetDeviceList
       → devices = the DeviceRegistry's bots (XMPP+MQTT), exact
         VacBotDevice shape from Bumper
    5. SASL XMPP against the embedded :5223 (FB already accepts it)

It also mounts ``/lookup.do`` and ``/newauth.do`` like Bumper's
confserver (firmware asking on 443 finds the same as on :8007), plus the
courtesy endpoints the app probes and Bumper answered with statics:
``checkAgreement``, ``checkVersion``, ``homePageAlert``.

**Modern app (Ecovacs Home)**: login with apptype ``global_`` answers
the EcoVacsHome_Login shape (comes free), but the rest of its flow signs
requests with HMAC (``X-ECO-SIGN``) and is not supported — see
docs/absorcion-bumper.md (FD).

Default port :443 (``servers.conf_port``), TLS with ``ServersConfig``
certs (``ensure_certs`` from FE). OFF by default: without
``servers.conf = true`` the bridge does not open the port.
"""

from __future__ import annotations

import datetime
import json
import logging
import ssl
import uuid
from dataclasses import dataclass

from aiohttp import web

from .lookup import lookup_response, parse_lookup_request
from .registry import DeviceRegistry

log = logging.getLogger(__name__)

RETURN_API_SUCCESS = "0000"
MSG_OK = "操作成功"  # "operation successful" — the app expects the Chinese literal

# Account-less model (Bumper's use_auth=False): EVERY login is the same
# virtual user. The corpus confirms it (uid fuid_tmpuser) and it avoids
# managing real accounts on a LAN.
DEFAULT_UID = "fuid_tmpuser"
DEFAULT_USERNAME = "fusername_tmpuser"

# private v1 API prefix (7 device-context segments, identical across
# Bumper's plugins)
V1_PRIVATE = ("/v1/private/{country}/{language}/{devid}/{apptype}"
              "/{appversion}/{devtype}/{aid}")


def _milli() -> int:
    return round(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)


def _ok(data: object, **extra: object) -> web.Response:
    """Common v1 API envelope: code/msg/time (plus occasional extras)."""
    body: dict = {"code": RETURN_API_SUCCESS, "data": data, "msg": MSG_OK,
                  "time": _milli()}
    body.update(extra)
    return web.json_response(body)


# ---------- app sessions (tokens/authcodes) ----------


@dataclass
class AppSession:
    """One app login: accessToken (login) → authCode (getAuthCode)."""

    token: str
    authcode: str | None = None


class ConfAuth:
    """Tokens and authcodes for the account-less model — the role of
    bumper.db's ``tokens`` table.

    Volatile on purpose: the app does login → getAuthCode →
    loginByItToken in the same burst of requests, so memory is enough
    (if the bridge restarts mid-session the app just logs in again on
    startup — its normal behaviour). The embedded XMPP (FB) accepts the
    app's SASL with ``use_auth=False``, so the authcode is only
    validated here.
    """

    def __init__(self, use_auth: bool = False) -> None:
        self.use_auth = use_auth
        self._by_devid: dict[str, AppSession] = {}
        self._by_authcode: dict[str, str] = {}  # authcode → devid

    def login(self, devid: str) -> AppSession:
        """(Re)login of a device: fresh token, clean authcode.

        The outgoing session's authcode is REVOKED (as Bumper did on
        login) — without this, re-logins would leave valid authcodes in
        _by_authcode forever (leak + zombie authcode).
        """
        old = self._by_devid.get(devid)
        if old is not None and old.authcode:
            self._by_authcode.pop(old.authcode, None)
        session = AppSession(token=uuid.uuid4().hex)
        self._by_devid[devid] = session
        return session

    def logout(self, devid: str) -> None:
        session = self._by_devid.pop(devid, None)
        if session and session.authcode:
            self._by_authcode.pop(session.authcode, None)

    def authcode(self, devid: str, country: str,
                 access_token: str | None = None,
                 require_token: bool = False) -> str | None:
        """The device's AuthCode, generating it on first use.

        Bumper cached it in the tokens table (same authcode while the
        token lived); same here: stable per login session.
        Without a prior login (e.g. bridge restart mid-session) the
        session is created on the fly — consistent with use_auth=False.

        With require_token (use_auth=True), the query's accessToken must
        match the session's: otherwise getAuthCode would emit valid
        authcodes without validating anything (the auth chain would be
        theatre).
        """
        session = self._by_devid.get(devid)
        if session is None:
            session = self.login(devid)
        if require_token and access_token != session.token:
            return None
        if session.authcode is None:
            session.authcode = f"{country}_{uuid.uuid4().hex}"
            self._by_authcode[session.authcode] = devid
        return session.authcode

    def check_authcode(self, uid: str, authcode: str) -> bool:
        """Was the authcode issued by US and is the uid the user?

        Like Bumper: the uid matches with or without the ``fuid_``
        prefix (the app sends "fuid_tmpuser" and the table stored
        "tmpuser").
        """
        if authcode not in self._by_authcode:
            return False
        return uid.replace("fuid_", "", 1) == DEFAULT_UID.replace("fuid_", "", 1)


def login_data(apptype: str, country: str, token: str) -> dict:
    """login data: Bumper's EcoVacs_Login / EcoVacsHome_Login shape.

    Exact fields in the same order ``toJSON()`` serialized (order does
    not matter to the app, but this keeps the diff vs Bumper null).
    """
    data: dict[str, object] = {}
    if "global_" in apptype:  # Ecovacs Home
        data["ucUid"] = DEFAULT_UID
        data["loginName"] = DEFAULT_USERNAME
        data["mobile"] = None
    data.update({
        "accessToken": token,
        "uid": DEFAULT_UID,
        "username": DEFAULT_USERNAME,
        "country": country,
        "email": "null@null.com",
    })
    return data


# ---------- server ----------


class ConfServer:
    """The app API on :conf_port (443) — aiohttp over TLS.

    - ``registry``: where GetDeviceList's devices come from (the bots
      registering via XMPP/MQTT — FD's key integration)
    - no certs → plain (tests only); in serve, ``ServersConfig``'s
      always arrive
    """

    def __init__(self, registry: DeviceRegistry,
                 announce_ip: str = "", xmpp_port: int = 5223,
                 listen: str = "0.0.0.0", port: int = 443,
                 server_cert: str | None = None,
                 server_key: str | None = None) -> None:
        self.registry = registry
        self.announce_ip = announce_ip
        self.xmpp_port = xmpp_port
        self.listen = listen
        self.port = port
        self.server_cert = server_cert
        self.server_key = server_key
        self.auth = ConfAuth(use_auth=registry.use_auth)
        self._runner: web.AppRunner | None = None

    # ---------- lifecycle ----------

    def _build_app(self) -> web.Application:
        app = web.Application()
        r = app.router
        # Bumper confserver base
        r.add_post("/lookup.do", self.handle_lookup)
        r.add_post("/newauth.do", self.handle_newauth)
        # private v1 API (old app; global_ = Ecovacs Home)
        for tail, handler in (
            ("/user/login", self.handle_login),
            ("/user/directRegister", self.handle_login),
            ("/user/checkLogin", self.handle_login),
            ("/user/getAuthCode", self.handle_get_authcode),
            ("/user/logout", self.handle_logout),
            ("/user/checkAgreement", self.handle_check_agreement),
            ("/user/checkAgreementBatch", self.handle_check_agreement),
            ("/common/checkVersion", self.handle_check_version),
            ("/campaign/homePageAlert", self.handle_home_page_alert),
        ):
            r.add_route("*", f"{V1_PRIVATE}{tail}", handler)
        # global auth (no path params — deviceId/uid go in the query)
        r.add_route("*", "/v1/global/auth/getAuthCode", self.handle_get_authcode)
        # portal (loginByItToken / device list)
        r.add_route("*", "/api/users/user.do", self.handle_user_do)
        return app

    async def start(self) -> None:
        if not self.announce_ip:
            log.warning("empty announce_ip: :443's /lookup.do will not be "
                        "able to announce the XMPP to the robot")
        self._runner = web.AppRunner(self._build_app(), access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.listen, self.port,
                           ssl_context=self._ssl_ctx())
        await site.start()
        log.info("embedded conf at https://%s:%d (Ecovacs app "
                 "login/getAuthCode/user.do)", self.listen, self.port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    def _ssl_ctx(self) -> ssl.SSLContext | None:
        if not self.server_cert:
            return None  # plain: tests
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.load_cert_chain(self.server_cert, self.server_key)
        return ctx

    # ---------- base (Bumper confserver) ----------

    async def handle_lookup(self, request: web.Request) -> web.Response:
        body = await parse_lookup_request(request)
        return lookup_response(str(body.get("service", "")),
                               self.announce_ip, self.xmpp_port)

    async def handle_newauth(self, request: web.Request) -> web.Response:
        """Robot's EcoMsgNew auth: echoes the received token (Bumper ditto)."""
        body = await parse_lookup_request(request)
        return web.json_response({
            "authCode": body.get("itToken", ""),
            "result": "ok",
            "todo": "result",
        })

    # ---------- private v1 API ----------

    async def handle_login(self, request: web.Request) -> web.Response:
        """login / directRegister / checkLogin — same handler in Bumper.

        use_auth=False: any credentials will do (account/password travel
        encrypted and are ignored); a fresh accessToken is issued.
        """
        devid = request.match_info.get("devid", "") or "dummy"
        country = request.match_info.get("country", "us")
        apptype = request.match_info.get("apptype", "")
        session = self.auth.login(devid)
        log.info("app login: devid=%s apptype=%s country=%s (%s)",
                 devid, apptype, country, request.path.rsplit("/", 1)[-1])
        return _ok(login_data(apptype, country, session.token))

    async def handle_get_authcode(self, request: web.Request) -> web.Response:
        """authCode for XMPP's SASL (and the portal's loginByItToken).

        The old app sends the devid in the path and uid/accessToken in
        the query; the global one (Ecovacs Home) uses ``deviceId`` in
        the query. With use_auth=True the query's accessToken must match
        the session (otherwise: fail, like Bumper which validated the
        token).
        """
        apptype = request.match_info.get("apptype", "")
        devid = (request.match_info.get("devid", "")
                 or request.query.get("deviceId", "") or "dummy")
        country = request.match_info.get("country", "us")
        uid = request.query.get("uid", DEFAULT_UID)
        authcode = self.auth.authcode(devid, country,
                                      access_token=request.query.get("accessToken"),
                                      require_token=self.auth.use_auth)
        if authcode is None:
            log.warning("getAuthCode rejected (invalid accessToken) "
                        "devid=%s uid=%s", devid, uid)
            return web.json_response({"time": _milli(), "data": None,
                                      "msg": "fail", "code": "0002"})
        log.info("getAuthCode: devid=%s uid=%s", devid, uid)
        return _ok({"authCode": authcode, "ecovacsUid": uid},
                   **({"success": True} if "global" in apptype else {}))

    async def handle_logout(self, request: web.Request) -> web.Response:
        devid = request.match_info.get("devid", "") or "dummy"
        self.auth.logout(devid)
        return _ok(None)

    async def handle_check_agreement(self, request: web.Request) -> web.Response:
        """No pending agreements (empty data = the app stays quiet)."""
        return _ok([])

    async def handle_check_version(self, request: web.Request) -> web.Response:
        """"No update" — v1_private_common plugin shape."""
        return _ok({"c": None, "img": None, "r": 0, "t": None,
                    "u": None, "ut": 0, "v": None})

    async def handle_home_page_alert(self, request: web.Request) -> web.Response:
        """No campaign (the app probes it constantly)."""
        next_alert = round((datetime.datetime.now(datetime.timezone.utc)
                            + datetime.timedelta(hours=12)).timestamp() * 1000)
        return _ok({"clickSchemeUrl": None, "clickWebUrl": None,
                    "hasCampaign": "N", "imageUrl": None,
                    "nextAlertTime": next_alert, "serverTime": _milli()})

    # ---------- portal (user.do) ----------

    async def handle_user_do(self, request: web.Request) -> web.Response:
        """The old app's portal: loginByItToken + bot management.

        Two encodings (Bumper told them apart the same way):
        form-urlencoded for loginByItToken, JSON for the rest.
        """
        if request.method == "GET":
            return web.json_response({"result": "fail", "todo": "result"})
        if request.content_type == "application/x-www-form-urlencoded":
            try:
                post: dict = dict(await request.post())
            except Exception:  # noqa: BLE001  (body broken at transport level)
                return web.json_response({"result": "fail", "todo": "result"})
        else:
            try:
                post = json.loads(await request.text())
            except (json.JSONDecodeError, UnicodeDecodeError):
                post = {}

        todo = str(post.get("todo", ""))
        if todo == "FindBest":  # lookup through the portal (old variant)
            return lookup_response(str(post.get("service", "")),
                                   self.announce_ip, self.xmpp_port)
        if todo == "loginByItToken":
            return self._login_by_it_token(post)
        if todo == "GetDeviceList":
            return web.json_response({"devices": self._devices(),
                                      "result": "ok", "todo": "result"})
        if todo in ("SetDeviceNick", "AddOneDevice"):
            self.registry.bot_set_nick(str(post.get("did", "")),
                                       str(post.get("nick", "")))
            return web.json_response({"result": "ok", "todo": "result"})
        if todo == "DeleteOneDevice":
            self.registry.bot_remove(str(post.get("did", "")))
            return web.json_response({"result": "ok", "todo": "result"})
        # unknown: Bumper replied with an empty body {}
        return web.json_response({})

    def _login_by_it_token(self, post: dict) -> web.Response:
        """loginByItToken: exchanges getAuthCode's authCode for a session.

        With ``use_auth=False`` it is accepted just like FB/FC (the
        embedded XMPP and MQTT) do; with accounts enabled some day, it
        truly validates.
        """
        accepted = (not self.registry.use_auth
                    or self.auth.check_authcode(str(post.get("userId", "")),
                                                str(post.get("token", ""))))
        if not accepted:
            log.warning("loginByItToken rejected (unknown authcode)")
            return web.json_response({})
        if "userId" not in post:  # Ecovacs Home variant (no userId)
            post["userId"] = DEFAULT_UID
        return web.json_response({
            "resource": post.get("resource", ""),
            "result": "ok",
            "todo": "result",
            "token": post.get("token", ""),
            "userId": post.get("userId", ""),
        })

    def _devices(self) -> list[dict]:
        """Registry bots in Bumper's VacBotDevice shape (GetDeviceList)."""
        return [{
            "class": bot.device_class,
            "company": bot.company,
            "did": bot.did,
            "name": bot.sn,
            "nick": bot.nick,
            "resource": bot.resource,
            "mqtt_connection": bot.mqtt,
            "xmpp_connection": bot.xmpp,
        } for bot in self.registry.bots.values()]
