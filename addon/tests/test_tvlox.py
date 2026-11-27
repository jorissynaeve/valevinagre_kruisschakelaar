"""Waarden van Loxone (`x:<naam>`) als bron voor de tv-pagina, bv. bij Energie: de echte Loxone-hub, het blok op het schema, de bronkeuze en de eenheid."""
import asyncio
import time

import pytest

from kkaddon import schema
from kkaddon.loxonehub import LoxoneHub
from kkaddon.model import ModelError
from kkaddon.tvhub import TvHub
from tests.rig import Rig
from tests.test_tv import StubHa


def setup(tmp_path):
    r = Rig(tmp_path)
    r.engine.set_tz("UTC")
    r.wall = time.time()
    lox = LoxoneHub(tmp_path, r.engine, None, port=0, gap=0.001, start_delay=0, tick=0.02)
    lox.miniserver = "127.0.0.1"
    tv = TvHub(tmp_path, r.engine, port=0)
    tv.ha = StubHa()
    return r, lox, tv


def signal(lox, sid, label, kind="float"):
    lox.add_signal(sid, {"dir": "in", "ref": f"x:{sid}", "kind": kind, "label": label})


def feed(lox, **values):
    return asyncio.run(lox.apply(list(values.items())))


def test_loxone_values_are_offered_as_sources_with_their_labels(tmp_path):
    r, lox, tv = setup(tmp_path)
    signal(lox, "verbruik", "Verbruik huis")
    signal(lox, "zon", "Zonnepanelen")
    feed(lox, verbruik=1240, zon=3.8)
    blocks = schema.view(r.model, r.engine.snapshot())["blocks"]
    addon = tv.sources(blocks)["addon"]
    assert ["x:verbruik", "Loxone · Verbruik huis", "", "1240.0"] in addon and ["x:zon", "Loxone · Zonnepanelen", "", "3.8"] in addon, addon


def test_a_loxone_number_is_read_and_unknown_until_loxone_sent_something(tmp_path):
    r, lox, tv = setup(tmp_path)
    signal(lox, "verbruik", "Verbruik")
    assert tv.read("x:verbruik")[0] is None, "nog niets ontvangen: onbekend, geen nul"
    feed(lox, verbruik=1240)
    assert tv.read("x:verbruik")[:2] == (1240.0, "")
    assert tv.read("x:bestaatniet")[0] is None


@pytest.mark.parametrize("unit,raw,shown", [("W", 1240, 1.24), ("kW", 1.24, 1.24), ("", 1.24, 1.24)])
def test_energy_takes_loxone_values_with_the_chosen_unit(tmp_path, unit, raw, shown):
    r, lox, tv = setup(tmp_path)
    signal(lox, "verbruik", "Verbruik")
    signal(lox, "zon", "Zon")
    signal(lox, "batterij", "Batterij")
    feed(lox, verbruik=raw, zon=3800 if unit == "W" else 3.8, batterij=68)
    tv.update({**tv.cfg, "energy": {"consumption": "x:verbruik", "solar": "x:zon", "battery": "x:batterij", "battery_kwh": "", "grid": "", "max": 5,
                                    "units": {"consumption": unit, "solar": unit, "grid": ""}}})
    tv.sample(time.time())
    tv.sample(time.time() + 61)
    e = tv.data()["energy"]
    assert e["consumption"] == {"value": shown, "unit": "kW"} and e["solar"] == {"value": 3.8, "unit": "kW"} and e["battery"]["pct"] == 68.0
    assert [p[1] for p in e["series"]["consumption"]] == [shown, shown], "de grafiek rekent mee"
    assert [p[1] for p in e["series"]["solar"]] == [3.8, 3.8]


def test_without_a_unit_a_loxone_value_in_watts_would_show_as_kilowatts_so_the_unit_is_an_option(tmp_path):
    r, lox, tv = setup(tmp_path)
    signal(lox, "verbruik", "Verbruik")
    feed(lox, verbruik=1240)
    tv.update({**tv.cfg, "energy": {**tv.cfg["energy"], "consumption": "x:verbruik"}})
    assert tv.data()["energy"]["consumption"]["value"] == 1240.0, "automatisch: Loxone geeft geen eenheid, dus kW"
    tv.update({**tv.cfg, "energy": {**tv.cfg["energy"], "units": {"consumption": "w", "solar": "", "grid": ""}}})
    assert tv.data()["energy"]["consumption"]["value"] == 1.24 and tv.cfg["energy"]["units"]["consumption"] == "W", "W of w: opgeruimd tot W"


def test_a_unit_other_than_w_or_kw_is_refused(tmp_path):
    _, _, tv = setup(tmp_path)
    with pytest.raises(ModelError, match="W of kW"):
        tv.update({"energy": {"units": {"consumption": "MW"}}})


def test_a_ha_sensor_keeps_its_own_unit_unless_one_is_chosen(tmp_path):
    _, _, tv = setup(tmp_path)
    tv.ha.put("sensor.w", 1240, unit_of_measurement="W")
    tv.update({**tv.cfg, "energy": {**tv.cfg["energy"], "consumption": "sensor.w"}})
    assert tv.data()["energy"]["consumption"]["value"] == 1.24
    tv.update({**tv.cfg, "energy": {**tv.cfg["energy"], "units": {"consumption": "kW", "solar": "", "grid": ""}}})
    assert tv.data()["energy"]["consumption"]["value"] == 1240.0, "een gekozen eenheid gaat voor die van de sensor"


def test_other_tiles_take_loxone_values_too(tmp_path):
    r, lox, tv = setup(tmp_path)
    signal(lox, "kelder", "Kelder temperatuur")
    signal(lox, "raam", "Raam open", kind="bool")
    feed(lox, kelder=14.5, raam=1)
    tv.update({**tv.cfg, "climate": {"title": "T", "rows": [{"label": "Kelder", "source": "x:kelder", "unit": "°C", "icon": "thermo"}], "chart": ["x:kelder"]},
               "chips": [{"label": "Raam", "source": "x:raam", "icon": "door", "on_text": "Open", "off_text": "Dicht"}]})
    tv.sample(time.time())
    d = tv.data()
    assert d["climate"]["rows"][0]["value"] == 14.5 and d["climate"]["rows"][0]["unit"] == "°C"
    assert d["chips"][0]["text"] == "Open" and d["chips"][0]["on"] is True
    assert d["climate"]["series"][0][0][1] == 14.5, "de grafiek van een waarde van Loxone bouwt zich op vanaf het opstarten"
