import asyncio
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from kkaddon import proto, schema, web
from kkaddon.model import Model, ModelError
from kkaddon.settings import Settings
from rig import KEY, Rig

DEVICES = [
    {"id": "dev1", "name": "Touch keuken", "area": "Keuken", "platform": "esphome", "entities": [
        {"entity_id": "switch.touch_relay_10", "name": "", "state": "off"},
        {"entity_id": "switch.touch_relay_2", "name": "", "state": "off"},
        {"entity_id": "switch.touch_status_led", "name": "", "state": "off"},
    ]},
    {"id": "dev2", "name": "Dingtian", "area": "Technische ruimte", "platform": "mqtt", "entities": [
        {"entity_id": "switch.dingtian_relay_1", "name": "", "state": "off"},
    ]},
]


def make(tmp_path, entities=("switch.touch_relay_2", "switch.dingtian_relay_1")):
    r = Rig(tmp_path)
    r.model.set_groups([{"id": "spots", "name": "Spots keuken", "entities": list(entities)}])
    r.model.set_group("spots", lamp=[e for e in entities if "dingtian" in e])
    return r


def blocks(r):
    return {b["key"]: b for b in schema.view(r.model, r.engine.snapshot())["blocks"]}


def test_convert_makes_room_pages_with_the_wires_as_they_are(tmp_path):
    r = make(tmp_path)
    before, rev = json.dumps(r.model.groups, sort_keys=True), r.model.rev
    res = schema.convert(r.model, r.engine.snapshot, DEVICES)
    assert json.dumps(r.model.groups, sort_keys=True) == before and r.model.rev == rev, "omzetten raakt geen schakeling"
    assert res["placed"] == 3 and res["switches"] == 2
    assert {p["name"] for p in r.model.schema["pages"]} == {"Keuken", "Technische ruimte"}
    b = blocks(r)
    lamp = b["g:spots"]
    assert [c["sw"] for c in lamp["inputs"]] == ["d:dev1"], "een touch-relais is een ingang"
    assert [c["sw"] for c in lamp["outputs"]] == ["d:dev2"], "het lamp-relais is de uitgang"
    assert lamp["page"] == b["d:dev1"]["page"] == "keuken", "de lamp hoort bij de ruimte in zijn naam"
    assert b["d:dev2"]["page"] == "technische_ruimte"
    assert [g["label"] for g in b["d:dev1"]["gangs"]] == ["2", "10"], "natuurlijke volgorde, geen status-LED"
    assert lamp["inputs"][0]["label"] == "2" and lamp["inputs"][0]["swName"] == "Touch keuken"


def test_convert_twice_leaves_what_stands(tmp_path):
    r = make(tmp_path)
    schema.convert(r.model, r.engine.snapshot, DEVICES)
    schema.place(r.model, r.engine.snapshot(), "g:spots", "keuken", 500, 700)
    again = schema.convert(r.model, r.engine.snapshot, DEVICES)
    assert again["placed"] == 0 and again["switches"] == 0
    assert r.model.schema["blocks"]["g:spots"] == {"page": "keuken", "x": 500, "y": 700}


def test_convert_without_registry_groups_on_entity_name(tmp_path):
    r = make(tmp_path, ("switch.touch01_relay_1", "switch.touch01_relay_2", "light.t4b_x_relay_1", "switch.gang"))
    r.model.set_group("spots", lamp=["light.t4b_x_relay_1"])
    schema.convert(r.model, r.engine.snapshot, [])
    assert {p["name"] for p in r.model.schema["pages"]} == {schema.NO_ROOM, "Spots keuken"}, "schakelaars zonder ruimte naar Centraal"
    sw = r.model.schema["switches"]
    assert [g["entity"] for g in sw["touch01"]["gangs"]] == ["switch.touch01_relay_1", "switch.touch01_relay_2"]
    assert set(sw) == {"touch01", "t4b_x", "gang"}


