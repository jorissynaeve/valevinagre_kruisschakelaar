import asyncio
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from kkaddon import proto, web
from kkaddon.proto import T
from kkaddon.settings import Settings
from rig import KEY, Rig


def run(coro):
    return asyncio.run(coro)


class Env:
    def __init__(self, tmp_path, dev=True):
        self.rig = Rig(tmp_path)
        s = Settings(data_dir=tmp_path, key=KEY, key_generated=False, dev_mode=dev)
        self.ctx = web.Ctx(s, self.rig.engine)
        h = self.rig
        h.heartbeat(1, relays=[(False, 0)], nbtn=3, nind=3, cfg_hash=proto.config_hash([[]], [None] * 3))
        h.heartbeat(2, relays=[(False, 0)] * 4, nbtn=0, nind=0, cfg_hash=proto.config_hash([[]] * 4, []))
        h.clear()

    async def client(self):
        c = TestClient(TestServer(web.make_app(self.ctx)))
        await c.start_server()
        return c


def test_static_and_state(tmp_path):
    async def go():
        env = Env(tmp_path)
        c = await env.client()
        try:
            r = await c.get("/")
            assert r.status == 200 and "Kruisschakelaars" in await r.text()
            for f in ("app.js", "style.css"):
                assert (await c.get(f"/static/{f}")).status == 200
            st = await (await c.get("/api/state")).json()
            assert [n["id"] for n in st["nodes"]] == [1, 2] and st["peers"] == [] and st["mqtt"]["connected"] is False
            assert st["version"] and st["learn"] is None and len(st["free_relays"]) == 5
        finally:
            await c.close()

    run(go())


def test_light_commands_and_validation(tmp_path):
    async def go():
        env = Env(tmp_path)
        c = await env.client()
        try:
            r = await c.post("/api/light/2/1/cmd", json={"op": "on"})
            assert r.status == 200
            assert [f.payload for f in env.rig.sent_of(T.CMD_LIGHT)] == [proto.cmd_light(2, 1, proto.Mode.ON)]
            assert (await c.post("/api/light/2/1/cmd", json={"op": "toggle"})).status == 200
            assert (await c.post("/api/light/9/1/cmd", json={"op": "on"})).status == 404
            assert (await c.post("/api/light/2/1/cmd", json={"op": "dim"})).status == 400
            assert (await c.post("/api/light/x/1/cmd", json={"op": "on"})).status == 400
            assert (await c.post("/api/light/2/1/cmd", data="geen json")).status == 400
            assert (await c.post("/api/light/2/1/cmd", json=[1])).status == 400
        finally:
            await c.close()

    run(go())


def test_edit_light_pushes_config_and_delete(tmp_path):
    async def go():
        env = Env(tmp_path)
        c = await env.client()
        try:
            r = await c.put("/api/light/2/0", json={"name": "Plafond", "triggers": [{"node": 1, "btn": 0, "mode": "toggle"}]})
            assert r.status == 200
            assert env.rig.model.lights[(2, 0)].name == "Plafond"
            await asyncio.sleep(0)
            env.rig.advance(0.3)
            cfg = env.rig.sent_of(T.CFG_SET)
            assert any(f.payload == proto.cfg_triggers(2, 0, [(1, 0, 0)]) for f in cfg)
            assert any(f.payload == proto.cfg_follow(1, 0, 2, 0) for f in cfg)  # touch 1: indicator 0 volgt het licht
            bad = await c.put("/api/light/2/0", json={"triggers": [{"node": 1, "btn": 9}]})
            assert bad.status == 400 and "btn" in (await bad.json())["error"]
            r = await c.delete("/api/light/2/0")
            assert r.status == 200 and (2, 0) not in env.rig.model.lights
            assert (await c.delete("/api/light/2/0")).status == 404
        finally:
            await c.close()

    run(go())


