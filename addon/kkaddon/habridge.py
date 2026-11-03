"""Koppeling met Home Assistant via de websocket-API (in de add-on: ws://supervisor/core/websocket met het
supervisor-token; buiten HA: KK_HA_URL en KK_HA_TOKEN met een long-lived token).

Doet drie dingen voor de externe apparaten (virtual.py):
  * na het verbinden de stand van alle gevolgde entiteiten ophalen (stil, geen druk),
  * state_changed-events doorgeven,
  * `turn_on`/`turn_off` aanroepen voor een bevel.
De add-on leest alleen entiteiten die in het model staan; de rest van HA blijft ongemoeid.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable

import aiohttp

from . import dashboard
from .engine import Engine

log = logging.getLogger("kk.ha")

# Entiteiten die als gang van een extern apparaat kunnen dienen
DOMAINS = ("switch", "light", "input_boolean", "fan")


# Gebeurtenissen waarbij het dashboard "Lampen" opnieuw moet kloppen
REGISTRY_EVENTS = ("entity_registry_updated", "device_registry_updated", "area_registry_updated")


class HaError(Exception):
    pass


class HaBridge:
    def __init__(self, url: str, token: str, engine: Engine, retry: tuple[float, float] = (2.0, 60.0), dashboard: bool = True, dashboard_delay: float = 5.0) -> None:
        self.url, self.token, self.engine = url, token, engine
        self.dashboard, self.dashboard_delay = dashboard, dashboard_delay
        self._dash_task: asyncio.Task | None = None
        self._dash_sig: str | None = None  # wat er laatst in HA staat; None: nog niet bekend
        self.enabled = bool(url and token)
        self.connected = False
        self.last_error = ""
        self.retry = retry
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._id = 0
        self._futures: dict[int, asyncio.Future] = {}
        self._tasks: set[asyncio.Task] = set()
        self._cache: list[dict] = []
        self._refresh_pending = False
        # voor de panelen (panelhub.py): alle standen met hun attributen, templates en voorspellingen
        self.states: dict[str, tuple[str, dict]] = {}
        self.templates: dict[str, Any] = {}
        self.state_listeners: list[Callable[[str | None], None]] = []  # entiteit, of None: alles kan veranderd zijn
        self.template_listeners: list[Callable[[str], None]] = []
        self._tpl_ids: dict[int, str] = {}
        self._tpl_wanted: set[str] = set()
        if self.enabled:
            engine.ha_call = self.call
            engine.subscribe(self._on_event)

    # ------------------------------------------------------------ verbinding

    async def run(self) -> None:
        if not self.enabled:
            log.info("geen HA-koppeling ingesteld: externe apparaten (bv. Tuya) blijven offline")
            return
        delay = self.retry[0]
        while True:
            try:
                async with aiohttp.ClientSession() as sess:
                    await self._session(sess)
                delay = self.retry[0]
            except asyncio.CancelledError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError, HaError, ValueError) as e:
                self.last_error = str(e) or e.__class__.__name__
                log.warning("HA-verbinding weg: %s", self.last_error)
            finally:
                self._down()
            await asyncio.sleep(delay)
            delay = min(delay * 2, self.retry[1])

    def _down(self) -> None:
        self.connected = False
        self._ws = None
        for fut in self._futures.values():
            if not fut.done():
                fut.set_exception(HaError("verbinding verbroken"))
        self._futures.clear()
        self._tpl_ids.clear()
        self._dash_sig = None  # na een nieuwe verbinding eerst vergelijken met wat HA heeft
        if self._dash_task:
            self._dash_task.cancel()
            self._dash_task = None
        self.engine.virt.set_link(False)

    async def _session(self, sess: aiohttp.ClientSession) -> None:
        async with sess.ws_connect(self.url, heartbeat=30, max_msg_size=64 * 1024 * 1024) as ws:
            hello = await ws.receive_json(timeout=10)
            if hello.get("type") != "auth_required":
                raise HaError(f"onverwacht antwoord van HA: {hello.get('type')}")
            await ws.send_json({"type": "auth", "access_token": self.token})
            reply = await ws.receive_json(timeout=10)
            if reply.get("type") != "auth_ok":
                raise HaError("HA weigert het token (controleer ha_token)" if reply.get("type") == "auth_invalid" else "inloggen bij HA mislukt")
            self._ws = ws
            self.connected = True
            self.last_error = ""
            log.info("verbonden met Home Assistant")
            init = asyncio.create_task(self._init())
            self._tasks.add(init)
            init.add_done_callback(self._tasks.discard)
            try:
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        self._on_message(msg.json())
                    elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                        break
            finally:
                init.cancel()

    async def _init(self) -> None:
        try:
            await self._request({"type": "subscribe_events", "event_type": "state_changed"})
            if self.dashboard:
                for ev in REGISTRY_EVENTS:  # een nieuw licht, een ander gebied: het dashboard volgt
                    try:
                        await self._request({"type": "subscribe_events", "event_type": ev})
                    except (HaError, asyncio.TimeoutError) as err:
                        log.warning("niet te volgen: %s (%s)", ev, err)
            self.engine.virt.set_link(True)
            await self._sync()
            self._schedule_dashboard()
            for text in sorted(self._tpl_wanted):
                await self._subscribe_template(text)
            try:
                cfg = (await self._request({"type": "get_config"}, timeout=10)).get("result", {})
                self.engine.set_tz(cfg.get("time_zone"))
                self.engine.set_location(cfg.get("latitude"), cfg.get("longitude"))
            except (HaError, asyncio.TimeoutError) as e:
                log.warning("tijdzone en locatie niet opgehaald: %s", e)
        except (HaError, asyncio.TimeoutError) as e:
            self.last_error = str(e) or "initialiseren mislukt"
            log.warning("HA initialiseren mislukt: %s", self.last_error)
            if self._ws:
                await self._ws.close()  # opnieuw proberen via de hoofdlus

    # ------------------------------------------------------------ berichten

    async def _request(self, payload: dict, timeout: float = 20.0, on_id: Callable[[int], None] | None = None) -> dict:
        ws = self._ws
        if ws is None or ws.closed:
            raise HaError("niet verbonden met HA")
        self._id += 1
        i = self._id
        if on_id:
            on_id(i)  # voor een abonnement: de gebeurtenissen kunnen vlak na het antwoord komen
        fut = asyncio.get_running_loop().create_future()
        self._futures[i] = fut
        await ws.send_json({"id": i, **payload})
        try:
            res = await asyncio.wait_for(fut, timeout)
        finally:
            self._futures.pop(i, None)
        if not res.get("success", False):
            raise HaError(str((res.get("error") or {}).get("message") or "HA wees het verzoek af"))
        return res

    def _on_message(self, m: dict) -> None:
        t = m.get("type")
        if t == "result":
            fut = self._futures.get(m.get("id"))
            if fut and not fut.done():
                fut.set_result(m)
        elif t == "event":
            ev = m.get("event") or {}
            if m.get("id") in self._tpl_ids:
                text = self._tpl_ids[m["id"]]
                if "result" in ev:
                    self.templates[text] = ev["result"]
                    self._notify(self.template_listeners, text)
                return
            if ev.get("event_type") in REGISTRY_EVENTS:
                self._schedule_dashboard()
                return
            if ev.get("event_type") != "state_changed":
                return
            data = ev.get("data") or {}
            ent = data.get("entity_id")
            new = data.get("new_state")
            if ent:
                if new:
                    self.states[ent] = (str(new.get("state")), new.get("attributes") or {})
                else:
                    self.states.pop(ent, None)
                self._notify(self.state_listeners, ent)
            if ent in self.engine.ha_entities():
                self.engine.ha_state(ent, new.get("state") if new else "unavailable")

    @staticmethod
    def _notify(listeners: list, arg) -> None:
        for cb in list(listeners):
            try:
                cb(arg)
            except Exception:  # een luisteraar mag de verbinding niet breken
                log.exception("luisteraar faalde")

    async def _sync(self) -> list[dict]:
        res = await self._request({"type": "get_states"})
        states = {s["entity_id"]: s.get("state") for s in res.get("result") or [] if "entity_id" in s}
        self.states = {s["entity_id"]: (str(s.get("state")), s.get("attributes") or {}) for s in res.get("result") or [] if "entity_id" in s}
        self._notify(self.state_listeners, None)
        self._cache = [
            {"entity_id": s["entity_id"], "name": (s.get("attributes") or {}).get("friendly_name") or s["entity_id"], "state": s.get("state")}
            for s in res.get("result") or []
            if s.get("entity_id", "").split(".")[0] in DOMAINS
        ]
        self.engine.ha_snapshot({e: states.get(e) for e in self.engine.ha_entities()})
        return self._cache

    async def entities(self) -> list[dict]:
        """Voor de UI: kandidaat-entiteiten, vers uit HA."""
        if not self.connected:
            return []
        try:
            return sorted(await self._sync(), key=lambda e: e["entity_id"])
        except (HaError, asyncio.TimeoutError):
            return sorted(self._cache, key=lambda e: e["entity_id"])

    async def devices(self) -> dict:
        """Voor de import: HA-apparaten met hun schakelaars/lichten, uit het apparaten-, entiteiten- en gebiedenregister.

        Zonder rechten voor de registers (of een HA die ze niet kent) komt `registry: False` terug en blijft de lijst leeg;
        de UI valt dan terug op losse entiteiten kiezen."""
        if not self.connected:
            return {"registry": False, "devices": []}
        try:
            states = {e["entity_id"]: e for e in await self._sync()}
            ents = (await self._request({"type": "config/entity_registry/list"})).get("result") or []
            devs = {d["id"]: d for d in (await self._request({"type": "config/device_registry/list"})).get("result") or []}
            try:
                areas = {a["area_id"]: a.get("name", "") for a in (await self._request({"type": "config/area_registry/list"})).get("result") or []}
            except (HaError, asyncio.TimeoutError):
                areas = {}
        except (HaError, asyncio.TimeoutError, KeyError, TypeError) as e:
            log.debug("registers niet beschikbaar: %s", e)
            return {"registry": False, "devices": []}
        out: dict[str, dict] = {}
        for r in ents:
            st = states.get(r.get("entity_id"))
            did = r.get("device_id")
            if st is None or not did or did not in devs or r.get("entity_category") or r.get("disabled_by") or r.get("hidden_by"):
                continue  # alleen echte bediening: geen config-/diagnose-entiteiten
            d = devs[did]
            o = out.setdefault(did, {
                "id": did,
                "name": d.get("name_by_user") or d.get("name") or did,
                "area": areas.get(r.get("area_id") or d.get("area_id"), ""),
                "platform": r.get("platform", ""),
                "entities": [],
            })
            o["entities"].append({"entity_id": st["entity_id"], "name": st["name"], "state": st["state"]})
        for o in out.values():
            o["entities"].sort(key=lambda x: x["entity_id"])
        return {"registry": True, "devices": sorted(out.values(), key=lambda d: d["name"].lower())}

    # ------------------------------------------------------------ naar HA

    def call(self, entity: str, on: bool) -> bool:
        ws = self._ws
        if ws is None or ws.closed or not self.connected:
            return False
        self._id += 1
        msg = {
            "id": self._id,
            "type": "call_service",
            "domain": "homeassistant",
            "service": "turn_on" if on else "turn_off",
            "service_data": {"entity_id": entity},
        }
        t = asyncio.get_running_loop().create_task(self._send(msg))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return True

    def call_service(self, domain: str, service: str, data: dict) -> bool:
        """Een willekeurige service aanroepen, zonder op het antwoord te wachten (een druk op het paneel)."""
        ws = self._ws
        if ws is None or ws.closed or not self.connected:
            return False
        self._id += 1
        msg = {"id": self._id, "type": "call_service", "domain": domain, "service": service, "service_data": data}
        t = asyncio.get_running_loop().create_task(self._send(msg))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return True

    async def forecast(self, entity: str, kind: str = "daily") -> list[dict]:
        """De voorspelling van een weer-entiteit (`weather.get_forecasts`); leeg als HA ze niet geeft."""
        try:
            res = await self._request({"type": "call_service", "domain": "weather", "service": "get_forecasts", "service_data": {"type": kind},
                                       "target": {"entity_id": entity}, "return_response": True})
            return ((res.get("result") or {}).get("response") or {}).get(entity, {}).get("forecast") or []
        except (HaError, asyncio.TimeoutError):
            return []

    def track_template(self, text: str) -> None:
        """Abonneren op een Jinja-sjabloon: `templates[text]` volgt het antwoord van HA, `template_listeners` horen elke wijziging."""
        if text in self._tpl_wanted:
            return
        self._tpl_wanted.add(text)
        if self.connected:
            t = asyncio.get_running_loop().create_task(self._subscribe_template(text))
            self._tasks.add(t)
            t.add_done_callback(self._tasks.discard)

    async def _subscribe_template(self, text: str) -> None:
        try:
            await self._request({"type": "render_template", "template": text, "report_errors": False}, on_id=lambda i: self._tpl_ids.__setitem__(i, text))
        except (HaError, asyncio.TimeoutError) as e:
            log.warning("sjabloon niet te volgen: %s", e)

    async def _send(self, msg: dict) -> None:
        try:
            if self._ws and not self._ws.closed:
                await self._ws.send_json(msg)
        except (aiohttp.ClientError, ConnectionError) as e:
            log.warning("bevel naar HA mislukt: %s", e)

    # ------------------------------------------------------------ dashboard "Lampen"

    def _schedule_dashboard(self) -> None:
        """Bundelen: veel wijzigingen vlak na elkaar (HA registreert ook net gepubliceerde lichten één voor één) geven één schrijfbeurt."""
        if not self.dashboard or not self.connected:
            return
        if self._dash_task and not self._dash_task.done():
            self._dash_task.cancel()

        async def later() -> None:
            try:
                await asyncio.sleep(self.dashboard_delay)
                await self.sync_dashboard()
            except asyncio.CancelledError:
                raise
            except (HaError, asyncio.TimeoutError, KeyError, TypeError) as err:
                log.warning("dashboard Lampen niet bijgewerkt: %s", err)

        self._dash_task = asyncio.get_running_loop().create_task(later())

    async def sync_dashboard(self) -> bool:
        """Maak het dashboard aan als het ontbreekt en schrijf de lampen per kamer erin. Geeft True als er geschreven is."""
        ents = (await self._request({"type": "config/entity_registry/list"})).get("result") or []
        devs = {d["id"]: d for d in (await self._request({"type": "config/device_registry/list"})).get("result") or []}
        names = {a["area_id"]: a.get("name", "") for a in (await self._request({"type": "config/area_registry/list"})).get("result") or []}
        entities: dict[str, str] = {}
        areas: dict[str, str] = {}
        for r in ents:
            uid = r.get("unique_id")
            if r.get("platform") != "mqtt" or not isinstance(uid, str) or not uid.startswith("kk_") or r.get("disabled_by") or r.get("hidden_by"):
                continue
            if not str(r.get("entity_id", "")).startswith("light."):
                continue
            entities[uid] = r["entity_id"]
            area = r.get("area_id") or (devs.get(r.get("device_id")) or {}).get("area_id")
            if area and names.get(area):
                areas[uid] = names[area]
        config = dashboard.build(self.engine.model, entities, areas)
        sig = json.dumps(config, sort_keys=True)
        if sig == self._dash_sig:
            return False
        listed = (await self._request({"type": "lovelace/dashboards/list"})).get("result") or []
        if not any(d.get("url_path") == dashboard.URL_PATH for d in listed):
            await self._request({"type": "lovelace/dashboards/create", "url_path": dashboard.URL_PATH, "title": dashboard.TITLE, "icon": dashboard.ICON,
                                 "show_in_sidebar": True, "require_admin": False, "mode": "storage"})
            log.info("dashboard %s aangemaakt", dashboard.TITLE)
        elif self._dash_sig is None:  # eerste keer na het verbinden: al hetzelfde dan niet schrijven
            try:
                have = (await self._request({"type": "lovelace/config", "url_path": dashboard.URL_PATH, "force": True})).get("result")
            except HaError:  # nog geen inhoud
                have = None
            if json.dumps(have, sort_keys=True) == sig:
                self._dash_sig = sig
                return False
        await self._request({"type": "lovelace/config/save", "url_path": dashboard.URL_PATH, "config": config})
        self._dash_sig = sig
        log.info("dashboard %s bijgewerkt: %d kamers", dashboard.TITLE, len(config["views"][0]["cards"]))
        return True

    # ------------------------------------------------------------ model veranderd

    def _on_event(self, e: dict) -> None:
        """Een nieuwe entiteit in het model: haal haar stand op (anders blijft ze 'onbekend' tot HA iets meldt)."""
        if e["t"] != "model" or not self.connected:
            return
        self._schedule_dashboard()
        if self._refresh_pending:
            return
        self._refresh_pending = True

        async def refresh() -> None:
            try:
                await asyncio.sleep(0.3)  # bundelen
                self._refresh_pending = False
                await self._sync()
            except (HaError, asyncio.TimeoutError) as err:
                log.debug("verversen mislukt: %s", err)
            finally:
                self._refresh_pending = False

        t = asyncio.get_running_loop().create_task(refresh())
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
