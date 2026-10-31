"""Tijd- en logicamodules: rekenen, vergelijken, de zon, de koppelingen en de disable-ingang van een lamp."""
import asyncio
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from aiohttp.test_utils import TestClient, TestServer

from fakeha import FakeHa
from kkaddon import schema, web
from kkaddon.logic import evaluate, sun_times
from kkaddon.model import Model, ModelError
from kkaddon.settings import Settings
from rig import KEY, Rig
from test_habridge import Env, run

A, B = "switch.a", "switch.b"
LISBON, BRUSSELS = (38.72, -9.14, ZoneInfo("Europe/Lisbon")), (50.85, 4.35, ZoneInfo("Europe/Brussels"))


def hm(t):
    return int(t[:2]) * 60 + int(t[3:])


def calc(modules, now="12:00", sun=(hm("07:00"), hm("20:00")), timers=None):
    full = {k: {"name": k, "inputs": {}, **v} for k, v in modules.items()}
    return evaluate(full, lambda t: (timers or {}).get(t), hm(now), sun)


T1, T2 = {"type": "time", "value": "22:00"}, {"type": "time", "value": "02:30"}


@pytest.mark.parametrize("kind,want", [("add", "00:30"), ("sub", "19:30")])
def test_adding_and_subtracting_times_wraps_around_midnight(kind, want):
    v = calc({"x": T1, "y": T2, "r": {"type": kind, "inputs": {"a": "m:x", "b": "m:y"}}})
    assert v["r"] == hm(want)


def test_subtracting_past_midnight_counts_back_from_24h():
    v = calc({"x": T2, "y": T1, "r": {"type": "sub", "inputs": {"a": "m:x", "b": "m:y"}}})
    assert v["r"] == hm("04:30")


@pytest.mark.parametrize("now,want", [("21:59", False), ("22:00", True), ("23:59", True), ("00:00", True), ("02:29", True), ("02:30", False), ("12:00", False)])
def test_between_runs_over_midnight_when_from_is_later_than_to(now, want):
    v = calc({"x": T1, "y": T2, "r": {"type": "between", "inputs": {"from": "m:x", "to": "m:y"}}}, now=now)
    assert v["r"] is want


@pytest.mark.parametrize("now,want", [("02:29", False), ("02:30", True), ("21:59", True), ("22:00", False)])
def test_between_within_one_day_includes_the_start_and_not_the_end(now, want):
    v = calc({"x": T2, "y": T1, "r": {"type": "between", "inputs": {"from": "m:x", "to": "m:y"}}}, now=now)
    assert v["r"] is want


def test_between_two_equal_times_is_never_true():
    assert calc({"x": T1, "r": {"type": "between", "inputs": {"from": "m:x", "to": "m:x"}}}, now="22:00")["r"] is False


@pytest.mark.parametrize("now,after,before", [("02:29", False, True), ("02:30", True, False), ("23:00", True, False)])
def test_after_and_before_are_exact_opposites(now, after, before):
    v = calc({"x": T2, "n": {"type": "after", "inputs": {"time": "m:x"}}, "v": {"type": "before", "inputs": {"time": "m:x"}}}, now=now)
    assert (v["n"], v["v"]) == (after, before)


@pytest.mark.parametrize("a,b,en,of", [(True, True, True, True), (True, False, False, True), (False, True, False, True), (False, False, False, False),
                                       (None, True, None, True), (None, False, False, None), (None, None, None, None)])
def test_and_or_truth_tables_with_unknown(a, b, en, of):
    v = calc({"en": {"type": "and", "inputs": {"a": "t:a", "b": "t:b"}}, "of": {"type": "or", "inputs": {"a": "t:a", "b": "t:b"}}}, timers={"a": a, "b": b})
    assert (v["en"], v["of"]) == (en, of)


