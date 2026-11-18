"""De web-API voor HUISDEV-scènes (scenehub.py). web.py roept `add_routes` een keer aan; de handlers staan hier zodat web.py klein blijft.

  GET    /api/scenes                       de bibliotheek: [{id, name, def, release, hash, bytes, bounded, total_ms}]
  POST   /api/scenes                       {name, def, release?} -> {ok, id}
  PUT    /api/scenes/<id>                  {name?, def?, release?}
  DELETE /api/scenes/<id>                  ruimt overal op (selectie, bedrading, slot)
  POST   /api/scenes/preview               {def, n?, samples?} -> scenes.preview (n leds, samples tijdstippen, kleuren 0..255)
  PUT    /api/espdev/<did>/scenes          {scenes: [ids]}: welke scènes op dit apparaat staan
  POST   /api/espdev/<did>/scene/play      {id} of {slot}
  POST   /api/espdev/<did>/scene/stop      {} of {slot} of {id}
"""
from __future__ import annotations

from aiohttp import web

from .model import ModelError


def add_routes(router, ctx_key, body) -> None:
    def hub(request: web.Request):
        h = request.app[ctx_key].espdev
        if h is None:
            raise ModelError("zelfbouwapparaten zijn niet beschikbaar")
        return h.scn

    async def get(request: web.Request) -> web.Response:
        return web.json_response(hub(request).view_library())

    async def post(request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "id": hub(request).add(await body(request))})

    async def put(request: web.Request) -> web.Response:
        hub(request).update(request.match_info["sid"], await body(request))
        return web.json_response({"ok": True})

    async def delete(request: web.Request) -> web.Response:
        ok = hub(request).delete(request.match_info["sid"])
        return web.json_response({"ok": ok}, status=200 if ok else 404)

    async def preview(request: web.Request) -> web.Response:
        from . import scenehub
        b = await body(request)
        n, samples = b.get("n", 20), b.get("samples", 60)
        hub(request)
        return web.json_response(scenehub.preview(b.get("def"), n, samples))

    async def select(request: web.Request) -> web.Response:
        hub(request).select(request.match_info["did"], (await body(request)).get("scenes"))
        return web.json_response({"ok": True})

    async def play(request: web.Request) -> web.Response:
        return web.json_response(await hub(request).play(request.match_info["did"], await body(request)))

    async def stop(request: web.Request) -> web.Response:
        return web.json_response(await hub(request).stop(request.match_info["did"], await body(request)))

    router.add_get("/api/scenes", get)
    router.add_post("/api/scenes", post)
    router.add_post("/api/scenes/preview", preview)  # voor `{sid}`, al is de methode anders
    router.add_put("/api/scenes/{sid}", put)
    router.add_delete("/api/scenes/{sid}", delete)
    router.add_put("/api/espdev/{did}/scenes", select)
    router.add_post("/api/espdev/{did}/scene/play", play)
    router.add_post("/api/espdev/{did}/scene/stop", stop)
