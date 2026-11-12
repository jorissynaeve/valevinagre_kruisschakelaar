"""Een nagebootst Brink Home: de IdentityServer van `/idsrv` (inlogformulier met anti-forgery token en cookie, code-flow met PKCE, doorverwijzing naar de app met `code`) en de API
`/portal/api/v1.1/` (de systemen, `uidescription`, `parameter-values`). Gebouwd naar wat de echte site toont (de openbare inlogpagina en de antwoorden van een ingelogde sessie van
een Flair 400), met andere ids en zonder gegevens van iemand.

Wat de echte site doet en de add-on moet verdragen: een inlogpoging met een onjuist wachtwoord geeft de inlogpagina terug (HTTP 200, geen doorverwijzing), het toegangstoken
verloopt, een opdracht is asynchroon (de waarde verandert pas later), en parameters hebben een `valueState`.
"""
import asyncio
import base64
import hashlib
import html
import json
import secrets
import time
from urllib.parse import parse_qs, quote, urlparse

from aiohttp import web

EMAIL, PASSWORD = "jan@example.org", "geheim-wachtwoord"
SYSTEM, FLAIR, WIFI = 9001, 9002, 9003
APP = "https://www.brink-home.com/app/"
BASE_ID = 5000


def flair_params() -> dict[str, dict]:
    """De 20 parameters van een Flair 400 zoals de echte beschrijving ze noemt (namen en keuzes), met eigen ids. valueState 0 is goed."""
    def p(i, name, rw, ct, value, items=None, state=0, **kw):
        d = {"valueId": BASE_ID + i, "name": name, "readWrite": rw, "controlType": ct, "value": value, "valueState": state, **kw}
        if items:
            d["listItems"] = [{"value": str(k), "translationId": v} for k, v in items.items()]
        return d
    ps = [
        p(1, "deviceTypeTitle", 1, 9, "Flair 400"), p(2, "softwareLabel", 1, 9, "S2.01.21"),
        p(3, "Status Filtermeldung", 1, 0, "0", {0: "Not dirty", 1: "Dirty"}), p(4, "Anzahl der Tage seit Filterreset", 1, 6, "181"),
        p(5, "Aktive Regelung", 1, 0, "13", {0: "Standby", 13: "Portal"}),
        p(6, "Lüftungsstufe", 3, 0, "1", {0: "Stufe 0", 1: "Stufe 1", 2: "Stufe 2", 3: "Stufe 3"}),
        p(7, "Betriebsart", 3, 0, "1", {0: "Automatikbetrieb", 1: "Handbetrieb"}),
        p(8, "Restlaufzeit Betriebsartfunktion", 1, 6, "0", unit="Min.", minValue=0, maxValue=480),
        p(9, "Funktion der Bypass Klappe", 3, 0, "0", {0: "Funktion", 1: "geschlossen", 2: "geöffnet"}),
        p(10, "Ist-Wert Luftdurchsatz Zuluft", 1, 6, "100", unit="m³/h"), p(11, "Ist-Wert Luftdurchsatz Abluft", 1, 6, "96", unit="m³/h"),
        p(12, "Frischlufttemperatur", 1, 6, "8.8", unit="°C"), p(13, "Zulufttemperatur", 1, 6, "20.5", unit="°C"),
        p(14, "Status Bypassklappe", 1, 0, "4", {0: "Initialisierung", 4: "Geschlossen"}), p(15, "Status Vorheizregister", 1, 0, "0", {0: "Off", 1: "Auto"}),
        p(16, "Relative Feuchte", 1, 6, "0.0", unit="%"),
        p(17, "PPM eBus CO2-sensor 1", 1, 6, "0", unit="PPM"), p(18, "PPM eBus CO2-sensor 2", 1, 6, "0", unit="PPM"),
    ]
    return {x["name"]: x for x in ps}


