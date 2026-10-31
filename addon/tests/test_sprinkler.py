"""De sprinkler: volgorde en tijden, start op de klok, blokkeren, herhalen, en wat er bij een fout gebeurt."""
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from aiohttp.test_utils import TestClient, TestServer

from kkaddon import schema, web
from kkaddon.model import Model, ModelError
from kkaddon.settings import Settings
from rig import KEY, Rig

Z1, Z2, Z3 = "switch.klep_1", "switch.klep_2", "switch.klep_3"


def at(rig, hour, minute=0, second=0):
    rig.wall = datetime(2026, 11, 2, hour, minute, second, tzinfo=ZoneInfo("Europe/Lisbon")).timestamp()  # een maandag


def make(tmp_path, **fields):
    rig = Rig(tmp_path)
    rig.engine.set_tz("Europe/Lisbon")
    sid = rig.model.add_sprinkler("Tuin", 3)
    zones = [{"name": f"Z{i + 1}", "entity": e, "seconds": s} for i, (e, s) in enumerate(((Z1, 10), (Z2, 20), (Z3, 30)))]
    rig.model.set_sprinkler(sid, **{"enabled": True, "zones": zones, "cycles": 2, "wait": 1, "refresh": 600, "start": "06:00", **fields})
    at(rig, 5)
    rig.advance(1.1, step=0.1)  # de eerste aansturing: alles uit
    rig.ha_calls.clear()
    return rig, rig.engine, sid


def ons(rig):
    return [c for c in rig.ha_calls if c[1]]


def test_zones_run_one_after_the_other_with_the_wait_between_them_for_all_cycles(tmp_path):
    rig, e, sid = make(tmp_path)
    e.sprinklers.start(sid)
    assert rig.ha_calls == [(Z1, True)], "zone 1 meteen aan"
    rig.ha_calls.clear()
    rig.advance(11.5, step=0.1)  # de aansturing loopt per seconde
    assert rig.ha_calls == [(Z1, False)], "na zijn tijd uit, en zone 2 wacht"
    assert e.sprinklers.status(sid)["zone"] is None and e.sprinklers.status(sid)["next"] == 1
    rig.advance(57, step=0.1)
    assert ons(rig) == []
    rig.advance(3, step=0.1)
    assert ons(rig) == [(Z2, True)], "na de wachttijd zone 2"
    assert e.sprinklers.status(sid)["zone"] == 1
    rig.ha_calls.clear()
    rig.advance(19 + 60 + 30 + 60 + 10 + 60 + 20 + 60 + 30 + 2, step=0.1)
    assert ons(rig) == [(Z3, True), (Z1, True), (Z2, True), (Z3, True)]
    assert rig.ha_calls[-1] == (Z3, False)
    assert e.sprinklers.status(sid)["running"] is False
    assert rig.events_of("sprinkler", what="done")


def test_it_starts_on_the_clock_once_on_the_chosen_days_and_never_catches_up(tmp_path):
    rig, e, sid = make(tmp_path, cycles=1, wait=0, days=[0])
    at(rig, 6, 0, 0)
    rig.advance(1.1, step=0.1)
    assert ons(rig) == [(Z1, True)]
    rig.ha_calls.clear()
    rig.advance(70, step=0.1)
    assert e.sprinklers.status(sid)["running"] is False
    assert len(rig.events_of("sprinkler", what="start")) == 1, "de beurt was binnen de minuut klaar: geen tweede"
    (tmp_path / "b").mkdir()
    rig, e, sid = make(tmp_path / "b", cycles=1, days=[0])
    at(rig, 6, 1)  # de minuut gemist
    rig.advance(5, step=0.1)
    assert e.sprinklers.status(sid)["running"] is False
    rig.model.set_sprinkler(sid, days=[1])
    at(rig, 6, 0)
    rig.advance(2, step=0.1)
    assert e.sprinklers.status(sid)["running"] is False, "niet op deze dag"


def test_a_short_run_does_not_start_twice_in_the_same_minute(tmp_path):
    rig, e, sid = make(tmp_path, cycles=1, wait=0, zones=[{"name": "Z", "entity": Z1, "seconds": 2}])
    at(rig, 6)
    rig.advance(10, step=0.1)
    assert len(rig.events_of("sprinkler", what="start")) == 1


