"""energyhub: de meters van de huisjes in het energiedashboard van HA, met prijzen, zonder andermans regels te wissen."""
import asyncio

import pytest
from aiohttp.test_utils import TestServer

from fakeha import FakeHa
from kkaddon.energyhub import EnergyHub, clean, merge, parse_price
from kkaddon.engine import Engine
from kkaddon.habridge import HaBridge
from kkaddon.model import Model, ModelError
from rig import KEY

HUIS = [{"role": "house", "entity": f"sensor.huis_{i}_energie", "name": f"Huisje {i}"} for i in range(1, 6)]


def test_prices_are_amounts_or_sensors():
    assert parse_price("0,31") == 0.31
    assert parse_price(" 0.25 ") == 0.25
    assert parse_price("") is None and parse_price(None) is None
    assert parse_price("sensor.stroomprijs") == "sensor.stroomprijs"
    for bad in ("veel", "-1", "99", "switch.x", True):
        with pytest.raises(ModelError):
            parse_price(bad)


def test_clean_rejects_bad_rows():
    for rows in (
        [{"role": "huis", "entity": "sensor.a"}],  # onbekende rol
        [{"role": "solar", "entity": "geen entiteit"}],
        [{"role": "battery", "entity": "sensor.a"}],  # laden ontbreekt
        [{"role": "solar", "entity": "sensor.a"}, {"role": "solar", "entity": "sensor.a"}],  # dezelfde meter twee keer met dezelfde rol
    ):
        with pytest.raises(ModelError):
            clean({"rows": rows})
    clean({"rows": [{"role": "solar", "entity": "sensor.a"}, {"role": "device", "entity": "sensor.a"}]})  # andere rol mag wel


def test_houses_count_as_grid_and_as_device_with_prices():
    cfg = clean({"rows": HUIS + [{"role": "solar", "entity": "sensor.deye_total"}, {"role": "grid_out", "entity": "sensor.deye_sold"},
                                 {"role": "battery", "entity": "sensor.batt_out", "entity_to": "sensor.batt_in"}], "price_in": "0,31", "price_out": "0,05"})
    out = merge({"energy_sources": [], "device_consumption": []}, cfg, set(), legacy=True)
    grid = out["energy_sources"][0]
    assert grid["type"] == "grid" and len(grid["flow_from"]) == 5
    assert grid["flow_from"][0] == {"stat_energy_from": "sensor.huis_1_energie", "stat_cost": None, "entity_energy_price": None, "number_energy_price": 0.31}
    assert grid["flow_to"] == [{"stat_energy_to": "sensor.deye_sold", "stat_compensation": None, "entity_energy_price": None, "number_energy_price": 0.05}]
    assert [d["name"] for d in out["device_consumption"]] == [f"Huisje {i}" for i in range(1, 6)]
    assert [s["type"] for s in out["energy_sources"]] == ["grid", "solar", "battery"]
    assert out["energy_sources"][2] == {"type": "battery", "stat_energy_from": "sensor.batt_out", "stat_energy_to": "sensor.batt_in"}


def test_new_ha_gets_one_named_grid_source_per_house_with_import_and_export():
    rows = [{"role": "house", "entity": f"sensor.huis_{i}_energie", "entity_to": f"sensor.huis_{i}_terug", "name": f"Huisje {i}"} for i in range(1, 3)]
    cfg = clean({"rows": rows + [{"role": "grid_out", "entity": "sensor.alleen_terug", "price": "0,02"}, {"role": "solar", "entity": "sensor.zon"}], "price_in": "0,31", "price_out": "0,05"})
    out = merge({"energy_sources": [], "device_consumption": []}, cfg, set())
    g = out["energy_sources"]
    assert g[0] == {"type": "grid", "stat_energy_from": "sensor.huis_1_energie", "stat_energy_to": "sensor.huis_1_terug", "stat_cost": None, "entity_energy_price": None,
                    "number_energy_price": 0.31, "stat_compensation": None, "entity_energy_price_export": None, "number_energy_price_export": 0.05, "cost_adjustment_day": 0.0, "name": "Huisje 1"}
    assert g[2]["stat_energy_from"] is None and g[2]["stat_energy_to"] == "sensor.alleen_terug" and g[2]["number_energy_price_export"] == 0.02 and g[2]["number_energy_price"] is None
    assert [x["type"] for x in g] == ["grid", "grid", "grid", "solar"] and not any("flow_from" in x for x in g)
    assert [d["name"] for d in out["device_consumption"]] == ["Huisje 1", "Huisje 2"]
    assert merge(out, cfg, {"sensor.huis_1_energie"}) == out  # opnieuw toepassen verdubbelt niets


def test_row_price_beats_default_and_a_sensor_price_is_an_entity():
    cfg = clean({"rows": [{"role": "house", "entity": "sensor.a", "price": "sensor.dynamisch"}, {"role": "grid_in", "entity": "sensor.b", "price": "0,4"},
                          {"role": "grid_in", "entity": "sensor.c"}], "price_in": "0,3"})
    f = merge({}, cfg, set(), legacy=True)["energy_sources"][0]["flow_from"]
    assert (f[0]["entity_energy_price"], f[0]["number_energy_price"]) == ("sensor.dynamisch", None)
    assert f[1]["number_energy_price"] == 0.4 and f[2]["number_energy_price"] == 0.3