def test_convert_places_the_lamp_with_most_of_its_touches_and_skips_idle_devices(tmp_path):
    r = Rig(tmp_path)
    r.model.set_groups([{"id": "x", "name": "Plafond", "entities": ["switch.a_1", "switch.b_1", "switch.c_1"]}])
    devs = [{"id": "a", "name": "A", "area": "Hal", "entities": [{"entity_id": "switch.a_1"}]},
            {"id": "b", "name": "B", "area": "Hal", "entities": [{"entity_id": "switch.b_1"}]},
            {"id": "c", "name": "C", "area": "Living", "entities": [{"entity_id": "switch.c_1"}]},
            {"id": "idle", "name": "Ongebruikt", "area": "Zolder", "entities": [{"entity_id": "switch.z_1"}]}]
    schema.convert(r.model, r.engine.snapshot, devs)
    assert blocks(r)["g:x"]["page"] == "hal"
    assert "d:idle" not in blocks(r) and "zolder" not in [p["id"] for p in r.model.schema["pages"]]


def test_wire_on_a_ha_lamp_changes_the_group_and_nothing_else(tmp_path):
    r = make(tmp_path)
    w = lambda d, e, **k: schema.wire(r.model, r.engine, "g:spots", d, {"entity": e}, **k)
    w("in", "switch.extra")
    assert r.model.groups["spots"]["entities"][-1] == "switch.extra" and r.model.groups["spots"]["lamp"] == ["switch.dingtian_relay_1"]
    w("out", "switch.extra")
    assert r.model.groups["spots"]["lamp"] == ["switch.dingtian_relay_1", "switch.extra"], "van ingang naar uitgang, geen dubbel lid"
    assert r.model.groups["spots"]["entities"].count("switch.extra") == 1
    w("in", "switch.extra")
    assert r.model.groups["spots"]["lamp"] == ["switch.dingtian_relay_1"]
    w("in", "switch.extra", remove=True)
    assert "switch.extra" not in r.model.groups["spots"]["entities"]
    w("out", "switch.dingtian_relay_1", remove=True)
    w("in", "switch.touch_relay_2", remove=True)
    assert r.model.groups["spots"]["entities"] == [], "een lamp mag leeg zijn"


def test_wire_respects_that_an_active_entity_has_one_owner(tmp_path):
    r = make(tmp_path)
    r.model.set_group("spots", enabled=True)
    gid = r.model.add_group("Andere")
    r.model.set_group(gid, enabled=True)
    with pytest.raises(ModelError, match="wordt al bestuurd"):
        schema.wire(r.model, r.engine, f"g:{gid}", "in", {"entity": "switch.touch_relay_2"})
    assert r.model.groups[gid]["entities"] == []


def test_wire_on_a_network_light_edits_triggers_and_its_output_is_fixed(tmp_path):
    r = Rig(tmp_path)
    r.heartbeat(1, relays=[(False, 0)], nbtn=3, nind=3, cfg_hash=proto.config_hash([[]], [None] * 3))
    r.heartbeat(2, relays=[(False, 0)] * 2, nbtn=0, nind=0, cfg_hash=proto.config_hash([[]] * 2, []))
    r.model.set_light(2, 0, name="Plafond")
    schema.wire(r.model, r.engine, "l2.0", "in", {"node": 1, "btn": 2})
    assert [(t.node, t.btn) for t in r.model.lights[(2, 0)].triggers] == [(1, 2)]
    b = blocks(r)
    assert b["l2.0"]["inputs"][0]["sw"] == "n1" and b["l2.0"]["outputs"][0]["fixed"] and b["l2.0"]["outputs"][0]["sw"] == "n2"
    assert [g["touch"] for g in b["n1"]["gangs"]] == [True] * 3 and [g["relay"] for g in b["n1"]["gangs"]] == [True, False, False]
    with pytest.raises(ModelError, match="ligt vast"):
        schema.wire(r.model, r.engine, "l2.0", "out", {"node": 2, "relay": 1})
    with pytest.raises(ModelError, match="extern apparaat"):
        schema.wire(r.model, r.engine, "l2.0", "in", {"entity": "switch.los"})
    schema.wire(r.model, r.engine, "l2.0", "in", {"node": 1, "btn": 2}, remove=True)
    assert r.model.lights[(2, 0)].triggers == []