def test_inputs_win_over_the_panel_and_unknown_falls_back_or_does_not_start(tmp_path):
    rig, e, sid = make(tmp_path)
    m = rig.model
    tijd, cycli, wacht = m.add_module("time", "Start", "07:30"), m.add_module("number", "Cycli", 3), m.add_module("number", "Wacht", 0)
    m.set_sprinkler(sid, inputs={"start": f"m:{tijd}", "cycles": f"m:{cycli}", "wait": f"m:{wacht}"})
    at(rig, 6)
    rig.advance(2, step=0.1)
    assert e.sprinklers.status(sid)["running"] is False, "het paneel zegt 06:00, maar de ingang gaat voor"
    at(rig, 7, 30)
    rig.advance(2, step=0.1)
    st = e.sprinklers.status(sid)
    assert st["running"] and st["cycles"] == 3 and abs(st["left"] - 3 * 60) <= 2
    e.sprinklers.stop(sid)
    zon = m.add_module("sunrise")
    m.set_sprinkler(sid, inputs={"start": f"m:{zon}"})
    at(rig, 6)
    rig.advance(2, step=0.1)
    assert e.sprinklers.status(sid)["running"] is False, "geen locatie: het startuur is onbekend, dus er start niets"


def test_settings_changed_during_a_run_only_count_from_the_next_run(tmp_path):
    rig, e, sid = make(tmp_path, cycles=1)
    e.sprinklers.start(sid)
    left = e.sprinklers.status(sid)["left"]
    rig.model.set_sprinkler(sid, cycles=5, wait=30)
    assert e.sprinklers.status(sid)["left"] == left


def test_blocking_stops_a_run_and_the_valve_closes_at_once_and_prevents_a_start(tmp_path):
    rig, e, sid = make(tmp_path)
    tid = rig.model.save_timer(None, "Regen", {})
    rig.model.set_sprinkler(sid, inputs={"block": f"t:{tid}"})
    e.sprinklers.start(sid)
    rig.ha_calls.clear()
    rig.model.save_timer(tid, "Regen", {"mon": [{"from": "05:00", "to": "07:00"}]})
    rig.advance(1.1, step=0.1)
    assert e.sprinklers.status(sid)["running"] is False and (Z1, False) in rig.ha_calls
    assert rig.events_of("sprinkler", what="blocked")
    with pytest.raises(ModelError, match="blokkeren"):
        e.sprinklers.start(sid)
    at(rig, 6)
    rig.advance(2, step=0.1)
    assert e.sprinklers.status(sid)["running"] is False


def test_the_state_is_sent_again_every_refresh_on_and_off(tmp_path):
    rig, e, sid = make(tmp_path, refresh=30, cycles=1, zones=[{"name": "Z", "entity": Z1, "seconds": 100}, {"name": "Y", "entity": Z2, "seconds": 5}])
    rig.advance(31, step=0.1)
    rig.ha_calls.clear()
    e.sprinklers.start(sid)
    rig.advance(65, step=0.1)
    assert rig.ha_calls.count((Z1, True)) == 3, "bij de start en twee keer herhaald"
    assert rig.ha_calls.count((Z2, False)) == 2, "ook uit wordt herhaald"


def test_after_a_restart_the_run_is_gone_and_the_valve_closes(tmp_path):
    rig, e, sid = make(tmp_path)
    e.sprinklers.start(sid)
    rig.ha_calls.clear()
    e = rig.restart()
    e.set_tz("Europe/Lisbon")
    rig.advance(1.1, step=0.1)
    assert e.sprinklers.status(sid)["running"] is False
    assert (Z1, False) in rig.ha_calls and ons(rig) == []


def test_without_ha_time_goes_on_and_nothing_is_caught_up(tmp_path):
    rig, e, sid = make(tmp_path, cycles=1, wait=0)
    e.sprinklers.start(sid)
    e.virt.set_link(False)
    rig.ha_calls.clear()
    rig.advance(15, step=0.1)
    assert rig.ha_calls == []
    e.virt.set_link(True)
    rig.advance(1.1, step=0.1)
    assert (Z1, False) in rig.ha_calls and (Z2, True) in rig.ha_calls, "zone 1 is voorbij, zone 2 is aan de beurt"


