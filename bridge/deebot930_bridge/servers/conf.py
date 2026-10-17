"""Servidor de configuración embebido — la API de la app Ecovacs (Fase FD).

El "confserver" de Bumper (:443, TLS): lo que la **app antigua "Ecovacs"**
(robots XMPP, apptype ``i_eco_e``) necesita para loguearse, listar sus
robots y conectar por XMPP. Flujo verificado contra el corpus
(``pairing-session.log``, app 1.4.9 contra Bumper):

    1. GET  /v1/private/{country}/{lang}/{devid}/{apptype}/.../user/login
       → code 0000 + accessToken (cualquier credencial: use_auth=False,
         como Bumper por defecto — en LAN no hay cuentas que validar)
    2. GET  .../user/getAuthCode?uid=…&accessToken=…
       → authCode "{country}_{uuid}" (lo que el XMPP espera en el SASL)
    3. POST /api/users/user.do  (form)  todo=loginByItToken&token={authCode}
       → result ok (el "portal" acepta el authcode del paso 2)
    4. POST /api/users/user.do  (json)  todo=GetDeviceList
       → devices = los bots del DeviceRegistry (XMPP+MQTT), shape exacto
         del VacBotDevice de Bumper
    5. SASL XMPP contra el :5223 embebido (FB ya lo acepta)

También monta ``/lookup.do`` y ``/newauth.do`` como el confserver de
Bumper (el firmware que pregunta por el 443 encuentra lo mismo que en el
:8007), más los endpoints "de cortesía" que la app sondea y Bumper
contestaba con estáticos: ``checkAgreement``, ``checkVersion``,
``homePageAlert``.

**App moderna (Ecovacs Home)**: login con apptype ``global_`` responde la
shape EcoVacsHome_Login (sale gratis), pero el resto de su flujo firma
requests con HMAC (``X-ECO-SIGN``) y no está soportado — ver
docs/absorcion-bumper.md (FD).

Puerto por defecto :443 (``servers.conf_port``), TLS con los certs de
``ServersConfig`` (``ensure_certs`` de FE). OFF por defecto: sin
``servers.conf = true`` el bridge no abre el puerto.
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
MSG_OK = "操作成功"  # "operación con éxito" — la app espera el literal chino

# Modelo sin cuentas (use_auth=False de Bumper): TODOS los logins son el
# mismo usuario virtual. El corpus lo confirma (uid fuid_tmpuser) y evita
# gestionar cuentas reales en LAN.
DEFAULT_UID = "fuid_tmpuser"
DEFAULT_USERNAME = "fusername_tmpuser"

# prefijo de la API privada v1 (7 segmentos de contexto del dispositivo,
# iguales en todos los plugins de Bumper)
V1_PRIVATE = ("/v1/private/{country}/{language}/{devid}/{apptype}"
              "/{appversion}/{devtype}/{aid}")


def _milli() -> int:
    return round(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)


def _ok(data: object, **extra: object) -> web.Response:
    """Envelope común de la API v1: code/msg/time (y extras puntuales)."""
    body: dict = {"code": RETURN_API_SUCCESS, "data": data, "msg": MSG_OK,
                  "time": _milli()}
    body.update(extra)
    return web.json_response(body)


# ---------- sesiones de la app (tokens/authcodes) ----------


@dataclass
class AppSession:
    """Un login de app: accessToken (login) → authCode (getAuthCode)."""

    token: str
    authcode: str | None = None


class ConfAuth:
    """Tokens y authcodes del modelo sin cuentas — el papel de la tabla
    ``tokens`` de bumper.db.

    Volátil a propósito: la app hace login → getAuthCode → loginByItToken
    en la misma tanda de requests, así que en memoria sobra (si el bridge
    reinicia a media sesión, la app rehace el login al arrancar — es su
    comportamiento normal). El XMPP embebido (FB) acepta el SASL de la app
    con ``use_auth=False``, así que el authcode solo se valida aquí.
    """

    def __init__(self) -> None:
        self._by_devid: dict[str, AppSession] = {}
        self._by_authcode: dict[str, str] = {}  # authcode → devid

    def login(self, devid: str) -> AppSession:
        """(Re)login de un dispositivo: token nuevo, authcode limpio."""
        session = AppSession(token=uuid.uuid4().hex)
        self._by_devid[devid] = session
        return session

    def logout(self, devid: str) -> None:
        session = self._by_devid.pop(devid, None)
        if session and session.authcode:
            self._by_authcode.pop(session.authcode, None)

    def authcode(self, devid: str, country: str) -> str:
        """AuthCode del dispositivo, generándolo si es la primera vez.

        Bumper lo cacheaba en la tabla tokens (mismo authcode mientras
        viviera el token); aquí igual: estable por sesión de login.
        Sin login previo (p.ej. reinicio del bridge a media sesión) se
        crea la sesión al vuelo — coherente con use_auth=False.
        """
        session = self._by_devid.get(devid)
        if session is None:
            session = self.login(devid)
        if session.authcode is None:
            session.authcode = f"{country}_{uuid.uuid4().hex}"
            self._by_authcode[session.authcode] = devid
        return session.authcode

    def check_authcode(self, uid: str, authcode: str) -> bool:
        """¿Es un authcode emitido por NOSOTROS y el uid es el usuario?

        Como Bumper: el uid casa con o sin prefijo ``fuid_`` (la app manda
        "fuid_tmpuser" y la tabla guardaba "tmpuser").
        """
        if authcode not in self._by_authcode:
            return False
        return uid.replace("fuid_", "", 1) == DEFAULT_UID.replace("fuid_", "", 1)


def login_data(apptype: str, country: str, token: str) -> dict:
    """data del login: shape EcoVacs_Login / EcoVacsHome_Login de Bumper.

    Campos exactos y en el mismo orden que serializaba ``toJSON()`` (el
    orden no importa para la app, pero así el diff con Bumper es nulo).
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