def test_a_real_node_cannot_join_a_ha_lamp_but_an_external_gang_can(tmp_path):
    r = make(tmp_path)
    with pytest.raises(ModelError, match="geflashte node"):
        schema.wire(r.model, r.engine, "g:spots", "in", {"node": 1, "btn": 0})
    vid = r.model.set_virtual(None, "Tuya", "Hal", [{"entity": "switch.tuya_1", "name": "Links", "lamp": False}])
    schema.wire(r.model, r.engine, "g:spots", "in", {"node": vid, "btn": 0})
    assert "switch.tuya_1" in r.model.groups["spots"]["entities"]


def test_an_external_device_takes_over_the_gang_from_a_ha_block(tmp_path):
    r = make(tmp_path)
    schema.convert(r.model, r.engine.snapshot, DEVICES)
    vid = r.model.set_virtual(None, "Dingtian", "", [{"entity": "switch.dingtian_relay_1", "name": "R1", "lamp": False}])
    b = blocks(r)
    assert b["g:spots"]["outputs"][0]["sw"] == f"n{vid}"
    assert "d:dev2" not in b, "een blok zonder eigen gangen verdwijnt"


def test_a_wire_without_a_block_stays_visible(tmp_path):
    r = make(tmp_path)
    c = blocks(r)["g:spots"]["inputs"][0]
    assert c["sw"] is None and c["text"] == "switch.touch_relay_2"


def test_schema_is_stored_exported_and_validated(tmp_path):
    r = make(tmp_path)
    schema.convert(r.model, r.engine.snapshot, DEVICES)
    again = Model(tmp_path / "network.json")
    assert again.schema == r.model.schema
    d = r.model.to_dict()
    d["schema"]["blocks"]["g:spots"]["page"] = "bestaat_niet"
    with pytest.raises(ModelError, match="onbekende ruimte"):
        r.model.load_dict(d)
    for bad in ({"pages": "x"}, {"pages": [{"id": "A B"}]}, {"pages": [{"id": "a"}, {"id": "a"}]},
                {"switches": {"x": {"gangs": []}}}, {"switches": {"x": {"gangs": [{"entity": "nope"}]}}},
                {"pages": [{"id": "a"}], "blocks": {"rare sleutel": {"page": "a", "x": 0, "y": 0}}},
                {"pages": [{"id": "a"}], "blocks": {"n1": {"page": "a", "x": -1, "y": 0}}}):
        with pytest.raises(ModelError):
            r.model.set_schema(bad)


def test_old_file_without_schema_stays_the_same(tmp_path):
    m = Model(tmp_path / "n.json")
    m.set_node(1, name="A")
    assert "schema" not in m.to_dict()


def test_deleting_a_page_returns_its_blocks_but_keeps_the_wires(tmp_path):
    r = make(tmp_path)
    schema.convert(r.model, r.engine.snapshot, DEVICES)
    schema.set_pages(r.model, [p for p in r.model.schema["pages"] if p["id"] != "keuken"])
    b = blocks(r)
    assert b["g:spots"]["page"] is None and b["d:dev2"]["page"] == "technische_ruimte"
    assert b["g:spots"]["inputs"][0]["sw"] == "d:dev1"