def test_unknown_sun_or_a_loose_input_gives_unknown_downstream():
    v = calc({"z": {"type": "sunset"}, "plus": {"type": "add", "inputs": {"a": "m:z", "b": "m:z"}}, "n": {"type": "after", "inputs": {"time": "m:plus"}}, "los": {"type": "before"}}, sun=None)
    assert v == {"z": None, "plus": None, "n": None, "los": None}


def test_a_loop_from_an_old_file_is_unknown_and_does_not_hang():
    v = calc({"a": {"type": "and", "inputs": {"a": "m:b", "b": "m:b"}}, "b": {"type": "or", "inputs": {"a": "m:a", "b": "m:a"}}})
    assert v == {"a": None, "b": None}


@pytest.mark.parametrize("day,place,rise,sets", [(date(2026, 6, 21), LISBON, "06:12", "21:05"), (date(2026, 12, 21), LISBON, "07:50", "17:19"),
                                                 (date(2026, 6, 21), BRUSSELS, "05:29", "22:00"), (date(2026, 12, 21), BRUSSELS, "08:44", "16:40")])
def test_sun_times_match_the_almanac_within_a_few_minutes(day, place, rise, sets):
    r, s = sun_times(day, *place)
    assert abs(r - hm(rise)) <= 3 and abs(s - hm(sets)) <= 3, (r, s)


def test_no_sunset_during_the_polar_day():
    assert sun_times(date(2026, 6, 21), 78.2, 15.6, ZoneInfo("Arctic/Longyearbyen")) is None


# ---------------------------------------------------------------- het model


def test_a_module_is_stored_exported_and_named_after_its_type(tmp_path):
    m = Model(tmp_path / "n.json")
    assert m.add_module("and") == "en" and m.add_module("and") == "en_2" and m.modules["en_2"]["name"] == "En 2"
    t = m.add_module("time", "Pauze", "02:30")
    m.set_module("en", inputs={"a": "m:en_2"})
    again = Model(tmp_path / "n.json")
    assert again.modules == m.modules and again.modules[t]["value"] == "02:30" and again.modules["en"]["inputs"] == {"a": "m:en_2"}
    m.set_module("en", inputs={"a": None})
    assert m.modules["en"]["inputs"] == {}, "None maakt de ingang los"


def test_old_file_without_modules_stays_the_same(tmp_path):
    m = Model(tmp_path / "n.json")
    m.set_node(1, name="A")
    assert "modules" not in m.to_dict()


def test_wrong_connections_are_refused_and_change_nothing(tmp_path):
    m = Model(tmp_path / "n.json")
    z, en, of = m.add_module("sunset"), m.add_module("and"), m.add_module("or")
    tussen = m.add_module("between")
    for mid, inputs, why in ((tussen, {"from": f"m:{en}"}, "verwacht een tijd"), (en, {"a": f"m:{z}"}, "verwacht een 0 of 1"), (en, {"a": "m:weg"}, "bestaat"), (en, {"a": "t:weg"}, "bestaat"),
                             (en, {"c": f"m:{of}"}, "onbekende ingang"), (en, {"a": "rommel"}, "ongeldige bron")):
        with pytest.raises(ModelError, match=why):
            m.set_module(mid, inputs=inputs)
    m.set_module(en, inputs={"a": f"m:{of}"})
    with pytest.raises(ModelError, match="lus"):
        m.set_module(of, inputs={"a": f"m:{en}"})
    with pytest.raises(ModelError, match="lus"):
        m.set_module(en, inputs={"b": f"m:{en}"})
    assert m.modules[of]["inputs"] == {} and m.modules[en]["inputs"] == {"a": f"m:{of}"}
    for bad in (lambda: m.add_module("rommel"), lambda: m.add_module("time", value="25:00"), lambda: m.set_module(en, name=" "), lambda: m.set_module("weg", name="x")):
        with pytest.raises(ModelError):
            bad()


