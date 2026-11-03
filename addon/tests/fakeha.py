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
        self.attrs: dict[str, dict] = {}  # attributen per entiteit (naast friendly_name)
        self.service_calls: list[tuple[str, str, dict]] = []  # (domein, service, data) van elke call_service
        self.templates: dict[str, object] = {}  # sjabloon -> antwoord
        self.forecasts: dict[str, list] = {}
        self.tpl_subs: dict[str, tuple[web.WebSocketResponse, int]] = {}
        self.subs: dict[web.WebSocketResponse, int] = {}
        self.sockets: set[web.WebSocketResponse] = set()
        self.auth_ok = 0
        self.overview: dict | None = {"views": [{"title": "Zwembad", "path": "zwembad", "cards": []}]}  # de opgeslagen Overview; None: automatisch
        self.saves = 0
        self.dashboards: dict[str, str] = {}  # dashboard-id -> url_path (de extra dashboards in de zijbalk)

    # ---- toestand

    def _attrs(self, entity: str) -> dict:
        return {"friendly_name": self.names.get(entity, entity), **self.attrs.get(entity, {})}

    async def push_template(self, text: str, value: object) -> None:
        self.templates[text] = value
        sub = self.tpl_subs.get(text)
        if sub and not sub[0].closed:
            await sub[0].send_json({"id": sub[1], "type": "event", "event": {"result": value, "listeners": {}}})

    async def set_state(self, entity: str, state: str, attrs: dict | None = None) -> None:
        if attrs is not None:
            self.attrs[entity] = attrs
        elif self.states.get(entity) == state:
            return
        self.states[entity] = state
        ev = {"event_type": "state_changed", "data": {"entity_id": entity, "new_state": {"entity_id": entity, "state": state, "attributes": self._attrs(entity)}}}
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
            elif t == "get_config":
                await ws.send_json({"id": i, "type": "result", "success": True, "result": {"time_zone": "Europe/Lisbon", "latitude": 38.72, "longitude": -9.14}})
            elif t == "get_states":
                res = [{"entity_id": e, "state": s, "attributes": self._attrs(e)} for e, s in self.states.items()]
                await ws.send_json({"id": i, "type": "result", "success": True, "result": res})
            elif t in ("config/entity_registry/list", "config/device_registry/list", "config/area_registry/list") and self.registry is not None:
                await ws.send_json({"id": i, "type": "result", "success": True, "result": self._registry(t)})
            elif t == "lovelace/dashboards/list":
                await ws.send_json({"id": i, "type": "result", "success": True, "result": [{"id": k, "url_path": v} for k, v in self.dashboards.items()]})
            elif t == "lovelace/dashboards/delete":
                self.dashboards.pop(m["dashboard_id"], None)
                await ws.send_json({"id": i, "type": "result", "success": True, "result": None})
            elif t == "lovelace/config":
                if self.overview is None:  # een automatisch gemaakte Overview
                    await ws.send_json({"id": i, "type": "result", "success": False, "error": {"code": "config_not_found", "message": "No config found."}})
                else:
                    await ws.send_json({"id": i, "type": "result", "success": True, "result": self.overview})
            elif t == "lovelace/config/save":
                self.overview = m["config"]
                self.saves += 1
                await ws.send_json({"id": i, "type": "result", "success": True, "result": None})
            elif t == "render_template":
                self.tpl_subs[m["template"]] = (ws, i)
                await ws.send_json({"id": i, "type": "result", "success": True, "result": None})
                if m["template"] in self.templates:
                    await ws.send_json({"id": i, "type": "event", "event": {"result": self.templates[m["template"]], "listeners": {}}})
            elif t == "call_service" and m.get("service") == "get_forecasts":
                ent = (m.get("target") or {}).get("entity_id")
                await ws.send_json({"id": i, "type": "result", "success": True, "result": {"context": {}, "response": {ent: {"forecast": self.forecasts.get(ent, [])}}}})
            elif t == "call_service" and m.get("domain") != "homeassistant":
                data = m.get("service_data") or {}
                self.service_calls.append((m["domain"], m["service"], data))
                await ws.send_json({"id": i, "type": "result", "success": True, "result": {}})
                ent = data.get("entity_id")
                if m["service"] == "toggle" and ent in self.states and not self.ignore:
                    asyncio.get_running_loop().create_task(self._later(ent, "off" if self.states[ent] == "on" else "on"))
            elif t == "call_service":
                ent = (m.get("service_data") or {}).get("entity_id")
                svc = m.get("service")
                self.calls.append((ent, svc))
                self.service_calls.append(("homeassistant", svc, m.get("service_data") or {}))
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
