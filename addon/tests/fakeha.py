"""Een nagebootste Home Assistant (websocket-API, voldoende voor habridge) met een besturingsingang voor tests.

  ws   /api/websocket   auth, subscribe_events, get_states, call_service (zet de stand na `delay` seconden)
  POST /ctl/press       {"entity": ".."}   iemand drukt op de fysieke touch: stand wisselt zonder call_service
  POST /ctl/set         {"entity": "..", "state": ".."}
  POST /ctl/ignore      {"on": true}       negeer call_service (apparaat reageert niet)
  POST /ctl/kick        sluit alle verbindingen
  config/{entity,device,area}_registry/list   alleen als `registry` is gezet (zoals HA met een admin-token)
  GET  /ctl/state       {"states": {..}, "calls": [[entity, "turn_on"], ..]}
"""
from __future__ import annotations

import asyncio
import os

from aiohttp import WSMsgType, web


class FakeHa:
    def __init__(self, token: str = "test", delay: float = 0.15, entities: dict[str, str] | None = None, registry: dict | None = None) -> None:
        self.token, self.delay = token, delay
        self.states = dict(entities if entities is not None else {f"switch.t_{i}": "off" for i in range(4)})
        self.names = {e: e.split(".")[1].replace("_", " ").title() for e in self.states}
        self.ignore = False
        # {"devices": {id: {"name", "area", "platform", "entities": [id | (id, {registry-velden})]}}}
        self.registry = registry
        self.calls: list[tuple[str, str]] = []
        self.subs: dict[web.WebSocketResponse, int] = {}
        self.sockets: set[web.WebSocketResponse] = set()
        self.auth_ok = 0

    # ---- toestand

    async def set_state(self, entity: str, state: str) -> None:
        if self.states.get(entity) == state:
            return
        self.states[entity] = state
        ev = {"event_type": "state_changed", "data": {"entity_id": entity, "new_state": {"entity_id": entity, "state": state, "attributes": {"friendly_name": self.names.get(entity, entity)}}}}
        for ws, sid in list(self.subs.items()):
            if not ws.closed:
                await ws.send_json({"id": sid, "type": "event", "event": ev})

    async def _later(self, entity: str, state: str) -> None:
        await asyncio.sleep(self.delay)
        await self.set_state(entity, state)

    # ---- websocket

    async def ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.sockets.add(ws)
        await ws.send_json({"type": "auth_required", "ha_version": "fake"})
        auth = await ws.receive_json()
        if auth.get("access_token") != self.token:
            await ws.send_json({"type": "auth_invalid", "message": "Invalid access token"})
            await ws.close()
            return ws
        await ws.send_json({"type": "auth_ok", "ha_version": "fake"})
        self.auth_ok += 1
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            m = msg.json()
            i, t = m.get("id"), m.get("type")
            if t == "subscribe_events":
                self.subs[ws] = i
                await ws.send_json({"id": i, "type": "result", "success": True, "result": None})
            elif t == "get_states":
                res = [{"entity_id": e, "state": s, "attributes": {"friendly_name": self.names.get(e, e)}} for e, s in self.states.items()]
                await ws.send_json({"id": i, "type": "result", "success": True, "result": res})
            elif t in ("config/entity_registry/list", "config/device_registry/list", "config/area_registry/list") and self.registry is not None:
                await ws.send_json({"id": i, "type": "result", "success": True, "result": self._registry(t)})
            elif t == "call_service":
                ent = (m.get("service_data") or {}).get("entity_id")
                svc = m.get("service")
                self.calls.append((ent, svc))
                ok = ent in self.states and svc in ("turn_on", "turn_off")
                await ws.send_json({"id": i, "type": "result", "success": ok, "result": {}, **({} if ok else {"error": {"code": "not_found", "message": "Entity not found"}})})
                if ok and not self.ignore:
                    asyncio.get_running_loop().create_task(self._later(ent, "on" if svc == "turn_on" else "off"))
            else:
                await ws.send_json({"id": i, "type": "result", "success": False, "error": {"code": "unknown_command", "message": str(t)}})
        self.subs.pop(ws, None)
        self.sockets.discard(ws)
        return ws

    def _registry(self, kind: str) -> list[dict]:
        devs = self.registry["devices"]
        if kind == "config/area_registry/list":
            return [{"area_id": a, "name": a.title()} for a in sorted({d["area"] for d in devs.values() if d.get("area")})]
        if kind == "config/device_registry/list":
            return [{"id": i, "name": d["name"], "name_by_user": d.get("name_by_user"), "area_id": d.get("area")} for i, d in devs.items()]
        out = []
        for i, d in devs.items():
            for ent in d["entities"]:
                eid, extra = ent if isinstance(ent, tuple) else (ent, {})
                out.append({"entity_id": eid, "device_id": i, "platform": d.get("platform", "esphome"), "area_id": None, "entity_category": None, "disabled_by": None, "hidden_by": None, **extra})
        out.append({"entity_id": "switch.zonder_apparaat", "device_id": None, "platform": "template"})
        return out

    # ---- besturing voor tests

    async def press(self, r: web.Request) -> web.Response:
        ent = (await r.json())["entity"]
        await self.set_state(ent, "off" if self.states[ent] == "on" else "on")
        return web.json_response({"state": self.states[ent]})

    async def set_(self, r: web.Request) -> web.Response:
        b = await r.json()
        await self.set_state(b["entity"], b["state"])
        return web.json_response({"ok": True})

    async def ignore_(self, r: web.Request) -> web.Response:
        self.ignore = bool((await r.json())["on"])
        return web.json_response({"ok": True})

    async def kick(self, r: web.Request) -> web.Response:
        for ws in list(self.sockets):
            await ws.close()
        return web.json_response({"ok": True})

    async def state(self, r: web.Request) -> web.Response:
        return web.json_response({"states": self.states, "calls": self.calls, "auth_ok": self.auth_ok})

    def app(self) -> web.Application:
        a = web.Application()
        a.router.add_get("/api/websocket", self.ws)
        a.router.add_post("/ctl/press", self.press)
        a.router.add_post("/ctl/set", self.set_)
        a.router.add_post("/ctl/ignore", self.ignore_)
        a.router.add_post("/ctl/kick", self.kick)
        a.router.add_get("/ctl/state", self.state)
        return a


if __name__ == "__main__":
    demo = {"devices": {
        "d_touch": {"name": "Touch gang", "area": "gang", "platform": "esphome", "entities": ["switch.t_0", "switch.t_1", "switch.t_2"]},
        "d_box": {"name": "Relaisbox", "area": "technisch", "platform": "esphome", "entities": ["switch.t_3"]},
    }}
    ha = FakeHa(token=os.environ.get("FAKE_TOKEN", "test"), delay=float(os.environ.get("FAKE_DELAY", "0.15")), registry=demo)
    web.run_app(ha.app(), port=8123, print=None)