def test_a_failed_command_is_retried_and_logged_once(tmp_path):
    rig, e, sid = make(tmp_path)
    calls = []
    e.ha_call = lambda ent, on: calls.append((ent, on)) or False
    e.sprinklers.start(sid)
    rig.advance(11, step=0.1)
    assert calls.count((Z1, True)) >= 2, "opnieuw geprobeerd"
    assert len([x for x in rig.events_of("sprinkler", what="failed") if x["entity"] == Z1]) == 1, "maar één keer in het logboek"


def test_disabling_or_deleting_closes_the_valve_and_leaves_it_alone_after_that(tmp_path):
    rig, e, sid = make(tmp_path)
    e.sprinklers.start(sid)
    rig.ha_calls.clear()
    rig.model.set_sprinkler(sid, enabled=False)
    rig.advance(1.1, step=0.1)
    assert e.sprinklers.status(sid)["running"] is False and sorted(rig.ha_calls) == [(Z1, False), (Z2, False), (Z3, False)]
    rig.ha_calls.clear()
    rig.advance(700, step=0.5)
    assert rig.ha_calls == [], "uit: raakt de relais niet meer aan"
    with pytest.raises(ModelError, match="actief"):
        e.sprinklers.start(sid)
    rig.model.set_sprinkler(sid, enabled=True)
    e.sprinklers.start(sid)
    rig.ha_calls.clear()
    assert rig.model.remove_sprinkler(sid)
    rig.advance(1.1, step=0.1)
    assert (Z1, False) in rig.ha_calls


def test_zones_without_a_relay_or_time_are_skipped_without_a_wait(tmp_path):
    rig, e, sid = make(tmp_path, cycles=1, wait=1, zones=[{"name": "A", "entity": Z1, "seconds": 10}, {"name": "B", "entity": None, "seconds": 10}, {"name": "C", "entity": Z2, "seconds": 0}, {"name": "D", "entity": Z3, "seconds": 10}])
    e.sprinklers.start(sid)
    assert e.sprinklers.status(sid)["left"] == 10 + 60 + 10
    rig.model.set_sprinkler(sid, zones=[{"name": "A", "entity": None, "seconds": 10}])
    e.sprinklers.stop(sid)
    with pytest.raises(ModelError, match="niets te doen"):
        e.sprinklers.start(sid)


def test_validation_and_storage(tmp_path):
    m = Model(tmp_path / "n.json")
    sid = m.add_sprinkler()
    assert m.sprinklers[sid]["enabled"] is False, "een nieuwe sprinkler staat uit"
    assert len(m.sprinklers[sid]["zones"]) == 3 and m.sprinklers[sid]["refresh"] == 30
    z = m.add_module("sunrise")
    en = m.add_module("and")
    getal = m.add_module("number", "Tien", 10)
    for bad, why in (({"zones": []}, "1 tot"), ({"zones": [{}] * 17}, "1 tot"), ({"zones": [{"entity": Z1}, {"entity": Z1}]}, "twee zones"), ({"zones": [{"entity": "rommel"}]}, "ongeldige"),
                     ({"zones": [{"seconds": -1}]}, "seconden"), ({"cycles": 0}, "cycli"), ({"wait": -1}, "wachttijd"), ({"refresh": 1}, "herhalen"), ({"start": "25:00"}, "UU:MM"), ({"days": [7]}, "dag"),
                     ({"name": " "}, "naam"), ({"rommel": 1}, "onbekend veld"), ({"inputs": {"start": f"m:{en}"}}, "een tijd"), ({"inputs": {"cycles": f"m:{z}"}}, "een getal"),
                     ({"inputs": {"block": f"m:{getal}"}}, "0 of 1"), ({"inputs": {"block": "m:weg"}}, "bestaat"), ({"inputs": {"rommel": f"m:{z}"}}, "onbekende ingang")):
        with pytest.raises(ModelError, match=why):
            m.set_sprinkler(sid, **bad)
    with pytest.raises(ModelError):
        m.add_module("number", "Te veel", 100001)
    m.set_sprinkler(sid, inputs={"start": f"m:{z}", "cycles": f"m:{getal}"})
    again = Model(tmp_path / "n.json")
    assert again.sprinklers == m.sprinklers and again.modules[getal]["value"] == 10
    assert m.remove_module(z) and m.sprinklers[sid]["inputs"] == {"cycles": f"m:{getal}"}, "een verwijderde bron is los"
    m.load_dict(m.to_dict())
    d = m.to_dict()
    d["sprinklers"][sid]["inputs"] = {"start": "m:weg"}
    with pytest.raises(ModelError):
        m.load_dict(d)