def test_node_follow_identify_sync(tmp_path):
    async def go():
        env = Env(tmp_path)
        c = await env.client()
        try:
            assert (await c.put("/api/node/2", json={"name": "Box", "room": "Living"})).status == 200
            assert env.rig.model.nodes[2]["name"] == "Box"
            assert (await c.post("/api/node/2/identify", json={})).status == 200
            assert env.rig.sent_of(T.IDENTIFY)[0].payload == proto.identify(2)
            env.rig.model.set_light(2, 1, name="Spots")
            assert (await c.put("/api/follow/1/2", json={"light": {"node": 2, "relay": 1}})).status == 200
            assert env.rig.model.follow[(1, 2)] == (2, 1)
            assert (await c.put("/api/follow/1/2", json={"light": "auto"})).status == 200
            assert (1, 2) not in env.rig.model.follow
            assert (await c.put("/api/follow/1/2", json={"light": "raar"})).status == 400
            r = await c.post("/api/node/2/sync", json={"full": True})
            assert (await r.json())["started"] is True
        finally:
            await c.close()

    run(go())


def test_learn_flow_through_api(tmp_path):
    async def go():
        env = Env(tmp_path)
        c = await env.client()
        try:
            assert (await c.post("/api/learn/start", json={"node": 2, "relay": 3})).status == 200
            env.rig.button(1, 1, 1)
            env.rig.button(1, 2, 1)
            st = await (await c.get("/api/state")).json()
            assert st["learn"]["target"] == [2, 3] and len(st["learn"]["detected"]) == 2
            r = await c.post("/api/learn/save", json={"sources": [{"node": 1, "btn": 2, "mode": "on"}]})
            assert r.status == 200
            assert [(t.node, t.btn, t.mode) for t in env.rig.model.lights[(2, 3)].triggers] == [(1, 2, "on")]
            st = await (await c.get("/api/state")).json()
            assert st["learn"] is None
            assert (await c.post("/api/learn/save", json={"sources": []})).status == 400  # geen sessie meer
        finally:
            await c.close()

    run(go())


def test_export_import(tmp_path):
    async def go():
        env = Env(tmp_path)
        c = await env.client()
        try:
            env.rig.model.set_light(2, 0, name="Plafond", triggers=[{"node": 1, "btn": 0}])
            exp = await (await c.get("/api/export")).json()
            assert exp["lights"][0]["name"] == "Plafond"
            env.rig.model.remove_light(2, 0)
            r = await c.post("/api/import", json=exp)
            assert r.status == 200 and env.rig.model.lights[(2, 0)].name == "Plafond"
            before = env.rig.model.to_dict()
            assert (await c.post("/api/import", json={"version": 1, "lights": [{"node": 0, "relay": 0}]})).status == 400
            assert env.rig.model.to_dict() == before
        finally:
            await c.close()

    run(go())


def test_secrets_endpoint(tmp_path):
    async def go():
        env = Env(tmp_path)
        c = await env.client()
        try:
            j = await (await c.get("/api/secrets")).json()
            assert j["key_hex"] == KEY.hex() and "KK_NETWORK_KEY" in j["secrets_h"]
        finally:
            await c.close()

    run(go())


def test_websocket_pushes_events(tmp_path):
    async def go():
        env = Env(tmp_path)
        c = await env.client()
        try:
            ws = await c.ws_connect("/api/ws")
            env.rig.button(1, 0, 5)
            msg = json.loads((await asyncio.wait_for(ws.receive(), 2)).data)
            assert msg["event"]["t"] == "btn" and msg["event"]["node"] == 1 and msg["event"]["btn"] == 0
            await ws.close()
        finally:
            await c.close()

    run(go())


def test_only_ingress_proxy_may_connect(tmp_path, monkeypatch):
    async def go():
        env = Env(tmp_path, dev=False)
        monkeypatch.setattr(web, "INGRESS_PROXY", {"172.30.32.2"})
        c = await env.client()
        try:
            assert (await c.get("/api/state")).status == 403
            assert (await c.get("/")).status == 403
            monkeypatch.setattr(web, "INGRESS_PROXY", {"127.0.0.1"})
            assert (await c.get("/api/state")).status == 200
        finally:
            await c.close()

    run(go())


GANGS = [{"entity": f"switch.t_{i}", "name": f"Gang {i + 1}", "lamp": i < 2} for i in range(4)]


