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
import logging

import aiohttp

from .engine import Engine

log = logging.getLogger("kk.ha")

# Entiteiten die als gang van een extern apparaat kunnen dienen
DOMAINS = ("switch", "light", "input_boolean", "fan")


class HaError(Exception):
    pass


class HaBridge:
    def __init__(self, url: str, token: str, engine: Engine, retry: tuple[float, float] = (2.0, 60.0)) -> None:
        self.url, self.token, self.engine = url, token, engine
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
            self.engine.virt.set_link(True)
            await self._sync()
        except (HaError, asyncio.TimeoutError) as e:
            self.last_error = str(e) or "initialiseren mislukt"
            log.warning("HA initialiseren mislukt: %s", self.last_error)
            if self._ws:
                await self._ws.close()  # opnieuw proberen via de hoofdlus

    # ------------------------------------------------------------ berichten

    async def _request(self, payload: dict, timeout: float = 20.0) -> dict:
        ws = self._ws
        if ws is None or ws.closed:
            raise HaError("niet verbonden met HA")
        self._id += 1
        i = self._id
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
            if ev.get("event_type") != "state_changed":
                return
            data = ev.get("data") or {}
            ent = data.get("entity_id")
            if ent in self.engine.virt.entities():
                new = data.get("new_state")
                self.engine.virt.ha_event(ent, new.get("state") if new else "unavailable")

    async def _sync(self) -> list[dict]:
        res = await self._request({"type": "get_states"})
        states = {s["entity_id"]: s.get("state") for s in res.get("result") or [] if "entity_id" in s}
        self._cache = [
            {"entity_id": s["entity_id"], "name": (s.get("attributes") or {}).get("friendly_name") or s["entity_id"], "state": s.get("state")}
            for s in res.get("result") or []
            if s.get("entity_id", "").split(".")[0] in DOMAINS
        ]
        self.engine.virt.ha_sync({e: states.get(e) for e in self.engine.virt.entities()})
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

    async def _send(self, msg: dict) -> None:
        try:
            if self._ws and not self._ws.closed:
                await self._ws.send_json(msg)
        except (aiohttp.ClientError, ConnectionError) as e:
            log.warning("bevel naar HA mislukt: %s", e)

    # ------------------------------------------------------------ model veranderd

    def _on_event(self, e: dict) -> None:
        """Een nieuwe entiteit in het model: haal haar stand op (anders blijft ze 'onbekend' tot HA iets meldt)."""
        if e["t"] != "model" or not self.connected or self._refresh_pending:
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