def test_an_import_with_a_bad_module_is_refused_but_an_old_file_still_starts(tmp_path):
    m = Model(tmp_path / "n.json")
    good = m.to_dict()
    bad = {**good, "modules": {"a": {"type": "and", "name": "A", "inputs": {"a": "m:weg"}}}}
    with pytest.raises(ModelError):
        m.load_dict(bad)
    assert m.modules == {}
    m.load_dict(bad, save=False)
    assert "a" in m.modules, "bij het opstarten nooit weigeren"


def test_deleting_a_source_disconnects_everything_that_used_it(tmp_path):
    r = Rig(tmp_path)
    r.model.set_groups([{"id": "g", "name": "G", "entities": [A, B]}])
    r.model.schema["pages"] = [{"id": "p", "name": "P"}]
    tid = r.model.save_timer(None, "Nacht", {})
    z, n, en = r.model.add_module("sunset"), r.model.add_module("after"), r.model.add_module("and")
    r.model.set_module(n, inputs={"time": f"m:{z}"})
    r.model.set_module(en, inputs={"a": f"m:{n}", "b": f"t:{tid}"})
    r.model.set_group("g", disable=f"m:{en}")
    schema.place(r.model, r.engine.snapshot(), f"m:{en}", "p", 0, 0)
    assert r.model.remove_timer(tid) and r.model.modules[en]["inputs"] == {"a": f"m:{n}"}
    assert r.model.remove_module(en) and "disable" not in r.model.groups["g"] and f"m:{en}" not in r.model.schema["blocks"]
    assert r.model.remove_module(z) and r.model.modules[n]["inputs"] == {}
    assert r.model.remove_module("weg") is False


# ---------------------------------------------------------------- de lamp: alleen touches tussen zonsondergang en zonsopgang


def at(rig, hour, minute=0):
    rig.wall = datetime(2026, 6, 21, hour, minute, tzinfo=ZoneInfo("Europe/Lisbon")).timestamp()


def daylight_blocked(tmp_path):
    """Disable = 1 overdag, dus de touch werkt alleen van zonsondergang tot zonsopgang."""
    m = Model()
    m.set_groups([{"id": "g", "name": "G", "entities": [A, B]}])
    m.set_group("g", enabled=True, lamp=[B])
    rig = Rig(tmp_path, model=m)
    e = rig.engine
    e.set_tz("Europe/Lisbon")
    e.set_location(38.72, -9.14)
    e.ha_snapshot({A: "off", B: "off"})
    up, down, tussen = m.add_module("sunrise"), m.add_module("sunset"), m.add_module("between", "Overdag")
    m.set_module(tussen, inputs={"from": f"m:{up}", "to": f"m:{down}"})
    m.set_group("g", disable=f"m:{tussen}")
    return rig, e, tussen


def test_a_touch_only_works_between_sunset_and_sunrise(tmp_path):
    rig, e, _ = daylight_blocked(tmp_path)
    at(rig, 12)
    assert e.snapshot()["groups"][0]["blocked"] is True
    e.ha_state(A, "on")
    assert rig.ha_calls == [(A, False)], "overdag: alleen de touch terug"
    e.ha_state(A, "off")
    rig.ha_calls.clear()
    at(rig, 23)
    assert e.snapshot()["groups"][0]["blocked"] is False
    e.ha_state(A, "on")
    assert rig.ha_calls == [(B, True)], "'s avonds volgt de lamp"


def test_without_a_location_the_lamp_stays_usable(tmp_path):
    rig, e, _ = daylight_blocked(tmp_path)
    e.set_location(None, None)
    at(rig, 12)
    assert e.snapshot()["groups"][0]["blocked"] is False


def test_only_a_zero_or_one_fits_the_disable_input(tmp_path):
    rig, e, _ = daylight_blocked(tmp_path)
    up = rig.model.add_module("sunrise")
    with pytest.raises(ModelError, match="0 of 1"):
        rig.model.set_group("g", disable=f"m:{up}")
    with pytest.raises(ModelError, match="onbekende module"):
        rig.model.set_group("g", disable="m:weg")
    with pytest.raises(ModelError):
        rig.model.set_group("g", disable=5)