def test_virtual_device_api(tmp_path):
    async def go():
        env = Env(tmp_path)
        c = await env.client()
        try:
            r = await c.post("/api/virtual", json={"name": "Tuya", "room": "Gang", "gangs": GANGS})
            assert r.status == 200
            vid = (await r.json())["id"]
            assert proto.is_virtual(vid) and env.rig.model.virtual[vid][0]["entity"] == "switch.t_0"
            st = await (await c.get("/api/state")).json()
            n = next(n for n in st["nodes"] if n["id"] == vid)
            assert n["virtual"] and n["nbtn"] == 4 and st["names"][str(vid)] == "Tuya"
            assert st["ha"] == {"enabled": False, "connected": False, "error": ""}
            assert {(l["node"], l["relay"]) for l in st["lights"]} == {(vid, 0), (vid, 1)}
            bad = await c.post("/api/virtual", json={"name": "x", "gangs": [{"entity": "geen"}]})
            assert bad.status == 400 and "entiteit" in (await bad.json())["error"]
            assert (await c.post("/api/virtual", json={"name": "x", "gangs": GANGS})).status == 400  # entiteiten al in gebruik
            assert (await c.put(f"/api/virtual/{vid}", json={"name": "Tuya 2", "gangs": GANGS[:2]})).status == 200
            assert len(env.rig.model.virtual[vid]) == 2 and env.rig.model.nodes[vid]["name"] == "Tuya 2"
            assert (await c.put(f"/api/virtual/{vid + 1}", json={"gangs": GANGS})).status == 404
            # een extern licht bedienen en er schakelaars aan koppelen
            r = await c.put(f"/api/light/{vid}/0", json={"triggers": [{"node": 1, "btn": 2}]})
            assert r.status == 200
            env.rig.advance(0.3)
            assert any(f.payload == proto.cfg_follow(1, 2, vid, 0) for f in env.rig.sent_of(T.CFG_SET))  # touch 1: LED volgt het externe licht
            assert not [f for f in env.rig.sent_of(T.CFG_SET) if int.from_bytes(f.payload[:2], "little") == vid]  # extern: nooit config
            assert (await c.delete(f"/api/virtual/{vid}")).status == 200
            assert (await c.delete(f"/api/virtual/{vid}")).status == 404
            assert vid not in env.rig.model.virtual and not env.rig.model.lights
        finally:
            await c.close()

    run(go())


def test_ha_entities_without_link_is_empty_not_an_error(tmp_path):
    async def go():
        env = Env(tmp_path)
        c = await env.client()
        try:
            assert await (await c.get("/api/ha/entities")).json() == {"connected": False, "entities": []}
        finally:
            await c.close()

    run(go())


def test_ha_devices_marks_what_is_already_imported(tmp_path):
    class StubHa:
        connected = True

        async def devices(self):
            return {"registry": True, "devices": [
                {"id": "a", "name": "Touch 01", "area": "", "platform": "esphome", "entities": [{"entity_id": "switch.t_0", "name": "x", "state": "on"}, {"entity_id": "switch.t_1", "name": "y", "state": "off"}]},
                {"id": "b", "name": "Touch 02", "area": "", "platform": "esphome", "entities": [{"entity_id": "switch.u_0", "name": "z", "state": "off"}]},
            ]}

    async def go():
        env = Env(tmp_path)
        env.ctx.ha = StubHa()
        env.rig.model.set_virtual(None, "Touch 01", "", [{"entity": "switch.t_0", "name": "a", "lamp": True}, {"entity": "switch.t_1", "name": "b", "lamp": True}])
        c = await env.client()
        try:
            r = await (await c.get("/api/ha/devices")).json()
            assert r["connected"] and r["registry"]
            assert [(d["name"], d["imported"]) for d in r["devices"]] == [("Touch 01", True), ("Touch 02", False)]
        finally:
            await c.close()

    run(go())


def test_ha_devices_without_link_is_empty_not_an_error(tmp_path):
    async def go():
        env = Env(tmp_path)
        c = await env.client()
        try:
            assert await (await c.get("/api/ha/devices")).json() == {"connected": False, "registry": False, "devices": []}
        finally:
            await c.close()

    run(go())


