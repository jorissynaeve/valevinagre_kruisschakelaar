"""De add-on leest wat de echte huisdev.h schrijft.

De fixtures in protocol/huisdev/tests/fixtures zijn gemaakt door de referentie-header zelf (host-test, zie die map). Zo vangen we een
verkeerde lezing van de spec die fakehuisdev.py en de add-on delen: hier komt de JSON uit de C++-code, niet uit onze eigen Python-versie.
"""
import json
import os
from pathlib import Path

import pytest

from kkaddon import espdevhub as E
from kkaddon.model import ModelError

ROOT = Path(os.environ.get("KK_PROJECT") or Path(__file__).resolve().parents[2])
FIX = ROOT / "protocol" / "huisdev" / "tests" / "fixtures"

pytestmark = pytest.mark.skipif(not FIX.is_dir(), reason="protocol/huisdev/tests/fixtures niet beschikbaar")


def load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def raw(name):
    return (FIX / name).read_bytes()


def points(desc):
    return {p["id"]: p for p in desc["points"]}


def test_discovery_van_de_header():
    d = E.parse_discovery(raw("discovery.json"), ("192.168.1.50", 47500))
    assert d and d["hid"] == "poolcontroller-a1b2c3" and d["ip"] == "192.168.1.50" and d["port"] == 80
    assert d["claimed"] is False and d["rev"] and E.proto_state(d["proto"]) == "ok"
    a = E.parse_discovery(raw("announce.json"), ("192.168.1.50", 47500))
    assert a and a["hid"] == d["hid"]


def test_descriptor_van_de_header_wordt_helemaal_begrepen():
    j = load("descriptor.json")
    desc = E.clean_descriptor(j)
    assert desc and desc["id"] == "poolcontroller-a1b2c3" and desc["rev"] == j["rev"]
    # er gaat geen punt of actie verloren in het schoonmaken
    assert len(desc["points"]) == len(j["points"])
    assert [a["id"] for a in desc["actions"]] == [a["id"] for a in j["actions"]]
    p = points(desc)
    assert p["ph"]["kind"] == "float" and p["ph"]["decimals"] == 2 and p["ph"]["stale"] == 120000
    assert p["temp"]["unit"] == "°C"  # UTF-8 uit de C++-kant komt heel aan
    assert p["mode_electro"]["kind"] == "enum" and [o["v"] for o in p["mode_electro"]["options"]] == [0, 1, 2]
    assert p["ph_set"]["writable"] and (p["ph_set"]["min"], p["ph_set"]["max"], p["ph_set"]["step"]) == (6.8, 7.6, 0.1)
    assert p["alarm_no_flow"]["group"] == "alarmen" and not p["alarm_no_flow"]["writable"]
    assert p["calibrate"]["danger"] and p["calibrate"]["confirm"]


def test_state_past_bij_de_descriptor():
    desc = E.clean_descriptor(load("descriptor.json"))
    st = load("state.json")
    assert st["rev"] == desc["rev"]
    ids = set(points(desc))
    assert set(st["points"]) <= ids
    assert st["points"]["ph"]["v"] == 7.2 and st["points"]["temp"]["v"] is None  # onbekend is null, nooit 0
    assert st["actions"]["flush"]["disabled"] is True
    assert "flush" in {a["id"] for a in desc["actions"]}


def test_waarden_volgens_de_regels_van_de_header():
    p = points(E.clean_descriptor(load("descriptor.json")))
    assert E.check_value(p["ph_set"], 7.2) == pytest.approx(7.2)
    assert E.check_value(p["ph_set"], 6.8) == pytest.approx(6.8)  # de stap telt vanaf min
    with pytest.raises(ModelError):
        E.check_value(p["ph_set"], 7.25)  # geen stap van 0,1
    with pytest.raises(ModelError):
        E.check_value(p["ph_set"], 8.0)  # boven max
    with pytest.raises(ModelError):
        E.check_value(p["mode_electro"], 7)  # geen optie
    assert E.check_value(p["mode_electro"], 2) == 2


def test_dynamische_wijziging_geeft_een_andere_rev_en_de_rest_blijft():
    a, b = load("descriptor.json"), load("descriptor_rev2.json")
    assert a["rev"] != b["rev"]
    da, db = E.clean_descriptor(a), E.clean_descriptor(b)
    assert da and db and da["id"] == db["id"]
    pa, pb = points(da), points(db)
    # bij een nieuwe rev verandert een bestaand id nooit van type
    for pid in set(pa) & set(pb):
        assert pa[pid]["kind"] == pb[pid]["kind"], pid
    assert set(pa) != set(pb) or any(pa[i] != pb[i] for i in set(pa) & set(pb))


def test_foutantwoorden_van_de_header_hebben_het_vaste_formaat():
    for f in FIX.glob("error_*.json"):
        status, code = f.stem.split("_")[1], f.stem.split("_", 2)[2]
        j = json.loads(f.read_text(encoding="utf-8"))
        assert j["ok"] is False and isinstance(j["error"], str) and "reason" in j, f.name
        assert status.isdigit() and 400 <= int(status) < 500
        # de bestandsnaam bevat het foutcode-deel (state-401 heet er 'state' achter)
        assert j["error"] in (code, code.replace("state", "token")), f.name


def test_set_en_do_antwoorden():
    for name, result in (("set_applied.json", "applied"), ("set_queued.json", "queued"), ("set_rejected.json", "rejected"), ("do_queued.json", "queued")):
        j = load(name)
        assert j["result"] == result and j["ok"] is (result != "rejected"), name