def test_api_flow(tmp_path):
    async def go():
        rig = make(tmp_path)
        s = Settings(data_dir=tmp_path, key=KEY, key_generated=False, dev_mode=True)
        c = TestClient(TestServer(web.make_app(web.Ctx(s, rig.engine))))
        await c.start_server()
        try:
            st = await (await c.get("/api/state")).json()
            assert st["schema"]["pages"] == [] and st["schema"]["blocks"][0]["key"] == "g:spots"
            r = await c.post("/api/schema/convert", json={})
            assert (await r.json())["registry"] is False
            page = rig.model.schema["pages"][0]["id"]
            assert (await c.post("/api/schema/place", json={"key": "g:spots", "page": page, "x": 100, "y": 200})).status == 200
            assert rig.model.schema["blocks"]["g:spots"]["x"] == 100
            assert (await c.post("/api/schema/place", json={"key": "g:bestaatniet", "page": page})).status == 400
            assert (await c.post("/api/schema/place", json={"key": "g:spots", "page": "nergens"})).status == 400
            # nieuwe lamp, nieuwe schakelaar, en een draad ertussen
            k = (await (await c.post("/api/schema/lamp", json={"name": "Nieuw", "page": page, "x": 10, "y": 10})).json())["key"]
            assert rig.model.groups[k[2:]]["enabled"] is False, "een nieuwe lamp staat uit"
            assert (await c.post("/api/schema/lamp", json={"name": ""})).status == 400
            sw = await (await c.post("/api/schema/switch", json={"name": "Touch hal", "page": page, "gangs": [{"entity": "switch.hal_1", "name": "Links"}]})).json()
            assert sw["key"] == "d:touch_hal"
            assert (await c.post("/api/schema/wire", json={"lamp": k, "dir": "in", "ref": {"entity": "switch.hal_1"}})).status == 200
            st = await (await c.get("/api/state")).json()
            lamp = next(b for b in st["schema"]["blocks"] if b["key"] == k)
            assert lamp["inputs"][0]["sw"] == "d:touch_hal" and lamp["inputs"][0]["label"] == "Links"
            assert (await c.post("/api/schema/wire", json={"lamp": k, "dir": "zij", "ref": {}})).status == 400
            assert (await c.put("/api/schema/switch/touch_hal", json={"name": "Hal", "gangs": [{"entity": "switch.hal_1", "name": "L"}]})).status == 200
            assert (await c.delete("/api/schema/switch/touch_hal")).status == 200
            assert (await c.delete("/api/schema/switch/touch_hal")).status == 404
            assert (await c.put("/api/schema/pages", json={"pages": [{"id": "a", "name": "A"}]})).status == 200
            assert rig.model.schema["blocks"] == {}
            assert (await (await c.get("/api/export")).json())["schema"]["pages"] == [{"id": "a", "name": "A"}]
            html = await (await c.get("/")).text()
            assert "static/schema.js?v=" in html and (await c.get("/static/schema.js")).status == 200
        finally:
            await c.close()

    asyncio.run(go())


def test_rooms_come_from_lamp_names_and_switches_without_a_room_go_to_centraal(tmp_path):
    r = Rig(tmp_path)
    names = ["Keuken", "Buiten keuken", "Buiten terras", "Quarto philippe", "Quarto philippe bed links", "Quarto das visitas", "Quarto das visitas badkamer", "Hal"]
    r.model.set_groups([{"id": f"g{i}", "name": n, "entities": [f"switch.t{i}_1", f"switch.box_relay_{i}"]} for i, n in enumerate(names)])
    res = schema.convert(r.model, r.engine.snapshot, [])
    rooms = {p["id"]: p["name"] for p in r.model.schema["pages"]}
    room = {n: rooms[r.model.schema["blocks"][f"g:g{i}"]["page"]] for i, n in enumerate(names)}
    assert room == {"Keuken": "Keuken", "Buiten keuken": "Buiten", "Buiten terras": "Buiten", "Quarto philippe": "Quarto philippe",
                    "Quarto philippe bed links": "Quarto philippe", "Quarto das visitas": "Quarto das visitas",
                    "Quarto das visitas badkamer": "Quarto das visitas", "Hal": "Hal"}
    b = r.model.schema["blocks"]
    assert b["d:box"]["page"] == b["d:t0"]["page"] == "centraal" and res["placed"] == len(b)
    here = [v for v in b.values() if v["page"] == "centraal"]
    assert len({(v["x"], v["y"]) for v in here}) == len(here) and len({v["x"] for v in here}) > 1, "een raster, niets op elkaar"


def test_a_new_layout_honours_the_area_of_a_block_that_already_exists(tmp_path):
    r = make(tmp_path)
    schema.convert(r.model, r.engine.snapshot, [])
    sid = next(k for k, v in r.model.schema["switches"].items() if v["gangs"][0]["entity"] == "switch.touch_relay_2")
    devs = [{"id": sid, "name": "T", "area": "Zolder", "entities": [{"entity_id": "switch.touch_relay_2"}]}]
    schema.reset_layout(r.model)
    assert r.model.schema["blocks"] == {} and r.model.schema["switches"], "de blokken zelf blijven"
    schema.convert(r.model, r.engine.snapshot, devs)
    assert r.model.schema["blocks"][f"d:{sid}"]["page"] == "zolder"