def test_old_file_without_sprinklers_stays_the_same(tmp_path):
    m = Model(tmp_path / "n.json")
    m.set_node(1, name="A")
    assert "sprinklers" not in m.to_dict()


def test_one_relay_has_one_owner(tmp_path):
    m = Model()
    m.set_groups([{"id": "g", "name": "G", "entities": [Z1, "switch.x"]}])
    m.set_group("g", enabled=True)
    a, b = m.add_sprinkler("A"), m.add_sprinkler("B")
    with pytest.raises(ModelError, match="wordt al bestuurd"):
        m.set_sprinkler(a, enabled=True, zones=[{"entity": Z1}])
    m.set_sprinkler(a, enabled=False, zones=[{"entity": Z1}])  # uit: geen probleem
    m.set_group("g", enabled=False)
    m.set_sprinkler(a, enabled=True)
    with pytest.raises(ModelError, match="wordt al bestuurd"):
        m.set_group("g", enabled=True)
    with pytest.raises(ModelError, match="wordt al bestuurd"):
        m.set_sprinkler(b, enabled=True, zones=[{"entity": Z1}])
    m.set_virtual(None, "Tuya", "", [{"entity": Z2, "name": "K", "lamp": False}])
    m.set_sprinkler(b, enabled=True, zones=[{"entity": Z2}])
    assert m.sprinklers[b]["zones"][0]["entity"] == Z2, "een extern apparaat mag: dat geeft zelf geen bevelen"


def test_it_is_a_block_and_the_api_works(tmp_path):
    async def go():
        rig = Rig(tmp_path)
        s = Settings(data_dir=tmp_path, key=KEY, key_generated=False, dev_mode=True)
        c = TestClient(TestServer(web.make_app(web.Ctx(s, rig.engine))))
        await c.start_server()
        try:
            await c.put("/api/schema/pages", json={"pages": [{"id": "a", "name": "A"}]})
            k = (await (await c.post("/api/sprinklers", json={"page": "a", "x": 10, "y": 20})).json())["key"]
            assert k == "s:sprinkler" and rig.model.schema["blocks"][k]["page"] == "a"
            bad = await c.put("/api/sprinklers/sprinkler", json={"cycles": 0})
            assert bad.status == 400 and "cycli" in (await bad.json())["error"]
            no = await c.post("/api/sprinklers/sprinkler/run", json={"op": "start"})
            assert no.status == 400 and "actief" in (await no.json())["error"]
            assert (await c.put("/api/sprinklers/sprinkler", json={"enabled": True, "zones": [{"name": "Z", "entity": Z1, "seconds": 60}]})).status == 200
            assert (await c.post("/api/sprinklers/sprinkler/run", json={"op": "start"})).status == 200
            assert rig.ha_calls[-1] == (Z1, True)
            st = await (await c.get("/api/state")).json()
            b = next(x for x in st["schema"]["blocks"] if x["key"] == k)
            assert b["kind"] == "sprinkler" and b["status"]["zone"] == 0 and [p["name"] for p in b["ports"]] == ["start", "cycles", "wait", "block"]
            assert (await c.post("/api/sprinklers/sprinkler/run", json={"op": "stop"})).status == 200
            assert rig.ha_calls[-1] == (Z1, False)
            assert (await c.post("/api/sprinklers/sprinkler/run", json={"op": "rommel"})).status == 400
            assert (await (await c.get("/api/export")).json())["sprinklers"]["sprinkler"]["enabled"] is True
            assert (await c.delete("/api/sprinklers/sprinkler")).status == 200 and (await c.delete("/api/sprinklers/sprinkler")).status == 404
            assert k not in rig.model.schema["blocks"]
        finally:
            await c.close()

    asyncio.run(go())


def test_the_automatic_layout_places_a_sprinkler(tmp_path):
    r = Rig(tmp_path)
    r.model.add_sprinkler("Tuin", 5)
    schema.convert(r.model, r.engine.snapshot, [])
    assert r.model.schema["blocks"]["s:tuin"]["page"] == "centraal"
