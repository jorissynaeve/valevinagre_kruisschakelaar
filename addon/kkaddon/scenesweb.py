"""De web-API voor HUISDEV-scènes (scenehub.py). web.py roept `add_routes` een keer aan; de handlers staan hier zodat web.py klein blijft.

  GET    /api/scenes                       de bibliotheek: [{id, name, def, release, hash, bytes, bounded, total_ms, window_ms}]
  POST   /api/scenes                       {name, def, release?} -> {ok, id}
  PUT    /api/scenes/<id>                  {name?, def?, release?}
  DELETE /api/scenes/<id>                  ruimt overal op (selectie, bedrading, slot)
  POST   /api/scenes/preview               {def, n?, samples?} -> scenes.preview (n leds, samples tijdstippen, kleuren 0..255)
  POST   /api/scenes/frame                 {def | id, t_ms, n?} -> {t_ms, total_ms, window_ms, bounded, frame: [[r,g,b]...]}: het beeld op dat tijdstip (tijdschuif)
  GET    /api/scenerules                   de blokken Scènes: [{id, name, device, inputs: [{id, name, kind, ref, value}], rules: [{scene, when, text, on}], active, ...}]
  POST   /api/scenerules                   {name?, device?, inputs?, rules?} -> {ok, id}
  PUT    /api/scenerules/<id>              {name?, device?, inputs?, refs?, rules?}: een lijst wordt helemaal vervangen; `refs` {ingang: bron} wijzigt alleen bronnen
  DELETE /api/scenerules/<id>              het blok weg; de scènes van het apparaat verdwijnen van zijn slots
  POST   /api/espdev/<did>/scene/play      {id} of {slot}
  POST   /api/espdev/<did>/scene/stop      {} of {slot} of {id}
  POST   /api/espdev/<did>/scene/seek      {def | id | slot, t_ms}: de strip toont het beeld op t_ms en houdt het vast (HUISDEV 1.2; samengevoegd, het nieuwste wint)
  POST   /api/espdev/<did>/scene/seek/stop stopt het vasthouden (alleen als het apparaat een beeld uit de tijdschuif toont)
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

    async def frame(request: web.Request) -> web.Response:
        return web.json_response(hub(request).frame(await body(request)))

    async def rules_get(request: web.Request) -> web.Response:
        return web.json_response(hub(request).rules_view())

    async def rules_post(request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "id": hub(request).add_block(await body(request))})

    async def rules_put(request: web.Request) -> web.Response:
        hub(request).update_block(request.match_info["bid"], await body(request))
        return web.json_response({"ok": True})

    async def rules_delete(request: web.Request) -> web.Response:
        ok = hub(request).delete_block(request.match_info["bid"])
        return web.json_response({"ok": ok}, status=200 if ok else 404)

    async def play(request: web.Request) -> web.Response:
        return web.json_response(await hub(request).play(request.match_info["did"], await body(request)))

    async def stop(request: web.Request) -> web.Response:
        return web.json_response(await hub(request).stop(request.match_info["did"], await body(request)))

    async def seek(request: web.Request) -> web.Response:
        return web.json_response(await hub(request).seek(request.match_info["did"], await body(request)))

    async def seek_stop(request: web.Request) -> web.Response:
        return web.json_response(await hub(request).seek_stop(request.match_info["did"]))

    router.add_get("/api/scenes", get)
    router.add_post("/api/scenes", post)
    router.add_post("/api/scenes/preview", preview)  # voor `{sid}`, al is de methode anders
    router.add_post("/api/scenes/frame", frame)
    router.add_put("/api/scenes/{sid}", put)
    router.add_delete("/api/scenes/{sid}", delete)
    router.add_get("/api/scenerules", rules_get)
    router.add_post("/api/scenerules", rules_post)
    router.add_put("/api/scenerules/{bid}", rules_put)
    router.add_delete("/api/scenerules/{bid}", rules_delete)
    router.add_post("/api/espdev/{did}/scene/play", play)
    router.add_post("/api/espdev/{did}/scene/stop", stop)
    router.add_post("/api/espdev/{did}/scene/seek", seek)
    router.add_post("/api/espdev/{did}/scene/seek/stop", seek_stop)