class FakeBrink:
    def __init__(self, email=EMAIL, password=PASSWORD):
        self.email, self.password = email, password
        self.params = flair_params()
        self.gateway_state = 2  # 0 vergrendeld, 1 offline, 2 online
        self.token_ttl = 3600
        self.tokens: dict[str, float] = {}
        self.codes: dict[str, dict] = {}
        self.logins = 0  # aantal geslaagde en mislukte inlogpogingen (POST van het formulier)
        self.bad_logins = 0
        self.lists = 0
        self.uis = 0
        self.puts: list[dict] = []
        self.put_status = 200
        self.apply_delay = 0.0  # s tot een opdracht doorwerkt
        self.api_status = 0  # de systeemlijst antwoordt met deze status
        self.hide_form = False  # een inlogpagina zonder formulier die we herkennen
        self.antiforgery: set[str] = set()
        self.runner = None
        self.port = 0

    # ------------------------------------------------------------ de IdentityServer

    @staticmethod
    def _login_page(token: str, action: str, error: bool = False) -> str:
        form = (f'<form method="post" action="{html.escape(action)}"><input name="__RequestVerificationToken" type="hidden" value="{token}" />'
                '<input type="text" name="Input.Username" /><input type="password" name="Input.Password" />'
                '<input type="checkbox" name="Input.RememberMe" value="true" /><input name="Input.RememberMe" type="hidden" value="false" />'
                '<button type="submit" id="spinner-button">Inloggen</button></form>')
        return ('<html><body><form class="toolbar-logo-container"></form><form method="post" class="input-fields"><input name="__RequestVerificationToken" type="hidden" value="x" /></form>'
                + ("<p>Foutieve gegevens</p>" if error else "") + form + "</body></html>")

    async def _authorize(self, r: web.Request) -> web.Response:
        q = r.query
        if q.get("client_id") != "spa" or q.get("response_type") != "code" or q.get("code_challenge_method") != "S256" or not q.get("code_challenge") or q.get("redirect_uri") != APP:
            return web.Response(status=400, text="invalid_request")
        pending = {"state": q.get("state", ""), "challenge": q["code_challenge"]}
        if r.cookies.get(".id") == "ingelogd":
            return self._callback_redirect(pending)
        ret = "/idsrv/connect/authorize/callback?" + "&".join(f"{k}={quote(v)}" for k, v in q.items())
        return web.Response(status=302, headers={"Location": f"/idsrv/account/login?ReturnUrl={quote(ret, safe='')}"})

    def _callback_redirect(self, pending: dict) -> web.Response:
        code = secrets.token_hex(8)
        self.codes[code] = pending
        return web.Response(status=302, headers={"Location": f"{APP}?code={code}&state={quote(pending['state'])}&session_state=abc"})

    async def _login_get(self, r: web.Request) -> web.Response:
        if self.hide_form:
            return web.Response(text="<html><body>Onderhoud</body></html>", content_type="text/html")
        token = secrets.token_hex(16)
        self.antiforgery.add(token)
        resp = web.Response(text=self._login_page(token, "/idsrv/account/login?ReturnUrl=" + quote(r.query.get("ReturnUrl", ""), safe="")), content_type="text/html")
        resp.set_cookie(".af", token)
        return resp

    async def _login_post(self, r: web.Request) -> web.Response:
        self.logins += 1
        f = await r.post()
        token = f.get("__RequestVerificationToken", "")
        if token not in self.antiforgery or r.cookies.get(".af") != token:
            return web.Response(status=400, text="antiforgery")
        if f.get("Input.Username") != self.email or f.get("Input.Password") != self.password:
            self.bad_logins += 1
            return web.Response(text=self._login_page(secrets.token_hex(16), "/idsrv/account/login", error=True), content_type="text/html")
        ret = r.query.get("ReturnUrl", "")
        resp = web.Response(status=302, headers={"Location": ret})
        resp.set_cookie(".id", "ingelogd")
        return resp

    async def _callback(self, r: web.Request) -> web.Response:
        if r.cookies.get(".id") != "ingelogd":
            return web.Response(status=401)
        q = r.query
        return self._callback_redirect({"state": q.get("state", ""), "challenge": q.get("code_challenge", "")})

    async def _token(self, r: web.Request) -> web.Response:
        f = await r.post()
        pending = self.codes.pop(f.get("code", ""), None)
        if not pending or f.get("client_id") != "spa" or f.get("redirect_uri") != APP or f.get("grant_type") != "authorization_code":
            return web.json_response({"error": "invalid_grant"}, status=400)
        digest = base64.urlsafe_b64encode(hashlib.sha256(f.get("code_verifier", "").encode()).digest()).decode().rstrip("=")
        if digest != pending["challenge"]:
            return web.json_response({"error": "invalid_grant", "detail": "pkce"}, status=400)
        tok = secrets.token_hex(24)
        self.tokens[tok] = time.monotonic() + self.token_ttl
        return web.json_response({"access_token": tok, "expires_in": self.token_ttl, "token_type": "Bearer", "scope": "openid api role locale"})

    # ------------------------------------------------------------ de API

    def _auth(self, r: web.Request) -> bool:
        h = r.headers.get("Authorization", "")
        t = h[7:] if h.startswith("Bearer ") else ""
        return t in self.tokens and time.monotonic() < self.tokens[t]

    def revoke_tokens(self):
        self.tokens.clear()

    async def _systems(self, r: web.Request) -> web.Response:
        if not self._auth(r):
            return web.Response(status=401)
        self.lists += 1
        if self.api_status:
            return web.Response(status=self.api_status)
        return web.json_response({"items": [{"systemName": "Thuis", "serialNumber": "X", "systemShareId": SYSTEM, "isSystemOwner": True, "isEditable": True, "accessLevel": 12,
                                             "gatewayState": self.gateway_state, "activeAlertCount": {"alertCount": 0}}], "totalCount": 1})

    def tree(self) -> dict:
        ip = {"valueId": BASE_ID + 90, "name": "IP Adresse", "readWrite": 1, "controlType": 9, "value": "192.0.2.42", "valueState": 1}
        return {"root": {"navId": "root", "name": "Ventilatie", "state": 0, "itemsNavigationType": 0, "navigationItems": [
            {"navId": "c-wifi", "name": "Internal Wifi Module", "componentId": WIFI, "state": 0, "navigationItems": [
                {"navId": "c-wifi-1", "name": "Internal Wifi Module", "componentId": WIFI, "parameterGroups": [{"parameters": [ip]}]}]},
            {"navId": "c-flair", "name": "Flair 400", "componentId": FLAIR, "state": 0, "navigationItems": [
                {"navId": "c-flair-1", "name": "Flair 400", "componentId": FLAIR, "parameterGroups": [{"parameters": list(self.params.values())}]}]}]}}

    async def _ui(self, r: web.Request) -> web.Response:
        if not self._auth(r):
            return web.Response(status=401)
        self.uis += 1
        return web.json_response(self.tree())

    def _apply(self, vid: int, value: str):
        for p in self.params.values():
            if p["valueId"] == vid:
                p["value"], p["valueState"] = value, 0

    async def _put(self, r: web.Request) -> web.Response:
        if not self._auth(r):
            return web.Response(status=401)
        body = await r.json()
        self.puts.append(body)
        if self.put_status != 200:
            return web.json_response({}, status=self.put_status)
        for w in body.get("writeValues", []):
            if self.apply_delay:
                asyncio.get_running_loop().call_later(self.apply_delay, self._apply, w["valueId"], w["value"])
            else:
                self._apply(w["valueId"], w["value"])
        return web.json_response({})

    # ------------------------------------------------------------ start en stop

    def set_value(self, name: str, value: str, state: int = 0):
        """De unit meldt iets anders (iemand draait aan de wandschakelaar)."""
        self.params[name]["value"], self.params[name]["valueState"] = value, state

    async def start(self, port=0):
        app = web.Application()
        app.router.add_get("/idsrv/connect/authorize", self._authorize)
        app.router.add_get("/idsrv/connect/authorize/callback", self._callback)
        app.router.add_get("/idsrv/account/login", self._login_get)
        app.router.add_post("/idsrv/account/login", self._login_post)
        app.router.add_post("/idsrv/connect/token", self._token)
        app.router.add_get("/portal/api/v1.1/systems", self._systems)
        app.router.add_get("/portal/api/v1.1/systems/{sid}/uidescription", self._ui)
        app.router.add_put("/portal/api/v1.1/systems/{sid}/parameter-values", self._put)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", port)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        return self

    async def stop(self):
        if self.runner:
            await self.runner.cleanup()
            self.runner = None