def test_plan_put_and_state(tmp_path):
    from test_model import PLAN

    async def go():
        env = Env(tmp_path)
        c = await env.client()
        try:
            assert (await (await c.get("/api/state")).json())["plan"] == {"floors": []}
            assert (await c.put("/api/plan", json=PLAN)).status == 200
            st = await (await c.get("/api/state")).json()
            assert st["plan"]["floors"][0]["items"] == {"l2.1": [200, 90], "n5": [120, 80]}
            assert (await c.put("/api/plan", json={"floors": [{"id": "!"}]})).status == 400
            assert (await (await c.get("/api/state")).json())["plan"] == st["plan"]
            assert (await (await c.get("/api/export")).json())["plan"] == st["plan"]
        finally:
            await c.close()

    run(go())


def test_static_files_are_not_cached_so_updates_show_up(tmp_path):
    async def go():
        env = Env(tmp_path)
        c = await env.client()
        try:
            for path in ("/static/app.js", "/static/plan.js", "/static/style.css", "/"):
                r = await c.get(path)
                assert r.status == 200 and r.headers.get("Cache-Control") == "no-cache", path
        finally:
            await c.close()

    run(go())


def test_index_pins_scripts_to_their_content(tmp_path):
    async def go():
        env = Env(tmp_path)
        c = await env.client()
        try:
            html = await (await c.get("/")).text()
            v = web.asset_version()
            for f in ("style.css", "plan.js", "groups.js", "app.js"):
                assert f'static/{f}?v={v}"' in html, f
            assert (await c.get(f"/static/app.js?v={v}")).status == 200
        finally:
            await c.close()

    run(go())


YAML_GROUPS = """
kruisschakelaar_lamp_keuken:
  module: kruisschakelaar
  class: Kruisschakelaar
  entities:
    - switch.a
    - light.b
kruisschakelaar_lamp_hal:
  module: kruisschakelaar
  class: Kruisschakelaar
  entities:
    - switch.c
    - switch.d
"""


def test_groups_import_enable_and_delete(tmp_path):
    class StubHa:
        connected, enabled, last_error = True, True, ""

        async def entities(self):
            return [{"entity_id": e, "name": e, "state": "off"} for e in ("switch.a", "light.b", "switch.c")]

    async def go():
        env = Env(tmp_path)
        env.ctx.ha = StubHa()
        c = await env.client()
        try:
            r = await (await c.post("/api/groups/parse", json={"yaml": YAML_GROUPS})).json()
            assert r["ha"] is True and [g["id"] for g in r["groups"]] == ["kruisschakelaar_lamp_keuken", "kruisschakelaar_lamp_hal"]
            keuken, hal = r["groups"]
            assert keuken["missing"] == [] and hal["missing"] == ["switch.d"] and not keuken["exists"]
            assert env.rig.model.groups == {}, "controleren schrijft niets"
            assert (await c.post("/api/groups/parse", json={"yaml": "a: ["})).status == 400
            ok = await c.post("/api/groups", json={"groups": [{"id": g["id"], "name": g["name"], "entities": g["entities"]} for g in r["groups"]]})
            assert ok.status == 200
            st = await (await c.get("/api/state")).json()
            assert [(g["name"], g["enabled"]) for g in st["groups"]] == [("Hal", False), ("Keuken", False)], "nieuwe kringen staan uit"
            assert (await c.put("/api/groups/kruisschakelaar_lamp_keuken", json={"enabled": True})).status == 200
            assert env.rig.model.groups["kruisschakelaar_lamp_keuken"]["enabled"] is True
            assert (await c.put("/api/groups/bestaat-niet", json={"enabled": True})).status == 400
            # een tweede kring met dezelfde entiteit mag niet ook aan
            await c.post("/api/groups", json={"groups": [{"id": "dubbel", "name": "Dubbel", "entities": ["switch.a", "switch.z"]}]})
            bad = await c.put("/api/groups/dubbel", json={"enabled": True})
            assert bad.status == 400 and "switch.a" in (await bad.json())["error"]
            assert (await (await c.post("/api/groups/all-off", json={})).json())["disabled"] == 1
            assert not any(g["enabled"] for g in env.rig.model.groups.values())
            assert (await c.delete("/api/groups/dubbel")).status == 200 and (await c.delete("/api/groups/dubbel")).status == 404
            assert (await (await c.get("/api/export")).json())["groups"].keys() == {"kruisschakelaar_lamp_keuken", "kruisschakelaar_lamp_hal"}
        finally:
            await c.close()

    run(go())