# ---------- servidor ----------


class ConfServer:
    """API de la app en :conf_port (443) — aiohttp sobre TLS.

    - ``registry``: de dónde salen los devices de GetDeviceList (los bots
      que se registran por XMPP/MQTT — la integración clave de FD)
    - sin certs → plano (solo tests); en serve siempre llegan los de
      ``ServersConfig``
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
        self.auth = ConfAuth()
        self._runner: web.AppRunner | None = None

    # ---------- ciclo de vida ----------

    def _build_app(self) -> web.Application:
        app = web.Application()
        r = app.router
        # base del confserver de Bumper
        r.add_post("/lookup.do", self.handle_lookup)
        r.add_post("/newauth.do", self.handle_newauth)
        # API v1 privada (app antigua; global_ = Ecovacs Home)
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
        # auth global (sin path params — deviceId/uid van en el query)
        r.add_route("*", "/v1/global/auth/getAuthCode", self.handle_get_authcode)
        # portal (loginByItToken / lista de dispositivos)
        r.add_route("*", "/api/users/user.do", self.handle_user_do)
        return app

    async def start(self) -> None:
        if not self.announce_ip:
            log.warning("announce_ip vacía: /lookup.do del :443 no podrá "
                        "anunciar el XMPP al robot")
        self._runner = web.AppRunner(self._build_app(), access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.listen, self.port,
                           ssl_context=self._ssl_ctx())
        await site.start()
        log.info("conf embebido en https://%s:%d (login/getAuthCode/user.do "
                 "de la app Ecovacs)", self.listen, self.port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    def _ssl_ctx(self) -> ssl.SSLContext | None:
        if not self.server_cert:
            return None  # plano: tests
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.load_cert_chain(self.server_cert, self.server_key)
        return ctx

    # ---------- base (confserver de Bumper) ----------

    async def handle_lookup(self, request: web.Request) -> web.Response:
        body = await parse_lookup_request(request)
        return lookup_response(str(body.get("service", "")),
                               self.announce_ip, self.xmpp_port)

    async def handle_newauth(self, request: web.Request) -> web.Response:
        """EcoMsgNew auth del robot: devuelve el token recibido (Bumper ídem)."""
        body = await parse_lookup_request(request)
        return web.json_response({
            "authCode": body.get("itToken", ""),
            "result": "ok",
            "todo": "result",
        })

    # ---------- API v1 privada ----------

    async def handle_login(self, request: web.Request) -> web.Response:
        """login / directRegister / checkLogin — mismo handler en Bumper.

        use_auth=False: cualquier credencial vale (account/password viajan
        cifradas y se ignoran); se emite un accessToken fresco.
        """
        devid = request.match_info.get("devid", "") or "dummy"
        country = request.match_info.get("country", "us")
        apptype = request.match_info.get("apptype", "")
        session = self.auth.login(devid)
        log.info("login de app: devid=%s apptype=%s country=%s (%s)",
                 devid, apptype, country, request.path.rsplit("/", 1)[-1])
        return _ok(login_data(apptype, country, session.token))

    async def handle_get_authcode(self, request: web.Request) -> web.Response:
        """authCode para el SASL del XMPP (y el loginByItToken del portal).

        La app antigua manda el devid en el path y uid/accessToken en el
        query; la global (Ecovacs Home) usa ``deviceId`` en el query.
        """
        apptype = request.match_info.get("apptype", "")
        devid = (request.match_info.get("devid", "")
                 or request.query.get("deviceId", "") or "dummy")
        country = request.match_info.get("country", "us")
        uid = request.query.get("uid", DEFAULT_UID)
        authcode = self.auth.authcode(devid, country)
        log.info("getAuthCode: devid=%s uid=%s", devid, uid)
        return _ok({"authCode": authcode, "ecovacsUid": uid},
                   **({"success": True} if "global" in apptype else {}))

    async def handle_logout(self, request: web.Request) -> web.Response:
        devid = request.match_info.get("devid", "") or "dummy"
        self.auth.logout(devid)
        return _ok(None)

    async def handle_check_agreement(self, request: web.Request) -> web.Response:
        """Sin acuerdos pendientes (data vacía = la app no molesta)."""
        return _ok([])

    async def handle_check_version(self, request: web.Request) -> web.Response:
        """"Sin actualización" — shape del plugin v1_private_common."""
        return _ok({"c": None, "img": None, "r": 0, "t": None,
                    "u": None, "ut": 0, "v": None})

    async def handle_home_page_alert(self, request: web.Request) -> web.Response:
        """Sin campaña (la app lo sondea constantemente)."""
        next_alert = round((datetime.datetime.now(datetime.timezone.utc)
                            + datetime.timedelta(hours=12)).timestamp() * 1000)
        return _ok({"clickSchemeUrl": None, "clickWebUrl": None,
                    "hasCampaign": "N", "imageUrl": None,
                    "nextAlertTime": next_alert, "serverTime": _milli()})

    # ---------- portal (user.do) ----------

    async def handle_user_do(self, request: web.Request) -> web.Response:
        """El portal de la app antigua: loginByItToken + gestión de bots.

        Dos codificaciones (Bumper las distinguía igual): form-urlencoded
        para loginByItToken, JSON para el resto.
        """
        if request.method == "GET":
            return web.json_response({"result": "fail", "todo": "result"})
        if request.content_type == "application/x-www-form-urlencoded":
            post: dict = dict(await request.post())
        else:
            try:
                post = json.loads(await request.text())
            except (json.JSONDecodeError, UnicodeDecodeError):
                post = {}

        todo = str(post.get("todo", ""))
        if todo == "FindBest":  # lookup por el portal (variante antigua)
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
        # desconocido: Bumper devolvía el body vacío {}
        return web.json_response({})

    def _login_by_it_token(self, post: dict) -> web.Response:
        """loginByItToken: canjea el authCode del getAuthCode por sesión.

        Con ``use_auth=False`` se acepta igual que hacen FB/FC (XMPP y
        MQTT embebidos); con cuentas activadas algún día, valida de verdad.
        """
        accepted = (not self.registry.use_auth
                    or self.auth.check_authcode(str(post.get("userId", "")),
                                                str(post.get("token", ""))))
        if not accepted:
            log.warning("loginByItToken rechazado (authcode desconocido)")
            return web.json_response({})
        if "userId" not in post:  # variante Ecovacs Home (sin userId)
            post["userId"] = DEFAULT_UID
        return web.json_response({
            "resource": post.get("resource", ""),
            "result": "ok",
            "todo": "result",
            "token": post.get("token", ""),
            "userId": post.get("userId", ""),
        })

    def _devices(self) -> list[dict]:
        """Bots del registry en la shape VacBotDevice de Bumper (GetDeviceList)."""
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
