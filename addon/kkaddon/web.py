"""Web-UI en API (HA-ingress, poort 8099). Alleen de HA-ingress-proxy (172.30.32.2) mag erbij, tenzij dev_mode."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path

from aiohttp import WSMsgType, web

from . import __version__
from .engine import Engine
from .model import Model, ModelError
from .settings import Settings, key_header

log = logging.getLogger("kk.web")

STATIC = Path(__file__).parent / "static"
CTX = web.AppKey("ctx", object)
INGRESS_PROXY = {"172.30.32.2", "127.0.0.1", "::1"}
# Events die de pagina moet kennen; de rest (bv. losse commando-statussen) is ruis.
PUSH = {"btn", "light", "node", "cfg", "learn", "cmd", "model"}


class Ctx:
    """Alles wat de handlers nodig hebben."""

    def __init__(self, settings: Settings, engine: Engine, transport=None, mqtt=None, ha=None) -> None:
        self.settings, self.engine, self.model = settings, engine, engine.model
        self.transport, self.mqtt, self.ha = transport, mqtt, ha
        self.clients: set[web.WebSocketResponse] = set()
        engine.subscribe(self._on_event)

    def _on_event(self, e: dict) -> None:
        if e["t"] not in PUSH:
            return
        msg = json.dumps({"event": e})
        for ws in list(self.clients):
            if ws.closed:
                self.clients.discard(ws)
                continue
            asyncio.get_running_loop().create_task(self._send(ws, msg))

    async def _send(self, ws: web.WebSocketResponse, msg: str) -> None:
        try:
            await ws.send_str(msg)
        except ConnectionError:
            self.clients.discard(ws)


@web.middleware
async def guard(request: web.Request, handler):
    ctx: Ctx = request.app[CTX]
    if not ctx.settings.dev_mode and request.remote not in INGRESS_PROXY:
        raise web.HTTPForbidden(text="alleen via Home Assistant (ingress)")
    try:
        return await handler(request)
    except ModelError as e:
        return web.json_response({"error": str(e)}, status=400)
    except (ValueError, KeyError, TypeError) as e:
        return web.json_response({"error": f"ongeldige aanvraag: {e}"}, status=400)


async def _no_cache(request: web.Request, response: web.StreamResponse) -> None:
    """Na een update van de add-on moet de browser de nieuwe pagina en scripts halen, niet een oude kopie (ingress cachet graag)."""
    if request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"


def _ids(request: web.Request, *names: str) -> list[int]:
    try:
        return [int(request.match_info[n]) for n in names]
    except ValueError:
        raise ModelError("ongeldig nummer in het adres")


async def _body(request: web.Request) -> dict:
    try:
        b = await request.json()
    except json.JSONDecodeError:
        raise ModelError("geen geldige JSON")
    if not isinstance(b, dict):
        raise ModelError("JSON-object verwacht")
    return b


def build_state(ctx: Ctx) -> dict:
    snap = ctx.engine.snapshot()
    snap["version"] = __version__
    snap["log"] = list(ctx.engine.log)[-150:]
    snap["peers"] = ctx.transport.peers_info() if ctx.transport else []
    snap["mqtt"] = {"connected": bool(ctx.mqtt and ctx.mqtt.connected), "enabled": bool(ctx.mqtt and ctx.mqtt.enabled), "error": ctx.mqtt.last_error if ctx.mqtt else ""}
    snap["ha"] = {"enabled": bool(ctx.ha and ctx.ha.enabled), "connected": bool(ctx.ha and ctx.ha.connected), "error": ctx.ha.last_error if ctx.ha else ""}
    snap["plan"] = ctx.model.plan
    snap["names"] = {str(k): v.get("name", "") for k, v in ctx.model.nodes.items()}
    return snap


async def state(request: web.Request) -> web.Response:
    return web.json_response(build_state(request.app[CTX]))


async def light_cmd(request: web.Request) -> web.Response:
    ctx: Ctx = request.app[CTX]
    node, relay = _ids(request, "node", "relay")
    op = (await _body(request)).get("op")
    if op not in ("on", "off", "toggle"):
        raise ModelError("op moet on, off of toggle zijn")
    ok = ctx.engine.toggle_light(node, relay) if op == "toggle" else ctx.engine.command_light(node, relay, op == "on")
    return web.json_response({"ok": ok}, status=200 if ok else 404)


async def light_put(request: web.Request) -> web.Response:
    ctx: Ctx = request.app[CTX]
    node, relay = _ids(request, "node", "relay")
    b = await _body(request)
    ctx.model.set_light(node, relay, name=b.get("name"), enabled=b.get("enabled"))
    if "triggers" in b:
        ctx.engine.set_triggers(node, relay, b["triggers"])
    return web.json_response({"ok": True})


async def light_delete(request: web.Request) -> web.Response:
    ctx: Ctx = request.app[CTX]
    node, relay = _ids(request, "node", "relay")
    old = ctx.model.lights.get((node, relay))
    affected = {node} | {t.node for t in old.triggers} if old else set()
    ok = ctx.model.remove_light(node, relay)
    for n in sorted(affected):
        ctx.engine.sync_node(n)
    return web.json_response({"ok": ok}, status=200 if ok else 404)


async def node_put(request: web.Request) -> web.Response:
    ctx: Ctx = request.app[CTX]
    (node,) = _ids(request, "node")
    b = await _body(request)
    ctx.model.set_node(node, name=b.get("name"), room=b.get("room"), adopted=b.get("adopted"))
    return web.json_response({"ok": True})


async def node_identify(request: web.Request) -> web.Response:
    (node,) = _ids(request, "node")
    request.app[CTX].engine.identify(node)
    return web.json_response({"ok": True})


async def node_sync(request: web.Request) -> web.Response:
    ctx: Ctx = request.app[CTX]
    (node,) = _ids(request, "node")
    b = await _body(request)
    ctx.model.adopt(node)
    started = ctx.engine.sync_node(node, full=bool(b.get("full", True)))
    return web.json_response({"ok": True, "started": started})


async def follow_put(request: web.Request) -> web.Response:
    ctx: Ctx = request.app[CTX]
    node, ind = _ids(request, "node", "ind")
    light = (await _body(request)).get("light")
    if isinstance(light, dict):
        light = (int(light["node"]), int(light["relay"]))
    elif light is not None and light != "auto":
        raise ModelError('light moet null, "auto" of {node, relay} zijn')
    ctx.model.set_follow(node, ind, light)
    ctx.engine.sync_node(node)
    return web.json_response({"ok": True})


async def learn_start(request: web.Request) -> web.Response:
    ctx: Ctx = request.app[CTX]
    b = await _body(request)
    target = (int(b["node"]), int(b["relay"])) if "node" in b else None
    ctx.engine.learn_start(target)
    return web.json_response({"ok": True})


async def learn_stop(request: web.Request) -> web.Response:
    request.app[CTX].engine.learn_stop()
    return web.json_response({"ok": True})


async def learn_save(request: web.Request) -> web.Response:
    ctx: Ctx = request.app[CTX]
    ctx.engine.learn_save((await _body(request)).get("sources", []))
    return web.json_response({"ok": True})


async def ha_entities(request: web.Request) -> web.Response:
    ha = request.app[CTX].ha
    return web.json_response({"connected": bool(ha and ha.connected), "entities": await ha.entities() if ha else []})


async def plan_put(request: web.Request) -> web.Response:
    ctx: Ctx = request.app[CTX]
    ctx.model.set_plan(await _body(request))
    return web.json_response({"ok": True})


async def ha_devices(request: web.Request) -> web.Response:
    ctx: Ctx = request.app[CTX]
    r = await ctx.ha.devices() if ctx.ha else {"registry": False, "devices": []}
    used = {g["entity"] for gangs in ctx.model.virtual.values() for g in gangs}
    for d in r["devices"]:
        d["imported"] = all(e["entity_id"] in used for e in d["entities"])
    return web.json_response({"connected": bool(ctx.ha and ctx.ha.connected), **r})


def _virtual_args(b: dict) -> tuple[str, str, list]:
    return b.get("name") or "", b.get("room") or "", b.get("gangs")


async def virtual_post(request: web.Request) -> web.Response:
    ctx: Ctx = request.app[CTX]
    vid = ctx.model.set_virtual(None, *_virtual_args(await _body(request)))
    ctx.engine.resync_all()
    return web.json_response({"id": vid})


async def virtual_put(request: web.Request) -> web.Response:
    ctx: Ctx = request.app[CTX]
    (vid,) = _ids(request, "vid")
    if vid not in ctx.model.virtual:
        return web.json_response({"error": "onbekend extern apparaat"}, status=404)
    ctx.model.set_virtual(vid, *_virtual_args(await _body(request)))
    ctx.engine.resync_all()
    return web.json_response({"ok": True})


async def virtual_delete(request: web.Request) -> web.Response:
    ctx: Ctx = request.app[CTX]
    (vid,) = _ids(request, "vid")
    ok = ctx.model.remove_virtual(vid)
    ctx.engine.resync_all()
    return web.json_response({"ok": ok}, status=200 if ok else 404)


async def export(request: web.Request) -> web.Response:
    return web.json_response(request.app[CTX].model.to_dict(), headers={"Content-Disposition": 'attachment; filename="kruisschakelaars.json"'})


async def import_(request: web.Request) -> web.Response:
    ctx: Ctx = request.app[CTX]
    ctx.model.load_dict(await _body(request))
    for n in sorted(ctx.engine.nodes):
        if ctx.model.adopted(n):
            ctx.engine.sync_node(n, full=True)
    return web.json_response({"ok": True})


async def secrets_(request: web.Request) -> web.Response:
    s = request.app[CTX].settings
    return web.json_response({"key_hex": s.key_hex, "secrets_h": key_header(s.key), "generated": s.key_generated})


async def ws(request: web.Request) -> web.WebSocketResponse:
    ctx: Ctx = request.app[CTX]
    sock = web.WebSocketResponse(heartbeat=25)
    await sock.prepare(request)
    ctx.clients.add(sock)
    try:
        async for msg in sock:
            if msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                break
    finally:
        ctx.clients.discard(sock)
    return sock


def asset_version() -> str:
    """Korte vingerafdruk van de scripts en de stijl: staat als ?v= in de pagina, zodat een browser na een update nooit
    een oud script bij een nieuwe pagina gebruikt."""
    h = hashlib.sha1()
    for f in sorted(STATIC.iterdir()):
        if f.suffix in (".js", ".css"):
            h.update(f.name.encode() + f.read_bytes())
    return h.hexdigest()[:10]


async def index(request: web.Request) -> web.Response:
    v = asset_version()
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for f in ("style.css", "plan.js", "app.js"):
        html = html.replace(f'static/{f}"', f'static/{f}?v={v}"')
    return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-cache"})


def make_app(ctx: Ctx) -> web.Application:
    app = web.Application(middlewares=[guard])
    app[CTX] = ctx
    app.on_response_prepare.append(_no_cache)
    r = app.router
    r.add_get("/", index)
    r.add_get("/api/state", state)
    r.add_get("/api/ws", ws)
    r.add_post("/api/light/{node}/{relay}/cmd", light_cmd)
    r.add_put("/api/light/{node}/{relay}", light_put)
    r.add_delete("/api/light/{node}/{relay}", light_delete)
    r.add_put("/api/node/{node}", node_put)
    r.add_post("/api/node/{node}/identify", node_identify)
    r.add_post("/api/node/{node}/sync", node_sync)
    r.add_put("/api/follow/{node}/{ind}", follow_put)
    r.add_post("/api/learn/start", learn_start)
    r.add_post("/api/learn/stop", learn_stop)
    r.add_post("/api/learn/save", learn_save)
    r.add_get("/api/ha/entities", ha_entities)
    r.add_get("/api/ha/devices", ha_devices)
    r.add_put("/api/plan", plan_put)
    r.add_post("/api/virtual", virtual_post)
    r.add_put("/api/virtual/{vid}", virtual_put)
    r.add_delete("/api/virtual/{vid}", virtual_delete)
    r.add_get("/api/export", export)
    r.add_post("/api/import", import_)
    r.add_get("/api/secrets", secrets_)
    r.add_static("/static/", STATIC, show_index=False)
    return app