def test_merge_keeps_other_peoples_lines_and_drops_removed_own_ones():
    prefs = {"energy_sources": [{"type": "grid", "flow_from": [{"stat_energy_from": "sensor.eigen", "stat_cost": None, "entity_energy_price": None, "number_energy_price": 0.1},
                                                              {"stat_energy_from": "sensor.oud_huis", "stat_cost": None, "entity_energy_price": None, "number_energy_price": 0.1}],
                                 "flow_to": [], "cost_adjustment_day": 0.0},
                                {"type": "solar", "stat_energy_from": "sensor.mijn_zon", "config_entry_solar_forecast": None},
                                {"type": "solar", "stat_energy_from": "sensor.oude_zon", "config_entry_solar_forecast": None}],
             "device_consumption": [{"stat_consumption": "sensor.tv"}, {"stat_consumption": "sensor.oud_huis", "name": "Oud"}]}
    cfg = clean({"rows": [{"role": "house", "entity": "sensor.nieuw", "name": "Nieuw"}]})
    out = merge(prefs, cfg, {"sensor.oud_huis", "sensor.oude_zon"}, legacy=True)
    grid = next(s for s in out["energy_sources"] if s["type"] == "grid")
    assert [f["stat_energy_from"] for f in grid["flow_from"]] == ["sensor.eigen", "sensor.nieuw"]
    assert [s["stat_energy_from"] for s in out["energy_sources"] if s["type"] == "solar"] == ["sensor.mijn_zon"]
    assert [d["stat_consumption"] for d in out["device_consumption"]] == ["sensor.tv", "sensor.nieuw"]
    assert merge(out, cfg, {"sensor.nieuw"}, legacy=True) == out  # opnieuw toepassen verdubbelt niets


class Env:
    def __init__(self, ha, tmp):
        self.ha, self.tmp = ha, tmp

    async def __aenter__(self):
        self.server = TestServer(self.ha.app())
        await self.server.start_server()
        self.engine = Engine(KEY, Model(), lambda raw: None)
        self.bridge = HaBridge(f"ws://127.0.0.1:{self.server.port}/api/websocket", "test", self.engine, retry=(0.05, 0.2), dashboard=False)
        self.task = asyncio.create_task(self.bridge.run())
        for _ in range(300):
            if self.bridge.connected and self.bridge.states:
                break
            await asyncio.sleep(0.01)
        self.hub = EnergyHub(self.tmp, self.bridge)
        return self

    async def __aexit__(self, *a):
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        await self.server.close()


def meters():
    ha = FakeHa(entities={f"sensor.huis_{i}_energie": "12.5" for i in range(1, 6)} | {"sensor.temp": "20"})
    for i in range(1, 6):
        ha.attrs[f"sensor.huis_{i}_energie"] = {"device_class": "energy", "state_class": "total_increasing", "unit_of_measurement": "kWh"}
    return ha


def test_candidates_are_only_energy_counters(tmp_path):
    async def go():
        async with Env(meters(), tmp_path) as env:
            assert [c["entity"] for c in env.hub.candidates()] == [f"sensor.huis_{i}_energie" for i in range(1, 6)]
    asyncio.run(go())


def test_apply_writes_to_ha_and_remembers_what_it_wrote(tmp_path):
    async def go():
        ha = meters()
        ha.energy = {"energy_sources": [], "device_consumption": [{"stat_consumption": "sensor.tv"}]}
        async with Env(ha, tmp_path) as env:
            res = await env.hub.apply({"rows": HUIS, "price_in": "0,3"})
            assert ha.energy_saves == 1 and "5 afnamemeters" in res["summary"]
            assert [d["stat_consumption"] for d in ha.energy["device_consumption"]] == ["sensor.tv"] + [f"sensor.huis_{i}_energie" for i in range(1, 6)]
            await env.hub.apply({"rows": HUIS[:2], "price_in": "0,3"})  # drie huisjes minder
            assert len(ha.energy["device_consumption"]) == 3 and ha.energy["device_consumption"][0]["stat_consumption"] == "sensor.tv"
            assert [x["stat_energy_from"] for x in ha.energy["energy_sources"]] == ["sensor.huis_1_energie", "sensor.huis_2_energie"]
            again = EnergyHub(tmp_path, env.bridge)  # herstart: instellingen en 'managed' zijn er nog
            assert len(again.cfg["rows"]) == 2 and again.managed == {"sensor.huis_1_energie", "sensor.huis_2_energie"}
    asyncio.run(go())


def test_apply_without_ha_says_so(tmp_path):
    async def go():
        hub = EnergyHub(tmp_path, HaBridge("", "", Engine(KEY, Model(), lambda raw: None)))
        with pytest.raises(ModelError, match="niet verbonden"):
            await hub.apply({"rows": HUIS})
        assert len(hub.cfg["rows"]) == 5  # wel opgeslagen
    asyncio.run(go())


def test_a_never_configured_energy_dashboard_is_filled_from_scratch(tmp_path):
    async def go():
        ha = meters()
        ha.energy = None  # HA: "No prefs"
        async with Env(ha, tmp_path) as env:
            await env.hub.apply({"rows": HUIS})
            assert len(ha.energy["device_consumption"]) == 5 and len(ha.energy["energy_sources"]) == 5
    asyncio.run(go())


def test_an_older_ha_with_flow_lists_gets_the_old_format_after_a_refusal(tmp_path):
    async def go():
        ha = meters()
        ha.energy_legacy = True
        async with Env(ha, tmp_path) as env:
            await env.hub.apply({"rows": HUIS, "price_in": "0,3"})
            assert ha.energy_saves == 1 and len(ha.energy["energy_sources"][0]["flow_from"]) == 5
            await env.hub.apply({"rows": HUIS[:2]})  # nu weet de add-on het uit wat HA terugstuurt
            assert len(ha.energy["energy_sources"][0]["flow_from"]) == 2
    asyncio.run(go())