def test_blocks_and_the_automatic_layout_put_a_chain_with_its_lamp(tmp_path):
    rig, e, tussen = daylight_blocked(tmp_path)
    at(rig, 12)
    schema.convert(rig.model, rig.engine.snapshot, [])
    b = {x["key"]: x for x in schema.view(rig.model, rig.engine.snapshot())["blocks"]}
    t = b[f"m:{tussen}"]
    assert t["kind"] == "module" and t["value"] is True and t["out"] == "bool" and t["tech"] == "Tussen"
    assert [p["ref"] for p in t["ports"]] == ["m:zonsopgang", "m:zonsondergang"]
    assert isinstance(b["m:zonsondergang"]["value"], int)
    assert t["page"] == b["g:g"]["page"] == b["m:zonsopgang"]["page"] == b["m:zonsondergang"]["page"], "de hele keten bij de lamp"
    here = [(v["x"], v["y"]) for k, v in rig.model.schema["blocks"].items() if k.startswith("m:")]
    assert len(set(here)) == 3, "niets op elkaar"


def test_a_loose_module_goes_to_centraal(tmp_path):
    r = Rig(tmp_path)
    r.model.add_module("sunset")
    schema.convert(r.model, r.engine.snapshot, [])
    assert r.model.schema["blocks"]["m:zonsondergang"]["page"] == "centraal"


def test_module_api(tmp_path):
    async def go():
        rig = Rig(tmp_path)
        rig.model.set_groups([{"id": "g", "name": "G", "entities": [A, B]}])
        s = Settings(data_dir=tmp_path, key=KEY, key_generated=False, dev_mode=True)
        c = TestClient(TestServer(web.make_app(web.Ctx(s, rig.engine))))
        await c.start_server()
        try:
            await c.put("/api/schema/pages", json={"pages": [{"id": "a", "name": "A"}]})
            k = (await (await c.post("/api/modules", json={"type": "time", "page": "a", "x": 10, "y": 20})).json())["key"]
            assert k == "m:vaste_tijd" and rig.model.schema["blocks"][k] == {"page": "a", "x": 10, "y": 20}
            assert (await c.put("/api/modules/vaste_tijd", json={"value": "02:30", "name": "Pauze"})).status == 200
            bad = await c.put("/api/modules/vaste_tijd", json={"value": "2u30"})
            assert bad.status == 400 and "UU:MM" in (await bad.json())["error"]
            assert (await c.post("/api/modules", json={"type": "rommel"})).status == 400
            await c.post("/api/modules", json={"type": "after", "page": "a"})
            assert (await c.put("/api/modules/na", json={"inputs": {"time": k}})).status == 200
            wrong = await c.post("/api/schema/disable", json={"lamp": "g:g", "timer": k})
            assert wrong.status == 400 and "0 of 1" in (await wrong.json())["error"]
            assert (await c.post("/api/schema/disable", json={"lamp": "g:g", "timer": "m:na"})).status == 200
            st = await (await c.get("/api/state")).json()
            by = {b["key"]: b for b in st["schema"]["blocks"]}
            assert by[k]["value"] == 150 and by[k]["param"] == "02:30" and by[k]["name"] == "Pauze"
            assert by["m:na"]["ports"][0]["ref"] == k and by["g:g"]["disable"] == "m:na"
            assert (await (await c.get("/api/export")).json())["modules"]["na"]["inputs"] == {"time": k}
            assert (await c.delete("/api/modules/na")).status == 200 and (await c.delete("/api/modules/na")).status == 404
        finally:
            await c.close()

    asyncio.run(go())


def test_the_location_comes_from_home_assistant():
    async def go():
        async with Env(FakeHa()) as e:
            e.model.add_module("sunset")
            await e.until(lambda: e.engine.location == (38.72, -9.14), what="locatie uit get_config")
            assert isinstance(e.engine.module_values()["zonsondergang"], int)

    run(go())