def test_groups_parse_without_ha_still_works_but_cannot_check_entities(tmp_path):
    async def go():
        env = Env(tmp_path)
        c = await env.client()
        try:
            r = await (await c.post("/api/groups/parse", json={"yaml": YAML_GROUPS})).json()
            assert r["ha"] is False and all(g["missing"] is None for g in r["groups"])
        finally:
            await c.close()

    run(go())


def test_group_cmd_endpoint(tmp_path):
    async def go():
        env = Env(tmp_path)
        env.rig.model.set_groups([{"id": "gang", "name": "Gang", "entities": ["switch.a", "switch.b"]}])
        env.rig.engine.ha_snapshot({"switch.a": "off", "switch.b": "off"})
        c = await env.client()
        try:
            r = await c.post("/api/groups/gang/cmd", json={"op": "on"})
            assert r.status == 200 and set(env.rig.ha_calls) == {("switch.a", True), ("switch.b", True)}
            env.rig.ha_calls.clear()
            r = await c.post("/api/groups/gang/cmd", json={"op": "toggle", "entity": "switch.a"})
            assert r.status == 200 and env.rig.ha_calls == [("switch.a", True)]
            assert (await c.post("/api/groups/nope/cmd", json={"op": "on"})).status == 404
            assert (await c.post("/api/groups/gang/cmd", json={"op": "boem"})).status == 400
            assert (await c.post("/api/groups/gang/cmd", json={"op": "on", "entity": 5})).status == 400
        finally:
            await c.close()

    run(go())


def test_group_lamp_relay_via_api(tmp_path):
    async def go():
        env = Env(tmp_path)
        env.rig.model.set_groups([{"id": "gang", "name": "Gang", "entities": ["switch.a", "switch.b"]}])
        c = await env.client()
        try:
            assert (await c.put("/api/groups/gang", json={"lamp": ["switch.b"]})).status == 200
            st = await (await c.get("/api/state")).json()
            assert st["groups"][0]["lamp"] == ["switch.b"] and [m["lamp"] for m in st["groups"][0]["members"]] == [False, True]
            assert (await c.put("/api/groups/gang", json={"lamp": ["switch.x"]})).status == 400
            assert (await c.put("/api/groups/gang", json={"enabled": True})).status == 200
            assert env.rig.model.groups["gang"]["lamp"] == ["switch.b"], "andere wijzigingen laten het lamp-relais staan"
            assert (await (await c.get("/api/export")).json())["groups"]["gang"]["lamp"] == ["switch.b"]
        finally:
            await c.close()

    run(go())


def test_group_entities_can_be_added_and_removed_via_api(tmp_path):
    async def go():
        env = Env(tmp_path)
        env.rig.model.set_groups([{"id": "gang", "name": "Gang", "entities": ["switch.a", "switch.b"]}])
        env.rig.model.set_group("gang", lamp=["switch.b"])
        c = await env.client()
        try:
            ok = await c.put("/api/groups/gang", json={"name": "Gang boven", "entities": ["switch.a", "switch.b", "light.c"]})
            assert ok.status == 200 and env.rig.model.groups["gang"]["entities"] == ["switch.a", "switch.b", "light.c"]
            assert env.rig.model.groups["gang"]["name"] == "Gang boven" and env.rig.model.groups["gang"]["lamp"] == ["switch.b"]
            ok = await c.put("/api/groups/gang", json={"entities": ["switch.a", "light.c"]})
            assert ok.status == 200 and env.rig.model.groups["gang"]["lamp"] == [], "het lamp-relais is uit de kring: de keuze valt weg"
            assert (await c.put("/api/groups/gang", json={"entities": ["switch.a", "geen id"]})).status == 400
            assert env.rig.model.groups["gang"]["entities"] == ["switch.a", "light.c"], "een afgewezen wijziging verandert niets"
            # een nieuwe kring met een eigen id
            ok = await c.post("/api/groups", json={"groups": [{"id": "hal", "name": "Hal", "entities": ["switch.x", "switch.y"]}]})
            assert ok.status == 200 and env.rig.model.groups["hal"]["enabled"] is False
        finally:
            await c.close()

    run(go())
